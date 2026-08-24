-- 业务表。新环境执行：python -m work_agent init-db
-- LangGraph checkpoint 三张表由 PostgresSaver.setup() 在同一条 CLI 里创建，不写在这里。

CREATE TABLE IF NOT EXISTS pipelines (
    pipeline_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    case_names TEXT NOT NULL,
    version TEXT NOT NULL,
    env TEXT NOT NULL,
    status TEXT NOT NULL,
    report_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT ''
);

-- 身份接线前留下的空 user_id，归到 CLI 未配工号时的默认身份。幂等。
UPDATE pipelines SET user_id = 'local-dev' WHERE user_id = '';

CREATE TABLE IF NOT EXISTS user_config (
    user_id TEXT PRIMARY KEY,
    config TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ci_cases (
    case_path TEXT PRIMARY KEY,
    logic_env TEXT NOT NULL DEFAULT '',
    logic_constraint TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT ''
);

-- 逻辑组网目录：校验白名单 + HITL 候选。种子由 init-db 从 config/logic_topologies.csv upsert。
CREATE TABLE IF NOT EXISTS logic_topologies (
    logic_env TEXT NOT NULL,
    logic_constraint TEXT NOT NULL,
    bbh_count INTEGER NOT NULL,
    bbl_count INTEGER NOT NULL,
    bbh_board TEXT NOT NULL DEFAULT '',
    bbl_board TEXT NOT NULL DEFAULT '',
    aliases TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (logic_env, logic_constraint)
);
