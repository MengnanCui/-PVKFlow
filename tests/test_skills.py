"""Skill 契约：注册、打分、运行、落库。"""
import pytest

from app.skills.base import (
    FileMatch, FileRef, Metric, OutputSpec, ParamSpec, Skill, SkillResult, SkillSpec,
    default_match_score,
)
from app.skills.registry import registry
from app.storage import db, ingest


@pytest.fixture()
def loaded(workspace, sample_dir):
    registry.load_all()
    ingest.ingest_paths(ingest.scan_preview(sample_dir)["files"])
    return sample_dir


def art(pattern: str) -> str:
    return db.scalar("SELECT artifact_id FROM artifact WHERE filename LIKE ?", (pattern,))


# ---------------------------------------------------------------- 注册
def test_builtin_skills_load_without_errors(loaded):
    ids = {s.spec.id for s in registry.all()}
    assert {"table.preview", "thickness.generic", "spectrum.generic"} <= ids
    assert registry.errors == []


def test_unready_skills_declare_themselves_honestly(loaded):
    thickness = registry.get("thickness.generic")
    assert thickness.spec.ready is False
    assert thickness.spec.ready_note      # 必须说清楚为什么不能用


# ---------------------------------------------------------------- 打分
def _ref(path, name, ext):
    return FileRef(artifact_id="x", path=path, filename=name, ext=ext)


def test_declared_but_unmatched_features_lower_the_score(tmp_path):
    """光谱 skill 声明了 *spec* 文件名特征，碰到 jv.csv 就该让位。

    没有这条惩罚，所有吃 .csv 的 skill 会挤在同一个分数上，排序等于没有。
    """
    f = tmp_path / "B12_S1_jv.csv"
    f.write_text("Voltage,Current\n0,1\n")
    ref = _ref(f, "B12_S1_jv.csv", ".csv")

    spectrum = FileMatch(extensions=[".csv"], filename_globs=["*spec*"],
                         content_keywords=["wavelength"])
    generic = FileMatch(extensions=[".csv"])
    assert default_match_score(spectrum, [ref]) < default_match_score(generic, [ref])


def test_full_match_scores_top(tmp_path):
    f = tmp_path / "B12_S1_spectrum.txt"
    f.write_text("Wavelength(nm)\tT\n300\t1\n")
    ref = _ref(f, "B12_S1_spectrum.txt", ".txt")
    m = FileMatch(extensions=[".txt"], filename_globs=["*spec*"],
                  content_keywords=["wavelength"])
    assert default_match_score(m, [ref]) == 1.0


