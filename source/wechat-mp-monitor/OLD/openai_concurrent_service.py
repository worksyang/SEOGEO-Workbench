"""
OpenAI 并发图片OCR服务实现 - 全并行架构版
每张图片独立并行处理：AI 视觉识别（含复杂度判断，复杂表格同样直接采用 AI 结果）
真正的端到端并发处理，最大化效率
"""
import os
import json
import random
import asyncio
import base64
import logging
import shutil
import re
import time
import traceback  # 添加traceback模块用于详细错误信息
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from threading import Lock
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from openai import OpenAI

# ===================== 模型/服务基础配置（方便快速修改） =====================
MODEL_CONFIG = {
    # 主模型配置
    "primary_platform": "siliconflow",  # 主平台：硅基流动
    "primary_model": "Qwen/Qwen3-VL-235B-A22B-Instruct",  # 主模型
    # 备用模型配置
    "fallback_model_1": "Gemini-3-Flash",  # 备用模型1（Poe平台）
    "fallback_model_1_platform": "poe",
    "fallback_model_2": "gemini-2.5-flash-lite",  # 备用模型2（ChatNP-Gemini平台）
    "fallback_model_2_platform": "chatnp_gemini",
    # 处理模式
    "processing_mode": "serial",  # serial: 串行处理, parallel: 并行处理
    # 常用请求参数
    "timeout": 60.0,              # 默认OpenAI客户端超时
    "secondary_timeout": 30.0,    # 主/备用模型识别超时
    "max_tokens": 4000,           # 初审/识别阶段
}
# ========================================================================

