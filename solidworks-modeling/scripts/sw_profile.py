#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""参照件型面提取：把成品 `.SLDPRT` 的**回转半剖面**反推成**元素序列**（直线 + 圆弧）。

## 为什么需要它

`sw_faces.py` 只给三张表（圆柱台阶 / 平面站位 / 锥面清单），够做**单调**的回转体
（阶梯轴、圆环、闷盖）。但盘盖类里的**透盖**（端盖1/3）型面是**非单调**的 ——
外圈台阶往外走一圈，中间又有内凹的轴承孔/密封座/退刀槽往里走，`stairway` 出来的
"站位 → 直径"串不成一条闭合轮廓（`parts.cover1._measured_faces` 里记着这桩悬案）。
同一个坑还挡住挡油环（台阶+锥面）、垫片1（解析值差 5%）、螺塞、通气塞。

## 怎么破 —— 读**面的边界圆**，不读草图、也不读顶点

不去读草图图元（§64：晚期绑定下 `ISketch.GetLines2` 回空、`GetArcs2` 回一堆 float）。

**也不读顶点** —— 这是现场踩出来的第二层坑：回转体的面边界全是**闭环圆边**，
而 SolidWorks 的闭环边**没有顶点**（`IEdge.GetStartVertex` 回 Nothing），
所以"读顶点"这条路在回转件上必然全空（34-套筒 实得 0/4）。
（`sw_ops._edge_mid` 的注释里其实已经记过这一脚："闭环边（圆柱口那种）没有顶点"。）

正解是走**边的曲线**：`IEdge.GetCurve()` → `ICurve.IsCircle` + `CircleParams`
（7 个 double：圆心 xyz、法向 xyz、半径）。回转体每张面都是"两个圆之间的一段"，
两个圆各自给出 `(x, r)` 一个端点：

    · 圆柱面  → 水平段   (x0, r) — (x1, r)
    · 平面⊥轴 → 竖直段   (x, Ri) — (x, Ro)
    · 圆锥面  → 斜段     (x0, R1) — (x1, R2)
    · **环面** → **圆弧**  (x0, R0) — (x1, R1)，圆心/半径取 `ISurface.TorusParams`
      （8 个 double：圆心 xyz、轴向 xyz、**主半径** = 轴线到管子中心的距离、
       **次半径** = 剖面圆弧半径；都是米）。19-端盖1 的 R2 圆角就是这么解出来的。

每张面**恰好贡献一个元素**。把元素按端点接起来就是闭合轮廓。
顶点/盒子+面积只是读不到曲线时的退路。

## 两道自校验（这是它敢用的前提）

1. **闭合**：拼出来的链必须首尾相接；开口链只允许两端都落在 `r≈0`（贴轴的实体，
   轴向那段本来就不是面）。
2. **Pappus 定理**：`V = 2π · r̄ · A`（A = 剖面面积，r̄ = 剖面形心到轴的距离）。
   把提取出的轮廓算出体积，和参照件实测体积比 —— 轮廓错一段，这条立刻红。

★ 实测（本轮）：
  · 34-套筒   → `x∈[−6.5,6.5] / r∈[15,18.5]`（OD37/孔30/厚13 的矩形环），差 **0.0006%**
  · 22-端盖2  → 8 点轮廓，与生成器里**手写**的型面完全同构，差 **0.0003%**
  · 28-端盖3  → 17 段直线闭合，差 **0.0000%**
  · 19-端盖1  → 17 段直线 + **1 段 R2 圆弧**，差 **0.0000%**
    （弧：圆心 (1,24)、半径 2.0，与 x=−1 端面、Ø52 壁都相切 ⇒ 回转腔底的 R2 圆角）

## 用法

    python scripts/sw_profile.py --doc "C:\\学习软件\\一级减速器\\19-端盖1.SLDPRT"
    python scripts/sw_profile.py --doc xx.SLDPRT --json out.json --expect-volume 24360.5
    python scripts/sw_profile.py --doc xx.SLDPRT --axis 0        # 手动指定回转轴

    # ★ 批量（复用一次 SolidWorks 会话，省启停；每件一个 json）
    python scripts/sw_profile.py --docs a.SLDPRT b.SLDPRT --json-dir D:/out/prof
    python scripts/sw_profile.py --dir "C:\\学习软件\\一级减速器" --json-dir D:/out/prof

只读打开、不保存，不改参照件。批量的 `--expect-volume` 不适用（每件体积不同）——
要校 Pappus 就逐件跑，或把 json 里的 `pappus_volume_mm3` 与 `sw_refscan` 的体积表对账。
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

import sw_connect  # noqa: E402
import sw_faces  # noqa: E402
from sw_connect import member, typed  # noqa: E402


# --------------------------------------------------------------------------- #
# 回转轴判定
# --------------------------------------------------------------------------- #

def _doms(v: list[float]) -> int:
    """向量主分量落在哪根轴上。"""
    return max(range(3), key=lambda i: abs(v[i]))


def detect_axis(faces: list[dict]) -> int:
    """按**圆柱面轴向投票**判回转轴（取圆柱面总面积最大的那根轴）。

    ★ 为什么不沿用 `sw_faces.py` 的"包围盒最长边"：盘盖类的包围盒是
      `[16, 68, 68]` —— 最长边是**直径**不是轴，会判成 Y 轴。盘、盖、环
      全都踩这个坑。回转体的轴向一定**躺着最多圆柱面**，按面积投票才稳。
    """
    score = [0.0, 0.0, 0.0]
    for f in faces:
        if f.get("kind") != "cyl":
            continue
        ax = _doms(f.get("axis") or [0.0, 0.0, 0.0])
        score[ax] += float(f.get("area_mm2") or 0.0)
    if max(score) <= 0.0:
        return 0
    return max(range(3), key=lambda i: score[i])


# --------------------------------------------------------------------------- #
# 读面的边界圆 + 环面参数
# --------------------------------------------------------------------------- #

