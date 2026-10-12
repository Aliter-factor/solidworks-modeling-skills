#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_gear.py —— 标准机械零件的几何计算（纯 Python，不依赖 SolidWorks、不依赖 pywin32）。

职责边界：
    本模块只算「几何点」和「参数校验」，不碰 COM。算出点之后由 sw_ops.py 去画草图。
    这样分开的好处是：没有 SolidWorks、没有 pywin32 也能跑单元测试，
    几何算错了在离线阶段就暴露，不用等开软件。

单位约定：
    外部输入一律**毫米 / 度**；返回值也是毫米（草图平面 2D 坐标）。
    转成 SolidWorks 要的米，由上层统一做（SW 的草图坐标和特征的深度都是**米**）。

齿轮数学（直齿/斜齿通用，斜齿按端面换算）：
    mt = mn / cosβ                    端面模数
    αt = atan(tanαn / cosβ)           端面压力角
    d  = mt·z                         分度圆
    db = d·cosαt                      基圆
    da = d + 2·mn·(ha* + x)           齿顶圆
    df = d − 2·mn·(ha* + c* − x)      齿根圆
    半齿厚角(分度圆)  ψp = st / d，   st = mt·(π/2 + 2x·tanαn)
    渐开线右齿廓极角  φ(r) = ψp + inv(αt) − inv(αr)，  αr = acos(db/2r)，inv(t)=tan t − t
        r=da 时最窄、r=db 时最宽、r=d 时正好 ψp —— 这三个点可以当自检。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict

# ★★ 标准件尺寸只有**一个权威来源** = assets/gb_standard.json（经 sw_gb 读）。
#   本模块**不再自留任何标准件表** —— 早先这里有一份 HEX_BOLT_TABLE 和一份轴承表，
#   和 JSON 里的值**对不上**（M8 的 dw 这写 11.5、JSON 写 11.6；M20 写 27.7 vs 28.2），
#   正是「改一处忘一处」的经典翻车。现在统一走 sw_gb（纯函数，不依赖 SolidWorks）。
try:
    import sw_gb
except ImportError:                                     # 直接跑本文件时的兜底
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import sw_gb

# ------------------------------------------------------------------ 通用小工具


def deg2rad(d: float) -> float:
    return d * math.pi / 180.0


def involute(t: float) -> float:
    """渐开线函数 inv(t) = tan t − t（t 为弧度）。"""
    return math.tan(t) - t


def _fmt(v: float, nd: int = 4) -> float:
    return round(v, nd)


# ------------------------------------------------------------------ 齿轮


