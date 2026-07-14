"""
基于官方OpenAI库的通用API服务
支持所有OpenAI兼容的平台：Poe, OpenAI, ChatNP等
"""
from typing import Dict, Any, Optional, List
import sys
import os
import time

try:
    import openai
except ImportError:
    print("❌ 需要安装 openai 库: pip install openai")
    sys.exit(1)

# 添加项目根目录到路径
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class OpenAIAPIService:
    """
    基于官方OpenAI库的通用API服务
    极简设计，专注核心功能
    """
    
    # 定义可选参数列表，避免重复定义
    _OPTIONAL_PARAMS = ['temperature', 'top_p', 'n', 'stop', 'presence_penalty', 
                       'frequency_penalty', 'logit_bias', 'user', 'response_format',
                       'reasoning_effort', 'extra_body']
    
    @staticmethod
    def load_config() -> Dict[str, Any]:
        """
        加载OpenAI API配置文件
        
        Returns:
            Dict: 配置字典
        """
        import json
        config_path = os.path.join(os.path.dirname(__file__), 'openaiapi.json')
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"配置文件不存在: {config_path}")
            
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        if not config:
            raise ValueError("OpenAI API配置为空")
            
        return config
    
    def __init__(self, config: Dict[str, Any]):
        """
        初始化OpenAI API服务
        
        Args:
            config: OpenAI API配置信息
        """
        # 加载所有平台配置
        self.platforms = config.get('platforms', {})
        if not self.platforms:
            raise ValueError("OpenAI API 平台配置为空")
        
        # 当前选中的平台和客户端
        self.current_platform = None
        self.current_client = None
        self.current_model = None
        
        # 全局配置
        self.concurrent = config.get('concurrent', True)
        self.max_concurrent_tasks = config.get('max_concurrent_tasks', 3)
        self.default_timeout = config.get('default_timeout', 300)
        
        print(f"✅ OpenAI API 服务初始化完成")
        print(f"   - 可用平台: {list(self.platforms.keys())}")
        print(f"   - 并发支持: {self.concurrent}")
        print(f"   - 最大并发数: {self.max_concurrent_tasks}")
        print(f"   - 默认超时: {self.default_timeout}s")
    
    def _validate_platform_exists(self, platform: str) -> None:
        """验证平台是否存在"""
        if platform not in self.platforms:
            raise ValueError(f"平台 {platform} 不存在")
    
    def _validate_client_ready(self) -> None:
        """验证客户端是否就绪"""
        if not self.current_client:
            raise ValueError("请先设置平台")
    
    def _add_token_params(self, request_params: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """
        添加Token限制参数
        
        Args:
            request_params: 请求参数字典
            kwargs: 用户传入的参数
        """
        max_tokens = kwargs.get('max_tokens')
        max_completion_tokens = kwargs.get('max_completion_tokens')
        
        if max_tokens and max_tokens > 0:
            # 优先使用用户指定的参数名
            request_params['max_tokens'] = max_tokens
        elif max_completion_tokens and max_completion_tokens > 0:
            request_params['max_completion_tokens'] = max_completion_tokens
    
    def _add_optional_params(self, request_params: Dict[str, Any], kwargs: Dict[str, Any]) -> None:
        """
        添加其他可选参数
        
        Args:
            request_params: 请求参数字典
            kwargs: 用户传入的参数
        """
        for param in self._OPTIONAL_PARAMS:
            if param in kwargs:
                request_params[param] = kwargs[param]
    
    def _build_base_request_params(self, model: str, messages: list, stream: bool, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """
        构建基础请求参数
        
        Args:
            model: 模型名称
            messages: 消息列表
            stream: 是否流式输出
            kwargs: 其他参数
            
        Returns:
            Dict: 请求参数字典
        """
        request_params = {
            'model': model,
            'messages': messages,
            'stream': stream
        }
        
        # 添加token限制参数
        self._add_token_params(request_params, kwargs)
        
        # 添加其他可选参数
        self._add_optional_params(request_params, kwargs)
        
        # Gemini 2.5 系列思考模式控制：默认关闭或降到最低
        # - 非 Pro：禁用推理（reasoning_effort="none"）
        # - Pro：无法完全禁用，降到最低（reasoning_effort="low"）
        self._apply_gemini_reasoning_controls(request_params, model)
        
        return request_params
    
    def _apply_gemini_reasoning_controls(self, request_params: Dict[str, Any], model: str) -> None:
        """
        根据模型名自动为 Gemini 2.5 系列设置思考/推理预算：
        - 非 Pro：reasoning_effort="none"
        - Pro：reasoning_effort="low"
        用户若已显式传入 reasoning_effort，则尊重用户传参。
        """
        model_name = (model or "").lower()
        # 仅对 Gemini 2.5 相关模型生效；避免与 extra_body 的 thinking_config 冲突
        if ("gemini" in model_name) and ("2.5" in model_name):
            if "reasoning_effort" not in request_params:
                if "pro" in model_name:
                    request_params["reasoning_effort"] = "low"
                else:
                    request_params["reasoning_effort"] = "none"
    
    def _print_debug_info(self, model: str, prompt_length: int, stream: bool, request_params: Dict[str, Any]) -> None:
        """
        打印调试信息
        
        Args:
            model: 模型名称
            prompt_length: 提示词长度
            stream: 是否流式输出
            request_params: 请求参数
        """
        print(f"\n🔧 OpenAI API 请求参数:")
        print(f"   平台: {self.current_platform}")
        print(f"   模型: {model}")
        print(f"   prompt长度: {prompt_length} 字符")
        print(f"   stream: {stream}")
        if 'max_tokens' in request_params:
            print(f"   max_tokens: {request_params['max_tokens']}")
        elif 'max_completion_tokens' in request_params:
            print(f"   max_completion_tokens: {request_params['max_completion_tokens']}")
        else:
            print(f"   token限制: 使用API默认值")
    
    def get_available_platforms(self) -> List[str]:
        """获取所有可用平台列表"""
        return list(self.platforms.keys())
    
    def get_platform_info(self, platform: str) -> Dict[str, Any]:
        """获取平台信息"""
        self._validate_platform_exists(platform)
        return self.platforms[platform]
    
    def get_platform_models(self, platform: str) -> List[str]:
        """获取指定平台的模型列表"""
        self._validate_platform_exists(platform)
        return self.platforms[platform].get('models', [])
    
    def set_platform(self, platform_name: str):
        """
        切换平台
        
        Args:
            platform_name: 平台名称
        """
        self._validate_platform_exists(platform_name)
        
        platform_config = self.platforms[platform_name]
        self.current_platform = platform_name
        
        # 使用官方OpenAI客户端
        self.current_client = openai.OpenAI(
            api_key=platform_config['api_key'],
            base_url=platform_config['base_url'],
            timeout=platform_config.get('timeout', self.default_timeout)
        )
        
        # 设置默认模型为第一个模型
        models = platform_config.get('models', [])
        if models:
            self.current_model = models[0]
        else:
            raise ValueError(f"平台 {platform_name} 没有配置模型")
        
        print(f"✅ 已切换到平台: {platform_config.get('name', platform_name)}")
        print(f"   - 基础URL: {platform_config['base_url']}")
        print(f"   - 可用模型: {models}")
        print(f"   - 当前模型: {self.current_model}")
    
    def set_model(self, model: str):
        """
        设置当前模型
        
        Args:
            model: 模型名称
        """
        if not self.current_platform:
            raise ValueError("请先设置平台")
        
        available_models = self.get_platform_models(self.current_platform)
        if model not in available_models:
            raise ValueError(f"模型 {model} 不在平台 {self.current_platform} 的可用模型列表中: {available_models}")
        
        self.current_model = model
        print(f"✅ 已切换到模型: {model}")
    
    def generate_text(self, prompt: str, **kwargs) -> Optional[str]:
        """
        生成文本
        
        Args:
            prompt: 输入提示
            **kwargs: 其他参数
            
        Returns:
            str: 生成的文本
        """
        self._validate_client_ready()
        
        try:
            # 构建请求参数，只传递有效参数
            model = kwargs.get('model', self.current_model)
            stream = kwargs.get('stream', True)
            messages = [{"role": "user", "content": prompt}]
            
            request_params = self._build_base_request_params(model, messages, stream, kwargs)
            
            # 添加调试信息
            self._print_debug_info(model, len(prompt), stream, request_params)
            
            # 使用官方库调用
            response = self.current_client.chat.completions.create(**request_params)
            
            # 处理响应（官方库自动处理流式和非流式）
            if stream:
                return self._handle_stream_response(response)
            else:
                return response.choices[0].message.content
                
        except Exception as e:
            print(f"\n❌ OpenAI API调用失败: {e}")
            return None
    
    def _handle_stream_response(self, response) -> str:
        """
        处理流式响应
        
        Args:
            response: OpenAI流式响应对象
            
        Returns:
            str: 完整的响应内容
        """
        full_response = ""
        print("\n生成内容: ", end="", flush=True)
        
        try:
            for chunk in response:
                # 检查choices是否存在且不为空
                if hasattr(chunk, 'choices') and chunk.choices and len(chunk.choices) > 0:
                    choice = chunk.choices[0]
                    if hasattr(choice, 'delta') and choice.delta and choice.delta.content is not None:
                        content = choice.delta.content
                        print(content, end="", flush=True)
                        full_response += content
        except Exception as e:
            print(f"\n❌ 处理流式响应时出错: {e}")
            return None
        
        print()  # 换行
        return full_response
    
    def validate_config(self) -> bool:
        """验证配置是否有效"""
        return bool(self.platforms and self.current_client)
    
    def test_platform(self, platform: str) -> bool:
        """
        测试指定平台连接
        
        Args:
            platform: 平台名称
            
        Returns:
            bool: 测试是否成功
        """
        try:
            # 保存当前状态
            original_platform = self.current_platform
            original_client = self.current_client
            original_model = self.current_model
            
            # 设置测试平台
            self.set_platform(platform)
            
            # 进行简单测试
            test_prompt = "你好，请回复'测试成功'"
            response = self.generate_text(test_prompt, stream=False, max_tokens=50)
            
            # 恢复原始状态
            if original_platform:
                self.current_platform = original_platform
                self.current_client = original_client
                self.current_model = original_model
            
            return response is not None and len(response.strip()) > 0
            
        except Exception as e:
            print(f"❌ 测试平台 {platform} 失败: {e}")
            return False


def test_openaiapi_service():
    """测试OpenAI API服务"""
    print("=" * 60)
    print("🚀 开始测试 OpenAI API 服务")
    print("=" * 60)
    
    # 使用静态方法加载配置
    try:
        config = OpenAIAPIService.load_config()
        print(f"📋 加载的配置: {list(config.get('platforms', {}).keys())}")
        
    except Exception as e:
        print(f"❌ 加载配置失败: {e}")
        return
    
    # 创建服务实例
    try:
        service = OpenAIAPIService(config)
    except Exception as e:
        print(f"❌ 创建服务实例失败: {e}")
        return
    
    # 获取所有平台
    platforms = service.get_available_platforms()
    if not platforms:
        print("❌ 没有可用的平台")
        return
    
    print(f"✅ 找到 {len(platforms)} 个平台: {platforms}")
    print()
    
    # 测试问题列表
    test_questions = [
        "你好，请自我介绍一下",
        "你是什么模型？",
        "请用一句话描述人工智能"
    ]
    
    # 测试每个平台
    for platform in platforms:
        print(f"\n{'='*50}")
        print(f"🧪 测试平台: {platform}")
        print(f"{'='*50}")
        
        try:
            # 设置平台
            service.set_platform(platform)
            
            # 获取该平台的模型
            models = service.get_platform_models(platform)
            print(f"📋 可用模型: {models}")
            
            # 测试第一个模型
            if models:
                service.set_model(models[0])
                
                # 测试第一个问题
                question = test_questions[0]
                print(f"\n📝 测试问题: {question}")
                print(f"{'-'*30}")
                
                response = service.generate_text(question, stream=True)
                if response:
                    print(f"\n✅ 平台 {platform} 测试成功")
                else:
                    print(f"\n❌ 平台 {platform} 测试失败：无响应")
            else:
                print(f"❌ 平台 {platform} 没有可用模型")
                
        except Exception as e:
            print(f"\n❌ 平台 {platform} 测试失败：{e}")
        
        print("\n" + "-" * 50)
        input("按回车键继续测试下一个平台...")
    
    print("\n" + "=" * 60)
    print("🎉 OpenAI API 服务测试完成")
    print("=" * 60)


if __name__ == "__main__":
    test_openaiapi_service() 