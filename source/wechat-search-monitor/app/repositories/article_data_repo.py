from __future__ import annotations

import json
from pathlib import Path
from threading import Lock


_WRITE_LOCK = Lock()


class ArticleDataRepository:
    def __init__(self, articles_path: Path) -> None:
        self.articles_path = Path(articles_path)

    def load(self) -> list[dict]:
        if not self.articles_path.exists():
            raise FileNotFoundError(f"articles data not found: {self.articles_path}")
        return json.loads(self.articles_path.read_text(encoding="utf-8"))

    def find_by_id(self, article_id: str) -> dict | None:
        for item in self.load():
            if item.get("article_id") == article_id:
                return item
        return None

    def update_cover_url(self, article_id: str, cover_url: str | None) -> dict:
        with _WRITE_LOCK:
            data = self.load()
            target = None
            for item in data:
                if item.get("article_id") != article_id:
                    continue
                item["cover_url"] = cover_url
                target = item
                break
            if target is None:
                raise FileNotFoundError(f"article not found: {article_id}")
            self.articles_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return target

    def update_metrics(
        self,
        article_id: str,
        *,
        read_count: int | None = None,
        like_count: int | None = None,
        friends_follow_count: int | None = None,
        original_article_count: int | None = None,
    ) -> dict:
        with _WRITE_LOCK:
            data = self.load()
            target = None
            for item in data:
                if item.get("article_id") != article_id:
                    continue
                item["read_count"] = read_count
                item["like_count"] = like_count
                item["friends_follow_count"] = friends_follow_count
                item["original_article_count"] = original_article_count
                target = item
                break
            if target is None:
                raise FileNotFoundError(f"article not found: {article_id}")
            self.articles_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return target