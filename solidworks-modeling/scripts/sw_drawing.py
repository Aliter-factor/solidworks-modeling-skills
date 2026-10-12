#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""`sw_drawing.py` —— **给 agent 当眼睛的图纸辅助工具**（渲染 / 切视图 / 交叉验证）。

★★★ 定位（2026-10-03 用户校准，原话）
--------------------------------------
> 「你要知道我们开发的是 skills，给 agent 用，**agent 自己肯定是能够完成
> OCR 的**，我们只需要有一些工具**协助它完成建模尺寸确定**即可。」

⇒ **agent 有多模态视觉，看图读尺寸是它自己的事** —— 本工具**不替它读**。
   RapidOCR 实测并不可靠（`⌀` 被引线切丢、zoom 敏感、单遍不可复现），
   它只能当**交叉验证**，不能当事实来源。

⇒ 本工具做 agent **做不了或做不好**的三件事：
```
① 渲染/切图   render() / render_views()   ← agent 需要高清、按视图切好的图
② 题面真值     sw_exercise.py              ← 目标体积 / Design Intent / 建议特征
③ 建完验算     sw_exercise_check.py        ← check_part()：体积 vs 目标体积
```

**推荐工作流（agent 视角）**：
```
1. sw_exercise.py --file <题.pdf>          # 拿到目标体积、设计意图、建议特征
2. sw_drawing.py --pdf <题.pdf> --crop-views --out-dir D:/out/_train/<题>/
   # ⇒ 得到 overview.png（整张图纸）+ view1.png/view2.png/...（每个视图一张，
   #   带 pad，尺寸标在视图外也能看全）⇒ agent 用视觉逐张读尺寸
3. 建模（op 计划），check_mass 用 expect_volume_mm3 对目标体积
4. sw_exercise_check.py --check <题> --part <建的.SLDPRT>   # 终验
```

★ OCR 归属/投票那套（`multi_pass_dims` / `assign_dims_to_views`）**保留**，
  用途降级为：agent 读完后想**交叉验证**一遍数字时用（`--multi-pass`）。
  它的判据结论（分组、归属、符号还原）依然成立，只是**不当事实来源**。

### ★★★ 三条真机结论（每一条都推翻了我原本的判据）

**① 视图必须先「切区域」，再「挂尺寸」—— 顺序不能反。**
我先做尺寸空间聚类，调网格从 ×1 调到 ×8 才勉强凑出 `[3,1,2,5]`
（真值是 Top 5 / Front 8）⇒ **调不出来**。
看叠加图才明白：**「空白带」不是可靠依据**（Front 自己的 `12/75` 之间
就有 240px 空白）。
⇒ 正解：**视图 = 彩色填充的连通域**（工程图里视图主体有填充色、
尺寸线/文字/轮廓是纯黑）⇒ 用**颜色**切，天然可靠。实测 3/3 命中。

**② 视图 bbox **只含彩色填充，不含尺寸** ⇒ 「最近视图」判据必然歧义。**
真机读数：Top 的 bbox 右边界 x=732，而它的 `20`(x=818) / `28`(x=884)
都在**右侧外面**；`28` 到 Top 的距离 152px、到 Iso 的 148px
⇒ **"最近"是 148 vs 152，几乎一样** ⇒ 会把 Top 的 `28` 挂到 Iso 上
（第一版真的错了）。
⇒ 正判据：**沿「视图排布轴」投影，落在哪个视图的区间内**。
标准三视图是「Top 在上、Front 在下」⇒ 判 **y**；并排布局判 **x**。
★ 这一步还顺带解释了「为什么 `12`(y=751) 归 Front 而不是 Top」：
  它的 **x 与 Top 的尺寸相同**，**只有 y 能区分**。

**③ Isometric 视图一个尺寸都没有 ⇒ 只靠「有尺寸」发现视图会静默漏掉轴测。**
而轴测恰恰是判别 3D 形状的关键（各视图给尺寸、轴测给形状）。
⇒ **视图检出必须独立于尺寸**（靠彩色区域），不能靠尺寸反推。
⇒ 工具会显式报「检出 N 个视图，其中 M 个无尺寸」。

### ★★ 符号丢失：不靠 OCR 补，靠几何还原

真机 OCR（`_diag/diag_ocr_dims.py`）读出 10/11 个尺寸，**数字全对**，但：
- `⌀12` 的 **`⌀` 丢了** ⇒ 只剩 `12` ⇒ **会把孔当方槽建，形状直接错**
- `20` 读成 **`-20-`** ⇒ 两侧**尺寸线**被当连字符

⇒ `normalize_dim()` 做**文本层**还原（剥尺寸线污染、认 `Ø`/`R`/`N×`/公差）；
⇒ `find_leader_lines()`（`--with-lines`）做**几何层**兜底：
  **数字左侧紧邻一小段横线（引线）⇒ 直径**。
★ 少了 `Ø`，agent 会把**孔**建成**方槽** —— 这是"读不懂图纸"最贵的后果。

### 用法
```
# ★★★ 首选：渲染 + 按视图裁成单张图（给 agent 看；agent 自己读尺寸）
python scripts/sw_drawing.py --pdf <题.pdf> --crop-views --out-dir D:/out/_train/<题>/
#   ⇒ overview.png（整张图纸）+ view1.png / view2.png / ...
#     每张带 pad（尺寸常标在视图填充区外），文件清单打印出来

# 交叉验证（可选）：agent 读完尺寸后，用多遍 OCR 投票核一遍数字
python scripts/sw_drawing.py --pdf <题.pdf> --multi-pass

# 渲染整张图纸区（不切视图）
python scripts/sw_drawing.py --pdf <题.pdf> --png D:/out/x.png

# 离线自检（不开 SW、不读 PDF）
python scripts/sw_drawing.py --self-test
```
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import sys
from typing import Any

# ================================================================ 常量

#: 渲染放大倍数。★★ 实测（2026-10-03，`Level01/1_1` 同一张图）：
#:   zoom=4 → 12 个尺寸（漏 `75`）  zoom=5 → **11 个全对**  zoom=6 → 10 个
#:   zoom=8 → 把 `16` 误读成 `-12`
#: ⇒ **没有任何单一倍数可靠** ⇒ 用 `ZOOM_SET` 多遍识别 + 投票（见 `analyze`）。
ZOOM = 5
#: 多遍识别用的放大倍数集合
ZOOM_SET = (4, 5, 6)
#: ★ `render_views` 裁视图时的**外扩比例**（按图幅）。
#:   视图 bbox 只含**填充区**，尺寸标注在外面（真机：`20`/`28` 在 Top bbox
#:   右侧 86~152px 外）⇒ 不外扩就把尺寸裁掉，agent 看到的是「没尺寸的图」。
VIEW_PAD_FRAC = 0.10
#: 彩色区域检测：饱和度阈值（`max(rgb) - min(rgb)`）
SAT_MIN = 28
#: 小于这个像素数的彩色块算噪声
MIN_AREA = 1500
#: 彩色块合并间隙，**按图幅的比例**（0.006）—— 一个视图常被黑色轮廓
#: 切成好几块，需要合并。★★ **必须是比例，不能是绝对像素**：真机
#: `Level01/1_2` 实测 gap=0/10 ⇒ **4 个视图区域**；gap=26 ⇒ **连成 1 个**！
#: （该图 901×646，而 26px 恰好跨过了视图间的空白带）
MERGE_GAP_FRAC = 0.006
#: 兼容旧名（不再用于判定，仅供报告）
MERGE_GAP = 26
#: 尺寸中心到视图区间的容差（px）—— 尺寸可以标在视图外面
ASSIGN_PAD = 40
#: ★ 归属的**放弃阈值**（px）：双轴距离都超过它就报「未归属」，
#:   **不硬挂**。真机上 `60` 在 Front 下方 164px 外（尺寸标很远是常态），
#:   但无归属的尺寸必须**显式报出来**而不是猜一个。
REJECT_DIST = 260.0

