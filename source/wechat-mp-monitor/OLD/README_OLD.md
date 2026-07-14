# OLD 文件夹说明

## 移动时间
2026-04-03

## 移动原因
这些文件与 `article_workflow.py` 和 `main.py` 的运行无关，已移动到此文件夹进行归档。

## 移动的文件清单

### 根目录 Python 文件
- `excell_to_md.py` - Excel转Markdown工具
- `img.py` - 图片处理工具
- `openaiapi.py` - OpenAI API工具
- `openaiapi.json` - OpenAI配置文件
- `png2md.py` - PNG转Markdown工具
- `test_ocr_config.py` - OCR配置测试
- `wxcookie.py` - 微信Cookie工具

### 文档文件
- `README.md` - 项目说明文档
- `OCR模型配置说明.md` - OCR配置说明
- `流程图对比.txt` - 流程图对比文档
- `SomeURL2MD_README.md` - SomeURL2MD模块说明

### 输出目录（程序会自动创建）
- `output/` - 主输出目录（包含CSV和MD报告）
- `output_md/` - Markdown输出目录
- `rejected_articles.csv` - 拒绝的文章记录

### 数据和配置
- `data/` - 数据目录
- `wxcookie.yaml` - 微信Cookie配置
- `SomeURL2MD_requirements.txt` - SomeURL2MD依赖配置

### SomeURL2MD 目录文件
- `baidu_table_ocr.py` - 百度表格OCR
- `example_usage.py` - 使用示例
- `excell_to_md.py` - Excel转Markdown
- `image_service.py` - 图片服务
- `md_service.py` - Markdown服务
- `openai_concurrent_service.py` - OpenAI并发服务
- `qr_scanner_service.py` - 二维码扫描服务
- `qwen_ocr_plus.py` - 通义OCR增强版
- `test_baidu_ocr_integration.py` - 百度OCR集成测试
- `wechat_to_markdown.py` - 微信转Markdown
- `wechat_uploader.py` - 微信上传工具

### 其他文件/目录
- `D1热门产品_终极PK/` - 数据目录
- `D1热门产品_终极PK.7z` - 压缩包
- `docker-compose.yml` - Docker配置
- `scripts/` - 脚本目录

## 保留的核心文件（项目根目录）

### 主程序文件
- `article_workflow.py` - 文章处理工作流主程序
- `main.py` - WeRSS客户端主程序

### 依赖文件
- `werss_client.py` - WeRSS API客户端（两个主程序都依赖）
- `prompts_config.py` - OCR提示词配置
- `config.py` - 项目配置文件
- `requirements.txt` - Python依赖包配置

### SomeURL2MD 核心模块
- `SomeURL2MD/__init__.py` - Python包初始化
- `SomeURL2MD/ai_classifier.py` - AI文章分类器
- `SomeURL2MD/SomeURL2MD.py` - URL转Markdown核心模块
- `SomeURL2MD/url_service.py` - URL服务

## 测试说明
在确认 `article_workflow.py` 和 `main.py` 都能正常运行后，OLD 文件夹中的内容可以安全删除。

## 恢复方法
如果需要恢复某个文件，直接从 OLD 文件夹移回项目根目录即可：
```bash
mv OLD/文件名 ./
```
