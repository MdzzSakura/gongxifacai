"""⚙️ 数据采集页:库状态总览 + 一键采集/重跑筛选(子进程+实时日志)。

DuckDB 单写者:同一时刻只允许一个采集/筛选子进程,运行期间按钮置灰。
进程句柄与日志存 session_state,页面被交互打断后可续读输出。

C1 修复(2026-07-13 终审):子进程持有写锁期间,只读连接一旦撞锁会**立即**
抛 duckdb.IOException(而非阻塞等待)。旧实现在 render 开头无条件查询
db_overview,一撞锁就被 app.py 全局 except 捕获、进度区渲染不到,子进程
stdout 管道无人排空,写满系统管道缓冲区后子进程本身也会阻塞在 write 上,
采集永久停摆且没有恢复路径。现改为两点:
  ① render 先判断 gxfc_proc 是否存活,存活则跳过 db_overview,只读连接
     压根不会去碰写锁,从根上避免撞锁。
  ② _launch 启动子进程的同时派生一个后台守护线程持续排空 stdout,与渲染
     线程解耦——用户停留在其他页面、或来回切换交互,都不影响排空,顺带
     修掉 I1(旧实现排空只在采集页渲染路径内发生)。
"""
import threading
import time
from pathlib import Path

import streamlit as st

from gxfc.web import actions, queries

# 长采集可能产生数十万行日志,session_state 常驻列表需要设上限,防止内存无限增长。
_LOG_LIMIT = 5000   # 超过此行数触发裁剪
_LOG_KEEP = 4000    # 裁剪后保留的尾部行数


def render(db_path: str) -> None:
    st.header("⚙️ 数据采集")
    proc = st.session_state.get("gxfc_proc")
    running = proc is not None and proc.poll() is None
    if running:
        # 子进程独占写锁期间,只读连接一撞就抛 IOException——此处必须跳过查询,
        # 而不是捕获异常后降级展示(异常会绕开下方的进度渲染,详见模块 docstring)。
        st.caption("采集/筛选进行中,库状态暂不可查")
    else:
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
    c1, c2 = st.columns(2)
    if c1.button("开始采集(联网)", disabled=running, type="primary"):
        _launch(actions.ingest_argv(db_path), "采集")
    if c2.button("重跑筛选(离线)", disabled=running or not Path(db_path).exists()):
        _launch(actions.screen_argv(db_path), "筛选")
    _render_progress()


def _drain(proc, lines: list) -> None:
    """后台守护线程体:持续消费子进程 stdout 直至 EOF(进程结束自然退出循环)。

    必须独立于渲染线程常驻运行——渲染只在用户触发 rerun 时才执行一次,若排空
    只发生在渲染路径内,用户停留在其他页面期间子进程日志写满系统管道缓冲区就
    会阻塞在 write() 上(I1)。list.append 在 CPython 下由 GIL 保护是原子操作,
    渲染层只读该列表,天然线程安全,无需额外加锁。
    """
    for line in proc.stdout:
        lines.append(line.rstrip())
        if len(lines) > _LOG_LIMIT:
            del lines[:len(lines) - _LOG_KEEP]


def _launch(argv: list, name: str) -> None:
    proc = actions.start_stream(argv)
    lines: list = []
    st.session_state["gxfc_proc"] = proc
    st.session_state["gxfc_proc_name"] = name
    st.session_state["gxfc_proc_log"] = lines
    thread = threading.Thread(target=_drain, args=(proc, lines), daemon=True)
    thread.start()
    st.session_state["gxfc_proc_thread"] = thread
    st.rerun()


def _render_progress() -> None:
    proc = st.session_state.get("gxfc_proc")
    if proc is None:
        return
    name = st.session_state.get("gxfc_proc_name", "任务")
    lines = st.session_state.setdefault("gxfc_proc_log", [])
    if proc.poll() is None:
        # 运行中:只展示后台线程已排空到的尾部日志,渲染线程本身绝不读管道,
        # 靠轮询(sleep + rerun)刷新画面。
        # 特意不用 `with st.status(...)`——with 退出时会自动把状态置为
        # "complete"(即便此刻仍在运行,只是这一轮 tick 结束),每次轮询都会
        # 误现"完成"图标；直接对返回对象调方法写内容,不进入 with 块，
        # 状态就会一直停在构造时的 "running",不会被隐式改写。
        status = st.status(f"{name}进行中…", expanded=True, state="running")
        status.code("\n".join(lines[-40:]) or "(暂无输出)")
        time.sleep(2)
        st.rerun()
        return
    # 已结束:先等后台排空线程把管道尾巴读完,再切换状态,避免丢最后几行。
    # join 设超时防止极端情况下线程卡死拖垮页面(正常情况下进程已退出,
    # stdout 已到 EOF,线程会立即结束,join 几乎不等待)。
    thread = st.session_state.get("gxfc_proc_thread")
    if thread is not None:
        thread.join(timeout=5)
    proc.wait()
    st.session_state["gxfc_proc"] = None
    st.session_state["gxfc_proc_thread"] = None
    if proc.returncode == 0:
        st.cache_data.clear()             # 数据已更新,面板/追踪缓存作废
        st.success(f"{name}完成")
        st.code("\n".join(lines[-20:]) or "(无输出)")
    else:
        st.error(f"{name}失败(退出码 {proc.returncode}),日志尾部:")
        st.code("\n".join(lines[-50:]) or "(无输出)")
