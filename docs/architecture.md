# 测试专属 Agent 架构设计（讨论稿 v0.5）

命令行 Agent，给测试人员用。编排框架用 LangGraph。
公司真实 tool 尚未接入，全部 mock，接口契约按真实系统设计，后续替换实现即可商用。

MVP 范围：**到「创建并启动流水线 + 可查询进度」为止**。最终结果用户去流水线前端看；归因、环境自动修复、用例自动生成放 Phase 2。

## 一、核心设计原则

1. **主干是确定性状态机。** 流程阶段固定、每步有明确产物，用 `StateGraph` 显式编排。自由推理只发生在单个节点内部。可审计、可复现是商用前提。

2. **一个模型 + 多套 prompt = 多个角色。** 不为每个「agent」起独立模型。测试分析 agent 本质是「主模型 + 加载了测试分析 skill 的 system prompt」。

3. **所有公司系统调用走 Protocol 抽象。** 图只依赖 Protocol，接真实 tool 时不改图。

4. **触发执行这类写操作必须人工确认。**

## 二、什么时候该用子 agent（重要判定标准）

「需要调 tool」不是子 agent 的理由。**门槛是需不需要自主决策。**

满足下面任意两条，才升级为 Role（LLM 驱动的子 agent）：

1. 下一步做什么取决于上一步结果，且分支无法穷举
2. 需要在多个 tool 之间自主选择、可能反复调用
3. 输出是开放文本，需要迭代打磨

对照本项目：

| 能力 | 判定 | 结论 |
|------|------|------|
| 执行用例 | 步骤固定：抽参数 → 确认 → 调 tool → 拿 run_id | **确定性 Flow**，LLM 只做一次参数抽取 |
| 查执行结果 | 步骤固定：定位 run → 调 tool → 呈现 | **确定性 Flow**，LLM 只做指代消解 |
| 测试分析 | 开放文本 + 需要重写迭代 | **Role**（LLM 驱动） |
| 结果归因（Phase 2） | 要读日志、按线索决定查什么 | **Role + tools** |

所以术语统一为三层：

| 类型 | 含义 | 实例 |
|------|------|------|
| Tool | 确定性外部调用 | 拉用例、执行用例、查结果、查日志 |
| Flow | 确定性子图，步骤固定，LLM 仅做结构化抽取 | 执行流、查询流 |
| Role | 主模型 + skill + 输出契约，需要自主推理 | 测试分析、结果归因 |

口诀：**能写成函数的是 tool，步骤能画死的是 flow，需要边想边试的才是 role。**

## 三、Skill 机制：markdown 即角色包

```text
skills/
  test_analysis/
    SKILL.md          # 角色定义 + 分析方法论 + 步骤要求
    template.md       # 输出结构模板
    references/       # 规格与业务资料 markdown（先模拟公司资料）
      256T_downlink.md
```

`load_skill("test_analysis")` 把 `SKILL.md` + `template.md` + `references/*.md` 全量拼进 system prompt。

**现阶段不做 RAG**：资料量小，全量注入准确率更高、无检索误差。`SkillLoader` 预留 `select_references(query)` 钩子，将来资料变多换检索实现，调用方不改。

产物落盘保证可追溯：

```text
workspace/
  runs/<task_id>/
    task.json           # 任务元数据与参数
    test_analysis.md    # 测试分析产物
    cases.json          # 本次用例集
    run_result.json     # 执行结果原始数据
    report.md           # 最终报告
  index.db              # 运行台账（跨会话查询用）
```

## 四、参数解析：物理环境与优先级链

执行 tool 现阶段入参是 **物理组网 IP**（如 `7.223.50.60`）。逻辑组网（如 `3BBL_86_1BBL86` / 「85+86 环境」）后续靠型号映射 markdown 解析，当前识别到会 interrupt 提示改传物理 IP。

一次用户输入可拆成 **多条执行计划**：不同环境各一条流水线（两次 `init_pipline`）；同一环境多个用例合并为一条（批量 `case_names`）。

参数来源优先级：

```mermaid
flowchart LR
  A["1 用户本次显式输入"] --> B["2 当前会话上下文"]
  B --> C["3 用户个人配置默认值"]
  C --> D["4 interrupt 反问用户"]
```

