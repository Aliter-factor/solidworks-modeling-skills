#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_segment.py —— 按**功能语义**给零件分段，并给每段定基准面与「是否镜像」。

## 为什么需要它（`sw_faceset` → `sw_prism` → `sw_symmetry` 之后的第四层）

前三层解决的是「几何上是什么」：
  · `sw_faceset`  平面族 / 棱柱判定
  · `sw_prism`    截面轮廓 + 出可执行计划
  · `sw_symmetry` 对称面 / 半边清单 / 装配定位面

缺的是「**该怎么建**」：一件复杂零件往往能拆成几段**功能不同**的部位，
每段的**基准面不同、是否镜像不同、用的 op 不同**。用户原话：

> 「大齿轮我们就可以拆分成三部分，一部分齿轮主体可以通过两侧对称进行拉伸，
> 然后切除减重槽的时候两侧是对称的，我们就可以切除一半然后进行对称命令完成
> 另一半的特征建设」

本脚本把「拆成哪几段」变成**可判定**的事：读面清单 → 按**几何证据**归类成
功能段 → 每段输出「基准面 / 是否镜像 / 建议 op / 为什么这么判」。

## 段的判据（全部基于几何证据，不是靠猜）

| 段 | 判据 | 建模含义 |
|---|---|---|
| `rim`（轮缘/齿） | 大量**周期性**小面（`other`/锥面，轴 ∥ 某轴，半径接近外径） | 齿廓单独成形（`gear_profile`），**不镜像**（它本身就是整圈） |
| `web`（腹板/盘） | **面积最大**的平面，且被上下两个平面夹住 | 主体，**可建一半 + 镜像** |
| `hub`（轮毂/凸台） | 位于两端、面积次大、法向 ∥ 腹板 | 两侧凸台，**成对 ⇒ 镜像** |
| `keyway`（键槽） | 窄长平面（宽 × 高 ≫ 厚），且成对出现 | 单侧特征，**不镜像** |
| `bore`（中心孔） | 圆柱面，轴 ∥ 腹板法向，半径最小 | 中心孔，**不镜像** |
| `pocket`（减重槽/腹板孔） | 圆柱面且**轴 ∥ 腹板法向**、半径**介于孔与外径之间**、且**成组出现**（≥4 个） | ★ 两侧对称 ⇒ **切一半 + 镜像** |
| `rib`（筋） | 平面且**横跨**对称面 | 建在对称面上，**不能只建一半** |

★ **「没有某一段」也要能报出来。** 真机实测 `33-齿轮-m=2,z=55`：
用户以为它有「减重槽」，实测半径谱里**只有 5 组圆柱面**（Ø32 中心孔、
Ø48 / Ø94 各 2 个、Ø105 / Ø114 各 55 个 = 齿），**没有任何成组的腹板孔**
⇒ `pocket = 0 个`。真实分段是 `rim + web + hub×2 + keyway + bore`。
**一个「本该有却没有」的段，和一个错判同样重要** —— 它会让人以为建模漏了。

## 一条设计红线：判据是**纯函数**

与 `sw_faceset` / `sw_symmetry` 同源：读数据要 SolidWorks，**判据一律纯函数**
（吃面清单吐分段），所以 `--self-test` 不开 SolidWorks 就能跑，
并被 `sw_selftest.py` 离线钉死。

## 用法

    export APPDATA="C:\\Users\\OMEN\\AppData\\Roaming"
    /c/Python313/python.exe scripts/sw_segment.py --doc "C:\\x\\大齿轮.SLDPRT"
    /c/Python313/python.exe scripts/sw_segment.py --doc xx.SLDPRT --json out.json
    /c/Python313/python.exe scripts/sw_segment.py --doc xx.SLDPRT --emit-plan p.json
    /c/Python313/python.exe scripts/sw_segment.py --self-test

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
import sw_symmetry                                          # noqa: E402

# ---------------------------------------------------------------- 常量与容差

#: 键槽的「窄长」判据：宽/厚 ≥ 2.5（真机键槽 10mm 宽 × 26mm 高，比例远超）
KEYWAY_SLENDERNESS = 2.5

#: 腹板判据：它必须是**面积最大**的平面，且占包围盒投影面积的比例够大
WEB_MIN_AREA_FRAC = 0.08

#: 轮缘判据：小面数量 ≥ 该阈值且半径 ≥ 外径的 85%（齿就在外圈）
RIM_MIN_FACES = 8
RIM_OUTER_FRAC = 0.85

#: 减重**孔阵列**判据：成组（同半径）数量 ≥ 该值（腹板上开一圈孔，典型 4~8 个）
POCKET_MIN_COUNT = 4

#: 减重**腔**判据：数量 ≤ 该值就是"腔"而不是"孔阵列"
#: （腹盘式齿轮 Ø48 减重腔 = 2 个面；孔阵列会是 4~8 个）
POCKET_MAX_FACES = 3

#: 「成组」的半径相对容差
GROUP_R_REL_TOL = 0.002

#: 每段的镜像建议阈值：该段占总面积 ≥ 此值才值得「建一半 + 镜像」
#: （太小的不如直接建全 —— 镜像特征本身有成本）
MIRROR_MIN_AREA_FRAC = 0.04


def _unit(v):
    n = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
    return [float(x) / n for x in v]


def _dot(a, b) -> float:
    return sum(float(x) * float(y) for x, y in zip(a, b))


# ================================================================ 纯函数层

def dominant_axis(vectors, cos_tol: float = 0.9) -> int | None:
    """一堆法向/轴向里"大家都指向"的那根轴（0/1/2）；不统一回 None。"""
    idx = None
    for v in vectors:
        if not v:
            return None
        u = _unit(v)
        k = max(range(3), key=lambda i: abs(u[i]))
        if abs(u[k]) < cos_tol:
            return None                     # 斜的 ⇒ 没有统一轴
        if idx is None:
            idx = k
        elif idx != k:
            return None
    return idx


def axis_label(idx) -> str:
    return "XYZ"[idx] if idx in (0, 1, 2) else "?"


