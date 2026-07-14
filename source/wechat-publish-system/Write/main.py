"""
主程序入口
"""
import os
import sys
import time
import random
from datetime import datetime
from typing import Dict, Any, List, Optional
import concurrent.futures
import asyncio
import importlib.util

from ai_services import get_ai_config
from ai_services.openaiapi import OpenAIAPIService
from file_processing.markdown_generator import MarkdownGenerator
from file_processing.document_processor import DocumentProcessor
from task_management.concurrent_executor import ConcurrentExecutor
from file_processing.error_recorder import ErrorRecorder
from file_processing import (
    SensitiveWordReplacer,
    clean_single_markdown_file,
)
from ai_services.prompt_logger import PromptLogger

class ContentManager:
    def __init__(self, base_folder: str, prompt_file_path: str = None, headfoot_file_path: str = None):
        """
        初始化内容管理器
        
        Args:
            base_folder: 基础文件夹路径
            prompt_file_path: 提示词文件路径
            headfoot_file_path: headfoot文件路径
        """
        self.base_folder = base_folder
        self.prompt_file_path = prompt_file_path
        self.headfoot_file_path = headfoot_file_path
        self.total_articles = 0
        self.completed_articles = 0
        self.success_articles = 0
        self.failed_articles = 0
        self.current_round = 1
        
        # 获取系统配置（保留以备将来扩展）
        system_config = get_ai_config('system')
        
        # 初始化文档处理器（使用统一的DocumentProcessor）
        self.document_processor = DocumentProcessor(base_folder)
        # 智能回退模式：不再强制退出，而是使用文件名作为标题
        missing_txt_files = self.document_processor.get_missing_txt_files()
        if missing_txt_files:
            print(f"\n✅ 智能回退模式已启用:")
            print(f"   - 有txt文件的MD: {len(self.document_processor.md_files_with_txt)} 个 (使用txt中的标题)")
            print(f"   - 无txt文件的MD: {len(missing_txt_files)} 个 (使用文件名作为标题)")
            print(f"   - 总可用文件: {len(self.document_processor.all_available_files)} 个")
        # 初始化AI服务
        self._init_ai_services()
        
        # 初始化文件处理（传递headfoot文件路径）
        self.markdown_generator = MarkdownGenerator(headfoot_file_path=self.headfoot_file_path)
        
        # 初始化错误记录器
        self.error_recorder = ErrorRecorder()
        
        # 初始化并发配置
        self.use_concurrent = False
        self.concurrent_tasks = 10  # 使用固定默认值，将被openaiapi_config覆盖
        self.concurrent_executor = None
        
        # 初始化统计信息
        self.total_rounds = 0
        
        # 初始化其他属性
        self.model_choice = None
        self.request_interval = 0.1
        self.last_request_time = 0
        self.failed_articles_info = []
        # 新增：统计因敏感关键词作废的文章数
        self.invalid_keyword_articles = 0
        
        # 新增：失败原因统计
        self.failure_stats = {
            'prompt_transmission_error': 0,    # Prompt传输问题
            'sensitive_keyword_filter': 0,     # 敏感关键词过滤
            'file_read_error': 0,             # 文件读取错误
            'ai_service_error': 0,            # AI服务错误
            'task_timeout': 0,                # 任务超时
            'other_exceptions': 0             # 其他异常
        }
        
        # 设置输出目录
        self.output_folder = os.path.join('C:\\', 'ai_output', '2025ALL')
        os.makedirs(self.output_folder, exist_ok=True)

        # 初始化 PromptLogger
        self.prompt_logger = PromptLogger(log_directory="C:\\txt")

        # OpenAIAPI (延迟初始化)
        self.openaiapi_service = None

    def _init_ai_services(self):
        """初始化AI服务（仅 OpenAI 兼容平台）"""
        # OpenAI API - 使用独立配置文件
        try:
            self.openaiapi_config = OpenAIAPIService.load_config()
            if self.openaiapi_config and self.openaiapi_config.get('concurrent', False):
                self.use_concurrent = True
                self.concurrent_tasks = self.openaiapi_config.get('max_concurrent_tasks', 3)
        except Exception as e:
            print(f"⚠️ OpenAI API配置加载失败: {e}")
            self.openaiapi_config = None
            self.openaiapi_service = None
                
    def init_openaiapi_client(self):
        """初始化OpenAI API客户端"""
        if self.openaiapi_service is None:
            self.openaiapi_service = OpenAIAPIService(self.openaiapi_config)
            # OpenAI API 根据配置决定是否使用并发
            self.use_concurrent = self.openaiapi_config.get('concurrent', False)
            if self.use_concurrent:
                self.concurrent_tasks = self.openaiapi_config.get('max_concurrent_tasks', 3)
        
    def ai_rewrite(self, content: str, title: str) -> Optional[str]:
        """
        使用AI服务改写文章
        
        Args:
            content: 原文内容
            title: 新标题
            
        Returns:
            Optional[str]: 改写后的内容
        """
        # 读取prompt模板
        prompt_file_path = self.prompt_file_path
        try:
            with open(prompt_file_path, 'r', encoding='utf-8') as f:
                prompt_template = f.read()
        except Exception as e:
            print(f"读取prompt模板失败: {e}")
            return None
            
        # 构建完整prompt
        full_prompt = f"{prompt_template.replace('{title}', title)}\n\n原文内容:\n{content}"

        # --- >>> 新增：调用 PromptLogger 保存提示词 <<< ---
        if self.prompt_logger:
            try:
                saved_log_path = self.prompt_logger.log_prompt(task_name=title, prompt_content=full_prompt)
                if saved_log_path:
                    # 为了避免控制台过于杂乱，可以选择只在特定调试模式下打印这个，或者完全不打印
                    # print(f"📝 提示词已记录到: {saved_log_path}") 
                    pass # 暂时不打印成功信息，减少干扰
                else:
                    print(f"⚠️ 无法记录提示词到文件 (任务: {title})")
            except Exception as log_e:
                print(f"⚠️ 记录提示词时发生意外错误 (任务: {title}): {log_e}")
        # --- <<< 结束新增代码 >>> ---
        
        # 添加调试信息
        print(f"\n📝 处理文章: {title}")
        print(f"📊 原文长度: {len(content)} 字符")
        print(f"📊 完整prompt长度: {len(full_prompt)} 字符")
        print(f"📊 原文前100字符: {content[:100]}...")
        
        try:
            if self.model_choice == "openaiapi":
                if not self.openaiapi_service:
                    self.init_openaiapi_client()
                return self.openaiapi_service.generate_text(full_prompt)
                
            else:
                raise ValueError(f"未知的模型选择: {self.model_choice}")
                
        except Exception as e:
            print(f"AI改写失败: {e}")
            return None
            
    def process_single_article(self, content: Dict[str, Any]) -> tuple:
        """
        处理单篇文章。
        此方法被设计为线程安全的，它不直接修改类的共享状态，
        而是返回一个包含处理结果的元组。
        返回:
            - ('success', {'saved_path': str, 'content': dict})
            - ('failure', {'type': str, 'error': str, 'content': dict})
        """
        try:
            # 首先读取文件内容
            try:
                with open(content['md_path'], 'r', encoding='utf-8') as f:
                    file_content = f.read()
                content['content'] = file_content  # 添加content键
            except Exception as e:
                error_msg = f"读取文件失败: {str(e)}"
                return 'failure', {'type': 'file_read_error', 'error': error_msg, 'content': content}

            # 1. AI生成文章
            gen_status, gen_data = self._generate_article(content)
            if gen_status == 'failure':
                # 将content信息附加到失败结果中
                gen_data['content'] = content
                return 'failure', gen_data
            
            saved_path = gen_data
                
            # 2. 清理Markdown标记
            if not clean_single_markdown_file(saved_path):
                error_msg = "Markdown清理失败"
                return 'failure', {'type': 'other_exceptions', 'error': error_msg, 'content': content}
                
            return 'success', {'saved_path': saved_path, 'content': content}
            
        except Exception as e:
            error_msg = f"处理文章时发生未知异常: {e}"
            return 'failure', {'type': 'other_exceptions', 'error': error_msg, 'content': content}
            
    def _generate_article(self, content: Dict[str, Any]) -> tuple:
        """
        生成文章的核心逻辑。不直接修改共享状态。
        返回:
            - ('success', saved_path)
            - ('failure', {'type': str, 'error': str})
        """
        try:
            # 原有的AI生成逻辑
            response_text = self.ai_rewrite(content['content'], content['title'])
            if not response_text:
                return 'failure', {'type': 'ai_service_error', 'error': 'AI服务未能生成有效文本'}
            
            # 检查AI是否要求提供文章（这表明可能没有正确接收到原文）
            request_article_keywords = ['请给我', '请提供', '请发送', '我需要看到文章', '请分享']
            lower_response = response_text.lower()
            triggered_request = next((kw for kw in request_article_keywords if kw in lower_response), None)
            if triggered_request:
                error_msg = f"AI要求提供文章（{triggered_request}），可能是prompt传输问题"
                return 'failure', {'type': 'prompt_transmission_error', 'error': error_msg}
            
            # 检查AI输出内容是否包含敏感关键词
            keywords = ['mermaid', 'python', '插入']
            lower_text = response_text.lower()
            triggered_keyword = next((kw for kw in keywords if kw in lower_text), None)
            if triggered_keyword:
                error_msg = f"包含敏感关键词（{triggered_keyword}）作废"
                return 'failure', {'type': 'sensitive_keyword_filter', 'error': error_msg}
                
            # 检查是否使用o1-mini-prompt.txt，如果是则跳过敏感词替换
            is_o1_mini_prompt = self.prompt_file_path and 'o1-mini-prompt.txt' in self.prompt_file_path
            
            if is_o1_mini_prompt:
                print("🔄 检测到使用o1-mini-prompt.txt，跳过敏感词替换（压缩模式）")
                processed_text = response_text
            else:
                # 在保存之前进行敏感词替换
                # 构建敏感词文件的绝对路径
                write_dir = os.path.dirname(os.path.abspath(__file__))
                sensitive_words_path = os.path.join(write_dir, 'sensitive_words.txt')
                replacer = SensitiveWordReplacer(config_path=sensitive_words_path)
                processed_text, replacements = replacer.replace_text(response_text)
                
                # 如果有替换发生，打印替换信息
                if replacements:
                    print("\n📝 敏感词替换情况:")
                    for original, count in replacements.items():
                        print(f"   - {original} → {replacer.sensitive_dict[original]}: {count} 处")
                
            # 保存处理后的内容
            saved_path = self.markdown_generator.save_markdown_with_template(
                processed_text,
                content['title'],
                self.output_folder
            )
            
            return 'success', saved_path
            
        except Exception as e:
            # 判断是否为AI服务错误
            error_str = str(e)
            failure_type = 'other_exceptions'
            if any(keyword in error_str.lower() for keyword in ['api', 'network', 'timeout', 'connection', 'http']):
                failure_type = 'ai_service_error'
            
            return 'failure', {'type': failure_type, 'error': error_str}
            
    def process_batch(self, contents):
        """处理一批文章，根据配置决定是否使用并发"""
        # 配置为不使用并发时，使用串行处理
        if not self.use_concurrent:
            total = len(contents)
            
            # 获取当前模型的间隔时间配置
            interval_seconds = 5  # 默认5秒
            if self.model_choice == "openaiapi":
                interval_seconds = self.openaiapi_config.get('interval_seconds', 5) if self.openaiapi_config else 5
            
            print(f"\n🔄 开始串行处理 {total} 篇文章（任务间隔: {interval_seconds}秒）...")
            
            for i, content in enumerate(contents, 1):
                print(f"\n📝 处理第 {i}/{total} 篇文章: {content['title']}")
                
                status, data = self.process_single_article(content)
                
                if status == 'success':
                    self.success_articles += 1
                    print(f"✅ 文章处理成功")
                else: # status == 'failure'
                    self.failed_articles += 1
                    failure_type = data['type']
                    error_msg = data['error']
                    self.add_failure_stat(failure_type, error_msg)
                    self.failed_articles_info.append({
                        'original_file': content.get('md_path', '未知文件'),
                        'title': content.get('title', '未知标题'),
                        'error': error_msg
                    })
                    print(f"❌ 文章处理失败")

                # 每篇文章处理完后等待配置的时间
                if i < total:
                    print(f"\n⏳ 等待 {interval_seconds} 秒后处理下一篇...")
                    time.sleep(interval_seconds)
            
            print(f"\n📊 本批次完成情况:")
            print(f"📝 总数: {total} 篇")
            print(f"✅ 成功: {self.success_articles} 篇")
            print(f"❌ 失败: {self.failed_articles} 篇")
            
            # 显示失败原因统计
            if self.failed_articles > 0:
                self.display_failure_stats()
            
            return self.success_articles
            
        # 使用并发处理
        if not self.concurrent_executor:
            # 获取当前模型的配置
            interval_seconds = self.openaiapi_config.get('interval_seconds', 10) if self.openaiapi_config else 10
            
            print(f"\n🚀 初始化并发执行器，最大并发数: {self.concurrent_tasks}，任务间隔: {interval_seconds}秒")
            self.concurrent_executor = ConcurrentExecutor(
                max_workers=self.concurrent_tasks,
                interval_seconds=interval_seconds
            )
        
        print(f"\n🚀 开始并发处理 {len(contents)} 篇文章...")
        # 注意：这里的 process_single_article 已经改造为不直接修改共享状态
        results = self.concurrent_executor.process_batch(contents, self.process_single_article, self)
        
        # 统一处理并发结果
        # 成功结果是 ('success', {'saved_path': ..., 'content': ...})
        for success_result in results['successes']:
            status, data = success_result
            if status == 'success':
                self.success_articles += 1
                print(f"✅ 文章处理成功: {data['content']['title']}")

        # 失败结果有两种可能：
        # 1. 函数正常返回的 ('failure', {...})
        # 2. 执行器捕获的异常 {'content': ..., 'error': ...}
        for failure in results['failures']:
            self.failed_articles += 1
            
            # 情况1: 我们的函数正常返回了失败信息
            if isinstance(failure, tuple) and failure[0] == 'failure':
                status, data = failure
                content = data['content']
                failure_type = data['type']
                error_msg = str(data['error'])
            # 情况2: 执行器捕获了意外异常
            else:
                content = failure.get('content', {})
                error_msg = str(failure.get('error', '未知执行器错误'))
                error_str = error_msg.lower()
                failure_type = 'other_exceptions'
                if 'timeout' in error_str:
                    failure_type = 'task_timeout'
                elif any(keyword in error_str for keyword in ['api', 'network', 'connection', 'http']):
                    failure_type = 'ai_service_error'

            # 统一记录失败
            self.add_failure_stat(failure_type, error_msg)
            self.failed_articles_info.append({
                'original_file': content.get('md_path', '未知文件'),
                'title': content.get('title', '未知标题'),
                'error': error_msg
            })
            print(f"❌ 文章处理失败: {content.get('title', '未知标题')}")


        print(f"\n📊 批次处理结束时统计:")
        print(f"📝 总提交: {len(contents)} 篇")
        print(f"✅ 累计成功: {self.success_articles} 篇")
        print(f"❌ 累计失败: {self.failed_articles} 篇")
        
        return self.success_articles

    def select_next_content(self) -> Optional[Dict[str, Any]]:
        """
        获取下一篇待处理的文章内容。

        Returns:
            Optional[Dict[str, Any]]: 文档选择结果，若无可用文件返回None。
        """
        contents = self.document_processor.select_content(1)
        if contents:
            return contents[0]
        return None

    def create_single_article(self, content: Optional[Dict[str, Any]] = None, update_stats: bool = True):
        """
        生成单篇文章（可选地更新内部统计信息）。

        Args:
            content: 预先选定的内容字典；若为None则自动选择下一篇。
            update_stats: 是否更新success/failed等统计信息。

        Returns:
            tuple: ('success', {'saved_path': str, 'content': dict}) 或
                   ('failure', {'type': str, 'error': str, 'content': dict})
        """
        if content is None:
            content = self.select_next_content()
            if not content:
                failure_result = {
                    'type': 'other_exceptions',
                    'error': '未找到可用的文章内容',
                    'content': {}
                }
                if update_stats:
                    self.failed_articles += 1
                    self.add_failure_stat(failure_result['type'], failure_result['error'])
                    self.failed_articles_info.append({
                        'original_file': '未知文件',
                        'title': '未选择内容',
                        'error': failure_result['error']
                    })
                return 'failure', failure_result

        result = self.process_single_article(content)
        status, data = result

        if update_stats:
            if status == 'success':
                self.success_articles += 1
            else:
                failure_type = data.get('type', 'other_exceptions')
                error_msg = data.get('error', '')
                self.failed_articles += 1
                self.add_failure_stat(failure_type, error_msg)
                content_info = data.get('content', content)
                self.failed_articles_info.append({
                    'original_file': content_info.get('md_path', '未知文件'),
                    'title': content_info.get('title', '未知标题'),
                    'error': error_msg
                })

        return result

    def add_failure_stat(self, failure_type: str, error_msg: str = ""):
        """
        添加失败统计并记录到failed_articles_info
        
        Args:
            failure_type: 失败类型 ('prompt_transmission_error', 'sensitive_keyword_filter', 
                         'file_read_error', 'ai_service_error', 'task_timeout', 'other_exceptions')
            error_msg: 错误信息
        """
        if failure_type == 'sensitive_keyword_filter':
            self.invalid_keyword_articles += 1

        if failure_type in self.failure_stats:
            self.failure_stats[failure_type] += 1
        else:
            self.failure_stats['other_exceptions'] += 1

    def display_failure_stats(self):
        """显示失败原因统计信息"""
        if self.failed_articles == 0:
            return
            
        print("\n📊 失败原因统计:")
        print("-"*50)
        
        failure_type_names = {
            'prompt_transmission_error': 'Prompt传输问题',
            'sensitive_keyword_filter': '敏感关键词过滤', 
            'file_read_error': '文件读取错误',
            'ai_service_error': 'AI服务错误',
            'task_timeout': '任务超时',
            'other_exceptions': '其他异常'
        }
        
        for error_type, count in self.failure_stats.items():
            if count > 0:
                type_name = failure_type_names.get(error_type, error_type)
                percentage = (count / self.failed_articles) * 100
                print(f"   {type_name}: {count} 篇 ({percentage:.1f}%)")
        
        print("-"*50)

    def get_current_batch_size(self) -> int:
        """
        获取当前服务的批次大小
        
        Returns:
            int: 当前批次大小
        """
        # 优先使用配置文件中的批次大小设置
        if self.model_choice == "openaiapi" and self.openaiapi_config:
            return self.openaiapi_config.get('max_concurrent_tasks', 10)
        return 10

    def run_processing_pipeline(self):
        """
        执行完整的处理流水线，一次性提交所有任务。
        """
        if self.total_articles <= 0:
            print("❌ 未设置要处理的文章数量。")
            return

        print("\n" + "="*50)
        print("🚀 开始执行文章处理流水线...")
        print(f"🔄 模式: {'并发处理' if self.use_concurrent else '串行处理'}")
        if self.use_concurrent:
            print(f"🛠️ 最大并发数: {self.concurrent_tasks}")
        print(f"📝 计划处理文章总数: {self.total_articles}")
        print("="*50 + "\n")

        # 1. 一次性选择所有需要处理的内容
        all_contents = self.document_processor.select_content(self.total_articles)
        if not all_contents:
            print("❌ 未能选择到任何有效内容进行处理。")
            return

        # 2. 将所有内容一次性交给 process_batch 处理
        self.process_batch(all_contents)

        # 处理完成后，所有统计信息（success_articles, failed_articles）都已被更新
        # 最终的统计信息将在 main 函数中显示

    def display_progress(self):
        """显示进度信息"""
        print("\n" + "="*50)
        print(f"🔄 === 第 {self.current_round}/{self.total_rounds} 轮 ===")
        print(f"📊 总需求：{self.total_articles}篇")
        
        remaining = self.total_articles - (self.success_articles + self.failed_articles)
        
        # 获取当前批次大小
        if not self.use_concurrent:
            current_batch_size = 1
        else:
            current_batch_size = self.get_current_batch_size()
            
        this_round = min(current_batch_size, remaining)
        
        print(f"📈 已完成：{self.success_articles + self.failed_articles}篇")
        print(f"✅ 成功：{self.success_articles}篇")
        print(f"❌ 失败：{self.failed_articles}篇")
        print(f"因包含敏感关键词作废的文章数: {self.invalid_keyword_articles} 篇")
        print(f"📝 本轮处理：{this_round}篇")
        print(f"⏳ 剩余需求：{remaining}篇")
        
        # 显示可用的md文件数量
        print(f"📁 可用MD文件：{len(self.document_processor.all_available_files)}个")
        print(f"   - 有txt文件：{len(self.document_processor.md_files_with_txt)}个")
        print(f"   - 无txt文件：{len(self.document_processor.md_files_without_txt)}个")
        if len(self.document_processor.all_available_files) < this_round:
            print(f"⚠️ 注意：可用MD文件数量少于本轮处理数量，将按顺序循环使用MD文件")
        
        print("="*50 + "\n")
        
    def calculate_total_rounds(self):
        """计算总轮次"""
        if self.model_choice in ["gemini"] or not self.use_concurrent:
            # 串行模式：每轮1篇，轮次等于文章总数
            self.total_rounds = self.total_articles
        else:
            # 并发模式：根据批次大小计算轮次
            current_batch_size = self.get_current_batch_size()
            self.total_rounds = (self.total_articles + current_batch_size - 1) // current_batch_size

