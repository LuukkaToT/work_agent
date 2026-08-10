# 5G 基站自动化测试故障知识库（Synthetic Mock）

> 用途：本数据集用于本地开发、RAG 检索、ReAct/Agent 故障诊断流程联调。
> 所有日志、错误码、组件名和故障案例均为模拟数据，不代表任何真实厂商、产品或内部实现。

## 1. 模拟执行链路

本数据集假设一条自动化用例大致经过：

AW 脚本解析
→ 测试环境初始化
→ COMM 公共通信/基础依赖加载
→ RAT 无线制式配置加载
→ CELL 小区配置与激活
→ UE 接入（RACH / RRC）
→ 业务执行
→ 结果判定

其中 COMM、RAT、AW 是模拟测试框架中的逻辑概念，不对应 3GPP 标准中的固定模块名。

## 2. 诊断原则

不要把最后一条 ERROR 当成根因。

例如：

CELL_LOAD_FAILED
→ RAT_NOT_READY
→ RAT_CONFIG_REJECTED
→ nr_bandwidth=120MHz 与当前 band/profile 不匹配

最终根因应归为 RAT 参数配置错误，而不是“小区加载失败”。

诊断输出建议包含：

- symptom: 顶层失败现象
- stage: 首个发生异常的阶段
- root_cause: 最可能根因
- evidence: 关键日志证据
- cascading_errors: 后续级联错误
- next_action: 推荐排查/修复动作
- confidence: high / medium / low

## 3. 推荐 Agent 流程

1. 获取失败摘要。
2. 按时间定位首个 ERROR/WARN。
3. 检索知识库，获得候选原因与“下一步证据”。
4. 调用日志工具搜索对应关键字或时间窗口。
5. 排除级联错误。
6. 形成根因 + 证据链。
7. 证据不足时输出候选根因，而不是强行下结论。

## 4. 文件结构

- `kb/`：用于 RAG 的 Markdown 知识文档。
- `logs/`：模拟真实流水线/基站自动化测试日志。
- `dataset/cases.jsonl`：案例元数据。
- `dataset/expected_diagnosis.jsonl`：标准诊断结果，可用于离线评估。
- `dataset/retrieval_queries.jsonl`：可用于测试 RAG 召回的查询。

## 5. 建议切块

Markdown 建议按二级/三级标题切块，每块保留：

- fault_type
- stage
- symptom
- evidence_patterns
- possible_causes
- next_checks

不要简单固定每 500 字切一次，否则“症状”和“下一步检查”容易被拆开。
