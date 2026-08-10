# 版本与依赖不兼容

## 典型场景

测试脚本、配置 schema、控制服务和 RAT 组件可能来自不同版本。

### 特征日志

```text
[COMM][INFO] handshake transport success
[COMM][ERROR] protocol version mismatch client=4.8 server=4.6
```

或：

```text
[RAT][ERROR] config schema v12 is not supported, supported=[v10,v11]
```

## 判断

如果：

1. 网络连接成功；
2. endpoint 正确；
3. 在 capability/schema negotiation 阶段失败；

则优先归因于版本/协议不兼容。

## 级联错误

后续可能出现：

- COMM_NOT_READY
- RAT_LOAD_TIMEOUT
- CELL_LOAD_FAILED

这些都不是最底层原因。

## 下一步检查

- 对比组件版本。
- 对比配置 schema 版本。
- 检查升级/回退后是否一致。
- 查看 capability negotiation 结果。
