import os
import subprocess
import socket
import time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

# ====== 配置部分 ======
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"  # 你的Chrome路径
DEBUG_PORT = 9222  # 远程调试端口
USER_DATA_DIR = r"C:\chrome_debug_temp"  # 临时用户数据目录
# =====================

def is_port_in_use(port):
    """检测端口是否被占用（即Chrome是否已启动）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0

def start_chrome_with_debug():
    """启动带远程调试端口的Chrome"""
    if not os.path.exists(USER_DATA_DIR):
        os.makedirs(USER_DATA_DIR)
    cmd = [
        CHROME_PATH,
        f"--remote-debugging-port={DEBUG_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check"
    ]
    print(f"正在启动Chrome...\n命令: {' '.join(cmd)}")
    subprocess.Popen(cmd)
    print("请手动登录并跳转到你要测试的页面（如 https://mp.weixin.qq.com），然后回到本窗口继续...")
    # 等待用户操作
    input("准备好后请按回车继续...")

def test_wechat_original():
    """点击“未声明”，输入作者，勾选协议，点击确定"""
    chrome_options = Options()
    chrome_options.debugger_address = f"127.0.0.1:{DEBUG_PORT}"
    driver = webdriver.Chrome(options=chrome_options)
    try:
        print("正在查找“未声明”按钮...")
        unset_btn = driver.find_element(By.CSS_SELECTOR, "div.js_unset_original_title")
        unset_btn.click()
        print("✅ 已点击“未声明”按钮")

        time.sleep(1)
        print("正在查找可见的作者输入框...")
        author_inputs = driver.find_elements(By.CSS_SELECTOR, "span.js_customerauthor_container input.js_author")
        author_input = None
        for inp in author_inputs:
            if inp.is_displayed():
                author_input = inp
                break

        if author_input is None:
            print("❌ 没有找到可见的作者输入框！")
        else:
            # 先点击激活
            author_input.click()
            time.sleep(0.2)
            # 清空再输入
            author_input.clear()
            author_input.send_keys("希希")
            time.sleep(0.5)
            # 用JS触发input事件（有些页面需要）
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }))", author_input)
            print("✅ 已输入作者")

        time.sleep(0.5)
        print("正在勾选协议复选框...")
        checkbox = driver.find_element(By.CSS_SELECTOR, "div.original_agreement input[type='checkbox']")
        if not checkbox.is_selected():
            try:
                checkbox.click()
            except Exception:
                driver.execute_script("arguments[0].click();", checkbox)
        print("✅ 已勾选协议")

        time.sleep(0.5)
        print("正在点击“确定”按钮...")
        ok_btn = driver.find_element(By.CSS_SELECTOR, "button.weui-desktop-btn_primary")
        ok_btn.click()
        print("✅ 已点击“确定”按钮，测试完成！")

    except NoSuchElementException as e:
        print("❌ 没有找到指定元素！", e)
    except Exception as e:
        print(f"❌ 操作元素时出错: {e}")
    finally:
        input("测试完成，按回车关闭浏览器...")
        driver.quit()

def main():
    if is_port_in_use(DEBUG_PORT):
        print(f"检测到端口 {DEBUG_PORT} 已有Chrome在运行，直接连接...")
    else:
        print(f"未检测到端口 {DEBUG_PORT} 的Chrome，自动启动...")
        start_chrome_with_debug()
    # 等待端口可用
    for _ in range(10):
        if is_port_in_use(DEBUG_PORT):
            break
        time.sleep(1)
    else:
        print("Chrome 启动失败，请检查路径和端口！")
        return
    test_wechat_original()

if __name__ == "__main__":
    main() 