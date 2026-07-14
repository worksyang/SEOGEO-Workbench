"""
配置管理模块
负责读取和管理所有配置信息
"""

import os
import json
from typing import Dict, Any, Optional

class ConfigManager:
    def __init__(self, config_path: str = None):
        """
        初始化配置管理器
        
        Args:
            config_path: 配置文件路径，默认为ai_services目录下的config.json
        """
        self.config_path = config_path or os.path.join(os.path.dirname(__file__), 'config.json')
        self.config: Dict[str, Any] = {}
        self.load_config()
    
    def load_config(self) -> None:
        """加载配置文件"""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
        except Exception as e:
            print(f"读取配置文件失败: {e}")
            raise
    
    def get_ai_config(self, platform: str) -> Dict[str, Any]:
        """
        获取指定AI平台的配置
        
        Args:
            platform: AI平台名称 (deepseek/chatnp/siliconflow)
            
        Returns:
            Dict: 平台配置信息
        """
        return self.config.get(platform, {})
    
    def get_proxy_config(self) -> Dict[str, str]:
        """获取代理配置"""
        return self.config.get('proxy', {})
    
    def get_output_dir(self) -> str:
        """获取输出目录配置"""
        return self.config.get('output_dir', 'ai_output')
    
    def get_template_config(self) -> Dict[str, str]:
        """获取模板配置"""
        return self.config.get('template', {})
    
    def save_config(self) -> None:
        """保存配置到文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"保存配置文件失败: {e}")
            raise 