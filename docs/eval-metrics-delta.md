# 上下文策略指标差距：能说到什么程度

**最新压满预算的基准**（8000 行日志、20K 历史触顶、抽取 Compressor 触发）见 [context-trim-eval.md](context-trim-eval.md)。下文这张 **recall 0.83 vs 1.00** 的表，是「头切 + 40 行噪声」时测的，算法上更接近真正的旧提交 `36a4364`，不是当前代码里已经留尾的 `legacy` 开关。相对旧提交的粗估见 [context-trim-eval.md §5](context-trim-eval.md)。下文其余部分保留过程记录，避免和最新表混读。


本文回答一件事：**用现在这套 eval，legacy（改造前）和 managed（现在）差多少。**

结论先说：

- mock 日志已加长到 **4000 行**，失败场景在根因证据前还有 **40 行 ERROR 噪声**。在这个数据上，**evidence_recall 是真正拉开的硬指标**：legacy 均值 0.83，managed 1.00。
- 抽取阶段的 `context_chars` **managed 更大**（2646 → 10103）。这不是改造失败：legacy 每条观察无脑压到 400 字，把排在噪声后面的 `AssertionError` / `retries` 截掉了；managed 在 12K 预算内把根因行留下。
- ReAct 历史这轮 **几乎打平**（约 18.7K，两边 `trimmed_steps=0`）：8 步 × 压缩后的观察刚好还没顶到 20K。Compressor 也没触发（抽取窗口 11K < 12K）。
- **不要宣称准确率提升。** 本次 A/B 的 fail_kind 由脚本写死，accuracy 两边都是 1.0，没有信息量。真 LLM 跑 `eval-diagnose` 时模型可能直接不调工具，那组数字不能拿来比上下文策略。

测量方式：`config/eval_cases.json` 全 6 条 × 两种策略，走真实的 `run_diagnosis` + `eval.runner`；ReAct 用脚本化 8 步（反复 `fetch_logs` / `grep_logs`，把 `react_max_steps` 用满），抽取用假 structured output。**不调真 LLM。** token 里的用量随发送字符估算，只能看相对大小，不能当账单。

---

## 1. 先分清在量哪一层

一次诊断里有两块上下文，eval 现在两层都记：

| 层 | 代码 | eval 字段 | legacy | managed |
|----|------|-----------|--------|---------|
| 抽取 working context | 喂给二次抽取的 `extract_ctx` | `context_chars` / `evidence_recall` | `assemble_blocks`，观察预先压到 400 字 | `ContextManager`，观察不预压，装得下就留 |
| ReAct 发给模型的历史 | `run_agent_loop` 每次 `invoke` 前的 `compose()` | `react_context_chars` / `trimmed_steps` | `history_max_chars=0`，不裁 | 默认 20K 按 step 裁 |

面试时如果说「上下文小了」，必须讲清楚是 **ReAct 历史** 还是 **抽取窗口**。把抽取 `context_chars` 变小当成 KPI，会跟改造目标对着干。

---

## 2. 加长 mock 之后的 golden set（实测）

日志形态（`work_agent/tools/mock/logs.py`）：

- 全文 **4000 行**，前面是 INFO/DEBUG/WARN 噪声；
- 失败场景在特征证据前插入 40 行 `ERROR … noise_seq=N`（不含 KeyError / mismatch / refused 等特征词）；
- 场景可判别行仍在**文件最末**，`tail_lines=80` 仍能看到 `KeyError`。

假模型轨迹对每条 case 相同：8 步工具（`fetch_logs(tail=400)` 与 `grep` 交错，含重复 fetch），再给结论。

### 2.1 逐条

| case_id | 策略 | 抽取 chars | ReAct chars | evidence_recall | trimmed_steps | 抽取压缩 |
|---------|------|------------|-------------|-----------------|---------------|----------|
| case_error_keyerror | legacy | 2627 | 18624 | 1.00 | 0 | 否 |
| case_error_keyerror | managed | 11156 | 18573 | 1.00 | 0 | 否 |
| version_fail_protocol_mismatch | legacy | 2710 | 18650 | 1.00 | 0 | 否 |
| version_fail_protocol_mismatch | managed | 11294 | 18747 | 1.00 | 0 | 否 |
| env_error_connection_refused | legacy | 2710 | 18676 | 1.00 | 0 | 否 |
| env_error_connection_refused | managed | 11256 | 18591 | 1.00 | 0 | 否 |
| env_error_logic_topology | legacy | 2710 | 18700 | **0.50**（缺 `retries`） | 0 | 否 |
| env_error_logic_topology | managed | 11284 | 18709 | **1.00** | 0 | 否 |
| all_pass_no_failure | legacy | 2490 | 19313 | 1.00 | 0 | 否 |
| all_pass_no_failure | managed | 4477 | 19307 | 1.00 | 0 | 否 |
| case_error_multi_case | legacy | 2627 | 18575 | **0.50**（缺 `AssertionError`） | 0 | 否 |
| case_error_multi_case | managed | 11152 | 18632 | **1.00** | 0 | 否 |

### 2.2 均值

| 指标 | legacy | managed | 相对变化 | 面试怎么说 |
|------|--------|---------|----------|------------|
| accuracy | 1.00 | 1.00 | 0 | 假模型写死 fail_kind，**无信息量** |
| evidence_recall | **0.83** | **1.00** | **+0.17** | 硬收益。400 字预压把排在噪声后面的根因行切掉了 |
| context_chars（抽取） | 2646 | 10103 | **+282%** | managed 用满预算留原文；数字变大是预期 |
| react_context_chars | 18756 | 18760 | ~0 | 8 步压缩后的观察合计约 18.7K，未顶到 20K，所以没裁 |
| token_total | 25427 | 25434 | ~0 | Compressor 未触发；历史也没裁，发送量几乎一样 |
| llm_calls | （估算） | 同左 | 0 | 8 步 ReAct + 1 次抽取，无摘要调用 |
| tool_calls | 8 | 8 | 0 | 轨迹写死 |
| trimmed_steps | 0 | 0 | 0 | 见上，差 1～2K 才触顶 |

