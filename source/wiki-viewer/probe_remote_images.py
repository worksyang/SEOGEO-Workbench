#!/usr/bin/env python3
"""Probe remote Markdown images and flag likely dead placeholders."""

from __future__ import annotations

import argparse
import json
import random
import re
import ssl
import struct
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse


MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)")
DEFAULT_DOMAINS = {"mmbiz.qpic.cn", "mmecoa.qpic.cn"}
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
}


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Scan Markdown image URLs and flag likely dead placeholder images."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=here.parent,
        help="Markdown root to scan. Default: output_md repo root.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Sample size when --all is not set. Default: 50.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan all unique image URLs instead of sampling.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for sampling. Default: 42.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Concurrent workers. Default: 8.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Per-request timeout in seconds. Default: 20.",
    )
    parser.add_argument(
        "--max-read",
        type=int,
        default=65536,
        help="Max bytes to read per image when content-length is large. Default: 65536.",
    )
    parser.add_argument(
        "--domains",
        default=",".join(sorted(DEFAULT_DOMAINS)),
        help="Comma-separated domain allowlist. Empty means all domains.",
    )
    parser.add_argument(
        "--min-bytes",
        type=int,
        default=1024,
        help="Flag images at or below this size. Default: 1024.",
    )
    parser.add_argument(
        "--max-height",
        type=int,
        default=16,
        help="Flag images at or below this height. Default: 16.",
    )
    parser.add_argument(
        "--strip-width",
        type=int,
        default=200,
        help="Minimum width for narrow-strip detection. Default: 200.",
    )
    parser.add_argument(
        "--strip-height",
        type=int,
        default=20,
        help="Maximum height for narrow-strip detection. Default: 20.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        help="Optional path to write the full JSON report.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="How many suspicious rows to print. Default: 20.",
    )
    return parser.parse_args()


def normalize_url(url: str) -> str:
    url = url.replace("\\u0026", "&")
    return re.sub(r"#imgIndex=\d+$", "", url)


def iter_markdown_images(root: Path, domains: set[str]) -> Iterable[tuple[str, int, str]]:
    for path in root.rglob("*.md"):
        if "wiki-viewer" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for line_no, line in enumerate(text.splitlines(), 1):
            for match in MARKDOWN_IMAGE_RE.finditer(line):
                url = normalize_url(match.group(1))
                domain = urlparse(url).netloc.lower()
                if domains and domain not in domains:
                    continue
                yield rel, line_no, url


def parse_png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", data[16:24])
    return None