| 参数 | 缺失时行为 |
|------|-----------|
| `case_names` | 缺失则 interrupt 询问；agent **不预校验**用例名，流水线自己验证 |
| `version` | 枚举 `27B/27A/26B/26A`；可由个人配置默认值静默填充，确认页展示 |
| `env` | 物理 IP；**不允许静默填充**；逻辑组网写法视为缺失并提示 |

个人配置放 `config/profile.yaml`：

```yaml
default_version: "27B"
frequent_topologies: ["topo_a", "topo_b"]
poll_interval_seconds: 30
poll_max_attempts: 40
create_retry_attempts: 1
```

环境必须问，是因为跑错环境代价高。`poll_*` 配置保留但执行链路已不再轮询；`create_retry_attempts` 控制 init/check 超时后的同 ID 重试次数。

## 五、主流程

```mermaid
flowchart TD
  Start(["用户输入 messages"]) --> Intake["intake 提取本轮 + 归零任务级"]
  Intake --> Router["router 意图识别 带对话历史"]
  Router -->|"analysis"| Analysis["Role test_analysis"]
  Router -->|"execute"| ExecFlow["子图 exec_flow"]
  Router -->|"query"| QueryRun["Flow query_run"]
  Router -->|"chat"| QuickAnswer["quick_answer"]

  Analysis --> Respond
  QueryRun --> Respond
  QuickAnswer --> Respond
  ExecFlow --> Respond

  Respond["respond 说成人话 + 追加 AIMessage"] --> Memory["memory 滚动摘要"]
  Memory --> Finish(["结束"])
```

执行子图 `exec_flow` 内部：

```mermaid
flowchart TD
  Params["exec_params 抽执行计划列表"] --> AskMissing["interrupt 逐计划补缺参"]
  AskMissing --> Confirm{"HITL 确认 N 条流水线"}
  Confirm -->|"cancel"| EndNode(["END"])
  Confirm -->|"proceed"| Create["create_pipelines write-ahead + 对账"]
  Create --> EndNode
```

`create_pipelines` 超时对账（`run_id` 是幂等键，超时不换新 ID）：

```mermaid
flowchart TD
  W["台账 write-ahead: status=creating"] --> I["init_pipline"]
  I -->|"成功"| C["check_pipline 幂等"]
  I -->|"超时/异常"| Q["query_result 对账"]
  Q -->|"查得到"| C
  Q -->|"查不到"| R["同 run_id 重试"]
  R -->|"成功"| C
  R -->|"重试用尽"| F["status=failed"]
  C --> OK["status=running"]
```

子图用独立 schema：`ExecFlowInput`（含 `dialogue_summary`）/ `ExecFlowOutput` / 私有字段（`exec_decision`）。`audit` 只出不进。不再轮询、不收集结果、不写执行报告——提交后用户去流水线前端看，问进度走 `query_run`。

### respond + memory

分支节点产出的 `summary` 是给报告、台账和程序看的结构化数据。`respond` 把本轮事实组织成中文回答，写入 `state.reply`，并追加 `AIMessage`。随后 `memory`：消息超过 12 条时，把窗口外旧对话压进会话级 `dialogue_summary`，用 `RemoveMessage` 裁到最近 8 条；阈值以下直通。router / exec_params 注入的是「历史摘要 + 最近对话」。
## 六、运行台账：支撑「前面那次执行怎么样了」

checkpointer 只按 `thread_id` 存图状态。用户换会话再问「上次执行怎么样了」时，需要能跨会话查到 run，所以额外建一张台账表 `workspace/index.db`：

| 字段 | 说明 |
|------|------|
| `run_id` | agent 生成并传入 `init_pipline` 的 id |
| `task_id` | 本地任务 id（同一任务可对应多条流水线） |
| `case_names` / `version` / `env` | 执行参数（env=物理 IP） |
| `status` | 最后已知状态 |
| `created_at` / `updated_at` | 时间戳 |

`query_run` 这个 Flow 的指代消解规则，按顺序尝试：

1. 用户明确给了 run_id 或用例名，直接匹配台账
2. 说「上次 / 前面那次 / 那几条」，取最近一条所属 task 的全部流水线，逐条 `query_result`
3. 台账为空或跨 task 有歧义，interrupt 列出候选让用户选

