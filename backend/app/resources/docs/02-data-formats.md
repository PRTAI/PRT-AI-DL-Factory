---
title: 数据格式说明
category: 入门
order: 2
summary: 七种上传格式的目录结构、必需字段与示例
---

# 数据格式说明

上传的是 **zip / tar.gz**，平台解压后按所选格式校验；字段不对会当场告诉你缺什么，不会等到训练时才炸。

## 格式速查

| 格式 | 用于 | 必需字段 / 文件 |
|---|---|---|
| `sft_alpaca` | SFT | `instruction` + `output` |
| `sft_sharegpt` | SFT（多轮 / 工具调用） | `conversations` |
| `dpo_pairs` | DPO / ORPO / SimPO | `chosen` + `rejected` |
| `kto_pairs` | KTO | `messages` + `label` |
| `harbor_tasks` | Agent 评测 / Agentic RL | `task.toml` + `instruction.md` + `environment/` |
| `verl_parquet` | verl RL | `train.parquet` |
| `raw` | 任意文件 | 不校验 |

> 文件名含 `eval` / `valid` / `test` / `dev` 的会被自动识别为验证集。

---

## SFT · Alpaca

```
dataset.zip
├── train.json
└── eval.json      # 可选
```

```json
[
  {
    "instruction": "解释一下 GRPO 与 PPO 的区别。",
    "input": "",
    "output": "GRPO 去掉了价值网络，用同一 prompt 的一组采样回报做基线……",
    "system": "你是一个严谨的算法助手。"
  }
]
```

## SFT · ShareGPT（多轮 / 工具调用）

```json
[
  {
    "conversations": [
      { "from": "human", "value": "帮我查一下今天的 GPU 占用" },
      { "from": "gpt", "value": "好的，我调用监控接口……" }
    ],
    "tools": "[{\"name\": \"get_gpu\", \"parameters\": {}}]"
  }
]
```

`from` 取值：`human` / `gpt` / `function_call` / `observation` / `system`

## 偏好对齐 · DPO / ORPO / SimPO

```json
[
  {
    "conversations": [{ "from": "human", "value": "写一个快速排序" }],
    "chosen": { "from": "gpt", "value": "def quicksort(a): ..." },
    "rejected": { "from": "gpt", "value": "你可以自己搜一下。" }
  }
]
```

也支持扁平写法：`{"instruction": ..., "chosen": "...", "rejected": "..."}`

## KTO

```json
[{ "messages": [{ "role": "user", "content": "总结这段日志" },
                { "role": "assistant", "content": "训练在第 300 步收敛……" }],
   "label": true }]
```

`label` 为 `true` 表示期望的回复，`false` 表示不期望。

---

## Harbor 任务树（评测 / Agentic RL）

```
dataset.zip
└── tasks/
    └── my-task-001/
        ├── task.toml          # 必需
        ├── instruction.md     # 必需
        ├── environment/
        │   └── Dockerfile     # 必需，缺失会被平台拒绝上传
        ├── tests/
        │   ├── test.sh        # verifier 入口，写出 reward
        │   └── reward.toml    # 可选，reward-kit 打分规则
        └── solution/
            └── solve.sh       # 可选，oracle agent 的参考解
```

`task.toml` 最小可用版本：

```toml
schema_version = "1.4"

[task]
name = "prt/my-task-001"
version = "1.0.0"

[metadata]
difficulty = "easy"
category = "programming"

[agent]
timeout_sec = 300.0

[verifier]
timeout_sec = 120.0

[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 2048
storage_mb = 10240
```

`tests/test.sh` 把分数写进 `/logs/verifier/reward.txt`：

```bash
#!/bin/bash
grep -q hello /app/answer.txt \
  && echo 1 > /logs/verifier/reward.txt \
  || echo 0 > /logs/verifier/reward.txt
```

### reward-kit judge

judge 在**任务容器内**运行，所以 Dockerfile 要自带 uv：

```dockerfile
FROM ubuntu:24.04
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
```

```bash
# tests/test.sh
uvx --from harbor-rewardkit rewardkit /tests
```

judge 需要的 API Key 填在提交评测页的「Verifier 环境变量」里，平台会用
`--verifier-env` 传入，并写入 0600 的 `.env` 供 `task.toml` 中 `${VAR}` 插值解析。
密钥不会出现在日志与命令记录中。

---

## verl parquet

```
dataset.zip
├── train.parquet
└── test.parquet    # 可选
```

每行字段：`data_source`、`prompt`（消息数组）、`reward_model.ground_truth`、`extra_info`

## 常见校验失败

| 报错 | 原因 |
|---|---|
| 缺少必需字段（需要 …） | 选错格式，或 JSON 首条记录字段名不对 |
| 压缩包包含非法路径 | zip 里有 `../` 或绝对路径，重新打包 |
| N 个任务缺少 instruction.md | Harbor 任务树层级不对，检查是否多套了一层目录 |
| N 个任务缺少 environment/ 目录 | Harbor 会跳过没有环境定义的任务，必须补上 |
| 未找到 .json / .jsonl 数据文件 | 压缩包里是别的格式，或文件在多层子目录中 |
