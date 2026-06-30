# DuckDB 采集/筛选解耦 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把数据采集与选股筛选解耦——采集阶段联网慢速把全市场数据落进本地 DuckDB(可断点续传),筛选阶段纯离线只读 DuckDB 出面板,彻底避开东财限流。

**Architecture:** 新增 `DuckStore`(DuckDB 读写)、`ingest`(联网采集→DuckStore)、`screen`(离线 DuckStore→复用现有因子→面板)三个组件。因子层/面板层完全复用阶段1 的纯函数。采集用 `Fetcher`(已带节流+退避),筛选零网络。

**Tech Stack:** Python 3.10+、DuckDB(本地列式库)、pandas、AKShare(仅采集)、pytest。复用 `gxfc.factors.*`、`gxfc.review.daily_board`、`gxfc.data.fetcher`。

## Global Constraints

- Python ≥ 3.10;所有源码与测试 UTF-8 无 BOM;从仓库根运行 `pytest`。
- 一切注释、文档、提交信息一律简体中文;仅代码标识符英文。
- 日志用标准库 `logging`,禁止 `print` 调试(面板正式输出走 render_console + print 允许)。
- 真实 AKShare 列名(阶段1 已核对,建表以真实列为准):涨停池含 `代码,名称,涨跌幅,连板数,炸板次数,所属行业`;跌停池含 `代码,名称,涨跌幅,连续跌停`;炸板池含 `代码,名称,涨跌幅,炸板次数`;行业板块含 `板块名称,涨跌幅,领涨股票`;行业成分含 `名称,涨跌幅,成交额`;业绩预告含 `股票代码,股票简称,预测指标,业绩变动幅度`(每票多行,净利润行 `预测指标=='归属于上市公司股东的净利润'`);日K含 `代码,日期,开盘,收盘,最高,最低,成交量,成交额`。
- DuckDB 中文列名一律用双引号包裹(如 `"代码"`、`"日期"`);内部固定表名(ascii)可直接拼接,周期值/代码用参数占位符传入。
- 筛选阶段严格离线:只读 DuckStore,绝不联网;缺数据则降级标注"请先采集"。
- 复用因子签名(不改):`compute_market_emotion(zt_df, dt_df, zb_df, spot_df=None, hot_up_count=4500, cold_up_count=800) -> MarketEmotion`;`rank_sectors(board_df, top_n=10)`;`core_stocks(cons_df, core_top_n=5)`;`scan_profit_fault(yjyg_df, daily_map, growth_threshold=50.0)`;`DailyBoard(date, emotion, sectors, candidates, sector_cores)`、`render_console`、`save_csv`。
- 阶段1 的 `gxfc/data/cache.py`(SQLite KV)与 `run_daily` 保留不动(遗留联网入口);新流程不依赖它,采集直接写 DuckStore。
- 每个任务遵循 TDD:先写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交。提交信息用 `feat:`/`test:`/`chore:` 前缀 + 简体中文。

---

### Task 1: DuckStore 骨架(连接 + 建表辅助)

**Files:**
- Modify: `requirements.txt`
- Create: `gxfc/store/__init__.py`
- Create: `gxfc/store/duck_store.py`
- Test: `tests/test_duck_store.py`

**Interfaces:**
- Consumes: 无
- Produces:
  - `class DuckStore(db_path: str)` — 打开/创建 DuckDB,持有连接
  - `DuckStore.table_exists(table: str) -> bool`
  - `DuckStore.close() -> None`

- [ ] **Step 1: 加依赖**

`requirements.txt` 追加一行:
```
duckdb>=0.10
```
并执行 `pip install duckdb>=0.10`。

- [ ] **Step 2: 写失败测试**

`tests/test_duck_store.py`:
```python
from gxfc.store.duck_store import DuckStore


def test_新建库无表(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    assert store.table_exists("zt_pool") is False
    store.close()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/test_duck_store.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'gxfc.store.duck_store'`

- [ ] **Step 4: 写最小实现**

`gxfc/store/__init__.py`:空文件。

`gxfc/store/duck_store.py`:
```python
"""本地 DuckDB 数据存储:采集阶段写入,筛选阶段只读。

快照表(涨跌停池/板块榜/业绩预告等)按周期列(trade_date 或 quarter_end)累积;
日K表按 (代码,日期) 唯一增量追加。中文列名在 SQL 中一律用双引号包裹。
"""
import duckdb


class DuckStore:
    def __init__(self, db_path: str):
        self._con = duckdb.connect(db_path)

    def table_exists(self, table: str) -> bool:
        row = self._con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchone()
        return row[0] > 0

    def close(self) -> None:
        self._con.close()
```

