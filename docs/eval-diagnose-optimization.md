# 真 LLM 诊断评测：怎么跑、怎么看、两轮怎么改

本文记录 2026-09-09～09-10 围绕 `eval-diagnose` 的完整过程：结果写在哪、字段怎么落盘、怎么读、怎么拆账，以及据此做的**两轮优化**（抽取负优化收口 → 短任务 overhead / 长任务 stress）。

配套设计仍看 [context-manager-and-eval.md](context-manager-and-eval.md)。20 条场景说明看 [baseband-mock-benchmark.md](baseband-mock-benchmark.md)。打满 20k 的假模型压测机制看 [context-trim-eval.md](context-trim-eval.md)（文中 12k 抽取预算已过时，以本文和 `config/profile.yaml` 为准）。

**这不是生产准确率。** 20 条 synthetic 样本上的 `kind_acc` / `root_acc` 只作趋势；同一模型连跑两次，个别题的分类会翻。能当硬结论的是：抽取窗口有没有回到 10k、证据有没有丢、短任务中位数有没有明显变差、长链路上 managed 是否封顶且 token 不高于 legacy。

---

## 1. 评测在测什么

失败归因节点 `error_analysis` 是全图唯一的 ReAct：模型自己决定调 `fetch_logs` / `grep_logs` 等，最后再做一次结构化抽取，吐出 `fail_kind` / `root_component` / `evidence`。

上下文策略有两套，**同一份诊断内核**上切换，不起第二张图：

| 策略 | ReAct 历史 | 抽取 working context | 用途 |
|------|------------|----------------------|------|
| `legacy` | 不裁（`history_max_chars=0`） | `assemble_blocks`：观察先压到约 400 字再拼 | A/B 对照 |
| `managed` | 按 `ReActStep` 整步裁，默认 20k | `render_diagnosis_context`（不再把 raw ToolMessage 丢进通用 `ContextManager.render`） | 线上默认路径 |
| `component_parallel` | 按 managed 投影上下文 | 另一套诊断引擎 | **必须显式 `--strategy`，本次两轮都没跑** |

默认命令只对比 `legacy` 与 `managed`（20 条 × 2 = 40 组）。

golden set：`config/eval_cases.json`，suite 名 `baseband_layered_benchmark_v1`。期望的 `fail_kind` / `root_component` / `expected_evidence_keys` **只存在这份 json 里**，不复制进结果文件，避免改一处忘一处。

一次诊断里要分开看的三层（混在一起就会改错东西）：

| 层 | 代码 | 结果字段 | 含义 |
|----|------|----------|------|
| ReAct 发给模型的历史 | `run_agent_loop` 每次 `compose()` | `react_context_chars`（最后一轮快照）、`react_prompt_chars_sum`（各轮之和）、`trimmed_steps` | 历史有没有封顶 |
| 二次抽取窗口 | `render_diagnosis_context` / `assemble_blocks` | `context_chars`、`evidence_recall`、`selected_context_ids` | 最终喂给抽取器的原文 |
| 账单 | 各阶段 `TokenUsage` | `token_total`、拆开的 `react_token_*` / `compress_*` / `extract_*` | 花了多少；短任务看**中位数** |

`react_context_chars` 解释不了均值被长尾拉高：它只是最后一轮快照。要从第二轮起用 `react_prompt_chars_sum`。

---

## 2. 怎么跑

入口：

```powershell
python -m work_agent.cli eval-diagnose --pause 1
```

- 实现：`work_agent/cli.py` 的 `eval-diagnose` → `work_agent/eval/runner.py` 的 `run_suite`。
- `--pause 1`：相邻两次诊断停 1 秒，防 OpenRouter 限流。本仓库这三轮真 LLM 都用这个参数。
- `--case` / `--strategy`：只跑一条或一种策略；`--no-store` 只打印不写 jsonl。
- 模型以 `.env` 为准（这三轮是 OpenRouter 上的 `z-ai/glm-5.2`）。密钥不要写进文档或提交。
- `TOOL_BACKEND=mock`，读的是分层基带 mock 日志，不连真基站。

