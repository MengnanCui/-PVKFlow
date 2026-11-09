"""生成假想的 QFLS 数据（P-54：「基于这些信息进行假想的(自己生成)数据处理」）。

**全部是造的**，只为了让平台有东西可处理、可展示。结构照你描述的真实情况：

  QFLS_demo/
    退火温度对比/a3f9c2e17b/slot1…4/point1…4/QFLS/0-20X-405R-1sun-<时间戳>-<uuid>.txt + pointN.qvdf
    钝化剂筛选（目录名故意写成 GBK 被当 cp437 解的乱码）/两个批次/slot…/point…（每个点 20X、100X 各一帧）
    substrate_ITO_vs_FTO/c81d0f6a2b44/slot1…4/…（100X 为主，有划痕、边缘效应）
    备份说明/、PL光谱_2026/（别的实验类型）、_tmp/、根目录零散文件  ← 应该被跳过
  QFLS 目录里还放了历史分析产物（png、csv、1- 开头的 txt、analysis_* 子目录）  ← 应该被跳过
  4 帧故意做坏：一帧少一个值（行列不一致）、一帧有空字段（缺失）、一帧 3% 像素是 0（超范围）、一帧行数不同

一共 60 帧 txt；qvdf 里记「60 帧、1sun、405R」。默认矩阵缩成 1/4（304 × 456，每帧约 1.3 MB），
full=True 时是真实的 1216 × 1824（每帧约 24 MB，60 帧约 1.4 GB）。
"""
from __future__ import annotations

import codecs
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

SMALL = (304, 456)
FULL = (1216, 1824)
SENSOR_PIXEL_UM = 5.86          # 假想相机的像元尺寸

# (实验目录名, 批次, slot 号, 点位数, 物镜列表, 场景参数)
# 场景参数：level 基准 QFLS（eV）、amp 细纹理起伏（eV，相关长度约 1 像素）、struct 大尺度起伏（eV，相关长度为短边的 1/5）、
#           dark 暗斑个数、scratch 划痕条数、bright 亮点个数、edge 边缘衰减（eV）、tilt 倾斜（eV）
E1, E2, E3 = "退火温度对比", "钝化剂筛选", "substrate_ITO_vs_FTO"
PLAN = [
    (E1, "a3f9c2e17b", 1, 4, ["20X"], dict(level=1.192, amp=0.006, dark=2, bright=6)),            # 100 °C
    (E1, "a3f9c2e17b", 2, 4, ["20X"], dict(level=1.212, amp=0.005, dark=1, bright=4)),            # 120 °C
    (E1, "a3f9c2e17b", 3, 4, ["20X"], dict(level=1.224, amp=0.004, dark=0, bright=3)),            # 140 °C
    (E1, "a3f9c2e17b", 4, 4, ["20X"], dict(level=1.206, amp=0.007, dark=7, big_dark=True, bright=10)),  # 160 °C 过退火
    (E2, "5e0c41d2", 1, 3, ["20X", "100X"], dict(level=1.185, amp=0.008, dark=9, bright=12)),     # 无钝化
    (E2, "5e0c41d2", 2, 3, ["20X", "100X"], dict(level=1.231, amp=0.004, dark=0, bright=2)),      # PEAI
    (E2, "5e0c41d2", 3, 3, ["20X", "100X"], dict(level=1.226, amp=0.005, struct=0.010, dark=1, bright=3)),  # OAI：大尺度不均
    (E2, "9b7a3f0e", 1, 3, ["20X", "100X"], dict(level=1.228, amp=0.005, dark=0, bright=2)),      # PEAI 复测
    (E3, "c81d0f6a2b44", 1, 5, ["100X"], dict(level=1.208, amp=0.004, dark=1, bright=3, odd={3: -0.022})),  # ITO；point3 局部没涂好
    (E3, "c81d0f6a2b44", 2, 5, ["100X"], dict(level=1.200, amp=0.007, struct=0.006, dark=2, bright=5, tilt=0.012)),  # FTO 粗糙
    (E3, "c81d0f6a2b44", 3, 3, ["20X", "100X"], dict(level=1.204, amp=0.005, dark=1, scratch=2, bright=4)),  # 划伤
    (E3, "c81d0f6a2b44", 4, 3, ["100X"], dict(level=1.202, amp=0.005, dark=1, edge=0.05, bright=3)),  # 边缘衰减
]
# 每个 slot 造的是什么情形（分析报告和方法说明用；真实数据没有这张表）
TREATMENT = {
    (E1, 1): "100 °C 退火", (E1, 2): "120 °C 退火", (E1, 3): "140 °C 退火", (E1, 4): "160 °C 退火（过退火）",
    (E2, 1): "无钝化", (E2, 2): "PEAI 钝化", (E2, 3): "OAI 钝化", (E2, "9b7a3f0e", 1): "PEAI 钝化（复测批次）",
    (E3, 1): "ITO 衬底", (E3, 2): "FTO 衬底", (E3, 3): "FTO，表面划伤", (E3, 4): "FTO，边缘衰减",
}
# 造进去的「已知答案」：哪些帧该被判成什么
DESIGNED = [
    ("数据无效：行列不一致", "invalid", "退火 slot3 point4 的 20X（少一个值）"),
    ("数据无效：缺失值", "invalid", "衬底 slot4 point3 的 100X（空字段）"),
    ("超范围像素（3% 为 0）", "out_of_range", "钝化 批次 5e0c41d2 slot1 point1 的 100X"),
    ("尺寸不同", "shape_differs", "衬底 slot3 point1 的 100X（少 1/16 的行）"),
    ("物镜和 qvdf 不符", "meta_mismatch", "退火 slot3 point2（qvdf 写 100X）"),
    ("缺 qvdf", "no_qvdf", "退火 slot1 point4"),
    ("划痕", "scratch", "衬底 slot3 的 6 帧（20X、100X 各 3，每帧 2 条）"),
    ("边缘效应", "edge_effect", "衬底 slot4（边缘低 50 meV；3 帧里 point3 是做坏的那帧，有效 2 帧）"),
    ("平行点离群", "group_outlier", "衬底 slot1 point3（整体低 22 meV）"),
    ("大面积低值", "large_low_area", "退火 slot4（一个大暗斑 + 6 个小暗斑）"),
]

