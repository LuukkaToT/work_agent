# MARP 检查点

关注天线映射、波束校准与 VUE 小区参数，而不是对比模块的 KPI 超差本身。

- 天线：`ANTENNA_MAP_MISSING`、映射表缺项、`errorcode=` 以 MARP 为前缀。
- 校准：波束校准版本不匹配、校准文件与小区 profile 对不上。
- VUE 小区失败时先确认本组件是否给出明确 MARP 码，再建议 compare 是否只是现象。
- 无命中时写清是否拉过 `marp.log` 以及检索过哪些码，禁止「MARP 正常」。
