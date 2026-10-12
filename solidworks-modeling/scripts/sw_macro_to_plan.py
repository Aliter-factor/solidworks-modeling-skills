#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_macro_to_plan.py —— 把 SolidWorks 录制的 VBA 宏翻译成建模计划（plan JSON）。

为什么这是「训练」环节的核心：
    想教会技能一个新的特征顺序，最靠谱的办法不是翻文档猜参数，而是
    **在 SolidWorks 里手工做一遍、录制宏、把宏翻译成计划、跑回归**。
    宏里是 SolidWorks 自己生成的调用，参数、枚举、常量名全都是对的 ——
    它是唯一不会撒谎的「接口说明书」。

关于 .swp 的重要事实（实测）：
    .swp 是 OLE2 复合文档，里面存的是 VBA 的 **p-code（已压缩+令牌化）**，
    **不是源码**。哪怕解压出来也是一串令牌（`\\x81\\x03` 之类），拼不回可读代码。
    所以不要指望直接解析 .swp。正确做法是把源码导出来：

        在 SolidWorks 里按 Alt+F11 打开 VBA 编辑器
        → 左侧双击模块 → Ctrl+A / Ctrl+C（或 文件 > 导出文件 存成 .bas）
        → 把代码存成 .bas / .txt 再交给本脚本

    也支持从标准输入读（直接把代码粘进来）：`--stdin`。

用法：
    python sw_macro_to_plan.py --stdin < macro.bas
    python sw_macro_to_plan.py macro.bas --out plan.json
    python sw_macro_to_plan.py macro.bas --list        # 只列出识别到的 API 调用
    python sw_macro_to_plan.py macro.swp               # 会提示怎么导出 .bas
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sw_ops  # noqa: E402

# VBA 源码里可能出现的一切「噪音行」，直接丢掉，不参与翻译。
# 注意：分组里**不能**留空的 `|` 分支 —— 那会让正则可以匹配空串，把每一行都吃掉。
NOISE = re.compile(
    r"^\s*(?:"
    r"Dim\s|Set\s+\w+\s*=\s*Nothing|Option\s|Attribute\s|"
    r"Set\s+\w+\s*=\s*(?:Application\b|swApp\.ActiveDoc\b)|"
    r"Const\s|'|Rem\s|End\s+Sub|End\s+Function|"
    r"(?:Private|Public)?\s*Sub\s|(?:Private|Public)?\s*Function\s|"
    r"Exit\s+Sub|End\s+If"
    r")",
    re.I,
)

# 会改变「全局选择状态」的调用，翻译时可以跳过但必须记账
SKIP_BUT_LOGGED = (
    "ClearSelection2", "GetSelectedObject", "SetPickMode", "ClearSelection",
    "GraphicsRedraw2", "ViewZoomtofit2", "Isolate",
    "EnableContourSelection", "SetAddToDB", "SketchManager.AddToDB",
    "DisplayWhenAdded", "SetUserPreference", "GetUserPreference",
    "SetUnits", "AutoSolve", "SketchManager.DisplayWhenAdded",
)


def unify(source: str) -> list[str]:
    """把 VBA 源码折成「一条语句一行」：拆掉续行符 ` _`、去掉行尾注释、去空行。"""
    src = source.replace("\r\n", "\n").replace("\r", "\n")
    src = re.sub(r"^\s*_\s*", " ", src, flags=re.M)          # 行首续行符
    joined: list[str] = []
    buf = ""
    for raw in src.split("\n"):
        line = raw.rstrip()
        # 行尾 ` _` 表示续行
        if re.search(r"[ \t]_$", line):
            buf += line[:-1].rstrip() + " "
            continue
        line = buf + line
        buf = ""
        # 去掉字符串外的注释（简易：只要 ' 前面不是双引号就切）
        line = _strip_comment(line).strip()
        if not line or NOISE.match(line):
            continue
        joined.append(line)
    return joined


def _strip_comment(line: str) -> str:
    out, in_str = [], False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '"':
            in_str = not in_str
            out.append(ch)
        elif ch == "'" and not in_str:
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def split_args(argstr: str) -> list[str]:
    """按顶层逗号切参数（忽略引号与括号内的逗号）。"""
    out, depth, cur, in_str = [], 0, [], False
    for ch in argstr:
        if ch == '"':
            in_str = not in_str
        if not in_str:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                out.append("".join(cur).strip())
                cur = []
                continue
        cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return out


def call_re(name: str) -> re.Pattern:
    return re.compile(rf"(?:\.|^){re.escape(name)}\s*\(([\s\S]*)\)\s*$")


# VBA 的数字可以带类型声明后缀：0# Double、1& Long、2! Single、3% Integer、
# 4@ Currency。float("0#") 会直接抛异常 —— 这是第一版翻译器漏掉所有图元的原因。
_VBA_SUFFIX = re.compile(r"[#!&%@]$")


