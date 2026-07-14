# 量能状态点亮实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按 [2026-07-14 量能状态设计](../specs/2026-07-14-volume-state-design.md) 把复盘面板的量能状态从固定"数据不足"点亮为 放量/缩量/平量（带比值）。

**Architecture:** 三层各一处小改动：`DuckStore.market_turnover`（daily 表按日 SUM 成交额）→ `compute_market_emotion` 新可选参数与判定 → `screen._emotion_offline` 接线。展示层零改动。

**Tech Stack:** DuckDB SQL 聚合、纯函数因子、pytest。

## Global Constraints

- 零网络、零新采集：两市成交额只从本地 `daily` 表重建。
- `compute_market_emotion` 新参数全部带默认值：不传时行为与现状完全一致，既有测试（含 `assert e.volume_state == "数据不足"`）必须原样通过。
- 阈值：`volume_up_ratio=1.15`（≥ 放量）、`volume_down_ratio=0.85`（≤ 缩量），来自 `config/strategy.yaml` 既有键。
- 文案格式（精确）：`放量({ratio:.2f})` / `缩量({ratio:.2f})` / `平量({ratio:.2f})`；缺数据一律 `数据不足`。
- 所有注释、提交信息、测试名简体中文；测试零网络；每任务一次提交。

---

### Task 1: 存储层 market_turnover + 因子层量能判定

**Files:**
- Modify: `gxfc/store/duck_store.py`（daily 序列表方法区，`read_market_pct` 附近；顶部 import 需补 `timedelta`）
- Modify: `gxfc/factors/market_emotion.py`（签名 + 量能判定 + docstring 更新，去掉"阶段2"表述）
- Test: `tests/test_market_turnover.py`（新建）、`tests/test_market_emotion.py`（追加）

**Interfaces:**
- Consumes: `daily` 表列（代码/日期/成交额）、`gxfc.dates.dash`。
- Produces:
  - `DuckStore.market_turnover(date: str, baseline_days: int = 5) -> tuple` — `(今日两市总成交额|None, 前N个有数据交易日均额|None)`；当日无行 → `(None, None)`；历史不足 N 天 → `(turnover, None)`。
  - `compute_market_emotion(..., turnover=None, turnover_baseline=None, volume_up_ratio=1.15, volume_down_ratio=0.85)` — Task 2 按此签名接线。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_market_turnover.py`：

```python
"""DuckStore.market_turnover:两市总成交额离线重建的三态行为。"""
import pandas as pd

from gxfc.store.duck_store import DuckStore

_DAYS = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07", "2026-07-08"]


def _daily(code: str, dates_amounts) -> pd.DataFrame:
    return pd.DataFrame([
        {"代码": code, "日期": d, "开盘": 10.0, "收盘": 10.0, "最高": 10.0,
         "最低": 10.0, "成交量": 1e6, "成交额": amt, "换手率": 1.0}
        for d, amt in dates_amounts
    ])


def test_正常_今日总额与前5日均额(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    try:
        # 单票逐日 100,200,...,600;另一票每日 50,验证按日加总
        store.append_daily(_daily("600000", [(d, 100.0 * (i + 1)) for i, d in enumerate(_DAYS)]))
        store.append_daily(_daily("000001", [(d, 50.0) for d in _DAYS]))
        turnover, baseline = store.market_turnover("2026-07-08")
        assert turnover == 650.0                    # 600 + 50
        assert baseline == 350.0                    # (150+250+350+450+550)/5
    finally:
        store.close()


def test_当日无行返回双None(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    try:
        store.append_daily(_daily("600000", [(d, 100.0) for d in _DAYS[:-1]]))
        assert store.market_turnover("2026-07-08") == (None, None)
    finally:
        store.close()


def test_历史不足5日基准为None(tmp_path):
    store = DuckStore(str(tmp_path / "t.duckdb"))
    try:
        store.append_daily(_daily("600000", [(d, 100.0) for d in _DAYS[2:]]))  # 仅4天
        turnover, baseline = store.market_turnover("2026-07-08")
        assert turnover == 100.0
        assert baseline is None
    finally:
        store.close()
```

在 `tests/test_market_emotion.py` 末尾追加（沿用该文件既有 `_zt/_dt/_zb` 辅助）：

```python
def test_量能_放量():
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0),
                               turnover=1.2e12, turnover_baseline=1.0e12)
    assert e.volume_state == "放量(1.20)"


def test_量能_缩量():
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0),
                               turnover=8.0e11, turnover_baseline=1.0e12)
    assert e.volume_state == "缩量(0.80)"