# 导入根目录的配置
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prompts_config import SIMPLE_OCR_PROMPT, ENABLE_COMPLEX_OCR

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class PNG2MDConverter:
    """图片转Markdown表格识别器 - 全并行处理版，支持备用模型重试"""
    
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, model: Optional[str] = None, pro_model: str = None):
        # 允许外部传入覆盖，否则使用统一配置
        if api_key and base_url:
            self.api_key = api_key
            self.base_url = base_url
            self.model = model or MODEL_CONFIG["primary_model"]
        else:
            # 使用配置的主平台（默认为 siliconflow）
            primary_platform = MODEL_CONFIG.get("primary_platform", "siliconflow")
            self.api_key, self.base_url = self._load_platform_credentials(primary_platform)
            # 主模型：优先使用外部传入，否则使用配置的主模型
            self.model = model or MODEL_CONFIG["primary_model"]

        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=MODEL_CONFIG["timeout"]  # 设置默认超时时间
        )
        
        # 加载备用模型配置（备用模型1: Poe平台, 备用模型2: ChatNP-Gemini平台）
        self._load_fallback_models()

    def _load_platform_credentials(self, platform_key: str) -> Tuple[str, str]:
        """
        从 openaiapi.json 加载指定平台的 api_key 与 base_url
        """
        config_path = Path(__file__).resolve().parents[1] / "openaiapi.json"
        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在: {config_path}")

        with config_path.open("r", encoding="utf-8") as f:
            config = json.load(f)

        platform = config.get("platforms", {}).get(platform_key)
        if not platform:
            raise KeyError(f"未找到平台配置: {platform_key}")

        api_key = platform.get("api_key")
        base_url = platform.get("base_url")
        if not api_key or not base_url:
            raise ValueError(f"平台 {platform_key} 缺少 api_key 或 base_url")

        logger.info(f"✅ 已从配置加载平台: {platform.get('name', platform_key)}")
        return api_key, base_url
    
    
    def _load_fallback_models(self):
        """加载备用模型配置：备用模型1(Poe平台) + 备用模型2(ChatNP-Gemini平台)"""
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(current_dir)
            config_path = os.path.join(project_root, 'openaiapi.json')
            
            if os.path.exists(config_path):
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    platforms = config.get('platforms', {})
                    
                    self.fallback_models = []
                    
                    # 备用模型1: Poe平台
                    poe_config = platforms.get(MODEL_CONFIG["fallback_model_1_platform"], {})
                    if poe_config:
                        fallback_model_1 = {
                            'name': MODEL_CONFIG["fallback_model_1"],
                            'api_key': poe_config.get('api_key'),
                            'base_url': poe_config.get('base_url'),
                            'model': MODEL_CONFIG["fallback_model_1"]
                        }
                        self.fallback_models.append(fallback_model_1)
                        logger.info(f"✅ 已加载备用模型1（{MODEL_CONFIG['fallback_model_1_platform']}）：{MODEL_CONFIG['fallback_model_1']}")
                    else:
                        logger.warning(f"⚠️ 未找到 {MODEL_CONFIG['fallback_model_1_platform']} 平台配置")
                    
                    # 备用模型2: ChatNP-Gemini平台
                    chatnp_gemini_config = platforms.get(MODEL_CONFIG["fallback_model_2_platform"], {})
                    if chatnp_gemini_config:
                        fallback_model_2 = {
                            'name': MODEL_CONFIG["fallback_model_2"],
                            'api_key': chatnp_gemini_config.get('api_key'),
                            'base_url': chatnp_gemini_config.get('base_url'),
                            'model': MODEL_CONFIG["fallback_model_2"]
                        }
                        self.fallback_models.append(fallback_model_2)
                        logger.info(f"✅ 已加载备用模型2（{MODEL_CONFIG['fallback_model_2_platform']}）：{MODEL_CONFIG['fallback_model_2']}")
                    else:
                        logger.warning(f"⚠️ 未找到 {MODEL_CONFIG['fallback_model_2_platform']} 平台配置")
                    
                    if not self.fallback_models:
                        logger.warning("⚠️ 未加载任何备用模型")
            else:
                self.fallback_models = []
                logger.warning(f"⚠️ 配置文件不存在: {config_path}")
        except Exception as e:
            self.fallback_models = []
            logger.error(f"❌ 加载备用模型配置失败: {str(e)}")
    
    def encode_image(self, image_path: str) -> str:
        """将图片编码为base64格式"""
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    
    async def process_single_image_full_pipeline(self, image_path: str, image_url: str, custom_prompt: Optional[str] = None, log_callback=None) -> str:
        """
        单张图片的完整并行处理流水线
        
        流程：
        1. AI 识别（并可判断表格复杂度，用于日志与后续扩展）
        2. 返回 AI 的 Markdown 结果（复杂表格不再调用第三方 OCR 或融合）
        
        Args:
            image_path: 图片文件路径
            image_url: 图片URL（用于日志显示）
            custom_prompt: 自定义提示词
            log_callback: 日志回调函数
            
        Returns:
            转换后的Markdown文本
        """
        img_name = os.path.basename(image_path)
        
        try:
            if log_callback:
                log_callback(f"🔍 开始处理图片: {img_name}")
            
            logger.info(f"🚀 启动全并行处理流水线: {img_name}")
            
            # 步骤1：AI初审（获取结果和复杂度）- 添加超时控制和备用模型重试
            try:
                # 增加超时时间，因为可能需要重试多个模型
                # 超时时间设置为150秒：主模型30秒 + 备用模型1 30秒 + 备用模型2 30秒 + 缓冲60秒
                ai_md, is_complex = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._ai_recognize_with_complexity,
                        image_path,
                        custom_prompt or SIMPLE_OCR_PROMPT,
                        use_fallback=False,
                        fallback_index=0
                    ),
                    timeout=150  # 150秒超时，给备用模型重试留出足够时间
                )
            except asyncio.TimeoutError:
                logger.error(f"⏱️ AI识别总超时（包括备用模型）: {img_name}")
                if log_callback:
                    log_callback(f"⏱️ AI识别总超时: {img_name}")
                return None  # 返回None表示处理超时，上层函数会处理
            except Exception as e:
                logger.error(f"❌ AI识别异常: {img_name} - {str(e)}")
                if log_callback:
                    log_callback(f"❌ AI识别异常: {img_name}")
                return None  # 返回None表示处理失败
            
            if not is_complex:
                # 简单图片，直接返回AI结果
                logger.info(f"✅ 简单图片处理完成: {img_name}")
                if log_callback:
                    log_callback(f"✅ 简单图片处理完成: {img_name}")
                return ai_md
            
            # 复杂表格：仅使用 AI 识别结果
            logger.info(f"✅ 复杂表格，使用 AI 识别结果: {img_name}")
            if log_callback:
                log_callback(f"✅ 复杂表格，使用 AI 识别结果: {img_name}")
            return ai_md
                
        except Exception as e:
            error_detail = traceback.format_exc()
            logger.error(f"❌ 图片处理失败: {img_name} - {str(e)}\n{error_detail}")
            if log_callback:
                log_callback(f"❌ 图片处理失败: {img_name}")
            return None  # 返回None表示处理失败
    
    def _ai_recognize_with_complexity(self, image_path: str, prompt: str, use_fallback: bool = False, fallback_index: int = 0) -> Tuple[str, bool]:
        """
        AI识别图片内容并判断复杂度，支持备用模型重试
        
        Args:
            image_path: 图片路径
            prompt: 提示词
            use_fallback: 是否使用备用模型
            fallback_index: 备用模型索引
            
        Returns:
            (markdown_result, is_complex)
        """
        try:
            img_name = os.path.basename(image_path)
            
            # 选择使用的模型和客户端
            if use_fallback and self.fallback_models and fallback_index < len(self.fallback_models):
                fallback = self.fallback_models[fallback_index]
                logger.info(f"🔄 使用备用模型 {fallback['name']} 识别: {img_name}")
                client = OpenAI(
                    api_key=fallback['api_key'],
                    base_url=fallback['base_url'],
                    timeout=MODEL_CONFIG["secondary_timeout"]
                )
                model = fallback['model']
            else:
                logger.info(f"🔍 AI识别: {img_name}")
                client = OpenAI(
                    api_key=self.api_key,
                    base_url=self.base_url,
                    timeout=MODEL_CONFIG["secondary_timeout"]  # 比外部超时更短，确保能正常返回
                )
                model = self.model
            
            base64_image = self.encode_image(image_path)
            
            # 调用API
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是一个专业的OCR识别助手，负责将图片中的文本转换为Markdown格式。"},
                    {"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]}
                ],
                max_tokens=MODEL_CONFIG["max_tokens"]
            )
            
            # 提取结果
            result = response.choices[0].message.content
            
            # 提取Markdown代码块
            markdown_result = self._extract_markdown_from_codeblock(result)
            
            # 判断是否为复杂内容（受开关控制）
            is_complex = self._is_complex_markdown(markdown_result) if ENABLE_COMPLEX_OCR else False
            
            return markdown_result, is_complex
            
        except Exception as e:
            error_detail = traceback.format_exc()
            img_name = os.path.basename(image_path)
            error_str = str(e).lower()
            
            # 判断是否为不可恢复的错误（API key错误、权限错误等，备用模型也无法解决）
            is_unrecoverable_error = (
                "invalid api key" in error_str or
                "unauthorized" in error_str or
                "forbidden" in error_str or
                "authentication" in error_str or
                "permission" in error_str or
                "401" in error_str or
                "403" in error_str
            )
            
            # 如果是不可恢复的错误，且不是备用模型，直接返回错误
            if is_unrecoverable_error and not use_fallback:
                logger.error(f"❌ AI识别失败（不可恢复错误）: {img_name} - {str(e)}")
                return f"## AI识别失败\n\n图片处理出错: {str(e)}", False
            
            # 如果有备用模型且还未使用，尝试使用备用模型（包括超时和其他可恢复错误）
            if not use_fallback and hasattr(self, 'fallback_models') and self.fallback_models:
                error_type = "超时" if ("timeout" in error_str or "timed out" in error_str or isinstance(e, (TimeoutError,))) else "错误"
                first_fallback_name = self.fallback_models[0]['name'] if self.fallback_models else '未知'
                logger.warning(f"⚠️ 主模型{error_type}，尝试备用模型1 ({first_fallback_name}): {img_name}")
                # 尝试第一个备用模型
                try:
                    return self._ai_recognize_with_complexity(image_path, prompt, use_fallback=True, fallback_index=0)
                except Exception as e2:
                    error_str2 = str(e2).lower()
                    error_type2 = "超时" if ("timeout" in error_str2 or "timed out" in error_str2 or isinstance(e2, (TimeoutError,))) else "错误"
                    # 尝试第二个备用模型
                    if len(self.fallback_models) > 1:
                        second_fallback_name = self.fallback_models[1]['name']
                        logger.warning(f"⚠️ 备用模型1{error_type2}，尝试备用模型2 ({second_fallback_name}): {img_name}")
                        try:
                            return self._ai_recognize_with_complexity(image_path, prompt, use_fallback=True, fallback_index=1)
                        except Exception as e3:
                            logger.error(f"❌ 所有模型都失败: {img_name}")
                            return f"## AI识别失败\n\n所有模型都失败: {str(e3)}", False
                    else:
                        logger.error(f"❌ 备用模型1失败，无更多备用模型: {img_name}")
                        return f"## AI识别失败\n\n主模型和备用模型都失败: {str(e2)}", False
            elif use_fallback and hasattr(self, 'fallback_models') and self.fallback_models and fallback_index < len(self.fallback_models) - 1:
                # 当前备用模型失败，尝试下一个备用模型
                next_index = fallback_index + 1
                next_model_name = self.fallback_models[next_index]['name'] if next_index < len(self.fallback_models) else '未知'
                error_type = "超时" if ("timeout" in error_str or "timed out" in error_str or isinstance(e, (TimeoutError,))) else "错误"
                logger.warning(f"⚠️ 备用模型{fallback_index+1}{error_type}，尝试备用模型{next_index+1} ({next_model_name}): {img_name}")
                try:
                    return self._ai_recognize_with_complexity(image_path, prompt, use_fallback=True, fallback_index=next_index)
                except Exception as e2:
                    logger.error(f"❌ 所有备用模型都失败: {img_name}")
                    return f"## AI识别失败\n\n所有模型都失败: {str(e2)}", False
            
            logger.error(f"❌ AI识别异常: {img_name} - {str(e)}")
            # 返回空结果和非复杂标记，避免进一步处理
            return f"## AI识别失败\n\n图片处理出错: {str(e)}", False
    
    def _extract_markdown_from_codeblock(self, content: str) -> str:
        """从AI输出中提取最后一个markdown代码块内容"""
        markdown_pattern = r'```markdown\s*(.*?)\s*```'
        matches = re.findall(markdown_pattern, content, re.DOTALL)
        
        if matches:
            return matches[-1].strip()
        return content
    
    def _is_complex_markdown(self, md: str) -> bool:
        """判断是否为复杂表格"""
        # 检查是否包含"该图片无任何信息，请删除"
        if "该图片无任何信息，请删除" in md:
            logger.info("✅ 检测到'该图片无任何信息，请删除'标记，视为简单图片")
            return False
            
        sections = md.split('\n\n')
        
        for section in sections:
            if '|' in section:
                # 检查是否为表格
                lines = section.strip().split('\n')
                if len(lines) >= 2:
                    # 检查第二行是否为表格分隔符
                    if lines[1].startswith('|') and all(c == '-' or c == '|' or c == ':' or c == ' ' for c in lines[1]):
                        # 这是一个表格，检查是否复杂
                        return self._is_single_table_complex(section)
        
        # 没有表格或者表格不复杂
        return False
    
    def _is_single_table_complex(self, table_text: str) -> bool:
        """判断单张表格是否复杂：列数不一致 或 大于8列"""
        lines = [l.strip() for l in table_text.splitlines() if '|' in l and l.strip()]
        if not lines:
            return False
        
        # 过滤掉分隔行
        table_lines = []
        for line in lines:
            if re.match(r'^[\|\-\:\s]+$', line):
                continue
            table_lines.append(line)
        
        if not table_lines:
            return False
        
        # 计算每行的列数
        col_counts = []
        for line in table_lines:
            if line.count('|') > 1:
                col_count = line.count('|') - 1
                col_counts.append(col_count)
        
        if not col_counts:
            return False
        
        unique_col_counts = set(col_counts)
        max_cols = max(col_counts)
        
        # 条件1：列数不一致
        has_inconsistent_cols = len(unique_col_counts) > 1
        
        # 条件2：大于8列
        has_many_cols = max_cols >= 8
        
        # 只要满足其中一个条件就认为是复杂表格
        is_complex = has_inconsistent_cols or has_many_cols
        
        if is_complex:
            reason = []
            if has_inconsistent_cols:
                reason.append(f"列数不一致({list(unique_col_counts)})")
            if has_many_cols:
                reason.append(f"列数过多({max_cols}列)")
            logger.info(f"🔍 表格复杂度分析: {', '.join(reason)}")
        
        return is_complex


