# AKShare 1.17.83 接口适配报告

生成时间：2026-06-30

---

## 一、变更概述

针对联网冒烟暴露的接口与列名差异，对 5 处代码文件做了精确外科修复，同步更新对应测试；不涉及架构调整。

---

## 二、逐条改动说明

### 改动 1：`gxfc/factors/market_emotion.py` — 情绪改用三个涨跌停池

**问题**：`ak.stock_market_activity_legu` 在 AKShare 1.17.83 已损坏（返回错误或空）。

**新签名**：
```python
def compute_market_emotion(
    zt_df, dt_df, zb_df, spot_df=None,
    hot_up_count=4500, cold_up_count=800,
) -> MarketEmotion:
```

**计算逻辑变化**：
- `limit_up = len(zt_df)`；`limit_down = len(dt_df)`；`broken = len(zb_df)`
- `broken_board_rate = broken / (limit_up + broken)`，分母为 0 则 0.0
- `highest_streak`：`zt_df["连板数"]` 数值化后的最大值，空池为 0
- `up_count/down_count`：有 `spot_df` 且含 `涨跌幅` 列时精确统计，否则为 `None`（Optional[int]）
- `volume_state`：固定 "数据不足"（阶段 2 接成交额后解锁）
- `sentiment_hint`：有 spot 时走旧逻辑；无 spot 时用涨跌停池粗粒度判断（偏冷/偏热/中性）

**DataClass 字段**：`up_count/down_count` 类型升级为 `Optional[int]`，其余字段不变。

---

### 改动 2：`gxfc/data/fetcher.py` — 增删接口方法

**删除**：
- `market_activity()`（legu 接口已坏，无替代必要）

**新增**：
- `dt_pool(date)` → `ak.stock_zt_pool_dtgc_em(date=date)`；列含 代码, 名称, 涨跌幅, 连续跌停 等
- `zb_pool(date)` → `ak.stock_zt_pool_zbgc_em(date=date)`；列含 代码, 名称, 涨跌幅, 炸板次数 等
- `spot()` → `ak.stock_zh_a_spot_em()`（use_cache=False）；列含 涨跌幅；docstring 注明限流风险与调用方降级策略

**更新 docstring**：
- `industry_board`：列名由 `领涨股` 更正为 `领涨股票`
- `yjyg`：列清单更新为真实多行结构（含 预测指标, 业绩变动幅度 等 9 列）

---

### 改动 3：`gxfc/factors/profit_fault.py` — 增速列与预测指标过滤

**问题**：AKShare `stock_yjyg_em` 每只股票返回多行（每行一个预测指标），原代码假设单行 `预测净利润-同比增长` 列已不存在。

**新常量**：
```python
_METRIC_COL = "预测指标"
_GROWTH_COL = "业绩变动幅度"
_NET_PROFIT = "归属于上市公司股东的净利润"
```

**新过滤逻辑**（在 `scan_profit_fault` 遍历头部）：
```python
if r.get(_METRIC_COL) != _NET_PROFIT:
    continue  # 跳过营收行、扣非行等
growth = r.get(_GROWTH_COL)  # 业绩变动幅度(%)
```

**不变**：`detect_gap`、`passes_growth`、返回列 `股票代码,股票简称,同比增长,有跳空`。

---

### 改动 4：`gxfc/factors/sector.py` — 领涨列名修正

**问题**：AKShare 1.17.83 `stock_board_industry_name_em` 返回列名为 `领涨股票`，原代码用 `领涨股`（少一个字）导致容错分支始终触发、领涨股数据丢失。

**修改**：`_SECTOR_COLS` 中 `"领涨股"` → `"领涨股票"`（单字符变更）。

---

### 改动 5：`gxfc/main.py` — 接口适配与礼貌延迟

**build_board 情绪部分**：
```python
# 旧
activity = fetcher.market_activity()
zt = fetcher.zt_pool(date)
emotion = compute_market_emotion(activity, zt, ...)

# 新
zt = fetcher.zt_pool(date)
dt = fetcher.dt_pool(date)
zb = fetcher.zb_pool(date)
spot = None  # TODO: 阶段2可接 fetcher.spot()
emotion = compute_market_emotion(zt, dt, zb, spot_df=spot, ...)
```

**礼貌延迟**：
- 板块成分股下钻循环：每次成功调用 `industry_cons` 后 `time.sleep(0.3)`
- 日K 拉取循环：每次成功调用 `stock_daily` 后 `time.sleep(0.3)`；同时去重（多行 yjyg 同一股票只拉一次）

**默认参数**：`top_codes_limit` 从 30 降为 20。

---

## 三、测试文件变更

| 测试文件 | 主要变化 |
|---|---|
| `tests/test_market_emotion.py` | 完全重写；fixture 改为三池；12 个测试覆盖基础计数/最高板/空池/无 spot 三种提示/有 spot 两种提示 |
| `tests/test_profit_fault.py` | yjyg fixture 改为多行结构（含 `预测指标` + `业绩变动幅度`）；新增"仅营收行不入选"测试；保留边界用例 |
| `tests/test_sector.py` | fixture 中 `领涨股` → `领涨股票`；容错测试断言列名同步更新 |
| `tests/test_main.py` | FakeFetcher 删除 `market_activity`，新增 `zt_pool/dt_pool/zb_pool`；yjyg 改为多行结构；industry_board 用 `领涨股票`；断言改为 `limit_up==3` 与 `up_count is None` |

---

## 四、测试命令与结果

```
python -m pytest -v
```

**结果**：40 passed in 3.75s（无新警告，无 skip）

测试分布：
- test_cache.py: 3
- test_daily_board.py: 2
- test_fetcher.py: 3
- test_main.py: 4
- test_market_emotion.py: 12（新增 6 个）
- test_profit_fault.py: 9（新增 2 个）
- test_sector.py: 5
- test_smoke.py: 2

原始 31 passed → 现在 40 passed（新增 9 个测试覆盖新行为）。

---

## 五、变更文件清单

| 文件 | 变更类型 |
|---|---|
| `gxfc/factors/market_emotion.py` | 重写（新签名，新计算逻辑） |
| `gxfc/data/fetcher.py` | 修改（删 1 方法，增 3 方法，更新 2 docstring） |
| `gxfc/factors/profit_fault.py` | 修改（新常量，新过滤逻辑） |
| `gxfc/factors/sector.py` | 修改（1 处列名字符修正） |
| `gxfc/main.py` | 修改（接口适配，sleep，去重，默认参数） |
| `tests/test_market_emotion.py` | 重写 |
| `tests/test_profit_fault.py` | 重写 |
| `tests/test_sector.py` | 修改（列名同步） |
| `tests/test_main.py` | 修改（FakeFetcher + 断言更新） |

---

## 六、顾虑与后续事项

1. **`spot()` 未接入主流程**：`main.py` 中 `spot = None`，情绪判断退为涨跌停池粗粒度。阶段 2 可将 `spot = fetcher.spot()` 放入 `try/except` 包裹（失败降级为 None），届时精确涨跌家数和量能状态将同步解锁。

2. **yjyg 多行 + head(N) 的语义漂移**：`yjyg.head(20)` 取的是行而非股票数，10 只股票 × 2 指标 = 20 行。若某票有 3+ 个指标行，`head(20)` 覆盖的股票数会减少。建议阶段 2 改为先 deduplicate 股票代码再 head，或直接在 fetcher 层按代码数截断。

3. **真实冒烟未做**：依规范不联网验证，由控制者在真实环境执行。
