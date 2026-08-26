# 上下文管理与离线评测：读代码、讲设计、应付面试

本文只讲两块：诊断链路里的 **ContextManager**，以及用来证明它有没有用的 **离线 A/B eval**。目标不是再抄一遍 API，而是让你能：

1. 顺着文件把代码看懂；
2. 说清楚每个指标在量什么、为什么要量、怎么算；
3. 面试时把「有技术含量」的取舍讲出来；
4. 被问「遇到什么问题、怎么解决」时，能按真实踩坑复述。

配套速查仍在 `docs/interview-guide.md`；架构总览在 `docs/architecture.md` 第十四节。本文是这两块的展开。

---

## 0. 先建立一张图：它解决什么问题

失败归因（`error_analysis`）是全图唯一的 ReAct 节点：模型自己决定调 `get_pipeline_status` / `fetch_logs` / `grep_logs` …… 一步一步取证，最后再做一次结构化抽取，吐出 `fail_kind` / `evidence` / `conclusion`。

改造前有四个具体毛病，不是「上下文太长」这种空话：

| 毛病 | 实际表现 |
|------|----------|
| 历史无限长 | ReAct 每步都把上一轮的 `AIMessage + ToolMessage` 原样塞回去。八步之后模型每次决策都在重读整段对话。 |
| 证据被无脑压扁 | 每条工具结果进上下文前都 `compress_observation(..., max_chars=400)`，不管预算还有没有余量。字符数看起来很低，根因原文可能已经被截没了。 |
| 重复浪费预算 | 模型经常对同一份日志 `fetch_logs` 两次（先 tail=200 再 tail=500，或两次同样的调用）。两份几乎一样的文本同时占着上下文。 |
| 说不清「最终喂给抽取器的是什么」 | 没有 `selected_ids`、没有 `context_chars`、摘要自己烧掉的 token 也不记账。改了策略只能靠感觉。 |

改造后分两层管：

```
ReAct 循环内部                          抽取阶段
─────────────────                      ─────────────────
run_agent_loop                         run_diagnosis
  维护 list[ReActStep]                   把 ToolMessage 变成 ContextItem
  按 step 原子裁剪历史                     ContextManager.render(...)
  不调 LLM、不写盘                         Normalize → Dedup → Rank → Budget
                                          装不下才 Compress + Archive
```

两层故意不混在一起。ReAct 是热路径（每步都走），插一次 LLM 摘要等于每步多一次往返；抽取只发生一次，才值得做去重、打分、超预算才摘要。

节点对外形状不变：`error_analysis(state)` 仍只回写 `summary` + `audit`。真正的内核是 `run_diagnosis(context_strategy="legacy"|"managed")`，所以 eval 可以对同一个 case 跑两种策略，不用起图、不用复制逻辑。

---

## 1. 代码怎么看：文件地图与调用链

建议按这个顺序读，不要从 `context_manager.py` 一头扎进去。

### 1.1 入口：诊断内核

| 文件 | 看什么 |
|------|--------|
| `work_agent/graph/nodes/error_analysis.py` | `run_diagnosis`、`DiagnosisResult`、`error_analysis` 薄壳。**先读这个。** |
| `work_agent/core/config.py` + `config/profile.yaml` | `react_observation_max_chars`（单条观察进 ToolMessage 前的上限）、`react_history_max_chars`（历史 step 的字符预算）。 |
| `work_agent/core/usage.py` | `TokenUsage` / `usage_from_message`。摘要和抽取的 token 从这里归集，eval 才对得上。 |

`run_diagnosis` 的分支只有一处：

- `legacy`：`history_max_chars=0`（不裁历史）；抽取上下文用原来的 `assemble_blocks`，整块按优先级丢。
- `managed`：历史按 step 裁；抽取走 `ContextManager`。

节点本身永远走 `managed`。`legacy` 只为 A/B 基线存在，线上不会走到。

注意：这个 `legacy` **不等于** git 上的旧提交 `36a4364`。旧提交还是 480 行日志、keyed 超限从头部切；当前开关已经带了留尾和加长 mock。相对真正旧分支的粗估（recall 约 0.83→1.0，失败 case 历史约 -16%）写在 [context-trim-eval.md §5](context-trim-eval.md)。

读 `DiagnosisResult` 时盯这几个字段：`context_text`（真正喂给抽取器的原文，eval 的 `evidence_recall` 在它上面算）、`context_chars`、`token_usage`、`selected_context_ids`、`trimmed_steps`。

两套预算不要混：`react_history_max_chars`（默认 20000）管的是 ReAct 每步发给模型的历史；抽取阶段 `ContextManager.render` 的 `limit` 是 `min(12000, react_total_chars_budget // 2)`。历史裁了不等于抽取上下文一定短，反之亦然。

### 1.2 ReAct 热路径

