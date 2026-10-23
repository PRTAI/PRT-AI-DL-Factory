# 训练、评测与恢复

先查询当前 `/resources/frameworks`、`/resources/algorithms`、`/resources/harbor-agents`、`/system/metrics`、`/system/settings`。用 `schema --model JobCreate` 及目标 `VerifyConfig` / `SFTConfig` / `RLConfig` 获取真实字段。下面是构造请求的示例骨架，不包含真实资源 ID。

## 提交与状态

`POST /jobs` 必须带可用的 `project_id`、`type`（`verify` / `sft` / `rl`）和相应配置块。顶层不填写任意 `framework`：SFT 固定 LLaMA-Factory，RL 在 `rl.framework` 指定，评测固定 Harbor。远程 API 评测通常 `resources.n_gpus=0`；训练和本地部署根据用户指定资源申请，不擅自扩大 GPU 数量。

`pending_review` 等待管理员审核，`queued` 等待资源，`preparing` 准备中，`running` 正在执行。终态包括 `succeeded`、`failed`、`cancelled`、`rejected`。管理员提交会自动审核通过；成员可能需要审核。需要用户已授权审批且账号具备管理员权限时，`POST /jobs/{id}/review`：`{"approve":true,"comment":"..."}`；不要反复提交来跳过审批。

创建返回 ID 后读取 `/jobs/{id}`，并可查询 `/jobs/{id}/command` 验证最终命令（该预览会生成任务工作文件）；路径和参数应对应请求。执行观测见 [client.md](client.md)。

## Harbor

```json
{
  "name": "实验-claude",
  "type": "verify",
  "project_id": "prj_ID",
  "resources": {"n_gpus": 0},
  "verify": {
    "dataset_id": "ds_ID",
    "agent": "claude-code",
    "model_api_id": "mdl_ID",
    "n_concurrent": 1,
    "n_attempts": 1,
    "environment_type": "docker"
  }
}
```

- 任务来源使用 `dataset_id`、`tasks_path`、`registry_dataset` 中对应的一种；不能把本地压缩包路径填入 `tasks_path`。有过滤需求时用 `task_names`、`exclude_task_names`、`n_tasks`，以服务端 Agent/catalog 能力为准。
- `model_api_id` 复用个人供应商密钥；`deployment_id` 复用本地就绪部署；或 `model` + `agent_env` 手动接入。优先前两种，不混用。`oracle` / `nop` 不要求模型。
- Claude Code 原生使用 Anthropic；启用 `use_openai_bridge:true` 后可使用 GPT 等支持 OpenAI Responses 的模型，`agent_api_protocol` 填 `openai`。Codex 也使用 Responses（该 API 字段填 `openai`，不自行填写 `responses`）。不要把供应商的模型前缀擅自删除或补加。
- RewardKit 数据集有评分时需选 `judge_model_api_id`；`judge_api_protocol` 为 `anthropic` / `openai`，`batch_judge` 控制 batched 评分。`disable_verification=true` 只用于用户要求的无评分运行，不能为绕过缺少 Judge 配置而偷偷启用。
- `agent_kwargs` 是字符串字典；Codex 原生设置放在 `agent_kwargs.config` 的 JSON **字符串**中。当前平台补充 MCP 启动等待，但资源列表为空仍不表示没有 MCP tools。不要因一次工具选择失败就重配 MCP 或替换 harness。
- `timeout_multiplier` 默认 1；阶段倍率为 `agent_timeout_multiplier` / `verifier_timeout_multiplier`。大倍率会把数小时任务拉长至数天，不能作为默认排障手段。

多 Agent 同数据集：`POST /jobs/verify-batch`，schema 为 `VerifyBatchCreate`。

```json
{
  "common": {
    "name": "对照实验", "type": "verify", "project_id": "prj_ID",
    "resources": {"n_gpus": 0},
    "verify": {"dataset_id": "ds_ID", "n_concurrent": 1, "n_attempts": 1}
  },
  "agents": [
    {"agent": "oracle"},
    {"agent": "claude-code", "model_api_id": "mdl_CLAUDE"},
    {"agent": "codex", "deployment_id": "dep_CODEX"}
  ]
}
```

返回多个 Job，名称自动追加模型名最后一段，oracle 用 Agent 名。共用 dataset/Judge/资源配置，Agent 差异放在 `agents` 列表；不要用循环提交来冒充原子批量请求。

### Claude Code + OpenAI Bridge

先确认在线 schema 支持 `use_openai_bridge`，再选择 `supported_protocols` 含 `responses` 且个人密钥 `ready` 的模型。例如单条任务的 `verify` 或批量的某一条 `agents` 配置：

```json
{"agent":"claude-code","use_openai_bridge":true,"agent_api_protocol":"openai","model_api_id":"mdl_GPT"}
```

仅标准 `claude-code` 支持此开关；每条批量 Agent 独立设置。使用本地服务时传支持 Responses 的 `deployment_id`；手动配置时传实际 `model` 和 `agent_env.OPENAI_API_KEY`、`OPENAI_BASE_URL`。只支持 Chat Completions 的接口不能据此当作 Responses 使用。Bridge 在 trial 容器内自动启动，保留 Claude Code 工具与 MCP；不需要用户另建全局桥接服务。重跑/恢复沿用开关。若仍提示必须使用 Anthropic，先核对实际提交的开关和在线 schema，不通过切换成原生 Anthropic 来掩盖版本问题。