这就是为什么查询类**不需要**子 agent：规则能穷举，用不着让模型自由探索。

## 七、Tool 契约

```python
class CaseProvider(Protocol):
    def list_cases(self, query: str | None = None) -> list[CaseInfo]: ...
    def fetch_cases(self, names: list[str]) -> list[CaseInfo]: ...

class PipelineTool(Protocol):
    def init_pipline(self, run_id: str, case_names: list[str], version: str, env: str) -> PipelineHandle: ...
    def check_pipline(self, run_id: str) -> bool: ...
    def query_result(self, run_id: str) -> PipelineResult: ...
```

对齐公司真实函数：`init_pipline` 创建、`check_pipline` 启动、查询函数拿执行数据。单个与批量统一成用例名列表。`run_id` 由 agent 生成后传入。

Mock 行为可配置四场景：全通过、版本失败、用例报错、环境不可用。`query_result` 用 tick 模拟分钟级执行进度。

## 八、状态设计

字段分两层：

| 层级 | 字段 | 生命周期 |
|------|------|----------|
| 会话级 | `messages`（`add_messages`）、`dialogue_summary` | 跨轮累积，intake 不重置 |
| 任务级 | 其余字段 | 每轮由 `intake` 显式归零 |

```python
class TestFlowState(TypedDict):
    # 会话级
    messages: Annotated[list[AnyMessage], add_messages]
    dialogue_summary: str          # 滚动摘要；memory 节点维护

    # 任务级：路由与产出
    task_id: str
    user_input: str                # intake 从 messages[-1] 提取
    intent: str                    # analysis | execute | query | chat（单一事实来源）
    requirement: str
    analysis_path: str
    exec_params: dict              # {plans: [{case_names, version, env, ...}, ...]}
    pipelines: list[dict]          # [{run_id, case_names, version, env, status, error}, ...]
    results: list[dict]            # query_run 聚合的用例结果
    summary: dict                  # 只放汇总量，见下
    reply: str
    audit: Annotated[list[dict], append_audit]
```

**单一事实来源约定：**

- 路由读 `intent` / 子图内 `exec_decision`，不读 `summary`
- 引用性字段（`pipelines` / `analysis_path` / `exec_params`）只在顶层
- `summary` 只放汇总量：`status` / `message` / `answer` / `total` / `passed` / `failed_count` / `failed` / `created` / `failed_pipelines`
- 不写 `summary["branch"]`（与 `intent` 永远相等，属冗余）

执行私有字段在子图 `ExecFlowState`：仅 `exec_decision`。

调用方只传 `{"messages": [HumanMessage(...)]}`；任务级重置由 intake 负责，不再维护外部 `empty_state` 清单。

## 九、实施路线：小模块拆分

每个模块几十到两百行，配一个 LangGraph 知识点，做完就能跑。

| 模块 | 内容 | 学到的 LangGraph 知识 | 规模 |
|------|------|----------------------|------|
| M1 | `graph/state.py` + 最小串行图（两个纯 Python 节点） | State / Node / Edge / compile / invoke | 约 80 行 |
| M2 | `core/config.py` 配置与个人默认值 | 无（工程基建） | 约 70 行 |
| M3 | `core/llm.py` Gemini 封装 + 连通性验证 | LLM 接入与结构化输出 | 约 60 行 |
| M4 | `router` 意图识别节点 | `add_conditional_edges` 条件路由 | 约 90 行 |
| M5 | `tools/protocols.py` 契约与数据模型 | 无（契约设计） | 约 90 行 |
| M6 | `tools/mock/` 可配置四场景 mock | 无（可测性） | 约 130 行 |
| M7 | `exec_params` + `create_pipelines` | 多计划抽取与批量 init/check | 约 150 行 |
| M8 | （已移除轮询） | — | — |
| M9 | （已移除执行报告落盘；结果看流水线前端） | — | — |
| M10 | SQLite checkpointer + thread_id | 持久化与断点恢复 | 约 80 行 |
| M11 | interrupt 补参数与执行前确认 | `interrupt` / `Command(resume)` | 约 110 行 |
| M12 | `skills/` 目录 + `SkillLoader` | prompt 组装 | 约 90 行 |
| M13 | `test_analysis` Role 节点 | Role 抽象与产物落盘 | 约 110 行 |
| M14 | 运行台账 + `query_run` Flow | 跨会话状态查询 | 约 120 行 |
| M15 | CLI REPL（typer + rich） | 与图的交互层 | 约 130 行 |
| M16 | mock 场景切换 + 端到端测试 | 图的可测试性 | 约 120 行 |

