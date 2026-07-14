"""
🤖 微信公众号文章自动发布模块
- 本模块开发目的：实现微信公众号文章的自动化发布流程管理，基于Selenium浏览器自动化技术

🔐 账号登录与身份验证
- 🍪 支持Cookie持久化登录，避免重复扫码
- 📱 支持微信扫码登录备用方案
- 🔑 自动获取并保存Token认证信息
- 💾 登录状态自动保存与恢复

🌐 浏览器环境初始化
- 🖥️ 创建Chrome浏览器实例
- ⚙️ 配置浏览器参数和用户数据目录
- 🔧 设置等待机制和异常处理
- 📂 加载指定账号的浏览器配置文件

📝 文章内容处理流程
- 📄 读取Markdown格式的文章文件
- ✏️ 自动填写文章标题到编辑器
- 📖 将文章正文内容粘贴到富文本编辑器
- 🔄 支持新版和旧版微信编辑器界面适配

🖼️ 封面图片管理
- 📁 扫描指定封面图片文件夹
- 🎲 随机选择合适的封面图片
- ⬆️ 自动上传图片到微信后台
- 🖼️ 设置图片为文章封面

📤 文章发布执行
- ✅ 点击发布按钮触发发布流程
- 🔕 自动关闭群发通知选项
- ⏱️ 智能等待发布操作完成
- 📊 检测发布状态并返回结果

📁 文件系统管理
- 🗂️ 发布成功后自动归档文章文件
- 📋 移动已发布文章到指定目录
- 🔄 保持源文件夹整洁有序
- 📝 记录文章发布历史

🛡️ 异常处理与重试机制
- ⚠️ 完善的错误捕获和处理
- 🔁 关键操作失败自动重试
- 📝 详细的日志输出和错误记录
- 🚪 程序异常退出保护机制
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException
from lxml import html
import time
import json
import os
import re
import shutil
import argparse
import random

# 导入folder_config配置
try:
    from config.folder_config import PUBLISHED_FOLDER, THEME_FOLDER
except ImportError:
    print("错误: 请先配置 config/folder_config.py 文件")
    exit(1)

# 基础工具类 - 包含共享的辅助方法
class BaseWeChatTool:
    def __init__(self, browser_manager):
        """初始化基础类"""
        self.browser_manager = browser_manager
        self.driver = browser_manager.driver
        self.wait = browser_manager.wait
    
    def wait_for_element_stable(self, locator, timeout=10):
        """等待元素稳定可交互"""
        end_time = time.time() + timeout
        last_exception = None
        
        while time.time() < end_time:
            try:
                # 首先等待元素存在
                element = self.wait.until(
                    EC.presence_of_element_located(locator)
                )
                
                # 然后等待元素可见
                if element.is_displayed():
                    # 尝试等待元素可点击
                    clickable_element = self.wait.until(
                        EC.element_to_be_clickable(locator)
                    )
                    
                    # 确保元素在视图中
                    self.driver.execute_script(
                        "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                        clickable_element
                    )
                    time.sleep(0.5)  # 等待滚动完成
                    
                    return clickable_element
                    
            except Exception as e:
                last_exception = e
                time.sleep(0.1)
                continue
                
        if last_exception:
            raise TimeoutException(
                f"元素 {locator} 在 {timeout} 秒内未稳定: {last_exception}"
            )
        raise TimeoutException(f"元素 {locator} 在 {timeout} 秒内未稳定")

    def verify_page_ready(self, timeout=10):
        """验证页面是否完全加载
        Args:
            timeout: 超时时间(秒)
        Returns:
            bool: 页面是否加载完成
        """
        try:
            return self.driver.execute_script(
                "return document.readyState === 'complete'"
            )
        except:
            return False

    def click_button_with_retry(self, locator_strategies, timeout=10, max_retries=3):
        """多策略按钮点击
        Args:
            locator_strategies: [{'name': '策略名', 'locator': (By.XX, 'selector')}]
            timeout: 每次尝试的超时时间
            max_retries: 最大重试次数
        Returns:
            bool: 是否点击成功
        """
        for attempt in range(max_retries):
            for strategy in locator_strategies:
                try:
                    # 等待页面加载完成
                    if not self.verify_page_ready():
                        time.sleep(1)
                        continue
                    
                    # 等待元素稳定
                    element = self.wait_for_element_stable(
                        strategy['locator'], 
                        timeout=timeout
                    )
                    
                    # 尝试多种点击方式
                    try:
                        # 先尝试常规点击
                        element.click()
                        print(f"  - {strategy['name']}点击成功")
                        return True
                    except:
                        try:
                            # 如果常规点击失败，尝试 ActionChains
                            ActionChains(self.driver).move_to_element(element).click().perform()
                            print(f"  - {strategy['name']}通过ActionChains点击成功")
                            return True
                        except:
                            # 如果 ActionChains 也失败，尝试 JavaScript
                            self.driver.execute_script(
                                "arguments[0].click();", 
                                element
                            )
                            print(f"  - {strategy['name']}通过JS点击成功")
                            return True
                    
                except Exception as e:
                    continue
                       
        return False

# 1. 登录类 - 负责登录到公众号编辑器打开
class WeChatLogin(BaseWeChatTool):
    def __init__(self, browser_manager):
        """初始化WeChatLogin对象"""
        super().__init__(browser_manager)
        self.cookie_file = None
        self.token_file = None
        self.token = None
    
    def save_cookies(self):
        """保存Cookie到文件"""
        cookies = self.driver.get_cookies()
        with open(self.cookie_file, 'w') as f:
            json.dump(cookies, f)
        print("Cookie已保存")

    def load_cookies(self):
        """从文件加载Cookie"""
        try:
            if os.path.exists(self.cookie_file):
                with open(self.cookie_file, 'r') as f:
                    cookies = json.load(f)
                for cookie in cookies:
                    self.driver.add_cookie(cookie)
                print("  - Cookie加载成功")
                return True
            return False
        except Exception as e:
            print(f"加载Cookie失败: {e}")
            return False

    def check_login_status(self):
        """检查是否已登录"""
        try:
            self.wait.until(
                EC.presence_of_element_located((By.XPATH, "//*[contains(text(), '设置与开发')]"))
            )
            print("  - 检测到已登录状态!")
            return True
        except:
            print("未检测到登录状态...")
            return False

    def open_wechat_admin(self):
        """打开微信公众号后台"""
        try:
            print("1️⃣  登录微信公众平台")
            self.driver.get("https://mp.weixin.qq.com/")
            
            # 尝试使用Cookie登录
            if self.load_cookies():
                self.driver.refresh()
                if self.check_login_status():
                    print("  - Cookie登录成功")
                    self.get_token()
                    return True
            
            print("  - 请扫码登录...")
            # 等待用户扫码登录
            timeout = 120  # 给用户2分钟的扫码时间
            try:
                self.wait.until(
                    EC.presence_of_element_located((By.XPATH, "//*[contains(text(), '设置与开发')]")),
                    timeout
                )
                print("  - 登录成功！")
                self.save_cookies()
                self.get_token()
                return True
            except:
                print("  - 登录超时，请重试")
                return False
                
        except Exception as e:
            print(f"登录失败: {e}")
            return False

    def handle_certification_popup(self):
        """处理微信认证提示弹窗"""
        try:
            print("  - 检测微信认证提示弹窗...")
            time.sleep(3)  # 等待3秒，确保弹窗有足够时间出现

            # 尝试查找弹窗
            popup = None
            try:
                popup = self.wait.until(
                    EC.presence_of_element_located((By.XPATH, "//div[@class='weui-desktop-dialog' and .//h3[contains(text(), '微信认证提示')]]"))
                )
                print("  - 检测到微信认证提示弹窗")
            except TimeoutException:
                print("  - 未检测到微信认证提示弹窗")
                return

            # 点击取消按钮
            cancel_button = popup.find_element(By.XPATH, ".//button[contains(@class, 'weui-desktop-btn_default') and contains(text(), '取消')]")
            cancel_button.click()
            print("  - 已关闭微信认证提示弹窗")

        except Exception as e:
            print(f"  - 该账号无需处理认证弹窗")

    def click_content_management(self):
        """点击内容管理"""
        try:
            print("2️⃣  进入内容管理")
            # 先处理弹窗
            self.handle_certification_popup()
            
            content_management = self.wait.until(
                EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), '内容管理')]"))
            )
            content_management.click()
            print("  - 已进入内容管理页面")
            return True
        except Exception as e:
            print(f"  - 进入内容管理失败: {e}")
            return False

    def click_drafts(self):
        """点击草稿箱"""
        try:
            print("3️⃣  进入草稿箱")
            drafts = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "a[title='草稿箱']"))
            )
            drafts.click()
            print("  - 已进入草稿箱")
            # 3️⃣新增：等待5秒后刷新页面
            import time
            time.sleep(5)
            self.driver.refresh()
            return True
        except Exception as e:
            print(f"点击草稿箱失败: {e}")
            # 尝试备用定位方式
            try:
                drafts = self.driver.find_element(By.XPATH, "//span[text()='草稿箱']/parent::a")
                drafts.click()
                print("使用备用方式1点击草稿箱成功")
                # 3️⃣新增：等待5秒后刷新页面
                time.sleep(5)
                self.driver.refresh()
                return True
            except Exception as e2:
                print(f"备用方式也失败了: {e2}")
                return False

    def click_new_article(self):
        """点击新建文章"""
        try:
            print("4️⃣  创建新文章")
            # 等待"新的创作"按钮出现并悬停
            new_creation = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.weui-desktop-card.weui-desktop-card_new"))
            )
            
            # 创建 ActionChains 对象来执行悬停操作
            actions = ActionChains(self.driver)
            actions.move_to_element(new_creation).perform()
            
            # 等待"写新文章"选项出现并点击
            time.sleep(1.5)
            write_new = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "li[data-type='0']"))
            )
            write_new.click()
            print("  - 已点击创建新文章")
            return True
            
        except Exception as e:
            print(f"点击写新文章失败: {e}")
            return False

    def switch_to_edit_window(self):
        """切换到编辑器窗口"""
        try:
            print("5️⃣  切换到编辑器窗口")
            # 等待新窗口打开
            time.sleep(8)
            
            # 获取所有窗口句柄
            handles = self.driver.window_handles
            
            # 记住当前（旧）窗口句柄
            old_handle = self.driver.current_window_handle
            
            # 切换到最新打开的窗口
            self.driver.switch_to.window(handles[-1])
            time.sleep(1.5)

            # 关闭旧窗口
            self.driver.switch_to.window(old_handle)
            self.driver.close()

            # 重新切换到编辑器窗口
            self.driver.switch_to.window(handles[-1])
            
            # 保存原始窗口句柄，供后续使用
            self.original_window_handle = old_handle
            
            # 刷新编辑器页面
            print("  - 刷新编辑器页面...")
            self.driver.refresh()
            time.sleep(8)  # 等待页面刷新完成
            
            print("  - 已切换到编辑器窗口")
            return True
        except Exception as e:
            print(f"切换到编辑器窗口失败: {e}")
            return False
            
    def get_token(self):
        """从页面提取token"""
        try:
            # 等待首页链接出现
            home_link = self.wait.until(
                EC.presence_of_element_located((By.XPATH, '//a[@title="首页"]'))
            )
            url = home_link.get_attribute('href')
            
            # 提取token
            token = re.findall(r'token=(\d+)', url)[0]
            print(f"  - 获取到token: {token}")
            
            # 保存token
            with open(self.token_file, 'w') as f:
                f.write(token)
            
            self.token = token
            return token
        except Exception as e:
            print(f"获取token失败: {e}")
            return None

# 2. 内容编辑类 - 负责输入标题、粘贴内容、上传封面图片
class ContentEditor(BaseWeChatTool):
    def __init__(self, browser_manager):
        """初始化ContentEditor对象"""
        super().__init__(browser_manager)
        self.theme_folder = THEME_FOLDER  # 使用配置的主题文件夹路径
        self.account_info = None # 新增：存储账号信息
    
    def create_new_article(self, max_retries=3):
        """新建图文
        
        在编辑器页面中创建一个新的图文消息。
        先点击"新建消息"按钮，然后点击"写新文章"选项。
        
        Args:
            max_retries: 最大重试次数
            
        Returns:
            bool: 操作是否成功
        """
        try:
            print("🆕  创建新图文")
            
            # 新建消息按钮的定位方式
            add_message_selectors = [
                (By.XPATH, "/html/body/div[2]/div/div/div/div[1]/div[3]/div/div/div[1]/div[1]/div[3]/div"),  # 用户提供的XPath
                (By.CSS_SELECTOR, "div[data-action='add']"),  # 测试成功的CSS选择器
                (By.CSS_SELECTOR, "div.preview_media_add_middle")  # 用户提供的元素类
            ]
            
            # 写新文章按钮的定位方式（在弹出菜单中）
            write_article_selectors = [
                (By.CSS_SELECTOR, "li.js_create_article[data-type='0']")  # CSS选择器
            ]
            
            for attempt in range(max_retries):
                try:
                    # 步骤1: 点击"新建消息"按钮
                    print("  - 尝试点击'新建消息'按钮...")
                    
                    add_button = None
                    for selector_type, selector in add_message_selectors:
                        try:
                            add_button = self.wait.until(
                                EC.element_to_be_clickable((selector_type, selector))
                            )
                            print(f"  - 找到'新建消息'按钮，使用选择器: {selector}")
                            break
                        except:
                            continue
                    
                    if not add_button:
                        print("  - 未找到'新建消息'按钮，尝试使用JavaScript...")
                        # 尝试使用JavaScript查找并点击
                        self.driver.execute_script("""
                            let buttons = document.querySelectorAll('[data-action="add"], .preview_media_add_middle');
                            if (buttons.length > 0) {
                                buttons[0].click();
                                return true;
                            }
                            return false;
                        """)
                        time.sleep(1)  # 等待可能的菜单出现
                    else:
                        # 确保按钮在视图中
                        self.driver.execute_script(
                            "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", 
                            add_button
                        )
                        time.sleep(1)
                        
                        # 点击按钮
                        add_button.click()
                    
                    print("  - '新建消息'按钮点击成功，等待菜单出现...")
                    if not add_button:
                        print("  - 按钮点击方法：JavaScript查询使用选择器 '[data-action=\"add\"], .preview_media_add_middle'")
                    else:
                        selector_type_name = "XPath" if add_button and selector_type == By.XPATH else "CSS选择器"
                        print(f"  - 按钮点击方法：Selenium {selector_type_name} '{selector}'")
                    time.sleep(1)  # 等待菜单展开
                    
                    # 步骤2: 点击"写新文章"选项
                    print("  - 尝试点击'写新文章'选项...")
                    print("  - 开始尝试以下定位方法：")
                    for idx, (s_type, s_value) in enumerate(write_article_selectors):
                        s_type_name = "XPath" if s_type == By.XPATH else "CSS选择器"
                        print(f"    {idx+1}. {s_type_name}: '{s_value}'")
                    
                    write_button = None
                    for selector_type, selector in write_article_selectors:
                        try:
                            write_button = self.wait.until(
                                EC.element_to_be_clickable((selector_type, selector))
                            )
                            print(f"  - 找到'写新文章'选项，使用选择器: {selector}")
                            break
                        except:
                            continue
                    
                    if not write_button:
                        print("  - 未找到'写新文章'选项，尝试使用JavaScript...")
                        # 尝试使用JavaScript执行点击
                        success = self.driver.execute_script("""
                            // 尝试多种可能的选择器
                            let options = [
                                document.evaluate('/html/body/div[43]/div/ul/li[1]', document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue,
                                document.querySelector('li.js_create_article[data-type="0"] a')
                            ];
                            
                            for (let opt of options) {
                                if (opt) {
                                    opt.click();
                                    return true;
                                }
                            }
                            return false;
                        """)
                        
                        if not success:
                            raise Exception("JavaScript点击'写新文章'选项失败")
                    else:
                        # 点击"写新文章"选项
                        write_button.click()
                    
                    print("  - '写新文章'选项点击成功")
                    time.sleep(2)  # 等待新窗口加载
                    print("  - 新建图文成功")
                    return True
                
                except Exception as e:
                    print(f"  - 第{attempt + 1}次尝试失败: {e}")
                    if attempt < max_retries - 1:
                        print(f"  - 等待2秒后重试...")
                        time.sleep(2)
                    else:
                        print(f"  - 新建图文失败，已达到最大重试次数")
                        return False
            
            return False
            
        except Exception as e:
            print(f"新建图文时发生错误: {e}")
            import traceback
            print(traceback.format_exc())
            return False
    
    def input_article_content(self, title: str = None, max_retries: int = 3) -> bool:
        """输入文章内容"""
        try:
            print(f"6️⃣  输入文章内容 (尝试 1/{max_retries})")
            
            # 输入标题（恢复原来的选择器）
            if title:
                try:
                    title_input = self.wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "textarea#title"))
                    )
                    title_input.clear()
                    title_input.send_keys(title)
                    print("  - 文章标题输入成功")
                    time.sleep(1)
                except Exception as e:
                    print(f"  - 标题输入失败: {str(e)}")
                    return False

            retry_count = 0
            while retry_count < max_retries:
                try:
                    if retry_count > 0:
                        print(f"  - 第 {retry_count + 1} 次尝试...")
                    
                    # 检测编辑器类型
                    is_new_editor = False
                    try:
                        # 尝试查找新版编辑器的特征元素
                        self.driver.find_element(By.CSS_SELECTOR, "div.ProseMirror")
                        is_new_editor = True
                        print("  - 检测到新版编辑器")
                    except:
                        print("  - 检测到旧版编辑器")
                        pass

                    if is_new_editor:
                        # 新版编辑器（如小鱼儿账号）的处理逻辑
                        # 方法1：直接定位 ProseMirror 编辑区域
                        try:
                            editor = self.wait.until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, "div.ProseMirror"))
                            )
                            editor.click()
                            # 使用 JavaScript 清空内容并粘贴
                            self.driver.execute_script(
                                "arguments[0].innerHTML = '';", editor)
                            ActionChains(self.driver).click(editor).key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
                            time.sleep(1)  # 等待粘贴完成
                            print("  - 文章内容输入完成")
                            return True
                        except Exception as e1:
                            print(f"  - 方法1失败，尝试方法2: {str(e1)}")
                            # 方法2：通过父元素定位
                            try:
                                editor_container = self.wait.until(
                                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.rich_media_content"))
                                )
                                editor = editor_container.find_element(By.CSS_SELECTOR, "div[contenteditable='true']")
                                editor.click()
                                self.driver.execute_script(
                                    "arguments[0].innerHTML = '';", editor)
                                ActionChains(self.driver).click(editor).key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
                                time.sleep(1)
                                print("  - 文章内容输入完成")
                                time.sleep(1)
                                return True
                            except Exception as e2:
                                print(f"  - 方法2失败: {str(e2)}")
                                raise e2
                    else:
                        # 旧版编辑器的处理逻辑（保持原有逻辑）
                        iframe = self.wait.until(
                            EC.presence_of_element_located((By.TAG_NAME, "iframe"))
                        )
                        self.driver.switch_to.frame(iframe)
                        editor_body = self.wait.until(
                            EC.presence_of_element_located((By.TAG_NAME, "body"))
                        )
                        editor_body.click()
                        ActionChains(self.driver).key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
                        time.sleep(1)
                        self.driver.switch_to.default_content()
                        print("  - 文章内容输入完成")
                        return True

                except Exception as e:
                    print(f"  - 第 {retry_count + 1} 次尝试失败: {str(e)}")
                    retry_count += 1
                    if retry_count >= max_retries:
                        print(f"输入文章内容失败 (已重试 {max_retries} 次)")
                        return False
                    time.sleep(2)  # 失败后等待2秒再重试

            return False
            
        except Exception as e:
            print(f"输入文章内容时发生错误: {str(e)}")
            return False

    def select_random_image(self):
        """从主题文件夹中随机选择一张图片
        
        Returns:
            str: 随机选择的图片的完整路径
            
        Raises:
            Exception: 当没有找到图片或选择失败时抛出异常
        """
        # 获取主题文件夹中的所有图片文件
        images = [f for f in os.listdir(self.theme_folder) 
                 if f.lower().endswith(('.png', '.jpg', '.jpeg'))]
        
        if not images:
            raise Exception("主题文件夹中没有找到图片")
        
        # 随机选择一张图片
        selected_image = random.choice(images)
        image_path = os.path.join(self.theme_folder, selected_image)
        print(f"  - 已随机选择封面图片: {selected_image}")
        return image_path

    def upload_cover_image(self, image_path=None):
        """上传封面图片
        Args:
            image_path: 可选，指定的图片路径。如果为None，则随机选择一张图片
        Returns:
            bool: 上传是否成功
        """
        try:
            print("7️⃣  上传封面图片")
            
            # 如果没有指定图片路径，随机选择一张
            if image_path is None:
                image_path = self.select_random_image()
            
            # 确保页面完全滚动到底部的几种方法
            print("  - 正在滚动到页面底部...")
            
            # 方法1：使用JavaScript滚动到最底部
            self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
            
            # 方法2：使用JavaScript分步滚动
            total_height = self.driver.execute_script("return document.body.scrollHeight")
            for height in range(0, total_height, 300):  # 每次滚动300像素
                self.driver.execute_script(f"window.scrollTo(0, {height});")
                time.sleep(0.1)
            
            # 方法3：再次确保到达底部
            self.driver.execute_script("""
                window.scrollTo({
                    top: document.body.scrollHeight,
                    behavior: 'smooth'
                });
            """)
            time.sleep(2)
            
            # 1. 定位封面区域并悬停
            try:
                cover_area = self.wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.select-cover__btn.js_cover_btn_area"))
                )
                
                # 确保封面区域在视图中
                self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", cover_area)
                time.sleep(1)
                
                # 执行悬停操作
                actions = ActionChains(self.driver)
                actions.move_to_element(cover_area).perform()
                print("  - '封面图区域'悬停成功")
                time.sleep(1)
            except Exception as e:
                print("  - 首次尝试定位封面区域失败，重试中...")
                # 最后尝试：直接滚动到最底部并多等待一会
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)
                cover_area = self.wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.select-cover__btn.js_cover_btn_area"))
                )
                self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});", cover_area)
                time.sleep(1)
                actions = ActionChains(self.driver)
                actions.move_to_element(cover_area).perform()
                print("  - '封面图区域'悬停成功（重试后）")
                time.sleep(1)
            
            # 2. 点击"从图片库选择"按钮
            library_btn = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "#js_cover_null .pop-opr__list .pop-opr__item a.pop-opr__button.js_imagedialog"))
            )
            library_btn.click()
            print("  - 点击'从图片库选择'按钮成功")
            time.sleep(1)
            
            # 3. 在弹窗中找到文件上传input
            file_input = self.wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[id^='rt_rt'] input[type='file']"))
            )
            print("  - 定位到文件选择 input 元素")
            
            # 额外打印 input 状态，辅助问题定位
            try:
                input_style = file_input.get_attribute("style")
                input_multiple = file_input.get_attribute("multiple")
                input_accept = file_input.get_attribute("accept")
                input_displayed = file_input.is_displayed()
                print(f"  - input属性: style={input_style}, multiple={input_multiple}, accept={input_accept}, is_displayed={input_displayed}")
                is_offset_parent_null = self.driver.execute_script("return arguments[0].offsetParent === null", file_input)
                print(f"  - input可交互性: offsetParent_is_null={is_offset_parent_null}")
            except Exception as _e:
                print(f"  - 读取input状态失败: {_e}")
            
            # 确保文件路径是绝对路径
            abs_path = os.path.abspath(image_path)
            
            # 优先通过 send_keys 直接赋值给 input；若隐藏则临时调整样式后重试，考虑 DOM 重建引发的 Stale
            from selenium.common.exceptions import StaleElementReferenceException, ElementNotInteractableException, ElementClickInterceptedException
            upload_success = False
            for attempt in range(1, 5):
                try:
                    # 每次重试都重新获取 input，避免 DOM 重建导致的句柄失效
                    file_input = self.wait.until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, "div[id^='rt_rt'] input[type='file']"))
                    )
                    # 若 input 隐藏，短暂调整样式，避免浏览器策略拦截
                    try:
                        self.driver.execute_script(
                            "arguments[0].style.display='block'; arguments[0].style.opacity=1; arguments[0].style.visibility='visible';",
                            file_input
                        )
                    except Exception:
                        pass
                    print(f"  - 尝试第{attempt}次发送文件到 input ...")
                    file_input.send_keys(abs_path)
                    upload_success = True
                    print("  - '图片文件'上传触发成功 (send_keys 已执行)")
                    break
                except (StaleElementReferenceException, ElementNotInteractableException) as e:
                    print(f"  - 第{attempt}次发送失败: {e}. 将等待并重试...")
                    time.sleep(1.2)
                except Exception as e:
                    print(f"  - 第{attempt}次发送遇到异常: {e}")
                    time.sleep(1.2)
            if not upload_success:
                raise Exception("多次尝试发送文件到 input 失败")
            
            # 4. 等待并点击"下一步"按钮 - 完全复制测试代码的简洁逻辑
            print("⏳ 等待图片上传完成...")
            time.sleep(3)
            
            # 查找活跃的对话框 - 完全复制测试代码逻辑
            dialogs = self.driver.find_elements(By.CSS_SELECTOR, "div.weui-desktop-dialog")
            active_dialog = None
            for d in dialogs[::-1]:
                try:
                    if d.is_displayed():
                        active_dialog = d
                        break
                except:
                    continue
                    
            if not active_dialog and dialogs:
                active_dialog = dialogs[-1]
            
            if not active_dialog:
                raise Exception("未找到图片选择对话框")
            
            # 查找下一步按钮 - 完全复制测试代码逻辑
            next_btn = None
            try:
                next_btn = active_dialog.find_element(By.XPATH, ".//div[contains(@class,'weui-desktop-dialog__ft')]//button[normalize-space()='下一步']")
            except:
                try:
                    next_btn = active_dialog.find_element(By.CSS_SELECTOR, "div.weui-desktop-dialog__ft button.weui-desktop-btn_primary")
                except:
                    pass
            
            if not next_btn:
                raise Exception("未找到'下一步'按钮")
            
            # 确保按钮可见并点击
            try:
                print("  - 开始点击'下一步'按钮...")
                self.driver.execute_script("arguments[0].scrollIntoView({behavior: 'instant', block: 'center'});", next_btn)
                time.sleep(0.5)
                next_btn.click()
                print("  - '下一步'按钮点击成功，进入图片编辑页面")
            except Exception as e:
                print(f"  - '下一步'按钮点击失败: {e}")
                raise e
            
            time.sleep(2)
            
            # 6. 点击确认按钮完成封面图上传 - 使用验证最成功的方法1
            try:
                print("  - 开始点击封面图确认按钮...")
                # 使用文本查找的方法（在测试中最成功）
                confirm_button = self.wait.until(
                    EC.element_to_be_clickable((By.XPATH, "//button[contains(@class, 'weui-desktop-btn_primary') and (contains(text(), '确认') or contains(text(), '完成'))]"))
                )
                confirm_button.click()
                print("  - 封面图确认按钮点击成功")
                time.sleep(3)
                print("  - 封面图片上传完成")
                
            except Exception as e:
                print(f"  - 封面图确认按钮点击失败: {e}")
                raise e

            # ====== 自动执行原创声明操作 - 根据账号类型决定是否执行 ======
            # 检查账号类型，只有大号才执行原创声明
            should_declare_original = True  # 默认执行原创声明（大号）
            
            if self.account_info and self.account_info.get('name'):
                account_name = self.account_info['name']
                if account_name.startswith('[小号]'):
                    should_declare_original = False
                    print(f"8️⃣  检测到小号账号：{account_name}，跳过原创声明操作")
                else:
                    print(f"8️⃣  检测到大号账号：{account_name}，开始执行原创声明操作")
            else:
                print("8️⃣  未获取到账号信息，默认执行原创声明操作")
            
            if should_declare_original:
                try:
                    print("8️⃣  自动执行原创声明操作...")
                    
                    # 1. 点击"未声明"按钮
                    try:
                        unset_btn = self.driver.find_element(By.CSS_SELECTOR, "div.js_unset_original_title")
                        unset_btn.click()
                        print("✅ 已点击'未声明'按钮")
                        time.sleep(1)
                    except Exception as e:
                        print(f"❌ 点击'未声明'按钮失败: {e}")
                        raise e
                    
                    # 2. 输入作者
                    try:
                        author_inputs = self.driver.find_elements(By.CSS_SELECTOR, "span.js_customerauthor_container input.js_author")
                        author_input = None
                        for inp in author_inputs:
                            if inp.is_displayed():
                                author_input = inp
                                break
                                
                        if author_input is None:
                            raise Exception("没有找到可见的作者输入框")
                        
                        author_input.click()
                        time.sleep(0.2)
                        author_input.clear()
                        author_input.send_keys("希希")
                        time.sleep(0.5)
                        self.driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }))", author_input)
                        print("✅ 已输入作者'希希'")
                        time.sleep(0.5)
                        
                    except Exception as e:
                        print(f"❌ 输入作者失败: {e}")
                        raise e
                    
                    # 3. 勾选协议
                    try:
                        checkbox = self.driver.find_element(By.CSS_SELECTOR, "div.original_agreement input[type='checkbox']")
                        if not checkbox.is_selected():
                            try:
                                checkbox.click()
                            except:
                                self.driver.execute_script("arguments[0].click();", checkbox)
                        print("✅ 已勾选协议")
                        time.sleep(0.5)
                    except Exception as e:
                        print(f"❌ 勾选协议失败: {e}")
                        raise e
                    
                    # 4. 点击确定按钮 - 使用测试验证最成功的完整XPath方法
                    try:
                        print("  - 开始点击原创声明确定按钮...")
                        
                        # 等待弹窗稳定
                        time.sleep(1)
                        
                        # 使用测试代码中验证成功的完整XPath定位
                        exact_xpath = "/html/body/div[2]/div/div/div/div/div[5]/mp-image-product-dialog/div/div[1]/div/div[3]/div/div[2]"
                        button_container = self.wait.until(
                            EC.presence_of_element_located((By.XPATH, exact_xpath))
                        )
                        # 在容器中查找确定按钮
                        confirm_button = button_container.find_element(By.TAG_NAME, "button")
                        
                        # 检查按钮是否可点击
                        if confirm_button.is_displayed() and confirm_button.is_enabled():
                            confirm_button.click()
                            print("  - 原创声明确定按钮点击成功")
                            time.sleep(2)
                        else:
                            raise Exception("按钮不可点击")
                        
                        print("✅ 原创声明操作完成！")
                        
                    except Exception as e:
                        print(f"❌ 点击确定按钮失败: {e}")
                        raise e
                        
                except Exception as e:
                    print(f"❌ 原创声明自动化失败: {e}")
                    # 原创声明失败不应该影响整个流程，所以这里不抛出异常
                    print("   继续执行后续流程...")
            # ====== 原创声明操作结束 ======

            return True
            
        except Exception as e:
            print(f"上传封面图片失败: {e}")
            import traceback
            print(traceback.format_exc())
            return False

# 3. 发布管理类 - 负责发布文章、处理确认弹窗、清除Cookie
class PublishManager(BaseWeChatTool):
    def __init__(self, browser_manager):
        """初始化PublishManager对象"""
        super().__init__(browser_manager)
        self.md_file_path = None  # 保存当前处理的文件路径
        self.articles_per_publish = 1  # 每次发布的图文数量，默认为1
    
    def prepare_for_publish(self, mass_send_notify=False):
        """准备发布文章的完整流程"""
        print("9️⃣  准备发表")
        
        # 点击发布按钮的处理
        publish_success = self._click_publish_button()
        if not publish_success:
            print("准备发表失败: 无法点击发表按钮")
            return False
        
        # 等待弹窗出现
        time.sleep(3)
        
        # 处理群发通知设置
        notification_result = self._handle_mass_notification(mass_send_notify)
        if not notification_result:
            print("准备发表失败: 群发通知处理失败")
            return False
        
        print("  - 发表准备就绪")
        return True
    
    def _click_publish_button(self):
        """点击发表按钮的内部方法"""
        try:
            publish_btn = self.wait.until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "button.mass_send"))
            )
            publish_btn.click()
            print("  - 发布按钮点击成功")
            print("  - 等待6秒后开始检测'创作来源声明'弹窗")
            time.sleep(6)

            # 检测并处理"创作来源声明"弹窗
            try:
                dialog_wait = WebDriverWait(self.driver, 8)
                
                # 方法1：通过关键词文本检测弹窗（更灵活的方式）
                # 先查找所有弹窗，再检查是否包含关键词
                dialog = None
                keywords = [
                    "需对创作来源进行声明",
                    "创作来源",
                    "发表内容涉及国内外时事",
                    "AI生成"
                ]
                
                # 查找所有可见的弹窗
                dialogs = self.driver.find_elements(By.CSS_SELECTOR, "div.weui-desktop-dialog")
                for d in dialogs:
                    try:
                        if d.is_displayed():
                            # 检查弹窗中是否包含关键词
                            dialog_text = d.text
                            for keyword in keywords:
                                if keyword in dialog_text:
                                    dialog = d
                                    print(f"  - 检测到'创作来源声明'弹窗（关键词：{keyword}）")
                                    break
                            if dialog:
                                break
                    except:
                        continue
                
                # 方法2：如果方法1失败，尝试直接通过XPath查找包含关键词的弹窗
                if not dialog:
                    for keyword in keywords:
                        try:
                            dialog = dialog_wait.until(
                                EC.presence_of_element_located((
                                    By.XPATH, 
                                    f"//div[contains(@class, 'weui-desktop-dialog') and .//div[contains(text(), '{keyword}')]]"
                                ))
                            )
                            if dialog and dialog.is_displayed():
                                print(f"  - 检测到'创作来源声明'弹窗（XPath关键词：{keyword}）")
                                break
                        except:
                            continue
                
                # 如果找到弹窗，点击"无需声明并发表"按钮
                if dialog:
                    # 优先查找"无需声明并发表"按钮
                    button_text_candidates = [
                        "无需声明并发表",
                        "继续发表"
                    ]
                    continue_button = None
                    for button_text in button_text_candidates:
                        try:
                            # 先在弹窗内查找
                            continue_button = dialog.find_element(
                                By.XPATH,
                                f".//button[contains(text(), '{button_text}')]"
                            )
                            if continue_button.is_displayed():
                                print(f"  - 准备点击'{button_text}'按钮")
                                break
                        except:
                            continue
                    
                    # 如果弹窗内找不到，尝试在整个页面查找
                    if not continue_button:
                        for button_text in button_text_candidates:
                            try:
                                continue_button = self.driver.find_element(
                                    By.XPATH,
                                    f"//button[contains(text(), '{button_text}')]"
                                )
                                if continue_button.is_displayed():
                                    print(f"  - 准备点击'{button_text}'按钮（页面查找）")
                                    break
                            except:
                                continue

                    if continue_button:
                        self.driver.execute_script("arguments[0].click();", continue_button)
                        print("  - 已点击声明弹窗确认按钮")
                        time.sleep(1)
                    else:
                        print("  - ⚠️ 未找到'无需声明并发表'按钮")
                else:
                    print("  - 未检测到'创作来源声明'弹窗，继续执行")
                
            except TimeoutException:
                print("  - 未检测到'创作来源声明'弹窗，继续执行")
                pass
            except Exception as e:
                print(f"  - 处理'创作来源声明'弹窗时出错: {e}")
                pass

            return True
        except Exception as error:
            print(f"点击发表按钮失败: {error}")
            return False
    
    def _handle_mass_notification(self, mass_send_notify):
        """处理群发通知设置的内部方法"""
        if mass_send_notify:
            print("  - 用户选择群发，跳过关闭群发通知")
            return True
        
        # 尝试关闭群发通知
        max_attempts = 3
        for attempt in range(max_attempts):
            success = self.ensure_mass_notification_off()
            if success:
                print("  - 群发通知设置完成")
                return True
            
            if attempt < max_attempts - 1:
                print(f"  - 第{attempt + 1}次尝试关闭群发通知失败，等待2秒后重试...")
                time.sleep(2)
        
        print("  ⚠️ 警告：多次尝试后仍无法确保群发通知关闭，为安全起见终止发布")
        return False

    def ensure_mass_notification_off(self):
        """确保群发通知功能被关闭，使用多种方式尝试关闭"""
        try:
            print("  - 检查并关闭群发通知...")

            toggle_containers = self.driver.find_elements(By.CSS_SELECTOR, "div.mass_send__notify .weui-desktop-switch")
            if not toggle_containers:
                print("  - 未找到群发通知开关，可能已取消该设置，跳过")
                return True
            
            # 方法1: 使用原始的CSS选择器方式
            try:
                notification_switch = self.wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.mass_send__notify .weui-desktop-switch"))
                )
                switch_input = notification_switch.find_element(By.CSS_SELECTOR, "input.weui-desktop-switch__input")
                is_checked = switch_input.get_property("checked")
                
                if is_checked:
                    notification_switch.click()
                    time.sleep(1)
                    if not switch_input.get_property("checked"):
                        print("  - 群发通知关闭成功")
                        return True
                else:
                    print("  - 群发通知已经是关闭状态")
                    return True
            except TimeoutException:
                print("  - 群发通知开关未在预期时间内出现，跳过")
                return True
            except Exception as e:
                print("  - CSS选择器方法失败，尝试其他方法...")

            # 方法2: 使用JavaScript点击
            try:
                js_script = """
                    var switches = document.querySelectorAll('.mass_send__notify .weui-desktop-switch');
                    for(var i=0; i<switches.length; i++) {
                        var input = switches[i].querySelector('input');
                        if(input && input.checked) {
                            switches[i].click();
                            return true;
                        }
                    }
                    return false;
                """
                clicked = self.driver.execute_script(js_script)
                if clicked:
                    time.sleep(1)
                    # 验证是否真的关闭了
                    try:
                        switch_input = self.driver.find_element(By.CSS_SELECTOR, "div.mass_send__notify .weui-desktop-switch input")
                        if not switch_input.get_property("checked"):
                            print("  - 群发通知关闭成功（JavaScript方法）")
                            return True
                    except Exception:
                        print("  - JavaScript点击后未检测到开关，视为已关闭")
                        return True
            except Exception as e:
                print("  - JavaScript方法失败，尝试最后方法...")

            # 方法3: 使用XPath和ActionChains
            try:
                switch_xpath = "//div[contains(@class, 'mass_send__notify')]//label[contains(@class, 'weui-desktop-switch')]"
                switch_element = self.wait.until(
                    EC.presence_of_element_located((By.XPATH, switch_xpath))
                )
                
                # 使用ActionChains执行点击
                actions = ActionChains(self.driver)
                actions.move_to_element(switch_element)
                actions.click()
                actions.perform()
                
                time.sleep(1)
                # 验证是否关闭
                switch_input = switch_element.find_element(By.CSS_SELECTOR, "input")
                if not switch_input.get_property("checked"):
                    print("  - 群发通知关闭成功（ActionChains方法）")
                    return True
            except Exception as e:
                print(f"  - ActionChains方法也失败: {e}")

            # 最后检查状态
            try:
                final_check = self.driver.find_element(By.CSS_SELECTOR, "div.mass_send__notify .weui-desktop-switch input")
                if not final_check.get_property("checked"):
                    print("  - 最终检查：群发通知已关闭")
                    return True
            except Exception:
                print("  - 未检测到最终开关元素，跳过")
                return True

            print("  ⚠️ 警告：尝试所有方法后群发通知仍未关闭")
            return False

        except Exception as e:
            print(f"检查群发通知状态时出错: {e}")
            return False

    def _handle_original_verification_dialog(self):
        """处理原创校验弹窗流程 - 使用JavaScript暴力遍历方式，不依赖DOM结构"""
        # 1. 检测弹窗：直接搜索页面源码中的关键字（最简单可靠）
        print("  - 检测原创校验弹窗...")
        found_dialog = False
        for i in range(5):  # 尝试检测5次
            if "未通过原创校验逻辑" in self.driver.page_source:
                print("  - 检测到“原创校验”弹窗")
                found_dialog = True
                break
            time.sleep(1)
        
        if found_dialog:
            print("  - 等待弹窗稳定...")
            time.sleep(2)  # 等待弹窗完全加载
        else:
            print("  - 未检测到弹窗关键字，但继续尝试点击按钮（可能已打开）")

        # 2. 点击“下一步”按钮 - 使用JavaScript遍历所有按钮
        print("  - 尝试点击“下一步”按钮...")
        js_click_next = """
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            var btn = btns[i];
            // 检查文本是否为“下一步” 且 元素可见 (offsetParent 不为 null)
            if (btn.innerText.trim() === '下一步' && btn.offsetParent !== null) {
                btn.click();
                return true;
            }
        }
        return false;
        """
        if self.driver.execute_script(js_click_next):
            print("  - 已点击“下一步”按钮")
            time.sleep(2)  # 等待弹窗更新
        else:
            print("  - 未找到可见的“下一步”按钮（可能已点过或无需此步）")

        # 3. 点击“继续发表”按钮 - 使用JavaScript遍历所有按钮
        print("  - 尝试点击“继续发表”按钮...")
        js_click_continue = """
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            var btn = btns[i];
            // 检查文本是否包含“继续发表” 且 元素可见
            if (btn.innerText.indexOf('继续发表') !== -1 && btn.offsetParent !== null) {
                btn.click();
                return true;
            }
        }
        return false;
        """
        if self.driver.execute_script(js_click_continue):
            print("  - 已点击“继续发表”按钮")
        else:
            print("  - 未找到可见的“继续发表”按钮")
            # 备选方案：点击弹窗里所有可见的蓝色主按钮
            print("  - 尝试备选方案：点击弹窗里可见的蓝色按钮...")
            js_blind_click = """
            var clicked = false;
            var primary_btns = document.querySelectorAll('.weui-desktop-dialog .weui-desktop-btn_primary');
            for (var i = 0; i < primary_btns.length; i++) {
                var btn = primary_btns[i];
                if (btn.offsetParent !== null) {
                    btn.click();
                    clicked = true;
                }
            }
            return clicked;
            """
            if self.driver.execute_script(js_blind_click):
                print("  - 已执行备选点击")
            else:
                print("  - 备选方案也未找到可见按钮，可能流程已结束")

    def publish_article(self):
        """执行发表文章操作"""
        try:
            print("🔟  正式发表文章")
            
            # 首先等待发表对话框完全加载
            print("  - 等待发表对话框加载...")
            dialog_loaded = False
            max_dialog_wait = 10  # 最多等待10秒
            
            for i in range(max_dialog_wait):
                try:
                    # 检查是否存在发表对话框
                    dialog = self.driver.find_element(By.XPATH, "//div[contains(@class, 'weui-desktop-dialog')]//h3[contains(text(), '发表')]")
                    if dialog:
                        dialog_loaded = True
                        print("  - 发表对话框已加载")
                        break
                except:
                    time.sleep(1)
                    continue
            
            if not dialog_loaded:
                print("  - 警告：未检测到发表对话框，但继续尝试点击发表按钮")
            
            # 优化后的发表按钮点击策略
            publish_strategies = [
                {
                    'name': '发表按钮-精确定位',
                    'locator': (By.XPATH, "//div[contains(@class, 'weui-desktop-dialog__ft')]//button[contains(@class, 'weui-desktop-btn_primary') and text()='发表']")
                },
                {
                    'name': '发表按钮-类名定位',
                    'locator': (By.CSS_SELECTOR, "button.weui-desktop-btn.weui-desktop-btn_primary")
                },
                {
                    'name': '发表按钮-文本定位',
                    'locator': (By.XPATH, "//button[contains(@class, 'weui-desktop-btn_primary') and contains(text(), '发表')]")
                },
                {
                    'name': '发表按钮-对话框内定位',
                    'locator': (By.XPATH, "//div[contains(@class, 'mass-send__footer')]//button[contains(@class, 'weui-desktop-btn_primary')]")
                }
            ]
            
            # 点击发表按钮
            if not self.click_button_with_retry(publish_strategies, timeout=10):
                # 如果所有策略都失败，尝试使用JavaScript直接点击
                try:
                    print("  - 尝试使用JavaScript直接点击发表按钮...")
                    js_script = """
                        var buttons = document.querySelectorAll('button.weui-desktop-btn.weui-desktop-btn_primary');
                        for(var i = 0; i < buttons.length; i++) {
                            if(buttons[i].textContent.trim() === '发表') {
                                buttons[i].click();
                                return true;
                            }
                        }
                        return false;
                    """
                    clicked = self.driver.execute_script(js_script)
                    if clicked:
                        print("  - JavaScript点击发表按钮成功")
                    else:
                        raise Exception("JavaScript也无法点击发表按钮")
                except Exception as e:
                    print(f"  - JavaScript点击失败: {e}")
                    raise Exception("发表按钮点击失败")
            
            time.sleep(5)  # 等待弹窗动画完成
            
            # 优化后的确认按钮点击策略
            confirm_strategies = [
                {
                    'name': '无需声明并发表',
                    'locator': (By.XPATH, "//button[contains(text(), '无需声明并发表')]")
                },
                {
                    'name': '继续发表',
                    'locator': (By.XPATH, "//button[contains(text(), '继续发表')]")
                },
                {
                    'name': '确认发表-对话框内',
                    'locator': (By.XPATH, "//div[contains(@class, 'weui-desktop-dialog')]//button[contains(@class, 'weui-desktop-btn_primary') and not(contains(text(), '取消'))]")
                },
                {
                    'name': '确认发表-类名定位',
                    'locator': (By.CSS_SELECTOR, "button.weui-desktop-dialog__btn.weui-desktop-btn_primary")
                }
            ]
            
            if not self.click_button_with_retry(confirm_strategies, timeout=10):
                # 如果所有策略都失败，尝试使用JavaScript直接点击
                try:
                    print("  - 尝试使用JavaScript直接点击确认按钮...")
                    js_script = """
                        var buttons = document.querySelectorAll('button');
                        for(var i = 0; i < buttons.length; i++) {
                            var text = buttons[i].textContent.trim();
                            if(text === '无需声明并发表' || text === '继续发表' || 
                               (text === '发表' && buttons[i].classList.contains('weui-desktop-btn_primary'))) {
                                buttons[i].click();
                                return true;
                            }
                        }
                        return false;
                    """
                    clicked = self.driver.execute_script(js_script)
                    if clicked:
                        print("  - JavaScript点击确认按钮成功")
                    else:
                        raise Exception("JavaScript也无法点击确认按钮")
                except Exception as e:
                    print(f"  - JavaScript点击失败: {e}")
                    raise Exception("确认发表按钮点击失败")

            # 等待弹窗加载后再检测原创校验弹窗
            time.sleep(1)
            self._handle_original_verification_dialog()
            
            print("  - 等发表完成...跳过检测")
            
            # 检测是否发表成功 - 通过检查"已发表"元素是否出现
            publish_success = False # 修正：初始值应为False而不是Ture
            max_wait_time = 20  # 最长等待16秒
            start_time = time.time()
            
            # 根据测试找到的精确XPath位置
            success_xpath = "//*[@id=\"list\"]/div[1]/div[1]/div[1]/div[2]/div[1]/div[1]/div[1]/div[2]/span[1]"
            progress_text = "正在发表"  # 进度文本(用于日志显示)
            
            # 是否首次显示等待信息
            first_waiting = True
            
            while time.time() - start_time < max_wait_time:
                try:
                    # 尝试查找"已发表"元素
                    try:
                        # 使用找到的精确XPath查找元素
                        element = self.driver.find_element(By.XPATH, success_xpath)
                        text = element.text.strip()
                        
                        # 检查是否包含"已发表"
                        if "已发表" in text:
                            publish_success = True
                            print(f"  - ✅ 检测到已发表状态！")
                            break
                        elif progress_text in text:
                            print(f"  - 检测到状态: {text} (等待发表完成...)")
                    except:
                        # 元素可能还未出现，只在首次显示等待信息
                        if first_waiting:
                            print("  - 等待发表状态变更...")
                            first_waiting = False # 修正：应为False而不是Ture
                        
                    # 短暂等待后再次检查
                    time.sleep(0.3)
                    
                except Exception as e:
                    print(f"  - 检测发表状态时出错: {e}")
                    time.sleep(0.3)
            
            if publish_success:
                print(f"  - 🎉 确认表成功！文章已发表")
            else:
                print("  ⚠️ 警告：未检测到\"已发表\"状态，但将继续执行")
                # 无论检测结果如何，都视为成功
                publish_success = True
            
            # 移动文件到已发布文件夹
            if self.md_file_path:
                published_folder = PUBLISHED_FOLDER
                os.makedirs(published_folder, exist_ok=True)
                target_path = os.path.join(published_folder, os.path.basename(self.md_file_path))
                
                try:
                    shutil.move(self.md_file_path, target_path)
                    print(f"已将 {os.path.basename(self.md_file_path)} 移动到 {published_folder}")
                except Exception as e:
                    print(f"移动文件失败: {e}")
            
            print("  - 文章发布成功")
            return True
        
        except Exception as e:
            print(f"发布文章失败: {e}")
            return False

    def clear_wechat_cookies(self):
        """精确清除微信相关的cookie"""
        try:
            # 获取所有cookie
            cookies = self.driver.get_cookies()
            
            # 微信相关的关键cookie名称
            wechat_cookie_names = [
                'ua_id',        # 初始访问标识
                'uuid',         # 登录会话标识
                'bizuin',       # 公众号身份标识
                'ticket',       # 登录票据
                'ticket_id',    # 票据ID
                'slave_sid',    # 会话ID
                'slave_user',   # 用户标识
                'data_ticket',  # 数据票据
                'cert'          # 证书
            ]
            
            # 精确删除微信相关cookie
            for cookie in cookies:
                if (cookie['domain'] == 'mp.weixin.qq.com' and 
                    any(name in cookie['name'].lower() for name in wechat_cookie_names)):
                    try:
                        self.driver.delete_cookie(cookie['name'])
                        print(f"  - 已清除cookie: {cookie['name']}")
                    except Exception as e:
                        print(f"  - 清除cookie {cookie['name']} 失败: {e}")
            
            print("  - 微信登录相关cookie已清除")
            return True
        except Exception as e:
            print(f"清除cookie失败: {e}")
            return False

# 主类 - 协调三个子类完成整个流程
class WeChatPublisher:
    def __init__(self, browser_manager):
        """初始化WeChatPublisher对象"""
        self.browser_manager = browser_manager
        self.driver = browser_manager.driver
        self.wait = browser_manager.wait
        
        # 不再设置默认 cookie/token 路径，必须显式设置
        self.cookie_file = None
        self.token_file = None
        self.md_file_path = None
        self.articles_per_publish = 1
        
        # 新增：存储完整账号信息
        self.account_info = None
        
        # 初始化三个子类
        self.login_manager = WeChatLogin(browser_manager)
        self.content_editor = ContentEditor(browser_manager)
        self.publish_manager = PublishManager(browser_manager)
        
        # 设置子类属性
        self.setup()
        
        # 存储token
        self.token = None

    def set_account_files(self, cookie_file, token_file):
        """设置账号专属的cookie和token文件路径，并同步到login_manager"""
        self.cookie_file = cookie_file
        self.token_file = token_file
        self.login_manager.cookie_file = cookie_file
        self.login_manager.token_file = token_file

    def set_account_info(self, account_info):
        """设置完整账号信息，并同步到content_editor"""
        self.account_info = account_info
        self.content_editor.account_info = account_info

    def setup(self):
        """设置共享属性（目前只同步md_file_path）"""
        self.publish_manager.md_file_path = self.md_file_path
        
    def get_token(self):
        """获取token并同步"""
        self.token = self.login_manager.token
        return self.token
        
    def run(self):
        """运行主程序"""
        try:
            print("=== 开始自动化发文流程 ===")
            
            # 1. 登录阶段
            if self.login_manager.open_wechat_admin():
                if self.login_manager.click_content_management():
                    time.sleep(2)
                    if self.login_manager.click_drafts():
                        time.sleep(2)
                        if self.login_manager.click_new_article():
                            if self.login_manager.switch_to_edit_window():
                                
                                # 2. 内容编辑阶段
                                if self.content_editor.input_article_content():
                                    # 使用新的随机图片功能
                                    if self.content_editor.upload_cover_image():
                                        
                                        # 3. 发布阶段
                                        if self.publish_manager.prepare_for_publish():
                                            self.publish_manager.publish_article()
                                            
            print("\n=== 流程执行完成 ===")
            input("按回车键退出...")
        finally:
            if 'driver' in locals():
                # 不再自动关闭浏览器
                # driver.quit()
                pass

if __name__ == "__main__":
    try:
        # 创建命令行参数解析器
        parser = argparse.ArgumentParser(description='微信公众号文章发布工具')
        parser.add_argument('--login-only', action='store_true', 
                           help='仅执行登录操作，保存Cookie并退出')
        args = parser.parse_args()
        
        # 配置Chrome选项
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
        current_dir = os.path.dirname(os.path.abspath(__file__))
        user_data_dir = os.path.join(current_dir, 'chrome_user_data')
        if not os.path.exists(user_data_dir):
            os.makedirs(user_data_dir)
            
        # 清理可能存在的DevTools文件
        devtools_file = os.path.join(user_data_dir, 'Default', 'DevTools Active Port')
        if os.path.exists(devtools_file):
            try:
                os.remove(devtools_file)
                print("  - 已清理DevTools文件")
            except:
                pass
                
        chrome_options.add_argument(f'--user-data-dir={user_data_dir}')
        
        # 初始化浏览器
        print("  - 正在启动浏览器...")
        driver = webdriver.Chrome(options=chrome_options)
        driver.maximize_window()
        wait = WebDriverWait(driver, 20)
        print("  - 浏览器启动成功")
        
        # 创建浏览器管理器
        class BrowserManager:
            def __init__(self, driver, wait):
                self.driver = driver
                self.wait = wait
        
        browser_manager = BrowserManager(driver, wait)
        
        # 创建发布器实例
        publisher = WeChatPublisher(browser_manager)
        
        # 根据参数决定执行模式
        if args.login_only:
            # 只执行登录检查和登录操作
            if publisher.login_manager.open_wechat_admin():
                print("\n当前状态：已登录")
                print("Cookie已保存，可以关闭程序了")
            else:
                print("\n登录失败，请重试")
        else:
            # 执行完整发布流程
            publisher.run()
            
        input("\n按回车键退出...")
        
    except Exception as e:
        print(f"发生错误: {e}")
        import traceback
        print(traceback.format_exc())
    finally:
        if 'driver' in locals():
            # 不再自动关闭浏览器
            # driver.quit()
            pass