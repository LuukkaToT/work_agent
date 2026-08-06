# 测试分析模块代码阅读指南

这份指南用于快速理解新增的 5G 基带测试分析模块、接入真实资料库、定位
模型兼容问题，以及准备面试。

## 一、先明确当前能力边界

### 1. 需求输入

当前测试分析的需求入口是纯文本：

```python
run_turn(text: str, ...)
```

CLI 也是把用户输入包装成 `HumanMessage(content=text)`。因此：

- 直接输入一段需求文本：可以处理。
- 输入一个 `.pptx` 或 `.docx` 文件路径：只会把路径当普通文字，不能解析文件。
- 把 PPT/Word 上传给当前 CLI：没有附件传输接口，不能处理。
- 手工把 PPT/Word 转成文字后粘贴：可以作为文本需求处理。

### 2. 知识库文件

当前知识库扫描支持：

```text
.md / .markdown / .txt
```

当前不支持：

```text
.pptx / .docx / .pdf / 图片 OCR
```

也就是说，PPT/Word 既不能作为需求附件直接解析，也不会作为知识库文档被
索引。对应限制在：

```python
work_agent/analysis/corpus.py

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
```

### 3. 后续增加 PPT/Word 的最小改法

不要把 PPT/Word 解析塞进 LLM Tool。更合理的扩展是：

```text
runtime 接收 AttachmentRef
  ↓
resolve_sources
  ↓
ParserRegistry 按后缀选择解析器
  ├── MarkdownParser
  ├── PptxParser
  └── DocxParser
  ↓
统一输出 normalized markdown + chunks
  ↓
继续复用现有测试分析子图
```

建议增加：

```text
work_agent/analysis/parsers/
├── base.py
├── markdown.py
├── pptx.py
└── docx.py
```

依赖可以使用：

```text
python-pptx
python-docx
```

第一版 PPTX 解析标题、文本框、表格、页码和页面顺序；DOCX 解析标题层级、
段落和表格。图片、SmartArt、流程图和 OCR 先记录 warning，不要假装已经
解析。

---

## 二、30 秒建立整体模型

把整个功能记成一句话：

> 用户需求先结构化，再按信道拆任务；每个任务通过受限 Tool 检索基础测试点
> 和信道资料，生成结构化场景；代码检查覆盖缺口，最多补一次，最后确定性
> 渲染 Markdown。

主流程：

```text
main_graph
  ↓
test_analysis 子图
  ↓
需求结构化
  ↓
按信道规划 DomainTask
  ↓
逐 DomainTask 运行 DomainResearch 子图
  ↓
ToolNode 检索资料
  ↓
证据充分性判断
  ↓
结构化场景生成
  ↓
代码覆盖检查
  ↓
Gap Repair，最多一次
  ↓
Markdown 报告
```

需要牢牢记住两个子图：

1. `TestAnalysisGraph`：控制整份测试分析的生命周期。
2. `DomainResearchGraph`：控制单个信道领域的 Tool 检索与场景生成。

---

## 三、目录地图

### 1. 主图接入

```text
work_agent/graph/main_graph.py
work_agent/graph/subgraphs/analysis_flow.py
```

`main_graph.py` 负责把 `analysis` 意图路由到测试分析子图。

`analysis_flow.py` 只负责节点和边的编排：

```text
initialize_analysis
→ extract_requirement
→ plan_domains
→ analyze_domains
→ review_coverage
→ repair_gaps / render_report
```

### 2. 领域数据模型

```text
work_agent/analysis/schemas.py
```

重点模型：

| 模型 | 含义 |
|---|---|
| `RequirementFact` | 结构化需求事实 |
| `DomainTask` | 一个信道/领域的分析任务 |
| `EvidenceHit` | 一段可引用资料 |
| `EvidenceAssessment` | 资料是否充分、是否需要重检 |
| `TestScenario` | 一条结构化测试场景 |
| `DomainAnalysisResult` | 一个领域的最终结果 |
| `CoverageGap` | 代码发现的覆盖缺口 |
| `AnalysisOutput` | 子图对主图的统一输出 |

