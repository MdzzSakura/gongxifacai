# 本地数据落地:采集/筛选解耦(DuckDB)— 架构设计

生成时间:2026-06-30
状态:已通过设计评审,待落地
背景:阶段1 联网冒烟暴露东财对高频请求限流;需把"数据采集"与"选股筛选"解耦——采集慢速落本地,筛选纯离线高频跑,避免 IP 被封。

---

## 1. 目标

把数据获取与选股彻底分成两个阶段:

- **采集(ingest)**:联网,慢速、礼貌、可断点续传,把全市场数据趸到本地 DuckDB。一天跑 1-2 次。
- **筛选(screen)**:纯离线,只读 DuckDB,可任意高频运行,永不触发限流。

核心:**联网只发生在采集**,筛选零网络。

---

## 2. 关键决策(已确认)

| 决策 | 选择 | 理由 |
|------|------|------|
| 本地存储 | **DuckDB 单文件** | 嵌入式零运维、列式 OLAP(适合算因子/join/窗口)、与 pandas 无缝、原生兼容 Parquet(后期冷热分离零成本)。现在不上 Parquet 文件湖(YAGNI) |
| 筛选模式 | **严格离线** | 只读本地,缺数据则标注"请先采集",绝不联网 |
| 日K 范围 | **全市场 ~5000 只** | 夜间趸着采、断点续传;支撑阶段2(板块动量、情绪周期需广覆盖) |

---

## 3. 数据流

```
【采集 ingest】(联网,慢,断点续传)
  AKShare(Fetcher,已带节流+指数退避)
      → 写 DuckDB:快照表(按交易日累积) + 日K表(全市场,增量 append)
      → 已采过(该日该票)直接跳过;被限流中断 → 重跑接着采

【筛选 screen】(纯离线,只读 DuckDB)
      读 DuckDB → 复用现有因子(情绪/板块/断层) → 出面板 + CSV
      本地缺某段 → 标"该段无数据,请先采集",不联网
```

---

## 4. DuckDB 库结构(gxfc_data.duckdb)

```
zt_pool       (trade_date, 代码, 名称, 涨跌幅, 连板数, 炸板次数, 所属行业, ...)   每日快照累积
dt_pool       (trade_date, 代码, 名称, 涨跌幅, 连续跌停, ...)
zb_pool       (trade_date, 代码, 名称, 涨跌幅, 炸板次数, ...)
industry_board(trade_date, 板块名称, 涨跌幅, 领涨股票, ...)
industry_cons (trade_date, 板块名称, 名称, 涨跌幅, 成交额, ...)
yjyg          (quarter_end, 股票代码, 股票简称, 预测指标, 业绩变动幅度, ...)
daily         (代码, 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, ...)  全市场日K
```

- 快照表按 `trade_date`(或 yjyg 的 `quarter_end`)区分;每次采集 **upsert 当期那批**(先删该期旧行再插)。
- `daily`:增量 **append**,`(代码, 日期)` 唯一(insert 前去重),采过的不再拉。
- 每张表加索引/主键约束以支持去重与按期读取。

---

## 5. 组件划分

```
gxfc/
├── store/
│   └── duck_store.py     # 【新】DuckDB 读写封装
├── ingest.py             # 【新】联网采集:Fetcher → DuckStore
├── screen.py             # 【新】离线筛选:DuckStore → 因子 → 面板
├── data/fetcher.py       # 复用(已带节流/退避),仅 ingest 调用
├── factors/*             # 完全复用(纯函数,不改)
└── review/daily_board.py # 复用(组装/渲染/CSV)
```

### 5.1 DuckStore (store/duck_store.py)
- 职责:DuckDB 建表 + 读写,对上层屏蔽 SQL。
- 写接口:
  - `upsert_snapshot(table: str, period_col: str, period: str, df) -> None` — 删 `period` 旧行再插(快照/业绩预告)
  - `append_daily(df) -> int` — 插入日K,`(代码,日期)` 去重,返回新增行数
- 读接口(供 screen,date 感知):
  - `read_snapshot(table, period_col, period) -> DataFrame`(无则空表)
  - `read_daily(code, start, end) -> DataFrame`