| 文件 | 看什么 |
|------|--------|
| `work_agent/graph/helpers/agent_loop.py` | `ReActStep`、`trim_steps`、`AgentLoopResult`、`run_agent_loop` |
| `tests/test_agent_loop.py` | `assert_tool_pairing`：配对不变量。这是硬约束，不是风格。 |

循环不再维护扁平 `messages`，而是 `prelude`（system + 用户目标，永不裁）+ `list[ReActStep]` + `tail`（触顶时的停止提示）。每次 `invoke` 前用 `trim_steps` 拼出真正发给模型的序列。`messages` 返回的是**未裁剪的完整历史**，给 `extract_tool_trace` / 观察提取用——裁的是「发给模型的那份」，不是事后审计那份。

### 1.3 抽取阶段的三段式

| 文件 | 副作用 | 一句话 |
|------|--------|--------|
| `context_selector.py` | 无 | 纯函数：normalize / dedup / 打分 / 预算选择 |
| `context_compressor.py` | 只调 LLM | 超预算才摘要；失败降级为 `compress_observation` |
| `context_archive.py` | 只写盘 | 原文落到 `workspace/diagnose_archive/<run_id>/` |
| `context_manager.py` | 编排 | 装得下就零 LLM 零 IO |

单测也按这个边界拆：

- `tests/test_context_selector.py`：不需要 mock。
- `tests/test_context_manager.py`：注入 `_FakeModel` / `_BoomModel`，验证「短文本不调 LLM」「失败降级」「归档可回读」。
- `tests/test_run_diagnosis_strategies.py`：把 ReAct 模型和抽取模型都换成假的，验证 `legacy` vs `managed` 接线。

### 1.4 Eval

| 文件 | 看什么 |
|------|--------|
| `config/eval_cases.json` | golden set。期望值**只存在这里**。 |
| `work_agent/eval/runner.py` | `evidence_recall`、`run_case`、`run_suite`、`summarize` |
| `work_agent/cli.py` 的 `eval-diagnose` | `--case` / `--strategy` / `--no-store` |
| `tests/test_eval_runner.py` | 注入 fake `diagnose`，不调真 LLM。 |

跑真实对比（会调 LLM）：

```text
python -m work_agent.cli eval-diagnose
python -m work_agent.cli eval-diagnose --case case_error_keyerror --strategy managed --no-store
```

结果追加写入 `workspace/eval_results.jsonl`。

### 1.5 一次 managed 诊断的完整数据流

```
用户诉求 + pipelines brief
        │
        ▼
run_agent_loop                          ← 每步：invoke → tool → compress_observation
  ReActStep 列表                         ← 历史按 step 裁，system/goal 不裁
  │   裁剪时（managed）：被压/被丢的 step 原文 → ContextArchive 落盘，
  │   压缩消息挂 [原文 N 字符已归档，详见 artifact xxx] 引用；
  │   整步丢弃的留一条 stub 占位。模型缺细节时用 fetch_archived_block 回读。
  AgentLoopResult.messages               ← 完整未裁历史
  AgentLoopResult.usage                  ← ReAct 各轮 token
        │
        ▼
collect_observation_items               ← 每条 ToolMessage → 一个 ContextItem（不预压缩）
_managed_items                          ← 加上 goal(immutable) + 结论草稿(protected)
        │
        ▼
ContextManager.render                   ← 复用同一个 ContextArchive 实例
  select = normalize → dedup → rank → budget
  装得下 → render
  装不下 → compress(+archive 引用) → 再 select → 没救回的全部 archive
        │
        ▼
_extract_structured(include_raw=True)   ← 二次抽取，token 并入总量
        │
        ▼
DiagnosisResult                         ← context_text / chars / usage / selected_ids / archived_n
```

读代码时如果迷了，就问自己：我现在看的是 **ReAct 热路径** 还是 **抽取阶段一次性组装**。两套预算、两套压缩，不要混。

### 1.6 归档回读闭环：裁掉 ≠ 丢掉

`ContextArchive` 在 managed 策略下有两个写入时机，共享同一实例、同一目录：

1. **ReAct 循环内**：`trim_steps` 压缩某个 step 时（`ReActStep.compact(archive=...)`），
   该 step 的判断 + 观察全文先按 `react_<step_id>` 落盘，压缩消息末尾追加
   `reference_note(ref)`；连摘要都装不下而整步丢弃时，留一条不带 tool_calls 的
   stub AIMessage 指向 artifact。`store` 按 item_id 去重，`compose()` 每轮重算裁剪也幂等。
2. **抽取阶段**：ContextManager 压缩/挤掉的块照旧归档（原有行为）。

回读出口是只读工具 `fetch_archived_block(artifact_id)`（见 `diagnose_tools.py`，
传了 `archive` 才注册；policy 表里已登记 read_only）。这样「原文移出上下文」之后
模型仍有一条确定性的取回路径——摘要不够用时自己把原文要回来，而不是靠猜。
legacy 全链路 archive 为 None：不归档、无第 7 个工具、行为与基线完全一致。