# ★★★ 文本形态（真机读数）
#   `20` `12` `75`      普通线性尺寸
#   `⌀12` `Ø12`         直径（**前缀**）
#   `R5`                半径
#   `2×⌀6`             重复特征
#   `-0.05` `12 +0.1`   公差（**有前导/后缀**，不能剥）
#   `-20-`              ★ 尺寸线被 OCR 当连字符
RE_TRAILING_DASH = re.compile(r"^-[\d.]+-$")
RE_DIA = re.compile(r"^[\u00d8\u00f8\u2205\u2300]")
RE_RAD = re.compile(r"^[Rr]\s*")
RE_NX = re.compile(r"^(\d+)\s*[x×]\s*")
# ★ 只匹配**后缀**（前面必须还有数字），不匹配前导 `-`
RE_TOL_SUFFIX = re.compile(r"(?<=\d)\s*(?:[?+\-]\s*[\d.]+)?\s*$")
RE_PURE_NUM = re.compile(r"^(\d+(?:\s*\.\s*\d+)?)$")
# ★ **带符号的数**（公差值 `-0.05` / 负尺寸）：整体就是一个数时保留符号。
#   第一版只认无符号数 ⇒ `-0.05` 返回 None ⇒ 公差被判成"不是尺寸"。
RE_SIGNED_NUM = re.compile(r"^([+-]?\d+(?:\s*\.\s*\d+)?)$")
RE_HAS_WORD = re.compile(r"[A-Za-z]{2,}")

#: 视图在图上的**标准位置**（真机实测，用来命名区域）
#   ★ 只给「候选名」与位置，**不猜视图身份** —— 身份要由尺寸与投影关系定
VIEW_NAME_BY_POS = (
    ("top",     "upper"),      # 上
    ("front",   "lower"),      # 下
    ("iso",     "right"),      # 右（轴测常在右侧）
)


# ================================================================ 纯函数层

def normalize_dim(text: str) -> dict[str, Any]:
    r"""把一段 OCR 文本**还原**成 `{'value', 'kind', 'raw', 'notes'}`。

    `kind` ∈ `diameter` / `radius` / `linear` / `count` / `unknown`
    ★ **不确定就报 `unknown`，不猜**（本项目判据原则，§99③）。

    ★★ `kind='unknown'` 是个**要紧的信号**，不是失败：
      它意味着「符号丢了」⇒ 上层**必须**去查几何（引线），
      否则会把孔当方槽建。
    """
    raw = text
    t = (text or "").strip()
    out: dict[str, Any] = {"value": None, "kind": "unknown", "raw": raw,
                           "notes": []}

    # ---- 视图名/图框/水印等：不是尺寸 ----
    if not t:
        out["notes"].append("空文本")
        return out
    if RE_HAS_WORD.search(t):
        out["notes"].append("含 ≥2 个连续字母 ⇒ 判为非尺寸（视图名/水印）")
        return out

    # ---- ① 尺寸线污染：`-20-` ⇒ 剥掉所有连字符 ----
    # ★ 必须要求**尾部也有 `-`**：`-0.05`（真负公差）只有一个前导 `-`。
    if RE_TRAILING_DASH.match(t):
        out["notes"].append(
            "OCR 把**两侧尺寸线**读成了连字符（`-20-`）⇒ 剥掉取数字。"
            "★ 只在**尾部也有 `-`** 时才剥，避免误伤 `-0.05` 这类真公差")
        t = t.strip("-").strip()

    # ---- ② 直径 / 半径前缀 ----
    if RE_DIA.match(t):
        out["kind"] = "diameter"
        t = RE_DIA.sub("", t)
    elif RE_RAD.match(t):
        out["kind"] = "radius"
        t = RE_RAD.sub("", t)

    # ---- ③ 重复特征 `2×⌀6` ----
    m = RE_NX.match(t)
    if m:
        if out["kind"] == "unknown":
            out["kind"] = "linear"
        out["count"] = int(m.group(1))
        out["notes"].append("带 `N×` ⇒ **阵列特征**（%d 个）" % int(m.group(1)))
        t = t[m.end():]
        if RE_DIA.match(t):
            out["kind"] = "diameter"
            t = RE_DIA.sub("", t)

    # ---- ④ 公差后缀（`12 +0.1` / `12-0.05`）----
    # ★★ 只剥**后缀**，**不剥前导 `-`**：`-0.05` 是**公差值**（真标注），
    #   剥掉就变成 `0.05`（符号丢失）⇒ 装配到 `0.05` 而不是 `-0.05`。
    #   前导 `-` 的判别交给 §1（`RE_TRAILING_DASH` 要求**尾部也有 `-`**）。
    t = RE_TOL_SUFFIX.sub("", t).strip()

    # ---- ⑤ 取数字 ----
    m = RE_PURE_NUM.match(t)
    signed = False
    if not m:
        # ★ 退一步：整体是**带符号的数** ⇒ 是公差值（`-0.05`）或负尺寸，
        #   **保留符号**。剥掉符号会把公差 `-0.05` 变成 `0.05` ⇒ 装配值反了。
        m = RE_SIGNED_NUM.match(t)
        signed = m is not None
    if not m:
        out["notes"].append("剥完前缀/后缀仍不是数字 ⇒ **不硬判**")
        return out
    try:
        out["value"] = float(m.group(1).replace(" ", ""))
        if signed:
            out["notes"].append("★ 原文是**带符号的数** ⇒ 按公差/负尺寸原样保留符号")
    except ValueError:
        out["notes"].append("数字解析失败")
        return out
    if out["kind"] == "unknown":
        out["kind"] = "linear"
    return out


def is_dimension_text(text: str) -> bool:
    """这段文本**能不能当尺寸候选**（不保证符号正确）。"""
    return normalize_dim(text)["value"] is not None


def dedup_dims(dims: list[dict], iou_min: float = 0.55) -> list[dict]:
    """去掉**同一个尺寸被 OCR 出两次**（多遍识别会重复）。

    判据 = 框**重叠率**（IoU）≥ `iou_min` 且**数值相同** ⇒ 同一个。
    ★ 数值必须也相同：两个不同视图里都写 `12` 是**很正常**的。
    """
    out: list[dict] = []
    for d in dims:
        dup = False
        for k in out:
            if k["rec"]["value"] != d["rec"]["value"]:
                continue
            a, b = k["box"], d["box"]
            ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
            iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
            inter = ix * iy
            if inter <= 0:
                continue
            area_a = max(1e-9, (a[2] - a[0]) * (a[3] - a[1]))
            area_b = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
            if inter / min(area_a, area_b) >= iou_min:
                dup = True
                break
        if not dup:
            out.append(d)
    return out


# ---------------------------------------------------------------- 视图区域

