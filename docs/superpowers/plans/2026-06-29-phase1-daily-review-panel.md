# 阶段1:每日复盘面板 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现"每日盘后复盘面板"——盘后一次运行,输出当日市场情绪温度计 + 板块涨幅榜 + 净利润断层候选,呈现为控制台表格并落地 CSV。

**Architecture:** 单向分层(数据层 → 因子层 → 复盘层 → 主流程)。数据层是唯一的网络/IO 边界(AKShare + SQLite 缓存),因子层全是纯函数(输入 DataFrame,输出结构化结果),复盘层把三类结果组装成面板。纯函数用固定样本(fixture)做单元测试,完全不依赖网络;只有一个端到端冒烟测试会真连 AKShare,默认跳过。

**Tech Stack:** Python 3.10+、AKShare(数据)、pandas(计算)、PyYAML(配置)、tabulate(控制台表格)、pytest(测试)、SQLite(标准库 sqlite3,缓存)。

## Global Constraints

- Python ≥ 3.10;所有源码与测试文件 UTF-8 无 BOM。
- 包根目录为 `gxfc/`(gongxifacai 缩写),测试在 `tests/`,从仓库根运行 `pytest`。
- 一切注释、文档、提交信息、日志、错误提示一律简体中文;仅代码标识符用英文。
- 日志用标准库 `logging`,禁止 `print` 调试输出(面板正式输出除外,走 stdout 渲染)。
- AKShare 接口名可能随版本变动;所有 AKShare 调用集中在 `gxfc/data/fetcher.py` 一处封装,接口变动只改这一个文件。
- "业绩超预期"无券商一致预期数据,统一用"单季度/预告净利润同比增速 ≥ 阈值"作为 proxy,面板标注"proxy口径"。
- 阈值集中在 `config/strategy.yaml`,因子函数以参数接收并带默认值,主流程从 yaml 注入。
- 每个任务遵循 TDD:先写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交。提交信息用 `feat:`/`test:`/`chore:` 前缀 + 简体中文描述。

---

### Task 1: 项目骨架与依赖

**Files:**
- Create: `requirements.txt`
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `config/strategy.yaml`
- Create: `gxfc/__init__.py`
- Create: `gxfc/data/__init__.py`
- Create: `gxfc/factors/__init__.py`
- Create: `gxfc/review/__init__.py`
- Create: `tests/__init__.py`
- Test: `tests/test_smoke.py`

**Interfaces:**
- Consumes: 无
- Produces: 可导入的包 `gxfc`;`pytest` 可从仓库根运行;`config/strategy.yaml` 提供阈值。

- [ ] **Step 1: 写依赖与项目配置文件**

`requirements.txt`:
```
akshare>=1.12
pandas>=1.5
PyYAML>=6.0
tabulate>=0.9
pytest>=7.0
```

`pyproject.toml`(让 pytest 能从仓库根 import gxfc):
```toml
[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
markers = [
    "network: 需要真实访问 AKShare 网络的测试(默认可跳过)",
]
```

`.gitignore`:
```
__pycache__/
*.pyc
.pytest_cache/
*.db
out/
.venv/
venv/
```

- [ ] **Step 2: 写阈值配置 `config/strategy.yaml`**

```yaml
# 市场情绪阈值
emotion:
  hot_up_count: 4500        # 上涨家数≥此值视为日内情绪高点(谨慎)
  cold_up_count: 800        # 上涨家数≤此值视为接近冰点(关注机会)
  volume_up_ratio: 1.15     # 今日成交额/近5日均额 ≥ 此值=放量
  volume_down_ratio: 0.85   # ≤ 此值=缩量

# 板块榜
sector:
  top_n: 10                 # 取涨幅前 N 个板块
  core_top_n: 5             # 每个板块取核心成分股数

# 净利润断层
profit_fault:
  growth_threshold: 50.0    # 预告/快报净利润同比增速≥此值(proxy超预期,单位%)
```

- [ ] **Step 3: 写各包的 `__init__.py`(空文件)与冒烟测试**

`gxfc/__init__.py`、`gxfc/data/__init__.py`、`gxfc/factors/__init__.py`、`gxfc/review/__init__.py`、`tests/__init__.py` 均为空文件。

`tests/test_smoke.py`:
```python
def test_包可导入():
    import gxfc
    import gxfc.data
    import gxfc.factors
    import gxfc.review
    assert gxfc is not None


def test_配置可加载():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path("config/strategy.yaml").read_text(encoding="utf-8"))
    assert cfg["profit_fault"]["growth_threshold"] == 50.0
    assert cfg["sector"]["top_n"] == 10
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pip install -r requirements.txt && pytest tests/test_smoke.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add requirements.txt pyproject.toml .gitignore config/ gxfc/ tests/
git commit -m "chore: 初始化项目骨架、依赖与阈值配置"
```

---

### Task 2: SQLite 缓存层

