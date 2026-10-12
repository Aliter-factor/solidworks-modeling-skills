#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""参照件取证：读一个成品 `.SLDPRT` 的**面**，反推出「图纸没写、但零件里有」的尺寸。

为什么需要它：真实项目里常常**只有成品模型、没有 2D 图纸**（用户的一级减速器就是
`C:\\学习软件\\一级减速器\\` 一整套 SLDPRT）。要照着它建模，就得把几何"倒推"出来。

为什么读面而不是读草图：晚期绑定下 `ISketch.GetArcs2` / `GetLines2` 回来的元素
类型不对（取不到几何，见 `30-pitfalls.md` §64）；而 `IFace2` + `ISurface` 是稳的。

三条输出（都按主轴方向聚合）：

  ① **台阶表**（圆柱面）—— 每级的轴向起止 + 直径。
     回转体（轴/套筒/齿轮轮毂）的一级台阶 = 一个圆柱面，`GetBox` 沿轴向的分量
     就是这一级的起止站位。**这是重建阶梯轴 `segments=[{d,l}]` 的直接依据。**
  ② **平面站位表**（法向平行主轴的平面）—— 位置 + 面数 + 总面积。
     轮辐/腹板的**轴向厚度**看这里（例如齿轮腹板 = 两个 L=±4.5 的平面夹出 9.0）。
  ③ **锥面清单**（倒角/过渡）—— 面积 + 包围盒。端面倒角就藏在这里。

用法：

    python scripts/sw_faces.py --doc "C:\\学习软件\\一级减速器\\20-轴.SLDPRT"
    python scripts/sw_faces.py --doc xx.SLDPRT --json out.json
    python scripts/sw_faces.py --doc xx.SLDPRT --axis 1        # 手动指定主轴

注意：本脚本**只读**（一律 `read_only` 打开、不保存），不改参照件。
`IFace2.GetBox` 对**平面**是精确的；对 B 样条/曲面会虚报（§53），所以台阶表
另给了 `r_from_box` 与 `CylinderParams` 的半径互相印证。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sw_connect  # noqa: E402
from sw_connect import member, typed  # noqa: E402


# --------------------------------------------------------------------------- #
# 读面
# --------------------------------------------------------------------------- #

def read_faces(sw, limit: int = 9000) -> list[dict]:
    """遍历所有实体上的所有面，逐个分类。

    ★ 取实体走 `sw.bodies()`（全项目唯一入口）—— 曾经用 `GetBodies2(0, True)`
      （只取可见）⇒ 实体一被抑制就"读到 0 个面"，看起来像零件空了（见该函数说明）。
    """
    out: list[dict] = []
    for b in sw.bodies():
        body = typed(b, "IBody2")
        f = member(body, "GetFirstFace")
        n = 0
        while f is not None and n < limit:
            n += 1
            f2 = typed(f, "IFace2")
            rec: dict = {}
            with contextlib.suppress(Exception):
                rec["area_mm2"] = round(float(member(f2, "GetArea")) * 1e6, 4)
            with contextlib.suppress(Exception):
                bb = list(member(f2, "GetBox"))
                rec["box_mm"] = [round(bb[i] * 1000.0, 4) for i in range(6)]

            srf = None
            with contextlib.suppress(Exception):
                srf = typed(member(f2, "GetSurface"), "ISurface")
            if srf is not None:
                with contextlib.suppress(Exception):
                    if bool(member(srf, "IsPlane")):
                        rec["kind"] = "plane"
                        pp = member(srf, "PlaneParams")
                        rec["normal"] = [round(float(v), 5) for v in pp[0:3]]
                        rec["point_mm"] = [round(float(v) * 1000.0, 4) for v in pp[3:6]]
                    elif bool(member(srf, "IsCylinder")):
                        rec["kind"] = "cyl"
                        cp = member(srf, "CylinderParams")
                        rec["r_mm"] = round(float(cp[6]) * 1000.0, 4)
                        rec["axis"] = [round(float(v), 5) for v in cp[3:6]]
                        rec["origin_mm"] = [round(float(v) * 1000.0, 4) for v in cp[0:3]]
                    elif bool(member(srf, "IsCone")):
                        rec["kind"] = "cone"
                    elif bool(member(srf, "IsTorus")):
                        # ★ v1.4.9 补：**环面 = 型面里的一段圆弧**（倒圆/圆角）。
                        #   回转件的半剖面里，环面出的是圆弧而不是直线段 ——
                        #   `stairway`/`plane_stations` 两张表都看不见它，正是
                        #   "型面读不出来"的常见来源（19-端盖1 就有一张，
                        #   box x∈[-1,1] 面积 498.87，把轮廓链断在 (-1,24)→(1,26)）。
                        rec["kind"] = "torus"
                    elif bool(member(srf, "IsSphere")):
                        rec["kind"] = "sphere"
                    elif bool(member(srf, "IsRevolved")):
                        rec["kind"] = "revolved"
                    else:
                        rec.setdefault("kind", f"ident{int(member(srf, 'Identity'))}")
            out.append(rec)
            with contextlib.suppress(Exception):
                f = member(f2, "GetNextFace")
    return out


# --------------------------------------------------------------------------- #
# 聚合
# --------------------------------------------------------------------------- #

def _doms(axis: list[float]) -> int:
    return max(range(3), key=lambda i: abs(axis[i]))


