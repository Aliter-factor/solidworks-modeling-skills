"""sw_practice.py — SolidWorks 官方练习题库（Practice Problems）索引器。

用途
====
把 Dassault 官方发布的 *Practice Problems*（教育版闯关题库，18 关 / 344 题）
扫成一个**机读索引**，再按题号吐出一份「建模任务单」。

为什么值得做
------------
题库里每份 PDF 都是一道**自带判卷标准**的题：
  · 图纸视图（带尺寸）—— 这是"输入"
  · `Volume: N mm3`    —— 这是**天然的正确答案**，正是 check_mass 需要的靶值
  · Suggested Features / HINT —— 这是参考解法路径
⇒ 它天然就是「读图 → 出计划 → 建模 → 按体积判对错」这条链路的**监督数据集**。
   本机 38 个减速器参照件是"我们自己项目的题"，题库则是"通用题海"。

命令
----
  # 扫题库 → 索引（默认写到 assets/practice/index.json）
  python scripts/sw_practice.py --scan <题库根目录>
  python scripts/sw_practice.py --scan <root> --out assets/practice/index.json

  # 看统计（按关 / 按认证 / 有/无体积判据）
  python scripts/sw_practice.py --stats

  # 挑一批题（可按关、难度、认证筛），列出来
  python scripts/sw_practice.py --list --level 6
  python scripts/sw_practice.py --list --cert CSWA --difficulty Easy --limit 20

  # 出任务单：给一道题生成 yoyo.swplan/1 骨架 + 期望体积 + 自检项
  python scripts/sw_practice.py --task 6.1 --out-dir D:/out/practice

题库路径优先级：命令行 > 环境变量 `YOYO_PRACTICE_ROOT` > 索引里记的 default_root。

设计约束
--------
· **纯离线**：只读 PDF / zip，不碰 SolidWorks，不联网。
· **不猜**：PDF 里读不到就记 None，并在 `coverage` 里如实报缺口 —— 不许用"估计值"填。
· 体积数值**原样保留字符串 + 解析值**两份（`volume_text` / `volume_mm3`），
  带千分位或小数点的写法不丢信息。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path

SCHEMA = "yoyo.practice/1"
DEFAULT_ROOT_ENV = "YOYO_PRACTICE_ROOT"
DEFAULT_INDEX = Path(__file__).resolve().parent.parent / "assets" / "practice" / "index.json"

LEVEL_TITLES = {
    1: "Basic Sketch and Extrusion",
    2: "Sketch Tools / End Conditions",
    3: "Global Variables / Sketch Patterns",
    4: "Extrude Cut / Fillet / Chamfer",
    5: "Reference Geometry",
    6: "Revolve Boss / Cut",
    7: "Feature Patterns",
    8: "Sweep Boss / Cut",
    9: "Assemblies / Mates",
    10: "CSWA Exam Level",
    11: "Hole Wizard",
    12: "Draft",
    13: "Shell",
    14: "Rib",
    15: "Configurations",
    16: "Global Variables / Equations",
    17: "Assembly Tools",
    18: "CSWP Exam Level",
}

# ---- PDF 文本解析 ----------------------------------------------------------

RE_PROBLEM = re.compile(r"Practice\s+Problem\s+(\d+)\s*[.]\s*([0-9]+[A-Za-z]?)", re.I)
RE_LEVEL_IN_TITLE = re.compile(r"\(Level\s*(\d+)\)", re.I)
# ★ 体积的**单位不一定是 mm3**：1.4/1.5 是 IPS 英寸制（'Volume: 0.146 in3'），
#   7.5 甚至印成 'cm3'（按题面量级看应是 mm3 的笔误）⇒ 必须**连单位一起读**，
#   原样留存 + 能换算的换算成 mm3；不确定的**标 suspect**，不许闷头当成 mm3。
RE_VOLUME = re.compile(
    r"Volume\s*:?\s*([0-9][0-9,.\s]*?)\s*"
    r"(mm\s*[³3]|in\s*[³3]|inch\s*[³3]|cm\s*[³3])", re.I)

# 单位换算到 mm3
UNIT_TO_MM3 = {
    "mm3": 1.0,
    "in3": 25.4 ** 3,     # 16387.064
    "cm3": 1000.0,
}


def _norm_unit(u: str) -> str:
    t = u.lower().replace("³", "3").replace(" ", "")
    if t.startswith("inch"):
        t = "in" + t[4:]
    return t


def _no_volume_kind(text: str) -> str:
    """没有机读靶值时，判定它属于哪一类（如实记因，不糊成 null）。"""
    if re.search(r"Volume\s*:?\s*See\s+Table", text, re.I):
        return "see_table"          # 多配置：各配置的体积在题面表格里
    if re.search(r"Volume\s*:?\s*See\s+Drawing", text, re.I):
        return "see_drawing"        # 体积标在图纸上
    if re.search(r"Volume\s*\d\s*[.:]", text, re.I) or \
       re.search(r"\bvolumes?\b\s*[.:]", text, re.I):
        return "multi_config"       # 'Volume 1. … Volume 2. …' 逐配置列
    if re.search(r"\bassembly\b", text, re.I):
        return "assembly"           # 装配题，本身没有单一零件体积
    return "none"
RE_UNITS = re.compile(r"Units\s*:\s*([A-Za-z]+)")
RE_DIFF = re.compile(r"Difficulty\s+Level\s*:\s*([A-Za-z ]+)", re.I)
RE_CERT = re.compile(r"Certification\s*:\s*([A-Za-z0-9+ ]+)", re.I)
RE_MINUTES = re.compile(r"Duration\s*(?:\(min\))?\s*:\s*(\d+)", re.I)
RE_HINT = re.compile(r"HINT\s*:\s*(.+)", re.I | re.S)


def _num(s: str):
    """'72,593' / '728.3' / '1 234' → float；解析不了回 None。"""
    if s is None:
        return None
    t = s.strip().replace(",", "").replace(" ", "")
    m = re.fullmatch(r"\d+(?:[.]\d+)?", t)
    return float(t) if m else None


def parse_pdf_text(text: str) -> dict:
    """从 PDF 抽出的文本里解出题目元数据。读不到的一律 None，不猜。"""
    out: dict = {}

    m = RE_PROBLEM.search(text)
    if m:
        out["level"] = int(m.group(1))
        out["problem"] = m.group(2)
        out["id"] = f"{m.group(1)}.{m.group(2)}"

    # 标题行：'Basic Sketch and Extrusion (Level 1)'
    for line in text.splitlines():
        line = line.strip()
        if "(Level" in line and len(line) < 120:
            out["title"] = re.sub(r"\s*\(Level\s*\d+\)\s*$", "", line).strip()
            lm = RE_LEVEL_IN_TITLE.search(line)
            if lm:
                out.setdefault("level", int(lm.group(1)))
            break

    m = RE_UNITS.search(text)
    if m:
        out["units"] = m.group(1).upper()

    m = RE_DIFF.search(text)
    if m:
        out["difficulty"] = m.group(1).strip()

    m = RE_CERT.search(text)
    if m:
        out["certification"] = m.group(1).strip()

    m = RE_MINUTES.search(text)
    if m:
        out["minutes"] = int(m.group(1))

    m = RE_VOLUME.search(text)
    if m:
        val = _num(m.group(1))
        unit = _norm_unit(m.group(2))
        out["volume_text"] = re.sub(r"\s+", "", m.group(1)) + " " + m.group(2).replace(" ", "")
        out["volume_value"] = val            # 题面原值（按题面单位）
        out["volume_unit"] = unit            # mm3 / in3 / cm3
        out["volume_kind"] = "value"
        if val is not None and unit in UNIT_TO_MM3:
            out["volume_mm3"] = round(val * UNIT_TO_MM3[unit], 4)
        # 题面写 cm3 但数值量级像 mm3（>100000）⇒ 大概率笔误，标出来人工裁决
        if unit == "cm3" and val is not None and val > 100000:
            out["volume_suspect"] = ("题面单位 cm3 与数值量级不符（按 1000× 换算会得到超大件），"
                                     "疑为 mm3 笔误 —— 用前需人工确认")
    else:
        # ★ 没靶值也要说清**为什么**：别把它变成一个含糊的 null。
        #   'See Table' = 多配置题，体积在表里；'See Drawing' = 图上给；
        #   'Volume 1./2.' = 每个配置一条；装配题干脆没有。
        out["volume_kind"] = _no_volume_kind(text)

    # HINT（到下一个已知小节为止）
    m = RE_HINT.search(text)
    if m:
        hint = re.split(r"\n\s*(?:Volume|Units|Difficulty|Certification|Duration|Suggested)\b",
                        m.group(1))[0]
        hint = re.sub(r"\s+", " ", hint).strip(" •·\t\n")
        if hint:
            out["hint"] = hint

    # Design Intent 的 'o ' 条目。★ PDF 会把长条目**折行**（'…is indicated' / 'in red.'），
    #   折行不是新条目 —— 只有以 'o ' 开头的才算，其余并进上一条。
    di: list[str] = []
    grab = False
    for line in text.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("design intent"):
            grab = True
            continue
        if grab:
            if low.startswith(("suggested", "volume", "hint")):
                break
            if not s or s in ("•", "·"):
                continue
            if re.match(r"^[•\s]*o\s+", s):
                di.append(re.sub(r"^[•\s]*o\s+", "", s).strip())
            elif di:
                di[-1] = (di[-1] + " " + s).strip()
    if di:
        out["design_intent"] = di

    # Sketch / Features 两行
    for key, field in (("Sketch", "sketch_tools"), ("Features", "features")):
        m = re.search(rf"^\s*{key}\s*:\s*(.+)$", text, re.I | re.M)
        if m:
            items = [x.strip() for x in re.split(r"[,、]", m.group(1)) if x.strip()]
            if items:
                out[field] = items
    return out


# ---- 目录扫描 --------------------------------------------------------------


def scan(root: Path) -> dict:
    root = Path(root)
    if not root.is_dir():
        raise SystemExit(f"题库根目录不存在：{root}")

    levels: dict[int, dict] = {}
    problems: list[dict] = []

    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        m = re.match(r"Level0*(\d+)_?(.*)", d.name)
        lv = int(m.group(1)) if m else None
        lv_rec = {
            "level": lv,
            "dir": d.name,
            "title": LEVEL_TITLES.get(lv, (m.group(2) if m else d.name).replace("_", " / ")),
            "n_pdf": 0, "n_zip": 0, "n_other": 0,
        }
        for f in sorted(d.iterdir()):
            suf = f.suffix.lower()
            if suf == ".pdf":
                lv_rec["n_pdf"] += 1
            elif suf == ".zip":
                lv_rec["n_zip"] += 1
            else:
                lv_rec["n_other"] += 1
        levels.setdefault(lv if lv is not None else 0, lv_rec)

        for f in sorted(d.iterdir()):
            if f.suffix.lower() != ".pdf":
                continue
            rec = _scan_one_pdf(f, root)
            rec["level_dir"] = d.name
            if lv is not None:
                rec.setdefault("level", lv)
            problems.append(rec)

        # zip：可能是参照件 / START 文件，也可能夹着一份 PDF
        for f in sorted(d.iterdir()):
            if f.suffix.lower() != ".zip":
                continue
            for rec in _scan_zip(f, root, lv):
                rec["level_dir"] = d.name
                problems.append(rec)

    problems.sort(key=lambda r: (r.get("level") or 99,
                                  _sort_key(r.get("problem") or "")))

    n_vol = sum(1 for p in problems if p.get("volume_mm3") is not None)
    n_zip_only = sum(1 for p in problems if p.get("from_zip"))
    by_vol_unit: dict[str, int] = {}
    by_vol_kind: dict[str, int] = {}
    n_suspect = 0
    for p in problems:
        if p.get("volume_unit"):
            by_vol_unit[p["volume_unit"]] = by_vol_unit.get(p["volume_unit"], 0) + 1
        if p.get("volume_suspect"):
            n_suspect += 1
        k = p.get("volume_kind")
        if k:
            by_vol_kind[k] = by_vol_kind.get(k, 0) + 1
    return {
        "schema": SCHEMA,
        "generated_by": "scripts/sw_practice.py --scan",
        "root_env": DEFAULT_ROOT_ENV,
        "default_root": root.as_posix(),
        "note": ("官方 Practice Problems 索引。volume_mm3 = **判卷靶值**（来自 PDF 的 "
                 "'Volume: N <单位>'，已统一换算成 mm3）。原值/原单位分别留在 "
                 "volume_value / volume_unit。种子版不含解法，只用来自动判对错。"),
        "counts": {
            "n_problems": len(problems),
            "n_with_volume": n_vol,
            "n_without_volume": len(problems) - n_vol,
            "n_from_zip": n_zip_only,
            "n_levels": len(levels),
            "volume_units": dict(sorted(by_vol_unit.items())),
            "volume_kinds": dict(sorted(by_vol_kind.items())),
            "n_volume_suspect": n_suspect,
        },
        "levels": [levels[k] for k in sorted(levels)],
        "problems": problems,
    }


def _sort_key(prob: str):
    m = re.match(r"(\d+)([A-Za-z]?)", prob)
    if not m:
        return (9999, "")
    return (int(m.group(1)), m.group(2).upper())


def _scan_one_pdf(f: Path, root: Path) -> dict:
    rec = {"file": f.relative_to(root).as_posix(), "from_zip": False}
    try:
        text = _pdf_text(f)
    except Exception as e:                                        # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec
    rec.update(parse_pdf_text(text))

    # 文件名兜底：PDF 里没解出版号时用文件名
    if "id" not in rec:
        m = re.match(r"PracticeProblems_(\d+)_([0-9]+[A-Za-z]?)", f.stem)
        if m:
            rec["id"] = f"{m.group(1)}.{m.group(2)}"
            rec.setdefault("level", int(m.group(1)))
            rec.setdefault("problem", m.group(2))
            rec["id_from"] = "filename"
    if "id" in rec:
        rec.setdefault("id_from", "pdf")
    rec["n_pages"] = _pdf_pages(f)
    return rec


def _scan_zip(f: Path, root: Path, lv):
    """zip 里通常是参照件（装配题给零件）或 START 文件；偶尔夹着一份 PDF。"""
    recs: list[dict] = []
    try:
        with zipfile.ZipFile(f) as z:
            names = z.namelist()
    except Exception as e:                                        # noqa: BLE001
        return [{"file": f.relative_to(root).as_posix(), "from_zip": True,
                 "error": f"zip 打不开：{e}", "level": lv}]

    model = [n for n in names if Path(n).suffix.lower() in
             (".sldprt", ".sldasm", ".slddrw")]
    inner_pdf = [n for n in names if Path(n).suffix.lower() == ".pdf"]

    base = {
        "file": f.relative_to(root).as_posix(),
        "from_zip": True,
        "zip_models": sorted(model),
        "zip_all": sorted(names),
        "level": lv,
    }
    m = re.match(r"PracticeProblems_(\d+)_([0-9]+[A-Za-z]?)", f.stem)
    if m:
        base["id"] = f"{m.group(1)}.{m.group(2)}"
        base["problem"] = m.group(2)
        base["id_from"] = "filename"

    if inner_pdf:
        base["pdf_in_zip"] = sorted(inner_pdf)
        try:
            with zipfile.ZipFile(f) as z:
                text = z.read(inner_pdf[0]).decode("latin-1", "ignore")
            # PDF 是二进制，这里只用来兜底找 'Volume'；失败就算了
            vm = re.search(r"Volume\s*:?\s*([0-9][0-9,.]*)\s*(mm|in|cm)\s*[³3]", text)
            if vm:
                unit = _norm_unit(vm.group(2) + "3")
                val = _num(vm.group(1))
                base["volume_text"] = vm.group(0)
                base["volume_value"] = val
                base["volume_unit"] = unit
                if val is not None and unit in UNIT_TO_MM3:
                    base["volume_mm3"] = round(val * UNIT_TO_MM3[unit], 4)
                base["volume_from"] = "zip_pdf_raw"
        except Exception:                                          # noqa: BLE001
            pass
    recs.append(base)
    return recs


def _pdf_text(p: Path) -> str:
    import pymupdf                                                # 延迟导入
    with pymupdf.open(p) as d:
        return "\n".join(pg.get_text() for pg in d)


def _pdf_pages(p: Path) -> int:
    try:
        import pymupdf
        with pymupdf.open(p) as d:
            return d.page_count
    except Exception:                                              # noqa: BLE001
        return 0


# ---- 索引读写 --------------------------------------------------------------


def resolve_root(index: dict | None, cli_root: str | None) -> Path:
    if cli_root:
        return Path(cli_root)
    env = os.environ.get(DEFAULT_ROOT_ENV)
    if env:
        return Path(env)
    if index and index.get("default_root"):
        return Path(index["default_root"])
    raise SystemExit(f"找不到题库根目录：请给 --scan <root>，或设 {DEFAULT_ROOT_ENV}")


def load_index(path: Path | None = None) -> dict:
    path = Path(path or DEFAULT_INDEX)
    if not path.is_file():
        raise SystemExit(f"索引不存在：{path}（先跑 --scan）")
    return json.loads(path.read_text(encoding="utf-8"))


def save_index(index: dict, path: Path | None = None) -> Path:
    path = Path(path or DEFAULT_INDEX)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")
    return path


# ---- 筛选 / 任务单 ---------------------------------------------------------


def select(index: dict, level=None, cert=None, difficulty=None,
           only_volume=False, limit=None) -> list[dict]:
    out = []
    for p in index["problems"]:
        if level is not None and p.get("level") != level:
            continue
        if cert and (p.get("certification") or "").upper() != cert.upper():
            continue
        if difficulty and (p.get("difficulty") or "").lower() != difficulty.lower():
            continue
        if only_volume and p.get("volume_mm3") is None:
            continue
        out.append(p)
    return out[:limit] if limit else out


def task_stub(index: dict, pid: str) -> dict:
    """把一道题变成「建模任务单」：输入图纸 + 判卷靶值 + 空计划骨架。

    刻意**不给解法**：计划 steps 留空，由建模者（人或 Agent）读图填写。
    """
    hits = [p for p in index["problems"] if p.get("id") == pid]
    if not hits:
        raise SystemExit(f"索引里没有题号 {pid}；用 --list 看看有哪些")
    p = hits[0]
    root = Path(os.environ.get(DEFAULT_ROOT_ENV) or index.get("default_root") or ".")
    return {
        "schema": "yoyo.practice_task/1",
        "id": p.get("id"),
        "level": p.get("level"),
        "title": p.get("title"),
        "certification": p.get("certification"),
        "difficulty": p.get("difficulty"),
        "duration_min": p.get("minutes"),
        "units": p.get("units") or "MMGS",
        "drawing": (root / p["file"]).as_posix(),
        "reference_models": p.get("zip_models") or [],
        "suggested": {
            "sketch_tools": p.get("sketch_tools") or [],
            "features": p.get("features") or [],
            "hint": p.get("hint"),
        },
        "design_intent": p.get("design_intent") or [],
        "expect": {
            # ★ 判卷靶值：来自官方 PDF 的 'Volume: N <单位>'
            "volume_mm3": p.get("volume_mm3"),
            "volume_text": p.get("volume_text"),
            "volume_unit_native": p.get("volume_unit"),
            "volume_value_native": p.get("volume_value"),
            "tolerance_pct": 1.0,
            "note": "体积对不上 = 建模错了（漏特征 / 尺寸读错 / 布尔方向反）。",
            **({"warning": p["volume_suspect"]} if p.get("volume_suspect") else {}),
        },
        "plan": {
            "schema": "yoyo.swplan/1",
            "meta": {"name": f"practice-{p.get('id')}", "source": "practice problems"},
            "params": {},
            "steps": [],
            "_todo": "读图填 steps；目标 = 真实机建出实体且体积落在 expect.volume_mm3 ±1%",
        },
    }


# ---- CLI -------------------------------------------------------------------


def _fmt_problem(p: dict) -> str:
    vol = p.get("volume_mm3")
    if vol is None:
        vol_s = "—"
    elif p.get("volume_unit") != "mm3":
        vol_s = f"{vol:g}({p['volume_unit']})"
    else:
        vol_s = f"{vol:g}"
    return (f"  {str(p.get('id')):<7} L{str(p.get('level')):<3}"
            f"{(p.get('difficulty') or '?'):<7}{(p.get('certification') or '?'):<6}"
            f"{vol_s:>14}   {p.get('file')}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Practice Problems 题库索引器")
    ap.add_argument("--scan", metavar="ROOT", help="扫题库根目录并重建索引")
    ap.add_argument("--out", metavar="PATH", help=f"索引输出路径（默认 {DEFAULT_INDEX}）")
    ap.add_argument("--stats", action="store_true", help="打印统计")
    ap.add_argument("--list", action="store_true", help="按条件列题")
    ap.add_argument("--task", metavar="ID", help="出一道任务单（如 6.1）")
    ap.add_argument("--out-dir", metavar="DIR", help="任务单输出目录（默认打印到 stdout）")
    ap.add_argument("--root", metavar="DIR", help="题库根目录（覆盖索引里的 default_root）")
    ap.add_argument("--level", type=int)
    ap.add_argument("--cert")
    ap.add_argument("--difficulty")
    ap.add_argument("--only-volume", action="store_true",
                    help="只要带体积判据的题")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args(argv)

    if a.scan:
        idx = scan(Path(a.scan))
        path = save_index(idx, Path(a.out) if a.out else None)
        c = idx["counts"]
        print(f"已建索引：{path}")
        print(f"  题目 {c['n_problems']} 道 / {c['n_levels']} 关"
              f"  带体积判据 {c['n_with_volume']}  缺体积 {c['n_without_volume']}"
              f"  zip 题 {c['n_from_zip']}")
        if c.get("volume_units"):
            print(f"  体积原单位分布：{c['volume_units']}"
                  f"（已统一换算成 mm3）")
        if c.get("volume_kinds"):
            print(f"  靶值形态：{c['volume_kinds']}"
                  f"（value=可直接判卷；其余=需人肉/表格补）")
        if c.get("n_volume_suspect"):
            print(f"  ⚠ 体积单位存疑 {c['n_volume_suspect']} 条（cm3 但数值像 mm3）")
        for lv in idx["levels"]:
            print(f"    L{lv['level']:<2} {lv['title']:<38}"
                  f"pdf={lv['n_pdf']:<3} zip={lv['n_zip']}")
        if c["n_without_volume"]:
            miss = [p.get("id") or p["file"] for p in idx["problems"]
                    if p.get("volume_mm3") is None]
            print(f"  缺体积判据的题（{len(miss)}）：{miss[:12]}"
                  + (" …" if len(miss) > 12 else ""))
        return 0

    idx = load_index(Path(a.out) if (a.out and Path(a.out).is_file()) else None)

    if a.task:
        t = task_stub(idx, a.task)
        # 让任务单里的路径能反映 --root 覆盖
        if a.root:
            p = next(p for p in idx["problems"] if p.get("id") == a.task)
            t["drawing"] = (Path(a.root) / p["file"]).as_posix()
        if a.out_dir:
            d = Path(a.out_dir)
            d.mkdir(parents=True, exist_ok=True)
            f = d / f"practice_{a.task.replace('.', '_')}.task.json"
            f.write_text(json.dumps(t, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")
            print(f"任务单：{f}")
        else:
            print(json.dumps(t, ensure_ascii=False, indent=2))
        return 0

    if a.stats:
        c = idx["counts"]
        print(f"题库：{idx['default_root']}")
        print(f"  题目 {c['n_problems']} 道 / {c['n_levels']} 关"
              f"  带体积判据 {c['n_with_volume']}"
              f"（{100 * c['n_with_volume'] / max(1, c['n_problems']):.1f}%）")
        by_cert: dict[str, int] = {}
        by_diff: dict[str, int] = {}
        for p in idx["problems"]:
            by_cert[p.get("certification") or "?"] = \
                by_cert.get(p.get("certification") or "?", 0) + 1
            by_diff[p.get("difficulty") or "?"] = \
                by_diff.get(p.get("difficulty") or "?", 0) + 1
        print("  按认证：" + "  ".join(f"{k}={v}" for k, v in sorted(by_cert.items())))
        print("  按难度：" + "  ".join(f"{k}={v}" for k, v in sorted(by_diff.items())))
        if c.get("volume_units"):
            print("  体积原单位：" + "  ".join(f"{k}={v}"
                                          for k, v in sorted(c["volume_units"].items()))
                  + "  （索引里已换算为 volume_mm3）")
        by_units: dict[str, int] = {}
        for p in idx["problems"]:
            by_units[p.get("units") or "?"] = by_units.get(p.get("units") or "?", 0) + 1
        print("  题目单位制：" + "  ".join(f"{k}={v}" for k, v in sorted(by_units.items())))
        print()
        for lv in idx["levels"]:
            sel = select(idx, level=lv["level"])
            nv = sum(1 for s in sel if s.get("volume_mm3") is not None)
            print(f"  L{lv['level']:<2} {lv['title']:<38} {len(sel):>3} 题"
                  f"  (带体积 {nv})")
        return 0

    if a.list:
        sel = select(idx, a.level, a.cert, a.difficulty, a.only_volume, a.limit)
        print(f"命中 {len(sel)} 题：")
        for p in sel:
            print(_fmt_problem(p))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
