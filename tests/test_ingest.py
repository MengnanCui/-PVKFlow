"""导入：分类落地、去重、断链检测。这是「原始数据不丢」的底线。"""
from pathlib import Path

import pytest

from app.storage import db, ingest


def test_scan_preview_classifies_text_and_images(workspace, sample_dir):
    prev = ingest.scan_preview(sample_dir)
    assert prev["count"] == 4
    assert prev["to_copy"] == 3          # 三个文本
    assert prev["to_reference"] == 1     # 一张图

    by_name = {Path(r["display_path"]).name: r for r in prev["files"]}
    assert by_name["B12_S1_jv.csv"]["storage_mode"] == "copied"
    assert by_name["B12_S1_sem.png"]["storage_mode"] == "referenced"
    assert by_name["B12_S1_jv.csv"]["sample"] == "S1"


def test_text_files_are_copied_images_are_not(workspace, sample_dir):
    prev = ingest.scan_preview(sample_dir)
    report = ingest.ingest_paths(prev["files"])
    assert len(report.imported) == 4
    assert not report.failed

    csv_row = db.query_one(
        "SELECT * FROM artifact WHERE filename = 'B12_S1_jv.csv'")
    assert csv_row["storage_mode"] == "copied"
    assert csv_row["stored_path"]
    assert (workspace / csv_row["stored_path"]).is_file()

    png_row = db.query_one("SELECT * FROM artifact WHERE filename = 'B12_S1_sem.png'")
    assert png_row["storage_mode"] == "referenced"
    assert png_row["stored_path"] is None
    assert Path(png_row["original_path"]).is_file()
    # 图像没被复制进工作区，但生成了缩略图
    assert png_row["thumb_path"]
    assert (workspace / png_row["thumb_path"]).is_file()


def test_original_files_are_never_modified(workspace, sample_dir):
    before = {p: p.read_bytes() for p in sample_dir.rglob("*") if p.is_file()}
    ingest.ingest_paths(ingest.scan_preview(sample_dir)["files"])
    for p, content in before.items():
        assert p.read_bytes() == content


def test_reimport_is_deduplicated_by_content(workspace, sample_dir):
    prev = ingest.scan_preview(sample_dir)
    first = ingest.ingest_paths(prev["files"])
    second = ingest.ingest_paths(prev["files"])

    assert len(first.imported) == 4
    assert len(second.imported) == 0
    assert len(second.duplicates) == 4
    assert db.scalar("SELECT COUNT(*) FROM artifact WHERE kind='raw'") == 4


def test_same_content_different_name_is_still_one_artifact(workspace, sample_dir):
    ingest.ingest_paths(ingest.scan_preview(sample_dir)["files"])
    copy = sample_dir / "B12" / "B12_S9_jv.csv"
    copy.write_bytes((sample_dir / "B12" / "B12_S1_jv.csv").read_bytes())

    report = ingest.ingest_paths([{
        "abs_path": str(copy), "display_path": "B12/B12_S9_jv.csv",
    }])
    assert len(report.duplicates) == 1
    assert db.scalar("SELECT COUNT(*) FROM artifact WHERE kind='raw'") == 4


def test_samples_are_created_once_and_reused(workspace, sample_dir):
    ingest.ingest_paths(ingest.scan_preview(sample_dir)["files"])
    names = [r["name"] for r in db.query("SELECT name FROM sample ORDER BY name")]
    assert names == ["S1", "S2", "S3"]      # S1 出现两次（csv + png），只建一个


def test_missing_reference_is_flagged_not_silent(workspace, sample_dir):
    ingest.ingest_paths(ingest.scan_preview(sample_dir)["files"])
    png = db.query_one("SELECT * FROM artifact WHERE filename='B12_S1_sem.png'")
    Path(png["original_path"]).unlink()

    result = ingest.verify_references()
    assert png["artifact_id"] in result["missing"]
    assert db.scalar("SELECT status FROM artifact WHERE artifact_id=?",
                     (png["artifact_id"],)) == "missing"


def test_local_path_raises_with_a_readable_message(workspace, sample_dir):
    from app.storage import artifacts

    ingest.ingest_paths(ingest.scan_preview(sample_dir)["files"])
    png = db.query_one("SELECT * FROM artifact WHERE filename='B12_S1_sem.png'")
    Path(png["original_path"]).unlink()

    with pytest.raises(FileNotFoundError) as exc:
        artifacts.local_path(png["artifact_id"])
    assert "引用" in str(exc.value)          # 说清楚这是引用型文件断链了


def test_uploads_always_copy_even_images(workspace):
    """浏览器不给真实路径，所以上传通道只能复制。这是有意的取舍。"""
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (20, 20)).save(buf, "PNG")
    report = ingest.ingest_uploads([("B12_S5_sem.png", buf.getvalue())])

    assert len(report.imported) == 1
    row = db.query_one("SELECT * FROM artifact WHERE filename='B12_S5_sem.png'")
    assert row["storage_mode"] == "copied"
    assert (workspace / row["stored_path"]).is_file()


def test_failed_entry_does_not_abort_the_batch(workspace, sample_dir):
    entries = ingest.scan_preview(sample_dir)["files"]
    entries.append({"abs_path": str(sample_dir / "does-not-exist.csv")})
    report = ingest.ingest_paths(entries)
    assert len(report.imported) == 4
    assert len(report.failed) == 1
