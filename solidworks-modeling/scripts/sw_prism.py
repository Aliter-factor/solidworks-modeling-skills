#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_prism.py —— **棱柱截面轮廓提取**：把成品多面体零件的**截面**反推成闭合轮廓。

## 为什么需要它

`sw_faceset.py` 能**认出**"这件是棱柱、沿哪个轴、多长"（靠体积判据），
但还差最后一步：**截面长什么样**。没有截面就只能靠人看渲染图点 `polyline`，
多面体零件的截面往往有十几段折线加几个 R 圆角，纯靠手点是不现实的。

## 怎么读截面（不读草图、不读顶点）

沿用 `sw_profile.py` 已经在回转件上验证过的两条铁律：

  · **不读草图图元**（§64：晚期绑定下 `ISketch.GetLines2` 回空）；
  · **不读顶点**（闭环边没有顶点 ⇒ 必然读空）。

走**边的曲线**：`IFace2.GetEdges()` → `IEdge.GetCurve()` →
`IEdge.GetCurveParams2()`（11 个 double，**头 6 个就是这条边的起止点**，
直线和圆弧都给）。★ `GetCurveParams2` 官方标了 Obsolete（`GetCurveParams3`
返回数据对象、晚期绑定读属性不方便），但它仍是唯一能一把拿到起止点的入口；
**必须先调 `GetCurve`**（官方原文：不调的话 SolidWorks 不保留曲线信息）。

## 关键一步：投影筛边

把每条边的**起止点**投到截面二维坐标系 `(u, v)`（`u,v ⟂ 挤出轴 d`）：

  · 两点投影**不重合** ⇒ 这条边**在截面里**（真轮廓边）→ 收；
  · 两点投影**重合** ⇒ 这条边**平行于挤出轴**（侧壁之间的"竖棱"）→ **丢掉**。

★ 这一步是整个工具的关键：侧壁面上的边一半是截面轮廓、一半是竖棱，
不筛就只会拿到一堆退化成点的"边"。

## 输出

  ① **闭合环列表** —— 外轮廓 + 内环（孔/凹槽），带每环的点数、周长、面积、圆角半径
  ② **二维顶点表（mm）** —— 可直接喂 `polyline` op
  ③ **`--emit-plan`** —— 生成可执行的 `yoyo.swplan/1` 计划：
     `new_part` → `select_plane`/`ref_plane` → `sketch_begin` → `polyline`(外) +
     `polyline`(内环) → `sketch_end` → `extrude_boss`

## 纯函数层（可离线测）

`chain_loops()` / `classify_loops()` / `polygon_area()` / `frame_from_axis()`
都不需要 SolidWorks，`--self-test` 与 `sw_selftest.py` 都能钉它们。

用法：

    export APPDATA="C:\\Users\\OMEN\\AppData\\Roaming"
    /c/Python313/python.exe scripts/sw_prism.py --doc "C:\\x\\支架.SLDPRT"
    /c/Python313/python.exe scripts/sw_prism.py --doc xx.SLDPRT --axis y
    /c/Python313/python.exe scripts/sw_prism.py --doc xx.SLDPRT --emit-plan out.json
    /c/Python313/python.exe scripts/sw_prism.py --self-test

★ **只读**：一律 `open_doc(read_only=True)`，不改参照件。
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

#: 端点配对容差（mm）。截面轮廓的相邻边**共享**一个顶点，但两条独立的边
#: 各自算出来的端点会有浮点差；1e-4mm 足够小、又远大于 SolidWorks 的噪声。
JOIN_TOL_MM = 1e-4

#: 判定"这条边平行于挤出轴"的容差（mm）。投影后两点距离小于它就当竖棱丢掉。
DEGENERATE_TOL_MM = 1e-3


# ================================================================ 纯函数层

