# GXFC Web 控制台实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 [2026-07-13 Web 控制台设计](../specs/2026-07-13-web-console-design.md) 实现 Streamlit 本地网页：复盘面板、信号追踪、交易日志（含网页记账）、数据采集触发。

**Architecture:** `gxfc/web/` 四层——`app.py` 入口导航、`queries.py` 唯一读取层（短连接只读）、`actions.py` 唯一动作层（subprocess 调既有 CLI）、`pages_/` 四个页面渲染模块。读走短连接不占写锁；写走子进程复用 CLI 全部校验逻辑。

**Tech Stack:** Streamlit ≥1.35、plotly ≥5.20、DuckDB（只读短连接）、pytest + streamlit.testing.v1.AppTest。

## Global Constraints

- 所有注释、文档、提交信息、测试名一律简体中文；文件 UTF-8 无 BOM。
- `gxfc/web/` 全包禁止 import `gxfc.data.fetcher`（严格离线，靠依赖关系保证）。
- 写操作绝不在 web 进程内打开写连接——一律 subprocess 调 `python -m gxfc.journal / gxfc.ingest / gxfc.screen`。
- 读操作一律 `DuckStore(db, read_only=True)` 短连接，即用即关。
- 页面目录命名 `pages_/`（避开 Streamlit 对 `pages/` 的自动路由魔法）。
- 库路径默认 `gxfc_data.duckdb`，可用环境变量 `GXFC_DB` 覆盖（测试注入用）。
- 全部测试零网络，`pytest` 默认可跑；测试函数名用中文描述行为（项目既有风格）。
- 子进程统一 `sys.executable` + `PYTHONIOENCODING=utf-8` 环境（Windows 中文输出防乱码）。
- 提交小步进行，每个任务一次提交，提交信息中文。

---

### Task 1: 依赖与 DuckStore 只读模式

**Files:**
- Modify: `requirements.txt`
- Modify: `gxfc/store/duck_store.py:42-47`（`__init__`）
- Test: `tests/test_duck_store_readonly.py`

**Interfaces:**
- Consumes: 现有 `DuckStore.__init__(self, db_path: str)`。
- Produces: `DuckStore(db_path: str, read_only: bool = False)` —— `read_only=True` 时以只读模式连接且**跳过 DDL**（只读连接执行 CREATE 会抛错）。后续所有 queries 层依赖此参数。

- [ ] **Step 1: requirements.txt 追加依赖并安装**

在 `requirements.txt` 末尾追加两行：

```
streamlit>=1.35
plotly>=5.20
```

运行：`.venv\Scripts\pip.exe install -r requirements.txt`
预期：streamlit 与 plotly 安装成功（`.venv\Scripts\python.exe -c "import streamlit, plotly"` 无报错）。

- [ ] **Step 2: 写失败测试**

创建 `tests/test_duck_store_readonly.py`：

```python
import duckdb
import pandas as pd
import pytest

from gxfc.store.duck_store import DuckStore


def _one_row_daily() -> pd.DataFrame:
    return pd.DataFrame({
        "代码": ["600000"], "日期": ["2026-07-08"], "开盘": [10.0], "收盘": [11.0],
        "最高": [11.1], "最低": [9.9], "成交量": [1e6], "成交额": [1e7], "换手率": [1.0],
    })


def test_只读模式可读禁写(tmp_path):
    db = str(tmp_path / "ro.duckdb")
    w = DuckStore(db)
    w.append_daily(_one_row_daily())
    w.close()
    r = DuckStore(db, read_only=True)
    try:
        assert r.daily_max_date() == "2026-07-08"
        with pytest.raises(duckdb.Error):
            r.con.execute("CREATE TABLE t(i INTEGER)")
    finally:
        r.close()


def test_只读模式不执行DDL(tmp_path):
    db = str(tmp_path / "bare.duckdb")
    duckdb.connect(db).close()          # 裸空库,无任何表
    r = DuckStore(db, read_only=True)   # 只读构造不得执行 DDL,否则此处抛错
    try:
        assert r.table_exists("daily") is False
    finally:
        r.close()
```

- [ ] **Step 3: 运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_duck_store_readonly.py -v`
预期：FAIL，`TypeError: __init__() got an unexpected keyword argument 'read_only'`。

- [ ] **Step 4: 实现**

修改 `gxfc/store/duck_store.py` 的 `__init__`：

```python
    def __init__(self, db_path: str, read_only: bool = False):
        """read_only=True 供展示层短连接使用:只读模式连接、跳过 DDL、不占写锁。"""
        self._con = duckdb.connect(db_path, read_only=read_only)
        if not read_only:
            for ddl in _DDL:
                self._con.execute(ddl)
```

- [ ] **Step 5: 运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_duck_store_readonly.py -v`
预期：2 passed。再跑 `.venv\Scripts\python.exe -m pytest` 确认既有测试无回归。

- [ ] **Step 6: 提交**

```bash
git add requirements.txt gxfc/store/duck_store.py tests/test_duck_store_readonly.py
git commit -m "feat: DuckStore 只读模式(短连接展示层前置)并引入 streamlit/plotly 依赖"
```

---

### Task 2: queries.py 唯一读取层

**Files:**
- Create: `gxfc/web/__init__.py`、`gxfc/web/queries.py`
- Create: `tests/conftest.py`（seeded_db 夹具，后续任务共用）
- Test: `tests/test_web_queries.py`

