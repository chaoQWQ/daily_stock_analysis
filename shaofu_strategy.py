# -*- coding: utf-8 -*-
"""
===================================
ShaoFu 选股策略模块
===================================

从 MyShaoFuStrategy 项目迁移，整合到 daily_stock_analysis

策略逻辑 (通达信优化战法):
1. J_OK: KDJ.J <= 13 (超卖)
2. YANGYIN_OK: 近期阳量显著大于阴量 (资金流入)
3. LQ & MVOK: 流动性充足 (均额>50万) & 市值>50亿
4. GOOD28 & MAX28_OK: 排除近期有"高开低走放量"恶劣形态 & 最大量不是阴线
5. TRIGGER:
   - 模式A: 28天内出现至少3次倍量阳线 (PLRY_CNT)
   - 模式B: 出现"关键K线" (向上跳空/实体阳线 + 放量 + 站上近期高位)
"""

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from pathlib import Path

import pandas as pd
import numpy as np

from mytt import BBI, KDJ, MACD, BOLL, MA

logger = logging.getLogger(__name__)


@dataclass
class StockSignal:
    """选股信号结果"""
    code: str
    name: str
    market_cap: float  # 市值(亿)
    signal: Optional[bool]  # True=买入, False=卖出, None=观望
    close: float
    kdj_j: float
    trigger_reason: str
    trend: int = 0  # 1=上涨, 0=震荡, -1=下跌
    bbi_derivative: float = 0.0
    news_summary: str = ""  # 新闻资讯摘要
    news_sentiment: str = ""  # 资讯情绪: 利好/利空/中性


@dataclass
class ShaoFuStrategyResult:
    """策略执行结果"""
    signals: List[StockSignal] = field(default_factory=list)
    buy_signals: List[StockSignal] = field(default_factory=list)
    sell_signals: List[StockSignal] = field(default_factory=list)
    run_time: datetime = field(default_factory=datetime.now)
    target_name: str = ""

    def get_summary(self) -> str:
        """生成结果摘要"""
        lines = [
            f"## 📊 ShaoFu 选股策略 - {self.target_name}",
            f"**运行时间**: {self.run_time.strftime('%Y-%m-%d %H:%M')}",
            "",
        ]

        if self.buy_signals:
            lines.append(f"### 🟢 买入信号 ({len(self.buy_signals)})")
            for s in self.buy_signals:
                signal_line = f"- **{s.name}** ({s.code}) | 收盘: {s.close} | KDJ.J: {s.kdj_j:.1f} | {s.trigger_reason}"
                # 添加资讯情绪标签
                if s.news_sentiment:
                    sentiment_emoji = {"利好": "📈", "利空": "📉", "中性": "➖"}.get(s.news_sentiment, "")
                    signal_line += f" | {sentiment_emoji}{s.news_sentiment}"
                lines.append(signal_line)
                # 添加新闻摘要
                if s.news_summary:
                    lines.append(f"  > 📰 {s.news_summary}")
            lines.append("")

        if not self.buy_signals:
            lines.append("*暂无买入信号*")

        return "\n".join(lines)


