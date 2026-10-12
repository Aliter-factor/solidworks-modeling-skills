#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_docbuild.py —— 从本机 SolidWorks 安装里抽取官方 API 知识库。

为什么需要它：
    SolidWorks 的 API 面有 1.4 万个方法、900 多个枚举，靠模型记忆一定编造。
    但每个 SolidWorks 安装都自带完整的 API 帮助（api/HelpViewer/*/*.cab 里是 .mshc，
    .mshc 其实就是 zip，里面是全套 HTML）。本脚本把它抽成结构化 JSON，
    于是任何版本、任何机器都能现场重建一份「准确的」接口表，而不是猜。

用法：
    python sw_docbuild.py --out ./build                 # 默认按内置白名单抽（几十个接口，快）
    python sw_docbuild.py --out ./build --all           # 抽全部 14000+ 页（慢，2~5 分钟）
    python sw_docbuild.py --out ./build --interfaces ISketchManager IFeatureManager
    python sw_docbuild.py --out ./build --enums-only     # 只要枚举表
    python sw_docbuild.py --out ./build --enums swEndConditions_e swSelectType_e

产物：
    <out>/enums.json   {枚举名: {summary, members:{成员名: {value, desc}}}}
    <out>/api.json     {接口名: {summary, members:{成员名: {kind, returns, params, desc, remarks}}}}
    <out>/INDEX.md     人/AI 可读的检索入口（接口清单 + 枚举清单）

注意：
    * 全程只读 SolidWorks 安装目录；缓存写到 --cache（默认 %LOCALAPPDATA%\\YoYo\\swcache）。
    * 展开 .cab 用的是 Windows 自带 expand.exe（写进缓存，只做一次）。
    * 不依赖 pywin32 —— 这一步纯粹是文本抽取，没装 SolidWorks 也能跑（只要有 api 目录）。
"""

from __future__ import annotations

import argparse
import html as _html
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- 安装定位

# 常见安装根。C:\SW2024 是本机实测路径（用户没用默认 Program Files）。
COMMON_ROOTS = [
    r"C:\SW2024",
    r"C:\Program Files\SOLIDWORKS Corp",
    r"C:\Program Files\SolidWorks Corp",
    r"D:\SW2024",
    r"D:\Program Files\SOLIDWORKS Corp",
    r"E:\SW2024",
]

# 零件建模真正会用到的接口。--all 才跳过这个白名单。
DEFAULT_INTERFACES = [
    # 应用与文档
    "ISldWorks", "IModelDoc2", "IModelDocExtension", "IPartDoc", "IAssemblyDoc",
    "IDrawingDoc", "IModelView", "IModelViewManager", "IEnvironment", "IUserUnit",
    # 选择
    "ISelectionMgr", "ISelectData",
    # 草图
    "ISketchManager", "ISketch", "ISketchSegment", "ISketchLine", "ISketchArc",
    "ISketchSpline", "ISketchPoint", "ISketchEllipse", "ISketchParabola",
    "ISketchSlot", "ISketchContour", "ISketchRegion", "ISketchText",
    # 特征
    "IFeatureManager", "IFeature", "IExtrudeFeatureData2", "IRevolveFeatureData2",
    "IFilletFeatureData2", "ISimpleFilletFeatureData2", "IChamferFeatureData2",
    "IShellFeatureData", "IDraftFeatureData2", "ISweepFeatureData",
    "IThickenFeatureData", "IRibFeatureData2", "IWizardHoleFeatureData2",
    "ICircularPatternFeatureData", "ILocalCircularPatternFeatureData",
    "ILinearPatternFeatureData", "ILocalLinearPatternFeatureData",
    "IMirrorPatternFeatureData", "IMirrorSolidFeatureData", "ITablePatternFeatureData",
    "ICombineBodiesFeatureData", "ISplitBodyFeatureData",
    # 几何与实体
    "IModeler", "IBody2", "IFace2", "IEdge", "IVertex", "ISurface", "ICurve",
    "IMathUtility", "IMathPoint", "IMathVector", "IMathTransform",
    # 参考几何
    "IRefPlane", "IRefAxis", "IRefPoint", "IRefPlaneFeatureData",
    # 参数化与属性
    "IDimension", "IDisplayDimension", "IConfiguration", "IConfigurationManager",
    "ICustomPropertyManager", "IEquationMgr", "IDesignTable",
    # 装配与干涉
    "IComponent2", "IMate2", "IInterferenceDetectionMgr",
    # 质量属性
    "IMassProperty", "IMassProperty2",
]

_WS = re.compile(r"[ \t\xa0\u3000]+")


def find_sw_root(explicit: str | None = None) -> Path:
    """定位 SolidWorks 安装根（含 SLDWORKS.exe 的目录的上一级或本身）。"""
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    env = os.environ.get("SW_ROOT")
    if env:
        cands.append(Path(env))
    for r in COMMON_ROOTS:
        cands.append(Path(r) / "SOLIDWORKS")
        cands.append(Path(r))
    for d in "CDEFG":
        for stem in ("SW20", "SOLIDWORKS", "SolidWorks", "Program Files/SOLIDWORKS Corp"):
            try:
                for p in Path(f"{d}:/").glob(f"{stem}*"):
                    cands.append(p / "SOLIDWORKS")
                    cands.append(p)
            except (OSError, PermissionError):
                pass
    for c in cands:
        try:
            if (c / "sldworks.exe").exists() or (c / "SLDWORKS.exe").exists():
                return c.resolve()
            if (c / "api" / "HelpViewer").is_dir():
                return c.resolve()
        except (OSError, PermissionError):
            continue
    raise SystemExit(
        "找不到 SolidWorks 安装。用 --sw-root 指定，或设环境变量 SW_ROOT=\\path\\to\\SOLIDWORKS"
    )


def find_help_zips(sw_root: Path) -> dict[str, Path]:
    """把 api/HelpViewer 下的 .cab 展开成 .mshc，返回 {'api'|'enums': 路径}。"""
    hv = sw_root / "api" / "HelpViewer"
    if not hv.is_dir():
        raise SystemExit(f"{hv} 不存在 —— 这个安装没带 API 帮助，无法抽取接口表")
    wanted = {
        "api": list(hv.glob("sldworksapi/*.cab")),
        "enums": list(hv.glob("swconst/*.cab")),
    }
    out: dict[str, Path] = {}
    for key, cabs in wanted.items():
        if not cabs:
            raise SystemExit(f"在 {hv} 下找不到 {key} 的 .cab")
        cab = cabs[0]
        target = cab.with_suffix(".mshc")
        if not target.exists() or target.stat().st_mtime < cab.stat().st_mtime:
            expand = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "expand.exe"
            # expand.exe 不接受正斜杠，必须给回 Windows 反斜杠路径
            r = subprocess.run(
                [str(expand), str(cab), str(target)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if not target.exists():
                raise SystemExit(
                    f"expand 失败: {r.stdout}\n{r.stderr}\n（可手动展开 {cab} 后重跑）"
                )
        out[key] = target
    return out


# ---------------------------------------------------------------- HTML 解析

def strip_tags(raw: str, cell_sep: str = " | ", row_sep: str = "\n") -> str:
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    s = re.sub(r"</t[dh]\s*>", cell_sep, s, flags=re.I)
    s = re.sub(r"</tr\s*>", row_sep, s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = _WS.sub(" ", s)
    s = re.sub(r"\n[ \t]*\n+", "\n", s)
    return s.strip()


def meta(h: str, name: str) -> str:
    m = re.search(
        rf'<meta\s+name="{re.escape(name)}"\s+content="(.*?)"\s*/?>', h, re.S | re.I
    )
    return _html.unescape(m.group(1)).strip() if m else ""