**Interfaces:**
- Consumes: `DuckStore(db, read_only=True)`（Task 1）；`gxfc.screen.build_board_offline(store, date, quarter_end, config)`、`gxfc.screen.load_config()`；`gxfc.review.tracker.track_signals / summarize / trade_stats`；`gxfc.dates.dash / derive_quarter_end`。
- Produces（后续页面依赖的精确签名）:
  - `open_store(db_path: str)` — 上下文管理器，产出只读 `DuckStore`
  - `trading_dates(db_path: str, limit: int = 120) -> list` — daily 表日期倒序
  - `load_board(db_path: str, date: str) -> DailyBoard`
  - `signal_strategies(db_path: str) -> list`
  - `tracking_report(db_path: str, strategy=None, start=None, end=None) -> tuple[pd.DataFrame, pd.DataFrame]` — (明细, 汇总)
  - `list_trades(db_path: str, open_only: bool = False) -> pd.DataFrame`
  - `trade_stats_report(db_path: str) -> pd.DataFrame`
  - `db_overview(db_path: str) -> Optional[dict]` — 键 `tables`(DataFrame[表,行数])、`daily_max`(str|None)、`recent_log`(DataFrame)；库文件不存在返回 None

- [ ] **Step 1: 建包与共享夹具**

创建 `gxfc/web/__init__.py`：

```python
"""GXFC Web 控制台:Streamlit 本地网页展示与操作层。

本包禁止 import gxfc.data.fetcher——严格离线,触网只发生在采集子进程内。
"""
```

创建 `tests/conftest.py`：

```python
"""web 层测试共享夹具:临时 DuckDB 造最小数据集。"""
import pandas as pd
import pytest

from gxfc.store.duck_store import DuckStore
from gxfc.store.journal_store import JournalStore


@pytest.fixture
def seeded_db(tmp_path) -> str:
    """含 6 行日K、1 条信号、1 笔已平仓交易的临时库,写完即关(供只读层测试)。"""
    db = str(tmp_path / "seeded.duckdb")
    store = DuckStore(db)
    store.append_daily(pd.DataFrame({
        "代码": ["600000"] * 6,
        "日期": ["2026-07-01", "2026-07-02", "2026-07-03",
               "2026-07-06", "2026-07-07", "2026-07-08"],
        "开盘": [10.0] * 6,
        "收盘": [10.0, 10.2, 10.5, 10.4, 10.8, 11.0],
        "最高": [10.1, 10.3, 10.6, 10.5, 10.9, 11.1],
        "最低": [9.9] * 6,
        "成交量": [1e6] * 6, "成交额": [1e7] * 6, "换手率": [1.0] * 6,
    }))
    store.upsert_securities(pd.DataFrame({"代码": ["600000"], "名称": ["浦发银行"]}))
    journal = JournalStore(store.con)
    journal.record_signals("2026-07-02", "profit_fault",
                           pd.DataFrame({"代码": ["600000"], "名称": ["浦发银行"]}))
    tid = journal.add_trade("600000", "浦发银行", "profit_fault",
                            "测试计划:破5日线止损", "2026-07-02", 10.2, 1000)
    journal.close_trade(tid, "2026-07-07", 10.8, "规则卖点", True, "")
    store.close()
    return db
```

- [ ] **Step 2: 写失败测试**

创建 `tests/test_web_queries.py`：

```python
import pytest

from gxfc.store.duck_store import DuckStore
from gxfc.web import queries


def test_trading_dates_倒序(seeded_db):
    dates = queries.trading_dates(seeded_db)
    assert dates[0] == "2026-07-08"
    assert dates[-1] == "2026-07-01"


def test_空库各查询返回空态而非抛错(tmp_path):
    db = str(tmp_path / "empty.duckdb")
    DuckStore(db).close()   # 只有系统表,无 signals/trades
    assert queries.trading_dates(db) == []
    assert queries.signal_strategies(db) == []
    perf, summary = queries.tracking_report(db)
    assert perf.empty and summary.empty
    assert queries.list_trades(db).empty
    assert queries.trade_stats_report(db).empty


def test_tracking_report_明细与汇总(seeded_db):
    perf, summary = queries.tracking_report(seeded_db)
    assert len(perf) == 1
    assert bool(perf.iloc[0]["可追踪"])
    # 信号日 07-02 收盘 10.2,T+3 = 07-07 收盘 10.8 → 5.88%
    assert perf.iloc[0]["T+3收益%"] == pytest.approx(5.88, abs=0.01)
    assert not summary.empty
    assert set(summary["策略"]) == {"profit_fault"}


def test_tracking_report_按策略过滤(seeded_db):
    perf, _ = queries.tracking_report(seeded_db, strategy="不存在的策略")
    assert perf.empty


def test_list_trades_与持仓过滤(seeded_db):
    assert len(queries.list_trades(seeded_db)) == 1
    assert queries.list_trades(seeded_db, open_only=True).empty  # 已平仓


def test_trade_stats_report(seeded_db):
    stats = queries.trade_stats_report(seeded_db)
    assert "按计划" in set(stats["分组"])


def test_load_board_情绪段降级(seeded_db):
    board = queries.load_board(seeded_db, "2026-07-08")
    assert board.date == "2026-07-08"
    # zt_pool 未采集 → 情绪段降级并给出引导语
    assert "未采集" in board.emotion.sentiment_hint


def test_db_overview(seeded_db, tmp_path):
    ov = queries.db_overview(seeded_db)
    assert ov["daily_max"] == "2026-07-08"
    assert "daily" in set(ov["tables"]["表"])
    assert queries.db_overview(str(tmp_path / "nope.duckdb")) is None
```

- [ ] **Step 3: 运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_queries.py -v`
预期：FAIL，`ModuleNotFoundError: No module named 'gxfc.web.queries'`。

- [ ] **Step 4: 实现 queries.py**

创建 `gxfc/web/queries.py`：

```python
"""唯一读取层:全部查询走 read_only 短连接,即用即关。

DuckDB 同一文件只允许"一个写者,或多个只读者":本层绝不持有常驻连接,
保证网页开着时采集/记账子进程随时能拿到写锁。
本模块不做任何 Streamlit 调用,保持纯函数可单测;缓存由页面层套壳。
"""
import contextlib
from pathlib import Path
from typing import Optional

import pandas as pd

from gxfc.dates import dash, derive_quarter_end
from gxfc.review.daily_board import DailyBoard
from gxfc.review.tracker import summarize, track_signals, trade_stats
from gxfc.screen import build_board_offline, load_config
from gxfc.store.duck_store import DuckStore


