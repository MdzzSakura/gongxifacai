from gxfc.web import actions


def test_add_argv_db在子命令前():
    argv = actions.journal_add_argv("db.duckdb", "600000", "浦发银行", "profit_fault",
                                    "断层+情绪回暖,破5日线止损", "20260707", 10.5, 1000)
    assert argv[1:3] == ["-m", "gxfc.journal"]
    assert argv.index("--db") < argv.index("add")   # argparse 主参数必须在子命令前
    assert "--plan" in argv
    assert "断层+情绪回暖,破5日线止损" in argv
    assert "1000" in argv and "10.5" in argv


def test_close_argv_守纪互斥():
    a = actions.journal_close_argv("db", "T20260707-001", "20260710", 11.2,
                                   "规则卖点", True, "")
    b = actions.journal_close_argv("db", "T20260707-001", "20260710", 11.2,
                                   "规则卖点", False, "卖飞")
    assert "--followed" in a and "--broke" not in a
    assert "--note" not in a                        # 空备注不传
    assert "--broke" in b and "--followed" not in b
    assert "--note" in b and "卖飞" in b


def test_ingest_screen_argv():
    assert actions.ingest_argv("x.duckdb")[1:3] == ["-m", "gxfc.ingest"]
    assert actions.screen_argv("x.duckdb")[1:3] == ["-m", "gxfc.screen"]
    assert "x.duckdb" in actions.ingest_argv("x.duckdb")


def test_run_action_注入runner():
    seen = {}

    class FakeProc:
        returncode = 0
        stdout = "已开仓 T20260707-001"
        stderr = ""

    def fake_runner(argv, **kwargs):
        seen["argv"] = argv
        return FakeProc()

    ok, out = actions.run_action(["python", "-m", "gxfc.journal"], runner=fake_runner)
    assert ok and "已开仓" in out
    assert seen["argv"] == ["python", "-m", "gxfc.journal"]


def test_run_action_失败返回输出():
    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "错误:交易 T1 不存在"

    ok, out = actions.run_action(["x"], runner=lambda argv, **kw: FakeProc())
    assert not ok and "不存在" in out