### 3. 知识库

```text
work_agent/analysis/corpus.py
```

它负责：

- 扫描资料目录。
- 从文件夹名称识别信道。
- 按基础资料和信道资料分区。
- Markdown/TXT 分块。
- 关键词打分。
- 只读取当前检索范围，不一次性读取全部信道资料。

### 4. 检索 Tool

```text
work_agent/analysis/tools.py
```

只有两个模型 Tool：

```text
search_basic_test_points
search_channel_knowledge
```

文件扫描、文件写入、覆盖检查都不是 Tool。

### 5. 单领域 Tool 子图

```text
work_agent/analysis/research.py
```

这里是最值得仔细阅读的文件，包含：

- `DomainResearchState`
- LLM `bind_tools`
- `ToolNode`
- `tools_condition`
- Tool 调用次数限制
- 证据去重
- 资料充分性判断
- 定向补检
- 结构化场景生成

### 6. 主分析节点

```text
work_agent/analysis/nodes.py
```

这里把需求、规划、多个领域子图、Gap Repair 和报告串起来。

### 7. 代码硬约束

```text
work_agent/analysis/coverage.py
```

它检查：

- normal、boundary、abnormal、reconfiguration、recovery 是否覆盖。
- 是否有前置条件。
- 是否有步骤。
- 是否有预期结果。
- 是否有观察点。
- 是否有资料依据。

### 8. 报告与产物

```text
work_agent/analysis/report.py
work_agent/analysis/artifacts.py
```

`report.py` 从结构化对象渲染 Markdown，不再调用模型自由写整篇报告。

`artifacts.py` 把所有文件限制在：

```text
workspace/runs/<task_id>/analysis/
```

### 9. Prompt 和 Mock 资料

```text
skills/test_analysis/prompts/
skills/test_analysis/references/
```

Prompt 按阶段拆分，不使用一个超长 Prompt。

Mock 资料模拟真实目录：

```text
references/
├── basic_test_points/
└── channels/
    ├── PUCCH/
    ├── PUSCH/
    ├── PRACH/
    ├── PDCCH/
    └── PDSCH/
```

### 10. 测试

```text
tests/test_analysis_corpus.py
tests/test_analysis_tools.py
tests/test_analysis_coverage.py
tests/test_analysis_report.py
tests/test_analysis_flow.py
```

---

## 四、推荐阅读顺序

不要从 `nodes.py` 第一行开始逐行硬啃。推荐按下面顺序读。

### 第一轮：20 分钟看懂骨架

1. `graph/subgraphs/analysis_flow.py`
2. `analysis/schemas.py`
3. `analysis_flow.py` 再看一遍

这一轮只回答三个问题：

- 输入是什么？
- 中间经过哪些阶段？
- 最终输出是什么？

### 第二轮：30 分钟看懂 Tool 检索

1. `analysis/tools.py`
2. `analysis/corpus.py`
3. `analysis/research.py`
4. `tests/test_analysis_tools.py`

这一轮回答：

- 模型能调用哪些 Tool？
- Tool 为什么不能访问任意路径？
- 信道白名单如何注入？
- Tool 循环如何停止？
- 检索失败时怎么降级？

### 第三轮：30 分钟看懂可靠性

1. `analysis/nodes.py`
2. `analysis/coverage.py`
3. `analysis/report.py`
4. `tests/test_analysis_coverage.py`
5. `tests/test_analysis_flow.py`

这一轮回答：

- 为什么同一信道任务会合并？
- 一个领域失败会不会让整份报告失败？
- 模型遗漏场景时谁发现？
- Gap Repair 为什么只执行一次？
- 为什么最终 Markdown 不由模型直接写？

### 第四轮：拿一次真实产物倒查

运行：

```powershell
.\.venv\Scripts\python.exe -m work_agent.cli ask "支持业务态修改 PUCCH 资源，请生成测试分析"
```

