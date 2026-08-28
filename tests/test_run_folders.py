"""按子文件夹导入 —— 一个子文件夹 = 一次测量 = 一个样品。

这里最要紧的一条：**ZG0014 的两次测量必须是两个样品**。
只按前缀 ZG0014 认身份的话，两次会被静默合并成一次，
数据没丢但全串了，而且小数据集上根本看不出来。
（同一类身份 bug 在这个项目里已经栽过两次，所以先写测试。）
"""
import numpy as np
import pytest

from app.storage import db, ingest, naming
from tests.test_insitu_csv import write_data_csv


RUNS = [
    "ZG0013_2026072918354709_Mode5_202607291932_SPS100",
    "ZG0014_2026072918250401_Mode5_202607291833_SPS100",
    "ZG0014_2026072918250402_Mode5_202607291835_SPS100",
]


@pytest.fixture()
def main_folder(tmp_path):
    """一个主文件夹，三个子文件夹，外加两个该被跳过的目录。"""
    root = tmp_path / "raw"
    for i, name in enumerate(RUNS):
        d = root / name
        d.mkdir(parents=True)
        # 波长要够密：775–1120 那个窗口里至少得有 32 个点
        write_data_csv(d / "Data.csv", n_lam=200 + i, ot=5000.0 + 400 * i)
        (d / "Options.json").write_text("{}", encoding="utf-8")
    (root / "空文件夹").mkdir()                       # 没有 Data.csv
    (root / ".hidden").mkdir()                        # 隐藏目录
    (root / "散落的.csv").write_text("a,b\n1,2\n", encoding="utf-8")   # 不是目录
    return root


# ---------------------------------------------------------------- 文件夹名解析
def test_parse_run_folder():
    r = naming.parse_run_folder(RUNS[0])
    assert r.name == RUNS[0]                     # 样品名是完整文件夹名
    assert r.device == "ZG0013"                  # 样品号单独一维
    assert r.measured_at == "2026-07-29T18:35:47.09"
    assert r.mode == "Mode5"


def test_parse_run_folder_survives_an_unknown_shape():
    """命名规则以后变了，最坏是筛选少一维，不能导不进来。"""
    r = naming.parse_run_folder("完全不按套路的名字")
    assert r.name == "完全不按套路的名字"
    assert r.measured_at == "" and r.mode == ""


# ---------------------------------------------------------------- 扫描
def test_scan_folders_finds_one_row_per_subfolder(main_folder):
    prev = ingest.scan_folders(main_folder)
    assert prev["count"] == 3
    assert prev["mode"] == "folders"
    assert {r["sample"] for r in prev["files"]} == set(RUNS)
    assert all(r["filename"] == "Data.csv" for r in prev["files"])


def test_scan_folders_says_what_it_skipped(main_folder):
    """跳过的文件夹要说出来，不能静默少几个。"""
    prev = ingest.scan_folders(main_folder)
    skipped = {s["folder"] for s in prev["skipped"]}
    assert "空文件夹" in skipped
    assert all("Data.csv" in s["reason"] for s in prev["skipped"])


