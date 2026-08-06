# 5G 基带测试分析模块

## 运行方式

默认使用仓库内的 Mock 资料：

```powershell
python -m work_agent.cli ask "支持业务态修改 PUCCH 资源，请生成测试分析"
```

接入真实资料库时无需复制资料，启动进程前设置根目录：

```powershell
$env:TEST_ANALYSIS_KNOWLEDGE_ROOT = "D:\real-data\5g-knowledge"
python -m work_agent.cli ask "你的真实需求"
```

可选限制：

```powershell
$env:TEST_ANALYSIS_MAX_TOOL_CALLS = "4"
$env:TEST_ANALYSIS_MAX_RETRIEVAL_ROUNDS = "2"
$env:TEST_ANALYSIS_TOP_K = "5"
```

修改环境变量后需要重启 CLI，使资料目录缓存重新构建。

## 资料目录识别

模块递归读取 `.md`、`.markdown` 和 `.txt`。目录名是第一层硬过滤：

```text
5g-knowledge/
├── 基础测试点资料库/
├── PUCCH/
├── PUSCH/
├── PRACH/
├── PDCCH/
└── PDSCH/
```

也支持 `01_PUCCH_上行控制信道` 这类带序号或中文说明的目录名。

当前可识别：

```text
PUCCH / PUSCH / PRACH / PDCCH / PDSCH
SRS / CSI-RS / SSB / PBCH / DMRS / PTRS
RRC / MAC
```

没有命中上述名称的目录按基础/通用资料处理。若真实目录使用其他别名，
在 `work_agent/analysis/corpus.py` 的 `CHANNEL_ALIASES` 中补一条映射即可。

## 检索 Tool

单领域研究子图注册两个只读 Tool：

- `search_basic_test_points`：检索基础测试点资料。
- `search_channel_knowledge`：检索当前 DomainTask 白名单内的信道资料。

模型看不到资料库绝对路径。`ToolNode` 从私有 State 注入允许信道，
非法信道查询会返回 `channel_not_allowed`。

每个领域默认最多 4 次 Tool 调用、2 轮充分性判断。父图只保存证据引用和
领域 JSON 文件，不保存 Tool 消息。

## 分析产物

```text
workspace/runs/<task_id>/analysis/
├── requirement.json
├── domain_plan.json
├── domains/
├── coverage.json
├── manifest.json
└── test_analysis.md
```

`test_analysis.md` 是最终报告；其他 JSON 用于审计模型拆解、检索证据和
覆盖修复过程。

## 当前边界

- 资料库读取支持 Markdown/TXT。
- 需求当前从 CLI 文本输入。
- PDF、Word、PPTX、图片 OCR 尚未接入。
- Mock 资料只用于验证工作流，不可作为正式产品判定依据。
- 产品门限、内部调度策略和错误码没有资料时必须标记为待确认。
