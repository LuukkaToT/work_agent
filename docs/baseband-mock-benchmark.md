# 分层基带 Mock 日志 Benchmark

## 1. 目标与边界

这套数据专门测试 `error_analysis` 的受限 ReAct：Agent 是否会先识别日志分层，
再跨组件对齐时间线、区分根因与级联错误，并在没有 ERROR 的情况下从 DEBUG 状态机
判断握手是否闭环。

数据完全是 vendor-neutral synthetic 数据。组件名、IP、消息、错误码均为本仓库原创，
不代表华为或其他厂商的产品实现，也没有复制厂商私有日志。

## 2. 数据规模

| 指标 | 数值 |
| --- | ---: |
| Benchmark 场景 | 20 组 |
| 每组组件 | 6 个：COMM、RAT、BBH、BBL、MARP、COMPARE |
| `.log` 文件 | 120 个 |
| 总行数 | 132,579 行 |
| UTF-8 总大小 | 19,473,951 bytes（18.57 MiB） |
| 平均每文件 | 1,104.8 行 / 162,282.9 bytes |
| 平均每场景 | 6,629 行 / 0.93 MiB |
| 故障注入事件 | 99 行（占总日志 0.0747%） |
| Golden 证据关键词 | 60 个（每场景 3 个） |
| Synthetic 错误码 | 19 个：11 个根因码、8 个级联/路由码 |

99.9253% 的内容是正常 INFO/DEBUG 背景流量。故障事件位于同一时间窗，但分散在不同
组件文件里；默认尾部读取可以看到现象，精确归因仍需要组件过滤或 grep。

## 3. 场景覆盖

| ID | 核心场景 | 期望类型 | 根因组件 | 诊断难点 |
| --- | --- | --- | --- | --- |
| b01 | COMM connection refused | env | comm | BBL 只报依赖失败 |
| b02 | COMM SYN timeout | env | comm | 区分拒绝与网络超时 |
| b03 | COMM protocol mismatch | version | comm | TCP 成功不等于握手成功 |
| b04 | n78 120MHz 非法 | case | rat | RAT 首错、BBL 级联 |
| b05 | RAT worker/hugepage 不足 | env | rat | 配置通过后进程退出 |
| b06 | PTP 失锁 | env | bbh | BBL activation timeout 是现象 |
| b07 | eCPRI sequence gap | env | bbh | Compare KPI fail 是级联 |
| b08 | RX 订阅无 ACK | env | bbh | 全程没有 ERROR，只看 DEBUG 重试 |
| b09 | TX 路由未安装 | env | bbl | 双方状态不对称、没有 ERROR |
| b10 | duplicate PCI | case | bbl | 多小区配置冲突 |
| b11 | cell context pool 耗尽 | env | bbl | 运行时泄漏与容量证据 |
| b12 | VUE antenna_map 缺失 | case | marp | BBL errorcode 指向 MARP |
| b13 | beam calibration revision mismatch | version | marp | 校准表版本链 |
| b14 | numerology/SCS 非法 | case | rat | RAT→BBH→BBL 两级级联 |
| b15 | SyncE holdover 超限 | env | bbh | 请求受理但无 first slot |
| b16 | PRACH timing 超窗 | env | bbh | 小区 ACTIVE 但 UE 接入失败 |
| b17 | RRCSetupComplete 缺失 | env | bbl | RACH 成功，RRC 未闭环 |
| b18 | baseline schema mismatch | version | compare | 无线侧正常、比对版本错误 |
| b19 | KPI 回归源于前传丢包 | env | bbh | Compare 不是根因 |
| b20 | 订阅重试后恢复 | none | none | 防止把瞬态 WARN 误报成故障 |

分类分布为 `env=12`、`case=4`、`version=3`、`none=1`；根因组件分布为
`comm=3`、`rat=3`、`bbh=6`、`bbl=4`、`marp=2`、`compare=1`、`none=1`。
19 个失败场景中有 2 个 DEBUG-only 场景，占 10.5%。

## 4. 文件与数据契约

```text
work_agent/tools/mock/mock_5g_fault_kb/
├── dataset/
│   ├── benchmark_cases.json   # 场景、根因、事件与 golden 证据
│   └── error_codes.json       # 错误码、候选组件、是否根因码、下一步检查
└── logs/benchmark/
    └── benchNN_.../
        ├── comm.log
        ├── rat.log
        ├── bbh.log
        ├── bbl.log
        ├── marp.log
        └── compare.log
```

