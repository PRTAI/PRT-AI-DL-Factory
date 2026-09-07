# 架构说明

## 分层

```
        浏览器 (React SPA)
              │  REST /api      WebSocket /ws
              ▼
   ┌──────────────────────────────────────────────┐
   │ FastAPI                                      │
   │  api/        路由（薄层，只做参数与权限）      │
   │  services/   业务逻辑                         │
   │  builders/   配置 → argv                      │
   │  store/      持久化（JSON 文件，可换 MySQL）   │
   └──────────────────────────────────────────────┘
        │ subprocess (独立进程组)
        ▼
  llamafactory-cli / verl.trainer.main_ppo / 生成的 TRL 脚本 / harbor run
```

后台常驻两个 asyncio 任务，由 FastAPI 的 lifespan 启动：

| 任务 | 周期 | 职责 |
|---|---|---|
| `scheduler` | 3s | 对账运行中任务、刷新指标、按 GPU 与并发额度派发排队任务 |
| `monitor` | 2s | 采样 CPU/内存/GPU/磁盘/网络，写入内存环形缓冲（默认 900 点 ≈ 30 分钟） |

## 任务状态机

```
                    ┌───────────── 管理员驳回 ─────────────┐
                    │                                     ▼
 提交 ──► pending_review ──审核通过──► queued ──派发──► preparing ──► running
                    │                    │                              │
              （关闭审核时直接进 queued）  └──── 用户/管理员取消 ────────┐  │
                                                                     ▼  ▼
                                                   cancelled / failed / succeeded
```

- `queued → preparing` 时才真正分配显卡编号（写入 `resources.gpu_ids`）。
- `preparing → running` 由 `runner.launch()` 完成：写 `run.sh`、配置文件、打开日志文件、`Popen(start_new_session=True)`。
- 子进程退出由**监督线程**回写状态；如果平台在此期间重启，调度器的对账逻辑会兜底。

## 目录约定

每个任务独享两个目录：

```
data/jobs/<job_id>/          # 输入与过程
├── run.sh                   # 可复现的启动脚本（密钥已脱敏，0750）
├── command.json             # 实际 argv + env（脱敏）
├── train_config.yaml        # LLaMA-Factory 配置（SFT 类任务）
├── verl_overrides.txt       # verl 的 Hydra 覆盖清单（RL 任务）
├── train_*.py               # TRL 生成的训练脚本（RL 任务）
├── lf_data/dataset_info.json# 合并后的 LLaMA-Factory 数据索引
├── .env                     # Harbor 评测的密钥（0600，仅评测任务）
├── stdout.log               # 合并的 stdout/stderr
└── metrics.json             # 解析出的曲线与摘要

data/outputs/<job_id>/       # 产物
├── checkpoint-*/            # 训练 checkpoint
└── <job_id>/                # Harbor 的 job 目录（config.json / result.json / trial 目录）
```

## 命令构建（builders/）

每个 builder 输入 `Job` + 两个目录，输出 `BuildResult`：

```python
@dataclass
class BuildResult:
    command: list[str]        # argv，直接 exec，不经过 shell
    cwd: str
    env: dict[str, str]
    files: dict[str, str]     # 相对工作目录的文件 → 内容
    output_dir: str
    notes: list[str]          # 展示给用户/审核人的说明
    mkdirs: list[str]
    secrets: list[str]        # 需要在渲染时替换成 *** 的字面值
```

要点：

- **不经过 shell**：argv 直接传给 `Popen`，不存在命令注入面。`run.sh` 只是给人看的复现脚本。
- **密钥脱敏**：`masked_command_line()` / `masked_env()` 用于写入数据库、`run.sh`、接口返回。
- **版本敏感的参数以源码为准**：Harbor 0.22 把 `--task-name` 改成了 `--include-task-name`、
  `--sharegpt` 改成了 `--export-sharegpt`，并且没有 `--shuffle/--seed/--no-cache`；
  verl 当前版本没有把 `overlong_buffer` 接进配置系统。这些都在 builder 里按实际源码处理，
  并由 `tests/test_config_keys.py` 持续校验。

## 指标解析（services/metrics_parser.py）

| 框架 | 数据来源 | 解析出的曲线 |
|---|---|---|
| LLaMA-Factory | `<output>/trainer_log.jsonl` | loss、eval_loss、lr、reward、accuracy、throughput、进度与 ETA |
| verl | stdout 里的 `step:N - key:value` | reward、score、pg_loss、entropy、kl、grad_norm、response_length |
| TRL | `<output>/**/trainer_state.json`，回退到 stdout 的 dict 行 | loss、lr、reward、grad_norm、kl |
| Harbor | `<jobs>/<job>/result.json` | pass@k、reward 分布、trial 计数、错误数、token、花费 |

解析器全部是**防御式**的：任何异常都返回空结果，绝不影响正在跑的任务。
曲线点数超过 1500 会等距抽稀并保留最后一个点。

## 前端

- 路由：`/`（大屏）、`/jobs`、`/jobs/:id`、`/submit/train`、`/submit/verify`、`/datasets`、`/models`、`/admin`、`/settings`。
- 数据获取：TanStack Query（轮询 + 失效刷新）；实时部分用两个 WebSocket：
  `/ws/system`（遥测）与 `/ws/jobs/{id}/log`（日志 + 进度 + 指标）。
- 设计令牌集中在 `src/index.css`，通过 `@theme inline` 把 CSS 变量暴露成 Tailwind 工具类，
  因此 `.theme-dark` / `.theme-light` 可以在运行时整体换肤。
