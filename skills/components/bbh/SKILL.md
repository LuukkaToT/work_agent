# BBH 检查点

关注时钟、前传和与 BBL 的 RX 订阅，而不是把 BBL 激活超时直接当根因。

- 时钟：`ptp_state=UNLOCKED` / `HOLDOVER`、`offset_ns`、`holdover_ms`、SyncE `LOS`。
- 前传：eCPRI 序号缺口、`expected_seq` / `received_seq`、slot indication gap。
- 握手订阅：RX `request_subscribe` 是否最终出现 `subscribe_ack` / `subscription_state=ESTABLISHED`。
- 无 ERROR 的停滞：同一 `request_subscribe` 多次 `attempt=` 且 `subscription_ack_missing`。
- 短暂重试后恢复（随后 ESTABLISHED）不是故障；只记录现象。
- BBL 的 `CELL_ACTIVATION_TIMEOUT` 且 `probable_component=bbh` 是相关现象，需本组件时钟/slot 证据才能支持因果。
