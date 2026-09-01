# 上线前自检清单

明天开服务前按顺序勾。现网约定：**流水线 create/start/query 已调通，失败拉诊断日志已支持，公司库已建。** 这份清单只拦「发错包 / 指错库 / 环境没配齐」。

部署方式见 [deploy.md](deploy.md)。

---

## 0. 发布包是不是现网那份

本仓库笔记本快照里，`work_agent/tools/real/logs.py` 曾是 `NotImplementedError` 占位。现网若已经接好拉日志，**不要用旧 stub 覆盖过去。**

- [ ] 即将部署的代码里，`RealLogTool.fetch_logs` / `grep_logs` / `list_logs` **没有**再抛 `NotImplementedError`
- [ ] `TOOL_BACKEND=real` 时 `get_pipeline_tool()` / `get_log_tool()` 走的是公司侧已调通实现
- [ ] `.env` 里是 `TOOL_BACKEND=real`，不是默认的 `mock`

不通过：先合现网 `logs.py`（以及 pipeline payload/client），再发。

---

## 1. 库：Agent 台账 ≠ 流水线平台库

公司库「已建」只说明有地方可连。Agent 还要有**自己的**表。`POSTGRES_DSN` 必须指向 Agent 库，不要误填流水线平台业务库。

在 Agent 库里检查：

- [ ] 能连上（`GET /readyz` 之后应为 200）
- [ ] 表 `schema_migrations` 有版本 `001_capacity_topology_catalog`（与 `sql/migrations/` 最新文件名一致）
- [ ] 有 `pipelines` / `user_config` / `logic_topologies` / `ci_cases`
- [ ] 有 LangGraph checkpoint 相关表（`init-db` 会 `PostgresSaver.setup()`）

没有就对 **Agent 库**执行一次，不要对流水线平台库执行：

```text
python -m work_agent.cli init-db
```

API 启动只会 `verify_schema_and_seed`，**不会**替你改结构。缺版本时进程会拒启动。

Postgres 约束：

- [ ] 网关直连 Postgres，或 PgBouncer **session 池**（禁止事务池，advisory lock 会失效）

---

## 2. 环境变量（real）

至少：

- [ ] `POSTGRES_DSN` → Agent 库
- [ ] `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`（内网网关可达）
- [ ] `TOOL_BACKEND=real`
- [ ] `PIPELINE_API_BASE_URL`
- [ ] `PIPELINE_API_TOKEN`，或 `PIPELINE_API_USERNAME` + `PIPELINE_API_PASSWORD`
- [ ] 若路径不是默认 `/pipelines`：已设 `PIPELINE_API_CREATE_PATH` / `START_PATH` / `QUERY_PATH`

建议：

- [ ] `LOG_FORMAT=json`、`LOG_LEVEL=INFO`
- [ ] CLI 冒烟时设 `WORK_AGENT_USER_ID`（合法工号：一位字母 + 8 位数字，如 `z00888363`）

开 MCP（对 Agent Space 暴露）时必须同时有，缺一启动失败：

- [ ] `MCP_ENABLED=true`
- [ ] `MCP_SERVICE_TOKEN`
- [ ] `MCP_ALLOWED_HOSTS`

W3 检索未配不挡创建/拉日志；旁证会空。

密钥走 Secret / `.env`，不要打进镜像、不要提交 git。

---

## 3. 进程怎么起

- [ ] 单副本（MCP 仍 `stateless_http=False`；多副本会丢 session）
- [ ] uvicorn worker = 1（锁占 PG 连接贯穿整轮 LLM）
- [ ] `GET /healthz` → 200
- [ ] `GET /readyz` → 200（失败先查 DSN / 库是否起来）
- [ ] `GET /metrics` 能打开（不要求当天接 Grafana）

```text
uvicorn work_agent.api.app:app --host 0.0.0.0 --port 8000
```

HTTP 请求带头：`X-User-Id: z00888363`（换成你的工号）。这是格式校验，不是 SSO。

---

## 4. 功能冒烟（必须做完再叫人试用）

用**真实环境、真实工号**，不要用 mock 场景名当成功。

1. [ ] 创建流水线：一句话带用例名 + 版本 + 环境（物理 IP 或完整逻辑组网）
2. [ ] HITL 确认页看到的是结构化参数，不是模型原文；确认后平台侧真有 `pipeline_id`
3. [ ] Agent 台账 `pipelines` 能查到该 id（不是一直停在 `local-*`）
4. [ ] 启动成功；再问进度，`query` 与平台一致
5. [ ] 对一条**失败**流水线做诊断：能拉到日志（`fetch_logs` / `grep_logs`），回复里有具体 ERROR/组件，不是 `[fetch_logs error] NotImplementedError`
6. [ ] 同一 `thread_id` 第二轮「查刚才那条」能对上，没有串会话

任一步失败：先看 stdout 里 `turn_failed` / `turn_complete` 的 `request_id`、`thread_id`、`code`，再查平台侧任务是否双建。

---

## 5. 对同事怎么划边界

可以说：

- 能真实创建、启动、查询流水线
- 失败后能拉日志做诊断
- 写操作前有人工确认

先不要承诺：

- 公司 SSO / 正式权限
- 多实例 MCP 无粘滞
- 知识库 / 错误码目录一定全（`lookup_error_code`、W3 以现网为准）
- 评测分数等于生产准确率（`eval-diagnose` / BFCL 是离线门槛）

---

## 6. 当天出问题先看哪

| 现象 | 先查 |
|------|------|
| `/readyz` 503 | `POSTGRES_DSN`、库进程、是否跑过 `init-db` |
| 启动报未升级到 `001_...` | 指错库，或没迁 Agent schema |
| create 失败 | `PIPELINE_API_*`、token、路径覆盖 |
| 诊断没有日志 / NotImplemented | 发布包仍是 stub `logs.py` |
| 409 该会话忙 | 同一 thread 并发了；等上一轮结束或查是否卡在 HITL |
| 500 `internal error` | 用响应头 `X-Request-Id` 对 stdout 栈 |
| MCP 连不上 | token、Host 白名单、是否多副本 |

创建已成功但台账仍是 `local-*`：服务端可能已有任务，本地 `replace_id` 失败——不要盲目再 create（防双建）。记下平台 id 再人工补台账。
