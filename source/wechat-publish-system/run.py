import os
import sys
import time
import importlib.util
from datetime import datetime, timedelta
from typing import List, Tuple


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WRITE_DIR = os.path.join(PROJECT_ROOT, "Write")
PUBLISH_DIR = os.path.join(PROJECT_ROOT, "Publish")

for path in (WRITE_DIR, PUBLISH_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)


def load_module(module_name: str, module_path: str):
    """按路径动态加载模块，避免同名冲突。"""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


write_module = load_module("write_main_module", os.path.join(WRITE_DIR, "main.py"))
publish_scheduler_module = load_module("publish_scheduler_module", os.path.join(PUBLISH_DIR, "publish_scheduler.py"))
account_manager_module = load_module("account_manager_module", os.path.join(PUBLISH_DIR, "account_manager.py"))

ContentManager = write_module.ContentManager
PublishScheduler = publish_scheduler_module.PublishScheduler
AccountManager = account_manager_module.AccountManager


def prompt_directory(message: str) -> str:
    while True:
        path = input(message).strip()
        if os.path.isdir(path):
            return path
        print("❌ 目录不存在，请重新输入。")


def prompt_choice(message: str, options: List[str], allow_empty: bool = False, empty_hint: str = "") -> int:
    while True:
        choice = input(message).strip()
        if not choice and allow_empty:
            return -1
        if choice.isdigit():
            value = int(choice)
            if 1 <= value <= len(options):
                return value
        print(empty_hint if allow_empty else "❌ 请输入有效编号。")


def prompt_positive_int(message: str, min_value: int = 1, max_value: int = None) -> int:
    while True:
        value = input(message).strip()
        if value.isdigit():
            number = int(value)
            if number >= min_value and (max_value is None or number <= max_value):
                return number
        print("❌ 请输入有效的数字。")


def choose_prompt_and_headfoot() -> Tuple[str, str]:
    prompt_dir = os.path.join(WRITE_DIR, "prompt")
    headfoot_dir = os.path.join(WRITE_DIR, "headfoot")

    prompt_files = sorted([f for f in os.listdir(prompt_dir) if f.endswith(".txt")])
    headfoot_files = sorted([f for f in os.listdir(headfoot_dir) if f.endswith(".py")])

    print("\n📝 可用提示词模板：")
    for idx, name in enumerate(prompt_files, 1):
        print(f"   {idx}. {name}")
    prompt_index = prompt_choice(f"请选择提示词模板编号 (1-{len(prompt_files)}): ", prompt_files)
    prompt_path = os.path.join(prompt_dir, prompt_files[prompt_index - 1])

    print("\n🎨 可用头尾模板：")
    for idx, name in enumerate(headfoot_files, 1):
        print(f"   {idx}. {name}")
    default_idx = headfoot_files.index("headfoot.py") + 1 if "headfoot.py" in headfoot_files else None
    headfoot_prompt = f"请选择头尾模板编号 (1-{len(headfoot_files)})，直接回车使用默认 headfoot.py: "
    headfoot_index = prompt_choice(headfoot_prompt, headfoot_files, allow_empty=True)
    if headfoot_index == -1 and default_idx:
        headfoot_path = os.path.join(headfoot_dir, "headfoot.py")
        print("✅ 已选择默认头尾模板 headfoot.py")
    elif headfoot_index == -1 and not default_idx:
        headfoot_path = os.path.join(headfoot_dir, headfoot_files[0])
        print(f"⚠️ 未找到默认模板，已自动选择 {headfoot_files[0]}")
    else:
        headfoot_path = os.path.join(headfoot_dir, headfoot_files[headfoot_index - 1])
        print(f"✅ 已选择头尾模板 {headfoot_files[headfoot_index - 1]}")

    return prompt_path, headfoot_path


def setup_ai_model(manager: ContentManager):
    manager.model_choice = "openaiapi"
    if not manager.openaiapi_config:
        raise RuntimeError("未找到 OpenAI API 配置文件，请先完成配置。")

    manager.init_openaiapi_client()
    service = manager.openaiapi_service

    platforms = service.get_available_platforms()
    if not platforms:
        raise RuntimeError("未获取到可用平台，请检查配置。")

    print("\n🤖 可用平台：")
    for idx, platform in enumerate(platforms, 1):
        info = service.get_platform_info(platform)
        print(f"   {idx}. {info.get('name', platform)}")
    platform_idx = prompt_choice(f"请选择平台编号 (1-{len(platforms)}): ", platforms)
    selected_platform = platforms[platform_idx - 1]
    service.set_platform(selected_platform)

    models = service.get_platform_models(selected_platform)
    if not models:
        raise RuntimeError("所选平台没有可用模型，请检查配置。")

    print(f"\n🔧 {service.get_platform_info(selected_platform).get('name', selected_platform)} 可用模型：")
    for idx, model in enumerate(models, 1):
        print(f"   {idx}. {model}")
    model_idx = prompt_choice(f"请选择模型编号 (1-{len(models)}): ", models)
    service.set_model(models[model_idx - 1])

    if manager.use_concurrent:
        print(f"✅ 已启用并发处理，最大并发数: {manager.concurrent_tasks}")
    else:
        print("ℹ️ 当前使用串行处理。")


