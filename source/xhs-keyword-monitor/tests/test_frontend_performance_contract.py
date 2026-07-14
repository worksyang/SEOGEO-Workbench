"""前端性能契约测试。

验证：
1. 首屏使用 BOOTSTRAP_URL 而非全量 DATA_URL
2. keyword/account 详情按需 fetch + Map 缓存
3. 评分 tooltip 懒构建（无 data-score-tooltip-html 预生成）
4. 博主列表首屏最多 100 条 + 加载更多
5. 无高频 mousemove elementFromPoint
6. ACCOUNT_BY_ID / KEYWORD_BY_ID 索引映射
7. turnover 缓存
8. CSS contain / content-visibility / load-more 样式
9. window.__XHS_PERF__ 埋点
"""
from __future__ import annotations

import os
import unittest


JS_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "static", "js", "monitor.js")
CSS_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "static", "css", "monitor.css")
API_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "web", "api.py")


class TestBootstrapEndpoint(unittest.TestCase):
    """契约 1: 首屏改 fetch bootstrap，不再请求 99MB /api/monitor-data"""

    def test_bootstrap_url_constant_defined(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("BOOTSTRAP_URL", js, "JS 应定义 BOOTSTRAP_URL 常量")
        self.assertIn("/api/monitor-data/bootstrap", js, "BOOTSTRAP_URL 应指向 /api/monitor-data/bootstrap")

    def test_loadData_uses_bootstrap(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("fetch(BOOTSTRAP_URL", js, "loadData 应 fetch BOOTSTRAP_URL 而非 DATA_URL")

    def test_api_has_bootstrap_route(self):
        with open(API_PATH) as f:
            api = f.read()
        self.assertIn("monitor-data/bootstrap", api, "bootstrap API 路由应存在")
        self.assertIn("monitor_data_bootstrap", api, "bootstrap 视图函数应存在")
        self.assertIn("get_bootstrap", api, "bootstrap 应通过 get_bootstrap 返回摘要数据（FastStore）")
        self.assertIn("get_bootstrap", api, "bootstrap 应通过 get_bootstrap 返回 account 摘要（FastStore）")

    def test_api_has_keyword_detail_route(self):
        with open(API_PATH) as f:
            api = f.read()
        self.assertIn("monitor-data/keyword/<keyword_id>", api, "关键词详情 API 路由应存在")

    def test_api_has_account_detail_route(self):
        with open(API_PATH) as f:
            api = f.read()
        self.assertIn("monitor-data/account/<account_id>", api, "博主详情 API 路由应存在")


class TestDetailLazyFetch(unittest.TestCase):
    """契约 2: keyword/account 详情按需 fetch + Map 缓存"""

    def test_keyword_detail_cache_exists(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("KEYWORD_DETAIL_CACHE", js, "关键词详情缓存 Map 应存在")
        self.assertIn("KEYWORD_DETAIL_PENDING", js, "关键词详情请求去重 Map 应存在")

    def test_account_detail_cache_exists(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("ACCOUNT_DETAIL_CACHE", js, "博主详情缓存 Map 应存在")
        self.assertIn("ACCOUNT_DETAIL_PENDING", js, "博主详情请求去重 Map 应存在")

    def test_fetch_functions_exist(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("_fetchKeywordDetail", js, "关键词详情 fetch 函数应存在")
        self.assertIn("_fetchAccountDetail", js, "博主详情 fetch 函数应存在")


class TestLazyScoreTooltip(unittest.TestCase):
    """契约 3: 评分 tooltip 彻底取消 data-score-tooltip-html 预生成"""

    def test_no_data_score_tooltip_html(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertNotIn("data-score-tooltip-html", js, "JS 不应包含 data-score-tooltip-html 属性（全部懒构建）")

    def test_lazy_builder_exists(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("lazyBuildScoreTooltipHtml", js, "懒构建函数应存在")
        self.assertIn("ACCOUNT_DETAIL_CACHE.get", js, "懒构建应从 ACCOUNT_DETAIL_CACHE 获取数据")

    def test_tooltip_loading_state(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("score-tip-loading", js, "tooltip 应有加载态样式类")
        self.assertIn("加载评分详情", js, "tooltip 应有加载态文案")

    def test_tooltip_error_handling(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("评分暂不可用", js, "tooltip 应有加载失败文案")
        self.assertIn(".catch(", js, "fetch 失败应有 catch 处理")


class TestAccountPaginated(unittest.TestCase):
    """契约 4: 博主列表分页行为"""

    def test_account_page_size_constant(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("ACCOUNT_PAGE_SIZE", js, "分页常量应存在")
        self.assertIn("100", js, "ACCOUNT_PAGE_SIZE 应为 100")

    def test_account_page_variable(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("_accountPage", js, "分页变量应存在")

    def test_account_list_slice(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("list.slice(0, _accountPage * ACCOUNT_PAGE_SIZE)", js,
                       "博主列表渲染前应 slice 分页")

    def test_load_more_function(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("loadMoreAccounts", js, "加载更多函数应存在")

    def test_page_not_reset_on_load_more(self):
        """loadMore 调用 renderList 时不应重置页码"""
        with open(JS_PATH) as f:
            js = f.read()
        # 找到 renderList 函数体
        rl_start = js.find("function renderList()")
        self.assertGreater(rl_start, 0, "renderList 函数应存在")
        rl_body = js[rl_start:rl_start + 3000]
        # 验证有状态追踪逻辑
        self.assertIn("renderList._lastFilter", rl_body, "renderList 应保存上次 filter 状态")
        self.assertIn("renderList._lastSortMode", rl_body, "renderList 应保存上次 sortMode 状态")
        self.assertIn("renderList._lastMode", rl_body, "renderList 应保存上次 mode 状态")
        # 验证 loadMore 后不重置
        self.assertIn("filterChanged || sortModeChanged", rl_body,
                      "只有 filter/sortMode/mode 变化时才重置页码")

    def test_page_reset_on_filter_sort(self):
        with open(JS_PATH) as f:
            js = f.read()
        # 验证 renderList 中有条件重置
        rl_start = js.find("function renderList()")
        rl_body = js[rl_start:rl_start + 3000]
        self.assertIn("_accountPage = 1", rl_body, "筛选/排序变化应重置分页")

    def test_load_more_total_uses_filtered_list(self):
        """加载更多按钮的 total 应基于筛选后的 list.length"""
        with open(JS_PATH) as f:
            js = f.read()
        # 按钮模板使用 ${total} 且 total 来自 accountListLen（即 list.length）
        if "const accountListLen" not in js:
            self.fail("accountListLen 变量未定义")
        acct_idx = js.find("const accountListLen")
        end_idx = js.find("acctList'.innerHTML", acct_idx)
        if end_idx < 0:
            end_idx = js.find('acctList', acct_idx) + 200
        load_more_region = js[acct_idx:end_idx + 100]
        self.assertIn("accountListLen", load_more_region, "加载更多按钮的 total 应来自 accountListLen（list.length）")
        self.assertNotIn("ALL_ACCOUNTS.length", load_more_region, "加载更多按钮不应引用 ALL_ACCOUNTS.length")


class TestNoMousemoveElementFromPoint(unittest.TestCase):
    """契约 5: 去除 mousemove 高频 elementFromPoint 路径"""

    def test_no_mousemove_handler(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("mouseover", js, "应有 mouseover 事件委托")
        # 只允许 chart 图表的 onmousemove HTML 属性，不允许 JS addEventListener('mousemove')
        mousemove_js = js.count("addEventListener('mousemove'")
        self.assertEqual(mousemove_js, 0, f"不应有 JS addEventListener mousemove，当前 {mousemove_js} 处")

    def test_has_event_delegation(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("event.target.closest", js, "应有事件委托（event.target.closest）")
        self.assertIn(".js-score-tooltip", js, "应有 .js-score-tooltip 选择器")


class TestIndexMaps(unittest.TestCase):
    """契约 6: ACCOUNT_BY_ID / KEYWORD_BY_ID 索引映射"""

    def test_account_by_id_map(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("ACCOUNT_BY_ID", js, "ACCOUNT_BY_ID Map 应存在")
        self.assertIn("ACCOUNT_BY_ID.get", js, "应有 ACCOUNT_BY_ID.get 调用")

    def test_keyword_by_id_map(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("KEYWORD_BY_ID", js, "KEYWORD_BY_ID Map 应存在")
        self.assertIn("KEYWORD_BY_ID.get", js, "应有 KEYWORD_BY_ID.get 调用")

    def test_rebuild_index_function(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("_rebuildIndexMaps", js, "索引重建函数应存在")


class TestTurnoverCache(unittest.TestCase):
    """契约 7: turnover 缓存只算一次"""

    def test_turnover_cache_exists(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("TURNOVER_CACHE", js, "Turnover 缓存 Map 应存在")


class TestCSSPerformance(unittest.TestCase):
    """契约 8: CSS contain / content-visibility / load-more"""

    def test_css_contain_on_acct_list(self):
        with open(CSS_PATH) as f:
            css = f.read()
        self.assertIn("contain:size layout style", css,
                       "acct-list 应有 contain:size layout style")
        self.assertIn("content-visibility:auto", css,
                       "acct-list 应有 content-visibility:auto")

    def test_css_contain_on_acct_row(self):
        with open(CSS_PATH) as f:
            css = f.read()
        self.assertIn("contain:layout style", css,
                       "acct-row 应有 contain:layout style（不含 size 以免裁切 tooltip）")

    def test_css_load_more_styles(self):
        with open(CSS_PATH) as f:
            css = f.read()
        self.assertIn(".load-more-row", css, "加载更多行样式应存在")
        self.assertIn(".load-more-btn", css, "加载更多按钮样式应存在")


class TestPerfMetrics(unittest.TestCase):
    """契约 9: window.__XHS_PERF__ 性能埋点"""

    def test_perf_global_exists(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("window.__XHS_PERF__", js, "性能埋点全局对象应存在")

    def test_bootstrap_timing(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("__XHS_PERF__.bootstrap.fetch", js, "bootstrap fetch 耗时埋点应存在")
        self.assertIn("__XHS_PERF__.bootstrap.parse", js, "bootstrap parse 耗时埋点应存在")
        self.assertIn("__XHS_PERF__.bootstrap", js, "bootstrap 埋点应存在")

    def test_render_timing(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("__XHS_PERF__.render.start", js, "首屏渲染 start 埋点应存在")
        self.assertIn("__XHS_PERF__.render.end", js, "首屏渲染 end 埋点应存在")

    def test_detail_timing(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("__XHS_PERF__.detail.fetch", js, "详情 fetch 耗时埋点应存在")


# ── 附：兼容性检查 ──────────────────────────────────────
class TestCompatCheck(unittest.TestCase):
    """确保改动不破坏原有功能"""

    def test_keyword_detail_renders(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("function renderKeywordDetail(kw)", js, "renderKeywordDetail 应保留")
        self.assertIn("function renderAccountDetail(name)", js, "renderAccountDetail 应保留")

    def test_keyword_manage_exists(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("function renderKeywordManageView", js, "关键词管理渲染函数应保留")

    def test_refresh_all_exists(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("/api/refresh-all/status", js, "刷新全部状态 API 引用应保留")
        self.assertIn("startKeywordRefresh", js, "单词刷新函数应保留")

    def test_three_board_sorting(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("setAccountSortMode", js, "三榜排序函数应保留")
        self.assertIn("accountSortMode", js, "accountSortMode 变量应保留")

    def test_article_list_compat(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("renderAccountArticleRow", js, "笔记列表渲染函数应保留")
        self.assertIn("buildAccountArticleFeed", js, "笔记列表构建函数应保留")

    def test_score_dashboard_compat(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("function accountScoreTooltipHtml", js, "评分 tooltip HTML 构建函数应保留")
        self.assertIn("function scoreRadarSvg", js, "六边形雷达图渲染函数应保留")
        self.assertIn("function scoreAxisCards", js, "评分轴卡构建函数应保留")

    def test_detail_drawer_compat(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("function openArtByUrl", js, "笔记抽屉打开函数应保留")
        self.assertIn("function closeDrawer", js, "笔记抽屉关闭函数应保留")

class TestBootstrapHistoryFields(unittest.TestCase):
    """契约 10: bootstrap 中 history_best/history_hits 存在且为数组"""

    FAST_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "services", "monitor_fast_service.py")

    def test_history_best_in_bootstrap_keyword(self):
        with open(self.FAST_PATH) as f:
            fast = f.read()
        self.assertIn("history_best", fast, "BOOTSTRAP_KEYWORD_FIELDS 应包含 history_best")

    def test_history_hits_in_bootstrap_keyword(self):
        with open(self.FAST_PATH) as f:
            fast = f.read()
        self.assertIn("history_hits", fast, "BOOTSTRAP_KEYWORD_FIELDS 应包含 history_hits")


class TestBuildHeatRowFallback(unittest.TestCase):
    """契约 11: buildHeatRow 对空/undefined history 安全 fallback"""

    def test_build_heat_row_fallback(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("!Array.isArray(history)", js, "buildHeatRow 应检测非数组 history")
        self.assertIn("return ''", js, "非数组时应返回空字符串")

    def test_get_keyword_runs_fallback(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("Array.isArray(k.runs)", js, "getKeywordRuns 应检测 runs 是否为数组")


class TestKeywordManageNoDoubleFetch(unittest.TestCase):
    """契约 12: loadGroups 复用 KM_DATA 避免重复请求"""

    def test_loadGroups_reuses_KM_DATA(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("KM_DATA", js, "KM_DATA 全局变量应存在")
        # loadGroups 应检查 KM_DATA 而非直接 fetch
        # 查找 loadGroups 函数体中的 KM_DATA 引用
        loadGroups_start = js.find("async function loadGroups")
        self.assertGreater(loadGroups_start, 0, "loadGroups 函数应存在")
        loadGroups_body = js[loadGroups_start:loadGroups_start + 2000]
        self.assertIn("typeof KM_DATA", loadGroups_body, "loadGroups 应检查 KM_DATA 是否已加载")


class TestDetailLazyFetchStaleGuard(unittest.TestCase):
    """契约 13: detail lazy fetch 有 stale guard 和 loading 状态"""

    def test_keyword_detail_loading_state(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("正在加载关键词详情", js, "keyword detail 应有加载态文案")

    def test_account_detail_loading_state(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("正在加载博主详情", js, "account detail 应有加载态文案")

    def test_keyword_detail_stale_guard(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("curKeyword !== _kw", js, "keyword detail 应有 stale guard")

    def test_account_detail_stale_guard(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("curAccount !== _name", js, "account detail 应有 stale guard")

    def test_keyword_detail_error_state(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("加载失败", js, "keyword detail 应有错误态文案")

    def test_account_detail_error_state(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("加载失败", js, "account detail 应有错误态文案")


class TestTooltipNoSummaryBuild(unittest.TestCase):
    """契约 14: tooltip 不从摘要构建正式 HTML"""

    def test_tooltip_no_summary_html(self):
        with open(JS_PATH) as f:
            js = f.read()
        # lazyBuildScoreTooltipHtml 不应在只有 summary 时返回 accountScoreTooltipHtml
        # 应返回 null 触发 fetch
        self.assertIn("return null", js.split("lazyBuildScoreTooltipHtml")[1].split("function")[0] 
                      if "lazyBuildScoreTooltipHtml" in js else "",
                      "lazyBuildScoreTooltipHtml 应返回 null 触发 fetch")


class TestLoadMoreButton(unittest.TestCase):
    """契约 15: 加载更多按钮模板存在"""

    def test_load_more_button_template(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("load-more-btn", js, "加载更多按钮 class 应存在")
        self.assertIn("loadMoreAccounts()", js, "加载更多按钮 onclick 应调用 loadMoreAccounts")
        # 验证模板包含计数（格式：已展示 X/Y 或 (X/Y)）
        self.assertIn("displayed", js, "加载更多按钮应显示 displayed 变量")
        self.assertIn("total", js, "加载更多按钮应显示 total 变量")
        # 验证按钮模板在 renderList 中，不在其他地方
        btn_template = js[js.find("load-more-btn"):js.find("load-more-btn") + 400] if "load-more-btn" in js else ""
        self.assertIn("${displayed}", btn_template, "按钮模板应使用 ${displayed}")
        self.assertIn("${total}", btn_template, "按钮模板应使用 ${total}")


class TestAccountByNameIndex(unittest.TestCase):
    """契约 16: ACCOUNT_BY_NAME / KEYWORD_BY_NAME 索引（结构性验证）"""

    def test_account_by_name_declared(self):
        with open(JS_PATH) as f:
            js = f.read()
        # 验证声明: let 语句
        self.assertIn("let ACCOUNT_BY_NAME = new Map()", js, "ACCOUNT_BY_NAME 必须声明为 let")

    def test_keyword_by_name_declared(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("let KEYWORD_BY_NAME = new Map()", js, "KEYWORD_BY_NAME 必须声明为 let")

    def test_account_by_name_filled(self):
        with open(JS_PATH) as f:
            js = f.read()
        # 验证 _rebuildIndexMaps 中填充 name
        rebuild_start = js.find("function _rebuildIndexMaps(")
        self.assertGreater(rebuild_start, 0, "_rebuildIndexMaps 函数应存在")
        rebuild_body = js[rebuild_start:rebuild_start + 800]
        self.assertIn('ACCOUNT_BY_NAME.set(acct.name, acct)', rebuild_body,
                      "_rebuildIndexMaps 应填充 ACCOUNT_BY_NAME")

    def test_keyword_by_name_filled(self):
        with open(JS_PATH) as f:
            js = f.read()
        rebuild_start = js.find("function _rebuildIndexMaps(")
        self.assertGreater(rebuild_start, 0)
        rebuild_body = js[rebuild_start:rebuild_start + 800]
        self.assertIn('KEYWORD_BY_NAME.set(kw.keyword, kw)', rebuild_body,
                      "_rebuildIndexMaps 应填充 KEYWORD_BY_NAME")

    def test_account_by_name_used(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("ACCOUNT_BY_NAME.get(name)", js,
                      "renderAccountDetail 应使用 ACCOUNT_BY_NAME.get(name)")

    def test_keyword_by_name_used(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("KEYWORD_BY_NAME.get(kw)", js,
                      "renderKeywordDetail 应使用 KEYWORD_BY_NAME.get(kw)")


class TestSnapshotWaterfallContract(unittest.TestCase):
    """契约 17: 当前快照采用小红书式 3:4 瀑布流，同时保留完整榜单信息。"""

    def test_waterfall_card_structure(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn('class="snapshot-waterfall"', js, "当前快照应使用瀑布流容器")
        self.assertIn('class="xhs-note-card"', js, "每篇笔记应使用独立卡片")
        self.assertIn('class="xhs-note-cover"', js, "卡片应包含竖版封面区")
        self.assertIn("buildArticleCoverHtml(art)", js, "瀑布流应复用现有封面加载与兜底机制")

    def test_waterfall_keeps_ranking_and_metadata(self):
        with open(JS_PATH) as f:
            js = f.read()
        for marker in (
            "xhs-note-rank",
            "xhs-note-hit",
            "xhs-note-published",
            "xhs-note-engagement",
            "xhs-note-action",
            "liked_count",
            "collected_count",
            "comment_count",
            "shared_count",
        ):
            self.assertIn(marker, js, f"瀑布流缺少榜单字段或互动指标：{marker}")

    def test_waterfall_keeps_account_chip_interaction(self):
        with open(JS_PATH) as f:
            js = f.read()
        self.assertIn("snapshot-account-chip", js, "应保留现有蓝底博主标签")
        self.assertIn("event.stopPropagation();selectAccount", js,
                      "点击博主标签应阻止打开笔记并切换到博主详情")

    def test_waterfall_css_is_three_by_four_and_responsive(self):
        with open(CSS_PATH) as f:
            css = f.read()
        self.assertIn(".snapshot-waterfall", css, "应有瀑布流布局样式")
        self.assertIn("grid-template-columns:repeat(auto-fill,minmax(165px,1fr))", css,
                      "桌面详情栏应按自适应多列展示")
        self.assertIn("aspect-ratio:3 / 4", css, "封面比例应为 3:4 竖图")
        self.assertIn("@container detail-column (max-width:480px)", css,
                      "窄详情栏应有响应式布局")


if __name__ == "__main__":
    unittest.main()