def parse_gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10 and (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
        return struct.unpack("<HH", data[6:10])
    return None


def parse_jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker in (0xD8, 0xD9):
            continue
        if index + 2 > len(data):
            break
        seg_len = struct.unpack(">H", data[index : index + 2])[0]
        if seg_len < 2 or index + seg_len > len(data):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height, width = struct.unpack(">HH", data[index + 3 : index + 7])
            return width, height
        index += seg_len
    return None


def parse_webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk_type = data[12:16]
    if chunk_type == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk_type == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    if chunk_type == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return width, height
    return None


def detect_dimensions(data: bytes) -> tuple[int, int] | None:
    for parser in (parse_png_size, parse_gif_size, parse_jpeg_size, parse_webp_size):
        size = parser(data)
        if size:
            return size
    return None


def should_stop_reading(buffer: bytes) -> bool:
    return detect_dimensions(buffer) is not None


def fetch_probe(
    url: str,
    timeout: float,
    max_read: int,
    ssl_ctx: ssl.SSLContext,
) -> tuple[dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=DEFAULT_HEADERS)
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_ctx) as response:
        headers = {key.lower(): value for key, value in response.getheaders()}
        content_length = headers.get("content-length")
        read_limit = max_read
        if content_length and content_length.isdigit():
            read_limit = min(int(content_length), max_read)
        chunks: list[bytes] = []
        total = 0
        while total < read_limit:
            chunk = response.read(min(8192, read_limit - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if should_stop_reading(b"".join(chunks)):
                break
        return headers, b"".join(chunks)


def classify(
    headers: dict[str, str],
    body: bytes,
    min_bytes: int,
    max_height: int,
    strip_width: int,
    strip_height: int,
) -> tuple[list[str], tuple[int, int] | None]:
    flags: list[str] = []
    x_info = headers.get("x-info", "")
    x_errno = headers.get("x-errno", "")
    content_length = headers.get("content-length")
    dimensions = detect_dimensions(body)

    if "notexist" in x_info.lower() or (x_errno and x_errno != "0"):
        flags.append("header_error")

    if content_length and content_length.isdigit():
        if int(content_length) <= min_bytes:
            flags.append("tiny_body")
    elif len(body) <= min_bytes:
        flags.append("tiny_body")

    if dimensions:
        width, height = dimensions
        if height <= max_height:
            flags.append("tiny_height")
        if width >= strip_width and height <= strip_height:
            flags.append("banner_strip")

    return flags, dimensions


def classify_severity(flags: list[str]) -> str | None:
    if not flags:
        return None
    flag_set = set(flags)
    if "header_error" in flag_set:
        return "high"
    if {"tiny_body", "tiny_height"} <= flag_set:
        return "high"
    if {"tiny_body", "banner_strip"} <= flag_set:
        return "high"
    if len(flag_set) >= 2:
        return "medium"
    return "review"


def probe_one(
    item: tuple[str, tuple[str, int]],
    timeout: float,
    max_read: int,
    min_bytes: int,
    max_height: int,
    strip_width: int,
    strip_height: int,
    ssl_ctx: ssl.SSLContext,
) -> dict[str, object]:
    url, (path, line_no) = item
    record: dict[str, object] = {
        "path": path,
        "line": line_no,
        "url": url,
    }
    try:
        headers, body = fetch_probe(url, timeout=timeout, max_read=max_read, ssl_ctx=ssl_ctx)
        flags, dimensions = classify(
            headers=headers,
            body=body,
            min_bytes=min_bytes,
            max_height=max_height,
            strip_width=strip_width,
            strip_height=strip_height,
        )
        content_length = headers.get("content-length")
        record.update(
            {
                "content_type": headers.get("content-type", ""),
                "content_length": int(content_length) if content_length and content_length.isdigit() else None,
                "downloaded_bytes": len(body),
                "x_info": headers.get("x-info", ""),
                "x_errno": headers.get("x-errno", ""),
                "flags": flags,
                "severity": classify_severity(flags),
                "width": dimensions[0] if dimensions else None,
                "height": dimensions[1] if dimensions else None,
            }
        )
    except urllib.error.HTTPError as exc:
        flags = ["request_failed"]
        record.update(
            {"flags": flags, "severity": classify_severity(flags), "error": f"HTTPError {exc.code}"}
        )
    except Exception as exc:  # noqa: BLE001
        flags = ["request_failed"]
        record.update(
            {"flags": flags, "severity": classify_severity(flags), "error": f"{type(exc).__name__}: {exc}"}
        )
    return record


def make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def format_dimensions(width: object, height: object) -> str:
    if isinstance(width, int) and isinstance(height, int):
        return f"{width}x{height}"
    return "-"


def print_summary(total_refs: int, total_unique: int, scanned: int, flagged: list[dict[str, object]]) -> None:
    high = sum(1 for row in flagged if row.get("severity") == "high")
    medium = sum(1 for row in flagged if row.get("severity") == "medium")
    review = sum(1 for row in flagged if row.get("severity") == "review")
    print(f"markdown_refs={total_refs}")
    print(f"unique_urls={total_unique}")
    print(f"scanned_urls={scanned}")
    print(f"flagged_urls={len(flagged)}")
    print(f"high_confidence={high}")
    print(f"medium_confidence={medium}")
    print(f"review_only={review}")


def print_rows(rows: list[dict[str, object]], top: int) -> None:
    if not rows:
        print("\nNo suspicious images found in this run.")
        return
    print("\nSuspicious images:")
    print("flags | bytes | dimensions | file:line")
    print("-" * 120)
    for row in rows[:top]:
        size = row.get("content_length") or row.get("downloaded_bytes") or "-"
        dims = format_dimensions(row.get("width"), row.get("height"))
        flags = ",".join(row.get("flags", []))  # type: ignore[arg-type]
        severity = row.get("severity") or "-"
        print(f"[{severity:6}] {flags:38} | {str(size):>8} | {dims:12} | {row['path']}:{row['line']}")
        if row.get("x_info") or row.get("x_errno"):
            print(f"  x-info={row.get('x_info', '')} x-errno={row.get('x_errno', '')}")
        if row.get("error"):
            print(f"  error={row['error']}")
        print(f"  {row['url']}")


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        print(f"Root not found: {root}", file=sys.stderr)
        return 1

    domains = {d.strip().lower() for d in args.domains.split(",") if d.strip()}
    refs = list(iter_markdown_images(root, domains))
    unique: dict[str, tuple[str, int]] = {}
    for path, line_no, url in refs:
        unique.setdefault(url, (path, line_no))

    items = list(unique.items())
    if not args.all:
        rng = random.Random(args.seed)
        rng.shuffle(items)
        items = items[: min(args.limit, len(items))]

    ssl_ctx = make_ssl_context()
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [
            pool.submit(
                probe_one,
                item,
                args.timeout,
                args.max_read,
                args.min_bytes,
                args.max_height,
                args.strip_width,
                args.strip_height,
                ssl_ctx,
            )
            for item in items
        ]
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(
        key=lambda row: (
            len(row.get("flags", [])) == 0,  # type: ignore[arg-type]
            {"high": 0, "medium": 1, "review": 2, None: 3}.get(row.get("severity"), 3),
            row.get("path", ""),
            row.get("line", 0),
        )
    )
    flagged = [row for row in results if row.get("flags")]

    print_summary(
        total_refs=len(refs),
        total_unique=len(unique),
        scanned=len(results),
        flagged=flagged,
    )
    print_rows(flagged, top=max(0, args.top))

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                {
                    "root": str(root),
                    "markdown_refs": len(refs),
                    "unique_urls": len(unique),
                    "scanned_urls": len(results),
                    "flagged_urls": len(flagged),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nJSON report written to {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