def face_marks(f2) -> tuple[list[dict], dict]:
    """读一张面 → `(边界圆列表, 解析面参数)`。

    边界圆：`[{"c":[x,y,z]mm, "r":mm, "axis":[...]}, …]`（去重）。
    解析面参数：环面 `{"torus":[8 个 double]}`、球面 `{"sphere":[cx,cy,cz,R]}`（**米**）。
    回转体面的边界是 1~2 个**整圆**（闭环边没有顶点，只能走曲线）。

    ★ 2026-10-02 补球面：反光片（3-反光片）的**球冠反光面**是 2 张 `sphere` 面，
      以前没人抓参数 ⇒ `sw_profile` 只能报「面类型 sphere 未支持」，
      反光片因此一直建不出来。球面与环面在**剖面里都是圆弧**，但：
        · 环面（torus）：剖面圆心**离轴有距离**（= 主半径）
        · 球面（sphere）：剖面圆心**就在轴上**（= 球心，离轴 0）
      所以两者必须分开处理，混用会把球冠画成圆角。
    """
    out: list[dict] = []
    extra: dict = {}
    edges = None
    with contextlib.suppress(Exception):
        edges = member(f2, "GetEdges")
    for e in (edges or ()):
        mark = None
        with contextlib.suppress(Exception):
            e2 = typed(e, "IEdge")
            cur = typed(member(e2, "GetCurve"), "ICurve")
            if cur is not None and bool(member(cur, "IsCircle")):
                cp = member(cur, "CircleParams")
                if cp is not None and len(cp) >= 7:
                    mark = {
                        "c": [round(float(cp[i]) * 1000.0, 4) for i in range(3)],
                        "r": round(float(cp[6]) * 1000.0, 4),
                        "axis": [round(float(cp[i]), 6) for i in range(3, 6)],
                    }
        if mark is None:
            continue
        if not any(abs(mark["c"][i] - m["c"][i]) < 1e-3 and abs(mark["r"] - m["r"]) < 1e-3
                   for m in out for i in range(3)):
            out.append(mark)
    with contextlib.suppress(Exception):
        srf = typed(member(f2, "GetSurface"), "ISurface")
        if srf is not None:
            if bool(member(srf, "IsTorus")):
                tp = member(srf, "TorusParams")
                if tp is not None and len(tp) >= 8:
                    extra["torus"] = [round(float(x), 9) for x in tp]
            elif bool(member(srf, "IsSphere")):
                # `ISurface::SphereParams` → [cx, cy, cz, R]（**米**）。
                # ★ 与其它 `*Params` 一样必须走 `member()`（晚期绑定下直接取属性
                #   会拿到绑定方法或 None —— 这个坑在 `GetTitle`/`X` 上各栽过一次）。
                sp = member(srf, "SphereParams")
                if sp is not None and len(sp) >= 4:
                    extra["sphere"] = [round(float(x), 9) for x in sp[:4]]
    return out, extra


def _mark_xr(mark: dict, ax: int) -> tuple:
    """边界圆 → (轴向站位 x, 半径 r)。只在**圆心在轴上**时才有意义。"""
    return (mark["c"][ax], mark["r"])


def _lat(ax: int) -> list[int]:
    """除回转轴之外的另外两个坐标下标。"""
    return [i for i in range(3) if i != ax]


def _off_axis(mark: dict, ax: int, center: tuple | None = None) -> float:
    """边界圆的**圆心离回转轴的距离**（mm）。

    ★ 回转体的面边界圆，圆心必然落在轴上（偏移 ≈ 0）。
    圆心明显偏离轴的圆 = **不在轴上的孔 / 球冠 / 凸台 / 键槽底**，
    它的半径**不是**回转半径。

    这是本工具最容易静默出错的地方：以前的 `_mark_xr` 直接取
    `(c[ax], r)`，对偏心圆照样折出一条"母线" ——
    垫片1（Ø34 环 + 偏心孔）就这么拼出 5 条边（3 横 2 竖，
    **奇数边根本闭不了环**），反光片的球冠被折成 `r=1.5` 的假直线。
    两个都是**静默**的假轮廓，只有 Pappus 体积对不上才露馅。

    ★★ `center` 是**回转轴在另两个坐标方向上的位置**（见 `detect_axis_center`）。
      不是所有参照件的轴都过原点：16-螺塞 的轴平行于 Z 但落在 (0, **1.5**)，
      那时若拿坐标轴当真轴，**每一张面**都会被判偏心 1.5mm ⇒ 整个零件被误判成
      "不是纯回转体"。所以判"偏不偏心"必须相对**真轴**，不是相对坐标轴。
    """
    c = mark.get("c") or [0.0, 0.0, 0.0]
    lat = _lat(ax)
    if center is None:
        return math.hypot(*(float(c[i]) for i in lat))
    return math.hypot(float(c[lat[0]]) - center[0], float(c[lat[1]]) - center[1])


def detect_axis_center(faces: list[dict], ax: int, tol: float = 0.05,
                       min_share: float = 0.5) -> tuple:
    """回转轴在**另两个坐标方向**上的位置（mm）。取不准就回 `(0, 0)`。

    ★★ 2026-10-02 新增，起因是 16-螺塞：它的回转轴平行于 Z，但落在
      `(x=0, y=1.5)` —— 于是它**每一张面**的边界圆圆心离 Z 轴都是 1.5mm，
      偏心守卫把整件判成"不是纯回转体"（**误判**）。这类"轴不过原点"的参照件
      以前完全没有处理，只会得到一份看不懂的 blockers。

    判据：把**所有边界圆的圆心**按 tol 分桶，取「桶内 Σr² 最大」的桶 ——
      与回转轴同轴的那些圆（外圆/端面外缘/轴颈）半径最大，权重压倒性。
      （偏心小孔的圆半径小、数量也不占优。）
    自检：胜出桶的权重必须占**总量的一半以上**（`min_share`），
      否则认不出主心骨 ⇒ 退回 `(0,0)`，保持原行为（宁可漏判，不可误判）。
    """
    lat = _lat(ax)
    buckets: dict[tuple, float] = {}
    total = 0.0
    for f in faces:
        for m in (f.get("marks") or []):
            c = m.get("c") or [0.0, 0.0, 0.0]
            r = float(m.get("r") or 0.0)
            key = (round(float(c[lat[0]]) / tol), round(float(c[lat[1]]) / tol))
            buckets[key] = buckets.get(key, 0.0) + r * r
            total += r * r
    if not buckets or total <= 0.0:
        return (0.0, 0.0)
    key = max(buckets, key=lambda k: buckets[k])
    if buckets[key] < min_share * total:
        return (0.0, 0.0)
    return (round(key[0] * tol, 4), round(key[1] * tol, 4))


def _issue(issues: list | None, f: dict, why: str) -> None:
    if issues is not None:
        issues.append({"face": _fid(f), "why": why})


def _axis_offset_of(f: dict, ax: int, center: tuple | None = None,
                    use_origin: bool = False) -> float | None:
    """面的**几何中心**离回转轴的距离（mm）。取不到返回 None。

    ★ 为什么不能只靠边界圆：`--no-marks`、或边界圆读不到（面被裁成非圆边界）时
      `marks` 是**空**的 —— 这时偏心的孔照样会漏进轮廓。反光片那两条
      `r=1.5` 的假直线就是这么来的（面是偏心小圆柱，`marks=[]`，
      于是走到 `_axial_extent` 的盒子退路，被当成"母线"）。
    ★ 圆柱面用 `origin_mm`（轴线上的点，`use_origin=True`）；
      平面**不能用** `point_mm` —— 平面参数里的"点"只是平面上的任意参考点，
      偏心平面上照样可能给到轴上，判不出偏心。平面一律用**盒子中心**
      （轴对称的环面必然关于轴居中）。
    """
    o = f.get("origin_mm") if use_origin else None
    if not o:
        bb = f.get("box_mm")
        if not bb:
            return None
        o = [(float(bb[i]) + float(bb[i + 3])) / 2.0 for i in range(3)]
    lat = _lat(ax)
    if center is None:
        return math.hypot(*(float(o[i]) for i in lat))
    return math.hypot(float(o[lat[0]]) - center[0], float(o[lat[1]]) - center[1])


