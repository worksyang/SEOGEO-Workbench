#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
SOMEURL2MD_DIR = os.path.join(PROJECT_DIR, "SomeURL2MD")
REFRESH_WAIT_SECONDS = 10
WECHAT_LOGIN_PROBE_KEYWORD = "大湾通一峰火燎源"


def _ensure_runtime_paths() -> None:
    os.chdir(PROJECT_DIR)
    sys.path[:] = [path for path in sys.path if path not in {PROJECT_DIR, SOMEURL2MD_DIR}]
    sys.path.insert(0, PROJECT_DIR)
    sys.path.insert(1, SOMEURL2MD_DIR)


_ensure_runtime_paths()

import main as main_module
import article_workflow as workflow
from server.config import DEFAULT_CLASSIFIER_MODEL, DEFAULT_CLASSIFIER_PLATFORM
from werss_client import WeRSSClient
from SomeURL2MD.ai_classifier import AIClassifier
from SomeURL2MD.openai_concurrent_service import MODEL_CONFIG as OCR_MODEL_CONFIG
from SomeURL2MD.openaiapi import OpenAIAPIService


ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]


def _to_abs_path(path: str) -> str:
    if os.path.isabs(path):
        return path
    return os.path.join(PROJECT_DIR, path)


@dataclass
class WorkflowOptions:
    base_url: str = main_module.BASE_URL
    username: str = main_module.USERNAME
    password: str = main_module.PASSWORD
    refresh_before_run: bool = True
    days_to_fetch: int = workflow.DAYS_TO_FETCH
    refresh_wait_seconds: int = REFRESH_WAIT_SECONDS
    selected_mp_ids: Optional[Set[str]] = None
    output_dir: str = _to_abs_path(workflow.OUTPUT_DIR)
    rejected_csv_file: str = _to_abs_path(workflow.REJECTED_CSV_FILE)
    start_page: Optional[int] = None
    end_page: Optional[int] = None
    use_ai_filter: bool = True
    classifier_platform: str = DEFAULT_CLASSIFIER_PLATFORM
    classifier_model: str = DEFAULT_CLASSIFIER_MODEL


def _emit_progress(callback: ProgressCallback, **payload: Any) -> None:
    if callback is not None:
        callback(payload)


def _is_stopped(stop_event: Any) -> bool:
    return bool(stop_event is not None and stop_event.is_set())


def _sleep_with_stop(seconds: int, stop_event: Any) -> bool:
    if seconds <= 0:
        return True

    deadline = time.time() + seconds
    while time.time() < deadline:
        if _is_stopped(stop_event):
            return False
        time.sleep(min(0.2, deadline - time.time()))
    return True


def prepare_workflow_paths(options: WorkflowOptions) -> None:
    workflow.OUTPUT_DIR = _to_abs_path(options.output_dir)
    workflow.REJECTED_CSV_FILE = _to_abs_path(options.rejected_csv_file)


def get_mp_id(mp: Dict[str, Any]) -> Optional[str]:
    return mp.get("id") or mp.get("mp_id")


def get_mp_name(mp: Dict[str, Any]) -> str:
    return mp.get("mp_name") or mp.get("name") or "未知公众号"


def extract_article_url(article: Dict[str, Any]) -> str:
    return str(article.get("url") or article.get("link") or article.get("content_url") or "").strip()


def extract_article_date(article: Dict[str, Any]) -> Optional[str]:
    publish_time = article.get("publish_time") or article.get("created_at") or article.get("update_time")
    if not publish_time:
        return None

    try:
        if isinstance(publish_time, (int, float)):
            return datetime.fromtimestamp(publish_time).strftime("%Y-%m-%d")
        return str(publish_time)[:10]
    except Exception:
        return None


def build_client(options: Optional[WorkflowOptions] = None) -> WeRSSClient:
    if options is None:
        options = WorkflowOptions()

    client = WeRSSClient(
        base_url=options.base_url,
        username=options.username,
        password=options.password,
    )

    if not client.token:
        raise RuntimeError("登录 WeRSS 失败，请检查账号配置")

    return client


