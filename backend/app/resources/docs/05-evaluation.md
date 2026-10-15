---
title: Agent 评测说明
category: 评测
order: 5
summary: Harbor pass@k、并发、agent 与密钥怎么配
---

# Agent 评测说明

评测基于 **Harbor**：在沙箱容器里跑 agent，再由任务自带的 verifier 打分。

## 参数对照

| 表单项 | Harbor 参数 | 说明 |
|---|---|---|
| pass@k | `--n-attempts` | 每题尝试次数，结果给出 pass@1..pass@k |
| 并发 | `--n-concurrent` | 同时跑的 trial 数，受本机 CPU/内存与 Docker 限制 |
| Agent | `--agent` | `oracle` / `nop` / `claude-code` / `codex` / `terminus` … |
| 模型 | `--model` + `--agent-env` | **选平台的模型配置**：密钥与地址由后端按 Harbor 标准变量注入；也可手填 litellm 格式模型名 |
| 思考模式 | `--agent-kwarg reasoning_effort=…` | low → max；各 agent 支持的档位不同，超出会自动收敛 |
| Agent 环境变量 | `--agent-env` | 手填模型时自己给 API Key |
| 详细日志 | `--debug` | 默认开：每个 trial 装了什么、跑了什么命令、为什么失败都进日志 |
| Verifier 环境变量 | `--verifier-env` | judge 打分用的 Key，同时写入 `.env` |
| 任务环境变量 | 宿主机环境 + `.env` 声明 | 自动读取 `[environment.env]` 引用的 `${VAR}`（如 MCP 所需 Key）；留空沿用 Agent 模型配置或服务器上的同名变量 |
| 沙箱后端 | `--env` | `docker` / `e2b` / `daytona` / `modal` … |
| 只跑匹配的任务 | `--include-task-name` | glob，多个用逗号分隔 |
| 排除任务 | `--exclude-task-name` | glob |
| 任务数量上限 | `--n-tasks` | 抽样评测用 |
| 超时倍数 | `--timeout-multiplier` | 慢模型或网络差时调到 2~5 |
| 强制重建镜像 | `--force-build` | 改过 Dockerfile 后用 |
| 导出轨迹 | `--export-traces` | 轨迹可直接拿去做 SFT |

## 任务集来源

| 来源 | 说明 |
|---|---|
| 已上传的任务集 | 「数据集」页上传的 `harbor_tasks` 压缩包 |
| **Harbor 官方数据集** | terminal-bench、aider-polyglot、aime … 直接从注册表选。运行时 Harbor 会从 GitHub 拉任务仓库，**机器需要外网** |
| 服务器目录 | 已经放在机器上的大任务集，填绝对路径 |

## 模型与密钥

平台与模型由管理员在「平台管理 → 模型 API」统一维护，全员可选；平台**不提供统一密钥**，
每个账号需在「个人设置 → 我的 API Key」为要用的平台填写自己的密钥（同平台下的模型共用一份），
运行时注入的是提交人自己的密钥，未填写的平台在提交页不可选。每条配置有一个
**厂家/协议**，它决定运行时注入哪些环境变量（变量名与 Harbor 自己的
`model_connection.py` 完全一致）：

| 协议 | 注入 | 适用 |
|---|---|---|
| `openai` | `OPENAI_API_KEY` / `OPENAI_BASE_URL` | GPT 系、中转站、任何 OpenAI 兼容端点（含 Qwen 的兼容模式） |
| `anthropic` | `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` | Claude 系；`claude-code` agent 只认这一档 |
| `dashscope` | `DASHSCOPE_API_KEY` | 百炼原生入口 |
| `google` / `openrouter` / `deepseek` / `xai` / `moonshot` | 各自的 `*_API_KEY` / `*_BASE_URL` | |

模型名怎么传也和 agent 有关：CLI 类 agent（codex、claude-code、cursor-cli…）拿到的是
裸模型名；litellm 类 agent（terminus、mini-swe-agent、aider…）会自动加上 `provider/` 前缀。

**思考模式**（reasoning effort）在 Agent 卡片里选，low → max。各 agent 接受的档位：