OBJ_OFFSET = {"20X": 0.0, "100X": -0.024}   # 不同物镜收集效率 / 标定不同 → 绝对值本来就不可比
OBJ_SCALE = {"20X": 1.0, "100X": 2.0}       # 100X 下同样的结构在像素上大 5 倍；这里取 2 倍让画面里还看得到几个


def _mojibake(name: str) -> str:
    """GBK 字节被当成 cp437 解出来的样子（Windows 打的 zip 在别处解压的经典乱码）。"""
    return name.encode("gbk").decode("cp437")


def _grf(rng, shape, corr_px: float) -> np.ndarray:
    """高斯随机场：白噪声在频域里乘一个高斯核，再归一到标准差 1。"""
    H, W = shape
    noise = rng.standard_normal(shape)
    fy = np.fft.fftfreq(H)[:, None]
    fx = np.fft.rfftfreq(W)[None, :]
    k = np.exp(-2 * (np.pi * corr_px) ** 2 * (fx ** 2 + fy ** 2))
    f = np.fft.irfft2(np.fft.rfft2(noise) * k, s=shape)
    return (f - f.mean()) / (f.std() or 1)


def frame(rng, shape, sc: dict, objective: str) -> np.ndarray:
    H, W = shape
    s = OBJ_SCALE[objective]
    short = min(H, W)
    yy, xx = np.mgrid[:H, :W].astype(np.float32)
    a = np.full(shape, sc["level"] + OBJ_OFFSET[objective] + sc.get("odd_shift", 0.0), dtype=np.float64)
    a += sc["amp"] * _grf(rng, shape, 0.8 * s)
    if sc.get("struct"):
        a += sc["struct"] * _grf(rng, shape, 0.2 * short)
    if sc.get("tilt"):
        a += sc["tilt"] * (xx / W - 0.5)
    n_dark = sc.get("dark", 0) if objective == "20X" else max(0, round(sc.get("dark", 0) / 2))
    for i in range(n_dark):
        r = rng.uniform(0.012, 0.03) * short * s
        if sc.get("big_dark") and i == 0:
            r = 0.09 * short
        cy, cx = rng.uniform(0.1, 0.9) * H, rng.uniform(0.1, 0.9) * W
        a -= rng.uniform(0.03, 0.06) * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * r * r))
    for _ in range(sc.get("scratch", 0)):
        ang = rng.uniform(-0.6, 0.6)
        cy, cx = rng.uniform(0.25, 0.75) * H, rng.uniform(0.3, 0.7) * W
        L = rng.uniform(0.35, 0.6) * W
        d = np.abs((yy - cy) * np.cos(ang) - (xx - cx) * np.sin(ang))
        along = np.abs((yy - cy) * np.sin(ang) + (xx - cx) * np.cos(ang))
        w = 1.2 * (s if objective == "100X" else 1)
        a -= 0.07 * np.exp(-(d ** 2) / (2 * w * w)) * (along < L / 2)
    for _ in range(sc.get("bright", 0)):
        cy, cx = rng.uniform(0.05, 0.95) * H, rng.uniform(0.05, 0.95) * W
        r = 1.3 * (1.6 if objective == "100X" else 1)
        a += rng.uniform(0.03, 0.05) * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * r * r))
    if sc.get("edge"):
        dist = np.minimum.reduce([yy, H - 1 - yy, xx, W - 1 - xx])
        a -= sc["edge"] * np.exp(-dist / (0.08 * short))
    a += 0.004 * rng.standard_normal(shape)          # 像素噪声 4 meV
    return a