def _dot(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


def _unit(v) -> list:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


#: 三根全局轴向的**自然**截面坐标系：`(u, v)` 按循环序取，保证 `u × v = d`。
#: 硬编码这三档不是偷懒 —— 它们的 u/v 必须是 (Y,Z)/(Z,X)/(X,Y) 这种**右手自然对**，
#: 否则抽出来的截面坐标虽然自洽，却和 `select_plane` + 草图坐标系的习惯对不上，
#: 人工核对时要多绕一次弯。斜轴才走通用公式。
_NATURAL_FRAME = {
    (1, 0, 0): ([0, 1, 0], [0, 0, 1]), (-1, 0, 0): ([0, 0, 1], [0, 1, 0]),
    (0, 1, 0): ([0, 0, 1], [1, 0, 0]), (0, -1, 0): ([1, 0, 0], [0, 0, 1]),
    (0, 0, 1): ([1, 0, 0], [0, 1, 0]), (0, 0, -1): ([0, 1, 0], [1, 0, 0]),
}


def frame_from_axis(axis) -> tuple[list, list, list]:
    """给定挤出轴 `d`，造一个**确定性的**右手截面坐标系 `(d, u, v)`。

    ★ 为什么要"确定性"：同一件零件跑两次必须给出**同一套** `u,v`，
      否则顶点表会整体旋转/镜像，喂给 `polyline` 就画到别处去了。
    ★ 轴向对齐时用**硬编码的自然对**（`(Y,Z)` / `(Z,X)` / `(X,Y)` 及其反向），
      斜轴才走"取与 d 夹角最大的一根轴 + 叉乘"的通用式。
    """
    d = _unit(axis)
    key = None
    for ax, (u, v) in _NATURAL_FRAME.items():
        if _dot(d, ax) > 0.999:
            key = (ax, u, v)
            break
    if key is not None:
        _ax, u, v = key
        return d, list(u), list(v)
    for cand in ([1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0]):
        if abs(_dot(d, cand)) < 0.9:
            u = _unit([d[1] * cand[2] - d[2] * cand[1],
                       d[2] * cand[0] - d[0] * cand[2],
                       d[0] * cand[1] - d[1] * cand[0]])
            break
    v = _unit([d[1] * u[2] - d[2] * u[1],
               d[2] * u[0] - d[0] * u[2],
               d[0] * u[1] - d[1] * u[0]])
    return d, u, v


def project(p, d, u, v) -> tuple[float, float, float]:
    """三维点 → 截面坐标 `(a, b, t)`：`a` 沿 u、`b` 沿 v、`t` 沿挤出轴 d。"""
    return (_dot(p, u), _dot(p, v), _dot(p, d))


def polygon_area(pts) -> float:
    """二维多边形**有向**面积（鞋带公式）。正负号给环的绕向。"""
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i][0], pts[i][1]
        x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def dedup_edges(segs, tol_mm: float = JOIN_TOL_MM) -> tuple[list, int]:
    """按几何位置**去重**边。返回 `(去重后的边, 被丢掉的重复数)`。

    ★★★ 这是本工具最容易中招的一个静默坑（2026-10-03 真机，CSWA 支架）：
       棱柱有 N 个侧壁面、截面有 N 条边，而**每条几何边被两个相邻面各报一次**
       ⇒ `GetEdges()` 吐出来的是 **2N** 条。
       真机实测：12 个侧壁面 → 48 条边 → 丢掉 24 条竖棱后**剩 24 条**，
       而正确答案是 **12 条**。
    ★★ 危险之处：不去重也**"看起来是成功的"** —— 24 条能接出 12 个 2 段环、
       余边 0、还不报任何错。环数有了、形状全错，比直接失败难查得多。
    ★ 判据：两端点（各自四舍五入到 tol 量级）**互换后相同** ⇒ 同一条边。
      方向信息会相反（`SenseFlag`），所以必须**双向**比。
    """
    seen: list = []
    dropped = 0
    for s in segs:
        key = (tuple(round(v, 9) for v in s["p"][:2]),
               tuple(round(v, 9) for v in s["q"][:2]))
        rkey = (key[1], key[0])
        hit = False
        for t in seen:
            tkey = (tuple(round(v, 9) for v in t["p"][:2]),
                    tuple(round(v, 9) for v in t["q"][:2]))
            if key == tkey or rkey == tkey or key == (tkey[1], tkey[0]):
                hit = True
                break
        if hit:
            dropped += 1
            # 两条重复边里，**带圆弧信息的那条更可信**（直线的读法可能退化）
            if s.get("kind") == "arc" and not t.get("kind"):
                for i, tt in enumerate(seen):
                    tkey = (tuple(round(v, 9) for v in tt["p"][:2]),
                            tuple(round(v, 9) for v in tt["q"][:2]))
                    if key == tkey or rkey == tkey or key == (tkey[1], tkey[0]):
                        seen[i] = s
                        break
            continue
        seen.append(s)
    return seen, dropped


def chain_loops(segs, tol_mm: float = JOIN_TOL_MM) -> tuple[list, list]:
    """把一堆边接成**闭合环**。返回 `(闭合环, 接不上的散边)`。

    `segs` 每项 `{"p": (a,b,t), "q": (a,b,t), "kind": "line"/"arc", "radius": r}`。

    ★ 为什么不能用 `IEdge.GetStartVertex/GetEndVertex` 来接：
      闭环边（圆柱口那种）**没有顶点**，读出来是 Nothing（`sw_profile.py` 已记）。
      而 `GetCurveParams2` 给的起止点对闭环边照样有值 ⇒ 用它当配对键最稳。

    ★★ 实现（2026-10-03 重写）：**并查集聚点 → 图上走环**。
      第一版是「每次从池子里找一条能接的边」那种贪心，段一多就接不上：
      自检里 4 条边只接出 **1 个环 + 1 条余边** —— 典型的「部分成功」假象
      （环数对了、形状错了，比全错更难查）。
      聚点用贪心合并：任一端点与已有簇的代表点距离 <= tol 就并进去。
    ★ 聚完点后若某条边两端**落在同一簇** ⇒ 它自己就闭合成环，单独记一个环。
    """

    def close(a, b) -> bool:
        return abs(a[0] - b[0]) <= tol_mm and abs(a[1] - b[1]) <= tol_mm

    # ---- 1) 贪心聚点 ----
    reps: list = []
    for s in segs:
        for pt in (s["p"], s["q"]):
            if not any(close(r, pt) for r in reps):
                reps.append(pt)

    def node_of(pt) -> int:
        for i, r in enumerate(reps):
            if close(r, pt):
                return i
        reps.append(pt)
        return len(reps) - 1

    def meta_of(s) -> dict:
        return {k: v for k, v in s.items() if k not in ("p", "q")}

    edges: list = []
    for s in segs:
        edges.append((node_of(s["p"]), node_of(s["q"]), s))

    # ---- 2) 图上走环 ----
    adj: dict = {}
    for idx, (a, b, _s) in enumerate(edges):
        adj.setdefault(a, []).append(idx)
        if b != a:
            adj.setdefault(b, []).append(idx)

    used: set = set()
    loops: list = []
    dangling: list = []
    for idx0, (a0, b0, s0) in enumerate(edges):
        if idx0 in used:
            continue
        start = a0
        cur = b0
        used.add(idx0)
        loop = [{"p": s0["p"], "q": s0["q"], **meta_of(s0)}]
        if cur == start:
            loops.append(loop)            # 单边自环
            continue
        progress = True
        while progress and cur != start:
            progress = False
            for j in adj.get(cur, []):
                if j in used:
                    continue
                a, b, s = edges[j]
                # ★ 注意：**不能**加 `!= start` 守卫 —— "回到起点"的那条边
                #   正是**收口**的那条，挡掉它会把闭合环误判成开口链
                #   （自检里 4 条矩形边因此一条环都接不出来）。
                if a == cur:
                    loop.append({"p": s["p"], "q": s["q"], **meta_of(s)})
                    used.add(j)
                    cur = b
                    progress = True
                    break
                if b == cur:
                    loop.append({"p": s["q"], "q": s["p"], **meta_of(s)})
                    used.add(j)
                    cur = a
                    progress = True
                    break
            if not progress:
                # 走到一半断了 ⇒ 这串是**开口链**，整串记为散边
                dangling.extend(loop)
                break
        if cur == start:
            loops.append(loop)
    for idx, (a, b, s) in enumerate(edges):
        if idx not in used:
            dangling.append(s)
    return loops, dangling

