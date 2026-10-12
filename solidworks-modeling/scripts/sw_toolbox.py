#!/usr/bin/env python3
"""Toolbox 标准件库的调用入口（含调研结论）。

调研结论 —— 2026-09 在本机（SW2024 Premium，库在 C:\\SOLIDWORKS Data）实测
--------------------------------------------------------------------------
**没有**「给我一个 M8×20 六角螺栓」这种零件级 Toolbox API。官方那套
`Toolbox Browser API`（SOLIDWORKS.Interop.swbrowser.dll）是**给 PDM 用的**：
要先用 `ISldWorks::GetAddInObject("{ED783340-D5DB-11d4-BD5A-00C04F019809}")`
取出 Toolbox 浏览器插件对象，解决的是「谁能访问被 PDM 托管的 Toolbox 部件」，
不是拿来生成几何的。`sldworks.tlb` 里跟 Toolbox 沾边的只有 5 个成员
（ExportToolboxItem / ImportToolboxItem / ToolboxPartType / UpdateToolboxComponent /
IncludeToolboxComponents），也都是装配体属性层面的东西。

**官方那套后期绑定下拿不到**（实测）：GetAddInObject("{ED783340-…}") 回 None，
app 上的 InsertSmartFastener / GetSmartFastener / SmartFastener 全都取不到。

**可以用的路径是直接读库文件**（只读、当参考库）：

    <Toolbox 根>\\browser\\<标准>\\<分类>\\<子类>\\<零件名> <标准>.sldprt
    本机实测根目录 = C:\\SOLIDWORKS Data
    标准齐全：GB / ISO / DIN / JIS / Ansi Metric / Ansi Inch / BSI / AS / KS …
    分类示例：bolts and studs / screws / nuts / washers and rings / bearing /
              pins and keys / power transmission / structural members / seals …

    ★ 但「规格」**不**在配置里：实测 hexagon head bolts 只回 Default / PreviewCfg 两个，
    规格是 Toolbox 运行时按自己的数据库算的。所以拿不到 M8x20 这种规格，
    只能当尺寸参考库：打开对文件量一下，把数字抄进计划参数。

用法
----
    python scripts/sw_toolbox.py --list                     # 本机装了哪些标准/分类
    python scripts/sw_toolbox.py --find 六角                 # 关键词找零件文件
    python scripts/sw_toolbox.py --find bearing --limit 20
    python scripts/sw_toolbox.py --open <文件>               # 打开并列出配置
    python scripts/sw_toolbox.py --open <文件> --config X --out <目标路径>
        # ↑ 仅当库文件真的带该配置时可用；Toolbox 原生件的规格不要指望能这样切

**结论**：标准件要「出零件」走**参数化模板**（bolt / gear / leadscrew）；
Toolbox 只作尺寸参考库。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sw_connect import SwError, SwSession, member  # noqa: E402

DEFAULT_ROOTS = (
    r"C:\SOLIDWORKS Data",
    r"D:\SOLIDWORKS Data",
    r"E:\SOLIDWORKS Data",
    r"C:\ProgramData\SOLIDWORKS\SOLIDWORKS Data",
)


def find_root(explicit: str | None = None) -> Path:
    """定位 Toolbox 根目录（就是含 browser 子目录的那层）。"""
    cands = [Path(explicit)] if explicit else [Path(p) for p in DEFAULT_ROOTS]
    for p in cands:
        if (p / "browser").is_dir():
            return p
    for drive in ("C:", "D:", "E:", "F:"):
        p = Path(drive) / "SOLIDWORKS Data"
        if (p / "browser").is_dir():
            return p
    raise SwError(
        "没找到 Toolbox 根目录。它在「含 browser 子目录的那一层」，常见位置：\n"
        "  C:\\SOLIDWORKS Data\n"
        "  <安装盘>:\\ProgramData\\SOLIDWORKS\\SOLIDWORKS Data\n"
        "  用 --root 显式指定；没装 Toolbox（Standard 版不带）就跳过。\n"
        "★ 找法：默认库里一定有 browser/ToolboxFiles.index 这个文件。")


def list_standards(root: Path) -> dict[str, list[str]]:
    """{标准: [分类, …]}"""
    out: dict[str, list[str]] = {}
    for std in sorted(p for p in (root / "browser").iterdir() if p.is_dir()):
        cats = sorted(c.name for c in std.iterdir() if c.is_dir())
        out[std.name] = cats
    return out


def find_parts(root: Path, keyword: str, limit: int = 40) -> list[Path]:
    kw = keyword.lower()
    hits: list[Path] = []
    for p in (root / "browser").rglob("*.sldprt"):
        if kw in p.name.lower() or kw in str(p.parent).lower():
            hits.append(p)
            if len(hits) >= limit:
                break
    return hits


def _cli() -> None:
    ap = argparse.ArgumentParser(description="SolidWorks Toolbox 标准件调用")
    ap.add_argument("--root", default=None, help="Toolbox 根目录（含 browser 的那层）")
    ap.add_argument("--list", action="store_true", help="列出本机的标准与分类")
    ap.add_argument("--find", default=None, help="按关键词找标准件文件")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--open", default=None, help="打开某个标准件文件并列出规格配置")
    ap.add_argument("--config", default=None, help="切到该配置（规格）")
    ap.add_argument("--out", default=None, help="配合 --config：另存为独立零件")
    ap.add_argument("--json", dest="as_json", default=None, help="结果写成 JSON")
    a = ap.parse_args()

    root = find_root(a.root)
    if a.list or a.find:
        print(f"Toolbox 根目录: {root}")
    if a.list:
        stds = list_standards(root)
        print(f"共 {len(stds)} 个标准：")
        for std, cats in stds.items():
            print(f"  · {std}")
            print(f"      {' / '.join(cats)}")
        if a.as_json:
            Path(a.as_json).write_text(json.dumps(stds, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        return

    if a.find:
        hits = find_parts(root, a.find, a.limit)
        print(f"命中 {len(hits)} 个：")
        for h in hits:
            print(f"  {h.relative_to(root / 'browser')}")
        return

    if a.open:
        sw = SwSession.open(visible=True)
        try:
            sw.open_doc(a.open)
            cfgs = []
            with contextlib.suppress(Exception):
                cfgs = [str(c) for c in (member(sw.model, "GetConfigurationNames") or ())]
            print(f"标准件: {Path(a.open).name}")
            print(f"共 {len(cfgs)} 个规格配置：")
            for c in cfgs:
                print(f"  · {c}")
            if a.config:
                if not sw.model.ShowConfiguration2(a.config):
                    raise SwError(f"切配置失败: {a.config}（上面列表里有这个名字吗？）")
                print(f"\n已切到配置 {a.config}")
                if a.out:
                    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
                    p = sw.save_as(a.out, overwrite=True)
                    print(f"已另存为 {p}")
            if a.as_json:
                Path(a.as_json).write_text(
                    json.dumps({"file": a.open, "configs": cfgs}, ensure_ascii=False, indent=2),
                    encoding="utf-8")
        finally:
            with contextlib.suppress(Exception):
                sw.close()
        return

    ap.print_help()


if __name__ == "__main__":
    _cli()