def write_txt(path: Path, a: np.ndarray, bom: bool = True) -> None:
    body = "\n".join(",".join(f"{v:.5f}" for v in row) for row in a)
    data = body.encode("ascii") + b"\n"
    path.write_bytes((codecs.BOM_UTF8 if bom else b"") + data)


def qvdf_text(point: str, slot: str, objectives: list[str], ts: datetime, shape, rng) -> str:
    """一个点位一个 qvdf；这个点用过几个物镜就列几个，像素尺寸按物镜分开写。"""
    px = [f"pixel_size_um_{o}={SENSOR_PIXEL_UM / float(o.rstrip('X')) * (FULL[1] / shape[1]):.4f}" for o in objectives]
    return "\n".join([
        "# QV 采集元数据（演示数据，假想格式）",
        "[Point]", f"name={point}", f"slot={slot}",
        f"stage_x_um={rng.uniform(-5000, 5000):.1f}", f"stage_y_um={rng.uniform(-5000, 5000):.1f}",
        "[Acquisition]", f"objective={','.join(objectives)}", "laser=405R", "wavelength_nm=405", "intensity=1sun",
        "frames=60", f"exposure_ms={rng.choice([20, 40, 80])}", *px,
        f"rows={shape[0]}", f"cols={shape[1]}", f"timestamp={ts.isoformat(timespec='seconds')}",
        "operator=demo", "",
    ])


