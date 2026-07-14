#!/usr/bin/env python3
"""Annotate Markdown images or describe a single image with a vision model."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote, urlparse

import requests
from openai import OpenAI

try:
    from wechat_to_markdown import WechatToMarkdownService
except ImportError:
    WechatToMarkdownService = None


SKILL_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = SKILL_DIR / ".env"
OCR_BEGIN = "<!-- OCR内容："
OCR_END = "-->"


PROMPTS: Dict[str, str] = {
    "mixed": """你是一个严谨的图片信息提取助手。

请识别图片中的文字、表格和主要视觉信息，输出简体中文 Markdown。

规则：
1. 如果图片包含表格、对比图、费率表、截图文字，请尽量完整转为 Markdown 文本或 Markdown 表格。
2. 如果图片是普通照片或示意图，请用 300 字以内客观描述它表达的信息。
3. 如果图片只是装饰、纯色块、无意义图案、低信息量表情包、分隔线，请只返回：该图片无任何信息，请删除。
4. 不要编造图片外的信息；看不清的内容标注为“无法辨认”。
5. 只输出最终结果，不要输出分析过程。""",
    "ocr": """请提取图片中的可读文字，输出简体中文 Markdown。

规则：
1. 保留标题、段落、列表、数字、单位和可辨认的关键符号。
2. 如果有表格，请尽量输出为 Markdown 表格，并保证每一行列数一致。
3. 看不清的内容标注为“无法辨认”。
4. 如果没有可读文字，请返回：该图片无任何信息，请删除。
5. 只输出最终结果，不要输出分析过程。""",
    "describe": """请客观描述这张图片的主要内容、结构、可见文字和关键信息。

