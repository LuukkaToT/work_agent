-- 业务表。生产环境由部署迁移脚本先升级，再启动 API。
-- LangGraph checkpoint 表仍由 PostgresSaver.setup() 创建。

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

UPDATE pipelines SET user_id = 'local-dev' WHERE user_id = '';

CREATE TABLE IF NOT EXISTS user_config (
    user_id TEXT PRIMARY KEY,
    config TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-z][0-9]{8}$'),
    cn_name TEXT NOT NULL CHECK (btrim(cn_name) <> ''),
    email TEXT NOT NULL CHECK (btrim(email) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uq ON users (lower(email));

CREATE TABLE IF NOT EXISTS logic_topologies (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    constraint_value TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{"boards": []}'::jsonb,
    aliases TEXT[] NOT NULL DEFAULT '{}',
    UNIQUE (name, constraint_value),
    CHECK (btrim(name) <> ''),
    CHECK (btrim(constraint_value) <> ''),
    CHECK (jsonb_typeof(config) = 'object'),
    CHECK (jsonb_typeof(config->'boards') = 'array')
);

CREATE TABLE IF NOT EXISTS capacity_topology_mappings (
    id BIGSERIAL PRIMARY KEY,
    capacity_ids TEXT[] NOT NULL,
    logic_topology_id BIGINT NOT NULL REFERENCES logic_topologies(id),
    UNIQUE (capacity_ids, logic_topology_id),
    CHECK (cardinality(capacity_ids) > 0)
);

CREATE TABLE IF NOT EXISTS ci_cases (
    case_name TEXT PRIMARY KEY,
    physical_topology TEXT NOT NULL DEFAULT '',
    logic_topology_id BIGINT REFERENCES logic_topologies(id),
    owner TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS diagnosis_run (
    run_id TEXT PRIMARY KEY CHECK (length(btrim(run_id)) > 0),
    user_id VARCHAR(256) NOT NULL DEFAULT '',
    pipeline_id VARCHAR(256) NOT NULL CHECK (length(btrim(pipeline_id)) > 0),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'succeeded', 'failed', 'insufficient')),
    current_round INTEGER NOT NULL DEFAULT 0 CHECK (current_round >= 0),
    max_rounds INTEGER NOT NULL CHECK (max_rounds >= 1),
    max_tool_calls INTEGER NOT NULL CHECK (max_tool_calls >= 0),
    used_tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (used_tool_calls >= 0),
    orchestrator_ref TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS investigation_task (
    investigation_id TEXT PRIMARY KEY CHECK (length(btrim(investigation_id)) > 0),
    run_id TEXT NOT NULL REFERENCES diagnosis_run(run_id) ON DELETE CASCADE,
    round_index INTEGER NOT NULL CHECK (round_index >= 1),
    component VARCHAR(32) NOT NULL CHECK (length(btrim(component)) > 0),
    question TEXT NOT NULL CHECK (length(btrim(question)) > 0),
    log_scope JSONB NOT NULL CHECK (jsonb_typeof(log_scope) = 'object'),
    status TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'EXPIRED')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    owner_token TEXT NOT NULL DEFAULT '',
    lease_until DOUBLE PRECISION NOT NULL DEFAULT 0,
    execution_id TEXT NOT NULL DEFAULT '',
    result_ref TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
