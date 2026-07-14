"""统一内容工作台 · 摄取层。

职责：
1. 把原始抓取产物（RawBatch）转换成 6 类事实输出。
2. 把 Markdown 文本落到 asset_store，对正文做哈希与 frontmatter 维护。
3. 持久化 ingestion_batches / ingestion_checkpoints，便于增量续跑。
"""
from .checkpoints import CheckpointStore, checkpoint_key_for
from .identity_resolver import IdentityResolver, ResolverDecision
from .markdown_store import MarkdownStore, MarkdownWriteResult
from .pipeline import IngestionPipeline, RawBatch
from .reconcile import ReconcileCheckResult, ReconcileEngine

__all__ = [
    "CheckpointStore",
    "IdentityResolver",
    "IngestionPipeline",
    "MarkdownStore",
    "MarkdownWriteResult",
    "RawBatch",
    "ReconcileCheckResult",
    "ReconcileEngine",
    "ResolverDecision",
    "checkpoint_key_for",
]