@contextlib.contextmanager
def open_store(db_path: str):
    """只读短连接上下文:进入即连,退出即关。"""
    store = DuckStore(db_path, read_only=True)
    try:
        yield store
    finally:
        store.close()


def trading_dates(db_path: str, limit: int = 120) -> list:
    """daily 表内有数据的交易日,倒序;无表/无数据返回空列表。"""
    with open_store(db_path) as store:
        if not store.table_exists("daily"):
            return []
        rows = store.con.execute(
            'SELECT DISTINCT "日期" FROM daily ORDER BY "日期" DESC LIMIT ?', [limit]
        ).fetchall()
    return [r[0] for r in rows]


def load_board(db_path: str, date: str) -> DailyBoard:
    """组装某日复盘面板,各段独立降级(口径与 python -m gxfc.screen 完全一致)。"""
    config = load_config()
    with open_store(db_path) as store:
        return build_board_offline(store, date, derive_quarter_end(dash(date)), config)


def signal_strategies(db_path: str) -> list:
    """signals 表中出现过的策略名;表不存在返回空列表。"""
    with open_store(db_path) as store:
        if not store.table_exists("signals"):
            return []
        rows = store.con.execute(
            "SELECT DISTINCT strategy FROM signals ORDER BY strategy"
        ).fetchall()
    return [r[0] for r in rows]


def tracking_report(db_path: str, strategy: Optional[str] = None,
                    start: Optional[str] = None,
                    end: Optional[str] = None) -> tuple:
    """信号前向收益 (明细, 汇总)。signals 缺失或无信号返回两张空表。"""
    horizons = tuple(load_config().get("tracking", {}).get("horizons", (1, 3, 5, 10)))
    with open_store(db_path) as store:
        if not store.table_exists("signals"):
            return pd.DataFrame(), pd.DataFrame()
        sql = "SELECT * FROM signals WHERE 1=1"
        params: list = []
        if strategy:
            sql += " AND strategy = ?"
            params.append(strategy)
        if start:
            sql += " AND signal_date >= ?"
            params.append(dash(start))
        if end:
            sql += " AND signal_date <= ?"
            params.append(dash(end))
        sql += ' ORDER BY signal_date, strategy, "代码"'
        signals = store.con.execute(sql, params).df()
        if signals.empty:
            return pd.DataFrame(), pd.DataFrame()
        perf = track_signals(signals, store.read_daily, horizons)
    return perf, summarize(perf, horizons)


def list_trades(db_path: str, open_only: bool = False) -> pd.DataFrame:
    """交易清单;trades 表不存在返回空表。"""
    with open_store(db_path) as store:
        if not store.table_exists("trades"):
            return pd.DataFrame()
        sql = "SELECT * FROM trades"
        if open_only:
            sql += " WHERE close_date IS NULL"
        sql += " ORDER BY open_date, trade_id"
        return store.con.execute(sql).df()


def trade_stats_report(db_path: str) -> pd.DataFrame:
    """计划-执行-纪律三组统计(口径同 python -m gxfc.journal stats)。"""
    return trade_stats(list_trades(db_path))


def db_overview(db_path: str) -> Optional[dict]:
    """库状态总览:各表行数、日K最新日期、最近台账。库文件不存在返回 None。"""
    if not Path(db_path).exists():
        return None
    with open_store(db_path) as store:
        tables = [r[0] for r in store.con.execute("SHOW TABLES").fetchall()]
        counts = pd.DataFrame(
            [(t, store.con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0])
             for t in tables],
            columns=["表", "行数"],
        )
        daily_max = store.daily_max_date() if store.table_exists("daily") else None
        recent_log = (
            store.con.execute(
                "SELECT run_id, dataset, period, status, rows, source, finished_at "
                "FROM ingest_log ORDER BY finished_at DESC LIMIT 20"
            ).df()
            if store.table_exists("ingest_log") else pd.DataFrame()
        )
    return {"tables": counts, "daily_max": daily_max, "recent_log": recent_log}
```

- [ ] **Step 5: 运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_queries.py -v`
预期：8 passed。

- [ ] **Step 6: 提交**

```bash
git add gxfc/web/__init__.py gxfc/web/queries.py tests/conftest.py tests/test_web_queries.py
git commit -m "feat: web 只读查询层(短连接,面板/追踪/日志/库状态)"
```

---

### Task 3: actions.py 唯一动作层

**Files:**
- Create: `gxfc/web/actions.py`
- Test: `tests/test_web_actions.py`

**Interfaces:**
- Consumes: 既有 CLI 的参数约定——`gxfc.journal` 的 `--db` 在子命令**之前**；`close` 的 `--followed/--broke` 互斥必选。
- Produces（页面层依赖的精确签名）:
  - `journal_add_argv(db_path, code, name, strategy, plan, date, price, shares) -> list`
  - `journal_close_argv(db_path, trade_id, date, price, reason, followed: bool, note: str) -> list`
  - `ingest_argv(db_path: str) -> list`、`screen_argv(db_path: str) -> list`
  - `run_action(argv: list, runner=None) -> tuple[bool, str]` — 一次性命令，返回 (成功?, 合并输出)
  - `start_stream(argv: list) -> subprocess.Popen` — 长任务，stdout/stderr 合并、行缓冲、UTF-8

- [ ] **Step 1: 写失败测试**

创建 `tests/test_web_actions.py`：

