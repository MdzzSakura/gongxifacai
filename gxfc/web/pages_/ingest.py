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
    # 已结束:快速结束的进程从未进过流式循环,先补读管道中剩余输出
    # (读已结束进程的 stdout 是安全的,返回缓冲区剩余行直到 EOF)
    if proc.stdout is not None:
        for line in proc.stdout:
            lines.append(line.rstrip())
    st.session_state["gxfc_proc"] = None
    if proc.returncode == 0:
        st.cache_data.clear()             # 数据已更新,面板/追踪缓存作废
        st.success(f"{name}完成")
        st.code("\n".join(lines[-20:]) or "(无输出)")
    else:
        st.error(f"{name}失败(退出码 {proc.returncode}),日志尾部:")
        st.code("\n".join(lines[-50:]) or "(无输出)")
