#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_env.py —— SolidWorks 环境探测（纯标准库，**不需要** pywin32、不需要开 SolidWorks）。

为什么单独一层：
    计划校验（sw_plan --check）、几何自检（sw_gear）、宏翻译（sw_macro_to_plan）
    这些都不该要求本机装着 SolidWorks 且授权可用。把它们和 COM 层分开，
    才能做到「离线开发 / 上线执行」，也让 CI 和单元测试跑得起来。

被探测的东西：
    · SolidWorks 装在哪（含非默认路径，例如本机的 C:\\SW2024\\SOLIDWORKS）
    · 默认零件/装配/工程图模板（gb_part.prtdot 等，中文环境通常是 GB 模板）
    · API 帮助在哪（供 sw_docbuild 生成接口表）
    · pywin32 在不在、Python 位数和 SolidWorks 位数是否匹配（不匹配连不上 COM）
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import platform
import struct
import sys
from pathlib import Path

# 常见安装根（本机实测在 C:\SW2024，不是默认的 Program Files）
SEARCH_ROOTS = [
    r"C:\SW2024", r"C:\SW2023", r"C:\SW2025",
    r"C:\Program Files\SOLIDWORKS Corp", r"C:\Program Files\SolidWorks Corp",
    r"D:\SW2024", r"D:\Program Files\SOLIDWORKS Corp",
    r"E:\SW2024", r"E:\Program Files\SOLIDWORKS Corp",
]

TEMPLATE_DIR_HINTS = [
    r"C:\ProgramData\SOLIDWORKS",           # 标准模板目录（gb_*.prtdot 在这里）
    r"C:\ProgramData\SolidWorks",
]


def find_install(explicit: str | None = None) -> Path | None:
    """返回 SolidWorks 安装目录（含 sldworks.exe），找不到返回 None。"""
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit))
    for var in ("SW_ROOT", "SOLIDWORKS_ROOT"):
        if os.environ.get(var):
            cands.append(Path(os.environ[var]))
    cands += [Path(r) for r in SEARCH_ROOTS]
    cands += [Path(r) / "SOLIDWORKS" for r in SEARCH_ROOTS]
    # 扫各盘符下形如 SW20xx / SOLIDWORKS* 的目录
    for drive in "CDEFG":
        for pat in (f"{drive}:/SW20*", f"{drive}:/SOLIDWORKS*", f"{drive}:/SolidWorks*"):
            try:
                cands += [Path(p) for p in glob.glob(pat)]
            except OSError:
                pass
    seen: set[str] = set()
    for c in cands:
        try:
            key = str(c.resolve()).lower()
        except OSError:
            key = str(c).lower()
        if key in seen:
            continue
        seen.add(key)
        try:
            if (c / "sldworks.exe").exists() or (c / "SLDWORKS.exe").exists():
                return c.resolve()
        except OSError:
            continue
    return None


def version_of(install: Path) -> str:
    """从安装目录名或 ../ 推断版本年份（C:\\SW2024\\SOLIDWORKS → 2024）。"""
    for p in [install, install.parent]:
        name = p.name
        digits = "".join(ch for ch in name if ch.isdigit())
        if len(digits) == 4 and digits.startswith("20"):
            return digits
    return "unknown"


def find_templates(install: Path | None) -> dict[str, str]:
    """找默认模板。返回 {'part':路径, 'assembly':路径, 'drawing':路径}，找不到的键不出现。

    优先级：用户 System Options 里配的 > ProgramData 下的 gb_* > 任意 *.prtdot。
    中文环境的默认零件模板一般是 gb_part.prtdot。
    """
    found: dict[str, str] = {}
    exts = {"part": "*.prtdot", "assembly": "*.asmdot", "drawing": "*.drwdot"}
    dirs: list[Path] = []
    for h in TEMPLATE_DIR_HINTS:
        hp = Path(h)
        if hp.is_dir():
            dirs.append(hp)
            dirs += [p for p in hp.iterdir() if p.is_dir()]
    for kind, pat in exts.items():
        # 模板可能直接在 <dir>/，也可能在 <dir>/templates/ —— 用 rglob 一次覆盖
        for d in dirs:
            try:
                hits = sorted(p for p in d.rglob(pat) if p.is_file())
            except OSError:
                continue
            if not hits:
                continue
            # 优先 templates 子目录里的、优先 gb_ 开头的
            hits.sort(key=lambda p: (0 if "template" in str(p.parent).lower() else 1,
                                     0 if p.name.lower().startswith("gb_") else 1,
                                     len(str(p))))
            found[kind] = str(hits[0])
            break
    return found


