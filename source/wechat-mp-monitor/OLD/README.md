# 微信公众号监控系统 - 智能文章处理工作流

## 项目简介

这是一个完整的微信公众号监控和文章处理系统，通过WeRSS API自动收集公众号文章，使用AI智能分类，并生成高质量的Markdown文档。系统采用多阶段工作流设计，确保高效、准确的文章处理。

## 🎯 核心工作流 (Workflow)

### 第一阶段：数据收集与更新
```
WeRSS API → 公众号列表 → 批量更新 → 文章数据获取
```

**关键组件：**
- `main.py` - 主程序入口，支持两种模式：
  - **更新模式**：调用WeRSS API更新所有公众号文章
  - **只读模式**：直接使用现有数据生成报告
- `werss_client.py` - WeRSS API客户端，处理认证和数据获取

**输出文件：**
- 每日Markdown报告 (`20250720.md`)
- 总文章CSV数据库 (`articles.csv`)
- 各公众号独立CSV文件 (`公众号名称.csv`)

### 第二阶段：AI智能分类与筛选
```
文章标题 → AI分类器 → 搜索流/推荐流/钩子文章/不选 → 筛选结果
```

**关键组件：**
- `article_workflow.py` - 核心工作流引擎
- `SomeURL2MD/ai_classifier.py` - AI分类器
- `config.py` - 分类规则和AI配置

**分类标准：**
- ✅ **搜索流**：产品对比、决策指南、权威建立型内容
- ⭐ **推荐流**：时效性新闻、热点事件
- ➿ **钩子文章**：数字诱惑但未明确产品的文章
- 🙅‍ **不选**：政策风险、无关内容
- ⛔ **看文章再确定**：需要进一步评估的内容

### 第三阶段：高级OCR处理
```
文章URL → 图片提取 → 两阶段OCR → 高质量Markdown
```

**关键组件：**
- `SomeURL2MD/SomeURL2MD.py` - URL转Markdown主引擎
- `png2md.py` - 图片转Markdown工具
- `SomeURL2MD/image_service.py` - 图片处理服务

**OCR流程：**
1. **AI并行初审**：使用AI快速识别简单图片
2. **GPU串行精加工**：复杂表格使用本地GPU OCR
3. **智能融合**：结合两种结果生成最优Markdown

### 第四阶段：文件组织与管理
```
分类结果 → 按类型/日期组织 → 重命名 → 最终归档
```

**文件结构：**
```
output_md/
├── 搜索流/
│   ├── 20250720/
│   │   ├── 文章标题_20250720_143022_公众号名称.md
│   │   └── ...
│   └── ...
└── 推荐流/
    ├── 20250720/
    │   └── ...
    └── ...
```

## 🚀 快速开始

### 1. 环境准备

```bash
# 安装依赖
pip install -r requirements.txt

# 安装SomeURL2MD子模块依赖
cd SomeURL2MD
pip install -r requirements.txt
cd ..
```

### 2. 配置系统

编辑 `main.py` 中的WeRSS配置：
```python
BASE_URL = "http://your-werss-server.com"  # WeRSS服务器地址
USERNAME = "your_username"                  # 用户名
PASSWORD = "your_password"                  # 密码
```

Web 版统一在 `server/config.py` 中设置默认分类模型，或在网页设置页修改；不要在旧版 `config.py` 中再维护 AI 模型配置。

### 3. 运行工作流

#### 模式1：完整更新流程
```bash
python main.py
# 选择模式1：更新所有账号并生成报告
```

#### 模式2：AI分类工作流
```bash
python article_workflow.py
```

#### 模式3：图片OCR处理
```bash
python png2md.py
```

## 📊 工作流详解

### 数据收集工作流

```python
# 1. 登录WeRSS系统
client = WeRSSClient(base_url, username, password)

# 2. 获取所有公众号
mps = client.get_all_mps()

# 3. 批量更新文章（可选）
for mp in mps:
    client.update_mp_articles(mp['id'])
    time.sleep(10)  # 等待爬虫完成

# 4. 获取文章数据
articles = client.get_mp_articles(mp['id'])
```

### AI分类工作流

```python
# 1. 获取最近文章
recent_articles = get_recent_articles(client, days=15)

# 2. 过滤已处理文章
filtered_articles = filter_existing_articles(articles, existing_titles, rejected_urls)

# 3. AI批量分类
classifier = AIClassifier()
classified_results = classifier.classify_titles(article_titles)

# 4. 匹配和筛选
accepted_articles, rejected_articles = match_and_filter_articles(
    filtered_articles, classified_results
)
```

### OCR处理工作流

