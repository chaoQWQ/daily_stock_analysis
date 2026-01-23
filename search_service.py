# -*- coding: utf-8 -*-
"""
===================================
A股自选股智能分析系统 - 搜索服务模块
===================================

职责：
1. 提供统一的新闻搜索接口
2. 支持 Tavily 和 SerpAPI 两种搜索引擎
3. 多 Key 负载均衡和故障转移
4. 搜索结果缓存和格式化
"""

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional
from itertools import cycle

logger = logging.getLogger(__name__)


# ========== 国际政经人物-行业映射表 ==========
# 用于根据行业查找相关的国际影响人物

GLOBAL_INFLUENCERS = {
    # 人物 -> 关联行业/关键词
    '马斯克': ['新能源汽车', '特斯拉', '电动车', 'AI', '脑机接口', 'SpaceX', '星链', '推特', 'X'],
    '特朗普': ['关税', '中美贸易', '半导体', '芯片', '制裁', '美股', '加密货币'],
    '巴菲特': ['投资', '银行', '保险', '消费', '可口可乐', '苹果'],
    '黄仁勋': ['AI', '英伟达', 'GPU', '芯片', '算力', '人工智能'],
    '马化腾': ['腾讯', '游戏', '社交', '云计算', '微信'],
    '马云': ['阿里巴巴', '电商', '蚂蚁', '金融科技'],
    '雷军': ['小米', '手机', '智能家居', '新能源汽车'],
    '任正非': ['华为', '5G', '芯片', '通信'],
    '比尔盖茨': ['微软', 'AI', '新能源', '医疗'],
    '扎克伯格': ['Meta', '元宇宙', 'VR', '社交'],
}

INDUSTRY_INFLUENCER_MAP = {
    # 行业关键词 -> 相关人物
    '新能源汽车': ['马斯克', '特朗普', '雷军'],
    '汽车': ['马斯克', '雷军'],
    '电动车': ['马斯克', '雷军'],
    '半导体': ['黄仁勋', '特朗普', '任正非'],
    '芯片': ['黄仁勋', '特朗普', '任正非'],
    '人工智能': ['马斯克', '黄仁勋', '比尔盖茨'],
    'AI': ['马斯克', '黄仁勋', '比尔盖茨', '扎克伯格'],
    '互联网': ['马化腾', '马云', '扎克伯格'],
    '游戏': ['马化腾'],
    '电商': ['马云'],
    '通信': ['任正非'],
    '5G': ['任正非'],
    '消费电子': ['雷军', '任正非'],
    '手机': ['雷军', '任正非'],
    '金融': ['巴菲特', '特朗普'],
    '银行': ['巴菲特'],
    '保险': ['巴菲特'],
    '医药': ['比尔盖茨'],
    '元宇宙': ['扎克伯格'],
    '加密货币': ['马斯克', '特朗普'],
    '光伏': ['特朗普'],  # 关税影响
    '锂电池': ['马斯克', '特朗普'],
}


@dataclass
class SearchResult:
    """搜索结果数据类"""
    title: str
    snippet: str  # 摘要
    url: str
    source: str  # 来源网站
    published_date: Optional[str] = None
    
    def to_text(self) -> str:
        """转换为文本格式"""
        date_str = f" ({self.published_date})" if self.published_date else ""
        return f"【{self.source}】{self.title}{date_str}\n{self.snippet}"

    def to_markdown(self) -> str:
        """转换为 Markdown 格式（带链接）"""
        date_str = f" ({self.published_date})" if self.published_date else ""
        # 标题作为链接
        if self.url:
            title_link = f"[{self.title}]({self.url})"
        else:
            title_link = self.title
        return f"**{title_link}**{date_str} - {self.source}\n{self.snippet}"


@dataclass 
class SearchResponse:
    """搜索响应"""
    query: str
    results: List[SearchResult]
    provider: str  # 使用的搜索引擎
    success: bool = True
    error_message: Optional[str] = None
    search_time: float = 0.0  # 搜索耗时（秒）
    
    def to_context(self, max_results: int = 5) -> str:
        """将搜索结果转换为可用于 AI 分析的上下文"""
        if not self.success or not self.results:
            return f"搜索 '{self.query}' 未找到相关结果。"
        
        lines = [f"【{self.query} 搜索结果】（来源：{self.provider}）"]
        for i, result in enumerate(self.results[:max_results], 1):
            lines.append(f"\n{i}. {result.to_text()}")
        
        return "\n".join(lines)