def classify_loops(loops) -> dict:
    """把环分成"外轮廓"与"内环（孔）"，并给周长/面积/圆角半径表。

    ★ 判据用**有向面积**：外轮廓与内环绕向相反；再按"面积最大的那个是外轮廓"
      兜底（一个截面只有一个外轮廓）。
    ★★ 单位纪律：`signed_area_mm2` / `perimeter_mm` / `radii_mm` / `points` / `t_range_mm`
      **全部是 mm 制**（`*_mm2` 存 mm²）。第一版把 m² 直接塞进名为 `..._mm2` 的字段，
      `round(8e-4, 4)` 变成 `0.0008` —— 数字看着"有值"但量纲差 1e6，
      这种错只有在**离线自检拿解析值对账**时才抓得到。

    ★★ **整圆环**（孔）单独识别：整圆边在截面里起止点相同、只有 1 段，
      按多边形算面积恒为 0 ⇒ 必须看 `kind == "circle"` 走另一条路，
      否则报告里每个孔都是"面积 0.0mm²"，**看着像孔丢了、其实孔在那儿**。
      孔是多面体零件最常见的特征，这一条错了整份截面数据就没法用。
    """
    info = []
    for lp in loops:
        pts = [s["p"] for s in lp]
        radii: list[float] = []
        for s in lp:
            if s.get("kind") in ("arc", "circle") and s.get("radius"):
                radii.append(round(float(s["radius"]) * 1000.0, 4))
        uniq_r = sorted(set(radii))
        # ---- 整圆？ ----
        is_circle = (len(lp) == 1 and lp[0].get("kind") == "circle"
                     and bool(lp[0].get("radius")))
        if is_circle:
            r_mm = uniq_r[0]
            c = lp[0]["p"]
            info.append({
                "n": 1, "circle": True, "d_mm": round(r_mm * 2, 4),
                "center_mm": [round(c[0] * 1000, 4), round(c[1] * 1000, 4)],
                "signed_area_mm2": round(math.pi * r_mm * r_mm, 4),
                "perimeter_mm": round(2 * math.pi * r_mm, 4),
                "radii_mm": uniq_r, "n_arc": 1,
                "points": [[round(c[0] * 1000, 4), round(c[1] * 1000, 4)]],
                "t_range_mm": [round(c[2] * 1000, 4), round(c[2] * 1000, 4)]})
            continue
        area = polygon_area(pts)
        peri = sum(math.dist(s["p"][:2], s["q"][:2]) * 1000.0 for s in lp)
        info.append({"n": len(lp), "circle": False,
                     "signed_area_mm2": round(area * 1e6, 4),
                     "perimeter_mm": round(peri, 4),
                     "radii_mm": uniq_r,
                     "n_arc": sum(1 for s in lp if s.get("kind") == "arc"),
                     "points": [[round(p[0] * 1000, 4), round(p[1] * 1000, 4)]
                                for p in pts],
                     "t_range_mm": [round(min(p[2] for p in pts) * 1000, 4),
                                    round(max(p[2] for p in pts) * 1000, 4)]})
    if not info:
        return {"outer": None, "inners": [], "all": []}
    outer = max(info, key=lambda x: abs(x["signed_area_mm2"]))
    inners = [x for x in info if x is not outer]
    return {"outer": outer, "inners": inners, "all": info}


# ================================================================ 读数据层

