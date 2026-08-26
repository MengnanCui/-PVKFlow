"""生成样例数据，格式和真实仪器输出一致。

    python sample_data/make_insitu.py

产物是一个主文件夹，下面若干子文件夹，每个子文件夹一次测量：

    sample_data/insitu/
        ZG0013_2026072918354709_Mode5_202607291932_SPS100/
            Data.csv
            Options.json
        ZG0014_2026072918250401_Mode5_202607291833_SPS100/
        ZG0014_2026072918250402_Mode5_202607291835_SPS100/   ← 同一片的第二次

Data.csv 是 tab 分隔、两个数据块（Origin / Absorption）、时间轴在块内
「相对第一帧时间(s)」那一行 —— 见 app/parsers/insitu_csv.py 的说明。

物理：湿膜 → 干燥收缩 → 结晶出吸收边。
    T(λ,t) = 包络(λ,t) · [1 + C(t)·cos(2π·2·OT(t)/λ)]
所以条纹频率在 k=1/λ 域上就是 2·OT，fringe_ot 应该能把 OT(t) 原样反解出来。

ZG0014 的两个文件夹是**同一片样品的两次测量**，故意放进来：
样品身份如果只看前缀，这两次会被静默合并成一次。

真实数据请用「数据处理 → 导入数据 → 按子文件夹」导入你自己的主文件夹。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np

OUT = Path(__file__).parent / "insitu"

# (文件夹名, 初始光学厚度 nm, 最终光学厚度 nm, 干燥时间常数 s, 吸收边 nm, 结晶时刻 s)
RUNS = [
    ("ZG0013_2026072918354709_Mode5_202607291932_SPS100", 7600, 2450, 4.2, 778, 8.0),
    ("ZG0014_2026072918250401_Mode5_202607291833_SPS100", 6900, 2180, 3.1, 772, 6.2),
    ("ZG0014_2026072918250402_Mode5_202607291835_SPS100", 8300, 2760, 5.6, 784, 11.5),
]

N_LAMBDA = 1028          # 真实文件的 Absorption 块约一千条波长
N_TIME = 211             # 约 10 Hz × 21 s
LAMBDA_LO, LAMBDA_HI = 330.276, 1120.568      # 上限就是你给的那个数


def wavelength_grid() -> np.ndarray:
    """光栅光谱仪的波长轴：像素上近似线性，带一点二次非线性。

    不做成严格等间隔 —— 真文件就不是（330.276 / 331.923 / 332.746 …），
    而解析器和 k 域重采样都必须扛得住不等间隔。
    """
    px = np.linspace(0.0, 1.0, N_LAMBDA)
    lam = LAMBDA_LO + (LAMBDA_HI - LAMBDA_LO) * (px + 0.035 * px * (1 - px))
    return lam * (LAMBDA_HI - LAMBDA_LO) / (lam[-1] - lam[0]) \
        - lam[0] * (LAMBDA_HI - LAMBDA_LO) / (lam[-1] - lam[0]) + LAMBDA_LO


def optical_thickness(t: np.ndarray, ot0: float, ot_end: float, tau: float) -> np.ndarray:
    """收缩后趋于平台。这是"真值"，fringe_ot 要把它找回来。"""
    return ot_end + (ot0 - ot_end) * np.exp(-t / tau)


def transmittance(lam, t, ot0, ot_end, tau, edge_nm, t_cryst, rng, noise=0.0018):
    OT = optical_thickness(t, ot0, ot_end, tau)
    C = 0.30 * np.exp(-t / (tau * 2.2)) + 0.06          # 条纹对比度：湿膜高，干膜低
    prog = 1 / (1 + np.exp(-(t - t_cryst) / 1.2))       # 结晶进度
    T = np.empty((len(lam), len(t)), dtype=np.float64)
    for j in range(len(t)):
        width = 60 - 42 * prog[j]                       # 吸收边随结晶变锐
        absorb = prog[j] * 0.93 / (1 + np.exp((lam - edge_nm) / width))
        env = (0.86 - absorb) * (1 - 0.06 * (lam - lam[0]) / (lam[-1] - lam[0]))
        T[:, j] = env * (1 + C[j] * np.cos(2 * np.pi * 2 * OT[j] / lam))
    return np.clip(T + rng.normal(0, noise, T.shape), 0.0, 1.2), OT


def guid(seed: str) -> str:
    """列头的 GUID。真文件里每一帧一个，内容无关，形状要像。"""
    return hashlib.md5(seed.encode()).hexdigest().upper()


def clock_strings(t: np.ndarray, start_s: float) -> list[str]:
    """采集时间列，格式 mm:ss.s —— 真文件就是这么写的（没有小时）。"""
    out = []
    for v in t:
        total = start_s + v
        out.append(f"{int(total // 60) % 60:02d}:{total % 60:04.1f}")
    return out


def write_block(f, name: str, lam, t, M, guids, clocks) -> None:
    """一个数据块：块头 + 采集时间 + 相对时间 + 每波长一行。"""
    f.write(f"{name} Wavelength\t" + "\t".join(guids) + "\n")
    f.write("采集时间\t" + "\t".join(clocks) + "\n")
    f.write("相对第一帧时间(s)\t" + "\t".join(f"{v:g}" for v in t) + "\n")
    for i, w in enumerate(lam):
        f.write(f"{w:.3f}\t" + "\t".join(f"{v:g}" for v in M[i]) + "\n")


def write_run(folder: Path, lam, t, T, OT, rng) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    bright, dark = 2520, 1448
    guids = [guid(f"{folder.name}-{j}") for j in range(len(t))]
    clocks = clock_strings(t, start_s=1970.3)

    # Origin：原始 PD 计数。波长轴比 Absorption 块**宽一点也密一点** ——
    # 真文件两个块的网格就不一样（322.036 起 vs 330.276 起）。
    lam_origin = np.linspace(322.036, LAMBDA_HI, N_LAMBDA + 12)
    T_origin = np.empty((len(lam_origin), len(t)))
    for j in range(len(t)):
        T_origin[:, j] = np.interp(lam_origin, lam, T[:, j])
    counts = np.round(dark + (bright - dark) * 0.62 * T_origin
                      + rng.normal(0, 3.2, T_origin.shape))

    # Absorption：0–100 的百分比，线性于 T（不是 -log10(T)）。
    # 短波端光源没输出，仪器会把值顶到 0/100 —— 真文件的 UV 端就是这种噪声。
    absorption = np.clip(100.0 * (1.0 - T / 0.86), 0.0, 100.0)
    uv = lam < 420
    absorption[uv] = np.round(rng.random(absorption[uv].shape)) * 100.0

    with open(folder / "Data.csv", "w", encoding="utf-8", newline="\n") as f:
        pad = "\t" * len(t)
        f.write(f"Mode\tMode5{pad}\n")
        f.write(f"CollectionDuration(s)\t120{pad}\n")
        f.write(f"TaskDuration(s)\t{t[-1]:.7f}{pad}\n")
        f.write(f"MeasurementBrightPD\t{bright}{pad}\n")
        f.write(f"MeasurementDarkPD\t{dark}{pad}\n")
        f.write(f"ReferencePDDiff\t32{pad}\n")
        f.write(f"ReferencePDRatio\t750{pad}\n")
        f.write(f"PDDeltaFromReference\t-1.386666667{pad}\n")
        f.write(f"PDRemainingRatio\t33.5{pad}\n")
        f.write(f"{pad}\n")
        write_block(f, "Origin", lam_origin, t, counts, guids, clocks)
        write_block(f, "Absorption", lam, t, absorption, guids, clocks)

    (folder / "Options.json").write_text(
        json.dumps({"mode": "Mode5", "collectionDuration": 120,
                    "note": "合成样例，不是真实测量"}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 真值留一份，测试拿它对答案
    np.savez(folder / "_truth.npz", t=t, ot=OT)


def main() -> None:
    rng = np.random.default_rng(11)
    lam = wavelength_grid()
    t = np.round(np.linspace(0, 21.082, N_TIME), 3)

    for name, *args in RUNS:
        T, OT = transmittance(lam, t, *args, rng)
        folder = OUT / name
        write_run(folder, lam, t, T, OT, rng)
        size = os.path.getsize(folder / "Data.csv") / 1e6
        print(f"{name}\n    {len(lam)}×{len(t)}  {size:.1f} MB  "
              f"OT {args[0]}→{args[1]} nm")


if __name__ == "__main__":
    main()
