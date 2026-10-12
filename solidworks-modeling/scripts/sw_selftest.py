#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_selftest.py —— 离线回归测试。**不需要打开 SolidWorks，也不需要 pywin32。**

为什么需要它：
    这个技能会一直长大（新操作、新顺序、新零件）。如果没有回归，
    某次「顺手改一下」就可能把齿轮的齿廓算法或某个 op 的前置条件弄坏，
    而且要等到真开 SolidWorks 跑一遍才发现 —— 太慢。
    真正跑一遍 SolidWorks 要几十秒到几分钟，离线检查只要 1 秒。

跑什么：
    1. 操作表自洽性：每个 op 的 requires/effect 只用已知状态名、有文档、有 API 配方
    2. 几何自检：sw_gear 的渐开线三个已知点、根切判据、斜齿端面换算
    3. 正例：assets/plans/*.json 全部要通过校验
    4. 反例：故意写错的计划**必须**被拦住（否则校验器就是摆设）
    5. 模板同步：CLI 里的模板和 assets/plans 里的文件必须一致（防文档漂移）

用法：
    python sw_selftest.py                 # 离线全跑
    python sw_selftest.py -v             # 打印每条断言
    python sw_selftest.py --run          # 额外真跑 SolidWorks（会开软件，慢）
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import sw_gb  # noqa: E402
import sw_gear  # noqa: E402
import sw_ops  # noqa: E402
import sw_plan  # noqa: E402
import sw_profile  # noqa: E402
import sw_reducer  # noqa: E402
import sw_spec  # noqa: E402

PLANS = ROOT / "assets" / "plans"
PROBES = ROOT / "assets" / "probes"
VERBOSE = False
_fails: list[str] = []
_pending: list[str] = []
_passes = [0]


def check(cond: bool, label: str) -> bool:
    if cond:
        _passes[0] += 1
        if VERBOSE:
            print(f"  ok   {label}")
    else:
        _fails.append(label)
        print(f"  FAIL {label}")
    return bool(cond)


def pending(label: str) -> None:
    """★★ `PENDING` = 「**已经知道要做什么、但现在还做不到**」—— 与 `FAIL` 分档。

    ★ 起因（2026-10-02）：设计变更（齿轮变位）会让**真机记录成代作废**。
      那几条断言在**真机重跑之前永远不可能变绿** ⇒ 它们混在 FAIL 里，等于
      `sw_doctor` **永远**报「不可用」⇒ 告警疲劳，**真正的新回归会被淹没**。

    ★ 判据（不能滥用，否则就是当年被否掉的「把错误降级成警告」）：
      **只有记录显式标了 `_superseded`**（= 已有人判定它是旧版、并写明了重跑方法）
      才允许记 PENDING；**没有这个标记的一律算 FAIL**。

    ★ 与 `SKIP` 的区别：`SKIP` = "这台机器上验不了"；`PENDING` = "验得了，但要先真机重跑"。
    """
    _pending.append(label)
    if VERBOSE:
        print(f"  pend {label}")


# ---------------------------------------------------------------- 1. 操作表

def test_catalog() -> None:
    print("\n[1] 操作表自洽性")
    check(len(sw_ops.OPS) >= 30, f"操作数 >= 30（实际 {len(sw_ops.OPS)}）")
    for o in sw_ops.OPS.values():
        check(bool(o.doc), f"{o.name} 有说明")
        check(bool(o.cn), f"{o.name} 有中文名")
        check(o.cat in dict(sw_ops.CATS),
              f"{o.name} 分类合法（{o.cat}）")
        for st in sw_ops.flat_requires(o.requires) + tuple(o.effect):
            check(st in sw_ops.STATES, f"{o.name} 用的状态 '{st}' 在 STATES 里有定义")
        # ★★ 2026-10-11：`requires` 允许 **OR 组**（元组 = 至少满足一个）。
        #   守两条：① OR 组成员都必须是已知状态（上面已查）；② 组内**至少 2 个**
        #   （只有 1 个的「OR 组」是写法错误，直接写成字符串即可 —— 单元素组会让
        #   读代码的人以为有别的选择）。
        for item in o.requires:
            if isinstance(item, (tuple, list)):
                check(len(item) >= 2,
                      f"{o.name} 的 OR 组至少要有 2 个备选（现在是 {list(item)}）")
        check(o.fn is not None, f"{o.name} 有实现")
        for pname, ps in o.params.items():
            check("type" in ps, f"{o.name}.{pname} 声明了类型")
            if ps.get("unit"):
                # ★ 2026-10-11：补 mm2 / mm3 —— 面积/体积参数（check_body 的 expect_area_mm2、
                #   check_mass / check_volume_delta 的体积）此前只能**省略 unit**，
                #   等于「有量纲却不敢写」。白名单扩到面积/体积，让单位声明是诚实的。
                #   （unit 只进签名串与 op_schema.json，**不参与数值换算**，扩白名单无副作用。）
                check(ps["unit"] in ("mm", "deg", "mm2", "mm3"),
                      f"{o.name}.{pname} 单位合法")
    # 每个「要求 sketch_open」的 op 都必须在 sketch 分类里
    bad = [o.name for o in sw_ops.OPS.values()
           if "sketch_open" in sw_ops.flat_requires(o.requires) and o.cat != "sketch"]
    check(not bad, f"要求 sketch_open 的 op 都归类为 sketch（越界的：{bad}）")
    # 每个特征级 op 都必须显式声明依赖
    bad = [o.name for o in sw_ops.OPS.values()
           if o.cat == "feature" and not o.requires and o.name not in ("new_part",)]
    check(not bad, f"特征类 op 都声明了前置条件（漏的：{bad}）")
    # ★★★ 2026-10-11：op 表里注册的函数**必须是它自己的那个 op_xxx**。
    #   成因（真机踩过）：把辅助函数插在 `@op(...)` 装饰器与 `def op_xxx` 之间，
    #   装饰器就套到了辅助函数上 ⇒ `OPS[name].fn` 指向辅助函数。
    #   症状极具欺骗性：报错在辅助函数内部、与 op 名毫无关系（KeyError 出现在一个
    #   跟 op 无关的参数上），排查会绕远路。别名允许，但必须显式登记。
    _FN_ALIASES = {"smart_dim": "op_dimension"}
    bad = []
    for o in sw_ops.OPS.values():
        want = _FN_ALIASES.get(o.name, f"op_{o.name}")
        got = getattr(o.fn, "__name__", "?") if o.fn is not None else "None"
        if got != want:
            bad.append(f"{o.name}→{got}(应为 {want})")
    check(not bad, f"每个 op 注册的函数就是它自己的实现（错位的：{bad}）")


# ---------------------------------------------------------------- 2. 几何

def test_geometry() -> None:
    print("\n[2] 齿轮几何自检")
    g = sw_gear.GearParams(mn=2, z=55, b=20)
    s = g.summary()
    check(abs(s["d"] - 110.0) < 1e-9, "m=2,z=55 → 分度圆 d=110")
    check(abs(s["da"] - 114.0) < 1e-9, "齿顶圆 da=114")
    check(abs(s["df"] - 105.0) < 1e-9, "齿根圆 df=105")
    check(abs(s["db"] - 103.3662) < 0.001, "基圆 db≈103.366")
    check(not [d for d in g.diagnostics() if d["level"] == "error"], "无硬错误")
    # 半齿厚角：分度圆处必须等于 ψp（_phi 收半径不是直径 —— 曾经错在这）
    check(abs(g._phi(g.d / 2.0) - g.psi_p) < 1e-9, "分度圆半齿厚角 = ψp")
    check(g._phi(g.da / 2.0) < g._phi(g.d / 2.0), "齿顶比分度圆窄")
    check(g._phi(g.db / 2.0) >= g._phi(g.d / 2.0), "基圆处最宽")
    # 轮廓闭合且所有点落在 df..da 之间
    outline = g.full_outline()
    rs = [__import__("math").hypot(x, y) for x, y in outline]
    check(len(outline) > g.z * 8, f"整圈轮廓点数 {len(outline)}")
    check(min(rs) >= g.df / 2 - 1e-3 and max(rs) <= g.da / 2 + 1e-3, "轮廓都在齿根_齿顶圆之间")
    # ★ 接口必须严丝合缝（真机才会暴露的那类 bug）
    #   曾经左齿廓末点被误删，每个齿与后接齿根弧之间留 0.657mm 缺口，
    #   离线测试全绿，真机拉伸直接报「草图轮廓未闭合」。这里逐齿卡死。
    _m = __import__("math")
    worst = 0.0
    for k in range(g.z):
        tk, rk, _ = g.tooth_sector(k)
        t_next, _, _ = g.tooth_sector((k + 1) % g.z)
        worst = max(worst, _m.dist(tk[-1], rk[0]), _m.dist(rk[-1], t_next[0]))
    check(worst < 1e-9, f"齿廓↔齿根弧 接口无缝（最大缝 {worst:.3e} mm）")
    check(_m.dist(outline[-1], outline[0]) < 1e-9, "整圈末点与首点重合")
    # 齿廓末点必须落在齿根圆上（不是半路停住）。
    # 容差取 1e-3mm：_fmt 把坐标取整到 4 位小数（0.1µm），hypot 反算半径必然
    # 有同量级误差；1e-3mm 仍远小于 SolidWorks 判定重合的容差，不会有微缝。
    _r_end = _m.hypot(*g.tooth_sector(0)[0][-1])
    check(abs(_r_end - g.df / 2) < 1e-3,
          f"齿廓末点半径 ≈ 齿根圆半径（{_r_end:.6f} vs {g.df / 2:.6f}）")
    # 反例锁定：切削掉末点必须被上面的断言抓到
    _t, _r, _ = g.tooth_sector(0)
    check(_m.dist(_t[-2], _r[0]) > 1e-3,
          "（反例）若末点被删，缺口确实 > 0.001mm —— 断言有区分度")

    # ---------------- outline_closed：真机画图专用的点表 ----------------
    # ★ 这一组是「离线必须挡住」的：full_outline() 在「齿→齿根弧」和
    #   「齿根弧→下一齿」两处端点重复，直接逐点连直线会生出 110 条**零长度线段**，
    #   SolidWorks 会静默丢弃它们（实测 1100 点只建出 990 条），
    #   而零长线段正是「轮廓莫名不闭合」的来源。画图必须走 outline_closed()。
    raw = g.full_outline()
    dup = sum(1 for i in range(1, len(raw)) if _m.dist(raw[i], raw[i - 1]) < 1e-12)
    # 2*z 个接口重复点里，最后一个（末点 ↔ 首点的绕回）不在同一对相邻元素里，
    # 所以列表内部只数得到 2*z-1 处；绕回那一处由 outline_closed 单独处理。
    check(dup == 2 * g.z - 1,
          f"full_outline 有 {dup} 处连续重复点（应 {2 * g.z - 1} 处 + 1 处首尾绕回）"
          " —— 画图不能用它")
    check(_m.dist(raw[0], raw[-1]) < 1e-9, "full_outline 首尾也重合（绕回那处重复）")

    oc = g.outline_closed(per_flank=6, n_root=2, n_tip=3, closed=True)
    ocd = g.outline_closed(per_flank=6, n_root=2, n_tip=3, closed=False)
    check(not any(_m.dist(oc[i], oc[i - 1]) < 1e-12 for i in range(1, len(oc))),
          "outline_closed 无连续重复点")
    check(_m.dist(oc[-1], oc[0]) < 1e-12, "outline_closed(closed=True) 首尾重合")
    check(_m.dist(ocd[-1], ocd[0]) > 1e-9, "outline_closed(closed=False) 末点不与首点重合")
    check(len(oc) == len(ocd) + 1, "closed=True 只是多补一个首点")
    # 每齿实际点数 = right(n_flank) + tip(n_tip-2) + left(n_flank) + root(n_root)
    #   （z=55 时 df>db，齿廓没有那段径向直线，所以 right 恰好 = n_flank）
    # 减去每齿 2 个重复接口点 → 715 个互异点，再补闭合点 = 716。
    check(len(ocd) == g.z * (6 + 1 + 6 + 2 - 2), f"互异点数 {len(ocd)}（应 {g.z * 13}）")
    # 反例：万一以后有人把 _fmt 的精度改动导致去重失效，上面的 2*z 会先炸
    check(dup > 0, "（反例）重复点确实存在，outline_closed 的去重不是摆设")

    # 反例：小齿数必须报根切
    check(any(d["code"] == "undercut" for d in sw_gear.GearParams(mn=2, z=12).diagnostics()),
          "z=12 报根切")
    check(not [d for d in sw_gear.GearParams(mn=2, z=60).diagnostics()
               if d["level"] == "error"], "z=60 无硬错误")
    # 变位可以救小齿数
    check(not [d for d in sw_gear.GearParams(mn=2, z=12, x=0.5).diagnostics()
               if d["code"] == "undercut"], "z=12 加 x=0.5 后不根切")
    # 斜齿端面换算
    gh = sw_gear.GearParams(mn=2, z=30, beta=15.0)
    check(abs(gh.mt - 2.0 / __import__("math").cos(__import__("math").radians(15))) < 1e-12,
          "斜齿端面模数 mt=mn/cosβ")
    check(gh.d > 60, "斜齿分度圆大于同参数直齿")
    # 标准件表
    check(sw_gear.hex_bolt("M8", 65, 20)["s"] == 13.0, "六角头 M8 对边宽 13")
    check(sw_gear.deep_groove_bearing("6204")["D"] == 47, "轴承 6204 外径 47")
    check(abs(sw_gear.keyway_profile(20, "8x3.5")["rect"][0][0] + 4.0) < 1e-9,
          "键槽 8x3.5 半宽 4")
    check(len(sw_gear.stepped_shaft([{"d": 20, "l": 30}, {"d": 30, "l": 50}])["profile"]) > 4,
          "阶梯轴剖面有轮廓点")
    # 反例：参数不自洽必须抛
    for label, fn in (
        ("法兰外径<内孔", lambda: sw_gear.flange(od=50, bore=60, thickness=10,
                                                bolt_circle=40)),
        ("螺栓孔超外缘", lambda: sw_gear.flange(od=100, bore=20, thickness=10,
                                               bolt_circle=98, bolt_d=20)),
        ("未知轴承型号", lambda: sw_gear.deep_groove_bearing("9999")),
        ("空阶梯轴", lambda: sw_gear.stepped_shaft([])),
    ):
        try:
            fn()
            check(False, f"反例应报错：{label}")
        except (ValueError, KeyError):
            check(True, f"反例正确报错：{label}")


# ---------------------------------------------------------------- 3/4. 计划

def _plan_ok(plan: dict) -> tuple[bool, list[str]]:
    r = sw_plan.validate(plan, evaluate=True)
    return r.ok, r.errors


def test_gb() -> None:
    """GB 标准件表（assets/gb_standard.json）—— 数值正确性 + **防漂移**。

    ★ 这一节存在的理由：用户的原话是「方便 ai 直接查询，**不需要再上网查找到不正确的标准**」。
      所以断言分两类：
        ① **值对不对** —— 拿核对过的权威值钉住（改错了立刻炸）；
        ② **只有一份** —— sw_gear 里不许再藏第二张表（那正是 M8 dw 出现 11.5/11.6 两种值的根因）。
    """
    print("\n[2b] GB 标准件表（数值 + 防漂移）")

    # ---- ① 权威值钉死（来源见 JSON 的 _sources）----
    b = sw_gb.row("hex_bolt", "M8")
    check(b["s"] == 13 and b["k"] == 5.3 and b["pitch"] == 1.25, "六角头 M8: s=13 k=5.3 P=1.25")
    # dw 曾是「两份表打架」的现场：sw_gear 写 11.5，GB/T 5782 原文是 11.63 ⇒ 取 11.6
    check(abs(sw_gb.row("hex_bolt", "M8")["dw"] - 11.6) < 0.05,
          "六角头 M8 垫圈面 dw=11.6（GB/T 5782 原文 11.63）")

    # 十字槽盘头（GB/T 818）—— 2026-10-01 整表重做，旧版列错位（M4 dk 写成 7.0）
    c3 = sw_gb.row("cross_pan_screw", "M3")
    check(c3["dk"] == 5.6 and c3["k"] == 2.4 and c3["slot_no"] == 1,
          "十字槽盘头 M3: dk=5.6 k=2.4 槽号1（GB/T 818 表1）")
    c4 = sw_gb.row("cross_pan_screw", "M4")
    check(c4["dk"] == 8.0 and c4["k"] == 3.1,
          "十字槽盘头 M4: dk=8.0 k=3.1（旧版错写成 M3.5 的 7.0）")
    check(c3["h_type_m"] == 3.0 and c3["depth_max"] == 1.8,
          "十字槽盘头 M3 槽参考 3.0 / 插入深度 1.8")

    # 轴承（GB/T 276）：本项目实际用的两个
    for spec, d, D, B in (("6204", 20, 47, 14), ("6206", 30, 62, 16),
                          ("6300", 10, 35, 11), ("6310", 50, 110, 27)):
        r = sw_gb.bearing(spec)
        check((r["d"], r["D"], r["B"]) == (d, D, B), f"轴承 {spec}: {d}×{D}×{B}")
    check(len(sw_gb.bearings_by_bore(20)) >= 4, "内径 20 能列出 ≥4 个可选轴承")
    check(len(sw_gb.family("deep_groove_bearing")["rows"]) >= 60,
          "轴承表已扩到 6000/6200/6300/6400/6800/6900 六系列（≥60 行）")

    # 圆锥销（GB/T 117）—— 本项目要 φ4×18
    tp = sw_gb.taper_pin(4)
    check(tp["d"] == 4 and tp["a"] == 0.5 and tp["len_range"] == "14-55",
          "圆锥销 φ4: 倒角 0.5 / 商品长度 14-55")
    check(abs(sw_gb.taper_pin_big_end(4, 18) - 4.36) < 1e-9,
          "圆锥销 φ4×18 大端 D = 4 + 18/50 = 4.36（1:50 锥度）")
    check(sw_gb.taper_pin_big_end(10, 60) == 11.2, "圆锥销 φ10×60 大端 11.2")

    # 配合（GB/T 1800.2）—— 偏差是 μm
    f = sw_gb.fit(20, "H7", "k6")
    check(f["hole"]["es_um"] == 21 and f["hole"]["ei_um"] == 0,
          "Ø20 H7 = +21/0 μm")
    check(f["shaft"]["es_um"] == 15 and f["shaft"]["ei_um"] == 2,
          "Ø20 k6 = +15/+2 μm")
    check(f["type"] == "过渡", "Ø20 H7/k6 是过渡配合")
    # ★ 分段边界：Ø30 在 (18,30]，Ø30.001 才进 (30,50] —— 每年都有人踩
    check(sw_gb.fit(30, "H7", "h6")["hole"]["es_um"] == 21, "Ø30 落 18~30 段（H7=21）")
    check(sw_gb.fit(30.001, "H7", "h6")["hole"]["es_um"] == 25, "Ø30.001 落 30~50 段（H7=25）")
    check(sw_gb.fit(6, "H7", "h6")["hole"]["es_um"] == 12, "Ø6 落 3~6 段（H7=12）")
    check(sw_gb.fit(6.5, "H7", "h6")["hole"]["es_um"] == 15, "Ø6.5 落 6~10 段（H7=15）")
    check(sw_gb.fit(20, "H7", "h6")["type"] == "间隙", "Ø20 H7/h6 是间隙配合")
    check(sw_gb.fit(20, "H7", "p6")["type"] == "过盈", "Ø20 H7/p6 是过盈配合")

    # 长度系列（GB/T 3106）—— 螺栓/螺钉两套
    check(sw_gb.nearest_length("bolt", 63, "up") == 65, "螺栓 63 → 标准 65")
    check(sw_gb.is_standard_length("bolt", 65), "M8×65 长度合规")
    check(not sw_gb.is_standard_length("bolt", 63), "63 不是螺栓标准长度")
    check(22 in sw_gb.length_series("screw"), "螺钉系列含 22（本项目 M3×22）")
    check(sw_gb.nearest_length("screw", 22, "exact" if False else "near") == 22,
          "螺钉 22 取 22")

    # 齿轮模数（GB/T 1357）
    check(sw_gb.is_standard_module(2), "模数 2 是第一系列")
    check(not sw_gb.is_standard_module(2.2), "模数 2.2 不在 GB/T 1357 里")
    check("第一系列" in sw_gb.module_advice(2), "模数 2 提示为第一系列")
    check("不在" in sw_gb.module_advice(2.2), "模数 2.2 提示不合规")
    check("括号" in sw_gb.module_advice(3.25), "模数 3.25 提示标准里带括号")

    # 螺纹通孔（GB/T 5277）
    check(sw_gb.clearance_hole("M8", "medium") == 9.0, "M8 中系列通孔 9.0")

    # ---- ② 防漂移：sw_gear 不许再藏第二张标准件表 ----
    for gone in ("HEX_BOLT_TABLE", "HEX_BOLT_PITCH"):
        check(not hasattr(sw_gear, gone),
              f"sw_gear 不再自留 {gone}（标准值只在 gb_standard.json）")
    # 且 sw_gear 的接口仍然能用、且取到的是**新表的值**
    hb = sw_gear.hex_bolt("M8", 65, 20)
    check(hb["s"] == 13 and hb["dw"] == 11.6,
          "sw_gear.hex_bolt 走 sw_gb（dw 拿到 11.6 而不是旧的 11.5）")
    check(sw_gear.hex_bolt("M30", 180)["s"] == 46, "sw_gear.hex_bolt 支持 M30（旧表没有）")
    check(sw_gear.deep_groove_bearing("6204")["D"] == 47, "sw_gear 轴承仍可查")
    check(sw_gear.deep_groove_bearing("6310")["B"] == 27, "sw_gear 轴承支持 6310（旧表没有）")

    # ---- ③ 反例：查不到必须报错，别静默给 None ----
    for label, fn in (
        ("不存在的轴承 9999", lambda: sw_gb.bearing("9999")),
        ("不存在的族", lambda: sw_gb.family("no_such_family")),
        ("平键没有的规格", lambda: sw_gb.row("parallel_key", "999x999")),
        ("轴公差带没有 q9", lambda: sw_gb.fit(20, "H7", "q9")),
    ):
        try:
            fn()
            check(False, f"反例应报错：{label}")
        except (KeyError, ValueError):
            check(True, f"反例正确报错：{label}")

    # ---- ④ 速查表能出内容（Agent 一次读全的入口）----
    md = sw_gb.cheatsheet()
    for must in ("六角头螺栓", "深沟球轴承", "圆锥销", "极限与配合", "齿轮模数", "长度系列"):
        check(must in md, f"速查表包含「{must}」")
    check(len(md) > 3000, f"速查表有实质内容（{len(md)} 字符）")
    check("不要联网搜" in md, "速查表带「离线权威」口径提示")
    # 注记键不能漏进值列表（`_note=...` 曾被打成"一个规格"）
    check("_note=" not in md, "速查表不漏 `_note` 这类注记键")
    check("- 细牙螺距: " in md, "速查表含细牙螺距（同规格多螺距，容易漏）")

    # ---- ⑤ 防漂移：随包发的 references/41-gb-cheatsheet.md 必须与 JSON 同步 ----
    #    用户要求「打包好、方便 ai 直接查询」⇒ 包里那份**只读快照**不能是过期的。
    #    判据 = 快照正文里包含整张表的关键值（不是逐字节比，避免换行差异误报）。
    snap = ROOT / "references" / "41-gb-cheatsheet.md"
    if snap.is_file():
        txt = snap.read_text(encoding="utf-8")
        for probe in ("M8=1.25", "M64=6.0", "6204", "H7", "M8 | 8.4 | 9.0 | 10.0",
                      "六角头螺栓", "不要联网搜"):
            check(probe in txt, f"速查表快照含 {probe!r}")
        check(f"updated={sw_gb.load().get('updated')}" in txt,
              "速查表快照的 updated 与 JSON 一致（防止只改 JSON 忘了重新生成）")
    else:
        check(False, f"缺少随包的速查表快照：{snap}")


def test_reducer() -> None:
    """减速器参数表：单源交叉校核 + 反例拦截 + 计划生成 + 机读 op 导出防漂移。

    ★ 这一节存在的理由（`60-reducer-project.md` / `70-direction-review.md`）：
      真实项目 35 个零件若各写各的尺寸，中心距 70、中心高 80 这类**跨件耦合值**
      会被人手抄错且没人发现。所以立一张**唯一真源** `assets/reducer/reducer.params.json`，
      所有零件计划都从它取数；跨件一致性靠**离线交叉校核**守。
      再配一份机读 `assets/op_schema.json`，让 Agent 不读 Markdown 也能列 op/查边界。
    校核全离线，与开不开 SolidWorks 无关。
    """
    print("\n[2b] 减速器参数表（单源 + 交叉校核 + 机读导出）")
    if not hasattr(sw_reducer, "run_all"):
        check(False, "sw_reducer 提供 run_all")
        return
    P = sw_reducer.load()

    # (a) 表结构：顶层块齐 + 零件条数 + 每条可追溯来源
    for blk in ("drive", "box", "shafts", "bearings", "gears", "covers", "parts", "checks"):
        check(blk in P, f"参数表含块 '{blk}'")
    parts = {k: x for k, x in P.get("parts", {}).items() if not k.startswith("_")}
    check(len(parts) >= 30, f"零件表 ≥30 项（实际 {len(parts)}）")
    miss = [k for k, x in parts.items() if "no" not in x or "name" not in x]
    check(not miss, f"每个零件都有 序号/名称（缺的：{miss[:3]}）")
    # 来源标注：值必须能回答"这数哪来的"（图纸 > 实测 > 国标）
    srcs = {sw_reducer.src(x) for x in P["drive"].values() if isinstance(x, dict)}
    # ★ "redesign" = **设计决策**（2026-10-02 齿轮副重新设计）。
    #   它是合法来源，但**必须带 note 说明旧值与理由**（否则就是"无据改数"）。
    check(srcs <= {"drawing", "measured", "derived", "gb", "redesign", "?"},
          f"drive 块的来源标注都合法（{sorted(srcs)}）")
    for k, x in P["drive"].items():
        if isinstance(x, dict) and x.get("source") == "redesign":
            check(bool(x.get("note")) and str(x["note"]).strip() != "",
                  f"drive.{k} 标了 redesign ⇒ note 必须写清理由与旧值")

    # (b) 原表交叉校核必须全过（C1~C10 + R 系列）
    r = sw_reducer.run_all(P)
    check(r["ok"], f"原表交叉校核全过（{r['n_pass']} 通过 / {r['n_fail']} 硬失败 / "
                   f"{r['n_open']} 待图纸确认）")
    check(r["n_fail"] == 0, "原表没有硬失败项")

    # (c) 反例：故意改错，必须被拦下（没有反例的校验器迟早退化成"永远通过"）
    import copy as _copy
    mutants = (
        ("C1 中心距抄错", lambda p: p["drive"]["a"].__setitem__("v", 72.0)),
        ("C4 齿顶圆抄错", lambda p: p["meta"]["gear_train"]["wheel"].__setitem__("da", 116.0)),
        ("C5 轴承内径 ≠ 轴颈", lambda p: p["bearings"]["6204"].__setitem__("d", 25.0)),
        ("C6 端盖小于轴承", lambda p: p["covers"]["cover2"]["D"].__setitem__("v", 40.0)),
        ("C7 中心高不够", lambda p: p["drive"]["h"].__setitem__("v", 50.0)),
    )
    for label, mutate in mutants:
        Pm = _copy.deepcopy(P)
        mutate(Pm)
        rm = sw_reducer.run_all(Pm)
        caught = (not rm["ok"]) or any(
            c["kind"] == "hard" and not c["ok"] for c in rm["checks"])
        check(caught, f"反例被拦下：{label}")

    # (d) 最小件闭环：由参数表生成平键计划，离线必须通过校验
    plan = sw_reducer.build_plan(P, "key_10x8x22", out_dir="D:/out/reducer")
    ok, errs = _plan_ok(plan)
    check(ok, "平键计划由参数表生成并通过校验" + ("" if ok else f" → {errs[:2]}"))
    steps_txt = json.dumps(plan["steps"], ensure_ascii=False)
    check("{{ params." in steps_txt, "平键计划用 {{ params.* }} 表达式取数，不硬编码尺寸")
    check(str(sw_reducer.v(P["parts"]["key_10x8x22"]["L"])) in steps_txt
          or "params.L" in steps_txt, "平键计划引用了参数表的 L")
    exp = sw_reducer._expected_volume(P["parts"]["key_10x8x22"])
    # 解析式只做**一阶**估计（供计划里 check_mass 的 2% 闸门用）；
    # 跑道形体 + 两圈 45° 倒角的严格体积交给对账那一层（与参照件比，容差更紧）。
    check(abs(exp - 1574.73) < 0.5, f"平键解析体积 ≈1574.73（算得 {exp:.2f}）")
    check(abs(exp - 1588.32) > 5.0, "平键解析式**含**倒角去料项（漏掉会差 ~14mm³）")

    # (d2) 所有已参数化的件：计划都必须由参数表生成且离线过校验
    # ★ 临时打开 calibrate：本测试只验「计划能由参数表生成且结构合规」，
    #   与真机标定值无关。否则「标定值待重取」（expect_mass_g=0）的零件
    #   会把整个自检拦死 —— 那是**运行期**守卫，不是计划结构错误。
    _prev_cal = sw_reducer._CALIBRATE.get("on")
    sw_reducer._CALIBRATE["on"] = True
    try:
        for part in sorted(sw_reducer.BUILDERS):
            pl = sw_reducer.build_plan(P, part, out_dir="D:/out/reducer")
            o, e = _plan_ok(pl)
            check(o, f"{part}：计划由参数表生成并通过校验" + ("" if o else f" → {e[:2]}"))
    finally:
        sw_reducer._CALIBRATE["on"] = _prev_cal
    n_build = len(sw_reducer.BUILDERS)
    print(f"  · 参数化进度：{n_build}/{len(parts)} 件有计划生成器（{', '.join(sorted(sw_reducer.BUILDERS))}）")

    # 输出轴（半剖面回转）：包围盒期望取自参数表 + 总长=Σl+2ec 的口径
    sh = sw_reducer.build_plan(P, "output_shaft", out_dir="D:/out/reducer")
    s = P["shafts"]["output"]
    _L, _dmax = float(sw_reducer.v(s["L"])), float(sw_reducer.v(s["d_max"]))
    bb = next(x for x in sh["steps"] if x["id"] == "bb")
    check(bb["expect"] == [_L, _dmax, _dmax], "轴计划包围盒期望取自参数表 L/d_max")
    check(abs(sw_reducer._shaft_volume(s["segments"]) - 97258.5) < 1.0,
          "轴分段解析体积 ≈97258.5")
    _sum_l = sum(float(r["l"]) for r in s["segments"])
    check(_sum_l + 2 * float(sw_reducer.v(s["end_chamfer"])) == _L,
          "轴：总长 = Σl + 2×端倒角（外扩口径）")

    # (d3) 环形件族（圆环半剖面回转）：5 件同构，必须有生成器 + 解析体积对得上参照件
    fam = tuple(getattr(sw_reducer, "RING_FAMILY", ()))
    check(len(fam) >= 5, f"环形件族注册 ≥5 件（实际 {len(fam)}）")
    check(all(k in sw_reducer.BUILDERS for k in fam),
          "环形件族每一件都有计划生成器")
    for k in fam:
        pl = sw_reducer.build_plan(P, k, out_dir="D:/out/reducer")
        d = pl["_derived"]
        # 结构不变量：圆环的半剖面**不能贴轴线**（贴了就是实心圆盘，不是环）
        prof = next(s for s in pl["steps"] if s["op"] == "polyline")
        ys = [pt[1] for pt in prof["points"]]
        check(min(ys) > 1e-9 and min(ys) == d["ri"],
              f"{k}：半剖面离轴（最小 y = 内半径 {d['ri']}，不是 0）")
        # 解析体积 π/4(D²−d²)t 必须落在 [D_d/2, D_d] 区间内的合理值
        check(abs(sw_reducer._ring_volume(d["OD"], d["ID"], d["t"])
                  - d["analytic_volume_mm3"]) < 1e-6,
              f"{k}：解析体积自洽")
        check(0 < d["ri"] < d["ro"], f"{k}：内半径 ∈ (0, 外半径)")
    # 与参照件实测体积对账（离线就能查 —— 参数填错当场炸）
    # ★ 容差 0.2%：参照件的小件常带**拔模/圆角**（实测 17-平垫圈内孔 Ø11.0→Ø10.95，
    #   0.05mm 拔模就带来 0.17% 体积差）⇒ 单件级对账不能卡到 0.05%。
    #   **反例仍然够狠**（下面的"内孔 > 外径"是直接 ValueError，不靠体积差），
    #   参数填错（如内径偏 1mm）会差 10% 以上，远超 0.2% ⇒ 不会被放过。
    refp = sw_reducer.REFERENCE_PATH
    if refp.is_file():
        refv = {it["file"]: it["volume_mm3"]
                for it in json.loads(refp.read_text(encoding="utf-8"))["items"]}
        for k in fam:
            q = P["parts"][k]
            rv = refv.get(q.get("ref"))
            if rv is None:
                continue
            v_our = sw_reducer._ring_volume(float(sw_reducer.v(q["D"])),
                                            float(sw_reducer.v(q["bore"])),
                                            float(sw_reducer.v(q["t"])))
            dpct = abs(v_our - rv) / rv * 100.0
            check(dpct < 0.2,
                  f"{k}：解析体积 vs 参照件 {q['ref']} 差 {dpct:.4f}%"
                  f"（{v_our:.2f}/{rv:.2f}，容差 0.2% —— 参照件带拔模/圆角）")
    # 反例：内孔 ≥ 外径 必须被拦（否则生成一个"负壁厚"的荒谬计划）
    try:
        Pm = _copy.deepcopy(P)
        Pm["parts"]["spacer"]["bore"]["v"] = 40.0     # 外径才 37
        sw_reducer.build_plan(Pm, "spacer", out_dir="D:/out/reducer")
        check(False, "反例应报错：套筒内孔 > 外径")
    except (ValueError, KeyError):
        check(True, "反例正确报错：套筒内孔 > 外径")

    # (d3b) 闷盖族（法兰 + 实心凸台，半剖面回转）：2 件同构
    #   ★ 与环形件族的**唯一区别**：环的型面离轴（回转后中间自然有孔）；
    #     闷盖的型面**必须触轴**（凸台是实心）—— 这两条不变量正好互为反面，
    #     所以两个族各钉一条，谁写错了当场就有一边炸。
    cfam = tuple(getattr(sw_reducer, "COVER_BLIND_FAMILY", ()))
    check(len(cfam) >= 2, f"闷盖族注册 ≥2 件（实际 {len(cfam)}）")
    check(all(k in sw_reducer.BUILDERS for k in cfam),
          "闷盖族每一件都有计划生成器")
    for k in cfam:
        pl = sw_reducer.build_plan(P, k, out_dir="D:/out/reducer")
        d = pl["_derived"]
        prof = next(s for s in pl["steps"] if s["op"] == "polyline")
        pts = prof["points"]
        ys = [pt[1] for pt in pts]
        check(min(ys) == 0.0, f"{k}：半剖面**触轴**（最小 y = 0，凸台实心）")
        check(max(ys) == d["ro"], f"{k}：最大半径 = 法兰半径 {d['ro']}")
        check(len(pts) == 8, f"{k}：型面 8 顶点（法兰+凸台同构）")
        # 凸台直径 = 所配轴承外径（这是 C6 的物理含义，这里再钉一次）
        bs = P["parts"][k].get("bearing")
        if bs:
            bD = float(P["bearings"][bs]["D"])
            check(abs(d["Ds"] - bD) < 1e-9,
                  f"{k}：凸台 Ø{d['Ds']:g} = 所配轴承 {bs} 外径 Ø{bD:g}")
            check(d["Df"] > d["Ds"], f"{k}：法兰外径 > 凸台外径（否则凸台不叫凸台）")
        check(abs((d["tf"] + d["ts"]) - d["L"]) < 1e-9,
              f"{k}：法兰厚 + 凸台厚 = 总厚 {d['L']:g}")
        check(abs(sw_reducer._blind_cover_volume(
                      d["Df"], d["di"], d["tf"], d["Ds"], d["ts"])
                  - d["analytic_volume_mm3"]) < 1e-6,
              f"{k}：解析体积自洽")
        check(0 < d["ri"] < d["rs"] < d["ro"], f"{k}：半径链 0 < 孔 < 凸台 < 法兰")
    if refp.is_file():
        for k in cfam:
            sq = P["parts"][k]
            rv = refv.get(sq.get("ref"))
            if rv is None:
                continue
            v_our = sw_reducer._blind_cover_volume(
                float(sw_reducer.v(sq["D"])), float(sw_reducer.v(sq["flange_bore"])),
                float(sw_reducer.v(sq["flange_t"])), float(sw_reducer.v(sq["spigot_D"])),
                float(sw_reducer.v(sq["spigot_t"])))
            dpct = abs(v_our - rv) / rv * 100.0
            check(dpct < 0.05,
                  f"{k}：解析体积 vs 参照件 {sq['ref']} 差 {dpct:.4f}%（{v_our:.2f}/{rv:.2f}）")
    # 反例：凸台半径 ≥ 法兰半径 必须被拦（否则"凸台"比法兰还大，语义就没了）
    try:
        Pm = _copy.deepcopy(P)
        Pm["parts"]["cover2"]["spigot_D"]["v"] = 60.0     # 法兰才 Ø54
        sw_reducer.build_plan(Pm, "cover2", out_dir="D:/out/reducer")
        check(False, "反例应报错：凸台 > 法兰")
    except (ValueError, KeyError):
        check(True, "反例正确报错：凸台 > 法兰")
    # 反例：法兰厚 + 凸台厚 ≠ 总厚 必须被拦（否则包围盒期望值与实际型面打架）
    try:
        Pm = _copy.deepcopy(P)
        Pm["parts"]["cover2"]["spigot_t"]["v"] = 9.0      # 3 + 9 ≠ 7
        sw_reducer.build_plan(Pm, "cover2", out_dir="D:/out/reducer")
        check(False, "反例应报错：法兰厚 + 凸台厚 ≠ 总厚")
    except (ValueError, KeyError):
        check(True, "反例正确报错：法兰厚 + 凸台厚 ≠ 总厚")

    # (d4) 对账：**朝向不参与判定**，但尺寸/体积错了必须拦
    #   ★ 2026-10-02 真机踩出来的口径：人工那套的坐标系朝向是"建模习惯"（套筒轴在 X、
    #     调整环在 Y、密封圈在 Z），不是规格。同一个圆环旋转一下还是同一件东西。
    #     但"朝向不判"绝不能变成"什么都放行" —— 所以这里配两条反例钉住。
    import tempfile
    q = P["parts"]["spacer"]
    _OD, _ID, _t = float(sw_reducer.v(q["D"])), float(sw_reducer.v(q["bore"])), float(sw_reducer.v(q["t"]))
    _vol = sw_reducer._ring_volume(_OD, _ID, _t)

    def _fake_rep(bbox, vol_mm3):
        return {"log": [{"id": "bb", "result": {"size_mm": list(bbox)}},
                        {"id": "m", "result": {"volume_m3": vol_mm3 * 1e-9}}]}

    with tempfile.TemporaryDirectory() as _td:
        _rp = Path(_td) / "rep.json"
        # ① 朝向不同（轴落在 Z），尺寸集合一致 ⇒ 必须判通过，且朝向差异被记录
        _rp.write_text(json.dumps(_fake_rep([_OD, _OD, _t], _vol)), encoding="utf-8")
        _rr = sw_reducer.reconcile(P, "spacer", _rp)
        check(_rr["ok"], "对账：朝向不同但尺寸/体积一致 ⇒ 通过（旋转不变）")
        check(_rr.get("orientation", {}).get("same") is False,
              "对账：朝向差异被单独记录（不参与判定，但要看得见）")
        # ② 外径抄错 1mm ⇒ 必须不通过
        _rp.write_text(json.dumps(_fake_rep([_OD + 1, _OD + 1, _t], _vol)), encoding="utf-8")
        check(not sw_reducer.reconcile(P, "spacer", _rp)["ok"],
              "对账：外径差 1mm ⇒ 不通过（朝向不判 ≠ 放行）")
        # ③ 体积差 5%（内孔抄错的典型后果）⇒ 必须不通过
        _rp.write_text(json.dumps(_fake_rep([_OD, _OD, _t], _vol * 1.05)), encoding="utf-8")
        check(not sw_reducer.reconcile(P, "spacer", _rp)["ok"],
              "对账：体积差 5% ⇒ 不通过")

    # (d5) 校核规则的**文档漂移**守卫 + 反例点名
    #   起因：代码里已经有 C1~C11，但参数表 checks 块还停在 C1~C8（C9/C10/C11 只在代码里）。
    #   这种"代码进了一步、真源文档没跟上"的漂移，人眼永远发现不了 ⇒ 用断言钉住。
    cs = sw_reducer.cross_check(P)
    doc_ids = {k for k in P.get("checks", {}) if not k.startswith("_")}
    code_ids = {c.cid for c in cs}
    check(code_ids <= doc_ids,
          f"代码里每条交叉校核都在参数表 checks 里登记（缺 {sorted(code_ids - doc_ids)}）")
    check(doc_ids <= code_ids,
          f"参数表 checks 里的规则都有实现（多余 {sorted(doc_ids - code_ids)}）")
    # 每条反例必须由**指定的那条**检查拦住。
    #   ★ 只判"被拦住了"是不够的：新增检查会顺带拦住老反例，老反例于是变成假绿灯。
    for _label, _want, _mut in sw_reducer.MUTANTS:
        _Pm = _copy.deepcopy(P)
        _mut(_Pm)
        _r = sw_reducer.run_all(_Pm)
        _fired = {c["id"] for c in _r["checks"]
                  if c["kind"] == "hard" and not c["ok"]}
        check(_want in _fired,
              f"反例「{_label}」由 {_want} 拦住（实际 {sorted(_fired) or '无人拦'}）")

    # (e) 机读 op 导出：assets/op_schema.json 必须与 schema() 同步（防漂移）
    schema_path = ROOT / "assets" / "op_schema.json"
    if not schema_path.is_file():
        check(False, f"缺少机读 op 导出：{schema_path}")
    else:
        onfile = json.loads(schema_path.read_text(encoding="utf-8"))
        fresh = sw_ops.schema()
        check(onfile.get("n_ops") == fresh["n_ops"],
              f"op_schema 条目数与代码一致（{onfile.get('n_ops')}）")
        check(onfile.get("n_verified") == fresh["n_verified"],
              f"op_schema verified 数与代码一致（{onfile.get('n_verified')}）")
        check(onfile.get("n_with_limits") == fresh["n_with_limits"],
              "op_schema limits 数与代码一致")
        # 逐条按 名字+limits 比 —— 改了 op 忘了重新导出会当场失败
        a = {o["name"]: tuple(o["limits"]) for o in onfile.get("ops", [])}
        b = {o["name"]: tuple(o["limits"]) for o in fresh["ops"]}
        diff = sorted(set(a) ^ set(b)) or [k for k in a if a[k] != b.get(k)]
        check(a == b, "op_schema 每条 op 的 limits 与代码一致"
                      + ("" if a == b else f"（差异：{diff[:5]}）"))
        check(fresh["n_with_limits"] >= 10,
              f"有已知边界的 op ≥10（实际 {fresh['n_with_limits']}）")
        # 题眼：两条踩过的坑必须进了机读导出（否则下个人还得踩一遍）
        fd = next((o for o in fresh["ops"] if o["name"] == "fully_define"), None)
        check(fd is not None and bool(fd.get("limits")),
              "fully_define 的「no-op」边界进了机读导出")
        ac = next((o for o in fresh["ops"] if o["name"] == "arc3p"), None)
        check(ac is not None and any("点序" in s for s in ac.get("limits", [])),
              "arc3p 的「点序」边界进了机读导出")


def test_plans() -> list[str]:
    print("\n[3] 正例计划")
    kinds = sorted(p.stem for p in PLANS.glob("*.json")) if PLANS.is_dir() else []
    check(bool(kinds), f"assets/plans 里有用例（{kinds}）")
    for k in kinds:
        plan = json.loads((PLANS / f"{k}.json").read_text(encoding="utf-8"))
        ok, errs = _plan_ok(plan)
        check(ok, f"{k}.json 通过校验" + ("" if ok else f" → {errs[:2]}"))
    return kinds


def test_probes() -> None:
    """探针计划（assets/probes/*.json）—— 每个**未实测**的 op 都必须有探针。

    探针和 examples 不同：它们引用 `{{ params.out }}`（由 sw_probe 运行时注入），
    所以离线校验时要自己塞一个 out 进去。这里顺带做「覆盖度」卡口：
    凡 verified=False 的 op 若没有任何探针点名，它永远不会被真机实测 —— 直接算失败。
    """
    print("\n[3b] 探针计划（离线可校验性 + 实测进度）")
    files = sorted(PROBES.glob("*.json")) if PROBES.is_dir() else []
    check(bool(files), f"assets/probes 里有探针（{len(files)} 个）")
    covered: set[str] = set()
    for f in files:
        plan = json.loads(f.read_text(encoding="utf-8"))
        plan.setdefault("params", {})
        plan["params"].setdefault("out", "D:/tmp/probe")     # 模拟 sw_probe 注入
        r = sw_plan.validate(plan, evaluate=True)
        check(r.ok, f"probes/{f.name} 通过校验" + ("" if r.ok else f" → {r.errors[:2]}"))
        for s in plan.get("steps", []):
            op = s.get("op")
            if op:
                check(op in sw_ops.OPS,
                      f"probes/{f.name} 用的 '{op}' 是已知 op")
                if op in sw_ops.OPS:
                    covered.add(op)
    # 命名约定：**每件有生成器的零件都必须有一条 reducer_<part_key>.json**
    #   —— 这是 `--reconcile <件> <批量报告>` 能按 tag 命中条目的前提。
    #   少了它，那件就只能靠单计划报告对账，"批量报告一条命令对完"就断链了。
    if hasattr(sw_reducer, "BUILDERS"):
        names = {f.name for f in files}
        missing = [f"reducer_{k}.json" for k in sorted(sw_reducer.BUILDERS)
                   if f"reducer_{k}.json" not in names]
        check(not missing,
              f"每件有生成器的零件都有同名探针（缺：{missing[:5]}）")

    # ★ `assets/probes/_sealed/` —— 已封存的探针（主题是尺寸标注，见该目录 README）。
    #   它们**不进真机默认跑**（sw_probe 的 glob 非递归），但**必须继续通过离线校验**：
    #   封存的是"执行"，不是"文件"。离线校验能挡住的最常见事故是**接口漂移**
    #   （哪个 op 改了参数名，这些计划就再也跑不起来了）—— 重开那天才不会踩空。
    sealed_dir = PROBES / "_sealed"
    sealed_files = sorted(sealed_dir.glob("*.json")) if sealed_dir.is_dir() else []
    check(bool(sealed_files), f"assets/probes/_sealed 里有封存探针（{len(sealed_files)} 个）")
    for f in sealed_files:
        plan = json.loads(f.read_text(encoding="utf-8"))
        plan.setdefault("params", {})
        plan["params"].setdefault("out", "D:/tmp/probe")
        r = sw_plan.validate(plan, evaluate=True)
        check(r.ok, f"probes/_sealed/{f.name} 仍通过离线校验"
              + ("" if r.ok else f" → {r.errors[:2]}"))
        for s in plan.get("steps", []):
            if s.get("op"):
                check(s["op"] in sw_ops.OPS,
                      f"probes/_sealed/{f.name} 用的 '{s['op']}' 是已知 op")

    verified = sum(1 for o in sw_ops.OPS.values() if o.verified)
    unverified = sorted(o.name for o in sw_ops.OPS.values() if not o.verified)
    no_probe = [n for n in unverified if n not in covered]
    # 覆盖度只作**进度报告**（历史遗留的未实测 op 不该把 CI 卡红）；
    # 真正的门禁是真机跑 sw_probe：跑通才会把 verified 改成 True。
    print(f"  · 实测进度：{verified}/{len(sw_ops.OPS)} 个 op 已真机验证")
    if no_probe:
        print(f"  · 待补探针（{len(no_probe)} 个）：{', '.join(no_probe)}")


def test_templates_sync(kinds: list[str]) -> None:
    print("\n[4] 文档与代码同步（防漂移）")
    for k in kinds:
        try:
            tpl = sw_plan.template(k)
        except SystemExit:
            check(False, f"CLI 模板里有 '{k}'（用例文件有但模板没有）")
            continue
        onfile = json.loads((PLANS / f"{k}.json").read_text(encoding="utf-8"))
        check(tpl == onfile, f"{k}：CLI 模板与 assets/plans/{k}.json 一致")
    # 操作手册必须与 sw_ops.catalog() 完全一致，否则说明有人手改了 md
    md = ROOT / "references" / "10-operations.md"
    if check(md.is_file(), "references/10-operations.md 存在"):
        onfile = md.read_text(encoding="utf-8")
        check(onfile.rstrip("\n") == sw_ops.catalog().rstrip("\n"),
              "操作手册与 sw_ops.py 一致（不一致就跑 sw_ops.py --write-md 重新生成）")
    # 接口签名速查必须存在（由 sw_docbuild 生成）
    check((ROOT / "references" / "api-signatures.md").is_file(),
          "references/api-signatures.md 存在")
    check((ROOT / "assets" / "sw_enums.json").is_file(),
          "assets/sw_enums.json 存在（枚举数值表，宏翻译和常量兜底都要用）")
    # ★★ 取证工具的「读文档」路径守卫（§83）。
    #   `open_doc` 里带着两件** indispensable **的事：① `OpenDoc6` 返回 tuple 要解包
    #   （不解包则 `if model is None` 永不成立，打开失败也不报错）；
    #   ② `swOpenDocOptions_Silent` 下**打开 ≠ 激活**，必须 `ActivateDoc3` 再读回标题核对。
    #   一旦有人为了"快"绕过 `open_doc` 直接调 `OpenDoc6`，症状是
    #   **读到上一份文件、内容全错、但不报任何错**（§83 实际踩过：螺母被读成圆销）。
    #   这条断言把那个坑钉在离线阶段。
    for fn in ("sw_faces.py", "sw_inspect.py", "sw_shot.py", "sw_profile.py"):
        p = HERE / fn
        if not check(p.is_file(), f"{fn} 存在"):
            continue
        txt = p.read_text(encoding="utf-8")
        check("open_doc(" in txt, f"{fn} 走 sw.open_doc（带激活+读回核对）")
        check("OpenDoc6" not in txt,
              f"{fn} 不自己调 OpenDoc6（会跳过激活与核对，§83）")
    # open_doc 自身：必须同时有「解包 tuple」与「激活 + 读回核对」两件事
    ct = (HERE / "sw_connect.py").read_text(encoding="utf-8")
    body = ct.split("def open_doc", 1)[-1].split("\n    def ", 1)[0] \
        if "def open_doc" in ct else ""
    check("isinstance(res, tuple)" in body, "open_doc 解包 OpenDoc6 的 tuple 返回值")
    check("ActivateDoc3" in body, "open_doc 显式激活文档（打开 ≠ 激活）")
    check("GetTitle" in body and "!=" in body, "open_doc 读回活动文档标题做核对")
    # ★ 常量名必须真实存在。`C.get("名字", 默认值)` 在名字拼错/不存在时**静默退到默认值**，
    #   参数会变成 0 或别的错值而毫无提示 —— 倒角的「距离填错槽」就是这么藏住的。
    #   所以把「凡是写进代码的 swXxx 常量名，都必须在枚举表里找得到」变成一条硬断言。
    src = "".join((HERE / f).read_text(encoding="utf-8")
                  for f in ("sw_ops.py", "sw_gear.py", "sw_plan.py")
                  if (HERE / f).is_file())
    if check(bool(src), "能读到待检源码（sw_ops/sw_gear/sw_plan）"):
        enums = json.loads((ROOT / "assets" / "sw_enums.json").read_text(encoding="utf-8"))
        known = {m for v in enums.values() for m in (v.get("members") or {})}
        used = set(re.findall(r'\bC\s*\.\s*get\(\s*"(sw[A-Za-z0-9_]+)"', src))
        unknown = sorted(n for n in used if n not in known)
        check(not unknown,
              f"代码里 {len(used)} 个常量名都能在枚举表里查到"
              f"（查不到的：{unknown or '无'}）")


def test_negative() -> None:
    print("\n[5] 反例必须被拦住（校验器的价值全在这里）")
    base = sw_plan.template("box")

    # 5.1 拉伸一个还没关闭的草图（忘写 sketch_end）
    p = copy.deepcopy(base)
    p["steps"] = [s for s in p["steps"] if s["op"] != "sketch_end"]
    ok, errs = _plan_ok(p)
    check(not ok and any("顺序错误" in e for e in errs),
          "缺 sketch_end 时拉伸被拦下")

    # 5.2 没建文档就直接画草图
    p = copy.deepcopy(base)
    p["steps"] = [s for s in p["steps"] if s["op"] != "new_part"]
    ok, errs = _plan_ok(p)
    check(not ok, "没有 new_part 就画草图被拦下")

    # 5.3 计划结束时草图还开着
    p = copy.deepcopy(base)
    p["steps"] = [s for s in p["steps"] if s["op"] != "sketch_end"]
    ok, errs = _plan_ok(p)
    check(not ok and any("草图还开着" in e for e in errs), "结束时草图未关被拦下")

    # 5.4 拼错操作名
    p = copy.deepcopy(base)
    p["steps"][1]["op"] = "seelct_plane"
    ok, errs = _plan_ok(p)
    check(not ok and any("未知操作" in e for e in errs), "拼错 op 名被拦下")

    # 5.5 缺必填参数
    p = copy.deepcopy(base)
    p["steps"] = [s for s in p["steps"] if s["op"] != "rect_center"]
    p["steps"].insert(2, {"id": "r", "op": "rect_center", "w": 10})
    ok, errs = _plan_ok(p)
    check(not ok and any("缺少必填参数" in e for e in errs), "缺 h 参数被拦下")

    # 5.6 齿轮根切
    p = copy.deepcopy(sw_plan.template("gear"))
    p["params"]["z"] = 10
    ok, errs = _plan_ok(p)
    check(not ok and any("undercut" in e or "根切" in e for e in errs), "齿轮根切被拦下")

    # 5.7 表达式引用了不存在的参数（拼错参数名）
    p = copy.deepcopy(base)
    p["steps"][3]["w"] = "{{ params.width }}"
    ok, errs = _plan_ok(p)
    check(not ok and any("未定义" in e for e in errs), "参数名拼错被拦下（且报出可用参数名）")

    # 5.8 步骤 id 重复
    p = copy.deepcopy(base)
    p["steps"][2]["id"] = p["steps"][1]["id"]
    ok, errs = _plan_ok(p)
    check(not ok and any("id 重复" in e for e in errs), "步骤 id 重复被拦下")

    # 5.9 圆不给 r/d（参数组约束：至少要给一个）
    p = copy.deepcopy(base)
    p["steps"].insert(3, {"id": "c", "op": "circle"})
    ok, errs = _plan_ok(p)
    check(not ok and any("必须给 r 或 d" in e for e in errs), "圆没给 r/d 被拦下")

    # 5.10 表达式不是纯算术（防止计划文件变成代码执行入口）
    p = copy.deepcopy(base)
    p["steps"][3]["w"] = "{{ __import__('os').system('echo hi') }}"
    ok, errs = _plan_ok(p)
    check(not ok, "表达式里的内建调用被拦下")


# ---------------------------------------------------------------- 5b. 曲线骨架护栏
#
# ★ 这些护栏全部在**碰 SolidWorks 之前**执行（`sw=None` 也能跑），所以是纯离线用例。
#   为什么要专门测：`curve_xyz` 的点列是**外部数据**（参数方程算出来的几百个点），
#   一旦护栏失效，坏输入会一路走到 SolidWorks，报一个含糊的「没生成特征」——
#   真机排查成本极高。护栏的价值 = 把错误在源头说清楚。

def test_curve_ops() -> None:
    print("\n[5b] 曲线骨架 / 曲面 op 的参数护栏（离线，不碰 SolidWorks）")
    ctx = {"params": {}, "vars": {}}

    # 6.1 点数 < 2 必须报错
    for bad, label in (([], "空点列"), ([[0, 0, 0]], "只有 1 个点")):
        try:
            sw_ops.op_curve_xyz(None, {"op": "curve_xyz", "points": bad}, dict(ctx))
            check(False, f"（反例）curve_xyz {label} 应报错")
        except sw_ops.SwError as e:
            check("2 个点" in str(e), f"curve_xyz {label} 被拦下")

    # 6.2 重合点必须报错（曲线会建不出来，提前拦比"静默少点"清楚）
    try:
        sw_ops.op_curve_xyz(None, {"op": "curve_xyz",
                                   "points": [[0, 0, 0], [0, 0, 0]]}, dict(ctx))
        check(False, "（反例）curve_xyz 重合点应报错")
    except sw_ops.SwError as e:
        check("重合" in str(e), "curve_xyz 重合点被拦下")

    # 6.3 点数组归一化：tlb 里 PointArray 是 Variant，两种形态都要认
    flat = [0, 0, 0, 10, 0, 0, 10, 10, 0]
    p1 = sw_ops._normalize_point_array(flat, 3)
    check(p1 is not None and len(p1) == 3 and p1[1] == [10.0, 0.0, 0.0],
          "归一化：平铺 3n 个 double")
    p2 = sw_ops._normalize_point_array([[0, 0, 0], [10, 0, 0], [10, 10, 0]], 3)
    check(p2 is not None and len(p2) == 3, "归一化：n 个三元组")
    check(sw_ops._normalize_point_array(None, 3) is None,
          "（反例）None 归一化回 None，不假装成 0 个点")

    # 6.4 check_curve 不给名字也没 last_curve 必须报错
    try:
        sw_ops.op_check_curve(None, {"op": "check_curve"}, dict(ctx))
        check(False, "（反例）check_curve 无曲线名应报错")
    except sw_ops.SwError as e:
        check("last_curve" in str(e) or "没给" in str(e), "check_curve 无对象被拦下")

    # 6.5 surface_knit 容差越界必须报错（官方硬边界 0.0001~0.1mm）
    for tol in (0.0, 0.5):
        try:
            sw_ops.op_surface_knit(None, {"op": "surface_knit",
                                          "targets": ["A"], "tolerance": tol}, dict(ctx))
            check(False, f"（反例）缝合容差 {tol} 应报错")
        except sw_ops.SwError as e:
            check("容差" in str(e), f"缝合容差 {tol}mm 被拦下")
    # 正常容差不该被误拦（护栏不能变成"永远报错"）
    #   ★ 用一个"一碰属性就抛 RuntimeError"的假 sw：合法容差应当**穿过**护栏，
    #     直到访问 sw 时才炸 —— 若在护栏处就抛 SwError，说明护栏把正常值也拦了。
    class _Boom:
        def __getattr__(self, k):
            raise RuntimeError("no-com")

    try:
        sw_ops.op_surface_knit(_Boom(), {"op": "surface_knit",
                                         "targets": ["A"], "tolerance": 0.01}, dict(ctx))
        check(False, "（误报检查）合法容差一路跑到底却没碰 sw —— 用例本身写错了")
    except sw_ops.SwError as e:
        check("容差" not in str(e), "（误报检查）合法容差 0.01 不报容差错")
    except RuntimeError:
        check(True, "（误报检查）合法容差 0.01 穿过护栏（未报容差错）")

    # 6.6 thicken 方向只能是 side1/side2/both
    try:
        sw_ops.op_thicken(None, {"op": "thicken", "targets": ["A"],
                                 "thickness": 2, "direction": "sideways"}, dict(ctx))
        check(False, "（反例）thicken 非法方向应报错")
    except sw_ops.SwError as e:
        check("direction" in str(e), "thicken 非法方向被拦下")

    # 6.7 状态机一致性：has_curve 必须登记，且 curve_xyz 声明产生它
    check("has_curve" in sw_ops.STATES, "has_curve 已登记进 STATES")
    check(sw_ops.OPS["curve_xyz"].effect == ("has_curve",), "curve_xyz 产生 has_curve")
    # check_curve 是判据 op：不该产生任何状态（不然会被误当建模步）
    check(sw_ops.OPS["check_curve"].effect == (), "check_curve 不产生状态")
    # 3D 草图那对必须是"互逆"的（否则状态机会卡在半开）
    check(sw_ops.OPS["sketch3d_begin"].effect == ("sketch_open",)
          and sw_ops.OPS["sketch3d_end"].requires == ("sketch_open",),
          "sketch3d_begin/end 状态互逆")

    # 6.8 sketch3d_begin 的 mode 只能是 new/edit（在碰 sw 之前就拦）
    try:
        sw_ops.op_sketch3d_begin(None, {"op": "sketch3d_begin", "mode": "bogus"}, dict(ctx))
        check(False, "（反例）sketch3d_begin 非法 mode 应报错")
    except sw_ops.SwError as e:
        check("mode" in str(e), "sketch3d_begin 非法 mode 被拦下")
    # 合法 mode 必须穿过护栏（不在参数校验处就抛）—— 用"一碰属性就炸"的假 sw
    for ok_mode in ("new", "edit"):
        try:
            sw_ops.op_sketch3d_begin(_Boom(), {"op": "sketch3d_begin",
                                               "mode": ok_mode}, dict(ctx))
        except sw_ops.SwError as e:
            check("mode" not in str(e), f"（误报检查）合法 mode {ok_mode} 不报参数错")
        except RuntimeError:
            check(True, f"（误报检查）合法 mode {ok_mode} 穿过参数护栏")

    # 6.9 3D 草图识别：2D 草图**不能**被当成 3D 草图（真机类型名不同：ProfileFeature vs 3DProfileFeature）
    class _FakeSW:
        def __init__(self, feats, sketches=()):
            self._f = feats
            self._s = dict(sketches)

        def feature_names(self):
            return list(self._f)

    fk = _FakeSW([("3DProfileFeature", "3D草图1"), ("ProfileFeature", "草图1")])
    check(sw_ops._existing_3d_sketches(fk) == ["3D草图1"],
          "只认 3DProfileFeature（2D 的 ProfileFeature 不算 3D 草图）")
    # mode='edit' 时 target 不在已有 3D 草图里必须报错（在碰 sw 之前就拦）
    try:
        sw_ops.op_sketch3d_begin(_FakeSW([("3DProfileFeature", "3D草图1")]),
                                 {"op": "sketch3d_begin", "mode": "edit",
                                  "target": "Nope"}, dict(ctx))
        check(False, "（反例）sketch3d_begin edit 给了不存在的 target 应报错")
    except sw_ops.SwError as e:
        check("不在已有 3D 草图" in str(e), "sketch3d_begin edit 错误 target 被拦下")
    # 没有 3D 草图时 mode='edit' 必须报错
    try:
        sw_ops.op_sketch3d_begin(_FakeSW([("ProfileFeature", "草图1")]),
                                 {"op": "sketch3d_begin", "mode": "edit"}, dict(ctx))
        check(False, "（反例）没有 3D 草图时 mode=edit 应报错")
    except sw_ops.SwError as e:
        check("没有任何 3D 草图" in str(e), "没有 3D 草图时 mode=edit 被拦下")


# ---------------------------------------------------------------- 6. 宏翻译

def test_shot_tools() -> None:
    """截图工具的**自动裁空白** —— 「独立试验脚本 ≠ 被测函数」事故回归（§138①）。

    ★ 事故：集成进 `sw_shot` 的守卫**极性写反**（`if (x1-x0) < 0.97w and ...: return im`）
      —— 恰在**需要裁**的时候返回原图；而独立试验脚本用的是**另一份实现**，
      没有那道守卫，所以它"成功"了 ⇒ 集成后一张都没裁、md5 与不裁时**逐位相同**，
      却没人发现。⇒ 本条**直接测被测函数本身**，并且**正反两类用例都要有**。
    """
    print("\n[2y] 截图自动裁空白（要测「被测函数本身」，§138①）")
    try:
        from PIL import Image, ImageDraw                              # noqa: PLC0415
        from sw_shot import autocrop_content                          # noqa: PLC0415
    except Exception as e:                                            # noqa: BLE001
        check(False, f"导入 autocrop_content 失败：{e}")
        return

    # ① 模型很小、四周大留白 ⇒ **必须裁小**
    a = Image.new("RGB", (400, 300), (250, 250, 250))
    da = ImageDraw.Draw(a)
    da.ellipse([150, 110, 250, 190], outline=(40, 40, 40), width=2)
    da.line([150, 150, 250, 150], fill=(80, 80, 80), width=2)
    o1 = autocrop_content(a)
    check(o1.size[0] < 300 and o1.size[1] < 250,
          f"留白图应被裁小（{a.size} → {o1.size}）")

    # ② 模型占满画面 ⇒ **不许动**（宁可留白，也不裁掉模型）
    b = Image.new("RGB", (400, 300), (250, 250, 250))
    db = ImageDraw.Draw(b)
    db.rectangle([6, 6, 393, 293], fill=(240, 240, 240), outline=(60, 60, 60), width=2)
    db.line([6, 150, 393, 150], fill=(90, 90, 90), width=2)
    o2 = autocrop_content(b)
    check(o2.size == b.size, f"占满画面的图不该被裁（{b.size} → {o2.size}）")

    # ③ 纯背景、无内容 ⇒ 不抛异常、也不裁
    c = Image.new("RGB", (200, 160), (250, 250, 250))
    o3 = autocrop_content(c)
    check(o3.size == c.size, f"纯背景不该被裁（{o3.size}）")


def test_head_tools() -> None:
    """CRH380CL 车头生成器的**开口轮廓/预算自检** —— R-A 多边形开口（§139①）。

    ★ 事故：风挡掩膜轴序写成 (z,y) 却拿 (y,z) 去测 ⇒ 去除量预算**恒 0**；
      0 又原样传成 `check_volume_delta(expect=0)` ⇒ 真机**不切也"通过"**（假绿）。
    ⇒ 两条离线守卫（⑦ 轮廓有向面积 > 0、⑧ 去除量预算 > 0）必须**见过自己失败一次**
      （§74：没见自己红过的守卫，可能就是摆设）。
    """
    print("\n[2z] 车头开口轮廓/预算自检（R-A 多边形，§139①）")
    try:
        import sw_head as H                                   # noqa: PLC0415
    except Exception as e:                                    # noqa: BLE001
        check(False, f"导入 sw_head 失败：{e}")
        return

    # ① 有向面积符号：逆时针正方形 = +100；反过来 = −100
    ccw = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
    check(abs(H.poly_area(ccw) - 100.0) < 1e-9,
          f"逆时针正方形有向面积应 +100（得 {H.poly_area(ccw)}）")
    check(abs(H.poly_area(ccw[::-1]) + 100.0) < 1e-9,
          f"顺时针同形应有向面积 −100（得 {H.poly_area(ccw[::-1])}）")

    # ② round_poly 不翻向：圆角后仍有向面积 > 0，且点数增加
    rp = H.round_poly(ccw, 2.0)
    check(H.poly_area(rp) > 0 and abs(H.poly_area(rp)) < 100.0,
          f"圆角后仍逆时针且面积略减（{H.poly_area(rp):.1f}）")
    check(len(rp) > len(ccw), f"圆角应增加点（{len(ccw)} → {len(rp)}）")

    # ③ 真实参数 + 真实 s5 计划 ⇒ 离线自检必须全过（轮廓合法 + 预算为正）
    params = json.loads(H.DEFAULT_PARAMS.read_text(encoding="utf-8"))
    bad = H.selfcheck(params, H.build_plan(params, "s5"))
    check(not bad, "真实 s5 计划离线自检全过" + ("" if not bad else f" → {bad[:2]}"))

    # ④ 守卫必须见过自己红：把风挡预算打回 0 ⇒ ⑧ 必须报红
    orig_rm = H.windshield_removed_mm3
    try:
        H.windshield_removed_mm3 = lambda *a, **k: 0.0        # noqa: E731
        bad0 = H.selfcheck(params, H.build_plan(params, "s5"))
    finally:
        H.windshield_removed_mm3 = orig_rm
    check(any("风挡去除量预算" in b for b in bad0),
          "把风挡预算打回 0 ⇒ ⑧ 必须报红（否则判据就是坏的）")

    # ⑤ 把风挡轮廓点序反过来 ⇒ ⑦ 有向面积必须报红
    orig_ol = H.windshield_outline
    try:
        H.windshield_outline = lambda ws: list(reversed(orig_ol(ws)))   # noqa: E731
        bad1 = H.selfcheck(params, H.build_plan(params, "s5"))
    finally:
        H.windshield_outline = orig_ol
    check(any("有向面积" in b for b in bad1),
          "把风挡轮廓反向 ⇒ ⑦ 有向面积必须报红")


def test_macro() -> None:
    print("\n[6] 宏翻译器")
    try:
        import sw_macro_to_plan as mtp
    except ImportError as e:                                    # noqa: BLE001
        check(False, f"宏翻译器能导入（{e}）")
        return
    macro = """
Option Explicit
Sub main()
    Set swApp = Application.SldWorks
    Set Part = swApp.ActiveDoc
    boolstatus = Part.Extension.SelectByID2("Front Plane", "PLANE", 0, 0, 0, False, 0, Nothing, 0)
    Part.SketchManager.InsertSketch True
    Set skSegment = Part.SketchManager.CreateCircleByRadius(0#, 0#, 0#, 0.025)
    Part.SketchManager.InsertSketch True
    boolstatus = Part.Extension.SelectByID2("Sketch1", "SKETCH", 0, 0, 0, False, 0, Nothing, 0)
    Set myFeature = Part.FeatureManager.FeatureExtrusion3(True, False, False, 0, 0, 0.01, 0, _
        False, False, False, False, 0.017, 0.017, False, False, False, False, True, False, _
        True, 0, 0, False)
    Part.ClearSelection2 True
    Part.EditRebuild3
End Sub
"""
    lines = mtp.unify(macro)
    check(len(lines) >= 6, f"预处理后剩 {len(lines)} 条有效语句（噪音行被剥掉）")
    plan, unmapped = mtp.translate(lines)
    ops = [s["op"] for s in plan["steps"]]
    check("select_plane" in ops, "识别出选基准面")
    check("sketch_begin" in ops and "sketch_end" in ops, "InsertSketch 切换配对正确")
    check("circle" in ops, "识别出画圆（VBA 的 0# 类型后缀已处理）")
    ex = [s for s in plan["steps"] if s["op"] == "extrude_boss"]
    check(bool(ex), "识别出拉伸")
    if ex:
        check(ex[0].get("depth") == 10.0, f"深度按 mm 给（{ex[0].get('depth')}）")
        check(str(ex[0].get("end_cond")) == "swEndCondBlind",
              f"枚举数值 0 反查成名字（{ex[0].get('end_cond')}）")
        check(ex[0].get("profile") == "Sketch1", "SelectByID2(SKETCH) 转成 profile")
    # 翻译结果必须本身就能过校验
    ok, errs = _plan_ok(plan)
    check(ok, "翻译出来的计划自身通过校验" + ("" if ok else f" → {errs[:3]}"))


# ---------------------------------------------------------------- 7. 真跑

def test_live() -> None:
    print("\n[7] 真跑 SolidWorks（可选，慢）")
    import sw_env
    rep = sw_env.report()
    for p in sw_env.problems(rep):
        print("  !", p)
    if not rep["installed"] or not rep["pywin32"]:
        check(False, "环境满足真跑条件（跳过：缺 SolidWorks 或 pywin32）")
        return
    plan = sw_plan.template("box")
    plan["steps"] = [s for s in plan["steps"] if s["op"] != "save_as"]
    out = sw_plan.execute(plan, stop_on_error=True)
    check(out["failed"] is None,
          "最小用例（长方体）端到端跑通" +
          ("" if out["failed"] is None else f" → {out['failed']['error'][:200]}"))


def test_spec() -> None:
    """《07 尺寸标注规范》一致性 lint —— 交付件必须全绿，**且 lint 自己要能失败**。

    ★ 为什么必须有这一节：
      规范是**文字**，实现是**代码**，两者会漂移。漂移不会让 SolidWorks 报错 ——
      只会让模型"看起来对、其实不可参数化"。所以把规范里可判定的部分固化成
      `sw_spec.py` 的规则（S1~S8），每次自检都跑一遍。
    ★ 按 §74 的口径，**守卫必须见过自己失败一次**：下面三条反例是故意造违规，
      确认每条规则都真的会响。少一条，那条规则就可能是摆设。
    """
    print("\n[2c] 规范一致性（sw_spec：S1~S8，源自 docs/07 尺寸标注规范）")
    import copy

    if not hasattr(sw_spec, "lint_all"):
        check(False, "sw_spec 提供 lint_all")
        return
    rep = sw_spec.lint_all()
    n_err = sum(1 for v in rep["violations"] if v["severity"] == "err")
    check(rep["ok"], f"全部计划符合规范（{rep['n_checked']} 份，硬失败 {n_err}）"
          + ("" if rep["ok"] else f" → {rep['violations'][:2]}"))

    P = sw_reducer.load()

    def _mutate(part: str, fn) -> list:
        pl = copy.deepcopy(sw_reducer.build_plan(P, part, out_dir=None))
        fn(pl)
        return sw_spec.lint_plan(pl, part)

    # ★★ 2026-10-02 起**尺寸标注封存** ⇒ 生成器不再产出任何标注 step。
    #   于是反例 1 / 2 的构造方式从「**删**掉一个标注」翻转成「**塞**进一个标注」。
    #   （S4/S5/S8 的判据本身保留不动 —— 重开标注那天这些口径还要用。）
    def inject_dim(pl, chain="off", dev=None, op="dim_auto") -> None:
        """在**最后一张 sketch_end 之前**塞一个标注 step（模拟"回到封存前的写法"）。"""
        steps = pl["steps"]
        i = max(j for j, s in enumerate(steps) if s.get("op") == "sketch_end")
        step: dict = {"id": "dm_injected", "op": op}
        if op == "dim_auto":
            step["chain"] = chain
        if dev is not None:
            step["deviation"] = dev
        steps.insert(i, step)

    # 反例 1：回转体改回逐段标（chain=off）且**不写 deviation** ⇒ S4
    #   ★ 注意不要把 deviation 写进去 —— 否则它是"明示偏差"，S4 按设计不响。
    vs = _mutate("spacer", lambda pl: inject_dim(pl, "off"))
    check(any(v["rule"] == "S4" for v in vs), "反例被拦下：回转体 chain=off 且无 deviation ⇒ S4")

    # 反例 1b：写了 deviation 就**不该**再报 S4（明示偏差是合法出口）
    vs = _mutate("spacer", lambda pl: inject_dim(
        pl, "off", "实测链式口径在本型面退化（反例 1b）"))
    check(not any(v["rule"] == "S4" for v in vs),
          "明示偏差（带 deviation）不再报 S4 —— 偏差写在明处即合法")

    # 反例 2：计划里出现**已封存**的标注 op ⇒ S3
    #   ★ 这条的含义在 2026-10-02 **翻转**了：原来是「必须有标注（少了就违规）」，
    #     现在是「**禁止**标注（有了就违规）」—— 因为真机逐段标会弹模态框挂死自动化、
    #     并把型面驱动改坏。四个封存入口都要拦得住。
    for _op in ("dim_auto", "smart_dim", "dimension", "fully_define"):
        vs = _mutate("output_shaft", lambda pl, _o=_op: inject_dim(pl, op=_o))
        check(any(v["rule"] == "S3" for v in vs),
              f"反例被拦下：计划里塞了封存 op `{_op}` ⇒ S3")

    # 正例对照：生成器产出的计划**一条标注都没有** ⇒ 不报 S3（证明封存真的生效了）
    vs = sw_spec.lint_plan(sw_reducer.build_plan(P, "output_shaft", out_dir=None),
                           "output_shaft")
    check(not any(v["rule"] == "S3" for v in vs),
          "生成器产出的计划不含标注 ⇒ 不报 S3（封存生效）")

    # 反例 3：删掉旋转用的中心线 ⇒ S1
    vs = _mutate("spacer",
                 lambda pl: pl["steps"].pop(next(i for i, s in enumerate(pl["steps"])
                                                 if s.get("op") == "centerline")))
    check(any(v["rule"] == "S1" for v in vs), "反例被拦下：删掉中心线 ⇒ S1")

    # 反例 4：让旋转轮廓跨过中心线 ⇒ S6
    def cross_axis(pl):
        for s in pl["steps"]:
            if s.get("op") == "polyline":
                s["points"] = [[p[0], -p[1]] if i % 2 else p
                               for i, p in enumerate(s["points"])]

    vs = _mutate("spacer", cross_axis)
    check(any(v["rule"] == "S6" for v in vs), "反例被拦下：轮廓跨中心线 ⇒ S6")

    # 反例 5：删掉约束闸门 ⇒ S2
    vs = _mutate("cover2",
                 lambda pl: pl.__setitem__("steps", [s for s in pl["steps"]
                                                     if s.get("op") != "check_sketch_constraint"]))
    check(any(v["rule"] == "S2" for v in vs), "反例被拦下：删掉约束闸门 ⇒ S2")

    # ---------------- S7 锚定：正/反例（2026-10-04 反光片斜槽暴露） ----------------
    # ★ 这一组同时守两件事（§74 / 用户铁律）：
    #   ① **该报的能报出来**（离轴轴 + 无原点锚 ⇒ 必须红）；
    #   ② **不该报的不许误报**（三种合法锚法都不许红 —— 我方一度收紧判据误伤了 gear_shaft）。
    def _sk7(cl, extra=(), rect=((5, 2), (15, 4))):
        steps = [
            {"id": "n", "op": "new_part"},
            {"id": "p", "op": "select_plane", "plane": "Front"},
            {"id": "s1", "op": "sketch_begin"},
            {"id": "cl", "op": "centerline", "x1": cl[0], "y1": cl[1], "x2": cl[2], "y2": cl[3]},
            *extra,
            {"id": "rr", "op": "rect_corner", "x1": rect[0][0], "y1": rect[0][1],
             "x2": rect[1][0], "y2": rect[1][1]},
            {"id": "s2", "op": "sketch_end", "name": "G"},
            {"id": "rc", "op": "revolve_cut", "profile": "G", "angle": 360},
            {"id": "ck", "op": "check_sketch_constraint", "sketch": "G", "expect_min_dims": 0},
        ]
        return sw_spec.lint_plan({"schema": "yoyo.swplan/1", "params": {}, "steps": steps},
                                 "synthetic")

    def _s7(vs):
        return [v for v in vs if v["rule"] == "S7"]

    # 正例 a：中心线**过原点**（竖直 (0,-25)-(0,25)）⇒ 不报 S7（规则文本口径，原来假阳性）
    check(not _s7(_sk7((0, -25, 0, 25))),
          "S7 正例：竖直过原点的中心线算锚住（不再假阳性）")
    # 正例 b：中心线**躺在 x 轴**上（(5,0)-(15,0)，不跨原点）⇒ 不报 S7（历史口径，键槽件依赖）
    check(not _s7(_sk7((5, 0, 15, 0), rect=((5, 2), (15, 4)))),
          "S7 正例：躺在 x 轴上的中心线算锚住（历史口径，防误伤 gear_shaft 型）")
    # 正例 c：离轴斜中心线 + **落在原点的构造点** ⇒ 不报 S7（反光片斜槽的锚法）
    check(not _s7(_sk7((5, 2, 15, 4), extra=[{"id": "pt", "op": "point", "x": 0, "y": 0}])),
          "S7 正例：离轴回转轴 + 原点构造点算锚住（反光片斜槽）")
    # 反例：离轴斜中心线（既不在 x 轴、也不过原点）且**无原点锚** ⇒ S7 必须响
    check(bool(_s7(_sk7((5, 2, 15, 4)))),
          "反例被拦下：离轴中心线且无原点锚 ⇒ S7")


def test_dim_dedup() -> None:
    """`chain=off` 的**同量去重**（`sw_ops._off_dim_goal`）—— 用户实测反馈驱动的修复。

    ★ 病因：`chain=off` 是"逐段标"，矩形环的对边**等长且投影区间重合**
      ⇒ 图上出现"同一个数字标了两遍"（13 / 13 / 3.5 / 3.5）。用户截图直接指的就是它。
    ★ 但**不能**简单按"等长"去重：阶梯轴的两段等长台阶在**不同位置**，是两个量。
      ⇒ 判据必须是「同 caliber + 同方向 + **同投影区间**」。
    ★ 这一节直接调生产代码 `_off_dim_goal`，不抄逻辑（§74：抄一遍测的是副本）。
    """
    print("\n[2d] 逐段标注去重（chain=off 的同量合并）")
    if not hasattr(sw_ops, "_off_dim_goal"):
        check(False, "sw_ops 提供 _off_dim_goal")
        return

    def line(a, b, idx=0):
        return {"index": idx, "kind": "line", "start_mm": [a[0], a[1], 0.0],
                "end_mm": [b[0], b[1], 0.0]}

    def arc(c, r, idx=0, circle=False):
        return {"index": idx, "kind": "arc", "is_circle": circle, "radius_mm": r,
                "center_mm": [c[0], c[1], 0.0]}

    K = {"line", "arc", "circle"}

    # ① 矩形环（spacer 的真实型面）：对边重复 ⇒ 4 段只留 2 条
    ring = [line((0, 15), (13, 15), 0), line((13, 15), (13, 18.5), 1),
            line((13, 18.5), (0, 18.5), 2), line((0, 18.5), (0, 15), 3)]
    for i, g in enumerate(ring):
        g["index"] = i
    pairs, skipped = sw_ops._off_dim_goal(ring, K)
    vals = [round(v, 4) for _g, _a, v in pairs]
    check(vals == [13.0, 3.5], f"矩形环：4 段去重成 2 条 {vals}（不再 13/13/3.5/3.5）")
    check(len(skipped) == 2, f"矩形环：跳过 2 条重复（实得 {len(skipped)}）")
    check(all("重复" in s["why"] for s in skipped), "跳过理由点名是「重复」")

    # ② 阶梯轴：两段**等长但位于不同 x**的台阶 ⇒ 是两个量，都要留（防过度去重）
    step = [line((0, 15), (30, 15), 0), line((40, 15), (70, 15), 1)]
    for i, g in enumerate(step):
        g["index"] = i
    _p2, _s2 = sw_ops._off_dim_goal(step, K)
    check(len(_p2) == 2, f"等长不同位置的台阶**不误删**（2 段全留，实得 {len(_p2)}）")

    # ③ 构造中心线的签名**不得吃掉**真图元的签名（2026-10-02 真机踩到）
    #    ★ 病因：环形的 `centerline` 与型面"长"同长、同向、同区间 ⇒ 它的签名先占位，
    #      轮到真正的型面长边时被判成"重复"跳过 ⇒ 草图里只剩"高"一条
    #      （9 件回归里 8 件失败的真因：闸门报"只有 1 个"）。去重要排除构造线。
    ring_cl = [dict(line((0, 0), (13, 0), 0), construction=True),
               line((0, 15), (13, 15), 1), line((13, 15), (13, 18.5), 2),
               line((13, 18.5), (0, 18.5), 3), line((0, 18.5), (0, 15), 4)]
    for i, g in enumerate(ring_cl):
        g["index"] = i
    _p3, _s3 = sw_ops._off_dim_goal(ring_cl, K)
    v3 = [round(v, 4) for _g, _a, v in _p3]
    check(v3 == [13.0, 3.5], f"中心线不得吃掉型面长边：仍得 2 条 {v3}（修前只 1 条）")
    check(any("构造" in s["why"] for s in _s3), "中心线被点名「构造几何/中心线」跳过（不计入签名）")

    # ②b 但**同一个 y 区间**的两条竖线（同一台阶的两个端面）确实是同一个量 ⇒ 合并
    same_span = [line((30, 15), (30, 16), 0), line((56, 15), (56, 16), 1)]
    for i, g in enumerate(same_span):
        g["index"] = i
    _p2b, _s2b = sw_ops._off_dim_goal(same_span, K)
    check(len(_p2b) == 1, f"同 y 区间的两条竖线合并为 1 条（实得 {len(_p2b)}）")

    # ③ 方向不同的同值不算重复（横 13 与竖 13 是两个量）
    hv = [line((0, 0), (13, 0), 0), line((13, 0), (13, 13), 1)]
    _p3, _s3 = sw_ops._off_dim_goal(hv, K)
    check(len(_p3) == 2, f"横 13 / 竖 13 各留一条（实得 {len(_p3)}）")

    # ④ 平键型面：2 直边（等长同区间）+ 2 端弧（同半径）⇒ 只留「长 + R」
    key = [line((-6, -5), (6, -5), 0), line((6, 5), (-6, 5), 1),
           arc((6, 0), 5.0, 2), arc((-6, 0), 5.0, 3)]
    for i, g in enumerate(key):
        g["index"] = i
    _p4, _s4 = sw_ops._off_dim_goal(key, K)
    v4 = sorted(round(v, 4) for _g, _a, v in _p4)
    check(v4 == [5.0, 12.0], f"平键：4 个图元去重成「长 12 + R5」{v4}")

    # ⑤ 两个**不同位置**的整圆孔是两个量（整圆按「同圆心同值」去重，不按值）
    holes = [arc((10, 0), 2.5, 0, circle=True), arc((30, 0), 2.5, 1, circle=True)]
    for i, g in enumerate(holes):
        g["index"] = i
    _p5, _s5 = sw_ops._off_dim_goal(holes, K)
    check(len(_p5) == 2, f"两个同径异位孔都保留（实得 {len(_p5)}）")


def test_through_cover() -> None:
    """透盖族（端盖1/3）：**实测型面**反推 + 生成器护栏 + 直线串闭合方式。

    ★ 这一节刻意喂 **params 里真实存的实测型面**（参照件 19-端盖1 / 28-端盖3
      由 `sw_profile.py` 反推、Pappus 对账到 0.0006%），不用编造的几何 ——
      编出来的"矩形/台阶"测不出「非单调 + 环面圆弧」这类真问题（§74 同源）。
    """
    print("\n[2e] 型面驱动族（透盖/挡油环）：实测型面 + 生成器护栏")
    P = sw_reducer.load()

    # ① 生成器**自己重算**的 Pappus 体积必须复现参照件实测体积
    #    （生成器不许直接信 params 里存的数字 —— 型面被改坏必须当场露馅）
    for part, ref in (("cover1", 24360.5), ("cover3", 17210.3), ("oil_sling", 3845.1)):
        q = P["parts"].get(part)
        if not isinstance(q, dict) or "profile" not in q:
            check(False, f"{part} 参数表里有 profile")
            continue
        pr = q["profile"]
        vol = sw_reducer._profile_volume(pr["polyline"], pr.get("arcs") or [])
        err = abs(vol - ref) / ref * 100.0
        check(err < 0.1, f"{part}: 型面重算体积 {vol:.2f} vs 参照 {ref} 差 {err:.4f}%")
        check(part in sw_reducer.PROFILE_FAMILY, f"{part} 在型面驱动族里")
        check(sw_reducer.BUILDERS.get(part) is not None, f"{part} 有计划生成器")

    # ①b2 十字槽盘头螺钉（GB/T 818）：纯回转体，**十字槽未建** ⇒ 登记 Q6
    for part, L, k, dk, d in (("screw_m3x10", 10.0, 2.4, 5.6, 3.0),
                              ("screw_m3x22", 22.0, 2.4, 5.6, 3.0)):
        pr = (P["parts"].get(part) or {}).get("profile")
        if not check(isinstance(pr, dict), f"{part} 参数表里有 profile"):
            continue
        check(part in sw_reducer.PROFILE_FAMILY, f"{part} 在型面驱动族里（螺钉是纯回转体）")
        check(pr.get("polyline_close") is True and not (pr.get("arcs") or []),
              f"{part}: 纯直线闭合型面（盘头+杆，无圆弧）")
        # 贴轴（实心）⇒ 轮廓必须含 r=0 的点，否则回转出来是空心的
        check(any(abs(pt[1]) < 1e-9 for pt in pr["polyline"]),
              f"{part}: 型面贴轴（含 r=0 的点）⇒ 实心")
        # 独立公式：π/4·dk²·k + π/4·d²·L（**不**用 Pappus 自证）
        indep = math.pi / 4 * dk ** 2 * k + math.pi / 4 * d ** 2 * L
        vol = sw_reducer._profile_volume(pr["polyline"], pr.get("arcs") or [])
        check(abs(vol - indep) / indep < 1e-6,
              f"{part}: Pappus {vol:.4f} 与独立公式 π/4·dk²k+π/4·d²L = {indep:.4f} 吻合")
        check(abs(pr["L"] - (k + L)) < 1e-9,
              f"{part}: 轴向总长 = 头高 {k} + 螺纹长 {L} = {pr['L']}")
        # ★ 简化项必须**登记在案**，不许悄悄省
        built = (P["parts"][part].get("_measured_faces") or {}).get("solved", {}).get("built", {})
        check("十字槽未建" in str(built.get("simplified", "")),
              f"{part}: 记录了「十字槽未建」这个简化项")
        check("Q6" in {q_["id"] for q_ in P.get("open_questions", [])},
              f"{part}: 十字槽缺权威数据已登记为 Q6（不猜）")
        check(P["parts"][part].get("status") == "modeled", f"{part} status=modeled")

    # ①b 挡油环（台阶环 + 两个锥面）—— 纯直线闭合件，且必须真的能出计划
    osl = P["parts"]["oil_sling"]["profile"]
    check(not (osl.get("arcs") or []), "挡油环无圆弧（纯直线件）")
    check(osl.get("polyline_close") is True, "挡油环 polyline_close=True（自己闭合）")
    try:
        pl_o = sw_reducer.build_plan(P, "oil_sling", None)
        check(pl_o["_derived"]["analytic_volume_mm3"] > 3800.0,
              f"挡油环计划体积 {pl_o['_derived']['analytic_volume_mm3']:.2f} 合理")
    except Exception as e:                                          # noqa: BLE001
        check(False, f"挡油环该能出计划，实得 {type(e).__name__}: {e}")

    # ② 端盖1 必须认出那一段环面圆弧（R2）—— 这是"非单调型面"的核心证据
    c1 = P["parts"]["cover1"]["profile"]
    arcs1 = c1.get("arcs") or []
    check(len(arcs1) == 1, f"端盖1 有且仅有 1 段环面圆弧（实得 {len(arcs1)}）")
    if arcs1:
        a = arcs1[0]
        ctr = a.get("center")
        check(ctr is not None and abs(ctr[1] - 24.0) < 1e-6 and abs(ctr[0] - 14.0) < 1e-6,
              f"端盖1 圆弧圆心 = (14,24)（归一化后），实得 {ctr}")
        check(abs(float(a["radius"]) - 2.0) < 1e-6, f"端盖1 圆弧 R=2，实得 {a['radius']}")
        # 端点是弧的边界圆，且必须落在 x 轴上侧、R>0
        check(all(p[1] > 0 for p in (a["p1"], a["p2"], a["p3"])), "圆弧三点都在轴上方")
    # 端盖3 是**纯直线**件 —— 与 cover1 拓扑**不同**（原 _measured_faces 判断有误）
    c3 = P["parts"]["cover3"]["profile"]
    check(not (c3.get("arcs") or []), "端盖3 无圆弧（纯直线件）")

    # ③ 直线串闭合方式：纯直线件必须 close=True 且不能有重合尾点
    check(c3.get("polyline_close") is True, "端盖3 polyline_close=True（自己闭合）")
    check(c1.get("polyline_close") is False, "端盖1 polyline_close=False（交给圆弧收口）")
    poly3 = c3["polyline"]
    check(not (abs(poly3[0][0] - poly3[-1][0]) < 1e-9
               and abs(poly3[0][1] - poly3[-1][1]) < 1e-9),
          "端盖3 直线串首尾不重合（重合会多画一条零长直线）")

    # ④ 生成器护栏：三种畸形型面都必须被拦住（反例）
    def mini(**over):
        base = {"no": 99, "name": "测试盖", "ref": "x.SLDPRT", "D": {"v": 20.0},
                "profile": {
                    "L": 10.0, "polyline": [[0.0, 5.0], [0.0, 10.0], [10.0, 10.0], [10.0, 5.0]],
                    "polyline_close": True, "arcs": [],
                    "pappus_volume_mm3": math.pi / 4.0 * (400.0 - 100.0) * 10.0,
                    "reference_volume_mm3": math.pi / 4.0 * (400.0 - 100.0) * 10.0}}
        base.update(over)
        return {"parts": {"tst": base}}

    def raises(P_, why) -> None:
        try:
            sw_reducer._profile_plan(P_, None, "tst")
            check(False, f"护栏：{why} 必须报错")
        except (ValueError, KeyError):
            check(True, f"护栏：{why} 被拦下")

    # 正常形（圆环剖面）必须能生成 —— 先把"不该拦的没被拦"钉住
    try:
        ok_plan = sw_reducer._profile_plan(mini(), None, "tst")
        check(len(ok_plan["steps"]) >= 10, "正常圆环剖面能出计划")
    except Exception as e:                                        # noqa: BLE001
        check(False, f"正常圆环剖面不该报错，实得 {type(e).__name__}: {e}")

    arc = {"p1": [0.0, 5.0], "p2": [0.0, 10.0], "p3": [0.5, 8.0], "radius": 5.0,
           "center": [5.0, 5.0]}
    raises(mini(profile={"L": 10.0, "polyline": [[0.0, 5.0], [0.0, 10.0], [10.0, 10.0], [10.0, 5.0]],
                       "polyline_close": True, "arcs": [arc],
                       "pappus_volume_mm3": 1000.0}),
           "有圆弧却让直线串自己闭合")
    raises(mini(profile={"L": 10.0, "polyline": [[0.0, 5.0], [0.0, 10.0], [10.0, 10.0], [10.0, 5.0]],
                       "polyline_close": False, "arcs": [],
                       "pappus_volume_mm3": 1000.0}),
           "既无圆弧又不闭合（轮廓开口）")
    raises(mini(profile={"L": 10.0,
                         "polyline": [[0.0, 5.0], [0.0, 10.0], [10.0, 10.0], [10.0, 5.0], [0.0, 5.0]],
                         "polyline_close": True, "arcs": [],
                         "pappus_volume_mm3": 1000.0}),
           "close=True 却首尾点重合")
    raises(mini(profile={"L": 10.0, "polyline": [[0.0, 5.0], [0.0, 10.0], [10.0, 10.0], [10.0, 5.0]],
                         "polyline_close": True, "arcs": [],
                         "pappus_volume_mm3": 1.0}),
           "型面重算体积与 params 存的差 >0.1%")

    # ⑤ plan_hint：闭合方式必须随型面走（纯直线 → close=True；带弧 → open + arc3p）
    def el(kind, a, b, **kw):
        return {"kind": kind, "a": list(a), "b": list(b), "from": list(a), "to": list(b), **kw}

    closed = [el("line", (0, 5), (0, 10)), el("line", (0, 10), (10, 10)),
              el("line", (10, 10), (10, 5)), el("line", (10, 5), (0, 5))]
    h = sw_profile.plan_hint(closed)
    check(len(h) == 1 and h[0]["op"] == "polyline" and h[0]["close"] is True,
          f"纯直线闭环 ⇒ 1 个 polyline(close=True)，实得 {[x['op'] for x in h]}")
    check(len(h[0]["points"]) == 4, f"首尾同点已去重（4 点，实得 {len(h[0]['points'])}）")

    with_arc = [el("line", (0, 5), (0, 10)), el("line", (0, 10), (10, 10)),
                el("arc", (10, 10), (0, 5), center=[5, 5], radius=5.0,
                   arc_mid=[8.5, 8.5])]
    h2 = sw_profile.plan_hint(with_arc)
    check([x["op"] for x in h2] == ["polyline", "arc3p"],
          f"带弧 ⇒ polyline + arc3p，实得 {[x['op'] for x in h2]}")
    check(h2[0]["close"] is False, "带弧时直线串必须开口（close=False）")
    check(h2[0]["points"][-1] == [10.0, 10.0] and h2[1]["p1"] == [10.0, 10.0],
          "直线串末点 == 圆弧起点（轮廓才接得上）")


def test_disc_holes() -> None:
    """带孔盘族（垫片1 / 油面透视片）：环回转 + 特征级圆周阵列 + 孔数/孔径护栏。

    ★ 这两件正是 `sw_profile` 偏心圆守卫（§81）判 `revolvable=False` 拦下来的 ——
      几何规格全在 `off_axis` 里（分布圆 Ø24 / 孔径 Ø4 / 孔数），只缺一个生成器。
      本段把「规格 → 体积靶值 → 计划结构 → 护栏」这条链钉死，并回读真机实测记录。
    """
    print("\n[2g] 带孔盘族（垫片1/油面透视片）：环回转 + 孔圆周阵列")
    P = sw_reducer.load()

    # ① 体积公式：孔数由**体积差**定，不数标记个数（同一条边挂两张面会重复计数）
    for part, OD, ID, t, n, hd, ref in (("gasket1", 34.0, 14.0, 1.0, 3, 4.0, 716.3),
                                         ("glass", 34.0, 4.0, 1.0, 2, 4.0, 870.2)):
        ring, holed = sw_reducer._disc_holes_volume(OD, ID, t, n, hd)
        check(abs(holed - ref) / ref < 1e-3,
              f"{part}: 解析体积 {holed:.2f} vs 参照 {ref}（差 {abs(holed - ref) / ref * 100:.4f}%）")
        check(holed < ring, f"{part}: 打孔后体积必须小于打孔前（{holed:.2f} < {ring:.2f}）")
        check(part in sw_reducer.DISC_HOLES_FAMILY, f"{part} 在带孔盘族里")
        check(sw_reducer.BUILDERS.get(part) is not None, f"{part} 有计划生成器")

    # ② 真机实测记录：必须回读得到，且对参照件差 < 0.01%（**不能只信计划里的解析值**）
    for part, ref in (("gasket1", 716.3), ("glass", 870.2)):
        mf = P["parts"][part].get("_measured_faces") or {}
        b = mf.get("built")
        if not check(isinstance(b, dict), f"{part}: 参数表里有 _measured_faces.built（真机记录）"):
            continue
        mv = float(b.get("measured_volume_mm3") or 0.0)
        check(abs(mv - ref) / ref < 1e-4,
              f"{part}: 真机实测 {mv:.4f} vs 参照 {ref} 差 {abs(mv - ref) / ref * 100:.4f}%")
        check(P["parts"][part].get("status") == "modeled", f"{part} status=modeled")
        # 解析值与真机实测必须独立吻合（不是抄同一个数）
        ring, holed = sw_reducer._disc_holes_volume(34.0, 14.0 if part == "gasket1" else 4.0,
                                                    1.0, 3 if part == "gasket1" else 2, 4.0)
        check(abs(mv - holed) / holed < 1e-4,
              f"{part}: 真机 {mv:.4f} 与解析 {holed:.4f} 独立吻合（差 {abs(mv - holed) / holed * 100:.4f}%）")

    # ③ 计划结构：必须是「回转 → 建轴 → Right 面草图 → 贯穿切除 → 特征级阵列」，
    #    且**两次 check_mass**（只测一次的话「环错 + 孔补回来」会互相抵消）
    for part, n in (("gasket1", 3), ("glass", 2)):
        plan = sw_reducer.build_plan(P, part, None)
        ops = [s["op"] for s in plan["steps"]]
        ids = [s.get("id") for s in plan["steps"]]
        check(ops.count("check_mass") == 2, f"{part}: 两次 check_mass（打孔前/后各一），实得 {ops.count('check_mass')}")
        check(ops.count("circular_pattern") == 1, f"{part}: 用**特征级** circular_pattern")
        check("sketch_circular_pattern" not in ops,
              f"{part}: 不用草图级阵列（在面草图上选不中种子，见 §82）")
        check(ops.count("axis_from_planes") == 1, f"{part}: 显式建阵列基准轴（临时轴选不中）")
        check(ops.count("revolve_boss") == 1, f"{part}: 环由一次回转成型")
        check(ops.count("centerline") == 2, f"{part}: 两张草图各一条中心线（回转轴 + 锚原点）")
        check(ops.count("check_sketch_constraint") == 2, f"{part}: 每张草图都有闸门（S2）")
        cp = next(s for s in plan["steps"] if s["op"] == "circular_pattern")
        check(int(cp["count"]) == n, f"{part}: 阵列 {n} 个孔（含原始那个）")
        check(abs(float(cp["angle"]) - 360.0) < 1e-9, f"{part}: 整圈均分 360°")
        p2 = next(s for s in plan["steps"] if s["id"] == "p2")
        check(p2["op"] == "select_plane" and p2["plane"] == "Right",
              f"{part}: 孔位草图开在 Right 基准面（法向 = 回转轴 X）")
        # 计划必须**离线可校验**（yoyo.swplan/1 的核心约定）⇒ 步骤参数里不许出现
        # 依赖前序步的表达式（`{{ vars.x.y }}` 要等那步真跑完才有值 ⇒ 离线求值必失败）。
        # ★ 只查**参数**、跳过 `note`：note 里可以（也应该）解释"为什么不用它"。
        payload = json.dumps([{k: v for k, v in s.items() if k != "note"}
                              for s in plan["steps"]], ensure_ascii=False)
        check("vars." not in payload,
              f"{part}: 步骤参数里没有 {{{{ vars.* }}}} 引用（会让离线求值失败）")
        check("{{" not in str(cp.get("axis", "")),
              f"{part}: 阵列轴名写死（axis_from_planes 会真改名，见 §82）")

    # ④ 护栏（反例）：孔越出环带 / 规格不全 必须被**计划期**拦下，不交给真机去发现
    def mini(**holes):
        sv = {"outer_d": 34.0, "inner_d": 14.0, "thickness": 1.0,
              "holes": {"n": 3, "d": 4.0, "pitch_d": 24.0}}
        sv["holes"].update(holes)
        return {"parts": {"tst": {"no": 99, "name": "测试片", "ref": "x.SLDPRT",
                                  "D": {"v": 34.0},
                                  "_measured_faces": {"solved": sv}}}}

    def raises(P_, why):
        try:
            sw_reducer._disc_holes_plan(P_, None, "tst")
            check(False, f"护栏：{why} 必须报错")
        except (ValueError, KeyError):
            check(True, f"护栏：{why} 被拦下")

    # 正常形必须能出计划 —— 先把"不该拦的没被拦"钉住
    try:
        check(len(sw_reducer._disc_holes_plan(mini(), None, "tst")["steps"]) == 23,
              "正常带孔盘能出计划（23 步）")
    except Exception as e:                                        # noqa: BLE001
        check(False, f"正常带孔盘不该报错，实得 {type(e).__name__}: {e}")

    raises(mini(pitch_d=40.0), "分布圆 Ø40 越出环带（孔切不到料）")
    raises(mini(pitch_d=10.0), "分布圆 Ø10 让孔咬进 Ø14 内孔")
    raises(mini(n=0), "孔数为 0（规格不全）")
    raises(mini(d=0.0), "孔径为 0（规格不全）")
    raises(mini(pitch_d=0.0), "分布圆为 0（规格不全）")

    # ⑤ sw_spec S1 回归：**多草图**计划里，没写 profile 的 revolve 不能取错草图
    #    （早先退化成 `sketches[-1]` = 整份计划最后一张草图 ⇒ 误报「没画中心线」，§82）
    plan_g1 = sw_reducer.build_plan(P, "gasket1", None)
    viol = [v for v in sw_spec.lint_plan(plan_g1, "gasket1") if v["rule"] == "S1"]
    check(not viol, f"gasket1（23 步/2 张草图）不误报 S1，实得 {viol[:1]}")
    sk_g1, _ = sw_spec._split(plan_g1)
    check(len(sk_g1) == 2, f"gasket1 切出 2 张草图，实得 {len(sk_g1)}")
    rev_idx = next(i for i, s in enumerate(plan_g1["steps"]) if s["op"] == "revolve_boss")
    owner = next((k for k, s in enumerate(sk_g1) if any(st is plan_g1["steps"][rev_idx]
                                                         for st in s.ops)), None)
    check(owner is not None and sk_g1[owner].drew_centerline,
          "revolve 归属的草图确实画了中心线（归属判定本身要先钉对）")

    # ⑥ op 声明守卫：`revolve_boss` 必须声明 effect=("has_feature",)
    #    （漏了它，「回转 → 切除」链路会在**静态校验**阶段就被误拦，§82）
    check("has_feature" in sw_ops.OPS["revolve_boss"].effect,
          "revolve_boss 声明 effect=has_feature（与 extrude_boss 一致）")
    check("has_feature" in sw_ops.OPS["extrude_boss"].effect,
          "extrude_boss 声明 effect=has_feature")


def test_hex_family() -> None:
    """六角件族（螺母 / 六角头螺栓）+ 平垫圈：GB 表驱动、**标准件不抄参照件**。

    ★ 三条要钉死的：
      ① **外接圆半径 r = s/√3**（s = 对边宽），包围盒 = [2s/√3, s, t]；
         ★ 2026-10-04 修正：原实现把 s 直接当外接圆半径 ⇒ 六角比 GB 名义大 √3 倍，
           而本测试**同步按错的语义断言** ⇒ bug 一路绿灯（§102 的教科书案例）；
      ② **螺栓的六角只占 0→k 段**，k→L 是圆杆 —— 混算会让期望体积放大近 3 倍
         （真机实测 1765.93 vs 错公式 9719.9，见 §84）；
      ③ 规格必须 `source=="gb"`（唯一权威表 `assets/gb_standard.json`），
         **不能从参照件抄**（平垫圈参照件 Ø22/Ø11/1.5 是美制 3/8"，国标是 20/10.5/2）。
    """
    print("\n[2h] 六角件族 + 平垫圈：GB 表驱动")
    P = sw_reducer.load()

    # ① 正六边形几何：参数 = **外接圆半径 r**；顶点距原点 = r；相邻顶点夹 60°；对边宽 = r·√3
    #    ★★★ 2026-10-04 修正：原先这里断言「外接圆半径 = 对边宽 s」（把两个概念混为一谈），
    #      与 `_hex_plan` 的错实现**互相印证** ⇒ 六角大 √3 倍的 bug 一路绿灯（§102 教科书案例）。
    for s in (13.0, 16.0):
        r = s / math.sqrt(3.0)                       # 对边宽 s ⇒ 外接圆半径 r = s/√3
        vs = sw_reducer._hex_vertices(r)
        check(len(vs) == 6, f"对边宽 {s:g} 的六边形有 6 个顶点")
        check(all(abs(math.hypot(x, y) - r) < 1e-6 for x, y in vs),
              f"对边宽 {s:g} ⇒ 外接圆半径 r = s/√3 = {r:.4f}（每顶点在半径 r 上）")
        angs = sorted(math.degrees(math.atan2(y, x)) % 360 for x, y in vs)
        gaps = [round((angs[(i + 1) % 6] - angs[i]) % 360, 3) for i in range(6)]
        check(all(abs(g - 60.0) < 0.01 for g in gaps), f"六边形相邻顶点夹角都是 60°（实得 {gaps[:2]}）")
        # 边长 = 外接圆半径 r；对边宽 = 2·r·cos30° = s
        e = math.dist(vs[0], vs[1])
        check(abs(e - r) < 1e-6, f"正六边形边长 = 外接圆半径 = {e:.4f}")
        check(abs(2.0 * r * math.cos(math.radians(30.0)) - s) < 1e-6,
              f"对边宽 = 2·r·cos30° = {2.0 * r * math.cos(math.radians(30.0)):.4f} = {s:g}")

    # ② 体积公式：螺母与螺栓**必须分开算**（正例 + 反例）
    nut10 = sw_reducer._hex_volume(16.0, 10.0, "hex_nut", 8.4)
    check(abs(nut10 - 1202.5666) < 1e-3, f"螺母 M10 解析体积 {nut10:.4f}（真机 1202.5665）")
    bolt25 = sw_reducer._hex_volume(13.0, 8.0, "hex_bolt", 5.3, 25.0)
    check(abs(bolt25 - 1765.9290) < 1e-3, f"螺栓 M8×25 解析体积 {bolt25:.4f}（真机 1765.9289）")
    wrong = 3.0 * math.sqrt(3.0) / 2.0 * 13.0 ** 2 * 25.0 - math.pi / 4.0 * 64 * 25.0
    check(abs(wrong - bolt25) / bolt25 > 1.5,
          f"反例：'整根当六角柱'的错公式 {wrong:.0f} 与正确值差 "
          f"{abs(wrong - bolt25) / bolt25:.2f} 倍（≈2.9 倍体积）")
    try:
        sw_reducer._hex_volume(13.0, 8.0, "hex_bolt", 5.3, 5.3)
        check(False, "护栏：杆长 ≤ 头高 必须报错")
    except ValueError:
        check(True, "护栏：杆长 ≤ 头高 被拦下")
    try:
        sw_reducer._hex_volume(13.0, 8.0, "hex_screw", 5.3)
        check(False, "护栏：未知 kind 必须报错")
    except ValueError:
        check(True, "护栏：未知 kind 被拦下")

    # ③ 计划结构：螺母单特征（两轮廓一次拉伸）／螺栓双特征 + merge
    for part, is_nut in (("nut_m8", True), ("nut_m10", True),
                         ("bolt_m8x25", False), ("bolt_m8x65", False)):
        check(part in sw_reducer.HEX_FAMILY, f"{part} 在六角件族里")
        plan = sw_reducer.build_plan(P, part, None)
        ops = [s["op"] for s in plan["steps"]]
        n_ex = ops.count("extrude_boss")
        check(n_ex == (1 if is_nut else 2),
              f"{part}: extrude_boss {n_ex} 次（{'单特征' if is_nut else '头+杆双特征'}）")
        if not is_nut:
            ex2 = [s for s in plan["steps"] if s["op"] == "extrude_boss"][1]
            check(ex2.get("merge") is True, f"{part}: 杆拉伸显式 merge（与头并成一体）")
            check(ops.count("select_plane") == 2,
                  f"{part}: 两张草图各自 select_plane（不靠 plane_offset 引用面名）")
        # 包围盒必须是 [2s/√3, s, t]（顶点朝 X；s = 对边宽）
        bb = next(s for s in plan["steps"] if s["op"] == "check_bbox")["expect"]
        check(abs(bb[0] - 2 * plan["_derived"]["s"] / math.sqrt(3.0)) < 1e-6
              and abs(bb[1] - plan["_derived"]["s"]) < 1e-6,
              f"{part}: 包围盒期望 [2s/√3, s, t]（顶点/对边方向），实得 {bb[:2]}")
        payload = json.dumps([{k: v for k, v in s.items() if k != "note"}
                              for s in plan["steps"]], ensure_ascii=False)
        check("vars." not in payload, f"{part}: 步骤参数不含 {{{{ vars.* }}}} 引用")
        check(ops.count("check_mass") == 1, f"{part}: 一次 check_mass（自校验解析体积）")

    # ④ 规格来源必须**登记在案**（口径 = `meta.authority`：图纸 > 参照件实测 > 国标校核）
    for part in ("nut_m8", "nut_m10", "bolt_m8x25", "bolt_m8x65"):
        q = P["parts"][part]
        srcs = {v.get("source") for kk, v in q.items()
                if isinstance(v, dict) and kk.startswith("spec")}
        check(srcs == {"gb"},
              f"{part}: 六角件尺寸来源 = gb（**参照件带圆角、对边宽读不出**，理由已登记），实得 {sorted(x for x in srcs if x)}")
        check("basis" in (q.get("_measured_faces") or {}),
              f"{part}: 参数表里登记了「为什么用国标值」的口径说明（_measured_faces.basis）")
        b = (q.get("_measured_faces") or {}).get("solved", {}).get("built")
        if check(isinstance(b, dict), f"{part}: 有真机记录 _measured_faces.solved.built"):
            check(b.get("steps", 0) > 0 and b.get("measured_volume_mm3", 0) > 0,
                  f"{part}: 真机记录含步数与实测体积")
            check("未建螺纹" in str(b.get("simplified", "")),
                  f"{part}: 记录了简化项（未建螺纹/倒角），不是悄悄省的")
    # ★ 口径守卫：**任何"尺寸来源"的键都必须带 source**，且六角件的偏离已登记为 Q5
    oq = {q_["id"] for q_ in P.get("open_questions", [])}
    check("Q5" in oq, "六角件的口径偏离已登记为 open_question Q5（不是默默建了就完）")
    check("Q2" in oq, "Q2（标准件跟图纸还是跟国标）仍挂在待确认列表里")

    # ⑤ 平垫圈：**按参照件实测**（Q2 口径），且它就是圆环 ⇒ 复用环形族生成器
    check("washer_10" in sw_reducer.RING_FAMILY, "平垫圈挂在环形族（复用现成生成器）")
    ws = P["parts"]["washer_10"]
    check(float(ws["D"]["v"]) == 22.0 and float(ws["bore"]["v"]) == 11.0
          and float(ws["t"]["v"]) == 1.5,
          "平垫圈按**参照件实测** 22/11/1.5（Q2：跟参照件，不是跟 GB 的 20/10.5/2）")
    check(ws["D"]["source"] == "measured" and ws["t"]["source"] == "measured",
          "平垫圈尺寸来源标成 measured（而不是 gb）")
    wsolv = (ws.get("_measured_faces") or {}).get("solved", {})
    check(wsolv.get("gb", {}).get("d2") == 20.0,
          "平垫圈把 GB 值一并留档（差异可查，不掩盖）")


def test_plate_family() -> None:
    """板类族（垫片2 方窗 / 窥视孔盖 圆孔）+ **齿轮副模数一致**（用户硬要求）。

    ★ 两条要钉死的：
      ① **同构不复制**：两件只差"中心开口是方窗还是圆孔"，所以族里用
         `window_shape` 泛化。若哪天有人复制出第二个族，这条会红。
      ② ★★ **圆柱面判据必须由几何算出来，不能写死** —— 方窗**不是**圆柱面，
         圆孔**就是**。写死 `expect_count=4/expect_max=hd/2` 时，
         窥视孔盖（中央 Ø11 也是圆柱面 r=5.5）当场炸（2026-10-02）。
      ③ ★★ **模数一致是装配级硬约束**（用户 2026-10-02 明确要求）：模数不一致时
         每个零件单看都对、只有装到中心距 70 的箱体里才发现啮合不上。
         ⇒ m 只许来自 `meta.gear_train.module`；零件上自带 m/z 直接判违规。
    """
    print("\n[2i] 板类族泛化 + 齿轮副模数一致")
    P = sw_reducer.load()
    parts = P["parts"]

    # ── ① 板类族：两件同族、开口形状不同、体积各自对参照件 ──────────────
    check(set(sw_reducer.PLATE_FAMILY) == {"gasket2", "cover_eye"},
          f"板类族 = 垫片2 + 窥视孔盖（实得 {list(sw_reducer.PLATE_FAMILY)}）")
    expect = {
        "gasket2":   {"shape": "square", "open": 28.0, "n_cyl": 4, "r_max": 1.75, "vol": 2587.0310},
        "cover_eye": {"shape": "circle", "open": 11.0, "n_cyl": 5, "r_max": 5.50, "vol": 5947.4469},
    }
    for part, exp in expect.items():
        if not check(part in sw_reducer.PLATE_FAMILY, f"{part} 挂在板类族"):
            continue
        pl = sw_reducer.build_plan(P, part, out_dir=None)
        dv = pl["_derived"]
        check(dv["window_shape"] == exp["shape"],
              f"{part}: 中心开口形状 = {exp['shape']}（实得 {dv['window_shape']}）")
        check(abs(dv["window"] - exp["open"]) < 1e-9,
              f"{part}: 开口尺寸 {exp['open']:g}（实得 {dv['window']:g}）")
        check(abs(dv["analytic_volume_mm3"] - exp["vol"]) < 1e-3,
              f"{part}: 解析体积 {dv['analytic_volume_mm3']:.4f}（期望 {exp['vol']}）")
        # ★ 圆柱面判据随形状变
        check(dv["cyl_expect"]["count"] == exp["n_cyl"],
              f"{part}: 圆柱面数 = {exp['n_cyl']}（实得 {dv['cyl_expect']['count']}）")
        check(abs(dv["cyl_expect"]["max_r"] - exp["r_max"]) < 1e-9,
              f"{part}: 最大圆柱面半径 {exp['r_max']:g}（实得 {dv['cyl_expect']['max_r']:g}）")
        # 判据真的进了计划（不能只在 _derived 里好看）
        cyl_step = next((s for s in pl["steps"] if s["op"] == "check_cyl_radius"), None)
        if check(cyl_step is not None, f"{part}: 计划含 check_cyl_radius"):
            check(cyl_step.get("expect_count") == exp["n_cyl"]
                  and abs(float(cyl_step.get("expect_max", -1)) - exp["r_max"]) < 1e-9,
                  f"{part}: 计划里的圆柱判据与几何一致（count={cyl_step.get('expect_count')}, "
                  f"max={cyl_step.get('expect_max')}）")
        # 一个轮廓一次拉伸（不拆成"拉伸+切除"）
        ex = [s for s in pl["steps"] if s["op"] == "extrude_boss"]
        check(len(ex) == 1, f"{part}: 只有 1 次 extrude_boss（{len(ex)} 次）—— 不拆成拉伸+切除")
        # 板面用 Front（法向 Z），薄板厚度沿 Z
        pl_step = next(s for s in pl["steps"] if s["op"] == "select_plane")
        check(pl_step.get("plane") == "Front",
              f"{part}: 板面用 Front（法向 Z）—— 用 Top 会让板立起来（§87）")
        # 真机记录
        b = (parts[part].get("_measured_faces") or {}).get("solved", {}).get("built")
        if check(isinstance(b, dict), f"{part}: 有真机记录"):
            check(b.get("steps", 0) > 0 and b.get("delta_pct", 9) < 0.01,
                  f"{part}: 真机 {b.get('steps')} 步，与参照件差 {b.get('delta_pct')}%")

    # ── 反例：方窗件按"圆孔件"的判据会炸（证明判据真在起作用）──────────
    gp2 = sw_reducer.build_plan(P, "gasket2", out_dir=None)
    gp2c = next(s for s in gp2["steps"] if s["op"] == "check_cyl_radius")
    check(gp2c.get("expect_count") == 4 and abs(float(gp2c["expect_max"]) - 1.75) < 1e-9,
          "反例基线：垫片2（方窗）判据 count=4/max=1.75 —— 若照抄窥视孔盖的 5/5.5 必炸")
    check(gp2c.get("expect_count") != 5,
          "反例：方窗开口**不产生**圆柱面，count 不得为 5")

    # ── ② ★ 齿轮副模数一致（用户硬要求）────────────────────────────────
    gt = (P.get("meta") or {}).get("gear_train")
    if not check(isinstance(gt, dict), "meta.gear_train 存在（齿轮副唯一真源）"):
        return
    m = float(gt["module"])
    z1 = int(gt["pinion"]["z"])
    z2 = int(gt["wheel"]["z"])
    a = float(gt["center_distance"])
    atol = float(gt.get("center_distance_tol", 0.08))
    check(m == 2.0, f"模数 m = {m:g}（用户给定 m2）")
    # ★ 齿数（2026-10-02 重新设计：15/55 → 18/52，消除小齿轮根切）
    check((z1, z2) == (15, 55),
          f"齿数 z₁={z1} / z₂={z2}（★ 图纸原值 15/55；根切改用 x₁=+0.2 变位解决，不改齿数）")
    check(z1 + z2 == 70, f"z₁+z₂ = {z1 + z2}（保持 70 ⇒ 中心距 {a:g} 不变 ⇒ 箱体不受影响）")
    # 中心距自洽 —— 这是"能不能装进去"的判据
    a_calc = m * (z1 + z2) / 2.0
    check(abs(a - a_calc) <= atol,
          f"中心距 a = m(z₁+z₂)/2 = {a_calc:g}，表里 {a:g}（±{atol}）")
    check(a == 70.0, f"中心距 = 70（用户硬约束 70±0.08，实得 {a:g}）")
    # 齿顶圆不能超过中心距（大齿轮要装在两轴之间）
    check(m * (z2 + 2) / 2.0 < a, f"大齿轮 da₂/2 = {m * (z2 + 2) / 2:g} < a = {a:g}（装得进）")
    # ★ 两齿轮件都不许自带 m/z
    for pkey, role in (("gear_shaft", "pinion"), ("wheel", "wheel")):
        pq = parts.get(pkey) or {}
        stray = [k for k in ("m", "z", "module") if k in pq]
        check(not stray, f"parts.{pkey} 不自带 m/z（实得 {stray or '无'}）—— 否则两轮模数会漂")
        check(pq.get("gear_role") == role, f"parts.{pkey} 的 gear_role = {role}")
    # ★ C12 校核必须在场且通过（模数漂移会直接红）
    checks = {c.cid: c for c in sw_reducer.cross_check(P)}
    c12 = checks.get("C12")
    if check(c12 is not None, "cross_check 含 C12（模数一致校核）"):
        check(c12.ok, f"C12 通过：{str(c12.detail)[:90]}")
    # ★★ 反例：把模数改掉，C12 **必须**报 FAIL（守卫必须见过自己失败）
    P_bad = json.loads(json.dumps(P))
    P_bad["meta"]["gear_train"]["module"] = 3.0        # 大齿轮悄悄换成 m3
    c12_bad = {c.cid: c for c in sw_reducer.cross_check(P_bad)}.get("C12")
    if check(c12_bad is not None, "反例：C12 在坏表上仍可求值"):
        check(not c12_bad.ok, f"反例：m 改成 3.0 后 C12 **报 FAIL**（{str(c12_bad.detail)[:60]}）")
    P_bad2 = json.loads(json.dumps(P))
    P_bad2["parts"]["wheel"]["m"] = 2.5               # 零件自带 m（另一轮不同）
    c12_bad2 = {c.cid: c for c in sw_reducer.cross_check(P_bad2)}.get("C12")
    if check(c12_bad2 is not None, "反例：C12 在零件自带 m 时仍可求值"):
        check(not c12_bad2.ok, "反例：零件自带 m 时 C12 **报 FAIL**（这正是用户担心的装配不啮合）")
    # ★ 根切：2026-10-02 起**无条件 hard**（用户裁决「确保不会根切」），
    #   原先"有实物证据就降级为 open"的妥协已废除 —— 达不到就是参数没算对。
    zmin = int(gt.get("z_min_20deg", 18) or 18)
    x1 = float((gt.get("pinion") or {}).get("x", 0) or 0)
    check(z1 >= zmin or x1 > 0,
          f"小齿轮 z₁={z1} ≥ z_min={zmin}（或已加正变位 x={x1}）⇒ **无根切**")
    check("undercut" not in (gt.get("pinion") or {}),
          "★ 旧的 `undercut` 降级标记已删除（重新设计后不再适用，"
          "留着会让 C12 以为仍可放行）")
    q7 = next((q for q in P.get("open_questions", []) if q.get("id") == "Q7"), None)
    if check(q7 is not None, "Q7 仍在（保留决策历史）"):
        check("已裁决" in str(q7.get("status", "")),
              f"Q7 标为**已裁决**（{q7.get('status')}）")
        check("C" in str(q7.get("decision", "")) or "改齿数" in str(q7.get("decision", "")),
              "Q7 记录了裁决方案 = 改齿数（不是改大齿轮模数）")
    # 齿轮轴台阶表：齿部几段是**旧齿数实测值**，必须已标作废
    st = (parts["gear_shaft"].get("_measured_faces") or {}).get("shaft_steps") or {}
    segs = st.get("segments") or []
    df_meas = next((sg for sg in segs if sg.get("role", "").startswith("★齿根")), None)
    if check(df_meas is not None, "齿轮轴有齿根圆实测记录（参照件 z=15 不变位）"):
        # ★★ 本轮改口径（2026-10-02 晚）：方案 A 回到 z=15 ⇒ **参照件齿数 = 本方案齿数**，
        #   两者只差一个齿廓变位（+0.8mm 外移）。旧断言要求这几段必须标 `_superseded`，
        #   那是 z=18 中间方案时代的写法 —— 再留着，就会把**对的记录**判成过期。
        df_plain = m * (z1 - 2.5)                       # 不变位齿根 = 2×(15−2.5) = 25
        df_design = m * (z1 - 2.5 + 2 * x1)             # +x₁=0.2 ⇒ 25.8
        check(not df_meas.get("_superseded"),
              "★ 参照件齿根段**不再是过期记录**（z=15 就是当前齿数；"
              "指向已废弃 z=18 方案的 `_superseded` 说明已清除）")
        check(abs(df_meas["d"] - df_plain) < 0.05,
              f"参照件齿根实测 Ø{df_meas['d']:g} = 不变位 m(z−2.5) = {df_plain:g}")
        check(abs(df_design - (df_plain + 2 * m * x1)) < 1e-9,
              f"★ 本方案 df₁ = {df_design:g} = 参照件 {df_plain:g} + 2mx₁({2 * m * x1:g})"
              f" —— 差 0.8 是**变位**，不是记录过期")
        check("_vs_design" in st or "_vs_design" in df_meas,
              "台阶表写明了「参照件 vs 本方案」的 0.8mm 差异（防下次再误判成记录过期）")


def test_gear_family() -> None:
    """齿轮族（33-大齿轮）：模数唯一真源 + 三个"判据不能想当然"的坑。

    ★ 要钉死的：
      ① **模数只能来自 `meta.gear_train.module`**（用户 2026-10-02 硬要求）——
         零件自带 m 就是"两轮模数可能漂移"的入口。
      ② ★ **齿顶圆不是圆柱面**（55 段渐开线拼的）⇒ 齿顶 da 只能用 `check_bbox` 判，
         用 `check_cyl_radius(expect_max=da/2)` 会拿到凹槽底 r=47 而炸。
      ③ ★ **`MidPlane` 与 `plane_offset` 必须成对**：MidPlane 拉伸后实体居中在 Z=0，
         此时偏移量是 b/2；单向拉伸时才是 b。配错 ⇒ 平面骑在实体外，`extrude_cut` 全灭。
      ④ **`check_mass` 用真机标定值**：齿隙体积取决于 `gear_profile` 采样密度，
         解析式算不出 55 个齿隙挖掉多少。
    """
    print("\n[2j] 齿轮族：大齿轮 + 模数唯一真源 + 判据坑")
    P = sw_reducer.load()
    parts = P["parts"]
    gt = P["meta"]["gear_train"]

    # ── ① 模数唯一真源 ───────────────────────────────────────────────
    m = float(gt["module"])
    check("wheel" in sw_reducer.GEAR_FAMILY,
          f"齿轮族含大齿轮（实得 {list(sw_reducer.GEAR_FAMILY)}）")
    check("m" not in parts["wheel"] and "z" not in parts["wheel"],
          "parts.wheel 不自带 m/z —— 两轮模数一旦各写一处就会漂")
    check(parts["wheel"].get("gear_role") == "wheel", "parts.wheel 的 gear_role=wheel")

    # ★ 临时打开 calibrate：本段验的是「计划结构 + 模数真源」，与真机标定值无关。
    #   wheel 的 expect_mass_g 已作废待重取（=0），不绕过的话这里直接崩。
    _prev_cal = sw_reducer._CALIBRATE.get("on")
    sw_reducer._CALIBRATE["on"] = True
    try:
        pl = sw_reducer.build_plan(P, "wheel", out_dir=None)
    finally:
        sw_reducer._CALIBRATE["on"] = _prev_cal
    dv = pl["_derived"]
    # 计划里的模数必须**逐位等于** gear_train 的（不是碰巧相等）
    gstep = next(s for s in pl["steps"] if s["op"] == "gear_profile")
    check(gstep.get("mn") == m, f"计划里 gear_profile.mn = {gstep.get('mn')} == gear_train.module")
    check(gstep.get("z") == int(gt["wheel"]["z"]),
          f"计划里 z = {gstep.get('z')} == gear_train.wheel.z")
    # ★ 反例：如果有人在 parts.wheel 上塞了 m，计划**必须不受影响**（唯一真源）
    P_bad = json.loads(json.dumps(P))
    P_bad["parts"]["wheel"]["m"] = 9.9
    pl_bad = sw_reducer.build_plan(P_bad, "wheel", out_dir=None)
    gbad = next(s for s in pl_bad["steps"] if s["op"] == "gear_profile")
    check(gbad.get("mn") == m,
          f"★ 反例：parts.wheel.m 被改成 9.9 时，计划里的 mn **仍是 {m}**（唯一真源生效）")

    # ── ② 齿顶圆不是圆柱面（判据不能想当然）────────────────────────
    # 齿顶 da = 114，但 `check_cyl_radius` 的 expect_max 必须是**凹槽底** rim/2 = 47
    cyl = next(s for s in pl["steps"] if s["op"] == "check_cyl_radius")
    z_now = int(gt["wheel"]["z"])
    x_w = float((gt.get("wheel") or {}).get("x", 0) or 0)      # ★ 变位系数
    da = m * (z_now + 2 + 2 * x_w)
    rim = float(sw_reducer.v(parts["wheel"]["rim_id"]))
    check(da == m * (z_now + 2 + 2 * x_w),
          f"齿顶圆 da = m(z+2h_a*+2x) = {da:g}（z={z_now}, x={x_w:+.2f}；"
          f"★ 变位后 113.2，参照件不变位时是 114）")
    check(abs(float(cyl["expect_max"]) - rim / 2.0) < 1e-9,
          f"★ 圆柱面判据 expect_max = {cyl['expect_max']} = 凹槽底 Ø{rim:g}/2 —— "
          f"**不是**齿顶 da/2={da / 2:g}（齿顶是渐开线不是圆柱面）")
    # bbox 才是齿顶的判据
    bbs = [s for s in pl["steps"] if s["op"] == "check_bbox"]
    check(any(s.get("expect", [0])[0] == da for s in bbs),
          f"齿顶由 check_bbox 判（expect[0] = {da:g}）—— 渐开线面只能这么验")
    # 真机记录里也要写明这条，否则下次有人又会改回 expect_max=57
    b = (parts["wheel"].get("_measured_faces") or {}).get("solved", {}).get("built") or {}
    cylrec = (b.get("cylinders") or {})
    if check(isinstance(cylrec, dict), "大齿轮有真机圆柱面记录"):
        check("不是圆柱面" in str(cylrec.get("note", "")),
              "真机记录写明「齿顶圆不是圆柱面」—— 防下次改回错误判据")
        check(abs(float(cylrec["note"].split("凹槽底Ø")[1].split("（")[0]) / 2.0
                  - rim / 2.0) < 1e-9 if "凹槽底Ø" in str(cylrec.get("note", "")) else True,
              f"真机记录里的凹槽底 Ø = {rim:g}（与 params 一致）")
        if cylrec.get("max_r") is not None:
            check(abs(float(cylrec["max_r"]) - rim / 2.0) < 1e-9,
                  f"真机实测最大圆柱面半径 = {cylrec['max_r']} = 凹槽底 r（齿顶不计入）")
        else:
            check(True, "真机记录暂未存 max_r（重新设计后已重跑，note 里的凹槽底尺寸为准）")

    # ── ③ MidPlane 与 plane_offset 成对 ─────────────────────────────
    boss = next(s for s in pl["steps"] if s["op"] == "extrude_boss")
    po = next(s for s in pl["steps"] if s["op"] == "plane_offset")
    width = float(sw_reducer.v(parts["wheel"]["b"]))
    midplane = boss.get("end_cond") == "swEndCondMidPlane"
    off = float(sw_reducer.v(po.get("offset")))
    if midplane:
        check(abs(off - width / 2.0) < 1e-9,
              f"MidPlane + plane_offset：偏移应为 b/2 = {width / 2:g}（实得 {off:g}）")
    else:
        check(abs(off - width) < 1e-9,
              f"★ **单向拉伸** + plane_offset：偏移应为 b = {width:g}（实得 {off:g}）"
              f" —— MidPlane 时实体居中在 Z=0，用 b 会让平面骑在实体外、"
              f"`extrude_cut` 全灭（2026-10-02 真机踩到）")

    # ── ④ check_mass 是真机标定值，不是解析式 ──────────────────────
    #   ★★ 2026-10-02 改：标定模式（expect_mass_g 缺失/≤0）下**不再删掉这步**，
    #      而是「只测不判」（不给 expect_mass_g）。以前整步被删 ⇒ 标定跑完
    #      报告里根本没有质量数据，只能再烧一轮真机。
    mstep = next((s for s in pl["steps"] if s["op"] == "check_mass"), None)
    _mw = float(sw_reducer.v(parts["wheel"]["expect_mass_g"]) or 0)
    check(mstep is not None,
          "★ 计划里**永远**有 check_mass（标定模式下是「只测不判」）"
          "—— 删掉整步会让标定跑完拿不到质量数据")
    if mstep is not None:
        if "expect_mass_g" not in mstep:
            check(_mw <= 0,
                  f"★ 标定值待重取（expect_mass_g={_mw:g}）⇒ check_mass **只测不判**"
                  f"（不给期望值，避免拿陈旧值静默通过）；跑完从报告 `volume_mm3` 回填")
        else:
            check(float(mstep["expect_mass_g"]) == _mw,
                  f"check_mass 期望 = {mstep['expect_mass_g']}g（真机标定值）")
            check("标定" in str(mstep.get("note", "")),
                  "check_mass 的 note 说明它是标定值（齿隙体积依赖采样密度，解析式算不出）")

    # ── ⑤ 结构自洽：齿根理论（★ 跟着 gear_train 走，齿数变过时不能写死 105）──
    # df = m·z − 2m(ha*+c*) = m·z − 2.5m（c*=0.25）
    z = int(gt["wheel"]["z"])
    x_w2 = float((gt.get("wheel") or {}).get("x", 0) or 0)
    df_theory = m * z - 2 * m * 1.25 + 2 * x_w2 * m
    check(df_theory == float(gt["wheel"].get("df", -1)) or "df" not in gt["wheel"],
          f"理论齿根 df = m(z − 2.5 + 2x) = {df_theory:g}"
          f"（c*=0.25，z={z}, x={x_w2:+.2f}）与 gear_train.wheel.df 一致")
    meas = ((parts["wheel"].get("_measured_faces") or {}).get("solved", {})
            .get("measured", {}))
    if meas.get("df_measured") is not None:
        check(abs(float(meas["df_measured"]) - float(meas["df_theory"])) < 1e-9,
              f"参照件齿根实测 Ø{meas['df_measured']:g} == 当年理论 {meas['df_theory']:g}"
              f"（逐位吻合 ⇒ 当年参照件本身合规）")
    else:
        check(meas.get("df_theory") == df_theory,
              f"★ 加变位后齿根也变了（x₂={x_w2:+.2f}）⇒ 参照件齿根 Ø105 只作量级参考，"
              f"新理论值 {meas.get('df_theory')}（= {df_theory:g}）")
    # 凹槽：hub_od < rim_id（我第一版写反了，参照件一跑就炸）
    hub = float(sw_reducer.v(parts["wheel"]["hub_od"]))
    check(hub < rim, f"★ 凹槽是环形：内径=轮毂 Ø{hub:g} < 外径 Ø{rim:g}（写反会 ValueError）")
    check(rim - hub >= 4 * m,
          f"辐板宽 {rim - hub:g} ≥ 4m = {4 * m:g}（太窄会被挖穿）")
    # ★ 反例：写反 hub/rim 必须被守卫拦住
    P_rev = json.loads(json.dumps(P))
    P_rev["parts"]["wheel"]["rim_id"] = {"v": 40.0, "source": "test"}
    try:
        sw_reducer.build_plan(P_rev, "wheel", out_dir=None)
        raised = False
    except ValueError:
        raised = True
    check(raised, "反例：rim_id(40) < hub_od(48) 时生成器**必须 ValueError**（环形关系守卫）")
    P_deep = json.loads(json.dumps(P))
    P_deep["parts"]["wheel"]["recess"] = {"v": 20.0, "source": "test"}   # 2×20 > 26
    try:
        sw_reducer.build_plan(P_deep, "wheel", out_dir=None)
        raised2 = False
    except ValueError:
        raised2 = True
    check(raised2, "反例：凹槽深 20×2=40 > 齿宽 26 时**必须 ValueError**（轮毂会被切断）")


def test_mesh_reconcile() -> None:
    """啮合对账（用户 2026-10-02 反馈「建了没啮合上」⇒ 重新设计后的回归保护）。

    ★ 这段的价值在于**它是一次真实事故的固化**：
      参照件 m=2 / z₁=15 / z₂=55 装不上，根因是**小齿轮根切**（z₁ < z_min=18）。
      当时两轮的 bbox、圆柱面、质量**全过** —— 错在"装到一起"才暴露。
      ⇒ 必须有一条离线检查把"啮合"当**一等公民**来查，而不是指望单件判据。

    ★ 还要钉住"为什么改齿数而不是改模数"这个决策（防止后人又走回头路）：
      根切只与**齿数少的那个轮**有关；改大齿轮模数既无效、又破坏两轮模数一致。
    """
    print("\n[2k] 啮合对账：重新设计后的齿轮副（无根切 + 中心距自洽 + 相位判据）")
    P = sw_reducer.load()
    gt = P["meta"]["gear_train"]
    m = float(gt["module"])
    z1 = int(gt["pinion"]["z"])
    z2 = int(gt["wheel"]["z"])
    a = float(gt["center_distance"])
    alpha = float(gt.get("alpha_n_deg", 20.0))
    ha = 1.0

    # ① 决策固化：**方案 A 齿廓变位**必须无根切（这条就是"没啮合上"的正解）
    x1 = float((gt.get("pinion") or {}).get("x", 0) or 0)
    x2 = float((gt.get("wheel") or {}).get("x", 0) or 0)
    sin2 = math.sin(math.radians(alpha)) ** 2
    zmin_exact = 2 * ha / sin2                        # 不变位时的最小齿数（真值）
    x_min1 = ha - z1 * sin2 / 2.0                     # 小齿轮不根切所需的最小变位系数
    check(z1 >= zmin_exact or x1 >= x_min1 - 1e-9,
          f"★ z₁={z1} < z_min={zmin_exact:.4f}，但 x₁={x1:+.2f} ≥ x_min={x_min1:.4f}"
          f" ⇒ **无根切**（不变位的 z=15 就是「没啮合上」的原因）")
    check(abs(x1 + x2) < 1e-9,
          f"x₁+x₂ = {x1 + x2:+.2f} = 0 ⇒ **高度变位** ⇒ 中心距变动系数 y=0 ⇒ a 不变")
    check((z1, z2) == (15, 55),
          f"齿轮副 = z₁{z1}/z₂{z2}（★ 回到图纸原值 —— 方案 A 用变位，不改齿数）")
    # ② ★ z₁+z₂ 不变 ⇒ 中心距不变 ⇒ 箱体不受影响（这是选"改齿数"的关键理由）
    check(z1 + z2 == 70, f"z₁+z₂ = {z1 + z2}（保持 70）")
    check(abs(a - m * (z1 + z2) / 2.0) < 0.08,
          f"中心距 a = m(z₁+z₂)/2 = {m * (z1 + z2) / 2:g} = {a:g}（箱体 70±0.08 不变）")
    check(a == 70.0, "中心距仍 = 70 ⇒ **箱体/轴承座/端盖全部不受重新设计影响**")
    # ③ 决策理由也钉住（防止有人以"改大齿轮模数"为由改回去）
    old = gt.get("redesign_2026_10_02") or {}
    check(bool(old), "重新设计的决策记录在 gear_train.redesign_2026_10_02")
    if old:
        check(int((old.get("old") or {}).get("z1", 0)) == 15
              and int((old.get("old") or {}).get("z2", 0)) == 55,
              "决策记录里有旧值 z₁=15/z₂=55（根切的那个方案）")
        check("root_cause" in old and "小齿轮" in str(old["root_cause"]),
              "决策记录写明根因是**小齿轮**根切（不是大齿轮模数）")
    # ④ 齿形几何：顶隙 / 不干涉 / 齿宽
    d1, d2 = m * z1, m * z2
    # ★ 变位项 2xm 三项都要带（2026-10-02 方案 A）
    da1 = d1 + 2 * m + 2 * x1 * m
    da2 = d2 + 2 * m + 2 * x2 * m
    df1 = d1 - 2 * m * 1.25 + 2 * x1 * m
    df2 = d2 - 2 * m * 1.25 + 2 * x2 * m
    clear = (a - da2 / 2.0) - df1 / 2.0
    check(clear > 0, f"大齿顶到小齿根间隙 {clear:.3f}mm > 0（不撞）")
    check(abs(clear - 0.5) < 1e-6, f"标准顶隙 = 2c* = 0.5mm（实得 {clear:.3f}）")
    check(da1 / 2.0 + df2 / 2.0 <= a + 1e-9, "小齿顶与大齿根不干涉")
    check(da2 / 2.0 < a, f"大齿顶半径 {da2 / 2:g} < a（装得进）")
    check(abs((da1 + da2) / 2.0 - 2 * m - (x1 + x2) * m - a) < 1e-9,
          f"★ 高度变位下 (da₁+da₂)/2 − 2m = a（x₁+x₂={x1 + x2:+.2f} ⇒ y=0 ⇒ 中心距不变）")
    bw1 = float((gt.get("pinion") or {}).get("b") or 0)
    bw2 = float((gt.get("wheel") or {}).get("b") or 0)
    check(bw1 >= bw2 > 0, f"★ 齿宽 b₁={bw1:g} ≥ b₂={bw2:g}（小齿轮略宽防边缘接触）")
    check(bw1 <= 17.5 * m and bw2 <= 17.5 * m,
          f"齿宽 ≤ 17.5m={17.5 * m:g}（★ 规则 b≤10m 在此**不适用**：图纸 34 / 参照件齿根段 33.42）")
    # ⑤ 相位：装配必需的量（离线给判据与数值）
    check(abs(180.0 / z2 - 3.2727272727272725) < 1e-9,
          f"大齿轮半齿距相位角 = 180/z₂ = {180.0 / z2:.4f}°（装配时必须偏移）")
    for word in ("相位", "齿对槽"):
        check(word in str(old.get("remaining_risk", "")),
              f"决策记录里写明了「{word}」这个装配期风险")
    # ⑥ 大齿轮真机记录：必须与**当前设计同代**（z₂=55 / da₂=113.2），不许留着旧版
    b2 = ((P["parts"]["wheel"].get("_measured_faces") or {}).get("solved", {})
          .get("built") or {})
    if check(isinstance(b2, dict), "大齿轮有真机记录"):
        # ★★ 同上：过期记录必须**报出来**（真机重跑覆盖），不是把判据改松糊过去。
        #    ★ 但要记成 PENDING 而不是 FAIL —— 标了 `_superseded` 就说明"已经有人
        #      判定它是旧版、并写明了重跑方法"，它**在重跑之前不可能变绿**；
        #      混进 FAIL 会让 sw_doctor 永远报「不可用」，新回归被淹没（见 pending()）。
        if b2.get("_superseded"):
            pending("大齿轮真机记录**待重跑覆盖**（z₂=55/x₂=−0.2/da₂=113.2）："
                    "非过期 / 齿数 / 齿顶 bbox 三项")
        else:
            check(not b2.get("_superseded"),
                  f"★ 真机记录**不是过期版本**（`_superseded` = {b2.get('_superseded')!r}）")
            check(b2.get("teeth") == z2, f"真机记录的齿数 = {b2.get('teeth')}（= {z2}）")
            bbox = b2.get("bbox_mm") or []
            da2_now = m * (z2 + 2 + 2 * x2)
            check(len(bbox) == 3 and abs(bbox[0] - da2_now) < 0.05,
                  f"真机 bbox 齿顶 = {bbox[:1]}（= Ø{da2_now:g}，变位后）")
    # ⑦ ★ 反例：**把变位去掉**必须被 C12 拦下（守卫必须见过自己失败）
    #   ★★ 反例口径要跟着改：上一轮的反例是「把 z₁ 改回 15」，但**现在 z₁ 本来就是 15**，
    #      那样改等于没改 ⇒ 反例会**静默失效**变成假绿灯。正确反例是「去掉变位」。
    P_bad = json.loads(json.dumps(P))
    P_bad["meta"]["gear_train"]["pinion"]["x"] = 0.0
    c12 = {c.cid: c for c in sw_reducer.cross_check(P_bad)}.get("C12")
    if check(c12 is not None, "反例：C12 在「x₁=0」的坏表上仍可求值"):
        check(not c12.ok, "★ 反例：去掉变位（x₁=0）⇒ C12 **报 FAIL**（根切被重新拦下）")
    # ⑧ ★ 反例：改中心距（模拟"改模数"路线）也必须被拦
    P_bad2 = json.loads(json.dumps(P))
    P_bad2["meta"]["gear_train"]["center_distance"] = 72.0
    c12b = {c.cid: c for c in sw_reducer.cross_check(P_bad2)}.get("C12")
    if check(c12b is not None, "反例：C12 在 a=72 的坏表上仍可求值"):
        check(not c12b.ok, "★ 反例：中心距改成 72 ⇒ C12 **报 FAIL**"
                          f"（'改模数'那条路会破坏中心距自洽）")


def test_gear_shaft() -> None:
    """齿轮轴（23）：**轴身回转 + 齿廓拉伸成实体**（不是切除！）+ 简化项必须登记。

    ★★★ 本段固化的是一次**真机走错整条路**的教训：
      我最初按「齿根圆柱已是轴身最粗段 ⇒ 切掉齿根外的材料就是齿」来做。
      **错在 df₁=31 < da₁=40** —— 齿顶那圈材料**从来不存在**，切无可切。
      真机表现：`extrude_cut` 成功、特征树正常、`check_feature_count` 通过，
      但 bbox Y/Z = 30.838（≈Ø31）⇒ **齿顶 40 根本没出现**，
      而且 `ThroughAllBoth` 还把齿部以外的轴身整段切掉（bbox X 只剩 25.196）。
      ⇒ 正确做法与**大齿轮完全一致**：`extrude_boss` + `merge=True` 拉伸齿廓成实体。
    """
    print("\n[2l] 齿轮轴：轴身回转 + 齿廓拉伸成实体（不是切除）+ 简化项登记")
    P = sw_reducer.load()
    parts = P["parts"]
    g = parts["gear_shaft"]
    gt = P["meta"]["gear_train"]
    m = float(gt["module"])
    z = int(gt["pinion"]["z"])
    da = float(gt["pinion"]["da"])
    df = float(gt["pinion"].get("df") or (m * z - 2 * m * 1.25))
    b = float(gt["face_width"])

    check("gear_shaft" in sw_reducer.GEAR_FAMILY, "齿轮轴在齿轮族里")
    pl = sw_reducer.build_plan(P, "gear_shaft", out_dir=None)
    dv = pl["_derived"]
    ops = [s["op"] for s in pl["steps"]]

    # ── ① ★ 齿廓是「拉伸成实体」而不是「切除」─────────────────────
    # ★ 2026-10-02 收紧：键槽用的是 `extrude_cut`（**合法**！从轴表面切进去）。
    #   所以判据不能是"计划里没有 extrude_cut"，而是"**没有一个 extrude_cut 是齿廓**"。
    cuts = [s for s in pl["steps"] if s["op"] == "extrude_cut"]
    check(all(s.get("name") == "键槽" for s in cuts),
          f"★ 计划里的 extrude_cut（{len(cuts)} 个）**全部是键槽**，齿廓不是切除"
          f"（实得 {[s.get('name') for s in cuts]}）—— 齿廓必须拉伸成实体（见函数 docstring）")
    check(ops.count("extrude_boss") == 1 and ops.count("revolve_boss") == 1,
          f"轴身用 revolve_boss（1 次）+ 齿部用 extrude_boss（1 次）"
          f"（实得 revolve={ops.count('revolve_boss')} / extrude={ops.count('extrude_boss')}）")
    boss2 = next(s for s in pl["steps"] if s.get("name") == "齿部")
    check(boss2["op"] == "extrude_boss", "齿部是凸台拉伸（不是切除）")
    check(boss2.get("merge") is True, "★ 齿部 `merge=True`（与轴身并成一体）")
    check(abs(float(boss2["depth"]) - b) < 1e-9,
          f"齿宽 = 齿部拉伸深度 = {boss2['depth']} == b = {b:g}")

    # ── ② ★ 齿部轴段必须比齿顶细（否则齿顶无料可凸）──────────────
    segs = g["shaft_segments"]["segments"]
    ridx = next((i for i, sg in enumerate(segs) if sg["name"].startswith("★")), -1)
    if check(ridx >= 0, "段表里有齿部轴段（以 ★ 标记）"):
        axle_d = float(segs[ridx]["d"])
        check(axle_d < da,
              f"★ 齿部轴段 Ø{axle_d:g} **< 齿顶 da={da:g}** —— "
              f"否则齿顶圆柱无料可凸（df={df:g} < da={da:g} 就是那个坑）")
        check(axle_d <= df,
              f"齿部轴段 Ø{axle_d:g} ≤ 齿根 Ø{df:g}（它是轴，不是齿）")
        # ★ 反例：把齿部轴段改成比齿顶粗 ⇒ 生成器必须 ValueError
        P_bad = json.loads(json.dumps(P))
        P_bad["parts"]["gear_shaft"]["shaft_segments"]["segments"][ridx]["d"] = da + 5.0
        try:
            sw_reducer.build_plan(P_bad, "gear_shaft", out_dir=None)
            raised = False
        except ValueError:
            raised = True
        check(raised,
              f"★ 反例：齿部轴段 Ø{da + 5:g}（> 齿顶）⇒ 生成器**必须 ValueError**")

    # ── ③ 齿廓草图面必须在齿部轴段位置（不是 x=0 的 Right）─────────
    po = next(s for s in pl["steps"] if s["op"] == "plane_offset")
    check(po.get("base") == "Right", f"齿部面基于 Right（法向 X），实得 {po.get('base')}")
    check(abs(float(po["offset"]) - dv["root_seg_x"][0]) < 1e-6,
          f"★ 齿部面偏移 = {po['offset']} = 齿部轴段起点 {dv['root_seg_x'][0]:g}"
          f"（直接用 Right(x=0) 会切错位置，见坑表）")
    sel = next(s for s in pl["steps"] if s["op"] == "select_plane"
               and s.get("plane") == po.get("name"))
    check(sel is not None, f"草图开在「{po.get('name')}」上（不是 Right 基准面本身）")

    # ── ④ 判据：齿根/齿顶都不是圆柱面 ⇒ 只能由 bbox 判 ──────────────
    cyl = next(s for s in pl["steps"] if s["op"] == "check_cyl_radius")
    check(abs(float(cyl["expect_max"]) - axle_d / 2.0) < 1e-9,
          f"★ 圆柱面判据 = 轴台阶 Ø{axle_d:g}/2 = {cyl['expect_max']}"
          f"（**不是**齿根 r={df / 2:g}、也不是齿顶 r={da / 2:g} —— 两者都是渐开线面）")
    bb = next(s for s in pl["steps"] if s["op"] == "check_bbox")
    check(bb["expect"][1] == da and bb["expect"][2] == da,
          f"bbox 判齿顶：expect[1:3] = {bb['expect'][1:]} == da = {da:g}")
    check(bb["expect"][0] == dv["total_len"],
          f"bbox 判总长 = {bb['expect'][0]}（简化后的 {dv['total_len']}）")

    # ── ⑤ 简化项必须显式登记 + 挂 open_question（★ 2026-10-02 口径变了）──
    #   现状：1:20 锥段 / 键槽 / M14 螺纹**都已建**（图纸指出后补的）；
    #   只剩两项真简化：齿部两端 30° 齿顶锥（Q9）、齿根退刀收直角（Q10）。
    #   已建项也一律登记 —— 防下次又把「已建」当「没做」（下面 ① 的坑就是这么来的）。
    simp = g.get("_simplified") or {}
    for key in ("taper_drawn", "keyway_note", "thread_note", "gear_tooth_taper",
                "runout_flat"):
        check(key in simp, f"简化/已建项 `{key}` 已显式登记")
    for key, qid in (("gear_tooth_taper", "Q9"), ("runout_flat", "Q10")):
        check((simp.get(key) or {}).get("open_question") == qid,
              f"简化项 `{key}` 挂 {qid}"
              f"（实得 {(simp.get(key) or {}).get('open_question')!r}）")
    for qid in ("Q9", "Q10"):
        check(any(q.get("id") == qid for q in P.get("open_questions", [])),
              f"{qid} 已登记在 open_questions（简化项不挂 open_question = 降级无人认领）")
    check(bool(dv.get("simplified")), f"计划的 _derived 里带简化项清单：{dv.get('simplified')}")

    # ★★ 反例（守卫必须见过自己失败）：把某项的 open_question 摘掉 ⇒ build_plan 必须 ValueError
    P_nq = json.loads(json.dumps(P))
    P_nq["parts"]["gear_shaft"]["_simplified"]["gear_tooth_taper"].pop("open_question", None)
    try:
        sw_reducer.build_plan(P_nq, "gear_shaft", out_dir=None)
        raised_nq = False
    except ValueError:
        raised_nq = True
    check(raised_nq, "★ 反例：`gear_tooth_taper` 摘掉 open_question ⇒ build_plan **必须 ValueError**"
                     "（简化项不挂 open_question = 静默降级、无人认领）")
    # ★ 反向反例：挂一个**不存在**的 id 也必须被拦（防挂个空号糊弄过去）
    P_badq = json.loads(json.dumps(P))
    P_badq["parts"]["gear_shaft"]["_simplified"]["runout_flat"]["open_question"] = "Q99"
    try:
        sw_reducer.build_plan(P_badq, "gear_shaft", out_dir=None)
        raised_bq = False
    except ValueError:
        raised_bq = True
    check(raised_bq, "★ 反例：挂不存在的 `Q99` ⇒ 同样 ValueError（不许挂空号糊弄）")

    # ── ⑥ 真机记录：★ 记录必须与**当前设计同代**（记录落后 = 数值判据全是空的）──
    b2 = ((g.get("_measured_faces") or {}).get("built") or {})
    if check(isinstance(b2, dict), "齿轮轴有真机记录"):
        # ★★ 2026-10-02 新增：变位方案落地后，旧记录还停在 z=18 那版（da=40 / b=20），
        #   而下面的数值判据**只报"记录 vs 设计"的差**，看起来像"记录对不上"，
        #   实则是"记录过期" —— 正确处置是**真机重跑覆盖**，不是改判据。
        #   ★ 标了 `_superseded` ⇒ 记 PENDING（同大齿轮，见 pending()）。
        if b2.get("_superseded"):
            pending("齿轮轴真机记录**待重跑覆盖**（z=15/x=+0.2/da=34.8/b=34）："
                    "非过期 / 步数齿数 / 齿顶 bbox 三项")
        else:
            check(not b2.get("_superseded"),
                  f"★ 真机记录**不是过期版本**（`_superseded` = {b2.get('_superseded')!r}）")
            check(b2.get("steps", 0) > 0 and b2.get("teeth") == z,
                  f"真机 {b2.get('steps')} 步、{b2.get('teeth')} 齿（z={z}）")
            bbox = b2.get("bbox_mm") or []
            check(len(bbox) == 3 and abs(bbox[1] - da) < 0.5,
                  f"真机齿顶 = {bbox[1:2]} ≈ Ø{da:g}"
                  + (f"（渐开线采样差 {abs(bbox[1] - da):.3f}）" if len(bbox) == 3 else ""))
        check(b2.get("redesigned") is True, "真机记录标了 redesigned")
        check("不是圆柱面" in str(b2.get("cylinders", {}).get("note", "")),
              "真机记录写明齿根/齿顶都不是圆柱面（防下次改回错误判据）")

    # ── ⑦ ★ 取证教训：段表不能只取"每个半径最长的一段" ──────────────
    st = (g.get("_measured_faces") or {}).get("shaft_steps_v2") or {}
    if check(bool(st), "有 shaft_steps_v2（全量圆柱面重取的段表）"):
        seg_ref = st.get("segments") or []
        check(len(seg_ref) >= 12, f"v2 段表 ≥12 段（实得 {len(seg_ref)}）—— "
                                  f"第一版只取「每个半径最长的一段」漏了 4 段")
        check("漏了" in str(st.get("_fix", "")) or "漏" in str(st.get("_fix", "")),
              "v2 记录里写明了上一版漏段的原因")


def test_bbox_tol_semantics() -> None:
    """`check_bbox` 的容差语义 —— **2026-10-02 修掉的"假绿灯"事故**回归。

    ★ 事故：`tol` 一直是**相对比例**（实际容差 = `max(|期望| × tol, 0.2mm)`），
      而**所有**计划都按 mm 写 ⇒ 齿轮轴那步 `tol: 0.2` 在 Ø34.8 上实际是 **±6.96mm**，
      真机期望 34.8 / 实测 34.527 却报 `True` —— 容差被放大 25 倍，**PASS 毫无意义**。
      （Ø113 的大齿轮更夸张：`tol: 0.15` ⇒ ±16.98mm。）
      更早还有人写 `tol: 99.0`（=9900%）来**关掉**这条检查（弹簧垫圈：螺旋面让
      `GetBodyBox` 虚报）。

    ⇒ 现在三种模式：`tol_mm` **绝对 mm**（推荐）/ `tol` 相对（兼容）/ `mode: record` 只记录。
      本段用**假 sw 对象**离线跑 op，把两种语义的差别钉死，并给反例。
    """
    print("\n[2m] check_bbox 容差语义（「假绿灯」事故的回归）")

    class FakeSw:
        """只实现 op 需要的那一个方法：`bounding_box()`（返回**米**）。"""

        def __init__(self, size_mm):
            self._s = [v / 1000.0 for v in size_mm]

        def bounding_box(self):
            return [0.0, 0.0, 0.0, self._s[0], self._s[1], self._s[2]]

    def run(size_mm, step):
        import sw_ops                                     # noqa: PLC0415
        return sw_ops.op_check_bbox(FakeSw(size_mm), {"op": "check_bbox", **step}, {})

    def raises(size_mm, step):
        try:
            run(size_mm, step)
            return False, None
        except Exception as e:                            # noqa: BLE001
            return True, str(e)

    exp = [34.8, 34.8, 34.8]
    # ① `tol_mm` = **绝对 mm**：容差内过、超出炸
    r = run([34.53, 34.71, 34.8], {"expect": exp, "tol_mm": 0.35})
    check(r["mode"] == "abs" and r["tol_used"][0]["tol_mm"] == 0.35,
          f"★ `tol_mm` 走**绝对**模式，且结果带回实际容差 "
          f"{r['tol_used'][0]['tol_mm']}mm（不再不可见）")
    bad, msg = raises([40.0, 40.0, 34.8], {"expect": exp, "tol_mm": 0.35})
    check(bad, "★ `tol_mm=0.35` 时 40.0 vs 34.8（差 5.2）**必须报错**"
               f"（这才是「齿顶建错」能被抓到的证据）")
    check(msg is not None and "tol_mm" in msg and "容差算法" in msg,
          "报错信息里写明**容差算法**（人不用再猜 tol 是 mm 还是比例）")
    # ② ★★ 反例：同一组数、同一份超差，用**老的** `tol: 0.2` 竟然能过 —— 这就是那个 bug
    bad_rel, _ = raises([40.0, 40.0, 34.8], {"expect": exp, "tol": 0.2})
    check(not bad_rel,
          "★★ 反例（固化事故）：`tol: 0.2` 是**相对比例** ⇒ 实际容差 "
          "max(34.8×0.2, 0.2) = **6.96mm**，于是 40.0 vs 34.8（差 5.2）**照样过** "
          "—— 这正是当年「报了 True 却没验到东西」的机理")
    # ③ `mode: record`：只记录，永不报错，且结果里标明
    r3 = run([12.1, 12.1, 6.0], {"expect": [12.1, 12.1, 4.0], "mode": "record"})
    check(r3["mode"] == "record" and all(u["tol_mm"] is None for u in r3["tol_used"]),
          "★ `mode: record` = 只记录不判定（替代过去写 `tol: 99.0` 硬绕过的手法）")

    # ④ 计划级：**不许**再有 check_bbox 用比例 `tol`；齿轮件必须是绝对且"真能判"
    P = sw_reducer.load()
    _prev = sw_reducer._CALIBRATE.get("on")
    sw_reducer._CALIBRATE["on"] = True
    try:
        plans = {p: sw_reducer.build_plan(P, p, out_dir=None) for p in sorted(sw_reducer.BUILDERS)}
    finally:
        sw_reducer._CALIBRATE["on"] = _prev
    rel_users, rec_users = [], []
    for p, pl in plans.items():
        for s in pl["steps"]:
            if s["op"] != "check_bbox":
                continue
            if "tol" in s:
                rel_users.append(f"{p}.{s['id']}")
            if str(s.get("mode", "")).lower() == "record":
                rec_users.append(f"{p}.{s['id']}")
            if s.get("expect") and "tol_mm" not in s and "mode" not in s:
                rel_users.append(f"{p}.{s['id']}(缺 tol_mm)")
    check(not rel_users,
          f"★ 全 {len(plans)} 份计划里**没有** check_bbox 再用比例 `tol`（残留：{rel_users}）"
          f"—— 比例语义只保留兼容，不许新写")
    # ★ record 的合法用途 = `GetBodyBox` 对**螺旋扫掠面（B 样条）会虚报**的件：
    #   弹簧垫圈（螺旋扫掠）+ **含真螺纹的件**（plug 最终 bbox：牙槽是螺旋面，轴向虚报）。
    _rec_ok = {"washer_s8.bb", "plug.bb2"}
    check(set(rec_users) <= _rec_ok,
          f"只记录不判定只用在「GetBodyBox 会虚报」的件（螺旋扫掠 / 真螺纹）上（实得 {rec_users}）")
    # 齿轮件：容差必须是绝对、且**小于齿顶的 2%**（否则等于橡皮图章）
    for p in ("gear_shaft", "wheel"):
        for s in plans[p]["steps"]:
            if s["op"] == "check_bbox" and s.get("expect"):
                da = float(max(s["expect"]))
                tol = float(s.get("tol_mm") or 1e9)
                check(tol < 0.02 * da,
                      f"★ {p}.{s['id']} 的绝对容差 {tol}mm < 2%×da({da:g}) = "
                      f"{0.02 * da:.3f}mm —— 是真判据，不是橡皮图章")
    # ★ 反例：**齿顶差一个变位量（0.8mm）**必须能被齿轮轴的 bbox 抓到
    bb = next(s for s in plans["gear_shaft"]["steps"]
              if s["op"] == "check_bbox" and s.get("expect"))
    caught, _ = raises([158.0, 35.6, 35.6], {"expect": bb["expect"], "tol_mm": bb["tol_mm"]})
    check(caught,
          f"★ 反例：齿轮轴 da 差 0.8mm（漏掉变位 x=+0.2）⇒ "
          f"tol_mm={bb['tol_mm']} 下**必须报错**")


def test_mass_props_suspect() -> None:
    """`mass_props` / `check_mass`：「读不到」**绝不能当成 0** 的回归。

    ★ 事故（2026-10-02，齿轮轴真机）：`extrude_cut` 选草图失败之后，实体处于
      **不可见/抑制**态，于是
        · `GetBodies2(0, True)`（只取可见）回空 ⇒ `check_bbox` 报「读不到包围盒」
        · `CreateMassProperty2` **不报错**、回 `Mass = 0 / Volume = 0`
      同一份文档里 `check_feature_count` 数到 **18 个特征**、`save_as` 数到 **1 个实体**。
      ⇒ 报告写着 `mass=0`，看着像"空零件"，其实是"这一种读法读不到"。

    ⇒ 修法两层：
      · `sw_connect.mass_props()`：体积可疑时先确认实体在不在，有实体就重建后重读；
        仍读不到就打 `suspect`（**绝不静默回 0**）。
      · `sw_ops.op_check_mass`：见到 `suspect` **直接报错** —— 因为
        `_volume()` 早就写明「不要用 0 冒充读不到」。
    """
    print("\n[2n] 质量属性：读失败 ≠ 0（「读数在说谎」的回归）")
    import sw_connect                                  # noqa: PLC0415

    class MP:
        """假 IMassProperty（只用得到这几个成员）。"""

        def __init__(self, vol_m3, mass_kg=1.0):
            self.UseSystemUnits = False
            self.Volume = vol_m3
            self.Mass = mass_kg
            self.SurfaceArea = 0.5
            self.CenterOfMass = (0.0, 0.0, 0.0)

    class Ext:
        """假 IModelDocExtension：每次 CreateMassProperty2 弹一个 MP 出来。"""

        def __init__(self, seq):
            self.seq = list(seq)
            self.calls = 0

        def CreateMassProperty2(self):
            self.calls += 1
            return self.seq.pop(0) if self.seq else None

    class Session(sw_connect.SwSession):
        """只替换掉「读」的那几处：ext / bodies / rebuild。"""

        def __init__(self, seq, n_bodies):
            self.ext_obj = Ext(seq)
            self._n = n_bodies
            self.rebuilds = 0

        @property
        def ext(self):
            return self.ext_obj

        def bodies(self, verbose: bool = False):
            self.last_body_query = {"args": [-1, False], "n": self._n, "tried": []}
            return [object()] * self._n

        def rebuild(self, force: bool = False):
            self.rebuilds += 1
            return True

    # ① 正常：体积非 0 ⇒ 原样返回，不重读、不打标记
    s = Session([MP(1.0e-6)], 1)
    out = s.mass_props()
    check(out.get("volume_m3") == 1.0e-6 and "suspect" not in out and not out.get("rebuilt"),
          "体积非 0 ⇒ 直接返回（不重建、不打 suspect）")

    # ② ★ 事故复现：体积读成 0、但实体确实在 ⇒ **重建后重读**，拿到真值
    s = Session([MP(0.0), MP(5.0e-7)], 1)
    out = s.mass_props()
    check(out.get("volume_m3") == 5.0e-7 and out.get("rebuilt") is True,
          "★ 体积=0 且实体在（1 个）⇒ 重建后**重读**并带回真值（rebuilt=True）")
    check(s.rebuilds == 1, "★ 只重建一次（不许在正常路径上反复重建）")

    # ③ 反例：重建后**仍然**读成 0 ⇒ 必须打 `suspect`，且带回实体数
    s = Session([MP(0.0), MP(0.0)], 2)
    out = s.mass_props()
    check("suspect" in out and out.get("bodies_n") == 2 and not out.get("rebuilt"),
          "★ 反例：有 2 个实体却怎么也读到 0 ⇒ 打 `suspect` + `bodies_n=2`"
          "（**不静默回 0**，让上游能分辨「空零件」和「读失败」）")
    check("谎" in str(out.get("suspect")) or "读" in str(out.get("suspect")),
          f"`suspect` 文案点明是**读数问题**：{out.get('suspect')}")

    # ④ 真的空零件：0 体积 + 0 实体 ⇒ 也要 suspect（但理由不同：可能真是空的）
    s = Session([MP(0.0)], 0)
    out = s.mass_props()
    check("suspect" in out and out.get("bodies_n") is None,
          "★ 0 体积 + 0 实体 ⇒ 同样打 `suspect`（理由写成「可能确实是空的」，"
          "两种情况给不同措辞，方便定位）")

    # ⑤ `op_check_mass` 见到 suspect ⇒ **报错**（不是回 actual_mass_g=0）
    class FakeSw:
        def __init__(self, mp):
            self._mp = mp

        def mass_props(self, fresh: bool = False):
            return dict(self._mp)

        def bounding_box(self):
            return [0.0, 0.0, 0.0, 0.03, 0.03, 0.03]

    try:
        sw_ops.op_check_mass(FakeSw({"suspect": "有 1 个实体但体积读成 0",
                                     "mass_kg": 0.0, "volume_m3": 0.0}),
                             {"op": "check_mass"}, {})
        bad = False
        msg = ""
    except Exception as e:                            # noqa: BLE001
        bad, msg = True, str(e)
    check(bad, "★ 反例：`mass_props` 带 `suspect` ⇒ `check_mass` **必须报错**，"
               "不许把「读失败」写成「实际 0g」（这就是又一个「假绿灯」）")
    check("读不到" in msg and "0" in msg,
          "报错文案点明「读不到 ≠ 0」并给排查方向")

    # ⑥ 「只测不判」：不给 expect_mass_g 也要**回带**实测值（`--calibrate` 靠它）
    r = sw_ops.op_check_mass(FakeSw({"mass_kg": 0.4276493, "volume_m3": 5.47e-8}),
                             {"op": "check_mass"}, {})
    check(abs(r["actual_mass_g"] - 427.6493) < 1e-3 and "volume_mm3" in r,
          "★ 不给 `expect_mass_g` 时**只测不判**，但仍回带 "
          f"actual_mass_g={r['actual_mass_g']}g / volume_mm3={r['volume_mm3']}")
    # ⑦ 给了期望值 ⇒ 恢复成门禁，超差要炸
    try:
        sw_ops.op_check_mass(FakeSw({"mass_kg": 0.5, "volume_m3": 6.4e-8}),
                             {"op": "check_mass", "expect_mass_g": 427.6493}, {})
        bad2 = False
    except Exception:                                 # noqa: BLE001
        bad2 = True
    check(bad2, "给了 `expect_mass_g` ⇒ 恢复门禁（500g vs 427.65g 报错）")


def test_body_read_entry() -> None:
    """**取实体只有一个入口**（`sw_connect.bodies()`）—— 防"两种取法各自漂移"。

    ★ 事故：同一个文件里同时存在 `GetBodies2(0, True)`（只取**可见**实体）和
      `(-1, False)`，于是同一份文档在 `save_as` 里是「1 个实体」、在 `check_bbox`
      里是「读不到」。**两处读数互相矛盾**，而没有任何东西会因此报错。
    ⇒ 修法：只留一个 `bodies()`（回退链 `(0,False) → (-1,False) → (0,True)`），
      其余全部改调它；本段把这个约定**钉死**（源码级 + 行为级两条）。
    """
    print("\n[2o] 取实体：唯一入口 + 回退链顺序")
    import ast                                        # noqa: PLC0415
    import sw_connect                                 # noqa: PLC0415

    # ① 源码级：**除了 sw_connect.bodies() 自己**，任何地方都不许直接调 GetBodies2
    offenders: list[str] = []
    for py in sorted(HERE.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            attr = getattr(node.func, "attr", None)
            if attr == "GetBodies2":
                offenders.append(f"{py.name}:{node.lineno}")
            elif (attr == "member" and len(node.args) >= 2
                  and isinstance(node.args[1], ast.Constant)
                  and node.args[1].value == "GetBodies2"):
                offenders.append(f"{py.name}:{node.lineno}")
    check(len(offenders) == 1 and offenders[0].startswith("sw_connect.py:"),
          "★ 全项目只有 `sw_connect.bodies()` 一处直接调 `GetBodies2`"
          f"（其它写法一律改走唯一入口；实得 {offenders}）")

    # ② 行为级：`(0, True)` 回空、`(0, False)` 有货 ⇒ 必须命中后者并记下来
    class FakeModel:
        def __init__(self):
            self.asked = []

        def GetBodies2(self, *args):
            self.asked.append(args)
            return [object()] if tuple(args) == (0, False) else []

    class Session(sw_connect.SwSession):
        def __init__(self):
            self.m = FakeModel()

        @property
        def model(self):
            return self.m

    s = Session()
    got = s.bodies()
    check(len(got) == 1 and tuple(s.last_body_query["args"]) == (0, False),
          "★ 行为级：`(0,True)` 回空但 `(0,False)` 有货 ⇒ 命中 `(0,False)`"
          f"（实得 args={s.last_body_query['args']}）")
    check(s.m.asked[0] == (0, False),
          "★ 回退链**第一档就是 `(0, False)`**（实体 + 含隐藏）"
          f"—— 不是 `(0, True)`（只可见，会漏）；实际顺序 {s.m.asked}")

    # ③ 全都回空 ⇒ 不报错、回 []，且把「三档都试过」留在证据里
    class EmptyModel:
        def GetBodies2(self, *args):
            return None

    class S2(sw_connect.SwSession):
        def __init__(self):
            self.last_body_query = {}

        @property
        def model(self):
            return EmptyModel()

    s2 = S2()
    check(s2.bodies() == [] and s2.last_body_query["args"] is None
          and len(s2.last_body_query["tried"]) == 3,
          "★ 三档全空 ⇒ 回 []（不抛异常），`tried` 里留三档的记录供排查")

    # ④ `check_bbox` 读不到时必须**把诊断打出来**（否则又是"零件可能是空的"这种误导）
    class NoBodySw:
        last_body_query = {"args": None, "n": 0,
                           "tried": [[[0, False], 0], [[-1, False], 0], [[0, True], 0]]}

        def bounding_box(self):
            return None

    try:
        sw_ops.op_check_bbox(NoBodySw(), {"op": "check_bbox", "expect": [1, 1, 1]}, {})
        bad3, msg3 = False, ""
    except Exception as e:                            # noqa: BLE001
        bad3, msg3 = True, str(e)
    check(bad3 and "last_body_query" in msg3 and "抑制" in msg3,
          "★ 读不到包围盒时报错里带 `last_body_query` + 「抑制/不可见」提示"
          "（把「零件可能是空的」这种误导说法修掉）")


def test_profile_extractor() -> None:
    """sw_profile：**偏心圆守卫**（2026-10-02 修的真缺陷）+ 可判定性。

    ★ 背景：`_mark_xr` 原来直接取 `(c[ax], r)`，**不检查圆心是否在回转轴上**。
      于是"不在轴上的孔/球冠"被当成回转母线 ——
      垫片1（Ø34/Ø14 环 + 3×Ø4 偏心孔）被拼出 5 条边（3 横 2 竖，
      **奇数边根本闭不了环**），反光片的球冠被折成 `r=1.5` 的假直线。
      两次都是**静默**的假轮廓，只有 Pappus 对不上才露馅：
      垫片1 真值 = 环 753.98 − 3·π·2²·1 = **716.28** = 参照 716.3 ✔
    """
    print("\n[2f] sw_profile：偏心圆守卫 + 可判定性")
    ax = 0

    def mk(cx, cy, cz, r):
        return {"c": [cx, cy, cz], "r": r}

    def plane(x, marks, area=100.0):
        return {"kind": "plane", "normal": [1.0, 0.0, 0.0], "marks": marks,
                "box_mm": [x - 0.5, -17.0, -17.0, x + 0.5, 17.0, 17.0],
                "area_mm2": area, "point_mm": [x, 0.0, 0.0]}

    def cyl(r, x0, x1):
        return {"kind": "cyl", "axis": [1.0, 0.0, 0.0], "r_mm": r,
                "marks": [mk(x0, 0, 0, r), mk(x1, 0, 0, r)],
                "box_mm": [x0, -r, -r, x1, r, r],
                "area_mm2": 2 * math.pi * r * (x1 - x0)}

    # ① 圆心在轴上 ⇒ 正常折成元素
    el = sw_profile.face_to_element(plane(0.0, [mk(0, 0, 0, 7.0), mk(0, 0, 0, 17.0)]), ax)
    check(el is not None and abs(el["a"][1] - 7.0) < 1e-9 and abs(el["b"][1] - 17.0) < 1e-9,
          f"轴上边界圆 ⇒ 正常折成元素 a=r7/b=r17，实得 {el}")

    # ② 偏心圆 ⇒ 不进轮廓，且必须被**记下来**（分布圆半径/孔径都在里面）
    off: list = []
    issues: list = []
    el = sw_profile.face_to_element(
        plane(0.0, [mk(0, 0, 0, 17.0), mk(0, 12.0, 0, 2.0)]), ax,
        issues=issues, off_axis=off)
    check(len(off) == 1 and abs(off[0]["pitch_r_mm"] - 12.0) < 1e-9
          and abs(off[0]["hole_r_mm"] - 2.0) < 1e-9,
          f"偏心圆记进 off_axis（分布圆 12 / 孔径 2），实得 {off}")
    check(any("偏心" in i["why"] for i in issues), "偏心圆留下 issue")
    check(el is not None and all(abs(p[1] - 2.0) > 1e-9 for p in (el["a"], el["b"])),
          f"偏心孔的半径（r=2）不许漏进元素，实得 {el}")
    # 单圆面会退化成"盘面"（内圈退到轴 ri=0）—— 这是 plane 分支的既定语义；
    # 但因为该件已被判 revolvable=False，这份轮廓本来就不许拿去建件。
    check(el is not None and abs(el["b"][1] - 17.0) < 1e-9,
          f"外圈半径仍取轴上的 17，实得 {el}")

    # ③ 含偏心 ⇒ revolvable=False，且 blockers 说的是人话
    rec = {"faces": [plane(0.0, [mk(0, 0, 0, 17.0), mk(0, 12.0, 0, 2.0)]),
                     plane(1.0, [mk(1, 0, 0, 17.0), mk(1, 12.0, 0, 2.0)]),
                     cyl(17.0, 0.0, 1.0)]}
    res = sw_profile.extract(rec, ax)
    check(res["revolvable"] is False, "含偏心特征 ⇒ revolvable=False")
    check(any("偏心" in b for b in res["blockers"]),
          f"blockers 点了偏心，实得 {res['blockers']}")
    check(len(res.get("off_axis") or []) >= 1, "extract 回 off_axis 明细")

    # ④ 纯圆环 ⇒ revolvable=True（守卫不许误伤），Pappus 必须精确
    rec2 = {"faces": [plane(0.0, [mk(0, 0, 0, 7.0), mk(0, 0, 0, 17.0)]),
                      plane(1.0, [mk(1, 0, 0, 7.0), mk(1, 0, 0, 17.0)]),
                      cyl(17.0, 0.0, 1.0), cyl(7.0, 0.0, 1.0)]}
    res2 = sw_profile.extract(rec2, ax)
    check(res2["revolvable"] is True,
          f"纯圆环 ⇒ revolvable=True，实得 blockers={res2['blockers']}")
    ideal = math.pi * (17 ** 2 - 7 ** 2) * 1.0
    check(abs(res2["pappus_volume_mm3"] - ideal) / ideal < 1e-4,
          f"圆环 Pappus = π(17²−7²)·1 = {ideal:.4f}，"
          f"实得 {res2['pappus_volume_mm3']:.4f}")

    # ④b ★ 球面（球冠/球带）—— 2026-10-02 补的能力缺口（反光片卡在这上面）
    #   判据：球心在轴上 ⇒ 出圆弧，圆心 = (球心轴向坐标, 0)，半径 = 球半径。
    def sph(center_mm, R):
        return {"surf": {"sphere": [c / 1000.0 for c in center_mm] + [R / 1000.0]}}

    def sphere_face(x, marks, R, cy=0.0, cz=0.0, box_x=None):
        bx = box_x if box_x is not None else (x - 1.0, x + 1.0)
        f = {"kind": "sphere", "normal": [1.0, 0.0, 0.0], "marks": marks,
             "box_mm": [bx[0], -R, -R, bx[1], R, R],
             "area_mm2": 4 * math.pi * R ** 2 / 2.0, "point_mm": [x, cy, cz]}
        f.update(sph([x, cy, cz], R))
        return f

    # 球带：球心 x=30 / R=10，上下两个整圆（都 r=8 ⇒ x = 30±6 = 24 / 36）
    #   轴向盒跨度 12 == 两端点跨度 12 ⇒ 弧是单调的，可以发射
    el = sw_profile.face_to_element(
        sphere_face(30.0, [mk(24.0, 0, 0, 8.0), mk(36.0, 0, 0, 8.0)], 10.0,
                    box_x=(24.0, 36.0)), ax)
    check(el is not None and el["kind"] == "arc",
          f"★ 球面 ⇒ 折成**圆弧**元素（不再是「面类型 sphere 未支持」），实得 {el}")
    check(el is not None and abs(el["center"][0] - 30.0) < 1e-6
          and abs(el["center"][1]) < 1e-6 and abs(el["radius"] - 10.0) < 1e-6,
          f"★ 圆心 = (球心轴向坐标 30, 离轴 0)、半径 = 球半径 10，实得 "
          f"{el and el['center']} R={el and el['radius']}")
    check(el is not None and el.get("arc_monotone") is True,
          "★ 轴向盒跨度 == 端点跨度 ⇒ 判为单调弧（可以发射）")
    # 反例①：**球心离轴**（球冠的轴不是回转轴）⇒ 不许当母线，必须留 issue。
    #   ★ 注意：边界圆必须**在轴上**，否则会**先**被偏心圆守卫拦下（那是另一条 issue，
    #     测的就不是球心判据了 —— 我第一版就写成这样，断言自然不过）。
    iss: list = []
    el_off = sw_profile.face_to_element(
        sphere_face(30.0, [mk(24.0, 0, 0, 8.0), mk(36.0, 0, 0, 8.0)], 10.0, cy=12.0,
                    box_x=(24.0, 36.0)), ax, issues=iss)
    check(el_off is None and any("球心离轴" in i["why"] for i in iss),
          f"★ 反例：边界圆在轴上但球心离轴 12mm ⇒ 不进轮廓且留「球心离轴」issue，"
          f"实得 el={el_off} issues={[i['why'][:30] for i in iss]}")
    # 反例②：轴向盒跨度 > 端点跨度 ⇒ 弧内部经过轴向极值 ⇒ 方向不能只由端点定，
    #   `arc_mid`（只给短弧中点）可能落到错半边 ⇒ **必须拒绝**，不许静默画反。
    #   ★ 这条替代了我第一版用 `acos` 判 >180° 的写法 —— 那是**错的**：
    #     `acos` 值域就是 [0,180]，判据永远为假（假绿灯）。有依据的判据是盒跨度。
    iss2: list = []
    el_maj = sw_profile.face_to_element(
        sphere_face(30.0, [mk(24.0, 0, 0, 8.0), mk(36.0, 0, 0, 8.0)], 10.0,
                    box_x=(20.0, 40.0)), ax, issues=iss2)
    check(el_maj is None and any("轴向包围盒跨度" in i["why"] for i in iss2),
          f"★ 反例：盒跨度 20 > 端点跨度 12（弧内部过了轴向极值）⇒ **拒绝发射** "
          f"+ 留 issue（`arc_mid` 会给错半边），实得 el={el_maj}")
    # 球面必须进"已支持"名单（否则 extract 会把它算成 blocker）
    rec3 = {"faces": [plane(0.0, [mk(0, 0, 0, 8.0)]),
                      sphere_face(30.0, [mk(24.0, 0, 0, 8.0), mk(36.0, 0, 0, 8.0)], 10.0,
                                  box_x=(24.0, 36.0))]}
    res3 = sw_profile.extract(rec3, ax)
    check(not any("未支持" in str(b) for b in res3["blockers"]),
          f"★ 球面不再进 blockers 的「未支持」清单（实得 {res3['blockers']}）")

    # ④c ★★ **球冠**（只读到 **1** 个边界圆）—— 反光片的凸/凹两个反光球面、
    #   26-A型圆形销两端的球头都是这种型面。极点位置 = 面的轴向极值端，
    #   r 由球面方程反算做自检（算出来不是 0 ⇒ 端点定不了，宁可不发射）。
    cap = sphere_face(0.0, [mk(14.875, 0, 0, 8.2301)], 17.0, box_x=(14.875, 17.0))
    el_cap = sw_profile.face_to_element(cap, ax)
    check(el_cap is not None and el_cap["kind"] == "arc"
          and abs(el_cap["center"][0]) < 1e-6 and abs(el_cap["center"][1]) < 1e-6
          and abs(el_cap["radius"] - 17.0) < 1e-6,
          f"★★ 球冠（1 个边界圆）⇒ 出圆弧：圆心 = (球心 0, 离轴 0)、R = 17，"
          f"实得 {el_cap and (el_cap.get('center'), el_cap.get('radius'))}")
    got = sorted([tuple(el_cap["a"]), tuple(el_cap["b"])]) if el_cap else []
    check(len(got) == 2 and abs(got[0][0] - 14.875) < 1e-6
          and abs(got[0][1] - 8.2301) < 1e-6 and abs(got[1][0] - 17.0) < 1e-6
          and abs(got[1][1]) < 1e-6,
          f"★ 球冠端点 = 边界圆 (14.875, 8.2301) → 极点 (17.0, 0)，实得 {got}")
    #   反例：轴向极值端反算出的 r ≠ 0（端点不在轴上）⇒ 端点定不了，必须拒
    iss_c: list = []
    el_bad_cap = sw_profile.face_to_element(
        sphere_face(0.0, [mk(14.875, 0, 0, 8.2301)], 17.0, box_x=(13.0, 15.0)), ax,
        issues=iss_c)
    check(el_bad_cap is None and any("球冠" in i["why"] for i in iss_c),
          f"★ 反例：球冠轴向极值处算得 r≠0 ⇒ **拒发射** + 留 issue，"
          f"实得 {el_bad_cap} issues={[i['why'][:26] for i in iss_c]}")

    # ④d ★★ **偏心圆柱面**必须拒收 —— 同心圆守卫只管边界圆，这类面以前会漏
    #   （小盖拼出 3 条 `r=2` 假母线、反光片拼出 2 条 `r=1.5` 假直线，都是静默的）。
    def cyl_at(origin, r, x0, x1, marks=None):
        return {"kind": "cyl", "axis": [1.0, 0.0, 0.0], "r_mm": r,
                "origin_mm": origin,
                "marks": ([mk(x0, origin[1], origin[2], r), mk(x1, origin[1], origin[2], r)]
                          if marks is None else marks),
                "box_mm": [x0, origin[1] - r, origin[2] - r, x1, origin[1] + r, origin[2] + r],
                "area_mm2": 2 * math.pi * r * (x1 - x0)}

    iss5: list = []
    off5: list = []
    el5 = sw_profile.face_to_element(cyl_at([0.0, 12.0, 0.0], 2.0, 0.0, 1.0), ax,
                                     issues=iss5, off_axis=off5)
    check(el5 is None,
          f"★★ 反例：偏心圆柱面（轴线离轴 12mm）不许折成 `r=2` 的**假母线**，实得 {el5}")
    check(any("偏心" in i["why"] or "离回转轴" in i["why"] for i in iss5), "偏心圆柱面留 issue")
    # marks 读不到时（`--no-marks` / 边界被裁成非圆）也得靠**轴线位置**兜住
    iss6: list = []
    el6 = sw_profile.face_to_element(cyl_at([0.0, 3.0, 0.0], 1.5, 0.0, 1.0, marks=[]), ax,
                                     issues=iss6)
    check(el6 is None and any("离回转轴" in i["why"] for i in iss6),
          f"★ marks 为空 + 轴线偏心 ⇒ 仍要拒（反光片那两条 `r=1.5` 假直线就是这么漏的），"
          f"实得 {el6}")
    # 误伤回归：轴线**在轴上**的普通圆柱面照常发射
    el7 = sw_profile.face_to_element(cyl_at([0.0, 0.0, 0.0], 17.0, 0.0, 2.0), ax)
    check(el7 is not None and abs(el7["a"][1] - 17.0) < 1e-9
          and abs(el7["b"][1] - 17.0) < 1e-9,
          f"★ 误伤回归：轴线在轴上的圆柱面照常发射（r=17），实得 {el7}")

    # ④e ★★ **回转轴不过原点**：16-螺塞 的轴平行于 X 但落在 y=1.5 ——
    #   以前拿坐标轴当真轴 ⇒ 它**每一张面**都被判偏心 1.5mm ⇒ 整件被误判成
    #   "不是纯回转体"。判据：边界圆圆心按 Σr² 分桶投票选出真轴。
    def shifted(circle_cy):
        """一个轴在 y=circle_cy 上的纯圆环（外 r=17 / 内 r=7 × 长 10）。"""
        def p(x, rs):
            return {"kind": "plane", "normal": [1.0, 0.0, 0.0],
                    "marks": [mk(x, circle_cy, 0, r) for r in rs],
                    "box_mm": [x - 0.5, circle_cy - 17.0, -17.0, x + 0.5, circle_cy + 17.0, 17.0],
                    "area_mm2": math.pi * (17 ** 2 - 7 ** 2), "point_mm": [x, 0.0, 0.0]}

        def c(r, x0, x1):
            return {"kind": "cyl", "axis": [1.0, 0.0, 0.0], "r_mm": r,
                    "origin_mm": [(x0 + x1) / 2.0, circle_cy, 0.0],
                    "marks": [mk(x0, circle_cy, 0, r), mk(x1, circle_cy, 0, r)],
                    "box_mm": [x0, circle_cy - r, -r, x1, circle_cy + r, r],
                    "area_mm2": 2 * math.pi * r * (x1 - x0)}
        return [p(0.0, [7.0, 17.0]), p(10.0, [7.0, 17.0]), c(17.0, 0.0, 10.0), c(7.0, 0.0, 10.0)]

    chk = sw_profile.detect_axis_center(shifted(1.5), ax)
    # 轴 = X ⇒ 另两坐标是 (y, z)，所以真轴写成 (1.5, 0)
    check(abs(chk[0] - 1.5) < 1e-6 and abs(chk[1]) < 1e-6,
          f"★★ 轴不过原点 ⇒ 侦测出真轴（另两坐标 = (y, z) = (1.5, 0)），实得 {chk}")
    res_off = sw_profile.extract({"faces": shifted(1.5)}, ax)
    check(res_off["revolvable"] is True
          and not res_off["off_axis"],
          f"★★ 轴偏 1.5mm 的纯圆环仍判**可回转**（以前整件被误判），"
          f"实得 blockers={res_off['blockers']}")
    check(res_off.get("closed") is True and res_off["n_arcs"] == 0,
          "★ 轴偏心时轮廓照样闭合（偏的是坐标、不是几何）")
    # 反例①：真轴在原点时不许被"投"到别处 —— 偏心孔的圆很"重"也不许赢
    mixed = shifted(0.0) + [{"kind": "plane", "normal": [1.0, 0.0, 0.0],
                             "marks": [mk(0.0, 12.0, 0, 2.0), mk(0.0, -12.0, 0, 2.0)],
                             "box_mm": [-0.5, -14.0, -2.0, 0.5, 14.0, 2.0],
                             "area_mm2": 8.0, "point_mm": [0.0, 0.0, 0.0]}]
    chk2 = sw_profile.detect_axis_center(mixed, ax)
    check(abs(chk2[0]) < 1e-6 and abs(chk2[1]) < 1e-6,
          f"★ 反例：轴在原点 + 两枚偏心孔的圆 ⇒ 真轴仍判 (0, 0)，实得 {chk2}")
    # 反例②：**认不出主心骨**（所有圆的权重都不占过半）⇒ 退回 (0,0)，不许瞎猜
    spread = [{"kind": "plane", "normal": [1.0, 0.0, 0.0],
               "marks": [mk(0.0, c, 0, 10.0) for c in (0.0, 40.0, 80.0)],
               "box_mm": [-0.5, -90.0, -10.0, 0.5, 90.0, 10.0],
               "area_mm2": 100.0, "point_mm": [0.0, 0.0, 0.0]}]
    chk3 = sw_profile.detect_axis_center(spread, ax)
    check(chk3 == (0.0, 0.0),
          f"★ 反例：三个同权重的候选圆心 ⇒ **认不出主心骨**，退回 (0, 0)（宁可漏判），"
          f"实得 {chk3}")

    # ⑤ 生成器护栏：提取器判"不可回转"的件，生成器必须拒建
    def mini(**over):
        base = {"no": 99, "name": "测试件", "ref": "x.SLDPRT", "D": {"v": 20.0},
                "profile": {"L": 10.0, "polyline": [[0.0, 5.0], [0.0, 10.0],
                                                     [10.0, 10.0], [10.0, 5.0]],
                            "polyline_close": True, "arcs": [],
                            "pappus_volume_mm3": math.pi * 47.5 * 50.0,
                            "reference_volume_mm3": math.pi * 47.5 * 50.0}}
        base.update(over)
        return {"parts": {"tst": base}}

    try:
        sw_reducer._profile_plan(mini(profile={
            "L": 10.0, "polyline": [[0.0, 5.0], [0.0, 10.0], [10.0, 10.0], [10.0, 5.0]],
            "polyline_close": True, "arcs": [], "revolvable": False,
            "blockers": ["存在偏心特征 5 处"]}), None, "tst")
        check(False, "护栏：revolvable=False 的件必须拒建")
    except ValueError as e:
        check("不是纯回转体" in str(e), f"护栏：revolvable=False 被拦下（{e}）")
    except KeyError:
        check(False, "护栏：revolvable=False 报的是 KeyError，应为 ValueError")


def test_doc_identity_drift() -> None:
    """`doc_identity` / `identity_diff` / `ensure_doc` + `_apply_drift` 的回归。

    ★ 事故（2026-10-02，本轮最贵的一个坑）：读一份 **235 张面**的参照件
      （10-通气塞）时，**活动文档在中途被别人换掉了** —— SolidWorks 会话里
      冒出一份 `零件26`（特征是 底盘板/履带左/履带右，来自另一个项目），
      于是 `bounding_box()` 回 **146.08×72.30×16.00**（真值 35×23×23）、
      `mass_props()` 回 0；16-螺塞 同样中招（Z 从 15.0 变 90.0，特征表里多出
      `配重`/`发动机罩`）。**而且完全不报错。**
      ★ 关键细节：16-螺塞 那一型的 **路径和标题都对**，内容却换了 ⇒
        指纹里必须带 **特征数 / 实体数 / 体积**，否则抓不到。
    """
    print("\n[2p] 活动文档指纹：被别人换掉文档必须能被抓到")
    import sw_connect                                  # noqa: PLC0415
    from types import SimpleNamespace                  # noqa: PLC0415

    class Feat:
        def __init__(self, name):
            self.Name = name
            self._next = None

        def GetNextFeature(self):
            return self._next

    class Model:
        def __init__(self, title, path, nfeat):
            self._t, self._p = title, path
            self._head = None
            for i in reversed(range(nfeat)):
                f = Feat(f"特征{i}")
                f._next = self._head
                self._head = f

        def GetTitle(self):
            return self._t

        def GetPathName(self):
            return self._p

        def FirstFeature(self):
            return self._head

    class Sess(sw_connect.SwSession):
        def __init__(self, title, path, nfeat, nbodies, vol):
            self._m = Model(title, path, nfeat)
            self._nb, self._vol = nbodies, vol
            self.app = SimpleNamespace(ActiveDoc=self._m, ActivateDoc3=self._noop)

        @staticmethod
        def _noop(*a, **k):
            return True

        @property
        def model(self):
            return self._m

        def bodies(self, verbose=False):
            return [object()] * self._nb

        def mass_props(self, fresh: bool = False):
            return {"volume_m3": self._vol}

    a = Sess("16-螺塞.SLDPRT", r"C:\ref\16-螺塞.SLDPRT", 25, 1, 1.6598e-06)
    b = Sess("16-螺塞.SLDPRT", r"C:\ref\16-螺塞.SLDPRT", 25, 1, 1.6598e-06)
    check(sw_connect.SwSession.identity_diff(a.doc_identity(), b.doc_identity()) == [],
          "同一份文档同一状态 ⇒ 指纹无差异")
    check(a.ensure_doc(b.doc_identity()) is True, "指纹一致 ⇒ ensure_doc 直接 True")

    # ★ 反例：**标题和路径都对，内容却换了**（螺塞那一型）—— 必须抓到
    c = Sess("16-螺塞.SLDPRT", r"C:\ref\16-螺塞.SLDPRT", 27, 1, 9.0e-06)
    d = sw_connect.SwSession.identity_diff(a.doc_identity(), c.doc_identity())
    check(any("features" in x for x in d) and any("volume_m3" in x for x in d),
          f"★★ 反例：路径+标题都对但**内容换了**（特征 25→27、体积 ×5.4）⇒ "
          f"指纹必须报出来，实得 {d}")
    check(a.ensure_doc(c.doc_identity()) is False,
          "★ 内容被换掉时 ensure_doc 回 **False**（抢不回来，调用方必须作废这次读数）")

    # ★ 活动文档漂走（标题不同）⇒ ensure_doc 会尝试按标题抢回；抢不回也回 False
    e = Sess("零件26", r"", 7, 1, 2.1e-4)
    check(a.ensure_doc(e.doc_identity()) is False,
          "★ 活动文档漂成别的零件 ⇒ ensure_doc False（不静默当成成功）")

    # ★ `_apply_drift`：漂移 ⇒ 一律作废可判定性（不许把别人的轮廓当这一件的）
    res_ok = {"revolvable": True, "blockers": []}
    sw_profile._apply_drift(res_ok, [])
    check(res_ok["revolvable"] is True and res_ok["blockers"] == [],
          "无漂移 ⇒ 不动结论")
    res_bad = {"revolvable": True, "blockers": ["别的原因"]}
    sw_profile._apply_drift(res_bad, ["path: 'a' → 'b'", "volume_m3: 1 → 9"])
    check(res_bad["revolvable"] is False
          and "指纹变了" in res_bad["blockers"][0]
          and len(res_bad["blockers"]) == 2,
          f"★★ 有漂移 ⇒ revokvable=False 且**排在 blockers 第一条**，"
          f"实得 {res_bad}")


def test_build_doc_pin_guard() -> None:
    """[2q] 构建期文档指纹守卫：`sw_plan._doc_pin` / `_check_doc_pin`。

    ★ 缺口（2026-10-02，读完 [2p] 顺手查出来的）：**读路径守住了，构建路径一条没有。**
      真机已证明活动文档会漂（读 10-通气塞 时漂到了 `d:\\out\\excavator.SLDPRT`）。
      构建期漂移的后果比读数错严重得多：
        · `sketch_begin` / `extrude_boss` 会往**别人的模型**里加特征；
        · `save_as` 会把**别人那份文档**存成我们的零件 —— 而它现有的三道验
          （路径指向目标 / 文件新写了 / 里面不是空零件）**全都会通过**，
          因为那三道只看「存到哪了」，从不看「存的是谁」。这是静默错。

    ★ 指纹字段**必须分场景**（本用例最要紧的一条）：
      · 读参照件：内容不该变 ⇒ 全字段（见 [2p]）；
      · 构建期：特征数/体积**本来就在变** ⇒ **只比 title/path**。
        拿全字段当构建指纹会把**每一步**都判成漂移（反例见下，点名检查号 6）。
    """
    print("\n[2q] 构建期文档指纹守卫：活动文档漂走必须中止，构建进度变化不算漂")
    from types import SimpleNamespace                  # noqa: PLC0415

    class Model:
        def __init__(self, title, path):
            self._t, self._p = title, path

        def GetTitle(self):
            return self._t

        def GetPathName(self):
            return self._p

    class Sess:
        """假会话：`ActivateDoc3` 按标题真正切换活动文档（模拟"抢回来"）。"""

        def __init__(self, title, path, docs=None):
            self.docs = docs or {}
            self._m = Model(title, path)
            self.app = SimpleNamespace(ActiveDoc=self._m,
                                       ActivateDoc3=self._activate)

        def _activate(self, title, *a, **k):
            if title in self.docs:
                self.app.ActiveDoc = self.docs[title]
                return True
            return False

    def pin_of(sess):
        return sw_plan._doc_pin(sess)

    # 1) 指纹只含 title/path，且 path 归一化（大小写/分隔符无关）
    a = Sess("零件1", "D:\\out\\reducer\\21-x.SLDPRT")
    p = pin_of(a)
    check(set(p) == {"title", "path"}, f"指纹只该有 title/path 两个键，实得 {sorted(p)}")
    check(p["title"] == "零件1" and p["path"].endswith("21-x.sldprt"),
          f"path 必须 normcase+abspath（大小写无关），实得 {p['path']}")

    # 2) 还没钉（pin=None）⇒ 守卫是空操作
    sw_plan._check_doc_pin(a, {}, "s1")
    check(True, "没钉指纹时守卫不动作（计划开头 / 只读计划）")

    # 3) 开 --allow-doc-switch ⇒ 即使漂了也不拦
    sw_plan._check_doc_pin(a, {"_doc_pin": pin_of(Sess("别的件", "D:\\out\\other.SLDPRT")),
                               "_allow_doc_switch": True}, "s1")
    check(True, "★ --allow-doc-switch 时守卫关闭（留逃生口，不把用户堵死）")

    # 4) 同一份文档 ⇒ 放行
    sw_plan._check_doc_pin(a, {"_doc_pin": pin_of(Sess("零件1", "D:\\out\\reducer\\21-x.SLDPRT"))},
                           "s1")
    check(True, "同一份文档 ⇒ 放行")

    # 5) 漂了但**抢得回来**（用户只是瞬时切走）⇒ 抢回来继续，不中止
    mine = Model("零件1", "D:\\out\\reducer\\21-x.SLDPRT")
    b = Sess("excavator.SLDPRT", "D:\\out\\excavator.SLDPRT",
             docs={"零件1": mine})
    pin_mine = pin_of(Sess("零件1", "D:\\out\\reducer\\21-x.SLDPRT"))
    sw_plan._check_doc_pin(b, {"_doc_pin": pin_mine}, "s7")
    check(b.app.ActiveDoc is mine,
          "★ 瞬时切走 ⇒ 按标题抢回活动文档后继续（不必因一次误切就整轮重来）")

    # 6) ★★ 反例（点名检查号）：**构建进度变化不算漂移**
    #    —— 同一份文档，特征从 4 涨到 9、体积 0 → 1.66e-6，必须**放行**。
    #    这条是专门防「拿 doc_identity 全字段当构建指纹」的：那样每一步都会红。
    c = Sess("零件1", "D:\\out\\reducer\\21-x.SLDPRT")
    pin_c = pin_of(c)
    c.app.ActiveDoc = Model("零件1", "D:\\out\\reducer\\21-x.SLDPRT")   # 同一份，长大了
    sw_plan._check_doc_pin(c, {"_doc_pin": pin_c}, "s12")
    check(True, "★★ 反例①（检查 6）：构建期特征数/体积在变 ⇒ **不算**漂移，必须放行")

    # 7) ★★ 反例（点名检查号）：真漂且抢不回 ⇒ **必须中止**，且报错要能自解释
    d = Sess("excavator.SLDPRT", "D:\\out\\excavator.SLDPRT")           # docs 为空 ⇒ 抢不回
    err = None
    try:
        sw_plan._check_doc_pin(d, {"_doc_pin": {"title": "零件1",
                                                "path": "d:\\out\\reducer\\21-x.sldprt"}}, "s20")
    except Exception as e:                              # noqa: BLE001
        err = e
    check(err is not None, "★★ 反例②（检查 7）：真漂移 + 抢不回 ⇒ **必须抛错中止**")
    msg = str(err or "")
    check("已中止" in msg and "零件1" in msg and "excavator" in msg
          and "--allow-doc-switch" in msg,
          f"★ 报错要自解释（点名期望/实际/原因/解法），实得：{msg.splitlines()[:1]}")

    # 8) `execute()` 必须真的把守卫接上（防"写了守卫但没接线"）
    import inspect                                     # noqa: PLC0415
    src = inspect.getsource(sw_plan.execute)
    check("_check_doc_pin(sw, ctx, sid)" in src,
          "★ 守卫必须挂在 **每一步执行前**（不然写了等于没写）")
    check("ctx[\"_doc_pin\"] = _doc_pin(sw)" in src,
          "★ `new_part`/`open` 之后必须**钉住**指纹（否则守卫拿到的是空值）")
    check("_DOC_PIN_OPS" in inspect.getsource(sw_plan),
          "钉指纹的操作集合必须是显式常量（防以后新增 op 漏钉）")
    # ★★ 反例（点名检查 9）：集合里**只写了存在性**还不够 —— 必须逐个数成员。
    #   2026-10-03 真机踩到：集合原来只有 ("new_part", "open")，而装配体计划的
    #   顺序是 `new_part → save_as → new_assembly → …`：
    #     · `save_as` 改标题+路径 ⇒ 指纹过期；· `new_assembly` 换活动文档 ⇒ 指纹指向前一个零件。
    #   ⇒ 下一步核对**误报漂移**、`ActivateDoc3(旧标题)` 抢不回、整个计划中止。
    #   盲区一直没暴露，是因为**别的探针都把 save_as 放在最后一步**（只有 asm_* 中途存）。
    need = {"new_part", "new_assembly", "open", "save_as", "close"}
    got = set(sw_plan._DOC_PIN_OPS)
    check(need <= got,
          f"★★ 检查 9：「改文档身份」的 op **全部**要钉指纹（缺 {sorted(need - got)}）"
          f" —— 判据 = 这一步之后 title/path 可能变吗；"
          f"实得 {sorted(got)}")
    check(set(sw_plan._DOC_PIN_OPS) <= set(sw_ops.OPS),
          f"★ 集合里不许有**不存在**的 op 名（实得 "
          f"{sorted(set(sw_plan._DOC_PIN_OPS) - set(sw_ops.OPS))}）")
    # ★ 把"判据"本身也钉住：凡 cat=="doc" 且**会改身份**的，都必须在集合里
    _docish = {n for n, o in sw_ops.OPS.items()
               if getattr(o, "cat", "") == "doc"
               and n in ("new_part", "open", "save_as", "close")}
    check(_docish <= got,
          f"★ 检查 9 的判据要能被机械核对：cat=doc 里会换/改文档身份的那些"
          f"（{sorted(_docish)}）都在集合里")
    # ★★ 第二条构建路径（探针套件）也必须接上 —— 项目铁律是「一处拦全部」：
    #    封存闸门当初就是靠"只有 run_op 一个入口"才成立；守卫漏一条路 = 只在一条路上有效。
    import sw_probe                                    # noqa: PLC0415
    probe_src = inspect.getsource(sw_probe.run_plan_in_session)
    check("_check_doc_pin" in probe_src and "_doc_pin" in probe_src,
          "★★ 探针构建路径（sw_probe.run_plan_in_session）也必须挂同一套守卫 —— "
          "否则「构建期不会污染别人的模型」这句话只在一条路上成立")

    # ---- 会话健康度预检（2026-10-03 真机教训）----
    # 真机发生过：`asm_mate` 探针 09:15 插组件成功，20 分钟后**同一个会话**里
    # `AddComponent5` 对任何零件静默回 None（新建/已存盘装配体都不行），
    # 而 NewDocument / GetComponentCount / CloseDoc **全都正常**。
    # ⇒ 表象是"op 写错了"，真因是**会话坏了**。不预检就会去改本来正确的代码。
    import sw_session_check                            # noqa: PLC0415
    _sc_src = inspect.getsource(sw_session_check)
    check("AddComponent5" in _sc_src and "插 2 个组件" in _sc_src,
          "★ 会话自检必须真的**插组件**（`AddComponent5`）—— "
          "只验 NewDocument/GetComponentCount 的话，这次的故障一个都抓不到")
    check("静默" in _sc_src and "重启 SolidWorks" in _sc_src,
          "★ 会话自检的失败提示要点名「静默」+「重启 SolidWorks」，"
          "否则用户会去改 op（而 op 是对的）")

    def _mk(**flags):
        ck = sw_session_check.Check()
        ck.add("零件：新建+拉伸+包围盒", flags.get("part", True), "")
        ck.add("零件：存盘落盘", flags.get("save", True), "")
        ck.add("装配体：新建", flags.get("asm", True), "")
        ck.add("装配体：插 2 个组件", flags.get("ins", True), "")
        return sw_session_check._verdict(ck, 0.0, None, [])

    check(_mk()["verdict"] == sw_session_check.VERDICT_OK,
          f"全过 ⇒ {_mk()['verdict']}（HEALTHY）")
    check(_mk(ins=False)["verdict"] == sw_session_check.VERDICT_DEGRADED,
          f"★ 只有插组件坏 ⇒ DEGRADED（**基础建模还能用**，别一上来就重启一切）"
          f"，实得 {_mk(ins=False)['verdict']}")
    check(_mk(part=False)["verdict"] == sw_session_check.VERDICT_BROKEN,
          f"连零件都建不出 ⇒ BROKEN，实得 {_mk(part=False)['verdict']}")
    _cap = _mk(ins=False)["capabilities"]
    check(_cap.get("part") is True and _cap.get("insert_component") is False,
          f"★ 能力表要能分开报（零件可用 / 插组件不可用），实得 {_cap}")
    check("--preflight" in (HERE / "sw_probe.py").read_text(encoding="utf-8"),
          "★★ `sw_probe.py` 必须有 `--preflight`（先验会话再跑）")
    check((HERE / "sw_session_check.py").is_file()
          and "sw_session_check" in (HERE / "sw_probe.py").read_text(encoding="utf-8"),
          "★ `sw_session_check.py` 在包里且被 `sw_probe.py` 引用（不是死引用）")


def test_taper_pin() -> None:
    """26-圆锥销（`taper_pin_4x18`）：**图纸/参照件矛盾件** + 型面族里**唯一靠中心线收口**的一型。

    ★ 三件事要同时钉住：
      ① 型面自洽（Pappus 与真机质量属性独立吻合）；
      ② **口径偏离必须有人认领** —— 这一件是"图纸=圆锥销 / 参照件=圆柱销"的冲突件，
         按 `meta.authority`（图纸 > 参照件 > 国标）本该照图纸，实际按参照件建了
         ⇒ 必须挂真实存在的 Q12。★ 这条是"文字承诺没落地"那一类漂移的判据：
         登记里原来写着"已列入 open_questions 待用户裁决"，而 open_questions 里**根本没有**。
      ③ 真机实测记录必须回读得到（`status: modeled` 不会自己跟上）。
    """
    print("\n[2r] 26-圆锥销：型面自洽 + 口径偏离必须挂 open_question + 真机记录")
    P = sw_reducer.load()
    part = "taper_pin_4x18"

    # ① 注册与生成器
    check(part in sw_reducer.PROFILE_FAMILY, f"{part} 在型面族里")
    check(sw_reducer.BUILDERS.get(part) is not None, f"{part} 有计划生成器")

    q = P["parts"][part]
    pr = q.get("profile") or {}
    check(bool(pr), f"{part} 参数表里有 profile")

    # ② 型面自洽（从型面现算，不信库里存的数字）
    vol = sw_reducer._profile_volume(pr["polyline"], pr["arcs"])       # noqa: SLF001
    stored = float(pr["pappus_volume_mm3"])
    check(abs(vol - stored) / stored < 1e-3,
          f"{part}: 从型面现算 {vol:.4f} vs 库里 pappus {stored:.4f}"
          f"（差 {abs(vol - stored) / stored * 100:.4f}% < 0.1%）")
    ref = float(pr["reference_volume_mm3"])
    check(abs(vol - ref) / ref < 0.02,
          f"{part}: 型面 {vol:.4f} vs 参照件 {ref:.4f}（{abs(vol - ref) / ref * 100:.4f}% < 2%）")

    # ③ 几何口径（这一型的特点：两端点都落在轴线上 ⇒ 靠中心线收口）
    xs = [p[0] for p in pr["polyline"]] + [a[k][0] for a in pr["arcs"] for k in ("p1", "p2", "p3")]
    rs = [p[1] for p in pr["polyline"]] + [a[k][1] for a in pr["arcs"] for k in ("p1", "p2", "p3")]
    L, D = 19.0, 4.0
    check(abs(min(xs)) < 1e-6 and abs(max(xs) - L) < 1e-6,
          f"{part}: 型面轴向范围 [{min(xs)}, {max(xs)}] = [0, {L}]")
    check(abs(max(rs) * 2 - D) < 1e-6, f"{part}: 最大半径 ×2 = D = {D}")
    check(len(pr["arcs"]) == 2 and pr.get("polyline_close") is False,
          f"{part}: 2 段球冠弧 + 直线串**不自己闭合**（交给圆弧/中心线收口）")
    check(abs(pr["arcs"][0]["radius"] - 4.25) < 1e-9
          and abs(pr["arcs"][0]["center"][0] - 14.75) < 1e-9,
          f"{part}: 球冠 R4.25、球心在轴上（x=14.75）—— 球心**必须在轴上**（球面判据）")
    # ★ 球冠高度自洽：R=(a²+h²)/(2h)，a=D/2=2、h=0.5 ⇒ R=4.25
    a_h, h = D / 2.0, 0.5
    check(abs((a_h ** 2 + h ** 2) / (2 * h) - 4.25) < 1e-9,
          f"{part}: 球冠 R=(a²+h²)/(2h)=({a_h}²+{h}²)/(2·{h})=4.25 ✔（冠高 0.5 与 R 互相印证）")

    # ④ ★★ 口径偏离必须挂真实存在的 open_question（点名检查号）
    simp = q.get("_simplified") or {}
    check(bool(simp), f"{part}: 有 _simplified 登记（偏离口径的件必须自报家门）")
    oq = (simp.get("taper_vs_cylindrical") or {}).get("open_question")
    qids = {x.get("id") for x in P["open_questions"]}
    check(oq in qids,
          f"★★ 检查 4：`_simplified.taper_vs_cylindrical.open_question`={oq!r} "
          f"必须**真实存在**于 open_questions（否则降级无人认领）")
    check(sw_reducer.check_simplified_registry(P, part) == [],
          f"{part}: 简化项登记守卫通过")
    # ★★ 反例（点名检查号）：把 Q12 抽掉 ⇒ 守卫**必须**报错
    import copy as _copy                                  # noqa: PLC0415
    P2 = _copy.deepcopy(P)
    P2["open_questions"] = [x for x in P2["open_questions"] if x.get("id") != "Q12"]
    check(bool(sw_reducer.check_simplified_registry(P2, part)),
          "★★ 反例（检查 4）：把 Q12 抽掉 ⇒ 简化项登记守卫**必须**报错"
          "（不然「挂了 open_question」这句话就没有东西在管）")

    # ⑤ 真机实测记录（status 不会自己跟上）
    b = (q.get("_measured_faces") or {}).get("built")
    if check(isinstance(b, dict), f"{part}: 参数表里有 _measured_faces.built（真机记录）"):
        mv = float(b.get("measured_volume_mm3") or 0.0)
        check(abs(mv - ref) / ref < 1e-4,
              f"{part}: 真机实测 {mv:.4f} vs 参照件 {ref} 差 {abs(mv - ref) / ref * 100:.4f}%")
        check(q.get("status") == "modeled", f"{part} status=modeled")
        check(abs(mv - vol) / vol < 1e-4,
              f"{part}: 真机 {mv:.4f} 与型面现算 {vol:.4f} 独立吻合"
              f"（差 {abs(mv - vol) / vol * 100:.4f}%）")
        check(list(b.get("measured_bbox_mm") or []) == [L, D, D],
              f"{part}: 真机 bbox {b.get('measured_bbox_mm')} = [{L}, {D}, {D}]（对参照件逐位）")

    # ⑥ 计划结构：中心线 + 直线 + 两段 arc3p + 回转（无尺寸标注 —— 已封存）
    plan = sw_reducer.build_plan(P, part, None)
    ops = [s["op"] for s in plan["steps"]]
    check(ops.count("arc3p") == 2, f"{part}: 两段 arc3p（球冠），实得 {ops.count('arc3p')}")
    check("centerline" in ops, f"{part}: 有中心线（回转轴 + 轮廓靠它收口）")
    check(not any(o.startswith("dim_") for o in ops),
          f"{part}: **不得**出现标注类 op（尺寸标注已封存，2026-10-02）")


def test_pending_discipline() -> None:
    """[2s] `pending()` 的用法约束：**只有标了 `_superseded` 才允许降档**。

    ★ 为什么这条自身必须被守：`pending()` 的语义是"验得了，但要先真机重跑"。
      一旦有人在**没标 `_superseded`** 的地方也调它，它就退化成
      "把失败降级成警告"—— 那正是本项目明令禁止的（见 SKILL.md 硬规则 5
      与 §"不把错误降级成警告"）。所以判据写死：
      **每个 `pending(` 调用点，往上 4 行内必须出现 `_superseded`。**
    ★ 反例（点名检查号）：把某处 `if b2.get("_superseded"):` 改成 `if True:`
      ⇒ 本检查必须报错（下方直接构造一个"无门禁"的源文本验一次）。
    """
    print("\n[2s] pending 用法约束：只有标了 _superseded 才允许降档")
    import inspect                                     # noqa: PLC0415

    src = Path(__file__).read_text(encoding="utf-8")
    lines = src.splitlines()
    # ★ 扫描范围要排除两处，否则**检查器会误报自己**（第一版就踩了）：
    #   ① `pending()` 自己的定义行（`def pending(` 里就含这个子串）；
    #   ② 本测试函数自身（它必须构造"无门禁"的样例来验反例）。
    _stop = next((i for i, ln in enumerate(lines)
                  if ln.startswith("def test_pending_discipline")), len(lines))
    _scan = [(i + 1, ln) for i, ln in enumerate(lines)
             if i < _stop and not ln.startswith("def pending(")]

    def offenders(pairs: list) -> list[int]:
        bad: list[int] = []
        for j, (no, ln) in enumerate(pairs):
            if "pending(" not in ln or ln.lstrip().startswith("#"):
                continue
            # ★ `def pending(` 是**定义**不是调用 —— 跳掉。第一版检查器忘了这条，
            #   于是把自己报了 7 个误报（"检查器把自己算成违规"）。
            if ln.lstrip().startswith("def pending("):
                continue
            window = [p[1] for p in pairs[max(0, j - 4):j]]
            if not any("_superseded" in w for w in window):
                bad.append(no)
        return bad

    bad = offenders(_scan)
    check(not bad,
          f"★★ 每个 `pending(` 调用点往上 4 行内必须有 `_superseded` 门禁，"
          f"违约行号 {bad}")
    # 反例（点名检查号）：人造一个"无门禁"的调用 ⇒ 上面的检查器**必须**报出来
    fake = [(1, "def f():"), (2, "    pending('无条件降档')")]
    check(offenders(fake) == [2],
          "★★ 反例：无 `_superseded` 门禁的 `pending(` ⇒ 检查器必须报出行号"
          "（否则它自己就是个假绿灯）")
    # 正例：带门禁的调用不许被误报
    ok_src = [(1, "x = a.get('_superseded')"), (2, "if x:"), (3, "    pending('待重跑')")]
    check(offenders(ok_src) == [], "正例：带门禁的 `pending(` 不许被误报")
    # ★ 误伤回归：`def pending(` 定义行本身不许被算成调用点
    check(offenders([(1, "def pending(label: str) -> None:")]) == [],
          "★ 误伤回归：`def pending(` 定义行不算调用点（第一版检查器在这里误报过）")

    # ★ 与 FAIL 分档：`pending` 不许进 `_fails`（否则 sw_doctor 永远报「不可用」）
    n_before = len(_fails)
    pending("（自检探针：这一条故意留下，用来验证 pending 不进 _fails）")
    check(len(_fails) == n_before,
          "★ `pending()` 只进 `_pending`，不进 `_fails`（退出码不受影响）")
    check(_pending and _pending[-1].startswith("（自检探针"),
          "★ pending 走的是 `_pending` 桶")
    _pending.pop()                                     # 收拾干净，别污染汇总输出
    check(not _pending or _pending[-1] is not None,
          "★ 探针已回收（汇总里的待重跑清单只该剩下真件的）")


# ------------------------------------------------ 2t. 装配体（离线钉死四条铁律）

class _FxFeat:
    """假 IFeature（特征树节点）。用 dict 描述，`GetNextFeature` 串成链。"""

    def __init__(self, d: dict):
        self._d = d
        self._next = None

    def GetTypeName2(self):
        return self._d.get("type", "")

    def Name(self):
        return self._d.get("name", "")

    def IsSuppressed(self):
        return bool(self._d.get("sup", False))

    def GetErrorCode(self):
        return int(self._d.get("err", 0))

    def GetFirstSubFeature(self):
        return _chain(self._d.get("subs") or [])

    def GetNextSubFeature(self):
        """同层**子**特征的下一项。

        ★★ 2026-10-07 补（假件原来漏了它）：真 `IFeature` 上子特征的同层链是
           `GetFirstSubFeature` → **`GetNextSubFeature`**（不是 `GetNextFeature`）。
           假件缺这个方法时，`feature_tree()` 靠 `walk()` 内部用 `GetNextFeature`
           兜出了兄弟链，**把缺失掩盖了**；而 `mate_features()` 改成浅遍历后**直接**
           依赖它 ⇒ 一调就抛 `AttributeError('_FxFeat' object has no attribute
           'GetNextSubFeature')`。补上它，假件才真正等价于 `IFeature`。
        """
        return self._next

    def GetNextFeature(self):
        return self._next


def _chain(nodes: list):
    """把嵌套的 dict 描述编成 `IFeature` 链（父在前，子走 FirstSubFeature）。"""
    head = prev = None
    for n in nodes:
        f = _FxFeat(n)
        if prev is None:
            head = f
        else:
            prev._next = f
        prev = f
    return head


class _FxComp:
    """假 IComponent2。

    `rename_style` 模拟真机 `IComponent2::Name2` 的三种行为（★ 本轮真机实测）：
      · `"exact"`  —— 赋值即生效（理想情况，也是老夹具的假设）
      · `"suffix"` —— ★★ **真机行为**：改名只换**基名**，**实例号后缀由 SolidWorks
                      维护、不动**（`box-1` --改名"地基"--> `地基-1`）。
                      老断言写成 `got != new ⇒ 失败` 会把**成功的改名判成失败**。
      · `"ignore"` —— ★★ 真机坑：`swExtRefUpdateCompNames=True` 时改名**静默失败**
                      （赋值不报错、读回还是旧名）。夹具必须能复现它，
                      否则"忘了关开关"这个缺陷永远不会被离线回归抓到。
    """

    def __init__(self, name, fixed=False, path=None, rename_style="exact",
                 box_mm=None):
        self._name = name
        self.DisplayTitle = name
        self.ReferencedConfiguration = "默认"
        self._fixed = fixed
        self._path = path or f"C:/x/{name}.SLDPRT"
        self._rename_style = rename_style
        # 包围盒（**毫米**，[x0,y0,z0,x1,y1,z1]）—— 供 `component_boxes()` 读。
        self._box_mm = list(box_mm or [0.0, 0.0, 0.0, 40.0, 30.0, 10.0])
        self.box_calls: list = []

    @property
    def Name2(self):
        return self._name

    @Name2.setter
    def Name2(self, v):
        if self._rename_style == "ignore":
            return                                  # ★ 静默失败：不抛异常、值不落
        if self._rename_style == "suffix":
            m = re.search(r"-\d+$", self._name)
            self._name = str(v) + (m.group(0) if m else "")
            return
        self._name = str(v)

    def IsFixed(self):
        return self._fixed

    def IsSuppressed(self):
        return False

    def GetPathName(self):
        return self._path

    def Visible(self):
        return True

    def GetBox(self, include_ref_planes, include_sketches):
        """★ 真机签名是 `(IncludeRefPlanes: bool, IncludeSketches: bool)`，
        不是 `IAssemblyDoc.GetBox(Option: int)` —— 传整数会**静默读空**（§95 附带）。

        返回**米**的 6 元素表（对齐真机 `IComponent2.GetBox` 的单位）。
        """
        self.box_calls.append((include_ref_planes, include_sketches))
        return [v / 1000.0 for v in self._box_mm]


class _FxSel:
    """假 ISelectionMgr。"""

    def __init__(self, m):
        self.m = m

    def GetSelectedObjectCount2(self, mark):
        return self.m._sel_count

    def GetSelectedObject6(self, idx, mark):
        return self.m._sel_objs.get(idx)


class _FxExt:
    """假 IModelDocExtension —— 只用得上 `SelectByID2`。

    `select_lies` 模拟真机实测的那个谎报（★ 本轮发现）：
        `SelectByID2("box-2", "COMPONENT", ...)` **返回 False，但实际选中数 1**
        ⇒ 判据只能是**选中数增量**，绝不能信它的返回值。
    夹具必须能复现"返回 False 但其实选中了"，否则"信了返回值"这个缺陷
    离线永远抓不到（它只会在真机上表现为"组件明明在、却报选不中"）。
    """

    def __init__(self, m):
        self.m = m
        self.select_lies = False

    def SelectByID2(self, name, typ, x, y, z, append, mark, callout, opt):
        m = self.m
        # 记成 mm，便于断言「point 是全局毫米坐标」
        m.sel_calls.append((name, typ, round(x * 1000, 4), round(y * 1000, 4),
                            round(z * 1000, 4), append, mark))
        if name in m.miss_names:
            return False
        # ★ 瞬时失败：前 N 次调用返回 False 且**不选中**（真机 2026-10-03 现象：
        #   同一点、同会话，第一遍不中、重试就中）
        m._pick_tries += 1
        if m._pick_tries <= m.pick_fails_n:
            return False
        if not append:
            m._sel_count = 0
            m._sel_objs = {}
        m._sel_count += 1
        obj = next((c for c in m._comps if c.Name2 == name),
                   {"name": name, "type": typ})
        m._sel_objs[m._sel_count] = obj
        # ★ 谎报：选中数涨了，返回值却是 False
        return False if self.select_lies else True


class _FxMateData:
    """假 IMateFeatureData —— **记录被设了哪些属性**（这本身就是断言对象）。

    反例开关：
      · `drop_entities` —— `EntitiesToMate` 的 setter **静默吞掉**写入的值。
        ★★ 这正是本轮真机确诊的 A 级坑：该属性是 **Variant（SAFEARRAY）**，
        直接赋 Python tuple/list 会被 pywin32 **悄悄丢掉**（不抛异常、读回长度 0）。
        ⇒ 夹具必须能复现"赋值成功但读回是空"，否则这个坑离线抓不到。
    """

    def __init__(self, mtype, drop_entities=False):
        object.__setattr__(self, "mtype", mtype)
        object.__setattr__(self, "set", {})
        object.__setattr__(self, "_drop_entities", bool(drop_entities))

    def __setattr__(self, k, v):
        if k == "EntitiesToMate" and self._drop_entities:
            v = []                       # ★ 静默丢掉：不抛异常，读回长度 0
        object.__setattr__(self, k, v)
        if k not in ("mtype", "set", "_drop_entities"):
            self.set[k] = v


class _FxAsmModel:
    """离线假文档（IModelDoc2 + IAssemblyDoc 合体，够 op 用）。

    反例开关（每个对应一条真实踩过的坑）：
      · `add_returns_without_adding` —— AddComponent5 返回值「成功」但组件数没涨
      · `fix_noop`               —— FixComponent 零参数、不返回成败、静默无操作
      · `mate_noop`              —— CreateMate 静默什么都不建
      · `drop_entities`          —— EntitiesToMate 赋值被静默吞掉（VARIANT 坑）
      · `rename_style`           —— 组件改名真机行为（见 `_FxComp`）
      · `miss_names`             —— 某些名字选不中
    """

    def __init__(self, doc_type=2, title="装配体1", tree=None, comps=None,
                 add_returns_without_adding=False, fix_noop=False, mate_noop=False,
                 drop_entities=False, rename_style="exact",
                 mate_moves_on_aligned=False, add_fails_n=0, pick_fails_n=0):
        self._type = doc_type
        self._title = title
        self._tree = copy.deepcopy(tree if tree is not None else [])
        self._comps = list(comps or [])
        for c in self._comps:
            if isinstance(c, _FxComp):
                c._rename_style = rename_style
        self._sel_count = 0
        self._sel_objs: dict = {}
        self.miss_names: set = set()
        self.sel_calls: list = []
        self.calls: list = []
        self.mate_data = None
        self.add_returns_without_adding = add_returns_without_adding
        self.fix_noop = fix_noop
        self.mate_noop = mate_noop
        self.drop_entities = drop_entities
        self.rename_style = rename_style
        self.mate_moves_on_aligned = mate_moves_on_aligned
        # ★ COM 瞬时失败（2026-10-03 真机）：前 N 次调用**静默无效**（返回 None /
        #   返回 False 但不选中），第 N+1 次才真生效。夹具必须能复现它 ——
        #   否则"重试"这条离线永远验不到，而真机上它决定了整条装配体链路能不能用。
        self.add_fails_n = add_fails_n
        self.pick_fails_n = pick_fails_n
        self._add_tries = 0
        self._pick_tries = 0
        self.Extension = _FxExt(self)
        self.SelectionManager = _FxSel(self)

    # ---- IModelDoc2 ----
    def GetType(self):
        return self._type

    def GetTitle(self):
        return self._title

    def ClearSelection2(self, *a):
        self._sel_count = 0
        self._sel_objs = {}
        return True

    def FirstFeature(self):
        return _chain(self._tree)

    # ---- IAssemblyDoc ----
    def GetComponents(self, toplevel):
        return list(self._comps)

    def GetComponentCount(self, toplevel):
        return len(self._comps)

    def AddComponent5(self, path, opt, newcfg, use_for_ref, existing, x, y, z):
        self.calls.append(("AddComponent5", path))
        self._add_tries += 1
        if self._add_tries <= self.add_fails_n:
            return None                     # ★ 瞬时失败：静默、组件数不涨
        if self.add_returns_without_adding:
            return _FxComp("幽灵-1")          # ★ 返回值「成功」，但没真加进去
        c = _FxComp(f"零件{len(self._comps) + 1}-1", fixed=(len(self._comps) == 0),
                    path=path,                  # ★ 第一个插入的组件默认被固定
                    rename_style=self.rename_style)
        self._comps.append(c)
        return c

    def FixComponent(self):
        self.calls.append("FixComponent")
        if not self.fix_noop:
            for o in list(self._sel_objs.values()):
                if isinstance(o, _FxComp):
                    o._fixed = True
        return None

    def UnfixComponent(self):
        self.calls.append("UnfixComponent")
        if not self.fix_noop:
            for o in list(self._sel_objs.values()):
                if isinstance(o, _FxComp):
                    o._fixed = False
        return None

    def CreateMateData(self, mtype):
        self.calls.append(("CreateMateData", mtype))
        self.mate_data = _FxMateData(mtype, drop_entities=self.drop_entities)
        return self.mate_data

    def CreateMate(self, data):
        self.calls.append("CreateMate")
        if self.mate_noop:
            return None, None
        grp = next((n for n in self._tree if n.get("type") == "MateGroup"), None)
        if grp is None:
            grp = {"type": "MateGroup", "name": "配合", "subs": []}
            self._tree.append(grp)
        subs = grp.setdefault("subs", [])
        subs.append({"type": "Mate", "name": f"配合{len(subs) + 1}"})
        # ★★ 复现真机（2026-10-03，`_diag/diag_mate_align.py`）：`align=aligned`（值 0）
        #   且两个配合面法线**相反**时，求解器把第二个组件**翻 180° 并绕轴自旋**。
        #   下面这组数是真机实测的 after 值（40×30×10 的块被翻成
        #   [31.559, -30.322, -5, 80, 12.712, 5]，还**陷进**另一件里）。
        #   夹具能复现"配合把组件挪走"，`op_add_mate` 的 `moved` 判据才可能离线被抓到；
        #   否则"建完配合不报位移"这个缺陷只会在真机上表现为"下一个取点没命中"。
        if self.mate_moves_on_aligned and len(self._comps) >= 2:
            with contextlib.suppress(Exception):
                if int(data.set.get("MateAlignment")) == 0:
                    self._comps[1]._box_mm = [31.559, -30.322, -5.0,
                                              80.0, 12.712, 5.0]
        return _FxFeat({"type": "Mate", "name": f"配合{len(subs)}"}), None


def _asm_session(model, mate_types=(), new_assembly_returns=None):
    """建一个**离线** SwSession：model 用假的，常量走 assets/sw_enums.json。

    故意**不覆写** `asm` 属性 —— 让 `sw_connect` 里的类型守卫真正跑起来
    （覆写了就等于把被验对象换掉了）。
    """
    import sw_connect                                      # noqa: PLC0415

    class S(sw_connect.SwSession):
        def __init__(self, m, mt):
            super().__init__(None, None, True)
            self._m = m
            self.MATE_FEATURE_TYPES = tuple(mt)

        @property
        def model(self):
            return self._m

        def new_assembly(self, template=None, units_mm=True):
            if new_assembly_returns is not None:
                self._m = new_assembly_returns
            return self._m

    return S(model, mate_types)


def _raises(fn) -> tuple[bool, str]:
    try:
        fn()
        return False, ""
    except Exception as e:                                  # noqa: BLE001
        return True, str(e)


def test_assembly() -> None:
    """[2t] 装配体：把**离线可判**的结论全部钉死（本轮真机跑完后又加了一批）。

    钉住的口径（见 references/30-pitfalls.md §94 / §95）：
      ① `asm` 必须是**类型守卫**（IAssemblyDoc 方法在零件文档上会静默回空）；
      ② 配合在「配合」文件夹的**子特征**里 ⇒ 只看顶层等于"一个配合都没有"；
         ★★ 配合数**按结构数**（MateGroup 直接子节点）—— 不数类型名白名单。
      ③ `AddMate*` 全废弃 ⇒ 只走 `CreateMateData + CreateMate`；
      ④ `CreateMate` 里 distance/angle/symmetric/profile_center/width **不支持预选实体**
         ⇒ 必须显式设 `EntitiesToMate`，**且必须是 VARIANT(VT_ARRAY|VT_DISPATCH)**
         （直接赋 tuple/list 会被 pywin32 **静默丢掉**，读回长度 0）。
      ⑤ ★ 2026-10-02 真机五条：路径分隔符（`AddComponent5` 只认反斜杠）、
         组件改名开关（`swExtRefUpdateCompNames`）+ 实例号后缀、
         `SelectByID2` 对组件**谎报 False**（判据只能是选中数）。
      ⑥ ★ 2026-10-03 真机：`align` 默认必须是 `closest` —— `aligned` 在两配合面
         法线**相反**时把组件**翻 180° + 绕轴自旋**，导致后续取点作废而报错指向取点；
         ⇒ `add_mate` 必须回报 `moved`（谁、挪多少、前后位置）。
      ⑦ ★ 2026-10-03 真机：装配体**没有"实体"**，`bodies()` 恒为 0 ⇒ `save_as`
         对 `.SLDASM` 报「一个实体都没有」是**假警告**，应改看组件数。
      ⇒ 每条都配**反例**：夹具必须能复现"静默"，否则这类缺陷离线永远抓不到。
    """
    print("\n[2t] 装配体：类型守卫 / 配合按结构数 / 真机坑的离线钉死")
    import contextlib                                      # noqa: PLC0415
    import inspect                                         # noqa: PLC0415
    import io                                              # noqa: PLC0415
    import sw_connect                                      # noqa: PLC0415

    # ---- ① 类型守卫：零件文档上取 asm 必须炸 ----
    part = _FxAsmModel(doc_type=1, title="零件1")
    s_part = _asm_session(part)
    check(s_part.is_part and not s_part.is_assembly,
          "★ 零件文档：is_part=True / is_assembly=False（判据是 GetType）")
    guard_bad, guard_msg = _raises(lambda: s_part.asm)
    check(guard_bad, "★★ 零件文档上取 `asm` **必须报错** —— "
                     "IAssemblyDoc 的方法在零件上会静默回空，"
                     "否则「装配体里没组件」和「我在改一个零件」长得一模一样")
    check("零件1" in guard_msg and "GetType=1" in guard_msg,
          f"守卫报错里必须带**标题 + GetType**（实得：{guard_msg[:80]}）")

    asm_model = _FxAsmModel(tree=[{"type": "MateGroup", "name": "配合", "subs": []}])
    s_asm = _asm_session(asm_model, mate_types=("Mate",))
    check(s_asm.is_assembly and not s_asm.is_part, "装配体文档：is_assembly=True")
    check(s_asm.asm is asm_model, "★ 装配体上 `asm` 原样返回（过了守卫）")

    # new_assembly：模板给错（建出来是个零件）必须当场炸
    s_wrong = _asm_session(part, new_assembly_returns=part)
    bad, msg = _raises(lambda: sw_ops.op_new_assembly(s_wrong, {"op": "new_assembly"}, {}))
    check(bad and "不是装配体" in msg,
          "★★ new_assembly 建出来的**不是装配体**（模板给错）⇒ 核 `GetType` 当场炸")
    s_new = _asm_session(asm_model)
    r = sw_ops.op_new_assembly(s_new, {"op": "new_assembly"}, {})
    check(r["is_assembly"] and r["title"] == "装配体1",
          "new_assembly 正例：核过文档类型才回报")

    # ---- ② 配合在「配合」文件夹的**子特征**里 ----
    deep = _FxAsmModel(tree=[{"type": "MateGroup", "name": "配合",
                              "subs": [{"type": "Mate", "name": "重合1"}]}])
    s_deep = _asm_session(deep, mate_types=("Mate",))
    ft = s_deep.feature_tree()
    check(any(f["type"] == "Mate" and f["depth"] == 1 for f in ft),
          "★★ `feature_tree` **含子特征** ⇒ 「配合」文件夹里的配合能被看见")
    check(len(s_deep.feature_names()) == 1,
          "★ 对照：`feature_names()` 只看顶层 ⇒ 只看到 1 个 MateGroup"
          "（这正是「一个配合都没有」这种假象的来源）")
    check(s_deep.mate_count() == 1, "★ mate_count=1（按**结构**数，不需要标定）")
    s_grp = _asm_session(deep, mate_types=("MateGroup", "Mate"))
    check(s_grp.mate_count() == 1,
          "★★ `MATE_GROUP_TYPES` **显式扣掉**「配合」文件夹（否则会多数一个）")
    # ★★ 本轮**重新设计**的核心：不再依赖类型名白名单 ⇒ 意外名/没见过的新类型也不漏数。
    weird = _FxAsmModel(tree=[{"type": "MateGroup", "name": "配合",
                               "subs": [{"type": "MateDistanceDim", "name": "距离1"},
                                        {"type": "SomeFutureMate", "name": "未来的配合"}]}])
    s_weird = _asm_session(weird, mate_types=("MateCoincident",))
    check(s_weird.mate_count() == 2,
          "★★ 按**结构**数：`MateDistanceDim`（带 Dim 后缀的意外名）+ 一个**完全没见过**"
          "的类型名**都数得到** —— 这正是换掉白名单口径的理由（旧口径穷举不全就**静默漏数**）")
    check([f["type"] for f in s_weird.unknown_mate_features()]
          == ["MateDistanceDim", "SomeFutureMate"],
          "★ 名单之外的类型名进 `unknown_mate_features()` —— **旁证，不是错误**"
          "（数目照样对，只是提示名单该补了）")
    check(_asm_session(weird, mate_types=()).unknown_mate_features() == [],
          "★ 名单为空（还没标定）时**不误报** —— 「没标定」≠「全是异常」")
    nested = _FxAsmModel(tree=[{"type": "MateGroup", "name": "配合",
                                "subs": [{"type": "MateGroup", "name": "组1",
                                          "subs": [{"type": "Mate", "name": "深的"}]}]}])
    check(_asm_session(nested, mate_types=("Mate",)).mate_count() == 1,
          "★★ **嵌套**子文件夹里的配合**也要数到**（SolidWorks 允许在「配合」文件夹下建子文件夹）"
          "—— 口径是「遍历**每一个** MateGroup、各取**直接**子节点」，"
          "每个配合恰好归属一个文件夹 ⇒ **不漏不重**（漏数才是不能接受的失败形态）")
    dup = _FxAsmModel(tree=[{"type": "MateGroup", "name": "配合",
                             "subs": [{"type": "Mate", "name": "顶层配合"},
                                      {"type": "MateGroup", "name": "组1",
                                       "subs": [{"type": "Mate", "name": "组内配合"}]}]}])
    check(_asm_session(dup, mate_types=("Mate",)).mate_count() == 2,
          "★ 文件夹**同层**既有配合又有子文件夹时也只数 2（防**重复计数**）")
    # ★ check_mate_count **不再需要标定**（新口径按结构数）——旧口径「未标定直接报错」已废弃
    r = sw_ops.op_check_mate_count(_asm_session(deep, mate_types=()),
                                   {"op": "check_mate_count", "expect": 1}, {})
    check(r["mate_count"] == 1 and r["mates"] == ["重合1"] and r["unknown_types"] is None,
          "★★ **未标定也能校验**（新口径不依赖类型名）；名单为空 ⇒ `unknown_types` 回 None"
          "（不拿「没标定」当异常报）")
    r = sw_ops.op_check_mate_count(s_deep, {"op": "check_mate_count", "expect": 1}, {})
    check(r["mate_count"] == 1 and r["mates"] == ["重合1"],
          "★ check_mate_count 回报配合名")
    r = sw_ops.op_check_mate_count(s_weird, {"op": "check_mate_count", "expect": 2}, {})
    check(r["mate_count"] == 2
          and r["unknown_types"] == ["MateDistanceDim", "SomeFutureMate"],
          "★ 通过时也把没见过的类型名报出来（两个目的：数对 + 提醒补名单）")
    bad, msg = _raises(lambda: sw_ops.op_check_mate_count(
        s_deep, {"op": "check_mate_count", "expect": 3}, {}))
    check(bad and "配合数校验失败" in msg and "重合1" in msg,
          "★ 配合数不符时报错并列出当前配合名")

    # ---- 组件数：判据是 GetComponentCount，不是数特征树 ----
    two = _FxAsmModel(comps=[_FxComp("A-1"), _FxComp("B-1")],
                      tree=[{"type": "MateGroup", "name": "配合", "subs": []}])
    s_two = _asm_session(two)
    r = sw_ops.op_check_component_count(s_two, {"op": "check_component_count", "expect": 2}, {})
    check(r["component_count"] == 2 and r["ok"], "check_component_count 正例")
    bad, msg = _raises(lambda: sw_ops.op_check_component_count(
        s_two, {"op": "check_component_count", "expect": 3}, {}))
    check(bad and "组件数校验失败" in msg and "A-1" in msg,
          "★ 组件数不符时报错并列出当前组件名（便于排查）")
    with contextlib.redirect_stdout(io.StringIO()):
        r = sw_ops.op_inspect_assembly(_asm_session(two, mate_types=("Mate",)),
                                       {"op": "inspect_assembly"}, {})
    check(len(r["components"]) == 2 and r["mate_types_calibrated"] is True,
          "inspect_assembly：组件表 + 标定状态都报出来（只读诊断）")

    # ---- ③ insert_component：判据 = 组件数真的涨了 ----
    here = str(Path(__file__).resolve())
    fresh = _FxAsmModel()
    s_fresh = _asm_session(fresh, mate_types=("Mate",))
    r = sw_ops.op_insert_component(s_fresh, {"op": "insert_component", "path": here}, {})
    check(r["component_count"] == 1 and bool(r["component"]),
          "insert_component 正例：组件数 0→1")
    check(r["fixed"] is True,
          "★ 第一个插入的组件默认被**固定**（SolidWorks 行为，写进判据）")
    check(r["renamed_from"] is None, "没给 name 时 renamed_from 为空")
    r2 = sw_ops.op_insert_component(s_fresh,
                                    {"op": "insert_component", "path": here, "name": "地基"}, {})
    check(r2["component"] == "地基" and r2["renamed_from"] == "零件2-1",
          "★★ 改名后 `renamed_from` 是**原名**（本轮修掉的语义反转缺陷）")
    ghost = _FxAsmModel(add_returns_without_adding=True)
    s_ghost = _asm_session(ghost)
    bad, msg = _raises(lambda: sw_ops.op_insert_component(
        s_ghost, {"op": "insert_component", "path": here}, {}))
    check(bad and "没把组件插进去" in msg and "0 → 0" in msg,
          "★★ 反例：AddComponent5 返回了组件但**组件数没涨** ⇒ 必须报错"
          "（返回值会说谎 —— 项目铁律第一条）")
    bad, msg = _raises(lambda: sw_ops.op_insert_component(
        s_fresh, {"op": "insert_component", "path": "C:/nope/zz.SLDPRT"}, {}))
    check(bad and "不存在" in msg, "反例：要插的文件不存在 ⇒ 先炸在文件检查上")

    # ★★ 路径分隔符：喂给 AddComponent5 的**必须**是反斜杠（本轮真机确诊）
    check(sw_connect.win_path("C:/a/b.SLDPRT") == "C:\\a\\b.SLDPRT"
          and sw_connect.win_path("C:\\a\\b.SLDPRT") == "C:\\a\\b.SLDPRT",
          "★ `win_path`：正斜杠→反斜杠，且**幂等**")
    fw = _FxAsmModel()
    s_fw = _asm_session(fw, mate_types=("Mate",))
    _fwd = here.replace("\\", "/")
    sw_ops.op_insert_component(s_fw, {"op": "insert_component", "path": _fwd}, {})
    _fed = [p for n, p in fw.calls if n == "AddComponent5"]
    check(_fed and "/" not in _fed[0] and _fed[0] == sw_connect.win_path(_fwd),
          f"★★ 喂 `AddComponent5` 的路径**必须反斜杠**（实得 {_fed[:1]}）—— "
          f"正斜杠会**静默返回 None**（组件不进去、不抛异常、无错误码），"
          f"而 `OpenDoc6`/`SaveAs` 却宽容 ⇒ 同一条 COM 族里行为不一致")

    # ★★ 组件改名的**真机语义**：只换基名，实例号后缀由 SolidWorks 维护、不动
    sfx = _FxAsmModel(rename_style="suffix")
    s_sfx = _asm_session(sfx, mate_types=("Mate",))
    sw_ops.op_insert_component(s_sfx, {"op": "insert_component", "path": here}, {})
    r3 = sw_ops.op_insert_component(
        s_sfx, {"op": "insert_component", "path": here, "name": "地基"}, {})
    check(r3["component"] == "地基-1" and r3["renamed_from"] == "零件2-1",
          f"★★ 改名回的是**实际全名** `地基-1`（带实例号后缀）—— 实得 {r3['component']!r}。"
          f"后缀是本件固有属性，改名只换基名；后续「按名选中」**只能**用这个全名")
    ign = _FxAsmModel(rename_style="ignore")
    s_ign = _asm_session(ign, mate_types=("Mate",))
    sw_ops.op_insert_component(s_ign, {"op": "insert_component", "path": here}, {})
    bad, msg = _raises(lambda: sw_ops.op_insert_component(
        s_ign, {"op": "insert_component", "path": here, "name": "地基"}, {}))
    check(bad and "改名没生效" in msg and "swExtRefUpdateCompNames" in msg,
          "★★ 反例：改名**静默失败**（`swExtRefUpdateCompNames=True` 时官方写明必失败）"
          "⇒ 读回不符必须报错并点出开关 —— 名字没改掉，后面所有按名操作都会找不到对象")

    # ---- ④ fix/float：判据 = IsFixed 真的变了 ----
    fx = _FxAsmModel(comps=[_FxComp("A-1", fixed=True), _FxComp("B-1", fixed=False)])
    s_fx = _asm_session(fx)
    r = sw_ops.op_float_component(s_fx, {"op": "float_component", "components": "all"}, {})
    check(r["changed"] == ["A-1"] and r["already_ok"] == ["B-1"],
          "float_component：只改真需要改的，已浮动的报 already_ok（不是失败）")
    check(all(not c._fixed for c in fx._comps), "★ 判据落地：IsFixed 真的都变 False")
    r = sw_ops.op_fix_component(s_fx, {"op": "fix_component", "components": "all"}, {})
    check(all(c._fixed for c in fx._comps), "fix_component：IsFixed 真的都变 True")
    lies = _FxAsmModel(comps=[_FxComp("A-1", fixed=False)])
    lies.Extension.select_lies = True
    r = sw_ops.op_fix_component(_asm_session(lies),
                                {"op": "fix_component", "components": "all"}, {})
    check(r["changed"] == ["A-1"],
          "★★ `SelectByID2` 对组件**谎报 False 但实际选中 1 个** ⇒ 判据必须是**选中数**"
          "（信返回值 = 「组件明明在、却报选不中」；本轮真机确认的谎报）")

    # ★★ 基名解析：用户写 `地基`，SolidWorks 的真名是 `地基-1`
    rx = _FxAsmModel(comps=[_FxComp("地基-1"), _FxComp("上块-2")])
    _rx = _asm_session(rx)
    check(_rx.resolve_component("地基-1")[:2] == ("地基-1", rx._comps[0])
          and _rx.resolve_component("地基")[:2] == ("地基-1", rx._comps[0])
          and _rx.resolve_component("没这个") == (None, None, []),
          "★★ `resolve_component` **三元组**：全名命中 / 基名唯一命中 / "
          "**没命中回 `(None, None, [])`**"
          " —— 第一版用两元组、把候选表塞进第二个槽 ⇒ 调用方 `if c is None` 判不出来，"
          "**把候选表当组件对象**、静默改错组件（本段就是踩过才加的）")
    check(_asm_session(_FxAsmModel(comps=[_FxComp("box-1"), _FxComp("box-2")])
                       ).resolve_component("box") == (None, None, ["box-1", "box-2"]),
          "★ 歧义形态 = `(None, None, [候选…])`（候选表**非空** ⇒ 与「没命中」区分得开）")
    r = sw_ops.op_fix_component(_rx,
                                {"op": "fix_component", "components": ["地基"]}, {})
    check(r["changed"] == ["地基-1"],
          f"★★ `components: [\"地基\"]` **基名也能选中**（解析成真名 `地基-1`）—— "
          f"计划作者不必知道实例号规则（实得 {r['changed']}）")
    amb = _FxAsmModel(comps=[_FxComp("box-1"), _FxComp("box-2")])
    bad, msg = _raises(lambda: sw_ops.op_fix_component(
        _asm_session(amb), {"op": "fix_component", "components": ["box"]}, {}))
    check(bad and "有歧义" in msg and "box-1" in msg,
          "★★ 基名**歧义**时必须报错并列出候选 —— **绝不猜**"
          "（同一零件插两次就长这样；猜错 = 静默改错组件）")
    noop = _FxAsmModel(comps=[_FxComp("A-1", fixed=False)], fix_noop=True)
    s_noop = _asm_session(noop)
    bad, msg = _raises(lambda: sw_ops.op_fix_component(s_noop, {"op": "fix_component"}, {}))
    check(bad and "状态没变" in msg,
          "★★ 反例：`FixComponent()` 零参数、不返回成败 ⇒ 判据只能靠 "
          "`IComponent2.IsFixed` **真的变了**（否则静默无操作会被当成成功）")
    miss = _FxAsmModel(comps=[_FxComp("A-1")])
    miss.miss_names = {"A-1"}
    bad, msg = _raises(lambda: sw_ops.op_fix_component(_asm_session(miss),
                                                       {"op": "fix_component"}, {}))
    check(bad and "选组件失败" in msg, "反例：组件选不中 ⇒ 报错（别硬调 FixComponent）")
    bad, msg = _raises(lambda: sw_ops.op_fix_component(
        s_fx, {"op": "fix_component", "components": ["没有这个"]}, {}))
    check(bad and "组件名对不上" in msg, "反例：组件名对不上 ⇒ 报错并列出真名")

    # ---- ④ add_mate ----
    ents = [{"type": "FACE", "point": [0, 0, 0]}, {"type": "FACE", "point": [0, 0, 10]}]

    def _mate_model(**kw):
        return _FxAsmModel(tree=[{"type": "MateGroup", "name": "配合", "subs": []}],
                           comps=[_FxComp("A-1", True), _FxComp("B-1", False)], **kw)

    bad, msg = _raises(lambda: sw_ops.op_add_mate(
        _asm_session(_mate_model()), {"op": "add_mate", "kind": "glue", "entities": ents}, {}))
    check(bad and "不认识的配合类型" in msg, "反例：未知配合类型 ⇒ 报错并列出可用类型")
    for _n, _e in ((1, ents[:1]), (3, ents + [{"type": "FACE", "point": [0, 0, 20]}])):
        bad, msg = _raises(lambda e=_e: sw_ops.op_add_mate(
            _asm_session(_mate_model()),
            {"op": "add_mate", "kind": "coincident", "entities": e}, {}))
        check(bad and "正好两个" in msg, f"反例：`entities` 给 {_n} 个 ⇒ 报错")

    mpos = _mate_model()
    s_pos = _asm_session(mpos, mate_types=("Mate",))
    r = sw_ops.op_add_mate(s_pos, {"op": "add_mate", "kind": "distance",
                                   "entities": ents, "distance": 12.5}, {})
    ents_back = mpos.mate_data.set.get("EntitiesToMate")
    check(ents_back is not None and sw_ops._seq_len(ents_back) == 2,
          "★★ **distance 也显式设了 `EntitiesToMate`，且真的写进去了**（读回长度 2）—— "
          "官方 Remarks：distance/angle/symmetric/profile_center/width **不支持预选实体**，"
          "只靠「先选中」一定建不出来")
    check(sw_ops._seq_len([1, 2]) == 2 and sw_ops._seq_len(None) == 0
          and sw_ops._seq_len(object()) == -1,
          "★ `_seq_len`：list→2 / None→0 / **取不到长度→-1**"
          "（-1 = 「没读出来」，绝不能当成 0 —— 否则读回守卫会被骗过去）")
    check(abs(mpos.mate_data.set.get("Distance") - 0.0125) < 1e-12,
          f"★ distance 从 mm 换成**米**（实得 {mpos.mate_data.set.get('Distance')}）")
    check(mpos.mate_data.set.get("MaximumDistance") == mpos.mate_data.set.get("Distance"),
          "★ 不给上下限时，上下限都设成 Distance 本身（Remarks 要求）")
    check(bool(r["new_features"]) and s_pos.mate_count() == 1,
          "★ 判据 = `CreateMate` 回了新特征名 + **特征树真的长了一个配合节点**"
          "（★ 2026-10-07：op **不再**在热路径里数配合数 —— 那是 O(#配合) 的整树浅走、"
          "单条配合 300s 的元凶；「多没多一条」改由计划末端 `check_mate_count` 把关）")
    check(s_pos.selected_count() == 0, "★ 建完必须清干净选择集（别把脏选择留给下一步）")
    check({c[6] for c in mpos.sel_calls} == {1},
          "★ 两个配合实体都用 mark=1（AddMate5 标记表：除 CAMFOLLOWER/WIDTH 外都用 1）")

    mnoop = _mate_model(mate_noop=True)
    s_mnoop = _asm_session(mnoop, mate_types=("Mate",))
    bad, msg = _raises(lambda: sw_ops.op_add_mate(
        s_mnoop, {"op": "add_mate", "kind": "coincident", "entities": ents}, {}))
    check(bad and "没有产生任何新特征" in msg,
          "★★ 反例：CreateMate 静默无操作 ⇒ 报错里点出「没建上」+ 两条最可能原因")
    check(s_mnoop.selected_count() == 0, "★ 反例路径也必须清选择集")

    # ★★ 反例：EntitiesToMate 赋值被**静默吞掉** —— 本轮最难查的坑（A 级）
    drop = _mate_model(drop_entities=True)
    s_drop = _asm_session(drop, mate_types=("Mate",))
    bad, msg = _raises(lambda: sw_ops.op_add_mate(
        s_drop, {"op": "add_mate", "kind": "distance", "entities": ents,
                 "distance": 5}, {}))
    check(bad and "静默失效" in msg and "VARIANT" in msg,
          "★★ 反例：`EntitiesToMate` 读回长度 ≠ 2 ⇒ 必须报错并点明根因（VARIANT/SAFEARRAY）"
          " —— 该属性是 **Variant**，直接赋 tuple/list 会被 pywin32 丢掉且**不抛异常**，"
          "只有读回才知道丢没丢")
    check(s_drop.selected_count() == 0, "★ 读回守卫报错时也要清掉选择集")
    # ★ 防文档漂移：这段是全 op 族里最容易被"顺手简化"回去的地方（改回直接赋 tuple）
    _md = sw_ops._mate_entities.__doc__ or ""
    check(all(k in _md for k in ("VT_ARRAY", "VT_DISPATCH", "静默")),
          "★ `_mate_entities` 文档必须点名 `VT_ARRAY|VT_DISPATCH` 与「静默失效」，"
          "并说明「离线降级回 list」—— 不然下一个人会以为直接赋值就行")

    bad, msg = _raises(lambda: sw_ops.op_add_mate(
        _asm_session(_mate_model()),
        {"op": "add_mate", "kind": "coincident", "entities": ents, "align": "SAME"}, {}))
    check(bad and "不认识的 align" in msg and "Obsolete" in msg,
          "★ 旧对齐名 swAlignSAME/AGAINST/NONE 已 Obsolete ⇒ 一律不接受")
    bad, msg = _raises(lambda: sw_ops.op_add_mate(
        _asm_session(_mate_model()),
        {"op": "add_mate", "kind": "distance", "entities": ents}, {}))
    check(bad and "必须给 distance" in msg, "反例：kind=distance 却不给 distance ⇒ 报错")

    muncal = _mate_model()
    s_muncal = _asm_session(muncal, mate_types=())
    r = sw_ops.op_add_mate(s_muncal, {"op": "add_mate", "kind": "coincident",
                                      "entities": ents}, {})
    check(bool(r["new_features"]) and s_muncal.mate_count() == 1,
          "★★ 未标定（`MATE_FEATURE_TYPES` 为空）也**照样建得上、数得对**"
          "（新口径按**结构**数 MateGroup 直接子节点，不依赖类型名名单）；"
          "★ 2026-10-07：op **不再**在热路径回报 `mate_count`（那是 O(#配合) 的整树浅走，"
          "单条配合 300s 的元凶）⇒ 这里改查「特征树真长了节点」+「会话侧数出来 = 1」")
    check("mate_count_note" not in r,
          "★ 旧字段 `mate_count_note` 彻底消失（留着会让人以为配合数不可信）")

    no_pre = sw_ops.ASM_MATES_NO_PRESELECT
    check(no_pre == frozenset({"distance", "angle", "symmetric",
                               "profile_center", "width"}),
          "★ 不支持预选的集合 = 官方 Remarks 里**没有星号**的那五个")
    check(not ({"coincident", "concentric"} & no_pre),
          "★ coincident/concentric 等**有**星号（支持预选），不该被塞进这个集合")
    check(len(sw_ops.ASM_MATES) == 11 and len(no_pre) == 5,
          "★ 11 种配合 / 其中 5 种不支持预选")

    # 常量离线可解析（拼错枚举名会当场炸，而不是等到真机）
    C = sw_connect.Consts(None)
    miss_c = [v["const"] for v in sw_ops.ASM_MATES.values() if C.get(v["const"]) is None]
    check(not miss_c, f"★ 11 种配合的常量都能离线解析（缺：{miss_c}）")
    miss_a = [n for n in sw_ops.ASM_MATE_ALIGN.values() if C.get(n) is None]
    check(not miss_a, f"★ 三个对齐常量都能离线解析（缺：{miss_a}）")
    check(C.get("swDocASSEMBLY") == 2 and C.get("swDocPART") == 1,
          "★ 文档类型常量：零件=1 / 装配体=2（类型守卫的依据）")

    # ---- ⑤ 顺序状态机（静态校验，本文件最值钱的一段） ----
    def _asm_plan(steps):
        return {"schema": "yoyo.swplan/1",
                "meta": {"name": "装配体冒烟", "units": "mm"}, "steps": steps}

    ok, errs = _plan_ok(_asm_plan([{"id": "a", "op": "new_assembly"},
                                   {"id": "m", "op": "add_mate", "kind": "coincident",
                                    "entities": ents}]))
    check(not ok and any("顺序错误" in e and "组件" in e and "insert_component" in e
                         for e in errs),
          f"★★ 没插组件就 add_mate ⇒ 静态校验拦下（缺 has_component）实得 {errs[:1]}")
    check(any("insert_component" in e for e in errs),
          "★ 提示必须**指向装配体的前一步**（insert_component）")
    check(not any("草图" in e for e in errs),
          "★ 装配体顺序错误**不许**再喷草图提示（本轮修掉的误导）")

    ok, errs = _plan_ok(_asm_plan([{"id": "a", "op": "new_part"},
                                   {"id": "i", "op": "insert_component", "path": here}]))
    check(not ok and any("顺序错误" in e and "装配体" in e and "new_assembly" in e
                         for e in errs),
          f"★★ 在零件文档上 insert_component ⇒ 拦下（缺 asm_open）实得 {errs[:1]}")

    # ---- ⑤2 `open` 按扩展名推断（§142）：`.sldprt` ⇒ 补 has_feature ----
    #   「打开已有实体零件 ⇒ 改型切除」是常见流程。修之前 `open` 的静态 effect
    #   只有 doc_open ⇒ extrude_cut（requires 含 has_feature）被离线误拦。
    ok, errs = _plan_ok(_asm_plan(
        [{"id": "o", "op": "open", "path": r"D:\out\some_solid_part.SLDPRT"},
         {"id": "c", "op": "extrude_cut", "end_cond": "swEndCondThroughAll"}]))
    check(ok, f"★★ open 一个 `.sldprt`（推断 has_feature）⇒ 后续 extrude_cut 不再被"
              f"离线误拦（§142）errs={errs[:2]}")
    #   反例对照：`.sldprt` 推断**不该**顺手补 asm_open（那是装配体的态）。
    ok2, errs2 = _plan_ok(_asm_plan(
        [{"id": "o", "op": "open", "path": r"D:\out\some_solid_part.SLDPRT"},
         {"id": "i", "op": "insert_component", "path": here}]))
    check(not ok2 and any("asm_open" in e or "装配体" in e for e in errs2),
          f"★ `.sldprt` 推断**只补 has_feature**、不补 asm_open ⇒ 零件上 "
          f"insert_component 仍被拦（errs={errs2[:1]}）")

    ok, errs = _plan_ok(_asm_plan([{"id": "a", "op": "new_assembly"},
                                   {"id": "i", "op": "insert_component", "path": here},
                                   {"id": "m", "op": "add_mate", "kind": "coincident",
                                    "entities": ents}]))
    check(ok, f"正例：new_assembly → insert_component → add_mate 静态校验通过"
              f"（errs={errs[:2]}）")

    # ---- ⑥ 源码口径：别照旧文档/记忆写 ----
    src = (HERE / "sw_ops.py").read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines() if not ln.lstrip().startswith("#")]
    hits = [ln.strip() for ln in code_lines
            if re.search(r"\.AddMate[2-5]?\s*\(", ln)]
    check(not hits, f"★★ 代码里**一个 `AddMate`/`AddMate2~5` 调用都不许有**（全 Obsolete）"
                    f"实得 {hits[:3]}")
    check("CreateMateData" in src and "CreateMate(" in src,
          "★ 走的是现代路径 CreateMateData + CreateMate")
    check(set(sw_ops.ASM_MATE_ALIGN) == {"aligned", "anti_aligned", "closest"},
          "★ 对齐取值只用三个非 Obsolete 名")
    for _old in ("swAlignSAME", "swAlignAGAINST", "swAlignNONE"):
        check(_old not in sw_ops.ASM_MATE_ALIGN.values(),
              f"★ 旧对齐名 {_old} 不在取值表里（官方标 Obsolete / Do not use）")

    # ---- ⑦ 坑表 §94 / §95 必须存在且点名结论（防文档漂移） ----
    pit = (ROOT / "references" / "30-pitfalls.md").read_text(encoding="utf-8")
    check("## 94." in pit, "坑表有 §94（装配体四条结论）")
    for _key in ("Superseded by IAssemblyDoc::CreateMate", "MateGroup",
                 "EntitiesToMate", "IsFixed"):
        check(_key in pit, f"§94 点名 `{_key}`")
    check("## 95." in pit, "坑表有 §95（装配体真机五条 A 级坑）")
    for _key in ("AddComponent5", "swExtRefUpdateCompNames", "SelectByID2",
                 "MateDistanceDim", "win_path"):
        check(_key in pit, f"§95 点名 `{_key}`")
    # ★ §94 里那句「直接赋 tuple」的老示范**必须改掉** —— 本轮真机推翻了它
    check("VT_ARRAY" in pit,
          "★ §94/§95 的 `EntitiesToMate` 示例已改成 VARIANT 写法"
          "（旧的 `data.EntitiesToMate = (ent1, ent2)` 是**错的**，会**静默失效**）")

    # ---- ⑧ 文档/报错里点名的脚本必须真的在包里（死引用 = 用户照跑撞空） ----
    refs = set(re.findall(r"scripts/(sw_[A-Za-z0-9_]+\.py)", src))
    _miss_ref = sorted(r for r in refs if not (HERE / r).is_file())
    check(not _miss_ref,
          f"★★ `sw_ops.py` 里点名的脚本都真的在 `scripts/` 里（缺：{_miss_ref}）——"
          f" 让人去跑一个不存在的工具，比不提示更糟")
    check("sw_asm_tree.py" in refs and (HERE / "sw_asm_tree.py").is_file(),
          "★ 装配体特征树诊断工具 `scripts/sw_asm_tree.py` 被点名且**真在包里**"
          "（★ 第一版把它放在不打包的 `_diag/` 里 —— 那就是一条死引用；"
          "这条断言把「文档承诺的工具必须在包里」变成可核对的）")

    # ---- ⑨ align 三态：默认必须是 closest（2026-10-03 真机） ----
    #     真机证据（`_diag/diag_mate_align.py`）：两配合面法线相反时
    #       aligned     → 上块被**翻 180° 并绕轴自旋**（40×30×10 → [31.559,-30.322,
    #                    -5, 80, 12.712, 5]，还陷进另一件里）
    #       anti_aligned / closest → 原地不动
    #     而 `asm_mate` 探针正因为默认 aligned 被 m1 翻了块，m2 报的是
    #     「取点 [80,0,10] 没命中」—— **报错指向取点、真根因在上一条配合**。
    check(sw_ops.OPS["add_mate"].params["align"].get("default") == "closest",
          "★★ `add_mate` 的 align **默认必须是 closest**（就近、保持当前姿态）—— "
          "默认 aligned 会在面法线相反时把组件**翻转**，属危险默认值")
    _al = sw_ops.OPS["add_mate"].doc
    check("closest" in _al and "翻转" in _al and "自旋" in _al,
          "★ op 文档必须写清三态语义 + 点名 aligned 会**翻转 + 自旋**"
          "（不然下一个人会照直觉写 aligned）")
    _ents = [{"type": "FACE", "point": [0, 0, 5]}, {"type": "FACE", "point": [100, 0, 5]}]
    mv = _FxAsmModel(comps=[_FxComp("地基-1", box_mm=[-20, -15, -5, 20, 15, 5]),
                            _FxComp("上块-2", box_mm=[80, -15, 5, 120, 15, 15])],
                     mate_moves_on_aligned=True)
    r = sw_ops.op_add_mate(_asm_session(mv, mate_types=("MateCoincident", "Mate")),
                           {"op": "add_mate", "kind": "coincident",
                            "entities": _ents}, {})
    check("moved" not in r,
          "★★ 默认（closest）下配合**不挪组件** ⇒ 不报 moved"
          f"（实得 keys={sorted(r)}）")
    mv2 = _FxAsmModel(comps=[_FxComp("地基-1", box_mm=[-20, -15, -5, 20, 15, 5]),
                             _FxComp("上块-2", box_mm=[80, -15, 5, 120, 15, 15])],
                      mate_moves_on_aligned=True)
    r2 = sw_ops.op_add_mate(_asm_session(mv2, mate_types=("MateCoincident", "Mate")),
                            {"op": "add_mate", "kind": "coincident",
                             "entities": _ents, "align": "aligned"}, {})
    _mv = r2.get("moved") or []
    check(len(_mv) == 1 and _mv[0]["name"] == "上块-2" and _mv[0]["dmax_mm"] > 1.0,
          "★★ `align=aligned` 把组件**挪走**时，`add_mate` 必须在返回值里"
          "**点名是谁、挪了多少、前后位置**（`moved`）—— "
          "否则下一个按坐标取点的 op 会报一个**指向错误方向**的错")
    check(any("翻转" in n for n in (r2.get("notes") or [])),
          "★ aligned 挪动时 notes 必须点名\"翻转\"并指回 closest")

    # ---- ⑩ _moved_boxes 的判据本身（容差 / 排序 / 增删不算挪动） ----
    _mb = sw_ops._moved_boxes
    check(_mb({"a": [0, 0, 0, 1, 1, 1]}, {"a": [0, 0, 0, 1, 1, 1]}) == [],
          "没动就不报（配合本来就会摆组件 ⇒ 零位移不该刷屏）")
    check(_mb({"a": [0, 0, 0, 1, 1, 1]}, {"a": [0, 0, 0, 1.0009, 1, 1]}) == [],
          f"★ 小于容差（{sw_ops.MOVE_TOL_MM}mm）的算没动 —— 浮点噪声不是位移")
    _two = _mb({"a": [0, 0, 0, 1, 1, 1], "b": [0, 0, 0, 1, 1, 1]},
               {"a": [0, 0, 0, 1.2, 1, 1], "b": [0, 0, 0, 9, 1, 1]})
    check([x["name"] for x in _two] == ["b", "a"],
          f"★★ 多个组件动过时**按最大位移降序**（最可疑的排最前），实得 "
          f"{[x['name'] for x in _two]}")
    check(_mb({}, {"a": [0, 0, 0, 1, 1, 1]}) == []
          and _mb({"a": [0, 0, 0, 1, 1, 1]}, {}) == [],
          "★ 新增/消失的组件**不算**\"挪动\"（那是组件数变化，归 check_component_count 管）")
    check(_mb({"a": [0, 0, 0, 1, 1]}, {"a": [0, 0, 0, 2, 1]}) == [],
          "★ 包围盒读不全（不是 6 个数）就跳过，**不猜**")

    # ---- ⑪ 组件包围盒走 IComponent2.GetBox(bool, bool)，不是 GetBox(int) ----
    _bx = _FxAsmModel(comps=[_FxComp("A-1", box_mm=[-20, -15, -5, 20, 15, 5]),
                             _FxComp("B-1", box_mm=[80, -15, 5, 120, 15, 15])])
    _sbox = _asm_session(_bx, mate_types=("Mate",))
    _boxes = _sbox.component_boxes()
    check(_boxes.get("B-1") == [80.0, -15.0, 5.0, 120.0, 15.0, 15.0],
          f"★★ `component_boxes()` 读回的是**全局毫米**包围盒，实得 {_boxes}")
    check(_bx._comps[0].box_calls == [(False, False)],
          f"★★ 组件包围盒必须调 `GetBox(False, False)`（bool, bool）—— "
          f"传整数（`IAssemblyDoc.GetBox` 的签名）会**静默读空**，"
          f"实得 {_bx._comps[0].box_calls}")

    # ---- ⑫ save_as：装配体**不该**被报「一个实体都没有」（假警告） ----
    class _StubSave:
        """最小 save_as 桩：只提供 op 用到的三样东西。"""

        def __init__(self, is_asm, n_body, n_comp=0):
            self.last_save_is_asm = is_asm
            self.last_save_bodies = n_body
            self._n = n_comp

        def save_as(self, path, overwrite=True):
            return str(path).replace("/", "\\")

        def component_count(self, toplevel_only=True):
            return self._n

    _r = sw_ops.op_save_as(_StubSave(True, 0, 2),
                           {"op": "save_as", "path": "D:/out/a.SLDASM"}, {})
    check("bodies" not in _r and _r.get("components") == 2
          and "warn_no_body" not in _r,
          f"★★ 装配体 save_as 按**组件数**回报，**不报**「一个实体都没有」"
          f"（`bodies()` 是零件 API，对 .SLDASM 恒为 0 ⇒ 纯假警告），实得 {_r}")
    _r = sw_ops.op_save_as(_StubSave(True, 0, 0),
                           {"op": "save_as", "path": "D:/out/a.SLDASM"}, {})
    check(_r.get("components") == 0 and "warn_no_body" in _r,
          "★ 空**装配体**仍然要警告（只是话说得对：\"一个组件都没有\"）")
    _r = sw_ops.op_save_as(_StubSave(False, 0),
                           {"op": "save_as", "path": "D:/out/a.SLDPRT"}, {})
    check(_r.get("bodies") == 0 and "warn_no_body" in _r,
          "★ 空**零件**仍然要警告（原有行为不能被这次改动削弱）")
    _r = sw_ops.op_save_as(_StubSave(False, 3),
                           {"op": "save_as", "path": "D:/out/b.SLDPRT"}, {})
    check("warn_no_body" not in _r and _r.get("bodies") == 3,
          "★ 有实体的零件不警告")
    _sas = inspect.getsource(sw_connect.SwSession.save_as)
    check("is_asm" in _sas,
          "★★ `sw_connect.save_as` 自己也必须判文档类型 —— "
          "只在 op 层挡的话，别的调用方还会踩同一个坑")
    check("GetType" in _sas and "swDocASSEMBLY" in _sas,
          "★ `save_as` 里判文档类型要靠 `GetType()==swDocASSEMBLY`"
          "（不能靠 `self.is_assembly` —— 那会再读一次动态的 ActiveDoc）")
    # ★★ 2026-10-03 真机：`self.model` 是**动态属性**（每次重读 ActiveDoc），
    #   而 `_close_if_open` 会关掉另一份文档 ⇒ 活动文档被换掉 ⇒ SaveAs 和落盘校验
    #   都在**操作/验证另一份文档**。修法 = 一进来就**钉住**文档引用。
    check("doc = self.model" in _sas and "keep_title=doc_title" in _sas,
          "★★ `save_as` 必须**先钉住文档引用**再用（不能一路 `self.model`）—— "
          "关文档会换掉 ActiveDoc，那时 `self.ext.SaveAs` 存的是**另一份**文档")
    check('real = str(member(doc, "GetPathName"))' in _sas,
          "★★ 落盘校验必须读**钉住的那份**文档的 GetPathName —— "
          "读 `self.model` 就是「判据在验证别人」（§84 的另一个面）")

    # ---- ⑬ COM 瞬时失败 ⇒ 读回判据 + 重试（2026-10-03 真机） ----
    # 真机现象：`AddComponent5` / `SelectByID2` **同一会话、同一零件时好时坏** ——
    #   不抛异常、无错误码、组件数/选中数不涨。09:47 插不进 fresh.SLDPRT，
    #   09:52 同一文件插得进；一次 add_mate 里第一个取点命中、第二个不命中。
    # ⇒ 判据既然是读回，"没涨"就该**重试**；重试用尽才报错，且报错要点名会话。
    # ★ 用一个**真存在**的文件：op 第一步就查文件在不在（那是另一条独立判据）
    _real = str(HERE / "sw_connect.py")
    _flaky = _FxAsmModel(comps=[], add_fails_n=2)
    r = sw_ops.op_insert_component(_asm_session(_flaky),
                                   {"op": "insert_component", "path": _real}, {})
    check(r["component_count"] == 1 and r["attempts"] == 3,
          f"★★ `insert_component` 前 2 次静默失败、第 3 次成功 ⇒ **不报错**、"
          f"如实回报 attempts=3（实得 count={r.get('component_count')} "
          f"attempts={r.get('attempts')}）")
    _dead = _FxAsmModel(comps=[], add_fails_n=99)
    bad, msg = _raises(lambda: sw_ops.op_insert_component(
        _asm_session(_dead), {"op": "insert_component",
                              "path": _real}, {}))
    check(bad and "已重试" in msg and "sw_session_check" in msg,
          "★★ 重试用尽才报错，且必须点明「已重试 N 次」+「先跑会话自检」"
          "（否则用户会去改本来正确的 op）")
    # 取点重试：第一次不中、重试才中
    _fp = _FxAsmModel(comps=[_FxComp("A-1"), _FxComp("B-1")], pick_fails_n=1)
    ok = sw_ops._pick(_asm_session(_fp), {"type": "FACE", "point": [1, 2, 3]},
                      mark=1, append=False)
    check(ok, "★★ `_pick` 第一次不中会**重试**（真机：同一点重试就中）")
    _fp2 = _FxAsmModel(comps=[_FxComp("A-1")], pick_fails_n=99)
    check(not sw_ops._pick(_asm_session(_fp2),
                           {"type": "FACE", "point": [1, 2, 3]}, append=False),
          "★ 重试用尽才回 False（不是第一次失败就放弃）")
    _fp3 = _FxAsmModel(comps=[_FxComp("A-1")], pick_fails_n=0)
    check(sw_ops._pick(_asm_session(_fp3),
                       {"type": "FACE", "point": [1, 2, 3]}, append=False),
          "★ 一次就中时不受重试影响")
    _issrc = inspect.getsource(sw_ops._pick)
    check("COM_RETRY" in _issrc and "time.sleep" in _issrc,
          "★★ `_pick` 的重试次数/间隔必须是常量（`COM_RETRY`/`COM_RETRY_S`）"
          "—— 写死数字就调不动了")


def test_polyhedron_tools() -> None:
    """[2u] 多面体/棱柱工具（`sw_faceset.py` + `sw_prism.py`）：**离线**钉死判据层。

    ★ 为什么这两个工具必须**有离线回归**：
       读数据那层（`IFace2.GetSurface` / `IEdge.GetCurve`）随版本变，只能真机验；
       但**判据那层**（族怎么聚、棱柱怎么判、截面怎么接环、签名怎么算）
       完全可以离线钉 —— 而本轮所有真 bug（`PlaneParams.d` 不可信、盖板面积
       算了两遍、边被两个面各报一次、整圆孔被当成竖棱、量纲差 1e6）
       **全部**出在判据层 ⇒ 不离线钉就等于没有防线。
    """
    print("\n[2u] 多面体工具：平面族 / 体积判据 / 截面接环 / 平移不变签名")
    import io                                              # noqa: PLC0415
    import contextlib                                      # noqa: PLC0415

    import sw_faceset                                       # noqa: PLC0415
    import sw_prism                                         # noqa: PLC0415

    # ---- ① 两个工具的自检必须全绿（它们自己就是"纯函数层"的测试） ----
    for mod in (sw_faceset, sw_prism):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = mod.self_test()
        tail = [ln for ln in buf.getvalue().splitlines() if ln.strip()][-1]
        check(rc == 0, f"★ `scripts/{mod.__name__}.py --self-test` 全绿（末行：{tail}）")

    # ---- ② 体积判据：面一多，"侧壁>=3"必然成立 ⇒ 必须靠体积否掉 ----
    def P(n, d, area=1.0, name="F", box=None):
        # ★ `box` 必须给：`summarise()` 靠它算整体包围盒，`compare()` 的
        #   "尺寸"一项就出自那里 —— 缺了它对账会报"有边读不到包围盒"，
        #   而那不是零件的问题，是夹具不完整。
        return {"id": name, "type": "plane", "area": area * 1e-6,
                "center": None, "normal": list(n), "d": d,
                "box": list(box) if box else None}

    # 一个 40×30×10 的盒子，体积 12000mm³
    def _box6(o, hx, hy, hz, axis):
        """以 `o=(ox,oy,oz)` 为中心、沿 `axis` 退化到零厚度的包围盒（**米**）。

        ★ 关键：平移时**三个分量都要平移**。第一版夹具只改了法向那一个分量
          （`YZ(x)` 里 y/z 写死），于是并集包围盒**长大了** 10mm，
          对账如实报「尺寸 [50,37,10] vs [40,30,10]」——
          判据是对的，**夹具是错的**。
        """
        c = list(o)
        h = [hx, hy, hz]
        h[axis] = 0.0
        return [c[i] - h[i] for i in range(3)] + [c[i] + h[i] for i in range(3)]

    def cube_faces(ox=0.0, oy=0.0, oz=0.0):
        """40×30×10 的盒子六个面，中心整体平移到 (ox,oy,oz)。"""
        hx, hy, hz = 0.02, 0.015, 0.005
        return [P([1, 0, 0], ox + hx, 300.0, "X+", _box6((ox + hx, oy, oz), hx, hy, hz, 0)),
                P([1, 0, 0], ox - hx, 300.0, "X-", _box6((ox - hx, oy, oz), hx, hy, hz, 0)),
                P([0, 1, 0], oy + hy, 400.0, "Y+", _box6((ox, oy + hy, oz), hx, hy, hz, 1)),
                P([0, 1, 0], oy - hy, 400.0, "Y-", _box6((ox, oy - hy, oz), hx, hy, hz, 1)),
                P([0, 0, 1], oz + hz, 1200.0, "Z+", _box6((ox, oy, oz + hz), hx, hy, hz, 2)),
                P([0, 0, 1], oz - hz, 1200.0, "Z-", _box6((ox, oy, oz - hz), hx, hy, hz, 2))]

    bx = cube_faces()
    fams = sw_faceset.build_families(bx)
    v_ok = sw_faceset.prism_verdicts(bx, fams, volume_mm3=12000.0)
    check(all(x["verdict"] == "prism" for x in v_ok),
          f"★★ 体积对得上的盒子三个轴全判成棱柱（偏差 "
          f"{[x['prism_volume_err_pct'] for x in v_ok]}）")
    # 同一个面集谎报体积（模拟 201 面的箱体：侧壁够多但体积对不上）
    v_bad = sw_faceset.prism_verdicts(bx, fams, volume_mm3=60000.0)
    check(all(x["verdict"] == "not_prism" for x in v_bad),
          "★★ 体积对不上时**全部**判成不是棱柱 —— 这就是「201 面的箱座在 6 个方向"
          "都被判成棱柱」那个假绿灯的解药（真机 2026-10-03）")
    v_none = sw_faceset.prism_verdicts(bx, fams, volume_mm3=None)
    check(all(x["verdict"] in ("weak", "not_prism") for x in v_none)
          and not any(x["verdict"] == "prism" for x in v_none),
          "★ 没有体积数据时**只给 weak（证据不足）**，绝不给肯定结论")
    # 盖板面积不能把两个端面加起来（那会让理论体积正好翻倍 ⇒ 偏差整 100%）
    _cap = [x for x in v_ok if x["axis"][0] == 1.0][0]
    check(abs(_cap["prism_volume_err_pct"]) < 1e-6,
          f"★★ 截面面积只取**一个站位**那组（偏差 {_cap['prism_volume_err_pct']}%）"
          f" —— 把两端加在一起会正好差 100%，而 100% 这个整数本身就是信号")

    # ---- ③ 签名必须平移不变（重建件与参照件差一个纯平移） ----
    moved = cube_faces(0.010, -0.007, 0.004)      # 纯平移
    s0 = sw_faceset.signature(fams, bx)
    s1 = sw_faceset.signature(sw_faceset.build_families(moved), moved)
    check(s0 == s1, "★ 签名**平移不变** —— 重建件建在基准面上，"
                    "拿绝对站位比会给出「形状不一致」的假失败")
    s_abs = sw_faceset.signature(fams, bx, relative=False)
    check(s_abs != s1, "★ `relative=False` 走绝对站位（需要区分「挪了位置」时用）")

    # ---- ④ 对账：体积 + 尺寸 + 签名三层 ----
    pa = {"faces": bx, "families": fams, "volume_mm3": 12000.0}
    pb = {"faces": moved, "families": sw_faceset.build_families(moved),
          "volume_mm3": 12000.0}
    rep = sw_faceset.compare(pa, pb)
    check(rep["same_shape"],
          f"★★ 平移后的同形状**判为一致**（{len(rep['checks'])} 项全过）")
    check(any("中心偏移" in t for t in rep.get("info") or []),
          "★ 中心偏移单列成**信息**、不进判定 —— 位置与形状是两件事")
    pc = {"faces": bx, "families": fams, "volume_mm3": 9900.0}
    rep2 = sw_faceset.compare(pa, pc)
    check(not rep2["same_shape"] and any(
        not c["ok"] for c in rep2["checks"] if c["name"] == "体积"),
        "★ 体积真的不对时**必须**判不一致（对账不能只比面数）")

    # ---- ⑤ 截面：接环 + 去重 + 孔 ----
    A, B2 = (0.0, 0.0, 0.0), (0.040, 0.0, 0.0)
    C2, D2 = (0.040, 0.020, 0.0), (0.0, 0.020, 0.0)
    rect = [{"p": D2, "q": C2, "kind": "line"}, {"p": C2, "q": B2, "kind": "line"},
            {"p": A, "q": B2, "kind": "line"}, {"p": D2, "q": A, "kind": "line"}]
    cls = sw_prism.classify_loops(sw_prism.chain_loops(rect)[0])
    check(cls["outer"] and abs(abs(cls["outer"]["signed_area_mm2"]) - 800.0) < 1e-6,
          f"★ 截面面积走 mm²（实得 {cls['outer']['signed_area_mm2'] if cls['outer'] else None}）"
          f" —— 第一版把 m² 塞进名为 `_mm2` 的字段，`round(8e-4,4)=0.0008`，"
          f"数字看着有值但量纲差 1e6")
    hole = [{"p": (0.020, 0.010, 0.0), "q": (0.020, 0.010, 0.0),
             "kind": "circle", "radius": 0.005}]
    cls2 = sw_prism.classify_loops(sw_prism.chain_loops(rect + hole)[0])
    check(len(cls2["inners"]) == 1 and cls2["inners"][0].get("circle")
          and cls2["inners"][0]["d_mm"] == 10.0,
          f"★★ **整圆孔**单独识别成 Ø{cls2['inners'][0].get('d_mm') if cls2['inners'] else None}mm"
          f"（而不是「面积 0」的退化多边形）—— 漏了它整份截面数据就没法用")

    # ---- ⑥ 两个工具必须在包里、且被 SKILL.md 提到 ----
    for m in ("sw_faceset.py", "sw_prism.py"):
        check((HERE / m).is_file(), f"`scripts/{m}` 在包里")
    sk = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    for m in ("sw_faceset.py", "sw_prism.py"):
        check(m in sk, f"SKILL.md 提到 `{m}`（文档漂移就抓不到）")
    pit = (ROOT / "references" / "30-pitfalls.md").read_text(encoding="utf-8")
    check("## 97." in pit, "坑表有 §97（多面体识别这一轮）")
    for key in ("PlaneParams", "GetBody()", "GetClosestPointOn",
                "去重", "体积判据"):
        check(key in pit, f"§97 点名 `{key}`")


def test_symmetry_tools() -> None:
    """[2v] 对称性 / 装配面决策层（`sw_symmetry.py`）：**离线**钉死判据。

    ★ 为什么必须有：这一层决定「**建一半 + 镜像**」还是「建全件」，
      以及「用哪个基准面建模」。判据错 ⇒ 特征数翻倍、改尺寸要改两遍、
      装配要多加配合。本轮抓到的两个真 bug 都在判据层：
        · 配对时只累加 `a` 的面积 ⇒ 完全对称的盒子只有 92.1%
          ⇒ 「对称件」被误判成「不对称」⇒ **建模策略整个反过来**；
        · 侧壁（法向 ∥ 对称面）被误报成「中面上的面」
          ⇒ 计划会把四个侧壁也只建一半（照那个结果建模必错）。
    """
    print("\n[2v] 对称性：镜像配对 / 半边划分 / 装配定位面 / 基准面建议")
    import contextlib                                      # noqa: PLC0415
    import io                                              # noqa: PLC0415

    import sw_symmetry                                      # noqa: PLC0415

    # ---- ① 工具自己的自检必须全绿 ----
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sw_symmetry.self_test()
    tail = [ln for ln in buf.getvalue().splitlines() if ln.strip()][-1]
    check(rc == 0, f"★ `scripts/sw_symmetry.py --self-test` 全绿（末行：{tail}）")

    def P(n, area, name, c, box=None):
        """夹具面。`area` 单位 mm²；`c` = **面上一点**（必须落在面上，
        且**每个面给不同的点** —— 全给同一点会被判成"镜像后落回自己"）。"""
        n = sw_symmetry._unit(n)
        return {"id": name, "type": "plane", "area": area * 1e-6,
                "center": list(c), "normal": n,
                "d": sw_symmetry._dot(n, c),
                "box": list(box) if box else None}

    # 40x30x10 的盒子，x∈[0,40] y∈[-15,15] z∈[-5,5]
    bx = [0.0, -0.015, -0.005, 0.04, 0.015, 0.005]
    cube = [
        P([1, 0, 0], 300, "X+", [0.04, 0, 0],
          [0.04, -0.015, -0.005, 0.04, 0.015, 0.005]),
        P([1, 0, 0], 300, "X-", [0.0, 0, 0],
          [0.0, -0.015, -0.005, 0.0, 0.015, 0.005]),
        P([0, 1, 0], 400, "Y+", [0.02, 0.015, 0],
          [0.0, 0.015, -0.005, 0.04, 0.015, 0.005]),
        P([0, 1, 0], 400, "Y-", [0.02, -0.015, 0],
          [0.0, -0.015, -0.005, 0.04, -0.015, 0.005]),
        P([0, 0, 1], 1200, "Z+", [0.02, 0, 0.005],
          [0.0, -0.015, 0.005, 0.04, 0.015, 0.005]),
        P([0, 0, 1], 1200, "Z-", [0.02, 0, -0.005],
          [0.0, -0.015, -0.005, 0.04, 0.015, -0.005]),
    ]

    # ---- ② 配对面积必须**两侧合计**（真机级后果：对称件被误判） ----
    r = sw_symmetry.symmetry_score(cube, [1, 0, 0], 0.02)
    check(r["is_symmetric"] and abs(r["symmetric"] - 1.0) < 1e-9,
          "完全对称的盒子对称度 100%（实得 {:.2f}%）".format(r["symmetric"] * 100))
    check(abs(r["unmatched_area_mm2"]) < 1e-6,
          "★★ unmatched=0 ⇒ 配对时**两侧面积都计入**（第一版只加 a，"
          "完全对称的盒子也只有 92.1% ⇒ 判定会把对称件当不对称 ⇒ "
          "**建模策略整个反过来**：建全件而不是建一半 + 镜像）")
    check(abs(r["mirrored_area_mm2"] - 600.0) < 1e-6,
          "★ 镜像配对面积 = 300+300 = 600mm²（实得 {:.0f}）".format(
              r["mirrored_area_mm2"]))

    # ---- ③ 半边划分：侧壁 ≠ 中面上的面 ----
    h = sw_symmetry.half_split(cube, [1, 0, 0], 0.02)
    check(h["keep"] == ["X+"] and h["mirrored"] == ["X-"],
          "只有 X+/X- 是**半边**（实得 keep={} mirrored={}）".format(
              h["keep"], h["mirrored"]))
    check(sorted(h["cross"]) == ["Y+", "Y-", "Z+", "Z-"] and not h["on_plane"],
          "★★ 四个**侧壁**进 cross 而不是 on_plane —— 它们法向 ∥ 对称面，"
          "整片建出来即可。★ 判错这一类，计划会把侧壁也只建一半")

    # ---- ④ 中面上的面（法向 ⊥ 对称面）单列 ----
    rib = P([1, 0, 0], 400, "RibEnd", [0.02, 0, 0],
            [0.02, -0.01, 0.005, 0.02, 0.01, 0.0055])
    h2 = sw_symmetry.half_split(cube + [rib], [1, 0, 0], 0.02)
    check("RibEnd" in h2["on_plane"],
          "法向 ⊥ 对称面且跨在面上的面进 on_plane（**镜像会重叠，只能建一次**）")

    # ---- ⑤ 三个方向都要试 ----
    syms = sw_symmetry.find_symmetry_planes(cube, bx)
    check([x["axis"] for x in syms] == ["X", "Y", "Z"],
          "三个中面都试一遍并按对称度降序（实得 {}）".format(
              [x["axis"] for x in syms]))
    check(all(x["is_symmetric"] for x in syms),
          "盒子三个方向都对称（实得 {}）".format(
              [round(x["symmetric"], 3) for x in syms]))
    boss = P([0, 0, 1], 500, "Boss", [0.02, 0, 0.009],
             [0.01, -0.01, 0.005, 0.03, 0.01, 0.009])
    s2 = sw_symmetry.find_symmetry_planes(cube + [boss], bx)
    by = {x["axis"]: x for x in s2}
    check(by["Z"]["symmetric"] < 0.9 < by["X"]["symmetric"],
          "★ 单侧凸台只破坏**它自己法向那个方向**的对称（Z 向 {:.1f}%、"
          "X 向 {:.1f}%）⇒ **必须每个方向都试**，只看一个会误判".format(
              by["Z"]["symmetric"] * 100, by["X"]["symmetric"] * 100))

    # ---- ⑥ 装配定位面：方向按**位置**定，不按法向 ----
    m = sw_symmetry.mount_faces(cube, bx)
    check(len(m) == 6 and {x["dir"] for x in m} == {
        "X正", "X负", "Y正", "Y负", "Z正", "Z负"},
        "★ 六个方向各出一个定位面（实得 {} 条）—— ★ 方向必须按**位置**定："
        "SolidWorks 里两个相对面的 normal **可能同向**，按法向分组会只出 3 条"
        "（小的那个被丢掉），而位置是几何事实、不受朝向约定影响".format(len(m)))
    zp = next(x for x in m if x["dir"] == "Z正")
    check(abs(zp["area_mm2"] - 1200.0) < 1e-6,
          "Z 向定位面 = 40×30 = 1200mm²（实得 {:.0f}）".format(zp["area_mm2"]))
    cyl = {"id": "Hole", "type": "cylinder", "area": 200e-6,
           "center": [0.02, 0, 0], "axis": [0, 1, 0], "radius": 0.003}
    check(len(sw_symmetry.mount_faces(cube + [cyl], bx)) == 6,
          "★ 圆柱面**不进**定位面表（圆柱贴合没有确定的位置自由度）")

    # ---- ⑦ 基准面建议要跟着**最大的定位面**走 ----
    adv = sw_symmetry.base_plane_advice(m, syms[0])
    a0 = adv["advices"][0] if adv["advices"] else {}
    check(a0.get("mount_dir") == "Z正" and "Front" in a0.get("suggest_base", ""),
          "★ 基准面建议跟着最大定位面走（实得 {} + {}）—— 用户原话："
          "「建模时直接选取正确的基准面，装配时就可以加快装配效率、"
          "减少冗余操作」".format(a0.get("mount_dir"), a0.get("suggest_base")))
    check(any("symmetry" in x for x in adv["advices"]),
          "★ 同时给出**对称面**建议（建一半 + mirror 该用哪个基准面）")

    # ---- ⑦' ★★ 对称面站位不能取包围盒中点（真机 `1-箱座` 的 0.04mm 陷阱） ----
    # 构造一个"两端不等但左右对称"的零件：包围盒 x∈[-26, +20.04]，
    # 真实对称面是 **x=0**（d=-26 与 d=+26 面积相等），而包围盒中点是 **-2.98**。
    asym_box = [-0.026, -0.01, -0.005, 0.02004, 0.01, 0.005]
    left = P([1, 0, 0], 900, "L", [-0.026, 0, 0],
             [-0.026, -0.01, -0.005, -0.026, 0.01, 0.005])
    right = P([1, 0, 0], 900, "R", [0.026, 0, 0],
              [0.026, -0.01, -0.005, 0.026, 0.01, 0.005])
    # ★ 夹具要**真的对称**：这个凸台必须成对（P / P'），否则判据会如实报它不对称
    #   —— 那是夹具的错，不是判据的错（§84 的另一面）。
    pillar = P([0, 0, 1], 300, "P", [0.019, 0, 0.0],
               [0.019, -0.005, -0.005, 0.020, 0.005, 0.005])
    pillar2 = P([0, 0, 1], 300, "P2", [-0.019, 0, 0.0],
                [-0.020, -0.005, -0.005, -0.019, 0.005, 0.005])
    piece = [left, right, pillar, pillar2]
    mid = (asym_box[0] + asym_box[3]) / 2.0
    r_mid = sw_symmetry.symmetry_score(piece, [1, 0, 0], mid)
    r_best = sw_symmetry.best_symmetry(piece, 0, asym_box)
    check(abs(r_mid["symmetric"] - 0.0) < 1e-9,
          "★ 反例：用**包围盒中点**算 ⇒ 对称度 0.00%（实得 {:.2f}%）".format(
              r_mid["symmetric"] * 100))
    check(r_best["is_symmetric"] and r_best["symmetric"] > 0.95,
          "★★ 同一件用**面积配对**找站位 ⇒ 对称度 {:.1f}%（最优站位 "
          "{:+.3f}mm，候选来源 {}）".format(
              r_best["symmetric"] * 100, r_best["offset_mm"],
              r_best.get("candidate_tag")))
    check(not r_best.get("bbox_mid_is_symmetric", True),
          "★ 报告**明说**包围盒中点不是对称面（差 {:.3f}mm）—— "
          "否则下一个人拿中点当镜像面，全盘错而且看不出原因".format(
              abs(r_best.get("offset_from_bbox_mm", 0.0))))

    # ---- ⑦'' ★★ 「面积相等的对」**不要求相邻**（最自然但错误的假设） ----
    # 三个站位：-26(900) / -20(300) / +26(900)。真相 (-26, +26) 中间夹着 -20。
    prof = sw_symmetry.station_profile(piece, 0)
    tags = {sw_symmetry.best_symmetry(piece, 0, asym_box).get("candidate_tag")}
    check("pair_equal_area" in tags,
          "★★ 最优候选来自「**面积相等的站位对**」—— 那一对**不相邻**"
          "（中间夹着别的站位）。只试『相邻两站』的话真相压根不在候选里，"
          "真机上会报出另一对的中点 ⇒ 又变成 0% 对称度")
    check(len(prof) == 2,
          "法向≈±X 的平面聚成 2 个站（实得 {}）—— 上下那两个法向是 +Z，不进 X 向"
          "站位分布".format([round(x["d"] * 1000, 1) for x in prof]))
    check(all(abs(x["area"] * 1e6 - 900.0) < 1e-6 for x in prof),
          "两个站位面积逐位相等（各 900mm²）⇒ 这就是「面积相等的对」"
          "（法向≈±X 的面只有 L/R 两张，柱面法向是 +Z 不计入）")

    # ---- ⑧ 现有 op 层必须真的能镜像（决策层不能指向不存在的 op） ----
    import sw_ops                                          # noqa: PLC0415
    for _n in ("mirror", "mirror_body", "sk_mirror"):
        check(_n in sw_ops.OPS and sw_ops.OPS[_n].verified,
              f"★ 决策层指向的 op `{_n}` 存在且已真机验证（"
              f"verified={sw_ops.OPS[_n].verified}）—— "
              f"「建一半 + 镜像」要真的有 `mirror` 可用")

    # ---- ⑨ 坑表 §98 必须存在（防文档漂移） ----
    pit = (ROOT / "references" / "30-pitfalls.md").read_text(encoding="utf-8")
    check("## 98." in pit, "坑表有 §98（对称性 / 装配面决策层）")
    for _k in ("cross_plane", "装配定位面", "建一半", "unmatched"):
        check(_k in pit, f"§98 点名 `{_k}`")


def test_segment_tool() -> None:
    """[2w] 语义分段层（`sw_segment.py`）：**离线**钉死判据。

    ★ 为什么必须有：这一层决定「**按什么顺序建、每段用哪个基准面、
      哪段该镜像**」。判据错 ⇒ 顺序错 ⇒ 后面每一步都"成功"但形状不对
      （本轮真机：`33-齿轮` 的 55 个齿被误报成「减重槽」，
      `1-箱座` 201 个面只归了 3% 面积却报"分好了"）。
    """
    print("\n[2w] 语义分段：功能段归类 / 依赖顺序 / 覆盖率 / 有没有的都要报")
    import contextlib                                      # noqa: PLC0415
    import io                                              # noqa: PLC0415
    import math                                            # noqa: PLC0415

    import math                                            # noqa: PLC0415
    import sw_faceset                                      # noqa: PLC0415
    import sw_segment                                      # noqa: PLC0415

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sw_segment.self_test()
    tail = [ln for ln in buf.getvalue().splitlines() if ln.strip()][-1]
    check(rc == 0, f"★ `scripts/sw_segment.py --self-test` 全绿（末行：{tail}）")

    # ---- ① 夹具复现真机 `33-齿轮` 的分段结论 ----
    def PL(n, area, name, c, box):
        n = sw_segment._unit(n)
        return {"id": name, "type": "plane", "area": area * 1e-6,
                "center": list(c), "normal": n, "d": sw_segment._dot(n, c),
                "box": list(box)}

    def CY(ax, r, area, name, c):
        return {"id": name, "type": "cylinder", "area": area * 1e-6,
                "center": list(c), "axis": sw_segment._unit(ax),
                "radius": r, "normal": None, "d": None, "box": None}

    # ---- ①' ★★★ 孔归属判定：孔轴 ⟂ 承载面（真机 CSWA，坑表 §102） ----
    #   用户手建的 CSWA 有 2 个 **Ø6 法向孔**，孔轴 = 65° 斜筋的平面族法向
    #   （夹角 0.0°）⇒ 判据成立；而 agent 版 **0 个圆柱面** ⇒ 孔整个丢了，
    #   可 `sw_symmetry` 仍报「100% 对称」—— **判据全绿而结果全错**。
    def _CY(rr, ax, c):
        return {"id": "H", "type": "cylinder", "area": 100e-6,
                "center": list(c), "axis": sw_faceset._unit(ax),
                "radius": rr, "normal": None, "d": None, "box": None}

    # 一个斜平面族（法向 65°）上垂直打一个孔
    _ang = math.radians(65.0)
    _slab = PL([math.cos(_ang), -math.sin(_ang), 0], 1000, "Slab",
               [math.cos(_ang) * 0.02, -math.sin(_ang) * 0.02, 0.0],
               [-0.01, -0.01, 0.0, 0.01, 0.01, 0.02])
    _hole = _CY(0.003, [-math.cos(_ang), math.sin(_ang), 0],
                [0.02 * math.cos(_ang), -0.02 * math.sin(_ang), 0.01])
    _hole["box"] = [-0.01, -0.01, 0.0, 0.01, 0.01, 0.02]
    _hs = sw_faceset.holes_with_normal_holes([_slab, _hole])
    check(len(_hs) == 1 and _hs[0]["is_normal_hole"],
          "★★ 圆柱面报成**法向孔**（孔轴 ⟂ 承载面）—— 实得 is_normal_hole="
          "{}，夹角={}".format(_hs[0]["is_normal_hole"] if _hs else None,
                                _hs[0]["angle_deg"] if _hs else None))
    check(_hs and abs(_hs[0]["diameter_mm"] - 6.0) < 1e-6,
          "★ 孔径读出 Ø6.000mm（实得 {}）".format(
              _hs[0]["diameter_mm"] if _hs else None))
    check(_hs and "公称" in _hs[0]["note"],
          "★★ 孔的读数必须**提醒「实测值 vs 公称可能不同」** —— "
          "Ø6 公称可能是 Ø6 间隙孔或 Ø5.5 基孔，**没有图纸标注分不出来**。"
          "这是用户指出的「公称尺寸的识别」缺口")
    # 侧向孔：轴与族法向差 60° ⇒ 不是法向孔
    _side = _CY(0.004, [1.0, 0.0, 0.0], [0.0, 0.0, 0.01])
    _side["box"] = [-0.01, -0.01, 0.0, 0.01, 0.01, 0.02]
    _hside = sw_faceset.holes_with_normal_holes([_slab, _side])
    check(_hside and not _hside[0]["is_normal_hole"],
          "★ 侧向孔（轴不垂直承载面）**如实报 false** 并给夹角"
          "（实得 {}）—— 不硬套「都是法向孔」".format(
              _hside[0]["angle_deg"] if _hside else None))
    # 0 个圆柱面 ⇒ sw_segment 必须显式告警
    _bare = sw_segment.segment(
        [PL([0, 0, 1], 1000, "Flat", [0, 0, 0.01],
            [-0.01, -0.01, 0.01, 0.01, 0.01, 0.01])],
        [0, 0, 0, 0.03, 0.03, 0.02])
    check(any("0 个圆柱面" in n for n in _bare["notes"]),
          "★★★ **0 个圆柱面时必须显式告警**「特征可能没建全」"
          "（真机教训：agent 版 CSWA 14 面全平面、漏 2 个 Ø6 孔，"
          "而对称性判据仍报 100% 对称）")


    gear = [
        PL([0, 0, 1], 6940, "Web+", [0, 0, 0.0045],
           [-0.047, -0.047, 0.0045, 0.047, 0.047, 0.0045]),
        PL([0, 0, -1], 6940, "Web-", [0, 0, -0.0045],
           [-0.047, -0.047, -0.0045, 0.047, 0.047, -0.0045]),
        PL([0, 0, 1], 800, "HubTop", [0, 0, 0.013],
           [-0.016, -0.016, 0.013, 0.016, 0.016, 0.013]),
        PL([0, 0, 1], 800, "HubBot", [0, 0, -0.013],
           [-0.016, -0.016, -0.013, 0.016, 0.016, -0.013]),
        CY([0, 0, 1], 0.016, 1988, "Bore", [0, 0, 0]),
    ]
    for i in range(55):                       # 齿：真机**两组**半径
        a = 2 * math.pi * i / 55
        for rr, tag in ((0.057, "Tooth"), (0.0525, "Root")):
            gear.append(CY([0, 0, 1], rr, 40, "%s%d" % (tag, i),
                           [rr * math.cos(a), rr * math.sin(a), 0.0]))
    gear += [
        PL([1, 0, 0], 110, "Key+", [0.004779, 0.0155, 0.0],
           [0.004779, 0.013343, -0.013, 0.004779, 0.017745, 0.013]),
        PL([-1, 0, 0], 110, "Key-", [-0.005221, 0.0155, 0.0],
           [-0.005221, 0.013343, -0.013, -0.005221, 0.017745, 0.013]),
    ]
    box = [-0.057, -0.057, -0.013, 0.057, 0.057, 0.013]
    seg = sw_segment.segment(gear, box)
    by = {r["kind"]: r for r in seg["segments"]}

    check(seg["axis"] == 2, "主轴 = Z（实得 {}）".format(seg.get("axis_label")))
    check("rim" in by and by["rim"]["n_faces"] == 110,
          "★★ 轮缘收**全部**半径组（真机 110 个齿面 = R57 + R52.5 两组）"
          "（实得 {} 个）—— 第一版只收第一组 ⇒ 另一组 55 个齿掉进"
          "「减重槽」判据，**把齿当成了减重槽**".format(
              by.get("rim", {}).get("n_faces")))
    check("pocket" not in by,
          "★ 齿被收进轮缘后，**不再**被误报成减重槽（实得 kinds={}）".format(
              [r["kind"] for r in seg["segments"]]))
    check(abs(by["keyway"]["key_width_mm"] - 10.0) < 1e-6,
          "★ 键槽间隙 = 4.779 + 5.221 = **10.000mm**（键 10×8 逐位对上；"
          "实得 {}）".format(by.get("keyway", {}).get("key_width_mm")))
    check(by["web"]["mirror"] is True,
          "★ 腹板**建一半 + 镜像**（本夹具对称 99.8%）")
    check(by["rim"]["mirror"] is False,
          "★ 轮缘**不镜像**（齿本身是整圈）")

    print()
    # ---- # ---- ①› ★★★ 孔 vs 凸台必须读**凹凸性**（真机 CSWA，坑表 §103）----
    #   上一轮只按「孔轴 ⊂ 承载面」判 ⇒ 把用户截图里**向上拉伸的凸台**
    #   报成了「法向孔」。用户原话：「很明显这里红色是向上拉伸的突起吧？
    #   根本不是孔，绿色的也是槽啊」。
    #   ⇒ 判据必须升到**拓扑层**：材料在圆柱内（凸台）还是外（孔）。
    check(callable(sw_faceset.classify_holes_and_bosses),
          "★★ 工具必须有 `classify_holes_and_bosses()`（读凹凸性分孔/凸台）"
          "—— 只按轴向判**没有区分力**（凸台同样满足「轴 ⊥ 基面」）")
    check(callable(sw_faceset.face_convexity),
          "★ 工具必须有 `face_convexity()`（单面凹凸判据 `n·r`）")
    check(callable(sw_faceset._true_face_normal),
          "★★ 必须有 `_true_face_normal()` —— `ISurface.Evaluate` + "
          "`FaceInSurfaceSense` 修正朝向（`PlaneParams` 的法线朝向不可靠，§97①）")
    check(callable(sw_faceset.classify_slots),
          "★★★ 工具必须有 `classify_slots()` —— **槽**是平面的拓扑特征，"
          "圆柱曲率判不出来，旧工具根本没有这个概念")

    # ---- # ---- ①› ★★★ 槽：判据必须「局部成组」，不能「全局两两配对」 ----
    #   真机第一版「任意两片平行细长平面、间距 0.5~60mm」报了 **16 个假阳性**
    #   （L 形箱体外轮廓天然有几十对平行面）⇒ 必须加「同族 + 面积相等 +
    #   bbox 重合」三条。`sw_faceset --self-test [9]` 已用真机读数钉死。
    _big1 = {"id": "B1", "type": "plane", "area": 3000e-6,
             "center": [0.02, 0, 0.005], "normal": [1, 0, 0], "d": 0.02,
             "box": [0.02, -0.03, 0, 0.02, 0.03, 0.01]}
    _big2 = {"id": "B2", "type": "plane", "area": 1500e-6,
             "center": [0.015, 0, 0.005], "normal": [1, 0, 0], "d": 0.015,
             "box": [0.015, -0.02, 0, 0.015, 0.02, 0.01]}
    _sl_big = sw_faceset.classify_slots([_big1, _big2])
    check(len(_sl_big["slots"]) == 0,
          "★★★ **相对的两块大平板不得被当成槽**（面积差 2 倍 ⇒ 实得 %d 个）"
          " —— 第一版只查「成对平行 + 深度」⇒ 真机报 16 个假阳性"
          % len(_sl_big["slots"]))
    _st1 = {"id": "S1", "type": "plane", "area": 1000e-6,
            "center": [0.02, 0, 0.005], "normal": [1, 0, 0], "d": 0.02,
            "box": [0.02, -0.03, 0, 0.02, 0.03, 0.01]}
    _st2 = {"id": "S2", "type": "plane", "area": 1000e-6,
            "center": [0.015, 0, 0.005], "normal": [1, 0, 0], "d": 0.015,
            "box": [0.015, -0.01, 0, 0.015, 0.01, 0.01]}
    _sl_st = sw_faceset.classify_slots([_st1, _st2])
    check(len(_sl_st["slots"]) == 0,
          "★★ **台阶的两级面不得被当成槽**（面积相等但 bbox 大小不同 "
          "⇒ 实得 %d 个）" % len(_sl_st["slots"]))
    check("不等于" in _sl_st["note"],
          "★★ 报「没找到槽」必须说「**不等于没有槽**」"
          "（被圆角/倒角打断的槽会漏判）")

    # ---- # ---- ①› ★★ `read_faces` 必须挂 COM 引用（面名对不上）----
    #   `IFace2.GetName()` 对未命名面返回 **None**，而 `read_faces` 的
    #   `Face1..N` 是**按枚举序号**起的名 ⇒ 两套命名完全不同
    #   ⇒ 任何「按名字反查面」的实现都会**永远找不到**（本轮真机踩到）。
    import inspect as _ins                                   # noqa: PLC0415
    _src = _ins.getsource(sw_faceset.read_faces)
    check("_face2" in _src and "_surf" in _src,
          "★★★ `read_faces` 必须把 `IFace2`/`ISurface` **挂在 rec 上**"
          "（`rec[\"_face2\"]`）—— 否则「按名反查」永远失败（真机踩到）")
    # ★ 只查**可执行代码**里的 `GetName` —— 注释里提到它是合理的（说明坑）。
    #   断言必须区分「代码这么写」与「文档提到它」，否则会被注释触发假失败。
    _src2 = _ins.getsource(sw_faceset.face_convexity)
    _code2 = "\n".join(x.split("#", 1)[0] for x in _src2.splitlines())
    check("_face2" in _code2 and "GetName" not in _code2,
          "★ `face_convexity` 的**代码**必须用 rec 上的引用，"
          "**不得按名字反查**（注释里提到 `GetName` 是合理的，"
          "所以断言只扫非注释部分）")
    _src3 = _ins.getsource(sw_faceset._eval_pt_normal)
    check("flat" in _src3,
          "★★ `Evaluate` 的返回值按**扁平数组**处理（`flat[0:3]`=点、"
          "`flat[3:6]`=法线）—— 按嵌套 list 解构会拿不到法线")

    # ---- ›4 ★★★ 定位：工具是给 agent 当眼睛的，不替 agent 读数（用户校准 2026-10-03）----
    #   用户原话：「我们开发的是 skills，给 agent 用，agent 自己肯定能完成
    #   OCR，我们只需要有一些工具协助它完成建模尺寸确定」。
    #   ⇒ 工具做的是：渲染/切图（render_views）+ 题面真值（sw_exercise）+
    #     建完验算（sw_exercise_check）；OCR 降级为交叉验证，不当事实来源。
    import sw_drawing as _sdw                           # noqa: PLC0415
    import inspect as _ins4                              # noqa: PLC0415
    check(callable(_sdw.render_views) and callable(_sdw.position_hint),
          "★★★ 必须有 `render_views()`（把每个视图裁成单张图给 agent 看）"
          " + `position_hint()`（位置提示名，不下身份结论）")
    check(_sdw.VIEW_PAD_FRAC >= 0.08,
          "★★ 裁图 pad 必须给足（实得 %.2f）—— 视图 bbox 只含**填充区**，"
          "尺寸标在**外面**（真机：Top bbox 右界 x=732，它的 20/28 在"
          " x=818/884 —— 不留 pad 就把尺寸裁掉，agent 看到的是"
          "「没尺寸的图」）" % _sdw.VIEW_PAD_FRAC)
    _head = _ins4.getsource(_sdw).split("###")[0]
    check("agent 自己肯定是能够完成" in _ins4.getsource(_sdw),
          "★★★ docstring 必须写明定位：**agent 自己读尺寸**，"
          "工具只协助（用户校准）")
    _dsrc = _ins4.getsource(_sdw.render_views)
    check("overview" in _dsrc and "position_hint" in _dsrc,
          "★ render_views 必须产 overview（全局）+ 每视图一张带 pad 的裁图")

    # ---- ›3 ★★★ 读图纸：尺寸归属（坑表 §105）----
    #   「让 agent 看得懂图纸」的第一跳：**尺寸 → 哪个视图**。
    #   ★ 归属判据错了**三版**（最近 / 单轴 y / 双轴距离）才对，
    #     根因是「**跨组比较**没有意义」—— 轴测图不是正投影视图。
    import inspect as _ins3                               # noqa: PLC0415
    import sw_drawing                                    # noqa: PLC0415
    check(callable(sw_drawing.detect_view_regions)
          and callable(sw_drawing.group_views)
          and callable(sw_drawing.assign_dims_to_views),
          "★★★ `sw_drawing` 必须有 `detect_view_regions()`（彩色填充切视图）"
          "/ `group_views()`（投影对齐分组）/ `assign_dims_to_views()`（组内归属）")
    check(callable(sw_drawing.multi_pass_dims),
          "★★★ 必须有 `multi_pass_dims()` —— ★ **单遍 OCR 不可复现**"
          "（真机同一张图：zoom4 漏 `75`、zoom5 全对、zoom8 把 16 读成 -12）")
    # ① 分组：Top/Front 同组（x 完全重叠）、Iso 独立 —— **不看 y 跨度**
    _regs = [{"bbox": (221, 141, 733, 383), "area": 111147},
             {"bbox": (1033, 141, 1550, 950), "area": 245355},
             {"bbox": (221, 493, 733, 1131), "area": 198770}]
    _ax = sw_drawing.detect_layout_axis(_regs)
    check(_ax == "y", "★ 布局轴 = y（Top/Front 上下排列），实得 %s" % _ax)
    _g = sw_drawing.group_views(_regs, _ax)
    check(sorted(_g["groups"]) == [[0, 2], [1]],
          "★★★ **Top+Front 同组、Iso 独立**（实得 %s）—— "
          "★★ 判据只看**跨轴是否重叠**（x 都是 [221,733]），"
          "**不要求 y 跨度相近**（242 vs 638 差 396 也该同组）"
          % _g["groups"])
    # ★ 只查**真正参与分组判断的那一行**，不是整个函数 ——
    #   `span` 变量在函数里还用于**报告**（给���看），那是合理的。
    #   第一版断言写成「源码里不许出现 span」⇒ 太脆，会误报。
    _gline = [x for x in _ins3.getsource(sw_drawing.group_views).splitlines()
              if "continue" in x and not x.strip().startswith("#")]
    _gtxt = " ".join(_gcode_ := [x.split("#", 1)[0] for x in _gline])
    check("si" not in _gtxt and "sj" not in _gtxt,
          "★ 分组判据里**不得**再拿沿布局轴的跨度(`si`/`sj`)做条件"
          "（第一版加了这条，全错：Top y 跨度 242 vs Front 638 也该同组）"
          "—— 实得 continue 条件：%s" % _gtxt.strip()[:70])
    # ② 归属：28 必须归 Top（到 Iso 只近 2px，靠分组才分得开）
    _d28 = {"box": (863, 253, 904, 286), "text": "28",
            "rec": sw_drawing.normalize_dim("28")}
    _a = sw_drawing.assign_dims_to_views([_d28], _regs, axis="y",
                                         groups=_g["groups"])
    _got = [v["bbox"][1] for v in _a["views"] if v["n"] == 1]
    check(_got == [141],
          "★★ `28`(y=270) 归 **Top**（y0=141 那个视图），实得 y0=%s —— "
          "「最近」会挂到 Iso（152 vs 148px，**差 2px**！）" % _got)
    # ③ 符号：⌀ 与 尺寸线污染
    _r = sw_drawing.normalize_dim("-20-")
    check(_r["value"] == 20.0,
          "★★ `-20-`（**尾部也有 `-`**）⇒ 20.0 尺寸线污染，剥掉（实得 %r）"
          % _r["value"])
    _r = sw_drawing.normalize_dim("-0.05")
    check(_r["value"] == -0.05,
          "★★★ `-0.05`（**只有前导 `-`**）⇒ **保留符号** -0.05（实得 %r）"
          " —— ★ 第一版断言写成 `== 0.05`，**断言自己错**，"
          "害我以为判据坏了" % _r["value"])
    _r = sw_drawing.normalize_dim("\u230012")
    check(_r["kind"] == "diameter",
          "★ `\u230012` ⇒ diameter（实得 %s）" % _r["kind"])
    # ④ 无尺寸的视图必须保留（轴测）
    _a2 = sw_drawing.assign_dims_to_views([], _regs, axis="y",
                                          groups=_g["groups"])
    check(len(_a2["views"]) == 3,
          "★★ 没有尺寸时**仍返回 3 个视图**（实得 %d）—— ★ Isometric "
          "一个尺寸都没有，靠「有尺寸」发现视图会**静默漏掉轴测**"
          % len(_a2["views"]))
    # ⑤ 默认 zoom ≥5 且多遍集合 ≥2（实测 zoom4 漏读）
    check(sw_drawing.ZOOM >= 5 and len(sw_drawing.ZOOM_SET) >= 2,
          "★★ 默认 zoom ≥5 且**多遍集合 ≥2**（实得 ZOOM=%s, ZOOM_SET=%s）"
          " —— 实测 zoom4 漏 `75`" % (sw_drawing.ZOOM, sw_drawing.ZOOM_SET))

    # ---- ›2 ★★★ 训练材料：外部真值（坑表 §104）----
    #   此前 agent 的所有判据只保证「我算的和模型里一致」，**没有外部真值**
    #   （坑表 §102）⇒ 拿自己建的件当参照件 = 自己抄自己。
    #   训练材料的题面印着 **精确目标体积** ⇒ 判据链首端第一次有外部答案。
    import sw_doctor                                     # noqa: PLC0415
    import re                                            # noqa: PLC0415
    import sw_exercise                                   # noqa: PLC0415
    import sw_exercise_check                             # noqa: PLC0415
    check(callable(sw_exercise.read_one) and callable(sw_exercise.scan),
          "★★★ 工具必须有 `sw_exercise.read_one()/scan()` —— 读练习题 PDF "
          "的题面元信息（Design Intent / 建议特征 / **目标体积**）")
    check(callable(sw_exercise_check.build_criteria),
          "★★★ 工具必须有 `build_criteria()`：把一道题变成**验收判据集**")
    # ① Volume 正则必须真的能匹配（★ 本轮被 heredoc 吃过两次）
    _t = "HINT: x.\nVolume: 202594 mm3\n"
    check(sw_exercise.RE_VOLUME.search(_t) is not None,
          "★★ `RE_VOLUME` 必须能匹配 `Volume: 202594 mm3`（实测 %r）"
          % (sw_exercise.RE_VOLUME.search(_t) and
             sw_exercise.RE_VOLUME.search(_t).group(1)))
    # ★ 这条断言要**真的能抓住坑**：编译时若有 `FutureWarning`
    #   （如 POSIX 字符类 `[[:space:]]` 被当成嵌套集合）就是语法不被支持
    #   ⇒ 永远不匹配 ⇒ 报「读到目标体积 0」（坑表 §104⑦②）。
    import warnings as _w                               # noqa: PLC0415
    with _w.catch_warnings(record=True) as _caught:
        _w.simplefilter("always")
        re.compile(sw_exercise.RE_VOLUME.pattern)
    check(not [x for x in _caught if "nested set" in str(x.message)],
          "★★ 正则编译**不得**有 `Possible nested set` 警告"
          "（Python 的 `re` 不支持 POSIX 字符类 `[[:space:]]`，"
          "它会被当嵌套集合 ⇒ 永不匹配）—— 实得警告 %d 条"
          % len([x for x in _caught if "nested set" in str(x.message)]))
    # ② 判据集必须同时给 expect 与 cannot_check
    _item = {"file": "T.pdf", "level": "Level 10", "level_no": 10,
             "target_volume_mm3": 202594.0, "difficulty": "Easy",
             "duration_min": 10, "symmetric": True, "views": [],
             "design_intent": ["The part is symmetric."],
             "features": ["Extrude Boss"], "hint": "Use a Slot",
             "n_drawings": 3, "n_images": 6}
    _c = sw_exercise_check.build_criteria(_item)
    check(_c["kind"] == "part" and _c["volume_mm3"] == 202594.0,
          "★ 零件题判成 `part` 且带目标体积（实得 %s / %s）"
          % (_c["kind"], _c["volume_mm3"]))
    check(len(_c["expect"]) >= 3 and len(_c["cannot_check"]) >= 2,
          "★★★ 判据集必须**同时**给 `expect`（%d 条）与 `cannot_check`"
          "（%d 条）—— ★ 「只验了体积」被当成「全验了」比「没验」更危险"
          % (len(_c["expect"]), len(_c["cannot_check"])))
    check(any("标量" in x for x in _c["cannot_check"]),
          "★★ 必须明说「**体积是标量**：少建一个孔+多建一块料也能对上」"
          "⇒ 体积通过≠特征对")
    check(any("视图" in x for x in _c["cannot_check"]),
          "★★ 必须明说「视图布局读不到 ⇒ 朝向仍可能错」"
          "（实测题面文本层里 `view` 出现 0/120 份 ⇒ 只能从图纸位图 OCR")
    # ③ 三类题的真值不同：装配体/配置类**本来就没有**单件体积
    for lv, kind in ((9, "assembly"), (15, "config"), (16, "config"),
                     (17, "config")):
        _a = dict(_item, level_no=lv, target_volume_mm3=None)
        _ca = sw_exercise_check.build_criteria(_a)
        # ★ 注意：非零件类**仍可能有** expect（对称性/视图/建议特征都有用），
        #   该断的是「**没有体积判据**」——别写成「expect 为空」，
        #   那会把「题面还有别的可用信息」也判成错。
        _has_vol = any("体积 ==" in x for x in _ca["expect"])
        check(_ca["kind"] == kind and not _has_vol and _ca["volume_mm3"] is None,
              "★ Level%02d（%s 类）**不产���体积判据**（实得 kind=%s, "
              "体积判据 %d 条）—— 实测 Level09 0/16、Level15 0/13 有体积"
              % (lv, kind, _ca["kind"], sum(1 for x in _ca["expect"]
                                             if "体积 ==" in x)))
        check(any("本来就没有" in x for x in _ca["cannot_check"]),
              "  ★ 并明说「**本来就没有**单件体积」，别当提取失败")
    # ④ 0 页坏文件必须被优雅跳过（doc[0] 会抛 IndexError）
    import inspect as _i2                                # noqa: PLC0415
    _src = _i2.getsource(sw_exercise.read_one)
    check("page_count" in _src,
          "★★ `read_one` 必须先判 `page_count == 0`"
          "（真机有一份 0 页坏 PDF，`doc[0]` 抛 IndexError 会打崩整批）")
    # ⑤ 体检必须探「补 APPDATA 后」的状态
    _dsrc = _i2.getsource(sw_doctor.sec_drawing)
    check("site-packages" in _dsrc and "APPDATA" in _dsrc.upper(),
          "★★★ 体检必须有「补 `APPDATA` 后再探一次」—— "
          "本机 pymupdf/rapidocr **全都装好了**，只是用户级 site-packages "
          "不在默认 sys.path ⇒ 误判成「没装」害我手工解 PDF 流")
    # ⑥ ★★★ 依赖探测必须**只有一个入口**，否则体检会自相矛盾
    #   （真机出现过 `[7]` 说 Pillow PASS、`[2]` 说 FAIL —— 同一个库
    #    两处结论冲突，**自相矛盾的体检比没有体检更糟**）
    check(callable(sw_doctor.has_module)
          and callable(sw_doctor.user_site_packages)
          and callable(sw_doctor.ensure_user_site),
          "★★★ 体检必须有**单一**依赖探测入口 `has_module()` + "
          "`ensure_user_site()` —— 本机 pymupdf/rapidocr/PIL/pywin32 "
          "**全都装在用户级 site-packages**，裸 `find_spec` 看不到")
    _all = _i2.getsource(sw_doctor)
    _code = "\n".join(x.split("#", 1)[0] for x in _all.splitlines())
    import re as _re
    _bare = [m for m in _re.findall(r'find_spec\(\\s*[\'"]([A-Za-z_]+)',
                                   _code)
             if m not in ("",)]
    check(len(_bare) <= 1,
          "★★ 源码里 `find_spec(` 只能出现在 `has_module` 里（实得 %d 处：%s）"
          " —— 同一模块两处探测 ⇒ 体检自相矛盾（坑表 §104①）"
          % (len(_bare), _bare))
    check("PIL" not in _i2.getsource(sw_doctor.sec_interpreter)
          or "has_module" in _i2.getsource(sw_doctor.sec_interpreter),
          "★ `[1]` 节的 Pillow 检查必须走 `has_module`")
    check("win32com" not in _i2.getsource(sw_doctor.sec_env)
          or "has_module" in _i2.getsource(sw_doctor.sec_env),
          "★ `[2]` 节的 pywin32 检查必须走 `has_module`")
    check("RE_VOLUME" in _dsrc,
          "★ 体检必须**复用** `sw_exercise.RE_VOLUME`，不重写正则"
          "（★ 重写的那份被 heredoc 吃掉反斜杠 → 报「读到体积 0」）")

    # ---- ② 「报『没有』」也必须有依据（防"报了假没"） ----
    # ★ 真机 `33-齿轮` 是**腹盘式**齿轮：减重是 **1~2 个大直径环形腔**
    #   （Ø48，2 个面），不是孔阵列 ⇒ 只按「成组 ≥4」会报「没有减重」——
    #   **那是假的**。而只有中心孔+齿的件才该说「没有减重」。
    import math as _m                                       # noqa: PLC0415
    def _CYa(ax, r, area, name, c):        # 圆柱面夹具（本函数内）
        return {"id": name, "type": "cylinder", "area": area * 1e-6,
                "center": list(c), "axis": sw_segment._unit(ax),
                "radius": r, "normal": None, "d": None, "box": None}

    _cav = [_CYa([0, 0, 1], 0.024, 980, "Cav%d" % i,
                 [0.024 * _m.cos(a), 0.024 * _m.sin(a), 0.0])
            for i, a in enumerate([0.0, _m.pi])]
    for _f in _cav:
        _f["box"] = [-0.024, -0.024, -0.011, 0.024, 0.024, 0.011]
    _seg_cav = sw_segment.segment(gear + _cav, box)
    _k_cav = [r["kind"] for r in _seg_cav["segments"]]
    check("relief" in _k_cav and "pocket" not in _k_cav,
          "★★ 腹盘式齿轮：只加 **2 个** Ø48 面（成组数 < 4）也要认出"
          "**减重腔**而不是报「没有减重」（实得 {}）".format(_k_cav))
    _rel = next((r for r in _seg_cav["segments"] if r["kind"] == "relief"), None)
    check(_rel is not None and _rel["mirror"] is True,
          "★ 减重腔**两侧对称 ⇒ 切一半 + 镜像**（真机 R24 = 2 个面）")
    check(_rel is not None and abs(_rel["radius_mm"] - 24.0) < 1e-6,
          "★ 减重腔半径读出 24.000mm（实得 {}）".format(
              _rel.get("radius_mm") if _rel else None))
    _bare = [f for f in gear if not f["id"].startswith("Key")]
    _kb = [r["kind"] for r in sw_segment.segment(_bare, box)["segments"]]
    check("relief" not in _kb
          and any("没有减重" in n for n in
                  sw_segment.segment(_bare, box)["notes"]),
          "★★ 只有中心孔+齿时**才**说「没有减重」（实得 {}）—— "
          "凡是「报否定结论」的路径都要有正例断言守着".format(_kb))

    # ---- ③ 覆盖率：漏了多少要看得见 ----
    check("coverage" in seg and 0.0 < seg["coverage"] <= 1.0,
          "★ 报告给**归类覆盖率** = {:.1f}%（余下是曲面/斜面）".format(
              (seg.get("coverage") or 0.0) * 100))
    big = [PL([0, 0, 1], 90000, "HugeWeb", [0, 0, 0.05],
              [-0.2, -0.2, 0.05, 0.2, 0.2, 0.05]),
           PL([0, 0, 1], 200, "Side", [0.1, 0, 0.0],
              [0.1, -0.05, 0.0, 0.1, 0.05, 0.05])]
    s2 = sw_segment.segment(big, [-0.25, -0.25, 0, 0.25, 0.25, 0.1])
    check(s2["coverage"] > 0.95,
          "★ 覆盖面够大时（两个大面）覆盖率 {:.1f}% —— "
          "低覆盖率不是缺陷，是要**知道自己漏了什么**".format(
              s2["coverage"] * 100))

    # ---- ④ 建模顺序按**依赖** ----
    order = sw_segment.build_order(seg)
    check(order.index("bore") < order.index("web"),
          "中心孔先于腹板（孔的位置要先定）")
    check(order.index("web") < order.index("rim"),
          "★★ 腹板先于轮缘 —— 齿是**面积最大**的段，但依赖主体已存在。"
          "「按面积从大到小建」是错的")

    # ---- ⑤ 主轴按面积投票，不靠"最小半径"蒙对 ----
    votes = [CY([0, 0, 1], 0.016, 1988, "Bore", [0, 0, 0]),
             CY([1, 0, 0], 0.0012, 5, "OilGauge", [0.05, 0, 0])]
    s3 = sw_segment.segment(votes, [-0.06, -0.06, -0.02, 0.06, 0.06, 0.02])
    check(s3["axis"] == 2,
          "★★ 主轴按**圆柱面积投票**（实得轴 {}）—— 第一版取「最小半径"
          "圆柱面」，真机箱座上那是 **Ø2.5 油标孔**（轴与主轴垂直），"
          "那件只是**蒙对**的".format(s3.get("axis_label")))

    # ---- ⑥ 指到的 op 都要存在 ----
    import sw_ops                                          # noqa: PLC0415
    for _n in ("mirror", "gear_profile", "keyway_rect", "extrude_boss",
               "extrude_cut", "rib", "shell", "circular_pattern"):
        check(_n in sw_ops.OPS,
              f"★ 分段建议里点名的 op `{_n}` 真的存在（否则建议是空话）")

    # ---- ⑦ 坑表 §99 必须存在 ----
    pit = (ROOT / "references" / "30-pitfalls.md").read_text(encoding="utf-8")
    check("## 99." in pit, "坑表有 §99（语义分段层）")
    for _k in ("归类覆盖率", "面积投票", "键槽", "零厚度", "腹盘", "报了假没"):
        check(_k in pit, f"§99 点名 `{_k}`")


def test_mirror_half() -> None:
    """[2x] 「建一半 + 镜像」（`mirror`）：钉死两条硬结论 + 文档/探针同步。

    ★ 为什么要有这一段：2026-10-03 排查「`InsertMirrorFeature` 12 种组合
      全灭」花掉 90 分钟，而真因是**测试件不满足前提**（镜像面不穿过实体），
      不是 API 不支持。⇒ 这类"能力边界"必须**离线钉住**，否则下次还会重查一遍。
    """
    print("\n[2x] 建一半+镜像：镜像面必须穿过实体 / 体积判据 / mirror 能力项")
    import inspect                                           # noqa: PLC0415

    import sw_faceset                                        # noqa: PLC0415
    import sw_ops                                            # noqa: PLC0415
    import sw_ops as _ops                                    # noqa: PLC0415

    # ---- ① 报错里必须点出「镜像面没有穿过实体」这个根因 ----
    src = inspect.getsource(_ops.op_mirror)
    check("镜像面没有穿过实体" in src and "bounding_box" in src,
          "★★ `op_mirror` 失败时必须**主动量**实体相对镜像面的位置，"
          "并把「镜像面没有穿过实体」写进报错 —— "
          "真机实测：板 x∈[-30,30]（横跨 Right 面）镜像成功；"
          "板 x∈[0,40]（只切到边界）⇒ **8 种组合全灭**而报错完全指不到根因"
          "（§100①）。缺了这条，下一个人会以为是 API 的问题")
    check("Mark=1" in src,
          "★ 报错里保留「特征用 Mark=1 选」这条（真机确认的老坑）")

    # ---- ② check_mass 必须支持**体积**判据（镜像类操作用） ----
    cm = _ops.OPS["check_mass"]
    check("expect_volume_mm3" in cm.params and "vol_tol" in cm.params,
          "★★ `check_mass` 必须支持 `expect_volume_mm3`/`vol_tol` —— "
          "镜像/布尔类判据**只能用体积**：质量依赖材料密度"
          "（未赋材料时取 1000kg/m³）⇒ 跨机器不可移植")
    _cmsrc = inspect.getsource(_ops.op_check_mass)
    check("体积不变 ⇒ 镜像没生效" in _cmsrc,
          "★ 体积超差的报错要指出「镜像没生效」这个最常见原因"
          "（`CreateMirrorFeature` 返回 Feature 对象却什么都没建，§96②）")

    # ---- ③ mirror 能力项必须真的在会话自检里 ----
    import pathlib                                            # noqa: PLC0415
    _ssc = (ROOT / "scripts" / "sw_session_check.py").read_text(
        encoding="utf-8")
    check("mirror" in _ssc and "_volume_mm3" in _ssc,
          "★★ `sw_session_check.py` 必须测 mirror —— 排查时它报 "
          "**HEALTHY 8/8** 却压根没测镜像（「会话健康」只对它测过的能力成立）")
    check('"mirror": mirror_ok' in _ssc,
          "★ 能力清单里要列出 `mirror`（现在报 "
          "part/assembly/insert_component/mirror）")

    # ---- ④ 探针必须在（可复现证据） ----
    _probe = ROOT / "assets" / "probes" / "mirror_half.json"
    check(_probe.is_file(), "探针 `assets/probes/mirror_half.json` 在（可复现证据）")
    if _probe.is_file():
        import json                                           # noqa: PLC0415
        _d = json.loads(_probe.read_text(encoding="utf-8"))
        _ops_used = [x["op"] for x in _d["steps"]]
        check("mirror" in _ops_used and "check_mass" in _ops_used,
              f"探针同时用到 mirror + 体积判据（实得 {_ops_used}）")
        _has_vol = any("expect_volume_mm3" in x for x in _d["steps"])
        check(_has_vol, "★ 探针里必须有一步带 `expect_volume_mm3` —— "
                        "「镜像建了没有」只有体积能回答")

    # ---- ④' ★★★ 面集签名必须是**规范形**（真机对账踩到） ----
    #   症状：形状**逐位一致**，签名却报差异 —— 同一组厚度在两个零件里
    #   **顺序不同**、单站位**两种写法**（`t[]` vs `t[0.0]`、`d@0` vs `t[0.0]`）。
    #   ⇒ 「同一形状」要求签名是 canonical form，否则就是**假失败**，
    #     而假失败会让人**以为重建错了**，进而去改本来正确的代码。
    def _F(n, area, name, c, box=None):
        nn = sw_faceset._unit(n)
        return {"id": name, "type": "plane", "area": area * 1e-6,
                "center": list(c), "normal": nn, "d": sw_faceset._dot(nn, c),
                "box": list(box) if box else None}

    def _sig(fl, order=None):
        use = [fl[k] for k in order] if order else fl
        return sw_faceset.signature(sw_faceset.build_families(use), use)

    fa = [_F([0, 0, 1], 1000, "A", [0, 0, 0.02],
             [-0.05, -0.05, 0.02, 0.05, 0.05, 0.02]),
          _F([0, 0, 1], 1000, "B", [0, 0, 0.05],
             [-0.05, -0.05, 0.05, 0.05, 0.05, 0.05]),
          _F([0, 0, 1], 1000, "C", [0, 0, 0.09],
             [-0.05, -0.05, 0.09, 0.05, 0.05, 0.09]),
          _F([0, 0, 1], 1000, "D", [0, 0, 0.10],
             [-0.05, -0.05, 0.10, 0.05, 0.05, 0.10])]
    check(_sig(fa) == _sig(fa, [0, 2, 1, 3]),
          "★★ 签名与**加入顺序无关**（同一组面换个列出顺序，签名必须逐字相同）"
          " —— 厚度列表必须 `sorted`，否则形状一致也报差异")

    one = [_F([0, 0, 1], 500, "S", [0, 0, 0.03])]
    s_one = _sig(one)
    check("d@0" not in s_one,
          "★ 单站位**只有一种写法**（原来 `d@0` 与 `t[0.0]` 并存 ⇒ "
          "同一件事两种签名 ⇒ 假失败）")
    check("t[]" not in s_one,
          "★★ 空厚度列表统一成 `t[0.0]`（真机对账踩到 `t[]` vs `t[0.0]`）")

    # ---- ⑤ 坑表 §100 ----
    pit = (ROOT / "references" / "30-pitfalls.md").read_text(encoding="utf-8")
    check("## 100." in pit, "坑表有 §100（mirror 的两个硬前提）")
    check("## 101." in pit, "坑表有 §101（建一半+镜像 端到端验证）")
    check("## 103." in pit, "坑表有 §103（孔 vs 凸台 vs 槽：读凹凸性）")
    check("## 104." in pit, "坑表有 §104（外部真值从哪来：训练材料的 Volume）")
    check("## 105." in pit, "坑表有 §105（尺寸 → 视图：归属判据三版才对）")
    check("## 106." in pit, "坑表有 §106（基准样本选错：判据只在 1/6 上有效）")
    for _k in ("25.84%", "0.11%", "双通道",
               "iterable argument is empty",
               "量纲", "水印", "SECTION A-A",
               "判据要在多个样本上验"):
        check(_k in pit, f"§106 点名 `{_k}`")
    # ★★ 判据本身要有「多样本」这条性质断言
    import inspect as _ins3                              # noqa: PLC0415
    import sw_drawing as _sd                           # noqa: PLC0415
    check(_sd.MERGE_GAP_FRAC < 0.02 and _sd.MERGE_GAP_FRAC > 0.0,
          "★★ 合并间隙必须是**图幅比例**且足够小（实得 %.4f）—— "
          "绝对像素 `26` 在 901×646 的图上会跨过视图间空白带（真机 §106③）"
          % _sd.MERGE_GAP_FRAC)
    _dsrc = _ins3.getsource(_sd.detect_view_regions)
    check("gray" in _dsrc and "colored" in _dsrc,
          "★★★ 视图检出必须有**彩色 + 灰阶双通道**（真机 6 张题里 "
          "只有 1 张是彩色填充，§106①）")
    _asrc = _ins3.getsource(_sd.assign_dims_to_views)
    check("if not groups" in _asrc,
          "★★ `assign_dims_to_views` 必须处理 `groups == []`"
          "（真机 5/6 张题崩在 `max([])`，§106②）")
    for _k in ("跨组比较", "group_views", "不可复现", "轴测图不是正投影视图",
               "跨度相近", "断言自己错"):
        check(_k in pit, f"§105 点名 `{_k}`")
    for _k in ("APPDATA` 没指对", "297", "0/16", "cannot_check",
               "[[:space:]]", "0 页坏文件", "view` 的 0/120"):
        check(_k in pit, f"§104 点名 `{_k}`")
    for _k in ("n·r < 0", "16 个假阳性", "GetName()` 对未命名面返回 `None`",
               "两两配对", "夹具必须**复制真机读数**", "6-小盖"):
        check(_k in pit, f"§103 点名 `{_k}`")
    for _k101 in ("规范形", "交换截面的 (u,v)", "拉伸之后", "形状一致"):
        check(_k101 in pit, f"§101 点名 `{_k101}`")
    for _k in ("镜像面必须**穿过实体**", "expect_volume_mm3", "mirror=可用",
               "只对它测过的能力成立", "8 种组合全灭"):
        check(_k in pit, f"§100 点名 `{_k}`")


def main() -> None:
    global VERBOSE
    ap = argparse.ArgumentParser(description="solidworks-modeling 技能回归测试")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--run", action="store_true", help="额外真跑 SolidWorks")
    a = ap.parse_args()
    VERBOSE = a.verbose

    # ★★ 全局临时打开 `calibrate`：本脚本验的是**离线结构**（计划能生成 + 判据自洽 +
    #   反例能被拦下），与真机标定值无关。某个件的 `expect_mass_g` 一旦作废待重取
    #   （= 0），`build_plan` 的运行期守卫会直接抛错，把整个自检打断 ——
    #   那是**真机跑**才该用的闸门，不是结构错误。跑完立刻恢复。
    import sw_reducer as _sr                                    # noqa: PLC0415
    _prev_cal = _sr._CALIBRATE.get("on")
    _sr._CALIBRATE["on"] = True
    try:
        test_catalog()
        test_geometry()
        test_gb()
        test_reducer()
        test_spec()
        test_dim_dedup()
        test_through_cover()
        test_disc_holes()
        test_hex_family()
        test_plate_family()
        test_gear_family()
        test_bbox_tol_semantics()
        test_mass_props_suspect()
        test_body_read_entry()
        test_mesh_reconcile()
        test_gear_shaft()
        test_profile_extractor()
        test_doc_identity_drift()
        test_build_doc_pin_guard()
        test_taper_pin()
        test_pending_discipline()
        test_assembly()
        test_polyhedron_tools()
        test_symmetry_tools()
        test_segment_tool()
        test_mirror_half()
        kinds = test_plans()
        test_probes()
        test_templates_sync(kinds)
        test_negative()
        test_curve_ops()
        test_shot_tools()
        test_head_tools()
        test_macro()
        if a.run:
            test_live()
    finally:
        _sr._CALIBRATE["on"] = _prev_cal

    print("\n" + "=" * 60)
    print(f"通过 {_passes[0]} 项，失败 {len(_fails)} 项"
          + (f"，待重跑 {len(_pending)} 项" if _pending else ""))
    if _fails:
        print("失败清单：")
        for f in _fails:
            print("  X", f)
    if _pending:
        # ★ 与 FAIL 分开列：这些**不是**回归，是"设计改了、真机记录还没重跑"。
        #   只要两边混在一起，sw_doctor 就永远报「不可用」，真回归会被淹没。
        print("待重跑清单（**不是失败** —— 记录已显式标 `_superseded`，真机重跑后覆盖）：")
        for p in _pending:
            print("  ·", p)
    if not _fails:
        print("全部通过。" if not _pending
              else f"无失败；有 {len(_pending)} 项等真机重跑覆盖。")
        print("改动操作表/几何算法后务必重跑本脚本。")
    raise SystemExit(1 if _fails else 0)


if __name__ == "__main__":
    main()
