from openai import OpenAI
import os
import time
import sys
import base64
import logging
from typing import Optional
import asyncio
from concurrent.futures import TimeoutError
import requests
# 导入根目录的配置
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prompts_config import STANDARD_OCR_PROMPT

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PLATFORM_CONFIG = {
    'tongyi': {
        'emoji': '🟩',
        'name': '通义千问',
        'api_key': os.environ.get('DASHSCOPE_API_KEY', ''),
        'api_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
        'model': 'qwen-vl-plus',
        'type': 'openai',
    },
    'siliconflow': {
        'emoji': '🟦',
        'name': '硅基流动',
        'api_key': os.environ.get('SILICONFLOW_API_KEY', ''),
        'api_url': 'https://api.siliconflow.cn/v1/chat/completions',
        'model': 'Qwen/Qwen2.5-VL-72B-Instruct',
        'type': 'http',
    },
    # 未来可扩展更多平台
}

DEFAULT_PROMPT = STANDARD_OCR_PROMPT

class PoeImageAnalyzerPlus:
    def __init__(self, platform: str = 'siliconflow', prompt: str = None, api_key: str = None, siliconflow_api_key: str = None, timeout: int = 60, max_retries: int = 3, stream: Optional[bool] = None):
        self.platform = platform
        self.prompt = prompt or DEFAULT_PROMPT
        self.timeout = timeout
        self.max_retries = max_retries
        self.config = PLATFORM_CONFIG.get(platform, PLATFORM_CONFIG['tongyi'])
        # 支持自定义API Key
        if platform == 'tongyi' and api_key:
            self.config['api_key'] = api_key
        if platform == 'siliconflow' and siliconflow_api_key:
            self.config['api_key'] = siliconflow_api_key
        # 流式输出控制
        if stream is not None:
            self.stream = stream
        else:
            self.stream = True if platform == 'tongyi' else False
        # 初始化 openai 客户端（仅通义）
        if self.config['type'] == 'openai':
            self.client = OpenAI(
                api_key=self.config['api_key'],
                base_url=self.config['api_url'],
                timeout=self.timeout
            )
        # logging.info(f"🚀 OCR服务已初始化，当前平台：{self.config['name']} {self.config['emoji']}，流式输出：{self.stream}")

    def _encode_image(self, image_path):
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')

    def analyze_image(self, image_path, custom_prompt=None) -> Optional[str]:
        if not os.path.exists(image_path):
            logging.error(f"图片文件不存在: {image_path}")
            return None
        emoji = self.config['emoji']
        name = self.config['name']
        logging.info(f"{emoji}[{name}] 开始单张图片识别...")
        
        # 使用传入的 custom_prompt 或默认的 self.prompt
        prompt_to_use = custom_prompt or self.prompt
        
        for attempt in range(self.max_retries):
            try:
                if self.config['type'] == 'openai':
                    messages = [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_to_use},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{self._encode_image(image_path)}"
                                }
                            }
                        ]
                    }]
                    logging.info(f"⚙️ [{name}] 开始分析图片...")
                    response = ""
                    for chunk in self.client.chat.completions.create(
                        model=self.config['model'],
                        messages=messages,
                        temperature=0.7,
                        max_tokens=2000,
                        stream=self.stream
                    ):
                        if chunk.choices[0].delta.content is not None:
                            content = chunk.choices[0].delta.content
                            print(content, end="", flush=True)
                            response += content
                    print()  # 换行
                    logging.info(f"✓ [{name}] 识别完成")
                    logging.info(f"识别结果：\n{response}")
                    return response
                elif self.config['type'] == 'http':
                    logging.info(f"⚙️ [{name}] 开始分析图片...")
                    image_b64 = self._encode_image(image_path)
                    payload = {
                        "model": self.config['model'],
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt_to_use},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
                                ]
                            }
                        ],
                        "stream": self.stream,
                        "max_tokens": 512,
                        "temperature": 0.7,
                        "top_p": 0.7,
                        "top_k": 50,
                        "frequency_penalty": 0.5,
                        "n": 1,
                        "response_format": {"type": "text"}
                    }
                    headers = {
                        "Authorization": f"Bearer {self.config['api_key']}",
                        "Content-Type": "application/json"
                    }
                    url = self.config['api_url']
                    logging.info(f"正在请求{name}API... (流式输出: {self.stream})")
                    resp = requests.post(url, json=payload, headers=headers, timeout=self.timeout)
                    resp.raise_for_status()
                    data = resp.json()
                    response = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    logging.info(f"✓ [{name}] 识别完成")
                    logging.info(f"识别结果：\n{response}")
                    return response
                else:
                    raise NotImplementedError(f"不支持的平台类型: {self.platform}")
            except Exception as e:
                if attempt == self.max_retries - 1:
                    logging.error(f"❌ [{name}] 图片分析失败: {str(e)}")
                    return None
                logging.warning(f"⚠️ [{name}] 分析失败，正在重试... ({attempt + 1}/{self.max_retries})")
                time.sleep(5)

    def analyze_images(self, image_paths):
        emoji = self.config['emoji']
        name = self.config['name']
        logging.info(f"🔍 开始批量识别，共 {len(image_paths)} 张图片，平台：{name} {emoji}")
        results = []
        total = len(image_paths)
        for idx, img_path in enumerate(image_paths, 1):
            logging.info(f"{name} {emoji} 正在识别第 {idx}/{total} 张图片...")
            result = self.analyze_image(img_path)
            results.append(result)
            if idx < total:
                time.sleep(0.5)
        logging.info(f"✅ 批量识别完成，平台：{name} {emoji}")
        return results