| agent | 档位 |
|---|---|
| claude-code、cursor-cli、copilot-cli、terminus | low / medium / high / xhigh / max |
| codex | low / medium / high / xhigh |
| gemini-cli、aider、openhands | low / medium / high |

选了 agent 不支持的档位，平台会收敛到它支持的最近一档，并在「配置与命令」的说明里写明。

## 任务进展页

评测任务详情默认打开「任务进展」：每个 trial 一行，阶段来自 Harbor 在 trial 目录里
留下的文件（Docker 把目录挂进容器，文件是实时出现的），**10 条一页**，可按阶段筛：

| 阶段 | 判断依据 |
|---|---|
| 待开始 | 在任务清单里但目录还没出现 |
| 环境准备 | 有 `config.json`；出现 `agent/setup/` 表示容器已起、正在装 agent |
| Agent 运行 | `agent/` 里出现 agent 的输出文件 |
| 验证中 | `verifier/` 里出现测试输出 |
| 完成 | `result.json` 已写且无异常；附 reward（1.0 通过 / 0.0 未通过） |
| 失败 | `result.json` 带 `exception_info`，如 `AgentSetupTimeoutError` |

点任意一行能看这个 trial 的 `trial.log`、agent 输出、verifier 输出和异常栈——
排查「为什么这一题挂了」不用再去服务器翻目录。

## 推荐流程

| 顺序 | agent | 期望结果 | 目的 |
|---|---|---|---|
| 1 | `oracle` | reward ≈ 1.0 | 任务与测试脚本自洽 |
| 2 | `nop` | reward = 0 | 任务不会「什么都不做也能过」 |
| 3 | 真实 agent | pass@1 | 正式评测 |
| 4 | 真实 agent | pass@4 | 需要统计显著性时 |

前两步跑不对，说明问题在任务集本身，跑再多真实 agent 也是浪费预算。

## 看结果

![任务列表](/api/handbook/assets/job-list.png)

评测任务详情多一个 **Trial 明细** 标签页，按 reward 从低到高排序，失败的排在最前面：

| 列 | 含义 |
|---|---|
| reward | `1.00` 通过 / `0.00` 未通过 / `无` 表示该 trial 崩了没打分 |
| 异常 | 如 `VerifierTimeoutError`，附带具体报错 |
| 耗时 | 单个 trial 的墙钟时间 |

> **通过率的口径**：崩掉的 trial 算失败（分母是已完成 trial 数），
> 不会因为"没打分就不计入"而虚高。

## 评测自己训练出来的模型

不用手敲命令了，平台里三步：

1. **（LoRA 才需要）合并导出** —— 任务详情页的「合并导出」按钮，把 adapter 并回底座
2. **部署** —— 侧栏「模型部署」→「部署模型」，来源选「已合并导出的产物」；
   「接口形式」按要用的 agent 选：codex 选 OpenAI Responses，claude-code 选 Anthropic
   Messages（这两种要配工具调用解析器），其他 agent 用默认的 OpenAI Chat 即可
3. **提交评测** —— agent 的「模型来源」切到「本地部署（vLLM）」，挑一个服务中的部署，
   模型名、地址和密钥由平台按该 agent 需要的接口自动注入

详见 [模型部署与合并导出](/docs/deployment)。

> 容器内访问宿主机**不能用 `127.0.0.1`**，要用宿主机的实际 IP —— 选「本地部署」时平台注入的就是
> 宿主机 IP；手填的话别抄部署列表里那个回环地址（那个是给本机 curl 验证用的）。

训练任务成功后，详情页右上角有「评测该产物」按钮，会把产物路径带过来。

## 常见问题

| 现象 | 原因 |
|---|---|
| 全部 reward = 0 | 先用 `oracle` 验一遍任务集；再检查「个人设置 → 我的 API Key」是否填了 |
| `VerifierTimeoutError` | verifier 超时，多见于 test.sh 里要联网装依赖；调大超时倍数 |
| 任务被跳过、trial 数比预期少 | 任务缺 `environment/`，Harbor 会静默跳过 |
| 提交页顶部提示 docker 不可用 | 运行平台的用户不在 `docker` 组，见管理员指南 |