def build_classifier(
    platform: Optional[str] = None,
    model: Optional[str] = None,
) -> AIClassifier:
    platform = (platform or DEFAULT_CLASSIFIER_PLATFORM).strip()
    default_model = (model or DEFAULT_CLASSIFIER_MODEL).strip()

    openai_config = OpenAIAPIService.load_config()
    platforms = openai_config.get("platforms", {})

    if platform not in platforms:
        raise RuntimeError(f"默认平台不存在：{platform}")

    if not default_model:
        sample_models = platforms[platform].get("models", [])
        if not sample_models:
            raise RuntimeError(
                f"平台 {platform} 未在 openaiapi.json 中配置示例模型列表，"
                "请在 Web UI 设置页或 server/config.py 中设置默认分类模型"
            )
        default_model = sample_models[0]

    return AIClassifier(platform=platform, model=default_model)


def list_ai_model_options(
    classifier_platform: Optional[str] = None,
    classifier_model: Optional[str] = None,
) -> Dict[str, Any]:
    openai_config = OpenAIAPIService.load_config()
    platforms = []
    for key, value in openai_config.get("platforms", {}).items():
        platforms.append(
            {
                "key": key,
                "name": value.get("name", key),
                "models": value.get("models", []),
            }
        )
    return {
        "platforms": platforms,
        "workflow_models": [
            {
                "key": "classifier",
                "name": "标题分类",
                "active": False,
                "configurable": True,
                "platform": classifier_platform or DEFAULT_CLASSIFIER_PLATFORM,
                "model": classifier_model or DEFAULT_CLASSIFIER_MODEL,
                "description": "默认不调用。只有任务参数打开 AI 过滤时，才按标题分类并决定保存或进入排除流。",
            },
            {
                "key": "markdown",
                "name": "文章转 Markdown",
                "active": True,
                "configurable": False,
                "platform": "",
                "model": "不调用模型",
                "description": "当前批量工作流只按微信原文 URL 转 Markdown，不做正文大模型分析。",
            },
            {
                "key": "image_ocr",
                "name": "图片 OCR / 图片解释",
                "active": False,
                "configurable": False,
                "platform": str(OCR_MODEL_CONFIG.get("primary_platform", "")),
                "model": str(OCR_MODEL_CONFIG.get("primary_model", "")),
                "description": (
                    "代码里保留了图片 OCR 能力，但当前批量工作流已关闭 OCR，"
                    "所以点“开始执行”不会调用图片模型。"
                ),
                "fallbacks": [
                    {
                        "platform": str(OCR_MODEL_CONFIG.get("fallback_model_1_platform", "")),
                        "model": str(OCR_MODEL_CONFIG.get("fallback_model_1", "")),
                    },
                    {
                        "platform": str(OCR_MODEL_CONFIG.get("fallback_model_2_platform", "")),
                        "model": str(OCR_MODEL_CONFIG.get("fallback_model_2", "")),
                    },
                ],
            },
        ],
    }


def probe_ai_model(
    platform: str,
    model: str,
    timeout_seconds: int = 60,
) -> Dict[str, Any]:
    if not platform or not model:
        raise RuntimeError("平台和模型不能为空")

    openai_config = OpenAIAPIService.load_config()
    platforms = openai_config.get("platforms", {})
    if platform not in platforms:
        raise RuntimeError(f"平台不存在：{platform}")

    platforms[platform]["timeout"] = timeout_seconds
    service = OpenAIAPIService(openai_config)
    service.set_platform(platform)
    service.set_model(model)

    started_at = time.perf_counter()
    response = service.generate_text(
        prompt="请只回复三个字：已收到。",
        stream=False,
        temperature=0,
    )
    elapsed_ms = int((time.perf_counter() - started_at) * 1000)
    if not response:
        raise RuntimeError("模型未返回有效响应")

    return {
        "ok": True,
        "platform": platform,
        "model": model,
        "elapsed_ms": elapsed_ms,
        "response": response.strip(),
        "timeout_seconds": timeout_seconds,
    }