---

## 2. ContextManager：有技术含量的地方

下面几条是面试该展开的，不是「做了个上下文裁剪器」这种空描述。

### 2.1 为什么拆成三段，而不是一个类

副作用性质不同。Selector 是纯函数，Compressor 调 LLM，Archive 写盘。混在一个类里就没法说「选择逻辑可单测、不依赖网络和磁盘」。`context_manager.py` 只编排，算法不在它身上。

这跟项目里别的拆法是同一条原则：`policy.py` 把权限事实收成一张表、`runtime.py` 把「谁来问 HITL」从「图怎么跑」里拆出去。能测的、有副作用的、只编排的，分开。

### 2.2 摘要后置，不是「每块都摘要」

处理链：

```
Normalize → Dedup → Rank → Budget
    ├─ 装得下 → 直接 Render          ← 常见路径：零 LLM、零 IO
    └─ 装不下 → Compress → Re-budget → Archive discarded → Render
```

为省 3000 字符去花一次摘要调用（可能烧掉 2000 token），收益很薄。这和 `nodes/memory.py`「消息不到 12 条就直通、不调摘要」是同一个判断。

Compressor 还有一层后置：已经短于 `summary_max_chars` 的块，就算要挂 archive 引用，也只拼后缀、不调 LLM。实现时一度把「有 suffix 就走摘要」写成了条件，短文本只因为要挂引用就会烧一次 token，这是错的。

### 2.3 Pin 三级：pinned ≠ 绕过预算

| 级别 | 含义 | 当前用在 |
|------|------|----------|
| `immutable` | 不可删不可压。**自己超预算就抛 `ImmutableBudgetExceeded`**，不静默降级 | 用户诉求 `goal` |
| `protected` | 不可删，但可压成摘要 + archive 引用 | 结论草稿 `conclusion_draft` |
| `normal` | 按分数参与裁剪，装不下进 `dropped` | 工具观察 |

如果做成 `pinned: bool` 且 pinned 直通不裁：一段 30K 的 pinned 文本就能让整个 20K 预算失效，后面的选择逻辑全部空转。`immutable` 超预算显式报错，是因为那已经不是「上下文太长」，而是 prompt 或预算配置写错了；静默截断会让后续排查极难定位。

但 `immutable` 不能直接拿用户输入原文去标。用户把整份日志贴进对话时，goal 自己就会超预算、诊断直接崩溃。所以 `_managed_items` 里 goal 先 `clip_text(..., max_chars=2000)` 再标 immutable。守卫留给配置错误，不留给用户输入。

### 2.4 打分：priority 压倒 recency

```
score = -priority * 10 + goal_relevance * 4 + evidence_bonus * 2 + recency * 0.5
```

`priority` 复用原来的 `PRIORITY_CONCLUSION=0 < EVIDENCE=1 < RULED_OUT=2 < RAG=3 < RAW_TOOL=4`。权重 10 是刻意的：这个次序是业务约定（结论比原始工具输出重要），不能被「比较新」翻盘。

`recency` 只当弱信号。如果让它主导裁剪，最早出现的证据会被系统性丢掉，而根因往往就在第一轮 `fetch_logs` 的尾部报错行。

`goal_relevance` 是目标词元在本块中的覆盖率。中文不能直接用 `retrieval.tokenize`：它把连续汉字当成一个 token，「流水线失败」对不上「流水线」。Selector 里对汉字串补了 bigram（`流水`/`水线`/`线失`/`失败`）。

`evidence_bonus` 命中 `ERROR` / `FAIL` / `Traceback` / `KeyError` 等关键词加分。词表和 `compress_observation` 共用 `EVIDENCE_KEYWORDS`，两处必须是同一份，否则「压缩器认为这行是证据、选择器认为不是」会对不上。

对照代码时看 `select_within_budget`：先把 immutable 全装进、超了就抛；再把 protected 全装进，超了不删、标进 `needs_compression`；最后 `normal` 按分数贪心填充，装不下进 `dropped`。`over_budget` 为真当且仅当有待压缩的 protected 或被挤掉的 normal。

### 2.5 去重：完全重复 + 同源包含

两层：

1. `(source, fingerprint)` 完全重复。指纹忽略大小写和空白，所以 `ERROR timeout` 和 `error   TIMEOUT` 算同一条。
2. 同源短文本被长文本包含。真实场景：`fetch_logs(tail=200)` 之后又 `fetch_logs(tail=500)`，前者是后者的子串，留两份纯属浪费预算。

不同 source 的相同文本会都留：`fetch_logs` 和 `grep_logs` 打到同一句报错，来源不同，抽取器可能需要两边都看到。

### 2.6 ReActStep：按 step 裁，不是按 Message 裁

这是整次改造里最「硬」的一条，面试被追问时优先讲它。

