"""
公司 SDK / 客户端 SDK 的放置处（不进 Agent 核心依赖图）。

约定：
- 这里只放「公司给什么就放什么」：sdk、client、原始 DTO。
- Agent 图与节点 **禁止** import external。
- 映射写在 work_agent/tools/real/：把公司 API 转成 Protocol（create/start/query）。

接真实系统时：
1. 把公司包放进本目录（或 pip 安装后在 real 里 import）
2. `.env` 设 `TOOL_BACKEND=real`，并填 `PIPELINE_API_BASE_URL` / `PIPELINE_API_TOKEN`（或用户名密码）
3. 对照 `pipeline_create.sample.json`，改 `work_agent/tools/real/pipeline_payload.py` 的请求体字段
4. 改 `pipeline_client.py` 里带 `COMPANY_REPLACE` 的鉴权 / HTTP 函数（或换成 SDK）
5. `RealPipelineTool` 只做编排，一般不用改
"""
