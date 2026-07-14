"""分析 HK储蓄未来 vs 全体账号分布，判断"超级黑马"判断是否成立、是否误判。

读 normalized/monitor-data.json，输出：
1. 该号在全体排名 + 关键指标百分位
2. 全体关键指标分布（中位/p90/p99/max）
3. 与 score Top10 其他号横向对比
4. 该号 day_scores 形态核查（"持续高位"是否属实）
5. 命中关键词明细 + today_rank / move
6. 误判疑点核查（重复命中、单日跳变、today_rank 真实性）
"""
from __future__ import annotations
import json
from statistics import median

DATA = "normalized/monitor-data.json"
TARGET = "HK储蓄未来"

d = json.load(open(DATA))
accts = d["accounts"]
window_days = d["window_days"]
print(f"窗口 {d['window_start']} ~ {d['window_end']}（{window_days}天），账号总数 {len(accts)}")
print("=" * 70)

# 定位目标
tgt = next((a for a in accts if a["name"] == TARGET), None)
if not tgt:
    print("未找到", TARGET); raise SystemExit
print(f"目标账号: {tgt['name']}  id={tgt['account_id']}")
print("=" * 70)

# ---- 1. 全体分布 + 该号百分位 ----
def percentile(sorted_vals, v):
    n = len(sorted_vals)
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_vals[mid] < v:
            lo = mid + 1
        else:
            hi = mid
    return round(100 * lo / n, 1)

metrics = ["score", "timeliness_score", "base_score", "kw_count", "article_count",
           "today_hit_count", "today_hit_ratio", "hit_days", "recent_hit_days",
           "longest_streak", "current_streak"]

print(f"{'指标':<22}{'该号':>10}{'中位':>10}{'p90':>10}{'p99':>10}{'max':>10}{'百分位':>9}")
print("-" * 81)
for m in metrics:
    vals = sorted(a.get(m, 0) or 0 for a in accts)
    v = tgt.get(m, 0) or 0
    n = len(vals)
    p90 = vals[int(0.9 * n)]
    p99 = vals[int(0.99 * n)]
    print(f"{m:<22}{v:>10}{median(vals):>10.2f}{p90:>10}{p99:>10}{vals[-1]:>10}{percentile(vals, v):>8.1f}%")

print("=" * 70)

# ---- 2. score Top10 横向对比 ----
top = sorted(accts, key=lambda a: -a["score"])[:10]
print("Score Top10:")
print(f"{'#':>2} {'name':<22}{'score':>7}{'tl':>7}{'base':>7}{'kw':>4}{'art':>4}{'today':>6}{'ratio':>6}{'streak':>7}{'best':>5}")
for i, a in enumerate(top, 1):
    mark = " ◀ 目标" if a["name"] == TARGET else ""
    print(f"{i:>2} {a['name'][:22]:<22}{a['score']:>7}{a['timeliness_score']:>7}{a['base_score']:>7}"
          f"{a['kw_count']:>4}{a['article_count']:>4}{a['today_hit_count']:>6}{a['today_hit_ratio']:>6}"
          f"{a['current_streak']:>7}{a['best_today'] or 0:>5}{mark}")
tgt_rank = next(i for i, a in enumerate(sorted(accts, key=lambda a: -a["score"]), 1) if a["name"] == TARGET)
print(f"目标 score 排名: {tgt_rank} / {len(accts)}")
print("=" * 70)

# ---- 3. day_scores 形态核查 ----
ds = tgt["day_scores"]
raw = tgt["raw_day_scores"]
print(f"day_scores(15天): {ds}")
print(f"raw_day_scores : {raw}")
print(f"最近5天 day_scores: {ds[-5:]}  raw: {raw[-5:]}")
print(f"最近5天均值={sum(ds[-5:])/5:.2f}  前5天均值={sum(ds[:5])/5:.2f}  中5天均值={sum(ds[5:10])/5:.2f}")
nz = [x for x in ds if x > 0]
print(f"非零天数={len(nz)}/15  最低={min(nz):.2f}  最高={max(nz):.2f}")
# 单日跳变
print("环比跳变(raw 环比):", [round(raw[i]-raw[i-1],2) if raw[i-1]>0 else None for i in range(1,15)])
print("=" * 70)

