#!/usr/bin/env python3
"""Convert WeChat public-account article URLs to Markdown."""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Union

import requests
from bs4 import BeautifulSoup, Tag


@dataclass
class ConversionRule:
    filter: Union[str, List[str]]
    replacement: Callable[[str, Tag], str]


class WechatToMarkdownService:
    """微信公众号文章转 Markdown 服务。"""

    def __init__(self) -> None:
        self._last_date_prefix = datetime.now().strftime("%y%m%d")
        self.rules: Dict[str, ConversionRule] = {
            "paragraph": ConversionRule("p", self._process_paragraph),
            "heading": ConversionRule(["h1", "h2", "h3", "h4", "h5", "h6"], self._process_heading),
            "line_break": ConversionRule("br", lambda _content, _node: "\n"),
            "blockquote": ConversionRule("blockquote", lambda content, _node: f"\n\n> {content.strip()}\n\n"),
            "list": ConversionRule(["ul", "ol"], self._process_list),
            "code": ConversionRule(["pre", "code"], self._process_code),
            "strong": ConversionRule(["strong", "b"], lambda content, _node: f"**{content}**" if content.strip() else ""),
            "emphasis": ConversionRule(["em", "i"], lambda content, _node: f"_{content}_" if content.strip() else ""),
            "strikethrough": ConversionRule(["del", "s", "strike"], lambda content, _node: f"~~{content}~~" if content.strip() else ""),
            "image": ConversionRule("img", self._process_image),
            "link": ConversionRule("a", self._process_link),
            "table": ConversionRule("table", self._process_table),
            "section": ConversionRule("section", self._process_section),
        }

    def url_to_markdown(self, url: str, output_path: Union[str, Path], filename: Optional[str] = None) -> Tuple[Path, str]:
        markdown_content, title = self.url_to_markdown_content(url)
        output_dir = Path(output_path).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        if filename:
            output_name = filename if filename.endswith(".md") else f"{filename}.md"
        else:
            output_name = self.build_filename(title)

        output_file = output_dir / output_name
        output_file.write_text(markdown_content, encoding="utf-8")
        return output_file, title

    def url_to_markdown_content(self, url: str) -> Tuple[str, str]:
        html = self._fetch_article_html(url)
        self._last_date_prefix = self._extract_date_prefix(html)
        markdown_content = self.convert_html_to_markdown(html)
        if not markdown_content.strip():
            raise RuntimeError("转换 Markdown 内容失败")

        match = re.search(r"^#\s+(.+?)\s*$", markdown_content, flags=re.MULTILINE)
        title = match.group(1).strip() if match else "未命名文章"
        return markdown_content.rstrip() + "\n", title

    def build_filename(self, title: str, suffix: str = "") -> str:
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or "未命名文章"
        suffix_part = f"_{suffix}" if suffix else ""
        return f"{self._last_date_prefix}_{safe_title}{suffix_part}.md"

    def _extract_date_prefix(self, html_content: str) -> str:
        patterns = [
            r'var\s+ct\s*=\s*["\'](\d{10})["\']',
            r'ct\s*:\s*["\'](\d{10})["\']',
            r'publish_time\s*[:=]\s*["\'](\d{4})-(\d{1,2})-(\d{1,2})',
            r'article:published_time["\'][^>]+content=["\'](\d{4})-(\d{1,2})-(\d{1,2})',
        ]
        for pattern in patterns[:2]:
            match = re.search(pattern, html_content)
            if match:
                return datetime.fromtimestamp(int(match.group(1))).strftime("%y%m%d")
        for pattern in patterns[2:]:
            match = re.search(pattern, html_content)
            if match:
                year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
                return datetime(year, month, day).strftime("%y%m%d")
        return datetime.now().strftime("%y%m%d")

    def batch_urls_to_markdown(self, urls: List[str], output_path: Union[str, Path]) -> List[Tuple[Path, str, str]]:
        results: List[Tuple[Path, str, str]] = []
        for url in urls:
            try:
                file_path, title = self.url_to_markdown(url, output_path)
                results.append((file_path, title, "成功"))
            except Exception as exc:
                results.append((Path(), "", f"失败: {exc}"))
        return results

    def _fetch_article_html(self, url: str) -> str:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://mp.weixin.qq.com/",
        }
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                response = requests.get(url, headers=headers, timeout=30)
                response.raise_for_status()
                response.encoding = "utf-8"
                text = response.text
                if len(text) < 100:
                    raise RuntimeError("获取到的内容异常，可能需要登录或文章已删除")
                if "环境异常" in text or "完成验证" in text:
                    raise RuntimeError("遇到微信环境验证页面，无法直接访问")
                return text
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2**attempt)
        raise RuntimeError(f"获取文章内容失败: {last_error}")

    def convert_html_to_markdown(self, html_content: str) -> str:
        soup = BeautifulSoup(html_content, "html.parser")
        title = self._extract_title(soup)
        content = self._extract_content_node(soup)
        if content is None:
            return f"# {title}\n\n> 无法获取文章内容，可能原因：文章已删除、需要登录访问或链接已过期。\n"

        body = self._process_node(content)
        body = re.sub(r"[ \t]+\n", "\n", body)
        body = re.sub(r"\n{3,}", "\n\n", body)
        return f"# {title}\n\n{body.strip()}\n"

    def _extract_title(self, soup: BeautifulSoup) -> str:
        selectors = [
            "#activity-name",
            ".rich_media_title",
            "h1.rich_media_title",
            "h2.rich_media_title",
            "#js_article_title",
            'meta[property="og:title"]',
            "title",
        ]
        for selector in selectors:
            node = soup.select_one(selector)
            if node is None:
                continue
            if node.name == "meta":
                title = node.get("content", "")
            else:
                title = node.get_text(strip=True)
            if title:
                return title
        return "未命名文章"

    def _extract_content_node(self, soup: BeautifulSoup) -> Optional[Tag]:
        for selector in ["#js_content", ".rich_media_content", ".rich_media_wrp"]:
            node = soup.select_one(selector)
            if node is not None:
                return node
        return None

    def _process_node(self, node: Optional[Tag]) -> str:
        if not node:
            return ""
        if isinstance(node, str):
            return str(node).strip()

        if node.name == "script" or node.name == "style":
            return ""

        for rule in self.rules.values():
            filters = rule.filter if isinstance(rule.filter, list) else [rule.filter]
            if node.name in filters:
                content = "".join(self._process_node(child) for child in node.children)
                return rule.replacement(content, node)

        return "".join(self._process_node(child) for child in node.children)

    def _has_close_parent(self, node: Tag) -> bool:
        return node.parent.name in ["li", "td", "th"] if node.parent else False

    def _process_paragraph(self, content: str, node: Tag) -> str:
        content = content.strip()
        if not content:
            return ""
        if self._has_close_parent(node):
            return content
        return f"\n\n{content}\n\n"

    def _process_heading(self, content: str, node: Tag) -> str:
        level = int(node.name[1])
        content = content.strip()
        return f"\n\n{'#' * level} {content}\n\n" if content else ""

    def _process_section(self, content: str, _node: Tag) -> str:
        return f"\n{content.strip()}\n" if content.strip() else ""

    def _process_code(self, content: str, node: Tag) -> str:
        if node.name == "pre":
            code_node = node.find("code")
            language = ""
            if code_node and code_node.get("class"):
                language = next((item.replace("language-", "") for item in code_node.get("class", []) if item.startswith("language-")), "")
            return f"\n\n```{language}\n{content.strip().replace('```', '````')}\n```\n\n"
        return f"`{content.strip().replace('`', '``')}`" if content.strip() else ""

    def _process_list(self, _content: str, node: Tag) -> str:
        items = node.find_all("li", recursive=False)
        lines: List[str] = []
        for index, item in enumerate(items, 1):
            item_content = self._process_node(item).strip()
            if not item_content:
                continue
            marker = f"{index}." if node.name == "ol" else "-"
            lines.append(f"{marker} {item_content}")
        return "\n" + "\n".join(lines) + "\n" if lines else ""

    def _process_table(self, _content: str, node: Tag) -> str:
        rows = node.find_all("tr")
        if not rows:
            return ""
        table_rows: List[List[str]] = []
        for row in rows:
            cells = [cell.get_text(" ", strip=True).replace("|", "\\|") for cell in row.find_all(["th", "td"])]
            if cells:
                table_rows.append(cells)
        if not table_rows:
            return ""

        max_cols = max(len(row) for row in table_rows)
        normalized = [row + [""] * (max_cols - len(row)) for row in table_rows]
        header = normalized[0]
        body = normalized[1:]
        result = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * max_cols) + " |"]
        result.extend("| " + " | ".join(row) + " |" for row in body)
        return "\n\n" + "\n".join(result) + "\n\n"

    def _process_link(self, content: str, node: Tag) -> str:
        href = node.get("href", "").strip()
        text = content.strip()
        if not href:
            return text
        if "qpic.cn" in href:
            return text
        return f"[{text}]({href})" if text else ""

    def _process_image(self, _content: str, node: Tag) -> str:
        src = node.get("data-src") or node.get("src") or ""
        src = src.strip()
        if not src or "qpic.cn" not in src:
            return ""
        alt = node.get("alt", "图片").replace("[", "\\[").replace("]", "\\]").strip() or "图片"
        title = node.get("title", "").strip()
        title_part = f' "{title}"' if title else ""
        return f"\n\n![{alt}]({src}{title_part})\n\n"


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert WeChat article URLs to Markdown.")
    parser.add_argument("url")
    parser.add_argument("--output-dir", type=Path, default=Path.cwd())
    parser.add_argument("--filename")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    file_path, title = WechatToMarkdownService().url_to_markdown(args.url, args.output_dir, args.filename)
    print(f"文章标题: {title}")
    print(f"保存路径: {file_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
