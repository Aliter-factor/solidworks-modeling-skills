#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_faceset.py —— **平面型零件（多面体 / 棱柱 / 支架类）的面集取证**。

## 为什么需要它（现有工具的盲区）

`sw_faces.py` 给三张表（圆柱台阶 / 平面站位 / 锥面清单），`sw_profile.py` 抽**回转半剖面**
—— 两者都是**绕一根轴**的思维，只能吃回转体（轴、套筒、齿轮、闷盖）。

而真实工程里大量零件是**多面体**：CSWA 校赛支架、减速器箱座/箱盖、端盖的方形凸台、
轴承座、齿轮箱端板…… 它们的共同特征是

    一堆**平面**按法向分成若干「平面族」，每族 = 一个法向 + 一个站位；
    族与族之间两两平行成对（`+X` 与 `−X`），差值就是某个方向上的厚度。

现有工具对这类零件只能给出"一堆平面的面积"，**说不出「这是一件什么样的棱柱」** ——
于是只能靠人看渲染图猜轮廓，再手点 `polyline`。本脚本就是补这一层。

## 输出三样

  ① **面清单** —— 每个面的 `id / 类型 / 面积 / 法向 / 平面方程 / 包围盒 / 圆���面参数`
  ② **平面族表** —— 按法向聚类：`(±nx,±ny,±nz | d)` → 面积、成员面名、站位间距
     ★ 对**成对平行族**给出"这对之间夹着多厚"（= 抽这个方向上的板厚/台阶）
  ③ **棱柱判定** —— 沿每个"可能是挤出轴"的族法向 d 试一遍：
     统计有多少面的法向 ⟂ d（= 侧壁）。若侧壁 ≥ 3 且**全是平面** ⇒
     这件在该方向上**是棱柱**；再报告该方向的"两片盖板"站位差 = **挤出长度**。

## 一条设计红线：**分析逻辑是纯函数**

`read_faces()` 需要 SolidWorks；但 `build_families()` / `prism_verdicts()` /
`signature()` 全是**纯函数**（吃面清单吐结论），所以能在没有 SolidWorks 的机器上被
`sw_selftest.py` 离线钉死 —— 这条对"识别类"工具尤其重要：
读数据的那部分最容易随版本变，**判据那部分必须能离线验**。

## 用法

    export APPDATA="C:\\Users\\OMEN\\AppData\\Roaming"
    /c/Python313/python.exe scripts/sw_faceset.py --doc "C:\\x\\支架.SLDPRT"
    /c/Python313/python.exe scripts/sw_faceset.py --doc xx.SLDPRT --json out.json
    /c/Python313/python.exe scripts/sw_faceset.py --doc xx.SLDPRT --axis 0   # 手工指定挤出轴
    /c/Python313/python.exe scripts/sw_faceset.py --self-test                 # 不开 SW 跑纯函数

★ 本脚本**只读**（`open_doc(read_only=True)`），不保存、不改参照件。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# ---------------------------------------------------------------- 常量与容差

#: 法向聚类容差（度）。平面法向的读取误差在 1e-6 量级，0.5° 足够宽松又能区分
#: 真正不同的朝向（65° 的斜面和竖直面差 25°，绝不会被聚成一族）。
FAMILY_ANGLE_TOL_DEG = 0.5

#: 平面站位容差（**米**）。SolidWorks 里"看起来同一平面"的两张面，站位差
#: 通常在 1e-7 量级；1e-4 mm 是安全的聚合阈值。
FAMILY_DIST_TOL_M = 1e-4


# ================================================================ 纯函数层
# （不需要 SolidWorks —— sw_selftest.py 离线钉的就是这一层）

def face_plane(f: dict) -> tuple[list, float] | None:
    """平面面取 `(法向, 站位)`；非平面回 None。

    站位 = `d`，平面方程 `n·x = d`。法向**统一到"指向实体外侧"**由读数据层负责，
    这里只做原样返回 —— 判族时会把 `n` 与 `-n` 都试一遍（`_same_family`）。
    """
    if f.get("type") != "plane":
        return None
    n = f.get("normal")
    d = f.get("d")
    if not n or d is None:
        return None
    return [float(v) for v in n], float(d)


def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _norm(v) -> float:
    return math.sqrt(sum(x * x for x in v)) or 1.0


def _unit(v) -> list:
    n = _norm(v)
    return [x / n for x in v]


def same_family(a: tuple, b: tuple, angle_tol_deg: float = FAMILY_ANGLE_TOL_DEG,
                dist_tol: float = FAMILY_DIST_TOL_M) -> bool:
    """两个 `(法向, 站位)` 是否**共面**（同一张平面上的两个面片）。

    ★ **法向要按"±都算"处理**：SolidWorks 里同一张实体表面，
      两片相对的面法向是**相反**的，而它们显然共面。
      ⇒ 先试 `n_a ≈ n_b`，再试 `n_a ≈ -n_b`（后者要把 `d` 也取反）。
    ★ 另：共面**不代表同族**（见 `build_families` 的注释 —— 族是"法向轴"，
      站位可以有很多个，那正是板厚/台阶的来源）。
    """
    (na, da), (nb, db) = a, b
    cos_tol = math.cos(math.radians(angle_tol_deg))
    c = _dot(_unit(na), _unit(nb))
    if c >= cos_tol:                 # 同向 ⇒ 站位直接比
        return abs(da - db) <= dist_tol
    if c <= -cos_tol:                # 反向 ⇒ 站位必须**同时取反**才算同一张面
        return abs(da + db) <= dist_tol
    return False


def same_axis(a, b, angle_tol_deg: float = FAMILY_ANGLE_TOL_DEG) -> bool:
    """两个法向是否**同轴**（互相平行，方向可反）。"""
    cos_tol = math.cos(math.radians(angle_tol_deg))
    return abs(_dot(_unit(a), _unit(b))) >= cos_tol


def canonical(plane: tuple) -> tuple:
    """把 `(n, d)` 规范化：法向取单位长度（**方向不改**，改的是族代表，不是面）。"""
    n, d = plane
    return _unit(n), d


def build_families(faces: list[dict], angle_tol_deg: float = FAMILY_ANGLE_TOL_DEG,
                   dist_tol: float = FAMILY_DIST_TOL_M) -> list[dict]:
    """把面清单聚成**平面族**。

    ★★ 族 = **法向轴**分组，**不是**"共面分组"。这个选择是刻意的：
       实体两侧那对平行面（板的两面）是**共面性不同、轴相同**，
       而它们的**间距就是板厚** —— 要是按"共面"聚，它们会被拆成两个族，
       厚度就丢了；反过来若按"轴 + 站位都相同"聚（第一版犯的错），
       它们又会被并成一个族而只剩一个 `d`，**厚度同样丢了**。
       ⇒ 正确口径：**族带一组站位**，`thicknesses` = 相邻站位差。

    每族：
        `n`        族代表法向（取第一个成员的方向，**全程不改**）
        `stations` `[{d, face, area, reversed}]` —— 沿 `n` 的站位（米）
        `area` / `count` / `faces`
        `thicknesses_mm` 相邻站位差（升序）—— 板厚 / 台阶尺寸
    """
    fams: list[dict] = []
    for f in faces:
        pl = face_plane(f)
        if pl is None:
            continue
        nf, df = _unit(pl[0]), pl[1]
        hit = None
        for fam in fams:
            if same_axis(fam["n"], nf, angle_tol_deg):
                hit = fam
                break
        if hit is None:
            n = nf
            fams.append({"n": n, "stations": [], "area": 0.0, "count": 0,
                         "faces": []})
            hit = fams[-1]
        # 站位要换算到**族代表方向**上：面法向与族代表反向时，d 要取反
        s = 1.0 if _dot(hit["n"], nf) >= 0 else -1.0
        hit["stations"].append({"d": df * s, "face": f.get("id"),
                                "area": float(f.get("area") or 0.0),
                                "reversed": s < 0})
        hit["area"] += float(f.get("area") or 0.0)
        hit["count"] += 1
        hit["faces"].append(f.get("id") or f.get("name") or "?")
    for fam in fams:
        ds = sorted({round(st["d"], 9) for st in fam["stations"]})
        fam["station_ds"] = ds
        fam["thicknesses_mm"] = [round((ds[i + 1] - ds[i]) * 1000, 4)
                                 for i in range(len(ds) - 1)]
    fams.sort(key=lambda f: -f["area"])
    return fams


