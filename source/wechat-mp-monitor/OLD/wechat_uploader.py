import requests
import json
from PIL import Image
import os
import time
from typing import Dict, Optional

class WeChatUploader:
    """微信公众号图片上传服务
    
    用于处理图片上传到微信公众号的相关操作，包括：
    - 获取access_token
    - 图片格式转换
    - 上传图片到公众号素材库
    - 自动处理token过期和重试
    """
    
    def __init__(self, app_id: str, app_secret: str):
        """初始化微信上传器
        
        Args:
            app_id: 微信公众号的AppID
            app_secret: 微信公众号的AppSecret
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token: Optional[str] = None
        self.token_expire_time: Optional[float] = None
        self.token_duration = 7200  # 2小时 = 7200秒
        self.refresh_threshold = 5400  # 1.5小时后主动刷新 = 5400秒
    
    def convert_webp_to_png(self, webp_path: str) -> str:
        """将webp格式转换为png格式
        
        Args:
            webp_path: webp图片的路径
            
        Returns:
            str: 转换后的png图片路径
        """
        # 生成新文件名
        png_path = os.path.splitext(webp_path)[0] + '.png'
        
        # 转换图片格式
        image = Image.open(webp_path)
        image.save(png_path, 'PNG')
        
        return png_path
    
    def is_token_valid(self) -> bool:
        """检查当前token是否有效
        
        Returns:
            bool: token是否有效（未过期且距离过期时间大于阈值）
        """
        if not self.access_token or not self.token_expire_time:
            return False
        
        current_time = time.time()
        # 如果当前时间超过了刷新阈值，认为token需要刷新
        return current_time < (self.token_expire_time - (self.token_duration - self.refresh_threshold))

    def get_access_token(self) -> str:
        """获取微信access_token
        
        Returns:
            str: 获取到的access_token
            
        Raises:
            Exception: 获取access_token失败时抛出异常
        """
        print("正在获取新的access_token...")
        url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={self.app_id}&secret={self.app_secret}"
        response = requests.get(url)
        result = response.json()
        
        if 'access_token' in result:
            self.access_token = result['access_token']
            # 记录获取时间，用于计算过期时间
            current_time = time.time()
            self.token_expire_time = current_time + self.token_duration
            print(f"access_token获取成功，有效期至: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.token_expire_time))}")
            return self.access_token
        else:
            raise Exception(f"获取access_token失败: {result}")
    
    def _do_upload(self, image_path: str) -> Dict:
        """执行实际的图片上传操作
        
        Args:
            image_path: 要上传的图片路径
            
        Returns:
            Dict: 上传结果
        """
        # 如果是webp格式，先转换
        if image_path.lower().endswith('.webp'):
            print("检测到webp格式，正在转换为png...")
            image_path = self.convert_webp_to_png(image_path)
            
        url = f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={self.access_token}&type=image"
        
        # 打开图片文件
        with open(image_path, 'rb') as f:
            files = {
                'media': f
            }
            response = requests.post(url, files=files)
            result = response.json()
            return result

    def upload_image(self, image_path: str, max_retries: int = 2) -> Dict:
        """上传图片到微信公众号（支持自动重试）
        
        Args:
            image_path: 要上传的图片路径
            max_retries: 最大重试次数，默认2次
            
        Returns:
            Dict: 上传成功后的返回结果，包含media_id和url
            
        Raises:
            Exception: 上传失败时抛出异常
        """
        for attempt in range(max_retries + 1):
            try:
                # 检查token是否有效，无效则重新获取
                if not self.is_token_valid():
                    self.get_access_token()
                
                # 执行上传
                result = self._do_upload(image_path)
                
                # 检查上传结果
                if 'media_id' in result:
                    # 上传成功
                    print(f"图片上传成功: {os.path.basename(image_path)}")
                    return result
                elif result.get('errcode') == 42001:
                    # access_token过期错误
                    if attempt < max_retries:
                        print(f"access_token过期，第{attempt + 1}次重试...")
                        # 强制重新获取token
                        self.access_token = None
                        self.token_expire_time = None
                        continue
                    else:
                        raise Exception(f"上传图片失败，已达到最大重试次数: {result}")
                else:
                    # 其他错误
                    raise Exception(f"上传图片失败: {result}")
                    
            except Exception as e:
                if attempt == max_retries:
                    # 已达到最大重试次数，抛出异常
                    raise e
                else:
                    print(f"上传失败，第{attempt + 1}次重试: {str(e)}")
                    # 如果是网络错误等，也清空token重试
                    if "access_token" in str(e).lower() or "42001" in str(e):
                        self.access_token = None
                        self.token_expire_time = None

# 用于测试的入口点
if __name__ == "__main__":
    # 使用你的AppID和AppSecret
    app_id = "wxc9ef51f6143c4165"
    app_secret = "d04c3d7d98d1657e507cb65fa6d0adcd"
    
    # 创建上传器实例
    uploader = WeChatUploader(app_id, app_secret)
    
    # 上传图片
    try:
        # 替换为你要上传的图片路径
        image_path = "水印4.webp"
        result = uploader.upload_image(image_path)
        print("上传成功！")
        print(f"media_id: {result['media_id']}")
        print(f"图片URL: {result.get('url', '无URL返回')}")
    except Exception as e:
        print(f"发生错误: {str(e)}")