def _plane_station_mm(f: dict) -> float:
    """平面到**主对称面**的绝对站位（mm）。

    ★ 优先用 `GetBox` 的**中点**：两片平行面各自的包围盒中心在法向上的投影
      就是站位，**与法向符号无关**。
      退化时才用 `abs(n·center)`。
    ★★ 不用 `d`、也不直接用 `n·center`：
        · `d = n·c` 里 n 反号时 d 也反号 ⇒ 相对的两片 d **同值**
          ⇒ `abs(d1−d2) = 0` ⇒ 键宽算成 0（真机就是这个坑，§97① 同源）；
        · `center` 是 `GetClosestPointOn(0,0,0)` 的结果，**落在这片面上但不在
          对称位置**（真机键槽两片的 center 都在 y≈+15.5，因为都从同一个
          原点出发做最近点投影）⇒ 用它算站位也是错的。
      **包围盒是唯一不依赖"取了哪个点"的几何量。**
    """
    b = f.get("box")
    if b and len(b) == 6:
        n = _unit(f.get("normal") or [0, 0, 0])
        mid = [(float(b[i]) + float(b[i + 3])) / 2.0 for i in range(3)]
        return abs(_dot(n, mid)) * 1000.0
    if f.get("center") and f.get("normal"):
        return abs(_dot(_unit(f["normal"]), f["center"])) * 1000.0
    return abs(float(f.get("d") or 0.0)) * 1000.0


def _plane_station_mm(f: dict) -> float:
    """平面到**对称面**的绝对站位（mm）—— 沿**该面法向**投影包围盒中点。

    ★★★ 一定要沿**法向**投影，不能"沿 box 三维里最厚的那一维"：
      键槽侧壁是**零厚度**的平面（真机 Face170 在 x=+4.779 那一**片**，
      box 三个方向里 x 跨度 = 0，而 y 跨度 = 4.4）
      ⇒ "最厚维"挑中 y ⇒ 拿到 y 的中点 15.544，两片**都**是 15.544
      ⇒ 键宽算成 0 ⇒ 键槽永远认不出来。
      （本轮为此绕了几轮：先改用 `d`、再改用 `center`、再改"和/差"，
       全是错的方向 —— 真因是"沿哪个轴算"。）
    ★ 沿法向投影还有个好处：**与法向符号无关**（取绝对值），
      所以相对的两片 d 同值那个坑（§97①）也一并绕开。
    """
    b = f.get("box")
    n = _unit(f.get("normal") or [0, 0, 0])
    if b and len(b) == 6:
        mid = [(float(b[i]) + float(b[i + 3])) / 2.0 for i in range(3)]
        return abs(_dot(n, mid)) * 1000.0
    if f.get("center"):
        return abs(_dot(n, f["center"])) * 1000.0
    return abs(float(f.get("d") or 0.0)) * 1000.0


def _group_by_radius(cyls, rel_tol: float = GROUP_R_REL_TOL):
    """把圆柱面按半径**聚成组**（成组 ⇒ 阵列特征，如齿/孔）。"""
    groups: list[dict] = []
    for f in cyls:
        r = float(f.get("radius") or 0.0)
        hit = next((g for g in groups
                    if abs(g["radius"] - r) <= max(rel_tol * r, 1e-6)), None)
        if hit is None:
            groups.append({"radius": r, "faces": [f], "area": float(f["area"])})
        else:
            hit["faces"].append(f)
            hit["area"] += float(f["area"])
    groups.sort(key=lambda g: g["radius"])
    return groups