def num(tok: str, scale: float = 1.0) -> float | None:
    if tok is None:
        return None
    t = _VBA_SUFFIX.sub("", tok.strip())
    try:
        return round(float(t) * scale, 6)
    except ValueError:
        return None


def unquote(tok: str) -> str:
    t = tok.strip()
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1]
    return t


def const_name(tok: str) -> str:
    """'swEndConditions_e.swEndCondBlind' → 'swEndCondBlind'；纯数字原样返回。"""
    t = tok.strip()
    if "." in t and not t[0].isdigit():
        return t.split(".")[-1]
    return t


M = 1000.0      # 米 → 毫米


# ---- 宏里记录的是枚举的**数值**（SW 录制时常写 6 而不是 swEndCondMidPlane），
# ---- 所以要把数字反查回名字。反查表来自 assets/sw_enums.json。
_ENUM_CACHE: dict[str, dict[int, str]] = {}


def reverse_enum(enum_name: str) -> dict[int, str]:
    if enum_name in _ENUM_CACHE:
        return _ENUM_CACHE[enum_name]
    out: dict[int, str] = {}
    jf = Path(__file__).resolve().parent.parent / "assets" / "sw_enums.json"
    if jf.exists():
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
            for k, v in data.get(enum_name, {}).get("members", {}).items():
                if v.get("value") is not None and v["value"] not in out:
                    out[v["value"]] = k
        except (OSError, json.JSONDecodeError):
            pass
    _ENUM_CACHE[enum_name] = out
    return out


