# 使用指南

## 1. 账号

账号由管理员在「平台管理 → 账号管理」创建，创建时会生成初始密码（可点「随机生成」）。
成员首次登录后会看到黄色提示条，请到「个人设置」修改密码。

| 能力 | 成员 | 管理员 |
|---|---|---|
| 提交训练 / 评测任务 | ✅ | ✅ |
| 查看自己的任务、日志、指标 | ✅ | ✅ |
| 查看**所有人**的任务 | ❌ | ✅ |
| 审核 / 驳回任务 | ❌ | ✅ |
| 终止任务 | 仅自己的 | 全部 |
| 上传 / 删除数据集 | ✅ / 仅自己的 | ✅ / 全部 |
| 下载模型 | ✅ | ✅ |
| 管理账号、系统设置、审计日志 | ❌ | ✅ |

## 2. 数据集格式

上传前在「数据集 → 格式说明」里能看到同样的内容；下面是完整版。

### 2.1 SFT · Alpaca

```
dataset.zip
├── train.json      # JSON 数组
└── eval.json       # 可选；文件名含 eval/valid/test/dev 会自动识别为验证集
```

```json
[
  {
    "instruction": "解释一下 GRPO 与 PPO 的区别。",
    "input": "",
    "output": "GRPO 去掉了价值网络，用同一 prompt 的一组采样回报做基线……",
    "system": "你是一个严谨的算法助手。",
    "history": [["上一轮问题", "上一轮回答"]]
  }
]
```

必需字段：`instruction` + `output`。

### 2.2 SFT · ShareGPT（多轮 / 工具调用）

```json
[
  {
    "conversations": [
      { "from": "human", "value": "帮我查一下今天的 GPU 占用" },
      { "from": "gpt", "value": "好的，我调用监控接口……" }
    ],
    "system": "你是运维助手",
    "tools": "[{\"name\": \"get_gpu\", \"parameters\": {}}]"
  }
]
```

`from` 取值：`human` / `gpt` / `function_call` / `observation` / `system`。

### 2.3 偏好对齐 · DPO / ORPO / SimPO

```json
[
  {
    "conversations": [{ "from": "human", "value": "写一个快速排序" }],
    "chosen": { "from": "gpt", "value": "def quicksort(a): ..." },
    "rejected": { "from": "gpt", "value": "你可以自己搜一下。" }
  }
]
```

也支持扁平写法 `{"instruction": ..., "chosen": "...", "rejected": "..."}`。

### 2.4 KTO

```json
[
  {
    "messages": [
      { "role": "user", "content": "总结这段日志" },
      { "role": "assistant", "content": "训练在第 300 步 loss 收敛……" }
    ],
    "label": true
  }
]
```

### 2.5 Harbor 任务树（评测 / agentic RL）

```
dataset.zip
└── tasks/
    └── my-task-001/
        ├── task.toml          # 必需
        ├── instruction.md     # 必需
        ├── environment/
        │   └── Dockerfile     # 必需（缺失会被 Harbor 直接跳过，平台会拒绝上传）
        ├── tests/
        │   ├── test.sh        # verifier 入口，写出 reward
        │   └── reward.toml    # 可选，reward-kit 打分规则
        └── solution/
            └── solve.sh       # 可选，oracle agent 的参考解
```

```toml
version = "1.0"

[metadata]
difficulty = "easy"
category = "programming"
tags = ["bash"]

[agent]
timeout_sec = 300.0

[verifier]
timeout_sec = 120.0

[environment]
build_timeout_sec = 600.0
cpus = 1
memory = "2G"
storage = "10G"
```

需要 API Key 的 judge（reward-kit / LLM judge）在 `task.toml` 里写成插值即可：

```toml
[verifier.env]
ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"
```

提交评测时把这个 key 填到「Verifier 环境变量」，平台会用 `--verifier-env` 传给 verifier，
同时写入任务目录下 0600 权限的 `.env` 并用 `--env-file` 传给 Harbor，`${VAR}` 插值就能解析到。

judge 本身在**任务容器内**运行，所以 `tests/test.sh` 通常是：

```bash
#!/bin/bash
uvx --from harbor-rewardkit rewardkit /tests
```

对应的 Dockerfile 需要带上 uv：

```dockerfile
FROM ubuntu:24.04
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
```

### 2.6 verl parquet

```
dataset.zip
├── train.parquet
└── test.parquet
```

