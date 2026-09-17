# PRT AI DL Factory

团队内部的**模型后训练 + Agent 评测**平台。在一个 Web 控制台里提交训练与评测任务，
平台负责生成框架命令、审核排队、分配 GPU、回传日志与指标。

- 后端：FastAPI（Python 3.12）+ 文件存储（可迁移 MySQL）
- 前端：React 19 + Vite + Tailwind v4，蓝白配色，实时曲线
- 训练：LLaMA-Factory（SFT/DPO/KTO/ORPO/SimPO）、verl（GRPO/DAPO/PPO/…）、TRL（GRPO/GSPO + Harbor agentic RL）
- 评测：Harbor（任意 agent × pass@k × 并发）

## 快速开始

```bash
scripts/setup.sh                  # 一次性：建 conda 环境、装依赖、生成适配表
scripts/start_frontend.sh build   # 构建前端
scripts/start_backend.sh          # 启动服务
```

- 控制台 http://\<主机\>:8000
- 接口文档 http://\<主机\>:8000/api/docs

首次启动自动创建管理员 `admin@prt.local` / `prt-admin-2026`，**请立即改密**，
或在 `backend/.env` 里用 `PRT_BOOTSTRAP_ADMIN_EMAIL` / `PRT_BOOTSTRAP_ADMIN_PASSWORD` 覆盖。

开发模式：

```bash
RELOAD=1 scripts/start_backend.sh   # 后端 :8000，热重载
scripts/start_frontend.sh           # 前端 :5153，自动代理 /api 与 /ws
```

## 使用流程

1. 上传数据集（Alpaca / ShareGPT，或 Harbor 任务树 zip），平台校验格式。
2. 提交训练或评测：选算法、模型、数据集，套用预设，申请 GPU。
3. 管理员在任务列表里查看平台生成的**完整命令与配置**，审核放行。
4. 任务详情页实时看日志与 loss/pass@k 曲线，结束后可一键评测产物。

任务状态：`待审核 → 排队 → 运行 → 完成/失败/终止`。
正式评测前建议先跑 `oracle`（应接近 1.0）和 `nop`（应为 0）验证任务集。

## 常用命令

```bash
PYTHONPATH=$PWD python -m pytest        # 后端测试
cd frontend && npm run build            # 前端构建
python scripts/gen_framework_support.py # 框架升级后重新生成适配表
python scripts/manage.py list-users     # 离线管理 CLI（建号/改密/清理）
```

## 配置

复制 `backend/.env.example` 为 `backend/.env`，常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRT_DATA_ROOT` | `./data` | 运行时数据目录 |
| `PRT_MODELS_ROOT` | `/workspace/.../models` | 模型仓库 |
| `PRT_*_PYTHON` | 空 | 各框架所在环境的解释器 |
| `PRT_MAX_CONCURRENT_JOBS` | `2` | 全局并发 |
| `PRT_REQUIRE_APPROVAL` | `true` | 是否强制审核 |

训练框架各自装在独立 conda 环境，平台只需知道用哪个 python 启动，自身无需 torch。

## 注意

- 只能单进程运行（`--workers 1`）：调度器与遥测采样是进程内单例。
- 命令行参数中的 API Key 在界面/数据库/`run.sh` 中已脱敏，但同机 `ps` 可见，共享主机请用项目级密钥。
- Harbor 评测需要 docker 权限：`sudo usermod -aG docker $USER`。
- 备份 `data/db/` 与 `data/audit/` 即可保住账号与任务元数据。

详细文档见 [`docs/`](docs/)：架构、部署、使用说明、MySQL 迁移。
