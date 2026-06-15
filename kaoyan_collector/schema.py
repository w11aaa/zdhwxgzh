from __future__ import annotations

import sqlite3
from pathlib import Path


DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    keywords TEXT NOT NULL,
    crawler_type TEXT NOT NULL,
    save_data_path TEXT NOT NULL,
    source_file TEXT,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS content_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_type TEXT,
    title TEXT,
    content TEXT,
    summary TEXT,
    author_name TEXT,
    author_id TEXT,
    author_profile_url TEXT,
    publish_time TEXT,
    like_count INTEGER,
    comment_count INTEGER,
    share_count INTEGER,
    collect_count INTEGER,
    view_count INTEGER,
    source_url TEXT,
    cover_url TEXT,
    tags TEXT,
    source_keyword TEXT,
    is_relevant INTEGER NOT NULL DEFAULT 0,
    relevance_score INTEGER NOT NULL DEFAULT 0,
    relevance_label TEXT NOT NULL DEFAULT 'unknown',
    relevance_reason TEXT,
    raw_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(platform, source_id)
);

CREATE INDEX IF NOT EXISTS idx_content_platform ON content_items(platform);
CREATE INDEX IF NOT EXISTS idx_content_keyword ON content_items(source_keyword);
CREATE INDEX IF NOT EXISTS idx_content_publish_time ON content_items(publish_time);

CREATE TABLE IF NOT EXISTS gongkao_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_platform TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    region TEXT,
    category TEXT,
    fenbi_exam_type_id TEXT,
    fenbi_exam_type_name TEXT,
    org_name TEXT,
    job_count INTEGER,
    qualification TEXT,
    major_requirements TEXT,
    registration_start TEXT,
    registration_deadline TEXT,
    registration_deadline_time TEXT,
    exam_date TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    publish_time TEXT,
    source_url TEXT NOT NULL,
    article_url TEXT,
    source_origin_url TEXT,
    source_origin_text TEXT,
    source_origin_html TEXT,
    origin_search_status TEXT NOT NULL DEFAULT 'pending',
    origin_search_attempts INTEGER NOT NULL DEFAULT 0,
    origin_last_checked_at TEXT,
    summary TEXT,
    raw_text TEXT,
    raw_json TEXT NOT NULL,
    hash_id TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(source_platform, source_id),
    UNIQUE(hash_id)
);

CREATE INDEX IF NOT EXISTS idx_gongkao_status ON gongkao_events(status, registration_deadline);
CREATE INDEX IF NOT EXISTS idx_gongkao_category ON gongkao_events(category);
CREATE INDEX IF NOT EXISTS idx_gongkao_region ON gongkao_events(region);

CREATE TABLE IF NOT EXISTS gongkao_event_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_source_platform TEXT NOT NULL,
    event_source_id TEXT NOT NULL,
    attachment_scope TEXT NOT NULL,
    name TEXT,
    href TEXT,
    oldsrc TEXT,
    file_ext TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending',
    local_path TEXT,
    file_size INTEGER,
    content_type TEXT,
    parse_status TEXT NOT NULL DEFAULT 'metadata_only',
    parsed_text TEXT,
    parsed_json TEXT,
    error_message TEXT,
    raw_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    UNIQUE(event_source_platform, event_source_id, attachment_scope, oldsrc, href, name)
);

CREATE INDEX IF NOT EXISTS idx_gongkao_attachment_event ON gongkao_event_attachments(event_source_platform, event_source_id);

CREATE TABLE IF NOT EXISTS wechat_publish_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_platform TEXT,
    source_id TEXT,
    title TEXT NOT NULL,
    author TEXT,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    media_id TEXT,
    publish_id TEXT,
    article_id TEXT,
    html_path TEXT,
    cover_path TEXT,
    submit_publish INTEGER NOT NULL DEFAULT 0,
    raw_output TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wechat_records_status ON wechat_publish_records(status, created_at);
CREATE INDEX IF NOT EXISTS idx_wechat_records_media ON wechat_publish_records(media_id);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT UNIQUE,
    objective TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger_source TEXT,
    input_json TEXT NOT NULL DEFAULT '{}',
    final_output TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    elapsed_seconds REAL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, started_at);
CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id);

CREATE TABLE IF NOT EXISTS agent_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    tool_name TEXT NOT NULL,
    tool_args_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    observation TEXT,
    error_message TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    elapsed_seconds REAL,
    FOREIGN KEY(run_id) REFERENCES agent_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, step_index);
