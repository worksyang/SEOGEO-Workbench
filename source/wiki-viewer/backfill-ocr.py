#!/usr/bin/env python3
"""
backfill-ocr.py —— 给 Wiki 母页引用、但 ocr-db.json 里缺失的图片补 OCR。

为什么需要：早期入库的 Raw 文章图片没做 OCR 注释，导致 wiki-viewer lightbox
对这些图显示「（无 OCR 记录）」。本脚本只补 Wiki 母页里引用到的图
（母页没引用的不补，用不上）。

流程：
  1. 扫 wiki/ 所有 .md，收集图片 URL，算出 core（图片身份 ID）；
  2. 跟 ocr-db.json 比对，找出 db 里没有的 core；
  3. 对每个缺失 core，在 Raw 层定位它出现的源文件，调 zk-vision-workflow
     的 MinerU 引擎对该图 URL 跑 OCR，拿到文字；
  4. 把 OCR 结果以 <!-- OCR内容：… --> 注释写回 Raw 源文件该图标签下方
     （和入库时格式一致，build-ocr-db.py 重建时会自动收录）；
  5. 断点续跑：已处理的 core 记在 ocr-backfill-done.json，重跑自动跳过。

跑完后执行：python3 build-ocr-db.py  重建库即可生效。

用法：
  python3 backfill-ocr.py              # 补全部缺失
  python3 backfill-ocr.py --limit 5    # 只跑前 5 张（试跑）
  python3 backfill-ocr.py --force      # 忽略 done 记录，重新处理
"""
import os, re, sys, json, argparse
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..'))
OCR_DB = os.path.join(HERE, 'ocr-db.json')
DONE = os.path.join(HERE, 'ocr-backfill-done.json')

# zk-vision-workflow skill 路径
SKILL_DIR = '/Users/works14/.skills-manager/skills/zk-vision-workflow'
sys.path.insert(0, os.path.join(SKILL_DIR, 'scripts'))

# Raw 层目录（与 build-ocr-db.py 一致，排除特殊目录）
SKIP_DIRS = {'wiki', 'output', '候选区', 'wiki-viewer', '.obsidian', '.git'}

IMG_RE = re.compile(r'!\[[^\]]*\]\((https?://[^)]+)\)')


def url_core(url):
    u = url.split('#')[0]
    u = u.split('?')[0]
    u = re.sub(r'/\d+$', '', u)
    return u


def collect_wiki_images():
    """扫 wiki/ 所有 md，返回 [(core, url, wiki_rel)]。"""
    out = []
    wiki_root = os.path.join(ROOT, 'wiki')
    for dirpath, dirnames, filenames in os.walk(wiki_root):
        for fname in filenames:
            if not fname.endswith('.md'):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, ROOT)
            text = open(fpath, encoding='utf-8').read()
            for m in IMG_RE.finditer(text):
                url = m.group(1)
                out.append((url_core(url), url, rel))
    return out


def build_raw_index():
    """建立 img_id → (raw_abs_path, raw_rel) 索引，遍历一次 Raw 层。
    img_id 取 core 末尾一段。"""
    index = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith('.')]
        for fname in filenames:
            if not fname.endswith('.md'):
                continue
            fpath = os.path.join(dirpath, fname)
            rel = os.path.relpath(fpath, ROOT)
            text = open(fpath, encoding='utf-8').read()
            for m in IMG_RE.finditer(text):
                c = url_core(m.group(1))
                if c not in index:
                    index[c] = (fpath, rel, text)
    return index


def write_back_ocr(raw_path, img_id, ocr_text):
    """在 raw 文件里定位含 img_id 的图片标签，在其后插入 OCR 注释。返回是否写入。"""
    text = open(raw_path, encoding='utf-8').read()
    esc = re.escape(img_id)
    m = re.search(r'(!\[[^\]]*\]\([^)]*' + esc + r'[^)]*\))', text)
    if not m:
        return False
    # 已紧跟 OCR 注释则不重复写
    tail = text[m.end():m.end() + 30]
    if '<!-- OCR内容' in tail:
        return False
    comment = '\n\n<!-- OCR内容：\n' + ocr_text.strip() + '\n-->'
    new_text = text[:m.end()] + comment + text[m.end():]
    open(raw_path, 'w', encoding='utf-8').write(new_text)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--limit', type=int, default=0, help='只处理前 N 张（试跑）')
    ap.add_argument('--force', action='store_true', help='忽略 done 记录重新处理')
    args = ap.parse_args()

    # 加载 skill 环境与识别函数
    import markdown_image_vision as miv
    miv.load_env()
    cache_dir = Path('/tmp/ocr-backfill-cache')
    cache_dir.mkdir(parents=True, exist_ok=True)

    db = json.load(open(OCR_DB, encoding='utf-8')) if os.path.exists(OCR_DB) else {}
    wiki_imgs = collect_wiki_images()
    print(f'Wiki 母页图片引用: {len(wiki_imgs)} 条')

    # 缺失的 core（去重，保留一个代表 url）
    missing = {}
    for core, url, wrel in wiki_imgs:
        if core not in db:
            missing.setdefault(core, (url, wrel))
    print(f'ocr-db 缺失: {len(missing)} 条')

    done = {}
    if not args.force and os.path.exists(DONE):
        done = json.load(open(DONE, encoding='utf-8'))

    raw_index = build_raw_index()
    print(f'Raw 层图片索引: {len(raw_index)} 条\n')

    todo = list(missing.items())
    if args.limit:
        todo = todo[:args.limit]

    ok = fail = no_src = skip = 0
    for i, (core, (url, wrel)) in enumerate(todo, 1):
        if core in done and not args.force:
            skip += 1
            continue
        img_id = core.split('/')[-1]
        rec = raw_index.get(core)
        if not rec:
            print(f'[{i}/{len(todo)}] ⚠️ Raw 层找不到源文件，跳过  wiki={wrel}  core末段={img_id[:24]}…')
            done[core] = 'no_raw_source'
            no_src += 1
            continue
        raw_path, raw_rel, _ = rec
        print(f'[{i}/{len(todo)}] OCR中  raw={raw_rel}  core末段={img_id[:24]}…')
        try:
            ocr = miv.recognize_image_ref(
                url, None, cache_dir, timeout=180,
                mode='mixed', prompt=None, engine='mineru',
            )
        except Exception as e:
            ocr = f'图片识别失败：{e}'
        if ocr.startswith('图片识别失败') or ocr.strip() in {miv.NO_INFO_RESULT, miv.DEAD_IMAGE_RESULT}:
            print(f'        ❌ 识别失败/无内容: {ocr[:80]}')
            done[core] = 'fail:' + ocr[:80]
            fail += 1
            continue
        wrote = write_back_ocr(raw_path, img_id, ocr)
        if wrote:
            print(f'        ✅ 写回 {raw_rel}  (OCR {len(ocr)} 字)')
            done[core] = 'ok'
            ok += 1
        else:
            print(f'        ⚠️ 定位图片标签失败，未写入')
            done[core] = 'write_fail'
            fail += 1
        # 每张落盘一次 done，便于断点续跑
        json.dump(done, open(DONE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

    print(f'\n完成：成功 {ok}，失败 {fail}，无Raw源 {no_src}，跳过 {skip}')
    print('下一步：python3 build-ocr-db.py  重建库')


if __name__ == '__main__':
    main()
