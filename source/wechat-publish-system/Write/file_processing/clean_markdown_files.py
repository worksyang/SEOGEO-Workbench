"""
# 🧹Markdown批量清洗器
1️⃣ 文件扫描  
   └─ 📂 批量收集目标目录所有.md文件  
2️⃣ 关键词清理  
   └─ ⛔ 按自定义词表移除脏词或水印内容  
3️⃣ 文件覆盖  
   └─ 💾 清洗结果写回原文件高效保存  
"""
import os
import glob

# ===================== 自定义关键词区域 =====================
# 在这里添加需要清理的自定义关键词
# 每个关键词将被替换为空字符串
CUSTOM_KEYWORDS = [
    "郑倩",  # 例如：某些特定的标记
    "Judy",  # 例如：不需要的文本片段
    "[ 香港保险行业资讯 ](javascript:void\(0\);)", 
    "wdgj980",  # 例如：某些特定的标记
    "公众号·香港保险行业资讯",  # 例如：不需要的文本片段
    "内地及香港同行勿扰，谢谢！",
    "关注该公众号",
    "原创",
    "Mark",
    "特别分子",
    "[ 特别分子Mark ](javascript:void\(0\);)",
    "https://mmbiz.qpic.cn/sz_mmbiz_png/3jb6cgHmdTQ3SV4EibUWTHbILYrEBslCIfsNarfiahkthrlSJiaZCIVQ3qFot6eCCicFcfwcUZQF1w6yv2HocB8LwQ/640?wx_fmt=other&from=appmsg&wxfrom=5&wx_lazy=1&wx_co=1&tp=webp",
    "[  ](javascript:void\(0\);)",
    "https://mmbiz.qpic.cn/sz_mmbiz_png/3jb6cgHmdTQ3SV4EibUWTHbILYrEBslCIfsNarfiahkthrlSJiaZCIVQ3qFot6eCCicFcfwcUZQF1w6yv2HocB8LwQ/640?wx_fmt=png&from=appmsg",
    "https://mmbiz.qpic.cn/mmbiz_jpg/Eibll6onRQp02icSQZ9W8NH8ZFicI1RClGYicktqauUXGlaHhfxkSNjWlwkb7DV11icuKgdWFRNgmRzpgeRbeXO1WNw/640?wx_fmt=jpeg&from=appmsg",
    "https://mmbiz.qpic.cn/mmbiz_png/Eibll6onRQp2h1BFxIlaNboq5tKcPuadv1fFPuiaHp1pDOszKucQ5TozrjY7aGxUTzCjCMZEq2HvABaC30gWHBtg/640?wx_fmt=png",
    "https://mmbiz.qpic.cn/mmbiz_jpg/Eibll6onRQp0tW5iaW3Zp5kUqiblGUB31iapQUAety5RNyYrtsficQbLqr8c6j2KwMe3f9gS1XHN3qOGMNUfdPqzjicw/640?wx_fmt=jpeg&from=appmsg",
    "https://mmbiz.qpic.cn/mmbiz_png/Eibll6onRQp1O9AL4v9zPSzIziaOuzMeQrxo83TpsAlFrh4OgX0OdOA7sZXnBKtib113Zaykhe0TTGyBsLVkANYhQ/640?wx_fmt=png",
    "https://mmbiz.qpic.cn/mmbiz_png/Eibll6onRQp0GCU6BpyLuhhFvvPHqiakH1ys4UBPzKDuqYJRLQMicJIS51icafJWAwW22p6beibRZwOwjAkMxEyqGgA/640?wx_fmt=png",   
    "https://mmbiz.qpic.cn/mmbiz_png/Eibll6onRQp2M33g7NJToAiboCawS5zqzArzwBKicIq5gicePwibic7JD8O7Ldz5bapJ9frn2slj0sgfptnicMsJtE3GQ/640?wx_fmt=png&from=appmsg",
    "https://mmbiz.qpic.cn/mmbiz_png/lsKAXZ6RgktsvGMuMvLic7icxSLSrD7oxmAMplGjKd7AQNyhKt6wwJ86BkAPb25iaxhhNxDoBlmNCib3rrBcegicOJg/640?wx_fmt=png",
    "https://mmbiz.qpic.cn/mmbiz_jpg/lsKAXZ6Rgkv9o90iczSpDe2b8llB6Oe4NKUJ9yrXIXG7lWcpAx2je5cOr4xrGx55XD9vAnHGfH9UD4R1ic9Aa7icA/640?wx_fmt=jpeg",
    "https://mmbiz.qpic.cn/mmbiz_png/b96CibCt70iaajvl7fD4ZCicMcjhXMp1v6UibM134tIsO1j5yqHyNhh9arj090oAL7zGhRJRq6cFqFOlDZMleLl4pw/640?wx_fmt=png",
    "https://mmbiz.qpic.cn/mmbiz_png/lsKAXZ6RgktsvGMuMvLic7icxSLSrD7oxmAMplGjKd7AQNyhKt6wwJ86BkAPb25iaxhhNxDoBlmNCib3rrBcegicOJg/640?wx_fmt=other&from=appmsg&wxfrom=5&wx_lazy=1&wx_co=1&tp=webp",
    "https://mmbiz.qpic.cn/mmbiz_png/b96CibCt70iaajvl7fD4ZCicMcjhXMp1v6UibM134tIsO1j5yqHyNhh9arj090oAL7zGhRJRq6cFqFOlDZMleLl4pw/640?wx_fmt=other&wxfrom=5&wx_lazy=1&wx_co=1&tp=webp",
    "https://mmbiz.qpic.cn/mmbiz_gif/42Raen3ejpMc7NXLEAeku4bib0mhVQovVBk0eLBMNcp8rObmA2XVica8WpxgOxkavyI5dI155PIONrDfBJFZdq7w/640?wx_fmt=gif&from=appmsg",
    "https://mmbiz.qpic.cn/mmbiz_png/42Raen3ejpMXjPA1chic4iaUcZY9icHhbibZoO7stS2cODLhDD9b33gNCTOeuKdTTjYBDudEI2bU2qerJhibdeTL01w/640?wx_fmt=png&from=appmsg",
    "蓝星探险",
    "乐乐",
    "：  ，  ，  ，  ，  ，  ，  ，  ，  ，  ，  ，  ，  。  视频  小程序  赞  ，轻点两下取消赞  在看  ，轻点两下取消在看",
    "乐乐", 
    "杨泊",
    "保尔·柯察金",
    "扫码添加小助手",
    "预约专业顾问详细沟通",
    "预览时标签不可点",
    "微信扫一扫",
    "保瓶儿小助手",
    "https://mmbiz.qpic.cn/mmbiz_jpg/42Raen3ejpMLo0EG8XMlWgpFlUDeicrdRfhVye7ibVuBiaUucRibOia59BcVqYDqebxDKpSEmRwvtxicvicRg8EdDkFMQ/640?wx_fmt=other&wxfrom=5&wx_lazy=1&wx_co=1&tp=webp",
    "https://mmbiz.qpic.cn/mmbiz_gif/42Raen3ejpNLRIkicTgzX7eHohOoOYjojLBvt94hOaiaKQukod9Igb3soDxRdKWUUg7zQu6FJ1hYJVFDBrpJRmrA/640?wx_fmt=gif&wxfrom=5&wx_lazy=1&tp=webp",
    "https://mmbiz.qpic.cn/mmbiz_gif/42Raen3ejpNLRIkicTgzX7eHohOoOYjojLBvt94hOaiaKQukod9Igb3soDxRdKWUUg7zQu6FJ1hYJVFDBrpJRmrA/640?wx_fmt=gif&tp=webp&wxfrom=5&wx_lazy=1",
    "![](https://mmbiz.qpic.cn/mmbiz_gif/42Raen3ejpNLRIkicTgzX7eHohOoOYjojLBvt94hOaiaKQukod9Igb3soDxRdKWUUg7zQu6FJ1hYJVFDBrpJRmrA/640?wx_fmt=gif)",
    "橘子",
    "橘子",
    "橘子",
    "橘子",
      # 例如：特定的格式标记
    # 在这里添加更多关键词...内地及香港同行勿扰，谢谢！
]
# ========================================================

