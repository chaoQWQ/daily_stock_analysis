# -*- coding: utf-8 -*-
"""
===================================
Telegram 监听主调度器
===================================

职责：
1. 协调 客户端 → 过滤器 → 分析器 → 推送 的完整流程
2. 管理消息队列和汇总推送
3. 控制频率和限流
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Callable, Dict, Any

from telethon.tl.types import Channel, Message

from config import get_config
from notification import NotificationService

from .client import TelegramClientWrapper
from .message_filter import MessageFilter, ImpactLevel, FilterResult
from .news_analyzer import TelegramNewsAnalyzer, NewsImpactResult

logger = logging.getLogger(__name__)


@dataclass
class QueuedMessage:
    """队列中的消息"""
    text: str
    channel_title: str
    channel_id: int
    timestamp: datetime
    filter_result: FilterResult
    analysis_result: Optional[NewsImpactResult] = None


@dataclass
class MonitorStats:
    """监控统计"""
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone(timedelta(hours=8))))
    total_messages: int = 0
    high_impact_count: int = 0
    medium_impact_count: int = 0
    low_impact_count: int = 0
    excluded_count: int = 0
    push_count: int = 0
    error_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        runtime = datetime.now(timezone(timedelta(hours=8))) - self.start_time
        return {
            'runtime_seconds': int(runtime.total_seconds()),
            'total_messages': self.total_messages,
            'high_impact_count': self.high_impact_count,
            'medium_impact_count': self.medium_impact_count,
            'low_impact_count': self.low_impact_count,
            'excluded_count': self.excluded_count,
            'push_count': self.push_count,
            'error_count': self.error_count,
        }


class TelegramMonitor:
    """
    Telegram 频道监听主调度器

    完整流程：
    1. 监听 Telegram 频道消息
    2. 过滤器筛选相关消息
    3. AI 分析消息影响
    4. 实时推送高影响消息，汇总推送中等影响消息

    使用方式：
        monitor = TelegramMonitor()
        await monitor.start()
        await monitor.run()
    """

    def __init__(
        self,
        channel_ids: Optional[List[int]] = None,
        summary_interval_minutes: int = 60,
        max_queue_size: int = 100,
        hourly_push_limit: int = 20,
        debug: bool = False
    ):
        """
        初始化监听器

        Args:
            channel_ids: 要监听的频道 ID 列表（默认从配置读取）
            summary_interval_minutes: 汇总推送间隔（分钟）
            max_queue_size: 消息队列最大长度
            hourly_push_limit: 每小时最大推送次数（防刷屏）
            debug: 调试模式
        """
        config = get_config()

        self._channel_ids = channel_ids or config.telegram_channels
        self._summary_interval = summary_interval_minutes
        self._max_queue_size = max_queue_size
        self._hourly_limit = hourly_push_limit
        self._debug = debug

        # 初始化组件
        self._client: Optional[TelegramClientWrapper] = None
        self._filter = MessageFilter()
        self._analyzer = TelegramNewsAnalyzer()
        self._notifier = NotificationService()

        # 消息队列（用于汇总推送）
        self._message_queue: deque = deque(maxlen=max_queue_size)

        # 推送频率控制
        self._push_timestamps: List[datetime] = []

        # 统计信息
        self._stats = MonitorStats()

        # 运行状态
        self._running = False
        self._summary_task: Optional[asyncio.Task] = None

        logger.info(
            f"Telegram 监听器初始化完成: "
            f"频道数={len(self._channel_ids)}, "
            f"汇总间隔={summary_interval_minutes}分钟"
        )

    async def start(self) -> bool:
        """
        启动监听器

        Returns:
            是否成功启动
        """
        if self._running:
            logger.warning("监听器已在运行")
            return True

        try:
            # 初始化 Telegram 客户端
            self._client = TelegramClientWrapper()
            success = await self._client.start()

            if not success:
                logger.error("Telegram 客户端启动失败")
                return False

            # 订阅频道
            subscribed = await self._client.subscribe_channels(
                self._channel_ids,
                self._on_new_message
            )

            if subscribed == 0:
                logger.error("没有成功订阅任何频道")
                await self._client.stop()
                return False

            self._running = True
            logger.info(f"监听器启动成功，已订阅 {subscribed} 个频道")

            return True

        except Exception as e:
            logger.error(f"启动监听器失败: {e}")
            return False

    async def run(self):
        """
        运行监听器（阻塞式）

        包括：
        1. 消息监听循环
        2. 定时汇总推送任务
        """
        if not self._running:
            logger.error("监听器未启动")
            return

        # 启动汇总推送定时任务
        self._summary_task = asyncio.create_task(self._summary_loop())

        logger.info("开始监听消息...")

        try:
            # 运行 Telegram 客户端
            await self._client.run_forever()
        finally:
            await self.stop()

    async def stop(self):
        """停止监听器"""
        if not self._running:
            return

        self._running = False

        # 取消汇总任务
        if self._summary_task:
            self._summary_task.cancel()
            try:
                await self._summary_task
            except asyncio.CancelledError:
                pass

        # 停止客户端
        if self._client:
            await self._client.stop()

        # 输出统计
        stats = self._stats.to_dict()
        logger.info(f"监听器已停止，统计: {stats}")

    async def _on_new_message(self, message: Message, channel: Channel):
        """
        新消息回调

        处理流程：
        1. 提取消息文本
        2. 过滤器判定
        3. 根据影响级别处理
        """
        try:
            # 提取文本
            text = message.text or message.message or ""
            if not text:
                return

            self._stats.total_messages += 1

            channel_title = getattr(channel, 'title', str(channel.id))
            channel_id = channel.id

            if self._debug:
                logger.debug(f"[{channel_title}] 收到消息: {text[:100]}...")

            # 过滤
            filter_result = self._filter.filter_message(text)

            # 根据影响级别处理
            if filter_result.impact_level == ImpactLevel.EXCLUDED:
                self._stats.excluded_count += 1
                if self._debug:
                    logger.debug(f"消息被排除: {filter_result.reason}")
                return

            elif filter_result.impact_level == ImpactLevel.HIGH:
                self._stats.high_impact_count += 1
                await self._handle_high_impact(
                    text, channel_title, channel_id, filter_result
                )

            elif filter_result.impact_level == ImpactLevel.MEDIUM:
                self._stats.medium_impact_count += 1
                await self._handle_medium_impact(
                    text, channel_title, channel_id, filter_result
                )

            else:  # LOW
                self._stats.low_impact_count += 1
                if self._debug:
                    logger.debug(f"低影响消息: {text[:50]}...")

        except Exception as e:
            self._stats.error_count += 1
            logger.error(f"处理消息时发生错误: {e}")

    async def _handle_high_impact(
        self,
        text: str,
        channel_title: str,
        channel_id: int,
        filter_result: FilterResult
    ):
        """
        处理高影响消息：立即分析并推送
        """
        logger.info(f"🔴 高影响消息 [{channel_title}]: {text[:80]}...")
        logger.info(f"   关键词: {filter_result.matched_keywords}")

        # 检查推送限流
        if not self._can_push():
            logger.warning("达到每小时推送上限，消息加入队列")
            self._add_to_queue(text, channel_title, channel_id, filter_result)
            return

        # AI 分析
        analysis = await asyncio.to_thread(
            self._analyzer.analyze,
            text,
            channel_title,
            filter_result.category
        )

        # 格式化推送内容
        notification = self._format_high_impact_notification(
            text, channel_title, filter_result, analysis
        )

        # 推送
        success = await self._push_notification(notification)

        if success:
            self._stats.push_count += 1
            logger.info("高影响消息已推送")

    async def _handle_medium_impact(
        self,
        text: str,
        channel_title: str,
        channel_id: int,
        filter_result: FilterResult
    ):
        """
        处理中等影响消息：加入队列，定时汇总推送
        """
        logger.info(f"🟡 中等影响消息 [{channel_title}]: {text[:50]}...")

        self._add_to_queue(text, channel_title, channel_id, filter_result)

    def _add_to_queue(
        self,
        text: str,
        channel_title: str,
        channel_id: int,
        filter_result: FilterResult
    ):
        """添加消息到队列"""
        queued = QueuedMessage(
            text=text,
            channel_title=channel_title,
            channel_id=channel_id,
            timestamp=datetime.now(timezone(timedelta(hours=8))),
            filter_result=filter_result
        )
        self._message_queue.append(queued)

    async def _summary_loop(self):
        """汇总推送定时循环"""
        interval_seconds = self._summary_interval * 60

        while self._running:
            try:
                await asyncio.sleep(interval_seconds)

                if self._message_queue:
                    await self._push_summary()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"汇总推送失败: {e}")

    async def _push_summary(self):
        """推送汇总消息"""
        if not self._message_queue:
            return

        # 取出所有待推送消息
        messages = list(self._message_queue)
        self._message_queue.clear()

        logger.info(f"准备推送汇总: {len(messages)} 条消息")

        # 格式化汇总内容
        notification = self._format_summary_notification(messages)

        # 推送
        success = await self._push_notification(notification)

        if success:
            self._stats.push_count += 1
            logger.info("汇总推送成功")

    def _format_high_impact_notification(
        self,
        text: str,
        channel_title: str,
        filter_result: FilterResult,
        analysis: NewsImpactResult
    ) -> str:
        """格式化高影响消息通知"""
        bj_time = datetime.now(timezone(timedelta(hours=8))).strftime('%H:%M')

        lines = [
            f"## 🚨 高影响消息提醒",
            f"",
            f"**时间**: {bj_time}",
            f"**来源**: {channel_title}",
            f"**分类**: {filter_result.category}",
            f"**关键词**: {', '.join(filter_result.matched_keywords[:5])}",
            f"",
            f"### 原文摘要",
            f"> {text[:300]}{'...' if len(text) > 300 else ''}",
            f"",
        ]

        if analysis.success:
            lines.extend([
                f"### AI 分析",
                f"",
                analysis.format_notification(),
                f"",
            ])

        return '\n'.join(lines)

    def _format_summary_notification(self, messages: List[QueuedMessage]) -> str:
        """格式化汇总通知"""
        bj_time = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')

        # 按分类分组
        by_category: Dict[str, List[QueuedMessage]] = {}
        for msg in messages:
            category = msg.filter_result.category or "其他"
            if category not in by_category:
                by_category[category] = []
            by_category[category].append(msg)

        lines = [
            f"## 📊 时政经济汇总",
            f"",
            f"**时间**: {bj_time}",
            f"**消息数**: {len(messages)} 条",
            f"",
        ]

        for category, msgs in by_category.items():
            lines.append(f"### {category} ({len(msgs)}条)")
            lines.append("")

            for i, msg in enumerate(msgs[:5], 1):  # 每类最多显示5条
                time_str = msg.timestamp.strftime('%H:%M')
                summary = msg.text[:80] + ('...' if len(msg.text) > 80 else '')
                lines.append(f"{i}. [{time_str}] {summary}")

            if len(msgs) > 5:
                lines.append(f"   ...还有 {len(msgs) - 5} 条")

            lines.append("")

        return '\n'.join(lines)

    async def _push_notification(self, content: str) -> bool:
        """推送通知"""
        try:
            # 记录推送时间
            self._push_timestamps.append(
                datetime.now(timezone(timedelta(hours=8)))
            )

            # 截断过长内容
            if len(content) > 3800:
                content = content[:3800] + "\n...(已截断)"

            # 推送到企业微信
            if self._notifier.is_available():
                success = self._notifier.send_to_wechat(content)
                return success
            else:
                logger.warning("通知服务不可用")
                return False

        except Exception as e:
            logger.error(f"推送失败: {e}")
            return False

    def _can_push(self) -> bool:
        """检查是否可以推送（限流控制）"""
        now = datetime.now(timezone(timedelta(hours=8)))
        one_hour_ago = now - timedelta(hours=1)

        # 清理过期的推送记录
        self._push_timestamps = [
            ts for ts in self._push_timestamps
            if ts > one_hour_ago
        ]

        # 检查是否达到上限
        return len(self._push_timestamps) < self._hourly_limit

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = self._stats.to_dict()
        stats['queue_size'] = len(self._message_queue)
        stats['subscribed_channels'] = (
            self._client.subscribed_channels if self._client else []
        )
        return stats


async def run_telegram_monitor(
    summary_interval: int = 60,
    debug: bool = False
):
    """
    运行 Telegram 监听器的便捷函数

    Args:
        summary_interval: 汇总推送间隔（分钟）
        debug: 调试模式
    """
    config = get_config()

    if not config.telegram_enabled:
        logger.warning("Telegram 监听功能未启用 (TELEGRAM_ENABLED=false)")
        return

    if not config.telegram_channels:
        logger.error("未配置监听频道 (TELEGRAM_CHANNELS)")
        return

    monitor = TelegramMonitor(
        summary_interval_minutes=summary_interval,
        debug=debug
    )

    success = await monitor.start()
    if not success:
        logger.error("监听器启动失败")
        return

    try:
        await monitor.run()
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        await monitor.stop()