# --------------------------------------------------------------------------- #
# 面 → 元素
# --------------------------------------------------------------------------- #

def face_to_element(f: dict, ax: int, tol: float = 0.05,
                    issues: list | None = None,
                    off_axis: list | None = None,
                    axis_center: tuple | None = None) -> dict | None:
    """把一张面折成一个元素。返回 None 表示这张面不该进轮廓。

    元素形如 `{"kind":"line"|"arc", "a":[x,r], "b":[x,r], …}`。

    ★ 只收**圆心在轴上**的边界圆。偏心圆（不在轴上的孔/球冠/凸台）
    不是回转母线 —— 详见 `_off_axis` 的说明；它们会被记进 `off_axis`
    列表（可以从里面反推出"孔的数目 / 孔径 / 分布圆半径"）。
    ★ `axis_center` 是**回转轴在另两个坐标方向的位置**（见 `detect_axis_center`）——
      传了它，"偏心"才是相对真轴算的。不传 = 相对坐标轴（旧行为）。
    """
    kind = f.get("kind")
    marks = f.get("marks") or []
    on_ax, bad = [], []
    for m in marks:
        (bad if _off_axis(m, ax, axis_center) > tol else on_ax).append(m)
    if bad:
        offs = [_off_axis(m, ax, axis_center) for m in bad]
        _issue(issues, f, f"偏心圆形特征 {len(bad)} 个（圆心离轴 "
                          f"{min(offs):.3f}~{max(offs):.3f} mm）—— 非回转母线")
        if off_axis is not None:
            for m in bad:
                off_axis.append({"face": _fid(f), "kind": kind,
                                 "pitch_r_mm": round(_off_axis(m, ax, axis_center), 4),
                                 "hole_r_mm": round(float(m.get("r") or 0.0), 4),
                                 "x_mm": round(float(m["c"][ax]), 4)})
    xr = sorted((_mark_xr(m, ax) for m in on_ax), key=lambda t: t[0])

    def _note_offface(off: float, why: str) -> None:
        """★ 拒收一张**没有边界圆**的偏心面时，也要把它的规格留下来。

        ★★ 2026-10-02：反光片有 2 张偏心小圆柱面**读不到边界圆**（`marks=[]`），
          它们被轴心偏移判据正确拒收了，但**没有进 `off_axis`** ⇒ 证据里少了
          2 个孔，而 Pappus 体积与实测差 105.7mm³ 里正有它们的一份（约 30mm³）。
          「拒收了但没记下来」等于下次还得再猜一遍 —— 凡是能用盒子估出
          分布圆半径/孔径的，都补一条（`hole_r_mm` 用盒子的径向半宽）。"""
        if off_axis is None:
            return
        bb = f.get("box_mm") or []
        lat = _lat(ax)
        hr = None
        if len(bb) >= 6:
            hr = round(min((float(bb[l + 3]) - float(bb[l])) / 2.0 for l in lat), 4)
        off_axis.append({"face": _fid(f), "kind": kind,
                         "pitch_r_mm": round(off, 4),
                         "hole_r_mm": hr, "x_mm": None,
                         "estimated_from": "box", "why": why})

    if kind == "cyl":
        if _doms(f.get("axis") or [0.0, 0.0, 0.0]) != ax:
            return None                       # 侧壁/横向孔，不是回转母线
        # ★★ 2026-10-02 修的第二个静默缺陷（同心圆守卫只挡住了"半边"）：
        #   偏心圆的守卫只管**边界圆**；**偏心圆柱面**（不在轴上的 Ø4 螺栓孔、
        #   球冠周围的小凸台）在过去仍会被折成一条 `r=<孔径>` 的"母线"混进轮廓。
        #   实测：小盖拼出 3 条 `line (x, 2.0)`、反光片拼出 3 条（其中 2 条 `r=1.5`）
        #   —— 全是**假母线**，而 `revolvable` 只因为它们不在轴心才侥幸判 False。
        #   判据两条，任一成立就拒收：
        #     ① 这张面的边界圆**全是**偏心的（真孔壁）
        #     ② 面的轴线本身离轴 > tol（marks 读不到时的兜底）
        if marks and not on_ax:
            _issue(issues, f, "圆柱面的边界圆全部偏心 ⇒ 这是不在轴上的孔/凸台，"
                              "不是回转母线")
            if not xr:
                _note_offface(_axis_offset_of(f, ax, axis_center, use_origin=True) or 0.0,
                              "边界圆全部偏心（无轴上的圆可参考）")
            return None
        _coff = _axis_offset_of(f, ax, axis_center, use_origin=True)
        if _coff is not None and _coff > max(tol, 0.05):
            _issue(issues, f, f"圆柱轴线离回转轴 {_coff:.3f}mm（> {max(tol, 0.05):g}）"
                              f" ⇒ 不在轴上的孔/凸台，不是回转母线")
            if not marks:
                _note_offface(_coff, "读不到边界圆，轴线偏心（规格由包围盒估算）")
            return None
        r = float(f.get("r_mm") or 0.0)
        if r <= 0.0 and xr:
            r = sum(t[1] for t in xr) / len(xr)
        if r <= 0.0:
            return None
        x0, x1 = _axial_extent(f, ax, xr)
        if x0 is None:
            return None
        return {"kind": "line", "a": [x0, r], "b": [x1, r], "face": _fid(f)}

    if kind == "plane":
        nrm = f.get("normal") or [0.0, 0.0, 0.0]
        if _doms(nrm) != ax or abs(abs(nrm[ax]) - 1.0) > 1e-3:
            return None                       # 不是垂直轴的端面
        # ★★ 2026-10-02：**偏心端面**（孔的沉孔台阶/锪平底面）也要拒收。
        #   它和偏心圆柱面是同一个病的两半：小盖那 3 个 Ø4/Ø6 阶梯孔，
        #   孔壁被拒了、但孔中间的台阶面**照样**折出一条 `line (x=-7.96, r 2→3)`
        #   混进轮廓（实测：小盖因此多 1 个悬空元素、轮廓不闭合）。
        if marks and not on_ax:
            _issue(issues, f, "端面的边界圆全部偏心 ⇒ 这是孔的台阶/沉孔底面，"
                              "不是回转端面")
            return None
        _poff = _axis_offset_of(f, ax, axis_center)
        if _poff is not None and _poff > max(tol, 0.05):
            _issue(issues, f, f"端面离回转轴 {_poff:.3f}mm（> {max(tol, 0.05):g}）"
                              f" ⇒ 不是回转端面")
            return None
        if xr:
            x = sum(t[0] for t in xr) / len(xr)
            rads = [t[1] for t in xr]
            ri, ro = min(rads), max(rads)
            if len(xr) == 1:
                ri = 0.0                      # 整圆盘面：内圈退到轴
        else:
            bb = f.get("box_mm")
            if not bb:
                return None
            x = float((f.get("point_mm") or [0.0, 0.0, 0.0])[ax])
            rr = _radii_from_box_area(f, ax)
            if rr is None:
                return None
            ri, ro = rr
        if abs(ro - ri) < 1e-6:
            return None
        return {"kind": "line", "a": [x, ri], "b": [x, ro], "face": _fid(f)}

    if kind == "cone":
        x0, x1 = _axial_extent(f, ax, xr)
        if x0 is None:
            return None
        if len(xr) >= 2:
            lo, hi = xr[0], xr[-1]
            if hi[0] - lo[0] < 1e-6:
                return None
            return {"kind": "line", "a": [lo[0], lo[1]], "b": [hi[0], hi[1]],
                    "face": _fid(f)}
        rr = _cone_radii_from_area(f, ax, x0, x1)
        if rr is None:
            return None
        return {"kind": "line", "a": [x0, rr[0]], "b": [x1, rr[1]], "face": _fid(f)}

    if kind == "sphere":
        # ★ 球面 = 型面里的一段圆弧（球冠/球带）—— 2026-10-02 补，为反光片。
        #   剖面里的圆心 = (球心的轴向坐标, 球心**离轴**的距离)。
        #   ★ 与环面的区别：环面的圆心离轴 = 主半径（≠0）；球面的圆心在轴上（≈0）。
        #     这条判据同时是个**自检**：球心明显离轴 ⇒ 球冠的轴不是回转轴，退回 blocker。
        sp = (f.get("surf") or {}).get("sphere")
        if not sp or len(sp) < 4:
            return None
        cx = float(sp[ax]) * 1000.0
        off = math.hypot(*[float(sp[i]) * 1000.0 for i in range(3) if i != ax])
        if off > tol:
            _issue(issues, f, f"球面球心离轴 {off:.3f}mm（> tol {tol}）—— "
                              f"球冠的轴不是回转轴，不能当回转母线")
            return None
        R = float(sp[3]) * 1000.0
        # ★★ 2026-10-02：**球冠**（只读到 **1** 个边界圆）要能进轮廓。
        #   反光片的凸/凹两个反光球面、26-A型圆形销两端的球形端头都是这种 ——
        #   以前 `len(xr) < 2` 直接拒收，反光片因此一直建不出来。
        #   极点的轴向位置 = 面的**轴向包围盒里离边界圆更远的那一端**（那里 r=0），
        #   并由球面方程反算 r 做**自检**：算出来不是 0 说明端点定不了，宁可不发射。
        if len(xr) == 1:
            bx = f.get("box_mm") or []
            if len(bx) < 6:
                _issue(issues, f, "球冠只有 1 个边界圆且读不到盒子 ⇒ 端点定不了")
                return None
            x1, r1 = xr[0]
            xs = [float(bx[ax]), float(bx[ax + 3])]
            pole = max(xs, key=lambda x: abs(x - x1))
            r_pole = math.sqrt(max(0.0, R * R - (pole - cx) ** 2))
            if r_pole > max(tol, 0.05):
                _issue(issues, f, f"球冠的轴向极值处算得 r={r_pole:.3f}≠0"
                                  f"（端点不在轴上）⇒ 端点定不了，需人工给")
                return None
            xr = sorted([(x1, r1), (pole, 0.0)], key=lambda t: t[0])
        elif len(xr) < 2:
            return None
        lo, hi = xr[0], xr[-1]
        # ★★ 弧走"短边"还是"长边"**不能只由两个端点定** ——
        #   我第一版拿 `acos` 算夹角并判 >180°，那是**错的**：`acos` 值域就是 [0,180]，
        #   判据永远为假（假绿灯，自检里那条"major_arc"断言会永远过）。
        #   有依据的判据是**面的轴向包围盒**：
        #     弧的内部若经过轴向极值点（θ=0 或 180），它的 |x−cx| 必然大于两端点，
        #     于是 `box[ax]` 的跨度 > 两端点跨度；反之亦然（等价）。
        #     ★ 而任何 >180° 的弧**必然**包含 θ=0 或 θ=180 之一 ⇒ 一定被这个判据抓到。
        #   抓到就**不发射元素**（留 issue），口径同偏心圆守卫：宁可漏判，不可误判。
        bx = f.get("box_mm") or []
        span_ends = abs(hi[0] - lo[0])
        if len(bx) >= 6 and bx[ax] is not None and bx[ax + 3] is not None:
            span_box = abs(float(bx[ax + 3]) - float(bx[ax]))
            if span_box > span_ends + max(tol, 0.05):
                _issue(issues, f, f"球面的轴向包围盒跨度 {span_box:.3f} > 两端点跨度 "
                                  f"{span_ends:.3f} ⇒ 弧内部经过轴向极值，"
                                  f"方向不能只由端点判定（`arc_mid` 会给错半边）—— 需人工给中点")
                return None
        el = {"kind": "arc", "a": [lo[0], lo[1]], "b": [hi[0], hi[1]],
              "center": [round(cx, 4), round(off, 4)], "radius": round(R, 4),
              "face": _fid(f), "arc_monotone": True}
        el["arc_mid"] = [round(v, 4) for v in
                         arc_mid(tuple(el["center"]), el["radius"],
                                 tuple(el["a"]), tuple(el["b"]))]
        return el

    if kind == "torus":
        # ★ 环面 = 型面里的一段圆弧（倒圆/圆角）。端点取两个边界圆；
        #   圆心/半径取 TorusParams（主半径 → 剖面圆心到轴的距离；次半径 → 弧半径）。
        if len(xr) < 2:
            return None
        lo, hi = xr[0], xr[-1]
        el = {"kind": "arc", "a": [lo[0], lo[1]], "b": [hi[0], hi[1]], "face": _fid(f)}
        tp = (f.get("surf") or {}).get("torus")
        if tp:
            el["center"] = [round(float(tp[ax]) * 1000.0, 4),
                            round(float(tp[6]) * 1000.0, 4)]
            el["radius"] = round(float(tp[7]) * 1000.0, 4)
            el["arc_mid"] = [round(v, 4) for v in
                             arc_mid(tuple(el["center"]), el["radius"],
                                     tuple(el["a"]), tuple(el["b"]))]
        return el

    return None


