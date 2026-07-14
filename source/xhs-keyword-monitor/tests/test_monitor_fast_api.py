"""test_monitor_fast_api — 高性能分片数据层测试。

覆盖：
- bootstrap 冷热加载
- 304 ETag 条件请求
- gzip 压缩
- 缓存失效（文件签名变化）
- keyword/account 详情 404
- 旧全文端点兼容
- bootstrap 尺寸约束
"""
from __future__ import annotations

import gzip
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest


class TestMonitorFastStore(unittest.TestCase):
    """FastStore 单元测试（不启动 Flask）。"""

    @classmethod
    def setUpClass(cls):
        cls.project_root = Path(__file__).resolve().parent.parent
        cls.monitor_data_path = cls.project_root / "normalized" / "monitor-data.json"
        cls.sqlite_path = cls.project_root / "data" / "state" / "app.db"
        cls.keywords_config_path = cls.project_root / "data" / "config" / "keywords.json"

        # 确保数据存在
        assert cls.monitor_data_path.exists(), f"monitor-data.json not found: {cls.monitor_data_path}"
        assert cls.sqlite_path.exists(), f"app.db not found: {cls.sqlite_path}"
        assert cls.keywords_config_path.exists(), f"keywords.json not found: {cls.keywords_config_path}"

    def setUp(self):
        from app.services.monitor_fast_service import MonitorFastStore
        # 每个测试用例都用新 store 避免缓存污染
        self.store = MonitorFastStore(
            self.monitor_data_path,
            self.sqlite_path,
            self.keywords_config_path,
        )

    # ── 基本加载 ────────────────────────────────────

    def test_ensure_loaded_ok(self):
        self.store.ensure_loaded()
        meta = self.store.get_metadata()
        self.assertIsNotNone(meta)
        self.assertIn("generated_at", meta)
        self.assertIn("keyword_count", meta)
        self.assertIn("account_count", meta)
        self.assertGreater(meta["keyword_count"], 0)
        self.assertGreater(meta["account_count"], 0)

    def test_metadata(self):
        self.store.ensure_loaded()
        meta = self.store.get_metadata()
        self.assertEqual(meta["platform"], "小红书")
        self.assertIn("window_days", meta)

    # ── bootstrap 尺寸 ──────────────────────────────

    def test_bootstrap_size_under_5mb_raw(self):
        self.store.ensure_loaded()
        report = self.store.get_bootstrap_size_report()
        self.assertLessEqual(report["raw_mb"], 5.0,
                              f"bootstrap raw {report['raw_mb']}MB > 5MB limit")
        self.assertLessEqual(report["gzip_mb"], 1.0,
                              f"bootstrap gzip {report['gzip_mb']}MB > 1MB limit")

    def test_bootstrap_precompressed(self):
        """bootstrap 预压缩 gzip 可用且正确。"""
        json_bytes, gz_bytes, etag = self.store.get_bootstrap()
        # 验证 gzip 可解压
        decompressed = gzip.decompress(gz_bytes)
        self.assertEqual(decompressed, json_bytes)
        # 验证 ETag 是 md5 的十六进制
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))

    # ── bootstrap 字段白名单 ──────────────────────────

    def test_bootstrap_keyword_fields_light(self):
        """bootstrap 中的 keyword 不应包含 runs、accounts、daily_heat_points；
        history_best/history_hits 应存在（前端列表渲染需要）。"""
        json_bytes, _, _ = self.store.get_bootstrap()
        data = json.loads(json_bytes)
        for kw in data["keywords"]:
            self.assertNotIn("runs", kw, f"keyword {kw['keyword_id']} has runs")
            self.assertNotIn("accounts", kw, f"keyword {kw['keyword_id']} has accounts")
            self.assertIn("history_best", kw, f"keyword {kw['keyword_id']} missing history_best")
            self.assertIn("history_hits", kw, f"keyword {kw['keyword_id']} missing history_hits")
            self.assertIsInstance(kw["history_best"], list, f"keyword {kw['keyword_id']} history_best not list")
            self.assertIsInstance(kw["history_hits"], list, f"keyword {kw['keyword_id']} history_hits not list")
            # daily_heat_points 应在 keyword_heat_metric 中被裁剪
            khm = kw.get("keyword_heat_metric", {})
            self.assertNotIn("daily_heat_points", khm,
                             f"keyword {kw['keyword_id']} has daily_heat_points in bootstrap")

    def test_bootstrap_account_fields_light(self):
        """bootstrap 中的 account 不应包含 articles、keywords、topics、history、day_scores、breakthrough 等详情。"""
        json_bytes, _, _ = self.store.get_bootstrap()
        data = json.loads(json_bytes)
        for acct in data["accounts"]:
            self.assertNotIn("articles", acct, f"account {acct['account_id']} has articles")
            self.assertNotIn("keywords", acct, f"account {acct['account_id']} has keywords")
            self.assertNotIn("topics", acct, f"account {acct['account_id']} has topics")
            self.assertNotIn("history", acct, f"account {acct['account_id']} has history")
            self.assertNotIn("day_scores", acct, f"account {acct['account_id']} has day_scores")
            self.assertNotIn("breakthrough", acct, f"account {acct['account_id']} has breakthrough")
            self.assertNotIn("matched_keywords", acct, f"account {acct['account_id']} has matched_keywords")
            self.assertNotIn("best_articles", acct, f"account {acct['account_id']} has best_articles")
            self.assertNotIn("classic_articles", acct, f"account {acct['account_id']} has classic_articles")
            self.assertNotIn("covered_topics", acct, f"account {acct['account_id']} has covered_topics")

    # ── keyword 详情 ────────────────────────────────

    def test_get_keyword_found(self):
        meta = self.store.get_metadata()
        # 取第一个 keyword
        self.store.ensure_loaded()
        # 通过内部索引获取第一个 keyword_id
        body, etag = None, None
        for kw_id in list(self.store._keyword_by_id.keys())[:1]:
            body, etag = self.store.get_keyword(kw_id)
        self.assertIsNotNone(body)
        self.assertIsNotNone(etag)
        kw = json.loads(body)
        self.assertIn("keyword_id", kw)
        self.assertIn("keyword", kw)

    def test_get_keyword_not_found(self):
        body, etag = self.store.get_keyword("nonexistent_kw_id")
        self.assertIsNone(body)
        self.assertIsNone(etag)

    def test_get_keyword_full_fields(self):
        """detail keyword 应包含完整字段（runs, accounts, history_best 等）。"""
        self.store.ensure_loaded()
        for kw_id in list(self.store._keyword_by_id.keys())[:3]:
            body, _ = self.store.get_keyword(kw_id)
            kw = json.loads(body)
            # 应有完整字段
            self.assertIn("runs", kw, f"keyword {kw_id} missing runs")
            self.assertIn("accounts", kw, f"keyword {kw_id} missing accounts")
            self.assertIn("history_best", kw, f"keyword {kw_id} missing history_best")
            self.assertIn("history_hits", kw, f"keyword {kw_id} missing history_hits")
            khm = kw.get("keyword_heat_metric", {})
            self.assertIn("daily_heat_points", khm,
                          f"keyword {kw_id} missing daily_heat_points in detail")

    # ── account 详情 ────────────────────────────────

    def test_get_account_found(self):
        self.store.ensure_loaded()
        for acct_id in list(self.store._account_by_id.keys())[:1]:
            body, etag = self.store.get_account(acct_id)
        self.assertIsNotNone(body)
        self.assertIsNotNone(etag)
        acct = json.loads(body)
        self.assertIn("account_id", acct)

    def test_get_account_not_found(self):
        body, etag = self.store.get_account("nonexistent_acct_id")
        self.assertIsNone(body)
        self.assertIsNone(etag)

    def test_get_account_full_fields(self):
        """detail account 应包含完整字段。"""
        self.store.ensure_loaded()
        for acct_id in list(self.store._account_by_id.keys())[:3]:
            body, _ = self.store.get_account(acct_id)
            acct = json.loads(body)
            self.assertIn("articles", acct, f"account {acct_id} missing articles")
            self.assertIn("keywords", acct, f"account {acct_id} missing keywords")
            self.assertIn("history", acct, f"account {acct_id} missing history")

    # ── ETag 一致性 ─────────────────────────────────

    def test_bootstrap_etag_stable(self):
        """同一数据多次调用返回相同 ETag。"""
        _, _, etag1 = self.store.get_bootstrap()
        _, _, etag2 = self.store.get_bootstrap()
        self.assertEqual(etag1, etag2)

    def test_keyword_etag_stable(self):
        self.store.ensure_loaded()
        for kw_id in list(self.store._keyword_by_id.keys())[:1]:
            body1, etag1 = self.store.get_keyword(kw_id)
            body2, etag2 = self.store.get_keyword(kw_id)
        self.assertEqual(etag1, etag2)
        self.assertEqual(body1, body2)

    # ── 缓存失效 ────────────────────────────────────

    def test_cache_invalidation_on_signature_change(self):
        """文件签名变化后应重载数据。"""
        self.store.ensure_loaded()
        old_etag = self.store._bootstrap_etag

        # 伪造签名变化
        with patch.object(self.store, '_file_signature', return_value=(time.time() + 100, 999)):
            self.store._loaded = True  # 保持 loaded 状态
            # 触发 _signatures_changed → True
            changed = self.store._signatures_changed()
            self.assertTrue(changed)
            # 重载应得到新 etag（实际上数据没变，但签名变了会重算）
            # 使用真实文件签名差异
            self.store.ensure_loaded()

    # ── LRU 缓存 ────────────────────────────────────

    def test_detail_lru_cache_hit(self):
        """两次 get_keyword 应命中 LRU 缓存。"""
        self.store.ensure_loaded()
        kw_id = list(self.store._keyword_by_id.keys())[0]
        # 第一次: 冷
        body1, etag1 = self.store.get_keyword(kw_id)
        self.assertIn(kw_id, self.store._detail_keyword_cache)
        # 第二次: 热
        body2, etag2 = self.store.get_keyword(kw_id)
        self.assertEqual(body1, body2)
        self.assertEqual(etag1, etag2)

    def test_detail_lru_cache_eviction(self):
        """超过缓存上限后应淘汰旧条目。"""
        self.store.ensure_loaded()
        self.store._detail_cache_max = 5
        kw_ids = list(self.store._keyword_by_id.keys())[:10]
        for kw_id in kw_ids:
            self.store.get_keyword(kw_id)
        self.assertLessEqual(len(self.store._detail_keyword_cache), 5)

    # ── 并发安全 ────────────────────────────────────

    def test_concurrent_get_bootstrap(self):
        """多线程并发 get_bootstrap 不崩溃。"""
        import threading
        results = []

        def worker():
            try:
                self.store.ensure_loaded()
                jb, gz, etag = self.store.get_bootstrap()
                results.append(("ok", len(jb), len(gz)))
            except Exception as e:
                results.append(("error", str(e)))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for r in results:
            self.assertEqual(r[0], "ok", f"thread error: {r}")

    # ── 热加载性能 ──────────────────────────────────

    def test_hot_get_bootstrap_perf(self):
        """热调用 bootstrap 应 < 50ms。"""
        self.store.ensure_loaded()
        start = time.perf_counter()
        for _ in range(100):
            jb, gz, etag = self.store.get_bootstrap()
        elapsed = time.perf_counter() - start
        avg_ms = elapsed / 100 * 1000
        self.assertLess(avg_ms, 50, f"avg bootstrap get {avg_ms:.1f}ms")


