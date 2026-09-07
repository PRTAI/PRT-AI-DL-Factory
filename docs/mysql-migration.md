# 从文件存储迁移到 MySQL

平台当前用 JSON 文件存储（`data/db/*.json` + `filelock`），这是刻意设计的过渡形态：
**所有持久化都收在 `backend/app/store/` 后面**，业务代码只依赖一组很小的方法。

## 1. 现有接口

`JsonCollection[T]`（每个集合一个 JSON 数组）：

```python
all()                        -> list[T]
get(key)                     -> T | None
find(predicate)              -> list[T]
first(predicate)             -> T | None
count(predicate=None)        -> int
put(item)                    -> T          # upsert
put_many(items)              -> None
replace_all(items)           -> None
update(key, mutate)          -> T | None   # 加锁的读-改-写
delete(key)                  -> bool
```

`JsonDocument[T]`（单个对象，系统设置用）：`read() / write(value) / update(mutate)`
`JsonlLog`（追加型，审计日志用）：`append(record) / tail(limit, predicate)`

集合在 `backend/app/store/registry.py` 里统一装配：

```python
class Store:
    users / jobs / datasets / downloads / notifications  # JsonCollection
    settings_doc                                          # JsonDocument
    audit                                                 # JsonlLog
```

## 2. 迁移步骤

1. **建表**。每个集合一张表即可，先用「主键 + 若干可查询列 + JSON 列」的混合模式，
   改动最小且能保留 pydantic 模型：

   ```sql
   CREATE TABLE jobs (
     id           VARCHAR(32) PRIMARY KEY,
     owner_id     VARCHAR(32)  NOT NULL,
     status       VARCHAR(24)  NOT NULL,
     type         VARCHAR(16)  NOT NULL,
     framework    VARCHAR(24)  NOT NULL,
     created_at   DATETIME(3)  NOT NULL,
     updated_at   DATETIME(3)  NOT NULL,
     payload      JSON         NOT NULL,        -- Job.model_dump(mode="json")
     INDEX idx_jobs_owner_status (owner_id, status),
     INDEX idx_jobs_created (created_at)
   ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
   ```

   `users` / `datasets` / `downloads` / `notifications` 同理；`audit` 用自增主键的追加表。

2. **实现 `SqlCollection`**，方法签名与 `JsonCollection` 完全一致：
   - `get/put/delete` 直接映射；
   - `find(predicate)` 有两种做法：先把热点查询改成显式条件方法（推荐），
     或者过渡期先 `SELECT payload FROM ...` 再在 Python 侧过滤；
   - `update(key, mutate)` 用 `SELECT ... FOR UPDATE` + 事务，替换掉 filelock。

3. **改装配**。只改 `registry.py` 里的几行：

   ```python
   self.jobs = SqlCollection(engine, "jobs", Job)
   ```

   `services/`、`api/`、`builders/` 一行都不用动。

4. **数据搬迁**：

   ```python
   from backend.app.store import store           # 旧的文件实现
   for job in store.jobs.all():
       sql_jobs.put(job)                          # 新实现
   ```

5. **回归**：`PYTHONPATH=$PWD python -m pytest`。测试只依赖公开行为，
   把 fixture 指向新实现即可复用（`conftest.py` 里通过环境变量切换）。

## 3. 需要一并考虑的点

- **并发**：文件实现用 `filelock` 保证跨进程安全；换成数据库后可以去掉锁，
  但 `update()` 必须在事务里做，避免并发审核/取消互相覆盖。
- **多副本**：迁到 MySQL 之后仍不能直接开多 worker——调度器与遥测采样是进程内单例。
  正确做法是把调度拆成单独进程（只有它派发任务），API 进程可以任意扩容。
- **审计日志**：`audit.jsonl` 是纯追加、按行的，迁移成表以后建议保留按天分区或定期归档。
- **大字段**：任务的曲线数据已经写在 `data/jobs/<id>/metrics.json`，没有进数据库，迁移时不需要处理。
