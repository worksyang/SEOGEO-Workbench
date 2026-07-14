#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试OCR配置脚本
用于验证模型配置是否正确
"""

import sys
import os

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from SomeURL2MD.openai_concurrent_service import OpenAIConcurrentService, MODEL_CONFIG

def test_config():
    """测试配置"""
    print("\n" + "="*60)
    print("🔍 OCR模型配置测试")
    print("="*60)
    
    print("\n📋 当前配置:")
    print(f"  主平台: {MODEL_CONFIG.get('primary_platform')}")
    print(f"  主模型: {MODEL_CONFIG.get('primary_model')}")
    print(f"  备用模型1: {MODEL_CONFIG.get('fallback_model_1')} ({MODEL_CONFIG.get('fallback_model_1_platform')})")
    print(f"  备用模型2: {MODEL_CONFIG.get('fallback_model_2')} ({MODEL_CONFIG.get('fallback_model_2_platform')})")
    print(f"  处理模式: {MODEL_CONFIG.get('processing_mode')}")
    
    print("\n🔧 初始化OCR服务...")
    try:
        ocr_service = OpenAIConcurrentService(max_workers=1, max_retries=3)
        
        print("\n✅ 服务初始化成功!")
        print(f"  实际使用的主模型: {ocr_service.model}")
        print(f"  API Base URL: {ocr_service.base_url}")
        print(f"  并发数: {ocr_service.max_workers}")
        
        if hasattr(ocr_service.png2md_converter, 'fallback_models'):
            print(f"\n🔄 备用模型配置:")
            for idx, fallback in enumerate(ocr_service.png2md_converter.fallback_models, 1):
                print(f"  备用模型{idx}: {fallback['name']}")
                print(f"    - Base URL: {fallback['base_url']}")
        
        print("\n" + "="*60)
        print("✅ 配置测试完成！")
        print("="*60)
        
    except Exception as e:
        print(f"\n❌ 初始化失败: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_config()