```python
from gxfc.web import actions


def test_add_argv_db在子命令前():
    argv = actions.journal_add_argv("db.duckdb", "600000", "浦发银行", "profit_fault",
                                    "断层+情绪回暖,破5日线止损", "20260707", 10.5, 1000)
    assert argv[1:3] == ["-m", "gxfc.journal"]
    assert argv.index("--db") < argv.index("add")   # argparse 主参数必须在子命令前
    assert "--plan" in argv
    assert "断层+情绪回暖,破5日线止损" in argv
    assert "1000" in argv and "10.5" in argv


def test_close_argv_守纪互斥():
    a = actions.journal_close_argv("db", "T20260707-001", "20260710", 11.2,
                                   "规则卖点", True, "")
    b = actions.journal_close_argv("db", "T20260707-001", "20260710", 11.2,
                                   "规则卖点", False, "卖飞")
    assert "--followed" in a and "--broke" not in a
    assert "--note" not in a                        # 空备注不传
    assert "--broke" in b and "--followed" not in b
    assert "--note" in b and "卖飞" in b


def test_ingest_screen_argv():
    assert actions.ingest_argv("x.duckdb")[1:3] == ["-m", "gxfc.ingest"]
    assert actions.screen_argv("x.duckdb")[1:3] == ["-m", "gxfc.screen"]
    assert "x.duckdb" in actions.ingest_argv("x.duckdb")


def test_run_action_注入runner():
    seen = {}

    class FakeProc:
        returncode = 0
        stdout = "已开仓 T20260707-001"
        stderr = ""

    def fake_runner(argv, **kwargs):
        seen["argv"] = argv
        return FakeProc()

    ok, out = actions.run_action(["python", "-m", "gxfc.journal"], runner=fake_runner)
    assert ok and "已开仓" in out
    assert seen["argv"] == ["python", "-m", "gxfc.journal"]


def test_run_action_失败返回输出():
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "错误:交易 T1 不存在"

    ok, out = actions.run_action(["x"], runner=lambda argv, **kw: FakeProc())
    assert not ok and "不存在" in out
```

- [ ] **Step 2: 运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_actions.py -v`
预期：FAIL，`ModuleNotFoundError: No module named 'gxfc.web.actions'`。

- [ ] **Step 3: 实现 actions.py**

创建 `gxfc/web/actions.py`：

```python
"""唯一动作层:全部写操作以子进程调用既有 CLI,web 进程永不持有写连接。

收益:复用 CLI 的全部参数校验、幂等与友好报错;子进程结束即释放 DuckDB 写锁。
子进程统一注入 PYTHONIOENCODING=utf-8——Windows 下子进程默认 GBK 输出,
父进程按 UTF-8 解码会乱码。
"""
import os
import subprocess
import sys


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def journal_add_argv(db_path: str, code: str, name: str, strategy: str, plan: str,
                     date: str, price: float, shares: int) -> list:
    """开仓命令。注意 --db 是主解析器参数,必须位于子命令 add 之前。"""
    return [sys.executable, "-m", "gxfc.journal", "--db", db_path, "add",
            "--code", code, "--name", name, "--strategy", strategy,
            "--plan", plan, "--date", date,
            "--price", str(price), "--shares", str(shares)]


def journal_close_argv(db_path: str, trade_id: str, date: str, price: float,
                       reason: str, followed: bool, note: str) -> list:
    argv = [sys.executable, "-m", "gxfc.journal", "--db", db_path, "close", trade_id,
            "--date", date, "--price", str(price), "--reason", reason,
            "--followed" if followed else "--broke"]
    if note:
        argv += ["--note", note]
    return argv


def ingest_argv(db_path: str) -> list:
    return [sys.executable, "-m", "gxfc.ingest", "--db", db_path]


def screen_argv(db_path: str) -> list:
    return [sys.executable, "-m", "gxfc.screen", "--db", db_path]


def run_action(argv: list, runner=None) -> tuple:
    """执行一次性写命令,返回 (是否成功, stdout+stderr 合并输出)。

    runner 注入供测试打桩(签名兼容 subprocess.run)。
    """
    run = runner or subprocess.run
    proc = run(argv, capture_output=True, text=True, encoding="utf-8",
               errors="replace", env=_child_env())
    return proc.returncode == 0, ((proc.stdout or "") + (proc.stderr or "")).strip()


def start_stream(argv: list) -> subprocess.Popen:
    """启动长任务(采集/筛选),stdout/stderr 合并行缓冲,供页面实时滚动。"""
    return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            bufsize=1, env=_child_env())
```

- [ ] **Step 4: 运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_actions.py -v`
预期：5 passed。

- [ ] **Step 5: 提交**

```bash
git add gxfc/web/actions.py tests/test_web_actions.py
git commit -m "feat: web 动作层(子进程调 journal/ingest/screen,复用 CLI 校验)"
```

---

### Task 4: app.py 入口 + 复盘面板页

**Files:**
- Create: `gxfc/web/app.py`、`gxfc/web/pages_/__init__.py`、`gxfc/web/pages_/review.py`
- Create: `gxfc/web/pages_/tracking.py`、`gxfc/web/pages_/journal.py`、`gxfc/web/pages_/ingest.py`（本任务先放最小占位 render，Task 5-7 完整实现——占位也必须可运行：显示"建设中"并不抛错）
- Test: `tests/test_web_pages.py`（AppTest 冒烟，后续任务追加用例）

**Interfaces:**
- Consumes: `queries.trading_dates / load_board`（Task 2）；`DailyBoard`/`MarketEmotion` 字段（`date, emotion, sectors, candidates, sector_cores, surge_candidates`；`up_count, down_count, limit_up, limit_down, broken_board_rate, highest_streak, volume_state, sentiment_hint`）。
- Produces: 每个页面模块统一暴露 `render(db_path: str) -> None`；`app.py` 读环境变量 `GXFC_DB` 决定库路径。AppTest 通过 `AppTest.from_file("gxfc/web/app.py")` 驱动。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_web_pages.py`：

```python
"""四个页面的 AppTest 冒烟:空库与造数库下渲染不抛异常、空态提示存在。"""
from streamlit.testing.v1 import AppTest

_APP = "gxfc/web/app.py"


def _run_app(db_path: str, monkeypatch) -> AppTest:
    monkeypatch.setenv("GXFC_DB", db_path)
    at = AppTest.from_file(_APP, default_timeout=60)
    return at.run()


def test_库缺失显示引导页(tmp_path, monkeypatch):
    at = _run_app(str(tmp_path / "nope.duckdb"), monkeypatch)
    assert not at.exception
    assert any("不存在" in str(i.value) for i in at.info)


