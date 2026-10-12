#!/usr/bin/env python3
"""sw_spec.py —— 把《docs/07 SolidWorks建模-尺寸标注规范》变成**可机器校验**的规则。

为什么需要它
------------
**规范和实现会漂移，而漂移不会报错。**

文档写「旋转特征必须画中心线」，代码里可能少画一条；写「不是每个尺寸都标」
（尺寸链口径），可能被悄悄改回"逐段标"；写「每张草图退出前要确认完全定义」，
新写的生成器可能整个忘了这一步。这些都不会让 SolidWorks 报错 ——
只会让模型**看起来对、其实不可参数化**（改一个尺寸整根轴乱动，出工程图带不出尺寸）。

所以把规范里**可判定的部分**固化成规则，对**每一个生成器产出的计划**跑一遍。
离线、秒级、不开 SolidWorks。每条规则都注明它守的是规范哪一条。

规则的"合法性来源"分三类（与 `docs/07-*` §10 的分类一致）：
  · 官方口径 —— SolidWorks 帮助原文可查（如"旋转轮廓不能跨中心线"）
  · 项目口径 —— 本项目自定的更严约定（如"草图必须用真尺寸画"）
  · 工程口径 —— 实测踩出来的等价要求（如 §70 的尺寸链落点）

用法
----
    python scripts/sw_spec.py                  # 校验所有生成器（默认）
    python scripts/sw_spec.py --plan p.json    # 只校验一份计划
    python scripts/sw_spec.py --json           # 机读输出
    python scripts/sw_spec.py --list-rules     # 只列规则表
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# --------------------------------------------------------------------------- 规则表
#
# 每条 = (规则号, 规范出处, 一句话说明, 严重度)
#   严重度 err  = 违反即不合规，必须改
#   严重度 warn = 已知偏差，必须**写明口径**而不是假装没有
#
RULES: list[tuple[str, str, str, str]] = [
    ("S1", "§5 #3（官方：旋转需中心线）",
     "凡 revolve 的草图，草图内必须先画中心线（或用自带中心线的 shaft_profile）", "err"),
    ("S2", "§5 #10 / §11.3（项目口径）",
     "每张草图退出后必须有 check_sketch_constraint 闸门（**尺寸封存后**它的职责降级为"
     "「记录约束状态」，不再校验尺寸条数）", "err"),
    ("S3", "★ 2026-10-02 用户决策：**尺寸标注封存**",
     "计划里**禁止**出现尺寸标注 op（dim_auto / dimension / smart_dim / fully_define）"
     "—— 真机复现它们会弹模态确认框挂死自动化并改坏型面（sw_ops.SEALED_REASON）；"
     "尺寸改由后续「图纸绘制」环节对接", "err"),
    ("S4", "§4 #1 / §70（工程口径）",
     "【标注封存期间不触发】回转体半剖面禁止 dim_auto(chain=off) 逐段标"
     "（Σ 段长=总长 ⇒ 冗余）", "warn"),
    ("S5", "§70（工程口径）",
     "【标注封存期间不触发】回转体的 dim_auto(chain=auto) 必须排在 revolve 之后", "warn"),
    ("S6", "§5 #6（官方）",
     "旋转轮廓不能跨越中心线（所有 y 必须同号或为 0）", "err"),
    ("S7", "§5 #9（项目口径）",
     "草图必须锚住原点：至少一个图元点落在原点，或中心线过原点", "err"),
    ("S8", "§5 #4（官方；本项目有已知偏差）",
     "回转体的径向尺寸口径必须在计划里写明（半径 or 直径）—— 偏差要写在明处", "warn"),
]

RULE_INDEX = {r[0]: r for r in RULES}

# 这些 op 自己会画中心线（免得 lint 误判）
CENTERLINE_OPS = {"centerline", "shaft_profile"}
DIM_OPS = {"dim_auto", "dimension", "smart_dim", "fully_define"}
#: ★ 2026-10-02 起**已封存**的标注类 op（与 `sw_ops.OpSpec.sealed` 同口径）。
#: 计划里出现任何一个 ⇒ S3 违规。尺寸改由后续「图纸绘制」环节对接。
SEALED_DIM_OPS = {"dim_auto", "dimension", "smart_dim", "fully_define"}
REVOLVE_OPS = {"revolve_boss", "revolve_cut"}
GEOM_OPS = {"line", "arc3p", "arc_center", "polyline", "circle", "rect_center",
            "rect_corner", "spline", "polygon", "point"}
EPS = 1e-9
_EXPR = re.compile(r"\{\{\s*(.+?)\s*\}\}")


# --------------------------------------------------------------------------- 工具
def _eval_expr(expr: str, ns: dict):
    """求一个 `{{ }}` 里的纯算术表达式（禁用一切内建函数，与 sw_ops 同口径）。"""
    names = {k: v for k, v in ns.items()
             if isinstance(v, (int, float)) and not isinstance(v, bool)}
    try:
        return eval(expr, {"__builtins__": {}}, {**math.__dict__, **names})  # noqa: S307
    except Exception:                                     # noqa: BLE001
        return None


def _num(x, ns: dict | None = None):
    """把 `3.0` / `"{{ params.r }}"` / `"-params.r"` 统一成 float；读不到回 None。"""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str) and ns:
        m = _EXPR.fullmatch(x.strip())
        if m:
            return _eval_expr(m.group(1), ns)
    return None


def _point(pt, ns: dict) -> tuple | None:
    if not isinstance(pt, (list, tuple)) or len(pt) < 2:
        return None
    x, y = _num(pt[0], ns), _num(pt[1], ns)
    if x is None or y is None:
        return None
    return (x, y)


def sketch_geometry(step: dict, ns: dict) -> list[tuple[float, float]]:
    """把一步几何 op 的坐标点抽出来（用于 S6/S7）。"""
    op = step.get("op")
    pts: list[tuple[float, float]] = []
    if op == "line":
        for sx, sy in (("x1", "y1"), ("x2", "y2")):
            x, y = _num(step.get(sx), ns), _num(step.get(sy), ns)
            if x is not None and y is not None:
                pts.append((x, y))
    elif op == "centerline":
        for sx, sy in (("x1", "y1"), ("x2", "y2")):
            x, y = _num(step.get(sx), ns), _num(step.get(sy), ns)
            if x is not None and y is not None:
                pts.append((x, y))
    elif op in ("arc3p",):
        for key in ("p1", "p2", "p3"):
            p = _point(step.get(key), ns)
            if p:
                pts.append(p)
    elif op == "circle":
        cx, cy = _num(step.get("cx"), ns), _num(step.get("cy"), ns)
        if cx is not None and cy is not None:
            pts.append((cx, cy))
    elif op == "point":
        # ★ 坑（2026-10-04）：`point` 用的是 `x`/`y`（和 `points` 列表），**不是** `cx`/`cy`
        #   —— 原来把它和 `circle` 归为同一支按 `cx`/`cy` 读 ⇒ 点的坐标**永远读不到**
        #   ⇒ 拿它当原点锚时 S7 判「没锚住」。这里按 op 的**真实参数名**读。
        ps = step.get("points")
        if ps:
            for p in ps:
                q = _point(p, ns)
                if q:
                    pts.append(q)
        else:
            x, y = _num(step.get("x"), ns), _num(step.get("y"), ns)
            if x is not None and y is not None:
                pts.append((x, y))
    elif op == "polyline":
        extra = []
        for p in (step.get("points") or []):
            q = _point(p, ns)
            if q:
                pts.append(q)
            else:
                extra.append(p)
        # 兜底：点没解析出来就报"读不全"，由调用方跳过几何判定
        if extra and not pts:
            return []
        return pts
    elif op in ("rect_center", "rect_corner"):
        for key in ("cx", "cy"):
            pass
        vals = [_num(step.get(k), ns) for k in ("cx", "cy", "w", "h")]
        if op == "rect_corner":
            vals = [_num(step.get(k), ns) for k in ("x1", "y1", "x2", "y2")]
        if all(v is not None for v in vals):
            if op == "rect_center":
                cx, cy, w, h = vals
                pts = [(cx - w / 2, cy - h / 2), (cx + w / 2, cy - h / 2),
                       (cx + w / 2, cy + h / 2), (cx - w / 2, cy + h / 2)]
            else:
                x1, y1, x2, y2 = vals
                pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    elif op == "polygon":
        cx, cy = _num(step.get("cx"), ns), _num(step.get("cy"), ns)
        if cx is not None and cy is not None:
            pts = [(cx, cy)]
    return pts


# --------------------------------------------------------------------------- 分区
class Sketch:
    """一张草图的账：画了什么、标了什么、有没有中心线。"""

    def __init__(self, begin_index: int):
        self.begin = begin_index
        self.end: int | None = None
        self.name: str | None = None
        #: 被 `helix` 消费掉的草图（SW 会把螺旋线提取成独立特征并删除该草图）⇒ 不要求 sketch_end
        self.consumed_by_helix = False
        self.ops: list[dict] = []

    @property
    def drew_centerline(self) -> bool:
        return any(s.get("op") in CENTERLINE_OPS for s in self.ops)

    @property
    def dim_steps(self) -> list[dict]:
        return [s for s in self.ops if s.get("op") in DIM_OPS]


def _split(plan: dict) -> tuple[list[Sketch], list[dict]]:
    """按 sketch_begin / sketch_end 切出草图段；返回 (草图表, 全部步骤)。

    ★ 两个必须做对的地方（否则 lint 会误报）：
      1. **同名草图要合并成一条记录**。回转体半剖面必须按
         `… → sketch_end(X) → revolve → dim_auto → sketch_end(X)`
         的顺序写（`dim_auto(chain=auto)` 要先有圆柱面才认得出尺寸链，见 §70），
         于是同一张草图会被"关两次"。不合并就会误报「退出前没标注」。
      2. **`sketch_end` 之后不把 cur 置空** —— `revolve` / `dim_auto` 这些
         步骤逻辑上仍属于这张草图的流程，要归进去。
    """
    steps = plan.get("steps") or []
    sketches: list[Sketch] = []
    cur: Sketch | None = None
    for i, s in enumerate(steps):
        op = s.get("op")
        if op == "sketch_begin":
            cur = Sketch(i)
            sketches.append(cur)
        elif op == "helix":
            # ★ `helix` 在**草图内**插入时，SolidWorks 会把螺旋线提取成独立 Helix 特征
            #   并**把那张草图删掉**（2026-10-02 弹簧垫圈实测）。所以草图到这里就算"结束"，
            #   后面**不该**再要求 sketch_end —— 要求了会误报「草图没有 sketch_end」。
            if cur is not None:
                cur.consumed_by_helix = True
                cur.end = i
                sketches.append(cur)
                cur = None
        elif op == "sketch_end":
            if cur is not None:
                cur.end = i
                if s.get("name"):
                    same = next((r for r in sketches if r is not cur and r.name == s["name"]), None)
                    if same is not None:
                        same.ops.extend(cur.ops)
                        same.end = i
                        sketches.remove(cur)
                        cur = same
                    else:
                        cur.name = s["name"]
            # 注意：不置空 cur —— 后续 revolve/dim_auto 仍归这张草图
        elif cur is not None:
            cur.ops.append(s)
    return sketches, steps


# --------------------------------------------------------------------------- 校验
def lint_plan(plan: dict, part: str = "", scope: str = "part") -> list[dict]:
    """对一份计划跑全部规则，返回违规列表（空 = 合规）。

    `scope` 决定"这套规范管不管它"：
      · `"part"`（默认）= **交付件**（生成器产出的零件计划）⇒ S1~S8 全查。
      · `"probe"` = **API 行为回归样本**。探针的草图是"能隔离被测 op 的最小样本"，
        它存在的意义是验 op（倒角/阵列/拔模…），不是交付零件 ⇒ 只查与
        几何/口径强相关的 S1(中心线) / S4·S5(回转体尺寸链) / S6(跨中心线)。
        拿交付件的全套标注要求去套探针是**过严**，只会制造噪声。
    """
    ns: dict = {}
    ns.update(plan.get("params") or {})
    ns.update(plan.get("_derived") or {})
    sketches, steps = _split(plan)
    out: list[dict] = []
    only = None if scope == "part" else {"S1", "S4", "S5", "S6"}

    def bad(rule: str, msg: str):
        if only is not None and rule not in only:
            return
        rid, ref, _desc, sev = RULE_INDEX[rule]
        out.append({"part": part, "rule": rid, "spec": ref, "severity": sev, "msg": msg})

    # 索引：草图名 → 草图
    by_name = {s.name: s for s in sketches if s.name}

    # ---------------- S1 旋转前必须有中心线 ----------------
    for i, s in enumerate(steps):
        if s.get("op") not in REVOLVE_OPS:
            continue
        prof = s.get("profile")
        if prof:
            target = by_name.get(prof)
        else:
            # ★ 没写 profile ⇒ SolidWorks 用的是「**当前草图**」，也就是 revolve 之前
            #   紧邻的那张（`sketch_end` 退出时会把它选上，`_revolve/_extrude` 吃的就是
            #   当前选择）。**不能退化成 `sketches[-1]`** ——
            #   那是**整份计划的最后一张草图**：只要 revolve 之后还画了别的草图
            #   （典型：先回转出环、再选端面画「孔位」草图去打孔），就会拿错对象，
            #   误报「revolve 的草图没画中心线」。带孔盘族（§82）第一次跑就踩了。
            #   ① 先找"这条草图的 ops 里就含这个 revolve 步"的 —— 同名草图被关两次
            #      （回转体半剖面 `sketch_end → revolve → dim_auto → sketch_end`）时，
            #      它的 `end` 落在 revolve **之后**，按 end<i 筛会漏。
            target = next((sk for sk in sketches if any(st is s for st in sk.ops)), None)
            if target is None:
                cand = [sk for sk in sketches if sk.end is not None and sk.end < i]
                target = cand[-1] if cand else None
        if target is not None and not target.drew_centerline:
            bad("S1", f"revolve({s.get('name') or s.get('id')}) 用的草图 "
                      f"'{target.name}' 没有画中心线")
        elif target is not None and s.get("axis"):
            pass   # 显式给了外部轴，也算合规

    # ---------------- S2 / S3 每张草图的闸门与标注 ----------------
    for sk in sketches:
        if sk.end is None:
            if not sk.consumed_by_helix:
                bad("S2", f"草图 '{sk.name or '(未命名)'}' 没有 sketch_end")
            continue
        if sk.consumed_by_helix:
            continue                     # helix 消费掉的草图，不再要求后续闸门
        # 该草图**整条流程**结束之后的检查步骤（含 dim_auto 重开后那次 sketch_end）
        gates = [st for st in steps[sk.end + 1:]
                 if st.get("op") == "check_sketch_constraint"
                 and (st.get("sketch") == sk.name or sk.name is None)]
        if not gates:
            bad("S2", f"草图 '{sk.name}' 流程结束后没有 check_sketch_constraint 闸门")
        else:
            # ★ 尺寸封存后，闸门的职责只剩「记录约束状态」，所以**不再要求**
            #   expect_min_dims / expect_fully_defined —— 但仍要求闸门存在（S2），
            #   并且如果写了 expect_min_dims，值只允许 ≥0。
            for st in gates:
                n = st.get("expect_min_dims")
                if n is not None and int(n) < 0:
                    bad("S2", f"草图 '{sk.name}' 的 expect_min_dims={n} 非法（必须 ≥0）")
        # ★★ S3（重定义，2026-10-02）：尺寸标注**已封存** ⇒ 计划里出现标注 op 就是违规。
        #   （总扫在下面统一做，这里不再逐草图查。）

    # ---------------- S3（总扫）：整份计划禁止出现已封存的标注 op ----------------
    #
    # ★ 为什么**总扫**而不是只在草图的 ops 里找：封存是"能力级"的禁令，
    #   跟它挂在哪个草图无关 —— 哪怕有人把它塞在 revolve 之后、或塞进一张没闭合的
    #   草图段里，也必须拦下来。判据与 `sw_ops.OpSpec.sealed` 同口径（SEALED_DIM_OPS），
    #   执行期还有 `sw_ops.run_op` 的第二道闸门，这里是**计划期的第一道**。
    for st in steps:
        op = st.get("op")
        if op in SEALED_DIM_OPS:
            bad("S3", f"计划里出现**已封存**的标注步骤 `{op}`（step id={st.get('id')}）—— "
                      f"尺寸标注于 2026-10-02 封存：真机逐段标会弹模态确认框挂死自动化"
                      f"（RPC_E_DISCONNECTED）并改坏型面；"
                      f"尺寸改由后续「图纸绘制」环节对接。详见 sw_ops.SEALED_REASON。")

    # ---------------- S4 / S5 / S8 回转体的标注口径 ----------------
    #
    # ★ 本节**只在标注未封存时才有意义** —— 现在 dim_auto 已被 S3 拦死，
    #   所以这里的判据实际上不会触发。**保留而不删**的原因：重开标注那天，
    #   这些口径（chain=auto 才会读圆柱面、离轴矩形环会退化、径向口径要写明）
    #   一条都不能丢，它们是真金白银实测出来的。
    #   ⇒ 这里有一个**明示偏差**机制：
    #   `chain="auto"`（尺寸链）是 §70 的目标口径，但实测它在**离轴矩形环**
    #   （圆环/闷盖这类型面）上会退化 —— 计划算得出「总长 + 2 条半径」，
    #   实际只落上总长（`n_dimmed=1/3`，草图里剩一串重复的总长值），
    #   而 `chain="off"` 在同样型面上能落 4 条。
    #   ⇒ 这种"能力不足导致的回退"**不算违规，但必须写在明处**：
    #     在 step 上写 `deviation`（说明为什么 + 实测数字）。
    #     不写 = err（默默违规），写了 = 通过并作为 info 带出来。
    #   这一条守的是规范 §10 的原则：**偏差要标注，不能假装没有**。
    for i, s in enumerate(steps):
        if s.get("op") != "dim_auto":
            continue
        # 这张草图之后是否出现了 revolve（= 这是回转体半剖面）
        rev_after = [st for st in steps[i + 1:] if st.get("op") in REVOLVE_OPS]
        rev_before = [st for st in steps[:i] if st.get("op") in REVOLVE_OPS]
        chain = str(s.get("chain", "auto")).lower()
        is_revolve_profile = bool(rev_after) or bool(rev_before)
        dev = str(s.get("deviation") or "").strip()
        if is_revolve_profile and chain == "off" and not dev:
            bad("S4", f"回转体半剖面用了 chain=off（逐段标 ⇒ Σ 段长=总长 ⇒ 冗余），"
                      f"且**没写 `deviation` 说明**。要么改 chain=auto，"
                      f"要么把「为什么只能回退 + 实测数字」写在 step 的 deviation 里。"
                      f"step id={s.get('id')}")
        if is_revolve_profile and chain == "auto" and not rev_before:
            bad("S5", f"dim_auto(chain=auto) 排在 revolve **之前** —— 此时读不到圆柱面，"
                      f"会静默退回逐个标。step id={s.get('id')}")
        if is_revolve_profile and not str(s.get("note") or "").strip():
            bad("S8", f"回转体的标注没写径向口径（半径/直径）。"
                      f"官方要求套类标直径，本项目当前实现为半径 —— 必须写在明处。"
                      f"step id={s.get('id')}")

    # ---------------- S6 / S7 轮廓与原点 ----------------
    for sk in sketches:
        pts: list[tuple[float, float]] = []
        for st in sk.ops:
            if st.get("op") in GEOM_OPS:
                pts.extend(sketch_geometry(st, ns))
        if not pts:
            continue
        # ★ 判"这张草图是不是旋转轮廓"要看**这条记录自己的 ops**：
        #   同名合并之后 `sk.end` 落在 dim_auto 之后那次 sketch_end 上，
        #   再拿 `steps[sk.end+1:]` 找 revolve 就会**漏判**（反例 4 抓到的就是这个）。
        is_revolve_sk = any(st.get("op") in REVOLVE_OPS for st in sk.ops)
        ys = [p[1] for p in pts]
        if is_revolve_sk and ys:
            pos, neg = [y for y in ys if y > EPS], [y for y in ys if y < -EPS]
            if pos and neg:
                bad("S6", f"草图 '{sk.name}' 的旋转轮廓**跨越了中心线**"
                          f"（y 跨 {min(ys):g} ~ {max(ys):g}）")
        # S7 锚住原点
        anchored = any(abs(p[0]) <= EPS and abs(p[1]) <= EPS for p in pts)
        if not anchored:
            # ② 中心线锚定 —— **两种口径都认**：
            #    (a) 历史口径：中心线**躺在 x 轴上**（y1=y2=0）。项目里"轴上开槽/键槽"
            #        这类件就是把构造中心线贴着 y=0 画来满足 S7 的
            #        （见 gear_shaft 的 `键槽` 草图：`(108.75,0)-(138.75,0)`，
            #         note 明写"S7 锚定要求中心线落在 y=0 上"）⇒ **不能收紧，会误伤已交付件**。
            #    (b) 规则文本口径：中心线**过原点**（原点落在线段上）。
            #        ★ 坑（2026-10-04）：原实现只认 (a) ⇒ 把"竖直过原点的中心线"
            #          （典型：轴上退刀槽 `/assets/probes/revolve_cut.json` 的
            #          `(0,-25)-(0,25)`）判成没锚住，**实现比规则文本更严**，是假阳性。
            #        两种都只锁"位置/方向"，强度相当，都该放行 —— 取并集（严格超集，不新增失败）。
            for st in sk.ops:
                if st.get("op") != "centerline":
                    continue
                a = (_num(st.get("x1"), ns), _num(st.get("y1"), ns))
                b = (_num(st.get("x2"), ns), _num(st.get("y2"), ns))
                if None in a or None in b:
                    continue
                ax, ay = a
                bx, by = b
                if abs(ay) <= EPS and abs(by) <= EPS:      # (a) 躺在 x 轴上
                    anchored = True
                    continue
                dx, dy = bx - ax, by - ay                 # (b) 过原点
                seg2 = dx * dx + dy * dy
                if seg2 <= EPS:                           # 退化成一个点：它就在原点才算
                    if abs(ax) <= EPS and abs(ay) <= EPS:
                        anchored = True
                    continue
                if abs(ax * by - ay * bx) <= EPS * math.sqrt(seg2):   # 原点到直线距离≈0
                    t = (-ax * dx - ay * dy) / seg2                   # 垂足参数，须落在线段内
                    if -1.0 - 1e-9 <= t <= 1.0 + 1e-9:
                        anchored = True
        if not anchored:
            # ③ 草图内**以原点为中心的圆周阵列**也算锚定 —— 孔位草图就是这种：
            #    图元（孔）故意不落在原点上（要偏到分布圆上），但**阵列中心 = 草图原点**
            #    ⇒ 整张草图被原点钉死，旋转自由度也没了。这比"某个点恰好在原点"是
            #   更强的约束，不该判违规。
            #    ★ 不能用"再补一条过原点的中心线"糊过去：`centerline` 会进
            #      `ctx.segments`，被 `sketch_circular_pattern(select=created)` 一起阵列。
            for st in sk.ops:
                if st.get("op") == "sketch_circular_pattern":
                    cx = _num(st.get("cx", 0), ns)
                    cy = _num(st.get("cy", 0), ns)
                    if cx is not None and cy is not None \
                            and abs(cx) <= EPS and abs(cy) <= EPS:
                        anchored = True
        if not anchored:
            bad("S7", f"草图 '{sk.name}' 没有锚住原点（无图元点落在原点、中心线也不过原点、"
                      f"也没有以原点为中心的圆周阵列）")

    return out


def lint_all(params_path=None) -> dict:
    """跑全部生成器 + 全部探针计划，返回 {ok, n_part, violations, checked}。"""
    import sw_reducer

    P = sw_reducer.load(params_path)
    checked: list[str] = []
    violations: list[dict] = []
    # ★ **结构校验不需要真机标定值**：临时打开 calibrate。否则「标定值待重取」的零件
    #   （`expect_mass_g = 0`）会把整个 lint 直接拦死 —— 那是**运行期**守卫，
    #   不是计划结构错误。跑完立刻恢复，不污染调用方状态。
    _prev_cal = sw_reducer._CALIBRATE.get("on")
    sw_reducer._CALIBRATE["on"] = True
    try:
        for part in sorted(sw_reducer.BUILDERS):
            plan = sw_reducer.build_plan(P, part, out_dir="D:/out/reducer")
            checked.append(part)
            violations.extend(lint_plan(plan, part))
    finally:
        sw_reducer._CALIBRATE["on"] = _prev_cal
    # 探针计划（如果存在）：它们是"已验证可用"的样本，同样要合规
    probe_dir = Path(sw_reducer.__file__).resolve().parent.parent / "assets" / "probes"
    if probe_dir.is_dir():
        for f in sorted(probe_dir.glob("*.json")):
            try:
                plan = json.loads(f.read_text(encoding="utf-8"))
            except Exception:                                     # noqa: BLE001
                continue
            if not isinstance(plan, dict) or "steps" not in plan:
                continue
            checked.append(f"probe:{f.stem}")
            violations.extend(lint_plan(plan, f"probe:{f.stem}", scope="probe"))
    return {"ok": not violations, "n_part": len(sw_reducer.BUILDERS),
            "n_checked": len(checked), "checked": checked,
            "violations": violations}


def print_violations(vs: list[dict], verbose: bool = True) -> None:
    if not vs:
        print("  [PASS] 全部计划符合《07 尺寸标注规范》（S1~S8）")
        return
    by_rule: dict[str, list[dict]] = {}
    for v in vs:
        by_rule.setdefault(v["rule"], []).append(v)
    for rid in sorted(by_rule):
        rid_, ref, desc, sev = RULE_INDEX[rid]
        tag = "ERR " if sev == "err" else "WARN"
        print(f"  [{tag}] {rid_} · {ref}\n        规则：{desc}")
        if verbose:
            for v in by_rule[rid][:8]:
                print(f"        - [{v['part']}] {v['msg']}")
            if len(by_rule[rid]) > 8:
                print(f"        … 还有 {len(by_rule[rid]) - 8} 条")


def main() -> int:
    ap = argparse.ArgumentParser(description="《07 尺寸标注规范》一致性校验（离线）")
    ap.add_argument("--plan", help="只校验这一份计划 JSON（不给则跑所有生成器）")
    ap.add_argument("--params", help="参数表路径")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--list-rules", action="store_true", help="只列规则表")
    a = ap.parse_args()

    if a.list_rules:
        for rid, ref, desc, sev in RULES:
            print(f"{rid}  [{sev:4}] {ref}\n      {desc}")
        return 0

    if a.plan:
        plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
        vs = lint_plan(plan, Path(a.plan).stem)
        rep = {"ok": not vs, "n_part": 1, "n_checked": 1,
               "checked": [Path(a.plan).stem], "violations": vs}
    else:
        rep = lint_all(a.params)

    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"规范一致性校验（《07 尺寸标注规范》S1~S8）——"
              f"共 {rep['n_checked']} 份计划")
        print_violations(rep["violations"])
        n_err = sum(1 for v in rep["violations"] if v["severity"] == "err")
        n_warn = len(rep["violations"]) - n_err
        print(f"\n结论：{'通过 ✅' if rep['ok'] else '不合规 ❌'}"
              f"（硬失败 {n_err} / 提示 {n_warn}）")
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
