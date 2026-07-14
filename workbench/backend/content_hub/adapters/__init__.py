"""适配器包：每个适配器实现统一的 ingest 输出。

已注册的适配器：
- WechatAdapter (微信搜一搜)
- MpAdapter      (公众号监控)
- XhsAdapter     (小红书)
- GeoAdapter     (GEO 引用源)
- WikiAdapter    (母文章库)
- WritingAdapter (WritingMoney 生产链路)
- PublishingAdapter (发布中心)
"""
from .base import AdapterStatus, AdapterTask
from .wechat import WechatAdapter
from .mp import MpAdapter
from .xhs import XhsAdapter
from .geo import GeoAdapter, RedfoxAdapter
from .wiki import run as run_wiki
from .writing import run as run_writing
from .publishing import run as run_publishing

__all__ = [
    "AdapterStatus",
    "AdapterTask",
    "WechatAdapter",
    "MpAdapter",
    "XhsAdapter",
    "GeoAdapter",
    "RedfoxAdapter",
    "run_wiki",
    "run_writing",
    "run_publishing",
]
