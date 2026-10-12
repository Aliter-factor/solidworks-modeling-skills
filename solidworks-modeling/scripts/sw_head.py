#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_head.py —— CRH380CL 车头曲面：把**参数化截面族**翻译成建模计划。

这是 `references/80-crh380cl-project.md` §4.2 的落地：

    不手工画草图，而是用**参数方程**生成截面 —— 这是能"改一个数全套重放"的前提。

    母截面 profile(t)：t∈[0,1] 绕断面走一圈，给出 (y, z)（x = 车长方向）；
    调制函数 shape(u)：u∈[0,1]，控制每个站位相对母截面的缩放/压扁/方度。
    第 i 站截面 = profile(t) ⊙ shape(u_i)  ⇒ 每站同样的 N 个点、**起点必然在同一侧**。

为什么必须参数化（R2 风险的正解）：
    放样要求所有截面**图元数一致、起点对齐**。手工画 N 条截面，起点一定对不齐
    ⇒ 放样出来是**扭的**，而且 **SolidWorks 不报错**（`StartMatchingType` 也救不回来）。

用法
----
    python scripts/sw_head.py --stage s2 --out assets/crh380cl/plan_s2.json
    python scripts/sw_head.py --stage s3 --out assets/crh380cl/plan_s3.json
    python scripts/sw_head.py --stage s2 --summary        # 只看数值，不写文件

    # 然后照常校验 / 执行 / 出结论
    python scripts/sw_plan.py assets/crh380cl/plan_s2.json --check
    python scripts/sw_plan.py assets/crh380cl/plan_s2.json --dry-run
    python scripts/sw_plan.py assets/crh380cl/plan_s2.json --run --report D:/out/crh_s2.json

★ 判据（写进计划，不在这里判）：放样后**单一曲面体**（n_sheet=1 / n_solid=0）、
  总面积 > 0、末端断面 = 车体断面（宽 3265 / 高 3890）。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import sw_env  # noqa: E402  —— 产出目录解析（YOYO_SW_OUT 优先）

DEFAULT_PARAMS = ROOT / "assets" / "crh380cl" / "crh380cl.params.json"

# 分阶段：S2 是"最小单曲面片"（3 截面 + 2 引导线），S3 是"全截面放样"。
# S4 是**收口**：把开放的放样曲面变成实体 —— 两条独立路线：
#   · s4_knit  ：鼻端/尾端各封一张**平面盖**（`surface_planar`）→ `surface_knit` 缝成**实体**；
#   · s4_shell ：直接 `thicken` 成**薄壳实体**（不封口，加厚自带边界侧壁）。
# S5 是**细节**：在曲面壳上开风挡 / 侧窗开口 —— 换路达成（`thicken` → `extrude_cut`），
#   原 `IFeatureManager.PreTrimSurface` 剪裁在 SW2024 下走不通（见 30-pitfalls §135④）。
STAGES = {
    # ★ s2 只有 3 站位 ⇒ 引导线只有 2 段；截面点 144 ⇒ 每段要 36 点才满足 selfcheck ③。
    "s2": {"n_stations": 3, "guides_deg": [90.0, 270.0], "tag": "nose_s2", "close": None,
           "subdiv": 36},
    "s3": {"n_stations": 13, "guides_deg": [90.0, 270.0], "tag": "nose_s3", "close": None},
    "s4_knit": {"n_stations": 13, "guides_deg": [90.0, 270.0], "tag": "nose_s4_solid",
                "close": "knit"},
    "s4_shell": {"n_stations": 13, "guides_deg": [90.0, 270.0], "tag": "nose_s4_shell",
                 "close": "thicken"},
    "s5": {"n_stations": 13, "guides_deg": [90.0, 270.0], "tag": "nose_s5",
           "close": "windows"},
}


# ---------------------------------------------------------------- 数学

# ---- 单调三次插值（PCHIP / Fritsch–Carlson）----
# ★ 为什么不用线性或普通三次样条：轮廓控制点（顶线 / 半宽 / 底线）都**单调**，
#   普通三次样条会在控制点之间**过冲**（鼓包），线性插值又留下折角 ⇒ PCHIP 两头都避开。
def _pchip_slopes(xs, ys):
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        if d[i - 1] * d[i] <= 0.0:
            m[i] = 0.0
        else:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    return h, m


