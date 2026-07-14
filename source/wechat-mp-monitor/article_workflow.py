#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🤖 文章处理工作流主程序

- 本模块开发目的：实现对采集到的文章进行自动化筛选、分类、去重、AI智能判别与Markdown格式转换，最终为后续内容发布或归档提供高质量的文章数据。

📥 文章采集与加载
- 🌐 支持从WeRSS等平台批量拉取近N天的文章数据
- 🗃️ 加载本地已拒绝（不合格）文章记录，避免重复处理

🔍 文章筛选与去重
- 🧹 过滤不符合要求的文章类型（如仅保留“搜索”“推荐”类）
- 🧠 利用thefuzz算法对标题/内容进行相似度去重，防止重复收录
- 🚫 跳过已被拒绝的文章

🤖 AI智能判别
- 🧑‍💻 批量调用AIClassifier对文章内容进行智能分类、质量判别
- 🏷️ 标记不合格或低质量文章，自动加入拒绝列表

📝 Markdown格式转换
- 🔄 调用SomeURL2MD模块，将合格文章内容转为标准Markdown格式
- 📂 自动创建输出目录，按需保存转换结果

📊 结果记录与统计
- ✅ 记录已处理、已拒绝、已输出的文章信息
- 📈 支持生成处理统计报告，便于后续分析

🔧 配置与依赖
- ⚙️ 支持灵活配置采集天数、相似度阈值、批量处理大小等参数
- 🔗 集成WeRSSClient、AIClassifier、SomeURL2MD等外部依赖
- 🛡️ 敏感信息支持环境变量或配置文件管理

🛠️ 错误处理与日志
- 📝 详细日志输出，便于开发者追踪处理流程
- 🚨 异常捕获与友好提示，提升系统健壮性
"""

import os
import re
import csv
import asyncio
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple

from thefuzz import fuzz

from werss_client import WeRSSClient
from SomeURL2MD.ai_classifier import AIClassifier
from SomeURL2MD.SomeURL2MD import SomeURL2MD
from SomeURL2MD.url_service import URLService

# --- 配置 ---
# 从主项目导入登录信息，或者在这里直接定义
# 注意：在实际使用中，建议将这些敏感信息配置在环境变量或安全的配置文件中
try:
    from main import BASE_URL, USERNAME, PASSWORD
except ImportError:
    BASE_URL = "http://192.168.31.89:8001"
    USERNAME = "admin"
    PASSWORD = "admin@123"

OUTPUT_DIR = "/Users/works14/Documents/output_md"
DAYS_TO_FETCH = 15
SIMILARITY_THRESHOLD = 95
# V6.0 新类型系统：所有允许的类型（文件夹名称）
ALLOWED_TYPES = [
    # 优先级A：热门产品（统一分类）
    "热门产品",
    # 优先级B：功能性与通用分类
    "z产品对比",
    "z香港vs内地",
    "港险优惠",
    "美联储降息",
    "保司盘点",
    "什么是香港保险",
    "香港储蓄险",
    # 优先级C：兜底分类
    "z非热门产品",
    "⛔ 看文章再确定",  # 保存到"其他"文件夹
]
REJECTED_CSV_FILE = "rejected_articles.csv"
AI_BATCH_SIZE = 50  # AI处理的批量大小

# 特殊账号配置：这些账号的文章不参与AI分类，直接保存到"特殊账号"文件夹
SPECIAL_ACCOUNTS = [
    "港险优选专家",
    "心水保",
    # 可以在这里添加更多特殊账号
]

def parse_article_datetime(article: Dict[str, Any]) -> datetime:
    publish_time = article.get("publish_time") or article.get("created_at") or article.get("update_time")
    if not publish_time:
        return datetime.now()

    try:
        if isinstance(publish_time, (int, float)):
            return datetime.fromtimestamp(publish_time)

        raw = str(publish_time).strip()
        if raw.isdigit():
            timestamp = int(raw)
            if timestamp > 10_000_000_000:
                timestamp = timestamp / 1000
            return datetime.fromtimestamp(timestamp)

        normalized = raw.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return datetime.strptime(raw[:10], "%Y-%m-%d")
    except Exception:
        return datetime.now()


def safe_filename_part(value: str, max_length: int = 120) -> str:
    clean_value = re.sub(r'[\\/:*?"<>|]', "_", str(value or "").strip())
    clean_value = re.sub(r"\s+", " ", clean_value).strip()
    clean_value = clean_value.strip(" ._")
    if not clean_value:
        clean_value = "未命名"
    return clean_value[:max_length]


def get_article_mp_name(article: Dict[str, Any], client: WeRSSClient) -> str:
    mp_id = article.get("mp_id") or article.get("account_id")
    if mp_id:
        try:
            mp_info = client.get_mp_info(mp_id)
            if mp_info:
                return mp_info.get("mp_name") or mp_info.get("name") or "未知公众号"
        except Exception:
            pass
    return article.get("mp_name") or article.get("account_name") or "未知公众号"


def build_obsidian_article_filename(article: Dict[str, Any], client: WeRSSClient) -> str:
    article_time = parse_article_datetime(article)
    date_part = article_time.strftime("%y%m%d")
    time_part = article_time.strftime("%H%M%S")
    mp_name = safe_filename_part(get_article_mp_name(article, client), max_length=40)
    title = safe_filename_part(article.get("title") or "无标题", max_length=120)
    return f"{date_part}_{mp_name}_{title}_{time_part}"

def separate_special_accounts(
    articles: List[Dict[str, Any]], 
    special_accounts: List[str], 
    client: WeRSSClient
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    将特殊账号的文章从文章列表中分离出来
    
    Args:
        articles: 文章列表
        special_accounts: 特殊账号名称列表
        client: WeRSS客户端（用于获取公众号信息）
        
    Returns:
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]: (特殊账号文章, 普通账号文章)
    """
    print(f"\n🔍 [Workflow] 开始分离特殊账号文章...")
    print(f"  - 特殊账号列表: {', '.join(special_accounts)}")
    
    special_articles = []
    normal_articles = []
    
    for article in articles:
        # 获取公众号名称
        mp_id = article.get('mp_id') or article.get('account_id')
        mp_name = "未知公众号"
        
        if mp_id:
            try:
                mp_info = client.get_mp_info(mp_id)
                if mp_info:
                    mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
            except Exception as e:
                print(f"  ⚠️ 获取公众号信息失败 (mp_id={mp_id}): {e}")
        
        # 检查是否为特殊账号
        if mp_name in special_accounts:
            # 为特殊账号文章添加分类类型标记
            article['classified_type'] = '特殊账号'
            special_articles.append(article)
        else:
            normal_articles.append(article)
    
    print(f"  - ✅ 分离完成：特殊账号 {len(special_articles)} 篇，普通账号 {len(normal_articles)} 篇")
    
    if special_articles:
        print(f"\n📋 [特殊账号文章列表]")
        for article in special_articles:
            mp_id = article.get('mp_id') or article.get('account_id')
            mp_name = "未知公众号"
            if mp_id:
                try:
                    mp_info = client.get_mp_info(mp_id)
                    if mp_info:
                        mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
                except:
                    pass
            print(f"  - [ {mp_name} ] {article.get('title', '无标题')}")
    
    return special_articles, normal_articles