class BaseSearchProvider(ABC):
    """搜索引擎基类"""
    
    def __init__(self, api_keys: List[str], name: str):
        """
        初始化搜索引擎
        
        Args:
            api_keys: API Key 列表（支持多个 key 负载均衡）
            name: 搜索引擎名称
        """
        self._api_keys = api_keys
        self._name = name
        self._key_cycle = cycle(api_keys) if api_keys else None
        self._key_usage: Dict[str, int] = {key: 0 for key in api_keys}
        self._key_errors: Dict[str, int] = {key: 0 for key in api_keys}
    
    @property
    def name(self) -> str:
        return self._name
    
    @property
    def is_available(self) -> bool:
        """检查是否有可用的 API Key"""
        return bool(self._api_keys)
    
    def _get_next_key(self) -> Optional[str]:
        """
        获取下一个可用的 API Key（负载均衡）
        
        策略：轮询 + 跳过错误过多的 key
        """
        if not self._key_cycle:
            return None
        
        # 最多尝试所有 key
        for _ in range(len(self._api_keys)):
            key = next(self._key_cycle)
            # 跳过错误次数过多的 key（超过 3 次）
            if self._key_errors.get(key, 0) < 3:
                return key
        
        # 所有 key 都有问题，重置错误计数并返回第一个
        logger.warning(f"[{self._name}] 所有 API Key 都有错误记录，重置错误计数")
        self._key_errors = {key: 0 for key in self._api_keys}
        return self._api_keys[0] if self._api_keys else None
    
    def _record_success(self, key: str) -> None:
        """记录成功使用"""
        self._key_usage[key] = self._key_usage.get(key, 0) + 1
        # 成功后减少错误计数
        if key in self._key_errors and self._key_errors[key] > 0:
            self._key_errors[key] -= 1
    
    def _record_error(self, key: str) -> None:
        """记录错误"""
        self._key_errors[key] = self._key_errors.get(key, 0) + 1
        logger.warning(f"[{self._name}] API Key {key[:8]}... 错误计数: {self._key_errors[key]}")
    
    @abstractmethod
    def _do_search(self, query: str, api_key: str, max_results: int) -> SearchResponse:
        """执行搜索（子类实现）"""
        pass
    
    def search(self, query: str, max_results: int = 5) -> SearchResponse:
        """
        执行搜索
        
        Args:
            query: 搜索关键词
            max_results: 最大返回结果数
            
        Returns:
            SearchResponse 对象
        """
        api_key = self._get_next_key()
        if not api_key:
            return SearchResponse(
                query=query,
                results=[],
                provider=self._name,
                success=False,
                error_message=f"{self._name} 未配置 API Key"
            )
        
        start_time = time.time()
        try:
            response = self._do_search(query, api_key, max_results)
            response.search_time = time.time() - start_time
            
            if response.success:
                self._record_success(api_key)
                logger.info(f"[{self._name}] 搜索 '{query}' 成功，返回 {len(response.results)} 条结果，耗时 {response.search_time:.2f}s")
            else:
                self._record_error(api_key)
            
            return response
            
        except Exception as e:
            self._record_error(api_key)
            elapsed = time.time() - start_time
            logger.error(f"[{self._name}] 搜索 '{query}' 失败: {e}")
            return SearchResponse(
                query=query,
                results=[],
                provider=self._name,
                success=False,
                error_message=str(e),
                search_time=elapsed
            )


