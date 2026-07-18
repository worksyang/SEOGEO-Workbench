"""微信文章正文互动指标解析与批量回填。

解析规则沿用旧系统 ``metrics_parser.py``，但这里不依赖旧服务：
Markdown 解析是纯函数，SQLite 写入集中在 ``WechatArticleMetricsService``。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from content_hub.db.connection import connect, transaction
from content_hub.db.writer_lock import writer_lock


_RE_READ = re.compile(r"阅读(\d+)")
_RE_FRIENDS_FOLLOW = re.compile(r"(\d+)个朋友关注")
_RE_IN_SIGHT = re.compile(r"在看(\d+)")
_RE_ZAN = re.compile(r"(?:赞|点赞)(\d+)")
_RE_GUANZHU_RECOMMEND = re.compile(r"关注(\d+)推荐")
_RE_ORIGINAL = re.compile(r"(?<![A-Za-z\d_])(\d{1,5})篇原创内容")

METRIC_KEYS = {
    "read_count": "wechat.article.read_count",
    "like_count": "wechat.article.like_count",
    "friends_follow_count": "wechat.article.friends_follow_count",
    "original_article_count": "wechat.article.original_article_count",
}

_METRIC_DEFINITIONS = (
    ("wechat.article.read_count", "微信搜一搜", "content", "微信阅读", "微信公众号累计阅读数"),
    ("wechat.article.like_count", "微信搜一搜", "content", "微信点赞", "微信点赞（好看）"),
    ("wechat.article.friends_follow_count", "微信搜一搜", "content", "朋友在看", "微信朋友在看数"),
    ("wechat.article.original_article_count", "微信搜一搜", "content", "微信原创", "公众号原创文章数"),
)


@dataclass(frozen=True, slots=True)
class ArticleMetrics:
    read_count: int | None = None
    like_count: int | None = None
    friends_follow_count: int | None = None
    original_article_count: int | None = None

    def present(self) -> dict[str, int]:
        """只返回正文中真实出现的指标，绝不把缺失值变成 0。"""
        return {
            key: value
            for key, value in (
                ("read_count", self.read_count),
                ("like_count", self.like_count),
                ("friends_follow_count", self.friends_follow_count),
                ("original_article_count", self.original_article_count),
            )
            if value is not None
        }


def parse_article_metrics(text: str) -> ArticleMetrics:
    """按旧系统底部口径从 Markdown 文本提取文章指标。"""
    text = str(text or "")
    end_idx = text.rfind("EndFragment")
    if end_idx == -1:
        footer = text[-800:]
        pre_footer = text[-3000:-800] if len(text) > 3000 else ""
    else:
        hash_idx = text.rfind("#", max(0, end_idx - 500), end_idx)
        if hash_idx == -1:
            footer = text[max(0, end_idx - 500) : end_idx]
            pre_footer = text[max(0, end_idx - 3000) : max(0, end_idx - 500)]
        else:
            footer = text[hash_idx:end_idx]
            pre_footer = text[max(0, hash_idx - 2000) : hash_idx]

    def last_int(pattern: Any, value: str) -> int | None:
        matches = pattern.findall(value)
        return int(matches[-1]) if matches else None

    like_count = last_int(_RE_ZAN, footer)
    if like_count is None:
        like_count = last_int(_RE_GUANZHU_RECOMMEND, footer)
    return ArticleMetrics(
        read_count=last_int(_RE_READ, pre_footer),
        like_count=like_count,
        friends_follow_count=(
            last_int(_RE_FRIENDS_FOLLOW, footer)
            if last_int(_RE_FRIENDS_FOLLOW, footer) is not None
            else last_int(_RE_IN_SIGHT, footer)
        ),
        original_article_count=last_int(_RE_ORIGINAL, pre_footer),
    )


extract_metrics = parse_article_metrics


def parse_article_metrics_file(path: str | Path) -> ArticleMetrics:
    """容错读取 Markdown；不可读文件视为无指标。"""
    try:
        return parse_article_metrics(Path(path).read_text(encoding="utf-8", errors="ignore"))
    except (OSError, UnicodeError):
        return ArticleMetrics()


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _observation_id(subject_id: str, metric_key: str, observed_at: str, source_ref: str) -> str:
    digest = hashlib.sha256(f"{subject_id}|{metric_key}|{observed_at}|{source_ref}".encode()).hexdigest()[:32]
    return f"obs_{digest}"


class WechatArticleMetricsService:
    """把 ``asset_store/wechat/*.md`` 中的文章指标写入 Hub。"""

    def __init__(self, settings: Any):
        self.settings = settings

    def backfill_contents(
        self,
        *,
        observed_at: str | None = None,
        source_ref: str | None = None,
        content_ids: Iterable[str] | None = None,
    ) -> dict[str, int]:
        """扫描现有微信 contents；可按 content_id 限定范围，重复执行幂等。"""
        at = observed_at or _now()
        wanted = tuple(dict.fromkeys(str(item) for item in (content_ids or ()) if str(item)))
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    self._ensure_metric_definitions(con)
                    return self._backfill_in_connection(
                        con, observed_at=at, source_ref=source_ref, content_ids=wanted
                    )

    def _backfill_in_connection(
        self,
        connection: Any,
        *,
        observed_at: str,
        source_ref: str | None = None,
        content_ids: Iterable[str] | None = None,
    ) -> dict[str, int]:
        """回填到调用者已打开的连接；不获取锁、不提交事务。"""
        wanted = tuple(dict.fromkeys(str(item) for item in (content_ids or ()) if str(item)))
        where = [
            """(
                c.content_type='wechat_article'
                OR EXISTS (
                    SELECT 1 FROM content_identifiers wi
                    WHERE wi.namespace='wechat_article' AND wi.content_id=c.content_id
                )
                OR EXISTS (
                    SELECT 1 FROM content_discoveries wd
                    WHERE wd.discovery_system='wechat-search' AND wd.content_id=c.content_id
                )
                OR EXISTS (
                    SELECT 1
                    FROM search_hits wh
                    JOIN search_snapshots ws ON ws.snapshot_id=wh.snapshot_id
                    WHERE wh.content_id=c.content_id AND ws.platform='wechat-search'
                )
            )""",
            "c.md_path IS NOT NULL",
            "c.md_path <> ''",
        ]
        params: list[Any] = []
        if wanted:
            where.append(f"c.content_id IN ({','.join('?' for _ in wanted)})")
            params.extend(wanted)
        rows = connection.execute(
            f"""SELECT c.content_id,c.md_path,c.payload_json
                FROM contents c
                WHERE {' AND '.join(where)}
                ORDER BY c.content_id""",
            params,
        ).fetchall()
        stats = {"scanned": 0, "with_metrics": 0, "updated": 0, "observations": 0, "skipped": 0}
        for row in rows:
            stats["scanned"] += 1
            metrics = parse_article_metrics_file(self._asset_path(row["md_path"]))
            if not metrics.present():
                stats["skipped"] += 1
                continue
            stats["with_metrics"] += 1
            ref = source_ref or f"asset:{Path(row['md_path']).as_posix()}"
            stats["observations"] += self._write_one(
                connection, content_id=row["content_id"], payload_raw=row["payload_json"],
                metrics=metrics, observed_at=observed_at, source_ref=ref,
            )
            stats["updated"] += 1
        return stats

    def backfill_file(
        self,
        *,
        content_id: str,
        path: str | Path,
        observed_at: str | None = None,
        source_ref: str | None = None,
    ) -> dict[str, int]:
        """回填单篇文章，供刷新链路或专项任务复用。"""
        metrics = parse_article_metrics_file(path)
        present = metrics.present()
        if not present:
            return {"scanned": 1, "with_metrics": 0, "updated": 0, "observations": 0, "skipped": 1}
        at = observed_at or _now()
        ref = source_ref or f"asset:{Path(path).as_posix()}"
        with writer_lock(self.settings.lock_path):
            with connect(self.settings) as con:
                with transaction(con):
                    self._ensure_metric_definitions(con)
                    row = con.execute("SELECT payload_json FROM contents WHERE content_id=?", (content_id,)).fetchone()
                    if row is None:
                        raise ValueError(f"微信文章不存在：{content_id}")
                    observations = self._write_one(
                        con, content_id=content_id, payload_raw=row["payload_json"],
                        metrics=metrics, observed_at=at, source_ref=ref,
                    )
                    return {"scanned": 1, "with_metrics": 1, "updated": 1, "observations": observations, "skipped": 0}

    def _asset_path(self, md_path: str) -> Path:
        root = Path(self.settings.asset_store_path).resolve()
        candidate = Path(md_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
        candidate.relative_to(root)
        return candidate

    @staticmethod
    def _ensure_metric_definitions(con: Any) -> None:
        for key, platform, subject_type, display_name, description in _METRIC_DEFINITIONS:
            con.execute(
                """INSERT OR IGNORE INTO metric_definitions(
                    metric_key,platform,subject_type,display_name,value_type,unit,accumulation_mode,description
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (key, platform, subject_type, display_name, "number", "次", "counter", description),
            )

    @staticmethod
    def _write_one(
        con: Any, *, content_id: str, payload_raw: Any, metrics: ArticleMetrics,
        observed_at: str, source_ref: str,
    ) -> int:
        try:
            payload = json.loads(payload_raw or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        for key, value in metrics.present().items():
            payload[key] = value
        con.execute(
            "UPDATE contents SET payload_json=?,updated_at=? WHERE content_id=?",
            (_json(payload), observed_at, content_id),
        )
        count = 0
        for field, value in metrics.present().items():
            metric_key = METRIC_KEYS[field]
            con.execute(
                """INSERT INTO metric_observations(
                    observation_id,subject_type,subject_id,metric_key,observed_at,
                    numeric_value,source_ref,payload_json
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(subject_type,subject_id,metric_key,observed_at,COALESCE(snapshot_id,'no-snapshot'))
                DO UPDATE SET numeric_value=excluded.numeric_value,source_ref=excluded.source_ref,
                              payload_json=excluded.payload_json""",
                (
                    _observation_id(content_id, metric_key, observed_at, source_ref),
                    "content", content_id, metric_key, observed_at, value, source_ref,
                    _json({"parser": "legacy.metrics_parser", "field": field}),
                ),
            )
            count += 1
        return count


def backfill_wechat_article_metrics(
    settings: Any, *, observed_at: str | None = None, source_ref: str | None = None,
    content_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    """函数式入口，便于脚本和主代理调用。"""
    return WechatArticleMetricsService(settings).backfill_contents(
        observed_at=observed_at, source_ref=source_ref, content_ids=content_ids
    )


def backfill(
    connection: Any,
    settings: Any,
    observed_at: str | None = None,
    source_ref: str | None = None,
    content_ids: Iterable[str] | None = None,
) -> dict[str, int]:
    """事务感知入口，供刷新链路在已有 SQLite transaction 内调用。"""
    service = WechatArticleMetricsService(settings)
    service._ensure_metric_definitions(connection)
    return service._backfill_in_connection(
        connection,
        observed_at=observed_at or _now(),
        source_ref=source_ref,
        content_ids=content_ids,
    )
