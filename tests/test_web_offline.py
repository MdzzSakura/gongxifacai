"""离线纪律:import 整个 web 包(app 除外,app 顶层会执行渲染)不得引入 fetcher。"""
import importlib
import sys


def test_web包不引入fetcher():
    for m in [m for m in list(sys.modules) if m.startswith("gxfc")]:
        sys.modules.pop(m)
    importlib.import_module("gxfc.web.queries")
    importlib.import_module("gxfc.web.actions")
    for page in ("review", "tracking", "journal", "ingest"):
        importlib.import_module(f"gxfc.web.pages_.{page}")
    assert "gxfc.data.fetcher" not in sys.modules
