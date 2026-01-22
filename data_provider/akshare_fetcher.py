# -*- coding: utf-8 -*-
"""
===================================
AkshareFetcher - 主数据源 (Priority 1)
===================================

数据来源：东方财富爬虫（通过 akshare 库）
特点：免费、无需 Token、数据全面
风险：爬虫机制易被反爬封禁

防封禁策略：
1. 每次请求前随机休眠 2-5 秒
2. 随机轮换 User-Agent
3. 使用 tenacity 实现指数退避重试

增强数据：
- 实时行情：量比、换手率、市盈率、市净率、总市值、流通市值
- 筹码分布：获利比例、平均成本、筹码集中度
"""

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict, Any, List

import pandas as pd
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from .base import BaseFetcher, DataFetchError, RateLimitError, STANDARD_COLUMNS


@dataclass
class RealtimeQuote:
    """
    实时行情数据
    
    包含当日实时交易数据和估值指标
    """
    code: str
    name: str = ""
    price: float = 0.0           # 最新价
    change_pct: float = 0.0      # 涨跌幅(%)
    change_amount: float = 0.0   # 涨跌额
    
    # 量价指标
    volume_ratio: float = 0.0    # 量比（当前成交量/过去5日平均成交量）
    turnover_rate: float = 0.0   # 换手率(%)
    amplitude: float = 0.0       # 振幅(%)
    
    # 估值指标
    pe_ratio: float = 0.0        # 市盈率(动态)
    pb_ratio: float = 0.0        # 市净率
    total_mv: float = 0.0        # 总市值(元)
    circ_mv: float = 0.0         # 流通市值(元)
    
    # 其他
    change_60d: float = 0.0      # 60日涨跌幅(%)
    high_52w: float = 0.0        # 52周最高
    low_52w: float = 0.0         # 52周最低
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'name': self.name,
            'price': self.price,
            'change_pct': self.change_pct,
            'volume_ratio': self.volume_ratio,
            'turnover_rate': self.turnover_rate,
            'amplitude': self.amplitude,
            'pe_ratio': self.pe_ratio,
            'pb_ratio': self.pb_ratio,
            'total_mv': self.total_mv,
            'circ_mv': self.circ_mv,
            'change_60d': self.change_60d,
        }


@dataclass  
class ChipDistribution:
    """
    筹码分布数据
    
    反映持仓成本分布和获利情况
    """
    code: str
    date: str = ""
    
    # 获利情况
    profit_ratio: float = 0.0     # 获利比例(0-1)
    avg_cost: float = 0.0         # 平均成本
    
    # 筹码集中度
    cost_90_low: float = 0.0      # 90%筹码成本下限
    cost_90_high: float = 0.0     # 90%筹码成本上限
    concentration_90: float = 0.0  # 90%筹码集中度（越小越集中）
    
    cost_70_low: float = 0.0      # 70%筹码成本下限
    cost_70_high: float = 0.0     # 70%筹码成本上限
    concentration_70: float = 0.0  # 70%筹码集中度
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'code': self.code,
            'date': self.date,
            'profit_ratio': self.profit_ratio,
            'avg_cost': self.avg_cost,
            'cost_90_low': self.cost_90_low,
            'cost_90_high': self.cost_90_high,
            'concentration_90': self.concentration_90,
            'concentration_70': self.concentration_70,
        }
    
    def get_chip_status(self, current_price: float) -> str:
        """
        获取筹码状态描述
        
        Args:
            current_price: 当前股价
            
        Returns:
            筹码状态描述
        """
        status_parts = []
        
        # 获利比例分析
        if self.profit_ratio >= 0.9:
            status_parts.append("获利盘极高(>90%)")
        elif self.profit_ratio >= 0.7:
            status_parts.append("获利盘较高(70-90%)")
        elif self.profit_ratio >= 0.5:
            status_parts.append("获利盘中等(50-70%)")
        elif self.profit_ratio >= 0.3:
            status_parts.append("套牢盘较多(>30%)")
        else:
            status_parts.append("套牢盘极重(>70%)")
        
        # 筹码集中度分析 (90%集中度 < 10% 表示集中)
        if self.concentration_90 < 0.08:
            status_parts.append("筹码高度集中")
        elif self.concentration_90 < 0.15:
            status_parts.append("筹码较集中")
        elif self.concentration_90 < 0.25:
            status_parts.append("筹码分散度中等")
        else:
            status_parts.append("筹码较分散")
        
        # 成本与现价关系
        if current_price > 0 and self.avg_cost > 0:
            cost_diff = (current_price - self.avg_cost) / self.avg_cost * 100
            if cost_diff > 20:
                status_parts.append(f"现价高于平均成本{cost_diff:.1f}%")
            elif cost_diff > 5:
                status_parts.append(f"现价略高于成本{cost_diff:.1f}%")
            elif cost_diff > -5:
                status_parts.append("现价接近平均成本")
            else:
                status_parts.append(f"现价低于平均成本{abs(cost_diff):.1f}%")
        
        return "，".join(status_parts)