然后按这个顺序查看：

```text
requirement.json
→ domain_plan.json
→ domains/<task_id>.json
→ coverage.json
→ manifest.json
→ test_analysis.md
```

这是最快的理解方法。看到某个结果不合理时，再反向定位产生它的节点和 Prompt。

---

## 五、沿着一条需求走完整流程

假设用户输入：

```text
支持业务态修改 PUCCH 资源集合和资源索引，要求切换过程中
HARQ-ACK 不中断，并验证配置失败后的恢复。
```

### 1. 主图路由

`router` 输出：

```json
{
  "intent": "analysis"
}
```

主图进入 `test_analysis` 子图。

### 2. 需求结构化

`extract_requirement` 调用 `RequirementFact` Structured Output，大致得到：

```json
{
  "title": "PUCCH 资源重配置",
  "directions": ["UL"],
  "channels": ["PUCCH"],
  "features": ["HARQ-ACK", "资源重配置"],
  "missing_information": ["产品版本", "PUCCH格式"]
}
```

模型输出后，代码还会：

- 标准化信道名称。
- 保留原始需求。
- 没有检索词时补默认检索词。
- 写入 `requirement.json`。

### 3. 规划领域任务

`plan_domains` 生成 `AnalysisPlan`。

即使模型按五种场景生成五个 PUCCH 任务，代码也会按信道集合合并成一个：

```json
{
  "domain": "PUCCH",
  "channels": ["PUCCH"],
  "required_scenario_types": [
    "normal",
    "boundary",
    "abnormal",
    "reconfiguration",
    "recovery"
  ]
}
```

这样可以避免同一资料目录被重复检索五次。

### 4. 建立允许信道范围

代码根据 `CHANNEL_DEPENDENCIES` 扩展白名单：

```text
PUCCH → PUSCH / SRS / RRC / MAC
```

只有真实资料库中实际存在的目录才会进入白名单。

### 5. Tool 检索

研究模型通常先调用：

```text
search_basic_test_points
```

获得正常、异常、边界、重配置和恢复的通用测试方法。

然后调用：

```text
search_channel_knowledge(channel="PUCCH")
```

获得 PUCCH 专属资料。

如果模型请求不在白名单中的信道，Tool 返回：

```json
{
  "error": "channel_not_allowed"
}
```

### 6. 资料充分性

`assess_evidence` 输出：

```text
sufficient
retry_search
expand_channels
corpus_gap
```

如果需要补检，模型返回具体查询词；代码同时检查：

- 是否还有 Tool 调用额度。
- 是否还有检索轮数。
- followup query 是否为空。

默认限制：

```text
最大 Tool 调用：4
最大充分性轮数：2
单次 top-k：5
最终证据上限：16
```

### 7. 场景生成

模型输出 `DomainAnalysisResult`，其中每条场景必须是 `TestScenario`。

代码会过滤不存在的 `evidence_refs`，模型不能引用自己编造的 chunk ID。

### 8. 覆盖检查

代码检查五类场景和五类必填字段。发现缺口后：

```text
按 task_id 分组
→ 构造 repair DomainTask
→ 再运行同一个 DomainResearch 子图
→ 合并去重
→ 再检查一次
```

`repair_count < 1` 防止无限循环。

### 9. 报告

最终由 `render_markdown()` 生成报告。

报告状态：

| 状态 | 条件 |
|---|---|
| `completed` | 有场景、无覆盖缺口、无失败领域、无资料告警 |
| `partial` | 有场景，但有资料缺口、覆盖缺口或部分领域失败 |
| `failed` | 没有任何有效场景 |

---

## 六、模型与代码的职责边界

