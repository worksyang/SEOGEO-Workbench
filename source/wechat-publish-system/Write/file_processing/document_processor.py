"""
# 🗂️文档批量管理器
1️⃣ 文件扫描  
   └─ 📂 分类收集文件夹下所有Markdown及关联txt  
2️⃣ 标题提取  
   └─ 🏷️ 获取每个Markdown的首行H1标题  
3️⃣ 调度与索引  
   ├─ 🔁 支持顺序或随机选择文档  
   └─ 📊 输出当前索引与完整文档列表  
"""
import os
import random
import re
from typing import List, Dict, Any, Set

class DocumentProcessor:
    def __init__(self, base_folder: str):
        """
        初始化文档处理器
        
        Args:
            base_folder: 基础文件夹路径
        """
        self.base_folder = base_folder
        self.md_files_with_txt = []  # 有对应txt文件的md文件列表
        self.md_files_without_txt = []  # 没有对应txt文件的md文件列表
        self.all_available_files = []  # 所有可用的文件（合并后的列表）
        self.current_md_index = 0  # 当前索引（针对打乱后的顺序）
        self._shuffled_order = []  # 打乱后的索引顺序（按轮次重置）
        self._scan_md_files()
        # 初始化首轮随机顺序
        self._reset_round_shuffle()

    def _reset_round_shuffle(self) -> None:
        """
        重置一轮的随机顺序（200个一轮，完整覆盖后再随机下一轮）
        """
        if not self.all_available_files:
            self._shuffled_order = []
            self.current_md_index = 0
            return
        self._shuffled_order = list(range(len(self.all_available_files)))
        random.shuffle(self._shuffled_order)
        self.current_md_index = 0
        
    def _extract_h1_from_md_content(self, md_path: str) -> str:
        """
        从MD文件内容中提取H1标题
        
        Args:
            md_path: MD文件路径
            
        Returns:
            str: 提取的H1标题，如果没有找到返回空字符串
        """
        try:
            with open(md_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 查找第一个H1标题
            h1_pattern = r'^# (.+?)$'
            h1_match = re.search(h1_pattern, content, re.MULTILINE)
            
            if h1_match:
                h1_title = h1_match.group(1).strip()
                # 清理标题中的特殊字符和多余空格
                h1_title = re.sub(r'\s+', ' ', h1_title)  # 多个空格替换为单个空格
                return h1_title
            
            return ""
            
        except Exception as e:
            print(f"⚠️ 读取MD文件失败 {md_path}: {e}")
            return ""
    
    def _extract_title_from_filename(self, filename: str) -> str:
        """
        从文件名中提取干净的标题
        
        处理格式如: 4家银行实测 _ 影响保费融资回报的关键因素！（含压力测试）_20250906_210542_20250906210541_有态度的精蒜湿.md
        提取出: 4家银行实测 _ 影响保费融资回报的关键因素！（含压力测试）
        
        Args:
            filename: 文件名（包含扩展名）
            
        Returns:
            str: 提取的标题
        """
        # 去除扩展名
        name_without_ext = os.path.splitext(filename)[0]
        
        # 使用正则表达式匹配并移除时间戳及其后的所有内容
        # 匹配第一个时间戳模式，并移除它及其后面的所有内容
        patterns = [
            r'_\d{8}_\d{6}.*$',        # _20250906_210542及其后的所有内容
            r'_\d{8}_\d+.*$',          # _20250906_210542及其后的所有内容  
            r'_\d{14}.*$',             # _20250906210542及其后的所有内容
            r'_\d{8}.*$',              # _20250906及其后的所有内容
            r'_\d+.*$',                # _任意数字及其后的所有内容
        ]
        
        clean_title = name_without_ext
        for pattern in patterns:
            clean_title = re.sub(pattern, '', clean_title)
            if clean_title != name_without_ext:  # 如果匹配到了，就停止
                break
        
        # 清理标题中的特殊字符
        clean_title = clean_title.replace('_', ' ')  # 下划线替换为空格
        clean_title = re.sub(r'\s+', ' ', clean_title)  # 多个空格替换为单个空格
        clean_title = clean_title.strip()
        
        # 如果清理后为空，使用原始文件名（不含扩展名）
        if not clean_title:
            clean_title = name_without_ext
            
        return clean_title
        
    def _get_smart_title(self, md_path: str, filename: str) -> str:
        """
        智能获取标题：优先从MD内容提取H1，失败则使用文件名
        
        Args:
            md_path: MD文件路径
            filename: 文件名
            
        Returns:
            str: 提取的标题
        """
        # 优先尝试从MD文件内容中提取H1标题
        h1_title = self._extract_h1_from_md_content(md_path)
        
        if h1_title:
            print(f"📝 从内容提取H1标题: {h1_title}")
            return h1_title
        else:
            # 如果没有H1标题，使用清理后的文件名
            filename_title = self._extract_title_from_filename(filename)
            print(f"📝 从文件名提取标题: {filename_title}")
            return filename_title

    def _scan_md_files(self) -> None:
        """
        递归扫描所有md文件，检查是否有对应的txt文件
        """
        try:
            for root, _, files in os.walk(self.base_folder):
                md_files = [f for f in files if f.endswith('.md')]
                txt_files = [f for f in files if f.endswith('.txt')]
                txt_basenames = [os.path.splitext(f)[0] for f in txt_files]
                
                for md_file in md_files:
                    md_basename = os.path.splitext(md_file)[0]
                    md_path = os.path.join(root, md_file)
                    
                    if md_basename in txt_basenames:
                        # 找到对应的txt文件
                        txt_path = os.path.join(root, f"{md_basename}.txt")
                        file_info = {
                            'folder': root,
                            'md_file': md_file,
                            'md_path': md_path,
                            'txt_path': txt_path,
                            'has_txt': True
                        }
                        self.md_files_with_txt.append(file_info)
                        self.all_available_files.append(file_info)
                    else:
                        # 没有找到对应的txt文件，使用智能标题提取
                        smart_title = self._get_smart_title(md_path, md_file)
                        file_info = {
                            'folder': root,
                            'md_file': md_file,
                            'md_path': md_path,
                            'smart_title': smart_title,
                            'has_txt': False
                        }
                        self.md_files_without_txt.append(file_info)
                        self.all_available_files.append(file_info)
                        
            print(f"扫描完成:")
            print(f"  - 有对应txt文件的md文件: {len(self.md_files_with_txt)} 个")
            print(f"  - 没有txt文件的md文件: {len(self.md_files_without_txt)} 个")
            print(f"  - 总可用文件: {len(self.all_available_files)} 个")
            
            if self.md_files_without_txt:
                print(f"✅ 智能标题提取: {len(self.md_files_without_txt)} 个文件将优先使用H1标题，回退到文件名标题")
                
        except Exception as e:
            print(f"扫描文件时出错: {e}")

    def select_content(self, max_count: int) -> List[Dict[str, Any]]:
        """
        选择指定数量的内容进行处理
        
        Args:
            max_count: 最大选择数量
            
        Returns:
            List[Dict[str, Any]]: 选中的内容列表
        """
        if not self.all_available_files:
            return []
            
        try:
            selected_contents = []
            
            for _ in range(max_count):
                # 如果一轮刚好走完，重新打乱开始新的一轮
                if not self._shuffled_order or self.current_md_index >= len(self._shuffled_order):
                    self._reset_round_shuffle()

                # 获取当前文件信息（按打乱后的顺序）
                file_idx = self._shuffled_order[self.current_md_index]
                file_info = self.all_available_files[file_idx]
                
                # 更新索引（到达末尾后，下一次循环会触发新一轮打乱）
                self.current_md_index += 1
                
                try:
                    if file_info['has_txt']:
                        # 有txt文件，从中随机选择标题
                        with open(file_info['txt_path'], 'r', encoding='utf-8') as f:
                            titles = [line.strip() for line in f if line.strip()]
                        
                        if not titles:
                            print(f"警告: {file_info['txt_path']} 中没有有效的标题，使用智能标题提取")
                            title = self._get_smart_title(file_info['md_path'], file_info['md_file'])
                        else:
                            title = random.choice(titles)
                            
                        print(f"📝 选择文件: {file_info['md_file']} (使用txt标题: {title})")
                    else:
                        # 没有txt文件，使用智能提取的标题
                        title = file_info['smart_title']
                        print(f"📝 选择文件: {file_info['md_file']} (使用智能标题: {title})")
                    
                    selected_contents.append({
                        'folder': file_info['folder'],
                        'md_file': file_info['md_file'],
                        'title': title,
                        'md_path': file_info['md_path']
                    })
                        
                except Exception as e:
                    print(f"处理文件 {file_info['md_path']} 时出错: {e}")
                    continue
                    
            return selected_contents
            
        except Exception as e:
            print(f"选择内容时出错: {e}")
            return []

    def get_missing_txt_files(self) -> List[Dict[str, str]]:
        """
        获取没有对应txt文件的md文件列表
        
        Returns:
            List[Dict[str, str]]: 缺失txt文件的信息列表
        """
        return [
            {
                'md_path': info['md_path'],
                'md_file': info['md_file'],
                'folder': info['folder']
            }
            for info in self.md_files_without_txt
        ]
        
    def read_content(self, file_path: str) -> str:
        """
        读取文件内容
        
        Args:
            file_path: 文件路径
            
        Returns:
            str: 文件内容
        """
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            print(f"读取文件失败 [{file_path}]: {e}")
            return ""
            
    def read_prompt_template(self, prompt_path: str) -> str:
        """
        读取提示词模板
        
        Args:
            prompt_path: 提示词模板路径
            
        Returns:
            str: 提示词模板内容
        """
        try:
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read()
        except Exception as e:
            print(f"读取提示词模板失败: {e}")
            return "" 