def segment(faces: list[dict], box=None) -> dict:
    """把面清单切成**功能段**。返回 `{"segments": [...], "axis": k, "notes": [...]}`。

    ★ `axis` = 全件的主轴（腹板法向 / 中心孔轴 / 齿轴一致的那根）。
      所有段都相对它描述，这样"该选哪个基准面"才有意义。
    """
    notes: list[str] = []
    planes = [f for f in faces if f.get("type") == "plane"]
    cyls = [f for f in faces if f.get("type") == "cylinder"]
    others = [f for f in faces if f.get("type") in ("other", "cone", "sphere",
                                                     "torus")]

    # ---- ① 主轴：**按圆柱面积投票** ----
    # ★★ 不能取"半径最小的圆柱面"当主轴依据：真机 `1-箱座` 88 个圆柱面里
    #   最小的是 **Ø2.5 油标孔**（一个小孔），它的轴与箱座主轴**垂直**。
    #   第一版靠蒙对的（那件恰好轴向一致），换个件就错。
    # ⇒ 改按「**总圆柱面积最大**的那根轴」投票：主轴上的孔/沉孔/轴承座孔
    #   面积最大，这是几何事实，不依赖"哪个孔最小"这种巧合。
    axis = None
    if cyls:
        votes: dict = {}
        for f in cyls:
            a = f.get("axis")
            if not a:
                continue
            u = _unit(a)
            k = max(range(3), key=lambda i: abs(u[i]))
            if abs(u[k]) < 0.9:            # 斜的轴不投票
                continue
            v = votes.setdefault(k, 0.0)
            votes[k] += float(f.get("area") or 0.0)
        if votes:
            axis = max(votes, key=lambda k: votes[k])
    if axis is None and planes:
        axis = dominant_axis([f.get("normal") for f in planes])
    if axis is None:
        notes.append("★ 读不出统一主轴（面法向很杂）⇒ 无法按主轴分段")
        return {"segments": [], "axis": None, "notes": notes}

    main = [1.0 if i == axis else 0.0 for i in range(3)]

    # 垂直于主轴的平面 = 端面族（腹板、凸台端面都在这一族里）
    perp_planes = [f for f in planes
                   if abs(_dot(_unit(f["normal"]), main)) >= 0.9]

    # ---- ② 腹板 = 垂直于主轴的**最大**平面 ----
    web = None
    if perp_planes:
        web = max(perp_planes, key=lambda f: float(f.get("area") or 0.0))
    total_plane_area = sum(float(f.get("area") or 0.0) for f in perp_planes) or 1.0
    web_frac = (float(web["area"]) / total_plane_area) if web else 0.0

    # ---- ③ 轮缘：成组的小面，半径接近外径 ----
    # ★ 「外径」是**垂直于主轴那个横截面上的最大半径**。
    #   真机大齿轮：主轴 = Z，齿顶 Ø114 落在 **XY** 平面上（z ∈ [−13,13]），
    #   而 `box[2]/box[5]` 给出的是 z 向半厚 13mm —— 拿它当外径会把 114 判成 13，
    #   于是"半径 ≥ 85% 外径"永远不成立，轮缘认不出来。
    #   ⇒ 必须取**垂直于主轴那两个方向**的最大绝对值。
    outer = 0.0
    if box and len(box) == 6:
        lat = [i for i in range(3) if i != axis]
        outer = max(max(abs(float(box[i])), abs(float(box[i + 3])))
                    for i in lat)
    # ★ 轮缘 = **所有**满足阈值的半径组，**不是第一组**。
    #   真机大齿轮有**两组**：R57（齿顶，55 个）与 R52.5（齿根/另一侧，55 个），
    #   两组半径都 ≥ 0.85×外径。第一版 `break` 掉了第二组 ⇒ 它掉进
    #   「介于中心孔与齿圈之间且成组」的减重槽判据 ⇒ **55 个齿被误报成减重槽**。
    #   ⇒ 轮缘要收全部组；pocket 判据再显式排除「已被收进轮缘的半径」。
    cyl_groups = _group_by_radius(cyls)
    rim_groups = [g for g in cyl_groups
                  if len(g["faces"]) >= RIM_MIN_FACES and outer > 0
                  and g["radius"] >= RIM_OUTER_FRAC * outer]
    rim = None
    if rim_groups:
        rim = {"radius": max(g["radius"] for g in rim_groups),
               "faces": [f for g in rim_groups for f in g["faces"]],
               "area": sum(g["area"] for g in rim_groups),
               "n_groups": len(rim_groups),
               "radii_mm": [round(g["radius"] * 1000, 4) for g in rim_groups]}
    if rim is None and len(others) >= RIM_MIN_FACES and others:
        # 渐开线齿廓：`other` 面成群且面积几乎相等（真机 110 个全是 123.5mm²）
        areas = [round(float(f["area"]) or 0.0, 9) for f in others]
        if max(areas) - min(areas) < 1e-6 * max(max(areas), 1.0) + 1e-9:
            rim = {"radius": None, "faces": list(others),
                   "area": sum(float(f["area"]) for f in others),
                   "by": "等面积小面成群（渐开线齿廓）"}

    # ---- ④ 中心孔 = **轴 ∥ 主轴**的圆柱组里半径最小的那组 ----
    #   ★ 不取"全体最小半径"：真机箱座的 Ø2.5 油标孔轴**垂直**主轴，
    #     它不是中心孔。
    main_cyls = [g for g in cyl_groups
                 if g["faces"][0].get("axis")
                 and abs(_dot(_unit(g["faces"][0]["axis"]), main)) >= 0.9]
    bore = main_cyls[0] if main_cyls else None

    # ---- ⑤ 减重槽 = 半径**介于孔与外径之间**、且**成组**的圆柱 ----
    r_lo = float(bore["radius"]) if bore else 0.0
    r_hi = float(rim["radius"]) if (rim and rim.get("radius")) else 0.0
    rim_radii = {round(g["radius"] * 1e6) for g in rim_groups}
    pockets = []
    for g in cyl_groups:
        if g is bore:
            continue
        if round(g["radius"] * 1e6) in rim_radii:      # 已在轮缘里
            continue
        if r_lo < g["radius"] < r_hi and len(g["faces"]) >= POCKET_MIN_COUNT:
            pockets.append(g)

    # ---- ⑤' ★ 减重**腔**（腹盘式齿轮）：数量 1~3 的中等半径圆柱 ----
    # ★★ 为什么必须和 `pocket` 分开：pocket 要求「成组 ≥4」是为了认**孔阵列**；
    #   腹盘式齿轮的减重是 **1~2 个大直径环形腔**（真机 R24 = 2 个）⇒ 成组数
    #   不足 ⇒ 第一版明写「没有减重槽」—— **那是假的**，减重确实存在。
    #   ⇒ 「报『没有』」也必须有依据：单一判据的阈值不成立时要换判据再问一次。
    rels = []
    for g in cyl_groups:
        if g is bore or round(g["radius"] * 1e6) in rim_radii:
            continue
        if not (r_lo < g["radius"] < r_hi):
            continue
        if len(g["faces"]) > POCKET_MAX_FACES:          # 数量多 ⇒ 是孔阵列
            continue
        # 轴向跨度：腔壁通常贯穿整个轮宽，而腹板只占中间一小段
        zs = []
        for f in g["faces"]:
            b = f.get("box")
            if b and len(b) == 6:
                zs.append((float(b[axis]) * 1000.0, float(b[axis + 3]) * 1000.0))
        span = max((hi - lo for lo, hi in zs), default=0.0)
        if span <= 0:
            continue
        rels.append({"group": g, "span_mm": round(span, 3),
                     "n": len(g["faces"])})
    if rels and not pockets:
        notes.append(
            "★ 检出**减重腔**（腹盘式齿轮的减重形态）：数量 1~{} 个、"
            "轴向跨度 {:.1f}~{:.1f}mm ⇒ 是**环形腔**不是孔阵列"
            "（★ 只按「成组 ≥{}」会报成『没有减重』—— 那是**假的**）".format(
                POCKET_MAX_FACES, min(r["span_mm"] for r in rels),
                max(r["span_mm"] for r in rels), POCKET_MIN_COUNT))

    # ---- ⑩ ★★ 「一个圆柱面都没有」要**显式告警**（真机 CSWA 教训，坑表 §102）----
    #   agent 建的 CSWA 14 个面**全是平面** ⇒ 2 个 Ø6 法向孔整个丢失，
    #   而 `sw_symmetry` 还报「100% 对称件」—— **判据全绿而结果全错**。
    #   ⇒ 凡是「特征可能漏了」的地方都要有一条**可见的告警**。
    n_cyl = len([f for f in faces if f.get("type") == "cylinder"])
    if n_cyl == 0:
        notes.append(
            "★★ **本件 0 个圆柱面** —— 若图纸上有孔/圆角/圆柱凸台，"
            "说明**特征没建全**。★ 真实的教训（坑表 §102）：agent 建的 CSWA "
            "14 个面全是平面、漏了 2 个 Ø6 法向孔，而对称性判据仍报"
            "「100% 对称」—— **判据全绿 ≠ 结论正确**，"
            "「特征可能漏」必须有可见告警。")
    if rels and n_cyl:
        pass

    if not pockets and not rels:
        notes.append(
            "★ **没有减重槽/腹板孔，也没有减重腔**：半径谱里除了中心孔与"
            "齿圈，既没有**成组**（≥{} 个同半径）的孔，也没有数量 ≤{} 个的"
            "中等半径环形腔".format(POCKET_MIN_COUNT, POCKET_MAX_FACES))

    # ---- ⑥ 键槽 = **成对**的平行窄面（槽壁），间隙 = 键宽 ----
    # ★★ 不能用「面本身细长比」当判据：键槽的槽壁是**零厚度**的平面
    #   （真机 Face170 在 x=+4.779 那一**片**，bbox 三个方向里两个都是 0）
    #   ⇒ 细长比恒为 0 ⇒ 永远认不出来。
    #   正确口径：**成对**（同法向轴 + 面积相等 + 站位关于主轴对称轴成对）
    #   + 站位差落在键宽量级（5~20mm）。
    keyways = []
    side_planes = [f for f in planes
                   if abs(_dot(_unit(f["normal"]),
                              [1.0 if i == axis else 0.0
                               for i in range(3)])) < 0.9]
    for i, f1 in enumerate(side_planes):
        for f2 in side_planes[i + 1:]:
            n1, n2 = _unit(f1["normal"]), _unit(f2["normal"])
            if abs(_dot(n1, n2)) < 0.9:                 # 必须是平行的一对
                continue
            a1, a2 = float(f1.get("area") or 0), float(f2.get("area") or 0)
            if max(a1, a2) <= 0 or abs(a1 - a2) / max(a1, a2) > 0.02:
                continue                               # 面积要相等
            # ★★ 必须用**绝对站位** |n·c|，不能用 d1−d2：
            #   真机 `Key+` 在 x=+5、`Key-` 在 x=−5，两片法向**相反**
            #   （+X / −X）⇒ `d = n·c` 里 n 反号、d 也反号 ⇒ 两片 d **都是 +5.0**
            #   ⇒ `abs(d1−d2) = 0` ⇒ 键宽算成 0 ⇒ 键槽永远认不出来。
            #   这与 `sw_faceset` 里「`PlaneParams.d` 符号约定逐面不一致」同源
            #   （坑表 §97①）。⇒ 站位一律取 `abs(n·c)`。
            # ★ 键宽 = 两片到**主轴所在平面**的距离，按「符号」决定和还是差：
            #   · 两片在主轴**异侧**（键槽夹在中心面两侧）⇒ 键宽 = 和
            #   · 两片在主轴**同侧**（键槽是单侧开口，如真机 33-齿轮）⇒ 键宽 = 差
            #   —— 真机 `33-齿轮`：槽壁在 x=+4.779 与 x=−5.221，两片法向 +X/−X
            #      ⇒ 站位**异号**，但都在"半径 5"这一圈上 ⇒ 键宽 = 4.779+5.221
            #        = **10.000mm**（键 10×8 逐位对上）。
            #   ★ 只写"成对"是不够的：还要知道它们是同侧还是异侧，
            #     否则 x=±4.779（对称件那种）会被算成 0.442 的键宽。
            # ★ 键宽 = 两个槽壁到**对称面**的距离**之和**。
            #   真机 33-齿轮：x=+4.779 与 x=−5.221 ⇒ 4.779 + 5.221 = **10.000mm**
            #   （键 10×8 逐位对上）。两片是**异侧**的（键槽单侧开口），
            #   但站位一律取绝对值 ⇒ 不管同侧异侧，"和"都是键宽。
            gap = _plane_station_mm(f1) + _plane_station_mm(f2)
            if not (5.0 <= gap <= 16.0):               # 键宽量级
                continue
            # ★ 键槽一定**贴着轴**（键是装在轴上的）⇒ 两片到主轴的距离都很小。
            #   真机箱座上「间隙 18.0mm」那对是**轴承座孔壁**（Ø47/Ø40），
            #   不是键槽 —— 它们离主轴 9mm 以外。
            if max(_plane_station_mm(f1), _plane_station_mm(f2)) > 12.0:
                continue
            pair = {f1["id"], f2["id"]}
            if not any(pair == {x["id"] for x in kv} for kv in keyways):
                keyways.append([f1, f2])
    keyway_faces = [f for pair in keyways for f in pair]
    keyway_gap = None
    if keyways:
        keyway_gap = round(min(_plane_station_mm(p[0])
                               + _plane_station_mm(p[1])
                               for p in keyways), 4)

    # ---- ⑦ 组装成段 ----
    segs: list[dict] = []
    tot_area = sum(float(f.get("area") or 0.0) for f in faces) or 1.0

    def add(kind, name, flist, why, base, mirror, ops, extra=None):
        a = sum(float(f.get("area") or 0.0) for f in flist)
        rec = {"kind": kind, "name": name,
               "n_faces": len(flist),
               "face_ids": [f.get("id") for f in flist],
               "area_mm2": round(a * 1e6, 3),
               "area_frac": round(a / tot_area, 4),
               "why": why, "base_plane": base, "mirror": mirror,
               "suggest_ops": ops}
        if extra:
            rec.update(extra)
        segs.append(rec)
        return rec

    if rim:
        add("rim", "轮缘/齿",
            list(rim["faces"]),
            "半径接近外径且**成组**出现（{} 个面 / {} 个半径组 {}）⇒ 齿形是阵列特征"
            .format(len(rim["faces"]), rim.get("n_groups", 1),
                    rim.get("radii_mm"))
            + ("（" + rim["by"] + "）" if rim.get("by") else ""),
            "垂直于主轴的基准面（草图平面）",
            False,                      # ★ 齿本身是整圈，镜像没意义
            ["gear_profile（一整圈齿廓）", "extrude_boss / extrude_cut"],
            {"rim_radius_mm": (None if rim.get("radius") is None
                               else round(rim["radius"] * 1000, 4)),
             "n_teeth": len(rim["faces"])})
    if web:
        add("web", "腹板/盘", [web],
            "垂直于主轴的**最大**平面（占端面族总面积 {:.1f}%）".format(
                web_frac * 100),
            "垂直于主轴的基准面",
            None,                       # 由对称性决定，调用方回填
            ["circle / polyline（外-内轮廓）", "extrude_boss",
             "mirror（若对称）"],
            {"web_thickness_mm": None, "station_mm": round(
                float(web["d"]) * 1000, 4)})
    for i, g in enumerate(cyl_groups):
        if g is bore or g is rim or any(g is p for p in pockets):
            continue
        if len(g["faces"]) == 1:
            add("hub", "轮毂/凸台 #{:d}".format(i + 1), list(g["faces"]),
                "单个小圆柱面（半径 {:.3f}mm）⇒ 局部凸台/台阶".format(
                    g["radius"] * 1000),
                "垂直于主轴的基准面", None, ["circle", "extrude_boss"],
                {"radius_mm": round(g["radius"] * 1000, 4)})
    for i, g in enumerate(pockets):
        add("pocket", "减重槽 #{:d}".format(i + 1), list(g["faces"]),
            "半径介于中心孔({:.3f})与齿圈之间且**成组**（{} 个）⇒ 腹板孔"
            "阵列".format(r_lo * 1000, len(g["faces"])),
            "垂直于主轴的基准面（腹板所在面）",
            True,                      # ★ 两侧对称 ⇒ 切一半 + 镜像
            ["circle × 1", "extrude_cut（切穿）", "circular_pattern",
             "mirror（另一半）"],
            {"radius_mm": round(g["radius"] * 1000, 4), "n": len(g["faces"])})
    for i, r in enumerate(rels, 1):
        g = r["group"]
        add("relief", "减重腔 #{:d}".format(i), list(g["faces"]),
            "半径 {:.3f}mm（介于中心孔 {:.3f} 与齿圈之间）、数量 {} 个、"
            "轴向跨度 {:.1f}mm ⇒ **环形减重腔**（腹盘式齿轮的减重形态，"
            "不是孔阵列）".format(g["radius"] * 1000, r_lo * 1000, r["n"],
                                  r["span_mm"]),
            "垂直于主轴的基准面（腹盘所在面）",
            True,                      # ★ 两侧对称 ⇒ 切一半 + 镜像
            ["circle（腔轮廓）", "extrude_cut（切穿）", "mirror（另一半）"],
            {"radius_mm": round(g["radius"] * 1000, 4), "n": r["n"],
             "axial_span_mm": r["span_mm"]})
    if keyway_faces:
        add("keyway", "键槽", keyway_faces,
            "**成对**平行窄面、面积相等、**贴着主轴**、间隙 {:.1f}mm ⇒ "
            "平键槽的槽壁（★ 不能用「面细长比」判：槽壁是**零厚度**的平面）"
            .format(keyway_gap or 0.0),
            "垂直于主轴的基准面（端面）", False,
            ["keyway_rect（专用 op，已含槽底圆角）", "extrude_cut"],
            {"n_faces": len(keyway_faces), "key_width_mm": keyway_gap})
    # ---- ⑨ 壁板：垂直于主轴的平面里，**不是**腹板/端面凸台的那些 ----
    #   ★ 没有这一段，箱座 201 个面只归了 3% 的面积，剩下的箱壁/筋全漏 ——
    #     而报告看着"分好了"。覆盖率是「有没有漏」的可见指标。
    web_id = web.get("id") if web else None
    hub_ids = {f.get("id") for f in faces
               if f.get("type") == "cylinder"
               and abs(_dot(_unit(f.get("axis") or [0, 0, 0]), main)) >= 0.9}
    shell_faces = [f for f in perp_planes
                   if f.get("id") != web_id
                   and abs(_dot(_unit(f["normal"]), main)) < 0.9]
    # 侧壁 = 法向 ⊥ 主轴的平面
    side_planes_all = [f for f in planes
                       if abs(_dot(_unit(f["normal"]), main)) < 0.9]
    if side_planes_all:
        add("shell", "箱壁/侧壁", side_planes_all,
            "法向**垂直于主轴**的平面（{} 张）⇒ 箱体外壁/隔壁/筋的端面。"
            "★ 这一段通常是**面积最大**的部分，没有它报告就只覆盖了几个百分点"
            .format(len(side_planes_all)),
            "垂直于主轴的基准面（建壁）", None,
            ["polyline（壁轮廓）", "extrude_boss / extrude_cut",
             "rib（筋）", "shell（抽壳，若做箱体）"],
            {"n_side_planes": len(side_planes_all)})

    if bore:
        add("bore", "中心孔", list(bore["faces"]),
            "半径最小的圆柱面（Ø{:.3f}mm）⇒ 中心孔".format(
                bore["radius"] * 2000),
            "垂直于主轴的基准面", False, ["circle", "extrude_cut"],
            {"radius_mm": round(bore["radius"] * 1000, 4),
             "diameter_mm": round(bore["radius"] * 2000, 4)})

    # ---- ⑧ 用对称性回填「腹板要不要镜像」 ----
    sym_axis = None
    if box and len(box) == 6:
        syms = sw_symmetry.find_symmetry_planes(faces, box)
        best = next((r for r in syms if r["is_symmetric"]), None)
        if best:
            sym_axis = best["axis"]
            web_rec = next((s for s in segs if s["kind"] == "web"), None)
            if web_rec is not None:
                web_rec["mirror"] = True
                web_rec["why"] += ("；全件关于 {} 对称 {:.1f}% ⇒ 腹板**建一半 + "
                                   "mirror**".format(best["axis"],
                                                     best["symmetric"] * 100))
                web_rec["symmetry_plane_axis"] = best["axis"]
                web_rec["symmetry_plane_mm"] = best["offset_mm"]
    for rec in segs:
        if rec["mirror"] is None:
            rec["mirror"] = (rec["area_frac"] >= MIRROR_MIN_AREA_FRAC
                             and sym_axis is not None)
            if rec["mirror"] and sym_axis is None:
                rec["why"] += "（本件未检出对称面 ⇒ **不要镜像**）"
    segs.sort(key=lambda r: -r["area_mm2"])
    covered = sum(float(f.get("area") or 0.0)
                  for f in faces
                  if f.get("id") in {i for r in segs for i in r["face_ids"]})
    cov = covered / tot_area if tot_area else 0.0
    if cov < 0.5:
        notes.append(
            "★ **只归类了 {:.1f}% 的面积** —— 曲面（`other`/锥面/球面/环面）"
            "与斜面通常不归段。覆盖低**不代表工具坏了**，但要知道自己漏了什么"
            "：本件还有 {:.0f}% 的面积（主要是曲面）没进任何段".format(
                cov * 100, (1 - cov) * 100))
    return {"coverage": cov, "segments": segs, "axis": axis,
            "axis_label": axis_label(axis), "symmetry_axis": sym_axis,
            "notes": notes}