@dataclass
class GearParams:
    """直齿/斜齿圆柱齿轮的完整几何参数。模数类用毫米。"""

    mn: float            # 法向模数
    z: int               # 齿数
    beta: float = 0.0    # 螺旋角（度），0 = 直齿
    alpha_n: float = 20.0  # 法向压力角（度）
    x: float = 0.0       # 变位系数
    ha_star: float = 1.0   # 齿顶高系数
    c_star: float = 0.25   # 顶隙系数
    b: float = 20.0      # 齿宽（轴向），仅用于拉伸，不参与齿形
    bore: float | None = None   # 中心孔直径；None = 不挖
    keyway: str | None = None   # 键槽规格，如 "8x3.5"（宽x深），配合 bore 用
    n_flank: int = 8     # 单侧齿廓取样点数（含基圆与齿顶两端点）
    n_tip: int = 3       # 齿顶圆弧取样点数（含两端点；中间点 = n_tip−2）
    n_root: int = 3      # 齿根圆弧取样点数（含两端点）
    root_fillet: float | None = None  # 齿根过渡圆角半径；None → 0.38·mn

    # ---- 派生量（在 __post_init__ 里算）----
    mt: float = field(init=False)
    alpha_t: float = field(init=False)
    d: float = field(init=False)
    db: float = field(init=False)
    da: float = field(init=False)
    df: float = field(init=False)
    st: float = field(init=False)
    psi_p: float = field(init=False)
    alpha_a: float = field(init=False)

    def __post_init__(self) -> None:
        cb = math.cos(deg2rad(self.beta))
        if cb <= 1e-9:
            raise ValueError("螺旋角 beta 过大（应 < 45°）")
        self.mt = self.mn / cb
        self.alpha_t = math.atan(math.tan(deg2rad(self.alpha_n)) / cb)
        self.d = self.mt * self.z
        self.db = self.d * math.cos(self.alpha_t)
        self.da = self.d + 2.0 * self.mn * (self.ha_star + self.x)
        self.df = self.d - 2.0 * self.mn * (self.ha_star + self.c_star - self.x)
        # 端面分度圆齿厚 → 半齿厚角
        self.st = self.mt * (math.pi / 2.0 + 2.0 * self.x * math.tan(deg2rad(self.alpha_n)))
        self.psi_p = self.st / self.d
        self.alpha_a = (
            math.acos(min(1.0, self.db / self.da)) if self.da > self.db else 0.0
        )

    # ---------- 校验 ----------

    def diagnostics(self) -> list[dict]:
        """返回问题清单（level: error/warn）。交给上层决定是拒绝还是让用户确认。"""
        out: list[dict] = []
        a = deg2rad(self.alpha_n)
        # 1) 根切：标准齿条刀具切齿，不变位时 z_min = 2·ha*/sin²α
        x_min = self.ha_star - (self.z * math.sin(a) ** 2) / 2.0
        if self.x < x_min - 1e-6:
            z_min = math.ceil(2.0 * self.ha_star / math.sin(a) ** 2)
            out.append({
                "level": "error" if self.x <= 0 and self.z < z_min else "warn",
                "code": "undercut",
                "msg": f"z={self.z} 且 x={self.x} 会根切（不变位最少 z={z_min}，"
                       f"此处至少需要 x={_fmt(x_min, 3)}）",
            })
        # 2) 齿顶变尖：齿顶圆上的弧齿厚 sa = da·(ψp + inv αt − inv αa)
        sa = self.da * (self.psi_p + involute(self.alpha_t) - involute(self.alpha_a))
        if sa <= 0:
            out.append({"level": "error", "code": "pointed_tip",
                        "msg": f"齿顶已变尖（sa={_fmt(sa,3)} ≤ 0），需减小 x 或增大 z"})
        elif sa < 0.25 * self.mn:
            out.append({"level": "warn", "code": "thin_tip",
                        "msg": f"齿顶弧齿厚 sa={_fmt(sa,3)}mm 偏薄（<0.25m={_fmt(0.25*self.mn,3)}）"})
        # 3) 根圆与基圆关系：z ≥ 42 时 df > db，齿廓根段不需要径向直线段
        if self.df < self.db - 1e-9:
            out.append({"level": "info", "code": "root_below_base",
                        "msg": f"df={_fmt(self.df,3)} < db={_fmt(self.db,3)}，"
                               f"齿根段用径向直线补（渐开线在基圆内不存在）"})
        # 4) 常规边界
        if self.z < 6:
            out.append({"level": "error", "code": "too_few_teeth", "msg": f"齿数 {self.z} 过少（<6）"})
        if self.df <= 0:
            out.append({"level": "error", "code": "bad_root", "msg": "齿根圆 ≤ 0，参数不自洽"})
        if self.b <= 0:
            out.append({"level": "error", "code": "bad_width", "msg": "齿宽必须 > 0"})
        if self.bore is not None and self.bore >= self.df - 2.0 * self.mn:
            out.append({"level": "error", "code": "bore_too_big",
                        "msg": f"孔径 {self.bore} 太接近/超过齿根圆 {_fmt(self.df,3)}，轮缘会太薄"})
        return out

    def summary(self) -> dict:
        return {
            "mn": self.mn, "z": self.z, "beta": self.beta, "alpha_n": self.alpha_n,
            "x": self.x, "b": self.b,
            "mt": _fmt(self.mt), "alpha_t_deg": _fmt(math.degrees(self.alpha_t), 3),
            "d": _fmt(self.d), "db": _fmt(self.db), "da": _fmt(self.da), "df": _fmt(self.df),
            "st": _fmt(self.st), "sa": _fmt(self.da * (self.psi_p + involute(self.alpha_t)
                                                       - involute(self.alpha_a))),
        }

    # ---------- 几何点 ----------
    #
    # 坐标约定（**唯一**的约定，改代码前先看这里）：
    #   · 原点 = 轴心；角度以 +x 轴为 0，逆时针为正（与 SolidWorks 草图一致）。
    #   · 第 k 个齿的中心线方向 = base + 90°，base = 2πk/z。
    #   · 齿廓上用「半齿厚角 φ(r)」描述：右齿廓在中心线 −φ(r) 侧，左齿廓在 +φ(r) 侧。
    #   · _phi() 收的是**半径**不是直径；r 与 φ 的量纲都是 mm / rad。

    def _phi(self, r: float) -> float:
        """半径 r 处的半齿厚角（rad）。r < db/2 时钳到基圆（基圆内无渐开线）。"""
        rb = self.db / 2.0
        r = max(min(r, self.da / 2.0), rb if rb > 1e-9 else 1e-9)
        ar = math.acos(min(1.0, rb / r))
        return self.psi_p + involute(self.alpha_t) - involute(ar)

    def _pt(self, r: float, phi: float, base: float) -> tuple[float, float]:
        """半径 r、偏中心线角 phi（右为负左为正）、齿序号偏移 base 的直角坐标。"""
        a = base + math.pi / 2.0 + phi
        return (_fmt(r * math.cos(a)), _fmt(r * math.sin(a)))

    @property
    def _r_flank_start(self) -> float:
        """齿廓起始半径：基圆与齿根圆取大者（z≥42 时齿根圆在基圆外）。"""
        return max(self.db, self.df) / 2.0

    def half_flank(self) -> list[tuple[float, float]]:
        """右齿廓点（从齿根侧到齿顶），第 0 号齿、未旋转。从下往上。"""
        r_start = self._r_flank_start
        ra = self.da / 2.0
        pts: list[tuple[float, float]] = []
        # df < db 时，渐开线在基圆内不存在 → 先用一段径向直线从齿根圆补到基圆
        if self.df / 2.0 < r_start - 1e-9:
            pts.append(self._pt(self.df / 2.0, -self._phi(r_start), 0.0))
        n = max(3, self.n_flank)
        for i in range(n):
            r = r_start + (ra - r_start) * (i / (n - 1))
            pts.append(self._pt(r, -self._phi(r), 0.0))
        return pts

    def tooth_sector(
        self, index: int = 0
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]], list[tuple[float, float]]]:
        """第 index 个齿的几何。

        返回 (右齿廓+左齿廓合成的一条「齿」点列, 齿根圆弧点, 齿顶圆弧点)：
            · tooth : 齿根 → 右齿廓 → 齿顶 → 左齿廓 → 齿根，**首尾都在齿根圆上**
            · root  : 本齿左齿根点 → 下一个齿右齿根点（沿齿根圆），两端不与 tooth 重复
            · tip   : 齿顶圆弧中间点（两端点已在 tooth 里），不会重复
        三者按 [tooth_k, root_k, tooth_k+1, root_k+1, ...] 顺序拼接即为整圈封闭轮廓。
        """
        base = 2.0 * math.pi * index / self.z
        phi_root = self._phi(self._r_flank_start)
        ra, rf = self.da / 2.0, self.df / 2.0
        phi_a = self._phi(ra)

        # 右齿廓（含可能的径向段）：从下往上。
        # 同时留下 (半径, 半齿厚角) 序列交给左齿廓复用 —— 这样左右两侧的端点
        # 是**同一个表达式**算出来的，接口处天然严丝合缝。
        r_start = self._r_flank_start
        pairs: list[tuple[float, float]] = []
        if rf < r_start - 1e-9:
            pairs.append((rf, phi_root))        # 基圆内不存在渐开线，用径向段补到齿根圆
        n = max(3, self.n_flank)
        for i in range(n):
            r = r_start + (ra - r_start) * (i / (n - 1))
            pairs.append((r, self._phi(r)))
        right = [self._pt(r, -p, base) for r, p in pairs]

        # 齿顶圆弧中间点（从右齿顶点绕到左齿顶点）。
        # 弧的两个端点分别由 right 末点、left 首点充当，所以这里只放中间点。
        tip: list[tuple[float, float]] = []
        nt = max(2, self.n_tip)
        for i in range(1, nt - 1):
            phi = -phi_a + 2.0 * phi_a * (i / (nt - 1))
            tip.append(self._pt(ra, phi, base))

        # 左齿廓：同一组 (r, φ) 把符号翻正再倒序，齿顶 → 齿根。
        # ★ 不要用 hypot(x,y) 反推半径：坐标已被 _fmt 取整，反推出来的半径会偏
        #   1e-4 量级，每个齿的齿根接口都会出现微缝。
        # ★ 也不要切掉首尾点：那会丢掉齿根端点，缺口变成 0.66mm 量级，真机拉伸
        #   直接报「草图轮廓未闭合」。
        left = [self._pt(r, p, base) for r, p in reversed(pairs)]
        tooth = right + tip + left

        # 齿根圆弧：本齿左齿根(角 +φ_root) → 下一齿右齿根(角 step−φ_root)，半径 rf
        step = 2.0 * math.pi / self.z
        root: list[tuple[float, float]] = []
        nr = max(2, self.n_root)
        for i in range(nr):
            phi = phi_root + (step - 2.0 * phi_root) * (i / (nr - 1))
            root.append(self._pt(rf, phi, base))
        return tooth, root, tip

    def full_outline(self, per_flank: int | None = None) -> list[tuple[float, float]]:
        """整圈封闭轮廓（mm），按 [齿, 齿根弧, 齿, 齿根弧, ...] 顺序，末点与首点不重复。"""
        old = self.n_flank
        if per_flank:
            self.n_flank = per_flank
        try:
            pts: list[tuple[float, float]] = []
            for k in range(self.z):
                tooth, root, _ = self.tooth_sector(k)
                pts += tooth + root
            return pts
        finally:
            self.n_flank = old

    def outline_polyline(self, per_flank: int | None = None) -> list[tuple[float, float]]:
        return self.full_outline(per_flank)

    def outline_closed(
        self,
        per_flank: int | None = None,
        n_root: int | None = None,
        n_tip: int | None = None,
        closed: bool = True,
    ) -> list[tuple[float, float]]:
        """给 SolidWorks 直接画图用的**整圈封闭点表**（mm，无重复点）。

        和 full_outline() 的区别（很重要，画图必须用这个）：
            · full_outline() 在「齿 → 齿根弧」和「齿根弧 → 下一齿」两处**端点重复**：
              tooth[-1] == root[0]、root[-1] == tooth_{k+1}[0]。
              拿它去 CreateLine 会生出 110 条**零长度线段**，SolidWorks 会静默丢弃
              （实测 1100 点只建出 990 条），而零长线段正是「轮廓莫名不自交」的来源。
            · 本方法把连续重复点去掉，得到 990 个**互不相同**的点。
        参数 closed=True 时把首点再补到末尾（配合 CreateSpline3 画单条闭合样条用）。
        """
        old = (self.n_flank, self.n_root, self.n_tip)
        if per_flank:
            self.n_flank = per_flank
        if n_root:
            self.n_root = n_root
        if n_tip:
            self.n_tip = n_tip
        try:
            pts: list[tuple[float, float]] = []
            for k in range(self.z):
                tooth, root, _ = self.tooth_sector(k)
                for p in tooth + root:
                    if not pts or abs(p[0] - pts[-1][0]) > 1e-12 or abs(p[1] - pts[-1][1]) > 1e-12:
                        pts.append(p)
            # 首尾也要去重（最后一个齿根弧的终点 = 第 0 齿右齿根点 = pts[0]）
            while len(pts) > 1 and abs(pts[-1][0] - pts[0][0]) < 1e-12 \
                    and abs(pts[-1][1] - pts[0][1]) < 1e-12:
                pts.pop()
            if closed and pts:
                pts = pts + [pts[0]]
            return pts
        finally:
            self.n_flank, self.n_root, self.n_tip = old