class TavilySearchProvider(BaseSearchProvider):
    """
    Tavily 搜索引擎
    
    特点：
    - 专为 AI/LLM 优化的搜索 API
    - 免费版每月 1000 次请求
    - 返回结构化的搜索结果
    
    文档：https://docs.tavily.com/
    """
    
    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys, "Tavily")
    
    def _do_search(self, query: str, api_key: str, max_results: int) -> SearchResponse:
        """执行 Tavily 搜索"""
        try:
            from tavily import TavilyClient
        except ImportError:
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message="tavily-python 未安装，请运行: pip install tavily-python"
            )
        
        try:
            client = TavilyClient(api_key=api_key)
            
            # 执行搜索（优化：使用advanced深度、限制最近7天）
            response = client.search(
                query=query,
                search_depth="advanced",  # advanced 获取更多结果
                max_results=max_results,
                include_answer=False,
                include_raw_content=False,
                days=7,  # 只搜索最近7天的内容
            )
            
            # 记录原始响应到日志
            logger.info(f"[Tavily] 搜索完成，query='{query}', 返回 {len(response.get('results', []))} 条结果")
            logger.debug(f"[Tavily] 原始响应: {response}")
            
            # 解析结果
            results = []
            for item in response.get('results', []):
                results.append(SearchResult(
                    title=item.get('title', ''),
                    snippet=item.get('content', '')[:500],  # 截取前500字
                    url=item.get('url', ''),
                    source=self._extract_domain(item.get('url', '')),
                    published_date=item.get('published_date'),
                ))
            
            return SearchResponse(
                query=query,
                results=results,
                provider=self.name,
                success=True,
            )
            
        except Exception as e:
            error_msg = str(e)
            # 检查是否是配额问题
            if 'rate limit' in error_msg.lower() or 'quota' in error_msg.lower():
                error_msg = f"API 配额已用尽: {error_msg}"
            
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message=error_msg
            )
    
    @staticmethod
    def _extract_domain(url: str) -> str:
        """从 URL 提取域名作为来源"""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc.replace('www.', '')
            return domain or '未知来源'
        except:
            return '未知来源'


class SerpAPISearchProvider(BaseSearchProvider):
    """
    SerpAPI 搜索引擎
    
    特点：
    - 支持 Google、Bing、百度等多种搜索引擎
    - 免费版每月 100 次请求
    - 返回真实的搜索结果
    
    文档：https://serpapi.com/
    """
    
    def __init__(self, api_keys: List[str]):
        super().__init__(api_keys, "SerpAPI")
    
    def _do_search(self, query: str, api_key: str, max_results: int) -> SearchResponse:
        """执行 SerpAPI 搜索"""
        try:
            from serpapi import GoogleSearch
        except ImportError:
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message="google-search-results 未安装，请运行: pip install google-search-results"
            )
        
        try:
            # 使用百度搜索（对中文股票新闻更友好）
            params = {
                "engine": "baidu",  # 使用百度搜索
                "q": query,
                "api_key": api_key,
            }
            
            search = GoogleSearch(params)
            response = search.get_dict()
            
            # 记录原始响应到日志
            logger.debug(f"[SerpAPI] 原始响应 keys: {response.keys()}")
            
            # 解析结果
            results = []
            organic_results = response.get('organic_results', [])
            
            for item in organic_results[:max_results]:
                results.append(SearchResult(
                    title=item.get('title', ''),
                    snippet=item.get('snippet', '')[:500],
                    url=item.get('link', ''),
                    source=item.get('source', self._extract_domain(item.get('link', ''))),
                    published_date=item.get('date'),
                ))
            
            return SearchResponse(
                query=query,
                results=results,
                provider=self.name,
                success=True,
            )
            
        except Exception as e:
            error_msg = str(e)
            return SearchResponse(
                query=query,
                results=[],
                provider=self.name,
                success=False,
                error_message=error_msg
            )
    
    @staticmethod
    def _extract_domain(url: str) -> str:
        """从 URL 提取域名"""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            return parsed.netloc.replace('www.', '') or '未知来源'
        except:
            return '未知来源'