运行时会把静态文件中的 `{{PIPELINE_ID}}` 替换成真实 mock pipeline id。合并读取按
ISO 时间戳排序，单组件读取保留原文件顺序。

benchmark 的 `get_pipeline_status` 故意只返回通用失败，不返回 golden 的
`fail_kind`、`root_component` 或 `root_cause`，避免模型不读日志就拿到答案。

## 5. ReAct 工具

诊断白名单从原有 6 个扩为 8 个（启用 archive 时为 9 个）：

- `list_log_files(pipeline_id)`：查看六组件、行数与大小；
- `fetch_logs(pipeline_id, tail_lines, component)`：读单组件或合并时间线；
- `grep_logs(pipeline_id, pattern, component, ...)`：跨组件或定点正则检索；
- `lookup_error_code(code)`：查 probable components、根因码属性和建议检查；
- 原有状态、用例历史、用例规格、知识检索工具保持不变。

建议轨迹：

```text
get_pipeline_status
→ list_log_files
→ fetch_logs(component="")
→ grep_logs(pattern="errorcode=|LOAD_FAIL|request_subscribe")
→ fetch_logs(component="bbh" 或 probable_component)
→ lookup_error_code（仅作路由提示）
→ 回到原日志确认首错与闭环
```

## 6. 运行与再生成

只跑 managed 策略的 20 组：

```powershell
python -m work_agent.cli eval-diagnose --strategy managed --pause 1
```

对比 legacy 与 managed 会产生 `20 × 2 = 40` 条诊断记录：

```powershell
python -m work_agent.cli eval-diagnose --pause 1
```

单跑 DEBUG-only 场景：

```powershell
python -m work_agent.cli eval-diagnose `
  --case b08_rx_subscription_debug_stall `
  --strategy managed `
  --no-store
```

日志和 golden set 的可复现生成入口：

```powershell
python scripts/generate_mock_benchmark.py
```

## 7. 应关注的性能指标

| 指标 | 回答的问题 |
| --- | --- |
| `accuracy` / kind accuracy | `env/case/version/none` 是否判对 |
| `root_component_accuracy` | 是否越过级联报错找到首个故障组件 |
| `diagnosis_accuracy` | 类型与根因组件是否同时正确 |
| `evidence_recall` | 60 个 golden 关键词有多少活到最终 working context |
| `tool_calls` | 是否用较少步骤完成取证 |
| `latency_ms` | 整体诊断延迟 |
| `token_total` | ReAct + 二次抽取 + 压缩的总 token 成本 |
| `context_chars` | 最终抽取上下文大小 |
| `react_context_chars` | 最后一次 ReAct 调用携带的历史大小 |
| `trimmed_steps` / `archived_n` | 上下文压力下裁剪和外部归档是否生效 |

20 组适合做开发期回归和 A/B，不足以宣称生产准确率。上线前仍需用脱敏真实样本扩充，
并按版本、组网、根因组件和难度分层报告置信区间。

## 8. 公开资料依据

- [srsRAN Outputs](https://docs.srsran.com/projects/project/en/latest/user_manuals/source/outputs.html)
  公开说明了按层/组件配置日志，以及时间戳、Layer、Level、TTI 的日志结构；本数据据此采用
  可排序时间戳、组件和日志级别字段。
- [O-RAN SC O-DU High Overview](https://docs.o-ran-sc.org/projects/o-ran-sc-o-du-l2/en/latest/overview.html)
  描述了 F1 Setup、MAC/Lower-MAC 配置、START.request、slot indication 到 CELL UP 的跨模块过程；
  本 benchmark 据此构造前置失败与小区激活级联。
- [ETSI / 3GPP TS 38.470](https://www.etsi.org/deliver/etsi_ts/138400_138499/138470/18.04.00_60/ts_138470v180400p.pdf)
  说明 F1 setup、error indication、reset 和 cell activation/deactivation 等接口功能。
- [Huawei 1588v2 overview](https://info.support.huawei.com/enterprise/en/doc/EDOC1100412643/21135210/overview-of-1588v2)
  说明 IP RAN 的同步要求及 5G NR TDD 对高精度时间同步的依赖；这里只借鉴公开时钟故障语义，
  没有使用华为日志格式或错误码。