# ---- 4. 命中关键词明细 ----
kws = tgt["keywords"]
print(f"命中关键词 {len(kws)} 个，按 today_rank 升序:")
print(f"{'keyword':<28}{'today':>6}{'prev':>6}{'hit_days':>9}{'best':>5}{'art':>4}")
rows = []
for k, info in kws.items():
    rows.append((k, info.get("today_rank"), info.get("today_prev"),
                 info.get("hit_days"), info.get("best_rank"), len(info.get("articles", {}))))
for k, tr, tp, hd, br, ac in sorted(rows, key=lambda r: (r[1] is None, r[1] or 99)):
    tp_s = "-" if tp is None else str(tp)
    tr_s = "未命中" if tr is None else str(tr)
    print(f"{k[:28]:<28}{tr_s:>6}{tp_s:>6}{hd:>9}{br or 0:>5}{ac:>4}")

# today_rank 分布
tr_vals = [info.get("today_rank") for info in kws.values() if info.get("today_rank") is not None]
print(f"今天有命中的关键词: {len(tr_vals)}/{len(kws)}，today_rank 分布: {sorted(tr_vals)}")
print("=" * 70)

# 每天命中关键词数 vs 每天最好rank —— 区分"命中面扩大"还是"rank提升"
daily_hit_kw = [0] * window_days
daily_best = [0] * window_days
for k, info in kws.items():
    h = info.get("history", [])
    for i, r in enumerate(h):
        if r and r > 0:
            daily_hit_kw[i] += 1
            if daily_best[i] == 0 or r < daily_best[i]:
                daily_best[i] = r
print("每天命中关键词数:", daily_hit_kw)
print("每天最好rank    :", daily_best)
print("  → 命中面(词数)若随时间膨胀，则分数上涨含'抓取面变广'成分；若词数稳定而rank改善，则真变强")
# 对比：该号命中词 hit_days 分布，看新近词占比
hd = sorted(info.get("hit_days", 0) for info in kws.values())
print(f"命中词 hit_days 分布: {hd}")
print(f"  hit_days<=3 的词(近3天内才命中): {sum(1 for x in hd if x<=3)} 个  hit_days>=12 的词(老牌稳定): {sum(1 for x in hd if x>=12)} 个")
print("=" * 70)

# ---- 5. 误判疑点 ----
print("【误判疑点核查】")
# 5a article vs kw：一篇文章命中多词
print(f"5a. kw_count={tgt['kw_count']} article_count={tgt['article_count']} → 平均每篇命中 {tgt['kw_count']/max(tgt['article_count'],1):.2f} 词")
# 5b today_hit_ratio 口径
print(f"5b. today_hit_count={tgt['today_hit_count']} article_count={tgt['article_count']} ratio={tgt['today_hit_ratio']} (={tgt['today_hit_count']}/{tgt['article_count']}={round(tgt['today_hit_count']/max(tgt['article_count'],1),3)})")
# 5c move_summary
ms = tgt["move_summary"]
print(f"5c. move: new={ms['new_count']} up={ms['up_count']} down={ms['down_count']} flat={ms['flat_count']}  primary={ms['primary_type']}/{ms['primary_count']} sec={ms['secondary_type']}/{ms['secondary_count']}")
# 5d best_today vs rank稳定
print(f"5d. best_today={tgt['best_today']}  history={tgt['history']}  → 今天最好排名 {tgt['best_today']}，'rank稳定1-2' 与今天 rank={tgt['best_today']} 是否一致")
# 5e continuity 放大倍数
print(f"5e. base_score={tgt['base_score']} × continuity={tgt['continuity_multiplier']} + topic_bonus={tgt['topic_breadth_bonus']} + bucket_bonus={tgt['bucket_breadth_bonus']} = score={tgt['score']}")
print(f"    实算: {round(tgt['base_score']*tgt['continuity_multiplier']+tgt['topic_breadth_bonus']+tgt['bucket_breadth_bonus'],2)} (应等于score)")