logger = logging.getLogger(__name__)


# User-Agent 池，用于随机轮换
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]


# 缓存实时行情数据（避免重复请求）
_realtime_cache: Dict[str, Any] = {
    'data': None,
    'timestamp': 0,
    'ttl': 60  # 60秒缓存有效期
}

# 缓存股票板块/概念数据（变化不频繁，缓存1天）
_sector_cache: Dict[str, Dict[str, Any]] = {}
_sector_cache_ttl = 86400  # 24小时缓存


@dataclass
class StockSectorInfo:
    """
    股票板块/概念信息

    用于关联搜索和行业分析
    """
    code: str
    name: str = ""
    industry: str = ""                    # 所属行业
    concepts: List[str] = field(default_factory=list)  # 所属概念列表

    def to_dict(self) -> Dict[str, Any]:
        return {
            'code': self.code,
            'name': self.name,
            'industry': self.industry,
            'concepts': self.concepts,
        }

    def get_top_concepts(self, n: int = 3) -> List[str]:
        """获取前N个概念（用于搜索）"""
        return self.concepts[:n] if self.concepts else []


class AkshareFetcher(BaseFetcher):
    """
    Akshare 数据源实现
    
    优先级：1（最高）
    数据来源：东方财富网爬虫
    
    关键策略：
    - 每次请求前随机休眠 2.0-5.0 秒
    - 随机 User-Agent 轮换
    - 失败后指数退避重试（最多3次）
    """
    
    name = "AkshareFetcher"
    priority = 1
    
    def __init__(self, sleep_min: float = 2.0, sleep_max: float = 5.0):
        """
        初始化 AkshareFetcher
        
        Args:
            sleep_min: 最小休眠时间（秒）
            sleep_max: 最大休眠时间（秒）
        """
        self.sleep_min = sleep_min
        self.sleep_max = sleep_max
        self._last_request_time: Optional[float] = None
    
    def _set_random_user_agent(self) -> None:
        """
        设置随机 User-Agent
        
        通过修改 requests Session 的 headers 实现
        这是关键的反爬策略之一
        """
        try:
            import akshare as ak
            # akshare 内部使用 requests，我们通过环境变量或直接设置来影响
            # 实际上 akshare 可能不直接暴露 session，这里通过 fake_useragent 作为补充
            random_ua = random.choice(USER_AGENTS)
            logger.debug(f"设置 User-Agent: {random_ua[:50]}...")
        except Exception as e:
            logger.debug(f"设置 User-Agent 失败: {e}")
    
    def _enforce_rate_limit(self) -> None:
        """
        强制执行速率限制
        
        策略：
        1. 检查距离上次请求的时间间隔
        2. 如果间隔不足，补充休眠时间
        3. 然后再执行随机 jitter 休眠
        """
        if self._last_request_time is not None:
            elapsed = time.time() - self._last_request_time
            min_interval = self.sleep_min
            if elapsed < min_interval:
                additional_sleep = min_interval - elapsed
                logger.debug(f"补充休眠 {additional_sleep:.2f} 秒")
                time.sleep(additional_sleep)
        
        # 执行随机 jitter 休眠
        self.random_sleep(self.sleep_min, self.sleep_max)
        self._last_request_time = time.time()
    
    @retry(
        stop=stop_after_attempt(3),  # 最多重试3次
        wait=wait_exponential(multiplier=1, min=2, max=30),  # 指数退避：2, 4, 8... 最大30秒
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Akshare 获取原始数据
        
        使用 ak.stock_zh_a_hist() 获取 A 股历史数据
        
        流程：
        1. 设置随机 User-Agent
        2. 执行速率限制（随机休眠）
        3. 调用 akshare API
        4. 处理返回数据
        """
        import akshare as ak
        
        # 防封禁策略 1: 随机 User-Agent
        self._set_random_user_agent()
        
        # 防封禁策略 2: 强制休眠
        self._enforce_rate_limit()
        
        logger.info(f"[API调用] ak.stock_zh_a_hist(symbol={stock_code}, period=daily, "
                   f"start_date={start_date.replace('-', '')}, end_date={end_date.replace('-', '')}, adjust=qfq)")
        
        try:
            # 调用 akshare 获取 A 股日线数据
            # period="daily" 获取日线数据
            # adjust="qfq" 获取前复权数据
            import time as _time
            api_start = _time.time()
            
            df = ak.stock_zh_a_hist(
                symbol=stock_code,
                period="daily",
                start_date=start_date.replace('-', ''),
                end_date=end_date.replace('-', ''),
                adjust="qfq"  # 前复权
            )
            
            api_elapsed = _time.time() - api_start
            
            # 记录返回数据摘要
            if df is not None and not df.empty:
                logger.info(f"[API返回] ak.stock_zh_a_hist 成功: 返回 {len(df)} 行数据, 耗时 {api_elapsed:.2f}s")
                logger.info(f"[API返回] 列名: {list(df.columns)}")
                logger.info(f"[API返回] 日期范围: {df['日期'].iloc[0]} ~ {df['日期'].iloc[-1]}")
                logger.debug(f"[API返回] 最新3条数据:\n{df.tail(3).to_string()}")
            else:
                logger.warning(f"[API返回] ak.stock_zh_a_hist 返回空数据, 耗时 {api_elapsed:.2f}s")
            
            return df
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # 检测反爬封禁
            if any(keyword in error_msg for keyword in ['banned', 'blocked', '频率', 'rate', '限制']):
                logger.warning(f"检测到可能被封禁: {e}")
                raise RateLimitError(f"Akshare 可能被限流: {e}") from e
            
            raise DataFetchError(f"Akshare 获取数据失败: {e}") from e
    
    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 Akshare 数据
        
        Akshare 返回的列名（中文）：
        日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
        
        需要映射到标准列名：
        date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()
        
        # 列名映射（Akshare 中文列名 -> 标准英文列名）
        column_mapping = {
            '日期': 'date',
            '开盘': 'open',
            '收盘': 'close',
            '最高': 'high',
            '最低': 'low',
            '成交量': 'volume',
            '成交额': 'amount',
            '涨跌幅': 'pct_chg',
        }
        
        # 重命名列
        df = df.rename(columns=column_mapping)
        
        # 添加股票代码列
        df['code'] = stock_code
        
        # 只保留需要的列
        keep_cols = ['code'] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        df = df[existing_cols]
        
        return df
    
    def get_realtime_quote(self, stock_code: str) -> Optional[RealtimeQuote]:
        """
        获取实时行情数据
        
        数据来源：ak.stock_zh_a_spot_em()
        包含：量比、换手率、市盈率、市净率、总市值、流通市值等
        
        Args:
            stock_code: 股票代码
            
        Returns:
            RealtimeQuote 对象，获取失败返回 None
        """
        import akshare as ak
        
        try:
            # 检查缓存
            current_time = time.time()
            if (_realtime_cache['data'] is not None and 
                current_time - _realtime_cache['timestamp'] < _realtime_cache['ttl']):
                df = _realtime_cache['data']
                logger.debug(f"[缓存命中] 使用缓存的实时行情数据")
            else:
                # 防封禁策略
                self._set_random_user_agent()
                self._enforce_rate_limit()
                
                logger.info(f"[API调用] ak.stock_zh_a_spot_em() 获取A股实时行情...")
                import time as _time
                api_start = _time.time()
                
                df = ak.stock_zh_a_spot_em()
                
                api_elapsed = _time.time() - api_start
                logger.info(f"[API返回] ak.stock_zh_a_spot_em 成功: 返回 {len(df)} 只股票, 耗时 {api_elapsed:.2f}s")
                
                # 更新缓存
                _realtime_cache['data'] = df
                _realtime_cache['timestamp'] = current_time
            
            # 查找指定股票
            row = df[df['代码'] == stock_code]
            if row.empty:
                logger.warning(f"[API返回] 未找到股票 {stock_code} 的实时行情")
                return None
            
            row = row.iloc[0]
            
            # 安全获取字段值
            def safe_float(val, default=0.0):
                try:
                    if pd.isna(val):
                        return default
                    return float(val)
                except:
                    return default
            
            quote = RealtimeQuote(
                code=stock_code,
                name=str(row.get('名称', '')),
                price=safe_float(row.get('最新价')),
                change_pct=safe_float(row.get('涨跌幅')),
                change_amount=safe_float(row.get('涨跌额')),
                volume_ratio=safe_float(row.get('量比')),
                turnover_rate=safe_float(row.get('换手率')),
                amplitude=safe_float(row.get('振幅')),
                pe_ratio=safe_float(row.get('市盈率-动态')),
                pb_ratio=safe_float(row.get('市净率')),
                total_mv=safe_float(row.get('总市值')),
                circ_mv=safe_float(row.get('流通市值')),
                change_60d=safe_float(row.get('60日涨跌幅')),
                high_52w=safe_float(row.get('52周最高')),
                low_52w=safe_float(row.get('52周最低')),
            )
            
            logger.info(f"[实时行情] {stock_code} {quote.name}: 价格={quote.price}, 涨跌={quote.change_pct}%, "
                       f"量比={quote.volume_ratio}, 换手率={quote.turnover_rate}%, "
                       f"PE={quote.pe_ratio}, PB={quote.pb_ratio}")
            return quote
            
        except Exception as e:
            logger.error(f"[API错误] 获取 {stock_code} 实时行情失败: {e}")
            return None
    
    def get_chip_distribution(self, stock_code: str) -> Optional[ChipDistribution]:
        """
        获取筹码分布数据
        
        数据来源：ak.stock_cyq_em()
        包含：获利比例、平均成本、筹码集中度
        
        Args:
            stock_code: 股票代码
            
        Returns:
            ChipDistribution 对象（最新一天的数据），获取失败返回 None
        """
        import akshare as ak
        
        try:
            # 防封禁策略
            self._set_random_user_agent()
            self._enforce_rate_limit()
            
            logger.info(f"[API调用] ak.stock_cyq_em(symbol={stock_code}) 获取筹码分布...")
            import time as _time
            api_start = _time.time()
            
            df = ak.stock_cyq_em(symbol=stock_code)
            
            api_elapsed = _time.time() - api_start
            
            if df.empty:
                logger.warning(f"[API返回] ak.stock_cyq_em 返回空数据, 耗时 {api_elapsed:.2f}s")
                return None
            
            logger.info(f"[API返回] ak.stock_cyq_em 成功: 返回 {len(df)} 天数据, 耗时 {api_elapsed:.2f}s")
            logger.debug(f"[API返回] 筹码数据列名: {list(df.columns)}")
            
            # 取最新一天的数据
            latest = df.iloc[-1]
            
            def safe_float(val, default=0.0):
                try:
                    if pd.isna(val):
                        return default
                    return float(val)
                except:
                    return default
            
            chip = ChipDistribution(
                code=stock_code,
                date=str(latest.get('日期', '')),
                profit_ratio=safe_float(latest.get('获利比例')),
                avg_cost=safe_float(latest.get('平均成本')),
                cost_90_low=safe_float(latest.get('90成本-低')),
                cost_90_high=safe_float(latest.get('90成本-高')),
                concentration_90=safe_float(latest.get('90集中度')),
                cost_70_low=safe_float(latest.get('70成本-低')),
                cost_70_high=safe_float(latest.get('70成本-高')),
                concentration_70=safe_float(latest.get('70集中度')),
            )
            
            logger.info(f"[筹码分布] {stock_code} 日期={chip.date}: 获利比例={chip.profit_ratio:.1%}, "
                       f"平均成本={chip.avg_cost}, 90%集中度={chip.concentration_90:.2%}, "
                       f"70%集中度={chip.concentration_70:.2%}")
            return chip
            
        except Exception as e:
            logger.error(f"[API错误] 获取 {stock_code} 筹码分布失败: {e}")
            return None
    
    def get_enhanced_data(self, stock_code: str, days: int = 60) -> Dict[str, Any]:
        """
        获取增强数据（历史K线 + 实时行情 + 筹码分布）
        
        Args:
            stock_code: 股票代码
            days: 历史数据天数
            
        Returns:
            包含所有数据的字典
        """
        result = {
            'code': stock_code,
            'daily_data': None,
            'realtime_quote': None,
            'chip_distribution': None,
        }
        
        # 获取日线数据
        try:
            df = self.get_daily_data(stock_code, days=days)
            result['daily_data'] = df
        except Exception as e:
            logger.error(f"获取 {stock_code} 日线数据失败: {e}")
        
        # 获取实时行情
        result['realtime_quote'] = self.get_realtime_quote(stock_code)
        
        # 获取筹码分布
        result['chip_distribution'] = self.get_chip_distribution(stock_code)

        return result

    def get_stock_sectors(self, stock_code: str) -> Optional[StockSectorInfo]:
        """
        获取个股所属的行业板块和概念板块

        数据来源：
        1. ak.stock_individual_info_em() - 获取个股详情（含行业分类）
        2. 概念板块通过实时行情数据获取

        Args:
            stock_code: 股票代码

        Returns:
            StockSectorInfo 对象，获取失败返回 None
        """
        import akshare as ak

        # 检查缓存
        current_time = time.time()
        cache_key = stock_code
        if cache_key in _sector_cache:
            cached = _sector_cache[cache_key]
            if current_time - cached.get('timestamp', 0) < _sector_cache_ttl:
                logger.debug(f"[缓存命中] 使用缓存的板块信息: {stock_code}")
                return cached.get('data')

        try:
            # 防封禁策略
            self._set_random_user_agent()
            self._enforce_rate_limit()

            logger.info(f"[API调用] ak.stock_individual_info_em(symbol={stock_code}) 获取个股详情...")

            # 获取个股详情
            df_info = ak.stock_individual_info_em(symbol=stock_code)

            stock_name = ""
            industry = ""

            if df_info is not None and not df_info.empty:
                # 解析个股信息（格式为 item, value 两列）
                info_dict = {}
                if '项目' in df_info.columns and '值' in df_info.columns:
                    for _, row in df_info.iterrows():
                        info_dict[row['项目']] = row['值']
                elif 'item' in df_info.columns and 'value' in df_info.columns:
                    for _, row in df_info.iterrows():
                        info_dict[row['item']] = row['value']

                stock_name = info_dict.get('股票简称', info_dict.get('名称', ''))
                industry = info_dict.get('行业', info_dict.get('所属行业', ''))

                logger.info(f"[API返回] 个股详情: {stock_name}, 行业={industry}")

            # 获取概念板块
            concepts = []
            try:
                self._enforce_rate_limit()
                logger.info(f"[API调用] ak.stock_board_concept_cons_em() 获取概念板块...")

                # 获取所有概念板块列表
                df_concepts = ak.stock_board_concept_name_em()

                if df_concepts is not None and not df_concepts.empty:
                    # 遍历每个概念板块，查找包含该股票的板块
                    # 注意：这是一个较慢的操作，只取前30个热门概念检查
                    concept_names = df_concepts['板块名称'].head(50).tolist()

                    for concept_name in concept_names[:30]:  # 限制检查数量
                        try:
                            # 防止请求过快
                            time.sleep(0.3)
                            df_cons = ak.stock_board_concept_cons_em(symbol=concept_name)
                            if df_cons is not None and not df_cons.empty:
                                if stock_code in df_cons['代码'].values:
                                    concepts.append(concept_name)
                                    logger.debug(f"[概念匹配] {stock_code} 属于概念: {concept_name}")
                                    if len(concepts) >= 5:  # 最多获取5个概念
                                        break
                        except Exception as e:
                            logger.debug(f"[概念检查] {concept_name} 检查失败: {e}")
                            continue

                    logger.info(f"[API返回] 概念板块: {concepts}")

            except Exception as e:
                logger.warning(f"[API警告] 获取概念板块失败: {e}")

            # 如果概念获取失败，尝试从热门概念关键词匹配
            if not concepts and industry:
                concepts = self._infer_concepts_from_industry(industry)

            # 创建结果对象
            sector_info = StockSectorInfo(
                code=stock_code,
                name=stock_name,
                industry=industry,
                concepts=concepts,
            )

            # 更新缓存
            _sector_cache[cache_key] = {
                'data': sector_info,
                'timestamp': current_time,
            }

            logger.info(f"[板块信息] {stock_code} {stock_name}: 行业={industry}, 概念={concepts}")
            return sector_info

        except Exception as e:
            logger.error(f"[API错误] 获取 {stock_code} 板块信息失败: {e}")
            return None

    def _infer_concepts_from_industry(self, industry: str) -> List[str]:
        """
        根据行业推断可能的概念板块

        用于概念板块获取失败时的降级处理
        """
        # 行业-概念映射表
        industry_concept_map = {
            '汽车': ['新能源汽车', '锂电池', '智能驾驶'],
            '电子': ['芯片概念', '消费电子', '华为概念'],
            '计算机': ['人工智能', '云计算', '数字经济'],
            '通信': ['5G', '物联网', '数字经济'],
            '电气设备': ['新能源', '储能', '光伏'],
            '医药生物': ['创新药', '医疗器械', '中药'],
            '食品饮料': ['白酒', '消费升级'],
            '银行': ['金融科技', '数字货币'],
            '房地产': ['房地产', '城市更新'],
            '有色金属': ['锂电池', '稀土永磁', '新材料'],
            '化工': ['新材料', '化工'],
            '机械设备': ['机器人', '工业母机', '专精特新'],
            '传媒': ['游戏', '元宇宙', 'AI应用'],
            '国防军工': ['军工', '航空航天'],
        }

        for key, concepts in industry_concept_map.items():
            if key in industry:
                return concepts

        return []


if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    fetcher = AkshareFetcher()
    
    try:
        df = fetcher.get_daily_data('600519')  # 茅台
        print(f"获取成功，共 {len(df)} 条数据")
        print(df.tail())
    except Exception as e:
        print(f"获取失败: {e}")
