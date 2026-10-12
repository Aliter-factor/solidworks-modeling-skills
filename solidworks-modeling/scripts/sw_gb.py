# -*- coding: utf-8 -*-
"""GB 标准件尺寸表 —— 读 `assets/gb_standard.json` 的**纯函数**入口。

★ 为什么单独一个模块：
  1. **不 import win32com** —— 这样没有 SolidWorks 的机器也能跑（离线自检、
     文档生成、别人拿这个包算尺寸都行）。和 `sw_naming.py` 是同一个套路。
  2. 让「标准件尺寸」只有一个权威来源。以前是 40-standard-parts.md 里的 Markdown 表 +
     代码里的常量各存一份，改一处忘一处（M8 的对边宽就出现过 13 / 14 两种、
     六角螺栓 dw 出现过 11.5 / 11.6 两种）。
  3. Agent 要查数据时**不用上网搜** —— 搜到的是二手表，公差和对边宽经常串行。
     本文件已经把权威值烘焙进来了（见 JSON 的 `_sources`）。

用法（Python）：
    import sw_gb
    sw_gb.pitch("M8")                       # 1.25
    sw_gb.tap_drill("M8")                   # 6.8
    sw_gb.row("hex_bolt", "M10")            # {'spec':'M10','d':10,'s':16,'k':6.4,...}
    sw_gb.search("垫圈")                     # 找到对应族
    sw_gb.bearing("6204")                   # {'d':20,'D':47,'B':14,'r':1.0}
    sw_gb.fit(20, "H7", "k6")               # 配合极限尺寸
    sw_gb.nearest_length("bolt", 63)        # 65（螺栓长度必须落标准系列）
    sw_gb.taper_pin_big_end(4, 18)          # 4.36（圆锥销大端 D = d + L/50）
用法（命令行）：
    python sw_gb.py --list
    python sw_gb.py --family hex_bolt
    python sw_gb.py --row hex_bolt M8
    python sw_gb.py --pitch M8
    python sw_gb.py --search 挡圈
    python sw_gb.py --bearing 6204
    python sw_gb.py --fit 20 H7 k6
    python sw_gb.py --module 2
    python sw_gb.py --length bolt 63
    python sw_gb.py --taper-pin 4 18
    python sw_gb.py --template slotted_pan_screw
    python sw_gb.py --cheatsheet            # ★ 一次打全表（给 Agent 用）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ASSET = Path(__file__).resolve().parent.parent / "assets" / "gb_standard.json"
_CACHE: dict | None = None


def asset_path() -> Path:
    return _ASSET


def load(reload: bool = False) -> dict:
    """读整张表（带缓存）。"""
    global _CACHE
    if _CACHE is None or reload:
        with open(_ASSET, encoding="utf-8") as f:
            _CACHE = json.load(f)
    return _CACHE


# ------------------------------------------------------------------ 螺纹

def pitch(spec: str, fine: float | None = None):
    """螺距。默认粗牙；给了 `fine` 就在细牙里核对（不在表里就返回 None）。"""
    t = load()["thread"]
    key = _norm_spec(spec)
    if fine is None:
        return t["pitch_coarse"].get(key)
    opts = t["pitch_fine"].get(key)
    if opts and float(fine) not in [float(v) for v in opts]:
        return None
    return float(fine)


def pitch_coarse(spec: str):
    return pitch(spec)


def pitch_fine_options(spec: str) -> list:
    return list(load()["thread"]["pitch_fine"].get(_norm_spec(spec), []))


def tap_drill(spec: str):
    """攻丝底孔直径（粗牙）。"""
    return load()["thread"]["tap_drill"].get(_norm_spec(spec))


def thread_profile(kind: str = "external") -> str:
    """real_thread 的 standard 用哪个 .SLDLFP。"""
    p = load()["thread"]["profile_file"]
    k = str(kind).lower()
    if k in ("external", "die", "外", "外螺纹", "metric_die"):
        return p["external"]
    if k in ("internal", "tap", "内", "内螺纹", "metric_tap"):
        return p["internal"]
    raise KeyError(f"未知螺纹方向 {kind!r}（external / internal）")


def clearance_hole(spec: str, series: str = "medium"):
    """螺栓通孔直径（GB/T 5277）。series: fine / medium / coarse。"""
    fam = load()["thread"]["clearance_hole"]
    cols = fam["columns"]
    if series not in cols:
        raise KeyError(f"series 只认 {cols[1:]}，收到 {series!r}")
    idx = cols.index(series)
    key = _norm_spec(spec)
    for r in fam["rows"]:
        if _norm_spec(str(r[0])) == key:
            return float(r[idx])
    return None


# ------------------------------------------------------------------ 族

def families() -> list:
    return list(load()["families"].keys())


def family(name: str) -> dict:
    fams = load()["families"]
    key = str(name).strip()
    if key in fams:
        return fams[key]
    low = key.lower()
    for k, v in fams.items():
        if k.lower() == low or str(v.get("cn", "")) == key or str(v.get("gb", "")) == key:
            return v
    raise KeyError(f"没有标准件族 {name!r}。可用：{', '.join(fams)}")


def gb_number(name: str) -> str:
    return family(name).get("gb", "")


def specs(name: str) -> list:
    """该族的所有规格（每行第一列）。"""
    return [str(r[0]) for r in family(name).get("rows", [])]


def _row_by_spec(fam: dict, spec) -> dict | None:
    cols = fam.get("columns") or []
    want = _norm_spec(str(spec))
    for r in fam.get("rows", []):
        if _norm_spec(str(r[0])) == want:
            return {c: v for c, v in zip(cols, r)}
    # 数字规格（销/轴承/挡圈）允许 '20' / '20.0' 互通
    try:
        wf = float(str(spec))
    except (TypeError, ValueError):
        return None
    for r in fam.get("rows", []):
        try:
            if abs(float(r[0]) - wf) < 1e-9:
                return {c: v for c, v in zip(cols, r)}
        except (TypeError, ValueError):
            continue
    return None


def row(name: str, spec: str) -> dict:
    """规格 → {列名: 值}。数字规格允许 20 / 20.0 互通。"""
    fam = family(name)
    got = _row_by_spec(fam, spec)
    if got is None:
        raise KeyError(f"{fam.get('cn', name)} 里没有规格 {spec!r}。可用："
                       f"{', '.join(specs(name))}")
    return got


def column(name: str, spec: str, col: str):
    return row(name, spec).get(col)


def range_of(name: str, spec: str, col: str) -> tuple:
    """把 '17-22' 这种区间列解析成 (17.0, 22.0)。"""
    v = column(name, spec, col)
    if isinstance(v, str) and "-" in v:
        a, b = v.split("-", 1)
        return (float(a), float(b))
    return (float(v), float(v))


def pick(family_name: str, value, col: str = "shaft_range") -> dict:
    """按某个区间列反查规格 —— 例：轴径 25 → 该用多大的平键。

    ★ 区间按**闭区间**判（lo ≤ v ≤ hi）。国标原文写的是「>17~22」，
      但工程上 Ø17 的轴就该配 6×6 键 —— 用开区间会让 Ø17 落空，
      所以这里取实用的闭区间。边界值（22）归**前一段**（6×6），与国标一致。"""
    fam = family(family_name)
    v = float(value)
    for r in fam.get("rows", []):
        d = {c: x for c, x in zip(fam.get("columns") or [], r)}
        try:
            lo, hi = range_of(family_name, str(r[0]), col)
        except Exception:                                        # noqa: BLE001
            continue
        if lo <= v <= hi:
            return d
    return {}


def key_lengths(b: float | int) -> list:
    """该键宽 b 可用的标准键长（GB/T 1096 长度系列）。"""
    return list(family("parallel_key").get("key_length_series", []))


# ------------------------------------------------------------------ 销 / 轴承

def taper_pin(d) -> dict:
    """GB/T 117 圆锥销：按小端直径查行。"""
    return row("taper_pin", d)


def taper_pin_big_end(d, length) -> float:
    """圆锥销大端直径 D = d + L/50（1:50 锥度）。"""
    return round(float(d) + float(length) / 50.0, 4)


def bearing(spec: str) -> dict:
    """深沟球轴承（GB/T 276）。等价于 row('deep_groove_bearing', spec)。"""
    return row("deep_groove_bearing", spec)


def bearings_by_bore(d) -> list:
    """按内径找所有系列可选型号（选型用）。"""
    fam = family("deep_groove_bearing")
    cols = fam["columns"]
    want = float(d)
    out = []
    for r in fam["rows"]:
        rec = {c: v for c, v in zip(cols, r)}
        if abs(float(rec["d"]) - want) < 1e-9:
            out.append(rec)
    return out


# ------------------------------------------------------------------ 长度系列

def length_series(kind: str = "bolt") -> list:
    """标准长度系列。kind: bolt（螺栓）/ screw（螺钉）。"""
    t = load()["length_series"]
    k = str(kind).lower()
    if k in ("bolt", "螺栓", "screw_bolt"):
        return list(t["bolt"])
    if k in ("screw", "螺钉", "screw_short"):
        return list(t["screw"])
    raise KeyError(f"kind 只认 bolt / screw，收到 {kind!r}")


def nearest_length(kind: str, target, direction: str = "up"):
    """给目标长度取标准值。

    direction: `up`=不小于（默认，长一点能装）/ `down`=不大于 / `near`=最近。
    取不到（超出系列范围）返回 None —— **别硬凑**。
    """
    seq = length_series(kind)
    t = float(target)
    if direction == "up":
        cand = [v for v in seq if v >= t - 1e-9]
        return cand[0] if cand else None
    if direction == "down":
        cand = [v for v in seq if v <= t + 1e-9]
        return cand[-1] if cand else None
    if direction == "near":
        return min(seq, key=lambda v: (abs(v - t), v))
    raise KeyError(f"direction 只认 up / down / near，收到 {direction!r}")


def is_standard_length(kind: str, value) -> bool:
    return any(abs(float(value) - v) < 1e-9 for v in length_series(kind))


# ------------------------------------------------------------------ 齿轮模数

def module_series() -> dict:
    return dict(load()["module_series"])


def is_standard_module(m) -> bool:
    s = load()["module_series"]
    return any(abs(float(m) - v) < 1e-9 for v in s["first"] + s["second"])


def module_advice(m) -> str:
    """给个模数，说一句它合不合规。"""
    s = load()["module_series"]
    v = float(m)
    if any(abs(v - x) < 1e-9 for x in s["first"]):
        return "第一系列（优先选用）✔"
    if any(abs(v - x) < 1e-9 for x in s.get("avoid", [])):
        return "第二系列但**标准里带括号**，尽量不用 ✘"
    if any(abs(v - x) < 1e-9 for x in s["second"]):
        return "第二系列（按需选用）△"
    return "**不在 GB/T 1357 系列里** —— 落刀前请复核 ✘"


# ------------------------------------------------------------------ 配合

def step_index(nominal) -> int:
    """按「大于 lo，至 hi」找尺寸分段下标。"""
    steps = load()["fits"]["steps"]
    v = float(nominal)
    for i, (lo, hi) in enumerate(steps):
        if lo < v <= hi or (i == 0 and v <= hi):
            return i
    raise KeyError(f"公称尺寸 {nominal} 超出 3~400 mm 的分段范围")


def IT(grade: str, nominal) -> int:
    """标准公差值（μm）。grade 如 'IT7'。"""
    t = load()["fits"]["IT_um"]
    g = str(grade).upper()
    if g not in t:
        raise KeyError(f"没有 {grade}（表里有 {', '.join(t)}）")
    return int(t[g][step_index(nominal)])


def fit(nominal, hole: str = "H7", shaft: str = "k6") -> dict:
    """算一组配合的极限尺寸。

    返回 {'nominal','step','hole':{es,ei,limits},'shaft':{...},
          'clearance_um':(min,max), 'type': '间隙'/'过渡'/'过盈'}
    偏差单位 μm，limits 单位 mm。
    """
    i = step_index(nominal)
    v = float(nominal)
    fits = load()["fits"]

    h = str(hole).upper()
    if not h.startswith("H"):
        raise KeyError("目前只支持基孔制 H 孔（H7/H8/H9…）")
    h_es = IT("IT" + h[1:], v)
    h_ei = 0

    s = str(shaft).lower()
    tab = fits["shaft_es_ei_um"]
    if s not in tab:
        raise KeyError(f"没有轴公差带 {shaft}（表里有 {', '.join(k for k in tab if not k.startswith('_'))}）")
    s_es, s_ei = tab[s][i]

    lo_fit = h_ei - s_es          # 最小间隙（可为负 = 最大过盈）
    hi_fit = h_es - s_ei          # 最大间隙
    if lo_fit >= 0:
        kind = "间隙"
    elif hi_fit <= 0:
        kind = "过盈"
    else:
        kind = "过渡"
    return {
        "nominal": v,
        "step": fits["steps"][i],
        "hole": {"class": h, "es_um": h_es, "ei_um": h_ei,
                 "limits_mm": [round(v + h_ei / 1000.0, 4), round(v + h_es / 1000.0, 4)]},
        "shaft": {"class": s, "es_um": s_es, "ei_um": s_ei,
                  "limits_mm": [round(v + s_ei / 1000.0, 4), round(v + s_es / 1000.0, 4)]},
        "clearance_um": [lo_fit, hi_fit],
        "type": kind,
        "note": f"{h}/{s} 是{'常用' if (h + '/' + s) in fits['preferred_hole_basis'] else ''}配合",
    }


def preferred_fits() -> dict:
    return {k: v for k, v in load()["fits"]["preferred_hole_basis"].items()
            if not k.startswith("_")}


# ------------------------------------------------------------------ 检索

def search(text: str) -> list:
    """按中文名/英文名/GB 号模糊找族，回 [(key, cn, gb, confidence), ...]。"""
    t = str(text).strip().lower()
    out = []
    for k, v in load()["families"].items():
        hay = " ".join([k, str(v.get("cn", "")), str(v.get("en", "")),
                        str(v.get("gb", "")), str(v.get("gb_full_thread", ""))]).lower()
        if t in hay:
            out.append((k, v.get("cn", ""), v.get("gb", ""), v.get("confidence", "")))
    # 顺带搜一下顶层节（fits / module_series / length_series）
    for sec in ("fits", "module_series", "length_series"):
        v = load().get(sec) or {}
        hay = " ".join([sec, str(v.get("cn", "")), str(v.get("gb", ""))]).lower()
        if t in hay:
            out.append((sec, v.get("cn", ""), v.get("gb", ""), v.get("confidence", "")))
    return out


def template(name: str) -> dict:
    return load().get("templates", {}).get(str(name), {})


def templates() -> list:
    return list(load().get("templates", {}).keys())


def toolbox_enabled() -> bool:
    return bool(load().get("toolbox", {}).get("enabled", False))


def _norm_spec(s: str) -> str:
    """规格归一：去空格、'M 8'→'M8'、'8X7'→'8x7'。大小写敏感度交给调用方。"""
    t = str(s).strip().replace(" ", "")
    return t


# ------------------------------------------------------------------ 速查表

def cheatsheet(section: str | None = None) -> str:
    """把整张表（或某一节）打成一段 Markdown —— **给 Agent 一次读全**，省得多轮查询。

    命令行：`python sw_gb.py --cheatsheet [hex_bolt]`
    """
    d = load()
    sec = (section or "").strip().lower()
    lines: list[str] = []
    src = d.get("_sources", {})

    def fam_md(key: str, v: dict) -> None:
        lines.append(f"### {v.get('cn', key)}（{v.get('gb', '')}）"
                     f"  `{key}`  [{v.get('confidence', '?')}]")
        if v.get("en"):
            lines.append(f"_{v['en']}_")
        for note_key in ("_cols", "_note", "_note2", "_revision_note"):
            if v.get(note_key):
                lines.append(f"- {v[note_key]}")
        cols = v.get("columns") or []
        if cols and v.get("rows"):
            lines.append("")
            lines.append("| " + " | ".join(str(c) for c in cols) + " |")
            lines.append("|" + "---|" * len(cols))
            for r in v["rows"]:
                lines.append("| " + " | ".join(str(x) for x in r) + " |")
        for extra in ("key_length_series", "length_series"):
            if v.get(extra):
                lines.append(f"- {extra}: {', '.join(str(x) for x in v[extra])}")
        if v.get("_formula"):
            lines.append(f"- {v['_formula']}")
        lines.append("")

    if not sec or sec in ("thread", "螺纹"):
        t = d["thread"]
        lines.append(f"## 螺纹（{t['gb']}）")
        # ★ 一律过滤 `_` 开头的注记键 —— 否则 `_note` 会被当成一个规格打进值列表
        #   （真机出现过 `M1.6=1.25, ..., _note=攻丝底孔（粗牙）...`）。
        def _kv(m: dict) -> str:
            return ", ".join(f"{k}={v}" for k, v in m.items() if not str(k).startswith("_"))
        lines.append("- 粗牙螺距: " + _kv(t["pitch_coarse"]))
        lines.append("- 细牙螺距: " + "; ".join(
            f"{k}: {'/'.join(str(x) for x in v)}"
            for k, v in t["pitch_fine"].items() if not str(k).startswith("_")))
        lines.append("- 攻丝底孔: " + _kv(t["tap_drill"]))
        ch = t["clearance_hole"]
        lines.append(f"- 通孔：{ch['_note']}")
        lines.append("")
        lines.append("| " + " | ".join(ch["columns"]) + " |")
        lines.append("|" + "---|" * len(ch["columns"]))
        for r in ch["rows"]:
            lines.append("| " + " | ".join(str(x) for x in r) + " |")
        lines.append("")

    if not sec or sec in ("length_series", "长度"):
        t = d["length_series"]
        lines.append(f"## 长度系列（{t['gb']} {t['cn']}）")
        lines.append(f"- 螺栓 bolt: {', '.join(str(x) for x in t['bolt'])}")
        lines.append(f"- 螺钉 screw: {', '.join(str(x) for x in t['screw'])}")
        lines.append(f"- {t.get('_note', '')}")
        lines.append("")

    if not sec or sec in ("module_series", "模数"):
        t = d["module_series"]
        lines.append(f"## 齿轮模数（{t['gb']} {t['cn']}）")
        lines.append(f"- 第一系列: {', '.join(str(x) for x in t['first'])}")
        lines.append(f"- 第二系列: {', '.join(str(x) for x in t['second'])}")
        lines.append(f"- 尽量不用: {', '.join(str(x) for x in t.get('avoid', []))}")
        lines.append(f"- 压力角 {t['pressure_angle_deg']}° / ha*={t['ha_star']} / c*={t['c_star']}")
        lines.append("")

    if not sec or sec in ("fits", "配合"):
        t = d["fits"]
        lines.append(f"## {t['cn']}（{t['gb']}）—— 偏差单位 **μm**")
        steps = t["steps"]
        lines.append(f"- 尺寸分段（大于 lo 至 hi）: {', '.join(f'{a}~{b}' for a, b in steps)}")
        for g, arr in t["IT_um"].items():
            lines.append(f"- {g}: {arr}")
        for g, arr in t["shaft_es_ei_um"].items():
            if g.startswith("_"):
                continue
            lines.append(f"- {g} (es/ei): {[tuple(x) for x in arr]}")
        lines.append(f"- H 孔: EI=0, ES=+IT（{t['hole_H_um']['H7']}）")
        for k, v in t["preferred_hole_basis"].items():
            if not k.startswith("_"):
                lines.append(f"  - {k}: {v}")
        lines.append("")

    if not sec:
        lines.append("## 标准件族（一览）")
        for k, v in d["families"].items():
            lines.append(f"- `{k}` {v.get('cn','')} {v.get('gb','')} "
                         f"[{v.get('confidence','?')}] 规格: {len(v.get('rows', []))} 条")
        lines.append("")
        lines.append("## 参照件模板")
        for k, v in d.get("templates", {}).items():
            lines.append(f"- `{k}` {v.get('cn','')} → {v.get('reference_part','')}")
        lines.append("")
        # ★ 「一次读全」不能只给族名 —— 真机踩到：快照里没有 6204 这种**规格值**，
        #   等于让 Agent 还得再查一遍。所以全表模式把每族的整张表都铺出来。
        lines.append("## 各族明细")
        lines.append("")
        for k, v in d["families"].items():
            fam_md(k, v)
    elif sec in d["families"]:
        fam_md(sec, d["families"][sec])
    else:
        for k, v in d["families"].items():
            if sec in k.lower() or sec in str(v.get("cn", "")).lower() \
                    or sec in str(v.get("gb", "")).lower():
                fam_md(k, v)

    lines.append(f"> 数据来源：{json.dumps(src, ensure_ascii=False)}")
    lines.append(f"> 版本 revision={d.get('revision','?')} updated={d.get('updated','?')}"
                 " —— **离线权威表，不要联网搜**。")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI

def _main(argv: list) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="GB 标准件尺寸表查询（离线权威表）")
    ap.add_argument("--list", action="store_true", help="列出所有族")
    ap.add_argument("--family", help="打印某个族的整张表")
    ap.add_argument("--row", nargs=2, metavar=("FAMILY", "SPEC"), help="打印一行")
    ap.add_argument("--pitch", help="查粗牙螺距，如 M8")
    ap.add_argument("--tap-drill", dest="tap", help="查攻丝底孔，如 M8")
    ap.add_argument("--hole", nargs="+", metavar=("SPEC", "SERIES"),
                    help="查螺栓通孔，如 --hole M8 medium")
    ap.add_argument("--search", help="按名字/GB 号模糊找族")
    ap.add_argument("--pick", nargs=3, metavar=("FAMILY", "VALUE", "COL"),
                    help="按区间列反查规格，如 --pick parallel_key 25 shaft_range")
    ap.add_argument("--bearing", help="查深沟球轴承，如 6204")
    ap.add_argument("--bearings-for", dest="bearings_for", help="按内径列出可选轴承，如 20")
    ap.add_argument("--taper-pin", dest="taper_pin", nargs="+", metavar="D [L]",
                    help="查圆锥销；给小端 d（和长度 L 时顺带算大端）")
    ap.add_argument("--length", nargs=2, metavar=("KIND", "TARGET"),
                    help="取标准长度，如 --length bolt 63")
    ap.add_argument("--module", help="判断模数是否标准，如 2")
    ap.add_argument("--fit", nargs="+", metavar="NOMINAL [HOLE] [SHAFT]",
                    help="算配合，如 --fit 20 H7 k6")
    ap.add_argument("--template", help="打印某个建模模板")
    ap.add_argument("--templates", action="store_true", help="列出模板")
    ap.add_argument("--cheatsheet", nargs="?", const="", default=None,
                    help="★ 一次打全表（或某一节）成 Markdown")
    ap.add_argument("--write-md", dest="write_md", metavar="PATH",
                    help="★ 把全表写成 Markdown 文件（随包发的速查表就这样生成）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    a = ap.parse_args(argv)

    def dump(x):
        if a.json:
            print(json.dumps(x, ensure_ascii=False, indent=1, default=str))
        else:
            if isinstance(x, list):
                for i in x:
                    print("  " + (json.dumps(i, ensure_ascii=False) if not isinstance(i, str) else i))
            elif isinstance(x, dict):
                for k, v in x.items():
                    print(f"  {k:<14} {json.dumps(v, ensure_ascii=False)}")
            else:
                print(x)
        return 0

    if a.write_md:
        import pathlib
        body = cheatsheet(None)
        head = ("<!-- 本文件由 `python scripts/sw_gb.py --write-md` 从 assets/gb_standard.json "
                "自动生成，**不要手改**。改数据请改 JSON 再重新生成。 -->\n"
                "# 41 · 国标速查表（离线全表）\n\n"
                "> ★ 这是 `assets/gb_standard.json` 的**只读快照**，给\"手边没有 Python\"或"
                "想一次看全的场合用。\n"
                "> 需要**精确取值**时仍以 `sw_gb.py` / op `lookup_standard` 为准（它们是同一份数据）。\n"
                "> **不要联网搜国标尺寸** —— 网上是各家互相抄的二手表，公差/对边宽/头高经常串行。\n\n")
        p = pathlib.Path(a.write_md)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(head + body + "\n", encoding="utf-8")
        print(f"已写出 {p}（{len(body.splitlines()) + len(head.splitlines())} 行）")
        return 0
    if a.cheatsheet is not None:
        print(cheatsheet(a.cheatsheet or None))
        return 0
    if a.list:
        rows = [(k, v.get("cn", ""), v.get("gb", ""), v.get("confidence", ""))
                for k, v in load()["families"].items()]
        if a.json:
            return dump([{"key": k, "cn": c, "gb": g, "confidence": f} for k, c, g, f in rows])
        print(f"共 {len(rows)} 个族：")
        for k, c, g, f in rows:
            print(f"  {k:<24} {c:<18} {g:<16} [{f}]")
        return 0
    if a.templates:
        return dump(templates())
    if a.family:
        return dump(family(a.family))
    if a.row:
        return dump(row(a.row[0], a.row[1]))
    if a.pitch:
        return dump({"spec": a.pitch, "pitch_coarse": pitch(a.pitch),
                     "pitch_fine": pitch_fine_options(a.pitch)})
    if a.tap:
        return dump({"spec": a.tap, "tap_drill": tap_drill(a.tap)})
    if a.hole:
        return dump({"spec": a.hole[0],
                     "series": a.hole[1] if len(a.hole) > 1 else "medium",
                     "hole": clearance_hole(a.hole[0],
                                            a.hole[1] if len(a.hole) > 1 else "medium")})
    if a.search:
        return dump(search(a.search))
    if a.pick:
        return dump(pick(a.pick[0], float(a.pick[1]), a.pick[2]))
    if a.bearing:
        return dump(bearing(a.bearing))
    if a.bearings_for:
        return dump(bearings_by_bore(a.bearings_for))
    if a.taper_pin:
        d = a.taper_pin[0]
        out = taper_pin(d)
        if len(a.taper_pin) > 1:
            out = dict(out)
            out["big_end_D"] = taper_pin_big_end(d, a.taper_pin[1])
        return dump(out)
    if a.length:
        kind, tgt = a.length
        return dump({"kind": kind, "target": float(tgt),
                     "up": nearest_length(kind, tgt, "up"),
                     "down": nearest_length(kind, tgt, "down"),
                     "near": nearest_length(kind, tgt, "near"),
                     "exact": is_standard_length(kind, tgt)})
    if a.module:
        m = float(a.module)
        return dump({"module": m, "standard": is_standard_module(m),
                     "advice": module_advice(m)})
    if a.fit:
        n = a.fit[0]
        h = a.fit[1] if len(a.fit) > 1 else "H7"
        s = a.fit[2] if len(a.fit) > 2 else "k6"
        return dump(fit(n, h, s))
    if a.template:
        return dump(template(a.template))
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