规则：
1. 使用简体中文。
2. 描述必须基于图片可见内容，不要推测图片外的信息。
3. 如果存在明显文字，请摘录关键文字。
4. 如果有图表或表格，请说明它的主题和主要字段。
5. 只输出最终结果，不要输出分析过程。""",
}


@dataclass(frozen=True)
class ImageToken:
    full_text: str
    image_text: str
    url: str
    start: int
    end: int


def load_env(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def is_remote_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def strip_markdown_title(url_part: str) -> str:
    text = url_part.strip()
    if not text:
        return text
    if text.startswith("<") and ">" in text:
        return text[1 : text.index(">")]
    match = re.match(r"^(.*?)(?:\s+(['\"]).*?\2)\s*$", text, flags=re.DOTALL)
    return match.group(1).strip() if match else text


def normalize_url(url: str) -> str:
    return unquote(strip_markdown_title(url).strip())


def find_markdown_images(content: str) -> List[ImageToken]:
    tokens: List[ImageToken] = []
    occupied: List[Tuple[int, int]] = []

    linked_pattern = re.compile(
        r"\[(!\[[^\]]*]\((?P<img_url>[^)]+)\))]\((?P<link_url>[^)]+)\)",
        re.DOTALL,
    )
    for match in linked_pattern.finditer(content):
        url = normalize_url(match.group("img_url"))
        tokens.append(ImageToken(match.group(0), match.group(1), url, match.start(), match.end()))
        occupied.append((match.start(), match.end()))

    image_pattern = re.compile(r"!\[[^\]]*]\((?P<img_url>[^)]+)\)", re.DOTALL)
    for match in image_pattern.finditer(content):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        url = normalize_url(match.group("img_url"))
        tokens.append(ImageToken(match.group(0), match.group(0), url, match.start(), match.end()))

    html_pattern = re.compile(r"<img\b[^>]*\bsrc=[\"'](?P<src>[^\"']+)[\"'][^>]*>", re.IGNORECASE | re.DOTALL)
    for match in html_pattern.finditer(content):
        url = normalize_url(match.group("src"))
        tokens.append(ImageToken(match.group(0), match.group(0), url, match.start(), match.end()))

    tokens.sort(key=lambda item: item.start)
    return tokens


def guess_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "image/jpeg"


def resolve_local_image_path(image_ref: str, markdown_path: Optional[Path]) -> Path:
    parsed = urlparse(image_ref)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path)).expanduser()

    candidate = Path(image_ref).expanduser()
    if candidate.is_absolute():
        return candidate

    if markdown_path is not None:
        return (markdown_path.parent / candidate).resolve()

    return candidate.resolve()


def download_image(url: str, cache_dir: Path, timeout: int) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
    parsed_path = Path(urlparse(url).path)
    suffix = parsed_path.suffix if parsed_path.suffix else ".img"
    output = cache_dir / f"{digest}{suffix}"
    if output.exists() and output.stat().st_size > 0:
        return output

    response = requests.get(
        url,
        timeout=timeout,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mp.weixin.qq.com/",
        },
    )
    response.raise_for_status()
    output.write_bytes(response.content)
    return output


def image_ref_to_path(image_ref: str, markdown_path: Optional[Path], cache_dir: Path, timeout: int) -> Path:
    if is_remote_url(image_ref):
        return download_image(image_ref, cache_dir, timeout)
    path = resolve_local_image_path(image_ref, markdown_path)
    if not path.exists():
        raise FileNotFoundError(f"图片不存在: {path}")
    return path


def image_to_data_url(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{guess_mime(path)};base64,{data}"


def make_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.poe.com/v1").strip()
    if not api_key:
        legacy = load_legacy_openai_credentials(base_url)
        if legacy:
            api_key, base_url = legacy
    if not api_key:
        raise RuntimeError(f"缺少 OPENAI_API_KEY，请填写 {ENV_PATH}")

    kwargs = {
        "api_key": api_key,
        "base_url": base_url,
        "timeout": float(os.getenv("VISION_TIMEOUT", "90")),
    }
    return OpenAI(**kwargs)


def load_legacy_openai_credentials(base_url: str) -> Optional[Tuple[str, str]]:
    """兼容本项目既有 SomeURL2MD/openaiapi.json 配置，不输出密钥。"""
    config_path = SKILL_DIR.parent / "SomeURL2MD" / "openaiapi.json"
    if not config_path.exists():
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    platforms = config.get("platforms", {})
    preferred_platform = os.getenv("VISION_PLATFORM", "").strip() or os.getenv("OPENAI_PLATFORM", "").strip()
    if preferred_platform and preferred_platform in platforms:
        platform = platforms[preferred_platform]
        key = platform.get("api_key", "").strip()
        url = platform.get("base_url", "").strip()
        return (key, url) if key and url else None

    for platform in platforms.values():
        key = platform.get("api_key", "").strip()
        url = platform.get("base_url", "").strip()
        if key and url and url.rstrip("/") == base_url.rstrip("/"):
            return key, url

    for platform_name in ("poe", "siliconflow", "chatnp_gemini", "chatnp_gpt"):
        platform = platforms.get(platform_name, {})
        key = platform.get("api_key", "").strip()
        url = platform.get("base_url", "").strip()
        if key and url:
            return key, url
    return None


def extract_markdown_block(text: str) -> str:
    match = re.search(r"```(?:markdown|md)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else text.strip()


def describe_image_path(path: Path, mode: str, prompt: Optional[str] = None) -> str:
    client = make_client()
    selected_prompt = prompt or PROMPTS[mode]
    model = os.getenv("OPENAI_MODEL", "gemini-2.5-flash-lite").strip()
    max_tokens = int(os.getenv("VISION_MAX_TOKENS", "4000"))
    temperature = float(os.getenv("VISION_TEMPERATURE", "0.2"))

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": selected_prompt},
                    {"type": "image_url", "image_url": {"url": image_to_data_url(path)}},
                ],
            }
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    result = response.choices[0].message.content or ""
    return extract_markdown_block(result)


def unique_tokens_by_url(tokens: List[ImageToken]) -> List[ImageToken]:
    seen = set()
    unique: List[ImageToken] = []
    for token in tokens:
        if token.url in seen:
            continue
        seen.add(token.url)
        unique.append(token)
    return unique


def recognize_image_token(
    token: ImageToken,
    markdown_path: Optional[Path],
    cache_dir: Path,
    timeout: int,
    mode: str,
    prompt: Optional[str],
) -> Tuple[str, str]:
    try:
        image_path = image_ref_to_path(token.url, markdown_path, cache_dir, timeout)
        result = describe_image_path(image_path, mode, prompt)
    except Exception as exc:
        result = f"图片识别失败：{exc}"
    return token.url, result


def remove_existing_ocr_after(content: str, pos: int) -> Tuple[str, int]:
    index = pos
    whitespace_match = re.match(r"\s*", content[index:])
    if whitespace_match:
        index += whitespace_match.end()
    if not content.startswith(OCR_BEGIN, index):
        return content, pos
    end = content.find(OCR_END, index)
    if end == -1:
        return content, pos
    end += len(OCR_END)
    trailing = re.match(r"\s*", content[end:])
    if trailing:
        end += trailing.end()
    return content[:pos] + "\n\n" + content[end:], pos + 2


def build_comment(result: str) -> str:
    safe_result = result.replace("-->", "-- >").strip()
    return f"\n\n{OCR_BEGIN}\n{safe_result}\n\n{OCR_END}\n\n"


def has_ocr_after_pos(content: str, pos: int) -> bool:
    index = pos
    ws = re.match(r"\s*", content[index:])
    if ws:
        index += ws.end()
    return content.startswith(OCR_BEGIN, index)


def extract_ocr_block(content: str, after_pos: int) -> str:
    """提取 <!-- OCR内容：...--> 块（含标签），用于保留旧注释。

    用 <!-- OCR内容：`` 定位开头（避免误匹配内容里的 <!--``），
    用 ``\n-->`` 定位结尾（避免误匹配内容里的 ``-->``）。
    """
    index = after_pos
    ws = re.match(r"\s*", content[index:])
    if ws:
        index += ws.end()
    ocr_start = content.find(OCR_BEGIN, index)
    if ocr_start == -1:
        return ""
    # 找 \n--> 结尾（确保是真正的注释结束，而非内容中的 -->）
    end_search_start = ocr_start + len(OCR_BEGIN)
    end_match = re.search(r"\n--+>", content[end_search_start:])
    if not end_match:
        return ""
    ocr_end = end_search_start + end_match.end()
    block = content[ocr_start:ocr_end]
    # 去掉首尾空白后提取 inner（注意：去掉首尾时 -->` 不会混入内部）
    inner = block[len(OCR_BEGIN) : -len(OCR_END)].strip()
    # 压缩内部连续空白
    inner = re.sub(r"[ \t]+\n", "\n", inner)
    inner = re.sub(r"\n{3,}", "\n\n", inner)
    # 安全化残留的 -->`（罕见，但会破坏 HTML 注释）
    safe_inner = re.sub(r"(-->)", r"-- >", inner)
    return f"\n\n{OCR_BEGIN}\n{safe_inner}\n\n{OCR_END}\n\n"


def annotate_markdown(
    markdown_path: Path,
    output_path: Optional[Path],
    mode: str,
    prompt: Optional[str],
    dry_run: bool,
    keep_empty: bool,
    force: bool,
    parallel: bool = False,
    workers: int = 4,
) -> str:
    content = markdown_path.read_text(encoding="utf-8")
    tokens = find_markdown_images(content)
    if not tokens:
        result = content
        if output_path and not dry_run:
            output_path.write_text(result, encoding="utf-8")
        return result

    # 预扫描：哪些图片已有 OCR 注释
    # replacements 记录每个 URL 最终要写入的注释内容
    replacements: Dict[str, str] = {}
    skipped: List[str] = []

    for token in tokens:
        if token.url in replacements:
            continue
        if not force and has_ocr_after_pos(content, token.end):
            skipped.append(token.url)
            # 保留旧注释（从 content 中提取）
            old = extract_ocr_block(content, token.end)
            replacements[token.url] = old if old else ""
        # else: 将在下面的识别循环中填充 replacements

    if skipped:
        print(f"跳过已有注释的图片: {len(skipped)} 张（用 --force 强制重新识别）", file=sys.stderr)

    # 识别没有旧注释的图片
    to_recognize = unique_tokens_by_url([t for t in tokens if t.url not in replacements])
    if not to_recognize:
        new_content = content
        if not dry_run:
            target = output_path or markdown_path
            target.write_text(new_content, encoding="utf-8")
        return new_content

    timeout = int(float(os.getenv("VISION_TIMEOUT", "90")))
    with tempfile.TemporaryDirectory(prefix="markdown-image-vision-") as temp_name:
        cache_dir = Path(temp_name)
        if parallel and len(to_recognize) > 1:
            max_workers = max(1, int(workers))
            print(f"并行识别图片: {len(to_recognize)} 张，workers={max_workers}", file=sys.stderr)
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(recognize_image_token, token, markdown_path, cache_dir, timeout, mode, prompt)
                    for token in to_recognize
                ]
                for idx, future in enumerate(as_completed(futures), 1):
                    url, result = future.result()
                    print(f"[{idx}/{len(to_recognize)}] 完成图片: {url}", file=sys.stderr)
                    if result.strip() == "该图片无任何信息，请删除。":
                        replacements[url] = build_comment(result) if keep_empty else ""
                    else:
                        replacements[url] = build_comment(result)
        else:
            for idx, token in enumerate(to_recognize, 1):
                print(f"[{idx}/{len(to_recognize)}] 识别图片: {token.url}", file=sys.stderr)
                url, result = recognize_image_token(token, markdown_path, cache_dir, timeout, mode, prompt)
                if result.strip() == "该图片无任何信息，请删除。":
                    replacements[url] = build_comment(result) if keep_empty else ""
                else:
                    replacements[url] = build_comment(result)

    new_content = content
    for token in reversed(tokens):
        insert_at = token.end
        new_content, insert_at = remove_existing_ocr_after(new_content, insert_at)
        comment = replacements[token.url]
        if comment:
            new_content = new_content[:insert_at] + comment + new_content[insert_at:]

    new_content = re.sub(r"\n{5,}", "\n\n\n\n", new_content).rstrip() + "\n"
    if dry_run:
        return new_content

    target = output_path or markdown_path
    target.write_text(new_content, encoding="utf-8")
    return new_content


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Annotate Markdown images or describe one image.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    annotate = subparsers.add_parser("annotate", help="Annotate images in a Markdown file.")
    annotate.add_argument("markdown", type=Path)
    annotate.add_argument("--output", type=Path)
    annotate.add_argument("--mode", choices=sorted(PROMPTS), default="mixed")
    annotate.add_argument("--prompt", help="Custom prompt text.")
    annotate.add_argument("--prompt-file", type=Path, help="Read custom prompt from a file.")
    annotate.add_argument("--dry-run", action="store_true")
    annotate.add_argument("--keep-empty", action="store_true")
    annotate.add_argument("--force", action="store_true", help="强制重新识别已有注释的图片，默认跳过已有 OCR 注释的图片")
    annotate.add_argument("--parallel", action="store_true", help="并行识别图片；不加则串行识别")
    annotate.add_argument("--workers", type=int, default=4, help="并行识别线程数，仅 --parallel 生效")

    describe = subparsers.add_parser("describe", help="Describe or OCR a single image.")
    describe.add_argument("image")
    describe.add_argument("--mode", choices=sorted(PROMPTS), default="describe")
    describe.add_argument("--prompt", help="Custom prompt text.")
    describe.add_argument("--prompt-file", type=Path, help="Read custom prompt from a file.")

    wechat = subparsers.add_parser("wechat", help="Convert a WeChat article URL to Markdown, optionally annotate images.")
    wechat.add_argument("url")
    wechat.add_argument("--output-dir", type=Path, default=Path.cwd())
    wechat.add_argument("--filename")
    wechat.add_argument("--annotate-images", action="store_true", help="转换后继续给文中的图片添加 OCR 注释")
    wechat.add_argument("--annotated-output", type=Path, help="OCR 标注结果另存为指定文件；不指定则原地更新")
    wechat.add_argument("--mode", choices=sorted(PROMPTS), default="mixed")
    wechat.add_argument("--prompt", help="Custom prompt text.")
    wechat.add_argument("--prompt-file", type=Path, help="Read custom prompt from a file.")
    wechat.add_argument("--keep-empty", action="store_true")
    wechat.add_argument("--force", action="store_true")
    wechat.add_argument("--parallel", action="store_true", help="并行识别图片；不加则串行识别")
    wechat.add_argument("--workers", type=int, default=4, help="并行识别线程数，仅 --parallel 生效")

    return parser.parse_args(argv)


def read_prompt(args: argparse.Namespace) -> Optional[str]:
    if getattr(args, "prompt_file", None):
        return args.prompt_file.read_text(encoding="utf-8")
    return getattr(args, "prompt", None)


def main(argv: Optional[Iterable[str]] = None) -> int:
    load_env()
    args = parse_args(argv)
    prompt = read_prompt(args)

    if args.command == "annotate":
        result = annotate_markdown(
            markdown_path=args.markdown.resolve(),
            output_path=args.output.resolve() if args.output else None,
            mode=args.mode,
            prompt=prompt,
            dry_run=args.dry_run,
            keep_empty=args.keep_empty,
            force=args.force,
            parallel=args.parallel,
            workers=args.workers,
        )
        if args.dry_run:
            print(result)
        return 0

    if args.command == "describe":
        timeout = int(float(os.getenv("VISION_TIMEOUT", "90")))
        with tempfile.TemporaryDirectory(prefix="markdown-image-vision-") as temp_name:
            image_path = image_ref_to_path(args.image, None, Path(temp_name), timeout)
            print(describe_image_path(image_path, args.mode, prompt))
        return 0

    if args.command == "wechat":
        if WechatToMarkdownService is None:
            raise RuntimeError("无法导入 wechat_to_markdown.py，请确认脚本位于同一 scripts 目录")
        service = WechatToMarkdownService()
        output_file, title = service.url_to_markdown(args.url, args.output_dir, args.filename)
        print(f"文章标题: {title}")
        print(f"保存路径: {output_file}")
        if args.annotate_images:
            annotated_output = args.annotated_output.resolve() if args.annotated_output else output_file.with_name(f"{output_file.stem}_OCR.md")
            annotate_markdown(
                markdown_path=output_file.resolve(),
                output_path=annotated_output,
                mode=args.mode,
                prompt=prompt,
                dry_run=False,
                keep_empty=args.keep_empty,
                force=args.force,
                parallel=args.parallel,
                workers=args.workers,
            )
            print(f"OCR标注完成: {annotated_output}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
