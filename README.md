# gongxifacai（恭喜发财）

一个面向个人投资者的 **A 股盘后复盘与选股工具**：白天收盘后一键采集数据到本地 DuckDB，随后所有筛选、复盘、追踪、记录全部离线完成，可任意重跑、调参，永不触发数据源限流。

## 核心理念

- **采集与筛选严格分离**：全项目只有 `gxfc.ingest` 一个入口联网，其余模块通过依赖关系（禁止 import fetcher）保证零网络请求。
- **证据驱动**：筛出来的票后来到底怎么走，用信号追踪报表给出胜率/盈亏比，作为调参和取舍策略的唯一依据。
- **纪律优先**：交易日志强制"先写计划再下单"，平仓时必须申报是否按计划执行，用统计暴露纪律成本。

## 四个 CLI 入口

| 命令 | 作用 | 是否联网 |
|------|------|----------|
| `python -m gxfc.ingest [YYYYMMDD]` | 增量采集当日全部所需数据落入 DuckDB（日历、涨跌停池、板块、业绩预告、全市场日K、逐股历史回补） | ✅ 唯一联网入口 |
| `python -m gxfc.screen [YYYYMMDD]` | 组装每日复盘面板：市场情绪、板块涨幅榜、策略候选，输出控制台 + CSV，并把候选写入信号表 | ❌ 只读本地库 |
| `python -m gxfc.track` | 信号前向收益追踪：按 策略 × 持有期（T+1/3/5/10）统计胜率、平均收益、盈亏比 | ❌ 只读本地库 |
| `python -m gxfc.journal add/close/list/stats` | 交易日志：开仓写计划、平仓记执行，输出"计划-执行-纪律"三组对比统计 | ❌ 只写本地库 |

## Web 控制台（本地网页）

```powershell
python -m streamlit run gxfc/web/app.py
```

浏览器打开 http://localhost:8501，四个页面：📊 复盘面板（切换日期）、📈 信号追踪（胜率/盈亏比图表）、📝 交易日志（网页开平仓）、⚙️ 数据采集（一键采集+实时日志）。库路径可用环境变量 `GXFC_DB` 覆盖。网页读库走只读短连接、写库走子进程调 CLI，网页开着不影响命令行操作。

## 内置策略（因子）

- **市场情绪**（`factors/market_emotion.py`）：由涨停池/跌停池/炸板池计算炸板率、最高板等指标，盘后一眼看清情绪冷热。
- **板块主线**（`factors/sector.py`）：板块涨幅榜定主线，下钻强势板块的核心成分股。
- **净利润断层**（`factors/profit_fault.py`）：业绩超预期（预告净利润同比增速 ≥ 阈值作 proxy）+ 次日跳空缺口。
- **底部爆量大涨**（`factors/bottom_volume.py`）：股价长期低迷后突放巨量 + 大涨（≥7%），捕捉趋势启动信号。

阈值均集中在 `config/strategy.yaml`，改配置即可调参。

## 项目结构

```
gongxifacai/
├── gxfc/
│   ├── ingest.py            # 联网采集编排（唯一触网入口，断点续传 + 除权自愈）
│   ├── screen.py            # 离线复盘面板（严格离线）
│   ├── track.py             # 信号追踪报表 CLI
│   ├── journal.py           # 交易日志 CLI
│   ├── dates.py             # 日期工具
│   ├── web/
│   │   ├── app.py           # Streamlit 应用入口
│   │   ├── queries.py       # 库读取层（只读短连接）
│   │   ├── actions.py       # 子进程动作层（调用 CLI）
│   │   └── pages_/          # 四个页面组件
│   │       ├── 1_dashboard.py      # 📊 复盘面板
│   │       ├── 2_tracking.py       # 📈 信号追踪
│   │       ├── 3_journal.py        # 📝 交易日志
│   │       └── 4_ingest.py         # ⚙️ 数据采集
│   ├── data/
│   │   ├── fetcher.py       # 多源数据抓取（东财/baostock/新浪，自动回退 + 重试）
│   │   └── quality.py       # 数据质量校验
│   ├── factors/             # 四个策略因子（见上）
│   ├── review/
│   │   ├── daily_board.py   # 复盘面板组装与渲染
│   │   └── tracker.py       # 前向收益计算（基于前复权序列现算，不存快照价）
│   └── store/
│       ├── duck_store.py    # DuckDB 行情库（快照表 upsert、日K 增量、采集台账）
│       └── journal_store.py # 信号表 + 交易日志表（与行情库同文件共用连接）
├── config/strategy.yaml     # 全部策略阈值与追踪持有期配置
├── tests/                   # pytest 单测（网络测试打 network 标记，默认可跳过）
├── docs/superpowers/        # 设计文档与实施计划
└── gxfc_data.duckdb         # 本地数据库（采集后生成）
```

## 快速开始

```powershell
# 1. 安装依赖
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. 盘后采集当日数据（首次会回补历史，耗时较长，中断重跑自动续传）
python -m gxfc.ingest

# 3. 生成复盘面板（同时把候选写入信号表）
python -m gxfc.screen

# 4. 过几天后看信号表现
python -m gxfc.track

# 5. 记一笔交易
python -m gxfc.journal add --code 600000 --name 浦发银行 --strategy profit_fault `
    --plan "断层+情绪回暖,破5日线止损" --date 20260707 --price 10.5 --shares 1000
python -m gxfc.journal close T20260707-001 --date 20260710 --price 11.2 `
    --reason 规则卖点 --followed
python -m gxfc.journal stats
```

## 运行测试

```powershell
pytest                 # 默认跳过需要联网的测试
pytest -m network      # 仅跑真实访问 AKShare 的网络测试
```

## 技术栈

Python 3 · DuckDB（本地存储）· AKShare / baostock（数据源）· pandas · PyYAML · tabulate · pytest · Streamlit（本地网页）· plotly（图表）

## 设计文档

详细设计与实施记录见 `docs/superpowers/`：

- `specs/2026-06-29-a-share-stock-screening-design.md` — 选股系统总体设计
- `specs/2026-06-30-local-data-store-ingest-screen-design.md` — 本地存储与采集/筛选分离
- `specs/2026-07-07-stable-data-ingest-design.md` — 稳定采集（限流对策、断点续传、除权自愈）
- `plans/2026-07-07-signal-tracking-and-journal.md` — 信号追踪与交易日志
- `specs/2026-07-13-web-console-design.md` — Web 控制台（Streamlit 本地网页）
