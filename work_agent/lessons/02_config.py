"""
在仓库根目录执行：

    python -m work_agent.lessons.02_config
"""

from work_agent.core.config import get_settings


def main() -> None:
    s = get_settings()
    print("project root via profile_path:", s.profile_path.parent.parent)
    print("llm_base_url :", s.llm_base_url)
    print("llm_model    :", s.llm_model)
    print("llm_api_key  :", (s.llm_api_key[:6] + "...") if s.llm_api_key else "(empty)")
    print("tool_backend :", s.tool_backend)
    print("workspace    :", s.workspace_dir)
    print("profile      :", s.profile)


if __name__ == "__main__":
    main()