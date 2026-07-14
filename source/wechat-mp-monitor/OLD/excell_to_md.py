"""
Excel转Markdown转换模块
通用 Excel → Markdown 文本转换（供历史脚本或工具使用）
"""

import re
try:
    from markitdown import MarkItDown
    MARKITDOWN_AVAILABLE = True
except ImportError:
    MARKITDOWN_AVAILABLE = False
    print("⚠️ MarkItDown未安装，将使用简化版Excel转换")

def excel_to_markdown_string(excel_file: str) -> str:
    """将Excel转换为Markdown字符串
    
    Args:
        excel_file: Excel文件路径
        
    Returns:
        str: 转换后的Markdown字符串
    """
    try:
        if MARKITDOWN_AVAILABLE:
            # 使用MarkItDown进行转换
            md = MarkItDown()
            result = md.convert(excel_file)
            content = result.text_content
            
            # 清理内容
            content = content.replace('NaN', '')  # 删除NaN
            content = content.replace('\\n', '<br>')  # 替换换行符
            content = re.sub(r'Unnamed: \d+', '', content)  # 替换Unnamed文本
            content = content.replace('## Sheet1', '## 图表')  # 替换Sheet1为图表
            
            return content
        else:
            # 简化版实现：使用pandas读取Excel并转换为Markdown
            try:
                import pandas as pd
                
                # 读取Excel文件
                df = pd.read_excel(excel_file)
                
                # 清理数据
                df = df.fillna('')  # 填充NaN值
                
                # 转换为Markdown表格
                markdown_table = df.to_markdown(index=False)
                
                return f"## 图表\n\n{markdown_table}"
                
            except ImportError:
                return "## 转换失败\n\n无法导入pandas库，请安装pandas: pip install pandas"
            except Exception as e:
                return f"## 转换失败\n\n读取Excel文件时出错: {str(e)}"
                
    except Exception as e:
        return f"## 转换失败\n\n转换过程中出错: {str(e)}"

def convert_excel_to_md(excel_file: str, output_file: str) -> bool:
    """将Excel文件转换为Markdown文件
    
    Args:
        excel_file: 输入Excel文件路径
        output_file: 输出Markdown文件路径
        
    Returns:
        bool: 转换是否成功
    """
    try:
        # 获取Markdown内容
        markdown_content = excel_to_markdown_string(excel_file)
        
        # 检查是否转换失败
        if markdown_content.startswith("## 转换失败"):
            print(f"❌ Excel转换失败: {markdown_content}")
            return False
        
        # 写入文件
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(markdown_content)
        
        print(f"✅ Excel转换成功: {excel_file} -> {output_file}")
        return True
        
    except Exception as e:
        print(f"❌ Excel转换失败: {str(e)}")
        return False

if __name__ == "__main__":
    # 测试代码
    print("Excel转Markdown转换模块测试")
    print(f"MarkItDown可用: {MARKITDOWN_AVAILABLE}")
    
    try:
        import pandas as pd
        print("✅ Pandas可用")
    except ImportError:
        print("❌ Pandas不可用") 