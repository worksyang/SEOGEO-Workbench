ALTER TABLE contract_comparisons ADD COLUMN diff_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE contract_comparisons ADD COLUMN diffs_truncated INTEGER NOT NULL DEFAULT 0 CHECK(diffs_truncated IN (0, 1));

CREATE TABLE contract_comparison_diffs (
    diff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    comparison_id TEXT NOT NULL,
    json_pointer TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('added', 'removed', 'changed')),
    legacy_value_json TEXT NOT NULL DEFAULT 'null' CHECK(json_valid(legacy_value_json)),
    hub_value_json TEXT NOT NULL DEFAULT 'null' CHECK(json_valid(hub_value_json)),
    severity TEXT NOT NULL DEFAULT 'error',
    rule TEXT NOT NULL DEFAULT 'default',
    truncated INTEGER NOT NULL DEFAULT 0 CHECK(truncated IN (0, 1)),
    FOREIGN KEY(comparison_id) REFERENCES contract_comparisons(comparison_id) ON DELETE CASCADE
);

CREATE INDEX ix_contract_comparison_diffs_comparison
ON contract_comparison_diffs(comparison_id, json_pointer);
