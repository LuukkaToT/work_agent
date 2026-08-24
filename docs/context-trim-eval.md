# 上下文裁剪优势：压测基准与实测

这份文档只回答：**把场景打满预算之后，managed 比 legacy 强在哪、数字是多少。**

基准（本次刻意打出来的）：

- mock 日志 **8000 行**，失败场景 **80 行 ERROR 噪声** + 尾部特征证据；
- 观察截断改为 **留尾**（根因在日志末尾，从头部切会丢掉 KeyError）；
- 评测轨迹 **8 步**工具（重复 `fetch_logs` + 多路 `grep`），走真实 `run_diagnosis` / `eval.runner`，假模型、不调真 LLM。

因此本轮能看到以前看不到的两件事：

1. ReAct 历史顶过 **20K** → managed `trimmed_steps = 1`；
2. 抽取窗口顶过 **12K** → 多数失败 case 上 Compressor 触发（`compressed_ids` 非空，`llm_calls` +1）。

---

## 1. 改造到底强在哪（先讲机制，再对表）

| 能力 | legacy | managed | 本轮有没有打出来 |
|------|--------|---------|------------------|
| 观察层留根因 | 每条再压到 400 字；留尾之后特征行通常还在 | 不预压到 400，按 12K 预算选 | recall 两边都是 1.0（留尾是共用修复） |
| 去重 | 同一份 fetch 压扁后仍拼两份 | `(source, fingerprint)` + 同源包含 | 有。managed 的 `selected_context_ids` 里 fetch 只留一块 |
| ReAct 历史封顶 20K | 不裁，随步数涨 | 按 `ReActStep` 整步 compact | **有。失败 case 历史约 -15%～-16%，trim=1** |
| 抽取超 12K | 整块按优先级丢 | 超预算才摘要 + 原文归档 | **有。4/6 条 compressed_ids 非空** |
| token | 历史越长 input 越多 | 历史变短可少花 input；摘要另计 | **有。均值 31260 → 29292（约 -6%）** |

不要用「字符越少越好」当总成绩。抽取 `context_chars` 变大，表示预算内多留了原文；ReAct `react_context_chars` 变小，才是历史裁剪的成绩。

---

## 2. 为了打满预算改了什么

### 2.1 mock 日志

`work_agent/tools/mock/logs.py`：

- `total_lines` 默认 8000；
- 失败场景 80 行 ERROR 噪声（不含特征词），特征证据仍在文件最末，`tail_lines=80` 仍能看到 `KeyError`。

### 2.2 观察截断留尾

`compress_observation` 在 keyed 行超限时改为保留**尾部**。日志根因在后，从头部切会把 80 行噪声留下、把 `KeyError` 切掉，两种策略的 recall 都会一起崩，eval 就测不到 ContextManager。

单测：`tests/test_context_budget.py::test_compress_keyed_overflow_keeps_tail_not_head`。

副作用：legacy 的 400 字预压现在也留尾，所以 **本轮 evidence_recall 打平 1.0**。这不是 ContextManager 没效，是观察层先被修对了。ContextManager 的增量改在历史封顶和抽取压缩。

### 2.3 场景是怎么补出来的

短 mock（480 行、ERROR 只有尾巴几行）测不出封顶：每条观察压缩后只有几百字，8 步也到不了 20K，抽取更装不满 12K。所以要**按预算反推数据量**，而不是随便加长。

两层预算（`config/profile.yaml`）：

| 预算 | 配置 | 要打满需要什么 |
|------|------|----------------|
| 单条观察 | `react_observation_max_chars = 4000` | 一次 `fetch_logs` / `grep` 经 `compress_observation` 之后接近 4K |
| ReAct 历史 | `react_history_max_chars = 20000` | 大约 8 步 × ~4K 观察 ＞ 20K（`react_max_steps` 默认就是 8） |
| 抽取窗口 | `min(12000, react_total_chars_budget/2)` | 去重之后仍剩 **多份互不相同的大块**（只重复 fetch 会被去成一份，顶不满） |

