---
title: 模型部署与合并导出
category: 部署
order: 6
summary: 用 vLLM 起 OpenAI 兼容服务；LoRA checkpoint 合并导出成完整权重
---

# 模型部署与合并导出

评测本地训练出来的模型需要一个能访问的推理端点。侧栏「模型部署」把这条链路做完了：

```
LoRA 训练完成  →  合并导出（adapter + 底座 = 完整权重）  →  vLLM 部署  →  评测时选这个端点
   ↑ 全参训练直接跳到这里 ─────────────────────────────────┘
```

---

## 一、合并导出 checkpoint

### 为什么必须先合并

LoRA 训练产出的是 **adapter**（`adapter_config.json` + `adapter_model.safetensors`，
通常只有几十 MB），不是完整模型。**vLLM 不能把 adapter 目录当 `--model` 加载**，
所以平台在部署时会直接拦下来，提示你先合并导出。

全参微调（`finetuning_type: full`）产出的本来就是完整权重，不需要这一步——
导出页会告诉你「全参微调的产物本身就是完整权重，直接部署即可」。

### 怎么做

两个入口，效果一样：

1. **任务详情页**：LoRA 训练成功后，标题栏出现「合并导出」按钮，一键用最终 adapter 导出
2. **模型部署页 → 合并导出**：可以挑具体的 checkpoint

| 参数 | 默认 | 说明 |
|---|---|---|
| 训练任务 | — | 只列出当前项目里**已结束的** LoRA / freeze 训练任务 |
| checkpoint | 最终 adapter | 也可以选某个 `checkpoint-<step>`，用来对比中间步 |
| 分片大小 | 5 GB | 对应 `export_size`，决定输出切成几个 safetensors |
| 合并设备 | `cpu` | **推荐 cpu**：更稳，且不和训练任务抢显存 |

产物落在 `data/exports/<导出 id>/model/`，导出列表里可以一键复制这个路径。

> 导出用的是 LLaMA-Factory 自己的 `llamafactory-cli export`（底座 + `adapter_name_or_path`
> + `template`），tokenizer、生成配置、分片都由它处理，比自己调 `merge_and_unload` 稳妥。
> 底座模型和对话模板从原训练任务里自动带出，不用手填。

---

## 二、vLLM 部署

![模型部署](/api/handbook/assets/deployments.png)

### 启动一个服务

「模型部署」页右上角「部署模型」。

| 参数 | 默认 | 说明 |
|---|---|---|
| 服务名称 | — | 平台内识别用 |
| 对外模型名 | 目录名 | **评测时 `--model` 填的就是这个** |
| 模型来源 | 模型仓库 | 或者「已合并导出的产物」 |
| GPU 数量 | 1 | `0` 表示不参与 GPU 记账（CPU 推理） |
| 端口 | 自动 | 自动从 `8100-8199` 里挑一个空的 |
| 张量并行 TP | 1 | **必须 ≤ GPU 数量**，超了会被拦下 |
| 显存占用比例 | 0.9 | vLLM 预分配的 KV cache 比例 |
| 最大上下文 | 模型自带 | 显存不够时调小这个最有效 |
| 精度 / 量化 | auto / 不量化 | 模型本身没量化就别选 |
| 接口形式 | OpenAI Chat | 见下方「接口形式」，决定哪些 agent 能用这个部署 |
| 工具调用解析器 | 不启用 | Responses / Anthropic 必填，按模型系列选（Qwen3 选 `hermes`，Qwen3-Coder 选 `qwen3_coder`…） |
| API Key | 空 | 留空不校验；填了会在日志和命令记录里打成 `***` |

启动后状态从 **启动中** 走到 **服务中**：平台每 3 秒探测一次 `/v1/models`，
能返回才算真的可用。大模型首次加载几分钟很正常，可以点「日志」看 vLLM 的加载进度。

### 接口形式

vLLM 同一个端口上同时提供三种接口，部署时选的是**评测要用哪一种**：

| 接口形式 | 路由 | 适用 agent |
|---|---|---|
| OpenAI Chat | `/v1/chat/completions` | terminus、mini-swe-agent 等走 litellm 的 agent |
| OpenAI Responses | `/v1/responses`（同时保留 Chat） | codex，以及上面所有 Chat 类 agent |
| Anthropic Messages | `/v1/messages` | claude-code |

- Responses / Anthropic 是给 agent 调工具用的，**必须选工具调用解析器**，否则模型的工具调用
  会被当成普通文本，agent 一步也走不动
