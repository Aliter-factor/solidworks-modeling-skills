#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_plan.py —— 建模计划的校验器与执行器。

计划文件（plan JSON）就是「AI 听得懂的语言」的落点：一份声明式的建模意图，
可以在**完全不开 SolidWorks**的情况下被校验，然后再一次性执行。

三种运行模式：
    --check     只做静态校验：操作是否存在、参数是否齐、**先后顺序对不对**、
                齿轮参数是否根切、有没有 verify/save 步骤。秒级，适用于迭代开发。
    --dry-run   在上面基础上把表达式全部求值、把每个步骤将要调用的 API 打出来，
                但不碰 SolidWorks。用来确认「AI 理解的动作序列」和预期一致。
    --run       真的连上 SolidWorks 按顺序执行，逐步计时，出错就报出是第几步、
                并给出该步的 API 序列方便定位。

为什么顺序校验要单独做一层：
    SolidWorks 的失败往往发生在**几步之后**（先画错面、阵列选错 mark、轮廓粘连），
    报错信息完全指不到根因。能在离线阶段拦住「拉伸一个还没闭合的草图」
    这类错误，一次能省十分钟。

用法：
    python sw_plan.py plan.json --check
    python sw_plan.py plan.json --dry-run
    python sw_plan.py plan.json --run --report run.json
    python sw_plan.py --template gear        # 打印一个齿轮计划的骨架
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sw_ops  # noqa: E402
import sw_gear  # noqa: E402
import sw_env  # noqa: E402

SCHEMA = "yoyo.swplan/1"

# 互斥状态：进入一个就清掉另一个。状态机只认这三种关系。
EXCLUSIVE = {
    "sketch_open": {"sketch_closed"},
    "sketch_closed": {"sketch_open"},
}

INITIAL_STATE = {"sketch_closed"}

# ★ 控制键：**不是** op 的参数，校验时不做「不认识参数」告警、执行时不传给 op。
#   `optional` 是 2026-10-02 新加的（见 execute 里的说明）。
CONTROL_KEYS = ("op", "id", "note", "allow_sealed", "optional")


out_dir = sw_env.out_dir          # 产出目录：YOYO_SW_OUT > D:/out > <cwd>/out


# ============================================================ 校验


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.notes: list[str] = []
        self.trace: list[dict] = []

    @property
    def ok(self) -> bool:
        return not self.errors

    def err(self, step_id: str, msg: str) -> None:
        self.errors.append(f"[{step_id}] {msg}")

    def warn(self, step_id: str, msg: str) -> None:
        self.warnings.append(f"[{step_id}] {msg}")

    def note(self, msg: str) -> None:
        self.notes.append(msg)


