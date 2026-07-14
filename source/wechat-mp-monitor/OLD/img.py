#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图片OCR补充工具

功能：
- 遍历指定文件夹下所有MD文件
- 检测MD文件中的图片链接
- 检测图片是否已有OCR信息
- 对没有OCR信息的图片进行OCR处理并补充
"""

import os
import re
import asyncio
from pathlib import Path
from typing import List, Dict, Tuple, Set
import sys

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from SomeURL2MD.image_service import ImageService
from SomeURL2MD.md_service import MDService
from SomeURL2MD.openai_concurrent_service import OpenAIConcurrentService
from prompts_config import SIMPLE_OCR_PROMPT, SIMPLE_OCR_PROMPT1, SPECIAL_OCR_ACCOUNTS

def get_ocr_prompt_by_mp_name(mp_name: str) -> str:
    """
    根据公众号名称获取对应的OCR提示词
    
    Args:
        mp_name (str): 公众号名称
    
    Returns:
        str: 对应的OCR提示词
    """
    if mp_name in SPECIAL_OCR_ACCOUNTS:
        return SIMPLE_OCR_PROMPT1
    else:
        return SIMPLE_OCR_PROMPT

def find_all_md_files(root_dir: str) -> List[str]:
    """
    递归查找所有MD文件
    
    Args:
        root_dir: 根目录路径
        
    Returns:
        MD文件路径列表
    """
    md_files = []
    for root, dirs, files in os.walk(root_dir):
        for file in files:
            if file.endswith('.md'):
                md_files.append(os.path.join(root, file))
    return md_files

def extract_image_urls(content: str) -> List[str]:
    """
    从Markdown内容中提取所有图片URL
    
    Args:
        content: Markdown内容
        
    Returns:
        图片URL列表
    """
    pattern = r'!\[.*?\]\((.*?)\)'
    return re.findall(pattern, content)

def has_ocr_comment(content: str, image_url: str) -> bool:
    """
    检查图片是否已有OCR注释
    
    Args:
        content: Markdown内容
        image_url: 图片URL
        
    Returns:
        是否已有OCR注释
    """
    # 查找图片标记后的OCR注释
    # 匹配模式：图片标记后可能有OCR注释 <!-- OCR内容：... -->
    # 需要转义URL中的特殊字符
    escaped_url = re.escape(image_url)
    pattern = rf'!\[.*?\]\({escaped_url}\)\s*\n*\s*<!--\s*OCR内容：'
    return bool(re.search(pattern, content, re.DOTALL))

def get_images_without_ocr(content: str) -> List[str]:
    """
    获取没有OCR信息的图片URL列表
    只处理包含 mmbiz.qpic.cn 的图片链接，其他链接（如 GitHub、placeholder 等）不处理
    
    Args:
        content: Markdown内容
        
    Returns:
        没有OCR信息的图片URL列表（仅包含 mmbiz.qpic.cn 的链接）
    """
    image_urls = extract_image_urls(content)
    images_without_ocr = []
    
    for img_url in image_urls:
        # 只处理包含 mmbiz.qpic.cn 的图片链接
        if "mmbiz.qpic.cn" not in img_url:
            continue
        
        if not has_ocr_comment(content, img_url):
            images_without_ocr.append(img_url)
    
    return images_without_ocr

def detect_mp_name_from_filepath(filepath: str) -> str:
    """
    从文件路径中检测公众号名称
    
    支持的文件命名格式：
    - 新格式：公众号名称_标题_YYYYMMDD_HHMMSS.md
    - 旧格式：标题_YYYYMMDD_HHMMSS_公众号名称.md
    
    Args:
        filepath: 文件路径
        
    Returns:
        公众号名称，如果无法检测则返回"普通账号"
    """
    filename = os.path.basename(filepath)
    name_without_ext = filename[:-3]  # 去掉 .md
    
    # 首先尝试新格式：公众号名称_标题_时间戳
    # 检查是否以时间戳结尾（格式：_YYYYMMDD_HHMMSS 或 _YYYYMMDDHHMMSS）
    timestamp_match = re.search(r'(_\d{8}_\d{6}|_\d{14})$', name_without_ext)
    if timestamp_match:
        # 提取时间戳之前的部分
        name_without_timestamp = name_without_ext[:-len(timestamp_match.group(1))]
        # 新格式：公众号名称_标题
        parts = name_without_timestamp.split('_', 1)  # 只分割第一个下划线
        if len(parts) == 2:
            mp_name = parts[0]
            if mp_name and mp_name != "未知公众号":
                return mp_name
    
    # 尝试旧格式：标题_时间戳_公众号名称 或 标题_时间戳
    parts = name_without_ext.split('_')
    if len(parts) >= 4:
        # 格式：标题_日期_时间_公众号（旧格式）
        mp_name = parts[-1]
        if mp_name and mp_name != "未知公众号":
            return mp_name
    elif len(parts) >= 3:
        # 可能是旧格式：标题_日期_公众号
        mp_name = parts[-1]
        if mp_name and mp_name != "未知公众号":
            return mp_name
    
    return "普通账号"

def is_error_result(result: str) -> bool:
    """
    判断OCR结果是否为错误信息
    
    Args:
        result: OCR结果字符串
        
    Returns:
        bool: 是否为错误信息
    """
    if not result or not result.strip():
        return True
    
    result_stripped = result.strip()
    
    # 检查是否为错误信息（检查特定的错误关键词）
    is_error = (
        result_stripped.startswith("## 处理错误") or
        result_stripped.startswith("## 文件错误") or
        result_stripped.startswith("## 编码错误") or
        result_stripped.startswith("## OCR识别失败") or
        result_stripped.startswith("## AI识别失败") or
        result_stripped.startswith("## 程序执行错误") or
        result_stripped.startswith("## 处理异常") or
        result_stripped.startswith("## 转换失败") or
        "处理图片失败" in result_stripped or
        "API调用失败" in result_stripped or
        "流式响应内容为空" in result_stripped
    )
    
    return is_error

def filter_valid_ocr_results(ocr_results: Dict[str, str], log_callback=None) -> Dict[str, str]:
    """
    过滤掉错误结果，只保留有效的OCR结果
    
    Args:
        ocr_results: OCR结果字典 {url: result}
        log_callback: 日志回调函数
        
    Returns:
        Dict[str, str]: 过滤后的有效OCR结果
    """
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)
    
    filtered_results = {}
    for url, result in ocr_results.items():
        if result is None:
            # None值不写入MD（通常是API错误）
            continue
        
        if is_error_result(result):
            # 错误信息不写入MD
            log(f"    ⚠️ 跳过错误结果: {url[:50]}... (不写入MD文件)")
            continue
        
        # 只有有效的OCR结果才写入MD
        filtered_results[url] = result
    
    return filtered_results

async def prepare_image_tasks(
    image_urls: List[str],
    image_service: ImageService,
    temp_dir: str,
    log_callback=None
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    下载并验证图片，准备OCR任务列表
    
    Args:
        image_urls: 图片URL列表
        image_service: 图片服务
        temp_dir: 临时目录
        log_callback: 日志回调函数
        
    Returns:
        Tuple[List[Tuple[str, str]], List[str]]: (image_tasks, failed_urls)
        image_tasks: [(image_path, image_url), ...]
        failed_urls: 失败的URL列表
    """
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)
    
    image_tasks = []
    failed_urls = []
    
    for img_url in image_urls:
        try:
            # 下载图片
            img_path = await asyncio.to_thread(
                image_service.download_image,
                img_url,
                temp_dir
            )
            
            if not img_path:
                log(f"    ❌ 下载失败: {img_url[:50]}...")
                failed_urls.append(img_url)
                continue
            
            # 验证图片尺寸
            valid = await asyncio.to_thread(
                image_service.validate_image,
                img_path
            )
            
            if not valid:
                log(f"    ⛔ 图片尺寸过小: {img_url[:50]}...")
                failed_urls.append(img_url)
                # 删除无效图片
                try:
                    os.remove(img_path)
                except:
                    pass
                continue
            
            # 检测二维码
            is_qr = await asyncio.to_thread(
                image_service.detect_qrcode_smart,
                img_path,
                0,  # 索引
                len(image_urls)  # 总数
            )
            
            if is_qr:
                log(f"    🔲 检测到二维码: {img_url[:50]}...")
                failed_urls.append(img_url)
                # 删除二维码图片
                try:
                    os.remove(img_path)
                except:
                    pass
                continue
            
            # 添加到OCR任务列表
            image_tasks.append((img_path, img_url))
            log(f"    ✅ 图片验证通过: {img_url[:50]}...")
            
        except Exception as e:
            log(f"    ❌ 处理图片时出错 {img_url[:50]}...: {str(e)}")
            failed_urls.append(img_url)
    
    return image_tasks, failed_urls