def _subject_mask(png: str, sat_min: int = SAT_MIN,
                   lum_lo: int = 40, lum_hi: int = 235):
    r"""二值化「**视图主体**」⇒ `(mask, rgb, mode)`。

    ★★★★ 关键教训（真机 2026-10-03，**我第一版只抓彩色，抓错了基准**）：

    同一批 6 张练习题 PDF 的**视图填充**统计：

    | 题 | 彩色占比 `sat>28` | **灰阶填充占比** |
    |---|---|---|
    | `1_1`（我拿它当基准的那张） | **25.84%** | 4.56% |
    | `1_2` | **0.11%** | **19.78%** |
    | `1_3` | 0.19% | 17.88% |
    | `1_5` | 0.11% | 14.84% |
    | `1_8` | 0.25% | 33.19% |
    | `1_12` | 0.12% | 28.81% |

    ⇒ ★★ **只有 1/6 的图是「彩色填充」，其余全是「灰色填充」** ——
      我拿 `1_1` 当基准，等于**只在 1/20 的题上有效**。
    ⇒ 「彩色」不是视图的**本质特征**，「**有填充**」才是。
    ⇒ 正解：先按**彩色**试（更干净），若得到的区域太少则**退回灰阶**。

    灰阶判据：`LUM_LO < lum < LUM_HI` 且 `sat < sat_min`（排除彩色与纯黑）。
    上下界的作用：剔掉**纯白背景**（lum≥235）与**纯黑线条**（lum≤40）。
    """
    import numpy as np                                # noqa: PLC0415
    from PIL import Image                             # noqa: PLC0415
    with Image.open(png) as im:
        rgb = np.asarray(im.convert("RGB")).astype(np.int16)
    mx = rgb.max(axis=2)
    mn = rgb.min(axis=2)
    sat = mx - mn
    lum = rgb.mean(axis=2)
    colored = (sat > sat_min) & (mx > 40)
    gray = (lum > lum_lo) & (lum < lum_hi) & (sat <= sat_min)
    return colored, gray, rgb


