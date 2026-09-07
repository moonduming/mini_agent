CREATE TABLE IF NOT EXISTS logs (
    id BIGSERIAL PRIMARY KEY,
    log_type VARCHAR(50) NOT NULL,
    file_path TEXT NOT NULL,
    source VARCHAR(255),
    occurred_at TIMESTAMP(6) NOT NULL,
    pid INTEGER,
    level VARCHAR(16),
    logger TEXT,
    message TEXT,
    raw_log TEXT NOT NULL,
    is_error BOOLEAN NOT NULL DEFAULT FALSE,
    error_kind VARCHAR(32),
    error_label TEXT,
    error_template TEXT,
    error_fingerprint CHAR(64),
    classifier_version VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_logs_type_occurred_at
    ON logs (log_type, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_logs_level
    ON logs (level);

CREATE INDEX IF NOT EXISTS idx_logs_logger
    ON logs (logger);

CREATE INDEX IF NOT EXISTS idx_logs_error_summary
    ON logs (log_type, error_fingerprint, occurred_at DESC)
    WHERE is_error;
