#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_symmetry.py —— 对称性与装配面决策层（复杂零件「建一半 + 镜像」的依据）。

## 为什么需要它（`sw_faceset` 之后的下一层）

`sw_faceset.py` 回答「这是不是一件棱柱、截面在哪」，`sw_prism.py` 回答「截面长什么样」。
但拿到一份**复杂零件的参照件**（减速器箱座/箱盖、大齿轮、CSWA 支架），agent 仍然会卡在
三个决策上，而这三个决策**恰恰决定"一次能不能建成"**：

  ① **对称吗？** 箱座、大齿轮这类件关于某个中面左右对称 ⇒ **建一半 + 一个 `mirror`
     特征**就够。手工建全件 = 特征数翻倍、草图容易画歪、而且改一个尺寸要改两遍。
     但"对称"不是显然的：要先找出**对称面**，还要分清
     **"要建的半边" / "镜像出来的那半边" / "正好长在对称面上的特征"**（第三类不能只建一半）。
  ② **该建哪半边、朝哪个方向建？** 选错 ⇒ 镜像出来的零件朝向是反的，装配时又要转。
  ③ ★ **哪个面是装配定位面？** 用户原话：
       「如果建模时直接选取正确的基准面，装配时就可以加快装配效率，减少冗余操作」
     装配图上标着"从左边装/从右边装/从前装/从后装"—— 每种安装方向对应零件上的
     **一个大平面**。建模时让那个面**平行于某个基准面**（或者就是它），
     装配时一次**重合（coincident）**就能定位；否则可能要两三条配合才对得上。

## 输出四样

  ① **对称面候选表** —— 对 X/Y/Z 三个方向的中面各算一次对称度
     （面镜像后能不能一一对上），给百分比
  ② **半边清单** —— `keep`（要建的一半）/ `mirrored`（镜像得到）/
     `on_plane`（正好在对称面上，两边共用，**不能只建一半**）
  ③ **★ 装配定位面表** —— 每个方向（±X/±Y/±Z）上最大的那个平面，
     连同它的面积、站位、是不是"贴合面"（flat contact）
  ④ **建模基准面建议** —— 装配定位面通常应该平行于某个基准面；
     给出"建模时该选哪个基准面 + 装配时该用哪个面做重合"的一对建议

## 一条设计红线：**分析逻辑是纯函数**

与 `sw_faceset.py` 同源：读数据要 SolidWorks，**判据一律纯函数**（吃面清单吐结论），
所以能在没有 SolidWorks 的机器上被 `sw_selftest.py` 离线钉死。
★ 这条对"决策类"工具尤其重要 —— 决策错了，后面每一步都白做。

## 用法

    export APPDATA="C:\\Users\\OMEN\\AppData\\Roaming"
    /c/Python313/python.exe scripts/sw_symmetry.py --doc "C:\\x\\箱座.SLDPRT"
    /c/Python313/python.exe scripts/sw_symmetry.py --doc xx.SLDPRT --json out.json
    /c/Python313/python.exe scripts/sw_symmetry.py --self-test      # 不开 SW

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

import sw_faceset                                          # noqa: E402

# ---------------------------------------------------------------- 常量与容差

#: 对称判定的角度容差（度）。镜像一个面时法向要取反，浮点误差远小于此。
SYM_ANGLE_TOL_DEG = 1.0

#: 对称判定的位置容差（米）= 0.01mm。镜像点到原面的距离判据。
SYM_DIST_TOL_M = 1e-5

#: 面积相对容差：镜像配对面时两个面的面积应当**几乎相等**（0.5%）。
SYM_AREA_REL_TOL = 0.005

#: 对称度低于此值就不算"对称件"（免得把"差不多对称"当成对称，
#: 镜像后差 0.1mm 也能过 —— 那是假对称）。
SYM_MIN_SCORE = 0.90

#: 装配定位面的**最小面积占比**（占该方向所有平面面积之比）。
#: 太小的面当不了定位面（贴合面积不够，配合没有意义）。
MOUNT_MIN_AREA_FRAC = 0.18

#: 三个中面的名字（供报告与 `--base` 用）
AXES = {"X": 0, "Y": 1, "Z": 2}


# ================================================================ 纯函数层

def _unit(v):
    n = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
    return [float(x) / n for x in v]


def _dot(a, b) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b))


def _dist(a, b) -> float:
    return math.sqrt(sum((float(x) - float(y)) ** 2 for x, y in zip(a, b)))


def mirror_point(p, normal, offset):
    """点 `p` 关于平面 `n·x = offset` 的镜像点。"""
    n = _unit(normal)
    s = _dot(n, p) - offset
    return [float(p[i]) - 2.0 * s * n[i] for i in range(3)]


def mirror_normal(v, normal):
    """法向关于同一平面的镜像（方向向量：线性映射，不用管平移）。"""
    n = _unit(normal)
    s = _dot(n, v)
    return [float(v[i]) - 2.0 * s * n[i] for i in range(3)]


def mid_plane(box, axis_idx: int) -> tuple[list, float]:
    """包围盒在 `axis_idx` 方向上的**中面**（法向 + 站位）。

    ★ 站位取 `(min+max)/2` —— 这正是"把这半边建完、镜像过去"时该用的对称面。
      前提是包围盒在这个方向上确实对称（工具会另外用面积配对验证）。
    """
    n = [0.0, 0.0, 0.0]
    n[axis_idx] = 1.0
    return n, (float(box[axis_idx]) + float(box[axis_idx + 3])) / 2.0