def user_pref_path() -> Path:
    """SolidWorks 用户设置目录（含默认模板路径的 config）。"""
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "SOLIDWORKS"


def pywin32_available() -> bool:
    return importlib.util.find_spec("win32com") is not None and \
        importlib.util.find_spec("pythoncom") is not None


def python_bits() -> int:
    return struct.calcsize("P") * 8


def sw_bits(install: Path | None) -> int | None:
    """读 sldworks.exe 的 PE 头判断 32/64 位。"""
    if not install:
        return None
    for exe in (install / "sldworks.exe", install / "SLDWORKS.exe"):
        if not exe.exists():
            continue
        try:
            with open(exe, "rb") as f:
                f.seek(0x3C)
                pe_off = struct.unpack("<I", f.read(4))[0]
                f.seek(pe_off + 4)
                machine = struct.unpack("<H", f.read(2))[0]
            return 64 if machine == 0x8664 else 32
        except (OSError, struct.error):
            continue
    return None


def api_help_available(install: Path | None) -> bool:
    if not install:
        return False
    return (install / "api" / "HelpViewer" / "sldworksapi").is_dir()


def report(install: Path | None = None) -> dict:
    root = install or find_install()
    bits = sw_bits(root)
    return {
        "os": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
        "python_exe": sys.executable,
        "python_bits": python_bits(),
        "installed": root is not None,
        "install_dir": str(root) if root else None,
        "version": version_of(root) if root else None,
        "sw_bits": bits,
        "bits_match": (bits == python_bits()) if bits else None,
        "pywin32": pywin32_available(),
        "templates": find_templates(root),
        "api_help": api_help_available(root),
        "user_pref_dir": str(user_pref_path()),
    }


def problems(rep: dict) -> list[str]:
    """把体检结果翻成「人能直接照做」的待办清单。"""
    out: list[str] = []
    if not rep["installed"]:
        out.append("找不到 SolidWorks 安装目录。设环境变量 SW_ROOT=<安装目录>，"
                   "例如 SW_ROOT=C:\\SW2024\\SOLIDWORKS")
    if not rep["pywin32"]:
        out.append(f"当前 Python 没装 pywin32（{rep['python_exe']} 里 import win32com 失败）。"
                   "装法：<该python> -m pip install pywin32 -i "
                   "https://pypi.tuna.tsinghua.edu.cn/simple")
    if rep.get("bits_match") is False:
        out.append(f"位数不匹配：SolidWorks 是 {rep['sw_bits']} 位，Python 是 "
                   f"{rep['python_bits']} 位 —— COM 连不上，必须换成同位数。")
    if rep["installed"] and not rep["templates"].get("part"):
        out.append("没找到零件模板 (*.prtdot)。SolidWorks 里 工具 > 选项 > 系统选项 > "
                   "默认模板 里指定一个，否则 NewDocument 会弹框阻塞自动化。")
    if rep["installed"] and not rep["api_help"]:
        out.append("这个安装没带 api/HelpViewer，无法用 sw_docbuild.py 生成接口表。")
    return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="SolidWorks 环境体检")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()
    rep = report()
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        for k, v in rep.items():
            print(f"{k:16} {v}")
    probs = problems(rep)
    if probs:
        print("\n待处理：")
        for p in probs:
            print("  -", p)
    else:
        print("\n环境 OK")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------- 产出目录

DEFAULT_OUT = "D:/out"


def out_dir() -> Path:
    """计划里 save_as / 截图 的默认落盘目录。

    优先级：环境变量 YOYO_SW_OUT > DEFAULT_OUT（不存在该盘时）> <当前工作目录>/out。
    这样换机器不用改代码，也不会因为 D: 盘不存在而直接报错。
    """
    var = os.environ.get("YOYO_SW_OUT")
    if var:
        return Path(var)
    d = Path(DEFAULT_OUT)
    if not Path(d.drive + "/").exists():
        return Path.cwd() / "out"
    return d
