# -*- coding: utf-8 -*-
"""
===================================
Telegram 频道监听模块
===================================

监听 Telegram 频道的全球时政经济信息，
AI 筛选对 A股 有影响的消息，实时/汇总推送到企业微信/邮件。

使用方式：
    python main.py --telegram-monitor
    python main.py --telegram-monitor --telegram-summary 30
"""

from .monitor import TelegramMonitor
from .message_filter import MessageFilter, ImpactLevel
from .news_analyzer import TelegramNewsAnalyzer

__all__ = [
    'TelegramMonitor',
    'MessageFilter',
    'ImpactLevel',
    'TelegramNewsAnalyzer',
]
