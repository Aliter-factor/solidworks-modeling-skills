#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_apisig.py —— 查 SolidWorks API 签名（写 op 前的**第一步**）。

为什么需要它：
    写一个 op = 写一次 API 调用。参数个数/顺序/类型错一个就静默失败或崩，
    而 SolidWorks API 有 1.4 万个方法 —— 凭记忆写 = 编造。
    所以流程强制拆成两步：**先抽签名（sw_docbuild）→ 再写 op**。

    本脚本是给第二步用的**查询台**：把 `_build/api.json` 里的签名、参数说明、
    Remarks（坑往往写在这儿）打出来，方便边看边写。

用法
----
    python scripts/sw_apisig.py --build                     # 先抽（转发 sw_docbuild，全量白名单）
    python scripts/sw_apisig.py --build -i IAssemblyDoc IDrawingDoc
    python scripts/sw_apisig.py --sig IFeatureManager.HoleWizard5
    python scripts/sw_apisig.py --sig HoleWizard5           # 不写接口名就全局搜
    python scripts/sw_apisig.py --find RevolveCut Slot Rib  # 按关键词列方法名
    python scripts/sw_apisig.py --iface IFeatureManager     # 列该接口全部成员签名
    python scripts/sw_apisig.py --gaps                      # 列出"签名没解析出来"的成员
    python scripts/sw_apisig.py --enums swRevolveType_e     # 查枚举成员值

    python scripts/sw_apisig.py --tlb IFeatureManager.InsertRib   # ★ 类型库真签名
    python scripts/sw_apisig.py --tlb ISketchSegment              # ★ 只给接口名 → 列该接口全表
    python scripts/sw_apisig.py --tlb-find Rib                    # ★ 类型库搜方法
    python scripts/sw_apisig.py --tlb-path C:/SW2024/SOLIDWORKS/sldworks.tlb

`--gaps` 很关键：有些页面的声明不是标准 VB `Function`（例如 `Sub` 形式的
`IPartDoc.SetMaterialPropertyName2`），解析器抓不到参数 —— 这类**不能当签名用**，
要么去 Remarks 里找示例，要么回 CHM 看。它会把这些列出来，避免盲写。

`--tlb` 是 `--gaps` 的**解药**：直接读 SolidWorks 自己注册的 `sldworks.tlb`，
参数个数/名字/类型/可选数一个不差，**离线可跑、不用开 SolidWorks**。
实测 `IFeatureManager.InsertRib` 在类型库里是 **10** 个参数、返回 **void**
（文档页看不出来），而已废弃的 `IModelDoc2.InsertRib2` 只有 9 个。
写 op 前用它对一遍，能省掉整轮真机试错。

退出码：0 = 有结果；1 = 没找到 / 产物缺失。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BUILD = HERE.parent / "_build"


# ---------------------------------------------------------------- 载入

def load(build: Path, name: str) -> dict:
    p = build / name
    if not p.exists():
        raise SystemExit(
            f"找不到 {p}\n先跑一次抽取：python scripts/sw_apisig.py --build"
        )
    return json.loads(p.read_text(encoding="utf-8"))


