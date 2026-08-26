"""命名规则解析。样品归属错了，后面全是错的。"""
from app.storage.naming import compile_rule, parse, preview

RULES = ["{batch}_{sample}_{method}", "{batch}_{sample}", "{sample}"]


def test_three_part_name():
    m = parse("B12/B12_S1_jv.csv", RULES)
    assert (m.sample, m.batch, m.method) == ("S1", "B12", "jv")
    assert m.rule == "{batch}_{sample}_{method}"


def test_falls_through_to_shorter_rule():
    assert parse("B12_S3.csv", RULES).sample == "S3"
    assert parse("S7.txt", RULES).sample == "S7"


def test_nested_path_uses_filename_only():
    m = parse("a/b/c/B01_X9_spectrum.dat", RULES)
    assert (m.sample, m.batch, m.method) == ("X9", "B01", "spectrum")


def test_discard_placeholder():
    assert parse("junk_S9.csv", ["{*}_{sample}"]).sample == "S9"


def test_builtin_parent_rules():
    assert parse("run/B12/017.csv", ["@parent"]).sample == "B12"
    m = parse("proj/B12/jv/017.csv", ["@parent2"])
    assert (m.sample, m.method) == ("B12", "jv")


def test_no_match_is_explicit_not_a_crash():
    m = parse("whatever.csv", ["{batch}_{sample}_{method}"])
    assert m.matched is False and m.sample == ""


def test_bad_rule_is_ignored_not_fatal():
    assert compile_rule("no placeholders here") is None
    assert compile_rule("{sample") is None
    # 坏规则不该让后面的好规则失效
    assert parse("B12_S1.csv", ["{sample", "{batch}_{sample}"]).sample == "S1"


def test_preview_reports_per_file():
    rows = preview(["B12_S1_jv.csv", "garbage"], RULES)
    assert rows[0]["matched"] is True
    assert rows[1]["sample"] == "garbage"      # 最后一条 {sample} 会吃下整个名字