```python
# 1. URL转Markdown
result = await SomeURL2MD.convert_urls_to_markdown(
    urls=article_urls,
    output_dir=output_folder,
    create_timestamp_dir=False
)

# 2. 两阶段OCR处理
# 阶段1：AI并行初审
simple_results, complex_tasks = await concurrent_ocr.run_ai_parallel_phase(
    image_tasks, custom_prompt=SIMPLE_OCR_PROMPT
)

# 阶段2：GPU串行精加工
refined_results = concurrent_ocr.run_gpu_serial_phase(complex_tasks)

# 阶段3：结果融合
all_results = simple_results.copy()
all_results.update(refined_results)
```

## 📁 输出文件说明

### 数据文件
- `output/articles.csv` - 总文章数据库
- `output/公众号名称.csv` - 各公众号独立数据
- `output/YYYYMMDD.md` - 每日文章报告

### 分类文件
- `output_md/搜索流/YYYYMMDD/` - 搜索流文章
- `output_md/推荐流/YYYYMMDD/` - 推荐流文章
- `rejected_articles.csv` - 被拒绝的文章记录

### 临时文件
- `temp_ocr_images/` - OCR处理临时图片
- `temp_ocr_output/` - OCR输出临时文件

## 🔧 配置参数

### 工作流参数 (`article_workflow.py`)
```python
OUTPUT_DIR = "output_md"           # 输出目录
DAYS_TO_FETCH = 15                # 获取天数
SIMILARITY_THRESHOLD = 95         # 相似度阈值
AI_BATCH_SIZE = 20               # AI批处理大小
```

### OCR参数 (`SomeURL2MD/SomeURL2MD.py`)
```python
max_workers = 30                 # 并发线程数
max_retries = 3                  # 重试次数
```

### 分类规则 (`config.py`)
- 政策风险词汇列表
- 安全关键词列表
- 分类判断标准

## 🎯 核心特性

### 智能增量处理
- 自动检测已处理文章，避免重复处理
- 模糊匹配算法，识别相似标题
- 拒绝记录管理，避免重复拒绝

### 高效并发处理
- 多线程图片下载和验证
- 并发AI OCR处理
- 智能任务分配和负载均衡

### 高质量输出
- 两阶段OCR确保复杂表格识别
- 智能图片过滤（尺寸、二维码检测）
- 结构化文件组织

### 完善的错误处理
- 网络异常重试机制
- Token失败自动切换
- 详细的日志记录

## 📈 性能优化

### 内存优化
- 流式处理大文件
- 及时清理临时文件
- 分批处理避免内存溢出

### 网络优化
- 连接池复用
- 请求超时控制
- 失败重试机制

### 并发优化
- 动态调整并发数
- 任务队列管理
- 资源竞争控制

## 🔍 故障排除

### 常见问题

**Q: WeRSS登录失败**
A: 检查服务器地址、用户名密码，确认网络连接

**Q: AI分类失败**
A: 检查API密钥配置，确认服务商状态

**Q: OCR处理缓慢**
A: 调整并发数设置，检查GPU资源

**Q: 文件组织混乱**
A: 检查文件权限，确认输出目录结构

### 日志分析
- 控制台实时日志输出
- 详细的错误信息
- 处理统计和性能指标

## 🤝 贡献指南

1. Fork项目
2. 创建功能分支
3. 提交更改
4. 发起Pull Request

## 📄 许可证

MIT License

## 🔗 相关链接

- [WeRSS项目](https://github.com/your-werss-repo)
- [SomeURL2MD文档](SomeURL2MD/README.md)
- [API文档](docs/api.md)

---

**注意**：本项目专注于微信公众号文章的智能处理和归档，通过多阶段工作流确保高效、准确的内容管理。

## 系统修复说明 (2024-06-27)

### 修复的问题

1. **缓存路径问题**
   - 修复了硬编码的缓存路径导致的权限问题
   - 实现了多级缓存路径尝试机制，自动选择可用路径
   - 最后回退到系统临时目录，确保总能找到可写入的位置

2. **临时文件管理**
   - 实现了统一的临时文件跟踪和清理机制
   - 使用UUID生成唯一文件名，避免文件冲突
   - 增强了异常情况下的临时文件清理

3. **超时控制**
   - 为AI调用和OCR处理添加了超时控制
   - 实现了跨平台的超时处理（Windows和Linux兼容）
   - 添加了流式响应的分段超时检测

4. **错误处理和重试**
   - 为所有关键操作添加了重试机制
   - 改进了错误日志记录，包括详细的堆栈跟踪
   - 实现了优雅的降级处理，确保即使部分功能失败也能返回结果

5. **资源管理**
   - 确保及时释放文件句柄和网络连接
   - 实现了分批处理机制，避免同时处理过多图片
   - 添加了更详细的进度和状态报告

### 使用建议

1. 对于大量图片处理，建议分批进行，每批不超过10张
2. 如果遇到GPU内存不足，可以考虑修改代码将OCR设备改为CPU模式
3. 定期清理缓存目录，避免占用过多磁盘空间
