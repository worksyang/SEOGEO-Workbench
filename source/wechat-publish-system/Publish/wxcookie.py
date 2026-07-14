#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
# 🤖微信公众号 Cookie 登录器
1️⃣ 登录操作  
   └─ 🖱️ 自动启动浏览器并跳转扫码页  
2️⃣ Cookie与Token管理  
   ├─ 💾 首次扫码后抓取并本地保存登录Cookie/Token  
   └─ 🔄 后续自动读取本地Cookie复用免扫码  
3️⃣ 遇到失效  
   └─ ♻️ 支持用户重新扫码并更新Cookie  
"""

import os
import re
import time
import yaml
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException

# 配置路径（直接保存在项目根目录，减少文件数量）
# 如果从 Publish 目录导入，需要指向项目根目录
import os
_current_dir = os.path.dirname(os.path.abspath(__file__))
_project_root = os.path.dirname(_current_dir) if os.path.basename(_current_dir) == 'Publish' else _current_dir
CONFIG_FILE = os.path.join(_project_root, "wxcookie.yaml")
WX_LOGIN_URL = "https://mp.weixin.qq.com/"
WX_HOME_URL = "https://mp.weixin.qq.com/cgi-bin/home"


class WxCookieManager:
    """微信公众号 Cookie 管理器"""
    
    def __init__(self, browser_type="chrome"):
        """
        初始化
        :param browser_type: 浏览器类型，'chrome' 或 'firefox'
        """
        self.browser_type = browser_type.lower()
        self.driver = None
        self.config_file = CONFIG_FILE
        self.config = {}
        
        # 加载配置（不再创建目录，直接使用项目根目录）
        self.load_config()
        
    def load_config(self):
        """加载配置文件，并自动迁移旧格式"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    self.config = yaml.safe_load(f) or {}
            except Exception as e:
                print(f"加载配置文件失败: {e}")
                self.config = {}
        else:
            self.config = {}
        
        # 自动迁移旧格式配置到新格式
        self._migrate_config_if_needed()
    
    def _migrate_config_if_needed(self):
        """迁移旧格式配置到新格式（多账号格式）"""
        # 检查是否是旧格式（直接有cookies字段，没有accounts字段）
        if 'cookies' in self.config and 'accounts' not in self.config:
            print("🔄 检测到旧格式配置，正在迁移到多账号格式...")
            
            # 创建账号数据
            account_data = {
                'cookies': self.config.get('cookies', []),
                'cookies_str': self.config.get('cookies_str', ''),
                'token': self.config.get('token'),
                'expiry': self.config.get('expiry'),
                'last_login': self.config.get('last_login'),
            }
            
            # 生成账号ID和昵称
            account_id = 'account_001'
            nickname = '我的公众号'  # 默认昵称
            
            # 尝试从cookies中提取昵称（从slave_user cookie的value中提取）
            nickname = self._extract_nickname_from_cookies(
                account_data['cookies'], 
                default_nickname=nickname
            )
            account_data['nickname'] = nickname
            
            # 创建新格式
            self.config = {
                'accounts': {
                    account_id: account_data
                }
            }
            
            # 保存新格式
            self.save_config()
            print(f"✅ 已迁移到多账号格式，账号ID: {account_id}，昵称: {nickname}")
    
    def save_config(self):
        """保存配置文件"""
        try:
            # 移除current_account字段（如果存在）
            if 'current_account' in self.config:
                del self.config['current_account']
            
            with open(self.config_file, 'w', encoding='utf-8') as f:
                yaml.dump(self.config, f, allow_unicode=True, default_flow_style=False)
            print(f"✅ 配置已保存到: {self.config_file}")
        except Exception as e:
            print(f"❌ 保存配置文件失败: {e}")
    
    def _generate_account_id(self):
        """生成新的账号ID（辅助方法）"""
        if 'accounts' not in self.config:
            return 'account_001'
        
        existing_ids = list(self.config['accounts'].keys())
        if not existing_ids:
            return 'account_001'
        
        max_num = 0
        for aid in existing_ids:
            if aid.startswith('account_'):
                try:
                    num = int(aid.split('_')[1])
                    max_num = max(max_num, num)
                except:
                    pass
        return f'account_{max_num + 1:03d}'
    
    def _extract_nickname_from_cookies(self, cookies, default_nickname=None):
        """
        从cookies中提取昵称（辅助方法）
        :param cookies: cookie列表
        :param default_nickname: 默认昵称，如果提取失败则使用此值
        :return: 昵称字符串
        """
        if not cookies:
            return default_nickname or '未知账号'
        
        for cookie in cookies:
            if cookie.get('name') == 'slave_user' and cookie.get('value'):
                nickname = cookie['value'][:20]  # 限制长度
                return nickname
        
        return default_nickname or '未知账号'
    
    def _calculate_cookie_expiry(self, cookies):
        """
        计算cookie过期时间（辅助方法）
        :param cookies: cookie列表
        :return: 过期时间字典，如果未找到则返回None
        """
        for cookie in cookies:
            if cookie.get('name') == 'slave_sid' and 'expiry' in cookie:
                try:
                    expiry_time = float(cookie['expiry'])
                    remaining_time = expiry_time - time.time()
                    if remaining_time > 0:
                        return {
                            'expiry_timestamp': expiry_time,
                            'remaining_seconds': int(remaining_time),
                            'expiry_time': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expiry_time))
                        }
                    break
                except ValueError:
                    pass
        return None
    
    def _format_expiry_time(self, remaining_seconds):
        """
        格式化过期时间显示（辅助方法）
        :param remaining_seconds: 剩余秒数
        :return: 格式化的时间字符串
        """
        if remaining_seconds <= 0:
            return ""
        
        hours = remaining_seconds // 3600
        days = hours // 24
        
        if days > 0:
            return f" | 剩余 {days} 天 {hours % 24} 小时"
        else:
            return f" | 剩余 {hours} 小时"
    
    def clear_browser_storage(self):
        """
        完整清除：清除浏览器中的所有存储（Cookies、LocalStorage、SessionStorage）
        用于解决Cookie失效后二维码无法刷新的问题
        """
        if not self.driver:
            return False
        
        try:
            print("🧹 正在清除浏览器存储...")
            
            # 获取当前域名
            current_url = self.driver.current_url if self.driver.current_url else WX_LOGIN_URL
            domain = '.weixin.qq.com'
            
            # 1. 清除所有 Cookies
            try:
                cookies = self.driver.get_cookies()
                for cookie in cookies:
                    try:
                        # 删除cookie
                        self.driver.delete_cookie(cookie['name'])
                    except:
                        pass
                print(f"   ✅ 已清除 {len(cookies)} 个 Cookie")
            except Exception as e:
                print(f"   ⚠️  清除 Cookie 时出错: {e}")
            
            # 2. 清除 LocalStorage
            try:
                self.driver.execute_script("""
                    try {
                        localStorage.clear();
                        return true;
                    } catch(e) {
                        return false;
                    }
                """)
                print("   ✅ 已清除 LocalStorage")
            except Exception as e:
                print(f"   ⚠️  清除 LocalStorage 时出错: {e}")
            
            # 3. 清除 SessionStorage
            try:
                self.driver.execute_script("""
                    try {
                        sessionStorage.clear();
                        return true;
                    } catch(e) {
                        return false;
                    }
                """)
                print("   ✅ 已清除 SessionStorage")
            except Exception as e:
                print(f"   ⚠️  清除 SessionStorage 时出错: {e}")
            
            # 4. 清除 IndexedDB（如果存在）
            try:
                self.driver.execute_script("""
                    try {
                        indexedDB.databases().then(databases => {
                            databases.forEach(db => {
                                indexedDB.deleteDatabase(db.name);
                            });
                        });
                        return true;
                    } catch(e) {
                        return false;
                    }
                """)
            except:
                pass
            
            print("✅ 浏览器存储清除完成")
            return True
            
        except Exception as e:
            print(f"❌ 清除浏览器存储失败: {e}")
            return False
    
    def extract_token(self, driver):
        """从页面中提取 token"""
        try:
            # 方法1: 从当前URL获取token
            current_url = driver.current_url
            token_match = re.search(r'token=([^&]+)', current_url)
            if token_match:
                return token_match.group(1)
            
            # 方法2: 从localStorage获取
            token = driver.execute_script("return localStorage.getItem('token');")
            if token:
                return token
                
            # 方法3: 从sessionStorage获取
            token = driver.execute_script("return sessionStorage.getItem('token');")
            if token:
                return token
                
            # 方法4: 从cookie中查找
            cookies = driver.get_cookies()
            for cookie in cookies:
                if 'token' in cookie['name'].lower():
                    return cookie['value']
                    
            return None
        except Exception as e:
            print(f"提取token时出错: {str(e)}")
            return None
    
    def start_browser(self, headless=False):
        """
        启动浏览器
        :param headless: 是否使用无头模式（不显示浏览器窗口）
        """
        try:
            if self.browser_type == "chrome":
                # Chrome 浏览器配置
                options = ChromeOptions()
                # 根据参数决定是否使用无头模式
                if headless:
                    options.add_argument('--headless')
                options.add_argument('--no-sandbox')
                options.add_argument('--disable-dev-shm-usage')
                options.add_argument('--disable-blink-features=AutomationControlled')
                options.add_experimental_option("excludeSwitches", ["enable-automation"])
                options.add_experimental_option('useAutomationExtension', False)
                
                # 尝试启动 Chrome
                try:
                    self.driver = webdriver.Chrome(options=options)
                except WebDriverException:
                    print("⚠️  未找到 Chrome 浏览器，尝试使用 Firefox...")
                    self.browser_type = "firefox"
                    return self.start_browser(headless=headless)
            
            elif self.browser_type == "firefox":
                # Firefox 浏览器配置
                options = FirefoxOptions()
                # 根据参数决定是否使用无头模式
                if headless:
                    options.add_argument('--headless')
                
                try:
                    self.driver = webdriver.Firefox(options=options)
                except WebDriverException as e:
                    print(f"❌ Firefox 启动失败: {e}")
                    print("请确保已安装 Firefox 浏览器和 geckodriver")
                    raise
            
            else:
                raise ValueError(f"不支持的浏览器类型: {self.browser_type}")
            
            mode_text = "无头模式" if headless else "正常模式"
            print(f"✅ {self.browser_type.upper()} 浏览器启动成功 ({mode_text})")
            return True
            
        except Exception as e:
            print(f"❌ 浏览器启动失败: {e}")
            return False
    
    def load_cookies(self, account_id):
        """加载已保存的 Cookie"""
        if not self.driver:
            return False
        
        if not account_id:
            print("📝 未指定账号ID")
            return False
        
        # 获取账号配置
        if 'accounts' not in self.config:
            print("📝 未找到已保存的 Cookie，需要重新登录")
            return False
        
        account = self.config['accounts'].get(account_id)
        if not account:
            print("📝 未找到指定账号的 Cookie，需要重新登录")
            return False
        
        cookies = account.get('cookies', [])
        if not cookies:
            print("📝 未找到已保存的 Cookie，需要重新登录")
            return False
        
        try:
            # 先访问登录页面
            self.driver.get(WX_LOGIN_URL)
            time.sleep(2)
            
            # 添加 Cookie
            success_count = 0
            for cookie in cookies:
                try:
                    # 删除可能有问题的字段，只保留必要的字段
                    cookie_to_add = {}
                    if 'name' in cookie:
                        cookie_to_add['name'] = cookie['name']
                    if 'value' in cookie:
                        cookie_to_add['value'] = cookie['value']
                    if 'domain' in cookie:
                        cookie_to_add['domain'] = cookie['domain']
                    else:
                        # 如果没有domain，设置默认值
                        cookie_to_add['domain'] = '.weixin.qq.com'
                    
                    if 'path' in cookie:
                        cookie_to_add['path'] = cookie['path']
                    else:
                        cookie_to_add['path'] = '/'
                    
                    # expiry 字段可能不存在，如果存在且已过期则跳过
                    if 'expiry' in cookie:
                        expiry_time = float(cookie['expiry'])
                        if expiry_time < time.time():
                            continue  # 跳过已过期的 Cookie
                        cookie_to_add['expiry'] = int(expiry_time)
                    
                    self.driver.add_cookie(cookie_to_add)
                    success_count += 1
                except Exception as e:
                    # 静默处理，避免打印太多警告
                    pass
            
            print(f"📦 已加载 {success_count} 个 Cookie")
            
            # 刷新页面
            self.driver.refresh()
            time.sleep(3)
            
            # 检查是否登录成功
            if "home" in self.driver.current_url:
                print("✅ 使用已保存的 Cookie 登录成功！")
                return True
            else:
                print("⚠️  Cookie 已过期或无效，需要重新登录")
                # 注意：不在这里清除存储，由调用者（login方法）统一处理
                return False
                
        except Exception as e:
            print(f"❌ 加载 Cookie 失败: {e}")
            return False
    
    def save_cookies(self, account_id):
        """保存 Cookie 和 Token"""
        if not self.driver:
            print("❌ 浏览器未启动")
            return False
        
        if not account_id:
            print("❌ 未指定账号ID")
            return False
        
        # 确保accounts存在
        if 'accounts' not in self.config:
            self.config['accounts'] = {}
        
        if account_id not in self.config['accounts']:
            self.config['accounts'][account_id] = {}
        
        try:
            
            account = self.config['accounts'][account_id]
            
            # 获取所有 Cookie
            cookies = self.driver.get_cookies()
            
            # 拼接 Cookie 字符串（用于直接使用）
            cookies_str = ""
            for cookie in cookies:
                cookies_str += f"{cookie['name']}={cookie['value']}; "
            
            # 提取 Token
            token = self.extract_token(self.driver)
            
            # 计算 slave_sid cookie 有效时间
            cookie_expiry = self._calculate_cookie_expiry(cookies)
            
            # 保存到账号配置
            account['cookies'] = cookies
            account['cookies_str'] = cookies_str.strip()
            account['token'] = token
            account['expiry'] = cookie_expiry
            account['last_login'] = time.strftime('%Y-%m-%d %H:%M:%S')
            
            # 如果没有昵称，尝试从cookie中提取
            if 'nickname' not in account or not account['nickname']:
                default_nickname = f'账号{account_id.split("_")[1]}'
                account['nickname'] = self._extract_nickname_from_cookies(cookies, default_nickname)
            
            self.save_config()
            
            # 打印信息
            print("\n" + "="*50)
            print("📦 保存的信息：")
            print(f"   账号: {account.get('nickname', account_id)} ({account_id})")
            print(f"   Cookie 数量: {len(cookies)}")
            if token:
                print(f"   Token: {token}")
            if cookie_expiry:
                print(f"   过期时间: {cookie_expiry['expiry_time']}")
                remaining_seconds = cookie_expiry['remaining_seconds']
                expiry_text = self._format_expiry_time(remaining_seconds)
                if expiry_text:
                    # 移除前缀 " | 剩余"，只保留时间部分
                    time_text = expiry_text.replace(" | 剩余 ", "")
                    print(f"   剩余时间: {time_text}")
            print("="*50 + "\n")
            
            return True
            
        except Exception as e:
            print(f"❌ 保存 Cookie 失败: {e}")
            return False
    
    def login(self, account_id=None):
        """
        登录流程（新版本）
        1. 如果account_id为None，让用户选择账号
        2. 选择账号后，自动检测cookie是否有效
        3. 如果有效，直接打开账号（保持浏览器打开）
        4. 如果无效，执行登录流程
        """
        print("\n" + "="*50)
        print("🚀 开始微信公众号登录流程")
        print("="*50 + "\n")
        
        # 加载配置
        self.load_config()
        
        # 如果没有指定账号ID，让用户选择账号
        if account_id is None:
            account_id = self.select_account()
            if account_id == 'cancel':
                # 用户选择取消，返回主菜单
                print("\n已返回主菜单")
                return False
            # 如果account_id是None，表示需要创建新账号（继续执行后面的创建逻辑）
        
        # 如果没有账号，创建新账号
        if not account_id or 'accounts' not in self.config or account_id not in self.config['accounts']:
            print("📝 未找到账号，将创建新账号")
            if 'accounts' not in self.config:
                self.config['accounts'] = {}
            
            # 生成新账号ID
            account_id = self._generate_account_id()
            
            # 创建新账号
            self.config['accounts'][account_id] = {
                'nickname': f'账号{account_id.split("_")[1]}',
                'cookies': [],
                'cookies_str': '',
                'token': None,
                'expiry': None,
                'last_login': None
            }
        
        # 显示账号信息
        account = self.config['accounts'].get(account_id)
        nickname = account.get('nickname', account_id) if account else account_id
        print(f"\n📱 已选择账号: {nickname} ({account_id})")
        
        # 检查是否有保存的Cookie
        has_cookies = account and account.get('cookies', [])
        
        if has_cookies:
            # 有Cookie，先快速检测是否有效（使用无头模式）
            print("\n🔍 正在检测 Cookie 有效性...")
            if not self.start_browser(headless=True):
                print("❌ 浏览器启动失败")
                return False
            
            try:
                # 快速检测cookie是否有效
                is_valid = self.load_cookies(account_id)
                self.close()  # 关闭无头浏览器
                
                if is_valid:
                    # Cookie有效，启动正常浏览器打开账号
                    print("✅ Cookie 有效！正在打开账号...")
                    if not self.start_browser():
                        return False
                    
                    # 再次加载Cookie（这次使用正常浏览器）
                    if self.load_cookies(account_id):
                        print("✅ 账号已打开！浏览器将保持打开状态")
                        print("💡 提示：您可以关闭浏览器窗口，Cookie 已保存")
                        return True
                    else:
                        print("⚠️  Cookie 检测时有效，但打开时失败，需要重新登录")
                else:
                    print("⚠️  Cookie 已失效，需要重新登录")
            except Exception as e:
                print(f"⚠️  Cookie 检测出错: {e}，需要重新登录")
                try:
                    self.close()
                except:
                    pass
        
        # Cookie无效或没有Cookie，需要重新登录
        # 启动浏览器
        if not self.start_browser():
            return False
        
        try:
            # 如果没有Cookie，尝试加载一次（虽然不太可能成功，但保持兼容性）
            # 如果Cookie已检测为无效，直接进入登录流程
            if not has_cookies:
                if self.load_cookies(account_id):
                    print("✅ 登录成功！浏览器将保持打开状态")
                    print("💡 提示：您可以关闭浏览器窗口，Cookie 已保存")
                    return True
            
            # 需要重新登录
            print("\n" + "="*50)
            print("📱 请扫码登录微信公众号")
            print("="*50 + "\n")
            
            # 清除所有浏览器存储，确保二维码能正常加载（只清除一次）
            print("🧹 正在清除浏览器存储以确保二维码正常加载...")
            self.clear_browser_storage()
            
            # 打开登录页面（清除存储后再打开，确保页面干净）
            print("🌐 浏览器已打开登录页面，请扫码登录...")
            self.driver.get(WX_LOGIN_URL)
            time.sleep(3)  # 等待页面加载
            
            # 检查是否有二维码加载失败的情况
            try:
                page_source = self.driver.page_source
                # 检查是否真的加载失败（而不是正常显示）
                if '二维码加载失败' in page_source or '点击刷新' in page_source:
                    print("⚠️  检测到二维码加载失败，正在清除存储并刷新...")
                    self.clear_browser_storage()
                    self.driver.refresh()
                    time.sleep(2)
            except:
                pass
            
            # 等待登录成功（检测 URL 变化）
            wait = WebDriverWait(self.driver, 300)  # 最多等待5分钟
            wait.until(EC.url_contains("home"))
            
            print("✅ 登录成功！")
            
            # 保存 Cookie 和 Token（保存到指定账号）
            time.sleep(2)  # 等待页面完全加载
            self.save_cookies(account_id)
            
            print("✅ 登录信息已保存！浏览器将保持打开状态")
            print("💡 提示：您可以关闭浏览器窗口，下次运行会自动使用保存的 Cookie")
            
            return True
            
        except TimeoutException:
            print("\n❌ 登录超时（5分钟），请重新运行程序")
            return False
        except Exception as e:
            print(f"\n❌ 登录过程出错: {e}")
            return False
        # 注意：不自动关闭浏览器，让用户可以看到登录状态
    
    def check_cookie_validity(self, account_id):
        """
        检测 Cookie 是否有效（使用和登录一样的逻辑）
        直接调用 load_cookies() 方法来判断，和登录流程完全一致
        """
        print("\n" + "="*50)
        print("🔍 开始检测 Cookie 有效性...")
        print("="*50 + "\n")
        
        if not account_id:
            print("❌ 未指定账号ID")
            return False
        
        # 加载配置
        self.load_config()
        
        # 检查是否有保存的 Cookie
        if 'accounts' not in self.config:
            print("❌ 未找到已保存的 Cookie，请先登录")
            return False
        
        account = self.config['accounts'].get(account_id)
        if not account:
            print(f"❌ 未找到账号 {account_id} 的 Cookie，请先登录")
            return False
        
        cookies_list = account.get('cookies', [])
        if not cookies_list:
            print("❌ 未找到已保存的 Cookie，请先登录")
            return False
        
        # 启动浏览器（使用无头模式，不显示浏览器窗口）
        if not self.start_browser(headless=True):
            print("❌ 浏览器启动失败")
            return False
        
        try:
            # 直接使用和登录一样的逻辑：调用 load_cookies() 方法
            # load_cookies() 会：访问登录页面 -> 加载Cookie -> 刷新页面 -> 检查URL是否包含home
            is_valid = self.load_cookies(account_id)
            
            # 输出结果
            print("\n" + "="*50)
            if is_valid:
                print("✅ Cookie 有效！")
                nickname = account.get('nickname', account_id)
                print(f"   账号: {nickname} ({account_id})")
                
                # 显示过期信息
                expiry_info = account.get('expiry', {})
                if expiry_info:
                    expiry_time = expiry_info.get('expiry_time', '未知')
                    remaining_seconds = expiry_info.get('remaining_seconds', 0)
                    print(f"   过期时间: {expiry_time}")
                    if remaining_seconds > 0:
                        expiry_text = self._format_expiry_time(remaining_seconds)
                        if expiry_text:
                            # 移除前缀 " | 剩余"，只保留时间部分
                            time_text = expiry_text.replace(" | 剩余 ", "")
                            print(f"   剩余时间: {time_text}")
                
                last_login = account.get('last_login', '未知')
                print(f"   最后登录: {last_login}")
            else:
                print("❌ Cookie 已失效，请重新登录")
                print(f"   当前URL: {self.driver.current_url}")
                print("   提示: Cookie 已过期或无效")
            print("="*50 + "\n")
            
            return is_valid
            
        except Exception as e:
            print(f"\n❌ 检测过程出错: {e}")
            import traceback
            traceback.print_exc()
            return False
        finally:
            # 关闭浏览器
            self.close()
    
    def check_all_cookies_validity(self):
        """
        批量检测账号的Cookie有效性
        用户可以选择要检测的账号，或直接回车检测所有账号
        依次检测每个账号，并更新状态到yaml文件
        """
        print("\n" + "="*50)
        print("🔍 Cookie 有效性检测")
        print("="*50 + "\n")
        
        # 加载配置
        self.load_config()
        
        # 检查是否有账号
        if 'accounts' not in self.config or not self.config['accounts']:
            print("📝 暂无账号，请先登录")
            return
        
        accounts = self.config['accounts']
        
        # 显示账号列表供用户选择
        print("📋 账号列表：")
        print("-" * 50)
        account_list = []
        for idx, (account_id, account) in enumerate(accounts.items(), 1):
            nickname = account.get('nickname', account_id)
            cookies = account.get('cookies', [])
            status = "✅ 已登录" if cookies else "❌ 未登录"
            
            # 显示过期时间
            expiry_info = account.get('expiry', {})
            expiry_text = self._format_expiry_time(
                expiry_info.get('remaining_seconds', 0) if expiry_info else 0
            )
            
            print(f"{idx}. {nickname} ({account_id}) - {status}{expiry_text}")
            
            account_list.append({
                'id': account_id,
                'nickname': nickname,
                'account': account
            })
        
        print("-" * 50)
        print(f"\n💡 提示：输入账号编号检测指定账号，直接回车检测所有账号\n")
        
        # 让用户选择要检测的账号
        selected_accounts = []
        try:
            user_input = input("请输入要检测的账号编号（直接回车检测所有）: ").strip()
            
            if user_input == "":
                # 直接回车，检测所有账号
                selected_accounts = account_list
                print("\n✅ 将检测所有账号...\n")
            else:
                # 用户输入了数字，检测指定账号
                try:
                    idx = int(user_input) - 1
                    if 0 <= idx < len(account_list):
                        selected_accounts = [account_list[idx]]
                        print(f"\n✅ 将检测账号: {account_list[idx]['nickname']}\n")
                    else:
                        print(f"❌ 无效的账号编号，请输入 1-{len(account_list)} 之间的数字")
                        return
                except ValueError:
                    print("❌ 请输入有效的数字")
                    return
        except KeyboardInterrupt:
            print("\n\n❌ 已取消操作")
            return
        
        if not selected_accounts:
            print("❌ 未选择要检测的账号")
            return
        
        # 开始检测
        print("="*50)
        print("🔍 开始检测 Cookie 有效性...")
        print("="*50 + "\n")
        
        total_count = len(selected_accounts)
        valid_count = 0
        invalid_count = 0
        no_cookie_count = 0
        
        # 依次检测每个账号
        for idx, account_info in enumerate(selected_accounts, 1):
            account_id = account_info['id']
            nickname = account_info['nickname']
            account = account_info['account']
            cookies_list = account.get('cookies', [])
            
            print(f"\n[{idx}/{total_count}] 正在检测账号: {nickname} ({account_id})")
            print("-" * 50)
            
            # 如果没有Cookie，跳过检测
            if not cookies_list:
                print(f"   ⚠️  账号 {nickname} 未保存Cookie，跳过检测")
                no_cookie_count += 1
                # 更新状态：将expiry设为None
                account['expiry'] = None
                continue
            
            # 启动浏览器（使用无头模式）
            if not self.start_browser(headless=True):
                print(f"   ❌ 浏览器启动失败，跳过账号 {nickname}")
                continue
            
            try:
                # 检测Cookie有效性
                is_valid = self.load_cookies(account_id)
                
                if is_valid:
                    # Cookie有效，更新expiry信息（从driver中获取最新的cookie信息）
                    print(f"   ✅ Cookie 有效！")
                    
                    # 重新获取cookie信息以更新expiry
                    try:
                        cookies = self.driver.get_cookies()
                        cookie_expiry = self._calculate_cookie_expiry(cookies)
                        
                        # 更新账号的expiry信息
                        if cookie_expiry:
                            account['expiry'] = cookie_expiry
                            remaining_seconds = cookie_expiry.get('remaining_seconds', 0)
                            expiry_text = self._format_expiry_time(remaining_seconds)
                            if expiry_text:
                                # 移除前缀 " | 剩余"，只保留时间部分
                                time_text = expiry_text.replace(" | 剩余 ", "")
                                print(f"   📅 剩余时间: {time_text}")
                        else:
                            print(f"   ⚠️  无法获取过期时间信息")
                    except Exception as e:
                        print(f"   ⚠️  更新expiry信息时出错: {e}")
                    
                    valid_count += 1
                else:
                    # Cookie失效，更新expiry为None
                    print(f"   ❌ Cookie 已失效")
                    account['expiry'] = None
                    invalid_count += 1
                    
            except Exception as e:
                print(f"   ❌ 检测过程出错: {e}")
                invalid_count += 1
            finally:
                # 关闭浏览器，准备检测下一个账号
                self.close()
        
        # 打印检测汇总
        print("\n" + "="*50)
        print("📊 检测汇总")
        print("="*50)
        print(f"   总账号数: {total_count}")
        print(f"   ✅ 有效: {valid_count}")
        print(f"   ❌ 失效: {invalid_count}")
        print(f"   ⚠️  无Cookie: {no_cookie_count}")
        print("="*50 + "\n")
        
        # 最终保存配置（确保所有更新都已保存）
        self.save_config()
        print("✅ 所有账号状态已更新到配置文件")
    
    def clear_saved_cookies(self, account_id):
        """
        清除保存的 Cookie 配置（不删除文件，只清空内容）
        用于解决cookie失效问题
        """
        print("\n" + "="*50)
        print("🧹 清除保存的 Cookie 配置")
        print("="*50 + "\n")
        
        if not account_id:
            print("❌ 未指定账号ID")
            return False
        
        # 加载配置
        self.load_config()
        
        if 'accounts' not in self.config:
            print("📝 未找到已保存的 Cookie，无需清除")
            return True
        
        account = self.config['accounts'].get(account_id)
        if not account or not account.get('cookies'):
            print("📝 未找到已保存的 Cookie，无需清除")
            return True
        
        try:
            # 清空cookie相关配置
            account['cookies'] = []
            account['cookies_str'] = ''
            account['token'] = None
            account['expiry'] = None
            account['last_login'] = None
            
            # 保存配置
            self.save_config()
            
            nickname = account.get('nickname', account_id)
            print(f"✅ 已清除账号 {nickname} ({account_id}) 的 Cookie 和 Token")
            print("💡 下次登录时将重新获取新的 Cookie")
            return True
            
        except Exception as e:
            print(f"❌ 清除配置失败: {e}")
            return False
    
    def list_accounts(self):
        """列出所有账号"""
        self.load_config()
        
        if 'accounts' not in self.config or not self.config['accounts']:
            print("\n📝 暂无账号，请先登录")
            return []
        
        accounts = self.config['accounts']
        print("\n" + "="*50)
        print("📋 账号列表")
        print("="*50)
        
        account_list = []
        for idx, (account_id, account) in enumerate(accounts.items(), 1):
            nickname = account.get('nickname', account_id)
            
            # 检查Cookie状态
            cookies = account.get('cookies', [])
            status = "✅ 已登录" if cookies else "❌ 未登录"
            
            # 显示过期时间
            expiry_info = account.get('expiry', {})
            expiry_text = self._format_expiry_time(
                expiry_info.get('remaining_seconds', 0) if expiry_info else 0
            )
            
            last_login = account.get('last_login', '未知')
            
            print(f"{idx}. {nickname} ({account_id})")
            print(f"   状态: {status}{expiry_text}")
            print(f"   最后登录: {last_login}")
            print()
            
            account_list.append({
                'id': account_id,
                'nickname': nickname,
                'has_cookies': bool(cookies)
            })
        
        print("="*50 + "\n")
        return account_list
    
    def select_account(self):
        """
        选择账号（用于登录）
        返回选中的账号ID，如果取消则返回'cancel'，如果需要创建新账号则返回None
        """
        self.load_config()
        
        # 获取账号列表
        accounts = self.list_accounts()
        
        if not accounts:
            # 没有账号，返回None表示需要创建新账号
            print("\n📝 暂无账号，将创建新账号")
            return None
        
        # 如果有账号，让用户选择
        print("\n请选择要登录的账号：")
        print("  输入账号编号选择账号")
        print("  输入 0 创建新账号")
        print("  输入 q 返回主菜单")
        
        while True:
            try:
                choice = input("\n请输入选项: ").strip().lower()
                
                if choice == 'q':
                    return 'cancel'  # 返回'cancel'表示取消，返回主菜单
                
                if choice == '0':
                    return None  # 返回None表示创建新账号
                
                idx = int(choice) - 1
                if 0 <= idx < len(accounts):
                    selected_account = accounts[idx]
                    return selected_account['id']
                else:
                    print("❌ 无效的账号编号，请重新选择")
            except ValueError:
                print("❌ 请输入有效的数字")
            except KeyboardInterrupt:
                return 'cancel'
    
    def add_account(self, nickname=None):
        """添加新账号"""
        self.load_config()
        
        if 'accounts' not in self.config:
            self.config['accounts'] = {}
        
        # 生成新账号ID
        account_id = self._generate_account_id()
        
        # 如果没有提供昵称，使用默认值
        if not nickname:
            nickname = f'账号{account_id.split("_")[1]}'
        
        # 创建新账号
        self.config['accounts'][account_id] = {
            'nickname': nickname,
            'cookies': [],
            'cookies_str': '',
            'token': None,
            'expiry': None,
            'last_login': None
        }
        
        self.save_config()
        
        print(f"\n✅ 已创建新账号: {nickname} ({account_id})")
        return account_id
    
    def delete_account(self, account_id):
        """删除账号"""
        self.load_config()
        
        if 'accounts' not in self.config or account_id not in self.config['accounts']:
            print(f"❌ 账号 {account_id} 不存在")
            return False
        
        account = self.config['accounts'][account_id]
        nickname = account.get('nickname', account_id)
        
        # 确认删除
        print(f"\n⚠️  确定要删除账号 {nickname} ({account_id}) 吗？")
        print("   此操作不可恢复！")
        confirm = input("   请输入 'yes' 确认: ").strip().lower()
        
        if confirm != 'yes':
            print("❌ 已取消删除")
            return False
        
        # 删除账号
        del self.config['accounts'][account_id]
        
        self.save_config()
        
        print(f"✅ 已删除账号: {nickname} ({account_id})")
        return True
    
    def rename_account(self, account_id, new_nickname):
        """重命名账号"""
        self.load_config()
        
        if 'accounts' not in self.config or account_id not in self.config['accounts']:
            print(f"❌ 账号 {account_id} 不存在")
            return False
        
        old_nickname = self.config['accounts'][account_id].get('nickname', account_id)
        self.config['accounts'][account_id]['nickname'] = new_nickname
        
        self.save_config()
        
        print(f"✅ 账号已重命名: {old_nickname} → {new_nickname}")
        return True
    
    def manage_accounts(self):
        """账号管理菜单"""
        while True:
            print("\n" + "="*50)
            print("👤 账号管理")
            print("="*50)
            print("请选择操作：")
            print("  1. 添加账号")
            print("  2. 删除账号")
            print("  0. 返回主菜单")
            print("="*50)
            
            choice = input("\n请输入选项 (0-2): ").strip()
            
            if choice == "1":
                nickname = input("\n请输入账号昵称（直接回车使用默认名称）: ").strip()
                if not nickname:
                    nickname = None
                self.add_account(nickname)
                input("\n按 Enter 键继续...")
            
            elif choice == "2":
                accounts = self.list_accounts()
                if not accounts:
                    input("\n按 Enter 键继续...")
                    continue
                
                try:
                    idx = input("\n请输入要删除的账号编号（输入 0 返回上一级）: ").strip()
                    if idx == "0":
                        continue  # 返回上一级
                    idx = int(idx) - 1
                    if 0 <= idx < len(accounts):
                        account_id = accounts[idx]['id']
                        self.delete_account(account_id)
                    else:
                        print("❌ 无效的账号编号")
                except ValueError:
                    print("❌ 请输入有效的数字")
                except Exception as e:
                    print(f"❌ 删除失败: {e}")
                
                input("\n按 Enter 键继续...")
            
            elif choice == "0":
                break
            
            else:
                print("\n⚠️  无效的选项，请重新选择\n")
    
    def close(self):
        """关闭浏览器"""
        if self.driver:
            try:
                self.driver.quit()
                print("✅ 浏览器已关闭")
            except:
                pass