class SearchService:
    """
    搜索服务
    
    功能：
    1. 管理多个搜索引擎
    2. 自动故障转移
    3. 结果聚合和格式化
    """
    
    def __init__(
        self,
        tavily_keys: Optional[List[str]] = None,
        serpapi_keys: Optional[List[str]] = None,
    ):
        """
        初始化搜索服务
        
        Args:
            tavily_keys: Tavily API Key 列表
            serpapi_keys: SerpAPI Key 列表
        """
        self._providers: List[BaseSearchProvider] = []
        
        # 初始化搜索引擎（按优先级排序）
        # Tavily 优先（免费额度更多，每月 1000 次）
        if tavily_keys:
            self._providers.append(TavilySearchProvider(tavily_keys))
            logger.info(f"已配置 Tavily 搜索，共 {len(tavily_keys)} 个 API Key")
        
        # SerpAPI 作为备选（每月 100 次）
        if serpapi_keys:
            self._providers.append(SerpAPISearchProvider(serpapi_keys))
            logger.info(f"已配置 SerpAPI 搜索，共 {len(serpapi_keys)} 个 API Key")
        
        if not self._providers:
            logger.warning("未配置任何搜索引擎 API Key，新闻搜索功能将不可用")
    
    @property
    def is_available(self) -> bool:
        """检查是否有可用的搜索引擎"""
        return any(p.is_available for p in self._providers)
    
    def _execute_search(self, query: str, max_results: int = 5) -> SearchResponse:
        """
        执行通用搜索（聚合模式：同时使用多个引擎并合并结果）

        Args:
            query: 搜索关键词
            max_results: 最大结果数

        Returns:
            SearchResponse（聚合后的结果）
        """
        logger.info(f"执行搜索: '{query}'")

        all_results = []
        providers_used = []
        seen_urls = set()  # 用于去重

        # 同时使用所有可用的搜索引擎
        for provider in self._providers:
            if not provider.is_available:
                continue

            response = provider.search(query, max_results)

            if response.success and response.results:
                providers_used.append(provider.name)
                # 去重：根据 URL 去重
                for result in response.results:
                    if result.url not in seen_urls:
                        seen_urls.add(result.url)
                        all_results.append(result)
                logger.info(f"[聚合搜索] {provider.name} 返回 {len(response.results)} 条结果")
            else:
                logger.warning(f"[聚合搜索] {provider.name} 搜索失败: {response.error_message}")

        # 如果有结果，返回聚合结果
        if all_results:
            # 按结果数量排序截取
            all_results = all_results[:max_results]
            provider_str = "+".join(providers_used)
            logger.info(f"[聚合搜索] 完成，共 {len(all_results)} 条去重结果，来源: {provider_str}")

            return SearchResponse(
                query=query,
                results=all_results,
                provider=provider_str,
                success=True,
            )

        # 所有引擎都失败
        return SearchResponse(
            query=query,
            results=[],
            provider="None",
            success=False,
            error_message="所有搜索引擎都不可用或搜索失败"
        )

    def search_stock_news(
        self,
        stock_code: str,
        stock_name: str,
        max_results: int = 5,
        focus_keywords: Optional[List[str]] = None
    ) -> SearchResponse:
        """
        搜索股票相关新闻
        
        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            max_results: 最大返回结果数
            focus_keywords: 重点关注的关键词列表
            
        Returns:
            SearchResponse 对象
        """
        # 默认重点关注关键词（基于交易理念）
        if focus_keywords is None:
            focus_keywords = [
                "年报预告", "业绩预告", "业绩快报",  # 业绩相关
                "减持", "增持", "回购",              # 股东动向
                "机构调研", "机构评级",              # 机构动向
                "利好", "利空",                      # 消息面
                "合同", "订单", "中标",              # 业务进展
            ]
        
        # 构建搜索查询（优化搜索效果）
        # 主查询：股票名称 + 核心关键词
        query = f"{stock_name} {stock_code} 股票 最新消息"
        
        return self._execute_search(query, max_results)

    def search_morning_news(self) -> List[Dict]:
        """
        搜索早盘前瞻所需的全方位情报（四大维度）
        
        维度：
        1. 隔夜与海外：美股、中概、A50、汇率、大宗
        2. 国内政策：头版头条、行业政策
        3. 公司消息：公告精选
        4. 市场状态：前日复盘
        
        Returns:
            新闻结果列表
        """
        if not self.is_available:
            return []
            
        # 获取北京时间日期
        from datetime import datetime, timezone, timedelta
        bj_now = datetime.now(timezone(timedelta(hours=8)))
        date_str = bj_now.strftime('%m月%d日')
        
        # 构建四大维度的精准查询
        queries = [
            # 1. 隔夜与海外
            f"隔夜美股收盘 纳斯达克 中国金龙指数 {date_str}",
            f"富时A50期货夜盘收盘 {date_str}",
            f"离岸人民币汇率 美元指数 最新 {date_str}",
            f"国际原油 黄金 铜期货价格 {date_str}",
            
            # 2. 国内政策与宏观
            f"证券报头版头条摘要 {date_str}",
            f"国家发改委 央行 证监会 最新政策 {date_str}",
            f"行业重磅利好政策 {date_str}",
            
            # 3. 公司层面
            f"A股 上市公司 晚间公告精选 利好 利空 {date_str}",
            
            # 4. 市场复盘
            f"昨日A股龙虎榜 机构游资动向 {date_str}"
        ]
        
        all_results = []
        # 使用 execute_search 内部方法，稍微减少 max_results 以免总数过多
        for q in queries:
            resp = self._execute_search(q, max_results=2)
            if resp and resp.results:
                # 为每个结果标记查询来源，方便后续分类（可选）
                all_results.extend(resp.results)
                
        return all_results
    
    def search_stock_events(
        self,
        stock_code: str,
        stock_name: str,
        event_types: Optional[List[str]] = None
    ) -> SearchResponse:
        """
        搜索股票特定事件（年报预告、减持等）

        专门针对交易决策相关的重要事件进行搜索

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            event_types: 事件类型列表

        Returns:
            SearchResponse 对象
        """
        if event_types is None:
            event_types = ["年报预告", "减持公告", "业绩快报"]

        # 构建针对性查询
        event_query = " OR ".join(event_types)
        query = f"{stock_name} ({event_query})"

        logger.info(f"搜索股票事件: {stock_name}({stock_code}) - {event_types}")

        # 依次尝试各个搜索引擎
        for provider in self._providers:
            if not provider.is_available:
                continue

            response = provider.search(query, max_results=5)

            if response.success:
                return response

        return SearchResponse(
            query=query,
            results=[],
            provider="None",
            success=False,
            error_message="事件搜索失败"
        )

    def search_sector_news(
        self,
        stock_name: str,
        industry: str,
        concepts: List[str],
        max_results: int = 3
    ) -> SearchResponse:
        """
        搜索板块/概念相关新闻

        Args:
            stock_name: 股票名称
            industry: 所属行业
            concepts: 概念板块列表
            max_results: 最大结果数

        Returns:
            SearchResponse 对象
        """
        # 构建查询：优先使用概念，其次使用行业
        if concepts:
            # 取前2个概念
            concept_str = " ".join(concepts[:2])
            query = f"{concept_str} 板块 最新政策 利好 2026年"
        elif industry:
            query = f"{industry} 行业 最新政策 利好 2026年"
        else:
            query = f"{stock_name} 板块 行业 动态"

        logger.info(f"[板块搜索] {stock_name}: query='{query}'")

        return self._execute_search(query, max_results)

    def search_global_impact(
        self,
        stock_name: str,
        industry: str,
        concepts: List[str],
        max_results: int = 3
    ) -> SearchResponse:
        """
        搜索国际政经人物对行业的影响

        根据行业/概念匹配相关的国际人物，搜索其最新言行对行业的影响

        Args:
            stock_name: 股票名称
            industry: 所属行业
            concepts: 概念板块列表
            max_results: 最大结果数

        Returns:
            SearchResponse 对象
        """
        # 根据行业/概念找到相关人物
        related_influencers = set()

        # 从行业匹配
        if industry:
            for key, influencers in INDUSTRY_INFLUENCER_MAP.items():
                if key in industry or industry in key:
                    related_influencers.update(influencers)

        # 从概念匹配
        for concept in concepts:
            for key, influencers in INDUSTRY_INFLUENCER_MAP.items():
                if key in concept or concept in key:
                    related_influencers.update(influencers)

        # 如果没有匹配到，使用默认的重要人物
        if not related_influencers:
            related_influencers = {'马斯克', '特朗普'}

        # 取前2个人物构建查询
        influencer_list = list(related_influencers)[:2]
        influencer_str = " ".join(influencer_list)

        # 构建查询
        if industry:
            query = f"{influencer_str} {industry} 影响 政策 最新 2026年"
        else:
            query = f"{influencer_str} 中国 A股 影响 最新 2026年"

        logger.info(f"[国际政经搜索] {stock_name}: 关联人物={influencer_list}, query='{query}'")

        return self._execute_search(query, max_results)

    def search_industry_chain(
        self,
        stock_name: str,
        industry: str,
        max_results: int = 3
    ) -> SearchResponse:
        """
        搜索产业链上下游动态

        Args:
            stock_name: 股票名称
            industry: 所属行业
            max_results: 最大结果数

        Returns:
            SearchResponse 对象
        """
        if not industry:
            return SearchResponse(
                query="",
                results=[],
                provider="None",
                success=False,
                error_message="行业信息缺失，无法搜索产业链"
            )

        # 构建产业链查询
        query = f"{stock_name} {industry} 产业链 上下游 供应链 最新动态 2026年"

        logger.info(f"[产业链搜索] {stock_name}: query='{query}'")

        return self._execute_search(query, max_results)
    
    def search_comprehensive_intel(
        self,
        stock_code: str,
        stock_name: str,
        max_searches: int = 6,
        industry: str = "",
        concepts: Optional[List[str]] = None
    ) -> Dict[str, SearchResponse]:
        """
        多维度情报搜索（增强版：含板块、国际政经、产业链）

        搜索维度：
        1. 最新消息 - 近期新闻动态
        2. 风险排查 - 减持、处罚、利空
        3. 业绩预期 - 年报预告、业绩快报
        4. 板块动态 - 所属板块/概念的政策和消息（新增）
        5. 国际政经 - 马斯克、特朗普等关键人物影响（新增）
        6. 产业链动态 - 上下游供应链消息（新增）

        Args:
            stock_code: 股票代码
            stock_name: 股票名称
            max_searches: 最大搜索次数（默认6次）
            industry: 所属行业（可选）
            concepts: 概念板块列表（可选）

        Returns:
            {维度名称: SearchResponse} 字典
        """
        results = {}
        search_count = 0
        concepts = concepts or []

        # 定义搜索维度（按重要性排序）
        search_dimensions = [
            {
                'name': 'latest_news',
                'query': f"{stock_name} {stock_code} 最新 新闻 2026年1月",
                'desc': '最新消息'
            },
            {
                'name': 'risk_check',
                'query': f"{stock_name} 减持 处罚 利空 风险",
                'desc': '风险排查'
            },
            {
                'name': 'earnings',
                'query': f"{stock_name} 年报预告 业绩预告 业绩快报 2025年报",
                'desc': '业绩预期'
            },
        ]

        # 如果有行业/概念信息，添加新的搜索维度
        if industry or concepts:
            # 板块动态
            search_dimensions.append({
                'name': 'sector_news',
                'type': 'sector',
                'desc': '板块动态'
            })
            # 国际政经影响
            search_dimensions.append({
                'name': 'global_impact',
                'type': 'global',
                'desc': '国际政经'
            })
            # 产业链动态
            if industry:
                search_dimensions.append({
                    'name': 'industry_chain',
                    'type': 'chain',
                    'desc': '产业链动态'
                })

        logger.info(f"开始多维度情报搜索: {stock_name}({stock_code}), 行业={industry}, 概念={concepts}")
        logger.info(f"计划搜索维度: {[d['desc'] for d in search_dimensions[:max_searches]]}")

        # 轮流使用不同的搜索引擎
        provider_index = 0

        for dim in search_dimensions:
            if search_count >= max_searches:
                break

            # 选择搜索引擎（轮流使用）
            available_providers = [p for p in self._providers if p.is_available]
            if not available_providers:
                break

            provider = available_providers[provider_index % len(available_providers)]
            provider_index += 1

            logger.info(f"[情报搜索] {dim['desc']}: 使用 {provider.name}")

            # 根据维度类型执行不同的搜索
            dim_type = dim.get('type', 'query')

            if dim_type == 'sector':
                response = self.search_sector_news(stock_name, industry, concepts, max_results=3)
            elif dim_type == 'global':
                response = self.search_global_impact(stock_name, industry, concepts, max_results=3)
            elif dim_type == 'chain':
                response = self.search_industry_chain(stock_name, industry, max_results=3)
            else:
                response = provider.search(dim['query'], max_results=3)

            results[dim['name']] = response
            search_count += 1

            if response.success:
                logger.info(f"[情报搜索] {dim['desc']}: 获取 {len(response.results)} 条结果")
            else:
                logger.warning(f"[情报搜索] {dim['desc']}: 搜索失败 - {response.error_message}")

            # 短暂延迟避免请求过快
            time.sleep(0.5)

        return results
    
    def format_intel_report(self, intel_results: Dict[str, SearchResponse], stock_name: str) -> str:
        """
        格式化情报搜索结果为报告（增强版：含板块、国际政经、产业链，带链接）

        Args:
            intel_results: 多维度搜索结果
            stock_name: 股票名称

        Returns:
            格式化的情报报告文本（Markdown 格式，标题带链接）
        """
        lines = [f"【{stock_name} 情报搜索结果】"]

        def format_result_item(idx: int, result: SearchResult) -> List[str]:
            """格式化单条搜索结果（标题带链接）"""
            date_str = f" [{result.published_date}]" if result.published_date else ""
            # 标题作为 Markdown 链接
            if result.url:
                title_link = f"[{result.title}]({result.url})"
            else:
                title_link = result.title
            snippet = result.snippet[:100] + "..." if len(result.snippet) > 100 else result.snippet
            return [
                f"  {idx}. {title_link}{date_str}",
                f"     {snippet}"
            ]

        # 最新消息
        if 'latest_news' in intel_results:
            resp = intel_results['latest_news']
            lines.append(f"\n📰 最新消息 (来源: {resp.provider}):")
            if resp.success and resp.results:
                for i, r in enumerate(resp.results[:3], 1):
                    lines.extend(format_result_item(i, r))
            else:
                lines.append("  未找到相关消息")

        # 风险排查
        if 'risk_check' in intel_results:
            resp = intel_results['risk_check']
            lines.append(f"\n⚠️ 风险排查 (来源: {resp.provider}):")
            if resp.success and resp.results:
                for i, r in enumerate(resp.results[:3], 1):
                    lines.extend(format_result_item(i, r))
            else:
                lines.append("  未发现明显风险信号")

        # 业绩预期
        if 'earnings' in intel_results:
            resp = intel_results['earnings']
            lines.append(f"\n📊 业绩预期 (来源: {resp.provider}):")
            if resp.success and resp.results:
                for i, r in enumerate(resp.results[:3], 1):
                    lines.extend(format_result_item(i, r))
            else:
                lines.append("  未找到业绩相关信息")

        # ========== 新增维度 ==========

        # 板块动态
        if 'sector_news' in intel_results:
            resp = intel_results['sector_news']
            lines.append(f"\n🏷️ 板块/概念动态 (来源: {resp.provider}):")
            if resp.success and resp.results:
                for i, r in enumerate(resp.results[:3], 1):
                    lines.extend(format_result_item(i, r))
            else:
                lines.append("  未找到板块相关信息")

        # 国际政经影响
        if 'global_impact' in intel_results:
            resp = intel_results['global_impact']
            lines.append(f"\n🌍 国际政经影响 (来源: {resp.provider}):")
            if resp.success and resp.results:
                for i, r in enumerate(resp.results[:3], 1):
                    lines.extend(format_result_item(i, r))
            else:
                lines.append("  未找到相关国际影响信息")

        # 产业链动态
        if 'industry_chain' in intel_results:
            resp = intel_results['industry_chain']
            lines.append(f"\n🔗 产业链动态 (来源: {resp.provider}):")
            if resp.success and resp.results:
                for i, r in enumerate(resp.results[:3], 1):
                    lines.extend(format_result_item(i, r))
            else:
                lines.append("  未找到产业链相关信息")

        return "\n".join(lines)
    
    def batch_search(
        self,
        stocks: List[Dict[str, str]],
        max_results_per_stock: int = 3,
        delay_between: float = 1.0
    ) -> Dict[str, SearchResponse]:
        """
        批量搜索多只股票新闻
        
        Args:
            stocks: 股票列表 [{"code": "300389", "name": "艾比森"}, ...]
            max_results_per_stock: 每只股票的最大结果数
            delay_between: 每次搜索之间的延迟（秒）
            
        Returns:
            {股票代码: SearchResponse} 字典
        """
        results = {}
        
        for i, stock in enumerate(stocks):
            if i > 0:
                time.sleep(delay_between)
            
            code = stock.get('code', '')
            name = stock.get('name', '')
            
            response = self.search_stock_news(code, name, max_results_per_stock)
            results[code] = response
        
        return results


# === 便捷函数 ===
_search_service: Optional[SearchService] = None


def get_search_service() -> SearchService:
    """获取搜索服务单例"""
    global _search_service
    
    if _search_service is None:
        from config import get_config
        config = get_config()
        
        _search_service = SearchService(
            tavily_keys=config.tavily_api_keys,
            serpapi_keys=config.serpapi_keys,
        )
    
    return _search_service


def reset_search_service() -> None:
    """重置搜索服务（用于测试）"""
    global _search_service
    _search_service = None


if __name__ == "__main__":
    # 测试搜索服务
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
    )
    
    # 手动测试（需要配置 API Key）
    service = get_search_service()
    
    if service.is_available:
        print("=== 测试股票新闻搜索 ===")
        response = service.search_stock_news("300389", "艾比森")
        print(f"搜索状态: {'成功' if response.success else '失败'}")
        print(f"搜索引擎: {response.provider}")
        print(f"结果数量: {len(response.results)}")
        print(f"耗时: {response.search_time:.2f}s")
        print("\n" + response.to_context())
    else:
        print("未配置搜索引擎 API Key，跳过测试")
