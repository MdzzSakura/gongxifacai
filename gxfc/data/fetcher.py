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

    def market_activity(self) -> pd.DataFrame:
        """市场赚钱效应,列:item,value(上涨/下跌/涨停/真实涨停/跌停/炸板/...)。"""
        import akshare as ak
        return self.fetch("market_activity", ak.stock_market_activity_legu, use_cache=False)

    def zt_pool(self, date: str) -> pd.DataFrame:
        """涨停股池,含 连板数。date 形如 '20260629'。"""
        import akshare as ak
        return self.fetch(f"zt_pool:{date}", lambda: ak.stock_zt_pool_em(date=date))

    def industry_board(self) -> pd.DataFrame:
        """东财行业板块实时行情,含 板块名称,涨跌幅,领涨股。"""
        import akshare as ak
        return self.fetch("industry_board", ak.stock_board_industry_name_em, use_cache=False)

    def industry_cons(self, board: str) -> pd.DataFrame:
        """行业板块成分股,含 名称,涨跌幅,成交额。"""
        import akshare as ak
        return self.fetch(
            f"industry_cons:{board}", lambda: ak.stock_board_industry_cons_em(symbol=board)
        )

    def yjyg(self, date: str) -> pd.DataFrame:
        """业绩预告,含 股票简称,预测净利润-同比增长。date 形如 '20260331'(季度末)。"""
        import akshare as ak
        return self.fetch(f"yjyg:{date}", lambda: ak.stock_yjyg_em(date=date))

    def stock_daily(self, code: str, start: str, end: str) -> pd.DataFrame:
        """个股前复权日K,含 日期,开盘,最高。start/end 形如 '20260101'。"""
        import akshare as ak
        return self.fetch(
            f"daily:{code}:{start}:{end}",
            lambda: ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"
            ),
        )