# ------------------------------------------------------------- 标准件：阶梯轴


def stepped_shaft(segments: list[dict], chamfer: float = 0.0) -> dict:
    """阶梯轴：segments = [{'d': 直径, 'l': 长度, 'name': 可选}, ...]，从一端到另一端。

    返回回转体的「半剖面轮廓」——绕 X 轴回转用，坐标 (x, y)，y = 半径。
    """
    if not segments:
        raise ValueError("阶梯轴至少要有一段")
    prof: list[tuple[float, float]] = []
    x = 0.0
    for i, s in enumerate(segments):
        d, ln = float(s["d"]), float(s["l"])
        if d <= 0 or ln <= 0:
            raise ValueError(f"第 {i+1} 段直径/长度必须 > 0")
        r = d / 2.0
        c = min(chamfer, ln / 3.0, r / 2.0) if chamfer > 0 else 0.0
        if c > 0:
            prof += [(x, r - c), (x + c, r)]
            prof += [(x + ln - c, r), (x + ln, r - c)]
        else:
            prof += [(x, r), (x + ln, r)]
        x += ln
    total_l = x
    # 闭合到轴线
    prof += [(x, 0.0), (0.0, 0.0)]
    return {"profile": prof, "length": _fmt(total_l), "revolution_axis": "x",
            "segments": segments}