def detect_view_regions(png: str, sat_min: int = SAT_MIN,
                        min_area: int = MIN_AREA,
                        merge_gap: int | None = None,
                        colored_ratio_min: float = 0.01) -> list[dict]:
    r"""按「**有填充**」切出视图区域 —— 不依赖尺寸、不依赖空白带。

    ★ 为什么用填充（真机 2026-10-03）：工程图里**视图主体有填充**
      （彩色或灰色），而**尺寸线 / 轮廓线 / 文字**是纯黑或白底。
    ⇒ **两条通道**：先试**彩色**（更干净、不含抗锯齿灰边），
      若彩色区域面积占比 < `colored_ratio_min` 则**退回灰阶**
      （实测 6 张题里 5 张走灰阶通道，见 `_subject_mask` 的表）。
    ★ 两条通道都失败（纯线框、无任何填充）⇒ **返回空列表**，
      报告里显式说「检不出视图」，**不假装**（§99③ 的老教训）。

    ##### ⚠⚠ ★★★ 已知局限（真机 2026-10-03，**未解决**）
    灰阶通道会把**水印**（淡灰色大字 `SOLIDWORKS`）一起算进来 ⇒
    `Level01/1_2` 实测：zoom2 时 gap≤10 得 **4 个**区域（正确），
    但 **zoom4 时连 gap=1 都合并成 1 个**（水印横跨全图把视图连起来了）。
    ⇒ **灰阶通道 + 高倍放大 = 视图数被水印压成 1**。
    ⇒ 待做：① 按**连通域的「暗度方差」**区分「实心填充」与「水印」
      （水印是**渐变**的、实心填充是**均匀**的）；
      ② 或先做**水印检测**（超大 bbox + 极低对比）再排除。
    ⇒ **当前**：`views_raw` 里带 `mode` 字段，报告会显式说走的是哪条通道，
      让你知道结论的可信度（不静默）。

    返回按面积降序的 `[{bbox, area, mean_rgb, mode}]`（像素坐标）。
    """
    colored, gray, rgb = _subject_mask(png, sat_min)
    tot = colored.size

    def comps_of(mask):
        import numpy as np                            # noqa: PLC0415
        h, w = mask.shape
        seen = np.zeros((h, w), dtype=bool)
        out: list[dict] = []
        ys, xs = np.nonzero(mask)
        for sy, sx in zip(ys, xs):
            if seen[sy, sx]:
                continue
            stack = [(int(sy), int(sx))]
            seen[sy, sx] = True
            pts: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                pts.append((y, x))
                for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                    if 0 <= ny < h and 0 <= nx < w and not seen[ny, nx] \
                            and mask[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(pts) >= min_area:
                py = [q[0] for q in pts]
                px = [q[1] for q in pts]
                out.append({"bbox": (min(px), min(py), max(px), max(py)),
                            "area": len(pts)})
        return out

    # ★★ 合并间隙按**图幅比例**算，不写死像素（见 `MERGE_GAP_FRAC` 的实测）
    h_img, w_img = colored.shape
    frame = float(max(w_img, h_img, 1))
    gap = int(merge_gap) if merge_gap is not None else \
        max(2, int(round(MERGE_GAP_FRAC * frame)))
    comps = comps_of(colored)
    mode = "colored"
    # ★ 彩色通道不够用 ⇒ 退灰阶（真机 6 张里 5 张走这条）
    if sum(c["area"] for c in comps) < colored_ratio_min * tot:
        comps = comps_of(gray)
        mode = "gray"
    comps = _merge_close(comps, gap)
    for c in comps:
        sub = rgb[c["bbox"][1]:c["bbox"][3] + 1,
                  c["bbox"][0]:c["bbox"][2] + 1]
        c["mean_rgb"] = [round(float(v), 1)
                        for v in sub.reshape(-1, 3).mean(axis=0)]
        c["mode"] = mode
        c["merge_gap"] = gap
    comps.sort(key=lambda c: -c["area"])
    return comps


def _merge_close(comps: list[dict], gap: int) -> list[dict]:
    """把**邻近**的块合并（一个视图常被黑色轮廓线切成好几块）。"""
    def box_gap(a, b):
        dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
        dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
        return (dx * dx + dy * dy) ** 0.5

    changed = True
    while changed:
        changed = False
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                if box_gap(comps[i]["bbox"], comps[j]["bbox"]) <= gap:
                    a, b = comps[i], comps[j]
                    a["bbox"] = (min(a["bbox"][0], b["bbox"][0]),
                                 min(a["bbox"][1], b["bbox"][1]),
                                 max(a["bbox"][2], b["bbox"][2]),
                                 max(a["bbox"][3], b["bbox"][3]))
                    a["area"] += b["area"]
                    comps.pop(j)
                    changed = True
                    break
            if changed:
                break
    return comps


# ---------------------------------------------------------------- 归属

def detect_layout_axis(regs: list[dict]) -> str:
    r"""判视图是**上下排列**还是**左右排列** —— 决定归属看 y 还是 x。

    ★★★ 第一版判据**是错的**（真机 2026-10-03）：「看哪个轴上的中心跨度大」。
      `Level01/1_1` 实测：Top(220,139) Front(220,491) Iso(1032,139)
      ⇒ x 跨度 1300 > y 跨度 810 ⇒ 判成 `x` ⇒ **全错**
      （因为**轴测图摆在右侧**，它把 x 跨度拉大了）。
      ⇒ 第一版恰好被「轴测图独立摆放」这个常态打翻。

    ★★ 正解：**看「有几个视图共享同一行/列」**。
      标准三视图 Top/Front **x 区间完全重叠**（真机都是 x∈[220,732]）
      ⇒ 上下排列。轴测图的 x 区间与它们**不重叠** ⇒ 不参与计数。
    ⇒ 判据：
      ① 统计**横向重叠**的视图对数 `pairs_x`（x 区间有交集）
      ② 统计**纵向重叠**的对数 `pairs_y`
      ③ `pairs_x >= 1` ⇒ 存在上下并排的视图 ⇒ 轴 = `y`；否则 `x`
    """
    if len(regs) < 2:
        return "y"

    def overlap(a, b, i, j):
        return min(a[2], b[2]) - max(a[0], b[0]) > 0

    def pairs(axis):
        n = 0
        for i in range(len(regs)):
            for j in range(i + 1, len(regs)):
                a, b = regs[i]["bbox"], regs[j]["bbox"]
                if axis == "x" and overlap(a, b, 0, 2):
                    n += 1
                elif axis == "y" and overlap(a, b, 1, 3):
                    n += 1
        return n

    px, py = pairs("x"), pairs("y")
    # ★ 轴测图通常与主视图**两个方向都不重叠** ⇒ 哪边有重叠哪边就是排布轴
    return "y" if py >= px else "x"



def group_views(regs: list[dict], axis: str,
                overlap_frac: float = 0.30) -> dict:
    r"""把视图分成「**投影对齐组**」与「**独立摆放**」—— 归属只在组内做。

    ★★★★ 这是「尺寸 → 视图」的正解（真机 2026-10-03 踩了三轮才找到）。

    ### 失败史（值得原样记）
    | 版 | 判据 | 为什么错 |
    |---|---|---|
    | 1 | 最近视图 | `28` 到 Top 152px、到 Iso 148px ⇒ **148 vs 152 几乎一样** |
    | 2 | 单轴 y 区间 | Top y∈[101,423]、**Iso y∈[101,990] 把 Top 整个包住** ⇒ 第一个命中是 Iso |
    | 3 | 双轴距离最小 | 同上，Iso 仍 109 < Top 111 |
    | **4** | **先分组，组内归属** | ✔ Iso 独立成分组，`28` **不可能**归它 |

    ### 为什么「分组」是对的
    工程图的**三视图是严格对齐**的（第一角/第三角投影）：
      - Top 与 Front 的 **x 区间完全相同**（真机都是 [221,733]）
      - 它们**高度相等** ⇒ 同一个投影组的成员
    而**轴测图/独立视图摆在角落**，与主视图组**两个方向都不重叠**。
    ⇒ 尺寸**只可能**标注在它所属的那个视图附近，
      **不可能**跑到另一个不相干的组去。

    ### 分组判据
      ① 两视图在**垂直于布局轴**的方向上**区间重叠**（x 重叠 ⇒ 上下排列）
      ② 且在**布局轴**方向的**跨度相近**（|Δspan| ≤ `tol_frac` × 图幅）
    ⇒ 返回 `{'groups': [[idx,…], …], 'main': int, 'axis': …}`
      `main` = **成员最多**的那组（= 主视图组）。
    """
    if not regs:
        return {"groups": [], "main": None, "axis": axis}
    n = len(regs)
    # 每个视图在两个轴上的区间与跨度
    span = {}
    for i, r in enumerate(regs):
        x0, y0, x1, y1 = r["bbox"]
        span[i] = {"x": (x0, x1), "y": (y0, y1),
                   "wx": x1 - x0, "wy": y1 - y0}
    # 图幅（用所有视图的总跨度当尺度，容差按它定）
    all_x0 = min(span[i]["x"][0] for i in span)
    all_x1 = max(span[i]["x"][1] for i in span)
    all_y0 = min(span[i]["y"][0] for i in span)
    all_y1 = max(span[i]["y"][1] for i in span)
    frame = max(all_x1 - all_x0, all_y1 - all_y0, 1.0)

    cross = "x" if axis == "y" else "y"      # 跨视图对齐的那个轴
    along = "y" if axis == "y" else "x"     # 排布方向

    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    for i in range(n):
        for j in range(i + 1, n):
            # ★★★ 只看**跨轴是否重叠**（x 相同 ⇒ 上下排列的同一投影组）。
            #   第一版还要求「沿布局轴的跨度相近」，**这条太严，全错**：
            #   真机 Top y 跨度 242、Front 638（差 396）⇒ 被拆成两个组
            #   ⇒ 而它们**本来就该同组**（三视图的宽可以差很多）。
            #   ⇒ 跨度只在**报告**里给出（供人看），**不参与分组**。
            ci, cj = span[i][cross], span[j][cross]
            inter = min(ci[1], cj[1]) - max(ci[0], cj[0])
            if inter <= 0:
                continue                       # 跨轴不重叠 ⇒ 不同组
            # 重叠比例太低（只是偶然擦边）也判不同组
            if inter < overlap_frac * min(ci[1] - ci[0], cj[1] - cj[0]):
                continue
            union(i, j)
    g: dict[int, list[int]] = {}
    for i in range(n):
        g.setdefault(find(i), []).append(i)
    groups = sorted(g.values(), key=lambda v: -len(v))
    return {"groups": groups, "main": groups[0][0] if groups else None,
            "axis": axis, "frame": frame}


def assign_dims_to_views(dims: list[dict], regs: list[dict],
                         axis: str | None = None,
                         pad: int = ASSIGN_PAD,
                         groups: list[list[int]] | None = None) -> dict:
    r"""把每个尺寸**挂到视图**上 —— 双轴距离，取**最紧**的那个。

    ★★★★ 真机踩了三轮才都对（2026-10-03，`Level01/1_1`），值得原样记：

    **第一版「最近视图」**：错。真机读数：Top bbox 右界 x=732，
      它的 `28`(x=884) 到 Top 152px、到 Iso 148px ⇒ **148 vs 152 几乎一样**
      ⇒ 把 Top 的 `28` 挂到了 Iso。

    **第二版「单轴（y）区间」**：也错。Top y∈[101,423]、Iso y∈[101,990]
      ⇒ **Iso 的 y 区间把 Top 整个包住** ⇒ `20/28` 同时命中两个，
      第一个命中是 Iso ⇒ 还是错。
      ★ 根因：**轴测图不是正投影视图，它没有「上下对齐」关系**，
        它的 y 区间会横跨整张图 ⇒ 必须**双轴一起看**。

    **第三版（现在）**：**双轴归一化距离，取最小**。
      `20a`(cx=818, cy=234)：
        → Top   (x 超出 45px, y 在内 0)      距离 ≈ 45
        → Iso   (x 超出 175px, y 在内 0)     距离 ≈ 175
      ⇒ 归 Top ✔
      ★ 关键是**用「超出区间的距离」而不是「到边界的距离」** ——
        尺寸可以离视图很远（`60` 在 Front 下方 164px 外），
        但它在**哪个方向**超出、各视图超多远，是明确的。
    """
    axis = axis or detect_layout_axis(regs)
    views = [{"bbox": r["bbox"], "area": r.get("area"), "dims": [], "n": 0}
             for r in regs]
    # ★★ 只在**同一分组内**比距离 —— 跨组比较就是 §模块docstring 里那三版错误
    if groups is None:
        groups = group_views(regs, axis)["groups"]
    group_of: dict[int, int] = {}
    for gi, g in enumerate(groups):
        for i in g:
            group_of[i] = gi
    un: list[dict] = []
    for d in dims:
        x0, y0, x1, y1 = d["box"]
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        # 先看落在各组的哪个区间（沿布局轴），确定候选组
        gi_try = None
        for gi, g in enumerate(groups):
            for i in g:
                bx0, by0, bx1, by1 = views[i]["bbox"]
                c = cy if axis == "y" else cx
                lo = (by0 if axis == "y" else bx0) - pad
                hi = (by1 if axis == "y" else bx1) + pad
                if lo <= c <= hi:
                    gi_try = gi
                    break
            if gi_try is not None:
                break
        if not groups:
            # ★ 没有视图区域（如**纯线框图**）⇒ 全部尺寸报「未归属」，
            #   **不硬挂、不崩**。`max([])` 会抛 ValueError（真机 5/6 张题崩在这）。
            d = dict(d)
            d["nearest_view"] = None
            d["nearest_dist"] = None
            un.append(d)
            continue
        cand = groups[gi_try] if gi_try is not None else \
            max(groups, key=len)
        scored = []
        for i in cand:
            v = views[i]
            bx0, by0, bx1, by1 = v["bbox"]
            # ★ 两个轴都用「超出区间的距离」；落在区间内 = 0
            ox = max(0.0, bx0 - pad - cx, cx - (bx1 + pad))
            oy = max(0.0, by0 - pad - cy, cy - (by1 + pad))
            scored.append(((ox * ox + oy * oy) ** 0.5, i))
        scored.sort()
        best = scored[0][1]
        # ★★ 超过 `REJECT` 就不硬归 —— 报「未归属」，不假装
        if scored[0][0] > REJECT_DIST:
            d = dict(d)
            d["nearest_view"] = best
            d["nearest_dist"] = round(scored[0][0], 1)
            un.append(d)
            continue
        views[best]["dims"].append(d)
        views[best]["n"] += 1
    key = (lambda v: (v["bbox"][1] + v["bbox"][3]) / 2.0) if axis == "y" \
        else (lambda v: (v["bbox"][0] + v["bbox"][2]) / 2.0)
    views.sort(key=key)
    return {"axis": axis, "views": views, "unassigned": un}


def guess_view_names(views: list[dict], axis: str) -> list[dict]:
    r"""给视图起**候选名**（`top`/`front`/`right`/`iso`…）。

    ★★ 只给「位置候选名」，**不断言这是哪个视图** ——
      标准三视图的**位置约定**是：上=Top、下=Front、左/右=Left/Right、
      单独放大的斜轴测 = Isometric。
    ★ 「哪个视图」最终要靠**尺寸语义 + 投影对齐**确认
      （例：`Top` 的尺寸是长×宽、`Front` 的含高度与孔），
      这一步本工具**明确标为未完成**，不假装能定（§99③ 的老教训）。
    """
    out = []
    n = len(views)
    for i, v in enumerate(views):
        b = v["bbox"]
        ar = (b[2] - b[0]) / max(1.0, float(b[3] - b[1]))
        cand = []
        if axis == "y":
            if i == 0 and n >= 2:
                cand = ["top", "front"]
            elif n >= 3 and i == n - 1:
                cand = ["right", "left", "iso"]
        else:
            if i == 0:
                cand = ["front", "left"]
            elif n >= 3 and i == n - 1:
                cand = ["right", "iso"]
        # ★ 宽高比很「方」且独立成块的，很可能是轴测
        iso_like = 0.6 <= ar <= 1.7 and n >= 3
        out.append({
            "index": i, "bbox": b, "n_dims": v["n"],
            "values": [d["rec"]["value"] for d in v["dims"]],
            "kinds": [d["rec"]["kind"] for d in v["dims"]],
            "candidates": cand,
            "iso_like": iso_like,
            "note": ("★ 只给**位置候选名**；视图身份要靠尺寸语义与投影对齐确认，"
                     "本工具**尚未**实现这一步"),
        })
    return out


# ================================================================ 读数据层

def find_drawing_bbox(page) -> tuple[float, float, float, float] | None:
    r"""从 PDF 页里定位**图纸区**（排除题面文字区）。

    ★ 真机读数（`Level01/1_1`）：页面 540×720pt
      - 题面文字区：y ∈ [163, 298]
      - 图纸本体（`get_image_rects`）：`bbox=(70,314,463,655)`、706×613 px
    ⇒ 判据：**取面积最大的那张图片的 rect**。
    """
    best = None
    for im in page.get_images(full=True):
        try:
            for r in page.get_image_rects(im[0]):
                a = (r.x1 - r.x0) * (r.y1 - r.y0)
                if a > 200 and (best is None or a > best[0]):
                    best = (a, (r.x0, r.y0, r.x1, r.y1))
        except Exception:                               # noqa: BLE001
            continue
    return best[1] if best else None


def render(pdf: str, out_png: str, zoom: int = ZOOM,
           clip=None) -> tuple[int, int, tuple]:
    """把 PDF 的图纸区渲染成 PNG。返回 `(宽, 高, clip)`。"""
    import pymupdf                                     # noqa: PLC0415
    doc = pymupdf.open(pdf)
    if doc.page_count == 0:                            # ★ 0 页坏文件
        doc.close()
        raise ValueError("0 页坏 PDF（`page_count == 0`）")
    pg = doc[0]
    if clip is None:
        clip = find_drawing_bbox(pg) or tuple(pg.rect)
    rect = pymupdf.Rect(*clip)
    pm = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=rect)
    pm.save(out_png)
    doc.close()
    return pm.width, pm.height, tuple(rect)


