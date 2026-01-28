# -*- coding: utf-8 -*-
"""
===================================
Telegram 监听主调度器
===================================

核心逻辑（v2）：
1. 所有消息先入队列（排除广告）
2. 每 N 分钟批量发给 Gemini 分析
3. 只推送有价值的消息（影响程度 >= 阈值）
"""

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any

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
    filter_result: Optional[FilterResult] = None


@dataclass
class MonitorStats:
    """监控统计"""
    start_time: datetime = field(default_factory=lambda: datetime.now(timezone(timedelta(hours=8))))
    total_messages: int = 0
    queued_count: int = 0
    excluded_count: int = 0
    analyzed_count: int = 0
    pushed_count: int = 0
    valuable_count: int = 0  # AI 判定有价值的消息数
    error_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        runtime = datetime.now(timezone(timedelta(hours=8))) - self.start_time
        return {
            'runtime_seconds': int(runtime.total_seconds()),
            'total_messages': self.total_messages,
            'queued_count': self.queued_count,
            'excluded_count': self.excluded_count,
            'analyzed_count': self.analyzed_count,
            'valuable_count': self.valuable_count,
            'pushed_count': self.pushed_count,
            'error_count': self.error_count,
        }


class TelegramMonitor:
    """
    Telegram 频道监听主调度器（v2）

    核心流程：
    1. 收到消息 → 简单过滤（排除广告）→ 入队列
    2. 每 N 分钟 → 批量取出队列 → Gemini 分析 → 筛选有价值的 → 推送

    使用方式：
        monitor = TelegramMonitor(batch_interval_minutes=5)
        await monitor.start()
        await monitor.run()
    """

    # AI 分析影响程度阈值（1-10），>= 此值才推送
    IMPACT_THRESHOLD = 4

    def __init__(
        self,
        channel_ids: Optional[List[int]] = None,
        batch_interval_minutes: int = 5,
        max_queue_size: int = 200,
        hourly_push_limit: int = 30,
        debug: bool = False
    ):
        """
        初始化监听器

        Args:
            channel_ids: 要监听的频道 ID 列表
            batch_interval_minutes: 批量分析间隔（分钟），默认 5 分钟
            max_queue_size: 消息队列最大长度
            hourly_push_limit: 每小时最大推送次数
            debug: 调试模式
        """
        config = get_config()

        self._channel_ids = channel_ids or config.telegram_channels
        self._batch_interval = batch_interval_minutes
        self._max_queue_size = max_queue_size
        self._hourly_limit = hourly_push_limit
        self._debug = debug

        # 初始化组件
        self._client: Optional[TelegramClientWrapper] = None
        self._filter = MessageFilter()
        self._analyzer = TelegramNewsAnalyzer()
        self._notifier = NotificationService()

        # 消息队列
        self._message_queue: deque = deque(maxlen=max_queue_size)

        # 推送频率控制
        self._push_timestamps: List[datetime] = []

        # 统计信息
        self._stats = MonitorStats()

        # 运行状态
        self._running = False
        self._batch_task: Optional[asyncio.Task] = None

        logger.info(
            f"Telegram 监听器初始化完成: "
            f"频道数={len(self._channel_ids)}, "
            f"批量分析间隔={batch_interval_minutes}分钟, "
            f"影响阈值={self.IMPACT_THRESHOLD}"
        )

    async def start(self) -> bool:
        """启动监听器"""
        if self._running:
            logger.warning("监听器已在运行")
            return True

        try:
            self._client = TelegramClientWrapper()
            success = await self._client.start()

            if not success:
                logger.error("Telegram 客户端启动失败")
                return False

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
        """运行监听器（阻塞式）"""
        if not self._running:
            logger.error("监听器未启动")
            return

        # 启动批量分析定时任务
        self._batch_task = asyncio.create_task(self._batch_analysis_loop())

        logger.info("开始监听消息...")

        try:
            await self._client.run_forever()
        finally:
            await self.stop()

    async def stop(self):
        """停止监听器"""
        if not self._running:
            return

        self._running = False

        # 取消批量任务
        if self._batch_task:
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass

        # 停止客户端
        if self._client:
            await self._client.stop()

        stats = self._stats.to_dict()
        logger.info(f"监听器已停止，统计: {stats}")

    async def _on_new_message(self, message: Message, channel: Channel):
        """
        新消息回调

        简单过滤后入队列，不做 AI 分析
        """
        try:
            text = message.text or message.message or ""
            if not text:
                return

            self._stats.total_messages += 1

            channel_title = getattr(channel, 'title', str(channel.id))
            channel_id = channel.id

            # 记录收到的消息
            logger.info(f"[收到 #{self._stats.total_messages}] {channel_title}: {text[:60]}...")

            # 简单过滤（只排除广告/垃圾）
            filter_result = self._filter.filter_message(text)

            if filter_result.impact_level == ImpactLevel.EXCLUDED:
                self._stats.excluded_count += 1
                logger.debug(f"[排除] {filter_result.reason}")
                return

            # 入队列，等待批量分析
            queued = QueuedMessage(
                text=text,
                channel_title=channel_title,
                channel_id=channel_id,
                timestamp=datetime.now(timezone(timedelta(hours=8))),
                filter_result=filter_result
            )
            self._message_queue.append(queued)
            self._stats.queued_count += 1

            logger.debug(f"[入队] 当前队列: {len(self._message_queue)} 条")

        except Exception as e:
            self._stats.error_count += 1
            logger.error(f"处理消息时发生错误: {e}")

    async def _batch_analysis_loop(self):
        """批量分析定时循环"""
        interval_seconds = self._batch_interval * 60

        while self._running:
            try:
                await asyncio.sleep(interval_seconds)

                if self._message_queue:
                    await self._process_batch()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"批量分析失败: {e}")

    async def _process_batch(self):
        """处理一批消息"""
        if not self._message_queue:
            return

        # 取出所有待分析消息
        messages = list(self._message_queue)
        self._message_queue.clear()

        logger.info(f"========== 开始批量分析 {len(messages)} 条消息 ==========")

        # 构建批量分析文本
        batch_text = self._build_batch_text(messages)

        # 调用 AI 批量分析
        analysis_result = await asyncio.to_thread(
            self._analyzer.analyze_batch,
            batch_text,
            len(messages)
        )

        self._stats.analyzed_count += len(messages)

        if not analysis_result.success:
            logger.warning(f"AI 分析失败: {analysis_result.error_message}")
            return

        # 筛选有价值的消息
        valuable_items = [
            item for item in analysis_result.items
            if item.get('impact_magnitude', 0) >= self.IMPACT_THRESHOLD
        ]

        self._stats.valuable_count += len(valuable_items)

        logger.info(f"分析完成: {len(messages)} 条消息, {len(valuable_items)} 条有价值")

        if valuable_items:
            # 格式化并推送
            notification = self._format_batch_notification(valuable_items, len(messages))
            success = await self._push_notification(notification)

            if success:
                self._stats.pushed_count += 1
                logger.info("批量推送成功")

    def _build_batch_text(self, messages: List[QueuedMessage]) -> str:
        """构建批量分析的输入文本"""
        lines = []
        for i, msg in enumerate(messages, 1):
            time_str = msg.timestamp.strftime('%H:%M')
            # 截取每条消息前 200 字符
            text = msg.text[:200] + ('...' if len(msg.text) > 200 else '')
            lines.append(f"[{i}] [{time_str}] {msg.channel_title}: {text}")

        return '\n\n'.join(lines)

    def _format_batch_notification(
        self,
        valuable_items: List[Dict[str, Any]],
        total_count: int
    ) -> str:
        """格式化批量分析推送内容"""
        bj_time = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')

        lines = [
            f"## 📊 时政经济情报汇总",
            f"",
            f"**时间**: {bj_time}",
            f"**分析消息**: {total_count} 条 → 有价值: {len(valuable_items)} 条",
            f"",
            "---",
            "",
        ]

        for item in valuable_items:
            emoji = self._get_impact_emoji(item.get('impact_direction', '中性'))
            magnitude = item.get('impact_magnitude', 0)
            summary = item.get('summary', '')
            sectors = item.get('affected_sectors', [])
            suggestion = item.get('action_suggestion', '')

            lines.append(f"### {emoji} {summary}")
            lines.append(f"")
            lines.append(f"- **影响程度**: {'█' * magnitude}{'░' * (10-magnitude)} {magnitude}/10")
            lines.append(f"- **影响方向**: {item.get('impact_direction', '中性')}")

            if sectors:
                lines.append(f"- **相关板块**: {', '.join(sectors[:5])}")

            if suggestion:
                lines.append(f"- **建议**: {suggestion}")

            lines.append("")

        return '\n'.join(lines)

    def _get_impact_emoji(self, direction: str) -> str:
        """获取影响方向 emoji"""
        return {'利好': '🟢', '利空': '🔴', '中性': '⚪'}.get(direction, '⚪')

    async def _push_notification(self, content: str) -> bool:
        """推送通知"""
        try:
            self._push_timestamps.append(
                datetime.now(timezone(timedelta(hours=8)))
            )

            if len(content) > 3800:
                content = content[:3800] + "\n...(已截断)"

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

        self._push_timestamps = [
            ts for ts in self._push_timestamps
            if ts > one_hour_ago
        ]

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
    summary_interval: int = 5,
    debug: bool = False
):
    """
    运行 Telegram 监听器

    Args:
        summary_interval: 批量分析间隔（分钟），默认 5 分钟
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
        batch_interval_minutes=summary_interval,
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