def load_api(build: Path) -> dict:
    """读 `api.json`（**API 签名库**），并挡住「读到的是别的产物」这种静默错配。

    ★★ 为什么必须挡（2026-10-09 真机踩到）：本机 `_build/api.json` 与
    `assets/op_schema.json` **字节完全相同**（同一 md5，都是 `sw_ops.py --schema`
    的产物）—— 也就是说这个位置**被 op 机读导出占用了**。此时
    `--gaps` 会抛 `AttributeError: 'list' object has no attribute 'get'`，
    **报错指向 cmd_gaps 内部一行，完全指不到"你读的不是签名库"**。
    ⇒ 读进来先验形状：顶层每个值都必须是 `{summary, members:{…}}`；
      不是就报「这文件看起来是什么 / 该怎么重建」，而不是让它烂在后面。

    ★ 顺带说明：**装配层的签名本来就不走这条路** —— 装配 op 是用
    `--tlb` / `--tlb-find` 直查 `sldworks.tlb` 写出来的（类型库是最终裁判）。
    所以「签名库被占用」不影响装配，只影响 `--sig` / `--find` / `--iface` / `--gaps`。
    """
    data = load(build, "api.json")
    if not isinstance(data, dict) or not data:
        raise SystemExit(f"★ {build / 'api.json'} 读出来不是对象（{type(data).__name__}）")
    bad = [k for k, v in data.items() if not (isinstance(v, dict) and "members" in v)]
    if len(bad) == len(data):
        keys = "、".join(sorted(data)[:6])
        raise SystemExit(
            f"★ {build / 'api.json'} **不是 API 签名库**。\n"
            f"  它的顶层键是：{keys} ……（共 {len(data)} 个）\n"
            f"  这看起来是 `sw_ops.py --schema` 的产物（op 机读导出），"
            f"不是 `sw_docbuild.py` 的接口签名库。\n"
            f"  ⇒ 两条路：① 用 `--tlb` / `--tlb-find` **直查类型库**（最终裁判，装配层就是这么做的）；\n"
            f"     ② 先重建签名库（`python scripts/sw_apisig.py --build`）再跑 `--sig/--iface/--gaps`。"
        )
    return data


def sig_of(info: dict) -> str:
    """把一条成员记录还原成一行可读签名。"""
    params = info.get("params")
    if params is None:
        return "() -> ?   ⚠ 参数没解析出来，见 --gaps"
    inner = ", ".join(f"{p['name']}: {p['type']}" for p in params)
    kind = info.get("kind", "?")
    ret = info.get("returns", "?")
    tail = "   [只读]" if info.get("readonly") else ""
    return f"({inner}) -> {ret}   [{kind}]{tail}"


def wrap(text: str, indent: str = "     ", width: int = 96) -> list[str]:
    """按宽度折行（中文按 2 列算），免得长 desc 把终端撑爆。"""
    out, line, used = [], "", 0
    for ch in text.replace("\n", " "):
        w = 2 if ord(ch) > 0x2000 else 1
        if used + w > width:
            out.append(indent + line)
            line, used = "", 0
        line += ch
        used += w
    if line:
        out.append(indent + line)
    return out


# ---------------------------------------------------------------- 命令

def cmd_sig(args, api: dict) -> int:
    hits = 0
    for spec in args.sig:
        iface, _, member = spec.rpartition(".")
        for iname in sorted(api):
            if iface and iname != iface:
                continue
            ms = api[iname].get("members", {})
            if member in ms:
                info = ms[member]
                hits += 1
                print(f"\n=== {iname}.{member}")
                print(f"  {sig_of(info)}")
                if info.get("summary"):
                    print(*wrap(info["summary"], "  说明: "))
                for i, p in enumerate(info.get("params") or [], 1):
                    print(f"    {i:2d}. {p['name']} : {p['type']}")
                    if p.get("desc"):
                        print(*wrap(p["desc"], "         "))
                if info.get("returns_desc"):
                    print(*wrap(info["returns_desc"], "  返回: "))
                if info.get("remarks"):
                    print(*wrap(info["remarks"], "  ⚠ 注意: "))
    if not hits:
        print(f"没找到: {', '.join(args.sig)}（试 --find 搜关键词）")
        return 1
    return 0


def cmd_find(args, api: dict) -> int:
    hits = 0
    for kw in args.find:
        print(f"\n### 含 '{kw}' 的成员")
        for iname in sorted(api):
            for mname in sorted(api[iname].get("members", {})):
                if kw.lower() in mname.lower():
                    info = api[iname]["members"][mname]
                    print(f"  {iname}.{mname}{sig_of(info)}")
                    hits += 1
    if not hits:
        print("没有匹配。")
        return 1
    return 0


