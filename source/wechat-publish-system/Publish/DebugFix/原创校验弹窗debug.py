from selenium import webdriver
from selenium.webdriver.chrome.options import Options
import time

def debug_click_original():
    # 1. 连接已打开的 Chrome (端口 9222)
    print("正在连接浏览器...")
    try:
        chrome_options = Options()
        chrome_options.add_experimental_option("debuggerAddress", "127.0.0.1:9222")
        driver = webdriver.Chrome(options=chrome_options)
        print(f"✅ 已连接，当前页面标题: {driver.title}")
    except Exception as e:
        print(f"❌ 连接失败，请确保 Chrome 已通过命令行启动且端口为 9222。错误: {e}")
        return

    # 2. 识别原创弹窗（基于文本内容）
    print("\n--- 步骤1: 检测弹窗 ---")
    found_dialog = False
    for i in range(5): # 尝试检测5次
        # 直接搜源码，最快最简单
        if "未通过原创校验逻辑" in driver.page_source:
            print("✅ 检测到“原创校验”弹窗！")
            found_dialog = True
            break
        print(f"  ({i+1}/5) 未检测到关键字，等待 1秒...")
        time.sleep(1)
    
    if found_dialog:
        print("⏳ 等待 2秒...")
        time.sleep(2)
    else:
        print("⚠️ 警告：未检测到弹窗关键字，但继续尝试点击按钮（也许是你已经点开了）")

    # 3. 点击“下一步”
    print("\n--- 步骤2: 点击“下一步” ---")
    # Vibe 逻辑：不管结构多复杂，遍历所有可见按钮，谁叫“下一步”就点谁
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
    if driver.execute_script(js_click_next):
        print("✅ 已点击“下一步”按钮")
        print("⏳ 等待 2秒...")
        time.sleep(2)
    else:
        print("❌ 未找到可见的“下一步”按钮（可能已经点过了？或者无需此步？）")

    # 4. 点击“继续发表”
    print("\n--- 步骤3: 点击“继续发表” ---")
    # Vibe 逻辑：同上，谁叫“继续发表”就点谁
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
    if driver.execute_script(js_click_continue):
        print("✅ 已点击“继续发表”按钮")
    else:
        print("❌ 未找到可见的“继续发表”按钮")
        
        # 备选方案：如果上面的精确匹配失败，尝试“盲点”所有可见的蓝色主按钮
        print("  -> 尝试备选方案：点击弹窗里所有可见的“蓝色按钮”...")
        js_blind_click = """
        var clicked = false;
        var primary_btns = document.querySelectorAll('.weui-desktop-dialog .weui-desktop-btn_primary');
        for (var i = 0; i < primary_btns.length; i++) {
            var btn = primary_btns[i];
            if (btn.offsetParent !== null) { // 只要是可见的
                console.log('点击了备选按钮:', btn.innerText);
                btn.click();
                clicked = true;
            }
        }
        return clicked;
        """
        if driver.execute_script(js_blind_click):
            print("✅ 已执行备选点击")
        else:
            print("❌ 备选方案也未找到可见按钮")

if __name__ == "__main__":
    debug_click_original()