def generate(root: str | Path, *, full: bool = False, seed: int = 20261109, progress=None,
             shape: tuple[int, int] | None = None) -> dict:
    root = Path(root)
    shape = shape or (FULL if full else SMALL)
    rng = np.random.default_rng(seed)
    root.mkdir(parents=True, exist_ok=True)
    t0 = datetime(2026, 11, 2, 9, 0, 0)
    jobs = []
    for exp, batch, slot, n_points, objs, sc in PLAN:
        for p in range(1, n_points + 1):
            for obj in objs:
                jobs.append((exp, batch, slot, p, obj, sc))
    # 退火 slot2 point1 复测一次（同一点两帧）
    jobs.insert(5, (E1, "a3f9c2e17b", 2, 1, "20X", PLAN[1][5]))
    assert len(jobs) == 60, len(jobs)
    broken = {12: "ragged", 59: "missing", 36: "zeros", 52: "shape"}
    seen = set()
    for i, (exp, batch, slot, p, obj, sc) in enumerate(jobs):
        if progress:
            progress(i, len(jobs))
        exp_dir = _mojibake(exp) if exp == E2 else exp
        pdir = root / exp_dir / batch / f"slot{slot}" / f"point{p}"
        q = pdir / "QFLS"
        q.mkdir(parents=True, exist_ok=True)
        ts = t0 + timedelta(minutes=7 * i, seconds=int(rng.integers(0, 50)))
        sh = (shape[0] - shape[0] // 16, shape[1]) if broken.get(i) == "shape" else shape
        a = frame(np.random.default_rng(seed + i), sh, {**sc, "odd_shift": sc.get("odd", {}).get(p, 0.0)}, obj)
        kind = broken.get(i)
        if kind == "zeros":
            a[: int(sh[0] * 0.17), : int(sh[1] * 0.17)] = 0.0
        u = uuid.UUID(int=int(rng.integers(0, 2 ** 63)) << 64 | int(rng.integers(0, 2 ** 63)))
        stamp = ts.strftime("%Y%m%d%H%M%S")
        if exp == E3 and slot == 2 and p in (2, 3):          # 有的采集软件写 Unix 毫秒
            stamp = str(int((ts - datetime(1970, 1, 1)).total_seconds() * 1000))
        name = f"0-{obj}-405R-1sun-{stamp}-{u.hex if (exp == E3 and slot == 1 and p == 5) else u}.txt"
        f = q / name
        write_txt(f, a)
        if kind == "ragged":
            lines = f.read_bytes().split(b"\n")
            lines[7] = lines[7].rsplit(b",", 1)[0]
            f.write_bytes(b"\n".join(lines))
        elif kind == "missing":
            lines = f.read_bytes().split(b"\n")
            cells = lines[20].split(b",")
            cells[5] = cells[9] = b""
            lines[20] = b",".join(cells)
            f.write_bytes(b"\n".join(lines))
        key = (exp_dir, batch, slot, p)
        if key not in seen:
            seen.add(key)
            objs = [o for o in next(x[4] for x in PLAN if x[:3] == (exp, batch, slot))]
            if exp == E1 and slot == 3 and p == 2:          # 元数据和文件名对不上
                objs = ["100X"]
            if not (exp == E1 and slot == 1 and p == 4):    # 这个点漏了 qvdf
                (pdir / f"point{p}.qvdf").write_text(qvdf_text(f"point{p}", f"slot{slot}", objs, ts, shape, rng),
                                                     encoding="utf-8")
        # 历史分析产物：只该取 0 开头的 txt
        if i % 4 == 0:
            (q / f"{name[:-4]}-heatmap.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
            (q / f"1-{obj}-405R-1sun-{stamp}-{u}.txt").write_text("0.1,0.2\n", encoding="utf-8")
        if i % 9 == 0:
            (q / "summary_old.csv").write_text("metric,value\nmean,1.2\n", encoding="utf-8")
            (q / "analysis_2026-10").mkdir(exist_ok=True)
            (q / "analysis_2026-10" / "report.txt").write_text("旧的分析结果", encoding="utf-8")
    # 不是实验的东西
    (root / "备份说明").mkdir(exist_ok=True)
    (root / "备份说明" / "readme.txt").write_text("这里是备份说明，不是实验数据", encoding="utf-8")
    pl = root / "PL光谱_2026" / "run01"
    pl.mkdir(parents=True, exist_ok=True)
    (pl / "spectrum_001.csv").write_text("wavelength,counts\n700,12\n", encoding="utf-8")
    (root / "_tmp").mkdir(exist_ok=True)
    (root / "说明.txt").write_text("演示数据：全部是程序生成的假想数据。", encoding="utf-8")
    (root / E1 / "a3f9c2e17b" / "备注.md").write_text("批次备注", encoding="utf-8")
    (root / E1 / "a3f9c2e17b" / "slot2" / "thumbs").mkdir(exist_ok=True)
    if progress:
        progress(len(jobs), len(jobs))
    return {"root": str(root), "frames": len(jobs), "shape": list(shape), "full": full, "seed": seed,
            "broken": {jobs[i][0] + f"/slot{jobs[i][2]}/point{jobs[i][3]} {jobs[i][4]}": k for i, k in broken.items()}}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="生成假想的 QFLS 演示数据")
    ap.add_argument("out")
    ap.add_argument("--full", action="store_true", help="真实尺寸 1216 × 1824（约 1.4 GB）")
    ap.add_argument("--seed", type=int, default=20261109)
    print(generate(ap.parse_args().out, full=ap.parse_args().full, seed=ap.parse_args().seed))