OpenAI 兼容端点的协议约束：每条带 `tool_calls` 的 assistant 消息，每个 `id` 都必须有一条 `role=tool` 的配对消息。按单条 Message 删历史，很容易出现：

- 留下带 `tool_calls` 的 `AIMessage`，删掉对应 `ToolMessage` → 下一次 `invoke` 直接 400；
- 或者反过来留下孤儿 `ToolMessage`，同样非法。

所以原子单元是 `ReActStep(assistant_message, tool_messages)`。`trim_steps` 从最新往回走：

1. 整步能装下 → 留全文（仍带配对的 ToolMessage）；
2. 装不下 → `compact()`：整对替换成一条**不带 `tool_calls`** 的摘要 `AIMessage`；
3. 连摘要都装不下 → 整步丢弃（AI 和 Tool 一起走）。

三种处理都保持配对不变量。`tests/test_agent_loop.py` 的 `assert_tool_pairing` 会对每一次真正发给模型的消息序列做校验，不只校验最终返回值。

热路径上刻意不调 LLM。每步 ReAct 都插一次摘要，延迟和成本都不划算；这里只用确定性的 `compress_observation`。

### 2.7 Working context vs External context

被裁掉的不是删掉。`ContextArchive.store` 把原文写到 `workspace/diagnose_archive/<run_id>/<artifact_id>.txt`，working context 里只留：

```text
[原文 18420 字符已归档，详见 artifact fetch_logs_001]
```

信息还在，只是不再常驻上下文。同一 `item_id` 重复归档只写一次。这是为事后追溯准备的：eval 或人工排查可以按 artifact 回读，而不必把 1.8 万字日志一直放在 prompt 里。

### 2.8 Token 必须把摘要开销算进去

`core/usage.py` 的存在理由就这一句：A/B 对比时，压缩自己烧掉的 token 必须进总量。否则 managed 看起来又省又快，成本藏在摘要调用里，对比是自欺。

归集点有三处，缺一不可：

1. ReAct 每轮 `model.invoke` 的 `usage_metadata`；
2. Compressor 自己的 LLM 调用；
3. 二次结构化抽取。抽取必须 `with_structured_output(..., include_raw=True)`，否则 LangChain 只给你 parsed 对象，原始 AIMessage 上的 token 统计不到。

`core/llm.py` 的 `invoke_text` 只返回字符串、丢掉用量，所以需要用量的调用方必须自己 `invoke` 拿 AIMessage。

---

## 3. Eval：每个指标干什么、怎么算

### 3.1 设计原则（面试时先讲这三个选择）

**长表，不是宽表。** 一行 = 一个 `case × strategy`，带 `strategy` 列。不要做成 `baseline_result` / `new_result` 两列——加第三种策略时宽表要改 schema，长表不用。

**期望值只在 golden 文件里。** `eval_results.jsonl` 不复制 `expected_fail_kind` / `expected_evidence_keys`。同一份期望写两处，改一条忘一条就会静默漂。

**jsonl，不是 Postgres。** 台账和 checkpoint 进库，是因为它们是运行时多租户状态（跨进程、跨会话、重启存活）。评测结果是离线开发产物：没有并发写、没有租户隔离、要的是随手 diff 两次跑分。为它建表加 repository 属于过度设计。

**只在 mock 后端上跑。** golden 的期望值绑在 `tools/mock/logs.py` 各 scenario 的尾部特征行上。真实后端的日志不长那样，期望对不上。`prepare_pipeline` 会真的 `create + start + query` 一条 mock 流水线——直接编造 `pipeline_id` 的话，状态查询全线报错，评测测的就变成「工具报错时模型怎么瞎猜」，不是在测上下文策略。

### 3.2 Golden set 里有什么

`config/eval_cases.json`，6 条，覆盖 `case` / `version` / `env` / `none`：

| case_id | scenario | 期望 fail_kind | 必须活到最终上下文的 key |
|---------|----------|----------------|--------------------------|
| `case_error_keyerror` | `case_error` | case | `KeyError`, `antenna_map`, `Traceback` |
| `version_fail_protocol_mismatch` | `version_fail` | version | `protocol mismatch`, `incompatible`, `27B` |
| `env_error_connection_refused` | `env_error` | env | `Connection refused`, `7.223.50.60`, `unreachable` |
| `env_error_logic_topology` | `env_error` | env | `Connection refused`, `retries` |
| `all_pass_no_failure` | `all_pass` | none | `verdict=pass` |
| `case_error_multi_case` | `case_error` | case | `KeyError`, `AssertionError` |

`expected_evidence_keys` 的语义是：**最终 working context 里还在**，不是「曾经在某一轮 ToolMessage 里出现过」。证据被压缩掉、被去重掉、被预算挤掉，都算没保住。

### 3.3 指标逐条

下面每个字段都对应 `run_case` 写出的一列。读结果时先分清「硬指标」和「趋势参考」。