def arc_mid(center: tuple, radius: float, a: tuple, b: tuple) -> tuple:
    """圆上 a→b 的**短弧中点**（供 `arc3p` 的 p3 用）。

    ★ 只能给 ≤180° 的弧。回转件的圆角/倒圆都是 ≤90°，够用；
    真遇到大于半圆的弧必须另判方向。
    """
    va = (a[0] - center[0], a[1] - center[1])
    vb = (b[0] - center[0], b[1] - center[1])
    la = math.hypot(*va) or 1.0
    lb = math.hypot(*vb) or 1.0
    ux, uy = va[0] / la + vb[0] / lb, va[1] / la + vb[1] / lb
    lu = math.hypot(ux, uy)
    if lu < 1e-9:                            # 正好 180°：取垂直方向
        ux, uy = -va[1] / la, va[0] / la
    else:
        ux, uy = ux / lu, uy / lu
    return (center[0] + radius * ux, center[1] + radius * uy)


def _fid(f: dict) -> str:
    return f"{f.get('kind')}|A={f.get('area_mm2')}|box={f.get('box_mm')}"


def _axial_extent(f: dict, ax: int, xr: list) -> tuple:
    """面的轴向起止。优先用边界圆（精确），没有就用盒子。"""
    if len(xr) >= 2:
        return (min(t[0] for t in xr), max(t[0] for t in xr))
    bb = f.get("box_mm")
    if not bb:
        return (None, None)
    return (bb[ax], bb[ax + 3])