def prism_verdicts(faces: list[dict], fams: list[dict] | None = None,
                   angle_tol_deg: float = FAMILY_ANGLE_TOL_DEG,
                   volume_mm3: float | None = None,
                   vol_tol_pct: float = 1.0) -> list[dict]:
    """沿每个候选轴试一遍"这是不是一件棱柱"，返回每个候选轴的判定。

    候选轴 = **所有平面族法向**（一件多面体零件的挤出轴一定是某个面的法向）。
    对每个候选 `d`：
        · `caps`   = 法向 ∥ d 的面（盖板）—— 站位差 = **挤出长度**
        · `sides`  = 法向 ⟂ d 的面（侧壁）
        · `n_sides >= 3` 只是**必要条件**，**不是**充分条件 ★★

    ★★ 为什么必须再加一条**体积判据**（2026-10-03 真机打脸）：
       第一版判据只有 `侧壁数 >= 3`，结果 **1-箱座（201 个面）在 6 个方向上全部
       被判成"棱柱"** —— 因为面一多，任何方向都凑得出 3 个以上侧壁。
       这就是典型的**"部分通过"型假绿灯**：判据形同虚设，还给出很自信的结论。
       真正过硬的判据是**体积**：棱柱的体积 = 盖板面积 × 挤出长度，
       所以 `|V − A_cap·L| / V <= vol_tol_pct` 才算棱柱。
       箱座上这条立刻不成立（截面沿长度是变的）⇒ 正确地判成"不是棱柱"。
       ★ 没有体积数据时（`volume_mm3=None`）**只报"侧壁够多"并显式标注
         `verdict="weak"`** —— 宁可说"证据不足"，也不给一个假的肯定。
    """
    fams = build_families(faces) if fams is None else fams
    cos_tol = math.cos(math.radians(angle_tol_deg))
    out: list[dict] = []
    for fam in fams:
        d = _unit(fam["n"])
        caps: list = []
        sides: list = []
        other = 0
        for f in faces:
            pl = face_plane(f)
            if pl is None:
                other += 1
                continue
            c = abs(_dot(_unit(pl[0]), d))
            if c >= cos_tol:
                s = 1.0 if _dot(d, _unit(pl[0])) >= 0 else -1.0
                caps.append({"d": pl[1] * s, "face": f.get("id"),
                             "area": float(f.get("area") or 0.0)})
            elif c <= 1.0 - cos_tol:
                sides.append((f, pl))
        cap_ds = sorted({round(c["d"], 9) for c in caps})
        length = (max(cap_ds) - min(cap_ds)) if len(cap_ds) >= 2 else None
        # 截面面积 = **某一个站位上**那些盖板面积之和，取**最大**的那一组。
        # ★★ 踩过的坑：第一版把**所有**盖板面积直接相加 ⇒ 两个盖板被算了两遍
        #   ⇒ 理论体积正好是实际体积的 2 倍 ⇒ 偏差**恰好 100.00%**。
        #   这个"整 100%"本身就是信号（真机 CSWA 支架上第一次跑出来就是 100.00%）。
        #   正确口径：棱柱的两个端面**等大**，所以取任一端都行；用"最大站位组"
        #   是为了容忍端面被拆成多张面（CSWA 沿 Z 有 4 片盖板 = 2+2）。
        by_station: dict[float, float] = {}
        for c in caps:
            k = round(c["d"], 9)
            by_station[k] = by_station.get(k, 0.0) + c["area"]
        cap_area = max(by_station.values()) if by_station else 0.0
        err = None
        if volume_mm3 and length and cap_area > 0:
            theo = cap_area * length            # m³
            vol = volume_mm3 * 1e-9             # mm³ → m³
            err = abs(vol - theo) / vol * 100.0 if vol else None
        weak = len(sides) >= 3
        if err is not None:
            verdict = "prism" if (weak and err <= vol_tol_pct) else "not_prism"
        else:
            verdict = "weak" if weak else "not_prism"
        out.append({
            "axis": [round(v, 6) for v in d],
            "axis_family_faces": list(fam["faces"]),
            "n_caps": len(caps), "cap_ds": cap_ds,
            "cap_area_mm2": round(cap_area * 1e6, 2),
            "n_sides": len(sides),
            "side_face_ids": [f.get("id") for f, _pl in sides],
            "is_prism": verdict == "prism",
            "verdict": verdict,
            "prism_volume_err_pct": None if err is None else round(err, 3),
            "length_mm": None if length is None else round(length * 1000, 4),
            "volume_mm3": volume_mm3,
            "n_curved_excluded": other,
        })
    out.sort(key=lambda r: (-r["n_sides"], r["prism_volume_err_pct"]
                            if r["prism_volume_err_pct"] is not None else 1e9))
    return out


def holes_with_normal_holes(faces: list[dict], fams: list[dict] | None = None,
                            angle_tol_deg: float = 3.0) -> list[dict]:
    r"""每个**圆柱面**（候选孔）打在**哪个平面族**上（孔轴垂直于承载面）。

    ★★ 这是「图纸特征判定」最基础的一条：**孔是垂直打在承载面上的**。
      真机（`D:\Desktop\零件CSWA.SLDPRT`，用户手建）抓到 2 个 **Ø6** 孔，
      轴向 `(−0.42, −0.91, 0)`，而 65° 斜筋的平面族法向是
      `(0.9063, −0.4226, 0)` ⇒ **夹角 ≈ 0°** ⇒ 两个孔都是**垂直打斜筋面**的
      法向孔。而 agent 版本**一个圆柱面都没有** ⇒ 孔特征整个丢了。

    `angle_tol_deg` 默认 3°：孔轴与平面法向**严格平行**才认；
    差得多的那些是"侧向孔"（打在立柱侧面之类），会如实报出夹角。

    ★ **实测值 vs 公称**：这里给的是**实体上量到的**直径/长度，
      图纸标的是**公称**（Ø6H7 / M6 等）。**没有图纸标注就分不出
      基孔/间隙孔** —— 工具必须显式提醒，不能替用户假设。
    """
    fams = build_families(faces) if fams is None else fams
    cos_tol = math.cos(math.radians(angle_tol_deg))
    out: list[dict] = []
    for f in faces:
        if f.get("type") != "cylinder" or not f.get("axis"):
            continue
        ax = _unit(f["axis"])
        best, best_cos = None, -2.0
        for fam in fams:
            c = abs(_dot(ax, _unit(fam["n"])))
            if c > best_cos:
                best, best_cos = fam, c
        d = None
        if best is not None and best.get("station_ds"):
            d = float(best["station_ds"][0]) * 1000.0
        length = None
        b = f.get("box")
        if b and len(b) == 6:
            # 孔长 = 沿孔轴方向的 bbox 跨度
            k = max(range(3), key=lambda i: abs(ax[i]))
            length = abs(float(b[k + 3]) - float(b[k])) * 1000.0
        out.append({
            "id": f.get("id"),
            "diameter_mm": (None if f.get("radius") is None
                            else round(float(f["radius"]) * 2000, 4)),
            "axis": [round(v, 4) for v in ax],
            "length_mm": (None if length is None else round(length, 3)),
            "on_family_n": (None if best is None
                            else [round(v, 4) for v in _unit(best["n"])]),
            "on_family_faces": (None if best is None else list(best["faces"])),
            "angle_deg": (None if best is None
                          else round(math.degrees(math.acos(
                              min(1.0, max(-1.0, best_cos)))), 3)),
            "is_normal_hole": bool(best_cos >= cos_tol),
            "note": "实测值，与图纸**公称**可能不同（Ø6 公称可能是 Ø6 间隙孔"
                    "或 Ø5.5 基孔）—— 没有图纸标注分不出来",
        })
    out.sort(key=lambda r: -(r["diameter_mm"] or 0.0))
    return out


def signature(families: list[dict], faces: list[dict],
              relative: bool = True) -> str:
    """面集**签名** —— 用来对账"我建的和参照件是不是同一个形状"。

    构成：
        每个平面族：`法向(取绝对值排序) @ 站位 : 面积`（+ 圆柱面集合 / 曲面标记）
    ★ 面积**进签名**是关键：只比"有几个面"会把"厚 5 的板"和"厚 6 的板"判成一样。
    ★ 法向取绝对值：面法向的朝向取决于建模顺序，比朝向更稳的是**法向的轴向**。

    ★★ `relative=True`（默认）= **平移不变**：站位改用**相邻站位差**（厚度）而不是
      绝对位置。理由：重建件常常建在基准面上、与参照件差一个纯平移
      （`sw_prism --emit-plan` 默认就这样，`origin_shift_mm` 记在 meta 里）——
      用绝对站位的话**平移一下就被判成"形状不同"**，而形状其实一模一样。
      这是"对账"工具最容易给出的假失败。
    """
    items = []
    for fam in families:
        n = sorted(abs(round(v, 3)) for v in fam["n"])
        # ★★★ 签名必须是**规范形**（canonical form）—— 真机踩到：
        #   同一族的一组厚度 `[8.0, 17.37, 16.725, 7.905, 8.0]` 在两个零件里
        #   **顺序不同**（`[8.0, 7.905, 16.725, 17.37, 8.0]`）而**集合完全相同**
        #   ⇒ 不排序就逐项比 ⇒ **形状明明一致却报"不一致"**（假失败）。
        #   ⇒ ① 厚度列表**排序**；② 单站位（无厚度）**统一**写成 `t[0.0]`
        #     （原来一个写 `d@0`、一个写 `t[0.0]` ⇒ 同一件事两种写法）。
        if relative:
            ds = sorted({round(t, 3) for t in fam.get("thicknesses_mm", [])})
            # ★ 空列表与 `[0.0]` 也要统一：真机踩到同一族在两个零件里
            #   一个是 `t[]`（没读到厚度）、一个是 `t[0.0]` ⇒ 假失败。
            tag = "t" + str(ds or [0.0])
        else:
            ds = sorted({round(v, 4) for v in fam.get("station_ds", [])})
            tag = "@" + str(ds)
        items.append(f"P({n[0]},{n[1]},{n[2]}){tag}:"
                     f"{round(fam['area'] * 1e6, 1)}")
    cyl = set()
    other = set()
    for f in faces:
        t = f.get("type")
        if t == "cylinder" and f.get("radius") is not None:
            a = sorted(abs(round(v, 3)) for v in (f.get("axis") or [0, 0, 0]))
            cyl.add(f"C({a[0]},{a[1]},{a[2]})r={round(f['radius'] * 1000, 3)}")
        elif t in ("sphere", "torus"):
            other.add(f"{t[0].upper()}:{round(float(f.get('area') or 0) * 1e6, 1)}")
    return "|".join(sorted(items + list(cyl) + list(other)))


