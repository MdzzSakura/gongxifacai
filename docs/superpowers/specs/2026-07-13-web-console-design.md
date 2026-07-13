# GXFC Web 控制台 — 本地网页可视化设计

生成时间：2026-07-13
状态：待评审
前置设计：[2026-06-30 本地数据落地：采集/筛选解耦（DuckDB）](2026-06-30-local-data-store-ingest-screen-design.md)、[2026-07-07 信号追踪与交易日志](../plans/2026-07-07-signal-tracking-and-journal.md) — 本篇不改动任何既有链路，只在其上加一层 Streamlit 展示与操作入口。

---

## 1. 目标与非目标

**目标**

1. 一条命令启动本地网页（`python -m streamlit run gxfc/web/app.py`），覆盖四个场景：每日复盘面板、信号追踪图表、交易日志（含网页记账）、数据采集触发。
2. 网页开着时不影响 CLI 正常读写数据库——不与采集/筛选/记账进程争抢 DuckDB 文件锁。
3. 全部写操作复用既有 CLI 逻辑，不复制第二份校验/落库代码。
4. 延续"严格离线"纪律：`gxfc/web/` 禁止 import `gxfc.data.fetcher`，触网只发生在采集子进程内。

**非目标**

- 个股K线图（用户明确暂不需要，daily 表数据具备，留作后续迭代）。
- 多用户/鉴权/公网部署——只服务于本机单人使用。
- 盘中实时刷新——数据粒度是"盘后采集一次"，网页无自动轮询。

---

## 2. 总体架构

```
                 ┌────────────────────────────────────────────┐
                 │ Streamlit 进程（gxfc/web/app.py 入口+导航）   │
                 │  📊 复盘面板 📈 信号追踪 📝 交易日志 ⚙️ 采集   │
                 └──────────┬─────────────────────┬───────────┘
                            │ 读                   │ 写
                 ┌──────────▼──────────┐ ┌────────▼──────────────┐
                 │ queries.py（读取层）  │ │ actions.py（动作层）    │
                 │  短连接 read_only     │ │  subprocess 调既有 CLI  │
                 │  即用即关+st.cache    │ │  journal/ingest/screen │
                 └──────────┬──────────┘ └────────┬──────────────┘
                            │ 只读(瞬时)            │ 子进程独占写锁(瞬时)
                 ┌──────────▼─────────────────────▼───────────┐
                 │            gxfc_data.duckdb                 │
                 └─────────────────────────────────────────────┘
```

**三条铁律**

1. **读走短连接**：DuckDB 同一文件只允许"一个写者，或多个只读者"。web 进程若常驻持有连接，采集子进程将拿不到写锁。因此 `queries.py` 每次查询 `duckdb.connect(read_only=True)` → 查询 → 立即 close（with 语义），查询结果用 `st.cache_data` 缓存降低重连频率。
2. **写走子进程**：记交易、触发采集/筛选一律 `subprocess` 调用 `python -m gxfc.journal / gxfc.ingest / gxfc.screen`。收益：复用全部既有参数校验、台账、幂等逻辑；web 进程自身永不打开写连接；子进程结束即释放锁。
3. **离线纪律**：`gxfc/web/` 任何模块禁止 import `gxfc.data.fetcher`（与 screen.py 同款，靠依赖关系而非运行时开关保证）。采集页的"触网"发生在 ingest 子进程里，web 进程本身零网络请求。

**文件布局**

```
gxfc/web/
├── __init__.py
├── app.py           # 入口：侧边栏导航、库文件不存在时的引导页
├── queries.py       # 唯一读取层：全部 SQL / DataFrame 查询（短连接封装）
├── actions.py       # 唯一动作层：subprocess 命令拼装与执行封装
└── pages_/
    ├── __init__.py
    ├── review.py    # 📊 复盘面板
    ├── tracking.py  # 📈 信号追踪
    ├── journal.py   # 📝 交易日志
    └── ingest.py    # ⚙️ 数据采集
```

页面模块只做渲染与交互编排，不直接写 SQL、不直接起子进程（关注点分离，读写各一个可单测的纯逻辑层）。

> 目录名用 `pages_/` 而非 `pages/`：Streamlit 对名为 `pages/` 的目录有自动多页路由魔法，会绕过 app.py 的导航逻辑，刻意避开。导航用 `st.sidebar.radio` 手工实现，行为完全可控。

---

## 3. 页面设计

### 3.1 📊 复盘面板

复刻 `python -m gxfc.screen` 的面板到网页，可切换日期。

