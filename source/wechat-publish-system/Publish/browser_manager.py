"""
🌐 浏览器管理模块 (BrowserManager)

📝 模块简介:
- 本模块开发目的：提供稳定的浏览器环境，支持微信公众号自动化操作

🔍 主要功能与处理流程:

1. 浏览器环境管理
- 🔧 自定义Chrome启动参数配置
- 📂 用户数据目录管理与维护
- 🧹 自动清理浏览器锁定文件
- 🔄 异常情况下的重启机制

2. 浏览器配置
- 🛡️ 安全相关参数设置
- 🍪 Cookie与缓存策略管理
- 🖥️ 显示与窗口设置
- 🌐 网络访问权限配置

3. 目录结构维护
- 📁 自动创建必要的目录结构
- 🔒 清理锁定文件
- 🔍 数据完整性验证
"""

import os
import shutil
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

class BrowserManager:
    def __init__(self, user_data_dir: str = None):
        self.user_data_dir = user_data_dir
        self.driver = None
        self.wait = None
        self._setup_browser()
        
    def _ensure_chrome_dirs(self):
        """确保Chrome用户数据目录结构完整"""
        if not self.user_data_dir:
            return
            
        # 创建主目录
        os.makedirs(self.user_data_dir, exist_ok=True)
        
        # 创建必要的子目录
        required_dirs = [
            os.path.join(self.user_data_dir, 'Default'),
            os.path.join(self.user_data_dir, 'Default', 'Cache'),
            os.path.join(self.user_data_dir, 'Default', 'Code Cache'),
            os.path.join(self.user_data_dir, 'Default', 'Network'),
        ]
        
        for directory in required_dirs:
            os.makedirs(directory, exist_ok=True)
            
        # 清理可能存在的锁定文件
        lock_files = [
            os.path.join(self.user_data_dir, 'Default', 'Cookies-journal'),
            os.path.join(self.user_data_dir, 'Default', 'History-journal'),
            os.path.join(self.user_data_dir, 'Default', 'DevTools Active Port'),
            os.path.join(self.user_data_dir, 'Singleton*'),
            os.path.join(self.user_data_dir, '*.lock'),
            os.path.join(self.user_data_dir, '*.log')
        ]
        
        for pattern in lock_files:
            try:
                import glob
                for lock_file in glob.glob(pattern):
                    try:
                        os.remove(lock_file)
                        print(f"已清理锁定文件: {lock_file}")
                    except:
                        pass
            except:
                pass
        
    def _setup_browser(self):
        """配置并启动Chrome浏览器"""
        chrome_options = Options()
        
        # 基础设置
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--ignore-certificate-errors')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
        
        # 添加更多权限设置
        chrome_options.add_argument('--enable-cookies')
        chrome_options.add_argument('--disable-web-security')
        chrome_options.add_argument('--allow-running-insecure-content')
        chrome_options.add_argument('--disable-site-isolation-trials')
        
        # 添加解决启动问题的参数
        chrome_options.add_argument('--disable-gpu')  # 禁用GPU加速
        chrome_options.add_argument('--no-first-run')  # 跳过首次运行检查
        chrome_options.add_argument('--remote-debugging-port=0')  # 使用随机调试端口
        chrome_options.add_argument('--window-size=1920,1080')  # 设置窗口大小
        
        # Cookie和缓存设置
        prefs = {
            'profile.default_content_setting_values': {
                'cookies': 1,  # 1允许所有cookie
                'images': 1,
                'javascript': 1,
                'plugins': 1,
                'popups': 1,
                'geolocation': 1,
                'notifications': 1,
                'auto_select_certificate': 1,
                'fullscreen': 1,
                'mouselock': 1,
                'mixed_script': 1,
                'media_stream': 1,
                'media_stream_mic': 1,
                'media_stream_camera': 1,
                'protocol_handlers': 1,
                'ppapi_broker': 1,
                'automatic_downloads': 1,
                'midi_sysex': 1,
                'push_messaging': 1,
                'ssl_cert_decisions': 1,
                'metro_switch_to_desktop': 1,
                'protected_media_identifier': 1,
                'app_banner': 1,
                'site_engagement': 1,
                'durable_storage': 1
            },
            'profile.managed_default_content_settings': {
                'images': 1
            },
            'profile.password_manager_enabled': False,
            'profile.default_content_settings.popups': 0,
            'download.prompt_for_download': False,
            'download.directory_upgrade': True,
            'safebrowsing.enabled': True,
            'credentials_enable_service': False
        }
        chrome_options.add_experimental_option('prefs', prefs)
        
        # 设置用户数据目录
        if self.user_data_dir:
            # 确保目录结构完整
            self._ensure_chrome_dirs()
            chrome_options.add_argument(f'--user-data-dir={self.user_data_dir}')
        
        # 初始化浏览器
        print("正在启动浏览器...")
        try:
            self.driver = webdriver.Chrome(options=chrome_options)
            self.driver.maximize_window()
            self.wait = WebDriverWait(self.driver, 20)
            print("浏览器启动成功")
        except Exception as e:
            print(f"浏览器启动失败: {e}")
            if self.user_data_dir and os.path.exists(self.user_data_dir):
                print("尝试清理用户数据目录并重新启动...")
                try:
                    shutil.rmtree(self.user_data_dir)
                    os.makedirs(self.user_data_dir)
                    self.driver = webdriver.Chrome(options=chrome_options)
                    self.driver.maximize_window()
                    self.wait = WebDriverWait(self.driver, 20)
                    print("浏览器重新启动成功")
                except Exception as e2:
                    print(f"浏览器重新启动失败: {e2}")
                    raise 