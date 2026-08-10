# AW 脚本与用例配置故障

> AW 为本模拟测试框架中的脚本层名称，不代表某个公开标准术语。

## 1. 必填参数缺失

### 常见现象

- variable not defined
- required field missing
- unresolved placeholder
- configuration value is empty
- generated config incomplete

### 典型证据

```text
[AW][ERROR] unresolved placeholder: ${CELL_BAND}
[AW][ERROR] required parameter "cell.band" is empty
```

### 级联表现

AW 层未能生成完整配置后，RAT/CELL 可能继续报：

- RAT_CONFIG_REJECTED
- CELL_LOAD_FAILED
- required object not found

这些后续错误不应单独作为根因。

### 下一步检查

- 检查 testcase 参数表。
- 检查变量替换结果。
- 检查环境 profile 是否提供默认值。
- 检查生成后的 RAT/CELL 配置文件，而不只看源模板。

## 2. 参数类型错误

例如脚本传入字符串 `"100MHz"`，而下游要求整数 `100`。

关键词：

- type mismatch
- cannot cast
- schema validation failed
- expected integer

## 3. 脚本步骤/语法错误

关键词：

- unknown action
- syntax error
- step not registered
- handler not found

若脚本解析阶段已失败，后续基础设施日志一般没有诊断价值。