async def process_images_with_service(
    image_tasks: List[Tuple[str, str]],
    ocr_service,
    ocr_prompt: str,
    log_callback=None
) -> Dict[str, str]:
    """
    使用指定的OCR服务处理图片
    
    Args:
        image_tasks: 图片任务列表 [(image_path, image_url), ...]
        ocr_service: OCR服务实例
        ocr_prompt: OCR提示词
        log_callback: 日志回调函数
        enable_thinking: 是否启用思考模式（仅对精确模型有效）
            - True: 输出推理过程（reasoning_content），不输出主内容
            - False: 输出主内容（content），不输出推理过程
        
    Returns:
        Dict[str, str]: OCR结果字典 {image_url: result}
    """
    # 仅使用并发模型处理图片
    ocr_results = await ocr_service.process_images_full_parallel(
        image_tasks,
        custom_prompt=ocr_prompt,
        log_callback=log_callback or (lambda msg: None)
    )
    
    return ocr_results

async def process_single_md_file(
    md_file_path: str,
    image_service: ImageService,
    md_service: MDService,
    ocr_service,  # 仅使用 OpenAIConcurrentService
    log_callback=None
) -> Tuple[bool, int, int]:
    """
    处理单个MD文件，补充缺失的OCR信息
    
    Args:
        md_file_path: MD文件路径
        image_service: 图片服务
        md_service: Markdown服务
        ocr_service: OCR服务
        log_callback: 日志回调函数
        
    Returns:
        (是否成功, 处理的图片数, 失败的图片数)
    """
    def log(msg):
        if log_callback:
            log_callback(msg)
        else:
            print(msg)
    
    try:
        log(f"\n📄 处理文件: {os.path.basename(md_file_path)}")
        
        # 读取MD文件内容
        content = md_service.read_md_file(md_file_path)
        
        # 获取没有OCR信息的图片
        images_without_ocr = get_images_without_ocr(content)
        
        if not images_without_ocr:
            log(f"  ✅ 所有图片已有OCR信息，跳过")
            return True, 0, 0
        
        log(f"  🔍 发现 {len(images_without_ocr)} 张图片需要OCR处理")
        
        # 检测公众号名称以选择合适的OCR提示词（根据账号类型选择提示词）
        mp_name = detect_mp_name_from_filepath(md_file_path)
        ocr_prompt = get_ocr_prompt_by_mp_name(mp_name)
        
        # 显示使用的提示词信息
        if mp_name in SPECIAL_OCR_ACCOUNTS:
            log(f"  📝 检测到特殊账号: {mp_name}，使用高精度提示词 (SIMPLE_OCR_PROMPT1)")
        else:
            log(f"  📝 检测到普通账号: {mp_name}，使用标准提示词 (SIMPLE_OCR_PROMPT)")
        
        # 创建临时目录用于下载图片
        temp_dir = os.path.join(os.path.dirname(md_file_path), "temp_ocr")
        os.makedirs(temp_dir, exist_ok=True)
        
        # 下载并验证图片
        image_tasks, failed_urls = await prepare_image_tasks(
            images_without_ocr,
            image_service,
            temp_dir,
            log
        )
        
        if not image_tasks:
            log(f"  ⚠️ 没有符合条件的图片需要OCR处理")
            # 清理临时目录
            try:
                os.rmdir(temp_dir)
            except:
                pass
            return True, 0, len(failed_urls)
        
        # 执行OCR处理（仅使用并发模型）
        log(f"  🚀 开始OCR处理 {len(image_tasks)} 张图片...")
        ocr_results = await process_images_with_service(
            image_tasks,
            ocr_service,
            ocr_prompt,
            log
        )
        
        # 清理临时文件
        for img_path, _ in image_tasks:
            try:
                os.remove(img_path)
            except:
                pass
        try:
            os.rmdir(temp_dir)
        except:
            pass
        
        # 过滤掉错误结果（不再使用精确模型兜底）
        filtered_ocr_results = filter_valid_ocr_results(ocr_results, log)
        
        # 更新MD文件（只写入有效的OCR结果）
        log(f"  📝 更新Markdown文件...")
        if filtered_ocr_results:
            updated_content = md_service.update_md_with_ocr(content, filtered_ocr_results)
            md_service.write_md_file(md_file_path, updated_content)
        else:
            log(f"  ℹ️ 没有有效的OCR结果，跳过MD文件更新")
        
        # 统计结果（只统计有效的OCR结果）
        success_count = len(filtered_ocr_results)
        log(f"  ✅ OCR处理完成: 成功 {success_count}/{len(image_tasks)} 张")
        
        return True, success_count, len(failed_urls) + (len(image_tasks) - success_count)
        
    except Exception as e:
        log(f"  ❌ 处理文件时出错: {str(e)}")
        import traceback
        traceback.print_exc()
        return False, 0, 0

