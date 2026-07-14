"""
AIDSO（dso.aidso.com）微信搜索词热度抓取服务。

==============================================================================
背景与数据流
==============================================================================

抓取目标：单关键词在 AIDSO 平台上的微信搜索热度详情，包括主词指标
（月均搜索/点击/下拉词数等）和下拉词列表。

事实层抓取链路：
    1. 启动 Playwright 持久化 Chromium 上下文（user_data_dir = profile_dir）
    2. 打开 detail 页（keyword 走 query string）
    3. 等到 localStorage.token 出现 → 视为登录态就绪
    4. 监听 page.on("response")，被动收集两个 XHR：
         /dso/api/keyword/info/wx/detail     -> 主词指标
         /dso/api/keyword/info/wx/down_word  -> 下拉词列表
    5. 整形为统一结构返回；登录态写回 profile_dir 复用

==============================================================================
踩坑笔记（2026-06-14，必读）
==============================================================================

环境：macOS 26.4 (25E246) / Apple Silicon / Cursor 中由 Playwright 1.58
拉起的 Chromium for Testing 145（chromium-1208）。

现象：launch_persistent_context 后浏览器窗口闪现立即 SIGTRAP(0x1)，
连续多次崩溃，~/Library/Logs/DiagnosticReports/ 留下 EXC_BREAKPOINT
报告。栈顶在 ChromeMain（CHECK 失败），紧邻一条 IMK 警告：
    `error messaging the mach port for IMKCFRunLoopWakeUpReliable`

排查关键证据：
- 直接从 shell 手动启动同一份 CfT，进程能稳定运行，只输出 IMK 警告。
- 崩溃报告里 `Parent Process: node / Responsible: Cursor`，说明只有
  「Playwright(node) 在 Cursor 进程上下文里拉起 CfT」 这条路径才会触发。
- 不是 profile 损坏问题：用全新空 profile 重现同样 SIGTRAP。

结论：Playwright bundled CfT 145 + macOS 26.4 + Cursor parent 的组合
不稳定，与 profile 状态无关。最稳的解法是改用 system Google Chrome。

==============================================================================
默认值约定
==============================================================================

- DEFAULT_BROWSER_CHANNEL = "chrome"
    走 Playwright channel 机制，调起 /Applications/Google Chrome.app。
    headed / headless 两种模式都已验证可正常完成抓取。

- 若运行环境没装 system Chrome（如 CI / 容器），调用方需要显式传
  channel=None，回落到 Playwright bundled chromium。注意此 fallback
  在 macOS 26.4 + Cursor 下大概率会复现 SIGTRAP，请在裸终端环境运行。

- 若需要锁定具体浏览器二进制（如 Beta/Canary），传 executable_path 即可，
  此时 channel 会被忽略。

调用方一律不需要硬编码 channel；遵循默认即可。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from time import sleep
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


class AidsoHeatError(RuntimeError):
    """AIDSO 抓取链路上的可预期错误（登录超时、未捕获响应等）。"""


class AidsoLoginRequiredError(AidsoHeatError):
    """当前 profile 不可直接抓取，需要人工补登录。"""


class AidsoProfileBusyError(AidsoHeatError):
    """当前 profile 正被其他浏览器实例占用。"""


DETAIL_URL_TEMPLATE = "https://dso.aidso.com/WsoKeyWordDetail/detail?keyword={keyword}"

DEFAULT_BROWSER_CHANNEL = "chrome"

_DETAIL_API_PATH = "/dso/api/keyword/info/wx/detail"
_DOWN_WORD_API_PATH = "/dso/api/keyword/info/wx/down_word"

_LOGIN_TOKEN_PROBE = "() => !!localStorage.getItem('token')"

_DEFAULT_VIEWPORT = {"width": 1440, "height": 960}

_LAUNCH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
]

_PROFILE_BUSY_MARKERS = (
    "ProcessSingleton",
    "SingletonLock",
    "profile is already in use",
)


def _normalize_keyword(keyword: str) -> str:
    keyword = str(keyword).strip()
    if not keyword:
        raise ValueError("keyword is required")
    return keyword


def _build_launch_kwargs(
    profile_path: Path,
    *,
    headless: bool,
    channel: str | None,
    executable_path: str | None,
) -> dict[str, Any]:
    """组装 launch_persistent_context 参数。

    优先级：executable_path > channel > bundled chromium。
    传入 channel="" 或 None 表示放弃 channel；executable_path 一旦给定
    就忽略 channel（Playwright 会直接用绝对路径启动）。
    """
    launch_kwargs: dict[str, Any] = dict(
        user_data_dir=str(profile_path),
        headless=headless,
        viewport=_DEFAULT_VIEWPORT,
        args=list(_LAUNCH_ARGS),
    )
    if executable_path:
        launch_kwargs["executable_path"] = executable_path
    elif channel:
        launch_kwargs["channel"] = channel
    return launch_kwargs


def _is_profile_busy_error(exc: Exception) -> bool:
    return any(marker in str(exc) for marker in _PROFILE_BUSY_MARKERS)


def _launch_persistent_context(browser_type, launch_kwargs: dict[str, Any]):
    for attempt in range(2):
        try:
            return browser_type.launch_persistent_context(**launch_kwargs)
        except PlaywrightError as exc:
            if not _is_profile_busy_error(exc):
                raise
            if attempt == 0:
                sleep(1)
                continue
            raise AidsoProfileBusyError(
                "AIDSO 浏览器 profile 当前正被其他 Chrome 或抓取任务占用，请稍后重试。"
            ) from exc


def _build_result(keyword: str, detail_payload: dict | None, down_word_payload: dict | None) -> dict:
    found = bool(detail_payload and detail_payload.get("data"))
    detail = detail_payload.get("data") if detail_payload else None
    down_words = []
    if down_word_payload and isinstance(down_word_payload.get("data"), dict):
        down_words = down_word_payload["data"].get("result") or []

    return {
        "keyword": keyword,
        "found": found,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "detail": None
        if not detail
        else {
            "keyword": detail.get("keyword") or keyword,
            "month_cover_count": detail.get("month_cover_count"),
            "month_cover_count_str": detail.get("month_cover_count_str"),
            "month_click_count": detail.get("month_click_count"),
            "down_keyword_count": detail.get("down_keyword_count"),
            "down_keyword_month_covercount": detail.get("down_keyword_month_covercount"),
            "competition": detail.get("competition"),
            "competition_cn": detail.get("competition_cn"),
            "word_length": detail.get("word_length"),
        },
        "down_words": [
            {
                "keyword": item.get("keyword"),
                "month_cover_count": item.get("month_cover_count"),
                "month_cover_count_str": item.get("month_cover_count_str"),
                "month_click_count": item.get("month_click_count"),
                "competition": item.get("competition"),
                "competition_cn": item.get("competition_cn"),
            }
            for item in down_words
        ],
    }


def _wait_for_login_token(page, wait_timeout_ms: int) -> None:
    """把“是否已登录”的判断统一收口到 localStorage.token。

    这是当前最稳定、最便宜的会话探针：
    - 已登录：token 存在，后续 detail/down_word XHR 会自动带鉴权
    - 未登录/登录态失效：token 不存在，需要切到 headed 让用户扫码
    """
    try:
        page.wait_for_function(_LOGIN_TOKEN_PROBE, timeout=wait_timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise AidsoLoginRequiredError("当前 Playwright profile 未登录，或登录态已失效。") from exc


def _decorate_source(result: dict, **extra: Any) -> dict:
    source = result.setdefault("source", {})
    source.update({key: value for key, value in extra.items() if value is not None})
    return result


def ensure_aidso_login(
    profile_dir: str | Path,
    keyword: str = "友邦环宇",
    wait_timeout_ms: int = 300_000,
    channel: str | None = DEFAULT_BROWSER_CHANNEL,
    executable_path: str | None = None,
) -> dict:
    """打开有头浏览器，等待用户在 AIDSO 完成登录。

    成功条件：localStorage.token 出现。整套登录态会写回 profile_dir，
    后续 fetch_aidso_keyword_heat 复用同一目录即可免登录。

    默认 channel 走 system Chrome（见模块顶部踩坑笔记）。
    headless 在登录场景没意义，固定为 False。
    """
    profile_path = Path(profile_dir).expanduser().resolve()
    profile_path.mkdir(parents=True, exist_ok=True)
    keyword = _normalize_keyword(keyword)

    launch_kwargs = _build_launch_kwargs(
        profile_path,
        headless=False,
        channel=channel,
        executable_path=executable_path,
    )

    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright.chromium, launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(DETAIL_URL_TEMPLATE.format(keyword=keyword), wait_until="domcontentloaded")
            _wait_for_login_token(page, wait_timeout_ms)
            return {
                "ok": True,
                "message": "AIDSO 登录态已就绪",
                "profile_dir": str(profile_path),
                "current_url": page.url,
                "logged_in_at": datetime.now().isoformat(timespec="seconds"),
            }
        except AidsoLoginRequiredError as exc:
            raise AidsoHeatError("等待登录超时。请在打开的浏览器里完成登录后重试。") from exc
        except PlaywrightTimeoutError as exc:
            raise AidsoHeatError("等待登录超时。请在打开的浏览器里完成登录后重试。") from exc
        finally:
            context.close()


def fetch_aidso_keyword_heat(
    keyword: str,
    profile_dir: str | Path,
    headless: bool = True,
    wait_timeout_ms: int = 30_000,
    channel: str | None = DEFAULT_BROWSER_CHANNEL,
    executable_path: str | None = None,
) -> dict:
    """以 profile_dir 中保存的登录态抓取单关键词热度。

    这是纯事实抓取入口，只假设“当前 profile 已经有可用登录态”。
    如果调用方需要“没登录时自动拉起有头扫码再继续”，请走
    resolve_aidso_keyword_heat，而不是在外部手写 try/catch 流程。

    headless 默认为 True：已经验证 system Chrome 在 headless 下能稳定
    完成 detail / down_word 两个 XHR 抓取（~3 秒级别）。如需观察页面，
    显式传 headless=False。

    抓取通过被动监听 response 完成，比 dom-scrape 更稳：
      - detail：response.url 含 /dso/api/keyword/info/wx/detail
      - down_word：response.url 含 /dso/api/keyword/info/wx/down_word
    detail 缺失视为致命错误；down_word 缺失则结果中 down_words 为空数组，
    不抛异常（部分关键词没有下拉词是合理状态）。

    超时语义：wait_for_function 等不到 token 视为 profile 未登录；
    XHR 等待用同一个 wait_timeout_ms 限时轮询。
    """
    keyword = _normalize_keyword(keyword)
    profile_path = Path(profile_dir).expanduser().resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    detail_payload: dict | None = None
    down_word_payload: dict | None = None

    launch_kwargs = _build_launch_kwargs(
        profile_path,
        headless=headless,
        channel=channel,
        executable_path=executable_path,
    )

    with sync_playwright() as playwright:
        context = _launch_persistent_context(playwright.chromium, launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()

            def handle_response(response) -> None:
                nonlocal detail_payload, down_word_payload
                if response.request.resource_type not in {"fetch", "xhr"}:
                    return
                url = response.url
                if _DETAIL_API_PATH in url and detail_payload is None:
                    try:
                        detail_payload = response.json()
                    except Exception:
                        detail_payload = None
                if _DOWN_WORD_API_PATH in url and down_word_payload is None:
                    try:
                        down_word_payload = response.json()
                    except Exception:
                        down_word_payload = None

            page.on("response", handle_response)
            page.goto(DETAIL_URL_TEMPLATE.format(keyword=keyword), wait_until="domcontentloaded")
            _wait_for_login_token(page, wait_timeout_ms)

            start = datetime.now().timestamp()
            while datetime.now().timestamp() - start < wait_timeout_ms / 1000:
                if detail_payload is not None and down_word_payload is not None:
                    break
                page.wait_for_timeout(250)

            if detail_payload is None:
                raise AidsoHeatError(
                    "未捕获到 detail 响应。请确认当前 profile 已登录，且该页面可以正常打开。"
                )

            result = _build_result(keyword, detail_payload, down_word_payload)
            return _decorate_source(
                result,
                provider="aidso",
                mode="playwright_persistent_context",
                profile_dir=str(profile_path),
                headless=headless,
                channel=channel,
                executable_path=executable_path,
                page_url=page.url,
                queried_at=datetime.now().isoformat(timespec="seconds"),
            )
        except PlaywrightTimeoutError as exc:
            raise AidsoHeatError("当前页面加载超时。") from exc
        finally:
            context.close()


def resolve_aidso_keyword_heat(
    keyword: str,
    profile_dir: str | Path,
    headless: bool = True,
    wait_timeout_ms: int = 30_000,
    auto_login: bool = True,
    login_wait_timeout_ms: int = 300_000,
    channel: str | None = DEFAULT_BROWSER_CHANNEL,
    executable_path: str | None = None,
) -> dict:
    """控制层入口：优先直接抓，缺登录时自动切到有头扫码，再回到目标模式抓取。

    这是给 CLI / 本地自动化调用的默认入口。

    状态机：
      1. 先按调用方目标模式抓取（通常是 headless=True）
      2. 若 profile 未登录，且 auto_login=True，则临时切到 headed 浏览器
      3. 等用户扫码成功后，再按原目标模式重试一次

    这样可以满足两类需求：
    - 日常定时/批量抓取：大部分时间直接无头跑完
    - 登录态失效：只在必要时拉起有头浏览器，不要求用户手工换命令
    """
    try:
        result = fetch_aidso_keyword_heat(
            keyword=keyword,
            profile_dir=profile_dir,
            headless=headless,
            wait_timeout_ms=wait_timeout_ms,
            channel=channel,
            executable_path=executable_path,
        )
        return _decorate_source(
            result,
            resolve_mode="direct_existing_session",
            login_recovered=False,
        )
    except AidsoLoginRequiredError as exc:
        if not auto_login:
            raise AidsoLoginRequiredError(
                "当前 Playwright profile 未登录，或登录态已失效。请先手动登录，或开启 auto_login。"
            ) from exc

        try:
            login_payload = ensure_aidso_login(
                profile_dir=profile_dir,
                keyword=keyword,
                wait_timeout_ms=login_wait_timeout_ms,
                channel=channel,
                executable_path=executable_path,
            )
        except AidsoHeatError as exc:
            raise AidsoHeatError("已自动拉起浏览器等待扫码登录，但等待登录超时。") from exc
        result = fetch_aidso_keyword_heat(
            keyword=keyword,
            profile_dir=profile_dir,
            headless=headless,
            wait_timeout_ms=wait_timeout_ms,
            channel=channel,
            executable_path=executable_path,
        )
        return _decorate_source(
            result,
            resolve_mode="interactive_login_recovery",
            login_recovered=True,
            login_recovered_at=login_payload.get("logged_in_at"),
            login_current_url=login_payload.get("current_url"),
        )