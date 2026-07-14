"""📊 复盘面板页：复刻 python -m gxfc.screen 的面板，可切换日期。"""
import streamlit as st

from gxfc.web import queries

_load_board = st.cache_data(ttl=600, show_spinner="组装面板…")(queries.load_board)


def render(db_path: str) -> None:
    st.header("📊 每日复盘面板")
    dates = queries.trading_dates(db_path)
    if not dates:
        st.info("库内无日K数据，请先到「⚙️ 数据采集」页运行采集")
        return
    date = st.selectbox("交易日", dates)
    board = _load_board(db_path, date)

    e = board.emotion
    st.subheader("市场情绪温度计")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("涨停家数", e.limit_up)
    c2.metric("跌停家数", e.limit_down)
    c3.metric("炸板率", f"{e.broken_board_rate:.1%}")
    c4.metric("最高板", f"{e.highest_streak} 板")
    up = "-" if e.up_count is None else e.up_count
    down = "-" if e.down_count is None else e.down_count
    st.caption(f"上涨/下跌家数：{up} / {down} · 量能状态：{e.volume_state}")
    st.info(e.sentiment_hint)

    st.subheader("板块涨幅榜")
    if board.sectors.empty:
        st.warning(f"{date} 板块数据未采集，该段降级为空")
    else:
        st.dataframe(board.sectors, hide_index=True, use_container_width=True)
        for name, cons in board.sector_cores.items():
            with st.expander(f"核心成分股：{name}"):
                st.dataframe(cons, hide_index=True, use_container_width=True)

    st.subheader("净利润断层候选")
    st.caption("proxy 口径：预告净利润同比增速，非券商一致预期")
    if len(board.candidates) > 0:
        st.dataframe(board.candidates, hide_index=True, use_container_width=True)
    else:
        st.warning("当日无达标候选（或业绩预告未采集）")

    st.subheader("底部爆量大涨")
    st.caption("全市场：大涨+爆量+底部低位；业绩高增叠加标记")
    surge = board.surge_candidates
    if surge is not None and len(surge) > 0:
        st.dataframe(surge, hide_index=True, use_container_width=True)
    else:
        st.warning("当日无达标候选（或日K数据不足）")