def main():
    """主函数"""
    print("\n🚀 AI文章批量改写工具")
    print("="*50)
    print("📖 程序功能说明:")
    print("   - 递归搜索指定文件夹及其所有子文件夹中的MD文件")
    print("   - 智能标题处理：")
    print("     • 有txt文件的MD文件：从txt文件中随机选择标题行")
    print("     • 无txt文件的MD文件：自动提取文件名作为标题")
    print("   - 支持OpenAI兼容平台（Poe、ChatNP等，统一走官方OpenAI SDK）")
    print("   - 支持高并发处理，MD文件按顺序循环使用")
    print("   - 生成的文章保存在 C:\\ai_output\\2025ALL 目录下")
    print("="*50)
    
    while True:
        # 直接要求用户输入文件夹路径
        while True:
            base_folder = input("\n📁 请输入包含MD文件的文件夹路径: ").strip()
            if not os.path.isdir(base_folder):
                print("❌ 无效的文件夹路径，请重新输入")
                continue
                
            # 选择prompt文件
            prompt_dir = os.path.join(os.path.dirname(__file__), 'prompt')
            if not os.path.isdir(prompt_dir):
                print(f"❌ 未找到prompt文件夹: {prompt_dir}")
                continue
            prompt_files = [f for f in os.listdir(prompt_dir) if f.endswith('.txt')]
            if not prompt_files:
                print(f"❌ prompt文件夹下没有可用的txt文件: {prompt_dir}")
                continue
            print("\n📝 可用的提示词模板:")
            for idx, fname in enumerate(prompt_files, 1):
                print(f"   {idx}. {fname}")
            while True:
                try:
                    prompt_choice = int(input(f"请选择要使用的提示词模板 (1-{len(prompt_files)}): "))
                    if 1 <= prompt_choice <= len(prompt_files):
                        break
                    print("❌ 请输入有效的编号")
                except ValueError:
                    print("❌ 请输入数字编号")
            prompt_file_path = os.path.join(prompt_dir, prompt_files[prompt_choice-1])
            
            # 选择headfoot文件
            headfoot_dir = os.path.join(os.path.dirname(__file__), 'headfoot')
            if not os.path.isdir(headfoot_dir):
                print(f"❌ 未找到headfoot文件夹: {headfoot_dir}")
                continue
            headfoot_files = [f for f in os.listdir(headfoot_dir) if f.endswith('.py')]
            if not headfoot_files:
                print(f"❌ headfoot文件夹下没有可用的py文件: {headfoot_dir}")
                continue
            
            print("\n🎨 可用的头尾模板:")
            for idx, fname in enumerate(headfoot_files, 1):
                print(f"   {idx}. {fname}")
            
            headfoot_file_path = None
            while True:
                try:
                    headfoot_input = input(f"请选择要使用的头尾模板 (1-{len(headfoot_files)}, 直接回车使用默认的headfoot.py): ").strip()
                    if headfoot_input == "":
                        # 直接回车，使用默认的headfoot.py
                        default_headfoot = os.path.join(headfoot_dir, 'headfoot.py')
                        if os.path.exists(default_headfoot):
                            headfoot_file_path = default_headfoot
                            print("✅ 使用默认头尾模板: headfoot.py")
                        else:
                            print("❌ 默认的headfoot.py文件不存在，请选择其他文件")
                            continue
                        break
                    else:
                        headfoot_choice = int(headfoot_input)
                        if 1 <= headfoot_choice <= len(headfoot_files):
                            headfoot_file_path = os.path.join(headfoot_dir, headfoot_files[headfoot_choice-1])
                            print(f"✅ 已选择头尾模板: {headfoot_files[headfoot_choice-1]}")
                            break
                        print("❌ 请输入有效的编号")
                except ValueError:
                    print("❌ 请输入数字编号或直接回车使用默认")
            
            # 初始化ContentManager
            manager = ContentManager(base_folder, prompt_file_path=prompt_file_path, headfoot_file_path=headfoot_file_path)
            if not manager.document_processor.all_available_files:
                print("❌ 未找到任何可用的MD文件")
                continue
            break
            
        # 输入需要生成的文章数量
        while True:
            try:
                prompt = f"\n📊 请输入需要生成的文章数量 (当前有{len(manager.document_processor.all_available_files)}个可用MD文件): "
                print("💡 提示：如果生成数量超过可用MD文件数量，系统将按顺序循环使用MD文件")
                print("        - 有txt文件的将使用txt中的随机标题")
                print("        - 无txt文件的将使用从文件名提取的标题")
                article_count = int(input(prompt))
                if article_count > 0:
                    manager.total_articles = article_count
                    manager.calculate_total_rounds()
                    break
                print("❌ 请输入大于0的数字")
            except ValueError:
                print("❌ 请输入有效的数字")
                
        # 选择AI模型
        # 仅使用 OpenAI 兼容平台
        while True:
            manager.model_choice = "openaiapi"
            # 检查OpenAI API配置是否存在
            if not manager.openaiapi_config:
                print("❌ 未找到OpenAI API配置")
                continue
            
            # 初始化OpenAI API服务
            manager.init_openaiapi_client()
            
            # 显示可用平台
            platforms = manager.openaiapi_service.get_available_platforms()
            print("\n🤖 请选择平台:")
            for i, platform in enumerate(platforms, 1):
                platform_info = manager.openaiapi_service.get_platform_info(platform)
                platform_name = platform_info.get('name', platform)
                print(f"   {i}. {platform_name}")
            
            # 用户选择平台
            while True:
                try:
                    platform_choice = int(input(f"请输入平台编号 (1-{len(platforms)}): "))
                    if 1 <= platform_choice <= len(platforms):
                        selected_platform = platforms[platform_choice-1]
                        manager.openaiapi_service.set_platform(selected_platform)
                        break
                    print("❌ 无效的选项，请重新选择")
                except ValueError:
                    print("❌ 请输入有效的数字")
            
            # 显示该平台的可用模型
            models = manager.openaiapi_service.get_platform_models(selected_platform)
            print(f"\n🔧 请选择 {manager.openaiapi_service.get_platform_info(selected_platform).get('name', selected_platform)} 的模型:")
            for i, model in enumerate(models, 1):
                print(f"   {i}. {model}")
            
            # 用户选择模型
            while True:
                try:
                    model_choice = int(input(f"请输入模型编号 (1-{len(models)}): "))
                    if 1 <= model_choice <= len(models):
                        selected_model = models[model_choice-1]
                        manager.openaiapi_service.set_model(selected_model)
                        
                        # 打印并发设置信息
                        if manager.use_concurrent:
                            print(f"✅ 已启用并发处理，最大并发数: {manager.concurrent_tasks}")
                        else:
                            print("❌ 并发处理未启用，将使用串行处理")
                        break
                    print("❌ 无效的选项，请重新选择")
                except ValueError:
                    print("❌ 请输入有效的数字")
            break
                
        # 开始处理
        manager.run_processing_pipeline()
            
        # 显示最终结果
        print("\n" + "="*50)
        print("🎉 所有文章处理完成!")
        print(f"📊 总计: {manager.total_articles} 篇")
        print(f"✅ 成功: {manager.success_articles} 篇")
        print(f"❌ 失败: {manager.failed_articles} 篇")
        print(f"⚠️ 因包含敏感关键词作废: {manager.invalid_keyword_articles} 篇")
        
        # 显示失败原因统计
        if manager.failed_articles > 0:
            manager.display_failure_stats()

        if manager.failed_articles_info:
            print("\n📋 失败文章详细信息:")
            print("-"*50)
            for i, info in enumerate(manager.failed_articles_info, 1):
                print(f"\n{i}. 失败文章:")
                print(f"   📄 原文件: {info['original_file']}")
                print(f"   📝 标题: {info['title']}")
                print(f"   ❌ 错误: {info['error']}")
        print("="*50)
        
        choice = input("\n🔄 是否继续处理其他文件夹? (y/n): ").strip().lower()
        if choice != 'y':
            print("\n👋 感谢使用,再见!")
            break
            
if __name__ == "__main__":
    main()