def _radii_from_box_area(f: dict, ax: int) -> tuple | None:
    """退路：平面⊥轴的内外半径（外径靠盒子，内径靠面积 `Ri = sqrt(Ro² − A/π)`）。

    ★ 内径靠面积是有条件的：环面被键槽/油孔切过就算错。所以有边界圆时一律走边界圆。
    """
    bb = f.get("box_mm")
    if not bb:
        return None
    lat = [i for i in range(3) if i != ax]
    ro = max((bb[l + 3] - bb[l]) / 2.0 for l in lat)
    area = float(f.get("area_mm2") or 0.0)
    inner2 = ro * ro - area / math.pi
    if inner2 < -1e-9:
        return None
    return (math.sqrt(max(0.0, inner2)), ro)


def _cone_radii_from_area(f: dict, ax: int, x0: float, x1: float) -> tuple | None:
    """退路：圆锥两端半径。外径靠盒子，内径解 `π(a+b)·sqrt(dx²+(a−b)²) = A`。"""
    bb = f.get("box_mm")
    if not bb:
        return None
    lat = [i for i in range(3) if i != ax]
    a = max((bb[l + 3] - bb[l]) / 2.0 for l in lat)
    dx = abs(x1 - x0)
    area = float(f.get("area_mm2") or 0.0)
    if a <= 0.0 or area <= 0.0:
        return None
    best = None
    prev = None
    for i in range(0, 401):
        b = a * i / 400.0
        g = math.pi * (a + b) * math.hypot(dx, a - b) - area
        if prev is not None and prev[1] * g <= 0.0:
            lo_b, lo_g = prev
            for _ in range(60):
                m = 0.5 * (lo_b + b)
                gm = math.pi * (a + m) * math.hypot(dx, a - m) - area
                if lo_g * gm <= 0.0:
                    b, g = m, gm
                else:
                    lo_b, lo_g = m, gm
            best = 0.5 * (lo_b + b)
            break
        prev = (b, g)
    if best is None:
        return None
    return (best, a) if x1 >= x0 else (a, best)


# --------------------------------------------------------------------------- #
# 拼装闭合轮廓
# --------------------------------------------------------------------------- #

def assemble(elements: list[dict], tol: float = 0.05) -> dict:
    """把元素接成一条链。返回 `{chain, closed, closed_via_axis, orphan, n_arcs}`。"""

    def key(p):
        return (int(round(p[0] / tol)), int(round(p[1] / tol)))

    adj: dict = {}
    for i, s in enumerate(elements):
        adj.setdefault(key(s["a"]), []).append(i)
        adj.setdefault(key(s["b"]), []).append(i)

    used: set = set()
    starts = [k for k, v in adj.items() if len(v) == 1]
    cur = starts[0] if starts else next(iter(adj), None)
    chain: list[dict] = []
    guard = 0
    while cur is not None and guard <= len(elements) + 2:
        guard += 1
        cand = [i for i in adj.get(cur, []) if i not in used]
        if not cand:
            break
        i = cand[0]
        used.add(i)
        s = elements[i]
        if key(s["a"]) == cur:
            chain.append({**s, "from": s["a"], "to": s["b"]})
            cur = key(s["b"])
        else:
            chain.append({**s, "from": s["b"], "to": s["a"]})
            cur = key(s["a"])

    orphan = [s for i, s in enumerate(elements) if i not in used]
    first = chain[0]["from"] if chain else None
    last = chain[-1]["to"] if chain else None
    closed = bool(chain) and key(first) == key(last)
    axis_closed = False
    if chain and not closed and abs(first[1]) < tol and abs(last[1]) < tol:
        axis_closed = True
    return {"chain": chain, "closed": closed or axis_closed,
            "closed_via_axis": axis_closed, "orphan": orphan,
            "n_arcs": sum(1 for s in elements if s["kind"] == "arc")}


def rotate_chain_after_arc(chain: list[dict]) -> list[dict]:
    """把链旋转到「从某段圆弧之后开始」—— 这样圆弧落在链尾，便于拼计划。

    建件的步骤序是 `polyline(一串直线) → arc3p(圆弧闭合)`，
    所以链必须从圆弧的**终点**起步。
    """
    if not chain:
        return chain
    for i, s in enumerate(chain):
        if s["kind"] == "arc":
            return chain[i + 1:] + chain[:i + 1]
    return chain


# --------------------------------------------------------------------------- #
# 几何量：面积 / 形心半径 / Pappus 体积
# --------------------------------------------------------------------------- #

def sample_points(chain: list[dict], arc_n: int = 48) -> list[list[float]]:
    """把链摊成密集点列（圆弧按 `arc_n` 段采样），供 shoelace / 画图。

    ★ 直线段也切成若干小段 —— 没必要，但让弧与直线在同一套代码里走，
      少一处分支就少一个错。这里只对圆弧加密。
    """
    pts: list[list[float]] = []
    for s in chain:
        if s["kind"] == "arc" and s.get("center") and s.get("radius"):
            cx, cy = s["center"]
            r = s["radius"]
            pa, pb = s["from"], s["to"]
            ta = math.atan2(pa[1] - cy, pa[0] - cx)
            tb = math.atan2(pb[1] - cy, pb[0] - cx)
            d = tb - ta
            while d > math.pi:
                d -= 2 * math.pi
            while d < -math.pi:
                d += 2 * math.pi
            for k in range(arc_n + 1):
                t = ta + d * k / arc_n
                pts.append([cx + r * math.cos(t), cy + r * math.sin(t)])
        else:
            pts.append(list(s["from"]))
            pts.append(list(s["to"]))
    if pts and abs(pts[0][0] - pts[-1][0]) < 1e-9 and abs(pts[0][1] - pts[-1][1]) < 1e-9:
        pts = pts[:-1]
    return pts


