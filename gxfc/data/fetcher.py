"""AKShare 数据抓取封装。

所有对 AKShare 的调用集中在本文件,接口名变动只改这里。
通用 fetch 提供"缓存命中优先 + 失败重试"语义;具体业务方法各自传入
对应的 AKShare loader。业务方法用到的列名见各方法 docstring。
"""
import logging
import time
from typing import Callable, Optional

import pandas as pd

from gxfc.data.cache import DataFrameCache

logger = logging.getLogger(__name__)


class Fetcher:
    def __init__(self, cache: Optional[DataFrameCache] = None, retries: int = 3):
        if retries < 1:
            raise ValueError("retries 必须 >= 1")
        self._cache = cache
        self._retries = retries

    def fetch(
        self, key: str, loader: Callable[[], pd.DataFrame], use_cache: bool = True
    ) -> pd.DataFrame:
        if use_cache and self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        last_err = None
        for attempt in range(1, self._retries + 1):
            try:
                df = loader()
                if use_cache and self._cache is not None:
                    self._cache.set(key, df)
                return df
            except Exception as err:  # AKShare 抛出的异常类型不固定,统一兜底
                last_err = err
                logger.warning("抓取 %s 第 %d 次失败:%s", key, attempt, err)
                if attempt < self._retries:
                    time.sleep(0.5 * attempt)
        raise last_err

    # —— 以下为具体业务接口,封装对应 AKShare 调用 ——

    def zt_pool(self, date: str) -> pd.DataFrame:
        """涨停股池。date 形如 '20260629'。
        列含:代码,名称,涨跌幅,连板数,炸板次数,所属行业 等。
        """
        import akshare as ak
        return self.fetch(f"zt_pool:{date}", lambda: ak.stock_zt_pool_em(date=date))

    def dt_pool(self, date: str) -> pd.DataFrame:
        """跌停股池。date 形如 '20260629'。
        列含:代码,名称,涨跌幅,连续跌停 等。
        """
        import akshare as ak
        return self.fetch(f"dt_pool:{date}", lambda: ak.stock_zt_pool_dtgc_em(date=date))

    def zb_pool(self, date: str) -> pd.DataFrame:
        """炸板股池。date 形如 '20260629'。
        列含:代码,名称,涨跌幅,炸板次数 等。
        """
        import akshare as ak
        return self.fetch(f"zb_pool:{date}", lambda: ak.stock_zt_pool_zbgc_em(date=date))

    def spot(self) -> pd.DataFrame:
        """全市场 A 股实时快照(东财)。含 涨跌幅 列。
        注意:东财对该接口限流较敏感,失败由通用 fetch 重试,
        最终失败由调用方降级(将 spot_df 置 None 传入 compute_market_emotion)。
        不走缓存,保证数据实时。
        """
        import akshare as ak
        return self.fetch("spot", ak.stock_zh_a_spot_em, use_cache=False)

    def industry_board(self) -> pd.DataFrame:
        """东财行业板块实时行情。
        列含:板块名称,涨跌幅,领涨股票 等。
        """
        import akshare as ak
        return self.fetch("industry_board", ak.stock_board_industry_name_em, use_cache=False)

    def industry_cons(self, board: str) -> pd.DataFrame:
        """行业板块成分股。含 名称,涨跌幅,成交额。"""
        import akshare as ak
        return self.fetch(
            f"industry_cons:{board}", lambda: ak.stock_board_industry_cons_em(symbol=board)
        )

    def yjyg(self, date: str) -> pd.DataFrame:
        """业绩预告。date 形如 '20260331'(季度末)。
        每只股票有多行,每行一个预测指标。列含:
          股票代码,股票简称,预测指标,业绩变动幅度,预测数值,
          业绩变动原因,预告类型,上年同期值,公告日期。
        净利润同比增速 = 预测指标=='归属于上市公司股东的净利润' 那行的 业绩变动幅度(%)。
        """
        import akshare as ak
        return self.fetch(f"yjyg:{date}", lambda: ak.stock_yjyg_em(date=date))

    def stock_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """个股前复权日K。含 日期,开盘,最高。start/end 形如 '20260101'。"""
        import akshare as ak
        return self.fetch(
            f"daily:{code}:{start}:{end}",
            lambda: ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"
            ),
        )