def build_order(seg: dict) -> list[str]:
    """给**建模顺序**的建议（依赖关系，不是面积顺序）。

    ★ 真实顺序：中心孔/键槽（内特征先定位置）→ 腹板（主体）→ 轮毂 → 减重槽
      → 轮缘（齿，最后成形，因为它要从已存在的外圆上切/拉）。
    ⇒ 面积大的先画是**错的**：齿占面积最大，但它依赖主体已经存在。
    """
    # 依赖顺序：中心孔/键槽（内特征先定位置）→ **箱壁**（主体轮廓）
    # → 腹板（壁上的板）→ 轮毂 → 减重槽 → 轮缘（最后，它要从已存在的外圆切/拉）
    order = {"bore": 10, "keyway": 20, "shell": 30, "web": 35, "hub": 40,
             "relief": 45, "pocket": 50, "rim": 60}
    return [s["kind"] for s in sorted(seg["segments"],
                                       key=lambda r: order.get(r["kind"], 99))]


def emit_plan(seg: dict, doc_out: str) -> dict:
    """把分段结论写成一个**人可读**的建模顺序说明（不生成可执行步骤）。

    ★ 刻意**不生成 `yoyo.swplan`**：每段用什么 op、基准面偏移多少，
      取决于还没定的尺寸参数。给一份"带说明的骨架"，
      由人或 agent 填参数后再 `sw_plan --check`。
    """
    plan = {
        "yoyo.swplan": 1,
        "kind": "segment_skeleton",
        "note": "这是**分段骨架**，不是可执行计划：每段还缺尺寸参数。"
                "填好后用 `python scripts/sw_plan.py <file> --check` 校验。",
        "axis": seg.get("axis_label"),
        "symmetry_axis": seg.get("symmetry_axis"),
        "steps": [],
    }
    for i, kind in enumerate(build_order(seg), 1):
        rec = next((s for s in seg["segments"] if s["kind"] == kind), None)
        if rec is None:
            continue
        plan["steps"].append({
            "id": "s%d" % i,
            "segment": rec["name"],
            "kind": rec["kind"],
            "base_plane": rec["base_plane"],
            "mirror": rec["mirror"],
            "suggest_ops": rec["suggest_ops"],
            "why": rec["why"],
            "params_needed": "TODO：这一段的尺寸参数（轮廓尺寸 / 深度 / "
                             "阵列个数…）",
        })
    Path(doc_out).write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return plan