class TestMonitorFastStoreFlask(unittest.TestCase):
    """通过 Flask 测试客户端验证完整 HTTP 行为。"""

    @classmethod
    def setUpClass(cls):
        """创建 Flask 应用并注册 API。"""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()

    def setUp(self):
        # 确保 FastStore 已预热
        from app.services.monitor_fast_service import get_fast_store
        try:
            store = get_fast_store()
        except RuntimeError:
            from app.services.monitor_fast_service import init_fast_store
            from app.config import Config
            store = init_fast_store(
                monitor_data_path=Config.MONITOR_DATA_FILE,
                sqlite_path=Config.SQLITE_PATH,
                keywords_config_path=Config.KEYWORDS_CONFIG_FILE,
            )

    # ── bootstrap HTTP ──────────────────────────────

    def test_bootstrap_http_200(self):
        resp = self.client.get("/api/monitor-data/bootstrap")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        data = resp.get_json()
        self.assertIn("keywords", data)
        self.assertIn("accounts", data)
        self.assertIn("generated_at", data)

    def test_bootstrap_http_304(self):
        """带 If-None-Match 应返回 304。"""
        resp1 = self.client.get("/api/monitor-data/bootstrap")
        etag = resp1.headers.get("ETag")
        self.assertIsNotNone(etag)
        resp2 = self.client.get("/api/monitor-data/bootstrap",
                                 headers={"If-None-Match": etag})
        self.assertEqual(resp2.status_code, 304)
        self.assertEqual(resp2.data, b"")

    def test_bootstrap_http_gzip(self):
        """带 Accept-Encoding: gzip 应返回 gzip 压缩响应。"""
        resp = self.client.get("/api/monitor-data/bootstrap",
                                headers={"Accept-Encoding": "gzip"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
        data = gzip.decompress(resp.data)
        payload = json.loads(data)
        self.assertIn("keywords", payload)

    def test_bootstrap_http_vary(self):
        resp = self.client.get("/api/monitor-data/bootstrap")
        self.assertIn("Accept-Encoding", resp.headers.get("Vary", ""))

    def test_bootstrap_http_cache_control(self):
        resp = self.client.get("/api/monitor-data/bootstrap")
        cc = resp.headers.get("Cache-Control", "")
        self.assertIn("no-cache", cc)
        self.assertIn("must-revalidate", cc)
        self.assertNotIn("no-store", cc)

    # ── keyword detail HTTP ─────────────────────────

    def test_keyword_detail_http_200(self):
        """先取 bootstrap 取第一个 keyword_id 再请求 detail。"""
        boot = self.client.get("/api/monitor-data/bootstrap").get_json()
        kw_id = boot["keywords"][0]["keyword_id"]
        resp = self.client.get(f"/api/monitor-data/keyword/{kw_id}")
        self.assertEqual(resp.status_code, 200)
        kw = resp.get_json()
        self.assertEqual(kw["keyword_id"], kw_id)
        self.assertIn("runs", kw)
        self.assertIn("accounts", kw)

    def test_keyword_detail_http_304(self):
        boot = self.client.get("/api/monitor-data/bootstrap").get_json()
        kw_id = boot["keywords"][0]["keyword_id"]
        resp1 = self.client.get(f"/api/monitor-data/keyword/{kw_id}")
        etag = resp1.headers.get("ETag")
        resp2 = self.client.get(f"/api/monitor-data/keyword/{kw_id}",
                                 headers={"If-None-Match": etag})
        self.assertEqual(resp2.status_code, 304)

    def test_keyword_detail_http_404(self):
        resp = self.client.get("/api/monitor-data/keyword/nonexistent_id")
        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertIn("error", data)

    # ── account detail HTTP ─────────────────────────

    def test_account_detail_http_200(self):
        boot = self.client.get("/api/monitor-data/bootstrap").get_json()
        acct_id = boot["accounts"][0]["account_id"]
        resp = self.client.get(f"/api/monitor-data/account/{acct_id}")
        self.assertEqual(resp.status_code, 200)
        acct = resp.get_json()
        self.assertEqual(acct["account_id"], acct_id)
        self.assertIn("articles", acct)
        self.assertIn("keywords", acct)

    def test_account_detail_http_304(self):
        boot = self.client.get("/api/monitor-data/bootstrap").get_json()
        acct_id = boot["accounts"][0]["account_id"]
        resp1 = self.client.get(f"/api/monitor-data/account/{acct_id}")
        etag = resp1.headers.get("ETag")
        resp2 = self.client.get(f"/api/monitor-data/account/{acct_id}",
                                 headers={"If-None-Match": etag})
        self.assertEqual(resp2.status_code, 304)

    def test_account_detail_http_404(self):
        resp = self.client.get("/api/monitor-data/account/nonexistent_id")
        self.assertEqual(resp.status_code, 404)

    # ── 旧端点兼容 ──────────────────────────────────

    def test_legacy_monitor_data_still_works(self):
        """旧 /api/monitor-data 端点应继续工作。"""
        resp = self.client.get("/api/monitor-data")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("keywords", data)
        self.assertIn("accounts", data)
        # 旧端点返回完整字段
        kw = data["keywords"][0]
        self.assertIn("runs", kw)
        self.assertIn("accounts", kw)

    def test_legacy_monitor_data_no_store_mutation(self):
        """旧端点不改变 FastStore 状态。"""
        from app.services.monitor_fast_service import get_fast_store
        store = get_fast_store()
        old_etag = store._bootstrap_etag
        self.client.get("/api/monitor-data")
        self.assertEqual(store._bootstrap_etag, old_etag)


if __name__ == "__main__":
    unittest.main()
