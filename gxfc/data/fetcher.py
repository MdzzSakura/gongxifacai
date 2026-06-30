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

# 新浪日K(英文列)→ 东财 stock_zh_a_hist(中文列)映射,
# 使日K回退源对下游(profit_fault.detect_gap 依赖 日期/开盘/最高)透明。
_SINA_DAILY_RENAME = {
    "date": "日期",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量",
    "amount": "成交额",
    "turnover": "换手率",
}


def _is_conn_error(err: Exception) -> bool:
    """判断是否为连接被拒类错误(东财风控掐连的典型表现),用于触发熔断。"""
    if isinstance(err, ConnectionError):
        return True
    s = str(err)
    return "RemoteDisconnected" in s or "Connection aborted" in s


def _sina_symbol(code: str) -> str:
    """6位纯数字代码 → 新浪带市场前缀格式(sh/sz/bj)。

    沪市 6/9 开头(含 688 科创),深市 0/2/3 开头(含 300/301 创业板),
    北交所 4/8 开头或 92 开头(如 920xxx)。
    """
    code = str(code).strip().zfill(6)
    # 北交所优先判断:920xxx 以 9 开头,会与沪市 9(B股)规则冲突,须先拦截
    if code.startswith("92") or code[0] in ("4", "8"):
        return "bj" + code
    if code[0] in ("0", "2", "3"):
        return "sz" + code
    if code[0] in ("6", "9"):
        return "sh" + code
    return "sh" + code  # 兜底,极少触达


def _sina_daily_to_em_schema(df: pd.DataFrame) -> pd.DataFrame:
    """把新浪日K归一化为东财列结构,日期统一为 'YYYY-MM-DD' 字符串(与东财一致)。"""
    out = df.rename(columns=_SINA_DAILY_RENAME)
    if "日期" in out.columns:
        out["日期"] = pd.to_datetime(out["日期"]).dt.strftime("%Y-%m-%d")
    return out


# 新浪行业榜(stock_sector_spot)→ 东财 industry_board 列;新浪行业成分股
# (stock_sector_detail,英文列)→ 东财 industry_cons 列,使板块段回退对下游
# (sector.rank_sectors / core_stocks)透明。
_SINA_SECTOR_RENAME = {"板块": "板块名称", "股票名称": "领涨股票"}
_SINA_DETAIL_RENAME = {"name": "名称", "changepercent": "涨跌幅", "amount": "成交额"}


def _sina_sector_to_em_schema(df: pd.DataFrame) -> pd.DataFrame:
    """新浪行业榜归一化为东财板块榜列结构(板块名称/涨跌幅/领涨股票)。"""
    return df.rename(columns=_SINA_SECTOR_RENAME)


def _sina_detail_to_em_schema(df: pd.DataFrame) -> pd.DataFrame:
    """新浪行业成分股归一化为东财成分股列结构(名称/涨跌幅/成交额)。"""
    return df.rename(columns=_SINA_DETAIL_RENAME)


def _sina_industry_cons(board: str) -> pd.DataFrame:
    """按东财板块名反查新浪行业 label,再下钻成分股并归一化。

    build_board 传入的是中文板块名,而新浪成分股接口 stock_sector_detail 需
    label(如 'new_blhy'),故先用 stock_sector_spot 建立 名称→label 映射。
    板块名在新浪行业分类中不存在时抛错,由上层 fetch 重试/降级处理。
    """
    import akshare as ak
    spot = ak.stock_sector_spot(indicator="新浪行业")
    hit = spot[spot["板块"] == board] if "板块" in spot.columns else spot.iloc[0:0]
    if hit.empty:
        raise ValueError(f"新浪行业分类未找到板块:{board}")
    label = hit.iloc[0]["label"]
    return _sina_detail_to_em_schema(ak.stock_sector_detail(sector=label))