def stairway(faces: list[dict], ax: int) -> list[dict]:
    """圆柱面 → 「站位区间 → 直径」台阶表（按 from 升序）。"""
    rows: list[dict] = []
    for c in faces:
        if c.get("kind") != "cyl" or "box_mm" not in c:
            continue
        if _doms(c.get("axis") or [0, 0, 0]) != ax:
            continue
        bb = c["box_mm"]
        lat = [i for i in range(3) if i != ax]
        rr = [(bb[i + 3] - bb[i]) / 2.0 for i in lat]
        rows.append({
            "from_mm": round(bb[ax], 3),
            "to_mm": round(bb[ax + 3], 3),
            "len_mm": round(bb[ax + 3] - bb[ax], 3),
            "d_mm": round(c.get("r_mm", 0.0) * 2.0, 3),
            "r_from_box_mm": round(sum(rr) / len(rr), 3),
            "area_mm2": c.get("area_mm2"),
        })
    rows.sort(key=lambda r: (r["from_mm"], -r["d_mm"]))
    return rows


def plane_stations(faces: list[dict], ax: int, tol: float = 1e-3) -> list[dict]:
    """法向平行主轴的平面 → 站位表（位置 + 面数 + 总面积 + 最大单面）。"""
    agg: dict[float, list[dict]] = {}
    for f in faces:
        if f.get("kind") != "plane":
            continue
        nrm = f.get("normal") or [0, 0, 0]
        if _doms(nrm) != ax or abs(abs(nrm[ax]) - 1.0) > tol:
            continue
        pos = round(float((f.get("point_mm") or [0, 0, 0])[ax]), 3)
        agg.setdefault(pos, []).append(f)
    rows = []
    for pos in sorted(agg):
        grp = agg[pos]
        areas = [g.get("area_mm2") or 0.0 for g in grp]
        rows.append({
            "at_mm": pos,
            "n_faces": len(grp),
            "area_mm2": round(sum(areas), 3),
            "max_face_mm2": round(max(areas) if areas else 0.0, 3),
        })
    return rows


def cones(faces: list[dict], top: int = 12) -> list[dict]:
    rows = [f for f in faces if f.get("kind") == "cone"]
    rows.sort(key=lambda x: -(x.get("area_mm2") or 0.0))
    return rows[:top]


def summarize(rec: dict, ax: int) -> dict:
    return {
        "bbox_mm": rec.get("bbox_mm"),
        "main_axis": ax,
        "n_faces": rec.get("n_faces"),
        "stairway": stairway(rec.get("faces") or [], ax),
        "plane_stations": plane_stations(rec.get("faces") or [], ax),
        "cones": cones(rec.get("faces") or []),
    }


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def print_report(rec: dict, ax: int) -> None:
    name_ax = "XYZ"[ax]
    print("=" * 74)
    print("bbox", rec.get("bbox_mm"), "主轴向", name_ax, "面数", rec.get("n_faces"))

    st = stairway(rec.get("faces") or [], ax)
    print(f"-- ① 台阶表（圆柱面，沿 {name_ax}）  {len(st)} 级")
    for r in st:
        print(f"   {name_ax} {r['from_mm']:>9.3f} -> {r['to_mm']:>9.3f}"
              f"  (L={r['len_mm']:>7.3f})  Ø{r['d_mm']:>8.3f}"
              f"  boxØ{r['r_from_box_mm'] * 2:>8.3f}  A={r['area_mm2']}")

    ps = plane_stations(rec.get("faces") or [], ax)
    print(f"-- ② 平面站位表（法向 || {name_ax}）  {len(ps)} 处")
    for r in ps:
        print(f"   {name_ax}={r['at_mm']:>9.3f}   面数={r['n_faces']:>3}"
              f"   总面积={r['area_mm2']:>11.2f}   最大单面={r['max_face_mm2']:>10.2f}")

    cn = cones(rec.get("faces") or [])
    print(f"-- ③ 锥面（倒角/过渡，按面积降序取前 {len(cn)} 个）")
    for f in cn:
        print(f"   A={f.get('area_mm2'):>10.2f}  box={f.get('box_mm')}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="读参照件的面，反推几何（只读，不改文件）")
    ap.add_argument("--doc", required=True, help="参照件 .SLDPRT 路径")
    ap.add_argument("--json", help="把结果写到这个 json")
    ap.add_argument("--axis", type=int, choices=[0, 1, 2],
                    help="主轴（默认取包围盒最长边）")
    ap.add_argument("--visible", action="store_true",
                    help="让 SolidWorks 可见（排查挂起时用）")
    args = ap.parse_args(argv)

    doc = Path(args.doc)
    if not doc.exists():
        print(f"找不到文件：{doc}", file=sys.stderr)
        return 2

    sw = sw_connect.SwSession.open(visible=args.visible)
    try:
        sw.open_doc(str(doc), read_only=True)
        sw.set_units_mm()
        rec: dict = {}
        bb = sw.bounding_box()
        if bb:
            rec["bbox_mm"] = [round((bb[i + 3] - bb[i]) * 1000.0, 3) for i in range(3)]
            rec["bbox_min_mm"] = [round(bb[i] * 1000.0, 3) for i in range(3)]
        faces = read_faces(sw)
        rec["n_faces"] = len(faces)
        rec["faces"] = faces
        if args.axis is not None:
            ax = args.axis
        elif bb:
            ax = max(range(3), key=lambda i: bb[i + 3] - bb[i])
        else:
            ax = 0
        rec["main_axis"] = ax
    finally:
        with contextlib.suppress(Exception):
            sw.close_doc(save=False)

    print_report(rec, ax)
    if args.json:
        slim = dict(rec)
        slim["summary"] = summarize(rec, ax)
        Path(args.json).write_text(
            json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n已写出 {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
