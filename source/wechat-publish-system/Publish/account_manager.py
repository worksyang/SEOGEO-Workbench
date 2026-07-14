"""
👥 微信公众号账号管理模块

📝 模块简介:
- 本模块负责管理多个微信公众号账号的配置信息，提供账号信息的加载、验证和查询功能

🔍 主要功能与处理流程:

1. 账号配置管理
- 📁 从JSON配置文件加载账号信息
- 🔄 自动处理相对路径转换
- ⚙️ 配置文件错误处理

2. 账号信息查询
- 🔍 获取活跃账号列表
- 📊 按ID查询单个账号
- 📋 批量查询多个账号

3. 目录管理
- 📂 自动创建账号相关目录
- 🛠️ 确保必要的目录结构存在

4. 用户交互
- 📝 显示可用账号列表
- 🔢 解析用户输入的账号ID
- ⚠️ 输入验证与错误提示
"""

import json
import os
from typing import List, Dict, Optional

class AccountManager:
    def __init__(self, config_file: str = './config/accounts.json'):
        # 获取项目根目录（相对于当前文件的位置）
        self.project_root = os.path.dirname(os.path.abspath(__file__))
        self.config_file = os.path.join(self.project_root, config_file)
        self.accounts = self._load_accounts()
        
    def _load_accounts(self) -> List[Dict]:
        """从配置文件加载账号信息"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                # 处理配置中的相对路径
                for account in config['accounts']:
                    account['chrome_user_data'] = os.path.abspath(os.path.join(
                        self.project_root, account['chrome_user_data']))
                    account['cookie_file'] = os.path.abspath(os.path.join(
                        self.project_root, account['cookie_file']))
                    account['token_file'] = os.path.abspath(os.path.join(
                        self.project_root, account['token_file']))
                return config['accounts']
        except Exception as e:
            print(f"加载账号配置失败: {e}")
            return []
            
    def get_active_accounts(self) -> List[Dict]:
        """获取所有活跃状态的账号"""
        return [acc for acc in self.accounts if acc['status'] == 'active']
        
    def get_account_by_id(self, account_id: int) -> Optional[Dict]:
        """根据ID获取账号信息"""
        for account in self.accounts:
            if account['id'] == account_id:
                return account
        return None
        
    def get_accounts_by_ids(self, account_ids: List[int]) -> List[Dict]:
        """根据ID列表获取多个账号信息"""
        return [acc for acc in self.accounts if acc['id'] in account_ids]
        
    def ensure_account_directories(self, account: Dict):
        """确保账号相关的目录和文件都存在"""
        directories = [
            os.path.dirname(account['chrome_user_data']),
            os.path.dirname(account['cookie_file']),
            os.path.dirname(account['token_file'])
        ]
        for directory in directories:
            if not os.path.exists(directory):
                os.makedirs(directory)
        # 创建空的 cookie_file 和 token_file（如果不存在）
        if not os.path.exists(account['cookie_file']):
            with open(account['cookie_file'], 'w', encoding='utf-8') as f:
                f.write('[]')  # 空cookie用[]
        if not os.path.exists(account['token_file']):
            with open(account['token_file'], 'w', encoding='utf-8') as f:
                f.write('')
                
    def display_accounts(self):
        """显示所有可用账号"""
        print("\n可用的公众号账号：")
        print("-" * 40)
        for account in self.get_active_accounts():
            print(f"[{account['id']}] {account['name']}")
        print("-" * 40)
        
    def parse_account_ids(self, input_str: str) -> List[int]:
        """解析用户输入的账号ID字符串"""
        try:
            # 将输入字符串按逗号分割，转换为整数列表
            account_ids = [int(id.strip()) for id in input_str.split(',')]
            # 验证所有ID是否有效
            valid_ids = []
            for id in account_ids:
                if self.get_account_by_id(id):
                    valid_ids.append(id)
                else:
                    print(f"警告: 账号ID {id} 不存在")
            return valid_ids
        except ValueError:
            print("错误: 请输入有效的账号ID（以逗号分隔的数字）")
            return [] 