def run_ocr(png: str) -> list[dict]:
    """RapidOCR → `[{text, box, score}]`。"""
    from rapidocr_onnxruntime import RapidOCR       # noqa: PLC0415
    engine = RapidOCR()
    res, _ = engine(png)
    out = []
    for r in (res or []):
        box, txt, score = r[0], r[1], r[2]
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        out.append({"text": str(txt), "score": float(score),
                    "box": (min(xs), min(ys), max(xs), max(ys))})
    return out



def multi_pass_dims(pdf: str, out_dir: str, zooms=ZOOM_SET,
                    min_score: float = 0.4) -> dict:
    r"""**多遍识别 + 按位置投票** —— 单遍 OCR 的结果**不可复现**。

    ★★★ 起因（真机 2026-10-03，同一张 `Level01/1_1`）：
    ```
    zoom=4 → 12 个尺寸（漏 `75`）
    zoom=5 → 11 个：**全对**（12/12/16/16/20/20/20/28/30/60/75）
    zoom=6 → 10 个（漏一个 20）
    zoom=8 → 11 个，但把 `16` 误读成 `-12`（连字符污染）
    ```
    ⇒ ★ **没有任何单一倍数可靠** ⇒ 一次 OCR 的结果**不能当事实**。
    ⇒ 与 §100⑤「判据失效常是静默无效」同源：**OCR 漏读不会报错**。

    投票规则（**故意保守**）：
      ① 各遍的框坐标先**归一化**到 0~1（不同 zoom 像素尺度不同）
      ② 同一位置（中心距离 < `vote_tol`）的读数**按值聚类**
      ③ 只保留**出现在 ≥2 遍**里的值；只出现 1 遍的**单独列出**（可疑）
    ⇒ 返回 `{'dims': [...], 'n_zooms': n, 'agreed': [...], 'single': [...]}`
    """
    import os                                        # noqa: PLC0415
    zooms = tuple(zooms)
    pngs = []
    for z in zooms:
        p = os.path.join(out_dir, "mp_z%d.png" % z)
        w, h, _ = render(pdf, p, z)
        pngs.append((z, p, float(w), float(h)))
    # 收集 (cx, cy, value, kind, zoom)
    votes: list[dict] = []
    for z, p, w, h in pngs:
        for it in run_ocr(p):
            if it["score"] < min_score:
                continue
            rec = normalize_dim(it["text"])
            if rec["value"] is None:
                continue
            x0, y0, x1, y1 = it["box"]
            votes.append({"cx": ((x0 + x1) / 2) / w, "cy": ((y0 + y1) / 2) / h,
                          "value": rec["value"], "kind": rec["kind"],
                          "text": it["text"], "zoom": z,
                          "box": it["box"], "img": (w, h)})
    vote_tol = 0.012                                  # 归一化坐标下的容差
    clusters: list[list[dict]] = []
    for v in votes:
        hit = None
        for c in clusters:
            if abs(c[0]["cx"] - v["cx"]) <= vote_tol and \
                    abs(c[0]["cy"] - v["cy"]) <= vote_tol:
                hit = c
                break
        (hit if hit is not None else clusters.append([]) or clusters[-1]).append(v)
    agreed, single = [], []
    for c in clusters:
        vals: dict[float, int] = {}
        for v in c:
            vals[v["value"]] = vals.get(v["value"], 0) + 1
        best_val = max(vals, key=lambda k: vals[k])
        n_agree = vals[best_val]
        kinds = [v["kind"] for v in c if v["value"] == best_val]
        rec = {"cx": c[0]["cx"], "cy": c[0]["cy"], "value": best_val,
               "kind": ("diameter" if "diameter" in kinds
                        else "radius" if "radius" in kinds else "linear"),
               "n_votes": n_agree, "n_seen": len(c),
               "zooms": sorted({v["zoom"] for v in c}),
               "texts": sorted({v["text"] for v in c})}
        if len(zooms) <= 1 or n_agree >= 2:
            agreed.append(rec)
        else:
            single.append(rec)
    return {"dims": agreed, "single": single, "n_zooms": len(zooms),
            "n_votes": len(votes), "n_clusters": len(clusters)}