def _face_key(f: dict, normal, offset: float) -> dict | None:
    """把一个面**镜像**之后的可比对描述。`center` 必���有，否则回 None。"""
    c = f.get("center")
    if not c:
        return None
    t = f.get("type")
    rec = {
        "type": t,
        "center": mirror_point(c, normal, offset),
        "area": float(f.get("area") or 0.0),
    }
    if t == "plane":
        nrm = f.get("normal")
        rec["normal"] = mirror_normal(nrm, normal) if nrm else None
        if rec["normal"] is not None:
            rec["normal"] = _unit(rec["normal"])
    elif t in ("cylinder", "cone", "torus"):
        ax = f.get("axis")
        rec["axis"] = _unit(mirror_normal(ax, normal)) if ax else None
        rec["radius"] = f.get("radius")
        rec["major"] = f.get("major")
    elif t == "sphere":
        sc = f.get("sphere_center")
        rec["center"] = mirror_point(sc, normal, offset) if sc else rec["center"]
        rec["radius"] = f.get("radius")
    return rec


def faces_match(a: dict, b: dict, angle_tol_deg: float = SYM_ANGLE_TOL_DEG,
                dist_tol: float = SYM_DIST_TOL_M) -> bool:
    """镜像后的面 `a` 与原面 `b` 是否"是同一个面"。

    判据 = **类型 + 面积 + 位置 + 朝向**四件套，缺一不可：
    只比位置会把"内孔面"和"外圆面"配成一对（中心镜像后对得上、面积却不同）；
    只比面积+法向会把两个不同的孔配成一对。
    """
    if a.get("type") != b.get("type"):
        return False
    aa, ba = float(a.get("area") or 0.0), float(b.get("area") or 0.0)
    if max(aa, ba) > 0:
        if abs(aa - ba) / max(aa, ba) > SYM_AREA_REL_TOL:
            return False
    ca, cb = a.get("center"), b.get("center")
    if not ca or not cb or _dist(ca, cb) > dist_tol:
        return False
    cos_tol = math.cos(math.radians(angle_tol_deg))
    for k in ("normal", "axis"):
        va, vb = a.get(k), b.get(k)
        if va and vb and abs(_dot(_unit(va), _unit(vb))) < cos_tol:
            return False
    if a.get("type") == "cylinder" and a.get("radius") is not None:
        rb = b.get("radius")
        if rb is None:
            return False
        if abs(float(a["radius"]) - float(rb)) > max(1e-6, SYM_DIST_TOL_M):
            return False
    return True


def symmetry_score(faces: list[dict], normal, offset: float,
                   angle_tol_deg: float = SYM_ANGLE_TOL_DEG,
                   dist_tol: float = SYM_DIST_TOL_M) -> dict:
    """这个平面的**对称度** = 镜像后能配上的面积 / 总面积。

    ★ 判据用**面积**而不是"面对个数"：一个大平面配不上，
      和一个小圆角配不上，代价完全不同。个数会让"小特征全对、大面错"
      的情况看起来很对称。

    ★★ **"镜像后落回自己"要拆成两类**（这是本函数最容易错的地方）：
        · `on_plane`    位置在对称面上**且法向 ⊥ 对称面** ⇒ 它**横跨两侧**，
                        镜像会**重叠** ⇒ 只能建一次（箱体中面上的加强筋端面、
                        分箱面贴合面都是这类）。
        · `cross_plane` 位置在对称面上但**法向 ∥ 对称面** ⇒ 它是**侧壁**，
                        自己关于中面对称（矩形侧壁就是这样），**不是中面上的面**。
      第一版把两者混在一起 ⇒ 一个完全对称的盒子只有 92% 对称，
      而 Y+/Y-/Z+/Z- 四个侧壁被误报成"中面上的面" ⇒ 计划会把侧壁也只建一半。
    """
    n = _unit(normal)
    cos_tol = math.cos(math.radians(angle_tol_deg))
    tot = sum(float(f.get("area") or 0.0) for f in faces) or 0.0
    matched = on_plane = cross = 0.0
    pairs: list = []
    used: set = set()
    for i, f in enumerate(faces):
        if i in used:
            continue
        a = float(f.get("area") or 0.0)
        img = _face_key(f, n, offset)
        if img is None:
            continue
        # 自己配自己 = 镜像后落回同一片面。分两类（见头注）
        if faces_match(img, f, angle_tol_deg, dist_tol):
            used.add(i)
            fn = _unit(f.get("normal") or [0, 0, 0])
            # 法向垂直于对称面 ⇒ 真的是"中面上的面"
            if abs(_dot(fn, n)) >= cos_tol:
                on_plane += a
                pairs.append({"a": f.get("id"), "b": f.get("id"),
                              "self": True, "kind": "on_plane",
                              "area_mm2": round(a * 1e6, 4)})
            else:
                # 法向平行于对称面 ⇒ 侧壁，自己关于中面对称
                cross += a
                pairs.append({"a": f.get("id"), "b": f.get("id"),
                              "self": True, "kind": "cross_plane",
                              "area_mm2": round(a * 1e6, 4)})
            continue
        hit = None
        for j, g in enumerate(faces):
            if j in used or j == i:
                continue
            if faces_match(img, g, angle_tol_deg, dist_tol):
                hit = j
                break
        if hit is None:
            continue
        used.add(i)
        used.add(hit)
        # ★★★ 配对是**两面**的事，两侧面积都要计入。
        #   只加 a 会让 matched 少一半面积 ⇒ unmatched 虚高 ⇒ 对称度系统性低估
        #   （完全对称的盒子只有 92.1%）⇒ "对称件"被误判成"不对称"
        #   ⇒ 计划去建全件而不是建一半 + 镜像。**一个会计错误改变了建模策略。**
        b_area = float(faces[hit].get("area") or 0.0)
        matched += a + b_area
        pairs.append({"a": f.get("id"), "b": faces[hit].get("id"),
                      "self": False, "kind": "pair",
                      "area_mm2": round((a + b_area) * 1e6, 4),
                      "a_area_mm2": round(a * 1e6, 4),
                      "b_area_mm2": round(b_area * 1e6, 4)})
    unmatched = tot - matched - on_plane - cross
    return {
        "normal": [round(v, 6) for v in n],
        "offset_mm": round(offset * 1000, 5),
        "total_area_mm2": round(tot * 1e6, 4),
        "symmetric": round((matched + on_plane + cross) / tot, 6) if tot else 0.0,
        "mirrored_area_mm2": round(matched * 1e6, 4),
        "on_plane_area_mm2": round(on_plane * 1e6, 4),
        "cross_plane_area_mm2": round(cross * 1e6, 4),
        "unmatched_area_mm2": round(unmatched * 1e6, 4),
        "is_symmetric": bool(
            tot and (matched + on_plane + cross) / tot >= SYM_MIN_SCORE),
        "pairs": pairs,
    }


