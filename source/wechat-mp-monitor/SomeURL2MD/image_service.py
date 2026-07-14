import os
import sys
import re
from typing import Optional, List, Dict
from dataclasses import dataclass
from PIL import Image
import cv2
import numpy as np

# 修改为当前目录导入
from qr_scanner_service import QRCodeScannerService
from qwen_ocr_plus import PoeImageAnalyzerPlus

@dataclass
class ImageProcessingResult:
    """图片处理结果数据类"""
    success: bool
    message: str
    output_path: Optional[str] = None
    metadata: Optional[Dict] = None

class WeChatUploader:
    """微信图床上传器"""
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self.access_token = None
    
    def get_access_token(self):
        """获取微信access_token"""
        import requests
        url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={self.app_id}&secret={self.app_secret}"
        response = requests.get(url)
        result = response.json()
        
        if 'access_token' in result:
            self.access_token = result['access_token']
            return self.access_token
        else:
            # 处理IP白名单错误
            if result.get('errcode') == 40164:
                ip = result.get('errmsg', '').split('invalid ip ')[-1].split(' ')[0]
                raise Exception(
                    f"IP未授权错误: {ip}\n"
                    f"请前往微信公众平台 -> 设置与开发 -> 基本配置 -> IP白名单\n"
                    f"将此IP添加到白名单中后重试"
                )
            else:
                raise Exception(f"获取access_token失败: {result}")
    
    def upload_image(self, image_path: str) -> Dict:
        """上传图片到微信公众号"""
        import requests
        
        if not self.access_token:
            self.get_access_token()
            
        url = f"https://api.weixin.qq.com/cgi-bin/material/add_material?access_token={self.access_token}&type=image"
        
        with open(image_path, 'rb') as f:
            files = {'media': f}
            response = requests.post(url, files=files)
            result = response.json()
            
            if 'media_id' in result:
                return result
            else:
                raise Exception(f"上传图片失败: {result}")