# ================================================================ 报告

def print_report(faces: list[dict], box, vol, seg: dict) -> None:
    s = sw_faceset.summarise(faces)
    print("面 {} 个   类型分布 {}".format(s["n_faces"], s["by_type"]))
    if vol:
        print("体积 = {:.1f} mm³".format(vol))
    print("整体包围盒(mm) = {}".format(s["box_mm"]))
    print("主轴 = {}   对称轴 = {}".format(seg.get("axis_label"),
                                          seg.get("symmetry_axis")))
    for n in seg.get("notes") or []:
        print("  {}".format(n))

    print("归类覆盖率 = {:.1f}%（余下是曲面/斜面，通常不进段）".format(
        (seg.get("coverage") or 0.0) * 100))
    print("\n== 功能分段（按面积降序）==")
    for i, r in enumerate(seg["segments"], 1):
        mir = {True: "建一半 + 镜像", False: "整件建",
               None: "未判定"}[r["mirror"]]
        print("  [{:d}] {:<12} ({:>9}) 面 {:>3} 个  面积 {:>9.1f}mm² "
              "占比 {:>5.1f}%  {}".format(
                  i, r["name"], r["kind"], r["n_faces"], r["area_mm2"],
                  r["area_frac"] * 100, mir))
        print("        判据：{}".format(r["why"]))
        print("        基准面：{}".format(r["base_plane"]))
        print("        建议 op：{}".format(" → ".join(r["suggest_ops"])))
    print("\n== 建议建模顺序（按依赖，不是按面积）==")
    print("  " + "  →  ".join(build_order(seg)))