class _Profile:
    """沿 X 的一维轮廓（PCHIP）。`at(x)`：端点之外取端点值。"""

    def __init__(self, table):
        self.xs = [float(p[0]) for p in table]
        self.ys = [float(p[1]) for p in table]
        self.h, self.m = _pchip_slopes(self.xs, self.ys)

    def at(self, x: float) -> float:
        xs, ys, h, m = self.xs, self.ys, self.h, self.m
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        lo, hi = 0, len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= x:
                lo = mid
            else:
                hi = mid
        i = lo
        t = (x - xs[i]) / h[i]
        t2, t3 = t * t, t * t * t
        return ((2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h[i] * m[i]
                + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h[i] * m[i + 1])


# ---- 断面形状：**归一化形状模板** R(phi) ----
# ★ v1.4.58 把「超椭圆 |y/a|^n+|z/b|^n=1」换成**一般断面形状**：
#   车体断面按用户给的 `3.txt` 控制点（**竖壁到 3300 + 肩部弧 + 平底 110**）——
#   超椭圆**做不出「竖壁 + 肩部 + 平底」**（它上下对称、底部收成一个点）。
#   做法：把车体断面**归一化**（Y=y/a、Z=(z-zc)/b）后，从中心做**射线**求 R(phi)，
#   得到一张「角度 -> 半径」表；鼻锥各站 = 这张表按 (半宽 a、半高 b、圆心 zc) 仿射缩放。
#   鼻尖附近再与**椭圆**（归一化圆，R恒为1）混合 ⇒ 前端圆润、后端接车体形状。

def _ray_poly_radius(poly, dx, dy):
    """从原点沿单位方向 (dx,dy) 的射线，与闭合多边形边界的**最近正交点**距离。"""
    best = None
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        ex, ey = x2 - x1, y2 - y1
        den = dx * ey - dy * ex
        if abs(den) < 1e-15:
            continue
        t = (x1 * ey - y1 * ex) / den
        s = (x1 * dy - y1 * dx) / den
        if t > 1e-12 and -1e-9 <= s <= 1.0 + 1e-9:
            if best is None or t < best:
                best = t
    return best if best is not None else 0.0


def _build_shape(body, m: int = 360):
    """车体断面的**归一化径向表** R(phi)（phi 均匀取 m 个）。"""
    a = float(body["half_width"])
    b = float(body["half_height"])
    zc = float(body["center_z"])
    poly = [(float(y) / a, (float(z) - zc) / b) for (y, z) in body["outline"]]
    return [_ray_poly_radius(poly, math.cos(2.0 * math.pi * k / m),
                             math.sin(2.0 * math.pi * k / m)) for k in range(m)]


def _rho(phi: float, shape) -> float:
    """周期性线性插值取 R(phi)。"""
    m = len(shape)
    t = (phi % (2.0 * math.pi)) / (2.0 * math.pi) * m
    i = int(t) % m
    f = t - math.floor(t)
    return shape[i] * (1.0 - f) + shape[(i + 1) % m] * f


def prepare(params: dict) -> dict:
    """把参数表编译成**可快速求值**的形状（幂等：已备则原样返回）。"""
    nose = params["nose"]
    if "_prep" in nose:
        return nose["_prep"]
    pr = nose["profiles"]
    body = dict(params["body"])
    # ★ 可选：把断面多边形的角倒圆（`outline_corner_r`，默认 0 = 忠实用户给的折线）。
    _r = float(body.get("outline_corner_r", 0.0) or 0.0)
    if _r > 0:
        body["outline"] = round_poly(
            [(float(y), float(z)) for (y, z) in body["outline"]], _r, seg=12,
            min_turn_deg=float(body.get("outline_corner_min_turn_deg", 80.0)))
    zv = [float(p[1]) for p in body["outline"]]
    z_lo, z_hi = min(zv), max(zv)
    body["half_width"] = float(body["width"]) / 2.0
    body["half_height"] = (z_hi - z_lo) / 2.0
    body["center_z"] = (z_hi + z_lo) / 2.0
    shape = _build_shape(body)
    # 混合断面的二次型系数：A(blend) = 0.5*a*b*(2pi + 2*blend*G1 + blend^2*G2)
    mm = 2000
    g1 = g2 = 0.0
    for k in range(mm):
        g = _rho((k + 0.5) * 2.0 * math.pi / mm, shape) - 1.0
        g1 += g
        g2 += g * g
    top_p = _Profile(pr["top"])
    bot_p = _Profile(pr["bottom"])
    wid_p = _Profile(pr["half_width"])
    nose["_prep"] = {
        "top": top_p,
        "bottom": bot_p,
        "width": wid_p,
        "shape": shape,
        "blend_power": float(nose.get("shape_blend_power", 0.55)),
        "g1": 2.0 * math.pi / mm * g1,
        "g2": 2.0 * math.pi / mm * g2,
        "tip_min": float(nose.get("tip_min_mm", 8.0)),
        # ★ 鼻尖「圆收缩」段长（0 = 关闭 ⇒ 逐位回到旧的线性收敛尖锥）
        "tip_round_len": float(nose.get("tip_round_len_mm", 0.0) or 0.0),
        # ★ 圆帽的顶点高度（默认取 top/bottom 在 x=0 的重合点）
        "tip_z": float(nose.get("tip_z_mm", 0.5 * (top_p.at(0.0) + bot_p.at(0.0)))),
        "body": body,
    }
    # ★ 截面 θ 采样表（均匀 + 尖角加密）—— 放在最后算，因为它要读 `_prep["body"]`。
    nose["_prep"]["thetas"] = _theta_list(nose)
    nose["_prep"]["sec_n"] = len(nose["_prep"]["thetas"])
    return nose["_prep"]


def _state(u: float, nose: dict):
    """第 u 站的形状参数：x, 半宽 a, 半高 b, 圆心高度 zc, 形状混合系数 blend。

    ★★★ 鼻尖「**圆收缩**」（2026-10-12，用户反馈「会不会太尖了？应该有个圆收缩吧」）：
      原设计 top / bottom / half_width 三条一维轮廓都**线性地**收敛到 (x=0, z=1000)，
      于是断面尺寸沿 x **线性增长** ⇒ 鼻尖是一个**尖锥（刀锋）**。真车鼻尖是**圆**的。
      做法（`tip_round_len_mm` = 圆收缩段长 xc，默认 1500；xc=0 ⇒ 关闭、逐位等价旧行为）：
        s = x / xc,   q(s) = √(1 − (1−s)²)        ← 1/4 椭圆的「圆帽」
                      q(0)=0 且 **q'(0)=∞** ⇒ 鼻尖切线**竖直**（曲率有限 ⇒ 圆头）
        圆帽三条：      a_c = a(xc)·q
                      zt_c = tz + (zt(xc) − tz)·q        tz = 鼻尖点高度
                      zb_c = tz − (tz − zb(xc))·q
        平滑权重：      w = s²(3 − 2s)             ← w(1)=1、**w'(1)=0**
        x < xc：        (参数) = (1−w)·圆帽 + w·原轮廓
        x ≥ xc：        原轮廓，不动
      ⇒ 鼻尖是圆头；且在 x = xc 处与原设计**值 + 斜率都连续**（不会多出一道折痕）。
      ★ 与 `tip_min_mm` 的关系：x→0 时 a→0，仍由 tip_min 兜成 Ø16 的小断面
        （放样不能从退化点起步）；圆帽只决定「怎么收」，不决定「收成多大」。
    """
    p = nose["_prep"]
    tmin = p["tip_min"]
    x = u * float(nose["length"])
    a = p["width"].at(x)
    zt = p["top"].at(x)
    zb = p["bottom"].at(x)
    xc = float(p.get("tip_round_len", 0.0) or 0.0)
    if xc > 1e-9 and x < xc:
        tz = float(p["tip_z"])
        s = x / xc
        q = math.sqrt(max(0.0, 1.0 - (1.0 - s) ** 2))
        w = s * s * (3.0 - 2.0 * s)
        a_c = p["width"].at(xc) * q
        zt_c = tz + (p["top"].at(xc) - tz) * q
        zb_c = tz - (tz - p["bottom"].at(xc)) * q
        a = (1.0 - w) * a_c + w * a
        zt = (1.0 - w) * zt_c + w * zt
        zb = (1.0 - w) * zb_c + w * zb
    a = max(a, tmin)
    zc = 0.5 * (zt + zb)
    b = max(0.5 * (zt - zb), tmin)
    return x, a, b, zc, u ** p["blend_power"]


def _sec_yz(phi: float, a: float, b: float, shape, blend: float):
    """断面在 (y, z-zc) 平面内的点。blend=0 => 椭圆；blend=1 => 车体形状。"""
    r = (1.0 - blend) + blend * _rho(phi, shape)
    return a * r * math.cos(phi), b * r * math.sin(phi)


def _section_area(a: float, b: float, g1: float, g2: float, blend: float) -> float:
    """断面面积 = 0.5*a*b*∮r(phi)^2 dphi = 0.5*a*b*(2pi + 2*blend*G1 + blend^2*G2)。"""
    return 0.5 * a * b * (2.0 * math.pi + 2.0 * blend * g1 + blend * blend * g2)


# ---------------------------------------------------------------- 点列

def _theta_list(nose: dict) -> list[float]:
    """截面的 θ 采样表（弧度、升序）—— **在断面多边形的尖角附近加密**。

    ★★★ 2026-10-12 真机确诊：截面曲线是 `InsertCurveFile` 的**过点样条**。若相邻两个
      采样点之间夹着一个多边形**尖角**（方向突变），样条会在那里**向外过冲**。实测
      车体断面底部 90° 直角（归一化 θ≈315°，相邻采样 310°/320°）：
        · 36 点均匀 ⇒ 放样面半宽 **1654.1**（设计 1628.5，多 **25.6mm/侧**）、底掉到
          **84.5**（设计 110，低 25.5）；S4_knit 的 bbox 判据就此报 +51.2 / +25.5；
        · **3 截面与 13 截面逐位相同** ⇒ 过冲来自「截面曲线自身」，**不是**站间插值；
        · 把 θ 均匀加密到 144 点只降到 14.4mm/侧，却把 `thicken` 试挂（尖头截面 Ø16 ×
          144 点 ⇒ 点距 0.35mm ⇒ 放样面在鼻尖附近畸变 ⇒ 偏移自交，复现 2/2）；
        · 只把尖角**几何倒圆**（R250）**完全无效** —— 该圆弧在极角上只跨 8.7°，
          而采样步长 10° ⇒ **一个采样点都落不到弧上**。
      ⇒ 唯一有效的解法 = **只在尖角处补点**：36 → 48 点，尖头点距仅 1.4→1.05mm。
    """
    m = int(nose["section_points"])
    base = [2.0 * math.pi * k / m for k in range(m)]
    prep = nose.get("_prep") or {}
    body = prep.get("body") or {}
    ol = body.get("outline") or []
    if len(ol) < 3:
        return base
    a = float(body["half_width"])
    b = float(body["half_height"])
    zc = float(body["center_z"])
    poly = [(float(y) / a, (float(z) - zc) / b) for (y, z) in ol]
    min_turn = math.radians(float(nose.get("corner_min_turn_deg", 80.0)))
    span = math.radians(float(nose.get("corner_span_deg", 9.0)))
    k_extra = max(int(nose.get("corner_extra", 6)), 1)
    n = len(poly)
    thetas = list(base)
    for i in range(n):
        pv, pc, pn = poly[(i - 1) % n], poly[i], poly[(i + 1) % n]
        u1 = _unit2(pv[0] - pc[0], pv[1] - pc[1])
        u2 = _unit2(pn[0] - pc[0], pn[1] - pc[1])
        cosphi = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        phi = math.acos(cosphi)
        if math.pi - phi < min_turn:            # 只补尖角，跳过接近平角的角
            continue
        th = math.atan2(pc[1], pc[0])
        for k in range(k_extra):
            thetas.append((th - span + 2.0 * span * k / (k_extra - 1)) % (2.0 * math.pi))
    thetas.sort()
    out: list[float] = []
    for t in thetas:                            # 去重（<0.5° 合并）
        if not out or (t - out[-1]) > math.radians(0.5):
            out.append(t)
    return out


def section_points(u: float, nose: dict) -> list[list[float]]:
    """第 u 站的**截面线**（绕断面一圈，开口列表；闭合由 op 的 closed=True 补首点）。

    ★ 起点固定在 theta=0（断面右侧 (a, zc)）—— 所有站同侧 ⇒ 放样不扭。
    ★ θ 表来自 `_theta_list`（均匀 + 尖角加密），**与 u 无关** ⇒ 各站点数一致。
    """
    x, a, b, zc, blend = _state(u, nose)
    shape = nose["_prep"]["shape"]
    ths = nose["_prep"].get("thetas") or _theta_list(nose)
    pts = []
    for th in ths:
        y, zr = _sec_yz(th, a, b, shape, blend)
        pts.append([round(x, 6), round(y, 6), round(zc + zr, 6)])
    return pts


def guide_u_list(nose: dict, n_stations: int, subdiv=None) -> list[float]:
    """引导线的 u 取值表：**每个放样段再细分 subdiv 份**。

    ★★★ 两条硬约束（2026-10-11 真机诊断出来的）：
      ① **每个截面站位都必须落一个引导线点** —— 否则引导线样条在该截面处与
         截面曲线**不相交**，SolidWorks 找不到「引导线 ∩ 截面」就直接**不建特征、
         不报错**（静默失败）；
      ② **点数不能太稀** —— 8 点的引导线即使端点正确也会失败；加密到与截面同量级即成功。
    """
    sd = int(subdiv if subdiv is not None else nose["guides"].get("subdiv", 8))
    sd = max(sd, 1)
    us = []
    for i in range(n_stations - 1):
        a, b = i / (n_stations - 1), (i + 1) / (n_stations - 1)
        for k in range(sd):
            us.append(a + (b - a) * k / sd)
    us.append(1.0)
    return us


def guide_points(theta_deg: float, nose: dict, us=None, n_stations: int = 0):
    """一条**纵向引导线**：固定极角 theta、沿给定 u 表取点（开口）。"""
    if us is None:
        us = guide_u_list(nose, n_stations or int(nose.get("stations", 3)))
    th = math.radians(float(theta_deg))
    shape = nose["_prep"]["shape"]
    pts = []
    for u in us:
        x, a, b, zc, blend = _state(u, nose)
        y, zr = _sec_yz(th, a, b, shape, blend)
        pts.append([round(x, 6), round(y, 6), round(zc + zr, 6)])
    return pts


def station_span(u: float, nose: dict) -> dict:
    """某站的**手算外廓**（宽/高/圆心高）—— 拿去和放样后读回的值对账。"""
    x, a, b, zc, blend = _state(u, nose)
    return {"u": round(u, 6), "x": round(x, 6),
            "half_width": round(a, 6), "half_height": round(b, 6),
            "center_z": round(zc, 6), "blend": round(blend, 6),
            "width": round(2 * a, 6), "height": round(2 * b, 6),
            "z_min": round(zc - b, 6), "z_max": round(zc + b, 6)}


def filled_volume_mm3(nose: dict, samples: int = 4001) -> float:
    """**填实**鼻锥的体积（mm^3）= 积分 A(u)*L du（梯形法，端点半权）。"""
    p = nose["_prep"]
    L = float(nose["length"])
    tot = 0.0
    for i in range(samples):
        u = i / (samples - 1)
        _x, a, b, _zc, blend = _state(u, nose)
        tot += (0.5 if i in (0, samples - 1) else 1.0) * _section_area(
            a, b, p["g1"], p["g2"], blend)
    return tot * (L / (samples - 1))


def lateral_area_mm2(nose: dict, s_phi: int = 361, s_u: int = 201) -> float:
    """放样曲面（侧面）的**解析面积**（mm^2）—— 用于 `s4_shell` 的体积判据。

    面积元 = |dr/du x dr/dphi| du dphi。
    """
    L = float(nose["length"])
    shape = nose["_prep"]["shape"]
    du = 1.0 / (s_u - 1)
    dphi = 2.0 * math.pi / (s_phi - 1)
    h = 1e-5
    tot = 0.0
    for i in range(s_u):
        u = i / (s_u - 1)
        wu = 0.5 if i in (0, s_u - 1) else 1.0
        ulo, uhi = max(u - h, 0.0), min(u + h, 1.0)
        _xl, alo, blo, _zl, bllo = _state(ulo, nose)
        _xh, ahi, bhi, _zh, blhi = _state(uhi, nose)
        duu = uhi - ulo
        for j in range(s_phi):
            phi = j * dphi
            wj = 0.5 if j in (0, s_phi - 1) else 1.0
            ypa, zpa = _sec_yz(phi - h, alo, blo, shape, bllo)
            ypb, zpb = _sec_yz(phi + h, alo, blo, shape, bllo)
            yp, zp = _sec_yz(phi, alo, blo, shape, bllo)
            yq, zq = _sec_yz(phi, ahi, bhi, shape, blhi)
            dy_phi, dz_phi = (ypb - ypa) / (2 * h), (zpb - zpa) / (2 * h)
            dy_u, dz_u = (yq - yp) / duu, (zq - zp) / duu
            cross = (dy_u * dz_phi - dz_u * dy_phi) ** 2 \
                + L * L * (dy_phi * dy_phi + dz_phi * dz_phi)
            tot += wu * wj * math.sqrt(max(cross, 0.0))
    return tot * du * dphi


# ---------------------------------------------------------------- 换路剪裁的解析判据

def _surf_pt_normal(u: float, phi: float, nose: dict):
    """曲面上一点 (x, y, z) 与**单位外法线**，以及面积元 |du x dphi|（有限差分）。"""
    h = 1e-5
    L = float(nose["length"])
    shape = nose["_prep"]["shape"]

    def P(uu: float, pp: float):
        _x, a, b, zc, blend = _state(uu, nose)
        y, zr = _sec_yz(pp, a, b, shape, blend)
        return (uu * L, y, zc + zr)

    ulo, uhi = max(u - h, 0.0), min(u + h, 1.0)
    p = P(u, phi)
    # ★ 中心差分：分子必须跨 (uhi - ulo) 的**同一段**，别用「前向差 h 除以 2h」。
    tu = [(P(uhi, phi)[k] - P(ulo, phi)[k]) / (uhi - ulo) for k in range(3)]
    tp = [(P(u, phi + h)[k] - P(u, phi - h)[k]) / (2 * h) for k in range(3)]
    cr = [tu[1] * tp[2] - tu[2] * tp[1],
          tu[2] * tp[0] - tu[0] * tp[2],
          tu[0] * tp[1] - tu[1] * tp[0]]
    zc = _state(u, nose)[3]
    rad = (0.0, p[1], p[2] - zc)
    if sum(cr[k] * rad[k] for k in range(3)) < 0:
        cr = [-v for v in cr]
    mag = math.sqrt(sum(v * v for v in cr)) or 1.0
    return p, [v / mag for v in cr], mag


def shell_removed_mm3(nose: dict, thk: float, inside, nu: int = 180, nphi: int = 260,
                      ns: int = 6) -> float:
    """「鼻锥薄壳 ∩ 判据区域」的体积（mm³）—— 换路剪裁（`extrude_cut`）的**解析断言值**。

    inside(x, y, z) -> bool ：该点是否落在**切除工具**的区域内（盒 / 圆柱…）。

    ★ 假设写死，便于对质：壳 = 参数曲面 S 沿**外法线**厚 `thk` 的薄层；厚度方向取
      **中点近似** `s ∈ [−t/2, +t/2]`（`thicken side1/side2` 的单侧之别只挪 t/2≈1.5mm，
      对小体积影响 <2%）。曲面本身用**径向参数化**（`_se_point`，正则、不发散）。
    ★ 与 `check_mass` 的关系：这是**独立于 SolidWorks 读回值**的解析/数值预算，
      拿它当期望 ⇒ 判据能证伪「切错方向 / 没切到 / 切过头」。
    """
    L = float(nose["length"])
    du, dphi = 1.0 / nu, 2.0 * math.pi / nphi
    tot = 0.0
    for i in range(nu):
        u = (i + 0.5) * du
        for j in range(nphi):
            phi = (j + 0.5) * dphi
            p, nrm, area = _surf_pt_normal(u, phi, nose)
            hit = 0
            for k in range(ns):
                s = thk * ((k + 0.5) / ns - 0.5)
                if inside(p[0] + s * nrm[0], p[1] + s * nrm[1], p[2] + s * nrm[2]):
                    hit += 1
            tot += area * (hit / ns)
    return tot * du * dphi * thk


# ---- 开口轮廓（R-A 精修：矩形/圆 → 真车形状的多边形）------------------------
#
# ★ R-A 的来历：拿 4 张真车参考照（正脸 / 站台 3/4 / 侧视×2）对照后，
#   风挡是**上宽下窄的圆角梯形深色面罩**、驾驶室侧窗是**随车身线后掠的斜四边形**，
#   而旧版是「矩形贯穿切口 + Ø240 圆」——形状差得最远（★显著，见对照页）。
#   修法：把工具轮廓从 `rect_center`/`circle` 换成 `polyline`（任意闭合折线），
#   圆角用**离线可算**的 `round_poly` 折线近似（不用 `sketch_fillet`：那个对选中集敏感）。

def _unit2(dx: float, dy: float) -> tuple[float, float]:
    m = math.hypot(dx, dy) or 1.0
    return (dx / m, dy / m)


def poly_area(pts: list) -> float:
    """鞋带公式（有向面积；逆时针为正）。用来判「点序有没有自交/反了」。"""
    s = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return 0.5 * s


def _point_in_poly(p: tuple, poly: list) -> bool:
    """射线法：点在多边形内。"""
    x, y = p
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _poly_mask(pts: list):
    """把多边形包成一个**带包围盒早退**的 inside(a,b)（数值积分里要被调几十万次）。"""
    x0 = min(p[0] for p in pts)
    x1 = max(p[0] for p in pts)
    y0 = min(p[1] for p in pts)
    y1 = max(p[1] for p in pts)

    def inside(a: float, b: float) -> bool:
        if a < x0 or a > x1 or b < y0 or b > y1:
            return False
        return _point_in_poly((a, b), pts)

    return inside


def round_poly(pts: list, r: float, seg: int = 10, min_turn_deg: float = 0.0) -> list:
    """把闭合凸多边形的角倒成半径 r 的圆角，返回新点列（不重复首点）。

    ★ 圆角几何：内角 φ ⇒ 切点距 d = r / tan(φ/2)，圆心在角平分线上；
      d 被**限制到相邻边长的一半**（否则相邻圆角互相吃掉）。退化角原样保留。
    ★ `min_turn_deg`：只倒**转角**（= 180° − 内角）大于它的角。用于「只倒尖角、
      保留接近平角的角」—— 例如车体断面只倒底部 90° 直角，别碰 175° 的车顶峰。
    """
    n = len(pts)
    out: list[tuple[float, float]] = []
    for i in range(n):
        pv, pc, pn = pts[(i - 1) % n], pts[i], pts[(i + 1) % n]
        u1 = _unit2(pv[0] - pc[0], pv[1] - pc[1])
        u2 = _unit2(pn[0] - pc[0], pn[1] - pc[1])
        cosphi = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        phi = math.acos(cosphi)
        turn = math.pi - phi
        if r <= 0 or phi < 1e-6 or math.pi - phi < 1e-6 \
                or turn < math.radians(min_turn_deg):
            out.append((pc[0], pc[1]))
            continue
        d = r / math.tan(phi / 2.0)
        d = min(d, 0.5 * math.hypot(pv[0] - pc[0], pv[1] - pc[1]),
                0.5 * math.hypot(pn[0] - pc[0], pn[1] - pc[1]))
        rr = d * math.tan(phi / 2.0)
        bis = _unit2(u1[0] + u2[0], u1[1] + u2[1])
        t1 = (pc[0] + d * u1[0], pc[1] + d * u1[1])
        t2 = (pc[0] + d * u2[0], pc[1] + d * u2[1])
        c = (pc[0] + (rr / math.sin(phi / 2.0)) * bis[0],
             pc[1] + (rr / math.sin(phi / 2.0)) * bis[1])
        a1 = math.atan2(t1[1] - c[1], t1[0] - c[0])
        a2 = math.atan2(t2[1] - c[1], t2[0] - c[0])
        da = a2 - a1
        while da <= -math.pi:
            da += 2 * math.pi
        while da > math.pi:
            da -= 2 * math.pi
        for k in range(seg + 1):
            a = a1 + da * k / seg
            out.append((c[0] + rr * math.cos(a), c[1] + rr * math.sin(a)))
    return out


def windshield_outline(ws: dict) -> list:
    """风挡轮廓 = **圆角梯形**（全局 (y, z)，闭合，逆时针；上宽下窄）。

    点序：左下 → 右下 → 右上 → 左上。
    """
    zb, zt = float(ws["z_bot"]), float(ws["z_top"])
    yb, yt = float(ws["y_bot"]), float(ws["y_top"])
    pts = [(-yb, zb), (yb, zb), (yt, zt), (-yt, zt)]
    return round_poly(pts, float(ws.get("corner_r", 0.0) or 0.0))


def wing_outline(wg: dict) -> list:
    """驾驶室侧窗（翼窗）轮廓 = **斜四边形**（全局 (x, z)，闭合；后掠）。

    点序：前下 → 前上 → 后上 → 后下。
    """
    pts = [(float(wg["x_front"]), float(wg["z_front_lo"])),
           (float(wg["x_rear"]), float(wg["z_rear_lo"])),
           (float(wg["x_rear"]), float(wg["z_rear_hi"])),
           (float(wg["x_front"]), float(wg["z_front_hi"]))]
    return round_poly(pts, float(wg.get("corner_r", 0.0) or 0.0))


def side_window_removed_mm3(nose: dict, outline: list, thk: float) -> float:
    """翼窗（Top 基准面画**斜四边形**、沿 Y 双贯穿）的去除量数值积分（mm³）。

    工具区域 = {(x,z) ∈ 翼窗多边形}（沿 Y 无限）⇒ 左右两侧各开一个。
    """
    inside_xz = _poly_mask([(float(p[0]), float(p[1])) for p in outline])

    def inside(x, y, z):
        return inside_xz(x, z)

    return shell_removed_mm3(nose, thk, inside, nu=160, nphi=240, ns=6)


def windshield_removed_mm3(nose: dict, outline: list, thk: float) -> float:
    """风挡（Right 基准面画**圆角梯形**、沿 X 完全贯穿）的去除量数值积分（mm³）。

    工具区域 = {(y,z) ∈ 风挡多边形}（沿 X 无限）⇒ 开口自然止于顶面离开轮廓之处。
    """
    # ★ 轮廓本身就是 (y, z)（`windshield_outline` 的定义）⇒ 掩膜也必须是 (y, z)。
    #   2026-10-12 实测踩过：这里曾写成 (p[1], p[0])（当成 (z, y)），于是「掩膜是 (z,y)、
    #   却拿 (y,z) 去测」⇒ 包围盒早退永远 False ⇒ **去除量恒为 0**。
    #   而 0 会一路传成 `check_volume_delta(expect_mm3=0, tol_mm3=0)` —— 真机不切也"通过"，
    #   典型**假绿**。现在自检里加了「预算必须 > 0」那条（见 ⑦）。
    inside_yz = _poly_mask([(float(p[0]), float(p[1])) for p in outline])

    def inside(x, y, z):
        return inside_yz(y, z)

    return shell_removed_mm3(nose, thk, inside, nu=160, nphi=240, ns=6)


# ---------------------------------------------------------------- 计划

def build_plan(params: dict, stage: str) -> dict:
    prepare(params)                      # 编译轮廓/形状模板（幂等）
    st = STAGES[stage]
    nose = params["nose"]
    body = params["body"]
    n = int(st["n_stations"])
    us = [i / (n - 1) for i in range(n)]

    steps: list[dict] = [{"id": "new", "op": "new_part"}]
    sec_names, gd_names = [], []
    for i, u in enumerate(us):
        nm = f"SEC_{i}"
        sec_names.append(nm)
        steps.append({"id": f"sec{i}", "op": "curve_xyz", "name": nm,
                      "points": section_points(u, nose), "closed": True})
    for j, ang in enumerate(st["guides_deg"]):
        nm = f"GUIDE_{j}"
        gd_names.append(nm)
        # ★ 引导线点数必须 ≥ 截面点数的一半（selfcheck ③，真机验证过「太稀⇒放样静默失败」）。
        #   截面点加密到 144 后，3 站位的 s2 段只有 2 段 ⇒ 每段要 36 点才够 ⇒ 用 st["subdiv"] 覆盖。
        steps.append({"id": f"gd{j}", "op": "curve_xyz", "name": nm,
                      "points": guide_points(ang, nose,
                                             guide_u_list(nose, n, st.get("subdiv"))),
                      "closed": False})

    steps.append({"id": "loft", "op": "surface_loft", "name": f"nose_{stage}",
                  "sections": sec_names, "guides": gd_names,
                  "closed": False,
                  "keep_tangency": bool(nose.get("loft_keep_tangency", True))})
    steps.append({"id": "v1", "op": "check_body",
                  "expect_sheet": 1, "expect_solid": 0, "min_sheet": 1})
    steps.append({"id": "v2", "op": "check_curve", "feature": sec_names[0],
                  "expect_points": int(nose["_prep"]["sec_n"]) + 1,
                  "expect_closed": True})
    steps.append({"id": "v3", "op": "check_feature_count",
                  "min": len(sec_names) + len(gd_names) + 2})

    # ---- S4 收口：把开放曲面变成实体（两条独立路线） ----
    close = st.get("close")
    if close == "knit":
        # ① 鼻端 / 尾端各封一张**平面盖**（`surface_planar` 按边拾取，见 §133），
        #    ② 再 `surface_knit(form_solid=True)` 缝成**单一实体**。
        steps.append({"id": "cap0", "op": "surface_planar",
                      "boundary": sec_names[0], "name": "CAP_0"})
        steps.append({"id": "cap8", "op": "surface_planar",
                      "boundary": sec_names[-1], "name": "CAP_8"})
        steps.append({"id": "knit", "op": "surface_knit",
                      "targets": [f"nose_{stage}", "CAP_0", "CAP_8"],
                      "tolerance": 0.01, "form_solid": True,
                      "merge_entities": True, "name": "NOSE_SOLID"})
        steps.append({"id": "v4", "op": "check_body", "expect_sheet": 0,
                      "expect_solid": 1})
        steps.append({"id": "v5", "op": "check_mass",
                      "expect_volume_mm3": round(filled_volume_mm3(nose), 1),
                      "vol_tol": 0.02})
        zv = [float(p[1]) for p in body["outline"]]
        # ★★ 容差为什么不是 2mm：放样面是「过点样条 + 多边形尖角」的产物，**系统性过冲**
        #   无法为零。实测（36 点均匀）半宽 1654.1 / 底 84.5，超设计 +51.2 / +25.5；
        #   截面点**在尖角处定向加密**（`corner_extra`/`corner_span_deg`）后降到
        #   +16.6 / +8.3（0.51% / 0.22%），且已被本文件的「截面贴合偏差」判据证明
        #   曲面确实过设计截面（未参与站位 ≤0.007mm）。⇒ 该 bbox 判据的职责是**拦量级/
        #   比例错误**，不是拦这 ~0.5% 的样条过冲；容差走参数、可调、写明。
        steps.append({"id": "v6", "op": "check_bbox",
                      "expect": [float(nose["length"]), float(body["width"]),
                                 max(zv) - min(zv)],
                      "tol_mm": float(nose.get("bbox_tol_mm", 25.0))})
    elif close == "thicken":
        # 直接加厚成**薄壳实体**（不封口；加厚自带边界侧壁）。
        thk = float(nose.get("shell_thickness", 3.0))
        steps.append({"id": "thk", "op": "thicken", "targets": [f"nose_{stage}"],
                      "thickness": thk, "direction": "side2", "name": "NOSE_SHELL"})
        steps.append({"id": "v4", "op": "check_body", "expect_sheet": 0,
                      "expect_solid": 1})
        steps.append({"id": "v5", "op": "check_mass",
                      "expect_volume_mm3": round(lateral_area_mm2(nose) * thk, 1),
                      "vol_tol": 0.08})

    elif close == "windows":
        # ---- S5：曲面壳 → 挖风挡 / 侧窗开口（换路 = thicken + extrude_cut）----
        win = params.get("windows", {})
        thk = float(win.get("shell_thickness", 3.0))
        area = lateral_area_mm2(nose)
        vol = area * thk

        # ① 加厚成薄壳实体（判据：体数 0→1，体积 ≈ 面积 × t）
        steps.append({"id": "thk", "op": "thicken", "targets": [f"nose_{stage}"],
                      "thickness": thk, "direction": "side2", "name": "NOSE_SHELL"})
        steps.append({"id": "v4", "op": "check_body", "expect_sheet": 0,
                      "expect_solid": 1})
        steps.append({"id": "v5", "op": "check_mass",
                      "expect_volume_mm3": round(vol, 1), "vol_tol": 0.08})
        steps.append({"id": "v5f", "op": "check_faces", "surfaces": False})
        # ★ 记基准：此后每一步去料都用「体积变化量」判定（绝对体积判不出「切了多少」）
        steps.append({"id": "v5b", "op": "check_volume_delta", "baseline": True})

        # ② 翼窗：**Top** 基准面画**斜四边形**（本地 u→+X、v→−Z ⇒ 点 (x, −z)）→ 沿 Y 双贯穿
        wg = win.get("wing")
        if wg:
            ol_wing = wing_outline(wg)
            rm = side_window_removed_mm3(nose, ol_wing, thk)
            vol -= rm
            pts_side = [[round(x, 4), round(-z, 4)] for (x, z) in ol_wing]
            steps.append({"id": "p_side", "op": "select_plane", "plane": "Top"})
            steps.append({"id": "sb_side", "op": "sketch_begin"})
            steps.append({"id": "w_side", "op": "polyline", "points": pts_side,
                          "close": True})
            steps.append({"id": "se_side", "op": "sketch_end", "name": "SIDE_WIN"})
            steps.append({"id": "cut_side", "op": "extrude_cut", "profile": "SIDE_WIN",
                          "end_cond": "swEndCondThroughAllBoth", "both_directions": True,
                          "name": "SIDE_CUT"})
            steps.append({"id": "v6", "op": "check_body", "expect_sheet": 0,
                          "expect_solid": 1})
            steps.append({"id": "v6d", "op": "check_volume_delta",
                          "expect_mm3": round(-rm, 1), "tol_mm3": round(rm * 0.08, 1)})
            steps.append({"id": "v6f", "op": "check_faces", "surfaces": False})
            # ★ 截图前隐藏构造几何（9 截面 + 2 引导线的曲线、以及刚画的窗草图；
            #   不隐藏就是蓝线/白框糊满曲面，**看不出开口形状**）
            steps.append({"id": "hr1", "op": "hide_refs"})
            steps.append({"id": "z1", "op": "zoom_fit"})
            steps.append({"id": "s1", "op": "screenshot",
                          "path": "{{ params.out }}/crh380cl_nose_s5_sidewin.bmp"})

        # ③ 前风挡：**Right** 基准面画**圆角梯形**（本地 u→−Z、v→+Y ⇒ 点 (−z, y)）→ 沿 X 完全贯穿
        ws = win.get("windshield")
        if ws:
            ol_ws = windshield_outline(ws)
            rm = windshield_removed_mm3(nose, ol_ws, thk)
            vol -= rm
            pts_ws = [[round(-z, 4), round(y, 4)] for (y, z) in ol_ws]
            steps.append({"id": "p_ws", "op": "select_plane", "plane": "Right"})
            steps.append({"id": "sb_ws", "op": "sketch_begin"})
            steps.append({"id": "w_ws", "op": "polyline", "points": pts_ws, "close": True})
            steps.append({"id": "se_ws", "op": "sketch_end", "name": "WIND_SK"})
            steps.append({"id": "cut_ws", "op": "extrude_cut", "profile": "WIND_SK",
                          "end_cond": "swEndCondBlind",
                          "depth": float(ws.get("cut_depth", nose["length"])),
                          "name": "WS_CUT"})
            steps.append({"id": "v7", "op": "check_body", "expect_sheet": 0,
                          "expect_solid": 1})
            steps.append({"id": "v7d", "op": "check_volume_delta",
                          "expect_mm3": round(-rm, 1), "tol_mm3": round(rm * 0.12, 1)})
            steps.append({"id": "v7f", "op": "check_faces", "surfaces": False})
            steps.append({"id": "hr2", "op": "hide_refs"})
            steps.append({"id": "z2", "op": "zoom_fit"})
            steps.append({"id": "s2", "op": "screenshot",
                          "path": "{{ params.out }}/crh380cl_nose_s5.bmp"})
            # 终值复核绝对量级（壳体积 ±8% − 两处开口）
            steps.append({"id": "v8m", "op": "check_mass",
                          "expect_volume_mm3": round(vol, 1), "vol_tol": 0.08})

    steps.append({"id": "sv", "op": "save_as",
                  "path": "{{ params.out }}/crh380cl_" + st["tag"] + ".SLDPRT",
                  "overwrite": True})

    return {
        "schema": "yoyo.swplan/1",
        "meta": {
            "name": f"CRH380CL 车头鼻锥 · {stage.upper()}（{n} 截面 + {len(gd_names)} 引导线）",
            "units": "mm",
            "stage": stage,
            "params_file": "assets/crh380cl/crh380cl.params.json",
            "params_schema": params.get("schema"),
            "generated_by": "scripts/sw_head.py",
        },
        "params": {"out": str(sw_env.out_dir())},
        "steps": steps,
    }


# ---------------------------------------------------------------- 自检

def selfcheck(params: dict, plan: dict) -> list[str]:
    """生成期的**离线**自检（不碰 SolidWorks）。返回问题列表，空 = 全过。"""
    prepare(params)
    bad: list[str] = []
    nose, body = params["nose"], params["body"]
    secs = [s for s in plan["steps"] if s["op"] == "curve_xyz" and s["name"].startswith("SEC_")]
    gds = [s for s in plan["steps"] if s["op"] == "curve_xyz" and s["name"].startswith("GUIDE_")]

    # ① 所有截面点数一致（放样不扭的前提）
    counts = {len(s["points"]) for s in secs}
    if len(counts) != 1:
        bad.append(f"截面点数不一致：{sorted(counts)} ⇒ 放样必然扭曲（R2）")
    _sn = int(nose["_prep"]["sec_n"])
    if counts and next(iter(counts)) != _sn:
        bad.append(f"截面点数 {next(iter(counts))} ≠ 参数 {_sn}"
                   "（= section_points + 尖角加密点）")

    # ② 所有截面起点同在 θ=0 侧（y>0 且 z ≈ 圆心高），且 x 严格单调递增
    xs, start_ys = [], []
    for i, s in enumerate(secs):
        p0 = s["points"][0]
        xs.append(p0[0])
        start_ys.append(p0[1])
        if p0[1] <= 0:
            bad.append(f"SEC_{i} 起点 y={p0[1]} ≤ 0 ⇒ 起点不在断面右侧（起点对不同侧会扭）")
    if xs != sorted(xs) or len(set(xs)) != len(xs):
        bad.append(f"截面 x 坐标不严格递增：{xs} ⇒ 放样方向不确定")

    # ③ 引导线点数一致 + 端点落在首末截面上 + ★★ 每个截面站位都落一个引导线点
    gc = {len(s["points"]) for s in gds}
    if len(gc) != 1:
        bad.append(f"引导线点数不一致：{sorted(gc)}")
    sec_xs = [s["points"][0][0] for s in secs]
    for s in gds:
        p0, p1 = s["points"][0], s["points"][-1]
        if abs(p0[0]) > 1e-9 or abs(p1[0] - float(nose["length"])) > 1e-9:
            bad.append(f"{s['name']} 端点 x 不在 [0, {nose['length']}] —— 引导线必须穿过首末截面")
        gxs = {round(p[0], 6) for p in s["points"]}
        miss_u = [x for x in sec_xs if round(x, 6) not in gxs]
        if miss_u:
            bad.append(f"{s['name']} 在这些截面站位上没有点：{miss_u} "
                       "⇒ 引导线与该截面**不相交**⇒ 放样**静默失败**（2026-10-11 真机确诊）")
        if len(s["points"]) < int(nose["_prep"]["sec_n"]) // 2:
            bad.append(f"{s['name']} 只有 {len(s['points'])} 点（截面 {nose['_prep']['sec_n']} 点）"
                       "—— 引导线太稀会让样条在截面处偏离 ⇒ 求交失败 ⇒ 放样静默失败")

    # ④ 末端断面（u=1）必须精确等于车体断面（宽/高/底/顶 + 形状 + 面积）
    sp = station_span(1.0, nose)
    prep = nose["_prep"]
    zv = [float(p[1]) for p in body["outline"]]
    z_lo, z_hi = min(zv), max(zv)
    if abs(sp["half_width"] - float(body["width"]) / 2.0) > 1e-6:
        bad.append(f"鼻锥末端半宽 {sp['half_width']} ≠ 车体 {float(body['width']) / 2.0}"
                   " ⇒ 过渡不闭合")
    if abs(sp["z_min"] - z_lo) > 1e-6 or abs(sp["z_max"] - z_hi) > 1e-6:
        bad.append(f"鼻锥末端 z∈[{sp['z_min']},{sp['z_max']}] ≠ 车体 z∈[{z_lo},{z_hi}]")
    if abs(sp["blend"] - 1.0) > 1e-9:
        bad.append(f"末端形状混合 blend={sp['blend']} ≠ 1 ⇒ 末端不是车体断面形状")
    body_area = abs(poly_area([(float(y), float(z)) for (y, z) in body["outline"]]))
    end_area = _section_area(sp["half_width"], sp["half_height"],
                             prep["g1"], prep["g2"], 1.0)
    if body_area > 0 and abs(end_area - body_area) / body_area > 0.02:
        bad.append(f"末端断面面积 {end_area:.0f} vs 车体轮廓多边形 {body_area:.0f}"
                   f"（差 {abs(end_area - body_area) / body_area:.1%}）"
                   " ⇒ 形状模板与 outline 不一致")

    # ⑤ 相邻站位间距不得为 0
    for i in range(1, len(secs)):
        if abs(secs[i]["points"][0][0] - secs[i - 1]["points"][0][0]) < 1e-6:
            bad.append(f"SEC_{i-1} 与 SEC_{i} 在同一平面 ⇒ 放样不出面")

    # ⑥ ★ 换路剪裁积分器自洽：数值 ∫area 必须 ≈ 解析 lateral_area_mm2
    #   （这条能抓「面积元差 2×」这类静默错误 —— 2026-10-11 就是它先报出来的）
    if plan["meta"].get("stage") in ("s4_shell", "s5"):
        nu, nphi = 120, 180
        du, dphi = 1.0 / nu, 2.0 * math.pi / nphi
        num = 0.0
        for i in range(nu):
            u = (i + 0.5) * du
            for j in range(nphi):
                num += _surf_pt_normal(u, (j + 0.5) * dphi, nose)[2]
        num *= du * dphi
        ana = lateral_area_mm2(nose, s_phi=181, s_u=101)
        if abs(num - ana) / ana > 0.02:
            bad.append(f"剪裁积分器不自洽：数值 ∫area={num:.0f} vs 解析 {ana:.0f}"
                       f"（相对差 {abs(num - ana) / ana:.1%}）⇒ 面积元公式有误")

    # ⑦ 风挡 / 翼窗轮廓合理性（S5 起；R-A 改为多边形后加「有向面积 > 0」这条）
    #   ★ 为什么要判有向面积：`round_poly` 的圆角弧方向由**顶点内角**决定，
    #     若点序写成顺时针（面积 < 0），圆角会朝**外**凸 ⇒ 轮廓自交 ⇒
    #     `polyline` 能画出来，但 `extrude_cut` 要么静默失败、要么切出乱形。
    #     离线就能拦，不用等真机。
    win = params.get("windows") or {}
    L = float(nose["length"])
    wg = win.get("wing")
    if wg:
        ol = wing_outline(wg)
        if len(ol) < 4:
            bad.append(f"翼窗轮廓点太少：{len(ol)}")
        if poly_area(ol) <= 0:
            bad.append(f"翼窗轮廓**有向面积 ≤ 0**（{poly_area(ol):.0f} mm²）"
                       "⇒ 点序自交/反向，切除会静默失败或切出乱形")
        xs = [p[0] for p in ol]
        if min(xs) < 0.0 or max(xs) > L:
            bad.append(f"翼窗 x∈[{min(xs):.0f},{max(xs):.0f}] 超出鼻锥 [0,{L:.0f}]")
    ws = win.get("windshield")
    if ws:
        if not (float(ws["z_top"]) > float(ws["z_bot"])):
            bad.append(f"风挡 z 带非法：[{ws['z_bot']},{ws['z_top']}]")
        if min(float(ws["y_bot"]), float(ws["y_top"])) <= 0:
            bad.append(f"风挡半宽非法：y_bot={ws['y_bot']} y_top={ws['y_top']}")
        ol = windshield_outline(ws)
        if len(ol) < 4:
            bad.append(f"风挡轮廓点太少：{len(ol)}")
        if poly_area(ol) <= 0:
            bad.append(f"风挡轮廓**有向面积 ≤ 0**（{poly_area(ol):.0f} mm²）⇒ 点序自交/反向")

    # ⑧ ★ 去除量预算必须 > 0 —— 防「掩膜轴序写错 ⇒ 恒 0 ⇒ **不切也通过**」这类假绿
    #   （2026-10-12 实测踩过：风挡掩膜写成了 (z,y) 却拿 (y,z) 去测，预算恒 0，
    #    而 0 会原样传成 `check_volume_delta(expect_mm3=0, tol_mm3=0)`。）
    if win:
        thk = float(win.get("shell_thickness", 3.0))
        if wg:
            rm = side_window_removed_mm3(nose, wing_outline(wg), thk)
            if rm <= 0:
                bad.append(f"翼窗去除量预算 = {rm:.1f} mm³（应为正）⇒ 掩膜/点序有误")
        if ws:
            rm = windshield_removed_mm3(nose, windshield_outline(ws), thk)
            if rm <= 0:
                bad.append(f"风挡去除量预算 = {rm:.1f} mm³（应为正）⇒ 掩膜/点序有误")
    return bad


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description="CRH380CL 车头参数化截面族 → 建模计划")
    ap.add_argument("--params", default=str(DEFAULT_PARAMS))
    ap.add_argument("--stage", default="s2", choices=sorted(STAGES))
    ap.add_argument("--out", default="")
    ap.add_argument("--summary", action="store_true", help="只打印站位数值，不写计划")
    a = ap.parse_args()

    params = json.loads(Path(a.params).read_text(encoding="utf-8"))
    plan = build_plan(params, a.stage)
    bad = selfcheck(params, plan)

    nose = params["nose"]
    n = STAGES[a.stage]["n_stations"]
    print(f"== CRH380CL 车头 · {a.stage.upper()} "
          f"（{n} 截面 × {nose['_prep']['sec_n']} 点 + {len(STAGES[a.stage]['guides_deg'])} 引导线）")
    print(f"   鼻锥长 {nose['length']}mm   车体断面 "
          f"{params['body']['width']}×{params['body']['height']}")
    print("   站位外廓（手算，放样后按此对账）：")
    for i in range(n):
        s = station_span(i / (n - 1), nose)
        print(f"     u={s['u']:.3f}  x={s['x']:8.1f}  "
              f"宽 {s['width']:8.1f}  高 {s['height']:8.1f}  "
              f"圆心z {s['center_z']:8.1f}  blend {s['blend']:.3f}")

    if bad:
        print("\n★★ 自检**不通过**（%d 条）：" % len(bad))
        for b in bad:
            print("   ✗ " + b)
        if not a.summary:
            return 1
    else:
        print("\n   自检全过：点数一致 / 起点同侧 / x 单调 / 引导线穿首末截面 / 末端 = 车体断面")

    if a.summary:
        return 0 if not bad else 1
    if not a.out:
        print("\n（没给 --out，不写文件）")
        return 0 if not bad else 1
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n   已写计划：{out}（{len(plan['steps'])} 步）")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