"""


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DDL)
        existing_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(content_items)").fetchall()
        }
        migrations = {
            "is_relevant": "ALTER TABLE content_items ADD COLUMN is_relevant INTEGER NOT NULL DEFAULT 0",
            "relevance_score": "ALTER TABLE content_items ADD COLUMN relevance_score INTEGER NOT NULL DEFAULT 0",
            "relevance_label": "ALTER TABLE content_items ADD COLUMN relevance_label TEXT NOT NULL DEFAULT 'unknown'",
            "relevance_reason": "ALTER TABLE content_items ADD COLUMN relevance_reason TEXT",
        }
        for column_name, sql in migrations.items():
            if column_name not in existing_columns:
                conn.execute(sql)
        # Defensive creation for databases initialized before new DDL blocks were added.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gongkao_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_platform TEXT NOT NULL,
                source_id TEXT NOT NULL,
                title TEXT NOT NULL,
                region TEXT,
                category TEXT,
                fenbi_exam_type_id TEXT,
                fenbi_exam_type_name TEXT,
                org_name TEXT,
                job_count INTEGER,
                qualification TEXT,
                major_requirements TEXT,
                registration_start TEXT,
                registration_deadline TEXT,
                registration_deadline_time TEXT,
                exam_date TEXT,
                status TEXT NOT NULL DEFAULT 'unknown',
                publish_time TEXT,
                source_url TEXT NOT NULL,
                article_url TEXT,
                source_origin_url TEXT,
                source_origin_text TEXT,
                source_origin_html TEXT,
                origin_search_status TEXT NOT NULL DEFAULT 'pending',
                origin_search_attempts INTEGER NOT NULL DEFAULT 0,
                origin_last_checked_at TEXT,
                summary TEXT,
                raw_text TEXT,
                raw_json TEXT NOT NULL,
                hash_id TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(source_platform, source_id),
                UNIQUE(hash_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_relevance ON content_items(is_relevant, relevance_score)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gongkao_status ON gongkao_events(status, registration_deadline)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gongkao_category ON gongkao_events(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gongkao_region ON gongkao_events(region)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gongkao_event_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_source_platform TEXT NOT NULL,
                event_source_id TEXT NOT NULL,
                attachment_scope TEXT NOT NULL,
                name TEXT,
                href TEXT,
                oldsrc TEXT,
                file_ext TEXT,
                parse_status TEXT NOT NULL DEFAULT 'metadata_only',
                parsed_text TEXT,
                raw_json TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(event_source_platform, event_source_id, attachment_scope, oldsrc, href, name)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gongkao_attachment_event ON gongkao_event_attachments(event_source_platform, event_source_id)"
        )
        attachment_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(gongkao_event_attachments)").fetchall()
        }
        attachment_migrations = {
            "download_status": "ALTER TABLE gongkao_event_attachments ADD COLUMN download_status TEXT NOT NULL DEFAULT 'pending'",
            "local_path": "ALTER TABLE gongkao_event_attachments ADD COLUMN local_path TEXT",
            "file_size": "ALTER TABLE gongkao_event_attachments ADD COLUMN file_size INTEGER",
            "content_type": "ALTER TABLE gongkao_event_attachments ADD COLUMN content_type TEXT",
            "parsed_json": "ALTER TABLE gongkao_event_attachments ADD COLUMN parsed_json TEXT",
            "error_message": "ALTER TABLE gongkao_event_attachments ADD COLUMN error_message TEXT",
        }
        for column_name, sql in attachment_migrations.items():
            if column_name not in attachment_columns:
                conn.execute(sql)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wechat_publish_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_platform TEXT,
                source_id TEXT,
                title TEXT NOT NULL,
                author TEXT,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                media_id TEXT,
                publish_id TEXT,
                article_id TEXT,
                html_path TEXT,
                cover_path TEXT,
                submit_publish INTEGER NOT NULL DEFAULT 0,
                raw_output TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wechat_records_status ON wechat_publish_records(status, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wechat_records_media ON wechat_publish_records(media_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT UNIQUE,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                trigger_source TEXT,
                input_json TEXT NOT NULL DEFAULT '{}',
                final_output TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                elapsed_seconds REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(status, started_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                step_index INTEGER NOT NULL,
                tool_name TEXT NOT NULL,
                tool_args_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL,
                observation TEXT,
                error_message TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                elapsed_seconds REAL,
                FOREIGN KEY(run_id) REFERENCES agent_runs(id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, step_index)")
        gongkao_columns = {row[1] for row in conn.execute("PRAGMA table_info(gongkao_events)").fetchall()}
        gongkao_migrations = {
            "source_origin_url": "ALTER TABLE gongkao_events ADD COLUMN source_origin_url TEXT",
            "source_origin_text": "ALTER TABLE gongkao_events ADD COLUMN source_origin_text TEXT",
            "source_origin_html": "ALTER TABLE gongkao_events ADD COLUMN source_origin_html TEXT",
            "origin_search_status": "ALTER TABLE gongkao_events ADD COLUMN origin_search_status TEXT NOT NULL DEFAULT 'pending'",
            "origin_search_attempts": "ALTER TABLE gongkao_events ADD COLUMN origin_search_attempts INTEGER NOT NULL DEFAULT 0",
            "origin_last_checked_at": "ALTER TABLE gongkao_events ADD COLUMN origin_last_checked_at TEXT",
            "fenbi_exam_type_id": "ALTER TABLE gongkao_events ADD COLUMN fenbi_exam_type_id TEXT",
            "fenbi_exam_type_name": "ALTER TABLE gongkao_events ADD COLUMN fenbi_exam_type_name TEXT",
            "registration_deadline_time": "ALTER TABLE gongkao_events ADD COLUMN registration_deadline_time TEXT",
        }
        for column_name, sql in gongkao_migrations.items():
            if column_name not in gongkao_columns:
                conn.execute(sql)
        conn.commit()