def test_量能_平量():
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0),
                               turnover=1.0e12, turnover_baseline=1.0e12)
    assert e.volume_state == "平量(1.00)"


def test_量能_缺基准仍数据不足():
    e = compute_market_emotion(_zt([1]), _dt(1), _zb(0), turnover=1.0e12)
    assert e.volume_state == "数据不足"
```

- [ ] **Step 2: 运行确认失败**

`.venv\Scripts\python.exe -m pytest tests/test_market_turnover.py tests/test_market_emotion.py -v`
预期：market_turnover 三条 FAIL（AttributeError），量能四条 FAIL（TypeError: unexpected keyword）；既有用例 PASS。

- [ ] **Step 3: 实现**

`gxfc/store/duck_store.py`：顶部 `from datetime import datetime` 改为 `from datetime import datetime, timedelta`；在 `read_market_pct` 之后新增：

```python
    def market_turnover(self, date: str, baseline_days: int = 5) -> tuple:
        """今日两市总成交额与之前 baseline_days 个有数据交易日的均额。

        返回 (turnover, baseline):当日 daily 无行返回 (None, None);
        历史有数据交易日不足 baseline_days 天时 baseline 为 None。
        限窗最近 30 个自然日(覆盖 5 个交易日绰绰有余);全天成交额皆
        NULL 的日期不计入基准天数。date 兼容 'YYYYMMDD'。
        """
        d = dash(date)
        start = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = self._con.execute(
            '''SELECT "日期", sum("成交额") AS total FROM daily
               WHERE "日期" BETWEEN ? AND ? AND "成交额" IS NOT NULL
               GROUP BY "日期" ORDER BY "日期" DESC LIMIT ?''',
            [start, d, baseline_days + 1],
        ).fetchall()
        if not rows or rows[0][0] != d or rows[0][1] is None:
            return None, None
        turnover = float(rows[0][1])
        hist = [float(r[1]) for r in rows[1:] if r[1] is not None]
        if len(hist) < baseline_days:
            return turnover, None
        return turnover, sum(hist) / len(hist)
```

`gxfc/factors/market_emotion.py`：

- 模块 docstring 第 5 行"阶段2接入成交额后量能状态将解锁。"改为"量能状态由调用方传入两市总成交额与前N日均额后解锁(离线由 daily 表重建)。"
- 签名追加四个参数（在 `cold_up_count` 之后）：

```python
    turnover: Optional[float] = None,
    turnover_baseline: Optional[float] = None,
    volume_up_ratio: float = 1.15,
    volume_down_ratio: float = 0.85,
```

- docstring Args 补四行说明（turnover 今日两市总成交额；turnover_baseline 前N日均额；两阈值判放量/缩量）。
- 第 69-70 行替换为：

```python
    # 量能状态:今日两市总成交额对前N日均额;任一缺失或基准非正则数据不足
    if turnover is not None and turnover_baseline is not None and turnover_baseline > 0:
        ratio = turnover / turnover_baseline
        if ratio >= volume_up_ratio:
            volume_state = f"放量({ratio:.2f})"
        elif ratio <= volume_down_ratio:
            volume_state = f"缩量({ratio:.2f})"
        else:
            volume_state = f"平量({ratio:.2f})"
    else:
        volume_state = "数据不足"
```

- [ ] **Step 4: 运行确认通过**

`.venv\Scripts\python.exe -m pytest tests/test_market_turnover.py tests/test_market_emotion.py -v` 全过，随后全量 pytest 无回归。

- [ ] **Step 5: 提交**

```bash
git add gxfc/store/duck_store.py gxfc/factors/market_emotion.py tests/test_market_turnover.py tests/test_market_emotion.py
git commit -m "feat: 量能状态判定(两市成交额离线重建+放量/缩量/平量)"
```

---

### Task 2: screen 接线 + config 注释 + 集成测试

**Files:**
- Modify: `gxfc/screen.py:52-61`（`_emotion_offline` 已采集分支）
- Modify: `config/strategy.yaml`（emotion 段两行"阶段2"注释）
- Test: `tests/test_screen.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `store.market_turnover(date)` 与 `compute_market_emotion` 新参数。
- Produces: 面板（CLI/网页）`volume_state` 自动点亮，无其他消费方需改。

- [ ] **Step 1: 写失败测试**

在 `tests/test_screen.py` 追加（import 区按该文件既有风格补 `DuckStore`/`pd`，若已有则不重复）：