- 依赖：平台 Harbor 镜像（较新版本）已预装 Bridge 独立环境，配合 `use_platform_image:true` 时 trial 内不联网；其他镜像在 setup 阶段从 PyPI 安装，网络慢会长时间停在 `setup_environment` 后失败。
- 报 `Bridge 依赖安装失败`：读 `GET /jobs/{id}/trials/{trial}` 中 `agent_logs` 的 `setup/openai-bridge.txt`。`files.pythonhosted.org ... Read timed out` 属于容器网络问题，告知用户在「平台管理 → Harbor 镜像」重建镜像并启用平台镜像，或改善网络；不要反复重提任务。此时 `/app/output/... Could not find the file` 与 Artifact collision 只是 Agent 未运行的连带结果。
- 缓存：Bridge 去掉 Claude Code 每次请求变化的归因首行，并按「模型 + 题目」固定 `prompt_cache_key`，同题的 trial（含 pass@k）共享上游前缀缓存；上游以 `prompt_tokens_details.cached_tokens` 返回命中时也计为缓存读取。命中率仍很低时先确认服务端已更新到包含这些修复的版本，再检查上游是否支持前缀缓存。

### 平台 Harbor 镜像

`VerifyConfig.use_platform_image:true` 复用平台公共依赖镜像，只构建题目文件与初始化层；仅支持 `environment_type:"docker"` 及上传/服务器本地任务集（不支持 registry 数据集）。提交前 `GET /system/harbor-image` 确认 `ready`；构建为 `POST /system/harbor-image/build`（管理员、占用服务器资源，需用户明确要求）。开启后不需要再对单题预构建。`ready` 只表示镜像存在，不代表是最新配方：镜像在服务端更新前构建时不含 Bridge 预装环境，Bridge 任务仍会联网安装，需要重建。

## SFT / 偏好训练

```json
{
  "name": "SFT实验", "type": "sft", "project_id": "prj_ID",
  "resources": {"n_gpus": 1},
  "sft": {
    "model_id": "实际模型ID", "model_path": "",
    "dataset_ids": ["ds_ID"], "stage": "sft", "template": "auto",
    "finetuning_type": "lora", "learning_rate": 0.0001,
    "num_train_epochs": 1, "per_device_train_batch_size": 1,
    "gradient_accumulation_steps": 8
  }
}
```

`model_path` 在 schema 中是必需字段；使用 `model_id` 时可提供空字符串，由服务端解析真实路径。使用服务器路径时填真实路径，不把模型 API ID 当成本地模型 ID。`stage` 可选 `sft/dpo/kto/orpo/simpo`，数据格式须匹配；用 `GET /resources/templates` 和模型 `llamafactory_template` 选模板。

## RL

```json
{
  "name": "GRPO实验", "type": "rl", "project_id": "prj_ID",
  "resources": {"n_gpus": 1},
  "rl": {
    "framework": "trl", "algorithm": "grpo",
    "model_id": "实际模型ID", "model_path": "", "dataset_id": "ds_ID",
    "finetuning_type": "lora", "learning_rate": 0.000001,
    "train_batch_size": 8, "mini_batch_size": 8,
    "rollout": {"n": 4, "tensor_model_parallel_size": 1}
  }
}
```

RL 支持哪些算法以当前 resources/catalog 和服务端校验为准。Harbor 题包目前走 TRL，`harbor.enabled` 会由服务端设置；verl 使用 parquet。组相对算法要求 rollout.n 至少 2，张量并行和资源数须兼容。框架、reward 函数、数据字段和内存需求不能仅靠登录信息推断；缺少关键实验参数时向用户确认。

## 重跑、评分、恢复与停止

操作前读取目标状态；源任务须已结束，不能自动取消正在执行的任务来满足条件。

| 目标 | 接口与 JSON | 行为 |
| --- | --- | --- |
| 整任务按原配置重跑 | `POST /jobs/{id}/resubmit`，无 body | 创建新任务 |
| 整任务恢复 / 重新评分 | 先 `GET /jobs/{id}/harbor-actions`，再 `POST` 同路径 `{"mode":"resume"}` 或 `{"mode":"regrade"}` | 使用记录副本创建派生任务 |
| 选中单条/多条原地重跑 | `POST /jobs/{id}/trial-actions`，`{"mode":"rerun","trial_names":["真实trial名"]}` | 在原任务内执行，保留名字，返回列表中的原 Job |
| 选中条目恢复 / 评分 | 同上，将 mode 改为 `resume` / `regrade` | 创建对应派生任务 |
| 批量整任务重跑 | `POST /jobs/batch-resubmit`，`{"job_ids":["job_ID"]}` | 返回成功与失败项，逐项核对 |
| 改已结束任务名称 | `PATCH /jobs/{id}/name`，`{"name":"新名"}` | 使用 Job ID，不改变产物目录 |
| 终止 | `POST /jobs/{id}/cancel`，`{"reason":"用户要求"}` | 需要用户指令覆盖终止动作 |

`trial_names` 来自 `/jobs/{id}/trials` 的 `trial_name`；未执行的计划条目可有 `selection_id`，只有服务端确实返回时才使用。评分需要有效记录/产物及评分题包，不能把任意文件夹直接当成完整 Harbor job。`resume` 保留已完成 trial，未完成 trial 从头执行；**不承诺继续同一个 Agent 会话或容器**。不要用 oracle/nop 伪装直接评分。

删除 Job 用 `DELETE /jobs/{id}`，`purge_outputs=true` 会清理产物，应明确覆盖用户的删除范围。批量导出 `POST /jobs/batch-export`，body 为 `job_ids`，使用 `--output` 下载 ZIP。
