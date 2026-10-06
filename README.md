# 海洋测绘航迹导出服务

海洋测绘队向协作方导出航迹前，值班员在页面上：

1. 编辑**当前字段遮蔽规则**；
2. 提交**少量 JSON 航迹记录**，服务返回**稳定导出标识 + 首次回执**；
3. 通过真实 API 轮询查看**处理阶段、冻结规则摘要、已发布工件摘要**并下载。

全程仅依赖 **Python 3.11 标准库**（`http.server` + `sqlite3`），无需安装任何第三方包。

## 一致性保证（对应需求）

| 需求 | 实现 |
| --- | --- |
| 提交时在**同一持久化裁决**中冻结规范化输入与规则快照 | `Storage.adjudicate_submission` 在单条 `BEGIN IMMEDIATE` 事务内完成"查重判定 + 写入冻结快照 + 首次回执 + 阶段日志"（`app/storage.py`） |
| 稳定导出标识 | `export_id = exp_ + sha256(规范化记录 + 规范化规则)`，业务键为航迹身份（`ship_id`+`track_id`）（`app/models.py`） |
| 提交后改动当前规则不影响已受理标识 | 工件始终由**冻结快照** `rules_snapshot` 渲染；当前规则只影响后续新提交 |
| 仅持有效租约的进程可处理 | 处理前必须 `acquire_lease/claim_next_pending`；租约带 TTL，他人持有时拒绝，崩溃后 TTL 过期可恢复认领 |
| 先临时工件、核验摘要、再原子发布 | `tmp/<id>.tmp.<owner>.*` 写入并 fsync → 磁盘 sha256 与冻结重算摘要一致后进入 `VERIFIED` → `os.replace` 原子落盘 → 单事务登记 `PUBLISHED`（`app/worker.py`） |
| 崩溃退出后重启：清理残缺件或收敛到同一完整工件 | 启动处理时 `_reconcile`：最终件完整则收敛；按摘要在残留临时件中选完整件并删除残缺件；都不可用则按冻结快照重写。恢复认领的阶段日志不回退 |
| 下载接口不暴露未核验内容 | 仅 `PUBLISHED` 且磁盘 sha256 与发布登记一致才返回，否则 `409 not_published` / 篡改 `409` / 缺失 `410` |
| 两个工作进程并行，只发布一次 | SQLite 写事务串行裁决 + 租约互斥；`commit_publish` 以 `PUBLISHED` 为吸收态，并发败者幂等收敛，绝不产生第二个工件 |
| 已发布阶段不得倒退 | 阶段机 `ACCEPTED→LEASED→TEMP_WRITTEN→VERIFIED→PUBLISHED`（`FAILED` 终态），`advance` 仅允许单调前进 |
| 业务等价重传返回首次回执、不产生第二工件 | 业务键命中且输入/规则哈希一致 → `200 duplicate`，回传同一 `export_id` 与同一 `receipt_id` |
| 记录或规则快照不同 → 明确冲突并保留原证据 | 业务键命中但哈希不一致 → `409 conflict`，返回双方哈希与差异标志，拒绝写入、不动既有工件 |
| 页面真实 API 轮询 | `web/` 纯静态页，`GET /api/exports` 每 2 秒轮询 |
| Compose 可配置宿主端口 / 健康响应 / 可执行 verify | `${WEB_HOST_PORT:-8080}`、`/health` + healthcheck、`verify` profile |
| verify 为执行后退出的一次性验收服务 | `python -m verify.run`：构建检查 + 代码测试 + 真实子进程 HTTP 冒烟，退出码 0/1 |

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康响应 |
| GET | `/api/rules` | 查看当前遮蔽规则 |
| PUT | `/api/rules` | 编辑当前规则 `{"rules":{"mask_fields":["note"]}}`（去重/排序规范化） |
| POST | `/api/exports` | 提交 `{"records":[...], "rules"?: {...}}`；不附 `rules` 时使用当前规则。201 新建 / 200 重传 / 409 冲突 |
| GET | `/api/exports/{id}` | 阶段、冻结规则摘要、工件 sha256、阶段日志 |
| GET | `/api/exports` | 最近导出列表（页面轮询） |
| GET | `/api/exports/{id}/artifact` | **仅发布且核验一致**可下载，否则 409 |

记录支持的字段：`ship_id, track_id, timestamp, longitude, latitude, sog, cog, heading, depth, note`（允许额外字段）。

## 本地运行（无需 Docker）

```bash
python3 -m app.main                 # API（8080）+ 同进程一个工作线程
python3 -m app.main --no-worker     # 只起 API
python3 -m app.worker_main          # 独立工作进程（可启动多个副本）
```

环境变量：`DATA_DIR`、`WEB_HOST`、`WEB_PORT`、`LEASE_SECONDS`、`POLL_SECONDS`、`WORKER_ID`。

## Docker Compose

```bash
docker compose up web worker-a worker-b         # 应用栈，宿主端口默认 8080
WEB_HOST_PORT=9090 docker compose up web        # 可配置宿主端口
docker compose run --rm verify                  # 一次性验收，退出码报告结果
```

- `web`：API 服务，带 `/health` 健康检查；
- `worker-a` / `worker-b`：两个竞争工作进程，验证租约互斥与只发布一次；
- `verify`：只运行一次的验收容器（profiles: `verify`）。

## 测试与验收

```bash
python3 -m unittest discover -s tests -v   # 23 项单元/集成/HTTP/并行测试
python3 -m verify.run                      # 构建检查 + 测试 + 三大冒烟场景
echo $?                                    # 0 验收通过，1 存在失败
```

`verify` 的 HTTP 冒烟全部使用**真实子进程 + 真实 socket**，场景：

1. **冻结快照**：提交后扩大当前遮蔽规则，已受理导出仍按冻结快照导出；
2. **崩溃恢复**：工作进程在临时工件写入后 `os._exit(3)`，重启后按日志/摘要清理残缺件并收敛到同一完整工件，崩溃期间下载始终 409；
3. **并行互斥**：两个工作进程竞争，6 个导出各恰好发布一次、阶段日志单调；
4. **重传与冲突**：业务等价重传返回首次回执且无第二工件；规则快照或记录不同返回 409 并保留原证据。

## 目录

```
app/models.py      规范化 / 稳定标识 / 业务键 / 工件渲染与摘要
app/storage.py     事务裁决、冻结快照、租约、单调阶段机、唯一发布登记
app/worker.py      临时件→核验→原子发布、崩溃恢复/收敛、租约主循环
app/server.py      HTTP API 与下载门控
app/main.py        API 入口（可选内嵌工作线程）
app/worker_main.py 独立工作进程入口
web/               值班台页面（真实 API 轮询）
tests/             单元 / 集成 / HTTP / 并行测试
verify/run.py      一次性验收服务
```