具体补法：

1. **INFO 铺行数。** `total_lines` 从 480 提到 **8000**。这些行几乎不含 ERROR，会被 `compress_observation` 丢掉，本身撑不满观察预算，但让 `fetch_logs(tail=400)` 的原文足够长，先吃到工具出口的 `tool_result_max_chars=8000`（head+tail），尾部才能带着噪声和根因进来。
2. **ERROR 噪声撑观察。** 失败场景在特征证据**之前**插 **80 行** `ERROR … noise_seq=N`。文案故意不用 `KeyError` / `mismatch` / `refused` / `retries` 等 golden 特征词，免得 `evidence_recall` 被噪声误伤。80×约 80 字 ≈ 6.4K keyed，超过 4000，截断后单步观察卡在上限附近。
3. **根因仍在文件最末。** `tail_lines=80` 的单测和默认 tail=200 必须还能看到 `KeyError`。80 行噪声 + 几行特征，最后 80 行仍然盖住特征。`all_pass` **不加** ERROR 噪声，否则 `ERROR not in tail=80` 会挂；这条也当成对照：装不满预算时 trim/Compressor 都不该触发。
4. **截断改留尾。** 噪声在前、根因在后。若仍从 keyed 行**头部**切 4000 字，根因被挤掉，两种策略 recall 一起变 0，后面的 A/B 无意义。所以 `compress_observation` 超限改为留尾。这是补场景时发现的，不是为了刷分。
5. **工具轨迹要「又重复、又不全相同」。** 只 `fetch_logs` 八次，managed 去重后抽取只剩一块，Compressor 仍不触发。所以 8 步设计成：

```text
1 fetch_logs(tail=400)          ← 大块
2 fetch_logs(tail=400)          ← 与 1 重复，测去重
3 grep_logs(ERROR, max=40)      ← 另一份大块（source=grep_logs，不会和 fetch 去重）
4 grep_logs(noise_seq, max=40)  ← 再一份不同指纹的 grep
5 grep_logs(场景特征词)         ← 保证 evidence_keys 能进上下文
6 fetch_logs(tail=400)          ← 再重复，把历史步数堆上去
7 grep_logs(ERROR, max=40)
8 grep_logs(场景特征词)
然后强制收尾给结论
```

历史层按 **step 计数**：重复 fetch 在历史上仍是 8 条 ToolMessage，每条 ~4K，合计能过 20K。抽取层按 **内容去重**：fetch 合成一份，再加两路大 grep，合计才能过 12K，Compressor 才有机会跑。

特征词按 case 的 `scenario` 选：`case_error→KeyError`，`version_fail→mismatch`，`env_error→Connection refused`，`all_pass→verdict=pass`。golden 仍是 `config/eval_cases.json` 那 6 条，没有另写一套 case，只是 mock 日志和取证轨迹变重了。

### 2.4 评测是怎么跑的

**没有调真 LLM。** 真跑 `eval-diagnose` 时模型可以零工具直接给结论（试过 `case_error_keyerror`，`tool_calls=0`，那组数作废）。要比的是上下文策略，必须把工具轨迹固定住。

做法：