def cmd_iface(args, api: dict) -> int:
    rc = 0
    for iname in args.iface:
        if iname not in api:
            print(f"没抽过接口 {iname}（不是所有接口都在 CHM 里，或还没抽）")
            rc = 1
            continue
        ms = api[iname].get("members", {})
        print(f"\n### {iname}  （{len(ms)} 个成员）")
        if api[iname].get("summary"):
            print(*wrap(api[iname]["summary"], "  "))
        for mname in sorted(ms):
            print(f"  {mname}{sig_of(ms[mname])}")
    return rc


def cmd_gaps(args, api: dict) -> int:
    print("以下成员的**参数没能解析出来**，不能当签名用（多半是 Sub 形式或")
    print("声明跨行）。写 op 前请去 CHM 或页面 Remarks 里核对：\n")
    n = 0
    for iname in sorted(api):
        for mname, info in sorted(api[iname].get("members", {}).items()):
            if info.get("kind") == "method" and info.get("params") is None:
                print(f"  {iname}.{mname}")
                n += 1
    print(f"\n共 {n} 个。")
    return 0 if n == 0 else 0


def cmd_enums(args, build: Path) -> int:
    enums = load(build, "enums.json")
    hits, rc = 0, 1
    for kw in args.enums:
        for ename in sorted(enums):
            if kw.lower() not in ename.lower():
                continue
            hits += 1
            rc = 0
            print(f"\n### {ename}  —— {enums[ename].get('summary', '')[:150]}")
            for m, info in enums[ename].get("members", {}).items():
                print(f"  {info.get('value')!s:>6}  {m}   {info.get('desc', '')[:80]}")
            if enums[ename].get("remarks"):
                print(*wrap(enums[ename]["remarks"], "  ⚠ "))
    if not hits:
        print(f"没找到枚举: {', '.join(args.enums)}")
    return rc


# ------------------------------------------------- 类型库（typelib）—— 最终裁判
#
# 为什么非要有这一层：
#   CHM 抽出来的签名有**缺口**（见 --gaps），像 IFeatureManager.InsertRib 这种
#   声明在页面上不是标准 VB Function 形式，参数就抓不到。这时候光看文档只会瞎写，
#   而真机调用失败信息又很含糊（例如 DISP_E_PARAMNOTOPTIONAL 只告诉你"少了必填参数"，
#   不说少几个、少哪个）。
#
#   sldworks.tlb 是 SolidWorks 自己注册的类型库 —— 里面是**编译器级**的真参数表：
#   参数个数、名字、类型、是否可选，一个不差。
#   本机实测：IFeatureManager.InsertRib 有 **10** 个参数（比 Obsolete 的
#   IModelDoc2.InsertRib2 多一个 IsDraftedFromWall），且返回 **void** ——
#   这两点文档页上都没写清楚，但决定了 op 能不能写对。
#
#   所以流程升级为：--sig（看说明/Remarks）→ **--tlb（对数）** → 再写 op。

_VT_NAMES = {
    0: "Variant", 1: "Variant", 2: "Short", 3: "Long", 4: "Single", 5: "Double",
    6: "Currency", 7: "Date", 8: "String", 9: "Object", 10: "Long", 11: "Boolean",
    12: "Variant", 13: "Object", 14: "Decimal", 16: "Byte", 17: "Byte",
    18: "Word", 19: "ULong", 20: "LongLong", 21: "ULongLong", 22: "Int",
    23: "UInt", 24: "void", 25: "HRESULT", 26: "对象(IFeature 等)",
    27: "SafeArray", 28: "CArray", 29: "用户定义类型",
}

# 面向对象阅读的常用别名（typelib 里 vt=9/13 都是"对象"，看不出是哪个接口）
_VT_HINT = {9: "对象(IDispatch)", 13: "对象(IUnknown)"}


