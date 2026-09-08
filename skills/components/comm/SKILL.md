# COMM 检查点

关注传输与控制通道，而不是小区协议本身。

- TCP / 端口：`Connection refused`、SYN 超时、`endpoint=`、`socket_state`。
- 握手：协议版本不兼容、对端未监听、反复 `reconnects` 且 `packets_rx=0`。
- 与 RAT/BBH 的关系：COMM 失败时常被下游标成 `cascade=true`；本组件要确认自己是否是最早故障。
- 无 ERROR 时仍要看 DEBUG 通道是否长期 `SYN_SENT` / `CONNECTING`。
- 本组件通道正常时，只缩小范围，不要把下游超时写成 COMM 根因。