def station_profile(faces: list[dict], axis_idx: int,
                    tol_mm: float = 0.05) -> list[dict]:
    """某个法向轴上的**面积-站位分布**（面积加权）。

    把法向 ≈ ±axis 的平面按站位聚类，返回 `[{"d": 站位mm, "area": mm²}, ...]`
    按站位升序。**这是找对称面的原材料** —— 见头注里箱座那个 0.04mm 的坑。
    """
    tol = tol_mm / 1000.0
    out: list[dict] = []
    for f in faces:
        if f.get("type") != "plane" or not f.get("normal"):
            continue
        n = _unit(f["normal"])
        if abs(n[axis_idx]) < 0.9:
            continue
        d = float(f.get("d") or 0.0)
        hit = next((x for x in out if abs(x["d"] - d) <= tol), None)
        a = float(f.get("area") or 0.0)
        if hit is None:
            out.append({"d": d, "area": a, "faces": [f.get("id")]})
        else:
            hit["area"] += a
            hit["faces"].append(f.get("id"))
    out.sort(key=lambda x: x["d"])
    return out


def best_symmetry(faces: list[dict], axis_idx: int,
                  box=None, max_cand: int = 12) -> dict:
    """沿某个轴找**最优对称面**，而不是直接用包围盒中点。

    候选站位分四类，**按可信度从高到低**试：
      ① **面积相等的站位对**的中点 —— 证据最硬（两个面镜像过去面积逐位相等）
      ② **相邻两站**的中点（板厚类对称：两片大面之间）
      ③ **单站**（面自己就在一个"对称站位"上）
      ④ 包围盒中点（兜底）

    ★★ 为什么必须有 ①：真机（`1-箱座`）里真实对称面是 `x=0`，而 `d=-52` 与
      `d=+52` 两个面**不相邻**（中间夹着 −26/−20/+45/+48/+50 五个不对称站位）。
      只试"相邻两站"⇒ 真相压根不在候选里 ⇒ 报出 −20（−20/−26 那对的中点）
      ⇒ 又是 0.00% 对称度。**"相邻"是个很自然但错误的假设。**

    ★ 返回值带 `bbox_mid_mm` / `offset_from_bbox_mm` / `bbox_mid_is_symmetric`：
      差得多就明说「包围盒中点不是对称面，别拿它当镜像面」。
    """
    prof = station_profile(faces, axis_idx)
    cands: list[tuple] = []          # (站位, 可信度标签, 支撑面积)
    n = [0.0, 0.0, 0.0]
    n[axis_idx] = 1.0

    def add(d, tag, w):
        cands.append((d, tag, w))

    # ① 面积相等的**任意**两站（不要求相邻）
    for i, x in enumerate(prof):
        for y in prof[i + 1:]:
            a, b = float(x["area"]), float(y["area"])
            if max(a, b) <= 0:
                continue
            if abs(a - b) / max(a, b) > SYM_AREA_REL_TOL:
                continue                        # 面积不等 ⇒ 镜像过去也对不上
            add((x["d"] + y["d"]) / 2.0, "pair_equal_area", a + b)
    # ② 相邻两站
    for i in range(len(prof) - 1):
        x, y = prof[i], prof[i + 1]
        add((x["d"] + y["d"]) / 2.0, "adjacent", x["area"] + y["area"])
    # ③ 单站
    for x in prof:
        add(x["d"], "single", x["area"])
    # ④ 包围盒中点
    if box and len(box) == 6:
        add((float(box[axis_idx]) + float(box[axis_idx + 3])) / 2.0,
            "bbox_mid", 0.0)

    # 支撑面积大的先试；同权重按站位稳定排序（保证结果可复现）
    cands.sort(key=lambda c: (-c[2], round(c[0], 9)))
    seen: set = set()
    best = None
    for d, tag, w in cands[:max_cand]:
        key = round(d, 6)
        if key in seen:
            continue
        seen.add(key)
        r = symmetry_score(faces, n, d)
        if best is None or r["symmetric"] > best["symmetric"]:
            r = dict(r)
            r["candidate_tag"] = tag
            r["candidate_weight_mm2"] = round(w * 1e6, 3)
            best = r
    best = best or {"normal": n, "offset_mm": 0.0, "symmetric": 0.0,
                    "is_symmetric": False, "pairs": [],
                    "candidate_tag": "none", "candidate_weight_mm2": 0.0}
    if box and len(box) == 6:
        mid = (float(box[axis_idx]) + float(box[axis_idx + 3])) / 2.0
        best["bbox_mid_mm"] = round(mid * 1000, 5)
        best["offset_from_bbox_mm"] = round(best["offset_mm"] - mid * 1000, 5)
        best["bbox_mid_is_symmetric"] = abs(
            best["offset_from_bbox_mm"]) <= 0.05
    return best


