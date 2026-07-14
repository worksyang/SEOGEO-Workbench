#!/usr/bin/env python3
"""
将 output_md 目录下每篇 Markdown 文章中的图片合并成带编号的拼图，
保存到 output/IMG/ 目录。
"""

import math
import re
import statistics
from io import BytesIO
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from PIL import Image, ImageDraw, ImageFont

# ── 配置 ──────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
INPUT_DIR = Path("/Users/works14/Documents/output_md")
OUTPUT_DIR = BASE_DIR / "output" / "IMG"

OUTPUT_SCALE = 2         # 最终输出放大倍数，用于提升导出清晰度
BASE_CELL_SIZE = 400     # 单元格基础尺寸（缩放前）
BASE_PADDING = 8         # 单元格基础内边距（缩放前）
BASE_BADGE_SIZE = 36     # 编号标签基础大小（缩放前）
BASE_BADGE_HORIZONTAL_PADDING = 12  # 编号标签左右内边距（缩放前）
BASE_LABEL_HEIGHT = 44   # 标签行基础高度（缩放前）

CELL_SIZE = BASE_CELL_SIZE * OUTPUT_SCALE
PADDING = BASE_PADDING * OUTPUT_SCALE
BADGE_SIZE = BASE_BADGE_SIZE * OUTPUT_SCALE
BADGE_HORIZONTAL_PADDING = BASE_BADGE_HORIZONTAL_PADDING * OUTPUT_SCALE
LABEL_HEIGHT = BASE_LABEL_HEIGHT * OUTPUT_SCALE

MAX_IMAGES_PER_COLLAGE = 9   # 单张拼图最多容纳的图片数，超过后自动分页
MAX_WORKERS = 10             # 并发下载线程数
DOWNLOAD_TIMEOUT = 15        # 下载超时(秒)

PORTRAIT_RATIO_THRESHOLD = 0.9
LANDSCAPE_RATIO_THRESHOLD = 1.15
PORTRAIT_RATIO_MIN = 9 / 16
PORTRAIT_RATIO_MAX = 4 / 5
LANDSCAPE_RATIO_MIN = 4 / 3
LANDSCAPE_RATIO_MAX = 16 / 9

BG_COLOR = (245, 245, 245)          # 拼图背景色
TILE_BG_COLOR = (239, 239, 239)     # 单元格浅灰背景色
TILE_BORDER_COLOR = (210, 210, 210) # 单元格边框色
LABEL_BG_COLOR = (248, 248, 248)    # 标签行背景色
LABEL_DIVIDER_COLOR = (218, 218, 218)  # 标签与图片区分隔线颜色
BADGE_COLOR = (220, 50, 50)         # 编号标签背景色
BADGE_TEXT_COLOR = (255, 255, 255)  # 编号文字颜色

IMAGE_PATTERN = re.compile(r'!\[.*?\]\((https?://[^)]+)\)')

# 请求头，模拟微信内置浏览器
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 MicroMessenger/7.0",
    "Referer": "https://mp.weixin.qq.com/",
}


def load_font_with_fallback(
    size: int,
    font_candidates: list[str],
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """按候选列表加载字体，全部失败时回退到默认字体。"""
    for font_path in font_candidates:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            continue
    return ImageFont.load_default()


def load_cjk_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载 macOS 上可用的中文字体。"""
    return load_font_with_fallback(
        size,
        [
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/System/Library/Fonts/STHeiti Light.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        ],
    )


def load_badge_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载数字标签字体。"""
    return load_font_with_fallback(
        size,
        [
            "/System/Library/Fonts/Helvetica.ttc",
            "/System/Library/Fonts/SFNS.ttf",
            "/System/Library/Fonts/SFNSMono.ttf",
        ],
    )


def extract_image_urls(md_path: Path) -> list[str]:
    """从 Markdown 文件中提取所有图片 URL。"""
    text = md_path.read_text(encoding="utf-8")
    return [match.group(1) for match in IMAGE_PATTERN.finditer(text)]


def download_image(url: str) -> Image.Image | None:
    """下载图片并返回 PIL Image 对象，失败返回 None。"""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=DOWNLOAD_TIMEOUT, stream=True)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        if hasattr(img, "n_frames") and img.n_frames > 1:
            img.seek(0)
        return img.convert("RGBA")
    except Exception:
        return None


