"""
📝 Markdown 转微信公众号 wxHTML 工具

🔍 模块简介:
- 本模块开发目的：实现 Markdown 内容到微信公众号 wxHTML 的自动化转换
- 基于 Selenium 实现浏览器自动化操作
- 支持 Markdown 文件的批量处理
- 支持剪贴板操作，实现内容快速复制

🛠️ 主要功能与处理流程:

1. Markdown 文件处理 (MarkdownReader)
- 📂 支持指定文件夹中的 Markdown 文件读取
- 📝 自动处理文件编码
- 🔄 支持文件内容读取和验证

2. 编辑器操作 (EditorOpener)
- 🌐 自动打开在线 Markdown 编辑器
- 🔄 智能等待页面加载
- 🚫 自动处理更新提示弹窗

3. wxHTML 转换 (ContentManager)
- 📋 支持内容自动输入和替换
- 📋 自动复制转换后的 wxHTML 内容
- ⚡ 使用剪贴板操作提高效率

4. Markdown转换协调 (MdToWxHtmlConverter)
- 📊 整合上述组件完成转换
- 📝 提供简洁的接口
- 🔄 管理浏览器生命周期
"""

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.keys import Keys
import os
import time
import pyperclip

class MarkdownReader:
    """负责Markdown文件的读取和处理"""
    
    def __init__(self, md_folder):
        self.md_folder = md_folder
        
    def read_md_content(self, file_path):
        """读取markdown文件内容"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return content
        except Exception as e:
            print(f"读取文件失败: {e}")
            return None
    
    def get_available_md_files(self):
        """获取指定文件夹中的md文件列表（不包括子文件夹）"""
        try:
            # 只获取指定文件夹中的md文件，不遍历子文件夹
            md_files = []
            for file in os.listdir(self.md_folder):
                if file.endswith('.md'):
                    md_files.append(os.path.join(self.md_folder, file))
            return md_files
        except Exception as e:
            print(f"获取md文件列表失败: {e}")
            return []

class EditorOpener:
    """负责打开和准备Markdown编辑器"""
    
    def __init__(self, browser_manager):
        self.driver = browser_manager.driver
        self.wait = browser_manager.wait
        
    def open_in_new_tab(self):
        """在新标签页中打开Markdown编辑器"""
        try:
            print("1️⃣  正在新标签页中打开Markdown编辑器...")
            # 使用JavaScript打开新标签页
            self.driver.execute_script("window.open('https://md.weiyan.cc/');")
            
            # 等待新窗口打开
            time.sleep(2)
            
            # 切换到新打开的标签页
            self.driver.switch_to.window(self.driver.window_handles[-1])
            
            # 等待页面加载完成
            self.wait.until(
                EC.presence_of_element_located((By.CLASS_NAME, "CodeMirror"))
            )
            print("  - Markdown编辑器在新标签页打开成功")
            
            # 处理可能出现的更新说明弹窗
            try:
                popup_wait = WebDriverWait(self.driver, 3)
                confirm_button = popup_wait.until(
                    EC.element_to_be_clickable((By.CLASS_NAME, "ant-btn.ant-btn-primary"))
                )
                confirm_button.click()
                print("  - 成功：关闭更新说明弹窗")
            except:
                print("  - 缓存加载成功")
            
            return True
            
        except Exception as e:
            print(f"在新标签页打开Markdown编辑器失败: {e}")
            return False

class ContentManager:
    """负责内容输入和复制HTML"""
    
    def __init__(self, browser_manager):
        self.driver = browser_manager.driver
        self.wait = browser_manager.wait
        
    def input_markdown_content(self, content):
        """在编辑器中输入Markdown内容"""
        try:
            print("2️⃣  准备输入Markdown内容...")
            
            # 等待编辑器加载完成
            editor = self.wait.until(
                EC.presence_of_element_located((By.CLASS_NAME, "CodeMirror"))
            )
            
            # 将内容复制到系统剪贴板
            pyperclip.copy(content)
            time.sleep(0.5)
            
            # 点击编辑器区域激活
            editor.click()
            
            # 全选当前内容 (Ctrl+A)
            ActionChains(self.driver).key_down(Keys.CONTROL).send_keys('a').key_up(Keys.CONTROL).perform()
            time.sleep(0.5)
            
            # 粘贴新内容 (Ctrl+V)
            ActionChains(self.driver).key_down(Keys.CONTROL).send_keys('v').key_up(Keys.CONTROL).perform()
            time.sleep(1.5)
            
            print("  - Markdown内容输入成功")
            return True
            
        except Exception as e:
            print(f"输入Markdown内容失败: {e}")
            return False

    def click_copy_button(self):
        """点击复制按钮"""
        try:
            print("3️⃣  准备复制转换后的HTML...")
            
            # 使用id选择器
            copy_button = self.wait.until(
                EC.element_to_be_clickable((By.ID, "nice-sidebar-wechat"))
            )
            copy_button.click()
            time.sleep(2)
            print("  - 已点击复制按钮")
            return True
            
        except Exception as e:
            print(f"点击复制按钮失败: {e}")
            return False

class MdToWxHtmlConverter:
    """协调Markdown到微信HTML的转换流程"""
    
    def __init__(self, md_folder, browser_manager):
        self.browser_manager = browser_manager
        self.driver = browser_manager.driver
        
        # 初始化各个组件
        self.reader = MarkdownReader(md_folder)
        self.editor_opener = EditorOpener(browser_manager)
        self.content_manager = ContentManager(browser_manager)
        
    def convert_md_to_html(self, md_file_path):
        """将Markdown文件转换为微信HTML并存入剪贴板"""
        try:
            print("\n=== 开始Markdown转换流程（切换窗口） ===")
            
            # 记住当前窗口句柄
            current_handle = self.driver.current_window_handle
            
            # 读取文件内容
            content = self.reader.read_md_content(md_file_path)
            if not content:
                return False
            
            # 检查是否已经有 weiyan.cc 窗口打开
            weiyan_handle = None
            for handle in self.driver.window_handles:
                self.driver.switch_to.window(handle)
                if 'md.weiyan.cc' in self.driver.current_url:
                    weiyan_handle = handle
                    print("  - 找到已打开的Markdown编辑器窗口")
                    break
            
            # 如果没有 weiyan.cc 窗口，则打开新窗口
            if not weiyan_handle:
                print("  - 未找到Markdown编辑器窗口，正在打开新窗口...")
                # 先切回原窗口
                self.driver.switch_to.window(current_handle)
                # 打开新窗口
                if not self.editor_opener.open_in_new_tab():
                    return False
                # 获取新打开的窗口句柄
                weiyan_handle = self.driver.window_handles[-1]
            else:
                # 如果找到了已打开的窗口，刷新页面以确保状态正常
                print("  - 正在刷新Markdown编辑器窗口...")
                self.driver.refresh()
                time.sleep(2)  # 等待刷新完成
            
            # 输入Markdown内容
            if not self.content_manager.input_markdown_content(content):
                return False
            
            # 复制转换后的HTML
            if not self.content_manager.click_copy_button():
                return False
            
            print("  - HTML内容已复制到剪贴板")
            
            # 切换回原窗口
            self.driver.switch_to.window(current_handle)
            
            print("=== Markdown转换完成，已返回编辑器窗口 ===")
            return True
            
        except Exception as e:
            print(f"Markdown转换失败: {e}")
            # 尝试切回原窗口
            try:
                self.driver.switch_to.window(current_handle)
            except:
                pass
            return False