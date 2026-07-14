#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI文章分类器 - 基于OpenAI API服务
"""
import json
import sys
import os
from typing import List, Dict, Any

# 添加项目根目录到路径，以便导入 openaiapi / 统一配置模块
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from .openaiapi import OpenAIAPIService
except ImportError:
    from openaiapi import OpenAIAPIService

from config import TITLE_PROMPT
from server.config import DEFAULT_CLASSIFIER_MODEL, DEFAULT_CLASSIFIER_PLATFORM

class AIClassifier:
    """
    使用OpenAI API服务对文章标题进行分类。
    """
    def __init__(self, platform: str = None, model: str = None):
        """
        初始化分类器。

        Args:
            platform (str): OpenAI API平台名称，默认从 server/config.py 读取
            model (str): 使用的模型名称，默认从 server/config.py 读取
        """
        # 未指定时使用 Web 后端统一默认值，避免旧脚本与 Web UI 配置分叉。
        if platform is None:
            platform = DEFAULT_CLASSIFIER_PLATFORM
        if model is None:
            model = DEFAULT_CLASSIFIER_MODEL
        try:
            # 加载OpenAI API配置
            config = OpenAIAPIService.load_config()
            
            # 初始化OpenAI API服务
            self.api_service = OpenAIAPIService(config)
            
            # 设置平台和模型
            self.api_service.set_platform(platform)
            self.api_service.set_model(model)

            self.platform = platform
            self.model = self.api_service.current_model
            
            print(f"🤖 [AIClassifier] 已初始化，使用 '{platform}' 平台的 '{self.model}' 模型。")
            
        except Exception as e:
            print(f"❌ [AIClassifier] 初始化失败: {e}")
            raise

    def classify_titles(self, titles: List[str]) -> List[Dict[str, Any]]:
        """
        对文章标题进行分类。

        Args:
            titles (List[str]): 文章标题列表

        Returns:
            List[Dict[str, Any]]: 分类结果列表
        """
        if not titles:
            return []

        # 构建提示词
        titles_text = "\n".join([f"{i+1}. {title}" for i, title in enumerate(titles)])
        full_prompt = f"{TITLE_PROMPT}\n\n请对以下标题进行分类：\n{titles_text}"
        
        print(f"📝 [AIClassifier] 正在分类 {len(titles)} 个标题...")
        
        try:
            # 分类任务优先使用非流式，便于网关稳定返回完整 JSON。
            use_stream = self.platform not in {"poe", "chatnp_gemini"}
            response_content = self.api_service.generate_text(
                prompt=full_prompt,
                stream=use_stream,
                max_tokens=4000,
                temperature=0.1,
            )
            
            if not response_content:
                print("❌ [AIClassifier] 未收到有效响应")
                return []
            
            print("\n✅ [AIClassifier] 响应接收完毕，开始解析内容...")
            
            # 解析JSON响应
            content_str = response_content.strip()
            
            # 清理可能的markdown代码块包装
            if content_str.startswith('```json'):
                content_str = content_str[7:]
            if content_str.endswith('```'):
                content_str = content_str[:-3]
            content_str = content_str.strip()
            
            # 找到JSON数组的开始和结束位置
            start_index = content_str.find('[')
            end_index = content_str.rfind(']')
            
            if start_index == -1 or end_index == -1:
                print("❌ [AIClassifier] 响应中未找到有效的JSON数组")
                return []
            
            json_str = content_str[start_index:end_index + 1]
            
            try:
                classifications = json.loads(json_str)
                if not isinstance(classifications, list):
                    print("❌ [AIClassifier] 解析的结果不是列表格式")
                    return []
                
                print(f"✅ [AIClassifier] 成功解析 {len(classifications)} 个分类结果")
                return classifications
                
            except json.JSONDecodeError as e:
                print(f"❌ [AIClassifier] JSON解析失败: {e}")
                print(f"尝试解析的内容: {json_str[:500]}...")
                return []
                
        except Exception as e:
            print(f"❌ [AIClassifier] 分类过程出错: {e}")
            return []

# 测试函数
def test_ai_classifier():
    """测试AI分类器"""
    print("=" * 60)
    print("🚀 开始测试 AI分类器")
    print("=" * 60)
    
    try:
        # 创建分类器实例（使用统一默认配置）
        classifier = AIClassifier()
        
        # 测试标题
        test_titles = [
            "友邦 VS 永明 | 50岁规划好，养老每月2万入账，身后还能留1000万给孩子！",
            "突发！汇丰叫停内地开户，跨境投资三件套危了？",
            "中年中产养老样本：一笔50万，养老用不尽，还能留7000万给孩子",
            "详解｜香港保险的「分红锁定」，原来这么好用！"
        ]
        
        print(f"📝 测试标题: {len(test_titles)} 个")
        for i, title in enumerate(test_titles, 1):
            print(f"  {i}. {title}")
        
        # 执行分类
        results = classifier.classify_titles(test_titles)
        
        if results:
            print(f"\n✅ 分类结果: {len(results)} 个")
            for result in results:
                print(f"  📋 {result.get('type', '未知')}: {result.get('title', '无标题')}")
                print(f"     理由: {result.get('why', '无说明')}")
                print()
        else:
            print("❌ 分类失败")
            
    except Exception as e:
        print(f"❌ 测试失败: {e}")
    
    print("=" * 60)
    print("🎉 测试完成")
    print("=" * 60)

if __name__ == "__main__":
    test_ai_classifier() 
