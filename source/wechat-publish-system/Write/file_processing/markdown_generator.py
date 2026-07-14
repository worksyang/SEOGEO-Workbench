"""
# 📝Markdown智能生成器
1️⃣ 模板合成 └─ 🚩 动态加载头尾模板统一格式
2️⃣ 标题规范 └─ 🏷️ 自动提取与安全截断标题
3️⃣ 正文识别 └─ ✂️ 准确抽取主内容代码块
4️⃣ 内容清洗 └─ 🗑️ 去除杂质优化图片链接格式
5️⃣ 文件输出 └─ 💾 统计保存为标准Markdown文件
"""
import os
import re
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from PIL import Image
from io import BytesIO
import requests
import importlib.util
import sys

class MarkdownGenerator:
    def __init__(self, headfoot_file_path: str = None):
        """
        初始化Markdown生成器
        
        Args:
            headfoot_file_path: headfoot文件路径，如果为None则使用默认的headfoot.py
        """
        self.headfoot_file_path = headfoot_file_path or os.path.join('headfoot', 'headfoot.py')
        self._load_headfoot_content()
        
    def _load_headfoot_content(self):
        """动态加载headfoot文件内容"""
        try:
            # 动态导入headfoot模块
            spec = importlib.util.spec_from_file_location("headfoot_module", self.headfoot_file_path)
            headfoot_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(headfoot_module)
            
            # 获取头部和底部内容
            self.header_content = getattr(headfoot_module, 'header_content', '')
            self.footer_content = getattr(headfoot_module, 'footer_content', '')
            
            print(f"✅ 已加载headfoot文件: {os.path.basename(self.headfoot_file_path)}")
            
        except Exception as e:
            print(f"❌ 加载headfoot文件失败: {e}")
            # 使用空内容作为备选
            self.header_content = ''
            self.footer_content = ''
    
    def truncate_title(self, title: str, max_length: int = 58) -> str:
        """
        截断超过指定长度的标题
        
        Args:
            title: 原始标题
            max_length: 最大允许的中文字符数
            
        Returns:
            str: 截断后的标题
        """
        # 计算中文字符数
        chinese_chars = sum(1 for char in title if '\u4e00' <= char <= '\u9fff')
        
        # 如果中文字符数超过限制，需要截断
        if chinese_chars > max_length:
            # 计算需要截断的字符数
            chars_to_keep = 0
            chinese_count = 0
            
            # 逐个字符检查，直到达到最大中文字符数
            for i, char in enumerate(title):
                if '\u4e00' <= char <= '\u9fff':
                    chinese_count += 1
                    if chinese_count > max_length:
                        break
                chars_to_keep = i
            
            # 截断标题并添加省略号
            truncated_title = title[:chars_to_keep+1] + "..."
            print(f"⚠️ 标题超过{max_length}个中文字符，已截断:")
            print(f"   原标题: {title}")
            print(f"   截断后: {truncated_title}")
            return truncated_title
        
        return title
        
    def clean_title(self, text: str) -> str:
        """
        清理标题，只保留中文、英文、数字和常用标点符号
        
        Args:
            text: 输入文本
            
        Returns:
            str: 清理后的文本
        """
        # 定义允许的标点符号（中英文常用标点）
        allowed_punctuation = (
            '，。！？；：、''""（）《》【】'  # 中文标点
            ',.!?;:()\'"[]{}'  # 英文标点
            '_-%'  # 下划线、连字符和百分号
        )
        
        # 保留中文、英文、数字和允许的标点
        cleaned = ''
        for char in text:
            if (
                '\u4e00' <= char <= '\u9fff'  # 中文字符
                or '\u0030' <= char <= '\u0039'  # 数字
                or '\u0041' <= char <= '\u005a'  # 大写英文
                or '\u0061' <= char <= '\u007a'  # 小写英文
                or char in allowed_punctuation  # 允许的标点
                or char == ' '  # 空格
            ):
                cleaned += char
                
        return cleaned.strip()
        
    def _make_safe_filename(self, text: str) -> str:
        """
        将标题转换为安全的文件名，使用优雅的字符替换策略
        
        Args:
            text: 输入文本
            
        Returns:
            str: 安全的文件名
        """
        # 第一步：定义优雅替换规则
        replacement_rules = {
            ':': '：',    # 英文冒号 -> 中文冒号
            '?': '？',    # 英文问号 -> 中文问号
            '"': '"',     # 英文双引号 -> 中文双引号
            '<': '《',    # 小于号 -> 中文书名号
            '>': '》',    # 大于号 -> 中文书名号
            '/': '／',    # 斜杠 -> 全角斜杠
            '\\': '＼',   # 反斜杠 -> 全角反斜杠
            '*': '＊',    # 星号 -> 全角星号
            '|': '｜',    # 竖线 -> 全角竖线
        }
        
        # 应用优雅替换规则
        safe_text = text
        for original, replacement in replacement_rules.items():
            safe_text = safe_text.replace(original, replacement)
        
        # 第二步：兜底模式 - 移除任何剩余的Windows不允许字符
        # 这里使用更严格的检查，确保文件名完全安全
        import re
        # 移除所有Windows不允许的字符（包括控制字符）
        safe_text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', safe_text)
        
        # 第三步：清理连续空格和首尾空格
        safe_text = re.sub(r'\s+', ' ', safe_text).strip()
        
        # 第四步：确保文件名不为空且不以点开头
        if not safe_text or safe_text.startswith('.'):
            safe_text = "article"
            
        return safe_text
        
    def save_markdown_with_template(self, content: str, h1_title: str, output_dir: str) -> str:
        """
        使用模板保存Markdown文件
        
        Args:
            content: Markdown内容
            h1_title: 文章标题
            output_dir: 输出目录
            
        Returns:
            str: 保存的文件路径
        """
        # 提取markdown内容
        markdown_content = content
        
        # 查找markdown区域
        start_marker = content.find("```markdown")
        if start_marker != -1:
            # 找到了markdown开始标记
            content_start = start_marker + len("```markdown")
            end_marker = content.rfind("```")
            
            if end_marker > content_start:
                # 有结束标记，取中间内容（不保留结束标记）
                markdown_content = content[content_start:end_marker].strip()
            else:
                # 没有结束标记，取后面所有内容
                markdown_content = content[content_start:].strip()
        else:
            # 找不到markdown标记，从第一个#开始读取
            hash_pos = content.find("#")
            if hash_pos != -1:
                markdown_content = content[hash_pos:].strip()
        
        # 清理所有markdown标记和```符号
        markdown_content = markdown_content.replace("markdown", "")
        markdown_content = markdown_content.replace("```", "")
        
        # 去除<think>标签内容
        markdown_content = re.sub(r'<think>.*?</think>\s*', '', markdown_content, flags=re.DOTALL)
        
        # 去除Thinking...文本
        markdown_content = re.sub(r'^Thinking\.\.\. \(\d+s elapsed\)\s*', '', markdown_content, flags=re.MULTILINE)
        
        # 提取H1标题
        h1_pattern = r'^# (.*?)$'
        h1_match = re.search(h1_pattern, markdown_content, re.MULTILINE)
        if h1_match:
            h1_title = h1_match.group(1).strip()
            markdown_content = re.sub(h1_pattern, '', markdown_content, flags=re.MULTILINE).strip()
        
        if not h1_title:
            h1_title = "无标题"
        
        # 截断超长标题
        h1_title = self.truncate_title(h1_title)
            
        # 生成文件名
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        # 先清理标题，只保留中文、英文、数字和常用标点
        clean_title = self.clean_title(h1_title)
        # 再处理Windows不允许的字符，使用更优雅的替换策略
        safe_title = self._make_safe_filename(clean_title)
        # 如果清理后为空，使用默认标题
        if not safe_title.strip():
            safe_title = "article"
        filename = f"{safe_title}_{timestamp}.md"
        
        # 清理内容
        final_content = self.clean_invalid_images(markdown_content)
        final_content = final_content.strip('`')
        final_content = re.sub(r'\n{3,}', '\n\n', final_content)
        
        # 添加头部和尾部（使用实例变量）
        formatted_content = f"{self.header_content}\n\n{final_content}\n\n{self.footer_content}"
        
        # 保存文件
        os.makedirs(output_dir, exist_ok=True)
        full_path = os.path.join(output_dir, filename)
        
        with open(full_path, 'w', encoding='utf-8') as f:
            f.write(formatted_content)
            
        print(f"✅ 已保存: {filename}")
        return full_path
        
    def check_article_length(self, md_path: str, min_chars: int = 800) -> bool:
        """
        检查文章长度是否达标
        
        Args:
            md_path: Markdown文件路径
            min_chars: 最小字符数要求
            
        Returns:
            bool: 是否达到长度要求
        """
        try:
            # 使用实例变量而不是导入
            template_content = self.header_content + self.footer_content
            
            # 统计模板字数（包含中英文、数字）
            template_chars = sum(1 for char in template_content if (
                '\u4e00' <= char <= '\u9fff'  # 中文字符
                or '\u0041' <= char <= '\u005A'  # 大写英文
                or '\u0061' <= char <= '\u007A'  # 小写英文
                or '\u0030' <= char <= '\u0039'  # 数字
            ))
            
            with open(md_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 统计总字数（包含中英文、数字）    
            total_chars = sum(1 for char in content if (
                '\u4e00' <= char <= '\u9fff'  # 中文字符
                or '\u0041' <= char <= '\u005A'  # 大写英文
                or '\u0061' <= char <= '\u007A'  # 小写英文
                or '\u0030' <= char <= '\u0039'  # 数字
            ))
            
            # 计算正文字数
            content_chars = total_chars - template_chars
            
            print(f"\n字数统计:")
            #print(f"- 模板字数: {template_chars} 字")
            #print(f"- 总字数: {total_chars} 字")
            print(f"- 正文字数: {content_chars} 字")
            print(f"- 最小要求: {min_chars} 字")
            
            if content_chars < min_chars:
                print(f"❌ 正文字数({content_chars})未达到最小要求({min_chars})")
                return False
                
            print(f"✅ 正文字数达标")
            return True
            
        except Exception as e:
            print(f"检查文章字数时出错: {e}")
            return False
            
    def is_image_url_valid(self, url: str) -> bool:
        """检查图片URL是否有效"""
        if 'qpic.cn' not in url:
            print(f"非 qpic.cn 域名图片链接，已标记为无效: {url}")
            return False
            
        try:
            response = requests.get(url, timeout=5)
            response.raise_for_status()
            image = Image.open(BytesIO(response.content))
            image.verify()
            return True
        except Exception as e:
            print(f"图片打开失败: {e}")
            return False
            
    def clean_invalid_images(self, markdown_content: str) -> str:
        """清理无效的图片链接"""
        # 统计信息
        total_images = 0
        valid_images = 0
        removed_links = 0
        removed_toc_links = 0
        
        lines = markdown_content.splitlines()
        cleaned_lines = []
        last_line_empty = False
        
        for line in lines:
            current_line = line.rstrip()
            
            if "![" in current_line and "](" in current_line:
                total_images += 1
                matches = re.finditer(r'!\[(.*?)\]\((.*?)\)', current_line)
                valid_line = current_line
                
                for match in matches:
                    url = match.group(2)
                    if self.is_image_url_valid(url):
                        valid_images += 1
                    else:
                        print(f"无效图片链接已移除: {url}")
                        valid_line = valid_line.replace(match.group(0), '')
                
                if valid_line.strip() or not last_line_empty:
                    cleaned_lines.append(valid_line)
                    last_line_empty = not valid_line.strip()
            else:
                line_cleaned = current_line
                
                # 处理目录链接
                toc_matches = re.finditer(r'\[(.*?)\]\(#.*?\)', line_cleaned)
                for match in toc_matches:
                    full_match = match.group(0)
                    text_only = match.group(1)
                    line_cleaned = line_cleaned.replace(full_match, text_only)
                    if full_match != text_only:
                        removed_toc_links += 1
                
                # 处理其他链接
                line_cleaned = re.sub(r'(?<!!)\[(.*?)\]\(((?!#).*?)\)', r'\1', line_cleaned)
                line_cleaned = re.sub(r'<https?://[^\s>]*>', '', line_cleaned)
                line_cleaned = re.sub(r'https?://[^\s<]*', '', line_cleaned)
                
                if line_cleaned.strip() or not last_line_empty:
                    cleaned_lines.append(line_cleaned)
                    last_line_empty = not line_cleaned.strip()
        
        if cleaned_lines and cleaned_lines[-1].strip():
            cleaned_lines.append('')
            
        print(f"\n链接清理统计:")
        print(f"- 总图片数: {total_images}")
        print(f"- 有效图片数: {valid_images}")
        print(f"- 移除的非图片链接数: {removed_links}")
        print(f"- 移除的目录链接数: {removed_toc_links}")
        
        return '\n'.join(cleaned_lines) 