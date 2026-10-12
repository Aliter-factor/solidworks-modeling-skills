#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_refscan.py —— 扫描一批参照零件，导出「验收基准表」。

为什么需要它：
    建模自动化最大的风险不是"画不出来"，而是"画出来了但不对"。
    如果手上已经有一套**人工做对的**零件（比如比赛/课程作业），
    那就等于拿到了一份现成的答案卷 —— 把它批量量一遍，
    以后每个自动生成的零件都能拿它逐项对账（包围盒/体积/特征数）。

它量什么（每件）：
    bbox_mm      实体包围盒（走 IBody2.GetBodyBox，不用会偏大的 GetPartBox）
    volume_mm3   体积         mass_g 质量
    bodies       实体数       features 特征数（含统计）
    feat_types   特征类型直方图（按 legacy GetTypeName）
    errors       错误码非 0 的特征（能揪出"建了但是坏的"）
    material     材料名（读不到就是 None，不报错）

用法：
    python scripts/sw_refscan.py --dir "C:\\学习软件\\一级减速器" --out ref.json
    python scripts/sw_refscan.py --dir ... --recursive        # 连子目录一起扫
    python scripts/sw_refscan.py --dir ... --md  ref.md       # 顺便出 Markdown 表
    python scripts/sw_refscan.py --dir ... --limit 5          # 先试 5 个

产物：
    <out>  JSON：{"schema","dir","scanned","items":[{...}], "problems":[...]}

注意：
    * 会启动 SolidWorks 并逐件打开/关闭；37 件大约 2~4 分钟。
    * 只读打开，不保存、不修改任何源文件。
    * 跳过 `~$xxx.SLDPRT`（SolidWorks 的锁文件，只有 5 字节）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sw_connect  # noqa: E402
from sw_connect import SwSession, member, typed  # noqa: E402

SCHEMA = "yoyo.swrefscan/1"


def _legacy_type(f) -> str | None:
    """特征的真实类型名。

    ★ `GetTypeName2()` 对 Instant3D 建出来的凸台/切除会回 "ICE"，
      那是 Instant3D 的标记、**不是错误**。要拿真类型必须用 GetTypeName()。
    """
    with contextlib.suppress(Exception):
        return member(f, "GetTypeName")
    return None


def _feature_stats(m) -> dict:
    """遍历特征树：数量 + 类型直方图 + 错误码。"""
    hist: dict[str, int] = {}
    errs: list[dict] = []
    n = 0
    f = member(m, "FirstFeature")
    while f is not None:
        n += 1
        t = _legacy_type(f) or "?"
        hist[t] = hist.get(t, 0) + 1
        with contextlib.suppress(Exception):
            code = member(f, "GetErrorCode")
            if code:
                errs.append({"name": member(f, "Name"), "type": t, "code": code,
                             "error": sw_connect.FEATURE_ERROR_NAMES.get(code, "?")})
        with contextlib.suppress(Exception):
            f = member(f, "GetNextFeature")
        if f is None:
            break
    return {"features": n, "feat_types": hist, "errors": errs}


def _material(m) -> str | None:
    """材料名。晚期绑定下 [out] 参数常常读不回，读不到就算了。"""
    for call in (
        lambda: member(typed(m, "IPartDoc"), "GetMaterialPropertyName2", "", None),
        lambda: member(m, "GetMaterialPropertyName2", "", None),
    ):
        with contextlib.suppress(Exception):
            v = call()
            if isinstance(v, str) and v:
                return v
    return None