def body_text(h: str) -> str:
    i = h.find("<body")
    return strip_tags(h[i:] if i >= 0 else h)


def first_paragraph(text: str) -> str:
    """取面包屑之后、Syntax 之前的摘要。"""
    lines = [ln.strip() for ln in text.split("\n")]
    skip = ("SOLIDWORKS API Help", "Collapse All", "Language Filter", "Visual Basic",
            "C#", "C++/CLI", "Syntax", "Example", "Members", "")
    for ln in lines:
        if ln in skip or ln.startswith("Language Filter"):
            continue
        if "Namespace" in ln and ":" in ln:      # 面包屑
            continue
        if re.fullmatch(r"[\w\.]+ (Method|Property|Interface|Delegate) \(?[\w\.]*\)?", ln):
            continue
        if len(ln) > 25 and not ln.startswith("Public Interface"):
            return ln
    return ""


# ---- 枚举页 ----

ENUM_MEMBER_RE = re.compile(
    r'<td[^>]*class="MemberNameCell"[^>]*>\s*(?:<strong>)?\s*([A-Za-z_]\w*)\s*(?:</strong>)?\s*</td>'
    r'\s*<td[^>]*class="DescriptionCell"[^>]*>(.*?)</td>',
    re.S | re.I,
)


def short_name(dotted: str) -> str:
    """'SolidWorks.Interop.swconst.swEndConditions_e' -> 'swEndConditions_e'"""
    return dotted.rsplit("~", 1)[-1].removesuffix(".html").rsplit(".", 1)[-1]


