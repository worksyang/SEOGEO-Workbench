#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🖼️ 图片转Markdown表格识别工具
-本模块开发目的：实现图片中表格内容的智能识别与Markdown格式转换

🤖 AI识别流程
-🔍 使用OpenAI视觉模型进行图片初步识别
-📊 自动判断表格复杂度（列数一致性、列数量）
-✨ 流式输出识别过程，实时显示进度
-📝 提取并格式化Markdown代码块内容

🧠 智能复杂度判断
-📏 检测表格列数是否一致
-📊 判断是否超过8列的大表格
-🎯 识别"该图片无任何信息，请删除"标记
-⚡ 简单表格直接采用AI结果，复杂表格启用OCR融合

🔧 OCR增强处理
-🖥️ 本地 GPU OCR（Paddle 等，见脚本内实现）
-🔄 AI 与本地 OCR 结果智能融合
-🎯 使用稳定主模型进行结果优化
-🔁 支持融合失败时的重试机制

📤 输出格式化
-📋 标准Markdown表格格式输出
-✅ 自动清理和优化表格结构
-🛡️ 错误处理与异常恢复机制
-📊 详细的处理状态反馈

🔗 系统集成特性
-⚙️ 支持自定义提示词配置
-🔄 与并发OCR服务模块协同工作
-📁 灵活的文件路径处理
-🛠️ 完整的错误追踪和日志记录
"""

import base64
import os
import json
import shutil
import re
import time
import traceback  # 添加traceback模块用于详细错误信息
from pathlib import Path
from typing import Optional
from openai import OpenAI
from paddleocr import TableRecognitionPipelineV2
from markitdown import MarkItDown
from prompts_config import SIMPLE_OCR_PROMPT, ENABLE_COMPLEX_OCR

# 历史脚本自用：两份 Markdown 融合提示（原 prompts_config.FUSION_PROMPT）
_LEGACY_FUSION_PROMPT = """
你的任务是接收两份Markdown表格，
请将两个表格，组装成一个最终表格。

核心哲学：结合你所看到的图片，和两个ai的输出结果，取长补短，重建并还原出最真实的表格结构。

请开始输出你最两张表格的侦探级分析过程：
过程可以自由发挥，目的只有一个，就是彻底重建并还原出最真实的表格结构。

