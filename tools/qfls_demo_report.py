"""用演示数据（假想）跑一遍 QFLS 处理，写一份分析报告：docs/QFLS_演示数据分析.md。

    .venv/bin/python tools/qfls_demo_report.py            # 默认 304 × 456，约 10 秒（不出图）
    .venv/bin/python tools/qfls_demo_report.py --full     # 真实尺寸 1216 × 1824（约 1.4 GB 临时数据）

报告分两半：① 平台有没有把**造进去的已知答案**找出来（演示数据的意义就在这）；
② 把它当成一批真实数据来读 —— 但所有「结论」都只是演示数据里造进去的情形，不代表任何真实材料。
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def med(v):
    v = [x for x in v if x is not None]
    return float(np.median(v)) if v else float("nan")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "QFLS_演示数据分析.md"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="qfls_report_"))
    os.environ["HTE_WORKSPACE"] = str(tmp / "ws")
    from app.qfls import demo, pipeline
    from app.qfls.metrics import PARAMS

    gen = demo.generate(tmp / "QFLS_demo", full=a.full)
    run = pipeline.process(tmp / "QFLS_demo", figures=False)
    pts = pipeline.load(run["run_id"], "points.json")
    summ = pipeline.load(run["run_id"], "summary.json")
    scan = pipeline.load(run["run_id"], "scan.json")
    valid = [p for p in pts if p["valid"]]
    code = {e["name"]: e["code"] for e in summ["experiments"]}

    def treat(p):
        t = demo.TREATMENT.get((p["experiment"], p["batch"], p["slot_no"])) or demo.TREATMENT.get((p["experiment"], p["slot_no"]))
        return t or "?"

    L = []
    w = L.append
    w("# QFLS 演示数据分析（假想数据）\n")
    w("> **这份报告里的所有数据都是程序生成的假想数据**（`app/qfls/demo.py`，种子 20261109），"
      "用来检验平台能不能按你给的格式正确识别、校验、计算、标出异常。下面「读数据」那一半的结论，"
      "都只是演示数据里**造进去的情形**被读了出来，**不代表任何真实材料或工艺**。\n")
    w(f"重新生成：`.venv/bin/python tools/qfls_demo_report.py{' --full' if a.full else ''}`。"
      f"矩阵 {gen['shape'][0]} × {gen['shape'][1]}（{'真实尺寸' if a.full else '真实尺寸 1216 × 1824 的 1/4'}），"
      f"处理 {run['seconds']} 秒（不出图）。阈值见方法说明页 / 计划文档 D-162 … D-177。\n")

    # ---------------------------------------------------------------- ① 识别
    w("## 1 · 数据识别\n")
    w(f"- 根目录下认出 **{len(scan['experiments'])} 个实验**、{sum(len(e['batches']) for e in scan['experiments'])} 个批次、"
      f"**{run['n_frames']} 帧**（20X {sum(p['objective'] == '20X' for p in pts)}、100X {sum(p['objective'] == '100X' for p in pts)}）")
    w(f"- 跳过 {len(scan['skipped'])} 处：" + "；".join(f"`{s['path']}`（{s['reason'][:22]}…）" if len(s['reason']) > 22 else f"`{s['path']}`（{s['reason']}）"
                                                for s in scan["skipped"]))
    w(f"- 历史分析产物 {scan['artifacts']} 个（png、csv、1- 开头的 txt、analysis_* 目录）全部没取")
    for r in scan["renamed"]:
        w(f"- 乱码目录名 `{r['raw']}` → **{r['fixed']}**（{r['how']}）；磁盘上的名字没改")
    w("- 编号对照：" + "、".join(f"{e['code']} = {e['name']}" for e in summ["experiments"]) + "\n")

    # ---------------------------------------------------------------- ② 已知答案
    w("## 2 · 造进去的已知答案，找到了没有\n")
    w("| 造的是什么 | 在哪 | 应出现的标记 | 实际被标的帧 | 结果 |")
    w("|---|---|---|---|---|")
    by_flag = defaultdict(list)
    for p in pts:
        for c in p["flag_codes"]:
            by_flag[c].append(p["code"])
    for what, flag, where in demo.DESIGNED:
        hits = by_flag.get(flag, [])
        if flag == "invalid":       # 两种无效分开对：按无效原因
            key = "行列" if "行列" in what else "缺失"
            hits = [p["code"] for p in pts if not p["valid"] and key in (p["error"] or "")]
        w(f"| {what} | {where} | `{flag}` | {len(hits)}：{'、'.join(hits[:8])}{' …' if len(hits) > 8 else ''} | {'✓ 找到' if hits else '✗ 没找到'} |")
    sc = [p for p in pts if p["experiment"] == demo.E3 and p["slot_no"] == 3 and p["valid"]]
    w(f"\n- 划痕：衬底 slot3 有效的 {len(sc)} 帧，每帧找到的划痕条数 = "
      f"{', '.join(str(p['scratch_count']) for p in sc)}（造的是每帧 2 条）")
    other_scr = [p["code"] for p in valid if p["scratch_count"] and not (p["experiment"] == demo.E3 and p["slot_no"] == 3)]
    w(f"- 划痕误报（不在划伤 slot 里却报了划痕）：{len(other_scr)} 帧{('：' + '、'.join(other_scr)) if other_scr else ''}")
    ed = [p for p in valid if p["experiment"] == demo.E3 and p["slot_no"] == 4]
    ec = ", ".join(f"{p['edge_minus_center'] * 1000:.0f}" for p in ed)
    w(f"- 边缘效应：衬底 slot4 的边缘 − 中心 = {ec} meV；"
      f"其它帧 |边缘 − 中心| 的最大值 {max(abs(p['edge_minus_center']) for p in valid if p not in ed) * 1000:.1f} meV")
    unif = [p for p in valid if demo.TREATMENT.get((p["experiment"], p["slot_no"])) in ("140 °C 退火", "PEAI 钝化") and p["objective"] == "20X"]
    w(f"- Moran's I：造得最均匀的两种（140 °C 退火、PEAI 钝化，20X）中位数 {med([p['morans_i'] for p in unif]):.3f}（≈ 0，随机）；"
      f"造了大尺度不均的 OAI 钝化 {med([p['morans_i'] for p in valid if treat(p) == 'OAI 钝化']):.2f}、"
      f"FTO 衬底 {med([p['morans_i'] for p in valid if treat(p) == 'FTO 衬底']):.2f}（> {PARAMS['moran_threshold']}，结构不均）")
    w("")

    # ---------------------------------------------------------------- ③ 分组
    w("## 3 · 按处理条件（slot）汇总\n")
    w("每组取各帧的中位数。**QFLS 中位数只在同一物镜内比较**（100X 的绝对值比 20X 低，是造进去的标定差，不是材料差）。\n")
    w("| 实验 | slot（造的情形） | 物镜 | 帧 | QFLS 中位数 (eV) | P90−P10 (meV) | 低于中位数−2σ | 最大低值连通域 | Moran's I | 综合分 | 等级 |")
    w("|---|---|---|---|---|---|---|---|---|---|---|")
    groups = defaultdict(list)
    for p in valid:
        groups[(code[p["experiment"]], p["batch"], p["slot_no"], p["objective"])].append(p)
    for (ec, b, s, o), ps in sorted(groups.items(), key=lambda x: (x[0][0], x[0][3], x[0][1], x[0][2])):
        g = Counter(p["grade"] for p in ps)
        w(f"| {ec} | {b[:6]} slot{s}（{treat(ps[0])}） | {o} | {len(ps)} | {med([p['median'] for p in ps]):.4f} | "
          f"{med([p['p90_p10'] for p in ps]) * 1000:.1f} | {med([p['low_2s_frac'] for p in ps]):.1%} | {med([p['low_cc_max_frac'] for p in ps]):.2%} | "
          f"{med([p['morans_i'] for p in ps]):.2f} | {med([p['score'] for p in ps]):.1f} | {' '.join(f'{k}{v}' for k, v in sorted(g.items()))} |")
    w("")

    # ---------------------------------------------------------------- ④ 读数据
    w("## 4 · 把它当成一批数据来读（结论只针对演示数据里造的情形）\n")
    ann = {s: [p for p in valid if p["experiment"] == demo.E1 and p["slot_no"] == s] for s in (1, 2, 3, 4)}
    order = sorted(ann, key=lambda s: -med([p["median"] for p in ann[s]]))
    w(f"**{code[demo.E1]} 退火温度（20X）**：QFLS 中位数从高到低 " + " > ".join(
        f"{demo.TREATMENT[(demo.E1, s)]}（{med([p['median'] for p in ann[s]]):.3f} eV）" for s in order) + "。"
      f"140 °C 的综合分最高（{med([p['score'] for p in ann[3]]):.0f}）、几乎没有低值区；160 °C 的 QFLS 回落、"
      f"出现大面积低值区（最大一块中位数占画面 {med([p['low_cc_max_frac'] for p in ann[4]]):.1%}），像过退火分解 —— 这正是造的情形。\n")
    for obj in ("20X", "100X"):
        pas = {t: [p for p in valid if p["experiment"] == demo.E2 and p["objective"] == obj and treat(p) == t]
               for t in ("无钝化", "PEAI 钝化", "OAI 钝化", "PEAI 钝化（复测批次）")}
        w(f"**{code[demo.E2]} 钝化剂（{obj}）**：" + "；".join(
            f"{t} QFLS {med([p['median'] for p in ps]):.3f} eV、分 {med([p['score'] for p in ps]):.0f}、Moran's I {med([p['morans_i'] for p in ps]):.2f}"
            for t, ps in pas.items()) + "。")
    peai = [p["median"] for p in valid if p["experiment"] == demo.E2 and p["objective"] == "20X" and treat(p).startswith("PEAI")]
    w(f"PEAI 两个批次（20X）的中位数相差 {abs(med([p['median'] for p in valid if p['experiment'] == demo.E2 and p['objective'] == '20X' and treat(p) == 'PEAI 钝化']) - med([p['median'] for p in valid if p['experiment'] == demo.E2 and p['objective'] == '20X' and treat(p) == 'PEAI 钝化（复测批次）'])) * 1000:.1f} meV（{len(peai)} 帧），复测一致。"
      "OAI 的 QFLS 和 PEAI 接近，但 Moran's I 高、综合分低：**平均水平好、空间上不均匀** —— 只看均值会漏掉。\n")
    sub = {s: [p for p in valid if p["experiment"] == demo.E3 and p["slot_no"] == s and p["objective"] == "100X"] for s in (1, 2, 3, 4)}
    w(f"**{code[demo.E3]} 衬底（100X）**：" + "；".join(
        f"{demo.TREATMENT[(demo.E3, s)]} 分 {med([p['score'] for p in sub[s]]):.0f}、P90−P10 {med([p['p90_p10'] for p in sub[s]]) * 1000:.0f} meV"
        for s in sub if sub[s]) + "。ITO 最均匀；FTO 起伏大、有倾斜；划伤和边缘衰减的样点分数最低，问题出在制样 / 测量而不是材料本身 —— "
      "这类帧建议先排除再做材料比较。\n")

    # 同一点两个物镜
    pair = defaultdict(dict)
    for p in valid:
        pair[(p["experiment"], p["batch"], p["slot_no"], p["point_no"])][p["objective"]] = p
    both = [v for v in pair.values() if "20X" in v and "100X" in v]
    if both:
        d_med = [(v["100X"]["median"] - v["20X"]["median"]) * 1000 for v in both]
        d_low = [(v["100X"]["low_2s_frac"] - v["20X"]["low_2s_frac"]) * 100 for v in both]
        d_cc = [v["100X"]["low_cc_count"] - v["20X"]["low_cc_count"] for v in both]
        w(f"**同一点、两个物镜**（{len(both)} 个点 20X 和 100X 都有）：QFLS 中位数 100X − 20X = {np.mean(d_med):.1f} ± {np.std(d_med):.1f} meV"
          f"（造的是 −24 meV 的标定差）；低值连通域数 100X − 20X 中位数 {med(d_cc):+.0f} 个；"
          f"而相对量「低于中位数 − 2σ 占比」只差 {np.mean(d_low):+.2f} 个百分点。"
          "**这就是为什么绝对 QFLS 和面积 / 数量类指标不能跨物镜比、相对量和组内 z 可以。**\n")

    w("## 5 · 这份数据里看不出来的\n")
    w("- 只有 1 sun 一个光强：没有伪 JV、理想因子、PCE、FF（你给的限制）")
    w("- 阈值全是平台定的、只在假想数据上调过；真实数据的噪声、像素尺寸、缺陷形态都可能让划痕 / 亮点 / 连通域的像素阈值需要重调（D-167 … D-169 建议你确认）")
    w("- qvdf 的真实格式未知（D-162）：这里用的是 INI 风格的假想格式\n")
    Path(a.out).write_text("\n".join(L), encoding="utf-8")
    print(f"写好了 {a.out}（{run['n_frames']} 帧，{run['seconds']} 秒）")


if __name__ == "__main__":
    main()