def scan_one(sw: SwSession, path: Path) -> dict:
    item: dict = {"name": path.stem, "file": path.name,
                  "size_kb": round(path.stat().st_size / 1024, 1)}
    doc = None
    try:
        doc = sw.open_doc(str(path), read_only=True)
        if doc is None:
            item["error"] = "打开失败（返回 None）"
            return item
        m = sw.model
        if m is None:
            item["error"] = "打开后没有活动文档"
            return item

        item["title"] = member(m, "GetTitle")
        with contextlib.suppress(Exception):
            item["configs"] = list(member(m, "GetConfigurationNames") or [])

        # ★★ 2026-10-02：读**面表**之前先取"身份指纹"，读完再取一次比对。
        #   真机踩到：读一份 235 面的参照件时活动文档被别人换成了 `零件26`
        #   （底盘板/履带左/履带右），于是 bbox 从 35×23×23 变成 146.08×72.30×16.00
        #   而**完全不报错**。光比标题/路径抓不到（16-螺塞 那型路径标题都对、
        #   内容却换了），所以指纹里带上特征数 / 实体数 / 体积（见 doc_identity）。
        ident0 = sw.doc_identity()

        bb = sw.bounding_box()
        if bb:
            item["bbox_mm"] = [round((bb[3] - bb[0]) / sw_connect.MM, 3),
                               round((bb[4] - bb[1]) / sw_connect.MM, 3),
                               round((bb[5] - bb[2]) / sw_connect.MM, 3)]

        mp = sw.mass_props()
        if mp.get("suspect"):
            # ★ 别把"读不到"写成 0 —— 见 sw_connect.mass_props()
            item["mass_suspect"] = mp["suspect"]
        if "volume_m3" in mp:
            item["volume_mm3"] = round(float(mp["volume_m3"]) / (sw_connect.MM ** 3), 1)
        if "mass_kg" in mp:
            item["mass_g"] = round(float(mp["mass_kg"]) * 1000, 2)
        if "center_of_mass_m" in mp:
            item["com_mm"] = [round(v / sw_connect.MM, 2) for v in mp["center_of_mass_m"]]

        # ★ 取实体走 sw.bodies()（唯一入口）—— 曾经用 GetBodies2(0, True) 只取可见，
        #   实体一被抑制就数成 0，和 check_bbox 读到空是同一个坑。
        with contextlib.suppress(Exception):
            bodies = sw.bodies()
            item["bodies"] = len(bodies)
            item["body_query"] = sw.last_body_query

        item.update(_feature_stats(m))
        mat = _material(m)
        if mat:
            item["material"] = mat

        # ★★ 读完面表之后复核身份指纹：不一致 ⇒ 这份读数**不可信**（明确标出来）
        drift = sw.identity_diff(ident0, sw.doc_identity())
        if drift:
            item["identity_drift"] = drift
            item["error"] = ("活动文档在读面期间变了（读数可能来自另一份文件）："
                             + "; ".join(drift))
    except Exception as e:                                        # noqa: BLE001
        item["error"] = f"{type(e).__name__}: {e}"
    finally:
        # ★ 只关「确实是我们刚打开的这一份」。close_doc() 关的是**活动文档**，
        #   如果 open_doc 抛异常、活动文档还停在用户自己开着的零件上，
        #   无脑 close 会把用户没存盘的东西一起关掉。这里先核对路径。
        if doc is not None:
            with contextlib.suppress(Exception):
                cur = os.path.normcase(os.path.abspath(str(member(sw.model, "GetPathName"))))
                if cur == os.path.normcase(os.path.abspath(str(path))):
                    sw.close_doc(save=False)
    return item


def main() -> int:
    ap = argparse.ArgumentParser(description="扫描参照零件，导出验收基准表")
    ap.add_argument("--dir", required=True, help="零件所在目录")
    ap.add_argument("--out", default=None, help="JSON 输出路径")
    ap.add_argument("--md", default=None, help="顺便输出 Markdown 表")
    ap.add_argument("--recursive", action="store_true", help="连子目录一起扫")
    ap.add_argument("--limit", type=int, default=0, help="只扫前 N 个（试跑用）")
    ap.add_argument("--invisible", action="store_true", help="隐藏 SolidWorks 窗口")
    a = ap.parse_args()

    root = Path(a.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1

    pat = "**/*.SLDPRT" if a.recursive else "*.SLDPRT"
    files = sorted(p for p in root.glob(pat)
                   if not p.name.startswith("~$") and p.is_file())
    if a.limit:
        files = files[:a.limit]
    if not files:
        print(f"{root} 下没找到 .SLDPRT")
        return 1

    print(f"待扫描 {len(files)} 件（目录 {root}）")
    items: list[dict] = []
    problems: list[str] = []
    t0 = time.time()

    with SwSession.open(visible=not a.invisible) as sw:
        with sw.suppress_prompts():
            for i, p in enumerate(files, 1):
                t = time.time()
                it = scan_one(sw, p)
                items.append(it)
                tag = "!"
                if "error" in it:
                    problems.append(f"{it['name']}: {it['error']}")
                elif it.get("errors"):
                    tag = "E"
                    problems.append(
                        f"{it['name']}: {len(it['errors'])} 个特征带错误码 "
                        f"({', '.join(e['name'] + '=' + str(e['code']) for e in it['errors'][:3])})")
                elif not it.get("bodies"):
                    tag = "0"
                    problems.append(f"{it['name']}: 0 个实体")
                else:
                    tag = "ok"
                bb = it.get("bbox_mm")
                print(f"  [{i:2d}/{len(files)}] {tag:2s} {it['name'][:28]:30s} "
                      f"bbox={bb} vol={it.get('volume_mm3')} "
                      f"feat={it.get('features')} ({time.time()-t:.1f}s)")

    out = {
        "schema": SCHEMA,
        "dir": str(root),
        "scanned": len(items),
        "elapsed_s": round(time.time() - t0, 1),
        "items": items,
        "problems": problems,
    }
    if a.out:
        op = Path(a.out)
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nJSON -> {op}")

    if a.md:
        lines = ["| 序 | 零件 | 包围盒 (mm) | 体积 (mm³) | 质量 (g) | 实体 | 特征 | 材料 |",
                 "|---:|---|---|---:|---:|---:|---:|---|"]
        for it in items:
            bb = it.get("bbox_mm")
            lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                it["file"].split("-")[0],
                it["name"],
                "×".join(str(v) for v in bb) if bb else "—",
                it.get("volume_mm3", "—"), it.get("mass_g", "—"),
                it.get("bodies", "—"), it.get("features", "—"),
                it.get("material") or "—"))
        mp = Path(a.md)
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Markdown -> {mp}")

    if problems:
        print(f"\n需要注意 {len(problems)} 条：")
        for x in problems[:20]:
            print("  -", x)
    print(f"\n完成：{len(items)} 件，用时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
