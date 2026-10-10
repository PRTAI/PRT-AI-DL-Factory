"""压缩包解压：路径穿越/符号链接防护，以及嵌套压缩包的递归展开。

数据集上传与样例审核都要解压用户上传的包，防护实现只能有一份，所以放在这里。

递归展开只有样例审核用得到：供应商交付的题包往往是「外层一个 zip，里面按领域再套
若干 zip」。内层包展开到同级的 ``<包名>!`` 目录，这样磁盘相对路径和报告里要写的逻辑
路径（``…/PRT-金融-会计-20260918.zip!/PRT-金融-会计-20260918/FIN-WF-006``）完全一致。
"""

from __future__ import annotations

import shutil
import stat
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException, status

NOISE_NAMES = {"__MACOSX", "__MACOSX/", ".DS_Store", "._.DS_Store"}
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2")
# 嵌套包最多展开几层：交付包实测两层，留一层余量，再深就是异常输入。
MAX_ARCHIVE_DEPTH = 3
MAX_MEMBERS = 200_000


def _is_noise(name: str) -> bool:
    parts = Path(name).parts
    return any(p in NOISE_NAMES for p in parts) or name.startswith("__MACOSX")


def is_within(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def looks_like_archive(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def safe_extract(archive: Path, dest: Path) -> None:
    """Extract zip/tar defending against path traversal and symlink escapes."""
    dest.mkdir(parents=True, exist_ok=True)
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            members = zf.infolist()
            _guard_count(len(members))
            for member in members:
                name = member.filename
                if name.startswith("/") or ".." in Path(name).parts:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, detail=f"压缩包包含非法路径: {name}"
                    )
                target = dest / name
                if not is_within(dest, target):
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"压缩包越界: {name}")
            zf.extractall(dest)
        return

    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            members = tf.getmembers()
            _guard_count(len(members))
            for member in members:
                if member.issym() or member.islnk():
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="压缩包包含符号链接，已拒绝")
                target = dest / member.name
                if member.name.startswith("/") or ".." in Path(member.name).parts or not is_within(dest, target):
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST, detail=f"压缩包包含非法路径: {member.name}"
                    )
            tf.extractall(dest)  # noqa: S202 - members validated above
        return

    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="仅支持 .zip / .tar / .tar.gz 压缩包")


def _guard_count(count: int) -> None:
    if count > MAX_MEMBERS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"压缩包条目过多（{count}），已拒绝"
        )


def flatten_single_root(root: Path) -> None:
    """``foo.zip`` that contains a single ``foo/`` directory is unwrapped."""
    entries = [p for p in root.iterdir() if p.name not in {"__MACOSX", ".DS_Store"}]
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        # 外层目录可能与其中一道任务同名，先腾空原路径再上移子项。
        # 用 rename 指定精确目标，避免 shutil.move 把内容套进已存在的目录。
        staging = Path(tempfile.mkdtemp(prefix=".prt-flatten-", dir=root))
        staged_inner = staging / "contents"
        moved: list[Path] = []
        try:
            inner.rename(staged_inner)
            for child in list(staged_inner.iterdir()):
                if child.name in NOISE_NAMES:
                    continue
                destination = root / child.name
                if destination.exists() or destination.is_symlink():
                    raise FileExistsError(f"目录展开目标已存在: {destination}")
                child.rename(destination)
                moved.append(destination)
        except OSError:
            # 移动失败时恢复原结构；若恢复也失败，保留暂存内容而不删除数据。
            if staged_inner.exists():
                for destination in reversed(moved):
                    destination.rename(staged_inner / destination.name)
                staged_inner.rename(inner)
            staging.rmdir()
            raise
        shutil.rmtree(staging)


# --------------------------------------------------------------------------- #
# 递归展开（样例审核用）
# --------------------------------------------------------------------------- #
@dataclass
class ExtractResult:
    """一次递归展开的结果。

    ``modes`` 是「压缩包里记录的权限位」，key 为相对 ``root`` 的路径（内层包用 ``!/``
    分隔，因为内层包就展开在 ``<包名>!`` 目录里，所以磁盘相对路径和报告里的逻辑路径
    是同一个字符串）。

    权限位必须以这份记录为准：Python 的 ``extractall`` 不保留可执行位，直接看磁盘
    会把所有脚本都读成 644，从而把整批题误判为「缺可执行位」。
    """

    root: Path
    modes: dict[str, int] = field(default_factory=dict)
    inner_archives: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def extract_recursive(archive: Path, dest: Path) -> ExtractResult:
    """展开 ``archive`` 到 ``dest``，内层压缩包一并展开。

    返回的 ``modes`` 里每个 key 都是相对 ``dest`` 的逻辑路径，内层包用 ``!/`` 分隔。
    同时按记录的权限位回写磁盘副本，让后续读文件的代码看到和包里一致的 mode。
    """
    result = ExtractResult(root=dest)
    _extract_into(archive, dest, prefix="", result=result, depth=0)
    return result


def _extract_into(archive: Path, dest: Path, *, prefix: str, result: ExtractResult, depth: int) -> None:
    if depth > MAX_ARCHIVE_DEPTH:
        result.notes.append(f"嵌套压缩包超过 {MAX_ARCHIVE_DEPTH} 层，未展开：{prefix or archive.name}")
        return

    recorded = _record_modes(archive)
    safe_extract(archive, dest)
    for rel, mode in recorded.items():
        logical = f"{prefix}{rel}" if prefix else rel
        result.modes[logical] = mode
        target = dest / rel
        if mode and target.is_file() and is_within(dest, target):
            # 只回写权限位，且抹掉 setuid/setgid/sticky
            target.chmod(mode & 0o777)

    for inner in sorted(dest.rglob("*")):
        if not inner.is_file() or not looks_like_archive(inner):
            continue
        rel = inner.relative_to(dest).as_posix()
        if _is_noise(rel):
            continue
        logical = f"{prefix}{rel}" if prefix else rel
        if logical in result.inner_archives:
            continue
        inner_dest = inner.parent / f"{inner.name}!"
        if inner_dest.exists():
            continue
        result.inner_archives.append(logical)
        try:
            _extract_into(
                inner, inner_dest, prefix=f"{logical}!/", result=result, depth=depth + 1
            )
        except HTTPException as exc:  # 内层包坏了不该让整次审核挂掉
            result.notes.append(f"内层压缩包无法展开（{logical}）：{exc.detail}")
            shutil.rmtree(inner_dest, ignore_errors=True)


def _record_modes(archive: Path) -> dict[str, int]:
    """读出压缩包里记录的 unix 权限位；包没记录（Windows 打的包）则不返回该条目。"""
    modes: dict[str, int] = {}
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                mode = member.external_attr >> 16
                if stat.S_IMODE(mode):
                    modes[member.filename] = stat.S_IMODE(mode)
        return modes
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as tf:
            for member in tf.getmembers():
                if member.isfile() and member.mode:
                    modes[member.name] = stat.S_IMODE(member.mode)
    return modes