def download_images(urls: list[str]) -> list[Image.Image | None]:
    """并发下载多张图片，保持顺序。"""
    results = [None] * len(urls)
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_to_idx = {pool.submit(download_image, url): i for i, url in enumerate(urls)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            results[idx] = future.result()
    return results


def calc_grid(n: int) -> tuple[int, int]:
    """根据图片数量计算网格大小。"""
    if n <= 0:
        return (0, 0)
    if n == 1:
        return (1, 1)
    if n == 2:
        return (2, 1)
    if n == 3:
        return (3, 1)

    target_ratio = 1.3
    cols = max(2, round(math.sqrt(n * target_ratio)))
    rows = math.ceil(n / cols)

    while rows > cols * 1.5 and cols < n:
        cols += 1
        rows = math.ceil(n / cols)

    return (cols, rows)


def fit_image_in_cell(img: Image.Image, cell_w: int, cell_h: int) -> Image.Image:
    """将图片等比缩放到 cell_w x cell_h 内，并居中放置。"""
    iw, ih = img.size
    avail_w = cell_w - PADDING * 2
    avail_h = cell_h - PADDING * 2
    if avail_w <= 0 or avail_h <= 0:
        return img.resize((cell_w, cell_h), Image.LANCZOS)

    scale = min(avail_w / iw, avail_h / ih)
    new_w = max(1, int(iw * scale))
    new_h = max(1, int(ih * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)

    cell = Image.new("RGBA", (cell_w, cell_h), (255, 255, 255, 255))
    x = (cell_w - new_w) // 2
    y = (cell_h - new_h) // 2
    cell.paste(resized, (x, y), resized)
    return cell


def get_cell_size_for_batch(
    images: list[Image.Image | None],
    cols: int,
    rows: int,
) -> tuple[int, int]:
    """为当前批次计算单元格尺寸；3x3 时按主导宽高比自适应。"""
    cell_w = CELL_SIZE
    cell_h = CELL_SIZE

    if cols != 3 or rows != 3:
        return (cell_w, cell_h)

    ratios = [
        img.width / img.height
        for img in images
        if img is not None and img.width > 0 and img.height > 0
    ]
    if not ratios:
        return (cell_w, cell_h)

    dominant_ratio = statistics.median(ratios)
    target_ratio = 1.0

    if dominant_ratio <= PORTRAIT_RATIO_THRESHOLD:
        target_ratio = max(PORTRAIT_RATIO_MIN, min(PORTRAIT_RATIO_MAX, dominant_ratio))
    elif dominant_ratio >= LANDSCAPE_RATIO_THRESHOLD:
        target_ratio = max(LANDSCAPE_RATIO_MIN, min(LANDSCAPE_RATIO_MAX, dominant_ratio))

    cell_h = max(1, round(cell_w / target_ratio))
    return (cell_w, cell_h)


def make_placeholder(cell_w: int, cell_h: int, text: str = "下载失败") -> Image.Image:
    """生成灰色占位图。"""
    cell = Image.new("RGBA", (cell_w, cell_h), (200, 200, 200, 255))
    draw = ImageDraw.Draw(cell)
    font = load_cjk_font(18 * OUTPUT_SCALE)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    draw.text(((cell_w - tw) // 2, (cell_h - th) // 2), text, fill=(120, 120, 120), font=font)
    return cell


def draw_badge(
    draw: ImageDraw.Draw,
    x: int,
    label_y: int,
    cell_w: int,
    label_h: int,
    number: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """在标签行内居中绘制可容纳三位数的胶囊标签。"""
    text = str(number)
    badge_h = BADGE_SIZE
    radius = badge_h // 2
    cx = x + cell_w // 2
    cy = label_y + label_h // 2

    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    badge_w = max(badge_h, tw + BADGE_HORIZONTAL_PADDING * 2)

    draw.rounded_rectangle(
        [cx - badge_w // 2, cy - badge_h // 2, cx + badge_w // 2, cy + badge_h // 2],
        radius=radius,
        fill=BADGE_COLOR,
        outline=(180, 30, 30),
        width=max(2, OUTPUT_SCALE),
    )
    draw.text(
        (cx - tw // 2 - bbox[0], cy - th // 2 - bbox[1]),
        text,
        fill=BADGE_TEXT_COLOR,
        font=font,
    )


def create_collage(
    images: list[Image.Image | None],
    cols: int,
    rows: int,
    cell_w: int,
    cell_h: int,
    start_number: int = 1,
) -> Image.Image:
    """将图片列表拼成网格并加编号。"""
    tile_h = LABEL_HEIGHT + cell_h
    canvas_w = cols * cell_w
    canvas_h = rows * tile_h
    canvas = Image.new("RGB", (canvas_w, canvas_h), BG_COLOR)

    draw = ImageDraw.Draw(canvas)
    badge_font = load_badge_font(BADGE_SIZE - 8)

    for idx, img in enumerate(images):
        r = idx // cols
        c = idx % cols
        x = c * cell_w
        y = r * tile_h
        label_y = y
        image_y = y + LABEL_HEIGHT

        draw.rectangle(
            [x, y, x + cell_w - 1, y + tile_h - 1],
            fill=TILE_BG_COLOR,
        )

        if img is not None:
            cell = fit_image_in_cell(img, cell_w, cell_h)
        else:
            cell = make_placeholder(cell_w, cell_h)

        if cell.mode == "RGBA":
            bg = Image.new("RGB", cell.size, (255, 255, 255))
            bg.paste(cell, mask=cell.split()[3])
            cell = bg
        canvas.paste(cell, (x, image_y))

        draw.rectangle(
            [x, label_y, x + cell_w - 1, label_y + LABEL_HEIGHT - 1],
            fill=LABEL_BG_COLOR,
        )
        draw.line(
            [(x, image_y), (x + cell_w, image_y)],
            fill=LABEL_DIVIDER_COLOR,
            width=max(1, OUTPUT_SCALE),
        )
        draw.rectangle(
            [x, y, x + cell_w - 1, y + tile_h - 1],
            outline=TILE_BORDER_COLOR,
            width=max(1, OUTPUT_SCALE),
        )

    for idx in range(len(images)):
        r = idx // cols
        c = idx % cols
        x = c * cell_w
        label_y = r * tile_h
        draw_badge(draw, x, label_y, cell_w, LABEL_HEIGHT, start_number + idx, badge_font)

    return canvas


def sanitize_filename(name: str) -> str:
    """清理文件名，移除不安全字符。"""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    if len(name) > 120:
        name = name[:120]
    return name


def calc_batch_sizes(total_images: int, max_per_collage: int) -> list[int]:
    """按最大容量顺序分页，每页最多容纳 max_per_collage 张。"""
    if total_images <= 0:
        return []

    full_pages, remainder = divmod(total_images, max_per_collage)
    batch_sizes = [max_per_collage] * full_pages
    if remainder:
        batch_sizes.append(remainder)
    return batch_sizes


def split_images_into_batches(
    images: list[Image.Image | None],
    max_per_collage: int,
) -> list[list[Image.Image | None]]:
    """把图片按分页规则切分成多个批次。"""
    batch_sizes = calc_batch_sizes(len(images), max_per_collage)
    batches: list[list[Image.Image | None]] = []
    start = 0
    for size in batch_sizes:
        batches.append(images[start:start + size])
        start += size
    return batches


def build_output_paths(out_sub: Path, stem: str, page_count: int) -> list[Path]:
    """根据分页数量生成输出路径。"""
    if page_count <= 1:
        return [out_sub / f"{stem}.jpg"]
    return [out_sub / f"{stem}_{page:02d}.jpg" for page in range(1, page_count + 1)]


def remove_stale_outputs(out_sub: Path, stem: str, keep_paths: list[Path]) -> None:
    """删除同一文章历史遗留但本次不会重写的旧输出文件。"""
    keep_set = {path.resolve() for path in keep_paths}
    patterns = [f"{stem}.jpg", f"{stem}_[0-9][0-9].jpg"]
    for pattern in patterns:
        for path in out_sub.glob(pattern):
            if path.resolve() not in keep_set and path.exists():
                path.unlink()


def process_article(md_path: Path, output_dir: Path) -> list[str] | None:
    """处理单篇文章并返回输出路径列表。"""
    urls = extract_image_urls(md_path)
    if not urls:
        return None

    rel = md_path.relative_to(INPUT_DIR)
    out_sub = output_dir / rel.parent
    out_sub.mkdir(parents=True, exist_ok=True)

    stem = sanitize_filename(md_path.stem)
    batch_sizes = calc_batch_sizes(len(urls), MAX_IMAGES_PER_COLLAGE)
    out_paths = build_output_paths(out_sub, stem, len(batch_sizes))
    remove_stale_outputs(out_sub, stem, out_paths)

    images = download_images(urls)
    image_batches = split_images_into_batches(images, MAX_IMAGES_PER_COLLAGE)
    if not image_batches:
        return None

    saved_paths: list[str] = []
    start_number = 1

    for batch, out_path in zip(image_batches, out_paths):
        cols, rows = calc_grid(len(batch))
        if cols == 0:
            continue

        cell_w, cell_h = get_cell_size_for_batch(batch, cols, rows)
        collage = create_collage(
            batch,
            cols,
            rows,
            cell_w=cell_w,
            cell_h=cell_h,
            start_number=start_number,
        )
        collage.save(str(out_path), "JPEG", quality=95, subsampling=0, optimize=True)
        saved_paths.append(str(out_path))
        start_number += len(batch)

    return saved_paths if saved_paths else None


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    md_files = sorted(INPUT_DIR.rglob("*.md"))
    print(f"找到 {len(md_files)} 篇文章")

    success = 0
    skip = 0
    fail = 0

    for i, md_path in enumerate(md_files, 1):
        rel = md_path.relative_to(INPUT_DIR)
        img_count = len(extract_image_urls(md_path))

        if img_count == 0:
            print(f"[{i}/{len(md_files)}] 跳过（无图片）: {rel}")
            skip += 1
            continue

        print(f"[{i}/{len(md_files)}] 处理中 ({img_count} 张图): {rel}")

        try:
            results = process_article(md_path, OUTPUT_DIR)
            if results:
                saved_names = ", ".join(Path(path).name for path in results)
                print(f"  ✓ 已保存: {saved_names}")
                success += 1
            else:
                skip += 1
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            fail += 1

    print(f"\n完成! 成功: {success}, 跳过: {skip}, 失败: {fail}")
    print(f"输出目录: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
