"""筛选式：编译、分面、选择即示例、样品集。

核心不变量：**一次选择是一个筛选式，不是一串 ID**。
"""
import pytest

from app.storage import db, naming, selection, sets


@pytest.fixture()
def populated(workspace):
    """3 个批次 × 若干样品，带方法、文件夹、关键结果。"""
    with db.tx() as c:
        bid = db.new_id("bat")
        c.execute("INSERT INTO import_batch(batch_id, source_hint, file_count, created_at)"
                  " VALUES(?,?,?,?)", (bid, "测试导入", 0, db.now()))
        n = 0
        for batch, count, folder in (("B20", 12, "2026-06"), ("B21", 8, "2026-07"),
                                     ("B22", 5, "2026-06")):
            for i in range(1, count + 1):
                sid = db.new_id("smp")
                c.execute("INSERT INTO sample(sample_id,name,batch,created_at)"
                          " VALUES(?,?,?,?)", (sid, f"{batch}_S{i}", batch, db.now()))
                c.execute(
                    "INSERT INTO artifact(artifact_id,kind,storage_mode,sha256,"
                    " display_path,filename,ext,size,status,is_matrix,sample_id,"
                    " batch_id,created_at)"
                    " VALUES(?,'raw','copied',?,?,?,'.csv',100,'ok',1,?,?,?)",
                    (db.new_id("art"), f"{n:064x}",
                     f"{folder}/{batch}/{batch}_S{i}.csv", f"{batch}_S{i}.csv",
                     sid, bid, db.now()))
                c.execute("INSERT INTO measurement(measurement_id,sample_id,method,"
                          " created_at) VALUES(?,?,?,?)",
                          (db.new_id("mea"), sid, "spectrum" if i % 2 else "jv", db.now()))
                run = db.new_id("run")
                c.execute("INSERT INTO analysis_run(analysis_run_id,skill_id,"
                          " skill_version,status,started_at) VALUES(?,'t','1','ok',?)",
                          (run, db.now()))
                c.execute("INSERT INTO key_result(sample_id,analysis_run_id,field_name,"
                          " value_num,unit,source,quality,created_at)"
                          " VALUES(?,?,'PCE',?,'%','skill','validated',?)",
                          (sid, run, 15.0 + i, db.now()))
                n += 1
    return n


# ---------------------------------------------------------------- 规范化
def test_unknown_filter_keys_are_rejected_by_name(workspace):
    with pytest.raises(selection.FilterError) as exc:
        selection.normalize({"batch": ["B20"], "sneaky": 1})
    assert "sneaky" in str(exc.value)      # 要说清楚是哪一项，不能只说「无效」


def test_field_name_is_whitelisted(workspace):
    """筛选式可能来自模型，字段名不能直接拼进 SQL。"""
    with pytest.raises(selection.FilterError):
        selection.normalize({"field": [{"name": "PCE; DROP TABLE sample--", "min": 1}]})


def test_range_needs_a_bound(workspace):
    with pytest.raises(selection.FilterError):
        selection.normalize({"field": [{"name": "PCE"}]})
    with pytest.raises(selection.FilterError):
        selection.normalize({"name_range": {"prefix": "S"}})


def test_empty_filter_means_everything(populated):
    assert selection.count({}) == populated


# ---------------------------------------------------------------- 各类筛选
def test_batch_filter(populated):
    assert selection.count({"batch": ["B20"]}) == 12
    assert selection.count({"batch": ["B20", "B21"]}) == 20      # 面内是「或」


def test_folder_filter_uses_the_directory_tree(populated):
    assert selection.count({"folder": ["2026-06"]}) == 17        # B20 + B22
    assert selection.count({"folder": ["2026-07"]}) == 8


def test_filters_combine_with_and(populated):
    assert selection.count({"batch": ["B20"], "folder": ["2026-07"]}) == 0
    assert selection.count({"batch": ["B20"], "folder": ["2026-06"]}) == 12


def test_field_range_filter(populated):
    # PCE = 15 + i，B20 有 i=1..12 → 16..27
    assert selection.count({"batch": ["B20"], "field": [{"name": "PCE", "min": 25}]}) == 3
    assert selection.count({"field": [{"name": "PCE", "min": 20, "max": 22}]}) >= 1


def test_name_range_filter(populated):
    assert selection.count(
        {"name_range": {"prefix": "B20_S", "min": 1, "max": 5}}) == 5
    assert selection.count(
        {"name_range": {"prefix": "B20_S", "min": 10, "max": 99}}) == 3


def test_name_range_ignores_non_numeric_tails(workspace):
    """S1a 不该被 CAST 悄悄当成 1。"""
    with db.tx() as c:
        for name in ("S1", "S2", "S1a", "Sx"):
            c.execute("INSERT INTO sample(sample_id,name,created_at) VALUES(?,?,?)",
                      (db.new_id("smp"), name, db.now()))
    assert selection.count({"name_range": {"prefix": "S", "min": 1, "max": 9}}) == 2


def test_exclude_and_ids(populated):
    ids = selection.sample_ids({"batch": ["B20"]})
    assert len(ids) == 12
    assert selection.count({"ids": ids[:3]}) == 3
    assert selection.count({"batch": ["B20"], "exclude": ids[:2]}) == 10


def test_page_is_server_side(populated):
    p = selection.page({}, limit=5, offset=0)
    assert p["total"] == populated and len(p["rows"]) == 5
    assert p["rows"][0]["matrix_id"]          # 矩阵信息一次 JOIN 带回来


