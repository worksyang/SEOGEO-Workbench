"""
📱 微信公众号批量发布工具

📝 模块简介:
- 本模块开发目的：实现多个微信公众号账号的文章批量自动化发布，提高运营效率

🔍 主要功能与处理流程:

1. 账号管理 (AccountManager)
- 👥 支持多个公众号账号管理
- 📊 账号信息展示与验证
- 🔑 账号ID解析与验证

2. 发布调度 (PublishScheduler)
- 📚 支持批量发布markdown文章
- ⏱️ 可配置发布时间间隔
- 🔄 多账号轮询发布
- 📁 自动扫描文章目录
- 🚦 发布进度监控
- 📑 支持多图文分组发布

3. 用户交互 (get_user_input)
- 📂 文章目录路径验证
- ⌛ 发布间隔时间设置
- 📊 发布数量控制
- 📑 分组发布设置
- ⚠️ 输入错误处理

4. 异常处理
- 🛡️ 优雅的错误处理机制
- ⌨️ 支持用户中断操作
- 📝 详细的错误日志输出
- 🔄 程序退出保护
"""

import os
from account_manager import AccountManager
from publish_scheduler import PublishScheduler

def get_user_input():
    """获取用户输入"""
    # 获取文章目录
    while True:
        article_dir = input("\n请输入要发布的文章目录路径: ").strip()
        if os.path.exists(article_dir):
            # 获取目录中的文章数量
            articles = [f for f in os.listdir(article_dir) if f.endswith('.md')]
            article_count = len(articles)
            if article_count == 0:
                print("错误: 目录中没有找到.md文件")
                continue
            print(f"\n在目录中找到 {article_count} 篇文章")
            break
        print("错误: 目录不存在，请重新输入")
    
    # 获取每组发布的图文数
    while True:
        try:
            articles_per_group = int(input("\n请输入每组发布的图文数 (1~8之间): ").strip())
            if 1 <= articles_per_group <= 8:
                break
            print("错误: 每组的图文数必须在 1 到 8 之间")
        except ValueError:
            print("错误: 请输入有效的数字")
    # 自动发布全部文章
    total_articles = article_count
    print(f"\n将自动发布当前目录下的全部文章（共 {total_articles} 篇），发布完成即停止")
    
    # 获取组间发布间隔
    while True:
        try:
            interval = int(input("\n请输入组间发布间隔（秒）: ").strip())
            if interval > 0:
                break
            print("错误: 间隔必须大于0")
        except ValueError:
            print("错误: 请输入有效的数字")
    
    return article_dir, interval, total_articles, articles_per_group

def main():
    try:
        # 初始化账号管理器
        account_manager = AccountManager()
        
        # 显示可用账号
        account_manager.display_accounts()
        
        # 获取要使用的账号
        while True:
            account_input = input("\n请输入要使用的账号ID（多个账号用逗号分隔，如: 1,2）: ").strip()
            account_ids = account_manager.parse_account_ids(account_input)
            if account_ids:
                break
            print("错误: 请至少选择一个有效的账号")
        
        # 获取发布参数
        article_dir, interval, total_articles, articles_per_group = get_user_input()

        # 新增：询问是否群发
        while True:
            mass_send_input = input("\n是否要群发？1-群发（不关闭群发通知），2-不群发（关闭群发通知）: ").strip()
            if mass_send_input in ('1', '2'):
                mass_send_notify = (mass_send_input == '1')
                mass_send_type = None
                if mass_send_notify:
                    print("\n请选择群发方式：")
                    print("1-定时群发（每天晚上8:30）")
                    print("2-正常群发（立即开始，按间隔依次群发）")
                    print("3-混合定时（平时间隔发布+8:30定时群发）")
                    while True:
                        type_input = input("请输入群发方式（1-定时群发，2-正常群发，3-混合定时）: ").strip()
                        if type_input == '1':
                            mass_send_type = 'timed'
                            print("\n⚠️  你选择了定时群发：所有账号将在每天晚上8:30发布，每个账号每天只能群发一次。")
                            break
                        elif type_input == '2':
                            mass_send_type = 'immediate'
                            print("\n⚠️  你选择了正常群发：所有账号立即开始群发，每个账号每天只能群发一次，组间间隔为你设置的间隔。")
                            break
                        elif type_input == '3':
                            mass_send_type = 'hybrid_timed'
                            print("\n⚠️  你选择了混合定时：平时按间隔不群发发布，每天8:30暂停间隔发布并进行群发，群发完成后恢复间隔发布。")
                            break
                        else:
                            print("错误: 请输入 1、2 或 3")
                break
            print("错误: 请输入 1 或 2")
        
        # 创建发布调度器
        scheduler = PublishScheduler(account_manager)
        
        # 执行发布计划
        scheduler.run_publish_schedule(
            article_dir=article_dir,                    # 文章目录
            interval_seconds=interval,                  # 组间发布间隔
            total_articles=total_articles,              # 要发布的文章总数
            articles_per_publish=articles_per_group,    # 每组发布的图文数
            account_ids=account_ids,                    # 账号ID列表
            mass_send_notify=mass_send_notify,          # 是否群发
            mass_send_type=mass_send_type               # 群发类型
        )
        
    except KeyboardInterrupt:
        print("\n\n程序被用户中断")
    except Exception as e:
        print(f"\n程序发生错误: {e}")
        import traceback
        print(traceback.format_exc())
    finally:
        input("\n按回车键退出...")

if __name__ == "__main__":
    main()