跑批时终端几乎没有逐条日志：进度在 Rich 状态栏（`评测中 · k/40 · case_id/strategy`）。整批结束才打印一张汇总表，并写「已追加写入 …/eval_results.jsonl」。中途看进度，读 jsonl 最后一行的 `case_id`，不要另开第二场——会抢同一把 API、往同一文件追加。

假模型长任务不走这条 CLI，走 pytest：

```powershell
python -m pytest tests/eval/test_long_context_stress.py -q
```

轨迹写死在 `work_agent/eval/long_context.py`（8000 行 mock 日志 + 8 步 fetch/grep），token 按发送字符 `/4` 估算，**不调真 LLM**。

有效行的硬条件：`tool_calls > 0`。模型零工具直接给结论，比的就不是上下文策略，整行作废。这三轮 40/40 都没有零工具行。

---

## 3. 结果写在哪里、怎么写

### 3.1 分数：`workspace/eval_results.jsonl`

离线开发产物，不进 Postgres。`runner.py` 文件头写了原因：没有并发写、没有租户隔离，要的是随手 diff 两次跑分。

- 一行 = 一个 `case × strategy`（长表，不是宽表两列）。加第三种策略不用改 schema。
- `run_suite` 每跑完一组就 `append_records` **追加一行**。几十分钟的真 LLM 批被中断，已完成的行还在。
- 每次 `run_suite` 生成一个 12 位 hex `run_id`（`uuid.uuid4().hex[:12]`）。三轮不要混着平均。
- 诊断内核内部的 `run_id` 更细：`{run_id}-{case_id}-{strategy}`，给 Archive 目录用，和 jsonl 的批次 id 不是同一个粒度。

写入逻辑在 `run_case`：先 `prepare_pipeline` 建 mock 流水线，再 `run_diagnosis(...)`。单条异常不中断整批，该行带 `error` 字段，准确率记 0。

同一 case 的 `legacy` 和 `managed` **连着跑**。mock 流水线工具按 scenario 缓存单实例，中途切 scenario 会清内存，先前的 `pipeline_id` 就查不到。

### 3.2 原文归档：`workspace/diagnose_archive/<run_id>/`

被裁掉的观察不是删掉，而是 `ContextArchive.store` 落到 `<artifact_id>.txt`。working context 里只留摘要加引用。这是过程材料，**不是评测分数**。不要用归档目录当跑分结果。

### 3.3 终端汇总表

`format_report(summarize(rows))` 只在 40 组结束后打印。表里的 `ctx_chars` 是抽取窗口均值，`react_ctx` 是 ReAct 最后一轮均值，`tokens` 是 `token_total` 均值。第二轮之后额外打出 token **中位数**，并注明短任务看中位数、均值会被长尾拉高。

这张表没有另存成报告文件。要复现数字，按 `run_id` 过滤 jsonl。

### 3.4 一行里有什么

核心字段（第二轮起才稳定有阶段拆账；`8241` / `b729` 的 `react_token_*` / `react_prompt_chars_sum` 为 0 或缺失，不能拿来对比阶段）：

| 字段 | 来源 | 怎么用 |
|------|------|--------|
| `run_id` / `case_id` / `strategy` / `recorded_at` | runner | 批次、题目、对照、时间 |
| `fail_kind` / `root_component` | 抽取结构化输出 | 和 golden 比 |
| `correct` | `fail_kind == expected` | 历史兼容的 kind 准确率 |
| `root_component_correct` | 根因组件是否命中 | 无期望时为 `null` |
| `diagnosis_correct` | kind 对 **且** 根因组件不是错的 | 锁回归差集时以这个为主 |
| `evidence_recall` / `missing_evidence` | 在 `context_text` 上做大小写不敏感子串 | 上下文管理最该盯的质量 |
| `context_chars` | 抽取窗口字符数 | **不是**「越小越好」 |
| `react_context_chars` | 最后一轮发给 ReAct 的字符 | 短任务上两边都打不满 20k |
| `react_prompt_chars_sum` | 各轮 `compose()` 字符之和 | 解释 token 长尾 |
| `token_input` / `token_output` / `token_total` / `llm_calls` | 合计用量 | 短任务看中位数 |
| `react_token_input` / `react_llm_calls` | 仅 ReAct | 拆账 |
| `compress_token_total` / `compress_llm_calls` | 仅抽取期摘要 | 短任务上几乎为 0 |
| `extract_token_input` / `extract_llm_calls` | 仅二次抽取 | 抽取窗口收口后应落在约 3k input |
| `tool_calls` / `trimmed_steps` / `readback_calls` | 轨迹与裁剪 | `readback_calls` 计 `fetch_archived_block` |
| `archived_n` / `selected_context_ids` / `compressed_ids` | 归档与入选 | 核对去重、是否触发 compressor |
| `error` | 执行失败 | 非空则该行作废 |