class OpenAIConcurrentService:
    """OpenAI并发OCR服务 - 支持串行/并行处理"""
    
    _lock = Lock()  # 类变量，用于线程同步
    _failed_tokens = set()  # 失败的token黑名单
    _token_usage_count = {}  # token使用计数
    
    def __init__(self, max_workers: int = 1, max_retries: int = 5):
        """
        初始化OpenAI并发服务
        
        Args:
            max_workers: 最大并发工作线程数（默认1为串行处理）
            max_retries: 单个图片的最大重试次数
        """
        self.max_workers = max_workers
        self.max_retries = max_retries
        
        # 初始化PNG2MD转换器（使用配置的主模型）
        self.png2md_converter = PNG2MDConverter(
            api_key=None,
            base_url=None,
            model=None,  # 使用 MODEL_CONFIG 中配置的主模型
            pro_model=None  # 不使用Pro模型
        )
        
        # 与转换器保持一致的主模型配置，避免重复读取配置
        self.api_key = self.png2md_converter.api_key
        self.base_url = self.png2md_converter.base_url
        self.model = self.png2md_converter.model
        
        # 为了兼容性，创建一个假的api_keys列表
        self.api_keys = [self.api_key]
        
        # 默认提示词
        self.default_prompt = SIMPLE_OCR_PROMPT
        
        # 处理模式
        processing_mode = "串行" if max_workers == 1 else f"并行(并发数: {max_workers})"
        
        logger.info(f"✅ OpenAI服务初始化完成")
        logger.info(f"🤖 主模型: {self.model}")
        logger.info(f"🔄 处理模式: {processing_mode}")
        
    def get_available_token(self) -> Optional[str]:
        """线程安全地获取一个可用的token（兼容性方法）"""
        with self._lock:
            if self.api_key in self._failed_tokens:
                logger.error("❌ OpenAI token已被标记为失败")
                return None
                
            # 更新使用计数
            self._token_usage_count[self.api_key] = self._token_usage_count.get(self.api_key, 0) + 1
            
            logger.debug(f"🔑 使用OpenAI token (使用次数: {self._token_usage_count[self.api_key]})")
            return self.api_key
    
    def mark_token_failed(self, token: str, error_msg: str = "") -> None:
        """标记token为失败状态"""
        with self._lock:
            self._failed_tokens.add(token)
            logger.warning(f"⚠️ OpenAI Token标记为失败: {error_msg}")

    async def process_images_full_parallel(self, image_tasks: List[Tuple[str, str]], custom_prompt: Optional[str] = None, log_callback=None) -> Dict[str, str]:
        """
        处理所有图片（支持串行/并行模式）
        每张图片独立执行完整的处理流水线：AI 视觉识别（复杂表格同样直接采用 AI 结果）
        
        Args:
            image_tasks: 图片任务列表 [(image_path, image_url), ...]
            custom_prompt: 自定义提示词
            log_callback: 日志回调函数
            
        Returns:
            Dict[str, str]: {image_url: markdown_result}
        """
        if not image_tasks:
            return {}
        
        start_time = time.time()
        total_images = len(image_tasks)
        
        processing_mode = "串行" if self.max_workers == 1 else f"并行(并发数: {self.max_workers})"
        
        if log_callback:
            log_callback(f"🚀 启动{processing_mode}处理 {total_images} 张图片")
            log_callback(f"📋 处理模式：每张图片独立执行完整流水线")
        
        logger.info(f"🚀 启动{processing_mode}处理 {total_images} 张图片")
        
        # 创建所有图片的并行任务
        async_tasks = []
        for img_path, img_url in image_tasks:
            # 检查文件是否存在
            if not os.path.exists(img_path):
                logger.error(f"❌ 图片文件不存在: {img_path}")
                if log_callback:
                    log_callback(f"❌ 图片文件不存在: {img_path}")
                continue
                
            task = self.png2md_converter.process_single_image_full_pipeline(
                img_path, 
                img_url, 
                custom_prompt, 
                log_callback
            )
            async_tasks.append((task, img_url))
        
        # 执行所有并行任务
        results = {}
        completed_tasks = 0
        
        # 分批处理，避免同时处理过多图片导致资源耗尽
        # 根据 max_workers 决定批次大小：串行模式(max_workers=1)每次处理1张，并行模式最多20张
        batch_size = self.max_workers if self.max_workers == 1 else min(20, len(async_tasks))
        
        for i in range(0, len(async_tasks), batch_size):
            batch = async_tasks[i:i+batch_size]
            
            # 使用asyncio.gather执行当前批次的任务
            batch_tasks = []
            batch_urls = []
            
            for task, url in batch:
                # 为每个任务单独添加超时控制
                # 超时时间设置为150秒，给备用模型重试留出足够时间（主模型30秒 + 备用模型1 30秒 + 备用模型2 30秒 + 缓冲60秒）
                safe_task = asyncio.create_task(self._process_single_image_with_timeout(task, url, 150, log_callback))
                batch_tasks.append(safe_task)
                batch_urls.append(url)
            
            if log_callback and len(async_tasks) > 1:
                mode_text = "串行处理" if batch_size == 1 else f"批次 {i//batch_size + 1}/{(len(async_tasks)-1)//batch_size + 1}"
                log_callback(f"📦 {mode_text} ({len(batch)} 张图片)")
            
            # 使用asyncio.gather处理批次，设置return_exceptions=True确保一个任务失败不会影响其他任务
            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            
            # 处理结果
            for j, result in enumerate(batch_results):
                img_url = batch_urls[j]
                completed_tasks += 1
                
                if isinstance(result, Exception):
                    logger.error(f"❌ 图片处理异常: {img_url} - {str(result)}")
                    results[img_url] = f"## 处理异常\n\n处理图片时发生错误: {str(result)}"
                    if log_callback:
                        log_callback(f"❌ 处理异常 ({completed_tasks}/{total_images}): {img_url}")
                elif result is None:
                    # 处理超时或失败的情况，返回None让md_service添加失败标注
                    logger.error(f"⏱️ 图片处理超时或失败: {img_url}")
                    results[img_url] = None  # 超时的图片标记为None，让md_service处理
                    if log_callback:
                        log_callback(f"⏱️ 处理超时 ({completed_tasks}/{total_images}): {img_url}")
                else:
                    results[img_url] = result
                    if log_callback:
                        log_callback(f"✅ 处理完成 ({completed_tasks}/{total_images}): {img_url}")
        
        # 统计结果
        end_time = time.time()
        total_time = end_time - start_time
        success_count = sum(1 for result in results.values() if result and result.strip() and not result.startswith("## 处理"))
        
        if log_callback:
            log_callback(f"🎉 全并行处理完成！")
            log_callback(f"📊 处理统计: 成功 {success_count}/{total_images} 张")
            log_callback(f"⏱️ 总耗时: {total_time:.2f}秒，平均每张: {total_time/total_images:.2f}秒")
        
        logger.info(f"🎉 全并行处理完成！成功 {success_count}/{total_images} 张，耗时 {total_time:.2f}秒")
        
        return results

    async def _process_single_image_with_timeout(self, task, img_url, timeout_seconds, log_callback=None):
        """
        处理单个图片任务，添加严格的超时控制
        
        Args:
            task: 异步任务
            img_url: 图片URL
            timeout_seconds: 超时时间（秒）
            log_callback: 日志回调函数
            
        Returns:
            处理结果或None（如果超时）
        """
        try:
            # 使用asyncio.wait_for添加超时控制
            result = await asyncio.wait_for(task, timeout=timeout_seconds)
            return result
        except asyncio.TimeoutError:
            # 超时处理
            logger.error(f"⏱️ 图片处理总超时: {img_url} (超过{timeout_seconds}秒)")
            if log_callback:
                log_callback(f"⏱️ 图片处理总超时: {os.path.basename(img_url)}")
            return None
        except Exception as e:
            # 其他异常处理
            logger.error(f"❌ 图片处理异常: {img_url} - {str(e)}")
            if log_callback:
                log_callback(f"❌ 图片处理异常: {os.path.basename(img_url)}")
            return None

    # 兼容性方法：保持原有的分阶段接口，但内部使用新的全并行架构
    async def run_ai_parallel_phase(self, image_tasks: List[Tuple[str, str]], custom_prompt: Optional[str] = None, log_callback=None) -> (Dict[str, str], List[Tuple[str, str, str]]):
        """
        兼容性方法：模拟原有的AI初审阶段
        实际上使用新的全并行架构处理所有图片
        """
        if log_callback:
            log_callback("🔄 兼容性模式：使用全并行架构替代分阶段处理")
        
        # 使用全并行处理
        all_results = await self.process_images_full_parallel(image_tasks, custom_prompt, log_callback)
        
        # 为了兼容性，返回空的复杂任务列表（因为已经全部处理完成）
        return all_results, []

    def run_baidu_serial_phase(self, complex_tasks: List[Tuple[str, str, str]], log_callback=None) -> Dict[str, str]:
        """
        兼容性占位：历史接口保留。复杂图片已在 process_images_full_parallel 中一并完成。
        """
        if log_callback and complex_tasks:
            log_callback("ℹ️ 兼容性模式：复杂图片已在全并行阶段处理完成")
        
        return {}  # 返回空结果，因为已经在全并行阶段处理完成

    def get_statistics(self) -> Dict[str, Any]:
        """获取服务统计信息"""
        with self._lock:
            return {
                "total_tokens": 1,  # OpenAI只有一个token
                "failed_tokens": len(self._failed_tokens),
                "available_tokens": 1 - len(self._failed_tokens),
                "token_usage": dict(self._token_usage_count),
                "failed_token_list": list(self._failed_tokens),
                "architecture": "full_parallel"  # 标识为全并行架构
            }
    
    def reset_failed_tokens(self) -> None:
        """重置失败token列表（用于新的处理会话）"""
        with self._lock:
            self._failed_tokens.clear()
            logger.info("🔄 已重置OpenAI失败token列表")
    
    # 兼容性方法
    async def analyze_images_concurrent_with_index(self, image_tasks_with_index, log_callback=None):
        """兼容性方法：支持带索引的图片任务"""
        # 转换格式
        image_tasks = [(img_path, img_url) for img_path, img_url, _ in image_tasks_with_index]
        
        # 使用全并行处理
        return await self.process_images_full_parallel(image_tasks, log_callback=log_callback)
    
    async def analyze_images_concurrent_with_prompt_and_index(self, image_tasks_with_index, custom_prompt=None, log_callback=None):
        """兼容性方法：支持自定义提示词和索引的图片任务"""
        # 转换格式
        image_tasks = [(img_path, img_url) for img_path, img_url, _ in image_tasks_with_index]
        
        # 使用全并行处理
        return await self.process_images_full_parallel(image_tasks, custom_prompt, log_callback)

    def analyze_single_image(self, img_path: str, img_url: str, custom_prompt=None, log_callback=None) -> str:
        """兼容性方法：单张图片处理"""
        try:
            # 使用asyncio.run来运行异步方法
            return asyncio.run(
                self.png2md_converter.process_single_image_full_pipeline(
                    img_path, 
                    img_url, 
                    custom_prompt, 
                    log_callback
                )
            )
        except Exception as e:
            logger.error(f"❌ 单张图片分析失败: {img_url} - {str(e)}")
            return None 