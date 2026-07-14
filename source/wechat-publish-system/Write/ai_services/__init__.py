"""
ai_services 包初始化
提供统一的配置读取函数，替代独立的 ConfigManager 类。
"""
import os
import json
from typing import Dict, Any

_CONFIG_CACHE: Dict[str, Any] = {}
_CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

def _load_config() -> None:
    """内部函数：加载配置到缓存"""
    global _CONFIG_CACHE
    if _CONFIG_CACHE:
        return
    try:
        with open(_CONFIG_PATH, 'r', encoding='utf-8') as f:
            _CONFIG_CACHE = json.load(f)
    except Exception as e:
        print(f"读取配置文件失败: {e}")
        _CONFIG_CACHE = {}

def get_ai_config(platform: str) -> Dict[str, Any]:
    """
    获取指定AI平台的配置
    
    Args:
        platform: 平台名称（如 system/deepseek/chatnp/siliconflow 等）
    """
    _load_config()
    return _CONFIG_CACHE.get(platform, {}) if isinstance(_CONFIG_CACHE, dict) else {}

def get_proxy_config() -> Dict[str, str]:
    """获取代理配置"""
    _load_config()
    return _CONFIG_CACHE.get('proxy', {}) if isinstance(_CONFIG_CACHE, dict) else {}

def get_output_dir() -> str:
    """获取输出目录"""
    _load_config()
    return _CONFIG_CACHE.get('output_dir', 'ai_output') if isinstance(_CONFIG_CACHE, dict) else 'ai_output'

def get_template_config() -> Dict[str, str]:
    """获取模板配置"""
    _load_config()
    return _CONFIG_CACHE.get('template', {}) if isinstance(_CONFIG_CACHE, dict) else {}

def save_config() -> None:
    """将缓存中的配置写回文件"""
    try:
        with open(_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(_CONFIG_CACHE, f, indent=4, ensure_ascii=False)
    except Exception as e:
        print(f"保存配置文件失败: {e}")


