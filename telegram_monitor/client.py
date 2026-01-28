# -*- coding: utf-8 -*-
"""
===================================
Telegram 客户端封装 (Telethon)
===================================

职责：
1. 封装 Telethon 客户端连接和认证
2. 订阅指定频道的新消息
3. 自动重连和心跳检测
"""

import asyncio
import logging
from pathlib import Path
from typing import Callable, List, Optional, Awaitable

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import Channel, Message

from config import get_config

logger = logging.getLogger(__name__)


class TelegramClientWrapper:
    """
    Telegram 客户端封装类

    使用 Telethon 库连接 Telegram，监听指定频道消息

    使用方式：
        client = TelegramClientWrapper()
        await client.start()
        await client.subscribe_channels(channels, callback)
        await client.run_forever()
    """

    def __init__(
        self,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        phone: Optional[str] = None,
        session_string: Optional[str] = None,
        session_dir: str = "./data/telegram"
    ):
        """
        初始化 Telegram 客户端

        Args:
            api_id: Telegram API ID（从 https://my.telegram.org/apps 获取）
            api_hash: Telegram API Hash
            phone: 手机号（用于登录验证）
            session_string: StringSession 字符串（用于无交互环境，如 GitHub Actions）
            session_dir: Session 文件存储目录（当 session_string 为空时使用）
        """
        config = get_config()

        self._api_id = api_id or config.telegram_api_id
        self._api_hash = api_hash or config.telegram_api_hash
        self._phone = phone or config.telegram_phone
        self._session_string = session_string or config.telegram_session

        if not self._api_id or not self._api_hash:
            raise ValueError(
                "Telegram API 凭证未配置。请在 .env 中设置 TELEGRAM_API_ID 和 TELEGRAM_API_HASH。\n"
                "获取方式：https://my.telegram.org/apps"
            )

        # 选择 session 类型
        if self._session_string:
            # 使用 StringSession（GitHub Actions 等无交互环境）
            session = StringSession(self._session_string)
            logger.info("使用 StringSession 模式（无交互）")
        else:
            # 使用文件 session（本地开发）
            session_path = Path(session_dir)
            session_path.mkdir(parents=True, exist_ok=True)
            session = str(session_path / "telegram_monitor")
            logger.info(f"使用文件 Session 模式: {session}.session")

        # 初始化 Telethon 客户端
        self._client = TelegramClient(
            session,
            self._api_id,
            self._api_hash,
            system_version="4.16.30-vxCUSTOM"  # 避免被识别为第三方客户端
        )

        self._running = False
        self._subscribed_channels: List[int] = []

        logger.info("Telegram 客户端初始化完成")

    async def start(self) -> bool:
        """
        启动客户端并完成登录

        首次登录需要输入验证码（仅文件 Session 模式）
        StringSession 模式无需交互

        Returns:
            是否成功启动
        """
        try:
            logger.info("正在连接 Telegram...")

            # 根据 session 类型选择启动方式
            if self._session_string:
                # StringSession 模式：已有认证信息，直接连接
                await self._client.connect()

                if not await self._client.is_user_authorized():
                    logger.error("StringSession 无效或已过期，请重新生成")
                    return False
            else:
                # 文件 Session 模式：可能需要手机号验证
                await self._client.start(phone=self._phone)

            me = await self._client.get_me()
            logger.info(f"Telegram 登录成功: {me.first_name} (@{me.username})")

            self._running = True
            return True

        except Exception as e:
            logger.error(f"Telegram 连接失败: {e}")
            return False

    async def subscribe_channels(
        self,
        channel_ids: List[int],
        message_callback: Callable[[Message, Channel], Awaitable[None]]
    ) -> int:
        """
        订阅频道消息

        Args:
            channel_ids: 频道 ID 列表（负数，如 -1001234567890）
            message_callback: 消息回调函数，接收 (message, channel) 参数

        Returns:
            成功订阅的频道数量
        """
        if not self._running:
            logger.error("客户端未启动，无法订阅频道")
            return 0

        subscribed = 0
        valid_channels = []

        for channel_id in channel_ids:
            try:
                # 验证频道是否存在且可访问
                entity = await self._client.get_entity(channel_id)
                if isinstance(entity, Channel):
                    valid_channels.append(channel_id)
                    subscribed += 1
                    logger.info(f"已订阅频道: {entity.title} ({channel_id})")
                else:
                    logger.warning(f"ID {channel_id} 不是一个频道")
            except Exception as e:
                logger.warning(f"无法订阅频道 {channel_id}: {e}")

        if not valid_channels:
            logger.warning("没有有效的频道可订阅")
            return 0

        # 注册消息处理器
        @self._client.on(events.NewMessage(chats=valid_channels))
        async def handler(event):
            try:
                message = event.message
                chat = await event.get_chat()
                logger.info(f"[收到消息] 频道: {chat.title}, 内容: {message.text[:50] if message.text else '(无文本)'}...")
                await message_callback(message, chat)
            except Exception as e:
                logger.error(f"处理消息时发生错误: {e}")

        self._subscribed_channels = valid_channels
        logger.info(f"成功订阅 {subscribed} 个频道")

        return subscribed

    async def run_forever(self):
        """
        持续运行，保持连接

        阻塞式运行，直到收到停止信号
        """
        if not self._running:
            logger.error("客户端未启动")
            return

        logger.info("开始监听消息...")

        try:
            await self._client.run_until_disconnected()
        except asyncio.CancelledError:
            logger.info("监听任务被取消")
        except Exception as e:
            logger.error(f"运行时发生错误: {e}")
        finally:
            await self.stop()

    async def stop(self):
        """停止客户端"""
        if self._running:
            self._running = False
            await self._client.disconnect()
            logger.info("Telegram 客户端已断开")

    @property
    def is_running(self) -> bool:
        """是否正在运行"""
        return self._running

    @property
    def subscribed_channels(self) -> List[int]:
        """已订阅的频道列表"""
        return self._subscribed_channels.copy()

    async def get_channel_info(self, channel_id: int) -> Optional[dict]:
        """
        获取频道信息

        Args:
            channel_id: 频道 ID

        Returns:
            频道信息字典，包含 title、username 等
        """
        try:
            entity = await self._client.get_entity(channel_id)
            if isinstance(entity, Channel):
                return {
                    'id': entity.id,
                    'title': entity.title,
                    'username': entity.username,
                    'participants_count': getattr(entity, 'participants_count', None),
                }
        except Exception as e:
            logger.warning(f"获取频道信息失败 {channel_id}: {e}")
        return None
