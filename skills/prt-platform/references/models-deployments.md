# 模型、密钥、部署与导出

## 模型与接入配置

- `GET /models?q=...&framework=...` 查询服务器已有模型，结果的 `id`、路径与模板用于训练/部署。`POST /models/refresh` 重新扫描服务器目录，不会上传 Agent 机器上的模型。
- `POST /models/downloads`：`{"model_id":"厂商/模型","revision":""}` 从 ModelScope 下载；用 `/models/downloads` 获取状态、`/models/downloads/{id}/log` 获取日志。下载是资源操作，按用户要求执行。
- `GET /model-apis` 查询可调用的模型 API，检查 `ready`、协议、base_url 与精确模型 ID；`GET /model-apis/platforms` 查询供应商平台，`GET /model-apis/platform-catalog` 查询厂家模板。
- 管理员可 `POST /model-apis/platforms` 创建供应商平台、`POST /model-apis` 创建模型配置；先查 schema（`ModelPlatformCreate` / `ModelApiCreate`），不要复制旧共享密钥字段。
- 用户自己的供应商密钥通过 `PUT /model-apis/my-keys/{target_id}`：`{"api_key":"用户提供的密钥"}`，target 为平台 ID 或配置 ID，以 `/model-apis/my-keys` 为准。只能提交用户实际提供的供应商密钥，不能把平台密码或 `***` 脱敏占位值用作 API Key。接口只返回掩码，不尝试恢复别人密钥。

平台登录与外部模型调用不是同一凭据。账号已有可用配置时可直接使用模型 API ID，不必再次索要供应商密钥。部署本地模型则依赖服务器模型和 GPU。

## vLLM 部署

先查询 `GET /system/metrics`、`GET /deployments?active_only=true` 和用户指定模型。检查端口与 GPU 占用，保持用户要求的协议/模型；模型 ID、部署 ID、Job ID 各自独立。

`schema --model DeploymentCreate` 与 `schema --model VLLMConfig` 获取真实参数。

```json
{
  "name": "本地模型服务", "project_id": "prj_ID",
  "source": "model", "model_id": "服务器实际模型ID",
  "served_model_name": "Qwen3.5-4B", "n_gpus": 1,
  "api_protocol": "responses",
  "config": {
    "tensor_parallel_size": 1,
    "gpu_memory_utilization": 0.85,
    "max_model_len": 32768,
    "tool_call_parser": "qwen3_xml"
  }
}
```

示例解析器仅说明字段位置，实际值必须匹配模型及服务器 vLLM 版本，不普遍套用 Qwen 配置。`source` 可为 `model`、`job`、`path`，分别提供对应 `model_id`、`job_id`、`model_path`；路径属于服务器。Codex 使用 `responses`，Claude Code 原生使用 `anthropic`，启用 [OpenAI Bridge](jobs.md#claude-code--openai-bridge) 时上游使用 `responses`；通常对话接口使用 `openai`。Agent 协议需要匹配工具调用解析器；Codex 对外模型名不能带 `/`。`port` 可省略让平台分配，不擅自停止已有服务来抢端口。

`POST /deployments` 返回 `starting` 后轮询 `GET /deployments/{id}`、`GET /deployments/{id}/log?tail_bytes=24000`，到 `serving` 才是就绪。探活成功并不证明复杂业务工具调用正确，按用户要求验证真实工作流。

`GET /deployments/endpoints` 返回就绪服务的地址。评测优先传 `deployment_id`；容器调用使用 `sandbox_base_url` / `sandbox_endpoints`，不能用容器内的 `127.0.0.1` 去访问宿主模型服务。`POST /deployments/{id}/stop` 停止，`DELETE /deployments/{id}` 删除记录；二者是不同动作，均应匹配本次用户指令。

## 训练权重导出

`GET /exports/candidates/{job_id}` 列出真实可导出的 checkpoint。有 LoRA adapter 时先合并，不能将 adapter 目录当成完整模型直接部署。

`POST /exports`：

```json
{"job_id":"job_TRAIN","name":"合并权重","adapter_path":"服务端实际checkpoint路径","export_size_gb":5,"export_device":"cpu"}
```

`adapter_path` 留空时取最终输出；以 candidates 为准。查询 `/exports/{id}` 及 `/exports/{id}/log`，仅 `succeeded` 后才能使用 `export_dir`。合并导出不等于将文件下载到本地，任务 ZIP 下载是另一接口。

随后部署时用 `source:"path"` 和实际 `export_dir`，或使用平台已解析的 Job 产物。导出有独立 `queued/running/succeeded/failed` 状态；取消用 `POST /exports/{id}/cancel`。删除支持 `purge_files`，只在明确要求清理文件时使用。
