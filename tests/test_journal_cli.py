"""journal CLI 端到端测试(临时库,零网络)。"""
from gxfc.journal import main as jmain


def test_开仓平仓统计全流程(tmp_path, capsys):
    db = str(tmp_path / "t.duckdb")
    assert jmain(["--db", db, "add", "--code", "600000", "--name", "甲",
                  "--strategy", "profit_fault", "--plan", "断层+情绪回暖,破5日线止损",
                  "--date", "20260707", "--price", "10", "--shares", "1000"]) == 0
    assert "T20260707-001" in capsys.readouterr().out
    assert jmain(["--db", db, "close", "T20260707-001", "--date", "20260710",
                  "--price", "12", "--reason", "规则卖点", "--followed"]) == 0
    capsys.readouterr()
    assert jmain(["--db", db, "stats"]) == 0
    out = capsys.readouterr().out
    assert "总盈亏" in out and "2000" in out


def test_清单空库友好提示(tmp_path, capsys):
    db = str(tmp_path / "t.duckdb")
    assert jmain(["--db", db, "list"]) == 0
    assert "无记录" in capsys.readouterr().out


def test_平仓不存在编号友好报错(tmp_path, capsys):
    db = str(tmp_path / "t.duckdb")
    assert jmain(["--db", db, "close", "T20990101-001", "--date", "20260710",
                  "--price", "12", "--reason", "规则卖点", "--followed"]) == 1
    assert "错误" in capsys.readouterr().out
