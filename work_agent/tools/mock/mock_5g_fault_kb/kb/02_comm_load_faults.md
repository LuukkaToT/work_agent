# COMM 加载失败

> COMM 在本模拟数据中表示测试环境的公共通信/基础依赖层。

## 故障语义

COMM 负责建立后续 RAT/CELL 配置所依赖的基础通信或控制通道。
因此 COMM 未 READY 时，RAT/CELL 的超时通常属于级联错误。

## 1. 连接失败

### 特征

- connection refused
- connect timeout
- endpoint unreachable
- handshake failed

### 证据示例

```text
[COMM][ERROR] connect 10.10.7.21:19090 failed: connection refused
[COMM][ERROR] state INIT -> FAILED
[RAT][ERROR] dependency COMM not ready
```

### 根因候选

- 依赖服务未启动。
- IP/端口配置错误。
- 网络不可达。
- 服务启动慢于测试流程。

### 下一步检查

1. 检查 endpoint 配置。
2. 检查远端服务状态。
3. 搜索 COMM 首次 connect 的日志。
4. 区分 connection refused 与 timeout：
   - refused 更偏向端口没有监听；
   - timeout 更偏向网络路径、防火墙或服务无响应。

## 2. 握手/版本不兼容

关键词：

- protocol version mismatch
- unsupported capability
- handshake rejected

若 TCP 已连接成功但随后 handshake rejected，应优先排查版本和能力协商，不应归为普通网络异常。
