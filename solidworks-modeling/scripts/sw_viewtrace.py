"""sw_viewtrace.py - 视图轮廓提取 + 比例尺标定 → 真实 mm 轮廓（用户 2026-10-03 提出的方法论:
视图分割 → 比例尺等比还原 → 直接得主体草图几何, 再做特征编辑细节推敲）
定位: 给 agent 当眼睛 —— 工具负责几何提取与坐标换算, agent 负责看叠加图核对与决策。
"""
from __future__ import annotations
import argparse
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np


def load_gray(path: str) -> np.ndarray:
    img = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"读不了图: {path}")
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def binarize(gray: np.ndarray) -> np.ndarray:
    """深色线条 → 白。自适应阈值对付光照不均的截图。"""
    blk = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                cv2.THRESH_BINARY_INV, 31, 12)
    return blk


def dehatch(bw: np.ndarray, ang_tol: int = 14, min_len: int = 12) -> np.ndarray:
    """剔除剖面线(45°细斜线)。

    剖面线是**独立细直线段**且方向集中在 ±45°: 用 HoughLinesP 检出后按角度过滤,
    从二值图抹掉。轮廓线是水平/垂直/大圆弧, 不会被误删(圆弧的局部朝向分散,
    而 hatch 每条线整体都是 45°)。
    """
    out = bw.copy()
    lines = cv2.HoughLinesP(bw, 1, np.pi / 180, threshold=min_len,
                            minLineLength=min_len, maxLineGap=2)
    if lines is None:
        return out
    removed = 0
    for l in lines[:, 0]:
        x1, y1, x2, y2 = map(int, l)
        ang = abs(math.degrees(math.atan2(y2 - y1, x2 - x1))) % 180.0
        # 只删集中在 45°±tol 的(正向或负向)
        d = min(abs(ang - 45.0), abs(ang - 135.0))
        if d <= ang_tol:
            cv2.line(out, (x1, y1), (x2, y2), 0, 3)
            removed += 1
    return out


def find_candidates(bw: np.ndarray, min_area_frac: float = 0.0005):
    """返回按面积降序的外层轮廓候选。"""
    h, w = bw.shape
    contours, hierarchy = cv2.findContours(bw, cv2.RETR_TREE, cv2.CHAIN_APPROX_NONE)
    cands = []
    for i, c in enumerate(contours):
        area = cv2.contourArea(c)
        if area < min_area_frac * h * w:
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        per = cv2.arcLength(c, True)
        cands.append({
            "id": len(cands),
            "contour": c,
            "area_px": area,
            "bbox_px": [x, y, x + cw, y + ch],
            "n_pts": len(c),
            "closure": per,
            "is_hole": bool(hierarchy is not None and hierarchy[0][i][3] >= 0),
            "parent": int(hierarchy[0][i][3]) if hierarchy is not None else -1,
        })
    cands.sort(key=lambda d: -d["area_px"])
    for j, d in enumerate(cands):
        d["id"] = j
    return cands


