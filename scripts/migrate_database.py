"""部署流水线入口：在启动 API 前执行版本化数据库迁移。"""

from work_agent.core.config import get_settings
from work_agent.core.db_init import migrate_database


def main() -> None:
    migrate_database(get_settings().postgres_dsn, setup_checkpointer=True)


if __name__ == "__main__":
    main()