def translate(lines: list[str], scale: float = M) -> tuple[dict, list[dict]]:
    """核心翻译。返回 (plan, unmapped)。"""
    steps: list[dict] = []
    unmapped: list[dict] = []
    seq = [0]
    end_conds = reverse_enum("swEndConditions_e")

    def add(op: str, **kw) -> None:
        # 连续重复的同类「无参」步骤（例如 ShowNamedView2 + ViewZoomtofit2 都译成
        # zoom_fit）没有信息量，压掉，免得计划里全是噪音。
        if steps and steps[-1]["op"] == op and op in ("zoom_fit", "rebuild"):
            return
        seq[0] += 1
        steps.append({"id": f"m{seq[0]:02d}", "op": op, **kw})

    in_sketch = False          # InsertSketch(True) 是**切换**开关，不是「进草图」
    pending_profile: str | None = None   # 宏里 SelectByID2(名字,"SKETCH") 是给下一次拉伸用的

    def close_sketch() -> None:
        nonlocal in_sketch
        if in_sketch:
            add("sketch_end")
            in_sketch = False

    for line in lines:
        handled = False

        # ---------- 选择 ----------
        m = call_re("SelectByID2").search(line)
        if m:
            a = split_args(m.group(1))
            if len(a) >= 2:
                name, typ = unquote(a[0]), unquote(a[1]).upper()
                if typ in ("PLANE", "DATUMPLANE") and name:
                    add("select_plane", plane=name)
                elif typ == "SKETCH" and name:
                    # 选中某个已有草图 = 准备拿它当拉伸轮廓。不单独出步骤，
                    # 而是记下来交给紧随其后的 extrude_* 用 profile 参数带上。
                    pending_profile = name
                else:
                    unmapped.append({
                        "call": "SelectByID2", "args": [name, typ],
                        "note": "选的是面/边，执行前请用 select_face_at / select_edges 补上"})
            handled = True

        # ---------- 草图开关 ----------
        if not handled and re.search(r"SketchManager\.InsertSketch\b", line):
            in_sketch = not in_sketch
            add("sketch_begin" if in_sketch else "sketch_end")
            handled = True

        # ---------- 草图图元 ----------
        if not handled:
            for api, opname, conv in (
                ("CreateCircleByRadius", "circle", "circle"),
                ("CreateCircleByRadius2", "circle", "circle"),
                ("CreateLine", "line", "line"),
                ("CreateCenterLine", "centerline", "line"),
                ("CreateCornerRectangle", "rect_corner", "rect"),
                ("CreateCenterRectangle", "rect_center", "rect"),
                ("Create3PointArc", "arc3p", "arc3p"),
            ):
                m = call_re(api).search(line)
                if m:
                    a = split_args(m.group(1))
                    vals = [num(t, scale) for t in a]
                    if any(v is None for v in vals):
                        unmapped.append({"call": api, "args": a,
                                         "note": "含变量，无法静态取值，请手工补参数"})
                    elif conv == "circle":
                        add(opname, cx=vals[0], cy=vals[1], r=vals[3])
                    elif conv == "line":
                        add(opname, x1=vals[0], y1=vals[1], x2=vals[3], y2=vals[4])
                    elif conv == "rect":
                        add("rect_center",
                            cx=round((vals[0] + vals[3]) / 2, 6),
                            cy=round((vals[1] + vals[4]) / 2, 6),
                            w=round(abs(vals[3] - vals[0]), 6),
                            h=round(abs(vals[4] - vals[1]), 6))
                    elif conv == "arc3p":
                        add(opname, p1=[vals[0], vals[1]], p2=[vals[3], vals[4]],
                            p3=[vals[6], vals[7]])
                    handled = True
                    break

        # ---------- 特征 ----------
        if not handled:
            for api, opname in (
                ("FeatureExtrusion3", "extrude_boss"),
                ("FeatureCut4", "extrude_cut"),
                ("FeatureCut3", "extrude_cut"),
                ("FeatureRevolve2", "revolve_boss"),
                ("FeatureFillet3", "fillet"),
                ("InsertFeatureChamfer", "chamfer"),
                ("FeatureCircularPattern5", "circular_pattern"),
                ("FeatureCircularPattern4", "circular_pattern"),
                ("FeatureLinearPattern5", "linear_pattern"),
                ("InsertMirrorFeature2", "mirror"),
                ("InsertFeatureShell", "shell"),
                ("FeatureExtrusion2", "extrude_boss"),
            ):
                m = call_re(api).search(line)
                if m:
                    a = split_args(m.group(1))
                    close_sketch()
                    kw: dict = {}
                    if opname in ("extrude_boss", "extrude_cut"):
                        raw = const_name(a[3]) if len(a) > 3 else ""
                        if raw.isdigit():
                            kw["end_cond"] = end_conds.get(int(raw), raw)
                            if kw["end_cond"] == raw:
                                kw["end_cond_numeric"] = int(raw)
                        elif raw:
                            kw["end_cond"] = raw
                        d = num(a[5], scale) if len(a) > 5 else None
                        if d:
                            kw["depth"] = d
                        if len(a) > 1:
                            kw["flip_side"] = a[1].strip().lower() == "true"
                        if len(a) > 2:
                            kw["reverse_dir"] = a[2].strip().lower() == "true"
                        if not kw.get("depth") and "swEndCond" not in str(kw.get("end_cond")):
                            kw["note"] = "深度取自宏，请确认"
                        if pending_profile:
                            kw["profile"] = pending_profile
                            pending_profile = None
                    elif opname == "revolve_boss":
                        if len(a) > 6 and num(a[6]):
                            kw["angle"] = round(num(a[6]) * 180 / 3.141592653589793, 4)
                        if pending_profile:
                            kw["profile"] = pending_profile
                            pending_profile = None
                    elif opname == "fillet":
                        r = num(a[1], scale) if len(a) > 1 else None
                        if r:
                            kw["r"] = r
                        kw["note"] = "圆角依赖选中的边，宏里的选择无法静态还原，需手工补选边步骤"
                    elif opname == "chamfer":
                        d = num(a[4], scale) if len(a) > 4 else None
                        if d:
                            kw["dist"] = d
                    elif opname == "circular_pattern":
                        kw["count"] = int(float(a[0])) if a and num(a[0]) else 2
                        if len(a) > 4 and a[4].strip().lower() == "true":
                            kw["equal_spacing"] = True
                        kw["axis"] = "请填基准轴（建议用 axis_from_planes 先建）"
                        kw["target"] = "请填要阵列的特征名"
                    elif opname == "linear_pattern":
                        kw["count"] = int(float(a[0])) if a and num(a[0]) else 2
                        sp = num(a[1], scale) if len(a) > 1 else None
                        if sp:
                            kw["spacing"] = sp
                        kw["direction"] = "请填方向参考（边或轴）"
                        kw["target"] = "请填要阵列的特征名"
                    elif opname == "shell":
                        th = num(a[0], scale) if a else None
                        if th:
                            kw["thickness"] = th
                    add(opname, **kw)
                    handled = True
                    break

        # ---------- 基准面 / 轴 ----------
        if not handled and "CreatePlaneAtOffset3" in line:
            m = call_re("CreatePlaneAtOffset3").search(line)
            a = split_args(m.group(1)) if m else []
            add("plane_offset", base="请填参考面", offset=num(a[0], scale) if a else 0,
                note="宏从当前选择取参考面，需手工补 base")
            handled = True
        if not handled and "InsertAxis2" in line:
            add("axis_from_planes", note="宏用当前选择建轴，请核对 plane1/plane2")
            handled = True

        # ---------- 新建 / 保存 / 重建 ----------
        if not handled and re.search(r"\.NewDocument\s*\(", line):
            m = call_re("NewDocument").search(line)
            a = split_args(m.group(1)) if m else []
            add("new_part", template=unquote(a[0]) if a else None)
            handled = True
        if not handled and re.search(r"SaveAs[234]?\s*\(", line) and "SaveAs" in line:
            m = call_re("SaveAs3").search(line) or call_re("SaveAs4").search(line) \
                or call_re("SaveAs2").search(line) or call_re("SaveAs").search(line)
            a = split_args(m.group(1)) if m else []
            add("save_as", path=unquote(a[0]) if a else "", overwrite=True)
            handled = True
        if not handled and re.search(r"EditRebuild3|ForceRebuild3|EditRebuild\b", line):
            add("rebuild")
            handled = True
        if not handled and re.search(r"ViewZoomtofit2|ShowNamedView2", line):
            add("zoom_fit")
            handled = True

        if not handled and not any(s in line for s in SKIP_BUT_LOGGED):
            unmapped.append({"call": line.strip()[:200], "note": "没识别出对应的 op"})

    close_sketch()

    # 宏通常是「假设文档已经开着」录的（录制时你已经开了零件）。
    # 但计划是要能被独立执行的，所以没有建/开文档步骤时补一个 new_part，
    # 否则后面的 select_plane/sketch_begin 一执行就报「没有活动文档」。
    if not any(s["op"] in ("new_part", "open") for s in steps):
        steps.insert(0, {
            "id": "m00", "op": "new_part",
            "note": "宏里没有新建/打开文档的步骤，翻译器自动补的。"
                    "如果要改已有的零件，请把这一步换成 open。",
        })

    plan = {
        "schema": "yoyo.swplan/1",
        "meta": {
            "name": "由 SolidWorks 宏翻译（请复核参数）",
            "units": "mm",
            "note": "本计划是**骨架**：宏里靠「当前选择」推断的引用（面、边、阵列方向、"
                    "要阵列的特征名）看不出代码，必须人工补；补完先跑 "
                    "sw_plan.py --check 再执行。",
        },
        "params": {},
        "steps": steps,
    }
    return plan, unmapped