def test_复盘面板渲染(seeded_db, monkeypatch):
    at = _run_app(seeded_db, monkeypatch)
    assert not at.exception
    # 默认落在复盘面板页:有日期选择器,且情绪段降级提示可见
    assert at.selectbox[0].value == "2026-07-08"
```

- [ ] **Step 2: 运行测试确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_pages.py -v`
预期：FAIL（app.py 不存在）。

- [ ] **Step 3: 实现 app.py 与 review 页**

创建 `gxfc/web/pages_/__init__.py`：

```python
"""页面渲染模块。目录名带下划线:避开 Streamlit 对 pages/ 目录的自动路由魔法。"""
```

创建 `gxfc/web/app.py`：

```python
"""GXFC Web 控制台入口:侧边栏导航 + 库缺失引导页。

启动:python -m streamlit run gxfc/web/app.py
库路径可用环境变量 GXFC_DB 覆盖(默认 gxfc_data.duckdb)。
本包禁止 import gxfc.data.fetcher——触网只发生在采集子进程内。
"""
import os
import sys
from pathlib import Path

# streamlit run 以脚本方式执行本文件,repo 根不在 sys.path,须手动补上
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import duckdb
import streamlit as st

from gxfc.web.pages_ import ingest, journal, review, tracking

DB_PATH = os.environ.get("GXFC_DB", "gxfc_data.duckdb")

_PAGES = {
    "📊 复盘面板": review.render,
    "📈 信号追踪": tracking.render,
    "📝 交易日志": journal.render,
    "⚙️ 数据采集": ingest.render,
}


def main() -> None:
    st.set_page_config(page_title="GXFC 控制台", page_icon="📈", layout="wide")
    st.sidebar.title("GXFC 控制台")
    choice = st.sidebar.radio("页面", list(_PAGES), label_visibility="collapsed")
    if choice != "⚙️ 数据采集" and not Path(DB_PATH).exists():
        st.info(f"本地库 {DB_PATH} 不存在,请先到「⚙️ 数据采集」页运行一次采集,"
                "或在命令行执行 python -m gxfc.ingest")
        return
    try:
        _PAGES[choice](DB_PATH)
    except duckdb.IOException:
        st.warning("数据库正被采集/筛选进程独占,请等待其完成后刷新页面")


main()
```

创建 `gxfc/web/pages_/review.py`：

```python
"""📊 复盘面板页:复刻 python -m gxfc.screen 的面板,可切换日期。"""
import streamlit as st

from gxfc.web import queries

_load_board = st.cache_data(ttl=600, show_spinner="组装面板…")(queries.load_board)


def render(db_path: str) -> None:
    st.header("📊 每日复盘面板")
    dates = queries.trading_dates(db_path)
    if not dates:
        st.info("库内无日K数据,请先到「⚙️ 数据采集」页运行采集")
        return
    date = st.selectbox("交易日", dates)
    board = _load_board(db_path, date)

    e = board.emotion
    st.subheader("市场情绪温度计")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("涨停家数", e.limit_up)
    c2.metric("跌停家数", e.limit_down)
    c3.metric("炸板率", f"{e.broken_board_rate:.1%}")
    c4.metric("最高板", f"{e.highest_streak} 板")
    up = "-" if e.up_count is None else e.up_count
    down = "-" if e.down_count is None else e.down_count
    st.caption(f"上涨/下跌家数:{up} / {down} · 量能状态:{e.volume_state}")
    st.info(e.sentiment_hint)

    st.subheader("板块涨幅榜")
    if board.sectors.empty:
        st.warning(f"{date} 板块数据未采集,该段降级为空")
    else:
        st.dataframe(board.sectors, hide_index=True, use_container_width=True)
        for name, cons in board.sector_cores.items():
            with st.expander(f"核心成分股:{name}"):
                st.dataframe(cons, hide_index=True, use_container_width=True)

    st.subheader("净利润断层候选")
    st.caption("proxy 口径:预告净利润同比增速,非券商一致预期")
    if len(board.candidates) > 0:
        st.dataframe(board.candidates, hide_index=True, use_container_width=True)
    else:
        st.warning("当日无达标候选(或业绩预告未采集)")

    st.subheader("底部爆量大涨")
    st.caption("全市场:大涨+爆量+底部低位;业绩高增叠加标记")
    surge = board.surge_candidates
    if surge is not None and len(surge) > 0:
        st.dataframe(surge, hide_index=True, use_container_width=True)
    else:
        st.warning("当日无达标候选(或日K数据不足)")
```

`tracking.py` / `journal.py` / `ingest.py` 本任务先创建**可运行占位**（Task 5-7 替换）：

```python
"""📈 信号追踪页(Task 5 完整实现)。"""
import streamlit as st


def render(db_path: str) -> None:
    st.header("📈 信号追踪")
    st.info("建设中")
```

（journal.py、ingest.py 同构，仅标题换成"📝 交易日志"/"⚙️ 数据采集"。）

- [ ] **Step 4: 运行测试确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_pages.py -v`
预期：2 passed。

- [ ] **Step 5: 人工冒烟（真实库）**

运行：`.venv\Scripts\python.exe -m streamlit run gxfc/web/app.py --server.headless true`
预期：终端输出 Local URL；浏览器打开 http://localhost:8501 能看到复盘面板。确认后 Ctrl+C 停止。

- [ ] **Step 6: 提交**

```bash
git add gxfc/web/app.py gxfc/web/pages_ tests/test_web_pages.py
git commit -m "feat: Web 控制台入口与复盘面板页(其余页面占位)"
```

---

### Task 5: 信号追踪页

**Files:**
- Modify: `gxfc/web/pages_/tracking.py`（替换占位）
- Test: `tests/test_web_pages.py`（追加）

**Interfaces:**
- Consumes: `queries.signal_strategies / tracking_report`（Task 2）；summary 列名 `策略/持有期/样本数/胜率%/平均收益%/中位收益%/盈亏比`；perf 含 `可追踪` 布尔列。
- Produces: `tracking.render(db_path)`。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_web_pages.py` 追加：