_ENUM_VAL_RE = re.compile(
    r"^(-?\d+)\s*(?:or\s*0[xX][0-9a-fA-F]+)?\s*(?:[;=]\s*(\S[\s\S]*))?$"
)


def _split_enum_desc(desc: str) -> tuple[int | None, str]:
    """枚举描述cell的三种排版都要认：
    '3 = End point is ...' / '1; See Remarks' / '2048 or 0x800; Export ...' / '0'
    """
    m = _ENUM_VAL_RE.match(desc.strip())
    if m:
        return int(m.group(1)), (m.group(2) or "").strip()
    return None, desc


def parse_enum(name_path: str, raw: str) -> tuple[str, dict]:
    name = short_name(name_path)
    members: dict[str, dict] = {}
    for mname, mdesc in ENUM_MEMBER_RE.findall(raw):
        desc = strip_tags(mdesc, cell_sep=" ", row_sep=" ")
        val, desc = _split_enum_desc(desc)
        members[mname] = {"value": val, "desc": desc}
    info = {"summary": meta(raw, "Description"), "members": members}
    # 有些枚举的含义写在 Remarks 里（例如 swEndConditions_e 各值怎么用）
    text = body_text(raw)
    m = re.search(r"Remarks\s*\n([\s\S]*?)(?:\n\s*(?:Example|See Also|Inheritance|Related)\b|\Z)", text)
    if m:
        v = _WS.sub(" ", m.group(1)).strip()
        if len(v) > 10:
            info["remarks"] = v[:1500]
    return name, info


# ---- 接口/方法页 ----

def parse_member_syntax(text: str) -> dict | None:
    """从页面纯文本里抠出 VB 声明签名。"""
    fn = re.search(
        r"\bFunction\s+(\w+)\s*\(([\s\S]*?)\)\s*As\s+([\w\.\[\]]+)", text
    )
    if fn:
        name, argblob, ret = fn.group(1), fn.group(2), fn.group(3)
        params = []
        for piece in argblob.split(","):
            piece = piece.strip().strip("_").strip()
            if not piece:
                continue
            pm = re.match(
                r"By(?:Val|Ref)\s+(\w+)\s*(?:\(\))?\s+As\s+([\w\.\(\)\[\]]+)", piece
            )
            if pm:
                params.append({"name": pm.group(1), "type": pm.group(2)})
            else:
                params.append({"name": piece, "type": "?"})
        return {"kind": "method", "returns": ret, "params": params}

    pr = re.search(r"\b(ReadOnly\s+)?Property\s+(\w+)\s*(?:\((.*?)\))?\s*As\s+([\w\.\[\]]+)", text)
    if pr:
        params = []
        if pr.group(3):
            for piece in pr.group(3).split(","):
                pm = re.match(r"By(?:Val|Ref)\s+(\w+)\s+As\s+([\w\.]+)", piece.strip())
                if pm:
                    params.append({"name": pm.group(1), "type": pm.group(2)})
        return {
            "kind": "property",
            "returns": pr.group(4),
            "params": params,
            "readonly": bool(pr.group(1)),
        }
    return None