1. `eval.runner.run_suite` 对 6 条 golden × `legacy`/`managed` 各跑一行（长表）。
2. `diagnose=` 注入包装：内部仍调真实的 `run_diagnosis`（真 `build_diagnose_tools`、真 mock 日志、真 `ContextManager` / `assemble_blocks`），只把两个模型换成脚本。
3. ReAct 模型：按上面 8 步顺序吐 `tool_calls`；第 9 次（触顶后的强制收尾）吐「根据日志给出结论」。`usage_metadata.input_tokens` 用**当次实际发给模型的字符数 / 4** 估算，所以历史变短时 token 会跟着降，不是写死常数。
4. 抽取 / Compressor 用快模型假实现：structured output 直接返回该 case 的 `expected_fail_kind`（所以 accuracy=1.0 **无信息量**）；Compressor 的 `invoke` 返回一段仍含特征词的摘要，并记 270 token，好让「多一次摘要」能进总量。
5. 每 case 先 `prepare_pipeline`（mock `create+start+query`），`get_pipeline_tool` / `get_log_tool` 按 scenario 清缓存，避免切场景后查不到 id。
6. 读的字段就是 jsonl 那几列：`context_chars`（抽取）、`react_context_chars`（最后一次 ReAct 发送）、`evidence_recall`、`trimmed_steps`、`compressed_ids`、`llm_calls`、`token_total`。

对照关系：

| 你看到的现象 | 对应测量 |
|--------------|----------|
| `trimmed_steps=1` | managed 的 `trim_steps` 把最旧一步 compact 掉了 |
| `react_context_chars` 约 23K→19K | 最后一次 `compose()` 的字符数，20K 封顶 |
| `compressed_ids=['obs-001-fetch_logs']` | 抽取超 12K 后 Compressor 动过 fetch 那一块 |
| `llm_calls` 10→11 | 8 步决策 + 1 次收尾 + 1 次抽取 + **1 次摘要** |
| `selected_context_ids` 里只有一个 fetch | 三次相同 fetch 被去重 |
| `all_pass` 两项都不触发 | 没 ERROR 噪声，观察短，15.5K＜20K、抽取也装得下 |

这不是 pytest 里的那组单测（单测用 3 步短轨迹）。那组保证接线正确；本组是 **打满预算的 A/B**。

---

## 3. 实测（6 × 2，脚本化 8 步）

### 3.1 逐条

| case_id | 策略 | 抽取 chars | ReAct chars | recall | trim | 抽取压缩 | llm_calls |
|---------|------|------------|-------------|--------|------|----------|-----------|
| case_error_keyerror | legacy | 2651 | **22832** | 1.00 | 0 | 否 | 10 |
| case_error_keyerror | managed | 8762 | **19222** | 1.00 | **1** | **是** | **11** |
| version_fail_protocol_mismatch | legacy | 2739 | 23070 | 1.00 | 0 | 否 | 10 |
| version_fail_protocol_mismatch | managed | 8621 | 19422 | 1.00 | **1** | 否（去重后刚装下） | 10 |
| env_error_connection_refused | legacy | 2739 | 23052 | 1.00 | 0 | 否 | 10 |
| env_error_connection_refused | managed | 8895 | 19432 | 1.00 | **1** | **是** | **11** |
| env_error_logic_topology | legacy | 2739 | 23045 | 1.00 | 0 | 否 | 10 |
| env_error_logic_topology | managed | 8900 | 19441 | 1.00 | **1** | **是** | **11** |
| all_pass_no_failure | legacy | 2121 | 15504 | 1.00 | 0 | 否 | 10 |
| all_pass_no_failure | managed | 4538 | 15504 | 1.00 | 0 | 否 | 10 |
| case_error_multi_case | legacy | 2651 | 22773 | 1.00 | 0 | 否 | 10 |
| case_error_multi_case | managed | 8742 | 19265 | 1.00 | **1** | **是** | **11** |

`all_pass` 没有 ERROR 噪声，观察压不满，历史 15.5K 未触顶，Compressor 也不触发。对比请看失败四条。

### 3.2 均值（含 all_pass）