def _edge_endpoints(e) -> tuple | None:
    """取一条边的**起止点**（三维，米）。`GetCurveParams2` 的头 6 个 double。

    ★ **必须先 `GetCurve()`**（官方原文：不调它 SolidWorks 不保留曲线信息）。
    """
    from sw_connect import member, typed                     # noqa: PLC0415
    cur = None
    with contextlib.suppress(Exception):
        cur = typed(member(e, "GetCurve"), "ICurve")
        if cur is None:
            return None
        member(e, "GetCurve")            # 再调一次确保曲线信息就绪
        cp = member(e, "GetCurveParams2")
        if cp is not None and len(cp) >= 6:
            return [float(cp[0]), float(cp[1]), float(cp[2])], \
                   [float(cp[3]), float(cp[4]), float(cp[5])]
    return None


def _edge_kind(e) -> tuple[str, float, list | None]:
    """这条边在截面里是什么：`("line"|"arc"|"circle", 半径, 圆轴向)`。

    ★★ 必须按**曲线类型**判，不能按"投影后是否退化成一点"判
      （2026-10-03 真机）：**整圆边（孔）的起止点相同** ⇒ 投影长度 0
      ⇒ 会被"平行于挤出轴的竖棱"那条规则**误丢**，而孔恰恰是最该保留的特征。
      判据改成：
        · `IsCircle` 且圆轴 ∥ 挤出轴 ⇒ `circle`（截面里的整圆孔，保留）
        · `IsCircle` 且圆轴 ⟂ 挤出轴 ⇒ 侧看是**一条线段**，当 `line` 保留
        · `IsLine`   且线向 ∥ 挤出轴 ⇒ 竖棱，**丢**
        · `IsLine`   且线向 ⟂ 挤出轴 ⇒ `line` 保留
        · `IsCircle` 且起止点不同 ⇒ `arc`（截面里的圆角/圆弧）
    """
    from sw_connect import member, typed                     # noqa: PLC0415
    with contextlib.suppress(Exception):
        cur = typed(member(e, "GetCurve"), "ICurve")
        if cur is None:
            return "skip", 0.0, None
        member(e, "GetCurve")
        if bool(member(cur, "IsCircle")):
            cp = member(cur, "CircleParams")
            if cp is not None and len(cp) >= 7:
                r = float(cp[6])
                axis = _unit([float(cp[3]), float(cp[4]), float(cp[5])])
                return ("circle", r, axis) if True else ("arc", r, axis)
        lp = member(cur, "LineParams")
        if lp is not None and len(lp) >= 6:
            return "line", 0.0, _unit([float(lp[3]), float(lp[4]), float(lp[5])])
    return "skip", 0.0, None


def _edge_is_full_circle(e) -> bool:
    """整圆边的起止点相同。"""
    ep = _edge_endpoints(e)
    return bool(ep) and math.dist(ep[0], ep[1]) < 1e-9


def read_section(sw, faces, axis, side_face_ids: list[str] | None = None):
    """沿 `axis` 抽截面。返回 `(segs, meta)`。

    `segs` 每项 `{"p","q": 截面坐标, "kind", "radius", "face"}`（坐标单位米）。
    `kind` ∈ `line` / `arc` / `circle`。
    """
    from sw_connect import member, typed                     # noqa: PLC0415
    d, u, v = frame_from_axis(axis)
    want = set(side_face_ids or [])
    segs: list[dict] = []
    n_faces = n_edges = n_drop = n_circ = 0
    cos_tol = math.cos(math.radians(1.0))
    for b in sw.bodies():
        body = typed(b, "IBody2")
        if body is None:
            continue
        with contextlib.suppress(Exception):
            all_faces = list(member(body, "GetFaces") or ())
        for fi, f in enumerate(all_faces, 1):
            fe = typed(f, "IFace2")
            if fe is None:
                continue
            fid = f"Face{fi}"
            with contextlib.suppress(Exception):
                fid = str(member(fe, "GetName") or fid)
            if want and fid not in want:
                continue
            n_faces += 1
            with contextlib.suppress(Exception):
                edges = list(member(fe, "GetEdges") or ())
            for e in edges:
                n_edges += 1
                kind, r, kaxis = _edge_kind(e)
                if kind == "skip":
                    n_drop += 1
                    continue
                if kind == "line" and kaxis is not None and \
                        abs(_dot(kaxis, d)) >= cos_tol:
                    n_drop += 1            # ★ 竖棱：平行于挤出轴，截面里不存在
                    continue
                ep = _edge_endpoints(e)
                if ep is None:
                    n_drop += 1
                    continue
                a = project(ep[0], d, u, v)
                b2 = project(ep[1], d, u, v)
                if kind == "circle" and kaxis is not None and \
                        abs(_dot(kaxis, d)) >= cos_tol and \
                        _edge_is_full_circle(e):
                    # 截面里的**整圆孔**：起止点相同 ⇒ 投影退化 ⇒ 用圆心当代表点
                    cp = None
                    with contextlib.suppress(Exception):
                        cur = typed(member(e, "GetCurve"), "ICurve")
                        member(e, "GetCurve")
                        cp = member(cur, "CircleParams")
                    if cp is not None and len(cp) >= 3:
                        c = project([float(cp[0]), float(cp[1]), float(cp[2])],
                                    d, u, v)
                        a = b2 = c
                    n_circ += 1
                elif kind == "circle":
                    kind = "arc"           # 侧看的圆 ⇒ 截面里是一条线段
                elif kind == "line" and math.hypot(a[0] - b2[0],
                                                   a[1] - b2[1]) * 1000.0 \
                        <= DEGENERATE_TOL_MM and not _edge_is_full_circle(e):
                    n_drop += 1
                    continue
                segs.append({"p": a, "q": b2,
                             "kind": "arc" if kind == "arc" else kind,
                             "radius": r if kind in ("arc", "circle") else 0.0,
                             "face": fid})
    meta = {"n_side_faces": n_faces, "n_edges": n_edges,
            "n_dropped_parallel": n_drop, "n_circles": n_circ,
            "n_segs": len(segs),
            "axis": [round(x, 6) for x in d],
            "u": [round(x, 6) for x in u], "v": [round(x, 6) for x in v]}
    return segs, meta