def prompt_account_selection(account_manager: AccountManager) -> List[int]:
    account_manager.display_accounts()
    while True:
        raw = input("\n请输入要使用的账号ID（多个用逗号分隔，如 1,2）: ").strip()
        account_ids = account_manager.parse_account_ids(raw)
        if account_ids:
            return account_ids
        print("❌ 请至少选择一个有效的账号。")


def prompt_mass_send_settings() -> Tuple[bool, str]:
    while True:
        choice = input("\n是否启用群发？1-群发，2-普通发布: ").strip()
        if choice in ("1", "2"):
            mass_send = (choice == "1")
            break
        print("❌ 请输入 1 或 2。")

    mass_send_type = None
    if mass_send:
        print("\n请选择群发模式：")
        print("1 - 定时群发（每日20:30）")
        print("2 - 立即群发（按间隔轮流群发）")
        print("3 - 混合定时（间隔发布 + 20:30 群发）")
        while True:
            option = input("请输入群发方式 (1/2/3): ").strip()
            if option == "1":
                mass_send_type = "timed"
                break
            if option == "2":
                mass_send_type = "immediate"
                break
            if option == "3":
                mass_send_type = "hybrid_timed"
                break
            print("❌ 请输入 1、2 或 3。")
    return mass_send, mass_send_type


def wait_until_accounts_available(scheduler: PublishScheduler, account_ids: List[int], wait_seconds: int = 60) -> int:
    """轮询账号可用性，必要时等待。"""
    while True:
        for account_id in account_ids:
            if scheduler._is_account_available(account_id):
                return account_id
        print(f"⏳ 当前账号均不可用，将等待 {wait_seconds} 秒后重试...")
        time.sleep(wait_seconds)


def generate_articles(manager: ContentManager, target_count: int) -> List[str]:
    """用于群发模式：预先生成指定数量的文章。"""
    saved_paths: List[str] = []
    consecutive_failures = 0

    while len(saved_paths) < target_count:
        status, data = manager.create_single_article()
        if status == "success":
            saved_paths.append(data["saved_path"])
            print(f"✅ 已生成第 {len(saved_paths)}/{target_count} 篇: {os.path.basename(data['saved_path'])}")
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            error_msg = data.get("error", "未知错误")
            print(f"❌ 生成失败：{error_msg}")
            if consecutive_failures >= 5:
                raise RuntimeError("连续生成失败次数过多，请检查源文件或模型配置。")

    return saved_paths


