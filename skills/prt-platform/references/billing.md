# 费用、折扣与统计排障

先查询在线 schema。`GET /system/pricing` 返回价格、折扣与 USD/CNY 汇率；`GET /system/dashboard` 的 `billing` 返回按项目 `projects`、人员 `people`、模型 `models` 汇总的人民币费用。普通用户大屏只统计自己的任务，管理员统计全部；不能把个人汇总声称为全平台总费用。

## 读取与解释

```bash
python3 "$PRT_API" schema --path /system/pricing --method GET
python3 "$PRT_API" request GET /system/pricing
python3 "$PRT_API" request GET /system/dashboard
python3 "$PRT_API" request GET /jobs/job_ID
```

任务 `billing` 包含 `amount_cny`、`agent_cny`、`judge_cny`、`requests`、`by_model`、`warnings`、`snapshot` 与 `updated_at`。金额是按记录的 Token、任务价格快照和折扣估算，包含失败和原地重跑的历史调用；恢复/重新评分复制的旧会话不重复计费，删除任务的已发生费用仍保留。当前汇总仅覆盖 Harbor 评测，不包括训练、本地 GPU、订阅、工具收费或样例审核，不能当成供应商账单。

| 状态/字段 | 判断 |
| --- | --- |
| 响应根本没有 `billing`，schema 也没有 `/system/pricing` | 后端版本未提供费用功能；通常需要同步代码并重启后端，不是尚未产生花费 |
| `pending` 且 `amount_cny:null` | 看 `warnings` 和 `requests`：无用量记录、读取失败或没有可用价格；不能算作零元 |
| `partial` | 金额只覆盖部分已知用量；说明缺失阶段/单价，不当作完整总费用 |
| `estimated` | 当前已识别记录的预估；不保证未记录的请求或其他计费项已覆盖 |
| `not_applicable` | 不属于当前 Token 费用统计范围 |

项目/人员/模型汇总中的 `incomplete_jobs` 表示费用不完整的任务数。按项目筛选时从 `billing.projects` 取真实项目 ID；`models[].id` 包含供应商，不能合并同名但价格不同的供应商模型。

## 缺失费用的检查顺序

1. 先验证后端功能是否存在。页面已更新但 API 无 `billing` 时，不反复启动评测来“产生费用”。后端重启可能影响正在运行的任务，应按用户授权和服务器维护安排处理。
2. 读取目标 `/jobs/{id}` 的 `billing.warnings`；此读取会刷新已有记录，旧任务不必重跑模型才能统计。
3. `requests > 0` 但无金额时，检查 `snapshot.agent/judge.price`、供应商、精确模型 ID、价格有效期以及未知的缓存单价。禁止猜价格、把未知别名模糊匹配到较便宜模型，或用缺失金额填零。
4. 无请求记录时检查 `/jobs/{id}/trials` 及允许下载的原生 Agent/Verifier 日志。缺少原始用量无法可靠补算；Harbor/LiteLLM 的 USD 汇总未必使用用户的折扣与缓存价格，不能直接当人民币费用。

## 修改价格

只有用户要求调整且账号是管理员时，才 `PUT /system/pricing`。先 GET 完整配置，保留其他模型、来源和汇率字段，仅修改明确要求的条目；发送 `PricingSettings` schema 允许的结构。价格为每百万 Token 的原币单价，`discount` 是乘数（4 折 = `0.4`），人民币金额为原价 × Token / 1,000,000 × 折扣，USD 再乘 `usd_cny`。`prices:[]` 会禁用全部价格，不要为了局部更新发送空列表。

保存只影响新任务，已有任务保留价格快照；历史任务首次统计使用当时的配置并标记 `historical`。不要声称改全局价格已重算所有旧任务。当前没有公开的强制覆盖历史价格接口，若用户要求重算，明确区分价格政策变更与读取记录刷新。