def report(plan: dict, unmapped: list[dict]) -> str:
    out = [f"翻译出 {len(plan['steps'])} 个步骤："]
    for s in plan["steps"]:
        extra = {k: v for k, v in s.items() if k not in ("id", "op", "note")}
        out.append(f"  {s['id']:>5} {s['op']:<22} {extra}")
    if unmapped:
        out.append(f"\n{len(unmapped)} 处没能自动翻译（**必须人工处理**）：")
        for u in unmapped[:40]:
            out.append(f"  - {u.get('call')}  →  {u.get('note', '需要手工补 op')}")
        if len(unmapped) > 40:
            out.append(f"  ...还有 {len(unmapped) - 40} 处")
    # 与操作表对照，检查翻译出来的 op 是否都存在
    bad = [s["op"] for s in plan["steps"] if s["op"] not in sw_ops.OPS]
    if bad:
        out.append(f"\n注意：这些 op 不存在，需要新增实现：{sorted(set(bad))}")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description="SolidWorks VBA 宏 → 建模计划")
    ap.add_argument("src", nargs="?", help=".bas/.txt/.vba 宏源码")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读")
    ap.add_argument("--out", default=None, help="写出 plan JSON")
    ap.add_argument("--list", action="store_true", help="只列出识别到的 API 调用")
    ap.add_argument("--no-scale", action="store_true",
                    help="不把米换算成毫米（宏里坐标是米，默认会 ×1000）")
    a = ap.parse_args()

    if a.stdin:
        text = sys.stdin.read()
    elif a.src:
        p = Path(a.src)
        if not p.exists():
            raise SystemExit(f"文件不存在: {p}")
        if p.suffix.lower() == ".swp":
            raise SystemExit(
                ".swp 里存的是 VBA 的 p-code（压缩+令牌化），不是源码，无法直接翻译。\n"
                "请这样导出源码：\n"
                "  1. 在 SolidWorks 里按 Alt+F11 打开 VBA 编辑器\n"
                "  2. 左侧工程树里双击你的模块\n"
                "  3. Ctrl+A 全选 → Ctrl+C 复制（或 文件 > 导出文件 存成 .bas）\n"
                "  4. 把内容粘贴/保存后重跑本脚本（也可直接 --stdin 粘进来）"
            )
        text = p.read_text(encoding="utf-8", errors="replace")
    else:
        raise SystemExit("给个宏文件，或用 --stdin")

    lines = unify(text)
    if a.list:
        for i, ln in enumerate(lines, 1):
            print(f"{i:4} {ln}")
        return
    plan, unmapped = translate(lines, scale=1.0 if a.no_scale else M)
    print(report(plan, unmapped))
    if a.out:
        Path(a.out).write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"\n已写出 {a.out}。下一步：python sw_plan.py {a.out} --check")
    else:
        print("\n" + json.dumps(plan, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