class Fetcher:
    def __init__(
        self,
        cache: Optional[DataFrameCache] = None,
        retries: int = 3,
        min_interval: float = 0.8,
    ):
        if retries < 1:
            raise ValueError("retries 必须 >= 1")
        self._cache = cache
        self._retries = retries
        # 东财等数据源对突发请求敏感,强制任意两次真实请求之间至少间隔 min_interval 秒
        self._min_interval = min_interval
        self._last_call = 0.0
        # 熔断标志:一旦探测到东财行情连接被风控掐断,本次运行后续有兜底的接口
        # 直接走新浪,不再对已知挂掉的东财反复重试,避免拖慢与刷屏。
        self._eastmoney_down = False

    def _throttle(self) -> None:
        """请求节流:距上次真实请求不足 min_interval 则补足等待,摊平突发流量。"""
        if self._min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_call = time.monotonic()

    def fetch(
        self,
        key: str,
        loader: Callable[[], pd.DataFrame],
        use_cache: bool = True,
        retries: Optional[int] = None,
    ) -> pd.DataFrame:
        """缓存命中优先 + 失败重试。retries 不传则用实例默认;有兜底源的调用
        可传 retries=1 让主源快速失败,把重试预算留给真正可用的回退源。"""
        if use_cache and self._cache is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
        attempts = retries if retries is not None else self._retries
        last_err = None
        for attempt in range(1, attempts + 1):
            try:
                self._throttle()
                df = loader()
                if use_cache and self._cache is not None:
                    self._cache.set(key, df)
                return df
            except Exception as err:  # AKShare 抛出的异常类型不固定,统一兜底
                last_err = err
                logger.warning("抓取 %s 第 %d 次失败:%s", key, attempt, err)
                if attempt < attempts:
                    # 指数退避(1/2/4...秒,封顶8秒),给限流恢复留时间
                    time.sleep(min(8.0, 2.0 ** (attempt - 1)))
        raise last_err

    def _fetch_with_fallback(
        self,
        key: str,
        em_loader: Callable[[], pd.DataFrame],
        sina_loader: Optional[Callable[[], pd.DataFrame]],
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """主源东财、失败回退新浪,带熔断。

        东财未熔断时只试 1 次(有兜底,不值得多次重试);若失败且为连接被拒
        (RemoteDisconnected 等)则触发熔断,本次运行后续直接走新浪。
        sina_loader 为 None 表示该项无可用新浪回退(如北交所日K),此时直接抛错
        由调用方降级跳过,不做无谓重试。
        """
        if not self._eastmoney_down:
            try:
                return self.fetch(key, em_loader, use_cache=use_cache, retries=1)
            except Exception as em_err:
                if _is_conn_error(em_err):
                    self._eastmoney_down = True
                    logger.warning("东财行情连接被风控掐断,本次运行后续直接走新浪源")
                if sina_loader is None:
                    raise
                logger.warning("东财 %s 失败,回退新浪源:%s", key, em_err)
        if sina_loader is None:
            raise RuntimeError(f"{key}:东财不可用且无新浪回退源")
        return self.fetch(key, sina_loader, use_cache=use_cache)

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
        # 新浪 stock_zh_a_spot 自带 '涨跌幅'(float),compute_market_emotion 可直接消费
        return self._fetch_with_fallback(
            "spot", ak.stock_zh_a_spot_em, ak.stock_zh_a_spot, use_cache=False
        )

    def industry_board(self) -> pd.DataFrame:
        """东财行业板块实时行情。列含:板块名称,涨跌幅,领涨股票 等。

        东财限流/断连时回退新浪行业榜 stock_sector_spot,归一化为东财列结构。
        注意:新浪行业分类口径与东财不同,回退期板块名称会随之切换。
        """
        import akshare as ak
        return self._fetch_with_fallback(
            "industry_board",
            ak.stock_board_industry_name_em,
            lambda: _sina_sector_to_em_schema(ak.stock_sector_spot(indicator="新浪行业")),
            use_cache=False,
        )

    def industry_cons(self, board: str) -> pd.DataFrame:
        """行业板块成分股。含 名称,涨跌幅,成交额。

        东财失败时回退新浪:按板块名反查 label 再下钻(见 _sina_industry_cons)。
        两源共用缓存键,任一成功即缓存。
        """
        import akshare as ak
        return self._fetch_with_fallback(
            f"industry_cons:{board}",
            lambda: ak.stock_board_industry_cons_em(symbol=board),
            lambda: _sina_industry_cons(board),
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
        """个股前复权日K。含 日期,开盘,最高。start/end 形如 '20260101'。

        主源东财 stock_zh_a_hist;东财限流/断连(RemoteDisconnected)时自动回退
        新浪 stock_zh_a_daily,并把新浪结果归一化为东财列结构,对下游透明。
        两源同用一个缓存键:任一源成功即写入缓存,下次直接命中不再触网。
        """
        import akshare as ak
        key = f"daily:{code}:{start}:{end}"
        sina_sym = _sina_symbol(code)
        # 新浪 stock_zh_a_daily 不覆盖北交所(920/8xx/4xx),此类无可用回退源
        sina_loader = None if sina_sym.startswith("bj") else (
            lambda: _sina_daily_to_em_schema(
                ak.stock_zh_a_daily(
                    symbol=sina_sym, start_date=start, end_date=end, adjust="qfq"
                )
            )
        )
        return self._fetch_with_fallback(
            key,
            lambda: ak.stock_zh_a_hist(
                symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"
            ),
            sina_loader,
        )
