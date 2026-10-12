#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""读 SolidWorks 官方练习题的 PDF，提取**题目元信息**。

★ 为什么这个工具存在（2026-10-03）
----------------------------------
用户给的训练材料 `C:\Users\OMEN\Downloads\d\PracticeProblems_ALL`
= 18 个等级 / 344 张 PDF。目标是「读图 → 提取标注 → 定建模顺序与基准面」。

★★ 但第一件取证就改变了判断：**这些 PDF 里不只有图纸，还有题目说明**：

    Practice Level (Level 10)
    Units: MMGS
    Difficulty Level: Easy
    Certification: CSWA
    Duration (min): 10
    Design Intent:
      o The part's origin location is indicated in red.
      o The part is symmetric.
      o All sketches are fully defined.
    Suggested Sketch Tools / Features
      Sketch: Lines, Centerlines, Circles, Slots
      Features: Extrude Boss, Extrude Cut, Fillets and Chamfers
      Reference Geometry: Planes
    HINT: Use a Slot for the center cut.
    Volume: 202594 mm3

⇒ **`Volume` 是每道题都有的精确目标体积**（mm³）。这是**判据链首端**
  一直缺的东西（坑表 §102）：在此之前，agent 的所有判据都只能
  「验证自己算的和模型一致」，**没有任何外部真值**。
  有了目标体积，「我建的件对不对」第一次有了**外部答案**。

⇒ **`Design Intent` 是人写的建模意图**（对称/草图全定义/原点位置），
  相当于「这道题该怎么做」的**权威提示**，可以直接喂给规划层。

★★ 与图纸本体的关系（必须分清）：
  - 文本层（`get_text`）= **题目说明**，可直接读
  - **图纸本体是位图**（`get_drawings()` 极少、`get_images()` 有若干）
    ⇒ **尺寸数字烧在图里**，要读它们得走 OCR（`yoyo.ocr` / RapidOCR）
  ⇒ 所以本工具**只负责题目元信息**；尺寸标注是另一条路（OCR），
    两者互补，**不要指望一个工具全干**。

### ★★ 环境硬事实（本机实测，别重新踩）
1. **必须 `export APPDATA="C:\Users\OMEN\AppData\Roaming"`**，
   否则 `import pymupdf` / `rapidocr_onnxruntime` 全部报"没装"
   —— 它们装在**用户级 site-packages**，不在默认 sys.path 上。
2. `pymupdf` **1.27.2.2** 已在（不需要联网装）。
3. ★★ **`Level11_HoleWizard/PracticeProblems_11_8_ENG.pdf` 是 0 页坏文件**
   ⇒ `doc[0]` 抛 `IndexError: page 0 not in document`。
   ⇒ **必须先判 `page_count == 0`**，否则一个坏文件会打崩整批。
4. ★★ **别在 heredoc 里写正则**：`\s` `\d` 会被吃掉一层变成 `/s` `/d`
   ⇒ 正则一律写成模块级常量（`re.compile(r"...")`），本文件就是证据。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

# ★★ 全部用 raw string 编译成常量：heredoc 会吃掉反斜杠一层（见模块 docstring）
# ★ 真文本是 `Exam Level (Level 10)`；`Practice Prob` 是**被版权行截断**的
#   残句（"Practice Problems" 撞在 "incorporated." 后面）—— 别去匹配它。
RE_EXAM = re.compile(r"Exam\s+Level\s*\(([^)]*)\)")
RE_UNITS = re.compile(r"Units:\s*([A-Z]+)")
RE_DIFF = re.compile(r"Difficulty\s+Level:\s*([A-Za-z]+)")
RE_CERT = re.compile(r"Certification:\s*([A-Za-z0-9 ]+)")
RE_DURATION = re.compile(r"Duration\s*\(min\):\s*(\d+)")
RE_VOLUME = re.compile(r"Volume:\s*([0-9.]+)\s*mm3")
RE_HINT = re.compile(r"HINT:\s*(.+?)(?=\n\s*Volume:|\Z)", re.S)
RE_SKETCH = re.compile(r"Sketch:\s*(.+?)(?=\n[•\s]*Features:)", re.S)
RE_FEATURES = re.compile(r"Features:\s*(.+?)(?=\n[•\s]*Reference|\Z)", re.S)
RE_REFGEOM = re.compile(r"Reference\s+Geometry:\s*(.+?)(?=\n\s*HINT:|\Z)", re.S)
RE_DESIGN = re.compile(r"Design\s+Intent:\s*(.+?)(?=\n\s*Suggested|\Z)", re.S)
RE_NUM_LVL = re.compile(r"PracticeProblems_(\d+)_")

