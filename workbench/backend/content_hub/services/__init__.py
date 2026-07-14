"""统一内容工作台 · 服务层。

为 API 路由聚合更易测试的 Service Facade；保持与 SQL / Adapter 解耦。
"""
from .content import ContentService
from .search import SearchService
from .geo import GeoService
from .metrics import MetricsService
from .signals import SignalsService
from .jobs import JobsService
from .system_health import SystemHealthService
from .backup import BackupService
from .wiki import WikiService
from .writing import WritingService
from .publishing import PublishingService

__all__ = [
    "BackupService",
    "ContentService",
    "GeoService",
    "JobsService",
    "MetricsService",
    "PublishingService",
    "SearchService",
    "SignalsService",
    "SystemHealthService",
    "WikiService",
    "WritingService",
]
