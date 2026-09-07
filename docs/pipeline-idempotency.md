# 流水线幂等接口与故障恢复

公司流水线服务按本文实现服务端幂等，Testing Agent 负责生成操作键、冻结请求、保存结果和从断点续跑。仅发送请求头不能保证幂等：服务端必须同时实现业务效果的去重和结果回放。

## HTTP 契约

创建接口与启动接口均接收 `Idempotency-Key` 请求头，路径沿用 `PIPELINE_API_CREATE_PATH`、`PIPELINE_API_START_PATH` 的配置。查询接口保持只读。

```http
POST /pipelines
Authorization: Bearer <服务令牌>
Content-Type: application/json
Idempotency-Key: <Testing Agent 生成的稳定操作键>

{"spec":{"...":"按现有流水线请求格式填写"}}
```

服务端将键视为不透明字符串，在经认证的调用方范围内唯一。创建和启动使用不同键；一次任务内重试和恢复始终沿用原键。用户有意发起的新任务即使参数相同，也获得新键。

服务端同时保存请求指纹：请求方法、路径和规范化 JSON 请求体的 SHA-256。JSON 对象键排序、无多余空白、保留数组顺序，不接受非有限数字；不能因 JSON 对象字段顺序不同判为不同请求。

| 同一调用方下的请求 | 服务端行为 |
| --- | --- |
| 首次使用该键 | 原子登记并执行一次，持久化最终响应 |
| 同键同参数，已完成 | 回放原 HTTP 状态码和响应体，返回原流水线 ID |
| 同键同参数，仍执行中 | 返回 `409`、错误码 `IDEMPOTENCY_IN_PROGRESS` 和 `Retry-After` |
| 同键不同参数 | 返回 `409`、错误码 `IDEMPOTENCY_CONFLICT`，不执行新请求 |

创建成功的响应沿用现有解析格式，例如：

```json
{"data":{"pipelineId":"pipeline-123"}}
```

启动成功例如 `{"ok":true}`。启动的去重记录代表同一次启动命令已受理，不要求等待整条流水线运行结束。

执行中示例：

```http
HTTP/1.1 409 Conflict
Content-Type: application/json
Retry-After: 3

{"code":"IDEMPOTENCY_IN_PROGRESS","message":"该操作仍在执行，请稍后使用原键重试"}
```

参数冲突示例：

```http
HTTP/1.1 409 Conflict
Content-Type: application/json

{"code":"IDEMPOTENCY_CONFLICT","message":"该操作键已经绑定不同请求"}
```

## 服务端必须保证的原子性

在同一事务中创建流水线与写入幂等成功结果，并以唯一约束处理并发请求。若启动通过队列实现，将启动任务与幂等受理记录同事务写入持久化发件箱，再由消费者按操作键去重。不能只在进程内加锁，或先完成副作用、稍后另行保存去重结果。

客户端超时并不证明操作失败。服务端提交成功后即使响应丢失，同键重试也必须返回原结果。进程退出后，业务效果已提交的操作不能成为可再次执行的“新请求”；未提交的事务可回滚后由原键重新执行。

去重记录本版不自动过期。若后续需要清理，应保留不可重新执行的键标记；过期旧键应明确拒绝，不能当成首次请求。认证令牌轮换不应改变同一调用方的去重范围。

## Testing Agent 的持久化边界

首次发送前记录操作键及完整请求快照，包括平台默认参数、调试开关和目标接口。恢复时使用原快照，避免修改个人配置或部署配置后向同一键发送不同请求。认证凭据不写入操作快照。

创建、启动各自记录结果。已经保存成功的操作直接复用，并补齐台账；远端已提交但本地未保存结果的操作，依靠服务端同键回放找回原结果。冲突停止恢复，执行中或暂时不可用保留待恢复状态，不在服务内无限循环重试。

在线 mock 将流水线和模拟服务端去重记录存入 Postgres，用于验证重启、并发及响应丢失。直接构造的离线 mock 可使用内存存储。

## MCP 客户端调用顺序

客户端保留 `testing_agent_turn` 返回的 `thread_id`。后续所有恢复都沿用该 ID 和同一 `user_id`。

1. 调用 `testing_agent_status` 查询状态。
2. `running`：原请求仍在执行，稍后查询。
3. `waiting_input`：将问题展示给用户，用 `testing_agent_resume` 提交回答。
4. `recoverable`：调用 `testing_agent_recover` 续跑，不发送新用户消息。
5. `blocked`：根据返回的脱敏错误码处理配置或兼容问题。
6. `done`：读取本轮结果。再次调用恢复只会返回当前结果。

```json
{"name":"testing_agent_recover","arguments":{"thread_id":"z00888363-会话标识","user_id":"z00888363"}}
```

恢复工具可能继续遇到人工确认，这时返回 `waiting_input`，不能自动替用户作答。`turn` 和 `resume` 超时后均先查询状态，不盲目重放；本协议不把重新发送的全新 `turn` 自动认定为原任务。

## 部署与验证

先执行 `python -m scripts.migrate_database` 升级 Postgres，再启动服务。历史已完成会话可继续查看；缺少新执行版本标记的未完成会话返回 `LEGACY_RECOVERY_UNSUPPORTED`，不能通过补一个新键重放旧写操作。

公司服务确认实现上述契约后设置：

```ini
PIPELINE_API_IDEMPOTENCY_SUPPORTED=true
```

默认值为 `false`。未启用时，结果不明的真实写操作不会自动重发，避免旧服务忽略请求头导致重复创建。此开关表示所连接服务已实现契约，不会替服务端实现去重。

验收需使用独立的 `POSTGRES_TEST_DSN`，验证同键并发、参数冲突、响应丢失、跨进程恢复、台账补写，以及诊断已完成工具不被重新调用；只通过内存测试不足以证明跨进程持久化。