- [ ] **Step 5: 运行测试确认通过**

Run: `pytest tests/test_duck_store.py -v`
Expected: 1 passed

- [ ] **Step 6: 提交**

```bash
git add requirements.txt gxfc/store/ tests/test_duck_store.py
git commit -m "feat: 新增 DuckStore 骨架与 duckdb 依赖"
```

---

### Task 2: 快照表 upsert / read / has

**Files:**
- Modify: `gxfc/store/duck_store.py`
- Test: `tests/test_duck_store.py`

**Interfaces:**
- Consumes: `DuckStore.table_exists`
- Produces:
  - `DuckStore.upsert_snapshot(table: str, period_col: str, period: str, df: pandas.DataFrame) -> None` — 给 df 补一列 `period_col=period`,删该周期旧行再插(表不存在则按 df 结构建)
  - `DuckStore.read_snapshot(table: str, period_col: str, period: str) -> pandas.DataFrame` — 读该周期全部行;表不存在返回空 DataFrame
  - `DuckStore.has_snapshot(table: str, period_col: str, period: str) -> bool`

- [ ] **Step 1: 写失败测试**

`tests/test_duck_store.py` 追加:
```python
import pandas as pd


def test_快照_写入后可读回(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    df = pd.DataFrame({"代码": ["000001", "000002"], "涨跌幅": [1.5, -2.0]})
    store.upsert_snapshot("zt_pool", "trade_date", "20260629", df)
    got = store.read_snapshot("zt_pool", "trade_date", "20260629")
    assert len(got) == 2
    assert set(got["代码"]) == {"000001", "000002"}
    assert (got["trade_date"] == "20260629").all()
    store.close()


def test_快照_同周期覆盖(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    store.upsert_snapshot("zt_pool", "trade_date", "20260629",
                          pd.DataFrame({"代码": ["A"], "涨跌幅": [1.0]}))
    store.upsert_snapshot("zt_pool", "trade_date", "20260629",
                          pd.DataFrame({"代码": ["B", "C"], "涨跌幅": [2.0, 3.0]}))
    got = store.read_snapshot("zt_pool", "trade_date", "20260629")
    assert set(got["代码"]) == {"B", "C"}
    store.close()


def test_快照_不同周期互不影响(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    store.upsert_snapshot("zt_pool", "trade_date", "20260629",
                          pd.DataFrame({"代码": ["A"], "涨跌幅": [1.0]}))
    store.upsert_snapshot("zt_pool", "trade_date", "20260630",
                          pd.DataFrame({"代码": ["B"], "涨跌幅": [2.0]}))
    assert store.has_snapshot("zt_pool", "trade_date", "20260629") is True
    assert store.has_snapshot("zt_pool", "trade_date", "20260630") is True
    assert store.has_snapshot("zt_pool", "trade_date", "20260701") is False
    assert len(store.read_snapshot("zt_pool", "trade_date", "20260629")) == 1
    store.close()


def test_快照_读不存在的表返回空(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    got = store.read_snapshot("不存在", "trade_date", "20260629")
    assert len(got) == 0
    store.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_duck_store.py -v`
Expected: FAIL，`AttributeError: 'DuckStore' object has no attribute 'upsert_snapshot'`

- [ ] **Step 3: 写最小实现**

在 `gxfc/store/duck_store.py` 的 `DuckStore` 类中追加方法:
```python
    def upsert_snapshot(self, table: str, period_col: str, period: str, df) -> None:
        data = df.copy()
        data[period_col] = period
        self._con.register("tmp_df", data)
        try:
            # 表不存在则按 df 结构建空表
            self._con.execute(
                f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM tmp_df WHERE 1=0"
            )
            self._con.execute(
                f'DELETE FROM {table} WHERE "{period_col}" = ?', [period]
            )
            self._con.execute(f"INSERT INTO {table} SELECT * FROM tmp_df")
        finally:
            self._con.unregister("tmp_df")

    def read_snapshot(self, table: str, period_col: str, period: str):
        import pandas as pd
        if not self.table_exists(table):
            return pd.DataFrame()
        return self._con.execute(
            f'SELECT * FROM {table} WHERE "{period_col}" = ?', [period]
        ).df()

    def has_snapshot(self, table: str, period_col: str, period: str) -> bool:
        if not self.table_exists(table):
            return False
        row = self._con.execute(
            f'SELECT count(*) FROM {table} WHERE "{period_col}" = ?', [period]
        ).fetchone()
        return row[0] > 0
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_duck_store.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/store/duck_store.py tests/test_duck_store.py
git commit -m "feat: DuckStore 快照表 upsert/read/has"
```

