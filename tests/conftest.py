"""每个测试跑在自己的临时工作区里，互不干扰。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    monkeypatch.setenv("HTE_WORKSPACE", str(ws))

    # config 在导入时就固化了路径，所以要重载
    import importlib

    from app import config as config_mod
    importlib.reload(config_mod)
    from app.storage import db as db_mod
    importlib.reload(db_mod)

    db_mod.close()
    config_mod.ensure_dirs()
    db_mod.init()
    db_mod.seed_defaults()

    yield ws

    db_mod.close()


@pytest.fixture()
def sample_dir(tmp_path):
    """造一小组文件：文本、图像、脏格式。"""
    src = tmp_path / "raw_data" / "B12"
    src.mkdir(parents=True)

    (src / "B12_S1_jv.csv").write_text(
        "# Instrument: SolarSim\n# Scan: reverse\n"
        "Voltage(V),Current density(mA/cm2)\n"
        "0.0,25.6\n0.2,25.5\n0.4,25.3\n0.6,24.8\n0.8,22.1\n1.0,12.4\n1.1,0.2\n",
        encoding="utf-8")

    (src / "B12_S2_spectrum.txt").write_text(
        "Wavelength(nm)\tTransmittance(%)\n300\t12.1\n400\t45.2\n500\t78.9\n600\t85.1\n",
        encoding="utf-8")

    (src / "B12_S3_messy.csv").write_text(
        "Experiment log\nDate: 2026-08-20\n\n\nidx;value;label\n1;3.2;a\n2;4.8;b\n3;;c\n",
        encoding="utf-8")

    img_dir = tmp_path / "raw_data" / "images"
    img_dir.mkdir(parents=True)
    from PIL import Image
    Image.new("RGB", (120, 90), (40, 90, 160)).save(img_dir / "B12_S1_sem.png")

    return tmp_path / "raw_data"
