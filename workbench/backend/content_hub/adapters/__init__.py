"""Content OS 采集适配器。"""

from .base import AdapterStatus, AdapterTask
from .geo import GeoAdapter, RedfoxAdapter
from .mp import MpAdapter
from .wechat import WechatAdapter
from .xhs import XhsAdapter

__all__ = [
    "AdapterStatus",
    "AdapterTask",
    "GeoAdapter",
    "RedfoxAdapter",
    "MpAdapter",
    "WechatAdapter",
    "XhsAdapter",
]
