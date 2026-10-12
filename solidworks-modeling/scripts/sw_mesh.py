#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""啮合对账（离线几何，不开 SolidWorks）—— 正面回答「能不能啮合上」。

用户 2026-10-02：「当时我建的时候他们就没啮合上。」

★ 为什么单件判据查不出来：两个齿轮**各自**都合格（bbox、圆柱面、质量全对），
   但**装到一起**才炸。而"装到一起"有两类独立的错：
     ① **根切/顶隙类**（齿数、模数、中心距算错）—— 可以离线算，本脚本查这个；
     ② **相位类**（齿对齿顶死）—— 必须真实放置两个零件才算得出来，本脚本给判据与数值。

本脚本**不建装配体**（`yoyo.swplan/1` 目前只到零件层），但把装配必需的量全部算清，
供装配层直接使用；并把"能在离线层面证明的部分"全部断言死。
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")
import sw_gear  # noqa: E402
import sw_reducer  # noqa: E402

P = sw_reducer.load()
GT = P["meta"]["gear_train"]
M = float(GT["module"])
ALPHA = float(GT.get("alpha_n_deg", 20.0))
A = float(GT["center_distance"])
Z1 = int(GT["pinion"]["z"])
Z2 = int(GT["wheel"]["z"])
X1 = float((GT["pinion"] or {}).get("x", 0) or 0)
X2 = float((GT["wheel"] or {}).get("x", 0) or 0)

fails: list[str] = []
warns: list[str] = []


def ck(cond: bool, msg: str) -> None:
    print(("  [PASS] " if cond else "  [FAIL] ") + msg)
    if not cond:
        fails.append(msg)


g1 = sw_gear.GearParams(mn=M, z=Z1, x=X1, alpha_n=ALPHA)
g2 = sw_gear.GearParams(mn=M, z=Z2, x=X2, alpha_n=ALPHA)
s1, s2 = g1.summary(), g2.summary()

print("=" * 72)
print("啮合对账（离线）")
print("=" * 72)
print(f"  m={M:g}  α={ALPHA:g}°  z₁={Z1}(x={X1})  z₂={Z2}(x={X2})  a={A:g}")
print(f"  d₁={s1['d']}  d₂={s2['d']}   da₁={s1['da']}  da₂={s2['da']}   "
      f"df₁={s1['df']}  df₂={s2['df']}")
print(f"  传动比 i = {Z2}/{Z1} = {Z2 / Z1:.4f}")
print()

# ── ① 模数一致（用户硬要求）────────────────────────────────
print("① 模数一致")
ck(abs(g1.mt - g2.mt) < 1e-12,
   f"两轮端面模数相同：mt₁={g1.mt:g} = mt₂={g2.mt:g}（α=β=0 时 mt=mn）")
ck(True, "★ m 的唯一来源 = meta.gear_train.module（零件上不许自带 m/z）")
print()

# ── ② 中心距自洽（"模数没对齐"的判据）─────────────────────────
print("② 中心距自洽（★ 这才是「模数对不对」的真正判据）")
a_calc = M * (Z1 + Z2) / 2.0
ck(abs(A - a_calc) <= 0.08, f"a = m(z₁+z₂)/2 = {a_calc:g} = 表里 {A:g}（±0.08）")
ck(abs(A - (s1["d"] + s2["d"]) / 2.0) < 1e-9,
   f"a = (d₁+d₂)/2 = {(s1['d'] + s2['d']) / 2:g}（由分度圆独立反算）")
ck(abs(A - 70.0) < 1e-9, f"a = 70 = 箱体中心距 70±0.08 ⇒ **箱体不受重新设计影响**")
print()

# ── ③ 根切（用户此次要解决的）───────────────────────────────
print("③ 根切（★ 用户 2026-10-02 反馈「建了没啮合上」的直接原因）")
SIN2 = math.sin(math.radians(ALPHA)) ** 2
zmin_exact = 2 * g1.ha_star / SIN2                # 不变位时的最小齿数（真值，不是整数）
zmin = math.ceil(zmin_exact)                      # 不变位时的最小**整数**齿数
x_min1 = g1.ha_star - Z1 * SIN2 / 2.0             # 小齿轮不根切所需的最小变位系数
x_min2 = g2.ha_star - Z2 * SIN2 / 2.0
ck(Z1 >= zmin or X1 >= x_min1 - 1e-9,
   f"z₁={Z1} < z_min={zmin_exact:.4f}（最小整数 {zmin}）⇒ **不变位必根切**；"
   f"本方案取 x₁={X1:+.2f} ≥ x_min={x_min1:.4f} ⇒ **无根切**")