def compare(a: dict, b: dict, vol_tol_pct: float = 0.5,
            area_tol_pct: float = 1.0) -> dict:
    """对账两份面集（`a` = 基准，通常是参照件；`b` = 待验产物）。

    返回 `{"same_shape": bool, "checks": [...], "only_in_a": [...], "only_in_b": [...]}`

    ★ 判据分两层：
      ① **体积**（最硬）—— 相对偏差 ≤ `vol_tol_pct`；
      ② **面集签名**（平移不变）—— 两边必须逐项相同。
      只报"签名不同"是不够的（差一个 2mm 的台阶体积可能只差 0.5%），
      只报体积也是不够的（体积对上但面数/圆角不同）。
    ★ 曲面**逐个比对**（面积 + 类型），平面的面积差按族汇总。
    """
    checks: list[dict] = []
    info: list[str] = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    va, vb = a.get("volume_mm3"), b.get("volume_mm3")
    if va and vb:
        d = abs(va - vb) / va * 100.0
        add("体积", d <= vol_tol_pct,
            f"{va:.1f} vs {vb:.1f} mm³（差 {d:.3f}%，容差 {vol_tol_pct}%）")
    else:
        add("体积", False, "至少一边体积读不到（mass_props 报 suspect）")
    # ★★ 比**尺寸**（max−min，逐轴），**不比绝对 min/max**。
    #   重建件常常建在基准面上、与参照件差一个纯平移（`sw_prism --emit-plan`
    #   默认如此，`origin_shift_mm` 记在 meta 里）—— 拿绝对值比，**平移 1.5mm
    #   就被判成"形状不一致"**，而形状其实逐位相同。这是"对账"最容易给的假失败。
    #   中心偏移单列成**信息**（不进 ok/ok 判定）。
    ba, bb = summarise(a["faces"])["box_mm"], summarise(b["faces"])["box_mm"]
    shift = None
    if ba and bb:
        size_a = [ba[i + 3] - ba[i] for i in range(3)]
        size_b = [bb[i + 3] - bb[i] for i in range(3)]
        dmax = max(abs(x - y) for x, y in zip(size_a, size_b))
        add("包围盒**尺寸**（逐轴）", dmax <= 0.05,
            f"{size_a} vs {size_b}（逐项最大差 {dmax:.4f}mm）")
        shift = [round((bb[i] + bb[i + 3]) / 2 - (ba[i] + ba[i + 3]) / 2, 4)
                 for i in range(3)]
        info.append(f"中心偏移 mm = {shift}"
                    + ("（纯平移，形状不受影响）"
                       if max(abs(x) for x in shift) > 1e-6 else "（完全重合）"))
    else:
        add("包围盒**尺寸**（逐轴）", False, "有边读不到包围盒")
    sa, sb = signature(a["families"], a["faces"]), signature(b["families"], b["faces"])
    set_a, set_b = set(sa.split("|")), set(sb.split("|"))
    only_a, only_b = sorted(set_a - set_b), sorted(set_b - set_a)
    add("面集签名（平移不变）", not only_a and not only_b,
        f"仅参照件有 {len(only_a)} 项、仅产物有 {len(only_b)} 项"
        + (f"；例：仅A {only_a[:2]} 仅B {only_b[:2]}" if (only_a or only_b) else ""))
    return {"same_shape": all(c["ok"] for c in checks), "checks": checks,
            "info": info,
            "only_in_a": only_a, "only_in_b": only_b,
            "signature_a": sa, "signature_b": sb}


def summarise(faces: list[dict]) -> dict:
    """面清单的一句话摘要（给人看的）。"""
    by_type: dict[str, int] = {}
    for f in faces:
        by_type[f.get("type") or "?"] = by_type.get(f.get("type") or "?", 0) + 1
    box = None
    for f in faces:
        b = f.get("box")
        if not b or len(b) != 6:
            continue
        if box is None:
            box = [b[0], b[1], b[2], b[3], b[4], b[5]]
        else:
            for i in range(3):
                box[i] = min(box[i], b[i])
            for i in range(3, 6):
                box[i] = max(box[i], b[i])
    return {
        "n_faces": len(faces),
        "by_type": by_type,
        "box_mm": None if box is None else [round(v * 1000, 3) for v in box],
    }


# ==================================================================
# ★★★ 凹凸性（convexity）—— 「孔」与「凸台」的分水岭
# ==================================================================
#
# 起因（2026-10-03，用户校准 + 截图）：`holes_with_normal_holes()` 只用
# 「孔轴 ⟂ 承载面」判 ⇒ 把 CSWA 上**向上拉伸的凸台**报成了「法向孔」。
# 用户原话：「很明显这里红色是向上拉伸的突起吧？根本不是孔，
# 绿色的也是槽啊，说明现在的判定还有问题。」
#
# ★★★ 根因：**圆柱面本身分不出孔与凸台** —— 两者都是圆柱面、
#   轴都垂直某个平面、直径可能相同。**唯一区别是材料在圆柱内还是外**：
#     · 孔   ⇒ 圆柱**内**是空的（材料在外）
#     · 凸台 ⇒ 圆柱**内**是实的（材料在内）
#
# ★★ 判据（真机 2026-10-03 实测，见 `_diag/diag_convexity.py`）：
#   取**真正的面法线** n（`ISurface.Evaluate` + `IFace2.FaceInSurfaceSense`
#   修正朝向），与**径向** r（面上点相对圆柱轴的横向分量）点积：
#     n·r < 0 ⇒ 法线指向轴心 ⇒ 材料在外 ⇒ **孔（凹）**
#     n·r > 0 ⇒ 法线背离轴心 ⇒ 材料在内 ⇒ **凸台（凸）**
#   CSWA 实测两个圆柱面 n·r = **+1.0000** ⇒ 都是**凸台**（用户在截图里
#   标红的那个向上拉伸的突起）—— 与人工判读完全一致。
#
# ★★ 两个 API 陷阱（都踩过，见坑表 §103）：
#   1. `IsCylinder` 是 **`ISurface` 上的属性**（用 `getattr`），
#      **不是 `IFace2` 上的方法** ⇒ 写成 `member(face,"IsCylinder")` 全 false。
#   2. `ISurface.Evaluate(u,v,0,0)` 返回**扁平数组**
#      `[px,py,pz, nx,ny,nz]`（0 阶导时）⇒ **不能**按 `[0]=点,[1]=法线` 解构。
#
# ★ 官方依据（`sw_apisig --sig IFace2.FaceInSurfaceSense`）：
#   "Checks whether the face normal has the opposite direction (sense) as the
#    underlying surface. ... the normal vector of faces are directed **away
#    from the body material**." ⇒ 面法线总是背离材料，所以要靠曲率判凹凸。

#: `n·r` 判据的容差（小于它算「退化」，不硬判）
CONVEXITY_EPS = 1e-6


def _eval_pt_normal(surf, u: float, v: float):
    r"""`ISurface.Evaluate(u,v,0,0)` → `(point[3], normal[3])`。

    ★★ 返回的是**扁平数组** `[px,py,pz, nx,ny,nz]`（0 阶导时长度 6），
      **不是** `[[x,y,z],[nx,ny,nz]]` —— 早期按后者解构导致「拿不到法线」。
    """
    with contextlib.suppress(Exception):
        res = getattr(surf, "Evaluate")(float(u), float(v), 0, 0)
        if res is None:
            return None
        flat = [float(x) for x in (res if isinstance(res, (list, tuple))
                                   else [res])]
        if len(flat) < 6:
            return None
        return flat[0:3], flat[3:6]
    return None


def _true_face_normal(fe, surf, u: float, v: float):
    """**真正的面法线** = 曲面法线，按 `FaceInSurfaceSense` 决定是否反号。

    ★ `FaceInSurfaceSense()` 为 True 表示「面法线与曲面法线**反向**」。
      `PlaneParams` 给的法线**朝向不可靠**（坑表 §97①），
      而这里先按官方语义修正过 ⇒ 可以直接用来判凹凸。
    """
    got = _eval_pt_normal(surf, u, v)
    if not got:
        return None
    pt, n = got
    sense = None
    with contextlib.suppress(Exception):
        sense = bool(getattr(fe, "FaceInSurfaceSense")())
    if sense:
        n = [-x for x in n]
    return pt, _unit(n), sense


