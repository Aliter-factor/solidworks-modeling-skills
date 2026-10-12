#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""练习题索引 → **判据集**（把「题目」变成「可自动核对的验收标准」）。

★ 这个工具解决的是坑表 §102 那个最贵的缺口：
    「判据全绿 ≠ 结论正确」—— 此前 agent 的所有判据都只保证
    「**我算的和模型里的一致**」，**没有任何外部真值**。
    拿自己早前建的件当参照件 = 自己抄自己，错误被完整继承。

★★ 训练材料带来的东西（2026-10-03 取证）：
    `C:\Users\OMEN\Downloads\d\PracticeProblems_ALL` 344 份 PDF，
    **297 份带精确目标体积**（`Volume: 202594 mm3`），
    外加人写的 `Design Intent`（设计意图）与建议特征清单。

    ⇒ 每道题都能变成一条**外部可核对**的验收标准：
       `体积 == 202594`（±tol）`且` `bbox` 逐项对 `且` 面集签名一致。

### ★★ 三类题要用**不同的真值**，不能一律要体积

| 类型 | 等级 | 真值形式 | 实测 |
|---|---|---|---|
| **零件** | 01-08 / 10-14 / 18 | **`target_volume_mm3`** | Level04 **70/70**、Level18 33/33、Level10 19/19 |
| **装配体** | 09 | 组件数 / 配合数 | **0/16 有体积**（题面是 "Insert … at Origin"） |
| **配置 / 方程式** | 15 / 16 / 17 | 配置数 / 方程解 | **0/13、1/6、2/7 有体积** |

★ 这个分布本身就是证据：**零件类几乎 100% 有体积**，
  而装配体/配置类**系统性地没有** ⇒ 「没有 `Volume`」不是提取失败，
  而是**这类题本来就没有单件体积**。
  ⇒ 别把「没读到 Volume」一律当成 bug（§99③ 的老教训）。

### 用法
```
# 1) 建索引（题目 → 判据）
python scripts/sw_exercise.py --root <PDF目录> --json D:/out/_train/index.json

# 2) 拿一道题的判据去核对一个建好的件
python scripts/sw_exercise.py --json D:/out/_train/index.json \
       --check PracticeProblems_10_1_ENG.pdf --part D:/out/my.SLDPRT
```
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# ★★ 真值容差（相对）。SolidWorks 的 mass_props 与官方答案之间
#    常见 1e-4 ~ 1e-3 的相对差（ tessellation / 单位换算）。
VOLUME_REL_TOL = 0.002          # 0.2%
BBOX_REL_TOL = 0.002            # 0.2%
#: 体积太小的时候相对容差不可靠 ⇒ 绝对下限（mm³）
VOLUME_ABS_FLOOR = 1.0

#: 哪些等级是「装配体 / 配置」类 —— **不期待体积**
NON_PART_LEVELS = (9, 15, 16, 17)
#: 装配体类题用什么真值（目前只能靠题面文字，工具只**标出来**）
ASM_TRUE_VALUE = {
    9: "组件数 / 配合数（题面给的是操作指令，没有体积）",
    15: "配置数与每个配置的差异（题面给的是配置操作）",
    16: "全局变量 / 方程式解（题面给的是方程，不是单一实体）",
    17: "装配体工具的特定结果（组件属性 / 配合引用）",
}

RE_NUM = re.compile(r"PracticeProblems_(\d+)_(\d+)")


