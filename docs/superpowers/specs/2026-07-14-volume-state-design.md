# 市场情绪量能状态点亮 — 设计

生成时间：2026-07-14
状态：已确认
前置设计：[2026-06-29 选股系统总体设计](2026-06-29-a-share-stock-screening-design.md)（量能状态在阶段1即预留，标注"阶段2接入两市成交额后解锁"）。本篇即该阶段2：不新增采集，只用本地 `daily` 表离线重建。

---

## 1. 目标与非目标

**目标**

1. 复盘面板（CLI 与网页）的"量能状态"从固定"数据不足"变为真实的 放量/缩量/平量 判定（带比值），口径：今日两市总成交额 / 前 5 个有数据交易日的均额。
2. 零网络、零新采集：两市总成交额由 `daily` 表按日 `SUM(成交额)` 离线重建。
3. 数据不足时（今日无行 / 历史不足 5 日）诚实维持"数据不足"，不掺估算。

**非目标**

- 不在采集端新增"两市成交额"数据集（本地已可推导）。
- 不把量能纳入情绪提示（sentiment_hint）的判定逻辑——本轮只点亮展示，是否参与判定留后续。
- 不做分市场（沪/深分开）口径。

## 2. 设计

三层各一处小改动，展示层零改动（CLI 面板与网页复盘页本就渲染 `volume_state` 字段）。

### 2.1 存储层：`DuckStore.market_turnover`

```python
def market_turnover(self, date: str, baseline_days: int = 5) -> tuple:
    """今日两市总成交额与之前 baseline_days 个有数据交易日的均额。

    返回 (turnover, baseline):
    - turnover: date 当日 SUM(成交额);当日 daily 无行返回 (None, None)。
    - baseline: date 之前(不含当日)最近 baseline_days 个有数据交易日的
      日总成交额均值;不足 baseline_days 天返回 None。
    date 兼容 'YYYYMMDD'。SQL 限窗最近 30 个自然日(覆盖 5 交易日绰绰有余)。
    """
```

单条 SQL：按 `日期` 分组 `SUM(成交额)`，`日期 <= date AND 日期 >= date-30天`，倒序取 `baseline_days + 1` 行；首行日期必须等于 date，否则视为当日无数据。

### 2.2 因子层：`compute_market_emotion` 新参数

```python
def compute_market_emotion(
    zt_df, dt_df, zb_df, spot_df=None,
    hot_up_count=4500, cold_up_count=800,
    turnover: Optional[float] = None,
    turnover_baseline: Optional[float] = None,
    volume_up_ratio: float = 1.15,
    volume_down_ratio: float = 0.85,
) -> MarketEmotion:
```

量能判定：`turnover` 与 `turnover_baseline` 均非 None 且 baseline > 0 时，`ratio = turnover / turnover_baseline`：

- ratio ≥ volume_up_ratio → `f"放量({ratio:.2f})"`
- ratio ≤ volume_down_ratio → `f"缩量({ratio:.2f})"`
- 其余 → `f"平量({ratio:.2f})"`

任一缺失或 baseline ≤ 0 → 维持 `"数据不足"`。**不传新参数时行为与现状完全一致**（既有测试 `assert e.volume_state == "数据不足"` 不破）。模块与函数 docstring 中"阶段2解锁"的表述同步更新。

### 2.3 编排层：`screen._emotion_offline` 接线

三池已采集的分支里调 `store.market_turnover(date)`，把 `(turnover, baseline)` 与 `emo_cfg["volume_up_ratio"]` / `emo_cfg["volume_down_ratio"]` 传入因子。降级分支（zt_pool 未采集）不变。`config/strategy.yaml` 中两行"阶段2生效"注释改为已生效的口径说明（今日两市总成交额/前5日均额，由 daily 表离线重建）。

## 3. 错误处理

| 场景 | 行为 |
|------|------|
| 当日 daily 无行（未采集/非交易日） | market_turnover 返回 (None, None) → "数据不足" |
| 历史有数据交易日不足 5 天 | baseline=None → "数据不足" |
| 成交额列含 NULL | SUM 自动忽略 NULL；某日全 NULL 则该日总额为 NULL，按无数据处理（不计入基准天数） |

**已知限制**：基准口径按"有数据的交易日"计天，不校验当日样本股票数是否完整；历史回补不完整时（如部分日期仅少数股票有日K行）日总额被低估、比值虚高。补齐历史（`python -m gxfc.ingest`）后自然修复；如需彻底防护可在后续给 market_turnover 加单日样本量下限校验。

## 4. 测试

零网络，沿用既有测试文件：

1. `tests/test_market_emotion.py` 追加：放量/缩量/平量/仅传 turnover 缺 baseline → 数据不足，共 4 条；既有"数据不足"用例保持通过。
2. `tests/test_duck_store_readonly.py` 或新文件追加 `market_turnover` 三态：正常（6 日数据取前 5 均值）、当日无行、历史不足。
3. `tests/test_screen.py` 集成：seeded 数据下 `_emotion_offline`（或 build_board_offline）产出的 `volume_state` 不再恒为"数据不足"（历史充足时）。

## 5. 影响面

- 网页复盘页 `st.caption` 与 CLI 面板自动显示新文案，无代码改动。
- `compute_market_emotion` 新参数全部带默认值，所有既有调用方无需修改。