**Files:**
- Create: `gxfc/data/cache.py`
- Test: `tests/test_cache.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class DataFrameCache(db_path: str)`
  - `DataFrameCache.get(key: str) -> pandas.DataFrame | None` — 命中返回 DataFrame,未命中返回 None
  - `DataFrameCache.set(key: str, df: pandas.DataFrame) -> None` — 覆盖写入
  - 缓存键约定:调用方用 `"<接口名>:<参数>"`(如 `"sector_industry:20260629"`)

- [ ] **Step 1: 写失败测试**

`tests/test_cache.py`:
```python
import pandas as pd
from gxfc.data.cache import DataFrameCache


def test_未命中返回None(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    assert cache.get("不存在的键") is None


def test_写入后能读回相同数据(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    df = pd.DataFrame({"代码": ["000001", "000002"], "涨跌幅": [1.5, -2.0]})
    cache.set("sector:20260629", df)
    got = cache.get("sector:20260629")
    pd.testing.assert_frame_equal(got, df)


def test_同键覆盖写入(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    cache.set("k", pd.DataFrame({"a": [1]}))
    cache.set("k", pd.DataFrame({"a": [2, 3]}))
    got = cache.get("k")
    assert list(got["a"]) == [2, 3]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_cache.py -v`
Expected: FAIL，报 `ModuleNotFoundError: No module named 'gxfc.data.cache'`

- [ ] **Step 3: 写最小实现**

`gxfc/data/cache.py`:
```python
"""基于 SQLite 的 DataFrame 缓存,避免重复访问 AKShare。

缓存以键值对存储:键是调用方约定的字符串(如 "sector:20260629"),
值是 DataFrame 序列化后的 JSON 文本。同键 set 覆盖旧值。
"""
import sqlite3
from io import StringIO

import pandas as pd


class DataFrameCache:
    def __init__(self, db_path: str):
        self._db_path = db_path
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS df_cache ("
                "cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def get(self, key: str):
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT payload FROM df_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return pd.read_json(StringIO(row[0]), orient="split")

    def set(self, key: str, df: pd.DataFrame) -> None:
        payload = df.to_json(orient="split")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO df_cache (cache_key, payload) VALUES (?, ?)",
                (key, payload),
            )
```

注意:`to_json(orient="split")` 会丢失 dtype,测试里 `assert_frame_equal` 对 `涨跌幅` 浮点能对上;若后续出现 dtype 不一致,在读回处用 `orient="split"` 配合显式列类型转换处理。本阶段样本均为字符串/浮点,无需额外处理。

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_cache.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/data/cache.py tests/test_cache.py
git commit -m "feat: 新增 SQLite DataFrame 缓存层"
```

---

### Task 3: AKShare 数据抓取封装(带重试与缓存)

**Files:**
- Create: `gxfc/data/fetcher.py`
- Test: `tests/test_fetcher.py`

**Interfaces:**
- Consumes: `gxfc.data.cache.DataFrameCache`
- Produces:
  - `class Fetcher(cache: DataFrameCache | None = None, retries: int = 3)`
  - `Fetcher.fetch(key: str, loader: Callable[[], pandas.DataFrame], use_cache: bool = True) -> pandas.DataFrame`
    —— 通用抓取:命中缓存直接返回;否则调用 `loader()`,失败重试 `retries` 次,成功后写缓存
  - 具体业务方法(内部各自调用 `self.fetch` + 对应 AKShare 接口):
    - `Fetcher.market_activity() -> DataFrame`(列 `item,value`,来自 `ak.stock_market_activity_legu`)
    - `Fetcher.zt_pool(date: str) -> DataFrame`(涨停池,含 `连板数`,来自 `ak.stock_zt_pool_em`)
    - `Fetcher.industry_board() -> DataFrame`(东财行业板块,含 `板块名称,涨跌幅,领涨股`,来自 `ak.stock_board_industry_name_em`)
    - `Fetcher.industry_cons(board: str) -> DataFrame`(板块成分股,含 `名称,涨跌幅,成交额`,来自 `ak.stock_board_industry_cons_em`)
    - `Fetcher.yjyg(date: str) -> DataFrame`(业绩预告,含 `股票简称,预测净利润-同比增长`,来自 `ak.stock_yjyg_em`)
    - `Fetcher.stock_daily(code: str, start: str, end: str) -> DataFrame`(前复权日K,含 `日期,开盘,最高`,来自 `ak.stock_zh_a_hist`)

- [ ] **Step 1: 写失败测试(只测通用 fetch 的重试/缓存逻辑,不连网络)**

`tests/test_fetcher.py`:
```python
import pandas as pd
import pytest
from gxfc.data.cache import DataFrameCache
from gxfc.data.fetcher import Fetcher


def test_loader成功时返回数据并写入缓存(tmp_path):
    cache = DataFrameCache(str(tmp_path / "t.db"))
    fetcher = Fetcher(cache=cache)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        return pd.DataFrame({"a": [1]})

    got = fetcher.fetch("k1", loader)
    assert list(got["a"]) == [1]
    assert calls["n"] == 1
    # 第二次应命中缓存,不再调用 loader
    fetcher.fetch("k1", loader)
    assert calls["n"] == 1