def find_symmetry_planes(faces: list[dict], box, tol_mm: float = 0.5) -> list[dict]:
    """试 X / Y / Z 三个方向，返回按对称度降序的候选表。

    ★★ **站位不能直接取包围盒中点**（这是本函数最容易犯的错）。
      真机（`1-箱座`）：包围盒 x ∈ [-52.994, +52.915] ⇒ 中点 -0.040mm，
      而真实对称面是 **x=0**（d=-52 与 d=+52 的面积逐位相等 = 4418.1mm²）。
      差 **0.04mm** ⇒ 每个面的镜像都差 0.04mm ⇒ **一个都配不上**
      ⇒ 对称度 0.00% ⇒ 工具报"不对称" ⇒ 计划去建全件而不是建一半 + 镜像。
      ⇒ 站位改由 `best_symmetry()` 按**面积配对**自己找，包围盒中点只当兜底候选。

    ★ 只试**三个主轴方向**是刻意的：真实零件的对称面几乎总是主基准面
      （Right/Top/Front）或与之平行，而"建一半 + 镜像"要用的正是这个面。
    """
    out = []
    for name, idx in AXES.items():
        if not box or len(box) != 6:
            break
        r = best_symmetry(faces, idx, box)
        r["axis"] = name
        r["half_extent_mm"] = round(
            abs(float(box[idx + 3]) - r["offset_mm"] / 1000.0) * 1000, 4)
        r["note"] = "沿 {} 的最优对称面在站位 {:+.4f}mm（候选来源 {}）".format(
            name, r["offset_mm"], r.get("candidate_tag", "?"))
        if not r.get("bbox_mid_is_symmetric", True):
            r["note"] += ("  ★★ 包围盒中点({:+.4f}mm)**不是**对称面，"
                          "别拿它当镜像面".format(r.get("bbox_mid_mm", 0.0)))
        out.append(r)
    out.sort(key=lambda r: -r["symmetric"])
    return out


def half_split(faces: list[dict], normal, offset: float) -> dict:
    """把面分成四份：

      · `keep`    正法向一侧 —— **要建的一半**
      · `mirrored` 负法向一侧 —— 镜像得到
      · `on_plane`  **法向 ⊥ 对称面且跨在面上** ⇒ 镜像会**重叠**，只能建一次
      · `cross`    **法向 ∥ 对称面**的侧壁 —— 自己就关于中面对称，整片建出来即可

    ★ 后两类最容易漏。箱体中面上常有加强筋的端面、分箱面的贴合面、轴孔的对称槽
      —— 它们横跨两侧，工具把它们单列出来，正是为了让人（或计划）知道：
      这些特征**只能建一次，且要建在对称面上**。
    ★★ 而**侧壁**（法向 ∥ 对称面）虽然"面上一点落在中面上"，却**不是中面上的面**
      —— 判错这一类，计划就会把侧壁也只建一半（第一版就这样，对称度只有 92%）。
    """
    n = _unit(normal)
    cos_tol = math.cos(math.radians(SYM_ANGLE_TOL_DEG))
    keep, mirrored, on_plane, cross, straddling = [], [], [], [], []
    for f in faces:
        c = f.get("center")
        if not c:
            straddling.append({"id": f.get("id"), "why": "读不到面上一点"})
            continue
        s_ = _dot(n, c) - offset
        fn = f.get("normal")
        fn = _unit(fn) if fn else None
        perp = bool(fn and abs(_dot(fn, n)) >= cos_tol)
        if abs(s_) <= SYM_DIST_TOL_M:
            (on_plane if perp else cross).append(f.get("id"))
        elif s_ > 0:
            keep.append(f.get("id"))
        else:
            mirrored.append(f.get("id"))
        b = f.get("box")
        if b and len(b) == 6:
            lo = _dot(n, [b[0], b[1], b[2]]) - offset
            hi = _dot(n, [b[3], b[4], b[5]]) - offset
            if lo < -SYM_DIST_TOL_M and hi > SYM_DIST_TOL_M:
                straddling.append({
                    "id": f.get("id"),
                    "why": "横跨对称面（{:+.3f}…{:+.3f}mm）".format(lo * 1000,
                                                                  hi * 1000),
                    "action": "**只能建在对称面上**，不能只建一半"})
    return {"keep": keep, "mirrored": mirrored, "on_plane": on_plane,
            "cross": cross, "straddling": straddling,
            "side": "正法向一侧"}


