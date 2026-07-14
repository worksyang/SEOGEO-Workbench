"""
重试管理器模块
"""
import time
from typing import Callable, Any, Optional
from functools import wraps

class RetryManager:
    def __init__(self, max_retries: int = 3, delay: float = 1.0, backoff: float = 2.0):
        """
        初始化重试管理器
        
        Args:
            max_retries: 最大重试次数
            delay: 初始延迟时间(秒)
            backoff: 延迟时间的增长倍数
        """
        self.max_retries = max_retries
        self.delay = delay
        self.backoff = backoff
        
    def execute_with_retry(self, func: Callable, *args, **kwargs) -> Any:
        """
        使用重试机制执行函数
        
        Args:
            func: 要执行的函数
            *args: 位置参数
            **kwargs: 关键字参数
            
        Returns:
            Any: 函数执行结果
            
        Raises:
            Exception: 重试次数用尽后仍然失败
        """
        last_exception = None
        current_delay = self.delay
        
        for attempt in range(self.max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_exception = e
                if attempt < self.max_retries - 1:
                    time.sleep(current_delay)
                    current_delay *= self.backoff
                    print(f"重试第 {attempt + 1} 次失败: {str(e)}")
                    
        raise last_exception
        
    def retry_decorator(self, func: Callable) -> Callable:
        """
        重试装饰器
        
        Args:
            func: 要装饰的函数
            
        Returns:
            Callable: 装饰后的函数
        """
        @wraps(func)
        def wrapper(*args, **kwargs):
            return self.execute_with_retry(func, *args, **kwargs)
        return wrapper 