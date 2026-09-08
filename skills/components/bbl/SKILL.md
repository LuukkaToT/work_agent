# BBL 检查点

关注小区激活、TX 发布与订阅表，不要把上游级联错误当成 BBL 自身根因。

- 激活：`CONFIGURED->ACTIVATING`、`CELL_ACTIVATION_TIMEOUT`、`wait_for=first_slot_indication`。
- 订阅：RX 表 `state=EMPTY`、TX `publish_announce` 是否有对应 ACK。
- 资源：重复 PCI、小区上下文池耗尽。
- 级联：`CELL_SETUP_DEPENDENCY_FAILED` + `cascade=true` 通常不是首个根因，记录后建议主诊断查 `probable_component`。
- DEBUG 长期等待 `rx_subscription` 且无 ERROR，仍要报告覆盖范围，不能写正常。
- 与 BBH 时钟/订阅的关联只作为 followup，不在本报告里下全局结论。
