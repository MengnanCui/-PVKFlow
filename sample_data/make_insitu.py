"""生成样例的原位光谱矩阵，用来试「吸收光谱 & 膜厚」这条链路。

    python sample_data/make_insitu.py

物理：湿膜厚 → 干燥收缩 → 结晶出吸收边。
    T(λ,t) = 包络(λ,t) · [1 + C(t)·cos(2π·2·OT(t)/λ)]

真实数据请用「数据处理 → 导入数据」导入你自己的文件。
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

OUT = Path(__file__).parent / "insitu"

# (样品名, 初始光学厚度 nm, 最终光学厚度 nm, 干燥时间常数 s, 吸收边 nm, 结晶时刻 s)
SAMPLES = [
    ("S1", 7600, 1080, 4.2, 778, 8.0),
    ("S2", 6900, 960, 3.1, 772, 6.2),
    ("S3", 8300, 1210, 5.6, 784, 11.5),
]


def make(lam, t, ot0, ot_end, tau, edge_nm, t_cryst, rng, noise=0.0015):
    OT = ot_end + (ot0 - ot_end) * np.exp(-t / tau)          # 收缩后趋于平台
    C = 0.28 * np.exp(-t / (tau * 2.2)) + 0.05               # 条纹对比度：湿膜高，干膜低
    prog = 1 / (1 + np.exp(-(t - t_cryst) / 1.2))            # 结晶进度
    M = np.empty((len(lam), len(t)), dtype=np.float32)
    for j in range(len(t)):
        width = 60 - 42 * prog[j]                            # 吸收边随结晶变锐
        absorb = prog[j] * 0.93 / (1 + np.exp((lam - edge_nm) / width))
        env = (0.86 - absorb) * (1 - 0.06 * (lam - lam[0]) / (lam[-1] - lam[0]))
        M[:, j] = env * (1 + C[j] * np.cos(2 * np.pi * 2 * OT[j] / lam))
    return np.clip(M + rng.normal(0, noise, M.shape), 0, None).astype(np.float32)


def main() -> None:
    rng = np.random.default_rng(11)
    lam = np.arange(400, 1150.5, 0.5)          # 1501 点，0.5 nm
    t = np.arange(0, 30.001, 0.05)             # 601 帧，20 Hz
    OUT.mkdir(parents=True, exist_ok=True)

    for name, *args in SAMPLES:
        M = make(lam, t, *args, rng)
        path = OUT / f"B20_{name}_absorbance.csv"
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Instrument: InSituSpec-2\n# Mode: Transmission\n")
            f.write("# CollectionDuration: 30.0\n")
            f.write("# MeasurementBrightPD: 48213\n# MeasurementDarkPD: 412\n")
            f.write("Wavelength(nm)," + ",".join(f"{x:.3f}" for x in t) + "\n")
            np.savetxt(f, np.column_stack([lam, M]), fmt="%.5g", delimiter=",")
        print(f"{path}  {M.shape[0]}×{M.shape[1]}  "
              f"{os.path.getsize(path) / 1e6:.1f} MB  OT {args[0]}→{args[1]} nm")


if __name__ == "__main__":
    main()
