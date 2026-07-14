import zxingcpp
from PIL import Image
import cv2
import numpy as np
import os
from typing import Dict, List, Optional, Union
from dataclasses import dataclass

@dataclass
class QRScanResult:
    """二维码扫描结果数据类"""
    success: bool
    has_qr: bool
    content: Optional[str]
    error: Optional[str]

@dataclass
class DirectoryScanResult:
    """目录扫描结果数据类"""
    file_path: str
    result: QRScanResult

class QRCodeScannerService:
    """二维码扫描服务
    
    提供图片中二维码的检测和识别功能，支持：
    - 单个图片的二维码检测
    - 批量目录扫描
    - 多种图片格式支持
    - GIF动图的逐帧检测
    """
    
    def __init__(self):
        """初始化二维码扫描服务"""
        self.supported_formats = ['.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif']

    def detect_qr_code(self, image_path: str) -> QRScanResult:
        """检测图片中的二维码
        
        Args:
            image_path: 图片文件路径
            
        Returns:
            QRScanResult: 包含检测结果的数据对象
        """
        result = QRScanResult(
            success=False,
            has_qr=False,
            content=None,
            error=None
        )
        
        try:
            # 使用PIL读取图片
            with Image.open(image_path) as pil_image:
                # 如果是GIF，获取所有帧
                if getattr(pil_image, "is_animated", False):
                    frames = []
                    for i in range(pil_image.n_frames):
                        pil_image.seek(i)
                        frames.append(pil_image.copy())
                else:
                    frames = [pil_image]

                # 遍历所有帧检测二维码
                for frame in frames:
                    # 转换为OpenCV格式
                    if frame.mode == 'RGBA':
                        frame = frame.convert('RGB')
                    image = cv2.cvtColor(np.array(frame), cv2.COLOR_RGB2BGR)
                    
                    # 使用zxingcpp进行识别
                    results = zxingcpp.read_barcodes(image)
                    
                    for barcode in results:
                        if barcode.valid:
                            # 如果检测到二维码
                            result.success = True
                            result.has_qr = True
                            result.content = barcode.text
                            return result

                # 如果所有帧都没有检测到二维码
                result.success = True
                return result
                
        except Exception as e:
            result.error = str(e)
            return result

    def is_valid_image(self, file_path: str) -> bool:
        """检查文件是否为有效的图片格式
        
        Args:
            file_path: 图片文件路径
            
        Returns:
            bool: 是否为有效的图片文件
        """
        try:
            if not os.path.exists(file_path):
                return False
            file_ext = os.path.splitext(file_path)[1].lower()
            if file_ext not in self.supported_formats:
                return False
            with Image.open(file_path) as img:
                img.verify()
            return True
        except Exception as e:
            print(f"验证图片时出错 {file_path}: {str(e)}")
            return False

    def scan_directory(self, directory_path: str) -> List[DirectoryScanResult]:
        """扫描目录中的所有图片文件
        
        Args:
            directory_path: 要扫描的目录路径
            
        Returns:
            List[DirectoryScanResult]: 所有文件的扫描结果列表
        """
        results: List[DirectoryScanResult] = []
        stats = {
            'total_files': 0,
            'valid_images': 0,
            'qr_detected': 0,
            'no_qr_detected': 0,
            'error_files': 0
        }
        
        try:
            for root, _, files in os.walk(directory_path):
                for file in files:
                    stats['total_files'] += 1
                    try:
                        file_path = os.path.join(root, file)
                        if self.is_valid_image(file_path):
                            stats['valid_images'] += 1
                            result = self.detect_qr_code(file_path)
                            results.append(DirectoryScanResult(
                                file_path=file_path,
                                result=result
                            ))
                            # 统计二维码检测结果
                            if result.success:
                                if result.has_qr:
                                    stats['qr_detected'] += 1
                                else:
                                    stats['no_qr_detected'] += 1
                            else:
                                stats['error_files'] += 1
                        else:
                            print(f"无效的图片文件: {file_path}")
                            stats['error_files'] += 1
                    except Exception as e:
                        print(f"处理文件时出错 {file}: {str(e)}")
                        stats['error_files'] += 1
            
            # 打印统计信息
            self._print_scan_stats(stats)
            return results
            
        except Exception as e:
            print(f"扫描目录时出错: {str(e)}")
            return results
            
    def _print_scan_stats(self, stats: Dict[str, int]) -> None:
        """打印扫描统计信息
        
        Args:
            stats: 包含统计数据的字典
        """
        print("\n" + "="*50)
        print("处理结果统计")
        print("="*50)
        print(f"总文件数：{stats['total_files']}")
        print(f"有效图片数：{stats['valid_images']}")
        print(f"检测到二维码的图片数：{stats['qr_detected']}")
        print(f"未检测到二维码的图片数：{stats['no_qr_detected']}")
        print(f"处理失败的文件数：{stats['error_files']}")
        print("="*50 + "\n")

# 用于测试的入口点
if __name__ == "__main__":
    scanner = QRCodeScannerService()
    directory_path = r"\\Z2PRO-X3MD\176xxxx2259\公众号相关\二维码待识别测试"
    
    if not os.path.exists(directory_path):
        print(f"目录不存在: {directory_path}")
    else:
        print(f"开始扫描目录: {directory_path}")
        results = scanner.scan_directory(directory_path)
        
        print("详细处理结果:")
        print("="*50)
        for item in results:
            print(f"\n文件: {os.path.basename(item.file_path)}")
            if item.result.success:
                if item.result.has_qr:
                    print(f"✓ 检测到二维码，内容为: {item.result.content}")
                else:
                    print("✗ 未检测到二维码")
            else:
                print(f"! 处理出错: {item.result.error}")
        print("="*50)