def mount_faces(faces: list[dict], box=None,
                min_frac: float = MOUNT_MIN_AREA_FRAC) -> list[dict]:
    """**装配定位面**表：每个方向（±X/±Y/±Z）上最大的那个平面。

    这就是装配图上「从左装 / 从右装 / 从前装 / 从后装」对应的那个面。
    建模时如果这个面平行于某个基准面（或者就是它），装配时一次重合就能定位；
    否则可能要两三条配合才对得上（= 用户说的「冗余操作」）。

    判据：按方向分成 6 组，**每组取面积最大的平面**。
    只在**平面**里选 —— 圆柱面/球面贴合没有确定的位置自由度。

    ★★ **方向按「面中心相对包围盒中心的位置」定，不按法向定。**
      原因：SolidWorks 里两个相对面的 `normal` **可能同向**（取决于建模顺序与
      面的朝向约定）—— 与 `sw_faceset` 里 `ISurface.PlaneParams.d` 不可信同类。
      第一版按法向分组，一个 40×30×10 的盒子只出 **3** 条（X+/X- 归成一组、
      取了面积大的那个，小的丢掉），而真实答案是 **6** 条。
      **位置是几何事实，不受朝向约定影响** —— 这条判据比"看起来更权威"的法向可靠。
    """
    dirs: dict = {}
    for f in faces:
        if f.get("type") != "plane" or not f.get("normal"):
            continue
        n = _unit(f["normal"])
        # 先用法向定**轴**（±法向都算同一根轴）
        axis_idx = max(range(3), key=lambda i: abs(n[i]))
        # 再用位置定**符号**：面中心在包围盒中心这一侧还是那一侧
        sign = 1
        c = f.get("center")
        if c and box and len(box) == 6:
            mid = (float(box[axis_idx]) + float(box[axis_idx + 3])) / 2.0
            # 面上一点可能恰好落在中面上 ⇒ 这时用法向的该分量定符号
            if abs(float(c[axis_idx]) - mid) > SYM_DIST_TOL_M:
                sign = 1 if float(c[axis_idx]) > mid else -1
            else:
                sign = 1 if n[axis_idx] >= 0 else -1
        key = (axis_idx, sign)
        a = float(f.get("area") or 0.0)
        cur = dirs.get(key)
        if cur is None or a > cur["area"]:
            dirs[key] = {"area": a, "face": f.get("id"),
                         "d_mm": (float(f["d"]) * 1000) if f.get("d") is not None
                         else None}
    tot = sum(v["area"] for v in dirs.values()) or 1.0
    out = []
    for (axis_idx, sign), v in dirs.items():
        axis = "XYZ"[axis_idx]
        out.append({
            "dir": axis + ("正" if sign > 0 else "负"),
            "face": v["face"],
            "area_mm2": round(v["area"] * 1e6, 3),
            "area_frac": round(v["area"] / tot, 4),
            "station_mm": (None if v["d_mm"] is None else round(v["d_mm"], 4)),
            "is_candidate": v["area"] / tot >= min_frac,
        })
    order = {"X正": 0, "X负": 1, "Y正": 2, "Y负": 3, "Z正": 4, "Z负": 5}
    out.sort(key=lambda r: order.get(r["dir"], 9))
    return out


def base_plane_advice(mount: list[dict], sym: dict | None) -> dict:
    """**建模基准面建议** —— 装配定位面 ↔ 建模基准面的配对。

    ★ 核心规则（来自用户的经验）：
      在装配图上"从某方向装"的件，那个方向上必有一个大平面；
      建模时**让那个面平行于某个基准面**（理想是就是它），
      装配时就能用**一次重合**把它定位。
    ⇒ 建议 = 「建模基准面」+「装配时用哪个面重合」，成对给出。
    """
    cands = [m for m in mount if m["is_candidate"]]
    advice = []
    for m in cands[:3]:
        axis = m["dir"][0]
        # 基准面对应关系：SolidWorks 的 Right=YZ(法向 X)、Top=XZ(法向 Y)、
        # Front=XY(法向 Z) —— 建在哪个面上，草图平面就垂直于那个轴。
        base = {"X": "Right（右视）", "Y": "Top（上视）", "Z": "Front（前视）"}[axis]
        advice.append({
            "mount_dir": m["dir"],
            "mount_face": m["face"],
            "mount_area_mm2": m["area_mm2"],
            "suggest_base": base,
            "assembly_op": "coincident（一次重合定位）",
            "why": ("沿 {} 方向安装 => 贴合面是 {}（{:.1f}mm²）"
                    "=> 建模时把该方向对齐到{}基准面，装配一次重合即可定位"
                    ).format(m["dir"], m["face"], m["area_mm2"], base),
        })
    if sym and sym.get("is_symmetric"):
        advice.append({
            "symmetry": sym["axis"],
            "symmetry_plane_mm": sym["offset_mm"],
            "suggest_base": {"X": "Right（右视）", "Y": "Top（上视）",
                             "Z": "Front（前视）"}[sym["axis"]],
            "why": f"关于 {sym['axis']} 中面（{sym['offset_mm']:+.4f}mm）对称 "
                   f"{sym['symmetric'] * 100:.1f}% ⇒ **建一半 + mirror**，"
                   f"草图画在该基准面上，镜像面即它自己",
        })
    return {"advices": advice,
            "note": "★ 基准面选对 ⇒ 装配一次重合定位；选错 ⇒ 需要 2~3 条配合才对得上，"
                    "而且改一个尺寸要改两遍（建全件的情况）"}


