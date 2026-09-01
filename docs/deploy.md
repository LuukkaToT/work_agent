# 部署与可观测接入

应用契约固定为：**stdout JSON + `/healthz` `/readyz` `/metrics` + 环境变量**。公司日志平台 / Prometheus / Ingress 自己刮，仓库不绑死 ELK 或某一家 APM。

**明天上线前先勾 [go-live-checklist.md](go-live-checklist.md)。**

## 本地

```text
docker compose up -d postgres
python -m work_agent.cli init-db
uvicorn work_agent.api.app:app --host 0.0.0.0 --port 8000
```

或 `docker compose up --build`（app 容器启动时会跑 `init-db`）。

探活：`GET /healthz`（进程）、`GET /readyz`（Postgres `SELECT 1`）、`GET /metrics`（Prometheus 文本）。三者都不走 `X-User-Id`。

## 接现有 DevOps / K8s

1. 用仓库根 [Dockerfile](../Dockerfile) 构建镜像；密钥（`POSTGRES_DSN`、`LLM_API_KEY`、`MCP_SERVICE_TOKEN`）走公司 Secret，不要打进镜像。
2. **先迁移再切流量**：`python -m work_agent.cli init-db`（K8s 示例在 initContainer）。API 启动只 `verify_schema_and_seed`，不会替你改表结构。
3. 探针：liveness=`/healthz`，readiness=`/readyz`。
4. 日志：采集容器 stdout。每条 turn 一行 `turn_complete` / `turn_failed`（含 `request_id`、`thread_id`、`intent`、`duration_ms`、诊断摘要）。**没有用户原文、没有完整 audit。**
5. 指标：刮 `/metrics`。面板草稿 [deploy/grafana/work-agent-ops.json](grafana/work-agent-ops.json)，告警草稿 [deploy/prometheus/alerts.yaml](prometheus/alerts.yaml)。
6. K8s 示例：[deploy/k8s/deployment.yaml](k8s/deployment.yaml)。Ingress/TLS 留给平台。

平台必须配合的约束：

- 网关 **直连 Postgres 或 PgBouncer session 池**，禁止事务池（advisory lock 绑会话）。
- MCP `stateless_http=False` 时多副本要粘滞，或先 `replicas: 1`。HITL 重入走 checkpoint，不靠 MCP 长连接。
- `workspace/` 默认空本地盘：诊断归档、评测缓存不跨 Pod 共享。
- worker 先 1：一次 turn 会占一条 PG 连接直到结束，池 `max_size=10`。

## 上线怎么看评测效果

三层数字不能横比、不能报公开榜：

- **发版前离线**：`eval-diagnose`、`eval-react --limit 20`。看 `evidence_recall` / AST，写进发版说明。
- **CI**：pytest。有内网 LLM 再加 BFCL 冒烟。
- **线上运营**：日志里的 `error_analysis` 摘要和 `/metrics` 的延迟、token、`fail_kind` 分布、`respond.source=fallback`、`THREAD_BUSY`。

线上没有 golden 标签，**算不出** kind accuracy / evidence_recall。慢了、贵了、fallback 多了才是线上评测。
