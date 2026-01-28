# -*- coding: utf-8 -*-
"""
===================================
消息过滤器
===================================

职责：
1. 关键词匹配过滤无关消息
2. 判定消息影响级别（高/中/低）
3. 排除广告等垃圾信息
"""

import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set

logger = logging.getLogger(__name__)


class ImpactLevel(Enum):
    """消息影响级别"""
    HIGH = "high"        # 高影响：立即推送
    MEDIUM = "medium"    # 中等影响：汇总推送
    LOW = "low"          # 低影响：记录但不推送
    EXCLUDED = "excluded"  # 排除：广告/垃圾信息


@dataclass
class FilterResult:
    """过滤结果"""
    impact_level: ImpactLevel
    matched_keywords: List[str] = field(default_factory=list)
    category: str = ""  # 分类：货币政策/地缘政治/宏观数据 等
    reason: str = ""    # 判定理由


class MessageFilter:
    """
    消息过滤器

    基于关键词规则过滤和分类消息

    使用方式：
        filter = MessageFilter()
        result = filter.filter_message("央行宣布降准0.5个百分点")
        if result.impact_level == ImpactLevel.HIGH:
            # 立即推送
    """

    # ========== 高影响关键词（立即推送）==========
    HIGH_IMPACT_KEYWORDS = {
        # 货币政策
        "货币政策": ["央行", "降准", "降息", "加息", "LPR", "MLF", "逆回购", "存款准备金",
                    "货币政策", "再贷款", "再贴现", "公开市场操作"],

        # 地缘政治
        "地缘政治": ["制裁", "关税", "贸易战", "出口管制", "实体清单", "脱钩", "断供",
                    "禁运", "封锁", "军事行动", "战争", "冲突升级", "紧张局势"],

        # 重大事件
        "重大事件": ["熔断", "崩盘", "暴跌", "暴涨", "黑天鹅", "闪崩", "跌停潮", "涨停潮",
                    "救市", "国家队", "紧急声明", "突发", "重大利好", "重大利空"],

        # 政策突变
        "政策突变": ["房地产新政", "IPO暂停", "印花税", "T+0", "涨跌幅限制", "注册制",
                    "停牌", "退市", "ST", "*ST"],

        # 关键人物
        "关键人物": ["特朗普", "Trump", "拜登", "Biden", "马斯克", "Musk", "耶伦", "Yellen",
                    "鲍威尔", "Powell", "习近平", "李强", "易纲", "潘功胜"],
    }

    # ========== 中等影响关键词（汇总推送）==========
    MEDIUM_IMPACT_KEYWORDS = {
        # 宏观数据
        "宏观数据": ["GDP", "CPI", "PPI", "PMI", "失业率", "就业", "通胀", "通缩",
                    "零售", "工业增加值", "进出口", "贸易顺差", "贸易逆差", "外汇储备"],

        # 国际市场
        "国际市场": ["美股", "纳斯达克", "道琼斯", "标普500", "港股", "恒生指数",
                    "A50", "富时", "日经", "欧股", "德指", "英股"],

        # 大宗商品
        "大宗商品": ["原油", "黄金", "白银", "铜", "铁矿石", "天然气", "煤炭",
                    "大豆", "玉米", "小麦", "锂", "钴", "镍", "稀土"],

        # 行业政策
        "行业政策": ["新能源", "光伏", "风电", "储能", "电动车", "智能驾驶",
                    "芯片", "半导体", "AI", "人工智能", "大模型", "算力",
                    "医药", "集采", "创新药", "医保", "养老", "教育",
                    "房地产", "限购", "限贷", "保交楼", "城中村"],

        # 外资动向
        "外资动向": ["北向资金", "外资", "QFII", "沪港通", "深港通", "陆股通",
                    "外资净买入", "外资净卖出", "外资流入", "外资流出"],

        # 市场情绪
        "市场情绪": ["牛市", "熊市", "震荡市", "反弹", "回调", "调整", "突破",
                    "支撑", "压力", "放量", "缩量", "主力", "游资"],
    }

    # ========== 排除关键词（垃圾信息）==========
    EXCLUDE_KEYWORDS = [
        # 广告
        "广告", "推广", "代理", "招商", "合作", "VIP", "会员",
        # 引流
        "加群", "私聊", "联系方式", "微信", "QQ群", "电报群",
        # 诈骗
        "稳赚", "包赚", "无风险", "高收益", "内幕", "荐股",
        "老师带队", "跟单", "喊单", "保证金", "杠杆交易",
        # 无关
        "娱乐", "八卦", "明星", "综艺", "电影", "电视剧",
    ]

    # ========== URL 模式（通常是广告）==========
    URL_PATTERN = re.compile(r'https?://\S+')

    def __init__(
        self,
        high_keywords: Optional[dict] = None,
        medium_keywords: Optional[dict] = None,
        exclude_keywords: Optional[List[str]] = None,
        min_message_length: int = 10,
        max_url_count: int = 3
    ):
        """
        初始化过滤器

        Args:
            high_keywords: 高影响关键词字典（可自定义覆盖）
            medium_keywords: 中等影响关键词字典
            exclude_keywords: 排除关键词列表
            min_message_length: 最小消息长度（过滤短消息）
            max_url_count: 最大 URL 数量（超过视为广告）
        """
        self._high_keywords = high_keywords or self.HIGH_IMPACT_KEYWORDS
        self._medium_keywords = medium_keywords or self.MEDIUM_IMPACT_KEYWORDS
        self._exclude_keywords = exclude_keywords or self.EXCLUDE_KEYWORDS
        self._min_length = min_message_length
        self._max_url_count = max_url_count

        # 构建扁平化关键词集合（用于快速查找）
        self._all_high_keywords: Set[str] = set()
        for keywords in self._high_keywords.values():
            self._all_high_keywords.update(keywords)

        self._all_medium_keywords: Set[str] = set()
        for keywords in self._medium_keywords.values():
            self._all_medium_keywords.update(keywords)

        logger.info(
            f"消息过滤器初始化完成: "
            f"高影响关键词 {len(self._all_high_keywords)} 个, "
            f"中等影响关键词 {len(self._all_medium_keywords)} 个, "
            f"排除关键词 {len(self._exclude_keywords)} 个"
        )

    def filter_message(self, text: str) -> FilterResult:
        """
        过滤消息

        Args:
            text: 消息文本

        Returns:
            FilterResult 过滤结果
        """
        if not text:
            return FilterResult(
                impact_level=ImpactLevel.EXCLUDED,
                reason="空消息"
            )

        # 1. 长度检查
        if len(text.strip()) < self._min_length:
            return FilterResult(
                impact_level=ImpactLevel.EXCLUDED,
                reason=f"消息过短（<{self._min_length}字符）"
            )

        # 2. URL 数量检查（过多 URL 可能是广告）
        urls = self.URL_PATTERN.findall(text)
        if len(urls) > self._max_url_count:
            return FilterResult(
                impact_level=ImpactLevel.EXCLUDED,
                reason=f"URL 过多（>{self._max_url_count}）"
            )

        # 3. 排除关键词检查
        for keyword in self._exclude_keywords:
            if keyword in text:
                return FilterResult(
                    impact_level=ImpactLevel.EXCLUDED,
                    matched_keywords=[keyword],
                    reason=f"命中排除关键词: {keyword}"
                )

        # 4. 高影响关键词检查
        high_matches = []
        high_category = ""
        for category, keywords in self._high_keywords.items():
            for keyword in keywords:
                if keyword in text:
                    high_matches.append(keyword)
                    if not high_category:
                        high_category = category

        if high_matches:
            return FilterResult(
                impact_level=ImpactLevel.HIGH,
                matched_keywords=high_matches,
                category=high_category,
                reason=f"命中高影响关键词: {', '.join(high_matches[:3])}"
            )

        # 5. 中等影响关键词检查
        medium_matches = []
        medium_category = ""
        for category, keywords in self._medium_keywords.items():
            for keyword in keywords:
                if keyword in text:
                    medium_matches.append(keyword)
                    if not medium_category:
                        medium_category = category

        if medium_matches:
            return FilterResult(
                impact_level=ImpactLevel.MEDIUM,
                matched_keywords=medium_matches,
                category=medium_category,
                reason=f"命中中等影响关键词: {', '.join(medium_matches[:3])}"
            )

        # 6. 无匹配
        return FilterResult(
            impact_level=ImpactLevel.LOW,
            reason="无匹配关键词"
        )

    def get_statistics(self) -> dict:
        """获取过滤器统计信息"""
        return {
            "high_impact_categories": list(self._high_keywords.keys()),
            "high_impact_keyword_count": len(self._all_high_keywords),
            "medium_impact_categories": list(self._medium_keywords.keys()),
            "medium_impact_keyword_count": len(self._all_medium_keywords),
            "exclude_keyword_count": len(self._exclude_keywords),
        }

    def add_high_keyword(self, category: str, keyword: str):
        """添加高影响关键词"""
        if category not in self._high_keywords:
            self._high_keywords[category] = []
        if keyword not in self._high_keywords[category]:
            self._high_keywords[category].append(keyword)
            self._all_high_keywords.add(keyword)

    def add_medium_keyword(self, category: str, keyword: str):
        """添加中等影响关键词"""
        if category not in self._medium_keywords:
            self._medium_keywords[category] = []
        if keyword not in self._medium_keywords[category]:
            self._medium_keywords[category].append(keyword)
            self._all_medium_keywords.add(keyword)

    def add_exclude_keyword(self, keyword: str):
        """添加排除关键词"""
        if keyword not in self._exclude_keywords:
            self._exclude_keywords.append(keyword)
