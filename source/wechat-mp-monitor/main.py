#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WeRSS API 客户端使用示例 - 统一版本
"""

from werss_client import WeRSSClient
from datetime import datetime, timedelta
import os
import time
import csv
import pandas as pd

# WeRSS 配置（模块级，供 main 与 article_workflow 共用）
BASE_URL = "http://192.168.31.89:8001"
USERNAME = "admin"
PASSWORD = "admin@123"

def format_timestamp(timestamp):
    """将时间戳转换为可读的日期时间格式"""
    try:
        if isinstance(timestamp, (int, float)):
            dt = datetime.fromtimestamp(timestamp)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            return str(timestamp)
    except Exception:
        return str(timestamp)

def get_date_range(days=7):
    """获取最近N天的日期列表，包括今天"""
    today = datetime.now().date()
    dates = []
    for i in range(days):
        date = today - timedelta(days=i)
        dates.append(date.strftime("%Y-%m-%d"))
    return dates

def generate_markdown_report(articles, target_date, client, output_dir="."):
    """生成Markdown格式的文章报告"""
    # 生成文件名
    date_str = target_date.replace("-", "")  # 20250621
    filename = f"{date_str}.md"
    filepath = os.path.join(output_dir, filename)
    
    # 准备Markdown内容
    markdown_content = []
    
    if len(articles) == 0:
        markdown_content.append("📭 当天暂无文章发布")
    else:
        # 添加表格头
        markdown_content.append("| 文章标题 | 公众号名称 | 发布时间 |")
        markdown_content.append("|----------|------------|----------|")
        
        # 添加文章数据
        for article in articles:
            title = article.get("title", "无标题").replace("|", "｜")  # 替换管道符避免表格格式错误
            url = article.get("url") or article.get("link") or article.get("content_url", "无链接")
            mp_id = article.get("mp_id") or article.get("account_id")
            
            # 获取公众号名称
            mp_name = "未知公众号"
            if mp_id:
                mp_info = client.get_mp_info(mp_id)
                if mp_info:
                    mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
            
            # 格式化发布时间
            publish_time = article.get("publish_time") or article.get("created_at") or article.get("update_time")
            formatted_time = format_timestamp(publish_time)
            
            # 创建表格行 - 只有标题带链接
            if url and url != "无链接":
                title_with_link = f"[{title}]({url})"
            else:
                title_with_link = title
                
            row = f"| {title_with_link} | {mp_name} | {formatted_time} |"
            markdown_content.append(row)
    
    # 写入文件
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write('\n'.join(markdown_content))
        print(f"📝 Markdown报告已生成: {filepath}")
        return filepath
    except Exception as e:
        print(f"❌ 生成Markdown报告失败: {e}")
        return None

def update_articles_csv(all_articles, client, output_dir, filename="articles.csv"):
    """更新文章CSV文件（增量录入）"""
    csv_file = os.path.join(output_dir, filename)
    
    print(f"\n📊 开始更新文章CSV文件: {filename}")
    
    # CSV列定义 - 调整为用户要求的顺序
    columns = ['title', 'publish_time', 'mp_name', 'url', 'mp_id', 'publish_date', 'author', 'content_summary', 'id']
    
    # 读取现有CSV文件（如果存在）
    existing_articles = {}
    existing_urls = set()  # 用于快速查找已存在的URL
    
    if os.path.exists(csv_file):
        try:
            df_existing = pd.read_csv(csv_file, encoding='utf-8')
            print(f"   📄 读取现有CSV文件，已有 {len(df_existing)} 篇文章")
            
            # 使用URL作为唯一标识符，同时记录到set中以提高查找效率
            for _, row in df_existing.iterrows():
                url = str(row['url']).strip() if pd.notna(row['url']) else ""
                if url:  # 只处理有效的URL
                    existing_articles[url] = row.to_dict()
                    existing_urls.add(url)
            
            print(f"   📋 已存在的唯一URL数量: {len(existing_urls)}")
        except Exception as e:
            print(f"   ⚠️ 读取现有CSV文件失败: {e}")
    else:
        print(f"   📄 CSV文件不存在，将创建新文件")
    
    # 处理新文章
    new_articles = []
    updated_count = 0
    skipped_count = 0
    
    for article in all_articles:
        # 获取并清理URL
        url = article.get("url") or article.get("link") or article.get("content_url")
        
        if not url:
            continue  # 跳过没有URL的文章
        
        # 清理URL（去除空格等）
        url = str(url).strip()
        
        # 检查是否已存在（使用set进行快速查找）
        if url in existing_urls:
            skipped_count += 1
            continue  # 文章已存在，跳过
        
        # 获取公众号信息
        mp_id = article.get("mp_id") or article.get("account_id")
        mp_name = "未知公众号"
        if mp_id:
            mp_info = client.get_mp_info(mp_id)
            if mp_info:
                mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
        
        # 处理发布时间
        publish_time = article.get("publish_time") or article.get("created_at") or article.get("update_time")
        publish_time_str = format_timestamp(publish_time) if publish_time else ""
        
        # 提取发布日期
        publish_date = ""
        if publish_time:
            try:
                if isinstance(publish_time, (int, float)):
                    dt = datetime.fromtimestamp(publish_time)
                    publish_date = dt.strftime("%Y-%m-%d")
                else:
                    publish_date = str(publish_time)[:10]
            except:
                pass
        
        # 准备文章数据 - 使用新的列顺序
        article_data = {
            'title': article.get("title", "无标题"),
            'publish_time': publish_time_str,
            'mp_name': mp_name,
            'url': url,
            'mp_id': mp_id or "",
            'publish_date': publish_date,
            'author': article.get("author", ""),
            'content_summary': article.get("digest", "")[:100] if article.get("digest") else "",
            'id': article.get("id", "")
        }
        
        new_articles.append(article_data)
        existing_urls.add(url)  # 添加到已存在的URL集合中，避免同一批次内的重复
        updated_count += 1
    
    if updated_count == 0:
        print(f"   ✅ 没有新文章需要添加")
        if skipped_count > 0:
            print(f"   📊 跳过了 {skipped_count} 篇重复文章")
        return csv_file
    
    print(f"   📝 发现 {updated_count} 篇新文章，正在添加...")
    if skipped_count > 0:
        print(f"   📊 跳过了 {skipped_count} 篇重复文章")
    
    # 合并现有数据和新数据
    all_csv_articles = list(existing_articles.values()) + new_articles
    
    # 按发布时间排序（最新的在前）
    def get_sort_key(article):
        publish_time = article.get('publish_time', '')
        if publish_time:
            try:
                return datetime.strptime(publish_time, "%Y-%m-%d %H:%M:%S")
            except:
                pass
        return datetime.min
    
    all_csv_articles.sort(key=get_sort_key, reverse=True)
    
    # 最终去重检查（以防万一）
    seen_urls = set()
    deduplicated_articles = []
    
    for article in all_csv_articles:
        url = str(article.get('url', '')).strip()
        if url and url not in seen_urls:
            deduplicated_articles.append(article)
            seen_urls.add(url)
    
    if len(deduplicated_articles) != len(all_csv_articles):
        removed_duplicates = len(all_csv_articles) - len(deduplicated_articles)
        print(f"   🔧 最终去重：移除了 {removed_duplicates} 个重复项")
        all_csv_articles = deduplicated_articles
    
    # 写入CSV文件
    try:
        df = pd.DataFrame(all_csv_articles, columns=columns)
        df.to_csv(csv_file, index=False, encoding='utf-8-sig')  # 使用utf-8-sig确保Excel能正确显示中文
        print(f"   ✅ CSV文件已更新: {csv_file}")
        print(f"   📊 总计文章数: {len(all_csv_articles)} 篇（新增 {updated_count} 篇）")
        return csv_file
    except Exception as e:
        print(f"   ❌ 写入CSV文件失败: {e}")
        return None

def safe_filename(name):
    """将公众号名称转换为安全的文件名"""
    # 替换不安全的字符
    unsafe_chars = ['<', '>', ':', '"', '/', '\\', '|', '?', '*']
    safe_name = name
    for char in unsafe_chars:
        safe_name = safe_name.replace(char, '_')
    
    # 限制长度
    if len(safe_name) > 50:
        safe_name = safe_name[:50]
    
    return safe_name.strip()

def comprehensive_update_and_export(client, output_dir, update_articles=True):
    """综合功能：更新所有公众号文章并导出所有数据"""
    print("\n" + "="*60)
    print("🚀 开始综合数据收集和整理任务")
    print("="*60)
    
    # 获取所有公众号
    mps = client.get_all_mps()
    if not mps:
        print("⚠️  未找到任何公众号，请先添加公众号")
        return
    
    print(f"📋 找到 {len(mps)} 个公众号")
    
    total_time_needed = 0  # 初始化预估时间

    # 第一阶段：批量更新所有公众号文章
    print("\n" + "="*60)
    if update_articles:
        print("📡 第一阶段：批量更新所有公众号文章 (更新模式)")
        total_time_needed = len(mps) * 10  # 10秒间隔
        print(f"⏱️  预计总耗时: {total_time_needed // 60} 分钟 {total_time_needed % 60} 秒")
    else:
        print("📡 第一阶段：直接获取现有公众号文章 (只读模式)")
    print("="*60)
    
    success_count = 0
    all_mp_stats = {}  # 存储每个公众号的统计信息
    all_mp_articles = {}  # 存储每个公众号的文章数据
    
    for i, mp in enumerate(mps, 1):
        mp_id = mp.get("id") or mp.get("mp_id")
        mp_name = mp.get("mp_name") or mp.get("name", "未知公众号")
        
        if not mp_id:
            print(f"⚠️  [{i}/{len(mps)}] {mp_name} 缺少ID，跳过")
            continue
        
        should_fetch_articles = False
        if update_articles:
            print(f"\n🔄 [{i}/{len(mps)}] 正在更新: {mp_name}")
            print(f"⏰ 开始时间: {datetime.now().strftime('%H:%M:%S')}")
            
            # 更新文章
            if client.update_mp_articles(mp_id):
                success_count += 1
                print(f"✅ 更新API调用成功")
                
                # 等待爬虫真正完成（爬虫需要时间处理每篇文章）
                print(f"⏳ 等待10秒让爬虫完成文章抓取...")
                print(f"📊 剩余: {len(mps) - i} 个公众号")
                time.sleep(10)
                should_fetch_articles = True
                
            else:
                print(f"❌ 更新失败")
                all_mp_stats[mp_name] = {'week_count': 0, 'today_count': 0, 'total_count': 0, 'mp_id': mp_id}
        else: # 只读模式
            print(f"\n🔍 [{i}/{len(mps)}] 正在读取: {mp_name}")
            should_fetch_articles = True
            success_count += 1 # 在只读模式下，我们视读取为“成功”处理

        # 获取文章数据的逻辑
        if should_fetch_articles:
            print(f"🔍 开始获取 [{mp_name}] 的最新文章数据...")
            try:
                # 立即重新获取该公众号的文章（模拟网页的fetchArticles）
                mp_articles = client.get_mp_articles(mp_id)
                all_mp_articles[mp_id] = {
                    'name': mp_name,
                    'articles': mp_articles
                }
                
                # 统计该公众号最近一周的文章
                dates = get_date_range(7)
                week_articles = []
                today_articles = []
                today_str = datetime.now().strftime("%Y-%m-%d")
                
                for article in mp_articles:
                    article_date = article.get("publish_time")
                    if article_date:
                        # 处理时间戳格式
                        if isinstance(article_date, (int, float)):
                            article_date_str = datetime.fromtimestamp(article_date).strftime("%Y-%m-%d")
                        else:
                            try:
                                dt = datetime.strptime(str(article_date)[:10], "%Y-%m-%d")
                                article_date_str = dt.strftime("%Y-%m-%d")
                            except:
                                continue
                        
                        if article_date_str in dates:
                            week_articles.append(article)
                            if article_date_str == today_str:
                                today_articles.append(article)
                
                # 显示统计结果
                print(f"   📈 一周总计: {len(week_articles)} 篇文章")
                print(f"   📅 今日文章: {len(today_articles)} 篇")
                print(f"   📚 历史总计: {len(mp_articles)} 篇文章")
                
                if len(today_articles) > 0:
                    print(f"   ✅ 今日有新文章发布！")
                    for article in today_articles:
                        title = article.get('title', '无标题')[:30] + ('...' if len(article.get('title', '')) > 30 else '')
                        print(f"      • {title}")
                else:
                    print(f"   📭 今日暂无文章发布")
                
                # 存储统计信息
                all_mp_stats[mp_name] = {
                    'week_count': len(week_articles),
                    'today_count': len(today_articles),
                    'total_count': len(mp_articles),
                    'mp_id': mp_id
                }
                
            except Exception as e:
                print(f"   ⚠️  获取文章数据失败: {e}")
                all_mp_stats[mp_name] = {
                    'week_count': 0,
                    'today_count': 0,
                    'total_count': 0,
                    'mp_id': mp_id
                }
    
    print("\n" + "="*60)
    if update_articles:
        print(f"✅ 第一阶段完成！成功更新 {success_count}/{len(mps)} 个公众号")
    else:
        print(f"✅ 第一阶段完成！成功读取 {success_count}/{len(mps)} 个公众号")
    print("="*60)

    # 第二阶段：为每个公众号生成独立的CSV文件
    print("\n" + "="*60)
    print("📊 第二阶段：为每个公众号生成独立CSV文件")
    print("="*60)
    
    individual_csv_files = []
    for mp_id, mp_data in all_mp_articles.items():
        mp_name = mp_data['name']
        mp_articles = mp_data['articles']
        
        if mp_articles:
            print(f"\n📝 正在为 [{mp_name}] 生成CSV文件...")
            
            # 生成安全的文件名
            safe_mp_name = safe_filename(mp_name)
            csv_filename = f"{safe_mp_name}.csv"
            
            # 调用CSV更新函数
            csv_file = update_articles_csv(mp_articles, client, output_dir, csv_filename)
            
            if csv_file:
                individual_csv_files.append(csv_file)
                print(f"   ✅ 已生成: {csv_filename} ({len(mp_articles)} 篇文章)")
            else:
                print(f"   ❌ 生成失败: {csv_filename}")
        else:
            print(f"📭 [{mp_name}] 无文章，跳过CSV生成")
    
    # 第三阶段：生成每日报告和总的CSV数据库
    print("\n" + "="*60)
    print("📰 第三阶段：生成每日报告和总数据库")
    print("="*60)

    # 从第一阶段的结果中整合所有文章到一个列表，避免重复请求
    print("正在从内存中整合所有文章...")
    master_article_list = []
    for mp_data in all_mp_articles.values():
        if mp_data.get('articles'):
            master_article_list.extend(mp_data['articles'])
    print(f"  已整合 {len(master_article_list)} 篇文章")
    
    # 获取最近7天的日期
    date_list = get_date_range(7)
    print(f"📅 将处理以下日期: {', '.join(date_list)}")
    
    total_articles = 0
    generated_files = []
    all_articles_for_csv = []
    
    # 为每一天生成报告
    for i, target_date in enumerate(date_list, 1):
        print(f"\n🔍 [{i}/{len(date_list)}] 处理日期: {target_date}")
        print("-" * 40)
        
        # 从内存中的主列表筛选当天的文章，而不是重复调用API
        articles_for_date = []
        for article in master_article_list:
            publish_time = article.get("publish_time")
            if not publish_time:
                continue
            
            try:
                if isinstance(publish_time, (int, float)):
                    article_date_str = datetime.fromtimestamp(publish_time).strftime("%Y-%m-%d")
                else:
                    article_date_str = str(publish_time)[:10]
                
                if article_date_str == target_date:
                    articles_for_date.append(article)
            except (ValueError, TypeError):
                continue # 跳过格式不正确的日期

        if not articles_for_date:
            print(f"📭 {target_date} 无文章发布")
        else:
            print(f"📚 找到 {len(articles_for_date)} 篇文章")
            total_articles += len(articles_for_date)
            all_articles_for_csv.extend(articles_for_date)
            
            # 显示文章标题（简化显示）
            for j, article in enumerate(articles_for_date, 1):
                title = article.get("title", "无标题")
                mp_id = article.get("mp_id") or article.get("account_id")
                
                # 获取公众号名称
                mp_name = "未知公众号"
                if mp_id:
                    mp_info = client.get_mp_info(mp_id)
                    if mp_info:
                        mp_name = mp_info.get("mp_name") or mp_info.get("name", "未知公众号")
                
                print(f"  📄 [{j}] {title} - {mp_name}")
        
        # 生成该日期的Markdown报告
        markdown_file = generate_markdown_report(articles_for_date, target_date, client, output_dir)
        if markdown_file:
            generated_files.append(markdown_file)
    
    # 更新总的CSV文件
    print("\n" + "="*60)
    print("📊 更新总文章CSV数据库")
    print("="*60)
    
    csv_file = update_articles_csv(all_articles_for_csv, client, output_dir)
    if csv_file:
        generated_files.append(csv_file)
    
    # 显示所有公众号的统计汇总
    print("\n" + "="*60)
    print("📊 各公众号文章统计汇总")
    print("="*60)
    
    total_week_articles = 0
    total_today_articles = 0
    total_all_articles = 0
    active_accounts = 0  # 一周内有文章的账号数
    today_active_accounts = 0  # 今日有文章的账号数
    
    for mp_name, stats in all_mp_stats.items():
        total_week_articles += stats['week_count']
        total_today_articles += stats['today_count']
        total_all_articles += stats['total_count']
        
        if stats['week_count'] > 0:
            active_accounts += 1
        if stats['today_count'] > 0:
            today_active_accounts += 1
        
        status_icon = "📝" if stats['today_count'] > 0 else "📭"
        print(f"{status_icon} {mp_name}: 今日 {stats['today_count']} 篇 | 一周 {stats['week_count']} 篇 | 总计 {stats['total_count']} 篇")
    
    print(f"\n🎯 总体统计:")
    print(f"   📅 今日总文章数: {total_today_articles} 篇")
    print(f"   📈 一周总文章数: {total_week_articles} 篇")
    print(f"   📚 历史总文章数: {total_all_articles} 篇")
    print(f"   ⭐ 今日活跃数: {today_active_accounts}/{len(mps)} 个")
    print(f"   🔥 活跃账号数: {active_accounts}/{len(mps)} 个（一周内有文章）")
    
    # 最终汇总报告
    print("\n" + "="*60)
    print("🎉 综合任务完成汇总")
    print("="*60)
    
    print(f"✅ 任务完成！")
    print(f"📈 统计信息:")
    if update_articles:
        print(f"  - 成功更新: {success_count}/{len(mps)} 个公众号")
    else:
        print(f"  - 成功读取: {success_count}/{len(mps)} 个公众号")
    print(f"  - 处理日期数: {len(date_list)} 天")
    print(f"  - 生成个人CSV: {len(individual_csv_files)} 个")
    print(f"  - 生成报告: {len(generated_files)} 个")
    print(f"  - 历史文章总数: {total_all_articles} 篇")
    
    if individual_csv_files:
        print(f"\n📁 生成的个人CSV文件:")
        for filepath in individual_csv_files:
            print(f"  - {os.path.basename(filepath)}")
    
    if generated_files:
        print(f"\n📝 生成的报告文件:")
        for filepath in generated_files:
            print(f"  - {os.path.basename(filepath)}")
    
    print(f"\n💡 提示:")
    print(f"  - 每个公众号的历史文章已保存为独立CSV文件")
    print(f"  - 每个日期的文章已保存为独立MD文件")
    print(f"  - 所有文章信息已更新到总CSV数据库")
    
    if total_time_needed > 0:
        print(f"⏰ 总耗时: 约 {total_time_needed // 60} 分钟")

def main():
    # 使用模块级 BASE_URL, USERNAME, PASSWORD（见文件顶部）
    # 创建客户端实例
    client = WeRSSClient(
        base_url=BASE_URL,
        username=USERNAME,
        password=PASSWORD
    )
    
    # 检查登录状态
    if not client.token:
        print("❌ 登录失败，请检查用户名和密码")
        print("请修改 main.py 文件中的 USERNAME 和 PASSWORD")
        return
    
    print("✅ 登录成功！")

    # 创建输出目录
    output_dir = "output"
    try:
        os.makedirs(output_dir, exist_ok=True)
        print(f"📂 所有生成的文件将保存在 {os.path.abspath(output_dir)} 文件夹中")
    except Exception as e:
        print(f"❌ 创建输出目录失败: {e}")
        return

    # 让用户选择运行模式
    while True:
        print("\n" + "="*60)
        print("请选择运行模式:")
        print("  (1) 更新所有账号并生成报告 (耗时较长)")
        print("  (2) 直接使用现有数据生成报告 (速度快)")
        mode = input("请输入选项 [1/2]: ").strip()
        if mode in ['1', '2']:
            break
        print("❌ 输入无效，请输入 1 或 2。")

    update_mode = (mode == '1')
    
    try:
        if update_mode:
            print("\n你选择了[更新所有账号]模式，程序将开始更新，请耐心等待...")
        else:
            print("\n你选择了[直接读取数据]模式，将跳过在线更新步骤。")

        comprehensive_update_and_export(client, output_dir, update_articles=update_mode)
    except KeyboardInterrupt:
        print("\n\n👋 程序被用户中断，再见！")
    except Exception as e:
        print(f"\n❌ 发生错误: {e}")
        print("程序异常退出")

if __name__ == "__main__":
    main() 