def parse_member_page(page: str, raw: str) -> tuple[str, dict]:
    iface, member = page.rsplit("~", 1)
    iface = short_name(iface)
    member = member.removesuffix(".html")
    text = body_text(raw)
    info: dict = {"iface": iface, "summary": meta(raw, "Description") or first_paragraph(text)}

    # 参数说明藏在 <div id="SdPopupContent" class="i-popup-content">
    popups = dict(
        re.findall(
            r'<div\s+id="(\w+)PopupContent"[^>]*>(.*?)</div>', raw, re.S
        )
    )
    sig = parse_member_syntax(text)
    if sig:
        info.update(sig)
        for p in info["params"]:
            desc = strip_tags(popups.get(p["name"], ""), cell_sep=" ", row_sep=" ")
            if desc:
                p["desc"] = _WS.sub(" ", desc)

    # Remarks / Return Value 段落（坑往往写在这里）
    stop = r"(?:\n\s*(?:Example|See Also|Remarks|Return Value|Syntax|Collapse All)\b|\Z)"
    for key, pat in (
        ("remarks", r"Remarks\s*\n([\s\S]*?)" + stop),
        ("returns_desc", r"Return Value\s*\n([\s\S]*?)" + stop),
    ):
        m = re.search(pat, text)
        if m:
            v = _WS.sub(" ", m.group(1)).strip()
            if v and len(v) > 3:
                info[key] = v[:1200]
    return member, info


# ---------------------------------------------------------------- 主流程