class ShaoFuDataFetcher:
    """
    ShaoFu 策略数据获取器

    支持获取:
    - 指数成分股 (沪深300, 中证500, 上证50)
    - 板块/概念股
    - 个股数据
    """

    def __init__(self, data_dir: str = "./data/shaofu"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ak = None
        self._market_cap_cache: Dict[str, float] = {}

    @property
    def ak(self):
        """懒加载 akshare"""
        if self._ak is None:
            import akshare as ak
            self._ak = ak
        return self._ak

    def get_index_stocks(self, index_code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """
        获取指数成分股历史数据

        Args:
            index_code: 指数代码 (000300=沪深300, 000016=上证50, 000905=中证500)
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
        """
        try:
            # 获取成分股列表
            if index_code == "000300":
                stocks_df = self.ak.index_stock_cons_csindex(symbol="000300")
            elif index_code == "000016":
                stocks_df = self.ak.index_stock_cons_csindex(symbol="000016")
            elif index_code == "000905":
                stocks_df = self.ak.index_stock_cons_csindex(symbol="000905")
            else:
                logger.error(f"不支持的指数代码: {index_code}")
                return None

            stock_codes = stocks_df['成分券代码'].tolist()
            logger.info(f"获取到 {len(stock_codes)} 只成分股")

            # 获取每只股票的历史数据
            all_data = []
            for code in stock_codes[:50]:  # 限制数量避免超时
                try:
                    df = self._fetch_single_stock(code, start_date, end_date)
                    if df is not None and not df.empty:
                        all_data.append(df)
                except Exception as e:
                    logger.warning(f"获取 {code} 数据失败: {e}")
                    continue

            if not all_data:
                return None

            result = pd.concat(all_data, ignore_index=True)
            return result

        except Exception as e:
            logger.error(f"获取指数成分股数据失败: {e}")
            return None

    def get_concept_stocks(self, concept_name: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """
        获取板块/概念股历史数据

        Args:
            concept_name: 板块名称 (如 "人工智能", "商业航天")
        """
        try:
            # 获取概念板块成分股
            concept_df = self.ak.stock_board_concept_cons_em(symbol=concept_name)
            if concept_df is None or concept_df.empty:
                logger.warning(f"未找到概念板块: {concept_name}")
                return None

            stock_codes = concept_df['代码'].tolist()
            stock_names = concept_df['名称'].tolist()
            logger.info(f"[{concept_name}] 获取到 {len(stock_codes)} 只成分股")

            # 获取每只股票的历史数据
            all_data = []
            for i, code in enumerate(stock_codes[:30]):  # 限制数量
                try:
                    df = self._fetch_single_stock(code, start_date, end_date)
                    if df is not None and not df.empty:
                        df['tic_name'] = stock_names[i] if i < len(stock_names) else ""
                        all_data.append(df)
                except Exception as e:
                    logger.warning(f"获取 {code} 数据失败: {e}")
                    continue

            if not all_data:
                return None

            result = pd.concat(all_data, ignore_index=True)
            return result

        except Exception as e:
            logger.error(f"获取概念板块数据失败: {e}")
            return None

    def _fetch_single_stock(self, code: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """获取单只股票历史数据"""
        try:
            df = self.ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust="qfq"
            )

            if df is None or df.empty:
                return None

            # 标准化列名
            df = df.rename(columns={
                '日期': 'date',
                '开盘': 'open',
                '收盘': 'close',
                '最高': 'high',
                '最低': 'low',
                '成交量': 'volume',
                '成交额': 'amount',
            })

            df['tic'] = code
            df['tic_name'] = ""

            # 获取股票名称
            try:
                info = self.ak.stock_individual_info_em(symbol=code)
                if info is not None and not info.empty:
                    name_row = info[info['item'] == '股票简称']
                    if not name_row.empty:
                        df['tic_name'] = name_row['value'].iloc[0]
            except:
                pass

            return df

        except Exception as e:
            logger.debug(f"获取 {code} 数据异常: {e}")
            return None

    def get_market_cap(self, code: str) -> float:
        """获取股票总市值(亿)"""
        if code in self._market_cap_cache:
            return self._market_cap_cache[code]

        try:
            df = self.ak.stock_individual_info_em(symbol=code)
            if df is not None and not df.empty:
                mv_row = df[df['item'] == '总市值']
                if not mv_row.empty:
                    mv = float(mv_row['value'].iloc[0]) / 100000000  # 转亿
                    self._market_cap_cache[code] = mv
                    return mv
        except Exception as e:
            logger.debug(f"获取 {code} 市值失败: {e}")

        return 0.0

    def get_all_market_caps(self, codes: List[str]) -> Dict[str, float]:
        """批量获取市值"""
        result = {}
        for code in codes:
            result[code] = self.get_market_cap(code)
        return result

    def get_all_stocks(self, start_date: str, end_date: str, min_market_cap: float = 50.0) -> Optional[pd.DataFrame]:
        """
        获取全市场 A 股数据 (已过滤市值)

        Args:
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD
            min_market_cap: 最小市值(亿)，默认50亿

        Returns:
            DataFrame 或 None
        """
        try:
            logger.info(">>> 全市场选股模式: 获取沪深A股实时行情...")

            # 获取全部A股实时行情 (含市值信息)
            spot_df = self.ak.stock_zh_a_spot_em()
            if spot_df is None or spot_df.empty:
                logger.error("获取A股实时行情失败")
                return None

            logger.info(f"共获取 {len(spot_df)} 只A股")

            # 市值过滤 (总市值列，单位: 元)
            spot_df['总市值'] = pd.to_numeric(spot_df['总市值'], errors='coerce').fillna(0)
            min_cap_yuan = min_market_cap * 100000000  # 亿 -> 元
            filtered_df = spot_df[spot_df['总市值'] >= min_cap_yuan].copy()
            logger.info(f"市值 >= {min_market_cap}亿 过滤后: {len(filtered_df)} 只")

            # 缓存市值数据
            for _, row in filtered_df.iterrows():
                code = row['代码']
                mv_yi = row['总市值'] / 100000000
                self._market_cap_cache[code] = mv_yi

            # 排除 ST、退市、北交所
            filtered_df = filtered_df[~filtered_df['名称'].str.contains('ST|退|N', na=False)]
            filtered_df = filtered_df[~filtered_df['代码'].str.startswith(('4', '8'))]  # 排除北交所
            logger.info(f"排除ST/退市/北交所后: {len(filtered_df)} 只")

            stock_codes = filtered_df['代码'].tolist()
            stock_names = dict(zip(filtered_df['代码'], filtered_df['名称']))

            # 获取历史数据 (分批处理，避免超时)
            all_data = []
            batch_size = 50
            total = len(stock_codes)

            for i in range(0, total, batch_size):
                batch = stock_codes[i:i+batch_size]
                logger.info(f"下载进度: {min(i+batch_size, total)}/{total} ({100*min(i+batch_size, total)/total:.1f}%)")

                for code in batch:
                    try:
                        df = self._fetch_single_stock(code, start_date, end_date)
                        if df is not None and not df.empty:
                            df['tic_name'] = stock_names.get(code, '')
                            all_data.append(df)
                    except Exception as e:
                        logger.debug(f"获取 {code} 失败: {e}")
                        continue

            if not all_data:
                return None

            result = pd.concat(all_data, ignore_index=True)
            logger.info(f"全市场数据获取完成: {len(result)} 条记录, {result['tic'].nunique()} 只股票")
            return result

        except Exception as e:
            logger.error(f"获取全市场数据失败: {e}")
            return None


class ShaoFuIndicatorCalculator:
    """
    ShaoFu 策略技术指标计算器

    计算: BBI, KDJ, MACD, BOLL, VOL_MA5
    """

    def __init__(self, df: pd.DataFrame):
        """
        Args:
            df: 股票数据 DataFrame，需包含 date, open, high, low, close, volume, tic 列
        """
        self.df = df.copy()
        self.unique_tickers = self.df['tic'].unique()

    def calculate_all(self) -> pd.DataFrame:
        """计算所有技术指标"""
        self._add_bbi()
        self._add_kdj()
        self._add_macd()
        self._add_boll()
        self._add_vol_ma()
        return self.df.fillna(0)

    def _add_bbi(self):
        """添加 BBI 多空指标"""
        bbi_results = []
        for tic in self.unique_tickers:
            tic_data = self.df[self.df['tic'] == tic]
            bbi_values = BBI(tic_data['close'].values)
            tmp = pd.DataFrame({
                'bbi': np.round(bbi_values, 2),
                'tic': tic,
                'date': tic_data['date'].values
            })
            bbi_results.append(tmp)

        bbi_df = pd.concat(bbi_results, ignore_index=True)
        self.df = self.df.merge(bbi_df, on=['tic', 'date'], how='left')

    def _add_kdj(self):
        """添加 KDJ 指标"""
        kdj_results = []
        for tic in self.unique_tickers:
            tic_data = self.df[self.df['tic'] == tic]
            k, d, j = KDJ(
                CLOSE=tic_data['close'].values,
                HIGH=tic_data['high'].values,
                LOW=tic_data['low'].values
            )
            tmp = pd.DataFrame({
                'kdj_k': np.round(k, 2),
                'kdj_d': np.round(d, 2),
                'kdj_j': np.round(j, 2),
                'tic': tic,
                'date': tic_data['date'].values
            })
            kdj_results.append(tmp)

        kdj_df = pd.concat(kdj_results, ignore_index=True)
        self.df = self.df.merge(kdj_df, on=['tic', 'date'], how='left')

    def _add_macd(self):
        """添加 MACD 指标"""
        macd_results = []
        for tic in self.unique_tickers:
            tic_data = self.df[self.df['tic'] == tic]
            dif, dea, macd = MACD(CLOSE=tic_data['close'].values)
            tmp = pd.DataFrame({
                'dif': np.round(dif, 2),
                'dea': np.round(dea, 2),
                'macd': np.round(macd, 2),
                'tic': tic,
                'date': tic_data['date'].values
            })
            macd_results.append(tmp)

        macd_df = pd.concat(macd_results, ignore_index=True)
        self.df = self.df.merge(macd_df, on=['tic', 'date'], how='left')

    def _add_boll(self):
        """添加布林带指标"""
        boll_results = []
        for tic in self.unique_tickers:
            tic_data = self.df[self.df['tic'] == tic]
            upper, mid, lower = BOLL(CLOSE=tic_data['close'].values)
            tmp = pd.DataFrame({
                'boll_upper': np.round(upper, 2),
                'boll_mid': np.round(mid, 2),
                'boll_lower': np.round(lower, 2),
                'tic': tic,
                'date': tic_data['date'].values
            })
            boll_results.append(tmp)

        boll_df = pd.concat(boll_results, ignore_index=True)
        self.df = self.df.merge(boll_df, on=['tic', 'date'], how='left')

    def _add_vol_ma(self):
        """添加5日均量线"""
        self.df['volume'] = pd.to_numeric(self.df['volume'], errors='coerce')
        vol_results = []
        for tic in self.unique_tickers:
            tic_data = self.df[self.df['tic'] == tic].copy()
            tic_data['vol_ma5'] = tic_data['volume'].rolling(window=5).mean().round(2)
            vol_results.append(tic_data[['date', 'tic', 'vol_ma5']])

        vol_df = pd.concat(vol_results, ignore_index=True)
        self.df = self.df.merge(vol_df, on=['tic', 'date'], how='left')


class ShaoFuStrategyAnalyzer:
    """
    ShaoFu 选股策略分析器

    基于通达信优化战法进行买卖点判断
    """

    def __init__(self, df: pd.DataFrame, market_caps: Dict[str, float] = None):
        """
        Args:
            df: 包含技术指标的 DataFrame
            market_caps: 股票代码 -> 市值(亿) 的映射
        """
        self.df = df.copy()
        self.unique_tickers = self.df['tic'].unique()
        self.market_caps = market_caps or {}

        # 参数配置
        self.threshold_j_buy = 13  # KDJ.J 超卖阈值
        self.threshold_j_sell = 80  # KDJ.J 超买阈值
        self.min_market_cap = 50  # 最小市值(亿)

    def analyze(self) -> List[StockSignal]:
        """
        执行选股分析

        Returns:
            StockSignal 列表
        """
        MIN_HISTORY = 100
        signals = []

        for tic in self.unique_tickers:
            df = self.df[self.df['tic'] == tic].sort_values(by='date')

            if len(df) < MIN_HISTORY:
                continue

            df = df.tail(MIN_HISTORY).copy()

            # 市值过滤
            mv_yi = self.market_caps.get(tic, 0)
            if mv_yi < self.min_market_cap:
                continue

            signal = self._analyze_single_stock(df, tic, mv_yi)
            if signal:
                signals.append(signal)

        return signals

    def _analyze_single_stock(self, df: pd.DataFrame, tic: str, mv_yi: float) -> Optional[StockSignal]:
        """分析单只股票"""
        try:
            # 基础数据
            C = df['close']
            O = df['open']
            H = df['high']
            L = df['low']
            VOL = df['volume']
            AMOUNT = df['amount'] if 'amount' in df.columns else VOL * C

            C_1 = C.shift(1)
            O_1 = O.shift(1)
            VOL_1 = VOL.shift(1)

            # KDJ 计算
            lowest_l = L.rolling(window=9).min()
            highest_h = H.rolling(window=9).max()
            rsv = (C - lowest_l) / (highest_h - lowest_l) * 100
            rsv = rsv.fillna(50)
            k = rsv.ewm(alpha=1/3, adjust=False).mean()
            d = k.ewm(alpha=1/3, adjust=False).mean()
            j = 3 * k - 2 * d

            j_ok = j <= self.threshold_j_buy

            # 阴阳量分析
            real_yang = (C > O) & ~(C < C_1)
            real_yin = (C < O) & ~(C > C_1)

            vol_yang = VOL * real_yang.astype(int)
            vol_yin = VOL * real_yin.astype(int)

            vol_yang21 = vol_yang.rolling(window=21).sum()
            vol_yin21 = vol_yin.rolling(window=21).sum()
            vol_yang14 = vol_yang.rolling(window=14).sum()
            vol_yin14 = vol_yin.rolling(window=14).sum()

            yangyin_ok = (vol_yang21 > 1.5 * vol_yin21) | (vol_yang14 > 1.5 * vol_yin14)

            # 流动性
            amount_ma28 = AMOUNT.rolling(window=28).mean() / 100000000
            lq_ok = amount_ma28 >= 0.005

            # 形态排雷
            llv_o_28 = O.rolling(window=28).min()
            hhv_o_28 = O.rolling(window=28).max()
            o85 = llv_o_28 + 0.925 * (hhv_o_28 - llv_o_28)
            top15o = O >= o85

            fd15 = (C < C_1) & (C <= O) & (VOL >= 1.15 * VOL_1)
            bad_pattern = top15o & fd15
            bad_pattern_count = bad_pattern.rolling(window=28).sum()
            good28 = bad_pattern_count == 0

            max_vol_28 = VOL.rolling(window=28).max()
            is_max_vol = (VOL == max_vol_28)
            is_max_vol_yin = is_max_vol & real_yin
            max28_yin_count = is_max_vol_yin.rolling(window=28).sum()
            max28_ok = max28_yin_count == 0

            # 触发条件
            avg40 = VOL.rolling(window=40).mean()
            plry = (VOL > 1.8 * VOL_1) & (C > O) & (VOL > avg40)
            plry_cnt = plry.rolling(window=28).sum() >= 3

            v40p = VOL_1.rolling(window=40).mean()
            bd = (C > C_1) & (C >= O)
            bigv = VOL > 1.75 * v40p

            llv_c_40 = C.rolling(window=40).min()
            hhv_c_40 = C.rolling(window=40).max()
            r55 = llv_c_40 + 0.55 * (hhv_c_40 - llv_c_40)
            pos_ok = C > r55

            key_k_trigger = bd & bigv & pos_ok
            trigger = plry_cnt | key_k_trigger

            # 综合选股
            xg = trigger & j_ok & lq_ok & good28 & max28_ok & yangyin_ok

            # 判定当前状态
            last_idx = -1
            is_buy = xg.iloc[last_idx]
            curr_j = j.iloc[last_idx]
            is_sell = curr_j > self.threshold_j_sell

            signal_val = None
            reason = ""

            if is_buy:
                signal_val = True
                if plry_cnt.iloc[last_idx]:
                    reason += "倍量堆积 "
                if key_k_trigger.iloc[last_idx]:
                    reason += "关键突破 "

            # 只返回买入信号，忽略卖出信号
            if signal_val is None:
                return None

            return StockSignal(
                code=tic,
                name=df['tic_name'].iloc[last_idx] if 'tic_name' in df.columns else tic,
                market_cap=mv_yi,
                signal=signal_val,
                close=round(C.iloc[last_idx], 2),
                kdj_j=round(curr_j, 2),
                trigger_reason=reason.strip()
            )

        except Exception as e:
            logger.warning(f"分析 {tic} 失败: {e}")
            return None

    def judge_trend(self, start_date: str, end_date: str) -> pd.DataFrame:
        """
        通过 BBI 指标的一阶导判断趋势

        Returns:
            DataFrame with columns: tic, tic_name, trend (1=上涨, 0=震荡, -1=下跌)
        """
        threshold = 0.5
        period_df = self.df[(self.df['date'] >= start_date) & (self.df['date'] <= end_date)]

        results = []
        for tic in self.unique_tickers:
            tic_data = period_df[period_df['tic'] == tic]
            if tic_data.empty or 'bbi' not in tic_data.columns:
                continue

            bbi_derivative = tic_data['bbi'].diff().dropna()
            avg_derivative = bbi_derivative.mean() if len(bbi_derivative) > 0 else 0

            if avg_derivative > threshold:
                trend = 1
            elif avg_derivative < -threshold:
                trend = -1
            else:
                trend = 0

            results.append({
                'tic': tic,
                'tic_name': tic_data['tic_name'].iloc[0] if 'tic_name' in tic_data.columns else '',
                'trend': trend,
                'bbi_derivative': round(avg_derivative, 3)
            })

        return pd.DataFrame(results)


class ShaoFuStrategy:
    """
    ShaoFu 选股策略主类

    整合数据获取、指标计算、策略分析的完整流程
    """

    # 支持的目标类型映射
    TARGET_MAP = {
        'csi300': '000300',
        '沪深300': '000300',
        'sse50': '000016',
        '上证50': '000016',
        'csi500': '000905',
        '中证500': '000905',
    }

    # 全市场标识
    ALL_MARKET_KEYS = ['all', 'ALL', '全市场', '沪深A股', 'a股', 'A股']

    def __init__(self, data_dir: str = "./data/shaofu", search_service=None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.fetcher = ShaoFuDataFetcher(str(self.data_dir))
        self.search_service = search_service  # 搜索服务（可选）

    def run(
        self,
        targets: List[str],
        start_date: str = None,
        end_date: str = None,
        skip_download: bool = False
    ) -> List[ShaoFuStrategyResult]:
        """
        执行选股策略

        Args:
            targets: 目标列表 (指数代码/板块名称)
            start_date: 开始日期 YYYYMMDD (默认1年前)
            end_date: 结束日期 YYYYMMDD (默认今天)
            skip_download: 是否跳过数据下载使用本地缓存

        Returns:
            每个目标的策略结果列表
        """
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=365)).strftime("%Y%m%d")
        if end_date is None:
            end_date = datetime.now().strftime("%Y%m%d")

        results = []

        for target in targets:
            logger.info(f">>> 处理目标: [{target}]")

            try:
                result = self._process_target(target, start_date, end_date, skip_download)
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"处理 {target} 失败: {e}")
                continue

        return results

    def _process_target(
        self,
        target: str,
        start_date: str,
        end_date: str,
        skip_download: bool
    ) -> Optional[ShaoFuStrategyResult]:
        """处理单个目标"""

        # 1. 数据获取
        cache_file = self.data_dir / f"data_{target}.csv"
        tech_file = self.data_dir / f"data_{target}_tech.csv"

        if skip_download and cache_file.exists():
            logger.info(f"使用缓存数据: {cache_file}")
            df = pd.read_csv(cache_file, dtype={'tic': str})
        else:
            logger.info(f"获取数据: {target}")

            # 判断目标类型
            if target in self.ALL_MARKET_KEYS:
                # 全市场选股
                df = self.fetcher.get_all_stocks(start_date, end_date)
            elif target in self.TARGET_MAP:
                index_code = self.TARGET_MAP[target]
                df = self.fetcher.get_index_stocks(index_code, start_date, end_date)
            elif target[0].isdigit():
                # 个股代码
                df = self.fetcher._fetch_single_stock(target, start_date, end_date)
            else:
                # 板块名称
                df = self.fetcher.get_concept_stocks(target, start_date, end_date)

            if df is None or df.empty:
                logger.warning(f"获取 {target} 数据失败")
                return None

            df.to_csv(cache_file, index=False)
            logger.info(f"数据已保存: {cache_file}")

        # 2. 计算技术指标
        if skip_download and tech_file.exists():
            logger.info(f"使用缓存指标数据: {tech_file}")
            tech_df = pd.read_csv(tech_file, dtype={'tic': str})
        else:
            logger.info("计算技术指标...")
            calculator = ShaoFuIndicatorCalculator(df)
            tech_df = calculator.calculate_all()
            tech_df.to_csv(tech_file, index=False)
            logger.info(f"指标数据已保存: {tech_file}")

        # 3. 获取市值数据
        unique_codes = tech_df['tic'].unique().tolist()
        market_caps = self.fetcher.get_all_market_caps(unique_codes)

        # 4. 策略分析
        logger.info("执行策略分析...")
        analyzer = ShaoFuStrategyAnalyzer(tech_df, market_caps)
        signals = analyzer.analyze()

        # 5. 为买入信号股票获取新闻资讯
        buy_signals = [s for s in signals if s.signal is True]
        if buy_signals and self.search_service and self.search_service.is_available:
            logger.info(f"为 {len(buy_signals)} 只买入信号股票获取新闻资讯...")
            buy_signals = self._enrich_signals_with_news(buy_signals)

        # 6. 整理结果
        result = ShaoFuStrategyResult(
            signals=signals,
            buy_signals=buy_signals,
            sell_signals=[s for s in signals if s.signal is False],
            run_time=datetime.now(),
            target_name=target
        )

        logger.info(f"[{target}] 分析完成: 买入信号 {len(result.buy_signals)}")

        return result

    def _enrich_signals_with_news(self, signals: List[StockSignal]) -> List[StockSignal]:
        """
        为买入信号股票获取新闻资讯并进行情绪分析

        Args:
            signals: 买入信号列表

        Returns:
            添加了新闻摘要和情绪的信号列表
        """
        enriched_signals = []

        for signal in signals:
            try:
                # 搜索该股票的最新新闻
                query = f"{signal.name} {signal.code} 最新消息"
                search_result = self.search_service.search(query, max_results=3)

                if search_result.success and search_result.results:
                    # 提取新闻摘要（取前2条）
                    news_items = []
                    for r in search_result.results[:2]:
                        news_items.append(f"{r.title}")

                    signal.news_summary = " | ".join(news_items)

                    # 简单情绪分析（基于关键词）
                    signal.news_sentiment = self._analyze_sentiment(search_result.results)

                    logger.info(f"[{signal.code}] 资讯获取成功: {signal.news_sentiment}")
                else:
                    logger.debug(f"[{signal.code}] 未找到相关资讯")

            except Exception as e:
                logger.warning(f"[{signal.code}] 获取资讯失败: {e}")

            enriched_signals.append(signal)

        return enriched_signals

    def _analyze_sentiment(self, results: list) -> str:
        """
        基于关键词的简单情绪分析

        Args:
            results: 搜索结果列表

        Returns:
            情绪标签: 利好/利空/中性
        """
        positive_keywords = [
            '利好', '上涨', '突破', '新高', '增长', '盈利', '超预期',
            '订单', '中标', '合作', '收购', '回购', '增持', '分红',
            '涨停', '大涨', '暴涨', '创新高', '业绩增长', '扭亏'
        ]
        negative_keywords = [
            '利空', '下跌', '暴跌', '跌停', '亏损', '减持', '质押',
            '处罚', '调查', '退市', '预亏', '下滑', '违规', '诉讼',
            '风险', '警示', '暂停', '终止', '负面', '爆雷'
        ]

        positive_count = 0
        negative_count = 0

        for r in results:
            text = f"{r.title} {r.snippet}".lower()
            for kw in positive_keywords:
                if kw in text:
                    positive_count += 1
            for kw in negative_keywords:
                if kw in text:
                    negative_count += 1

        if positive_count > negative_count + 1:
            return "利好"
        elif negative_count > positive_count + 1:
            return "利空"
        else:
            return "中性"


def run_shaofu_strategy(
    targets: List[str] = None,
    skip_download: bool = False,
    data_dir: str = "./data/shaofu",
    search_service=None
) -> List[ShaoFuStrategyResult]:
    """
    ShaoFu 策略执行入口函数

    Args:
        targets: 目标列表，默认 ["沪深300"]
        skip_download: 是否跳过下载
        data_dir: 数据目录
        search_service: 搜索服务实例（用于获取新闻资讯）

    Returns:
        策略结果列表
    """
    if targets is None:
        targets = ["沪深300"]

    strategy = ShaoFuStrategy(data_dir=data_dir, search_service=search_service)
    return strategy.run(targets=targets, skip_download=skip_download)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')

    # 测试运行
    targets = sys.argv[1:] if len(sys.argv) > 1 else ["沪深300"]
    results = run_shaofu_strategy(targets=targets)

    for result in results:
        print(result.get_summary())
