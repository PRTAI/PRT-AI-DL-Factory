# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

团队内部的模型后训练（SFT/DPO/GRPO/DAPO）与 Harbor Agent 评测平台。
FastAPI + React，文件存储，通过子进程调用外部训练/评测框架。

## 常用命令

```bash
# 环境（一次）
scripts/setup.sh

# 后端（含已构建前端）
scripts/start_backend.sh                 # :8000，接口文档 /api/docs
RELOAD=1 scripts/start_backend.sh        # 开发模式

# 前端
cd frontend && npm install               # node_modules 不在仓库里，首次必须装
scripts/start_frontend.sh                # vite dev :5153（代理 /api /ws 到 :8000）
scripts/start_frontend.sh build          # 产出 frontend/dist（有 dist 时后端直接托管 SPA）

# 测试与构建（提交前都要跑）
conda activate prt-ai-dl-factory
PYTHONPATH=$PWD python -m pytest         # 114 passed（见下「跑测试的前提」）
PYTHONPATH=$PWD python -m pytest backend/tests/test_builders.py::test_verl_grpo_overrides   # 单个用例
cd frontend && npm run build             # tsc -b && vite build

# 框架升级后重新生成适配表
python scripts/gen_framework_support.py

# 谁都登录不进来时的离线管理 CLI（要用和服务相同的 PRT_* 环境）
python scripts/manage.py list-users | create-user | reset-password | unlock-admin | gc
```

### 跑测试的前提

- `backend/.env` 必须存在，且 `PRT_TRL_PYTHON` 等框架解释器填**绝对路径**。
  TRL builder 的 `_known_config_keys()` 是从解释器所在环境的 site-packages 里读
  transformers `TrainingArguments` 的字段来做白名单的；解释器是裸名 `python` 时这部分是空集，
  于是 `per_device_train_batch_size` / `output_dir` 这些**继承字段会被静默过滤掉**，
  `test_trl_generation_batch_is_divisible_by_group_size` 随之失败。
  按 `backend/.env.example` 复制一份再跑；本机 trl 环境是 `/home/user/.conda/envs/trl/bin/python`。
- `backend/tests/conftest.py` 在 `import backend.app` **之前**设置 `PRT_*`（settings 是 import 期单例），
  新增需要环境变量的测试必须写在那一段里，不能在测试函数内 `os.environ[...]`。

## 架构要点

请求链路：`api/`（只做参数解析与权限判断）→ `services/`（业务逻辑）→ `builders/`（配置 → argv）
→ `services/runner.py`（`Popen`）；一切持久化经 `store/`。

进程内有两个 asyncio 常驻循环，由 FastAPI lifespan 启动，**因此只能单 worker**：

| 循环 | 周期 | 职责 |
|---|---|---|
| `services/scheduler.py` | 3s | 对账运行中任务（进程没了就置 failed）、刷新指标、按并发与 GPU 额度派发排队任务 |
| `services/monitor.py` | 2s | 采样 CPU/内存/GPU/磁盘/网络，写内存环形缓冲（900 点 ≈ 30 分钟，无 TSDB） |

任务状态机（`JobStatus`）：
`draft → pending_review →（审核）queued → preparing → running → succeeded/failed/cancelled`。

- `queued → preparing` 时才真正分配显卡编号并写入 `resources.gpu_ids`。
- 子进程退出由 runner 的**监督线程**回写状态；平台若在此期间重启，靠 scheduler 的对账兜底。
- 任务维度：`JobType` = sft / rl / verify，`Framework` = llamafactory / verl / trl / harbor，
  `builders/__init__.py` 按 framework 分派；哪个算法能吃哪种数据集在 `job_service._ALLOWED_DATASETS`。

每个任务独享两个目录：`data/jobs/<job_id>/`（`run.sh`、`command.json`、生成的配置/脚本、
`stdout.log`、`metrics.json`）和 `data/outputs/<job_id>/`（checkpoint / Harbor job 目录）。

**不走审核与调度队列的三个子系统**（改调度相关代码时最容易漏）：

- `deployment_service`：vLLM 常驻 OpenAI 兼容服务，没有「跑完」，靠轮询 `/v1/models` 定状态。
  它的占卡通过 `busy_gpu_ids()` 合并进 `scheduler.busy_gpu_ids()`，否则会把已被部署占住的卡再派给训练。