```python
def test_信号追踪页渲染(seeded_db, monkeypatch):
    at = _run_app(seeded_db, monkeypatch)
    at.sidebar.radio[0].set_value("📈 信号追踪")
    at = at.run()
    assert not at.exception
    # 有信号:策略下拉存在且包含 profit_fault
    assert "profit_fault" in at.selectbox[0].options


def test_信号追踪页无信号空态(tmp_path, monkeypatch):
    from gxfc.store.duck_store import DuckStore
    db = str(tmp_path / "nosig.duckdb")
    DuckStore(db).close()
    at = _run_app(db, monkeypatch)
    at.sidebar.radio[0].set_value("📈 信号追踪")
    at = at.run()
    assert not at.exception
    assert any("gxfc.screen" in str(i.value) for i in at.info)
```

- [ ] **Step 2: 运行确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_pages.py -v -k 信号追踪`
预期：FAIL（占位页没有策略下拉）。

- [ ] **Step 3: 实现 tracking.py**

替换 `gxfc/web/pages_/tracking.py` 全文：

```python
"""📈 信号追踪页:策略×持有期 胜率/盈亏比汇总图表 + 信号明细。"""
import plotly.express as px
import streamlit as st

from gxfc.web import queries

_report = st.cache_data(ttl=600, show_spinner="回算信号收益…")(queries.tracking_report)


def render(db_path: str) -> None:
    st.header("📈 信号追踪")
    strategies = queries.signal_strategies(db_path)
    if not strategies:
        st.info("尚无信号记录,先运行 python -m gxfc.screen(或到采集页「重跑筛选」)产生信号")
        return
    strategy = st.selectbox("策略", ["(全部)"] + strategies)
    perf, summary = _report(db_path, None if strategy == "(全部)" else strategy)
    if perf.empty:
        st.info("该条件下无信号")
        return

    untrackable = int((~perf["可追踪"]).sum()) if "可追踪" in perf.columns else 0
    if untrackable:
        st.caption(f"另有 {untrackable} 条信号因信号日无日K(停牌/未采集)不可追踪,未计入统计")

    st.subheader("策略 × 持有期汇总")
    if summary.empty:
        st.info("信号尚无可评估的持有期收益(未来交易日数据不足)")
    else:
        st.dataframe(summary, hide_index=True, use_container_width=True)
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(
                px.bar(summary, x="持有期", y="胜率%", color="策略",
                       barmode="group", title="胜率"),
                use_container_width=True)
        with c2:
            st.plotly_chart(
                px.bar(summary, x="持有期", y="平均收益%", color="策略",
                       barmode="group", title="平均收益"),
                use_container_width=True)

    st.subheader("信号明细")
    st.dataframe(perf, hide_index=True, use_container_width=True)
```

- [ ] **Step 4: 运行确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_pages.py -v`
预期：4 passed。

- [ ] **Step 5: 提交**

```bash
git add gxfc/web/pages_/tracking.py tests/test_web_pages.py
git commit -m "feat: 信号追踪页(胜率/平均收益图表+明细)"
```

---

### Task 6: 交易日志页

**Files:**
- Modify: `gxfc/web/pages_/journal.py`（替换占位）
- Test: `tests/test_web_pages.py`（追加）

**Interfaces:**
- Consumes: `queries.list_trades / trade_stats_report / signal_strategies`；`actions.journal_add_argv / journal_close_argv / run_action`。trades 列名 `trade_id/代码/名称/strategy/plan/open_date/open_price/shares/close_date/close_price/exit_reason/followed_plan/note`。
- Produces: `journal.render(db_path)`。写成功 → `st.session_state["journal_flash"]` + `st.rerun()`（渲染顶部消费 flash），并 `st.cache_data.clear()`。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_web_pages.py` 追加（AppTest 冒烟只测渲染与空态，不提交表单——表单提交会真起子进程，属人工验证范围）：

```python
def test_交易日志页渲染(seeded_db, monkeypatch):
    at = _run_app(seeded_db, monkeypatch)
    at.sidebar.radio[0].set_value("📝 交易日志")
    at = at.run()
    assert not at.exception
    # 已有一笔平仓交易:纪律统计表出现"按计划"分组
    assert len(at.dataframe) >= 1


def test_交易日志页空库(tmp_path, monkeypatch):
    from gxfc.store.duck_store import DuckStore
    db = str(tmp_path / "notrade.duckdb")
    DuckStore(db).close()
    at = _run_app(db, monkeypatch)
    at.sidebar.radio[0].set_value("📝 交易日志")
    at = at.run()
    assert not at.exception
    assert any("无已平仓交易" in str(i.value) for i in at.info)
```

- [ ] **Step 2: 运行确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_pages.py -v -k 交易日志`
预期：FAIL（占位页无 dataframe/提示）。

- [ ] **Step 3: 实现 journal.py**

替换 `gxfc/web/pages_/journal.py` 全文：