```python
def test_量能状态接线(tmp_path):
    """三池已采集且日K充足时,情绪段量能不再是"数据不足"。"""
    from gxfc.screen import _emotion_offline
    from gxfc.store.duck_store import DuckStore
    store = DuckStore(str(tmp_path / "s.duckdb"))
    try:
        days = ["2026-07-01", "2026-07-02", "2026-07-03",
                "2026-07-06", "2026-07-07", "2026-07-08"]
        store.append_daily(pd.DataFrame([
            {"代码": "600000", "日期": d, "开盘": 10.0, "收盘": 10.0, "最高": 10.0,
             "最低": 10.0, "成交量": 1e6, "成交额": 1e8, "换手率": 1.0}
            for d in days
        ]))
        date = "2026-07-08"
        store.upsert_snapshot("zt_pool", "trade_date", date,
                              pd.DataFrame({"代码": ["600000"], "名称": ["甲"],
                                            "连板数": [1], "炸板次数": [0]}))
        store.upsert_snapshot("dt_pool", "trade_date", date,
                              pd.DataFrame({"代码": ["000002"], "名称": ["乙"]}))
        store.upsert_snapshot("zb_pool", "trade_date", date,
                              pd.DataFrame({"代码": ["000003"], "名称": ["丙"]}))
        store.log("t", "zt_pool", date, "ok")
        emo_cfg = {"hot_up_count": 4500, "cold_up_count": 800,
                   "volume_up_ratio": 1.15, "volume_down_ratio": 0.85}
        e = _emotion_offline(store, date, emo_cfg)
        assert e.volume_state == "平量(1.00)"   # 每日总额相同 → 比值恰为 1
    finally:
        store.close()
```

- [ ] **Step 2: 运行确认失败**

`.venv\Scripts\python.exe -m pytest tests/test_screen.py -v -k 量能`
预期：FAIL（volume_state 仍为"数据不足"，接线未通）。

- [ ] **Step 3: 实现**

`gxfc/screen.py` `_emotion_offline` 已采集分支改为：

```python
    zt = store.read_snapshot("zt_pool", "trade_date", date)
    dt = store.read_snapshot("dt_pool", "trade_date", date)
    zb = store.read_snapshot("zb_pool", "trade_date", date)
    pct = store.read_market_pct(date)
    turnover, baseline = store.market_turnover(date)
    return compute_market_emotion(
        zt, dt, zb, spot_df=pct if not pct.empty else None,
        hot_up_count=emo_cfg["hot_up_count"],
        cold_up_count=emo_cfg["cold_up_count"],
        turnover=turnover,
        turnover_baseline=baseline,
        volume_up_ratio=emo_cfg["volume_up_ratio"],
        volume_down_ratio=emo_cfg["volume_down_ratio"],
    )
```

`_emotion_offline` docstring 首行补一句"量能由 daily 表离线重建的两市成交额判定"。

`config/strategy.yaml` emotion 段：把两条"阶段2接入两市成交额历史后生效;当前主流程未喂成交额,量能状态显示'数据不足'"注释替换为一条：

```yaml
  # 量能:今日两市总成交额 / 前5个有数据交易日均额(由 daily 表离线重建)
```

（保留 `volume_up_ratio`/`volume_down_ratio` 两键与取值不变。）

- [ ] **Step 4: 运行确认通过**

`.venv\Scripts\python.exe -m pytest tests/test_screen.py -v` 全过；随后全量 pytest 无回归（既有 web AppTest 中面板 caption 文案变化不影响断言——现有测试未断言量能文案）。

- [ ] **Step 5: 真实库冒烟**

`.venv\Scripts\python.exe -m gxfc.screen`（真实库离线秒级）：确认面板"量能状态"显示 放量/缩量/平量 之一（若真实库历史充足），并在报告记录实际输出行。

- [ ] **Step 6: 提交**

```bash
git add gxfc/screen.py config/strategy.yaml tests/test_screen.py
git commit -m "feat: 情绪段接入量能状态(screen 编排+配置注释更新)"
```

---

## 自审记录

- **规格覆盖**：spec §2.1 → Task 1 存储层；§2.2 → Task 1 因子层；§2.3 → Task 2；§3 错误处理三态 → Task 1 三条 market_turnover 测试 + 因子缺参测试；§4 测试三组 → Task 1/Task 2 步骤。无遗漏。
- **占位符**：无。
- **类型一致性**：`market_turnover -> tuple(Optional[float], Optional[float])` 在 Task 1 定义、Task 2 以 `turnover, baseline = ...` 消费一致；`compute_market_emotion` 新参数名两任务拼写一致。
