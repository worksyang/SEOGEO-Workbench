#!/usr/bin/env python3
"""
扫描 Raw 层所有 .md 文件，提取图片 URL 和紧跟的 OCR 注释，写入 ocr-db.json
格式：{ "图片core": { "ocr": "...", "source": "相对路径.md" }, ... }

key 用 url_core（图片身份 ID），而非完整 URL：
微信图 URL 的 /640 尺寸、?query、#锚点都会变（/640 改 /0 即原图），
只有 img_id 那段不变。用 core 做主键，同一张图无论哪种写法都能对上。
同一张图被多篇 Raw 引用、各做过一次 OCR 时，取内容最长那条。
"""
import os, re, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ocr-db.json')

# Wiki 层和特殊目录不扫描（只扫 Raw 层）
SKIP_DIRS = {'wiki', 'output', '候选区', 'wiki-viewer', '.obsidian', '.git'}

# 匹配 ![xxx](URL) 后紧跟的 <!-- OCR内容：...内容... -->
PATTERN = re.compile(
    r'!\[[^\]]*\]\((https?://[^)]+)\)\s*\n\n?<!-- OCR内容：(.*?)-->',
    re.DOTALL
)


def url_core(url):
    """抽取图片 URL 的不变身份 ID：去掉 #anchor、?query、末尾 /尺寸。"""
    u = url.split('#')[0]
    u = u.split('?')[0]
    u = re.sub(r'/\d+$', '', u)
    return u


def better(a, b):
    """两条记录取 OCR 内容更长的那条（信息更全）。"""
    if a is None:
        return b
    if b is None:
        return a
    return a if len(a.get('ocr', '')) >= len(b.get('ocr', '')) else b


def scan():
    db = {}
    if os.path.exists(OUT):
        with open(OUT, 'r', encoding='utf-8') as f:
            old = json.load(f)
        # 旧库可能按完整 URL 存，迁移成 core key（重复取最长）
        for k, v in old.items():
            c = url_core(k)
            db[c] = better(db.get(c), v)

    added = 0
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # 剪枝：跳过特殊目录
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
        for fname in filenames:
            if not fname.endswith('.md'):
                continue
            fpath = os.path.join(dirpath, fname)
            rel   = os.path.relpath(fpath, ROOT)
            text  = open(fpath, encoding='utf-8').read()
            for url, ocr_text in PATTERN.findall(text):
                core = url_core(url)
                rec = {'ocr': ocr_text.strip(), 'source': rel}
                prev = db.get(core)
                if prev is None:
                    db[core] = rec
                    added += 1
                else:
                    # 同一图被多篇引用各做过 OCR，取内容最长那条
                    db[core] = better(prev, rec)

    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    print(f'完成：共 {len(db)} 条记录，本次新增 {added} 条 → {OUT}')


if __name__ == '__main__':
    scan()