def get_ocr_prompt_by_mp_name(mp_name: str) -> str:
    """
    根据公众号名称获取对应的OCR提示词
    
    Args:
        mp_name (str): 公众号名称
    
    Returns:
        str: 对应的OCR提示词
    """
    from prompts_config import SIMPLE_OCR_PROMPT, SIMPLE_OCR_PROMPT1, SPECIAL_OCR_ACCOUNTS
    
    if mp_name in SPECIAL_OCR_ACCOUNTS:
        return SIMPLE_OCR_PROMPT1
    else:
        return SIMPLE_OCR_PROMPT

def load_rejected_articles(csv_file: str) -> set:
    """加载已被拒绝的文章URL列表"""
    print("\n🔍 [Workflow] 加载已拒绝文章记录...")
    
    rejected_urls = set()
    
    if not os.path.exists(csv_file):
        print("  - 拒绝记录文件不存在，所有文章都是新的。")
        return rejected_urls
    
    try:
        with open(csv_file, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                url = row.get('url', '').strip()
                if url:
                    rejected_urls.add(url)
        
        print(f"  - 加载完成，发现 {len(rejected_urls)} 篇已拒绝的文章。")
    except Exception as e:
        print(f"  - 加载拒绝记录失败: {e}")
    
    return rejected_urls

def save_rejected_articles(rejected_articles: List[Dict[str, Any]], csv_file: str, client) -> int:
    """保存被拒绝的文章到CSV文件"""
    
    # 始终读取现有数据，以获取准确的总数
    existing_data = []
    if os.path.exists(csv_file):
        try:
            with open(csv_file, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                existing_data = list(reader)
        except Exception as e:
            print(f"  - 读取现有拒绝记录失败: {e}")

    if not rejected_articles:
        return len(existing_data)

    print(f"\n📝 [Workflow] 保存 {len(rejected_articles)} 篇新拒绝的文章到记录...")
    
    # CSV列定义
    columns = ['title', 'url', 'mp_name', 'rejected_type', 'rejected_reason', 'rejected_time']
    
    # 准备数据
    csv_data = []
    for item in rejected_articles:
        # 获取公众号名称
        mp_id = item.get('original_article', {}).get('mp_id')
        mp_name = "未知公众号"
        if mp_id:
            try:
                mp_info = client.get_mp_info(mp_id)
                if mp_info:
                    mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
            except:
                pass
        
        csv_row = {
            'title': item.get('title', ''),
            'url': item.get('original_article', {}).get('url', ''),
            'mp_name': mp_name,
            'rejected_type': item.get('type', ''),
            'rejected_reason': item.get('why', ''),
            'rejected_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        csv_data.append(csv_row)
    
    # 合并新旧数据
    all_data = existing_data + csv_data
    
    # 写入CSV文件
    try:
        with open(csv_file, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(all_data)
        return len(all_data)
    except Exception as e:
        print(f"  - ❌ 保存拒绝记录失败: {e}")
        return len(existing_data)

def scan_existing_articles(output_dir: str) -> set:
    """扫描现有的MD文件，提取已处理过的文章标题"""
    print("\n🔍 [Workflow] 扫描现有MD文件，检测已处理的文章...")
    
    existing_titles = set()
    
    if not os.path.exists(output_dir):
        print("  - 输出目录不存在，所有文章都是新的。")
        return existing_titles
    
    # V6.0：遍历所有类型文件夹（文件直接存放在类型文件夹下，不再有日期子文件夹）
    for item in os.listdir(output_dir):
        item_path = os.path.join(output_dir, item)
        if not os.path.isdir(item_path):
            continue
        
        # 检查是否是日期文件夹（旧格式：8位数字格式YYYYMMDD）
        if re.match(r'^\d{8}$', item):
            # 这是日期文件夹（旧格式），直接扫描
            scan_md_files_in_folder(item_path, existing_titles)
        else:
            # 这是类型文件夹（新格式），直接扫描类型文件夹下的MD文件
            try:
                scan_md_files_in_folder(item_path, existing_titles)
            except (PermissionError, OSError):
                # 如果无法访问，跳过
                pass
    
    print(f"  - 扫描完成，发现 {len(existing_titles)} 篇已处理的文章。")
    return existing_titles

def scan_md_files_in_folder(folder_path: str, existing_titles: set):
    """扫描指定文件夹中的MD文件并提取标题"""
    try:
        for filename in os.listdir(folder_path):
            if not filename.endswith('.md'):
                continue
                
            file_path = os.path.join(folder_path, filename)
            try:
                # 从文件内容中提取标题（第一行的# 标题）
                with open(file_path, 'r', encoding='utf-8') as f:
                    first_line = f.readline().strip()
                    if first_line.startswith('# '):
                        title = first_line[2:].strip()  # 去掉 "# "
                        existing_titles.add(title.lower())  # 转换为小写便于匹配
            except Exception as e:
                # 如果读取失败，尝试从文件名提取标题
                try:
                    # 文件名格式：标题_YYYYMMDD_HHMMSS_公众号名称.md
                    # 需要去掉 .md 后缀，然后去掉最后三个下划线部分（日期、时间、公众号）
                    name_without_ext = filename[:-3]  # 去掉 .md
                    parts = name_without_ext.split('_')
                    if len(parts) >= 4:
                        # 去掉最后三部分（日期、时间、公众号），保留标题部分
                        title_parts = parts[:-3]
                        title_part = '_'.join(title_parts)
                    else:
                        # 如果分割后少于4部分，可能是旧格式，尝试去掉最后2部分
                        if len(parts) >= 3:
                            title_parts = parts[:-2]
                            title_part = '_'.join(title_parts)
                        else:
                            # 格式不符合预期，直接使用整个文件名
                            title_part = name_without_ext
                    existing_titles.add(title_part.lower())
                except:
                    continue
    except Exception as e:
        # 忽略无法访问的文件夹
        pass

def filter_existing_articles(
    articles: List[Dict[str, Any]], 
    existing_titles: set,
    rejected_urls: set
) -> Tuple[List[Dict[str, Any]], int]:
    """过滤掉已经存在MD文件或已被拒绝的文章"""
    print("\n🔍 [Workflow] 过滤已存在和已拒绝的文章...")
    
    new_articles = []
    skipped_by_md = 0
    skipped_by_rejected = 0
    
    for article in articles:
        title = article.get('title', '').lower()
        url = article.get('url') or article.get('link', '')
        
        # 检查是否已有MD文件（使用模糊匹配）
        is_existing_md = False
        for existing_title in existing_titles:
            similarity = fuzz.token_sort_ratio(title, existing_title)
            if similarity >= SIMILARITY_THRESHOLD:
                is_existing_md = True
                break
        
        # 检查是否已被拒绝
        is_rejected = url in rejected_urls
        
        if is_existing_md:
            skipped_by_md += 1
        elif is_rejected:
            skipped_by_rejected += 1
        else:
            new_articles.append(article)
    
    print(f"  - 过滤完成：跳过 {skipped_by_md} 篇已存在MD，跳过 {skipped_by_rejected} 篇已拒绝，保留 {len(new_articles)} 篇新文章。")
    return new_articles, skipped_by_md + skipped_by_rejected

def get_recent_articles(client: WeRSSClient, days: int) -> List[Dict[str, Any]]:
    """获取最近N天的所有文章 - 最终优化版"""
    print(f"\n🚀 [Workflow] 开始获取最近 {days} 天的文章 (高效模式)...")
    
    # 1. 获取所有公众号
    print("  - 正在获取所有公众号列表...")
    try:
        mps = client.get_all_mps()
        if not mps:
            print("  - ⚠️ 未找到任何已配置的公众号。")
            return []
        print(f"  - ✅ 找到 {len(mps)} 个公众号。") 
    except Exception as e:
        print(f"  - ❌ 获取公众号列表失败: {e}")
        return []

    # 2. 遍历每个公众号，获取其所有文章
    all_articles_dump = []
    print("  - 正在遍历公众号以获取所有文章...")
    for i, mp in enumerate(mps, 1):
        mp_id = mp.get("id")
        mp_name = mp.get("mp_name", "未知公众号")
        if not mp_id:
            continue
        try:
            # print(f"    - [{i}/{len(mps)}] 获取 '{mp_name}' 的文章...")
            articles_from_mp = client.get_mp_articles(mp_id)
            if articles_from_mp:
                all_articles_dump.extend(articles_from_mp)
        except Exception as e:
            print(f"    - ❌ 获取 '{mp_name}' 的文章失败: {e}")
    
    print(f"  - ✅ 所有公众号遍历完成，共获取 {len(all_articles_dump)} 篇文章，开始按日期筛选。")

    # 3. 在内存中按日期筛选
    recent_articles = []
    seen_urls = set()
    today = datetime.now().date()
    
    date_range = [today - timedelta(days=i) for i in range(days)]
    date_str_range = {date.strftime("%Y-%m-%d") for date in date_range}
    
    for article in all_articles_dump:
        url = article.get("url") or article.get("link")
        if not url or url in seen_urls:
            continue
            
        publish_time = article.get("publish_time")
        if not publish_time:
            continue
        
        try:
            if isinstance(publish_time, (int, float)):
                article_date_str = datetime.fromtimestamp(publish_time).strftime("%Y-%m-%d")
            else:
                article_date_str = str(publish_time)[:10]
            
            if article_date_str in date_str_range:
                recent_articles.append(article)
                seen_urls.add(url)
        except (ValueError, TypeError):
            continue
            
    print(f"✅ [Workflow] 获取完成，最近 {days} 天总共获得 {len(recent_articles)} 篇不重复的文章。")
    return recent_articles

def match_and_filter_articles(
    original_articles: List[Dict[str, Any]],
    classified_results: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    将AI分类结果与原始文章进行模糊匹配，并根据类型进行筛选，增加详细日志。
    
    Returns:
        Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]: (接受的文章, 被拒绝的文章)
    """
    print("\n🚀 [Workflow] 开始匹配和筛选文章...")
    print(f"  - 收到 {len(original_articles)} 篇原始文章和 {len(classified_results)} 条AI分类结果。")
    
    accepted_articles = []
    rejected_by_type = []
    rejected_by_match_fail = []
    
    # 为了高效匹配，创建一个原始标题到文章的映射
    # 使用 .lower() 来减少大小写干扰
    original_title_map = {article['title'].lower(): article for article in original_articles}
    classified_titles_processed = set()

    for classified in classified_results:
        classified_title = classified.get("title")
        classified_type = classified.get("type")
        
        if not classified_title or not classified_type:
            continue
        
        # 记录已处理的AI分类标题，用于后续查找匹配失败项
        classified_titles_processed.add(classified_title.lower())

        # 1. 根据类型筛选（V6.0：精确匹配或包含检查）
        # 检查是否为拒绝类型
        if classified_type == "🙅‍ 不选" or "🙅‍ 不选" in classified_type:
            # 尝试找到对应的原始文章
            best_match_original_title = None
            best_match_score = 0
            for original_title_lower in original_title_map.keys():
                score = fuzz.token_sort_ratio(classified_title.lower(), original_title_lower)
                if score > best_match_score:
                    best_match_score = score
                    best_match_original_title = original_title_lower
            
            # 将拒绝信息与原始文章关联
            rejected_item = classified.copy()
            if best_match_score >= SIMILARITY_THRESHOLD and best_match_original_title:
                rejected_item['original_article'] = original_title_map[best_match_original_title]
            else:
                # 如果匹配度不够，创建一个虚拟的原始文章记录
                rejected_item['original_article'] = {
                    'title': classified_title,
                    'url': '',  # 无法匹配到具体URL
                    'mp_id': None
                }
            
            rejected_by_type.append(rejected_item)
            continue
        
        # 检查是否为允许的类型（精确匹配或包含检查）
        is_allowed = False
        for allowed_type in ALLOWED_TYPES:
            if classified_type == allowed_type or allowed_type in classified_type:
                is_allowed = True
                break
        
        if not is_allowed:
            # 尝试找到对应的原始文章
            best_match_original_title = None
            best_match_score = 0
            for original_title_lower in original_title_map.keys():
                score = fuzz.token_sort_ratio(classified_title.lower(), original_title_lower)
                if score > best_match_score:
                    best_match_score = score
                    best_match_original_title = original_title_lower
            
            # 将拒绝信息与原始文章关联
            rejected_item = classified.copy()
            if best_match_score >= SIMILARITY_THRESHOLD and best_match_original_title:
                rejected_item['original_article'] = original_title_map[best_match_original_title]
            else:
                # 如果匹配度不够，创建一个虚拟的原始文章记录
                rejected_item['original_article'] = {
                    'title': classified_title,
                    'url': '',  # 无法匹配到具体URL
                    'mp_id': None
                }
            
            rejected_by_type.append(rejected_item)
            continue

        # 2. 模糊匹配标题
        best_match_score = 0
        best_match_original_title = None

        # 使用 token_sort_ratio 忽略单词顺序差异
        for original_title_lower in original_title_map.keys():
            score = fuzz.token_sort_ratio(classified_title.lower(), original_title_lower)
            if score > best_match_score:
                best_match_score = score
                best_match_original_title = original_title_lower
        
        if best_match_score >= SIMILARITY_THRESHOLD and best_match_original_title:
            matched_article = original_title_map[best_match_original_title]
            matched_article['classified_type'] = classified_type
            accepted_articles.append(matched_article)
            
            # 从映射中移除已匹配项，避免重复匹配
            del original_title_map[best_match_original_title]
        else:
            # 类型符合，但匹配分数不够
            rejected_by_match_fail.append({
                "classified_title": classified_title,
                "classified_type": classified_type,
                "best_match_attempt": best_match_original_title,
                "score": best_match_score
            })

    # --- 打印处理摘要 ---
    print("\n" + "="*25 + " 处理摘要 " + "="*25)
    
    # 1. 成功入选的文章
    print(f"\n✅ 成功入选: {len(accepted_articles)} 篇")
    if accepted_articles:
        for article in accepted_articles:
            print(f"  - [ {article['classified_type']} ] {article['title']}")
    
    # 2. 因类型被拒绝的文章
    print(f"\n❌ 因类型被拒绝: {len(rejected_by_type)} 篇")
    if rejected_by_type:
        for item in rejected_by_type:
            print(f"  - [ {item['type']} ] {item['title']}")
            
    # 3. 因匹配失败被拒绝的文章
    print(f"\n⚠️ 因匹配失败被拒绝: {len(rejected_by_match_fail)} 篇")
    if rejected_by_match_fail:
        for item in rejected_by_match_fail:
            print(f"  - AI标题: [ {item['classified_type']} ] {item['classified_title']} (最高匹配度: {item['score']}%)")

    print("\n" + "="*60)
    print(f"✅ [Workflow] 筛选完成，共有 {len(accepted_articles)} 篇文章将进行下一步处理。")
    
    # 合并所有被拒绝的文章
    all_rejected = rejected_by_type + [
        {
            'title': item['classified_title'],
            'type': '匹配失败',
            'why': f"无法找到相似度超过{SIMILARITY_THRESHOLD}%的原始文章",
            'original_article': {'title': item['classified_title'], 'url': '', 'mp_id': None}
        }
        for item in rejected_by_match_fail
    ]
    
    return accepted_articles, all_rejected

async def save_rejected_articles_as_markdown(rejected_items: List[Dict[str, Any]], output_dir: str, client: WeRSSClient) -> Tuple[int, int]:
    """将被拒绝的文章归档为Markdown文件，直接保存到排除流文件夹（不按日期），不进行OCR处理"""
    print("\n🚀 [Workflow] 开始将被拒绝的文章归档到Markdown文件 (仅转换模式，不进行OCR)...")
    
    articles_to_save = []
    for item in rejected_items:
        original_article = item.get('original_article')
        if original_article and (original_article.get('url') or original_article.get('link')):
            original_article['classified_type'] = item.get('type', '未知拒绝')
            articles_to_save.append(original_article)

    if not articles_to_save:
        print("  - 没有可归档的被拒绝文章（缺少URL）。")
        return 0, 0

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # 不按日期分组，直接保存到排除流文件夹
    type_folder = '排除流'
    print(f"\n📁 [Workflow] 处理 {type_folder} 的 {len(articles_to_save)} 篇被拒文章...")
    
    final_folder = os.path.join(output_dir, type_folder)
    os.makedirs(final_folder, exist_ok=True)
    
    total_saved = 0
    total_failed = 0
    
    # 直接处理所有被拒绝的文章（不按日期分组）
    articles = articles_to_save
        
    group_urls = [art.get("url") or art.get("link") for art in articles if art.get("url") or art.get("link")]
    
    if not group_urls:
        print(f"  - ⚠️ 没有有效的URL")
        return 0, 0

    try:
        # 只进行URL转Markdown，不进行OCR处理
        result = await convert_urls_to_markdown_only(
            urls=group_urls,
            output_dir=final_folder,
            articles=articles,
            client=client
        )
        
        if result["success"]:
            print(f"  - ✅ 归档成功 {result['success_count']}/{result['total_urls']} 篇")
            total_saved += result['success_count']
            total_failed += result['failed_count']
        else:
            error_msg = result.get('error', '未知错误')
            print(f"  - ❌ 归档失败 - {error_msg}")
            total_failed += len(articles)
            
    except Exception as e:
        print(f"  - ❌ 归档异常 - {str(e)}")
        total_failed += len(articles)
        
    return total_saved, total_failed

def append_mp_name_to_md_file(file_path: str, mp_name: str):
    """
    在MD文件末尾添加公众号信息
    
    Args:
        file_path: MD文件路径
        mp_name: 公众号名称
    """
    try:
        # 读取文件内容
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 检查文件末尾是否已有公众号信息（使用正则表达式匹配）
        pattern = r'---\s*\n\s*公众号[：:]\s*[^\n]+'
        if re.search(pattern, content):
            # 已存在公众号信息，检查是否需要更新
            # 如果已存在且名称相同，则无需重复添加
            existing_match = re.search(r'---\s*\n\s*公众号[：:]\s*([^\n]+)', content)
            if existing_match and existing_match.group(1).strip() == mp_name:
                return  # 已存在且名称相同，无需重复添加
            # 如果已存在但名称不同，替换为新的公众号名称
            content = re.sub(pattern, f'---\n\n公众号：{mp_name}\n', content, count=1)
        else:
            # 移除末尾的空白字符，然后添加公众号信息
            content = content.rstrip()
            content += f'\n\n---\n\n公众号：{mp_name}\n'
        
        # 写回文件
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        print(f"  ⚠️ 添加公众号信息到文件失败 {file_path}: {str(e)}")

async def convert_urls_to_markdown_only(
    urls: List[str], 
    output_dir: str,
    articles: List[Dict[str, Any]] = None,
    client: WeRSSClient = None
) -> Dict[str, Any]:
    """
    只将URL转换为Markdown文件，不进行OCR处理
    
    Args:
        urls: URL列表
        output_dir: 输出目录
        articles: 文章信息列表（用于获取公众号信息，用于后续重命名）
        client: WeRSS客户端（用于获取公众号信息，用于后续重命名）
        
    Returns:
        处理结果字典
    """
    url_service = URLService()
    success_count = 0
    failed_count = 0
    
    # 创建URL到文章的映射
    url_to_article = {}
    if articles:
        for article in articles:
            url = article.get("url") or article.get("link")
            if url:
                url_to_article[url] = article
    
    for url in urls:
        try:
            article = url_to_article.get(url)
            custom_filename = build_obsidian_article_filename(article, client) if article and client else None
            result = await asyncio.to_thread(url_service.convert_single_url, url, output_dir, custom_filename)
            if result.get('success'):
                success_count += 1
                
                # 兼容无文章元数据的旧调用；主流程已通过 custom_filename 直接生成 Obsidian 文件名。
                output_path = result.get('output_path')
                if output_path and os.path.exists(output_path) and not custom_filename:
                    # 获取对应的文章信息
                    article = url_to_article.get(url)
                    if article and client:
                        mp_id = article.get("mp_id") or article.get("account_id")
                        mp_name = "未知公众号"
                        if mp_id:
                            try:
                                mp_info = client.get_mp_info(mp_id)
                                if mp_info:
                                    mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
                            except:
                                pass
                        
                        # 如果获取到公众号名称，重命名文件
                        if mp_name and mp_name != "未知公众号":
                            try:
                                old_filename = os.path.basename(output_path)
                                name_without_ext = os.path.splitext(old_filename)[0]
                                
                                # 检查文件名是否已经包含公众号名称（在开头）
                                safe_mp_name = re.sub(r'[\\/:*?"<>|]', '_', mp_name)[:20]
                                
                                # 检查是否已经是新格式（公众号名称在开头）
                                if name_without_ext.startswith(f"{safe_mp_name}_"):
                                    continue  # 已经是新格式，跳过
                                
                                # 检查是否是旧格式（公众号名称在末尾）
                                if name_without_ext.endswith(f"_{safe_mp_name}"):
                                    # 解析旧格式：标题_时间戳_公众号名称 -> 公众号名称_标题_时间戳
                                    # 移除末尾的公众号名称
                                    name_without_mp = name_without_ext[:-len(f"_{safe_mp_name}")]
                                    # 提取时间戳（格式：_YYYYMMDD_HHMMSS 或 _YYYYMMDDHHMMSS）
                                    timestamp_match = re.search(r'(_\d{8}_\d{6}|_\d{14})$', name_without_mp)
                                    if timestamp_match:
                                        timestamp = timestamp_match.group(1)
                                        title_part = name_without_mp[:-len(timestamp)]
                                        new_filename = f"{safe_mp_name}_{title_part}{timestamp}.md"
                                    else:
                                        # 如果没有时间戳，直接重组
                                        new_filename = f"{safe_mp_name}_{name_without_mp}.md"
                                else:
                                    # 文件名中没有公众号名称，需要添加
                                    # 提取时间戳（格式：_YYYYMMDD_HHMMSS 或 _YYYYMMDDHHMMSS）
                                    timestamp_match = re.search(r'(_\d{8}_\d{6}|_\d{14})$', name_without_ext)
                                    if timestamp_match:
                                        timestamp = timestamp_match.group(1)
                                        title_part = name_without_ext[:-len(timestamp)]
                                        new_filename = f"{safe_mp_name}_{title_part}{timestamp}.md"
                                    else:
                                        # 如果没有时间戳，直接添加公众号名称到开头
                                        new_filename = f"{safe_mp_name}_{name_without_ext}.md"
                                
                                new_path = os.path.join(output_dir, new_filename)
                                
                                # 如果新文件名已存在，跳过重命名
                                if not os.path.exists(new_path):
                                    os.rename(output_path, new_path)
                                    print(f"  📝 已添加公众号名称到文件名: {new_filename}")
                            except Exception as e:
                                print(f"  ⚠️ 添加公众号名称到文件名失败: {str(e)}")
            else:
                failed_count += 1
        except Exception as e:
            print(f"  - ❌ 转换失败 {url}: {str(e)}")
            failed_count += 1
    
    return {
        "success": True,
        "total_urls": len(urls),
        "success_count": success_count,
        "failed_count": failed_count,
        "output_dir": output_dir
    }

async def save_articles_as_markdown(articles_to_save: List[Dict[str, Any]], output_dir: str, client: WeRSSClient) -> Tuple[int, int]:
    """将文章保存为Markdown文件，按类型组织文件夹（不按日期），不进行OCR处理（仅转换URL为MD）"""
    print("\n🚀 [Workflow] 开始保存文章到Markdown文件 (仅转换模式，不进行OCR)...")
    
    if not articles_to_save:
        print("  - 无需保存的文章。")
        return 0, 0

    # 确保输出目录存在
    os.makedirs(output_dir, exist_ok=True)
    
    # V6.0：按类型分组文章（类型直接对应文件夹名称，不按日期分组）
    articles_by_type = {}
    for article in articles_to_save:
        # 获取AI分类类型
        classified_type = article.get('classified_type', '未分类')
        
        # V6.0：直接使用classified_type作为文件夹名，但需要处理特殊情况
        if classified_type == "⛔ 看文章再确定" or "⛔ 看文章再确定" in classified_type:
            # 看文章再确定类型保存到"其他"文件夹
            type_folder = "其他"
        elif classified_type in ALLOWED_TYPES:
            # 精确匹配，直接使用类型作为文件夹名
            type_folder = classified_type
        else:
            # 尝试模糊匹配（处理可能的变体）
            type_folder = None
            for allowed_type in ALLOWED_TYPES:
                if allowed_type in classified_type or classified_type in allowed_type:
                    type_folder = allowed_type
                    break
            
            if not type_folder:
                # 无法匹配到任何已知类型，保存到"其他"文件夹
                print(f"⚠️ 无法识别的分类类型: {classified_type}，将保存到'其他'文件夹")
                type_folder = "其他"
        
        # 按类型分组（不按日期）
        if type_folder not in articles_by_type:
            articles_by_type[type_folder] = []
        articles_by_type[type_folder].append(article)
    
    total_saved = 0
    total_failed = 0
    
    # 按类型处理文章（不按日期）
    for type_folder, articles in articles_by_type.items():
        print(f"\n📁 [Workflow] 处理 {type_folder} 的 {len(articles)} 篇文章...")
        
        # 创建类型文件夹（不创建日期子文件夹）
        final_folder = os.path.join(output_dir, type_folder)
        os.makedirs(final_folder, exist_ok=True)
        
        # 提取该组的URL列表
        group_urls = []
        for article in articles:
            url = article.get("url") or article.get("link")
            if url:
                group_urls.append(url)
        
        if not group_urls:
            print(f"  - ⚠️ 没有有效的URL")
            continue

        # 只进行URL转Markdown，不进行OCR处理
        try:
            result = await convert_urls_to_markdown_only(
                urls=group_urls,
                output_dir=final_folder,
                articles=articles,
                client=client
            )
            
            if result["success"]:
                print(f"  - ✅ 成功 {result['success_count']}/{result['total_urls']} 篇")
                
                # 检查生成的文件，处理已删除的文章
                deleted_articles = await check_and_handle_deleted_articles(articles, final_folder, client)
                
                # 更新统计数据
                actual_success = result['success_count'] - len(deleted_articles)
                total_saved += actual_success
                total_failed += result['failed_count'] + len(deleted_articles)
                
                if deleted_articles:
                    print(f"  - 🗑️ 发现 {len(deleted_articles)} 篇已删除文章，已加入拒绝列表")
                
            else:
                error_msg = result.get('error', '未知错误')
                print(f"  - ❌ 处理失败 - {error_msg}")
                total_failed += len(articles)
                
        except Exception as e:
            print(f"  - ❌ 处理异常 - {str(e)}")
            total_failed += len(articles)
            
    return total_saved, total_failed

async def check_and_handle_deleted_articles(articles: List[Dict[str, Any]], date_folder: str, client: WeRSSClient) -> List[Dict[str, Any]]:
    """检查并处理已删除的文章（标题为"未命名文章"的文件）"""
    deleted_articles = []
    
    try:
        # 获取文件夹中的所有MD文件
        md_files = [f for f in os.listdir(date_folder) if f.endswith('.md')]
        
        for md_file in md_files:
            file_path = os.path.join(date_folder, md_file)
            
            try:
                # 检查文件是否为已删除文章（标题为"未命名文章"）
                with open(file_path, 'r', encoding='utf-8') as f:
                    first_line = f.readline().strip()
                    
                if first_line == "# 未命名文章":
                    # 通过URL匹配找到对应的原始文章
                    corresponding_article = None
                    
                    # 尝试从文件内容中提取URL
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                            # 查找可能的URL引用或者直接匹配
                            for article in articles:
                                url = article.get("url") or article.get("link")
                                if url and url in content:
                                    corresponding_article = article
                                    break
                    except:
                        pass
                    
                    # 如果还没找到，按顺序匹配（简单策略）
                    if not corresponding_article and articles:
                        # 由于是按批次处理，通常第一个未匹配的文章就是对应的
                        corresponding_article = articles[0]
                    
                    if corresponding_article:
                        # 创建拒绝记录
                        deleted_article = {
                            'title': corresponding_article.get('title', '未知标题'),
                            'type': '已删除',
                            'why': '文章已被删除或无法访问，生成了"未命名文章"',
                            'original_article': corresponding_article
                        }
                        deleted_articles.append(deleted_article)
                        
                        # 删除这个无用的MD文件
                        os.remove(file_path)
                        print(f"    🗑️ 已删除无效文件: {md_file}")
                    
            except Exception as e:
                print(f"    ⚠️ 检查文件时出错 {md_file}: {str(e)}")
                continue
    
    except Exception as e:
        print(f"  ⚠️ 检查已删除文章时出错: {str(e)}")
    
    # 如果有已删除的文章，保存到拒绝列表
    if deleted_articles:
        save_rejected_articles(deleted_articles, REJECTED_CSV_FILE, client)
    
    return deleted_articles

async def rename_files_with_mp_info(articles: List[Dict[str, Any]], date_folder: str, client: WeRSSClient):
    """重命名文件以包含公众号信息"""
    try:
        # 获取文件夹中的所有MD文件
        md_files = [f for f in os.listdir(date_folder) if f.endswith('.md')]
        
        for article in articles:
            url = article.get("url") or article.get("link")
            title = article.get("title", "无标题")
            
            # 获取公众号名称
            mp_id = article.get("mp_id") or article.get("account_id")
            mp_name = "未知公众号"
            if mp_id:
                try:
                    mp_info = client.get_mp_info(mp_id)
                    if mp_info:
                        mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
                except:
                    pass
            
            # 查找匹配的文件
            safe_title = re.sub(r'[\\/:*?"<>|]', '_', title)[:50]
            safe_mp_name = re.sub(r'[\\/:*?"<>|]', '_', mp_name)[:20]
            
            # 查找可能匹配的文件
            for md_file in md_files:
                if safe_title.lower() in md_file.lower():
                    old_path = os.path.join(date_folder, md_file)
                    
                    # 检查文件名是否已经包含公众号名称
                    name_without_ext = md_file[:-3]  # 去掉.md
                    
                    # 检查是否已经是新格式（公众号名称在开头）
                    if name_without_ext.startswith(f"{safe_mp_name}_"):
                        md_files.remove(md_file)  # 从列表中移除已处理的文件
                        break
                    
                    # 检查是否是旧格式（公众号名称在末尾）
                    if name_without_ext.endswith(f"_{safe_mp_name}"):
                        # 解析旧格式：标题_时间戳_公众号名称 -> 公众号名称_标题_时间戳
                        # 移除末尾的公众号名称
                        name_without_mp = name_without_ext[:-len(f"_{safe_mp_name}")]
                        # 提取时间戳（格式：_YYYYMMDD_HHMMSS 或 _YYYYMMDDHHMMSS）
                        timestamp_match = re.search(r'(_\d{8}_\d{6}|_\d{14})$', name_without_mp)
                        if timestamp_match:
                            timestamp = timestamp_match.group(1)
                            title_part = name_without_mp[:-len(timestamp)]
                            new_filename = f"{safe_mp_name}_{title_part}{timestamp}.md"
                        else:
                            # 如果没有时间戳，直接重组
                            new_filename = f"{safe_mp_name}_{name_without_mp}.md"
                    else:
                        # 文件名中没有公众号名称，需要添加
                        # 提取时间戳（格式：_YYYYMMDD_HHMMSS 或 _YYYYMMDDHHMMSS）
                        timestamp_match = re.search(r'(_\d{8}_\d{6}|_\d{14})$', name_without_ext)
                        if timestamp_match:
                            timestamp = timestamp_match.group(1)
                            title_part = name_without_ext[:-len(timestamp)]
                            new_filename = f"{safe_mp_name}_{title_part}{timestamp}.md"
                        else:
                            # 如果没有时间戳，直接添加公众号名称到开头
                            new_filename = f"{safe_mp_name}_{name_without_ext}.md"
                    
                    new_path = os.path.join(date_folder, new_filename)
                    
                    # 如果新文件名已存在，跳过重命名
                    if os.path.exists(new_path):
                        md_files.remove(md_file)  # 从列表中移除已处理的文件
                        break
                    
                    try:
                        os.rename(old_path, new_path)
                        md_files.remove(md_file)  # 从列表中移除已处理的文件
                        break
                    except Exception as e:
                        print(f"    ⚠️ 重命名文件失败: {str(e)}")
                        
    except Exception as e:
        print(f"  ⚠️ 重命名文件时出错: {str(e)}")

async def main_workflow():
    """主工作流函数"""
    print("="*60)
    print("自动化文章筛选与归档工作流启动")
    print("="*60)

    # 1. 初始化客户端
    client = WeRSSClient(base_url=BASE_URL, username=USERNAME, password=PASSWORD)
    if not client.token:
        print("❌ [Workflow] 登录WeRSS失败，请检查配置。")
        return
    print("✅ [Workflow] WeRSS客户端登录成功。")

    # 2. 获取最近的文章
    recent_articles = get_recent_articles(client, DAYS_TO_FETCH)
    total_articles_fetched = len(recent_articles)
    if not recent_articles:
        print("✅ [Workflow] 没有需要处理的新文章，任务结束。")
        return

    # 3. 扫描现有文章并过滤重复项（增量处理）
    existing_titles = scan_existing_articles(OUTPUT_DIR)
    rejected_urls = load_rejected_articles(REJECTED_CSV_FILE)
    filtered_articles, skipped_articles_count = filter_existing_articles(recent_articles, existing_titles, rejected_urls)
    
    if not filtered_articles:
        print("✅ [Workflow] 所有文章都已处理过，无需重复处理，任务结束。")
        return
    
    # 3.5. 分离特殊账号文章（提前过滤，不参与AI分类）
    special_articles, normal_articles = separate_special_accounts(filtered_articles, SPECIAL_ACCOUNTS, client)
    
    # 特殊账号文章直接保存
    special_saved = 0
    special_failed = 0
    if special_articles:
        print(f"\n📁 [Workflow] 开始保存 {len(special_articles)} 篇特殊账号文章...")
        special_saved, special_failed = await save_articles_as_markdown(special_articles, OUTPUT_DIR, client)
        print(f"✅ [Workflow] 特殊账号文章保存完成：成功 {special_saved} 篇，失败 {special_failed} 篇")
    
    # 如果没有普通账号文章需要处理，直接结束
    if not normal_articles:
        print("✅ [Workflow] 所有文章均为特殊账号文章，已处理完成，任务结束。")
        # 最终任务总结
        print("\n" + "="*60)
        print(" " * 22 + "🎉 任务完成 🎉")
        print("="*60)
        print(f"-  总计发现文章: {total_articles_fetched} 篇")
        print(f"-  已跳过文章: {skipped_articles_count} 篇 (已有MD文件或之前已拒绝)")
        print("")
        print("-  [特殊账号文章]")
        print(f"     -  成功处理: {special_saved} 篇")
        print(f"     -  处理失败: {special_failed} 篇")
        print("="*60)
        return
        
    article_titles = [article['title'] for article in normal_articles]
    print(f"\n📋 [Workflow] 准备将 {len(article_titles)} 篇普通账号文章发送给AI进行分类...")

    # 4. 调用AI进行分类（批量处理，每批最多20条）
    classifier = AIClassifier()  # 使用 Web 后端统一默认分类模型
    all_classified_results = []
    
    if len(article_titles) <= AI_BATCH_SIZE:
        # 如果文章数量不超过批量大小，直接处理
        print(f"📝 [Workflow] 文章数量 {len(article_titles)} 条，直接处理...")
        classified_results = classifier.classify_titles(article_titles)
        if classified_results:
            all_classified_results.extend(classified_results)
    else:
        # 分批处理
        total_batches = (len(article_titles) + AI_BATCH_SIZE - 1) // AI_BATCH_SIZE
        print(f"📝 [Workflow] 文章数量 {len(article_titles)} 条，分为 {total_batches} 批处理，每批 {AI_BATCH_SIZE} 条...")
        
        for batch_idx in range(0, len(article_titles), AI_BATCH_SIZE):
            batch_end = min(batch_idx + AI_BATCH_SIZE, len(article_titles))
            batch_titles = article_titles[batch_idx:batch_end]
            current_batch_num = (batch_idx // AI_BATCH_SIZE) + 1
            
            print(f"\n🔄 [Workflow] 正在处理第 {current_batch_num}/{total_batches} 批 ({len(batch_titles)} 篇文章)...")
            
            batch_classified_results = classifier.classify_titles(batch_titles)
            if batch_classified_results:
                all_classified_results.extend(batch_classified_results)
                print(f"✅ [Workflow] 第 {current_batch_num} 批处理完成，获得 {len(batch_classified_results)} 条分类结果")
            else:
                print(f"❌ [Workflow] 第 {current_batch_num} 批处理失败")
    
    if not all_classified_results:
        print("❌ [Workflow] AI分类失败或未返回任何结果，任务终止。")
        return
    
    print(f"✅ [Workflow] 所有批次处理完成，总共获得 {len(all_classified_results)} 条分类结果")

    # 5. 匹配和筛选
    accepted_articles, rejected_articles = match_and_filter_articles(normal_articles, all_classified_results)

    # 6. 保存为Markdown（按日期组织，使用高级OCR处理）
    accepted_saved, accepted_failed = await save_articles_as_markdown(accepted_articles, OUTPUT_DIR, client)

    # 7. 将被拒绝的文章也归档为Markdown
    rejected_saved, rejected_failed = await save_rejected_articles_as_markdown(rejected_articles, OUTPUT_DIR, client)

    # 8. 保存被拒绝的文章记录到CSV（用于去重）
    total_rejected_in_csv = save_rejected_articles(rejected_articles, REJECTED_CSV_FILE, client)

    # --- 最终任务总结 ---
    print("\n" + "="*60)
    print(" " * 22 + "🎉 任务完成 🎉")
    print("="*60)
    print(f"-  总计发现文章: {total_articles_fetched} 篇")
    print(f"-  已跳过文章: {skipped_articles_count} 篇 (已有MD文件或之前已拒绝)")
    print("")
    if special_articles:
        print("-  [特殊账号文章]")
        print(f"     -  成功处理: {special_saved} 篇")
        print(f"     -  处理失败: {special_failed} 篇")
        print("")
    print("-  [已接受文章 (AI分类)]")
    print(f"     -  成功处理: {accepted_saved} 篇")
    print(f"     -  处理失败: {accepted_failed} 篇")
    print("")
    print("-  [排除流]")
    print(f"     -  成功归档: {rejected_saved} 篇")
    print(f"     -  归档失败: {rejected_failed} 篇")
    print("")
    if total_rejected_in_csv > 0:
        print(f"-  CSV拒绝记录: 更新成功，总计 {total_rejected_in_csv} 篇")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(main_workflow()) 
