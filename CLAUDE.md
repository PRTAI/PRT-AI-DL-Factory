# PRT AI DL Factory — 给协作者 / AI 助手的项目说明

团队内部的模型后训练（SFT/DPO/GRPO/DAPO）与 Harbor Agent 评测平台。
FastAPI + React，文件存储，通过子进程调用外部训练/评测框架。

## 常用命令

```bash
# 环境（一次）
scripts/setup.sh

# 后端（含已构建前端）
scripts/start_backend.sh                 # :8000
RELOAD=1 scripts/start_backend.sh        # 开发模式

# 前端
scripts/start_frontend.sh                # vite dev :5173（代理 /api /ws 到 :8000）
scripts/start_frontend.sh build          # 产出 frontend/dist

# 测试与构建（提交前都要跑）
conda activate prt-ai-dl-factory
PYTHONPATH=$PWD python -m pytest         # 62 passed
cd frontend && npm run build             # tsc -b && vite build

# 框架升级后重新生成适配表
python scripts/gen_framework_support.py
```

## 代码结构约定

- `backend/app/api/` 只做参数解析与权限判断，业务逻辑一律放 `services/`。
- 持久化只允许经过 `backend/app/store/`。这是将来换 MySQL 的唯一替换点，
  services 里不要出现文件路径读写数据库的代码。
- `backend/app/builders/` 负责「配置 → argv」。要点：
  - argv 直接 `Popen`，**不经过 shell**；
  - 密钥放进 `BuildResult.secrets`，渲染时会被替换成 `***`；
  - **参数名以本机框架源码为准**，不要照抄文档。`backend/tests/test_config_keys.py`
    会用框架自己的源码（LLaMA-Factory hparams dataclass、verl 的 Hydra 配置树、
    Harbor 的 Typer 签名）校验我们生成的每一个键，改 builder 后必须跑这个测试。
- 解析框架日志的代码（`services/metrics_parser.py`）必须是防御式的：
  任何异常都返回空结果，绝不能影响正在运行的任务。
- 前端设计令牌集中在 `frontend/src/index.css`（`@theme inline` + CSS 变量），
  组件里不要写死颜色，用 `bg-card` / `text-fg-muted` 这类语义类名，
  否则浅色主题会失效。

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
- LLaMA-Factory 训练结束时会**补写一条只有进度、没有 loss/lr 的记录**，解析摘要时必须取
  「最后一个非空值」而不是「最后一条记录」（TRL 的 `train_runtime` 收尾记录同理），
  已有回归测试 `test_llamafactory_final_progress_record_keeps_last_values`。

## 不要做的事

- 不要把运行时数据（`data/`）、`frontend/dist`、`node_modules` 提交进仓库。
- 不要在 uvicorn 上开多 worker：调度器与遥测采样是进程内单例。
- 不要把密钥写进 `run.sh` / `command.json` / 接口返回；用 `BuildResult.secrets`。
