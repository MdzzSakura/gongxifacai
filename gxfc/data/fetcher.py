"""数据抓取封装(多源)。项目内唯一联网入口,接口/源变动只改这里。

通用 fetch 提供"失败重试(指数退避)"语义;_fetch_first_ok 按优先级在多个数据源
间自动回退。各源结果统一归一化为东财列结构,对下游透明;唯一例外是成交量单位:
东财原始为"手",本层统一换算为"股"(baostock/新浪原生即股),保证 daily 序列
跨源一致。

源档案(_SOURCE_PROFILE)集中管理每个源的节流间隔与是否东财:东财对突发请求
敏感,间隔最长且带随机抖动(等间隔请求最易被识别为爬虫);baostock 自有服务器
可放开到 0.1s。熔断分两层:东财连接被掐(RemoteDisconnected)立即熔断本轮;
非东财源连续失败 3 次也临时熔断本轮,避免对已挂的源空转重试。

数据落地由 gxfc.ingest 负责(写 DuckDB),本层不做缓存。
"""
import contextlib
import io
import logging
import random
import time
from typing import Callable, List, Optional, Tuple

import pandas as pd

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

# 每个源的请求节流间隔(秒)与是否东财系。东财间隔取 None 表示用实例默认
# (构造参数 min_interval,盘后批量采集可调大)。
_SOURCE_PROFILE = {
    "baostock": {"interval": 0.1, "em": False},
    "新浪": {"interval": 0.5, "em": False},
    "东财": {"interval": None, "em": True},
    "腾讯": {"interval": 0.5, "em": False},
}

# 非东财源连续失败达到该次数,本轮运行内熔断该源(东财有专门的连接级熔断)
_SOURCE_FAIL_LIMIT = 3


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


def _baostock_trade_dates(start: str, end: str) -> pd.DataFrame:
    """Baostock 交易日历,返回单列 calendar_date('YYYY-MM-DD',仅交易日)。"""
    import baostock as bs
    _ensure_baostock_login()
    with contextlib.redirect_stdout(io.StringIO()):
        rs = bs.query_trade_dates(start_date=start, end_date=end)
        if rs.error_code != "0":
            raise RuntimeError(f"baostock 交易日历查询失败:{rs.error_msg}")
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
    df = pd.DataFrame(rows, columns=rs.fields)
    if df.empty:
        raise RuntimeError("baostock 交易日历为空")
    trading = df[df["is_trading_day"] == "1"]
    return pd.DataFrame({"calendar_date": trading["calendar_date"].tolist()})


def _sina_trade_dates(start: str, end: str) -> pd.DataFrame:
    """新浪交易日历(tool_trade_date_hist_sina 覆盖历史与未来),过滤到区间。"""
    import akshare as ak
    df = ak.tool_trade_date_hist_sina()
    dates = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
    keep = dates[(dates >= start) & (dates <= end)]
    if keep.empty:
        raise RuntimeError(f"新浪交易日历区间无数据:{start}~{end}")
    return pd.DataFrame({"calendar_date": keep.tolist()})


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


def _em_daily_to_std_volume(df: pd.DataFrame) -> pd.DataFrame:
    """东财日K成交量 手→股(×100),与 baostock/新浪(原生股)统一。"""
    out = df.copy()
    if "成交量" in out.columns:
        out["成交量"] = pd.to_numeric(out["成交量"], errors="coerce") * 100
    return out


# 腾讯日K(stock_zh_a_hist_tx)列名映射:其 amount 列实为成交量(单位手),
# 接口不提供成交额;仅覆盖沪深,北交所三种复权均无数据
_TX_DAILY_RENAME = {"date": "日期", "open": "开盘", "close": "收盘",
                    "high": "最高", "low": "最低", "amount": "成交量"}