- `export_service`：用 `llamafactory.cli export` 把 LoRA 合并回底座，纯 CPU、不占 GPU。
- `review_service`（样例审核）：按需求文档逐题静态验收样例包，出固定格式 docx 返修报告。
  不占 GPU、只花模型 API 额度，跑的是事件循环里的 `asyncio.Task` 而不是子进程——所以重启
  连对账的余地都没有，`reconcile()` 直接把非终态的置 failed。

- `prebuild_service`（评测前预构建容器）：对选中的 Harbor 任务跑 `docker build` / `compose build` / `pull`，
  记录只在进程内存里、日志在 `data/prebuilds/`，重启即丢，不需要对账。

- `issue_service`（问题反馈 / 托管修复）：管理员触发后把仓库 clone 到 `PRT_ISSUE_WORKSPACE_ROOT/<issue_id>`（仓库之外），在 `fix/issue-<id>` 分支上跑本机 `claude -p`（独立会话、禁止 push），完成后置 `pending_merge`；合并由管理员确认，平台在运行目录 merge → push → 有前端改动时 build → `os.execv` 自重启。合并提交的署名按「个人设置里的 Git 署名 > 绑定的 GitHub 账号 > 平台账号名称与邮箱」取，总是用 `-c user.name/-c user.email` 显式传（运行目录可能没有任何 git 身份）；推送时如果操作人绑定了 GitHub 账号就用它的 token（通过 git 配置环境变量传、不走命令行参数），未绑定则用平台本机的 git 凭据。`reconcile()` 对仍存活的 Agent 继续等待，已退出的按副本 git 状态收尾，合并中断的退回 `pending_merge`。

前三者的进程都不归调度器管，所以 lifespan 里各自 `reconcile()` 一次，否则重启后会永远挂在 serving/running。

指标解析来源：LLaMA-Factory 读 `<output>/trainer_log.jsonl`；verl 解析 stdout 的 `step:N - key:value`；
TRL 读 `trainer_state.json` 并回退到 stdout；Harbor 读 `<job>/result.json`。

静态目录 `backend/app/resources/`：`framework_support.json`（由 `scripts/gen_framework_support.py`
从本机框架源码生成，不要手改）、`algorithms.json`、`dataset_specs.json`、`docs/*.md`
（front-matter + GFM，按 mtime 缓存，改完刷新页面即可，不用重启）。

前端：TanStack Query 轮询 + 两个 WebSocket（`/ws/system` 遥测、`/ws/jobs/{id}/log` 日志+进度+指标）。
浏览器的 WS 握手带不了 header，所以 token 走 query 参数（见 `deps.get_ws_user`）。

## 代码结构约定

- `backend/app/api/` 只做参数解析与权限判断，业务逻辑一律放 `services/`。
- 持久化只允许经过 `backend/app/store/`。这是将来换 MySQL 的唯一替换点，
  services 里不要出现文件路径读写数据库的代码；新集合在 `store/registry.py` 里注册。
- `backend/app/builders/` 负责「配置 → argv」。要点：
  - argv 直接 `Popen`，**不经过 shell**（`run.sh` 只是给人看的复现脚本）；
  - 密钥放进 `BuildResult.secrets`，渲染时会被替换成 `***`；
  - **参数名以本机框架源码为准**，不要照抄文档。`backend/tests/test_config_keys.py`
    会用框架自己的源码（LLaMA-Factory hparams dataclass、verl 的 Hydra 配置树、
    Harbor 的 Typer 签名）校验我们生成的每一个键，改 builder 后必须跑这个测试。
- 启动任何子进程前都要过 `services/proc_env.with_interpreter_on_path()`：
  平台自己在 `prt-ai-dl-factory` 环境里，不把目标解释器的 bin 目录提到 PATH 最前面，
  框架运行时 shell 出去调 `ninja` / `torchrun` / `ray` / `deepspeed` 会 FileNotFoundError。
- 解析框架日志的代码（`services/metrics_parser.py`、`services/harbor_trials.py`）必须是防御式的：
  任何异常都返回空结果，绝不能影响正在运行的任务。
- 样例审核（`services/review_*.py`）有两条不能破的规矩：**证据只由平台采集**
  （`review_evidence.py`，无 LLM），模型只在证据上判定；**质检项只能来自需求文档原文**——
  每项必须带 `quote`，平台归一化空白后回查文档，定位不到就丢弃并记进 `notes`。
  模型不许落盘任何产物（不写不跑脚本），交付物只有 `report.docx`。
- 前端设计令牌集中在 `frontend/src/index.css`（`@theme inline` + CSS 变量），
  组件里不要写死颜色，用 `bg-card` / `text-fg-muted` / `bg-primary` 这类语义类名，
  否则换肤会失效。配色是**蓝白**：primary `#2563EB`，浅色为默认主题，
  深色主题同样收敛在蓝色系；状态色（成功绿/警告琥珀/危险红）保持语义化不跟着变蓝。

