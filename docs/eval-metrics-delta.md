# 上下文策略指标差距：能说到什么程度

本文回答一件事：**用现在这套 eval，legacy（改造前）和 managed（现在）差多少。**

结论先说：

- 当前 mock golden set 上，**不要宣称「字符砍半、准确率提升」**。硬指标里 `evidence_recall` 两边都是 1.0；抽取阶段的 `context_chars` 均值甚至 **managed 更大**。
- 这不是改造失败。legacy 每条观察无脑压到 400 字，字符数被人为压低，根因原文可能已经被截掉。managed 的目标是 **预算内尽量留证据**，不是「数字越小越好」。
- 真正能拿出手数的差距在两处：**(1) 同一份日志被取两次时的去重；(2) ReAct 历史按 step 裁剪**。抽取阶段的 Compressor 在 mock 上几乎不触发。

测量方式：`config/eval_cases.json` 全 6 条 × 两种策略；ReAct 与抽取都用脚本化假模型（固定「fetch 两次 + grep 一次」），**不调真 LLM**。因此 `accuracy` 两边都是 1.0，只说明接线没把 fail_kind 弄丢，**不能当模型质量结论**。token 里的用量也是假模型写死的，只有「调了几次 LLM」这一层有解释力。

---

## 1. 先分清在量哪一层

一次诊断里有两块上下文，eval 默认记的是第一块：

| 层 | 代码 | eval 字段 | legacy | managed |
|----|------|-----------|--------|---------|
| 抽取 working context | `run_diagnosis` 里喂给二次抽取的 `extract_ctx` | `context_chars` / `evidence_recall` | `assemble_blocks`，观察预先压到 400 字 | `ContextManager`，观察不预压，装得下就留 |
| ReAct 发给模型的历史 | `run_agent_loop` 每次 `invoke` 前的 `compose()` | 目前 **没有**单独落进 jsonl；只间接体现在 `trimmed_steps` | `history_max_chars=0`，不裁 | 默认 20000 字按 step 裁 |

面试时如果说「上下文小了」，必须讲清楚是 **ReAct 历史** 还是 **抽取窗口**。把抽取 `context_chars` 变小当成 KPI，会跟改造目标对着干。

---

## 2. 当前 mock golden set（实测）

假模型轨迹对每条 case 都一样：`fetch_logs(tail=400)` 两次，再按场景 `grep` 一次，然后给结论。mock 日志约 480 行，ERROR 特征只有尾部几行。

### 2.1 逐条

| case_id | 策略 | context_chars | evidence_recall | llm_calls | trimmed_steps | 抽取是否触发压缩 |
|---------|------|---------------|-----------------|-----------|---------------|------------------|
| case_error_keyerror | legacy | 932 | 1.00 | 5 | 0 | 否 |
| case_error_keyerror | managed | **739** | 1.00 | 5 | 0 | 否 |
| version_fail_protocol_mismatch | legacy | 719 | 1.00 | 5 | 0 | 否 |
| version_fail_protocol_mismatch | managed | 772 | 1.00 | 5 | 0 | 否 |
| env_error_connection_refused | legacy | 729 | 1.00 | 5 | 0 | 否 |
| env_error_connection_refused | managed | 768 | 1.00 | 5 | 0 | 否 |
| env_error_logic_topology | legacy | 729 | 1.00 | 5 | 0 | 否 |
| env_error_logic_topology | managed | 774 | 1.00 | 5 | 0 | 否 |
| all_pass_no_failure | legacy | 1328 | 1.00 | 5 | 0 | 否 |
| all_pass_no_failure | managed | **4546** | 1.00 | 5 | 0 | 否 |
| case_error_multi_case | legacy | 932 | 1.00 | 5 | 0 | 否 |
| case_error_multi_case | managed | **719** | 1.00 | 5 | 0 | 否 |

### 2.2 均值

