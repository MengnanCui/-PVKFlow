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


# ------------------------------------------------------------------ 时间维度
def test_time_filter_compiles_to_measured_at(workspace):
    """按时间筛选走 measurement.measured_at。

    ISO 8601 是字典序可比的，所以用字符串比较而不是 datetime 函数 ——
    函数会让 idx_mea_measured_at 这个索引用不上。
    """
    from app.storage import selection

    c = selection.compile_filter({"time": {"from": "2026-07-29T00:00",
                                           "to": "2026-07-30T00:00"}})
    assert "measured_at >= ?" in c.where and "measured_at <= ?" in c.where
    assert "datetime(" not in c.where and "strftime(" not in c.where
    # 上界按用户给的精度补到那一刻的末尾 —— 见下面那条测试
    assert c.params == ["2026-07-29T00:00", "2026-07-30T00:00:59~"]


def test_upper_bound_includes_the_whole_minute_the_user_typed(workspace):
    """★「到 18:35」在人话里包含 18:35 这一分钟。

    字符串比较下 `"…T18:35:47" <= "…T18:35"` 是**假**的，不补齐的话那一分钟里
    的测量会被静默排除。真实数据里同一轮实验的几次测量只差几分钟
    （18:25 / 18:33 / 18:35），少掉一整分钟就是少掉一次测量，
    而且界面上只表现为「怎么少了一个」。
    """
    from app.storage import db, selection

    ts = db.now()
    with db.tx() as conn:
        for i, at in enumerate(["2026-07-29T18:25:04.01",
                                "2026-07-29T18:35:47.09",     # 就在上界那一分钟里
                                "2026-07-29T18:36:00.00"]):
            conn.execute("INSERT INTO sample (sample_id, name, created_at) VALUES (?,?,?)",
                         (f"s{i}", f"样品{i}", ts))
            conn.execute(
                "INSERT INTO measurement (measurement_id, sample_id, method,"
                "                         measured_at, created_at) VALUES (?,?,?,?,?)",
                (f"m{i}", f"s{i}", "absorbance", at, ts))

    got = selection.count({"time": {"from": "2026-07-29T18:30", "to": "2026-07-29T18:35"}})
    assert got == 1, "18:35:47 那次测量必须落在「到 18:35」里"

    # 只给到日的时候补到当天末尾，整天都算
    assert selection.count({"time": {"from": "2026-07-29", "to": "2026-07-29"}}) == 3


def test_inclusive_upper_bound_handles_each_precision(workspace):
    from app.storage.selection import _inclusive_to

    assert _inclusive_to("2026-07-29") == "2026-07-29T23:59:59~"
    assert _inclusive_to("2026-07-29T18:35") == "2026-07-29T18:35:59~"
    assert _inclusive_to("2026-07-29T18:35:47") == "2026-07-29T18:35:47~"
    # 已经带小数秒的原样放行 —— 再补尾巴会把下一秒也圈进来
    assert _inclusive_to("2026-07-29T18:35:47.09") == "2026-07-29T18:35:47.09"


def test_time_filter_accepts_one_open_end(workspace):
    from app.storage import selection

    c = selection.compile_filter({"time": {"from": "2026-01-01"}})
    assert "measured_at >= ?" in c.where and "measured_at <= ?" not in c.where


def test_time_filter_swaps_inverted_ends(workspace):
    """端点写反了就换过来，不用为这个报错。"""
    from app.storage import selection

    got = selection.normalize({"time": {"from": "2026-09-01", "to": "2026-01-01"}})
    assert got["time"] == {"from": "2026-01-01", "to": "2026-09-01"}


def test_empty_time_object_means_no_filter(workspace):
    """`{}` = 没有时间约束，直接丢掉，跟其他键一致。"""
    from app.storage import selection

    assert selection.normalize({"time": {}}) == {}


def test_time_filter_with_both_ends_blank_is_an_error(workspace):
    """`{from:"", to:""}` 不一样 —— 调用方以为自己在筛，实际什么也没筛。

    这种「以为筛了其实没筛」比报错危险得多：你会拿全部样品当成筛出来的一批。
    """
    from app.storage import selection

    with pytest.raises(selection.FilterError, match="至少要有"):
        selection.normalize({"time": {"from": "", "to": ""}})


def test_unknown_filter_keys_are_still_rejected(workspace):
    """砍掉面板不等于放松筛选式。多一个键都不认 ——

    筛选式可能来自模型，静默忽略一个拼错的键会给出「看起来对」的错结果。
    """
    from app.storage import selection

    with pytest.raises(selection.FilterError, match="不认识的筛选项"):
        selection.normalize({"tiem": {"from": "2026-01-01"}})


def test_folder_facet_is_dropped_when_every_folder_holds_one_sample(workspace, tmp_path):
    """一个子文件夹一次测量时，文件夹分面 = 把样品列表抄了一遍。

    40 个各含 1 个的 chip 不是筛选，是噪声 —— 整个分面直接不给。
    想按文件夹名找某一个，用搜索框。
    """
    import sys
    sys.path.insert(0, "tests")
    from test_insitu_csv import write_data_csv

    from app.storage import ingest, selection

    root = tmp_path / "raw"
    for i in range(4):
        d = root / f"ZG{i:04d}_2026072909{i:02d}0000_Mode5"
        d.mkdir(parents=True)
        write_data_csv(d / "Data.csv", n_lam=40, ot=5000.0 + 300 * i)
    ingest.ingest_paths(ingest.scan_folders(root)["files"])

    assert selection.count({}) == 4
    assert selection.facets({})["folder"] == []


def test_folder_facet_survives_when_folders_actually_group(workspace, tmp_path):
    """真的有分组时照常给 —— 挡掉的只是退化情况。"""
    from app.storage import ingest, selection

    src = tmp_path / "raw"
    for d, day in enumerate(("2026-07", "2026-08")):
        (src / day).mkdir(parents=True)
        for i in range(3):
            # 内容必须各不相同 —— 导入是按 sha256 内容去重的，
            # 两个字节相同的文件只会登记一次（这是有意设计）。
            (src / day / f"B{day[-2:]}_S{i}_jv.csv").write_text(
                f"V,I\n0,{d * 10 + i}\n1,{d * 10 + i + 1}\n", encoding="utf-8")
    ingest.ingest_paths(ingest.scan_preview(src)["files"])

    folders = {r["value"]: r["count"] for r in selection.facets({})["folder"]}
    assert folders == {"2026-07": 3, "2026-08": 3}
