"""
真实 SDK / 客户端 SDK 的放置处（不进 Agent 核心依赖图）。

约定：
- 这里只放真实实现提供的原始内容：SDK、client、原始 DTO。
- Agent 图与节点 **禁止** import external。
- 映射写在 work_agent/tools/real/：把真实 API 转成 Protocol（create/start/query）。

接真实系统时：
1. 把真实实现依赖包放进本目录（或 pip 安装后在 real 里 import）
2. 在 tools/real/pipeline.py 实现 RealPipelineTool
3. .env 设 TOOL_BACKEND=real
"""