def filter_selected_mps(mps: List[Dict[str, Any]], selected_mp_ids: Optional[Set[str]]) -> List[Dict[str, Any]]:
    if not selected_mp_ids:
        return mps
    selected = []
    for mp in mps:
        mp_id = get_mp_id(mp)
        if mp_id and mp_id in selected_mp_ids:
            selected.append(mp)
    return selected


def get_wechat_auth_status(
    client: WeRSSClient,
    keyword: str = WECHAT_LOGIN_PROBE_KEYWORD,
) -> Dict[str, Any]:
    raw_qr_status = client.get_qr_status()
    strong_check = client.check_wechat_login(keyword=keyword)

    raw_login_status = bool(raw_qr_status.get("login_status"))
    logged_in = bool(strong_check.get("logged_in"))
    can_confirm = bool(strong_check.get("can_confirm"))

    if can_confirm:
        display_status = "已登录" if logged_in else "未登录"
    else:
        display_status = "状态未知"

    return {
        "logged_in": logged_in,
        "can_confirm": can_confirm,
        "display_status": display_status,
        "message": str(strong_check.get("message") or "强校验未返回结果"),
        "probe_keyword": keyword,
        "raw_qr_status": raw_qr_status,
        "raw_login_status": raw_login_status,
        "raw_display_status": "已登录" if raw_login_status else "未登录",
        "qr_code_available": bool(raw_qr_status.get("qr_code")),
        "inconsistent": can_confirm and raw_login_status != logged_in,
    }


def get_runtime_overview(options: Optional[WorkflowOptions] = None) -> Dict[str, Any]:
    client = build_client(options)
    wechat_status = get_wechat_auth_status(client)
    mps = client.get_all_mps()
    return {
        "qr_status": wechat_status.get("raw_qr_status", {}),
        "wechat_status": wechat_status,
        "mps": mps,
        "stats": {
            "total_mps": len(mps),
            "enabled_mps": sum(1 for mp in mps if mp.get("status") == 1),
        },
    }


def refresh_mps(
    client: WeRSSClient,
    mps: List[Dict[str, Any]],
    wait_seconds: int = REFRESH_WAIT_SECONDS,
    start_page: Optional[int] = None,
    end_page: Optional[int] = None,
    progress_callback: ProgressCallback = None,
    stop_event: Any = None,
) -> Dict[str, int]:
    print("\n" + "=" * 60)
    print("第一阶段：刷新公众号")
    print("=" * 60)

    if not mps:
        print("⚠️ 未找到任何公众号，任务终止。")
        return {"total": 0, "success": 0, "failed": 0, "skipped": 0, "stopped": 0}

    print(f"📋 共找到 {len(mps)} 个公众号")
    if wait_seconds > 0:
        estimated = len(mps) * wait_seconds
        print(f"⏱️ 按当前策略，预计等待时长约 {estimated // 60} 分钟 {estimated % 60} 秒")

    success_count = 0
    failed_count = 0
    skipped_count = 0
    stopped = 0

    for index, mp in enumerate(mps, 1):
        if _is_stopped(stop_event):
            stopped = 1
            print("⏹️ 已收到停止请求，刷新阶段提前结束。")
            break

        mp_id = get_mp_id(mp)
        mp_name = get_mp_name(mp)

        _emit_progress(
            progress_callback,
            stage="refresh",
            current=index - 1,
            total=len(mps),
            message=f"正在刷新：{mp_name}",
            mp_name=mp_name,
        )

        if not mp_id:
            skipped_count += 1
            print(f"\n⚠️ [{index}/{len(mps)}] {mp_name} 缺少 ID，跳过")
            continue

        print(f"\n🔄 [{index}/{len(mps)}] 正在刷新：{mp_name}")
        print(f"⏰ 开始时间：{datetime.now().strftime('%H:%M:%S')}")

        if client.update_mp_articles(mp_id, start_page=start_page, end_page=end_page):
            success_count += 1
            print("✅ 刷新请求已提交")
            if wait_seconds > 0 and index < len(mps):
                print(f"⏳ 等待 {wait_seconds} 秒，给爬虫留出抓取时间...")
                print(f"📊 剩余公众号：{len(mps) - index} 个")
                if not _sleep_with_stop(wait_seconds, stop_event):
                    stopped = 1
                    print("⏹️ 已收到停止请求，刷新等待被中断。")
                    break
        else:
            failed_count += 1
            print("❌ 刷新失败")

        _emit_progress(
            progress_callback,
            stage="refresh",
            current=min(index, len(mps)),
            total=len(mps),
            message=f"已完成刷新：{mp_name}",
            mp_name=mp_name,
        )

    print("\n" + "=" * 60)
    print("刷新阶段完成")
    print("=" * 60)
    print(f"✅ 成功刷新：{success_count}/{len(mps)}")
    if failed_count:
        print(f"❌ 刷新失败：{failed_count}")
    if skipped_count:
        print(f"⚠️ 跳过账号：{skipped_count}")
    if stopped:
        print("⏹️ 本阶段已提前停止")

    return {
        "total": len(mps),
        "success": success_count,
        "failed": failed_count,
        "skipped": skipped_count,
        "stopped": stopped,
    }