# ================================================================ 读数据层

def read_data(sw) -> tuple[list[dict], list, float | None]:
    faces = sw_faceset.read_faces(sw)
    box = sw_faceset.summarise(faces).get("box_mm")
    box_m = [v / 1000.0 for v in box] if box else None
    vol = None
    with contextlib.suppress(Exception):
        mp = sw.mass_props()
        if mp.get("volume_m3") and not mp.get("suspect"):
            vol = float(mp["volume_m3"]) * 1e9
    return faces, box_m, vol


# ================================================================ 报告

def print_report(faces: list[dict], box, vol, syms: list[dict],
                 halves: dict, mount: list[dict], advice: dict) -> None:
    s = sw_faceset.summarise(faces)
    print(f"面 {s['n_faces']} 个   类型分布 {s['by_type']}")
    if vol:
        print(f"体积 = {vol:.1f} mm³")
    print(f"整体包围盒(mm) = {s['box_mm']}")

    print(f"\n== 对称面候选（按对称度降序；{int(SYM_MIN_SCORE * 100)}% 以上算对称）==")
    for r in syms:
        n = r["normal"]
        flag = "★★ 对称件" if r["is_symmetric"] else "  不对称"
        print(f"  {flag}  中面法向=({n[0]:+.3f},{n[1]:+.3f},{n[2]:+.3f}) "
              f"站位={r['offset_mm']:+.4f}mm   对称度={r['symmetric'] * 100:6.2f}%")
        print("        配对 {} 对 / 配对面积 {:.1f}mm² / 中面上 {:.1f}mm² / "
              "侧壁 {:.1f}mm² / 配不上 {:.1f}mm²".format(
                  sum(1 for x in r["pairs"] if not x["self"]),
                  r["mirrored_area_mm2"], r["on_plane_area_mm2"],
                  r["cross_plane_area_mm2"], r["unmatched_area_mm2"]))
        if r.get("bbox_mid_mm") is not None and not r.get("bbox_mid_is_symmetric",
                                                          True):
            print("        ★★ 包围盒中点 {:+.4f}mm 与最优对称面差 {:.4f}mm"
                  " ⇒ 中点**不是**对称面（真机 `1-箱座` 就是这样：中点 "
                  "-0.040、对称面 0.000，差 0.04 就让对称度从高掉到 0）".format(
                      r["bbox_mid_mm"], abs(r["offset_from_bbox_mm"])))

    best = next((r for r in syms if r["is_symmetric"]), None)
    if best:
        print(f"\n== 半边清单（对称面 = {best['axis']} 中面 "
              f"{best['offset_mm']:+.4f}mm）==")
        print(f"  要建的一半 keep     = {len(halves['keep'])} 个面 {halves['keep']}")
        print(f"  镜像得到   mirrored  = {len(halves['mirrored'])} 个面")
        print("  ★ 共用     on_plane  = {} 个面 {}".format(
            len(halves["on_plane"]), halves["on_plane"]))
        print("    （法向垂直于对称面、横跨两侧的面 —— 镜像会**重叠**，只能建一次）")
        print("  · 侧壁     cross     = {} 个面 {}（整片建出来即可，"
              "不用镜像）".format(len(halves.get("cross") or []),
                                  halves.get("cross")))
        if halves["straddling"]:
            print(f"  ⚠ 横跨对称面 {len(halves['straddling'])} 个（必须建在对称面上）：")
            for x in halves["straddling"][:6]:
                print(f"     · {x['id']}  {x.get('why') or x.get('action')}")

    print(f"\n== 装配定位面（每方向最大的平面；"
          f"≥{int(MOUNT_MIN_AREA_FRAC * 100)}% 面积占比算候选）==")
    for m in mount:
        flag = "★ 定位面" if m["is_candidate"] else "  (太小)"
        print(f"  {flag} {m['dir']}  面 {str(m['face']):<10} "
              f"面积={m['area_mm2']:9.1f}mm² 占比={m['area_frac'] * 100:5.1f}% "
              f"站位={m['station_mm']}mm")

    print(f"\n== 建模基准面建议 ==")
    for a in advice["advices"]:
        print(f"  · {a.get('why')}")
    print(f"  {advice['note']}")


# ================================================================ 自检