| 工作 | LLM | 代码 |
|---|---:|---:|
| 理解自然语言需求 | 是 | 兜底提取 |
| 判断影响信道 | 是 | 标准化、白名单校验 |
| 规划领域任务 | 是 | 合并重复任务、限制数量 |
| 选择检索 Tool | 是 | 限制 Tool 集合和次数 |
| 改写检索词 | 是 | 限制轮数、去重证据 |
| 判断语义资料缺口 | 是 | 检查是否允许继续 |
| 生成测试场景 | 是 | Pydantic 校验、引用过滤 |
| 判断固定覆盖维度 | 否 | 是 |
| 防止无限循环 | 否 | 是 |
| 写文件 | 否 | 是 |
| 渲染最终 Markdown | 否 | 是 |

面试时可以概括为：

> 模型负责语义发现，代码负责边界、完整性下限、幂等性和终止条件。

---

## 七、State 为什么分两层

### 1. 主测试分析 State

主分析子图主要保存引用：

```text
requirement_ref
plan_ref
domain_result_refs
coverage_ref
analysis_path
```

完整文档和完整报告不放进父 State。

### 2. DomainResearch 私有 State

单领域研究才保存：

```text
research_messages
tool_call_count
retrieval_round
evidence
assessment
```

`research_messages` 不会输出给主图。

这样做的原因：

- 减少 Checkpoint 体积。
- 避免多个领域的 Tool 消息互相污染。
- 避免恢复时重复追加大量上下文。
- 父图只关心结果，不关心领域研究过程中的对话。

---

## 八、GLM 5.1 接入时重点检查什么

当前项目统一通过：

```text
work_agent/core/llm.py
```

创建 `ChatOpenAI`，切换真实模型通常只需要配置：

```text
LLM_BASE_URL
LLM_API_KEY
LLM_MODEL
```

GLM 5.1 是否完全兼容需要现场验证。重点检查以下能力。

### 1. Chat Completions 兼容

确认真实模型端点支持：

```text
/chat/completions
system / user / assistant / tool 消息
```

如果只支持其他请求协议，在 `core/llm.py` 换模型适配器，业务图不应该修改。

### 2. Structured Output

当前使用：

```python
model.with_structured_output(PydanticModel)
```

需要验证：

- 是否支持 JSON Schema。
- 枚举、数组、可空字段是否正确。
- 返回值是否能被 Pydantic 校验。
- 是否会在 JSON 前后增加解释文字。

如果不兼容，优先只改：

```text
work_agent/analysis/llm.py
```

可降级为：

```text
Prompt 要求 JSON
→ 提取 JSON
→ Pydantic.model_validate
→ 校验失败后重试一次
```

### 3. Tool Calling

当前使用：

```python
get_chat_model().bind_tools(RESEARCH_TOOLS)
```

需要验证模型返回的消息能否被 LangChain 解析为：

```python
AIMessage.tool_calls
```

每个 Tool Call 至少需要：

```text
name
args
id
type
```

如果真实接口字段不同，应该在模型适配层转换，而不是修改检索 Tool 的业务实现。

### 4. ToolMessage 回传

确认工具执行结果可以通过：

```text
role=tool
tool_call_id=<原调用 ID>
```

与模型调用对应。

当前研究子图为了兼容不同模型端点，不会把旧函数调用历史原样回放给下一轮；
它会把已经去重的证据压缩成一个新的 HumanMessage，再进行下一次 Tool 决策。
这也能降低上下文膨胀。

### 5. 并行 Tool Call

确认 GLM 是否可能一次返回多个 Tool Call。`ToolNode` 能执行并行调用，但要检查：

- 调用 ID 是否唯一。
- 参数是否完整。
- 真实模型网关是否保持 Tool Call 顺序。
- 多个 ToolMessage 是否都能正确回传。

### 6. Content 格式

有些 OpenAI 兼容端点返回：

```text
content: string
```

有些返回分段列表。项目现有 `invoke_text()` 已处理两种形式，但 Tool 和
Structured Output 仍需单独烟测。

### 7. 最小兼容烟测

建议在真实环境依次测试：

1. 普通 `invoke()`。
2. `with_structured_output(RequirementFact)`。
3. 单次 `bind_tools()`。
4. `ToolNode` 执行一次基础资料检索。
5. 基础 Tool + 信道 Tool 连续两次。
6. 完整 PUCCH 分析。