def keyway_profile(bore: float, spec: str) -> dict:
    """平键键槽（轴上的槽 / 轮毂上的槽都用同一尺寸表）。

    spec 形如 '8x3.5' = 键宽 x 毂槽深（GB/T 1095 常用值）。
    返回轮毂侧键槽矩形（相对孔心的局部坐标，孔心为原点，槽开在 +y 方向）。
    """
    w, t = (float(v) for v in spec.lower().split("x"))
    r = bore / 2.0
    half = w / 2.0
    bottom = math.sqrt(max(r * r - half * half, 0.0))   # 键槽底与孔面的交点高度
    top = r + t
    return {
        "width": w, "depth": t, "bore": bore,
        "rect": [(-half, bottom), (half, bottom), (half, top), (-half, top)],
        "note": "槽底是平的；bottom 是槽侧壁与孔面的交点，实际建模时用「矩形+到孔壁的切除」",
    }


# ------------------------------------------------------------- 标准件：六角螺栓


def hex_bolt(spec: str, length: float, thread_len: float | None = None) -> dict:
    """六角头螺栓。spec='M8'，length=公称长度 L（不含头），thread_len=None → 全螺纹。

    ★ 尺寸全部来自 `assets/gb_standard.json`（GB/T 5782）—— 本函数不再自带表。
    六角头对边宽 s 与头高 k 查表；头部两端倒 30° 倒角。
    """
    spec = spec.upper().replace(" ", "")
    try:
        t = sw_gb.row("hex_bolt", spec)
    except KeyError:
        raise ValueError(f"暂不支持 {spec}，GB 表里只有 "
                         f"{', '.join(sw_gb.specs('hex_bolt'))}") from None
    d = float(t["d"])
    p = float(t["pitch"])
    L = float(length)
    if L <= float(t["k"]):
        raise ValueError("螺栓长度必须大于头高")
    tl = L if thread_len is None else min(float(thread_len), L)
    return {
        "spec": spec, "d": d, "pitch": p,
        "s": float(t["s"]), "k": float(t["k"]), "dw": float(t["dw"]),
        "length": L, "thread_len": tl,
        "hex_circumradius": _fmt(float(t["s"]) / math.cos(math.pi / 6.0)),  # 外接圆半径
        "head_chamfer": 30.0,
        "gb": sw_gb.gb_number("hex_bolt"),
        "note": "螺纹用装饰螺纹线（InsertCosmeticThread3）表示，不要真画螺纹牙；"
                "长度须落在 length_series.bolt 上（用 sw_gb.nearest_length 校）。",
    }


