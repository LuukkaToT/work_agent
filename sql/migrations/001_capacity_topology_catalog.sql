-- 目录模型切换为：逻辑组网(name + constraint + JSON config)、容量 ID 映射、新 CI 字段。
-- 已确认旧目录/CI 数据允许重建；运行态流水线、用户配置和 checkpoint 不受影响。

DROP TABLE IF EXISTS capacity_topology_mappings;
DROP TABLE IF EXISTS ci_cases;
DROP TABLE IF EXISTS logic_topologies;

CREATE TABLE logic_topologies (
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

CREATE TABLE capacity_topology_mappings (
    id BIGSERIAL PRIMARY KEY,
    capacity_ids TEXT[] NOT NULL,
    logic_topology_id BIGINT NOT NULL REFERENCES logic_topologies(id),
    UNIQUE (capacity_ids, logic_topology_id),
    CHECK (cardinality(capacity_ids) > 0)
);

CREATE TABLE ci_cases (
    case_name TEXT PRIMARY KEY,
    physical_topology TEXT NOT NULL DEFAULT '',
    logic_topology_id BIGINT REFERENCES logic_topologies(id),
    owner TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY CHECK (id ~ '^[a-z][0-9]{8}$'),
    cn_name TEXT NOT NULL CHECK (btrim(cn_name) <> ''),
    email TEXT NOT NULL CHECK (btrim(email) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_uq ON users (lower(email));
