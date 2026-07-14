"""
启动调试模式的 Chrome 浏览器
用于 RPA 热调试 - 连接已打开的浏览器进行测试
"""
import os
import subprocess
import sys
from pathlib import Path

def find_chrome_path():
    """自动查找 Chrome 安装路径"""
    possible_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
    ]
    
    for path in possible_paths:
        if os.path.exists(path):
            return path
    
    # 如果都找不到，尝试从注册表查找（Windows）
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
        )
        chrome_path = winreg.QueryValue(key, None)
        winreg.CloseKey(key)
        if os.path.exists(chrome_path):
            return chrome_path
    except:
        pass
    
    return None

def start_debug_chrome():
    """启动调试模式的 Chrome"""
    chrome_path = find_chrome_path()
    
    if not chrome_path:
        print("❌ 未找到 Chrome 安装路径")
        print("请手动指定 Chrome 路径，或确保 Chrome 已正确安装")
        return False
    
    print(f"✅ 找到 Chrome: {chrome_path}")
    
    # 使用项目目录下的临时目录作为用户数据目录（避免D盘问题）
    current_dir = Path(__file__).parent
    user_data_dir = current_dir / "debug_chrome_data"
    user_data_dir.mkdir(exist_ok=True)
    
    print(f"📁 用户数据目录: {user_data_dir}")
    
    # 调试端口
    debug_port = 9222
    
    # 构建启动命令
    cmd = [
        chrome_path,
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    
    print(f"\n🚀 正在启动调试模式的 Chrome...")
    print(f"   调试端口: {debug_port}")
    print(f"   用户数据目录: {user_data_dir}")
    print(f"\n💡 提示：")
    print(f"   1. Chrome 窗口会打开，请手动登录微信公众号")
    print(f"   2. 操作到需要测试的步骤（比如原创弹窗）")
    print(f"   3. 然后运行 debug-fix.py 来连接这个浏览器进行测试")
    print(f"\n按 Ctrl+C 可以关闭此窗口（Chrome 会继续运行）\n")
    
    try:
        # 启动 Chrome（不等待它关闭）
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == 'win32' else 0
        )
        
        print("✅ Chrome 已启动！")
        print(f"   进程ID: {process.pid}")
        print(f"\n现在可以运行 debug-fix.py 来连接这个浏览器了")
        
        # 等待用户按 Ctrl+C
        try:
            process.wait()
        except KeyboardInterrupt:
            print("\n\n⚠️  注意：关闭此窗口不会关闭 Chrome")
            print("   如需关闭 Chrome，请手动关闭浏览器窗口")
        
        return True
        
    except Exception as e:
        print(f"❌ 启动失败: {e}")
        return False

if __name__ == "__main__":
    start_debug_chrome()