- 这两种接口需要 **vLLM ≥ 0.18**。启动后平台会检查 `/openapi.json` 里有没有对应路由，
  没有就直接判「已失败」并释放 GPU，错误信息里会提示升级
- Anthropic 形式填了 API Key 时，平台会自动给 vLLM 挂一个中间件，把 claude-code 发来的
  `x-api-key` 转成 vLLM 认的 `Authorization: Bearer`，不用自己处理

### GPU 记账

部署是**常驻服务**，没有「跑完」这一说，只有你显式停止。它占的 GPU 会和训练任务
**共用同一本账**：

- 部署占着的卡不会再分给训练任务
- 反过来，训练任务占着的卡也部署不了，会提示「当前空闲 GPU 只有 N 张」
- 数据大屏的 GPU 分配里能看到哪张卡被哪个部署占着

所以**评测跑完记得停掉不用的服务**，否则显卡一直被挂着。侧栏「模型部署」右边的
绿色数字就是当前活着的服务数，提醒你别忘了。

### 权限

| 操作 | 谁可以做 |
|---|---|
| 查看列表、端点、日志 | 所有登录用户（评测要用别人起的端点） |
| 停止 / 删除 | 部署的负责人、管理员 |

---

## 三、评测时用上它

提交评测页给 agent 选模型时，「模型来源」切到 **本地部署（vLLM）**，再从下拉里挑一个
**服务中**的部署即可，模型名、地址、密钥都由平台注入：

- 下拉里接口形式和当前 agent 不匹配的部署是灰的：codex 需要 Responses，claude-code 需要
  Anthropic，其他 agent 用任意部署都行
- 模型名就是部署的「对外模型名」（litellm 系 agent 会自动加 `openai/` / `anthropic/` 前缀）
- OpenAI 系注入 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_API_BASE`（地址带 `/v1`）；
  Anthropic 注入 `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL`（地址不带 `/v1`），并把 claude-code
  的 opus / sonnet / haiku、后台小任务和子 agent 的模型都指到这个部署
- 部署没设 API Key 时密钥给一个占位值（vLLM 不校验）；设了的话按密钥处理，日志里打码
- codex 会把模型名按 `/` 切开只取最后一段，所以给 codex 用的部署，对外模型名不要带 `/`

提交时平台会再确认一次部署仍在服务中；评测排队期间部署被停掉的话，任务启动时会直接报错。

> ⚠️ **地址用的是宿主机 IP，不是 `127.0.0.1`**。Harbor 的 agent 跑在 Docker 容器里，
> 容器内的 `127.0.0.1` 指的是容器自己，用回环地址一定连不上宿主机的 vLLM。
> 平台会自动探测宿主机的出口 IP 并填进去；部署列表里那个 `127.0.0.1` 的地址是给你
> 在本机 curl 验证用的，别手抄到评测表单里。

如果这块显示「先到模型部署起一个服务」，说明当前没有处于「服务中」状态的部署。

---

## 常见问题

**部署刚起来就变「已失败」**
点「日志」看 vLLM 的报错。最常见的三种：

| 日志里的迹象 | 原因 |
|---|---|
| `No module named vllm` | 部署用的解释器没装 vLLM，改 `PRT_VLLM_PYTHON` |
| `CUDA out of memory` | 显存不够：调低「显存占用比例」或「最大上下文」 |
| `does not appear to have a file named config.json` | 指到了 adapter 目录或非模型目录 |
| 错误信息「当前 vLLM 没有提供 /v1/…」 | vLLM 版本低于 0.18，不支持所选接口形式 |

**评测里 agent 报连不上模型 / Connection refused**
八成是地址用了 `127.0.0.1`。在提交评测页用「本地部署」来源，或者手填宿主机 IP。
也确认一下 vLLM 是监听 `0.0.0.0`（平台默认就是）而不是只听回环。

**codex / claude-code 连上了但一直不调用工具**
部署时选的工具调用解析器和模型系列不匹配。停掉后换一个解析器重新部署。

**「这是 LoRA adapter 目录，请先合并导出」**
说明选的任务产物是 adapter。按上面第一节先导出，然后在「模型来源」里选
「已合并导出的产物」。

**端口冲突**
`8100-8199` 用满了，或者你手填的端口被别的进程占了。留空让平台自动分配即可。

**平台重启之后**
部署进程是独立起的，平台重启不会自动把它们拉起来。启动时会对账一次：
进程还活着的继续显示服务中，已经没了的标成失败并把 GPU 记账还回去。