def face_convexity(sw, face: dict) -> dict:
    r"""一个面的**凹凸性**（是孔/凹槽壁，还是凸台/外表面）。

    返回 dict：
    ```
    kind      'hole' | 'boss' | 'flat' | 'unknown'
    n_dot_r   圆柱面：真面法线 · 径向（<0 孔 / >0 凸台）
    sense     FaceInSurfaceSense 的读数（留证据）
    votes     多个 (u,v) 采样点的投票（防单点退化）
    ```
    ★ **平面判不出凹凸**（`kind='flat'`）—— 平面的凹凸要靠**拓扑**
      （见 `classify_slots_bosses`）。这是几何事实，不该硬套。
    """
    out = {"id": face.get("id"), "type": face.get("type")}
    if face.get("type") != "cylinder":
        out["kind"] = "flat" if face.get("type") == "plane" else "unknown"
        return out

    ax = face.get("axis")
    org = face.get("cyl_origin")
    r = face.get("radius")
    if not ax or org is None or not r:
        out["kind"] = "unknown"
        out["why"] = "圆柱面缺 axis/origin/radius"
        return out

    # ★ 直接用 `read_faces` 挂在 rec 上的引用 —— **不要按名字反查**
    #   （`GetName()` 对未命名面返回 None，名字对不上，见 read_faces 注释）。
    fe = face.get("_face2")
    surf = face.get("_surf")
    if surf is None and fe is not None:
        with contextlib.suppress(Exception):
            surf = getattr(fe, "GetSurface")()
    if fe is None:
        out["kind"] = "unknown"
        out["why"] = ("拿不到 IFace2 —— 这条记录**不是 `read_faces()` 产的**"
                      "（自造夹具没有 COM 引用）。★ 真实模型请走 read_faces")
        return out
    votes, senses = [], []
    for u in (0.0, 0.25, 0.5, 0.75):
        got = (_true_face_normal(fe, surf, u, 0.5) if surf is not None
               else None)
        if not got:
            continue
        pt, n, sense = got
        if sense is not None:
            senses.append(sense)
        d = [pt[i] - org[i] for i in range(3)]
        k = _dot(d, ax)
        rad = [d[i] - k * ax[i] for i in range(3)]
        if math.sqrt(_dot(rad, rad)) < 1e-9:
            continue
        votes.append(_dot(n, _unit(rad)))
    if not votes:
        out["kind"] = "unknown"
        out["why"] = ("Evaluate 拿不到点/法线（面名可能对不上，"
                      "或该面不是参数化曲面）")
        return out
    val = sum(votes) / len(votes)
    out["n_dot_r"] = val
    out["sense"] = senses[0] if senses else None
    out["votes"] = [round(v, 4) for v in votes]
    unanimous = all(v < -CONVEXITY_EPS for v in votes) or \
        all(v > CONVEXITY_EPS for v in votes)
    if not unanimous:
        out["kind"] = "unknown"
        out["why"] = "多采样点投票不统一（近退化面）"
    elif abs(val) <= CONVEXITY_EPS:
        out["kind"] = "unknown"
        out["why"] = "n·r ≈ 0（退化圆柱）"
    else:
        out["kind"] = "hole" if val < 0 else "boss"
    return out


# ★★ 已删 `_find_face2()` / `_FACE2_CACHE`（2026-10-03）：
#    它按 `IFace2.GetName()` 找面，而 `read_faces` 的 `Face1..N` 是
#    **按枚举序号**起的名、`GetName()` 对未命名面返回 `None`
#    ⇒ 两套命名永不相等 ⇒ 这个函数**永远返回 None**。
#    正解：`read_faces` 直接把引用挂在 `rec["_face2"]` / `rec["_surf"]`。


def classify_holes_and_bosses(sw, faces: list[dict]) -> dict:
    r"""把**圆柱面**分成「孔」与「凸台」—— 必须读**凹凸性**，不是轴向。

    ★★★ 这修正了 `holes_with_normal_holes()` 的根本缺陷：
      它只用「孔轴 ⟂ 承载面」，而**凸台同样满足**（凸台的圆柱面
      也垂直于它的基面）⇒ 真机上把用户手建的 CSWA 上
      **2 个 Ø6 凸台**报成了「法向孔」。

    返回：
    ```
    {'holes': [{id, diameter_mm, length_mm, axis, on_family, angle_deg}],
     'bosses': [{id, diameter_mm, length_mm, axis}],
     'unknown': [{id, why}],
     'note': str}
    ```
    """
    cyls = [f for f in faces if f.get("type") == "cylinder"]
    out = {"holes": [], "bosses": [], "unknown": [], "note": ""}
    if not cyls:
        out["note"] = ("本件 **0 个圆柱面** —— 若图纸上有孔/圆角/圆柱凸台，"
                       "说明**特征没建全**")
        return out
    for f in cyls:
        c = face_convexity(sw, f)
        rec = {"id": f.get("id"),
               "diameter_mm": round((f.get("radius") or 0.0) * 2000.0, 4),
               "axis": [round(v, 4) for v in (f.get("axis") or [])],
               "center_mm": [round(v * 1000, 3)
                             for v in (f.get("cyl_origin") or [])],
               "n_dot_r": c.get("n_dot_r"),
               "length_mm": _cyl_length_mm(f)}
        if c["kind"] == "hole":
            rec["why"] = ("n·r = {:+.4f} < 0 ⇒ 面法线指向轴心 ⇒ "
                          "**材料在圆柱外** ⇒ 是孔（凹）".format(
                              c.get("n_dot_r") or 0.0))
            out["holes"].append(rec)
        elif c["kind"] == "boss":
            rec["why"] = ("n·r = {:+.4f} > 0 ⇒ 面法线背离轴心 ⇒ "
                          "**材料在圆柱内** ⇒ 是**凸台/轴肩**（不是孔！）"
                          .format(c.get("n_dot_r") or 0.0))
            out["bosses"].append(rec)
        else:
            rec["why"] = c.get("why", "读不出凹凸")
            out["unknown"].append(rec)
    if out["unknown"]:
        out["note"] = ("★ 有 {} 个圆柱面**读不出凹凸**（{}）—— "
                       "不硬判".format(len(out["unknown"]),
                                     out["unknown"][0].get("why")))
    return out


def _cyl_length_mm(f: dict) -> float | None:
    """圆柱面的**轴向长度**（mm）—— 由包围盒沿轴投影得到。"""
    b = f.get("box")
    ax = f.get("axis")
    if not b or len(b) != 6 or not ax:
        return None
    spans = [abs(float(b[i + 3]) - float(b[i])) for i in range(3)]
    # 沿轴的分量 = 跨度 × |轴分量| 的最大值（轴对齐时最准）
    k = max(range(3), key=lambda i: spans[i] * abs(ax[i] or 0.0))
    return round(spans[k] * 1000.0, 4)


# ==================================================================
# ★★★ 槽（slot）—— 平面拓扑的「凹」，圆柱曲率判不出来
# ==================================================================
#
# 起因：用户截图里绿框那片是**槽**，而旧工具根本没有「槽」这个概念。
# 用户原话：「绿色的也是槽啊，说明现在的判定还有问题。」
#
# ★★★ 第一版判据**报了 16 个假阳性**（真机 CSWA），根因值得记：
#   我用的是「**任意两片**近似平行的细长平面，间距在 0.5~60mm」。
#   而一个 L 形箱体的**外轮廓**天然就有几十对平行面（相对的两块大平板、
#   台阶的两级面…）⇒ 判据松到没有区分力。
#   ⇒ **教训：判据必须用「局部、成组」的证据，不能用「全局两两配对」。**
#
# ★★ 正解（真机 CSWA 读数反推出来的）：
#   槽 = **同一个平面族内**，**站位相近**（间距 = 槽深）、**面积几乎相等**、
#   **包围盒几乎重合**的**两片平行面**。这三条合起来才唯一地指认「槽」。
#
#   真机实测（`D:\Desktop\零件CSWA.SLDPRT`，用户手建）：
#   ```
#   面     站位mm    bbox 跨度mm      面积mm2    法向
#   Face6  41.883    15.2 × 32.6      1008.0     (0.906,-0.423,0)   ← 槽壁
#   Face8  36.883    15.2 × 32.6      1008.0     (0.906,-0.423,0)   ← 槽壁
#   ⇒ 站位差 = 5.000mm = 槽深；面积、bbox 逐位相同
#   Face10 21.883    24.2 × 52.0      1090.1     (0.906,-0.423,0)   ← 斜筋**外表面**
#   ⇒ 它没有配对 ⇒ 不是槽
#   ```
#
# ★ 三条判据的分工（每条都排掉一类假阳性）：
#   ① **同族**（法向一致，由 `build_families` 保证）
#      —— 排掉「不同朝向的平行面」（L 形件的 X 面与 Y 面不平行，本就排除）
#   ② **面积几乎相等**（相对差 ≤ 2%）
#      —— 排掉「相对的两块大平板」（面积常差很多）
#   ③ **包围盒几乎重合**（各维相对差 ≤ 5%）
#      —— 排掉「台阶的两级面」（bbox 大小不同）
#   ④ **站位差 = 槽深**在 `SLOT_DEPTH_RANGE` 内，且**不是零**
#      —— 排掉「同一个面被重复记录」