# ================================================================ 自检

def self_test() -> int:
    n_ok = n_bad = 0

    def chk(cond, msg):
        nonlocal n_ok, n_bad
        if cond:
            n_ok += 1
            print("  PASS  {}".format(msg))
        else:
            n_bad += 1
            print("  FAIL  {}".format(msg))

    def PL(n, area, name, d, c, box=None):
        n = _unit(n)
        return {"id": name, "type": "plane", "area": area * 1e-6,
                "center": list(c), "normal": n,
                "d": _dot(n, c), "box": list(box) if box else None}

    def CY(ax, r, area, name, c):
        return {"id": name, "type": "cylinder", "area": area * 1e-6,
                "center": list(c), "axis": _unit(ax), "radius": r,
                "normal": None, "d": None, "box": None}

    print("[1] 主轴判定")
    chk(dominant_axis([[0, 0, 1], [0, 0, -1]]) == 2,
        "±Z 都算同一根轴")
    chk(dominant_axis([[0, 0, 1], [1, 0, 0]]) is None,
        "★ 两根不同的轴 ⇒ 没有统一主轴（不能按主轴分段）")
    chk(dominant_axis([[math.cos(0.3), math.sin(0.3), 0]]) == 0,
        "★ 17° 的斜轴**仍算** X 轴（方向余弦 0.955 ≥ 0.9）—— 箱体加强筋常带"
        "小角度，不能因为「斜了一点」就丢掉主轴")
    chk(dominant_axis([[math.cos(0.5), math.sin(0.5), 0]]) is None,
        "★ 30° 的斜轴**不算**（余弦 0.878 < 0.9）⇒ 真的没有主轴，"
        "这时**返回空段**而不是瞎分")

    print("[2] ★ 半径聚类 = 阵列证据")
    gs = _group_by_radius([CY([0, 0, 1], 0.0525, 40, "t%d" % i, [0, 0, 0.01])
                           for i in range(55)])
    chk(len(gs) == 1 and len(gs[0]["faces"]) == 55,
        "55 个同半径圆柱面聚成**一组**（实得 {} 组 / {} 个）".format(
            len(gs), len(gs[0]["faces"])))
    gs2 = _group_by_radius([CY([0, 0, 1], 0.016, 1988, "bore", [0, 0, 0]),
                            CY([0, 0, 1], 0.0525, 40, "t1", [0, 0, 0])])
    chk(len(gs2) == 2, "不同半径分成两组")

    # ---- 造一个"简化大齿轮"：腹板 + 双侧凸台 + 中心孔 + 键槽 + 55 个齿 ----
    print("[3] 简化大齿轮的分段")
    gear: list = []
    # 腹板：z=±4.5，Ø94 圆 → 面积 ≈ π·47² = 6940
    gear.append(PL([0, 0, 1], 6940, "Web+", 0.0045, [0, 0, 0.0045],
                   [-0.047, -0.047, 0.0045, 0.047, 0.047, 0.0045]))
    gear.append(PL([0, 0, -1], 6940, "Web-", 0.0045, [0, 0, -0.0045],
                   [-0.047, -0.047, -0.0045, 0.047, 0.047, -0.0045]))
    # 端面凸台：z=±13，Ø32 区域（面积小）
    gear.append(PL([0, 0, 1], 800, "HubTop", 0.013, [0, 0, 0.013],
                   [-0.016, -0.016, 0.013, 0.016, 0.016, 0.013]))
    gear.append(PL([0, 0, 1], 800, "HubBot", -0.013, [0, 0, -0.013],
                   [-0.016, -0.016, -0.013, 0.016, 0.016, -0.013]))
    # 中心孔 Ø32
    gear.append(CY([0, 0, 1], 0.016, 1988, "Bore", [0, 0, 0]))
    # 齿：55 个 R52.5 圆柱面（齿顶）
    for i in range(55):
        a = 2 * math.pi * i / 55
        gear.append(CY([0, 0, 1], 0.0525, 40, "T%d" % i,
                       [0.0525 * math.cos(a), 0.0525 * math.sin(a), 0.0]))
    # ★ 键槽：**单侧**，照真机 `33-齿轮` 的实测值 —— 键 10×8
    #   Face170 x=+4.779（法向 +X）/ Face172 x=−5.221（法向 −X）/ Face173 槽底
    #   ⇒ 键宽 = 4.779 + 5.221 = **10.000mm**，槽深到 y=17.745。
    #   ★★ 第一版把它写成 x=±5（关于中面对称）⇒ 站位差 = 0 ⇒ 键宽算成 0
    #   ⇒ 键槽认不出来。**夹具错了，不是判据错了** —— 键槽本来就不对称。
    gear.append(PL([1, 0, 0], 110, "Key+", 0, [0.004779, 0.0155, 0.0],
                   [0.004779, 0.013343, -0.013, 0.004779, 0.017745, 0.013]))
    gear.append(PL([-1, 0, 0], 110, "Key-", 0, [-0.005221, 0.0155, 0.0],
                   [-0.005221, 0.013343, -0.013, -0.005221, 0.017745, 0.013]))
    gear.append(PL([0, 1, 0], 260, "KeyFloor", 0, [0.0, 0.017745, 0.0],
                   [-0.005221, 0.017745, -0.013, 0.004779, 0.017745, 0.013]))

    box = [-0.057, -0.057, -0.013, 0.057, 0.057, 0.013]
    seg = segment(gear, box)
    sw_segment_segment = segment   # 便于断言里复用
    kinds = [r["kind"] for r in seg["segments"]]
    chk("web" in kinds, "认出**腹板**（实得 kinds={}）".format(kinds))
    chk("bore" in kinds, "认出**中心孔**")
    chk("rim" in kinds, "认出**轮缘/齿**（55 个成组小圆柱面）")
    chk("keyway" in kinds, "认出**键槽**（窄长平面）")
    chk("pocket" not in kinds,
        "★★ **没有减重槽时必须报出来**，不能沉默（真机 33-齿轮 就是这样："
        "半径谱里只有中心孔/凸台/齿，没有成组的腹板孔）")
    chk(any("没有减重槽" in n for n in seg["notes"]),
        "★ 报告里明写「没有减重槽」—— 让人知道不是漏判而是真的没有")
    web_rec = next((r for r in seg["segments"] if r["kind"] == "web"), None)
    chk(web_rec is not None and web_rec["mirror"] is True,
        "★ 腹板**建一半 + 镜像**（本夹具左右对称，工具应检出对称轴）")
    rim_rec = next((r for r in seg["segments"] if r["kind"] == "rim"), None)
    chk(rim_rec is not None and rim_rec["mirror"] is False,
        "★ 轮缘**不镜像**（齿本身是整圈，镜像没意义）")

    print("[4] ★ 建模顺序按**依赖**，不是按面积")
    order = build_order(seg)
    chk(order.index("bore") < order.index("web"),
        "中心孔先于腹板（孔的位置要先定）")
    chk(order.index("web") < order.index("rim"),
        "★★ 腹板先于轮缘 —— 齿是**面积最大**的段，但依赖主体已存在。"
        "「按面积从大到小建」是错的")
    chk(order[-1] == "rim", "轮缘最后（实得 {}）".format(order))
    from sw_segment import build_order as _bo        # noqa: PLC0415
    _mixed = {"segments": [{"kind": "rim"}, {"kind": "shell"}, {"kind": "web"},
                           {"kind": "bore"}], "axis": 2, "notes": []}
    _mo = _bo(_mixed)
    chk(_mo.index("shell") < _mo.index("web"),
        "★ 有箱壁时：**箱壁先于腹板**（先有壁，腹板才是壁上的板）"
        "（实得 {}）".format(_mo))

    print("[5] 有减重槽时能不能认出来")
    gear2 = list(gear) + [CY([0, 0, 1], 0.032, 300, "P%d" % i,
                              [0.032 * math.cos(2 * math.pi * i / 6),
                               0.032 * math.sin(2 * math.pi * i / 6), 0.0])
                          for i in range(6)]
    seg2 = segment(gear2, box)
    pk = [r for r in seg2["segments"] if r["kind"] == "pocket"]
    chk(len(pk) == 1 and pk[0]["mirror"] is True,
        "★ 6 个成组中等半径圆柱面 ⇒ 认出**减重槽**且**标为两侧对称**"
        "（切一半 + 镜像）")

    print("[5'] ★★ 减重**腔**（腹盘式齿轮的减重形态，不是孔阵列）")
    # 照真机 `33-齿轮`：R24 减重腔 **2 个**面，z∈[-11,+11]（贯穿轮宽）
    gear2r = list(gear) + [
        CY([0, 0, 1], 0.024, 980, "Cav%d" % i,
           [0.024 * math.cos(a), 0.024 * math.sin(a), 0.0])
        for i, a in enumerate([0.0, math.pi])]
    for f in gear2r:
        if f["id"].startswith("Cav"):
            f["box"] = [-0.024, -0.024, -0.011, 0.024, 0.024, 0.011]
    seg2r = sw_segment_segment(gear2r, box)
    kinds2r = [r["kind"] for r in seg2r["segments"]]
    chk("relief" in kinds2r,
        "★★ 只加 **2 个** Ø48 面（成组数 < {}）也能认出**减重腔**（实得 {}）"
        "—— 真机 33-齿轮就是这么建的".format(POCKET_MIN_COUNT,
                                                kinds2r))
    chk("pocket" not in kinds2r,
        "★ 数量 1~{} ⇒ 判为**腔**而不是**孔阵列**".format(
            POCKET_MAX_FACES))
    rel = next((r for r in seg2r["segments"] if r["kind"] == "relief"), None)
    chk(rel is not None and rel["mirror"] is True,
        "★ 减重腔**两侧对称 ⇒ 切一半 + 镜像**（真机 R24 是 2 个面）")
    chk(rel is not None and abs(rel["radius_mm"] - 24.0) < 1e-6,
        "★ 减重腔半径读出 24.000mm（实得 {}）".format(
            rel.get("radius_mm")))
    chk(any("减重腔" in n for n in seg2r["notes"]),
        "★ 报告明说检出的是**减重腔**而不是孔阵列")

    print("[5''] ★ 「报『没有』」也必须有依据")
    # 只有中心孔 + 齿，中间什么都不该有 ⇒ 此时才该说「没有减重」
    bare = [f for f in gear if not f["id"].startswith("Key")]
    segb = sw_segment_segment(bare, box)
    kb = [r["kind"] for r in segb["segments"]]
    chk("relief" not in kb and "pocket" not in kb
        and any("没有减重" in n for n in segb["notes"]),
        "★★ 只有中心孔+齿时**才**说「没有减重」（实得 {}）—— "
        "上一轮就是在这里**报了假没**".format(kb))

    print("[6] 渐开线齿廓（other 面等面积成群）")
    gear3 = [f for f in gear if f["id"].startswith("T")] + [
        {"id": "I%d" % i, "type": "other", "area": 123.5e-6,
         "center": [0.057 * math.cos(a), 0.057 * math.sin(a), 0.0],
         "normal": None, "d": None, "box": None}
        for i, a in enumerate([2 * math.pi * i / 110 for i in range(110)])]
    seg3 = segment(gear3, box)
    chk(any(r["kind"] == "rim" for r in seg3["segments"]),
        "★ 110 个**等面积**的 other 面（真机 123.5mm²）也认成轮缘"
        "（渐开线齿廓，`IsPlane/IsCylinder/...` 全 false ⇒ 归到 other）")

    print("[7] 读不出主轴时不硬分")
    chk(segment([PL([1, 0, 0], 100, "A", 0.01, [0.01, 0, 0]),
                 PL([0, 1, 0], 100, "B", 0.01, [0, 0.01, 0])], None)["segments"]
        == [], "两件正交的面 ⇒ 主轴读不出 ⇒ **返回空段**而不是瞎分")

    print("\n自检：{} 通过 / {} 失败".format(n_ok, n_bad))
    return 0 if n_bad == 0 else 1