def test_scan_folders_needs_a_directory(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("a\n", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        ingest.scan_folders(f)


# ---------------------------------------------------------------- 导入与身份
def test_two_runs_of_the_same_device_stay_separate(workspace, main_folder):
    """★ ZG0014 测了两次 = 两个样品，不是一个。"""
    prev = ingest.scan_folders(main_folder)
    rep = ingest.ingest_paths(prev["files"]).as_dict()
    assert rep["counts"]["imported"] == 3

    assert db.scalar("SELECT COUNT(*) FROM sample") == 3
    zg14 = db.query("SELECT name FROM sample WHERE batch='ZG0014' ORDER BY name")
    assert len(zg14) == 2
    assert zg14[0]["name"] != zg14[1]["name"]


def test_device_goes_to_batch_for_the_sample_number_facet(workspace, main_folder):
    """样品号存进 sample.batch —— 复用已有的列和索引，界面上换个标题。"""
    prev = ingest.scan_folders(main_folder)
    ingest.ingest_paths(prev["files"])
    devices = {r["batch"] for r in db.query("SELECT DISTINCT batch FROM sample")}
    assert devices == {"ZG0013", "ZG0014"}


def test_measured_at_lands_on_the_measurement_row(workspace, main_folder):
    """按时间筛选全靠这一列。文件夹名里的时间戳必须落到库里。"""
    prev = ingest.scan_folders(main_folder)
    ingest.ingest_paths(prev["files"])
    times = sorted(r["measured_at"] for r in
                   db.query("SELECT measured_at FROM measurement"))
    assert len(times) == 3
    assert times[0].startswith("2026-07-29T18:25:04")
    assert times[-1].startswith("2026-07-29T18:35:47")


def test_measured_at_falls_back_to_file_mtime(workspace, tmp_path):
    """文件夹名里没时间戳时退回文件修改时间 —— 有个近似值比这一维失效强。"""
    root = tmp_path / "raw"
    d = root / "没有时间戳的文件夹"
    d.mkdir(parents=True)
    write_data_csv(d / "Data.csv")
    ingest.ingest_paths(ingest.scan_folders(root)["files"])
    at = db.scalar("SELECT measured_at FROM measurement")
    assert at and at.startswith("20")


# ---------------------------------------------------------------- is_matrix
def test_is_matrix_is_decided_at_import_not_lazily(workspace, main_folder):
    """导入完就该能立刻批处理。

    以前 is_matrix 是惰性的：只有当有人访问 /api/spectra/samples 时才回填。
    在那之前筛选式的 has_matrix 看到的是零个矩阵 —— 刚导完去跑批处理
    命中 0 个样品，而且没有任何提示。
    """
    from app.storage import selection

    prev = ingest.scan_folders(main_folder)
    ingest.ingest_paths(prev["files"])

    assert db.scalar("SELECT COUNT(*) FROM artifact WHERE is_matrix IS NULL") == 0
    assert selection.count({"has_matrix": True}) == 3


def test_non_matrix_files_are_marked_zero_not_null(workspace, tmp_path):
    src = tmp_path / "misc"
    src.mkdir()
    (src / "B1_S1_note.csv").write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    ingest.ingest_paths(ingest.scan_preview(src)["files"])
    assert db.scalar("SELECT is_matrix FROM artifact") == 0


# ---------------------------------------------------------------- 端到端
def test_imported_run_parses_and_gives_a_thickness_curve(workspace, main_folder):
    """导进来的文件要能一路走到膜厚曲线。"""
    from app.analysis import fringe_ot
    from app.parsers import matrix
    from app.storage import artifacts

    prev = ingest.scan_folders(main_folder)
    rep = ingest.ingest_paths(prev["files"]).as_dict()
    aid = rep["imported"][0]["artifact_id"]

    sm = matrix.load_cached(artifacts.local_path(aid))
    assert sm.meta["block"] == "Absorption"

    res = fringe_ot.extract_series(
        sm.lam, sm.t, sm.M, target_times_s="all",
        window_nm=fringe_ot.PLATFORM_WINDOW_NM)
    assert len(res["points"]) == sm.t.size
    assert np.isfinite([q["ot_nm"] for q in res["points"]]).all()


# ---------------------------------------------------------------- 去重
def test_reimporting_the_same_folder_is_a_duplicate(workspace, main_folder):
    """同一个主文件夹导两次，第二次应该一个都不进。"""
    prev = ingest.scan_folders(main_folder)
    first = ingest.ingest_paths(prev["files"]).as_dict()
    assert first["counts"]["imported"] == 3

    second = ingest.ingest_paths(prev["files"]).as_dict()
    assert second["counts"]["imported"] == 0
    assert second["counts"]["duplicates"] == 3
    assert db.scalar("SELECT COUNT(*) FROM sample") == 3


def test_folder_dedup_survives_the_file_being_re_exported(workspace, main_folder):
    """★ 仪器重新导出一次，字节变了、sha 变了，但那还是同一次测量。

    只靠 sha256 去重的话这一次会当成新数据放进来，于是同一个子文件夹
    在库里有了两份 —— 而它们本该是**一个样品的一次测量**。
    """
    prev = ingest.scan_folders(main_folder)
    ingest.ingest_paths(prev["files"])
    n_art = db.scalar("SELECT COUNT(*) FROM artifact")

    # 原地改一个字节：内容哈希变了，路径和文件夹没变
    target = main_folder / RUNS[0] / "Data.csv"
    target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    rep = ingest.ingest_paths(ingest.scan_folders(main_folder)["files"]).as_dict()
    assert rep["counts"]["imported"] == 0
    assert db.scalar("SELECT COUNT(*) FROM artifact") == n_art
    whys = {d.get("reason") for d in rep["duplicates"]}
    assert "同一个文件夹" in whys or "同一个文件" in whys


def test_duplicate_rows_say_why_they_were_skipped(workspace, main_folder):
    """跳过的理由要写出来 —— 「导了怎么一个都没进」得有答案。"""
    prev = ingest.scan_folders(main_folder)
    ingest.ingest_paths(prev["files"])
    rep = ingest.ingest_paths(prev["files"]).as_dict()
    assert all(d.get("reason") for d in rep["duplicates"])
