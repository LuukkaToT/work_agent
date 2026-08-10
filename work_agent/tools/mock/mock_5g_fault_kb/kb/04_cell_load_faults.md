# CELL 小区加载与激活失败

## 1. 小区参数冲突

常见关键词：

- duplicate PCI
- cell identity conflict
- frequency conflict
- invalid TAC
- invalid ARFCN

### 示例

```text
[CELL][ERROR] duplicate PCI detected: pci=101 cells=[NR_CELL_0, NR_CELL_1]
```

若配置规则要求同一局部范围内不能出现该冲突，可直接归为小区配置错误。

## 2. RAT 未 READY 导致的小区失败

```text
[RAT][ERROR] RAT_CONFIG_REJECTED
[CELL][ERROR] activate NR_CELL_0 failed: RAT_NOT_READY
```

根因位于 RAT，不应写成“CELL 激活失败”。

## 3. CELL 激活超时

如果前置配置均成功：

```text
[RAT][INFO] state=READY
[CELL][INFO] create NR_CELL_0 success
[CELL][INFO] activate NR_CELL_0
[CELL][ERROR] wait ACTIVE timeout
```

下一步应检查：

- 底层 radio/resource 状态；
- 同时间段 module error；
- sync/clock 状态；
- 激活请求是否收到明确拒绝码。

## 4. 多错误日志处理

CELL 激活失败后常会出现：

```text
[CASE][ERROR] setup failed
[CASE][ERROR] testcase aborted
[CLEANUP][WARN] cell already inactive
```

cleanup 错误通常不是原始故障。
