"""诊断 / 标定：装配体特征树的**真实类型名** —— 用来填 `SwSession.MATE_FEATURE_TYPES`。

为什么需要它：
    `mate_count()` 靠白名单（`GetTypeName2` 返回的内部类型串）数配合。
    白名单**不许猜**（猜错就是静默回 0 = 假绿灯），必须真机打出来照抄。
    而"配合"在特征树里是**「配合」文件夹的子特征** —— 只看顶层永远只有一个
    `MateGroup`，所以本脚本**必须**递归打子特征。

用法：
    # ① 直接开一个已有装配体
    export APPDATA="C:\\Users\\OMEN\\AppData\\Roaming"
    /c/Python313/python.exe scripts/sw_asm_tree.py --doc "D:/out/xxx.SLDASM"

    # ② 不给 --doc：现场搭一个最小装配体（盒子 ×2 + 一条重合配合），
    #    正好也是 assets/probes/asm_mate.json 的等价物
    /c/Python313/python.exe scripts/sw_asm_tree.py

输出：
    · 组件表（名字 / 固定 / 抑制 / 配置 / 路径）
    · **完整**特征树（含子特征，带 depth）
    · 去重后的「类型名 → 出现次数 → 名字样例」—— 配合那一行就是要抄进
      `MATE_FEATURE_TYPES` 的值（注意**别**把 `MateGroup` 抄进去，
      它是文件夹；`MATE_GROUP_TYPES` 已经负责扣掉它）
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sw_connect  # noqa: E402
import sw_ops  # noqa: E402
from sw_connect import member  # noqa: E402


def build_min_assembly(sw) -> str:
    """现场搭一个最小装配体（两件同款盒子 + 一条重合配合），返回它的路径。

    ★ 每次跑用**独立子目录**（带时间戳）：
      上一版固定写 `D:/out/_diag/diag_asm_box.SLDPRT`，跑第二遍时那份文件
      已经被上一次的 SolidWorks 会话**占着**，`SaveAs` 会**静默失效**
      （回 True 但没落盘、活动文档路径仍为空）—— 实测踩到。
      诊断工具本身不该有副作用，更不该因为"上次的残留"而失败。
    """
    import time as _time
    stamp = _time.strftime("%H%M%S")
    out = os.path.join(os.environ.get("YOYO_SW_OUT", "D:/out"), "_diag", f"asm_{stamp}")
    os.makedirs(out, exist_ok=True)
    box = os.path.join(out, "box.SLDPRT")
    asm = os.path.join(out, "min_asm.SLDASM")

    ctx: dict = {}
    sw.new_part(None)
    sw_ops.op_select_plane(sw, {"op": "select_plane", "plane": "Front"}, ctx)
    sw_ops.op_sketch_begin(sw, {"op": "sketch_begin"}, ctx)
    sw_ops.op_rect_corner(sw, {"op": "rect_corner", "x1": 0, "y1": 0,
                               "x2": 40, "y2": 30}, ctx)
    sw_ops.op_sketch_end(sw, {"op": "sketch_end", "name": "Sk"}, ctx)
    sw_ops.op_extrude_boss(sw, {"op": "extrude_boss", "depth": 10, "name": "Box"}, ctx)
    sw_ops.op_save_as(sw, {"op": "save_as", "path": box, "overwrite": True}, ctx)
    print(f"[i] 盒子已存: {box}")

    sw_ops.op_new_assembly(sw, {"op": "new_assembly"}, ctx)
    sw_ops.op_insert_component(sw, {"op": "insert_component", "path": box,
                                    "x": 0, "y": 0, "z": 0, "name": "地基"}, ctx)
    sw_ops.op_insert_component(sw, {"op": "insert_component", "path": box,
                                    "x": 0, "y": 0, "z": 40, "name": "上块"}, ctx)
    try:
        sw_ops.op_add_mate(sw, {"op": "add_mate", "kind": "coincident",
                                "entities": [{"type": "FACE", "point": [20, 15, 10]},
                                             {"type": "FACE", "point": [20, 15, 40]}],
                                "name": "底上重合"}, ctx)
        print("[i] 重合配合已建")
    except Exception as e:                                     # noqa: BLE001
        print(f"[!] 配合没建上（不影响看类型名，但请留意）: {type(e).__name__}: {e}")
    with contextlib.suppress(Exception):
        sw_ops.op_save_as(sw, {"op": "save_as", "path": asm, "overwrite": True}, ctx)
        print(f"[i] 装配体已存: {asm}")
    return asm


def preflight(sw) -> list[str]:
    """跑之前先看现场 —— 列出**有未保存改动**的文档。

    为什么值得单列一段：
        本脚本（以及 `--doc` 那条路之外的 `build_min_assembly`）会 `NewDocument`。
        在 MDI 下新建**不会**关文档、也不会弹保存框，所以未保存文档**不阻断**本脚本。
        但是：① 真机跑之前人应该知道现场有什么（本项目铁律）；
              ② 一旦以后有人把它改成"先关掉再建"，这个预检就是唯一的刹车。
        ⇒ **只警告不阻断**（列表照常打印），并把结果回给调用方。
    """
    dirty: list[str] = []
    with contextlib.suppress(Exception):
        for d in (member(sw.app, "GetDocuments") or ()):
            flag = False
            with contextlib.suppress(Exception):
                flag = bool(member(d, "GetSaveFlag"))
            if flag:
                with contextlib.suppress(Exception):
                    dirty.append(str(member(d, "GetTitle")))
    if dirty:
        print(f"[!] 现场有 {len(dirty)} 个**未保存**的文档：{', '.join(dirty)}")
        print("    （MDI 下新建文档不影响它们，本脚本继续跑；但请自行确认没有正在编辑的东西）")
    return dirty


def dump(sw) -> None:
    print("\n" + "=" * 70)
    print("组件表（IAssemblyDoc.GetComponents / IComponent2）")
    print("=" * 70)
    if not sw.is_assembly:
        print("  [!] 当前活动文档**不是装配体** —— 只能打零件特征树。")
    comps = sw.components()
    print(f"  GetComponentCount = {sw.component_count()}，组件 {len(comps)} 个")
    for c in comps:
        print(f"  · {str(c.get('name')):<24} fixed={c.get('fixed')} "
              f"suppressed={c.get('suppressed')} config={c.get('config')}")
        print(f"      {c.get('path')}")

    print("\n" + "=" * 70)
    print("完整特征树（**含子特征**；配合在「配合」文件夹的子特征里）")
    print("=" * 70)
    tree = sw.feature_tree()
    for f in tree:
        print(f"  {'  ' * f['depth']}[{f['type']}] {f['name']}"
              + ("  (抑制)" if f.get("suppressed") else ""))

    print("\n" + "=" * 70)
    print("类型名汇总（★ 「配合」那一行就是要抄进 MATE_FEATURE_TYPES 的值）")
    print("=" * 70)
    cnt = Counter(f["type"] for f in tree)
    first: dict[str, str] = {}
    for f in tree:
        first.setdefault(f["type"], f["name"])
    for t, n in cnt.most_common():
        mark = ""
        if t in sw.MATE_GROUP_TYPES:
            mark = "   ← 文件夹，别抄进白名单"
        print(f"  {t:<28} ×{n:<4} 例: {first[t]}{mark}")
    print(f"\n  当前 MATE_FEATURE_TYPES = {sw.MATE_FEATURE_TYPES!r}")
    if not sw.MATE_FEATURE_TYPES:
        print("  ⇒ 还没标定。把上面「配合」那个类型名填进 "
              "sw_connect.SwSession.MATE_FEATURE_TYPES，然后跑 "
              "sw_selftest.py 确认没漂移。")


def main() -> None:
    ap = argparse.ArgumentParser(description="装配体特征树诊断 / MATE_FEATURE_TYPES 标定")
    ap.add_argument("--doc", default=None,
                    help="已有 .SLDASM 路径；不给就现场搭一个最小装配体")
    ap.add_argument("--quiet", action="store_true", help="不显示 SolidWorks 窗口")
    a = ap.parse_args()

    sw = sw_connect.SwSession.open(visible=not a.quiet)
    preflight(sw)
    if a.doc:
        m = sw.open_doc(a.doc)
        print(f"[i] 已打开: {member(m, 'GetTitle')}")
    else:
        build_min_assembly(sw)
    dump(sw)


if __name__ == "__main__":
    main()