---

### Task 3: 日K表 append(去重)/ read / has_daily

**Files:**
- Modify: `gxfc/store/duck_store.py`
- Test: `tests/test_duck_store.py`

**Interfaces:**
- Consumes: `DuckStore.table_exists`
- Produces:
  - `DuckStore.append_daily(df: pandas.DataFrame) -> int` — 把日K追加进 `daily` 表;按 `(代码,日期)` 去重(已存在的不重复插);`日期` 统一存为字符串;返回新增行数
  - `DuckStore.read_daily(code: str, start: str, end: str) -> pandas.DataFrame` — 读某票 `[start,end]`(字符串日期 'YYYY-MM-DD')区间,按 `日期` 升序
  - `DuckStore.has_daily(code: str, upto_date: str) -> bool` — 该票已存日K的最大日期 ≥ `upto_date`('YYYY-MM-DD') 则 True

- [ ] **Step 1: 写失败测试**

`tests/test_duck_store.py` 追加:
```python
def _daily_df(code, rows):
    # rows: [(日期, 开盘, 最高), ...]
    return pd.DataFrame({
        "代码": [code] * len(rows),
        "日期": [d for d, _, _ in rows],
        "开盘": [o for _, o, _ in rows],
        "最高": [h for _, _, h in rows],
    })


def test_日K_追加并去重(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    n1 = store.append_daily(_daily_df("000001", [("2026-06-28", 10.0, 10.5),
                                                  ("2026-06-29", 11.0, 11.8)]))
    assert n1 == 2
    # 重复追加同样两天 + 新增一天,只新增1行
    n2 = store.append_daily(_daily_df("000001", [("2026-06-29", 11.0, 11.8),
                                                  ("2026-06-30", 12.0, 12.5)]))
    assert n2 == 1
    got = store.read_daily("000001", "2026-06-01", "2026-06-30")
    assert list(got["日期"]) == ["2026-06-28", "2026-06-29", "2026-06-30"]
    store.close()


def test_日K_区间读取(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    store.append_daily(_daily_df("000001", [("2026-06-20", 1, 2), ("2026-06-25", 3, 4),
                                            ("2026-06-29", 5, 6)]))
    got = store.read_daily("000001", "2026-06-24", "2026-06-29")
    assert list(got["日期"]) == ["2026-06-25", "2026-06-29"]
    store.close()


def test_has_daily_续传判断(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    store.append_daily(_daily_df("000001", [("2026-06-28", 10, 10.5),
                                            ("2026-06-29", 11, 11.8)]))
    assert store.has_daily("000001", "2026-06-29") is True
    assert store.has_daily("000001", "2026-06-30") is False
    assert store.has_daily("000002", "2026-06-29") is False
    store.close()


def test_has_daily_空表(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    assert store.has_daily("000001", "2026-06-29") is False
    store.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_duck_store.py -v`
Expected: FAIL，`AttributeError: 'DuckStore' object has no attribute 'append_daily'`

- [ ] **Step 3: 写最小实现**

在 `DuckStore` 类中追加:
```python
    def append_daily(self, df) -> int:
        if df is None or len(df) == 0:
            return 0
        data = df.copy()
        data["日期"] = data["日期"].astype(str)
        data["代码"] = data["代码"].astype(str)
        self._con.register("tmp_daily", data)
        try:
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS daily AS SELECT * FROM tmp_daily WHERE 1=0"
            )
            before = self._con.execute("SELECT count(*) FROM daily").fetchone()[0]
            self._con.execute(
                'INSERT INTO daily SELECT * FROM tmp_daily t '
                'WHERE NOT EXISTS (SELECT 1 FROM daily d '
                'WHERE d."代码" = t."代码" AND d."日期" = t."日期")'
            )
            after = self._con.execute("SELECT count(*) FROM daily").fetchone()[0]
        finally:
            self._con.unregister("tmp_daily")
        return after - before

    def read_daily(self, code: str, start: str, end: str):
        import pandas as pd
        if not self.table_exists("daily"):
            return pd.DataFrame()
        return self._con.execute(
            'SELECT * FROM daily WHERE "代码" = ? AND "日期" >= ? AND "日期" <= ? '
            'ORDER BY "日期"',
            [str(code), start, end],
        ).df()

    def has_daily(self, code: str, upto_date: str) -> bool:
        if not self.table_exists("daily"):
            return False
        row = self._con.execute(
            'SELECT max("日期") FROM daily WHERE "代码" = ?', [str(code)]
        ).fetchone()
        return row[0] is not None and str(row[0]) >= upto_date
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_duck_store.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/store/duck_store.py tests/test_duck_store.py
git commit -m "feat: DuckStore 日K append去重/read/has_daily(断点续传)"
```

