"""
🔐 微信公众号账号登录管理工具

📝 模块简介:
- 本模块开发目的：实现多个微信公众号账号的登录状态管理和账号配置管理，确保发布流程的账号可用性

🔍 主要功能与处理流程:

1. 账号管理功能
- 👥 支持添加新公众号账号
- 🗑️ 支持删除已有账号
- 📊 账号信息展示与验证
- 🔑 账号ID自动生成与管理

2. 登录状态管理
- 🔄 支持单个账号登录状态更新
- 🔄 支持批量账号登录状态更新
- 🍪 Cookie和Token自动保存
- 🛡️ 登录状态验证

3. 用户交互
- 📝 账号信息输入与验证
- ⚠️ 删除操作二次确认
- 🔄 操作结果反馈
- ⌨️ 支持用户中断操作

4. 异常处理
- 🛡️ 优雅的错误处理机制
- 📝 详细的错误日志输出
- 🔄 程序退出保护
"""

import os
import sys
import json
import shutil
from threading import Thread
from account_manager import AccountManager
from browser_manager import BrowserManager
from wechat_publisher import WeChatPublisher

def add_new_account(account_manager: AccountManager):
    """添加新账号"""
    try:
        print("\n=== 添加新账号 ===")
        
        # 获取现有账号列表
        existing_accounts = account_manager.get_active_accounts()
        existing_ids = [acc['id'] for acc in existing_accounts]
        
        # 生成新账号ID（取最大ID + 1）
        new_id = max(existing_ids) + 1 if existing_ids else 1
        
        # 获取账号信息
        name = input("请输入账号名称: ").strip()
        if not name:
            print("错误: 账号名称不能为空")
            return False
            
        # 生成账号配置
        profile_dir = f"account{new_id}"
        new_account = {
            "id": new_id,
            "name": name,
            "profile_dir": profile_dir,
            "chrome_user_data": f"./config/accounts/{profile_dir}/chrome_user_data",
            "cookie_file": f"./config/accounts/{profile_dir}/cookies.json",
            "token_file": f"./config/accounts/{profile_dir}/token.txt",
            "status": "active"
        }
        
        # 读取现有配置
        with open(account_manager.config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        # 添加新账号
        config['accounts'].append(new_account)
        
        # 保存配置
        with open(account_manager.config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
            
        # 创建账号目录
        account_manager.ensure_account_directories(new_account)
        
        print(f"\n账号 {name} 添加成功!")
        print("现在您可以选择更新该账号的登录状态")
        return True
        
    except Exception as e:
        print(f"添加账号时发生错误: {e}")
        return False

def remove_account(account_manager: AccountManager):
    """删除账号"""
    try:
        print("\n=== 删除账号 ===")
        
        # 显示可用账号
        account_manager.display_accounts()
        
        # 获取要删除的账号ID
        while True:
            try:
                account_id = int(input("\n请输入要删除的账号ID（输入0取消）: ").strip())
                if account_id == 0:
                    return False
                account = account_manager.get_account_by_id(account_id)
                if account:
                    break
                print("错误: 无效的账号ID")
            except ValueError:
                print("错误: 请输入数字ID")
                
        # 确认删除
        confirm = input(f"\n确定要删除账号 {account['name']} 吗？此操作不可恢复！(y/n): ").strip().lower()
        if confirm != 'y':
            print("操作已取消")
            return False
            
        # 读取现有配置
        with open(account_manager.config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        # 移除账号配置
        config['accounts'] = [acc for acc in config['accounts'] if acc['id'] != account_id]
        
        # 保存配置
        with open(account_manager.config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
            
        # 删除账号目录
        account_dir = os.path.dirname(account['chrome_user_data'])
        if os.path.exists(account_dir):
            shutil.rmtree(account_dir)
            
        print(f"\n账号 {account['name']} 已删除!")
        return True
        
    except Exception as e:
        print(f"删除账号时发生错误: {e}")
        return False

def update_account_status(account_id: int = None):
    """更新指定账号的登录状态"""
    try:
        print("\n=== 微信公众号账号登录工具 ===")
        
        # 初始化账号管理器
        account_manager = AccountManager()
        
        # 如果没有指定账号ID，显示账号列表供用户选择
        if account_id is None:
            account_manager.display_accounts()
            while True:
                user_input = input("\n请输入要登录的账号ID (输入 'a' 同时启动所有账号): ").strip().lower()
                if user_input == 'a':
                    all_accounts = account_manager.get_active_accounts()
                    if not all_accounts:
                        print("没有可用的账号。")
                        return False

                    print("\n=== 同时启动所有账号的浏览器 ===")

                    def start_account_in_browser(account_info):
                        print(f"\n--- 正在启动账号: {account_info['name']} ---")
                        account_manager.ensure_account_directories(account_info)
                        browser = BrowserManager(user_data_dir=account_info['chrome_user_data'])
                        publisher = WeChatPublisher(browser)
                        publisher.set_account_files(account_info['cookie_file'], account_info['token_file'])
                        publisher.set_account_info(account_info)
                        if publisher.login_manager.open_wechat_admin():
                            print(f"账号 {account_info['name']} 登录成功!")
                        else:
                            print(f"账号 {account_info['name']} 登录失败。")

                    threads = [
                        Thread(target=start_account_in_browser, args=(account_info,))
                        for account_info in all_accounts
                    ]

                    for thread in threads:
                        thread.start()

                    for thread in threads:
                        thread.join()

                    print("\n=== 所有账号浏览器已启动 ===")
                    print("完成操作后，请手动关闭浏览器窗口并返回主菜单。")
                    return True

                try:
                    account_id = int(user_input)
                    account = account_manager.get_account_by_id(account_id)
                    if account:
                        break
                    print("错误: 无效的账号ID")
                except ValueError:
                    print("错误: 请输入数字ID")
        else:
            account = account_manager.get_account_by_id(account_id)
            if not account:
                print(f"错误: 未找到ID为 {account_id} 的账号")
                return False
        
        print(f"\n=== 更新账号登录状态: {account['name']} ===")
        print(f"用户数据目录: {account['chrome_user_data']}")
        
        # 确保账号目录存在
        account_manager.ensure_account_directories(account)
        
        # 创建浏览器实例
        browser = BrowserManager(user_data_dir=account['chrome_user_data'])
        
        # 创建发布器实例
        publisher = WeChatPublisher(browser)
        publisher.set_account_files(account['cookie_file'], account['token_file'])
        publisher.set_account_info(account)  # 设置完整账号信息
        
        # 尝试登录
        if publisher.login_manager.open_wechat_admin():
            print(f"\n账号 {account['name']} 登录成功!")
            print("Cookie和Token已更新")
            
            # 提示用户可以查看账号数据
            print("\n" + "="*50)
            print("🎉 登录成功！")
            print("📊 您现在可以在浏览器中查看账号数据、粉丝量、文章统计等信息")
            print("🔗 当前页面：微信公众平台后台")
            print("💡 请尽快查看所需信息")
            print("="*50)
            print("\n按回车键返回主菜单（按回车后浏览器将关闭）...")
            
            # 等待用户确认
            input()
            
            return True
        else:
            print(f"\n账号 {account['name']} 登录失败")
            return False
            
    except Exception as e:
        print(f"发生错误: {e}")
        import traceback
        print(traceback.format_exc())
        return False

def main():
    try:
        while True:
            print("\n=== 微信公众号账号管理 ===")
            print("1. 更新单个账号登录状态")
            print("2. 更新所有账号登录状态")
            print("3. 添加新账号")
            print("4. 删除账号")
            print("5. 退出")
            
            choice = input("\n请选择操作 (1-5): ").strip()
            
            account_manager = AccountManager()
            
            if choice == "1":
                update_account_status()
            elif choice == "2":
                accounts = account_manager.get_active_accounts()
                print("\n开始更新所有账号状态...")
                for account in accounts:
                    print(f"\n正在处理账号: {account['name']}")
                    update_account_status(account['id'])
                    input("\n按回车继续下一个账号...")
            elif choice == "3":
                add_new_account(account_manager)
            elif choice == "4":
                remove_account(account_manager)
            elif choice == "5":
                print("\n感谢使用!")
                break
            else:
                print("\n无效的选择，请重试")
                
    except KeyboardInterrupt:
        print("\n\n程序被用户中断")
    except Exception as e:
        print(f"\n程序发生错误: {e}")
        import traceback
        print(traceback.format_exc())
    finally:
        input("\n按回车键退出...")

if __name__ == "__main__":
    main() 