def _vt(vt) -> str:
    """VARTYPE → 可读名。

    PyFUNCDESC 的元素描述是**多层嵌套元组**（实测 (vt, flags, hint)，
    对象类型还会再套一层），所以这里层层剥到整数为止。
    """
    depth = 0
    while isinstance(vt, (tuple, list)) and depth < 4:
        vt = vt[0] if vt else 0
        depth += 1
    if not isinstance(vt, int):
        return str(vt)
    base = vt & 0x0FFF
    tags = []
    if vt & 0x2000:                       # VT_ARRAY
        tags.append("Array of")
    if vt & 0x4000:                       # VT_BYREF
        tags.append("ByRef")
    nm = _VT_NAMES.get(base) or _VT_HINT.get(base) or f"vt=0x{vt:04x}"
    return (" ".join(tags) + " " + nm).strip() if tags else nm


def _tlb_path(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    if os.environ.get("SW_TLB"):
        p = Path(os.environ["SW_TLB"])
        if p.exists():
            return p
    try:
        import sw_env
        inst = sw_env.find_install()
    except Exception:                                          # noqa: BLE001
        inst = None
    if inst:
        for nm in ("sldworks.tlb", "sldworks.olb", "sldworks.dll"):
            if (inst / nm).exists():
                return inst / nm
    return None


def _load_tlb(explicit: str | None = None):
    try:
        import pythoncom
    except ImportError as e:                                   # noqa: BLE001
        raise SystemExit(f"读类型库需要 pywin32（{e}）。装：<python> -m pip install pywin32")
    p = _tlb_path(explicit)
    if p is None:
        raise SystemExit("找不到 sldworks.tlb；用 --tlb-path 显式指定，或设 SW_TLB 环境变量")
    try:
        return pythoncom.LoadTypeLib(str(p)), p
    except Exception as e:                                     # noqa: BLE001
        raise SystemExit(f"加载类型库失败 {p}: {e}")


def _tlb_walk(tlb):
    """产出 (接口名, 方法名, 参数名列表, 参数类型列表, 返回类型, 可选参数数)。

    PyITypeLib **没有** GetTypeInfoByName（本机 pywin32 实测），只能按文档名遍历。
    """
    for k in range(tlb.GetTypeInfoCount()):
        doc = tlb.GetDocumentation(k)
        iname = doc[0] if doc else None
        if not iname:
            continue
        try:
            ti = tlb.GetTypeInfo(k)
            attr = ti.GetTypeAttr()
        except Exception:                                      # noqa: BLE001
            continue
        for i in range(attr.cFuncs):
            try:
                fd = ti.GetFuncDesc(i)
                names = ti.GetNames(fd.memid)
            except Exception:                                  # noqa: BLE001
                continue
            if not names or not names[0]:
                continue
            args = list(fd.args or [])
            pnames = [names[j + 1] if j + 1 < len(names) else f"<arg{j}>"
                      for j in range(len(args))]
            ptypes = [_vt(a) for a in args]
            yield (iname, names[0], pnames, ptypes, _vt(fd.rettype),
                   getattr(fd, "cParamsOpt", 0) or 0)


def _print_tlb_hit(iname, mname, pnames, ptypes, ret, nopt, mark=False):
    star = "★ " if mark else "  "
    opt = f"，其中 {nopt} 个可选" if nopt else ""
    print(f"\n{star}{iname}.{mname}   [{len(pnames)} 个参数{opt}] -> {ret}")
    for j, (n, t) in enumerate(zip(pnames, ptypes), 1):
        print(f"    {j:2d}. {n:28s} : {t}")


def cmd_tlb(args) -> int:
    tlb, path = _load_tlb(args.tlb_path)
    print(f"# 类型库：{path}")
    specs = args.tlb or []
    hits = 0
    for spec in specs:
        iface, _, meth = spec.rpartition(".")
        found = False
        for iname, mname, pn, pt, ret, nopt in _tlb_walk(tlb):
            if iface:
                # "接口.方法" —— 接口与方法都精确匹配
                if mname.lower() != meth.lower():
                    continue
                if iname.lower() != iface.lower():
                    continue
            else:
                # 只给一个词（没有点）：**既能当方法名，也能当接口名**。
                # 早先只比方法名，于是 `--tlb ISketchSegment` 直接回「没找到」——
                # 而它明明在类型库里（真机踩到：ISketchSegment / IDisplayDimension
                # 都查不出来，白绕了一圈去开 SolidWorks 枚举）。
                if meth.lower() != mname.lower() and meth.lower() != iname.lower():
                    continue
            _print_tlb_hit(iname, mname, pn, pt, ret, nopt)
            hits += 1
            found = True
        if not found:
            print(f"\n  没找到 {spec}")
    return 0 if hits else 1


def cmd_tlb_find(args) -> int:
    tlb, path = _load_tlb(args.tlb_path)
    print(f"# 类型库：{path}")
    hits = 0
    for kw in args.tlb_find:
        print(f"\n### 方法名含 '{kw}'")
        for iname, mname, pn, pt, ret, nopt in _tlb_walk(tlb):
            if kw.lower() in mname.lower():
                _print_tlb_hit(iname, mname, pn, pt, ret, nopt)
                hits += 1
    return 0 if hits else 1


def cmd_build(args) -> int:
    cmd = [sys.executable, str(HERE / "sw_docbuild.py"), "--out", str(args.out)]
    if args.interfaces:
        cmd += ["--interfaces", *args.interfaces]
    if args.enums_filter:
        cmd += ["--enums", *args.enums_filter]
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd).returncode