---

### Task 4: 采集快照(ingest_snapshots)

**Files:**
- Create: `gxfc/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `DuckStore`(upsert_snapshot/has_snapshot/read_snapshot)、`Fetcher` 风格对象(`zt_pool(date)`/`dt_pool(date)`/`zb_pool(date)`/`industry_board()`/`industry_cons(board)`/`yjyg(quarter_end)`)
- Produces:
  - `ingest_snapshots(fetcher, store, date: str, quarter_end: str, cons_top_n: int = 3) -> None` — 采集涨停/跌停/炸板池、板块榜、板块榜前 `cons_top_n` 个板块的成分股、业绩预告,写入 store;已存在的快照跳过;单项失败仅警告不中断

- [ ] **Step 1: 写失败测试**

`tests/test_ingest.py`:
```python
import pandas as pd
from gxfc.store.duck_store import DuckStore
from gxfc.ingest import ingest_snapshots


class FakeFetcher:
    def __init__(self):
        self.calls = []

    def zt_pool(self, date):
        self.calls.append("zt")
        return pd.DataFrame({"代码": ["A"], "涨跌幅": [9.9], "连板数": [3]})

    def dt_pool(self, date):
        self.calls.append("dt")
        return pd.DataFrame({"代码": ["B"], "涨跌幅": [-10.0]})

    def zb_pool(self, date):
        self.calls.append("zb")
        return pd.DataFrame({"代码": ["C"], "涨跌幅": [5.0]})

    def industry_board(self):
        self.calls.append("board")
        return pd.DataFrame({"板块名称": ["电力", "煤炭"], "涨跌幅": [5.6, 1.2],
                             "领涨股票": ["X", "Y"]})

    def industry_cons(self, board):
        self.calls.append(f"cons:{board}")
        return pd.DataFrame({"名称": [f"{board}核心"], "涨跌幅": [3.0], "成交额": [1e8]})

    def yjyg(self, quarter_end):
        self.calls.append("yjyg")
        return pd.DataFrame({"股票代码": ["000001"], "股票简称": ["甲"],
                             "预测指标": ["归属于上市公司股东的净利润"],
                             "业绩变动幅度": [80.0]})


