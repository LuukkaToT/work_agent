# UE 接入阶段：RACH / RRC 故障

> 本文只用于构造接近真实 5G 测试诊断流程的模拟数据。

## 1. 随机接入失败

如果小区已经 ACTIVE，但 UE 无法建立接入，可关注：

- PRACH attempt
- random access response
- preamble
- RA timeout
- contention resolution

### 模拟案例

```text
[UE][INFO] PRACH preamble tx attempt=1
[UE][WARN] no random access response before timer expiry
[UE][INFO] PRACH preamble tx attempt=2
[UE][ERROR] random access failed after max attempts
```

候选原因包括：

- PRACH 配置不一致；
- UE 与小区使用的配置 profile 不匹配；
- 无线/同步条件异常；
- 下行响应未成功接收。

不要在没有射频或协议证据时直接断言具体物理层根因。

## 2. RRC 建立阶段失败

模拟特征：

```text
[RRC][INFO] RRCSetup sent
[RRC][ERROR] RRCSetupComplete not received before timeout
```

诊断应描述为“RRC 建立未完成”，然后继续查：

- UE 是否收到 RRCSetup；
- UE 是否发生重建/重试；
- 是否存在更早的 RACH 或链路异常。

只有顶层 `RRC setup failed` 时，根因通常仍不足以确定。