`summarize()` 按 strategy 聚合：准确率、recall、字符均值、`token_total` 均值和中位数、时延、工具次数、error 计数。

---

## 4. 怎么看、怎么分析

### 4.1 看某一批

仓库里的临时脚本（可改 `run_id`，不必提交）：

```powershell
python tmp/_summarize_eval_run.py a4145c290be4
python tmp/_compare_eval_runs.py b72925255e52 a4145c290be4
```

手写过滤也可以：读 `workspace/eval_results.jsonl`，留下 `run_id` 相等的 40 行，再按 `strategy` 分桶。

跑批中途：看 jsonl 最后一行的 `case_id`/`strategy`/`recorded_at`。进程还在、最后一行几分钟前写入，通常是当前那组还在打工具，不是卡死。

### 4.2 分析时固定问的几件事

1. **这一层是哪一层？** 抽取 `context_chars` 变大，可能是「预算内多留了原文」，不一定是回归。ReAct 历史变小，才是 Transcript / `trim_steps` 的成绩。
2. **差集锁哪张表？** 以 `diagnosis_correct` 为主（kind+组件），`fail_kind` 单独再列一列。只看 kind 会漏掉「类型对了、根因组件 unknown」那种。
3. **质量能不能记在 Context Manager 头上？** Skill、`classification.md`、抽取 prompt 若和上下文同一批改，分数涨了不能全算上下文。
4. **token 均值有没有被 3～5 条长尾撑起来？** 先看中位数，再列出 `managed - legacy` 最大的几条，核对 `tool_calls` 是否多了 1～4 轮。多搜且判对了，不要用截步去压均值。
5. **Compressor / trim 有没有真正触发？** 短任务 `trimmed_steps` 均值接近 0，20k 封顶根本没打中，不能用来证明 Context Manager「省钱」。
6. **有没有零工具行、error 行？** 有则先剔除再比。

不要做的事：把 `eval-diagnose` 准确率画进 Grafana；拿 BFCL `ast_acc` 和这 20 条横比；默认加跑 `component_parallel`。

---

## 5. 三轮批次一览

全部在同一份 `workspace/eval_results.jsonl`。用 `run_id` 切开。

| 批次 | `run_id` | 墙钟 | 代码状态 | 目的 |
|------|----------|------|----------|------|
| 对照翻车 | `8241cf1bad71` | 2026-09-09 19:58 → 20:44（约 47 分钟） | 抽取仍可能把 raw ToolMessage 喂进通用 render，预算 12k | 真 LLM 20×2 基线 |
| 第一轮收口后 | `b72925255e52` | 2026-09-09 22:47 → 23:31（约 44 分钟） | `extract_chars_budget=7000` + 确定性摘录 + 证据受保护 | 验证抽取负优化是否消失 |
| 第二轮验证 | `a4145c290be4` | 2026-09-10 00:26 → 01:05（约 39 分钟） | 加上阶段拆账、回读摘录、报告中位数；长任务假模型已收回 CI | 确认短任务不回归 |

三轮命令相同：`python -m work_agent.cli eval-diagnose --pause 1`。模型相同。40/40，`error=0`，`tool_calls=0` 的行数为 0。

---

## 6. 第一轮：抽取负优化收口

### 6.1 现象（`8241`）

