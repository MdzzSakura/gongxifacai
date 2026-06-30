# 全市场"底部爆量大涨"扫描 — 架构设计

生成时间:2026-06-30
状态:设计确认,待落地
背景:在现有盘后复盘面板基础上扩大扫描面到全市场,以"量价关系"为主筛选信号。
用户选定策略:**底部爆量大涨**(地量地价后突放巨量 + 大涨,趋势启动信号)。

---

## 1. 策略定义

三条件同时满足即入选:

| 条件 | 含义 | 计算口径(均基于 Baostock 前复权日K) |
|------|------|------|
| **大涨** | 当日涨幅够大 | `今日涨跌幅 = (今收 - 昨收)/昨收 ≥ 7%` |
| **爆量** | 成交量突然放大 | `量比 = 今日成交量 / 前5日平均成交量 ≥ 2` |
| **底部** | 此前处于相对低位 | `今收 ≤ 最近60日最高价 × 0.6`(距高点回落 40%+ 视为低位) |

阈值默认值:`涨幅≥7%`、`量比≥2`、`底部系数=0.6`、`底部窗口=60日`、`量比基准=5日`。全部可配。

叠加标记(非筛选条件):命中"业绩预告净利润同比增速 ≥ 阈值"集合的 → 标 `业绩高增=True`。

---

## 2. 混合架构(为什么不被封、当天可用)

```
第1步 粗筛(1 次联网,零历史)
  新浪 stock_zh_a_spot(全市场 ~5500 只,含北交所)
    → 仅按 今日涨跌幅 ≥ 7% 留下幸存者(通常 5000 → 几十~两三百只)

第2步 精算(只对幸存者,每只 1 次 Baostock)
  对每个幸存者拉 Baostock 60 日前复权日K(复用 fetcher.stock_daily,走现有缓存)
    → 从日K现算 大涨/爆量/底部 三条件(口径统一、前复权精确)
    → 三条件全过 → 入选

第3步 叠加 业绩高增 标记(复用已采业绩预告)
```

- **请求量** = 1 次新浪 + 数十~数百次 Baostock(均不碰东财 push2),稳定不被封。
- **零预热**:不累积快照、不做全市场逐股首采,第一天即可用。
- **北交所**:新浪粗筛含北交所,但 Baostock 不覆盖北交所 → 北交所幸存者无法精算,跳过并日志标注。

口径说明:大涨/爆量/底部**全部从 Baostock 日K 现算**(单位一致、前复权);新浪 `涨跌幅` 仅用于粗筛缩小范围,不参与最终判定。盘后运行时 Baostock 当日K已更新,可取到今日 bar。

---

## 3. 组件划分

```
gxfc/
├── data/fetcher.py        # 【改】新增 market_spot():新浪全市场快照(粗筛用)
├── factors/bottom_volume.py  # 【新】纯函数:底部爆量大涨筛选
├── main.py                # 【改】build_board 集成扫描;run_daily 不变
└── review/daily_board.py  # 【改】DailyBoard 加 surge_candidates;渲染 + CSV
```

### 3.1 Fetcher.market_spot()
- 调新浪 `stock_zh_a_spot`,归一化:`代码`(去 sh/sz/bj 前缀转6位)、`名称`、`涨跌幅`、`最新价`、`成交额`。
- 专用方法,只走新浪(含北交所、列稳定);现有 `spot()`(东财主、算涨跌家数)不动。

### 3.2 factors/bottom_volume.py(纯函数,可单测)
- `scan_bottom_volume(survivors_df, daily_map, high_growth_codes, *, rise_threshold=7.0, volume_ratio_threshold=2.0, bottom_ratio=0.6, bottom_window=60, volume_baseline=5) -> DataFrame`
  - `survivors_df`:粗筛幸存者(代码、名称、成交额 等,来自新浪快照)。
  - `daily_map`:`{代码: 60日日K DataFrame}`(列:日期/开盘/收盘/最高/最低/成交量)。
  - 对每只:数据不足(<bottom_window 或 <volume_baseline+1 根)→ 跳过;
    计算 `今日涨跌幅`、`量比`、`底部`,三条件全过则入选。
  - 输出列:`代码,名称,今日涨跌幅,量比,距高点折价,成交额,业绩高增`,按 `量比` 降序。
- 辅助:`compute_volume_ratio(volumes, baseline)`(今量/前baseline日均量),`is_bottom(closes, highs, window, ratio)`。

### 3.3 main.py 集成(build_board)
- 新增段:拉 `market_spot` → 粗筛 ≥ rise_threshold → 对幸存者 `fetcher.stock_daily(code, start, end)`(start = date 往前约 90 自然日,保证 ≥60 交易日;礼貌延迟)→ `scan_bottom_volume` → 写入 `DailyBoard.surge_candidates`。
- 整段独立 try/except 降级:粗筛失败 → 该段空 + 日志。
- 复用现有 yjyg 拉取得到 `high_growth_codes`(净利润行 且 增速 ≥ profit_fault.growth_threshold)。

### 3.4 daily_board.py
- `DailyBoard` 加字段 `surge_candidates: pd.DataFrame`。
- `render_console` 加【底部爆量大涨】主表(top N,带"业绩高增"标记)。
- `save_csv` 加 `surge_candidates_<date>.csv`。

---

## 4. 配置(strategy.yaml)

```yaml
bottom_volume:
  rise_threshold: 7.0       # 大涨:今日涨幅 ≥ %
  volume_ratio_threshold: 2.0  # 爆量:量比 ≥
  bottom_ratio: 0.6         # 底部:今收 ≤ 60日最高 × 此系数
  bottom_window: 60         # 底部窗口(交易日)
  volume_baseline: 5        # 量比基准(前 N 日均量)
  top_n: 50                 # 面板展示条数
```
业绩高增叠加复用现有 `profit_fault.growth_threshold`。

---

## 5. 测试策略

- `bottom_volume`(纯函数):
  - 三条件全过 → 入选;任一不过 → 排除(分别构造大涨不足 / 量比不足 / 非底部 的样本)。
  - 数据不足(K线 < 窗口)→ 跳过不报错。
  - 业绩高增叠加标记正确;按量比降序。
  - `compute_volume_ratio` / `is_bottom` 边界。
- `fetcher.market_spot`:monkeypatch 新浪 → 断言列归一化(代码去前缀、关键列齐全)。
- `build_board`:假 fetcher(market_spot + stock_daily + yjyg)→ 断言 surge_candidates 正确;粗筛无幸存者 → 空段不报错。
- 复用既有因子/面板测试。

---

## 6. 风险 / 不做

- **风险**:① 北交所无 Baostock 数据 → 跳过(日志标注);② 盘后过早运行 Baostock 当日K未更新 → 该票今日涨幅算不出按数据不足跳过(可重跑);③ `bottom_ratio=0.6` 为经验值,实测后可调。
- **不做(YAGNI)**:DuckDB 持久化(本策略每次现算,复用现有 SQLite 缓存即可)、跳空缺口(用户已确认非必要)、多因子打分。
