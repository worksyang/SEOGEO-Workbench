"""
📅 微信公众号文章发布调度器

📝 模块简介:
- 本模块开发目的：实现微信公众号文章批量发布的智能调度，提高运营效率
- 支持多账号轮换发布，避免单个账号发布频率过高
- 自动管理账号状态，实现智能冷却机制
- 支持随机文章选择，实现内容发布的多样性

🔍 主要功能与处理流程:

1. 账号管理 (PublishScheduler)
- 🔄 多账号轮换使用
- ⏱️ 智能冷却时间管理
- 📊 账号状态实时监控
- 🔄 自动恢复可用状态

2. 文章调度（run_publish_schedule）
- 📝 支持批量文章发布
- ⏱️ 可配置发布间隔时间
- 🎲 支持随机文章选择
- 📊 发布进度实时显示

3. 发布流程（_publish_single_article）
- ✅ 自动处理单篇文章发布
- 📁 自动归档已发布文章
- 📊 发布结果统计
- 📝 失败文章记录

4. 异常处理（process_single_article）
- 🛡️ 完善的错误处理机制
- 🔄 自动重试机制
- 📝 详细的日志输出
- 📊 发布结果汇总
"""

import os
import time
import random
import shutil
import re
import threading
import queue
from typing import List, Dict, Union
from account_manager import AccountManager
from browser_manager import BrowserManager
from wechat_publisher import WeChatPublisher
from md_wxhtml import MdToWxHtmlConverter
from datetime import datetime, timedelta
from config.folder_config import PUBLISHED_FOLDER

