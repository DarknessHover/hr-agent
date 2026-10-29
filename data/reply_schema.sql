-- HR 端招聘 Agent · 新招呼回复模块 v1
--
-- 本文件只定义回复链路专属表。候选人简历继续复用 data/schema.sql
-- 中的 candidate 表，但运行时写入独立数据库 reply_agent.sqlite3，因而不会
-- 与主动招呼库 hr_agent_v2.sqlite3 混用。

PRAGMA foreign_keys = ON;
PRAGMA user_version = 2;

CREATE TABLE IF NOT EXISTS reply_conversation (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id             INTEGER NOT NULL REFERENCES candidate(id)
                               ON DELETE CASCADE,
    platform                 TEXT NOT NULL DEFAULT 'boss_zhipin',
    platform_conversation_id TEXT,
    conversation_fingerprint TEXT NOT NULL UNIQUE,
    list_display_name        TEXT NOT NULL,
    job_title                TEXT,
    unread_count_before_open INTEGER,
    unread_signal            TEXT,
    source_activity          TEXT,
    evidence_json            TEXT,
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_message (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id          INTEGER NOT NULL REFERENCES reply_conversation(id)
                               ON DELETE CASCADE,
    platform_message_id      TEXT,
    message_fingerprint      TEXT NOT NULL UNIQUE,
    visible_time_text        TEXT,
    body_text                TEXT NOT NULL,
    direction                TEXT NOT NULL DEFAULT 'inbound'
                               CHECK (direction = 'inbound'),
    evidence_json            TEXT,
    observed_at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reply_action (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id          INTEGER NOT NULL REFERENCES reply_conversation(id)
                               ON DELETE CASCADE,
    inbound_message_id       INTEGER NOT NULL UNIQUE REFERENCES inbound_message(id)
                               ON DELETE CASCADE,
    idempotency_key          TEXT NOT NULL UNIQUE,
    profile_key              TEXT,
    profile_version          TEXT,
    decision                 TEXT NOT NULL
                               CHECK (decision IN
                                 ('request_resume', 'reject', 'needs_review')),
    decision_reason          TEXT NOT NULL,
    phrase_key               TEXT,
    phrase_text              TEXT,
    state                    TEXT NOT NULL DEFAULT 'planned'
                               CHECK (state IN
                                 ('planned', 'pending', 'sent', 'unknown',
                                  'needs_review')),
    planned_at               TEXT NOT NULL,
    send_boundary_at         TEXT,
    verified_at              TEXT,
    evidence_json            TEXT
);

CREATE TABLE IF NOT EXISTS reply_send_attempt (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    reply_action_id          INTEGER NOT NULL REFERENCES reply_action(id)
                               ON DELETE CASCADE,
    attempt_no               INTEGER NOT NULL CHECK (attempt_no >= 1),
    phrase_key               TEXT NOT NULL,
    phrase_text              TEXT NOT NULL,
    state                    TEXT NOT NULL
                               CHECK (state IN ('pending', 'sent', 'unknown')),
    send_boundary_at         TEXT NOT NULL,
    verified_at              TEXT,
    evidence_json            TEXT,
    resolution_note          TEXT,
    retry_authorized_at      TEXT,
    UNIQUE (reply_action_id, attempt_no)
);

CREATE INDEX IF NOT EXISTS ix_reply_conversation_candidate
    ON reply_conversation(candidate_id);
CREATE INDEX IF NOT EXISTS ix_inbound_message_conversation
    ON inbound_message(conversation_id);
CREATE INDEX IF NOT EXISTS ix_reply_action_conversation
    ON reply_action(conversation_id);
CREATE INDEX IF NOT EXISTS ix_reply_send_attempt_action
    ON reply_send_attempt(reply_action_id);
