# RAT 检查点

关注无线配置、小区参数与调度进程，而不是基带时钟或对比 KPI。

- 配置拒绝：`RAT_PROFILE_REJECTED`、带宽/SCS/频段越界、`field=` `value=` `allowed=`。
- 进程：调度启动后立刻退出、`CELL_BAND unresolved`。
- 级联：下游 BBH/BBL 出现 `DEPENDENCY_FAILED` / `NOT_READY` / `cascade=true` 且 `probable_component=rat` 时，回到本组件最早 ERROR。
- 接入：PRACH / RRC 异常要区分是 RAT 配置还是 BBH 时序；只记录本组件证据。
- 不要把 COMM 端口失败或 BBH 时钟失锁写成 RAT 根因。