#: 槽壁面积的相对容差
SLOT_AREA_REL_TOL = 0.02
#: 槽壁包围盒各维的相对容差
SLOT_BOX_REL_TOL = 0.05
#: 槽的深度区间（mm）
SLOT_DEPTH_RANGE = (0.3, 40.0)


def _plane_station_mm(f: dict) -> float:
    """平面到主对称面的绝对站位（沿**法向**投影包围盒中点，mm）。

    ★ 必须沿**法向**投影，不能"沿 box 三维里最厚的那一维"：
      键槽侧壁/槽壁是**零厚度**的平面（box 在法向上的跨度 = 0）
      ⇒ "最厚维"必然挑错（坑表 §99④）。
    ★ 沿法向投影还有个好处：**与法向符号无关**（取绝对值），
      所以相对两片 `d` 同值那个坑（§97①）也一并绕开。
    """
    b = f.get("box")
    n = _unit(f.get("normal") or [0, 0, 0])
    if b and len(b) == 6:
        mid = [(float(b[i]) + float(b[i + 3])) / 2.0 for i in range(3)]
        return abs(_dot(n, mid)) * 1000.0
    if f.get("center"):
        return abs(_dot(n, f["center"])) * 1000.0
    return abs(float(f.get("d") or 0.0)) * 1000.0


def _box_sides(f: dict) -> list[float]:
    b = f.get("box")
    if not b or len(b) != 6:
        return []
    return [abs(float(b[i + 3]) - float(b[i])) * 1000.0 for i in range(3)]


def _same_shape(f1: dict, f2: dict) -> bool:
    """两片面的**包围盒**是否几乎重合（各维相对差 ≤ 容差）。"""
    a, b = _box_sides(f1), _box_sides(f2)
    if len(a) != 3 or len(b) != 3:
        return False
    for u, v in zip(a, b):
        m = max(u, v)
        if m < 1e-9:
            continue
        if abs(u - v) / m > SLOT_BOX_REL_TOL:
            return False
    return True


def classify_slots(faces: list[dict], fams: list[dict] | None = None,
                   ) -> dict:
    r"""从**平面**里认「槽」（slot）—— 旧工具完全没有这个概念。

    ★ 为什么必须单列：槽的壁是**平面**，而「孔 vs 凸台」靠**圆柱面曲率**
      ⇒ 平面天生判不出凹凸。槽只能靠**同族内的成组拓扑**来认。

    判据（全部可离线单测，见 `self_test`）：
      ① **同族**（法向一致）—— 传入 `fams`；没传就自己建
      ② 两片**面积几乎相等**（≤ `SLOT_AREA_REL_TOL`）
      ③ 两片**包围盒几乎重合**（≤ `SLOT_BOX_REL_TOL`）
      ④ **站位差**（= 槽深）在 `SLOT_DEPTH_RANGE` 内且 **> 0.1mm**

    返回 `{'slots': [...], 'note': str}`；每条给 `depth_mm / wall_a / wall_b /
    normal / length_mm / width_mm / why`。
    """
    if fams is None:
        fams = build_families(faces)
    out = {"slots": [], "note": ""}
    n_planes = sum(1 for f in faces if f.get("type") == "plane")
    if n_planes < 2:
        out["note"] = "平面不足 2 个，谈不上槽"
        return out

    # ★ `build_families` 的 `fam["faces"]` 存的是**面名字符串**（不是面对象）
    #   —— 真机踩到：`fam["faces"][0].get(...)` 直接 `AttributeError`。
    #   ⇒ 建一张 `id → 面记录` 的表再回查。
    by_id = {f["id"]: f for f in faces if f.get("id")}

    for fam in fams:
        walls = [by_id[i] for i in fam.get("faces", [])
                 if i in by_id and by_id[i].get("box")
                 and len(by_id[i]["box"]) == 6]
        for i in range(len(walls)):
            for j in range(i + 1, len(walls)):
                f1, f2 = walls[i], walls[j]
                a1, a2 = f1.get("area") or 0.0, f2.get("area") or 0.0
                if a1 <= 0 or a2 <= 0:
                    continue
                if abs(a1 - a2) / max(a1, a2) > SLOT_AREA_REL_TOL:  # ②
                    continue
                if not _same_shape(f1, f2):                          # ③
                    continue
                depth = abs(_plane_station_mm(f1) - _plane_station_mm(f2))
                if not (SLOT_DEPTH_RANGE[0] <= depth
                        <= SLOT_DEPTH_RANGE[1]):                    # ④
                    continue
                sides = sorted(_box_sides(f1), reverse=True)
                out["slots"].append({
                    "depth_mm": round(depth, 4),
                    "normal": [round(v, 4) for v in _unit(fam["n"])],
                    "wall_a": f1["id"], "wall_b": f2["id"],
                    "area_mm2": round(a1 * 1e6, 1),
                    "length_mm": round(sides[0] if sides else 0.0, 3),
                    "width_mm": round(sides[1] if len(sides) > 1 else 0.0, 3),
                    "why": ("同族内两片**面积相等（%.1fmm²）**、**包围盒重合**"
                            "（%.1f×%.1fmm）的平行平面，站位差 %.3fmm ⇒ "
                            "**槽的两片壁**（不是孔：孔是圆柱面；"
                            "也不是凸台：凸台是单个圆柱面）"
                            % (a1 * 1e6, sides[0] if sides else 0.0,
                               sides[1] if len(sides) > 1 else 0.0, depth)),
                })
    out["slots"].sort(key=lambda r: -r["depth_mm"])
    if not out["slots"]:
        out["note"] = ("没找到槽。★ **「没找到」不等于「没有槽」** —— "
                       "判据要求两片壁**都完整存在**且面积/包围盒都对得上，"
                       "被圆角/倒角/抽壳打断的槽会漏判（要确认请看孔/圆柱面清单）")
    return out



# ================================================================ 读数据层
# （需要 SolidWorks；晚期绑定的取法照 `sw_faces.py` / `sw_profile.py` 已验证的写法）

def _surface_kind(surf) -> str:
    """从一个 `ISurface` 判类型。顺序有讲究：先问便宜的、再问贵的。"""
    for api, kind in (("IsPlane", "plane"), ("IsCylinder", "cylinder"),
                      ("IsCone", "cone"), ("IsSphere", "sphere"),
                      ("IsTorus", "torus")):
        with contextlib.suppress(Exception):
            if bool(getattr(surf, api)):
                return kind
    return "other"


def _params(surf, api: str, n: int) -> list | None:
    with contextlib.suppress(Exception):
        v = getattr(surf, api)
        if v is not None and len(v) >= n:
            return [float(x) for x in v[:n]]
    return None


