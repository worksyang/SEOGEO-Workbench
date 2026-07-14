import re
import os
from typing import Dict

class MDService:
    """处理MD文件内容更新"""
    
    def __init__(self, log_callback=None):
        """初始化服务
        Args:
            log_callback: 日志回调函数，用于向UI报告处理进度
        """
        self.log_callback = log_callback
    
    def log(self, message: str):
        """输出日志"""
        if self.log_callback:
            self.log_callback(message)
    
    def update_md_content(self, content: str, image_map: Dict[str, str], keep_ocr_only: bool = False) -> str:
        """更新Markdown内容中的图片链接
        
        Args:
            content: 原始Markdown内容
            image_map: 图片URL映射 {原URL: 新URL或None}
            keep_ocr_only: 已废弃参数，保留是为了兼容性
            
        Returns:
            str: 更新后的Markdown内容
        """
        try:
            # 处理每个需要删除或替换的图片
            for old_url, new_url in image_map.items():
                if new_url is None:
                    # 需要删除的图片：只删除图片标记，保留其他内容
                    content = re.sub(r'!\[.*?\]\(' + re.escape(old_url) + r'\)', '', content)
                else:
                    # 需要替换的图片：更新图片链接
                    content = content.replace(old_url, new_url)
            
            # 清理多余的空行
            content = re.sub(r'\n{3,}', '\n\n', content)
            return content.strip() + '\n'
            
        except Exception as e:
            self.log(f"更新Markdown内容失败: {str(e)}")
            return content
    
    def clean_external_links(self, content: str) -> str:
        """清理外部链接，只保留图片链接"""
        # 保留图片链接
        def replace_link(match):
            whole = match.group(0)
            if whole.startswith('!['):  # 是图片链接
                return whole
            return ''  # 非图片链接，删除
            
        # 匹配所有Markdown链接
        pattern = r'!?\[.*?\]\(.*?\)'
        return re.sub(pattern, replace_link, content)
    
    def update_md_with_ocr(self, content: str, all_ocr_results: Dict[str, str]) -> str:
        """
        根据最终的OCR结果字典，一步到位更新整个Markdown文件。
        - 成功的OCR结果会被添加为注释。
        - 结果为None或空字符串的图片链接将被从文档中移除。
        - 结果包含"该图片无任何信息，请删除"的图片链接将被从文档中移除。
        
        重要：只更新本次处理的图片，不影响其他已有OCR注释的图片
        """
        self.log(f"📝 开始更新MD文件，收到 {len(all_ocr_results)} 个OCR结果")
        
        # 1. 只清理本次处理的图片的旧OCR注释，保留其他图片的OCR注释
        # 这样可以避免删除已有的、不在本次处理列表中的图片的OCR注释
        for url in all_ocr_results.keys():
            # 转义URL中的特殊字符
            escaped_url = re.escape(url)
            # 匹配该图片后的OCR注释
            pattern = rf'(!\[.*?\]\({escaped_url}\))\s*\n*\s*<!--\s*OCR内容：.*?-->\s*\n*'
            content = re.sub(pattern, r'\1\n', content, flags=re.DOTALL)
        
        # 清理多余的空行
        content = re.sub(r'\n{3,}', '\n\n', content)

        # 2. 准备一个图片URL到新内容的映射
        image_update_map = {}
        for url, result in all_ocr_results.items():
            if result and result.strip():
                # 检查是否包含"该图片无任何信息，请删除"
                if "该图片无任何信息，请删除" in result:
                    # 标记为需要删除的图片
                    image_update_map[url] = None
                    self.log(f"  - 准备移除图片: {url[:50]}... (OCR结果指示删除)")
                else:
                    # 格式化为标准的HTML注释
                    formatted_comment = f"\n\n<!-- OCR内容：\n{result}\n\n-->\n"
                    image_update_map[url] = formatted_comment
                    self.log(f"  - 准备添加OCR注释: {url[:50]}... (长度: {len(result)})")
            elif result is None:
                # 对于被过滤的图片（尺寸过小、包含二维码等），直接删除
                image_update_map[url] = None
                self.log(f"  - 准备删除被过滤的图片: {url[:50]}... (被预处理过滤)")
            else:
                # 对于OCR失败的图片，保留图片链接并添加失败标注
                failure_comment = f"\n\n<!-- 图片识别失败，请根据上下文自行判断该图片是否重要以及是否需要保留 -->\n"
                image_update_map[url] = failure_comment
                self.log(f"  - 准备添加失败标注: {url[:50]}... (图片识别失败)")

        # 3. 遍历文档中的图片，应用更新
        # 使用函数式替换，避免因字符串替换顺序导致的问题
        def _replacer(match):
            img_tag = match.group(0)  # 整个图片标签
            img_url = match.group(2)  # URL部分
            
            if img_url in image_update_map:
                update_action = image_update_map[img_url]
                if update_action is None:
                    # 返回空字符串以删除整个图片标签（仅限于明确要求删除的情况）
                    return ""
                else:
                    # 返回图片标签 + OCR注释或失败标注
                    return img_tag + update_action
            else:
                # 如果图片不在处理列表中，保持原样（保留已有的OCR注释）
                return img_tag

        content = re.sub(r'(!\[.*?\]\((.*?)\))', _replacer, content)

        # 5. 最后，清理因删除图片可能导致的多余空行
        content = re.sub(r'\n{3,}', '\n\n', content)
        
        # 6. 统计最终结果
        final_image_count = len(re.findall(r'!\[.*?\]\(.*?\)', content))
        final_ocr_count = len(re.findall(r'<!--\s*OCR内容：', content))
        self.log(f"📊 MD更新完成: 图片 {final_image_count} 张, OCR注释 {final_ocr_count} 个")
        
        return content.strip() + '\n'

    def normalize_url(self, url):
        """规范化URL，移除查询参数和域名部分"""
        try:
            # 移除查询参数
            base_url = url.split('?')[0] if url else url
            # 提取文件名部分
            parts = base_url.split('/')
            filename = parts[-1] if parts else ''
            self.log(f"URL规范化: {url} -> {filename}")
            return filename
        except Exception as e:
            self.log(f"URL规范化失败: {str(e)}")
            return url
    
    def add_ocr_comments(self, content: str, ocr_results: dict, keep_ocr_only: bool = False) -> str:
        """在图片后添加OCR注释"""
        self.log("\n============= 开始处理OCR注释 =============")
        self.log(f"收到 {len(ocr_results)} 个OCR结果")
        
        # 打印原始OCR结果的URL
        self.log("\n【原始OCR结果URL】")
        for url in ocr_results.keys():
            self.log(f"• {url}")
        
        # 获取所有图片URL
        image_matches = list(re.finditer(r'!\[.*?\]\((.*?)\)', content))
        self.log(f"\n【文档中的图片URL】")
        
        # 创建图片URL到OCR结果的映射
        image_ocr_map = {}
        for idx, match in enumerate(image_matches, 1):
            img_url = match.group(1)
            self.log(f"\n图片 {idx}:")
            self.log(f"原始URL: {img_url}")
            
            # 直接使用完整URL匹配
            if img_url in ocr_results:
                image_ocr_map[img_url] = ocr_results[img_url]
                self.log(f"✓ 找到对应OCR结果，长度: {len(ocr_results[img_url])}")
            else:
                self.log("✗ 未找到对应OCR结果")
        
        # 首先删除所有已存在的OCR注释
        self.log("\n【清理现有OCR注释】")
        content = re.sub(
            r'\n*<!--\s*OCR内容：\n.*?-->\n*',
            '\n',
            content,
            flags=re.DOTALL
        )
        
        # 清理多余的空行
        content = re.sub(r'\n{3,}', '\n\n', content)
        
        # 处理每个图片的OCR结果
        self.log("\n【开始处理图片OCR注释】")
        result_lines = []
        current_pos = 0
        
        for match in image_matches:
            # 添加图片前的内容
            result_lines.append(content[current_pos:match.start()])
            
            # 添加图片标记
            img_line = content[match.start():match.end()]
            result_lines.append(img_line)
            
            # 获取并添加OCR结果
            img_url = match.group(1)
            if img_url in image_ocr_map:
                ocr_result = image_ocr_map[img_url]
                if ocr_result:
                    self.log(f"\n添加OCR结果 - URL: {img_url}")
                    self.log(f"OCR内容长度: {len(ocr_result)}")
                    result_lines.append('\n')
                    result_lines.append(ocr_result)
                    result_lines.append('\n')
            
            current_pos = match.end()
        
        # 添加剩余内容
        result_lines.append(content[current_pos:])
        
        # 合并所有内容
        content = ''.join(result_lines)
        
        # 清理多余的空行
        content = re.sub(r'\n{3,}', '\n\n', content)
        
        # 检查最终结果
        self.log("\n【最终结果检查】")
        final_image_count = len(re.findall(r'!\[.*?\]\(.*?\)', content))
        final_ocr_count = len(re.findall(r'<!--\s*OCR内容：', content))
        self.log(f"最终图片数量: {final_image_count}")
        self.log(f"最终OCR注释数量: {final_ocr_count}")
        
        return content.strip() + '\n'
    
    def read_md_file(self, file_path: str) -> str:
        """读取MD文件内容"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            self.log(f"读取MD文件失败: {str(e)}")
            raise Exception(f"读取MD文件失败: {str(e)}")
    
    def write_md_file(self, file_path: str, content: str):
        """写入MD文件内容"""
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
        except Exception as e:
            self.log(f"写入MD文件失败: {str(e)}")
            raise Exception(f"写入MD文件失败: {str(e)}") 