# ---------------------------------------------------------------- 分面
def test_facet_counts_do_not_zero_out_the_selected_dimension(populated):
    """选了 B20 之后，其它批次的计数必须还在 —— 否则没法改选。"""
    f = selection.facets({"batch": ["B20"]})
    assert f["total"] == 12
    by = {b["value"]: b for b in f["batch"]}
    assert by["B21"]["count"] == 8            # 没有被自己这一面压成 0
    assert by["B20"]["selected"] is True
    assert by["B21"]["selected"] is False


def test_facets_narrow_across_dimensions(populated):
    """跨面是「且」：选了文件夹之后，批次计数应该跟着变小。"""
    f = selection.facets({"folder": ["2026-07"]})
    by = {b["value"]: b["count"] for b in f["batch"]}
    assert by.get("B21") == 8
    assert by.get("B20", 0) == 0              # B20 不在 2026-07


def test_name_facet_exposes_the_real_range(populated):
    """范围要画出来，不能让人自己去查。"""
    f = selection.facets({})
    labels = {p["prefix"]: p for p in f["name"]["patterns"]}
    assert labels["B20_S"]["min"] == 1 and labels["B20_S"]["max"] == 12
    assert labels["B20_S"]["complete"] is True


# ---------------------------------------------------------------- 可枚举段
def test_detect_enumerations():
    names = [f"B20_S{i}" for i in range(1, 13)] + [f"B21_S{i}" for i in range(1, 9)]
    out = {e.prefix: e for e in naming.detect_enumerations(names)}
    assert out["B20_S"].max == 12 and out["B20_S"].count == 12
    assert out["B21_S"].max == 8


def test_detect_enumerations_reports_gaps():
    e = naming.detect_enumerations([f"S{i}" for i in (1, 2, 3, 7, 8)])[0]
    assert (e.min, e.max, e.count, e.complete) == (1, 8, 5, False)


def test_detect_enumerations_handles_zero_padding():
    e = naming.detect_enumerations([f"S{i:03d}" for i in range(1, 6)])[0]
    assert e.width == 3


def test_detect_enumerations_ignores_names_without_numbers():
    assert naming.detect_enumerations(["ctrl", "blank", "ref"]) == []


# ---------------------------------------------------------------- 选择即示例
def test_suggestion_offers_a_filter_not_an_id_list(populated):
    picked = selection.sample_ids({"name_range": {"prefix": "B20_S", "min": 1, "max": 3}})
    out = selection.suggest_expansion(picked)
    assert out, "手选 3 个应该有扩展提议"
    assert all("filter" in s for s in out)      # 给的是规则，不是 ID
    batch_offer = next(s for s in out if s["filter"].get("batch") == ["B20"])
    assert batch_offer["count"] == 12 and batch_offer["adds"] == 9


def test_no_suggestion_that_adds_nothing(populated):
    """已经选完整个批次了，就不该再提议「选中该批次全部」。"""
    offers = selection.suggest_expansion(selection.sample_ids({"batch": ["B20"]}))
    assert not any(s["filter"].get("batch") == ["B20"] for s in offers)
    assert all(s["adds"] > 0 for s in offers)


def test_suggestion_needs_at_least_two_samples(populated):
    assert selection.suggest_expansion(selection.sample_ids({"batch": ["B20"]})[:1]) == []


# ---------------------------------------------------------------- 样品集
def test_dynamic_set_grows_pinned_set_does_not(populated):
    dyn = sets.create("B20 全批", "dynamic", {"batch": ["B20"]})
    pin = sets.create("论文图3", "pinned",
                      sample_ids=selection.sample_ids({"batch": ["B20"]})[:3])
    assert dyn["count"] == 12 and pin["count"] == 3

    with db.tx() as c:
        c.execute("INSERT INTO sample(sample_id,name,batch,created_at)"
                  " VALUES(?,?,?,?)", (db.new_id("smp"), "B20_S99", "B20", db.now()))

    assert sets.get(dyn["set_id"])["count"] == 13       # 新样品自动进来
    assert sets.get(pin["set_id"])["count"] == 3        # 钉死不变


def test_freeze_turns_dynamic_into_a_snapshot(populated):
    dyn = sets.create("B20 全批", "dynamic", {"batch": ["B20"]})
    frozen = sets.freeze(dyn["set_id"])
    assert frozen["kind"] == "pinned" and frozen["count"] == 12
    with db.tx() as c:
        c.execute("INSERT INTO sample(sample_id,name,batch,created_at)"
                  " VALUES(?,?,?,?)", (db.new_id("smp"), "B20_S99", "B20", db.now()))
    assert sets.get(dyn["set_id"])["count"] == 12       # 冻结之后不再生长


def test_dynamic_set_needs_a_filter(populated):
    with pytest.raises(sets.SetError):
        sets.create("空的", "dynamic", {})


def test_duplicate_set_name_is_rejected(populated):
    sets.create("同名", "dynamic", {"batch": ["B20"]})
    with pytest.raises(sets.SetError):
        sets.create("同名", "dynamic", {"batch": ["B21"]})


def test_resolve_gives_an_executable_filter(populated):
    pin = sets.create("固定", "pinned", sample_ids=selection.sample_ids({"batch": ["B21"]}))
    assert selection.count(sets.resolve(pin["set_id"])) == 8
    dyn = sets.create("动态", "dynamic", {"batch": ["B21"]})
    assert selection.count(sets.resolve(dyn["set_id"])) == 8