#### `context_chars`（硬指标）

最终喂给结构化抽取的 `context_text` 的字符数，`len(extract_ctx)`。

它量的是「抽取器实际看到多大一块」。ReAct 历史裁了多少、工具观察去重了多少，最终都反映在这里。

单独看它会误判：`legacy` 把每条观察无脑压到 400 字符，`context_chars` 可能更小，但那正是「还没判断有没有余量就先丢证据」这个问题本身。必须和 `evidence_recall` 一起看。

#### `evidence_recall`（硬指标，最该盯）

```
recall = 命中的 expected_evidence_keys 数 / 总 key 数
```

在 `result.context_text` 上做大小写不敏感的子串匹配。keys 为空时记 1.0。缺失的 key 会写进 `missing_evidence`，方便看「省字符时丢掉了哪条根因」。

这是上下文管理的成败标准：省字符很容易，把 `KeyError: 'antenna_map'` 一起省掉就是净损失。

#### `token_total` / `token_input` / `token_output` / `llm_calls`（硬指标）

一次诊断里**全部** LLM 调用的累计，包括：

- ReAct 每轮决策；
- 二次结构化抽取；
- ContextCompressor 的摘要（如果触发了）。

`llm_calls` 是调用次数。managed 如果触发了摘要，`llm_calls` 应该比 legacy 多，即便 `context_chars` 更小。看到「字符少、调用次数也少」要想一想是不是摘要 token 没计入。

端点不给 `usage_metadata` 时，`usage_from_message` 仍记 `calls=1`、token 为 0。统计缺失不该打断诊断，但调用次数还要记。

#### `accuracy` / `correct`（趋势参考，不是统计结论）

`correct = (预测 fail_kind == expected_fail_kind)`。`summarize` 里的 accuracy 是该策略所有行的均值。

golden set 只有 6 条。面试里不要说「准确率提升了 X%」。它只能回答「这两种策略会不会把 case 判成 env」这种方向性问题。样本量不够支撑统计结论。

#### `latency_ms`

`run_diagnosis` 墙钟时间，毫秒。受网络抖动影响大，看趋势不看绝对值。Compressor 触发时 managed 理应更慢——用时间换字符，这是预期，不是回归。

#### `tool_calls`

ReAct 期间模型发起的工具调用次数（`type=="call"` 的 trace 条数）。上下文策略原则上不改变「模型想调几次工具」；如果两种策略 tool_calls 差很多，更可能是模型抖动，不是裁剪逻辑的功劳。

#### `selected_context_ids` / `compressed_ids` / `trimmed_steps`

可解释性字段，不是分数：

- `selected_context_ids`：最终 working context 由哪些块组成。legacy 没有这个粒度，记空列表。
- `compressed_ids`：被送进 Compressor 的块。
- `trimmed_steps`：ReAct 历史里被压缩或丢弃的 step 数。legacy 恒为 0。

### 3.4 怎么读一份对比报告

`summarize` + `format_report` 会打出类似：

```text
strategy     n  accuracy  evid_recall  ctx_chars   tokens      ms  tools  err
------------------------------------------------------------------------------
legacy       6      0.83         1.00       4200     8100     420    3.2    0
managed      6      0.83         1.00       3100     8400     480    3.2    0

managed 相对 legacy 的 context_chars 变化：-26.2%
证据留存 1.00 → 1.00，token 8100 → 8400
```

健康的改造成绩单通常是：

- `evidence_recall` 不下降（最好持平或升）；
- `context_chars` 下降；
- `token_total` 可能略升（摘要开销），这是诚实的；
- `accuracy` 在 6 条上差不多，不要过度解读。

不健康的信号：

- 字符降了、`evidence_recall` 也降了 → 在丢根因，改造失败；
- 字符几乎没变、token 却升了 → 摘要在不该触发的时候触发了；
- `err > 0` → 先看 jsonl 里的 `error` 列，不要拿残缺行算均值。

### 3.5 对当前 mock 日志要诚实

`compress_observation` 已经按 ERROR/FAIL 关键词把观察压得很短，mock 日志的报错特征又只有几行。所以现在两种策略的差距主要来自：

1. **去重**：同一份 `fetch_logs` 被取两次时，managed 只留一份；
2. **ReAct 历史裁剪**：八步之后 legacy 每次决策都重读全文，managed 会 compact 旧 step。

抽取阶段（ContextManager 那次 `render`）在 mock 上往往装得下，Compressor 经常不触发。真实日志里 ERROR 行成百上千时，超预算路径才会成为主路径。框架先接好，是为了到那时对比已经是现成的——不是现在就能拿出一张「字符砍半、准确率还升」的成绩单。

面试里主动说这句，比被问「那你的数字呢」再解释体面得多。

---

## 4. 面试怎么讲：一段 90 秒 + 可能被追问的点

### 4.1 90 秒版本（被问「上下文怎么管的」）