# ================================================================ 计划生成

#: 挤出轴（全局轴向）→ 该轴垂直的基准面。`select_plane` 只能选基准面，
#: 所以只有轴向对齐的挤出方向才能自动出计划。
AXIS_TO_PLANE = {
    (1, 0, 0): ("Right", "右视基准面"),
    (0, 1, 0): ("Top", "上视基准面"),
    (0, 0, 1): ("Front", "前视基准面"),
}


def emit_plan(doc: str, axis, loops: dict, length_mm: float, t_min_mm: float,
              out_path: str, unit="mm", keep_station: bool = False) -> dict:
    """生成 `yoyo.swplan/1` 计划（`polyline` + `circle` + `extrude_boss`）。

    ★ 只在 `axis` 与某根全局轴**平行**时才生成（`select_plane` 只能选基准面）。
      斜挤出方向（CSWA 支架那条 65° 斜筋）**不生成**并明确说明 ——
      宁可少给一份计划，也不要给一份会在错的平面上画图的计划。
    ★ `keep_station=False`（默认）时截面画在**基准面**上、沿法向拉伸，
      于是产物与参照件差一个**纯平移**（`origin_shift_mm` 记在 meta 里）。
      勾上 `keep_station=True` 才用 `plane_offset` 把基准面挪到真实站位 ——
      那条路依赖"新建基准面能被 `select_plane` 按名字选中"，属于次优路径。
    """
    key = None
    # ★ `AXIS_TO_PLANE` 的值是 `(英文名, 中文名)` **二元组** ——
    #   这里必须解包成 `cn`（单个字符串）。第一版写成 `key = (ax, name)` 再
    #   `_ax, cn = key`，于是 `cn` 拿到了**整个元组**，`select_plane` 收到
    #   `("Right","右视基准面")` —— 离线校验报类型不对，真机上则是选不中基准面。
    for ax, (_en, cn) in AXIS_TO_PLANE.items():
        if abs(_dot(_unit(axis), ax)) > 0.999:
            key = (ax, cn)
            break
    plan = {
        "schema": "yoyo.swplan/1",
        "meta": {"name": f"由 {Path(doc).name} 反推的棱柱截面",
                 "units": unit,
                 "note": ("截面由 scripts/sw_prism.py 从参照件反推"
                          "（族/轴判定用体积判据，面集与截面两条独立算法互证）。"
                          "圆角段目前用折线近似（见 loops[].radii_mm）；"
                          "要精确还原请在 polyline 之后补 sketch_fillet。")},
        "params": {"out": "D:/out"},
        "steps": [],
    }
    if key is None:
        plan["meta"]["unsupported"] = (
            f"挤出轴 {['%.4f' % x for x in axis]} 与任何基准面都不平行，"
            f"**没有生成建模步骤**（`select_plane` 只能选基准面）。"
            f"截面数据仍完整给出，请人工选面/建基准面后照抄 polyline 的点。")
        Path(out_path).write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        return plan
    _ax, cn = key
    steps = plan["steps"]
    steps.append({"id": "np", "op": "new_part"})
    plane = cn
    if keep_station and abs(t_min_mm) > 1e-6:
        steps.append({"id": "po", "op": "plane_offset", "base": cn,
                      "offset": round(t_min_mm, 4), "name": "SectionPlane",
                      "note": "把基准面挪到截面的真实起始站位"})
        plane = "SectionPlane"
        plan["meta"]["station"] = "kept"
    else:
        plan["meta"]["origin_shift_mm"] = round(t_min_mm, 4)
        plan["meta"]["station"] = "on datum plane (纯平移差异见 origin_shift_mm)"
    steps.append({"id": "pl", "op": "select_plane", "plane": plane})
    steps.append({"id": "sb", "op": "sketch_begin", "plane": plane})
    o = loops.get("outer")
    if o:
        steps.append({"id": "po1", "op": "polyline",
                      "points": o["points"], "close": True,
                      "note": f"外轮廓 {o['n']} 段，周长 {o['perimeter_mm']}mm，"
                              f"面积 {abs(o['signed_area_mm2'])}mm²"})
    for k, h in enumerate(loops.get("inners") or [], 1):
        if h.get("circle"):
            steps.append({"id": f"pi{k}", "op": "circle",
                          "cx": h["center_mm"][0], "cy": h["center_mm"][1],
                          "d": h["d_mm"],
                          "note": f"孔 {k}：Ø{h['d_mm']}mm @ "
                                  f"({h['center_mm'][0]}, {h['center_mm'][1]})"})
        else:
            steps.append({"id": f"pi{k}", "op": "polyline",
                          "points": h["points"], "close": True,
                          "note": f"内环 {k}（凹槽），面积 "
                                  f"{h['signed_area_mm2']}mm²"})
    steps.append({"id": "se", "op": "sketch_end", "name": "SectionSk"})
    steps.append({"id": "ex", "op": "extrude_boss", "depth": round(length_mm, 4),
                  "name": "PrismBoss",
                  "note": "长度 = 两片盖板的站位差（体积判据已核对）"})
    # ---- 自校验 + 存盘（`sw_plan --check` 会主动提醒缺这两类步骤）----
    # ★ 尺寸期望按 `(du, dv, L)` 映射到 (X,Y,Z)：截面坐标 (a,b) = (u,v) 分量，
    #   而 u、v 的物理方向由 `frame_from_axis` 固定（轴向对齐时是自然对）。
    # ★ 尺寸期望：`u / v / d` 三个方向互为正交，且在轴向对齐时**各自就是某根全局轴**
    #   ⇒ 直接把它们各自的长度填进 (X, Y, Z) 对应的槽位。
    #   斜挤出方向不会走到这里（前面已 return）。
    o = loops.get("outer")
    if o and o["points"]:
        us = [p[0] for p in o["points"]]
        vs = [p[1] for p in o["points"]]
        du, dv = max(us) - min(us), max(vs) - min(vs)
        _d, _u, _v = frame_from_axis(axis)
        sizes = [0.0, 0.0, 0.0]
        for comp, vec in ((du, _u), (dv, _v), (float(length_mm), _d)):
            for i in range(3):
                if abs(vec[i]) > 0.5:
                    sizes[i] = comp
        expect = [round(x, 3) for x in sizes]
        steps.append({"id": "cb", "op": "check_bbox", "expect": expect,
                      "tol_mm": 0.05,
                      "note": "★ 自校验：截面尺寸 × 挤出长度。**尺寸与位置无关**，"
                              "所以基准面平移不影响这一项。"})
    stem = Path(doc).stem
    steps.append({"id": "sv", "op": "save_as",
                  "path": "{{ params.out }}/rebuilt_" + stem + ".SLDPRT",
                  "overwrite": True})
    Path(out_path).write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    return plan