| 指标 | legacy | managed | 相对变化 | 面试怎么说 |
|------|--------|---------|----------|------------|
| accuracy | 1.00 | 1.00 | 0 | 假模型写死了 fail_kind，**无信息量** |
| evidence_recall | 1.00 | 1.00 | 0 | mock 证据就几行，两边都留住了；这是底线，不是成绩 |
| context_chars（抽取） | 895 | 1386 | **+55%** | 均值被 `all_pass` 拉高：无 ERROR 关键词时 legacy 走 head+tail 截成 ~400×N，managed 在预算内留了更完整的一份日志 |
| llm_calls | 5 | 5 | 0 | 4 次 ReAct + 1 次抽取；Compressor 没触发，所以没有摘要开销 |
| tool_calls | 3 | 3 | 0 | 轨迹写死了，策略不改变「想调几次工具」 |
| trimmed_steps | 0 | 0 | 0 | 三步工具调用远小于 20K 历史预算，裁剪未发生 |

### 2.3 这张表里唯一能当「改造有效」的数字

有 ERROR 特征、且同一份 `fetch_logs` 被取了两次时，去重生效：

- `case_error_keyerror`：932 → 739（**-21%**）
- `case_error_multi_case`：932 → 739（**-21%**）

`version_fail` / `env_error` 上 managed 略大（+6% 左右），因为多带了 immutable 的用户诉求块，而重复的 fetch 去重之后和 legacy 的双份 400 字摘录差不多长。

`all_pass` 是反例教材：没有 ERROR/FAIL 行，`compress_observation` 只能 head+tail。legacy 抽取窗口 1328 字；managed 认为预算还很宽（上限 12K），把一份更完整的日志留在 working context 里（4546 字）。**字符变多、证据还在**，这才是「别无脑先截」的本意。

---

## 3. ReAct 历史层（实测，不在 golden jsonl 里）

模拟 8 步 ReAct，每步一条约 1542 字、带 `ERROR` + `KeyError` 的观察（已过 `observation_max_chars=4000`，不再截）：

| `history_max_chars` | 最后一次发给模型的 context_chars | trimmed_steps | 峰值发送字符 | 假 input token 累计 |
|---------------------|----------------------------------|---------------|--------------|---------------------|
| 0（= legacy） | 22924 | 0 | 11059 | 11286 |
| 20000（= 当前 managed 默认） | 20120（**-12%**） | 1 | 9950 | 11009（-2%） |
| 8000（压预算看上限） | 8028（**-65%**） | 5 | 4638 | 6799（**-40%**） |

读法：

- 默认 20K 对「8 步 × 1.5K 观察」只裁掉最旧 1 步，差距不明显。
- 步数更多、单步观察接近 4K 时，未裁历史大约 `8 × 4K ≈ 32K`，会被 20K 顶住，最后一次 invoke **大约少 30%～40% 历史字符**。
- 配对不变量仍然成立：裁的是整步，不是单条 Message。

eval 的 `trimmed_steps` 在 golden 上全是 0，就是因为脚本只跑了 3 次工具。要在 jsonl 里看见这项，需要更长的真实 ReAct，或单独做上面这种历史压测。

---

## 4. 噪声变多之后会发生什么（压测，不是 golden）

在 mock 全文里插入 800 行 `ERROR timeout heartbeat`（插在尾部特征行之前），同一套假轨迹再跑一遍。

| 指标 | legacy | managed | 说明 |
|------|--------|---------|------|
| context_chars 均值 | 1247 | 4562 | 仍未触顶 12K；managed 继续「有余量就多留」 |
| evidence_recall 均值 | 0.92 | 0.92 | `case_error_multi_case` 两边都丢了 `AssertionError`（0.50） |
| Compressor | 未触发 | 未触发 | 抽取窗口还装得下 |

两边同时丢掉 `AssertionError` 的原因 **不在 ContextManager**：ReAct 写入 `ToolMessage` 前已经 `compress_observation(..., max_chars=4000)`。800 行 `ERROR ...` 都算证据行，截断从前往后切，真正的 `AssertionError` / 部分 `KeyError` 行排在后面，可能被这一层先丢掉。grep 到 `KeyError` 的 case 还能靠 grep 结果补回，只期望 `AssertionError` 的那条就两边一起挂。

这是面试里可以主动说的剩余缺口：**ContextManager 救不回 ReAct 观察层已经裁掉的东西。** 噪声全是 ERROR 时，`observation_max_chars` 的截断顺序比抽取策略更致命。

---

## 5. 预计：真实长日志上大概什么量级

没有公司真实日志，下面是按机制推的 **量级**，不是实测百分数。面试请用「大概」「取决于」而不是「提升了 47%」。

