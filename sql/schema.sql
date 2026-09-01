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
