# 部署与运维

## 1. 平台自身

```bash
scripts/setup.sh                    # conda env prt-ai-dl-factory + 依赖 + 前端依赖 + 适配表
cp backend/.env.example backend/.env
$EDITOR backend/.env                # 改管理员初始账号、模型目录、框架路径
scripts/start_frontend.sh build     # 产出 frontend/dist
scripts/start_backend.sh            # 0.0.0.0:8000，同时提供 API 与前端
```

平台自身**不需要**安装 torch / transformers；它只负责拼命令并拉起子进程。

## 2. 训练框架

各框架依赖冲突严重（三者的 torch / transformers 版本互不兼容），必须一框架一环境，
再在 `backend/.env` 里指向对应解释器。本机已按下面的方式装好：

```bash
# LLaMA-Factory —— 构建后端不写源码目录，可直接可编辑安装
conda create -y -n llamafactory python=3.11 && conda activate llamafactory
pip install -e /workspace/LiHanlin_Workspace/github/LlamaFactory

# TRL / verl —— github/ 下的检出是 root 只读，`pip install -e` 会因为写不了
# egg-info 而失败；平台本来就用 PYTHONPATH 指向检出运行，所以只装依赖即可
conda create -y -n trl python=3.12 && conda activate trl
python - <<'EOF' > /tmp/trl_deps.txt
import tomllib, pathlib
d = tomllib.loads(pathlib.Path('/workspace/LiHanlin_Workspace/github/trl/pyproject.toml').read_text())
deps = list(d['project']['dependencies'])
for extra in ('peft', 'liger', 'vllm'):
    deps += d['project'].get('optional-dependencies', {}).get(extra, [])
print("\n".join(dict.fromkeys(deps)))
EOF
pip install torch -r /tmp/trl_deps.txt

conda create -y -n verl python=3.12 && conda activate verl
grep -vE '^\s*(#|$)|^pre-commit' /workspace/LiHanlin_Workspace/github/verl/requirements.txt > /tmp/verl_deps.txt
printf 'torch\nvllm>=0.18.0\n' >> /tmp/verl_deps.txt
pip install -r /tmp/verl_deps.txt
```

```ini
PRT_LLAMAFACTORY_PYTHON=/home/user/.conda/envs/llamafactory/bin/python
PRT_VERL_PYTHON=/home/user/.conda/envs/verl/bin/python
PRT_TRL_PYTHON=/home/user/.conda/envs/trl/bin/python
```

验证（平台就是这样启动它们的）：

```bash
PYTHONPATH=/workspace/LiHanlin_Workspace/github/trl \
  /home/user/.conda/envs/trl/bin/python -c "import trl, torch; print(trl.__file__, torch.cuda.is_available())"
```

「平台管理 → 工具链」页面会用配置的解释器真正 import 一次框架与 torch，
所以那一页显示绿色就等于任务真的能起来。

## 3. Harbor

评测依赖 `harbor` 命令行与可用的容器后端：

```bash
uv tool install harbor          # 或 pipx install harbor
harbor --version
docker info                     # docker 后端需要守护进程可用
```

```ini
PRT_HARBOR_BIN=/home/user/.local/bin/harbor
```

未安装时提交评测页会提前给出告警，任务仍可提交但会在启动阶段失败（日志里能看到原因）。

## 4. 反向代理

WebSocket 需要 upgrade 头透传：

```nginx
server {
    listen 80;
    server_name prt.internal;
    client_max_body_size 0;          # 0 = 不限制，与 PRT_MAX_UPLOAD_MB 保持一致

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 3600s;    # 长时间挂着的日志流
    }

    location /ws/ {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade $http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
```

## 5. systemd

```ini
# /etc/systemd/system/prt-factory.service
[Unit]
Description=PRT AI DL Factory
After=network.target docker.service

[Service]
Type=simple
User=user
WorkingDirectory=/workspace/LiHanlin_Workspace/self-developed/PRT-AI-DL-Factory
Environment=PORT=8000
ExecStart=/workspace/LiHanlin_Workspace/self-developed/PRT-AI-DL-Factory/scripts/start_backend.sh
Restart=on-failure
RestartSec=5
# 训练子进程是独立进程组，重启服务不会误杀它们；
# 平台起来后会对账并恢复这些任务的状态。
KillMode=process

[Install]
WantedBy=multi-user.target
```

> `KillMode=process` 很重要：默认的 `control-group` 会在重启服务时把所有训练子进程一起杀掉。

## 6. 备份

| 内容 | 路径 | 建议 |
|---|---|---|
| 账号 / 任务 / 数据集元数据 | `data/db/*.json` | 每日备份，体积很小 |
| 审计日志 | `data/audit/audit.jsonl` | 每日备份，追加写 |
| 数据集 | `data/datasets/` | 按需，原始压缩包一般在别处还有 |
| 训练产物 | `data/outputs/` | 按需，通常很大 |
| 任务过程文件 | `data/jobs/` | 可用 `scripts/manage.py gc` 定期清理 |

```bash
tar czf prt-db-$(date +%F).tgz data/db data/audit
```

## 7. 常见问题

**任务一直排队不启动**
- 「平台管理 → 系统设置」里的全局并发已用满，或者账号自身的并发配额用满。
- 申请的 GPU 数超过空闲卡数（大屏上能看到哪几张卡被谁占用）。

**任务刚启动就失败**
- 打开任务详情 → 「配置与命令」，直接把 `run.sh` 里的命令复制到终端跑一遍，错误会更直观。
- 多半是对应框架的 `PRT_*_PYTHON` 环境缺依赖。

**评测全部 reward=0**
- 先用 `oracle` agent 跑一遍：如果 oracle 也不是 1.0，问题在任务集本身（`tests/test.sh` 或 `solution/solve.sh`）。
- 检查 agent 需要的 API Key 是否填写；密钥在界面里是密码框，值不会回显。

**大屏没有 GPU 数据**
- 平台环境里需要能 `import pynvml`（`nvidia-ml-py`）且宿主机有 NVIDIA 驱动。缺失时其它指标照常工作。