def profile_metrics(points: list[list[float]]) -> dict:
    """Shoelace 面积 + 形心半径 + Pappus 体积。

    ★ **必须回卷闭合**：`range(n-1)` 只走 n−1 条边，对"首尾同点"的闭合点列是够的
      （最后一条边退化），但对**开口链**（还差一张面）会漏掉收口那条边 ——
      19-端盖1 就因此把体积算成 27159（真值 24541，差 11.5%）。
      改成 `j = (i+1) % n` 后两种情况都对。
    """
    n = len(points)
    if n < 3:
        return {"area_mm2": 0.0, "centroid_r_mm": 0.0, "pappus_volume_mm3": 0.0}
    s = 0.0
    cr = 0.0
    for i in range(n):
        x0, r0 = points[i]
        x1, r1 = points[(i + 1) % n]
        cross = x0 * r1 - x1 * r0
        s += cross
        cr += (r0 + r1) * cross
    area = abs(s) / 2.0
    centroid_r = (cr / (6.0 * area)) if area > 1e-12 else 0.0
    return {"area_mm2": round(area, 4),
            "centroid_r_mm": round(centroid_r, 4),
            "pappus_volume_mm3": round(2.0 * math.pi * abs(centroid_r) * area, 4)}


def normalize(points: list[list[float]]) -> list[list[float]]:
    """把轮廓平移到 `x 起点 = 0`（建件时轴线画在 x 轴上，平移不影响形状）。"""
    if not points:
        return []
    x0 = min(p[0] for p in points)
    return [[round(p[0] - x0, 4), round(p[1], 4)] for p in points]


def ascii_plot(points: list[list[float]], width: int = 62, height: int = 20) -> str:
    """把轮廓画成 ASCII 图（终端里一眼看出型面对不对）。"""
    if not points:
        return "(空)"
    xs = [p[0] for p in points]
    rs = [p[1] for p in points]
    x0, x1 = min(xs), max(xs)
    r0, r1 = min(rs), max(rs)
    if x1 - x0 < 1e-9:
        x1 = x0 + 1.0
    if r1 - r0 < 1e-9:
        r1 = r0 + 1.0
    grid = [[" "] * (width + 1) for _ in range(height + 1)]

    def put(x, r, ch):
        cx = max(0, min(width, int(round((x - x0) / (x1 - x0) * width))))
        cy = max(0, min(height, int(round((r1 - r) / (r1 - r0) * height))))
        grid[cy][cx] = ch

    for i in range(len(points) - 1):
        xa, ra = points[i]
        xb, rb = points[i + 1]
        steps = max(2, int(max(abs(xb - xa), abs(rb - ra)) * 3))
        for k in range(steps + 1):
            t = k / steps
            put(xa + (xb - xa) * t, ra + (rb - ra) * t, "#")
    for v in points:
        put(v[0], v[1], "o")
    lines = ["".join(row) for row in grid]
    head = f"  轴向 x ∈ [{x0:g}, {x1:g}]   半径 r ∈ [{r0:g}, {r1:g}]"
    return head + "\n" + "\n".join(lines)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def extract(rec: dict, ax: int, tol: float = 0.05,
            axis_center: tuple | None = None) -> dict:
    """faces(带 marks/surf) → 元素 → 链 → 几何量。

    除了轮廓本身，还回一份**可判定性**（`revolvable` / `blockers`）：
    只有「轮廓闭合 + 没有偏心特征 + 没有没处理的面 + 没有悬空元素」
    才敢说"这是一个纯回转体，可以照着回转建"。
    否则把原因列进 `blockers` —— 宁可判"不可回转"，也不要静默给个假轮廓。

    ★★ `axis_center`：回转轴在另两个坐标方向的位置。不给就自动侦测
      （见 `detect_axis_center`）—— 因为**不是所有参照件的轴都过原点**
      （16-螺塞 的轴在 y=1.5），拿坐标轴当真轴会把整件误判成"不是回转体"。
    """
    faces = rec.get("faces") or []
    if axis_center is None:
        axis_center = detect_axis_center(faces, ax, tol=tol)
    elements: list[dict] = []
    unhandled: list[dict] = []
    issues: list[dict] = []
    off_axis: list[dict] = []
    for f in faces:
        with contextlib.suppress(Exception):
            el = face_to_element(f, ax, tol=tol, issues=issues, off_axis=off_axis,
                                 axis_center=axis_center)
            if el is not None:
                elements.append(el)
            elif f.get("kind") in ("cyl", "plane", "cone", "torus", "sphere"):
                unhandled.append({"why": "不进轮廓（非回转母线/退化）", "face": _fid(f),
                                  "marks": f.get("marks") or []})
            else:
                unhandled.append({"why": f"面类型 {f.get('kind')} 未支持", "face": _fid(f),
                                  "marks": f.get("marks") or []})
    uniq: list[dict] = []
    for s in elements:
        sig = (round(s["a"][0], 3), round(s["a"][1], 3),
               round(s["b"][0], 3), round(s["b"][1], 3))
        sig2 = (sig[2], sig[3], sig[0], sig[1])
        if any(u["_sig"] in (sig, sig2) for u in uniq):
            continue
        s["_sig"] = sig
        uniq.append(s)
    asm = assemble(uniq, tol=tol)
    chain = rotate_chain_after_arc(asm["chain"]) if asm["closed"] else asm["chain"]
    pts = sample_points(chain)
    met = profile_metrics(pts)

    blockers: list[str] = []
    if not asm["closed"]:
        blockers.append(f"轮廓不闭合（{len(asm.get('orphan') or [])} 个悬空元素）")
    if off_axis:
        pitches = sorted({round(o["pitch_r_mm"], 3) for o in off_axis})
        blockers.append(f"存在偏心特征 {len(off_axis)} 处（分布圆半径 {pitches}）"
                        f" —— 不是纯回转体，需另加孔/阵列")
    if unhandled:
        kinds = sorted({u["why"] for u in unhandled})
        blockers.append(f"有 {len(unhandled)} 张面没进轮廓（{kinds}）")
    return {"elements": uniq, "unhandled": unhandled, "issues": issues,
            "off_axis": off_axis, "blockers": blockers,
            "revolvable": not blockers,
            "chain": chain, "points": pts,
            **{k: v for k, v in asm.items() if k != "chain"}, **met}