## 本机环境（已装好，勿重复安装）

| conda 环境 | 用途 | 关键版本 |
|---|---|---|
| `prt-ai-dl-factory` | 平台自身（FastAPI） | python 3.12，不含 torch |
| `llamafactory` | SFT/DPO/KTO/ORPO/SimPO | torch 2.14+cu130, transformers 5.8.0（可编辑安装，指向检出） |
| `trl` | TRL GRPO/GSPO/DPO | torch 2.11+cu130, transformers 5.16.1, vllm 0.26.0（只装依赖） |
| `verl` | verl GRPO/DAPO/PPO | torch 2.13+cu130, transformers 5.10.4, vllm 0.28.0, ray 2.58.0（只装依赖） |
| `harbor` | Harbor 评测 CLI | harbor 0.22.0 + harbor-rewardkit 0.2.0 |

`github/` 下四个检出都是 **root 只读**：TRL / verl 不能 `pip install -e`（写不了 egg-info），
所以只装依赖、代码通过 PYTHONPATH 指向检出——这正是平台启动它们的方式，不要改成从 PyPI 装框架本体，
否则跑的就不是团队检出的那份代码了。

Docker 守护进程在跑但当前用户不在 `docker` 组，Harbor 评测需要先
`sudo usermod -aG docker $USER`。

## 已知的框架适配坑

- Harbor 0.22：`--include-task-name`（不是 `--task-name`）、`--export-sharegpt`
  （不是 `--sharegpt`）、`--override-cpus/-memory-mb/-gpus`、`--force-build`；
  没有 `--shuffle/--seed/--no-cache/--no-color`。
- Harbor `--path` 指向的是**任务集目录**（其子目录才是任务），
  单个任务目录也能识别；任务缺少 `environment/` 会被 Harbor 静默跳过，
  所以上传数据集时就直接拒绝。
- verl 当前版本没有把 DAPO 的 `overlong_buffer` 接进配置系统，
  builder 会跳过并在 notes 里说明，不要凭空发 `+reward_model.overlong_buffer.*`。
- LLaMA-Factory 会在 `get_device_count() > 1` 时自动切 torchrun，
  所以多卡只需要设置 `FORCE_TORCHRUN` / `NPROC_PER_NODE`，不要自己拼 torchrun。
- verl 只吃 parquet；Harbor 任务树做 RL 走 TRL 的 `trl.experimental.harbor.HarborSpec`。
- 本机这版 TRL 的 `GRPOConfig` 已经没有 `max_prompt_length`，`loss_type` 只有 GRPOConfig 有
  （RLOOConfig 没有）；发一个不存在的键会在 `XxxConfig(**CONFIG)` 那行直接 TypeError，
  而那时已经排队拿到 GPU 了——所以 builder 里有 `_drop_unknown_keys()` 兜底。
- Harbor 每个 trial 的结果文件是 `<trial>/result.json`（**单数**），
  尽管它自己 `TrialPaths` 的 docstring 写的是 `results.json`。用错文件名的后果是
  Trial 明细页永远空白，且真实任务里不会报错。
- Harbor `JobStats.increment()` 对**每个**结束的 trial 都 `n_completed += 1`，
  报错的再额外 `n_errored += 1` —— errored 是 completed 的子集，两者相加会凭空多出 trial。
  通过率必须用 `成功数 / completed`，否则 9/10 崩掉的评测会显示 100%。
- LLaMA-Factory 训练结束时会**补写一条只有进度、没有 loss/lr 的记录**，解析摘要时必须取
  「最后一个非空值」而不是「最后一条记录」（TRL 的 `train_runtime` 收尾记录同理），
  已有回归测试 `test_llamafactory_final_progress_record_keeps_last_values`。

## 不要做的事

- 不要把运行时数据（`data/`）、`frontend/dist`、`node_modules` 提交进仓库。
- 不要在 uvicorn 上开多 worker：调度器与遥测采样是进程内单例。
- 不要把密钥写进 `run.sh` / `command.json` / 接口返回；用 `BuildResult.secrets`，
  接口层只返回密钥尾号（`model_api_service`），落盘的 `model_apis.json` 是 0600。

更长的说明见 `docs/`：`architecture.md`、`deployment.md`、`user-guide.md`、`mysql-migration.md`；
面向平台用户的帮助文档在 `backend/app/resources/docs/`（会在控制台里渲染）。