```python
"""📝 交易日志页:清单与纪律统计 + 网页开仓/平仓(子进程调 journal CLI)。

强制"先写计划再下单":开仓表单计划必填;平仓必选 按计划/破计划。
"""
import streamlit as st

from gxfc.web import actions, queries


def render(db_path: str) -> None:
    st.header("📝 交易日志")
    flash = st.session_state.pop("journal_flash", "")
    if flash:
        st.success(flash)

    stats = queries.trade_stats_report(db_path)
    st.subheader("纪律统计(全部/按计划/未按计划)")
    if stats.empty:
        st.info("无已平仓交易,统计从首笔平仓后开始")
    else:
        st.dataframe(stats, hide_index=True, use_container_width=True)

    open_trades = queries.list_trades(db_path, open_only=True)
    all_trades = queries.list_trades(db_path)
    tab_open, tab_all = st.tabs(["持仓中", "全部记录"])
    with tab_open:
        if open_trades.empty:
            st.caption("(无持仓)")
        else:
            st.dataframe(open_trades, hide_index=True, use_container_width=True)
    with tab_all:
        if all_trades.empty:
            st.caption("(无记录)")
        else:
            st.dataframe(all_trades, hide_index=True, use_container_width=True)

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        _add_form(db_path)
    with c2:
        _close_form(db_path, open_trades)


def _submit(argv: list) -> None:
    """执行写命令:成功则闪存消息+清缓存+重跑;失败就地报错。"""
    ok, out = actions.run_action(argv)
    if ok:
        st.cache_data.clear()
        st.session_state["journal_flash"] = out
        st.rerun()
    else:
        st.error(out[-1000:] or "命令执行失败")


def _add_form(db_path: str) -> None:
    st.subheader("开仓(先写计划再下单)")
    strategies = queries.signal_strategies(db_path) + ["手动"]
    with st.form("add_trade", clear_on_submit=True):
        code = st.text_input("股票代码")
        name = st.text_input("股票名称")
        strategy = st.selectbox("策略", strategies)
        date = st.date_input("开仓日")
        price = st.number_input("开仓价", min_value=0.01, step=0.01)
        shares = st.number_input("股数", min_value=100, step=100)
        plan = st.text_area("计划(买入理由+卖出规则,必填)")
        if st.form_submit_button("记录开仓", type="primary"):
            if not code.strip() or not plan.strip():
                st.error("股票代码与计划为必填项")
            else:
                _submit(actions.journal_add_argv(
                    db_path, code.strip(), name.strip(), strategy, plan.strip(),
                    date.strftime("%Y%m%d"), price, int(shares)))


def _close_form(db_path: str, open_trades) -> None:
    st.subheader("平仓(申报是否按计划)")
    if open_trades.empty:
        st.caption("(无持仓可平)")
        return
    options = {f'{r["trade_id"]} {r["代码"]} {r["名称"]}': r["trade_id"]
               for _, r in open_trades.iterrows()}
    with st.form("close_trade"):
        label = st.selectbox("交易", list(options))
        date = st.date_input("平仓日")
        price = st.number_input("平仓价", min_value=0.01, step=0.01)
        reason = st.selectbox("离场原因", ["规则卖点", "止损", "情绪", "其他"])
        followed = st.radio("执行情况", ["按计划", "破计划"], horizontal=True)
        note = st.text_input("备注(卖飞/拿住等复盘线索)")
        if st.form_submit_button("记录平仓", type="primary"):
            _submit(actions.journal_close_argv(
                db_path, options[label], date.strftime("%Y%m%d"), price,
                reason, followed == "按计划", note.strip()))
```

- [ ] **Step 4: 运行确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_pages.py -v`
预期：6 passed。

- [ ] **Step 5: 人工验证表单写路径（真实子进程）**

运行：`$env:GXFC_DB="out\manual_test.duckdb"; .venv\Scripts\python.exe -m streamlit run gxfc/web/app.py --server.headless true`
在交易日志页提交一笔开仓（代码 600000、任意计划）→ 预期顶部出现"已开仓 T…"，持仓表出现该行；再平仓 → 纪律统计出现数据。验证后删除 `out\manual_test.duckdb`。

- [ ] **Step 6: 提交**

```bash
git add gxfc/web/pages_/journal.py tests/test_web_pages.py
git commit -m "feat: 交易日志页(清单/纪律统计+网页开平仓表单)"
```

---

### Task 7: 数据采集页 + 离线纪律测试

**Files:**
- Modify: `gxfc/web/pages_/ingest.py`（替换占位）
- Test: `tests/test_web_pages.py`（追加）、`tests/test_web_offline.py`（新建）

**Interfaces:**
- Consumes: `queries.db_overview`；`actions.ingest_argv / screen_argv / start_stream`。
- Produces: `ingest.render(db_path)`。进程句柄存 `st.session_state["gxfc_proc"]`，运行期间按钮 disabled；日志行存 `st.session_state["gxfc_proc_log"]`。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_web_pages.py` 追加：

```python
def test_数据采集页渲染(seeded_db, monkeypatch):
    at = _run_app(seeded_db, monkeypatch)
    at.sidebar.radio[0].set_value("⚙️ 数据采集")
    at = at.run()
    assert not at.exception
    labels = [b.label for b in at.button]
    assert any("开始采集" in x for x in labels)
    assert any("重跑筛选" in x for x in labels)


def test_数据采集页库缺失引导(tmp_path, monkeypatch):
    at = _run_app(str(tmp_path / "nope.duckdb"), monkeypatch)
    at.sidebar.radio[0].set_value("⚙️ 数据采集")
    at = at.run()
    assert not at.exception
    assert any("尚不存在" in str(i.value) for i in at.info)
```

创建 `tests/test_web_offline.py`：

```python
"""离线纪律:import 整个 web 包(app 除外,app 顶层会执行渲染)不得引入 fetcher。"""
import importlib
import sys


def test_web包不引入fetcher():
    for m in [m for m in list(sys.modules) if m.startswith("gxfc")]:
        sys.modules.pop(m)
    importlib.import_module("gxfc.web.queries")
    importlib.import_module("gxfc.web.actions")
    for page in ("review", "tracking", "journal", "ingest"):
        importlib.import_module(f"gxfc.web.pages_.{page}")
    assert "gxfc.data.fetcher" not in sys.modules
```

- [ ] **Step 2: 运行确认失败**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_pages.py -k 数据采集 tests/test_web_offline.py -v`
预期：采集页两条 FAIL（占位页无按钮/引导）；离线纪律条 PASS（占位页本就不触网，通过属正常）。

- [ ] **Step 3: 实现 ingest.py**

替换 `gxfc/web/pages_/ingest.py` 全文：

```python
"""⚙️ 数据采集页:库状态总览 + 一键采集/重跑筛选(子进程+实时日志)。

DuckDB 单写者:同一时刻只允许一个采集/筛选子进程,运行期间按钮置灰。
进程句柄与日志存 session_state,页面被交互打断后可续读输出。
"""
from pathlib import Path