def load_prev(path: Path) -> dict:
    """把上一次的产物读回来，本次只覆盖抽到的那些键（**增量抽取**）。

    为什么必须增量：`IFeatureManager` 一个接口就 284 个成员，`IAssemblyDoc`、
    `IDrawingDoc` 各有几百，一次全抽很慢，实际都是**分批抽**。
    而批次之间经常跨界（先抽特征、再抽装配、再抽工程图）——
    如果每次都从零写，第二批就把第一批**整个覆盖**掉（已踩，白跑一轮）。
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:                            # noqa: BLE001
        print(f"  ! 旧 {path.name} 读不动（{e}），按空处理", file=sys.stderr)
        return {}
    # ★★ 形状守卫（2026-10-10）：本位置是**接口签名库**，顶层每个值都必须是
    #   {summary, members:{…}}。若读到的是别的产物（真机踩过：被
    #   `sw_ops.py --schema` 的 op 机读导出占用了同名文件），**增量合并会把
    #   字符串当接口写进去**，然后在 `stats["members"]` 处抛
    #   `TypeError: string indices must be integers`——报错完全指不到根因。
    #   ⇒ 读进来先验形状：只要有一个键不成 shape 就不合并（按空重建），
    #     并把「这文件看起来是什么」打出来，免得静默烂在后面。
    if data and not all(
        isinstance(v, dict) and "members" in v for v in data.values()
    ):
        bad = [k for k, v in data.items()
               if not (isinstance(v, dict) and "members" in v)]
        keys = "、".join(sorted(data)[:6])
        print(
            f"  ! 旧 {path.name} **不是接口签名库**（顶层键：{keys} ……；"
            f"不成形状 {len(bad)}/{len(data)}）——这像是 `sw_ops.py --schema` 的产物。\n"
            f"    按空处理并**原地重建**，不参与增量合并。",
            file=sys.stderr,
        )
        return {}
    return data


def build(
    out_dir: Path,
    sw_root: Path,
    interfaces: list[str] | None,
    enum_filter: list[str] | None,
    do_api: bool,
    do_enums: bool,
    want_md: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    zips = find_help_zips(sw_root)
    stats = {}

    if do_enums:
        z = zipfile.ZipFile(zips["enums"])
        pages = [n for n in z.namelist() if n.endswith("_e.html")]
        enums: dict[str, dict] = load_prev(out_dir / "enums.json")
        for n in pages:
            name = short_name(n)
            if enum_filter and name not in enum_filter:
                continue
            try:
                ename, info = parse_enum(n, z.read(n).decode("utf-8", "replace"))
            except Exception as e:                      # noqa: BLE001
                print(f"  ! enum {n}: {e}", file=sys.stderr)
                continue
            enums[ename] = info
        (out_dir / "enums.json").write_text(
            json.dumps(enums, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        stats["enums"] = len(enums)
        stats["enum_members"] = sum(len(v["members"]) for v in enums.values())

    if do_api:
        z = zipfile.ZipFile(zips["api"])
        pages = [n for n in z.namelist() if n.endswith(".html")]
        if interfaces:
            keep = set(interfaces)
            member_pages = [
                n for n in pages
                if "~" in n.rsplit("/", 1)[-1]
                and n.rsplit("~", 1)[0].rsplit(".", 1)[-1] in keep
            ]
        else:
            member_pages = [n for n in pages if "~" in n.rsplit("/", 1)[-1]]

        api: dict[str, dict] = load_prev(out_dir / "api.json")
        for n in member_pages:
            try:
                member, info = parse_member_page(n, z.read(n).decode("utf-8", "replace"))
            except Exception as e:                      # noqa: BLE001
                print(f"  ! api {n}: {e}", file=sys.stderr)
                continue
            iface = info.pop("iface")
            api.setdefault(iface, {"members": {}})["members"][member] = info

        # 接口自身摘要（用接口主页）
        for iface in list(api):
            for n in pages:
                base = n.rsplit("/", 1)[-1].removesuffix(".html")
                if "~" in base:
                    continue
                if short_name(base) == iface:
                    raw = z.read(n).decode("utf-8", "replace")
                    api[iface]["summary"] = meta(raw, "Description") or first_paragraph(
                        body_text(raw)
                    )
                    break
        (out_dir / "api.json").write_text(
            json.dumps(api, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        stats["interfaces"] = len(api)
        stats["members"] = sum(len(v["members"]) for v in api.values())

    if want_md:
        write_index_md(out_dir, stats)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


def write_index_md(out_dir: Path, stats: dict) -> None:
    lines = ["# SolidWorks API 索引（自动生成，勿手改）", ""]
    lines += [
        "由 `scripts/sw_docbuild.py` 从本机 SolidWorks 自带 API 帮助抽取。",
        f"接口 {stats.get('interfaces', 0)} 个 / 成员 {stats.get('members', 0)} 个；"
        f"枚举 {stats.get('enums', 0)} 组 / 成员 {stats.get('enum_members', 0)} 个。",
        "",
    ]
    api_p = out_dir / "api.json"
    if api_p.exists():
        api = json.loads(api_p.read_text(encoding="utf-8"))
        lines += ["## 接口", ""]
        for iface in sorted(api):
            summ = api[iface].get("summary", "")
            lines.append(f"- **{iface}** — {summ[:110]}  （{len(api[iface]['members'])} 个成员）")
        lines += ["", "## 枚举", ""]
    enum_p = out_dir / "enums.json"
    if enum_p.exists():
        enums = json.loads(enum_p.read_text(encoding="utf-8"))
        for e in sorted(enums):
            lines.append(
                f"- **{e}** — {enums[e].get('summary', '')[:110]}  （{len(enums[e]['members'])}）"
            )
    (out_dir / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="抽取本机 SolidWorks API 帮助为结构化索引")
    ap.add_argument("--out", default="./swbuild", help="输出目录")
    ap.add_argument("--sw-root", default=None, help="SolidWorks 安装目录（含 sldworks.exe）")
    ap.add_argument("--all", action="store_true", help="抽全部接口（慢）")
    ap.add_argument("--interfaces", nargs="*", default=None, help="只抽这些接口")
    ap.add_argument("--enums", nargs="*", default=None, help="只抽这些枚举")
    ap.add_argument("--enums-only", action="store_true")
    ap.add_argument("--api-only", action="store_true")
    ap.add_argument("--no-md", action="store_true")
    a = ap.parse_args()

    root = find_sw_root(a.sw_root)
    print(f"SolidWorks 安装: {root}")
    if a.interfaces:
        ifaces = a.interfaces
    elif a.all:
        ifaces = None
    else:
        ifaces = DEFAULT_INTERFACES
    build(
        Path(a.out), root, ifaces, a.enums,
        do_api=not a.enums_only, do_enums=not a.api_only, want_md=not a.no_md,
    )


if __name__ == "__main__":
    main()
