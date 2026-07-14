"""
并发任务执行器
"""
import concurrent.futures
import time
import threading
from typing import List, Callable, Any, Dict, Optional, Tuple
from .retry_manager import RetryManager
from tqdm import tqdm

class ConcurrentExecutor:
    def __init__(self, max_workers: int = 3, interval_seconds: int = 10, task_timeout_seconds: int = 330):
        """
        初始化并发执行器
        
        Args:
            max_workers: 最大工作线程数
            interval_seconds: 任务间隔时间(秒)
            task_timeout_seconds: 单个任务的最大等待时间（秒），默认330秒，兼容下游5分钟上限的缓冲
        """
        self.max_workers = max_workers
        self.interval_seconds = interval_seconds
        self.request_interval = 0.1
        self.last_request_time = 0
        self.task_timeout_seconds = task_timeout_seconds
        self.futures = []
        
    def delayed_process(self, process_func: Callable, content: Dict[str, Any], delay_seconds: int = 0) -> Tuple[bool, Optional[str]]:
        """
        延迟执行任务的包装函数
        
        Args:
            process_func: 原处理函数
            content: 任务内容
            delay_seconds: 延迟秒数
            
        Returns:
            Tuple[bool, Optional[str]]: 处理结果和保存路径
        """
        if delay_seconds > 0:
            print(f"\n任务 [{content.get('title', '未知标题')}] 将在 {delay_seconds} 秒后开始...")
            time.sleep(delay_seconds)
        return process_func(content)
        
    def process_batch(self, contents: List[Dict[str, Any]], task_func: Callable, manager_instance) -> int:
        """
        处理一批内容，返回成功处理的数量
        
        策略：每隔 interval_seconds 提交一个任务到线程池
        - 线程池会自动调度：有空闲worker就立即执行，没有就排队等待
        - 支持真正的并发执行
        - 边提交边收集结果，实时显示任务完成情况
        """
        results = {'successes': [], 'failures': []}
        future_to_content = {}
        results_lock = threading.Lock()  # 用于线程安全地更新结果
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # 使用tqdm创建进度条
            with tqdm(total=len(contents), desc="处理进度", position=0) as pbar:
                
                # 启动一个后台线程来实时收集已完成的任务结果
                def collect_results():
                    """后台线程：实时收集已完成的任务"""
                    completed_futures = set()
                    
                    while True:
                        # 检查是否有新完成的任务
                        with results_lock:
                            pending_futures = set(future_to_content.keys()) - completed_futures
                        
                        if not pending_futures:
                            # 如果没有待处理的future，检查是否所有任务都已提交
                            time.sleep(0.5)
                            with results_lock:
                                if len(future_to_content) == len(contents):
                                    # 所有任务都已提交，且没有待处理的，退出
                                    if len(completed_futures) == len(contents):
                                        break
                            continue
                        
                        # 等待任意一个任务完成
                        try:
                            done, _ = concurrent.futures.wait(
                                pending_futures, 
                                timeout=1,
                                return_when=concurrent.futures.FIRST_COMPLETED
                            )
                            
                            for future in done:
                                content = future_to_content[future]
                                try:
                                    # 获取任务结果
                                    result = future.result(timeout=0.1)
                                    status, data = result
                                    
                                    with results_lock:
                                        if status == 'success':
                                            results['successes'].append(result)
                                            print(f"\n✅ 任务完成: {content.get('title', '未知标题')}")
                                        else:
                                            results['failures'].append(result)
                                            print(f"\n❌ 任务失败: {content.get('title', '未知标题')} - {data.get('error', '未知错误')}")
                                        
                                        completed_futures.add(future)
                                        pbar.update(1)
                                        
                                except concurrent.futures.TimeoutError:
                                    continue
                                except Exception as e:
                                    with results_lock:
                                        print(f"\n❌ 任务执行器捕获到意外异常: {content.get('title', '未知标题')} - {e}")
                                        failure_data = ('failure', {'type': 'other_exceptions', 'error': str(e), 'content': content})
                                        results['failures'].append(failure_data)
                                        completed_futures.add(future)
                                        pbar.update(1)
                                        
                        except Exception as e:
                            print(f"\n⚠️ 结果收集线程异常: {e}")
                            time.sleep(1)
                
                # 启动结果收集线程
                collector_thread = threading.Thread(target=collect_results, daemon=True)
                collector_thread.start()
                
                # 主线程：智能提交任务（限流 + 动态调度）
                last_submit_time = time.time()
                submitted_count = 0
                
                for i, content in enumerate(contents):
                    current_time = time.time()
                    
                    # 如果不是第一个任务，检查是否需要等待
                    if i > 0 and self.interval_seconds > 0:
                        elapsed = current_time - last_submit_time
                        remaining_wait = self.interval_seconds - elapsed
                        
                        if remaining_wait > 0:
                            # 智能等待：每1秒检查一次是否有worker空闲
                            wait_start = time.time()
                            while True:
                                # 检查是否已经等够了时间
                                elapsed_wait = time.time() - wait_start
                                if elapsed_wait >= remaining_wait:
                                    break
                                
                                # 检查是否有worker空闲（判断：已提交任务数 > 已完成任务数 + max_workers）
                                with results_lock:
                                    completed_count = len(results['successes']) + len(results['failures'])
                                    active_tasks = submitted_count - completed_count
                                    
                                    # 如果活跃任务数 < max_workers，说明有空闲worker
                                    if active_tasks < self.max_workers:
                                        print(f"\n✨ 检测到空闲worker（活跃任务: {active_tasks}/{self.max_workers}），提前提交下一个任务！")
                                        break
                                
                                # 每0.5秒检查一次
                                time.sleep(min(0.5, remaining_wait - elapsed_wait))
                    
                    # 提交任务到线程池（不等待完成）
                    future = executor.submit(task_func, content)
                    with results_lock:
                        future_to_content[future] = content
                        submitted_count += 1  # 记录已提交的任务数
                    last_submit_time = time.time()
                    
                    print(f"\n📤 已提交任务 {i+1}/{len(contents)}: {content.get('title', '未知标题')}")
                    if i == 0:  # 只在第一次打印模式信息
                        if self.max_workers == 1:
                            print(f"   💡 单线程模式：任务将按提交顺序串行执行")
                        else:
                            print(f"   💡 并发模式：最多 {self.max_workers} 个任务可同时执行")
                
                print(f"\n{'='*60}")
                print(f"📋 所有任务已提交完毕，共 {len(contents)} 个任务")
                print(f"⏳ 等待剩余任务执行完成...")
                print(f"{'='*60}\n")
                
                # 等待结果收集线程完成
                collector_thread.join()

        # 返回详细结果
        return results
        
    def process_with_interval(self, func: Callable, *args, **kwargs) -> Any:
        """
        使用间隔时间处理任务
        
        Args:
            func: 要执行的函数
            *args: 位置参数
            **kwargs: 关键字参数
            
        Returns:
            Any: 函数执行结果
        """
        current_time = time.time()
        if current_time - self.last_request_time < self.request_interval:
            time.sleep(self.request_interval)
        self.last_request_time = current_time
        
        return func(*args, **kwargs)
        
    def get_success_rate(self, results: List[Dict[str, Any]]) -> float:
        """
        计算任务成功率
        
        Args:
            results: 任务执行结果列表
            
        Returns:
            float: 成功率(0-1)
        """
        if not results:
            return 0.0
            
        success_count = sum(1 for r in results if r['status'] == 'success')
        return success_count / len(results)
        
    def get_failed_tasks(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        获取失败的任务列表
        
        Args:
            results: 任务执行结果列表
            
        Returns:
            List[Dict[str, Any]]: 失败的任务列表
        """
        return [r for r in results if r['status'] == 'failed']