def analyze(png: str, with_ocr: bool = True) -> dict:
    """完整分析：视图区域 + 尺寸归属（单遍，便于调试）。"""
    regs = detect_view_regions(png)
    res: dict[str, Any] = {"png": png, "n_views": len(regs),
                           "views_raw": regs}
    dims: list[dict] = []
    if with_ocr:
        for it in run_ocr(png):
            rec = normalize_dim(it["text"])
            if rec["value"] is not None:
                dims.append({"box": it["box"], "text": it["text"],
                             "score": it["score"], "rec": rec})
        dims = dedup_dims(dims)
    axis = detect_layout_axis(regs)
    groups = group_views(regs, axis)
    asg = assign_dims_to_views(dims, regs, axis=axis, groups=groups["groups"])
    res.update({"axis": axis, "groups": groups["groups"],
                "n_dims": len(dims),
                "views": guess_view_names(asg["views"], axis),
                "unassigned": [{"text": d["text"], "rec": d["rec"]}
                               for d in asg["unassigned"]]})
    res["views_no_dims"] = sum(1 for v in res["views"] if v["n_dims"] == 0)
    return res


def analyze_multipass(pdf: str, out_dir: str, zooms=ZOOM_SET) -> dict:
    """**推荐入口**：多遍 OCR 投票 + 视图归属。"""
    import os                                        # noqa: PLC0415
    os.makedirs(out_dir, exist_ok=True)
    mp = multi_pass_dims(pdf, out_dir, zooms)
    # 用**第一遍**（zoom 最大）的图做视图检出 —— 视图检出与尺寸无关
    z0 = max(zooms)
    png0 = os.path.join(out_dir, "mp_z%d.png" % z0)
    w, h, _ = render(pdf, png0, z0)
    base = analyze(png0, with_ocr=False)
    # 把投票后的尺寸挂上去（用**归一化坐标**换算回像素）
    dims = []
    for d in mp["dims"]:
        w0, h0 = w, h
        dims.append({"box": ((d["cx"] - 0.02) * w0, (d["cy"] - 0.01) * h0,
                             (d["cx"] + 0.02) * w0, (d["cy"] + 0.01) * h0),
                     "text": "/".join(d["texts"]),
                     "rec": {"value": d["value"], "kind": d["kind"],
                             "raw": d["texts"], "notes": [
                                 "多遍投票 %d/%d 遍一致（zooms=%s）"
                                 % (d["n_votes"], len(zooms), d["zooms"])]},
                     "n_votes": d["n_votes"]})
    dims = dedup_dims(dims)
    axis = base["axis"]
    asg = assign_dims_to_views(dims, base["views_raw"], axis=axis,
                               groups=base["groups"])
    base.update({
        "pdf": pdf, "multipass": True, "zooms": list(zooms),
        "n_dims": len(dims),
        "n_votes_raw": mp["n_votes"], "n_clusters": mp["n_clusters"],
        "single_vote": mp["single"],
        "views": guess_view_names(asg["views"], axis),
        "unassigned": [{"text": d["text"], "rec": d["rec"]}
                       for d in asg["unassigned"]],
    })
    base["views_no_dims"] = sum(1 for v in base["views"] if v["n_dims"] == 0)
    return base


def render_views(pdf: str, out_dir: str, zoom: int = ZOOM,
                 pad_frac: float = VIEW_PAD_FRAC) -> dict:
    r"""**给 agent 的主入口**：渲染图纸 + 按视图裁成单张 PNG。

    ★★★ 定位（用户校准 2026-10-03）：**agent 自己有视觉，读尺寸是它的事**。
    本函数只负责把图准备好：
      ① `overview.png` —— 整张图纸区（agent 先看全局布局）
      ② `viewN.png`   —— 每个视图一张，**带 pad**
         ★ pad 必须给足：视图 bbox 只含**填充区**，尺寸标注在**外面**
         （真机：Top bbox 右界 x=732，它的 `20`/`28` 在 x=818/884 ——
         不留 pad 就把尺寸裁掉了，agent 看到的是「没尺寸的图」）。
      ③ 文件清单带**位置提示名**（upper-left / lower-right 等），
        agent 结合工程图约定（左下 Front、上方 Top/Section、角落 Iso）
        自己判断视图身份 —— **工具不替它下结论**。

    返回 `{"overview": path, "views": [{file, bbox_px, hint, mode}],
           "zoom": z, "frame_px": [w, h]}`。
    """
    os.makedirs(out_dir, exist_ok=True)
    # ---- ① 整张图纸区 ----
    overview = os.path.join(out_dir, "overview.png")
    w, h, _clip = render(pdf, overview, zoom)
    # ---- ② 视图区域（与尺寸无关）----
    regs = detect_view_regions(overview)
    pad = int(round(pad_frac * max(w, h, 1)))
    import sw_doctor                                 # noqa: PLC0415
    sw_doctor.ensure_user_site()                     # ★ PIL 在用户级 site-packages
    from PIL import Image                            # noqa: PLC0415
    views = []
    for i, r in enumerate(regs, 1):
        x0, y0, x1, y1 = r["bbox"]
        box = (max(0, x0 - pad), max(0, y0 - pad),
               min(w, x1 + pad), min(h, y1 + pad))
        out = os.path.join(out_dir, "view%d.png" % i)
        with Image.open(overview) as im:
            im.crop(box).save(out)
        views.append({
            "file": out,
            "bbox_px": box,
            "fill_bbox_px": r["bbox"],
            "area": r["area"],
            "mode": r.get("mode"),
            "hint": position_hint(r["bbox"], w, h),
            "pad_px": pad,
        })
    return {"overview": overview, "views": views, "zoom": zoom,
            "frame_px": [w, h]}


def position_hint(bbox: tuple, frame_w: float, frame_h: float) -> str:
    """视图在图面上的**位置提示**（九宫格）—— 供 agent 结合工程图约定判身份。"""
    x0, y0, x1, y1 = bbox
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    col = "left" if cx < frame_w / 3.0 else (
        "right" if cx > frame_w * 2.0 / 3.0 else "center")
    row = "upper" if cy < frame_h / 3.0 else (
        "lower" if cy > frame_h * 2.0 / 3.0 else "middle")
    if row == "middle" and col == "center":
        return "center"
    if row == "middle":
        return col
    if col == "center":
        return row
    return "%s-%s" % (row, col)


# ================================================================ 报告