def test_loader失败时按次数重试(tmp_path):
    fetcher = Fetcher(cache=None, retries=3)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        raise RuntimeError("模拟网络错误")

    with pytest.raises(RuntimeError):
        fetcher.fetch("k2", loader)
    assert calls["n"] == 3


def test_重试中途成功则返回(tmp_path):
    fetcher = Fetcher(cache=None, retries=3)
    calls = {"n": 0}

    def loader():
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("第一次失败")
        return pd.DataFrame({"ok": [1]})

    got = fetcher.fetch("k3", loader)
    assert calls["n"] == 2
    assert list(got["ok"]) == [1]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_fetcher.py -v`
Expected: FAIL，报 `No module named 'gxfc.data.fetcher'`

- [ ] **Step 3: 写最小实现**

`gxfc/data/fetcher.py`:
```python
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
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_fetcher.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/data/fetcher.py tests/test_fetcher.py
git commit -m "feat: 新增 AKShare 抓取封装(缓存命中优先+失败重试)"
```

---

### Task 4: 市场情绪因子

**Files:**
- Create: `gxfc/factors/market_emotion.py`
- Test: `tests/test_market_emotion.py`

**Interfaces:**
- Consumes: 无(纯函数,输入 DataFrame)
- Produces:
  - `@dataclass MarketEmotion`:字段 `up_count:int, down_count:int, limit_up:int, limit_down:int, broken_board_rate:float, highest_streak:int, volume_state:str, sentiment_hint:str`
  - `compute_market_emotion(activity_df, zt_pool_df, today_amount=None, avg5_amount=None, hot_up_count=4500, cold_up_count=800, volume_up_ratio=1.15, volume_down_ratio=0.85) -> MarketEmotion`
    - `activity_df`:列 `item,value`(来自 `Fetcher.market_activity`)
    - `zt_pool_df`:含列 `连板数`(来自 `Fetcher.zt_pool`)
    - `today_amount`/`avg5_amount`:两市成交额与近5日均额(亿元),缺失时 `volume_state="数据不足"`

- [ ] **Step 1: 写失败测试**

`tests/test_market_emotion.py`:
```python
import pandas as pd
from gxfc.factors.market_emotion import compute_market_emotion


def _activity(up, down, limit_up, limit_down, broken):
    return pd.DataFrame(
        {
            "item": ["上涨", "下跌", "涨停", "跌停", "炸板"],
            "value": [up, down, limit_up, limit_down, broken],
        }
    )


def test_基础计数与炸板率():
    activity = _activity(3000, 1800, 40, 5, 10)
    zt = pd.DataFrame({"连板数": [1, 2, 5, 3]})
    e = compute_market_emotion(activity, zt)
    assert e.up_count == 3000
    assert e.down_count == 1800
    assert e.limit_up == 40
    assert e.limit_down == 5
    # 炸板率 = 炸板/(涨停+炸板) = 10/(40+10) = 0.2
    assert abs(e.broken_board_rate - 0.2) < 1e-9
    assert e.highest_streak == 5


def test_情绪高点提示():
    activity = _activity(4600, 300, 80, 2, 5)
    zt = pd.DataFrame({"连板数": [1]})
    e = compute_market_emotion(activity, zt)
    assert "情绪高点" in e.sentiment_hint


def test_接近冰点提示():
    activity = _activity(700, 4000, 10, 60, 8)
    zt = pd.DataFrame({"连板数": [1]})
    e = compute_market_emotion(activity, zt)
    assert "冰点" in e.sentiment_hint


def test_量能放量判定():
    activity = _activity(3000, 1800, 40, 5, 10)
    zt = pd.DataFrame({"连板数": [1]})
    e = compute_market_emotion(activity, zt, today_amount=12000, avg5_amount=10000)
    assert e.volume_state == "放量"


def test_量能数据不足():
    activity = _activity(3000, 1800, 40, 5, 10)
    zt = pd.DataFrame({"连板数": [1]})
    e = compute_market_emotion(activity, zt)
    assert e.volume_state == "数据不足"


def test_涨停池为空时最高板为0():
    activity = _activity(3000, 1800, 0, 5, 0)
    e = compute_market_emotion(activity, pd.DataFrame({"连板数": []}))
    assert e.highest_streak == 0
    assert e.broken_board_rate == 0.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_market_emotion.py -v`
Expected: FAIL，报 `No module named 'gxfc.factors.market_emotion'`

- [ ] **Step 3: 写最小实现**

`gxfc/factors/market_emotion.py`:
```python
"""市场情绪因子:把市场赚钱效应与涨停池算成情绪指标。

设计意图:盘后一眼看清当日情绪冷热——涨跌家数判断广度,炸板率判断
追涨风险,最高板判断市场高度,量能判断持续性。阈值来自用户文档经验值。
"""
import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd


@dataclass
class MarketEmotion:
    up_count: int
    down_count: int
    limit_up: int
    limit_down: int
    broken_board_rate: float
    highest_streak: int
    volume_state: str
    sentiment_hint: str