不要一开始就拿跨五个信道的大需求测试，否则不容易定位是模型协议问题、
检索问题还是 Prompt 问题。

---

## 九、真实资料库接入检查表

### 1. 设置根目录

```powershell
$env:TEST_ANALYSIS_KNOWLEDGE_ROOT = "D:\real-data\5g-knowledge"
```

### 2. 确认目录能被识别

当前标准信道：

```text
PUCCH / PUSCH / PRACH / PDCCH / PDSCH
SRS / CSI-RS / SSB / PBCH / DMRS / PTRS
RRC / MAC
```

如果目录名类似：

```text
01_PUCCH_上行控制信道
```

也可以识别。

如果真实资料目录使用其他名称，在：

```text
work_agent/analysis/corpus.py
```

补 `CHANNEL_ALIASES`。

### 3. 确认基础测试点目录

下面这些名称会识别为基础资料：

```text
basic
baseline
common
basic_test_points
基础测试点
基础测试点资料库
通用
公共
```

### 4. 先用单信道需求

建议第一条真实需求只涉及 PUCCH 或 PUSCH。

检查：

- `requirement.json` 是否识别正确信道。
- `domain_plan.json` 是否只有少量任务。
- `domains/*.json` 是否同时引用基础资料和信道资料。
- `coverage.json` 是否出现合理缺口。
- `test_analysis.md` 是否把未知内容标为待确认。

### 5. `partial` 不一定是错误

如果报告生成了场景，但资料评估发现产品细节不足，状态应当是：

```text
partial
```

这是预期行为。需要检查的是 `missing_topics` 是否准确，而不是强行要求所有
报告都变成 `completed`。

---

## 十、常见问题怎么定位

### 1. 没有识别到信道

检查：

```text
requirement.json → channels
domain_plan.json → tasks[].channels
```

可能原因：

- 文件夹别名未注册。
- 需求里使用了未登记的业务简称。
- Structured Output 模型没有按 schema 返回。

### 2. Tool 没有调用

检查本轮 `audit` 中的：

```text
research_agent.tool_calls
```

如果为空：

- 检查 GLM Tool Calling 格式。
- 检查模型是否返回 `AIMessage.tool_calls`。
- 检查 Prompt 是否被真实模型网关截断。

即使 Tool 模型调用失败，代码也会执行受限的确定性兜底检索，并把失败写入
warning。

### 3. 只检索到基础资料

检查：

- `DomainTask.channels` 是否为空。
- `allowed_channels` 中是否包含目标信道。
- 真实目录名是否被 `CorpusCatalog` 识别。
- `search_channel_knowledge` 是否返回 `channel_not_allowed`。

### 4. 报告场景有内容但全是 `partial`

查看：

```text
domains/*.json → missing_topics / warnings
```

通常表示知识库只有通用测试点，没有产品级事实，不是图执行失败。

### 5. 场景引用为空

检查模型生成的 `evidence_refs` 是否来自当前证据 chunk ID。代码会主动删除
不存在的引用，然后覆盖检查会产生 evidence gap。

### 6. 调用次数太多

检查：

```text
domain_plan.json
```

同一信道任务正常情况下会被合并。可以通过环境变量进一步限制：

```powershell
$env:TEST_ANALYSIS_MAX_TOOL_CALLS = "3"
$env:TEST_ANALYSIS_MAX_RETRIEVAL_ROUNDS = "1"
$env:TEST_ANALYSIS_TOP_K = "4"
```

---

## 十一、测试怎么读

### `test_analysis_corpus.py`

证明：

- 目录名可以成为信道元数据。
- 检索只读取选中的信道范围。
- `../../PUCCH` 不能伪装成合法信道名。

### `test_analysis_tools.py`

证明：

- `InjectedState` 能把信道白名单注入 Tool。
- 越权信道会被拒绝。
- 新一轮模型决策不会回放旧函数调用，避免不同模型端点的特殊签名问题。