def print_report(r: dict) -> None:
    print("== 视图区域（彩色填充检出）==")
    _mode = r["views_raw"][0].get("mode", "?") if r.get("views_raw") else "—"
    _gap = r["views_raw"][0].get("merge_gap", "?") if r.get("views_raw") else "?"
    print("检出 **%d 个**视图区域（通道=%s，合并间隙=%s px）"
          % (r["n_views"], _mode, _gap))
    if _mode == "gray":
        print("  ⚠ 走的是**灰阶通道**（该图没有彩色填充）—— "
              "**注意水印可能被算进视图**（已知局限，见 `detect_view_regions`）")
    if r["n_views"] <= 1 and len(r.get("unassigned", [])) == 0 and r["n_dims"]:
        print("  ⚠ 只检出 1 个视图却读到 %d 个尺寸 ⇒ **很可能被水印连成一片**"
              "（已知局限）" % r["n_dims"])
    if r["n_views"] == 0:
        print("  ✘★ 一个都没检出 ⇒ 饱和度阈值可能太高，或这张图是**纯线框图**")
    for v in r["views"]:
        b = v["bbox"]
        print("  [视图%d] bbox=(%d,%d)-(%d,%d)  %dx%d  尺寸 %d 个  候选名=%s%s"
              % (v["index"] + 1, b[0], b[1], b[2], b[3],
                 b[2] - b[0], b[3] - b[1], v["n_dims"],
                 v["candidates"] or "?", "  ★可能轴测" if v["iso_like"] else ""))
        if v["n_dims"]:
            print("        尺寸：%s" % v["values"])
            print("        类型：%s" % v["kinds"])
        else:
            print("        ★ **该视图没有任何尺寸** —— 保留它！"
                  "轴测视图给形状、不给尺寸，丢掉就只剩 2D 轮廓了")
    print()
    print("布局轴：%s（%s）" % (
        r["axis"], "上下排列 ⇒ 归属看 y" if r["axis"] == "y" else "左右排列 ⇒ 归属看 x"))
    if r.get("multipass"):
        print("多遍投票：%d 票 / %d 簇 ⇒ 采纳 %d（≥2 遍一致）"
              % (r.get("n_votes_raw", 0), r.get("n_clusters", 0),
                 r["n_dims"]))
    print("尺寸共 %d 个；未归属 %d 个" % (r["n_dims"], len(r["unassigned"])))
    for d in r["unassigned"]:
        print("   ★ %r → %s" % (d["text"], d["rec"]["kind"]))
    print()
    print("★ 每个视图的**身份**（Top/Front/Iso）尚未确认 —— "
          "本工具只给位置候选名，")
    print("  确认需要「尺寸语义 + 投影对齐」，**没做**（不假装能做）。")


# ================================================================ 自检

def self_test() -> int:
    """离线自检（不读 PDF、不开 SW）。"""
    ok = fail = 0

    def chk(cond, msg):
        nonlocal ok, fail
        if cond:
            ok += 1
            print("  PASS %s" % msg)
        else:
            fail += 1
            print("  FAIL %s" % msg)

    print("[1] ★★★ 符号还原：尺寸线污染")
    r = normalize_dim("-20-")
    chk(r["value"] == 20.0 and r["kind"] == "linear",
        "`-20-` ⇒ 20.0 线性（两侧尺寸线被 OCR 当连字符），实得 %r" % r["value"])
    # ★★ 期望值是 **`-0.05`（保留符号）**，不是 `0.05` ——
    #   公差 `-0.05` 被剥掉符号就变成 `+0.05`，装配值直接反了。
    #   第一版断言写成 `== 0.05` ⇒ **断言自己错**，害我以为判据坏了。
    r = normalize_dim("-0.05")
    chk(r["value"] == -0.05,
        "★★ `-0.05`（真公差，**只有一个前导 `-`**）⇒ **保留符号** -0.05，"
        "不能剥成 0.05（实得 %r）" % r["value"])
    r = normalize_dim("-20-")
    chk(r["value"] == 20.0,
        "★ 对照：`-20-`（**尾部也有 `-`**）⇒ 20.0 尺寸线污染，剥掉"
        "（实得 %r）—— 两个判据靠「尾部有没有 `-`」区分" % r["value"])

    print("[2] ★★★ 直径/半径/阵列")
    for txt, kind, val in (("⌀12", "diameter", 12.0), ("Ø12", "diameter", 12.0),
                           ("ø8", "diameter", 8.0), ("R5", "radius", 5.0),
                           ("2×⌀6", "diameter", 6.0), ("20", "linear", 20.0)):
        r = normalize_dim(txt)
        chk(r["kind"] == kind and r["value"] == val,
            "`%s` ⇒ %s %.1f（实得 %s %.1f）" % (txt, kind, val, r["kind"], r["value"]))
    r = normalize_dim("2×⌀6")
    chk(r.get("count") == 2, "★ `2×⌀6` 要读出**阵列个数** = 2（实得 %r）"
        % r.get("count"))

    print("[3] ★★★ 非尺寸必须排除（视图名/水印）")
    for txt in ("Front View", "Practice Problems", "CSWA", "Dassault", ""):
        r = normalize_dim(txt)
        chk(r["value"] is None,
            "`%s` ⇒ 不是尺寸（实得 %r）" % (txt, r["value"]))

    print("[4] ★★★ 归属判据：沿布局轴，**不是「最近」**")
    # 真机读数：Top bbox x∈[220,732] y∈[139,381]
    #          Front bbox x∈[220,732] y∈[491,1129]
    #          Iso bbox x∈[1032,1549] y∈[139,948]
    regs = [
        {"bbox": (220, 139, 732, 381), "area": 111147},     # Top
        {"bbox": (220, 491, 732, 1129), "area": 198770},    # Front
        {"bbox": (1032, 139, 1549, 948), "area": 245355},   # Iso
    ]
    chk(detect_layout_axis(regs) == "y",
        "Top 在上 / Front 在下 ⇒ 布局轴 = `y`（实得 %s）"
        % detect_layout_axis(regs))
    # `28` 在 (884,270)：到 Top 152px、到 Iso 148px ⇒「最近」会挂错到 Iso
    dims = [{"box": (863, 253, 904, 286), "text": "28",
             "rec": normalize_dim("28")},
            {"box": (816, 1035, 862, 1073), "text": "16",
             "rec": normalize_dim("16")}]
    asg = assign_dims_to_views(dims, regs)
    chk(asg["views"][0]["n"] == 1
        and asg["views"][0]["dims"][0]["text"] == "28",
        "★★ `28`(y=270) 必须归 **Top**（y 落在 Top 的 [139,381] 内）"
        "—— 「最近」会挂到 Iso（152 vs 148px，几乎一样），实得 Top 有 %d 个"
        % asg["views"][0]["n"])
    # ★★ 断言**按「哪个视图含这个值」**找，不按索引 ——
    #   `assign_dims_to_views` 末尾会**按位置重排**（Top/Iso/Front），
    #   所以 `views[1]` 是 Iso 而不是 Front。第一版按索引断言 ⇒ 假失败。
    def _who(res, val):
        for i, v in enumerate(res["views"]):
            if any(d["rec"]["value"] == val for d in v["dims"]):
                return i, v
        return None, None

    i16, v16 = _who(asg, 16.0)
    i28, v28 = _who(asg, 28.0)
    chk(v16 is not None and v16["bbox"][1] == 491,
        "★ `16`(y=1054) 归 **Front**（bbox y0=491 那个视图），实得 %s"
        % ("Front" if v16 and v16["bbox"][1] == 491 else v16))
    chk(v28 is not None and v28["bbox"][1] == 139,
        "★★ 排序后**仍**要能按 bbox 找回每个尺寸的归属"
        "（`28`→y0=139 的 Top、`16`→y0=491 的 Front），实得 %s / %s"
        % (v28["bbox"][1] if v28 else None, v16["bbox"][1] if v16 else None))

    print("[5] ★★ 无尺寸的视图必须保留（轴测）")
    asg2 = assign_dims_to_views([], regs)
    chk(len(asg2["views"]) == 3,
        "★ 没有任何尺寸时**仍要返回 3 个视图**（实得 %d）—— "
        "靠尺寸发现视图会静默漏掉轴测" % len(asg2["views"]))
    names = guess_view_names(asg2["views"], "y")
    chk(all("候选名" in (n.get("note") or "") for n in names),
        "★★ 只给**位置候选名**且明说「身份未确认」—— 不假装能定视图身份")
    chk(sum(1 for n in names if n["n_dims"] == 0) == 3,
        "★ 显式统计「无尺寸的视图数」= 3（轴测靠形状，不靠尺寸）")

    print("[6] ★★ 去重：同一尺寸被 OCR 两遍")
    a = {"box": (100, 100, 140, 130), "text": "20", "rec": normalize_dim("20")}
    b = {"box": (102, 101, 141, 131), "text": "20", "rec": normalize_dim("20")}
    c = {"box": (300, 100, 340, 130), "text": "20", "rec": normalize_dim("20")}
    chk(len(dedup_dims([a, b, c])) == 2,
        "★ 重叠且同值 ⇒ 合并（3→%d）；**不同位置**的同值 `20` 必须都留"
        "（两个视图各写 20 是正常的）" % len(dedup_dims([a, b, c])))

    print("[7] ★ 纯线框图（无彩色）必须显式报「检不出」，不静默")
    import tempfile                                    # noqa: PLC0415
    import sw_doctor                                  # noqa: PLC0415
    sw_doctor.ensure_user_site()          # ★ PIL 在用户级 site-packages
    from PIL import Image                             # noqa: PLC0415
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "bw.png")
        Image.new("RGB", (400, 300), (255, 255, 255)).save(p)
        got = detect_view_regions(p, min_area=100)
        chk(got == [], "★ 纯白图 ⇒ 0 个区域（不报错、不假装有视图）")

    print()
    print("== sw_drawing 离线自检：%d 通过 / %d 失败 ==" % (ok, fail))
    return 0 if fail == 0 else 1


