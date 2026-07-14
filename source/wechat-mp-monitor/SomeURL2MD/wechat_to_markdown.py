import requests
from bs4 import BeautifulSoup, Tag
import re
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union, Callable
from dataclasses import dataclass
import time

@dataclass
class ConversionRule:
    """HTML到Markdown的转换规则"""
    filter: Union[str, List[str]]
    replacement: Callable[[str, Tag], str]

class WechatToMarkdownService:
    """微信文章转Markdown服务
    
    提供将微信公众号文章转换为Markdown格式的功能，包括：
    - HTML到Markdown的转换
    - 图片链接处理
    - 代码块格式化
    - 表格转换
    - 列表处理
    """
    
    def __init__(self):
        """初始化转换服务，设置转换规则"""
        self.rules: Dict[str, ConversionRule] = {
            'paragraph': ConversionRule(
                filter='p',
                replacement=self._process_paragraph
            ),
            'heading': ConversionRule(
                filter=['h1', 'h2', 'h3', 'h4', 'h5', 'h6'],
                replacement=lambda content, node: f"\n\n{'#' * int(node.name[1])} {content}\n\n"
            ),
            'lineBreak': ConversionRule(
                filter='br',
                replacement=lambda content, node: '\n'
            ),
            'blockquote': ConversionRule(
                filter='blockquote',
                replacement=lambda content, node: f'\n\n> {content.strip()}\n\n'
            ),
            'list': ConversionRule(
                filter=['ul', 'ol'],
                replacement=lambda content, node: f'\n{content}\n'
            ),
            'code': ConversionRule(
                filter=['pre', 'code'],
                replacement=self._process_code
            ),
            'strong': ConversionRule(
                filter=['strong', 'b'],
                replacement=lambda content, node: f'**{content}**' if content.strip() else ''
            ),
            'emphasis': ConversionRule(
                filter=['em', 'i'],
                replacement=lambda content, node: f'_{content}_' if content.strip() else ''
            ),
            'strikethrough': ConversionRule(
                filter=['del', 's', 'strike'],
                replacement=lambda content, node: f'~~{content}~~' if content.strip() else ''
            ),
            'image': ConversionRule(
                filter='img',
                replacement=self._process_image
            ),
            'link': ConversionRule(
                filter='a',
                replacement=self._process_link
            ),
            'table': ConversionRule(
                filter='table',
                replacement=self._process_table
            ),
            'section': ConversionRule(
                filter='section',
                replacement=self._process_section
            )
        }

    def _has_close_parent(self, node: Tag) -> bool:
        """检查是否有特定的父节点
        
        Args:
            node: BeautifulSoup节点
            
        Returns:
            bool: 是否有特定的父节点
        """
        close_parents = ['li', 'td', 'th']
        return node.parent.name in close_parents if node.parent else False

    def _process_paragraph(self, content: str, node: Tag) -> str:
        """处理段落，添加适当的空行
        
        Args:
            content: 段落内容
            node: BeautifulSoup节点
            
        Returns:
            str: 处理后的Markdown文本
        """
        if not self._has_close_parent(node):
            return f'\n\n{content}\n\n'
        return content

    def _process_code(self, content: str, node: Tag) -> str:
        """处理代码块和行内代码
        
        Args:
            content: 代码内容
            node: BeautifulSoup节点
            
        Returns:
            str: 处理后的Markdown文本
        """
        if node.name == 'pre':
            code_node = node.find('code')
            # 处理代码中的特殊字符
            content = content.replace('```', '````')
            
            # 获取语言标识
            language = ''
            if code_node and 'class' in code_node.attrs:
                classes = code_node.get('class', [])
                language = next((c.replace('language-', '') for c in classes if c.startswith('language-')), '')
                
            # 确保代码块前后有正确的空行
            return f'\n\n```{language}\n{content.strip()}\n```\n\n'
        else:
            # 行内代码处理
            content = content.replace('`', '``')
            return f'`{content.strip()}`'

    def _process_table(self, content: str, node: Tag) -> str:
        """处理表格
        
        Args:
            content: 表格内容
            node: BeautifulSoup节点
            
        Returns:
            str: 处理后的Markdown文本
        """
        rows = node.find_all(['tr'])
        if not rows:
            return ''
            
        def get_alignment(cell: Tag) -> str:
            """获取单元格对齐方式"""
            align = cell.get('align', '')
            if align == 'left':
                return ':---'
            elif align == 'right':
                return '---:'
            elif align == 'center':
                return ':---:'
            return '---'
        
        # 处理表头
        header = rows[0]
        header_cells = header.find_all(['th', 'td'])
        header_text = ' | '.join(cell.get_text().strip() for cell in header_cells)
        
        # 处理对齐
        alignments = [get_alignment(cell) for cell in header_cells]
        separator = ' | '.join(alignments)
        
        # 处理表格内容
        table_rows = []
        for row in rows[1:]:
            cells = row.find_all(['td', 'th'])
            row_text = ' | '.join(cell.get_text().strip() for cell in cells)
            table_rows.append(row_text)
            
        return f'\n\n| {header_text} |\n| {separator} |\n' + '\n'.join(f'| {row} |' for row in table_rows) + '\n\n'

    def _process_section(self, content: str, node: Tag) -> str:
        """处理section，确保每个section都有换行
        
        Args:
            content: section内容
            node: BeautifulSoup节点
            
        Returns:
            str: 处理后的Markdown文本
        """
        # 检查是否有span子元素
        if node.select_one('span'):
            # 对于包含span的section，添加换行
            return f'\n{content.strip()}\n'
            
        # 对于其他section（可能是容器），保持原样
        return content

    def convert_html_to_markdown(self, html_content: str) -> str:
        """将HTML内容转换为Markdown格式"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # 获取标题（尝试多个可能的选择器）
            title_element = None
            title_selectors = [
                '#activity-name',  # 新版微信文章标题
                '.rich_media_title',  # 标准选择器
                'h1.rich_media_title',  # 备用选择器1
                'h2.rich_media_title',  # 备用选择器2
                '#js_article_title',  # 备用选择器3
                'meta[property="og:title"]',  # Meta标题
                'title'  # 页面标题
            ]
            
            for selector in title_selectors:
                title_element = soup.select_one(selector)
                if title_element is not None:
                    break
            
            if title_element is None:
                title = "未命名文章"
            else:
                # 根据元素类型获取标题文本
                if title_element.name == 'meta':
                    title = title_element.get('content', '未命名文章')
                else:
                    title = title_element.get_text(strip=True)
                
                # 验证标题文本
                if not title or len(title.strip()) == 0:
                    title = "未命名文章"
            
            # 使用H1格式的标题
            title_content = f"# {title}\n\n"
            
            # 获取正文内容（尝试多个可能的选择器）
            content = None
            content_selectors = [
                '#js_content',  # 新版微信文章内容
                '.rich_media_content',  # 标准选择器
                '.rich_media_wrp'  # 备用选择器
            ]
            
            for selector in content_selectors:
                content = soup.select_one(selector)
                if content is not None:
                    break
            
            if content is None:
                return title_content + "\n\n> 无法获取文章内容，可能原因：\n> 1. 文章已被删除\n> 2. 需要登录访问\n> 3. 链接已过期\n"
            
            markdown_content = self._process_node(content)
            
            # 清理多余的空行，但保留有意义的空行
            markdown_content = re.sub(r'\n{3,}', '\n\n', markdown_content)
            
            return title_content + markdown_content
            
        except Exception as e:
            error_msg = f"转换过程出错：{str(e)}"
            return f"# 转换失败\n\n> {error_msg}\n"

    def _process_node(self, node: Optional[Tag]) -> str:
        """处理单个HTML节点
        
        Args:
            node: BeautifulSoup节点
            
        Returns:
            str: 处理后的Markdown文本
        """
        if not node:
            return ''
            
        # 处理文本节点
        if isinstance(node, str):
            return node.strip()
            
        # 特殊处理section内的span
        if node.name == 'span' and node.parent and node.parent.name == 'section':
            content = ''.join(self._process_node(child) for child in node.children)
            return content.strip()
            
        # 根据规则处理节点
        for rule_name, rule in self.rules.items():
            if node.name in (rule.filter if isinstance(rule.filter, list) else [rule.filter]):
                content = ''.join(self._process_node(child) for child in node.children)
                return rule.replacement(content, node)
        
        # 处理其他节点
        return ''.join(self._process_node(child) for child in node.children)

    def url_to_markdown(self, url: str, output_path: str, filename: str = None) -> Tuple[str, str]:
        """将微信文章URL转换为Markdown文件
        
        Args:
            url: 微信文章URL
            output_path: 输出目录路径
            filename: 可选的文件名，如果不提供则自动生成
            
        Returns:
            Tuple[str, str]: (文件路径, 文章标题)
        """
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            # 添加重试机制
            max_retries = 3
            response = None
            
            for attempt in range(max_retries):
                try:
                    response = requests.get(url, headers=headers, timeout=30)
                    response.raise_for_status()
                    break
                except requests.RequestException as e:
                    if attempt == max_retries - 1:
                        raise Exception(f"获取文章内容失败: {str(e)}")
                    time.sleep(2 ** attempt)  # 指数退避
            
            if not response:
                raise Exception("无法获取文章内容")
            
            response.encoding = 'utf-8'
            
            # 检查响应内容
            if len(response.text) < 100:
                raise Exception("获取到的内容异常，可能需要登录或文章已删除")
            
            # 检查是否包含验证页面的特征
            if "环境异常" in response.text or "完成验证" in response.text:
                raise Exception("遇到环境验证页面，无法直接访问")
            
            # 转换内容
            markdown_content = self.convert_html_to_markdown(response.text)
            
            if not markdown_content:
                raise Exception("转换Markdown内容失败")
            
            # 提取标题
            match = re.search(r'# (.*?)\n', markdown_content)
            article_title = match.group(1) if match else "未命名文章"
            
            # 处理文件名
            if filename:
                # 使用用户提供的文件名
                if not filename.endswith('.md'):
                    filename += '.md'
            else:
                # 自动生成文件名
                safe_title = re.sub(r'[\\/:*?"<>|]', '_', article_title)
                # 如果标题太长，截取前30个字符
                safe_title = safe_title[:30] + ('...' if len(safe_title) > 30 else '')
                filename = f"{safe_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            
            full_path = os.path.join(output_path, filename)
            
            # 确保输出目录存在
            os.makedirs(output_path, exist_ok=True)
            
            # 写入文件
            with open(full_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            
            # 验证文件是否成功写入
            if not os.path.exists(full_path):
                raise Exception("文件写入失败")
            
            return full_path, article_title
            
        except Exception as e:
            error_msg = f"转换失败: {str(e)}"
            
            # 创建错误报告文件（只在错误时输出详细信息）
            print(f"❌ [WECHAT_SERVICE] {error_msg}")
            print(f"🔍 [WECHAT_SERVICE] URL: {url}")
            
            error_filename = f"error_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
            error_path = os.path.join(output_path, error_filename)
            
            os.makedirs(output_path, exist_ok=True)
            with open(error_path, 'w', encoding='utf-8') as f:
                f.write(f"# 转换失败\n\n")
                f.write(f"- URL: {url}\n")
                f.write(f"- 错误信息: {error_msg}\n")
                f.write(f"- 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                
                # 添加响应内容用于调试
                if 'response' in locals() and response and response.text:
                    f.write("\n## 响应内容（前1000字符）：\n```\n")
                    f.write(response.text[:1000])
                    f.write("\n```\n")
            
            print(f"📄 [WECHAT_SERVICE] 错误报告已保存: {error_path}")
            return error_path, "转换失败"

    def url_to_markdown_content(self, url: str) -> Tuple[str, str]:
        """将微信文章URL转换为Markdown内容（不保存文件）
        
        Args:
            url: 微信文章URL
            
        Returns:
            Tuple[str, str]: (Markdown内容, 文章标题)
        """
        print(f"🌐 [WECHAT_SERVICE] 开始URL转Markdown内容转换...")
        print(f"📝 [WECHAT_SERVICE] 参数: url={url}")
        
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
            
            print(f"🔗 [WECHAT_SERVICE] 开始处理URL: {url}")
            
            # 添加重试机制
            max_retries = 3
            response = None
            
            for attempt in range(max_retries):
                try:
                    print(f"📡 [WECHAT_SERVICE] 尝试获取文章内容... (第{attempt + 1}次)")
                    response = requests.get(url, headers=headers, timeout=30)
                    print(f"📊 [WECHAT_SERVICE] 响应状态码: {response.status_code}")
                    response.raise_for_status()
                    print(f"✅ [WECHAT_SERVICE] 请求成功")
                    break
                except requests.RequestException as e:
                    print(f"❌ [WECHAT_SERVICE] 请求失败 (第{attempt + 1}次): {str(e)}")
                    if attempt == max_retries - 1:
                        print(f"💥 [WECHAT_SERVICE] 所有重试都失败了")
                        raise Exception(f"获取文章内容失败: {str(e)}")
                    print(f"⏳ [WECHAT_SERVICE] {2 ** attempt}秒后重试...")
                    time.sleep(2 ** attempt)  # 指数退避
            
            if not response:
                print(f"❌ [WECHAT_SERVICE] 响应对象为空")
                raise Exception("无法获取文章内容")
            
            response.encoding = 'utf-8'
            print(f"📊 [WECHAT_SERVICE] 响应内容长度: {len(response.text)}")
            
            # 检查响应内容
            if len(response.text) < 100:
                print(f"⚠️ [WECHAT_SERVICE] 响应内容过短，可能是错误页面")
                raise Exception("获取到的内容异常，可能需要登录或文章已删除")
            
            # 检查是否包含验证页面的特征
            if "环境异常" in response.text or "完成验证" in response.text:
                print(f"🚫 [WECHAT_SERVICE] 检测到验证页面")
                raise Exception("遇到环境验证页面，无法直接访问")
            
            print("✅ [WECHAT_SERVICE] 成功获取文章内容，开始转换...")
            
            # 转换内容
            print("🔄 [WECHAT_SERVICE] 开始HTML到Markdown转换...")
            markdown_content = self.convert_html_to_markdown(response.text)
            
            if not markdown_content:
                print("❌ [WECHAT_SERVICE] Markdown转换结果为空")
                raise Exception("转换Markdown内容失败")
            
            print(f"📊 [WECHAT_SERVICE] Markdown内容长度: {len(markdown_content)}")
            
            # 提取标题
            print("📝 [WECHAT_SERVICE] 提取文章标题...")
            match = re.search(r'# (.*?)\n', markdown_content)
            article_title = match.group(1) if match else "未命名文章"
            print(f"📰 [WECHAT_SERVICE] 提取到的标题: {article_title}")
            
            print("🎉 [WECHAT_SERVICE] 内容转换完成")
            return markdown_content, article_title
            
        except Exception as e:
            error_msg = f"转换失败: {str(e)}"
            print(f"❌ [WECHAT_SERVICE] {error_msg}")
            raise Exception(error_msg)

    def batch_urls_to_markdown(self, urls: List[str], output_path: str) -> List[Tuple[str, str, str]]:
        """批量将微信文章URL转换为Markdown文件
        
        Args:
            urls: 微信文章URL列表
            output_path: 输出目录路径
            
        Returns:
            List[Tuple[str, str, str]]: [(文件路径, 文章标题, 状态)] 列表
        """
        print(f"🌐 [WECHAT_SERVICE] 开始批量URL转Markdown转换...")
        print(f"📝 [WECHAT_SERVICE] 参数: urls数量={len(urls)}, output_path={output_path}")
        
        results = []
        
        for i, url in enumerate(urls, 1):
            print(f"\n🔄 [WECHAT_SERVICE] 处理第 {i}/{len(urls)} 个URL: {url}")
            
            try:
                file_path, title = self.url_to_markdown(url, output_path)
                results.append((file_path, title, "成功"))
                print(f"✅ [WECHAT_SERVICE] 第 {i} 个URL处理成功")
            except Exception as e:
                error_msg = f"处理失败: {str(e)}"
                print(f"❌ [WECHAT_SERVICE] 第 {i} 个URL处理失败: {error_msg}")
                results.append(("", "", error_msg))
        
        print(f"\n🎉 [WECHAT_SERVICE] 批量转换完成，成功: {len([r for r in results if r[2] == '成功'])}/{len(urls)}")
        return results

    def _process_list(self, content: str, node: Tag) -> str:
        """处理列表
        
        Args:
            content: 列表内容
            node: BeautifulSoup节点
            
        Returns:
            str: 处理后的Markdown文本
        """
        # 添加列表深度处理
        depth = len(list(node.parents))
        indent = '  ' * (depth - 1)
        
        if node.name == 'ol':
            # 处理有序列表
            items = node.find_all('li', recursive=False)
            start = node.get('start', 1)
            result = []
            for i, item in enumerate(items):
                num = int(start) + i
                item_content = self._process_node(item)
                result.append(f'{indent}{num}. {item_content.strip()}')
            return '\n'.join(result)
        else:
            # 处理无序列表
            items = node.find_all('li', recursive=False)
            return '\n'.join(f'{indent}- {self._process_node(item).strip()}' for item in items)

    def _process_link(self, content: str, node: Tag) -> str:
        """处理链接
        
        Args:
            content: 链接内容
            node: BeautifulSoup节点
            
        Returns:
            str: 处理后的Markdown文本
        """
        # 如果链接不包含qpic.cn,则整行去除,返回空字符串
        href = node.get('href', '')
        if 'qpic.cn' not in href:
            return ''
        
        # 否则返回链接文本
        return content

    def _process_image(self, content: str, node: Tag) -> str:
        """处理图片
        
        Args:
            content: 图片内容
            node: BeautifulSoup节点
            
        Returns:
            str: 处理后的Markdown文本
        """
        # 获取图片的alt文本,如果没有就使用"图片"作为默认文本
        alt = node.get('alt', '图片').replace('[', '\\[').replace(']', '\\]') 
        if not alt.strip():
            alt = '图片'
            
        # 获取图片链接,优先使用data-src
        src = node.get('data-src') or node.get('src', '')
        
        if not src:
            return ''
            
        # 只保留包含qpic.cn的图片链接
        if 'qpic.cn' not in src:
            return ''
            
        title = node.get('title', '')
        
        # 处理title属性
        title_part = f' "{title}"' if title else ''
        return f'\n\n![{alt}]({src}{title_part})\n\n'

# 用于测试的入口点
if __name__ == "__main__":
    service = WechatToMarkdownService()
    url = input("请输入微信文章URL：").strip()
    output_dir = "wechat_articles"
    
    try:
        file_path, title = service.url_to_markdown(url, output_dir)
        print(f"转换成功！")
        print(f"文章标题: {title}")
        print(f"保存路径: {file_path}")
    except Exception as e:
        print(f"转换失败: {e}") 