"""
📝 PromptLogger —— AI提示词日志追踪模块

本模块开发目的：
    实现对每次AI调用的「prompt（提示词）」
    内容进行本地化详细记录，
    便于后续追溯诊断、对话还原、内容溯源和调试。
- 📁 支持自定义日志存储目录，默认为 "C:\\txt"
- ✏️ 支持记录每次生成的完整 prompt 内容
- 📝 文件以「任务名称+时间戳」自动命名，确保唯一性和可追溯性
- 📄 每条日志为独立文本文件，内容即当次 prompt

"""
import os
import re
import datetime
from typing import Optional

class PromptLogger:
    def __init__(self, log_directory: str = "C:\\txt"):
        """
        初始化 PromptLogger.

        Args:
            log_directory: 保存提示词日志文件的目录.
        """
        self.log_directory = log_directory
        try:
            if not os.path.exists(self.log_directory):
                os.makedirs(self.log_directory)
                print(f"日志目录已创建: {self.log_directory}")
        except Exception as e:
            print(f"❌ 创建日志目录失败 '{self.log_directory}': {e}")
            self.log_directory = None 

    def _sanitize_filename(self, filename: str) -> str:
        """
        清理文件名，移除或替换不适用于文件名的字符.
        """
        filename = str(filename) # 确保是字符串
        filename = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', filename)
        filename = re.sub(r'[\s.]+', '_', filename)
        if not filename: # 如果清理后为空
            filename = "untitled"
        return filename[:100]

    def log_prompt(self, task_name: str, prompt_content: str) -> Optional[str]:
        """
        将提示词内容记录到文件.

        Args:
            task_name: 任务名称 (例如文章标题)，用于构建文件名.
            prompt_content: 要保存的完整提示词内容.

        Returns:
            Optional[str]: 如果成功，返回保存的文件路径；否则返回 None.
        """
        if not self.log_directory:
            print("⚠️ PromptLogger 未正确初始化 (日志目录无效)，无法记录提示词。")
            return None

        filepath = None  # 初始化 filepath 以免在异常情况下引用未定义变量
        try:
            sanitized_task_name = self._sanitize_filename(task_name)
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            
            filename = f"{sanitized_task_name}_{timestamp}.txt"
            filepath = os.path.join(self.log_directory, filename)

            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(prompt_content)
            
            return filepath
        except IOError as e:
            # 确保 filepath 在打印错误时已定义
            error_path = filepath if filepath else "未知路径"
            print(f"❌ 写入提示词日志文件失败 '{error_path}': {e}")
            return None
        except Exception as e:
            print(f"❌ 记录提示词时发生未知错误 (任务: {task_name}): {e}")
            return None

if __name__ == '__main__':
    # 测试 PromptLogger
    # 注意：直接在 C:\txt 测试可能需要管理员权限或者会导致混乱
    # 建议测试时使用用户目录下的子文件夹
    test_log_dir = os.path.join(os.path.expanduser("~"), "prompt_logger_test_output")
    print(f"测试日志将保存在: {test_log_dir}")
    
    logger = PromptLogger(log_directory=test_log_dir)
    
    test_title_1 = "这是一个正常的标题"
    test_prompt_1 = "这是第一个测试提示词的内容。\n包含换行符。"
    path1 = logger.log_prompt(test_title_1, test_prompt_1)
    if path1:
        print(f"测试1 成功记录到: {path1}")

    test_title_2 = "标题包含/特殊\\:*?\"<>|字符"
    test_prompt_2 = "这是第二个提示词，用于测试特殊字符文件名处理。"
    path2 = logger.log_prompt(test_title_2, test_prompt_2)
    if path2:
        print(f"测试2 成功记录到: {path2}")

    test_title_3 = "" # 空标题
    test_prompt_3 = "测试空标题的情况。"
    path3 = logger.log_prompt(test_title_3, test_prompt_3)
    if path3:
        print(f"测试3 成功记录到: {path3}")
        
    logger_fail_dir = PromptLogger(log_directory="Z:\\non_existent_drive_for_testing")
    path_fail = logger_fail_dir.log_prompt("test_fail_dir", "wont be logged")
    if not path_fail:
        print("测试4 目录创建失败，日志未记录 (符合预期)")

    # 模拟写入失败 (例如磁盘满或权限问题，较难直接模拟，但IOError会捕获)
    # 假设 _sanitize_filename 返回了一个无效的路径组件（虽然不太可能）
    # 或者手动创建一个只读的 C:\txt 目录进行测试
    print("\n如果 C:\\txt 目录存在且可写，下面的测试会尝试写入。")
    logger_c_txt = PromptLogger() # 使用默认 C:\txt
    path_c_txt = logger_c_txt.log_prompt("c_drive_test", "Test content for C:\\txt")
    if path_c_txt :
        print(f"测试 C:\\txt 成功记录到: {path_c_txt}")
    else:
        print(f"测试 C:\\txt 记录失败。请检查 C:\\txt 目录权限或是否存在问题。") 

