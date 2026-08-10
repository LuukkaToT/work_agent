# 故障诊断总则

## 核心原则：区分现象、直接原因和根因

测试日志经常存在级联失败。

例如：

```text
CELL activation failed
RAT is not ready
RAT configuration rejected
invalid parameter: nr_bandwidth=120
```

- 现象：CELL activation failed
- 直接原因：RAT is not ready
- 根因：RAT 参数不合法

## 首错优先，但不能机械使用

通常优先分析时间线上最早出现的高价值异常：

- ERROR
- FATAL
- rejected
- invalid
- timeout
- connection refused
- dependency not ready

但一些 WARN 属于预期重试，应结合后续是否恢复判断。

## 级联故障特征

典型表达：

- dependency not ready
- skip xxx because previous stage failed
- aborted due to upstream failure
- xxx unavailable
- timeout waiting for xxx ready

这些通常是结果，不是根因。

## 推荐证据链

一个高质量诊断至少应提供两个相互支持的证据：

1. 第一处异常；
2. 后续组件对该异常的引用或状态变化。

## 证据不足

若只能看到：

```text
CELL_LOAD_TIMEOUT
```

但没有 RAT/COMM/AW 前序日志，应输出：

`root_cause=unknown` 或候选原因列表，并建议继续拉取对应时间窗口日志。
