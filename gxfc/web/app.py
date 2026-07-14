"""GXFC Web 控制台入口：侧边栏导航 + 库缺失引导页。

启动：python -m streamlit run gxfc/web/app.py
库路径可用环境变量 GXFC_DB 覆盖（默认 gxfc_data.duckdb）。
本包禁止 import gxfc.data.fetcher——触网只发生在采集子进程内。
"""
import os
import sys
from pathlib import Path

# streamlit run 以脚本方式执行本文件，repo 根不在 sys.path，须手动补上
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
        st.info(f"本地库 {DB_PATH} 不存在，请先到「⚙️ 数据采集」页运行一次采集，"
                "或在命令行执行 python -m gxfc.ingest")
        return
    try:
        _PAGES[choice](DB_PATH)
    except duckdb.IOException:
        st.warning("数据库正被采集/筛选进程独占，请等待其完成后刷新页面")


main()