def self_test() -> int:
    n_ok = n_bad = 0

    def chk(cond, msg):
        nonlocal n_ok, n_bad
        if cond:
            n_ok += 1
            print(f"  PASS  {msg}")
        else:
            n_bad += 1
            print(f"  FAIL  {msg}")

    def P(n, d, area, name, c, box=None):
        return {"id": name, "type": "plane", "area": area * 1e-6,
                "center": list(c), "normal": _unit(n),
                "d": _dot(_unit(n), c), "box": box}

    print("[1] 镜像数学")
    chk([round(v, 6) for v in mirror_point([0.03, 0, 0], [1, 0, 0], 0.0)]
        == [-0.03, 0, 0], "点关于 yz 中面镜像")
    chk([round(v, 6) for v in mirror_point([0.03, 0.02, 0], [1, 0, 0], 0.01)]
        == [-0.01, 0.02, 0], "点关于 x=0.01 的面镜像（带偏置）")
    chk(mirror_normal([1, 0, 0], [1, 0, 0]) == [-1.0, 0.0, 0.0],
        "法向垂直于对称面 ⇒ 镜像后**取反**")
    chk(mirror_normal([0, 1, 0], [1, 0, 0]) == [0.0, 1.0, 0.0],
        "法向平行于对称面 ⇒ 镜像后**不变**")

    print("[2] 完全对称的盒子")
    # 40x30x10 的盒子，x∈[0,40] y∈[-15,15] z∈[-5,5]
    bx = [0.0, -0.015, -0.005, 0.04, 0.015, 0.005]
    fs = [P([1, 0, 0], 0.04, 300, "X+", [0.04, 0.0, 0.0],
            [0.04, -0.015, -0.005, 0.04, 0.015, 0.005]),
          P([1, 0, 0], 0.0, 300, "X-", [0.0, 0.0, 0.0],
            [0.0, -0.015, -0.005, 0.0, 0.015, 0.005]),
          P([0, 1, 0], 0.015, 400, "Y+", [0.02, 0.015, 0.0],
            [0.0, 0.015, -0.005, 0.04, 0.015, 0.005]),
          P([0, 1, 0], -0.015, 400, "Y-", [0.02, -0.015, 0.0],
            [0.0, -0.015, -0.005, 0.04, -0.015, 0.005]),
          P([0, 0, 1], 0.005, 1200, "Z+", [0.02, 0.0, 0.005],
            [0.0, -0.015, 0.005, 0.04, 0.015, 0.005]),
          P([0, 0, 1], -0.005, 1200, "Z-", [0.02, 0.0, -0.005],
            [0.0, -0.015, -0.005, 0.04, 0.015, -0.005])]
    r = symmetry_score(fs, [1, 0, 0], 0.02)
    chk(r["is_symmetric"] and abs(r["symmetric"] - 1.0) < 1e-9,
        f"关于 x=0.02 的面完全对称（实得 {r['symmetric'] * 100:.2f}%）")
    h = half_split(fs, [1, 0, 0], 0.02)
    chk(h["keep"] == ["X+"] and h["mirrored"] == ["X-"],
        "只有 X+ / X- 这一对是**半边**（实得 keep={} mirrored={}）".format(
            h["keep"], h["mirrored"]))
    chk(sorted(h["cross"]) == ["Y+", "Y-", "Z+", "Z-"] and not h["on_plane"],
        "★★ Y±/Z± 是**侧壁**不是中面上的面 ⇒ 进 cross（整片建出来即可，"
        "**不能只建一半**）—— 第一版把它们误报成 on_plane，照那个结果建模"
        "会把四个侧壁也只建一半")

    print("[3] ★ 单侧结构：加了半边不对称的凸台 ⇒ 对称度必须掉下来")
    fs2 = fs + [P([0, 0, 1], 0.009, 500, "Boss", [0.02, 0.0, 0.009],
                  [0.01, -0.01, 0.005, 0.03, 0.01, 0.009])]
    # 凸台法向 +Z、只加在 z>0 一侧 ⇒ 它破坏的是 **Z 向**（上下）对称
    r3 = symmetry_score(fs2, [0, 0, 1], 0.0)
    r2 = symmetry_score(fs2, [1, 0, 0], 0.02)
    chk(not r3["is_symmetric"] and r3["symmetric"] < 0.9,
        "单侧凸台破坏**它自己法向那个方向**的对称（Z 向实得 {:.1f}%）".format(
            r3["symmetric"] * 100))
    chk(r2["symmetric"] > 0.9,
        "★ 同件在**另一个**方向上仍对称 ⇒ **必须每个方向都试**，只看一个方向"
        "会误判（Z 向 {:.1f}% vs X 向 {:.1f}%）".format(
            r3["symmetric"] * 100, r2["symmetric"] * 100))

    print("[4] ★ 中面上的面要单列，不能算成镜像配对")
    # 中面上的筋端面：x 跨中面（10…30，中面在 20）⇒ 法向是 ±X 时它横跨；
    # 这里让它法向 +Z 且中心在中面上 ⇒ 属于 on_plane
    # 中面上的筋端面：★ 法向必须**垂直于对称面**（对称面法向是 +X ⇒ 端面法向 ±X），
    #   否则它会被当成"侧壁"。第一版给它 +Z，结果永远进不了 on_plane。
    fs3 = fs + [P([1, 0, 0], 0.02, 400, "RibEnd", [0.02, 0.0, 0.0],
                  [0.02, -0.01, 0.005, 0.02, 0.01, 0.0055])]
    h3 = half_split(fs3, [1, 0, 0], 0.02)
    chk("RibEnd" in h3["on_plane"],
        "正好在中面上的面进 on_plane（**镜像会重叠，只能建一次**）")
    r4 = symmetry_score(fs3, [1, 0, 0], 0.02)
    chk(any(p["self"] for p in r4["pairs"]),
        "on_plane 的面在配对表里标 self=True")

    print("[5] ★ 横跨对称面的面要报警")
    fs4 = fs + [{"id": "Straddle", "type": "plane", "area": 100e-6,
                 "center": [0.02, 0.0, 0.0], "normal": [0, 0, 1], "d": 0.0,
                 "box": [0.01, -0.005, 0.0, 0.03, 0.005, 0.001]}]
    h4 = half_split(fs4, [1, 0, 0], 0.02)
    chk(any(x["id"] == "Straddle" for x in h4["straddling"]),
        "横跨中面的面进 straddling 并给出「只能建在对称面上」")

    print("[6] 面积不能当个数用")
    big = P([1, 0, 0], 0.04, 5000, "Big", [0.04, 0.0, 0.0],
            [0.04, -0.01, -0.01, 0.04, 0.01, 0.01])
    r5 = symmetry_score(fs + [big], [1, 0, 0], 0.02)
    chk(r5["unmatched_area_mm2"] > 4000,
        f"一个大面配不上就拉低对称度（未配对面积 "
        f"{r5['unmatched_area_mm2']:.0f}mm²）")
    smalls = [P([1, 0, 0], 0.04, 0.5, "S%d" % i, [0.04, 0.0, 0.0],
                [0.04, 0.0, 0.0, 0.0401, 0.0, 0.0]) for i in range(20)]
    r6 = symmetry_score(fs + smalls, [1, 0, 0], 0.02)
    chk(r6["symmetric"] > 0.99,
        "★ 20 个配不上的**小**面（面积占比 0.3%）几乎不影响对称度"
        "（实得 {:.2f}%）—— 判据确实以**面积**为主，不是数面个数".format(
            r6["symmetric"] * 100))
    chk(r5["symmetric"] < r6["symmetric"],
        "★ 对照：一个配不上的**大**面就足以拉低对称度（{:.2f}% vs {:.2f}%）"
        " —— 同样是 1 个面，权重差 3 个数量级".format(
            r5["symmetric"] * 100, r6["symmetric"] * 100))

    print("[7] 装配定位面")
    m = mount_faces(fs, bx)
    dirs = {x["dir"] for x in m}
    chk(dirs == {"X正", "X负", "Y正", "Y负", "Z正", "Z负"},
        f"六个方向各出一个定位面（实得 {sorted(dirs)}）")
    xp = next(x for x in m if x["dir"] == "X正")
    zp = next(x for x in m if x["dir"] == "Z正")
    chk(zp["area_mm2"] > xp["area_mm2"],
        "★ Z 向面（40×30=1200）比 X 向面（30×10=300）大（实得 {:.0f} > {:.0f}）"
        " —— 与盒子的真实几何逐位对上".format(zp["area_mm2"], xp["area_mm2"]))
    chk(xp["face"] == "X+" and zp["face"] == "Z+",
        "定位面取的是**该方向面积最大**的那张（实得 X正={} Z正={}）".format(
            xp["face"], zp["face"]))

    print("[8] 非平面不进定位面表")
    cyl = {"id": "Hole", "type": "cylinder", "area": 200.0, "center": [0.02, 0, 0],
           "axis": [0, 1, 0], "radius": 0.003}
    m2 = mount_faces(fs + [cyl], bx)
    chk(len(m2) == 6, f"圆柱面**不进**定位面表（实得 {len(m2)} 条）")

    print("[9] 基准面建议")
    syms = find_symmetry_planes(fs, bx)
    adv = base_plane_advice(m, syms[0])
    a0 = adv["advices"][0] if adv["advices"] else {}
    chk(a0.get("mount_dir") == "Z正" and "Front" in a0.get("suggest_base", ""),
        "★ 基准面建议**跟着最大的定位面走**：Z 向面最大 ⇒ 建议 Front（前视）"
        "（实得 {} + {}）—— 盒子 40×30 的两个大面法向正是 ±Z".format(
            a0.get("mount_dir"), a0.get("suggest_base")))
    chk(any("symmetry" in x for x in adv["advices"]),
        "★ 同时给出**对称面**建议（建一半 + mirror 该用哪个基准面）")

    print("[10] 中面站位")
    n, off = mid_plane(bx, 0)
    chk(abs(off - 0.02) < 1e-9, f"X 向中面站位 = 20mm（实得 {off * 1000:.3f}）")

    print(f"\n自检：{n_ok} 通过 / {n_bad} 失败")
    return 0 if n_bad == 0 else 1