### `test_analysis_coverage.py`

证明：

- 缺少场景类型可以被代码发现。
- 缺少前置、步骤、预期、观察点、证据也能被发现。

### `test_analysis_report.py`

证明：

- 结构化结果能稳定渲染成 Markdown。
- 原始需求、场景、覆盖结果和证据引用不会丢失。

### `test_analysis_flow.py`

证明：

- 整个子图能写出所有结构化产物。
- 同信道的多个模型任务会被代码合并。
- 最终状态和场景数量符合预期。

运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

---

## 十二、面试可能会问什么

### 1. 为什么不能用一个大 Prompt 直接生成测试分析？

回答重点：

- 长上下文容易让模型遗漏早期信息。
- 资料读取、影响分析、场景发散和报告生成混在一起，无法定位错误阶段。
- 无法判断模型遗漏了哪些固定测试维度。
- 结构化中间产物便于恢复、审计和复用。

### 2. 为什么测试分析要做成独立子图？

回答重点：

- 主图只负责意图路由。
- 测试分析有自己的多阶段状态和失败语义。
- Tool 消息和证据不能污染执行流水线分支。
- 子图输入输出契约能限制父 State 膨胀。

### 3. 为什么读取文件和写 Markdown 不做成 Tool？

回答重点：

- 它们是确定性步骤，不需要模型选择。
- 任意文件读写权限风险高。
- 确定性调用更容易测试、恢复和保证幂等。
- 只有检索资料源与检索词需要语义决策，所以只注册检索 Tool。

### 4. 为什么需要基础测试点 Tool 和信道知识 Tool 两个 Tool？

回答重点：

- 基础库回答“应该从哪些测试维度展开”。
- 信道库回答“该信道具体机制和配置是什么”。
- 两类资料查询目标、过滤条件和缺口语义不同。
- 模型可以根据当前证据决定补查哪类资料。

### 5. 如何防止 Tool 无限循环？

回答重点：

- 最大 Tool 调用次数。
- 最大充分性轮数。
- top-k 限制。
- 证据去重和数量上限。
- 没有 followup query 就停止。
- 资料库缺失时输出 `corpus_gap`，不重复相同查询。

### 6. 模型如何判断资料是否充分？

回答重点：

- 模型输出结构化 `EvidenceAssessment`。
- 区分 `sufficient`、`retry_search`、`expand_channels`、`corpus_gap`。
- 模型负责语义判断，代码负责是否允许继续执行。

### 7. 如何降低幻觉？

回答重点：

- 场景必须输出 `evidence_refs`。
- 引用只能来自当前检索返回的 chunk ID。
- 代码过滤无效引用。
- 无依据产品行为进入 assumptions 或 missing topics。
- 最终报告区分 completed、partial、failed。

### 8. 如何判断测试分析是否完整？

回答重点：

- 不让模型自己宣布“完整”。
- 代码维护最低覆盖清单。
- 检查场景类型和必填字段。
- 发现缺口后做定向 Gap Repair。
- 只能证明规定维度被覆盖，不能证明开放世界里绝对没有遗漏。

### 9. 为什么 Gap Repair 最多一次？

回答重点：

- 防止模型在不完整资料上无限自我修正。
- 第二次仍缺通常说明知识库缺资料，而不是需要继续推理。
- 成本和延迟可控。
- 缺口最终显式进入报告。

### 10. 为什么同一信道任务要合并？

回答重点：

- Planner 可能按 normal、boundary 等拆成多个任务。
- 资料库物理结构按信道组织。
- 分开会重复读取相同资料、增加费用。
- 代码按信道集合合并 objectives、queries 和 required types。

### 11. 如何处理跨信道需求？

回答重点：

- Planner 可以生成多个信道任务。
- `CHANNEL_DEPENDENCIES` 提供受控扩展范围。
- 模型只能在白名单内调用其他信道 Tool。
- 最终可增加独立 CrossDomain 节点分析信道间关系。

### 12. 为什么 State 只放引用？