def _to_int(value) -> int:
    """从 AKShare 的 value(可能是 '1234' 或 1234 或 '12家')里抠出整数。"""
    if value is None:
        return 0
    digits = re.sub(r"[^0-9-]", "", str(value))
    return int(digits) if digits not in ("", "-") else 0


def compute_market_emotion(
    activity_df: pd.DataFrame,
    zt_pool_df: pd.DataFrame,
    today_amount: Optional[float] = None,
    avg5_amount: Optional[float] = None,
    hot_up_count: int = 4500,
    cold_up_count: int = 800,
    volume_up_ratio: float = 1.15,
    volume_down_ratio: float = 0.85,
) -> MarketEmotion:
    table = {str(row["item"]): row["value"] for _, row in activity_df.iterrows()}
    up = _to_int(table.get("上涨"))
    down = _to_int(table.get("下跌"))
    limit_up = _to_int(table.get("涨停"))
    limit_down = _to_int(table.get("跌停"))
    broken = _to_int(table.get("炸板"))

    denom = limit_up + broken
    broken_rate = (broken / denom) if denom > 0 else 0.0

    if zt_pool_df is not None and "连板数" in zt_pool_df.columns and len(zt_pool_df) > 0:
        streaks = pd.to_numeric(zt_pool_df["连板数"], errors="coerce").dropna()
        highest = int(streaks.max()) if len(streaks) > 0 else 0
    else:
        highest = 0

    if today_amount is None or avg5_amount is None or avg5_amount == 0:
        volume_state = "数据不足"
    else:
        ratio = today_amount / avg5_amount
        if ratio >= volume_up_ratio:
            volume_state = "放量"
        elif ratio <= volume_down_ratio:
            volume_state = "缩量"
        else:
            volume_state = "平量"

    if up >= hot_up_count:
        hint = "情绪高点(追涨谨慎)"
    elif up <= cold_up_count:
        hint = "接近冰点(关注机会)"
    else:
        hint = "中性"

    return MarketEmotion(
        up_count=up,
        down_count=down,
        limit_up=limit_up,
        limit_down=limit_down,
        broken_board_rate=broken_rate,
        highest_streak=highest,
        volume_state=volume_state,
        sentiment_hint=hint,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_market_emotion.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/factors/market_emotion.py tests/test_market_emotion.py
git commit -m "feat: 新增市场情绪因子(涨跌家数/炸板率/最高板/量能/情绪提示)"
```

---

### Task 5: 板块因子(涨幅榜 + 核心成分股)

**Files:**
- Create: `gxfc/factors/sector.py`
- Test: `tests/test_sector.py`

**Interfaces:**
- Consumes: 无(纯函数,输入 DataFrame)
- Produces:
  - `rank_sectors(board_df, top_n=10) -> pandas.DataFrame` —— 按 `涨跌幅` 降序取前 `top_n`,保留列 `板块名称,涨跌幅,领涨股`(列缺失时容错保留存在的列),重置索引
  - `core_stocks(cons_df, core_top_n=5) -> pandas.DataFrame` —— 成分股按 `涨跌幅` 降序取前 `core_top_n`,保留 `名称,涨跌幅,成交额`,重置索引

- [ ] **Step 1: 写失败测试**

`tests/test_sector.py`:
```python
import pandas as pd
from gxfc.factors.sector import rank_sectors, core_stocks


def test_板块按涨幅降序取前N():
    board = pd.DataFrame(
        {
            "板块名称": ["煤炭", "电力", "稀土", "银行"],
            "涨跌幅": [1.2, 5.6, 3.3, -0.4],
            "领涨股": ["A", "B", "C", "D"],
        }
    )
    out = rank_sectors(board, top_n=2)
    assert list(out["板块名称"]) == ["电力", "稀土"]
    assert out.index.tolist() == [0, 1]


def test_板块top_n超过数量时返回全部():
    board = pd.DataFrame({"板块名称": ["X"], "涨跌幅": [1.0], "领涨股": ["a"]})
    out = rank_sectors(board, top_n=10)
    assert len(out) == 1


def test_核心成分股按涨幅降序取前N():
    cons = pd.DataFrame(
        {
            "名称": ["甲", "乙", "丙"],
            "涨跌幅": [2.0, 9.9, 5.0],
            "成交额": [1e8, 5e8, 2e8],
        }
    )
    out = core_stocks(cons, core_top_n=2)
    assert list(out["名称"]) == ["乙", "丙"]
    assert out.index.tolist() == [0, 1]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_sector.py -v`
Expected: FAIL，报 `No module named 'gxfc.factors.sector'`

- [ ] **Step 3: 写最小实现**

`gxfc/factors/sector.py`:
```python
"""板块因子:板块涨幅榜与板块内核心成分股。

设计意图:复刻"先看板块涨幅,再在板块里挑核心票"。板块按涨幅排序定主线,
成分股按涨幅(辅以成交额可读性)排序近似"地位/带动性"。
"""
import pandas as pd

_SECTOR_COLS = ["板块名称", "涨跌幅", "领涨股"]
_CORE_COLS = ["名称", "涨跌幅", "成交额"]


def _keep_existing(df: pd.DataFrame, cols) -> pd.DataFrame:
    existing = [c for c in cols if c in df.columns]
    return df[existing]


def rank_sectors(board_df: pd.DataFrame, top_n: int = 10) -> pd.DataFrame:
    ranked = board_df.sort_values("涨跌幅", ascending=False).head(top_n)
    return _keep_existing(ranked, _SECTOR_COLS).reset_index(drop=True)


def core_stocks(cons_df: pd.DataFrame, core_top_n: int = 5) -> pd.DataFrame:
    ranked = cons_df.sort_values("涨跌幅", ascending=False).head(core_top_n)
    return _keep_existing(ranked, _CORE_COLS).reset_index(drop=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_sector.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/factors/sector.py tests/test_sector.py
git commit -m "feat: 新增板块因子(涨幅榜+核心成分股)"
```

---

### Task 6: 净利润断层扫描

**Files:**
- Create: `gxfc/factors/profit_fault.py`
- Test: `tests/test_profit_fault.py`

**Interfaces:**
- Consumes: 无(纯函数,输入 DataFrame/标量)
- Produces:
  - `detect_gap(daily_df) -> bool` —— `daily_df` 按 `日期` 升序,最后一行 `开盘` > 前一行 `最高` 即跳空缺口;不足2行返回 False
  - `passes_growth(growth_pct, threshold) -> bool` —— `growth_pct` 为同比增速(%),`None`/`NaN` 返回 False
  - `scan_profit_fault(yjyg_df, daily_map, growth_threshold=50.0) -> pandas.DataFrame` ——
    - `yjyg_df`:含 `股票简称,股票代码,预测净利润-同比增长`
    - `daily_map`:`{股票代码: daily_df}`
    - 返回命中(增速达标 且 有跳空缺口)的候选,列 `股票代码,股票简称,同比增长,有跳空`,重置索引

- [ ] **Step 1: 写失败测试**

`tests/test_profit_fault.py`:
```python
import numpy as np
import pandas as pd
from gxfc.factors.profit_fault import detect_gap, passes_growth, scan_profit_fault


def _daily(opens_highs):
    """opens_highs: [(开盘,最高), ...] 按日期升序。"""
    return pd.DataFrame(
        {
            "日期": [f"2026-06-{i+1:02d}" for i in range(len(opens_highs))],
            "开盘": [o for o, _ in opens_highs],
            "最高": [h for _, h in opens_highs],
        }
    )


def test_跳空缺口成立():
    # 最后一行开盘11.0 > 前一行最高10.5
    df = _daily([(10.0, 10.5), (11.0, 11.8)])
    assert detect_gap(df) is True


def test_无跳空缺口():
    df = _daily([(10.0, 10.5), (10.3, 10.8)])
    assert detect_gap(df) is False


def test_不足两行返回False():
    df = _daily([(10.0, 10.5)])
    assert detect_gap(df) is False


def test_增速达标判定():
    assert passes_growth(60.0, 50.0) is True
    assert passes_growth(40.0, 50.0) is False
    assert passes_growth(None, 50.0) is False
    assert passes_growth(np.nan, 50.0) is False


def test_扫描出增速达标且跳空的候选():
    yjyg = pd.DataFrame(
        {
            "股票代码": ["000001", "000002", "000003"],
            "股票简称": ["甲", "乙", "丙"],
            "预测净利润-同比增长": [80.0, 30.0, 120.0],
        }
    )
    daily_map = {
        "000001": _daily([(10.0, 10.5), (11.0, 11.8)]),   # 增速达标+跳空 → 入选
        "000002": _daily([(10.0, 10.5), (11.0, 11.8)]),   # 增速不达标 → 落选
        "000003": _daily([(10.0, 10.5), (10.2, 10.6)]),   # 增速达标但无跳空 → 落选
    }
    out = scan_profit_fault(yjyg, daily_map, growth_threshold=50.0)
    assert list(out["股票代码"]) == ["000001"]
    assert bool(out.iloc[0]["有跳空"]) is True


def test_缺失日K的票被跳过():
    yjyg = pd.DataFrame(
        {"股票代码": ["000009"], "股票简称": ["缺数据"], "预测净利润-同比增长": [99.0]}
    )
    out = scan_profit_fault(yjyg, {}, growth_threshold=50.0)
    assert len(out) == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_profit_fault.py -v`
Expected: FAIL，报 `No module named 'gxfc.factors.profit_fault'`

- [ ] **Step 3: 写最小实现**

`gxfc/factors/profit_fault.py`:
```python
"""净利润断层扫描。

净利润断层 = 业绩超预期 + 次日跳空缺口。受限于 AKShare 无券商一致预期,
"超预期"用"预告净利润同比增速≥阈值"作为 proxy(单季度/预告口径)。
跳空缺口 = 出业绩后下一交易日 今开 > 昨高。
"""
import pandas as pd

_GROWTH_COL = "预测净利润-同比增长"


def detect_gap(daily_df: pd.DataFrame) -> bool:
    if daily_df is None or len(daily_df) < 2:
        return False
    ordered = daily_df.sort_values("日期").reset_index(drop=True)
    today_open = float(ordered.iloc[-1]["开盘"])
    prev_high = float(ordered.iloc[-2]["最高"])
    return today_open > prev_high


def passes_growth(growth_pct, threshold: float) -> bool:
    if growth_pct is None:
        return False
    try:
        value = float(growth_pct)
    except (TypeError, ValueError):
        return False
    if value != value:  # NaN
        return False
    return value >= threshold


def scan_profit_fault(
    yjyg_df: pd.DataFrame, daily_map: dict, growth_threshold: float = 50.0
) -> pd.DataFrame:
    rows = []
    for _, r in yjyg_df.iterrows():
        code = str(r["股票代码"])
        growth = r.get(_GROWTH_COL)
        if not passes_growth(growth, growth_threshold):
            continue
        daily = daily_map.get(code)
        if daily is None:
            continue
        if not detect_gap(daily):
            continue
        rows.append(
            {
                "股票代码": code,
                "股票简称": r.get("股票简称", ""),
                "同比增长": float(growth),
                "有跳空": True,
            }
        )
    return pd.DataFrame(rows, columns=["股票代码", "股票简称", "同比增长", "有跳空"])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_profit_fault.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/factors/profit_fault.py tests/test_profit_fault.py
git commit -m "feat: 新增净利润断层扫描(同比增速proxy+跳空缺口)"
```

---

### Task 7: 每日复盘面板(组装 + 渲染 + CSV)

**Files:**
- Create: `gxfc/review/daily_board.py`
- Test: `tests/test_daily_board.py`

**Interfaces:**
- Consumes: `gxfc.factors.market_emotion.MarketEmotion`、`rank_sectors`/`core_stocks` 的输出 DataFrame、`scan_profit_fault` 的输出 DataFrame
- Produces:
  - `@dataclass DailyBoard`:字段 `date:str, emotion:MarketEmotion, sectors:pandas.DataFrame, candidates:pandas.DataFrame`
  - `render_console(board) -> str` —— 渲染为带标题的多段文本(含 proxy 口径声明),用 `tabulate`
  - `save_csv(board, out_dir) -> list[str]` —— 把 sectors 与 candidates 各存一个 CSV(UTF-8-SIG,便于 Excel 打开),返回写出的文件路径列表

- [ ] **Step 1: 写失败测试**

`tests/test_daily_board.py`:
```python
import pandas as pd
from gxfc.factors.market_emotion import MarketEmotion
from gxfc.review.daily_board import DailyBoard, render_console, save_csv


def _board():
    emotion = MarketEmotion(
        up_count=3000, down_count=1800, limit_up=40, limit_down=5,
        broken_board_rate=0.2, highest_streak=5, volume_state="放量",
        sentiment_hint="中性",
    )
    sectors = pd.DataFrame({"板块名称": ["电力"], "涨跌幅": [5.6], "领涨股": ["B"]})
    candidates = pd.DataFrame(
        {"股票代码": ["000001"], "股票简称": ["甲"], "同比增长": [80.0], "有跳空": [True]}
    )
    return DailyBoard(date="20260629", emotion=emotion, sectors=sectors, candidates=candidates)


def test_渲染包含关键信息():
    text = render_console(_board())
    assert "20260629" in text
    assert "情绪" in text
    assert "电力" in text
    assert "000001" in text
    assert "proxy" in text.lower() or "口径" in text


def test_保存两个csv文件(tmp_path):
    paths = save_csv(_board(), str(tmp_path))
    assert len(paths) == 2
    for p in paths:
        assert p.endswith(".csv")
        content = open(p, encoding="utf-8-sig").read()
        assert len(content) > 0
    # 候选 CSV 内含股票代码
    joined = "".join(open(p, encoding="utf-8-sig").read() for p in paths)
    assert "000001" in joined
    assert "电力" in joined
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_daily_board.py -v`
Expected: FAIL，报 `No module named 'gxfc.review.daily_board'`

- [ ] **Step 3: 写最小实现**

`gxfc/review/daily_board.py`:
```python
"""每日复盘面板:把情绪、板块榜、断层候选组装成可读面板并落地 CSV。

面板回答用户每天盘后最关心的三件事:今天情绪冷热、哪些板块在涨、
哪些票出了净利润断层。候选基于 proxy 口径(同比增速),面板显式声明。
"""
import os
from dataclasses import dataclass

import pandas as pd
from tabulate import tabulate

from gxfc.factors.market_emotion import MarketEmotion


@dataclass
class DailyBoard:
    date: str
    emotion: MarketEmotion
    sectors: pd.DataFrame
    candidates: pd.DataFrame


def render_console(board: DailyBoard) -> str:
    e = board.emotion
    lines = []
    lines.append(f"===== A股每日复盘面板 {board.date} =====")
    lines.append("")
    lines.append("【市场情绪温度计】")
    emotion_rows = [
        ["上涨/下跌家数", f"{e.up_count} / {e.down_count}"],
        ["涨停/跌停家数", f"{e.limit_up} / {e.limit_down}"],
        ["炸板率", f"{e.broken_board_rate:.1%}"],
        ["最高板", f"{e.highest_streak} 板"],
        ["量能状态", e.volume_state],
        ["情绪提示", e.sentiment_hint],
    ]
    lines.append(tabulate(emotion_rows, tablefmt="grid"))
    lines.append("")
    lines.append("【板块涨幅榜】")
    lines.append(tabulate(board.sectors, headers="keys", tablefmt="grid", showindex=False))
    lines.append("")
    lines.append("【净利润断层候选】(proxy口径:预告净利润同比增速,非券商一致预期)")
    if len(board.candidates) > 0:
        lines.append(
            tabulate(board.candidates, headers="keys", tablefmt="grid", showindex=False)
        )
    else:
        lines.append("(当日无达标候选)")
    return "\n".join(lines)


def save_csv(board: DailyBoard, out_dir: str) -> list:
    os.makedirs(out_dir, exist_ok=True)
    sector_path = os.path.join(out_dir, f"sectors_{board.date}.csv")
    cand_path = os.path.join(out_dir, f"candidates_{board.date}.csv")
    board.sectors.to_csv(sector_path, index=False, encoding="utf-8-sig")
    board.candidates.to_csv(cand_path, index=False, encoding="utf-8-sig")
    return [sector_path, cand_path]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_daily_board.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/review/daily_board.py tests/test_daily_board.py
git commit -m "feat: 新增每日复盘面板(组装+控制台渲染+CSV落地)"
```

---

### Task 8: 主流程串联 + 端到端冒烟

**Files:**
- Create: `gxfc/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `Fetcher`、`compute_market_emotion`、`rank_sectors`/`core_stocks`、`scan_profit_fault`、`DailyBoard`/`render_console`/`save_csv`、`config/strategy.yaml`
- Produces:
  - `load_config(path="config/strategy.yaml") -> dict`
  - `build_board(fetcher, date, quarter_end, config, top_codes_limit=30) -> DailyBoard` —— 用注入的 `fetcher` 拉数并组装面板;`top_codes_limit` 限制断层扫描时拉日K的股票数(控制网络量)
  - `run_daily(date, quarter_end, out_dir="out") -> DailyBoard` —— 真实入口:建 Fetcher(带缓存)→ build_board → 打印 + 存 CSV
  - `python -m gxfc.main <date> <quarter_end>` CLI 入口

- [ ] **Step 1: 写失败测试(用假 Fetcher,不连网络)**

`tests/test_main.py`:
```python
import pandas as pd
import pytest
from gxfc.main import build_board, load_config


class FakeFetcher:
    """返回固定样本的假 Fetcher,模拟各接口。"""

    def market_activity(self):
        return pd.DataFrame(
            {"item": ["上涨", "下跌", "涨停", "跌停", "炸板"],
             "value": [3000, 1800, 40, 5, 10]}
        )

    def zt_pool(self, date):
        return pd.DataFrame({"连板数": [1, 2, 5]})

    def industry_board(self):
        return pd.DataFrame(
            {"板块名称": ["电力", "煤炭"], "涨跌幅": [5.6, 1.2], "领涨股": ["B", "A"]}
        )

    def yjyg(self, date):
        return pd.DataFrame(
            {"股票代码": ["000001", "000002"], "股票简称": ["甲", "乙"],
             "预测净利润-同比增长": [80.0, 30.0]}
        )

    def stock_daily(self, code, start, end):
        if code == "000001":
            return pd.DataFrame(
                {"日期": ["2026-06-28", "2026-06-29"], "开盘": [10.0, 11.0],
                 "最高": [10.5, 11.8]}
            )
        return pd.DataFrame(
            {"日期": ["2026-06-28", "2026-06-29"], "开盘": [10.0, 10.2],
             "最高": [10.5, 10.6]}
        )


def test_配置加载():
    cfg = load_config()
    assert cfg["profit_fault"]["growth_threshold"] == 50.0


def test_组装面板包含情绪板块与候选():
    cfg = load_config()
    board = build_board(FakeFetcher(), "20260629", "20260331", cfg)
    assert board.date == "20260629"
    assert board.emotion.up_count == 3000
    assert list(board.sectors["板块名称"])[0] == "电力"
    # 仅 000001 增速达标且跳空
    assert list(board.candidates["股票代码"]) == ["000001"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_main.py -v`
Expected: FAIL，报 `No module named 'gxfc.main'`

- [ ] **Step 3: 写最小实现**

`gxfc/main.py`:
```python
"""主流程:盘后一次运行,拉数 → 算因子 → 组装面板 → 打印 + 存 CSV。

build_board 接受注入的 fetcher(便于用假对象测试);run_daily 是真实入口,
内部构建带 SQLite 缓存的 Fetcher。断层扫描仅对业绩预告里的股票拉日K,
并用 top_codes_limit 限制数量以控制网络请求量。
"""
import logging
import sys
from pathlib import Path

import yaml

from gxfc.data.cache import DataFrameCache
from gxfc.data.fetcher import Fetcher
from gxfc.factors.market_emotion import compute_market_emotion
from gxfc.factors.profit_fault import scan_profit_fault
from gxfc.factors.sector import rank_sectors
from gxfc.review.daily_board import DailyBoard, render_console, save_csv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def load_config(path: str = "config/strategy.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _daily_window(date: str) -> tuple:
    """断层检测只需最近几天日K,这里取该日往前一个月足够覆盖前一交易日。"""
    year = int(date[:4])
    start = f"{year}{date[4:6]}01"  # 当月1日,足够包含前一交易日
    return start, date


def build_board(fetcher, date: str, quarter_end: str, config: dict,
                top_codes_limit: int = 30) -> DailyBoard:
    emo_cfg = config["emotion"]
    sec_cfg = config["sector"]
    pf_cfg = config["profit_fault"]

    activity = fetcher.market_activity()
    zt = fetcher.zt_pool(date)
    emotion = compute_market_emotion(
        activity, zt,
        hot_up_count=emo_cfg["hot_up_count"],
        cold_up_count=emo_cfg["cold_up_count"],
    )

    board_df = fetcher.industry_board()
    sectors = rank_sectors(board_df, top_n=sec_cfg["top_n"])

    yjyg = fetcher.yjyg(quarter_end).head(top_codes_limit)
    start, end = _daily_window(date)
    daily_map = {}
    for _, r in yjyg.iterrows():
        code = str(r["股票代码"])
        try:
            daily_map[code] = fetcher.stock_daily(code, start, end)
        except Exception as err:
            logger.warning("拉取 %s 日K失败,跳过:%s", code, err)
    candidates = scan_profit_fault(yjyg, daily_map, growth_threshold=pf_cfg["growth_threshold"])

    return DailyBoard(date=date, emotion=emotion, sectors=sectors, candidates=candidates)


def run_daily(date: str, quarter_end: str, out_dir: str = "out") -> DailyBoard:
    config = load_config()
    fetcher = Fetcher(cache=DataFrameCache("gxfc_cache.db"))
    board = build_board(fetcher, date, quarter_end, config)
    print(render_console(board))
    paths = save_csv(board, out_dir)
    logger.info("已保存:%s", paths)
    return board


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python -m gxfc.main <交易日YYYYMMDD> <季度末YYYYMMDD>")
        sys.exit(1)
    run_daily(sys.argv[1], sys.argv[2])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_main.py -v`
Expected: 2 passed

- [ ] **Step 5: 运行全量测试**

Run: `pytest -v`
Expected: 全部通过(约 25 个用例)

- [ ] **Step 6: 真实冒烟(手动,网络可用时执行)**

Run: `python -m gxfc.main 20260629 20260331`
Expected: 控制台打印面板三段;`out/` 下生成 `sectors_20260629.csv` 与 `candidates_20260629.csv`。
若 AKShare 某接口报错,按报错信息核对 `gxfc/data/fetcher.py` 里对应接口名与参数(接口随版本可能更名),改这一处即可。

- [ ] **Step 7: 提交**

```bash
git add gxfc/main.py tests/test_main.py
git commit -m "feat: 新增主流程串联与端到端组装(每日复盘面板入口)"
```

---

## 自检结果

**1. Spec 覆盖**(对照设计文档阶段1范围):
- 数据层(AKShare 封装 + SQLite 缓存 + 清洗):Task 2(缓存)、Task 3(抓取封装,清洗内联在因子的容错解析中) ✓
- 市场情绪温度计:Task 4 ✓
- 板块涨幅榜:Task 5 ✓
- 净利润断层扫描器:Task 6 ✓
- 每日复盘面板(控制台/CSV):Task 7 + Task 8 ✓
- 阶段2-4(情绪周期判定、质地评分、回补风险、买点、看板、交易复盘)不在本计划,留待后续计划。

**2. 占位符扫描**:无 TBD/TODO;每个代码步骤都给了完整可运行代码与测试。

**3. 类型一致性**:`MarketEmotion` 字段在 Task 4 定义、Task 7/8 消费一致;`scan_profit_fault` 返回列 `股票代码/股票简称/同比增长/有跳空` 在 Task 6 定义、Task 8 测试一致;`rank_sectors` 列 `板块名称/涨跌幅/领涨股` 在 Task 5 与 Task 7/8 一致;`Fetcher` 方法名在 Task 3 定义、Task 8 的 FakeFetcher 同名实现一致。

**已知简化**(阶段1有意为之,不算缺口):
- 量能状态在主流程未注入 `today_amount/avg5_amount`,面板显示"数据不足";阶段2接入两市成交额历史后补齐。
- `core_stocks` 已实现但阶段1面板未展开每个板块的成分股(仅展示板块榜);阶段2在看板里下钻。
- 断层扫描对业绩预告前 30 只拉日K(`top_codes_limit`),已通过参数显式声明,避免静默截断。
