#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_session_check.py —— **真机会话健康度**端到端冒烟（★ 不开 SolidWorks 跑不了）。

为什么需要它（2026-10-03 真实教训）：
    09:15 `asm_mate` 探针在某个 SolidWorks 会话里**成功**插进 2 个组件、建了配合；
    20 分钟后同一个会话里，`AddComponent5` 对**任何**零件、**任何**配置选项、
    **新建的**和**已存盘的**装配体，一律**静默返回 None**（不抛异常、无错误码、
    组件数 0→0），而同一时刻 `NewDocument` / `GetComponentCount` / `GetComponents` /
    `CloseDoc` **全都正常**。

    ⇒ 这种故障的表象是"我的 op 写错了"，真因却是**会话坏了**。
      不做这一步检查，一次探针失败会让人去改**本来正确的代码**
      （本项目第 84 条铁律：报错时先怀疑校验器自己 —— 这里同理：失败时先怀疑环境）。

本脚本做的事：**从零建一个最小零件 → 存盘 → 新建装配体 → 插两次（已打开的零件）
→ 把零件存盘后关掉、再按磁盘路径插一次 → 建一条重合配合 → 读配合数 → 清理**，
每一环都用**读回**当判据（不信任何返回值），最后给
`HEALTHY` / `DEGRADED` / `BROKEN` 三档结论 + 退出码 0/1/2。

    HEALTHY  全部通过 ⇒ 可以放心跑探针 / 建模计划
    DEGRADED 基础建模可用、装配体不可用（就是本次遇到的那种）
    BROKEN   连新建零件/拉伸都不行

★★ 2026-10-05 补的**盲区**：`AddComponent5` 可能**只认已在会话里打开的零件** ——
   喂**磁盘路径**时静默回 None（重试 / 换 ConfigOption / 给配置名全无效），
   而 `OpenDoc6` 打开同一文件正常；此时**旧的第④项仍会 PASS（假绿）**。
   故新增「插『未打开』的零件（磁盘路径）」一项 + `insert_from_disk` 能力位。
   对策：把要插的零件**先 `open`** 再插（见 references/30-pitfalls.md §120⑭）。

用法：
    export APPDATA="C:\\Users\\OMEN\\AppData\\Roaming"
    /c/Python313/python.exe scripts/sw_session_check.py            # 人看的
    /c/Python313/python.exe scripts/sw_session_check.py --json     # 供代理解析

★ 副作用：会新建并**关闭**临时文档、在 `--out` 目录（默认系统临时目录）留一个
  `session_check.SLDPRT`。**不碰任何已打开的文档**（只用 NewDocument/CloseDoc
  针对自己新建的标题），不改用户偏好。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import sw_connect  # noqa: E402
import sw_ops      # noqa: E402

VERDICT_OK = "HEALTHY"
VERDICT_DEGRADED = "DEGRADED"
VERDICT_BROKEN = "BROKEN"


class Check:
    """一格检查：名字 + 跑了什么 + 读回什么 + 结论。"""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, name: str, ok: bool, detail: str = "", hint: str = "") -> bool:
        self.items.append({"name": name, "ok": bool(ok), "detail": detail,
                           "hint": hint if not ok else ""})
        return bool(ok)

    @property
    def failed(self) -> list[dict]:
        return [i for i in self.items if not i["ok"]]


def _open_titles(sw) -> set[str]:
    out: set[str] = set()
    with contextlib.suppress(Exception):
        from sw_connect import member                # noqa: PLC0415
        for d in (member(sw.app, "GetDocuments") or ()):
            with contextlib.suppress(Exception):
                t = member(d, "GetTitle")
                if t:
                    out.add(str(t))
    return out


def _volume_mm3(sw) -> float:
    """当前文档体积（mm³）；读不到回 0.0。"""
    with contextlib.suppress(Exception):
        mp = sw.mass_props()
        if mp.get("volume_m3") and not mp.get("suspect"):
            return float(mp["volume_m3"]) * 1e9
    return 0.0


def _close(sw, title: str | None) -> None:
    """只关**指定标题**的文档 —— 绝不 `CloseAllDocuments`（那会连用户文档一起丢改动）。"""
    if not title:
        return
    with contextlib.suppress(Exception):
        sw.app.CloseDoc(title)