# ---------------------------------------------------------------- CLI

def main() -> None:
    ap = argparse.ArgumentParser(
        description="查 SolidWorks API 签名（写 op 的第一步）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--build", action="store_true", help="先抽签名（转发 sw_docbuild.py）")
    ap.add_argument("-i", "--interfaces", nargs="*", default=None,
                    help="--build 时限定接口")
    ap.add_argument("--enums-filter", nargs="*", default=None,
                    help="--build 时限定枚举")
    ap.add_argument("--out", default=str(DEFAULT_BUILD), help="抽取产物目录")
    ap.add_argument("--in", dest="indir", default=None, help="查询用的产物目录（默认同 --out）")
    ap.add_argument("--sig", nargs="*", default=[], help="打印 sign：接口.成员 / 光成员名")
    ap.add_argument("--find", nargs="*", default=[], help="按关键词搜成员名")
    ap.add_argument("--iface", nargs="*", default=[], help="列出整个接口")
    ap.add_argument("--gaps", action="store_true", help="列出签名没解析出来的成员")
    ap.add_argument("--enums", nargs="*", default=[], help="查枚举")
    ap.add_argument("--tlb", nargs="*", default=[],
                    help="【真签名】从 sldworks.tlb 读：接口.成员 / 光成员名")
    ap.add_argument("--tlb-find", nargs="*", default=[],
                    help="【真签名】按关键词在类型库里搜方法名")
    ap.add_argument("--tlb-path", default=None, help="显式指定 sldworks.tlb 路径")
    a = ap.parse_args()

    if a.build:
        return_rc = cmd_build(a)
        if not (a.sig or a.find or a.iface or a.gaps or a.enums
                or a.tlb or a.tlb_find):
            sys.exit(return_rc)

    build = Path(a.indir or a.out)

    if a.tlb:
        rc = cmd_tlb(a)
    elif a.tlb_find:
        rc = cmd_tlb_find(a)
    elif a.sig:
        api = load_api(build)
        rc = cmd_sig(a, api)
    elif a.find:
        api = load_api(build)
        rc = cmd_find(a, api)
    elif a.iface:
        api = load_api(build)
        rc = cmd_iface(a, api)
    elif a.gaps:
        api = load_api(build)
        rc = cmd_gaps(a, api)
    elif a.enums:
        rc = cmd_enums(a, build)
    else:
        ap.print_help()
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    main()
