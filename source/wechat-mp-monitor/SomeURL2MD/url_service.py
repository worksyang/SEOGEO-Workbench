import os
import re
import csv
from datetime import datetime
from wechat_to_markdown import WechatToMarkdownService

class URLService:
    def __init__(self):
        self.converter = WechatToMarkdownService()
    
    def validate_url(self, url: str) -> bool:
        """验证URL是否为有效的微信文章链接"""
        url = url.strip()
        # 微信文章URL通常以 https://mp.weixin.qq.com/s 开头，后面可能跟着各种参数
        return url.startswith('https://mp.weixin.qq.com/s') or url.startswith('http://mp.weixin.qq.com/s')
    
    def clean_url(self, url: str) -> str:
        """清理URL，去除空白字符和多余字符"""
        return url.strip()
    
    def convert_single_url(self, url, output_dir=None, custom_filename=None):
        """转换单个URL
        如果不指定output_dir，则使用当前目录
        如果指定custom_filename，则使用自定义文件名而不是文章标题
        """
        print(f"🔄 [URL_SERVICE] 开始转换单个URL...")
        print(f"📝 [URL_SERVICE] 参数: url={url}, output_dir={output_dir}, custom_filename={custom_filename}")
        
        if not output_dir:
            output_dir = os.getcwd()
            print(f"📁 [URL_SERVICE] 使用默认输出目录: {output_dir}")
        
        # 清理URL    
        url = self.clean_url(url)
        print(f"🧹 [URL_SERVICE] 清理后的URL: {url}")
        
        # 验证URL
        if not self.validate_url(url):
            print(f"❌ [URL_SERVICE] URL验证失败: {url}")
            return {
                'url': url,
                'success': False,
                'error': '无效的微信文章URL'
            }
        
        print(f"✅ [URL_SERVICE] URL验证通过")
            
        try:
            print(f"🚀 [URL_SERVICE] 开始处理URL: {url}")
            
            # 添加时间戳到文件名而不是目录
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            print(f"⏰ [URL_SERVICE] 生成时间戳: {timestamp}")
            
            try:
                print(f"📡 [URL_SERVICE] 调用转换器进行转换...")
                output_path, article_title = self.converter.url_to_markdown(
                    url, output_dir
                )
                print(f"📊 [URL_SERVICE] 转换器返回结果:")
                print(f"  - 输出路径: {output_path}")
                print(f"  - 文章标题: {article_title}")
                
                if not output_path or not article_title:
                    print(f"❌ [URL_SERVICE] 转换结果无效")
                    raise Exception("转换失败：未能获取文章内容")
                    
                if article_title == "未命名文章":
                    print("⚠️ [URL_SERVICE] 警告：无法获取文章标题，原文可能已被删除或设为私密。")
                
                if output_path:
                    # 检查原文件名是否已经包含时间戳（格式：_YYYYMMDD_HHMMSS）
                    original_filename = os.path.basename(output_path)
                    name_without_ext = os.path.splitext(original_filename)[0]
                    
                    # 检查是否已经包含时间戳格式（_YYYYMMDD_HHMMSS 或 _YYYYMMDDHHMMSS）
                    timestamp_pattern = r'_\d{8}_\d{6}$|_\d{14}$'
                    has_timestamp = bool(re.search(timestamp_pattern, name_without_ext))
                    
                    # 如果指定了自定义文件名，使用自定义文件名；否则使用原始逻辑
                    if custom_filename:
                        # 使用自定义文件名
                        filename = custom_filename
                        # 确保文件名安全（移除不安全字符）
                        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
                        # 如果原文件名已经有时间戳，且自定义文件名不包含时间戳，则保留原时间戳
                        if has_timestamp and not re.search(timestamp_pattern, filename):
                            # 提取原文件名中的时间戳
                            original_timestamp_match = re.search(r'(_\d{8}_\d{6}|_\d{14})$', name_without_ext)
                            if original_timestamp_match:
                                new_filename = f"{filename}{original_timestamp_match.group(1)}.md"
                            else:
                                new_filename = f"{filename}_{timestamp}.md"
                        else:
                            new_filename = f"{filename}_{timestamp}.md"
                        print(f"📝 [URL_SERVICE] 使用自定义文件名: {new_filename}")
                    else:
                        # 使用原始逻辑（文章标题）
                        # 如果原文件名已经包含时间戳，就不再追加
                        if has_timestamp:
                            new_filename = original_filename
                            print(f"📝 [URL_SERVICE] 原文件名已包含时间戳，保持原文件名: {new_filename}")
                        else:
                            filename = os.path.basename(output_path)
                            new_filename = f"{os.path.splitext(filename)[0]}_{timestamp}.md"
                            print(f"📝 [URL_SERVICE] 使用文章标题文件名: {new_filename}")
                    
                    new_path = os.path.join(output_dir, new_filename)
                    
                    print(f"📝 [URL_SERVICE] 重命名文件:")
                    print(f"  - 原文件名: {os.path.basename(output_path)}")
                    print(f"  - 新文件名: {new_filename}")
                    print(f"  - 新路径: {new_path}")
                    
                    try:
                        os.rename(output_path, new_path)
                        print(f"✅ [URL_SERVICE] 文件重命名成功: {new_path}")
                    except Exception as e:
                        print(f"❌ [URL_SERVICE] 重命名文件失败: {str(e)}")
                        new_path = output_path  # 如果重命名失败，使用原始路径
                    
                    print(f"🎉 [URL_SERVICE] 转换成功完成")
                    return {
                        'url': url,
                        'success': True,
                        'title': article_title,
                        'output_path': new_path,
                        'custom_filename': custom_filename
                    }
                
            except Exception as e:
                print(f"❌ [URL_SERVICE] 转换过程出错: {str(e)}")
                import traceback
                print(f"🔍 [URL_SERVICE] 详细错误堆栈：")
                traceback.print_exc()
                return {
                    'url': url,
                    'success': False,
                    'error': str(e)
                }
            
        except Exception as e:
            error_msg = str(e)
            print(f"❌ [URL_SERVICE] 处理URL出错: {error_msg}")
            import traceback
            print(f"🔍 [URL_SERVICE] 详细错误堆栈：")
            traceback.print_exc()
            return {
                'url': url,
                'success': False,
                'error': error_msg
            }
    
    def read_urls_from_file(self, file_path: str) -> list:
        """从文件中读取URL列表
        
        支持两种格式：
        1. TXT文件：每行一个URL
        2. CSV文件：第一列为关键词（文件名），第四列为URL
        
        Returns:
            list: 对于TXT文件返回URL字符串列表
                  对于CSV文件返回包含{'url': str, 'filename': str}的字典列表
        """
        file_ext = os.path.splitext(file_path)[1].lower()
        
        if file_ext == '.csv':
            return self._read_urls_from_csv(file_path)
        else:
            return self._read_urls_from_txt(file_path)
    
    def _read_urls_from_txt(self, file_path: str) -> list:
        """从TXT文件中读取URL列表"""
        urls = []
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                url = self.clean_url(line)
                if url and self.validate_url(url):
                    urls.append(url)
        return urls
    
    def _read_urls_from_csv(self, file_path: str) -> list:
        """从CSV文件中读取URL和文件名
        
        CSV格式：关键词,关键词备注,类型,URL,水印,公众号,公众号备注
        返回格式：[{'url': 'xxx', 'filename': 'xxx'}, ...]
        """
        url_data = []
        
        # 尝试不同的编码格式
        encodings = ['utf-8', 'gbk', 'gb2312', 'utf-8-sig', 'latin1']
        
        for encoding in encodings:
            try:
                print(f"🔍 [CSV] 尝试使用 {encoding} 编码读取文件...")
                
                with open(file_path, 'r', encoding=encoding) as f:
                    # 尝试检测CSV分隔符
                    sample = f.read(1024)
                    f.seek(0)
                    
                    # 检测分隔符
                    sniffer = csv.Sniffer()
                    try:
                        delimiter = sniffer.sniff(sample).delimiter
                    except:
                        # 如果检测失败，使用逗号作为默认分隔符
                        delimiter = ','
                    
                    f.seek(0)
                    reader = csv.reader(f, delimiter=delimiter)
                    
                    # 跳过标题行
                    next(reader, None)
                    
                    for row_num, row in enumerate(reader, 2):  # 从第2行开始计数
                        if len(row) < 4:
                            print(f"⚠️ [CSV] 第{row_num}行列数不足，跳过: {row}")
                            continue
                        
                        keyword = row[0].strip()  # 第1列：关键词
                        url = row[3].strip()      # 第4列：URL
                        
                        # 验证数据
                        if not keyword:
                            print(f"⚠️ [CSV] 第{row_num}行关键词为空，跳过")
                            continue
                        
                        if not url:
                            print(f"⚠️ [CSV] 第{row_num}行URL为空，跳过")
                            continue
                        
                        # 验证URL格式
                        if not self.validate_url(url):
                            print(f"⚠️ [CSV] 第{row_num}行URL格式无效，跳过: {url}")
                            continue
                        
                        url_data.append({
                            'url': url,
                            'filename': keyword
                        })
                        
                        print(f"✅ [CSV] 第{row_num}行解析成功: {keyword} -> {url}")
                
                print(f"✅ [CSV] 成功使用 {encoding} 编码读取文件")
                break  # 成功读取，跳出循环
                
            except UnicodeDecodeError as e:
                print(f"❌ [CSV] {encoding} 编码失败: {str(e)}")
                continue
            except Exception as e:
                print(f"❌ [CSV] 使用 {encoding} 编码读取失败: {str(e)}")
                # 如果是最后一个编码也失败了，抛出异常
                if encoding == encodings[-1]:
                    raise Exception(f"CSV文件读取失败，尝试了所有编码格式: {str(e)}")
                continue
        
        if not url_data:
            raise Exception("CSV文件读取失败：未能解析到任何有效数据")
        
        print(f"📊 [CSV] 共解析到 {len(url_data)} 条有效数据")
        return url_data
    
    def convert_multiple_urls(self, urls, output_dir, progress_callback=None):
        """批量转换URL
        
        Args:
            urls: URL列表（可以是字符串列表或字典列表）
            output_dir: 输出目录
            progress_callback: 进度回调函数
            
        Returns:
            tuple: (结果列表, 输出目录)
        """
        # 创建带时间戳的输出子目录
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        batch_output_dir = os.path.join(output_dir, f'批量下载-{timestamp}')
        os.makedirs(batch_output_dir, exist_ok=True)
        
        results = []
        total = len(urls)
        
        for idx, url_data in enumerate(urls, 1):
            # 处理不同格式的URL数据
            if isinstance(url_data, dict):
                # CSV格式：包含URL和文件名
                url = url_data['url']
                custom_filename = url_data['filename']
                display_url = f"{custom_filename} ({url})"
            else:
                # TXT格式：只有URL
                url = url_data
                custom_filename = None
                display_url = url
            
            if progress_callback:
                progress_callback(idx, total, f"正在处理: {display_url}")
                
            result = self.convert_single_url(url, batch_output_dir, custom_filename)
            results.append(result)
            
        return results, batch_output_dir 