"""从本机 sldworks.tlb 导出「成员是方法还是属性」的判据。

为什么必须做这件事
------------------
SolidWorks 拒绝 pywin32 生成 makepy（报 "can not automate the makepy process"），
所以只能用**晚期绑定**。晚期绑定下 pywin32 靠类型库的 invoke kind 决定行为：

  - invkind == INVOKE_FUNC(1)        → 是方法，Python 必须写 GetTitle()
  - invkind == PROPERTYGET(2)/PUT(4) → 是属性，Python 必须写 GetTitle（不带括号）

写错就是 `TypeError: 'str' object is not callable` —— 而这个错误看起来像是
"对象不对"，实际只是括号多了。SolidWorks API 里大量 `Get*` 成员是**属性**，
所以在 VBA 里写 `swModel.GetTitle` 是不带括号的。

用法
----
    python sw_member_kind.py                      # 打印统计 + 存 assets/sw_member_kind.json
    python sw_member_kind.py --query GetTitle     # 查某个成员
    python sw_member_kind.py --iface IModelDoc2   # 列出某接口的全部成员
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

try:
    import pythoncom
except ImportError:  # pragma: no cover
    pythoncom = None

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TLB = r"C:\SW2024\SOLIDWORKS\sldworks.tlb"
DEFAULT_OUT = os.path.join(HERE, "..", "assets", "sw_member_kind.json")

INVOKE_FUNC = 1
INVOKE_PROPERTYGET = 2
INVOKE_PROPERTYPUT = 4
INVOKE_PROPERTYPUTREF = 8

# typekind
TK_ENUM, TK_RECORD, TK_MODULE, TK_INTERFACE, TK_DISPATCH = 0, 1, 2, 3, 4
TK_COCLASS, TK_ALIAS, TK_UNION = 5, 6, 7

TK_NAME = {
    TK_ENUM: "enum", TK_RECORD: "record", TK_MODULE: "module",
    TK_INTERFACE: "interface", TK_DISPATCH: "dispatch", TK_COCLASS: "coclass",
    TK_ALIAS: "alias", TK_UNION: "union",
}


def find_tlb(explicit: str | None = None) -> str:
    """按优先级找一个可用的 sldworks.tlb。"""
    cands = []
    if explicit:
        cands.append(explicit)
    # 环境变量优先（换机器不用改代码），本机常见路径只作兜底
    root = os.environ.get("SW_ROOT") or os.environ.get("SOLIDWORKS_ROOT")
    if root:
        cands.append(os.path.join(root, "sldworks.tlb"))
    cands.append(DEFAULT_TLB)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    # 兜底：扫一遍盘根下的常见位置
    import glob
    for pat in (r"C:\SW*\SOLIDWORKS\sldworks.tlb", r"C:\Program Files\SOLIDWORKS*\sldworks.tlb"):
        hit = sorted(glob.glob(pat))
        if hit:
            return hit[-1]
    raise FileNotFoundError("找不到 sldworks.tlb，请用 --tlb 显式指定")


def _doc_name(ti) -> str:
    try:
        return ti.GetDocumentation(-1)[0] or ""
    except Exception:
        return ""


def dump(tlb_path: str) -> dict:
    """扫描类型库，返回 {接口名: {成员名: "method"|"property"}}。"""
    tl = pythoncom.LoadTypeLib(tlb_path)
    out: dict[str, dict[str, str]] = {}
    stats = defaultdict(int)

    for i in range(tl.GetTypeInfoCount()):
        ti = tl.GetTypeInfo(i)
        ta = ti.GetTypeAttr()
        kind = ta.typekind
        stats[TK_NAME.get(kind, str(kind))] += 1
        if kind not in (TK_INTERFACE, TK_DISPATCH):
            continue
        iname = _doc_name(ti)
        if not iname:
            continue

        # 同一个 memid 可能同时有 propget 与 propput 两条 funcdesc；
        # 只要有一条不是 INVOKE_FUNC，它就是属性。
        by_memid: dict[int, list[int]] = defaultdict(list)
        mem_names: dict[int, str] = {}
        for j in range(ta.cFuncs):
            try:
                fd = ti.GetFuncDesc(j)
            except Exception:
                continue
            by_memid[fd.memid].append(fd.invkind)
            if fd.memid not in mem_names:
                try:
                    names = ti.GetNames(fd.memid)
                except Exception:
                    names = ()
                if names:
                    mem_names[fd.memid] = names[0]

        members: dict[str, str] = {}
        for memid, kinds in by_memid.items():
            name = mem_names.get(memid)
            if not name:
                continue
            members[name] = "property" if any(k != INVOKE_FUNC for k in kinds) else "method"

        if members:
            out[iname] = dict(sorted(members.items()))

    return {"tlb": tlb_path, "types": out, "stats": dict(stats)}


def _vtype_name(vt: int) -> str:
    """把 VARIANT 类型号翻成人话（够用就行，不追求全覆盖）。"""
    base = vt & 0x0FFF
    names = {
        0: "Empty", 1: "Null", 2: "I2", 3: "I4", 4: "R4", 5: "R8", 6: "CY",
        7: "Date", 8: "BSTR", 9: "IDispatch", 10: "Error", 11: "BOOL",
        12: "Variant", 13: "IUnknown", 14: "Decimal", 16: "I1", 17: "UI1",
        18: "UI2", 19: "UI4", 20: "I8", 21: "UI8", 22: "Int", 23: "Uint",
        24: "Void", 25: "HResult", 26: "Ptr", 27: "SafeArray", 28: "CArray",
        29: "UserDefined", 30: "LPStr", 31: "LPWStr", 36: "Record",
    }
    n = names.get(base, f"vt{base}")
    if vt & 0x2000:      # VT_ARRAY
        n = f"{n}[]"
    if vt & 0x4000:      # VT_BYREF
        n = f"{n}*"
    return n


def dump_signatures(tlb_path: str) -> dict[str, list[dict]]:
    """返回 {成员名: [{接口, 签名, 返回类型, 参数}]}，用于查权威调用签名。"""
    tl = pythoncom.LoadTypeLib(tlb_path)
    out: dict[str, list[dict]] = defaultdict(list)

    for i in range(tl.GetTypeInfoCount()):
        ti = tl.GetTypeInfo(i)
        ta = ti.GetTypeAttr()
        if ta.typekind not in (TK_INTERFACE, TK_DISPATCH):
            continue
        iname = _doc_name(ti)
        if not iname:
            continue
        for j in range(ta.cFuncs):
            try:
                fd = ti.GetFuncDesc(j)
                names = ti.GetNames(fd.memid)
            except Exception:
                continue
            if not names:
                continue
            mname = names[0]
            params, idx = [], 1
            # pywin32 不同版本里参数字段叫 params 或 args，都试一下
            arglist = []
            for attr in ("params", "args"):
                v = getattr(fd, attr, None)
                if v:
                    arglist = list(v)
                    break
            for k, a in enumerate(arglist):
                # 元素一般是 (flags, vartype)；保守地多试几种形态
                if isinstance(a, (tuple, list)) and len(a) >= 2:
                    flags, vt = a[0], a[1]
                else:
                    flags, vt = 0, a
                pname = names[k + 1] if k + 1 < len(names) else f"p{k}"
                ist = "Optional " if isinstance(flags, int) and flags & 0x08 else ""
                tip = _vtype_name(vt) if isinstance(vt, int) else str(vt)
                params.append(f"{ist}{tip} {pname}")
                idx += 1
            kinds = {INVOKE_FUNC: "", INVOKE_PROPERTYGET: "get ",
                     INVOKE_PROPERTYPUT: "put ", INVOKE_PROPERTYPUTREF: "putref "}
            kind = kinds.get(fd.invkind, str(fd.invkind))
            # 返回类型：elemdescFunc 的第 0 项是 vartype
            ret = None
            edf = getattr(fd, "elemdescFunc", None)
            if isinstance(edf, (tuple, list)) and edf:
                ret = edf[0]
            sig = f"{kind}{mname}({', '.join(params)})"
            if isinstance(ret, int) and ret:
                sig = f"{sig} -> {_vtype_name(ret)}"
            out[mname].append({"interface": iname, "signature": sig,
                               "nargs": len(params), "invkind": fd.invkind})
    return dict(out)


def flatten(types: dict) -> dict[str, str]:
    """把 {接口: {成员: 种类}} 压成 {成员: 种类}，用于按名字快查。

    同名成员在不同接口里种类不一致时按 **property 优先** 合并：
    把属性误当方法写多一对括号，会立刻炸出 'str' object is not callable；
    反过来把方法误当属性则不会报错，只会静默拿到错的东西 —— 所以宁可往会报错那边倒。
    """
    flat: dict[str, str] = {}
    for members in types.values():
        for name, kind in members.items():
            if flat.get(name) != "property":
                flat[name] = kind
    return dict(sorted(flat.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description="导出 SolidWorks 类型库的方法/属性判据")
    ap.add_argument("--tlb", help="sldworks.tlb 路径")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出 JSON（默认 assets/sw_member_kind.json）")
    ap.add_argument("--query", help="查某个成员名（模糊匹配）")
    ap.add_argument("--iface", help="列出某个接口的成员")
    ap.add_argument("--sig", help="打印某成员的权威调用签名（参数名与类型）")
    a = ap.parse_args()

    if pythoncom is None:
        print("需要 pywin32（pythoncom）。装法见 SKILL.md")
        return 2

    tlb = find_tlb(a.tlb)
    data = dump(tlb)
    types, stats = data["types"], data["stats"]
    flat = flatten(types)

    if a.iface:
        hit = {k: v for k, v in types.items() if a.iface.lower() in k.lower()}
        if not hit:
            print(f"没有匹配 {a.iface!r} 的接口")
            return 1
        for iname, members in hit.items():
            print(f"\n=== {iname} ===")
            for n, k in members.items():
                print(f"  {k:9s} {n}")
        return 0

    if a.sig:
        sigs = dump_signatures(tlb)
        q = a.sig.lower()
        hits = {n: v for n, v in sigs.items() if q == n.lower()} or \
               {n: v for n, v in sigs.items() if q in n.lower()}
        if not hits:
            print(f"没有匹配 {a.sig!r} 的成员")
            return 1
        for n, lst in sorted(hits.items()):
            for e in lst:
                print(f"  [{e['interface']}] {e['signature']}    ({e['nargs']} 参)")
        print(f"\n命中 {len(hits)} 个成员")
        return 0

    if a.query:
        q = a.query.lower()
        hits = {n: k for n, k in flat.items() if q in n.lower()}
        for n, k in sorted(hits.items()):
            print(f"  {k:9s} {n}   {'(() 必须带括号)' if k == 'method' else '(不带括号)'}")
        print(f"\n命中 {len(hits)} 条 / 成员总数 {len(flat)}")
        return 0

    n_prop = sum(1 for v in flat.values() if v == "property")
    print(f"类型库: {tlb}")
    print(f"  类型统计: {stats}")
    print(f"  接口数: {len(types)}   成员名总数: {len(flat)}"
          f"  其中属性 {n_prop} 方法 {len(flat) - n_prop}")

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    payload = {"source": os.path.basename(tlb), "interfaces": types, "members": flat}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"  已写出 {os.path.abspath(a.out)}  ({os.path.getsize(a.out) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
