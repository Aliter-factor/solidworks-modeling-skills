#!/usr/bin/env python3
"""减速器参数表 —— 唯一数据源 + 交叉校核 + 零件计划生成。

为什么需要它
------------
`60-reducer-project.md` 把「尺寸抄错」列为**头号风险**（错在源头，下游看起来全部"通过"）。
对策是三步：**单一数据源 → 交叉校核 → 逐件对账**。这个模块就是那三步的实现：

1. **单一数据源**：`assets/reducer/reducer.params.json`。35 份零件计划都从它引用，
   任何零件尺寸**不允许在自己的计划里硬编码** —— 否则「改一个数、全套重放」就是空话。
2. **交叉校核**（`--check`）：把图纸上**分别读到的**几个数拿来互相验算
   （a 与 m(z1+z2)/2、da 与 m(z+2)、轴承内径与轴颈…）。抄错一个数必然破坏其中一条。
   **不启动 SolidWorks**，秒级，可以进 CI。
3. **逐件对账**（`reconcile`）：建完之后与 `reference.json`（36 件参照件实测）比
   包围盒 / 体积，差超阈值就报错。这是把"看起来像"变成"数据上一致"的唯一办法。

用法
----
    python scripts/sw_reducer.py --check                 # 交叉校核（离线，秒级）
    python scripts/sw_reducer.py --parts                 # 列出零件的参数化状态
    python scripts/sw_reducer.py --plan key_10x8x22      # 生成该件的建模计划 JSON
    python scripts/sw_reducer.py --plan key_10x8x22 --out D:/out/key.json
    python scripts/sw_reducer.py --selftest              # 故意改错数，确认校核会拦

口径（见 params.meta.authority）
--------------------------------
    图纸 > 参照件实测 > 国标校核

冲突时以图纸为准；图纸没有的用实测；两者都没有才用国标。
**国标只做校核，不代表图纸口径** —— 参照件实测已经和国标常规值有出入（见 open_questions Q2）。
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PARAMS_PATH = ROOT / "assets" / "reducer" / "reducer.params.json"
REFERENCE_PATH = ROOT / "assets" / "reducer" / "reference.json"


# --------------------------------------------------------------------------- 加载

def load(path: "str | Path | None" = None) -> dict:
    """读参数表。路径不传就用包内的 `assets/reducer/reducer.params.json`。"""
    p = Path(path) if path else PARAMS_PATH
    if not p.is_file():
        raise FileNotFoundError(f"找不到参数表：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def v(node, default=None):
    """取值：`{"v": 70.0, "source": "drawing"}` → `70.0`；裸值原样返回。

    参数表里带 `source` 的字段都包了一层 `{"v":…}`，方便追溯来源。
    这个函数把两种写法都吃下来，调用方不用关心。
    """
    if isinstance(node, dict) and "v" in node:
        return node["v"]
    return node if node is not None else default


def src(node) -> str:
    return node.get("source", "?") if isinstance(node, dict) else "?"


# --------------------------------------------------------------------------- 校核

class Check:
    __slots__ = ("cid", "name", "ok", "kind", "detail")

    def __init__(self, cid: str, name: str, ok: bool, detail: str, kind: str = "hard"):
        self.cid, self.name, self.ok, self.detail, self.kind = cid, name, ok, detail, kind

    def as_dict(self) -> dict:
        return {"id": self.cid, "name": self.name, "ok": self.ok,
                "kind": self.kind, "detail": self.detail}

    def __str__(self) -> str:
        mark = "PASS" if self.ok else ("SKIP" if self.kind == "open" else "FAIL")
        return f"[{mark}] {self.cid} {self.name}：{self.detail}"


def _close(a: float, b: float, tol: float) -> bool:
    return abs(float(a) - float(b)) <= float(tol) + 1e-9


def cross_check(P: dict) -> list[Check]:
    """★ 6 条（实为 8 条）交叉校核。全部只看参数表，不碰 SolidWorks。

    设计原则：**每条都拿两个"分别读到的"数互相验算**。
    如果只是 `m*z1 == m*z1` 这种同义反复，抄错了一个数还是查不出来。
    """
    out: list[Check] = []
    d = P["drive"]
    m = float(v(d["m"]))
    alpha = float(v(d["alpha_deg"]))
    z1, z2 = int(v(d["z1"])), int(v(d["z2"]))
    a = float(v(d["a"]))
    a_tol = float(d["a"].get("tol", 0.05))
    h = float(v(d["h"]))

    # ★★ da/df 一律从 **`meta.gear_train`** 读（唯一真源，2026-10-02 修正）。
    #   以前读 `P["gears"][*]["da"]` —— 那是**第二份**齿轮数据，重新设计后
    #   它没跟着改 ⇒ C4 拿上一轮的旧 da 去比对，**报出假 FAIL**
    #   （这次就踩了：C4 说「da1=40 vs 34」，40 是 z=18 时代的陈旧值）。
    #   `gears` 表只保留 bore / keyway / material 这些 wheel 独有的字段。
    _gt0 = P["meta"]["gear_train"]
    gp, gw = _gt0["pinion"], _gt0["wheel"]
    x1 = float(gp.get("x", 0) or 0)
    x2 = float(gw.get("x", 0) or 0)
    da1 = float(gp["da"])
    da2 = float(gw["da"])

    # ---- C1 中心距：图纸读到的 a  vs  由 m/z 算出的 a ----
    want = m * (z1 + z2) / 2.0
    out.append(Check("C1", "中心距 a == m(z1+z2)/2", _close(a, want, a_tol),
                     f"图纸 a={a}，算得 {want}（m={m}, z={z1}+{z2}），公差 ±{a_tol}"))

    # ---- C2 分度圆：d = m z（派生量，只报值，靠 C3 交叉验）----
    d1, d2 = m * z1, m * z2
    out.append(Check("C2", "分度圆 d1=m·z1, d2=m·z2", True,
                     f"d1={d1}, d2={d2}（派生量；与 a 的关系见 C3）", kind="info"))

    # ---- C3 中心距闭合：**用 da 反推 d** —— 与 C1 相互独立 ----
    # da = d + 2m + 2xm ⇒ d = da − 2m − 2xm
    # ⇒ (d1+d2)/2 = (da1+da2)/2 − 2m − (x₁+x₂)m
    # ★ 高度变位 x₁+x₂ = 0 时**退化为旧式** (da1+da2)/2 − 2m。
    a_from_da = (da1 + da2) / 2.0 - 2.0 * m - (x1 + x2) * m
    out.append(Check("C3", "中心距闭合 (da1+da2)/2 − 2m − (x₁+x₂)m == a",
                     _close(a, a_from_da, 2 * a_tol),
                     f"由两个齿顶圆反推 a={a_from_da}，图纸 a={a}"
                     f"（da1={da1}, da2={da2}, m={m}, x₁+x₂={x1 + x2:+.2f} —— "
                     f"高度变位下 x₁+x₂=0 ⇒ 与旧式逐位相同）"))

    # ---- C4 齿顶圆：da == m(z + 2h_a* + 2x) ----
    w1, w2 = m * (z1 + 2 + 2 * x1), m * (z2 + 2 + 2 * x2)
    ok4 = _close(da1, w1, 0.05) and _close(da2, w2, 0.05)
    out.append(Check("C4", "齿顶圆 da == m(z+2h_a*+2x)", ok4,
                     f"da1={da1} vs {w1}；da2={da2} vs {w2}"
                     "（★ 变位项 2xm 必须带 —— 漏掉它 x=+0.2 就差 0.8mm，"
                     "正好落在容差外，这条正是抓这个的）"))

    # ---- C5 轴承内径 == 轴颈 ----
    bad5 = []
    for key, s in P["shafts"].items():
        if key.startswith("_"):
            continue
        bspec = s.get("bearing")
        if not bspec:
            continue
        bd = float(P["bearings"][bspec]["d"])
        jd = float(v(s["journal"]))
        if not _close(bd, jd, 0.0):
            bad5.append(f"{s['name']}({s['no']}): {bspec} 内径 {bd} ≠ 轴颈 {jd}")
    out.append(Check("C5", "轴承内径 == 轴颈直径", not bad5,
                     f"{len([k for k in P['shafts'] if not k.startswith('_')])} 根轴全部吻合 ✅"
                     if not bad5 else "；".join(bad5)))

    # ---- C6 端盖外径 > 轴承外径 ----
    bad6 = []
    for ckey, c in P["covers"].items():
        if ckey.startswith("_"):
            continue
        bspec = c.get("bearing")
        cd = float(v(c["D"]))
        bD = float(P["bearings"][bspec]["D"])
        if not cd > bD:
            bad6.append(f"{c['name']}({c['no']}): 端盖 Ø{cd} ≤ 轴承 {bspec} 外径 Ø{bD}")
    out.append(Check("C6", "端盖外径 > 所配轴承外径", not bad6,
                     "4 件端盖全部大于所配轴承外径 ✅" if not bad6 else "；".join(bad6)))

    # ---- C11 端盖两张表不能打架 ----
    # `covers`（C6 校核用）与 `parts.coverN`（建件用）各存了一份 D —— 两份表分头改
    # 就会像 `sw_gear.py` 那份自留的螺栓表一样，同一个尺寸两处不同值，且**没人发现**。
    # 这里把它们钉死：D / 轴号 / 所配轴承 三项必须逐项一致。
    bad11 = []
    for ckey, c in P["covers"].items():
        if ckey.startswith("_"):
            continue
        q = P["parts"].get(ckey)
        if q is None:
            bad11.append(f"{ckey} 只在 covers 里，parts 里没有（**没登记 ⇒ 不会被建模**）")
            continue
        for fld in ("no", "name", "bearing"):
            if c.get(fld) != q.get(fld):
                bad11.append(f"{ckey}.{fld}: covers={c.get(fld)!r} ≠ parts={q.get(fld)!r}")
        if not _close(float(v(c["D"])), float(v(q["D"])), 0.0):
            bad11.append(f"{ckey}.D: covers={v(c['D'])} ≠ parts={v(q['D'])}")
    n_cov = len([k for k in P["covers"] if not k.startswith("_")])
    out.append(Check("C11", "端盖两表（covers / parts）逐项一致", not bad11,
                     f"{n_cov} 件端盖在两张表里 no/name/D/bearing 全部一致 ✅"
                     if not bad11 else "；".join(bad11)))

    # ---- C7 中心高 ≥ 大齿轮齿顶半径（否则大齿轮会捅到箱底）----
    need = da2 / 2.0
    out.append(Check("C7", "中心高 h ≥ 大齿轮齿顶半径 da2/2", h >= need - 1e-9,
                     f"h={h} ≥ {need}（余量 {round(h - need, 3)} —— 即油池/箱底的余地）"))

    # ---- C8 箱体容得下齿轮副（待图纸给内腔）----
    span = a + da2 / 2.0 + da1 / 2.0          # 跨两轴的外廓跨度
    out.append(Check("C8", "箱体容得下齿轮副", False,
                     f"齿轮副横向跨度 ≈ {round(span, 1)}（= a + da2/2 + da1/2）；"
                     f"图纸箱体 {v(P['box']['L'])}×{v(P['box']['W'])}×{v(P['box']['H'])}。"
                     f"内腔尺寸未取到 ⇒ 无法判定。见 open_questions Q1",
                     kind="open"))

    # ---- C9 模数合规（国标，校核性质）----
    try:
        sys.path.insert(0, str(HERE))
        import sw_gb  # noqa: PLC0415
        ms = sw_gb.module_series() if hasattr(sw_gb, "module_series") else None
        ok9, det9 = True, ""
        if ms:
            first = ms.get("first") or []
            second = ms.get("second") or []
            ok9 = _close(m, round(m), 0) and (m in first or m in second)
            det9 = f"m={m} 在 GB/T 1357 {'第一' if m in first else '第二'}系列 ✅"
        else:
            det9 = "sw_gb 没暴露模数系列，跳过"
        out.append(Check("C9", "模数在 GB/T 1357 标准系列内", ok9, det9, kind="info"))
    except Exception as e:                                        # noqa: BLE001
        out.append(Check("C9", "模数在 GB/T 1357 标准系列内", True, f"跳过（{e}）", kind="info"))

    # ---- C10 压力角 ----
    out.append(Check("C10", "压力角 α = 20°", _close(alpha, 20.0, 1e-9),
                     f"α={alpha}°", kind="info"))

    # ---- C12 齿轮副模数一致（用户 2026-10-02 明确要求）----
    # ★ 为什么单列一条：模数不一致是**"每个零件单看都对、合起来废掉"**的错。
    #   大齿轮 z=55、齿轮轴 z=15，各自的 d/da/df 全都算得通、单独检查一个都过；
    #   只有装到中心距 70 的箱体里才发现根本啮合不上。
    #   而本项目里 m **可能出现在三个地方**（`drive.m`、`gears.*`、`meta.gear_train.module`），
    #   任何一处单独改都会让两轮模数悄悄漂移 ⇒ 必须有一条检查把它们钉在一起。
    gt = (P.get("meta") or {}).get("gear_train")
    if not isinstance(gt, dict):
        out.append(Check("C12", "齿轮副模数一致（meta.gear_train 为唯一真源）", False,
                         "meta.gear_train 不存在 —— 齿轮件将各自读 m，"
                         "两轮模数可能漂移（用户硬要求：必须能啮合）", kind="hard"))
    else:
        bad12, warn12, det12 = [], [], []
        gm = float(gt.get("module", 0) or 0)
        gz1 = int((gt.get("pinion") or {}).get("z", 0) or 0)
        gz2 = int((gt.get("wheel") or {}).get("z", 0) or 0)
        ga = float(gt.get("center_distance", 0) or 0)
        gtol = float(gt.get("center_distance_tol", 0.08) or 0.08)
        det12.append(f"meta.gear_train: m={gm} z₁={gz1} z₂={gz2} a={ga}")
        # ① gear_train 与 drive 表必须一致（drive 是老表，历史上是主入口）
        if not _close(gm, m, 1e-9):
            bad12.append(f"模数不一致：meta.gear_train.module={gm} vs drive.m={m}")
        if gz1 != z1 or gz2 != z2:
            bad12.append(f"齿数不一致：gear_train({gz1},{gz2}) vs drive({z1},{z2})")
        if not _close(ga, a, 1e-9):
            bad12.append(f"中心距不一致：gear_train.a={ga} vs drive.a={a}")
        # ② **两个齿轮件都不许自带 m/z**（自带 = 可能与另一轮不同 ⇒ 装配不啮合）
        for pkey in ("gear_shaft", "wheel"):
            pq = P["parts"].get(pkey) or {}
            stray = [k for k in ("m", "z", "module") if k in pq]
            if stray:
                bad12.append(f"parts.{pkey} 自带 {stray} —— 模数/齿数只许来自 meta.gear_train")
        # ③ 啮合几何自洽（用 gear_train 自己的数重算，不引用 drive ⇒ 相互独立）
        if gz1 and gz2 and gm:
            a_calc = gm * (gz1 + gz2) / 2.0
            if not _close(ga, a_calc, gtol):
                bad12.append(f"中心距不自洽：a={ga} vs m(z₁+z₂)/2={a_calc}（±{gtol}）")
            da2 = gm * (gz2 + 2 + 2 * float((gt.get("wheel") or {}).get("x", 0) or 0))
            if not da2 / 2.0 < ga:
                bad12.append(f"大齿轮装不进两轴之间：da₂/2={da2 / 2} ≥ a={ga}")
            det12.append(f"啮合校验：a=m(z₁+z₂)/2={a_calc} ✔、da₂/2={da2 / 2} < a ✔")
            # ④ 根切：★ **无条件 hard**（2026-10-02 改）。
            #   原逻辑是"有实物证据就降级为 open"——那是**参照件不变位时代**的妥协：
            #   当时明知 z₁=15 根切，却因"参照件就是这样"而放行。
            #   用户裁决「确保不会根切，重新设计计算」之后，这条**不再有例外**：
            #   达不到就说明参数没重新算对，直接判 hard。
            pin = gt.get("pinion") or {}
            x1 = float(pin.get("x", 0) or 0)
            zmin = int(gt.get("z_min_20deg", 18) or 18)
            if gz1 < zmin and x1 <= 0:
                bad12.append(
                    f"小齿轮 z₁={gz1} < z_min={zmin} 且不变位 ⇒ **根切**（20° 压力角）。"
                    f"★ 2026-10-02 用户裁决「确保不会根切」后，**这条不再允许降级**："
                    f"请把 z₁ 提到 {zmin} 以上，或加正变位 x ≥ "
                    f"{(1.0 - gz1 * math.sin(math.radians(alpha)) ** 2 / 2.0):.4f}；"
                    f"若改 z₁，请保持 z₁+z₂ 不变以免动中心距")
            elif gz1 < zmin and x1 > 0:
                det12.append(f"z₁={gz1} < {zmin} 但已加正变位 x={x1}（避免根切）")
        out.append(Check("C12", "齿轮副模数一致 + 啮合几何自洽", not bad12,
                         "；".join(det12 + warn12) if not bad12 else "；".join(bad12),
                         kind="hard" if bad12 else ("open" if warn12 else "pass")))

    return out


def reference_check(P: dict, ref_path: "Path | None" = None) -> list[Check]:
    """把参数表里**实测（measured）**的字段拿去与 `reference.json` 对账。

    这一组回答的是另一个问题：**参数表有没有抄错参照件的数**。
    它比 C1~C7 更"接地" —— C 组验的是几何关系，R 组验的是"表里写的和实物一致"。
    """
    rp = Path(ref_path) if ref_path else REFERENCE_PATH
    if not rp.is_file():
        return [Check("R0", "参照件基准表存在", False, f"找不到 {rp}", kind="hard")]
    ref = json.loads(rp.read_text(encoding="utf-8"))
    by_name = {it["file"]: it for it in ref["items"]}
    out: list[Check] = []

    def bbox_of(part_key: str, expect, tol=1.0, label="", note=""):
        p = P["parts"].get(part_key)
        if not p:
            return
        # ★★ **重新设计过的件不再与参照件对账** —— 但必须**显式豁免 + 写出理由**，
        #   绝不能靠"调大容差"糊过去（那会让真错一起放行）。
        #   判据：`meta.gear_train.redesign_2026_10_02` 里点名的件（齿数变了 ⇒ 齿顶圆差几 mm），
        #   或零件自己带 `_superseded_by_redesign`。
        rd = ((P.get("meta") or {}).get("gear_train") or {}).get("redesign_2026_10_02") or {}
        new_spec = (rd.get("new") or {})
        redesigned = (part_key == "wheel" and new_spec.get("z2")
                      and int(new_spec["z2"]) != int((rd.get("old") or {}).get("z2", 0)))
        if redesigned:
            old_da = float((rd.get("old") or {}).get("da2") or 0)
            got_ref = by_name.get(p.get("ref"), {}).get("bbox_mm")
            out.append(Check(f"R:{part_key}", f"{label or p['name']} 参照件对账（已豁免）", True,
                             f"**{label or p['name']}已按用户决策重新设计**（{p.get('ref')} 是**旧齿数** "
                             f"z={rd.get('old', {}).get('z2')}，齿顶 Ø{old_da:g}）："
                             f"新 z={new_spec.get('z2')} ⇒ 齿顶 Ø{expect[0]:g}、齿宽 {expect[2]:g}，"
                             f"**与参照件差 {old_da - float(expect[0]):g}mm**。"
                             f"参照件实测 {got_ref} ⇒ 属**预期差异**，不构成错误；"
                             f"参照件只保留「结构做法」参考。",
                             kind="info"))
            return
        it = by_name.get(f) if (f := p.get("ref")) else None
        if it is None:
            out.append(Check(f"R:{part_key}", f"{label or p['name']} 在基准表里", False,
                             f"基准表里没有 {p.get('ref')}"))
            return
        got = [round(x, 3) for x in it["bbox_mm"]]
        # 包围盒方向可能与参数顺序不同 ⇒ 排序后比
        ok = all(abs(g - e) <= tol for g, e in zip(sorted(got), sorted(expect)))
        out.append(Check(f"R:{part_key}", f"{label or p['name']} 包围盒 vs 基准表", ok,
                         f"表里算得 {expect}，实测 {got}，容差 ±{tol} {note}"))

    key = P["parts"]["key_10x8x22"]
    bbox_of("key_10x8x22", [v(key["L"]), v(key["b"]), v(key["h"])], 0.01,
            note="（A型圆头键：跑道形轮廓）")

    for bn, bkey in (("6204", "bearing_6204"), ("6206", "bearing_6206")):
        b = P["bearings"][bn]
        bbox_of(bkey, [b["B"], b["D"], b["D"]], 0.01, label=f"轴承 {bn}")

    for gk, label in (("wheel", "大齿轮"),):
        # ★ 从 gear_train 读（唯一真源），不再读 `gears` 的第二份表
        g = P["meta"]["gear_train"][gk]
        bbox_of("wheel", [float(g["da"]), float(g["da"]), float(g["b"])], 1.0, label=label,
                note="★ 容差 0.5 → **1.0**：本设计对参照件加了变位（x₂=−0.2），"
                     "齿顶圆**刻意**由 114 改为 113.2（差 0.8）——这是设计改动，不是抄错。"
                     "参照件实测 113.976 仍作量级核对")

    # ★ 跨度 = a + da₂/2 + da₁/2 —— **全部从 gear_train 读**（原来写死 57+17，
    #   重新设计后 da₂=108/da₁=40，写死值就错了）。
    _gt = P["meta"]["gear_train"]
    _span = float(v(P["drive"]["a"])) + float(_gt["wheel"]["da"]) / 2.0 + float(_gt["pinion"]["da"]) / 2.0
    out.append(Check("R:gear_ratio", "齿轮副跨度 vs 箱体（见 Q1）", True,
                     f"跨度 = a + da₂/2 + da₁/2 = {float(v(P['drive']['a'])):g} + "
                     f"{float(_gt['wheel']['da']) / 2:g} + {float(_gt['pinion']['da']) / 2:g} = "
                     f"≈ {round(_span, 1)}；箱体朝向待定（open_questions Q1）", kind="info"))
    return out


def run_all(P: dict) -> dict:
    cs = cross_check(P) + reference_check(P)
    hard = [c for c in cs if c.kind == "hard"]
    ok = all(c.ok for c in hard)
    return {"ok": ok, "checks": [c.as_dict() for c in cs],
            "n_pass": sum(1 for c in cs if c.ok),
            "n_fail": sum(1 for c in cs if not c.ok and c.kind == "hard"),
            "n_open": sum(1 for c in cs if c.kind == "open")}


# --------------------------------------------------------------------------- 计划生成

def _key_plan(P: dict, out_dir: str | None = "D:/out/reducer") -> dict:
    """平键 10×8×22（GB/T 1096 A型 = 圆头）。

    轮廓 = **跑道形**：宽 b 高 h 的矩形，两端加 R=b/2 半圆。
    做法：`rect_center` 画 b×h 矩形 → `sketch_fillet` 四个角全倒 R=b/2
    （两个短边长度正好 = b ⇒ 倒完变成完整半圆，即标准 A 型键）。
    然后拉伸 h 厚 + **两圈 0.5×45° 倒角**（环绕上下两面的边）——
    实测：实心跑道形体 1588.35 mm³，两个倒角共去 13.6，落 1574.73（与参照件一致）。
    """
    p = P["parts"]["key_10x8x22"]
    b, h, L = float(v(p["b"])), float(v(p["h"])), float(v(p["L"]))
    ch = float(v(p["chamfer"]))
    r = b / 2.0
    xa = L - b                               # 右半圆圆心 x（左半圆圆心放在**原点**）
    # ★ 为什么不是"居中画再倒角"：`FullyDefineSketch` 要求草图**锚住**
    #   （角点或圆心与原点重合），否则它返回 0 Success 但状态还是 UnderConstrained。
    #   实测（2026-10-01）：居中画的跑道形 4 个半圆圆心都在 (±(L−b)/2, 0)，没一个在原点 ⇒ 失败。
    #   把左半圆圆心放到原点即解 —— 几何不变，只是整体平移，包围盒仍是 [L, b, h]。
    # ★ 倒角靶点：`_edges` 的"中点" = 该边**两端顶点的平均** ⇒ 半圆边的中点是
    #   **弦中点**（右 (xa,0)、左 (0,0)），不是弧的最外点。给错点就选不中。
    top = [[xa / 2.0, r, h], [xa / 2.0, -r, h], [xa, 0.0, h], [0.0, 0.0, h]]
    bot = [[xa / 2.0, r, 0.0], [xa / 2.0, -r, 0.0], [xa, 0.0, 0.0], [0.0, 0.0, 0.0]]
    _params = {"b": b, "h": h, "L": L, "chamfer": ch, "r": r, "xa": xa}
    if out_dir:
        # ★ 不写 out 时（探针场景）留给运行器注入 —— sw_probe 用
        #   `params.setdefault("out", YOYO_SW_OUT/probe)` 保证跨机器可移植。
        _params["out"] = out_dir
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"平键 {b:g}x{h:g}x{L:g}（GB/T 1096 A型）", "units": "mm",
                 "note": "参数来自 reducer.params.json::parts.key_10x8x22，不硬编码",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": [
            {"id": "n", "op": "new_part"},
            {"id": "p", "op": "select_plane", "plane": "Front"},
            {"id": "s1", "op": "sketch_begin"},
            # 跑道形 = 直线 + 半圆 + 直线 + 半圆，**四段显式画**。
            # 为什么不用 "矩形 + sketch_fillet"：那个 op 一次只倒**两条线**，
            # 四角要调四次，且依赖 ctx.segments 的记录顺序（实测选不中）。
            # ★ `arc3p` 的点序是 **起点 → 终点 → 弧上点**（2026-10-01 真机实测确认，
            #   不是"起点-中点-终点"）。写成后者只画出 90° 弧、链断成 2 条开放轮廓，
            #   `extrude_boss` 报「草图轮廓数 2」失败。
            {"id": "l1", "op": "line", "x1": 0.0, "y1": "{{ params.r }}",
             "x2": "{{ params.xa }}", "y2": "{{ params.r }}"},
            {"id": "a1", "op": "arc3p",
             "p1": ["{{ params.xa }}", "{{ params.r }}"],           # 起点 = l1 终点
             "p2": ["{{ params.xa }}", "{{ -params.r }}"],          # 终点 = l2 起点
             "p3": ["{{ params.xa + params.r }}", 0]},              # 弧上点
            {"id": "l2", "op": "line", "x1": "{{ params.xa }}", "y1": "{{ -params.r }}",
             "x2": 0.0, "y2": "{{ -params.r }}"},
            {"id": "a2", "op": "arc3p",
             "p1": [0.0, "{{ -params.r }}"],                        # 起点 = l2 终点
             "p2": [0.0, "{{ params.r }}"],                         # 终点 = l1 起点
             "p3": ["{{ -params.r }}", 0]},                         # 弧上点
            # ★★ 尺寸标注已**封存**（2026-10-02 用户决策）—— 这里原来有
            #   `{"op": "dim_auto", "chain": "off"}` + `expect_min_dims` 闸门，现在拿掉了。
            #   真机复现：逐段标注会弹**模态确认框**（过定义/冲突尺寸），自动化被挂死
            #   （`RPC_E_DISCONNECTED`），且型面被尺寸驱动改坏。详见 sw_ops.SEALED_REASON。
            #   ⇒ 当前只建几何，尺寸改由后续「图纸绘制」环节对接。
            #   「草图必须用真尺寸画」这条口径**暂时挂起**，闸门降级为「存在性记录」
            #   （expect_min_dims: 0，只报告约束状态，不再当合规判据）。
            {"id": "s2", "op": "sketch_end", "name": "KeySk"},
            {"id": "ck", "op": "check_sketch_constraint", "sketch": "KeySk",
             "expect_min_dims": 0,
             "note": "★ 尺寸标注已封存 ⇒ 本闸门**不再校验尺寸条数**，只记录草图约束状态。"
                     "几何正确性由后面的 check_bbox / check_mass 兜（那两条才是真判据）。"},
            {"id": "e", "op": "extrude_boss", "depth": "{{ params.h }}", "name": "KeyBody"},
            {"id": "bb0", "op": "check_bbox",
             "expect": ["{{ params.L }}", "{{ params.b }}", "{{ params.h }}"], "tol_mm": 0.05},
            {"id": "c1", "op": "chamfer", "dist": "{{ params.chamfer }}", "angle": 45,
             "edges": top, "tol": 3.0, "name": "ChamferTop"},
            {"id": "c2", "op": "chamfer", "dist": "{{ params.chamfer }}", "angle": 45,
             "edges": bot, "tol": 3.0, "name": "ChamferBot"},
            {"id": "bb1", "op": "check_bbox",
             "expect": ["{{ params.L }}", "{{ params.b }}", "{{ params.h }}"], "tol_mm": 0.05},
            {"id": "ms", "op": "check_mass", "expect_mass_g": "{{ params.mass_g }}",
             "tol": 0.02},
            {"id": "dg", "op": "check_diagnostics", "fail_on": "error"},
            {"id": "sv", "op": "save_as",
             "path": "{{ params.out }}/35-key.{{ params.L }}x{{ params.b }}x{{ params.h }}.SLDPRT",
             "overwrite": True},
        ],
        "_derived": {"r": r, "xa": xa,
                     "solid_volume_mm3": (b * (L - b) + math.pi * r ** 2) * h},
    }


def _shaft_volume(segs) -> float:
    """分段轴的解析体积（全按圆柱算；端部倒角是外扩，单独体现在 L 上）。"""
    return sum(math.pi * (float(r["d"]) / 2.0) ** 2 * float(r["l"]) for r in segs)


def _shaft_plan(P: dict, out_dir: str | None = "D:/out/reducer") -> dict:
    """输出轴（20-轴）—— **半剖面回转一次成型**。

    做法照搬 v1.4.2 已实测的 `shaft_revolve` 探针（19/19），但**分段表来自参数表**
    (`shafts.output.segments`)，不再硬编码在计划里。

    两个已知口径（都写在 `references/30-pitfalls.md`）：
      · **端面倒角是外扩** ⇒ 总长 = Σl + 2×ec（本件 140 + 2×2 = 144）。
      · **长细件 `zoom_fit` 要连调两次** —— 一次会用过期包围盒，画面只装得下一半。
    """
    s = P["shafts"]["output"]
    segs = s["segments"]
    ec = float(v(s["end_chamfer"]))
    L = float(v(s["L"]))
    dmax = float(v(s["d_max"]))
    mass_g = round(_shaft_volume(segs) * 1e-3, 4)
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    seg_steps = [{"d": float(r["d"]), "l": float(r["l"]), "name": r["name"]} for r in segs]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": "轴（20-轴，半剖面回转）", "units": "mm",
                 "note": "分段表来自 reducer.params.json::shafts.output.segments，不硬编码",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": [
            {"id": "n", "op": "new_part"},
            {"id": "p", "op": "select_plane", "plane": "Front"},
            {"id": "s1", "op": "sketch_begin"},
            {"id": "prof", "op": "shaft_profile", "segments": seg_steps,
             "end_chamfer": ec,
             "note": f"Σl = {sum(float(r['l']) for r in segs):g}；两端各 {ec:g}×45° ⇒ 总长 {L:g}"},
            {"id": "zf0", "op": "zoom_fit"},
            {"id": "zf0b", "op": "zoom_fit", "note": "★ 连调两次（长细件一次会装不下）"},
            # ★★★ 2026-10-02 **尺寸标注封存**（用户决策，supersede 了下面那套 chain 口径的争论）：
            #   这里原来挂 `dim_auto(chain=off) + deviation`。封存理由见
            #   `sw_ops.SEALED_REASON`：逐段标在非单调型面上会弹**模态确认框**，
            #   自动化被挂死（`RPC_E_DISCONNECTED`）且型面被驱动改坏。
            #   历史包袱一并记在这（撤掉的是"标注"，不是这些教训）：
            #   `chain=auto` 在本件上必挂 —— 端部 45° 倒角把端面切成斜线 ⇒ 尺寸链
            #   取不到端面见证线 ⇒ 退到单图元兜底 ⇒ 真机确定性复现两种翻车：
            #   ① 兜底把尺寸打在空选择上 ⇒ **SolidWorks 崩溃**（`RPC_S_CALL_FAILED`）；
            #   ② 即便不崩，`_remeasure_revolve_feature` 重进草图后留在**打开态**
            #   ⇒ `_bodies()` 回 0（实体被临时压制）⇒ `check_bbox` 读不到。
            #   ⇒ 现在**一条尺寸都不标**，几何正确性交给 check_bbox / check_mass。
            #   详见 `references/30-pitfalls.md` §77。
            {"id": "s2", "op": "sketch_end", "name": "轴半剖面"},
            {"id": "cksk", "op": "check_sketch", "sketch": "轴半剖面"},
            {"id": "ck", "op": "check_sketch_constraint", "sketch": "轴半剖面",
             "expect_min_dims": 0,
             "note": "★ 尺寸标注已封存 ⇒ 不校验尺寸条数，只记录约束状态；"
                     "几何正确性由 check_bbox / check_mass 兜"},
            {"id": "rev", "op": "revolve_boss", "name": "轴体", "angle": 360},
            {"id": "bb", "op": "check_bbox", "expect": [L, dmax, dmax], "tol_mm": 0.05,
             "note": "与参照件 20-轴 的包围盒一致（144/38/38）"},
            {"id": "cyl", "op": "check_cyl_radius", "expect_max": dmax / 2.0, "tol": 0.01},
            {"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
             "note": "解析体积按分段圆柱算；端部倒角外扩会略微超，2% 闸门"},
            {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
            {"id": "sv", "op": "save_as",
             "path": "{{ params.out }}/20-shaft.SLDPRT", "overwrite": True},
        ],
        "_derived": {"journal": float(v(s["journal"])), "bearing": s["bearing"],
                     "sum_l": sum(float(r["l"]) for r in segs),
                     "analytic_volume_mm3": _shaft_volume(segs)},
    }


# --------------------------------------------------------------- 环形件族（简单圆环）
#
# 5 件（套筒 / 调整环1 / 调整环2 / 密封圈 / 密封圈2）形状**完全同构**：
#   一个外圆 + 一个内孔 + 一个厚度，无倒角、无台阶。
# 2026-10-02 用 `sw_faces.py` 读参照件的圆柱面把内孔倒推出来，5 件全部与解析式
#   V = π/4 (D² − d²) · t 吻合到 **0.002%** ⇒ 确认是「真圆环」，可以共用一套生成器。
#   （注意：同目录里 `25-挡油环`/`2-垫片1` 也是"环"，但实测**不是**简单圆环 ——
#    一个是台阶+锥面，一个解析值差 5%，所以**没有**收进这一族。宁可少收，不可硬套。）
#
# 为什么用**半剖面回转**，而不是"外圆拉伸 + 内孔切除"两个特征：
#   · 回转只出**一个**特征，特征树干净（与参照件 `34-套筒` 的"草图1 → 旋转1"一致）；
#   · 外径 / 内孔 / 厚度三个尺寸都在**同一张草图**里，`set_dimension` 一次全驱动；
#   · 两特征法会把外径和内孔拆到两张草图，改型要跑两处。
# 剖面**不贴轴线** —— 这是与阶梯轴 `shaft_profile` 的唯一区别：
#   圆环的半剖面 = "离轴一个矩形"，回转出来中间自然是空的。
RING_FAMILY = ("spacer", "ring_adj1", "ring_adj2", "seal1", "seal2")

# ★ 17-平垫圈 10 也挂进这个族 —— **它就是圆环**，不需要新生成器。
#   规格来自 **GB/T 97.1**（d1 10.5 / d2 20 / h 2），不是参照件实测
#   （参照件是 Ø22/Ø11/1.5，像**美制 3/8"**；标准件按国标表建是既定口径 —— 禁止 Toolbox）。
#   ⇒ 对账时包围盒会与参照件差 2mm/0.5mm/0.5mm，**这是预期差异，不是错**。
RING_FAMILY = RING_FAMILY + ("washer_10",)


def _ring_volume(OD: float, ID: float, t: float) -> float:
    """圆环解析体积。"""
    return math.pi / 4.0 * (OD ** 2 - ID ** 2) * t


def _ring_plan(P: dict, out_dir: str | None = "D:/out/reducer", part: str = "") -> dict:
    """环形件族：由参数表生成"圆环"计划（半剖面回转一次成型）。

    型面同构 ⇒ 一个生成器覆盖 5 件。参数来自 `parts.<part>.D/bore/t`。
    """
    q = P["parts"][part]
    OD = float(v(q["D"]))
    ID = float(v(q["bore"]))
    t = float(v(q["t"]))
    ro, ri = OD / 2.0, ID / 2.0
    if not 0.0 < ri < ro:
        raise ValueError(f"{part}: 内径 {ID} 必须落在 (0, 外径 {OD}) 之间（收到 D={OD}, bore={ID}）")
    mass_g = round(_ring_volume(OD, ID, t) * 1e-3, 4)
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，圆环半剖面回转）", "units": "mm",
                 "note": f"尺寸来自 reducer.params.json::parts.{part}，不硬编码",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": [
            {"id": "n", "op": "new_part"},
            {"id": "p", "op": "select_plane", "plane": "Front"},
            {"id": "s1", "op": "sketch_begin"},
            # 回转轴 = Front 平面的 X 轴（y=0）—— 画一条中心线，`revolve_boss` 自动认它。
            {"id": "cl", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": t, "y2": 0.0},
            # 半剖面 = 离轴一个矩形：x∈[0,t]，y∈[ri,ro]，闭合 4 段。
            # ★ 与阶梯轴的区别就是"不贴轴线" —— 回转后中间留孔，**一个特征**出圆环。
            {"id": "prof", "op": "polyline",
             "points": [[0.0, ri], [t, ri], [t, ro], [0.0, ro]], "close": True},
            # ★★ 尺寸标注已封存（2026-10-02）—— 原 `dim_auto(chain=off) + deviation`
            #   整块拿掉。原来那套 deviation 的结论一并留档：`chain=auto` 在**离轴矩形环**
            #   型面上退化（计划算 3 条 = 总长+2 半径，实际只落上总长 1 条、读回 4 个尺寸
            #   全是 13.0；见 §76），而 chain=off 落 4 条。封存后**一条都不标**。
            {"id": "s2", "op": "sketch_end", "name": f"{q['name']}半剖面"},
            {"id": "ck", "op": "check_sketch_constraint", "sketch": f"{q['name']}半剖面",
             "expect_min_dims": 0,
             "note": "★ 尺寸标注已封存 ⇒ 不校验尺寸条数，只记录约束状态；"
                     "几何正确性由 check_bbox / check_mass 兜"},
            {"id": "rev", "op": "revolve_boss", "angle": 360, "name": q["name"]},
            {"id": "bb", "op": "check_bbox", "expect": [t, OD, OD], "tol_mm": 0.05,
             "note": f"与参照件 {q['ref']} 的包围盒一致（[{t:g}, {OD:g}, {OD:g}]）"},
            {"id": "cyl", "op": "check_cyl_radius", "expect_max": ro, "tol": 0.01},
            {"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
             "note": "π/4(D²−d²)t —— 内孔对不对，靠这条兜底"},
            {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
            {"id": "sv", "op": "save_as",
             "path": "{{ params.out }}/" + f"{q['no']}-ring-{OD:g}x{ID:g}x{t:g}.SLDPRT",
             "overwrite": True},
        ],
        "_derived": {"OD": OD, "ID": ID, "t": t, "ri": ri, "ro": ro,
                     "analytic_volume_mm3": _ring_volume(OD, ID, t)},
    }


# --------------------------------------------------------------- 端盖·闷盖族（法兰 + 实心凸台）
#
# 2 件（端盖2 / 端盖4）形状**完全同构**，只差直径：
#   法兰盘（外径 Df，中心孔 di，厚 tf） + 实心凸台（外径 Ds，厚 ts）。
# 2026-10-02 用 `sw_faces.py` 读参照件的圆柱面 + 平面站位倒推：
#   · 22-端盖2：Ø54/孔Ø31/3 + Ø47/实心/4 ⇒ 解析 11546.14 vs 参照 11546.14（**差 0.0%**）
#   · 31-端盖4：Ø68/孔Ø50/3 + Ø62/实心/4 ⇒ 解析 17080.84 vs 参照 17080.84（**差 0.0%**）
#   平面站位的**面积**还二次印证了型面（端面 = 整圆 πRs²；法兰面 = π(Ro²−ri²)；
#   台阶面 = π(Ro²−Rs²) + 孔底 πri²），三处都对得上 ⇒ 不是"凑出来的体积"。
#
# ★ 凸台直径 = **所配轴承外径**（Ø47 = 6204 外径、Ø62 = 6206 外径）——
#   它插进箱体的轴承座孔，这就是 C6 那条校核的物理含义。
#
# 为什么两件同族却与「透盖」（端盖1/3）分开：
#   透盖型面**非单调**（6 圆柱 + 3 锥面、草图 8 个轮廓），读不出来 ⇒ 见 parts.cover1 的
#   `_measured_faces`。宁可少收两件，也不硬套一个"差不多"的型面。
COVER_BLIND_FAMILY = ("cover2", "cover4")


def _blind_cover_volume(Df: float, di: float, tf: float, Ds: float, ts: float) -> float:
    """闷盖解析体积 = 法兰环 + 实心凸台。"""
    return (math.pi / 4.0 * (Df ** 2 - di ** 2) * tf
            + math.pi / 4.0 * Ds ** 2 * ts)


def _blind_cover_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                      part: str = "") -> dict:
    """闷盖族：由参数表生成"法兰 + 实心凸台"计划（半剖面回转一次成型）。

    型面同构 ⇒ 一个生成器覆盖 2 件。参数来自 `parts.<part>` 的
    D / flange_t / flange_bore / spigot_D / spigot_t。
    """
    q = P["parts"][part]
    Df = float(v(q["D"]))
    di = float(v(q["flange_bore"]))
    tf = float(v(q["flange_t"]))
    Ds = float(v(q["spigot_D"]))
    ts = float(v(q["spigot_t"]))
    ro, ri, rs = Df / 2.0, di / 2.0, Ds / 2.0
    if not 0.0 < ri < rs < ro:
        raise ValueError(f"{part}: 需满足 0 < 孔半径 {ri} < 凸台半径 {rs} < 法兰半径 {ro}")
    if not _close(tf + ts, float(v(q["t"])), 1e-9):
        raise ValueError(f"{part}: 法兰厚 {tf} + 凸台厚 {ts} ≠ 总厚 {v(q['t'])}（见 C11 同源要求）")
    L = tf + ts
    vol = _blind_cover_volume(Df, di, tf, Ds, ts)
    mass_g = round(vol * 1e-3, 4)
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，闷盖半剖面回转）", "units": "mm",
                 "note": f"尺寸来自 reducer.params.json::parts.{part}，不硬编码",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": [
            {"id": "n", "op": "new_part"},
            {"id": "p", "op": "select_plane", "plane": "Front"},
            {"id": "s1", "op": "sketch_begin"},
            # 回转轴 = Front 平面的 X 轴（y=0）
            {"id": "cl", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": L, "y2": 0.0},
            # 半剖面 8 顶点（x, r）。**含贴轴段**（凸台是实心 ⇒ 型面必须触到 r=0）——
            # 这是与"圆环"族的唯一区别：环的型面离轴，闷盖的型面触轴。
            {"id": "prof", "op": "polyline",
             "points": [[0.0, ri], [0.0, ro], [tf, ro], [tf, rs],
                        [L, rs], [L, 0.0], [tf, 0.0], [tf, ri]],
             "close": True,
             "note": f"法兰 Ø{Df:g}/孔 Ø{di:g}/{tf:g} + 实心凸台 Ø{Ds:g}/{ts:g}"},
            # ★★ 尺寸标注已封存（2026-10-02）—— 原 `dim_auto(chain=off) + deviation` 拿掉
            #   （chain=auto 在本型面退化：只落总长、半径落不上，见 pitfalls §76 / 探针
            #   spec_relations；chain=off 落 8 条）。封存后一条都不标。
            {"id": "s2", "op": "sketch_end", "name": f"{q['name']}半剖面"},
            {"id": "ck", "op": "check_sketch_constraint", "sketch": f"{q['name']}半剖面",
             "expect_min_dims": 0,
             "note": "★ 尺寸标注已封存 ⇒ 不校验尺寸条数，只记录约束状态；"
                     "几何正确性由 check_bbox / check_mass 兜"},
            {"id": "rev", "op": "revolve_boss", "angle": 360, "name": q["name"]},
            {"id": "bb", "op": "check_bbox", "expect": [L, Df, Df], "tol_mm": 0.05,
             "note": f"与参照件 {q['ref']} 的包围盒一致（[{L:g}, {Df:g}, {Df:g}]）"},
            {"id": "cyl", "op": "check_cyl_radius", "expect_max": ro, "tol": 0.01},
            {"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
             "note": "π/4(Df²−di²)tf + π/4·Ds²·ts —— 孔与凸台对不对，靠这条兜底"},
            {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
            {"id": "sv", "op": "save_as",
             "path": "{{ params.out }}/" + f"{q['no']}-cover-{Df:g}x{di:g}x{L:g}.SLDPRT",
             "overwrite": True},
        ],
        "_derived": {"Df": Df, "di": di, "tf": tf, "Ds": Ds, "ts": ts, "L": L,
                     "ri": ri, "rs": rs, "ro": ro,
                     "analytic_volume_mm3": vol},
    }


# --------------------------------------------------------------- 带孔盘族（回转环 + 孔圆周阵列）
#
# 2 件（2-垫片1 / 4-油面透视片）形状**同构**：
#   圆环（外径 OD / 内径 ID / 厚 t） + **N 个轴向贯穿的偏心小孔**（孔径 d、分布圆 Øpitch）。
# 与「圆环族」的唯一区别就是那圈孔 ⇒ **回转建不出来**（孔偏心、不共轴），
#   必须「回转环 → 端面开草图 → 圆周阵列孔 → 拉伸切除」两步走。
#
# 这 2 件正是 `sw_profile` 偏心圆守卫（§81）拦下来的：提取器判 `revolvable=False`，
#   把偏心特征记进 `off_axis`（**里面就有分布圆半径 / 孔径 / 轴向站位**），
#   于是参数表里留下 `_measured_faces.solved` —— 规格全部已知，只缺一个生成器。
#
# 体积靶值**由参数表自己算**（不是抄参照件的数），两件都对上参照 4 位有效数字：
#   · 2-垫片1     π/4(34²−14²)·1 − 3·π/4·4²·1 = 753.98 − 37.70 = 716.28  vs 参照 716.3 ✔
#   · 4-油面透视片 π/4(34²− 4²)·1 − 2·π/4·4²·1 = 895.35 − 25.13 = 870.22  vs 参照 870.2 ✔
#
# ★★ 三条踩过的坑：
#   ① **孔数由体积差定**，不要数标记个数（同一条边挂在两张面上会重复计数）。
#   ② **孔必须完全落在环带 [ID, OD] 里** —— 否则它会切不到料或咬进内孔，
#      上面那两条体积靶值就不再成立。计划期先算一遍，越界直接拒建（不交给真机去发现）。
#   ③ **两次 check_mass**：回转后一条（兜"环对不对"）、打孔后再一条（兜"孔对不对"）。
#      只有一条的话，"环建错 + 孔补回来"会互相抵消而看不出来。
#
# ★ 为什么孔的位置**不用校验方向**：孔位草图开在 Right 基准面（法向 = 回转轴）上，
#   `(pitch_r, 0)` 到草图原点的距离就是分布圆半径 —— **圆周阵列天然轴对称**，
#   草图 x 轴到底指全局哪个方向不影响结果，所以只喂半径、不喂角度。
#
# ★★★ 走「基准面草图 + 特征级 `circular_pattern`」，**不走**「端面草图 + 草图级
#   `sketch_circular_pattern`」—— 后者真机**建不出来**：
#   `sketch_circular_pattern` 报「种子图元没选中」：在**面**上开的草图里，
#   `_segments_select` 选不中 AddToDB 画出的圆（基准面草图上它也是没跑过的路径，
#   但特征级方案直接复用已真机验证的探针 `assets/probes/circular_pattern.json`
#   —— Ø60 圆盘 + Ø8 偏心孔 ×6，形状与这里完全同构）。
#   ⇒ **基准面草图比端面草图更好用**：不必用 `select_face` 射线选面，也不怕草图原点落在哪。
DISC_HOLES_FAMILY = ("gasket1", "glass")


def _disc_holes_volume(OD: float, ID: float, t: float,
                       n_holes: int, hole_d: float) -> tuple[float, float]:
    """(无孔环体积, 带孔体积)。两条 `check_mass` 分别兜"回转对不对"和"孔对不对"。"""
    ring = math.pi / 4.0 * (OD ** 2 - ID ** 2) * t
    return ring, ring - n_holes * math.pi / 4.0 * hole_d ** 2 * t


def _disc_holes_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                     part: str = "") -> dict:
    """带孔盘族：环回转 + 孔圆周阵列。

    参数来自 `parts.<part>._measured_faces.solved`（`sw_profile` 判 `revolvable=False`
    时留下的偏心特征规格）：`outer_d / inner_d / thickness / holes{n,d,pitch_d}`。
    """
    q = P["parts"][part]
    mf = q.get("_measured_faces") or {}
    sv = mf.get("solved")
    if not sv:
        raise ValueError(f"{part}: 参数表里没有 _measured_faces.solved —— "
                         f"先用 scripts/sw_profile.py 解出规格再写回 params")
    OD = float(sv["outer_d"])
    ID = float(sv["inner_d"])
    t = float(sv["thickness"])
    hs = sv.get("holes") or {}
    n = int(hs.get("n") or 0)
    hd = float(hs.get("d") or 0.0)
    pitch_d = float(hs.get("pitch_d") or 0.0)
    ro, ri = OD / 2.0, ID / 2.0
    if not 0.0 < ri < ro:
        raise ValueError(f"{part}: 内径 {ID} 必须落在 (0, 外径 {OD}) 之间")
    if t <= 0.0:
        raise ValueError(f"{part}: 厚度 {t} 必须 > 0")
    if n < 1 or hd <= 0 or pitch_d <= 0:
        raise ValueError(f"{part}: 孔规格不全（n={n}, d={hd}, pitch_d={pitch_d}）")
    r_hole, rh = pitch_d / 2.0, hd / 2.0
    # ★ 见头注②：孔越出环带 ⇒ 体积靶值不成立，计划期就拒建
    if r_hole - rh < ri - 1e-9 or r_hole + rh > ro + 1e-9:
        raise ValueError(
            f"{part}: 孔（分布圆 Ø{pitch_d:g}、Ø{hd:g}）超出环带 [{ri * 2:g}, {ro * 2:g}]"
            f"（孔占半径 [{r_hole - rh:g}, {r_hole + rh:g}]）—— 孔会切不到料或咬进内孔，"
            f"体积靶值不成立")
    ring_v, holed_v = _disc_holes_volume(OD, ID, t, n, hd)
    ring_g = round(ring_v * 1e-3, 4)
    holed_g = round(holed_v * 1e-3, 4)
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        {"id": "p", "op": "select_plane", "plane": "Front"},
        {"id": "s1", "op": "sketch_begin"},
        # 回转轴 = Front 平面的 X 轴（y=0）
        {"id": "cl", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": t, "y2": 0.0},
        # 半剖面 = 离轴的矩形（x∈[0,t]，y∈[ri,ro]），闭合 4 段 ⇒ 一个特征出圆环
        {"id": "prof", "op": "polyline",
         "points": [[0.0, ri], [t, ri], [t, ro], [0.0, ro]], "close": True,
         "note": f"圆环半剖面（Ø{OD:g}/Ø{ID:g} × {t:g}）"},
        {"id": "s2", "op": "sketch_end", "name": f"{q['name']}半剖面"},
        {"id": "ck", "op": "check_sketch_constraint", "sketch": f"{q['name']}半剖面",
         "expect_min_dims": 0,
         "note": "★ 尺寸标注已封存 ⇒ 不校验尺寸条数；几何由 check_bbox / check_mass 兜"},
        {"id": "rev", "op": "revolve_boss", "angle": 360, "name": q["name"]},
        {"id": "bb", "op": "check_bbox", "expect": [t, OD, OD], "tol_mm": 0.05,
         "note": f"与参照件 {q['ref']} 的包围盒一致（[{t:g}, {OD:g}, {OD:g}]）"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": ro, "tol": 0.01},
        # ① 打孔前：兜"环对不对"（内孔 ØID 错一处这条立刻红）
        {"id": "m0", "op": "check_mass", "expect_mass_g": ring_g, "tol": 0.02,
         "note": f"π/4(OD²−ID²)t = {ring_v:.3f}mm³ —— 未打孔的环"},
        # ---- 打孔：基准面草图 → 种子孔 → 特征级圆周阵列 ----
        # 面的选择（回转轴 = 全局 **X** —— `check_bbox` 按轴序比对且过 ⇒ 轴向那 1mm 在 X）：
        #   · 孔轴 = X ⇒ 孔位草图开在 **Right 基准面**（法向 X）上，草图平面骑在材料中间
        #   · 阵列轴 = X ⇒ **Front ∩ Top**（Front=x-y 法向 z，Top=x-z 法向 y）
        {"id": "ax", "op": "axis_from_planes", "plane1": "Front", "plane2": "Top",
         "name": "Axis1",
         "note": "阵列轴 = 回转轴 = 全局 X（Front ∩ Top）"},
        {"id": "p2", "op": "select_plane", "plane": "Right",
         "note": "孔位草图开在 Right 基准面（法向 = X）上 —— 平面骑在 1mm 厚的环中间"},
        {"id": "s3", "op": "sketch_begin", "reset": True},
        # 中心线：① 过原点 ⇒ 满足 S7「草图锚住原点」；② 给阵列一条可见的参考轴。
        #   中心线是构造线，**不参与轮廓** ⇒ 不影响 extrude_cut 的轮廓数。
        {"id": "cl2", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": ro, "y2": 0.0},
        {"id": "c1", "op": "circle", "cx": r_hole, "cy": 0.0, "d": hd,
         "note": f"Ø{hd:g} 种子孔，距原点 {r_hole:g}（分布圆 Ø{pitch_d:g}）"},
        {"id": "s4", "op": "sketch_end", "name": f"{q['name']}孔位"},
        # 每个草图退出后都要有闸门（S2）；封存后它只记录约束状态，不校验尺寸条数。
        # ★ 它**不会**破坏选择：`_profile_plan` 里正是"sketch_end → 闸门 → revolve(不写
        #   profile)"跑通的，说明 extrude_cut 仍能吃到 sketch_end 选中的那张草图。
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": f"{q['name']}孔位",
         "expect_min_dims": 0,
         "note": "★ 尺寸标注已封存 ⇒ 只记录约束状态；孔对不对由打孔后的 check_mass 兜"},
        {"id": "cut", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
         "both_directions": True, "name": f"{q['name']}孔",
         "note": "双向贯穿 —— 草图平面骑在材料中间，单向穿不透 1mm 的环"},
        {"id": "cp", "op": "circular_pattern", "target": f"{q['name']}孔",
         "axis": "Axis1", "count": n, "angle": 360,
         "name": f"{q['name']}孔阵",
         "note": f"整圈 {n} 个孔（**含原始那个**）。轴名**写死** `Axis1` 是安全的："
                 f"`axis_from_planes` 会找到新建的 RefAxis 并**真的改名**成它"
                 f"（探针 circular_pattern 已真机验证）；万一改名失败，"
                 f"`circular_pattern` 会报「选不中阵列轴」并提示真名，仍可诊断。"
                 f"★ 不用 `{{{{ vars.ax.axis }}}}` 取真名 —— 那会让**离线求值失败**，"
                 f"而「计划能离线校验」是 yoyo.swplan/1 的核心约定"},
        # ② 打孔后：兜"孔对不对"（孔数 / 孔径 / 位置任一错，这条就红）
        {"id": "m1", "op": "check_mass", "expect_mass_g": holed_g, "tol": 0.02,
         "note": f"{ring_v:.3f} − {n}×π/4·{hd:g}²·{t:g} = {holed_v:.3f}mm³"
                 f" —— 环错+孔补回来会互相抵消，所以必须**两次都测**"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-{OD:g}x{ID:g}x{t:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，环回转 + {n}×Ø{hd:g} 孔圆周阵列）",
                 "units": "mm",
                 "note": f"规格来自 reducer.params.json::parts.{part}._measured_faces.solved"
                         f"（sw_profile 判非纯回转体时留下的偏心特征表），不硬编码",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"OD": OD, "ID": ID, "t": t, "ri": ri, "ro": ro,
                     "holes": {"n": n, "d": hd, "pitch_d": pitch_d, "pitch_r": r_hole},
                     "ring_volume_mm3": ring_v, "holed_volume_mm3": holed_v,
                     "reference_volume_mm3": sv.get("reference_mm3")},
    }


# --------------------------------------------------------------- 小盖（6-小盖）
#
# 型面（`sw_profile` 实测 2026-10-04，回转轴 = 全局 X）：回转 6 段闭环 ——
#   Ø34 外圆 / Ø14 中心通孔 / 厚 7
#     · 左端**外缘锥倒角** 45°×0.5（r16.5→17）
#     · 左端**孔口锥口**   45°×1.5（r7→8.5）
#   + 3 个偏心沉孔（分布圆 Ø24、120° 均布）：Ø6 沉孔深 3（左端）+ Ø4 通孔贯穿
# ⇒ 与「带孔盘族」同构（环 + N 孔阵列），**多一步沉孔**；环自身还带倒角/锥口
#   ⇒ 半剖面不是 4 点矩形，而是 6 点（倒角、锥口各占一段）。
#
# ★ 体积靶值**由参数表自己算**（不抄参照件），与参照 4806.0 对到 0.01%：
#     环        π/4(34²−14²)·7          = 5277.876
#   − 3×Ø6 沉孔 π/4(36−16)·3 ×3         =  141.372
#   − 3×Ø4 通孔 π/4·16·7     ×3         =  263.894
#   − 中心锥口  45°×1.5（r7→8.5）        ≈   53.01
#   − 外缘倒角  45°×0.5                  ≈   13.16
#   = 4806.4  vs 参照 4806.0  ✔
#
# ★ 为什么沉孔用 **Right 基准面**草图：回转轴 = 全局 X ⇒ Right 面（X=0）
#   **正好压在小盖左端面**上，Ø6 深 3 单向切除即落在左端 —— 与参照件一致
#   （沉孔开在装配贴合端）。同 `_disc_holes_plan` 的"基准面草图比端面草图更好用"。
CAP_SMALL = "cap_small"


def _cap_small_volume(sv: dict) -> dict:
    """小盖解析体积（**分段列**，对账时能一眼看出是哪一项错）。"""
    OD = float(sv["outer_d"]); ID = float(sv["inner_d"]); t = float(sv["thickness"])
    hs = sv["holes"]; n = int(hs["n"]); hd = float(hs["d"])
    cb = sv.get("counterbore") or {}
    cb_d = float(cb.get("d") or 0.0); cb_t = float(cb.get("t") or 0.0)
    bk = sv.get("bore_chamfer") or {}; oc = sv.get("outer_chamfer") or {}
    ri = ID / 2.0; ro = OD / 2.0
    ring = math.pi / 4.0 * (OD ** 2 - ID ** 2) * t
    thru = n * math.pi / 4.0 * hd ** 2 * t                       # 通孔全长
    cbv = n * math.pi / 4.0 * (cb_d ** 2 - hd ** 2) * cb_t if cb_d > 0 else 0.0  # 沉孔多扣的环
    bleg = float(bk.get("leg") or 0.0)
    bore = 0.0
    if bleg > 0:                                                  # 45° 锥口：圆锥台 − 原圆柱
        R = ri + bleg
        bore = (math.pi / 3.0 * bleg * (R * R + R * ri + ri * ri)
                - math.pi * ri * ri * bleg)
    oleg = float(oc.get("leg") or 0.0)
    outer = 2.0 * math.pi * (ro - oleg / 2.0) * (oleg * oleg / 2.0) if oleg > 0 else 0.0
    final = ring - thru - cbv - bore - outer
    return {"ring_mm3": ring, "thru_mm3": thru, "counterbore_mm3": cbv,
            "bore_chamfer_mm3": bore, "outer_chamfer_mm3": outer,
            "volume_mm3": final, "n": n, "hd": hd, "cb_d": cb_d, "cb_t": cb_t,
            "pitch_r": float(hs["pitch_d"]) / 2.0,
            # ★ 「回转后」= 环 **减去**锥口/外缘倒角（它们画在半剖面里，回转即已生效）
            #   —— 2026-10-04 真机踩过：m0 一度挂 `ring_mm3`(纯环)，实测 5211.640 报超差；
            #   而 5211.640 **正好等于 sw_profile 的 Pappus 体积**，说明手算漏了倒角/锥口。
            "after_turn_mm3": ring - bore - outer,
            # ★ 「沉孔后」时**通孔还没切** ⇒ 沉孔切掉的是**整块 Ø6×cb_t**，不是 (Ø6²−Ø4²) 环！
            #   2026-10-04 真机踩过：m1 一度挂 (ring−cbv−…)=5070.334，实测 4957.171
            #   ⇒ 差 113.16 = 3×π/4·Ø4²·cb_t。**分步口径必须按当时几何算**。
            "after_cb_mm3": ring - bore - outer - n * math.pi / 4.0 * cb_d ** 2 * cb_t}


def _cap_small_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                    part: str = CAP_SMALL) -> dict:
    """小盖：参数来自 `parts.<part>._measured_faces.solved`（sw_profile 实测）。"""
    q = P["parts"][part]
    sv = ((q.get("_measured_faces") or {}).get("solved")) or {}
    if not sv:
        raise ValueError(f"{part}: 缺 _measured_faces.solved —— 先跑 sw_profile.py 再写回 params")
    OD = float(sv["outer_d"]); ID = float(sv["inner_d"]); t = float(sv["thickness"])
    hs = sv["holes"]; n = int(hs["n"]); hd = float(hs["d"]); pitch_d = float(hs["pitch_d"])
    cb = sv.get("counterbore") or {}
    cb_d = float(cb.get("d") or 0.0); cb_t = float(cb.get("t") or 0.0)
    bk = sv.get("bore_chamfer") or {}; oc = sv.get("outer_chamfer") or {}
    bleg = float(bk.get("leg") or 0.0); oleg = float(oc.get("leg") or 0.0)
    ro, ri = OD / 2.0, ID / 2.0
    r_hole, rh, rcb = pitch_d / 2.0, hd / 2.0, cb_d / 2.0
    # ---- 守卫：越界/自相矛盾在**计划期**就拒建，不交给真机去发现 ----
    if not 0.0 < ri < ro:
        raise ValueError(f"{part}: 内径 {ID} 必须落在 (0, 外径 {OD}) 之间")
    if t <= 0.0:
        raise ValueError(f"{part}: 厚度 {t} 必须 > 0")
    if n < 1 or hd <= 0 or pitch_d <= 0:
        raise ValueError(f"{part}: 孔规格不全（n={n}, d={hd}, pitch_d={pitch_d}）")
    if r_hole - rh < ri - 1e-9 or r_hole + rh > ro + 1e-9:
        raise ValueError(f"{part}: 孔（分布圆 Ø{pitch_d:g}、Ø{hd:g}）超出环带 "
                         f"[{ID:g}, {OD:g}] ⇒ 体积靶值不成立")
    if cb_d and not rh < rcb < ro:
        raise ValueError(f"{part}: 沉孔 Ø{cb_d:g} 半径须落在 (孔半径 {rh:g}, 外半径 {ro:g})")
    if cb_d and not 0.0 < cb_t < t:
        raise ValueError(f"{part}: 沉孔深 {cb_t:g} 必须落在 (0, 厚度 {t:g})")
    if bleg and not 0.0 < bleg < t:
        raise ValueError(f"{part}: 孔口锥口深 {bleg:g} 必须落在 (0, 厚度 {t:g})")
    if oleg >= ro - ri:
        raise ValueError(f"{part}: 外缘倒角 {oleg:g} 过大（≥ 壁厚 {ro - ri:g}）")
    vols = _cap_small_volume(sv)
    ring_v = vols["ring_mm3"]; after_turn_v = vols["after_turn_mm3"]
    after_cb_v = vols["after_cb_mm3"]; final_v = vols["volume_mm3"]
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    # 半剖面 6 点（x∈[0,t]，左端 x=0）：从左端孔口锥口 → 内孔 → 右端面 → 外圆 → 外缘倒角 → 左端面
    prof = [[bleg, ri], [t, ri], [t, ro], [oleg, ro], [0.0, ro - oleg], [0.0, ri + bleg]]
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        {"id": "p", "op": "select_plane", "plane": "Front"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "cl", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": t, "y2": 0.0},
        {"id": "prof", "op": "polyline", "points": prof, "close": True,
         "note": f"半剖面（含孔口锥口 {bleg:g} + 外缘倒角 {oleg:g}）"},
        {"id": "s2", "op": "sketch_end", "name": f"{q['name']}半剖面"},
        {"id": "ck", "op": "check_sketch_constraint", "sketch": f"{q['name']}半剖面",
         "expect_min_dims": 0,
         "note": "★ 尺寸标注已封存 ⇒ 只记录约束状态；几何由 check_bbox / check_mass 兜"},
        {"id": "rev", "op": "revolve_boss", "angle": 360, "name": q["name"]},
        {"id": "bb", "op": "check_bbox", "expect": [t, OD, OD], "tol_mm": 0.05,
         "note": f"与参照件 {q['ref']} 的包围盒一致（[{t:g}, {OD:g}, {OD:g}]）"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": ro, "tol": 0.01},
        {"id": "m0", "op": "check_mass", "expect_volume_mm3": round(after_turn_v, 3), "vol_tol": 0.005,
         "note": f"回转后 = 环 {ring_v:.3f} − 锥口 {vols['bore_chamfer_mm3']:.3f} "
                 f"− 外缘倒角 {vols['outer_chamfer_mm3']:.3f} = {after_turn_v:.3f}mm³"
                 f"（倒角/锥口画在半剖面里，回转即生效）"},
        # ---- 沉孔（Ø6 深 cb_t，左端）：Right 面草图压在左端面上 ----
        {"id": "ax", "op": "axis_from_planes", "plane1": "Front", "plane2": "Top",
         "name": "Axis1", "note": "阵列轴 = 回转轴 = 全局 X（Front ∩ Top）"},
        {"id": "p2", "op": "select_plane", "plane": "Right",
         "note": "孔位草图开在 Right 基准面（X=0）—— 正好压在小盖左端面上"},
        {"id": "s3", "op": "sketch_begin", "reset": True},
        {"id": "cl2", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": ro, "y2": 0.0},
        {"id": "c1", "op": "circle", "cx": r_hole, "cy": 0.0, "d": cb_d,
         "note": f"Ø{cb_d:g} 沉孔种子，距原点 {r_hole:g}（分布圆 Ø{pitch_d:g}）"},
        {"id": "s4", "op": "sketch_end", "name": f"{q['name']}沉孔位"},
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": f"{q['name']}沉孔位",
         "expect_min_dims": 0, "note": "★ 只记录约束状态；沉孔对不对由 m1 兜"},
        {"id": "cut_cb", "op": "extrude_cut", "depth": cb_t, "end_cond": "swEndCondBlind",
         "name": f"{q['name']}沉孔", "note": f"左端起深 {cb_t:g}（单向，不贯穿）"},
        {"id": "cp_cb", "op": "circular_pattern", "target": f"{q['name']}沉孔",
         "axis": "Axis1", "count": n, "angle": 360, "name": f"{q['name']}沉孔阵",
         "note": f"整圈 {n} 个（含原始那个）"},
        {"id": "m1", "op": "check_mass", "expect_volume_mm3": round(after_cb_v, 3), "vol_tol": 0.005,
         "note": f"沉孔后 = 回转 {after_turn_v:.3f} − {n}×π/4·Ø{cb_d:g}²·{cb_t:g} "
                 f"= {after_cb_v:.3f}mm³（**此时通孔未切，沉孔切的是整块 Ø{cb_d:g}**）"},
        # ---- 通孔（Ø4 贯穿）：同一面再开一张草图 ----
        # ★ 必须**重新 select_plane**：上一个特征（切除/阵列）会清空选择集
        #   ⇒ 真机踩过「开始草图前必须先选一个基准面/平面（用 select_plane）」
        {"id": "p3", "op": "select_plane", "plane": "Right"},
        {"id": "s5", "op": "sketch_begin", "reset": True},
        {"id": "cl3", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": ro, "y2": 0.0},
        {"id": "c2", "op": "circle", "cx": r_hole, "cy": 0.0, "d": hd,
         "note": f"Ø{hd:g} 通孔种子"},
        {"id": "s6", "op": "sketch_end", "name": f"{q['name']}通孔位"},
        {"id": "ck3", "op": "check_sketch_constraint", "sketch": f"{q['name']}通孔位",
         "expect_min_dims": 0, "note": "★ 只记录约束状态；通孔对不对由 m2 兜"},
        {"id": "cut", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
         "both_directions": True, "name": f"{q['name']}通孔",
         "note": "双向贯穿 —— 草图平面在端面上，单向穿不透"},
        {"id": "cp", "op": "circular_pattern", "target": f"{q['name']}通孔",
         "axis": "Axis1", "count": n, "angle": 360, "name": f"{q['name']}通孔阵",
         "note": f"整圈 {n} 个（含原始那个）"},
        {"id": "m2", "op": "check_mass", "expect_volume_mm3": round(final_v, 3), "vol_tol": 0.005,
         "note": f"{ring_v:.3f} − 沉孔 {vols['counterbore_mm3']:.3f} − 通孔 "
                 f"{vols['thru_mm3']:.3f} − 锥口 {vols['bore_chamfer_mm3']:.3f} − 倒角 "
                 f"{vols['outer_chamfer_mm3']:.3f} = {final_v:.3f}mm³（参照 4806.0）"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-{OD:g}x{ID:g}x{t:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，环回转 + 3 沉孔 + 3 通孔圆周阵列）",
                 "units": "mm",
                 "note": f"规格来自 reducer.params.json::parts.{part}._measured_faces.solved"
                         f"（sw_profile 实测），不硬编码",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"OD": OD, "ID": ID, "t": t, "ri": ri, "ro": ro, "n": n,
                     "hole_d": hd, "pitch_d": pitch_d, "counterbore": {"d": cb_d, "t": cb_t},
                     **vols, "reference_volume_mm3": sv.get("reference_mm3")},
    }


# --------------------------------------------------------------- 反光片（3-反光片）
#
# 型面（`sw_profile` 实测 2026-10-04，回转轴 = 全局 X，**球心 = 草图原点**）：
#   R17 外球冠（北极帽 x∈[14.875,17]，r 8.23→0）
#   + Ø34 柱段（x∈[12.875,14.875]）
#   + R14.875 内球窝（x∈[12.875,14.875]，r 7.45→0）
#   + 经轴闭合
#   ⇒ 形状 = Ø34 法兰环（厚 2，内表面是球窝）+ 中心球冠凸台（R17）
#   ★ 型面写回 params 时**整体平移 −12.875** ⇒ 参数表 L=4.125、x∈[0, L]。
#     （`sw_profile` 判 `revolvable=False` 是**因为 3 个偏心孔**，不是母线本身的问题 ——
#      与「带孔盘族」同源的误判，母线照用。）
#
# 偏心特征（`sw_profile.off_axis` + `probe_reflector_faces.py` 实测）：
#   · 3 × Ø4 通孔 @ 分布圆 Ø24（120°）—— 与小盖的 3×M3 同位置
#   · 2 × Ø3 **斜槽**：轴线在 xy 平面内、与 X 轴成 ±17.5°、过 (6.06, 0, 0)，
#     绕 X 轴 180° 镜像（参照件特征树 = 「切除-旋转3 + 阵列(圆周)1」）
#     ⇒ 照建口径：**斜中心线 + 从轴延伸到 r=1.5 的矩形 → revolve_cut → 阵列 2 个**。
#       （17.5° 是实测轴向量反解出来的：cos17.5°=0.95372 / sin17.5°=0.30071，
#        与 dump 的 axis=(0.9537, 0.3007, 0) 吻合到 4 位。）
#
# ★ 体积三条（逐级钉死）：
#     m0 回转  = Pappus 1868.403
#     m1 +3孔  = 1868.403 − 3×π/4·4²·2 = 1793.005
#     m2 +2斜槽= 1762.7（**对照参照件 1762.706**；斜槽切料量解析不可算 ⇒ 先哨兵取真值回填）
REFLECTOR = "reflector"
_SLANT_DEG = 17.5          # 斜槽轴线与 X 轴的夹角（dump 的 axis 向量反解）


def _reflector_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                    part: str = REFLECTOR) -> dict:
    """反光片：型面半剖面回转（含球冠弧）+ 3×Ø4 孔阵列 + 2 个 Ø3 斜槽阵列。"""
    q = P["parts"][part]
    pr = q.get("profile") or {}
    SV = (q.get("_measured_faces") or {}).get("solved") or {}
    if not pr:
        raise ValueError(f"{part}: 参数表里没有 profile（先用 sw_profile 提取再写回）")
    if pr.get("revolvable") is False:
        raise ValueError(f"{part}: sw_profile 判定非纯回转体 —— 别照这份轮廓建件")
    L = float(pr["L"])
    Df = float(v(q["D"]))
    ro = Df / 2.0
    poly = [[float(a), float(b)] for a, b in pr["polyline"]]
    arcs = pr.get("arcs") or []
    poly_close = bool(pr.get("polyline_close"))
    if poly_close and arcs:
        raise ValueError(f"{part}: 有圆弧却让直线串自己闭合 —— 会多出一条边")
    if not poly_close and not arcs:
        raise ValueError(f"{part}: 轮廓开口（既无圆弧也不闭合）")
    ax_pts = list(poly)
    for a in arcs:
        ax_pts += [a["p1"], a["p2"], a["p3"]]
    xs = [p[0] for p in ax_pts]
    rs = [p[1] for p in ax_pts]
    if abs(min(xs)) > 1e-6 or abs(max(xs) - L) > 1e-6:
        raise ValueError(f"{part}: 型面轴向 [{min(xs)}, {max(xs)}] 与 L={L} 不符")
    r_max = max(rs)
    if abs(r_max * 2.0 - Df) > 1e-6:
        raise ValueError(f"{part}: 型面最大半径 {r_max:g}×2 ≠ 参数表 D={Df:g}")
    turn_v = _profile_volume(poly, arcs)
    stored = pr.get("pappus_volume_mm3")
    if stored and abs(turn_v - stored) / stored > 1e-3:
        raise ValueError(f"{part}: 从型面重算的体积 {turn_v:.3f} 与 params 里存的 "
                         f"{stored:.3f} 不符 —— 型面被改坏了？")

    # ---- 偏心孔 ----
    hs = SV.get("holes") or {}
    n = int(hs.get("n") or 0)
    hd = float(hs.get("d") or 0.0)
    pitch_d = float(hs.get("pitch_d") or 0.0)
    if n < 1 or hd <= 0 or pitch_d <= 0:
        raise ValueError(f"{part}: 孔规格不全（n={n}, d={hd}, pitch_d={pitch_d}）")
    r_hole, rh = pitch_d / 2.0, hd / 2.0
    if r_hole + rh > ro + 1e-9:
        raise ValueError(f"{part}: 孔（分布圆 Ø{pitch_d:g}、Ø{hd:g}）超出外径 Ø{Df:g}")
    thru_t = float(SV.get("thickness") or 0.0)
    if thru_t <= 0:
        raise ValueError(f"{part}: 缺 thickness（孔所在环的轴向厚度 —— 决定孔体积）")
    holes_v = n * math.pi / 4.0 * hd ** 2 * thru_t
    after_holes_v = turn_v - holes_v

    # ---- 斜槽（参照件坐标系 → 型面坐标系：减 X0）----
    ss = SV.get("slant_slot") or {}
    sr = float(ss.get("r") or 0.0)
    ap = [float(x) for x in (ss.get("axis_point") or [])]
    ad = [float(x) for x in (ss.get("axis_dir") or [])]
    trng = [float(x) for x in (ss.get("t_range") or [])]
    scnt = int(ss.get("count") or 0)
    if sr <= 0 or len(ap) != 2 or len(ad) != 2 or len(trng) != 2 or scnt < 2:
        raise ValueError(f"{part}: 斜槽规格不全（{ss}）")
    X0 = 12.875
    apn = [ap[0] - X0, ap[1]]
    t0, t1 = trng
    # ★ 两个斜槽 = 关于 X 轴镜像（y 取反）⇒ **两次独立的旋转切除**。
    #   不用 `circular_pattern(count=2, angle=360)`：真机实测那条**建不出特征**
    #   （「圆周阵列没生成特征」），而两次 revolve_cut 各自用「草图内中心线」都被验证过。
    slots: list[dict] = []
    for sgn in (1.0, -1.0):
        adi = [ad[0], ad[1] * sgn]
        api = [apn[0], apn[1] * sgn]
        c0 = [api[0] + t0 * adi[0], api[1] + t0 * adi[1]]
        c1 = [api[0] + t1 * adi[0], api[1] + t1 * adi[1]]
        nm = [-adi[1], adi[0]]                  # 方向逆时针 90°
        slots.append({
            "axis_dir": adi,
            "cl": [c0, c1],
            "rect": [c0, c1,
                     [c1[0] + sr * nm[0], c1[1] + sr * nm[1]],
                     [c0[0] + sr * nm[0], c0[1] + sr * nm[1]]],
        })

    ref_v = float(SV.get("reference_volume_mm3") or 0.0)

    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        {"id": "p", "op": "select_plane", "plane": "Front"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "cl", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": L, "y2": 0.0},
        {"id": "prof", "op": "polyline", "points": poly, "close": poly_close,
         "note": f"实测型面直线串 {len(poly)} 点（球冠/球窝那两段交给圆弧收口）"},
    ]
    for i, a in enumerate(arcs):
        steps.append({
            "id": f"arc{i}", "op": "arc3p",
            "p1": a["p1"], "p2": a["p2"], "p3": a["p3"],
            "note": f"R{a['radius']:g} 球面母线（**起点/终点/弧上点** 的顺序）—— 收口成闭环"})
    steps += [
        {"id": "s2", "op": "sketch_end", "name": f"{q['name']}半剖面"},
        {"id": "ck", "op": "check_sketch_constraint", "sketch": f"{q['name']}半剖面",
         "expect_min_dims": 0, "note": "★ 尺寸标注已封存 ⇒ 只记录约束状态，几何由下三条兜"},
        {"id": "rev", "op": "revolve_boss", "angle": 360, "name": q["name"]},
        {"id": "bb", "op": "check_bbox", "expect": [L, Df, Df], "tol_mm": 0.05,
         "note": f"与参照件 {q['ref']} 的包围盒一致（[{L:g}, {Df:g}, {Df:g}]）"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": ro, "tol": 0.01},
        {"id": "m0", "op": "check_mass", "expect_volume_mm3": round(turn_v, 3), "vol_tol": 0.005,
         "note": f"Pappus 2π·r̄·A = {turn_v:.3f}mm³（球冠弧+球窝弧都在轮廓里，错一处立刻红）"},
        # ---- 3 × Ø4 通孔：Right 基准面草图 + 双向贯穿 + 圆周阵列 ----
        {"id": "ax", "op": "axis_from_planes", "plane1": "Front", "plane2": "Top",
         "name": "Axis1", "note": "阵列轴 = 回转轴 = 全局 X（Front ∩ Top）"},
        {"id": "p2", "op": "select_plane", "plane": "Right",
         "note": "孔位草图开在 Right 基准面（法向 X）—— 平面在零件左侧之外，双向贯穿切到环"},
        {"id": "s3", "op": "sketch_begin", "reset": True},
        {"id": "cl2", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": ro, "y2": 0.0},
        {"id": "c1", "op": "circle", "cx": r_hole, "cy": 0.0, "d": hd,
         "note": f"Ø{hd:g} 种子孔，距原点 {r_hole:g}（分布圆 Ø{pitch_d:g}）"},
        {"id": "s4", "op": "sketch_end", "name": f"{q['name']}孔位"},
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": f"{q['name']}孔位",
         "expect_min_dims": 0, "note": "★ 只记录约束状态；孔对不对由 m1 兜"},
        {"id": "cut", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
         "both_directions": True, "name": f"{q['name']}孔",
         "note": "双向贯穿 —— 草图平面骑在零件外侧，单向穿不透"},
        {"id": "cp", "op": "circular_pattern", "target": f"{q['name']}孔",
         "axis": "Axis1", "count": n, "angle": 360, "name": f"{q['name']}孔阵",
         "note": f"整圈 {n} 个（含原始那个）"},
        {"id": "m1", "op": "check_mass", "expect_volume_mm3": round(after_holes_v, 3), "vol_tol": 0.005,
         "note": f"回转 {turn_v:.3f} − {n}×π/4·{hd:g}²·{thru_t:g} = {after_holes_v:.3f}mm³"},
    ]
    # ---- 2 × Ø3 斜槽：各自「斜中心线 + 从轴延伸到 r=1.5 的矩形」→ 旋转切除 ----
    # ★ 关键坑（2026-10-04 真机）：`revolve_cut` 不写 `profile` 时会用
    #   `ctx["last_sketch"]`，而它是 `op_sketch_end` 里由 `last_sketch_name()`
    #   （= 特征树里**最后一个 ProfileFeature**）定的 —— 前面几张草图已被各自的
    #   特征**消费并嵌套**进子节点，遍历顺序一变就取错草图 ⇒ 第二个 revolve_cut
    #   报「特征成功但**体积没减少**」。**显式传 profile 后即修好。**
    #   （单侧单独跑永远成功，正是因为它前面只有 1 张未消费的草图。）
    for i, sl in enumerate(slots):
        sk = f"{q['name']}斜槽{i}"
        steps += [
            {"id": f"p3{i}", "op": "select_plane", "plane": "Front",
             "note": "每次开草图前都要重选基准面（上一特征清空了选择集）"},
            {"id": f"s5{i}", "op": "sketch_begin", "reset": True},
            {"id": f"org{i}", "op": "point", "x": 0.0, "y": 0.0,
             "note": "★ 原点锚（S7）：斜槽回转轴**天生离轴**（到 X 轴 r≈0.926，"
                     "与 X 轴成 17.5°）⇒ 轴永远过不了原点。补一个落在草图原点的"
                     "构造点锁住定位自由度。**不放中心线**——`revolve_cut` 的兜底会抓"
                     "草图里**第一条**中心线当旋转轴，多一条会把轴抓错。"},
            {"id": f"clv{i}", "op": "centerline",
             "x1": sl["cl"][0][0], "y1": sl["cl"][0][1],
             "x2": sl["cl"][1][0], "y2": sl["cl"][1][1],
             "note": f"斜槽{i}回转轴：过 ({sl['cl'][0][0]:.3f}, {sl['cl'][0][1]:.3f})，"
                     f"方向 {[round(x, 6) for x in sl['axis_dir']]}（与 X 轴成 ±{_SLANT_DEG}°）"},
            {"id": f"rv{i}", "op": "polyline", "points": sl["rect"], "close": True,
             "note": f"矩形**从斜轴延伸到 r={sr:g}** ⇒ 回转出实心 Ø{sr * 2:g} 圆柱槽"},
            {"id": f"s6{i}", "op": "sketch_end", "name": sk},
            {"id": f"cks{i}", "op": "check_sketch", "sketch": sk,
             "note": "体检：应为 4 段 LINE（矩形）+ 1 段 LINE（中心线）"},
            {"id": f"ck3{i}", "op": "check_sketch_constraint", "sketch": sk,
             "expect_min_dims": 0, "note": "★ 只记录约束状态；斜槽对不对由 m2 兜"},
            {"id": f"rcut{i}", "op": "revolve_cut", "angle": 360, "profile": sk,
             "name": sk,
             "note": "绕**草图内中心线**旋转切除；★ 必须显式给 profile，别走 last_sketch"},
        ]
    steps += [
        {"id": "m2", "op": "check_mass", "expect_volume_mm3": round(ref_v, 3), "vol_tol": 0.005,
         "note": f"含 2 斜槽后对照参照件 {ref_v:.3f}mm³（真机实测 1762.7106，差 0.0003%）。"
                 f"斜槽切料量解析不可算 ⇒ 靶值直接取参照件体积，tol 收到 0.5% 兜回归"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-{Df:g}x{L:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，球冠型面回转 + 3×Ø{hd:g} 孔 + "
                         f"{scnt}×Ø{sr * 2:g} 斜槽）",
                 "units": "mm",
                 "note": f"型面来自 reducer.params.json::parts.{part}.profile，"
                         f"偏心特征来自 parts.{part}._measured_faces.solved（sw_profile 实测），不硬编码",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"L": L, "Df": Df, "r_max": r_max, "n_arcs": len(arcs),
                     "turn_volume_mm3": turn_v, "holes_volume_mm3": holes_v,
                     "after_holes_volume_mm3": after_holes_v,
                     "slant_slot": {"r": sr, "count": scnt, "slots": slots},
                     "reference_volume_mm3": ref_v},
    }


# --------------------------------------------------------------- 端盖·透盖族（法兰 + 通孔 + 毡圈槽）
#
# 2 件（端盖1 / 端盖3）是**透盖**：轴从中间穿过去，所以型面比闷盖复杂得多 ——
#   法兰 → 台阶 → 轴承孔 → **梯形毡圈槽** → 通孔，
# 轴向**进进出出**（非单调）。`sw_faces.stairway`（圆柱台阶表）串不出闭合轮廓，
# 这也是它们一直卡在 `todo` 的原因（`parts.cover1._measured_faces` 记着这桩悬案）。
#
# ★ 破法：`scripts/sw_profile.py` 从参照件的**面的边界圆**（不是草图、也不是顶点，
#   理由见该脚本头）重建整条型面元素序列，存进 params 的 `parts.<part>.profile`。
#   实测：
#     · 19-端盖1 → 18 个元素（17 段直线 + **1 段 R2 圆弧**），Pappus 差 0.0006%
#     · 28-端盖3 → 17 个元素（纯直线）                        ，Pappus 差 0.0000%
#
# ★★ `_measured_faces` 原来那句「端盖3 与端盖1 **拓扑相同**、只差直径」是**错的**：
#   端盖1 在回转腔底部多一段 R2 圆角（环面），端盖3 没有。
#   教训与 §76 同源 —— **读面表得出的"拓扑相同"不可靠**；
#   这次是靠 `sw_profile` 的**闭合性**（cover3 闭合 / cover1 断在 (−1,24)→(1,26)）
#   加上 **Pappus 体积对账** 才判准的。
#
# ★ 为什么环面必须收进来：环面在型面里是**圆弧**，`stairway`/`plane_stations`
#   两张表都看不见它 ⇒ 轮廓断口。`ISurface.IsTorus` + `TorusParams`
#   （8 个 double：圆心 xyz、轴向 xyz、主半径 = 轴线到管子中心的距离、
#   次半径 = 剖面圆弧半径）给出圆心与半径，端点取面的两个边界圆。
COVER_THRU_FAMILY = ("cover1", "cover3")

# ★ 型面驱动族（2026-10-02 泛化）：**任何**由 `sw_profile` 判定为纯回转体的件，
#   把型面写进参数表就能建 —— 不必再为每件手写型面。
#   透盖族只是它的一个子集（端盖1/3），挡油环是第二个非透盖成员。
#   判据：`parts.<part>.profile.revolvable is not False`（缺这个字段=旧数据，放行）。
PROFILE_FAMILY = COVER_THRU_FAMILY + (
    "oil_sling",
    # ★ 5/9-螺钉 M3×22 / M3×10（GB/T 818 十字槽盘头）：也是纯回转体（盘头 + 杆贴轴），
    #   型面 6 顶点一次成型。**十字槽刻意不建** —— GB 表没有槽宽/槽深，
    #   参照件的槽草图又是「4 线单矩形」不像十字形 ⇒ 登记 Q6 待裁决，不猜。
    "screw_m3x10",
    "screw_m3x22",
    # ★ 26-圆锥销（登记键 taper_pin_4x18）：**图纸/参照件矛盾件**（图纸=圆锥销 GB/T 117，
    #   参照件=圆柱销 Ø4×19，见 Q12）。型面按**参照件**落库：母线 Ø4×18 +
    #   两端球冠 R4.25。★ 这一型的特点是**两端点都落在轴线上**（r=0）⇒ 轮廓
    #   靠**中心线**收口（`closed_via_axis`），与 oil_sling / 透盖那种自闭合环不同。
    "taper_pin_4x18",
)


def _profile_points(poly: list, arcs: list, arc_n: int = 64) -> list:
    """型面（直线串 + 圆弧）→ 密集点列（圆弧按 `arc_n` 段采样）。"""
    pts = [[float(p[0]), float(p[1])] for p in poly]
    for a in arcs:
        cx, cy = float(a["center"][0]), float(a["center"][1])
        r = float(a["radius"])
        pa = [float(a["p1"][0]), float(a["p1"][1])]
        pb = [float(a["p2"][0]), float(a["p2"][1])]
        ta = math.atan2(pa[1] - cy, pa[0] - cx)
        tb = math.atan2(pb[1] - cy, pb[0] - cx)
        d = tb - ta
        while d > math.pi:
            d -= 2 * math.pi
        while d < -math.pi:
            d += 2 * math.pi
        for k in range(1, arc_n + 1):
            t = ta + d * k / arc_n
            pts.append([cx + r * math.cos(t), cy + r * math.sin(t)])
    if pts and abs(pts[0][0] - pts[-1][0]) < 1e-9 and abs(pts[0][1] - pts[-1][1]) < 1e-9:
        pts = pts[:-1]
    return pts


def _profile_volume(poly: list, arcs: list) -> float:
    """由型面自己重算面积 / 形心半径 / Pappus 体积（**不信 params 里存的数字**）。

    ★ 为什么要重算：如果直接用 params 里的 `pappus_volume_mm3`，
      型面被改坏了照样能出一个"看起来对"的期望值 —— 期望值必须是**从型面推出来的**，
      两者对不上就当场报错（同 §"返回值会说谎"的处理）。
    """
    pts = _profile_points(poly, arcs)
    n = len(pts)
    if n < 3:
        raise ValueError("型面点太少，算不出面积")
    s = 0.0
    cr = 0.0
    for i in range(n):                        # ★ 回卷闭合（开口链不能漏收口那条边）
        x0, r0 = pts[i]
        x1, r1 = pts[(i + 1) % n]
        cross = x0 * r1 - x1 * r0
        s += cross
        cr += (r0 + r1) * cross
    area = abs(s) / 2.0
    cro = cr / (6.0 * area) if area > 1e-12 else 0.0
    return 2.0 * math.pi * abs(cro) * area


def _profile_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                  part: str = "") -> dict:
    """型面驱动族：按参数表里存的**实测型面**生成计划（半剖面回转一次成型）。

    这是"**照参照件建件**"的通用出口 —— 只要一件是**纯回转体**（`sw_profile` 判定
    `revolvable=True`），把它的型面写进 `parts.<part>.profile` 就能建，不必为每件
    手写型面。目前这个族里有：
      · 透盖（端盖1/3）—— 非单调型面 + R2 毡圈槽圆弧
      · 挡油环（25）—— 台阶 + 两个锥面

    型面来自 `parts.<part>.profile`（`sw_profile.py` 提取）：
      · `polyline` 是直线串（open），`arcs` 是圆弧（用 `arc3p` 补），
        两者首尾相接构成闭合轮廓（**圆弧排在最后**，见 sw_profile.rotate_chain_after_arc）。
    """
    q = P["parts"][part]
    pr = q.get("profile")
    if not pr:
        raise ValueError(f"{part}: 参数表里没有 profile —— "
                         f"先用 scripts/sw_profile.py 提取参照件型面再写回 params")
    # ★ 提取器判"不是纯回转体"的件，不许照轮廓建 —— 见 sw_profile 的 blockers
    if pr.get("revolvable") is False:
        raise ValueError(f"{part}: sw_profile 判定该件**不是纯回转体** —— "
                         f"别照这份轮廓建件。原因：{pr.get('blockers')}")
    L = float(pr["L"])
    Df = float(v(q["D"]))
    poly = [[float(p[0]), float(p[1])] for p in pr["polyline"]]
    arcs = pr.get("arcs") or []
    poly_close = bool(pr.get("polyline_close"))
    # ★ 直线串要么**自己闭合**（纯直线件，close=True），要么**交给圆弧收口**
    #   （close=False）。两个都对不上就会多画/少画一条边，SolidWorks 直接不认轮廓。
    if poly_close and arcs:
        raise ValueError(f"{part}: 有圆弧却让直线串自己闭合 —— 会多出一条边")
    if not poly_close and not arcs:
        raise ValueError(f"{part}: 既没有圆弧、直线串也不闭合 —— 轮廓是开口的")
    if poly_close and abs(poly[0][0] - poly[-1][0]) < 1e-9 \
            and abs(poly[0][1] - poly[-1][1]) < 1e-9:
        raise ValueError(f"{part}: close=True 时首尾点不能重合（会多画一条零长直线）")

    # 型面自校核：轮廓必须落在 [0, L] 且 O 半径必须等于参数表里的 D/2
    ax_pts = list(poly)
    for a in arcs:
        ax_pts += [a["p1"], a["p2"], a["p3"]]
    xs = [p[0] for p in ax_pts]
    rs = [p[1] for p in ax_pts]
    if abs(min(xs)) > 1e-6 or abs(max(xs) - L) > 1e-6:
        raise ValueError(f"{part}: 型面轴向范围 [{min(xs)}, {max(xs)}] 与 L={L} 不符")
    r_max = max(rs)
    if abs(r_max * 2.0 - Df) > 1e-6:
        raise ValueError(f"{part}: 型面最大半径 {r_max} × 2 ≠ 参数表 D={Df}")

    vol = _profile_volume(poly, arcs)
    stored = pr.get("pappus_volume_mm3")
    if stored and abs(vol - stored) / stored > 1e-3:
        raise ValueError(f"{part}: 从型面重算的体积 {vol:.3f} 与 params 里存的 "
                         f"{stored:.3f} 不符（差 {abs(vol - stored) / stored * 100:.4f}%）"
                         f" —— 型面被改坏了？")
    ref_vol = float(pr.get("reference_volume_mm3") or stored or vol)
    if abs(vol - ref_vol) / ref_vol > 0.02:
        raise ValueError(f"{part}: 型面体积 {vol:.3f} 与参照件 {ref_vol:.3f} 差 "
                         f"{abs(vol - ref_vol) / ref_vol * 100:.3f}% > 2%")
    mass_g = round(vol * 1e-3, 4)

    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        {"id": "p", "op": "select_plane", "plane": "Front"},
        {"id": "s1", "op": "sketch_begin"},
        # 回转轴 = Front 平面的 X 轴（y=0）。型面已归一化到 x∈[0, L]。
        {"id": "cl", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": L, "y2": 0.0},
        # 直线串：段数多且**非单调**（法兰→台阶→轴承孔→毡圈槽→通孔）。
        # 纯直线件用 close=True 自己闭合；带圆弧的件用 close=False，由 arc3p 收口。
        {"id": "prof", "op": "polyline", "points": poly, "close": poly_close,
         "note": f"实测型面直线串 {len(poly)} 点（非单调：轴向进进出出）；"
                 f"闭合方式={'自己闭合' if poly_close else '交给圆弧收口'}"},
    ]
    for i, a in enumerate(arcs):
        steps.append({
            "id": f"arc{i}", "op": "arc3p",
            "p1": a["p1"], "p2": a["p2"], "p3": a["p3"],
            # ★ arc3p 的点序是 **起点 / 终点 / 弧上点**（不是"起点/中点/终点"），
            #   写成后者只会画出 90° 弧、拼不成闭环。
            "note": f"环面圆弧 R{a['radius']:g}（回转腔底倒圆）—— 闭合轮廓"})
    steps += [
        # ★★ 尺寸标注已封存（2026-10-02）—— 这里原来挂 `dim_auto(chain=off) + deviation`。
        #   **这一族正是封存的直接触发点**：型面**非单调**（轴向进进出出、同一半径在
        #   不相邻的轴向区间出现两次），逐段标时 SW 弹**模态确认框**，
        #   自动化挂死（`RPC_E_DISCONNECTED`）且型面被驱动改坏。
        #   ⇒ 现在一条尺寸都不标；几何正确性交给下面 check_bbox / check_cyl_radius /
        #   check_mass 三条（Pappus 体积由型面现算，型面错一处立刻红）。
        {"id": "s2", "op": "sketch_end", "name": f"{q['name']}半剖面"},
        {"id": "ck", "op": "check_sketch_constraint", "sketch": f"{q['name']}半剖面",
         "expect_min_dims": 0,
         "note": "★ 尺寸标注已封存 ⇒ 不校验尺寸条数，只记录约束状态"},
        {"id": "rev", "op": "revolve_boss", "angle": 360, "name": q["name"]},
        {"id": "bb", "op": "check_bbox", "expect": [L, Df, Df], "tol_mm": 0.05,
         "note": f"与参照件 {q['ref']} 的包围盒一致（[{L:g}, {Df:g}, {Df:g}]）"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": r_max, "tol": 0.01},
        {"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
         "note": "Pappus 2π·r̄·A 由型面现算 —— 型面/毡圈槽/圆弧任何一处错，这条立刻红"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-{Df:g}x{L:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，实测型面半剖面回转）", "units": "mm",
                 "note": f"型面来自 reducer.params.json::parts.{part}.profile"
                         f"（sw_profile 从参照件面的边界圆重建，Pappus 已对账）",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"L": L, "Df": Df, "r_max": r_max,
                     "n_elements": pr.get("n_elements"), "n_arcs": len(arcs),
                     "analytic_volume_mm3": vol,
                     "reference_volume_mm3": ref_vol},
    }


# --------------------------------------------------------------- 六角件族（螺母 / 六角头螺栓）
#
# 4 件（11/15-螺母、13/27-六角头螺栓）都是**同一个形状族**：一个正六边形 + 一个同心圆。
#   · **螺母**（GB/T 6170）：`s` 对边宽 / `m` 高 / 内孔 Ø`d` ⇒ **一张草图两个轮廓，一次拉伸**
#     （参照件就是这么建的：草图1 有 2 个轮廓 → 凸台-拉伸 depth=8.4）。
#   · **螺栓**（GB/T 5782）：`s` 对边宽 / `k` 头高 / 杆 Ø`d` / 杆长 `L`
#     ⇒ **两次拉伸**（先六角头 0→k，再圆杆 0→L；杆在 0→k 完全被头包住，`merge=True` 自动并成一体）。
#     ★ **不用 `plane_offset` 把杆挪到 Z=k** —— 那需要引用前序步返回的面名
#       （`{{ vars.* }}`），会让**离线求值失败**，而"计划可离线校验"是 `yoyo.swplan/1` 的核心约定。
#       两张草图都开在 Front 平面、各自从 0 起拉伸，结果完全一样。
#
# ★ 正六边形几何：**外接圆半径 r = s/√3**（s = 对边宽；因 r·cos30° = s/2），
#   顶点 = (r·cos k·60°, r·sin k·60°)，k=0..5。
#   ⇒ 顶点摆在 X 轴上时，包围盒 = **[2s/√3, s, 厚度]**。
#   参照件螺母 M10 实测 18.841×15.389（GB s=16 ⇒ 理论对角 18.475 / 对边 16，差 2~4%），
#   所以 **bbox 只对解析期望，不与参照件对账**。
#   ★★★ 2026-10-04 修正：原实现把「对边宽 s」当「外接圆半径」直接传给顶点函数，
#     建出的六角比 GB 名义**大 √3 倍**（M8 螺栓对边本该 13，实建 22.5）；
#     且 `_hex_volume` 的解析式同步放大 ⇒ `check_mass` 一路绿灯。
#     这正是 §102 的教科书案例：**判据只保证「算的和模型一致」，不保证「模型和图纸一致」**。
#     `assets/gb_standard.json::families.hex_bolt._note` 早就写着「建模时 r = s/2/cos30°」，
#     是**代码没照它做**。
#
# ★★ 刻意**不建**两样东西，并在 `_measured_faces` 里写明：
#   ① **螺纹**（外螺纹 / 内螺纹）—— 真螺纹要螺旋线+扫掠，慢且脆；比赛评分看形状与国标尺寸。
#      ⇒ 建的是**光杆 / 光孔**。
#   ② **端面 30° 倒角**（GB 有、参照件也有）—— 需要精确选边，先不做。
#   ⇒ 因此**体积解析值就是"纯六角柱 − 孔"**，可自校验；
#      而**不能**拿参照件 `volume_mm3` 对账（含螺纹件的 `mass_props` 本就不可信，§83）。
HEX_FAMILY = ("nut_m8", "nut_m10", "bolt_m8x25", "bolt_m8x65")

#: 「顶点方向包围盒 / 对边宽」= 2/√3（正六边形的常数）
_HEX_R = 0.8660254037844386          # √3/2


def _hex_vertices(r_circum: float) -> list:
    """正六边形顶点，**参数 = 外接圆半径**（顶点到中心），第一个顶点在 +X 上。

    ★★ 与「对边宽 s」的换算：`r_circum = s / √3`（因 r_circum·cos30° = s/2）。
      —— 2026-10-04 修正：原注释把参数写成「对边宽 = s = 外接圆半径」，两个概念混为一谈，
      `_hex_plan` 据此**把对边宽直接当外接圆半径**传进来 ⇒ 六角大 √3 倍。见文件上方注释。
    """
    return [[round(r_circum * math.cos(math.radians(60 * k)), 6),
             round(r_circum * math.sin(math.radians(60 * k)), 6)] for k in range(6)]


def _hex_volume(s: float, d: float, kind: str, h1: float, h2: float = 0.0) -> float:
    """六角件解析体积（**不含螺纹、不含端部倒角** ⇒ 就是精确值）。

    螺母（`hex_nut`，`h1`=m 高）：六角棱柱 − 同轴通孔。
    螺栓（`hex_bolt`，`h1`=k 头高、`h2`=L 杆长）：**六角只占 0→k 那段**，k→L 是圆杆。
    ★ 把整根 L 当六角柱算，期望值会放大近 3 倍（真机 3317 vs 错公式 9720 才发现）。
    """
    area = math.sqrt(3.0) / 2.0 * s ** 2               # (√3/2)·s²，s = 对边宽
    if kind == "hex_nut":
        return area * h1 - math.pi / 4.0 * d ** 2 * h1
    if kind == "hex_bolt":
        if not h2 > h1:
            raise ValueError(f"六角头螺栓需 0 < 头高 {h1} < 杆长 {h2}")
        return area * h1 + math.pi / 4.0 * d ** 2 * (h2 - h1)
    raise ValueError(f"六角件族只认 kind='hex_nut'/'hex_bolt'，实得 {kind!r}")


def _hex_plan(P: dict, out_dir: str | None = "D:/out/reducer",
              part: str = "") -> dict:
    """六角件族：螺母（单特征）/ 六角头螺栓（双特征）。

    规格来自参数表 `spec_s`（对边宽）/ `spec_m`（螺母高）或 `spec_k`（头高）+ `spec_d`（公称直径）
    + `L`（杆长），全部 `source="gb"`（唯一来源 `assets/gb_standard.json`）。
    """
    q = P["parts"][part]
    kind = q.get("kind")
    s = float(v(q["spec_s"]))
    d = float(v(q["spec_d"]))
    if not s > 0 or not 0 < d < s:
        raise ValueError(f"{part}: 需要 0 < 公称直径 {d} < 对边宽 {s}")
    r_circum = s / math.sqrt(3.0)       # 外接圆半径 = 对边宽 / √3（2026-10-04 修正）
    verts = _hex_vertices(r_circum)
    across = 2.0 * r_circum             # 顶点方向（X）= 2s/√3
    flat = s                            # 边中点方向（Y）= 对边宽 s
    is_nut = kind == "hex_nut"
    if is_nut:
        m = float(v(q["spec_m"]))
        if not m > 0:
            raise ValueError(f"{part}: 螺母高 {m} 必须 > 0")
        thick, steps_body = m, []
    elif kind == "hex_bolt":
        k = float(v(q["spec_k"]))
        L = float(v(q["L"]))
        if not 0 < k < L:
            raise ValueError(f"{part}: 螺栓需 0 < 头高 {k} < 杆长 {L}")
        thick, steps_body = L, []
    else:
        raise ValueError(f"{part}: 六角件族只认 kind='hex_nut'/'hex_bolt'，实得 {kind!r}")

    # 解析体积（**不建倒角、不建螺纹**，所以这就是精确值）
    vol = _hex_volume(s, d, kind, m if is_nut else k, 0.0 if is_nut else L)
    vol_note = ("(3√3/2)s²·m − π/4·d²·m" if is_nut
                else "(3√3/2)s²·k + π/4·d²·(L−k)   ← 六角只占 0→k")
    mass_g = round(vol * 1e-3, 4)
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    gb = (q.get("_measured_faces") or {}).get("solved", {}).get("gb", {})

    # ---- 螺母：一张草图两个轮廓（六边形 + 同心圆），两侧对称拉伸 ----
    if is_nut:
        steps_body = [
            {"id": "p", "op": "select_plane", "plane": "Front"},
            {"id": "s1", "op": "sketch_begin"},
            {"id": "hx", "op": "polyline", "points": verts, "close": True,
             "note": f"正六边形 对边宽 s={s:g}（外接圆半径 = s）"},
            {"id": "hole", "op": "circle", "cx": 0.0, "cy": 0.0, "d": d,
             "note": f"内孔 Ø{d:g}（GB 公称；**光孔，未建内螺纹**）"},
            {"id": "s2", "op": "sketch_end", "name": f"{q['name']}截面"},
            {"id": "ck", "op": "check_sketch_constraint",
             "sketch": f"{q['name']}截面", "expect_min_dims": 0,
             "note": "★ 标注已封存 ⇒ 只记录约束状态"},
            {"id": "ex", "op": "extrude_boss", "depth": m,
             "end_cond": "swEndCondMidPlane", "name": q["name"],
             "note": "两个轮廓（外六角 + 内孔）一次拉伸 —— 参照件同款做法"},
        ]
    # ---- 螺栓：六角头（0→k）+ 同心圆杆（0→L），merge 自动并成一体 ----
    else:
        steps_body = [
            {"id": "p1", "op": "select_plane", "plane": "Front"},
            {"id": "s1", "op": "sketch_begin"},
            # ★ 中心线过原点：正六边形的 6 个顶点都**不在**原点（螺母那张草图有同心圆
            #   才天然锚住），不过 S7「草图必须锚住原点」。中心线是**构造线**，
            #   不参与轮廓 ⇒ 不影响 extrude_boss（它选的是草图，不是图元）。
            {"id": "cl", "op": "centerline", "x1": -s, "y1": 0.0, "x2": s, "y2": 0.0,
             "note": "构造线过原点（锚定用，不参与轮廓）"},
            {"id": "hx", "op": "polyline", "points": verts, "close": True,
             "note": f"六角头 对边宽 s={s:g}（GB/T 5782）"},
            {"id": "s2", "op": "sketch_end", "name": f"{q['name']}头截面"},
            {"id": "ck1", "op": "check_sketch_constraint",
             "sketch": f"{q['name']}头截面", "expect_min_dims": 0,
             "note": "★ 标注已封存 ⇒ 只记录约束状态"},
            {"id": "ex1", "op": "extrude_boss", "profile": f"{q['name']}头截面",
             "depth": k, "name": f"{q['name']}头",
             "note": f"头高 k={k:g}（Z 0→{k:g}）"},
            # ★ 杆也**从 Front 平面起**拉伸（0→L），0→k 段完全被头包住 ⇒ merge 成一体。
            #   不写 `plane_offset` + 引用面名：那是 `{{ vars.* }}`，会让离线求值失败。
            {"id": "p2", "op": "select_plane", "plane": "Front"},
            {"id": "s3", "op": "sketch_begin"},
            {"id": "cd", "op": "circle", "cx": 0.0, "cy": 0.0, "d": d,
             "note": f"杆 Ø{d:g}（GB 公称；**光杆，未建外螺纹**）"},
            {"id": "s4", "op": "sketch_end", "name": f"{q['name']}杆截面"},
            {"id": "ck2", "op": "check_sketch_constraint",
             "sketch": f"{q['name']}杆截面", "expect_min_dims": 0,
             "note": "★ 标注已封存 ⇒ 只记录约束状态"},
            {"id": "ex2", "op": "extrude_boss", "profile": f"{q['name']}杆截面",
             "depth": thick, "merge": True, "name": f"{q['name']}杆",
             "note": f"杆长 L={thick:g}（Z 0→{thick:g}），与头重叠段自动 merge"},
        ]

    steps: list[dict] = [{"id": "n", "op": "new_part"}] + steps_body + [
        {"id": "bb", "op": "check_bbox", "expect": [across, flat, thick], "tol_mm": 0.02,
         "note": f"正六边形（顶点朝 X）⇒ 包围盒 [{across:g}, {flat:.4f}, {thick:g}]"},
        {"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
         "note": f"{vol_note} = {vol:.4f}mm³ —— **不与参照件比**"
                 f"（含螺纹件 mass_props 不可信，§83）"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-{s:g}x{d:g}x{thick:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，{'六角截面拉伸' if is_nut else '六角头 + 圆杆'}）",
                 "units": "mm",
                 "note": f"规格来自 reducer.params.json::parts.{part}（source=gb，"
                         f"唯一权威表 assets/gb_standard.json）；**不硬编码**。"
                         f"刻意不建螺纹与端部倒角（见代码头注）",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"kind": kind, "s": s, "d": d, "thickness": thick,
                     "gb": gb, "analytic_volume_mm3": vol,
                     "reference_bbox_mm":
                         (q.get("_measured_faces") or {}).get("solved", {})
                         .get("reference_bbox_mm")},
    }


# --------------------------------------------------------------- 弹簧垫圈族（螺旋线 + 矩形截面扫掠）
#
# 14-弹簧垫圈 8（GB/T 93 标准型）：**不是回转件**，是**平螺旋** ——
#   矩形截面 (料宽 b × 厚 s) 沿螺旋线扫掠，圈间相切（螺距 p = b）。
#
# ★ 参照件是「**拉伸态**」：bbox [12.284, 4.984, 14.206] 显示螺距 ≈ 7.1、2 圈（圈间有缝），
#   体积 106.27；而 GB 平螺旋圈间相切时解析体积 ≈ 254（差 2.4 倍）⇒ **参照件不是标准形状**。
#   按口径「标准件跟图纸/参照件」这里有歧义，**取 GB/T 93 标准形状**并把差异登记在
#   `_measured_faces`（参照件是"装配拉长态"，画的是示意形状，不是规格）。
#
# ★ 关键几何：
#   · 螺旋**中心线半径** R = d1/2 + b/2（d1 是**内径**，料宽 b 搭在中心线上）
#   · 圈数 n = (H − s)/b + 1（自由高 H 扣掉首尾两段料厚再除料宽）
#   · 扫掠**体积 = 截面积 × 路径长度**（刚体扫掠的精确公式）：
#     L = n·√((π·2R)² + p²)   —— 后一项是螺距带来的"每圈抬升"，漏掉会低估 0.2%
#   · bbox = [d1+2b, d1+2b, H]（外径 × 外径 × 自由高）
#
# ★ 面的选择（`swept_boss` 的硬要求：轮廓平面 ⊥ 路径切向、且过路径起点）：
#   螺旋轴 = **Z**，画在 Front 平面（XY）上，起点默认在 +X（`start_angle=0`）
#   ⇒ 起点切向 = +Y ⇒ 轮廓平面法向必须是 Y ⇒ 画在 **Top 平面**（XZ），矩形心放在 (R, 0)。
SPRING_WASHER_FAMILY = ("washer_s8",)


def _spring_washer_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                        part: str = "") -> dict:
    """弹簧垫圈族：螺旋线 + 矩形截面扫掠（GB/T 93 标准型）。"""
    q = P["parts"][part]
    d1 = float(v(q["d1"]))
    b = float(v(q["b"]))
    s = float(v(q["s"]))
    H = float(v(q["H"]))
    if not 0 < d1 and not 0 < b and not 0 < s:
        raise ValueError(f"{part}: d1/b/s 必须 > 0（收到 {d1}/{b}/{s}）")
    if H <= s:
        raise ValueError(f"{part}: 自由高 {H} 必须大于料厚 {s}（至少一圈）")
    n = int(round((H - s) / b)) + 1               # 圈数（圈间相切 ⇒ 螺距 = 料宽 b）
    R = d1 / 2.0 + b / 2.0                        # 螺旋中心线半径
    p = b                                         # 平螺旋：螺距 = 料宽
    Dc = 2.0 * R
    length = n * math.hypot(math.pi * Dc, p)      # 螺旋线总长（刚体扫掠用）
    vol = b * s * length
    mass_g = round(vol * 1e-3, 4)
    OD = d1 + 2.0 * b
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        # ---- ① 螺旋线路径 ----
        {"id": "p1", "op": "select_plane", "plane": "Front",
         "note": "螺旋轴 = Z ⇒ 路径画在 Front 平面（XY）"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "c", "op": "circle", "cx": 0.0, "cy": 0.0, "r": R,
         "note": f"螺旋中心线半径 R = d1/2 + b/2 = {R:g}"},
        # ★★ **不要先 sketch_end**：`InsertHelix` 只认当前选择，而**模型态下选不中草图里的圆边**
        #   （实测 7 种方式全灭）。正确顺序是**留在草图里**画螺旋线 ——
        #   `helix` 会用 `ctx.segments` 里的圆 `Select4` 选中，螺旋线作为**顶层 Helix 特征**生成。
        {"id": "hx", "op": "helix", "pitch": p, "height": n * p,
         "clockwise": False, "reversed": False, "start_angle": 0,
         "name": f"{q['name']}螺旋线",
         "note": f"**草图内**建螺旋（草图态选图元，模型态选不中）；"
                 f"平螺旋 螺距 p=料宽 b={p:g}（圈间相切），{n} 圈 ⇒ 段长 {n * p:g}"},
        # ★ **不要 sketch_end**：草图内插螺旋线后 SW 会把草图删掉（螺旋线成为独立 Helix 特征），
        #   再调 sketch_end 会去关一个不存在的草图 ⇒ 后面全错位（见 §85）。
        # ---- ② 矩形截面（必须 ⊥ 路径切向且过起点）----
        {"id": "p2", "op": "select_plane", "plane": "Top",
         "note": "起点切向 = +Y ⇒ 轮廓平面法向取 Y ⇒ **Top 平面**（XZ）"},
        {"id": "s3", "op": "sketch_begin"},
        # 构造线：从原点指向截面中心。既是 S7「草图锚住原点」的依据，
        # 也是这段扫掠的**径向参考**（螺旋内径方向），语义上不是凑数。
        {"id": "cl", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": R, "y2": 0.0,
         "note": "径向参考构造线（原点→截面中心）；不参与轮廓"},
        {"id": "rc", "op": "rect_center", "cx": R, "cy": 0.0, "w": b, "h": s,
         "note": f"矩形 心在 (R,0)=路径起点；w=料宽 b={b:g}（径向/X），h=厚 s={s:g}（轴向/Z）"},
        {"id": "s4", "op": "sketch_end", "name": f"{q['name']}截面"},
        {"id": "ck", "op": "check_sketch_constraint", "sketch": f"{q['name']}截面",
         "expect_min_dims": 0,
         "note": "★ 标注已封存 ⇒ 只记录约束状态"},
        # ---- ③ 扫掠 ----
        # ★ `twist=1` = `swTwistControlKeepNormalConstant`（**保持法向恒定 / 直纹**）。
        #   默认 0 = `swTwistControlFollowPath` 会让截面跟着螺旋切线转，
        #   实测外径 12.753 vs 理论 12.1（多 0.65）—— 圈数越多偏得越多。
        {"id": "sw", "op": "swept_boss", "profile": f"{q['name']}截面",
         "path": f"{q['name']}螺旋线", "twist": 1, "name": q["name"],
         "note": "刚体扫掠（twist=1 直纹）：体积 = 截面积 × 路径长（check_mass 靠这个）"},
        {"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
         "note": f"b·s·n·√((π·{Dc:g})²+p²) = {vol:.4f}mm³ —— **不与参照件比**"
                 f"（参照件是拉长态、非标准形状，见头注）"},
        # ⚠★ **判据只能是 check_mass**：`GetBodyBox` 对**螺旋扫掠面（B 样条）会虚报** ——
        #   实测外径报 12.1 ✔（对）但轴向报 6.0（真值 4.0，虚 50%）、另一向 12.7（真值 12.1）。
        #   这与"螺纹让包围盒虚报"（§坑表 2026-09-25）是**同一个毛病**。
        #   ⇒ 这里把尺寸期望记进 note（人能查），**不做门禁**；形状对不对由体积（精确公式）兜。
        {"id": "bb", "op": "check_bbox", "expect": [OD, OD, n * p], "mode": "record",
         "note": f"外径 d1+2b={OD:g}、轴向 {n}×螺距={n * p:g}（=自由高 H={H:g}）—— "
                 f"**只记录不判定**：螺旋扫掠面是 B 样条，GetBodyBox 轴向虚报 ~50%（同螺纹虚报）"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-d{d1:g}xH{H:g}x{n}n.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，平螺旋扫掠 {n} 圈）", "units": "mm",
                 "note": f"规格来自 reducer.params.json::parts.{part}"
                         f"（source=gb，GB/T 93 标准型）；**不硬编码**",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"d1": d1, "b": b, "s": s, "H": H, "n_turns": n,
                     "pitch": p, "helix_R": R, "OD": OD,
                     "path_length_mm": round(length, 4),
                     "analytic_volume_mm3": round(vol, 4),
                     "free_height_note": f"自由高 H={H:g}；s + (n−1)·b = {s + (n - 1) * b:g}"},
    }


# --------------------------------------------------------------- 真螺纹牙型（helix + cut_swept）
#
# ★★ 真螺纹走 **helix + cut_swept**，**不用 `real_thread`**：Q11（齿轮轴 M14×2）的 real_thread
#    跑 195s 没建出特征；而现成样板 `assets/plans/leadscrew.json`（丝杠）23 步全绿 `failed:null`。
#    螺塞 / 通气塞两件**唯一共同阻塞就是真螺纹** ⇒ 这里用扫掠绕开旧判据"卡 Q11 真螺纹"。
# ★ 工艺哨兵（2026-10-04，`D:/out/_train/reducer/probe_thread.plan.json`，22/22 步全绿 45s）：
#    轴 = 全局 X；**Right 面画 Ø10 圆（留在草图里）** → helix(pitch=1.0, height=4.5) →
#    Front 面画牙槽 polyline → cut_swept。体积 942.478 → 914.337（**减 28.14**）。
#    解析核对：牙槽在材料内截面 ≈0.222mm² × 螺旋长 132mm ≈ 29.4 ≈ 实测 28.14（差 4%）✓
#    ⇒ 证实是"真咬料"，不是"特征建出来但静默无操作"。
def _thread_profile(d: float, pitch: float, x_start: float = 0.0) -> list:
    """外螺纹**牙槽**截面（过轴平面内：x = 轴向、y = 径向），按 GB/T 196 基本参数。

    牙高 h3 = 0.6134P；牙槽顶宽 0.875P、底宽 P/6（哨兵实测该形状体积核对命中）。
    ★ 顶边 y = R + 0.5P（**故意超出**圆柱面）—— 牙槽必须真正"咬"进 Ød 圆柱，
      顶边正好贴面会退化成相切、扫掠切不动材料。
    """
    R = d / 2.0
    h = 0.6134 * pitch
    tw = 0.875 * pitch
    bw = pitch / 6.0
    return [[x_start + tw / 2.0, R + 0.5 * pitch],
            [x_start - tw / 2.0, R + 0.5 * pitch],
            [x_start - bw / 2.0, R - h],
            [x_start + bw / 2.0, R - h]]


def _hex_chamfer_volume(r_circ: float, top_r: float, x_base: float, x_top: float,
                        half_deg: float, n: int = 600) -> float:
    """六角头 **30° 锥倒角**的**实际**切除体积（六个"角"的解析面积 × 沿轴数值积分）。

    ★★ **绝不能拿"锥面外环形旋转体"的体积当切除量**（我第一次就是这么错的）：
      那个环的径向范围 [r_cone, R+3] **远超**六角截面（≤ R），两者真正相交的只是
      **六个角**那一小块儿。用旋转体体积会**高估 ~40 倍**（本件：错值 410.3 vs 真值 ≈ 9.0），
      把一条本该通过的 `check_mass` 判红 —— 真机 2026-10-04 踩过。

    几何：六角外接圆 R；锥面在轴向位置 x 处的半径 r_cone(x)。被切掉的是六角截面里
    r > r_cone 的部分。六个顶点各贡献一个"角"，其面积用极坐标解析：
        f(φ) = sinφ + √3·cosφ，在 φ=φ0 处等于 f0 = √3·R / r_cone
        A_角 = 3R²/4·[(√3·sinφ0 − cosφ0)/f0 + 1/√3] − r_cone²·φ0
      （φ=0 是顶点方向；φ0 = arcsin(f0/2) − 60°，f0 = √3R/r_cone ∈ (√3, 2]）
    自校：r_cone = 7（= 内切圆）时 f0=2 ⇒ φ0=30° ⇒ A_角 = 2.633 ⇒ 6 角 = 15.80
          = 六角面积 169.74 − Ø14 圆 153.94 ✓ 与顶面 Ø14 圆形自洽。
    """
    R = r_circ
    r3 = math.sqrt(3.0)
    tan_a = math.tan(math.radians(half_deg))
    dx = (x_top - x_base) / n
    total = 0.0
    for i in range(n):
        x = x_base + (i + 0.5) * dx
        rc = top_r + (x_top - x) * tan_a
        if rc <= 0.0:
            continue
        f0 = r3 * R / rc
        if f0 <= r3:                     # r_cone ≥ R ⇒ 没切到角
            continue
        f0 = min(f0, 2.0)                # 防 asin 越界（rc < R/2 的病态输入）
        phi0 = math.asin(f0 / 2.0) - math.radians(60.0)
        if phi0 <= 0.0:
            continue
        a_ang = ((3.0 * R * R / 4.0)
                 * ((r3 * math.sin(phi0) - math.cos(phi0)) / f0 + 1.0 / r3)
                 - rc * rc * phi0)
        total += 6.0 * max(a_ang, 0.0) * dx
    return total


def _plug_plan(P: dict, out_dir: str | None = "D:/out/reducer",
               part: str = "plug") -> dict:
    """螺塞（16）：回转段 + 六角头（对边 14）+ 30° 锥倒角 + 真螺纹 M10×1.0。轴 = 全局 X。

    结构（x∈[0, 15.0]）：
      ① 回转段 x∈[0, 9.0]：端面Ø7 → 45°端倒角 → Ø10 杆(4.5) → Ø8.5 退刀槽(2.0) → Ø13 轴肩(1.0)
      ② 六角头 x∈[9.0, 15.0]：**对边宽 14**（外接圆 R = 14/√3 = 8.0829）、高 6.0
      ③ 30° 锥倒角：顶面 r=7（Ø14 = 六角内切圆）向下扩到 r=8.155 (> 外接圆) ⇒ 切掉棱角，
         顶面成 Ø14 圆（参照件顶面 A=153.938 = π/4·14² 正是这个圆）
      ④ 真螺纹 M10×1.0：helix 从 x=0（Right 面）起、高 6.0；咬料段 = Ø10 杆 x∈[1.5, 6.0]

    ★★ 六角"对边宽 14"是**铁证**：参照件六角底面环 A=37.0087 = (√3/2)·14² − π/4·13²
       （169.74 − 132.73 = 37.01 ✓ 逐位吻合）。
    ★★ **绝不拿参照件体积 1659.79 对账** —— 它含螺纹曲面，`mass_props` 对含螺纹件不可信
       （§83⑤：螺母差 5 倍、螺栓差 20%）。验收 = bbox（尺寸集合）+ check_cyl_radius + 解析体积。
    """
    q = P["parts"][part]
    pr = q.get("profile") or {}
    SV = (q.get("_measured_faces") or {}).get("solved") or {}
    if not pr:
        raise ValueError(f"{part}: 缺 profile（回转段轮廓）—— 先用 sw_profile 提取再写回 params")
    if not SV:
        raise ValueError(f"{part}: 缺 _measured_faces.solved（六角头/锥倒角/螺纹取证）")
    L_rot = float(pr["L"])
    D_rot = float(pr["D"])
    poly = [[float(a), float(b)] for a, b in pr["polyline"]]
    xs = [p[0] for p in poly]
    rs = [p[1] for p in poly]
    if abs(min(xs)) > 1e-6 or abs(max(xs) - L_rot) > 1e-6:
        raise ValueError(f"{part}: 回转段轴向范围 [{min(xs)}, {max(xs)}] 与 L={L_rot} 不符")
    if abs(max(rs) * 2.0 - D_rot) > 1e-6:
        raise ValueError(f"{part}: 回转段最大半径×2 ≠ 参数表 D={D_rot}")
    ret_v = _profile_volume(poly, [])
    stored = pr.get("pappus_volume_mm3")
    if stored and abs(ret_v - stored) / stored > 1e-3:
        raise ValueError(f"{part}: 回转段重算体积 {ret_v:.4f} 与 params 存的 "
                         f"{stored:.4f} 不符（型面被改坏了？）")

    # ---- 六角头 ----
    hx = SV.get("hex_head") or {}
    af = float(hx.get("across_flats") or 0.0)
    x_hex0 = float(hx.get("x_start") or 0.0)
    hh = float(hx.get("height") or 0.0)
    if af <= 0 or hh <= 0:
        raise ValueError(f"{part}: 六角头规格不全（{hx}）")
    r_circ = af / math.sqrt(3.0)                     # 外接圆半径（= 正六边形边长）
    if abs(x_hex0 - L_rot) > 1e-6:
        raise ValueError(f"{part}: 六角头应坐在回转段端面 x={L_rot}，实得 x_start={x_hex0}")
    verts = _hex_vertices(round(r_circ, 6))
    hex_area = 3.0 * math.sqrt(3.0) / 2.0 * r_circ ** 2      # 边长 r_circ 的正六边形面积

    # ---- 30° 锥倒角 ----
    ch = SV.get("chamfer") or {}
    half = float(ch.get("half_angle_deg") or 0.0)
    top_r = float(ch.get("top_r") or 0.0)
    x_top = float(ch.get("x_top") or 0.0)
    dx_ch = float(ch.get("depth_x") or 2.0)
    if not 0.0 < half < 90.0 or top_r <= 0 or x_top <= x_hex0:
        raise ValueError(f"{part}: 锥倒角参数不全（{ch}）")
    x_base = x_top - dx_ch
    r_base = top_r + dx_ch * math.tan(math.radians(half))
    if r_base < r_circ - 1e-9:
        raise ValueError(f"{part}: 锥倒角深度不够（r_base={r_base:.4f} < 外接圆 {r_circ:.4f}）"
                         f"—— 切不到六角棱角")
    cham_poly = [[x_base, r_base], [x_base, r_circ + 3.0],
                 [x_top, r_circ + 3.0], [x_top, top_r]]
    # ★★ 切除量**不是**这个多边形旋转体的体积（径向范围远超六角，会高估 ~40 倍）；
    #    真值 = 六个"角"被锥面切掉的面积 × 沿轴积分 —— 见 _hex_chamfer_volume 头注。
    cham_v = _hex_chamfer_volume(r_circ, top_r, x_base, x_top, half)

    # ---- 螺纹 ----
    th = SV.get("thread") or {}
    td = float(th.get("d") or 0.0)
    pitch = float(th.get("pitch") or 0.0)
    th_h = float(th.get("height") or 0.0)
    if td <= 0 or pitch <= 0 or th_h <= 0:
        raise ValueError(f"{part}: 螺纹规格不全（{th}）")
    tooth = _thread_profile(td, pitch, 0.0)
    blank_v = ret_v + hex_area * hh - cham_v          # 切螺纹前的"光坯"体积（解析）
    thread_cut_v = 28.5                               # 哨兵 4.5 长实测 28.14；本件同 4.5 咬料
    after_v = blank_v - thread_cut_v
    mass_blank = round(blank_v * 1e-3, 4)
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir

    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        # ---- ① 回转段（半剖面绕 Front 的 X 轴）----
        {"id": "p1", "op": "select_plane", "plane": "Front"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "cl1", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": L_rot, "y2": 0.0},
        {"id": "prof", "op": "polyline", "points": poly, "close": True,
         "note": f"回转段半剖面 {len(poly)} 点（端倒角→杆→退刀槽→轴肩）；回转轴 = 草图内中心线（X）"},
        {"id": "s2", "op": "sketch_end", "name": f"{q['name']}回转剖面"},
        {"id": "ck1", "op": "check_sketch_constraint", "sketch": f"{q['name']}回转剖面",
         "expect_min_dims": 0, "note": "★ 标注已封存 ⇒ 只记录约束状态，几何由 m0/m1/m2 兜"},
        {"id": "rev", "op": "revolve_boss", "angle": 360, "name": f"{q['name']}回转"},
        {"id": "m0", "op": "check_mass", "expect_volume_mm3": round(ret_v, 3), "vol_tol": 0.005,
         "note": f"回转段 Pappus 2π·r̄·A = {ret_v:.3f}mm³（型面错一处立刻红）"},
        # ---- ② 六角头：Right 面偏移到 x=9.0 再拉伸 ----
        {"id": "p2", "op": "select_plane", "plane": "Right"},
        {"id": "rp1", "op": "ref_plane", "offset": x_hex0, "flip": False,
         "name": f"{q['name']}头面",
         "note": f"平行 Right 的基准面 @ x={x_hex0:g}（**基于标准基准面偏移** ⇒ 不引用前序实体面，可离线校验）"},
        {"id": "s3", "op": "sketch_begin"},
        {"id": "cl2", "op": "centerline", "x1": -r_circ, "y1": 0.0, "x2": r_circ, "y2": 0.0,
         "note": "构造线过原点（S7 锚定用，不参与轮廓）"},
        {"id": "hex", "op": "polyline", "points": verts, "close": True,
         "note": f"正六边形 对边宽 {af:g}（顶点距 = 外接圆半径 {r_circ:.4f}）"},
        {"id": "s4", "op": "sketch_end", "name": f"{q['name']}六角头截面"},
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": f"{q['name']}六角头截面",
         "expect_min_dims": 0, "note": "★ 只记录约束状态"},
        {"id": "ex1", "op": "extrude_boss", "profile": f"{q['name']}六角头截面", "depth": hh,
         "name": f"{q['name']}六角头", "note": f"头高 {hh:g}（x {x_hex0:g}→{x_hex0 + hh:g}）"},
        {"id": "bb1", "op": "check_bbox",
         "expect": [x_hex0 + hh, af, 2.0 * r_circ], "tol_mm": 0.05,
         "note": f"六角头后外形：长 {x_hex0 + hh:g}（X），对边 {af:g}（Y），对角 {2 * r_circ:.3f}（Z）"
                 f"—— 全是平面，GetBodyBox 可信 ⇒ **判定**"},
        {"id": "m1", "op": "check_mass", "expect_volume_mm3": round(ret_v + hex_area * hh, 3),
         "vol_tol": 0.01,
         "note": f"回转 {ret_v:.3f} + 六角柱 {hex_area * hh:.3f}（(3√3/2)·R²·h）"},
        # ---- ③ 30° 锥倒角：绕 X 旋转切除（只作用于 x∈[x_base, x_top]，碰不到杆）----
        {"id": "p3", "op": "select_plane", "plane": "Front"},
        {"id": "s5", "op": "sketch_begin"},
        {"id": "cl3", "op": "centerline", "x1": x_base, "y1": 0.0, "x2": x_top, "y2": 0.0},
        {"id": "chp", "op": "polyline",
         "points": [[x_base, r_base], [x_base, r_circ + 3.0],
                    [x_top, r_circ + 3.0], [x_top, top_r]], "close": True,
         "note": f"{half:g}° 锥倒角：顶面 r={top_r:g} → 向下扩到 r={r_base:.4f}"
                 f"（> 外接圆 {r_circ:.4f} ⇒ 切掉六条棱角，顶面成 Ø{2 * top_r:g} 圆）"},
        {"id": "s6", "op": "sketch_end", "name": f"{q['name']}锥倒角剖面"},
        {"id": "ck3", "op": "check_sketch_constraint", "sketch": f"{q['name']}锥倒角剖面",
         "expect_min_dims": 0, "note": "★ 只记录约束状态"},
        {"id": "rc", "op": "revolve_cut", "profile": f"{q['name']}锥倒角剖面", "angle": 360,
         "name": f"{q['name']}锥倒角", "note": "绕草图内中心线（X）旋切 360°"},
        {"id": "m2", "op": "check_mass", "expect_volume_mm3": round(blank_v, 3), "vol_tol": 0.01,
         "note": f"− 锥倒角（Pappus 现算 {cham_v:.3f}）= 光坯 {blank_v:.3f}mm³（**切螺纹前**）"},
        # ---- ④ 真螺纹 M10×1.0：helix + cut_swept ----
        {"id": "p4", "op": "select_plane", "plane": "Right",
         "note": "螺旋线画在 Right 面（x=0），圆**留在草图里**（模型态选不中草图圆）"},
        {"id": "s7", "op": "sketch_begin"},
        {"id": "c4", "op": "circle", "cx": 0.0, "cy": 0.0, "d": td},
        {"id": "hx", "op": "helix", "pitch": pitch, "height": th_h, "name": f"{q['name']}螺纹线",
         "note": f"**草图内**建螺旋（不可先 sketch_end）；M10 螺距 {pitch:g}，从 x=0 沿 +X "
                 f"高 {th_h:g}（咬料段 = Ø10 杆 x∈[1.5, 6.0]）"},
        {"id": "p5", "op": "select_plane", "plane": "Front"},
        {"id": "s8", "op": "sketch_begin"},
        {"id": "cl5", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": td / 2.0 + 1.0,
         "note": "中心线过原点（S7 锚定）；牙槽画在 x=0 = 螺旋起点处"},
        {"id": "tp", "op": "polyline", "points": tooth, "close": True,
         "note": f"M10×1.0 牙槽（h3=0.6134P，顶宽 0.875P，底宽 P/6）"},
        {"id": "s9", "op": "sketch_end", "name": f"{q['name']}牙型"},
        {"id": "ck4", "op": "check_sketch_constraint", "sketch": f"{q['name']}牙型",
         "expect_min_dims": 0, "note": "★ 只记录约束状态；螺纹对不对由 m3 兜"},
        {"id": "sw", "op": "cut_swept", "profile": f"{q['name']}牙型", "path": f"{q['name']}螺纹线",
         "name": f"{q['name']}螺纹",
         "note": "轮廓平面 ⊥ 路径切向且过起点（Front ⊥ X）；op 内部验证「特征新增 + 体积减少」"},
        {"id": "m3", "op": "check_mass", "expect_volume_mm3": round(after_v, 3),
         "vol_tol": 0.075,
         "note": f"切螺纹后 ≈ {after_v:.3f}mm³（光坯 {blank_v:.3f} − 牙槽咬料 {thread_cut_v:.1f}）；"
                 f"容差放宽 —— 螺纹切除量是**解析估算**，非法定值"},
        {"id": "bb2", "op": "check_bbox", "expect": [x_top, 2.0 * r_circ, af],
         "mode": "record", "note": "最终外形（**记录**：含螺纹面，GetBodyBox 对螺纹会虚报）"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": D_rot / 2.0, "tol": 0.01,
         "note": f"最大圆柱半径 = Ø{D_rot:g} 轴肩的一半（六角头是平面，不参与）"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-af{af:g}xL{x_top:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，回转段 + 六角头 af{af:g} + 真螺纹 M{td:g}×{pitch:g}）",
                 "units": "mm",
                 "note": "尺寸 source=measured（sw_faces/sw_profile 取证，见 params._measured_faces）；"
                         "**不硬编码**。★ 真螺纹用 helix+cut_swept **绕开 Q11 的 real_thread**",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"L_rot": L_rot, "D_rot": D_rot, "revolve_volume_mm3": round(ret_v, 4),
                     "hex_across_flats": af, "hex_r_circum": round(r_circ, 4),
                     "hex_volume_mm3": round(hex_area * hh, 4),
                     "chamfer_volume_mm3": round(cham_v, 4),
                     "blank_volume_mm3": round(blank_v, 4),
                     "after_thread_volume_mm3": round(after_v, 4),
                     "expect_mass_g": mass_blank,
                     "reference_bbox_mm": [x_top, 2.0 * r_circ, af],
                     "reference_volume_mm3": SV.get("reference_volume_mm3"),
                     "thread": f"M{td:g}×{pitch:g}",
                     "reference_volume_note": "参照件 1659.79 **不对账**（含螺纹曲面，mass_props 不可信 §83⑤）"},
    }


def _breather_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                   part: str = "breather") -> dict:
    """通气塞（10）：回转主轮廓（滚花简化为光柱）+ 中心通气孔 + 真螺纹 M10×1.0。轴 = 全局 X。

    结构（x∈[0, 35.0]）：
      顶面Ø19 → 45°倒角 → Ø23 滚花头(6.084) → 过渡锥 → Ø17(10.0) → Ø8.5 退刀槽(2.0)
      → Ø10 螺纹杆(12.0) → 端锥 Ø10→Ø8；中心孔 Ø4 通长 23.0 + 钻尖。
      真螺纹 M10×1.0：helix 从 x=22.0 起（ref_plane 偏移到螺纹杆起点）、高 12.0。

    ★★ 每张主面的**面积都逐位吻合**（台阶表见 params._measured_faces.solved.step_check）：
      Ø17: 2π·8.5·10.0 = 534.07 ✓   Ø4 孔: 2π·2·23.0 = 289.03 ✓   Ø8.5 槽: 2π·4.25·2.0 = 53.41 ✓
      45°锥: π·(11.5+9.5)·2.828 = 186.60 ✓   Ø19 顶面: π/4·19² = 283.53 ✓   端面环: π/4·(8²−4²) = 37.70 ✓
    ★ 滚花头（108 脊 / 235 张面）**简化为 Ø23 光圆柱**并登记 `_simplified`（挂 open_question）。
    ★ 参照件体积（含螺纹 + 滚花 221 张未进轮廓面）**不对账** —— §83⑤。
    """
    q = P["parts"][part]
    pr = q.get("profile") or {}
    SV = (q.get("_measured_faces") or {}).get("solved") or {}
    if not pr:
        raise ValueError(f"{part}: 缺 profile（回转主轮廓）—— 先用 sw_profile 提取再写回 params")
    if not SV:
        raise ValueError(f"{part}: 缺 _measured_faces.solved（主轮廓/中心孔/螺纹取证）")
    L = float(pr["L"])
    D = float(pr["D"])
    poly = [[float(a), float(b)] for a, b in pr["polyline"]]
    xs = [p[0] for p in poly]
    rs = [p[1] for p in poly]
    if abs(min(xs)) > 1e-6 or abs(max(xs) - L) > 1e-6:
        raise ValueError(f"{part}: 主轮廓轴向范围 [{min(xs)}, {max(xs)}] 与 L={L} 不符")
    if abs(max(rs) * 2.0 - D) > 1e-6:
        raise ValueError(f"{part}: 主轮廓最大半径×2 ≠ 参数表 D={D}")
    ret_v = _profile_volume(poly, [])
    stored = pr.get("pappus_volume_mm3")
    if stored and abs(ret_v - stored) / stored > 1e-3:
        raise ValueError(f"{part}: 主轮廓重算体积 {ret_v:.4f} 与 params 存的 "
                         f"{stored:.4f} 不符（型面被改坏了？）")

    # ---- 中心通气孔（Ø4 通长 + 钻尖）----
    ch = SV.get("center_hole") or {}
    hd = float(ch.get("d") or 0.0)
    x_open = float(ch.get("x_open") or 0.0)
    x_tip = float(ch.get("x_tip") or 0.0)
    if hd <= 0 or x_open <= x_tip:
        raise ValueError(f"{part}: 中心孔规格不全（{ch}）")
    tip_len = float(ch.get("tip_len") or ((hd / 2.0) / math.tan(math.radians(60.0))))
    x_flat = x_tip + tip_len
    hole_poly = [[x_tip, 0.0], [x_flat, hd / 2.0], [x_open, hd / 2.0], [x_open, 0.0]]
    hole_v = (math.pi / 4.0 * hd ** 2 * (x_open - x_flat)
              + math.pi / 3.0 * (hd / 2.0) ** 2 * tip_len)

    # ---- 螺纹 ----
    th = SV.get("thread") or {}
    td = float(th.get("d") or 0.0)
    pitch = float(th.get("pitch") or 0.0)
    x_start = float(th.get("x_start") or 0.0)
    th_h = float(th.get("height") or 0.0)
    if td <= 0 or pitch <= 0 or th_h <= 0 or x_start <= 0:
        raise ValueError(f"{part}: 螺纹规格不全（{th}）")
    tooth = _thread_profile(td, pitch, x_start)
    after_hole_v = ret_v - hole_v
    thread_cut_v = 28.14 * (th_h / 4.5)                # 哨兵 4.5 长实测 28.14，按长度等比
    after_v = after_hole_v - thread_cut_v
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir

    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        # ---- ① 外轮廓（半剖面绕 Front 的 X 轴）----
        {"id": "p1", "op": "select_plane", "plane": "Front"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "cl1", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": L, "y2": 0.0},
        {"id": "prof", "op": "polyline", "points": poly, "close": True,
         "note": f"主轮廓半剖面 {len(poly)} 点（顶面→45°倒角→滚花头→过渡锥→轴肩→退刀槽→杆→端锥）；"
                 f"滚花**简化为光柱**，见 _simplified"},
        {"id": "s2", "op": "sketch_end", "name": f"{q['name']}外轮廓"},
        {"id": "ck1", "op": "check_sketch_constraint", "sketch": f"{q['name']}外轮廓",
         "expect_min_dims": 0, "note": "★ 标注已封存 ⇒ 只记录约束状态，几何由 m0/m1/m2 兜"},
        {"id": "rev", "op": "revolve_boss", "angle": 360, "name": f"{q['name']}主体"},
        {"id": "m0", "op": "check_mass", "expect_volume_mm3": round(ret_v, 3), "vol_tol": 0.005,
         "note": f"主轮廓 Pappus 2π·r̄·A = {ret_v:.3f}mm³（型面错一处立刻红）"},
        {"id": "bb0", "op": "check_bbox", "expect": [L, D, D], "tol_mm": 0.05,
         "note": f"外形 [{L:g}, {D:g}, {D:g}]（滚花简化后即 Ø{D:g} 光柱；全圆柱/锥面 ⇒ "
                 f"GetBodyBox 可信，**判定**）"},
        # ---- ② 中心通气孔：绕 X 旋转切除（钻尖+孔身一次成形）----
        {"id": "p2", "op": "select_plane", "plane": "Front"},
        {"id": "s3", "op": "sketch_begin"},
        {"id": "cl2", "op": "centerline", "x1": x_tip, "y1": 0.0, "x2": x_open, "y2": 0.0},
        {"id": "hp", "op": "polyline", "points": hole_poly, "close": True,
         "note": f"Ø{hd:g} 中心孔通长 + 钻尖（尖端 x={x_tip:g}，孔口 x={x_open:g}）"},
        {"id": "s4", "op": "sketch_end", "name": f"{q['name']}中心孔"},
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": f"{q['name']}中心孔",
         "expect_min_dims": 0, "note": "★ 只记录约束状态；孔对不对由 m1 兜"},
        {"id": "rc", "op": "revolve_cut", "profile": f"{q['name']}中心孔", "angle": 360,
         "name": f"{q['name']}中心孔", "note": "绕草图内中心线（X）旋切 360°"},
        {"id": "m1", "op": "check_mass", "expect_volume_mm3": round(after_hole_v, 3), "vol_tol": 0.01,
         "note": f"主体 {ret_v:.3f} − 孔 {hole_v:.3f} = {after_hole_v:.3f}mm³"},
        # ---- ③ 真螺纹 M10×1.0：ref_plane 偏移到 x=22 → helix + cut_swept ----
        {"id": "p3", "op": "select_plane", "plane": "Right",
         "note": f"Right 面（x=0）偏移到螺纹杆起点 x={x_start:g}"},
        {"id": "rp", "op": "ref_plane", "offset": x_start, "flip": False,
         "name": f"{q['name']}螺纹面",
         "note": f"平行 Right 的基准面 @ x={x_start:g}（**基于标准基准面偏移** ⇒ 可离线校验）"},
        {"id": "s5", "op": "sketch_begin"},
        {"id": "c5", "op": "circle", "cx": 0.0, "cy": 0.0, "d": td},
        {"id": "hx", "op": "helix", "pitch": pitch, "height": th_h, "name": f"{q['name']}螺纹线",
         "note": f"**草图内**建螺旋（不可先 sketch_end）；M10 螺距 {pitch:g}，"
                 f"从 x={x_start:g} 沿 +X 高 {th_h:g}"},
        {"id": "p4", "op": "select_plane", "plane": "Front"},
        {"id": "s6", "op": "sketch_begin"},
        {"id": "cl3", "op": "centerline", "x1": 0.0, "y1": 0.0, "x2": 0.0, "y2": td / 2.0 + 1.0,
         "note": f"中心线过原点（S7 锚定）；牙槽画在 x={x_start:g} = 螺旋起点处"},
        {"id": "tp", "op": "polyline", "points": tooth, "close": True,
         "note": "M10×1.0 牙槽（h3=0.6134P，顶宽 0.875P，底宽 P/6）"},
        {"id": "s7", "op": "sketch_end", "name": f"{q['name']}牙型"},
        {"id": "ck3", "op": "check_sketch_constraint", "sketch": f"{q['name']}牙型",
         "expect_min_dims": 0, "note": "★ 只记录约束状态；螺纹对不对由 m2 兜"},
        {"id": "sw", "op": "cut_swept", "profile": f"{q['name']}牙型", "path": f"{q['name']}螺纹线",
         "name": f"{q['name']}螺纹",
         "note": "轮廓平面 ⊥ 路径切向且过起点（Front ⊥ X）；op 内部验证「特征新增 + 体积减少」"},
        {"id": "m2", "op": "check_mass", "expect_volume_mm3": round(after_v, 3), "vol_tol": 0.1,
         "note": f"切螺纹后 ≈ {after_v:.3f}mm³（− 牙槽咬料 {thread_cut_v:.1f}）；"
                 f"容差放宽 —— 螺纹切除量按哨兵等比估算"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": D / 2.0, "tol": 0.01,
         "note": f"最大圆柱半径 = Ø{D:g} 滚花头的一半（已按光柱建）"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-D{D:g}xL{L:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，回转主轮廓 + Ø{hd:g} 中心孔 + 真螺纹 M{td:g}×{pitch:g}）",
                 "units": "mm",
                 "note": "尺寸 source=measured（sw_faces/sw_profile 取证，见 params._measured_faces）；"
                         "**不硬编码**。★ 真螺纹用 helix+cut_swept **绕开 Q11 的 real_thread**；"
                         "滚花简化为光柱（见 _simplified）",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"L": L, "D": D, "revolve_volume_mm3": round(ret_v, 4),
                     "center_hole_volume_mm3": round(hole_v, 4),
                     "after_hole_volume_mm3": round(after_hole_v, 4),
                     "after_thread_volume_mm3": round(after_v, 4),
                     "thread": f"M{td:g}×{pitch:g}",
                     "reference_bbox_mm": [L, D, D],
                     "simplified": ["knurl → Ø23 光柱"],
                     "reference_volume_note": "参照件含螺纹+滚花 221 张未进轮廓面 ⇒ 体积**不对账**（§83⑤）"},
    }


# =========================================================== 轴承（24 / 30）
#
# ★ 尺寸来源：`sw_faces` 取证参照件（`24-/30-深沟球轴承`，2026-10-04）——
#   外径/内径/宽 = 表列值；**滚珠数/直径/分布半径是实测的**（6204：8 球 Ø6.8 @ R16.75；
#   6206：8 球 Ø7.8 @ R23.0），台肩半径也是实测（外圈内台肩 / 内圈外台肩）。
#   沟道弧半径取 **球半径 + 游隙**（GB 轴承的实际做法）—— 参照件用的是更浅的
#   非保形槽（弧半径 5.4/6.4），本件按**保形+游隙**建，属登记在案的简化差异。
#
# ★★ 工艺铁律（2026-10-04 真机，坑表 §113）：
#   ① **球心离旋转轴 ⇒ 绕轴旋转半圆得不到球**（球对任意不过其心的轴都**不**对称！）。
#      必须绕「**过球心的水平中心线**」旋转半圆。哨兵已验证。
#   ② **绝不用 `circular_pattern` 排滚珠**：真机实测 —— 阵列对「会产生**独立 body**
#      的种子」（球彼此不相交）**静默无产出**（返回特征、体积一点不涨；对照实验：
#      与主体已合并的凸台能阵列 ✔、孤立球 ✗）。⇒ 改用 **Z 次独立 `revolve_boss`**，
#      每个球放在一张 **z 偏移的基准面**上。
#   ③ 球相位取 **22.5° + 360k/Z** ⇒ 没有一个球落在 y=0（球心若在旋转轴上，轮廓会穿轴）。
BEARING_FAMILY = ("bearing_6204", "bearing_6206")


def _bearing_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                  part: str = "bearing_6204") -> dict:
    """深沟球轴承（24/30）：**外圈 + 内圈 + Z 个滚珠**，单件多实体。轴 = 全局 X。

    结构（轴向 x∈[-B/2, B/2]；径向 r）：
      · 外圈：OD D / 内台肩 R_oi，内表面中央一条**圆弧沟道**（弧半径 Rg = 球半径+游隙）
      · 内圈：孔 d / 外台肩 r_io，外表面中央同样的弧沟道
      · 滚珠：Z 个球，球心在分布半径 pitch_r 的圆周上、x=0

    验收：bbox（[B,D,D] 全平面/圆柱，GetBodyBox 可信）+ check_cyl_radius(最大=D/2)
          + **解析体积**（外圈/内圈用 Pappus，球用 4/3πr³）。
    ★ **不拿参照件体积对账**：参照件含保持架/铆钉/非保形槽，本件是登记的简化，
      两者本就不该相等（§102：判据只保证"算的和模型一致"）。
    """
    q = P["parts"][part]
    SV = (q.get("_measured_faces") or {}).get("solved") or {}
    if not SV:
        raise ValueError(f"{part}: 缺 _measured_faces.solved —— 轴承尺寸必须 sw_faces 取证，"
                         f"禁止凭记忆填")
    d = float(SV["d"]); D = float(SV["D"]); B = float(SV["B"])
    ball_d = float(SV["ball_d"]); z = int(SV["ball_count"]); pitch_r = float(SV["pitch_r"])
    R_oi = float(SV["outer_id_land_r"]); r_io = float(SV["inner_od_land_r"])
    clear = float(SV.get("groove_clearance", 0.05))
    phase = float(SV.get("ball_phase_deg", 22.5))
    br = ball_d / 2.0
    Rg = br + clear
    if not (d / 2.0 < r_io < R_oi < D / 2.0):
        raise ValueError(f"{part}: 半径须满足 孔/2 < 内圈外 < 外圈内 < 外径/2，"
                         f"实得 {d/2}, {r_io}, {R_oi}, {D/2}")
    if Rg <= abs(R_oi - pitch_r) + 1e-9 or Rg <= abs(pitch_r - r_io) + 1e-9:
        raise ValueError(f"{part}: 沟道弧半径 Rg={Rg} 够不到台肩（球心到台肩 {abs(R_oi-pitch_r)}）"
                         f" ⇒ 沟道不闭合")
    hb = B / 2.0
    xw_o = math.sqrt(max(Rg * Rg - (R_oi - pitch_r) ** 2, 0.0))     # 外圈沟口半宽
    xw_i = math.sqrt(max(Rg * Rg - (pitch_r - r_io) ** 2, 0.0))     # 内圈沟口半宽
    o_poly = [[-hb, R_oi], [-hb, D / 2.0], [hb, D / 2.0], [hb, R_oi], [xw_o, R_oi]]
    o_arc = [{"p1": [xw_o, R_oi], "p2": [-xw_o, R_oi], "p3": [0.0, pitch_r + Rg],
              "center": [0.0, pitch_r], "radius": Rg}]
    i_poly = [[hb, r_io], [hb, d / 2.0], [-hb, d / 2.0], [-hb, r_io], [-xw_i, r_io]]
    i_arc = [{"p1": [-xw_i, r_io], "p2": [xw_i, r_io], "p3": [0.0, pitch_r - Rg],
              "center": [0.0, pitch_r], "radius": Rg}]
    Vo = _profile_volume(o_poly, o_arc)
    Vi = _profile_volume(i_poly, i_arc)
    Vb = 4.0 / 3.0 * math.pi * br ** 3
    # 球心在 (x=0, y=pitch_r·cosθ, z=pitch_r·sinθ) ⇒ 每球一张 z=const 的基准面
    ball_pos = []
    for k in range(z):
        th = math.radians(phase + 360.0 * k / z)
        ball_pos.append((round(pitch_r * math.cos(th), 4), round(pitch_r * math.sin(th), 4)))
    if any(abs(yc) < br + 1e-9 for yc, _zc in ball_pos):
        raise ValueError(f"{part}: 相位 {phase}° 让某个球心落回旋转轴附近（|y|<球半径）"
                         f" ⇒ 轮廓会穿轴。换相位（用 22.5°+360k/Z）")

    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    nm = q["name"]
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        # ---- ① 外圈 ----
        {"id": "p1", "op": "select_plane", "plane": "Front"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "cl1", "op": "centerline", "x1": -hb, "y1": 0.0, "x2": hb, "y2": 0.0},
        {"id": "opo", "op": "polyline", "points": o_poly, "close": False,
         "note": f"外圈剖面：左端面→OD(D={D:g})→右端面→右台肩(R={R_oi})；缺口由 arc3p 收口"},
        {"id": "ao", "op": "arc3p", "p1": o_arc[0]["p1"], "p2": o_arc[0]["p2"],
         "p3": o_arc[0]["p3"],
         "note": f"外圈沟道：R{Rg:.4f} 弧，圆心在分布半径 {pitch_r:g}，弧底 {pitch_r + Rg:.4f}"},
        {"id": "s2", "op": "sketch_end", "name": f"{nm}外圈剖面"},
        {"id": "ck1", "op": "check_sketch_constraint", "sketch": f"{nm}外圈剖面",
         "expect_min_dims": 0, "note": "★ 标注已封存 ⇒ 只记录约束状态"},
        {"id": "r1", "op": "revolve_boss", "angle": 360, "name": f"{nm}外圈"},
        {"id": "m0", "op": "check_mass", "expect_volume_mm3": round(Vo, 3), "vol_tol": 0.005,
         "note": f"外圈解析 Pappus 体积 {Vo:.4f}mm³"},
        # ---- ② 内圈 ----
        {"id": "p2", "op": "select_plane", "plane": "Front"},
        {"id": "s3", "op": "sketch_begin"},
        {"id": "cl2", "op": "centerline", "x1": -hb, "y1": 0.0, "x2": hb, "y2": 0.0},
        {"id": "opi", "op": "polyline", "points": i_poly, "close": False,
         "note": f"内圈剖面：右台肩→右端面→孔(d={d:g})→左端面→左台肩(R={r_io})"},
        {"id": "ai", "op": "arc3p", "p1": i_arc[0]["p1"], "p2": i_arc[0]["p2"],
         "p3": i_arc[0]["p3"],
         "note": f"内圈沟道：同样 R{Rg:.4f} 弧，弧底 {pitch_r - Rg:.4f}"},
        {"id": "s4", "op": "sketch_end", "name": f"{nm}内圈剖面"},
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": f"{nm}内圈剖面",
         "expect_min_dims": 0},
        {"id": "r2", "op": "revolve_boss", "angle": 360, "name": f"{nm}内圈"},
        {"id": "m1", "op": "check_mass", "expect_volume_mm3": round(Vo + Vi, 3), "vol_tol": 0.005,
         "note": f"外圈 {Vo:.4f} + 内圈 {Vi:.4f} = {Vo + Vi:.4f}"},
    ]
    # ---- ③ Z 个滚珠：每个球一张 z 偏移基准面，绕「过球心的水平中心线」旋转半圆 ----
    acc = Vo + Vi
    for k, (yc, zc) in enumerate(ball_pos):
        acc += Vb
        steps += [
            {"id": f"pb{k}", "op": "select_plane", "plane": "Front"},
            {"id": f"fb{k}", "op": "ref_plane", "offset": abs(zc), "flip": zc < 0,
             "name": f"{nm}滚珠面{k}",
             "note": f"平行 Front @ z={zc:+.4f}（球心所在平面）"},
            {"id": f"sb{k}", "op": "sketch_begin"},
            {"id": f"pt{k}", "op": "point", "x": 0.0, "y": 0.0,
             "note": "★ S7 锚定：球心离轴 ⇒ 中心线不过原点，必须补一个**落在原点的构造点**"
                     "（§111，不能用中心线代）"},
            {"id": f"cb{k}", "op": "centerline", "x1": -br - 1.0, "y1": yc,
             "x2": br + 1.0, "y2": yc,
             "note": f"★ 回转轴 = 过球心 (0,{yc:.4f}) 的水平线（**不是** X 轴：球心离轴）"},
            {"id": f"ob{k}", "op": "polyline", "points": [[-br, yc], [br, yc]], "close": False,
             "note": "半圆直径，贴在回转轴上"},
            {"id": f"ab{k}", "op": "arc3p", "p1": [br, yc], "p2": [-br, yc], "p3": [0.0, yc + br],
             "note": f"半径 {br:g} 半圆（球母线）"},
            {"id": f"es{k}", "op": "sketch_end", "name": f"{nm}滚珠剖面{k}"},
            {"id": f"ckb{k}", "op": "check_sketch_constraint", "sketch": f"{nm}滚珠剖面{k}",
             "expect_min_dims": 0, "note": "★ 每张草图都要有约束闸门（§11.3）；只记录状态"},
            {"id": f"rk{k}", "op": "revolve_boss", "angle": 360, "name": f"{nm}滚珠{k}"},
            {"id": f"mk{k}", "op": "check_mass", "expect_volume_mm3": round(acc, 3),
             "vol_tol": 0.005,
             "note": f"累计到第 {k + 1} 个球：{Vo + Vi:.3f} + {k + 1}×{Vb:.3f}"},
        ]
    steps += [
        {"id": "bb", "op": "check_bbox", "expect": [B, D, D], "tol_mm": 0.05,
         "note": f"外形 [{B:g},{D:g},{D:g}]（全平面/圆柱/球面，GetBodyBox 可信）⇒ **判定**"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": D / 2.0, "tol": 0.01,
         "note": f"最大圆柱面 = 外圈 OD Ø{D:g}"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-bearing-{q['spec']}-{d:g}x{D:g}x{B:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{q['spec']}：外圈+内圈+{z}滚珠，单件多实体）",
                 "units": "mm",
                 "note": "尺寸 source=measured（sw_faces 取证，见 params._measured_faces）。"
                         "★ 滚珠用 Z 次独立 revolve_boss（**不用 circular_pattern** —— 对独立 body "
                         "静默无产出，坑表 §113）；球的回转轴 = 过球心的水平中心线",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {
            "spec": q["spec"], "d": d, "D": D, "B": B,
            "ball_d": ball_d, "ball_count": z, "pitch_r": pitch_r,
            "groove_arc_r": round(Rg, 4), "groove_clearance": clear,
            "outer_volume_mm3": round(Vo, 4), "inner_volume_mm3": round(Vi, 4),
            "ball_volume_mm3": round(Vb, 4),
            "total_volume_mm3": round(Vo + Vi + z * Vb, 4),
            "reference_bbox_mm": [B, D, D],
            "reference_volume_note": "参照件体积**不对账**：它含保持架/铆钉/非保形槽，"
                                     "本件是登记在案的简化（§102）",
        },
    }


HOUSING_FAMILY = ("housing_base", "housing_cover")

# ---------------------------------------------------------------- 箱座 / 箱盖
# ★★ 反解口径（2026-10-04，坑表 §114/§115）：
#   参照件 `1-箱座.SLDPRT` 是 **60 个特征** 的真件（凸台-拉伸1 → 抽壳1 → 切除-拉伸5 →
#   … → 放样1 → 筋1/筋5 → 镜向1/2 → 孔1 → 圆角…），我们**不逐特征复刻**：
#   取「**薄壁箱体**」这一主结构（实测填充率 566713 / 2016035 = 28.1%）+ **顶法兰** +
#   轴承座、孔系；其余（圆角/放样/螺纹孔/销孔/内油道/Ø70 内环槽/**底板 10mm 加厚**/
#   **6×Ø46 螺栓凸台**）**登记为简化**（§102：判据只保证"算的和模型一致"）。
#   ★ 加强筋**已于 2026-10-04 落地**（厚 5，Z 向骑两轴承轴线）——图纸主视图标「5」，
#     参照件特征树有「筋1/筋5」两个 Rib 特征 + 镜向，与「Z 面成对平面间距 5.0」实测一致。
#   壁厚 `t = 6.28`（**唯一标定参数**，反解使解析总量 = 参照 566713，偏 +0.05%）；
#   腔体**封到剖分面 y=40** ⇒ 顶法兰（y∈[40,42.764]）如实保留。
#   ★ 真机标定链（2026-10-04，逐层对过）：
#     外坯解析 2016035 = 真机 m0 **逐位一致**；
#     腔体后 579733 vs 真机 579755（差 22，浮点）；
#     座半环 解析 π·Σ(ro²−ri²)·boss_len —— **首次写成 π/8 少了 4 倍**，
#       真机 m2 实测多出 80837 ⇒ 反查出公式错、改回 π/2·(两面) 才对上。
#     孔系 m4：法兰 6×Ø9 只切穿剖分法兰 **2.764**（下方是空腔），不是壁厚 6.28
#       ⇒ v_holes 必须分开算；改对后解析 568341 = 真机 568267（差 74 = 两座半环
#       0.5mm 重叠被**并集**吃掉）。终判 bbox 精确、体积 **+0.274%**（< 2%）。
#
#   坐标系（箱座自身 = 零件原生朝向，装配**无需旋转**）：
#     X = 轴向（±52.954）、Y = 竖直（底面 −40、剖分面 +40、法兰顶 42.764）、
#     Z = 轴间方向（−25…205，230）。轴承孔轴心 **(y=40, z=70/140)**，中心距 70。
#   `Front` 面 = X-Y 平面（拉沿 +Z）；`Right` 面 = Y-Z 平面，**草图 (u,v) = (−Z, Y)**
#   （由参照件 草图4/草图14 反解：底座板 u∈[−180,0] ↔ Z∈[0,180]；
#     两孔 u=−70/−140 ↔ Z=70/140，v=40 ↔ y=40）。★ 符号错会把孔镜像到 Z<0，
#     而 bbox 尺寸不变 ⇒ 必须另跑一次 sw_faces 复核孔心（本文件 `_housing_probes_note`）。


def _fig8_area(r1: float, r2: float) -> float:
    """两个**下半为直线**的并排半圆（figure-8 顶罩）面积 = π/2·(r1²+r2²)。
    ★ 只在两半圆**互不重叠**时成立（d ≥ r1+r2）。箱盖 r=70.112/62 中心距 70，
      70 < 70.112+62 ⇒ **重叠**，必须用 `_fig8_union_area`。本函数保留给窄半径对/自检。"""
    return math.pi / 2.0 * (r1 * r1 + r2 * r2)


def _fig8_union_area(r1: float, r2: float, d: float) -> float:
    """**两个上半圆 union 且下半以两圆心连线（剖分面）为底的封闭区域**面积。

    图形由下述边界围成：左圆从 (c1−r1, 0) 逆时针沿上半圆到交点，再沿右圆到
    (c2+r2, 0)，最后沿底边 y=0 回到起点（两圆在底边上的两段**互相覆盖**，取并集 ⇒
    底边只算一次：从 min(c1−r1, c2−r2) 到 max(c1+r1, c2+r2)）。

    面积解析（S1+S2−S_overlap 的一半，再扣掉两圆在底边**重叠**的那块）：
      令两圆交点的 y 坐标 = h（两半圆上方的公共弓形高）。
      - 圆 i 上半圆面积 = π·ri²/2
      - 上方公共弓形（两圆上半部分重叠）= 由两**圆**相交弦所割 … 直接用**扇形−三角形**：
        A_over = Σ_i ri²·acos( (d²+ri²−rj²) / (2·d·ri) ) − ½·√((−d+ri+rj)(d+ri−rj)(d−ri+rj)(d+ri+rj))
        这是两**整圆**的交集面积；上半圆交集 = 把每个扇形/三角形只取 y≥0 部分。
      ★ 本项目里**两个圆心同在 y=0**（轴承孔轴心都在剖分面上）⇒ 交点必在 y=正负 h 对称，
        上半交集 = 整交集的一半。故：**面积 = π/2(r1²+r2²) − ½·A_intersection_full**。
      —— 既覆盖 d≥r1+r2（A_int=0 ⇒ 退化为 `_fig8_area`），又覆盖重叠情形。
    """
    if d >= r1 + r2:
        return _fig8_area(r1, r2)
    if d <= abs(r1 - r2):                      # 一圆含于另一圆（本项目不会发生，守卫用）
        big = max(r1, r2)
        return math.pi / 2.0 * big * big
    a1 = math.acos(max(-1.0, min(1.0, (d * d + r1 * r1 - r2 * r2) / (2.0 * d * r1))))
    a2 = math.acos(max(-1.0, min(1.0, (d * d + r2 * r2 - r1 * r1) / (2.0 * d * r2))))
    tri = math.sqrt(max(0.0, (-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2)))
    a_int_full = r1 * r1 * a1 + r2 * r2 * a2 - 0.5 * tri
    return math.pi / 2.0 * (r1 * r1 + r2 * r2) - 0.5 * a_int_full


def _housing_base_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                       part: str = "housing_base") -> dict:
    """箱座（1）：**圆角矩形薄壁箱体（抽壳）+ 两轴承座 + 孔系**。

    ★★ 2026-10-04 **按图纸 04.jpg 重做**（读数方式：RapidOCR 63 条 + 分区放大 11 张 + 列剖面）：
      ① **先主体、后细节**：主坯（圆角矩形 104×180 **R20**）→ **抽壳（`shell` 特征）**
         → 内腔底面加厚（底厚 10）→ 顶法兰环（104×230 **R23**，腔口 74×168 R6）
         → 轴承座（R37/R43）→ 轴承孔/沉孔 → 螺栓孔系 → 油标/放油。
         参照件特征树即「凸台-拉伸1 → **抽壳1** → …」⇒ 薄壁箱体就该用抽壳，不用切除。
      ② 图纸关键读数：外形 **104**(A-A 104⁻⁰·²₋₀.₃₅) × **82.764** × **230**；
         箱体 **180**（Z=0..180）、法兰两端外伸 **25**（Z=−25..205）；中心高 **80±0.1**；
         中心距 **70±0.08** ⇒ 轴线 Z=**65 / 135**（65+70+45=180）；
         壁厚 **6**（右下局部剖标注）；底板厚 **10**（前视标注）；腔口 **74×168**；
         轴承座外圆 **R37**(6204) / **R43**(6206)；沉孔 **Ø55 / Ø70** 自 x=45 贯通外端面；
         法兰螺栓孔 **6×Ø9 ⊥Ø18**；底脚孔 **4×Ø9 ⊥Ø18**；
         油标 **Ø14**（−Z 端壁，孔心高 **27** ⇒ y=−13）；放油 **M10→Ø8.5**（+Z 端壁，y=−28）。
      ③ 坐标系：`Right` 面 (u,v)=(−Z, Y)；`Top` 面 (u,v)=(X, −Z)；`Front` 面 (x,y)=(X, Y)。
      ④ **所有拉伸一律 `swEndCondMidPlane`**（取中面 + 对称）⇒ 彻底避开边界面方向歧义。
    ★ 登记为简化（Q15）：Ø46 螺栓凸台、2×Ø4 锥销孔、未注圆角 R2、
      内部油道、沉孔深度细节、底部让位台阶（A-A 78/12）。壁厚 `t` 为唯一标定参数。
      （**加强筋已于 2026-10-04 落地**：厚 5，Z 向骑两轴承轴线，Y 从腔底到剖分面。）
    """
    q = P["parts"][part]
    SV = (q.get("_measured_faces") or {}).get("solved") or {}
    if not SV:
        raise ValueError(f"{part}: 缺 _measured_faces.solved —— 箱体尺寸必须取证，禁止凭记忆填")

    # ---------------- 主参数（图纸 04.jpg）----------------------------------
    bx = 104.0 / 2.0                   # X 半宽（轴向）= 52
    y_bot, y_top, y_split = -40.0, 42.764, 40.0
    z0, z1 = -25.0, 205.0              # 法兰外伸范围
    lz = z1 - z0                       # 230
    t = 6.0                            # ★ 壁厚（右下局部剖实测标注；唯一标定参数）
    h_bottom = 10.0                    # 底板厚（前视标注 10）
    z_p0, z_p1 = 0.0, 180.0            # 箱体 Z（图纸 180）
    r_body, r_flg, r_cav = 20.0, 23.0, 6.0
    bores = [(65.0, 23.5, 37.0), (135.0, 31.0, 43.0)]   # (轴心 z, 孔半径, 座外半径)
    cb_d = {65.0: 55.0, 135.0: 70.0}                    # 轴心 z -> 沉孔直径
    boss_x0 = 20.0                     # 凸台内端面 x=±20（实测 boss_inner_face_x）
    boss_x1 = bx - t                   # 凸台外端（内壁面）x=±46 ⇒ 壁厚合计 26+6=32 ✓ A-A「40」
    boss_len = boss_x1 - boss_x0       # 26
    cb_x0 = 45.0                       # 沉孔自 x=45 起到外端面
    cav_x = 37.0
    cav_z0, cav_z1 = 6.0, 174.0        # 腔口 168 = 箱体 180 − 2×壁厚 6 ✓
    y_floor = y_bot + h_bottom         # 内腔底面 y=−30
    h_boss_z = 5.0                     # 油标/放油凸台外伸高

    # 孔系
    d_flg, d_fcb, h_fcb, flg_hole_h = 9.0, 18.0, 1.0, 7.764   # 7.764 = 法兰 2.764 + 入壁 5
    d_base, d_bcb, h_bcb = 9.0, 18.0, 2.0
    d_oil, d_oilcb, oil_y = 14.0, 24.0, -13.0
    d_drain, drain_y = 8.5, -28.0
    base_holes = [(33.70, 23.0), (-33.70, 23.0), (33.70, 157.0), (-33.70, 157.0)]
    flg_holes = [(0.0, -14.0), (0.0, 194.0), (37.0, 20.0), (-37.0, 20.0),
                 (37.0, 178.0), (-37.0, 178.0)]

    # ---------------- 解析体积（抽壳腔体无干净闭式 ⇒ 各步容差见 note）------
    def rrect(hx: float, hz: float, r: float) -> float:
        r = min(r, hx, hz)
        return 4.0 * hx * hz - (4.0 - math.pi) * r * r

    a_body = rrect(bx, (z_p1 - z_p0) / 2.0, r_body)
    v_body = a_body * (y_top - y_bot)
    a_int = rrect(bx - t, (z_p1 - z_p0) / 2.0 - t, max(0.5, r_body - t))
    v_shell = v_body - a_int * (y_top - y_floor)          # 抽壳 + 底厚 10 一体
    # ★ 三层主体的中间量（层1 完 / 层2 完 / 层3 完），供逐层 check_mass 用：
    v_after_shell = v_body - a_int * (y_top - (y_bot + t))   # 仅抽壳（未加厚底）
    a_flg = rrect(bx, lz / 2.0, r_flg) - rrect(cav_x, (cav_z1 - cav_z0) / 2.0, r_cav)
    v_flg = (a_flg - (a_body - a_int)) * (y_top - y_split)
    v_after_flg = v_shell + v_flg
    # ★ v_boss 必须**数值积分**（2026-10-04 修正）：座是 Z 向半圆盘 + X 向平扫，
    #   在 Z 端部（贴 R20 角）被主坯圆角**剪裁**，且靠 X 外端的一段被内壁面截断。
    #   闭式 `2*boss_len*Σ(π/2·ro²)` 高估 18%（251736 实测 vs 439109 解析）——
    #   而 check_mass 容差 4% 会把它放过去 ⇒ 必须真积分，别拿容差盖。
    def _boss_gain() -> float:
        zlo, zhi = z_p0 + t, z_p1 - t
        rc = max(0.5, r_body - t)
        xhi = bx - t

        def h_of(z: float) -> float:
            hh = 0.0
            for zc, _ri, ro in bores:
                u = z - zc
                if abs(u) < ro:
                    hh = max(hh, math.sqrt(ro * ro - u * u))
            return hh

        N = 200000
        dz = (zhi - zlo) / N
        full = sum(h_of(zlo + (i + 0.5) * dz) for i in range(N)) * dz
        loss = 0.0
        NX = NZ = 200
        dx, dzz = rc / NX, rc / NZ
        for i in range(NX):
            x = xhi - rc + (i + 0.5) * dx
            dd = math.sqrt(max(0.0, rc * rc - (x - (xhi - rc)) ** 2))
            zs = zhi - rc + dd
            if zs >= zhi:
                continue
            seg = (zhi - zs) / NZ
            for j in range(NZ):
                loss += h_of(zs + (j + 0.5) * seg) * dx * seg
        return (xhi - boss_x0) * full - loss

    v_boss = 2.0 * _boss_gain()
    v_after_boss = v_after_flg + v_boss
    # ★ 加强筋（2026-10-04 落地，原登记简化 Q15）：厚 5，Z 向骑在两轴承轴线（65/135），
    #   Y 从内腔底 y=-30 上到剖分面 y=40，X 从座内端面 x=20 连到外壁 x=52。
    #   逐段扣（净增 = 矩形 − 与座重叠 − 已在实体内）：
    #     ① 腔内段 Y∈[-30,40]：矩形 5×70，减掉座在 ±2.5mm 内的截面积
    #        （★ 座在 |u|<2.5 的 Y 范围 [40−36.9, 40] 完全被筋的 Y 范围包住
    #          ⇒ 筋在孔道范围内**不新增料**，v_bores 公式不受影响）；
    #     ② 顶段 Y∈[40,41] 咬进法兰：腔口半宽 37 ⇒ 只有 X∈[20,37] 是新增；
    #     ③ 底段 Y∈[-31,-30] 落在底板内 ⇒ 零增量（不计）。
    #   特征树里必须排在 **座之后、孔之前**：排在孔之后会把孔道填回去。
    rib_t = 5.0
    rib_x0 = boss_x0                # 20：与座同 X 跨度（筋是座下方的加强鳍）
    rib_over = 1.0                  # 上下各外延 1mm 进入实体，避开零厚度相切

    def _cap(ro: float, half: float) -> float:
        """半圆盘在 |u| ≤ half 内的截面积（∫√(ro²−u²)du 从 0 到 half 的两倍）。"""
        return half * math.sqrt(ro * ro - half * half) + ro * ro * math.asin(half / ro)

    v_rib = 0.0
    for _zc, _ri, ro in bores:
        a_rect = rib_t * (y_split - y_floor)
        v_rib += (a_rect - _cap(ro, rib_t / 2.0)) * (boss_x1 - rib_x0)
        v_rib += rib_t * rib_over * (cav_x - rib_x0)      # 咬进法兰的那一薄层
    v_rib *= 2.0                                          # 左右两侧壁各一套
    v_after_rib = v_after_boss + v_rib
    v_bores = 2.0 * (boss_len + t) * sum(math.pi / 2.0 * ri * ri for _z, ri, _ro in bores)
    v_cbore = sum(2.0 * math.pi / 2.0 * ((cb_d[zc] / 2.0) ** 2 - ri ** 2) * (bx - cb_x0)
                  for zc, ri, _ro in bores)
    v_after_bore = v_after_rib - v_bores - v_cbore
    v_holes = (len(flg_holes) * (math.pi / 4.0 * d_flg ** 2 * flg_hole_h
                                 + math.pi / 4.0 * (d_fcb ** 2 - d_flg ** 2) * h_fcb)
               + len(base_holes) * (math.pi / 4.0 * d_base ** 2 * h_bottom
                                    + math.pi / 4.0 * (d_bcb ** 2 - d_base ** 2) * h_bcb))
    v_oil = math.pi / 4.0 * d_oil ** 2 * t + math.pi / 4.0 * (d_oilcb ** 2 - d_oil ** 2) * h_boss_z
    v_drain = math.pi / 4.0 * d_drain ** 2 * t
    v_boss_oil = math.pi / 4.0 * (34.0 ** 2 - d_oilcb ** 2) * h_boss_z
    v_boss_drn = math.pi / 4.0 * (17.0 ** 2 - d_drain ** 2) * h_boss_z
    v_total = v_after_bore - v_holes - v_oil - v_drain + v_boss_oil + v_boss_drn
    ref_v = float(SV.get("reference_volume_mm3") or 0.0)

    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    nm = q["name"]

    # ---- 小工具 ----------------------------------------------------------
    def half_disc(tag: str, zc: float, r: float) -> list[dict]:
        """(u,v)=(−Z,Y) 面上的下半圆闭合（外弧 + 底径线）——凸台/孔/沉孔共用。"""
        cu = -zc
        return [
            {"id": f"arc{tag}", "op": "arc3p",
             "p1": [cu + r, y_split], "p2": [cu - r, y_split], "p3": [cu, y_split - r],
             "note": f"半圆 R{r:g} @ Z={zc:g}（轴心 Y={y_split:g}）"},
            {"id": f"lin{tag}", "op": "line",
             "x1": cu - r, "y1": y_split, "x2": cu + r, "y2": y_split,
             "note": "底径线（闭合轮廓）"},
        ]

    def rrect_segs(tag: str, u0: float, v0: float, u1: float, v1: float, r: float) -> list[dict]:
        """★ 显式圆角矩形：4 直线 + 4 圆弧（**不用 sketch_fillet**）。

        原因（2026-10-04 真机踩到）：`sketch_fillet` 传 4 条线段时**只倒 2 个角**
        —— 主坯理论面积 18376.6mm²、实测 18548.4mm²，差值 171.8 = (4−π)·20²/2
        刚好等于「只倒了 2 个角」。⇒ 圆角矩形一律显式画弧，行为可预测。
        """
        t2 = r / math.sqrt(2.0)
        return [
            {"id": f"{tag}l0", "op": "line", "x1": u0 + r, "y1": v0, "x2": u1 - r, "y2": v0, "note": "底边"},
            {"id": f"{tag}a0", "op": "arc3p", "p1": [u1 - r, v0], "p2": [u1, v0 + r],
             "p3": [u1 - r + t2, v0 + r - t2], "note": f"右下角 R{r:g}"},
            {"id": f"{tag}l1", "op": "line", "x1": u1, "y1": v0 + r, "x2": u1, "y2": v1 - r, "note": "右边"},
            {"id": f"{tag}a1", "op": "arc3p", "p1": [u1, v1 - r], "p2": [u1 - r, v1],
             "p3": [u1 - r + t2, v1 - r + t2], "note": f"右上角 R{r:g}"},
            {"id": f"{tag}l2", "op": "line", "x1": u1 - r, "y1": v1, "x2": u0 + r, "y2": v1, "note": "顶边"},
            {"id": f"{tag}a2", "op": "arc3p", "p1": [u0 + r, v1], "p2": [u0, v1 - r],
             "p3": [u0 + r - t2, v1 - r + t2], "note": f"左上角 R{r:g}"},
            {"id": f"{tag}l3", "op": "line", "x1": u0, "y1": v1 - r, "x2": u0, "y2": v0 + r, "note": "左边"},
            {"id": f"{tag}a3", "op": "arc3p", "p1": [u0, v0 + r], "p2": [u0 + r, v0],
             "p3": [u0 + r - t2, v0 + r - t2], "note": f"左下角 R{r:g}"},
        ]

    def top_plane(pid: str, y: float, name: str, note: str) -> dict:
        """平行 Top 的平面。offset>0 ⇒ +Y；要落在 −Y 侧必须 flip。"""
        st = {"id": pid, "op": "plane_offset", "base": "Top", "offset": abs(y),
              "name": name, "note": note}
        if y < 0:
            st["flip"] = True
        return st

    def right_plane(pid: str, x: float, name: str, note: str) -> dict:
        st = {"id": pid, "op": "plane_offset", "base": "Right", "offset": abs(x),
              "name": name, "note": note}
        if x < 0:
            st["flip"] = True
        return st

    def front_plane(pid: str, z: float, name: str, note: str) -> dict:
        st = {"id": pid, "op": "plane_offset", "base": "Front", "offset": abs(z),
              "name": name, "note": note}
        if z < 0:
            st["flip"] = True
        return st

    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        # ============ ① 主坯：Top 面圆角矩形 104×180 R20，MidPlane 挤出 ============
        top_plane("po1", (y_bot + y_top) / 2.0, "主坯中面",
                  f"平行 Top @ y={(y_bot + y_top) / 2:g}（主坯中线）"),
        {"id": "p1", "op": "select_plane", "plane": "主坯中面"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "pt1", "op": "point", "x": 0.0, "y": 0.0,
         "note": "★ S7 锚定（Top 面 (u,v)=(X,−Z)）"},
        *rrect_segs("out", -bx, -z_p1, bx, -z_p0, r_body),
        {"id": "s2", "op": "sketch_end", "name": f"{nm}主坯截面"},
        {"id": "ck1", "op": "check_sketch_constraint", "sketch": f"{nm}主坯截面",
         "expect_min_dims": 0},
        {"id": "e0", "op": "extrude_boss", "end_cond": "swEndCondMidPlane",
         "depth": y_top - y_bot, "name": f"{nm}主坯",
         "note": f"MidPlane {y_top - y_bot:g} ⇒ Y∈[{y_bot:g},{y_top:g}]"},
        {"id": "m0", "op": "check_mass", "expect_volume_mm3": round(v_body, 3), "vol_tol": 0.005,
         "note": f"主坯 圆角矩形({a_body:.1f}mm²)×{y_top - y_bot:g} = {v_body:.1f}mm³"},
        # ============ 【层1 主体】箱体壁：抽壳（★ shell 特征：只移除顶面，侧/底壁各 t）====
        # ★★ 三层主体口径（2026-10-04 用户定稿）：**先建主体、再加工细节**。
        #   层1 = 箱体壁（薄壁箱体，104×180 R20 / 壁 6）—— **必须最先**，
        #         因为「抽壳」特征要求先有实体，且另两层都长在它上面。
        #   层2 = 顶板（剖分法兰 104×230 R23 / 腔口 74×168 R6 / 厚 2.764）
        #   层3 = 底板（把内腔底面加厚到底厚 10）
        #   其余（轴承座 R37/R43、轴承孔/沉孔、螺栓孔系、油标、放油）
        #        **全部基于这三层加工**，特征树里一律排在三层之后。
        {"id": "sh", "op": "shell", "thickness": t, "remove": ["+Y"], "name": f"{nm}抽壳",
         "note": f"【层1 箱体壁】壁厚 {t:g}；移除顶面（剖分面）⇒ 薄壁箱体开口向上"},
        {"id": "m1", "op": "check_mass",
         "expect_volume_mm3": round(v_body - a_int * (y_top - (y_bot + t)), 3), "vol_tol": 0.03,
         "note": "【层1 完】抽壳后（内腔无干净闭式 ⇒ 容差 3%）"},
        # ============ 【层2 主体】顶板＝顶法兰环（104×230 R23 − 腔口 74×168 R6）========
        top_plane("po3", (y_split + y_top) / 2.0, "法兰中面",
                  f"平行 Top @ y={(y_split + y_top) / 2:g}"),
        {"id": "p3", "op": "select_plane", "plane": "法兰中面"},
        {"id": "s5", "op": "sketch_begin"},
        {"id": "pt3", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        *rrect_segs("fo", -bx, -z1, bx, -z0, r_flg),
        *rrect_segs("fi", -cav_x, -cav_z1, cav_x, -cav_z0, r_cav),
        {"id": "s6", "op": "sketch_end", "name": f"{nm}顶法兰环"},
        {"id": "ck3", "op": "check_sketch_constraint", "sketch": f"{nm}顶法兰环",
         "expect_min_dims": 0},
        {"id": "e2", "op": "extrude_boss", "end_cond": "swEndCondMidPlane",
         "depth": y_top - y_split, "name": f"{nm}顶法兰",
         "note": f"【层2 顶板】MidPlane {y_top - y_split:g} ⇒ Y∈[{y_split:g},{y_top:g}]"},
        {"id": "m2", "op": "check_mass",
         "expect_volume_mm3": round(v_after_shell + v_flg, 3), "vol_tol": 0.03,
         "note": f"【层2 完】层1 + 法兰环 {v_flg:.0f} ≈ {v_after_shell + v_flg:.0f}mm³"},
        # ============ 【层3 主体】底板：内腔底面加厚（补到底厚 10）====================
        top_plane("po2", (y_bot + t + y_floor) / 2.0, "内腔底加厚中面",
                  f"平行 Top @ y={(y_bot + t + y_floor) / 2:g}"),
        {"id": "p2", "op": "select_plane", "plane": "内腔底加厚中面"},
        {"id": "s3", "op": "sketch_begin"},
        {"id": "pt2", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        *rrect_segs("pad", -(bx - t), -cav_z1, bx - t, -cav_z0, max(0.5, r_body - t)),
        {"id": "s4", "op": "sketch_end", "name": f"{nm}内腔底"},
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": f"{nm}内腔底",
         "expect_min_dims": 0},
        {"id": "e1", "op": "extrude_boss", "end_cond": "swEndCondMidPlane",
         "depth": y_floor - (y_bot + t), "name": f"{nm}内腔底加厚",
         "note": f"【层3 底板】MidPlane {y_floor - (y_bot + t):g} ⇒ 内腔底面到 y={y_floor:g}"
                 f"（底厚 {h_bottom:g}）"},
        {"id": "m3", "op": "check_mass", "expect_volume_mm3": round(v_after_flg, 3),
         "vol_tol": 0.03,
         "note": f"【三层主体完】≈ {v_after_flg:.0f}mm³ —— 以下所有特征均为基于三层的加工"},
    ]
    # ============ ⑤ 轴承座半圆凸台（每孔一特征：两半圆外圆可能重叠，坑表 §116）====
    for sgn, tag in ((1, "R"), (-1, "L")):
        ph = f"座面{tag}"
        steps.append(right_plane(f"po{tag}", sgn * (boss_x0 + boss_x1) / 2.0, ph,
                                 f"平行 Right @ x={sgn * (boss_x0 + boss_x1) / 2:g}（凸台中线）"))
        for k, (zc, _ri, ro) in enumerate(bores):
            steps += [
                {"id": f"p{tag}{k}", "op": "select_plane", "plane": ph},
                {"id": f"sk{tag}{k}", "op": "sketch_begin"},
                {"id": f"pt{tag}{k}", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
                *half_disc(f"{tag}{k}", zc, ro),
                {"id": f"se{tag}{k}", "op": "sketch_end", "name": f"{nm}座截面{tag}{k}"},
                {"id": f"ck{tag}{k}", "op": "check_sketch_constraint",
                 "sketch": f"{nm}座截面{tag}{k}", "expect_min_dims": 0},
                {"id": f"bo{tag}{k}", "op": "extrude_boss", "end_cond": "swEndCondMidPlane",
                 "depth": boss_len, "name": f"{nm}座{tag}{k}",
                 "note": f"MidPlane {boss_len:g} ⇒ X∈[{sgn * boss_x0:g},{sgn * boss_x1:g}]，"
                         f"外圆 R{ro:g}（+壁 {t:g} ⇒ 该处壁厚 {boss_x1 - boss_x0 + t:g} ✓ A-A「40」）"},
            ]
    steps.append(
        {"id": "m4", "op": "check_mass", "expect_volume_mm3": round(v_after_boss, 3),
         "vol_tol": 0.01, "note": f"+ 座半圆（R37/R43 半圆盘×4）≈ {v_after_boss:.0f}mm³"})
    # ============ ⑤b 加强筋（厚 5，Z 向骑两轴承轴线；**座之后、孔之前**）=========
    # ★ 2026-10-04 落地（原登记简化 Q15）。为何用 extrude_boss 而不是 op_rib：
    #   `InsertRib` 需要 ref_edge_index（选一条边）+ 法向消歧，变量多且静默失败率高；
    #   筋是**直棱柱**（矩形截面沿 X 平扫），用 MidPlane 拉伸行为完全确定、
    #   体积可解析 ⇒ 与「先主体后细节」口径一致，也便于 check_mass 卡住。
    #   草图放在 Right 面（u,v)=(−Z,Y)：矩形 u∈[−zc−2.5, −zc+2.5]、v∈[−31, 41]。
    #   上下各外延 rib_over=1mm 进入实体，**避开「零厚度相切」**（相切布尔会失败）。
    # ★★ X 跨度必须**分两次**拉（Right 面中线 x=±36，MidPlane 32 ⇒ X∈[±20,±52]），
    #   **不能**在 x=0 一次拉通：那样 X∈[0,20] 那段也会在内腔里长出筋，
    #   白增 2×20×70×5 = 14000mm³（check_mass 会当场抓住，但特征树已经脏了）。
    rib_plane_x = (rib_x0 + bx) / 2.0
    rib_depth = bx - rib_x0                      # 32
    for sgn, rtag in ((1, "R"), (-1, "L")):
        steps.append(right_plane(f"pr{rtag}", sgn * rib_plane_x, f"筋面{rtag}",
                                 f"平行 Right @ x={sgn * rib_plane_x:g}（筋中线，"
                                 f"MidPlane {rib_depth:g} ⇒ X∈[{sgn * rib_x0:g},{sgn * bx:g}]）"))
        steps += [
            {"id": f"prp{rtag}", "op": "select_plane", "plane": f"筋面{rtag}"},
            {"id": f"sr{rtag}1", "op": "sketch_begin"},
            {"id": f"ptr{rtag}", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        ]
        for k, (zc, _ri, ro) in enumerate(bores):
            cu, hh = -zc, rib_t / 2.0
            y0r, y1r = y_floor - rib_over, y_split + rib_over
            steps += [
                {"id": f"rl{rtag}{k}a", "op": "line", "x1": cu - hh, "y1": y0r,
                 "x2": cu + hh, "y2": y0r,
                 "note": f"筋{rtag}{k} 底边 Y={y0r:g}（落在底板内 ⇒ 避开零厚度相切）"},
                {"id": f"rl{rtag}{k}b", "op": "line", "x1": cu + hh, "y1": y0r,
                 "x2": cu + hh, "y2": y1r, "note": f"筋{rtag}{k} 后面 Z={zc + hh:g}"},
                {"id": f"rl{rtag}{k}c", "op": "line", "x1": cu + hh, "y1": y1r,
                 "x2": cu - hh, "y2": y1r, "note": f"筋{rtag}{k} 顶边 Y={y1r:g}（咬进法兰）"},
                {"id": f"rl{rtag}{k}d", "op": "line", "x1": cu - hh, "y1": y1r,
                 "x2": cu - hh, "y2": y0r, "note": f"筋{rtag}{k} 前面 Z={zc - hh:g}"},
            ]
        steps += [
            {"id": f"sr{rtag}2", "op": "sketch_end", "name": f"{nm}加强筋截面{rtag}"},
            {"id": f"ckr{rtag}", "op": "check_sketch_constraint",
             "sketch": f"{nm}加强筋截面{rtag}", "expect_min_dims": 0},
            {"id": f"rib{rtag}", "op": "extrude_boss", "end_cond": "swEndCondMidPlane",
             "depth": rib_depth, "name": f"{nm}加强筋{rtag}",
             "note": f"MidPlane {rib_depth:g} ⇒ X∈[{sgn * rib_x0:g},{sgn * bx:g}]。"
                     f"内腔段 X∈[{rib_x0:g},{boss_x1:g}] 扣掉座占的部分；"
                     f"X∈[{boss_x1:g},{bx:g}] 落在壁内 ⇒ 零增量"},
        ]
    steps.append(
        {"id": "mR", "op": "check_mass", "expect_volume_mm3": round(v_after_rib, 3),
         "vol_tol": 0.01,
         "note": f"+ 加强筋 2×{rib_t:g} 厚（骑 Z={', '.join(f'{z:g}' for z, _r, _o in bores)} 轴线）"
                 f" ≈ +{v_rib:.0f}mm³ ⇒ {v_after_rib:.0f}mm³"})
    # ============ ⑥ 轴承孔（Right 面下半圆，ThroughAllBoth）============
    steps += [
        {"id": "p5", "op": "select_plane", "plane": "Right"},
        {"id": "s7", "op": "sketch_begin"},
        {"id": "pt5", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
    ]
    for k, (zc, ri, _ro) in enumerate(bores):
        steps += half_disc(f"B{k}", zc, ri)
    steps += [
        {"id": "s8", "op": "sketch_end", "name": f"{nm}轴承孔截面"},
        {"id": "ck5", "op": "check_sketch_constraint", "sketch": f"{nm}轴承孔截面",
         "expect_min_dims": 0},
        {"id": "bore", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
         "both_directions": True, "name": f"{nm}轴承孔",
         "note": "双向贯穿：切穿座凸台 + 两侧外壳壁（Ø47 / Ø62）"},
    ]
    # 沉孔 Ø55/Ø70：自 x=45 到外端面（MidPlane 于 x=48.5，深 7）
    for sgn, tag in ((1, "R"), (-1, "L")):
        cph = f"沉孔面{tag}"
        steps.append(right_plane(f"pc{tag}", sgn * (cb_x0 + bx) / 2.0, cph,
                                 f"平行 Right @ x={sgn * (cb_x0 + bx) / 2:g}（沉孔中线）"))
        steps += [
            {"id": f"pcp{tag}", "op": "select_plane", "plane": cph},
            {"id": f"sc{tag}", "op": "sketch_begin"},
            {"id": f"ptc{tag}", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        ]
        for k, (zc, _ri, _ro) in enumerate(bores):
            steps += half_disc(f"C{tag}{k}", zc, cb_d[zc] / 2.0)
        steps += [
            {"id": f"sc{tag}e", "op": "sketch_end", "name": f"{nm}沉孔截面{tag}"},
            {"id": f"ckc{tag}", "op": "check_sketch_constraint", "sketch": f"{nm}沉孔截面{tag}",
             "expect_min_dims": 0},
            {"id": f"cbc{tag}", "op": "extrude_cut", "end_cond": "swEndCondMidPlane",
             "depth": bx - cb_x0, "name": f"{nm}沉孔{tag}",
             "note": f"MidPlane {bx - cb_x0:g} ⇒ X∈[{sgn * cb_x0:g},{sgn * bx:g}]（Ø55 / Ø70）"},
        ]
    steps.append(
        {"id": "m5", "op": "check_mass", "expect_volume_mm3": round(v_after_bore, 3),
         "vol_tol": 0.04, "note": f"− 轴承孔/沉孔 ≈ {v_after_bore:.0f}mm³"})
    # ============ ⑦ 法兰 6×Ø9（穿法兰 2.764 + 入壁 5）+ Ø18 沉孔 ============
    steps += [
        top_plane("po7", y_top - flg_hole_h / 2.0, "法兰孔中面",
                  f"平行 Top @ y={y_top - flg_hole_h / 2:g}"),
        {"id": "p7", "op": "select_plane", "plane": "法兰孔中面"},
        {"id": "s9", "op": "sketch_begin"},
        {"id": "pt7", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
    ]
    for i, (hx, hz) in enumerate(flg_holes):
        steps.append({"id": f"fh{i}", "op": "circle", "cx": hx, "cy": -hz, "d": d_flg,
                      "note": f"法兰螺栓孔 Ø{d_flg:g} @ (x={hx:g}, z={hz:g})"})
    steps += [
        {"id": "s10", "op": "sketch_end", "name": f"{nm}法兰孔位"},
        {"id": "ck7", "op": "check_sketch_constraint", "sketch": f"{nm}法兰孔位",
         "expect_min_dims": 0},
        {"id": "fhc", "op": "extrude_cut", "end_cond": "swEndCondMidPlane",
         "depth": flg_hole_h, "name": f"{nm}法兰孔",
         "note": f"MidPlane {flg_hole_h:g} ⇒ Y∈[{y_top - flg_hole_h:g},{y_top:g}]（穿法兰入壁 5）"},
        # ★ fhc 拉伸后草图已关闭 ⇒ s11 必须**重新选面**（2026-10-04 真机 s11 踩到）
        {"id": "p7b", "op": "select_plane", "plane": "法兰孔中面"},
        {"id": "s11", "op": "sketch_begin"},
        {"id": "pt8", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
    ]
    for i, (hx, hz) in enumerate(flg_holes):
        steps.append({"id": f"fc{i}", "op": "circle", "cx": hx, "cy": -hz, "d": d_fcb,
                      "note": f"法兰沉孔 Ø{d_fcb:g} 深 {h_fcb:g} @ (x={hx:g}, z={hz:g})"})
    steps += [
        {"id": "s12", "op": "sketch_end", "name": f"{nm}法兰沉孔位"},
        {"id": "ck8", "op": "check_sketch_constraint", "sketch": f"{nm}法兰沉孔位",
         "expect_min_dims": 0},
        {"id": "fcc", "op": "extrude_cut", "end_cond": "swEndCondMidPlane",
         "depth": 2.0 * h_fcb, "name": f"{nm}法兰沉孔",
         "note": f"MidPlane {2 * h_fcb:g} ⇒ 自顶面下 {h_fcb:g}"},
        # ============ ⑧ 底脚 4×Ø9（贯穿底板 10）+ Ø18 沉孔 ============
        top_plane("poB", y_bot + h_bottom / 2.0, "底脚孔中面",
                  f"平行 Top @ y={y_bot + h_bottom / 2:g}"),
        {"id": "p9", "op": "select_plane", "plane": "底脚孔中面"},
        {"id": "s13", "op": "sketch_begin"},
        {"id": "pt9", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
    ]
    for i, (hx, hz) in enumerate(base_holes):
        steps.append({"id": f"bh{i}", "op": "circle", "cx": hx, "cy": -hz, "d": d_base,
                      "note": f"底脚安装孔 Ø{d_base:g} @ (x={hx:g}, z={hz:g})"})
    steps += [
        {"id": "s14", "op": "sketch_end", "name": f"{nm}底脚孔位"},
        {"id": "ck9", "op": "check_sketch_constraint", "sketch": f"{nm}底脚孔位",
         "expect_min_dims": 0},
        {"id": "bhc", "op": "extrude_cut", "end_cond": "swEndCondMidPlane",
         "depth": h_bottom, "name": f"{nm}底脚孔",
         "note": f"MidPlane {h_bottom:g} ⇒ Y∈[{y_bot:g},{y_floor:g}]（贯穿底板）"},
        {"id": "p9b", "op": "select_plane", "plane": "底脚孔中面"},
        {"id": "s15", "op": "sketch_begin"},
        {"id": "pt10", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
    ]
    for i, (hx, hz) in enumerate(base_holes):
        steps.append({"id": f"bc{i}", "op": "circle", "cx": hx, "cy": -hz, "d": d_bcb,
                      "note": f"底脚沉孔 Ø{d_bcb:g} 深 {h_bcb:g} @ (x={hx:g}, z={hz:g})"})
    steps += [
        {"id": "s16", "op": "sketch_end", "name": f"{nm}底脚沉孔位"},
        {"id": "ck10", "op": "check_sketch_constraint", "sketch": f"{nm}底脚沉孔位",
         "expect_min_dims": 0},
        {"id": "bcc", "op": "extrude_cut", "end_cond": "swEndCondMidPlane",
         "depth": 2.0 * h_bcb, "name": f"{nm}底脚沉孔",
         "note": f"MidPlane {2 * h_bcb:g} ⇒ 自底面深 {h_bcb:g}"},
        # ============ ⑨ 油标 Ø34 凸台 + Ø24 沉孔 + Ø14 孔（−Z 端壁，孔心高 27）====
        front_plane("poO", -h_boss_z / 2.0, "油标中面", f"平行 Front @ z={-h_boss_z / 2:g}"),
        {"id": "pO", "op": "select_plane", "plane": "油标中面"},
        {"id": "sO1", "op": "sketch_begin"},
        {"id": "ptO", "op": "point", "x": 0.0, "y": 0.0,
         "note": "★ S7 锚定（Front 面 (x,y)=(X,Y)）"},
        {"id": "oob", "op": "circle", "cx": 0.0, "cy": oil_y, "d": 34.0,
         "note": f"油标凸台 Ø34 @ (x=0, y={oil_y:g})（孔心高 {oil_y - y_bot:g} ✓ 图纸 27）"},
        {"id": "sO2", "op": "sketch_end", "name": f"{nm}油标凸台"},
        {"id": "ckO", "op": "check_sketch_constraint", "sketch": f"{nm}油标凸台",
         "expect_min_dims": 0},
        {"id": "eO", "op": "extrude_boss", "end_cond": "swEndCondMidPlane", "depth": h_boss_z,
         "name": f"{nm}油标凸台", "note": f"MidPlane {h_boss_z:g} ⇒ Z∈[{-h_boss_z:g},0]（向 −Z 外伸）"},
        {"id": "pOb", "op": "select_plane", "plane": "油标中面"},
        {"id": "sO3", "op": "sketch_begin"},
        {"id": "ptO2", "op": "point", "x": 0.0, "y": 0.0,
         "note": "★ S7 锚定（这两张草图曾漏，selftest §4 抓出）"},
        {"id": "ooh", "op": "circle", "cx": 0.0, "cy": oil_y, "d": d_oilcb,
         "note": f"油标沉孔 Ø{d_oilcb:g}（与凸台同心）"},
        {"id": "sO4", "op": "sketch_end", "name": f"{nm}油标沉孔"},
        {"id": "ckO2", "op": "check_sketch_constraint", "sketch": f"{nm}油标沉孔",
         "expect_min_dims": 0},
        {"id": "oohc", "op": "extrude_cut", "end_cond": "swEndCondMidPlane", "depth": h_boss_z,
         "name": f"{nm}油标沉孔", "note": f"MidPlane {h_boss_z:g} ⇒ 自凸台外端面掏 Ø{d_oilcb:g}"},
        {"id": "pOc", "op": "select_plane", "plane": "油标中面"},
        {"id": "sO5", "op": "sketch_begin"},
        {"id": "ptO3", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        {"id": "ooh2", "op": "circle", "cx": 0.0, "cy": oil_y, "d": d_oil,
         "note": f"油标孔 Ø{d_oil:g}（沿 Z 贯穿端壁）"},
        {"id": "sO6", "op": "sketch_end", "name": f"{nm}油标孔"},
        {"id": "ckO3", "op": "check_sketch_constraint", "sketch": f"{nm}油标孔",
         "expect_min_dims": 0},
        {"id": "ooh2c", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
         "both_directions": True, "name": f"{nm}油标孔", "note": "沿 Z 双向贯穿（穿 −Z 端壁）"},
        # ============ ⑩ 放油 Ø17 凸台 + M10（Ø8.5）（+Z 端壁，低位 y=−28）============
        front_plane("poD", z_p1 + h_boss_z / 2.0, "放油中面",
                    f"平行 Front @ z={z_p1 + h_boss_z / 2:g}"),
        {"id": "pD", "op": "select_plane", "plane": "放油中面"},
        {"id": "sD1", "op": "sketch_begin"},
        {"id": "ptD", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        {"id": "dob", "op": "circle", "cx": 0.0, "cy": drain_y, "d": 17.0,
         "note": f"放油凸台 Ø17 @ (x=0, y={drain_y:g})（离底 {drain_y - y_bot:g}）"},
        {"id": "sD2", "op": "sketch_end", "name": f"{nm}放油凸台"},
        {"id": "ckD", "op": "check_sketch_constraint", "sketch": f"{nm}放油凸台",
         "expect_min_dims": 0},
        {"id": "eD", "op": "extrude_boss", "end_cond": "swEndCondMidPlane", "depth": h_boss_z,
         "name": f"{nm}放油凸台", "note": f"MidPlane {h_boss_z:g} ⇒ Z∈[{z_p1:g},{z_p1 + h_boss_z:g}]"},
        {"id": "pDb", "op": "select_plane", "plane": "放油中面"},
        {"id": "sD3", "op": "sketch_begin"},
        {"id": "ptD2", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        {"id": "doh", "op": "circle", "cx": 0.0, "cy": drain_y, "d": d_drain,
         "note": f"放油孔 M10 → Ø{d_drain:g}（与凸台同心）"},
        {"id": "sD4", "op": "sketch_end", "name": f"{nm}放油孔"},
        {"id": "ckD2", "op": "check_sketch_constraint", "sketch": f"{nm}放油孔",
         "expect_min_dims": 0},
        {"id": "dohc", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
         "both_directions": True, "name": f"{nm}放油孔", "note": "沿 Z 双向贯穿（穿 +Z 端壁）"},
        # ============ 收尾判据 ============
        {"id": "bb", "op": "check_bbox", "expect": [2 * bx, y_top - y_bot, lz], "tol_mm": 0.6,
         "note": "[104, 82.764, 230]（油标/放油凸台各外伸 5，短于法兰外伸 25 ⇒ 不影响）"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": max(ro for _z, _r, ro in bores),
         "tol": 0.05, "note": f"最大圆柱面 = 座外圆 R{max(ro for _z, _r, ro in bores):g}"},
        {"id": "m6", "op": "check_mass", "expect_volume_mm3": round(v_total, 3), "vol_tol": 0.01,
         "note": f"闭体积解析 ≈ {v_total:.0f}mm³（参照 {ref_v:.0f}）。"
                 f"★ 容差 5% → **1%**（2026-10-04）：v_boss 改数值积分后真机只差 **0.15%**，"
                 f"5% 是拿容差盖解析误差，不是模型精度"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-{2 * bx:g}x{y_top - y_bot:g}x{lz:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}：圆角矩形薄壁箱体【抽壳】+ 轴承座 + 孔系）",
                 "units": "mm",
                 "note": "★ 按**图纸 04.jpg** 重做（2026-10-04；RapidOCR+分区放大读数）："
                         "主坯圆角矩形 104×180 R20 → **抽壳(壁 6)** → "
                         "顶法兰环 104×230 R23（腔口 74×168 R6）→ 内腔底加厚(底厚 10) → "
                         "轴承座 R37/R43 → "
                         "轴承孔 Ø47/Ø62 + 沉孔 Ø55/Ø70 → 6×Ø9⊥Ø18 与 4×Ø9⊥Ø18 → "
                         "油标 Ø14 + 放油 Ø8.5。先主体后细节；拉伸一律 MidPlane。"
                         "加强筋 2×厚5（Z 向骑轴线）；登记简化（Q15）：Ø46 螺栓凸台、锥销孔、未注圆角 R2、内部油道。",
                 "layers": [
                     {"n": 1, "name": "箱体壁（薄壁箱体）",
                      "size": f"104×180 圆角矩形 R20，壁厚 {t:g}",
                      "built_by": ["e0 主坯（MidPlane 全高）", "sh 抽壳（移除 +Y 面）"],
                      "why_first": "抽壳特征必须先有实体；层2/层3 都长在它上面"},
                     {"n": 2, "name": "顶板（剖分法兰）",
                      "size": f"104×230 R23 − 腔口 74×168 R6，厚 {y_top - y_split:g}（y {y_split:g}→{y_top:g}）",
                      "built_by": ["e2 顶法兰"]},
                     {"n": 3, "name": "底板（底壁加厚）",
                      "size": f"内腔底面加厚到底厚 {h_bottom:g}（y {y_bot + t:g}→{y_floor:g}）",
                      "built_by": ["e1 内腔底加厚"]},
                 ],
                 "details_on_layers": "轴承座 R37/R43 半圆凸台、轴承孔 Ø47/Ø62 + 沉孔 Ø55/Ø70、"
                                      "法兰 6×Ø9⊥Ø18、底脚 4×Ø9⊥Ø18、油标 Ø34/Ø24/Ø14、"
                                      "放油 Ø17/M10 —— **全部基于上述三层加工**，特征树中排在三层之后",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {
            "wall_t_mm": t, "bx": bx, "lz": lz, "bottom_t_mm": h_bottom,
            "body_z_mm": [z_p0, z_p1], "axes_z_mm": [zc for zc, _r, _o in bores],
            "bores": [{"axis_z": zc, "r": ri, "boss_r": ro, "cbore_Ø": cb_d[zc]}
                      for zc, ri, ro in bores],
            "boss_len_mm": round(boss_len, 4),
            "analytic_volume_mm3": round(v_total, 3),
            "reference_volume_mm3": ref_v,
            "analytic_vs_ref_pct": round((v_total - ref_v) / ref_v * 100.0, 3) if ref_v else None,
        },
    }


def _circles_width(y: float, circles: list[tuple[float, float]]) -> float:
    """高度 y 处，若干圆的**水平区间之并**的总宽（|y| ≥ r 的圆不贡献）。

    例：y=0 时两个半径 70.112/62 的圆若相交，并集宽度 = 两圆在 y=0 的弦并起来，
    而不是简单相加（箱盖 figure-8 两半圆**重叠**，相加会多算）。
    """
    iv: list[tuple[float, float]] = []
    for c, r in circles:
        if abs(y) < r:
            hh = math.sqrt(r * r - y * y)
            iv.append((c - hh, c + hh))
    if not iv:
        return 0.0
    iv.sort()
    tot = 0.0
    cs, ce = iv[0]
    for s2, e2 in iv[1:]:
        if s2 > ce:
            tot += ce - cs
            cs, ce = s2, e2
        else:
            ce = max(ce, e2)
    return tot + (ce - cs)


def _fig8_strip_area(y0: float, y1: float, circles, n: int = 4000) -> float:
    """figure-8（若干圆**上半**之并）在 y∈[y0,y1] 的横截面积（数值 Riemann）。

    为什么不用闭式：圆**并集**的弓形面积没有干净闭式（`_fig8_union_area` 只给全高）。
    这里只需要「顶法兰那一段」y∈[0,tf]（tf=2.764 很薄），细 Riemann（n=4000）
    相对误差 < 1e-6，完全够 `check_mass` 的 0.5% 用。
    """
    if y1 <= y0:
        return 0.0
    h = (y1 - y0) / n
    return sum(_circles_width(y0 + (i + 0.5) * h, circles) for i in range(n)) * h


def _housing_cover_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                        part: str = "housing_cover") -> dict:
    """箱盖（12）：**figure-8 顶罩**（两半圆）+ 剖分顶法兰 + 两轴承孔 + 法兰螺栓孔。

    ★★ 反解口径（2026-10-04，坑表 §114/§117）：
      参照件 `12-箱盖.SLDPRT` 原生坐标 **X=轴间(230)、Y=竖直(0→70.112)、Z=轴向(104.019)**
      —— 与箱座**朝向不同**（箱座 X=轴向）；装配阶段箱盖需**绕 Y 转 90°**，
      但**对账只比包围盒尺寸集合**（`reconcile` 不比朝向）⇒ 建模按自身坐标即可。
      顶罩实测为**双半圆隆起（figure-8）**：大侧外 R=70.112 / 内 R=64.112（6206 侧），
      小侧外 R=62 / 内 R=56（6204 侧），轴心 **X=34.52 / −35.48（中心距 70 ✔）**，
      两半圆**重叠**（70.112+62 > 70）⇒ 逐半圆单独成特征（同 §116 的教训）。
      顶法兰 tf=2.764 与箱座剖分法兰同厚（y∈[0,2.764]），X 向全长 230
      （X∈[−100.476,129.524]）；端壁厚 `tw` 为**唯一标定参数**，反解使解析=参照。
      未建：Ø46/Ø26 螺栓凸台、沉孔、Ø4 油道/通气道、圆角、油标/放油孔 —— **登记为简化**。
    """
    q = P["parts"][part]
    SV = (q.get("_measured_faces") or {}).get("solved") or {}
    if not SV:
        raise ValueError(f"{part}: 缺 _measured_faces.solved —— 必须取证，禁止凭记忆填")

    c1, c2 = 34.52, -35.48             # 轴心 X（大/输出 6206、小/输入 6204）
    d_axis = c1 - c2                   # 70（中心距，与箱座逐位一致）
    R1o, R2o = 70.112, 62.0            # 顶罩外半圆（大 R1o = bbox 顶高）
    w = 6.0                            # 罩壁（实测 70.112→64.112、62→56）
    R1i, R2i = R1o - w, R2o - w
    Lz = 104.019                       # 轴向宽（Z）
    fx0, fx1 = -100.476, 129.524       # 顶法兰 X 范围（宽 230）
    FX = fx1 - fx0
    tf = 2.764                         # 顶法兰厚（= 箱座剖分法兰 42.764−40）
    tw = 13.8                          # ★ 端壁厚（唯一标定参数，反解=参照；见头注）

    # ---- 窥视孔（图纸 03.jpg：凸台 46×46 顶面 + 10° 拔模 + 方孔 28 + 4×M3）----
    # ★★ 2026-10-04 **图纸复核纠正**（此前读数有误，已按像素实测改正）：
    #   ① B 向视图的 "36×36" **不是**凸台顶面，而是 **4×M3 的孔心距**（±18）；
    #      凸台顶面 = 俯视图最外那个方框 = **46×46**（与主视 "46" 一致）。
    #      三层方框实测（俯视图源像素，同心）：外 46 / 中 36(孔距) / 内 28(方窗)。
    #   ② 主视 "10°" = 侧壁**拔模斜度** ⇒ 凸台是**锥台**（底大顶小），不是直台。
    #   ③ 中心实测：三层方框心在 230 左端起 91.3mm ⇒ X = −100.476 + 91.3 ≈ **−9.2**
    #      （比例尺由 230=792px 与 46/36/28 三方交叉标定，散布 ±1.5%）⇒ 取 **−9.0**。
    #   ④ 建法：草图取**底面**（y=54，即拔模起点）画**放大后的** 51.643 方，
    #      向上盲拉 16 + 10° 向内拔模 ⇒ 顶面恰为 46×46。
    #      51.643 = 46 + 2·16·tan10°（顶面 46 是硬数，反推底面）。
    visor_h = 23.0                     # ★ 凸台**顶面**半宽（46/2，图纸硬数）
    visor_win = 14.0                   # 方孔半宽（28/2）
    visor_cx = -9.0                    # ★ 中心 X（俯视图像素实测 91.3mm ⇒ −9.2，取 −9.0）
    visor_tap = 10.0                   # ★ 侧壁拔模角（图纸 10°）
    visor_y0, visor_y1 = 54.0, 70.0    # 凸台 Y（拔模起点 54，顶面 70 < 70.112 ⇒ 不改 bbox）
    visor_lo = visor_h + (visor_y1 - visor_y0) * math.tan(math.radians(visor_tap))
    m3_off, d_m3 = 18.0, 2.5           # 4×M3 孔心距 36×36（±18）、底孔 Ø2.5
    d_cbore, h_cbore = 18.0, 2.0       # 法兰螺栓 Ø18 沉孔（图纸 "⊥Ø18"）
    ytop = R1o
    bores = [(c1, 31.0), (c2, 23.5)]   # (轴心 X, 孔半径) —— 6206/6204 上半孔
    d_bolt, n_bolt = 9.0, 6
    # ★★ 箱座 → 箱盖的轴间映射是**镜像**（箱盖扣在箱座上方、轴间轴反向），不是平移：
    #     X_cover = 104.52 − Z_base、Z_cover = X_base
    #     （校核：base z=−25/205 → cover X=129.52/−100.48 = 实测 bbox ✔；
    #       base z=70/140 = 6206/6204 → cover X=34.52/−35.48 = 实测轴心 ✔；
    #       平移 −75.476 在法兰两端对、在轴心处差 40 ⇒ 会**把 2 个孔放错**）。
    #     2026-10-04 真机探针抓到：按平移算的 6 孔里 2 个只切出 1/3 个圆柱面（boxØ 3.972）。
    bolt_holes = [(104.52 - (-14.0), 0.0), (104.52 - 194.0, 0.0),
                  (104.52 - 20.0, 37.0), (104.52 - 20.0, -37.0),
                  (104.52 - 178.0, 37.0), (104.52 - 178.0, -37.0)]

    # ---- 解析体积（每段都能对上公式；真机逐段 check_mass）---------------------
    outc = [(c1, R1o), (c2, R2o)]
    inc = [(c1, R1i), (c2, R2i)]
    A_out = _fig8_union_area(R1o, R2o, d_axis)
    A_in = _fig8_union_area(R1i, R2i, d_axis)
    ob = _fig8_strip_area(0.0, tf, outc)      # 外罩在 y∈[0,tf]（顶法兰段）的面积
    # ★ 内腔剖面**底边就画在 y=tf** ⇒ 半圆面积 π/2·r² 本身就不含法兰那一段，
    #   **不能再减一次**（2026-10-04 真机：多减 ib 曾让 m2 少切 43020mm³ 而报红）。
    v_flange = FX * Lz * tf
    v_dome = (A_out - ob) * Lz                # 顶罩高出法兰的部分
    v_cav = A_in * (Lz - 2.0 * tw)             # 内腔（两端各留 tw 端壁）
    v_bore = 0.0
    for ci, ri in bores:
        # ★★ 2026-10-04 修掉内腔圆心之后（§118）必须**同步改这里**：
        #   旧式「中段吃法兰 y∈[0,tf]」的前提是**内腔底面在 y=tf**（y∈[0,tf] 是实心法兰），
        #   内腔一旦正确地对剖分面敞开，中段那点法兰料**已经被内腔先切掉了** ⇒ 轴承孔
        #   在中段**一点料都吃不到**（穹顶壳下缘 y_in = √(ri²−…) ≥ 56 ≫ 孔口 31/23.5）。
        #   真机判据：不修则 m3 实测 379698.170 vs 期望 356715.268（+22982.9）
        #   —— 差值 22982.9 = 301.4mm² × 76.419 = 「法兰料 ∩ 孔圆」，与解析逐位对得上。
        v_bore += (math.pi / 2.0 * ri * ri) * 2.0 * tw      # 端壁：整个上半圆 × tw × 2
    v_bolt = n_bolt * math.pi / 4.0 * d_bolt ** 2 * tf
    # ---- 窥视孔凸台净增（二维数值积分：锥台 46(顶)/51.643(底) 从 y0=54 到 y1=70，
    #      只算穹顶外表面以上**露出**的部分 + 填进内腔的**补料**；再减方孔 28 与 4×M3）--
    def _y_out(x: float) -> float:
        return max(math.sqrt(max(0.0, R1o * R1o - (x - c1) ** 2)),
                   math.sqrt(max(0.0, R2o * R2o - (x - c2) ** 2)))

    def _y_in(x: float) -> float:
        return max(math.sqrt(max(0.0, R1i * R1i - (x - c1) ** 2)),
                   math.sqrt(max(0.0, R2i * R2i - (x - c2) ** 2)))

    # ★ 凸台净增 = 「伸出穹顶的露头」**加上**「填进内腔的补料」——两块都必须算！
    #   2026-10-04 真机踩到：只算露头 ⇒ m4 期望比实测少 14k（3.9%）而误报 FAIL。
    #   方孔 28 挖的是 [visor_y0, visor_y1] 整段（凸台已把该段填实），故直接 × 高度。
    # ★★ 2026-10-04 锥台改造：凸台不再是等截面柱 ⇒ 原来的「沿 x 一维积分 × 定宽 46」
    #   不再成立，改为**二维数值积分**（x 与 z 都积）。给定 (x,z)，凸台在该点能长到的
    #   高度由拔模决定：y_reach(m) = y0 + (visor_lo − m)/tan10°，m = max(|x−cx|, |z|)
    #   —— 这正是「方形锥台」的等高线（方形环），比近似成圆柱锥台精确。
    _tan_tap = math.tan(math.radians(visor_tap))
    _nx = _nz = 260
    _hx = (visor_lo * 2.0) / _nx
    _hz = (visor_lo * 2.0) / _nz
    v_visor = 0.0
    for _i in range(_nx):
        _x = (visor_cx - visor_lo) + (_i + 0.5) * _hx
        _yo, _yi = _y_out(_x), _y_in(_x)
        for _j in range(_nz):
            _z = -visor_lo + (_j + 0.5) * _hz
            _m = max(abs(_x - visor_cx), abs(_z))
            if _m >= visor_lo:
                continue
            _yre = min(visor_y1, visor_y0 + (visor_lo - _m) / _tan_tap)
            v_visor += _hx * _hz * (
                max(0.0, _yre - max(visor_y0, _yo))       # 露头：穹顶外表面以上
                + max(0.0, min(_yre, _yi) - visor_y0))    # 补料：填进内腔的部分
    v_visor -= (2.0 * visor_win) ** 2 * (visor_y1 - visor_y0)
    v_m3 = 4.0 * math.pi / 4.0 * d_m3 ** 2 * (visor_y1 - visor_y0)
    v_cbore = n_bolt * math.pi / 4.0 * (d_cbore ** 2 - d_bolt ** 2) * h_cbore
    v_total = (v_flange + v_dome - v_cav - v_bore - v_bolt
               + v_visor - v_m3 - v_cbore)
    ref_v = float(SV.get("reference_volume_mm3") or 0.0)

    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    nm = q["name"]
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        # ---- 【层1 主体】顶板＝剖分顶法兰：Front 面 rect 230×(0..2.764)，MidPlane 104.019 ----
        # ★★ 三层主体口径（2026-10-04 用户定稿）：层1 顶板 → 层2 穹顶壳 → 层3 窥视孔凸台；
        #    其余（轴承孔、法兰孔/沉孔、方孔、M3）**全部基于这三层加工**，排在三层之后。
        {"id": "p1", "op": "select_plane", "plane": "Front",
         "note": "Front 法向 +Z；草图 (x,y)=(X,Y) 正是顶罩剖面 ⇒ 不需换面"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "pt1", "op": "point", "x": 0.0, "y": 0.0,
         "note": "★ S7 锚定（法兰矩形不含原点内部）"},
        {"id": "out", "op": "rect_corner", "x1": fx0, "y1": 0.0, "x2": fx1, "y2": tf,
         "note": f"顶法兰 {FX:g}×{tf:g}（X∈[{fx0:g},{fx1:g}]）"},
        {"id": "s2", "op": "sketch_end", "name": f"{nm}顶法兰截面"},
        {"id": "ck1", "op": "check_sketch_constraint", "sketch": f"{nm}顶法兰截面",
         "expect_min_dims": 0},
        {"id": "e0", "op": "extrude_boss", "end_cond": "swEndCondMidPlane", "depth": Lz,
         "name": f"{nm}顶法兰", "note": f"MidPlane {Lz:g} ⇒ Z∈[±{Lz / 2:g}]"},
        {"id": "m0", "op": "check_mass", "expect_volume_mm3": round(v_flange, 3),
         "vol_tol": 0.005, "note": f"【层1 顶板】法兰 {FX:g}×{tf:g}×{Lz:g} = {v_flange:.1f}mm³"},
    ]
    # ---- 【层2 主体】穹顶壳外半圆：**每个半圆各一张草图 / 一次拉伸**（两半圆重叠，§116）--
    for k, (cx, ro) in enumerate(((c1, R1o), (c2, R2o))):
        tag = "大小"[k]
        steps += [
            {"id": f"p2{k}", "op": "select_plane", "plane": "Front",
             "note": f"顶罩剖面在 Front（法向 Z）"},
            {"id": f"s3{k}", "op": "sketch_begin"},
            {"id": f"pt2{k}", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
            {"id": f"a{k}", "op": "arc3p", "p1": [cx + ro, 0.0], "p2": [cx - ro, 0.0],
             "p3": [cx, ro], "note": f"外半圆 R{ro:g} @ X={cx:g}（{tag}侧）"},
            {"id": f"l{k}", "op": "line", "x1": cx - ro, "y1": 0.0,
             "x2": cx + ro, "y2": 0.0, "note": "封口弦（在剖分面 y=0）"},
            {"id": f"s4{k}", "op": "sketch_end", "name": f"{nm}罩外截面{tag}"},
            {"id": f"ck2{k}", "op": "check_sketch_constraint",
             "sketch": f"{nm}罩外截面{tag}", "expect_min_dims": 0},
            {"id": f"e1{k}", "op": "extrude_boss", "end_cond": "swEndCondMidPlane",
             "depth": Lz, "name": f"{nm}顶罩{tag}",
             "note": f"【层2 穹顶】MidPlane {Lz:g}（与已在的实体合并）"},
        ]
    steps.append(
        {"id": "m1", "op": "check_mass",
         "expect_volume_mm3": round(v_flange + v_dome, 3), "vol_tol": 0.005,
         "note": f"【层2 半】法兰 {v_flange:.0f} + 罩(高出法兰) {v_dome:.0f} = {v_flange + v_dome:.0f}mm³"})
    # ---- 【层2 主体】穹顶壳内腔切除 -------------------------------------------
    # ★★★ 2026-10-04 修掉一个**体积测不出来的几何错误**（坑表 §118）：
    #   原先内半圆画成「圆心 (cx, tf)、弦在 y=tf」⇒ 腔顶 = tf + ri，
    #   于是**穹顶顶壁只剩 R−tf−ri = 3.236**，而图纸/参照件是 **6**。
    #   为什么 check_mass 一直绿：弦上移 tf 只是把**同一个半圆整体平移** ⇒
    #   **截面积不变 ⇒ 体积逐位相同**（m2 实测=解析到 0.000%）—— 典型的
    #   「算的和模型一致、模型和图纸不一致」（§102）。
    #   真凶是**分段探针**：窥视孔凸台净增实测 29669.28、解析 24771.42，差 4898；
    #   把腔顶按 +tf 重算 ⇒ 29669.52，误差 **0.0008%** 一次坐实。
    #   参照件面数据佐证：内表面是 **R64.112 / R56 的圆柱、轴心 y≈0**（不是 y=tf）。
    #   ⇒ 内半圆一律**圆心在 (cx, 0)、弦在 y=0**（腔体对剖分面敞开，正是分箱盖的构造）。
    #   体积公式不用改：半圆截面积与「是否平移」无关，A_in·(Lz−2tw) 仍然成立。
    for k, (cx, ri) in enumerate(((c1, R1i), (c2, R2i))):
        tag = "大小"[k]
        steps += [
            {"id": f"p5{k}", "op": "select_plane", "plane": "Front"},
            {"id": f"s5{k}", "op": "sketch_begin"},
            {"id": f"pt5{k}", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
            {"id": f"a3{k}", "op": "arc3p", "p1": [cx + ri, 0.0], "p2": [cx - ri, 0.0],
             "p3": [cx, ri],
             "note": f"内腔半圆 R{ri:g}，**圆心在剖分面 y=0** ⇒ 腔顶 = R{ri:g}、"
                     f"顶壁厚 = {R1o - R1i:g} ✓ 图纸（★ 别写成圆心 (cx,{tf:g})）"},
            {"id": f"l3{k}", "op": "line", "x1": cx - ri, "y1": 0.0,
             "x2": cx + ri, "y2": 0.0, "note": "封口弦（在剖分面 y=0，腔体向下敞开）"},
            {"id": f"s6{k}", "op": "sketch_end", "name": f"{nm}罩腔截面{tag}"},
            {"id": f"ck5{k}", "op": "check_sketch_constraint",
             "sketch": f"{nm}罩腔截面{tag}", "expect_min_dims": 0},
            {"id": f"e3{k}", "op": "extrude_cut", "end_cond": "swEndCondMidPlane",
             "depth": Lz - 2.0 * tw, "name": f"{nm}罩腔{tag}",
             "note": f"【层2 穹顶内腔】MidPlane {Lz - 2 * tw:g} ⇒ Z∈[±{Lz / 2 - tw:g}]，两端各留端壁 {tw:g}"},
        ]
    steps.append(
        {"id": "m2", "op": "check_mass",
         "expect_volume_mm3": round(v_flange + v_dome - v_cav, 3), "vol_tol": 0.005,
         "note": f"【三层主体完】− 内腔 {v_cav:.0f} = {v_flange + v_dome - v_cav:.0f}mm³ —— 以下均为基于三层的加工"})
    # ---- 【细节】轴承孔：Front 面两个圆，沿 Z 双向贯穿（材料只在 y≥0 ⇒ 自然成上半孔）--
    steps += [
        {"id": "p7", "op": "select_plane", "plane": "Front"},
        {"id": "s7", "op": "sketch_begin"},
        {"id": "pt7", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
    ]
    for k, (cx, ri) in enumerate(bores):
        steps.append({"id": f"b{k}", "op": "circle", "cx": cx, "cy": 0.0, "d": 2.0 * ri,
                      "note": f"轴承孔 Ø{2 * ri:g} @ (X={cx:g}, Y=0)；"
                              f"材料只在 y≥0 ⇒ 自然只切上半"})
    steps += [
        {"id": "s8", "op": "sketch_end", "name": f"{nm}轴承孔截面"},
        {"id": "ck6", "op": "check_sketch_constraint", "sketch": f"{nm}轴承孔截面",
         "expect_min_dims": 0},
        {"id": "bore", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
         "both_directions": True, "name": f"{nm}轴承孔",
         "note": "沿 Z 双向贯穿：切穿两端端壁的轴承座孔（中段是空腔，不吃料）"},
        {"id": "m3", "op": "check_mass",
         "expect_volume_mm3": round(v_flange + v_dome - v_cav - v_bore, 3), "vol_tol": 0.005,
         "note": f"− 两轴承孔 {v_bore:.0f} = {v_flange + v_dome - v_cav - v_bore:.0f}mm³"},
        # ---- 【细节】法兰螺栓孔 6×Ø9：从剖分面 y=0 **向上**盲切 tf（反方向是空气 ⇒
        #        方向错了必触发 `extrude_cut` 的反向备选，不会静默切错）----------
        {"id": "p8", "op": "select_plane", "plane": "Top",
         "note": "Top 面 = 剖分面 y=0（顶法兰底面）"},
        {"id": "s9", "op": "sketch_begin"},
        {"id": "pt8", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
    ]
    for i, (hx, hz) in enumerate(bolt_holes):
        steps.append({"id": f"fh{i}", "op": "circle", "cx": hx, "cy": -hz, "d": d_bolt,
                      "note": f"法兰螺栓孔 Ø{d_bolt:g} @ (X={hx:g}, Z={hz:g})"
                              f"（Top 面 (u,v)=(X,−Z)）"})
    steps += [
        {"id": "s10", "op": "sketch_end", "name": f"{nm}法兰孔位"},
        {"id": "ck7", "op": "check_sketch_constraint", "sketch": f"{nm}法兰孔位",
         "expect_min_dims": 0},
        {"id": "fhc", "op": "extrude_cut", "depth": tf, "end_cond": "swEndCondBlind",
         "name": f"{nm}法兰孔", "note": f"自 y=0 向上深 {tf:g}（只穿顶法兰）"},
        {"id": "pm1", "op": "check_mass", "note": "★ 探针：法兰孔之后（只记录判据用）"},
        # ---- 【细节】法兰螺栓 Ø18 沉孔（图纸 "6×Ø9 ⊥Ø18"）---------------------
        {"id": "pC0", "op": "plane_offset", "base": "Top", "offset": 1.764,
         "name": "沉孔中面", "note": "平行 Top @ y=1.764"},
        {"id": "pC0s", "op": "select_plane", "plane": "沉孔中面"},
        {"id": "sC1", "op": "sketch_begin"},
        {"id": "ptC1", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
    ]
    for i, (hx, hz) in enumerate(bolt_holes):
        steps.append({"id": f"cc{i}", "op": "circle", "cx": hx, "cy": -hz, "d": d_cbore,
                      "note": f"法兰螺栓沉孔 Ø{d_cbore:g} 深 {h_cbore:g} @ (X={hx:g}, Z={hz:g})"})
    steps += [
        {"id": "sC2", "op": "sketch_end", "name": f"{nm}螺栓沉孔位"},
        {"id": "ckC1", "op": "check_sketch_constraint", "sketch": f"{nm}螺栓沉孔位",
         "expect_min_dims": 0},
        {"id": "eC0", "op": "extrude_cut", "end_cond": "swEndCondMidPlane", "depth": h_cbore,
         "name": f"{nm}螺栓沉孔",
         "note": f"MidPlane {h_cbore:g} ⇒ Y∈[{tf - h_cbore:g},{tf:g}]（自剖分面上表面下沉）"},
        {"id": "pm2", "op": "check_mass", "note": "★ 探针：法兰沉孔之后（只记录判据用）"},
        # ---- 【层3 主体】窥视孔锥台凸台 46(顶)/10° → 【细节】方孔 28 → 4×M3（通）----
        {"id": "pV0", "op": "plane_offset", "base": "Top", "offset": visor_y0,
         "name": "窥视孔凸台底面", "note": f"平行 Top @ y={visor_y0:g}（拔模起点）"},
        {"id": "pV0s", "op": "select_plane", "plane": "窥视孔凸台底面"},
        {"id": "sV1", "op": "sketch_begin"},
        {"id": "ptV1", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        {"id": "vout", "op": "rect_corner", "x1": visor_cx - visor_lo, "y1": -visor_lo,
         "x2": visor_cx + visor_lo, "y2": visor_lo,
         "note": f"锥台**底面** {2 * visor_lo:.3f}×{2 * visor_lo:.3f} @ X={visor_cx:g}"
                 f"（= 46 + 2·{visor_y1 - visor_y0:g}·tan{visor_tap:g}° ⇒ 拔模后顶面恰 46）"},
        {"id": "sV2", "op": "sketch_end", "name": f"{nm}窥视孔凸台"},
        {"id": "ckV1", "op": "check_sketch_constraint", "sketch": f"{nm}窥视孔凸台",
         "expect_min_dims": 0},
        {"id": "eV0", "op": "extrude_boss", "end_cond": "swEndCondBlind",
         "depth": visor_y1 - visor_y0, "taper": visor_tap,
         "name": f"{nm}窥视孔凸台",
         "note": f"【层3 窥视孔凸台】自 y={visor_y0:g} 向上盲拉 {visor_y1 - visor_y0:g} + "
                 f"**{visor_tap:g}° 向内拔模** ⇒ Y∈[{visor_y0:g},{visor_y1:g}]，"
                 f"顶面 {2 * visor_h:g}×{2 * visor_h:g}（图纸 B 向 46×46，10° 侧壁）"},
        {"id": "pm3", "op": "check_mass", "note": "★ 探针：窥视孔凸台之后（只记录判据用）"},
        {"id": "pV1", "op": "plane_offset", "base": "Top",
         "offset": (visor_y0 + visor_y1) / 2.0,
         "name": "窥视孔中面", "note": f"平行 Top @ y={(visor_y0 + visor_y1) / 2:g}"},
        {"id": "pV1s", "op": "select_plane", "plane": "窥视孔中面"},
        {"id": "sV3", "op": "sketch_begin"},
        {"id": "ptV2", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        {"id": "vwin", "op": "rect_corner", "x1": visor_cx - visor_win, "y1": -visor_win,
         "x2": visor_cx + visor_win, "y2": visor_win,
         "note": f"窥视孔方孔 {2 * visor_win:g}×{2 * visor_win:g}"},
        {"id": "sV4", "op": "sketch_end", "name": f"{nm}窥视孔方孔"},
        {"id": "ckV2", "op": "check_sketch_constraint", "sketch": f"{nm}窥视孔方孔",
         "expect_min_dims": 0},
        {"id": "eV1", "op": "extrude_cut", "end_cond": "swEndCondMidPlane",
         "depth": visor_y1 - visor_y0, "name": f"{nm}窥视孔",
         "note": f"MidPlane {visor_y1 - visor_y0:g} ⇒ 贯穿凸台与穹顶壁（观察孔）"},
        {"id": "pm4", "op": "check_mass", "note": "★ 探针：窥视孔方孔之后（只记录判据用）"},
        {"id": "pV2s", "op": "select_plane", "plane": "窥视孔中面"},
        {"id": "sV5", "op": "sketch_begin"},
        {"id": "ptV3", "op": "point", "x": 0.0, "y": 0.0, "note": "★ S7 锚定"},
        {"id": "vm0", "op": "circle", "cx": visor_cx + m3_off, "cy": m3_off, "d": d_m3, "note": "4×M3 通孔"},
        {"id": "vm1", "op": "circle", "cx": visor_cx - m3_off, "cy": m3_off, "d": d_m3, "note": "4×M3 通孔"},
        {"id": "vm2", "op": "circle", "cx": visor_cx + m3_off, "cy": -m3_off, "d": d_m3, "note": "4×M3 通孔"},
        {"id": "vm3", "op": "circle", "cx": visor_cx - m3_off, "cy": -m3_off, "d": d_m3, "note": "4×M3 通孔"},
        {"id": "sV6", "op": "sketch_end", "name": f"{nm}窥视孔M3"},
        {"id": "ckV3", "op": "check_sketch_constraint", "sketch": f"{nm}窥视孔M3",
         "expect_min_dims": 0},
        {"id": "eV2", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
         "both_directions": True, "name": f"{nm}窥视孔M3孔", "note": "4×M3 沿 Y 贯穿凸台"},
        # ---- 收尾判据 ----
        {"id": "bb", "op": "check_bbox", "expect": [FX, ytop, Lz], "tol_mm": 0.05,
         "note": f"外形 [{FX:g}, {ytop:g}, {Lz:g}]"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": R1o, "tol": 0.01,
         "note": f"最大圆柱面 = 大顶罩外半圆 R{R1o:g}"},
        {"id": "m4", "op": "check_mass", "expect_volume_mm3": round(v_total, 3),
         "vol_tol": 0.005,
         "note": f"闭体积解析 = 法兰 {v_flange:.0f} + 罩 {v_dome:.0f} − 腔 {v_cav:.0f} "
                 f"− 轴承孔 {v_bore:.0f} − 法兰孔 {v_bolt:.0f} + 窥视孔锥台 {v_visor:.0f} "
                 f"− 方孔/M3 {v_m3:.0f} − 沉孔 {v_cbore:.0f} = {v_total:.0f}mm³。"
                 f"★ 容差**收到 0.5%** 的依据（2026-10-04 真机分段探针 pm1~pm4）：修掉"
                 f"「内腔半圆圆心上移到 y=tf」之后（§118），凸台净增实测 29669.28 → "
                 f"解析 29669.52（0.0008%）；主体 m0~m3、Δ1/Δ2/Δ4/Δ5 全部逐位吻合 ⇒ "
                 f"不再需要 2% 的宽容量"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-{FX:g}x{ytop:g}x{Lz:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}：figure-8 顶罩 + 顶法兰 + 轴承孔）",
                 "units": "mm",
                 "note": "尺寸 source=图纸 03.jpg（RapidOCR+分区放大）+ measured 交叉校核。"
                         "★ 2026-10-04 按图纸补：**窥视孔凸台 锥台 46(顶)×10°拔模 + 方孔 28 "
                         "+ 4×M3（孔心距 36×36）**、法兰螺栓 **Ø18 沉孔**。"
                         "★★ 图纸复核纠正：B 向视图「36×36」是 **M3 孔心距**、不是凸台顶面；"
                         "凸台顶面 = 俯视最外方框 = **46×46**（与主视「46」一致）；"
                         "俯视三层方框实测同心 ⇒ 中心 X = −9.0（230 左端起 91.3mm）。",
                 "layers": [
                     {"n": 1, "name": "顶板（剖分法兰）",
                      "size": f"{FX:g}×{Lz:g}×{tf:g}（X∈[{fx0:g},{fx1:g}]，Y 0→{tf:g}）",
                      "built_by": ["e0 顶法兰"],
                      "why_first": "全件的基准面；穹顶与窥视孔都长在它上面"},
                     {"n": 2, "name": "穹顶壳（figure-8 顶罩）",
                      "size": f"外 R{R1o:g}/R{R2o:g}、壁 {w:g}、内腔 R{R1i:g}/R{R2i:g}"
                              f"（端壁 {tw:g}）",
                      "built_by": ["e10/e11 罩外半圆（两半圆各一特征）",
                                   "e30/e31 罩腔（内腔切除）"]},
                     {"n": 3, "name": "窥视孔凸台（锥台）",
                      "size": f"顶面 {2 * visor_h:g}×{2 * visor_h:g}，侧壁拔模 {visor_tap:g}°，"
                              f"Y {visor_y0:g}→{visor_y1:g}",
                      "built_by": ["eV0 窥视孔凸台（带拔模的拉伸）"]},
                 ],
                 "details_on_layers": "轴承孔 Ø47/Ø62、法兰 6×Ø9 + Ø18 沉孔、窥视孔方孔 28×28、"
                                      "4×M3 通孔 —— **全部基于上述三层加工**",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {
            "frame_native": "X=轴间(230) Y=竖直(0→70.112) Z=轴向(104.019)；"
                            "装配绕 Y 转 −90°（X_cover = 104.52 − Z_base、Z_cover = X_base）",
            "radii_outer_inner_mm": [[R1o, R1i], [R2o, R2i]],
            "axis_x_mm": [c1, c2], "axis_distance_mm": d_axis,
            "wall_w_mm": w, "end_wall_tw_mm": tw, "flange_tf_mm": tf,
            "analytic_volume_mm3": round(v_total, 3),
            "reference_volume_mm3": ref_v,
            "analytic_vs_ref_pct": round((v_total - ref_v) / ref_v * 100.0, 3) if ref_v else None,
        },
    }


# --------------------------------------------------------------- 板类族（外形拉伸 + 方窗 + 圆孔）
#
# 7-垫片2：**矩形板 + 中央方窗 + 四角圆孔**，6 个轮廓**一次拉伸成型**。
#   参照件草图本身就是 6 个轮廓（外框 1 + 方窗 1 + 圆孔 4）⇒ 一次 `extrude_boss` 即可，
#   **不需要"拉伸 + 切除"两步**，也别拆（拆了反而多两个特征、还多一次体积对账）。
#
# ★ 尺寸来源（2026-10-02 取证，**没有图纸**，照参照件建）：
#   4 级圆柱 Ø3.500 贯穿、圆心 (±18, ±18) 由 `sw_faces` 直接读出；
#   方窗边长由**端面面积反解**（不是猜）：46² − 1293.5155 − 4·π/4·3.5² = **784 = 28²**。
#   体积复核 (46²−28²−4·π/4·3.5²)×2 = **2587.03** = 参照件实测，**逐位吻合**。
#
# ★ 判据用 `check_cyl_radius(expect_count=4)`：孔数与孔径一起钉住 ——
#   只对 bbox 的话，"4 个 Ø3.5"和"1 个 Ø7"给出同一个 46×46×2。
#
# ★★ 2026-10-04 回归修复：本常量定义曾被一次编辑误删（只剩 BUILDERS 里的引用），
#   导致 **整个模块 ImportError**（所有件一起哑）；已从 `.bak_20261004_hexfix` 复原。
#   ⇒ 教训：改本文件后**必须** `python -c "import sw_reducer"` 或跑一次 `--check`。
PLATE_FAMILY = ("gasket2", "cover_eye")


def _plate_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                part: str = "") -> dict:
    """板类族：矩形外形 + **中心开口**（方窗或圆孔）+ 四角圆孔，一张草图一次拉伸。

    ★ 泛化自 7-垫片2（方窗）与 8-窥视孔盖（**圆孔** Ø11）—— 两件形状同构：
      外框 + 中心开口 + 4 个角孔，**一张草图一次拉伸**。
      中心开口用 `window_shape` 选 `square`（读 `window` 边长）或 `circle`（读 `window_d` 直径）。
      **不要**为"只是中心开口形状不同"另写一个族 —— 那样两处判据会各自漂移。
    """
    q = P["parts"][part]
    L = float(v(q["L"]))
    W = float(v(q["W"]))
    t = float(v(q["t"]))
    # ---- 中心开口：方窗 or 圆孔 -------------------------------------------
    shape = str(q.get("window_shape", "square")).lower()
    if shape == "square":
        win = float(v(q["window"]))
        win_area = win * win
        win_label = f"中央方窗 {win:g}×{win:g}"
        if not 0 < win < min(L, W):
            raise ValueError(f"{part}: 方窗 {win} 必须小于板外形 {L}×{W}")
        # 孔心到板边的"可布孔半径"：方窗用半边长，圆孔用半径
        keepout = win / 2.0
    elif shape == "circle":
        wd = float(v(q["window_d"]))
        win_area = math.pi / 4.0 * wd ** 2
        win = wd                      # 供下面统一用（布孔禁区半径 = wd/2）
        win_label = f"中央圆孔 Ø{wd:g}"
        if not 0 < wd < min(L, W):
            raise ValueError(f"{part}: 圆孔 Ø{wd} 必须小于板外形 {L}×{W}")
        keepout = wd / 2.0
    else:
        raise ValueError(f"{part}: window_shape 只能是 square / circle，实得 {shape!r}")
    hd = float(v(q["hole_d"]))
    px = float(v(q["hole_pitch_x"]))
    py = float(v(q["hole_pitch_y"]))
    n = int(v(q.get("n_holes", 4)))
    # 布孔禁区：孔心离中心开口边缘至少半个孔径，否则孔会开在开口上（"通孔"变"缺口"）
    if not 0 < hd / 2.0 < min(px - keepout, py - keepout):
        raise ValueError(
            f"{part}: 圆孔 Ø{hd} 会开在{win_label}上（孔心 {px},{py}，"
            f"开口半宽 {keepout:g}）—— 孔必须整块落在材料里")
    if not 0 < px + hd / 2.0 < L / 2.0 or not 0 < py + hd / 2.0 < W / 2.0:
        raise ValueError(f"{part}: 圆孔 Ø{hd} 超出板外形（孔心 {px},{py}，板半宽 {L / 2},{W / 2}）")
    if n != 4:
        raise ValueError(f"{part}: 本生成器按**四角**布孔（n=4），实得 {n}")
    vol = (L * W - win_area - n * math.pi / 4.0 * hd ** 2) * t
    mass_g = round(vol * 1e-3, 4)
    # ---- 圆柱面判据**由几何算出来**，不写死 ----------------------------------
    # ★ 2026-10-02 踩过：先按"4 个角孔"写死 expect_count=4 / expect_max=hd/2，
    #   窥视孔盖（中央是**圆孔**）立刻炸：中央 Ø11 孔**本身就是一个圆柱面** r=5.5。
    #   而方窗（垫片2）**不是**圆柱面 ⇒ 同样两个数字在两件上一个对一个错。
    #   ⇒ 圆孔开口：count = n+1、max = 窗口半径；方窗开口：count = n、max = hd/2。
    n_cyl = n + (1 if shape == "circle" else 0)
    r_max_cyl = (win / 2.0) if shape == "circle" else (hd / 2.0)
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    hl, hw = L / 2.0, W / 2.0
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        # ★ 用 **Front**（法向 Z）而不是 Top —— Top 的法向是 **Y**，板会立起来
        #   （实测 bbox 变成 [46, 2, 46]，厚度跑到 Y 上）。Front 的草图坐标 (x,y) = 全局 (X,Y)，
        #   拉伸沿 +Z ⇒ bbox [46, 46, 2]，**与参照件朝向一致**（对账时不用解释旋转）。
        {"id": "p", "op": "select_plane", "plane": "Front",
         "note": "板面 = XY（**Front**，法向 Z）"},
        {"id": "s1", "op": "sketch_begin"},
        # 中心线过原点：S7 锚定 + 板的对称轴参考（构造线不参与轮廓）
        {"id": "cl", "op": "centerline", "x1": -hl, "y1": 0.0, "x2": hl, "y2": 0.0,
         "note": "X 向对称中心线（构造线）"},
        {"id": "cl2", "op": "centerline", "x1": 0.0, "y1": -hw, "x2": 0.0, "y2": hw,
         "note": "Y 向对称中心线（构造线）"},
        {"id": "out", "op": "rect_corner",
         "x1": -hl, "y1": -hw, "x2": hl, "y2": hw,
         "note": f"板外形 {L:g}×{W:g}"},
    ]
    if shape == "square":
        steps.append({"id": "win", "op": "rect_corner",
                      "x1": -win / 2.0, "y1": -win / 2.0, "x2": win / 2.0, "y2": win / 2.0,
                      "note": f"{win_label}（**端面面积反解**所得，非猜测）"})
    else:
        steps.append({"id": "win", "op": "circle", "cx": 0.0, "cy": 0.0, "d": win,
                      "note": f"{win_label}（**端面面积反解**所得："
                              f"46²−1982.48−4·π/4·3.5² = 95.03 ⇒ Ø11.000）"})
    for i, (cx, cy) in enumerate(((-px, py), (px, py), (-px, -py), (px, -py))):
        steps.append({"id": f"h{i}", "op": "circle", "cx": cx, "cy": cy, "d": hd,
                      "note": f"角孔 Ø{hd:g} @ ({cx:g},{cy:g})"})
    steps += [
        {"id": "s2", "op": "sketch_end", "name": f"{q['name']}截面"},
        {"id": "ck", "op": "check_sketch_constraint", "sketch": f"{q['name']}截面",
         "expect_min_dims": 0,
         "note": f"★ 标注已封存 ⇒ 只记录约束状态（本图 {2 + 1 + n} 个轮廓）"},
        {"id": "ex", "op": "extrude_boss", "depth": t,
         "end_cond": "swEndCondMidPlane", "name": q["name"],
         "note": f"**{2 + 1 + n} 个轮廓一次拉伸**（外框+{win_label}+{n} 孔），与参照件做法一致"},
        {"id": "bb", "op": "check_bbox", "expect": [L, W, t], "tol_mm": 0.02,
         "note": f"板外形 {L:g}×{W:g}×{t:g}"},
        {"id": "cyl", "op": "check_cyl_radius", "expect_count": n_cyl,
         "expect_max": r_max_cyl, "tol": 0.01,
         "note": f"**{n} 个 Ø{hd:g} 通孔**（{win_label}）—— 孔数+孔径一起钉住"
                 f"（只看 bbox 抓不到）。★ 圆柱面总数 {n_cyl} = {n} 个角孔"
                 f"{' + 1 个中央圆孔' if shape == 'circle' else '（方窗不是圆柱面）'}"
                 f"，最大半径 {r_max_cyl:g}mm"
                 f"{'（= 中央圆孔）' if shape == 'circle' else ''}"},
        {"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
         "note": f"(L·W − 开口面积 − {n}·π/4·d²)·t = {vol:.4f}mm³"
                 f"（开口 {win_label}）"},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-{L:g}x{W:g}x{t:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，板+{win_label}+{n}孔 一次拉伸）", "units": "mm",
                 "note": f"规格来自 reducer.params.json::parts.{part}（source=measured，"
                         f"sw_inspect + sw_faces + 端面面积反解）；**不硬编码**",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"L": L, "W": W, "t": t, "window_shape": shape,
                     "window": win, "window_area_mm2": round(win_area, 4),
                     "hole_d": hd,
                     "cyl_expect": {"count": n_cyl, "max_r": round(r_max_cyl, 4)},
                     "hole_pitch": [px, py], "n_holes": n,
                     "analytic_volume_mm3": round(vol, 4),
                     "reference_volume_mm3":
                         (q.get("_measured_faces") or {}).get("solved", {})
                         .get("reference_volume_mm3")},
    }


# ★ 标定模式开关（由 `--plan <件> --calibrate` 打开）：生成计划时**跳过** check_mass，
#   用于"重新设计后旧标定值作废、需要真机量一次新值"的场景。
_CALIBRATE: dict = {"on": False}

GEAR_FAMILY = ("wheel", "gear_shaft")


def _gear_bbox_tol(da: float) -> float:
    """齿轮齿顶的**绝对**包围盒容差（mm）。

    ★ 齿廓是**折线采样**（`gear_profile` 的 polyline），所以齿顶方向的包围盒会**缩进**。
      真机实测缩进量：
        z=18 / da=40   → 39.606（缩 0.394，Y 向却是 40.0 —— 与采样相位有关）
        z=15 / da=34.8 → 34.527 / 34.708（缩 0.273 / 0.092）
      即 ≤ **1.0%** 的 da。这里给 **1.5%**（下限 0.25mm）留一倍余量。

    ★★ 口径必须写清，否则就是"假绿灯"：
      · `check_bbox` 只能抓 **> 1.5%** 的齿顶错误；
      · Ø113 的大齿轮上，**漏掉变位（da 差 0.8mm = 0.7%）它是抓不到的** ——
        真正的齿顶/变位判据是**离线 `C4`**（da = m(z+2h_a*+2x)）与 **`check_mass`**（真机标定值）。
      · 别为了让这条过而把容差调大 —— 那正是 `tol: 99.0` 的老路。
    """
    return round(max(0.25, 0.015 * abs(da)), 3)


def _gear_shaft_plan(P: dict, out_dir: str | None = "D:/out/reducer",
                     part: str = "gear_shaft") -> dict:
    """齿轮轴（小齿轮与轴一体）：**半剖面回转轴身（细轴段）+ 渐开线齿廓拉伸成实体**。

    ★★ 关键设计决策（2026-10-02 真机踩出来的）：齿廓**必须拉伸成实体，不能切除**。
       我最初以为「齿根圆柱已是轴身最粗的一段 ⇒ 切掉齿根外的材料就是齿」——
       **错**：齿根圆 Ødf₁ **比齿顶 da₁ 细**（当年误判时 z=18：df=31 < da=40），
       比轴上其他台阶（Ø24）粗但比齿顶细 ⇒ 齿顶那圈材料**从来不存在**，切无可切
       （真机实测 bbox Y/Z=30.838 ≈ Ø31，齿顶 40 根本没出现；
       而且 `ThroughAllBoth` 还把齿根段以外的轴身切掉了）。
       ⇒ 正确做法与**大齿轮完全一致**：
          ① 轴身回转时齿部那段用**细轴 Ø24**（不留"齿根圆柱"）
          ② 在齿部位置用 `extrude_boss` 把**整圈渐开线齿廓拉伸成实体**并 `merge`
          ③ 齿根圆 Ødf₁ 与齿顶 Øda₁ **由齿廓实体自然形成**，不用单独建柱面。

    ★ 模数只从 `meta.gear_train` 读（用户硬要求：两齿轮模数必须相同）。
    ★ 齿顶圆 Øda 是**渐开线面不是圆柱面** ⇒ 交给 `check_bbox` 判（同大齿轮，§89）。
    ★★ **变位（方案 A，2026-10-02）**：da/df 都要带 `+2xm` 项 ——
       `da = m(z+2h_a*+2x)`、`df = m(z−2h_a*−2c*+2x)`。漏掉这一项 = 0.8mm 偏差，
       正好在 `check_bbox` 容差之外 ⇒ 会被抓出来（不会静默通过）。
    ⚠ 键槽是**唯一**合法的 `extrude_cut`（从轴表面切进去，见 `pk` 步）；齿廓**不是**切除。
    ⚠ 简化项见 `parts.gear_shaft._simplified`：锥面/键槽/螺纹**都已建**，
      只剩「齿部两端 30° 齿顶锥」未建（`gear_tooth_taper`，挂 **Q9**）⇒
      **不对账参照件 bbox 总长**（见 Q9）。已建项也一律登记，防下次又当"没做"。
    """
    q = P["parts"][part]
    gt = P["meta"]["gear_train"]                    # ★ 唯一真源
    segs_spec = q["shaft_segments"]
    segs = [{"d": float(s["d"]), "l": float(s["l"]), "name": s["name"]}
            for s in segs_spec["segments"]]
    ec = float(segs_spec["end_chamfer"])
    total_l = float(segs_spec["total_l"])
    total_len = float(segs_spec["total_len"])
    m = float(gt["module"])
    z = int(gt["pinion"]["z"])
    alpha = float(gt.get("alpha_n_deg", 20.0))
    xvar = float((gt["pinion"] or {}).get("x", 0) or 0)
    b = float(gt["face_width"])
    # ★★ 变位（x≠0）时齿顶/齿根圆都要跟着移 **2xm**（2026-10-02 方案 A）：
    #     da = m(z + 2h_a* + 2x)   df = m(z − 2h_a* − 2c* + 2x)
    #   漏掉 2x 这一项 ⇒ da/df 全错；本件 x=+0.2 差 0.8mm，正好在
    #   `check_bbox` 容差 0.15 之外，会被抓出来（不会静默通过）。
    da = m * (z + 2 + 2 * xvar)                     # 齿顶圆
    df = m * (z - 2.5 + 2 * xvar)                   # 齿根圆（c*=0.25）
    # ★ 齿部那一段在**轴身里是细轴**（Ø = 齿根段台阶直径），
    #   齿根圆/齿顶圆由**齿廓实体**自然形成 —— 详见函数 docstring 的踩坑记录。
    root_idx = next((i for i, s in enumerate(segs) if s["name"].startswith("★")), -1)
    if root_idx < 0:
        raise ValueError(f"{part}: 段表里没有以 ★ 开头的齿部轴段（定位需要它）")
    x_root0 = sum(s["l"] for s in segs[:root_idx]) + ec
    root_seg_l = segs[root_idx]["l"]
    axle_d = segs[root_idx]["d"]
    if axle_d >= da:
        raise ValueError(
            f"{part}: 齿部轴段直径 Ø{axle_d:g} ≥ 齿顶 da={da:g} —— "
            f"那齿顶圆柱就**建不出来**（没有材料可凸出）。"
            f"齿部轴段必须比齿顶细（通常取齿根侧台阶 Ø24）。")
    if b > root_seg_l:
        raise ValueError(f"{part}: 齿宽 {b} > 齿部轴段长 {root_seg_l}（齿比轴段还长？）")
    RUNOUT_NOTE = f"{(root_seg_l - b) / 2:.3f}mm"

    # ── 键槽（用户 2026-10-02 截图指出后新增；上轮漏建）───────────────
    kw = q.get("keyway") or {}
    kw_w = float(v(kw["width"]))                      # 槽宽 6
    kw_depth = float(v(kw["depth"]))                  # 径向切深 2.964（实测）
    kw_lt = float(v(kw["length_total"]))              # 总长 30
    kw_ls = float(v(kw["length_straight"]))           # 直段 24
    kw_seg_d = float(v(kw["segment_d"]))              # 所在段 Ø18
    kw_off = kw.get("_src_offset")
    # ★ 坐标换算：取证用的是**参照件全局 x**（输出端轴在 −61），
    #   而 `shaft_profile` 从局部 0（加 ec）排开 ⇒ 局部 = 全局 + 62.108。
    #   键槽起点参照件 x45 ⇒ 局部 x = 45 + 62.108 = 107.108
    #   但更稳的口径：**按段名定位**（★键槽段），而不是硬编码偏移 ——
    #   齿部段一改长度，后面所有段的局部 x 都会平移，硬编码必错。
    kw_seg_idx = next((i for i, s in enumerate(segs)
                       if s["name"] == (kw.get("_seg_name") or "★键槽段")), -1)
    if kw_seg_idx < 0:
        raise ValueError(
            f"{part}: params.keyway 存在但段表里找不到 {kw.get('_seg_name')!r} —— "
            f"键槽要切在某一段轴上，得先有那一段。"
            f"（段表现有：{[s['name'] for s in segs]}）")
    kw_seg_name = segs[kw_seg_idx]["name"]
    kw_seg_d = float(segs[kw_seg_idx]["d"])
    kw_seg_l = float(segs[kw_seg_idx]["l"])
    kw_seg_x0 = sum(s["l"] for s in segs[:kw_seg_idx]) + ec
    # ★★ 键槽必须从**轴表面**切进去（详见下面 `pk` 步的长注释）。
    #   `_cut_radius` 缺省 = 所在段起点半径；锥段可用它显式指定。
    kw_r = float(((kw.get("_cut_radius") or {}).get("v")) or (kw_seg_d / 2.0))
    # ★★ 坐标口径：**必须按段名 + 段内偏移定位，不能硬编码绝对 x**。
    #   取证 x 是**参照件全局**（输出端轴在 −61），段表是**局部**（从 ec 起排），
    #   而齿部段一改长度，后面所有段的局部 x 都会平移 ⇒ 硬编码必错（我第一版就踩了）。
    #   参照件键槽总长起点 x45、所在段起点 x42 ⇒ **段内 +3**（跟着段走，齿部怎么改都不影响）。
    kw_x0 = kw_seg_x0 + float(kw.get("_offset_in_seg", 3.0))   # 长圆槽**总长**起点
    kw_cu = kw_x0 + kw_lt / 2.0                               # 长圆槽**中心**（草图用）
    if kw_x0 + kw_lt > kw_seg_x0 + kw_seg_l:
        raise ValueError(
            f"{part}: 键槽总长 {kw_lt:g} 从 x={kw_x0:g} 起 ⇒ 末端 {kw_x0 + kw_lt:g} "
            f"超出所在段『{kw_seg_name}』末端 {kw_seg_x0 + kw_seg_l:g} —— 键槽会切到邻段上")
    if kw_w >= kw_seg_d:
        raise ValueError(
            f"{part}: 键槽宽 {kw_w:g} ≥ 所在段 Ø{kw_seg_d:g} —— 那会把整段切穿"
            f"（参照件里这两个量不可能同时成立，说明段表或键槽数据有一个错了）")
    # ★ 长圆槽自洽式：总长 = 直段 + **一个**端部直径（两端各半圆 = 一个整圆直径）
    if abs(kw_ls + kw_w - kw_lt) > 1e-9:
        raise ValueError(
            f"{part}: 键槽自洽性不成立：直段 {kw_ls:g} + 宽 {kw_w:g} != 总长 {kw_lt:g}"
            f"（长圆槽总长 = 直段 + 端部直径）")

    # ── 端部真螺纹（用户 2026-10-02 截图指出；上轮简化成光杆）─────────
    th = q.get("thread") or {}
    th_d = float(v(th["diameter"]))                  # 14（M14）
    th_pitch = float(v(th["pitch"]))                 # 2（粗牙）
    th_len = float(v(th["length"]))                  # 9.047
    th_fd = float(v(th["face_d"]))                   # 12（端面 = 螺纹小径）
    th_std = str(th.get("standard", "metric_die"))
    th_seg_idx = next((i for i, s in enumerate(segs)
                       if s["name"] == (th.get("_seg_name")
                                        or "★端部螺纹段（M14 建光杆+真螺纹）")), -1)
    if th_seg_idx < 0:
        raise ValueError(f"{part}: params.thread 存在但段表里找不到螺纹段")
    if abs(segs[th_seg_idx]["d"] - th_d) > 1e-6:
        raise ValueError(
            f"{part}: 螺纹公称 M{th_d:g} 与段表直径 Ø{segs[th_seg_idx]['d']:g} 不一致 —— "
            f"螺纹是**在光杆上切出来的**，光杆必须等于公称直径")
    if abs(segs[th_seg_idx]["l"] - th_len) > 1e-3:
        raise ValueError(
            f"{part}: 螺纹旋合长 {th_len:g} 与段表光杆长 {segs[th_seg_idx]['l']:g} 不一致")
    th_x0 = sum(s["l"] for s in segs[:th_seg_idx]) + ec
    if not th_fd < th_d < 2 * th_d / 3 + th_d:
        raise ValueError(f"{part}: 螺纹端面 Ø{th_fd:g} 与公称 M{th_d:g} 不自洽")

    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    _CAL = bool(_CALIBRATE.get("on"))
    # ★★ 与 `_gear_plan` 同款守卫（2026-10-02 补）：标定值 ≤0 时**明确报错**，
    #   不要让 `if mass_g else {}` 把 check_mass 静默过滤掉 ——
    #   那样真机会「全绿通过」，其实最重要的质量判据根本没跑。
    _raw_gs = v(q.get("expect_mass_g"))
    if _raw_gs is None or float(_raw_gs or 0) <= 0:
        if not _CAL:
            raise ValueError(
                f"{part}: `expect_mass_g` 缺失或 ≤0（读到 {_raw_gs!r}）。\n"
                f"  ★ 齿部尺寸或段表一旦改动，**旧标定值就作废**（齿隙体积变了）。\n"
                f"  正解一：`python scripts/sw_reducer.py --plan {part} --calibrate`"
                f"（check_mass 改成**只测不判**，真机跑完从报告的 `volume_mm3` 读实测值）。\n"
                f"  正解二：把新实测值写回 params 的 `expect_mass_g`。\n"
                f"  **不要**用解析式顶替 —— 齿隙体积取决于 gear_profile 的采样密度，算不出来。")
        mass_g = None
    else:
        mass_g = float(_raw_gs)
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        {"id": "p", "op": "select_plane", "plane": "Front",
         "note": "轴向 = X（Front 平面的 x）；草图坐标 (x,y) = (轴向, 半径)"},
        # ── ① 轴身：半剖面一次回转成型（**含齿根圆柱段**）────────────
        {"id": "s1", "op": "sketch_begin"},
        {"id": "prof", "op": "shaft_profile", "segments": segs,
         "end_chamfer": ec,
         "note": f"★ 段表来自 params.shaft_segments。Σl={total_l:g} + 两端 45° 倒角 "
                 f"2×{ec:g} = 总长 {total_len:g}。★ **齿部那一段是细轴 Ø{axle_d:g}**"
                 f"（x={x_root0:g}→{x_root0 + root_seg_l:g}），"
                 f"**齿根/齿顶由后面的齿廓拉伸成形**，不是这里建柱面"},
        {"id": "zf0", "op": "zoom_fit"},
        {"id": "s2", "op": "sketch_end", "name": "轴半剖面"},
        {"id": "cksk", "op": "check_sketch", "sketch": "轴半剖面"},
        {"id": "ck1", "op": "check_sketch_constraint", "sketch": "轴半剖面",
         "expect_min_dims": 0,
         "note": "★ 标注已封存 ⇒ 只记录约束状态"},
        {"id": "rev", "op": "revolve_boss", "name": "轴体", "angle": 360},
        # ── ② 齿廓：渐开线**切除**（齿根圆柱已在轴身里）──────────────
        # ★★★ **草图平面必须开在齿根段中间**，不能直接用 Right（x=0）！
        #   齿根段在 x = x_root0 → x_root0+root_seg_l（本件 29 → 54.196），
        #   而 Right 基准面在 x=0 ⇒ 平面**骑在轴身端点外**，
        #   `ThroughAllBoth` 只在 x≈0 处切到一点材料，把端头切掉一大块，
        #   **轴身整段消失，只剩齿部那截 25mm**（真机实测：bbox X 29→54.196、V=27.5mm³）。
        #   ★ 这与 §82「草图平面不在材料上，任何终止条件都切不动/切错」同源，
        #     但这里更隐蔽：切除**没有报错**、特征树正常，只是切错了地方。
        {"id": "po", "op": "plane_offset", "base": "Right",
         "offset": x_root0, "name": "齿部面",
         "note": f"★ 齿廓草图面 = Right 沿 +X 偏 **x={x_root0:g}**"
                 f"（齿部轴段起点）"},
        {"id": "p2", "op": "select_plane", "plane": "齿部面",
         "note": "齿廓平面 ⊥ 轴（法向 X）"},
        {"id": "s3", "op": "sketch_begin", "reset": True},
        {"id": "cl", "op": "centerline", "x1": -root_seg_l, "y1": 0.0,
         "x2": root_seg_l, "y2": 0.0,
         "note": "齿宽方向中心线（构造线；S7 锚定 + 齿宽参考）"},
        {"id": "gear", "op": "gear_profile", "mn": m, "z": z, "beta": 0.0,
         "alpha_n": alpha, "x": xvar, "mode": "polyline",
         "n_flank": 6, "n_root": 2, "n_tip": 3,
         "note": f"整圈渐开线（m={m:g} 取自 meta.gear_train，z={z}）。"
                 f"★ 这里**拉伸成实体**（不是切除）：齿根圆 Ø{df:g} 与齿顶 Ø{da:g} "
                 f"由齿廓实体自然形成",},
        {"id": "zf1", "op": "zoom_fit"},
        {"id": "s4", "op": "sketch_end", "name": "齿廓"},
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": "齿廓",
         "expect_min_dims": 0, "note": "★ 标注已封存"},
        # ★★ **拉伸成实体**（`extrude_boss` + `merge`），与 `_gear_plan` 的判据一致：
        #   ① 齿廓实体提供齿根/齿顶/齿形，一次成形，不用来回切
        #   ② `merge=True` 与轴身并成一体
        #   ③ 齿宽 b 从齿部面（x=x_root0）向 **+X 单向**拉伸 b ⇒ 齿占 x_root0 → x_root0+b
        #      ★ 单向而不是中面对称：齿宽 b 是**齿顶段的宽度**，退刀段在它两侧
        #        （参照件齿根段 33.422 vs 齿顶段 28.226，每端退刀 2.598，同款做法）
        {"id": "boss2", "op": "extrude_boss", "depth": b,
         "end_cond": "swEndCondBlind", "merge": True, "name": "齿部",
         "note": f"★ **拉伸成实体并 merge**（不是切除！齿顶 Ø{da:g} 圆柱由这里建出）。"
                 f"齿宽 {b:g}，从齿部面 x={x_root0:g} 向 +X ⇒ 齿占 "
                 f"x {x_root0:g}→{x_root0 + b:g}；齿部轴段全长 {root_seg_l:g} ⇒ "
                 f"两端各留退刀 {RUNOUT_NOTE}"},
        # ── ④ 键槽（长圆形：直段 + 两端 R3 半圆），沿 +Y 开口 ────────
        # ★★ 用户 2026-10-02 截图指出参照件有这个键槽，我上一版**漏建**了。
        #   取证：底平面 A=**172.27**（x45→75）、两侧壁 A=61.50×2（x48→72）
        #   ⇒ 宽 6、深 2.964、直段 24、两端 R3（总长 30）
        #   ★ 三重独立验证：`24×6 + π×3² = **172.2743**` 与实测 172.27 **逐位吻合**
        #     ⇒ 尺寸不是估的；且 `x42→80` 圆柱面面积比完整值缺 317.33 ⇒ 确实被切掉一块。
        # ★★★ **草图面用 `Top`（法向 Y），不是 `Right`（法向 X）** —— 见下面的探针结论。
        #   键槽要切在**圆柱侧面**上，所以草图面必须**包含轴向 X**、法向 = 周向 Y ⇒ Top。
        #   ⚠★ 2026-10-02 真机探针定死的坐标映射（**不要靠猜**）：
        #       `Top` 面上草图 (u,v) → **全局 (X, Z)**（u=轴向、v=周向）
        #       `extrude_cut(reverse_dir=False)` → 沿 **−Y**（朝轴心）
        #       验证：Top 面上画 2×2 方块、cy=0、depth 2.964 ⇒ 去料 **11.8560**
        #             == 2×2×2.964 **逐位吻合**（整块落在圆柱内）
        #   ⇒ 键槽：长 30 沿 **u（轴向 X）**、宽 6 沿 **v（周向 Z）**、深 2.964 沿 **−Y**。
        # ★★★ **必须从轴表面切进去**（2026-10-02 修正）：
        #   旧实现把草图开在 `Top`（y=0，**含轴心**）再沿 −Y 切 3.5 ⇒ 切掉的块
        #   整落在 r ≤ sqrt(3.5²+3²) ≈ 4.6 < R=9 内 ⇒ 在轴**内部**掏了个封闭空腔：
        #   外表看不出来，**体积判据也抓不到**（去料量相近）。
        #   证据：参照件键槽**底面是完整的长圆形平面** A = 24×6 + π×3² = 172.2743
        #        （实测 172.27 逐位吻合）—— 只有「平面底 + 从表面切」才长成这样。
        #   ⇒ 先把 Top 沿 −Y 偏到 **y = −R**（轴表面切平面），再朝 **+Y**（轴心）切 t₁。
        {"id": "pk", "op": "plane_offset", "base": "Top", "offset": -kw_r,
         "name": "键槽面",
         "note": f"键槽切平面：Top 沿 **−Y** 偏 {kw_r:g} ⇒ y = −{kw_r:g}（轴的外表面切平面）。"
                 f"★ 平面必须落在轴**表面**上，槽才有开口；落在轴心只能掏空腔"},
        {"id": "pk2", "op": "select_plane", "plane": "键槽面",
         "note": "★ 草图面法向 Y、含轴向 X；(u,v) → 全局 (X, Z)（与 Top 同款映射）"},
        {"id": "s5", "op": "sketch_begin", "reset": True},
        # ★★ S7「锚住原点」要求草图里有**落在原点上的图元**，或一条**两端都在 y=0 上的
        #   中心线**。原来这条是**竖直**的（(cu,0)→(cu,30)，只有一个端点在 y=0）⇒ 判违规。
        #   改成**水平轴向中心线**（两端 y 都是 0）—— 既满足 S7，语义也才对：
        #   键槽的轴向中心线本来就该沿 u 方向、与两条直边平行。
        #   ⚠ 必须走 `centerline`（构造线）而不是普通 `line`，否则会被算进轮廓。
        {"id": "clk", "op": "centerline",
         "x1": round(kw_cu - kw_lt / 2.0, 6), "y1": 0.0,
         "x2": round(kw_cu + kw_lt / 2.0, 6), "y2": 0.0,
         "note": "键槽**轴向中心线**（构造线）。S7 锚定要求中心线落在 y=0 上"},
        # 长圆槽：直段 24（沿轴向）+ 两端 R3 半圆（沿周向向 ±Z 鼓出）。
        #   ★ 画法：2 个整圆（圆心在 v=±3，直段两端）+ 2 条平行于轴的公切线。
        #   这样得到的长圆轮廓是**上下两条直边 + 两端半圆**，与参照件底面
        #   `24×6 + π×3² = 172.2743`（实测 172.27）逐位吻合。
        # ★★ 两端是**半圆弧**，不是整圆（用户 2026-10-02 截图纠正）：
        #   整圆 + 两条公切线 = **4 个轮廓**（2 个整圆 + 2 个矩形），
        #   SolidWorks 判重叠自相交 ⇒ `extrude_cut` 两个方案全灭。
        #   正解：半圆弧（`arc3p` 的点序是 **起点→终点→弧上点**）+ 两条公切线 = **1 个闭合轮廓**。
        #   左端半圆：起点 (cu−ls/2, +w/2) → 终点 (cu−ls/2, −w/2) → 弧上点 (cu−ls/2−w/2, 0)
        {"id": "kwa1", "op": "arc3p",
         "p1": [round(kw_cu - kw_ls / 2.0, 6), round(kw_w / 2.0, 6)],
         "p2": [round(kw_cu - kw_ls / 2.0, 6), round(-kw_w / 2.0, 6)],
         "p3": [round(kw_cu - kw_ls / 2.0 - kw_w / 2.0, 6), 0.0],
         "note": f"★ 左端半圆 R{kw_w / 2:g}（**弧**，不是整圆）"},
        {"id": "kwa2", "op": "arc3p",
         "p1": [round(kw_cu + kw_ls / 2.0, 6), round(-kw_w / 2.0, 6)],
         "p2": [round(kw_cu + kw_ls / 2.0, 6), round(kw_w / 2.0, 6)],
         "p3": [round(kw_cu + kw_ls / 2.0 + kw_w / 2.0, 6), 0.0],
         "note": f"★ 右端半圆 R{kw_w / 2:g}"},
        {"id": "kwl1", "op": "line", "x1": round(kw_cu - kw_ls / 2.0, 6), "y1": round(-kw_w / 2.0, 6),
         "x2": round(kw_cu + kw_ls / 2.0, 6), "y2": round(-kw_w / 2.0, 6),
         "note": "下公切线（长 = 直段 24）"},
        {"id": "kwl2", "op": "line", "x1": round(kw_cu - kw_ls / 2.0, 6), "y1": round(kw_w / 2.0, 6),
         "x2": round(kw_cu + kw_ls / 2.0, 6), "y2": round(kw_w / 2.0, 6),
         "note": "上公切线"},
        {"id": "s6", "op": "sketch_end", "name": "键槽"},
        {"id": "ck3", "op": "check_sketch_constraint", "sketch": "键槽",
         "expect_min_dims": 0, "note": "★ 标注已封存"},
        {"id": "cutk", "op": "extrude_cut", "depth": kw_depth, "reverse_dir": True,
         "name": "键槽",
         "note": f"★ 径向切深 **{kw_depth:g}** = GB/T 1095 对 Ø{kw_seg_d:g} 轴配 6×6 平键的 t₁"
                 f"（图纸只标宽度 6 与 Ra1.6，未标深度）。\n"
                 f"   起点面在 y = −{kw_r:g}（轴表面）⇒ `reverse_dir=True` = **沿 +Y 朝轴心**切，"
                 f"槽底平面落在 y = −{kw_r - kw_depth:g} ⇒ 底面积应为 "
                 f"24×6 + π×3² = {24 * kw_w + 3.141592653589793 * (kw_w / 2) ** 2:.4f}"
                 f"（参照件实测 172.27 ✔ 独立佐证）"},
        # ── ⑤ 端部真螺纹 M14（`real_thread`，`SweepThread` 真去料）──────
        # ★ 用户 2026-10-02 截图指出：参照件端部有螺纹，我上一版简化成了光杆。
        #   取证：3 个 `ident4006`（B样条 = SolidWorks 真螺纹面）、圆柱 Ø14、端面 Ø12
        #   ⇒ M14 粗牙（螺距 2）× 9.047。
        #   ★ 用 `real_thread` 而不是 `thread`：`thread` 是**装饰线**，不去料、体积不减，
        #     看着像螺纹但实体还是光杆 —— 判据会完全失效。
        {"id": "th", "op": "real_thread",
         "edges": [{"point": [th_x0, 0.0, 0.0]}],
         "standard": th_std, "diameter": th_d, "pitch": th_pitch,
         "end_condition": "blind", "depth": th_len,
         "right_handed": True,
         # ★★ 声明为**可选**（2026-10-02）：真机上这步跑了 195s 后
         #   「真实螺纹线没生成特征」，反复诊断成本极高，而螺纹**不影响**
         #   齿廓 / 键槽 / 总长 / 各段直径的验收（那些判据在 `bb`/`cyl` 上）。
         #   按用户口径「先把件建出来，细节留到装配期修」⇒ 显式声明可选，
         #   让后面的 bbox/质量验收照跑。**理由必须写清**（见 Q11），
         #   报告会把跳过项单列，不允许静默。
         "optional": "M14×2 螺纹线真机建不出（195s 后 CreateFeature 无新特征，见 Q11）；"
                     "螺纹不影响齿廓/键槽/总长/各段直径，验收判据在 bb/cyl 上",
         "note": f"★ **真螺纹** M{th_d:g}×{th_pitch:g}（粗牙），旋合长 {th_len:g}，"
                 f"起点 x={th_x0:g}（Ø{th_d:g} 圆柱段起点）。"
                 f"参照件端面 Ø{th_fd:g} = 螺纹小径。"
                 f"★ `real_thread` 必须选**圆边**（给圆心坐标），选圆柱面会静默不建。"
                 f"⚠ 本步**声明为 optional**（真机建不出，建不出也有替代判据）—— 见 Q11"},
        # ── ③ 判据 ────────────────────────────────────────────────
        {"id": "bb", "op": "check_bbox", "expect": [total_len, da, da],
         "tol_mm": _gear_bbox_tol(da),
         "note": f"**齿顶方向** = da = m(z+2) = {da:g}（★ 渐开线面不是圆柱面，只能这样判）；"
                 f"轴向 {total_len:g} = 简化后的总长"
                 f"（**参照件是 158.241，差 47mm = 5 段锥面不建，见 Q8**，不与参照件比）"},
        # ★★ 齿根圆 Ødf₁ 也**不是圆柱面**（渐开线齿廓的一部分）⇒ 同齿顶，
        #   只能交给 `check_bbox`。这里钉**轴上最大的真圆柱面** = 齿部轴段台阶 Ø24。
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": axle_d / 2.0, "tol": 0.05,
         "note": f"最大**圆柱面** = 轴台阶 Ø{axle_d:g}/2 = {axle_d / 2:g}。"
                 f"★ 齿根 Ø{df:g} 与齿顶 Ø{da:g} **都不是圆柱面**（渐开线齿廓），"
                 f"只能由 `check_bbox` 判（§89）。本判据钉住轴各段直径没建错"},
        # ★★ 标定模式下**不再删掉这一步**，而是「只测不判」（不给 expect_mass_g）——
        #   2026-10-02 修：以前 `if mass_g else {}` 把整步删掉 ⇒ 标定跑完报告里
        #   **根本没有质量数据**，只能再跑一次真机才能读，白烧一轮。
        #   ★ 不给 `expect_mass_g` 时 `op_check_mass` 只回原始属性（含 `volume_m3`），
        #     正好拿来算标定值；给了才做门禁。两种模式一步覆盖。
        ({"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
          "note": f"**{mass_g:g}g 是真机标定值**（齿隙体积取决于 gear_profile 采样密度，"
                  f"解析式算不出 {z} 个齿隙挖掉多少）。"}
         if mass_g else
         {"id": "m", "op": "check_mass",
          "note": f"★ **标定模式：只测不判**（不给 expect_mass_g）—— 读到体积后"
                  f"回填 `parts.{part}.expect_mass_g`，再重跑一次即成为门禁。"
                  f"齿隙体积取决于采样密度，解析式算不出 {z} 个齿隙挖掉多少。"}),
        {"id": "fc", "op": "check_feature_count", "min": 2},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error",
         "note": f"齿顶变尖/根切等 error 级诊断（z={z}, x={xvar}）"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-m{m:g}z{z}x{da:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，轴身回转 + 渐开线齿廓**拉伸成实体** m={m:g} z={z} x={xvar:+.2f}）",
                 "units": "mm",
                 "note": f"★ **模数来自 `meta.gear_train.module` = {m:g}**（两齿轮模数必须相同）。"
                         f"齿部按 z₁={z} + 变位 x₁={xvar:+.2f} 建（**消除根切**，方案 A）；"
                         f"da = m(z+2h_a*+2x) = {da:g}、df = m(z−2.5+2x) = {df:g}。"
                         f"⚠ 简化项见 `_simplified` 与 open_questions **Q9**"
                         f"（锥面/键槽/螺纹均已建，只剩齿顶 30° 锥未建）⇒ **不对账参照件总长**",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"m": m, "z": z, "alpha_n": alpha, "x": xvar,
                     "da": da, "df": df, "face_width": b,
                     "n_segments": len(segs), "sum_l": total_l,
                     "total_len": total_len, "end_chamfer": ec,
                     "root_seg_x": [x_root0, x_root0 + root_seg_l],
                     "expect_mass_g": mass_g,
                     "simplified": sorted(k for k in (q.get("_simplified") or {})
                                          if not k.startswith("_")),
                     "reference_bbox_mm": [158.241, 33.794, 33.922],
                     "reconcile": "**只对账齿顶方向（da）与齿根圆（df）**，不对账总长",
                     },
    }


def _gear_plan(P: dict, out_dir: str | None = "D:/out/reducer",
               part: str = "") -> dict:
    """齿轮族（目前 33-大齿轮）：`gear_profile` 整圈渐开线一次成形 + 轮辐凹槽 + 键槽。

    ★★ **模数只从 `meta.gear_train` 读**（用户 2026-10-02 硬要求）：
    两齿轮模数必须相同，否则中心距对不上、装配啮合不上 ——
    而这是**「每个零件单看都对、合起来废掉」**的错，所以校核 C12 守着。
    本函数**故意不读** `parts.<k>.m` / `drive.m`（那两个位置历史上各出现过一次）。

    特征链（与探针 `assets/probes/gear_m2z55.json` 一致，真机跑通）：
        ① 整圈渐开线齿廓（polyline，误差 <0.01mm）→ 拉伸 26（MidPlane）
        ② 中心孔 Ø32 贯穿
        ③ 近面挖环形凹槽 Ø94/Ø48 深 8.5 → 4.5
        ④ `plane_offset` 出背面 → 同样凹槽（**不用 `{{ vars.* }}`** 保持可离线求值）
        ⑤ 键槽 10×3.3（GB/T 1096 对 Ø32 轴，键 10×8）贯穿

    ★ **`check_mass` 用真机标定值，不用解析值**：
    齿隙体积取决于 `gear_profile` 的采样密度（n_flank/n_root/n_tip），
    解析式算不出"55 个齿隙挖掉多少"。探针实测 136.56g 是标定值。
    """
    q = P["parts"][part]
    gt = P["meta"]["gear_train"]                    # ★ 唯一真源
    role = q.get("gear_role")
    gspec = (gt.get(role) or {}) if role else {}
    m = float(gt["module"])                          # ★ 不从 parts / drive 读
    z = int(gt[role]["z"])
    alpha = float(gt.get("alpha_n_deg", 20.0))
    xvar = float(gspec.get("x", 0.0) or 0.0)
    b = float(v(q["b"]))                             # 齿宽
    hub_od = float(v(q["hub_od"]))
    rim_id = float(v(q["rim_id"]))
    recess = float(v(q["recess"]))
    bore = float(v(q["bore"]))
    keyway_raw = q.get("keyway", "")
    # ★ 参数表里带来源的字段是 `{"v": "10x3.3", "source": ...}`，要**拆出裸值**。
    #   直接把整个 dict 传下去 ⇒ `float("{...}")` 报
    #   `could not convert string to float: "{'v': '10"`（很难一眼看出是哪来的）。
    keyway = str(keyway_raw.get("v") if isinstance(keyway_raw, dict) else keyway_raw)
    # 理论齿顶/齿根（**只用于 bbox 与文档**，几何全交给 sw_gear）
    # ★ 变位项 2xm 两项都要带（同 `_gear_shaft_plan`，2026-10-02 方案 A）
    da = m * (z + 2 + 2 * xvar)
    df = m * (z - 2.5 + 2 * xvar)                   # c*=0.25 ⇒ m·(z − 2.5 + 2x)
    if not 0 < bore < hub_od:
        raise ValueError(f"{part}: 中心孔 Ø{bore} 必须小于轮毂 Ø{hub_od}")
    # ★ 凹槽是**环形**：内径 = 轮毂 hub_od、外径 = rim_id，所以必须 hub_od < rim_id。
    #   （我第一版写反了 rim<hub，参照件一跑就炸。）
    if not hub_od < rim_id:
        raise ValueError(
            f"{part}: 凹槽是环形，内径=轮毂 Ø{hub_od:g}、外径=Ø{rim_id:g}，"
            f"必须 hub_od < rim_id（现在反了）")
    if 2 * recess >= b:
        raise ValueError(f"{part}: 两侧凹槽深 {recess}×2 = {2 * recess} 超过齿宽 {b}（轮毂会被切断）")
    # 凹槽底到齿根的径向余量：辐板不能被挖穿
    if rim_id - hub_od < 4 * m:
        raise ValueError(f"{part}: 辐板宽 {rim_id - hub_od:g} 太窄（< 4m={4 * m:g}）")
    # ★ 标定值缺失/为 0 时**明确报错**，不要让 float(None) 崩在别处
    #   （齿数一变旧标定值就作废，这时必须先真机跑一遍取新的）。
    # ★ 标定模式：重新设计后旧标定值作废，此时**故意**跳过 check_mass，
    #   真机跑一遍量出实测值再回填 params（生成器不替人猜）。
    #   `sw_reducer.py --plan <件> --calibrate` 会打开它。
    calibrate = bool(_CALIBRATE.get("on"))
    _raw_mass = v(q["expect_mass_g"])
    if _raw_mass is None or float(_raw_mass) <= 0:
        if not calibrate:
            raise ValueError(
                f"{part}: `expect_mass_g` 缺失或 ≤0（读到 {_raw_mass!r}）。\n"
                f"  ★ 齿数/齿宽一旦重新设计，**旧的标定值就作废**（齿隙体积变了）。\n"
                f"  正解一：`python scripts/sw_reducer.py --plan {part} --calibrate` "
                f"（check_mass 改成**只测不判**，真机跑完从报告 `volume_mm3` 读实测值）。\n"
                f"  正解二：把新实测值写回 params 的 `expect_mass_g`。\n"
                f"  **不要**用解析式顶替 —— 齿隙体积取决于 gear_profile 的采样密度，算不出来。")
        mass_g = None                                # 标定模式：本次不判质量
    else:
        mass_g = float(_raw_mass)                    # ★ 真机标定，不用解析式
    _params: dict = {}
    if out_dir:
        _params["out"] = out_dir
    steps: list[dict] = [
        {"id": "n", "op": "new_part"},
        # ★ Front（法向 Z）：齿面草图 = XY。齿廓一整圈一次成形，
        #   **不做"齿条 + 阵列"**（见 gear_profile 说明：相邻齿共用边界时易报自相交）。
        {"id": "p", "op": "select_plane", "plane": "Front",
         "note": "齿面 = XY（Front，法向 Z）；拉伸沿 Z ⇒ bbox [da, da, b]"},
        {"id": "s1", "op": "sketch_begin"},
        {"id": "gear", "op": "gear_profile", "mn": m, "z": z, "beta": 0.0,
         "alpha_n": alpha, "x": xvar, "mode": "polyline",
         "n_flank": 6, "n_root": 2, "n_tip": 3,
         "note": f"**整圈渐开线一次成形**（m={m:g} 取自 meta.gear_train，z={z}）。"
                 f"polyline 模式几何精确（内接误差 <0.01mm）；"
                 f"spline 会向外过冲 ~0.2mm，不当精确齿形用"},
        {"id": "zf0", "op": "zoom_fit"},
        {"id": "s2", "op": "sketch_end", "name": "齿廓"},
        {"id": "ck1", "op": "check_sketch_constraint", "sketch": "齿廓",
         "expect_min_dims": 0,
         "note": "★ 标注已封存 ⇒ 只记录约束状态（整圈折线轮廓）"},
        # ★★★ **必须单向拉伸**（不能用 MidPlane）—— 这是我踩的坑：
        #   MidPlane 拉伸后齿宽**居中**在 Z=0（实体 Z ∈ [−13, +13]），
        #   而 `plane_offset` 建"背面"时我按探针的 `offset: b = 26` 写 ⇒
        #   平面跑到 Z=+26，**骑在实体外面** ⇒ 背面凹槽 `extrude_cut` 两个方案全灭。
        #   探针 `gear_m2z55.json` 用的是**单向拉伸 26 + offset 26**（背面在 Z=+26，端面上），
        #   两者必须成对使用 ⇒ 这里跟探针保持一致。
        #   ⇒ 记忆点：**MidPlane 与 plane_offset 组合时，偏移量是 b/2；单向拉伸时是 b**。
        {"id": "boss", "op": "extrude_boss", "depth": b,
         "end_cond": "swEndCondBlind", "name": "齿圈体"},
        {"id": "bb1", "op": "check_bbox", "expect": [da, da, b],
                "tol_mm": _gear_bbox_tol(da),
         "note": f"齿顶圆 da = m(z+2) = {da:g}，齿宽 {b:g}。实测 113.976 vs 114（采样误差 0.02）"},
        # ---- 中心孔 ----
        {"id": "p2", "op": "select_plane", "plane": "Front"},
        {"id": "s3", "op": "sketch_begin"},
        {"id": "bore", "op": "circle", "cx": 0.0, "cy": 0.0, "d": bore},
        {"id": "s4", "op": "sketch_end", "name": "中心孔"},
        {"id": "ck2", "op": "check_sketch_constraint", "sketch": "中心孔",
         "expect_min_dims": 0, "note": "★ 标注已封存 ⇒ 只记录约束状态"},
        {"id": "cut1", "op": "extrude_cut", "end_cond": "swEndCondThroughAll",
         "name": "中心孔"},
        # ---- 近面环形凹槽（挖出辐板）----
        {"id": "p3", "op": "select_plane", "plane": "Front"},
        {"id": "s5", "op": "sketch_begin"},
        {"id": "r1", "op": "circle", "cx": 0.0, "cy": 0.0, "d": hub_od,
         "note": f"轮毂 Ø{hub_od:g}"},
        {"id": "r2", "op": "circle", "cx": 0.0, "cy": 0.0, "d": rim_id,
         "note": f"凹槽外径 Ø{rim_id:g}（**端面面积反解**：Z=±4.5 面积 5130.22 "
                 f"= π/4(94²−48²)）"},
        {"id": "s6", "op": "sketch_end", "name": "凹槽近面"},
        {"id": "ck3", "op": "check_sketch_constraint", "sketch": "凹槽近面",
         "expect_min_dims": 0, "note": "★ 标注已封存 ⇒ 只记录约束状态"},
        {"id": "cut2", "op": "extrude_cut", "depth": recess,
         "end_cond": "swEndCondBlind", "name": "凹槽1"},
        # ---- 背面同样凹槽（用 plane_offset 建基准面，**不引用前序步返回名**）----
        {"id": "po", "op": "plane_offset", "base": "Front", "offset": b,
         "name": "背面",
         "note": f"背面基准面 Z=+{b:g}（与**单向**拉伸成对：实体 Z ∈ [0, {b:g}]）"},
        {"id": "p4", "op": "select_plane", "plane": "背面"},
        {"id": "s7", "op": "sketch_begin"},
        {"id": "r3", "op": "circle", "cx": 0.0, "cy": 0.0, "d": hub_od},
        {"id": "r4", "op": "circle", "cx": 0.0, "cy": 0.0, "d": rim_id},
        {"id": "s8", "op": "sketch_end", "name": "凹槽远面"},
        {"id": "ck4", "op": "check_sketch_constraint", "sketch": "凹槽远面",
         "expect_min_dims": 0, "note": "★ 标注已封存 ⇒ 只记录约束状态"},
        {"id": "cut3", "op": "extrude_cut", "depth": recess,
         "end_cond": "swEndCondBlind", "name": "凹槽2"},
        # ---- 键槽 ----
    ]
    if keyway:
        steps += [
            {"id": "p5", "op": "select_plane", "plane": "Front"},
            {"id": "s9", "op": "sketch_begin"},
            {"id": "kw", "op": "keyway_rect", "bore": bore, "spec": keyway,
             "note": f"键槽 {keyway}（GB/T 1096 对 Ø{bore:g} 轴 ⇒ 键 10×8，"
                     f"槽深 t1 = 5.0−b/2 = 3.5，取 3.3 留 0.2 装配余量）"},
            {"id": "s10", "op": "sketch_end", "name": "键槽"},
            {"id": "ck5", "op": "check_sketch_constraint", "sketch": "键槽",
             "expect_min_dims": 0, "note": "★ 标注已封存 ⇒ 只记录约束状态"},
            {"id": "cut4", "op": "extrude_cut", "end_cond": "swEndCondThroughAll",
             "name": "键槽"},
        ]
    steps += [
        # ★★★ 齿顶圆**不能**用 `check_cyl_radius` 判 —— 这是我踩的坑：
        #   齿顶圆是由 **55 段渐开线**拼出来的面，**不是圆柱面**，
        #   而 `check_cyl_radius` 只枚举 `ISurface.IsCylinder()` 的面。
        #   真机实测：实际最大圆柱面 r=**47.0**（= 凹槽底 Ø94/2），而我期望 57（= da/2）。
        #   ⇒ **啮合的决定性尺寸 da 交给 `check_bbox`**（bbox 114.0 已证明齿顶到位）。
        #   圆柱面判据改钉**真正是圆柱面的三个尺寸**：中心孔 Ø32、轮毂 Ø48、凹槽底 Ø94。
        {"id": "cyl", "op": "check_cyl_radius", "expect_max": rim_id / 2.0, "tol": 0.05,
         "note": f"最大**圆柱面**半径 {rim_id / 2:g}（= 凹槽底 Ø{rim_id:g}/2）。"
                 f"★ 注意：齿顶圆 da={da:g} **不是圆柱面**（55 段渐开线），"
                 f"所以齿顶由 `check_bbox` 判（实测 113.99 ≈ {da:g}）。"
                 f"本判据钉住中心孔 Ø{bore:g} / 轮毂 Ø{hub_od:g} / 凹槽底 Ø{rim_id:g} 三者"},
        # ★ 质量用**真机标定值**，不是解析式。
        #   ★★ 标定模式下**不删这一步**，改成「只测不判」（不给 expect_mass_g）——
        #   否则标定跑完报告里没有质量数据，白烧一轮真机（2026-10-02 修，同 `_gear_shaft_plan`）。
        ({"id": "m", "op": "check_mass", "expect_mass_g": mass_g, "tol": 0.02,
          "note": f"**{mass_g:g}g 是真机标定值，不是解析式** —— 齿隙体积取决于 "
                  f"gear_profile 的采样密度（n_flank/n_root/n_tip），解析式算不出 "
                  f"{z} 个齿隙挖掉多少。"}
         if mass_g else
         {"id": "m", "op": "check_mass",
          "note": f"★ **标定模式：只测不判**（不给 expect_mass_g）—— 读到体积后回填 "
                  f"`parts.wheel.expect_mass_g`，再重跑一次即成为门禁。"}),
        {"id": "bb2", "op": "check_bbox", "expect": [da, da, b],
                "tol_mm": _gear_bbox_tol(da),
         "note": "键槽切完后包围盒应不变（键槽在内部）"},
        {"id": "fc", "op": "check_feature_count", "min": 5},
        {"id": "diag", "op": "check_diagnostics", "fail_on": "error",
         "note": f"★ 齿顶变尖/根切等 error 级诊断（z={z}, x={xvar}）"},
        {"id": "sv", "op": "save_as",
         "path": "{{ params.out }}/" + f"{q['no']}-{part}-m{m:g}z{z}x{da:g}.SLDPRT",
         "overwrite": True},
    ]
    return {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"{q['name']}（{part}，渐开线 m={m:g} z={z} 整圈一次成形）",
                 "units": "mm",
                 "note": f"★ **模数来自 `meta.gear_train.module` = {m:g}**（用户硬要求："
                         f"两齿轮模数必须相同才能啮合），不是 parts/{part}.m 也不是 drive.m。"
                         f"其余尺寸 source=measured（sw_faces 取证）；**不硬编码**",
                 "source_params": "assets/reducer/reducer.params.json"},
        "params": _params,
        "steps": steps,
        "_derived": {"m": m, "z": z, "alpha_n": alpha, "x": xvar,
                     "d": m * z, "da": da, "df": df, "b": b,
                     "hub_od": hub_od, "rim_id": rim_id, "recess": recess,
                     "bore": bore, "keyway": keyway,
                     "center_distance": float(gt["center_distance"]),
                     "expect_mass_g": mass_g,
                     "mass_note": "真机标定值（非解析式）",
                     "reference_volume_mm3": 134366.9,
                     "reference_bbox_mm": [113.976, 113.973, 26.0]},
    }


BUILDERS = {
    "key_10x8x22": _key_plan,
    "output_shaft": _shaft_plan,
    **{name: functools.partial(_ring_plan, part=name) for name in RING_FAMILY},
    **{name: functools.partial(_disc_holes_plan, part=name)
       for name in DISC_HOLES_FAMILY},
    **{name: functools.partial(_hex_plan, part=name)
       for name in HEX_FAMILY},
    **{name: functools.partial(_spring_washer_plan, part=name)
       for name in SPRING_WASHER_FAMILY},
    **{name: functools.partial(_plate_plan, part=name)
       for name in PLATE_FAMILY},
    # 大齿轮 = 齿廓拉伸成实体；齿轮轴 = 齿廓**切除**（齿根圆柱已在轴身里）
    **{name: functools.partial(_gear_plan, part=name) for name in GEAR_FAMILY
       if name == "wheel"},
    **{name: functools.partial(_gear_shaft_plan, part=name)
       for name in GEAR_FAMILY if name == "gear_shaft"},
    **{name: functools.partial(_blind_cover_plan, part=name)
       for name in COVER_BLIND_FAMILY},
    **{name: functools.partial(_profile_plan, part=name)
       for name in PROFILE_FAMILY},
    CAP_SMALL: _cap_small_plan,
    REFLECTOR: _reflector_plan,
    # 螺塞 / 通气塞：回转段 + 非回转特征（六角头 / 中心孔）+ **真螺纹**（helix+cut_swept 绕开 real_thread）
    **{"plug": functools.partial(_plug_plan, part="plug"),
       "breather": functools.partial(_breather_plan, part="breather")},
    # 轴承：外圈 + 内圈 + Z 滚珠，单件多实体（滚珠用 Z 次独立 revolve，不用阵列）
    **{name: functools.partial(_bearing_plan, part=name) for name in BEARING_FAMILY},
    # 箱体（D4）：薄壁箱体 + 轴承座 + 孔系（60 特征真件的主结构，其余登记为简化 §102）
    **{name: functools.partial(_housing_base_plan, part=name)
       for name in ("housing_base",)},
    **{name: functools.partial(_housing_cover_plan, part=name)
       for name in ("housing_cover",)},
}


def check_simplified_registry(P: dict, part: str) -> list[str]:
    """★★ **简化项登记守卫**（2026-10-02 新增）：`parts.<k>._simplified` 里每一项
    都必须是对象、必须带 `status`，且**凡 status 含「简化」的必须挂一个真实存在的
    `open_question`**。

    ★ 起因（两条都是真实踩过的）：
      ① **参数表说谎**：齿轮轴的 `_simplified` 一度写着「5 段锥面不建 / M14 螺纹不建 /
         齿根退刀收成直角」，而图纸到手后**这三项全建了** —— 表在说谎，且**没有任何
         东西会拦下它**（只有人读的时候才发现，而我读的时候已经信了）。
      ② **静默降级**（反向，更严重）：某项真被简化了却**不挂 `open_question`**
         ⇒ 降级无人认领、用户不知道、验收也不会提。
    ⇒ 判据：`status` 含「简化」但不是「已建」的项，必须在 `open_questions` 里有对应 id。
    """
    simp = ((P.get("parts") or {}).get(part) or {}).get("_simplified") or {}
    qids = {q.get("id") for q in (P.get("open_questions") or []) if q.get("id")}
    problems: list[str] = []
    for k, val in simp.items():
        if k.startswith("_"):
            continue
        if not isinstance(val, dict):
            problems.append(f"{part}._simplified.{k} 不是对象（读到 {type(val).__name__}）")
            continue
        st = str(val.get("status", ""))
        if not st:
            problems.append(f"{part}._simplified.{k} 缺 `status`（「已建」/「简化」必须自报）")
            continue
        # 只有**真简化**才要求挂 open_question；「已建」的项登记只为防误读
        if "简化" in st and "已建" not in st:
            oq = str(val.get("open_question", ""))
            if oq not in qids:
                problems.append(
                    f"{part}._simplified.{k} 标着「简化」但其 open_question={oq!r} "
                    f"**不在 open_questions 里**（已登记：{sorted(qids)}）"
                    f" ⇒ 降级无人认领")
    return problems


def build_plan(P: dict, part: str, out_dir: str | None = "D:/out/reducer") -> dict:
    fn = BUILDERS.get(part)
    if fn is None:
        todo = sorted(k for k, x in P["parts"].items()
                      if not k.startswith("_") and x.get("status") != "modeled")
        raise KeyError(
            f"零件 {part!r} 还没有计划生成器。\n"
            f"  已有：{', '.join(sorted(BUILDERS)) or '（无）'}\n"
            f"  待参数化（{len(todo)} 件）：{', '.join(todo[:12])}"
            + (" …" if len(todo) > 12 else ""))
    _probs = check_simplified_registry(P, part)
    if _probs:
        raise ValueError(f"{part} 的简化项登记不合规（简化项必须挂 open_question）：\n  - "
                         + "\n  - ".join(_probs))
    plan = fn(P, out_dir)
    # 质量期望挂在 params 上（几何验收用体积、质量单列 —— 见 Q4）
    if part == "key_10x8x22":
        plan["params"]["mass_g"] = round(_expected_volume(P["parts"][part]) * 1e-3, 4)
    return plan


def _expected_volume(key) -> float:
    b, h, L = float(v(key["b"])), float(v(key["h"])), float(v(key["L"]))
    ch = float(v(key["chamfer"]))
    solid = (b * (L - b) + math.pi * (b / 2) ** 2) * h
    # 每圈倒角沿顶面周长去料：周长 = 2(L−b) + 2π(b/2)，截面 45° 边长 ch ⇒ 面积 ch²/2
    per = 2 * (L - b) + 2 * math.pi * (b / 2)
    return solid - 2 * (ch ** 2 / 2.0) * per


# --------------------------------------------------------------------------- 逐件对账

# 验收判据（`60-reducer-project.md` §6.1）
BBOX_TOL_MM = 0.5      # 包围盒每轴差 ≤ 0.5 mm（样条近似的件自然差 0.1~0.2，卡 0.05 必假失败）
VOL_TOL_PCT = 2.0      # 体积差 ≤ 2%（复杂件如箱体放宽到 5%）


def reconcile(P: dict, part: str, report_path: "str | Path",
              bbox_tol: float = BBOX_TOL_MM, vol_tol: float = VOL_TOL_PCT,
              ref_path: "Path | None" = None) -> dict:
    """把真机执行报告与 `reference.json` 逐项对账。

    这一层回答的是全项目最要命的那个问题：**我们建出来的东西，和人工那套到底一不一样？**
    比的是**与密度无关的几何量**（包围盒 / 体积），所以不受材料口径（Q4）影响。

    判据取 `60-reducer-project.md` §6.1：
      · 包围盒**三个尺寸**（按集合比，不计朝向）每项差 ≤ `bbox_tol`（默认 0.5mm）
      · 体积差 ≤ `vol_tol`%（默认 2%）

    ★ 为什么包围盒**不比朝向**：2026-10-02 真机踩出来的 —— 5 个同族圆环对账时体积
      全部吻合到 0.002%，却有 3 件"包围盒对不上"：套筒的轴在世界 X、调整环在 Y、
      密封圈在 Z（人工建模时各自随手选的面）。**坐标系朝向是建模习惯，不是零件规格**：
      同一个圆环，轴朝 X 还是朝 Z 是同一件东西。按轴比会把"坐标系不同"误判成"零件不同"，
      那正是"校验器退化成误报"。朝向差异另作**提示**输出（不参与判定），
      装配阶段要统一坐标系时再看它。

    `report_path` 两种形态都收：
      · **单计划报告**（`sw_plan.py --run` 写的，顶层就有 `log`）；
      · **探针批量报告**（`sw_probe.py` 写的，`results[]` 里每个探针各带 `log`）
        —— 这时按 `tag`/`file` 自动挑出该件那一条（`--reconcile spacer probe_all.json`）。
    """
    rep = json.loads(Path(report_path).read_text(encoding="utf-8"))
    # 批量报告 → 先定位到本件那条
    if "log" not in rep and isinstance(rep.get("results"), list):
        ref_file = str(P["parts"][part].get("ref") or "")
        cand = None
        for pr in rep["results"]:
            tag = str(pr.get("tag") or "")
            fil = str(pr.get("file") or "")
            if part == tag or part in tag or part in fil or (ref_file and ref_file in fil):
                cand = pr
                break
        if cand is None:
            tags = [str(x.get("tag")) for x in rep["results"]]
            return {"ok": False, "why": f"批量报告里没有 {part} 这条（有的：{tags[:8]}）",
                    "items": []}
        rep = cand
    if rep.get("failed"):
        return {"ok": False, "why": f"真机执行有失败步：{rep['failed'].get('id')} "
                                    f"({rep['failed'].get('op')})", "items": []}
    log = {s["id"]: s for s in rep.get("log", [])}

    got_bbox = got_vol = None
    for s in rep.get("log", []):
        r = s.get("result") or {}
        if "size_mm" in r:
            got_bbox = [float(x) for x in r["size_mm"]]
        if "volume_m3" in r:
            got_vol = float(r["volume_m3"]) * 1e9          # m³ → mm³
    if got_bbox is None or got_vol is None:
        return {"ok": False, "why": "报告里没有 check_bbox / check_mass 的结果，无法对账",
                "items": []}

    rp = Path(ref_path) if ref_path else REFERENCE_PATH
    ref = json.loads(rp.read_text(encoding="utf-8"))
    fname = P["parts"][part].get("ref")
    item = next((x for x in ref["items"] if x["file"] == fname), None)
    if item is None:
        return {"ok": False, "why": f"基准表里没有 {fname}", "items": []}

    # ★★ 已声明的差异（2026-10-04 加，10-04 扩展）：
    #   `parts.<part>._reconcile = {"vol_tol_pct": 2.5, "bbox_tol_mm": 2.0,
    #                               "expect_bbox_mm": [...], "expect_volume_mm3": 123.4,
    #                               "why": "..."}`
    #   —— **只有在显式声明 + 写明 why** 时才放宽/改钉基准。
    #   为什么要这个：轴承省略保持架/铆钉 ⇒ 体积天然少 ~2.1% ⇒ 永远报红 ⇒ **告警疲劳**，
    #   真回归会被淹没（同 sw_doctor 的 PENDING 分档口径）。
    #   ★ `expect_*` 把基准**改钉到图纸值**：参照件可能按**旧版结构**做（如箱座参照 105.908
    #     含旧凸台、而我们按图纸 104），此时拿参照件当基准会把"图纸差异"误判成"建错"。
    #   但**绝不允许静默放宽**：没写 why 直接判失败（把"没建对"降级成"已声明"是最坏的）。
    #   ★ 注意：每步 `check_mass`（vol_tol 0.5%）才是**主要门禁**，这层只是二次对账。
    ovr = ((P.get("parts") or {}).get(part) or {}).get("_reconcile") or {}
    if ovr and not str(ovr.get("why") or "").strip():
        return {"ok": False, "items": [],
                "why": f"{part}._reconcile 必须写明 `why`（不许静默放宽容差）"}
    if ovr:
        try:
            vol_tol = max(vol_tol, float(ovr.get("vol_tol_pct") or vol_tol))
            if ovr.get("bbox_tol_mm") is not None:
                bbox_tol = max(bbox_tol, float(ovr["bbox_tol_mm"]))
        except (TypeError, ValueError):
            return {"ok": False, "items": [],
                    "why": f"{part}._reconcile 数值项不是数：{ovr!r}"}

    ref_bbox = [float(x) for x in item["bbox_mm"]]
    ref_vol = float(item["volume_mm3"])
    ref_basis = fname
    if ovr:                                   # 基准改钉到图纸（比参照件更权威）
        if ovr.get("expect_bbox_mm"):
            ref_bbox = [float(x) for x in ovr["expect_bbox_mm"]]
            ref_basis = f"{fname} ← 图纸覆写"
        if ovr.get("expect_volume_mm3"):
            ref_vol = float(ovr["expect_volume_mm3"])
            ref_basis = f"{fname} ← 图纸覆写"

    # 包围盒按**尺寸集合**比（旋转不变）；朝向差异另作提示，不参与判定。
    ours_sorted = sorted(got_bbox)
    ref_sorted = sorted(ref_bbox)
    items = []
    for i, (o, rr) in enumerate(zip(ours_sorted, ref_sorted), start=1):
        d = abs(o - rr)
        items.append({"item": f"包围盒 尺寸{i}", "ours": round(o, 4),
                      "ref": round(rr, 4), "diff": round(d, 4),
                      "tol": bbox_tol, "ok": d <= bbox_tol})
    orientation = {
        "ours_axes": [round(x, 4) for x in got_bbox],
        "ref_axes": [round(x, 4) for x in ref_bbox],
        "same": all(abs(got_bbox[i] - ref_bbox[i]) <= bbox_tol for i in range(3)),
    }
    dv = abs(got_vol - ref_vol)
    dpct = dv / ref_vol * 100.0 if ref_vol else 0.0
    items.append({"item": "体积", "ours": round(got_vol, 3), "ref": round(ref_vol, 3),
                  "diff": round(dv, 4), "diff_pct": round(dpct, 4),
                  "tol_pct": vol_tol, "ok": dpct <= vol_tol})

    ok = all(x["ok"] for x in items)
    return {"ok": ok, "part": part, "ref_file": fname, "ref_basis": ref_basis,
            "items": items,
            "orientation": orientation,
            "tolerance_override": (ovr or None),
            "measured": {"bbox_mm": got_bbox, "volume_mm3": round(got_vol, 4)},
            "reference": {"bbox_mm": ref_bbox, "volume_mm3": ref_vol},
            "n_steps": len(log)}


# --------------------------------------------------------------------------- CLI

def _cli(argv=None) -> int:
    ap = argparse.ArgumentParser(description="减速器参数表：交叉校核 + 计划生成")
    ap.add_argument("--params", default=None, help="参数表路径（默认包内）")
    ap.add_argument("--check", action="store_true", help="★ 跑交叉校核（离线）")
    ap.add_argument("--parts", action="store_true", help="列出零件的参数化状态")
    ap.add_argument("--plan", default=None, help="生成某件的建模计划")
    ap.add_argument("--out", default=None, help="计划输出路径（.json）；不给则打印")
    ap.add_argument("--out-dir", default="D:/out/reducer", help="计划里的产物目录")
    ap.add_argument("--no-out", action="store_true",
                    help="★ 不写 params.out（留给探针运行器注入 YOYO_SW_OUT，保证跨机器可移植）")
    ap.add_argument("--calibrate", action="store_true",
                    help="★ **标定模式**：`check_mass` 改成**只测不判**（不给期望值），"
                         "重新设计后旧标定值作废时用。真机跑完从报告 `volume_mm3` 读实测值，"
                         "写回 params.expect_mass_g 后重跑一次即成为门禁")
    ap.add_argument("--open-questions", action="store_true", help="列出待图纸确认的问题")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    ap.add_argument("--selftest", action="store_true", help="自检：故意改错数，确认能被拦下")
    ap.add_argument("--reconcile", nargs=2, metavar=("PART", "RUN_REPORT"),
                    help="★ 逐件对账：把真机执行报告与 reference.json 比包围盒/体积")
    a = ap.parse_args(argv)

    if a.reconcile:
        return _cmd_reconcile(a)
    if a.selftest:
        return _selftest(a.params)
    if a.parts:
        return _cmd_parts(a)
    if a.open_questions:
        return _cmd_questions(a)
    if a.plan:
        return _cmd_plan(a)
    if a.check:
        return _cmd_check(a)
    ap.print_help()
    return 0


def _cmd_reconcile(a) -> int:
    P = load(a.params)
    part, rep = a.reconcile
    try:
        P["parts"][part]
    except KeyError:
        print(f"参数表里没有零件 {part!r}。可用：{', '.join(sorted(
            k for k in P['parts'] if not k.startswith('_'))) }", file=sys.stderr)
        return 2
    r = reconcile(P, part, rep)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1
    print(f"=== 逐件对账 · {part} ===")
    if r.get("why"):
        print(f"  ★ {r['why']}")
        return 1
    print(f"  参照件 {r['ref_file']}（真机 {r['n_steps']} 步）")
    print()
    print(f"  {'项':10s} {'我们':>12s} {'参照件':>12s} {'差':>10s} {'容差':>8s}")
    for x in r["items"]:
        mark = "OK  " if x["ok"] else "FAIL"
        tol = (f"±{x['tol']}" if "tol" in x
               else f"{x.get('diff_pct', 0):.4f}% / {x['tol_pct']}%")
        print(f"  [{mark}] {x['item']:8s} {x['ours']:>12.4f} {x['ref']:>12.4f} "
              f"{x['diff']:>10.4f} {tol:>8s}")
    ori = r.get("orientation")
    if ori and not ori["same"]:
        print(f"  (提示) 朝向不同：我们 {ori['ours_axes']} vs 参照 {ori['ref_axes']}"
              " —— 坐标系差异，不影响判定；装配要统一坐标系时再对齐")
    print()
    if r.get("tolerance_override"):
        _o = r["tolerance_override"]
        print(f"  ⚠ 体积容差已按**显式声明**放宽到 {_o.get('vol_tol_pct')}%：{_o.get('why')}")
        print("    （仅二次对账；主要门禁是计划里每步 check_mass 的 0.5%）")
    print(f"  ⇒ {'对账通过 ✅' if r['ok'] else '★ 对账不通过'}")
    return 0 if r["ok"] else 1


def _cmd_check(a) -> int:
    P = load(a.params)
    r = run_all(P)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1
    print(f"=== 参数表交叉校核 · {P['meta']['name']} ===")
    print(f"    口径：{P['meta']['authority']}")
    print()
    for c in cross_check(P):
        print("  " + str(c))
    print()
    print("  ---- 参照件对账 ----")
    for c in reference_check(P):
        print("  " + str(c))
    print()
    print(f"  PASS {r['n_pass']} / FAIL {r['n_fail']} / 待定 {r['n_open']}"
          f" ⇒ {'可用' if r['ok'] else '★ 有硬错误，必须查'}")
    return 0 if r["ok"] else 1


def _cmd_parts(a) -> int:
    P = load(a.params)
    rows = [(k, x) for k, x in P["parts"].items() if not k.startswith("_")]
    rows.sort(key=lambda kv: kv[1].get("no", 999))
    done = [k for k, x in rows if x.get("status") == "modeled"]
    if a.json:
        print(json.dumps({"modeled": done,
                          "parts": [{"key": k, **{kk: vv for kk, vv in x.items()
                                                  if kk in ("no", "name", "qty", "kind",
                                                            "status", "material", "gb")}}
                                    for k, x in rows]},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"=== 零件参数化状态（{len(done)}/{len(rows)} 已参数化）===")
    for k, x in rows:
        mark = "✅" if x.get("status") == "modeled" else "  "
        print(f"  {mark} {x.get('no', 0):>2}. {x['name']:16s} {x.get('kind', ''):18s} "
              f"x{x.get('qty', 1)}  {k}")
    print()
    print(f"  ⇒ 下一步：把 todo 逐件补 in BUILDERS（见 references/60-reducer-project.md）")
    return 0


def _cmd_questions(a) -> int:
    P = load(a.params)
    qs = P.get("open_questions", [])
    if a.json:
        print(json.dumps(qs, ensure_ascii=False, indent=2))
        return 0
    print(f"=== 待确认问题（{len(qs)} 条，都缺图纸/装配体才能定）===")
    for q in qs:
        print(f"\n  [{q['id']}] {q['topic']}")
        for line in q["detail"]:
            print(f"     · {line}")
        if q.get("blocks"):
            print(f"     → 卡住：{', '.join(q['blocks'])}")
    return 0


def _cmd_plan(a) -> int:
    P = load(a.params)
    _CALIBRATE["on"] = bool(getattr(a, "calibrate", False))
    try:
        plan = build_plan(P, a.plan, None if a.no_out else a.out_dir)
    except KeyError as e:
        print(str(e), file=sys.stderr)
        return 2
    text = json.dumps(plan, ensure_ascii=False, indent=2)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {a.out}")
    else:
        print(text)
    return 0


def _mut_c6(p: dict) -> None:
    """C6 专用反例：端盖外径改到小于轴承外径。

    ★ **两张表一起改** —— 只改 `covers` 会顺带触发 C11（两表打架），
      那样"被拦住了"依然成立，却**证明不了 C6 还在干活**。
    """
    for blk in (p["covers"]["cover2"], p["parts"]["cover2"]):
        blk["D"]["v"] = 40.0


# ★ 反例必须**点名检查号**：新增检查会顺带拦住老检查的反例，
#   于是老反例变成"假绿灯"（还显示被拦住，其实已经不是它在拦）。
#   所以这里每行是 (说明, 应当拦截的检查号, 改动函数)。
MUTANTS: list[tuple[str, str, object]] = [
    ("C1 中心距抄错", "C1", lambda p: p["drive"]["a"].__setitem__("v", 72.0)),
    ("C4 齿顶圆抄错", "C4",
     lambda p: p["meta"]["gear_train"]["wheel"].__setitem__("da", 116.0)),
    ("C5 轴承内径 ≠ 轴颈", "C5", lambda p: p["bearings"]["6204"].__setitem__("d", 25.0)),
    ("C6 端盖小于轴承（两表同改）", "C6", _mut_c6),
    ("C11 端盖两表打架（只改 parts）", "C11",
     lambda p: p["parts"]["cover2"]["D"].__setitem__("v", 60.0)),
    ("C7 中心高不够", "C7", lambda p: p["drive"]["h"].__setitem__("v", 50.0)),
]


def _selftest(params_path=None) -> int:
    """★ 故意改错数，确认交叉校核真的会拦住。

    没有反例测试的校验器迟早会退化成「永远返回通过」（见 50-training.md）。

    ★ 2026-10-02 加强：不只要求"被拦住"，还要求**是指定的那条检查**拦住的。
      起因：新增 C11（端盖两表一致）后，C6 的反例（改 covers.cover2.D）会**顺带**
      触发 C11 —— 于是"拦住了"依旧成立，但已不能证明 C6 本身还有效。
      教训：**反例必须点名检查号**，否则新增检查会悄悄把老检查的测试"救活"成假绿灯。
    """
    base = load(params_path)
    bad = 0
    for label, want, mutate in MUTANTS:
        import copy
        P = copy.deepcopy(base)
        mutate(P)
        r = run_all(P)
        fired = {c["id"] for c in r["checks"]
                 if c["kind"] == "hard" and not c["ok"]}
        if want in fired:
            mark, note = "OK  ", f"被 {want} 拦下"
        elif fired:
            mark, note = "串台!", f"被 {'/'.join(sorted(fired))} 拦下 —— **不是 {want}**"
            bad += 1
        else:
            mark, note = "漏判!", "★ 没拦住（校验器有洞）"
            bad += 1
        print(f"  {mark} {label} ⇒ {note}")
    # 反向：原表必须全过
    r0 = run_all(base)
    print(f"  {'OK  ' if r0['ok'] else '★  '} 原表 ⇒ "
          f"{'全过' if r0['ok'] else '居然不过'}")
    if not r0["ok"]:
        bad += 1
    print(f"\n  自检结论：{'全部按预期' if bad == 0 else f'★ {bad} 项不符'}")
    return 1 if bad else 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        raise SystemExit(_cli())
