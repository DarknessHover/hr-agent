-- HR 端招聘 Agent · 单表结构 v2
-- 设计原则：
--   1. 求职者信息全部存放在 candidate 一张表内，不用 JOIN。
--   2. 工作/项目/教育/社团/技能/荣誉等长文本区块以 TEXT 原样保存，系统不解析其内部结构。
--   3. 「牛人分析器」及其之后的页面内容不采集、不入库。
--   4. 岗位画像打分保留，作为本表内的列。
-- 事实源：docs/公共-求职者界面侦察文档-v0.3.md

PRAGMA foreign_keys = ON;
PRAGMA user_version = 3;

CREATE TABLE IF NOT EXISTS candidate (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 身份与去重
    platform                 TEXT NOT NULL DEFAULT 'boss_zhipin',
    platform_candidate_id    TEXT,
    fingerprint              TEXT UNIQUE,            -- 可见字段临时指纹；为空表示身份存疑
    display_name             TEXT NOT NULL,

    -- 基本资料（简历页头部）
    age                      INTEGER,
    work_status              TEXT,                  -- 离职-随时到岗
    active_status            TEXT,                  -- 刚刚活跃
    work_years               TEXT,                  -- 2年 / 26年应届生
    degree                   TEXT,                  -- 本科
    headline_subtitle        TEXT,                  -- 公司·职位 或 学校·专业
    summary                  TEXT,                  -- 个人优势/自述全文
    tags                     TEXT,                  -- 公办本科 / 已上传作品集

    -- 求职期望
    expect_role              TEXT,                  -- 期望职位，如「美工」
    expect_city              TEXT,
    expect_salary            TEXT,
    expect_industry          TEXT,

    -- 简历长文本区块（原样 TEXT，不结构化）
    work_experience_text     TEXT,
    project_experience_text  TEXT,
    education_text           TEXT,
    club_experience_text     TEXT,
    skills_text              TEXT,
    honors_text              TEXT,
    internship_text          TEXT,                  -- 实习经历（应届生常填）
    certificate_text         TEXT,                  -- 资格证书
    portfolio_image_text     TEXT,                  -- 图片作品
    portfolio_video_text     TEXT,                  -- 视频作品
    training_text            TEXT,                  -- 培训经历
    volunteer_text           TEXT,                  -- 志愿服务经历

    -- 首次招呼（唯一不可逆动作）
    greeting_phrase          TEXT,
    greeting_state           TEXT CHECK (greeting_state IN
                               ('pending', 'session_exists', 'sent', 'unknown', 'aborted')),
    greeting_planned_at      TEXT,
    greeting_boundary_at     TEXT,
    greeting_verified_at     TEXT,

    -- 岗位画像打分（保留）
    eval_job_profile_key     TEXT,
    eval_job_profile_version TEXT,
    eval_score               REAL,
    eval_threshold           REAL,
    eval_decision            TEXT CHECK (eval_decision IS NULL OR eval_decision IN
                               ('greet', 'do_not_greet', 'needs_review')),
    eval_reasons_text        TEXT,
    eval_evaluated_at        TEXT,
    eval_decision_source     TEXT,

    -- 采集与留证
    source_activity          TEXT,
    capture_version          INTEGER,
    content_hash             TEXT UNIQUE,           -- 本次可见快照内容哈希，重采去重
    raw_capture_json         TEXT,                  -- 原始可见文本/结构快照
    evidence_json            TEXT,                  -- 证据文件 [{path, sha256, type}]
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_candidate_display_name ON candidate(display_name);
CREATE INDEX IF NOT EXISTS ix_candidate_last_seen ON candidate(last_seen_at);
