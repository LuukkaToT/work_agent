# RAT 加载失败

> RAT 在本模拟框架中表示 NR/LTE 等无线制式相关配置加载阶段。

## 1. 参数校验失败

### 常见日志

- RAT_CONFIG_REJECTED
- invalid parameter
- unsupported bandwidth
- invalid numerology
- frequency out of range

### 示例

```text
[RAT][ERROR] NR profile validation failed:
band=n78 bandwidth=120MHz allowed=[20,40,50,60,80,100]
```

### 诊断

此类错误通常属于配置问题。
若后续 CELL 报 `RAT_NOT_READY`，CELL 错误为级联错误。

## 2. 前置 COMM 未准备好

```text
[COMM][ERROR] connect failed
[RAT][WARN] wait COMM ready attempt=1/3
[RAT][ERROR] RAT_LOAD_TIMEOUT dependency=COMM
```

此时根因应归 COMM，而不是 RAT。

## 3. RAT 模块初始化超时

若 COMM 正常，配置校验正常，但 RAT 状态长期停留在 INIT：

```text
[RAT][INFO] config validation passed
[RAT][INFO] state INIT -> STARTING
[RAT][ERROR] wait state READY timeout after 30s
```

可能原因：

- RAT 进程异常。
- 底层资源未就绪。
- 软件版本缺陷。
- 某个内部模块启动失败。

下一步应继续搜索同一时间窗口中的进程 crash、resource unavailable、module init failed 等信息。
