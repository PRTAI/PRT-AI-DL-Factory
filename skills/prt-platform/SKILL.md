---
name: prt-platform
description: "使用平台地址及用户凭据，通过 PRT AI DL Factory HTTP API 操作项目、数据集、训练评测和模型部署，查询费用并排查任务。无需平台源码或 SSH。"
---

# PRT 平台操作

将本 Skill 的整个目录交给其他 Agent 即可使用。客户端 `scripts/prt_api.py` 只依赖 Python 3.10+ 标准库，不依赖平台代码、浏览器、SSH 或额外 MCP。以用户本次提供的平台地址、账号、密码为准；Skill 不携带任何账号、密钥或固定服务器地址。

## 连接与接口发现

1. 确认用户要操作的对象及目标；提供凭据本身不代表要求创建任务。已有明确的创建、上传、提交或部署指令时直接执行，不重复索要授权。
2. 为当前工作创建独立临时会话文件，设置 `PRT_PLATFORM_SESSION` 为其路径。将 `PRT_API` 设为本 Skill 下 `scripts/prt_api.py` 的实际绝对路径。
3. 用下述方式登录。用户已提供密码时，通过执行器的 stdin/input 传入，不要求再次输入；交互终端也可省略 `--password-stdin` 使用隐藏输入。

```bash
# 这些变量来自本次用户输入或执行器的秘密输入，不将实际值写进技能。
printf '%s' "$PRT_PLATFORM_PASSWORD" | python3 "$PRT_API" login \
  --url "$PRT_PLATFORM_URL" --email "$PRT_PLATFORM_EMAIL" --password-stdin
python3 "$PRT_API" request GET /auth/me
python3 "$PRT_API" request GET /health
python3 "$PRT_API" schema --search projects
python3 "$PRT_API" schema --path /jobs --method POST
python3 "$PRT_API" schema --model VerifyConfig
```

`PRT_PLATFORM_SESSION` 也可用每条命令前的 `--session /绝对路径/session.json` 指定。客户端接受站点根地址、`/api`、`/login` 或 `/jobs/...` 页面地址，支持代理前缀。所有接口路径绑定该会话的平台；不会向重定向地址转发登录密码或 Bearer token。

服务器 `/api/openapi.json` 是接口及 schema 的当前依据。`schema --path`、`--model` 同时输出依赖的 schema，不必读取整份文档。`schema --search` 按路径、描述及参数检索（如 `jobs`、`deployments`、`users`、`reviews`）。本 Skill 的参考提供流程和最小示例，不能代替服务端版本校验。404/HTML 表示地址或版本不匹配，先核实，不猜测其他主机或端口。

## 选择操作流程

- 项目创建、数据集上传/替换/检查：读 [references/projects-data.md](references/projects-data.md)。
- Harbor 评测、批量 Agent、SFT/RL、审核、原地重跑/评分/恢复：读 [references/jobs.md](references/jobs.md)。
- 本地模型、个人供应商密钥、vLLM 部署与训练产物导出：读 [references/models-deployments.md](references/models-deployments.md)。
- 任务费用、项目/人员/模型花费汇总与价格折扣：读 [references/billing.md](references/billing.md)。
- 通用客户端参数、下载与故障判断：读 [references/client.md](references/client.md)。

其他平台功能（账户、审核报告、系统设置等）通过 `schema --search` 找到真实路由，再用 `request` 调用。不要靠界面名称推测 HTTP 方法或字段。资源名称用于检索，变更必须使用服务器返回的 ID；重名时核实负责人、项目和创建时间。

页面出现开关不代表后端已支持：使用 Bridge 前检查 `VerifyConfig` 和 `VerifyAgentConfig` 是否包含 `use_openai_bridge`；费用统计检查 `/system/pricing` 及 `/system/dashboard` 返回的 `billing`。缺失时报告前后端版本不一致，需要更新并重启后端，不将未知字段发给旧服务端（它可能静默忽略）。

## 执行与核验

- 登录密码仅用于 `/auth/login`；供应商 API Key 是另一种凭据。优先复用账号已有的可用模型 API 配置或部署 ID；缺失供应商密钥、数据文件、模型或 GPU 时如实说明，不能声称仅凭平台账号就能补齐这些资源。
- 用户指定的任务和操作即为本次授权范围。停止、删除、替换、重新运行、改变权限或审批需要覆盖这些具体动作的用户指令；不要因为持有管理员账号就擅自审核别人的任务或修改全局设置。
- 创建、上传、提交、部署等请求**不自动重试**。连接断开或超时后结果可能已生效，先 GET/列表核对；无法确认时报告“结果未知”，避免重复提交。401 核对凭据或重新登录，403 尊重权限，422 用当前 schema 修正参数。
- 写入前检查目标项目、数据/模型、Agent、协议和资源数；写入后 GET 资源并保存返回 ID、状态和页面链接。部署达到 `serving` 才算就绪，数据达到 `ready` 才算可用。任务 `queued`/`pending_review` 不算已启动，`running` 不证明模型还有进展。
- `wait` 是单次有界观察；到时仍在运行应报告当前进度，不据此取消、重新提交或创建后台监控。需要持续监控时遵循用户另行给定的范围。
- 会话文件保存 Bearer token，权限 0600；不保存密码，不回显密钥，不提交到仓库。JSON 请求中的秘密值可从一次性 stdin 传入。下载的原始文件可能包含任务数据或日志，应按用户所选范围保存。任务结束用 `logout` 删除本地会话并清理临时请求文件及环境中的密码；本平台 logout 不撤销已签发 token。

最终交代实际执行的动作、资源 ID/链接、当前状态和未完成条件。清楚区分“提交成功”“开始运行”“执行完成”和“评测通过”。