顺序说明：M1 先用纯 Python 节点建立对图的直觉，不引入 LLM 的不确定性；M3 之后才开始接模型。

## 十、Phase 2（本期不做，架构留位）

- 结果自动归因三分类：版本问题 / 用例异常 / 环境异常
- 版本问题自动提单（`DefectTracker` Protocol）
- 环境异常自动换环境重试（`EnvPool` Protocol）
- 用例自动生成（`case_build` Role）

留位方式：Protocol 里定义好签名，`tools/real/` 放空实现抛 `NotImplementedError`，图里不接线。

## 十一、技术选型

**模型接入统一走 OpenAI 兼容协议**，这样公司网关和 Gemini 用同一套代码，切换只改配置。

`core/llm.py` 只暴露一个工厂函数，全项目不允许别处直接构造模型客户端：

```python
def get_chat_model(*, temperature: float | None = None, model: str | None = None) -> BaseChatModel:
    return ChatOpenAI(
        model=model or settings.llm_model,
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        temperature=temperature if temperature is not None else settings.llm_temperature,
        timeout=settings.llm_timeout,
        max_retries=settings.llm_max_retries,
    )
```

学习阶段配置（Gemini 的 OpenAI 兼容端点）：

```ini
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
LLM_API_KEY=${GEMINI_API_KEY}
LLM_MODEL=gemini-2.5-flash
```

切公司环境只改这三行指向内部网关。注意该端点只支持 `/chat/completions`，不支持 `/responses`，`ChatOpenAI` 走的正是前者。M3 会带一个连通性自检命令，顺便拉一次 `/models` 列表确认当前可用模型 ID。

其他选型：

- CLI：`typer` + `rich`，PowerShell 直接可用
- 持久化：`langgraph-checkpoint-sqlite`
- 已装：langgraph 1.2.10、langchain 1.3.14、langchain-openai（无需再装 google 专用包）

## 十二、从 MVP 到商用的加固清单

M1 到 M16 产出的是**端到端跑得通的最小闭环**，约 1500 到 1800 行。商用级的成本不在主干流程，而在下面这些边界与可靠性工作，这部分才是大头（含测试约 6000 到 10000 行）。

| 加固项 | 具体内容 | 阶段 |
|--------|----------|------|
| 错误处理与重试 | tool 调用失败、网络超时、LLM 限流的分级重试与降级 | 闭环跑通后立刻做 |
| 幂等与防重 | 同一任务重复触发执行的拦截，run_id 去重 | 同上 |
| 参数校验 | 用例名/版本/组网的合法性前置校验，避免无效执行 | 同上 |
| 结构化日志与审计 | 谁在何时触发了什么执行、用了什么参数，可回溯 | 同上 |
| 结果解析健壮性 | 日志格式变化、部分成功、超时未结束等情况 | 接真实 tool 时 |
| 并发执行 | 多组网或多版本并行跑，结果聚合 | 需求出现时 |
| 配置分层 | 默认值 / 项目配置 / 用户配置 / 命令行覆盖 | 逐步 |
| 权限与密钥 | 谁能触发执行、key 管理、日志脱敏 | 上线前必须 |
| prompt 回归评测 | 测试分析质量的固定评测集，改 prompt 不退化 | 接真实资料后 |
| 可观测性 | 每步耗时、token 消耗、失败率 | 逐步 |
| 测试矩阵 | mock 四场景 × 三入口意图的端到端用例 | 与功能同步 |

**为什么先窄后深**：过早抽象是最大的浪费。只有真实跑过一遍流程，才知道哪些边界情况真的会发生。所以先把最小闭环打通，再在调测中逐层加固。

代码量本身不是质量指标。这套架构真正的价值是边界清晰：接公司真实 tool 时只需要写 `tools/real/` 下的实现，图、Role、CLI 一行都不用改。
