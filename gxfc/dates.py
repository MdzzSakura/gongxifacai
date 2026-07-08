"""日期口径工具:库内 'YYYY-MM-DD',接口侧 'YYYYMMDD',互转 + 季度末推导。

独立成模块的目的:screen(严格离线)与 ingest(联网)都需要这些函数,
下沉到零依赖的公共层,避免 screen 为借一个日期函数传递性 import fetcher。
"""
from datetime import datetime


def dash(date: str) -> str:
    """'YYYYMMDD' 或 'YYYY-MM-DD' → 'YYYY-MM-DD'(库内统一口径)。"""
    d = str(date).strip()
    if len(d) == 8 and d.isdigit():
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return d


def ymd(date: str) -> str:
    """任意口径 → 'YYYYMMDD'(fetcher 侧口径)。"""
    return dash(date).replace("-", "")


def derive_quarter_end(date: str) -> str:
    """按目标日推导业绩预告对应的季度末(最近一个 ≤ 目标日的季末,'YYYYMMDD')。"""
    d = datetime.strptime(ymd(date), "%Y%m%d")
    for m, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
        q = datetime(d.year, m, day)
        if q <= d:
            return q.strftime("%Y%m%d")
    return f"{d.year - 1}1231"
