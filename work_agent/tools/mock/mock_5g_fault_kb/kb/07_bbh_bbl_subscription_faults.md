# BBH/BBL 订阅握手与 DEBUG-only 故障

## 为什么可能完全没有 ERROR

BBH 与 BBL 的传输连接不是一个布尔开关，而是多个方向的发布/订阅闭环。
连接中断时，组件可能仍处在设计允许的重试窗口，只持续打印 DEBUG：

```text
[BBH][DEBUG] request_subscribe direction=RX peer=bbl attempt=10 link_state=CONNECTING
[BBH][DEBUG] request_subscribe direction=RX peer=bbl attempt=11 link_state=CONNECTING
[BBH][DEBUG] request_subscribe direction=RX peer=bbl attempt=12 link_state=CONNECTING subscription_ack_missing=true
[BBL][DEBUG] cell_state state=CONFIGURING wait_for=rx_subscription elapsed_ms=12000
```

这类场景不能用“是否出现 ERROR”作为唯一判据。高价值信号是：

- 同一 `topic + direction + peer` 的 attempt 单调增加；
- 一直没有配对的 `subscribe_ack`；
- `link_state=CONNECTING` 或 `route_state=WAITING` 长期不变；
- 小区状态停留在 `CONFIGURING`，等待项恰好是该订阅。

## 正常闭环与故障闭环

正常瞬态重试：

```text
request_subscribe direction=RX attempt=1 ack=false
subscription retry backoff_ms=500 transient=true
subscribe_ack direction=RX attempt=2 subscription_state=ESTABLISHED
subscription_matrix RX=ESTABLISHED TX=ESTABLISHED
cell_state=ACTIVE
```

不能只看到第一条 `ack=false` 就判故障。必须检查同一时间窗口的后续事件是否恢复。

故障模式 A（RX）：BBH 重复请求，BBL 的订阅表一直 `EMPTY`，可能是网络路径断开、
端口/路由错误或进程未接收请求。

故障模式 B（TX）：BBL 重复 `publish_announce` 但 `subscriber_count=0`，BBH 同时
持续订阅且 `ack=false`，若 COMM 又有 `route_lookup result=MISS`，优先归传输/路由环境。

## 推荐检索

1. 跨组件搜索 `request_subscribe|publish_announce|subscribe_ack`。
2. 按 `topic` 和 `direction` 对齐两端时间线。
3. 搜索 `subscription_state=ESTABLISHED`，确认是否只是瞬态重试。
4. 找不到显式错误码时，输出行为证据链；不要杜撰 errorcode。

## 归因边界

- 重试后 ESTABLISHED 且小区 ACTIVE：`fail_kind=none`。
- 请求持续无 ACK，COMM 路由/连接异常：通常 `fail_kind=env`。
- topic 名或方向由用例生成错误，且双方网络正常：可归 `case`。
- 只有 BBL 的 `CELL_LOAD_FAILED`，没有订阅或上游时间窗：证据不足，归 `unknown`。