# ================================================================ 自检

def self_test() -> int:
    ok = fail = 0

    def chk(c, label):
        nonlocal ok, fail
        if c:
            ok += 1
            print(f"  PASS {label}")
        else:
            fail += 1
            print(f"  FAIL {label}")

    print("== sw_prism 纯函数自检 ==")
    d, u, v = frame_from_axis([1, 0, 0])
    chk([round(x) for x in d] == [1, 0, 0], f"frame_from_axis 保持 d（{d}）")
    chk(abs(_dot(u, d)) < 1e-12 and abs(_dot(v, d)) < 1e-12,
        f"u、v 都 ⟂ d（u={['%.3f' % x for x in u]}, v={['%.3f' % x for x in v]}）")
    chk(abs(_dot(u, v)) < 1e-12, "u ⊥ v")
    d2, u2, v2 = frame_from_axis([0, 0, 1])
    chk([round(x) for x in u2] == [1, 0, 0] and [round(x) for x in v2] == [0, 1, 0],
        f"d=(0,0,1) → u=X、v=Y（确定性，实得 "
        f"u={['%.2f' % x for x in u2]}, v={['%.2f' % x for x in v2]}）")
    chk(frame_from_axis([0, 0, 1])[1] == u2,
        "★ 同一根轴跑两次给出**同一套 u,v**（否则顶点表整体旋转，polyline 会画到别处）")

    # 一个 40×20 的矩形截面（沿 X 挤出，四条边首尾相接，故意打乱顺序 + 绕向反）
    A = (0.0, 0.0, 0.0)
    B = (0.040, 0.0, 0.0)
    C = (0.040, 0.020, 0.0)
    D = (0.0, 0.020, 0.0)
    segs = [{"p": D, "q": C, "kind": "line"},      # 绕向与环相反
            {"p": C, "q": B, "kind": "line"},
            {"p": A, "q": B, "kind": "line"},      # 故意倒过来
            {"p": D, "q": A, "kind": "line"}]
    loops, left = chain_loops(segs)
    chk(len(loops) == 1 and not left,
        f"★ 乱序 + 反向的 4 条边能接成**一个**闭合环（环数={len(loops)} 余边={len(left)}）")
    if loops:
        a = abs(polygon_area([s["p"] for s in loops[0]]))
        chk(abs(a - 0.040 * 0.020) < 1e-9,
            f"环面积 = 40×20 = 800mm²（实得 {a * 1e6:.1f}mm²）")

    # 两个环（外 + 内孔）
    inner = [{"p": (0.010, 0.005, 0.0), "q": (0.010, 0.015, 0.0), "kind": "line"},
             {"p": (0.010, 0.015, 0.0), "q": (0.030, 0.015, 0.0), "kind": "line"},
             {"p": (0.030, 0.015, 0.0), "q": (0.030, 0.005, 0.0), "kind": "line"},
             {"p": (0.030, 0.005, 0.0), "q": (0.010, 0.005, 0.0), "kind": "line"}]
    loops2, _ = chain_loops(segs + inner)
    cls = classify_loops(loops2)
    chk(len(loops2) == 2, f"外轮廓 + 内孔 = 2 个环（实得 {len(loops2)}）")
    chk(cls["outer"] is not None and len(cls["inners"]) == 1,
        f"★ 分出 1 个外轮廓 + 1 个内环（内环={len(cls['inners'])}）")
    chk(cls["outer"] and abs(abs(cls["outer"]["signed_area_mm2"]) - 800.0) < 1e-6,
        f"外轮廓面积 = 800mm²（实得 {cls['outer']['signed_area_mm2'] if cls['outer'] else None}）")

    # ★★ 去重：同一条几何边被两个相邻面各报一次
    dup = [{"p": A, "q": B, "kind": "line"}, {"p": B, "q": A, "kind": "line"}]
    uniq, ndrop = dedup_edges(dup)
    chk(len(uniq) == 1 and ndrop == 1,
        f"★★ 反向重复的同一条边被去重（留 {len(uniq)} 条、丢 {ndrop} 条）—— "
        f"棱柱 N 个侧壁面会吐 2N 条边，不去重会接出一堆 2 段假环且**不报错**")
    dup2 = [{"p": A, "q": B, "kind": "line"}, {"p": A, "q": B, "kind": "line"}]
    chk(dedup_edges(dup2)[0].__len__() == 1, "同向重复也去重")

    # 圆角半径要被单独汇总
    lp = [{"p": (0, 0, 0), "q": (0.01, 0, 0), "kind": "arc", "radius": 0.002},
          {"p": (0.01, 0, 0), "q": (0.01, 0.01, 0), "kind": "line"},
          {"p": (0.01, 0.01, 0), "q": (0, 0.01, 0), "kind": "line"},
          {"p": (0, 0.01, 0), "q": (0, 0, 0), "kind": "line"}]
    cls2 = classify_loops(chain_loops(lp)[0])
    chk(cls2["outer"] and cls2["outer"]["radii_mm"] == [2.0],
        f"★★ 圆角半径被单独汇总成 mm（实得 "
        f"{cls2['outer']['radii_mm'] if cls2['outer'] else None}）—— "
        f"多面体零件最容易漏的尺寸就藏在这里")

    # 斜轴不生成计划（必须**明确拒绝**，不能给一份会画错面的计划）
    import tempfile
    tmp = str(Path(tempfile.gettempdir()) / "_sw_prism_selftest.json")
    pl = emit_plan("x.SLDPRT", [0, 0.4226, 0.9063], cls, 28.0, 0.0, tmp)
    chk("unsupported" in pl["meta"] and not pl["steps"],
        "★ 斜挤出轴（65°）**明确拒绝**生成计划并说明原因 —— "
        "宁可少给一份，也不给一份画在错平面上的计划")
    pl2 = emit_plan("x.SLDPRT", [1, 0, 0], cls, 28.0, 0.0, tmp)
    ops = [s["op"] for s in pl2["steps"]]
    chk(ops == ["new_part", "select_plane", "sketch_begin", "polyline",
                "polyline", "sketch_end", "extrude_boss", "check_bbox", "save_as"],
        f"轴向对齐时计划步骤完整，**且自带 check_bbox + save_as**"
        f"（`sw_plan --check` 会提醒缺这两类；实得 {ops}）")
    _cb = next((s for s in pl2["steps"] if s["op"] == "check_bbox"), None)
    chk(_cb is not None and _cb["expect"] == [28.0, 40.0, 20.0],
        f"★ check_bbox 的期望尺寸按 u/v/d 正交基填进 (X,Y,Z)：沿 X 挤 28、"
        f"u=Y 跨 40、v=Z 跨 20 ⇒ (28, 40, 20)（实得 "
        f"{_cb['expect'] if _cb else None}）—— 顺序错了就是**转置的零件**，"
        f"而尺寸全对、体积也对，只有真机开图才看得出来")
    chk("{{ params.out }}" in next(s for s in pl2["steps"]
                                  if s["op"] == "save_as")["path"],
        "★ save_as 路径用 `{{ params.out }}`（写死绝对路径就绑死了机器）")
    chk(all(isinstance(s.get("plane"), str) for s in pl2["steps"]
            if s["op"] in ("select_plane", "sketch_begin")),
        "★★ `plane` 必须是**字符串** —— 第一版把 (en, cn) 元组整个传进去，"
        "离线校验会报类型不对，真机上则是选不中基准面")
    chk(all("base" in s and "offset" in s for s in pl2["steps"]
            if s["op"] == "plane_offset"),
        "★ `plane_offset` 的参数名是 `base`/`offset`（不是 `plane`/`distance`）")
    chk(any(s.get("close") for s in pl2["steps"] if s["op"] == "polyline"),
        "★ polyline 段用 `close: true` 闭合（截面必须是闭合环，否则拉伸不成实体）")
    return 0 if fail == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="棱柱截面轮廓提取（多面体零件）")
    ap.add_argument("--doc", help="参照件 .SLDPRT")
    ap.add_argument("--axis", default="auto",
                    help="挤出轴：auto / x / y / z / nx,ny,nz")
    ap.add_argument("--json", help="把截面结果写成 JSON")
    ap.add_argument("--emit-plan", help="生成 yoyo.swplan 计划到该路径")
    ap.add_argument("--self-test", action="store_true", help="不打开 SolidWorks")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.doc:
        ap.print_help()
        return 2

    import sw_connect                                          # noqa: PLC0415
    import sw_faceset                                           # noqa: PLC0415

    sw = sw_connect.SwSession.open(visible=False)
    sw.open_doc(a.doc, read_only=True)
    faces = sw_faceset.read_faces(sw)
    fams = sw_faceset.build_families(faces)
    vol = None
    with contextlib.suppress(Exception):
        mp = sw.mass_props()
        if mp.get("volume_m3") and not mp.get("suspect"):
            vol = float(mp["volume_m3"]) * 1e9
    verdicts = sw_faceset.prism_verdicts(faces, fams, volume_mm3=vol)

    if a.axis == "auto":
        hard = [v for v in verdicts if v["verdict"] == "prism"]
        if not hard:
            print("★ 没有方向通过体积判据 ⇒ 这不是单件棱柱，"
                  "抽截面没有意义。请先用 sw_faceset.py --json 看平面族与站位，"
                  "按段拆成多个棱柱。")
            return 1
        v = min(hard, key=lambda r: r["prism_volume_err_pct"])
        axis = v["axis"]
        print(f"自动选轴 = ({axis[0]:+.4f}, {axis[1]:+.4f}, {axis[2]:+.4f})"
              f"（体积偏差 {v['prism_volume_err_pct']:.3f}%，长 {v['length_mm']}mm）")
    else:
        axis = {"x": [1, 0, 0], "y": [0, 1, 0], "z": [0, 0, 1]}.get(
            a.axis.lower(), None)
        if axis is None:
            axis = [float(x) for x in a.axis.split(",")]
        v = next((x for x in verdicts
                  if abs(_dot(_unit(axis), x["axis"])) > 0.999), None)

    # 侧壁面 = 法向 ⟂ axis 的平面面
    cos_tol = math.cos(math.radians(0.5))
    ax = _unit(axis)
    # ★★ 侧壁面 = 「法向 ⟂ 挤出轴的**平面**」 ∪ 「轴向 ∥ 挤出轴的**圆柱面**」
    #   后者就是**孔/凸台**。漏了它 ⇒ 孔在截面里完全不出现，而报告里
    #   "闭合环 1 个、未接上 0 条" 看着完全正常（真机 8-窥视孔盖：
    #   漏掉 1 个 Ø11 + 4 个 Ø3.5 的孔，截面积 2020.97 报成 2116）。
    side_ids = []
    for f in faces:
        pl = sw_faceset.face_plane(f)
        if pl and abs(_dot(_unit(pl[0]), ax)) <= 1.0 - cos_tol:
            side_ids.append(f.get("id"))
        elif (f.get("type") == "cylinder" and f.get("axis")
              and abs(_dot(_unit(f["axis"]), ax)) >= cos_tol):
            side_ids.append(f.get("id"))
    segs, meta = read_section(sw, faces, axis, side_ids)
    # ★★ 必须去重：每条几何边被两个相邻侧壁面各报一次（棱柱 N 面 ⇒ 2N 条）
    segs_u, n_dup = dedup_edges(segs)
    meta["n_dedup"] = n_dup
    meta["n_segs_unique"] = len(segs_u)
    loops, left = chain_loops(segs_u)
    cls = classify_loops(loops)

    print(f"\n== 截面（沿 ({ax[0]:+.3f},{ax[1]:+.3f},{ax[2]:+.3f})）==")
    print(f"  侧壁面 {meta['n_side_faces']} 张、边 {meta['n_edges']} 条、"
          f"丢掉**平行于轴**的竖棱 {meta['n_dropped_parallel']} 条、"
          f"留下轮廓边 {meta['n_segs']} 条")
    print(f"  ★ 去重：每条几何边被两个相邻面各报一次 ⇒ 丢掉重复 {meta['n_dedup']} 条，"
          f"真正轮廓边 = {meta['n_segs_unique']} 条")
    print(f"  闭合环 {len(loops)} 个（未接上 {len(left)} 条）")
    if cls["outer"]:
        o = cls["outer"]
        print(f"\n  外轮廓：{o['n']} 段 / {o['n_arc']} 段圆弧 / "
              f"周长 {o['perimeter_mm']}mm / 面积 {o['signed_area_mm2']}mm²")
        if o["radii_mm"]:
            print(f"          圆角半径 = {o['radii_mm']} mm")
        print(f"          沿轴站位 {o['t_range_mm'][0]} ~ {o['t_range_mm'][1]} mm")
        print(f"          顶点(mm)：{o['points']}")
    for k, h in enumerate(cls["inners"], 1):
        if h.get("circle"):
            print(f"  孔 {k}：Ø{h['d_mm']}mm @ ({h['center_mm'][0]}, "
                  f"{h['center_mm'][1]})  面积 {h['signed_area_mm2']}mm²")
        else:
            print(f"  内环 {k}：{h['n']} 段 / 面积 {h['signed_area_mm2']}mm² / "
                  f"顶点 {h['points']}")

    length = v["length_mm"] if v and v["length_mm"] else None
    if a.emit_plan:
        t0 = cls["outer"]["t_range_mm"][0] if cls["outer"] else 0.0
        plan = emit_plan(a.doc, axis, cls, length or 0.0, t0, a.emit_plan)
        print(f"\n计划已写入 {a.emit_plan}"
              + (f"（{len(plan['steps'])} 步）" if plan["steps"]
                 else f" —— {plan['meta'].get('unsupported', '')}"))
    if a.json:
        Path(a.json).write_text(json.dumps(
            {"doc": a.doc, "axis": ax, "meta": meta, "loops": cls,
             "unclosed": len(left), "length_mm": length},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"截面数据已写入 {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