失败归因是 ReAct，历史会越滚越长，改造前有两个具体问题：一是按单条 Message 裁会把 `tool_call` 和 `tool` 结果拆开，OpenAI 兼容端点直接 400；二是每条观察无脑压到 400 字，预算还有余量也先丢证据。

我拆成两层。ReAct 热路径按 `ReActStep` 原子裁剪，AIMessage 和它的 ToolMessage 整对保留或整对压成一条不带 `tool_calls` 的摘要，热路径不调 LLM。抽取只发生一次，走 ContextManager：Selector 纯函数做去重和打分，装得下就直接渲染；装不下才调 fast model 摘要，原文归档到 workspace，working context 只留一句引用。Pin 分三级，pinned 不等于绕过预算。

为了证明不是自我感觉良好，诊断内核抽成 `run_diagnosis(strategy)`，同一 golden set 上 legacy 和 managed 各跑一遍。盯三个硬指标：最终上下文字符数、证据关键词留存率、全部 LLM token（含摘要自己烧的）。准确率只有 6 条样本，我只当趋势，不当统计结论。

### 4.2 面试官可能怎么问

下面按「大概率会被追」到「想显得你想过边界」排列。答案保持口头能说的长度。

**Q：为什么不直接上 embedding / 向量召回做相关性？**

诊断上下文是当次 ReAct 里刚拿到的几条工具结果，不是一个需要检索的大语料库。词面重叠（用例名、KeyError、IP、版本号）已经足够；上 embedding 要多一次模型调用，还要把当次观察先写进向量库，对这个规模是过设计。打分里 `goal_relevance` 用的是现成的 `tokenize` 加中文 bigram，零 IO。真要上 embedding，也该放在知识库检索那一层（`search_knowledge`），不要塞进当次预算选择。

**Q：去重会不会把「两次调用结果其实不同」误杀？**

指纹带 `source`，不同工具的相同文本都留。包含关系也要求同源。两次 `fetch_logs` 如果时间变了、内容变了，指纹不同，不会去重。误杀风险主要在「同一工具、同一内容、只是空白或大小写不同」——那正是该去的。

**Q：immutable 超预算直接抛异常，线上诊断岂不是会挂？**

会，但这是配置错误该挂。真正的用户输入在标 immutable 之前已经 clip 到 2000 字。如果 clip 之后仍然超预算，说明 `react` 的总预算配得比 2000 还小，那是 `profile.yaml` 写错了，应该暴露而不是静默截掉用户问题。

**Q：Archive 落本地磁盘，多实例部署怎么办？**

当前单进程、单工作区，路径是 `workspace/diagnose_archive/<run_id>/`。多实例要换成对象存储或共享盘，接口已经是 `store / read / reference_note`，换存储只需改 `ContextArchive` 内部。评测结果同理走 jsonl 而不是 Postgres，也是因为这不是运行时多租户状态。两者都是「先把语义做对（原文不丢、可回读），存储可以换」。

**Q：Compressor 用快模型，摘要会不会把 KeyError 摘要没了？**

Prompt 明确要求保留报错关键词原文、不要下结论。LLM 失败或返回空，降级为 `compress_observation`，它本身就会优先保留含 ERROR/FAIL/Traceback 的行。eval 的 `evidence_recall` 就是在盯这件事：摘要如果把 key 弄丢了，recall 会掉，对比报告里能看见。

**Q：legacy 和 managed 共用同一套工具和同一份模型，A/B 公平吗？**

公平的是「同一 case、同一 mock 场景、同一诊断内核，只换上下文策略」。不公平、也没假装公平的是：两次调用是两次独立的 ReAct，模型自己决定调几次工具，`tool_calls` 可能略有抖动。所以硬指标看 `context_chars` / `evidence_recall` / `token_total`，不把 `tool_calls` 和 `accuracy` 当因果证据。

**Q：你怎么保证裁完还能复现当次诊断？**

三份东西分开存：checkpointer 里是对话状态；`DiagnosisResult.context_text` 是当次抽取看到的 working context；Archive 里是被挪出去的原文。eval 记录还带 `selected_context_ids` 和 `run_id`。不是「裁完就没了」，是「常驻的变少，外部的可回读」。

**Q：和 memory 节点的摘要是不是重复造轮子？**

不是同一层。`memory` 压的是跨轮对话（阈值 12 条，留最近 8 条），服务的是 router / exec_params 的指代消解。ContextManager 压的是当次诊断的工具观察，服务的是二次结构化抽取。阈值以下直通、超了才摘要，判断是一样的，对象和生命周期不一样。

**Q：如果我让你加第三种策略，比如「只去重不打分」，改动大吗？**

`run_diagnosis` 加一个 `context_strategy` 分支；eval 的 `strategies=` 传入新名字；jsonl 多一种 `strategy` 值。长表不用改。这正是当时不做成 `baseline_result`/`new_result` 两列的原因。

