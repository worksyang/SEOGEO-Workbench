---
name: markdown-image-vision
description: 对 Markdown 文件中的图片进行 OCR 识别或视觉描述，或对单张图片进行描述。使用场景包括：普通 Markdown 图片、带链接的图片、HTML img 标签、本地图片或远程图片 URL。
metadata:
  short-description: Markdown 图片 OCR 识别与标注
---

# Markdown Image Vision

## 功能说明

本 Skill 用于：
- 对 Markdown 文件中的所有图片批量进行 OCR 或视觉描述。
- 对单张本地或远程图片进行描述或 OCR。
- 将微信公众号文章 URL 转换为 Markdown，并可继续给文章内所有图片添加 OCR 注释。
- 支持普通图片、链接包裹图片、HTML img 标签等多种图片格式。

## 文件说明

- `scripts/markdown_image_vision.py`：主程序，同时支持命令行调用和作为模块导入。
- `scripts/wechat_to_markdown.py`：微信公众号文章 URL 转 Markdown 转换器。
- `.env`：本地 API 和代理配置。
- `.env.example`：配置模板，复制为 `.env` 后填写密钥。

## 快速开始

安装依赖：

```bash
pip install openai requests
pip install beautifulsoup4
```

对 Markdown 文件中的图片进行批量识别（默认原地修改）：

```bash
python3 markdown-image-vision/scripts/markdown_image_vision.py annotate article.md
```

识别结果写入新文件（不修改原文件）：

```bash
python3 markdown-image-vision/scripts/markdown_image_vision.py annotate article.md --output article.ocr.md
```

预览修改效果（不写文件）：

```bash
python3 markdown-image-vision/scripts/markdown_image_vision.py annotate article.md --dry-run
```

并行识别 Markdown 内图片：

```bash
python3 markdown-image-vision/scripts/markdown_image_vision.py annotate article.md --parallel --workers 4
```

对单张图片进行识别：

```bash
python3 markdown-image-vision/scripts/markdown_image_vision.py describe ./image.png
python3 markdown-image-vision/scripts/markdown_image_vision.py describe "https://example.com/image.png"
```

将微信公众号文章转换为 Markdown：

```bash
python3 markdown-image-vision/scripts/markdown_image_vision.py wechat "https://mp.weixin.qq.com/s/xxx" --output-dir ./output
```

默认文件名格式为：

```text
YYMMDD_原文标题.md
```

将微信公众号文章转换为 Markdown，并给所有图片写入 OCR 注释：

```bash
python3 markdown-image-vision/scripts/markdown_image_vision.py wechat "https://mp.weixin.qq.com/s/xxx" --output-dir ./output --annotate-images --keep-empty
```

启用图片 OCR 时，默认额外输出：

```text
YYMMDD_原文标题_OCR.md
```

如果希望并行处理图片，追加 `--parallel --workers 4`；如果并行请求不稳定，去掉 `--parallel` 即回到串行。

## 识别模式

- `mixed`（默认）：提取图片中的文字、表格和主要视觉信息。内容过少时返回"该图片无任何信息，请删除。"
- `ocr`：专注提取可读文字，表格输出为 Markdown 表格。
- `describe`：客观描述图片内容、结构、可见文字和关键信息。

## Markdown 图片格式支持

支持以下所有写法：

```markdown
![](local.png)
![alt](https://example.com/a.png)
[![](https://example.com/a.png)](https://example.com/page)
<img src="https://example.com/a.png">
```

识别结果以 HTML 注释形式插入图片下方：

```markdown
![alt](https://example.com/a.png)

<!-- OCR内容：
识别结果

-->
```

## 跳过已有注释的行为

默认情况下，已有 `<!-- OCR内容：...-->` 注释的图片会被**跳过**，不重复识别，保留原有注释。

如需强制重新识别全部图片，加 `--force` 参数。

## 参数说明

| 参数 | 说明 |
|------|------|
| `--output <文件>` | 将识别结果写入指定文件，不指定则原地修改 |
| `--mode <模式>` | 选择识别模式：`mixed`（默认）、`ocr`、`describe` |
| `--dry-run` | 预览模式，仅输出结果，不写文件 |
| `--keep-empty` | 空图片（无信息）也保留注释；不加此参数时空图片不添加注释 |
| `--force` | 强制重新识别所有图片，删除旧注释后重新生成 |
| `--parallel` | 并行识别图片；不加此参数时串行识别 |
| `--workers <数量>` | 并行线程数，仅 `--parallel` 生效，默认 4 |
| `--prompt <文本>` | 自定义提示词，覆盖默认提示词 |
| `--prompt-file <文件>` | 从文件中读取自定义提示词 |

### 微信文章子命令参数

```bash
python3 markdown-image-vision/scripts/markdown_image_vision.py wechat <微信文章URL> --output-dir <目录> [--filename <文件名>] [--annotate-images]
```

| 参数 | 说明 |
|------|------|
| `--output-dir <目录>` | Markdown 输出目录 |
| `--filename <文件名>` | 指定输出文件名；不指定时按 `YYMMDD_原文标题.md` 自动命名 |
| `--annotate-images` | 转换完成后继续给图片添加 OCR 注释 |
| `--annotated-output <文件>` | OCR 标注结果另存为指定文件；不指定则输出为 `YYMMDD_原文标题_OCR.md` |
| `--keep-empty` | 对低信息量图片也写入 OCR 注释，用于确保每张图都有标注 |
| `--parallel --workers 4` | 可选并行识别；并行异常或限流时去掉 `--parallel` 改为串行 |

## 配置说明

在 `markdown-image-vision/.env` 中填写配置。

必填项：
- `OPENAI_API_KEY`：API 密钥

兼容本项目配置：
- 如果 `.env` 中 `OPENAI_API_KEY` 为空，脚本会尝试读取项目根目录 `SomeURL2MD/openaiapi.json`。
- 可用 `VISION_PLATFORM=poe`、`VISION_PLATFORM=siliconflow` 等指定平台；不指定时优先匹配 `OPENAI_BASE_URL`。

通常需要填写：
- `OPENAI_BASE_URL=https://api.poe.com/v1`
- `OPENAI_MODEL=gemini-2.5-flash-lite`
- `HTTPS_PROXY=http://192.168.31.89:7890`
- `HTTP_PROXY=http://192.168.31.89:7890`
- `ALL_PROXY=http://192.168.31.89:7890`

脚本会自动读取 `.env` 文件，同时也会尊重系统中已有的环境变量。

## 注意事项

- 修改重要文件前，建议先加 `--dry-run` 预览效果。
- 不要将包含真实密钥的 `.env` 提交到代码仓库。
