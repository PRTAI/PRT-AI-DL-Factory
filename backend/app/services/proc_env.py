"""子进程环境修正：让框架能找到自己那套 conda 环境里的可执行文件。

平台自己跑在 ``prt-ai-dl-factory`` 环境里，但训练/评测/部署用的是各框架自己的
解释器。``os.environ.copy()` 带过去的 ``PATH`` 是**平台**的 bin 目录，于是框架
在运行时 shell 出去调工具就会失败，比如：

* vLLM 的 torch.compile 要 ``ninja`` → ``FileNotFoundError: 'ninja'``
* LLaMA-Factory 多卡要 ``torchrun``
* verl 要 ``ray``
* DeepSpeed 要 ``deepspeed`` / ``ninja``

这些工具明明装在框架自己的环境里（``<env>/bin/``），只是不在 PATH 上。
所以启动任何子进程之前，都要把它那个解释器的 bin 目录挪到 PATH 最前面
—— 等价于 ``conda activate <env>`` 的核心效果。
"""

from __future__ import annotations

import os
from pathlib import Path


def with_interpreter_on_path(env: dict[str, str], executable: str) -> dict[str, str]:
    """就地把 ``executable`` 所在目录插到 ``env["PATH"]`` 最前面并返回 env。

    ``executable`` 可以是绝对路径（``/opt/conda/envs/verl/bin/python``）或裸名
    （``python``）；后者说明用的是 PATH 上的解释器，无需改动。
    """
    bin_dir = _bin_dir_of(executable)
    if bin_dir is None:
        return env

    current = env.get("PATH") or os.defpath
    parts = current.split(os.pathsep)
    if parts and parts[0] == bin_dir:
        return env
    env["PATH"] = os.pathsep.join([bin_dir, *(p for p in parts if p != bin_dir)])
    return env


def _bin_dir_of(executable: str) -> str | None:
    if not executable or os.sep not in executable:
        return None  # 裸命令名：已经在 PATH 上
    parent = Path(executable).resolve().parent
    return str(parent) if parent.is_dir() else None
