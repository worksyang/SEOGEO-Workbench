"""用 Python Playwright 截图 demo 页面，存到 260630_文章轮换率热力日历/preview/。

为了避开现有 chrome 实例冲突，这里用一个独立的 isolated user-data-dir。
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
URL = "http://127.0.0.1:8766/index.html"
OUT_DIR = HERE / "preview"
OUT_DIR.mkdir(exist_ok=True)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1400, "height": 1800})
        page = context.new_page()
        page.goto(URL, wait_until="networkidle")
        page.wait_for_timeout(800)  # 等热力图渲染

        # 1) 全页截图
        full_path = OUT_DIR / "01_full.png"
        page.screenshot(path=str(full_path), full_page=True)
        print(f"✅ {full_path.name}")

        # 2) 仅第一段（30天轮换率日历）截图
        cal = page.locator(".section").nth(0)
        cal.screenshot(path=str(OUT_DIR / "02_calendar.png"))
        print(f"✅ 02_calendar.png")

        # 3) 仅第二段（文章寿命热力图）
        life = page.locator(".section").nth(1)
        life.screenshot(path=str(OUT_DIR / "03_life.png"))
        print(f"✅ 03_life.png")

        # 4) 交互演示：悬停任意已出现的格子显示 tooltip
        page.locator(
            ".life-cell.state-new, .life-cell.state-return, .life-cell.state-short, .life-cell.state-stable, .life-cell.state-core"
        ).first.hover()
        page.wait_for_timeout(300)
        life.screenshot(path=str(OUT_DIR / "04_life_hover.png"))
        print(f"✅ 04_life_hover.png")

        browser.close()


if __name__ == "__main__":
    main()