def _tx_daily_to_em_schema(df: pd.DataFrame) -> pd.DataFrame:
    """腾讯日K归一化为东财列结构:成交量手转股,成交额置 NULL(消费方判空)。"""
    out = df.rename(columns=_TX_DAILY_RENAME)
    if "日期" in out.columns:
        out["日期"] = pd.to_datetime(out["日期"]).dt.strftime("%Y-%m-%d")
    if "成交量" in out.columns:
        out["成交量"] = pd.to_numeric(out["成交量"], errors="coerce") * 100
    out["成交额"] = None
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


# 全市场日K快照(采集"快照一次成型"用):统一输出列。收盘即最新价;昨收用于
# 除权检测(除权日交易所口径的昨收=除权参考价,与库内前收比对即可发现除权)。
_DAILY_SNAPSHOT_COLS = ["代码", "名称", "今开", "最高", "最低", "收盘", "昨收",
                        "成交量", "成交额", "换手率"]


def _em_spot_to_daily_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    """东财全市场快照 → 日K快照标准列。成交量 手→股。"""
    out = df.rename(columns={"最新价": "收盘"}).copy()
    out["成交量"] = pd.to_numeric(out["成交量"], errors="coerce") * 100
    missing = [c for c in _DAILY_SNAPSHOT_COLS if c not in out.columns]
    if missing:
        raise ValueError(f"东财快照缺列:{missing}")
    return out[_DAILY_SNAPSHOT_COLS]


