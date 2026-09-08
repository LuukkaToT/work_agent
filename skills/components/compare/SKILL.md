# COMPARE 检查点

关注结果对比、基线 schema 与 KPI 判定，通常是现象层。

- 基线：schema / 结果版本不兼容、对比未跑 `verdict=NOT_RUN`。
- KPI：`KPI_OUT_OF_TOLERANCE`、`actual=` `baseline=`、`probable_component=`。
- `cascade=true` 或 `probable_component` 指向 bbh/bbl/marp 时，本组件结论只能是对比失败现象，建议主诊断去对应组件取证。
- 健康对照：`verdict=pass` 且无 ERROR 时，报告覆盖范围并标 no_hit 或明确「对比通过」，仍不要用对比通过否定其它组件已有反证。