真 LLM 跑完后，ReAct 历史是 **managed 更短**（约 17.4k → 14.7k），说明按 step 裁历史是对的。真正翻车的是二次抽取窗口 `context_chars`：

| 指标 | legacy | managed |
|------|--------|---------|
| fail_kind | 16/20（80%） | 14/20（70%） |
| 双对 `diagnosis_correct` | 16/20（80%） | 11/20（55%） |
| evidence_recall | 0.967 | 0.933 |
| 抽取 `context_chars` 均值 | **5537** | **10167** |
| ReAct 最后一轮 | 17425 | 14705 |
| token 均值 | 38736 | 41344 |
| token 中位数 | 33165 | 39822 |

证据掉点的例子：`b08` 丢掉 `attempt=12`，`b17` / `b20` 也掉。token 合计大约 775k → 827k。

### 6.2 怎么定位到抽取层

不是「ContextManager 没工作」，而是 **工作在错误的输入上**：

1. 抽取预算当时写死 `min(12_000, react_total_chars_budget/2)`。
2. managed 把未预压的 ToolMessage 全文（单条观察上限 4000）当成 `PIN_NORMAL` 的 `tool_result` 塞进通用 `ContextManager.render`。
3. 拼出来约 10.2k，仍低于 12k，**Compressor 根本不触发**，窗口就被原文填满。
4. legacy 走 `compress_observation(..., max_chars=400)` 再 `assemble_blocks`，自然落在约 5k。

只把 12k 改成 7k、却仍喂 4k 原文，会把 raw dump 推进 LLM 摘要，比现在更差。所以不能只改一个数字。

### 6.3 锁定差集（重跑必须再出同一张表）

以 `diagnosis_correct`：legacy 对、managed 错（5 条）：

| case | legacy | managed | 备注 |
|------|--------|---------|------|
| `b04_rat_invalid_bandwidth` | case/rat | case/unknown | kind 对，组件丢了 |
| `b07_bbh_fronthaul_sequence_gap` | env/bbh | version/bbh | kind 也错 |
| `b14_cell_load_cascade_from_rat` | case/rat | version/rat | kind 也错 |
| `b16_rach_timing_out_of_window` | env/bbh | version/rat | kind 和组件都错 |
| `b20_transient_subscription_recovered` | none/none | none/bbh | 把恢复误判成故障组件 |

fail_kind 回归是其中的 `b07` / `b14` / `b16`。两侧都错的（`b06`/`b08`/`b09`/`b17`）不当成「managed 独有的锅」，但 `b08` 的证据缺失要盯。

### 6.4 决定改什么、不改什么

当时代码里已经有 `diagnosis_context.py`：日志用确定性 `log_excerpt`（异常 / 恢复 / `attempt=` 轮流留行），普通块装不下只归档、不调摘要。方向对，比「一律再压 400 字」更贴这 20 条。

补上的门禁是：

1. **`extract_chars_budget=7000`** 写进 `config/profile.yaml` 和 `Settings`，`run_diagnosis` 用它替换写死的 12000。`limit = min(extract_chars_budget, react_total_chars_budget/2)`。
2. **单条日志摘录上限 800**（不是 1600）。总预算 7k，若干条 fetch/grep 各占 1600 仍会把窗口顶满。
3. **日志和 pipeline status 升 `PIN_PROTECTED`，`kind=evidence`**。预算一紧也不能把 grep 命中裁掉。
4. **证据不走 `compress_batch` LLM**。`ContextCompressor` 对 `kind=evidence` 只做确定性摘录；`compress_batch` 最多压结论草稿。普通块溢出只归档。
5. **单测三轴**（`tests/graph/helpers/test_diagnosis_context.py`）：普通块超预算 `compressor.calls=0`；受保护块最多一次 `compress_batch`；本案故障行不被外对象 ACK / 更早 ACTIVE 挤出窗口。`log_excerpt` 并不按对象过滤，跨对象结案主要靠分类规则，测试不能假装已经按 cell/peer 切开。

明确不做：

- 不碰 Multi-Agent / `component_parallel`。
- 不把抽取改回「一律 `compress_observation` 400」。
- 不把质量提升全写进 Context Manager：分类规则、抽取 prompt 和上下文在同一批改过。