def fetch_recent_articles(
    client: WeRSSClient,
    days: int,
    mps: List[Dict[str, Any]],
    progress_callback: ProgressCallback = None,
    stop_event: Any = None,
) -> Tuple[List[Dict[str, Any]], int]:
    print("\n" + "=" * 60)
    print(f"第二阶段：拉取最近 {days} 天文章")
    print("=" * 60)

    if not mps:
        print("⚠️ 没有可读取的公众号。")
        return [], 0

    today = datetime.now().date()
    date_range = {(today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(days)}

    all_recent_articles: List[Dict[str, Any]] = []
    seen_urls = set()
    fetch_failed = 0

    for index, mp in enumerate(mps, 1):
        if _is_stopped(stop_event):
            print("⏹️ 已收到停止请求，文章拉取提前结束。")
            return all_recent_articles, 1

        mp_id = get_mp_id(mp)
        mp_name = get_mp_name(mp)

        if not mp_id:
            continue

        _emit_progress(
            progress_callback,
            stage="fetch",
            current=index - 1,
            total=len(mps),
            message=f"正在读取：{mp_name}",
            mp_name=mp_name,
        )

        print(f"\n🔍 [{index}/{len(mps)}] 正在读取：{mp_name}")
        try:
            mp_articles = client.get_mp_articles(mp_id)
        except Exception as exc:
            fetch_failed += 1
            print(f"❌ 读取失败：{exc}")
            continue

        matched_count = 0
        for article in mp_articles:
            url = extract_article_url(article)
            if not url or url in seen_urls:
                continue

            article_date = extract_article_date(article)
            if article_date in date_range:
                all_recent_articles.append(article)
                seen_urls.add(url)
                matched_count += 1

        print(f"📄 近 {days} 天命中：{matched_count} 篇")
        _emit_progress(
            progress_callback,
            stage="fetch",
            current=index,
            total=len(mps),
            message=f"已读取：{mp_name}",
            mp_name=mp_name,
        )

    print("\n" + "=" * 60)
    print("拉取阶段完成")
    print("=" * 60)
    print(f"✅ 最近 {days} 天共获得 {len(all_recent_articles)} 篇不重复文章")
    if fetch_failed:
        print(f"❌ 读取失败公众号数：{fetch_failed}")

    return all_recent_articles, 0


def _empty_workflow_summary() -> Dict[str, int]:
    return {
        "fetched": 0,
        "skipped": 0,
        "special_saved": 0,
        "special_failed": 0,
        "accepted_saved": 0,
        "accepted_failed": 0,
        "rejected_saved": 0,
        "rejected_failed": 0,
        "rejected_csv_total": 0,
        "stopped": 0,
    }


async def run_integrated_workflow(
    client: WeRSSClient,
    mps: List[Dict[str, Any]],
    options: WorkflowOptions,
    progress_callback: ProgressCallback = None,
    stop_event: Any = None,
) -> Dict[str, int]:
    prepare_workflow_paths(options)

    print("\n" + "=" * 60)
    print("第三阶段：执行融合版 Article Workflow")
    print("=" * 60)
    print(f"📂 Markdown 输出目录：{workflow.OUTPUT_DIR}")
    print(f"📄 拒绝记录文件：{workflow.REJECTED_CSV_FILE}")
    print(f"🤖 AI 过滤文章：{'开启' if options.use_ai_filter else '关闭'}")

    os.makedirs(workflow.OUTPUT_DIR, exist_ok=True)
    summary = _empty_workflow_summary()

    recent_articles, stopped = fetch_recent_articles(
        client,
        options.days_to_fetch,
        mps=mps,
        progress_callback=progress_callback,
        stop_event=stop_event,
    )
    summary["fetched"] = len(recent_articles)
    summary["stopped"] = stopped
    if stopped:
        return summary

    if not recent_articles:
        print("✅ 没有需要处理的新文章，任务结束。")
        return summary

    _emit_progress(
        progress_callback,
        stage="workflow",
        current=0,
        total=1,
        message="扫描本地输出与拒绝记录",
    )
    existing_titles = workflow.scan_existing_articles(workflow.OUTPUT_DIR)
    rejected_urls = workflow.load_rejected_articles(workflow.REJECTED_CSV_FILE)
    filtered_articles, skipped_articles_count = workflow.filter_existing_articles(
        recent_articles,
        existing_titles,
        rejected_urls,
    )
    summary["skipped"] = skipped_articles_count

    if not filtered_articles:
        print("✅ 所有文章都已处理过，无需重复处理，任务结束。")
        summary["rejected_csv_total"] = len(rejected_urls)
        return summary

    special_articles, normal_articles = workflow.separate_special_accounts(
        filtered_articles,
        workflow.SPECIAL_ACCOUNTS,
        client,
    )

    if _is_stopped(stop_event):
        summary["stopped"] = 1
        print("⏹️ 已收到停止请求，分类前结束流程。")
        return summary

    if special_articles:
        _emit_progress(
            progress_callback,
            stage="workflow",
            current=0,
            total=len(filtered_articles),
            message=f"处理特殊账号文章：{len(special_articles)} 篇",
        )
        summary["special_saved"], summary["special_failed"] = await workflow.save_articles_as_markdown(
            special_articles,
            workflow.OUTPUT_DIR,
            client,
        )

    if not normal_articles:
        print("✅ 所有文章均为特殊账号文章，普通账号无需进入 AI 分类。")
        print_final_workflow_summary(summary)
        return summary

    if _is_stopped(stop_event):
        summary["stopped"] = 1
        print("⏹️ 已收到停止请求，AI 分类前结束流程。")
        return summary

    if not options.use_ai_filter:
        print(f"\n📋 AI 过滤已关闭，普通账号文章不做标题分类，直接保存：{len(normal_articles)} 篇")
        for article in normal_articles:
            article["classified_type"] = article.get("classified_type") or "未分类"

        _emit_progress(
            progress_callback,
            stage="save",
            current=0,
            total=1,
            message="AI 过滤关闭，直接保存普通文章",
        )
        summary["accepted_saved"], summary["accepted_failed"] = await workflow.save_articles_as_markdown(
            normal_articles,
            workflow.OUTPUT_DIR,
            client,
        )
        summary["rejected_csv_total"] = len(rejected_urls)
        _emit_progress(
            progress_callback,
            stage="save",
            current=1,
            total=1,
            message="保存完成",
        )
        print_final_workflow_summary(summary)
        return summary

    article_titles = [article["title"] for article in normal_articles if article.get("title")]
    print(f"\n📋 普通账号待分类文章：{len(article_titles)} 篇")

    classifier = build_classifier(
        platform=options.classifier_platform,
        model=options.classifier_model,
    )
    all_classified_results: List[Dict[str, Any]] = []

    if len(article_titles) <= workflow.AI_BATCH_SIZE:
        _emit_progress(
            progress_callback,
            stage="classify",
            current=0,
            total=1,
            message="AI 分类处理中",
        )
        print("📝 标题数量未超过批量上限，直接分类...")
        classified_results = classifier.classify_titles(article_titles)
        if classified_results:
            all_classified_results.extend(classified_results)
        _emit_progress(
            progress_callback,
            stage="classify",
            current=1,
            total=1,
            message="AI 分类完成",
        )
    else:
        total_batches = (len(article_titles) + workflow.AI_BATCH_SIZE - 1) // workflow.AI_BATCH_SIZE
        print(f"📝 标题数量较多，将分 {total_batches} 批执行 AI 分类...")

        for start in range(0, len(article_titles), workflow.AI_BATCH_SIZE):
            if _is_stopped(stop_event):
                summary["stopped"] = 1
                print("⏹️ 已收到停止请求，批量分类提前结束。")
                return summary

            end = min(start + workflow.AI_BATCH_SIZE, len(article_titles))
            batch_titles = article_titles[start:end]
            batch_no = (start // workflow.AI_BATCH_SIZE) + 1

            _emit_progress(
                progress_callback,
                stage="classify",
                current=batch_no - 1,
                total=total_batches,
                message=f"AI 分类第 {batch_no}/{total_batches} 批",
            )

            print(f"\n🔄 正在处理第 {batch_no}/{total_batches} 批，共 {len(batch_titles)} 篇")
            batch_results = classifier.classify_titles(batch_titles)
            if batch_results:
                all_classified_results.extend(batch_results)
                print(f"✅ 第 {batch_no} 批完成，获得 {len(batch_results)} 条分类结果")
            else:
                print(f"❌ 第 {batch_no} 批未返回有效分类结果")

        _emit_progress(
            progress_callback,
            stage="classify",
            current=total_batches,
            total=total_batches,
            message="AI 分类完成",
        )

    if not all_classified_results:
        raise RuntimeError("AI 分类失败或未返回任何结果，任务终止")

    accepted_articles, rejected_articles = workflow.match_and_filter_articles(
        normal_articles,
        all_classified_results,
    )

    if _is_stopped(stop_event):
        summary["stopped"] = 1
        print("⏹️ 已收到停止请求，保存 Markdown 前结束流程。")
        return summary

    _emit_progress(
        progress_callback,
        stage="save",
        current=0,
        total=2,
        message="保存入选文章",
    )
    summary["accepted_saved"], summary["accepted_failed"] = await workflow.save_articles_as_markdown(
        accepted_articles,
        workflow.OUTPUT_DIR,
        client,
    )

    _emit_progress(
        progress_callback,
        stage="save",
        current=1,
        total=2,
        message="归档排除流文章",
    )
    summary["rejected_saved"], summary["rejected_failed"] = await workflow.save_rejected_articles_as_markdown(
        rejected_articles,
        workflow.OUTPUT_DIR,
        client,
    )
    summary["rejected_csv_total"] = workflow.save_rejected_articles(
        rejected_articles,
        workflow.REJECTED_CSV_FILE,
        client,
    )

    _emit_progress(
        progress_callback,
        stage="save",
        current=2,
        total=2,
        message="保存完成",
    )
    print_final_workflow_summary(summary)
    return summary


def print_final_workflow_summary(summary: Dict[str, int]) -> None:
    print("\n" + "=" * 60)
    print("融合工作流任务完成")
    print("=" * 60)
    print(f"- 总计发现文章：{summary['fetched']} 篇")
    print(f"- 已跳过文章：{summary['skipped']} 篇")
    print(f"- 特殊账号成功处理：{summary['special_saved']} 篇")
    print(f"- 特殊账号处理失败：{summary['special_failed']} 篇")
    print(f"- 入选/直接保存成功处理：{summary['accepted_saved']} 篇")
    print(f"- 入选/直接保存处理失败：{summary['accepted_failed']} 篇")
    print(f"- 排除流成功归档：{summary['rejected_saved']} 篇")
    print(f"- 排除流归档失败：{summary['rejected_failed']} 篇")
    if summary["rejected_csv_total"]:
        print(f"- 拒绝记录总数：{summary['rejected_csv_total']} 篇")
    if summary.get("stopped"):
        print("- 任务状态：用户已请求停止")


def run_full_workflow(
    options: Optional[WorkflowOptions] = None,
    progress_callback: ProgressCallback = None,
    stop_event: Any = None,
) -> Dict[str, Any]:
    if options is None:
        options = WorkflowOptions()

    prepare_workflow_paths(options)

    print("=" * 60)
    print("微信公众号刷新 + Article Workflow 一体化任务启动")
    print("=" * 60)
    print(f"📁 项目目录：{PROJECT_DIR}")

    client = build_client(options)
    print("✅ WeRSS 登录成功")

    wechat_status = get_wechat_auth_status(client)
    wechat_login = bool(wechat_status.get("logged_in"))
    print(f"📱 微信登录强校验：{wechat_status.get('display_status')}")
    print(f"🧪 强校验详情：{wechat_status.get('message')}")
    if wechat_status.get("inconsistent"):
        print(
            "⚠️ 检测到后端原始扫码状态与强校验结果不一致，"
            f"原始接口={wechat_status.get('raw_display_status')}，已按强校验结果处理。"
        )

    all_mps = client.get_all_mps()
    if not all_mps:
        print("⚠️ 未找到任何公众号，任务终止。")
        return {
            "selected_mps": 0,
            "refresh": {"total": 0, "success": 0, "failed": 0, "skipped": 0, "stopped": 0},
            "workflow": _empty_workflow_summary(),
            "wechat_login": wechat_login,
            "wechat_status": wechat_status,
            "stopped": False,
        }

    selected_mps = filter_selected_mps(all_mps, options.selected_mp_ids)
    print(f"📚 本次纳入流程的公众号：{len(selected_mps)}/{len(all_mps)} 个")

    if not selected_mps:
        raise RuntimeError("当前没有任何公众号被选中，无法开始任务")

    if options.refresh_before_run and not wechat_login:
        if wechat_status.get("can_confirm"):
            raise RuntimeError("微信未通过强校验登录，无法执行刷新。请先点击“扫码登录”完成授权，或切换为仅使用现有数据。")
        raise RuntimeError(
            "微信登录状态强校验失败，当前无法确认授权是否有效："
            f"{wechat_status.get('message')}。请先重新扫码或检查 WeRSS 服务。"
        )

    if options.refresh_before_run:
        refresh_summary = refresh_mps(
            client,
            selected_mps,
            wait_seconds=options.refresh_wait_seconds,
            start_page=options.start_page,
            end_page=options.end_page,
            progress_callback=progress_callback,
            stop_event=stop_event,
        )
    else:
        print("\n你选择了[直接使用现有数据]模式，将跳过公众号刷新。")
        refresh_summary = {
            "total": len(selected_mps),
            "success": 0,
            "failed": 0,
            "skipped": len(selected_mps),
            "stopped": 0,
        }

    if _is_stopped(stop_event) or refresh_summary.get("stopped"):
        return {
            "selected_mps": len(selected_mps),
            "refresh": refresh_summary,
            "workflow": _empty_workflow_summary(),
            "wechat_login": wechat_login,
            "wechat_status": wechat_status,
            "stopped": True,
        }

    workflow_summary = asyncio.run(
        run_integrated_workflow(
            client,
            selected_mps,
            options,
            progress_callback=progress_callback,
            stop_event=stop_event,
        )
    )

    print("\n" + "=" * 60)
    print("总控脚本执行结束")
    print("=" * 60)
    if options.refresh_before_run:
        print(
            f"刷新结果：成功 {refresh_summary['success']} / 失败 {refresh_summary['failed']} / "
            f"跳过 {refresh_summary['skipped']}"
        )
    else:
        print(f"刷新结果：本次未执行刷新，直接读取现有数据（公众号数 {refresh_summary['total']}）")
    print(
        f"工作流结果：发现 {workflow_summary['fetched']} 篇，"
        f"入选/直接保存成功 {workflow_summary['accepted_saved']} 篇，"
        f"排除流归档成功 {workflow_summary['rejected_saved']} 篇"
    )

    return {
        "selected_mps": len(selected_mps),
        "refresh": refresh_summary,
        "workflow": workflow_summary,
        "wechat_login": wechat_login,
        "wechat_status": wechat_status,
        "stopped": bool(workflow_summary.get("stopped")),
    }