回答重点：

- LangGraph Checkpoint 会持久化 State。
- 大段资料和 Tool 历史会增加保存、恢复和回放成本。
- ArtifactStore 保存完整 JSON/Markdown。
- 父 State 保存 ref、status 和摘要。

### 13. 如何保证重试和恢复不产生重复结果？

回答重点：

- 领域结果按 `task_id` 写固定文件。
- 重跑采用覆盖而不是列表追加。
- 场景按“类型 + 标题”去重。
- 证据按 `chunk_id` 去重。
- Gap Repair 有计数器。

### 14. Structured Output 不兼容怎么办？

回答重点：

- 模型调用统一封装。
- 可替换成 JSON Prompt + Pydantic 校验 + 有限重试。
- 业务节点仍消费相同 Pydantic 模型，不需要整体重写。

### 15. Tool Calling 不兼容怎么办？

回答重点：

- 检查模型返回是否能映射到 `AIMessage.tool_calls`。
- 在模型适配层转换字段。
- 最差可退回“模型输出 RetrievalDecision，代码调用 Retriever”的确定性模式。
- Retriever、场景 schema 和覆盖检查都可以继续复用。

### 16. 当前轻量检索如何升级成 RAG？

回答重点：

- 保持 `ChannelKnowledgeRetriever` 接口。
- 替换内部实现为向量检索或真实知识库 API。
- 仍保留目录/信道/版本元数据硬过滤。
- 向量相似度不能替代版本和适用范围过滤。

### 17. 如何评测这个系统？

回答重点：

- 准备脱敏需求和期望领域集合。
- 统计领域召回、必需场景覆盖率、有效引用率。
- 检查 corpus gap 是否准确。
- 对比一次大 Prompt 和分域流程的遗漏率、调用成本、延迟。
- Prompt 回归评测不能只看文档是否“读起来不错”。

### 18. 当前最大限制是什么？

回答重点：

- 需求只支持文本。
- 知识库只支持 Markdown/TXT。
- 轻量关键词检索不理解复杂语义。
- Mock 资料不能验证真正的领域准确率。
- 覆盖规则是最低完整性，不是绝对完整性证明。

---

## 十三、一分钟项目介绍

可以这样说：

> 我把原来一次大 Prompt 的 5G 基带测试分析重构成了 LangGraph 分阶段子图。
> 系统先把需求结构化，再按受影响信道生成 DomainTask。每个任务内部有一个
> 受限 Tool Calling 子图，模型可以检索基础测试点库和信道专属资料库，但
> Tool 次数、信道白名单、top-k 和检索轮数由代码控制。模型生成的不是最终
> Markdown，而是带 evidence ref 的结构化测试场景。之后代码检查正常、边界、
> 异常、重配置、恢复等维度，缺口最多补一次，最后确定性渲染报告。完整资料
> 和 Tool 历史不进父 State，只保存 artifact ref，便于 Checkpoint、审计和恢复。

---

## 十四、掌握完成的自测清单

如果下面问题都能脱离代码回答，说明已经掌握：

- [ ] 主图为什么只传 `task_id` 和 `user_input`？
- [ ] 两个子图分别负责什么？
- [ ] 哪些能力是 Tool，哪些是普通 Service？
- [ ] `InjectedState` 如何限制信道？
- [ ] Tool 循环有哪些停止条件？
- [ ] `corpus_gap` 和 `retry_search` 有什么区别？
- [ ] 同信道任务为什么要合并？
- [ ] 无效 evidence ref 怎么处理？
- [ ] Coverage Gap 如何发现和修复？
- [ ] 为什么最终报告不用 LLM 直接写？
- [ ] 为什么父 State 不保存完整 Tool 历史？
- [ ] GLM Structured Output 不兼容时改哪里？
- [ ] GLM Tool Calling 不兼容时改哪里？
- [ ] PPT/Word 支持应该加在哪一层？

完成后，再用一条真实 PUCCH 需求实际查看六个分析产物，比继续读代码更有效。