### 6.5 收口后重跑（`b729`）

| 指标 | legacy | managed | 相对 `8241` |
|------|--------|---------|-------------|
| fail_kind | 19/20 | 18/20 | managed 14→18 |
| 双对 | 16/20 | 17/20 | managed 11→17 |
| recall | 0.983 | 0.983 | 两侧都拉回来 |
| 抽取窗口 | 5211 | **5295（+1.6%）** | 从 10.2k 回到约 5.3k |
| ReAct 最后一轮 | 15824 | 15871 | 短任务上打平，封顶仍没打中 |
| token 均值 | 41259 | 44281（**+7.3%**） | 见下一轮 |
| token 中位数 | 40501 | 45255 | 这轮中位数 managed 并不低，见 §7.1 |
| `trimmed_steps` 均值 | 0 | 0.20 | 几乎不裁 |
| 零工具 / error | 0 | 0 | |

`8241` 上那 5 条双对回归，`b729` 里只剩 **`b16`**（legacy env/bbh 对，managed 判成 case/rat）。`b04`/`b07`/`b14`/`b20` 都双对了。两侧仍错：`b08`（kind 对、组件不对）、`b17`（unknown/unknown）。

结论：抽取负优化消失。剩下来的 `b16` 是分类错，recall=1.0，不能再怪 10k 窗口。token 均值贵 7% 是下一轮的问题，不是再把抽取往回砍。

---

## 7. 第二轮：短任务只消灭真 overhead，长任务才是考场

### 7.1 对「+7% token」的拆账

先把 `b729` 的 `token_total` 按 case 做 `managed - legacy`，头部几乎全是 **多搜了几轮 ReAct**，不是每轮均匀加税：

| case | Δ token | tools legacy→managed | 这轮判对了吗 |
|------|--------:|----------------------|--------------|
| `b09_tx_publication_debug_stall` | +31920 | 12→17 | managed 双对（legacy 组件错） |
| `b14_cell_load_cascade_from_rat` | +27689 | 11→12 | 双对 |
| `b08_rx_subscription_debug_stall` | +25513 | 9→14 | kind 对、组件仍错 |
| `b06_bbh_clock_unlocked` | +17409 | 10→13 | 双对 |
| `b04_rat_invalid_bandwidth` | +11260 | 7→11 | 双对 |

这三条（b09/b14/b08）就能把均值撑起来。差额几乎全是 **input**。`react_context_chars` 两侧都约 15.8k，解释不了均值。

计划阶段曾说「中位数 managed 已略低」。用 jsonl 按偶数个样本取中间两数平均，`b729` 实际是 **40501 → 45255**（managed 更高）。定性仍然成立：**不是每题都贵，是长尾**。因此：

- 短任务门禁用中位数，不用「均值 managed ≤ legacy」。
- **不要截 b04/b14 的额外 tool** 去刷均值——它们判对了。
- **不要再砍 Evidence Ranking，也不要把 5.3k 抽取窗口继续往下压。**

`8241`/`b729` 还没有阶段字段，当时无法在 jsonl 里直接看到 ReAct vs 抽取 vs 压缩各花多少。这本身就是要补的可观测性。

### 7.2 决定的优化手段（三层，不是再开更长的真 LLM）

用户确认的方案：

1. **短任务（真 LLM 20 条）**  
   保证质量不差；token 看中位数；只消灭证得出来的 overhead。不自动把「managed ≤ legacy 均值」当门禁。

2. **长任务（假模型、固定 8 步）**  
   把已经删掉的打满预算脚本收回 CI。失败场景必须 `trim≥1`、ReAct chars 有上界、**token_total ≤ legacy**、recall 不降。不要再开「更长的真 LLM 组」——轨迹不齐，又贵又比不出封顶。

3. **Multi-Agent stress 冻结。**

短任务上具体改了三处：

**（1）阶段拆账**

