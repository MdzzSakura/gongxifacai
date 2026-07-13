"""📝 交易日志页:清单与纪律统计 + 网页开仓/平仓(子进程调 journal CLI)。

强制"先写计划再下单":开仓表单计划必填;平仓必选 按计划/破计划。
"""
import streamlit as st

from gxfc.web import actions, queries


def render(db_path: str) -> None:
    st.header("📝 交易日志")
    flash = st.session_state.pop("journal_flash", "")
    if flash:
        st.success(flash)

    stats = queries.trade_stats_report(db_path)
    st.subheader("纪律统计(全部/按计划/未按计划)")
    if stats.empty:
        st.info("无已平仓交易,统计从首笔平仓后开始")
    else:
        st.dataframe(stats, hide_index=True, use_container_width=True)

    open_trades = queries.list_trades(db_path, open_only=True)
    all_trades = queries.list_trades(db_path)
    tab_open, tab_all = st.tabs(["持仓中", "全部记录"])
    with tab_open:
        if open_trades.empty:
            st.caption("(无持仓)")
        else:
            st.dataframe(open_trades, hide_index=True, use_container_width=True)
    with tab_all:
        if all_trades.empty:
            st.caption("(无记录)")
        else:
            st.dataframe(all_trades, hide_index=True, use_container_width=True)

    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        _add_form(db_path)
    with c2:
        _close_form(db_path, open_trades)


def _submit(argv: list) -> None:
    """执行写命令:成功则闪存消息+清缓存+重跑;失败就地报错。"""
    ok, out = actions.run_action(argv)
    if ok:
        st.cache_data.clear()
        st.session_state["journal_flash"] = out
        st.rerun()
    else:
        st.error(out[-1000:] or "命令执行失败")


def _add_form(db_path: str) -> None:
    st.subheader("开仓(先写计划再下单)")
    strategies = queries.signal_strategies(db_path) + ["手动"]
    with st.form("add_trade", clear_on_submit=False):
        code = st.text_input("股票代码")
        name = st.text_input("股票名称")
        strategy = st.selectbox("策略", strategies)
        date = st.date_input("开仓日")
        price = st.number_input("开仓价", min_value=0.01, step=0.01)
        shares = st.number_input("股数", min_value=100, step=100)
        plan = st.text_area("计划(买入理由+卖出规则,必填)")
        if st.form_submit_button("记录开仓", type="primary"):
            if not code.strip() or not plan.strip():
                st.error("股票代码与计划为必填项")
            else:
                _submit(actions.journal_add_argv(
                    db_path, code.strip(), name.strip(), strategy, plan.strip(),
                    date.strftime("%Y%m%d"), price, int(shares)))


def _close_form(db_path: str, open_trades) -> None:
    st.subheader("平仓(申报是否按计划)")
    if open_trades.empty:
        st.caption("(无持仓可平)")
        return
    options = {f'{r["trade_id"]} {r["代码"]} {r["名称"]}': r["trade_id"]
               for _, r in open_trades.iterrows()}
    with st.form("close_trade"):
        label = st.selectbox("交易", list(options))
        date = st.date_input("平仓日")
        price = st.number_input("平仓价", min_value=0.01, step=0.01)
        reason = st.selectbox("离场原因", ["规则卖点", "止损", "情绪", "其他"])
        followed = st.radio("执行情况", ["按计划", "破计划"], horizontal=True)
        note = st.text_input("备注(卖飞/拿住等复盘线索)")
        if st.form_submit_button("记录平仓", type="primary"):
            _submit(actions.journal_close_argv(
                db_path, options[label], date.strftime("%Y%m%d"), price,
                reason, followed == "按计划", note.strip()))