def clean_markdown_files(folder_path: str):
    """
    清理指定文件夹中所有md文件的markdown标记、代码块符号和自定义关键词
    
    Args:
        folder_path: 文件夹路径
    """
    # 获取文件夹中所有的md文件
    md_files = glob.glob(os.path.join(folder_path, "*.md"))
    
    if not md_files:
        print(f"❌ 在 {folder_path} 中未找到.md文件")
        return
        
    print(f"找到 {len(md_files)} 个.md文件")
    
    # 统计信息
    total_files = len(md_files)
    cleaned_files = 0
    total_markdown_count = 0
    total_backtick_count = 0
    custom_keywords_stats = {keyword: 0 for keyword in CUSTOM_KEYWORDS}
    
    # 处理每个文件
    for file_path in md_files:
        try:
            # 读取文件内容
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 统计标记数量
            markdown_count = content.count("markdown")
            backtick_count = content.count("```")
            
            # 统计自定义关键词数量
            keyword_counts = {keyword: content.count(keyword) for keyword in CUSTOM_KEYWORDS}
            
            # 如果没有需要清理的内容，跳过此文件
            if markdown_count == 0 and backtick_count == 0 and all(count == 0 for count in keyword_counts.values()):
                print(f"⏭️ 跳过: {os.path.basename(file_path)} (无需清理)")
                continue
                
            # 清理markdown标记和代码块符号
            cleaned_content = content.replace("markdown", "").replace("```", "")
            
            # 清理自定义关键词
            for keyword in CUSTOM_KEYWORDS:
                if keyword_counts[keyword] > 0:
                    cleaned_content = cleaned_content.replace(keyword, "")
                    custom_keywords_stats[keyword] += keyword_counts[keyword]
            
            # 保存清理后的内容
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(cleaned_content)
            
            # 更新统计信息
            cleaned_files += 1
            total_markdown_count += markdown_count
            total_backtick_count += backtick_count
            
            print(f"✅ 已清理: {os.path.basename(file_path)}")
            if markdown_count > 0:
                print(f"   - 清理了 {markdown_count} 个 'markdown' 标记")
            if backtick_count > 0:
                print(f"   - 清理了 {backtick_count} 个 '```' 符号")
            for keyword, count in keyword_counts.items():
                if count > 0:
                    print(f"   - 清理了 {count} 个 '{keyword}' 关键词")
            
        except Exception as e:
            print(f"❌ 处理 {os.path.basename(file_path)} 时出错: {e}")
    
    # 打印总结信息
    print("\n📊 清理完成!")
    print(f"总文件数: {total_files}")
    if cleaned_files > 0:
        print(f"处理文件数: {cleaned_files}")
        skipped_files = total_files - cleaned_files
        if skipped_files > 0:
            print(f"跳过文件数: {skipped_files}")
            
        # 只在有对应标记时显示统计信息
        if total_markdown_count > 0 or total_backtick_count > 0:
            print(f"清理标记总数:")
            if total_markdown_count > 0:
                print(f"- 'markdown' 标记: {total_markdown_count}")
                print(f"- 平均每文件: {total_markdown_count/cleaned_files:.1f} 个")
            if total_backtick_count > 0:
                print(f"- '```' 符号: {total_backtick_count}")
                print(f"- 平均每文件: {total_backtick_count/cleaned_files:.1f} 个")
        
        # 显示自定义关键词清理统计
        custom_keywords_cleaned = {k: v for k, v in custom_keywords_stats.items() if v > 0}
        if custom_keywords_cleaned:
            print("\n自定义关键词清理统计:")
            for keyword, count in custom_keywords_cleaned.items():
                print(f"- '{keyword}': {count} 个")
                print(f"  平均每文件: {count/cleaned_files:.1f} 个")
    else:
        print("没有需要清理的文件")