# ------------------------------------------------------------- 标准件：法兰


def flange(od: float, bore: float, thickness: float, bolt_circle: float,
           n_bolts: int = 6, bolt_d: float = 0.0, hub_od: float = 0.0,
           hub_len: float = 0.0, relief: float = 0.0) -> dict:
    """圆板法兰。所有尺寸 mm。返回可直接喂给 plan 的几何描述。"""
    if not (od > bore > 0):
        raise ValueError("必须满足 外径 > 内孔 > 0")
    if bolt_d <= 0:
        bolt_d = max(6.0, round(od / 12.0))
    if bolt_circle <= 0:
        bolt_circle = (od + bore) / 2.0
    # 注意：bolt_circle 是**分布圆直径**（BCD），中心距要除以 2 再比较
    if bolt_circle / 2.0 + bolt_d / 2.0 > od / 2.0 - 2.0:
        raise ValueError(
            f"螺栓孔已超出法兰外缘：BCD/2+孔半径={_fmt(bolt_circle/2+bolt_d/2,1)} "
            f"> 外半径−2={_fmt(od/2-2,1)}"
        )
    if bolt_circle / 2.0 - bolt_d / 2.0 < bore / 2.0 + 2.0:
        raise ValueError(
            f"螺栓孔与内孔干涉：BCD/2−孔半径={_fmt(bolt_circle/2-bolt_d/2,1)} "
            f"< 内半径+2={_fmt(bore/2+2,1)}"
        )
    return {
        "od": od, "bore": bore, "thickness": thickness,
        "bolt_circle": _fmt(bolt_circle), "n_bolts": int(n_bolts), "bolt_d": _fmt(bolt_d),
        "hub_od": hub_od, "hub_len": hub_len, "relief": relief,
        "bolt_angles": [round(360.0 * i / n_bolts, 4) for i in range(n_bolts)],
    }