def run(out_dir: str, verbose: bool = True) -> dict:
    c = Check()
    t0 = time.time()
    box = os.path.join(out_dir, "session_check.SLDPRT").replace("/", "\\")
    asm = os.path.join(out_dir, "session_check.SLDASM").replace("/", "\\")

    sw = None
    closed_docs: list[str] = []
    try:
        sw = sw_connect.SwSession.open(visible=True)
    except Exception as e:                                        # noqa: BLE001
        c.add("连上 SolidWorks", False, f"{type(e).__name__}: {e}",
              "SolidWorks 没起来。手动开一下，或看许可证/启动报错。")
        return _verdict(c, t0, None, [])

    ver = getattr(sw, "version", "?")
    c.add("连上 SolidWorks", True, f"版本 {ver}，常量来源 {sw.C.source}")

    ctx = {"params": {}, "vars": {}, "outputs": {}, "feature_names": [],
           "_doc_pin": None, "_allow_doc_switch": False}

    # ★ 收尾靠**快照差集**，不靠"追踪自己开过的标题" ——
    #   因为 `save_as` 之后文档标题会变（零件1 → session_check.SLDPRT），
    #   按旧标题关会漏掉它（漏掉 = 下一轮撞上"同名文件被打开"）。
    #   差集的口径 = 「这一轮之前没有、这一轮之后有」= 一定是本脚本新建的。
    before_titles = _open_titles(sw)

    def step(sid: str, st: dict):
        return sw_ops.run_op(sw, st, ctx)

    def track() -> None:
        """中途留个记录用（收尾不依赖它）。"""
        return

    try:
        # ---- ① 零件：新建 → 草图 → 拉伸 → 包围盒 ----
        part_ok = True
        try:
            step("np", {"op": "new_part"})
            track()
            step("pl", {"op": "select_plane", "plane": "Front"})
            step("s1", {"op": "sketch_begin"})
            step("r1", {"op": "rect_corner", "x1": 0, "y1": 0, "x2": 40, "y2": 30})
            step("s2", {"op": "sketch_end", "name": "ChkSk"})
            step("e1", {"op": "extrude_boss", "depth": 10, "name": "ChkBox"})
            r = step("bb", {"op": "check_bbox", "expect": [40, 30, 10],
                            "tol_mm": 0.05})
            part_ok = abs(r["size_mm"][0] - 40.0) < 0.05
        except Exception as e:                                    # noqa: BLE001
            part_ok = False
            c.add("零件：新建+拉伸+包围盒", False,
                  f"{type(e).__name__}: {str(e).splitlines()[0][:160]}",
                  "连最基础的建模都不行 ⇒ 会话（或许可证/模板）坏了，先重启 SolidWorks。")
        if part_ok:
            c.add("零件：新建+拉伸+包围盒", True, "40×30×10 读回一致")
        if not part_ok:
            return _verdict(c, t0, sw, [])

        # ---- ② 存盘（落盘验证在 sw_connect.save_as 里） ----
        try:
            step("sv", {"op": "save_as", "path": box, "overwrite": True})
            landed = os.path.exists(box) and os.path.getsize(box) > 1000
            c.add("零件：存盘落盘", landed,
                  f"{os.path.basename(box)} {os.path.getsize(box) if landed else 0} 字节",
                  "SaveAs 说成功但没落盘 —— 看 save_as 的路径占用/权限排查串。")
        except Exception as e:                                    # noqa: BLE001
            c.add("零件：存盘落盘", False,
                  f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")

        # ---- ②' ★★ mirror 能力（2026-10-03 补：它之前根本没被测过）----
        #   完整闭环：板**横跨镜像面** → 切孔 → 镜像 → **体积必须变**。
        #   ★ 判据用体积而不是返回值（§96②：本项目铁律）。
        try:
            m0 = _volume_mm3(sw)
            for _st in ({"op": "new_part"},
                        {"op": "select_plane", "plane": "Front"},
                        {"op": "sketch_begin"},
                        # ★★ 板必须**横跨 x=0**（镜像面 Right 从中间切开）
                        {"op": "rect_corner", "x1": -30, "y1": -10,
                         "x2": 30, "y2": 10},
                        {"op": "sketch_end", "name": "MirSk"},
                        {"op": "extrude_boss", "depth": 10,
                         "end_cond": "swEndCondMidPlane", "name": "MirPlate"},
                        {"op": "select_plane", "plane": "Front"},
                        {"op": "sketch_begin"},
                        {"op": "circle", "cx": 20, "cy": 0, "r": 5},
                        {"op": "sketch_end", "name": "MirHoleSk"},
                        {"op": "extrude_cut",
                         "end_cond": "swEndCondThroughAllBoth",
                         "both_directions": True, "name": "MirHole1"}):
                step("mir_%s" % _st["op"], _st)
            track()
            v_before = _volume_mm3(sw)
            rm = step("mr", {"op": "mirror", "target": "MirHole1",
                             "plane": "Right", "name": "MirMirror1"})
            v_after = _volume_mm3(sw)
            shrank = bool(v_before and v_after and v_after < v_before - 1.0)
            c.add("零件：mirror 特征级镜像（体积判据）", shrank,
                  "体积 %.1f → %.1f mm³，镜像特征 %s" % (
                      v_before or 0, v_after or 0, (rm or {}).get("feature")),
                  "★ 镜像没生效时**返回值仍是 Feature 对象、err=None、"
                  "GetErrorCode=0**（§96②）⇒ 只能看体积。"
                  "★ 最常见根因：**镜像面没有穿过实体**（板整个在一侧 ⇒ "
                  "只切到边界 ⇒ 镜像结果与原件重合，坑表 §100①）。")
        except Exception as e:                                    # noqa: BLE001
            c.add("零件：mirror 特征级镜像（体积判据）", False,
                  f"{type(e).__name__}: {str(e).splitlines()[0][:160]}",
                  "★ 这一项之前**根本没被测过** ⇒ 「会话健康」并不覆盖镜像能力。")

        # ---- ③ 装配体：新建 ----
        asm_ok = False
        asm_title = None
        try:
            r_na = step("na", {"op": "new_assembly"})
            track()
            asm_ok = bool(sw.is_assembly)
            asm_title = (r_na or {}).get("title")
            c.add("装配体：新建", asm_ok,
                  f"组件数 {sw.component_count()}",
                  "NewDocument 建出来不是装配体 ⇒ 装配体模板可能丢了。")
        except Exception as e:                                    # noqa: BLE001
            c.add("装配体：新建", False,
                  f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")

        # ---- ④ ★ 插组件（本项目最容易被"静默"掉的一环） ----
        ins_ok = False
        if asm_ok:
            try:
                n0 = sw.component_count()
                r1 = step("c1", {"op": "insert_component", "path": box,
                                 "x": 0, "y": 0, "z": 0, "name": "A"})
                r2 = step("c2", {"op": "insert_component", "path": box,
                                 "x": 100, "y": 0, "z": 10, "name": "B"})
                n1 = sw.component_count()
                ins_ok = n1 == n0 + 2
                c.add("装配体：插 2 个组件", ins_ok,
                      f"组件数 {n0}→{n1}，名字 {[r1.get('component'), r2.get('component')]}",
                      "★ 这就是 2026-10-03 遇到的那一环：`AddComponent5` **静默**回 None"
                      "（不抛异常、无错误码、组件数不涨）。它坏掉时**同会话的"
                      " NewDocument/GetComponentCount 都正常** ⇒ 只能靠这一步发现。"
                      "对策：重启 SolidWorks；别去改 op（op 是对的）。")
            except Exception as e:                                # noqa: BLE001
                c.add("装配体：插 2 个组件", False,
                      f"{type(e).__name__}: {str(e).splitlines()[0][:160]}",
                      "★ 同上：这一环坏了就是会话级故障，重启 SolidWorks。")

        # ---- ④b ★ 插「**未打开**的零件（磁盘路径）」——
        #   2026-10-05 真机发现的一个**盲区**：`AddComponent5` 若喂**磁盘路径**而该零件
        #   **不在当前会话里打开**，会**静默回 None**（不抛异常、无错误码、组件数不涨，
        #   重试 3 次 / 换 3 种 ConfigOption / 显式给配置名"默认" 全无效），而同一时刻
        #   `OpenDoc6` 打开同一个文件完全正常。**上面 ④ 插的是"自己刚建的、还开着的"
        #   零件 ⇒ 在这种故障下 ④ 仍会 PASS（假绿）**。这一步把零件**存盘后关掉、再按
        #   磁盘路径插**，专门把这种故障暴露出来。
        #   取证（干净态实测）：new_assembly→插未打开的件 FAIL；open 该件→插 FAIL
        #   （活动文档变成零件）；**open 全部零件 → new_assembly → 逐个插 全 OK**。
        #   ⇒ 对策：把要插的零件**先 `open`** 再插（见坑表 §120⑭）。
        if ins_ok:
            try:
                _close(sw, os.path.basename(box))          # 把刚建的零件关掉
                time.sleep(0.4)
                if asm_title:                              # 关掉零件后把装配体抢回活动位
                    with contextlib.suppress(Exception):
                        sw.app.ActivateDoc3(asm_title, False, 0)
                n0 = sw.component_count()
                step("c3", {"op": "insert_component", "path": box,
                            "x": 200, "y": 0, "z": 0})
                n1 = sw.component_count()
                disk_ok = n1 == n0 + 1
                c.add("装配体：插「未打开」的零件（磁盘路径）", disk_ok,
                      f"组件数 {n0}→{n1}",
                      "★ 若本项 FAIL 而上一条「插 2 个组件」PASS ⇒ `AddComponent5` "
                      "**只认已在会话里打开的零件**（喂磁盘路径静默回 None）。"
                      "对策：要插的零件**先 `open`** 再插 —— 见坑表 §120⑭。")
            except Exception as e:                                # noqa: BLE001
                c.add("装配体：插「未打开」的零件（磁盘路径）", False,
                      f"{type(e).__name__}: {str(e).splitlines()[0][:160]}",
                      "同上：把要插的零件先 `open` 再插（坑表 §120⑭）。")

        # ---- ⑤ 配合：建一条 + 数出来 ----
        mate_ok = False
        if ins_ok:
            try:
                m0 = sw.mate_count()
                rm = step("m1", {"op": "add_mate", "kind": "coincident",
                                 "entities": [{"type": "FACE", "point": [0, 0, 5]},
                                              {"type": "FACE", "point": [100, 0, 5]}],
                                 "align": "anti_aligned"})
                m1 = sw.mate_count()
                moved = bool((rm or {}).get("moved"))
                mate_ok = m1 == m0 + 1
                c.add("装配体：建 1 条重合配合", mate_ok,
                      f"配合数 {m0}→{m1}，新增 {(rm or {}).get('new_features')}"
                      f"，组件被挪动={moved}",
                      "配合建不出来 ⇒ 查 `EntitiesToMate` 的 VARIANT 写法"
                      "（直接赋 tuple/list 会**静默失效**）。")
                c.add("配合：没把组件挪走（anti_aligned）", not moved,
                      "组件包围盒前后一致" if not moved
                      else f"被挪了：{[(rm or {}).get('moved')]}",
                      "★ anti_aligned 竟然也挪了组件 ⇒ 语义与真机文档不符，"
                      "先别信任何 align 取值。")
            except Exception as e:                                # noqa: BLE001
                c.add("装配体：建 1 条重合配合", False,
                      f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")

        # ---- ⑥ 存装配体 ----
        if ins_ok:
            try:
                step("sv2", {"op": "save_as", "path": asm, "overwrite": True})
                landed = os.path.exists(asm)
                c.add("装配体：存盘落盘", landed, os.path.basename(asm))
            except Exception as e:                                # noqa: BLE001
                c.add("装配体：存盘落盘", False,
                      f"{type(e).__name__}: {str(e).splitlines()[0][:160]}")
    finally:
        # ---- 收尾：**快照差集** = 只关这一轮自己新建的文档，一个都不多碰 ----
        mine = sorted(_open_titles(sw) - before_titles)
        # 多遍：关掉一个可能让另一个（被引用的零件）才变成可关的
        for _ in range(3):
            for t in list(mine):
                _close(sw, t)
            left = sorted(_open_titles(sw) - before_titles)
            if not left:
                break
            mine = left
        closed_docs = mine
        with contextlib.suppress(Exception):
            sw.close()

    return _verdict(c, t0, sw, closed_docs)


def _verdict(c: Check, t0: float, sw, made: list[str] | None) -> dict:
    names = {i["name"] for i in c.items}
    # ★ mirror 能力项（2026-10-03 补）
    mir_items = [i for i in c.items if "mirror" in i["name"]]
    mirror_ok = bool(mir_items) and all(i["ok"] for i in mir_items)
    part_bad = any(i["name"].startswith("零件") and not i["ok"] for i in c.items)
    asm_bad = any(i["name"].startswith("装配体") and not i["ok"] for i in c.items)
    ins_bad = any("插 2 个组件" in i["name"] and not i["ok"] for i in c.items)
    # ★ 2026-10-05 补：插「**未打开**的零件（磁盘路径）」是**独立**能力 ——
    #   它坏掉时上面的 `ins_bad` 仍然 PASS（插的是已打开的零件）⇒ 必须单列。
    disk_items = [i for i in c.items if "未打开" in i["name"]]
    disk_bad = bool(disk_items) and any(not i["ok"] for i in disk_items)
    if part_bad:
        verdict = VERDICT_BROKEN
    elif ins_bad or asm_bad:
        verdict = VERDICT_DEGRADED
    elif any(not i["ok"] for i in c.items):
        verdict = VERDICT_DEGRADED
    else:
        verdict = VERDICT_OK
    return {"verdict": verdict,
            "seconds": round(time.time() - t0, 2),
            "solidworks": getattr(sw, "version", None) if sw else None,
            "checks": c.items,
            "closed_docs": made or [],
            "leftover": made or [],
            "n_failed": len(c.failed),
            "n_total": len(c.items),
            "capabilities": {
                "part": not part_bad,
                "assembly": not asm_bad and not ins_bad,
                "insert_component": not ins_bad,
                # ★★ 2026-10-05 补：**磁盘路径**插入（零件未打开）是独立能力。
                #   它坏掉时装配体计划会在第一步就卡住，而旧版 session_check 全绿。
                "insert_from_disk": (not disk_bad) if disk_items else None,
                # ★★ 2026-10-03 补：mirror 之前**没被测过**，
                #   而它坏掉时上面三项**全绿**（「会话健康」不覆盖它）。
                "mirror": mirror_ok,
            } if names else {}}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="SolidWorks 真机会话健康度端到端冒烟")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--out", default="",
                    help="临时产物目录（默认 **每轮一个唯一子目录**）")
    a = ap.parse_args()
    # ★ 每轮**唯一子目录**：健康检查不该和上一轮撞名。上一次留下的
    #   session_check.SLDPRT 若还开在 SolidWorks 里，就会让这一轮
    #   「存盘落盘」报「SaveAs 返回成功但文档仍指向 (未保存)」——
    #   那是**上一轮的残留**在捣乱，不是这一轮的问题（真机踩过，2026-10-03）。
    out = a.out or os.path.join(tempfile.gettempdir(),
                                "sw_session_check_" + time.strftime("%H%M%S"))
    os.makedirs(out, exist_ok=True)
    r = run(out)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("=" * 62)
        print("SolidWorks 会话健康度（端到端冒烟）")
        print("=" * 62)
        for i in r["checks"]:
            flag = "PASS" if i["ok"] else "FAIL"
            print(f"  [{flag}] {i['name']}")
            if i["detail"]:
                print(f"         {i['detail']}")
            if i["hint"]:
                print(f"         ★ {i['hint']}")
        print("-" * 62)
        print(f"结论：{r['verdict']}   耗时 {r['seconds']}s   "
              f"通过 {r['n_total'] - r['n_failed']}/{r['n_total']}")
        cap = r.get("capabilities") or {}
        print("能力：" + "  ".join(
            f"{k}={'可用' if v else '不可用'}" for k, v in cap.items()))
        if r["verdict"] != VERDICT_OK:
            print("\n★ 建议：**重启 SolidWorks** 再跑探针/建模计划。")
            print("  失败时先怀疑环境（本项目的铁律：报错先怀疑校验器自己，"
                  "失败先怀疑会话）。")
    return {VERDICT_OK: 0, VERDICT_DEGRADED: 1, VERDICT_BROKEN: 2}[r["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
