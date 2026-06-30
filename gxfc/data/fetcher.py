"""数据抓取封装(多源)。

对外数据调用集中在本文件,接口/源变动只改这里。通用 fetch 提供"缓存命中优先 +
失败重试"语义;_fetch_first_ok 按优先级在多个数据源间自动回退。业务方法用到的
列名见各方法 docstring。各源结果统一归一化为东财列结构,对下游透明。

源稳定性:个股日K 优先 Baostock(自有服务器,不爬东财→不受东财风控),失败再
回退新浪、东财;板块/实时快照 主东财、回退新浪;涨跌停池为东财独有(push2ex),
免费渠道无替代,断连时由调用方降级或吃缓存。
"""
import contextlib
import io
import logging
import time
from typing import Callable, List, Optional, Tuple

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


# Baostock 日K(英文列、数值为字符串)→ 东财中文列。Baostock 自有服务器,
# 不爬东财,是免费里最稳的日K源;仅覆盖沪深,不含北交所。
_BAOSTOCK_RENAME = {
    "date": "日期", "open": "开盘", "high": "最高", "low": "最低",
    "close": "收盘", "volume": "成交量", "amount": "成交额",
}


def _baostock_symbol(code: str) -> str:
    """6位代码 → Baostock 格式(sh./sz.)。仅沪深:6/9→sh,其余→sz。"""
    code = str(code).strip().zfill(6)
    prefix = "sh" if code[0] in ("6", "9") else "sz"
    return f"{prefix}.{code}"


