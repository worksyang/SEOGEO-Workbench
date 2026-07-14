#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SomeURL2MD.py - 微信文章URL转Markdown工具（命令行版）
简单识别模式 + 并发OCR处理

功能特点：
- 支持单个或多个URL输入
- 简单识别模式OCR（200字描述）
- 并发图片处理，提升效率
- 智能二维码检测和过滤
- 自动创建带时间戳的输出目录
- 无需UI界面，纯命令行操作

使用方法：
python SomeURL2MD.py
"""

import os
import sys
import asyncio
import re
from datetime import datetime
from pathlib import Path
import time # Added for overall timeout monitoring

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 导入必要的服务
from url_service import URLService
from image_service import ImageService
from md_service import MDService
from openai_concurrent_service import OpenAIConcurrentService
import sys
import os
# 添加项目根目录到 Python 路径以导入根目录的配置
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from prompts_config import SIMPLE_OCR_PROMPT

class SimpleLogger:
    """简单的控制台日志记录器"""
    
    def __init__(self, external_callback=None):
        self.messages = []
        self.external_callback = external_callback
    
    def log(self, message: str):
        """记录并显示日志，避免重复打印"""
        # 如果有外部回调，则只通过回调传递消息
        if self.external_callback:
            self.external_callback(message)
            # 记录消息供内部使用，但不打印
            timestamp = datetime.now().strftime("%H:%M:%S")
            formatted_message = f"[{timestamp}] {message}"
            self.messages.append(formatted_message)
        else:
            # 如果没有外部回调，则自己打印
            timestamp = datetime.now().strftime("%H:%M:%S")
            formatted_message = f"[{timestamp}] {message}"
            print(formatted_message)
            self.messages.append(formatted_message)
    
    def get_logs(self):
        """获取所有日志"""
        return self.messages

class SomeURL2MD:
    """URL转MD命令行工具"""
    
    def __init__(self, log_callback=None):
        """初始化工具"""
        self.logger = SimpleLogger(log_callback)
        
        # 初始化服务
        self.logger.log("🔧 正在初始化服务...")
        self.url_service = URLService()
        self.image_service = ImageService(log_callback=self.logger.log)
        self.md_service = MDService(log_callback=self.logger.log)
        
        # 初始化并发OCR服务（简单识别模式）
        self.concurrent_ocr = OpenAIConcurrentService(
            max_workers=30,  # 默认并发数
            max_retries=3    # 重试次数
        )
        
        # 自定义OCR提示词属性
        self.custom_ocr_prompt = None
        
        self.logger.log("✅ 服务初始化完成")
    

    def get_urls_from_user(self):
        """获取用户输入的URL"""
        print("\n" + "="*60)
        print("📝 请输入微信文章URL（支持多个URL，一行一个）")
        print("输入完成后，输入空行结束：")
        print("="*60)
        
        urls = []
        while True:
            try:
                url = input().strip()
                if not url:  # 空行，结束输入
                    break
                if self.url_service.validate_url(url):
                    urls.append(url)
                    print(f"✅ 已添加: {url}")
                else:
                    print(f"❌ 无效URL，请重新输入: {url}")
            except KeyboardInterrupt:
                print("\n\n操作已取消")
                return []
        
        return urls
    
    def get_output_directory(self):
        """获取输出目录"""
        print("\n" + "="*60)
        print("📁 请输入输出目录路径（直接回车使用当前目录）：")
        print("="*60)
        
        while True:
            try:
                output_dir = input().strip()
                if not output_dir:
                    output_dir = os.getcwd()
                    print(f"使用当前目录: {output_dir}")
                
                # 验证目录
                if os.path.exists(output_dir):
                    if not os.path.isdir(output_dir):
                        print("❌ 路径不是目录，请重新输入")
                        continue
                else:
                    # 尝试创建目录
                    try:
                        os.makedirs(output_dir, exist_ok=True)
                        print(f"✅ 已创建目录: {output_dir}")
                    except Exception as e:
                        print(f"❌ 无法创建目录: {str(e)}")
                        continue
                
                return output_dir
                
            except KeyboardInterrupt:
                print("\n\n操作已取消")
                return None
    
    async def process_single_url(self, url, batch_output_dir, url_idx, total_urls, enable_ocr: bool = True):
        """处理单个URL
        
        Args:
            url: 微信文章URL
            batch_output_dir: 本次批处理的输出目录
            url_idx: 当前URL序号
            total_urls: URL总数
            enable_ocr: 是否对图片执行OCR与AI解释
        """
        try:
            self.logger.log(f"\n{'='*60}")
            self.logger.log(f"📄 开始处理URL ({url_idx}/{total_urls}): {url}")
            
            # 设置整体超时监控
            start_time = time.time()
            max_processing_time = 600  # 最长处理时间10分钟
            
            # 1. URL转MD
            self.logger.log("⚙️ 正在转换URL为Markdown...")
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.url_service.convert_single_url, url, batch_output_dir
                    ),
                    timeout=120  # 2分钟超时
                )
            except asyncio.TimeoutError:
                self.logger.log(f"⏱️ URL转换超时: {url}")
                return False
            except Exception as e:
                self.logger.log(f"❌ URL转换失败: {str(e)}")
                return False
            
            if not result['success']:
                self.logger.log(f"❌ URL转换失败: {result['error']}")
                return False
            
            md_file_path = result['output_path']
            article_title = result['title']
            self.logger.log(f"✅ URL转换成功: {article_title}")
            self.logger.log(f"📁 文件路径: {md_file_path}")
            
            # 2. 读取MD文件内容
            self.logger.log("⚙️ 正在读取Markdown文件...")
            try:
                content = await asyncio.wait_for(
                    asyncio.to_thread(self.md_service.read_md_file, md_file_path),
                    timeout=30  # 30秒超时
                )
            except (asyncio.TimeoutError, Exception) as e:
                self.logger.log(f"❌ 读取Markdown文件失败: {str(e)}")
                return False
            
            # 3. 提取图片链接
            self.logger.log("🔍 正在提取图片链接...")
            try:
                image_urls = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.image_service.extract_images_from_md, content
                    ),
                    timeout=30  # 30秒超时
                )
            except (asyncio.TimeoutError, Exception) as e:
                self.logger.log(f"❌ 提取图片链接失败: {str(e)}")
                return False
            
            if not image_urls:
                self.logger.log("ℹ️ 未找到图片链接，跳过图片处理")
                return True
            
            # 如果未启用OCR，则在这里直接返回，保持原始图片标签不做AI解释
            if not enable_ocr:
                self.logger.log("ℹ️ 未启用OCR，仅保留原始图片标签，不进行AI解释")
                return True
            
            self.logger.log(f"✅ 找到 {len(image_urls)} 个图片链接")
            
            # 4. 并发下载和验证图片
            self.logger.log("⚙️ 开始并发下载和验证图片...")
            # 统一的temp目录，所有文章的图片都放在这里
            temp_dir = os.path.join(batch_output_dir, "temp")
            os.makedirs(temp_dir, exist_ok=True)
            
            # 并发下载所有图片
            async def download_and_validate_image(img_url, img_idx, total_imgs):
                """并发下载和验证单张图片"""
                try:
                    # 检查是否已经超时
                    if time.time() - start_time > max_processing_time:
                        self.logger.log(f"⏱️ 整体处理时间超时，跳过图片: {img_url}")
                        return img_url, None, "整体超时", img_idx
                    
                    self.logger.log(f"  📥 [{img_idx:02d}/{total_imgs:02d}] 开始下载图片")
                    
                    # 下载图片
                    try:
                        img_path = await asyncio.wait_for(
                            asyncio.to_thread(
                                self.image_service.download_image, img_url, temp_dir
                            ),
                            timeout=60  # 60秒超时
                        )
                    except asyncio.TimeoutError:
                        self.logger.log(f"  ⏱️ [{img_idx:02d}/{total_imgs:02d}] 下载超时")
                        return img_url, None, "下载超时", img_idx
                    
                    if not img_path:
                        self.logger.log(f"  ❌ [{img_idx:02d}/{total_imgs:02d}] 下载失败")
                        return img_url, None, "下载失败", img_idx
                    
                    # 验证图片尺寸
                    try:
                        valid = await asyncio.wait_for(
                            asyncio.to_thread(
                                self.image_service.validate_image, img_path
                            ),
                            timeout=30  # 30秒超时
                        )
                    except asyncio.TimeoutError:
                        self.logger.log(f"  ⏱️ [{img_idx:02d}/{total_imgs:02d}] 验证超时")
                        return img_url, None, "验证超时", img_idx
                    
                    if not valid:
                        self.logger.log(f"  ⛔ [{img_idx:02d}/{total_imgs:02d}] 图片过小被过滤")
                        return img_url, None, "尺寸过小", img_idx
                    
                    # 智能二维码检测
                    try:
                        is_qr = await asyncio.wait_for(
                            asyncio.to_thread(
                                self.image_service.detect_qrcode_smart, img_path, img_idx - 1, total_imgs
                            ),
                            timeout=30  # 30秒超时
                        )
                    except asyncio.TimeoutError:
                        self.logger.log(f"  ⏱️ [{img_idx:02d}/{total_imgs:02d}] 二维码检测超时")
                        return img_url, None, "二维码检测超时", img_idx
                    
                    if is_qr:
                        self.logger.log(f"  🔲 [{img_idx:02d}/{total_imgs:02d}] 检测到二维码被过滤")
                        return img_url, None, "包含二维码", img_idx
                    
                    self.logger.log(f"  ✅ [{img_idx:02d}/{total_imgs:02d}] 图片验证通过")
                    return img_url, img_path, "成功", img_idx
                    
                except Exception as e:
                    self.logger.log(f"  ❌ [{img_idx:02d}/{total_imgs:02d}] 处理异常: {str(e)}")
                    return img_url, None, f"处理出错", img_idx
            
            # 并发处理所有图片下载和验证
            download_tasks = [
                download_and_validate_image(img_url, idx, len(image_urls))
                for idx, img_url in enumerate(image_urls, 1)
            ]
            
            self.logger.log(f"🚀 启动并发下载验证 {len(image_urls)} 张图片...")
            try:
                download_results = await asyncio.wait_for(
                    asyncio.gather(*download_tasks, return_exceptions=True),
                    timeout=300  # 5分钟超时
                )
            except asyncio.TimeoutError:
                self.logger.log(f"⏱️ 图片下载验证整体超时")
                # 尽可能保存已处理的内容
                await asyncio.to_thread(
                    self.md_service.write_md_file,
                    md_file_path,
                    content
                )
                return False
            
            # 处理下载结果
            image_tasks = []
            image_map = {}
            stats = {
                "下载成功": 0,
                "下载失败": 0,
                "尺寸过小": 0,
                "包含二维码": 0,
                "处理异常": 0,
                "下载超时": 0,
                "验证超时": 0,
                "二维码检测超时": 0,
                "整体超时": 0
            }
            
            for result in download_results:
                if isinstance(result, Exception):
                    self.logger.log(f"❌ 下载任务异常: {str(result)}")
                    stats["处理异常"] += 1
                    continue
                
                img_url, img_path, status, img_idx = result
                
                if status == "成功":
                    image_tasks.append((img_path, img_url, img_idx))
                    stats["下载成功"] += 1
                elif status == "下载失败":
                    image_map[img_url] = None  # 标记删除
                    stats["下载失败"] += 1
                elif status == "尺寸过小":
                    image_map[img_url] = None  # 标记删除
                    stats["尺寸过小"] += 1
                elif status == "包含二维码":
                    image_map[img_url] = None  # 标记删除
                    stats["包含二维码"] += 1
                elif status == "下载超时":
                    image_map[img_url] = None  # 标记删除
                    stats["下载超时"] += 1
                elif status == "验证超时":
                    image_map[img_url] = None  # 标记删除
                    stats["验证超时"] += 1
                elif status == "二维码检测超时":
                    image_map[img_url] = None  # 标记删除
                    stats["二维码检测超时"] += 1
                elif status == "整体超时":
                    image_map[img_url] = None  # 标记删除
                    stats["整体超时"] += 1
                else:
                    image_map[img_url] = None  # 标记删除
                    stats["处理异常"] += 1
            
            # 显示统计结果
            self.logger.log(f"\n📊 图片预处理统计:")
            for key, value in stats.items():
                if value > 0:
                    self.logger.log(f"  • {key}: {value}")
            
            # 如果没有需要OCR的图片，则直接结束
            if not image_tasks:
                self.logger.log("ℹ️ 没有符合条件的图片需要OCR，处理完成。")
                return True
                
            # 检查是否已经超时
            if time.time() - start_time > max_processing_time * 0.8:  # 如果已经用了80%的时间
                self.logger.log(f"⏱️ 处理时间已接近超时限制，跳过OCR处理")
                # 尽可能保存已处理的内容
                await asyncio.to_thread(
                    self.md_service.write_md_file,
                    md_file_path,
                    content
                )
                return True
                
            # 5. 执行新的全并行OCR流程
            try:
                ocr_result = await asyncio.wait_for(
                    self.run_full_parallel_ocr(image_tasks),
                    timeout=max_processing_time - (time.time() - start_time)  # 剩余时间作为超时
                )
                
                # 检查是否因失败率过高而返回False
                if ocr_result is False:
                    self.logger.log(f"❌ 图片处理失败率过高，跳过文件生成")
                    return False
                
                all_ocr_results = ocr_result
                
            except asyncio.TimeoutError:
                self.logger.log(f"⏱️ OCR处理整体超时")
                # 尽可能保存已处理的内容
                await asyncio.to_thread(
                    self.md_service.write_md_file,
                    md_file_path,
                    content
                )
                return False
            
            # 6. 更新Markdown文件
            self.logger.log("📝 正在更新Markdown文件...")
            
            # 将被过滤的图片信息合并到OCR结果中
            final_ocr_results = all_ocr_results.copy()
            for filtered_url, status in image_map.items():
                if status is None:  # 被过滤的图片
                    final_ocr_results[filtered_url] = None  # 标记为删除
                    
            self.logger.log(f"📝 合并过滤结果：OCR结果 {len(all_ocr_results)} 个，被过滤图片 {len(image_map)} 个")
            
            try:
                updated_content = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.md_service.update_md_with_ocr,
                        content,
                        final_ocr_results
                    ),
                    timeout=60  # 60秒超时
                )
                
                await asyncio.wait_for(
                    asyncio.to_thread(
                        self.md_service.write_md_file,
                        md_file_path,
                        updated_content
                    ),
                    timeout=30  # 30秒超时
                )
            except asyncio.TimeoutError:
                self.logger.log(f"⏱️ 更新Markdown文件超时")
                # 尽可能保存已处理的内容
                try:
                    await asyncio.to_thread(
                        self.md_service.write_md_file,
                        md_file_path,
                        content
                    )
                except:
                    pass
                return False
            except Exception as e:
                self.logger.log(f"❌ 更新Markdown文件失败: {str(e)}")
                return False

            self.logger.log(f"✅ Markdown文件更新成功: {md_file_path}")
            return True

        except Exception as e:
            self.logger.log(f"❌ 处理URL时发生严重错误: {url} - {str(e)}")
            return False
            
    async def run_full_parallel_ocr(self, image_tasks):
        """执行新的全并行OCR处理流程"""
        
        # 准备全并行任务
        # image_tasks from [(img_path, img_url, img_idx), ...] to [(img_path, img_url), ...]
        parallel_tasks = [(path, url) for path, url, idx in image_tasks]
        
        # --- 全并行处理：每张图片独立并行执行完整流水线 ---
        self.logger.log("🚀 启动全并行处理模式：每张图片独立并行执行完整流水线")
        self.logger.log("📋 流程：AI 视觉识别（含表格复杂度判断，结果均为 AI 输出）")
        
        # 选择OCR提示词：优先使用自定义提示词，否则使用默认提示词
        selected_prompt = self.custom_ocr_prompt if self.custom_ocr_prompt else SIMPLE_OCR_PROMPT
        
        all_results = await self.concurrent_ocr.process_images_full_parallel(
            parallel_tasks,
            custom_prompt=selected_prompt,
            log_callback=self.logger.log
        )
        
        # 确保所有图片URL都在结果中，缺失的标记为None
        all_urls = {task[1] for task in parallel_tasks}
        processed_urls = set(all_results.keys())
        for url in all_urls:
            if url not in processed_urls:
                all_results[url] = None # 标记为无结果（可能下载失败或被过滤）
                self.logger.log(f"  - 图片URL缺失，标记为None: {url[:50]}...")
        
        # 记录最终结果统计
        success_count = sum(1 for result in all_results.values() if result and result.strip())
        total_count = len(all_results)
        self.logger.log(f"📊 OCR结果统计: 成功 {success_count}/{total_count} 张图片")
        
        # 检查失败率是否超过60%
        if total_count > 0:
            failure_rate = (total_count - success_count) / total_count
            if failure_rate > 0.6:
                self.logger.log(f"❌ 图片处理失败率过高: {failure_rate:.1%} (超过60%)，不生成MD文件")
                return False
        
        return all_results

    async def process_urls(self, urls, output_dir, create_timestamp_dir: bool = False, enable_ocr: bool = True):
        """批量处理多个URL
        
        Args:
            urls: 需要处理的URL列表
            output_dir: 输出目录
            create_timestamp_dir: 是否创建带时间戳的子目录
            enable_ocr: 是否对图片执行OCR与AI解释
        """
        if not urls:
            self.logger.log("❌ 未找到有效的URL")
            return
        
        # 创建带时间戳的输出目录
        if create_timestamp_dir:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            batch_output_dir = os.path.join(output_dir, f'SomeURL2MD_{timestamp}')
            os.makedirs(batch_output_dir, exist_ok=True)
            self.logger.log(f"✅ 输出目录已创建: {batch_output_dir}")
        else:
            batch_output_dir = output_dir

        # 重置失败token列表
        self.concurrent_ocr.reset_failed_tokens()
        
        self.logger.log("🚀 开始执行URL转MD处理流程")
        if enable_ocr:
            self.logger.log("🤖 当前模式：启用图片OCR与AI解释")
        else:
            self.logger.log("🖼️ 当前模式：不启用OCR，仅保留原始图片标签")
        self.logger.log(f"✅ 获取到 {len(urls)} 个URL")
        self.logger.log(f"⚙️ 当前并发数设置: {self.concurrent_ocr.max_workers} 个线程")
        
        # 处理每个URL
        total_urls = len(urls)
        success_count = 0
        
        for url_idx, url in enumerate(urls, 1):
            success = await self.process_single_url(
                url,
                batch_output_dir,
                url_idx,
                total_urls,
                enable_ocr=enable_ocr
            )
            if success:
                success_count += 1
        
        # 处理完成总结
        self.logger.log(f"\n{'='*60}")
        self.logger.log(f"🎉 所有URL处理完成！")
        self.logger.log(f"📊 处理总结:")
        self.logger.log(f"  • 总计处理: {total_urls} 个URL")
        self.logger.log(f"  • 成功处理: {success_count} 个URL")
        self.logger.log(f"  • 处理失败: {total_urls - success_count} 个URL")
        self.logger.log(f"  • 输出目录: {batch_output_dir}")
        
        # 显示token使用统计
        stats = self.concurrent_ocr.get_statistics()
        self.logger.log(f"\n🔑 Token使用统计:")
        self.logger.log(f"  • 总token数: {stats['total_tokens']}")
        self.logger.log(f"  • 可用token数: {stats['available_tokens']}")
        self.logger.log(f"  • 失败token数: {stats['failed_tokens']}")
        
        return {
            "success": True,
            "total_urls": total_urls,
            "success_count": success_count,
            "failed_count": total_urls - success_count,
            "output_dir": batch_output_dir
        }

    async def run(self):
        """运行主程序"""
        print("\n" + "="*60)
        print("🚀 SomeURL2MD - 微信文章URL转Markdown工具")
        print("💡 简单识别模式 + 并发OCR处理")
        print("="*60)
        
        try:
            # 获取URL
            urls = self.get_urls_from_user()
            if not urls:
                print("\n❌ 未输入任何有效URL，程序退出")
                return
            
            # 获取输出目录
            output_dir = self.get_output_directory()
            if not output_dir:
                print("\n❌ 未指定输出目录，程序退出")
                return
            
            # 开始处理
            print(f"\n🚀 开始处理 {len(urls)} 个URL...")
            await self.process_urls(urls, output_dir, True)
            
            print(f"\n✅ 处理完成！请查看输出目录中的文件。")
            
        except KeyboardInterrupt:
            print("\n\n❌ 用户取消操作")
        except Exception as e:
            print(f"\n❌ 程序运行出错: {str(e)}")
            import traceback
            traceback.print_exc()

    @staticmethod
    async def convert_urls_to_markdown(
        urls: list,
        output_dir: str,
        create_timestamp_dir: bool = False,
        log_callback=None,
        custom_prompt=None,
        enable_ocr: bool = True,
    ):
        """
        [静态方法] 将多个URL批量转换为Markdown文件，并处理图片。
        这是提供给外部调用的主要接口。

        Args:
            urls (list): 需要转换的URL列表
            output_dir (str): MD文件的输出目录
            create_timestamp_dir (bool): 是否在输出目录下创建带时间戳的子目录
            log_callback (function): 用于接收内部日志的回调函数
            custom_prompt (str): 自定义OCR提示词，如果不提供则使用默认提示词
            enable_ocr (bool): 是否对图片执行OCR与AI解释；为False时仅做URL转Markdown，不做AI解释
        """
        # 创建一个SomeURL2MD实例
        converter = SomeURL2MD(log_callback=log_callback)
        
        # 如果提供了自定义提示词，设置到converter中
        if custom_prompt:
            converter.custom_ocr_prompt = custom_prompt
        
        # 调用实例方法进行处理
        result = await converter.process_urls(
            urls,
            output_dir,
            create_timestamp_dir=create_timestamp_dir,
            enable_ocr=enable_ocr,
        )
        return result

def main():
    """主函数"""
    # 检查Python版本
    if sys.version_info < (3, 7):
        print("❌ 需要Python 3.7或更高版本")
        sys.exit(1)
    
    # 创建并运行工具
    tool = SomeURL2MD()
    
    # 运行异步主程序
    try:
        asyncio.run(tool.run())
    except KeyboardInterrupt:
        print("\n\n程序已退出")
    except Exception as e:
        print(f"\n❌ 程序启动失败: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main() 