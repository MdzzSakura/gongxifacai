"""📈 信号追踪页：策略×持有期 胜率/盈亏比汇总图表 + 信号明细。"""
import plotly.express as px
import streamlit as st

from gxfc.web import queries

_report = st.cache_data(ttl=600, show_spinner="回算信号收益…")(queries.tracking_report)


def render(db_path: str) -> None:
    st.header("📈 信号追踪")
    strategies = queries.signal_strategies(db_path)
    if not strategies:
        st.info("尚无信号记录，先运行 python -m gxfc.screen（或到采集页「重跑筛选」）产生信号")
        return
    strategy = st.selectbox("策略", ["(全部)"] + strategies)
    perf, summary = _report(db_path, None if strategy == "(全部)" else strategy)
    if perf.empty:
        st.info("该条件下无信号")
        return

    untrackable = int((~perf["可追踪"]).sum()) if "可追踪" in perf.columns else 0
    if untrackable:
        st.caption(f"另有 {untrackable} 条信号因信号日无日K（停牌/未采集）不可追踪，未计入统计")

    st.subheader("策略 × 持有期汇总")
    if summary.empty:
        st.info("信号尚无可评估的持有期收益（未来交易日数据不足）")
    else:
        st.dataframe(summary, hide_index=True, use_container_width=True)
        c1, c2 = st.columns(2)
        with c1:
            st.plotly_chart(
                px.bar(summary, x="持有期", y="胜率%", color="策略",
                       barmode="group", title="胜率"),
                use_container_width=True)
        with c2:
            st.plotly_chart(
                px.bar(summary, x="持有期", y="平均收益%", color="策略",
                       barmode="group", title="平均收益"),
                use_container_width=True)

    st.subheader("信号明细")
    st.dataframe(perf, hide_index=True, use_container_width=True)