#: 视图布局名（`Suggested Sketch Tools / Features` 里的那一行）
VIEW_WORDS = ("Top", "Front", "Right", "Isometric", "Trimetric")


def _clean(s: str | None) -> str:
    """把 PDF 里的换行/项目符号压成一行，保留可读性。"""
    if not s:
        return ""
    s = re.sub(r"[•\u2022\u25cf]", " ", s)
    s = re.sub(r"\s*\n\s*", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    # ★ 只去**首尾**的标点，**保留句末的句号** ——
    #   `Design Intent` 的条目是完整句子（"The part is symmetric."），
    #   去掉句号反而让读起来像残句。
    return s.strip(" ;,")


#: 条目行前缀（`o xxx` / `- xxx` / `• xxx`）
_BULLET_RE = re.compile(r"^\s*[o\u25cf\-\u2022]\s*")


def _bullets(block: str | None) -> list[str]:
    r"""把 `o xxx` / `- xxx` 形式的条目拆成列表。

    ★★★ 必须处理「**硬换行把一条目切成两半**」——真机（2026-10-03）里
      每份 PDF 都是这样：
      ```
      o The part's origin location is indicated
      in red.                      ← 续行，没有 `o` 前缀
      o The part is symmetric.
      Features: Extrude Boss, Extrude Cut, Fillets
      and Chamfers                 ← 续行
      ```
      ⇒ 规则：**没有条目前缀的行 = 上一条的续行**，直接拼接。
      第一版没做这件事，于是
      `"The part's origin location is indicated"` 和 `"in red"` 成了两条 ——
      ★ **看起来像识别错了，其实是切分错**。
    """
    if not block:
        return []
    out: list[str] = []
    for raw in re.split(r"[\n\r]+", block):
        if not raw.strip():
            continue
        if _BULLET_RE.match(raw):
            out.append(_clean(_BULLET_RE.sub("", raw)))
        elif out:
            # ★ 续行：接到上一条尾巴上
            out[-1] = _clean(out[-1] + " " + raw)
        else:
            out.append(_clean(raw))
    return [x for x in out if x]


def detect_views(text: str) -> list[str]:
    """从「Design Intent」里挑出视图布局名（Top/Front/Right/Isometric…）。"""
    found = []
    for line in text.splitlines():
        for w in VIEW_WORDS:
            # 形如 "Isometric view" / "Front view"
            if re.search(r"\b%s\b" % w, line) and w not in found:
                found.append(w)
    return found


def read_one(path: str) -> dict[str, Any]:
    """读一份练习题 PDF。**任何异常都变成 `ok: False` + `why`，不抛。**"""
    import pymupdf                                     # noqa: PLC0415

    out: dict[str, Any] = {
        "file": os.path.basename(path),
        "path": path,
        "ok": False,
        "why": "",
    }
    try:
        doc = pymupdf.open(path)
    except Exception as e:                              # noqa: BLE001
        out["why"] = "打不开：%s: %s" % (type(e).__name__, str(e)[:80])
        return out
    try:
        # ★★ 先判页数：坏文件 `page_count == 0`，`doc[0]` 会抛 IndexError
        n = doc.page_count
        out["pages"] = n
        if n == 0:
            out["why"] = "★ **0 页坏文件**（`doc[0]` 会抛 IndexError）"
            return out
        page = doc[0]
        text = page.get_text()
        out["text_len"] = len(text)
        # 图纸本体是位图 ⇒ 尺寸数字要靠 OCR；这两个数是「该不该走 OCR」的判据
        try:
            out["n_drawings"] = len(page.get_drawings())
        except Exception:                               # noqa: BLE001
            out["n_drawings"] = None
        try:
            out["n_images"] = len(page.get_images())
        except Exception:                               # noqa: BLE001
            out["n_images"] = None

        m = RE_EXAM.search(text)
        out["level"] = _clean(m.group(1)) if m else None
        m = RE_NUM_LVL.search(os.path.basename(path))
        out["level_no"] = int(m.group(1)) if m else None
        m = RE_UNITS.search(text)
        out["units"] = _clean(m.group(1)) if m else None
        m = RE_DIFF.search(text)
        out["difficulty"] = _clean(m.group(1)) if m else None
        m = RE_CERT.search(text)
        out["certification"] = _clean(m.group(1)) if m else None
        m = RE_DURATION.search(text)
        out["duration_min"] = int(m.group(1)) if m else None

        # ★★★ Volume：外部真值，判据链首端（坑表 §102 缺的就是这个）
        m = RE_VOLUME.search(text)
        if m:
            out["target_volume_mm3"] = float(m.group(1))
        else:
            out["target_volume_mm3"] = None
            out["why"] = "★ 没读到 `Volume:` ⇒ **没有外部真值**，" \
                         "只有内部自洽判据"

        out["design_intent"] = _bullets(RE_DESIGN.search(text).group(1)
                                        if RE_DESIGN.search(text) else None)
        out["views"] = detect_views(text)
        m = RE_SKETCH.search(text)
        out["sketch_tools"] = _bullets(m.group(1) if m else None)
        m = RE_FEATURES.search(text)
        out["features"] = _bullets(m.group(1) if m else None)
        m = RE_REFGEOM.search(text)
        out["reference_geometry"] = _bullets(m.group(1) if m else None)
        m = RE_HINT.search(text)
        out["hint"] = _clean(m.group(1)) if m else None

        out["symmetric"] = any("symmetric" in d.lower()
                               for d in out["design_intent"])
        out["fully_defined"] = any("fully defined" in d.lower()
                                   for d in out["design_intent"])
        out["origin_marked"] = any("origin" in d.lower()
                                   for d in out["design_intent"])
        out["ok"] = bool(out.get("target_volume_mm3"))
    except Exception as e:                              # noqa: BLE001
        out["why"] = "%s: %s" % (type(e).__name__, str(e)[:80])
    finally:
        with_suppress = getattr(doc, "close", None)
        if with_suppress:
            try:
                doc.close()
            except Exception:                           # noqa: BLE001
                pass
    return out


def scan(root: str, verbose: bool = True) -> dict[str, Any]:
    """扫一个目录（或它下面的所有子目录）里的全部练习题 PDF。"""
    import glob                                        # noqa: PLC0415

    pdfs = sorted(glob.glob(os.path.join(root, "**", "*.pdf"),
                            recursive=True))
    items = [read_one(p) for p in pdfs]
    good = [i for i in items if i["ok"]]
    bad = [i for i in items if not i["ok"]]
    by_level: dict[str, int] = {}
    feat_counter: dict[str, int] = {}
    for i in good:
        k = i.get("level") or ("Level%02d" % (i.get("level_no") or 0))
        by_level[k] = by_level.get(k, 0) + 1
        for f in (i.get("features") or []):
            feat_counter[f] = feat_counter.get(f, 0) + 1
    res = {
        "root": root,
        "n_pdf": len(pdfs),
        "n_ok": len(good),
        "n_bad": len(bad),
        "bad": [{"file": b["file"], "why": b["why"]} for b in bad],
        "with_volume": sum(1 for i in good if i.get("target_volume_mm3")),
        "by_level": dict(sorted(by_level.items())),
        "features_top": sorted(feat_counter.items(),
                               key=lambda kv: -kv[1])[:12],
        "items": items,
    }
    if verbose:
        _print_report(res)
    return res


def _print_report(res: dict[str, Any]) -> None:
    print("== 练习题扫描（%s）==" % res["root"])
    print("PDF %d 份：**可用 %d / 异常 %d**（可用 = 读到了 `Volume:`）"
          % (res["n_pdf"], res["n_ok"], res["n_bad"]))
    for b in res["bad"]:
        print("   ★ %-34s %s" % (b["file"], b["why"]))
    print("★ **外部真值（目标体积）覆盖 %d / %d**"
          % (res["with_volume"], res["n_ok"]))
    print()
    print("各等级题量：")
    for k, v in res["by_level"].items():
        print("   %-40s %d" % (k, v))
    print()
    print("建议特征组合 Top 10：")
    for k, v in res["features_top"][:10]:
        print("   %3d×  %s" % (v, k))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="读 SolidWorks 练习题 PDF 的题目元信息"
                    "（Design Intent / 建议特征 / **目标体积**）")
    ap.add_argument("--root", default=None, help="PDF 目录（递归）")
    ap.add_argument("--file", default=None, help="只看一份（调试用）")
    ap.add_argument("--json", default=None, help="把结果写到这个 JSON")
    ap.add_argument("--list", action="store_true", help="逐题打印一行摘要")
    args = ap.parse_args(argv)

    if args.file:
        r = read_one(args.file)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if r["ok"] else 1
    if not args.root:
        ap.error("要 --root 或 --file")
    res = scan(args.root)
    if args.list:
        print()
        print("== 逐题摘要 ==")
        print("%-30s %-8s %-9s %-12s %s"
              % ("文件", "体积mm³", "难度", "视图", "特征"))
        for i in res["items"]:
            if not i["ok"]:
                continue
            print("%-30s %-8.0f %-9s %-12s %s"
                  % (i["file"], i.get("target_volume_mm3") or 0,
                     i.get("difficulty") or "?",
                     ",".join(i.get("views") or []) or "-",
                     ", ".join(i.get("features") or [])[:40]))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print("\n⇒ 已写 %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