def annotate(gray: np.ndarray, cands: list[dict], out: str, top: int = 14) -> str:
    vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    colors = [(0, 0, 255), (0, 160, 0), (255, 0, 0), (0, 128, 255), (160, 0, 200),
              (0, 200, 200), (200, 0, 120), (80, 80, 0), (0, 90, 0), (120, 120, 0)]
    for d in cands[:top]:
        c = colors[d["id"] % len(colors)]
        cv2.drawContours(vis, [d["contour"]], -1, c, 2)
        x, y = d["bbox_px"][0], d["bbox_px"][1]
        tag = f"#{d['id']}" + ("(hole)" if d["is_hole"] else "")
        cv2.putText(vis, tag, (x, max(14, y - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    0.55, c, 2, cv2.LINE_AA)
    cv2.imencode(".png", vis)[1].tofile(out)
    return out


def crop_contour(c, bx: int, by: int):
    if bx <= 0 and by <= 0:
        return c
    return c.copy()  # 裁剪发生在预裁剪图上, 无需平移


def simplify(contour: np.ndarray, eps_frac: float = 0.0015):
    """折线化: eps 相对周长。返回简化点列(px)。"""
    per = cv2.arcLength(contour, True)
    eps = eps_frac * per
    approx = cv2.approxPolyDP(contour, eps, True)
    return approx.reshape(-1, 2), eps


def fit_arcs(points_px: np.ndarray, contour: np.ndarray):
    """MVP: 只做整圆检测 — 用闭合轮廓的最小二乘圆拟合, 报圆度。
    圆弧分段拟合留 v2。返回 dict(r_px, center_px, rms, circularity)"""
    (cx, cy), r = cv2.minEnclosingCircle(contour)
    pts = contour.reshape(-1, 2).astype(np.float64)
    d = np.hypot(pts[:, 0] - cx, pts[:, 1] - cy)
    rms = float(np.sqrt(np.mean((d - r) ** 2)))
    area = cv2.contourArea(contour)
    per = cv2.arcLength(contour, True)
    circ = float(4 * math.pi * area / (per * per)) if per > 0 else 0.0
    return {"cx_px": float(cx), "cy_px": float(cy), "r_px": float(r),
            "rms_px": rms, "circularity": circ}


def px2mm_scale(p1, p2, mm: float) -> float:
    dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if dist <= 0:
        raise SystemExit("标定两点重合")
    return mm / dist


def hough_circles(gray: np.ndarray, min_r: int = 8, max_r: int = 400):
    """检测圆(标注引线会干扰, 所以在 opening 后的图上做)。返回按半径降序去重。"""
    bw = binarize(gray)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k)
    inv = cv2.bitwise_not(bw)
    circles = cv2.HoughCircles(inv, cv2.HOUGH_GRADIENT, dp=1, minDist=20,
                               param1=100, param2=40,
                               minRadius=min_r, maxRadius=max_r)
    out = []
    if circles is None:
        return out
    for c in circles[0]:
        x, y, r = float(c[0]), float(c[1]), float(c[2])
        if any(abs(x - o["cx"]) < 15 and abs(y - o["cy"]) < 15 for o in out):
            continue
        out.append({"cx": x, "cy": y, "r": r})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="视图位图 → 轮廓候选叠加图 → 按比例尺还原真实 mm 轮廓(swplan 草图素材)")
    ap.add_argument("--png", required=True, help="截图/渲染图")
    ap.add_argument("--crop", help="只看这个区域 x1,y1,x2,y2(先裁后提, 剖面线/尺寸线少很多)")
    ap.add_argument("--annotate-out", default="", help="候选轮廓叠加图输出路径")
    ap.add_argument("--pick", type=int, default=-1, help="选中的候选 id(-1 只出叠加图)")
    ap.add_argument("--calib-px", help="标定两点 x1,y1,x2,y2(图上某一已知长度线段两端, 原图坐标)")
    ap.add_argument("--calib-mm", type=float, default=0.0, help="该线段真实长度 mm")
    ap.add_argument("--dehatch", action="store_true",
                    help="剔除 45° 剖面线(剖视图轮廓提取前必开)")
    ap.add_argument("--thin-filter", action="store_true",
                    help="形态学开运算去细线(尺寸线~1px, 粗实线轮廓~2.5px 保留)")
    ap.add_argument("--hough", action="store_true",
                    help="检测视图里的圆(孔/外圆), 用于标定与孔位验证")
    ap.add_argument("--measure",
                    help="量点对(原图像素坐标): 'x1,y1,x2,y2;x3,y3,x4,y4…' 配合标定输出 mm")
    ap.add_argument("--top", type=int, default=10, help="打印前 N 个候选")
    ap.add_argument("--json", default="", help="结果 JSON 输出路径")
    ap.add_argument("--origin", help="mm 坐标原点(像素) x,y, 默认用轮廓 bbox 中心")
    args = ap.parse_args()

    gray_full = load_gray(args.png)
    ox, oy = 0, 0
    gray = gray_full
    if args.crop:
        x1, y1, x2, y2 = (int(v) for v in args.crop.split(","))
        gray = gray_full[y1:y2, x1:x2]
        ox, oy = x1, y1

    bw = binarize(gray)
    if args.dehatch:
        bw = dehatch(bw)
        dh = str(Path(args.png).with_name("viewtrace_dehatched.png"))
        cv2.imencode(".png", bw)[1].tofile(dh)
        print("去剖面线中间图:", dh)
    if args.thin_filter:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN, k)
    cands = find_candidates(bw)
    ann = args.annotate_out or str(Path(args.png).with_name("viewtrace_annotated.png"))
    annotate(gray, cands, ann)
    print(f"候选轮廓 {len(cands)} 个 → 叠加图 {ann}")
    for d in cands[:args.top]:
        print(f"  #{d['id']} area={d['area_px']:.0f} bbox={d['bbox_px']} "
              f"pts={d['n_pts']} hole={d['is_hole']}")

    scale = None
    if args.calib_px and args.calib_mm:
        vals = [float(v) for v in args.calib_px.split(",")]
        scale = px2mm_scale((vals[0] - ox, vals[1] - oy), (vals[2] - ox, vals[3] - oy),
                            args.calib_mm)
        print(f"比例尺: {args.calib_mm}mm / {math.hypot(vals[2]-vals[0], vals[3]-vals[1]):.1f}px "
              f"= {scale:.5f} mm/px")
    if args.measure:
        if scale is None:
            raise SystemExit("--measure 需要 --calib-px/--calib-mm 先标定")
        for seg in args.measure.split(";"):
            v = [float(p) for p in seg.split(",")]
            if len(v) != 4:
                raise SystemExit(f"measure 段格式错: {seg}")
            dx, dy = v[2] - v[0], v[3] - v[1]
            d_px = math.hypot(dx, dy)
            print(f"  ({v[0]:.0f},{v[1]:.0f})->({v[2]:.0f},{v[3]:.0f}): "
                  f"{d_px:.1f}px = {d_px*scale:.2f}mm "
                  f"(dx={dx*scale:+.2f}, dy={-dy*scale:+.2f} mm)")
    if args.hough:
        for c in hough_circles(gray):
            print(f"  circle: center=({c['cx']+ox:.0f},{c['cy']+oy:.0f}) r={c['r']:.1f}px"
                  + (f" = {c['r']*2*scale:.2f}mm 直径" if scale else ""))

    if args.pick < 0:
        print("→ 看叠加图选 --pick <id> 出轮廓 mm 点列; 或只标定/测量到此为止")
        return

    picked = next((d for d in cands if d["id"] == args.pick), None)
    if picked is None:
        raise SystemExit(f"没有 #{args.pick}")
    contour = picked["contour"]
    pts_px, eps = simplify(contour, args.eps)
    circle = fit_arcs(pts_px, contour)

    if args.origin:
        o = [float(v) for v in args.origin.split(",")]
        org_px = (o[0] - ox, o[1] - oy)
    else:
        x, y, x2, y2 = picked["bbox_px"]
        org_px = ((x + x2) / 2, (y + y2) / 2)

    out = {
        "png": args.png, "pick": args.pick, "eps_px": round(eps, 2),
        "n_poly_pts": len(pts_px),
        "circle_fit": {k: round(v, 3) for k, v in circle.items()},
        "scale_mm_per_px": round(scale, 6) if scale else None,
        "origin_px": [round(org_px[0] + ox, 1), round(org_px[1] + oy, 1)],
    }
    if scale:
        mm_pts = [[round((p[0] - org_px[0]) * scale, 2),
                   round(-(p[1] - org_px[1]) * scale, 2)] for p in pts_px]
        out["polyline_mm"] = mm_pts
        out["circle_fit_mm"] = {
            "r": round(circle["r_px"] * scale, 2),
            "center": [round((circle["cx_px"] - org_px[0]) * scale, 2),
                       round(-(circle["cy_px"] - org_px[1]) * scale, 2)],
        }
        # swplan 草图片段(polygon 素材, y 已翻成数学方向)
        out["swplan_polyline_points"] = mm_pts
        print(f"轮廓 mm 点列 {len(mm_pts)} 点 (y 已翻成数学方向, 原点见 origin_px)")
        print("  前 12 点:", mm_pts[:12])
        print(f"  整圆拟合: r={out['circle_fit_mm']['r']}mm rms={circle['rms_px']:.1f}px "
              f"circularity={circle['circularity']:.3f}")
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
        print("→ 已写", args.json)


if __name__ == "__main__":
    main()