- `AgentLoopResult.prompt_chars_sum`：每次 `compose()` 后累加。
- `DiagnosisResult` 增加 `react_prompt_chars_sum`、`react_token_*`、`compress_token_*`、`extract_token_*`。合计 `token_usage` 不变，避免 A/B 漏记摘要。
- jsonl 同步这些字段；`summarize` 增加 `token_median`；报告脚注改为短任务看中位数。

**（2）回读走摘录**

`fetch_archived_block` 不再把 archive 全文灌回下一轮。`excerpt_archived_block` 默认 800 字：先 `log_excerpt`，退化则 `plain_excerpt`。原文仍在磁盘。避免「trim 完又把 4k～8k 读回来」，那是真 overhead，而且短任务偶尔 `readback_calls>0`（`8241` managed 2 次，`b729` 1 次）。

**（3）长任务组收回**

`work_agent/eval/long_context.py`：4 条旧场景（`case_error` / `version_fail` / `env_error` / `all_pass`），8000 行 mock，写死 8 步 fetch/grep。测试在 `tests/eval/test_long_context_stress.py`：

- 失败 case：`trimmed_steps≥1`、ReAct chars / `react_prompt_chars_sum` / `token_total` 均 ≤ legacy、recall=1。
- `all_pass` 不加 ERROR 噪声，打不满预算，两边都不该裁（负对照）。

实施时相关单测 99 个通过；验证前再跑长任务 + runner + diagnose tools 等 76 个通过。然后才开 `a414` 真 LLM，**没有**并行第二场，也没有跑 `component_parallel`。

### 7.3 验证结果（`a414` vs `b729`）

| 指标 | `a414` legacy | `a414` managed | 对照 `b729` managed |
|------|---------------|----------------|---------------------|
| fail_kind | 19/20 | **20/20** | 18/20 |
| 双对 | 16/20 | **18/20** | 17/20 |
| recall | 0.983 | **1.000** | 0.983 |
| 抽取窗口 | 4958 | 5415（+9.2%） | 5295；**没有回到 10k** |
| ReAct 最后一轮 | 17200 | 15602（−9.3%） | 15871 |
| token 均值 | 43001 | 43816（+1.9%） | 上一轮 +7.3% |
| token 中位数 | 44585 | **43754** | 本轮 managed 更低 |
| ReAct input 均值 | 35692 | 36148 | 新字段，可拆账 |
| 抽取 input 均值 | 3149 | 3386 | 和 5k 窗口一致 |
| compress token 均值 | 0 | 219 | 短任务几乎不摘要 |
| `trimmed_steps` 均值 | 0 | 0.10 | 仍打不满 20k |
| `readback_calls` 合计 | 0 | 0 | 回读这轮没打到 |
| error / 零工具 | 0 | 0 | |

本轮 `diagnosis_correct` 差集：

- legacy 对、managed 错：只有 `b17`（两侧 kind=env，managed 组件写成 `ue_or_radio_link`）。
- managed 对、legacy 错：`b09`、`b13`、`b16`。
- 两侧都错：仍是 `b08`（kind=env，组件 comm vs bbl）。

`b729` → `a414` 的质量翻页（同一题、同一策略，对错变了）有 `b10`/`b13`/`b16`/`b17`。典型是 `b16`：上一轮 managed 错、这一轮 managed 对而 legacy 错。**20 条上这种抖动正常，不能写成准确率提升。**

token 长尾换了题目：本轮最大是 `b12` +31926（tools 10→14，`react_prompt_chars_sum` 37320→111742），再次证明贵在多搜，不在每轮 Context 税。`b06` 这轮反而 managed 便宜 2.8 万。

阶段拆账对齐了先前的判断：短任务上 compressor 基本不跑；抽取只占约 3.3k input；账单主体是 ReAct。回读摘录在这 20 条上几乎没被打到（`trimmed_steps` 太低），它的回归保护在假模型长任务组和 `test_diagnose_tools`。

### 7.4 长任务组（Context Manager 的考场）

短任务 20 条证明不了「优化上下文管理图什么」：完整历史在短轨迹上是强 baseline。仓库里曾经用 8000 行日志 + 写死 8 步 + 假模型打满 20k，失败 case 历史约 23k→19k、token 约 −6%，脚本后来删了。第二轮把它收回 CI。