def sequential_publish_flow(
    manager: ContentManager,
    scheduler: PublishScheduler,
    account_ids: List[int],
    total_articles: int,
    articles_per_group: int,
    interval_seconds: int,
):
    produced_success = 0
    published_success = 0
    group_index = 0
    consecutive_failures = 0
    account_index = 0  # 账号轮换索引

    print("\n🚀 开始二合一串行流程（写完立刻发布）")
    print(f"计划生成并发布 {total_articles} 篇文章，组内篇数: {articles_per_group}，组间间隔: {interval_seconds} 秒")

    while published_success < total_articles:
        remaining = total_articles - published_success
        group_target = min(articles_per_group, remaining)
        group_index += 1
        print(f"\n=== 第 {group_index} 轮：准备生成 {group_target} 篇文章 ===")

        group_paths: List[str] = []
        while len(group_paths) < group_target and produced_success < total_articles:
            status, data = manager.create_single_article()
            if status == "success":
                saved_path = data["saved_path"]
                produced_success += 1
                group_paths.append(saved_path)
                article_name = os.path.basename(saved_path)
                print(f"📝 已生成第 {produced_success}/{total_articles} 篇：{article_name}")
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                error_msg = data.get("error", "未知错误")
                print(f"❌ 生成失败：{error_msg}")
                if consecutive_failures >= 5 and not group_paths:
                    print("⚠️ 连续失败次数过多，流程已停止。")
                    return

        if not group_paths:
            print("⚠️ 本轮未生成有效文章，流程终止。")
            return

        # 轮换选择账号：按顺序使用账号，如果当前账号不可用则等待或选择下一个
        account_id = None
        attempts = 0
        max_attempts = len(account_ids) * 2  # 最多尝试所有账号两轮
        
        while account_id is None and attempts < max_attempts:
            # 从当前索引开始轮换选择账号
            current_account_id = account_ids[account_index % len(account_ids)]
            account_index += 1  # 更新索引，下次使用下一个账号
            
            if scheduler._is_account_available(current_account_id):
                account_id = current_account_id
                break
            
            attempts += 1
            # 如果所有账号都不可用，等待后重试
            if attempts % len(account_ids) == 0:
                print(f"⏳ 当前账号均不可用，将等待 60 秒后重试...")
                time.sleep(60)
        
        # 如果仍然没有可用账号，使用原来的等待逻辑
        if account_id is None:
            account_id = wait_until_accounts_available(scheduler, account_ids)
        
        # 获取账号信息以显示名称
        account = scheduler.account_manager.get_account_by_id(account_id)
        account_name = account['name'] if account else f"账号{account_id}"
        print(f"📤 使用账号 {account_name} (ID: {account_id}) 发布 {len(group_paths)} 篇文章...")

        publish_success = scheduler.publish_articles(group_paths, account_id, mass_send_notify=False)
        if publish_success:
            published_success += len(group_paths)
            print(f"🎉 已成功发布 {published_success}/{total_articles} 篇文章")
        else:
            print("⚠️ 发布失败，请登录后台检查。")

        if published_success >= total_articles:
            break

        next_time = datetime.now() + timedelta(seconds=interval_seconds)
        print(f"⏳ 等待 {interval_seconds} 秒后继续（预计 {next_time.strftime('%H:%M:%S')}）")
        time.sleep(interval_seconds)

    print("\n✅ 串行发布流程完成")


def main():
    print("\n=== AI 写作与公众号发布二合一工具 ===")

    base_folder = prompt_directory("\n📁 请输入原始文章所在文件夹路径: ")
    prompt_path, headfoot_path = choose_prompt_and_headfoot()

    manager = ContentManager(base_folder, prompt_file_path=prompt_path, headfoot_file_path=headfoot_path)
    if not manager.document_processor.all_available_files:
        print("❌ 未找到可用的 Markdown 文件，程序退出。")
        return

    total_articles = prompt_positive_int(
        f"\n📊 本次计划发布多少篇文章？（最多可重复使用 {len(manager.document_processor.all_available_files)} 篇源文件）: "
    )
    manager.total_articles = total_articles
    manager.calculate_total_rounds()

    setup_ai_model(manager)

    account_manager = AccountManager()
    account_ids = prompt_account_selection(account_manager)

    interval_seconds = prompt_positive_int("\n⏱️ 请输入每组发布间隔（秒）: ", min_value=1)
    articles_per_group = prompt_positive_int("\n🗂️ 请输入每组发布的图文数 (1~8): ", min_value=1, max_value=8)

    mass_send, mass_send_type = prompt_mass_send_settings()
    scheduler = PublishScheduler(account_manager)

    try:
        if mass_send:
            print("\n⚠️ 群发模式下，将先完成全部文章生成，再进入原有发布调度流程。")
            generated_paths = generate_articles(manager, total_articles)
            print("\n✅ 所有文章已生成，开始调用原发布调度器执行群发计划。")
            scheduler.run_publish_schedule(
                article_dir=manager.output_folder,
                interval_seconds=interval_seconds,
                total_articles=len(generated_paths),
                articles_per_publish=articles_per_group,
                account_ids=account_ids,
                mass_send_notify=True,
                mass_send_type=mass_send_type,
            )
        else:
            sequential_publish_flow(
                manager=manager,
                scheduler=scheduler,
                account_ids=account_ids,
                total_articles=total_articles,
                articles_per_group=articles_per_group,
                interval_seconds=interval_seconds,
            )
    except KeyboardInterrupt:
        print("\n⛔ 已手动中断流程。")
    except Exception as exc:
        print(f"\n❌ 程序出现异常：{exc}")
    finally:
        print("\n=== 本次任务总结 ===")
        print(f"✍️ 写作成功：{manager.success_articles} 篇，失败：{manager.failed_articles} 篇")
        print(f"📤 发布成功：{scheduler.success_articles} 篇，失败：{scheduler.failed_articles} 篇")
        print(f"📁 最新输出目录：{manager.output_folder}")


if __name__ == "__main__":
    main()


