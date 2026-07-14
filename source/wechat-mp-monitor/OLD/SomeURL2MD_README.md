# SomeURL2MD - 全并行架构版

微信文章URL转Markdown工具的独立命令行版本，采用革命性全并行架构，每张图片独立并行处理完整流水线，极大提升处理效率。

## 🚀 功能特点

- **🚀 全并行架构**：每张图片独立并行处理完整流水线，无需等待
- **⚡ 极速处理**：AI初审、百度OCR、Gemini Pro融合全部并行执行
- **🎯 高精度识别**：百度官方OCR + AI双重校验，识别准确率更高
- **🧠 统一稳定模型**：使用经过验证的稳定主模型处理所有任务
- **🔍 智能复杂度检测**：自动识别表格复杂程度，按需启用增强处理
- **🌐 多URL支持**：支持单个或多个URL批量处理
- **🛡️ 错误容错**：多重备选方案确保稳定性
- **🔄 完美兼容**：旧代码无需修改，自动使用新架构
- **📝 标准化输出**：HTML注释格式OCR结果

## 🚀 全并行处理流程

### 🔥 革命性架构：单图片并行流水线

每张图片独立并行执行完整处理流程，无需等待其他图片：

**1️⃣ AI初审（并行）**
- 使用 **Gemini 2.5 Flash Lite** 模型
- 识别图片内容并判断复杂度
- 简单图片直接返回结果

**2️⃣ 复杂图片处理（并行，如需）**
- 并行调用**百度官方OCR API**
- 高精度表格识别
- 支持图片缓存和去重机制

**3️⃣ 智能融合（并行，如需）**
- 使用 **稳定主模型** 进行智能融合
- 结合AI和百度OCR的优势
- 智能纠错和结构优化
- 输出最终高质量结果

### ⚡ 性能优势

- **真正并行**：所有图片同时处理，无串行等待
- **资源最大化**：充分利用CPU、内存和网络资源
- **时间大幅缩短**：处理时间接近最慢单张图片的时间
- **完美扩展**：支持数百张图片同时处理
- **稳定可靠**：统一使用验证过的稳定主模型

## 📋 系统要求

- Python 3.7+
- 稳定的网络连接
- OpenAI API访问权限
- 百度OCR API密钥（已内置默认密钥）

## 🔧 安装依赖

```bash
pip install -r requirements.txt
```

## 🚀 使用方法

```bash
python SomeURL2MD.py
```

按提示操作：
1. 输入微信文章URL（支持多行，一行一个）
2. 选择输出目录
3. 等待处理完成

## 📁 输出格式

生成的Markdown文件将包含：
- 原始文章内容
- 图片后的OCR识别注释（HTML注释格式）
- 自动过滤的无效图片已被删除

## ⚙️ 配置说明

默认配置：
- 并发线程数：30个
- 重试次数：3次
- 识别模式：简单模式（200字描述）

可在代码中修改相关参数。

## ⚙️ 模型配置

- **架构类型**：全并行处理（每张图片独立并行）
- **主AI模型**：`gemini-2.5-flash-lite`（用于初审和融合）
- **API基础URL**：`http://doubao.zwchat.cn/v1`
- **最大并发数**：30个线程
- **复杂表格阈值**：>8列或列数不一致
- **稳定性优化**：统一使用主模型确保稳定性

## 📝 注意事项

1. 确保OpenAI API配置正确且有足够额度
2. 网络连接稳定，用于下载图片和调用API
3. 输出目录有足够空间存储结果文件
4. 只支持微信公众号文章URL
5. 复杂表格处理时间较长，请耐心等待
6. 百度OCR使用官方API，识别准确率更高

## 🔗 相关文件

- `SomeURL2MD.py` - 主程序文件
- `openai_concurrent_service.py` - 百度OCR集成并发服务（核心）
- `baidu_table_ocr.py` - 百度OCR官方API模块
- `excell_to_md.py` - Excel转Markdown转换模块
- `prompts_config.py` - 提示词配置（包含融合提示词）
- `url_service.py` - URL转换服务
- `image_service.py` - 图片处理服务
- `md_service.py` - Markdown文件处理服务
- `wechat_to_markdown.py` - 微信文章转换服务
- `qr_scanner_service.py` - 二维码检测服务
- `qwen_ocr_plus.py` - AI图片分析服务
- `test_baidu_ocr_integration.py` - 百度OCR集成测试脚本
- `example_usage.py` - 使用示例和说明

所有依赖文件都在同一目录下，无需复杂的文件夹结构。

## 🛠️ 微信转Markdown服务接口

`wechat_to_markdown.py` 提供了多种灵活的接口：

### 1. 单个URL转文件
```python
from wechat_to_markdown import WechatToMarkdownService

service = WechatToMarkdownService()
file_path, title = service.url_to_markdown(url, output_path, filename=None)
```

### 2. 单个URL转内容（不保存文件）
```python
content, title = service.url_to_markdown_content(url)
```

### 3. 批量URL处理
```python
urls = ["url1", "url2", "url3"]
results = service.batch_urls_to_markdown(urls, output_path)
# 返回: [(文件路径, 标题, 状态), ...]
```

### 4. HTML转Markdown
```python
markdown_content = service.convert_html_to_markdown(html_content)
```

### 5. 图片处理特点
- **保留原始链接**：`_process_image()` 方法保留原始图片链接，不进行OCR转换
- **只处理微信图片**：只保留包含 `qpic.cn` 的图片链接
- **支持多种属性**：处理 `data-src`、`src`、`alt`、`title` 等属性 