-- Immutable task-scoped diagnosis content. No process-local manifest is required.
CREATE TABLE IF NOT EXISTS diagnosis_archive (
    task_id UUID NOT NULL,
    artifact_id TEXT NOT NULL,
    content TEXT NOT NULL,
    chars BIGINT NOT NULL CHECK (chars >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, artifact_id),
    CHECK (artifact_id ~ '^sha256_[0-9a-f]{64}$')
);