def read_faces(sw) -> list[dict]:
    """把当前零件的**每个面**读成一条记录（单位一律**米**）。

    字段：`id / type / area / center / normal / d / box / radius / axis / ...`
    ★ `center` = 用 `IFace2.GetClosestPointOn(0,0,0)` 取的"面上一点"（不是质心，
      但**保证落在面上**）—— 后续所有"点是否在面上"的判据都要靠它。
    """
    from sw_connect import member, typed                      # noqa: PLC0415
    out: list[dict] = []
    for b in sw.bodies():
        body = typed(b, "IBody2")
        if body is None:
            continue
        with contextlib.suppress(Exception):
            faces = list(member(body, "GetFaces") or ())
        for i, f in enumerate(faces, 1):
            fe = typed(f, "IFace2")
            if fe is None:
                continue
            rec: dict = {"id": f"Face{i}", "type": "other", "area": 0.0,
                         "center": None, "normal": None, "d": None, "box": None}
            # ★★ 挂住 COM 对象本身：省掉二次枚举，也**绕开命名对不上**。
            #   ★ 真机踩到（2026-10-03）：`IFace2.GetName()` 对这些面返回
            #     **`None`**（面没命名），而 `read_faces` 的 `Face1..N` 是
            #     **按枚举序号**起的名 ⇒ 两套命名完全不同 ⇒ 任何"按名找面"
            #     的实现（哪怕同一个文件里）都会**永远找不到**。
            #   ⇒ 从此**只认 rec 里的这份引用**，不再按名字反查。
            rec["_face2"] = fe
            with contextlib.suppress(Exception):
                rec["id"] = str(member(fe, "GetName") or rec["id"])
            with contextlib.suppress(Exception):
                rec["area"] = float(member(fe, "GetArea"))
            bx = None
            with contextlib.suppress(Exception):
                v = member(fe, "GetBox")
                if v is not None and len(v) == 6:
                    bx = [float(x) for x in v]
            rec["box"] = bx
            with contextlib.suppress(Exception):
                cp = member(fe, "GetClosestPointOn", 0.0, 0.0, 0.0)
                if cp is not None and len(cp) >= 3:
                    rec["center"] = [float(x) for x in cp[:3]]
            with contextlib.suppress(Exception):
                surf = member(fe, "GetSurface")
                if surf is None:
                    continue
                rec["_surf"] = surf
                rec["type"] = _surface_kind(surf)
                if rec["type"] == "plane":
                    pp = _params(surf, "PlaneParams", 6)
                    if pp:
                        rec["normal"] = _unit(pp[0:3])
                        rec["d_surface"] = pp[3]
                        # ★★★ `d` **不信 API**，用"面上一点"自己算（2026-10-03 真机）。
                        #   `ISurface.PlaneParams` 的第 4 个数与它自己给的法向**不自洽**：
                        #   8-窥视孔盖 的 Face3 实体在 x=+23mm，却回 `n=(−1,0,0), d=+0.023`
                        #   —— 按 `n·x=d` 推得 x=−23，与 `GetBox` 矛盾；同件的 Face5 在
                        #   x=−23mm 回 `n=(+1,0,0), d=−0.023` 却是自洽的。
                        #   ⇒ 不同面的 `d` 符号约定不一致，**按它算站位会算错厚度**。
                        #   而 `center`（`GetClosestPointOn(0,0,0)`，保证落在面上）
                        #   与 `normal` 都是可靠的 ⇒ `d = n·center` 恒等成立。
                        if rec.get("center") and rec.get("normal"):
                            rec["d"] = _dot(rec["normal"], rec["center"])
                            rec["d_mismatch_mm"] = abs(rec["d"]
                                                        - rec["d_surface"]) * 1000.0
                elif rec["type"] == "cylinder":
                    cp2 = _params(surf, "CylinderParams", 7)
                    if cp2:
                        rec["axis"] = _unit(cp2[3:6])
                        rec["radius"] = cp2[6]
                        rec["cyl_origin"] = cp2[0:3]
                elif rec["type"] == "cone":
                    cp2 = _params(surf, "ConeParams", 9)
                    if cp2:
                        rec["axis"] = _unit(cp2[3:6])
                        rec["half_angle_deg"] = math.degrees(cp2[6])
                        rec["cone_origin"] = cp2[0:3]
                elif rec["type"] == "sphere":
                    sp = _params(surf, "SphereParams", 4)
                    if sp:
                        rec["radius"] = sp[3]
                        rec["sphere_center"] = sp[0:3]
                elif rec["type"] == "torus":
                    tp = _params(surf, "TorusParams", 8)
                    if tp:
                        rec["axis"] = _unit(tp[3:6])
                        rec["major"] = tp[6]
                        rec["minor"] = tp[7]
            out.append(rec)
    return out


# ================================================================ 报告

def print_report(faces: list[dict], fams: list[dict], verdicts: list[dict],
                 sig: str, sw=None) -> None:
    """打印面清单报告。

    ★ `sw` 是 `SwSession` —— **孔/凸台的凹凸判据需要 COM 引用**
      （`ISurface.Evaluate` 取真面法线，见 `face_convexity`）。
      不传就**跳过**圆柱面清单并明说原因，不静默。
    """
    s = summarise(faces)
    print(f"面 {s['n_faces']} 个   类型分布 {s['by_type']}")
    print(f"整体包围盒(mm) = {s['box_mm']}")
    print(f"\n== 平面族（{len(fams)} 族，按面积降序；族 = 法向轴分组，"
          f"族内站位差 = 板厚/台阶）==")
    for i, f in enumerate(fams, 1):
        n = f["n"]
        ds = "/".join(f"{v * 1000:+.3f}" for v in f["station_ds"])
        th = ("  厚=" + "/".join(f"{t:g}" for t in f["thicknesses_mm"]) + "mm"
              if f["thicknesses_mm"] else "  （单层，无厚度）")
        print(f"  [{i:2}] n=({n[0]:+.4f},{n[1]:+.4f},{n[2]:+.4f})  "
              f"站位(mm)={ds}  面积={f['area'] * 1e6:9.1f}  面数={f['count']}{th}")
        print(f"        成员: {', '.join(str(x) for x in f['faces'][:8])}"
              + (" ..." if len(f["faces"]) > 8 else ""))
    # 读数自检：PlaneParams 的 d 与自算 d 的偏差
    bad = [f for f in faces if (f.get("d_mismatch_mm") or 0.0) > 0.1]
    if bad:
        print(f"\n[读数自检] ★ {len(bad)} 个平面面** `ISurface.PlaneParams` 的 d 与"
              f"\"n·面上一点\"自算值差 > 0.1mm** ⇒ 本工具一律用自算值。"
              f"（真机实测 `8-窥视孔盖` Face3 在 x=+23mm 却回 d=+0.023 配 n=(−1,0,0)）")
        print(f"            例：" + "；".join(
            f"{f['id']} 差{f['d_mismatch_mm']:.3f}mm" for f in bad[:4]))

    # ---- ★★★ 圆柱面清单：**先分凹凸**（孔 / 凸台），再看轴向 ----
    #   2026-10-03 用户校准：「很明显这里红色是向上拉伸的突起吧？根本不是孔，
    #   绿色的也是槽啊」—— 只按轴向判会把**凸台**报成孔（坑表 §103）。
    hb = (classify_holes_and_bosses(sw, faces) if sw is not None
          else {"holes": [], "bosses": [], "unknown": [],
                "note": "没传 `sw` ⇒ 跳过圆柱面凹凸判据"
                        "（判据必须读 `ISurface.Evaluate` 的真面法线）"})
    old_holes = holes_with_normal_holes(faces, fams)
    old_ids = {h["id"] for h in old_holes if h["is_normal_hole"]}
    new_hole_ids = {h["id"] for h in hb["holes"]}

    if hb["holes"] or hb["bosses"] or hb["unknown"]:
        print(f"\n== 圆柱面清单（孔 {len(hb['holes'])} / 凸台 {len(hb['bosses'])}"
              f" / 读不出 {len(hb['unknown'])}；**判据 = 曲率 n·r**）==")
        for h in hb["holes"]:
            print("  【孔】   %-9s Ø%-8.3f 长%-8s"
                  % (h["id"], h["diameter_mm"], h["length_mm"]))
            print("          %s" % h["why"])
        for b in hb["bosses"]:
            print("  【凸台】 %-9s Ø%-8.3f 长%-8s  ★ **不是孔**"
                  % (b["id"], b["diameter_mm"], b["length_mm"]))
            print("          %s" % b["why"])
        for u in hb["unknown"]:
            print("  【?】    %-9s Ø%-8.3f  %s"
                  % (u["id"], u["diameter_mm"], u["why"]))
        if hb["note"]:
            print("  ★ %s" % hb["note"])
        # ---- 旧判据（只按轴向）作为对照，把误判显式点出来 ----
        wrong = sorted(old_ids - new_hole_ids)
        if wrong:
            print("  ⚠ **只按「孔轴 ⊥ 承载面」判会把 %s 误报成法向孔** ——"
                  % (", ".join(wrong)))
            print("     凸台同样满足「轴垂直基面」，**必须读凹凸性**（坑表 §103）")
    else:
        print("\n  ★★ **本件一个圆柱面都没有** —— 若图纸上有孔/圆角/倒角/"
              "圆柱凸台，")
        print("     说明**特征没建全**（真机教训：agent 建的 CSWA 漏了 2 个"
              "Ø6 特征，见坑表 §102）")

    # ---- 槽（平面拓扑的「凹」；圆柱曲率判不出来）----
    sl = classify_slots(faces, fams)
    if sl["slots"]:
        print(f"\n== 槽清单（{len(sl['slots'])} 个；判据 = **同族内两片"
              "面积相等 + 包围盒重合的平行平面**）==")
        for r in sl["slots"]:
            print("  【槽】 深%-9.3fmm  %s / %s  %.1f×%.1fmm  法向%s"
                  % (r["depth_mm"], r["wall_a"], r["wall_b"],
                     r["length_mm"], r["width_mm"], r["normal"]))
            print("          %s" % r["why"])
    else:
        print("\n  · 没识别出槽。%s" % sl["note"])

    print(f"\n== 棱柱判定（沿每个平面族法向试一遍，按侧壁数降序）==")
    for v in verdicts[:6]:
        a = v["axis"]
        flag = {"prism": "★★ 棱柱", "weak": "· 侧壁够多(证据不足)",
                "not_prism": "  不是"}[v["verdict"]]
        L = "长度=?" if v["length_mm"] is None else f"长度={v['length_mm']:.3f}mm"
        vol = ""
        if v.get("volume_mm3"):
            chk = v.get("prism_volume_err_pct")
            vol = (f"  体积={v['volume_mm3']:.0f}mm³ 截面积={v['cap_area_mm2']:.1f}mm²"
                   + (f"  棱柱理论值偏差={chk:.3f}%" if chk is not None else ""))
        print(f"  {flag} 轴=({a[0]:+.3f},{a[1]:+.3f},{a[2]:+.3f})  "
              f"盖板={v['n_caps']} 侧壁={v['n_sides']}  {L}{vol}")
    hard = [v for v in verdicts if v["verdict"] == "prism"]
    if hard:
        v = min(hard, key=lambda r: r["prism_volume_err_pct"])
        print(f"\n★★ 判定为棱柱的方向（体积判据通过）："
              f"({v['axis'][0]:+.4f}, {v['axis'][1]:+.4f}, {v['axis'][2]:+.4f})"
              f"，{v['n_sides']} 个侧壁、{v['n_caps']} 片盖板、"
              f"长 {v['length_mm']}mm、理论体积偏差 {v['prism_volume_err_pct']:.3f}%")
        print("   → 下一步：`python scripts/sw_prism.py --doc 同上` 抽截面轮廓")
    else:
        weak = [v for v in verdicts if v["verdict"] == "weak"]
        print("\n★ 没有一个方向通过**体积判据** ⇒ 这不是单件棱柱。")
        if weak:
            print(f"   最接近的方向（侧壁最多）：({weak[0]['axis'][0]:+.3f}, "
                  f"{weak[0]['axis'][1]:+.3f}, {weak[0]['axis'][2]:+.3f})"
                  f"，{weak[0]['n_sides']} 侧壁 / {weak[0]['n_caps']} 盖板 —— "
                  f"侧壁数够但体积对不上 ⇒ 截面沿长度**是变的**"
                  f"（= 多段棱柱 / 带孔 / 带斜面），需要按段拆。")
        print("   → 用 `--json` 拿平面族与站位表，再按族逐段拆成多个棱柱。")