请你最后输出最终的表格结果：
最终结果输出区域必须为```markdown\s*(.*?)\s*```的代码窗口类型输出，
因为最后我的代码会读取```markdown\s*(.*?)\s*```部分的所有内容。
若最终结果包含多张表格，请在表格之间用两个空行隔开。
"""


class PNG2MDConverter:
    """图片转Markdown表格识别器"""
    
    def __init__(self):
        self.openai_service = OpenAIService()
    
    def convert_image_to_markdown(self, image_path: str) -> str:
        """
        将图片转换为Markdown表格
        
        Args:
            image_path: 图片文件路径
            
        Returns:
            转换后的Markdown文本
        """
        if not os.path.exists(image_path):
            raise FileNotFoundError(f"❌ 文件不存在: {image_path}")
        
        try:
            print("\n正在用AI识别图片，请稍候...\n")
            
            # 第一步：AI完整识别（流式输出）
            ai_full_output = self.openai_service.image_to_markdown(image_path)
            print("\n" + "=" * 50)
            print("AI识别完成！正在处理结果...")
            
            # 第二步：提取markdown代码块内容
            ai_md = extract_markdown_from_codeblock(ai_full_output)
            
            print("\n===== AI初步识别结果（Markdown） =====\n")
            print(ai_md)
            print("\n====================================\n")
            
            # 第三步：判断是否复杂表格（只在AI完全输出完毕后进行）
            print("正在分析表格复杂度...")
            is_complex = is_complex_markdown(ai_md) if ENABLE_COMPLEX_OCR else False
            
            if not ENABLE_COMPLEX_OCR:
                print("🔧 复杂识别开关已关闭，跳过本地 GPU OCR 辅助")
            
            if is_complex:
                print("⚠️ 检测到复杂表格，自动调用本地GPU OCR辅助识别...")
                
                # 添加超时控制
                try:
                    # 设置最大尝试次数
                    max_retries = 3
                    retry_delay = 2  # 秒
                    
                    for attempt in range(max_retries):
                        try:
                            ocr_md = ocr_to_xlsx_to_md(image_path)
                            if ocr_md and not ocr_md.startswith("未识别到表格") and not ocr_md.startswith("## OCR识别失败"):
                                break
                            else:
                                if attempt < max_retries - 1:
                                    print(f"⚠️ OCR识别结果无效，尝试重试 ({attempt+1}/{max_retries})...")
                                    time.sleep(retry_delay)
                                else:
                                    print("⚠️ OCR识别失败，已达到最大重试次数，将使用AI结果")
                                    return ai_md
                        except Exception as e:
                            if attempt < max_retries - 1:
                                print(f"⚠️ OCR处理异常，尝试重试 ({attempt+1}/{max_retries}): {str(e)}")
                                time.sleep(retry_delay)
                            else:
                                print(f"⚠️ OCR处理失败，已达到最大重试次数: {str(e)}")
                                print("✅ 将使用AI识别结果作为备选")
                                return ai_md
                    
                    print("\n===== 本地GPU OCR转换结果（Markdown） =====\n")
                    print(ocr_md)
                    print("\n==================================\n")
                    
                    # 如果OCR结果为空或无效，使用AI结果
                    if not ocr_md or ocr_md.strip() == "" or ocr_md.startswith("未识别到表格") or ocr_md.startswith("## OCR识别失败"):
                        print("⚠️ OCR结果无效，使用AI识别结果")
                        return ai_md
                    
                    print("\n正在融合两份结果，请稍候...\n")
                    
                    # 融合结果也添加超时和重试
                    max_fusion_retries = 2
                    for fusion_attempt in range(max_fusion_retries):
                        try:
                            fusion_full_output = self.openai_service.fuse_markdown_and_json(ai_md, ocr_md)
                            
                            # 提取融合结果的markdown代码块
                            fusion_md = extract_markdown_from_codeblock(fusion_full_output)
                            
                            # 检查融合结果是否有效
                            if fusion_md and fusion_md.strip() != "":
                                print("\n===== 融合优化后的Markdown结果 =====\n")
                                print(fusion_md)
                                print("\n====================================\n")
                                return fusion_md
                            else:
                                if fusion_attempt < max_fusion_retries - 1:
                                    print(f"⚠️ 融合结果无效，尝试重试 ({fusion_attempt+1}/{max_fusion_retries})...")
                                    time.sleep(retry_delay)
                                else:
                                    print("⚠️ 融合失败，使用AI识别结果")
                                    return ai_md
                        except Exception as e:
                            if fusion_attempt < max_fusion_retries - 1:
                                print(f"⚠️ 融合处理异常，尝试重试 ({fusion_attempt+1}/{max_fusion_retries}): {str(e)}")
                                time.sleep(retry_delay)
                            else:
                                print(f"⚠️ 融合处理失败: {str(e)}")
                                print("✅ 将使用AI识别结果作为备选")
                                return ai_md
                
                except Exception as e:
                    print(f"❌ OCR或融合处理失败: {str(e)}")
                    print("✅ 将使用AI识别结果作为备选")
                    return ai_md
                    
            else:
                print("✅ 表格结构简单，直接采用AI识别结果。\n")
                return ai_md
                
        except Exception as e:
            error_detail = traceback.format_exc()
            print(f"❌ 识别失败: {str(e)}")
            print(f"错误详情: {error_detail}")
            # 返回一个错误信息的Markdown
            return f"## 识别失败\n\n处理图片时发生错误: {str(e)}"


class OpenAIService:
    def __init__(self):
        self.client = OpenAI(
            api_key=os.environ.get('OPENAI_API_KEY', ''),
            base_url="http://doubao.zwchat.cn/v1",
            timeout=60.0  # 设置默认超时时间为60秒
        )
        self.model = "gemini-2.5-flash-lite"
        self.max_retries = 3  # 最大重试次数
        self.retry_delay = 2  # 重试间隔（秒）
    
    def encode_image(self, image_path: str) -> str:
        """将图片编码为base64格式"""
        try:
            with open(image_path, "rb") as image_file:
                return base64.b64encode(image_file.read()).decode('utf-8')
        except Exception as e:
            print(f"❌ 图片编码失败: {str(e)}")
            raise
    
    def image_to_markdown(self, image_path: str, custom_prompt: Optional[str] = None) -> str:
        """
        将图片转换为Markdown格式
        
        Args:
            image_path: 图片文件路径
            custom_prompt: 自定义提示词
            
        Returns:
            转换后的Markdown文本
        """
        # 检查图片文件是否存在
        if not Path(image_path).exists():
            raise FileNotFoundError(f"图片文件不存在: {image_path}")
        
        # 编码图片
        base64_image = self.encode_image(image_path)
        
        # 使用配置文件中的默认提示词
        default_prompt = SIMPLE_OCR_PROMPT
        
        prompt = custom_prompt or default_prompt
        
        # 添加重试机制
        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": prompt
                                },
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": f"data:image/png;base64,{base64_image}"
                                    }
                                }
                            ]
                        }
                    ],
                    max_tokens=30000,  # 大幅提高token限制，避免输出被截断
                    stream=True,  # 启用流式输出
                    timeout=60  # 60秒超时
                )
                
                # 收集流式响应
                full_content = ""
                print("正在分析图片并生成Markdown...")
                print("=" * 50)
                
                # 设置流式响应超时
                start_time = time.time()
                timeout = 120  # 流式响应总超时时间（秒）
                last_chunk_time = start_time
                chunk_timeout = 30  # 单个chunk超时时间（秒）
                
                for chunk in response:
                    # 检查总超时
                    if time.time() - start_time > timeout:
                        print("\n⏱️ 总响应超时，返回已收到的内容")
                        break
                        
                    # 检查chunk超时
                    if time.time() - last_chunk_time > chunk_timeout:
                        print("\n⏱️ 响应块接收超时，返回已收到的内容")
                        break
                    
                    # 更新最后接收时间
                    last_chunk_time = time.time()
                    
                    # 检查chunk是否有choices且不为空
                    if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                        choice = chunk.choices[0]
                        # 检查是否有delta和content
                        if hasattr(choice, 'delta') and hasattr(choice.delta, 'content'):
                            content = choice.delta.content
                            if content is not None:
                                print(content, end='', flush=True)  # 实时输出
                                full_content += content
                
                print("\n" + "=" * 50)
                print("转换完成！")
                
                # 检查内容是否为空或太短
                if not full_content or len(full_content) < 10:
                    if attempt < self.max_retries - 1:
                        print(f"⚠️ 响应内容为空或太短，尝试重试 ({attempt+1}/{self.max_retries})...")
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        print("⚠️ 响应内容为空或太短，已达到最大重试次数")
                        return "## AI识别失败\n\n服务返回内容为空或太短，请稍后重试。"
                
                return full_content
                
            except Exception as e:
                if attempt < self.max_retries - 1:
                    print(f"⚠️ API请求失败，尝试重试 ({attempt+1}/{self.max_retries}): {str(e)}")
                    time.sleep(self.retry_delay)
                else:
                    print(f"❌ API请求失败，已达到最大重试次数: {str(e)}")
                    raise Exception(f"API请求失败: {str(e)}")
    
    def fuse_markdown_and_json(self, ai_markdown: str, ocr_markdown: str, custom_prompt: Optional[str] = None) -> str:
        """
        融合AI Markdown和OCR Markdown，输出最终Markdown
        """
        prompt = custom_prompt or _LEGACY_FUSION_PROMPT
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"AI初步识别的Markdown表格如下：\n{ai_markdown}\n\n本地OCR输出的Markdown表格如下：\n{ocr_markdown}"}
        ]
        
        # 添加重试机制
        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=30000,  # 大幅提高token限制，避免输出被截断
                    stream=True,  # 启用流式输出
                    timeout=60  # 60秒超时
                )
                
                # 收集流式响应
                full_content = ""
                print("正在融合两份结果...")
                print("=" * 50)
                
                # 设置流式响应超时
                start_time = time.time()
                timeout = 120  # 流式响应总超时时间（秒）
                last_chunk_time = start_time
                chunk_timeout = 30  # 单个chunk超时时间（秒）
                
                for chunk in response:
                    # 检查总超时
                    if time.time() - start_time > timeout:
                        print("\n⏱️ 总响应超时，返回已收到的内容")
                        break
                        
                    # 检查chunk超时
                    if time.time() - last_chunk_time > chunk_timeout:
                        print("\n⏱️ 响应块接收超时，返回已收到的内容")
                        break
                    
                    # 更新最后接收时间
                    last_chunk_time = time.time()
                    
                    # 检查chunk是否有choices且不为空
                    if hasattr(chunk, 'choices') and len(chunk.choices) > 0:
                        choice = chunk.choices[0]
                        # 检查是否有delta和content
                        if hasattr(choice, 'delta') and hasattr(choice.delta, 'content'):
                            content = choice.delta.content
                            if content is not None:
                                print(content, end='', flush=True)  # 实时输出
                                full_content += content
                
                print("\n" + "=" * 50)
                print("融合完成！")
                
                # 检查内容是否为空或太短
                if not full_content or len(full_content) < 10:
                    if attempt < self.max_retries - 1:
                        print(f"⚠️ 融合响应内容为空或太短，尝试重试 ({attempt+1}/{self.max_retries})...")
                        time.sleep(self.retry_delay)
                        continue
                    else:
                        print("⚠️ 融合响应内容为空或太短，已达到最大重试次数")
                        return "## 融合失败\n\n服务返回内容为空或太短，请稍后重试。"
                
                return full_content.strip()
                
            except Exception as e:
                if attempt < self.max_retries - 1:
                    print(f"⚠️ 融合请求失败，尝试重试 ({attempt+1}/{self.max_retries}): {str(e)}")
                    time.sleep(self.retry_delay)
                else:
                    print(f"❌ 融合请求失败，已达到最大重试次数: {str(e)}")
                    raise Exception(f"融合请求失败: {str(e)}")


def excel_to_markdown_string(excel_file):
    """将Excel转换为Markdown字符串"""
    try:
        md = MarkItDown()
        result = md.convert(excel_file)
        # 处理NaN和换行符
        content = result.text_content
        content = content.replace('NaN', '')  # 删除NaN
        content = content.replace('\\n', '<br>')  # 替换换行符
        # 处理Unnamed单元格
        content = re.sub(r'Unnamed: \d+', '', content)  # 替换所有Unnamed文本
        # 替换Sheet1为图表
        content = content.replace('## Sheet1', '## 图表')
        return content
    except Exception as e:
        raise Exception(f"转换失败: {str(e)}")


def is_ascii(s):
    try:
        s.encode('ascii')
        return True
    except UnicodeEncodeError:
        return False


def safe_image_path(image_path):
    """如路径含中文或特殊字符，自动复制到英文临时文件，返回新路径和是否为临时文件"""
    if not is_ascii(image_path):
        temp_path = './temp_image.png'
        shutil.copy(image_path, temp_path)
        return temp_path, True
    return image_path, False


def extract_markdown_from_codeblock(content: str) -> str:
    """从AI输出中提取最后一个markdown代码块内容"""
    # 尝试匹配所有 ```markdown 代码块
    markdown_pattern = r'```markdown\s*(.*?)\s*```'
    matches = re.findall(markdown_pattern, content, re.DOTALL)
    
    if matches:
        # 返回最后一个匹配的markdown代码块
        last_markdown = matches[-1].strip()
        print(f"📝 找到 {len(matches)} 个markdown代码块，使用最后一个（第 {len(matches)} 个）")
        return last_markdown
    
    # 如果没有找到markdown代码块，返回完整内容
    print("📝 未找到markdown代码块，使用完整内容")
    return content


def is_complex_markdown(md: str) -> bool:
    """判断是否为复杂表格：分别检查每张表格，只要有一张表格列数不一致且大于8列就算复杂"""
    print("🔍 开始分析表格复杂度...")
    
    # 按空行分割，找出所有可能的表格块
    sections = md.split('\n\n')
    
    table_count = 0
    for i, section in enumerate(sections):
        if '|' in section:  # 只分析包含表格的section
            table_count += 1
            print(f"\n📊 分析第 {table_count} 张表格:")
            
            if is_single_table_complex(section, table_count):
                print(f"🚨 第 {table_count} 张表格被判定为复杂表格！")
                return True
    
    print(f"\n✅ 共分析了 {table_count} 张表格，都是简单表格。")
    return False


def is_single_table_complex(table_text: str, table_num: int) -> bool:
    """判断单张表格是否复杂：列数不一致且大于8列"""
    # 确保只检查表格行（包含|的行）
    lines = [l.strip() for l in table_text.splitlines() if '|' in l and l.strip()]
    if not lines:
        print(f"   表格 {table_num}: 未找到表格行")
        return False
    
    print(f"   表格 {table_num}: 找到 {len(lines)} 行包含 '|' 的行")
    
    # 过滤掉分隔行（只包含|、-、:、空格的行）
    table_lines = []
    separator_lines = []
    for line in lines:
        # 跳过表格分隔行（如 |---|---|）
        if re.match(r'^[\|\-\:\s]+$', line):
            separator_lines.append(line)
            continue
        table_lines.append(line)
    
    print(f"   表格 {table_num}: 过滤掉 {len(separator_lines)} 行分隔行")
    print(f"   表格 {table_num}: 剩余 {len(table_lines)} 行数据行")
    
    if not table_lines:
        print(f"   表格 {table_num}: 没有有效的数据行")
        return False
    
    # 计算每行的列数
    col_counts = []
    for j, line in enumerate(table_lines):
        if line.count('|') > 1:
            col_count = line.count('|') - 1
            col_counts.append(col_count)
            print(f"   表格 {table_num} 第 {j+1} 行: {col_count} 列")
        else:
            print(f"   表格 {table_num} 第 {j+1} 行: 无效行（'|' 数量 <= 1）")
    
    if not col_counts:
        print(f"   表格 {table_num}: 没有有效的列数据")
        return False
    
    # 统计分析
    unique_col_counts = set(col_counts)
    max_cols = max(col_counts)
    min_cols = min(col_counts)
    
    print(f"   表格 {table_num}: 列数统计 -> 最小: {min_cols}, 最大: {max_cols}")
    print(f"   表格 {table_num}: 不同列数: {sorted(unique_col_counts)}")
    
    # 检查列数是否不一致
    if len(unique_col_counts) > 1:
        print(f"   表格 {table_num}: ❌ 列数不一致！")
        # 只有列数不一致时，才检查是否大于8列
        if max_cols >= 8:
            print(f"   表格 {table_num}: ❌ 最大列数 {max_cols} >= 8！")
            return True
        else:
            print(f"   表格 {table_num}: ✅ 最大列数 {max_cols} < 8，不算复杂")
    else:
        print(f"   表格 {table_num}: ✅ 列数一致（{max_cols} 列）")
    
    return False


def ocr_to_xlsx_to_md(image_path: str) -> str:
    """用PaddleOCR识别表格，输出XLSX，然后转换为MD格式"""
    real_path, is_temp = safe_image_path(image_path)
    temp_files = []  # 跟踪临时文件
    
    try:
        # 设置超时时间 - 使用平台兼容的方式
        import platform
        
        # 定义超时处理类
        class OCRTimeoutManager:
            def __init__(self, timeout_seconds):
                self.timeout_seconds = timeout_seconds
                self.is_windows = platform.system() == "Windows"
                self.start_time = None
                
            def __enter__(self):
                self.start_time = time.time()
                if not self.is_windows:
                    # 在非Windows系统上使用signal
                    import signal
                    def timeout_handler(signum, frame):
                        raise TimeoutError("OCR处理超时")
                    signal.signal(signal.SIGALRM, timeout_handler)
                    signal.alarm(self.timeout_seconds)
                return self
                
            def __exit__(self, exc_type, exc_val, exc_tb):
                if not self.is_windows:
                    # 在非Windows系统上取消alarm
                    import signal
                    signal.alarm(0)
                
            def check_timeout(self):
                # 在Windows上手动检查超时
                if self.is_windows and (time.time() - self.start_time) > self.timeout_seconds:
                    raise TimeoutError("OCR处理超时")
        
        # 使用超时管理器
        with OCRTimeoutManager(timeout_seconds=120) as timeout_mgr:
            pipeline = TableRecognitionPipelineV2(device="gpu")
            output = pipeline.predict(real_path)
            timeout_mgr.check_timeout()  # Windows上检查是否超时
        
        if not output:
            return "未识别到表格"
        
        # 保存为XLSX文件
        output_dir = "./output"
        os.makedirs(output_dir, exist_ok=True)
        
        # 使用UUID生成唯一文件名，避免冲突
        import uuid
        temp_id = uuid.uuid4().hex
        xlsx_path = f"{output_dir}/temp_{temp_id}.xlsx"
        temp_files.append(xlsx_path)
        
        output[0].save_to_xlsx(xlsx_path)
        
        # 将XLSX转换为MD
        md_content = excel_to_markdown_string(xlsx_path)
        return md_content
        
    except TimeoutError:
        print("⚠️ OCR处理超时，请检查GPU状态或尝试使用CPU模式")
        return "## OCR识别失败\n\n处理超时，请稍后重试"
    except Exception as e:
        error_detail = traceback.format_exc()
        print(f"❌ OCR处理失败: {str(e)}")
        print(f"错误详情: {error_detail}")
        return f"## OCR识别失败\n\n- 错误信息：{str(e)}"
    finally:
        # 清理临时文件
        if is_temp and os.path.exists(real_path):
            try:
                os.remove(real_path)
            except Exception as e:
                print(f"⚠️ 清理临时图片失败: {str(e)}")
        
        # 清理其他临时文件
        for temp_file in temp_files:
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                    print(f"🧹 临时文件已清理: {os.path.basename(temp_file)}")
            except Exception as e:
                print(f"⚠️ 清理临时文件失败 {os.path.basename(temp_file)}: {str(e)}")


def main():
    """主函数：直接运行智能模式"""
    print("🎯 图片转Markdown表格识别工具")
    print("请输入图片路径（支持绝对路径或相对路径），直接回车退出：")
    
    converter = PNG2MDConverter()
    
    while True:
        image_path = input("\n🖼️ 请输入图片路径: ").strip().strip('"')
        if not image_path:
            print("👋 已退出。")
            break
        
        try:
            result = converter.convert_image_to_markdown(image_path)
            # 这里可以进一步处理结果，比如保存到文件等
        except Exception as e:
            print(f"❌ 处理失败: {e}")


if __name__ == "__main__":
    main() 