def _apply_drift(res: dict, drift: list | None) -> dict:
    """指纹漂移 ⇒ **作废可判定性**。

    ★★ 2026-10-02：读一份 235 面的参照件时，别人的 `零件26`
      （底盘板/履带左/履带右）抢走了活动文档 ⇒ 包围盒从 35×23×23 变成
      146.08×72.30×16.00，而 `GetTitle` 还写着 `10-通气塞`。
      ⇒ 宁可判"不可用"，也绝不把别的零件的轮廓当成这一件的。
      （拆成函数是为了**可离线测试** —— 这条判据不能在真机才验得到。）
    """
    if drift:
        res["revolvable"] = False
        res.setdefault("blockers", []).insert(
            0, "★ 读面期间活动文档指纹变了（读数可能来自另一份文件），本次结论作废："
               + "; ".join(drift))
    return res


def plan_hint(chain: list[dict]) -> list[dict]:
    """轮廓 → 建件步骤提示（`polyline` 串直线 + `arc3p` 补圆弧）。

    ★ 三个现场踩出来的细节：
      ① 步骤序固定为「**一串直线 → 圆弧**」，所以 `chain` 必须先
         `rotate_chain_after_arc`（圆弧落在链尾才能用它闭合）。
      ② **一段直线串若首尾同点，必须去掉尾点并改用 `close=True`** ——
         否则 `op_polyline(close=False)` 会多画一条**零长直线**，
         SolidWorks 的 CreateLine 对重合点返回 None，`op_polyline` 随即
         以"折线只画出 n−1/n 段"报错（28-端盖3 就是纯直线闭环这一型）。
      ③ 直线串与圆弧必须**首尾严格相接**，否则 SW 不认成一个轮廓。
    """
    out: list[dict] = []
    runs: list[list[list[float]]] = []
    run: list[list[float]] = []
    for s in chain:
        if s["kind"] == "arc":
            if len(run) >= 2:
                runs.append(run)
            run = []
        else:
            if not run:
                run.append(list(s["from"]))
            run.append(list(s["to"]))
    if len(run) >= 2:
        runs.append(run)

    def emit(r: list[list[float]]) -> dict:
        same = (abs(r[0][0] - r[-1][0]) < 1e-9 and abs(r[0][1] - r[-1][1]) < 1e-9)
        pts = r[:-1] if same else r
        return {"op": "polyline", "points": pts, "close": same}

    ri = 0
    for s in chain:
        if s["kind"] == "arc":
            if ri < len(runs):
                out.append(emit(runs[ri]))
                ri += 1
            out.append({"op": "arc3p", "p1": s["from"], "p2": s["to"],
                        "p3": s.get("arc_mid"), "radius": s.get("radius"),
                        "center": s.get("center")})
    if ri < len(runs):
        out.append(emit(runs[ri]))
    return out


def collect(sw, read_marks: bool = True) -> dict:
    """当前文档 → faces（含 marks / surf）。"""
    faces = sw_faces.read_faces(sw)
    if read_marks:
        mm = _read_all_face_marks(sw)
        for i, f in enumerate(faces):
            if i < len(mm):
                f["marks"], f["surf"] = mm[i]
            else:
                f["marks"], f["surf"] = [], {}
    return {"faces": faces, "n_faces": len(faces)}


def _read_all_face_marks(sw) -> list[tuple]:
    """与 read_faces 同序地再遍历一遍面，取边界圆 + 面参数。

    ★ 取实体必须与 `sw_faces.read_faces` **用同一个入口**（`sw.bodies()`），
      否则两边遍历的实体集合不同 ⇒ 面序错位 ⇒ 标记整体串台（静默错）。
    """
    out: list[tuple] = []
    for b in sw.bodies():
        body = typed(b, "IBody2")
        f = member(body, "GetFirstFace")
        n = 0
        while f is not None and n < 9000:
            n += 1
            f2 = typed(f, "IFace2")
            out.append(face_marks(f2))
            with contextlib.suppress(Exception):
                f = member(f2, "GetNextFace")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="参照件型面提取：从面的边界圆反推回转半剖面元素（只读）")
    ap.add_argument("--doc", help="参照件 .SLDPRT 路径（单个）")
    ap.add_argument("--docs", nargs="+", help="多个参照件路径（复用一次会话）")
    ap.add_argument("--dir", help="扫这个目录下的 *.SLDPRT（跳过 ~$ 锁文件）")
    ap.add_argument("--json", help="把结果写到这个 json（仅单件时有效）")
    ap.add_argument("--json-dir", help="批量时每件写一个 <件名>.profile.json 到这个目录")
    ap.add_argument("--axis", type=int, choices=[0, 1, 2], help="回转轴（默认自动投票）")
    ap.add_argument("--expect-volume", type=float, help="参照件实测体积，用来校 Pappus 体积")
    ap.add_argument("--no-marks", action="store_true", help="不读边界圆，只用盒子+面积")
    ap.add_argument("--visible", action="store_true")
    args = ap.parse_args(argv)

    docs: list[Path] = []
    if args.doc:
        docs.append(Path(args.doc))
    if args.docs:
        docs.extend(Path(d) for d in args.docs)
    if args.dir:
        root = Path(args.dir)
        if not root.is_dir():
            print(f"目录不存在：{root}", file=sys.stderr)
            return 2
        docs.extend(sorted(p for p in root.glob("*.SLDPRT")
                           if not p.name.startswith("~$")))
    seen: set[str] = set()
    uniq: list[Path] = []
    for d in docs:
        k = str(d.resolve()).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    docs = uniq
    if not docs:
        print("没给参照件：用 --doc / --docs / --dir 指定", file=sys.stderr)
        return 2
    if len(docs) > 1 and args.json:
        print("★ 多个参照件时 --json 忽略，改走 --json-dir（自动按件命名）",
              file=sys.stderr)

    rc = 0
    with sw_connect.SwSession.open(visible=args.visible) as sw:
        for i, d in enumerate(docs, 1):
            if len(docs) > 1:
                print(f"\n{'#' * 20} [{i}/{len(docs)}] {d.name}")
            out = None
            if args.json_dir:
                out = Path(args.json_dir) / (d.stem + ".profile.json")
            elif len(docs) == 1 and args.json:
                out = Path(args.json)
            rc = _one(sw, d, args, out) or rc
    return rc


