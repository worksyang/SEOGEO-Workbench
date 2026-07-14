#!/usr/bin/env python3
"""
DSO 批量抓取脚本 - 用 Playwright 直接打开 AIDSO DSO 详情页, 提取数据
复用 AIDSO MCP 的登录态: --user-data-dir /Users/works14/.aidso-mcp-profile
"""
import json
import re
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/Users/works14/.claude/监控/wechat-ybxhyyh-top3")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.repositories.keyword_registry_repo import KeywordRegistryRepository

OUTPUT_PATH = ROOT / "normalized/aidso_dso_heat.json"
PROFILE_DIR = "/Users/works14/.aidso-mcp-profile"

EXTRACT_JS = r"""
async () => {
  const main = document.querySelector('main');
  if (!main) return {error: 'no main'};

  const txt = main.innerText;
  const lines = txt.split('\n').map(l => l.trim()).filter(l => l);

  const result = {
    keyword: null,
    stats: {},
    down_words: []
  };

  // 1. 提取关键词名
  const dashIdx = lines.findIndex(l => l === '大屏');
  if (dashIdx >= 1) {
    result.keyword = lines[dashIdx - 1];
  }

  // 2. 提取顶部 stats
  const dataIdx = lines.findIndex(l => l === '数据展示');
  if (dashIdx !== -1 && dataIdx !== -1) {
    const statsLines = lines.slice(dashIdx + 1, dataIdx);
    const labels = ['月覆盖人次', '7日平均搜索', '下拉词数量', '下拉词月覆盖', '竞争度', '占比最大城市', '类型'];

    for (let i = 0; i < statsLines.length; i++) {
      const cur = statsLines[i];
      if (labels.includes(cur)) {
        if (i > 0) {
          const prev = statsLines[i - 1];
          if (!labels.includes(prev)) {
            result.stats[cur] = prev;
          }
        }
        if (cur === '竞争度' && !(cur in result.stats)) {
          result.stats[cur] = 'n/a';
        }
      }
    }
  }

  // 3. 提取下拉词
  const exportIdx = lines.findIndex(l => l === '全部导出');
  if (exportIdx !== -1) {
    const endIdx = lines.findIndex((l, i) => i > exportIdx && (l === '脑图' || l === '当前关键词暂无脑图' || l === '关联作品排名'));
    const downEnd = endIdx === -1 ? lines.length : endIdx;
    const downLines = lines.slice(exportIdx + 1, downEnd);

    let i = 0;
    while (i < downLines.length) {
      const tok = downLines[i];
      if (/^\d+$/.test(tok)) {
        const rank = parseInt(tok);
        if (i + 2 < downLines.length && /^[\d.wW\-]+$/.test(downLines[i + 2])) {
          const text = downLines[i + 1];
          if (text && !['关键词', '月覆盖人次', '7日平均搜索', '下拉词数量', '下拉词月覆盖', '操作'].includes(text)) {
            result.down_words.push({rank, text, month_cover_count: downLines[i + 2]});
          }
          i += 3;
          continue;
        }
      }
      i++;
    }
  }

  return result;
}
"""


def load_keywords():
    return KeywordRegistryRepository(
        ROOT / "data/state/app.db"
    ).list_keywords(include_archived=False)


def load_output():
    if not OUTPUT_PATH.exists():
        return {
            "version": 1,
            "source": "aidso_dso",
            "channel": "dso",
            "platform": "douyin",
            "api_endpoints": {
                "detail": "https://task.aidso.com/dso/api/keyword/info/detail",
                "down_word": "https://api.aidso.com/dso/api/keyword/info/down_word",
                "trend": "https://api.aidso.com/dso/api/keyword/search/compare/trend"
            },
            "page_url_template": "https://dso.aidso.com/keyWordDetail/detail?keyword={keyword}",
            "profile_dir": PROFILE_DIR,
            "type_enum_map": {
                "BLUE_SEA_WORD": "蓝海词",
                "PEER_BUY_WORD": "同行买词",
                "REGION_WORD": "区域词",
                "NEWS_WORD": "新闻词",
                "SEARCH_WORD": "搜索词"
            },
            "created_at": "2026-06-14T20:00:00+08:00",
            "updated_at": "2026-06-14T20:00:00+08:00",
            "total_keywords": 0,
            "fetched_count": 0,
            "error_count": 0,
            "items": []
        }
    with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_output(data):
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    data["fetched_count"] = sum(1 for it in data["items"] if it.get("error") is None)
    data["error_count"] = sum(1 for it in data["items"] if it.get("error") is not None)
    data["total_keywords"] = len(data["items"])
    tmp = OUTPUT_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(OUTPUT_PATH)


def parse_count(raw):
    """解析页面显示的数字: '1.17w' -> 11700, '3.04w' -> 30400, '11700' -> 11700, '-' -> 0"""
    if raw is None:
        return 0
    s = str(raw).strip()
    if s in ('', '-', 'n/a', 'N/A', '—', '无'):
        return 0
    s = s.replace(',', '').replace(' ', '')
    # 处理单位
    if s.endswith('亿'):
        try:
            return int(float(s[:-1]) * 100000000)
        except ValueError:
            return 0
    if s.endswith('w') or s.endswith('W') or s.endswith('万'):
        try:
            return int(float(s[:-1]) * 10000)
        except ValueError:
            return 0
    # 纯数字
    try:
        return int(float(s))
    except ValueError:
        return 0