---

## 5. 实现时踩过的问题（面试「遇到什么困难」就讲这些）

按「问题 → 为什么会犯 → 怎么改 → 现在怎么防」写。口述时挑 2～3 个最硬的讲透，不必全背。建议就讲 **5.3 ReActStep 配对**、**5.2 pin 不等于绕过预算**、**5.5 / 5.6 指标怎么选**——前两个是正确性，后一个是「你怎么知道改好了」。

### 5.1 「每一步都是纯函数」和 LLM / 写盘互相矛盾

**触发：** 第一版设计把 ContextManager 写成一条流水线，对外宣传「每一步都是纯函数」，但里面既有摘要调用又有落盘。评审一眼就看出这话不成立。

**原因：** 想强调可测试性，却把「选择算法可测」和「整个 Manager 无副作用」混成一句。有 IO 的东西不可能是纯函数。

**解决：** 拆成 Selector（纯）/ Compressor（只调 LLM）/ Archive（只写盘）/ Manager（只编排）。单测可以只测 Selector 不 mock；Compressor 单独注入 fake model；Archive 用 `tmp_path`。

**面试怎么说：** 「不是为了拆而拆，是副作用性质不同。混在一起就没法声称选择逻辑可单测。」

### 5.2 pinned 直通会让整个 budget 失效

**触发：** 初稿用 `pinned: bool`，pinned 内容不参与裁剪。追问：如果 pinned 自己就超过 20K 呢？

**原因：** 把「不允许删」理解成了「不占用预算」。预算是总和约束，特权项仍然占额度。

**解决：** 三级 pin。`protected` = 不允许删除，但仍可压缩/引用化。`immutable` 单独超预算显式抛 `ImmutableBudgetExceeded`，不静默降级。

**后续又踩的一刀：** 用户输入标成 immutable 之后，超长粘贴会真的把诊断打崩。所以 goal 先 clip 到 2000 再标。守卫留给配置错误，不留给输入。对应测试：`test_very_long_user_input_does_not_break_immutable_budget`。

### 5.3 按 Message 裁历史会 400

**触发：** 这是评审里明确说「最值得修的一点」。OpenAI 兼容协议要求 tool_call 和 tool 结果配对。

**原因：** 直觉上「删最旧的几条消息」最简单，但 ReAct 的消息不是独立的，是成对的协议单元。

**解决：** 引入 `ReActStep`，裁剪只在 step 粒度。`compact()` 整对替换成一条不带 `tool_calls` 的消息。测试里 `assert_tool_pairing` 对每次真正发给模型的序列都校验，不只看最终返回值——因为 bug 出现在「中间某一轮发给模型的那份」，最终完整历史可能仍是配对的。

**面试怎么说：** 「这不是洁癖，是端点的硬约束。按 Message 裁是在制造必现的 400。」

### 5.4 Eval schema 和测试计划互相打架

**触发：** 初稿 DDL 是 `baseline_result` / `new_result` 两列（一行一个 case），测试计划却写「每 case 两行」。

**原因：** 脑子里同时有「A/B 是一对」和「每策略一行」两种表，没选。

**解决：** 选长表。`strategy` 列，一行 = `case × strategy`。加第三种策略不用改 schema。期望值不进结果行。

**存储也降了级：** 初稿还要在 `sql/schema.sql` 加 `eval_records`。评测是离线开发产物，不是运行时多租户状态，jsonl 足够。面试被问「为什么不用数据库」就答这句。

### 5.5 指标缺了最重要的两个

**触发：** 初稿指标只有准确率、耗时、工具次数。评审指出还缺 `context_chars` 和 evidence retention。

**原因：** 下意识用「任务成没成功」衡量上下文模块。上下文模块的职责不是判对 fail_kind（那是模型和 prompt 的事），而是「在更小的窗口里把根因证据留住」。

**解决：**

- `context_chars`：最终 working context 大小；
- `evidence_recall`：golden 里声明的 key 有多少活到最终上下文；
- 准确率降级为 6 条样本的趋势参考。

`evidence_recall` 必须打在 `context_text`（抽取器真正看到的那份）上，不能打在「历史中曾经出现过」。否则只要第一轮 fetch 到了 KeyError，后面全裁光 recall 仍是 1.0，指标就废了。所以 `DiagnosisResult` 专门留了 `context_text` 字段。

### 5.6 摘要 token 不计入，A/B 就是自欺

**触发：** 设计评审时就提了；实现时 `with_structured_output` 默认还不给 raw message，抽取那一跳的 token 会凭空消失。

**原因：** LangChain 的 structured output 把 AIMessage 吃掉，只返回 Pydantic 对象。`invoke_text` 同样只回字符串。用量在这些便捷 API 后面丢掉了。