import streamlit as st

from gxfc.web import actions, queries


def render(db_path: str) -> None:
    st.header("⚙️ 数据采集")
    overview = queries.db_overview(db_path)
    if overview is None:
        st.info(f"本地库 {db_path} 尚不存在,点击「开始采集」将自动创建并回补历史"
                "(首次耗时较长,中断重跑自动续传)")
    else:
        st.caption(f'日K最新日期:{overview["daily_max"] or "(无)"}')
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("各表行数")
            st.dataframe(overview["tables"], hide_index=True, use_container_width=True)
        with c2:
            st.subheader("最近采集台账")
            if overview["recent_log"].empty:
                st.caption("(无台账)")
            else:
                st.dataframe(overview["recent_log"], hide_index=True,
                             use_container_width=True)

    st.divider()
    proc = st.session_state.get("gxfc_proc")
    running = proc is not None and proc.poll() is None
    c1, c2 = st.columns(2)
    if c1.button("开始采集(联网)", disabled=running, type="primary"):
        _launch(actions.ingest_argv(db_path), "采集")
    if c2.button("重跑筛选(离线)", disabled=running or not Path(db_path).exists()):
        _launch(actions.screen_argv(db_path), "筛选")
    _render_progress()


def _launch(argv: list, name: str) -> None:
    st.session_state["gxfc_proc"] = actions.start_stream(argv)
    st.session_state["gxfc_proc_name"] = name
    st.session_state["gxfc_proc_log"] = []
    st.rerun()


def _render_progress() -> None:
    proc = st.session_state.get("gxfc_proc")
    if proc is None:
        return
    name = st.session_state.get("gxfc_proc_name", "任务")
    lines = st.session_state.setdefault("gxfc_proc_log", [])
    if proc.poll() is None:
        with st.status(f"{name}进行中…", expanded=True):
            box = st.empty()
            for line in proc.stdout:      # 阻塞直至进程结束,期间实时刷新
                lines.append(line.rstrip())
                box.code("\n".join(lines[-40:]))
        proc.wait()
        st.rerun()
        return
    st.session_state["gxfc_proc"] = None
    if proc.returncode == 0:
        st.cache_data.clear()             # 数据已更新,面板/追踪缓存作废
        st.success(f"{name}完成")
        st.code("\n".join(lines[-20:]) or "(无输出)")
    else:
        st.error(f"{name}失败(退出码 {proc.returncode}),日志尾部:")
        st.code("\n".join(lines[-50:]) or "(无输出)")
```

- [ ] **Step 4: 运行确认通过**

运行：`.venv\Scripts\python.exe -m pytest tests/test_web_pages.py tests/test_web_offline.py -v`
预期：全部 passed（页面 8 条 + 离线 1 条）。

- [ ] **Step 5: 人工验证"重跑筛选"流式日志（离线,秒级)**

运行：`.venv\Scripts\python.exe -m streamlit run gxfc/web/app.py --server.headless true`
在采集页点"重跑筛选(离线)" → 预期 st.status 内滚动 screen 日志,结束显示"筛选完成"。（"开始采集"联网耗时长,不在本步强制验证。）

- [ ] **Step 6: 提交**

```bash
git add gxfc/web/pages_/ingest.py tests/test_web_pages.py tests/test_web_offline.py
git commit -m "feat: 数据采集页(一键采集/筛选+实时日志)与 web 离线纪律测试"
```

---

### Task 8: README 更新 + 全量回归收尾

**Files:**
- Modify: `README.md`（"四个 CLI 入口"表格后加 Web 控制台小节；技术栈行加 Streamlit/plotly；项目结构树加 `gxfc/web/`）

**Interfaces:**
- Consumes: 前七个任务全部产物。
- Produces: 文档与最终验证。

- [ ] **Step 1: 更新 README**

在"四个 CLI 入口"表格之后插入：

```markdown
## Web 控制台（本地网页）

```powershell
python -m streamlit run gxfc/web/app.py
```

浏览器打开 http://localhost:8501，四个页面：📊 复盘面板（切换日期）、📈 信号追踪（胜率/盈亏比图表）、📝 交易日志（网页开平仓）、⚙️ 数据采集（一键采集+实时日志）。库路径可用环境变量 `GXFC_DB` 覆盖。网页读库走只读短连接、写库走子进程调 CLI，网页开着不影响命令行操作。
```

同步更新"项目结构"树（`gxfc/web/` 四个文件 + `pages_/`）与"技术栈"行（追加 Streamlit + plotly）。设计文档索引追加 `specs/2026-07-13-web-console-design.md`。

- [ ] **Step 2: 全量回归**

运行：`.venv\Scripts\python.exe -m pytest -v`
预期：全部通过（既有测试 + 新增 web 测试），零网络。

- [ ] **Step 3: 提交**

```bash
git add README.md
git commit -m "docs: README 补充 Web 控制台使用说明"
```

---

## 自审记录

- **规格覆盖**：spec §2 架构三铁律 → Task 1/2/3；§3.1-3.4 四页面 → Task 4-7；§4 错误处理四档 → app.py IOException 捕获（Task 4）、空态 st.info（Task 2 查询层 + 各页）、子进程失败展示（Task 6/7）、库缺失引导（Task 4）；§5 测试四条 → Task 2（queries 单测）、Task 3（actions 打桩）、Task 4-7（AppTest）、Task 7（离线纪律）；§6 依赖 → Task 1、启动命令 → Task 8 README。无遗漏。
- **占位符扫描**：Task 4 的三个页面占位是显式设计（可运行、后续任务替换），非未完成项；其余无 TBD/省略。
- **类型一致性**：`render(db_path: str)` 四页统一；`tracking_report` 返回 (perf, summary) 二元组在 Task 2 定义、Task 5 消费一致；`run_action` 返回 (bool, str) 在 Task 3 定义、Task 6 消费一致。