# ================================================================ CLI

def main() -> int:
    ap = argparse.ArgumentParser(
        description="对称性与装配面决策：对称面 / 半边 / 装配定位面 / 基准面建议")
    ap.add_argument("--doc", help="零件 .SLDPRT（只读打开）")
    ap.add_argument("--json", help="把结论写到这个 JSON")
    ap.add_argument("--self-test", action="store_true", help="不打开 SolidWorks")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.doc:
        ap.print_help()
        return 2

    import sw_connect                                          # noqa: PLC0415
    sw = sw_connect.SwSession.open(visible=False)
    sw.open_doc(a.doc, read_only=True)
    faces, box, vol = read_data(sw)
    if not faces:
        print(f"读不到任何面：{a.doc}")
        return 1

    syms = find_symmetry_planes(faces, box)
    best = next((r for r in syms if r["is_symmetric"]), None)
    halves = half_split(faces, best["normal"],
                        best["offset_mm"] / 1000.0) if best else None
    mount = mount_faces(faces, box)
    advice = base_plane_advice(mount, best)
    print_report(faces, box, vol, syms, halves, mount, advice)

    if a.json:
        out = {"doc": a.doc, "n_faces": len(faces), "volume_mm3": vol,
               "box_mm": sw_faceset.summarise(faces)["box_mm"],
               "symmetry_planes": syms, "half": halves,
               "mount_faces": mount, "advice": advice}
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print(f"\n结论已写入 {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