def load_plan(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"计划文件不存在: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"计划文件不是合法 JSON: {p}\n  {e}")


def validate(plan: dict, evaluate: bool = False) -> Report:
    """核心校验。evaluate=True 时额外做表达式求值（dry-run 用）。"""
    r = Report()

    # --- 顶层结构 ---
    if plan.get("schema") != SCHEMA:
        r.warn("-", f"schema 建议写成 '{SCHEMA}'，当前 {plan.get('schema')!r}")
    params = plan.get("params", {})
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise SystemExit("计划里必须有非空的 steps 数组")
    if not isinstance(params, dict):
        raise SystemExit("params 必须是对象")

    # --- 模型类别检查 ---
    state = set(INITIAL_STATE)
    ctx: dict = {"params": params, "vars": {}, "outputs": {}}
    seen_ids: set[str] = set()
    used_ops: dict[str, int] = {}
    has_verify = False
    has_save = False
    gear_decls: list[str] = []

    # ★ 齿轮体检要报齿宽 b，但 `gear_profile` 步**不带 b**（b 是 `extrude_boss.depth`），
    #   而 `sw_gear.GearParams.b` 的默认值是 **20** ⇒ 不喂进去就会**打印 20 而实际拉伸 26**。
    #   2026-10-02 自查踩到：那行"b: 20.0"看着像"齿宽建错了"，其实只是**体检输出在说谎**
    #   （又是"返回值会说谎"，只是这次说谎的是我自己的打印）。
    #   ⇒ 从同一份计划里的**凸台拉伸深度**取 b；取不到就**不报**（宁可不报，不报错的）。
    _plan_gear_b: float | None = None
    for _st in steps:
        if isinstance(_st, dict) and _st.get("op") == "extrude_boss":
            _d = _st.get("depth")
            if isinstance(_d, (int, float)):
                _plan_gear_b = float(_d)
                break

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            r.err(f"#{i}", "步骤必须是对象")
            continue
        sid = step.get("id") or f"#{i}"
        if sid in seen_ids:
            r.err(sid, "id 重复，后续步骤引用会产生歧义")
        seen_ids.add(sid)

        name = step.get("op")
        if not name:
            r.err(sid, "缺少 op 字段")
            continue
        spec = sw_ops.OPS.get(name)
        if spec is None:
            r.err(sid, f"未知操作 '{name}'。可用：{', '.join(sorted(sw_ops.OPS))}")
            continue
        used_ops[name] = used_ops.get(name, 0) + 1
        if spec.cat == "verify":
            has_verify = True
        if name == "save_as":
            has_save = True

        # --- 参数齐备性 ---
        given = {k: v for k, v in step.items() if k not in CONTROL_KEYS}
        for pname, pspec in spec.params.items():
            required = pspec.get("required", True)
            has_default = "default" in pspec
            if pname not in given and required and not has_default:
                r.err(sid, f"{name} 缺少必填参数 '{pname}'（{pspec.get('type')}"
                           f"{'/' + pspec['unit'] if pspec.get('unit') else ''}）")
        for k in given:
            if k not in spec.params and k not in ("x", "y", "z"):
                r.warn(sid, f"{name} 不认识参数 '{k}'（会被忽略）")
        # 参数组：至少要给一个（例如圆必须给 r 或 d）
        for group in spec.one_of:
            if not any(g in given for g in group):
                r.err(sid, f"{name} 必须给 {' 或 '.join(group)} 中的至少一个")
        # ★★ `optional` 必须是**非空理由字符串**（2026-10-02）：允许某步真机建不出来
        #   而计划仍能跑完（拿到后面的 bbox/质量验收），但**必须写明为什么** ——
        #   `"optional": true` 这种写法**拦死**，否则"可选"会变成藏失败的暗门。
        if "optional" in step:
            ov = step["optional"]
            if not isinstance(ov, str) or not ov.strip():
                r.err(sid, "`optional` 必须是**非空字符串（写清为什么可以不要这步）**；"
                           "不接受 true/false —— 那样会把失败藏起来")
            elif len(ov.strip()) < 8:
                r.warn(sid, f"`optional` 理由太短（{ov!r}），建议写清后果与替代判据")

        # --- 顺序约束（本文件最值钱的一段）---
        # ★★ 2026-10-11：`requires` 现在支持 **OR 组**（元素写成元组 = 至少满足一个），
        #   所以判断交给 `sw_ops.missing_requires()`；这里只负责把「缺什么」讲清楚。
        missing = sw_ops.missing_requires(spec.requires, state)
        if missing:
            # ★ 提示必须**按缺的状态给**：以前一律喷「忘了 sketch_begin / 拉伸之前
            #   草图还没画」，装配体顺序错了也这么说 ⇒ 把人往错方向指。
            _hint = {
                "sketch_open": "忘了 sketch_begin",
                "sketch_closed": "忘了 sketch_end（草图还没关）",
                "doc_open": "还没有活动文档（new_part / new_assembly / open）",
                "asm_open": "还没 new_assembly（或 open 一个 .SLDASM）",
                "has_component": "装配体里还没插组件（insert_component）—— "
                                 "配合是两个组件之间的关系，空装配体里加不了",
                "has_feature": "还没有实体特征（先建出第一个实体）",
                "has_curve": "还没有参考曲线（先 curve_xyz）",
                "has_sketch": "还没画过任何草图（先 sketch_begin / sketch_end）",
                "helix": "还没有螺旋线（先建 helix）",
            }

            def _one(m):
                if isinstance(m, (tuple, list)):
                    return "（" + " 或 ".join(sw_ops.STATES.get(s, s) for s in m) + "）"
                return sw_ops.STATES.get(m, m)

            def _why(m):
                if isinstance(m, (tuple, list)):
                    return "需要 " + " 或 ".join(f"`{s}`" for s in m) + " 中的**至少一个**"
                return _hint.get(m, f"缺状态 {m}")

            r.err(sid, f"顺序错误：{name}（{spec.cn}）需要 "
                       f"{'/'.join(_one(m) for m in missing)}，"
                       f"但此刻的状态是 {{{', '.join(sorted(state))}}}。"
                       f"常见原因：" + "；".join(_why(m) for m in missing) + "。")
        # --- 状态转移 ---
        for e in spec.effect:
            state.add(e)
            for drop in EXCLUSIVE.get(e, ()):
                state.discard(drop)
        # ★★ 2026-10-05：`open` 的 effect 是**静态**的（只有 `doc_open`），
        #   但「打开一个 .SLDASM」事实上就已经进入装配体态。不补这一步，
        #   任何「先 open 参照装配体、再 check/interference」的诊断计划都会被
        #   离线校验器判成「顺序错误」⇒ 逼人去真机跑，而离线校验的全部价值
        #   就在于**不用开 SolidWorks 也能拦住顺序错**。
        #   ⇒ 按**扩展名**推：`.sldasm` ⇒ 补 asm_open + has_component。
        #   ★ `has_component` 是**推断**（离线读不到装配体里有没有组件），
        #     所以这里显式给一条 warning —— 推断出来的绿灯必须自己声明。
        if name == "open":
            _p = str(step.get("path") or "")
            if _p.lower().endswith(".sldasm"):
                state.add("asm_open")
                state.add("has_component")
                r.warn(sid, "按 `.sldasm` 扩展名**推断**打开的是装配体（补 asm_open/"
                            "has_component）。离线读不到里面到底有没有组件 —— "
                            "若实际为空，后面依赖 `has_component` 的 op 会在真机报错")
            elif _p.lower().endswith(".sldprt"):
                # ★★ 2026-10-11（§142）：打开一个**已有实体零件**后再去料/加特征是常见
                #   改型流程，但 `open` 的静态 effect 只有 `doc_open` ⇒ 后面的
                #   `extrude_cut`（requires 含 has_feature）会被离线校验器判「顺序错误」，
                #   逼人开真机跑。离线读不到里面有几个体，所以按**扩展名推断**补 has_feature，
                #   并显式给一条 warning —— 推断出来的绿灯必须自己声明。
                #   ★ 若打开的是**纯曲面**零件（无实体），此推断为**假绿**：后续
                #     `thicken` 需要的是 `has_surface`、`extrude_cut` 需要实体，
                #     真机上仍会报错。故 warning 里点明「只当参照/改实体件用」。
                state.add("has_feature")
                r.warn(sid, "按 `.sldprt` 扩展名**推断**打开的零件**已有实体特征**"
                            "（补 has_feature）。离线读不到里面到底有没有实体 —— "
                            "若打开的是**纯曲面**件，依赖 `has_feature` 的 op（如 "
                            "extrude_cut）会在真机报错（纯曲面件要的是 `thicken`，"
                            "它需要 `has_surface`）")
        if name == "sketch_begin":
            state.add("sketch_open")
            state.discard("sketch_closed")

        # --- 齿轮参数离线体检（不需要开 SolidWorks）---
        if name == "gear_profile":
            gear_decls.append(sid)
            try:
                p = {k: (sw_ops.resolve(v, ctx) if evaluate else v)
                     for k, v in given.items() if k != "note"}
                if evaluate:
                    g = sw_gear.GearParams(
                        mn=float(p["mn"]), z=int(p["z"]),
                        beta=float(p.get("beta", 0) or 0),
                        alpha_n=float(p.get("alpha_n", 20) or 20),
                        x=float(p.get("x", 0) or 0),
                        bore=p.get("bore"),
                        # ★ b 从同一计划的凸台拉伸深度取（不取默认 20 —— 见上方注释）
                        **({"b": _plan_gear_b} if _plan_gear_b is not None else {}))
                    for d in g.diagnostics():
                        if d["level"] == "error":
                            r.err(sid, f"齿轮参数硬错误 [{d['code']}] {d['msg']}")
                        elif d["level"] == "warn":
                            r.warn(sid, f"齿轮参数提醒 [{d['code']}] {d['msg']}")
                    r.note(f"{sid} 齿轮几何: {g.summary()}"
                           + (f"（b={_plan_gear_b:g} 取自同计划 extrude_boss.depth）"
                              if _plan_gear_b is not None
                              else "（本计划无凸台拉伸 ⇒ b 未参与；b 只用于拉伸不参与齿形）"))
                    for md in g.diagnostics():
                        if md["level"] == "info":
                            r.note(f"{sid} {md['msg']}")
            except Exception as e:                              # noqa: BLE001
                r.err(sid, f"齿轮参数无法解析: {e}")

        # --- 表达式求值（dry-run）---
        resolved = dict(given)
        if evaluate:
            try:
                resolved = {k: sw_ops.resolve(v, ctx) for k, v in given.items()}
                ctx["vars"][sid] = resolved
            except Exception as e:                              # noqa: BLE001
                r.err(sid, f"参数求值失败: {e}")
        r.trace.append({"id": sid, "op": name, "cn": spec.cn,
                        "requires": [list(x) if isinstance(x, (tuple, list)) else x
                                     for x in spec.requires],
                        "params": {k: (v if not isinstance(v, (list, dict)) else "...")
                                   for k, v in resolved.items()},
                        "api": list(spec.api)})

    # --- 全局建议 ---
    if not has_verify:
        r.warn("-", "计划里没有 verify 类步骤（check_bbox / check_mass / "
                    "check_feature_count）。加一步能自动化判断「建出来的对不对」")
    if not has_save:
        r.warn("-", "计划里没有 save_as，执行完结果只在内存在，关掉就没了")
    if "sketch_open" in state:
        r.err("-", "计划结束时草图还开着（缺 sketch_end）—— 后续特征会拿不到闭合轮廓")
    r.note(f"用到 {len(used_ops)} 种操作，共 {len(steps)} 步："
           + "、".join(f"{k}×{v}" for k, v in sorted(used_ops.items())))
    return r


def print_report(r: Report, verbose: bool = True) -> None:
    if verbose:
        print("=" * 68)
        print("步骤 / API 调用序列")
        print("=" * 68)
        for t in r.trace:
            pstr = ", ".join(f"{k}={v}" for k, v in t["params"].items())
            print(f"\n  {t['id']:>8}  {t['op']}  — {t['cn']}")
            if pstr:
                print(f"            {pstr}")
            for api in t["api"]:
                print(f"            → {api}")
    for n in r.notes:
        print(f"\n  · {n}")
    if r.warnings:
        print("\n警告：")
        for w in r.warnings:
            print("  !", w)
    if r.errors:
        print("\n错误：")
        for e in r.errors:
            print("  X", e)
    print(f"\n结果：{'通过' if r.ok else f'不通过（{len(r.errors)} 个错误）'}"
          f"{'' if r.ok else ''}，{len(r.warnings)} 个警告")


# ============================================================ 执行
#
# ------------------------------------------------- 构建期"文档指纹"守卫
#
# ★★ 2026-10-02 真机坐实（本轮最危险的一个缺口）：**这套脚本是挂在用户那个
#   "活着"的 SolidWorks 会话里干活的**。同一个会话里，用户随时可能切走活动文档 ——
#   实测在读数期间活动文档从 `10-通气塞.SLDPRT` 变成了 `d:\out\excavator.SLDPRT`
#   （特征 28 → 49、实体 1 → 2、体积 ×300），而**所有读操作都挂在活动文档上**。
#
#   读路径已经守住了（`sw_connect.doc_identity` / `sw_profile._apply_drift`），
#   但**构建路径一条守卫都没有**，后果比读错严重得多：
#     · `sketch_begin` / `extrude_boss` 会往**别人的模型**里加特征；
#     · `save_as` 会把**别人那份文档**存成我们的零件，而它现有的三道验
#       （路径指向正确 / 文件新写了 / 里面不是空零件）**全都会通过** ——
#       因为那三道验只看"存到哪了"，从不看"存的是谁"。
#   ⇒ 因此：凡是**我们自己主动换/改文档身份**的 op（见 `_DOC_PIN_OPS`）之后钉住指纹，
#     **每一步执行前**核对一次；
#     漂了就按标题抢回来（用户切走通常是瞬时的），抢不回就**中止**。
#
# ★ 指纹字段必须与"读参照件"分开：
#   · 读参照件：内容**不该变** ⇒ 用 title/path/features/bodies/volume（全字段）；
#   · 构建期：特征数/实体数/体积**本来就在变**（每一步都在加特征）⇒ 用全字段
#     会把每一步都判成漂移，所以这里**只取 title/path**（这两个表征"是哪份文档"，
#     不随建模进度变）。
#
# ★★ 2026-10-03 补漏：**"钉指纹的 op"必须覆盖所有"我们自己主动换/改文档身份"的操作**。
#   原来的集合只有 `new_part` / `open` —— 这在**零件类**计划里恰好够用，
#   因为那些计划只在开头 `new_part`、到最后才 `save_as`（此后没有步骤了）。
#   但装配体类计划是 `new_part → save_as → new_assembly → …`：
#     · `save_as` 会**改标题和路径** ⇒ 指纹立刻"过期"；
#     · `new_assembly` 会**换活动文档** ⇒ 指纹指向上一个零件。
#   两者都会让下一步的核对**误报漂移**（`ActivateDoc3(旧标题)` 抢不回 ⇒ 直接中止）。
#   ★ 这个盲区一直没暴露，是因为**别的探针都把 `save_as` 放在最后一步**
#     （2026-10-03 逐个探针数出来的：只有 asm_* 是中途 save_as）。
#
# ★ 判据（新增 op 时照这条问自己）：**这一步之后，活动文档的 title/path 可能变吗？**
#   会变 ⇒ 必须加进来。加进来是**安全**的，因为核对发生在**每一步执行之前**：
#   真漂移会在 `save_as` 之前就被拦下，所以"存完再钉"不会掩盖漂移。
_DOC_PIN_OPS = ("new_part", "new_assembly", "open", "save_as", "close")


def _doc_pin(sw) -> dict:
    """当前活动文档的"是哪一份"指纹：**只有 title / path**（见上面的取舍说明）。"""
    out: dict = {"title": "", "path": ""}
    with contextlib.suppress(Exception):
        m = sw.app.ActiveDoc
        if m is not None:
            out["title"] = str(sw_connect_mod().member(m, "GetTitle") or "")
            out["path"] = os.path.normcase(
                os.path.abspath(str(sw_connect_mod().member(m, "GetPathName") or "")))
    return out


def _check_doc_pin(sw, ctx: dict, sid: str) -> None:
    """每一步执行前核对"活动文档还是不是我们钉住的那一份"。

    漂移时先尝试按标题抢回（瞬时切换能救回来）；抢不回**直接中止** ——
    宁可这一步失败，也不能把特征加到用户的文件上、或把用户的文件存成我们的零件。
    """
    pin = ctx.get("_doc_pin")
    if not pin or ctx.get("_allow_doc_switch"):
        return
    if _doc_pin(sw) == pin:
        return
    got = _doc_pin(sw)
    with contextlib.suppress(Exception):
        if pin.get("title"):
            sw.app.ActivateDoc3(pin["title"], False, 0)
    if _doc_pin(sw) == pin:
        print(f"        ↳ 活动文档被切走过又抢回来了（{sid}）")
        return
    raise sw_connect_mod().SwError(
        "★ 活动文档已经不是本计划要建的那一份 —— **已中止，避免污染别人的模型**。\n"
        f"  期望：{pin}\n"
        f"  实际：{got}\n"
        "  原因：本脚本用的是**你正在用的那个 SolidWorks 会话**；期间有人/别的操作\n"
        "        切换了活动文档，而所有建模操作都挂在活动文档上。\n"
        "  ⇒ 解法：构建期间别在 SolidWorks 里切文档；或把任务交给一个干净的会话。\n"
        "  ⇒ 确认无风险时可用 --allow-doc-switch 关掉这条守卫（默认开）。")


def sw_connect_mod():
    """延迟 import：`execute()` 里也是延迟 import 的（避免 import 期就连 COM）。"""
    import sw_connect
    return sw_connect


def _auto_salvage_path(plan: dict, ctx: dict) -> str | None:
    """从中止的计划里派生抢救路径 = 计划里**最后一个 save_as** 的路径 + `_salvage` 后缀。

    为什么必须抢在进程退出前存盘：sw_plan 是 SolidWorks 的**自动化客户端**；进程一结束、
    COM 引用释放，**由本进程拉起的 SolidWorks 会随之退出**，内存里那份「建好了但还没
    走到 save_as」的模型就凭空消失。plan 里不写 save_as 时返回 None（这时只能靠 keep_open
    让文档留在 SW 里，由人来处理）。
    """
    for st in reversed(plan.get("steps", [])):
        if st.get("op") != "save_as":
            continue
        try:
            p = sw_ops._p(st, ctx, "path")
        except Exception:                                     # noqa: BLE001
            return None
        if not p or "{{" in str(p) or "}}" in str(p):
            return None
        p = str(p)
        for ext in (".SLDASM", ".SLDPRT", ".SLDDRW"):
            if p.upper().endswith(ext):
                return p[: -len(ext)] + "_salvage" + p[-len(ext):]
        return p + "_salvage"
    return None


def execute(plan: dict, report_path: str | None = None,
            stop_on_error: bool = True, keep_open: bool = True,
            allow_doc_switch: bool = False, salvage_out: str | None = None) -> dict:
    import sw_connect

    steps = plan["steps"]
    ctx: dict = {"params": plan.get("params", {}), "vars": {}, "outputs": {},
                 "feature_names": [],
                 # ★ 构建期文档指纹守卫（见上面的长注释）
                 "_doc_pin": None, "_allow_doc_switch": bool(allow_doc_switch)}
    log: list[dict] = []
    failed = None
    skipped: list[dict] = []        # ★ 声明了 optional 却失败的步（会进报告，绝不静默）

    sw = sw_connect.SwSession.open(visible=True)
    print(f"已连接 SolidWorks（主版本 {sw.version}，常量来源 {sw.C.source}）"
          + ("；文档切换守卫 **已关闭**（--allow-doc-switch）"
             if allow_doc_switch else "；文档切换守卫已开"))
    try:
        with sw.suppress_prompts():
            for i, step in enumerate(steps):
                sid = step.get("id") or f"#{i}"
                t0 = time.time()
                entry = {"id": sid, "op": step.get("op"), "params": {}}
                try:
                    # ★★ 每一步动手之前先核指纹（见 _check_doc_pin）。放在 try 里，
                    #   这样中止也会带 id/证据进报告，而不是抛到外面什么都没有。
                    _check_doc_pin(sw, ctx, sid)
                    entry["params"] = {k: v for k, v in step.items()
                                       if k not in CONTROL_KEYS}
                    result = sw_ops.run_op(sw, step, ctx)
                    # ★ 这一步决定了"我们要建的是哪一份文档" ⇒ 立刻钉住指纹
                    if step.get("op") in _DOC_PIN_OPS:
                        ctx["_doc_pin"] = _doc_pin(sw)
                        entry["doc_pin"] = dict(ctx["_doc_pin"])
                    entry["ok"] = True
                    entry["seconds"] = round(time.time() - t0, 3)
                    entry["result"] = _jsonable(result)
                    print(f"  [{i+1}/{len(steps)}] {sid:<24} {entry['op']:<24} "
                          f"OK  {entry['seconds']}s")
                except Exception as e:                          # noqa: BLE001
                    entry["ok"] = False
                    entry["seconds"] = round(time.time() - t0, 3)
                    entry["error"] = str(e)
                    spec = sw_ops.OPS.get(step.get("op"))
                    if spec:
                        entry["api_hint"] = list(spec.api)
                    if not isinstance(e, sw_connect.SwError):
                        entry["traceback"] = traceback.format_exc()
                    # ★★ `optional: "<理由>"`：这一步真机建不出来时**不算失败**，
                    #   但**必须**（a）在屏幕上打 WARN、（b）进报告的 `skipped_optional`、
                    #   （c）带上作者写的理由。三者缺一不可 —— 否则"可选"= 藏失败的暗门。
                    reason = step.get("optional")
                    if isinstance(reason, str) and reason.strip():
                        entry["optional_failed"] = True
                        entry["optional_reason"] = reason
                        skipped.append({"id": sid, "op": entry["op"],
                                        "reason": reason,
                                        "error": entry["error"].splitlines()[0]})
                        print(f"  [{i+1}/{len(steps)}] {sid:<24} {entry['op']:<24} "
                              f"SKIP(optional，{entry['seconds']}s)  "
                              f"{entry['error'].splitlines()[0]}")
                        print(f"        ↳ 理由：{reason.strip()}")
                    else:
                        failed = entry
                        print(f"  [{i+1}/{len(steps)}] {sid:<24} {entry['op']:<24} "
                              f"FAIL  {entry['error'].splitlines()[0]}")
                        if stop_on_error:
                            break
                log.append(entry)

            # ★★ 中止抢救（2026-10-07）：硬失败中止时，**先把当前文档存盘再收手**。
            #   为什么必须抢在退出前：本进程是 SolidWorks 的自动化客户端，进程一退出、
            #   COM 引用释放，**由本进程拉起的 SolidWorks 会随之退出**，内存里那份
            #   「建好了但还没走到 save_as」的模型就凭空消失 —— run13 白丢 43 分钟就是这么来的。
            #   路径：优先用显式 --salvage-out；否则自动取计划里最后一个 save_as 加 `_salvage`。
            if failed:
                _sp = salvage_out or _auto_salvage_path(plan, ctx)
                if _sp:
                    try:
                        sw_ops.op_save_as(sw, {"op": "save_as", "path": _sp,
                                               "overwrite": True}, ctx)
                        print(f"\n★ 中止抢救：当前文档已存到 {_sp}")
                    except Exception as e:                        # noqa: BLE001
                        print(f"\n★ 中止抢救失败：{e}\n"
                              "  ⇒ 文档仍在 SolidWorks 内存里，**先别关 SolidWorks**，"
                              "手工「另存为」保住成果再排查。")
    finally:
        if not keep_open:
            sw.close()

    out = {"schema": SCHEMA, "steps": len(steps), "executed": len(log),
           "failed": failed, "skipped_optional": skipped, "log": log,
           # ★ 构建期钉住的文档指纹（title/path）—— 出问题时第一眼看这个：
           #   它证明"这一批特征加在谁身上、并且存的是谁"
           "doc_pin": ctx.get("_doc_pin"),
           "doc_switch_guard": not allow_doc_switch,
           "gear_diagnostics": ctx.get("gear_diagnostics")}
    if report_path:
        # ★ 报告路径的父目录不存在时会直接抛异常 —— 那会让整个 --run 以非 0 退出，
        #   看起来像「建模失败」，其实模型已经建好并保存了。先建目录。
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n执行报告已写入 {report_path}")
    if skipped:
        print(f"\n★ 跳过了 {len(skipped)} 个**显式声明为可选**的步骤（模型已建，但这些特征缺失）：")
        for s in skipped:
            print(f"    · {s['id']}（{s['op']}）：{s['error']}\n      理由：{s['reason']}")
    if failed:
        print(f"\n失败于 {failed['id']}（{failed['op']}）：{failed['error']}")
        if failed.get("api_hint"):
            print("  该步的 API 序列：" + " → ".join(failed["api_hint"]))
        print("  SolidWorks 里可以 Alt+F8 录一个同样动作的宏来对照参数。")
    return out


def _jsonable(obj: Any) -> Any:
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


# ============================================================ 模板


def template(kind: str) -> dict:
    """给几种常用零件生成计划骨架。这是「AI 怎么写计划」的最短上手路径。"""
    if kind == "gear":
        return {
            "schema": SCHEMA,
            "meta": {"name": "直齿圆柱齿轮", "units": "mm",
                     "note": "整圈渐开线点表一次成形，不做草图阵列；参数改 mn/z 就能重建同族零件。"
                             "mode=polyline（默认）用折线内接真实渐开线，误差 <0.01mm，"
                             "z=55 时约 770 个图元；mode=spline 换成一条闭合样条（1 个图元、"
                             "曲线光滑）但插值会向外过冲约 0.2mm，只适合看效果。"
                             "包围盒用的是相对容差，0.01 → da=114 时允许 ±1.14mm，"
                             "足够覆盖「正对 +x 的采样点未必落在最大值处」这点偏差。"},
            "params": {"mn": 2.0, "z": 55, "b": 20.0, "alpha_n": 20.0, "x": 0.0,
                       "bore": 20.0, "keyway": "6x2.8",
                       "mode": "polyline", "n_flank": 6},
            "steps": [
                {"id": "n", "op": "new_part"},
                {"id": "p", "op": "select_plane", "plane": "Front"},
                {"id": "s1", "op": "sketch_begin"},
                {"id": "g", "op": "gear_profile",
                 "mn": "{{ params.mn }}", "z": "{{ params.z }}",
                 "beta": 0, "alpha_n": "{{ params.alpha_n }}", "x": "{{ params.x }}",
                 "mode": "{{ params.mode }}", "n_flank": "{{ params.n_flank }}",
                 "n_root": 2, "n_tip": 3,
                 "bore": "{{ params.bore }}",
                 "strict": True},
                {"id": "ck", "op": "check_diagnostics", "fail_on": "error"},
                {"id": "e1", "op": "sketch_end"},
                # 拉伸前先体检轮廓：图元数/类型分布/轮廓数。拉伸挂了先看这一步的输出。
                {"id": "cs", "op": "check_sketch"},
                {"id": "x1", "op": "extrude_boss", "depth": "{{ params.b }}",
                 "end_cond": "swEndCondMidPlane", "name": "GearBody"},
                # 键槽单独一张草图 + 单独切除。和孔画在一起会被判「轮廓自相交」。
                {"id": "p2", "op": "select_plane", "plane": "Front"},
                {"id": "s2", "op": "sketch_begin"},
                {"id": "kw", "op": "keyway_rect",
                 "bore": "{{ params.bore }}", "spec": "{{ params.keyway }}"},
                {"id": "s3", "op": "sketch_end"},
                {"id": "ks", "op": "check_sketch"},
                {"id": "k1", "op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth",
                 "name": "Keyway"},
                {"id": "rb", "op": "rebuild"},
                {"id": "v1", "op": "check_bbox",
                 "expect": ["{{ params.mn*(params.z+2) }}", "{{ params.mn*(params.z+2) }}",
                            "{{ params.b }}"], "tol": 0.01},
                {"id": "v2", "op": "check_feature_count", "min": 2},
                {"id": "sv", "op": "save_as", "path": str(out_dir() / "gear.SLDPRT"),
                 "overwrite": True},
            ],
        }
    if kind == "shaft":
        return {
            "schema": SCHEMA,
            "meta": {"name": "阶梯轴（旋转）", "units": "mm"},
            "params": {"segments": [{"d": 20, "l": 30}, {"d": 30, "l": 50},
                                    {"d": 20, "l": 30}]},
            "steps": [
                {"id": "n", "op": "new_part"},
                {"id": "p", "op": "select_plane", "plane": "Front"},
                {"id": "s1", "op": "sketch_begin"},
                {"id": "ax", "op": "centerline", "x1": 0, "y1": 0, "x2": 0, "y2": 130},
                {"id": "l1", "op": "line", "x1": 0, "y1": 10, "x2": 30, "y2": 10},
                {"id": "l2", "op": "line", "x1": 30, "y1": 15, "x2": 80, "y2": 15},
                {"id": "l3", "op": "line", "x1": 80, "y1": 10, "x2": 110, "y2": 10},
                {"id": "l4", "op": "line", "x1": 110, "y1": 10, "x2": 110, "y2": 0},
                {"id": "l5", "op": "line", "x1": 110, "y1": 0, "x2": 0, "y2": 0},
                {"id": "l6", "op": "line", "x1": 30, "y1": 10, "x2": 30, "y2": 15},
                {"id": "l7", "op": "line", "x1": 80, "y1": 15, "x2": 80, "y2": 10},
                {"id": "s2", "op": "sketch_end"},
                {"id": "rv", "op": "revolve_boss", "angle": 360},
                {"id": "v1", "op": "check_feature_count", "min": 2},
                {"id": "sv", "op": "save_as", "path": str(out_dir() / "shaft.SLDPRT"),
                 "overwrite": True},
            ],
        }
    if kind == "bolt":
        return {
            "schema": SCHEMA,
            "meta": {"name": "六角头螺栓 M8x65", "units": "mm",
                     "note": "头部用六边形拉伸+30°倒角；螺纹用装饰螺纹线，不画牙"},
            "params": {"s": 13.0, "k": 5.3, "L": 65.0, "head_h": 5.3, "d": 8.0},
            "steps": [
                {"id": "n", "op": "new_part"},
                {"id": "p1", "op": "select_plane", "plane": "Top"},
                {"id": "s1", "op": "sketch_begin"},
                {"id": "hex", "op": "polygon", "n": 6,
                 "r": "{{ params.s/2/cos(pi/6) }}"},
                {"id": "s2", "op": "sketch_end"},
                {"id": "h1", "op": "extrude_boss", "depth": "{{ params.k }}",
                 "name": "Head"},
                {"id": "p2", "op": "select_plane", "plane": "Top"},
                {"id": "s3", "op": "sketch_begin"},
                {"id": "cd", "op": "circle", "d": "{{ params.d }}"},
                {"id": "s4", "op": "sketch_end"},
                {"id": "h2", "op": "extrude_boss", "depth": "{{ params.L }}",
                 "end_cond": "swEndCondBlind", "reverse_dir": False, "name": "Shank"},
                {"id": "v1", "op": "check_feature_count", "min": 3},
                {"id": "sv", "op": "save_as", "path": str(out_dir() / "bolt.SLDPRT"),
                 "overwrite": True},
            ],
        }
    if kind == "flange":
        return {
            "schema": SCHEMA,
            "meta": {"name": "圆板法兰", "units": "mm"},
            "params": {"od": 160, "bore": 60, "t": 18, "bcd": 110,
                       "n": 6, "bolt_d": 13},
            "steps": [
                {"id": "n", "op": "new_part"},
                {"id": "p", "op": "select_plane", "plane": "Front"},
                {"id": "s1", "op": "sketch_begin"},
                {"id": "c1", "op": "circle", "d": "{{ params.od }}"},
                {"id": "s2", "op": "sketch_end"},
                {"id": "e1", "op": "extrude_boss", "depth": "{{ params.t }}",
                 "name": "Plate"},
                {"id": "p2", "op": "select_plane", "plane": "Front"},
                {"id": "s3", "op": "sketch_begin"},
                {"id": "c2", "op": "circle", "d": "{{ params.bore }}"},
                {"id": "s4", "op": "sketch_end"},
                {"id": "cut1", "op": "extrude_cut", "end_cond": "swEndCondThroughAll",
                 "reverse_dir": True, "name": "BoreCut"},
                {"id": "p3", "op": "select_plane", "plane": "Front"},
                {"id": "s5", "op": "sketch_begin"},
                {"id": "bc", "op": "circle", "cx": "{{ params.bcd/2 }}", "d": "{{ params.bolt_d }}"},
                {"id": "cp", "op": "sketch_circular_pattern", "count": "{{ params.n }}",
                 "cx": 0, "cy": 0, "angle": 360},
                {"id": "s6", "op": "sketch_end"},
                {"id": "cut2", "op": "extrude_cut", "end_cond": "swEndCondThroughAll",
                 "reverse_dir": True, "name": "BoltHoles"},
                {"id": "v1", "op": "check_bbox",
                 "expect": ["{{ params.od }}", "{{ params.od }}", "{{ params.t }}"]},
                {"id": "v2", "op": "check_feature_count", "min": 4},
                {"id": "sv", "op": "save_as", "path": str(out_dir() / "flange.SLDPRT"),
                 "overwrite": True},
            ],
        }
    if kind == "box":
        return {
            "schema": SCHEMA,
            "meta": {"name": "长方体（最小可运行示例）", "units": "mm"},
            "params": {"w": 80, "h": 40, "t": 10},
            "steps": [
                {"id": "n", "op": "new_part"},
                {"id": "p", "op": "select_plane", "plane": "Front"},
                {"id": "s1", "op": "sketch_begin"},
                {"id": "r1", "op": "rect_center", "w": "{{ params.w }}", "h": "{{ params.h }}"},
                {"id": "s2", "op": "sketch_end"},
                {"id": "e1", "op": "extrude_boss", "depth": "{{ params.t }}"},
                {"id": "v1", "op": "check_bbox",
                 "expect": ["{{ params.w }}", "{{ params.h }}", "{{ params.t }}"]},
                {"id": "v2", "op": "check_feature_count", "min": 2},
                {"id": "sv", "op": "save_as", "path": str(out_dir() / "box.SLDPRT"),
                 "overwrite": True},
            ],
        }
    if kind == "leadscrew":
        # 梯形螺纹丝杠。牙型按「中径处牙槽等宽」自洽构造，不做行业查表：
        #   牙高 H = 0.5P，两侧各 15° → 槽半宽从 P/4 起，向牙顶/牙底各偏 (P/4)·tan15°。
        # 于是 大径处槽半宽 = P/4 + (P/4)tan15°，小径处 = P/4 − (P/4)tan15°，
        # 换任何 P 都自动成立，牙型角恒为 30°。
        d, P = 24.0, 5.0          # Tr24×5
        Lth, dj, Lj = 200.0, 18.0, 40.0
        r_out = d / 2.0
        r_in = d / 2.0 - 0.5 * P                      # 牙高 = 0.5P
        t15 = math.tan(math.radians(15.0))
        half_o = P / 4.0 + (P / 4.0) * t15            # 大径处槽半宽
        half_i = P / 4.0 - (P / 4.0) * t15            # 小径处槽半宽
        r_ext = r_out + 1.0                           # 径向多切 1mm，保证切透圆柱面
        # ★ 牙型草图在「上视基准面」(XZ 平面)。SolidWorks 上视面的草图 +v 轴
        #   对应全局 −Z，而螺纹段起点在 z = −Lthread/2，所以牙型要画在 v = +Lthread/2。
        #   这个映射是实测出来的；画反了扫掠会找不到起始截面 —— inspect 一眼能看出。
        v0 = Lth / 2.0
        profile = [[r_ext, v0 - half_o], [r_out, v0 - half_o], [r_in, v0 - half_i],
                   [r_in, v0 + half_i], [r_out, v0 + half_o], [r_ext, v0 + half_o]]
        return {
            "schema": SCHEMA,
            "meta": {"name": "梯形螺纹丝杠", "units": "mm",
                     "note": "轴身 + 中间梯形螺纹段。螺纹走「螺旋线 → 沿螺旋线扫掠切除」，"
                             "和手工建模的步骤顺序一一对应；改 d/P/Lthread 就能重建同族零件。"
                             "牙型角恒 30°、牙高 0.5P，按中径处牙槽等宽构造。"
                             "螺旋线与牙型都从「螺纹段起点面」出发，两者天然对齐。"},
            "params": {"d": d, "P": P, "Lthread": Lth, "dj": dj, "Lj": Lj},
            "steps": [
                {"id": "n", "op": "new_part"},
                # ① 轴身 Ø18：整根，两侧对称（总长 = 螺纹段 + 两端轴头）
                {"id": "p1", "op": "select_plane", "plane": "Front"},
                {"id": "s1", "op": "sketch_begin"},
                {"id": "c1", "op": "circle", "d": "{{ params.dj }}"},
                {"id": "e1", "op": "sketch_end"},
                {"id": "x1", "op": "extrude_boss",
                 "depth": "{{ params.Lthread + 2 * params.Lj }}",
                 "end_cond": "swEndCondMidPlane", "name": "ShaftBody"},
                # ② 螺纹段起点面：从前视面往 −Z 偏 Lthread/2，螺纹段就落在
                #    z ∈ [−Lthread/2, +Lthread/2]，和前一步的两侧对称轴身同轴居中。
                {"id": "p2", "op": "select_plane", "plane": "Front"},
                {"id": "rp", "op": "ref_plane", "offset": "{{ params.Lthread / 2 }}",
                 "flip": True, "name": "螺纹起点面"},
                # ③ 螺纹段外圆 + 螺旋线 —— 共用同一张草图：
                #    ref_plane 会把新基准面选上，紧接着就能画草图
                {"id": "s2", "op": "sketch_begin"},
                {"id": "c2", "op": "circle", "d": "{{ params.d }}"},
                {"id": "e2", "op": "sketch_end"},
                {"id": "h1", "op": "helix", "pitch": "{{ params.P }}",
                 "height": "{{ params.Lthread }}", "name": "螺纹线"},
                # ★ 这里**必须**用给定深度（blind），不能用 swEndCondMidPlane：
                #   MidPlane 是「以草图平面为中心两侧各一半」，草图已经在螺纹段起点
                #   z=−Lthread/2 上了，再取中面会让整段跑到 z∈[−L,0]，螺纹段就偏到一端。
                {"id": "x2", "op": "extrude_boss", "depth": "{{ params.Lthread }}",
                 "end_cond": "swEndCondBlind", "name": "ThreadBlank"},
                # ④ 牙型草图（上视基准面 / XZ 平面里画径向-轴向截面）
                {"id": "p3", "op": "select_plane", "plane": "上视基准面"},
                {"id": "s3", "op": "sketch_begin"},
                {"id": "pf", "op": "polyline", "points": profile, "close": True},
                {"id": "e3", "op": "sketch_end"},
                {"id": "ks", "op": "check_sketch"},
                # ⑤ 沿螺旋线扫掠切除 = 螺纹
                {"id": "sw1", "op": "cut_swept", "name": "螺纹"},
                {"id": "rb", "op": "rebuild"},
                # 隐藏螺旋线和基准面 —— 不然几百圈的螺旋线会盖满螺纹，
                # 截图里根本看不清牙型（可见性会存进文件，是一次性做好的事）。
                {"id": "hr", "op": "hide_refs"},
                {"id": "v1", "op": "check_bbox",
                 "expect": ["{{ params.d }}", "{{ params.d }}",
                            "{{ params.Lthread + 2 * params.Lj }}"], "tol": 0.6},
                {"id": "v2", "op": "check_feature_count", "min": 4},
                {"id": "sv", "op": "save_as", "path": str(out_dir() / "leadscrew.SLDPRT")},
            ],
        }
    raise SystemExit(f"没有模板 '{kind}'。"
                     "可选：gear / shaft / bolt / flange / box / leadscrew")


# ============================================================ CLI


def main() -> None:
    ap = argparse.ArgumentParser(description="SolidWorks 建模计划的校验与执行")
    ap.add_argument("plan", nargs="?", help="计划 JSON 路径")
    ap.add_argument("--check", action="store_true", help="静态校验")
    ap.add_argument("--dry-run", action="store_true", help="校验 + 求值 + 打印 API 序列")
    ap.add_argument("--run", action="store_true", help="连上 SolidWorks 真执行")
    ap.add_argument("--report", default=None, help="执行报告输出路径（JSON）")
    ap.add_argument("--continue-on-error", action="store_true")
    ap.add_argument("--allow-doc-switch", action="store_true",
                    help="关掉「活动文档被切换就中止」的守卫（默认开；仅在确认无风险时用）")
    ap.add_argument("--salvage-out", default=None,
                    help="硬失败中止时把当前文档**抢救保存**到该路径。默认自动派生："
                         "取计划里最后一个 save_as 的路径加 `_salvage` 后缀。"
                         "★ 必须有：本进程是 SolidWorks 的自动化客户端，退出后 SW 会随之"
                         "退出，内存里没保存的模型会**直接消失**。")
    ap.add_argument("--quiet", action="store_true", help="不打印逐步 API 序列")
    ap.add_argument("--template", default=None, help="打印计划模板：gear/shaft/bolt/flange/box/leadscrew")
    ap.add_argument("--catalog", default=None, help="打印操作目录：doc/ref/sketch/feature/param/verify")
    a = ap.parse_args()

    if a.template:
        print(json.dumps(template(a.template), ensure_ascii=False, indent=2))
        return
    if a.catalog:
        for o in sorted([x for x in sw_ops.OPS.values() if x.cat == a.catalog],
                        key=lambda x: x.name):
            print(f"{o.name:24} {o.cn:18} {o.signature()}")
        return
    if not a.plan:
        ap.error("要么给计划文件，要么用 --template 打印骨架")

    plan = load_plan(a.plan)
    if a.run:
        r = validate(plan, evaluate=True)
        print_report(r, verbose=False)
        if not r.ok:
            print("\n静态校验没过，拒绝执行（先修计划，别去浪费 SolidWorks 的时间）")
            raise SystemExit(2)
        out = execute(plan, a.report, stop_on_error=not a.continue_on_error,
                      allow_doc_switch=a.allow_doc_switch, salvage_out=a.salvage_out)
        raise SystemExit(1 if out["failed"] else 0)

    if a.dry_run:
        r = validate(plan, evaluate=True)
        print_report(r, verbose=not a.quiet)
        raise SystemExit(0 if r.ok else 2)

    r = validate(plan, evaluate=True)
    print_report(r, verbose=not a.quiet)
    raise SystemExit(0 if r.ok else 2)


if __name__ == "__main__":
    main()