def test_wrong_extension_scores_zero(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(b"x")
    assert default_match_score(FileMatch(extensions=[".csv"]),
                               [_ref(f, "a.png", ".png")]) == 0.0


def test_suggestions_rank_the_right_skill_first(loaded):
    from app.skills.runner import build_file_refs

    refs = build_file_refs([art("%spectrum.txt")])
    assert registry.suggest(refs)[0]["skill_id"] == "spectrum.generic"


# ---------------------------------------------------------------- 运行
def test_run_writes_run_results_and_parquet(loaded):
    from app.skills import runner

    out = runner.run_skill("table.preview", [art("%jv.csv")], {})

    run_id = out["analysis_run_id"]
    run = db.query_one("SELECT * FROM analysis_run WHERE analysis_run_id=?", (run_id,))
    assert run["status"] == "ok"
    assert run["skill_version"] == "1.0.0"      # 版本要能追溯

    assert out["metrics_written"] > 0
    assert db.scalar("SELECT COUNT(*) FROM key_result WHERE analysis_run_id=?", (run_id,)) \
        == out["metrics_written"]

    assert out["tables"], "数值表应该落成 Parquet"
    table = db.query_one("SELECT * FROM data_table WHERE analysis_run_id=?", (run_id,))
    assert (workspace_path() / table["path"]).is_file()

    assert out["preview"]["series"], "应该给出可绘制的曲线"


def workspace_path():
    from app import config
    return config.WORKSPACE


def test_dry_run_writes_nothing(loaded):
    from app.skills import runner

    before = db.scalar("SELECT COUNT(*) FROM key_result")
    out = runner.run_skill("table.preview", [art("%jv.csv")], {}, save=False)

    assert out["saved"] is False
    assert out["metrics"], "试跑还是要给出结果，只是不写库"
    assert db.scalar("SELECT COUNT(*) FROM key_result") == before
    assert db.scalar("SELECT COUNT(*) FROM analysis_run") == 0


def test_unready_skill_refuses_clearly(loaded):
    from app.skills import runner

    with pytest.raises(RuntimeError) as exc:
        runner.run_skill("thickness.generic", [art("%jv.csv")])
    assert "算法尚未接入" in str(exc.value)
    # 拒绝执行的 skill 不该留下 running 状态的僵尸记录
    assert db.scalar("SELECT COUNT(*) FROM analysis_run") == 0


def test_failure_is_recorded_not_swallowed(loaded, monkeypatch):
    from app.skills import runner

    class Boom(Skill):
        spec = SkillSpec(id="test.boom", name="炸", category="test", version="0.0.1",
                         accepts=FileMatch(extensions=[".csv"]))

        def run(self, ctx):
            raise ValueError("算法内部出错了")

    registry.register(Boom())
    with pytest.raises(RuntimeError):
        runner.run_skill("test.boom", [art("%jv.csv")])

    run = db.query_one("SELECT * FROM analysis_run WHERE skill_id='test.boom'")
    assert run["status"] == "failed"
    assert "算法内部出错了" in run["error"]


def test_messy_file_is_sniffed_correctly(loaded):
    """有抬头、分号分隔、带空值 —— 仪器导出的常见样子。"""
    from app.skills import runner

    out = runner.run_skill("table.preview", [art("%messy%")], {})
    metrics = {m["field_name"]: m["value"] for m in out["metrics"]}
    assert metrics["n_rows"] == 3
    assert metrics["n_columns"] == 3
    assert "idx" in metrics["columns"]


def test_params_are_coerced_to_declared_types(loaded):
    from app.skills import runner

    # 前端传来的都是字符串
    out = runner.run_skill("table.preview", [art("%jv.csv")], {"max_rows": "5"})
    assert out["params"]["max_rows"] == 5.0
    assert {m["field_name"]: m["value"] for m in out["metrics"]}["n_rows"] == 5


def test_custom_skill_result_lands_in_key_result(loaded):
    """一个最小 skill：验证 spec → 表单 → 结果 → 落库 这条链是通的。"""
    from app.skills import runner

    class Mini(Skill):
        spec = SkillSpec(
            id="test.mini", name="最小样例", category="test", version="2.1.0",
            accepts=FileMatch(extensions=[".csv"]),
            params=[ParamSpec("factor", "系数", "number", default=2)],
            outputs=[OutputSpec("answer", "答案", unit="eV")],
        )

        def run(self, ctx):
            return SkillResult(metrics=[
                Metric("answer", 21 * ctx.param("factor"), unit="eV", quality="validated")])

    registry.register(Mini())
    out = runner.run_skill("test.mini", [art("%jv.csv")], {"factor": 2})

    row = db.query_one("SELECT * FROM key_result WHERE field_name='answer'")
    assert row["value_num"] == 42
    assert row["unit"] == "eV"
    assert row["quality"] == "validated"
    assert row["version"] == "2.1.0"
    assert row["source"] == "skill"
    assert row["sample_id"], "结果应该挂到样品上"


def test_text_and_numeric_values_go_to_different_columns(loaded):
    from app.skills import runner
    from app.storage import results

    run_id = results.start_run("t", "1.0")
    results.write_results(run_id, [
        {"field_name": "num", "value": 3.5},
        {"field_name": "txt", "value": "abc"},
    ])
    num = db.query_one("SELECT * FROM key_result WHERE field_name='num'")
    txt = db.query_one("SELECT * FROM key_result WHERE field_name='txt'")
    assert num["value_num"] == 3.5 and num["value_text"] is None
    assert txt["value_text"] == "abc" and txt["value_num"] is None
