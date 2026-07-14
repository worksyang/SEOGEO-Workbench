from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import time

def debug_publish():
    """测试发表功能 - 连接已打开的浏览器进行测试"""
    # 1. 连接已打开的 Chrome (端口 9222)
    print("正在连接浏览器...")
    try:
        chrome_options = Options()
        chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
        driver = webdriver.Chrome(options=chrome_options)
        wait = WebDriverWait(driver, 20)
        print(f"✅ 已连接，当前页面标题: {driver.title}")
    except Exception as e:
        print(f"❌ 连接失败，请确保 Chrome 已通过命令行启动且端口为 9222。错误: {e}")
        return

    # 2. 准备发表流程
    print("\n--- 步骤1: 准备发表 ---")
    if not click_publish_button(driver, wait):
        print("❌ 点击发表按钮失败")
        return
    
    # 等待弹窗出现
    print("⏳ 等待弹窗出现...")
    time.sleep(3)
    
    # 3. 处理群发通知设置
    print("\n--- 步骤2: 处理群发通知 ---")
    if not handle_mass_notification(driver, wait):
        print("⚠️ 警告：群发通知处理失败，但继续执行")
    
    print("\n✅ 发表准备完成！")

def click_publish_button(driver, wait):
    """点击发表按钮并处理创作来源声明弹窗"""
    try:
        print("  - 查找发表按钮...")
        publish_btn = wait.until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button.mass_send"))
        )
        publish_btn.click()
        print("  - ✅ 发表按钮点击成功")
        print("  - ⏳ 等待6秒后开始检测'创作来源声明'弹窗...")
        time.sleep(6)

        # 检测并处理"创作来源声明"弹窗
        try:
            dialog_wait = WebDriverWait(driver, 8)
            
            # 方法1：通过关键词文本检测弹窗
            dialog = None
            keywords = [
                "需对创作来源进行声明",
                "创作来源",
                "发表内容涉及国内外时事",
                "AI生成"
            ]
            
            # 查找所有可见的弹窗
            dialogs = driver.find_elements(By.CSS_SELECTOR, "div.weui-desktop-dialog")
            for d in dialogs:
                try:
                    if d.is_displayed():
                        dialog_text = d.text
                        for keyword in keywords:
                            if keyword in dialog_text:
                                dialog = d
                                print(f"  - ✅ 检测到'创作来源声明'弹窗（关键词：{keyword}）")
                                break
                        if dialog:
                            break
                except:
                    continue
            
            # 方法2：如果方法1失败，尝试通过XPath查找
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
                            print(f"  - ✅ 检测到'创作来源声明'弹窗（XPath关键词：{keyword}）")
                            break
                    except:
                        continue
            
            # 如果找到弹窗，点击"无需声明并发表"按钮
            if dialog:
                button_text_candidates = ["无需声明并发表", "继续发表"]
                continue_button = None
                
                # 先在弹窗内查找
                for button_text in button_text_candidates:
                    try:
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
                            continue_button = driver.find_element(
                                By.XPATH,
                                f"//button[contains(text(), '{button_text}')]"
                            )
                            if continue_button.is_displayed():
                                print(f"  - 准备点击'{button_text}'按钮（页面查找）")
                                break
                        except:
                            continue

                if continue_button:
                    driver.execute_script("arguments[0].click();", continue_button)
                    print("  - ✅ 已点击声明弹窗确认按钮")
                    time.sleep(1)
                else:
                    print("  - ⚠️ 未找到'无需声明并发表'按钮")
            else:
                print("  - 未检测到'创作来源声明'弹窗，继续执行")
            
        except TimeoutException:
            print("  - 未检测到'创作来源声明'弹窗，继续执行")
        except Exception as e:
            print(f"  - 处理'创作来源声明'弹窗时出错: {e}")

        return True
        
    except Exception as error:
        print(f"  - ❌ 点击发表按钮失败: {error}")
        return False

def handle_mass_notification(driver, wait):
    """处理群发通知设置 - 确保关闭群发通知"""
    try:
        print("  - 检查群发通知开关...")
        
        toggle_containers = driver.find_elements(By.CSS_SELECTOR, "div.mass_send__notify .weui-desktop-switch")
        if not toggle_containers:
            print("  - 未找到群发通知开关，可能已取消该设置")
            return True
        
        # 方法1: 使用CSS选择器方式
        try:
            notification_switch = wait.until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.mass_send__notify .weui-desktop-switch"))
            )
            switch_input = notification_switch.find_element(By.CSS_SELECTOR, "input.weui-desktop-switch__input")
            is_checked = switch_input.get_property("checked")
            
            if is_checked:
                notification_switch.click()
                time.sleep(1)
                if not switch_input.get_property("checked"):
                    print("  - ✅ 群发通知已关闭")
                    return True
            else:
                print("  - 群发通知已经是关闭状态")
                return True
        except TimeoutException:
            print("  - 群发通知开关未在预期时间内出现")
            return True
        except Exception as e:
            print(f"  - CSS选择器方法失败，尝试其他方法: {e}")

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
            clicked = driver.execute_script(js_script)
            if clicked:
                time.sleep(1)
                print("  - ✅ 群发通知已关闭（JavaScript方法）")
                return True
        except Exception as e:
            print(f"  - JavaScript方法失败: {e}")

        # 最后检查状态
        try:
            final_check = driver.find_element(By.CSS_SELECTOR, "div.mass_send__notify .weui-desktop-switch input")
            if not final_check.get_property("checked"):
                print("  - ✅ 最终检查：群发通知已关闭")
                return True
        except:
            pass

        print("  - ⚠️ 警告：尝试所有方法后群发通知仍未关闭")
        return False
        
    except Exception as e:
        print(f"  - 检查群发通知状态时出错: {e}")
        return False

if __name__ == "__main__":
    debug_publish()