# ================================================================ 自检

def self_test() -> int:
    """不打开 SolidWorks，验证纯函数层（形状由手算的坐标构造）。"""
    ok = fail = 0
    fs_unit, fs_dot = _unit, _dot

    def chk(cond: bool, msg: str) -> None:
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS %s" % msg)
        else:
            fail += 1
            print("  FAIL %s" % msg)

    # ================= ★★★ 槽（slot）判据 =================
    # 起因（2026-10-03，用户校准 + 截图）：「绿色的也是槽啊」。
    # 真机 CSWA：Face6/Face8 同族、面积都是 1008.0mm²、bbox 都是
    # 32.6×28.0mm、站位差 5.000mm ⇒ 槽深 5mm。Face10 单独一片（无配对）
    # ⇒ 斜筋外表面，不是槽。
    print("[9] ★★ 槽（成对平行等面积平面）")
    box = [-0.03, -0.03, 0.0, 0.03, 0.03, 0.01]

    def _PLF(n, area, name, c, bx):
        nn = fs_unit(n)
        return {"id": name, "type": "plane", "area": area * 1e-6,
                "center": list(c), "normal": nn, "d": fs_dot(nn, c),
                "box": list(bx)}

    # 槽：法向 (0.9063,-0.4226,0)，两片站位差 5mm，面积/bbox 全同
    sn = [0.9063077870366485, -0.4226182617407028, 0.0]
    # ★★ 直接照**真机 CSWA 的绝对读数**建夹具（不再自己造几何）——
    #   第一版自己造 bbox 时把两片在 X/Y 上错开了 ⇒ `_same_shape` 判
    #   「包围盒不重合」⇒ 连槽都认不出来。**夹具必须复制真机数据。**
    #   真机 `D://Desktop//零件CSWA.SLDPRT`：
    #   Face6  站位 41.883  bbox [55.04,18.92,0, 70.25,51.55,28]  面积 1008.0
    #   Face8  站位 36.883  bbox [50.50,21.03,0, 65.72,53.66,28]  面积 1008.0
    #   ⇒ 面积逐位相同、bbox 两维都是 15.21×32.63、站位差 5.000mm
    _B6 = [0.05504, 0.01892, 0.0, 0.07025, 0.05155, 0.028]
    _B8 = [0.05050, 0.02103, 0.0, 0.06572, 0.05366, 0.028]
    _B10 = [0.02788, 0.008, 0.0, 0.05212, 0.060, 0.028]        # 斜筋外表面
    slot_a = _PLF(sn, 1008.0, "Face6",
                  [(_B6[i] + _B6[i + 3]) / 2 for i in range(3)], _B6)
    slot_b = _PLF(sn, 1008.0, "Face8",
                  [(_B8[i] + _B8[i + 3]) / 2 for i in range(3)], _B8)
    skin = _PLF(sn, 1090.1, "Face10",
                [(_B10[i] + _B10[i + 3]) / 2 for i in range(3)], _B10)
    s_faces = [slot_a, slot_b, skin]
    s_fams = build_families(s_faces)
    sl = classify_slots(s_faces, s_fams)
    chk(len(sl["slots"]) == 1,
        "★★ 同族内**两片面积相等 + bbox 重合**的平行平面 ⇒ 认成 1 个槽"
        "（实得 %d 个）" % len(sl["slots"]))
    if sl["slots"]:
        chk(abs(sl["slots"][0]["depth_mm"] - 5.0) < 0.05,
            "★★ 槽深读出 **5.000mm**（真机 CSWA 值，实得 %.3f）"
            % sl["slots"][0]["depth_mm"])
        chk({sl["slots"][0]["wall_a"], sl["slots"][0]["wall_b"]}
            == {"Face6", "Face8"},
            "★ 槽壁认成 Face6/Face8（实得 %s/%s）"
            % (sl["slots"][0]["wall_a"], sl["slots"][0]["wall_b"]))
        chk("Face10" not in (sl["slots"][0]["wall_a"],
                             sl["slots"][0]["wall_b"]),
            "★★ **没有配对的斜筋外表面 Face10 不得被当成槽壁**"
            "（真机：它是单独一片）")

    # ★ 反例 1：两片**面积差很多** ⇒ 不是槽（相对的两块大平板）
    big_a = _PLF([1, 0, 0], 3000.0, "A1", [0.02, 0, 0.005],
                 [0.02, -0.03, 0.0, 0.02, 0.03, 0.01])
    big_b = _PLF([1, 0, 0], 1500.0, "A2", [0.015, 0, 0.005],
                 [0.015, -0.02, 0.0, 0.015, 0.02, 0.01])
    sl2 = classify_slots([big_a, big_b], build_families([big_a, big_b]))
    chk(len(sl2["slots"]) == 0,
        "★★ 两片**面积差 2 倍** ⇒ **不是槽**（实得 %d 个）—— "
        "第一版判据只查「成对平行 + 深度」⇒ 报 16 个假阳性"
        % len(sl2["slots"]))

    # ★ 反例 2：两片**bbox 大小不同** ⇒ 台阶的两级面，不是槽
    st_a = _PLF([1, 0, 0], 1000.0, "S1", [0.02, 0, 0.005],
                [0.02, -0.03, 0.0, 0.02, 0.03, 0.01])
    st_b = _PLF([1, 0, 0], 1000.0, "S2", [0.015, 0, 0.005],
                [0.015, -0.01, 0.0, 0.015, 0.01, 0.01])
    sl3 = classify_slots([st_a, st_b], build_families([st_a, st_b]))
    chk(len(sl3["slots"]) == 0,
        "★★ 两片**包围盒大小不同**（台阶面）⇒ **不是槽**（实得 %d 个）"
        % len(sl3["slots"]))

    # ★ 反例 3：站位相同（重复面）⇒ 深度 0，不是槽
    dup = classify_slots([slot_a, dict(slot_a, id="Face6b")],
                         build_families([slot_a, dict(slot_a, id="Face6b")]))
    chk(len(dup["slots"]) == 0,
        "★★ 站位相同 ⇒ 深度 0 ⇒ **不是槽**（实得 %d 个）"
        % len(dup["slots"]))

    # ★ 「没找到槽」必须明说「不等于没有」
    chk("不等于" in sl3["note"] or "没找到" in sl3["note"],
        "★★ 报「没找到槽」时必须说「**不等于没有槽**」（要靠证据下否定结论）")

    # ================= ★★★ 孔 vs 凸台：曲率判据的离线部分 =================
    print("[10] ★★★ 孔 vs 凸台（曲率 n·r）")
    # `face_convexity` 需要 COM 引用 ⇒ 离线只测「拿不到引用时**不硬判**」
    fake = {"id": "X", "type": "cylinder", "area": 1e-4, "center": [0, 0, 0],
            "axis": [0, 0, 1], "radius": 0.003, "cyl_origin": [0, 0, 0],
            "box": None, "normal": None, "d": None}
    cv = face_convexity(None, fake)
    chk(cv["kind"] == "unknown",
        "★★ 没有 COM 引用时**如实报 unknown**，不硬判孔/凸台"
        "（实得 %r）" % cv["kind"])
    # 平面必须报 flat（平面判不出凹凸 —— 不硬套）
    pf = {"id": "P", "type": "plane", "area": 1e-3, "center": [0, 0, 0],
          "normal": [0, 0, 1], "d": 0.0, "box": box, "axis": None,
          "radius": None, "cyl_origin": None}
    chk(face_convexity(None, pf)["kind"] == "flat",
        "★ 平面报 `flat`（**平面判不出凹凸**，槽要靠 `classify_slots`）")
    # 退化圆柱（缺 radius）⇒ unknown
    deg = dict(fake)
    deg.pop("radius")
    chk(face_convexity(None, deg)["kind"] == "unknown",
        "★ 缺 radius 的退化圆柱报 unknown（不猜）")

    def chk(cond, label):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  PASS {label}")
        else:
            fail += 1
            print(f"  FAIL {label}")

    def P(n, d, area=1.0, name="FaceN"):
        """`area` 单位 **mm²**（内部换算成 m²）—— 写测试时最容易差 1e6。"""
        return {"id": name, "type": "plane", "area": area * 1e-6, "center": None,
                "normal": list(n), "d": d, "box": None}

    print("== sw_faceset 纯函数自检 ==")
    # 一个 40×30×10 的盒子（单位 m），六个面
    bx = [P([1, 0, 0], 0.02, 300.0, "F1"), P([1, 0, 0], -0.02, 300.0, "F2"),
          P([0, 1, 0], 0.015, 400.0, "F3"), P([0, 1, 0], -0.015, 400.0, "F4"),
          P([0, 0, 1], 0.005, 1200.0, "F5"), P([0, 0, 1], -0.005, 1200.0, "F6")]
    fams = build_families(bx)
    chk(len(fams) == 3, f"盒子的 6 个面聚成 3 个平面族（实得 {len(fams)}）")
    thick = sorted(t for f in fams for t in f["thicknesses_mm"])
    chk(thick == [10.0, 30.0, 40.0],
        f"★ 三个方向的厚度从**族内站位差**读出 = 10/30/40mm（实得 {thick}）"
        f" —— 按\"共面\"聚类会把板的两面拆成两族而丢掉厚度；按\"轴+站位都同\"聚"
        f"会并成一族只剩一个 d，同样丢厚度")
    v = prism_verdicts(bx, fams)
    chk(all(x["verdict"] == "weak" for x in v[:3]),
        "无体积数据时盒子沿三个轴都只是\"侧壁够多(证据不足)\"（**不给肯定**）")
    chk(not any(x["is_prism"] for x in v),
        "★★ 没有体积数据**绝不**给 is_prism=True —— 宁可说证据不足")
    v_vol = prism_verdicts(bx, fams, volume_mm3=40.0 * 30.0 * 10.0)
    chk(all(x["verdict"] == "prism" for x in v_vol[:3]),
        f"★ 给了正确体积（12000mm³）后三个轴全判成棱柱（偏差 "
        f"{[x['prism_volume_err_pct'] for x in v_vol[:3]]}）")
    lens = sorted({x["length_mm"] for x in v if x["length_mm"]})
    chk(lens == [10.0, 30.0, 40.0], f"挤出长度读出 10/30/40（实得 {lens}）")

    # 斜面必须**不能**被聚进竖直族（65° 支架那条筋）
    sl = P([math.cos(math.radians(65)), math.sin(math.radians(65)), 0], 0.01,
           500.0, "S1")
    fams2 = build_families(bx + [sl])
    chk(len(fams2) == 4, f"加 1 张 65° 斜面 ⇒ 4 族（实得 {len(fams2)}）")
    v2 = prism_verdicts(bx + [sl], fams2)
    axes = [tuple(round(x, 3) for x in t["axis"]) for t in v2]
    chk(any(abs(a[0] - 0.4226) < 1e-2 and abs(a[1] - 0.9063) < 1e-2
            for a in axes), f"65° 斜面法向也进了候选轴（实得 {axes[:4]}）")

    # 非平面不得进族
    cyl = {"id": "C1", "type": "cylinder", "area": 500e-6, "center": None,
           "normal": None, "d": None, "box": None, "radius": 0.005,
           "axis": [0, 0, 1]}
    fams3 = build_families(bx + [cyl])
    chk(len(fams3) == 3, f"圆柱面不进平面族（实得 {len(fams3)}）")
    chk(prism_verdicts(bx + [cyl], fams3)[0]["n_curved_excluded"] == 1,
       "★ 曲面**单独计数**（`n_curved_excluded`）而不是被悄悄丢掉 —— "
       "丢了就以为件是纯多面体")

    # 同族判据：法向相反 + 站位相反 ⇒ 同族（最容易写错的一处）
    chk(same_family(([1, 0, 0], 0.02), ([-1, 0, 0], -0.02)),
        "★ `same_family` 认 `n` 与 `-n`、且 `d` 同时取反时为同族"
        "（实体两侧那对平行面 ⇒ 写错会算不出板厚）")
    chk(not same_family(([1, 0, 0], 0.02), ([0, 1, 0], 0.02)),
        "不同法向不同族")
    chk(not same_family(([1, 0, 0], 0.02), ([1, 0, 0], 0.030)),
        f"同法向但站位差 10mm ⇒ 不同族（容差 {FAMILY_DIST_TOL_M * 1000}mm）")

    # 签名：面积必须参与
    s1 = signature(build_families(bx), bx)
    thick2 = list(bx)
    thick2[0] = P([1, 0, 0], 0.02, 310.0, "F1")
    s2 = signature(build_families(thick2), thick2)
    chk(s1 != s2, f"★★ 签名**含面积** ⇒ 厚 5 与厚 6 的板不会被判成同一个形状"
                  f"（签名实得 {s1[:60]}... vs {s2[:60]}...）")
    chk(s1 == signature(build_families(list(reversed(bx))), list(reversed(bx))),
        "★ 签名与**面的列出顺序无关**（否则对账会把同一形状判成不同）")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="平面型零件（多面体/棱柱）的面集取证")
    ap.add_argument("--doc", help="参照件 .SLDPRT 路径")
    ap.add_argument("--json", help="把结果写成 JSON")
    ap.add_argument("--axis", type=int, help="只看这一个平面族（1-based，同 --json 的顺序）")
    ap.add_argument("--compare", help="与另一份零件**对账**（体积 + 包围盒 + 面集签名）")
    ap.add_argument("--self-test", action="store_true",
                    help="不打开 SolidWorks，只验纯函数层")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.doc:
        ap.print_help()
        return 2

    import sw_connect                                          # noqa: PLC0415
    sw = sw_connect.SwSession.open(visible=False)
    sw.open_doc(a.doc, read_only=True)
    faces = read_faces(sw)
    vol = None
    with contextlib.suppress(Exception):
        mp = sw.mass_props()
        # ★ `mass_props()` 的键是 `volume_m3`（不是 volume）—— 而且它**绝不把
        #   "读不到"当 0**（读不到会带 `suspect` 键），所以这里必须判 `suspect`。
        vol = None
        if mp.get("volume_m3") and not mp.get("suspect"):
            vol = float(mp["volume_m3"]) * 1e9      # m³ → mm³
    fams = build_families(faces)
    verdicts = prism_verdicts(faces, fams, volume_mm3=vol)
    sig = signature(fams, faces)

    if a.axis:
        v = verdicts[a.axis - 1]
        print(f"沿族 #{a.axis} 轴 ({v['axis']}) 侧壁 {v['n_sides']} 盖板 "
              f"{v['n_caps']} 长度 {v['length_mm']}")
        print(f"侧壁面：{v['side_face_ids']}")
    else:
        print_report(faces, fams, verdicts, sig, sw=sw)

    if a.compare:
        pa = {"faces": faces, "families": fams, "volume_mm3": vol}
        # ★ 参照件只能开**只读**，且要**逐个关**，否则它会一直占着文档
        with contextlib.suppress(Exception):
            sw.close_doc(save=False)
        sw.open_doc(a.compare, read_only=True)
        fb = read_faces(sw)
        fb_fams = build_families(fb)
        volb = None
        with contextlib.suppress(Exception):
            mp = sw.mass_props()
            if mp.get("volume_m3") and not mp.get("suspect"):
                volb = float(mp["volume_m3"]) * 1e9
        pb = {"faces": fb, "families": fb_fams, "volume_mm3": volb}
        rep = compare(pa, pb)
        print(f"\n== 对账 ==")
        print(f"  参照件 {a.doc}\n  产物   {a.compare}")
        for c in rep["checks"]:
            print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}  {c['detail']}")
        for t in rep.get("info") or []:
            print(f"  [信息] {t}")
        print(f"  ⇒ {'**形状一致**' if rep['same_shape'] else '**不一致**'}")
        if a.json:
            Path(a.json).write_text(json.dumps(
                {"compare": rep, "a": {k: v for k, v in pa.items() if k != "faces"},
                 "b": {k: v for k, v in pb.items() if k != "faces"}},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  对账详情已写入 {a.json}")
        return 0 if rep["same_shape"] else 1

    if a.json:
        out = {"doc": a.doc, "summary": summarise(faces), "faces": faces,
               "volume_mm3": vol, "families": fams, "prism_verdicts": verdicts,
               "signature": sig}
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"\n已写入 {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