| 指标 | legacy | managed | 变化 | 含义 |
|------|--------|---------|------|------|
| evidence_recall | 1.00 | 1.00 | 0 | 留尾后两边都能看到特征词 |
| 抽取 context_chars | 2607 | 8076 | **+210%** | 预算内多留原文；压缩后仍大于 legacy 的 400×N |
| react_context_chars | 21713 | 18714 | **-13.8%** | 20K 封顶生效 |
| token_total | 31260 | 29292 | **-6.3%** | 历史变短省的 input，大于摘要那一次的开销 |
| llm_calls（失败 case） | 10 | 10 或 11 | 摘要触发时 +1 | 8 步 ReAct + 收尾 + 抽取，[+ Compressor] |
| trimmed_steps（失败 case） | 0 | 1 | 旧 step 被 compact | 配对不变量仍成立 |
| tool_calls | 8 | 8 | 0 | 轨迹写死 |

失败四条上 ReAct 历史大约 **23000 → 19300（约 -16%）**，比总均值更干净。

### 3.3 优势对应到数字

1. **历史不会无限涨。** legacy 最后一次发给模型约 23K；managed 压在约 19.2K，并记录 `trimmed_steps=1`。这是 20K 预算第一次被打实。
2. **超抽取预算会压缩而不是静默丢。** 4 条失败 case 的 `compressed_ids` 含 `obs-001-fetch_logs`：大块被压成摘要，`llm_calls` 从 10 变成 11，摘要 token 记进总量。
3. **重复 fetch 不占两份。** managed `selected_context_ids` 里只有一个 `fetch_logs` 块，其余是不同 pattern 的 grep。
4. **token 没有因为「多一次摘要」而整体变贵。** 本轮历史缩短省下的 input 更多，总量约 -6%。不能外推成「永远更省」：若历史本来就短、只狂触发摘要，总量可能略升。
5. **recall 本轮打平** 是观察留尾的结果。上一版短噪声、从头部截时，legacy 会丢掉 `AssertionError` / `retries`（0.83 vs 1.00）。那一档优势在「观察层切错」时仍然成立；本基准把切错修掉了，增量改到历史和抽取压缩。

---

## 4. 怎么读、面试怎么说

健康信号（本轮都有）：

- 失败 case：`trimmed_steps > 0` 且 `react_context_chars` 下降；
- 多数失败 case：`compressed_ids` 非空且 `llm_calls` 多 1；
- `evidence_recall` 不降；
- token 持平或略降。

不要说：

- 「抽取字符砍半」——本轮抽取窗口是变大的；
- 「准确率提升」—— fail_kind 是脚本写死的；
- 「Compressor 每条都触发」—— `version_fail` 去重后刚装进 12K，`all_pass` 更装不满。

30 秒：

> 我把 mock 日志加到 8000 行、失败场景 80 行 ERROR 噪声，观察截断改成留尾，再用 8 步取证把预算打满。旧方案 ReAct 历史涨到约 23K；新方案按 step 压在约 19K，trim 掉最旧一步，不会把 tool_call 和结果拆开。抽取超过 12K 时才调快模型摘要，原文归档，4 条失败 case 上能看到多 1 次 LLM、fetch 大块进了 compressed_ids。token 本轮大约少 6%，因为历史变短省的 input 盖过了摘要开销。证据留存两边都是 1.0——根因在尾部，留尾之后 400 字窗口也能看到；ContextManager 多出来的是封顶和超预算压缩，不是再截一次根因。

---

## 5. 复现

打满预算的 A/B **不是** `eval-diagnose` 默认行为（默认真 LLM 自己决定调几次工具）。当时是：注入 8 步脚本模型 + 真实 `run_diagnosis`，对 `config/eval_cases.json` 跑 `run_suite`。脚本用完已删，要复现就按 §2.3 / §2.4 把轨迹和假模型接回去。

```text
# 锁住留尾截断 + 长日志形态
python -m pytest tests/test_context_budget.py tests/test_log_tool.py -q

# 真 LLM 整套（仅当 tool_calls>0 才有资格进对比）
python -m work_agent.cli eval-diagnose --no-store
```

相关代码：`tools/mock/logs.py`，`context_budget.compress_observation`，`agent_loop.trim_steps`，`context_manager.render`，`eval/runner.py`，`config/eval_cases.json`。
