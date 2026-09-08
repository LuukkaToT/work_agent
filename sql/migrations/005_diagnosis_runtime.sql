-- 最小 Durable Investigation control plane：任务租约与结果引用，不含报告全文。
CREATE TABLE IF NOT EXISTS diagnosis_run (
    run_id TEXT PRIMARY KEY CHECK (length(btrim(run_id)) > 0),
    user_id VARCHAR(256) NOT NULL DEFAULT '' ,
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

COMMENT ON TABLE diagnosis_run IS '一次并行诊断的 control plane：预算与轮次，不含组件报告原文';
COMMENT ON COLUMN diagnosis_run.used_tool_calls IS '已消耗工具次数，恢复时不重置';
COMMENT ON COLUMN diagnosis_run.orchestrator_ref IS '可选的主上下文 artifact 引用，不是调查结果';

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

CREATE INDEX IF NOT EXISTS idx_investigation_task_run_id
    ON investigation_task (run_id, round_index);

CREATE INDEX IF NOT EXISTS idx_investigation_task_lease
    ON investigation_task (status, lease_until);

COMMENT ON TABLE investigation_task IS '组件调查任务：谁该跑、租约是谁的；报告原文只经 result_ref 回读';
COMMENT ON COLUMN investigation_task.execution_id IS '本 attempt 的 transcript 执行 ID，过期重试必须换新';
COMMENT ON COLUMN investigation_task.result_ref IS '组件 transcript 内 put_json({report, evidences}) 的 artifact_id';
COMMENT ON COLUMN investigation_task.owner_token IS 'claim 写入，finish 用 CAS 校验；0 行表示晚到结果丢弃';
