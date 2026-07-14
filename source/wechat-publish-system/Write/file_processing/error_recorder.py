"""
# 🐞错误日志自动管理器
1️⃣ 目录保障  
   └─ 📂 检查/创建主日志文件夹及日期子目录，自动降级至本地目录防止写入失败  
2️⃣ 错误收集  
   ├─ 📝 捕获异常/上下文详细信息，独立日志文件存储  
   └─ 🕒 日志以时间命名，避免覆盖，便于溯源  
3️⃣ 用途  
   └─ ⚡ 支持自动化批量处理、AI任务等场景异常追踪与归档  
"""
import os
import time
import traceback
from datetime import datetime
from typing import Dict, Any, Optional

class ErrorRecorder:
    def __init__(self, base_error_dir: str = "C:/MD/error_MD"):
        """
        初始化错误记录器
        
        Args:
            base_error_dir: 错误文件保存的基础目录
        """
        self.base_error_dir = base_error_dir
        self.today = datetime.now().strftime('%Y%m%d')
        self.error_dir = os.path.join(self.base_error_dir, self.today)
        self._ensure_error_dir()
        
    def _ensure_error_dir(self) -> None:
        """
        确保错误日志目录存在，如果不存在则创建
        同时检查目录的写入权限
        """
        try:
            # 检查基础目录
            if not os.path.exists(self.base_error_dir):
                print(f"\n创建基础错误日志目录: {self.base_error_dir}")
                os.makedirs(self.base_error_dir)
            
            # 检查日期子目录
            if not os.path.exists(self.error_dir):
                print(f"创建今日错误日志目录: {self.error_dir}")
                os.makedirs(self.error_dir)
                
            # 测试写入权限
            test_file = os.path.join(self.error_dir, '.test_write')
            try:
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
            except Exception as e:
                raise PermissionError(f"错误日志目录没有写入权限: {str(e)}")
                
        except Exception as e:
            print(f"\n❌ 创建错误日志目录失败: {str(e)}")
            print("将尝试在当前目录创建 error_logs 文件夹作为备用")
            
            # 使用当前目录作为备用
            self.base_error_dir = os.path.join(os.getcwd(), 'error_logs')
            self.error_dir = os.path.join(self.base_error_dir, self.today)
            
            try:
                os.makedirs(self.error_dir, exist_ok=True)
                print(f"✅ 已创建备用错误日志目录: {self.error_dir}")
            except Exception as backup_error:
                print(f"❌ 创建备用目录也失败了: {str(backup_error)}")
                raise
        
    def format_error_content(self, 
                           error_info: Dict[str, Any], 
                           prompt_template: str,
                           original_content: str,
                           partial_content: Optional[str] = None) -> str:
        """
        格式化错误信息为Markdown格式
        
        Args:
            error_info: 错误信息字典
            prompt_template: 使用的提示词模板
            original_content: 原始文章内容
            partial_content: 部分生成的内容（如果有）
            
        Returns:
            str: 格式化后的Markdown内容
        """
        timestamp = datetime.fromtimestamp(error_info['timestamp']).strftime('%Y-%m-%d %H:%M:%S')
        
        content = [
            "# 文章生成失败记录",
            "",
            "## 基本信息",
            f"- 原始文件：{error_info['original_file']}",
            f"- 目标标题：{error_info['title']}",
            f"- 失败时间：{timestamp}",
            f"- 使用模型：{error_info['model']}",
            "",
            "## 错误信息",
            f"- 错误类型：{error_info['error_type']}",
            f"- 错误消息：{error_info['error_message']}",
        ]
        
        # 添加详细错误信息（如果有）
        if error_info.get('error_detail'):
            content.extend([
                "",
                "### 详细错误信息",
                "```",
                error_info['error_detail'],
                "```"
            ])
        
        # 添加提示词和原文内容
        content.extend([
            "",
            "## 生成内容",
            "### 使用的提示词",
            "```",
            prompt_template,
            "```",
            "",
            "### 原文内容",
            "```",
            original_content,
            "```"
        ])
        
        # 添加部分生成的内容（如果有）
        if partial_content:
            content.extend([
                "",
                "### 已生成内容",
                "```",
                partial_content,
                "```"
            ])
            
        return "\n".join(content)
        
    def save_error_record(self, 
                         error_info: Dict[str, Any],
                         prompt_template: str,
                         original_content: str,
                         partial_content: Optional[str] = None) -> str:
        """
        保存错误记录
        
        Args:
            error_info: 错误信息字典
            prompt_template: 使用的提示词模板
            original_content: 原始文章内容
            partial_content: 部分生成的内容（如果有）
            
        Returns:
            str: 保存的文件路径
        """
        # 生成文件名
        timestamp = datetime.fromtimestamp(error_info['timestamp']).strftime('%Y%m%d_%H%M%S')
        safe_title = "".join(x for x in error_info['title'] if x.isalnum() or x in "- _")[:50]
        filename = f"{safe_title}_{error_info['error_type']}_{timestamp}.md"
        filepath = os.path.join(self.error_dir, filename)
        
        # 格式化内容
        content = self.format_error_content(
            error_info,
            prompt_template,
            original_content,
            partial_content
        )
        
        # 保存文件
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
            
        return filepath
        
    def record_error(self,
                    original_file: str,
                    title: str,
                    model: str,
                    error: Exception,
                    prompt_template: str,
                    original_content: str,
                    partial_content: Optional[str] = None) -> str:
        """
        记录错误信息
        
        Args:
            original_file: 原始文件路径
            title: 文章标题
            model: 使用的模型
            error: 异常对象
            prompt_template: 使用的提示词模板
            original_content: 原始文章内容
            partial_content: 部分生成的内容（如果有）
            
        Returns:
            str: 保存的错误记录文件路径
        """
        error_info = {
            'original_file': original_file,
            'title': title,
            'model': model,
            'timestamp': time.time(),
            'error_type': type(error).__name__,
            'error_message': str(error),
            'error_detail': getattr(error, '__traceback__', None) and f"{type(error).__name__}: {str(error)}\n{''.join(traceback.format_tb(error.__traceback__))}" or None
        }
        
        return self.save_error_record(
            error_info,
            prompt_template,
            original_content,
            partial_content
        ) 