class ImageService:
    """图片处理基础服务
    
    提供基础的图片处理功能，包括：
    - 图片格式转换
    - 图片压缩
    - 图片元数据处理
    """
    
    def __init__(self, log_callback=None):
        """初始化图片处理服务
        
        Args:
            log_callback: 日志回调函数，用于记录处理过程
        """
        self.qr_scanner = QRCodeScannerService()
        self.image_analyzer = PoeImageAnalyzerPlus()
        self.log_callback = log_callback
        self.wechat_uploader = WeChatUploader(
            app_id="wxc9ef51f6143c4165",
            app_secret="d04c3d7d98d1657e507cb65fa6d0adcd"
        )
        
    def log(self, message: str):
        """记录日志
        
        Args:
            message: 日志消息
        """
        if self.log_callback:
            self.log_callback(message)
        
    def extract_images_from_md(self, content: str) -> List[str]:
        """从Markdown内容中提取所有图片链接
        
        Args:
            content: Markdown文本内容
            
        Returns:
            List[str]: 图片URL列表
        """
        pattern = r'!\[.*?\]\((.*?)\)'
        return re.findall(pattern, content)
        
    def detect_qrcode(self, image_path: str) -> bool:
        """检测图片是否包含二维码
        
        Args:
            image_path: 图片路径
            
        Returns:
            bool: 是否包含二维码
        """
        try:
            result = self.qr_scanner.detect_qr_code(image_path)
            return result.has_qr
        except Exception as e:
            self.log(f"二维码检测失败: {str(e)}")
            return False

    def should_detect_qrcode(self, image_index: int, total_images: int, min_size_threshold: int = 80000) -> bool:
        """判断是否需要对指定位置的图片进行二维码检测
        
        Args:
            image_index: 图片在列表中的索引（从0开始）
            total_images: 图片总数
            min_size_threshold: 小图片像素阈值，小于此值的图片总是检测二维码
            
        Returns:
            bool: 是否需要检测二维码
        """
        # 如果图片总数很少，全部检测
        if total_images <= 6:
            return True
            
        # 检测前3张和后3张
        is_first_three = image_index < 3
        is_last_three = image_index >= total_images - 3
        
        return is_first_three or is_last_three

    def detect_qrcode_smart(self, image_path: str, image_index: int, total_images: int) -> bool:
        """智能二维码检测：只检测前3张和后3张图片
        
        Args:
            image_path: 图片路径
            image_index: 图片在列表中的索引（从0开始）
            total_images: 图片总数
            
        Returns:
            bool: 是否包含二维码（如果不需要检测则返回False）
        """
        # 首先检查图片大小，小图片总是检测二维码
        try:
            with Image.open(image_path) as img:
                width, height = img.size
                total_pixels = width * height
                
                # 小于8万像素的图片总是检测二维码
                if total_pixels < 80000:
                    self.log(f"小图片({total_pixels}像素)，执行二维码检测")
                    return self.detect_qrcode(image_path)
        except Exception as e:
            self.log(f"获取图片尺寸失败: {str(e)}")
            
        # 判断是否需要检测二维码
        if not self.should_detect_qrcode(image_index, total_images):
            self.log(f"图片位置[{image_index+1}/{total_images}]不在检测范围内，跳过二维码检测")
            return False
            
        # 执行二维码检测
        self.log(f"图片位置[{image_index+1}/{total_images}]在检测范围内，执行二维码检测")
        return self.detect_qrcode(image_path)
            
    def compress_for_ocr(self, image_path: str) -> Optional[str]:
        """压缩图片到适合OCR的大小（<5MB）
        
        Args:
            image_path: 原图路径
            
        Returns:
            Optional[str]: 压缩后的图片路径，失败返回None
        """
        try:
            # 获取原图大小（字节）
            file_size = os.path.getsize(image_path)
            max_size = 5 * 1024 * 1024  # 5MB
            
            # 如果已经小于5MB，直接返回原图路径
            if file_size <= max_size:
                return image_path
                
            # 读取图片
            img = Image.open(image_path)
            
            # 计算压缩比例
            ratio = max_size / file_size
            quality = int(100 * ratio)  # 初始质量
            quality = max(quality, 30)  # 最低质量30%
            
            # 生成临时文件路径
            temp_dir = os.path.dirname(image_path)
            temp_path = os.path.join(temp_dir, f"compressed_{os.path.basename(image_path)}")
            
            # 压缩并保存
            img.save(temp_path, "JPEG", quality=quality, optimize=True)
            
            # 检查压缩后的大小
            compressed_size = os.path.getsize(temp_path)
            if compressed_size > max_size:
                # 如果还是太大，继续降低质量
                while compressed_size > max_size and quality > 30:
                    quality -= 10
                    img.save(temp_path, "JPEG", quality=quality, optimize=True)
                    compressed_size = os.path.getsize(temp_path)
            
            self.log(f"图片已压缩: {file_size/1024/1024:.1f}MB -> {compressed_size/1024/1024:.1f}MB (质量: {quality}%)")
            return temp_path
            
        except Exception as e:
            self.log(f"图片压缩失败: {str(e)}")
            return None
            
    def perform_ocr(self, image_path: str) -> Optional[str]:
        """执行OCR识别
        
        Args:
            image_path: 图片路径
            
        Returns:
            Optional[str]: OCR识别结果文本，失败返回None
        """
        try:
            # 先压缩图片
            compressed_path = self.compress_for_ocr(image_path)
            if not compressed_path:
                return None
                
            # 使用压缩后的图片进行OCR
            result = self.image_analyzer.analyze_image(compressed_path)
            
            # 如果是临时压缩文件，删除它
            if compressed_path != image_path:
                try:
                    os.remove(compressed_path)
                except:
                    pass
                    
            return result
            
        except Exception as e:
            self.log(f"OCR识别失败: {str(e)}")
            return None
            
    def validate_image(self, image_path: str) -> bool:
        """验证图片是否满足处理条件
        
        Args:
            image_path: 图片路径
            
        Returns:
            bool: 是否满足条件
        """
        try:
            with Image.open(image_path) as img:
                width, height = img.size
                # 检查图片尺寸是否过小（阈值从200调整为100，允许更多图片通过验证）
                if width < 100 or height < 100:
                    self.log(f"图片尺寸过小: {width}x{height}")
                    return False
                return True
        except Exception as e:
            self.log(f"图片验证失败: {str(e)}")
            return False
            
    def download_image(self, url: str, save_dir: str) -> Optional[str]:
        """下载图片
        
        Args:
            url: 图片URL
            save_dir: 保存目录
            
        Returns:
            Optional[str]: 保存的文件路径，失败返回None
        """
        import requests
        import uuid
        
        try:
            # 创建保存目录
            os.makedirs(save_dir, exist_ok=True)
            
            # 下载图片
            response = requests.get(url, timeout=30)  # 增加图片下载超时时间
            response.raise_for_status()
            
            # 生成文件名
            filename = f"{uuid.uuid4().hex}.jpg"
            filepath = os.path.join(save_dir, filename)
            
            # 保存图片
            with open(filepath, 'wb') as f:
                f.write(response.content)
                
            return filepath
            
        except Exception as e:
            self.log(f"下载图片失败: {str(e)}")
            return None
            
    def convert_format(self, input_path: str, output_format: str) -> ImageProcessingResult:
        """转换图片格式
        
        Args:
            input_path: 输入图片路径
            output_format: 目标格式(jpg, png等)
            
        Returns:
            ImageProcessingResult: 处理结果
        """
        try:
            img = Image.open(input_path)
            output_path = os.path.splitext(input_path)[0] + f".{output_format}"
            img.save(output_path, output_format.upper())
            return ImageProcessingResult(
                success=True,
                message="格式转换成功",
                output_path=output_path
            )
        except Exception as e:
            return ImageProcessingResult(
                success=False,
                message=f"格式转换失败: {str(e)}"
            )
            
    def compress_image(self, input_path: str, quality: int = 85) -> ImageProcessingResult:
        """压缩图片
        
        Args:
            input_path: 输入图片路径
            quality: 压缩质量(1-100)
            
        Returns:
            ImageProcessingResult: 处理结果
        """
        try:
            img = Image.open(input_path)
            output_path = os.path.splitext(input_path)[0] + "_compressed.jpg"
            img.save(output_path, "JPEG", quality=quality, optimize=True)
            return ImageProcessingResult(
                success=True,
                message="压缩成功",
                output_path=output_path
            )
        except Exception as e:
            return ImageProcessingResult(
                success=False,
                message=f"压缩失败: {str(e)}"
            )
            
    def get_image_metadata(self, image_path: str) -> Dict:
        """获取图片元数据
        
        Args:
            image_path: 图片路径
            
        Returns:
            Dict: 图片元数据
        """
        try:
            with Image.open(image_path) as img:
                return {
                    "format": img.format,
                    "mode": img.mode,
                    "size": img.size,
                    "width": img.width,
                    "height": img.height,
                    "is_animated": getattr(img, "is_animated", False),
                    "n_frames": getattr(img, "n_frames", 1)
                }
        except Exception as e:
            return {"error": str(e)}

    def create_rounded_mask(self, width: int, height: int, radius: int) -> np.ndarray:
        """创建圆角遮罩"""
        mask = np.zeros((height, width), dtype=np.uint8)
        radius = min(radius, height // 2)
        
        # 填充中间矩形部分
        mask[:, radius:width-radius] = 255
        
        # 绘制左右两边的圆角
        for i in range(radius):
            for j in range(height):
                # 左边的圆角
                x = i
                y = j
                if (x - radius) ** 2 + (y - height//2) ** 2 <= radius ** 2:
                    mask[j, i] = 255
                
                # 右边的圆角
                x = width - i - 1
                if (x - (width-radius-1)) ** 2 + (y - height//2) ** 2 <= radius ** 2:
                    mask[j, x] = 255
        
        return mask

    def remove_watermark(self, image_path: str, output_path: str = None, radius_ratio: float = 0.5) -> ImageProcessingResult:
        """去除图片水印
        
        Args:
            image_path: 输入图片路径
            output_path: 输出图片路径，如果不指定则在原图同目录生成
            radius_ratio: 圆角半径比例
            
        Returns:
            ImageProcessingResult: 处理结果
        """
        try:
            # 读取图片
            image = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                return ImageProcessingResult(False, "图片加载失败")
            
            # 检查图片条件
            img_height, img_width = image.shape[:2]
            total_pixels = img_height * img_width
            
            if total_pixels <= 80000:
                return ImageProcessingResult(False, f"图片像素数({total_pixels})小于8万，不处理")
            
            aspect_ratio = max(img_height, img_width) / min(img_height, img_width)
            if aspect_ratio > 8:
                return ImageProcessingResult(False, f"图片长宽比({aspect_ratio:.2f})大于8，不处理")
            
            # 计算水印区域
            bottom_area_height = int(img_width * 0.1)
            right_margin = int(img_width * 0.026)
            watermark_height = int(bottom_area_height * 0.42)
            watermark_width = int(img_width * 0.32)
            bottom_margin = int(bottom_area_height * 0.20)
            
            y_start = img_height - bottom_margin - watermark_height
            x_start = img_width - watermark_width - right_margin
            
            # 创建圆角遮罩
            radius = int(watermark_height * radius_ratio)
            mask = self.create_rounded_mask(watermark_width, watermark_height, radius)
            
            # 提取ROI区域
            roi = image[y_start:y_start + watermark_height, x_start:x_start + watermark_width].copy()
            
            # 强烈模糊
            blurred_roi = cv2.GaussianBlur(roi, (45, 45), 10)
            
            # 使用遮罩合并原图和模糊图
            mask_3d = np.stack([mask] * 3, axis=2) / 255.0
            roi_result = blurred_roi * mask_3d + roi * (1 - mask_3d)
            
            # 将处理后的区域放回原图
            image[y_start:y_start + watermark_height, x_start:x_start + watermark_width] = roi_result
            
            # 保存结果
            if output_path is None:
                output_path = os.path.join(
                    os.path.dirname(image_path),
                    f"watermark_removed_{os.path.basename(image_path)}"
                )
            
            _, buffer = cv2.imencode('.jpg', image)
            buffer.tofile(output_path)
            
            return ImageProcessingResult(
                success=True,
                message="水印去除成功",
                output_path=output_path
            )
            
        except Exception as e:
            return ImageProcessingResult(
                success=False,
                message=f"水印去除失败: {str(e)}"
            )

    def is_animated_gif(self, image_path: str) -> bool:
        """检查是否为动态GIF图片
        
        Args:
            image_path: 图片路径
            
        Returns:
            bool: 是否为动态GIF
        """
        try:
            with Image.open(image_path) as img:
                # 检查是否为GIF且是动图
                is_gif = img.format == 'GIF'
                is_animated = getattr(img, "is_animated", False)
                if is_gif and is_animated:
                    self.log(f"检测到动态GIF图片: {os.path.basename(image_path)}")
                    return True
                return False
        except Exception as e:
            self.log(f"GIF检测失败: {str(e)}")
            return False

    def process_and_upload_image(self, image_path: str) -> ImageProcessingResult:
        """处理图片水印并上传到微信图床
        
        Args:
            image_path: 图片路径
            
        Returns:
            ImageProcessingResult: 处理结果，metadata中包含上传后的URL
        """
        try:
            # 检查是否为动态GIF
            if self.is_animated_gif(image_path):
                return ImageProcessingResult(
                    success=True,
                    message="动态GIF图片，跳过水印处理",
                    metadata={"is_gif": True}
                )
            
            # 1. 去除水印
            result = self.remove_watermark(image_path)
            if not result.success:
                return result
            
            # 2. 上传到微信图床
            upload_result = self.wechat_uploader.upload_image(result.output_path)
            
            # 3. 删除临时文件
            try:
                os.remove(result.output_path)
            except:
                pass
            
            return ImageProcessingResult(
                success=True,
                message="图片处理并上传成功",
                metadata={
                    "media_id": upload_result.get("media_id"),
                    "url": upload_result.get("url"),
                    "is_gif": False
                }
            )
            
        except Exception as e:
            return ImageProcessingResult(
                success=False,
                message=f"图片处理或上传失败: {str(e)}"
            ) 