def _sina_spot_to_daily_snapshot(df: pd.DataFrame) -> pd.DataFrame:
    """新浪全市场快照 → 日K快照标准列。代码去前缀;无换手率列置 NULL。"""
    out = df.rename(columns={"最新价": "收盘"}).copy()
    out["代码"] = out["代码"].astype(str).str.replace(r"^[A-Za-z]+", "", regex=True)
    if "换手率" not in out.columns:
        out["换手率"] = None
    missing = [c for c in _DAILY_SNAPSHOT_COLS if c not in out.columns]
    if missing:
        raise ValueError(f"新浪快照缺列:{missing}")
    return out[_DAILY_SNAPSHOT_COLS]


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
    def __init__(self, retries: int = 3, min_interval: float = 0.8):
        if retries < 1:
            raise ValueError("retries 必须 >= 1")
        self._retries = retries
        # 东财等数据源对突发请求敏感,强制任意两次真实请求之间至少间隔 min_interval 秒
        self._min_interval = min_interval
        self._last_call = 0.0
        # 熔断标志:一旦探测到东财行情连接被风控掐断,本次运行后续有兜底的接口
        # 直接走新浪,不再对已知挂掉的东财反复重试,避免拖慢与刷屏。
        self._eastmoney_down = False
        # 每源健康度:{源名: {"ok": 成功数, "fail": 失败数, "consec": 连续失败数}}
        # 非东财源 consec 达 _SOURCE_FAIL_LIMIT 即本轮熔断;采集摘要用于观测。
        self.health: dict = {}
        # 最近一次 _fetch_first_ok 成功命中的源名,供采集台账记录实际来源
        self.last_source: str = ""

    def _throttle(self, interval: Optional[float] = None) -> None:
        """请求节流:距上次真实请求不足间隔则补足等待,并加 ±30% 随机抖动摊平指纹。"""
        base = self._min_interval if interval is None else interval
        if base <= 0:
            return
        target = base * random.uniform(0.7, 1.3)
        elapsed = time.monotonic() - self._last_call
        if elapsed < target:
            time.sleep(target - elapsed)
        self._last_call = time.monotonic()

    def _health(self, name: str) -> dict:
        return self.health.setdefault(name, {"ok": 0, "fail": 0, "consec": 0})

    def _mark(self, name: str, ok: bool) -> None:
        h = self._health(name)
        if ok:
            h["ok"] += 1
            h["consec"] = 0
        else:
            h["fail"] += 1
            h["consec"] += 1

    def _source_down(self, name: str, is_em: bool) -> bool:
        """该源本轮是否已熔断:东财看连接级熔断标志,其余看连续失败次数。"""
        if is_em and self._eastmoney_down:
            return True
        return self._health(name)["consec"] >= _SOURCE_FAIL_LIMIT

    def fetch(
        self,
        key: str,
        loader: Callable[[], pd.DataFrame],
        retries: Optional[int] = None,
        interval: Optional[float] = None,
    ) -> pd.DataFrame:
        """失败重试(指数退避)。retries 不传则用实例默认;有兜底源的调用
        可传 retries=1 让主源快速失败,把重试预算留给真正可用的回退源。"""
        attempts = retries if retries is not None else self._retries
        last_err = None
        for attempt in range(1, attempts + 1):
            try:
                self._throttle(interval)
                return loader()
            except Exception as err:  # AKShare 抛出的异常类型不固定,统一兜底
                last_err = err
                logger.warning("抓取 %s 第 %d 次失败:%s", key, attempt, err)
                if attempt < attempts:
                    # 指数退避(1/2/4...秒,封顶8秒),给限流恢复留时间
                    time.sleep(min(8.0, 2.0 ** (attempt - 1)))
        raise last_err

    def _fetch_first_ok(self, key: str, sources: List[Tuple[str, Callable]]) -> pd.DataFrame:
        """按优先级依次尝试多个数据源,首个成功即返回。

        sources 为有序列表,每项 (源名, loader);源名须在 _SOURCE_PROFILE 注册,
        据此取节流间隔与东财标记。
        东财源:熔断后跳过;只试 1 次(易风控,有后续兜底则不值得多试);失败且为
        连接被拒(RemoteDisconnected 等)时触发熔断,本次运行后续跳过所有东财源。
        非东财源:连续失败 3 次本轮熔断;作为最后兜底时用默认 retries(值得多试),
        否则只试 1 次。全部失败抛最后一个异常,由调用方降级跳过。
        """
        last_err: Optional[Exception] = None
        n = len(sources)
        for i, (name, loader) in enumerate(sources):
            profile = _SOURCE_PROFILE[name]
            is_em = profile["em"]
            if self._source_down(name, is_em):
                continue
            is_last = i == n - 1
            retries = None if (is_last and not is_em) else 1
            try:
                df = self.fetch(key, loader, retries=retries, interval=profile["interval"])
                self._mark(name, ok=True)
                self.last_source = name
                return df
            except Exception as err:
                last_err = err
                self._mark(name, ok=False)
                if is_em and _is_conn_error(err):
                    self._eastmoney_down = True
                    logger.warning("东财行情连接被风控掐断,本次运行后续跳过东财源")
                elif not is_em and self._health(name)["consec"] == _SOURCE_FAIL_LIMIT:
                    logger.warning("%s 源连续失败 %d 次,本轮熔断", name, _SOURCE_FAIL_LIMIT)
                logger.warning("%s 源 %s 失败:%s", name, key, err)
        if last_err is None:
            raise RuntimeError(f"{key}:无可用数据源(全部已熔断)")
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
        """全市场 A 股实时快照(东财主、新浪兜底),原始列结构。含 涨跌幅 列。"""
        import akshare as ak
        return self._fetch_first_ok(
            "spot",
            [("东财", ak.stock_zh_a_spot_em), ("新浪", ak.stock_zh_a_spot)],
        )

    def daily_snapshot(self) -> pd.DataFrame:
        """全市场日K快照:收盘后一次请求拿到全部股票的当日 OHLCV。

        统一列:代码(6位)/名称/今开/最高/最低/收盘/昨收/成交量(股)/成交额/换手率。
        东财主源(含换手率)、新浪兜底(换手率为 NULL)。昨收供除权检测。
        采集"快照一次成型"的数据基础:每日增量 1 次请求替代 ~5000 次逐股拉取。
        """
        import akshare as ak
        return self._fetch_first_ok(
            "daily_snapshot",
            [
                ("东财", lambda: _em_spot_to_daily_snapshot(ak.stock_zh_a_spot_em())),
                ("新浪", lambda: _sina_spot_to_daily_snapshot(ak.stock_zh_a_spot())),
            ],
        )

    def trade_dates(self, start: str, end: str) -> List[str]:
        """[start, end] 区间交易日('YYYY-MM-DD' 升序)。Baostock 主、新浪兜底。

        start/end 形如 '2026-01-01'(两源均支持未来日期,可拉全年日历)。
        """
        sources: List[Tuple[str, Callable]] = []
        if _baostock_ready():
            sources.append(("baostock", lambda: _baostock_trade_dates(start, end)))
        sources.append(("新浪", lambda: _sina_trade_dates(start, end)))
        df = self._fetch_first_ok(f"trade_dates:{start}:{end}", sources)
        return sorted(df["calendar_date"].astype(str).tolist())

    def market_spot(self) -> pd.DataFrame:
        """新浪全市场实时快照(~5500 只,含北交所),用于全市场粗筛。

        归一化为 代码(去前缀6位)/名称/涨跌幅/最新价/成交额。只走新浪(列稳定、
        含北交所、批量接口抗限流)。
        """
        import akshare as ak
        return _normalize_market_spot(self.fetch("market_spot", ak.stock_zh_a_spot))

    def industry_board(self) -> pd.DataFrame:
        """东财行业板块实时行情。列含:板块名称,涨跌幅,领涨股票 等。

        东财限流/断连时回退新浪行业榜 stock_sector_spot,归一化为东财列结构。
        注意:新浪行业分类口径与东财不同,回退期板块名称会随之切换。
        """
        import akshare as ak
        return self._fetch_first_ok(
            "industry_board",
            [
                ("东财", ak.stock_board_industry_name_em),
                ("新浪", lambda: _sina_sector_to_em_schema(ak.stock_sector_spot(indicator="新浪行业"))),
            ],
        )

    def industry_cons(self, board: str) -> pd.DataFrame:
        """行业板块成分股。含 名称,涨跌幅,成交额。

        东财失败时回退新浪:按板块名反查 label 再下钻(见 _sina_industry_cons)。
        """
        import akshare as ak
        return self._fetch_first_ok(
            f"industry_cons:{board}",
            [
                ("东财", lambda: ak.stock_board_industry_cons_em(symbol=board)),
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

        数据源优先级:Baostock(自有服务器最稳,仅沪深)→ 新浪 → 东财 → 腾讯
        (兜底,无成交额列)。北交所(920/8xx/4xx)Baostock/新浪/腾讯均不覆盖,
        仅能尝试东财。各源结果归一化为东财列结构,成交量统一为"股"(东财/
        腾讯原始为手,已换算)。
        """
        import akshare as ak
        key = f"daily:{code}:{start}:{end}"
        sina_sym = _sina_symbol(code)
        em_loader = lambda: _em_daily_to_std_volume(ak.stock_zh_a_hist(
            symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"
        ))
        if sina_sym.startswith("bj"):
            # 北交所:免费源 Baostock/新浪/腾讯 均不覆盖,仅东财可能有
            sources = [("东财", em_loader)]
        else:
            sources = []
            if _baostock_ready():  # 未安装 baostock 则直接跳过该源,不逐股报错
                sources.append(("baostock", lambda: _baostock_daily(code, start, end)))
            sources.append(("新浪", lambda: _sina_daily_to_em_schema(
                ak.stock_zh_a_daily(symbol=sina_sym, start_date=start, end_date=end, adjust="qfq"))))
            sources.append(("东财", em_loader))
            sources.append(("腾讯", lambda: _tx_daily_to_em_schema(ak.stock_zh_a_hist_tx(
                symbol=sina_sym, start_date=start, end_date=end, adjust="qfq"))))
        return self._fetch_first_ok(key, sources)