def show_menu():
    """显示菜单"""
    print("\n" + "="*50)
    print("📱 微信公众号 Cookie 管理工具")
    print("="*50)
    print("请选择操作：")
    print("  1. 登录（选择账号后自动检测Cookie，有效则直接打开，无效则登录）")
    print("  2. 检测 Cookie 有效性（可选择账号或检测所有）")
    print("  3. 账号管理")
    print("  4. 清除指定账号的 Cookie 配置")
    print("  0. 退出")
    print("="*50)


def main():
    """主函数"""
    import sys
    
    # 检查浏览器类型参数
    browser_type = "chrome"
    if len(sys.argv) > 1:
        browser_type = sys.argv[1].lower()
        if browser_type not in ["chrome", "firefox"]:
            print("⚠️  不支持的浏览器类型，使用默认 Chrome")
            browser_type = "chrome"
    
    manager = WxCookieManager(browser_type=browser_type)
    
    try:
        # 显示菜单
        while True:
            show_menu()
            choice = input("\n请输入选项 (0-4): ").strip()
            
            if choice == "1":
                # 登录流程
                success = manager.login()
                if success:
                    # 保持程序运行，让用户可以看到浏览器
                    print("\n" + "="*50)
                    print("💡 浏览器将保持打开状态")
                    print("   按 Enter 键返回菜单（浏览器会关闭）")
                    print("="*50 + "\n")
                    
                    # 等待用户按Enter
                    try:
                        input()
                    except KeyboardInterrupt:
                        pass
                    finally:
                        manager.close()
                        print("\n✅ 已返回菜单\n")
                else:
                    print("\n❌ 登录失败\n")
            
            elif choice == "2":
                # 批量检测所有账号的 Cookie 有效性
                manager.check_all_cookies_validity()
                print("\n按 Enter 键继续...")
                try:
                    input()
                except KeyboardInterrupt:
                    break
            
            elif choice == "3":
                # 账号管理
                manager.manage_accounts()
            
            elif choice == "4":
                # 清除保存的 Cookie 配置
                manager.load_config()
                accounts = manager.list_accounts()
                if not accounts:
                    print("\n📝 暂无账号，无需清除")
                else:
                    try:
                        idx = input("\n请输入要清除Cookie的账号编号: ").strip()
                        idx = int(idx) - 1
                        if 0 <= idx < len(accounts):
                            account_id = accounts[idx]['id']
                            account = manager.config['accounts'][account_id]
                            nickname = account.get('nickname', account_id)
                            confirm = input(f"\n⚠️  确定要清除账号 {nickname} 的 Cookie 配置吗？(y/n): ").strip().lower()
                            if confirm == 'y' or confirm == 'yes':
                                manager.clear_saved_cookies(account_id)
                            else:
                                print("已取消操作")
                        else:
                            print("❌ 无效的账号编号")
                    except ValueError:
                        print("❌ 请输入有效的数字")
                    except Exception as e:
                        print(f"❌ 清除失败: {e}")
                print("\n按 Enter 键继续...")
                try:
                    input()
                except KeyboardInterrupt:
                    break
            
            elif choice == "0":
                print("\n👋 再见！")
                break
            
            else:
                print("\n⚠️  无效的选项，请重新选择\n")
    
    except KeyboardInterrupt:
        print("\n\n👋 程序退出")
    except Exception as e:
        print(f"\n❌ 程序运行出错: {e}")
        import traceback
        traceback.print_exc()
    finally:
        manager.close()


if __name__ == "__main__":
    main()