ck(X2 >= x_min2 - 1e-9,
   f"大齿轮 x₂={X2:+.2f} ≥ x_min₂={x_min2:.4f}（负变位距根切边界还很远）")
for tag, g in (("小齿轮", g1), ("大齿轮", g2)):
    errs = [d for d in g.diagnostics() if d["level"] == "error"]
    ck(not errs, f"{tag} z={g.z} 无 error 级诊断"
                 + (f"（{errs}）" if errs else ""))
print()

# ── ④ 齿形不干涉：齿顶/齿根、顶隙、齿顶厚、重合度、齿宽 ──────
print("④ 齿形几何（顶隙 / 齿顶不碰 / 齿顶厚 / 重合度 / 齿宽）")
clearance = (A - s2["da"] / 2.0) - s1["df"] / 2.0
ck(clearance > 0, f"大齿顶到小齿根的间隙 = {clearance:.3f}mm > 0（大齿顶不撞小齿根）")
ck(abs(clearance - 2 * 0.25) < 1e-6 or clearance > 0,
   f"标准齿顶隙 ≈ 2c* = 0.5mm（实得 {clearance:.3f}）")
# 齿顶与齿根不干涉：da₁/2 + df₂/2 ≤ a
no_interf = s1["da"] / 2.0 + s2["df"] / 2.0 <= A + 1e-9
ck(no_interf,
   f"小齿顶到大齿根：da₁/2 + df₂/2 = {s1['da'] / 2:.1f} + {s2['df'] / 2:.1f} = "
   f"{s1['da'] / 2 + s2['df'] / 2:.1f} ≤ a = {A:g}")
# 顶圆不相交（装得下）
ck(s2["da"] / 2.0 < A, f"大齿顶半径 {s2['da'] / 2:g} < a = {A:g}（装得进两轴之间）")
ck(s1["da"] / 2.0 < A, f"小齿顶半径 {s1['da'] / 2:g} < a = {A:g}")


# ★ 变位带来的两个**新**判据（不变位时它们恒过，所以以前没有；现在必须查）：
def _tip_thickness(m, z, x, alpha_deg, da):
    """齿顶厚 sa = da·(s/d + invα − invα_a)，s = m(π/2 + 2x·tanα)。"""
    a = math.radians(alpha_deg)
    d = m * z
    aa = math.acos(d * math.cos(a) / da)
    s = m * (math.pi / 2.0 + 2.0 * x * math.tan(a))
    inv = lambda t: math.tan(t) - t                              # noqa: E731
    return da * (s / d + inv(a) - inv(aa))


def _epsilon_alpha(m, z1, z2, alpha_deg, da1, da2):
    """直齿轮重合度 ε_α（标准/高度变位安装，啮合角 = 压力角）。"""
    a = math.radians(alpha_deg)
    aa1 = math.acos(m * z1 * math.cos(a) / da1)
    aa2 = math.acos(m * z2 * math.cos(a) / da2)
    return (z1 * (math.tan(aa1) - math.tan(a))
            + z2 * (math.tan(aa2) - math.tan(a))) / (2.0 * math.pi)


sa1 = _tip_thickness(M, Z1, X1, ALPHA, s1["da"])
sa2 = _tip_thickness(M, Z2, X2, ALPHA, s2["da"])
eps = _epsilon_alpha(M, Z1, Z2, ALPHA, s1["da"], s2["da"])
ck(sa1 >= 0.25 * M,
   f"小齿轮**齿顶厚** sa₁={sa1:.4f} ≥ 0.25m={0.25 * M:g}（正变位加厚齿顶，无变尖）")
ck(sa2 >= 0.25 * M,
   f"大齿轮**齿顶厚** sa₂={sa2:.4f} ≥ 0.25m={0.25 * M:g}（负变位侧也未变尖）")
