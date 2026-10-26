"""Runtime configuration for the PRT AI DL Factory backend.

Everything that points at the outside world (model zoo, framework checkouts,
python interpreters used to launch trainings) lives here so a new deployment
only has to touch environment variables or ``backend/.env``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / "backend" / ".env", REPO_ROOT / ".env"),
        env_prefix="PRT_",
        extra="ignore",
    )

    # ---------------------------------------------------------------- app ---
    app_name: str = "PRT AI DL Factory"
    app_version: str = "1.0.0"
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # Comma separated list, or "*" during development.
    cors_origins: str = "*"

    # --------------------------------------------------------------- auth ---
    # Generated on first boot and persisted to data/db/secret.key when unset.
    secret_key: str = ""
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60 * 12
    bootstrap_admin_email: str = "admin@prt.local"
    bootstrap_admin_password: str = "prt-admin-2026"

    # -------------------------------------------------------------- paths ---
    data_root: Path = REPO_ROOT / "data"
    models_root: Path = Path("/workspace/LiHanlin_Workspace/models")

    llamafactory_root: Path = Path("/workspace/LiHanlin_Workspace/github/LlamaFactory")
    verl_root: Path = Path("/workspace/LiHanlin_Workspace/github/verl")
    trl_root: Path = Path("/workspace/LiHanlin_Workspace/github/trl")
    harbor_root: Path = Path("/workspace/LiHanlin_Workspace/github/harbor")

    # Per-framework python interpreter. Empty -> falls back to `python_default`.
    python_default: str = "python"
    llamafactory_python: str = ""
    verl_python: str = ""
    trl_python: str = ""
    harbor_python: str = ""
    harbor_bin: str = "harbor"
    modelscope_bin: str = "modelscope"
    # 模型部署用的解释器（需要装 vllm）；留空则依次回落到 verl / trl / 默认解释器
    vllm_python: str = ""
    deploy_port_start: int = 8100
    deploy_port_end: int = 8199

    # ---------------------------------------------------------- scheduling ---
    max_concurrent_jobs: int = 2
    scheduler_interval_seconds: float = 3.0
    monitor_interval_seconds: float = 2.0
    monitor_history_points: int = 900  # ~30 min at 2s resolution
    log_tail_bytes: int = 200_000
    max_upload_mb: int = 0  # 单次上传大小上限，0 表示不限制

    # Jobs must be approved by an admin before they run. Kept configurable so a
    # single-user install can opt out; the UI surfaces it as a system setting.
    require_approval: bool = True

    # ------------------------------------------------------ 样例审核（质检） ---
    # 审核不占 GPU，只花模型 API 额度，所以自己限流而不是走 GPU 调度队列。
    review_concurrency: int = 4           # 同时在跑的 subagent 数
    review_batch_tasks: int = 20          # 一个 subagent 一次判定多少道题
    review_max_tasks: int = 500           # 单次审核最多受检题数，超出即拒绝
    review_evidence_bytes: int = 8000     # 每份证据文本截断长度
    review_batch_chars: int = 60_000      # 一次 subagent 请求里证据 JSON 的字符上限
    review_agent_timeout_seconds: float = 900.0
    review_agent_image: str = "prt-ai-factory-harbor-eval-base-image:latest"
    review_max_doc_chars: int = 120_000   # 需求文档送进 prompt 的上限

    # ------------------------------------------------- 问题反馈（托管修复） ---
    # Agent 只在仓库副本里改代码，副本目录必须在仓库之外，不影响正在运行的平台。
    issue_repo_root: Path = REPO_ROOT
    issue_workspace_root: Path = Path("~/prt-agent-workspaces")
    issue_agent_bin: str = "claude"
    issue_agent_model: str = ""           # 空 = 本机 Claude Code 的默认模型
    issue_agent_permission_mode: str = "acceptEdits"
    # acceptEdits 下 Bash 需要白名单；git push 一律禁止，推送只由平台在合并时做
    issue_agent_allowed_tools: str = (
        "Read,Edit,Write,Glob,Grep,Bash(git:*),Bash(python:*),Bash(python3:*),Bash(pytest:*),"
        "Bash(npm run:*),Bash(npm test:*),Bash(node:*),Bash(ls:*),Bash(cd:*)"
    )
    issue_agent_disallowed_tools: str = (
        "Bash(git push:*),Bash(git remote:*),Bash(git checkout main:*),Bash(git switch main:*),"
        "Bash(git reset --hard:*),Bash(npm install:*),Bash(npm i:*),Bash(pip:*),Bash(python -m pip:*),WebFetch,WebSearch"
    )
    issue_agent_timeout_seconds: float = 3600.0
    issue_main_branch: str = "main"
    issue_push_remote: str = "origin"
    issue_auto_restart: bool = True
    # 非空时用这条 shell 命令重启（例如 systemctl restart prt）；空则平台自己重新拉起 uvicorn
    issue_restart_command: str = ""

    @field_validator(
        "data_root",
        "models_root",
        "llamafactory_root",
        "verl_root",
        "trl_root",
        "harbor_root",
        "issue_repo_root",
        "issue_workspace_root",
        mode="before",
    )
    @classmethod
    def _expand(cls, value: object) -> object:
        if isinstance(value, str):
            path = Path(os.path.expanduser(value))
            # 相对路径（如 .env 里的 PRT_DATA_ROOT=./data）按仓库根解析成绝对路径。
            # 子进程的 cwd 是 job 目录，若把 ./data/jobs/<id>/.env 这样的相对路径塞进
            # 命令行，会被再拼接一次（harbor: "Env file not found"），--jobs-dir 同理。
            return path if path.is_absolute() else REPO_ROOT / path
        return value

    # ------------------------------------------------------- derived paths ---
    @property
    def db_dir(self) -> Path:
        return self.data_root / "db"

    @property
    def datasets_dir(self) -> Path:
        return self.data_root / "datasets"

    @property
    def uploads_dir(self) -> Path:
        return self.data_root / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_root / "jobs"

    @property
    def outputs_dir(self) -> Path:
        return self.data_root / "outputs"

    @property
    def downloads_dir(self) -> Path:
        return self.data_root / "downloads"

    @property
    def audit_dir(self) -> Path:
        return self.data_root / "audit"

    @property
    def deployments_dir(self) -> Path:
        return self.data_root / "deployments"

    @property
    def exports_dir(self) -> Path:
        return self.data_root / "exports"

    @property
    def reviews_dir(self) -> Path:
        return self.data_root / "reviews"

    @property
    def prebuilds_dir(self) -> Path:
        return self.data_root / "prebuilds"

    @property
    def issues_dir(self) -> Path:
        return self.data_root / "issues"

    @property
    def resources_dir(self) -> Path:
        return Path(__file__).resolve().parent / "resources"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_root,
            self.db_dir,
            self.datasets_dir,
            self.uploads_dir,
            self.jobs_dir,
            self.outputs_dir,
            self.downloads_dir,
            self.audit_dir,
            self.deployments_dir,
            self.exports_dir,
            self.reviews_dir,
            self.prebuilds_dir,
            self.issues_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def python_for(self, framework: str) -> str:
        mapping = {
            "llamafactory": self.llamafactory_python,
            "verl": self.verl_python,
            "trl": self.trl_python,
            "harbor": self.harbor_python,
        }
        return mapping.get(framework) or self.python_default

    def python_for_vllm(self) -> str:
        """vLLM 装在哪个环境里：显式配置 > verl > trl > 默认解释器。"""
        return self.vllm_python or self.verl_python or self.trl_python or self.python_default

    def root_for(self, framework: str) -> Path | None:
        mapping = {
            "llamafactory": self.llamafactory_root,
            "verl": self.verl_root,
            "trl": self.trl_root,
            "harbor": self.harbor_root,
        }
        return mapping.get(framework)

    @property
    def cors_origin_list(self) -> list[str]:
        raw = (self.cors_origins or "").strip()
        if not raw or raw == "*":
            return ["*"]
        return [origin.strip() for origin in raw.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    if not settings.secret_key:
        settings.secret_key = _load_or_create_secret(settings.db_dir / "secret.key")
    return settings


def _load_or_create_secret(path: Path) -> str:
    if path.exists():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    import secrets

    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return value


settings = get_settings()