# ================================================================ CLI

def main() -> int:
    ap = argparse.ArgumentParser(
        description="按功能语义分段，并给每段定基准面与「是否镜像」")
    ap.add_argument("--doc", help="零件 .SLDPRT（只读打开）")
    ap.add_argument("--json", help="把结论写到这个 JSON")
    ap.add_argument("--emit-plan", help="写一份**分段骨架**（不是可执行计划）")
    ap.add_argument("--self-test", action="store_true", help="不打开 SolidWorks")
    a = ap.parse_args()

    if a.self_test:
        return self_test()
    if not a.doc:
        ap.print_help()
        return 2

    import sw_connect                                      # noqa: PLC0415
    sw = sw_connect.SwSession.open(visible=False)
    sw.open_doc(a.doc, read_only=True)
    faces = sw_faceset.read_faces(sw)
    if not faces:
        print("读不到任何面：{}".format(a.doc))
        return 1
    box_mm = sw_faceset.summarise(faces).get("box_mm")
    box = [v / 1000.0 for v in box_mm] if box_mm else None
    vol = None
    with contextlib.suppress(Exception):
        mp = sw.mass_props()
        if mp.get("volume_m3") and not mp.get("suspect"):
            vol = float(mp["volume_m3"]) * 1e9
    seg = segment(faces, box)
    print_report(faces, box, vol, seg)

    if a.json:
        out = {"doc": a.doc, "n_faces": len(faces), "volume_mm3": vol,
               "box_mm": box_mm, "main_axis": seg.get("axis_label"),
               "symmetry_axis": seg.get("symmetry_axis"),
               "segments": seg["segments"], "notes": seg["notes"],
               "build_order": build_order(seg)}
        Path(a.json).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        print("\n结论已写入 {}".format(a.json))
    if a.emit_plan:
        emit_plan(seg, a.emit_plan)
        print("分段骨架已写入 {}（**还需填尺寸参数**才能 --check）".format(
            a.emit_plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
