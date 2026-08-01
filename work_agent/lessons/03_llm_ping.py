"""
在仓库根目录执行：

    python -m work_agent.lessons.03_llm_ping
"""

from langchain_core.messages import HumanMessage

from work_agent.core.config import get_settings
from work_agent.core.llm import get_chat_model


def main() -> None:
    s = get_settings()
    print("model   :", s.llm_model)
    print("base_url:", s.llm_base_url)

    llm = get_chat_model()
    resp = llm.invoke(
        [HumanMessage(content="只回复两个字：你好")]
    )
    print("reply   :", resp.content)


if __name__ == "__main__":
    main()