def _baostock_daily_to_em_schema(df: pd.DataFrame) -> pd.DataFrame:
    """Baostock 日K归一化为东财列结构;数值列转 float,日期保持 'YYYY-MM-DD'。"""
    out = df.rename(columns=_BAOSTOCK_RENAME)
    for col in ("开盘", "最高", "最低", "收盘", "成交量", "成交额"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


_baostock_logged_in = False
_baostock_available: Optional[bool] = None


def _baostock_ready() -> bool:
    """进程内探测 baostock 是否可用(未安装则只提示一次,后续静默跳过)。"""
    global _baostock_available
    if _baostock_available is None:
        try:
            import baostock  # noqa: F401
            _baostock_available = True
        except Exception:
            _baostock_available = False
            logger.warning(
                "未安装 baostock,个股日K回退新浪源;建议 pip install baostock 获得更稳的免费日K"
            )
    return _baostock_available


def _ensure_baostock_login() -> None:
    """进程内只登录一次 Baostock。批量拉日K时避免每次 login/logout 的网络开销。

    登录/登出会打印到 stdout,这里重定向吞掉以保持控制台干净。
    """
    global _baostock_logged_in
    if _baostock_logged_in:
        return
    import baostock as bs
    with contextlib.redirect_stdout(io.StringIO()):
        bs.login()
    _baostock_logged_in = True


def _baostock_daily(code: str, start: str, end: str) -> pd.DataFrame:
    """Baostock 前复权日K,归一化为东财列结构。

    code 为6位纯数字;start/end 形如 '20260101'。空结果/查询错误抛异常,
    由 _fetch_first_ok 转下一个源。进程内复用单次登录(见 _ensure_baostock_login)。
    """
    import baostock as bs
    sym = _baostock_symbol(code)
    s = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
    e = f"{end[:4]}-{end[4:6]}-{end[6:8]}"
    _ensure_baostock_login()
    with contextlib.redirect_stdout(io.StringIO()):
        rs = bs.query_history_k_data_plus(
            sym, "date,open,high,low,close,volume,amount",
            start_date=s, end_date=e, frequency="d", adjustflag="2",  # 2=前复权
        )
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 查询失败:{rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    if df.empty:
        raise RuntimeError(f"baostock 无数据:{sym}")
    return _baostock_daily_to_em_schema(df)


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


_MARKET_SPOT_COLS = ["代码", "名称", "涨跌幅", "最新价", "成交量", "成交额"]


def _normalize_market_spot(df: pd.DataFrame) -> pd.DataFrame:
    """新浪全市场快照归一化:代码去市场前缀(bj/sh/sz)转6位,保留粗筛所需列。"""
    out = df.copy()
    out["代码"] = out["代码"].astype(str).str.replace(r"^[A-Za-z]+", "", regex=True)
    cols = [c for c in _MARKET_SPOT_COLS if c in out.columns]
    return out[cols]


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

    def _fetch_first_ok(
        self,
        key: str,
        sources: List[Tuple],
        use_cache: bool = True,
    ) -> pd.DataFrame:
        """按优先级依次尝试多个数据源,首个成功即缓存返回。

        sources 为有序列表,每项 (名称, loader) 或 (名称, loader, is_eastmoney)。
        东财源:熔断后跳过;只试 1 次(易风控,有后续兜底则不值得多试);失败且为
        连接被拒(RemoteDisconnected 等)时触发熔断,本次运行后续跳过所有东财源。
        非东财源:作为最后兜底时用默认 retries(值得多试),否则只试 1 次。
        全部失败抛最后一个异常,由调用方降级跳过。
        """
        last_err: Optional[Exception] = None
        n = len(sources)
        for i, src in enumerate(sources):
            name, loader = src[0], src[1]
            is_em = src[2] if len(src) > 2 else False
            if is_em and self._eastmoney_down:
                continue
            is_last = i == n - 1
            retries = None if (is_last and not is_em) else 1
            try:
                return self.fetch(key, loader, use_cache=use_cache, retries=retries)
            except Exception as err:
                last_err = err
                if is_em and _is_conn_error(err):
                    self._eastmoney_down = True
                    logger.warning("东财行情连接被风控掐断,本次运行后续跳过东财源")
                logger.warning("%s 源 %s 失败:%s", name, key, err)
        if last_err is None:
            raise RuntimeError(f"{key}:无可用数据源")
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
        # 新浪 stock_zh_a_spot 自带 '涨跌幅'(float),compute_market_emotion 可直接消费
        return self._fetch_first_ok(
            "spot",
            [("东财", ak.stock_zh_a_spot_em, True), ("新浪", ak.stock_zh_a_spot)],
            use_cache=False,
        )

    def market_spot(self) -> pd.DataFrame:
        """新浪全市场实时快照(~5500 只,含北交所),用于全市场粗筛。

        归一化为 代码(去前缀6位)/名称/涨跌幅/最新价/成交额。只走新浪(列稳定、
        含北交所、批量接口抗限流);不走缓存,保证当日数据。
        """
        import akshare as ak
        return _normalize_market_spot(self.fetch("market_spot", ak.stock_zh_a_spot, use_cache=False))

    def industry_board(self) -> pd.DataFrame:
        """东财行业板块实时行情。列含:板块名称,涨跌幅,领涨股票 等。

        东财限流/断连时回退新浪行业榜 stock_sector_spot,归一化为东财列结构。
        注意:新浪行业分类口径与东财不同,回退期板块名称会随之切换。
        """
        import akshare as ak
        return self._fetch_first_ok(
            "industry_board",
            [
                ("东财", ak.stock_board_industry_name_em, True),
                ("新浪", lambda: _sina_sector_to_em_schema(ak.stock_sector_spot(indicator="新浪行业"))),
            ],
            use_cache=False,
        )

    def industry_cons(self, board: str) -> pd.DataFrame:
        """行业板块成分股。含 名称,涨跌幅,成交额。

        东财失败时回退新浪:按板块名反查 label 再下钻(见 _sina_industry_cons)。
        两源共用缓存键,任一成功即缓存。
        """
        import akshare as ak
        return self._fetch_first_ok(
            f"industry_cons:{board}",
            [
                ("东财", lambda: ak.stock_board_industry_cons_em(symbol=board), True),
                ("新浪", lambda: _sina_industry_cons(board)),
            ],
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

        数据源优先级:Baostock(自有服务器最稳,仅沪深)→ 新浪 → 东财。北交所
        (920/8xx/4xx)Baostock/新浪均不支持,仅能尝试东财。各源结果归一化为东财
        列结构,共用一个缓存键:任一源成功即写入,下次直接命中不再触网。
        """
        import akshare as ak
        key = f"daily:{code}:{start}:{end}"
        sina_sym = _sina_symbol(code)
        em_loader = lambda: ak.stock_zh_a_hist(
            symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"
        )
        if sina_sym.startswith("bj"):
            # 北交所:免费源 Baostock/新浪 均不覆盖,仅东财可能有
            sources = [("东财", em_loader, True)]
        else:
            sources = []
            if _baostock_ready():  # 未安装 baostock 则直接跳过该源,不逐股报错
                sources.append(("baostock", lambda: _baostock_daily(code, start, end)))
            sources.append(("新浪", lambda: _sina_daily_to_em_schema(
                ak.stock_zh_a_daily(symbol=sina_sym, start_date=start, end_date=end, adjust="qfq"))))
            sources.append(("东财", em_loader, True))
        return self._fetch_first_ok(key, sources)
