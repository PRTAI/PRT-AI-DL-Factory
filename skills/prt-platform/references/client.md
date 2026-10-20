# 客户端与排障

以下命令中的 `PRT_API` 为脚本路径，`PRT_PLATFORM_SESSION` 为当前任务独立会话文件。全局 `--session`、`--timeout` 放在子命令前；默认网络超时 60 秒。客户端输出 JSON，默认隐藏敏感字段；不会自动重试请求。

## JSON、查询、上传与下载

```bash
python3 "$PRT_API" request GET /jobs --query project_id=prj_ID --query limit=10
# 列表参数重复指定，例如查询多个状态。
python3 "$PRT_API" request GET /jobs --query status=running --query status=queued
python3 "$PRT_API" request POST /projects --json - <<'JSON'
{"name":"模型研究","description":"训练与评测","tags":["research"]}
JSON
python3 "$PRT_API" request POST /jobs --json /绝对路径/job.json
python3 "$PRT_API" upload /绝对路径/tasks.zip --name 评测集 --kind harbor_tasks --project-id prj_ID
python3 "$PRT_API" request GET /jobs/job_ID/export --output /绝对路径/job_ID.zip
python3 "$PRT_API" request GET /jobs/job_ID/artifacts/download \
  --query 'path=job_ID/TRIAL/agent/claude-code.txt' --output /绝对路径/claude-code.txt
python3 "$PRT_API" logout
```

`--json` 接受文件或 `-`（stdin），不能直接填写 JSON 字面量。`--query key=value` 会编码中文、空格及特殊字符。接口路径可使用 `/jobs` 或 `/api/jobs`。下载不会覆盖已有文件，连接中断或长度不匹配会删除不完整结果；上传分块发送文件，避免整包装入内存。没有通用“把服务端任意路径导入数据集”的接口，上传需要当前 Agent 能读取的压缩包。

动态接口查询：

```bash
python3 "$PRT_API" schema --search deployments
python3 "$PRT_API" schema --path /datasets --method POST
python3 "$PRT_API" schema --model SFTConfig
```

登录方法也可从 Python 调用：用 `importlib.util.spec_from_file_location` 加载脚本后，`Client(url).login(email, password, session_path)`。返回值只有账号、地址及会话路径，token 留在会话中。不要打印会话文件或原始登录响应。

## 有界等待

```bash
python3 "$PRT_API" wait /jobs/job_ID --until succeeded --max-seconds 60 --interval 5
python3 "$PRT_API" wait /datasets/ds_ID --until ready --max-seconds 60
python3 "$PRT_API" wait /deployments/dep_ID --until serving --max-seconds 60
```

结果包含 `wait_state` 和资源本体。退出码 0 表示达到目标；1 表示失败/错误；2 表示观察期结束仍未完成或需要审核。`pending_review` 立即返回 `attention`。后台任务的运行不因 wait 结束而停止。

## 任务排障

读取 `/jobs/{id}`、`/jobs/{id}/trials?page=1&page_size=100`、`/jobs/{id}/log?tail_bytes=24000`，必要时读取 `/jobs/{id}/trials/{trial_name}`。逐 trial 返回 `phase`、`phase_seconds`、异常、日志和文件列表；汇总日志的“等待 N 秒”只说明外部命令未退出。

对比两次观察中的完成数、日志大小/offset、会话时间戳、模型输出及工具结果。`updated_at` 和平台心跳可能持续更新，即使模型没有新输出。`running_agent` 尚未评分，`running_verifier` 才在评分；`succeeded` 表示进程完成，评分结果仍须核对 rewards 及 verifier 异常。系统错误或 fail-closed 占位零分不能当作有效模型得分。

`/jobs/{id}/artifacts` 可能返回服务端绝对路径；下载参数是相对于该 job 的 `runtime.output_dir` 的路径，不能直接使用绝对路径。常见形态为 `job_ID/TRIAL/agent/...`，以实际输出目录和文件列表为准。文件可列出但内容为空或下载中断时，检查权限/读取问题，不能据此认定 Agent 没有轨迹。只凭平台账号无法读取任意服务器进程或容器调试信息；需要时向用户获取 SSH 或相应日志，而非绕过文件边界。

网页登录地址可能是前端 5153 或反向代理；所有客户端调用走同地址的 `/api`，不要求预先知道后端端口。HTTP 环境沿用用户提供的内网部署方式，HTTPS 保持证书验证。