**解决：** `core/usage.py` 统一从 `usage_metadata` / `response_metadata.token_usage` 抽。抽取改 `include_raw=True`。Compressor 的 `CompressionOutcome.usage` 并进 `DiagnosisResult.token_usage`。单测断言 managed 的 `llm_calls` 在触发摘要时大于 legacy。

### 5.7 配置断裂：`observation_max_chars` 写了但没传

**触发：** Profile 里已有 `tool_result_max_chars`，观察压缩上限却在 `run_agent_loop` 里写死默认值，节点调用时没传。

**原因：** 工具出口截断（`CharBudget.take`）和进 ReAct 上下文的再压缩是两层，参数同名感很强，漏传不容易看出来——单测里显式传了 200，线上走默认 4000，行为「看起来能跑」。

**解决：** Profile 增加 `react_observation_max_chars` / `react_history_max_chars`，`run_diagnosis` 显式传入。`legacy` 的 `history_max_chars=0` 保持「不裁历史」的旧行为。

### 5.8 中文相关性几乎永远是 0

**触发：** 单测 `goal_relevance` 时发现「流水线失败」和正文对不上。

**原因：** 复用的 `retrieval.tokenize` 把连续汉字当成一个 token。整句中文对不上子串中文。

**解决：** Selector 里对汉字 token 补 bigram。英文数字词仍然整词匹配。不另造一套分词器，避免和知识检索行为分叉。

### 5.9 短文本挂 archive 引用也会去调 LLM

**触发：** 写 Compressor 时第一版条件是「超长 **或** 有 suffix 就摘要」。有 suffix（archive 引用）的短块也会烧一次 token。

**原因：** 把「要改文本」和「要调 LLM」绑在一起了。拼一个后缀不是摘要。

**解决：** 够短就只拼 suffix；只有超长才 `invoke`。`test_compressor_appends_suffix_without_llm_for_short_text` 锁住这个行为。

### 5.10 Eval 若按 strategy 外层循环，mock 流水线会丢

**触发：** `get_pipeline_tool(scenario=...)` 是 `lru_cache`，按 scenario 缓存**同一个**内存实例。如果先把所有 case 的 legacy 跑完再跑 managed，中间切了 scenario，实例被换掉（或内部 `_runs` 对不上），先前 create 的 `pipeline_id` 会 `KeyError`。

**原因：** mock 的权威状态在进程内存里，不是按 id 持久化的。评测自己在制造「查一条不存在的流水线」。

**解决：** `run_suite` 内层是 strategy、外层是 case，同一 case 的两种策略连着跑。`prepare_pipeline` 每个 `case × strategy` 都重新 create+start，不复用上一行的 id。注释写在 `run_suite` 里，避免以后为了「先跑完所有 legacy」改循环顺序。

### 5.11 返回值从 `list[Message]` 改成 `AgentLoopResult`，旧测试全挂

**触发：** 一改 `run_agent_loop` 的返回值，`tests/test_agent_loop.py` 六条全部 `TypeError: not subscriptable`。

**原因：** 这是预期的破坏性变更，但说明调用面已经不止测试——任何拿返回值当消息列表的地方都要改。节点侧改为 `loop.messages` / `loop.usage`。

**解决：** 测试全部改读 `.messages`，并补上 pairing、usage 累加、history trimming 的新断言。这反而逼出了「返回值必须是结构化结果」：eval 需要 `usage` 和 `context_chars`，继续返回裸 list 会把代价信息丢掉。

---

## 6. 口述时建议避开的说法

| 别说 | 改成 |
|------|------|
| 「我们做了一个智能上下文压缩」 | 「按 step 原子裁 ReAct 历史，抽取阶段超预算才摘要」 |
| 「准确率提升了」 | 「6 条样本上看 fail_kind 没掉；硬指标是字符数和证据留存」 |
| 「每一步都是纯函数」 | 「Selector 是纯函数；Compressor / Archive 有副作用，Manager 只编排」 |
| 「pinned 的永远不裁」 | 「immutable 不可改，protected 不可删但可压；pinned 仍占预算」 |
| 「信息删掉了」 | 「原文归档，working context 只留引用」 |
| 「token 更省」 | 「字符更省；token 要把摘要开销算进去，所以总量可能略升」 |

---

## 7. 建议的阅读顺序（给自己过一遍）

1. `error_analysis.py` 的 `run_diagnosis` 和 `DiagnosisResult`（30 分钟）
2. `agent_loop.py` 的 `ReActStep` + `trim_steps`，对照 `assert_tool_pairing`（20 分钟）
3. `context_selector.py` 从头到尾，对照 `test_context_selector.py`（30 分钟）
4. `context_manager.py` 的 `render`，看超预算分支（15 分钟）
5. `eval/runner.py` 的 `evidence_recall` + `run_case` 写出的字段（20 分钟）
6. 用自己的话把「5.3 ReActStep」「5.5 evidence_recall」「5.6 摘要 token」讲一遍，能讲顺就够面试用