def test_采集快照全部落库(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    fetcher = FakeFetcher()
    ingest_snapshots(fetcher, store, "20260629", "20260331", cons_top_n=2)
    assert len(store.read_snapshot("zt_pool", "trade_date", "20260629")) == 1
    assert len(store.read_snapshot("dt_pool", "trade_date", "20260629")) == 1
    assert len(store.read_snapshot("zb_pool", "trade_date", "20260629")) == 1
    assert len(store.read_snapshot("industry_board", "trade_date", "20260629")) == 2
    # 前2个板块各1行成分股
    cons = store.read_snapshot("industry_cons", "trade_date", "20260629")
    assert len(cons) == 2
    assert set(cons["板块名称"]) == {"电力", "煤炭"}
    assert len(store.read_snapshot("yjyg", "quarter_end", "20260331")) == 1
    store.close()


def test_采集快照_已存在则跳过(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    f1 = FakeFetcher()
    ingest_snapshots(f1, store, "20260629", "20260331", cons_top_n=2)
    f2 = FakeFetcher()
    ingest_snapshots(f2, store, "20260629", "20260331", cons_top_n=2)
    # 第二次所有快照已存在,不应再调用 fetcher 的池/板块/业绩接口
    assert "zt" not in f2.calls
    assert "board" not in f2.calls
    assert "yjyg" not in f2.calls
    store.close()


def test_采集快照_单项失败不中断(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))

    class PartialFetcher(FakeFetcher):
        def dt_pool(self, date):
            raise RuntimeError("模拟跌停池失败")

    ingest_snapshots(PartialFetcher(), store, "20260629", "20260331", cons_top_n=2)
    # 跌停池失败,但其余仍落库
    assert len(store.read_snapshot("zt_pool", "trade_date", "20260629")) == 1
    assert len(store.read_snapshot("dt_pool", "trade_date", "20260629")) == 0
    assert len(store.read_snapshot("yjyg", "quarter_end", "20260331")) == 1
    store.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ingest.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'gxfc.ingest'`

- [ ] **Step 3: 写最小实现**

`gxfc/ingest.py`:
```python
"""联网采集:用 Fetcher 拉数据写入 DuckStore。

采集是唯一联网阶段,慢速、礼貌(Fetcher 已带节流/退避)、可断点续传
(已落库的快照/日K跳过)。单项失败只警告不中断,重跑接着采。
"""
import logging

logger = logging.getLogger(__name__)


def ingest_snapshots(fetcher, store, date: str, quarter_end: str, cons_top_n: int = 3) -> None:
    # (表名, 周期列, 周期值, 取数函数)
    jobs = [
        ("zt_pool", "trade_date", date, lambda: fetcher.zt_pool(date)),
        ("dt_pool", "trade_date", date, lambda: fetcher.dt_pool(date)),
        ("zb_pool", "trade_date", date, lambda: fetcher.zb_pool(date)),
        ("industry_board", "trade_date", date, lambda: fetcher.industry_board()),
        ("yjyg", "quarter_end", quarter_end, lambda: fetcher.yjyg(quarter_end)),
    ]
    for table, col, period, loader in jobs:
        if store.has_snapshot(table, col, period):
            logger.info("快照 %s@%s 已存在,跳过", table, period)
            continue
        try:
            store.upsert_snapshot(table, col, period, loader())
        except Exception as err:
            logger.warning("采集快照 %s 失败,跳过:%s", table, err)

    # 板块成分股:取板块榜前 cons_top_n 个,合并后一次性 upsert(避免逐个 upsert 互相覆盖)
    if store.has_snapshot("industry_cons", "trade_date", date):
        logger.info("快照 industry_cons@%s 已存在,跳过", date)
        return
    board = store.read_snapshot("industry_board", "trade_date", date)
    if len(board) == 0:
        return
    import pandas as pd
    frames = []
    for name in list(board["板块名称"].head(cons_top_n)):
        try:
            cons = fetcher.industry_cons(name)
            cons = cons.copy()
            cons["板块名称"] = name
            frames.append(cons)
        except Exception as err:
            logger.warning("采集板块 %s 成分股失败,跳过:%s", name, err)
    if frames:
        store.upsert_snapshot("industry_cons", "trade_date", date, pd.concat(frames, ignore_index=True))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_ingest.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add gxfc/ingest.py tests/test_ingest.py
git commit -m "feat: 采集快照入库(ingest_snapshots,已存在则跳过)"
```

---

### Task 5: 采集全市场日K(ingest_daily)+ 采集入口与 CLI

**Files:**
- Modify: `gxfc/ingest.py`
- Test: `tests/test_ingest.py`

**Interfaces:**
- Consumes: `DuckStore`(append_daily/has_daily)、`Fetcher`(`stock_daily(code, start, end)`)、`ingest_snapshots`
- Produces:
  - `ingest_daily(fetcher, store, date: str, lister, window_days: int = 120) -> dict` — `lister()` 返回含 `code` 列的全市场清单 DataFrame;逐票拉 `[date-window_days, date]` 日K append 入库;`has_daily(code, date 的 YYYY-MM-DD)` 命中则跳过(续传);返回 `{"采集": n, "跳过": m, "失败": k}`
  - `run_ingest(date: str, quarter_end: str, db_path: str = "gxfc_data.duckdb") -> None` — 真实入口:构建 `Fetcher` + `DuckStore`,跑 snapshots + daily;CLI `python -m gxfc.ingest <date> <quarter_end>`

- [ ] **Step 1: 写失败测试**

`tests/test_ingest.py` 追加:
```python
from gxfc.ingest import ingest_daily


class DailyFetcher:
    def __init__(self):
        self.pulled = []

    def stock_daily(self, code, start, end):
        self.pulled.append(code)
        return pd.DataFrame({
            "代码": [code, code],
            "日期": ["2026-06-28", "2026-06-29"],
            "开盘": [10.0, 11.0],
            "最高": [10.5, 11.8],
        })


def _lister(codes):
    return lambda: pd.DataFrame({"code": codes, "name": [f"票{c}" for c in codes]})


def test_采集日K_全市场入库(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    fetcher = DailyFetcher()
    stat = ingest_daily(fetcher, store, "20260629", _lister(["000001", "000002"]))
    assert stat["采集"] == 2
    assert len(store.read_daily("000001", "2026-06-01", "2026-06-29")) == 2
    assert len(store.read_daily("000002", "2026-06-01", "2026-06-29")) == 2
    store.close()


def test_采集日K_断点续传跳过已采(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    f1 = DailyFetcher()
    ingest_daily(f1, store, "20260629", _lister(["000001", "000002"]))
    # 第二次重跑:两票都已采到 2026-06-29,应全部跳过
    f2 = DailyFetcher()
    stat = ingest_daily(f2, store, "20260629", _lister(["000001", "000002"]))
    assert stat["跳过"] == 2
    assert stat["采集"] == 0
    assert f2.pulled == []
    store.close()


def test_采集日K_单票失败计入失败(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))

    class FlakyFetcher(DailyFetcher):
        def stock_daily(self, code, start, end):
            if code == "000002":
                raise RuntimeError("模拟失败")
            return super().stock_daily(code, start, end)

    stat = ingest_daily(FlakyFetcher(), store, "20260629", _lister(["000001", "000002"]))
    assert stat["采集"] == 1
    assert stat["失败"] == 1
    store.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_ingest.py -v`
Expected: FAIL，`ImportError: cannot import name 'ingest_daily'`

- [ ] **Step 3: 写最小实现**

在 `gxfc/ingest.py` 顶部追加导入,并新增函数:
```python
import sys
from datetime import datetime, timedelta

from gxfc.data.fetcher import Fetcher
from gxfc.store.duck_store import DuckStore


def ingest_daily(fetcher, store, date: str, lister, window_days: int = 120) -> dict:
    end_dt = datetime.strptime(date, "%Y%m%d")
    start = (end_dt - timedelta(days=window_days)).strftime("%Y%m%d")
    upto = end_dt.strftime("%Y-%m-%d")
    codes = [str(c) for c in lister()["code"]]
    total = len(codes)
    stat = {"采集": 0, "跳过": 0, "失败": 0}
    for i, code in enumerate(codes, 1):
        if store.has_daily(code, upto):
            stat["跳过"] += 1
            continue
        try:
            store.append_daily(fetcher.stock_daily(code, start, date))
            stat["采集"] += 1
        except Exception as err:
            stat["失败"] += 1
            logger.warning("采集 %s 日K失败,跳过:%s", code, err)
        if i % 100 == 0:
            logger.info("日K进度 %d/%d %s", i, total, stat)
    logger.info("日K采集完成 %s", stat)
    return stat


def _default_lister():
    import akshare as ak
    return ak.stock_info_a_code_name()  # 列含 code, name


def run_ingest(date: str, quarter_end: str, db_path: str = "gxfc_data.duckdb") -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    fetcher = Fetcher()
    store = DuckStore(db_path)
    try:
        logger.info("开始采集快照 date=%s quarter_end=%s", date, quarter_end)
        ingest_snapshots(fetcher, store, date, quarter_end)
        logger.info("开始采集全市场日K(可断点续传)")
        ingest_daily(fetcher, store, date, _default_lister)
    finally:
        store.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: python -m gxfc.ingest <交易日YYYYMMDD> <季度末YYYYMMDD>")
        sys.exit(1)
    run_ingest(sys.argv[1], sys.argv[2])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_ingest.py -v`
Expected: 6 passed

- [ ] **Step 5: 跑全量确认不回归**

Run: `pytest -q`
Expected: 全部通过(约 55 个用例,以实际为准)

- [ ] **Step 6: 提交**

```bash
git add gxfc/ingest.py tests/test_ingest.py
git commit -m "feat: 采集全市场日K(断点续传)+采集入口run_ingest与CLI"
```

---

### Task 6: 离线筛选(screen)+ CLI

**Files:**
- Create: `gxfc/screen.py`
- Test: `tests/test_screen.py`

**Interfaces:**
- Consumes: `DuckStore`(read_snapshot/read_daily)、因子(`compute_market_emotion`/`rank_sectors`/`core_stocks`/`scan_profit_fault`)、`DailyBoard`/`render_console`/`save_csv`
- Produces:
  - `build_board_offline(store, date: str, quarter_end: str, config: dict) -> DailyBoard` — 纯离线:从 store 读各数据集 → 复用因子 → 组装 `DailyBoard`;某段无数据则降级(情绪给占位提示、板块/候选空)
  - `run_screen(date: str, quarter_end: str, db_path: str = "gxfc_data.duckdb", out_dir: str = "out") -> DailyBoard` — 读 config + DuckStore → build_board_offline → print(render_console) + save_csv;CLI `python -m gxfc.screen <date> <quarter_end>`

- [ ] **Step 1: 写失败测试**

`tests/test_screen.py`:
```python
import pandas as pd
from gxfc.store.duck_store import DuckStore
from gxfc.screen import build_board_offline

CONFIG = {
    "emotion": {"hot_up_count": 4500, "cold_up_count": 800},
    "sector": {"top_n": 10, "core_drill_top_n": 3, "core_top_n": 5},
    "profit_fault": {"growth_threshold": 50.0},
}


def _seed(store):
    store.upsert_snapshot("zt_pool", "trade_date", "20260629",
                          pd.DataFrame({"代码": ["A", "B"], "连板数": [1, 3]}))
    store.upsert_snapshot("dt_pool", "trade_date", "20260629",
                          pd.DataFrame({"代码": ["C"]}))
    store.upsert_snapshot("zb_pool", "trade_date", "20260629",
                          pd.DataFrame({"代码": ["D"]}))
    store.upsert_snapshot("industry_board", "trade_date", "20260629",
                          pd.DataFrame({"板块名称": ["电力"], "涨跌幅": [5.6],
                                        "领涨股票": ["X"]}))
    store.upsert_snapshot("yjyg", "quarter_end", "20260331",
                          pd.DataFrame({"股票代码": ["000001"], "股票简称": ["甲"],
                                        "预测指标": ["归属于上市公司股东的净利润"],
                                        "业绩变动幅度": [80.0]}))
    # 000001 跳空:次日开盘 > 前日最高
    store.append_daily(pd.DataFrame({"代码": ["000001", "000001"],
                                     "日期": ["2026-06-28", "2026-06-29"],
                                     "开盘": [10.0, 11.0], "最高": [10.5, 11.8]}))


def test_离线组装面板(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    _seed(store)
    board = build_board_offline(store, "20260629", "20260331", CONFIG)
    assert board.emotion.limit_up == 2
    assert board.emotion.limit_down == 1
    assert board.emotion.highest_streak == 3
    assert list(board.sectors["板块名称"]) == ["电力"]
    assert list(board.candidates["股票代码"]) == ["000001"]
    store.close()


def test_离线_缺数据降级(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))  # 空库
    board = build_board_offline(store, "20260629", "20260331", CONFIG)
    assert "采集" in board.emotion.sentiment_hint
    assert len(board.sectors) == 0
    assert len(board.candidates) == 0
    store.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/test_screen.py -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'gxfc.screen'`

- [ ] **Step 3: 写最小实现**

`gxfc/screen.py`:
```python
"""离线筛选:只读 DuckStore,复用因子组装面板,全程不联网。

某数据集本地缺失则该段降级(情绪给"请先采集"提示,板块/候选为空),
不触发任何网络请求。
"""
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

from gxfc.factors.market_emotion import MarketEmotion, compute_market_emotion
from gxfc.factors.profit_fault import scan_profit_fault
from gxfc.factors.sector import core_stocks, rank_sectors
from gxfc.review.daily_board import DailyBoard, render_console, save_csv
from gxfc.store.duck_store import DuckStore

logger = logging.getLogger(__name__)

_NET_PROFIT = "归属于上市公司股东的净利润"


def _daily_window(date: str) -> tuple:
    end_dt = datetime.strptime(date, "%Y%m%d")
    start = (end_dt - timedelta(days=10)).strftime("%Y-%m-%d")
    return start, end_dt.strftime("%Y-%m-%d")


def build_board_offline(store, date: str, quarter_end: str, config: dict) -> DailyBoard:
    emo_cfg = config["emotion"]
    sec_cfg = config["sector"]
    pf_cfg = config["profit_fault"]

    # 1) 情绪
    zt = store.read_snapshot("zt_pool", "trade_date", date)
    dt = store.read_snapshot("dt_pool", "trade_date", date)
    zb = store.read_snapshot("zb_pool", "trade_date", date)
    if len(zt) == 0 and len(dt) == 0 and len(zb) == 0:
        emotion = MarketEmotion(
            up_count=None, down_count=None, limit_up=0, limit_down=0,
            broken_board_rate=0.0, highest_streak=0,
            volume_state="数据不足", sentiment_hint="情绪数据缺失,请先采集",
        )
    else:
        emotion = compute_market_emotion(
            zt, dt, zb, spot_df=None,
            hot_up_count=emo_cfg["hot_up_count"], cold_up_count=emo_cfg["cold_up_count"],
        )

    # 2) 板块榜 + 核心成分股
    board = store.read_snapshot("industry_board", "trade_date", date)
    if len(board) > 0:
        sectors = rank_sectors(board, top_n=sec_cfg["top_n"])
    else:
        sectors = pd.DataFrame(columns=["板块名称", "涨跌幅", "领涨股票"])
    sector_cores = {}
    cons_all = store.read_snapshot("industry_cons", "trade_date", date)
    if len(cons_all) > 0 and len(sectors) > 0:
        for name in list(sectors["板块名称"].head(sec_cfg["core_drill_top_n"])):
            sub = cons_all[cons_all["板块名称"] == name]
            if len(sub) > 0:
                sector_cores[name] = core_stocks(sub, core_top_n=sec_cfg["core_top_n"])

    # 3) 断层候选
    yjyg = store.read_snapshot("yjyg", "quarter_end", quarter_end)
    if len(yjyg) > 0:
        start, end = _daily_window(date)
        net = yjyg[yjyg["预测指标"] == _NET_PROFIT]
        daily_map = {}
        for code in [str(c) for c in net["股票代码"].unique()]:
            daily_map[code] = store.read_daily(code, start, end)
        candidates = scan_profit_fault(yjyg, daily_map, growth_threshold=pf_cfg["growth_threshold"])
    else:
        candidates = pd.DataFrame(columns=["股票代码", "股票简称", "同比增长", "有跳空"])

    return DailyBoard(date=date, emotion=emotion, sectors=sectors,
                     candidates=candidates, sector_cores=sector_cores)


def run_screen(date: str, quarter_end: str, db_path: str = "gxfc_data.duckdb",
               out_dir: str = "out") -> DailyBoard:
    config = yaml.safe_load(Path("config/strategy.yaml").read_text(encoding="utf-8"))
    store = DuckStore(db_path)
    try:
        board = build_board_offline(store, date, quarter_end, config)
    finally:
        store.close()
    print(render_console(board))
    paths = save_csv(board, out_dir)
    logger.info("已保存:%s", paths)
    return board


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) != 3:
        print("用法: python -m gxfc.screen <交易日YYYYMMDD> <季度末YYYYMMDD>")
        sys.exit(1)
    run_screen(sys.argv[1], sys.argv[2])
```

- [ ] **Step 4: 运行测试确认通过**

Run: `pytest tests/test_screen.py -v`
Expected: 2 passed

- [ ] **Step 5: 跑全量确认不回归**

Run: `pytest -q`
Expected: 全部通过(约 57 个用例,以实际为准)

- [ ] **Step 6: 提交**

```bash
git add gxfc/screen.py tests/test_screen.py
git commit -m "feat: 离线筛选build_board_offline+run_screen与CLI(只读DuckStore)"
```

---

## 自检结果

**1. Spec 覆盖**(对照设计文档):
- DuckDB 库结构(快照表 + 日K表):Task 1-3 ✓
- 采集 ingest(快照 + 全市场日K + 断点续传 + CLI):Task 4-5 ✓
- 离线 screen(读库 → 因子 → 面板 + CLI + 缺数据降级):Task 6 ✓
- 退休 SQLite cache:本计划采取保守策略——新流程(ingest/screen)不依赖 `DataFrameCache`,但保留 `cache.py` 与遗留 `run_daily` 不动以免破坏阶段1 的 40 个测试。设计文档第5.4 的"退休"在此理解为"新流程不使用",非物理删除(已在 Global Constraints 注明)。
- 错误处理/降级、测试策略:各任务的失败/续传/降级测试覆盖 ✓

**2. 占位符扫描**:无 TBD/TODO;每个代码步骤均为完整可运行代码与测试。

**3. 类型一致性**:`DuckStore` 方法名(upsert_snapshot/read_snapshot/has_snapshot/append_daily/read_daily/has_daily/table_exists/close)在 Task 1-3 定义,Task 4-6 一致消费;`ingest_snapshots`/`ingest_daily` 签名在 Task 4-5 定义并在 run_ingest 一致调用;`build_board_offline` 复用的因子签名与阶段1 一致(compute_market_emotion 的 zt/dt/zb 三池入参、scan_profit_fault 的 yjyg 多行结构 + 预测指标过滤);`DailyBoard` 字段(date/emotion/sectors/candidates/sector_cores)与阶段1 一致。

**已知约束**:全市场日K 首采耗时长(~5000 票 × 节流),靠 Task 5 的断点续传分次完成;真实 AKShare 列名沿用阶段1 已核对结果。