async def process_folder(folder_path: str, ocr_service):
    """
    处理文件夹下所有MD文件
    
    Args:
        folder_path: 文件夹路径
        ocr_service: OCR服务实例（仅使用 OpenAIConcurrentService）
    """
    print("="*60)
    print("📁 图片OCR补充工具")
    print("="*60)
    
    if not os.path.exists(folder_path):
        print(f"❌ 文件夹不存在: {folder_path}")
        return
    
    if not os.path.isdir(folder_path):
        print(f"❌ 路径不是文件夹: {folder_path}")
        return
    
    # 查找所有MD文件
    print(f"\n🔍 正在扫描文件夹: {folder_path}")
    md_files = find_all_md_files(folder_path)
    print(f"✅ 找到 {len(md_files)} 个MD文件")
    
    if not md_files:
        print("⚠️ 未找到任何MD文件")
        return
    
    # 初始化服务
    print("\n🔧 正在初始化服务...")
    image_service = ImageService()
    md_service = MDService()
    print("✅ 服务初始化完成")
    
    # 处理每个MD文件
    total_processed = 0
    total_success = 0
    total_failed = 0
    total_images_processed = 0
    total_images_failed = 0
    
    for idx, md_file in enumerate(md_files, 1):
        print(f"\n[{idx}/{len(md_files)}] 处理文件...")
        success, images_processed, images_failed = await process_single_md_file(
            md_file,
            image_service,
            md_service,
            ocr_service
        )
        
        total_processed += 1
        if success:
            total_success += 1
        else:
            total_failed += 1
        
        total_images_processed += images_processed
        total_images_failed += images_failed
    
    # 输出统计结果
    print("\n" + "="*60)
    print("🎉 处理完成")
    print("="*60)
    print(f"📊 文件统计:")
    print(f"  - 总计处理: {total_processed} 个文件")
    print(f"  - 成功处理: {total_success} 个文件")
    print(f"  - 处理失败: {total_failed} 个文件")
    print(f"\n📊 图片统计:")
    print(f"  - 成功OCR: {total_images_processed} 张")
    print(f"  - OCR失败: {total_images_failed} 张")
    print("="*60)

def main():
    """主函数"""
    print("\n" + "="*60)
    print("📁 图片OCR补充工具")
    print("="*60)
    
    try:
        # 使用串行模式（max_workers=1）
        print("\n🔧 正在初始化OCR服务（串行模式）...")
        ocr_service = OpenAIConcurrentService(max_workers=1, max_retries=3)
        # 动态读取服务初始化后的实际模型名称，避免写死
        print(f"✅ 已选择: {getattr(ocr_service, 'model', '未知模型')} - 串行处理")
        
        print("\n请输入要处理的文件夹路径:")
        print("(直接回车使用当前目录)")
        
        folder_path = input().strip()
        if not folder_path:
            folder_path = os.getcwd()
        
        # 运行异步处理
        asyncio.run(process_folder(folder_path, ocr_service))
        
    except KeyboardInterrupt:
        print("\n\n❌ 用户取消操作")
    except Exception as e:
        print(f"\n❌ 程序运行出错: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()

