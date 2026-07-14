"""
一次性回填：对每个还没有 headimg_url 的账号，从它的多篇文章中逐一尝试解析头像并写回 accounts.json。
完成后自动 rebuild monitor-data.json。
用法：python scripts/backfill_account_headimg.py [--limit N] [--max-articles N] [--target-only]
"""
import argparse, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.ingest.common import is_placeholder_url
from app.ingest.content.cover_extractor import REQUEST_HEADERS, extract_account_headimg_from_html, fetch_article_html

ACCOUNTS_PATH = ROOT / "normalized" / "accounts.json"
ARTICLES_PATH = ROOT / "normalized" / "articles.json"


def load_json(p):
    return json.loads(p.read_text(encoding="utf-8"))

def save_json(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 个账号，0=全部")
    parser.add_argument("--max-articles", type=int, default=3, help="每个账号最多尝试几篇文章 URL，0=全部")
    parser.add_argument("--target-only", action="store_true", help="只处理已有真实URL文章但还缺头像的账号（跳过 placeholder URL 的）")
    args = parser.parse_args()

    accounts = load_json(ACCOUNTS_PATH)
    articles = load_json(ARTICLES_PATH)

    # 每个账号收集所有有真实 URL 的文章列表
    acct_articles: dict[str, list[dict]] = {}
    for art in articles:
        aid = art.get("account_id", "")
        url = art.get("raw_url", "")
        if aid and url and not is_placeholder_url(url):
            acct_articles.setdefault(aid, []).append({"article_id": art["article_id"], "url": url})

    acct_map = {a["account_id"]: a for a in accounts}

    # 过滤出需要回填的账号
    targets: dict[str, list[dict]] = {}
    for aid, art_list in acct_articles.items():
        acct = acct_map.get(aid)
        if not acct or acct.get("headimg_url"):
            continue
        if args.target_only and len(art_list) == 0:
            continue
        if args.max_articles > 0:
            art_list = art_list[:args.max_articles]
        targets[aid] = art_list

    if args.limit:
        targets = dict(list(targets.items())[:args.limit])

    print(f"需要回填的账号：{len(targets)} 个")

    ok = fail = skip = retry_ok = 0
    for i, (aid, art_list) in enumerate(targets.items(), 1):
        name = acct_map.get(aid, {}).get("canonical_name", aid)
        headimg = None
        tried = 0
        for info in art_list:
            tried += 1
            try:
                html = fetch_article_html(info["url"], timeout=10)
                headimg = extract_account_headimg_from_html(html)
                if headimg:
                    break
            except Exception:
                continue

        if headimg:
            acct_map[aid]["headimg_url"] = headimg
            if tried > 1:
                retry_ok += 1
                print(f"[{i}/{len(targets)}] ✓ {name}（重试第 {tried} 篇成功）", flush=True)
            else:
                ok += 1
                print(f"[{i}/{len(targets)}] ✓ {name}", flush=True)
        else:
            skip += 1
            tried_n = len(art_list)
            print(f"[{i}/{len(targets)}] — {name}（尝试 {tried_n} 篇 URL 均无头像字段）", flush=True)

        if i % 50 == 0:
            save_json(ACCOUNTS_PATH, list(acct_map.values()))
            print(f"  [checkpoint] 已保存 {i}/{len(targets)}", flush=True)
        time.sleep(0.2)

    save_json(ACCOUNTS_PATH, list(acct_map.values()))
    print(f"\n完成：首次成功 {ok} / 重试成功 {retry_ok} / 全部失败 {skip}")

    print("正在 rebuild monitor-data.json ...")
    from app.ingest.rebuild import rebuild_all
    rebuild_all(verbose=False)
    print("rebuild 完成。")


if __name__ == "__main__":
    main()
