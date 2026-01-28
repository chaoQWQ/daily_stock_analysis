# -*- coding: utf-8 -*-
"""
Telegram Session 生成脚本

用法：
    1. 本地运行此脚本，完成登录验证
    2. 生成的 session 文件上传到服务端
    3. 服务端使用 session 文件无需再次验证

生成文件：
    ./data/telegram/telegram_monitor.session
"""

import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / '.env')


async def main():
    print("=" * 50)
    print("Telegram Session 生成器")
    print("=" * 50)

    # 检查配置
    api_id = os.getenv('TELEGRAM_API_ID')
    api_hash = os.getenv('TELEGRAM_API_HASH')
    phone = os.getenv('TELEGRAM_PHONE')

    if not all([api_id, api_hash, phone]):
        print("❌ 缺少配置，请检查 .env 文件")
        return

    print(f"\nAPI_ID: {api_id}")
    print(f"PHONE: {phone}")

    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("\n❌ 未安装 telethon: pip install telethon")
        return

    # 方式1：生成 session 文件（适合自托管服务器）
    session_dir = Path("./data/telegram")
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "telegram_monitor"

    print(f"\n[方式1] Session 文件")
    print(f"  路径: {session_file}.session")

    client = TelegramClient(
        str(session_file),
        int(api_id),
        api_hash
    )

    print("\n正在连接 Telegram...")
    print("（首次登录会发送验证码到你的 Telegram）\n")

    await client.start(phone=phone)

    me = await client.get_me()
    print(f"\n✅ 登录成功: {me.first_name} (@{me.username})")
    print(f"✅ Session 文件已保存: {session_file}.session")

    # 方式2：生成 StringSession（适合 GitHub Secrets）
    print("\n" + "=" * 50)
    print("[方式2] StringSession（适合 GitHub Secrets）")
    print("=" * 50)

    # 使用 StringSession 重新连接获取字符串
    string_client = TelegramClient(
        StringSession(),
        int(api_id),
        api_hash
    )

    await string_client.start(phone=phone)
    session_string = string_client.session.save()

    print(f"\n将以下字符串保存到 GitHub Secrets（名称: TELEGRAM_SESSION）:\n")
    print("-" * 50)
    print(session_string)
    print("-" * 50)

    print(f"\n字符串长度: {len(session_string)} 字符")

    await client.disconnect()
    await string_client.disconnect()

    print("\n" + "=" * 50)
    print("部署指南")
    print("=" * 50)
    print("""
方式1（自托管服务器）:
  1. 将 ./data/telegram/telegram_monitor.session 上传到服务器
  2. 确保路径一致

方式2（GitHub Actions）:
  1. 在 GitHub 仓库设置中添加 Secret:
     - Name: TELEGRAM_SESSION
     - Value: 上面输出的字符串
  2. 代码中使用 StringSession 加载
""")


if __name__ == "__main__":
    asyncio.run(main())