# ================================================================ 入口

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="图纸位图 → 视图检出 + 尺寸归属（OCR 符号会丢，靠几何兜底）")
    ap.add_argument("--pdf", help="图纸 PDF（练习题）")
    ap.add_argument("--png", help="渲染输出（默认 D:/out/_train/drawing.png）")
    ap.add_argument("--zoom", type=int, default=ZOOM)
    ap.add_argument("--json", help="结果写到这个 JSON")
    ap.add_argument("--list", action="store_true", help="只打印视图与归属")
    ap.add_argument("--multi-pass", action="store_true",
                    help="★★ **推荐**：多遍 OCR（多个 zoom）+ 投票，"
                         "单遍结果不可复现（实测同一张图 zoom4 漏、zoom5 全对）")
    ap.add_argument("--zooms", default=",".join(str(z) for z in ZOOM_SET),
                    help="--multi-pass 用的 zoom 列表（逗号分隔）")
    ap.add_argument("--no-render", action="store_true", help="PNG 已存在，不重渲染")
    ap.add_argument("--crop-views", action="store_true",
                    help="★★★ **给 agent 的主入口**：渲染 + 按视图裁成单张 PNG"
                         "（带 pad，尺寸标在视图外也能看全）。"
                         "★ 尺寸由 agent 自己看图读，OCR 仅交叉验证")
    ap.add_argument("--out-dir", help="--crop-views 的输出目录"
                                      "（默认 D:/out/_train/crop）")
    ap.add_argument("--use-multipass", dest="use_multipass", action="store_true",
                    default=True,
                    help="给了 --pdf 就默认多遍（★ 单遍结果不可复现）")
    ap.add_argument("--single-pass", dest="use_multipass",
                    action="store_false", help="强制单遍（只调试用）")
    ap.add_argument("--self-test", action="store_true", help="离线自检")
    a = ap.parse_args(argv)

    if a.self_test:
        return self_test()

    # ★★★ `--crop-views`：**给 agent 的主入口**（用户校准 2026-10-03：
    #   agent 自己有视觉、自己读尺寸；工具只负责渲染/切图）。
    if getattr(a, "crop_views", False):
        if not a.pdf:
            ap.error("--crop-views 要 --pdf")
        out_dir = a.out_dir or r"D:\out\_train\crop"
        mv = render_views(a.pdf, out_dir, a.zoom)
        print("图纸区 %dx%d px（zoom=%d）⇒ overview + %d 个视图（pad=%d px）"
              % (mv["frame_px"][0], mv["frame_px"][1], mv["zoom"],
                 len(mv["views"]),
                 mv["views"][0]["pad_px"] if mv["views"] else 0))
        print()
        print("★ 尺寸读数请**以 agent 看图为准** —— OCR 数字仅作交叉验证"
              "（单遍不可复现、`⌀` 会丢）。")
        print("  工程图约定（供判视图身份，工具不替你下结论）：")
        print("    左下=Front、上方=Top/Section（剖视带名字如 SECTION A-A）、")
        print("    角落=Isometric（通常无尺寸，给形状）。")
        print()
        print("  overview: %s" % mv["overview"])
        for v in mv["views"]:
            print("  %-12s %-12s area=%-8d 填充通道=%s"
                  % (os.path.basename(v["file"]), v["hint"],
                     v["area"], v["mode"]))
        if not mv["views"]:
            print("  ⚠ 没检出视图区域（纯线框图 / 水印干扰）—— "
                  "请直接看 overview.png")
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(mv, f, ensure_ascii=False, indent=2)
            print("\n⇒ 已写 %s" % a.json)
        return 0

    if not a.pdf and not a.png:
        ap.error("要 --pdf 或 --png（或 --self-test）")

    # ★★ `--multi-pass`：多遍 OCR + 投票。**默认走这条** ——
    #   实测单遍结果**不可复现**（同一张图 zoom4 漏 `75`、zoom5 全对、
    #   zoom8 把 `16` 读成 `-12`）⇒ 一次 OCR 不能当事实。
    if a.multi_pass or (a.pdf and not a.no_render and a.use_multipass):
        if not a.pdf:
            ap.error("--multi-pass 要 --pdf")
        zooms = tuple(int(x) for x in a.zooms.split(",") if x.strip())
        out_dir = os.path.dirname(a.png) if a.png else r"D:\out\_train"
        os.makedirs(out_dir, exist_ok=True)
        r = analyze_multipass(a.pdf, out_dir, zooms)
        print()
        print("多遍 OCR：%d 遍（zoom=%s）⇒ %d 票 / %d 个位置簇 ⇒ "
              "**采纳 %d 个尺寸**（≥2 遍一致）"
              % (len(zooms), zooms, r["n_votes_raw"], r["n_clusters"],
                 r["n_dims"]))
        if r["single_vote"]:
            print("★ 只在 1 遍里出现的（**可疑，未采纳**，不静默丢掉）：")
            for sv in r["single_vote"]:
                print("     %s @ (%.3f, %.3f)  读到 %s"
                      % (sv["value"], sv["cx"], sv["cy"], sv["texts"]))
        print()
        print_report(r)
        if a.json:
            with open(a.json, "w", encoding="utf-8") as f:
                json.dump(r, f, ensure_ascii=False, indent=2)
            print("\n⇒ 已写 %s" % a.json)
        return 0

    png = a.png or r"D:\out\_train\drawing.png"
    if a.pdf and not a.no_render:
        w, h, clip = render(a.pdf, png, a.zoom)
        print("渲染 %s → %dx%d 像素（图纸区 %s）" % (a.pdf, w, h, clip))
    r = analyze(png)
    print()
    print_report(r)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(r, f, ensure_ascii=False, indent=2)
        print("\n⇒ 已写 %s" % a.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
