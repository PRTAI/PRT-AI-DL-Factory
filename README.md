# PRT AI DL Factory

团队内部的**模型后训练 + Agent 评测**一体化平台：在一个 Web 控制台里提交 SFT / DPO / GRPO / DAPO
训练与 Harbor Agent 评测，平台负责生成框架命令、审核排队、分配 GPU、回传日志与指标。

- **后端** FastAPI（Python 3.12），文件存储，可平滑迁移到 MySQL
- **前端** React 19 + Vite + Tailwind v4 + Radix，深色优先，实时折线图
- **训练框架** LLaMA-Factory（SFT/DPO/KTO/ORPO/SimPO）、verl（GRPO/DAPO/PPO/RLOO/…）、TRL（GRPO/GSPO + Harbor agentic RL）
- **评测框架** Harbor（pass@k、并发、任意 agent、reward-kit judge）

---

## 目录

- [功能总览](#功能总览)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [使用流程](#使用流程)
- [框架适配](#框架适配)
- [配置项](#配置项)
- [运维](#运维)
- [测试](#测试)
- [已知限制](#已知限制)

---

## 功能总览

### 账号与权限
- 账号由**管理员创建**，邮箱登录，不开放自助注册；首次登录强制修改初始密码。
- 两种角色：**管理员**（审核/终止任务、管理成员、系统设置）与**成员**（提交任务、查看自己的任务与日志）。
- 全量审计日志（登录、任务提交/审核、账号变更、数据集与模型操作）+ 站内通知。
- 支持内网邮箱域（`@corp.local`、`@corp.internal` 等不会被拒绝）。

### 任务
| 类型 | 框架 | 算法 |
|---|---|---|
| 监督/偏好训练 | LLaMA-Factory | SFT、DPO、KTO、ORPO、SimPO |
| 强化学习 | verl | GRPO、DAPO、PPO、RLOO、REINFORCE++、ReMax |
| 强化学习 | TRL | GRPO、GSPO、RLOO、DPO、KTO；**Harbor 任务树作为 agentic RL 环境** |
| Agent 评测 | Harbor | 任意 agent × pass@k × 并发 |

任务生命周期：`待审核 → 排队 → 运行 → 完成/失败/终止`。
**训练与评测都必须经管理员审核**（可在系统设置里关闭）。审核人可以在任务详情页看到平台生成的
**完整命令与配置文件**再决定是否放行。

### 调度
- 按卡分配 GPU（`CUDA_VISIBLE_DEVICES`），全局并发上限 + 每人并发/显卡配额。
- 子进程以独立进程组启动，终止时整组信号（torchrun worker、ray actor、docker 驱动的 harbor trial 一并回收）。
- 平台重启后会**对账**：进程已消失但状态还停在 running 的任务会被标记失败，不会永远挂着。

### 可观测
- 实时日志（WebSocket 推送，支持关键字过滤 / 自动滚动 / 下载）。
- 指标曲线：LLaMA-Factory 的 `trainer_log.jsonl`、verl 的 `step:N - k:v` 控制台输出、
  TRL 的 `trainer_state.json`、Harbor 的 `result.json`（pass@k、reward 分布、token 与花费）。
- 数据大屏：CPU / 内存 / GPU 利用率与显存的滚动折线（类似任务管理器，可暂停）、每张卡的温度/功耗/占用、
  磁盘与网络吞吐、运行中任务进度。

### 资源
- **模型仓库**：扫描 `/workspace/LiHanlin_Workspace/models` 下的 `{厂家}/{模型名}`，解析参数量、架构、上下文长度，
  并自动匹配 LLaMA-Factory 的对话模板；未收录的模型可直接粘贴 ModelScope ID 后台下载。
- **数据集**：上传 zip/tar.gz → 安全解压（防目录穿越）→ 按格式校验 → 登记到规范目录，
  并为 LLaMA-Factory 生成 `dataset_info.json`。
- **适配速查文档**：在提交页一键打开，内容由 `scripts/gen_framework_support.py`
  从本机框架源码解析而来（635 个模型的 template 映射、verl 的优势估计器、TRL trainer、Harbor agent 列表）。

---

## 快速开始

```bash
# 1) 一次性环境准备（创建 conda 环境 prt-ai-dl-factory、安装依赖、生成适配表、装前端依赖）
scripts/setup.sh

# 2) 构建前端（生产模式；开发时可跳过，见下）
scripts/start_frontend.sh build

# 3) 启动服务（同时提供 API 与前端页面）
scripts/start_backend.sh
# → http://<主机>:8000        控制台
# → http://<主机>:8000/api/docs  OpenAPI 文档
```

首次启动会自动创建管理员账号：

| 邮箱 | 密码 |
|---|---|
| `admin@prt.local` | `prt-admin-2026` |

**请立即登录并修改密码**，或在启动前用 `backend/.env` 覆盖
`PRT_BOOTSTRAP_ADMIN_EMAIL` / `PRT_BOOTSTRAP_ADMIN_PASSWORD`。

### 开发模式

```bash
RELOAD=1 scripts/start_backend.sh   # 后端 :8000，改代码自动重启
scripts/start_frontend.sh           # 前端 :5173，/api 与 /ws 自动代理到 :8000
```

---

## 目录结构

```
PRT-AI-DL-Factory/
├── backend/
│   ├── app/
│   │   ├── main.py           FastAPI 入口（API + WebSocket + 静态前端）
│   │   ├── config.py         全部可配置项（PRT_* 环境变量）
│   │   ├── security.py       bcrypt + JWT
│   │   ├── deps.py           鉴权依赖与角色校验
│   │   ├── models/           领域模型（pydantic）
│   │   ├── store/            文件存储（JSON + filelock），MySQL 的替换点
│   │   ├── services/         业务逻辑：账号 / 数据集 / 模型 / 任务 / 调度 / 监控 / 指标解析
│   │   ├── builders/         配置 → 可执行命令（llamafactory / verl / trl / harbor）
│   │   ├── api/              路由（薄层）
│   │   └── resources/        适配表与预设（framework_support.json 由脚本生成）
│   └── tests/                59 个测试，含端到端跑一次真实调度
├── frontend/                 React 控制台
├── scripts/
│   ├── setup.sh              环境准备
│   ├── start_backend.sh      启动 API
│   ├── start_frontend.sh     启动/构建前端
│   ├── manage.py             离线管理 CLI（建号、改密、清理）
│   └── gen_framework_support.py  从框架源码生成适配表
├── docs/                     架构 / 部署 / 使用说明
└── data/                     运行时数据（已 gitignore）
    ├── db/                   users.json、jobs.json、datasets.json…
    ├── datasets/<id>/        解压后的数据集
    ├── jobs/<id>/            run.sh、配置文件、stdout.log、metrics.json
    ├── outputs/<id>/         checkpoint / harbor 结果
    └── audit/audit.jsonl     审计日志
```

---

## 使用流程

### 提交一次 SFT
1. **数据集**页上传 zip（Alpaca 或 ShareGPT 格式），平台校验字段并显示样例。
2. **提交训练**页：填名称与备注 → 选算法 `SFT` → 选模型（template 自动带出）→ 选数据集
   → 套用预设（LoRA 快速验证 / LoRA 标准 / 全参 ZeRO-3）→ 按需微调超参 → 申请 GPU 数 → 提交。
3. 管理员在**任务列表**或**平台管理 → 待审核**里查看生成的 `llamafactory-cli train train_config.yaml`
   与完整 YAML，通过后任务进入排队。
4. 任务详情页实时看 loss/lr 曲线与日志；结束后可一键「评测该产物」。

### 提交一次 Agent 评测
1. 上传 **harbor_tasks** 类型的数据集（`tasks/<task>/task.toml` + `instruction.md` + `environment/` + `tests/`）。
2. **提交评测**页：选任务集 → 选 agent（`oracle` 自检 / `nop` 基线 / `claude-code`、`codex` 等真实 agent）
   → 填模型与 API Key → 设 pass@k 与并发 → 提交。
3. 结果回填 pass@1..pass@k、reward 分布、错误数、token 与花费。

> 建议正式评测前先跑一次 `oracle`（应接近 1.0）和一次 `nop`（应为 0），确认任务集本身可靠。

### Harbor 数据格式做 RL
选择 **GRPO/GSPO** + **harbor_tasks** 数据集时，平台会自动把框架切到 **TRL**，
用 `trl.experimental.harbor.HarborSpec` 把任务树变成 RL 环境，奖励直接取自任务 verifier。
verl 不吃 Harbor 任务树，需要 parquet（平台会明确拒绝并提示）。

---

## 框架适配

`backend/app/resources/framework_support.json` 由脚本从**本机框架源码**解析生成，升级框架后重跑即可：

```bash
python scripts/gen_framework_support.py
```

当前解析结果（本机版本）：

| 框架 | 版本 | 解析内容 |
|---|---|---|
| LLaMA-Factory | 0.9.6.dev0 | 635 个模型条目、111 套对话模板、ModelScope 路径 |
| verl | 0.9.0.dev | 14 种优势估计器、Megatron 已注册模型、transformers 加速补丁 |
| TRL | 1.10.0.dev0 | 6 个 Trainer、Harbor 集成与 3 种 harness |
| Harbor | 0.22.0 | 43 个 agent、26 种沙箱后端 |

`backend/tests/test_config_keys.py` 会**用框架自己的源码校验平台生成的每个配置键**：
LLaMA-Factory 的 hparams dataclass 字段、verl 的 Hydra 配置树、Harbor 的 Typer 参数签名。
框架升级后如果改了参数名，测试会直接失败，而不是等训练跑起来才炸。

---

## 配置项

复制 `backend/.env.example` 为 `backend/.env` 后按需修改，常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRT_DATA_ROOT` | `./data` | 平台数据目录 |
| `PRT_MODELS_ROOT` | `/workspace/LiHanlin_Workspace/models` | 模型仓库根目录 |
| `PRT_LLAMAFACTORY_ROOT` / `PRT_VERL_ROOT` / `PRT_TRL_ROOT` / `PRT_HARBOR_ROOT` | 见示例 | 框架源码位置 |
| `PRT_LLAMAFACTORY_PYTHON` 等 | 空 | 各框架使用的 python 解释器，留空则用 `PRT_PYTHON_DEFAULT` |
| `PRT_HARBOR_BIN` | `harbor` | Harbor 可执行文件 |
| `PRT_MODELSCOPE_BIN` | `modelscope` | ModelScope CLI（找不到会自动探测常见路径） |
| `PRT_MAX_CONCURRENT_JOBS` | `2` | 全局并发任务数（管理员可在界面覆盖） |
| `PRT_REQUIRE_APPROVAL` | `true` | 任务是否必须审核 |
| `PRT_MAX_UPLOAD_MB` | `8192` | 单个压缩包上限 |

> 训练框架依赖很重，通常各自装在独立 conda 环境里。平台只需要知道**用哪个 python 启动**，
> 因此 `PRT_*_PYTHON` 指向对应环境的解释器即可，平台自身环境不必安装 torch。

---

## 运维

```bash
python scripts/manage.py list-users                        # 看账号
python scripts/manage.py create-user ops@team.com --role admin
python scripts/manage.py reset-password ops@team.com        # 随机新密码
python scripts/manage.py unlock-admin                       # 管理员全部失联时恢复
python scripts/manage.py gc --days 30 --dry-run             # 清理旧任务工作目录（产物保留）
```

- **备份**：`data/db/`（账号与任务元数据）+ `data/audit/`。数据集与产物按需备份。
- **迁移到 MySQL**：见 [`docs/mysql-migration.md`](docs/mysql-migration.md)，只需替换 `backend/app/store/`。
- **多副本**：调度器与监控采样是进程内单例，因此当前只支持单进程（`--workers 1`）。
  需要横向扩展时先迁移到共享数据库，再把调度拆成独立进程。

---

## 测试

```bash
conda activate prt-ai-dl-factory
PYTHONPATH=$PWD python -m pytest          # 59 passed
cd frontend && npm run build              # tsc + vite 构建
```

测试覆盖：鉴权与 RBAC、数据集校验（含 zip-slip 防护）、任务生命周期
（**真实跑通一次调度：排队 → 启动 → 退出 → 状态回写**，以及运行中终止）、
四个命令构建器、四种日志/指标解析、WebSocket 日志与遥测流、密钥脱敏、框架配置键一致性。

---

## 已知限制

- 训练/评测进程的命令行参数（含通过 `--agent-env` 传入的 API Key）在同机器上可被 `ps` 看到。
  平台在数据库、`run.sh`、界面中都会脱敏，但共享主机上仍建议用只读的项目级密钥。
- Harbor CLI 需要单独安装；未安装时评测页会提前告警，任务可以提交但会在启动时失败。
- verl 当前版本没有把 DAPO 的 `overlong_buffer` 接进配置系统，平台会跳过该项并在任务备注中说明。
- 文件存储适合团队规模（数千条任务记录）；再大需要迁移到 MySQL。