### 2.3 这张表里真正能当「改造有效」的数字

1. **证据留存**：6 条里有 2 条 legacy 丢掉特征词，managed 全留住。
   - `case_error_multi_case`：legacy 缺 `AssertionError`（ERROR 噪声占满 400 字窗口，特征行在后）。
   - `env_error_logic_topology`：legacy 缺 `retries`（同理）。
2. **重复 fetch**：managed 抽取窗口里同一份 `fetch_logs` 只留一块（`selected_context_ids` 可核对）；legacy 把压扁后的摘录拼两份。
3. **抽取字符变多**：这是「别无脑先截」的直接后果，不要说成回归。

---

## 3. ReAct 历史层（本轮为什么几乎打平）

本轮 8 步、每步观察经 `compress_observation(max_chars=4000)` 之后大约 2K 级，合计 **18.7K < 20K**，managed 的 `trim_steps` 还没轮到。

之前用「每步 1542 字 × 8、无关键字压缩」压测时（观察不经 ERROR 过滤）：

| `history_max_chars` | 最后一次发给模型的 context_chars | trimmed_steps |
|---------------------|----------------------------------|---------------|
| 0（= legacy） | 22924 | 0 |
| 20000（= 当前默认） | 20120（**-12%**） | 1 |
| 8000 | 8028（**-65%**） | 5 |

读法：历史层的差距要 **步数更多或单步观察接近 4K 且关键字过滤后仍很长** 才会出现。当前 40 行 ERROR 噪声故意卡在 4K 以下，为的是根因行能活过观察截断；代价就是还顶不满 20K 历史预算。

---

## 4. 噪声设计上踩过的坑（和指标绑在一起）

若把成百上千行 `ERROR timeout` 插在根因**前面**，`compress_observation` 会按文件顺序拼接所有证据行，再从**头部**切到 4000 字。根因在尾部，**两种策略都会丢**，eval 就测不到 ContextManager。

所以 mock 现在是：

- INFO 噪声可以很多（4000 行），先被 `CharBudget` / `observation_max_chars` 截成 head+tail；
- ERROR 噪声只有 40 行，加上特征行仍低于 4000 字，根因能进 `ToolMessage`；
- 然后才轮到 legacy 的二次 400 字预压 vs managed 的预算选择——这才是 A/B 要比的那一层。

---

## 5. 预计：真实公司日志上还会怎样

没有公司原文，下面是按机制推的量级。

| 指标 | 预计 | 可信度 |
|------|------|--------|
| evidence_recall | 噪声 ERROR 行 ≫ 根因、且根因靠后时，legacy 的 400 字预压会系统性丢词；managed 持平或更好，幅度看根因在不在 12K 窗口里 | 高（本节 golden 已见到 +0.17） |
| 抽取 context_chars | managed 常常更大，直到触顶 12K 才摘要 | 高 |
| ReAct context_chars | 单步观察接近 4K 且 ≥8 步时，managed 被 20K 封顶，大约 **-10%～-40%** | 中（第 3 节同构压测） |
| token_total | 历史未裁、未摘要时打平；摘要触发时 managed 可能略升（摘要自己要记账） | 中 |
| accuracy | 6 条样本不要比 | — |

---

## 6. 健康 / 不健康的成绩单

以后跑 `python -m work_agent.cli eval-diagnose` 时用这张表读。真 LLM 必须实际调了 `fetch_logs` / `grep_logs`（`tool_calls > 0`），否则整行作废。

| 信号 | 含义 |
|------|------|
| evidence_recall 不降或上升，抽取 chars 变大 | 正常。预算内多留了原文。本次实测就是这种。 |
| 有 case 上 legacy missing_evidence 非空、managed 为空 | 400 字预压在丢根因，改造有效。 |
| trimmed_steps > 0 且 react_context_chars 下降 | 历史裁剪在工作。 |
| evidence_recall 两边一起降 | 根因在进 ContextManager 之前就被观察层切掉了，先查 `observation_max_chars`。 |
| tool_calls = 0 | 模型没取证，不能拿来比上下文策略。 |
| accuracy 从 4/6 变成 5/6 | **不要当结论**。 |

---

## 7. 面试 30 秒说法（建议背这个，不要背整张表）

> mock 日志我加到 4000 行，失败场景前面还有几十行 ERROR 噪声。用同一套 eval、同一条 8 步取证轨迹对比改造前后：旧逻辑每条观察先压到 400 字，6 条里有 2 条把 `AssertionError`、`retries` 截没了，证据留存 0.83；新逻辑在 12K 预算内把特征行留下，留存 1.0。抽取窗口从约 2.6K 涨到约 10K——数字变大是因为不再无脑先截，不是回归。这一轮 8 步历史还没顶到 20K，所以历史裁剪几乎打平；真日志单步观察更长时才会看见按 step 封顶。准确率我不会拿 6 条样本说提升。

不要说的：

- 「上下文压缩让 token 省了一半」——本轮 token 打平，Compressor 没触发。
- 「所有指标全面领先」——抽取 `context_chars` 均值就是变大的，而且这是故意的。
- 「真 LLM 的 eval-diagnose 已经证明准确率」——试跑时模型可以不调工具，那组数无效。