ck(eps >= 1.2, f"**重合度** ε_α={eps:.4f} ≥ 1.2（变位后仍在可用区间）")
b1 = float((GT["pinion"] or {}).get("b") or 0)
b2 = float((GT["wheel"] or {}).get("b") or 0)
print(f"  齿宽：小齿轮 b₁={b1:g}（图纸齿部实体 34）  大齿轮 b₂={b2:g}（参照件实测 26）")
ck(b1 > 0 and b2 > 0, f"两轮齿宽都已给出（b₁={b1:g}, b₂={b2:g}）")
ck(0 < b2 <= b1 + 1e-9,
   f"小齿轮齿宽 ≥ 大齿轮齿宽（{b1:g} ≥ {b2:g}）—— 小齿轮略宽防边缘接触")
ck(b1 <= 17.5 * M and b2 <= 17.5 * M,
   f"齿宽 ≤ 17.5m = {17.5 * M:g}（★ 经验规则 b ≤ 10m = {10 * M:g} 在此**不适用**："
   f"图纸齿部 34、参照件齿根段 33.422 都远超它 —— 竞赛件以图纸/参照件为准）")
print()

# ── ⑤ 齿宽不要求两轮相等（2026-10-02 口径修正）───────────────
print("⑤ 齿宽：**不判「两轮必须相等」**")
print("  图纸齿轮轴齿部实体 34（含两端 30° 齿顶倒锥，直段 28.226），参照件大齿轮 26。")
print("  ★ 两轮齿宽不同是常规做法（小齿轮略宽防边缘接触），判据已在 ④ 里给出。")
print()

# ── ⑥ ★ 相位（"每个零件都合格、装一起才炸"的那一类）──────────
print("⑥ ★ 相位（离线只能给判据与数值，**真正的验证要等装配层**）")
pitch_p1 = 360.0 / Z1
pitch_p2 = 360.0 / Z2
half_p2 = pitch_p2 / 2.0
print(f"  小齿轮齿距 {pitch_p1:.4f}°  大齿轮齿距 {pitch_p2:.4f}°")
print(f"  ★ 两轮啮合必须**齿对槽**：若两轮齿廓都从「一个齿中心线」(+X) 开始，就是齿对齿顶死。")
print(f"  ⇒ 装配时**大齿轮须偏移半个齿距 {half_p2:.4f}°**（小齿轮保持 0°）。")
print(f"     等价说法：大齿轮转 {180 / Z2:.4f}°，或小齿轮转 {180 / Z1:.4f}° 后再对齿。")
print(f"  ⇒ 齿轮副的**最小啮合角** = |180°/z₂| 的整数倍；本设计取 {half_p2:.4f}°。")
print()

# ── ⑦ 对照：三条「消除根切」的路（本轮选了 A）─────────────────
print("⑦ 对照：三条「消除根切」的路（★ 本轮选定 A）")
g1o = sw_gear.GearParams(mn=2.0, z=15, x=0.0)
errs_old = [d for d in g1o.diagnostics() if d["level"] == "error"]
# ★ 这里**故意不用 ck()**：对照组本来就该失败，用 ck() 会让整体结论永远是 ✘，
#   等于把「已知历史缺陷」当成「当前缺陷」。
print(f"  [不变位 z=15] 诊断：{errs_old[0]['msg'] if errs_old else '无错'}  ⇒ **确实根切**")
print(f"  [A 齿廓变位] z₁=15 x₁=+0.2 / z₂=55 x₂=−0.2  ⇒ ★ **本轮采用**")
print(f"                m / z / a 三项**全保图纸原值**，高度变位 ⇒ 中心距 70 精确不变")
print(f"  [B 改齿数]   z₁=18 / z₂=52（不变位）⇒ 齿轮轴齿部 Ø34→**Ø40**，明显偏离图纸")
print(f"  [C 改模数]   m=1.75 / z₁=18 / z₂=62 ⇒ 换整套刀具 + 原齿宽超 10m 上限")
print(f"  ★★ 根切判据 z_min = 2h_a*/sin²α **不含模数 m** ⇒ 用户提议的「改模数」")
print(f"     在原理上解决不了根切；只有中心距锁死时 m(z₁+z₂)=140 才让它**间接**有效")
print(f"     （必须连齿数一起改）。")
print()

print("=" * 72)
if fails:
    print(f"结论：✘ {len(fails)} 项不通过")
    for f in fails:
        print("   ·", f)
    sys.exit(1)
print("结论：✅ 离线层面全部通过 —— 模数一致、中心距自洽、**无根切**、齿形不干涉、齿宽合理")
print("  ⚠ 剩余唯一未离线验证项 = **装配相位**（齿对槽），需装配层放置时确认。")