def main():
    keywords = load_keywords()
    output = load_output()

    # 找未抓取的 (跳过已成功的, 失败的会重试)
    success_texts = {it.get("keyword_text") for it in output["items"] if not it.get("error")}
    pending = [kw for kw in keywords if kw["keyword_text"] not in success_texts]
    # 移除失败记录以便重试
    output["items"] = [it for it in output["items"] if not it.get("error") or it.get("keyword_text") not in {k["keyword_text"] for k in keywords}]
    print(f"[DSO] 总关键词: {len(keywords)}, 已成功: {len(success_texts)}, 待抓: {len(pending)}", flush=True)

    if not pending:
        print("[DSO] 无待抓关键词, 退出")
        return

    from datetime import datetime
    start_ts = datetime.now().isoformat()

    with sync_playwright() as p:
        try:
            # 复用 MCP 登录态
            browser = p.chromium.launch_persistent_context(
                user_data_dir=PROFILE_DIR,
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            print(f"[DSO] 启动浏览器失败: {e}")
            sys.exit(1)

        try:
            page = browser.pages[0] if browser.pages else browser.new_page()
        except Exception:
            page = browser.new_page()

        page.set_default_timeout(15000)

        success = 0
        failed = 0
        rate_limited = 0

        for idx, kw in enumerate(pending):
            text = kw["keyword_text"]
            kw_id = kw["keyword_id"]
            url = f"https://dso.aidso.com/keyWordDetail/detail?keyword={text}"

            t0 = time.time()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                # 等月覆盖人次出现
                try:
                    page.wait_for_selector("text=月覆盖人次", timeout=8000)
                except Exception:
                    pass
                time.sleep(1.0)  # 额外等待下拉词渲染

                extracted = page.evaluate(EXTRACT_JS)

                if not extracted or "error" in extracted:
                    raise RuntimeError(f"extract failed: {extracted}")

                # 组装 item
                stats = extracted.get("stats", {})
                item = {
                    "keyword_id": kw_id,
                    "keyword_text": text,
                    "fetched_at": datetime.now().isoformat() + "Z",
                    "month_cover_count": parse_count(stats.get("月覆盖人次")),
                    "_month_cover_count_raw": stats.get("月覆盖人次"),
                    "week_avg_search": parse_count(stats.get("7日平均搜索")),
                    "down_keyword_count": parse_count(stats.get("下拉词数量")),
                    "down_keyword_month_covercount": parse_count(stats.get("下拉词月覆盖")),
                    "competition": stats.get("竞争度", "n/a"),
                    "city_level": stats.get("占比最大城市", "-"),
                    "tags_raw": stats.get("类型", "-"),
                    "down_words_top20": extracted.get("down_words", [])[:20],
                    "down_words_total": len(extracted.get("down_words", [])),
                    "error": None
                }

                # tags_raw 是 "-" 或标签组合, 暂存为字符串
                if item["tags_raw"] == "-":
                    item["tags"] = []
                else:
                    item["tags"] = [t.strip() for t in item["tags_raw"].split() if t.strip()]

                output["items"].append(item)
                success += 1
                elapsed = time.time() - t0
                print(f"[DSO] [{idx+1}/{len(pending)}] OK {text} ({elapsed:.1f}s)", flush=True)

            except Exception as e:
                err = str(e)[:200]
                # 检测登录态失效
                if "导航" in err or "登录" in err or "首页" in err:
                    rate_limited += 1
                item = {
                    "keyword_id": kw_id,
                    "keyword_text": text,
                    "fetched_at": datetime.now().isoformat() + "Z",
                    "month_cover_count": 0,
                    "week_avg_search": 0,
                    "down_keyword_count": 0,
                    "down_keyword_month_covercount": 0,
                    "competition": None,
                    "city_level": None,
                    "tags_raw": None,
                    "tags": [],
                    "down_words_top20": [],
                    "down_words_total": 0,
                    "error": err
                }
                output["items"].append(item)
                failed += 1
                elapsed = time.time() - t0
                print(f"[DSO] [{idx+1}/{len(pending)}] FAIL {text}: {err} ({elapsed:.1f}s)", flush=True)

            # 每 5 个词保存一次
            if (idx + 1) % 5 == 0 or idx == len(pending) - 1:
                save_output(output)
                print(f"[DSO] 进度: success={success}, failed={failed}, rate_limited={rate_limited}", flush=True)

            # 间隔 2-3 秒, 避免反爬
            time.sleep(2.5)

        save_output(output)
        print(f"\n[DSO] 完成. 成功={success}, 失败={failed}, rate_limited={rate_limited}", flush=True)
        browser.close()


if __name__ == "__main__":
    main()
