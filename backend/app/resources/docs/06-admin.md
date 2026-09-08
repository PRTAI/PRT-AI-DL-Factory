---
title: 管理员指南
category: 管理
order: 6
summary: 建账号、审核任务、配额与工具链健康
---

# 管理员指南

## 账号

平台**不开放自助注册**。平台管理 → 账号管理 → 新建账号，填邮箱、点「随机生成」拿初始密码，
把邮箱和密码发给成员，对方首次登录会被要求改密。

| 能力 | 成员 | 管理员 |
|---|---|---|
| 提交任务、看自己的日志 | ✅ | ✅ |
| 看**所有人**的任务 | ❌ | ✅ |
| 审核 / 驳回任务 | ❌ | ✅ |
| 终止任务 | 仅自己的 | 全部 |
| 管理账号、系统设置、审计日志 | ❌ | ✅ |

## 审核

平台管理 → 待审核，或任务列表里筛选「待审核」。

**通过之前建议点一下「查看命令」**——那里是平台将要执行的完整命令行与配置文件，
GPU 数、batch、数据集路径都在里面，比只看表单更容易发现问题。

驳回时写清原因，提交人会收到站内通知。

## 配额

| 位置 | 作用 |
|---|---|
| 系统设置 → 全局并发任务数 | 同时运行的任务上限 |
| 系统设置 → 单任务默认 GPU 上限 | 成员未单独设置时生效 |
| 账号管理 → 并发任务上限 / 单任务 GPU 上限 | 覆盖某个成员的默认值 |
| 系统设置 → 任务需要管理员审核 | 关掉后成员提交即排队，请谨慎 |
| 系统设置 → 全局公告 | 会显示在所有页面顶部 |

## 工具链

平台管理 → 工具链。这一页不是只检查命令存不存在，而是**用配置的解释器真的
`import` 一次框架和 torch，并 ping 一次 docker 守护进程**，所以显示绿色就等于任务真的能起来。

| 状态 | 含义 |
|---|---|
| 可用 | 能直接启动任务 |
| 依赖缺失 | 解释器在，但没装 torch / 框架，需要指定正确的 `PRT_*_PYTHON` |
| 未安装 | 命令或解释器根本不存在 |

### Docker 权限

Harbor 评测默认用 docker 沙箱，运行平台的用户必须能访问 `docker.sock`：

```bash
sudo usermod -aG docker $USER
newgrp docker            # 或重新登录后生效
docker version           # 能打印 Server 版本即可
```

没权限时工具链页会直接标红并给出这条命令。

## 运维命令

```bash
python scripts/manage.py list-users                   # 看账号
python scripts/manage.py create-user ops@team.com --role admin
python scripts/manage.py reset-password ops@team.com  # 随机新密码
python scripts/manage.py unlock-admin                 # 管理员全部失联时恢复
python scripts/manage.py gc --days 30 --dry-run       # 清理旧任务工作目录（产物保留）
```

## 备份

| 内容 | 路径 | 频率 |
|---|---|---|
| 账号 / 任务 / 数据集元数据 | `data/db/*.json` | 每日，体积很小 |
| 审计日志 | `data/audit/audit.jsonl` | 每日 |
| 数据集 / 训练产物 | `data/datasets/`、`data/outputs/` | 按需，通常很大 |

```bash
tar czf prt-db-$(date +%F).tgz data/db data/audit
```

## 审计日志

平台管理 → 审计日志，记录登录、任务提交/审核、账号变更、数据集与模型操作，可按类别筛选。
成员在「个人设置」里能看到自己的操作记录。