假设一次诊断 6～8 步，两次重复 `fetch_logs`，全文 5 万～20 万字，ERROR 行成百上千，抽取预算仍是 12K、历史预算 20K。

| 指标 | 预计 legacy | 预计 managed | 差距量级 | 可信度 |
|------|-------------|--------------|----------|--------|
| evidence_recall | 0.6～1.0，长尾巴根因容易被 400 字预压丢掉 | 同等或更好：去重后留一份更完整的；超预算才摘要，prompt 要求保留关键词 | **持平到 +0.1～+0.3**（若根因只出现在被 400 字截掉的那截，差距会明显） | 中：机制清楚，幅度看日志长什么样 |
| 抽取 context_chars | 大约 `400 × 观察条数`（3 条 ≈ 1.2K，8 条 ≈ 3K） | 接近 min(去重后全文, 12K)；重复 fetch 只计一份 | **常常是 managed 更大**（用满预算），重复多时相对 legacy 的「双份 400 字」可能更小 | 高 |
| ReAct 最后一次 context_chars | 随步数线性涨，8×4K 观察可到 3 万+ | 被 20K 封顶，旧 step compact | **-30%～-60%**（步数 ≥ 6 且观察接近 4K 时） | 高（第 3 节同构压测已见） |
| token_total | 只有 ReAct + 抽取 | 同上；**仅当抽取超预算**才多 1～4 次快模型摘要（大约 +200～+2000 token） | 历史变短可能让后期 ReAct **少 10%～40% input**；摘要触发时总量可能略升 | 中 |
| llm_calls | ≈ `步数 + 1`（抽取） | 超预算时 `+1～+4` | 常见路径 +0；压预算路径明显变多 | 高 |
| latency_ms | 基线 | 摘要触发时大约 +0.5～2s（快模型） | 用时间换「不丢原文」；历史变短后期步可能略快 | 低（网络抖动更大） |
| accuracy / fail_kind | 6 条样本上不要比 | 不要比 | **禁止讲提升了 X%** | — |

一句话对照：

- **mock 现在：** 证据留存打平；抽取窗口 managed 往往更大；去重在重复 fetch 上大约 **-20% 字符**；历史裁剪几乎看不到。
- **真实长日志 + 多步 ReAct：** 历史窗口大约 **-30%～-60%**；抽取窗口不再无脑 400 字，证据留存是主收益；token 不一定更省（摘要要记账）。

---

## 6. 健康 / 不健康的成绩单（对照真实 `eval-diagnose`）

以后跑 `python -m work_agent.cli eval-diagnose`（真 LLM）时，用这张表读，不要只看准确率。

| 信号 | 含义 |
|------|------|
| evidence_recall 不降，抽取 chars 升或持平 | 正常。预算内多留了原文。 |
| 重复 fetch 的 case 上 managed chars 下降约 15%～30% | 去重在工作。 |
| trimmed_steps > 0 且后期步的 token input 下降 | 历史裁剪在工作。 |
| evidence_recall 下降、chars 也下降 | 失败：省字符把根因一起省了。 |
| chars 几乎不变、llm_calls 却升 | 摘要在不该触发时触发了。 |
| accuracy 从 4/6 变成 5/6 | **不要当结论**。样本太小，模型抖动就能解释。 |

---

## 7. 面试 30 秒说法（建议背这个，不要背表）

> 我用同一套 golden set 对改造前后做了 A/B。当前 mock 日志太短，Compressor 基本不触发，两边 evidence_recall 都是 1.0，准确率我不会拿 6 条样本说提升。能测到的硬差有两块：同一份 fetch 取两次，managed 去重大约少 20% 抽取字符；ReAct 跑到 8 步、观察上千字时，按 step 裁历史，最后一次发给模型的上下文能少三成到六成，而且不会把 tool_call 和 tool 结果拆开。抽取阶段的字符数 managed 往往更大，因为旧逻辑每条观察先压到 400 字——那个数字好看，但是在没判断有没有余量之前就丢证据。真实长日志上我预期主收益是证据留存和历史封顶，不是准确率。

不要说的：

- 「上下文压缩让 token 省了一半」——mock 上 token 持平；真触发摘要时总量还可能升。
- 「准确率从 x 提到 y」——假模型或 6 条样本都撑不住。
- 「所有指标全面领先」——抽取 `context_chars` 均值现在就是落后的，而且这是故意的。
