# 错误码、MARP 与 Compare 级联归因

## 错误码不是根因标签

本项目的 synthetic 错误码分为两类：

- 根因码：如 `E-BBH-2101 CLOCK_REFERENCE_UNLOCKED`、
  `E-MARP-4106 ANTENNA_MAP_MISSING`；
- 级联码：如 `E-BBL-3104 CELL_SETUP_DEPENDENCY_FAILED`、
  `E-CMP-5102 KPI_OUT_OF_TOLERANCE`。

字段 `probable_component` 只决定下一步读哪个日志，不足以单独证明根因。
看到 `cascade=true`、`DEPENDENCY_FAILED`、`NOT_READY` 时应向时间线前方追。

## VUE 小区建立失败

典型链路：

```text
[MARP][ERROR] ANTENNA_MAP_MISSING errorcode=E-MARP-4106 field=antenna_map
[BBL][ERROR] VUE_CELL_SETUP_FAILED errorcode=E-BBL-3108 probable_component=marp cascade=true
```

根因在 MARP/用例配置，不在最后报错的 BBL。若 MARP 报
`BEAM_CALIBRATION_MISMATCH`，还要比较 `profile_revision` 与
`calibration_revision`，版本不同应归 `version`。

## Compare 失败

Compare 是最终判定者，经常只描述症状：

```text
[COMPARE][ERROR] KPI_OUT_OF_TOLERANCE metric=dl_bler probable_component=bbh cascade=true
```

若更早的 BBH 日志有 eCPRI sequence gap、packet loss burst 或 symbol drop，根因在
前传环境。只有 Compare 自己出现 `BASELINE_SCHEMA_MISMATCH result_schema=v5
baseline_schema=v4` 时，才把 Compare/基线版本作为根因。

## 推荐工具顺序

1. `lookup_error_code` 获取候选组件和 `is_root_code`。
2. `grep_logs(pattern=错误码)` 找原始时间点。
3. 按 `probable_component` 定点读取对应组件。
4. 用更早的具体状态/参数错误验证，不以目录解释代替日志证据。
