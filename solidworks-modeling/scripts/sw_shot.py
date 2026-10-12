#!/usr/bin/env python3
"""图形区截图 —— 「眼见为实」的自查工具。

为什么必须有它
--------------
脚本能返回的只有数字（包围盒、体积、轮廓数、特征数）。但现实里大量错误是
**数字全对、图一看就错**：

  · 键槽只开了一半（包围盒完全正常，因为孔本来就在里面）
  · 螺纹扫掠没切穿，只啃掉一圈皮
  · 倒角打在了错误的边上
  · 切除方向反了，切到了另一侧

所以建模后的标准动作应该是：**截图 → 看一眼 → 不对就换方案**，
而不是盯着返回的 True/False 宣布成功。

原理
----
    IModelDoc2.SaveBMP(FileName, Width, Height)  把**当前图形区**存成 BMP
    IModelDoc2.ShowNamedView2(Name, ViewId)      切标准视角
    IModelDoc2.ViewZoomtofit2()                  缩放到合适大小
    存完用 Pillow 转 PNG（多模态可直接读），可选拼成一张网格图。

用法
----
    python scripts/sw_shot.py --doc D:\\out\\gear.SLDPRT --out D:\\out\\shots \\
        --views iso,front,right,top
    python scripts/sw_shot.py --doc ... --views iso --grid      # 拼成一张

输出
----
    <out>/<零件名>_<视图>.png，以及 --grid 时的 <零件名>_sheet.png
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sw_connect import SwError, SwSession, member  # noqa: E402
from sw_ops import hide_construction_geometry  # noqa: E402

# 视图名（中文 SolidWorks）+ swStandardViews_e 数值。
# 名字只是给 ShowNamedView2 当提示用的，真正决定视角的是那个 id。
VIEWS: dict[str, tuple[str, int]] = {
    "front": ("前视", 1),
    "back": ("后视", 2),
    "left": ("左视", 3),
    "right": ("右视", 4),
    "top": ("上视", 5),
    "bottom": ("下视", 6),
    "iso": ("等轴测", 7),
    "dimetric": ("不等轴测", 8),
    "trimetric": ("三轴测", 9),
}


def autocrop_content(im, pad: int = 14, thr: int = 8, min_run: int = 3,
                     keep_frac: float = 0.03):
    """裁掉图形区四周的空白，让模型占据画面。

    为什么需要：`ViewZoomtofit2` 是fit到**当前视窗**的，而 `SaveBMP` 按
    指定宽高另存 —— 两者长宽比不一致时，产物四周就留大片空白，模型缩在中间
    （真机踩过：1400×1050 的图里模型只占 45%）。图能用，但看着不利索。

    怎么判「哪里是内容」：**不能用颜色差** —— SolidWorks 渲染出来模型是白的、
    背景也是白的，白底白模的差分几乎为零，裁剪会失效（已踩）。改用**结构**：
    背景是平滑渐变（局部梯度≈0），模型有轮廓边 + 明暗交界边（梯度大）。
    对灰度图取 |∂x|+|∂y|，按「行/列上至少 min_run 个边缘像素」定位内容框。

    产量守卫：若内容框几乎盖满（>97%）或没有内容，就**原样返回**，不裁 ——
    宁可留白，也不要把模型裁掉。
    """
    try:
        import numpy as np  # type: ignore

        g = np.asarray(im.convert("L")).astype(np.int16)
        gx = np.abs(np.diff(g, axis=1, prepend=g[:, :1]))
        gy = np.abs(np.diff(g, axis=0, prepend=g[:1, :]))
        mask = (gx + gy) > thr
        h, w = mask.shape
        cols = np.where(mask.sum(axis=0) >= min_run)[0]
        rows = np.where(mask.sum(axis=1) >= min_run)[0]
        if len(cols) == 0 or len(rows) == 0:
            return im
        x0, x1, y0, y1 = int(cols.min()), int(cols.max()), int(rows.min()), int(rows.max())
        if (x1 - x0) * (y1 - y0) > 0.97 * w * h:
            return im
        x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
        x1, y1 = min(w - 1, x1 + pad), min(h - 1, y1 + pad)
        # 只有「两维都几乎没削掉」才不值得裁 —— ★极性别写反：
        #   写反了就会在**正好需要裁**的时候返回原图（2026-10-11 踩过：
        #   集成进来后 1400×1050 一张都没裁，而独立测试脚本却裁了，
        #   因为那个脚本用的是「长得像」的另一份实现 ⇒ 教训：**要测被测函数本身**）。
        if (x1 - x0) > (1 - keep_frac) * w and (y1 - y0) > (1 - keep_frac) * h:
            return im
        return im.crop((x0, y0, x1 + 1, y1 + 1))
    except Exception:                                 # noqa: BLE001
        return im


def _to_png(bmp: Path, crop: bool = True) -> Path:
    """BMP → PNG（可选自动裁剪空白）；没有 Pillow 就原样留着。"""
    png = bmp.with_suffix(".png")
    try:
        from PIL import Image  # type: ignore

        with Image.open(bmp) as im:
            out = im.convert("RGB")
            if crop:
                out = autocrop_content(out)
            out.save(png)
        bmp.unlink(missing_ok=True)
        return png
    except Exception:
        return bmp


def shot_view(sw: SwSession, out_dir: Path, key: str, stem: str,
              width: int, height: int, crop: bool = True) -> Path | None:
    """切到 key 视图并截图。返回产物路径；失败返回 None。"""
    if key not in VIEWS:
        raise SwError(f"未知视图 {key!r}，可选：{', '.join(VIEWS)}")
    view_name, view_id = VIEWS[key]
    m = sw.model

    # ★ 先统一成「带边线上色」。不这么做，截图结果会随文档上次的显示状态漂移 ——
    #   实测遇到过模型停在「线架图」，抓出来的图**只有曲线、看不到实体**，
    #   白吓一跳（以为实体没了）。截图是拿来做判断的，前提必须是显示状态可复现。
    with contextlib.suppress(Exception):
        member(m, "ViewDisplayShaded")

    # 切视角。中文/英文模板名字不同，带不带 '*' 前缀行为也不一样，两个都试。
    switched = False
    for attempt in ("*" + view_name, view_name):
        with contextlib.suppress(Exception):
            member(m, "ShowNamedView2", attempt, view_id)
            switched = True
            break
    if not switched:
        raise SwError(f"切视图失败：{view_name}")
    with contextlib.suppress(Exception):
        member(m, "ViewZoomtofit2")

    # 路径必须用**纯 ASCII**：SaveBMP 对非 ASCII 路径会静默返回 False 或写坏文件。
    bmp = out_dir / f"{stem}_{key}.bmp"
    with contextlib.suppress(Exception):
        member(m, "SaveBMP", str(bmp), int(width), int(height))

    # ★ 不能信返回值。文件真存在、且不是一张空白小图，才算成功。
    if not bmp.exists() or bmp.stat().st_size < 2048:
        return None
    return _to_png(bmp, crop=crop)


def make_sheet(images: list[Path], out: Path, cols: int = 2,
               label: bool = True) -> Path | None:
    """把多张截图拼成一张网格图，方便一次看完。"""
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception:
        return None
    ims = []
    for p in images:
        try:
            with Image.open(p) as im:
                ims.append((p.stem, im.convert("RGB").copy()))
        except Exception:
            continue
    if not ims:
        return None
    w = max(i.width for _, i in ims)
    h = max(i.height for _, i in ims) + (22 if label else 0)
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (w * cols, h * rows), (245, 245, 243))
    dr = ImageDraw.Draw(sheet)
    for idx, (name, im) in enumerate(ims):
        r, c = divmod(idx, cols)
        x, y = c * w, r * h
        if label:
            dr.text((x + 8, y + 5), name, fill=(40, 40, 40))
        sheet.paste(im, (x, y + (22 if label else 0)))
    sheet.save(out)
    return out


def default_out():
    """截图默认落盘目录：<产出目录>/shots（产出目录见 sw_env.out_dir）。"""
    import sw_env
    return sw_env.out_dir() / "shots"


def _cli() -> None:
    ap = argparse.ArgumentParser(description="SolidWorks 图形区截图（建模自查）")
    ap.add_argument("--doc", default=None, help="零件/装配体路径；不给就截当前活动文档")
    ap.add_argument("--out", default=str(default_out()), help="输出目录")
    ap.add_argument("--views", default="iso,front,right,top",
                    help="逗号分隔：iso/front/back/left/right/top/bottom/dimetric/trimetric")
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=1050)
    ap.add_argument("--grid", action="store_true", help="额外拼一张网格总览图")
    ap.add_argument("--invisible", action="store_true",
                    help="隐藏 SW 窗口（注意：隐藏后图形区可能存出空白图，默认可见）")
    ap.add_argument("--keep-sketch", action="store_true",
                    help="保留构造几何（草图/曲线）。默认**隐藏** —— 否则 3D 草图"
                         "（放样截面/引导线）会把实体糊满，看不清几何")
    ap.add_argument("--no-crop", action="store_true",
                    help="不自动裁剪四周空白（默认按「结构边缘」裁掉空白，"
                         "让模型占满画面；白底白模时靠边缘而非颜色判断）")
    a = ap.parse_args()

    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    keys = [k.strip() for k in a.views.split(",") if k.strip()]

    sw = SwSession.open(visible=not a.invisible)
    try:
        if a.doc:
            sw.open_doc(a.doc)
        elif sw.model is None:
            raise SwError("没有打开的文档，请给 --doc")
        stem = Path(a.doc).stem if a.doc else "active"
        # ★ 交付截图先隐藏构造几何（草图/螺旋线/参考曲线）：否则 3D 草图（放样
        #   截面与引导线）的蓝线会把实体糊满 —— 2026-10-11 S5 车头截图踩到过。
        if not a.keep_sketch:
            with contextlib.suppress(Exception):
                r = hide_construction_geometry(sw)
                n = len(r.get("features_hidden") or [])
                # ★ 判据落在「藏了几个」，不是「这个函数跑过了」——
                #   白名单不匹配 / 选择集为空时它会**静默地一个都不藏**（2026-10-11 真机踩到）。
                print(f"  (已隐藏 {n} 个构造几何（草图/曲线）{r.get('groups')}；"
                      "要保留加 --keep-sketch)")
                if n == 0:
                    print("  ! 一个都没藏到 —— 蓝线会盖住几何，"
                          "先查 sw_ops.CURVE_TYPES / SKETCH_TYPES 白名单")
        made: list[Path] = []
        for k in keys:
            p = shot_view(sw, out_dir, k, stem, a.width, a.height, crop=not a.no_crop)
            print(f"  {k:9} {'OK  ' + str(p) if p else 'FAIL 存图失败或图形区是空的'}")
            if p:
                made.append(p)
        if a.grid and made:
            g = make_sheet(made, out_dir / f"{stem}_sheet.png")
            print(f"  {'grid':9} {g if g else 'FAIL（没装 Pillow？）'}")
        print(json.dumps({"ok": len(made), "total": len(keys), "out": str(out_dir)},
                         ensure_ascii=False))
    finally:
        with contextlib.suppress(Exception):
            sw.close()


if __name__ == "__main__":
    _cli()
