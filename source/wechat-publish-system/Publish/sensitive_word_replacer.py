"""
📝 敏感词替换工具

📝 模块简介:
- 本模块开发目的：自动检测和替换文本中的敏感词，保障内容合规

🔍 主要功能与处理流程:

1. 敏感词管理 (SensitiveWordReplacer)
- 📋 从配置文件加载敏感词替换规则
- 🔍 支持精确匹配替换
- 📊 替换统计与报告生成

2. 文本处理功能
- 📄 单段文本敏感词替换
- 📂 单个文件处理
- 📁 整个文件夹批量处理
- 🔍 支持文件模式匹配

3. 报告生成
- 📊 处理文件统计
- 📑 替换详情记录
- ❌ 错误文件记录

4. 异常处理
- 🛡️ 文件读写异常处理
- 📝 详细的错误日志输出
"""

import os
import glob
from typing import Dict, List, Tuple, Optional

class SensitiveWordReplacer:
    def __init__(self, config_path: str = "sensitive_words.txt"):
        """
        初始化替换器
        Args:
            config_path: 敏感词配置文件路径
        """
        self.config_path = config_path
        self.sensitive_dict = self.load_sensitive_words()

    def load_sensitive_words(self) -> dict:
        """加载敏感词替换规则"""
        sensitive_dict = {}
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and '=' in line:
                        original, replacement = line.split('=')
                        sensitive_dict[original.strip()] = replacement.strip()
            return sensitive_dict
        except Exception as e:
            print(f"❌ 读取配置文件时出错: {e}")
            return {}

    def replace_text(self, text: str) -> Tuple[str, Dict[str, int]]:
        """
        处理单段文本
        Args:
            text: 要处理的文本
        Returns:
            处理后的文本和替换统计
        """
        replacements = {}
        processed_text = text
        for original, replacement in self.sensitive_dict.items():
            count = text.count(original)
            if count > 0:
                processed_text = processed_text.replace(original, replacement)
                replacements[original] = count
        return processed_text, replacements

    def replace_file(self, file_path: str) -> Optional[Dict[str, int]]:
        """
        处理单个文件
        Args:
            file_path: 文件路径
        Returns:
            替换统计信息
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            new_content, replacements = self.replace_text(content)
            
            if replacements:  # 只有在有替换时才写入文件
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
            
            return replacements
        except Exception as e:
            print(f"❌ 处理文件出错 {file_path}: {e}")
            return None

    def replace_folder(self, folder_path: str, file_pattern: str = "*.md") -> Dict[str, List[Tuple[str, Dict[str, int]]]]:
        """
        处理文件夹
        Args:
            folder_path: 文件夹路径
            file_pattern: 文件匹配模式
        Returns:
            处理报告
        """
        report = {
            "processed_files": [],
            "skipped_files": [],
            "error_files": []
        }

        files = glob.glob(os.path.join(folder_path, file_pattern))
        for file_path in files:
            file_name = os.path.basename(file_path)
            replacements = self.replace_file(file_path)
            
            if replacements is None:
                report["error_files"].append((file_name, {}))
            elif replacements:
                report["processed_files"].append((file_name, replacements))
            else:
                report["skipped_files"].append((file_name, {}))

        return report

    def print_report(self, report: Dict[str, List[Tuple[str, Dict[str, int]]]]):
        """打印处理报告"""
        print("\n" + "="*20 + " 替换报告 " + "="*20)
        
        total_files = len(report["processed_files"]) + len(report["skipped_files"]) + len(report["error_files"])
        print(f"\n📊 文件统计")
        print(f"- 总文件数: {total_files}")
        print(f"- 处理文件数: {len(report['processed_files'])}")
        print(f"- 跳过文件数: {len(report['skipped_files'])}")
        print(f"- 错误文件数: {len(report['error_files'])}")

        if report["processed_files"]:
            print("\n📑 文件详细替换记录:")
            for file_name, replacements in report["processed_files"]:
                print(f"\n文件: {file_name}")
                for original, count in replacements.items():
                    print(f"- {original} → {self.sensitive_dict[original]}: {count} 处")

        if report["error_files"]:
            print("\n❌ 处理失败的文件:")
            for file_name, _ in report["error_files"]:
                print(f"- {file_name}")

def main():
    """独立运行时的入口函数"""
    folder_path = r"C:\Users\works\Downloads\20250315"  # 实际使用时替换为你的文件夹路径
    config_path = "sensitive_words.txt"
    
    if not os.path.exists(folder_path):
        print(f"❌ 文件夹路径不存在: {folder_path}")
        return
    if not os.path.exists(config_path):
        print(f"❌ 配置文件不存在: {config_path}")
        return

    replacer = SensitiveWordReplacer(config_path)
    report = replacer.replace_folder(folder_path)
    replacer.print_report(report)

if __name__ == "__main__":
    main() 