- **日期选择**：`st.selectbox` 列出库内有数据的交易日（倒序），默认最新。
- **情绪区**：`st.metric` 卡片行——涨停数、跌停数、炸板率、最高板；情绪结论一句话（复用 `factors/market_emotion.py` 的判定）。
- **板块区**：板块涨幅榜表格（前 N，配置同 strategy.yaml）；点选强势板块下钻其核心成分股表。
- **候选区**：净利润断层候选表、底部爆量候选表，列结构与 `out/*.csv` 一致。
- **降级行为**：某数据集当日无数据时该区显示 `st.warning`（"该日期未采集 XX 数据"）并跳过，不阻塞整页——与 screen.py 的降级语义一致。
- **计算口径**：直接函数复用 `factors/` 与 `review/daily_board.py` 的现有逻辑（只读、零网络），不重写因子计算。

### 3.2 📈 信号追踪

把 `python -m gxfc.track` 的报表可视化。

- **汇总区**：策略 × 持有期（T+1/3/5/10，读 strategy.yaml）的胜率/平均收益/盈亏比汇总表 + 两张 plotly 柱状图（胜率、平均收益，按策略分组）。
- **明细区**：信号明细表，支持按策略、日期区间过滤；"不可追踪"信号单列计数说明。
- **计算口径**：直接复用 `review/tracker.py` 的 `track_signals` / `summarize`（只读，无需子进程）。
- **空态**：`signals` 表不存在或为空（当前库即此状态）→ `st.info` 提示"先运行 python -m gxfc.screen 产生信号"。

### 3.3 📝 交易日志

查看 + 网页记账，等价替代 `python -m gxfc.journal` 四个子命令。

- **清单区**：持仓中 / 已平仓两个表（对应 `journal list --open` / `list`）。
- **统计区**：计划-执行-纪律三组统计（复用 `review/tracker.py` 的 `trade_stats`）。
- **开仓表单**（`st.form`）：代码、名称、策略（下拉：库内既有策略名+自由输入）、日期、价格、股数、**计划（必填多行文本）**——呼应"先写计划再下单"，前端即校验非空。
- **平仓表单**（`st.form`）：交易编号（下拉列出持仓中编号）、日期、价格、卖出理由、**按计划/破计划（必选单选）**、备注。
- **提交路径**：表单 → `actions.py` 拼装 `python -m gxfc.journal add/close ...` → 子进程执行 → 成功则回显新交易编号并失效缓存刷新清单；失败则展示 CLI 的友好报错原文。

### 3.4 ⚙️ 数据采集

- **库状态总览**：各表行数、日K最新日期、`ingest_log` 最近若干条台账（成功/失败/数据源）。
- **"开始采集"按钮**：`subprocess.Popen` 启动 `python -m gxfc.ingest`，`st.status` 容器内实时滚动子进程 stdout/stderr；进程句柄存 `st.session_state`，运行期间按钮置灰防止重复触发（DuckDB 单写者，双采集必然锁冲突）。
- **"重跑筛选"按钮**：同机制调 `python -m gxfc.screen`，用于采集完成后立即产出面板与信号。
- **结束态**：退出码 0 → `st.success` + 失效全部查询缓存；非 0 → `st.error` 展示日志尾部 50 行。

---

## 4. 数据流与错误处理

**读路径**：页面 → `queries.py`（短连接只读 SQL / 复用 factors、tracker 纯函数）→ DuckDB。
**写路径**：页面表单/按钮 → `actions.py`（命令拼装 + subprocess）→ CLI 模块 → DuckDB。

错误处理三档，全部就地展示、不抛裸异常：

| 场景 | 行为 |
|------|------|
| `gxfc_data.duckdb` 文件不存在 | app.py 渲染引导页：说明先运行 `python -m gxfc.ingest` |
| 某表不存在 / 查询无行（signals、trades 未建等） | 对应区块 `st.info` 提示产生该数据的命令，其余区块正常 |
| 子进程非零退出 | `st.error` 展示命令行与输出尾部；页面数据保持旧值 |
| 读连接被写者短暂阻塞 | 捕获 duckdb.IOException，`st.warning` 提示"采集进行中，稍后刷新" |

---

## 5. 测试策略

沿用 pytest，全部零网络、默认可跑：

1. **queries.py 单测**：内存/临时 DuckDB 造最小表数据，断言各查询函数返回的 DataFrame 结构与降级行为（表不存在返回空态标记而非抛异常）。
2. **actions.py 单测**：只测命令拼装（参数 → argv 列表），subprocess 执行用注入的 runner 打桩，不真起进程。
3. **页面冒烟测试**：`streamlit.testing.v1.AppTest` 驱动四个页面在"空库"与"造数库"两种夹具下渲染不抛异常、空态提示存在。
4. **离线纪律测试**：断言 `gxfc.web` 全包 import 后 `sys.modules` 中不存在 `gxfc.data.fetcher`（与既有 screen 的纪律测试同款写法）。

---

## 6. 依赖与启动

- requirements.txt 新增：`streamlit>=1.35`、`plotly>=5.20`。
- 启动：`python -m streamlit run gxfc/web/app.py`（默认 http://localhost:8501）。
- 不新增任何配置文件；页面所需阈值/持有期一律读现有 `config/strategy.yaml`。
