# 项目与数据集

先用 `schema --search projects` / `schema --search datasets` 确认当前部署支持的操作。下文 ID 均须替换为实际返回值。

## 项目

- `GET /projects?q=名称` 返回列表；项目详情 `GET /projects/{id}` 为 `{project, stats}`。
- `POST /projects` 请求 `{"name":"研究项目","description":"...","tags":["eval"]}`，返回项目 ID。
- `GET /projects/{id}/jobs?limit=20` 查看最近任务；完整分页用 `/jobs?project_id=...&limit=...&offset=...`。
- `PATCH /projects/{id}` 可修改 `name`、`description`、`tags`、`archived`、`owner_id`。归档、转交及删除仅在用户要求时操作；`DELETE /projects/{id}` 要求先清空任务，不自动级联删除。

列表为团队共享，能读到不代表能修改。先复用用户指明的项目，名称不唯一时核实 ID；不要为了测试连通性创建项目。

样例审核也必须归属项目：`POST /reviews` 是 multipart，必填 `project_id`、`name`、`model_api_id`、`archive` 与可重复的 `docs` 文件字段，`agent` 选择 `claude-code` 或 `codex`。这是 Agent harness 审核，不是单次 LLM 调用；Claude Code 原生需要 Anthropic，Codex 需要 Responses，当前审核 schema 没有评测用的 Bridge 开关。先用 `schema --path /reviews --method POST` 获取实际字段；现有 `upload` 子命令仅上传数据集，不能拿它代替审核的多文件请求。审核结果查询 `/reviews?project_id=...`、`/reviews/{id}`、`/reviews/{id}/findings` 和 `/reviews/{id}/log`；成功后可下载 `/reviews/{id}/report.docx`。

## 上传与校验

`GET /datasets/specs` 返回服务端支持的格式说明。`GET /datasets?project_id=...&mine_only=true&kind=...` 查询已有数据。客户端调用 multipart `POST /datasets`，字段为 `file`、`name`、`kind`、`project_id`、`description`、逗号分隔的 `tags`、`rewardkit`。

| kind | 用途 |
| --- | --- |
| `harbor_tasks` | Harbor 任务树 |
| `sft_alpaca` / `sft_sharegpt` | 监督微调 |
| `dpo_pairs` / `kto_pairs` | 偏好训练 |
| `verl_parquet` | RL parquet 数据 |
| `raw` | 原始文件，不能默认用于训练 |

```bash
python3 "$PRT_API" upload /本地路径/tasks.zip \
  --name harbor-eval --kind harbor_tasks --project-id prj_ID --rewardkit
python3 "$PRT_API" request GET /datasets/ds_ID
python3 "$PRT_API" request GET /datasets/ds_ID/preview --query limit=3
```

`rewardkit` 仅在题包确实使用 RewardKit 时启用，不把所有 Harbor 集自动标成 RewardKit。支持 zip / tar.gz；数据与任务布局依据 `/datasets/specs`，不要只改 kind 以绕过格式校验。

上传后检查 `status`：仅 `ready` 可提交任务；`uploading/extracting/validating` 尚在处理，`invalid` 读取 `error` 和 `stats.warnings`。核实 `stats.n_tasks`/`n_records` 与用户预期一致。HTTP 201 并不替代数据校验。

## 更新与预构建

- `PATCH /datasets/{id}` 修改元信息（`name`、`project_id`、`description`、`tags`、`rewardkit`）。
- 显式替换内容时：`python3 "$PRT_API" upload /本地路径/new.zip --replace ds_ID --kind harbor_tasks`，调用 `PUT /datasets/{id}/file`，不另建数据集；重新检查状态。
- `POST /datasets/{id}/prebuild` 的 JSON 为 `{"task":"具体任务名"}`；用返回的 build ID 查询 `GET /datasets/prebuilds/{build_id}`。构建会使用服务器资源，不能作为普通读取的附带动作。
- `DELETE /datasets/{id}` 为删除操作，服从本次用户范围。

本地文件路径属于 Agent 所在机器；数据集 `root_path`、训练模型路径和 `verify.tasks_path` 属于平台服务器。两者不能互换。Harbor 如需直接引用服务器已有题包，可用评测的 `tasks_path`（仍应确认服务端可访问和用户授权），这不等于注册了一个新数据集。