class PublishScheduler:
    def __init__(self, account_manager: AccountManager):         # 初始化发布调度器
        self.account_manager = account_manager
        self.published_folder = PUBLISHED_FOLDER                 # 从配置文件中读取已发布文件的目标文件夹
        # 统计信息
        self.total_articles = 0      # 文章总数
        self.total_groups = 0        # 总组数
        self.current_group = 0       # 当前处理的组号
        self.success_articles = 0    # 成功发布的文章数
        self.failed_articles = 0     # 发布失败的文章数
        self.failed_paths = []       # 失败文章的路径
        # 账号状态管理
        self.account_status = {}     # 格式: {account_id: {'available': True, 'available_time': timestamp}}
        # 当前处理的文章标题
        self.title = None            # 当前文章的标题
        # 群发模式统计
        self.mass_send_stats = {}    # 格式: {account_id: {'success': 0, 'failed': 0, 'no_homepage': 0, 'name': 'account_name'}}
        
        # 混合调度相关变量
        self.hybrid_stop_event = threading.Event()      # 停止混合调度的信号
        self.interval_stop_event = threading.Event()    # 停止间隔发布的信号
        self.mass_send_stop_event = threading.Event()   # 停止群发的信号
        self.article_queue = queue.Queue()              # 文章队列，线程安全
        self.published_groups_count = 0                 # 已发布的组数
        self.hybrid_lock = threading.Lock()             # 线程锁，保护共享变量
            
    def extract_title(self, filename: str) -> str:
        """从文件名提取标题：从左到右遇到第一个日期/时间样式即截断"""
        # 去除扩展名
        name = re.sub(r"\.md$", "", filename, flags=re.IGNORECASE)
        # 以下划线切分
        parts = name.split("_") if "_" in name else [name]
        title_parts = []
        for part in parts:
            # 检测日期/时间样式：YYYYMMDD / HHMMSS / YYYYMMDDHHMMSS
            if re.fullmatch(r"\d{8}", part) or re.fullmatch(r"\d{6}", part) or re.fullmatch(r"\d{14}", part):
                break
            title_parts.append(part)
        # 若未检测到时间样式，则使用完整去扩展名的名称
        raw_title = "_".join(title_parts) if title_parts else name
        # 清理尾随下划线、空白与常见标点
        title = raw_title.strip().strip(" _-—:：;；,.，。!！?？")
        if not title:
            raise Exception(f"无法从文件名 '{filename}' 中提取标题")
        return title
        
    def _is_account_available(self, account_id: int) -> bool:    # 检查账号是否可用
        # 如果账号不在状态字典中，初始化为可用
        if account_id not in self.account_status:
            self.account_status[account_id] = {
                'available': True,
                'available_time': datetime.now()
            }
            return True
            
        # 如果账号标记为不可用，检查是否已经过了冷却时间
        if not self.account_status[account_id]['available']:
            current_time = datetime.now()
            available_time = self.account_status[account_id]['available_time']
            
            # 如果当前时间已经超过了可用时间，重新标记为可用
            if current_time >= available_time:
                self.account_status[account_id]['available'] = True
                print(f"账号 {account_id} 已恢复可用状态")
                return True
            else:
                # 计算还需等待的时间
                wait_time = available_time - current_time
                wait_minutes = wait_time.total_seconds() / 60
                print(f"账号 {account_id} 暂时不可用，还需等待约 {wait_minutes:.1f} 分钟")
                return False
                
        return True
        
    def _set_account_unavailable(self, account_id: int, minutes: int = 30):    # 将账号设置为不可用状态
        self.account_status[account_id] = {
            'available': False,
            'available_time': datetime.now() + timedelta(minutes=minutes)
        }
        print(f"账号 {account_id} 已设置为休息状态，将在 {minutes} 分钟后恢复")
            
    def _publish_article_group(self, article_paths: List[str], account: Dict, articles_per_publish: int = 1, article_dir: str = None, mass_send_notify: bool = False) -> bool:
        """
        发布一组文章
        :param article_paths: 这一组要发布的文章路径列表
        :param account: 账号信息
        :param articles_per_publish: 每组发布的图文数量（1-8）
        :param article_dir: 文章所在目录
        :param mass_send_notify: 是否群发
        :return: 是否发布成功
        """
        try:
            group_size = len(article_paths)
            print(f"\n=== 开始发布第 {self.current_group} 组文章 ===")
            print(f"本组文章数: {group_size}")
            print(f"使用账号: {account['name']}")
            
            # 创建浏览器管理器实例，使用账号特定的配置
            browser_manager = BrowserManager(
                user_data_dir=account['chrome_user_data']
            )
            
            try:
                # 创建Markdown到微信HTML转换器和微信发布工具实例
                md_converter = MdToWxHtmlConverter(article_dir, browser_manager)  # 使用传入的文章目录
                wechat_publisher = WeChatPublisher(browser_manager)               # 设置微信发布工具
                wechat_publisher.set_account_files(account['cookie_file'], account['token_file'])  # 设置cookie和token文件
                wechat_publisher.set_account_info(account)  # 设置完整账号信息
                
                # 登录微信公众平台并打开编辑器窗口
                if not wechat_publisher.login_manager.open_wechat_admin():
                    raise Exception("登录微信公众平台失败")
                if not wechat_publisher.login_manager.click_content_management():
                    raise Exception("进入内容管理失败")
                time.sleep(2)
                if not wechat_publisher.login_manager.click_drafts():
                    raise Exception("进入草稿箱失败")
                time.sleep(2)
                if not wechat_publisher.login_manager.click_new_article():
                    raise Exception("点击新建文章失败")
                if not wechat_publisher.login_manager.switch_to_edit_window():
                    raise Exception("切换到编辑器窗口失败")
                
                # 在新标签页中打开 weiyan.cc 以备后续使用
                print("🌐 正在新标签页中打开Markdown编辑器...")
                time.sleep(1.5)
                browser_manager.driver.execute_script("window.open('https://md.weiyan.cc/');")
                
                time.sleep(1.5)
                # 切换回编辑器窗口
                browser_manager.driver.switch_to.window(browser_manager.driver.window_handles[-2])
                print("✅ Markdown编辑器已在新标签页打开，并已切回编辑器窗口")
                
                # 保存窗口句柄供后续使用
                weiyan_handle = browser_manager.driver.window_handles[-1]
                editor_handle = browser_manager.driver.window_handles[-2]
                
                # 保存窗口句柄到md_converter对象，优化窗口切换
                md_converter.editor_handle = editor_handle
                md_converter.weiyan_handle = weiyan_handle
                
                # 逐篇处理文章
                for article_path in article_paths:
                    # 使用extract_title方法获取文章标题
                    self.title = self.extract_title(os.path.basename(article_path))
                    
                    # 转换Markdown为微信HTML
                    if not md_converter.convert_md_to_html(article_path):
                        raise Exception(f"文章 {os.path.basename(article_path)} Markdown转换失败")
                    
                    time.sleep(1)
                    # 输入文章内容到微信编辑器
                    if not wechat_publisher.content_editor.input_article_content(self.title):
                        raise Exception(f"输入文章内容失败: {os.path.basename(article_path)}")
                    
                    # 上传封面图片
                    if not wechat_publisher.content_editor.upload_cover_image():
                        raise Exception(f"上传封面图片失败: {os.path.basename(article_path)}")
                    
                    # 如果还有下一篇文章，直接创建新图文
                    if article_path != article_paths[-1]:
                        try:
                            print(f"  - 创建下一篇文章...")
                            # 直接使用ContentEditor的create_new_article方法创建新图文
                            if not wechat_publisher.content_editor.create_new_article():
                                raise Exception("创建新图文失败")
                            print("  - 已创建新图文")
                            time.sleep(1)  # 等待新图文编辑器加载完成
                        except Exception as e:
                            print(f"  - 创建新文章失败: {str(e)}")
                            raise Exception("创建新文章失败")
                
                # 所有文章都处理完后，调用发布流程
                if not wechat_publisher.publish_manager.prepare_for_publish(mass_send_notify=mass_send_notify):
                    raise Exception("准备发布失败")
                
                if not wechat_publisher.publish_manager.publish_article():
                    # 发布失败，将账号设置为不可用
                    self._set_account_unavailable(account['id'], minutes=30)
                    raise Exception("微信发布失败")
                
                # 发布成功后移动所有文件
                for article_path in article_paths:
                    self._move_to_published(article_path)
                self.success_articles += len(article_paths)
                print(f"第 {self.current_group} 组文章发布成功！")
                return True
                
            finally:
                # 确保浏览器实例被清理
                if browser_manager and browser_manager.driver:
                    browser_manager.driver.quit()
                    print("\n浏览器已关闭")
            
        except Exception as e:
            print(f"发布过程出错: {str(e)}")
            self.failed_articles += len(article_paths)
            self.failed_paths.extend(article_paths)
            return False
            
    def _move_to_published(self, md_file: str) -> bool:
        """将已发布的md文件移动到已发布文件夹"""
        try:
            # 确保目标文件夹存在
            os.makedirs(self.published_folder, exist_ok=True)
            
            # 获取源文件名
            file_name = os.path.basename(md_file)
            
            # 构建目标路径
            target_path = os.path.join(self.published_folder, file_name)
            
            # 检查目标文件是否已存在
            if os.path.exists(target_path):
                print(f"文件 {file_name} 已存在于 {self.published_folder}")
                return True
            
            # 尝试移动文件
            try:
                shutil.move(md_file, target_path)
                print(f"已将 {file_name} 移动到 {self.published_folder}")
            except Exception as e:
                # 失败时仍移动文件
                shutil.move(md_file, target_path)
                print(f"发布失败，但已将 {file_name} 移动到 {self.published_folder}")
                if os.path.exists(target_path):
                    print(f"文件 {file_name} 已成功移动到 {self.published_folder}")
                    return True
                print(f"移动文件失败: {e}")
                return False
            
        except Exception as e:
            print(f"移动文件过程出错: {e}")
            return False
            
    def display_progress(self, current_group: int, total_groups: int):
        """显示当前进度"""
        print(f"\n进度: 第 {current_group} 组 / 共 {total_groups} 组")
        
    def display_summary(self):
        """显示发布汇总信息"""
        print("\n=== 发布汇总 ===")
        print(f"文章总数: {self.total_articles}")
        print(f"总组数: {self.total_groups}")
        print(f"成功发布: {self.success_articles} 篇")
        print(f"发布失败: {self.failed_articles} 篇")
        if self.failed_paths:
            print("\n失败文章列表:")
            for path in self.failed_paths:
                if os.path.exists(path):  # 只显示仍然存在的失败文件路径
                    print(f"- {os.path.basename(path)}")
    
    def _display_mass_send_summary(self):
        """显示群发模式下的详细汇总信息"""
        if not self.mass_send_stats:
            return
            
        print("\n" + "="*50)
        print("📊 群发模式发布情况汇总")
        print("="*50)
        
        total_success = 0
        total_failed = 0
        total_no_homepage = 0
        
        # 按账号显示统计
        for account_id, stats in self.mass_send_stats.items():
            print(f"\n📱 账号: {stats['name']} (ID: {account_id})")
            print(f"   ✅ 成功发布: {stats['success']} 组")
            print(f"   ❌ 发布失败: {stats['failed']} 组")
            if stats['no_homepage'] > 0:
                print(f"   ⚠️  未检测到首页: {stats['no_homepage']} 组")
            
            total_success += stats['success']
            total_failed += stats['failed']
            total_no_homepage += stats['no_homepage']
        
        # 总体统计
        print(f"\n📈 总体统计:")
        print(f"   ✅ 总成功: {total_success} 组")
        print(f"   ❌ 总失败: {total_failed} 组")
        if total_no_homepage > 0:
            print(f"   ⚠️  总未检测到首页: {total_no_homepage} 组")
        
        total_attempts = total_success + total_failed + total_no_homepage
        if total_attempts > 0:
            success_rate = (total_success / total_attempts) * 100
            print(f"   📊 成功率: {success_rate:.1f}%")
        
        print("="*50)

    def _run_mass_send_schedule(self, article_dir: str, account_ids: List[int], articles_per_publish: int, total_articles: int, mass_send_notify: bool):
        """
        以每天固定时间（晚上8:30）的模式执行群发计划
        """
        # 初始化群发统计
        self.mass_send_stats = {}
        for account_id in account_ids:
            account = self.account_manager.get_account_by_id(account_id)
            if account:
                self.mass_send_stats[account_id] = {
                    'success': 0,
                    'failed': 0,
                    'no_homepage': 0,
                    'name': account['name']
                }
        
        # 获取所有文章并随机选择指定数量
        try:
            all_articles_list = [f for f in os.listdir(article_dir) if f.endswith('.md')]
            if not all_articles_list:
                print("错误: 在指定目录中未找到.md文章。")
                return
            # 随机选择指定数量的文章
            selected_count = min(total_articles, len(all_articles_list))
            selected_articles = random.sample(all_articles_list, selected_count)
            all_articles = [os.path.join(article_dir, f) for f in selected_articles]
        except FileNotFoundError:
            print(f"错误: 文章目录 '{article_dir}' 不存在。")
            return
        
        # 将文章分组
        article_groups = [all_articles[i:i + articles_per_publish] for i in range(0, len(all_articles), articles_per_publish)]
        
        self.total_articles = len(all_articles)
        self.total_groups = len(article_groups)
        
        if self.total_groups == 0:
            print("没有要发布的文章组。")
            return
        
        print("\n--- 启动定时群发模式 ---")
        print(f"总计需要发布 {self.total_articles} 篇文章，分为 {self.total_groups} 组。")
        print(f"使用账号ID: {account_ids}")

        published_groups_count = 0
        
        while published_groups_count < self.total_groups:
            # 计算下一个晚上8:30的发布时间
            now = datetime.now()
            publish_time = now.replace(hour=20, minute=30, second=0, microsecond=0)
            
            if now > publish_time:
                publish_time += timedelta(days=1) # 如果今天已经过了8:30，则安排在明天
            
            wait_seconds = (publish_time - now).total_seconds()
            
            if wait_seconds > 0:
                wait_duration = timedelta(seconds=int(wait_seconds))
                print(f"\n下一次群发将在 {publish_time.strftime('%Y-%m-%d %H:%M:%S')} 进行。")
                print(f"等待时间: {wait_duration}")
                
                # 群发模式下，每小时显示一次剩余时间
                remaining_seconds = wait_seconds
                while remaining_seconds > 0:
                    try:
                        # 每小时检查一次，或者如果剩余时间不足1小时则直接等待
                        check_interval = min(3600, remaining_seconds)  # 1小时 = 3600秒
                        time.sleep(check_interval)
                        remaining_seconds -= check_interval
                        
                        if remaining_seconds > 0:
                            remaining_hours = remaining_seconds / 3600
                            if remaining_hours >= 1:
                                print(f"⏰ 距离下次群发还有 {remaining_hours:.1f} 小时")
                            else:
                                remaining_minutes = remaining_seconds / 60
                                print(f"⏰ 距离下次群发还有 {remaining_minutes:.1f} 分钟")
                                
                    except KeyboardInterrupt:
                        print("\n用户手动中断等待。")
                        return

            print(f"\n--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
            print("到达预定发布时间，开始处理今日的群发任务...")
            
            # 当天轮流使用账号发布 - 每天所有账号都要发布一组文章
            for account_id in account_ids:
                if published_groups_count >= self.total_groups:
                    print("所有文章组已发布完毕。")
                    break

                account = self.account_manager.get_account_by_id(account_id)
                if not account:
                    print(f"警告: 找不到ID为 {account_id} 的账号，跳过。")
                    continue
                
                print(f"\n轮到账号: {account['name']} (ID: {account_id})")
                
                # 获取当前要发布的文章组
                article_group_to_publish = article_groups[published_groups_count]
                self.current_group = published_groups_count + 1
                
                # 发布文章组
                success = self._publish_article_group(
                    article_paths=article_group_to_publish,
                    account=account,
                    articles_per_publish=articles_per_publish,
                    article_dir=article_dir,
                    mass_send_notify=mass_send_notify
                )
                
                if success:
                    print(f"账号 {account['name']} 已成功发布一组文章。")
                    self.mass_send_stats[account_id]['success'] += 1
                    published_groups_count += 1
                    
                    # 群发模式下，账号间固定间隔10秒
                    if published_groups_count < self.total_groups and account_id != account_ids[-1]:
                        print("等待10秒后发布下一个账号...")
                        time.sleep(10)
                else:
                    print(f"账号 {account['name']} 发布文章组失败。")
                    self.mass_send_stats[account_id]['failed'] += 1
                    # 发布失败时，不增加计数，让下一个账号尝试发布同一组文章
                    continue
            
            # 每天每个账号只能群发一次，所以每天最多发布 len(account_ids) 组文章
            if published_groups_count < self.total_groups:
                print(f"\n今日群发任务已处理完毕（每个账号每天只能群发一次）。")
                print(f"已发布 {published_groups_count} 组，剩余 {self.total_groups - published_groups_count} 组。")
                print("等待下一轮定时发布。")
        
        print("\n--- 所有定时群发任务已完成 ---")
        self._display_mass_send_summary()
        self.display_summary()

    def _run_immediate_mass_send_schedule(self, article_dir: str, account_ids: List[int], articles_per_publish: int, total_articles: int, interval_seconds: int, mass_send_notify: bool):
        """
        立即群发模式：所有账号立即开始群发，每个账号每天只发一组，组间间隔为 interval_seconds。
        """
        # 初始化群发统计
        self.mass_send_stats = {}
        for account_id in account_ids:
            account = self.account_manager.get_account_by_id(account_id)
            if account:
                self.mass_send_stats[account_id] = {
                    'success': 0,
                    'failed': 0,
                    'no_homepage': 0,
                    'name': account['name']
                }
        
        # 获取所有文章并随机选择指定数量
        try:
            all_articles_list = [f for f in os.listdir(article_dir) if f.endswith('.md')]
            if not all_articles_list:
                print("错误: 在指定目录中未找到.md文章。")
                return
            # 随机选择指定数量的文章
            selected_count = min(total_articles, len(all_articles_list))
            selected_articles = random.sample(all_articles_list, selected_count)
            all_articles = [os.path.join(article_dir, f) for f in selected_articles]
        except FileNotFoundError:
            print(f"错误: 文章目录 '{article_dir}' 不存在。")
            return
        
        # 将文章分组
        article_groups = [all_articles[i:i + articles_per_publish] for i in range(0, len(all_articles), articles_per_publish)]
        
        self.total_articles = len(all_articles)
        self.total_groups = len(article_groups)
        
        if self.total_groups == 0:
            print("没有要发布的文章组。")
            return
        
        print("\n--- 启动正常群发模式（立即开始，按间隔依次群发） ---")
        print(f"总计需要发布 {self.total_articles} 篇文章，分为 {self.total_groups} 组。")
        print(f"使用账号ID: {account_ids}")

        published_groups_count = 0
        
        while published_groups_count < self.total_groups:
            print(f"\n--- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---")
            print("开始处理本轮群发任务...")
            
            # 当天轮流使用账号发布 - 每天所有账号都要发布一组文章
            for account_id in account_ids:
                if published_groups_count >= self.total_groups:
                    print("所有文章组已发布完毕。")
                    break

                account = self.account_manager.get_account_by_id(account_id)
                if not account:
                    print(f"警告: 找不到ID为 {account_id} 的账号，跳过。")
                    continue
                
                print(f"\n轮到账号: {account['name']} (ID: {account_id})")
                
                # 获取当前要发布的文章组
                article_group_to_publish = article_groups[published_groups_count]
                self.current_group = published_groups_count + 1
                
                # 发布文章组
                success = self._publish_article_group(
                    article_paths=article_group_to_publish,
                    account=account,
                    articles_per_publish=articles_per_publish,
                    article_dir=article_dir,
                    mass_send_notify=mass_send_notify
                )
                
                if success:
                    print(f"账号 {account['name']} 已成功发布一组文章。")
                    self.mass_send_stats[account_id]['success'] += 1
                    published_groups_count += 1
                    
                    # 正常群发模式下，账号间使用用户设置的间隔
                    if published_groups_count < self.total_groups and account_id != account_ids[-1]:
                        print(f"等待 {interval_seconds} 秒后发布下一个账号...")
                        time.sleep(interval_seconds)
                else:
                    print(f"账号 {account['name']} 发布文章组失败。")
                    self.mass_send_stats[account_id]['failed'] += 1
                    # 发布失败时，不增加计数，让下一个账号尝试发布同一组文章
                    continue
            
            # 每天每个账号只能群发一次，所以每天最多发布 len(account_ids) 组文章
            if published_groups_count < self.total_groups:
                print(f"\n本轮群发任务已处理完毕（每个账号每天只能群发一次）。")
                print(f"已发布 {published_groups_count} 组，剩余 {self.total_groups - published_groups_count} 组。")
                print(f"等待24小时后继续下一轮群发...")
                time.sleep(24 * 3600)
        
        print("\n--- 所有正常群发任务已完成 ---")
        self._display_mass_send_summary()
        self.display_summary()

    def _run_hybrid_timed_schedule(self, article_dir: str, account_ids: List[int], articles_per_publish: int, total_articles: int, interval_seconds: int, mass_send_notify: bool):
        """
        混合定时调度模式：平时间隔发布（不群发） + 8:30定时群发 + 群发后恢复间隔发布
        
        工作流程：
        1. 启动间隔发布线程（不群发模式）
        2. 启动定时检查线程，每天8:30停止间隔发布，执行群发
        3. 群发完成后，恢复间隔发布
        """
        # 初始化统计信息
        self.mass_send_stats = {}
        for account_id in account_ids:
            account = self.account_manager.get_account_by_id(account_id)
            if account:
                self.mass_send_stats[account_id] = {
                    'success': 0,
                    'failed': 0,
                    'no_homepage': 0,
                    'name': account['name']
                }
        
        # 获取所有文章并随机选择指定数量
        try:
            all_articles_list = [f for f in os.listdir(article_dir) if f.endswith('.md')]
            if not all_articles_list:
                print("错误: 在指定目录中未找到.md文章。")
                return
            # 随机选择指定数量的文章
            selected_count = min(total_articles, len(all_articles_list))
            selected_articles = random.sample(all_articles_list, selected_count)
            all_articles = [os.path.join(article_dir, f) for f in selected_articles]
        except FileNotFoundError:
            print(f"错误: 文章目录 '{article_dir}' 不存在。")
            return
        
        # 将文章分组
        article_groups = [all_articles[i:i + articles_per_publish] for i in range(0, len(all_articles), articles_per_publish)]
        
        self.total_articles = len(all_articles)
        self.total_groups = len(article_groups)
        self.published_groups_count = 0
        
        if self.total_groups == 0:
            print("没有要发布的文章组。")
            return
        
        # 将文章组放入队列
        for group in article_groups:
            self.article_queue.put(group)
        
        print("\n--- 启动混合定时调度模式 ---")
        print(f"总计需要发布 {self.total_articles} 篇文章，分为 {self.total_groups} 组。")
        print(f"使用账号ID: {account_ids}")
        print(f"平时间隔: {interval_seconds}秒（不群发）")
        print(f"定时群发: 每天8:30（群发）")
        
        # 重置所有事件
        self.hybrid_stop_event.clear()
        self.interval_stop_event.clear()
        self.mass_send_stop_event.clear()
        
        # 启动间隔发布线程
        interval_thread = threading.Thread(
            target=self._interval_publish_worker,
            args=(article_dir, account_ids, articles_per_publish, interval_seconds, False)  # False表示不群发
        )
        interval_thread.daemon = True
        interval_thread.start()
        
        # 启动定时检查线程
        schedule_thread = threading.Thread(
            target=self._schedule_coordinator,
            args=(article_dir, account_ids, articles_per_publish, interval_seconds, True)  # True表示群发
        )
        schedule_thread.daemon = True
        schedule_thread.start()
        
        try:
            # 主线程等待所有任务完成
            while not self.hybrid_stop_event.is_set() and self.published_groups_count < self.total_groups:
                time.sleep(1)
                
                # 检查是否所有文章都已发布
                with self.hybrid_lock:
                    if self.published_groups_count >= self.total_groups:
                        print("\n--- 所有文章已发布完成 ---")
                        break
        
        except KeyboardInterrupt:
            print("\n用户手动中断混合调度。")
        finally:
            # 停止所有线程
            self.hybrid_stop_event.set()
            self.interval_stop_event.set()
            self.mass_send_stop_event.set()
            
            # 等待线程结束
            if interval_thread.is_alive():
                interval_thread.join(timeout=5)
            if schedule_thread.is_alive():
                schedule_thread.join(timeout=5)
        
        print("\n--- 混合定时调度任务完成 ---")
        self._display_mass_send_summary()
        self.display_summary()

    def _interval_publish_worker(self, article_dir: str, account_ids: List[int], articles_per_publish: int, interval_seconds: int, mass_send_notify: bool):
        """
        间隔发布工作线程：按指定间隔发布文章（不群发模式）
        """
        print(f"\n🔄 间隔发布线程启动 - 间隔: {interval_seconds}秒（不群发）")
        
        accounts = self.account_manager.get_accounts_by_ids(account_ids)
        if not accounts:
            print("错误: 未找到指定的账号")
            return
        
        account_index = 0  # 轮流使用账号的索引
        
        while not self.interval_stop_event.is_set() and not self.hybrid_stop_event.is_set():
            try:
                # 检查是否还有文章需要发布
                if self.article_queue.empty():
                    with self.hybrid_lock:
                        if self.published_groups_count >= self.total_groups:
                            print("📝 间隔发布：所有文章已发布完成")
                            break
                    time.sleep(1)
                    continue
                
                # 获取下一篇文章组
                try:
                    article_group = self.article_queue.get_nowait()
                except queue.Empty:
                    time.sleep(1)
                    continue
                
                # 选择账号（轮流使用）
                account = accounts[account_index % len(accounts)]
                account_index += 1
                
                # 检查账号是否可用
                if not self._is_account_available(account['id']):
                    # 如果账号不可用，将文章组放回队列，等待下次处理
                    self.article_queue.put(article_group)
                    print(f"⏳ 账号 {account['name']} 暂不可用，等待...")
                    time.sleep(60)  # 等待1分钟后重试
                    continue
                
                with self.hybrid_lock:
                    self.current_group = self.published_groups_count + 1
                
                print(f"\n📤 间隔发布 - 账号: {account['name']} - 第{self.current_group}组")
                
                # 发布文章组
                success = self._publish_article_group(
                    article_paths=article_group,
                    account=account,
                    articles_per_publish=articles_per_publish,
                    article_dir=article_dir,
                    mass_send_notify=mass_send_notify  # 这里是False，表示不群发
                )
                
                if success:
                    with self.hybrid_lock:
                        self.published_groups_count += 1
                        print(f"✅ 间隔发布成功 - 已发布 {self.published_groups_count}/{self.total_groups} 组")
                    
                    # 设置账号冷却时间
                    self._set_account_cooldown(account['id'])
                    
                    # 间隔等待
                    if not self.interval_stop_event.is_set():
                        print(f"⏱️ 等待 {interval_seconds} 秒后继续间隔发布...")
                        self.interval_stop_event.wait(timeout=interval_seconds)
                else:
                    # 发布失败，将文章组放回队列
                    self.article_queue.put(article_group)
                    print("❌ 间隔发布失败，文章组已放回队列")
                    
                    # 短暂等待后重试
                    time.sleep(30)
                
            except Exception as e:
                print(f"❌ 间隔发布线程出错: {e}")
                time.sleep(10)
        
        print("🔄 间隔发布线程已停止")

    def _schedule_coordinator(self, article_dir: str, account_ids: List[int], articles_per_publish: int, interval_seconds: int, mass_send_notify: bool):
        """
        调度协调器：负责在8:30时停止间隔发布，执行群发，然后恢复间隔发布
        """
        print("📅 调度协调器启动 - 监控每日8:30定时群发")
        
        while not self.hybrid_stop_event.is_set():
            try:
                # 计算下一个8:30的时间
                now = datetime.now()
                target_time = now.replace(hour=20, minute=30, second=0, microsecond=0)
                
                # 如果当前时间已经过了今天的8:30，则安排在明天
                if now >= target_time:
                    target_time += timedelta(days=1)
                
                wait_seconds = (target_time - now).total_seconds()
                
                print(f"📅 下次群发时间: {target_time.strftime('%Y-%m-%d %H:%M:%S')}")
                print(f"📅 距离群发还有: {timedelta(seconds=int(wait_seconds))}")
                
                # 等待到8:30，每小时检查一次
                while wait_seconds > 0 and not self.hybrid_stop_event.is_set():
                    check_interval = min(3600, wait_seconds)  # 最多等待1小时
                    
                    if self.hybrid_stop_event.wait(timeout=check_interval):
                        return  # 收到停止信号
                    
                    wait_seconds -= check_interval
                    
                    # 重新计算时间（防止系统时间变化）
                    now = datetime.now()
                    wait_seconds = (target_time - now).total_seconds()
                    
                    if wait_seconds > 3600:  # 还有超过1小时
                        hours = wait_seconds / 3600
                        print(f"⏰ 距离群发还有 {hours:.1f} 小时")
                    elif wait_seconds > 60:  # 还有超过1分钟
                        minutes = wait_seconds / 60
                        print(f"⏰ 距离群发还有 {minutes:.1f} 分钟")
                
                if self.hybrid_stop_event.is_set():
                    break
                
                print(f"\n🎯 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - 到达群发时间！")
                
                # 停止间隔发布
                print("⏸️ 暂停间隔发布，准备执行群发...")
                self.interval_stop_event.set()
                time.sleep(2)  # 给间隔发布线程一点时间停止
                
                # 执行群发
                self._execute_mass_send(article_dir, account_ids, articles_per_publish, mass_send_notify)
                
                # 恢复间隔发布
                if not self.hybrid_stop_event.is_set():
                    print("▶️ 群发完成，恢复间隔发布...")
                    self.interval_stop_event.clear()
                    
                    # 重启间隔发布线程
                    interval_thread = threading.Thread(
                        target=self._interval_publish_worker,
                        args=(article_dir, account_ids, articles_per_publish, interval_seconds, False)
                    )
                    interval_thread.daemon = True
                    interval_thread.start()
                
            except Exception as e:
                print(f"❌ 调度协调器出错: {e}")
                time.sleep(60)  # 出错后等待1分钟再重试
        
        print("📅 调度协调器已停止")

    def _execute_mass_send(self, article_dir: str, account_ids: List[int], articles_per_publish: int, mass_send_notify: bool):
        """
        执行群发：在8:30时执行的群发逻辑
        """
        print(f"\n🚀 开始执行定时群发 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        accounts = self.account_manager.get_accounts_by_ids(account_ids)
        if not accounts:
            print("错误: 未找到指定的账号")
            return
        
        # 记录群发开始前的发布数量
        mass_send_start_count = self.published_groups_count
        
        # 每个账号在8:30时发布一组文章（群发）
        for account in accounts:
            if self.hybrid_stop_event.is_set():
                break
                
            # 检查是否还有文章需要发布
            if self.article_queue.empty():
                with self.hybrid_lock:
                    if self.published_groups_count >= self.total_groups:
                        print("📝 群发：所有文章已发布完成")
                        break
                continue
            
            # 获取文章组
            try:
                article_group = self.article_queue.get_nowait()
            except queue.Empty:
                print(f"📝 群发：账号 {account['name']} 没有可发布的文章")
                continue
            
            with self.hybrid_lock:
                self.current_group = self.published_groups_count + 1
            
            print(f"\n📢 群发 - 账号: {account['name']} - 第{self.current_group}组")
            
            # 发布文章组（群发模式）
            success = self._publish_article_group(
                article_paths=article_group,
                account=account,
                articles_per_publish=articles_per_publish,
                article_dir=article_dir,
                mass_send_notify=mass_send_notify  # True表示群发
            )
            
            if success:
                with self.hybrid_lock:
                    self.published_groups_count += 1
                    print(f"✅ 群发成功 - 已发布 {self.published_groups_count}/{self.total_groups} 组")
                
                # 更新群发统计
                if account['id'] in self.mass_send_stats:
                    self.mass_send_stats[account['id']]['success'] += 1
                
                # 群发模式下，账号间固定间隔10秒
                if account != accounts[-1] and not self.hybrid_stop_event.is_set():
                    print("⏱️ 群发间隔：等待10秒...")
                    self.hybrid_stop_event.wait(timeout=10)
            else:
                # 发布失败，将文章组放回队列
                self.article_queue.put(article_group)
                print("❌ 群发失败，文章组已放回队列")
                
                # 更新群发统计
                if account['id'] in self.mass_send_stats:
                    self.mass_send_stats[account['id']]['failed'] += 1
        
        # 统计本次群发结果
        mass_send_count = self.published_groups_count - mass_send_start_count
        print(f"\n🎯 本次群发完成 - 成功发布 {mass_send_count} 组文章")
        print(f"📊 总进度: {self.published_groups_count}/{self.total_groups} 组")

    def publish_articles(self, article_paths: Union[str, List[str]], account_id: int, mass_send_notify: bool = False) -> bool:
        """
        发布一篇或多篇文章（同一批次）。

        Args:
            article_paths: 待发布文章的绝对路径列表或单个路径。
            account_id: 使用的账号ID。
            mass_send_notify: 是否以群发模式发布。

        Returns:
            bool: 发布是否成功。
        """
        if isinstance(article_paths, str):
            article_paths = [article_paths]

        if not article_paths:
            print("错误: 未提供待发布文章。")
            return False

        account = self.account_manager.get_account_by_id(account_id)
        if not account:
            print(f"错误: 找不到ID为 {account_id} 的账号。")
            return False

        missing_files = [path for path in article_paths if not os.path.isfile(path)]
        if missing_files:
            print("错误: 以下文章文件不存在:")
            for missing in missing_files:
                print(f"   - {missing}")
            return False

        # 确保所有文章在同一目录下，便于图片等资源定位
        article_dirs = {os.path.dirname(path) for path in article_paths}
        if len(article_dirs) != 1:
            print("错误: 当前仅支持同一目录下的文章同时发布。")
            return False
        article_dir = article_dirs.pop()

        if not self._is_account_available(account_id):
            print(f"账号 {account_id} 当前不可用，暂时无法发布。")
            return False

        # 更新统计信息
        self.total_articles += len(article_paths)
        self.total_groups += 1
        self.current_group = self.total_groups

        success = self._publish_article_group(
            article_paths=article_paths,
            account=account,
            articles_per_publish=len(article_paths),
            article_dir=article_dir,
            mass_send_notify=mass_send_notify
        )

        if success:
            names = ", ".join(os.path.basename(path) for path in article_paths)
            print(f"✅ 文章发布成功: {names}")
        else:
            names = ", ".join(os.path.basename(path) for path in article_paths)
            print(f"❌ 文章发布失败: {names}")

        return success

    def publish_single_article(self, article_path: str, account_id: int, mass_send_notify: bool = False) -> bool:
        """兼容方法：以单篇模式发布文章。"""
        return self.publish_articles([article_path], account_id, mass_send_notify)

    def run_publish_schedule(self, article_dir: str, interval_seconds: int, 
                           total_articles: int, account_ids: List[int], articles_per_publish: int = 1, mass_send_notify: bool = False, mass_send_type: str = None):
        """
        运行发布计划
        :param article_dir: 文章目录
        :param interval_seconds: 组间发布间隔（秒）
        :param total_articles: 要发布的文章总数
        :param account_ids: 要使用的账号ID列表
        :param articles_per_publish: 每组发布的图文数（1-8之间）
        :param mass_send_notify: 是否群发
        :param mass_send_type: 群发类型（timed/ immediate/ None）
        """
        # 如果是群发模式
        if mass_send_notify:
            if mass_send_type == 'timed':
                self._run_mass_send_schedule(article_dir, account_ids, articles_per_publish, total_articles, mass_send_notify)
                return
            elif mass_send_type == 'immediate':
                self._run_immediate_mass_send_schedule(article_dir, account_ids, articles_per_publish, total_articles, interval_seconds, mass_send_notify)
                return
            elif mass_send_type == 'hybrid_timed':
                self._run_hybrid_timed_schedule(article_dir, account_ids, articles_per_publish, total_articles, interval_seconds, mass_send_notify)
                return
            else:
                print("错误: 未指定群发类型，无法执行群发。"); return
        # 非群发模式，走原有逻辑
        # 1. 准备工作
        print("\n--- 开始发布计划 ---")
        print(f"文章目录: {article_dir}")
        print(f"组间间隔: {interval_seconds}秒")
        print(f"文章总数: {total_articles}")
        print(f"每组图文数: {articles_per_publish}")
        print(f"使用账号ID: {account_ids}")
        
        # 2. 获取账号信息
        if not account_ids:
            print("错误: 未指定有效的账号")
            return
            
        accounts = self.account_manager.get_accounts_by_ids(account_ids)
        if not accounts:
            print("错误: 未找到指定的账号")
            return
            
        print(f"使用账号: {', '.join(acc['name'] for acc in accounts)}")
        
        # 3. 重置统计信息
        self.total_articles = 0
        self.success_articles = 0
        self.failed_articles = 0
        self.failed_paths = []
        self.total_groups = 0
        self.current_group = 0
        
        try:
            while True:
                all_articles = [os.path.join(article_dir, f) for f in os.listdir(article_dir) if f.endswith('.md')]
                if not all_articles:
                    print("没有要发布的文章。")
                    break
                
                selected_files = random.sample(all_articles, min(articles_per_publish, len(all_articles)))
                
                self.total_groups += 1
                self.current_group = self.total_groups
                
                print(f"\n第 {self.current_group} 组将发布 {len(selected_files)} 篇文章:")
                for idx, article in enumerate(selected_files, 1):
                    print(f"  {idx}. {os.path.basename(article)}")
                print()
                
                available_accounts = []
                for acc in accounts:
                    if self._is_account_available(acc['id']):
                        available_accounts.append(acc)
                
                if not available_accounts:
                    print("当前没有可用账号，等待5分钟后重试...")
                    time.sleep(300)
                    for acc in accounts:
                        if self._is_account_available(acc['id']):
                            available_accounts.append(acc)
                    if not available_accounts:
                        print("仍然没有可用账号，跳过当前组")
                        self.failed_articles += len(selected_files)
                        self.failed_paths.extend(selected_files)
                        continue
                
                current_account = available_accounts[(self.current_group - 1) % len(available_accounts)]
                print(f"本组使用账号: {current_account['name']}")
                
                self._publish_article_group(selected_files, current_account, articles_per_publish, article_dir, mass_send_notify=mass_send_notify)
                
                self.total_articles += len(selected_files)
                
                print(f"\n等待 {interval_seconds/60:.1f} 分钟后处理下一组...")
                time.sleep(interval_seconds)
                    
        finally:
            self.display_summary()
                
        print("\n发布计划执行完成!")