门禁含义：

- `trim≥1`：20k 真的触顶了。
- ReAct chars / prompt_sum / token_total ≤ legacy：规模上去之后 managed 有成本上界。
- `all_pass` 不要求 trim：装不满时两边行为应一样，防止测试造假。

面试口径与实现一致：**短任务保证不添乱（质量、中位数）；长链路用固定轨迹证明封顶。**

---

## 8. 两轮之后现在怎么读数

把三轮抽成一张总表（均为 20 条均值，除非标明中位数 / 计数）：

| | `8241` L | `8241` M | `b729` L | `b729` M | `a414` L | `a414` M |
|--|--------:|--------:|--------:|--------:|--------:|--------:|
| fail_kind | 16/20 | 14/20 | 19/20 | 18/20 | 19/20 | 20/20 |
| 双对 | 16/20 | 11/20 | 16/20 | 17/20 | 16/20 | 18/20 |
| recall | 0.967 | 0.933 | 0.983 | 0.983 | 0.983 | 1.000 |
| 抽取 chars | 5537 | **10167** | 5211 | **5295** | 4958 | **5415** |
| ReAct 末轮 | 17425 | 14705 | 15824 | 15871 | 17200 | 15602 |
| token 均值 | 38736 | 41344 | 41259 | 44281 | 43001 | 43816 |
| token 中位数 | 33165 | 39822 | 40501 | 45255 | 44585 | 43754 |

可以下的结论：

1. **第一轮改对了层。** 抽取从 10.2k 回到约 5.3～5.4k，recall 回来，`8241` 上那张 5 条双对差集不再稳定复现。
2. **第二轮没有把短任务均值门禁化。** `a414` 均值只贵 1.9%，中位数 managed 更低；剩下的差额仍是个别题多搜。
3. **Context Manager 的存在理由在长任务 CI**，不在这 20 条的 token 均值。
4. **不要宣称准确率提升。** `a414` managed 20/20 kind 好看，但和 `b729` 之间有分类翻页，样本太小。

配置现状（`config/profile.yaml`）：

- `react_observation_max_chars=4000`
- `react_history_max_chars=20000`
- `extract_chars_budget=7000`
- `diagnosis_engine=legacy`（线上诊断引擎开关；上下文策略节点本身走 managed）

---

## 9. 代码地图（改动落在哪）

| 主题 | 文件 |
|------|------|
| 跑批 / 落盘 / 汇总 | `work_agent/eval/runner.py`、`work_agent/cli.py` |
| golden | `config/eval_cases.json` |
| 抽取预算 | `config/profile.yaml`、`work_agent/core/config.py`、`run_diagnosis` 里的 `limit` |
| 确定性摘录与受保护证据 | `work_agent/graph/helpers/diagnosis_context.py` |
| 证据不走 LLM 摘要 | `work_agent/graph/helpers/context_compressor.py`（`kind=evidence`） |
| 回读摘录 | `work_agent/graph/helpers/diagnose_tools.py` 的 `fetch_archived_block` |
| 各轮 prompt 之和 | `work_agent/graph/helpers/agent_loop.py` |
| 阶段字段 | `DiagnosisResult`（`error_analysis.py`） |
| 长任务假模型 | `work_agent/eval/long_context.py`、`tests/eval/test_long_context_stress.py` |
| 抽取单测 | `tests/graph/helpers/test_diagnosis_context.py` |

`docs/context-manager-and-eval.md` §1.1 仍可能写着抽取 `min(12000, …)` 和「抽取走 `ContextManager.render`」。以 `extract_chars_budget` 和 `render_diagnosis_context` 为准。

---

## 10. 还没做 / 不要做

- Multi-Agent / `component_parallel` 真 LLM 评测：冻结。
- 为短任务均值去截步、去砍 Ranking、再压 5.3k 抽取窗口。
- 用更长的真 LLM 组代替假模型 8 步。轨迹不齐就比不出封顶。
- 把这 20 条的 kind/root 准确率说成生产质量，或和 BFCL 横比。
- 并行开第二场 `eval-diagnose`（抢 jsonl 和 API）。
