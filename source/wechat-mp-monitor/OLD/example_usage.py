#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SomeURL2MD 使用示例
展示全并行表格/配图识别流程：AI 视觉模型识别（复杂表格同样为 AI 直接输出）
"""

import asyncio
from SomeURL2MD import SomeURL2MD

def example_single_url():
    """单个URL处理示例"""
    print("=" * 60)
    print("📝 单个URL处理示例")
    print("=" * 60)
    
    # 初始化工具
    tool = SomeURL2MD()
    
    # 示例URL（请替换为实际的微信文章URL）
    url = "https://mp.weixin.qq.com/s/your-article-url-here"
    
    print(f"处理URL: {url}")
    print("注意：请将上面的URL替换为实际的微信文章URL")

def example_multiple_urls():
    """多个URL批量处理示例"""
    print("=" * 60)
    print("📝 多个URL批量处理示例")
    print("=" * 60)
    
    # 示例URL列表（请替换为实际的微信文章URL）
    urls = [
        "https://mp.weixin.qq.com/s/article-1-url",
        "https://mp.weixin.qq.com/s/article-2-url",
        "https://mp.weixin.qq.com/s/article-3-url"
    ]
    
    print("批量处理URLs:")
    for i, url in enumerate(urls, 1):
        print(f"  {i}. {url}")
    print("注意：请将上面的URLs替换为实际的微信文章URLs")

async def example_processing_flow():
    """展示全并行处理流程"""
    print("=" * 60)
    print("🚀 全并行处理流程说明")
    print("=" * 60)
    
    print("🔥 革命性架构：每张图片独立并行处理完整流水线")
    print()
    
    print("单图片处理流程：")
    print("  1️⃣ AI初审（Gemini 2.5 Flash Lite）")
    print("     • 识别图片内容并判断复杂度")
    print("     • 简单图片直接返回结果")
    print()
    
    print("  2️⃣ 复杂表格 / 多列表格")
    print("     • 同样由主 AI 模型输出 Markdown，无第三方 OCR")
    print()
    
    print("🚀 全并行优势：")
    print("  • 所有图片同时处理，无需等待")
    print("  • 每张图独立跑完整识别流水线")
    print("  • 最大化利用系统资源")
    print("  • 处理时间大幅缩短")

def example_features():
    """功能特点展示"""
    print("=" * 60)
    print("✨ 功能特点")
    print("=" * 60)
    
    features = [
        "🚀 全并行架构：每张图片独立并行处理完整流水线",
        "🎯 表格与配图：由主 AI 视觉模型统一识别为 Markdown",
        "⚡ 极速处理：多图并行、主备模型容错",
        "🧠 统一稳定模型：使用经过验证的稳定主模型处理所有任务",
        "🔍 智能复杂度检测：自动识别表格复杂程度",
        "💾 智能缓存机制：避免重复处理相同内容",
        "🛡️ 错误容错处理：多重备选方案确保稳定性",
        "📝 标准化输出：HTML注释格式OCR结果",
        "🌐 多URL批量处理：支持批量文章转换",
        "🔄 完美兼容：旧代码无需修改，自动使用新架构"
    ]
    
    for feature in features:
        print(f"  {feature}")

def example_configuration():
    """配置说明"""
    print("=" * 60)
    print("⚙️ 配置说明")
    print("=" * 60)
    
    print("模型配置：")
    print("  • 主AI模型：gemini-2.5-flash-lite（用于初审和融合）")
    print("  • API基础URL：http://doubao.zwchat.cn/v1")
    print("  • 稳定性优化：统一使用主模型确保稳定性")
    
    print("\n处理配置：")
    print("  • 全并行架构：每张图片独立处理")
    print("  • 最大并发数：30个线程")
    print("  • 重试次数：3次")
    print("  • 复杂表格阈值：>8列或列数不一致")
    
    print("\n说明：")
    print("  • 已不再依赖第三方表格 OCR 服务")
    print("  • 支持图片缓存和去重（以实际 SomeURL2MD 实现为准）")

async def main():
    """主函数"""
    print("🎉 SomeURL2MD - 全并行架构版使用指南")
    
    # 展示处理流程
    await example_processing_flow()
    
    # 展示功能特点
    example_features()
    
    # 展示配置信息
    example_configuration()
    
    # 展示使用示例
    example_single_url()
    example_multiple_urls()
    
    print("\n" + "=" * 60)
    print("🚀 开始使用")
    print("=" * 60)
    print("运行命令：python SomeURL2MD.py")
    print("然后按提示输入微信文章URL即可开始处理")
    
    print("\n💡 提示：")
    print("  • 确保网络连接稳定")
    print("  • 确保API密钥有足够额度")
    print("  • 复杂表格处理时间较长，请耐心等待")
    print("  • 处理结果将保存在带时间戳的目录中")

if __name__ == "__main__":
    asyncio.run(main()) 