def clean_single_markdown_file(file_path: str) -> bool:
    """
    清理单个markdown文件
    
    Args:
        file_path: 文件路径
    Returns:
        bool: 是否成功清理
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # 统计标记数量
        markdown_count = content.count("markdown")
        backtick_count = content.count("```")
        
        # 统计自定义关键词数量
        keyword_counts = {keyword: content.count(keyword) for keyword in CUSTOM_KEYWORDS}
        
        # 如果没有需要清理的内容，返回True
        if markdown_count == 0 and backtick_count == 0 and all(count == 0 for count in keyword_counts.values()):
            return True
            
        # 清理markdown标记和代码块符号
        cleaned_content = content.replace("markdown", "").replace("```", "")
        
        # 清理自定义关键词
        for keyword in CUSTOM_KEYWORDS:
            if keyword_counts[keyword] > 0:
                cleaned_content = cleaned_content.replace(keyword, "")
        
        # 保存清理后的内容
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(cleaned_content)
        
        return True
        
    except Exception as e:
        print(f"❌ 处理文件出错 {file_path}: {e}")
        return False

if __name__ == "__main__":
    # 示例路径，实际使用时替换为你的文件夹路径
    folder_path = r"C:\Users\works\Downloads\微信公众号批量下载工具3.5\下载\蓝星探险"
    
    # 确保路径存在
    if not os.path.exists(folder_path):
        print(f"❌ 路径不存在: {folder_path}")
    else:
        clean_markdown_files(folder_path) 