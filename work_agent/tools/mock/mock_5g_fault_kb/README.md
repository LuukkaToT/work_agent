# 5G 基站自动化测试故障知识库（Synthetic Mock）

> 用途：本数据集用于本地开发、RAG 检索、ReAct/Agent 故障诊断流程联调。
> 所有日志、错误码、组件名和故障案例均为模拟数据，不代表任何真实厂商、产品或内部实现。

## 1. 模拟执行链路

本数据集假设一条自动化用例大致经过：

AW 脚本解析
→ 测试环境初始化
→ COMM 公共通信/基础依赖加载
→ RAT 无线制式配置加载
→ BBH/BBL/MARP 小区配置、传输订阅与激活
→ UE 接入（RACH / RRC）
→ 业务执行
→ 结果判定

其中 COMM、RAT、BBH、BBL、MARP、COMPARE 都是本项目的 synthetic 逻辑组件，
不对应某家厂商的固定模块名。

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
2. 用 `list_log_files` 确认六个分组件日志。
3. 先看合并时间线，再按组件搜索首个异常和状态迁移。
4. 检索知识库，获得候选原因与“下一步证据”。
5. 遇到 `errorcode=` 可查错误码目录，但错误码只做路由提示。
6. 对无 ERROR 场景检查 DEBUG 握手是否闭环，例如订阅请求后是否收到 ACK。
7. 排除级联错误，形成根因 + 证据链。
8. 证据不足时输出候选根因，而不是强行下结论。

## 4. 文件结构

- `kb/`：用于 RAG 的 Markdown 知识文档。
- `logs/`：模拟真实流水线/基站自动化测试日志。
- `logs/benchmark/<scenario>/{comm,rat,bbh,bbl,marp,compare}.log`：20 组、
  120 个分层日志文件，约 13.2 万行。
- `dataset/benchmark_cases.json`：20 组场景元数据、期望根因和 golden 证据。
- `dataset/error_codes.json`：19 个项目自造错误码及排查建议。
- `dataset/cases.jsonl`：案例元数据。
- `dataset/expected_diagnosis.jsonl`：标准诊断结果，可用于离线评估。
- `dataset/retrieval_queries.jsonl`：可用于测试 RAG 召回的查询。

默认 `config/eval_cases.json` 已指向这 20 组数据。运行：

```powershell
python -m work_agent.cli eval-diagnose --strategy managed --pause 1
python -m work_agent.cli eval-diagnose --case b08_rx_subscription_debug_stall --no-store
```

重新生成静态日志与 golden set：

```powershell
python scripts/generate_mock_benchmark.py
```

> 重要：所有错误码和日志原文均为本仓库原创 synthetic 数据，不是华为、
> 爱立信、诺基亚、中兴或其他厂商的真实/私有日志。

## 5. 建议切块

Markdown 建议按二级/三级标题切块，每块保留：

- fault_type
- stage
- symptom
- evidence_patterns
- possible_causes
- next_checks

不要简单固定每 500 字切一次，否则“症状”和“下一步检查”容易被拆开。
