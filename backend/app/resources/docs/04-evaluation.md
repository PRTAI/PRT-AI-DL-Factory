---
title: Agent 评测说明
category: 评测
order: 4
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
| 模型 | `--model` | litellm 格式，如 `anthropic/claude-sonnet-4-5` |
| Agent 环境变量 | `--agent-env` | 注入 agent 容器的 API Key |
| Verifier 环境变量 | `--verifier-env` | judge 打分用的 Key，同时写入 `.env` |
| 沙箱后端 | `--env` | `docker` / `e2b` / `daytona` / `modal` … |
| 只跑匹配的任务 | `--include-task-name` | glob，多个用逗号分隔 |
| 排除任务 | `--exclude-task-name` | glob |
| 任务数量上限 | `--n-tasks` | 抽样评测用 |
| 超时倍数 | `--timeout-multiplier` | 慢模型或网络差时调到 2~5 |
| 强制重建镜像 | `--force-build` | 改过 Dockerfile 后用 |
| 导出轨迹 | `--export-traces` | 轨迹可直接拿去做 SFT |

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

1. 用 vLLM 起一个 OpenAI 兼容服务：

```bash
python -m vllm.entrypoints.openai.api_server \
  --model /path/to/data/outputs/<job_id> \
  --served-model-name local-model --port 8000
```

2. 提交评测：agent 选 `terminus`，模型填 `hosted_vllm/local-model`，
   Agent 环境变量加：

```
OPENAI_API_BASE = http://<宿主机 IP>:8000/v1
OPENAI_API_KEY  = EMPTY
```

> 容器内访问宿主机**不能用 `127.0.0.1`**，要用宿主机的实际 IP。

训练任务成功后，详情页右上角有「评测该产物」按钮，会把产物路径带过来。

## 常见问题

| 现象 | 原因 |
|---|---|
| 全部 reward = 0 | 先用 `oracle` 验一遍任务集；再检查 API Key 是否填了 |
| `VerifierTimeoutError` | verifier 超时，多见于 test.sh 里要联网装依赖；调大超时倍数 |
| 任务被跳过、trial 数比预期少 | 任务缺 `environment/`，Harbor 会静默跳过 |
| 提交页顶部提示 docker 不可用 | 运行平台的用户不在 `docker` 组，见管理员指南 |