每行：`data_source`、`prompt`（消息数组）、`reward_model.ground_truth`、`extra_info`。

## 3. 提交训练

### 关键参数

| 参数 | 说明 | 经验值 |
|---|---|---|
| 微调方式 | LoRA / 全参 / 冻结 | 迭代期用 LoRA；效果收尾再考虑全参 |
| LoRA rank / alpha | 适配器容量 | alpha ≈ 2 × rank；rank 8 起步，难任务上 32/64 |
| 学习率 | — | LoRA `1e-4`，全参 `1e-5`，偏好对齐 `5e-6` |
| cutoff_len | 截断长度 | 覆盖 95% 样本即可，越长显存越吃紧 |
| batch × 梯度累积 | 等效 batch | 等效 batch = 单卡 batch × 累积 × 卡数，常见 32~128 |
| DeepSpeed | ZeRO 级别 | 多卡全参用 ZeRO-3；LoRA 一般不需要 |
| pref_beta | 偏好强度 | DPO `0.1`；SimPO `2.0~2.5` |

### RL 特有

| 参数 | 说明 |
|---|---|
| rollout n | 组大小。GRPO/DAPO 建议 8~16，越大方差越小、越慢 |
| max_response_length | 长 CoT 任务给到 8192+ |
| clip low / high | DAPO 的解耦裁剪，论文用 `0.2 / 0.28` |
| 动态采样 filter_groups | 丢弃全对/全错的组并重采，DAPO 的关键技巧 |
| 显存占用比例 | 留给 vLLM 的显存，训练 OOM 时先调低这个 |
| 张量并行度 | 不能大于申请的 GPU 数（平台会拦） |

框架选择：
- **verl** —— 大规模、需要 Megatron/多机、数据是 parquet。
- **TRL** —— 与 HF 生态一致、需要 **Harbor 任务树做 agentic RL**、或者要 GSPO。

## 4. 提交评测

| 参数 | 对应 Harbor 参数 | 说明 |
|---|---|---|
| pass@k | `--n-attempts / -k` | 每题尝试次数，结果给出 pass@1..pass@k |
| 并发 | `--n-concurrent / -n` | 同时跑的 trial 数，受本机 CPU/内存与 Docker 限制 |
| Agent | `--agent / -a` | `oracle` 自检、`nop` 基线、`claude-code` / `codex` / `terminus` 等 |
| 模型 | `--model / -m` | litellm 格式，如 `anthropic/claude-sonnet-4-5` |
| Agent 环境变量 | `--agent-env` | 注入 agent 容器的 API Key |
| Verifier 环境变量 | `--verifier-env` | judge 打分用的 Key，同时写入 `.env` |
| 沙箱后端 | `--env / -e` | `docker` / `e2b` / `daytona` / `modal` / … |
| 任务过滤 | `--include-task-name` / `--exclude-task-name` / `--n-tasks` | glob 模式 |
| 超时倍数 | `--timeout-multiplier` | 慢模型可调到 2.0 |
| 导出轨迹 | `--export-traces` (+`--export-sharegpt`) | 评测轨迹可直接拿去做 SFT |

**推荐流程**：`oracle`（应 ≈1.0）→ `nop`（应 =0）→ 真实 agent pass@1 → 需要统计显著性时上 pass@4。

### 评测自己训练出来的模型

1. 用 vLLM 起一个 OpenAI 兼容服务：
   ```bash
   python -m vllm.entrypoints.openai.api_server \
     --model /path/to/data/outputs/<job_id> --served-model-name local-model --port 8000
   ```
2. 提交评测：agent 选 `terminus`，模型填 `hosted_vllm/local-model`，
   Agent 环境变量加 `OPENAI_API_BASE=http://<宿主机 IP>:8000/v1` 与 `OPENAI_API_KEY=EMPTY`。
   注意容器内访问宿主机不能用 `127.0.0.1`。

## 5. 看结果

- **任务详情 → 概览**：进度、关键指标、运行信息、配置摘要。
- **实时日志**：WebSocket 推送，可过滤关键字、下载完整日志。
- **指标曲线**：训练看 loss/lr/reward；评测看 reward 分布与 pass@k。
- **配置与命令**：平台生成的完整命令、环境变量与配置文件，可直接复制到终端复现。
- **产物**：checkpoint 或 Harbor 的 job 目录（含每个 trial 的轨迹）。

标签页支持深链接，例如 `/jobs/<id>?tab=logs` 可以直接发给同事。
