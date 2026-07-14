CREATE VIEW v_content_latest_metrics AS
WITH ranked AS (
    SELECT
        subject_type,
        subject_id,
        metric_key,
        observed_at,
        numeric_value,
        text_value,
        snapshot_id,
        ROW_NUMBER() OVER (
            PARTITION BY subject_type, subject_id, metric_key
            ORDER BY observed_at DESC, observation_id DESC
        ) AS row_number
    FROM metric_observations
)
SELECT
    subject_type,
    subject_id,
    metric_key,
    observed_at,
    numeric_value,
    text_value,
    snapshot_id
FROM ranked
WHERE row_number = 1;

CREATE VIEW v_new_keyword_entries AS
SELECT
    date(captured_at) AS snapshot_date,
    platform,
    keyword,
    COUNT(*) AS snapshot_count,
    MIN(captured_at) AS first_snapshot_at
FROM search_snapshots
GROUP BY date(captured_at), platform, keyword;

CREATE VIEW v_rank_changes AS
WITH ranked_hits AS (
    SELECT
        s.platform,
        s.keyword,
        s.captured_at,
        h.content_id,
        h.rank,
        LAG(h.rank) OVER (
            PARTITION BY s.platform, s.keyword, h.content_id
            ORDER BY s.captured_at
        ) AS previous_rank
    FROM search_hits h
    JOIN search_snapshots s ON s.snapshot_id = h.snapshot_id
    WHERE h.content_id IS NOT NULL
)
SELECT
    platform,
    keyword,
    captured_at,
    content_id,
    rank,
    previous_rank,
    CASE WHEN previous_rank IS NULL THEN NULL ELSE previous_rank - rank END AS rank_delta
FROM ranked_hits;

CREATE VIEW v_daily_overview AS
SELECT
    date('now') AS report_date,
    (SELECT COUNT(*) FROM contents) AS content_count,
    (SELECT COUNT(*) FROM creators) AS creator_count,
    (SELECT COUNT(*) FROM search_snapshots) AS snapshot_count,
    (SELECT COUNT(*) FROM metric_observations) AS observation_count,
    (SELECT COUNT(*) FROM geo_answers) AS geo_answer_count,
    (SELECT COUNT(*) FROM signals WHERE status = 'new') AS new_signal_count,
    (SELECT COUNT(*) FROM production_jobs WHERE status IN ('queued', 'running')) AS active_job_count;