def deep_groove_bearing(spec: str) -> dict:
    """深沟球轴承主要尺寸（GB/T 276）。

    ★ 尺寸来自 `assets/gb_standard.json` —— 覆盖 6000/6200/6300/6400/6800/6900 六个系列。
      本项目（一级减速器）用 **6204**（齿轮轴）与 **6206**（输出轴）。
    """
    key = str(spec).replace("GB/T 276-", "").replace("-", "").strip()
    try:
        t = sw_gb.bearing(key)
    except KeyError:
        co = sorted({str(r[0]) for r in sw_gb.family("deep_groove_bearing")["rows"]})
        raise ValueError(f"表里没有轴承 {spec!r}。可用：{', '.join(co)}") from None
    out = {"spec": key, "d": float(t["d"]), "D": float(t["D"]), "B": float(t["B"]),
           "r": float(t["r"]), "gb": sw_gb.gb_number("deep_groove_bearing"),
           "note": "轴承是标准件，建模只需内外圈+滚珠的简化体；装配可只做外轮廓。"
                   "轴配合取 k6、座孔取 H7（见 gb fits 节）。"}
    return out


# ------------------------------------------------------------------ 自检


def _selftest() -> None:
    """不依赖 SolidWorks 的几何自检：三个已知点 + 齿厚 + 根切判据。"""
    g = GearParams(mn=2, z=55, b=20)
    s = g.summary()
    print("m=2 z=55:", s)
    assert abs(s["d"] - 110.0) < 1e-6, s["d"]           # d = mz = 110
    assert abs(s["da"] - 114.0) < 1e-6, s["da"]          # da = m(z+2) = 114
    assert abs(s["df"] - 105.0) < 1e-6, s["df"]          # df = m(z-2.5) = 105
    assert abs(s["db"] - 103.369) < 0.01, s["db"]        # db = mz·cos20°
    assert not [d for d in g.diagnostics() if d["level"] == "error"], g.diagnostics()

    # 半齿厚角自检：分度圆上 φ 必须等于 ψp（_phi 收半径，传直径是经典错误）
    assert abs(g._phi(g.d / 2.0) - g.psi_p) < 1e-9, (g._phi(g.d / 2.0), g.psi_p)
    # 齿顶最窄、基圆最宽
    assert g._phi(g.da / 2.0) < g._phi(g.d / 2.0) < g._phi(g.db / 2.0) + 1e-12

    hf = g.half_flank()
    assert len(hf) == g.n_flank, len(hf)
    assert abs(math.hypot(*hf[0]) - g.df / 2.0) < 1e-3   # 首个点在齿根圆
    assert abs(math.hypot(*hf[-1]) - g.da / 2.0) < 1e-3  # 末个点在齿顶圆

    tooth, root, tip = g.tooth_sector(0)
    assert len(root) == g.n_root and len(tip) == max(0, g.n_tip - 2)
    for x, y in tooth + root + tip:
        r = math.hypot(x, y)
        assert g.df / 2.0 - 1e-3 <= r <= g.da / 2.0 + 1e-3, (x, y, r)
    # 轮廓必须首尾闭合：最后一个齿根点绕一圈后与第一个齿根起点同半径
    outline = g.full_outline()
    assert abs(math.hypot(*outline[0]) - g.df / 2.0) < 1e-3
    assert len(outline) == g.z * (g.n_flank + max(0, g.n_tip - 2)
                                  + max(0, g.n_flank - 2) + g.n_root), len(outline)
    # 相邻齿的齿根弧不应相交（齿根弧角宽 = step − 2φ_root > 0）
    assert 2.0 * math.pi / g.z - 2.0 * g._phi(g._r_flank_start) > 0

    # 小齿数必须报根切
    g17 = GearParams(mn=2, z=12)
    assert any(d["code"] == "undercut" for d in g17.diagnostics()), g17.diagnostics()
    # 大齿数不报
    assert not [d for d in GearParams(mn=2, z=60).diagnostics() if d["level"] == "error"]

    # 斜齿端面换算
    gh = GearParams(mn=2, z=30, beta=15.0)
    assert abs(gh.mt - 2.0 / math.cos(deg2rad(15))) < 1e-9
    assert gh.d > gh.mt * 30 - 1e-6

    print("stepped_shaft:", stepped_shaft([{"d": 20, "l": 30}, {"d": 30, "l": 50}])["length"])
    print("keyway 8x3.5 on d20:", keyway_profile(20, "8x3.5"))
    print("hex_bolt M8x65:", hex_bolt("M8", 65, thread_len=20))
    print("flange:", flange(od=160, bore=60, thickness=18, bolt_circle=0, n_bolts=6))
    print("bearing 6204:", deep_groove_bearing("6204"))
    print("outline points (z=55, n_flank=12):", len(g.full_outline()))
    print("\nsw_gear 自检通过")


if __name__ == "__main__":
    _selftest()