- 续传查询:
  - `has_daily(code, upto_date) -> bool`(该票已采到 ≥ 截止日则跳过)
  - `snapshot_dates(table) -> list`(已采哪些日)

### 5.2 ingest.py(联网采集,断点续传)
- `ingest_snapshots(fetcher, store, date, quarter_end)`:拉涨停/跌停/炸板池、板块榜、板块榜 top 板块成分股、业绩预告 → upsert 入库。
- `ingest_daily(fetcher, store, date, codes=None)`:取全市场清单(`ak.stock_info_a_code_name()`),逐票拉日K窗口 append 入库;`has_daily` 命中即跳过(续传);每票礼貌延迟。
- `run_ingest(date, quarter_end)`:真实入口,构建 Fetcher + DuckStore,跑 snapshots + daily,进度 logging。
- CLI:`python -m gxfc.ingest <交易日> <季度末>`。**长耗时、可重跑续传**。

### 5.3 screen.py(离线筛选)
- `build_board_offline(store, date, quarter_end, config) -> DailyBoard`:从 DuckStore 读各数据集 → 复用 `compute_market_emotion`/`rank_sectors`/`core_stocks`/`scan_profit_fault` → 组装 `DailyBoard`。某段无数据则降级标注"请先采集"。
- `run_screen(date, quarter_end, out_dir="out")`:读 config + DuckStore → build_board_offline → 打印面板 + 存 CSV。**纯离线、零网络**。
- CLI:`python -m gxfc.screen <交易日> <季度末>`。

### 5.4 退休 SQLite DataFrameCache
- 阶段1 的 `gxfc/data/cache.py`(SQLite KV)统一退休,数据落地全走 DuckStore,避免两套存储。`Fetcher` 不再强制依赖 cache(ingest 直接写 DuckStore;Fetcher 仅作联网取数,缓存职责移交 DuckStore 的"已采则跳过")。

---

## 6. 错误处理 / 健壮性

- **采集**:逐项 try/except,单票/单接口失败只 `warning` + 跳过整体不中断;**断点续传**——`has_daily`/`snapshot_dates` 命中即跳,限流中断后重跑接着采;Fetcher 的节流(请求间隔)+ 指数退避已就位。
- **筛选**:严格离线;数据集缺失 → 复用现有"分段降级",面板标注该段"无数据,请先运行采集"。

---

## 7. 测试策略

- `duck_store`:临时/内存 DuckDB 测 upsert(覆盖同期)、append_daily 去重、read_*、has_daily 续传判断。
- `ingest`:注入 FakeFetcher → 临时 store,断言数据落库;第二次跑断言已采的被跳过(续传)。
- `screen`:预置 store 数据(含跳空样本)→ 断言面板组装正确、严格不联网(FakeStore/临时 DuckDB,无网络调用)。
- 复用阶段1 的 40 个因子/面板测试(因子纯函数不变)。

---

## 8. 落地范围(本次)

一次落地完整的"采集→DuckDB→离线筛选"闭环:
1. DuckStore(建表 + 读写 + 续传查询)
2. ingest(快照 + 全市场日K,断点续传,CLI)
3. screen(离线组装面板,CLI)
4. 退休 SQLite cache,接口收敛到 DuckStore

验证标准:`python -m gxfc.ingest <date> <quarter_end>` 能把数据落进 DuckDB(中断可续传);`python -m gxfc.screen <date> <quarter_end>` 纯离线读库出完整面板(情绪/板块/断层候选),全程不联网。

---

## 9. 关键风险

- **全市场日K 首采耗时长**:~5000 票 × 礼貌延迟,可能数十分钟到小时级;靠断点续传分次完成,进度 logging 可见。
- **DuckDB 依赖**:新增 `duckdb` 包;嵌入式无服务器,风险低。
- **接口名/列名**:沿用阶段1 已核对的真实列(涨跌停池、板块榜 `领涨股票`、业绩预告 `业绩变动幅度`+`预测指标`),建表列以真实列为准。
- **trade_date 来源**:快照接口本身不带日期列,落库时由采集参数 `date` 注入为 `trade_date` 列。