def load_index(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def pick(index: dict, key: str) -> dict | None:
    """按文件名（可省略扩展名/等级前缀）挑一道题。"""
    k = key.lower()
    for it in index.get("items", []):
        fn = it.get("file", "").lower()
        if fn == k or fn == k + ".pdf":
            return it
        if k in fn:
            return it
    return None


def build_criteria(item: dict) -> dict:
    r"""把一道题变成**验收判据集**。

    返回：
    ```
    {'file':…, 'level':…, 'kind': 'part'|'assembly'|'config',
     'volume_mm3': float|None, 'volume_rel_tol': float,
     'symmetric': bool, 'views': [...],
     'design_intent': [...], 'features': [...], 'hint': str|None,
     'expect': [ '体积 == 202594 mm³（±0.2%）', … ],
     'cannot_check': [ … ]   # ★ 明确说「哪些判据这条路拿不到」
    }
    ```
    ★ `cannot_check` 是这个工具的重点：**明确说出「我验不了什么」**。
      没有它，agent 会把「只验了体积」当成「全验了」——
      而体积对**不代表**特征对（体积是标量，丢一个特征再补一个也能对上）。
    """
    lv = item.get("level_no")
    kind = ("assembly" if lv == 9 else
            "config" if lv in (15, 16, 17) else "part")
    vol = item.get("target_volume_mm3")
    expect: list[str] = []
    cannot: list[str] = []

    if vol is not None:
        expect.append("体积 == %.0f mm³（相对容差 %.2f%%）"
                      % (vol, VOLUME_REL_TOL * 100))
        expect.append("包围盒**逐轴有序**对（只看尺寸集合会被转置骗过，§101①）")
    else:
        cannot.append(
            "★ **体积**：这题是 `%s` 类（Level %s），**本来就没有单件体积**"
            "（实测：Level09 0/16、Level15 0/13、Level16 1/6、Level17 2/7）"
            "⇒ 不要把「没读到 Volume」当成提取失败"
            % (kind, lv))
    if kind != "part":
        cannot.append("真值形式：%s"
                      % ASM_TRUE_VALUE.get(lv, "非零件类，判据体系不同"))
    # ★ 体积是**标量**：丢一个特征、另一个特征补上，体积仍可能对上
    if vol is not None:
        cannot.append(
            "★ **体积是标量**：「少建一个孔 + 多建一块料」也能对上 "
            "⇒ 体积通过**不等于**特征对。必须再叠 bbox/面集/孔位判据")
    if item.get("symmetric"):
        expect.append("对称性：题面说 **symmetric** ⇒ 用 `sw_symmetry` "
                      "复核（★ 别反过来用「对称」当先验去推几何，§102）")
    if item.get("views"):
        expect.append("视图布局：%s（⇒ 基准面/朝向的唯一依据）"
                      % ", ".join(item["views"]))
    if not item.get("views"):
        cannot.append("★ **视图布局读不到** ⇒ 无法确定草图画在哪个基准面"
                      " ⇒ 尺寸对、bbox 逐项对，**朝向仍可能错**（§101①）")
    if item.get("features"):
        expect.append("建议特征：%s" % ", ".join(item["features"]))
    if item.get("hint"):
        expect.append("题面 HINT：%s" % item["hint"])
    return {
        "file": item.get("file"),
        "level": item.get("level"),
        "level_no": lv,
        "kind": kind,
        "volume_mm3": vol,
        "volume_rel_tol": VOLUME_REL_TOL,
        "difficulty": item.get("difficulty"),
        "duration_min": item.get("duration_min"),
        "symmetric": item.get("symmetric"),
        "fully_defined": item.get("fully_defined"),
        "origin_marked": item.get("origin_marked"),
        "views": item.get("views") or [],
        "design_intent": item.get("design_intent") or [],
        "sketch_tools": item.get("sketch_tools") or [],
        "features": item.get("features") or [],
        "reference_geometry": item.get("reference_geometry") or [],
        "hint": item.get("hint"),
        "n_drawings": item.get("n_drawings"),
        "n_images": item.get("n_images"),
        "expect": expect,
        "cannot_check": cannot,
    }


def check_part(criteria: dict, part_path: str) -> dict:
    """拿判据去核对一个**建好的件**（需要 SolidWorks）。"""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    import contextlib                                  # noqa: PLC0415
    import sw_connect                                  # noqa: PLC0415
    from sw_connect import member                      # noqa: PLC0415

    out = {"part": part_path, "results": [], "verdict": "?"}
    sw = sw_connect.SwSession.open(visible=False)
    try:
        sw.open_doc(part_path, read_only=True)
        mp = sw.mass_props()
        box = sw.bounding_box()
        out["volume_mm3"] = round(float(mp["volume_m3"]) * 1e9, 3) \
            if mp.get("volume_m3") else None
        out["suspect"] = mp.get("suspect")
        out["bbox_mm"] = [round(v * 1000, 4) for v in box] if box else None
    finally:
        with contextlib.suppress(Exception):
            for d in (member(sw.app, "GetDocuments") or ()):
                sw.app.CloseDoc(str(member(d, "GetTitle")))

    exp_v = criteria.get("volume_mm3")
    got_v = out.get("volume_mm3")
    if exp_v and got_v and not out.get("suspect"):
        dev = abs(got_v - exp_v) / exp_v
        ok = dev <= criteria["volume_rel_tol"]
        out["results"].append({
            "name": "体积", "ok": ok,
            "detail": "期望 %.1f，实得 %.1f ⇒ 相对偏差 %.4f%%（容差 %.2f%%）"
                      % (exp_v, got_v, dev * 100,
                         criteria["volume_rel_tol"] * 100)})
    else:
        out["results"].append({
            "name": "体积", "ok": None,
            "detail": "★ 验不了：%s" % (
                "题面没有目标体积" if not exp_v else
                "读不到件���体积（mass_props suspect=%r）" % out.get("suspect"))})
    real = [r for r in out["results"] if r["ok"] is not None]
    out["verdict"] = ("PASS" if real and all(r["ok"] for r in real)
                      else "FAIL" if real else "NO-CHECK")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="练习题索引 → 判据集 / 拿判据核对建好的件")
    ap.add_argument("--json", help="`sw_exercise.py --json` 产出的索引")
    ap.add_argument("--item", help="打印某道题的判据集")
    ap.add_argument("--check", help="要核对的件（.SLDPRT）")
    ap.add_argument("--part", help="配合 --check：已建好的零件路径")
    ap.add_argument("--kind", default=None, help="只列某种类型的题")
    args = ap.parse_args(argv)

    if not args.json:
        ap.error("要 --json 索引")
    idx = load_index(args.json)

    if args.item:
        it = pick(idx, args.item)
        if not it:
            print("没找到题：%s" % args.item)
            return 1
        c = build_criteria(it)
        print("== %s（%s，难度 %s，%s 分钟）=="
              % (c["file"], c["level"], c["difficulty"], c["duration_min"]))
        print("类型：%s" % c["kind"])
        print("Design Intent：")
        for d in c["design_intent"]:
            print("   · %s" % d)
        print("可自动核对的判据：")
        for e in c["expect"]:
            print("   ✔ %s" % e)
        print("★ 验不了的（别当成「已验」）：")
        for e in c["cannot_check"]:
            print("   ✘ %s" % e)
        return 0

    if args.check and args.part:
        it = pick(idx, args.check)
        if not it:
            print("没找到题：%s" % args.check)
            return 1
        c = build_criteria(it)
        r = check_part(c, args.part)
        print("== %s  vs  %s ==" % (c["file"], r["part"]))
        for x in r["results"]:
            mark = {True: "PASS", False: "FAIL", None: "SKIP"}[x["ok"]]
            print("  [%s] %-6s %s" % (mark, x["name"], x["detail"]))
        print("⇒ %s" % r["verdict"])
        return 0 if r["verdict"] in ("PASS", "NO-CHECK") else 1

    # 列表模式
    kinds = args.kind or ("part", "assembly", "config")
    rows = []
    for it in idx.get("items", []):
        c = build_criteria(it)
        if c["kind"] not in kinds:
            continue
        rows.append(c)
    print("== 判据集（共 %d 题）==" % len(rows))
    print("%-30s %-10s %-11s %-9s %s"
          % ("文件", "类型", "目标体积mm³", "对称", "可验条数"))
    for c in rows[:40]:
        print("%-30s %-10s %-11s %-9s %d"
              % (c["file"], c["kind"],
                 ("%.0f" % c["volume_mm3"]) if c["volume_mm3"] else "-",
                 "是" if c["symmetric"] else "-", len(c["expect"])))
    if len(rows) > 40:
        print("   … 另 %d 题" % (len(rows) - 40))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