def _one(sw, doc: Path, args, json_out: Path | None = None) -> int:
    """在一个**已连接**的会话里提取单个参照件的型面（批量时复用会话，省启停）。"""
    if not doc.exists():
        print(f"找不到文件：{doc}", file=sys.stderr)
        return 2

    sw.open_doc(str(doc), read_only=True)
    try:
        sw.set_units_mm()
        # ★★ 2026-10-02 真机踩到，**顺序很要命**：先读"便宜且不碰模型"的量，
        #   再读面。原因：`16-螺塞` 与 `10-通气塞` 在**读完整份面表之后**，
        #   `GetBodies2` 三档全回 0、`bounding_box()` 变 None、质量回 0 ——
        #   看起来像"零件是空的"，其实打开那一刻它们分别是 1 个实体、
        #   bbox 17.78×14×15 / 35×23×23、体积 1659.8 / 6712.6 mm³。
        #   同批的 6-小盖 / 3-反光片 / 26-圆形销 **不受影响**；坏的两件都有
        #   **真实螺纹**（B 样条面）。机制还没定论，但"先把真值拿到手"永远成立。
        bbox_open = sw.bounding_box()
        mass_open = sw.mass_props()
        ident0 = sw.doc_identity()
        rec = collect(sw, read_marks=not args.no_marks)
        drift = sw.identity_diff(ident0, sw.doc_identity())
        rec["identity_open"] = ident0
        if drift:
            rec["drift"] = drift
        bb = bbox_open or sw.bounding_box()
        if bb:
            rec["bbox_mm"] = [round((bb[i + 3] - bb[i]) * 1000.0, 3) for i in range(3)]
        rec["mass_props"] = mass_open
        if not bbox_open and sw.bounding_box() is None:
            rec.setdefault("warnings", []).append(
                "★ 读面之后包围盒就读不到了（会话被读进坏状态）—— "
                "本件的 bbox/质量取自**打开那一刻**，可信")
        ax = args.axis if args.axis is not None else detect_axis(rec["faces"])
        rec["axis"] = ax
        rec["axis_center_mm"] = list(detect_axis_center(rec["faces"], ax))
    finally:
        with contextlib.suppress(Exception):
            sw.close_doc(save=False)

    res = extract(rec, ax)
    # ★★ 指纹漂移 ⇒ 见 `_apply_drift`
    _apply_drift(res, rec.get("drift"))
    name_ax = "XYZ"[ax]

    print("=" * 74)
    print(f"{doc.name}   bbox={rec.get('bbox_mm')}   回转轴={name_ax}   面数={rec.get('n_faces')}")
    _ac = rec.get("axis_center_mm") or [0.0, 0.0]
    if abs(_ac[0]) > 1e-6 or abs(_ac[1]) > 1e-6:
        print(f"★ 回转轴**不过原点**：轴落在另两坐标 {_ac} mm —— "
              f"偏心判据是相对这条轴算的（以前会把整件误判成「不是回转体」）")
    for _w in rec.get("warnings") or []:
        print(f"⚠ {_w}")
    n_m = sum(1 for f in rec["faces"] if f.get("marks"))
    print(f"边界圆可读的面：{n_m}/{rec.get('n_faces')}"
          + ("（全部走边界圆）" if n_m == rec.get("n_faces") else "（其余退回盒子+面积）"))
    # ★ 可判定性先说：不能回转就别往下照着建
    if res.get("revolvable"):
        print(f"可判定性：**纯回转体** ✔（轮廓闭合、面全用上、无偏心特征）")
    else:
        print(f"可判定性：**不是纯回转体** ✘ —— 别照这份轮廓建件")
        for b in res.get("blockers") or []:
            print(f"   · {b}")
    print(f"-- 元素  {len(res['elements'])} 个（圆弧 {res['n_arcs']} 段）")
    for s in res["elements"]:
        tag = ""
        if s["kind"] == "arc":
            tag = f"   圆心={s.get('center')} R={s.get('radius')}"
        print(f"   {s['kind']:<5}  ({s['a'][0]:>8.3f}, {s['a'][1]:>8.3f})"
              f" -> ({s['b'][0]:>8.3f}, {s['b'][1]:>8.3f}){tag}")
    print(f"-- 链  {len(res['chain'])} 段   闭合={res['closed']}"
          + ("（经轴闭合）" if res.get("closed_via_axis") else ""))
    print(ascii_plot(res["points"]))
    print(f"-- 几何量  面积={res['area_mm2']} mm²  形心半径={res['centroid_r_mm']} mm"
          f"  Pappus 体积={res['pappus_volume_mm3']} mm³")
    if args.expect_volume:
        got, exp = res["pappus_volume_mm3"], args.expect_volume
        err = abs(got - exp) / exp * 100.0 if exp else 0.0
        print(f"   与参照件体积 {exp} 比：{err:.4f}%  [{'OK' if err <= 2.0 else 'FAIL'}]")
    print("-- 建件步骤提示：")
    for h in plan_hint(res["chain"]):
        if h["op"] == "polyline":
            print(f"   polyline  {len(h['points'])} 点  {h['points'][:3]} … {h['points'][-1]}")
        else:
            print(f"   arc3p     p1={h['p1']} p2={h['p2']} p3={h['p3']} R={h['radius']}")
    if res["orphan"]:
        print(f"-- 悬空元素 {len(res['orphan'])} 个（没接进链，需人工看）")
        for s in res["orphan"]:
            print(f"   {s['kind']} ({s['a']}) -> ({s['b']})")
    if res["unhandled"]:
        print(f"-- 未进轮廓的面 {len(res['unhandled'])} 张：")
        for u in res["unhandled"]:
            print(f"   {u['why']}   {u['face']}")
            for m in u.get("marks") or []:
                print(f"        边界圆: x={m['c'][ax]:>9.3f}  r={m['r']:>8.3f}")

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        slim = {
            "doc": str(doc), "bbox_mm": rec.get("bbox_mm"), "axis": ax,
            # ★ 回转轴在另两个坐标方向的位置（不是所有参照件的轴都过原点，
            #   16-螺塞 就在 y=1.5）—— 判"偏不偏心"是相对它算的，必须留证据
            "axis_center_mm": rec.get("axis_center_mm"),
            "mass_props": rec.get("mass_props"),
            "warnings": rec.get("warnings") or [],
            "identity_open": rec.get("identity_open"),
            "drift": rec.get("drift") or [],
            "n_faces": rec.get("n_faces"), "n_faces_with_marks": n_m,
            # ★ 可判定性：revolvable=False 就别照这份轮廓建件（见 blockers）
            "revolvable": res.get("revolvable"),
            "blockers": res.get("blockers") or [],
            "off_axis": res.get("off_axis") or [],
            "issues": res.get("issues") or [],
            "elements": [{k: v for k, v in s.items() if not k.startswith("_")}
                         for s in res["elements"]],
            "chain": [{k: v for k, v in s.items() if not k.startswith("_")}
                      for s in res["chain"]],
            "profile_points": res["points"],
            "profile_points_normalized": normalize(res["points"]),
            "plan_hint": plan_hint(res["chain"]),
            "n_arcs": res["n_arcs"],
            "closed": res["closed"], "closed_via_axis": res.get("closed_via_axis"),
            "area_mm2": res["area_mm2"], "centroid_r_mm": res["centroid_r_mm"],
            "pappus_volume_mm3": res["pappus_volume_mm3"],
            "expect_volume_mm3": args.expect_volume,
            "orphan": [{k: v for k, v in s.items() if not k.startswith("_")}
                       for s in res["orphan"]],
            "unhandled": res["unhandled"],
        }
        json_out.write_text(json.dumps(slim, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        print(f"\n已写出 {json_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
