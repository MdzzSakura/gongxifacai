def test_包可导入():
    import gxfc
    import gxfc.data
    import gxfc.factors
    import gxfc.review
    assert gxfc is not None


def test_配置可加载():
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path("config/strategy.yaml").read_text(encoding="utf-8"))
    assert cfg["profit_fault"]["growth_threshold"] == 50.0
    assert cfg["sector"]["top_n"] == 10
