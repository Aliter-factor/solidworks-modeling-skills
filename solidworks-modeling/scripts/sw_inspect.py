#!/usr/bin/env python3
"""零件几何体检 —— 把「脚本说成功、实物是错的」照出来。

为什么需要它
------------
op 的返回值只说明「API 调用没报错」，不说明「形状对」。实测踩过的：
  · 键槽矩形画到了别的象限（`CreateCenterRectangle` 返回 True，包围盒也正常）
  · 切除方向反了，啃掉的是另一侧
  · 草图里混进构造线，轮廓数悄悄从 1 变 2
  · 特征定义根本没生效（以为设了「完全贯穿」，实际还是盲孔 0mm）

这个工具把**真实几何**摊开，不再靠猜：
  · 特征树：类型 / 名称 / 压缩状态 / 定义参数（深度、方向、起止条件）
  · 每张草图：图元数、轮廓数、图元类型分布
  · 每个图元：**构造线标记** + 精确坐标（mm）—— 矩形画哪儿了一眼可见

配合 `sw_shot.py` 的截图，就是完整的自查闭环：
    建模 → inspect（读数）+ shot（看图）→ 不对就换方案 → 再查一遍

用法
----
    python scripts/sw_inspect.py --doc D:\\out\\gear.SLDPRT
    python scripts/sw_inspect.py --doc ... --json D:\\out\\gear_report.json
    python scripts/sw_inspect.py --doc ... --sketch 草图2      # 只看某张草图
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sw_connect import SwError, SwSession, member, typed  # noqa: E402

# swSketchSegments_e
SEG_TYPES = {0: "LINE", 1: "ARC", 2: "ELLIPSE", 3: "SPLINE",
             4: "TEXT", 5: "PARABOLA"}

# 这些特征类型是「有形状的实体特征」，它们的定义参数值得读出来
SHAPE_TYPES = {"Extrusion", "Cut", "Revolution", "CutExtrude", "Boss",
               "Fillet", "Chamfer", "Hole", "SweptCut", "Sweep"}

# swFeatureError_e —— 特征「重建失败」的原因码。
# ★ 非 0 就是错的，而且此时 GetTypeName2() 往往只给一个 **"ICE"**
#   （不是正常的 Cut / Extrusion）。光看 op 的返回值**完全看不出来**：
#   切除调用返回了非 None、特征也确实进了模型树，但实物根本没切透。
#   见 references/30-pitfalls.md「返回值撒谎」。
ERRORS = {
    0: "OK", 1: "未知错误",
    30: "拉伸结果不相交", 31: "找不到终止面", 32: "几何条件不合法",
    33: "切除轮廓既开放又闭合", 34: "切除轮廓非法（自相交/重叠）",
    35: "开放切除轮廓非法",
    36: "凸台轮廓既开放又闭合", 37: "凸台轮廓非法",
    49: "特征已废弃", 50: "特征已过时", 51: "草图外部参考失效",
}


def _m(v) -> float:
    """米 → 毫米，保留 4 位。"""
    return round(float(v) * 1000.0, 4)


def _coords(pt) -> list[float] | None:
    """ISketchPoint → [x, y, z]（**毫米**）。

    ★ 走 X / Y / Z **属性**，不要走 `GetCoords()`：后者是三个 `[out]` 参数的
      方法，晚期绑定下 pywin32 读不回 out 参数、静默返回 None —— 症状就是
      「所有坐标都是 None」，看着像草图空的（其实草图画得好好的），
      会把人往完全错误的方向带。
    """
    if pt is None:
        return None
    try:
        return [_m(member(pt, "X")), _m(member(pt, "Y")), _m(member(pt, "Z"))]
    except Exception:                                        # noqa: BLE001
        return None


def _seg_info(seg) -> dict:
    """一个草图图元的类型 / 构造线标记 / 关键坐标。"""
    out: dict = {}
    with contextlib.suppress(Exception):
        out["type"] = SEG_TYPES.get(int(member(seg, "GetType")), "?")
    with contextlib.suppress(Exception):
        out["construction"] = bool(member(seg, "ConstructionGeometry"))
    with contextlib.suppress(Exception):
        out["length_mm"] = _m(member(seg, "GetLength"))
    t = out.get("type")
    if t in ("LINE", "ARC", "ELLIPSE", "PARABOLA"):
        with contextlib.suppress(Exception):
            out["start"] = _coords(member(seg, "GetStartPoint2"))
        with contextlib.suppress(Exception):
            out["end"] = _coords(member(seg, "GetEndPoint2"))
    if t == "ARC":
        with contextlib.suppress(Exception):
            out["center"] = _coords(member(seg, "GetCenterPoint2"))
        with contextlib.suppress(Exception):
            out["radius_mm"] = _m(member(seg, "GetRadius"))
    if t == "SPLINE":
        with contextlib.suppress(Exception):
            pts = member(seg, "GetPoints2")
            if isinstance(pts, (tuple, list)):
                out["n_points"] = len(pts)
                if pts:
                    out["start"] = _coords(pts[0])
                    out["end"] = _coords(pts[-1])
    return out


def _sketch_info(sw: SwSession, feat) -> dict:
    """一张草图的完整几何。"""
    info: dict = {"name": "", "segments": 0, "contours": None, "items": []}
    with contextlib.suppress(Exception):
        info["name"] = str(member(feat, "Name"))
    sk = None
    for iface in ("ISketch",):
        with contextlib.suppress(Exception):
            sk = typed(member(feat, "GetSpecificFeature2"), iface)
    if sk is None:
        return info
    with contextlib.suppress(Exception):
        info["contours"] = int(member(sk, "GetSketchContourCount"))
    segs = []
    with contextlib.suppress(Exception):
        r = member(sk, "GetSketchSegments")
        if isinstance(r, (tuple, list)):
            segs = list(r)
    info["segments"] = len(segs)
    for s in segs:
        s = typed(s, "ISketchSegment") if s is not None else None
        if s is not None:
            info["items"].append(_seg_info(s))
    return info


def _feature_def(sw: SwSession, feat, type_name: str) -> dict:
    """实体特征的定义参数：深度 / 方向 / 起止条件。"""
    out: dict = {}
    if type_name not in SHAPE_TYPES:
        return out
    d = None
    with contextlib.suppress(Exception):
        d = typed(member(feat, "GetDefinition"), "IExtrudeFeatureData2")
    if d is None:
        return out
    for tag, flag in (("depth1", True), ("depth2", False)):
        with contextlib.suppress(Exception):
            out[tag + "_mm"] = _m(member(d, "GetDepth", flag))
    with contextlib.suppress(Exception):
        out["reverse"] = bool(member(d, "ReverseDirection"))
    for tag, flag in (("end_cond1", True), ("end_cond2", False)):
        with contextlib.suppress(Exception):
            out[tag] = int(member(d, "GetEndCondition", flag))
    return out


def inspect(sw: SwSession, only_sketch: str | None = None) -> dict:
    rep: dict = {"doc": "", "features": [], "sketches": []}
    with contextlib.suppress(Exception):
        rep["doc"] = str(member(sw.model, "GetPathName")) or str(member(sw.model, "GetTitle"))

    f = typed(member(sw.model, "FirstFeature"), "IFeature")
    guard = 0
    while f is not None and guard < 5000:
        guard += 1
        tname = ""
        fname = ""
        with contextlib.suppress(Exception):
            tname = str(member(f, "GetTypeName2"))
        with contextlib.suppress(Exception):
            fname = str(member(f, "Name"))
        row: dict = {"type": tname, "name": fname}
        # ★ GetTypeName2() 对「Instant3D 方式创建」的拉伸/切除会回一个 **"ICE"** ——
        #   那**不是错误状态**（SolidWorks 官方文档明确说明）。API 建出来的
        #   FeatureExtrusion3 / FeatureCut4 经常被打上这个标，几何完全正常。
        #   旧版 GetTypeName() 给的才是具体类型（Boss / Cut / Extrusion…）。
        #   两个都记，免得把好好的零件当成坏的。
        with contextlib.suppress(Exception):
            t_old = str(member(f, "GetTypeName"))
            if t_old and t_old != tname:
                row["type_legacy"] = t_old
        with contextlib.suppress(Exception):
            row["suppressed"] = bool(member(f, "IsSuppressed"))
        with contextlib.suppress(Exception):
            ec = int(member(f, "GetErrorCode"))
            row["error_code"] = ec
            if ec:
                row["error"] = ERRORS.get(ec, f"错误码 {ec}")
        row["def"] = _feature_def(sw, f, row.get("type_legacy") or tname)
        rep["features"].append(row)

        if tname in ("ProfileFeature", "Sketch") and (not only_sketch or fname == only_sketch):
            rep["sketches"].append(_sketch_info(sw, f))
        nxt = None
        with contextlib.suppress(Exception):
            nxt = member(f, "GetNextFeature")
        f = typed(nxt, "IFeature") if nxt is not None else None

    with contextlib.suppress(Exception):
        rep["bbox_mm"] = [_m(v) for v in sw.bounding_box()]
    with contextlib.suppress(Exception):
        mp = sw.mass_props()
        # ★ 键名是 volume_m3 / mass_kg（SI）。写成 volume/mass 会静默拿到 0，
        #   看着像「模型没有实体」，实际是读错了字段。
        rep["volume_mm3"] = round(float(mp.get("volume_m3", 0)) * 1e9, 2)
        rep["mass_g"] = round(float(mp.get("mass_kg", 0)) * 1000.0, 3)
    return rep


def _print(rep: dict, verbose: bool) -> None:
    print(f"文档: {rep['doc']}")
    print("\n特征树:")
    for i, r in enumerate(rep["features"], 1):
        flags = " [压缩]" if r.get("suppressed") else ""
        if r.get("error_code"):
            flags += f" [特征错误 {r['error_code']}: {r.get('error', '?')}]"
        extra = ""
        if r.get("def"):
            d = r["def"]
            bits = [f"{k}={v}" for k, v in d.items() if v not in (None, 0, False)]
            if bits:
                extra = "  " + " ".join(bits)
        tshow = r["type"]
        if r.get("type_legacy"):
            tshow += f"→{r['type_legacy']}"
        print(f"  {i:>2}. {tshow:<22} {r['name']}{flags}{extra}")

    print("\n草图体检:")
    for sk in rep["sketches"]:
        cons = sum(1 for it in sk["items"] if it.get("construction"))
        kinds: dict[str, int] = {}
        for it in sk["items"]:
            k = it.get("type", "?")
            kinds[k] = kinds.get(k, 0) + 1
        print(f"  · {sk['name']}: {sk['segments']} 个图元"
              f"（构造线 {cons}）、轮廓数 {sk['contours']}、类型 {kinds}")
        if not verbose:
            continue
        for j, it in enumerate(sk["items"], 1):
            tag = "构造" if it.get("construction") else "    "
            line = f"      [{j:>3}] {tag} {it.get('type', '?'):<8}"
            if "start" in it:
                line += f" {it['start']} → {it.get('end')}"
            if "center" in it:
                line += f"  圆心={it['center']} R={it.get('radius_mm')}"
            if "n_points" in it:
                line += f"  {it['n_points']} 点"
            if "length_mm" in it:
                line += f"  L={it['length_mm']}"
            print(line)

    if "bbox_mm" in rep:
        print(f"\n包围盒(mm): {rep['bbox_mm']}")
    if "volume_mm3" in rep:
        print(f"体积(mm³): {rep['volume_mm3']}   质量(g): {rep.get('mass_g')}")


def _cli() -> None:
    ap = argparse.ArgumentParser(description="SolidWorks 零件几何体检")
    ap.add_argument("--doc", required=True, help="零件路径")
    ap.add_argument("--sketch", default=None, help="只看这张草图（名字，如 草图2）")
    ap.add_argument("--json", default=None, help="把结果写成 JSON")
    ap.add_argument("--full", action="store_true", help="逐个图元打印坐标（可能很长）")
    a = ap.parse_args()

    sw = SwSession.open(visible=True)
    try:
        sw.open_doc(a.doc)
        rep = inspect(sw, a.sketch)
        _print(rep, verbose=a.full)
        if a.json:
            Path(a.json).parent.mkdir(parents=True, exist_ok=True)
            Path(a.json).write_text(json.dumps(rep, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
            print(f"\n已写入 {a.json}")
        if not rep["features"]:
            raise SwError("没读到任何特征 —— 检查文档是否真的打开了")
    finally:
        with contextlib.suppress(Exception):
            sw.close()


if __name__ == "__main__":
    _cli()
