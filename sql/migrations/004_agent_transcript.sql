-- 完整作用域隔离的事件与正文存储，闲置过期记录保留且不能隐式复活
CREATE TABLE IF NOT EXISTS agent_transcript_streams (
    stream_id TEXT PRIMARY KEY CHECK (stream_id ~ '^[0-9a-f]{64}$'),
    user_id VARCHAR(256) NOT NULL CHECK (length(btrim(user_id)) > 0),
    task_id VARCHAR(256) NOT NULL CHECK (length(btrim(task_id)) > 0),
    agent_id VARCHAR(256) NOT NULL CHECK (length(btrim(agent_id)) > 0),
    execution_id VARCHAR(256) NOT NULL CHECK (length(btrim(execution_id)) > 0),
    last_seq BIGINT NOT NULL DEFAULT 0 CHECK (last_seq >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE (user_id, task_id, agent_id, execution_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_transcript_streams_expires_at
    ON agent_transcript_streams (expires_at);

COMMENT ON TABLE agent_transcript_streams IS '每个用户、任务、代理、执行轮次独立一行，写入仅锁本行';
COMMENT ON COLUMN agent_transcript_streams.expires_at IS '默认闲置三十天到期，每次成功写入刷新，读取不刷新，不自动删除';
COMMENT ON COLUMN agent_transcript_streams.last_seq IS '当前流最后事件序号，只在新增事件的事务内递增';

CREATE TABLE IF NOT EXISTS agent_transcript_events (
    stream_id TEXT NOT NULL REFERENCES agent_transcript_streams(stream_id) ON DELETE CASCADE,
    event_id VARCHAR(256) NOT NULL CHECK (length(btrim(event_id)) > 0),
    seq BIGINT NOT NULL CHECK (seq > 0),
    kind VARCHAR(64) NOT NULL CHECK (length(btrim(kind)) > 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (stream_id, event_id),
    UNIQUE (stream_id, seq)
);

COMMENT ON TABLE agent_transcript_events IS '不可变小型事件，应用限制脱敏前后紧凑 JSON 均不超过 65536 字节，大正文另存引用';
COMMENT ON COLUMN agent_transcript_events.payload_sha256 IS '脱敏前原始 JSON 的稳定摘要，仅用于检测同标识不同内容，不保存原始秘密';

CREATE TABLE IF NOT EXISTS agent_transcript_artifacts (
    stream_id TEXT NOT NULL REFERENCES agent_transcript_streams(stream_id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL CHECK (artifact_id ~ '^sha256_[0-9a-f]{64}$'),
    content TEXT NOT NULL,
    chars BIGINT NOT NULL CHECK (chars >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (stream_id, artifact_id),
    CHECK (chars = char_length(content))
);

COMMENT ON TABLE agent_transcript_artifacts IS '完整脱敏正文，以作用域和正文 UTF-8 内容摘要寻址，不裁剪、不覆盖';
