#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_ops.py —— 建模操作字典：把「人的动作」翻译成「SolidWorks API 调用序列」。

这是整个技能的核心。它回答三个问题：
    1. 有哪些操作可用（OPS 表，每个操作带中文名、参数、单位、前置条件）
    2. 每个操作内部到底调了哪些 API（OpSpec.api 字段，就是可执行的配方）
    3. 操作的**先后顺序**约束怎么表达（OpSpec.requires / effect，由 sw_plan 校验）

设计原则（踩坑换来的，别改）：
    · 一个 op = 一个有意义的机械动作（"拉伸凸台"），**不是**一个 API 调用。
      op 内部可以是一串 API + 必须的清理动作（清选择、改 AddToDB、重建）。
    · 前置条件写成声明式（requires="sketch_open"），这样 sw_plan 能在**没开 SolidWorks**
      的时候就校验出顺序错误 —— 这是「训练」阶段最省时间的能力。
    · 所有长度参数一律**毫米**，角度一律**度**；内部统一乘 mm()/deg()。
      SolidWorks API 底层是米和弧度，混用是头号 bug 来源。
    · 消耗品：每个 op 结束时必须把选择集清干净。SolidWorks 的选择是隐式全局状态，
      留着上次的选择会让下一个特征莫名其妙地失败。
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sw_connect  # noqa: E402  （导入安全：pywin32 只在函数内部 import）
import sw_gb  # noqa: E402  （GB 标准件尺寸表，纯函数，不依赖 SolidWorks）
import sw_gear  # noqa: E402
import sw_naming as sw_naming_mod  # noqa: E402  （命名权威表，纯函数）

mm = sw_connect.mm
deg = sw_connect.deg
SwError = sw_connect.SwError
member = sw_connect.member
typed = sw_connect.typed
FEATURE_ERROR_NAMES = sw_connect.FEATURE_ERROR_NAMES
variant_r8 = sw_connect.variant_r8


# ============================================================ 规格与注册表


@dataclass
class OpSpec:
    name: str
    cn: str                        # 中文名（给人看 & 给 AI 认）
    cat: str                       # doc / ref / sketch / feature / param / std / verify
    doc: str                       # 干什么、有什么坑
    api: tuple[str, ...] = ()      # 实际调用的 API（教学 + 排错用）
    params: dict[str, dict] = field(default_factory=dict)
    requires: tuple = ()            # 前置状态，见 STATES。
    #   ★★ 元素可以是**状态名字符串**，也可以是**元组 = OR 组**（组内至少满足一个）。
    #   例：`surface_planar` 的边界既可来自参考曲线、也可来自草图 ⇒
    #       `requires=(("has_curve", "has_sketch"),)`
    #   （2026-10-11 加：离线自检此前会把「用草图当边界」的探针判成「顺序错误」——
    #    因为 `requires` 只支持 AND。见 `missing_requires()`。）
    effect: tuple[str, ...] = ()    # 执行后进入的状态
    one_of: tuple[tuple[str, ...], ...] = ()   # 参数组：每组至少给一个
    fn: Callable | None = None
    verified: bool = False          # 是否已在本机 SolidWorks 上实测通过
    limits: tuple[str, ...] = ()    # ★「已知边界」：这个 op 明确做不了 / 有条件的地方。
    #   以前这些只写在 doc 正文里，Agent 读不到或读了不算数 ——
    #   `verified=True` 只说明"真机跑通过"，不说明"什么事都能干"。
    #   例：hole_wizard 建不出盲孔/螺纹孔；chamfer 只有 AngleDistance 一种真去料。
    #   导出到 assets/op_schema.json，供 `--schema` / 离线自检消费。
    # ★★ `sealed` = **已封存**：这个 op 的能力被判定为「当前阶段做不到」，
    #   主动关掉入口，防止它再被计划调起来。见 `SEALED_REASON` / `run_op`。
    sealed: bool = False
    sealed_reason: str = ""

    def signature(self) -> str:
        bits = []
        for k, v in self.params.items():
            ty = v.get("type", "any")
            unit = v.get("unit")
            mark = "" if v.get("required", True) else "?"
            bits.append(f"{k}{mark}: {ty}{'/' + unit if unit else ''}")
        return f"{self.name}({', '.join(bits)})"


OPS: dict[str, OpSpec] = {}

#: ★★ 已封存能力的**统一理由** —— 讲清「为什么关」「什么时候再开」。
#:
#: 背景（2026-10-02，用户决策 + 真机复现）：在**非单调回转型面**（端盖这类：
#: 法兰 → 台阶 → 轴承孔 → 毡圈槽 → 通孔，轴向进进出出）上逐段标注时，
#: SolidWorks 会**弹出模态确认框**（过定义 / 冲突尺寸的询问），自动化当场被挂住 ——
#: 实测报错 `RPC_E_DISCONNECTED`（被调用的对象已与其客户端断开连接），
#: 并且型面已经被尺寸驱动**改坏**（读出 10.0075 这种歪值、实体扭曲）。
#:
#: ⇒ 决策：**标注与建模解耦**。当前阶段只建几何 —— 型面回转**不标注**实测 100% 成功；
#:   尺寸改由后续「图纸绘制」环节用别的方式对接（在**工程图**里标，不在建模草图里标）。
#: ⇒ 重开条件：找到一条**不会弹模态框**的标注路径（例如先补几何关系再标、
#:   或拦截弹框），并在真机上把全部零件跑绿。
SEALED_REASON = (
    "尺寸标注已于 2026-10-02 **主动封存**（用户决策）。\n"
    "真机复现：在非单调回转型面（端盖：法兰→台阶→轴承孔→毡圈槽→通孔）上逐段标注时，"
    "SolidWorks 会弹**模态确认框**（过定义/冲突尺寸），自动化被挂死 —— "
    "实测 `RPC_E_DISCONNECTED`（对象已与客户端断开连接），且型面已被驱动改坏"
    "（读出 10.0075 歪值、实体扭曲）。\n"
    "⇒ 标注与建模**解耦**：先只建几何，尺寸改由后续**图纸绘制**环节对接"
    "（在工程图里标，不在建模草图里标）。\n"
    "⇒ 重开条件：找到不弹模态框的标注路径，并在真机上把全部零件跑绿。"
    "重开方式：step 里写 `allow_sealed: true`，或设环境变量 `YOYO_ALLOW_SEALED=1`。"
)


# ★ 分类表 = **单一来源**（顺序即手册里的章节顺序）。
#   为什么要提升成常量：以前 `catalog()` 里写死一份、`sw_selftest.py` 里又写死一份
#   白名单 ⇒ 加一个分类要改两处，漏改就自检报「分类不合法」。两份清单必然漂移。
#   `sw_selftest` 用 CATS 的键做白名单，`catalog()` 用 CATS 的顺序出章节。
CATS: tuple[tuple[str, str], ...] = (
    ("doc", "文档"),
    ("asm", "装配体"),
    ("ref", "参考几何"),
    ("sketch", "草图"),
    ("feature", "特征"),
    ("param", "参数化"),
    ("std", "标准件数据"),
    ("verify", "校验"),
)


# 状态机里用到的「世界状态」名词。op 的 requires/effect 只允许用这里的词。
STATES = {
    "doc_open": "有活动文档（零件或装配体）",
    "sketch_open": "正在编辑某个草图",
    "sketch_closed": "草图已关闭（可做特征）",
    "has_feature": "已有至少一个实体特征",
    "has_surface": "已有至少一个**曲面**体（与 has_feature **不是**一回事："
                   "曲面没有体积、没有质量，判据看曲面体数 + 面积）",
    "has_curve": "已有至少一条**参考曲线**（`curve_xyz` 建的 `REFERENCECURVES`）——"
                 "放样/扫掠的骨架。曲线**不是体**：既不是 has_feature 也不是 has_surface，"
                 "判据看**点坐标**（不是体积/面积）",
    # ★★ 2026-10-11 新增：初值 `sketch_closed` **一开始就为真**（`INITIAL_STATE`），
    #   所以它只表示「现在没有草图开着」，**不能**用来表达「已经画过草图」。
    #   把 `sketch_closed` 写进 requires 等于**没有约束**（假绿）。
    #   `has_sketch` 补上真正缺的那个状态：`sketch_end` 之后才为真。
    "has_sketch": "已有至少一条**已完成的草图**（`sketch_end` 之后置位）——"
                  "可作为封闭边界源、可被特征引用。"
                  "★ 与初值 `sketch_closed` **不同**：后者开局即为真，表达不了「画过草图」",
    "helix": "已有螺旋线（可沿它扫掠）",
    # ---- 装配体（D1）----
    "asm_open": "有活动**装配体**文档（`doc_open` 只管「有文档」，不区分零件/装配体）",
    "has_component": "装配体里已插入至少一个组件",
    "has_mate": "装配体里已有至少一个配合",
}
# ★ 装配体的三个状态**故意不写成互斥**（互斥表 `EXCLUSIVE` 在 `sw_plan.py`）：
#   `asm_open` / `has_component` / `has_mate` 是**累积**的 —— 插了组件依然是个
#   打开的装配体、加了配合依然有组件。写成互斥会让后面每一步都判「顺序错误」。


def flat_requires(requires) -> tuple[str, ...]:
    """把 `requires` 摊平成**所有**状态名（OR 组也摊平）。

    为什么要摊平：`requires` 允许写成
        `("doc_open", ("has_curve", "has_sketch"))`
    其中**嵌套元组 = OR 组**（至少满足一个）。做「状态名合法性」检查、
    「本 op 是否要求 sketch_open」这类判断时，都得先摊平，否则会漏看组里那些名字
    （或把元组本身当成状态名，直接判不合法）。
    """
    out: list[str] = []
    for item in requires or ():
        if isinstance(item, (tuple, list)):
            out.extend(str(x) for x in item)
        else:
            out.append(str(item))
    return tuple(out)


def missing_requires(requires, state) -> list:
    """返回**未满足**的 requires 项（字符串项 = 单个状态；元组项 = OR 组）。

    · 单个状态：不在 `state` 里 ⇒ 缺；
    · **OR 组**：组里**一个都不在** `state` 里 ⇒ 缺（只要有一个满足就放行）。
    返回的项保持原形（字符串 / 元组），交给调用方拼错误信息。
    """
    missing = []
    for item in requires or ():
        if isinstance(item, (tuple, list)):
            if not any(s in state for s in item):
                missing.append(tuple(item))
        elif item not in state:
            missing.append(item)
    return missing


def req_label(requires) -> str:
    """把 `requires` 渲染成一行给人看的文字（OR 组用「或」连接）。"""
    bits = []
    for item in requires or ():
        if isinstance(item, (tuple, list)):
            bits.append("(" + " 或 ".join(f"`{s}`" for s in item) + ")")
        else:
            bits.append(f"`{item}`")
    return "、".join(bits)


def op(name: str, cn: str, cat: str, doc: str, api=(), params=None,
       requires=(), effect=(), one_of=(), verified=False, limits=(),
       sealed=False, sealed_reason=""):
    def deco(fn):
        OPS[name] = OpSpec(name=name, cn=cn, cat=cat, doc=doc, api=tuple(api),
                           params=params or {}, requires=tuple(requires),
                           effect=tuple(effect), one_of=tuple(one_of),
                           fn=fn, verified=verified, limits=tuple(limits),
                           sealed=sealed, sealed_reason=sealed_reason)
        return fn
    return deco


# ============================================================ 表达式求值

class AttrDict(dict):
    """同时支持 p["mn"] 和 p.mn 两种写法。

    为什么需要：计划文件里写 `{{ params.mn }}` 比 `{{ params['mn'] }}` 可读得多，
    但 json 解出来是 dict，属性访问会炸。这层薄包装把报错也变清楚了 ——
    拼错参数名会直接告诉你 params 里有什么。
    """

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise SwError(
                f"参数 '{key}' 未定义。当前可用参数：{', '.join(sorted(self)) or '(空)'}"
            ) from None


def _eval_expr(expr: str, ctx: dict):
    """求值一个 `{{ }}` 里的表达式。只允许纯算术，禁用一切内建函数，
    避免计划文件变成任意代码执行入口。"""
    env = {"__builtins__": {}, "math": math}
    env.update({k: v for k, v in math.__dict__.items() if not k.startswith("_")})
    env["params"] = AttrDict(ctx.get("params", {}))
    env["P"] = env["params"]
    env["vars"] = AttrDict(ctx.get("vars", {}))
    try:
        return eval(expr, env)                            # noqa: S307 局部计划文件，已禁内建
    except SwError:
        raise
    except Exception as e:                                # noqa: BLE001
        raise SwError(f"表达式无法求值: {{{{{expr}}}}}  ({e})")


_EXPR_RE = re.compile(r"\{\{(.*?)\}\}", re.S)


def resolve(value: Any, ctx: dict) -> Any:
    """解析参数值。支持三种写法：

        {"depth": 20}                          → 原样
        {"depth": "{{ params.b }}"}             → 整值是一个表达式（保留类型：数字/列表）
        {"path": "{{ params.out }}/a.SLDPRT"}   → **嵌在字符串里**（拼成字符串）

    只允许纯算术，禁用一切内建函数，避免计划文件变成任意代码执行入口。

    ★ 早先只支持第二种：判据是 `value.startswith("{{") and value.endswith("}}")`，
      于是 `"{{ params.out }}/part.SLDPRT"` 这种**最常见的拼接写法直接不生效** ——
      它会把模板原样当路径传下去，保存时看着像成功、其实写到了名为
      `{{ params.out }}` 的目录（M1 探针实测踩到）。现在统一按出现次数替换：
      整个值就是唯一一个表达式 → 返回求值结果的**原类型**；否则 → 字符串拼接。
    """
    if isinstance(value, str) and "{{" in value:
        whole = _EXPR_RE.fullmatch(value.strip())
        if whole:
            return _eval_expr(whole.group(1).strip(), ctx)
        return _EXPR_RE.sub(lambda m: str(_eval_expr(m.group(1).strip(), ctx)), value)
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    return value


def _p(step: dict, ctx: dict, key: str, default: Any = None) -> Any:
    if key in step:
        return resolve(step[key], ctx)
    return default


# ============================================================ 辅助


def _model(sw):
    return sw.model


def _sm(sw):
    return sw.skm()


def _fm(sw):
    return sw.fm()


def _select_segment_ex(seg, append: bool = True) -> tuple:
    """选中一个草图图元，返回 `(Select4 的原始返回, 错误串或 None)`。

    ★ 与 `_select_segment` 分开的原因：`Select4` 会**抛异常**（不是返回假），
      而 `_select_segment` 把异常 `suppress` 掉之后，真机只剩一句「选不中」——
      指不到根因。凡是要给出可信报错的地方都走这个 `_ex` 版本。

    ★ 真机实测（2026-09-25，v1.4.3 `dim_auto` 探针）：
        `Select4` 回 **True 但 `GetSelectedObjectCount2(-1)` = 0** —— 返回值和
        实际选中数**可以不一致**。所以判「选上了没有」必须数选中数，
        不能信返回值（同 §并 `_segments_select` 的教训）。
    """
    if seg is None:
        return False, "图元对象是 None"
    try:
        return bool(typed(seg, "ISketchSegment").Select4(append, None)), None
    except Exception as e:                                    # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _select_segment(seg, append: bool = True) -> bool:
    """选中一个草图图元（布尔版，保留给不需要报错的调用点）。

    ★ 必须走 typed()，否则必踩「类型不匹配」。

    真机实测（10-04 探针 probe15）：
        seg.Select4(True, None)                      → com_error -2147352571 类型不匹配
        typed(seg, "ISketchSegment").Select4(True,None) → True，选中数 1

    原因：`CreateCircleByRadius` / `CreateLine` 返回的是**未绑定的 IDispatch**，
    pywin32 不知道 Select4 第 2 参是 IDispatch 指针，传 None 时按 VT_NULL 发过去，
    SolidWorks 直接回 DISP_E_TYPEMISMATCH。套上 sldworks.tlb 的 ITypeInfo 之后，
    pywin32 知道该参数是接口指针，才把 None 编组成 VT_DISPATCH(NULL)。
    这条和 reference 几何的 SelectByID2 是**同一个病根**（见 30-pitfalls）。
    """
    return _select_segment_ex(seg, append)[0]


def _live_segment(sw, ctx: dict, i: int):
    """按下标拿一个**活的**草图图元。

    ★ 为什么需要它：`CreateLine` / `CreateCircleByRadius` 返回的 IDispatch 在
      AddToDB 模式下会**变旧**，之后再 `Select4` / 读属性会抛
      `com_error -2147417848 (0x80010108) RPC_E_DISCONNECTED`
      ——「被调用的对象已与其客户端断开连接」（diag_dim7 实测）。
      从草图 **现取** 一份（`GetSketchSegments()`）就能继续用（diag_dim8 实测）。

    只在「图元总数一致 + 该位置类型一致」时才用现取的那份，
    否则说明下标空间对不上（例如草图 reset=False 混了上一张草图的图元），
    这时退回记录里的对象，避免张冠李戴。
    """
    segs = list(ctx.get("segments") or [])
    rec = segs[i] if 0 <= i < len(segs) else None
    live = None
    with contextlib.suppress(Exception):
        sk = typed(member(sw.model, "GetActiveSketch2"), "ISketch")
        live = list(member(sk, "GetSketchSegments") or [])
    if live and len(live) == len(segs) and 0 <= i < len(live):
        t_rec = None
        with contextlib.suppress(Exception):
            t_rec = member(rec, "GetType")
        t_live = None
        with contextlib.suppress(Exception):
            t_live = member(live[i], "GetType")
        if t_rec is None or t_live is None or int(t_rec) == int(t_live):
            return live[i]
    return rec


def _refresh_view(sw) -> None:
    """把视口刷一遍。

    ★ 为什么标注前必须刷：AddToDB 模式下新建的草图图元，SolidWorks 可能**还没把
      它们纳入可选集合**。实测（v1.4.3：diag_dim6/7/9 三份取证）：
        同一个「建 2 个圆 → 选第 1 个」，**紧挨着建完就选**会得到
        「`Select4` 回 True 但选中数 0」或者 `RPC_E_DISCONNECTED (0x80010108)`；
        中间隔一次读几何 / 刷视图（几十毫秒）就正常。
      ⇒ 这是个**时序竞态**，不是 API 用错。所以标注前主动刷一次，不靠运气。
    """
    with contextlib.suppress(Exception):
        member(sw.view(), "GraphicsRedraw2")


def _select_one_strict(sw, ctx: dict, i: int, attempts: int = 4) -> tuple:
    """选中下标 i 的图元，**以实际选中数为准**，带「刷视图 + 重试」。
    返回 `(成功?, 尝试日志, 用到的对象)`。

    轮询顺序：每轮先试「记录的对象」，再试「从草图现取的对象」；
    两者都没让 `GetSelectedObjectCount2(-1) == 1` 就 `GraphicsRedraw2` +
    短等待，再进下一轮。全部失败才返回 False，并把**真实错误**（`Select4`
    的返回、异常、实际选中数）带出去 —— 而不是一句指不到根因的「选不中」。
    """
    rec_segs = list(ctx.get("segments") or [])
    rec = rec_segs[i] if 0 <= i < len(rec_segs) else None
    tried: list[str] = []
    for k in range(max(1, attempts)):
        live = _live_segment(sw, ctx, i)
        seen: list[int] = []
        for tag, seg in (("记录", rec), ("现取", live)):
            if seg is None or id(seg) in seen:
                continue
            seen.append(id(seg))
            sw.clear_selection()
            ok, err = _select_segment_ex(seg, append=False)
            n = sw.selected_count()
            if n == 1:
                return True, f"第{k + 1}轮 {tag}: 选中 1", seg
            tried.append(f"第{k + 1}轮 {tag}: Select4→{ok} err={err} 选中={n}")
        _refresh_view(sw)
        time.sleep(0.15)
    return False, "；".join(tried[-8:]), None


def _segments_select(sw, segs: list) -> int:
    """把之前创建的草图图元选上（草图阵列/镜像/圆角要用）。返回**实际**选中个数。

    ★ 两个坑叠在一起，早先让草图圆周阵列一直报「种子图元没选中」：
      1. 图元是未绑定的 IDispatch，不套 typed() 直接 Select4 会抛「类型不匹配」；
      2. Select4 的**返回值不可信**（晚期绑定下常回 None），必须用
         SelectionManager.GetSelectedObjectCount2(-1) 数。
    """
    for s in segs:
        _select_segment(s, append=True)
    if not segs:
        return 0
    n = sw.selected_count()
    if n == 0:
        # 兜底：有些图元（构造线/样条）可能不是 ISketchSegment，
        # 换成基接口 ISketchSegment 之外再试一次 Select4。
        for s in segs:
            with contextlib.suppress(Exception):
                typed(s, "ISketchSegment").Select4(True, None)
        n = sw.selected_count()
    return n


def _select_feature(sw, name: str, mark: int) -> bool:
    """按名字选中一个特征（草图或实体特征），成功返回 True。

    ★ 绝对不要用 append=True：SolidWorks 的 SelectByID2 在 append 模式下是
      **切换**语义 —— 已经选中的对象会被取消选中。这正是「刚画完草图，
      紧接着拉伸却报『选不中草图』」的根因。统一走 sw.select()（append=False）。
    """
    for typ in ("SKETCH", "BODYFEATURE"):
        if sw.select(name, typ, mark=mark):
            return True
    return False


# ============================================================ 文档类


@op("new_part", "新建零件", "doc",
    "用默认模板新建一个零件文档。模板必须是已配置好的 .prtdot，"
    "否则 SolidWorks 会弹「选择模板」对话框卡死脚本。",
    api=["ISldWorks.NewDocument", "ISldWorks.SetUserPreferenceIntegerValue"],
    params={"template": {"type": "str", "required": False, "desc": "prtdot 路径，留空用系统默认"},
            "units": {"type": "str", "required": False, "default": "mm"}},
    effect=("doc_open", "sketch_closed"), verified=True)
def op_new_part(sw, step, ctx):
    tpl = _p(step, ctx, "template")
    m = sw.new_part(tpl)
    ctx["feature_names"] = []
    return {"title": member(m, "GetTitle")}


@op("open", "打开已有零件", "doc", "打开 .SLDPRT/.SLDASM，后续用参数化改型而不是重建。",
    api=["ISldWorks.OpenDoc6"],
    params={"path": {"type": "str", "desc": "文件路径"},
            "read_only": {"type": "bool", "required": False, "default": False}},
    effect=("doc_open",), verified=True)
def op_open(sw, step, ctx):
    m = sw.open_doc(_p(step, ctx, "path"), bool(_p(step, ctx, "read_only", False)))
    ctx["opened"] = member(m, "GetPathName")
    return {"path": member(m, "GetPathName"), "title": member(m, "GetTitle")}


@op("save_as", "另存为", "doc",
    "另存到新路径。默认不覆盖（overwrite=False）更安全，避免把用户已有零件冲掉。",
    api=["IModelDocExtension.SaveAs"],
    params={"path": {"type": "str", "desc": "目标路径"},
            "overwrite": {"type": "bool", "required": False, "default": False}},
    requires=("doc_open",), verified=True)
def op_save_as(sw, step, ctx):
    # ★ 默认**覆盖**：save_as 的语义就是「把我的产物写到这个路径」，
    #   同族零件反复重建会落在同一个文件名上，默认不覆盖会让第二次必失败
    #   （「目标已存在（overwrite=False）」看着像建图失败，其实只是没覆盖）。
    #   想保护旧文件就显式写 "overwrite": false。
    path = _p(step, ctx, "path")
    if not path:
        raise SwError("save_as 需要 path")
    # ★ 守住「未解析的表达式变成字面量路径」这道坑：早先 resolve() 只处理整值
    #   `{{ x }}`、不处理内嵌 `"{{ out }}/a.SLDPRT"`，于是磁盘上真长出一个
    #   名为 `{{ params.out }}` 的目录，而计划看着「跑到 save 才失败」。
    if "{{" in str(path) or "}}" in str(path):
        raise SwError(
            f"save_as 的 path 里还有没解析掉的表达式：{path!r}\n"
            "  ① `{{ }}` 里的参数名拼错了？② 该参数没定义？"
            "先在计划里把路径写对，别让它在磁盘上生成字面量目录。")
    # ★ 目标目录不存在就建（与 `screenshot` 保持一致）—— 否则「SaveAs 到一个新文件夹」
    #   会直接失败，而计划作者只看到「保存失败」，误以为是建模问题。
    with contextlib.suppress(Exception):
        Path(str(path)).parent.mkdir(parents=True, exist_ok=True)
    p = sw.save_as(path, bool(_p(step, ctx, "overwrite", True)))
    out = {"saved": p}
    # ★★ 2026-10-03 修（真机假警告）：`bodies()` 是**零件** API，对 .SLDASM **恒为 0**。
    #   原来对装配体也报"一个实体都没有" ⇒ 用户以为保存失败，其实是**装配体本来就没有实体**。
    #   ⇒ 判据分文档类型：零件看**实体数**，装配体看**组件数**。
    if getattr(sw, "last_save_is_asm", False):
        n_comp = 0
        with contextlib.suppress(Exception):
            n_comp = sw.component_count()
        out["components"] = n_comp
        if n_comp == 0:
            out["warn_no_body"] = (
                "保存的装配体里**一个组件都没有** —— 只建了基准面/草图？"
                "用 sw_inspect.py --tree 打开看一眼，别直接交付。"
                "（★ 装配体没有\"实体\"概念，别拿 bodies 数它。）")
    else:
        n_body = getattr(sw, "last_save_bodies", None)
        if n_body is not None:
            out["bodies"] = n_body
            if n_body == 0:
                out["warn_no_body"] = (
                    "保存的零件里**一个实体都没有** —— 只有草图/曲线？"
                    "用 sw_inspect.py 打开看一眼，别直接交付。")
    return out


@op("close", "关闭文档", "doc", "关闭当前文档，可选保存。",
    api=["ISldWorks.CloseDoc"], params={"save": {"type": "bool", "required": False, "default": False}},
    requires=("doc_open",), verified=True)
def op_close(sw, step, ctx):
    sw.close_doc(bool(_p(step, ctx, "save", False)))
    return {"closed": True}


@op("rebuild", "重建", "doc", "强制重建。每批特征之后重建一次，能尽早暴露约束冲突。",
    api=["IModelDoc2.ForceRebuild3", "IModelDoc2.EditRebuild3"], requires=("doc_open",), verified=True)
def op_rebuild(sw, step, ctx):
    return {"rebuilt": sw.rebuild(force=bool(_p(step, ctx, "force", False)))}


@op("zoom_fit", "整屏显示", "doc", "把模型缩放到填满视图（截图/给人看之前调）。",
    api=["IModelDoc2.ViewZoomtofit2"], requires=("doc_open",), verified=True)
def op_zoom_fit(sw, step, ctx):
    sw.zoom_fit()
    return {}


@op("screenshot", "截图（视口存盘）", "verify",
    "把**当前视口**存成图片（BMP，可自动转 PNG）。\n"
    "★ 这是「数值吵不出结论就看图」这条铁律的落地工具，也是**尺寸/标注这类"
    "纯视觉问题唯一的自动证据**：\n"
    "  `dimension` 曾经功能正常（包围盒会变）但文字压在原点上「看不见」，\n"
    "  任何数值断言都抓不到，只有截图能证伪。探针里放一张，人才看得到。\n"
    "★ 用 `IModelDoc2.SaveBMP`：直接抓视口，**不需要激活窗口、不截屏**，\n"
    "  headless 跑脚本时也能用。\n"
    "★ 想让人看清标注，先 `zoom_fit`，必要时再 `rebuild`。",
    api=["IModelDoc2.SaveBMP"],
    params={"path": {"type": "str", "desc": "输出路径（建议 .bmp；会自动另存一份 .png）"},
            "width": {"type": "int", "required": False, "default": 1400},
            "height": {"type": "int", "required": False, "default": 1000},
            "png": {"type": "bool", "required": False, "default": True,
                    "desc": "是否顺带转 PNG（需要 Pillow；失败不影响 BMP）"},
            "crop": {"type": "bool", "required": False, "default": True,
                     "desc": "转 PNG 时自动裁掉四周空白（按结构边缘判，白底白模也能裁）"}},
    requires=("doc_open",), verified=True)
def op_screenshot(sw, step, ctx):
    path = str(_p(step, ctx, "path"))
    w = int(_p(step, ctx, "width", 1400) or 1400)
    h = int(_p(step, ctx, "height", 1000) or 1000)
    p = Path(path)
    if not p.is_absolute():
        p = Path(os.getcwd()) / p
    p.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(Exception):
        p.unlink()
    r, err = _call(member, sw.model, "SaveBMP", str(p), w, h)
    if not p.exists():
        raise SwError(f"截图失败（SaveBMP 没落盘）: {p}；返回 {r!r}，错误 {err}")
    out = {"bmp": str(p), "bytes": p.stat().st_size, "size": [w, h]}
    if _p(step, ctx, "png", True):
        with contextlib.suppress(Exception):
            from PIL import Image                                # noqa: PLC0415
            # 局部 import 避免 sw_ops ↔ sw_shot 循环依赖（sw_shot 顶部 import 本模块）
            try:
                from sw_shot import autocrop_content             # noqa: PLC0415
            except Exception:                                    # noqa: BLE001
                autocrop_content = None
            png = p.with_suffix(".png")
            with Image.open(p) as im:
                img = im.convert("RGB")
                if autocrop_content is not None and _p(step, ctx, "crop", True):
                    img = autocrop_content(img)
                img.save(png)
            out["png"] = str(png)
    return out


# ============================================================ 参考几何


@op("select_plane", "选基准面", "ref",
    "选中前视/上视/右视基准面（或任意命名的基准面/平面）。"
    "选完不直接画图，留给 sketch_begin 用 —— SolidWorks 的「在面上建草图」就是靠当前选择。",
    api=["IModelDocExtension.SelectByID2"],
    params={"plane": {"type": "str", "default": "Front",
                      "desc": "Front/Top/Right 或基准面名，也可填 'm:1' 这种已有面"},
            "mark": {"type": "int", "required": False, "default": 0}},
    requires=("doc_open",), verified=True)
def op_select_plane(sw, step, ctx):
    which = _p(step, ctx, "plane", "Front")
    ok = sw.select_plane(which, int(_p(step, ctx, "mark", 0) or 0))
    if not ok:
        raise SwError(f"选不中基准面 '{which}'。中文界面请用「前视基准面」，"
                      f"英文界面用 'Front Plane'；自定义模板可能改了名字。")
    return {"selected": which}


@op("select_face", "选实体面", "ref",
    "按**全局坐标射线**选中实体的一个面（也可以选边） —— 面通常没名字，只能按坐标选。\n"
    "选完留给 sketch_begin 用：`select_face(point=[...])` → `sketch_begin` 就在这个面上开草图。\n"
    "★ 这是**异型孔向导的正确前置**：孔位草图必须开在要打孔的**面**上，\n"
    "  开在穿过实体内部的基准面（如前视面）上，HoleWizard5 会静默不生成特征。\n"
    "point 必须是**全局坐标**（不是草图坐标），取在目标面**内部**、别落在棱上。",
    api=["IModelDocExtension.SelectByID2", "IModelDocExtension.SelectByRay"],
    params={"point": {"type": "list", "required": False,
                      "desc": "面上的一个全局坐标点 [x,y,z]（mm）"},
            "name": {"type": "str", "required": False, "desc": "显式面名（可选，一般不需要）"},
            "type": {"type": "str", "required": False, "default": "FACE",
                     "desc": "FACE（默认）/ EDGE"},
            "mark": {"type": "int", "required": False, "default": 0}},
    requires=("doc_open",), verified=True)
def op_select_face(sw, step, ctx):
    item = {"type": str(_p(step, ctx, "type", "FACE") or "FACE").upper()}
    nm = _p(step, ctx, "name")
    pt = _p(step, ctx, "point")
    if nm:
        item["name"] = nm
    if pt:
        item["point"] = pt
    if not nm and not pt:
        raise SwError("select_face 需要 point（面上的全局坐标 [x,y,z]）或 name（面名）")
    mark = int(_p(step, ctx, "mark", 0) or 0)
    # 先清选：SelectByRay/SelectByID2 的 append 语义会让上一个选择残留
    sw.clear_selection()
    if not _pick(sw, item, mark=mark, append=False):
        raise SwError(
            f"选不中这个{'面' if item['type'] == 'FACE' else '边'}：{item}。\n"
            "  排查：① point 是**全局坐标**吗（不是草图坐标）\n"
            "        ② 它真的落在实体的那个面内部吗（别落在棱上/实体外）\n"
            "        ③ 实体建出来了吗（先 check_diagnostics 看一眼）")
    return {"type": item["type"], "point": pt, "name": nm, "mark": mark}


@op("plane_offset", "偏移基准面", "ref",
    "从某个面偏移一个距离新建基准面。用于「在距底面 15mm 处开始拉伸」这类需求。",
    api=["IModelDocExtension.SelectByID2", "IModelDoc2.CreatePlaneAtOffset3"],
    params={"base": {"type": "str", "default": "Front"}, "offset": {"type": "float", "unit": "mm"},
            "flip": {"type": "bool", "required": False, "default": False},
            "name": {"type": "str", "required": False, "desc": "新建面的名字，便于后面引用"}},
    requires=("doc_open",), verified=True)
def op_plane_offset(sw, step, ctx):
    base = _p(step, ctx, "base", "Front")
    if not sw.select_plane(base):
        raise SwError(f"偏移基准面的参考选不中: {base}")
    # ★★ **负偏移必须转成 `Flip`**（2026-10-02 真机踩到）：
    #   `CreatePlaneAtOffset3(Offset, Flip, AutoSize)` 里 **Offset 是距离量**（API 只要正值），
    #   方向由 `Flip` 控制 ⇒ 传负数**直接返回 None**，而日志只会说「返回空」，
    #   完全看不出原因（键槽面 `Top 偏 −9` 就这么挂的，卡了 19/35）。
    #   ⇒ 本 op 现在接受**有符号偏移**：负号自动折成「abs + 翻转 Flip」。
    #     （旧调用方全是正偏移 ⇒ 行为逐位不变，不会牵连已建件。）
    raw = float(_p(step, ctx, "offset") or 0.0)
    flip = bool(_p(step, ctx, "flip", False))
    if raw < 0:
        raw, flip = -raw, not flip
    # AutoSize=True：面自动缩放，省得算尺寸
    plane = sw.model.CreatePlaneAtOffset3(mm(raw), flip, True)
    if plane is None:
        raise SwError(
            f"CreatePlaneAtOffset3 返回空（base={base} offset={raw:g} flip={flip}）。\n"
            "  排查：① Offset 必须是**正距离**（负号要转 Flip；本 op 已自动转，"
            "若仍报错说明另有原因）\n"
            "        ② base 基准面是否真的存在（新建零件默认有 Front/Top/Right 三基准面）\n"
            "        ③ 上一步是布尔/拉伸的话，是否需要先 Rebuild 再建面")
    name = _p(step, ctx, "name")
    if name:
        with contextlib.suppress(Exception):
            plane.Name = name
    sw.clear_selection()
    return {"plane": name or "Plane", "base": base, "offset_mm": raw, "flip": flip}


@op("axis_from_planes", "两平面建基准轴", "ref",
    "用两个基准面的交线建基准轴。圆周阵列需要一个**可命名的**轴向参考，"
    "临时轴选不中，所以显式建一根最稳。齿轮/法兰绕中心阵列就走这个。",
    api=["IModelDocExtension.SelectByID2", "IModelDoc2.InsertAxis2"],
    params={"plane1": {"type": "str", "default": "Top"}, "plane2": {"type": "str", "default": "Right"},
            "name": {"type": "str", "required": False, "default": "Axis1"}},
    requires=("doc_open",), verified=True)
def op_axis_from_planes(sw, step, ctx):
    p1 = _p(step, ctx, "plane1", "Top")
    p2 = _p(step, ctx, "plane2", "Right")
    want = _p(step, ctx, "name", "Axis1") or "Axis1"
    sw.clear_selection()
    if not sw.select_plane(p1):
        raise SwError(f"选不中 {p1}")
    if not sw.select(p2, "plane", append=True):
        raise SwError(f"选不中 {p2}")
    tree0 = {n for _t, n in sw.feature_names()}
    ok = sw.model.InsertAxis2(True)
    sw.clear_selection()
    if not ok:
        raise SwError("InsertAxis2 失败（两个平面必须相交且不平行）")
    # ★ 真机实测（2026-09-25，M2）：InsertAxis2 **不会**照你给的名字命名 ——
    #   它按界面语言自动叫「基准轴1」/「Axis1」。原来直接 `return {"axis": name}`
    #   是**谎报**，于是 circular_pattern / linear_pattern 拿 "Axis1" 去 SelectByID2
    #   必然选不中（中文界面下真名是「基准轴1」）。这里像 plane_offset 一样
    #   找到新建的 RefAxis 并**真的改名**，返回值也以真名为准。
    real = want
    for t, nm in sw.feature_names():
        if t == "RefAxis" and nm not in tree0:
            with contextlib.suppress(Exception):
                typed(sw.model.FeatureByName(nm), "IFeature").Name = want
            real = want if want else nm
            break
    if not _select_ref_axis(sw, real):
        # 改不到名也别让计划挂在这里 —— 至少报出来真名是什么
        real = _first_ref_axis_name(sw) or real
    sw.clear_selection()
    return {"axis": real, "requested": want}


# ============================================================ 草图类


@op("sketch_begin", "开始草图", "sketch",
    "进入草图编辑。若当前已选中基准面/平面，就在它上面建草图；"
    "没选就报错（不猜，猜错=画在错误的面上，后面全废）。\n"
    "进草图后默认打开 AddToDB（直接写库，速度差一个量级）。\n"
    "★★ 关于 `add_to_db`：**它不影响约束，别指望它拿到自动约束**。\n"
    "  真机实测（2026-09-25，M3）：同一个 `CreateCornerRectangle`，\n"
    "  AddToDB=True 与 False 画出来的草图 `GetConstrainedStatus` **完全一样**\n"
    "  （都是 2 UnderConstrained），尺寸数也都是 0。\n"
    "  所以想要「带尺寸约束的草图」，正确做法是画完调 `fully_define`，\n"
    "  或者用 `dimension` 显式标注 —— 不是切这个开关。\n"
    "  留这个参数只为个别需要靠推断吸附（把新端点粘到已有几何上）的场景。",
    api=["ISketchManager.InsertSketch", "ISketchManager.AddToDB", "ISketchManager.DisplayWhenAdded"],
    params={"plane": {"type": "str", "required": False, "desc": "不给则用当前选择"},
            "reset": {"type": "bool", "required": False, "default": True,
                      "desc": "是否先清空画图元记录"},
            "add_to_db": {"type": "bool", "required": False, "default": True,
                          "desc": "直接写库（快）。实测**不影响自动约束**，默认保持 True"}},
    requires=("doc_open",), effect=("sketch_open",), verified=True)
def op_sketch_begin(sw, step, ctx):
    plane = _p(step, ctx, "plane")
    if plane:
        if not sw.select_plane(plane):
            raise SwError(f"开始草图时选不中面: {plane}")
    if sw.selected_count() == 0:
        raise SwError("开始草图前必须先选一个基准面/平面（用 select_plane）。")
    sm = _sm(sw)
    sm.InsertSketch(True)
    with contextlib.suppress(Exception):
        sm.AddToDB = bool(_p(step, ctx, "add_to_db", True))
        sm.DisplayWhenAdded = False       # 批量画的时候别一个个刷屏
    if _p(step, ctx, "reset", True):
        ctx["segments"] = []
        ctx["dimmed_segments"] = []      # 换了一张草图，尺寸记账也要重来
    ctx["sketch_open"] = True
    return {}


@op("sketch_end", "结束草图", "sketch",
    "退出草图。会把 AddToDB 关掉（**必须**，否则 SolidWorks 一直处于直接写库模式，"
    "后续特征行为异常）、重绘视图、清选择。\n"
    "★ 建议**总是给 name**：后续 op 的 profile/path/sketch 参数都按名字引用，"
    "而草图自动名（Sketch1 / 草图1）在不同语言界面下不一样，命名后跨机器才可复现。",
    api=["ISketchManager.AddToDB", "ISketchManager.InsertSketch", "IModelView.GraphicsRedraw2",
         "IModelDoc2.ClearSelection2"],
    params={"name": {"type": "str", "required": False,
                     "desc": "给这张草图起的名字（强烈建议给）"},
            "select": {"type": "bool", "required": False, "default": True,
                       "desc": "退出后是否自动选中该草图（默认是，拉伸/旋转要用）"}},
    requires=("sketch_open",), effect=("sketch_closed", "has_sketch"), verified=True)
def op_sketch_end(sw, step, ctx):
    sm = _sm(sw)
    with contextlib.suppress(Exception):
        sm.AddToDB = False
    with contextlib.suppress(Exception):
        sm.DisplayWhenAdded = True
    # ★ 试过改成"退出前抓 `sm.ActiveSketch` 的名字"（2026-10-02），**回滚了** ——
    #   那版在真机回归里把**螺母/垫片1 弄坏**（`extrude_boss` 报「选不中草图 草图1」，
    #   改名没生效）。`last_sketch_name()` 只在"文档里有 2 张以上草图"时才会选错对象，
    #   而**那种场景已经被另一个办法消掉了**（见下），所以老逻辑对现存件是可靠的。
    #   教训：为一个**只在特定组合下出现**的问题去动**所有草图共用**的改名逻辑，
    #   风险远大于收益；真要动，必须先跑全量真机回归。
    #   ★ 那个"2 张草图"的场景是：草图1 画圆 → **草图内 helix 把草图1 删掉** → 再开截面草图。
    #     修法在 `op_helix`（插入后把 `ctx["sketch_open"]` 置 False）+ 生成器里**不再**多写
    #     一步 `sketch_end`；到 `sketch_end(截面)` 时文档里只剩 1 张草图 ⇒ 老逻辑正好够用。
    sm.InsertSketch(True)                  # 再调一次 = 退出草图
    ctx["sketch_open"] = False
    with contextlib.suppress(Exception):
        member(sw.view(), "GraphicsRedraw2")
    sw.clear_selection()
    sk_name = sw.last_sketch_name()
    # 改名：自动名（Sketch1/草图1）随语言变，跨机器不可复现 —— 命名后引用才稳。
    want = _p(step, ctx, "name")
    renamed = False
    if want and sk_name and str(want) != str(sk_name):
        with contextlib.suppress(Exception):
            typed(sw.model.FeatureByName(sk_name), "IFeature").Name = str(want)
        renamed = True
        after = sw.last_sketch_name()          # 改名也可能静默失败，读回来核对
        if after:
            sk_name = after
    ctx["last_sketch"] = sk_name
    # ★ 退出草图后必须把这张草图**选上**。
    #   SolidWorks 的特征命令吃的是「当前选择」，不是参数对象；
    #   早先这里只清空选择，导致所有不写 profile 的 extrude/revolve 全部失败
    #   —— 也就是「画完草图直接拉伸」这条最常见的链路整个不通。
    if sk_name and _p(step, ctx, "select", True):
        sw.select(sk_name, "sketch")
    # 顺手体检轮廓数：!=1 的话后面拉伸必然出问题，早点报出来比在拉伸处报清楚
    n_contour = sw.sketch_contour_count(sk_name) if sk_name else None
    ctx["last_sketch_contours"] = n_contour
    out = {"sketches": member(sw.model, "GetFeatureCount"), "sketch": sk_name}
    if renamed:
        out["renamed_to"] = sk_name
    if n_contour is not None:
        out["contours"] = n_contour
        if n_contour == 0:
            out["warn_contours"] = (
                "这个草图有 0 个闭合轮廓，拉伸/旋转一定失败。"
                "查：是不是一个图元都没画上（样条尤其容易静默消失，见 30-pitfalls），"
                "或者轮廓是开口的。"
            )
        elif n_contour > 1:
            out["warn_contours"] = (
                f"这个草图有 {n_contour} 个独立轮廓。**多数情况是正常的** —— "
                "外轮廓 + 中心孔 + 孔阵就是好几个轮廓，SolidWorks 照常拉伸。"
                "只有在拉伸/切除报「轮廓未闭合/自相交」时才需要怀疑："
                "① 多段图元的端点没粘合（样条端点**不会**自动吸附，圆弧会）；"
                "② 两个闭环互相重叠（典型：键槽矩形压在孔圆上）。见 30-pitfalls。"
            )
        if n_contour != 1:
            out["contour_hint"] = "多轮廓不一定是错的，先看拉伸结果再决定要不要排查。"
    return out


# ============================================================ 草图约束（M3）
#
# 背景（2026-09-25，需求 3）：早先全靠 AddToDB 直接写库，SolidWorks **不推断
# 约束也不加尺寸** ⇒ 画出来的草图 `GetConstrainedStatus` = 2 UnderConstrained、
# 尺寸个数 = 0。后果是「参数化」这个卖点其实不成立：改一个尺寸，图形会乱动。
# 真机实测对照（同一个 40×30 矩形）：
#     画完直接退出           → 状态 2，尺寸 0 个，**没法用尺寸驱动**
#     画完调 fully_define    → 尺寸出现，`set_dimension` 40→60 生效、重建成功
# 所以这一节就是需求 3 的落地。

#: `ISketch.GetConstrainedStatus` 的返回值语义（swConstrainedStatus_e）
CONSTRAINED_STATUS = {
    0: "Unknown", 1: "UnknownConstraint", 2: "UnderConstrained",
    3: "FullyConstrained", 4: "OverConstrained", 5: "NoSolution",
    6: "InvalidSolution", 7: "AutosolveOff",
}
#: 十种自动关系按位或全开（swSketchFullyDefineRelationType_e）
FULLY_DEFINE_ALL_RELATIONS = 1023
FULLY_DEFINE_RELATION_NAMES = {
    "equal": 1, "horizontal": 2, "vertical": 4, "tangent": 8,
    "perpendicular": 16, "colinear": 32, "concentric": 64,
    "parallel": 128, "midpoint": 256, "coincident": 512,
}


def _isketch_of(sw, name: str | None = None):
    """取 ISketch：给了名字按特征取，没给就取**当前活动草图**。

    ★ 两条真机结论（2026-09-25，M3）：
      1. `GetConstrainedStatus` 对**已闭合的草图也有效** ——
         `FeatureByName(name)` → `GetSpecificFeature2` → `ISketch` 这条路，
         草图退出编辑后照样读得到（实测欠约束回 2、完全定义回 3）。
         所以校验不用重新进草图。
      2. 活动草图要从 `IModelDoc2.GetActiveSketch2()` 拿，它回的是 **IFeature**
         （`GetTypeName` = `ProfileFeature`），不是 ISketch，得再套一层
         `GetSpecificFeature2`。
    """
    feat = None
    if name:
        with contextlib.suppress(Exception):
            feat = sw.model.FeatureByName(str(name))
    else:
        with contextlib.suppress(Exception):
            feat = member(sw.model, "GetActiveSketch2")
    if feat is None:
        return None
    with contextlib.suppress(Exception):
        return typed(member(feat, "GetSpecificFeature2"), "ISketch")
    return None


def _constrained_status(sw, name: str | None = None) -> int | None:
    """读约束状态码；读不到回 None（**不要**当成"没问题"）。"""
    sk = _isketch_of(sw, name)
    if sk is None:
        return None
    with contextlib.suppress(Exception):
        return int(member(sk, "GetConstrainedStatus"))
    return None


def _relations_bitmask(rel) -> int:
    """把 `"all"` / `"horizontal,vertical"` / `["coincident"]` / 整数 统一成位掩码。"""
    if rel is None or rel == "":
        return FULLY_DEFINE_ALL_RELATIONS
    if isinstance(rel, (int, float)):
        return int(rel)
    if isinstance(rel, str):
        key = rel.strip().lower()
        if key in ("all", "*", "全部", "所有"):
            return FULLY_DEFINE_ALL_RELATIONS
        toks = [t for t in re.split(r"[,|\s+]+", key) if t]
    elif isinstance(rel, (list, tuple)):
        toks = [str(t).strip().lower() for t in rel if str(t).strip()]
    else:
        raise SwError(f"relations 参数看不懂: {rel!r}")
    bits = 0
    for t in toks:
        if t not in FULLY_DEFINE_RELATION_NAMES:
            raise SwError(
                f"未知的关系名 {t!r}。可用：{', '.join(sorted(FULLY_DEFINE_RELATION_NAMES))}"
                "，或直接给 all。")
        bits |= FULLY_DEFINE_RELATION_NAMES[t]
    return bits


@op("fully_define", "完全定义草图", "sketch",
    "★★ 需求「草图要有标准尺寸约束、改图不变形」的直接解。\n"
    "调 `ISketchManager.FullyDefineSketch`：自动补几何关系 + 尺寸，把草图从\n"
    "欠约束推到**完全定义**。必须在草图**处于打开状态**时调用。\n"
    "★ 为什么 `require` 默认 True —— 它**会撒谎**：\n"
    "  真机实测（2026-09-25，M3）：一个不碰原点的 40×30 矩形，返回码 `0 Success`，\n"
    "  却只补了 1 个尺寸，读回 `GetConstrainedStatus` **仍然是 2 UnderConstrained**。\n"
    "  跟倒角那次的病根一模一样 —— **别信返回码，读回状态才算数**。\n"
    "  想让它收敛，草图得**锚住**（角点/圆心与原点或已有几何重合）；\n"
    "  否则至少空着 2 个定位自由度，任何自动算法都补不上。\n"
    "  锚不住时正确做法：`require: false` 让它在报告里说清，再用 `dimension`\n"
    "  显式把定位尺寸标上。\n"
    "★ 参数含义（官方定义，10 参全在这）：`entities` 全部实体/仅选中；\n"
    "  `relations` 允许用的关系（默认 all = 1023 = 十种全开）；\n"
    "  `use_dimensions` 是否补尺寸（**要参数化就必须 True**）；\n"
    "  `scheme` 尺寸方案 0=链式 1=基准 2=坐标；`above` 放置 0=上/右 1=下/左。",
    api=["ISketchManager.FullyDefineSketch", "ISketch.GetConstrainedStatus",
         "IModelDoc2.GetActiveSketch2", "IFeature.GetSpecificFeature2"],
    params={"entities": {"type": "bool", "required": False, "default": True,
                         "desc": "True=全部实体，False=只处理当前选中的"},
            "relations": {"type": "str", "required": False, "default": "all",
                          "desc": "'all' 或 'horizontal,vertical,tangent…' 或整数位掩码"},
            "use_relations": {"type": "bool", "required": False, "default": True},
            "use_dimensions": {"type": "bool", "required": False, "default": True,
                               "desc": "★ 要参数化就保持 True；False 只补关系不补尺寸"},
            "scheme": {"type": "int", "required": False, "default": 0,
                       "desc": "尺寸方案 0=链式 1=基准 2=坐标"},
            "above": {"type": "bool", "required": False, "default": True,
                      "desc": "尺寸放置：True=上/右，False=下/左"},
            "require": {"type": "bool", "required": False, "default": True,
                        "desc": "★ 没达到完全定义就抛错（默认）；False 只报告不抛"}},
    requires=("sketch_open",), verified=True,
    sealed=True, sealed_reason=SEALED_REASON + "\n（另一个已实测的理由："
    "`FullyDefineSketch` 在本机会**撒谎** —— 返回 0 Success 却只补 1 个尺寸、"
    "读回仍是 UnderConstrained，见本 op 正文。）")
def op_fully_define(sw, step, ctx):
    bits = _relations_bitmask(_p(step, ctx, "relations", "all"))
    before = _constrained_status(sw)
    sm = _sm(sw)
    rc = None
    err = None
    try:
        rc = int(member(sm, "FullyDefineSketch",
                        bool(_p(step, ctx, "entities", True)),
                        bool(_p(step, ctx, "use_relations", True)),
                        int(bits),
                        bool(_p(step, ctx, "use_dimensions", True)),
                        int(_p(step, ctx, "scheme", 0) or 0), None,
                        int(_p(step, ctx, "scheme", 0) or 0), None,
                        0 if _p(step, ctx, "above", True) else 1,
                        0 if _p(step, ctx, "above", True) else 1))
    except Exception as e:                                    # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    with contextlib.suppress(Exception):
        member(sw.view(), "GraphicsRedraw2")
    after = _constrained_status(sw)
    n_dims = None
    with contextlib.suppress(Exception):
        n_dims = len(_all_dimensions(sw))
    out = {"return_code": rc,
           "return_meaning": {0: "Success", 1: "Fail", 2: "NoSketch",
                              3: "NotEnoughEntities"}.get(rc, None),
           "status_before": before,
           "status_before_meaning": CONSTRAINED_STATUS.get(before),
           "status_after": after,
           "status_after_meaning": CONSTRAINED_STATUS.get(after),
           "fully_defined": after == 3,
           "relations_bits": bits}
    if n_dims is not None:
        out["n_dims"] = n_dims
    if err:
        out["call_error"] = err
    if _p(step, ctx, "require", True) and after != 3:
        raise SwError(
            f"完全定义**没成功**：状态 {after}（{CONSTRAINED_STATUS.get(after)}），"
            f"期望 3（FullyConstrained）。\n"
            f"  SolidWorks 返回码是 {rc}（{out['return_meaning']}）—— "
            "注意它**可能就是 0 Success**：这个 API 会返回成功但没干完活。\n"
            "  最常见原因：草图**没有锚住**（角点/圆心都没跟原点或已有几何重合），\n"
            "  自动算法补不上定位自由度。两种修法：\n"
            "    ① 把草图锚到原点（画图元时让某个端点/圆心落在 0,0）；\n"
            "    ② 用 `dimension` 显式标出定位尺寸（水平 + 垂直各一个）。\n"
            "  只想拿报告不报错：`require: false`。")
    return out


@op("line", "直线", "sketch", "画一条直线（草图内，单位 mm）。",
    api=["ISketchManager.CreateLine"],
    params={"x1": {"type": "float", "unit": "mm"}, "y1": {"type": "float", "unit": "mm"},
            "x2": {"type": "float", "unit": "mm"}, "y2": {"type": "float", "unit": "mm"},
            "z1": {"type": "float", "unit": "mm", "required": False, "default": 0,
                   "desc": "★ 只在 **3D 草图**（`sketch3d_begin`）里有意义；2D 草图里 z 被忽略"},
            "z2": {"type": "float", "unit": "mm", "required": False, "default": 0}},
    requires=("sketch_open",), verified=True)
def op_line(sw, step, ctx):
    s = _sm(sw).CreateLine(mm(_p(step, ctx, "x1")), mm(_p(step, ctx, "y1")),
                           mm(_p(step, ctx, "z1", 0) or 0),
                           mm(_p(step, ctx, "x2")), mm(_p(step, ctx, "y2")),
                           mm(_p(step, ctx, "z2", 0) or 0))
    ctx.setdefault("segments", []).append(s)
    return {"segments": len(ctx["segments"])}


@op("centerline", "中心线", "sketch",
    "画构造中心线（作为镜像轴/旋转轴用）。中心线不参与轮廓，但 SW 特征会认它。",
    api=["ISketchManager.CreateCenterLine"],
    params={"x1": {"type": "float", "unit": "mm"}, "y1": {"type": "float", "unit": "mm"},
            "x2": {"type": "float", "unit": "mm"}, "y2": {"type": "float", "unit": "mm"}},
    requires=("sketch_open",), verified=True)
def op_centerline(sw, step, ctx):
    s = _sm(sw).CreateCenterLine(mm(_p(step, ctx, "x1")), mm(_p(step, ctx, "y1")), 0,
                                 mm(_p(step, ctx, "x2")), mm(_p(step, ctx, "y2")), 0)
    ctx.setdefault("segments", []).append(s)
    ctx["axis_segment"] = s
    return {}


@op("circle", "圆", "sketch",
    "画整圆。给 r 或 d 之一（都行，d 优先）。圆常用于齿轮的齿顶圆/齿根圆参考、"
    "或直接拉伸成圆柱。",
    api=["ISketchManager.CreateCircleByRadius"],
    params={"cx": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "cy": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "r": {"type": "float", "unit": "mm", "required": False},
            "d": {"type": "float", "unit": "mm", "required": False}},
    one_of=(("r", "d"),),
    requires=("sketch_open",), verified=True)
def op_circle(sw, step, ctx):
    cx = _p(step, ctx, "cx", 0) or 0
    cy = _p(step, ctx, "cy", 0) or 0
    d = _p(step, ctx, "d")
    r = _p(step, ctx, "r")
    if d is None and r is None:
        raise SwError("圆要给出 r 或 d")
    rr = float(d) / 2.0 if d is not None else float(r)
    if rr <= 0:
        raise SwError(f"圆半径必须 > 0，当前 {rr}")
    s = _sm(sw).CreateCircleByRadius(mm(cx), mm(cy), 0, mm(rr))
    ctx.setdefault("segments", []).append(s)
    # 记下来给 sketch_circular_pattern 用：那个命令要的是「种子到阵列中心的距离」，
    # 光知道阵列中心（通常是原点）算不出来。
    ctx["last_circle"] = {"cx": float(cx), "cy": float(cy), "r": rr}
    return {"r": rr}


@op("rect_center", "中心矩形", "sketch", "以 (cx,cy) 为心、宽 w 高 h 的矩形。",
    api=["ISketchManager.CreateCenterRectangle"],
    params={"cx": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "cy": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "w": {"type": "float", "unit": "mm"}, "h": {"type": "float", "unit": "mm"}},
    requires=("sketch_open",), verified=True)
def op_rect_center(sw, step, ctx):
    cx, cy = float(_p(step, ctx, "cx", 0) or 0), float(_p(step, ctx, "cy", 0) or 0)
    w, h = float(_p(step, ctx, "w")), float(_p(step, ctx, "h"))
    if min(w, h) <= 0:
        raise SwError("矩形宽高必须 > 0")
    # ★ CreateCenterRectangle 的参数是「**中心点** + 一个角点」，
    #   不是「两个对角点」。按对角点传会把矩形画成 2 倍大且偏到一角
    #   （实测 w=80 出 160）。这个错误离线测不出来，只能靠真机跑 + 量包围盒。
    s = _sm(sw).CreateCenterRectangle(
        mm(cx), mm(cy), 0, mm(cx + w / 2), mm(cy + h / 2), 0)
    ctx.setdefault("segments", []).append(s)
    return {}


@op("rect_corner", "对角矩形", "sketch",
    "给左上、右下两个对角点画矩形。SolidWorks 宏录出来的是这种（CreateCornerRectangle），"
    "所以宏翻译会产出它。",
    api=["ISketchManager.CreateCornerRectangle"],
    params={"x1": {"type": "float", "unit": "mm"}, "y1": {"type": "float", "unit": "mm"},
            "x2": {"type": "float", "unit": "mm"}, "y2": {"type": "float", "unit": "mm"}},
    requires=("sketch_open",), verified=True)
def op_rect_corner(sw, step, ctx):
    x1, y1 = float(_p(step, ctx, "x1")), float(_p(step, ctx, "y1"))
    x2, y2 = float(_p(step, ctx, "x2")), float(_p(step, ctx, "y2"))
    if abs(x2 - x1) < 1e-9 or abs(y2 - y1) < 1e-9:
        raise SwError("矩形两个对角点不能有相同坐标（宽或高为 0）")
    s = _sm(sw).CreateCornerRectangle(mm(x1), mm(y1), 0, mm(x2), mm(y2), 0)
    # ★ 真机实测（2026-09-25，M2）：`CreateCornerRectangle` 返回的是**4 条线段组成的数组**，
    #   不是 1 个图元。早先把整个数组当成"一个图元"塞进 ctx["segments"]，
    #   于是 `dimension` / `sketch_fillet` 按下标取时整体错位（segs[1] 直接 IndexError）。
    #   这里摊平，保证 ctx["segments"] 里**每个元素都是一个 ISketchSegment**。
    segs = []
    if s is None:
        raise SwError("CreateCornerRectangle 没返回图元（对角点重合？）")
    if isinstance(s, (tuple, list)):
        segs = [x for x in s if x is not None]
    else:
        segs = [s]
    ctx.setdefault("segments", []).extend(segs)
    return {"segments": len(segs), "total": len(ctx["segments"])}


@op("arc3p", "三点圆弧", "sketch",
    "过三点的圆弧。齿轮的齿根圆弧、倒角过渡用它。\n"
    "★★ **点序是「起点 → 终点 → 弧上点」，不是「起点 → 中点 → 终点」**\n"
    "   （2026-10-01 真机实测：按后者画出来的弧只有 90°，拼不成闭环）。\n"
    "   签名 `Create3PointArc(X1,Y1,Z1, X2,Y2,Z2, X3,Y3,Z3)` 的参数名 P1/P2/P3\n"
    "   在 tlb 里**不说明语义**，只能实测确认：P1=Start、P2=End、P3=PointOnArc。\n"
    "   判断依据：拿 (12,5) (17,0) (12,-5) 建弧，回读发现弧度只有 90° ——\n"
    "   说明 SW 把 (12,-5) 当成**弧上一点**而不是终点（终点被当成 (17,0)）。\n"
    "★ 三点**不能共线**，否则返回 None。\n"
    "★ 想让弧与相邻直线接上，P1/P2 必须**与那两条线的端点坐标完全一致**。",
    api=["ISketchManager.Create3PointArc"],
    params={"p1": {"type": "list", "unit": "mm", "desc": "起点 [x,y]"},
            "p2": {"type": "list", "unit": "mm", "desc": "★ 终点 [x,y]（不是中点！）"},
            "p3": {"type": "list", "unit": "mm", "desc": "弧上任意一点 [x,y]"}},
    requires=("sketch_open",), verified=True,
    limits=("点序 = 起点/终点/弧上点；写成「起点/中点/终点」会只画出 90° 弧、拼不成闭环",
            "三点共线直接返回 None"))
def op_arc3p(sw, step, ctx):
    p1 = resolve(_p(step, ctx, "p1"), ctx)
    p2 = resolve(_p(step, ctx, "p2"), ctx)
    p3 = resolve(_p(step, ctx, "p3"), ctx)
    s = _sm(sw).Create3PointArc(mm(p1[0]), mm(p1[1]), 0,
                                mm(p2[0]), mm(p2[1]), 0,
                                mm(p3[0]), mm(p3[1]), 0)
    if s is None:
        raise SwError(f"三点圆弧创建失败（三点可能共线）: {p1} {p2} {p3}")
    ctx.setdefault("segments", []).append(s)
    return {}


@op("spline", "样条曲线", "sketch",
    "过一串点的样条。**齿轮渐开线、凸轮轮廓、导流曲面都靠它**。"
    "points 是 [[x,y], ...] 或 [[x,y,z], ...]（mm）。closed=True 时自动把首点补到末尾形成闭合样条。"
    "点在 3 个以上，否则 SolidWorks 直接失败。\n"
    "★★★ **点表必须包成 `VARIANT(VT_ARRAY|VT_R8)`**（本项目用 `sw_connect.variant_r8()`）。\n"
    "  直接传 Python list/tuple 时 pywin32 编组出的数组元素类型不对，SolidWorks 的表现是\n"
    "  —— **不报错、不抛异常、返回非 None，但草图里图元数恒为 0**（「样条静默消失」）。\n"
    "  ★ 2026-10-09 由新建的 `sketch3d` 探针抓出来：**此前没有任何探针碰过这个 op**，\n"
    "    所以它一直挂着 `verified=True` 却是**假绿**（现在已修，并有了真机用例）。\n"
    "★ 点数也有上限：整圈 1100 点的样条会被丢弃（草图直接消失），拆到 660 点以内没问题。\n"
    "★ 给了第 3 个坐标（z）就按 **3D 空间点**走 —— 必须配合 `sketch3d_begin`；"
    "  在 2D 草图里 z 被忽略（想画空间曲线请用 `sketch3d_begin`）。",
    api=["ISketchManager.CreateSpline3"],
    params={"points": {"type": "list", "unit": "mm", "desc": "[[x,y], ...] 或 [[x,y,z], ...]"},
            "closed": {"type": "bool", "required": False, "default": False},
            "natural_ends": {"type": "bool", "required": False, "default": False}},
    requires=("sketch_open",), verified=True)
def op_spline(sw, step, ctx):
    pts = resolve(_p(step, ctx, "points"), ctx)
    if len(pts) < 3:
        raise SwError(f"样条至少要 3 个点，给了 {len(pts)}")
    flat: list[float] = []
    for p in pts:
        flat += [mm(p[0]), mm(p[1]), (mm(p[2]) if len(p) > 2 else 0.0)]
    if _p(step, ctx, "closed", False):
        flat += [flat[0], flat[1], flat[2]]
    # ★★ `variant_r8()` 不是可选项 —— 见 op 文档与 sw_connect.variant_r8 的踩坑记录。
    s = _sm(sw).CreateSpline3(variant_r8(flat), None, None,
                              bool(_p(step, ctx, "natural_ends", False)), None)
    if s is None:
        raise SwError("CreateSpline3 返回空（点重复或过近？）")
    ctx.setdefault("segments", []).append(s)
    return {"points": len(pts)}


@op("polygon", "正多边形", "sketch",
    "正 n 边形（外接圆半径 r，顶点在圆上）。六角螺栓头用它。"
    "SW 没有现成的 CreatePolygon，用 n 条直线拼。",
    api=["ISketchManager.CreateLine"],
    params={"n": {"type": "int", "default": 6}, "r": {"type": "float", "unit": "mm"},
            "cx": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "cy": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "rot": {"type": "float", "unit": "deg", "required": False, "default": 0}},
    requires=("sketch_open",), verified=True)
def op_polygon(sw, step, ctx):
    n = int(_p(step, ctx, "n", 6) or 6)
    r = float(_p(step, ctx, "r"))
    cx, cy = float(_p(step, ctx, "cx", 0) or 0), float(_p(step, ctx, "cy", 0) or 0)
    rot = float(_p(step, ctx, "rot", 0) or 0)
    if n < 3 or r <= 0:
        raise SwError("多边形需要 n>=3 且 r>0")
    sm = _sm(sw)
    pts = []
    for i in range(n):
        a = deg(rot) + 2 * math.pi * i / n
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        ctx.setdefault("segments", []).append(
            sm.CreateLine(mm(x1), mm(y1), 0, mm(x2), mm(y2), 0))
    return {"n": n, "r": r}


@op("sketch_fillet", "草图圆角", "sketch",
    "在草图里给两条线倒圆角。**必须先选中要倒角的两条线段**，"
    "SW 靠选择集判断倒哪里。",
    api=["IModelDocExtension.SelectByID2", "ISketchManager.CreateFillet"],
    params={"r": {"type": "float", "unit": "mm"},
            "which": {"type": "list", "required": False, "desc": "要倒角的线段下标，不给则用全部已画图元"}},
    requires=("sketch_open",), verified=True)
def op_sketch_fillet(sw, step, ctx):
    r = float(_p(step, ctx, "r"))
    idx = _p(step, ctx, "which")
    segs = ctx.get("segments", [])
    sw.clear_selection()
    targets = [segs[i] for i in idx] if idx else segs
    n = _segments_select(sw, targets)
    if n < 2:
        raise SwError(f"草图圆角需要选中两条线段，只选上 {n} 条")
    s = _sm(sw).CreateFillet(mm(r), 0)
    sw.clear_selection()
    if s is None:
        raise SwError("草图圆角失败（两线可能平行/不相交）")
    return {}


@op("sketch_circular_pattern", "草图圆周阵列", "sketch",
    "把草图里已有的图元绕一个点圆周阵列。**注意**：整圈齿形用一整条闭合轮廓更稳"
    "（见 gear_profile），这个 op 适合「一个孔阵列出 6 个」这种局部场景。"
    "阵列前必须选中种子图元。",
    api=["IModelDocExtension.SelectByID2", "ISketchManager.CreateCircularSketchStepAndRepeat"],
    params={"count": {"type": "int", "desc": "含种子在内的总个数"},
            "cx": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "cy": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "angle": {"type": "float", "unit": "deg", "required": False, "default": 360},
            "radius": {"type": "float", "unit": "mm", "required": False,
                       "desc": "种子到阵列中心的距离；不给则按最近画的圆自动算"},
            "select": {"type": "str", "required": False, "default": "created",
                       "desc": "created=本次画的图元  all=全选"}},
    requires=("sketch_open",), verified=True)
def op_sketch_circular_pattern(sw, step, ctx):
    cnt = int(_p(step, ctx, "count"))
    if cnt < 2:
        raise SwError("圆周阵列个数至少 2")
    cx, cy = float(_p(step, ctx, "cx", 0) or 0), float(_p(step, ctx, "cy", 0) or 0)
    total = float(_p(step, ctx, "angle", 360) or 360)
    sw.clear_selection()
    if _p(step, ctx, "select", "created") == "all":
        member(sw.ext, "SelectAll")
    else:
        n_sel = _segments_select(sw, ctx.get("segments", []))
        if n_sel == 0:
            raise SwError("圆周阵列前必须先画种子图元（ctx.segments 为空，种子选不上）")
    # ArcRadius 是**种子到阵列中心的距离**，不是阵列中心到原点的距离。
    # 早先传 hypot(cx,cy)（中心就在原点时=0），命令直接失败。
    r_seed = _p(step, ctx, "radius")
    if r_seed is None:
        seed = ctx.get("last_circle")
        if seed is not None:
            r_seed = math.hypot(seed["cx"] - cx, seed["cy"] - cy)
        else:
            r_seed = math.hypot(cx, cy)
    r_seed = float(r_seed or 0)
    if r_seed <= 0:
        raise SwError("算不出种子的阵列半径：种子圆心不能与阵列中心重合，"
                      "请显式给 radius 参数")
    ang = deg(total)
    # 9 参版本：ArcRadius, ArcAngle, PatternNum, PatternSpacing,
    #           PatternRotate, DeleteInstances(布尔!), RadiusDim, AngleDim, CreateNumOfInstancesDim
    # DeleteInstances 早先传了空字符串 ""，应为布尔。
    # 查法：python scripts/sw_memberkind.py --sig CreateCircularSketchStepAndRepeat
    ok = _sm(sw).CreateCircularSketchStepAndRepeat(
        mm(r_seed), ang, cnt, ang / cnt if cnt else ang,
        True, False, False, False, False)
    sw.clear_selection()
    if not ok:
        raise SwError("草图圆周阵列失败（种子图元没选中，或阵列中心/半径不对）")
    return {"count": cnt, "radius": round(r_seed, 4)}


# ============================================================ 草图尺寸（智能尺寸）
#
# 背景（2026-09-25，用户现场）：「草图里画了圆，**还是没显示尺寸参数**」。
# `dimension` 功能上一直是生效的（包围盒会跟着变），但视口里**看不到尺寸**。
#
# 真机 A/B 取证（D:\out\diag_dim1.py，五种标注 API × 原点/偏置两种放置）：
#     AddAlongXDimension(0,0,0)        → 尺寸建出来了（D1，读回 30），
#                                        但文字压在**原点**上；而 `circle(cx=0,cy=0)`
#                                        的圆心也在原点 ⇒ 文字与原点标记叠成一团灰影。
#                                        放大截图后才看清「Ø30」确实在那儿。
#     AddDiameterDimension2(19.5,19.5) → 清清楚楚。
# 结论：**用哪个标注 API 不致命，文字放哪儿才致命。**
#
# 另一个真机结论（D:\out\diag_dim5.py）：想「顺带完全定义」就
#     `AddDiameterDimension2` + 选中**圆心点** + `SketchAddConstraints("sgFIXED")`
#   → 状态 3 FullyConstrained，且直径**照样能驱动**（Ø30→Ø40 读回 40.0）。
# 反面：把**整个圆** sgFIXED → 状态 4 OverConstrained，直径被锁死（改 40 读回 30）。

#: `ISketchSegment.GetType()` → swSketchSegments_e
SEG_KIND = {0: "line", 1: "arc", 2: "ellipse", 3: "spline", 4: "text", 5: "parabola"}


def _pt_mm(p) -> list | None:
    """`ISketchPoint` → [x, y, z]（**毫米**）。读不到返回 None。

    ★ 坐标是**米**，必须 ×1000。
    ★ 必须 `member(pt, "X")`：`getattr(pt, "X")` 在晚期绑定下拿不到值
      （会拿到绑定方法或 None）—— 这个坑在 diag_dim2 上白跑了一轮。
    """
    q = typed(p, "ISketchPoint")
    if q is None:
        return None
    out: list = []
    for ax in ("X", "Y", "Z"):
        v, _ = _call(member, q, ax)
        out.append(float(v) * 1000.0 if isinstance(v, (int, float)) else None)
    if None in out:
        return None
    return out


def _seg_geom(sw, seg) -> dict:
    """读一个草图图元的几何（毫米）。

    ★ 必须走 `GetType()` → 派生接口这条路：官方在 swSketchSegments_e 的备注里
      明写了「you can obtain the appropriate derived class (that is, ISketchLine,
      ISketchArc, and so on)」。实测 `GetType()`：直线 = 0、圆/弧 = 1。
    """
    t, _ = _call(member, seg, "GetType")
    g: dict = {"type_code": t, "kind": SEG_KIND.get(t, f"?{t}")}
    if t == 1:                                              # swSketchARC（含整圆）
        arc = typed(seg, "ISketchArc")
        v, _ = _call(member, arc, "IsCircle")
        g["is_circle"] = bool(v) if v is not None else None
        v, _ = _call(member, arc, "GetRadius")
        g["radius_mm"] = round(float(v) * 1000.0, 4) if isinstance(v, (int, float)) else None
        cp, _ = _call(member, arc, "GetCenterPoint2")
        g["center_mm"] = _pt_mm(cp)
    elif t == 0:                                            # swSketchLINE
        ln = typed(seg, "ISketchLine")
        for meth, key in (("GetStartPoint2", "start_mm"), ("GetEndPoint2", "end_mm")):
            p, _ = _call(member, ln, meth)
            g[key] = _pt_mm(p)
        v, _ = _call(member, ln, "Angle")
        g["angle_deg"] = (round(math.degrees(float(v)), 4)
                          if isinstance(v, (int, float)) else None)
    return g


def _dim_place_mm(geom: dict, margin: float | None = None) -> list | None:
    """算尺寸文字该放哪儿 —— **必须在图元外面**。这是「看得见」的唯一关键。

    圆/弧：圆心 + (r+m, r+m)  —— 右上斜向，SW 的引线会自然指回圆周
    直线：  中点 + 法向偏移   —— 法向取「朝上/朝右」那一侧，免得压在图元上
    """
    kind = geom.get("kind")
    if kind == "arc":
        c, r = geom.get("center_mm"), geom.get("radius_mm")
        if c and r:
            m = margin if margin is not None else max(4.0, 0.30 * r)
            return [c[0] + r + m, c[1] + r + m, 0.0]
    if kind == "line":
        a, b = geom.get("start_mm"), geom.get("end_mm")
        if a and b:
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy) or 1.0
            m = margin if margin is not None else max(4.0, 0.30 * length)
            nx, ny = -dy / length, dx / length               # 逆时针 90°
            if ny < 0 or (abs(ny) < 1e-9 and nx < 0):        # 翻到「朝上/朝右」
                nx, ny = -nx, -ny
            return [(a[0] + b[0]) / 2.0 + nx * m, (a[1] + b[1]) / 2.0 + ny * m, 0.0]
    return None


def _sketch_bbox_place_mm(sw, seg, margin: float = 6.0) -> list:
    """兜底放置点：草图所有点的包围盒**右上角外面**一格。

    只在图元类型读不出几何时用（样条/椭圆/文字/抛物线）。点数太多就截断 ——
    这只是兜底，不值得为它把 110 段齿廓的点全读一遍。
    """
    sk = None
    with contextlib.suppress(Exception):
        sk = typed(member(seg, "GetSketch"), "ISketch")
    pts = None
    if sk is not None:
        pts, _ = _call(member, sk, "GetSketchPoints2")
    xs, ys = [], []
    for p in list(pts or [])[:600]:
        c = _pt_mm(p)
        if c:
            xs.append(c[0])
            ys.append(c[1])
    if not xs:
        return [25.0, 25.0, 0.0]
    return [max(xs) + margin, max(ys) + margin, 0.0]


def _anchor_center(sw, seg) -> dict:
    """把弧/圆的**圆心点**固定住（sgFIXED）—— 配合已有尺寸即**完全定义**。

    ★ 真机三条（diag_dim5，都是实跑出来的）：
      ① 圆心点 + sgFIXED + 直径尺寸 → **3 FullyConstrained**，直径仍可驱动；
      ② 整个圆 sgFIXED → **4 OverConstrained**，直径被锁死（改 40 读回 30）
         ⇒ **只能锁圆心点，不能锁图元本身**；
      ③ 本想走「圆心点 + 原点 重合」这条更正统的路，但
         `SelectByID2("Point1@Origin", "EXTSKETPOINT" | "SKETCHPOINT" | 别名)`
         **五种变体全部返回 False**（diag_dim4），本机取不到原点实体 ⇒ 改用 sgFIXED。
    """
    out: dict = {}
    before = _constrained_status(sw)
    out["status_before"] = before
    out["status_before_meaning"] = CONSTRAINED_STATUS.get(before)
    cp, _ = _call(member, typed(seg, "ISketchArc"), "GetCenterPoint2")
    cpt = typed(cp, "ISketchPoint")
    if cpt is None:
        out["anchor_error"] = "取不到圆心点（ISketchArc.GetCenterPoint2 返回空）"
        return out
    sw.clear_selection()
    r, e = _call(member, cpt, "Select4", False, None)
    out["center_selected"] = sw.selected_count()
    if out["center_selected"] != 1:
        out["anchor_error"] = (f"选圆心点没选上（选中数 {out['center_selected']}）；"
                               f"Select4 回 {r!r}，错误 {e}")
        return out
    _call(member, sw.model, "SketchAddConstraints", "sgFIXED")
    st = _constrained_status(sw)
    out["status_after"] = st
    out["status_after_meaning"] = CONSTRAINED_STATUS.get(st)
    out["fully_defined"] = st == 3
    sw.clear_selection()
    return out


@op("dimension", "智能尺寸（自动挑标注方式 + 自动避开图元）", "sketch",
    "给草图图元标一个**能被 `set_dimension` 驱动**的尺寸。这是参数化的入口。\n"
    "★★ **尺寸文字放哪儿，比用哪个 API 重要得多** —— 这是用户报障的直接原因：\n"
    "  旧实现固定放在 (0,0,0)，而 `circle(cx=0, cy=0)` 的圆心正好在原点，\n"
    "  文字与原点标记叠成一团灰影，看起来就是「没有尺寸参数」。现在默认**自动放到图元外面**。\n"
    "★ `as` 决定标注方式（默认 auto，按图元类型挑）：\n"
    "    auto     圆→diameter、弧→radius、直线→length\n"
    "    diameter 直径（等价界面上点圆的智能尺寸）\n"
    "    radius   半径\n"
    "    length   线性长度（AddDimension2，让 SW 自己判）\n"
    "    x / y    沿 X / 沿 Y 的投影尺寸\n"
    "★ **圆标出来是直径**：r=15 的圆 `as=diameter` 读回 30。要半径就显式 `as=radius`。\n"
    "★ `anchor: true` 会**顺带把圆心点固定**（sgFIXED）：圆心固定 + 尺寸 ⇒ 草图\n"
    "  **完全定义**，而尺寸**照样能驱动**。只对圆/弧有效；别指望它锁整个图元\n"
    "  （锁整个圆会过约束、尺寸全锁死）。\n"
    "★ `segment` 是**图元下标**（从 0 起，按画图顺序），不是「第几个尺寸」。\n"
    "★ `at` 与 `margin`：不给 `at` 就自动放置（圆=圆心+(r+m,r+m)，直线=中点+法向 m）。",
    api=["ISketchSegment.GetType", "ISketchArc.GetCenterPoint2", "ISketchArc.GetRadius",
         "ISketchLine.GetStartPoint2", "IModelDoc2.AddDiameterDimension2",
         "IModelDoc2.AddRadialDimension2", "IModelDoc2.AddDimension2",
         "ISketchManager.AddAlongXDimension", "ISketchManager.AddAlongYDimension",
         "IModelDoc2.SketchAddConstraints", "IDimension.SetSystemValue3"],
    params={"segment": {"type": "int", "desc": "图元下标（从 0 起，按画图顺序）"},
            "value": {"type": "float", "unit": "mm"},
            "as": {"type": "str", "required": False, "default": "auto",
                   "desc": "auto / diameter / radius / length / x / y"},
            "at": {"type": "list", "unit": "mm", "required": False,
                   "desc": "手工指定文字位置 [x,y]；不给则自动放到图元外"},
            "margin": {"type": "float", "unit": "mm", "required": False,
                       "desc": "自动放置时距图元的外扩量；默认 max(4, 0.3×尺寸)"},
            "anchor": {"type": "bool", "required": False, "default": False,
                       "desc": "圆/弧：顺带固定圆心点，使草图完全定义"},
            "name": {"type": "str", "required": False, "desc": "尺寸名（尽力而为，可能被拒）"},
            "axis": {"type": "str", "required": False,
                     "desc": "【旧名，等价 as 的 x/y/auto】保留兼容老计划"}},
    requires=("sketch_open",), verified=True,
    sealed=True, sealed_reason=SEALED_REASON)
def op_dimension(sw, step, ctx):
    segs = ctx.get("segments", [])
    i = int(_p(step, ctx, "segment"))
    if i < 0 or i >= len(segs):
        raise SwError(f"图元下标 {i} 越界（本次草图共画了 {len(segs)} 个图元；下标从 0 起）")
    seg = _live_segment(sw, ctx, i) or segs[i]
    v = float(_p(step, ctx, "value"))
    geom = _seg_geom(sw, seg)

    # ---- 1. 定标注方式（`as` 优先；`axis` 是旧名，保留兼容老计划）----
    as_ = _p(step, ctx, "as")
    if as_ is None:
        as_ = _p(step, ctx, "axis", "auto")
    as_ = str(as_ or "auto").strip().lower()
    if as_ in ("auto", ""):
        as_ = {"arc": "diameter", "line": "length"}.get(geom.get("kind"), "plain")
    # ★ 真机实测（M2）：`ISketchManager.AddDimension` **不存在**。类型库里能吃的只有
    #   AddAlongX/YDimension（ISketchManager）与 AddDimension2 / AddDiameterDimension2 /
    #   AddRadialDimension2（IModelDoc2）。早先写 AddDimension ⇒ 每次 AttributeError。
    maker = {
        "diameter": ("IModelDoc2.AddDiameterDimension2",
                     lambda p: member(sw.model, "AddDiameterDimension2", p[0], p[1], p[2])),
        "radius": ("IModelDoc2.AddRadialDimension2",
                   lambda p: member(sw.model, "AddRadialDimension2", p[0], p[1], p[2])),
        "x": ("ISketchManager.AddAlongXDimension",
              lambda p: member(_sm(sw), "AddAlongXDimension", p[0], p[1], p[2])),
        "y": ("ISketchManager.AddAlongYDimension",
              lambda p: member(_sm(sw), "AddAlongYDimension", p[0], p[1], p[2])),
        "length": ("IModelDoc2.AddDimension2",
                   lambda p: member(sw.model, "AddDimension2", p[0], p[1], p[2])),
        "plain": ("IModelDoc2.AddDimension2",
                  lambda p: member(sw.model, "AddDimension2", p[0], p[1], p[2])),
    }
    if as_ not in maker:
        raise SwError(f"as 参数看不懂: {as_!r}。可用：auto / diameter / radius / length / x / y")
    api_name, call = maker[as_]

    # ---- 2. 算文字位置（★「看不见」的根因就在这一步）----
    at = _p(step, ctx, "at")
    if at:
        place = [mm(float(at[0])), mm(float(at[1])),
                 mm(float(at[2])) if len(list(at)) > 2 else 0.0]
        place_src = "manual"
    else:
        pm = _dim_place_mm(geom, _p(step, ctx, "margin"))
        if pm is None:
            pm = _sketch_bbox_place_mm(sw, seg)
            place_src = "bbox-fallback"
        else:
            place_src = "auto-outside"
        # ★ 自检：自动放置点必须真的落在图元**外面**。
        #   这正是用户这次报障的根因（文字压在图元/原点上 = 看不见），
        #   写成硬断言，免得以后谁改坏公式又悄悄退回「看不见」。
        if place_src == "auto-outside" and geom.get("kind") == "arc":
            c0, r0 = geom.get("center_mm"), geom.get("radius_mm")
            if c0 and r0:
                dist = math.hypot(pm[0] - c0[0], pm[1] - c0[1])
                if dist <= r0:
                    raise SwError(
                        f"自动放置点 {[round(x, 3) for x in pm]}mm 落在圆内"
                        f"（距圆心 {dist:.2f}mm ≤ 半径 {r0}mm）—— "
                        "尺寸文字会被图元压住，用户看不见。")
        place = [mm(pm[0]), mm(pm[1]), mm(pm[2])]

    # ---- 3. 选中图元并标注（★ 以**实际选中数**为准，不信 Select4 的返回值）----
    ok, sel_log, seg_ok = _select_one_strict(sw, ctx, i)
    if not ok:
        raise SwError(
            f"选不中下标 {i} 的图元（标注前必须选中它）。\n  尝试记录：{sel_log}\n"
            "  两个已知根因：① 图元对象已失效 —— 报错里会带 0x80010108 /「断开连接」，"
            "这是创建时拿到的 IDispatch 在 AddToDB 模式下变旧了；"
            "② `Select4` 回 True 但**实际一个都没选上** —— 所以这里一律数选中数。")
    if seg_ok is not None:
        seg = seg_ok
    dd, err = _call(call, place)
    if dd is None:
        raise SwError(
            f"标注失败（{api_name}，文字位置 {[round(x * 1000.0, 2) for x in place]}mm）："
            f"{err}\n  图元类型 {geom.get('kind')}（GetType={geom.get('type_code')}）。")
    # 统一成 IDimension：AddXxxDimension 给的是 DisplayDimension，要先 GetDimension2
    dim_obj = dd
    with contextlib.suppress(Exception):
        d2 = member(dd, "GetDimension2", 0)
        if d2 is not None:
            dim_obj = d2

    name = _p(step, ctx, "name")
    if name:
        # FullName 要 'D1@Sketch1' 这种带 @ 的规范名；裸名字多半被拒，
        # 拒了也别崩 —— 后面 set_dimension 用 sketch+index 照样能寻址。
        for attr in ("FullName", "Name"):
            with contextlib.suppress(Exception):
                setattr(dim_obj, attr, name)

    before, _ = _call(member, dim_obj, "SystemValue")
    _call(member, dim_obj, "SetSystemValue3", mm(v), 2, None)
    full, after = None, None
    with contextlib.suppress(Exception):
        full = member(dim_obj, "FullName")
    with contextlib.suppress(Exception):
        after = round(float(member(dim_obj, "SystemValue")) * 1000.0, 4)

    out: dict = {"kind": geom.get("kind"), "as": as_, "api": api_name,
                 "value": v, "value_mm_after": after,
                 "place_mm": [round(x * 1000.0, 3) for x in place],
                 "place_src": place_src, "geom": geom,
                 "full_name": full, "n_dims": len(_all_dimensions(sw))}
    if isinstance(before, (int, float)):
        out["value_mm_before"] = round(float(before) * 1000.0, 4)
    # ★ 圆 / 弧的「沿 X 尺寸」量的是**直径**不是半径（M2 实测）：r=15 标出来是 Ø30。
    #   所以把**读回来的值**放进返回值 —— 计划作者能立刻看出语义对不对，
    #   而不是等到下游 check_bbox 超差才发现。
    if geom.get("kind") == "arc" and as_ in ("x", "y") and geom.get("radius_mm"):
        out["semantic_hint"] = (
            f"圆/弧的沿 X/Y 尺寸量的是直径：R{geom['radius_mm']} 标出来是 "
            f"Ø{geom['radius_mm'] * 2}。喂 value={geom['radius_mm']} 会把圆缩一半。")

    # ---- 4. 可选：固定圆心点 ⇒ 完全定义 ----
    if _p(step, ctx, "anchor", False):
        if geom.get("kind") == "arc":
            sw.clear_selection()
            out["anchor"] = _anchor_center(sw, seg)
            out["fully_defined"] = out["anchor"].get("fully_defined", False)
        else:
            out["anchor_skipped"] = f"anchor 只对圆/弧有效，当前图元是 {geom.get('kind')}"
    out["status"] = _constrained_status(sw)
    out["status_meaning"] = CONSTRAINED_STATUS.get(out["status"])
    rec = [int(x) for x in (ctx.get("dimmed_segments") or [])]
    if i not in rec:
        rec.append(i)
    ctx["dimmed_segments"] = rec
    sw.clear_selection()
    return out


#: `smart_dim` = 用户嘴里的「智能尺寸」。做成**同一个 OpSpec 的第二个键**，
#: 而不是再抄一份定义 —— 两份定义必然漂移（replace 共享 fn/doc/params，只换展示名）。
OPS["smart_dim"] = replace(OPS["dimension"], name="smart_dim",
                           cn="智能尺寸（`dimension` 的同义词）")


# ---------------------------------------------------------------- 批量真尺寸

def _kinds_set(v) -> set:
    """把 `kinds` 参数归一成集合。all / 空 ⇒ 三种都要。"""
    if v is None:
        return {"line", "circle", "arc"}
    if isinstance(v, (list, tuple, set)):
        items = [str(x).strip().lower() for x in v]
    else:
        items = [s.strip().lower() for s in str(v).replace("/", ",").split(",")]
    items = [s for s in items if s]
    if not items or "all" in items:
        return {"line", "circle", "arc"}
    bad = [s for s in items if s not in ("line", "circle", "arc")]
    if bad:
        raise SwError(f"kinds 里有看不懂的类型: {bad}。可用 all / line / circle / arc")
    return set(items)


def _auto_dim_spec(geom: dict, kinds: set) -> tuple | None:
    """按几何决定「该标什么尺寸、值多少」—— 标的是**设计意图**，不是坐标。

    圆 → 直径(Ø)    弧 → 半径(R)    直线 → 长度

    ★ 这跟 `fully_define` 自动补的尺寸**不是一回事**：后者按 scheme 补「到原点的
      坐标尺寸」或链式尺寸，数值对，但跟图纸习惯对不上（图纸上不会把轴段的直径
      标成"到原点的 Y 坐标 15"）。真尺寸要的就是 Ø / R / 长度。
    """
    kind = geom.get("kind")
    if kind == "arc":
        r = geom.get("radius_mm")
        if not r:
            return None
        if geom.get("is_circle"):
            if "circle" in kinds or "arc" in kinds:
                return ("diameter", float(r) * 2.0)
            return None
        if "arc" in kinds:
            return ("radius", float(r))
        return None
    if kind == "line" and "line" in kinds:
        a, b = geom.get("start_mm"), geom.get("end_mm")
        if not a or not b:
            return None
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length <= 1e-9:
            return None
        return ("length", length)
    return None


def _dim_signature(geom: dict, as_: str, value: float) -> tuple | None:
    """「逐段标注」的**去重签名** —— 同 caliber + 同测量区间 ⇒ 是同一个量，只标一次。

    ★ 为什么按「**投影区间**」而不是「等长」（2026-10-02，用户实测反馈驱动）：
      `chain=off` 是"逐段标"，在**矩形 / 台阶型面**上会把同一个量标多遍 ——
      矩形环的对边等长**且投影区间完全重合**（上下边都跨 x∈[0,13]），标两遍是纯粹的
      重复，图上出现"同一个数字标了两遍"（用户截图：13 出现两次、3.50 出现两次）。
      而阶梯轴的两段**等长台阶在不同位置**（x∈[0,30] vs x∈[56,82]）⇒ 是两个量，
      都得留。⇒ 判据必须用**区间**，用裸长度就会误删。
    ★ 方向（角度）也要进签名：横向 13 与纵向 13 是两个不同的量。
    """
    kind = geom.get("kind")
    if kind == "line":
        a, b = geom.get("start_mm"), geom.get("end_mm")
        if not a or not b:
            return None
        ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
        if abs(ang) < 1e-6 or abs(ang - 180.0) < 1e-6:       # 水平 ⇒ 量的是 x 区间
            lo, hi = sorted((a[0], b[0]))
        else:                                                # 竖直 ⇒ 量的是 y 区间
            lo, hi = sorted((a[1], b[1]))
        return (as_, round(ang, 3), round(lo, 4), round(hi, 4))
    if kind == "arc":
        c = geom.get("center_mm")
        if geom.get("is_circle"):
            # 整圆的 Ø：**同圆心同值**才算重复（两个不同位置的孔是两个量）
            return (as_, round(c[0], 4), round(c[1], 4)) if c else (as_, round(value, 4))
        # 弧的 R：同 caliber 同半径 ⇒ 同一个量（典型：平键两端 R5、圆角 R2 一次写）
        return (as_, round(value, 4))
    return None


def _off_dim_goal(geoms: list, kinds: set) -> tuple:
    """`chain=off`（逐段标）的标注清单 —— **带同量去重**。

    返回 `(pairs, skipped)`：`pairs` = `[(geom, as_, value), …]`。
    ★ 抽成独立函数，是为了让 `sw_selftest` **直接测这条判据本身**，
      而不是在测试里把逻辑抄一遍 —— 抄一遍测的是副本，不是真代码（§74）。
    """
    pairs: list = []
    skipped: list = []
    sig_seen: list = []
    for g in geoms:
        # ★★★ 2026-10-02 真机踩到（9 件回归 8 件失败的真因）：
        #   **构造几何（中心线）必须在算签名之前排除**。
        #   环形的 `centerline` 长度恰好等于型面的"长"（都从 x=0 起、同向、同区间）
        #   ⇒ 它的签名先占了位，轮到真正的型面长边时被当成"重复"跳过 ⇒
        #   草图里只剩"高"一条尺寸（实测 `n_dimmed=1`，闸门报"只有 1 个"）。
        #   这不是"去重太狠"，是**去重把构造线也算进去了**。
        if g.get("construction"):
            skipped.append({"index": g["index"], "why": "构造几何/中心线"})
            continue
        spec = _auto_dim_spec(g, kinds)
        if spec is None:
            skipped.append({"index": g["index"],
                            "why": f"{g.get('kind')} 不在 kinds({sorted(kinds)}) 内/"
                                   f"或读不到几何"})
            continue
        sig = _dim_signature(g, spec[0], float(spec[1]))
        if sig is not None and sig in sig_seen:
            skipped.append({"index": g["index"],
                            "why": f"与已标图元重复（{float(spec[1]):g}，同向同区间）"
                                   f"⇒ 同一个量不标第二遍"})
            continue
        if sig is not None:
            sig_seen.append(sig)
        pairs.append((g, spec[0], float(spec[1])))
    return pairs, skipped


def _line_dir(geom: dict) -> tuple | None:
    """直线方向单位向量 + 起点。"""
    a, b = geom.get("start_mm"), geom.get("end_mm")
    if not a or not b:
        return None
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy)
    if n <= 1e-12:
        return None
    return (dx / n, dy / n), (a[0], a[1]), n


def _dim_span(sw, seg_a, seg_b, place_mm: list, value: float, mode: str = "auto",
              wit: str | None = None) -> dict:
    """标**两个图元之间**的距离（半剖面件：轮廓线 → 中心线 = 半径）。

    ★ 「两个图元之间」和「单个图元自己的长度」用的是**同一个 API 家族**，
      靠**选择集**区分语义 —— 所以这里必须先同时选中两个。

    ★ 用哪个标注 API：实测（v1.4.3）两个图元都选上之后，`AddDimension2`
      报 `com_error -2147352573 (0x80020003) DISP_E_MEMBERNOTFOUND`。
      换成语义更明确的 `AddVerticalDimension2`（量 Y 向距离）/
      `AddHorizontalDimension2`（量 X 向距离）——「到轴的距离」在一个方向上唯一，
      正好对上。这里按顺序都试，全败才抛错并列出各自错误。
    """
    # ★ 不能信 `Select4` 的返回值（晚期绑定下常回 None，甚至回 True 却没选上）
    #   —— 数选中数才算数；新建图元还有时序竞态，所以要「刷视图 + 重试」。
    # ★ 见证点覆盖：「两个图元之间」默认取**最近端点**，遇到"承载线自己躺在轴上"
    #   的总长尺寸就不对。给 `wit` 就补选**两条竖直边上的端点**（SW 按点做见证点），
    #   这样"0 → 50"一定穿过整个轮廓。取几何里的实际点（mm），不是猜的。
    #   ★ 必须在 `clear_selection()` **之前**选好，否则会被清掉。
    wit_pts = list(wit) if (wit and isinstance(wit, (list, tuple)) and
                            len(wit) == 2 and all(w for w in wit)) else None
    n, log = 0, ""
    for k in range(4):
        sw.clear_selection()
        r1, e1 = _select_segment_ex(seg_a, append=False)
        r2, e2 = _select_segment_ex(seg_b, append=True)
        # ★ 顶点（mark=1）加在两端点连线之上；`Select4` 的 mark 只有在
        #   "先按类型成对出现"时才稳，单独选顶点会丢 —— 所以最坏情况只是
        #   退化成"两端点之间"的度量，不会更差。
        if wit_pts:
            for w in wit_pts:
                with contextlib.suppress(Exception):
                    sw.model.Extension.SelectByID2(
                        "", "EXTSKETCHPOINT", mm(w[0]), mm(w[1]), mm(w[2]), True, 1, None, 0)
        n = sw.selected_count()
        log = (f"第{k + 1}轮: 图元A→{r1} err={e1}；图元B→{r2} err={e2}；选中 {n}")
        if n >= 2:
            break
        _refresh_view(sw)
        time.sleep(0.15)
    if n < 2:
        raise SwError(f"span：需要同时选中 2 个图元，实际 {n} 个。\n  {log}")

    x, y, z = mm(place_mm[0]), mm(place_mm[1]), mm(place_mm[2])
    order: list[tuple[str, str]] = []
    if mode in ("v", "auto"):
        order.append(("AddVerticalDimension2", "量 Y 向距离"))
    if mode in ("h", "auto"):
        order.append(("AddHorizontalDimension2", "量 X 向距离"))
    order.append(("AddDimension2", "泛型智能尺寸"))

    dd, errs = None, []
    for meth, why in order:
        got, e = _call(member, sw.model, meth, x, y, z)
        if got is not None:
            dd, errs = got, [f"✓ {meth}（{why}）"]
            break
        errs.append(f"{meth}: {e}")
    if dd is None:
        raise SwError("span：两图元之间的距离标不出来（三种 API 全失败）：\n    "
                      + "\n    ".join(errs))

    dim_obj = dd
    with contextlib.suppress(Exception):
        d2 = member(dd, "GetDimension2", 0)
        if d2 is not None:
            dim_obj = d2
    before, _ = _call(member, dim_obj, "SystemValue")
    _call(member, dim_obj, "SetSystemValue3", mm(value), 2, None)
    after = None
    with contextlib.suppress(Exception):
        after = round(float(member(dim_obj, "SystemValue")) * 1000.0, 4)
    full = None
    with contextlib.suppress(Exception):
        full = member(dim_obj, "FullName")
    out = {"value_mm": round(float(value), 4), "value_mm_after": after,
           "place_mm": [round(float(x), 3) for x in place_mm], "full_name": full,
           "api": errs[0]}
    if isinstance(before, (int, float)):
        out["value_mm_before"] = round(float(before) * 1000.0, 4)
    return out


# ------------------------------------------------ 尺寸链（链式标注）求解
#
# ★★ 为什么要有这一段：用户口径 = **「不是每个尺寸都标」**。
#    一张阶梯轴半剖面，逐段把 6 条线的长度全标上 ⇒ 前 5 段之和 = 总长，
#    属于**过定义**；再叠上"到轴距"更是重复。真机实测（2026-09-25）SW 会
#    **静默拒标**：op 报 n_dimmed=6，模型里只剩 1 个尺寸、状态还是欠定义。
#
# ★ 规则（一张图里同一方向只留一套尺寸）：
#    ① **总长**（整体跨度）必标，且只标标一次；
#    ② 横向**功能尺寸**（台阶定位）必标；
#    ③ 其余段长 = **派生量**（总长 − 已标段），一条都不标；
#    ④ 直径走「到轴线距离」（半剖面）/ 圆本身标 Ø —— 与长度方向正交，不冲突。
#  ⇒ 尺寸总数 = 1(总长) + (n台阶) 个功能长度 + 各段半径/直径，**完备且不冗余**。

def _pct_dist(a: float, b: float) -> float:
    """相对偏差（用于"这两条边是不是同一个坐标"的判定）。"""
    m = max(abs(a), abs(b))
    return abs(a - b) / m if m > 1e-9 else 0.0


def _remeasure_revolve_feature(sw, prof: dict, feat_name: str) -> tuple:
    """★ 真机实测：`revolve_boss` 进 AddToDB 模式后**不自动退出**（不像 extrude），
      所以草图画完仍在 AddToDB 里 —— 新建图元全部 `RPC_E_DISCONNECTED`。
       自愈法 = **退出草图再重进**（重建特征树后，选择集才刷新）。
       判据不能用返回值（`SelectByID2` 会说谎）→ 用 `feature_count()` 数特征树。
       返回 (ok, 说明)；ok=False 时调用方自己决定要不要继续（能标多少标多少）。"""
    before = None
    with contextlib.suppress(Exception):
        before = sw.feature_count()
    with contextlib.suppress(Exception):
        sw.select(prof["name"], "sketch")
        sw.model.EditSketch()
        sw.model.InsertSketch(False)          # 退出草图（提交一次重建）
        time.sleep(0.4)
        sw.select("Front", "plane")
        sw.select(prof["name"], "sketch", append=True)
        sw.model.EditSketch()                 # 重进草图
        time.sleep(0.3)
    after = None
    with contextlib.suppress(Exception):
        after = sw.feature_count()
    if before is None or after is None:
        return True, "特征计数读不到，按「已重开」继续"
    if after >= before:
        return True, f"特征数 {before} → {after}（已退出 AddToDB，重进草图）"
    return False, f"特征数反而少了（{before} → {after}），重开没生效"


def _cylinder_set(sw):
    """读回转体的圆柱面 → 半径列表（用于把轮廓线分成「母线 / 端面」）。

    ★★ 真机坑（2026-09-25）：**`GetSurface` 只能在 `typed(face,"IFace2")` 上调用**。
       直接 `face.GetSurface()` 走原始 dispatch 会报
       `-2147352573 DISP_E_MEMBERNOTFOUND 找不到成员`（脚本里被 suppress 吞掉，
       表现为「明明有圆柱面却读到 0 个」）。用 `member(typed(f,"IFace2"),"GetSurface")`。
    ★ `CylinderParams` 是**扁平 7 元组**：`(原点x,y,z, 轴向量x,y,z, 半径)` —— 半径是
      最后一个字段，**不是** `p[6]` 之外的什么 `p[8]`（老代码按 11 字段读，全错）。
    ★ 拿全部实体走现成的 `_bodies(sw)`（`GetBodies2(-1, False)`）；别用
      `sw.model.GetBodies2(0, True)` —— 多实体/隐藏时会漏。
    ★ 先 `rebuild` 一次，避免特征刚建完读到的还是旧拓扑。
    """
    out = []
    with contextlib.suppress(Exception):
        sw.rebuild(force=True)
    for b in _bodies(sw):
        with contextlib.suppress(Exception):
            for face in b.GetFaces() or []:
                with contextlib.suppress(Exception):
                    surf = member(typed(face, "IFace2"), "GetSurface")
                    if surf is None or not member(surf, "IsCylinder"):
                        continue
                    p = member(surf, "CylinderParams")
                    if not p or len(p) < 7:
                        continue
                    r = float(p[6])                     # 半径（米）
                    if r <= 0:
                        continue
                    out.append(round(r * 1000.0, 4))
    return sorted(set(out))


def _plan_chain(geoms: list[dict], cyc: list) -> tuple:
    """把半剖面轮廓拆成**一组不重复的尺寸**。

    返回 `(goal, diag)`：`goal` = [{'type','index'/'edge','value','tag',...}]，逐条去标。
    """
    segL = [g for g in geoms if g.get("kind") == "line"]
    segA = [g for g in geoms if g.get("kind") == "arc" and g.get("is_circle")]
    diag: dict = {}

    # --- 有一整个圆柱面 ⇒ 这是个**回转体**，走半剖面那套 ---
    if cyc:
        # 母线候选：轴向半径 ≈ 某个圆柱面半径的轮廓线 ⇒ 它就是这段轴的**母线**（长度是结构长度）。
        # ★ 近似圆（采样渐开线/样条）没有圆柱面 ⇒ 会被判成端面，只参与总长，正好把"不标"的意图带过去。
        tol = 0.04
        bus = []
        for g in segL:
            if g.get("axis"):
                continue
            a, b = g.get("start_mm"), g.get("end_mm")
            if not a or not b:
                continue
            # ★ 母线判据 = **该线到轴线的距离 ≈ 某个圆柱面半径**。
            #   别去读线的 `radius_mm` —— `_seg_geom` 只给**圆弧**填这个字段，
            #   直线根本没有（真机踩到：bus 恒为空，于是半径一条都标不上）。
            #   轴线是水平的（轮廓闭合边），它的 y 就是 0，故距离 = |线的 y|。
            if abs(b[1] - a[1]) > 1e-6:
                continue                                   # 只认平行于轴的水平线
            dist = abs(a[1])
            if any(abs(dist - cr) <= tol * max(cr, 1e-6) for cr in cyc):
                bus.append({"index": g["index"], "value": round(dist, 4),
                            "tag": "母线→到轴距", "geom_kind": "line"})
        # --- 总长：最左 / 最右端面点 ---
        xs = [p for g in segL for p in _line_xs(g)]
        if not xs:
            return [], {"error": "读不到轮廓的 x 坐标"}
        x1, x2 = min(xs), max(xs)
        # ★★ 关键：横向尺寸一律**跨两根竖线**标（总长 = 左端面↔右端面），
        #   纵向尺寸留在**水平母线**上。两者落点分离 ⇒ 不会互相顶掉。
        #   真机踩到（2026-09-25）：`AddAlongXDimension` 是"该图元到原点/轴的距离"，
        #   打在母线上会和同一条线的 Y 尺寸抢同一根图元 —— seg3 先挂 X=20，
        #   再挂 Y=15 就被静默拒绝（值停在 20）；反过来 seg4 的 AddAlongX
        #   读到的是 seg3 的 Y（15）。换成 AddDimension2(两条竖线) 后语义清晰。
        lv, rv = _span_pair(geoms, x1, x2, 0.0)
        goal = [{"type": "balance", "value": round(x2 - x1, 4), "tag": "总长",
                 "i1": lv, "i2": rv,
                 "prefer_seg": _best_total_seg(segL, x1, x2)}]
        # --- 横向功能尺寸：**台阶的位置**（相对基准端 x1），去重 ---
        # ★ 标的是「从 x1 到台阶竖线」的一段，不是母线长度。母线起点在左端贴基准
        #   时距离是 0（退化零尺寸，真机踩到 D=0.0）⇒ 跳过。
        # ★ 台阶取**竖线** x，且不含 x2（那是总长的右见证，重复标就过定义）。
        starts = []
        for sx in _stations_x(geoms):
            if _pct_dist(sx, x1) <= 1e-6 or _pct_dist(sx, x2) <= 1e-6:
                continue
            loc = abs(sx - x1)
            if any(_pct_dist(loc, s["value"]) < 1e-4 for s in starts):
                continue
            i1, i2 = _span_pair(geoms, x1, sx, 0.0)
            starts.append({"value": round(loc, 4), "tag": "台阶定位",
                           "i1": i1, "i2": i2, "x": round(sx, 4)})
        goal += [{"type": "station", "value": s["value"], "tag": s["tag"],
                  "i1": s["i1"], "i2": s["i2"], "x": s["x"]} for s in starts]
        # --- 半径（直径）：母线到轴线的距离，按半径去重 ---
        # ★ 「到轴距」= **轴线 ↔ 母线** 两条平行线的间距（`AddDimension2`），
        #   不是单图元的 AddAlongYDimension —— 后者在重进过草图的场景会把尺寸挂错图元，
        #   半径静默停在错值（seg3 想设 15，before/after 都在 20）。见 handler 注释。
        # ★ 轴线见证优先取**实线**（阶梯轴型面贴轴，那条 y=0 的边就是轴）；
        #   实线取不到时必须**退回构造中心线** —— 环形件 / 闷盖的型面**离轴**
        #   （y∈[ri,ro]，没有 y=0 的实线），唯一躺在轴上的图元就是 `centerline`
        #   画的那条（construction=True）。
        #   2026-10-02 真机踩到：不放行构造线 ⇒ `axis_seg=None` ⇒ span 退回**单图元**
        #   `AddAlongYDimension`（已知不可靠）⇒ 半径/直径一条都标不上，草图里只剩
        #   一串重复的总长值（探针 `spec_relations`：goal 3 条算对了，落地 1 条，
        #   读回全是 13.0）。放行构造线后走 `AddDimension2(轴线↔母线)`，半径才落得下。
        ax_seg = next((g["index"] for g in segL
                       if g.get("axis") and not g.get("construction")), None)
        if ax_seg is None:
            ax_seg = next((g["index"] for g in segL
                           if g.get("axis") and g.get("construction")), None)
        seen = []
        for b in bus:
            r = b["value"]
            if any(_pct_dist(r, (s["value"] / 2.0)) < tol for s in seen):
                continue
            goal.append({"type": "span_radius", "index": b["index"], "value": r,
                         "axis_seg": ax_seg, "tag": "半径→Ø"})
            seen.append({"value": r * 2.0})
        diag["mode"] = "revolve_halfsection"
        diag["axis_seg"] = ax_seg
        diag["witness"] = [round(x1, 4), round(x2, 4)]
        diag["bus"] = [(b["index"], b["value"]) for b in bus]
    else:
        # --- 非回转：横向按「总长 + 台阶差」链式，圆另标直径 ---
        xs = [p for g in segL for p in _line_xs(g)]
        if not xs:
            return [], {"error": "读不到轮廓的 x 坐标"}
        x1, x2 = min(xs), max(xs)
        uniq = sorted({round(v, 4) for v in xs})
        goal = []
        # 端点必留 X=0（洞的左壁）；其余取「相邻间距最大的那些」当台阶
        stations = [uniq[0]]
        spans = [(uniq[i], uniq[i + 1]) for i in range(len(uniq) - 1)]
        keep_spans = {uniq[0]}
        for a, b in spans:
            keep_spans.add(b)
        # 台阶判据：某站的竖线长度 > 两侧竖线（简化为：竖线存在且该 x 有"最高的"竖线）
        hor = [g for g in segL if _is_horiz(g)]
        goal.append({"type": "balance", "value": round(x2 - x1, 4), "tag": "总长",
                     "prefer_seg": _best_total_seg(segL, x1, x2)})
        for g in hor:
            sx = g["start_mm"][0]
            if _pct_dist(sx, x1) < 1e-6 or _pct_dist(sx, x2) < 1e-6:
                continue
            goal.append({"type": "real", "index": g["index"], "value": round(abs(sx - x1), 4),
                         "tag": "段长（相对左端）", "span": "to_left"})
        # 圆弧/齿轮近似：外径用半径尺寸，保留但去重
        if segA:
            goal.append({"type": "real_arc", "value": None, "tag": "外径"})
        diag["mode"] = "chain_horizontal"
        diag["stations"] = uniq
    return goal, diag


def _line_xs(g: dict) -> list:
    a, b = g.get("start_mm"), g.get("end_mm")
    if not a or not b:
        return []
    return [a[0], b[0]]


def _vert_xs(geoms: list[dict], x: float, tol: float = 1e-4) -> list:
    """取所有**竖线（端面/台阶面）**落在 x 上的索引。

    ★ 用法：横向尺寸（总长/台阶定位）必须打在**竖线**上，纵向尺寸（到轴距）
      打在**水平母线**上。同一根图元上既挂 X 又挂 Y，真机会互相顶掉
      （实测：seg3 先挂 X=20 后挂 Y=15 ⇒ Y 被静默拒绝，值停在 20）。"""
    out = []
    for g in geoms:
        if g.get("kind") != "line" or g.get("construction"):
            continue
        a, b = g.get("start_mm"), g.get("end_mm")
        if not a or not b:
            continue
        if abs(a[0] - b[0]) > 1e-6 or abs(b[1] - a[1]) <= 1e-9:
            continue                                   # 不是竖线
        if _pct_dist(a[0], x) <= tol:
            out.append(g["index"])
    return out


def _span_pair(geoms: list[dict], x1: float, x2: float, y: float = 0.0) -> tuple:
    """在 x1 / x2 处各挑一根**竖线**，组成 (左索引, 右索引)；优先靠近 y 的那根。
       取不到竖线时退回 None（调用方降级到 AddAlongXDimension）。"""
    def pick(x):
        cands = _vert_xs(geoms, x)
        if not cands:
            return None
        def dy(i):
            g = geoms[i]
            return min(abs((g["start_mm"] or [0, 0])[1] - y),
                       abs((g["end_mm"] or [0, 0])[1] - y))
        return min(cands, key=dy)
    return pick(x1), pick(x2)


def _stations_x(geoms: list[dict]) -> list:
    """轮廓里所有**竖线**的 x（台阶候选），去重升序。"""
    xs = []
    for g in geoms:
        if g.get("kind") != "line" or g.get("construction"):
            continue
        a, b = g.get("start_mm"), g.get("end_mm")
        if not a or not b:
            continue
        if abs(a[0] - b[0]) <= 1e-6 and abs(b[1] - a[1]) > 1e-9:
            xs.append(round(a[0], 4))
    return sorted(set(xs))


def _is_horiz(g: dict) -> bool:
    a, b = g.get("start_mm"), g.get("end_mm")
    if not a or not b:
        return False
    return abs(a[1] - b[1]) < 1e-6 and abs(b[0] - a[0]) > 1e-9


def _best_total_seg(segL: list, x1: float, x2: float):
    """挑一条横跨 x1..x2 的线（优先轮廓闭合边）当总长的**承载**图元 —— 它保证
       "总长"尺寸一定标得上（引用存在的图元），Witness 再重新点到真正的端点上。"""
    best = None
    for g in segL:
        a, b = g.get("start_mm") or [None, None], g.get("end_mm") or [None, None]
        if a[0] is None or b[0] is None:
            continue
        lo, hi = min(a[0], b[0]), max(a[0], b[0])
        if _pct_dist(lo, x1) > 1e-4 or _pct_dist(hi, x2) > 1e-4:
            continue
        if abs(a[1] - b[1]) < 1e-6:          # 水平承载线优先
            return g["index"]
        best = best if best is not None else g["index"]
    return best


def _witness_span(geoms: list[dict]) -> tuple:
    """总长的两条**见证边** x 坐标：取最左 / 最右的竖线（端面）的 x，没有竖线就用全部点的极值。"""
    vx = []
    for g in geoms:
        if g.get("kind") != "line" or g.get("construction"):
            continue
        a, b = g.get("start_mm"), g.get("end_mm")
        if not a or not b:
            continue
        if abs(a[0] - b[0]) < 1e-6 and abs(b[1] - a[1]) > 1e-9:   # 竖线 = 端面
            vx.append(round(a[0], 4))
    if vx:
        return (min(vx), max(vx))
    xs = [p for g in geoms for p in _line_xs(g)]
    return (min(xs), max(xs)) if xs else (None, None)


def _top_y(geoms: list[dict]) -> float:
    ys = [p for g in geoms if not g.get("construction")
          for p in ([g.get("start_mm")], [g.get("end_mm")]) if p and p[0] is not None]
    flat = [q[1] for grp in ys for q in grp]
    return max(flat) if flat else 0.0


def _dim_between(sw, ctx, geoms, i1: int, i2: int, mode: str,
                 value: float, margin: float) -> tuple:
    """跨**两个图元**标一条线性尺寸（`AddDimension2`），并设成 `value`。

    ★★ 为什么非要用它（2026-09-25 真机结论）：
      `AddAlongXDimension` / `AddAlongYDimension` 都是**单图元相对原点/轴**的距离。
      同一根母线既挂 X（台阶定位）又挂 Y（到轴距）时，**后挂的会被静默拒标**
      （实测 seg3：先挂 X=20，再挂 Y=15 值停在 20；seg4 的 AddAlongX 又读到
      seg3 的 Y=15）。⇒ 横向必须改成「左端面竖线 ↔ 台阶竖线」这种**跨图元**的量法，
      和纵向的母线彻底分开落点。

    `mode`: `alongX`=量两线在 X 上的间距（线都给竖线）；`alongY`=量 Y 间距（横线）。
    返回 (record, err)；失败给 (None, err) 让调用方自己降级，不抛。"""
    sw.clear_selection()
    place = _between_place(geoms, i1, i2, mode, margin)
    for i in (i1, i2):
        live = _live_segment(sw, ctx, i) or (ctx.get("segments") or [None] * (i + 1))[i]
        if live is None:
            sw.clear_selection()
            return None, f"图元 {i} 取不到活对象"
        _select_segment_ex(live, append=True)
    nsel = sw.selected_count()
    if nsel != 2:
        sw.clear_selection()
        return None, f"跨图元标注需选中 2 个，实际 {nsel}（pair={i1},{i2}）"
    got, err = _call(member, sw.model, "AddDimension2",
                     mm(place[0]), mm(place[1]), mm(place[2]))
    if got is None:
        sw.clear_selection()
        return None, f"AddDimension2 失败：{err}"
    dim_obj = got
    with contextlib.suppress(Exception):
        d2 = member(got, "GetDimension2", 0)
        if d2 is not None:
            dim_obj = d2
    before = None
    with contextlib.suppress(Exception):
        before = round(float(member(dim_obj, "SystemValue")) * 1000.0, 4)
    _call(member, dim_obj, "SetSystemValue3", mm(value), 2, None)
    after = None
    with contextlib.suppress(Exception):
        after = round(float(member(dim_obj, "SystemValue")) * 1000.0, 4)
    full = None
    with contextlib.suppress(Exception):
        full = member(dim_obj, "FullName")
    rec = {"api": "AddDimension2", "value_mm": round(value, 4),
           "value_mm_after": after,
           "place_mm": [round(float(x), 3) for x in place],
           "full_name": full, "pair": [i1, i2], "dim_mode": mode}
    if before is not None:
        rec["value_mm_before"] = before
    sw.clear_selection()
    return rec, None


def _between_place(geoms, i1: int, i2: int, mode: str, margin: float) -> list:
    """线性尺寸文字落点：两图元中点的中点，向「远离轮廓」的方向外扩 margin。"""
    a1 = geoms[i1].get("start_mm") or [0, 0]
    b1 = geoms[i1].get("end_mm") or a1
    a2 = geoms[i2].get("start_mm") or [0, 0]
    b2 = geoms[i2].get("end_mm") or a2
    xs = [a1[0], b1[0], a2[0], b2[0]]
    ys = [a1[1], b1[1], a2[1], b2[1]]
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    top = _top_y(geoms)
    if mode == "alongX":
        # 横向尺寸放轮廓**上方**（y 往上让开），文字点取两线中间
        return [cx, max(top, cy) + margin, 0.0]
    # alongY：纵向（到轴距）放在轮廓**右侧**，文字点取两水平线中点的高度
    return [max(xs) + margin, cy, 0.0]


@op("dim_auto", "批量真尺寸标注（草图的关键操作）", "sketch",
    "★★ 用户口径：**参数标注是草图的关键操作，草图必须用「真尺寸」画**。两条理由：\n"
    "     ① 没有尺寸的草图在图里能随意拖动、改型就变形 —— 不可参数化；\n"
    "     ② 后面出**工程图**要靠「模型项目」把尺寸带进视图 —— 没标过就没有。\n"
    "★★★ **「不是每个尺寸都标」** —— 这是本 op 的第二条铁律（用户原话）。\n"
    "     逐段把 6 条线长全标上 ⇒ Σ(前5段)=总长 ⇒ **过定义**；再叠「到轴距」更是重复。\n"
    "     真机实测（2026-09-25）：SW 会**静默拒标** —— op 报 n_dimmed=6，模型里只剩 1 个。\n"
    "   ⇒ 默认 `chain=auto`：从几何**反推尺寸链**，只标一套完整且不冗余的尺寸。\n"
    "     阶梯轴半剖面（用户给的习惯，对照 `dim_auto_shaft` 探针）就标 **4 条**：\n"
    "     ① **总长** —— 跨「左端面竖线 ↔ 右端面竖线」标一次（图纸上在最外圈）；\n"
    "     ② **横向台阶定位** —— 跨「基准端竖线 ↔ 台阶竖线」标一次（套在总长里面）；\n"
    "        中间各段长度 = 派生量 ⇒ **一条都不标**（图纸上根本不出现）；\n"
    "     ③ 各段**半径/直径** —— 「轴线 ↔ 该段母线」两条平行线的间距（= 半径）。\n"
    "     判据是**圆柱面**：母线（半径≈某个圆柱面）参与定位+半径；端面（近似圆/样条）\n"
    "     只参与总长 —— 齿轮那种采样渐开线**没有圆柱面**，天然不会被误标。\n"
    "★★★ **横向/纵向必须落在不同图元上**（2026-09-25 血泪）：\n"
    "     横向（总长/台阶）一律 `AddDimension2` **跨两根竖线**；纵向（半径）跨\n"
    "     「轴线 + 母线」。**别用单图元的 `AddAlongX/YDimension` 干这两件事** ——\n"
    "     它对「该图元到原点」的语义在重进过草图后会**挂到邻居图元上**（seg3 的 alongY\n"
    "     返回 before=20，正是 seg4 的值），于是半径静默停在错值、`check_bbox` 超差。\n"
    "★ `chain=off` = 老行为：逐个图元标（圆→Ø、弧→R、线→长度）。适合非轮廓草图。\n"
    "★ 只在外面挑好线（先只挑不算），**标到一半失败会报错并指出是哪条**，不会静默留半张。\n"
    "★ 默认跳过**构造几何/中心线**（`ISketchSegment.ConstructionGeometry`）——\n"
    "  给中心线标长度没有意义。\n"
    "★ 只标不锁：标完**不会**自动变成完全定义，状态用 `check_sketch_constraint` 读。",
    api=["ISketchSegment.GetType", "ISketchSegment.ConstructionGeometry",
         "ISketchArc.GetRadius", "ISketchArc.IsCircle",
         "ISketchLine.GetStartPoint2", "ISketchLine.GetEndPoint2",
         "IFace2.GetSurface", "ISurface.CylinderParams",
         "IModelDoc2.AddDiameterDimension2", "IModelDoc2.AddRadialDimension2",
         "IModelDoc2.AddDimension2", "IDimension.SetSystemValue3",
         "IModelDoc2.SketchAddConstraints"],
    params={"kinds": {"type": "str", "required": False, "default": "all",
                      "desc": "要标的类型：all / line / circle / arc，或逗号组合"},
            "chain": {"type": "str", "required": False, "default": "auto",
                      "desc": "★ auto=按尺寸链求解（总长+定位+Ø，派生量不标）；off=逐个标"},
            "skip_construction": {"type": "bool", "required": False, "default": True,
                                  "desc": "跳过构造几何/中心线"},
            "margin": {"type": "float", "unit": "mm", "required": False,
                       "desc": "文字离图元的外扩量；不给按尺寸自动算"},
            "anchor": {"type": "bool", "required": False, "default": False,
                       "desc": "圆/弧的圆心点 sgFIXED（圆心固定 + 尺寸 ⇒ 完全定义）"}},
    # ★ `requires` 只要求 `doc_open`：`chain=auto` 且识别出圆柱面时，本 op 会**自己
    #   退出再重进草图**（`_remeasure_revolve_feature` —— `revolve_boss` 留在 AddToDB 里，
    #   不重进图元选不中）。所以它**不需要**调用方先把草图开着；
    #   但**执行完草图是开着的**（真机实测 2026-09-25：`revolve_boss` 后
    #   `SketchManager.ActiveSketch` 是 None，`dim_auto` 之后是「半剖面」）。
    #   ⇒ 声明 `effect=("sketch_open",)`，计划里后面**必须补 `sketch_end`**。
    #   `chain=off` 在草图已开时调用则不改状态；统一按最常用路径声明，保证计划可校验。
    #   ★ 判据读法：`member(sw.model.SketchManager, "ActiveSketch")` ——
    #     `IModelDoc2.ActiveSketch` **在这台机器上恒为 None**，别用它。
    requires=("doc_open",), effect=("sketch_open",), verified=True,
    sealed=True, sealed_reason=SEALED_REASON)
def op_dim_auto(sw, step, ctx):
    rec_segs = list(ctx.get("segments") or [])
    if not rec_segs:
        raise SwError("这张草图还没有图元 —— 先画（line/circle/rect_center/…）再 dim_auto。")
    # ★ 一律先换成**活的对象**：`CreateLine`/`CreateCircleByRadius` 返回的那批
    #   在 AddToDB 模式下会失效（RPC_E_DISCONNECTED），拿旧对象去选/去读几何
    #   会以「选不中」这种误导形式失败（v1.4.3 真机踩到）。
    segs = [(_live_segment(sw, ctx, i) or rec_segs[i]) for i in range(len(rec_segs))]
    # ★ 批量标注前先刷一遍视口：新建的图元有一小段「还没进可选集合」的窗口期，
    #   本 op 恰恰是「刚画完就立刻全部标注」，最容易撞上（见 `_refresh_view`）。
    _refresh_view(sw)
    kinds = _kinds_set(_p(step, ctx, "kinds", "all"))
    skip_con = bool(_p(step, ctx, "skip_construction", True))
    margin = _p(step, ctx, "margin")
    anchor = bool(_p(step, ctx, "anchor", False))
    chain = str(_p(step, ctx, "chain", "auto") or "auto").lower()
    if chain not in ("auto", "off"):
        raise SwError(f"chain 只认 auto / off，收到 {chain!r}")

    def _is_con(s) -> bool | None:
        with contextlib.suppress(Exception):
            return bool(member(typed(s, "ISketchSegment"), "ConstructionGeometry"))
        return None

    # -------------------- 收集几何 --------------------
    geoms: list[dict] = []
    skipped: list[dict] = []
    for i, seg in enumerate(segs):
        geom = _seg_geom(sw, seg)
        geom["index"] = i
        geom["construction"] = bool(_is_con(seg))
        if geom.get("kind") == "line":
            # ★ `axis` = 这条线**躺在轴上**（水平 **且** y≈0），不是"平行于轴"。
            #   真机踩到：只判「方向水平」会把两条母线（y=10/15）也标成轴，
            #   于是 bus 恒为空、半径一条都标不上。
            a, b = geom.get("start_mm"), geom.get("end_mm")
            geom["axis"] = bool(a and b and abs(b[1] - a[1]) < 1e-6 and abs(a[1]) < 1e-6)
        geoms.append(geom)

    made: list[dict] = []
    spans: list[dict] = []
    diag: dict = {"chain": chain}
    cyc: list = []

    if chain == "auto":
        # ★ 顺序很重要：**先读圆柱面，再重进草图**。
        #   一旦进了草图编辑态，`_bodies()` 会回 0（实体在草图态被临时压制），
        #   圆柱面就读不到了 —— 真机踩到（2026-09-25）。
        # ★ 特征记录用**约定键**：草图是 `ctx["last_sketch"]`，特征名在
        #   `ctx["feature_names"]`。**没有** `ctx["features"]` 这个键（别自己发明 ——
        #   真机踩到：读出来是 None，于是整个圆柱面分支被静默跳过）。
        prof = ctx.get("last_sketch")
        fnames = [str(x) for x in (ctx.get("feature_names") or [])]
        has_rev = bool(fnames)
        if has_rev:
            # ★ 只在**真的有圆柱面**时才认这是回转体：草图态下 `_bodies()` 回 0、
            #   圆柱面读不到 ⇒ cyc 为空，`_plan_chain` 会自动退回逐段链式。
            cyc = _cylinder_set(sw)
            diag["cylinders"] = len(cyc)
            diag["cyl_radii"] = cyc
        # ★ revolve 之后特征树是「草图 → 旋转」，转完**不进** AddToDB —— 这是标注的窗口。
        #   若还在 AddToDB 里（草图刚画完），新建图元全是断的 ⇒ 自愈：退出再重进草图。
        if has_rev and prof:
            ok, why = _remeasure_revolve_feature(sw, {"name": prof}, "")
            diag["reopen"] = why
            if not ok:
                diag["reopen_failed"] = True

    # -------------------- 尺寸链求解 --------------------
    # ★★ `chain=auto` **只对「有圆柱面的回转体半剖面」生效**。判据 = `cyc` 非空。
    #    别拿「有直线」当条件 —— 纯圆草图（同心双圆）也会被判进链式，把 2 个 Ø
    #    错当成"两个长度"，真机踩到（2026-09-25）。圆/弧草图永远走逐个标。
    if chain == "auto" and cyc:
        goal, cdiag = _plan_chain(geoms, cyc)
        diag.update(cdiag)
        # 兜底：链式求解空手而归 ⇒ 退回逐个标
        if not goal:
            diag["fallback"] = "链式求解没有可标项，退回逐个标注"
            chain = "off"
    elif chain == "auto":
        diag["chain_skipped"] = ("没有圆柱面（不是回转体半剖面）⇒ 不启用尺寸链，"
                                 "退回逐个标注（圆→Ø / 弧→R / 线→长度）")
        chain = "off"
    if chain == "off":
        # ★★ 去重（2026-10-02，用户实测反馈驱动）：`chain=off` = 逐段标，
        #   在**矩形 / 台阶型面**上会把同一个量标多遍 —— 矩形的对边等长且**投影区间
        #   重合**，台阶型面的对称竖线同理 ⇒ 图上"同一个数字标了两遍"（用户截图）。
        #   判据与跳过理由都在 `_off_dim_goal` 里（抽出去是为了让自检能直接测它）。
        pairs, skip2 = _off_dim_goal(geoms, kinds)
        skipped.extend(skip2)
        goal = [{"type": "real", "index": g["index"], "value": val,
                 "as": a_, "tag": "逐个标注"} for g, a_, val in pairs]

    # -------------------- 逐条执行 --------------------
    diag["goal"] = [{"type": g.get("type"), "index": g.get("index"),
                     "value": g.get("value"), "tag": g.get("tag")} for g in goal]
    for item in goal:
        idx = item.get("index")
        if idx is not None and idx < 0:
            skipped.append({"index": idx, "why": item.get("tag", "无效图元")})
            continue
        if idx is not None and geoms[idx].get("construction"):
            skipped.append({"index": idx, "why": "构造几何/中心线"})
            continue
        t = item["type"]
        try:
            if t == "real":
                g = geoms[idx]
                sub = {"segment": idx, "value": item["value"],
                       "as": item.get("as") or _auto_dim_spec(g, kinds)[0]}
                if margin is not None:
                    sub["margin"] = margin
                r = op_dimension(sw, sub, ctx)
                rec = {"index": idx, "kind": g.get("kind"), "as": sub["as"],
                       "value_mm": round(item["value"], 4), "tag": item.get("tag"),
                       "place_mm": r.get("place_mm"), "full_name": r.get("full_name")}
                if anchor and g.get("kind") == "arc":
                    a = _anchor_center(sw, segs[idx])
                    rec["anchor"] = {"status_after": a.get("status_after"),
                                     "fully_defined": a.get("fully_defined")}
                made.append(rec)
            elif t == "span_radius":
                g = geoms[idx]
                if abs(g["start_mm"][1] - 0) < 1e-9:      # 母线在轴上 ⇒ 无距离可标
                    skipped.append({"index": idx, "why": "与中心线重合，无距离可标"})
                    continue
                a1, b1 = g["start_mm"], g["end_mm"]
                midx = (a1[0] + b1[0]) / 2.0
                dist = abs(a1[1])
                m = margin if margin is not None else max(4.0, 0.30 * dist)
                ny = 1.0 if a1[1] > 0 else -1.0
                place = [midx, dist + ny * m, 0.0]
                # ★★★ 半剖面的「到轴距」= 母线到轴线的 **Y 向距离**。三种 API 实测（2026-09-25）：
                #   ① `AddAlongYDimension`（单图元）—— **语义不可靠**：它标的是"该图元相对
                #      草图原点的 Y 距离"，但真机在**重进过草图**的场景下会把尺寸挂到
                #      **别的图元**上（seg3 的 before=20、seg4 的 before=15、seg0 的 before=10
                #      —— 全是邻居的值），于是 SetSystemValue3 静默失败、半径停在错值。
                #   ② `AddDimension2` 量"两条**水平**线之间" ⇒ 建出的是**弧长**之类的东西
                #      （before=1570.79=100π），把轮廓拉成 Y=4019mm。
                #   ③ ✅ **选「轴线(seg_axis) + 母线」两条线 → `AddDimension2`**：量的是
                #      两条平行线之间的间距 ⇒ 就是半径，值确定（实测 15.0 一次到位）。
                #   ⇒ 走 ③，轴线取不到时才退回 ①。
                ax = item.get("axis_seg")
                sp = None
                if ax is not None and ax != idx:
                    sp, _e = _dim_between(sw, ctx, geoms, ax, idx, "alongY", dist, m)
                if sp is None:
                    _select_one_strict(sw, ctx, idx)
                    n_sel = sw.selected_count()
                    if n_sel != 1:
                        raise SwError(f"span_radius：母线 {idx} 没选上（选中 {n_sel}）")
                    got, err = _call(member, _sm(sw), "AddAlongYDimension",
                                     mm(place[0]), mm(place[1]), mm(place[2]))
                    if got is None:
                        raise SwError(f"span_radius：AddAlongYDimension 失败：{err}")
                    dim_obj = got
                    with contextlib.suppress(Exception):
                        d2 = member(got, "GetDimension2", 0)
                        if d2 is not None:
                            dim_obj = d2
                    before, _ = _call(member, dim_obj, "SystemValue")
                    _call(member, dim_obj, "SetSystemValue3", mm(dist), 2, None)
                    sp = {"api": "AddAlongYDimension", "place_mm": place}
                    if isinstance(before, (int, float)):
                        sp["value_mm_before"] = round(float(before) * 1000.0, 4)
                sp.update({"index": idx, "tag": item.get("tag", "半径→Ø"),
                           "from_axis": round(dist, 4), "d_mm": round(dist * 2.0, 4)})
                spans.append(sp)
            elif t == "station":
                # ★ 「台阶定位」= 从基准端竖线到台阶竖线的 **X 向距离**。
                #   首选 AddDimension2(两条竖线) —— 跨图元量距，落点干净；
                #   拿不到竖线时才退回 AddAlongXDimension（单图元到轴/原点距离）。
                val = item["value"]
                i1, i2 = item.get("i1"), item.get("i2")
                m = margin if margin is not None else max(4.0, 0.30 * val)
                sp = None
                if i1 is not None and i2 is not None and i1 != i2:
                    sp, _e = _dim_between(sw, ctx, geoms, i1, i2, "alongX", val, m)
                if sp is None:
                    # --- 降级：单图元 X 位置 ---
                    idx = item.get("index")
                    if idx is None:
                        idxs = _vert_xs(geoms, item.get("x"))
                        idx = idxs[0] if idxs else None
                    if idx is None:
                        skipped.append({"index": -1, "why": f"台阶 {item.get('x')} 找不到竖线"})
                        continue
                    g = geoms[idx]
                    a1, b1 = g["start_mm"], g["end_mm"]
                    midx, midy = (a1[0] + b1[0]) / 2.0, (a1[1] + b1[1]) / 2.0
                    place = [midx, midy + m, 0.0]
                    # ★ 同 balance：单图元兜底前必须先确认"真的选中了"，
                    #   否则尺寸打在空选择上会把 SolidWorks 直接打崩（见 balance 注释）。
                    ok_sel, _selog, _seg = _select_one_strict(sw, ctx, idx)
                    if not ok_sel or sw.selected_count() != 1:
                        skipped.append({"index": idx,
                                        "why": f"台阶定位：竖线 {idx} 选不中 ⇒ 跳过"})
                        continue
                    got, err = _call(member, _sm(sw), "AddAlongXDimension",
                                     mm(place[0]), mm(place[1]), mm(place[2]))
                    if got is None:
                        skipped.append({"index": idx,
                                        "why": f"台阶定位：AddAlongXDimension 失败（{err}）⇒ 跳过"})
                        continue
                    dim_obj = got
                    with contextlib.suppress(Exception):
                        d2 = member(got, "GetDimension2", 0)
                        if d2 is not None:
                            dim_obj = d2
                    _call(member, dim_obj, "SetSystemValue3", mm(val), 2, None)
                    sp = {"api": "AddAlongXDimension", "index": idx}
                sp.update({"kind": "line", "as": "alongX", "value_mm": round(val, 4),
                           "tag": item.get("tag")})
                made.append(sp)
            elif t == "balance":
                # ★ 总长：**左端面竖线 ↔ 右端面竖线** 的 X 向尺寸。
                #   真机踩到：用 AddAlongXDimension 打在单根母线上，会和该线的 Y
                #   半径尺寸抢同一根图元（互相顶掉）；跨两条竖线的 AddDimension2
                #   才语义唯一、落点分离。
                val = item["value"]
                i1, i2 = item.get("i1"), item.get("i2")
                sp = None
                if i1 is not None and i2 is not None and i1 != i2:
                    sp, _e = _dim_between(sw, ctx, geoms, i1, i2, "alongX", val, 12.0)
                if sp is None:
                    vs = []
                    for g in geoms:
                        if g.get("kind") != "line" or g.get("construction"):
                            continue
                        a, b = g.get("start_mm"), g.get("end_mm")
                        if not a or not b:
                            continue
                        if abs(a[0] - b[0]) < 1e-6 and abs(b[1] - a[1]) > 1e-9:
                            vs.append(g)
                    if not vs:
                        skipped.append({"index": -1, "why": "找不到端面竖线来承载总长"})
                        continue
                    hi = max(vs, key=lambda g: g["start_mm"][0])
                    place = [hi["start_mm"][0] / 2.0, _top_y(geoms) + 12.0, 0.0]
                    # ★★★ 2026-10-02 真机崩溃点（隔离诊断 `_diag/diag_shaft_bbox.py` 复现）：
                    #   这里原本 `_select_one_strict` 完**不看结果**就调 `AddAlongXDimension`。
                    #   端面配对 `i1/i2` 取不到时（轴的端面被 45° 倒角切成斜线 ⇒
                    #   `_span_pair` 回 None）会退到这条单图元兜底；而 `geoms` 是**重进
                    #   草图之前**读的，`_remeasure_revolve_feature` 退出又重进后图元被
                    #   重建、索引失效 ⇒ 选不中 ⇒ 单图元标注打在空选择上 ⇒
                    #   **SolidWorks 进程直接崩**（`-2147023170 RPC_S_CALL_FAILED`，随后
                    #   `-2147023174 RPC 服务器不可用`，之后所有读数回 None —— 表现为
                    #   "读不到包围盒"，其实零件是空的）。
                    #   崩在 API 内部，异常**接不住**；唯一办法是**别做这次调用**：
                    #   选中数 != 1 就跳过 + 记原因，让 `expect_min_dims` 闸门去发现。
                    ok_sel, _selog, _seg = _select_one_strict(sw, ctx, hi["index"])
                    if not ok_sel or sw.selected_count() != 1:
                        skipped.append({"index": hi["index"],
                                        "why": f"总长：端面竖线 {hi['index']} 选不中"
                                               f"（重进草图后索引可能失效）⇒ 跳过，"
                                               f"不冒险调用会崩 SW 的单图元标注"})
                        continue
                    got, err = _call(member, _sm(sw), "AddAlongXDimension",
                                     mm(place[0]), mm(place[1]), mm(place[2]))
                    if got is None:
                        skipped.append({"index": hi["index"],
                                        "why": f"总长：AddAlongXDimension 失败（{err}）⇒ 跳过"})
                        continue
                    dim_obj = got
                    with contextlib.suppress(Exception):
                        d2 = member(got, "GetDimension2", 0)
                        if d2 is not None:
                            dim_obj = d2
                    _call(member, dim_obj, "SetSystemValue3", mm(val), 2, None)
                    sp = {"api": "AddAlongXDimension", "index": hi["index"],
                          "witness": hi["index"]}
                sp.update({"kind": "line", "as": "alongX", "value_mm": round(val, 4),
                           "tag": item.get("tag", "总长")})
                made.append(sp)
        except SwError as e:
            raise SwError(f"[{t}] {item.get('tag')}（index={idx}，值 "
                          f"{item.get('value')}）标尺寸失败：{e}\n"
                          f"  ▸ 尺寸链诊断：{json.dumps(diag, ensure_ascii=False)[:700]}\n"
                          f"  ▸ 已标：{json.dumps(made, ensure_ascii=False)[:400]}")

    ctx["dimmed_segments"] = sorted({m["index"] for m in made if m.get("index") is not None
                                     and m["index"] >= 0}
                                    | {m["index"] for m in spans if m.get("index") is not None})
    st = _constrained_status(sw)
    n_total = len(_all_dimensions(sw))
    out = {"n_dimmed": len(made), "dims": made,
           "n_dimmed_span": len(spans), "spans": spans,
           "n_segments": len(segs), "n_goals": len(goal),
           "n_dims_total": n_total,
           "chain": diag,
           "skipped": skipped,
           "status": st, "status_meaning": CONSTRAINED_STATUS.get(st),
           "fully_defined": st == 3}
    mode = diag.get("mode")
    if mode == "revolve_halfsection":
        notes = [f"本张草图共 {len(segs)} 个图元，是**回转体半剖面**。按尺寸链只标了 "
                 f"{len(made) + len(spans)} 条（总长/台阶 {len(made)} + 半径 {len(spans)}），"
                 f"中间的段长都是派生量，**一条都没标**。"]
        notes.append("★ 尺寸链规则（图纸习惯）：**总长只标一次 + 台阶定位 + 各段 Ø**；"
                     "中间段长 = 派生量 ⇒ 逐段累加会 Σ=总长⇒过定义，SW 会静默拒标。")
        notes.append("★ 落点分离：横向跨**两根竖线**、纵向跨**轴线+母线** —— 两套尺寸不共用图元，"
                     "所以不会互相顶掉。")
    else:
        notes = [f"本张草图共 {len(segs)} 个图元。逐个图元标注（非回转体，未启用尺寸链）："
                 f"实际标上 {len(made) + len(spans)} 条。"]
        notes.append("★ 尺寸链（回转体半剖面）只在识别出**圆柱面**时才启用 —— 圆/弧/齿轮"
                     "轮廓不适用，走的是这套逐个标注。")
    if not out["fully_defined"]:
        notes.append("标完仍**不是**完全定义 —— 尺寸只约束了大小，定位还得靠关系/锚点。"
                     "可再调 `fully_define(require=false)`，或用 `anchor` 固定圆心。")
    out["notes"] = notes
    sw.clear_selection()
    return out


@op("gear_profile", "齿轮齿廓（渐开线）", "sketch",
    "★ 一次性画出整圈的渐开线齿轮齿廓。这是「参数 → 几何」的样板：\n"
    "   1. sw_gear.GearParams 算 m/z/d/da/df/db 和每齿的半齿厚角\n"
    "   2. sw_gear.outline_closed() 采出整圈**无重复点**的点表\n"
    "   3. 按 mode 落成 SolidWorks 图元\n"
    " mode 二选一（默认 polyline）：\n"
    "   polyline —— 逐段直线串成整圈。**几何精确**（点表内接于真实渐开线，\n"
    "               误差 <0.01mm），代价是图元多（z=55 时约 770 条）。拉伸最稳。\n"
    "   spline   —— 一整条闭合样条。只有 1 个图元、曲线光滑，但插值**会向外过冲\n"
    "               ~0.2mm**（实测 da 114.0 → 114.4），不适合当精确齿形用。\n"
    " 整圈一次成形，不需要草图阵列、不需要修剪 —— 相邻齿共用边界时\n"
    " SolidWorks 判定轮廓粘连，拉伸容易报「轮廓开放/自相交」。\n"
    " 校验：z<17 不变位会根切（sw_gear 会报出来）。",
    api=["ISketchManager.CreateLine", "ISketchManager.CreateSpline3",
         "ISketchManager.CreateCircleByRadius"],
    params={"mn": {"type": "float", "unit": "mm", "desc": "法向模数"},
            "z": {"type": "int", "desc": "齿数"},
            "beta": {"type": "float", "unit": "deg", "required": False, "default": 0},
            "alpha_n": {"type": "float", "unit": "deg", "required": False, "default": 20},
            "x": {"type": "float", "required": False, "default": 0, "desc": "变位系数"},
            "mode": {"type": "str", "required": False, "default": "polyline",
                     "desc": "polyline=折线(精确) / spline=单条闭合样条(光滑但过冲)"},
            "n_flank": {"type": "int", "required": False, "default": 6,
                        "desc": "单侧齿廓取样点数，越大越细"},
            "n_root": {"type": "int", "required": False, "default": 2,
                       "desc": "齿根弧取样点数"},
            "n_tip": {"type": "int", "required": False, "default": 3,
                      "desc": "齿顶弧取样点数"},
            "bore": {"type": "float", "unit": "mm", "required": False, "desc": "中心孔直径"},
            "keyway": {"type": "str", "required": False, "desc": "键槽 '8x3.5'"},
            "strict": {"type": "bool", "required": False, "default": True,
                       "desc": "有 error 级诊断就中止"}},
    requires=("sketch_open",), verified=True)
def op_gear_profile(sw, step, ctx):
    g = sw_gear.GearParams(
        mn=float(_p(step, ctx, "mn")), z=int(_p(step, ctx, "z")),
        beta=float(_p(step, ctx, "beta", 0) or 0),
        alpha_n=float(_p(step, ctx, "alpha_n", 20) or 20),
        x=float(_p(step, ctx, "x", 0) or 0),
        n_flank=int(_p(step, ctx, "n_flank", 6) or 6),
        bore=_p(step, ctx, "bore"), keyway=_p(step, ctx, "keyway"))
    diags = g.diagnostics()
    ctx["gear"] = g
    ctx["gear_diagnostics"] = diags
    if _p(step, ctx, "strict", True):
        errs = [d for d in diags if d["level"] == "error"]
        if errs:
            raise SwError("齿轮参数有硬错误，已中止：\n  " + "\n  ".join(
                f"[{d['code']}] {d['msg']}" for d in errs))

    mode = str(_p(step, ctx, "mode", "polyline") or "polyline").lower()
    # ★ 必须用 outline_closed()：full_outline() 在齿与齿根弧的接头处端点重复，
    #   直接逐点连直线会产生 110 条零长度线段（SolidWorks 静默丢弃），
    #   而且 GetSketchContourCount 会因此算出 55 个碎轮廓。
    pts = g.outline_closed(
        per_flank=int(_p(step, ctx, "n_flank", 6) or 6),
        n_root=int(_p(step, ctx, "n_root", 2) or 2),
        n_tip=int(_p(step, ctx, "n_tip", 3) or 3),
        closed=True)
    sm = _sm(sw)
    made = 0

    if mode == "spline":
        flat: list[float] = []
        for x, y in pts:
            flat += [mm(x), mm(y), 0.0]
        s = sm.CreateSpline3(variant_r8(flat), None, None, False, None)
        if s is None:
            raise SwError("闭合样条创建失败")
        ctx.setdefault("segments", []).append(s)
        made = 1
    else:
        # 逐段直线。首尾用 [i], [i+1] 走一圈 —— pts 已经带了闭合重复点，
        # 所以最后一段自然落回起点，形成封闭轮廓。
        for i in range(len(pts) - 1):
            x1, y1 = pts[i]
            x2, y2 = pts[i + 1]
            s = sm.CreateLine(mm(x1), mm(y1), 0, mm(x2), mm(y2), 0)
            if s is not None:
                ctx.setdefault("segments", []).append(s)
                made += 1
    if made == 0:
        raise SwError(f"齿廓一个图元都没画出来（mode={mode}, 点数={len(pts)}）")

    # 中心孔
    bore = _p(step, ctx, "bore")
    if bore:
        seg = sm.CreateCircleByRadius(0, 0, 0, mm(float(bore) / 2.0))
        ctx.setdefault("segments", []).append(seg)
    # 键槽：**故意不在这里画**。
    # 矩形横跨孔壁时，会和孔圆形成「两个互相重叠的闭环」，SolidWorks 直接判成
    # 自相交轮廓 —— 实测「齿轮外轮廓 + 孔 + 键槽」同草图 → 3 个轮廓 → 拉伸失败。
    # 正确做法：单独开一张草图用 keyway_rect 画矩形，再 extrude_cut。
    kw = _p(step, ctx, "keyway")
    if kw:
        raise SwError(
            "键槽不能和齿廓/中心孔画在同一张草图里："
            "矩形压在孔壁上会形成两个重叠闭环，SolidWorks 判定「轮廓自相交」，拉伸必然失败。\n"
            "  改法：gear_profile 去掉 keyway 参数 → extrude_boss 出齿轮体 →\n"
            "        select_plane + sketch_begin → keyway_rect(bore, spec) → sketch_end\n"
            "        → extrude_cut(end_cond=swEndCondThroughAll, reverse_dir=True)")
    return {"teeth": g.z, "mode": mode, "points": len(pts), "entities": made,
            "da": round(g.da, 4), "diagnostics": diags, "params": g.summary()}


@op("keyway_rect", "键槽矩形", "sketch",
    "画平键键槽的矩形（孔心为原点，槽开在 +y 方向）。**必须单独开一张草图 + 单独切除**，"
    "不要和孔/外轮廓画在一起 —— 矩形横跨孔壁会形成两个重叠闭环，SolidWorks 判自相交。",
    api=["ISketchManager.CreateCenterRectangle"],
    params={"bore": {"type": "float", "unit": "mm", "desc": "孔直径"},
            "spec": {"type": "str", "desc": "键槽规格 '8x3.5' = 键宽x毂槽深"},
            "cx": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "cy": {"type": "float", "unit": "mm", "required": False, "default": 0}},
    requires=("sketch_open",), verified=True)
def op_keyway_rect(sw, step, ctx):
    bore = float(_p(step, ctx, "bore"))
    if bore <= 0:
        raise SwError("孔直径必须 > 0")
    kp = sw_gear.keyway_profile(bore, str(_p(step, ctx, "spec")))
    cx, cy = float(_p(step, ctx, "cx", 0) or 0), float(_p(step, ctx, "cy", 0) or 0)
    r = bore / 2.0
    half = kp["width"] / 2.0
    bottom = math.sqrt(max(r * r - half * half, 0.0))
    top = r + kp["depth"]
    # CreateCenterRectangle = 中心点 + 一个角点
    s = _sm(sw).CreateCenterRectangle(mm(cx), mm(cy + (bottom + top) / 2.0), 0,
                                      mm(cx + half), mm(cy + top), 0)
    if s is None:
        raise SwError("键槽矩形创建失败")
    ctx.setdefault("segments", []).append(s)
    return {"width": kp["width"], "depth": kp["depth"],
            "y_from": round(bottom, 4), "y_to": round(top, 4)}


@op("shaft_profile", "阶梯轴半剖面（回转用，支持锥段）", "sketch",
    "★★ **阶梯轴的正统画法**：按「段表」一次性画出半剖面（含中心线），\n"
    "   紧接一个 `revolve_boss` 就成轴。这正是用户参照件的做法\n"
    "   （`20-轴.SLDPRT`：草图1 → 旋转1；`23-齿轮轴.SLDPRT`：草图1 → 旋转1）。\n"
    "★ 为什么不用「一层层拉伸再合并」：段表一次成形的特征树只有**一个**旋转特征，\n"
    "  没有多余台阶面、没有合并留下的接缝；而且每段的直径/长度都能被 `smart_dim`\n"
    "  标出来驱动 —— 改一个尺寸整根轴跟着长，这是「防变形」需求真正落地的地方。\n"
    "★ `segments` = `[{\"d\": 直径, \"l\": 长度}, ...]`，**从 x=0 端向 +X 依次排开**。\n"
    "  所有段首尾相接，总长 = Σl。画完自动把轮廓沿轴线闭合。\n"
    "★ 中心线默认画在 y=0（从 x=0 到总长），所以 `revolve_boss` **不用传 axis**。\n"
    "★★ `end_chamfer` = 两个**最外端**的 45° 倒角（只倒头尾，不倒中间台阶）。\n"
    "   参照件 `20-轴.SLDPRT` 两端就是 2×45°（Ø30→Ø26、Ø24.317→Ø20.3），\n"
    "   实测两端各占 2mm，所以总长 = Σl + 2×end_chamfer（144 = 140 + 2×2）。\n"
    "   ★ 为什么不画完再调 `chamfer` op：圆柱口的闭环边没有顶点，`_edge_mid` 只能退到\n"
    "     **曲线参数中点**，位置不可预测 —— 按坐标选这种边很脆。画进剖面最稳。\n"
    "★ 中间的台阶过渡（`chamfer` 参数）是「每段两端各倒一次」，跟 `end_chamfer` 不是一回事。",
    api=["ISketchManager.CreateLine", "ISketchManager.CreateCenterLine"],
    params={"segments": {"type": "list",
                         "desc": "[{\"d\": 直径, \"l\": 长度}, ...]，单位 mm"},
            "end_chamfer": {"type": "float", "unit": "mm", "required": False, "default": 0,
                            "desc": "两端的 45° 倒角（只倒头尾，计入总长）"},
            "chamfer": {"type": "float", "unit": "mm", "required": False, "default": 0,
                        "desc": "每段两端的过渡倒角（一般给 0）"},
            "centerline": {"type": "bool", "required": False, "default": True,
                           "desc": "是否画中心线（回转轴）"}},
    requires=("sketch_open",), verified=True)
def op_shaft_profile(sw, step, ctx):
    raw = _p(step, ctx, "segments") or []
    if not raw:
        raise SwError("segments 不能为空 —— 每段至少给 {\"d\": 直径, \"l\": 长度}")
    segs: list[dict] = []
    for i, s in enumerate(raw):
        if not isinstance(s, dict) or s.get("d") is None or s.get("l") is None:
            raise SwError(f"第 {i+1} 段要写成 {{\"d\": 直径, \"l\": 长度}}，收到 {s!r}")
        _sg = {"d": float(s["d"]), "l": float(s["l"]), "name": s.get("name")}
        # ★ 锥段：可选 `d2`（段末直径）。不给就是圆柱 —— 向后兼容。
        if s.get("d2") is not None:
            d2 = float(s["d2"])
            if d2 <= 0:
                raise SwError(f"第 {i+1} 段 d2={d2} 必须 > 0")
            if d2 == _sg["d"]:
                raise SwError(
                    f"第 {i+1} 段同时给了 d 与 d2 且相等（都是 {d2:g}）—— "
                    f"那是圆柱，**删掉 d2**（留着会被当成锥段，白费一次锥面公差）")
            _sg["d2"] = d2
        segs.append(_sg)
    ch = float(_p(step, ctx, "chamfer", 0) or 0)
    g = sw_gear.stepped_shaft(segs, chamfer=ch)      # 只借用它的入参校验与 Σl
    base_len = float(g["length"])
    note: list[str] = []
    ec = float(_p(step, ctx, "end_chamfer", 0) or 0)
    rs = [s["d"] / 2.0 for s in segs]
    # 锥段的段末半径
    re_ = [s["d2"] / 2.0 if s.get("d2") else s["d"] / 2.0 for s in segs]
    if ec > 0 and (ec >= rs[0] - 1e-9 or ec >= rs[-1] - 1e-9):
        raise SwError(f"end_chamfer={ec} 太大：两端半径只有 {rs[0]}/{rs[-1]}")
    # ★ 端部倒角是**外扩**：段表里的 l 只算「圆柱段」，45° 倒角额外各加 ec 长度。
    #   ⇒ 总长 = Σl + 2·ec（参照件 20-轴：140 + 2×2 = 144）。
    #   真机踩过：先按「内切」实现，总长只有 Σl，对不上参照件的 144 —— 是画法问题，
    #   不是几何问题，肉眼根本看不出来，只有量总长才发现。
    pts: list[tuple[float, float]] = []
    if ec > 0:
        pts.append((0.0, rs[0] - ec))
        pts.append((ec, rs[0]))
        x = ec
    else:
        pts.append((0.0, rs[0]))
        x = 0.0
        note.append("未加端部倒角")
    for i, s in enumerate(segs):
        # ★★ **锥段支持**（2026-10-02，用户 23-齿轮轴图纸上有 1:20 拔模段）：
        #   段上多给一个 `d2` ⇒ 该段是**锥**（从 d 锥到 d2，长度仍是 l）。
        #   只给 `d`（不写 d2）⇒ 圆柱，**行为与以前一字不变**（向后兼容）。
        r2 = s["d2"] / 2.0 if s.get("d2") else rs[i]
        if i > 0:
            pts.append((round(x, 9), rs[i]))     # 台阶的竖边
        if abs(r2 - rs[i]) > 1e-9:
            # 锥段：段末半径不同 ⇒ 这一段画的是**斜线**，回转出来就是圆锥台
            x = round(x + s["l"], 9)
            pts.append((x, r2))
            note.append(f"第 {i+1} 段是**锥段** Ø{s['d']:g}→Ø{s['d2']:g}（长 {s['l']:g}）")
        else:
            x = round(x + s["l"], 9)
            pts.append((x, rs[i]))
    if ec > 0:
        pts.append((x, rs[-1] - ec))             # 末端 45°
        x = round(x + ec, 9)
        note.append(f"两端并入 45° 倒角 {ec}mm（各占 {ec}mm 长度）")
    pts.append((x, 0.0))
    pts.append((0.0, 0.0))
    # ★ `stepped_shaft` 只回到轴线 (0,0)，**不会替你闭合到起点** —— 轮廓仍是开的。
    #   回转要求闭合轮廓，所以这里必须自己补最后一段 (0,0) → 起点。
    if abs(pts[-1][0] - pts[0][0]) > 1e-9 or abs(pts[-1][1] - pts[0][1]) > 1e-9:
        pts = pts + [pts[0]]
    total = round(x, 4)
    sm = _sm(sw)
    made = 0
    dups = 0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if abs(x1 - x2) < 1e-9 and abs(y1 - y2) < 1e-9:
            dups += 1
            continue                      # 倒角算出的重合点，跳过（CreateLine 会画 0 长度段）
        s = sm.CreateLine(mm(x1), mm(y1), 0, mm(x2), mm(y2), 0)
        if s is not None:
            ctx.setdefault("segments", []).append(s)
            made += 1
    if made == 0:
        raise SwError("半剖面一段都没画出来 —— segments 内容不合法？")
    if _p(step, ctx, "centerline", True):
        # 中心线在 y=0（就是回转轴）。★ 它和轮廓的闭合边重合，但**构造几何不参与轮廓**，
        #   真机验证过可以正常回转；若哪天报「轮廓与中心线重合」，把 centerline 关掉、
        #   改用 revolve_boss 的 axis 选草图边。
        c = sm.CreateCenterLine(mm(0), 0, 0, mm(total), 0, 0)
        ctx.setdefault("segments", []).append(c)
        ctx["axis_segment"] = c
    return {"length": round(total, 4), "base_length": base_len, "end_chamfer": ec,
            "n_segments": len(segs), "entities": made, "skipped_dup": dups,
            "profile_points": len(pts), "notes": note,
            "diameters": [s["d"] for s in segs], "lengths": [s["l"] for s in segs]}


# ============================================================ 特征类


def _resolve_end_cond(C, value, default: str = "swEndCondBlind") -> int:
    """结束条件可以是枚举名，也可以是宏翻译出来的**数值**（SW 录制时常写成 6/1）。

    直接 C.get("6") 会静默拿到默认值 —— 那是「拉伸深度明明给了却变成盲孔 0mm」
    这类怪现象的根因，所以这里必须显式区分。
    """
    if value is None or value == "":
        value = default
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    return C.get(s, 0)


def _volume(sw) -> float | None:
    """零件体积（m³）。拿不到返回 None。

    ★ 不要用 0 冒充「读不到」：切除之后要靠**体积变化**判断到底有没有咬到材料，
      把「读失败」当成 0 既会把成功判成失败，也会把失败漏过去。
    ★★ `fresh=True`（2026-10-12）：**先重建再读**。旧行为在「体积非 0 但陈旧」时
      会采信陈旧值 ⇒ 特征守门（`abs(vol1-vol0)<1e-12` 判「没咬到材料」）会看错。
      见 `SwSession.mass_props` 的长注释与 `_diag/plans/plan_grid.json` 的现场。
    """
    with contextlib.suppress(Exception):
        v = sw.mass_props(fresh=True).get("volume_m3")
        if v is not None:
            return float(v)
    return None


def _drop_feature(sw, feat) -> bool:
    """把**明确报错**的特征从模型树里删掉。

    一个带错误标记的特征留在树里会连累后面：feature_names 变脏、选择变难、
    保存下来的是个带错误的零件。只在 GetErrorCode 非 0 时调用；删不掉也不致命。
    """
    try:
        name = str(member(feat, "Name"))
        sw.clear_selection()
        if not _select_feature(sw, name, 0):
            return False
        return bool(member(sw.model, "EditDelete"))
    except Exception:                                        # noqa: BLE001
        return False


def _call_extrude(sw, v: dict, cut: bool):
    """按一组参数真正调一次 FeatureCut4 / FeatureExtrusion3。

    ★ 参数个数必须对着类型库数：
        FeatureCut4       —— 27 参（IFeatureManager 版）
        FeatureExtrusion3 —— 23 参（IFeatureManager 版）
      少一个/多一个都报 DISP_E_PARAMNOTOPTIONAL「非选择性的参数」，
      这个报错完全指不到根因。核对：python scripts/sw_memberkind.py --sig FeatureCut4
    """
    fm = _fm(sw)
    fn = fm.FeatureCut4 if cut else fm.FeatureExtrusion3
    # ★ 2026-10-04 新增：带拔模的拉伸（锥台）
    #   FeatureExtrusion3 的 8~13 号参数就是拔模开关/方向/角度，原先是**硬写关闭**：
    #     8  Dchk1 = True 启用方向1拔模    9  Dchk2 方向2
    #     10 Ddir1 = True 向外拔模 / False 向内   11 Ddir2
    #     12 Dang1 方向1拔模角（**弧度**） 13 Dang2
    #   v["taper"] 缺省 None ⇒ 完全等同旧行为（全 False / 0.0），不影响任何既有计划。
    taper = v.get("taper")
    if taper:
        dchk1, ddir1, dang1 = True, bool(v.get("taper_outward", False)), deg(float(taper))
    else:
        dchk1, ddir1, dang1 = False, False, 0.0
    common = (
        bool(v["Sd"]),          # 1  Sd    True = 只用方向1（单向）
        bool(v["Flip"]),        # 2  Flip  反侧切除
        bool(v["Dir"]),         # 3  Dir   反向
        v["T1"], v["T2"],       # 4  T1    5  T2   终止条件 swEndConditions_e
        v["D1"], v["D2"],       # 6  D1    7  D2   深度（米）
        dchk1, False,           # 8  Dchk1 9  Dchk2
        ddir1, False,           # 10 Ddir1 11 Ddir2
        dang1, 0.0,             # 12 Dang1 13 Dang2
        False, False,           # 14 OffsetReverse1 15 OffsetReverse2
        False, False,           # 16 TranslateSurface1 17 TranslateSurface2
    )
    if cut:
        return fn(*common,
                  False,                 # 18 NormalCut（钣金用法，实体切除给 False）
                  False, True,           # 19 UseFeatScope 20 UseAutoSelect
                  True, True, True,      # 21 AssemblyFeatureScope
                                         # 22 AutoSelectComponents
                                         # 23 PropagateFeatureToParts
                  0, 0.0, False,         # 24 T0=0 从草图平面起 25 StartOffset 26 FlipStartOffset
                  True)                  # 27 OptimizeGeometry
    return fn(*common,
              bool(v["Merge"]),          # 18 Merge
              False, True,               # 19 UseFeatScope 20 UseAutoSelect
              0, 0.0, False,             # 21 T0=0 22 StartOffset 23 FlipStartOffset
              )


def _extrude_variants(sw, step, ctx, cut: bool) -> list[dict]:
    """候选方案序列：同一张草图，换「终止条件 + 方向」再试一次。

    为什么必须有备选
    ----------------
    同一张草图，SolidWorks 对不同终止条件的接受度并不一致：草图平面不骑在材料上、
    实体只朝一侧长、「完全贯穿-两者」在某些拓扑下算不出终止面…… 主方案失败时
    死磕只会白等。这里按「最常用 → 最保险」排序逐个试，谁先成功用谁。

    ★ Sd（Single Direction）这个坑值一整段注释：
      实测 `Sd=True` 会把「完全贯穿-两者 / 两侧对称」**静默降级成单向** ——
      特征照样进模型树、GetErrorCode 也不报错，只是**只切了一半**。
      齿轮键槽「只开半边」的真凶就是它。所以 Sd 必须由终止条件反推，
      不能只看调用方有没有显式给 both_directions。
    """
    C = sw.C
    base = _resolve_end_cond(C, _p(step, ctx, "end_cond", "swEndCondBlind"))
    flip = bool(_p(step, ctx, "flip_side", False))
    rev = bool(_p(step, ctx, "reverse_dir", False))
    both = bool(_p(step, ctx, "both_directions", False))
    merge = bool(_p(step, ctx, "merge", True))
    depth = mm(float(_p(step, ctx, "depth", 0) or 0))
    # ★ 拔模角（锥台）：taper 为**角度(度)**，None = 不拔模（默认，行为与旧版逐位一致）
    taper = _p(step, ctx, "taper", None)
    taper_out = bool(_p(step, ctx, "taper_outward", False))

    BOTH = {C.get("swEndCondThroughAllBoth"), C.get("swEndCondMidPlane")}
    THRU = C.get("swEndCondThroughAll")
    TAB = C.get("swEndCondThroughAllBoth")
    MID = C.get("swEndCondMidPlane")
    BLIND = C.get("swEndCondBlind")

    def one(t1, sd, dirv, d1, t2=0, d2=0.0, tag=""):
        # ★ taper/taper_outward 走同一条通道（见 _call_extrude 的 8~13 号参数）
        return {"T1": t1, "T2": t2, "Sd": sd, "Dir": dirv, "D1": d1, "D2": d2,
                "Flip": flip, "Merge": merge, "tag": tag,
                "taper": taper, "taper_outward": taper_out}

    sd0 = not (both or base in BOTH)          # ← 双向终止条件必须 Sd=False
    out = [one(base, sd0, rev, depth,
               base if not sd0 else 0, depth if not sd0 else 0.0, "主方案")]
    if not cut:
        return out
    # 以下只在切除时作为备选 —— 「切不到材料 / 方向反了」是最常见的一类失败。
    if base in BOTH or base == THRU:
        out.append(one(THRU, True, not rev, 0, 0, 0.0, "备选1 单向完全贯穿+反向"))
        out.append(one(TAB, False, rev, 0, TAB, 0.0, "备选2 双向完全贯穿-两者"))
        out.append(one(MID, False, rev, max(depth, mm(100)), MID,
                       max(depth, mm(100)), "备选3 两侧对称 ±100mm"))
    elif base == BLIND and depth > 0:
        # ★★ 2026-09-25 真机踩到：盲孔切除**原先一个备选都没有** ——
        #   而「草图平面骑在实体上、方向却朝着空气」恰恰是盲切最常见的失败。
        #   齿轮环形凹槽就栽在这（主方案返回 None，然后直接抛，白等一轮）。
        #   ★ 这里**只翻方向、绝不改终止条件**：把盲孔悄悄降级成「完全贯穿」
        #     会把零件切穿，而体积照样变小 ⇒ 现有的「体积必须变小」这条闸门
        #     根本拦不住。宁可失败得响一点。
        out.append(one(BLIND, True, not rev, depth, 0, 0.0, "备选1 盲孔+反向"))
    return out


def _extrude(sw, step, ctx, cut: bool):
    # 不写 profile 就用「刚刚画的那张草图」——这是最常用也最容易踩坑的默认值：
    # SolidWorks 的特征命令只认**当前选择**，所以每次尝试前都得重新把草图选上。
    prof = _p(step, ctx, "profile") or ctx.get("last_sketch")
    variants = _extrude_variants(sw, step, ctx, cut)
    vol0 = _volume(sw) if cut else None
    notes: list[str] = []
    f = None
    used = ""
    for v in variants:
        if prof and not _select_feature(sw, prof, 0):
            raise SwError(f"选不中要拉伸的草图 '{prof}'")
        cand = None
        with contextlib.suppress(Exception):
            cand = _call_extrude(sw, v, cut)
        sw.clear_selection()
        if cand is None:
            notes.append(f"{v['tag']}：SolidWorks 返回 None")
            continue
        ec = 0
        with contextlib.suppress(Exception):
            ec = int(member(cand, "GetErrorCode"))
        if ec:
            notes.append(f"{v['tag']}：特征重建失败 {ec}"
                         f"（{FEATURE_ERROR_NAMES.get(ec, '未知')}）")
            _drop_feature(sw, cand)
            continue
        # ★ 特征「成功」≠「形状对」。切除必须验证**体积真的变小了** ——
        #   实测存在特征无错、却没咬到材料的情况（方向穿到实体外面去了）。
        if cut and vol0 is not None:
            vol1 = _volume(sw)
            if vol1 is not None and abs(vol1 - vol0) < 1e-12:
                notes.append(f"{v['tag']}：特征成功但**体积没变**（没切到材料）")
                _drop_feature(sw, cand)
                continue
        f, used = cand, v["tag"]
        break

    if f is None:
        sk = prof or ctx.get("last_sketch")
        n = sw.sketch_contour_count(sk) if sk else None
        hint = ""
        if n is not None and n > 1:
            # ★★ 别一律说「轮廓多是错的」（2026-09-25 真机打脸）：环形凹槽、法兰、
            #   带孔板天生就是 2 个以上**嵌套**轮廓，SolidWorks 照常拉伸。
            #   真正致命的是「多个轮廓互相重叠」或「多段图元没粘起来」。
            hint = (f"\n  草图轮廓数 {n} —— **多轮廓本身多半是正常的**（外轮廓 + 孔"
                    "就是环，法兰/带孔板/环形凹槽都这样）。\n"
                    "  只有在报「未闭合 / 自相交」时才怀疑：① 多段图元的端点没粘合"
                    "（样条端点**不会**自动吸附，圆弧会）；\n"
                    "  ② 两个闭环互相重叠（典型：键槽矩形压在孔圆上）。"
                    "见 references/30-pitfalls.md「多段图元拼不成一个轮廓」。")
        detail = "".join(f"    · {x}\n" for x in notes)
        raise SwError(
            f"{'切除' if cut else '拉伸'}失败，{len(variants)} 个方案全部没成：\n"
            + detail
            + f"  草图轮廓数：{n}" + hint
            + ("\n  排查顺序：① 草图是不是闭合轮廓 ② 方向对不对"
               "（reverse_dir / flip_side）\n"
               "            ③ 草图平面是否骑在材料上 —— 不在材料上，"
               "任何终止条件都切不动" if cut else ""))

    name = _p(step, ctx, "name") or str(member(f, "Name"))
    if _p(step, ctx, "name"):
        with contextlib.suppress(Exception):
            f.Name = _p(step, ctx, "name")
    ctx.setdefault("feature_names", []).append(name)
    ctx["last_feature"] = name
    if cut:
        ctx["last_cut"] = name
    return {"feature": name, "depth": _p(step, ctx, "depth"),
            "variant": used, "notes": notes or None}


@op("extrude_boss", "拉伸凸台", "feature",
    "把一个闭合草图沿法向拉成实体。**必须先选中草图**（profile 可给名字）。\n"
    "end_cond 用 swEndConditions_e 的名字：swEndCondBlind(盲孔/给定深度)、"
    "swEndCondMidPlane(两侧对称)、swEndCondThroughAll(完全贯穿)、swEndCondUpToNext 等。\n"
    "坑：depth 单位是**米**，这里已经帮你按 mm 换算；两侧对称要用 swEndCondMidPlane 而不是给负深度。\n"
    "★ `taper`（可选）= 拔模角(度)，把拉伸做成**锥台**（铸件凸台的脱模斜度）：\n"
    "   默认 `taper_outward=False` ⇒ **向内收**（远离草图平面的截面变小）；\n"
    "   要「底大顶小」就画**大**的那个截面当草图、给 taper。\n"
    "   实测（2026-10-04）：箱盖窥视孔凸台用 10° 拔模，顶面 46×46 由 51.65 的草图反推。",
    api=["IModelDocExtension.SelectByID2", "IFeatureManager.FeatureExtrusion3"],
    params={"profile": {"type": "str", "required": False, "desc": "草图名，不给用当前选择"},
            "depth": {"type": "float", "unit": "mm", "required": False,
                      "desc": "盲孔深度；贯通类条件可省"},
            "end_cond": {"type": "str", "required": False, "default": "swEndCondBlind"},
            "flip_side": {"type": "bool", "required": False, "default": False},
            "reverse_dir": {"type": "bool", "required": False, "default": False},
            "both_directions": {"type": "bool", "required": False, "default": False},
            "merge": {"type": "bool", "required": False, "default": True},
            "taper": {"type": "deg", "required": False,
                      "desc": "拔模角(度)，把拉伸做成锥台；默认向内收"},
            "taper_outward": {"type": "bool", "required": False, "default": False,
                              "desc": "True=向外拔模（远离草图平面变大）"},
            "name": {"type": "str", "required": False}},
    requires=("sketch_closed",), effect=("has_feature",), verified=True)
def op_extrude_boss(sw, step, ctx):
    return _extrude(sw, step, ctx, cut=False)


@op("extrude_cut", "拉伸切除", "feature",
    "用闭合草图挖掉材料。参数同拉伸凸台。挖中心孔、键槽、减重孔都用它。",
    api=["IModelDocExtension.SelectByID2", "IFeatureManager.FeatureCut4"],
    params={"profile": {"type": "str", "required": False},
            "depth": {"type": "float", "unit": "mm", "required": False},
            "end_cond": {"type": "str", "required": False, "default": "swEndCondThroughAll"},
            "flip_side": {"type": "bool", "required": False, "default": True},
            "reverse_dir": {"type": "bool", "required": False, "default": False},
            "both_directions": {"type": "bool", "required": False, "default": False},
            "name": {"type": "str", "required": False}},
    requires=("sketch_closed", "has_feature"), verified=True)
def op_extrude_cut(sw, step, ctx):
    return _extrude(sw, step, ctx, cut=True)


@op("revolve_boss", "旋转凸台", "feature",
    "把草图绕一条中心线旋转成实体。**轴 = 草图里的中心线**（用 centerline 画），"
    "或者另选一条基准轴/边。轴必须穿过轮廓所在平面。\n"
    "阶梯轴、法兰轮毂、垫圈都这么画：先画半剖面（含中心线），再旋转 360°。",
    api=["IModelDoc2.SelectByID2", "IFeatureManager.FeatureRevolve2"],
    params={"profile": {"type": "str", "required": False, "desc": "草图名"},
            "axis": {"type": "str", "required": False,
                     "desc": "轴名（基准轴/中心线）。不给则用草图里的中心线"},
            "angle": {"type": "float", "unit": "deg", "required": False, "default": 360},
            "reverse_dir": {"type": "bool", "required": False, "default": False},
            "name": {"type": "str", "required": False}},
    # ★ `effect=("has_feature",)`：旋转凸台**就是**产生实体特征的那一步（同 extrude_boss）。
    #   早先漏了这一条，于是「回转出环 → 选端面 → 草图 → 圆周阵列孔 → 拉伸切除」
    #   这条链路在**静态校验**就被拦下（extrude_cut 的 requires 含 has_feature，
    #   而状态机不知道前面那步回转已经建出实体了）。段差在于 extrude_boss 声明了、
    #   revolve_boss 没声明 —— 只有同时用到"回转 + 切除"的件才会暴露。
    requires=("sketch_closed",), effect=("has_feature",), verified=True)
def op_revolve_boss(sw, step, ctx):
    # 同 _extrude：不给 profile 就用刚刚画的那张草图
    prof = _p(step, ctx, "profile") or ctx.get("last_sketch")
    sw.clear_selection()
    if prof and not _select_feature(sw, prof, 0):
        raise SwError(f"选不中旋转用的草图 '{prof}'")
    axis = _p(step, ctx, "axis")
    if axis:
        if not sw.ext.SelectByID2(axis, "AXIS", 0, 0, 0, True, 1, None, 0):
            sw.ext.SelectByID2(axis, "EXTSKETCHSEG", 0, 0, 0, True, 1, None, 0)
    # ★ IFeatureManager.FeatureRevolve2 是 20 参，顺序是
    #   SingleDir, IsSolid, IsThin, IsCut, ReverseDir, BothDirectionUpToSameEntity,
    #   Dir1Type, Dir2Type, Dir1Angle, Dir2Angle, OffsetReverse1, OffsetReverse2,
    #   OffsetDistance1, OffsetDistance2, ThinType, ThinThickness1, ThinThickness2,
    #   Merge, UseFeatScope, UseAutoSelect
    #   早期版本漏了 IsThin / IsCut 两个，后面全部错位，报「非选择性的参数」。
    #   查法：python scripts/sw_memberkind.py --sig FeatureRevolve2
    f = _fm(sw).FeatureRevolve2(
        True,                                      # SingleDir（单向旋转）
        True,                                      # IsSolid（要实体，不是曲面）
        False,                                     # IsThin
        False,                                     # IsCut
        bool(_p(step, ctx, "reverse_dir", False)), # ReverseDir
        False,                                     # BothDirectionUpToSameEntity
        sw.C("swEndCondBlind"), 0,                 # Dir1Type 用角度, Dir2Type=0
        deg(float(_p(step, ctx, "angle", 360) or 360)), 0.0,   # Dir1Angle, Dir2Angle
        False, False, 0.0, 0.0,                    # OffsetReverse1,2 / OffsetDistance1,2
        0, 0.0, 0.0,                               # ThinType / 薄壁厚度（非薄壁）
        True, False, True)                         # Merge, UseFeatScope, UseAutoSelect
    sw.clear_selection()
    if f is None:
        raise SwError("旋转失败（草图必须有中心线/轴，且轮廓不能穿过轴）")
    name = _p(step, ctx, "name")
    if name:
        with contextlib.suppress(Exception):
            f.Name = name
        ctx.setdefault("feature_names", []).append(name)
    return {"feature": name or f.Name}


@op("fillet", "圆角", "feature",
    "对选中的边倒圆角。\n"
    "★★ **边要先选出来**，两种给法（2026-09-25 M2 起支持坐标，不再依赖手动选择）：\n"
    "    `edges: \"all\"`                    全部边（小零件「一键全身倒圆」）\n"
    "    `edges: [[x,y,z], …]`              给**边中点**的全局坐标（mm），挑最近的一条\n"
    "    `edges: [{\"point\":[…],\"tol\":3}]`  同上，收窄匹配半径\n"
    "  坐标是「边的中点」，不是端点、也不是边上随便一点 —— 中点按两端顶点算，\n"
    "  闭环边（圆柱口）取曲线参数中点。\n"
    "★ 容差是**特意**的：最近那条边离目标点超容差就判「没匹配上」并报错，\n"
    "  不勉强选一条 —— 勉强选中的结果是**不报错但倒错地方**。\n"
    "★ 半径别超过相邻壁厚的一半，否则报「无法生成圆角」（这条会抛错，不会静默）。\n"
    "★ **凹角圆角是加料的**（腔内角、叉口底角的圆角填材料，体积变大）——\n"
    "  这类边给 `material: \"add\"`，否则会被「圆角一定去料」的守门错杀。\n"
    "★★★ **选边失败的第三种原因：切除顺序**（2026-10-04 真机，坑表 §109⑥）——\n"
    "  要吃边的圆被**先做的切除**打断成多段弧时（如 Ø34 孔口圆被先切的键槽切成两段），\n"
    "  闭环边的「中点」退化成「两端点平均」⇒ 两段弧中点**完全相同**（都是 (0,16.2481,40)）\n"
    "  ⇒ 坐标选边必然命中错的弧、反复报「圆角重建失败」——**半径怎么调都没用**。\n"
    "  **解法**：把打断它的切除**移到圆角之后**\n"
    "  （原则：先建「要靠它取边」的特征，再切「会打断这条边」的槽/孔）。\n"
    "  **验证手法**：造一个「去掉打断特征」的对照模型，在上面单独测这条圆角；\n"
    "  对照成功 + 正式版失败 ⇒ 坐实是顺序问题，**别再改几何/半径**。",
    api=["IFeatureManager.FeatureFillet3", "IEntity.Select4"],
    params={"r": {"type": "float", "unit": "mm"},
            "edges": {"type": "list", "required": False,
                      "desc": '要倒圆的边："all" 或 [[x,y,z],…]（边中点，mm）'},
            "tol": {"type": "float", "unit": "mm", "required": False, "default": 8,
                    "desc": "坐标匹配容差（mm）"},
            "propagate": {"type": "bool", "required": False, "default": True,
                          "desc": "是否沿相切边延伸"},
            "material": {"type": "str", "required": False, "default": "remove",
                         "desc": "remove=外棱去料(默认,体积必须变小)/"
                                 "add=凹角加料(腔内圆角,体积必须变大)/"
                                 "any=只要求体积有变化"},
            "name": {"type": "str", "required": False}},
    requires=("has_feature",), verified=True)
def op_fillet(sw, step, ctx):
    notes: list[str] = []
    C = sw.C
    opts = int(C.get("swFeatureFilletUniformRadius", 2))
    if _p(step, ctx, "propagate", True):
        opts |= int(C.get("swFeatureFilletPropagate", 1))
    r_m = mm(float(_p(step, ctx, "r")))
    ftyp = C.get("swFeatureFilletType_Simple", 0)
    ovf = C.get("swFilletOverFlowType_Default", 0)
    v0 = _volume(sw)
    # ★★★ **建特征调用要重试**（2026-10-12，坑表 §146）：
    #   `IFeatureManager.FeatureFillet` 存在**间歇性静默失败** —— 选中集数好端端 = 4、
    #   调用返回 None、不建特征、体积一字不变、不报错。同一会话里连试可从"全灭"翻成"8/8"
    #   （真机实测），与本文件 `_pick`（SelectByID2）/ `AddComponent5` 属同一类 COM 瞬时失败。
    #   ⇒ 判据仍落**特征树 diff**（不信返回值）：没新增特征就**重选、重调**，最多 COM_RETRY 次；
    #     重试次数如实写进结果 `attempts`，不掩盖"第一次其实没成"。
    # ★ 真机前的签名核对（2026-09-25，M2）：类型库里 FeatureFillet3 是 **14 参**，
    #   原实现只喂了 12 个 ⇒ 参数错位。改用最短、语义最清楚的
    #   `IFeatureManager.FeatureFillet`（7 参：Options/R1/Ftyp/OverflowType/…）。
    f, err, attempts = None, None, 0
    sel_ok = False
    for attempts in range(1, COM_RETRY + 1):
        if _select_by_spec(sw, step, ctx, "edges", notes, kind="edge"):
            sel_ok = True
        else:
            if attempts < COM_RETRY:
                time.sleep(COM_RETRY_S)
                continue
            break
        tree0 = {n for _t, n in sw.feature_names()}
        _res, err = _call(member, _fm(sw), "FeatureFillet",
                          opts, r_m, ftyp, ovf, None, None, None)
        sw.clear_selection()
        f = _new_feature(sw, tree0)
        if f is not None:
            break
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)
    if f is None:
        if not sel_ok:
            raise SwError("圆角前必须先选中边（给 edges 参数，或先用 select_face 手动选）")
        raise SwError("圆角没生成特征（半径超过相邻壁厚？或选中的边不能倒圆）"
                      + (f"（COM 报错：{err}）" if err else "")
                      + f"（已重试 {attempts} 次）")
    if isinstance(f, tuple):
        raise SwError(f"圆角重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    name = _p(step, ctx, "name")
    if name:
        with contextlib.suppress(Exception):
            f.Name = name
    v1 = _volume(sw)
    # ★ `_volume()` 给的是 **m³**，阈值要按 m³ 想：去料 < 1 mm³ 才算「没去料」。
    #   写成 `v1 > v0 - 1e-6` 是按 mm³ 的直觉写的，但 1e-6 m³ = **1000 mm³** ——
    #   会把一个正常的 360 mm³ 倒角判成「没去料」，反过来白报错。
    # ★ material 开关（2026-10-03 半联轴器真机）：**凹角圆角是加料的**——
    #   叉口左端面与叉口顶面的内角 R8 fillet 体积 +631 mm³，是正确的几何，
    #   旧断言「圆角一定去料」把它错杀了。§103：先分清夹具错还是判据错。
    mat = str(_p(step, ctx, "material", "remove") or "remove").lower()
    dv = (v0 - v1) * 1e9 if (v0 is not None and v1 is not None) else None
    if dv is not None:
        if mat == "remove" and dv < 1.0:
            raise SwError(f"圆角特征建出来了但**体积没减少**（{v0 * 1e9:.2f} → {v1 * 1e9:.2f} mm³）—— "
                          "半径没生效，或倒在了不该倒的边（外棱圆角一定去料）。"
                          " 若这是腔内凹角圆角（加料），给 material=\"add\"。")
        if mat == "add" and dv > -1.0:
            raise SwError(f"凹角圆角**体积没增加**（{v0 * 1e9:.2f} → {v1 * 1e9:.2f} mm³）—— "
                          "半径没生效，或选中的边不是凹角（凹角圆角填材料，体积必须变大）。")
        if mat == "any" and abs(dv) < 1.0:
            raise SwError(f"圆角特征建出来了但**体积没变化**（{v0 * 1e9:.2f} → {v1 * 1e9:.2f} mm³）—— "
                          "半径没生效，或圆角倒在了别处。")
    return {"feature": name or _fname(f), "volume_before": v0, "volume_after": v1,
            "attempts": attempts, "notes": notes or None}


@op("chamfer", "倒角", "feature",
    "对选中的边倒角（等距型，默认 45°）。\n"
    "选边规则与 `fillet` **完全一致**（`edges: \"all\"` 或 `[[x,y,z],…]` 边中点坐标），\n"
    "实现共用 `_select_by_spec`，所以两个 op 的选择行为不会漂。\n"
    "★ 倒角同样是**去料**：体积必须变小，否则只算「多了个特征名」，不算成功。",
    api=["IFeatureManager.InsertFeatureChamfer", "IEntity.Select4"],
    params={"dist": {"type": "float", "unit": "mm", "desc": "等距倒角的距离"},
            "edges": {"type": "list", "required": False,
                      "desc": '要倒角的边："all" 或 [[x,y,z],…]（边中点，mm）'},
            "tol": {"type": "float", "unit": "mm", "required": False, "default": 8},
            "angle": {"type": "float", "unit": "deg", "required": False, "default": 45},
            "name": {"type": "str", "required": False}},
    requires=("has_feature",), verified=True)
def op_chamfer(sw, step, ctx):
    notes: list[str] = []
    d = mm(float(_p(step, ctx, "dist")))
    ang = float(_p(step, ctx, "angle", 45) or 45)
    C = sw.C
    # ★ 真机签名核对（2026-09-25，M2）：`IFeatureManager.InsertFeatureChamfer` 是 **8 参**，
    #   顺序 (Options, ChamferType, Width, Angle, OtherDist, VCD1, VCD2, VCD3)。
    #   ★★ 距离填**哪个槽由 ChamferType 决定**，而且**只有 1 号真的去料**。
    #   6 个变体在真机上一个个试过、每次都量体积（见 references/30-pitfalls.md §46）：
    #        AngleDistance(1) + Width=d + Angle=θ       → ΔV = −360.0 mm³   ✅ 唯一成功
    #        EqualDistance(16)：d 填 Width 或 OtherDist → ΔV = 0，静默只加特征名 ❌
    #        AngleDistance(1) + OtherDist=d            → 特征都不生成      ❌
    #   官方参数说明写着 "OtherDist 给 EqualDistance 用"，**SW2024 实测不成立**。
    #   45° 的等距倒角本来就等于角度-距离型 → 统一走 1 号，别再用 16。
    ctype = int(C.get("swChamferAngleDistance", 1))
    cham_args = (ctype, d, deg(ang), 0.0, 0.0, 0.0, 0.0)
    opts = int(C.get("swFeatureChamferTangentPropagation", 4))
    v0 = _volume(sw)
    # ★★ 建特征调用重试（2026-10-12，坑表 §146）：与 `fillet` 同款间歇性静默失败 ——
    #   选中集非空、`InsertFeatureChamfer` 却不建特征也不报错。判据落特征树 diff，重选重调。
    f, err, attempts, sel_ok = None, None, 0, False
    for attempts in range(1, COM_RETRY + 1):
        if _select_by_spec(sw, step, ctx, "edges", notes, kind="edge"):
            sel_ok = True
        else:
            if attempts < COM_RETRY:
                time.sleep(COM_RETRY_S)
                continue
            break
        tree0 = {n for _t, n in sw.feature_names()}
        _res, err = _call(member, _fm(sw), "InsertFeatureChamfer", opts, *cham_args)
        sw.clear_selection()
        f = _new_feature(sw, tree0)
        if f is not None:
            break
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)
    if f is None:
        if not sel_ok:
            raise SwError("倒角前必须先选中边（给 edges 参数）")
        raise SwError("倒角没生成特征" + (f"（COM 报错：{err}）" if err else "")
                      + f"（已重试 {attempts} 次）")
    if isinstance(f, tuple):
        raise SwError(f"倒角重建失败 {f[1]}({FEATURE_ERROR_NAMES.get(f[1], '未知')})")
    name = _p(step, ctx, "name")
    if name:
        with contextlib.suppress(Exception):
            f.Name = name
    v1 = _volume(sw)
    # ★ `_volume()` 给的是 **m³**，阈值要按 m³ 想：去料 < 1 mm³ 才算「没去料」。
    #   写成 `v1 > v0 - 1e-6` 是按 mm³ 的直觉写的，但 1e-6 m³ = **1000 mm³** ——
    #   会把一个正常的 360 mm³ 倒角判成「没去料」，反过来白报错。
    if v0 is not None and v1 is not None and (v0 - v1) * 1e9 < 1.0:
        raise SwError(
            f"倒角特征建出来了但**体积没减少**（{v0 * 1e9:.2f} → {v1 * 1e9:.2f} mm³）—— 距离参数没生效。\n"
            "必须用 swChamferAngleDistance(1) + Width=距离 + Angle=角度；"
            "swChamferEqualDistance(16) 实测**只加特征名、不去料**（2026-09-25 六变体实测）。")
    return {"feature": name or _fname(f), "volume_before": v0, "volume_after": v1,
            "attempts": attempts, "notes": notes or None}


@op("shell", "抽壳", "feature",
    "给实体抽壳（箱体类零件的主力）。\n"
    "★★ `remove` = 要**移除**的面，两种给法：\n"
    "    `remove: [\"+Z\"]`       方向串 → 法向匹配里**面积最大**的那个面\n"
    "                            （箱体顶面就是 '+Z'；-Z 底面、+X/-X 侧面同理）\n"
    "    `remove: [[x,y,z], …]`  面上的全局坐标（mm）→ 射线拾取，失败退到包围盒中心最近邻\n"
    "    `remove: []`            不移除任何面 = **中空封壳**（做浮球/油箱）；合法，别当漏参数\n"
    "★ 抽壳后**体积必然大减**（内腔被掏空）；如果体积没变，说明只是多了个特征名。\n"
    "★ 壁厚不能超过零件最薄方向的一半，否则报错。",
    api=["IFeatureManager.InsertFeatureShell", "IEntity.Select4"],
    params={"thickness": {"type": "float", "unit": "mm"},
            "remove": {"type": "list", "required": False, "default": [],
                       "desc": '要移除的面：["+Z"] 或 [[x,y,z],…]；空列表=中空封壳'},
            "tol": {"type": "float", "unit": "mm", "required": False, "default": 8},
            "outward": {"type": "bool", "required": False, "default": False}},
    requires=("has_feature",), verified=True)
def op_shell(sw, step, ctx):
    notes: list[str] = []
    spec = _p(step, ctx, "remove") or []
    if spec:
        sw.clear_selection()
        hit, miss = _select_faces(sw, spec, float(_p(step, ctx, "tol", 8) or 8))
        if miss:
            notes.append(f"没匹配上要移除的面：{miss}")
        if not hit:
            raise SwError(
                "一个面都没选中。\n"
                "  排查：① 方向串只能是 '+X/-X/+Y/-Y/+Z/-Z'（大写、带符号）\n"
                "        ② 给坐标的话必须是**面上**的全局坐标（mm）\n"
                "        ③ 想「不移除面」请显式给 `remove: []`")
    v0 = _volume(sw)
    tree0 = {n for _t, n in sw.feature_names()}
    # ★ 真机前的签名核对（2026-09-25，M2）：`IFeatureManager.InsertFeatureShell`
    #   **在类型库里根本不存在**（原实现调的就是它 ⇒ 必然 AttributeError）。
    #   真身是 `IModelDoc2.InsertFeatureShell(Thickness, Outward)`，而且**返回 void**
    #   —— 所以只能靠特征树 diff 判成败，绝不能看返回值。
    _res, err = _call(member, sw.model, "InsertFeatureShell",
                      mm(float(_p(step, ctx, "thickness"))),
                      bool(_p(step, ctx, "outward", False)))
    sw.clear_selection()
    f = _new_feature(sw, tree0)
    if f is None:
        raise SwError("抽壳没生成特征（壁厚超过最薄方向的一半？）"
                      + (f"（COM 报错：{err}）" if err else ""))
    if isinstance(f, tuple):
        raise SwError(f"抽壳重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    v1 = _volume(sw)
    if v0 is not None and v1 is not None and v1 >= v0:
        notes.append("特征成了但**体积没减少** —— 抽壳必然掏空内腔，体积不变说明没抽上")
    return {"feature": _fname(f), "volume_before": v0, "volume_after": v1,
            "removed": spec, "notes": notes or None}


# ============================================================ 曲线骨架（S1）
#
# ★ 为什么单开一节：CRH380CL 车头是**曲面放样**。放样要求所有截面
#   「图元数一致、起点对齐」—— 手工画 N 条截面必然**扭**，而且**不报错**
#   （`StartMatchingType` 也救不回来）。参数化生成 = 先有『点列』：
#   同一个参数方程算出每站的 N 个点、起点同侧 ⇒ 放样不扭（见 80-crh380cl §4.2）。
#
#   参考：`references/80-crh380cl-project.md` S1 阶段。
#
# ★★★ 三条铁律（2026-10-09 抽自**官方帮助 + tlb**，别再踩）：
#   ① 点坐标单位 = **米**。官方 VBA 范例里 12.7mm 写作 `0.0127`
#      —— 与全项目「一切 mm」口径相反；本 op 内部乘 `mm()`，调用方一律写 mm。
#   ② `InsertCurveFileBegin` 必须在**所有** `InsertCurveFilePoint` 之前，
#      `InsertCurveFileEnd` 必须**包住**所有点 —— 官方 Remarks 明文。
#   ③ 判据落在**读回的点坐标**（`IFreePointCurveFeatureData`），
#      **不是** `InsertCurveFileEnd()` 的返回值（铁律①：返回值会说谎）。
#
# 签名（2026-10-09 `sw_apisig.py --tlb` 实读）：
#     IModelDoc2.InsertCurveFileBegin()                       -> void
#     IModelDoc2.InsertCurveFilePoint(X, Y, Z: Double)        -> Boolean
#     IModelDoc2.InsertCurveFileEnd()                         -> Boolean
#     IFreePointCurveFeatureData.GetPointCount / PointArray / SavePointsToFile
#     ISketchManager.Insert3DSketch(UpdateEditRebuild: Boolean) -> void  （开关式）


def _curve_data(f):
    """把 `IFeature` 转成 `IFreePointCurveFeatureData`（读回曲线的点）。

    ★ 拿不到就回 None —— **不要**吞成"点数为 0"（那会把失败伪装成成功）。
    """
    with contextlib.suppress(Exception):
        fd = member(typed(f, "IFeature"), "GetDefinition")
        if fd is None:
            return None
        return typed(fd, "IFreePointCurveFeatureData")
    return None


def _normalize_point_array(raw, n):
    """把 `IFreePointCurveFeatureData.PointArray`（Variant）归一化成
    `[[x,y,z], ...]`（**米**）。归一不了回 None。

    ★ 为什么不直接 `list(raw)` 就完事：tlb 里 `PointArray` 是 **Variant**
      （`IGetPointArray` 才是 `Double` 数组）。真机可能回**平铺的 3n 个 double**、
      也可能回 **n 个三元组** —— 两种都归一化，归一不了就如实报错，
      免得把"读法不对"误判成"曲线点不对"。
    """
    if raw is None:
        return None
    try:
        seq = list(raw)
    except Exception:                                         # noqa: BLE001
        return None
    if not seq:
        return None
    # 形态 A：n 个三元组（元素本身是序列）
    if all(hasattr(x, "__len__") and not isinstance(x, str) for x in seq):
        out = []
        for tup in seq:
            v = list(tup)
            if len(v) < 3:
                return None
            out.append([float(v[0]), float(v[1]), float(v[2])])
        return out
    # 形态 B：平铺的 3n 个 double
    #   ★★★ 真机实测（2026-10-09）：`GetPointCount()` 回的是**坐标个数 = 3 × 点数**
    #     —— 13 个点回 **39**、3 个点回 **9**。所以 n 与"点数"差 3 倍，
    #     交叉校验必须按这一条算，否则会把"读法不对"误判成"曲线点不对"（首轮就踩了）。
    if len(seq) % 3 == 0:
        out = [[float(seq[i]), float(seq[i + 1]), float(seq[i + 2])]
               for i in range(0, len(seq), 3)]
        if n is None or int(n) in (len(out), 3 * len(out)):
            return out
    return None


def _curve_points_mm(f):
    """读回一条「通过 XYZ 点的曲线」的点列（**mm**）。返回 `(points|None, err|None)`。

    ★ 真机实测（2026-10-09）：这类特征 `GetTypeName2()` = **`CurveInFile`**；
      `GetPointCount()` = **坐标个数 = 3 × 点数**（别当成点数）；
      `PointArray` = **平铺 tuple**，值以**米**为单位。
    """
    fd = _curve_data(f)
    if fd is None:
        return None, "拿不到 IFreePointCurveFeatureData（这条特征不是『通过 XYZ 点的曲线』？）"
    n = None
    with contextlib.suppress(Exception):
        n = int(member(fd, "GetPointCount"))
    raw = None
    with contextlib.suppress(Exception):
        raw = member(fd, "PointArray")
    pts = _normalize_point_array(raw, n)
    if pts is None:
        return None, (f"点数组无法归一化（count={n}，raw 类型 {type(raw).__name__}）"
                      "—— 读法不对，别当成『曲线点不对』")
    return [[x * 1000.0, y * 1000.0, z * 1000.0] for x, y, z in pts], None


@op("curve_xyz", "通过 XYZ 点的曲线", "feature",
    "把一串三维点连成一条**参考曲线**（`REFERENCECURVES`）—— 参数化放样/扫掠的骨架。\n"
    "★ 走 `IModelDoc2.InsertCurveFileBegin` → `InsertCurveFilePoint(X,Y,Z)` × N →\n"
    "  `InsertCurveFileEnd`（签名 2026-10-09 抽自 tlb；点单位是**米**，本 op 内部乘 mm()）。\n"
    "★★★ **判据落在读回的点坐标**（`IFreePointCurveFeatureData.PointArray`），\n"
    "  **不落** `InsertCurveFileEnd()` 的返回值 —— 它回 Boolean 但不保证几何对（铁律①）。\n"
    "  ⇒ 返回值带 `points_readback_mm` 与 `max_dev_mm`（读回点与输入点的最大偏差）。\n"
    "★ 官方硬约束：`Begin` 必须在**所有** `Point` 之前、`End` 必须**包住**所有点。\n"
    "★ 点正好 2 个会退化成**线段**；点重合/过近可能整条失败（本 op 提前拦重合点）。\n"
    "★ 这是**放样截面族**的基础：同一套参数方程算出每站的 N 个点、起点同侧 ⇒ 放样不扭。",
    api=["IModelDoc2.InsertCurveFileBegin", "IModelDoc2.InsertCurveFilePoint",
         "IModelDoc2.InsertCurveFileEnd",
         "IFeature.GetDefinition", "IFreePointCurveFeatureData.PointArray"],
    params={"points": {"type": "list",
                       "desc": "[[x,y,z], ...] mm，至少 2 个（放样截面建议 ≥ 8 个）"},
            "closed": {"type": "bool", "required": False, "default": False,
                       "desc": "把首点再补一次做成闭合曲线"},
            "name": {"type": "str", "required": False},
            "tol_mm": {"type": "float", "unit": "mm", "required": False, "default": 0.01,
                       "desc": "读回点与输入点的容差；超出即报错"}},
    requires=("doc_open",), effect=("has_curve",), verified=True)
def op_curve_xyz(sw, step, ctx):
    pts = _p(step, ctx, "points")
    if not isinstance(pts, (list, tuple)) or len(pts) < 2:
        raise SwError(f"`points` 至少要 2 个点（实得 {0 if not pts else len(pts)} 个）—— "
                      "一条曲线过不了 2 个点。")
    disp = [[float(p[0]), float(p[1]), (float(p[2]) if len(p) > 2 else 0.0)] for p in pts]
    if _p(step, ctx, "closed", False):
        disp = disp + [list(disp[0])]
    for i in range(1, len(disp)):
        if all(abs(disp[i][k] - disp[i - 1][k]) < 1e-9 for k in range(3)):
            raise SwError(f"第 {i} 个点与上一个点**完全重合**（{disp[i]}）—— 曲线会建不出来")
    tol = float(_p(step, ctx, "tol_mm", 0.01) or 0.01)
    tree0 = {n for _t, n in sw.feature_names()}
    f, err, attempts, end_ret = None, None, 0, None
    for attempts in range(1, COM_RETRY + 1):
        _b, e1 = _call(member, sw.model, "InsertCurveFileBegin")
        for d in disp:
            _call(member, sw.model, "InsertCurveFilePoint", mm(d[0]), mm(d[1]), mm(d[2]))
        end_ret, e2 = _call(member, sw.model, "InsertCurveFileEnd")
        err = e1 or e2
        f = _new_feature(sw, tree0)
        if f is not None:
            break
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)
    if isinstance(f, tuple):
        raise SwError(f"曲线特征重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    if f is None:
        raise SwError(
            "曲线没生成（特征树没涨）。\n"
            "  ○ 官方硬约束：`InsertCurveFileBegin` 必须在**所有** `InsertCurveFilePoint` 之前、\n"
            "    `InsertCurveFileEnd` 必须**包住**所有点。\n"
            "  ○ 是不是有**重合点**？点太近/重复会让整条曲线失败。\n"
            "  ○ 当前文档是**零件**吗？（参考曲线只能建在零件里）"
            + (f"\n  ○ COM 报错：{err}" if err else "")
            + f"\n  ★ 已重试 {attempts} 次（COM 瞬时失败在本机是已知现象）")
    ftype = ""
    with contextlib.suppress(Exception):
        ftype = str(member(typed(f, "IFeature"), "GetTypeName2") or "")
    name = _finish(sw, f, step, ctx, {})["feature"]
    ctx["last_curve"] = name
    out = {"feature": name, "feature_type": ftype, "n_points_in": len(disp),
           "attempts": attempts,
           "end_return": end_ret if isinstance(end_ret, bool) else None}
    pts_back, rerr = _curve_points_mm(f)
    if pts_back is None:
        out["readback_error"] = rerr
        out["warn"] = "读不回曲线点 —— 特征建出来了，但**几何没验**（不算通过）"
        return out
    back_mm = [[round(p[0], 6), round(p[1], 6), round(p[2], 6)] for p in pts_back]
    out["n_points_readback"] = len(back_mm)
    out["points_readback_mm"] = back_mm
    if len(back_mm) != len(disp):
        raise SwError(f"读回点数 {len(back_mm)} ≠ 输入点数 {len(disp)} —— "
                      f"有 `InsertCurveFilePoint` 被静默吞掉（返回值不可信，铁律①）")
    dev = max(math.dist(disp[i], back_mm[i]) for i in range(len(disp)))
    out["max_dev_mm"] = round(dev, 6)
    if dev > tol:
        raise SwError(
            f"读回点与输入点偏差 {dev:.4f}mm > 容差 {tol}mm —— 曲线不是过这些点的。\n"
            f"  常见原因：单位错（点应是**米**，本 op 内部已乘 mm()）、点顺序错。")
    out["notes"] = [f"读回 {len(back_mm)} 点，最大偏差 {out['max_dev_mm']}mm（容差 {tol}）"]
    return out


@op("check_curve", "校验曲线点", "verify",
    "读回一条「通过 XYZ 点的曲线」的**点数与关键坐标**（mm），可与期望比对。\n"
    "★ 曲面判据看面积；**曲线判据看点** —— 端点、逐点、点数、是否闭合。\n"
    "★ 一个期望都不给 = **只测不判**（标定用）：回带实测的点列 `points_mm`。\n"
    "★ `IFreePointCurveFeatureData.GetPointCount` + `PointArray`（tlb 确认）。\n"
    "★ 逐点/端点比对是**绝对 mm 容差**（`tol_mm`），与 check_bbox 的 `tol_mm` 同口径。",
    api=["IFeature.GetDefinition", "IFreePointCurveFeatureData.GetPointCount",
         "IFreePointCurveFeatureData.PointArray"],
    params={"feature": {"type": "str", "required": False,
                        "desc": "曲线特征名；不给用上一步的曲线（last_curve）"},
            "expect_points": {"type": "int", "required": False, "desc": "期望点数"},
            "expect_start": {"type": "list", "required": False, "desc": "[x,y,z] mm"},
            "expect_end": {"type": "list", "required": False, "desc": "[x,y,z] mm"},
            "expect_closed": {"type": "bool", "required": False, "desc": "是否期望首尾重合"},
            "expect_points_list": {"type": "list", "required": False,
                                   "desc": "逐点比对 [[x,y,z], ...] mm"},
            "tol_mm": {"type": "float", "unit": "mm", "required": False, "default": 0.01}},
    requires=("doc_open",), verified=True)
def op_check_curve(sw, step, ctx):
    nm = _p(step, ctx, "feature") or ctx.get("last_curve")
    if not nm:
        raise SwError("没给 `feature`，也没有上一步的曲线（`last_curve`）")
    feat = None
    with contextlib.suppress(Exception):
        feat = sw.model.FeatureByName(str(nm))
    if feat is None:
        raise SwError(f"找不到特征 '{nm}'（曲线名对不对？用 `feature_names()` 列一下）")
    pts, rerr = _curve_points_mm(feat)
    if pts is None:
        raise SwError(f"读不到曲线 '{nm}' 的点：{rerr}")
    mm_pts = [[round(p[0], 6), round(p[1], 6), round(p[2], 6)] for p in pts]
    tol = float(_p(step, ctx, "tol_mm", 0.01) or 0.01)
    out = {"feature": str(nm), "n_points": len(mm_pts),
           "start_mm": mm_pts[0], "end_mm": mm_pts[-1], "points_mm": mm_pts,
           "expect_ok": None}
    exp_n = _p(step, ctx, "expect_points")
    exp_s = _p(step, ctx, "expect_start")
    exp_e = _p(step, ctx, "expect_end")
    exp_c = _p(step, ctx, "expect_closed")
    epl = _p(step, ctx, "expect_points_list")
    if all(v is None for v in (exp_n, exp_s, exp_e, exp_c, epl)):
        out["note"] = "没给任何期望值 —— 只回带实测点列，没有判定"
        return out
    if exp_n is not None and len(mm_pts) != int(exp_n):
        raise SwError(f"曲线点数不符：实际 {len(mm_pts)}，期望 {exp_n}")
    for key, idx, label, exp in (("expect_start", 0, "起点", exp_s),
                                 ("expect_end", -1, "终点", exp_e)):
        if exp is None:
            continue
        e = [float(exp[0]), float(exp[1]), (float(exp[2]) if len(exp) > 2 else 0.0)]
        d = math.dist(e, mm_pts[idx])
        if d > tol:
            raise SwError(f"曲线{label}坐标超差：实际 {mm_pts[idx]}，"
                          f"期望 {e}，差 {d:.4f}mm > {tol}mm")
    if exp_c is not None:
        d = math.dist(mm_pts[0], mm_pts[-1])
        is_closed = d <= tol
        if bool(exp_c) != is_closed:
            raise SwError(f"闭合性不符：首尾距离 {d:.4f}mm ⇒ "
                          f"{'闭合' if is_closed else '开口'}，"
                          f"期望 {'闭合' if exp_c else '开口'}")
    if epl is not None:
        if len(epl) != len(mm_pts):
            raise SwError(f"逐点比对的点数不符：给 {len(epl)}，实际 {len(mm_pts)}")
        dev = max(
            math.dist([float(e[0]), float(e[1]), (float(e[2]) if len(e) > 2 else 0.0)],
                      mm_pts[i]) for i, e in enumerate(epl))
        out["max_dev_mm"] = round(dev, 6)
        if dev > tol:
            raise SwError(f"逐点最大偏差 {dev:.4f}mm > 容差 {tol}mm")
    out["expect_ok"] = True
    return out


def _active_sketch_name(sw):
    """当前活动草图的**名字**（读回）。没在编辑草图时回 None。

    ★★★ 真机实测（2026-10-09）：**必须用裸 `member(obj, "Name")`，不能套 `typed()`**。
      `GetActiveSketch2()` 回的对象是一条 `ISketch` 的 IDispatch（不是 IFeature）；
      对它 `typed(obj, "IFeature")` 之后取 `Name`，pywin32 会按**错误的 DISPID**
      解析，拿到 **16 个浮点的变换矩阵**（= `ISketch.ModelToSketchTransform`），
      于是"草图名"变成一个矩阵字符串 —— 后续按这个名字查特征必然找不到。
      同一条坑：`typed(obj,"IFeature").GetTypeName2()` 会直接抛「非选择性的参数」，
      而 `typed(obj,"ISketch").GetType()` 正常（回 9）。
    """
    f = None
    with contextlib.suppress(Exception):
        f = member(sw.model, "GetActiveSketch2")
    if f is None:
        return None
    with contextlib.suppress(Exception):
        return str(member(f, "Name"))          # ★ 裸取，别 typed()
    return None


def _existing_3d_sketches(sw) -> list:
    """文档里已有的 **3D 草图**名。

    ★★ 真机实测（2026-10-09）：3D 草图的特征类型名是 **`3DProfileFeature`**
      （2D 草图才叫 `ProfileFeature`）—— 用 `ProfileFeature` 去筛**永远筛不到 3D 草图**。
      这里两条判据并用（类型名 **或** `ISketch.Is3D`），任一条成立即算。
    """
    out = []
    for t, nm in sw.feature_names():
        if t == "3DProfileFeature":
            out.append(nm)
            continue
        if t != "ProfileFeature":
            continue
        sk = _isketch_of(sw, nm)
        if sk is None:
            continue
        is3d = False
        with contextlib.suppress(Exception):
            is3d = bool(member(sk, "Is3D"))
        if is3d:
            out.append(nm)
    return out


@op("sketch3d_begin", "开始 3D 草图", "sketch",
    "进入 **3D 草图**（不依赖基准面，图元直接在空间里画）—— 空间引导线/样条用它。\n"
    "★ 走 `ISketchManager.Insert3DSketch(UpdateEditRebuild)`（tlb 确认 1 参，**开关式**：\n"
    "  调一次进入、再调一次退出 —— 官方原文：『Inserts a new 3D sketch **or closes the\n"
    "  active sketch**』）。退出用 `sketch3d_end`。\n"
    "★★★ **不要依赖它去编辑已有的 3D 草图**（2026-10-09 真机确诊）：\n"
    "  官方契约**只承诺**『新建一张 3D 草图』或『关闭活动草图』，**没有**承诺\n"
    "  `打开已有草图去编辑`；而**同一台机器、同一份代码**实测到过两种相反行为 ——\n"
    "  一次**打开了已有草图**、另一次**新建了第二张**（`3D草图2`）。\n"
    "  ⇒ 本 op 的做法是**按读回判定，绝不预测**：调用后读回真正打开的是哪一张，\n"
    "     `mode='new'` 时若发现进的是**已有**草图 ⇒ **直接报错**（防止两批引导线\n"
    "     悄悄并进同一张草图、而且不报任何错）；`mode='edit'` 时若发现**新建**了 ⇒ 也报错。\n"
    "  ★ 建模口径：**所有 3D 引导实体放进同一张 3D 草图**（一张能装多条）；\n"
    "    要几张互不相干的曲线，优先用 `curve_xyz`（它根本不需要草图）。\n"
    "★ 3D 草图里 `line`/`spline`/`point` 的 **z 才有效**（2D 草图里 z 被忽略）。\n"
    "★ 草图名靠 `GetActiveSketch2()` **裸取 Name**（套了 `typed(...,'IFeature')` 会\n"
    "  拿到 16 个浮点的变换矩阵 —— 真机实测的坑，见 `_active_sketch_name`）。",
    api=["ISketchManager.Insert3DSketch", "IModelDoc2.GetActiveSketch2"],
    params={"mode": {"type": "str", "required": False, "default": "new",
                     "desc": "new=必须**新建**（读回发现进了已有草图就报错）/ "
                             "edit=必须进**已有**草图（不可靠，仅供明确要接着画时用）"},
            "target": {"type": "str", "required": False,
                       "desc": "mode=edit 时要求打开的就是这一张"},
            "update": {"type": "bool", "required": False, "default": True,
                       "desc": "UpdateEditRebuild（是否立即重建）"},
            "reset": {"type": "bool", "required": False, "default": True}},
    requires=("sketch_closed",), effect=("sketch_open",), verified=True)
def op_sketch3d_begin(sw, step, ctx):
    mode = str(_p(step, ctx, "mode", "new") or "new").lower()
    if mode not in ("new", "edit"):
        raise SwError(f"`mode` 只能是 'new' 或 'edit'（实得 {mode!r}）")
    before = _existing_3d_sketches(sw)
    before_set = set(before)
    tgt = _p(step, ctx, "target")
    if mode == "edit":
        if not before:
            raise SwError("`mode='edit'` 但文档里**没有任何 3D 草图** —— 第一次请用 mode='new'")
        if tgt and str(tgt) not in before_set:
            raise SwError(f"`target='{tgt}'` 不在已有 3D 草图 {before} 里")
    sm = _sm(sw)
    # ★ tree0 在**调用之前**记（`Insert3DSketch` 会立刻建特征；记晚了 diff 就是空的）。
    ctx["sketch3d_tree0"] = {n for _t, n in sw.feature_names()}
    _res, err = _call(member, sm, "Insert3DSketch",
                      bool(_p(step, ctx, "update", True)))
    active = _active_sketch_name(sw)
    if active is None:
        raise SwError("没进入 3D 草图（GetActiveSketch2 回 None）。\n"
                      "  ○ 当前是不是已经开着一张草图了？（先在别的草图里）\n"
                      "  ○ `Insert3DSketch` 是**开关式** —— 若已在 3D 草图里，再调一次是**退出**。"
                      + (f"\n  ○ COM 报错：{err}" if err else ""))
    existed = active in before_set
    if mode == "new" and existed:
        raise SwError(
            f"`mode='new'` 想要**新建**一张 3D 草图，但读回发现进的是**已有**的 '{active}'。\n"
            f"  ★★ 官方契约只承诺『新建一张 3D 草图 或 关闭活动草图』，**没承诺**\n"
            f"     `打开已有草图去编辑` —— 同机实测两种行为都出现过。\n"
            f"  ○ 要接着画那张 ⇒ 明确写 `mode='edit'`（能进就进，进不去会报错）；\n"
            f"  ○ 要把两批曲线分开 ⇒ 用 `curve_xyz`（不需要草图，且判据更硬）；\n"
            f"  ○ 或者把 3D 引导实体都放进**同一张**草图（推荐口径）。")
    if mode == "edit" and not existed:
        raise SwError(
            f"`mode='edit'`（想编辑 {tgt!r}）但读回发现**新建**了 '{active}'。\n"
            f"  ★ `Insert3DSketch` **不保证**打开已有草图 —— 这条路线在真机上不可靠，\n"
            f"     本 op 选择**报错而不是将就**（否则你会以为在改老草图，其实新建了一张）。")
    if tgt and active != str(tgt):
        raise SwError(f"想编辑 '{tgt}'，但实际打开的是 '{active}'")
    with contextlib.suppress(Exception):
        sm.DisplayWhenAdded = False
    if _p(step, ctx, "reset", True):
        ctx["segments"] = []
        ctx["dimmed_segments"] = []
    ctx["sketch_open"] = True
    ctx["sketch_is_3d"] = True
    ctx["sketch3d_active"] = active
    ctx["sketch3d_reuse"] = existed
    return {"mode": "3d", "sketch": active, "created_new": (not existed)}


@op("sketch3d_end", "结束 3D 草图", "sketch",
    "退出 3D 草图（**再调一次** `Insert3DSketch` —— 它是开关式）。\n"
    "★ 判据：退出后 `GetActiveSketch2()` 应回 None（读回，不信返回值）。\n"
    "★ 草图名取自 `sketch3d_begin` **读回的活动草图名**（不靠特征树 diff —— 复用场景 diff 为空）。\n"
    "★ `name` 只在**新建**场景下改名；`mode='edit'` 复用的草图**保持原名**（避免把别人的名字改掉）。",
    api=["ISketchManager.Insert3DSketch", "IModelDoc2.GetActiveSketch2",
         "IModelView.GraphicsRedraw2"],
    params={"name": {"type": "str", "required": False},
            "update": {"type": "bool", "required": False, "default": True}},
    requires=("sketch_open",), effect=("sketch_closed",), verified=True)
def op_sketch3d_end(sw, step, ctx):
    sm = _sm(sw)
    before = _active_sketch_name(sw)
    _res, err = _call(member, sm, "Insert3DSketch",
                      bool(_p(step, ctx, "update", True)))
    with contextlib.suppress(Exception):
        sm.DisplayWhenAdded = True
    if _isketch_of(sw) is not None:
        raise SwError("3D 草图没退出（GetActiveSketch2 仍回一个草图）。\n"
                      "  ○ `Insert3DSketch` 是开关式：进、出各调一次。\n"
                      "  ○ 确认进入时确实进过（`sketch3d_begin`）"
                      + (f"（COM 报错：{err}）" if err else ""))
    ctx["sketch_open"] = False
    ctx["sketch_is_3d"] = False
    with contextlib.suppress(Exception):
        member(sw.view(), "GraphicsRedraw2")
    sw.clear_selection()
    sk_name = ctx.get("sketch3d_active") or before
    # 新建场景才改名；复用（edit）保持原名
    want = _p(step, ctx, "name")
    renamed = False
    if want and sk_name and not ctx.get("sketch3d_reuse") and str(want) != str(sk_name):
        with contextlib.suppress(Exception):
            typed(sw.model.FeatureByName(sk_name), "IFeature").Name = str(want)
        # ★ 读回核对：改名**会静默失败**（2026-10-09 真机踩到 —— `Name =` 不抛异常，
        #   但按新名字 `FeatureByName` 回 None）。典型诱因：草图里**一个图元都没有**
        #   （样条点表没包 `variant_r8()` 时就会「静默消失」，见 `spline` op 文档）。
        back = None
        with contextlib.suppress(Exception):
            f2 = sw.model.FeatureByName(str(want))
            if f2 is not None:
                back = str(member(typed(f2, "IFeature"), "Name"))
        if not back:
            raise SwError(
                f"3D 草图改名没生效：'{sk_name}' → '{want}' 之后按新名字查不到。\n"
                f"  ○ 若草图里**一个图元都没有**，SolidWorks 可能不保留这张草图特征 ——\n"
                f"    典型是 `spline` 的点表**没包 `variant_r8()`**（样条静默消失）；\n"
                f"  ○ 也可能名字与已有特征冲突（换一个名字）。")
        sk_name = back
        renamed = True
    ctx["last_sketch"] = sk_name
    out = {"sketch": sk_name}
    if renamed:
        out["renamed_to"] = sk_name
    return out


# ============================================================ 曲面族（S）
#
# ★★★ 曲面与实体的**判据完全不同**（2026-10-09 立）：
#   实体件看 **体积/包围盒/质量**；曲面件**没有体积、没有质量**，只能看
#   **曲面体数 + 面积**。混着看会得出"建了个空零件"的假结论。
#   分型靠 `IBody2.GetType()`（`swBodyType_e`：0=实体、1=曲面体、-1=全部）——
#   从 tlb 抽的签名（`IBody2.IsSolid` / `IBody2.GetArea` **不存在**，别照别的 CAD 写）。
#
#   参考：`references/80-crh380cl-project.md`（CRH380CL 曲面建模计划书，S 族定义在那里）。

def _body_stat(sw) -> dict:
    """分别数「实体 / 曲面体」并累加**总面积**（mm²）。曲面建模每一步的验收判据在这。

    ★ 为什么不能只看"体数变了没"：实体件和曲面件都回"有体"。
      必须分型 —— `IBody2.GetType()`：`0`=实体、`1`=曲面体。
      ★ 面积只能**逐面累加**（`IFace2.GetArea`）：`IBody2` 上**没有** `GetArea`
      （tlb 里查过：`IBody2.GetArea` 不存在）；`GetFaces()` 才是对的入口。
      ★ 取体走 `sw.bodies(body_type=-1)`（显式全部体）—— 默认回退链第一档只要实体，
      纯曲面零件会被第二档兜住，**读不出"到底有没有曲面"**。
    """
    out = {"n_all": 0, "n_solid": 0, "n_sheet": 0, "area_mm2": None}
    allb = sw.bodies(body_type=int(sw.C("swAllBodies")))
    n_solid = n_sheet = 0
    area = 0.0
    got_area = False
    for b in allb:
        t = None
        with contextlib.suppress(Exception):
            t = int(member(typed(b, "IBody2"), "GetType"))
        if t == 0:
            n_solid += 1
        else:
            n_sheet += 1
        for fc in (member(typed(b, "IBody2"), "GetFaces") or []):
            with contextlib.suppress(Exception):
                area += float(member(typed(fc, "IFace2"), "GetArea"))
                got_area = True
    out.update(n_all=len(allb), n_solid=n_solid, n_sheet=n_sheet,
               area_mm2=round(area * 1e6, 4) if got_area else None)
    return out


@op("surface_extrude", "拉伸曲面", "feature",
    "把一张草图拉成**曲面**（sheet body），不是实体。\n"
    "★★ **与 `extrude_boss` 的三处不同**（别照搬实体经验）：\n"
    "   ① 出来的东西**没有体积、没有质量** ⇒ 判据只能看**曲面体数 + 面积**（本 op 会回 `body_*`）；\n"
    "   ② **不要求闭合轮廓**（一条直线都能拉出面），`requires` 仍按 `sketch_closed` 走只是保守；\n"
    "   ③ 曲面拉伸做完后零件里**仍然是 0 个实体** —— 别当成失败。\n"
    "★ 走 `IModelDoc2.FeatureExtruRefSurface`（**15 个位置参数**，签名 2026-10-09 抽自 tlb）。\n"
    "  ★ `Sd` / `Flip` 在 tlb 里**没有注释** ⇒ 内部按「单向前 / 单向反 / 双向」三个组合依次试，\n"
    "    判据是**特征树真涨了**（返回值是 void，看不了 —— 铁律①）。",
    api=["IModelDoc2.FeatureExtruRefSurface"],
    params={"profile": {"type": "str", "required": False, "desc": "草图名；不给则用刚画的"},
            "depth": {"type": "float", "unit": "mm", "required": False, "default": 10},
            "end_cond": {"type": "str", "required": False, "default": "swEndCondBlind"},
            "reverse_dir": {"type": "bool", "required": False, "default": False,
                            "desc": "留给调用方表达意图；实际由内部组合试出"},
            "both_directions": {"type": "bool", "required": False, "default": False},
            "draft_deg": {"type": "float", "unit": "deg", "required": False,
                          "desc": "拔模角(度)；给了就是拔模曲面"},
            "draft_outward": {"type": "bool", "required": False, "default": False},
            "name": {"type": "str", "required": False}},
    requires=("sketch_closed",), effect=("has_surface",), verified=True)
def op_surface_extrude(sw, step, ctx):
    prof = _p(step, ctx, "profile") or ctx.get("last_sketch")
    if not prof:
        raise SwError("没给 profile，也没有刚画的草图 —— 曲面拉伸必须有个轮廓")
    t = int(sw.C(str(_p(step, ctx, "end_cond", "swEndCondBlind") or "swEndCondBlind")))
    d = mm(float(_p(step, ctx, "depth", 10) or 10))
    draft = _p(step, ctx, "draft_deg")
    ang = deg(float(draft)) if draft else 0.0
    both = bool(_p(step, ctx, "both_directions", False))
    notes: list[str] = []
    tree0 = {n for _t, n in sw.feature_names()}
    b0 = _body_stat(sw)
    f, err = None, None
    for tag, sd, flip in (("单向前", True, False), ("单向反", True, True), ("双向", False, False)):
        if not _select_feature(sw, prof, 0):
            raise SwError(f"选不中拉伸曲面用的草图 '{prof}'")
        _res, err = _call(member, sw.model, "FeatureExtruRefSurface",
                          bool(sd), bool(flip), False,
                          t, (t if both else int(sw.C("swEndCondBlind"))),
                          d, (d if both else 0.0),
                          bool(draft), bool(draft),
                          bool(_p(step, ctx, "draft_outward", False)), False,
                          ang, ang, False, False)
        sw.clear_selection()
        f = _new_feature(sw, tree0)
        if f is not None:
            notes.append(f"参数组合={tag}(Sd={sd},Flip={flip})")
            break
        notes.append(f"{tag}(Sd={sd},Flip={flip})：没生成特征")
    if f is None:
        raise SwError("曲面拉伸没生成特征，三个参数组合都没成：\n"
                      + "".join(f"  · {x}\n" for x in notes)
                      + (f"  COM 报错：{err}" if err else "")
                      + "  排查：① 草图选上了没有 ② 草图是不是被别的特征吃掉了")
    if isinstance(f, tuple):
        raise SwError(f"曲面拉伸特征重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    b1 = _body_stat(sw)
    if b1["n_sheet"] <= b0["n_sheet"]:
        notes.append("★★ 曲面体数**没有增加** —— 特征树涨了但没造出面（铁律①：不信返回值）")
    if b1["n_solid"] > b0["n_solid"]:
        notes.append("★★ 出来的是**实体**不是曲面 —— `IsThin/IsSolid` 类语义要再核")
    name = _fname(f)
    if _p(step, ctx, "name"):
        with contextlib.suppress(Exception):
            f.Name = _p(step, ctx, "name")
        name = str(_p(step, ctx, "name"))
    ctx.setdefault("feature_names", []).append(name)
    ctx["last_surface"] = name
    return {"feature": name, "body_before": b0, "body_after": b1, "notes": notes or None}


@op("surface_revolve", "旋转曲面", "feature",
    "把草图绕一条轴旋转成**曲面**（sheet body）。轴 = 草图里的中心线，或 `axis` 给基准轴名。\n"
    "★ 走 `IModelDoc2.InsertRevolvedRefSurface(Angle, ReverseDir, Angle2, RevType)`（4 参，返回 **void**）。\n"
    "★ `RevType` 用 `swRevolveTypeOneDirection`(0) —— 角度由 `Angle`（**弧度**）给，\n"
    "  别用 `swRevolveTypeOneDirection360Degrees`(3) 那种「类型自带角度」的档（容易和 Angle 打架）。\n"
    "★ 判据同样落**曲面体数 + 面积**，不看体积。",
    api=["IModelDoc2.InsertRevolvedRefSurface"],
    params={"profile": {"type": "str", "required": False, "desc": "草图名；不给则用刚画的"},
            "axis": {"type": "str", "required": False, "desc": "基准轴名；不给则用草图中心线"},
            "angle": {"type": "float", "unit": "deg", "required": False, "default": 360},
            "reverse_dir": {"type": "bool", "required": False, "default": False},
            "name": {"type": "str", "required": False}},
    requires=("sketch_closed",), effect=("has_surface",), verified=True)
def op_surface_revolve(sw, step, ctx):
    prof = _p(step, ctx, "profile") or ctx.get("last_sketch")
    if not prof:
        raise SwError("没给 profile，也没有刚画的草图 —— 旋转曲面必须有个轮廓")
    sw.clear_selection()
    if not _select_feature(sw, prof, 0):
        raise SwError(f"选不中旋转曲面用的草图 '{prof}'")
    axis = _p(step, ctx, "axis")
    if axis:
        if not sw.ext.SelectByID2(axis, "AXIS", 0, 0, 0, True, 1, None, 0):
            sw.ext.SelectByID2(axis, "EXTSKETCHSEG", 0, 0, 0, True, 1, None, 0)
    tree0 = {n for _t, n in sw.feature_names()}
    b0 = _body_stat(sw)
    _res, err = _call(member, sw.model, "InsertRevolvedRefSurface",
                      deg(float(_p(step, ctx, "angle", 360) or 360)),
                      bool(_p(step, ctx, "reverse_dir", False)),
                      0.0,
                      int(sw.C("swRevolveTypeOneDirection")))
    sw.clear_selection()
    f = _new_feature(sw, tree0)
    if f is None:
        raise SwError("旋转曲面没生成特征。草图必须有中心线（或给了 axis），且轮廓不能穿过轴"
                      + (f"（COM 报错：{err}）" if err else ""))
    if isinstance(f, tuple):
        raise SwError(f"旋转曲面特征重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    b1 = _body_stat(sw)
    notes = []
    if b1["n_sheet"] <= b0["n_sheet"]:
        notes.append("★★ 曲面体数没增加 —— 特征树涨了但没造出面（铁律①）")
    name = _fname(f)
    if _p(step, ctx, "name"):
        with contextlib.suppress(Exception):
            f.Name = _p(step, ctx, "name")
        name = str(_p(step, ctx, "name"))
    ctx.setdefault("feature_names", []).append(name)
    ctx["last_surface"] = name
    return {"feature": name, "body_before": b0, "body_after": b1, "notes": notes or None}


#: 截面/引导线的选择类型串：按「草图 → 通过 XYZ 点的曲线 → 特征」依次退。
#: ★ 曲线用 `REFERENCECURVES`：§130 实测 —— `CurveInFile` 是**特征类型名**，
#:   `REFERENCECURVES` 才是 `SelectByID2` 吃的**选择串**，两个不是一回事。
_PROFILE_SEL_TYPES = ("SKETCH", "REFERENCECURVES", "BODYFEATURE")


def _select_profiles(sw, names, mark: int, clear: bool = True) -> list:
    """按**给定顺序**多选一组截面/引导线，返回没选中的名字。

    ★★ 必须**累积**选择：`append=False` 会**清掉前面的选择**，多目标时每个都传 False
      ⇒ 只剩最后一个（§129 记的坑，与加厚/缝合同源）。
      ⇒ 统一做法：先 `clear_selection()`，再每个都 `append=True`。
    ★★★ **顺序 = 放样方向**（官方 Remarks："selection of the profiles must be in an
      order consistent with the desired direction of the loft"）
      ⇒ 所以这里**不排序、不去重**，原样喂进去。
    ★★★ **`clear` 只在「一组选择的第一组」传 True**（2026-10-11 真机血的教训）：
      截面（mark=1）、引导线（mark=2）、中心线（mark=4）是**同一批选择**的三类，
      必须**共存**在一个选择集里。若每组都清场，后一组会把前一组**全清掉** ——
      症状是「曲面照建、但形状完全不对、不报任何错」：
      S2 车头就是**只留了 2 条引导线（都在 y=0 平面）**去放样，
      出来一张 **y 恒为 0 的退化曲面**（16 个三角片、面积只有真值的 1/3），
      而 `check_body` 照样报「1 曲面体」——**判据全绿、几何全错**。
    """
    if clear:
        sw.clear_selection()
    miss = []
    for nm in names:
        ok = False
        for typ in _PROFILE_SEL_TYPES:
            if _pick(sw, {"name": str(nm), "type": typ}, mark=mark, append=True):
                ok = True
                break
        if not ok:
            miss.append(str(nm))
    return miss


def _curve_bbox_mm(sw, names) -> tuple[list, list] | None:
    """一组「通过 XYZ 点的曲线」的**并集 bbox**（mm）。读不到点就回 None（不误报）。

    ★ 专给放样用的几何护栏：放样曲面过所有截面 ⇒ 它的 bbox 必然包住这个并集。
      装不下 = 用的根本不是这些截面（见 `op_surface_loft` 的选择集护栏）。
    """
    pts: list = []
    for nm in names or []:
        f = None
        with contextlib.suppress(Exception):
            f = sw.model.FeatureByName(str(nm))
        if f is None:
            continue
        p, _e = _curve_points_mm(f)
        if p:
            pts.extend(p)
    if not pts:
        return None
    return ([min(q[i] for q in pts) for i in range(3)],
            [max(q[i] for q in pts) for i in range(3)])


@op("surface_loft", "放样曲面", "feature",
    "把**若干截面**（草图 / 3D 曲线）按给定顺序放样成一张**曲面**（sheet body）——"
    " **车头鼻锥的主力**（CRH380CL S2）。\n"
    "★★★ **三个官方约定（2026-10-10 抽自官方 Remarks，写错就静默不出面）**：\n"
    "  ① **选择标记**：截面 = **mark 1**、引导线 = **2**、中心线 = **4**、\n"
    "     起点相切矢量 = 8、终点相切矢量 = 32；\n"
    "  ② **选择顺序 = 放样方向**（\"must be in an order consistent with the desired\n"
    "     direction of the loft\"）—— 顺序反了曲面会扭；\n"
    "  ③ ★ **曲面放样的截面可以是开口的**（\"the section profiles can be open\"）——\n"
    "     这正是曲面放样比实体放样宽的地方。\n"
    "★ `closed=True` 的硬约束：**至少 3 个截面**；若带引导线，**引导线也必须闭合**。\n"
    "★ 官方**强烈建议**给引导线，尤其在特征树里选截面时。\n"
    "★ 判据落**读回**：曲面体数 + 总面积（`_body_stat`）。返回值是 void，看不了（铁律①）。\n"
    "★ 走 `IModelDoc2.InsertLoftRefSurface2(Closed, KeepTangency, ForceNonRational,\n"
    "  TessToleranceFactor, StartMatchingType, EndMatchingType)`（6 参，2026-10-10 抽自 tlb）。\n"
    "  `start/end_matching`：0=无、1=切于截面法向、2=切于选定矢量、3=切于所有相邻面。",
    api=["IModelDoc2.InsertLoftRefSurface2"],
    params={"sections": {"type": "list",
                         "desc": "截面名，**按放样方向排序**（mark=1；不排序、不去重）"},
            "guides": {"type": "list", "required": False,
                       "desc": "引导线名（mark=2）；官方**强烈建议**给"},
            "centerline": {"type": "str", "required": False, "desc": "中心线名（mark=4）"},
            "closed": {"type": "bool", "required": False, "default": False,
                       "desc": "闭合放样；True 时**至少 3 个截面**"},
            "keep_tangency": {"type": "bool", "required": False, "default": False},
            "force_non_rational": {"type": "bool", "required": False, "default": False},
            "tess_tolerance": {"type": "float", "required": False, "default": 1.0,
                               "desc": "带中心线时控制中间截面数；官方默认 1.0"},
            "start_matching": {"type": "int", "required": False, "default": 0,
                               "desc": "起点相切类型 0/1/2/3"},
            "end_matching": {"type": "int", "required": False, "default": 0},
            "name": {"type": "str", "required": False}},
    requires=("sketch_closed",), effect=("has_surface",), verified=True)
def op_surface_loft(sw, step, ctx):
    sec = _p(step, ctx, "sections") or []
    if not isinstance(sec, (list, tuple)) or len(sec) < 2:
        raise SwError(
            "`sections` 必须给**至少 2 个**截面名（按放样方向排序）。\n"
            "  ★ 只给 1 个截面时，官方要求『所选引导线必须是闭合曲线』——"
            "那种情形本 op 不覆盖。")
    closed = bool(_p(step, ctx, "closed", False))
    if closed and len(sec) < 3:
        raise SwError(f"`closed=True` 要求**至少 3 个截面**（官方硬约束），只给了 {len(sec)} 个。")
    guides = list(_p(step, ctx, "guides") or [])
    cl = _p(step, ctx, "centerline")
    if closed and guides:
        raise SwError("`closed=True` 且带引导线时，官方要求**引导线也闭合**。\n"
                      "  ★ 本 op 无法自动判定引导线是否闭合 ⇒ 确认后请用 `closed=False` 表达，"
                      "或去掉引导线。")
    notes: list[str] = []
    tree0 = {n for _t, n in sw.feature_names()}
    b0 = _body_stat(sw)
    missing = _select_profiles(sw, sec, 1)
    if missing:
        raise SwError(
            f"这些**截面**选不中：{missing}\n"
            f"  ○ 选择标记必须是 **mark=1**（官方 Remarks：profile selections should be 1）；\n"
            f"  ○ 类型串按 SKETCH → REFERENCECURVES（通过 XYZ 点的曲线）→ BODYFEATURE 依次退过；\n"
            f"  ○ 排查：名字是不是**草图/曲线名**；这个零件里到底有什么（`feature_names()`）。")
    if guides:
        gmiss = _select_profiles(sw, guides, 2, clear=False)   # ★ 不能清场，否则截面全丢
        if gmiss:
            sw.clear_selection()
            raise SwError(f"这些**引导线**（mark=2）选不中：{gmiss}")
    if cl:
        _select_profiles(sw, [cl], 4, clear=False)             # 中心线 mark=4
    # ★★★ 护栏（2026-10-11 真机血的教训）：截面/引导线/中心线是**同一批选择**，
    #   三者必须共存在选择集里。曾经因为「选引导线时清了场」，放样只拿到引导线
    #   ⇒ 出来一张**退化曲面**（y 恒 0）却**不报任何错**、判据全绿。
    #   ⇒ 放样前数一遍选择集，少一个就当场拦下。
    want_n = len(sec) + len(guides) + (1 if cl else 0)
    sel_n = None
    with contextlib.suppress(Exception):
        sel_n = int(member(sw.selmgr(), "GetSelectedObjectCount2", -1))
    if sel_n is not None and sel_n != want_n:
        sw.clear_selection()
        raise SwError(
            f"选择集里只有 {sel_n} 个对象，应该有 {want_n} 个"
            f"（截面 {len(sec)} + 引导线 {len(guides)}"
            + (f" + 中心线 1" if cl else "") + "）。\n"
            "  ★★ 这是**静默出坏面**的头号原因：SolidWorks 会拿残缺的选择集**照样建出曲面**，\n"
            "    而且**不报错**（本项目就出过「只留引导线 ⇒ y 恒为 0 的退化曲面」）。\n"
            "  ○ 排查：是不是某组选择把前一组清掉了（`clear=False` 有没有漏传）。")
    notes.append(f"选择集 {sel_n if sel_n is not None else '?'} 个（截面 {len(sec)}"
                 + (f" + 引导线 {len(guides)}" if guides else "")
                 + (" + 中心线 1" if cl else "") + "）")
    f, err = _call(member, sw.model, "InsertLoftRefSurface2",
                   closed,
                   bool(_p(step, ctx, "keep_tangency", False)),
                   bool(_p(step, ctx, "force_non_rational", False)),
                   float(_p(step, ctx, "tess_tolerance", 1.0) or 1.0),
                   int(_p(step, ctx, "start_matching", 0) or 0),
                   int(_p(step, ctx, "end_matching", 0) or 0))
    sw.clear_selection()
    nf = _new_feature(sw, tree0)
    if isinstance(nf, tuple):
        raise SwError(f"放样曲面特征重建失败 {nf[1]}（{FEATURE_ERROR_NAMES.get(nf[1], '未知')}）"
                      + (f"；COM 报错：{err}" if err else ""))
    if nf is None:
        raise SwError(
            "放样曲面**没生成特征**（特征树没涨）。\n"
            "  排查顺序：① 截面选上了没有（mark=1，且**按放样方向排序**）；\n"
            "            ② 截面是否**在彼此不同的位置**（同一平面上的两个截面放不出面）；\n"
            "            ③ `closed=True` 但截面不足 3 个 / 引导线没闭合；\n"
            "            ④ `start/end_matching=2`（切于选定矢量）却**没给矢量**"
            + (f"\n  COM 报错：{err}" if err else ""))
    b1 = _body_stat(sw)
    if b1["n_sheet"] <= b0["n_sheet"]:
        notes.append("★★ 曲面体数**没有增加** —— 特征树涨了但没造出面（铁律①：不信返回值）")
    if b1["n_solid"] > b0["n_solid"]:
        notes.append("★★ 出来的是**实体**不是曲面")
    # ★★★ 几何护栏（2026-10-11）：放样曲面**过所有截面** ⇒ 它的 bbox 必然**包住**截面点集的并集。
    #   若装不下，说明放样用的根本不是这些截面（典型：选择集被清成只剩引导线）
    #   —— 这正是「体数/面积判据全绿、几何却全错」的那一类，必须当场拦下。
    sb = _curve_bbox_mm(sw, sec)
    if sb:
        boxes = []
        for _b in sw.bodies(body_type=-1):
            with contextlib.suppress(Exception):
                boxes.append([float(v) * 1000.0 for v in
                              member(typed(_b, "IBody2"), "GetBodyBox")])
        if boxes:
            lo = [min(b[i] for b in boxes) for i in range(3)]
            hi = [max(b[i + 3] for b in boxes) for i in range(3)]
            shorts = [("xyz"[ax], round(lo[ax], 3), round(hi[ax], 3),
                       round(sb[0][ax], 3), round(sb[1][ax], 3))
                      for ax in range(3)
                      if lo[ax] > sb[0][ax] + 1.0 or hi[ax] < sb[1][ax] - 1.0]
            if shorts:
                raise SwError(
                    "放样出来的曲面**装不下**截面（bbox 包不住）⇒ 用的不是这些截面。\n"
                    + "".join(f"  轴 {a}: 曲面 [{v0},{v1}] vs 截面需要 [{e0},{e1}]\n"
                              for a, v0, v1, e0, e1 in shorts)
                    + "  ★★ 这是「判据全绿、几何全错」的典型：SolidWorks 拿残缺的选择集\n"
                      "    照样建面、照样不报错。头号原因是**选引导线时把截面清掉了**。")
    notes.append(f"截面 {len(sec)} 个（mark=1）"
                 + (f"，引导线 {len(guides)} 条（mark=2）" if guides else "")
                 + (f"，中心线 '{cl}'（mark=4）" if cl else ""))
    out = _finish(sw, nf, step, ctx,
                  {"body_before": b0, "body_after": b1, "notes": notes})
    ctx["last_surface"] = out["feature"]
    return out


#: 平面区域的边界选择类型（按顺序退）：参考曲线 → 草图 → 特征
_PLANAR_SEL_TYPES = ("REFERENCECURVES", "SKETCH", "BODYFEATURE")


def _planar_edge_pick(sw, boundary, edge_at):
    """给平面区域找「按坐标拾取边」的落点（mm）。返回 `(point|None, source)`。"""
    if edge_at and len(edge_at) >= 3:
        return [float(edge_at[0]), float(edge_at[1]), float(edge_at[2])], "edge_at 参数"
    if boundary:
        feat = None
        with contextlib.suppress(Exception):
            feat = sw.model.FeatureByName(str(boundary))
        if feat is not None:
            pts, _err = _curve_points_mm(typed(feat, "IFeature"))
            if pts:
                return list(pts[0]), f"'{boundary}' 的首点"
    return None, None


@op("surface_planar", "平面区域（封口）", "feature",
    "把一条**封闭边界**填成一张平面曲面 —— 曲面收口时用它**封盖**（堵住放样曲面的开口）。\n"
    "★★ 走 `IModelDoc2.InsertPlanarRefSurface()`（**0 参**、全靠**预选**边界；签名见矩阵 S5）。\n"
    "  ⇒ 成败**全在选择**：先选中一条**封闭且共面**的边界，再调 API。\n"
    "★★★ **必须选 `EDGE`（边），不能选特征**（2026-10-11 真机对照实验确诊）。\n"
    "  同一条闭合曲线，选 `REFERENCECURVES`（曲线特征）⇒ API 返回 False、**不建特征、不报错**；\n"
    "  改选它自己的 `EDGE`（按坐标落在曲线上）⇒ 一次成功（sheet 0→1、面积出现）。\n"
    "  ★ 症状与 `thicken`/`surface_knit` 的 mark 坑同族：**选错层级 = 静默失败**。\n"
    "  ⇒ 本 op 首选「按坐标拾取边」：`edge_at` 显式给点；不给就从参考曲线的**首点**取。\n"
    "  ★ 选 **特征级**（`_PLANAR_SEL_TYPES` = SKETCH → REFERENCECURVES → BODYFEATURE）只作兜底。\n"
    "  ★ 边界**必须共面**（我们拿它封鼻端/尾端断面，本来就在同一个 x 平面上）。\n"
    "★ 边界来源**两种**（`requires` 因此写成 OR 组 `(has_curve 或 has_sketch)`）：\n"
    "  ① **参考曲线**（`curve_xyz`，`REFERENCECURVES`）—— 计划里只写 `boundary=曲线名` 即可\n"
    "     （本 op 自动读它的首点当落点）；② **草图边**（把草图当边界，如整圆）—— 此时\n"
    "     **必须显式给 `edge_at`**（草图拿不到曲线点），例：`edge_at=[20,0,0]`。\n"
    "★★ 判据：**曲面体数 +1** 且**面积**增加，**不看返回值**（这里返回值恰好是 Boolean，\n"
    "  但按铁律① 仍然以「特征树涨没涨 + 体数/面积」为准）。\n"
    "★ 真机实测：闭合曲线（through-points **样条**，r≈20）×24 点 ⇒ 封盖面积 **1256.4637 mm²**，\n"
    "  比 πr²=1256.6371 小 **0.17**（0.014%）—— 这是**样条不是圆弧**的真实几何差，不是 bug\n"
    "  （用真圆草图封盖才会逐位等于 πr²）。探针 `surface_planar`；见 30-pitfalls §133。",
    api=["IModelDoc2.InsertPlanarRefSurface"],
    params={"boundary": {"type": "str", "required": False,
                         "desc": "封闭边界名（参考曲线 / 草图）；不给则用上一条曲线（`last_curve`）"},
            "edge_at": {"type": "list", "unit": "mm", "required": False,
                        "desc": "★首选：边界上的一点 [x,y,z]，按坐标拾取**边**（EDGE）；"
                                "不给则从参考曲线首点自动取"},
            "name": {"type": "str", "required": False}},
    requires=(("has_curve", "has_sketch"),), effect=("has_surface",), verified=True)
def op_surface_planar(sw, step, ctx):
    nm = _p(step, ctx, "boundary") or ctx.get("last_curve")
    edge_at = _p(step, ctx, "edge_at")
    if not nm and not edge_at:
        raise SwError("既没给 `boundary`/`edge_at`，也没有上一条曲线（`last_curve`）—— "
                      "平面区域是「把这条**封闭边界**填成面」，没有边界就没有这步")
    nm = str(nm) if nm else ""
    tree0 = {n for _t, n in sw.feature_names()}
    b0 = _body_stat(sw)
    # ★★ 首选：按坐标拾取**边**（EDGE）—— 真机对照实验证明只有这条能成
    pick, src = _planar_edge_pick(sw, nm, edge_at)
    hit = None
    sw.clear_selection()
    if pick:
        ok = False
        with contextlib.suppress(Exception):
            ok = bool(sw.ext.SelectByID2(
                "", "EDGE", mm(pick[0]), mm(pick[1]), mm(pick[2]), False, 0, None,
                _C(sw, "swSelectOptionExtensive", 1)))
        if ok:
            hit = f"EDGE @({pick[0]:.3f},{pick[1]:.3f},{pick[2]:.3f}) ← {src}"
    # ★ 兜底：特征级选择（SKETCH → REFERENCECURVES → BODYFEATURE）
    if hit is None and nm:
        for typ in _PLANAR_SEL_TYPES:
            ok = False
            with contextlib.suppress(Exception):
                ok = bool(sw.ext.SelectByID2(nm, typ, 0, 0, 0, False, 0, None, 0))
            if ok:
                hit = typ
                break
    if hit is None:
        raise SwError(
            f"选不中封闭边界（boundary='{nm}'，edge_at={edge_at}）。\n"
            f"  ○ ★★ 该 API 要的是**边（EDGE）**：给它 `edge_at` = 边界上的一点（mm）。\n"
            f"  ○ 若给的是**参考曲线名**，本 op 会自动读它的首点当落点（读不到点就会失败）。\n"
            f"  ○ 边界必须**封闭且共面**；排查：`check_curve` 看它 `closed` 没。\n"
            f"  ○ 特征级兜底链={_PLANAR_SEL_TYPES}（已知对参考曲线**无效**，仅对草图可能有效）。")
    _r, err = _call(member, sw.model, "InsertPlanarRefSurface")
    sw.clear_selection()
    f = _new_feature(sw, tree0)
    if isinstance(f, tuple):
        raise SwError(f"平面区域特征重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    if f is None:
        raise SwError(
            f"平面区域没生成特征（特征树没涨；API 返回 {_r!r}）。\n"
            "  ○ 官方要求：**先选中一条封闭边界（边）**，再调 0 参的 `InsertPlanarRefSurface()`。\n"
            "  ○ 常见原因：① 边界**不闭合**（曲线首尾没接上）；② 边界**不共面**（三维空间曲线）；\n"
            "    ③ 选中的是**特征**而不是它的**边** —— 真机已证选特征会静默失败（见 op 文档）。"
            + (f"\n  COM 报错：{err}" if err else ""))
    b1 = _body_stat(sw)
    notes = [f"边界 '{nm or '(edge_at)'}'（按 {hit} 选中）"]
    if b1["n_sheet"] <= b0["n_sheet"]:
        notes.append("★★ 曲面体数**没有增加** —— 特征树涨了但没造出面（铁律①）")
    if b1["n_solid"] > b0["n_solid"]:
        notes.append("★★ 出来的是**实体**不是曲面")
    area_added = None
    if b0.get("area_mm2") is not None and b1.get("area_mm2") is not None:
        area_added = round(b1["area_mm2"] - b0["area_mm2"], 4)
        notes.append(f"面积增加 {area_added} mm²")
    out = _finish(sw, f, step, ctx,
                  {"body_before": b0, "body_after": b1, "area_added_mm2": area_added,
                   "boundary": nm or None, "selected_as": hit, "notes": notes})
    ctx["last_surface"] = out["feature"]
    return out


@op("surface_knit", "缝合曲面", "feature",
    "把多张曲面缝成一张；`form_solid=True` 时**缝成实体**（曲面建模的收口方式之一）。\n"
    "★★★ **必读：缝合必须用 `mark=1` 选曲面体**（2026-10-09 真机确诊）。\n"
    "  官方 Remarks 原文 `Make selections using SelectByID2 with a mark number of 1.`\n"
    "  —— 与加厚是**同一句话**。传 mark=0 时典型症状是\n"
    "  **无异常、返回 None、特征树不涨**（官方论坛上就有人栽在这里，一直回 null）。\n"
    "  本 op 已内置 mark=1 + `SURFACEBODY`（退 `BODYFEATURE`，再退 `GetSelectionId`）。\n"
    "★★ `tolerance`（缝合容差）**必须显式给**：太大 ⇒ 把不该连的连上；太小 ⇒ 缝不上。\n"
    "  **官方硬边界：容差只能在 0.0001 ~ 0.1 mm 之间**，且要满足\n"
    "  `最小间隙 ≤ 容差 ≤ min(0.1mm, 最大间隙)`。超范围 ⇒ 静默失败。默认 0.01mm。\n"
    "★ 判据落**体数**：`form_solid=True` 时缝完应 `n_solid == 1` 且 `n_sheet` 归零；\n"
    "  只缝面（`form_solid=False`）时 `n_sheet` 应减少。**别信返回值。**\n"
    "★★★ **`form_solid=True` 必须配 `gap_filters=True`，否则静默不成实体**\n"
    "  （2026-10-11 真机对照实验，夹具 = 放样筒 + 两端平面盖，3 张面）：\n"
    "    · gap_filters=**False** + form_solid=True ⇒ 3 体→1 体，但仍是**曲面**（`GetType()=1`），\n"
    "      而且**0 条自由边**（拓扑闭合）—— 「缝上了、就是不成实体」；\n"
    "    · gap_filters=**True**  ⇒ 3 体→**1 实体**（`GetType()=0`）。\n"
    "  ★ 与容差无关：tol 取 0.001 / 0.01 / 0.1 都是「只缝成面」；只有翻 gap_filters 才成实体。\n"
    "  ⇒ 本 op 的 `gap_filters` **默认已改成 True**（旧默认 False 会让 `form_solid` 名不副实）。\n"
    "★ 走 `IFeatureManager.InsertSewRefSurface(UseGapFilters, TryToFormSolid, MergeEntities,\n"
    "  KnitTolerance, MaxValueForGapRange)`（5 参，2026-10-09 抽自 tlb）。\n"
    "  也有一条老式的 `IModelDoc2.InsertSewRefSurface`（**0 参**、纯靠预选）—— 不推荐，参数说不出话。",
    api=["IFeatureManager.InsertSewRefSurface"],
    params={"targets": {"type": "list", "desc": "要缝合的曲面特征名（按体选，mark=1）"},
            "tolerance": {"type": "float", "unit": "mm", "required": False, "default": 0.01,
                          "desc": "缝合容差；**显式给**，且官方限定 0.0001~0.1mm"},
            "form_solid": {"type": "bool", "required": False, "default": True,
                           "desc": "True=缝成实体（TryToFormSolid）"},
            "merge_entities": {"type": "bool", "required": False, "default": True},
            "gap_filters": {"type": "bool", "required": False, "default": True,
                            "desc": "★ 默认 True。**`form_solid=True` 必须开它**，"
                                    "否则只缝成面、不成实体（2026-10-11 真机对照实验）"},
            "max_gap": {"type": "float", "unit": "mm", "required": False, "default": 0.1,
                        "desc": "间隙范围上限（MaxValueForGapRange），官方范例用 0.0001"},
            "name": {"type": "str", "required": False}},
    requires=("has_surface",), verified=True)

def op_surface_knit(sw, step, ctx):
    tg = _p(step, ctx, "targets") or []
    if not isinstance(tg, (list, tuple)) or not tg:
        raise SwError("`targets` 必须给**至少一个**曲面特征名 —— "
                      "缝合是「把这些面缝起来」，没有对象就没有这步")
    # ★ 显式 0 不能被 `or 0.01` 静默换成 0.01（0 是**非法容差**，必须被下面的护栏抓住）。
    #   2026-10-09 由 sw_selftest [5b] 的"容差 0 必须报错"用例逼出来 —— 「or 默认值」
    #   这个全项目惯用法对**合法的 0** 是错的（深度/角度为 0 有别的含义，容差为 0 是无效值）。
    _tol_raw = _p(step, ctx, "tolerance")
    tol_mm = 0.01 if _tol_raw is None else float(_tol_raw)
    # ★ 官方硬边界：0.0001 ~ 0.1 mm。超范围不报错、直接静默失败，所以在这里拦。
    if not (0.0001 <= tol_mm <= 0.1):
        raise SwError(f"缝合容差 {tol_mm}mm 超出官方允许范围 0.0001 ~ 0.1mm。\n"
                      f"  ★ 超范围时 SolidWorks **不报错、直接静默失败**，很难查。")
    form_solid = bool(_p(step, ctx, "form_solid", True))
    tree0 = {n for _t, n in sw.feature_names()}
    b0 = _body_stat(sw)
    # ★★ 2026-10-09：整段「选择 + InsertSewRefSurface」包进**重试**（`COM_RETRY` 次）。
    #   真机证据：同一份几何、同一套参数，在一次会话里**失败过一次**（特征建出来了、
    #   体数却不合并），紧接着单独重跑 **6/6 全成**。这正是本项目已知的
    #   「SolidWorks COM 层瞬时失败」（同 §`add_mate` / `_select_obj` 的处理）。
    #   ⇒ 判据是**读回**（曲面体该少、或实体该多），没变就重试；
    #     重试次数写进返回值 `attempts`，报告里看得见（不是"掩盖错误"）。
    f, err, attempts, b1, merged = None, None, 0, b0, False
    for attempts in range(1, COM_RETRY + 1):
        sw.clear_selection()
        miss = _select_surface_bodies(sw, tg, 1)
        if miss:
            raise SwError(f"这些要缝合的对象选不中：{miss}\n"
                          f"  ○ 缝合的选择方式必须是**按体选 + mark=1**（`SURFACEBODY`）。\n"
                          f"  ○ 排查：名字是不是**曲面特征名**（不是文件名 / 草图名）；\n"
                          f"    这个零件里到底有没有曲面体（`_body_stat` 的 `n_sheet`）。")
        f, err = _call(member, _fm(sw), "InsertSewRefSurface",
                       bool(_p(step, ctx, "gap_filters", True)),
                       form_solid,
                       bool(_p(step, ctx, "merge_entities", True)),
                       mm(tol_mm), mm(float(_p(step, ctx, "max_gap", 0.1) or 0.1)))
        sw.clear_selection()
        b1 = _body_stat(sw)
        # ★ 判据是**读回**：曲面体变少，或实体变多
        merged = b1["n_sheet"] < b0["n_sheet"] or b1["n_solid"] > b0["n_solid"]
        if merged:
            break
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)
    if isinstance(f, tuple):
        raise SwError(f"缝合特征重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    if not merged:
        raise SwError("缝合没生效（体数没有任何变化）。\n"
                      "  常见原因：① 要缝的面**根本没共用边** —— 注意：\n"
                      "      缝合合并的是**共边**（coincident edges），不是**共面**！\n"
                      "      两张只重叠一个面、不共边的曲面，缝了也不会变成一个体。\n"
                      "            ② 选中的不是曲面体（必须 mark=1，见 op 文档）\n"
                      "            ③ `form_solid=True` 但缝出来**不封闭** ⇒ 成不了实体\n"
                      "            ④ 容差不在 0.0001~0.1mm，或不在间隙范围内"
                      + (f"（COM 报错：{err}）" if err else "")
                      + f"\n  ★ 已重试 {attempts} 次（COM 瞬时失败在本机是已知现象）")
    notes = [f"容差={tol_mm}mm，form_solid={form_solid}",
             f"选择=体+mark1，重试={attempts}，gap_filters={bool(_p(step, ctx, 'gap_filters', True))}"]
    if form_solid and b1["n_solid"] < 1:
        notes.append("★★★ `form_solid=True` 但**一个实体都没出现** —— 曲面没封闭，只缝成了面。\n"
                     "  ① 先把 `gap_filters` 打开（**默认已是 True**；关掉时 form_solid 会**静默失效**，\n"
                     "     真机对照实验：3 张闭合面 knit 后仍是曲面、且 0 条自由边 —— 很像成了，其实没有）；\n"
                     "  ② 再看边界是否**真的都对上**（自由边 = 只被 1 张面用到的边）；\n"
                     "  ③ 若确实缝不成体，改用 `thicken`（加厚自带边界侧壁，不要求封闭）。")
    if b1["n_sheet"] >= b0["n_sheet"] and b1["n_solid"] <= b0["n_solid"]:
        notes.append("★★ 体数没有任何变化 —— 缝合**静默无操作**（容差/选择集问题）")
    name = _fname(f)
    if _p(step, ctx, "name"):
        with contextlib.suppress(Exception):
            f.Name = _p(step, ctx, "name")
        name = str(_p(step, ctx, "name"))
    ctx.setdefault("feature_names", []).append(name)
    return {"feature": name, "body_before": b0, "body_after": b1, "notes": notes}


def _select_surface_body(sw, name: str, mark: int = 1, append: bool = False) -> bool:
    """按名字选中一个**曲面体**（`thicken` / `surface_knit` 的必需前置）。

    ★★★ 2026-10-09 真机确诊 —— 这个 op 卡了很久的根因就在这一行。
      官方帮助（IFeatureManager.FeatureBossThicken）原文：
          "**The surface must be selected with a mark of 1.**"
      而之前一直传 mark=0 / mark=4 ⇒ **无异常、返回 None、特征树不涨**，
      试了 23 个变体（3 种 API × 各种选择方式）全静默失败。
      **`InsertSewRefSurface`（缝合）的官方 Remarks 是同一句话** ——
      所以这是**曲面/体类特征的通用规矩，不是加厚特有的**：
          凡是"对这些**体**做特征"的 API，选择一律 mark=1。

    ★ 真机对照实验：`BODYFEATURE` + mark=1 **也成** ⇒ 关键在 **mark=1**，
      类型字符串只是要选到"曲面体"这一层（选面、选草图都不行）。
      ⇒ 主选 `SURFACEBODY`，失败退 `BODYFEATURE`，两个都带 mark=1。

    ★ 第三档兜底用 `IBody2.GetSelectionId`（官方论坛范例就是这么写的）：
      有些体**特征名 ≠ 体名**，按特征名选不到，必须拿体自己的选择 id。

    ★★★ `append` 参数（2026-10-09 第二个真机坑，**必须从第二个对象起传 True**）：
      `SelectByID2` 的 `append=False` 是**清掉已有选择再选**。多目标时如果每个都传 False，
      **只有最后一个留在选择集里** —— 症状是「特征建出来了、体数死活不合并」，
      而且**不报任何错**。缝合两个共边平面时实测：append 全 False ⇒ 曲面体 2→2；
      第二个改 True ⇒ 2→1。多目标请用 `_select_surface_bodies()`。
    """
    if sw.ext.SelectByID2(name, "SURFACEBODY", 0, 0, 0, append, mark, None, 0):
        return True
    if sw.ext.SelectByID2(name, "BODYFEATURE", 0, 0, 0, append, mark, None, 0):
        return True
    for b in sw.bodies(body_type=int(sw.C("swAllBodies"))):
        bb = typed(b, "IBody2")
        bnm = ""
        with contextlib.suppress(Exception):
            bnm = str(member(bb, "Name") or "")
        if bnm != name:
            continue
        sid = ""
        with contextlib.suppress(Exception):
            sid = str(member(bb, "GetSelectionId") or "")
        if sid and sw.ext.SelectByID2(sid, "SURFACEBODY", 0, 0, 0, append, mark, None, 0):
            return True
    return False


def _select_surface_bodies(sw, names, mark: int = 1) -> list:
    """依次选中**多个**曲面体。返回选不中的名字列表（空 = 全成功）。

    ★ 起头那个 `append=False`（顺便清掉旧选择），**其余一律 `append=True`** ——
      这是多目标选择的唯一正确写法，理由见 `_select_surface_body` 的 append 段。
    """
    miss = []
    for i, nm in enumerate(names):
        if not _select_surface_body(sw, nm, mark, append=(i > 0)):
            miss.append(nm)
    return miss


@op("split_line", "分割线（投影）", "feature",
    "把一张草图**投影到面/曲面上**，在面上划出边界线（**不切断，只划痕**）——\n"
    "车头的风挡 / 车窗边界、箱体分区线就是它；之后配 `trim_surface` 剪掉多余部分。\n"
    "★ 走 `IModelDoc2.InsertSplitLineProject(IsDirectional, FlipDir)`（2 参，返回 **void**）。\n"
    "★★★ **预选顺序与 mark 是硬要求**（2026-10-11 真机对照确诊，**唯一**成功组合）：\n"
    "     ① **草图 mark=4**（先选、清场）→ ② **目标面 mark=1**（后选、append）。\n"
    "  ★ 试过的死法（全都 `SelectByID2` 返回 True、选择集看着是满的，但 API\n"
    "    **不建特征、不报错** = 静默失败）：草图m1+面m4 / 草图m1+面m1 / 草图m4+面m1 但面先选 /\n"
    "    只选草图 / 全 mark=0。与 `thicken`『体必须 mark=1』同族 —— 0/2 参 void 型特征的\n"
    "    mark 语义**只能真机对照试出来**，别照搬别的特征。\n"
    "★ `directional=False`（默认）= 沿**草图平面法线**投影；\n"
    "  `directional=True` = 沿**指定方向**投影，此时还要再多选一个方向参考（基准面/线性边）。\n"
    "★★★ **草图平面不能与目标面平行**（2026-10-11 真机确诊，第二条静默失败）：\n"
    "  草图 ∥ 目标面 ⇒ 投影方向与目标面**平行、无交点** ⇒ 照旧**返回 void、特征树不涨、不报错**。\n"
    "  实测：目标面在 z=20 时，草图放 **Top**（法线 Z、∥）失败、放 **Front**（法线 Y、⊥）成功，复现 2/2。\n"
    "  ⇒ 让草图平面**斜交或垂直**于目标面（投影方向要能「扫」过目标面）。\n"
    "★ 判据（返回值是 void，看不了 —— 铁律①）：**特征树 +1** 且 **面数增加**\n"
    "  （一条投影线把一张面**劈成两张**）。本 op 回 `n_faces_before/after` 与 `faces_hit`。\n"
    "  ★★ 面积**基本不变**（只是划痕，不是切除）—— 别拿面积当判据。\n"
    "★ `faces` 规格同 `fillet`/`shell`：坐标 `[[x,y,z],…]` 最稳（**曲面必须用坐标**）；\n"
    "  也支持「+Z」这类方向串与「all」（后两者对**曲面**零件无效 —— 面枚举默认只实体）。",
    api=["IModelDoc2.InsertSplitLineProject"],
    params={"sketch": {"type": "str", "required": False,
                       "desc": "要投影的草图名；不给则用刚画完的草图 (`last_sketch`)"},
            "faces": {"type": "list", "required": True,
                      "desc": "投影到的面；坐标 [[x,y,z],…] 最稳（**曲面必须用坐标**）"},
            "directional": {"type": "bool", "required": False, "default": False,
                            "desc": "True=沿指定方向投影（需另选方向参考）；False=沿草图法线"},
            "flip": {"type": "bool", "required": False, "default": False},
            "tol": {"type": "float", "required": False, "default": 8,
                    "desc": "面匹配容差 (mm)"},
            "name": {"type": "str", "required": False}},
    requires=("has_sketch",), effect=("has_feature",), verified=True)
def op_split_line(sw, step, ctx):
    sk = _p(step, ctx, "sketch") or ctx.get("last_sketch")
    if not sk:
        raise SwError("没给 sketch，也没有刚画的草图 —— 分割线是「把哪张草图投影出去」"
                      "（草图要用 sketch_begin/sketch_end 画好）")
    spec = _p(step, ctx, "faces")
    if not spec:
        raise SwError("必须给 faces（要投影到的面）—— 不给人不知道投到哪张面上")
    f0 = len(_faces(sw, include_surfaces=True))
    tree0 = {n for _t, n in sw.feature_names()}
    notes: list[str] = []
    tol = float(_p(step, ctx, "tol", 8) or 8)

    # ★★ §146A 同类（2026-10-12 真机复跑逼出）：`InsertSplitLineProject` **返回 void**，
    #   失败时**不抛异常、特征树也不涨** —— 只能按**特征树 diff** 判。既然 COM 会
    #   间歇性静默失败（同一计划时绿时红），就按 diff 重试；重试要**连选择一起重做**
    #   （失败后选择集可能已被消耗/清空）。
    f = None
    err = None
    hit, miss = 0, None
    attempts = 0
    for attempts in range(1, COM_RETRY + 1):
        # ★★ 预选 ①：草图 mark=4（先选、清场）
        sw.clear_selection()
        if not sw.select(str(sk), "SKETCH", mark=4):
            if attempts < COM_RETRY:
                time.sleep(COM_RETRY_S)
                continue
            raise SwError(f"选不中要投影的草图 '{sk}'（mark=4）。\n"
                          "  ○ 草图名对不对（`sketch_end` 时会命名）；是不是还停在编辑状态。")
        # ★★ 预选 ②：目标面 mark=1（后选、append）
        hit, miss = _select_faces(sw, spec, tol, mark=1, surfaces=True)
        if hit <= 0:
            sw.clear_selection()
            if attempts < COM_RETRY:
                time.sleep(COM_RETRY_S)
                continue
            raise SwError(f"选不中要投影到的面：{spec}（mark=1）。\n"
                          "  ○ **曲面件必须用坐标**给面（`_faces` 默认只枚举实体，"
                          "方向串 / all 都会落空）；坐标要落在面上。")
        if miss:
            notes.append(f"没匹配上的面：{miss}")
        _r, err = _call(member, sw.model, "InsertSplitLineProject",
                        bool(_p(step, ctx, "directional", False)),
                        bool(_p(step, ctx, "flip", False)))
        sw.clear_selection()
        f = _new_feature(sw, tree0)                    # ← 判据：特征树 diff
        if f is not None:
            break
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)
    f1 = len(_faces(sw, include_surfaces=True))
    if f is None:
        raise SwError("分割线没生成特征（返回值是 void，判据看特征树涨没涨）。\n"
                      f"  已按特征树 diff 重试 {attempts} 次仍无。\n"
                      "  排查：① 草图 mark=4 + 面 mark=1 的**顺序 / 标记**对不对（见本 op 文档）；\n"
                      "        ② `directional=False` 时沿**草图法线**投影 —— 草图平面要能「扫到」目标面；\n"
                      "        ③ 草图轮廓是不是真的落在目标面的投影范围内"
                      + (f"（COM 报错：{err}）" if err else ""))
    if f1 <= f0:
        notes.append("★★ 特征树涨了但**面数没变** —— 分割线可能没真的划在面上（铁律①）")
    name = _fname(f)
    if _p(step, ctx, "name"):
        with contextlib.suppress(Exception):
            f.Name = _p(step, ctx, "name")
        name = str(_p(step, ctx, "name"))
    ctx.setdefault("feature_names", []).append(name)
    ctx["last_split_line"] = name
    return {"feature": name, "n_faces_before": f0, "n_faces_after": f1,
            "faces_hit": hit, "attempts": attempts, "notes": notes or None}


@op("thicken", "曲面加厚", "feature",
    "把曲面加厚成实体（曲面建模最常用的收口方式，比缝合更常用）。\n"
    "★★ 判据**只能看体积与实体数**：加厚前 `n_solid=0`、体积 0 / None；\n"
    "  加厚后 `n_solid>=1` 且体积 > 0。**不看返回值**（它回一个特征对象，不代表成功）。\n"
    "★★★ **必读：加厚必须用 `mark=1` 选曲面体**（2026-10-09 真机确诊）。\n"
    "  官方原文 `The surface must be selected with a mark of 1.` —— 传 mark=0/4 时\n"
    "  **不报错、返回 None、特征树不涨**，是最难查的静默失败（本 op 为此卡了 23 个变体）。\n"
    "  选择类型走 `SURFACEBODY`（特征名通常与体名同）；本 op 已内置，调用方不用管。\n"
    "★★★ 加厚是曲面链路里**最脆的一步**：曲面局部**曲率半径 < 厚度** ⇒ 自交失败\n"
    "  （报错很含糊，或干脆静默）。⇒ 厚度先取小（2~3mm）；失败就换 `direction` 或减薄。\n"
    "★ 走 `IFeatureManager.FeatureBossThicken(Thickness, Direction, FaceIndex, FillVolume,\n"
    "  Merge, UseFeatScope, UseAutoSelect)`（7 参，2026-10-09 抽自 tlb）。\n"
    "  `Direction` 用 `swThickenDirection_e`：`side1`(0) / `side2`(1) / `both`(2)。\n"
    "  `FaceIndex` **官方注明 Not used**，固定传 0。\n"
    "★ 真机实测（60×40 方管曲面、厚 3mm、面面积 8000mm²）——**体积与手算逐位吻合**：\n"
    "     side1 → 25440mm³（外偏：66×46−60×40 = 636，×40）\n"
    "     side2 → 22560mm³（内偏：60×40−54×34 = 564，×40）\n"
    "     both  → 48000mm³（= 25440+22560）\n"
    "  这组数是本 op 的**回归基准**，改任何东西后要能重现。\n"
    "★ 另一条可用路线（低层、不产生「加厚」特征而是「输入」特征，一般不用）：\n"
    "     `IBody2.Copy()` → `IModeler.ThickenSheet(body2, t, dir)` 得**临时体** →\n"
    "     `IPartDoc.CreateFeatureFromBody3(body, False, swCreateFeatureBodyCheck)` →\n"
    "     `IModelDoc2.EditRebuild3()`。\n"
    "  ★★ `ThickenSheet` 返回的是**临时体、不进特征树** ⇒ 用「特征树涨没涨 / 实体数涨没涨」\n"
    "    当判据**根本看不见它**（会把成功判成失败）。要验它必须直接看返回的 `IBody2.GetType()==0`。",
    api=["IFeatureManager.FeatureBossThicken"],
    params={"targets": {"type": "list", "required": False,
                        "desc": "要加厚的曲面特征名；不给则用上一步的曲面 (`last_surface`)"},
            "thickness": {"type": "float", "unit": "mm"},
            "direction": {"type": "str", "required": False, "default": "side1",
                          "desc": "side1 / side2 / both"},
            "fill_volume": {"type": "bool", "required": False, "default": False},
            "merge": {"type": "bool", "required": False, "default": True},
            "name": {"type": "str", "required": False}},
    # ★★ `effect=("has_feature",)`（2026-10-11 补）：加厚**一定**把曲面变成**实体**
    #   （加厚是"生成凸台"，不是"生成曲面"）⇒ 之后就该能走「实体侧」的 op 了。
    #   早先漏了这条，于是 `曲面 → thicken → extrude_cut`（S5 换路剪裁，
    #   见 assets/probes/shell_window_cut.json）在**静态校验**就被拦下：
    #   `extrude_cut` 的 requires 含 `has_feature`，而状态机不知道加厚已经产生了实体。
    #   ★ 与 `revolve_boss` 漏声明（§见该 op 注释）是**同一类**状态机漏配。
    requires=("has_surface",), effect=("has_feature",), verified=True)
def op_thicken(sw, step, ctx):
    tg = _p(step, ctx, "targets") or []
    if not tg:
        last = ctx.get("last_surface")
        if last:
            tg = [last]
        else:
            raise SwError("`targets` 没给，也没有上一步的曲面（`last_surface`）—— "
                          "加厚是「把哪张面加厚」，没有对象就没有这步")
    dkey = {"side1": "swThickenDirection_Side1", "side2": "swThickenDirection_Side2",
            "both": "swThickenDirection_Both"}
    dname = dkey.get(str(_p(step, ctx, "direction", "side1") or "side1").lower())
    if dname is None:
        raise SwError(f"`direction` 只能是 {sorted(dkey)}，实得 "
                      f"{_p(step, ctx, 'direction')!r}")
    thk = mm(float(_p(step, ctx, "thickness")))
    if thk <= 0:
        raise SwError(f"`thickness` 必须 > 0（实得 {_p(step, ctx, 'thickness')!r}）")
    tree0 = {n for _t, n in sw.feature_names()}
    b0 = _body_stat(sw)
    v0 = _volume(sw)
    # ★ 最后一次尝试成功的组合会被记进 notes（免得"时绿时红"时说不清是哪个组合成的）
    f, err, used = None, None, None
    for tag, merge, feat_scope, auto_sel in (
            ("merge/Merge,FeatScope=False,AutoSelect=False", bool(_p(step, ctx, "merge", True)), False, False),
            ("Merge,FeatScope=False,AutoSelect=True", bool(_p(step, ctx, "merge", True)), False, True),
            ("Merge,FeatScope=True,AutoSelect=True", bool(_p(step, ctx, "merge", True)), True, True)):
        sw.clear_selection()
        miss = _select_surface_bodies(sw, tg, 1)
        if miss:
            raise SwError(f"这些要加厚的对象选不中：{miss}\n"
                          f"  ○ 曲面的选择方式必须是**按体选 + mark=1**（`SURFACEBODY`）。\n"
                          f"  ○ 排查：名字是不是**曲面特征名**（不是文件名 / 草图名）；\n"
                          f"    这个零件里到底有没有曲面体（`_body_stat` 的 `n_sheet`）。")
        f, err = _call(member, _fm(sw), "FeatureBossThicken",
                       thk, int(sw.C(dname)), 0,
                       bool(_p(step, ctx, "fill_volume", False)),
                       merge, feat_scope, auto_sel)
        if f is None:
            f = _new_feature(sw, tree0)          # 返回值可能为空，但特征树涨了
        if f is not None:
            used = tag
            break
    sw.clear_selection()
    if f is None:
        raise SwError("加厚没生成特征，三个参数组合都没成。\n"
                      "  最常见原因：**曲面局部曲率半径小于厚度** ⇒ 加厚自交。\n"
                      "  对策：① 减小厚度  ② 换 `direction`  ③ 把那块大曲率的区域先剪掉\n"
                      "  ○ 若厚度已经很小还失败，回查选择集：必须 mark=1（见 op 文档）"
                      + (f"（COM 报错：{err}）" if err else ""))
    if isinstance(f, tuple):
        raise SwError(f"加厚特征重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    b1 = _body_stat(sw)
    v1 = _volume(sw)
    notes = [f"厚度={float(_p(step, ctx, 'thickness'))}mm，"
             f"方向={_p(step, ctx, 'direction', 'side1')}",
             f"选择=面体+mark1，成功组合={used}"]
    # ★★ 变厚类 op 的**硬判据**：实体数必须涨，且体积必须真的出现
    if b1["n_solid"] <= b0["n_solid"]:
        notes.append("★★★ 实体数**没有增加** —— 加厚没成（铁律②：变厚必须量，别信返回值）")
    if (v1 is None or v1 <= 0) and b1["n_solid"] > b0["n_solid"]:
        notes.append("★ 出现了实体但体积读不出来 —— 用 check_mass 再核一次")
    name = _fname(f)
    if _p(step, ctx, "name"):
        with contextlib.suppress(Exception):
            f.Name = _p(step, ctx, "name")
        name = str(_p(step, ctx, "name"))
    ctx.setdefault("feature_names", []).append(name)
    return {"feature": name, "body_before": b0, "body_after": b1,
            "volume_before": v0, "volume_after": v1, "notes": notes}


@op("circular_pattern", "圆周阵列（特征级）", "feature",
    "把已有特征绕轴阵列（法兰/端盖的螺栓孔圈就用它）。\n"
    "★ **走的 API 是 `IModelDoc2.FeatureCirPattern`** —— 也就是**录宏录出来的那条**。\n"
    "  为什么不用看起来更「现代」的 `IFeatureManager.FeatureCircularPattern5`：\n"
    "  它在 CHM 里被明确标成 **Obsolete**，真机上 18 种 mark 组合**全部静默无操作**\n"
    "  （不抛异常、不建特征、体积不变）；官方指的那条 `CreateFeature` + feature data\n"
    "  路线在晚期绑定下 `AccessSelections` 直接抛 `com_error(-2147417851)`。\n"
    "★ **Mark 约定**：方向参考（轴）Mark=1、种子特征 Mark=4 ——\n"
    "  但更要紧的是**选轴时 Append 必须为真**，否则会把刚选好的种子特征冲掉，\n"
    "  表现就是「都选上了、不报错、什么都不建」。\n"
    "★ `angle` 是**总角度**（默认 360 = 整圈等分）；内部换算成**相邻实例夹角**喂进去\n"
    "  （`FeatureCirPattern` 的 Spacing 是「间隔」不是「总角」，这点跟新版签名相反）。\n"
    "★ 轴用 `axis_from_planes` 显式建一根（临时轴选不中）。\n"
    "★ 整圈齿轮轮廓已经在**草图层**做完了，那里不需要再阵列 —— 用错地方会重复实体。",
    api=["IModelDoc2.FeatureCirPattern", "IModelDocExtension.SelectByID2"],
    params={"target": {"type": "str", "desc": "要阵列的特征名"},
            "axis": {"type": "str", "desc": "基准轴名（axis_from_planes 建的）"},
            "count": {"type": "int", "desc": "含原始的实例总数"},
            "angle": {"type": "float", "unit": "deg", "required": False, "default": 360,
                      "desc": "总角度；等间距时按 count 均分"},
            "equal_spacing": {"type": "bool", "required": False, "default": True},
            "geometry_pattern": {"type": "bool", "required": False, "default": True,
                                 "desc": "保留参数（当前 API 无此开关）"},
            "flip": {"type": "bool", "required": False, "default": False},
            "name": {"type": "str", "required": False}},
    requires=("has_feature",), verified=True)
def op_circular_pattern(sw, step, ctx):
    tgt, axis = _p(step, ctx, "target"), _p(step, ctx, "axis")
    cnt = int(_p(step, ctx, "count"))
    if cnt < 2:
        raise SwError("阵列个数至少 2")
    sw.clear_selection()
    if not _select_feature(sw, tgt, 4):
        raise SwError(f"选不中要阵列的特征 '{tgt}'（Mark=4）")
    if not _select_ref_axis(sw, axis, mark=1):
        raise SwError(
            f"选不中阵列轴 '{axis}'（Mark=1）。\n"
            "  先跑 axis_from_planes。注意：InsertAxis2 建的轴在**中文界面下叫「基准轴1」**，\n"
            "  `axis_from_planes` 会尽力把它改名成你要的名字并把真名放在返回的 axis 里。")
    eq = bool(_p(step, ctx, "equal_spacing", True))
    total = float(_p(step, ctx, "angle", 360) or 360)
    # ★ `IModelDoc2.FeatureCirPattern` 的 Spacing 是**相邻两个实例之间的角度**，
    #   不是总角度（这里没有 EqualSpacing 开关）。所以「整圈 n 等分」= 总角 / n。
    spacing = deg(total / cnt) if eq else deg(total)
    tree0 = {n for _t, n in sw.feature_names()}
    # ★★ 走 `IModelDoc2.FeatureCirPattern`（4 参、返回 **void**）—— **这是录宏录出来的那条**。
    #   `IFeatureManager.FeatureCircularPattern5` 在 CHM 里被标成 Obsolete，
    #   实测 18 种 mark 组合全部静默无操作；`CreateFeature`+feature data 路线在
    #   晚期绑定下 `AccessSelections` 抛 com_error(-2147417851)。
    #   宏录制路径最贴近 UI 的真实调用链，是本机唯一能出特征的一条。
    _res, err = _call(member, sw.model, "FeatureCirPattern",
                      cnt, spacing, bool(_p(step, ctx, "flip", False)), "")
    sw.clear_selection()
    f = _new_feature(sw, tree0, skip=(tgt,))
    if f is None:
        raise SwError("圆周阵列没生成特征（轴/种子没同时选中？）"
                      + (f"（COM 报错：{err}）" if err else ""))
    if isinstance(f, tuple):
        raise SwError(f"圆周阵列重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    name = _p(step, ctx, "name")
    if name:
        with contextlib.suppress(Exception):
            f.Name = name
    return {"feature": name or _fname(f), "count": cnt,
            "spacing_rad": spacing, "api": "IModelDoc2.FeatureCirPattern"}


@op("linear_pattern", "线性阵列（特征级）", "feature",
    "沿一条边/轴阵列特征。方向参考 Mark=1、种子特征 Mark=4。\n"
    "★★ **方向优先用基准轴，别用实体边**（2026-09-25 真机对比结论）：\n"
    "    `direction: \"Axis1\"`        ← 推荐。先 `axis_from_planes` 建一根\n"
    "                                 （Front ∩ Top = X 向，Front ∩ Right = Y 向，\n"
    "                                  Top ∩ Right = Z 向）\n"
    "    `direction_point: [x,y,z]`   ← 备选。「中点离它最近」的那条边（mm，全局坐标）。\n"
    "       实测用**实体边**当方向时，特征能建出来但**重建报错 51**（草图外部参考失效）；\n"
    "       用基准轴则干净通过。所以能建轴就建轴。\n"
    "★ 走的 API 是 `IModelDoc2.FeatureLinearPattern`（8 参、**返回 void**）—— 录宏那条；\n"
    "  `FeatureLinearPattern5` 在本机静默无操作（同 `FeatureCirPattern` 那边的原因）。\n"
    "★ 种子特征名必须对（给 extrude_cut/extrude_boss 的 `name`）—— 用 check_diagnostics 看。",
    api=["IModelDoc2.FeatureLinearPattern", "IModelDocExtension.SelectByID2", "IEntity.Select4"],
    params={"target": {"type": "str", "desc": "要阵列的种子特征名"},
            "direction": {"type": "str", "required": False, "desc": "基准轴名（如 Axis1）"},
            "direction_point": {"type": "list", "required": False,
                                "desc": "方向边的中点的全局坐标 [x,y,z]（mm）"},
            "count": {"type": "int"}, "spacing": {"type": "float", "unit": "mm"},
            "reverse": {"type": "bool", "required": False, "default": False},
            "direction2": {"type": "str", "required": False},
            "count2": {"type": "int", "required": False, "default": 1},
            "spacing2": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "name": {"type": "str", "required": False}},
    requires=("has_feature",), verified=True)
def op_linear_pattern(sw, step, ctx):
    tgt = _p(step, ctx, "target")
    notes: list[str] = []
    sw.clear_selection()
    if not _select_feature(sw, tgt, 4):
        raise SwError(f"选不中要阵列的特征 '{tgt}'")
    dpt = _p(step, ctx, "direction_point")
    if dpt:
        # ★ Mark=1 是阵列方向参考的硬约定。`IEntity.Select4` 没有 mark 参数，
        #   所以这条必须走 SelectByID2（`_select_edges(mark=1)` 内部会换路）。
        hit, miss = _select_edges(sw, [{"point": dpt,
                                       "tol": float(_p(step, ctx, "tol", 8) or 8)}],
                                  mark=1)
        if not hit:
            raise SwError(
                f"选不中方向边（坐标 {dpt}，mm）。\n"
                "  排查：① 给的是**边的中点**全局坐标，不是端点\n"
                "        ② 容差太小：加 \"tol\": 15\n"
                f"        ③ 没匹配上：{miss}")
    else:
        nm = _p(step, ctx, "direction")
        if not nm:
            raise SwError("linear_pattern 需要 direction（轴名）或 direction_point（边中点坐标）")
        if not _select_ref_axis(sw, nm, mark=1):
            raise SwError(
                f"选不中方向参考 '{nm}'（Mark=1）。\n"
                "  先跑 axis_from_planes 建一根轴最稳 —— 注意 InsertAxis2 建的轴\n"
                "  在中文界面下叫「基准轴1」，直接写 'Axis1' 是选不中的。")
    # 方向参考现在必须正好 1 个（Mark=1），否则 SW 会报「无效的参考」
    d2 = _p(step, ctx, "direction2")
    if d2:
        sw.ext.SelectByID2(d2, "EDGE", 0, 0, 0, True, 2, None, 0)
    tree0 = {n for _t, n in sw.feature_names()}
    # ★ 走 `IModelDoc2.FeatureLinearPattern`（8 参、返回 **void**）—— 录宏录出来的那条。
    #   原因同 circular_pattern：`FeatureLinearPattern5` 那条路在本机静默无操作。
    #   注意它**没有 GeometryPattern 开关**（老签名），参数到 DName2 为止。
    _res, err = _call(member, sw.model, "FeatureLinearPattern",
                      int(_p(step, ctx, "count")), mm(float(_p(step, ctx, "spacing"))),
                      int(_p(step, ctx, "count2", 1) or 1),
                      mm(float(_p(step, ctx, "spacing2", 0) or 0)),
                      bool(_p(step, ctx, "reverse", False)), False, "", "")
    sw.clear_selection()
    f = _new_feature(sw, tree0, skip=(tgt,))
    if f is None:
        raise SwError("线性阵列没生成特征（方向参考选错？种子特征名不对？）"
                      + (f"（COM 报错：{err}）" if err else ""))
    if isinstance(f, tuple):
        raise SwError(f"线性阵列重建失败 {f[1]}（{FEATURE_ERROR_NAMES.get(f[1], '未知')}）")
    name = _p(step, ctx, "name")
    if name:
        with contextlib.suppress(Exception):
            f.Name = name
    return {"feature": name or _fname(f), "count": int(_p(step, ctx, "count")),
            "api": "IModelDoc2.FeatureLinearPattern", "notes": notes or None}


@op("mirror", "镜像（特征级）", "feature",
    "把已有特征关于一个面镜像。\n"
    "★★ **mark 用反了是这个 op 唯一致命的坑**（2026-09-25 真机扫出来的）：\n"
    "    种子特征 → **Mark=1**（不是文档说的 4！）\n"
    "    镜像面   → Mark=2\n"
    "  用 Mark=4 选特征时，`InsertMirrorFeature` / `InsertMirrorFeature2`\n"
    "  **8 种组合全部静默无操作**：不抛异常、不建特征、不改体积，只在选择集里\n"
    "  看到 `mark=4 type=22` 就以为选对了。改成 Mark=1 立刻出「镜向1」、错误码 0。\n"
    "  本 op 会**依次轮试**：特征 Mark=(1,4) × api(4参/5参) × GeometryPattern(true/false)，\n"
    "  每次轮试都**重新选一遍**（选择集会被 SW 消费掉），重建失败的变体删掉再试下一个。\n"
    "★ 要镜像**整个实体**请用 `mirror_body`（走 IPartDoc 那条路，完全另一套 API）。",
    api=["IModelDocExtension.SelectByID2", "IFeatureManager.InsertMirrorFeature",
         "IFeatureManager.InsertMirrorFeature2"],
    params={"target": {"type": "str", "desc": "要镜像的特征名"},
            "plane": {"type": "str", "default": "Right",
                      "desc": "镜像基准面（默认右视面）"},
            "mirror_body": {"type": "bool", "required": False, "default": False,
                            "desc": "True 则镜像实体而不是特征"},
            "name": {"type": "str", "required": False}},
    requires=("has_feature",), verified=True)
def op_mirror(sw, step, ctx):
    tgt = _p(step, ctx, "target")
    plane = _p(step, ctx, "plane", "Right")
    mb = bool(_p(step, ctx, "mirror_body", False))

    def reselect(fmark: int) -> None:
        sw.clear_selection()
        if not _select_feature(sw, tgt, fmark):
            raise SwError(f"选不中要镜像的特征 '{tgt}'（Mark={fmark}）")
        if not sw.select(plane, "plane", append=True, mark=2):
            raise SwError(f"选不中镜像基准面 '{plane}'（Mark=2）。"
                          " 中文界面下基准面叫「右视基准面」，sw.select 会自动换名。")

    # 顺序不是随便排的：实测能成的是「特征 Mark=1 + 4 参 API + gp=False」，
    # 放第一组，省掉另外 7 次无用轮试。
    cands = []
    for fmk in (1, 4):
        for use2 in (False, True):
            for g in (False, True):
                meth = f"InsertMirrorFeature{2 if use2 else ''}"
                args = (mb, g, True, True) + ((0,) if use2 else ())
                cands.append((fmk,
                              f"{meth}(fmark={fmk}, gp={g})",
                              (lambda m=meth, a=args: member(_fm(sw), m, *a))))
    f, used, notes = None, "", []
    for fmk, label, fn in cands:
        reselect(fmk)
        tree0 = {n for _t, n in sw.feature_names()}
        _res, e = _call(fn)
        sw.clear_selection()
        got = _new_feature(sw, tree0)
        if got is None:
            notes.append(f"{label}：特征树没新增" + (f"（{e}）" if e else ""))
            continue
        if isinstance(got, tuple):
            notes.append(f"{label}：重建失败 {got[1]}"
                         f"（{FEATURE_ERROR_NAMES.get(got[1], '未知')}）")
            _drop_feature(sw, got[2])
            continue
        f, used = got, label
        break
    if f is None:
        # ★★ 2026-10-03 真机（坑表 §100①）：最常见的根因是**镜像面不穿过实体**。
        #   实测：板 x∈[-30,30]（横跨 Right 面）⇒ 镜像成功，体积 11214.6→10429.2；
        #        板 x∈[0,40]（整个在 +X 侧，Right 面只切到**边界**）
        #   ⇒ **8 种组合全灭**，而原报错里完全看不出这一条。
        #   ⇒ 这里主动量一次"实体相对镜像面的位置"，把根因写进报错（§84）。
        hint = ""
        with contextlib.suppress(Exception):
            _bb = list(sw.bounding_box() or [])
            if len(_bb) == 6:
                _ax = {"Right": 0, "Top": 1, "Front": 2,
                       "右视": 0, "上视": 1, "前视": 2}.get(str(plane)[:2], None)
                if _ax is not None:
                    _lo, _hi = _bb[_ax] * 1000.0, _bb[_ax + 3] * 1000.0
                    if not (_lo < -1e-3 < _hi or _lo < 1e-3 < _hi):
                        hint = (
                            "\n  ★★ **最可能的根因：镜像面没有穿过实体。**\n"
                            "     实体在 {} 轴上是 [{:+.3f}, {:+.3f}]mm，镜像面在 0"
                            "\n     ⇒ 它只切到实体的**边界**，镜像结果与原件重合，"
                            "SolidWorks 什么都不建。\n"
                            "     ⇒ 「建一半 + 镜像」的前提是**那一半真的只占"
                            "镜像面的一侧**。\n"
                        ).format("XYZ"[_ax], _lo, _hi)
        raise SwError(
            f"镜像特征 '{tgt}' 失败，{len(cands)} 种组合都没成：\n"
            + "".join(f"    · {x}\n" for x in notes[:6])
            + hint
            + "  排查：① **镜像面必须穿过实体**（见上面，这是最常见的死因）\n"
            "        ② **先把特征用 Mark=1 选**（不是 4）\n"
            "        ③ 镜像面选对了吗（plane 参数，Right / 「右视基准面」都认）\n"
            "        ④ 目标特征名对不对 —— 用 check_diagnostics 看特征树\n"
            "        ⑤ 兜底：先 `mirror_body` 把实体镜像过去，再布尔合并")
    name = _p(step, ctx, "name")
    if name:
        with contextlib.suppress(Exception):
            f.Name = name
    return {"feature": name or _fname(f), "api": used, "notes": notes or None}


# ============================================================ 参数化类


@op("set_dimension", "改尺寸（参数化改型）", "param",
    "★ 直接改已有零件的尺寸并重建 —— 比「从零建模」快得多的路。\n"
    "★★ 三种寻址方式，给**一种**就行（不再逼你预先知道尺寸名）：\n"
    "    ① `full_name: \"D1@Sketch1@Part1.SLDPRT\"`  精确名，最稳\n"
    "    ② `sketch: \"DiscSk\"` + `index: 0`          该草图第 N 个尺寸（推荐先用它）\n"
    "    ③ `match: \"D1@\"`                          子串匹配，必须**正好命中 1 个**\n"
    "        （宁可报错也不猜：命中多个/零个都会抛，并把候选列出来）\n"
    "  先用 `list_dimensions` 看有哪些尺寸，再挑一种寻址。\n"
    "★ 值按 **mm** 给（`angle: true` 时按度）。改完自动 **`ForceRebuild3`**\n"
    "   （强制重建 —— §148：`EditRebuild3` 不会让几何跟随新尺寸，必须强重建）。\n"
    "★ 找不到尺寸时会抛错并附带候选清单 —— 不静默跳过。",
    api=["IModelDoc2.Parameter", "IDimension.SetSystemValue3", "IModelDoc2.ForceRebuild3",
         "IFeature.GetFirstDisplayDimension"],
    params={"full_name": {"type": "str", "required": False,
                          "desc": "'D1@Sketch1@Part1.SLDPRT'"},
            "sketch": {"type": "str", "required": False,
                       "desc": "草图名；配合 index 用"},
            "index": {"type": "int", "required": False,
                      "desc": "该草图里的尺寸下标（从 0 起，可为负）"},
            "match": {"type": "str", "required": False,
                      "desc": "子串匹配（对 full_name 和所属特征名都试），必须正好命中 1 个"},
            "value": {"type": "float", "unit": "mm"},
            "angle": {"type": "bool", "required": False, "default": False,
                      "desc": "True 表示这是角度尺寸（按度给）"}},
    requires=("doc_open",), verified=True)
def op_set_dimension(sw, step, ctx):
    full = _p(step, ctx, "full_name")
    v = resolve(_p(step, ctx, "value"), ctx)
    if not full:
        dims = _all_dimensions(sw)
        if not dims:
            raise SwError(
                "这个零件里一个尺寸都没有。\n"
                "  尺寸**不会自动产生** —— AddToDB 模式下 SolidWorks 不推断约束和尺寸。\n"
                "  先画图元并用 `dimension` 标注，再用 list_dimensions 确认。")
        lines = "\n".join(f"    {d['full_name']} = {d.get('value_mm')} mm（{d['feature']}）"
                          for d in dims[:25])
        m = _p(step, ctx, "match")
        sk, idx = _p(step, ctx, "sketch"), _p(step, ctx, "index")
        if m:
            pat = str(m).lower()
            hit = [d for d in dims if pat in str(d["full_name"]).lower()
                   or pat in str(d["feature"]).lower()]
            if len(hit) != 1:
                raise SwError(
                    f"match={m!r} 命中 {len(hit)} 个尺寸，必须正好 1 个。\n"
                    f"  候选：\n{lines}")
            full = hit[0]["full_name"]
        elif sk is not None and idx is not None:
            sub = [d for d in dims if str(d["feature"]) == str(sk)]
            if not sub:
                sub = [d for d in dims if f"@{sk}@" in str(d["full_name"])]
            i = int(idx)
            if not sub:
                raise SwError(f"草图 '{sk}' 上一个尺寸都没有。全部尺寸：\n{lines}")
            if i >= len(sub) or i < -len(sub):
                raise SwError(
                    f"草图 '{sk}' 只有 {len(sub)} 个尺寸，下标 {i} 越界。\n"
                    "  该草图的尺寸：\n"
                    + "\n".join(f"    [{j}] {d['full_name']} = {d.get('value_mm')} mm"
                                for j, d in enumerate(sub)))
            full = sub[i]["full_name"]
        else:
            raise SwError("set_dimension 需要 full_name、或 sketch+index、或 match。\n"
                          f"  当前可用尺寸：\n{lines}")
    dim = sw.model.Parameter(full)
    if dim is None:
        raise SwError(f"找不到尺寸 '{full}'（名字对了但 Parameter() 取不到）。"
                      " 用 list_dimensions 打印实际名字。")
    raw = deg(float(v)) if _p(step, ctx, "angle", False) else mm(float(v))
    _r, err = _call(member, dim, "SetSystemValue3", raw, 2, None)
    # ★★ §148（2026-10-12 真机取证）：改尺寸后必须 **强制重建**。
    #   `EditRebuild3`（= 旧代码的 `sw.rebuild()`）**不会**把新尺寸算进几何 ——
    #   实测：SetSystemValue3 后 SystemValue 读回已是新值（0.06），
    #   但 EditRebuild3 之后包围盒**纹丝不动**（还是 40），
    #   只有 `ForceRebuild3` 才让包围盒变成 60。
    #   （与 whichConfigurations 无关：0/1/2/3 都能写进去，差别只在**重建方式**。）
    #   ⇒ 判据铁律①「返回值会说谎」的又一例：SetSystemValue3 返回 0、SystemValue
    #     读回也对，但**几何没变** —— 只有读包围盒/体积才看得出来。
    ok = sw.rebuild(force=True)
    after = None
    with contextlib.suppress(Exception):
        after = round(float(member(dim, "SystemValue")) * 1000.0, 4)
    out = {"dimension": full, "value": v, "rebuild": bool(ok),
           "rebuild_mode": "force", "value_mm_after": after}
    if err:
        out["set_error"] = err
    return out


def _feature_of_fullname(full: str) -> str:
    """从 `D2@草图1@零件1.Part` 里取出所属特征名（`草图1`）。

    尺寸全名是**三段用 @ 分隔**：`尺寸名@特征名@文档名`。取第 2 段。
    """
    parts = str(full).split("@")
    return parts[1] if len(parts) >= 2 else ""


def _dims_via_annotations(sw) -> list:
    """走**注解链**枚举尺寸 —— 这是主力路径。

    ★ 为什么不用「特征链」（`IFeature.GetFirstDisplayDimension` +
      `IDisplayDimension.GetNext3`）：真机实测（v1.4.3）它**只回第 1 个尺寸**，
      `GetNext2` / `GetNext` 也一律回 None。后果很隐蔽 ——
      一张标了 2 个 Ø 的草图，`list_dimensions` 只报 1 个、
      `check_sketch_constraint` 的 `n_dims` 少报，而整个链被
      `contextlib.suppress(Exception)` 包着，**一声不响**。
      「读数比现实小」比「报错」危险得多：验收闸门会被它悄悄放过去。

    ★ 换成注解链（`IModelDoc2.GetFirstAnnotation2` → `IAnnotation.GetNext3`）后
      枚举完整。而且 `IAnnotation.GetName()` 回的就是尺寸全名，
      再用 `IModelDoc2.Parameter(名字)` 反查一下 —— 取得到的就是**真尺寸**
      （注解里还混着基准面名、注释等非尺寸项，这一步顺手把它们滤掉）。
    """
    ann = None
    for meth in ("GetFirstAnnotation2", "GetFirstAnnotation"):
        with contextlib.suppress(Exception):
            ann = member(sw.model, meth)
        if ann is not None:
            break
    out: list = []
    seen: set[str] = set()
    guard = 0
    while ann is not None and guard < 4000:
        guard += 1
        a = typed(ann, "IAnnotation")
        name = None
        with contextlib.suppress(Exception):
            name = member(a, "GetName")
        if name:
            full = str(name)
            dim = None
            with contextlib.suppress(Exception):
                dim = sw.model.Parameter(full)
            if dim is not None and full not in seen:
                seen.add(full)
                rec = {"full_name": full, "feature": _feature_of_fullname(full)}
                with contextlib.suppress(Exception):
                    rec["value_mm"] = round(float(member(dim, "SystemValue")) * 1000.0, 4)
                out.append(rec)
        nxt = None
        for meth in ("GetNext3", "GetNext2", "GetNext"):
            with contextlib.suppress(Exception):
                nxt = member(a, meth)
            if nxt is not None:
                break
        ann = nxt
    return out


def _dd_one(sw, dd) -> dict | None:
    """把一个 `IDisplayDimension` 读成 `{"full_name","value_mm"}`。读不出名字就算废。"""
    rec: dict = {}
    with contextlib.suppress(Exception):
        d = member(dd, "GetDimension2", 0)
        if d is not None:
            with contextlib.suppress(Exception):
                rec["full_name"] = member(d, "FullName")
            with contextlib.suppress(Exception):
                rec["value_mm"] = round(float(member(d, "SystemValue")) * 1000.0, 4)
    return rec if rec.get("full_name") else None


def _dims_via_features(sw) -> list:
    """走特征链枚举尺寸，**优先用下标版 `IFeature.GetDisplayDimension(i)`**。

    ★ 真机实测（v1.4.3）：`IFeature.GetFirstDisplayDimension` +
      `IDisplayDimension.GetNext3` / `GetNext2` / `GetNext` 这条「链」
      **只回第 1 个尺寸**（next 一律 None）。后果非常隐蔽 ——
      一张标了 2 个 Ø 的草图，`list_dimensions` 只报 1 个、
      `check_sketch_constraint` 的 `n_dims` 少报；因为整条链包在
      `contextlib.suppress(Exception)` 里，**一声不响**。
      「读数比现实小」比「报错」危险得多：验收闸门会被它悄悄放过去。

    ★ 换用**下标版** `GetDisplayDimension(0/1/2/…)`：读不到（None 或抛错）就停。
      `sldworks.tlb` 里这个方法就是 `[1 个参数] -> Object`，是官方给的下标入口。
    """
    out: list = []
    f = typed(member(sw.model, "FirstFeature"), "IFeature")
    guard = 0
    while f is not None and guard < 2000:
        guard += 1
        fname = ""
        with contextlib.suppress(Exception):
            fname = member(f, "Name")
        got_any = False
        misses = 0
        for i in range(200):
            dd = None
            with contextlib.suppress(Exception):
                dd = member(f, "GetDisplayDimension", i)
            if dd is None:
                # ★ 不能「第一次拿到 None 就 break」：`GetDisplayDimension(0)`
                #   在有些特征上会抛错/回空，但 (1)、(2) 是好的 —— 早先一 break
                #   就整条特征链退化成「每个特征只有 1 个尺寸」。
                misses += 1
                if misses >= (3 if got_any else 8):
                    break
                continue
            misses = 0
            rec = _dd_one(sw, dd)
            if rec:
                rec["feature"] = fname
                out.append(rec)
                got_any = True
        if not got_any:
            # 兜底：下标版拿不到（个别文档/版本）→ 退回「首个 + 链」
            dd = None
            with contextlib.suppress(Exception):
                dd = member(f, "GetFirstDisplayDimension")
            while dd is not None:
                rec = _dd_one(sw, dd)
                if rec:
                    rec["feature"] = fname
                    out.append(rec)
                nxt = None
                for meth in ("GetNext3", "GetNext2", "GetNext"):
                    with contextlib.suppress(Exception):
                        nxt = member(dd, meth)
                    if nxt is not None:
                        break
                dd = nxt
        # ★★ 最后一道兜底：**按名字探测**。真机实测（2026-09-25）`GetDisplayDimension(i)`
        #    在所有下标上都回 None、注解链 `GetFirstAnnotation2` 也回 None —— 两条路
        #    全哑火，一张「完全定义」的同心双圆草图只被数出 1 个尺寸（实际 2 个，
        #    `Parameter("D2@RingSk")` 明明回 16.0）。这是最危险的一类 bug：
        #    读数比现实小，验收闸门被悄悄放过去。
        #    而 `IModelDoc2.Parameter("D{n}@{特征名}")` **是可靠的**（回 IDimension）。
        #    所以这里按命名规律 `D1..D99` 逐个探，探到连续 miss 就停。
        # ★★ 兜底：**按名字探测**，与上面两条路**合并**（不是"只在空时才用"）。
        #    真机实测（2026-09-25）`GetDisplayDimension(i)` 所有下标都回 None、
        #    注解链 `GetFirstAnnotation2` 也回 None —— 两条路全哑火，一张「完全定义」
        #    的同心双圆草图只被数出 1 个尺寸（实际 2 个：`Parameter("D2@RingSk")`
        #    明明回 16.0）。这是最危险的一类 bug：**读数比现实小**，
        #    验收闸门被悄悄放过去。而 `IModelDoc2.Parameter("D{n}@{特征名}")` 是可靠的。
        #    探到「连续 3 个缺口」就收手（`Parameter()` 不便宜，别把探针拖超时）。
        have = {str(r.get("full_name")) for r in out}
        miss = 0
        for i in range(1, 100):
            full = f"D{i}@{fname}"
            if full in have:
                miss = 0
                continue
            dim = None
            with contextlib.suppress(Exception):
                dim = sw.model.Parameter(full)
            if dim is None:
                miss += 1
                if miss >= 3:
                    break
                continue
            miss = 0
            rec = {"full_name": full, "feature": fname}
            with contextlib.suppress(Exception):
                rec["value_mm"] = round(float(member(dim, "SystemValue")) * 1000.0, 4)
            out.append(rec)
        f = typed(member(f, "GetNextFeature"), "IFeature")
    return out


def _all_dimensions(sw, include_sketch: bool = True) -> list:
    """零件里所有尺寸 → [{"full_name","value_mm","feature"}，…]。

    `list_dimensions`、`check_sketch_constraint`、`set_dimension`（按草图+下标寻址）
    共用这一份枚举 —— 两处各写一遍的结果必然是「列出来的名字和能改的名字对不上」。

    ★ 修正（2026-09-25）：草图特征的**类型名是 `ProfileFeature`**（不是 "Sketch"），
      早先按 `tname != "Sketch"` 过滤，条件恒为真 ⇒ `include_sketch=False`
      实际上从来没生效过。这里按 ProfileFeature/Sketch 两个都认。
    ★ 修正（v1.4.3）：枚举改成**两条路合并去重** ——
      「逐特征 + 下标版 `IFeature.GetDisplayDimension(i)`」与「注解链
      `IModelDoc2.GetFirstAnnotation2` → `IAnnotation.GetNext3`」。
      只走其中一条都会**静默少报**（见 `_dims_via_features` 的注释）。
      合并按 `full_name` 去重，所以两条路重叠不会把数量算多。
    """
    merged: dict[str, dict] = {}
    for src in (_dims_via_features(sw), _dims_via_annotations(sw)):
        for d in src:
            full = str(d.get("full_name") or "")
            if not full:
                continue
            # ★ 归一化键（2026-10-02）：同一条尺寸在不同枚举路下会给出**两种全名** ——
            #   带文档名的 `D1@轴半剖面@零件99.Part` 与不带文档名的 `D1@轴半剖面`。
            #   早先直接拿全名当键 ⇒ 同一条尺寸被算**两次**，`n_dims` 虚高
            #   （实测轴 n=15 里有重复项），`expect_min_dims` 这道闸门被悄悄放水。
            #   取前两段（尺寸名@特征名）当键；文档名不同的同名尺寸在这里合并。
            parts = full.split("@")
            key = "@".join(parts[:2]) if len(parts) >= 2 else full
            if key in merged:
                cur = merged[key]
                # 保留**带文档名**的那个全名（check_sketch_constraint 会用 `@特征@` 过滤）
                if len(parts) > len(str(cur.get("full_name") or "").split("@")):
                    cur["full_name"] = full
                for k, v in d.items():
                    if cur.get(k) in (None, "") and v not in (None, ""):
                        cur[k] = v
            else:
                d.setdefault("feature", _feature_of_fullname(full))
                merged[key] = d
    out = list(merged.values())
    if include_sketch:
        return out
    # 排除草图尺寸：靠尺寸全名里的特征段判（草图的类型名是 ProfileFeature/Sketch）
    names = set()
    f = typed(member(sw.model, "FirstFeature"), "IFeature")
    guard = 0
    while f is not None and guard < 2000:
        guard += 1
        tname = ""
        with contextlib.suppress(Exception):
            tname = member(f, "GetTypeName2")
        if tname in ("ProfileFeature", "Sketch"):
            nm = ""
            with contextlib.suppress(Exception):
                nm = member(f, "Name")
            if nm:
                names.add(str(nm))
        f = typed(member(f, "GetNextFeature"), "IFeature")
    return [d for d in out if str(d.get("feature")) not in names]


@op("list_dimensions", "列出所有尺寸", "param",
    "把零件里所有尺寸的完整名（'D1@Sketch1@x.SLDPRT'）与当前值列出来。\n"
    "**参数化改型第一步就是跑它**，不要凭想象写尺寸名 —— 返回的 `full_name`\n"
    "可以直接喂给 `set_dimension`。\n"
    "★ 一个零件「一个尺寸都没有」是正常的：AddToDB 模式下 SolidWorks\n"
    "  **不自动加约束和尺寸**，得先自己用 `dimension` 标出来。",
    api=["IFeature.GetFirstDisplayDimension", "IDisplayDimension.GetDimension2",
         "IDimension.FullName / IDimension.SystemValue"],
    params={"include_sketch": {"type": "bool", "required": False, "default": True,
                               "desc": "是否包含草图里的尺寸"}},
    requires=("doc_open",), verified=True)
def op_list_dimensions(sw, step, ctx):
    out = _all_dimensions(sw, bool(_p(step, ctx, "include_sketch", True)))
    return {"dimensions": out, "count": len(out)}


def _active_config_name(sw) -> str:
    """当前**活动配置的真名**。

    ★ 真机实测（2026-09-25，M2）：中文界面下配置叫「默认」，**不叫 "Default"**。
      `IModelDocExtension.CustomPropertyManager("Default")` 在中文版上直接返回
      **None**，于是 `mgr.Add3(...)` 报 `'NoneType' object has no attribute 'Add3'`
      —— 报错指向 Add3，根因却在一层之外的配置名。凡是拿"配置名"当入参的 API
      都要走这个函数，别硬编码 "Default"。
    """
    for path in (("ConfigurationManager", "ActiveConfiguration"),):
        with contextlib.suppress(Exception):
            cm = member(sw.model, path[0])
            ac = member(typed(cm, "IConfigurationManager"), path[1])
            nm = member(typed(ac, "IConfiguration"), "Name")
            if nm:
                return str(nm)
    return "Default"


@op("set_custom_property", "写自定义属性", "param",
    "写零件自定义属性（代号/材料/设计者）。文件属性窗口里能直接看到。\n"
    "★ `config` 留空 = **自动取当前活动配置**（推荐）—— 中文界面下配置名是\n"
    "  「默认」，硬编码 \"Default\" 会让 `CustomPropertyManager` 返回 None，\n"
    "  报错还指不到根因（会报 Add3 不存在）。\n"
    "★ 覆盖语义：同名属性已存在时会被**覆盖**（OverwriteExisting=1）。",
    api=["IModelDocExtension.CustomPropertyManager", "ICustomPropertyManager.Add3",
         "IModelDoc2.ConfigurationManager.ActiveConfiguration"],
    params={"name": {"type": "str"},
            "value": {"type": "str"},
            "config": {"type": "str", "required": False, "default": "",
                       "desc": "配置名；留空则用当前活动配置"}},
    requires=("doc_open",), verified=True)
def op_set_custom_property(sw, step, ctx):
    cfg = _p(step, ctx, "config") or _active_config_name(sw)
    mgr, used, errs = None, "", []
    for c in [cfg] + [x for x in ("Default", "默认") if x != cfg]:
        m, e = _call(member, sw.ext, "CustomPropertyManager", c)
        if m is not None:
            mgr, used = m, c
            break
        errs.append(f"CustomPropertyManager({c!r}): {e}")
    if mgr is None:
        raise SwError("拿不到 CustomPropertyManager：\n"
                      + "\n".join(f"    · {x}" for x in errs))
    nm, val = _p(step, ctx, "name"), _p(step, ctx, "value")
    res, err = _call(member, mgr, "Add3", nm, 30, val, 1)
    out = {"name": nm, "value": val, "config": used, "result": res}
    if err:
        out["add_error"] = err
    # 回读：Add3 返回 0 也不代表写进去了（另一处"返回值说谎"），能读就读一眼。
    # GetN 系列版本差异很大，逐个试，读不到就留 None —— 不因为回读失败把整步带崩。
    got = None
    for meth, args in (("Get5", (nm, False, "", False)),
                       ("Get6", (nm, False)),
                       ("Get4", (nm, False))):
        v, _e = _call(member, mgr, meth, *args)
        if isinstance(v, (tuple, list)) and len(v) >= 2 and v[1]:
            got = v[1]
            break
    out["read_back"] = got
    return out


# ============================================================ 校验类


# ============================================================ 真实螺纹线（M3，需求 5）
#
# 用户 2026-09-25 拍板：螺纹要**真实螺纹线**（SweepThread，真的去料），不要装饰线。
#
# ★★ 这个 op 的写法全部来自真机实测（D:/out/diag_thread.py / diag_thread2.py），
#    因为 CHM 里**没有** IThreadFeatureData 的文档 —— 只能读 sldworks.tlb + 试。
#    四条硬结论（每条都反直觉）：
#
#  ① **`Size` 是彻底的 no-op，别用。**
#     写 "M10" / "M6" 都被静默丢弃：属性本地读回是 "M10"，但特征落库之后
#     `GetDefinition()` 读回来**永远是 "M1.2x0.25"**，而且 Ø10 圆柱配 M10 与配 M6
#     体积减少**完全相同（−23.714 mm³）**。真正驱动几何的是数值型的
#     `Diameter` / `Pitch`（+ `EndCondition` / `Revolutions`）。
#
#  ② **`Type` 是螺纹轮廓库的 .SLDLFP 文件全路径**，不是 "Die"/"Tapped" 这种字符串。
#     实测把 Type 设成 "Die" 读回来是空串。默认值是
#     `…\thread profiles\Metric Die.SLDLFP`（公制外螺纹）。本 op 按**友好名**映射。
#
#  ③ **必须选「圆边」，选圆柱面不行。**
#     实测选最大的圆柱面 → `CreateFeature` 返回 None、什么都不建、体积一字不变
#     （跟装饰螺纹线那次一模一样的脾气）。目标边用**该圆边的圆心坐标**定位。
#
#  ④ `AccessSelections` 在晚期绑定下**必定抛** `com_error(-2147417851)`
#     （跟阵列那次同一个"服务器出现意外情况"）。但它**不致命** ——
#     目标已经在文档里被选中，`CreateFeature` 照样吃得到。
#     所以这里故意吞掉并记录，不当失败判据。
#
#  ★★ 验证口径（吃过倒角的亏）：**读回特征自己的定义**才算数。
#     只看"建出来了"会漏掉参数没生效的情况 —— 这正是本 op 把 `want` 和 `got` 都
#     报出来的原因，不一致直接抛。

THREAD_PROFILES = {
    "metric_die": "Metric Die.SLDLFP",      # 公制外螺纹（螺钉/螺栓杆部）
    "metric_tap": "Metric Tap.SLDLFP",      # 公制内螺纹（螺母/螺孔）
    "inch_die": "Inch Die.SLDLFP",          # 英制外螺纹
    "inch_tap": "Inch Tap.SLDLFP",          # 英制内螺纹
    "bottle": "SP4xx Bottle.SLDLFP",        # 瓶口螺纹
}
THREAD_PROFILE_CN = {
    "公制外螺纹": "metric_die", "公制内螺纹": "metric_tap",
    "英制外螺纹": "inch_die", "英制内螺纹": "inch_tap", "瓶口": "bottle",
}
THREAD_END_CONDITIONS = {"blind": 0, "revolutions": 1, "up_to_selection": 2,
                         "盲孔": 0, "圈数": 1, "到选定面": 2}
THREAD_METHODS = {"cut": 0, "extrude": 1, "切除": 0, "拉伸": 1}
_SW_FM_SWEEP_THREAD = 87          # swFmSweepThread（从 sldworks.tlb 读出）


def _thread_profile_dir(sw) -> str | None:
    """螺纹轮廓库目录（`…\\ProgramData\\SolidWorks\\SOLIDWORKS 2024\\thread profiles`）。

    ★ 为什么不「问 SolidWorks 要默认值」：试过了，**不行**。
      新建的 `IThreadFeatureData` 上 `Type` 是**空串**，只有特征真正落库之后
      `GetDefinition()` 才读得到 `…\\Metric Die.SLDLFP`（实测 diag_thread2：
      `after_set` 里 Type=""，而 `definition_on_feature` 里才有完整路径）。
      所以只能按版本号推。

    ★ 版号 ↔ 年份的对应：`RevisionNumber` 主版本 N ↔ 年份 1992+N
      （SW2024 = 32、SW2023 = 31、SW2025 = 33）。推不出来就在 ProgramData
      底下 glob 一遍 `SOLIDWORKS*/thread profiles`，再不行让调用方直接给全路径。
    """
    env = os.environ.get("YOYO_SW_THREAD_PROFILES")
    if env and Path(env).is_dir():
        return str(Path(env))
    base = Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "SolidWorks"
    if not base.is_dir():
        return None
    rev = None
    with contextlib.suppress(Exception):
        rev = int(str(sw.version).split(".")[0])
    if rev:
        p = base / f"SOLIDWORKS {1992 + rev}" / "thread profiles"
        if p.is_dir():
            return str(p)
    for p in sorted(base.glob("SOLIDWORKS*/thread profiles")):
        if p.is_dir():
            return str(p)
    return None


def _thread_profile_path(sw, spec) -> str:
    """友好名 → .SLDLFP 全路径。也接受直接给全路径。"""
    s = str(spec or "metric_die").strip()
    if s.lower().endswith(".sldlfp"):
        return s
    key = THREAD_PROFILE_CN.get(s, s.lower())
    fname = THREAD_PROFILES.get(key)
    if not fname:
        raise SwError(
            f"未知的螺纹标准 {spec!r}。可用：{', '.join(sorted(THREAD_PROFILES))}"
            f"（或中文：{', '.join(THREAD_PROFILE_CN)}），也可直接给 .SLDLFP 全路径。")
    d = _thread_profile_dir(sw)
    if not d:
        raise SwError(
            "拿不到螺纹轮廓库目录（读默认定义的 Type 失败）。\n"
            "  可直接把 standard 填成 .SLDLFP 的**全路径**，例如\n"
            r"  C:\ProgramData\SolidWorks\SOLIDWORKS 2024\thread profiles\Metric Die.SLDLFP")
    return f"{d}\\{fname}"


def _thread_definition(sw, feat):
    """把已建螺纹特征的真实定义读回来（`GetDefinition` → IThreadFeatureData）。"""
    out: dict = {}
    with contextlib.suppress(Exception):
        f = typed(sw.model.FeatureByName(str(feat)), "IFeature")
        d = member(f, "GetDefinition")
        td = typed(d, "IThreadFeatureData")
        for p in ("Type", "Size", "Diameter", "Pitch", "ThreadMethod", "EndCondition",
                  "Revolutions", "BlindDepth", "RightHanded", "MaintainThreadLength",
                  "NumberOfStarts", "ThreadStartAngle"):
            with contextlib.suppress(Exception):
                v = getattr(td, p)
                if p in ("Type", "Size"):
                    v = str(v).rsplit("\\", 1)[-1]          # 只留文件名，报告别太长
                out[p] = v
    return out


def _cylinder_radii(sw) -> list[dict]:
    """列出实体上所有**圆柱面**的半径（mm）/轴向/面积。

    ★★ 为什么必须靠它、不能靠包围盒（2026-09-25 真机实测，跑了 diag_thread6~11）：
      螺纹的螺旋面在 SolidWorks 里是 **B 样条面**（`ISurface.Identity()==4006`）。
      `IBody2.GetBodyBox` / `IFace2.GetBox` 对 B 样条面报的是**未裁剪曲面**的盒子，
      于是 Ø3 光杆做完 M3×0.5 后：
          包围盒 [3.0, 3.0, 22.0] → [3.0, **3.4641**, 22.0]
      3.4641 = 2√3，恰好是「对边宽 = 轴径」的正六边形对角尺寸，**极像**真多出了一圈
      六棱面；而且只有 Y 向长、X 向纹丝不动。
      追查过程（省得后人再走一遍）：
        ① `GetBodies2` 只有 **1 个实体** ⇒ 不是"多出来一个怪实体"；
        ② 删掉螺纹特征 → 盒子立刻回 [3.0,3.0,22.0]、体积复原 ⇒ 确是螺纹特征引起；
        ③ 逐面扫描：凸出的 3 个面 `Identity` 全是 **4006（B 样条）**，
           而主圆柱面半径**仍是 1.5**、它自己的盒子也仍是 ±1.5；
           全程**没有出现大块平面**（若真长六棱面，会有 6 个平面，实测只有 0.019mm² 的碎面）；
        ④ `IModelDoc2.SaveBMP` 抓视口存图一看 —— **实体就是个干净的螺纹杆**，
           外形是标准圆柱，没有凸棱。
      ⇒ 结论：**包围盒在撒谎，几何是对的**。凡是螺纹/螺旋面，判据一律用本函数。
    """
    out: list[dict] = []
    with contextlib.suppress(Exception):
        for b in sw.bodies():        # ★ 唯一取实体入口（别用 GetBodies2(0, True)）
            body = typed(b, "IBody2")
            f = member(body, "GetFirstFace")
            n = 0
            while f is not None and n < 4000:
                n += 1
                f2 = typed(f, "IFace2")
                srf = None
                with contextlib.suppress(Exception):
                    srf = typed(member(f2, "GetSurface"), "ISurface")
                is_cyl = False
                if srf is not None:
                    with contextlib.suppress(Exception):
                        is_cyl = bool(member(srf, "IsCylinder"))
                if is_cyl:
                    rec: dict = {}
                    with contextlib.suppress(Exception):
                        cp = member(srf, "CylinderParams")
                        rec["r_mm"] = round(float(cp[6]) * 1000.0, 4)
                        rec["axis"] = [round(float(v), 4) for v in cp[3:6]]
                        rec["origin_mm"] = [round(float(v) * 1000.0, 4) for v in cp[0:3]]
                    with contextlib.suppress(Exception):
                        rec["area_mm2"] = round(float(member(f2, "GetArea")) * 1e6, 4)
                    out.append(rec)
                with contextlib.suppress(Exception):
                    f = member(f2, "GetNextFace")
    return out


def _max_cyl_radius_mm(sw) -> float | None:
    """实体上最大的圆柱面半径（mm）。没有圆柱面就回 None。"""
    rs = [d["r_mm"] for d in _cylinder_radii(sw) if "r_mm" in d]
    return max(rs) if rs else None


@op("real_thread", "真实螺纹线（去料）", "feature",
    "★★ 需求 5 的落地：**真实螺纹线**（特征类型 `SweepThread`，真的去料），\n"
    "   不是 `thread` 那个装饰线。用在螺钉/螺栓杆部、螺母/螺孔上。\n"
    "★ 走的是 `CreateDefinition(swFmSweepThread=87)` + `IThreadFeatureData` 路线\n"
    "  （CHM 里没这个接口的文档，签名是从 `sldworks.tlb` 读的）。\n"
    "★★ 四条反直觉的硬规矩（全部真机实测，见 30-pitfalls）：\n"
    "  ① **`size` 参数故意不提供** —— `Size` 是彻底的 no-op，写 \"M10\"/\"M6\" 都被\n"
    "     静默丢弃，Ø10 圆柱配 M10 与配 M6 体积减少**一模一样**。\n"
    "     真正驱动几何的是 `diameter` + `pitch`。\n"
    "  ② **必须选圆边，不能选圆柱面** —— 选面时 `CreateFeature` 回 None、什么都不建。\n"
    "     目标用**该圆边的圆心坐标**给（见 `edges`）。\n"
    "  ③ `AccessSelections` 在晚期绑定下必抛 `com_error(-2147417851)`，\n"
    "     但**不致命**（选择已在文档里，CreateFeature 吃得到）—— 本 op 吞掉并记录。\n"
    "  ④ 建完会**读回特征自己的定义**，与请求值逐项比对；不一致直接抛。\n"
    "     只看「特征建出来了」会漏掉参数没生效（倒角那次就是这么躺了一轮的）。\n"
    "  ⑤ **判「牙顶齐不齐轴径」绝不能看包围盒** —— 螺旋面是 B 样条面，\n"
    "     `IBody2.GetBodyBox` 会按**未裁剪曲面**虚报（Ø3 做完 M3×0.5 后 Y 向从\n"
    "     3.0 虚涨到 3.4641=2√3，而视口截图显示实体就是干净圆柱）。\n"
    "     本 op 改用**圆柱面半径不变式**：建螺纹前后取「最大圆柱面半径」，\n"
    "     涨了超过 0.02mm 就抛。详见 `_cylinder_radii` 的注释与 30-pitfalls。",
    api=["IFeatureManager.CreateDefinition", "IThreadFeatureData.InitializeThreadData",
         "IThreadFeatureData.Type", "IThreadFeatureData.Diameter",
         "IThreadFeatureData.Pitch", "IThreadFeatureData.EndCondition",
         "IThreadFeatureData.AccessSelections", "IFeatureManager.CreateFeature",
         "IFeature.GetDefinition", "IFeatureManager.InsertCosmeticThread3",
         "ISurface.IsCylinder", "ISurface.CylinderParams"],
    params={"edges": {"type": "list", "required": True,
                      "desc": "目标圆边，给**圆心坐标** [[x,y,z]] 或 [{'point':[…],'tol':…}]"},
            "standard": {"type": "str", "required": False, "default": "metric_die",
                         "desc": "metric_die(公制外)/metric_tap(公制内)/inch_die/inch_tap/bottle，或 .SLDLFP 全路径"},
            "diameter": {"type": "float", "unit": "mm", "required": True,
                         "desc": "螺纹公称直径（M3 → 3）"},
            "pitch": {"type": "float", "unit": "mm", "required": True,
                      "desc": "螺距（M3 粗牙 → 0.5）"},
            "end_condition": {"type": "str", "required": False, "default": "blind",
                              "desc": "blind(盲孔,配 depth) / revolutions(配 revolutions) / up_to_selection"},
            "depth": {"type": "float", "unit": "mm", "required": False,
                      "desc": "end_condition=blind 时的螺纹长度"},
            "revolutions": {"type": "float", "required": False,
                            "desc": "end_condition=revolutions 时的圈数"},
            "right_handed": {"type": "bool", "required": False, "default": True},
            "method": {"type": "str", "required": False, "default": "cut",
                       "desc": "cut(去料,默认) / extrude(增料)"},
            "start_angle": {"type": "float", "unit": "deg", "required": False, "default": 0},
            "outward": {"type": "bool", "required": False, "default": False,
                        "desc": "Outward=向外(涡状)，会让螺旋线**半径大于**所画的圆 ⇒ 扫掠件跟着偏"},
            "tapered": {"type": "bool", "required": False, "default": False},
            "name": {"type": "str", "required": False},
            "tol": {"type": "float", "unit": "mm", "required": False, "default": 8.0,
                    "desc": "圆边坐标容差"}},
    requires=("has_feature",), verified=True)
def op_real_thread(sw, step, ctx):
    spec = _p(step, ctx, "edges")
    dia = _p(step, ctx, "diameter")
    pitch = _p(step, ctx, "pitch")
    if spec is None:
        raise SwError("real_thread 需要 edges（目标圆边的**圆心坐标**，如 [[0,0,30]]）。\n"
                      "  不要给圆柱面上的点 —— 选面是建不出来的（实测 CreateFeature 回 None）。")
    if dia is None or pitch is None:
        raise SwError("real_thread 需要 diameter（公称直径）和 pitch（螺距）。\n"
                      "  粗牙查国标：M3→0.5、M4→0.7、M5→0.8、M6→1.0、M8→1.25、M10→1.5。")
    prof = _thread_profile_path(sw, _p(step, ctx, "standard", "metric_die"))

    ec = _p(step, ctx, "end_condition", "blind")
    ec_code = THREAD_END_CONDITIONS.get(str(ec).lower(), None)
    if ec_code is None:
        if str(ec).isdigit():
            ec_code = int(ec)
        else:
            raise SwError(f"未知的 end_condition {ec!r}。可用："
                          f"{', '.join(sorted(set(THREAD_END_CONDITIONS)))}")
    depth = _p(step, ctx, "depth")
    revs = _p(step, ctx, "revolutions")
    if ec_code == 0 and depth is None:
        raise SwError("end_condition=blind 必须给 depth（螺纹长度 mm）。")
    if ec_code == 1 and revs is None:
        raise SwError("end_condition=revolutions 必须给 revolutions（圈数）。")
    meth = _p(step, ctx, "method", "cut")
    meth_code = THREAD_METHODS.get(str(meth).lower(), 0)

    v0 = _volume(sw)
    r_before = _max_cyl_radius_mm(sw)      # ★ 牙顶不变式的基准（见 _cylinder_radii）
    tree0 = {n for _t, n in sw.feature_names()}
    fm = _fm(sw)
    # ★★★ 这个字典的**顺序不能改**（真机实测 diag_thread5，两轮对照）：
    #   `Diameter` / `Pitch` **只有在对应的 `*Override` 标志已为 True 时才写得进去**。
    #   先写值、后写标志 → 值被**静默忽略**（读回来还是默认 Diameter=0.0 / Pitch=0.01m=10mm），
    #   而 10mm 螺距套在 Ø3 的轴上是不合法的 ⇒ `CreateFeature` **什么都不建、还不报错**。
    #   O1（先标志后值）→ 建出 SweepThread，ΔV=−6.921 mm³；
    #   O2（先值后标志）→ 树里没特征，ΔV=0.000。
    #   这就是「参数静默失效」的第三次现身（前两次：`Size` 是 no-op、AddToDB 不影响约束）。
    want = {
        "DiameterOverride": True,                       # ← 必须在 Diameter 之前
        "PitchOverride": True,                          # ← 必须在 Pitch 之前
        "Diameter": mm(float(dia)),
        "Pitch": mm(float(pitch)),
        "Type": prof,
        "ThreadMethod": int(meth_code),
        "EndCondition": int(ec_code),
        "RightHanded": bool(_p(step, ctx, "right_handed", True)),
        "ThreadStartAngle": deg(float(_p(step, ctx, "start_angle", 0) or 0)),
    }
    if ec_code == 0:
        want["BlindDepth"] = mm(float(depth))
    elif ec_code == 1:
        want["Revolutions"] = float(revs)

    # ★★ §146A / §148 同源：`CreateFeature` 这类"建特征"调用会**间歇性静默失败** ——
    #   同一会话同一零件，实测时好时坏（返回 None、不抛异常、**特征树不涨、体积一字不变**）。
    #   判据铁律①：只看**特征树 diff**（`_new_feature`）；没新增特征就**重选、重调**，
    #   最多 `COM_RETRY` 次；次数如实写进结果 `attempts`。
    #   ★ 为什么重试也要重选：`CreateFeature` 之后选择集可能已被消耗/清空，
    #     不重选直接重调必然再失败。
    f = None
    c_err = None
    hit, miss = 0, None
    bad: dict = {}
    acc_err = None
    attempts = 0
    for attempts in range(1, COM_RETRY + 1):
        sw.clear_selection()
        hit, miss = _select_edges(sw, spec, tol_mm=float(_p(step, ctx, "tol", 8.0) or 8.0))
        if hit == 0:                                       # 选不中就重试，不勉强 —— 勉强的结果是
            if attempts < COM_RETRY:                       # CreateFeature 回 None、什么都不建。
                time.sleep(COM_RETRY_S)
            continue
        d, derr = _call(member, fm, "CreateDefinition", _SW_FM_SWEEP_THREAD)
        if d is None:
            c_err = f"CreateDefinition(swFmSweepThread={_SW_FM_SWEEP_THREAD}) 失败：{derr}"
            if attempts < COM_RETRY:
                time.sleep(COM_RETRY_S)
            continue
        td = typed(d, "IThreadFeatureData")
        _call(member, td, "InitializeThreadData")
        # ★★ 顺序不能反：**先 AccessSelections，再设参数**。
        #   实测（diag_thread2）：先设参数再 AccessSelections，特征建不出来；
        #   反过来（AccessSelections → 设参数 → CreateFeature）才能成功 ——
        #   这也解释了为什么 `Size` 会被"吃掉"：AccessSelections 会重置数据。
        #   `AccessSelections` 在晚期绑定下必定抛 com_error(-2147417851)，但选择已经在
        #   文档里，CreateFeature 照样吃得到 —— 吞掉，只记录，不算失败。
        _r, acc_err = _call(member, td, "AccessSelections", sw.model, None)
        bad = {}
        for k, v in want.items():
            _r, e = _call(setattr, td, k, v)
            if e:
                bad[k] = e
        _f, c_err = _call(member, fm, "CreateFeature", td)
        with contextlib.suppress(Exception):
            member(td, "ReleaseSelectionAccess")
        with contextlib.suppress(Exception):
            member(sw.model, "EditRebuild3")
        sw.clear_selection()
        f = _new_feature(sw, tree0)                        # ← 判据：特征树 diff
        if f is not None:
            break
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)

    if f is None and hit == 0:
        raise SwError(
            f"圆边一条都没选中：{miss}\n"
            "  `edges` 必须给**圆边的圆心坐标**（圆边的中点就是圆心）。\n"
            "  例：Ø3 轴伸到 z=22 时，顶面圆边的圆心是 [0,0,22]。\n"
            "  **不要给圆柱面** —— 选面路线实测建不出特征。")
    if f is None:
        raise SwError(
            f"真实螺纹线没生成特征（已按特征树 diff 重试 {attempts} 次）。\n"
            "  最常见原因：给了**圆柱面上的点**而不是**圆边的圆心**。\n"
            "  选面路线实测「不报错、不建特征、体积一字不变」。\n"
            + (f"  （COM: {c_err}）" if c_err else ""))
    fname = ""
    with contextlib.suppress(Exception):
        fname = str(member(typed(f, "IFeature"), "Name"))
    typed_ok = ""
    with contextlib.suppress(Exception):
        typed_ok = str(member(typed(f, "IFeature"), "GetTypeName2"))
    got = _thread_definition(sw, fname)
    v1 = _volume(sw)
    r_after = _max_cyl_radius_mm(sw)
    out = {"feature": fname, "type": typed_ok,
           "volume_before": v0, "volume_after": v1,
           "removed_mm3": round((v0 - v1) * 1e9, 3) if (v0 and v1) else None,
           "outer_r_before_mm": r_before, "outer_r_after_mm": r_after,
           "attempts": attempts,
           "want": {"Type": prof.rsplit("\\", 1)[-1], "Diameter_mm": dia,
                    "Pitch_mm": pitch, "EndCondition": ec_code},
           "got": got}
    if bad:
        out["set_errors"] = bad
    if acc_err:
        out["access_selections_error"] = ("已知现象，非致命：" + str(acc_err))
    # ★★ 判据一：去料 op 必须真的去料
    if v0 is not None and v1 is not None and (v0 - v1) * 1e9 < 1.0:
        raise SwError(
            f"螺纹特征建出来了但**几乎没去料**（{(v0 - v1) * 1e9:.3f} mm³）。\n"
            f"  读回的定义：{got}\n"
            "  常见原因：选的是圆柱面而不是圆边（那样根本不建特征），\n"
            "  或者直径/螺距填得比毛坯还大。")
    # ★★ 判据二：参数必须**真的生效**（Size 那次的教训）
    mism = []
    gd = got.get("Diameter")
    gp = got.get("Pitch")
    if isinstance(gd, (int, float)) and abs(gd - mm(float(dia))) > 1e-9:
        mism.append(f"diameter 请求 {dia}mm，落库 {gd * 1000:.4f}mm")
    if isinstance(gp, (int, float)) and abs(gp - mm(float(pitch))) > 1e-9:
        mism.append(f"pitch 请求 {pitch}mm，落库 {gp * 1000:.4f}mm")
    if isinstance(got.get("EndCondition"), (int, float)) and int(got["EndCondition"]) != ec_code:
        mism.append(f"end_condition 请求 {ec_code}，落库 {got['EndCondition']}")
    if mism:
        raise SwError(
            "螺纹参数**没生效**（特征建出来了，但定义里的值跟请求的不一致）：\n  "
            + "\n  ".join(mism)
            + f"\n  读回的定义：{got}")
    # ★★ 判据三：**牙顶必须齐轴径** —— 用圆柱面半径不变式，不用包围盒。
    #   螺旋面是 B 样条面，包围盒会虚报（Ø3 做完 M3×0.5 后 Y 向 3.0→3.4641），
    #   而实体其实干净得很。见 `_cylinder_radii` 的注释。
    if r_before is not None and r_after is not None and r_after > r_before + 0.02:
        raise SwError(
            f"螺纹把外圆撑大了：最大圆柱面半径 {r_before}mm → {r_after}mm。\n"
            "  牙顶应当齐轴径（去料），撑大说明螺纹是**增料**方向或选错了边/标准。\n"
            "  排查：`standard` 用 metric_die（外螺纹）还是 metric_tap（内螺纹）？\n"
            f"  读回的定义：{got}")
    return out


# ============================================================ 命名规范（M3，需求 2）
#
# 病根：SolidWorks 的自动名跟**界面语言**绑死（草图1 ↔ Sketch1、基准轴1 ↔ Axis1），
# 于是同一份计划在中文机/英文机上产出不同特征名，计划里写死名字的那一步就找不到东西。
# 用户 2026-09-25 拍板：**主名 = 中文**，只改「看得出是自动名」的，中英双向都认。
# 权威表在 assets/sw_names.json，逻辑在 sw_naming.py。这里只放 op。


def _feature_type_name(feat) -> str:
    """特征的类型名。**必须处理 "ICE"**。

    `GetTypeName2()` 对「Instant3D 方式创建」的拉伸/切除会回一个 `"ICE"` ——
    它是 Instant3D 的类型名，**不是错误**（见 30-pitfalls）；旧接口
    `GetTypeName()` 给的才是具体类型（Boss / Cut / Extrusion…）。同一份
    参考螺钉里 `切除-拉伸1` / `凸台-拉伸3` 都是 ICE，靠这个兜底才分得出来。
    """
    t = ""
    with contextlib.suppress(Exception):
        t = str(member(feat, "GetTypeName2"))
    if t == "ICE":
        with contextlib.suppress(Exception):
            t = str(member(feat, "GetTypeName"))
    return t


def _rename_feature(sw, old: str, new: str) -> str | None:
    """改名并**读回核对**（改名的返回值同样不可信）。成功回新名，失败回 None。"""
    f = None
    with contextlib.suppress(Exception):
        f = typed(sw.model.FeatureByName(str(old)), "IFeature")
    if f is None:
        return None
    with contextlib.suppress(Exception):
        f.Name = str(new)
    got = None
    with contextlib.suppress(Exception):
        got = str(member(sw.model.FeatureByName(str(new)), "Name"))
    return got


@op("set_feature_name", "重命名特征", "doc",
    "把特征树里的某个特征改名。**改名的返回值不可信，这里会读回核对**。\n"
    "★★ 一条必须知道的事实（2026-09-25 实测）：**改特征名会让引用它的尺寸全名跟着变** ——\n"
    "  `D1@草图1@零件.Part` 在把 `草图1` 改名成 `Rect40` 之后变成 `D1@Rect40@零件.Part`。\n"
    "  也就是说：**任何写死了尺寸全名的计划，都会因为一次改名而失效**。\n"
    "  ⇒ 规矩是「先定名、再标注尺寸」，或者干脆用 `set_dimension` 的\n"
    "  `sketch`+`index` / `match` 寻址（不依赖全名）。\n"
    "★ 新名不能跟树里已有名字重复（SolidWorks 会拒绝，读回核对会当场发现）。",
    api=["IModelDoc2.FeatureByName", "IFeature.Name"],
    params={"feature": {"type": "str", "desc": "现在的名字"},
            "new_name": {"type": "str", "desc": "想改成什么"}},
    requires=("doc_open",), verified=True)
def op_set_feature_name(sw, step, ctx):
    old = str(_p(step, ctx, "feature") or "").strip()
    new = str(_p(step, ctx, "new_name") or "").strip()
    if not old or not new:
        raise SwError("set_feature_name 需要 feature 和 new_name 两个参数。")
    # 先确认旧名存在 —— 这样「old == new」也能当**存在性断言**用
    # （探针里就靠 `feature=上视基准面, new_name=上视基准面` 来证明改名生效了）。
    if sw.model.FeatureByName(old) is None:
        raise SwError(f"特征树里没有叫 '{old}' 的东西。用 check_feature_count / "
                      "sw_inspect 核对一下实际名字。")
    if old == new:
        return {"feature": old, "name": new, "renamed": False,
                "note": "名字没变（已确认该名字存在）"}
    if sw.model.FeatureByName(new) is not None:
        raise SwError(f"树里已经有叫 '{new}' 的东西了，SolidWorks 不允许重名。"
                      "换个名字，或先把旧的改掉。")
    got = _rename_feature(sw, old, new)
    if got != new:
        raise SwError(
            f"改名失败：想把 '{old}' 改成 '{new}'，读回来是 {got!r}。\n"
            "  常见原因：① 原名字不存在（用 `list_features` 核对）；"
            "② 新名与已有特征重名；③ 该特征被压缩/在配置里没激活。")
    return {"feature": old, "name": got, "renamed": True}


@op("normalize_names", "统一特征名（按主名语言）", "doc",
    "★ 需求 2 的落地：扫一遍特征树，把**自动名**统一成主名语言（默认中文）。\n"
    "  `草图1 / 草图9` → 原样；`Sketch1` → `草图1`；`Boss-Extrude1` → `凸台-拉伸1`。\n"
    "★★ 三条安全规矩（都写死在这里，别放宽）：\n"
    "  ① **只改看得出来是自动名的**（能匹配某语言的模板 + 数字）。\n"
    "     显式命名过的（`Rect40`、`箱体轮廓`、`Plate`）**一律不碰** ——\n"
    "     自动改名覆盖人的命名，是比命名混乱更糟的事故。\n"
    "  ② 文件夹类节点（设计活页夹/材质/实体…）永不改。\n"
    "  ③ 目标名已被占用时，顺延到下一个空号，不覆盖。\n"
    "★ `planes`：默认 **False**（不动自带的三个基准面）。\n"
    "  想在英文机上把 `Front Plane` 也统一成 `前视基准面`，再显式给 True。\n"
    "★ `dry_run`：只报告要改什么，一个都不动 —— 破坏性操作前的标准动作。",
    api=["IModelDoc2.FirstFeature", "IFeature.GetNextFeature", "IFeature.GetTypeName2",
         "IFeature.GetTypeName", "IFeature.Name"],
    params={"primary": {"type": "str", "required": False, "default": "cn",
                        "desc": "主名语言 cn / en"},
            "planes": {"type": "bool", "required": False, "default": False,
                       "desc": "是否连自带基准面一起统一（默认不动）"},
            "dry_run": {"type": "bool", "required": False, "default": False,
                        "desc": "只列要改什么，不改"}},
    requires=("doc_open",), verified=True)
def op_normalize_names(sw, step, ctx):
    lang = str(_p(step, ctx, "primary", "cn") or "cn").lower()
    do_planes = bool(_p(step, ctx, "planes", False))
    dry = bool(_p(step, ctx, "dry_run", False))
    feats: list[tuple[str, str]] = []
    f = typed(member(sw.model, "FirstFeature"), "IFeature")
    guard = 0
    while f is not None and guard < 2000:
        guard += 1
        nm = ""
        with contextlib.suppress(Exception):
            nm = str(member(f, "Name"))
        if nm:
            feats.append((_feature_type_name(f), nm))
        f = typed(member(f, "GetNextFeature"), "IFeature")
    taken = {nm for _t, nm in feats}
    plan: list[dict] = []
    skipped: list[dict] = []
    for tname, nm in feats:
        if sw_naming_mod.is_never_rename(tname):
            continue
        kind = sw_naming_mod.kind_of(tname)
        if kind is None:
            skipped.append({"name": nm, "type": tname, "why": "类型不在命名表里"})
            continue
        if kind == "plane" and not do_planes:
            continue
        # 自带基准面（前视基准面 ↔ Front Plane）的名字是**固定名**，不在 {n} 模板里，
        # 得走 default_planes 表单独判 —— 2026-09-25 真机踩过：把「上视基准面」
        # 改成 "Top Plane" 之后，match_auto() 认不出它，被误判成「显式命名」跳过。
        pk = sw_naming_mod.match_default_plane(nm) if kind == "plane" else None
        if pk is not None:
            target = sw_naming_mod.plane_name(pk, lang) or nm
            if target == nm:
                continue
            if target in taken:
                skipped.append({"name": nm, "type": tname,
                                "why": f"目标名 '{target}' 已被占用，跳过（不覆盖已有名字）"})
                continue
            plan.append({"from": nm, "to": target, "kind": kind, "type": tname,
                         "how": f"自带基准面（{pk}）"})
            taken.discard(nm)
            taken.add(target)
            continue
        hit = sw_naming_mod.match_auto(nm, lang)
        if hit is None:
            skipped.append({"name": nm, "type": tname, "why": "显式命名，不动"})
            continue
        _k, idx = hit
        target = sw_naming_mod.canonical(kind, idx, lang)
        if target == nm:
            continue
        n = idx
        while target in taken and target != nm:
            n += 1
            target = sw_naming_mod.canonical(kind, n, lang)
        plan.append({"from": nm, "to": target, "kind": kind, "type": tname,
                     "how": "自动名", "renumbered": n != idx})
        taken.discard(nm)
        taken.add(target)
    done: list[dict] = []
    if not dry:
        for p in plan:
            got = _rename_feature(sw, p["from"], p["to"])
            if got != p["to"]:
                raise SwError(
                    f"统一命名失败：'{p['from']}' → '{p['to']}' 读回来是 {got!r}。\n"
                    "  已经改过的不会回滚（SolidWorks 没有事务），"
                    f"已完成 {len(done)} 项：{done}")
            done.append({"from": p["from"], "to": got, "kind": p["kind"]})
        with contextlib.suppress(Exception):
            sw.rebuild()
    return {"primary": lang,
            "language": sw.language_code(),
            "dry_run": dry,
            "renamed": done if not dry else plan,
            "would_rename": len(plan),
            "count": len(done) if not dry else 0,
            "skipped": skipped or None}


@op("check_language", "校验界面语言", "verify",
    "读 SolidWorks 界面语言，跟期望的语言码比对。\n"
    "★ 为什么值得单独有个 op：**自动名跟着语言走**，语言不对，\n"
    "  `草图1` 就会变成 `Sketch1`，计划里所有写死名字的引用全部失效。\n"
    "  而且 `GetCurrentLanguage()` 回的是**字符串**（`'chinese-simplified'`），\n"
    "  不是枚举整数 —— 照某些示例猜成 int 断言会永远为假且不报错。\n"
    "  跨机器跑计划前先跑它，能省掉一整轮「怎么找不到草图」的排查。",
    api=["ISldWorks.GetCurrentLanguage"],
    params={"expect": {"type": "str", "required": False, "default": "cn",
                       "desc": "期望的语言码 cn / en；给了不符就抛"}},
    requires=("doc_open",), verified=True)
def op_check_language(sw, step, ctx):
    raw = sw.current_language()
    code = sw_naming_mod.raw_to_lang(raw)
    primary = sw_naming_mod.primary_lang()
    out = {"language_raw": raw, "language": code,
           "primary": primary, "primary_satisfied": code == primary}
    exp = _p(step, ctx, "expect", "cn")
    if exp is not None:
        exp_code = sw_naming_mod.raw_to_lang(str(exp))
        out["expect"] = exp_code
        if exp_code != code:
            raise SwError(
                f"界面语言不符：实际 {raw!r}（→{code}），期望 {exp_code}。\n"
                "  自动命名会跟着语言走（cn: 草图1 / en: Sketch1），\n"
                "  语言不对时计划里写死的特征名会全部找不到。\n"
                "  要么切到期望语言的界面，要么先把计划里的名字过一遍 normalize_names。")
        out["expect_ok"] = True
    return out


@op("check_feature_count", "校验特征数", "verify",
    "数一下要素树里有几个特征，用于回归测试（建模是否真的产生了特征）。",
    api=["IModelDoc2.FirstFeature / IFeature.GetNextFeature"],
    params={"min": {"type": "int", "required": False, "default": 1},
            "max": {"type": "int", "required": False}},
    requires=("doc_open",), verified=True)
def op_check_feature_count(sw, step, ctx):
    n = sw.feature_count()
    lo = int(_p(step, ctx, "min", 1) or 1)
    hi = _p(step, ctx, "max")
    ok = n >= lo and (hi is None or n <= int(hi))
    if not ok:
        raise SwError(f"特征数校验失败：实际 {n}，期望 ["
                      f"{lo}, {hi if hi is not None else '∞'}]")
    return {"feature_count": n, "ok": True}


@op("check_sketch", "校验草图轮廓", "verify",
    "★ 「草图为什么拉伸不了」的第一诊断工具。报出：图元数、图元类型分布、轮廓数。\n"
    " 判读方法：\n"
    "   · 图元数 = 0 → 图元根本没画上。**样条最容易静默消失**（点表没包 VARIANT，见 30-pitfalls 6.2）\n"
    "   · 轮廓数 = 0 → 曲线是开口的，拉伸必挂\n"
    "   · 轮廓数 = 1 → 干净\n"
    "   · 轮廓数 > 1 → 多数正常（孔/槽天生多轮廓）；只有拉伸报「未闭合/自相交」时\n"
    "     才要怀疑：① 多段图元的端点没粘合（样条端点不会自动吸附）；② 两个闭环重叠（键槽压孔）\n"
    "   · 图元数远大于轮廓数且轮廓数很多 → 典型「一段一段拼不起来」",
    api=["IFeature.GetSpecificFeature2", "ISketch.GetSketchContourCount",
         "ISketch.GetSketchSegments", "ISketchSegment.GetType"],
    params={"sketch": {"type": "str", "required": False,
                       "desc": "草图名，不给就用刚画完的那张"}},
    requires=("doc_open",), verified=True)
def op_check_sketch(sw, step, ctx):
    # ★★ §149（2026-10-12）：草图**正在编辑**时也要能工作。
    #   以前只认 `sketch` 参数或 `ctx["last_sketch"]` —— 而 `sketch_begin` 阶段
    #   SolidWorks **还没给草图定名**（名字在 `sketch_end` 才落库），所以
    #   `ctx["last_sketch"]` 是空的 ⇒ 在草图里直接 `check_sketch` 必然报
    #   「没给草图名，也拿不到刚画完的草图」（探针 `sk_relation` /
    #   `sketch_circular_pattern` 就是这么红的）。
    #   ⇒ 补一条**活动草图**兜底：`GetActiveSketch2()` 的裸 Name（正在编辑的草图特征
    #     其实**已经在特征树里**，只是还没被 `sketch_begin` 记进 ctx）。
    name = _p(step, ctx, "sketch") or ctx.get("last_sketch") or _active_sketch_name(sw)
    if not name:
        raise SwError("没给草图名，也拿不到「刚画完的草图」，且当前没有正在编辑的草图。")
    # 草图图元类型码（swSketchSegments_e）
    TYPES = {0: "LINE", 1: "ARC", 2: "ELLIPSE", 3: "SPLINE", 4: "TEXT",
             5: "PARABOLA", 6: "HYPERBOLA"}
    hist: dict[str, int] = {}
    n_seg = 0
    want_pts = bool(_p(step, ctx, "dump_points", False))
    seg_pts: list[dict] = []
    # ★★★ 判据盲区修复（2026-10-09 真机逼出来）：以前整个读取块包在 `suppress(Exception)` 里，
    #   于是**「按名字找不到草图」和「草图是空的」都报成 `segments=0`** ——
    #   排查时会一路去查"图元为什么没画上"，而真因只是**名字不对**（改名没生效）。
    #   ⇒ 名字查不到 / 拿不到 ISketch 一律**显式抛错**，只有"草图真在、就是没图元"才回 0。
    feat = None
    with contextlib.suppress(Exception):
        feat = sw.model.FeatureByName(str(name))
    if feat is None:
        raise SwError(
            f"找不到草图特征 '{name}' —— 注意：这**不是**「草图是空的」，是**名字不对**。\n"
            f"  ○ 用 `feature_names()` 列一下当前文档里到底有哪些特征；\n"
            f"  ○ 中文模板下自动名形如 `草图1` / `3D草图1`（跨语言不可复现 ⇒ 建议建模时显式命名）。")
    sk = typed(member(typed(feat, "IFeature"), "GetSpecificFeature2"), "ISketch")
    if sk is None:
        raise SwError(f"'{name}' 不是草图特征（拿不到 ISketch）—— "
                      f"它可能是曲面/实体特征，`check_sketch` 只看草图。")
    for s in (member(sk, "GetSketchSegments") or ()):
        n_seg += 1
        t = -1
        with contextlib.suppress(Exception):
            t = int(member(typed(s, "ISketchSegment"), "GetType"))
        hist[TYPES.get(t, f"TYPE{t}")] = hist.get(TYPES.get(t, f"TYPE{t}"), 0) + 1
        if want_pts and t == 0:                     # 0 = LINE
            ln = typed(s, "ISketchLine")
            rec: dict = {"type": "LINE"}
            for tag, meth in (("start", "GetStartPoint2"), ("end", "GetEndPoint2")):
                with contextlib.suppress(Exception):
                    pt = typed(member(ln, meth), "ISketchPoint")
                    rec[tag] = [round(float(member(pt, "X")) * 1000, 6),
                                round(float(member(pt, "Y")) * 1000, 6),
                                round(float(member(pt, "Z")) * 1000, 6)]
            if "start" in rec and "end" in rec:
                seg_pts.append(rec)
    n_contour = sw.sketch_contour_count(name)
    out = {"sketch": name, "segments": n_seg, "contours": n_contour, "types": hist}
    exp_seg = _p(step, ctx, "expect_segments")
    if exp_seg is not None and n_seg != int(exp_seg):
        raise SwError(f"草图图元数不符：实际 {n_seg}，期望 {exp_seg}。\n"
                      f"  ★ 若实际 0：图元一个都没建上（3D 草图里 z 未生效？样条少点？）。")
    if want_pts:
        out["line_endpoints_mm"] = seg_pts
        out["n_lines_with_points"] = len(seg_pts)
    if n_seg == 0:
        out["verdict"] = "草图是空的 —— 图元一个都没建上。样条请检查是否用了 variant_r8()。"
    elif n_contour == 0:
        out["verdict"] = "没有闭合轮廓 —— 曲线是开口的，或端点没接上。"
    elif n_contour == 1:
        out["verdict"] = "单个闭合轮廓，正常。"
    else:
        out["verdict"] = (f"{n_contour} 个轮廓。有孔/槽/孔阵时是正常的；"
                          "若拉伸报未闭合/自相交，查端点粘合与闭环重叠。")
    return out


@op("check_sketch_constraint", "校验草图约束状态", "verify",
    "★ 参数化的体检项。读草图的约束状态 + 尺寸个数。\n"
    "  判读（`swConstrainedStatus_e`）：\n"
    "    · **2 UnderConstrained** → 图形还能被拖动，改尺寸会变形，**不可参数化**\n"
    "    · **3 FullyConstrained** → 目标状态\n"
    "    · 4 OverConstrained → 有多余约束，要删\n"
    "    · 7 AutosolveOff → 自动求解被关了，这时状态不可信\n"
    "  `n_dims` = 0 是最典型的病症：`AddToDB` 模式下 SolidWorks 不加任何尺寸，\n"
    "  于是 `set_dimension` 无从下手 —— **画完记得 `fully_define`**。\n"
    "★★ 两个真机细节（2026-09-25，M3）：\n"
    "  ① **已闭合的草图照样能读**（按名字走 FeatureByName → ISketch），\n"
    "     不必重新进草图编辑；\n"
    "  ② 草图**开着**的时候读的是活动草图。\n"
    "  给了 `expect` 就对不上就抛错，用来当验收闸门。",
    api=["ISketch.GetConstrainedStatus", "IFeature.GetSpecificFeature2",
         "IModelDoc2.GetActiveSketch2", "IFeature.GetFirstDisplayDimension"],
    params={"sketch": {"type": "str", "required": False,
                       "desc": "草图名；不给则：草图开着用活动草图，否则用刚画完的那张"},
            "expect": {"type": "str", "required": False,
                       "desc": "期望状态名（如 'FullyConstrained'）或状态码；给了就对不上就抛"},
            "dims": {"type": "bool", "required": False, "default": True,
                     "desc": "是否顺带列出该草图的尺寸"},
            "expect_min_dims": {"type": "int", "required": False,
                                "desc": "★ 要求该草图**至少有 N 个真尺寸** —— "
                                        "「草图必须用真尺寸画」这条口径的验收闸门"},
            "expect_fully_defined": {"type": "bool", "required": False,
                                     "desc": "要求完全定义（等价 expect=FullyConstrained）"},
            "dump_points": {"type": "bool", "required": False, "default": False,
                            "desc": "★ 回带每个**直线段**的端点坐标（mm）。3D 草图的硬判据 —— "
                                    "读 `ISketchLine.GetStartPoint2/GetEndPoint2` → `ISketchPoint.X/Y/Z`"
                                    "（读回是**全局 mm**，2D 草图里 z 恒为 0）"},
            "expect_segments": {"type": "int", "required": False,
                                "desc": "期望图元个数；不符即报错（给了就是验收闸门）"}},
    requires=("doc_open",), verified=True)
def op_check_sketch_constraint(sw, step, ctx):
    want = _p(step, ctx, "sketch")
    target: str | None = None
    if want:
        target = str(want)
    elif not ctx.get("sketch_open"):
        target = ctx.get("last_sketch")
    if not target and not ctx.get("sketch_open"):
        raise SwError("没给草图名，也拿不到「刚画完的草图」。")
    code = _constrained_status(sw, target)
    if code is None:
        raise SwError(
            f"读不到草图{' ' + repr(target) if target else '（活动草图）'}的约束状态。"
            "请确认草图名对、或草图处于打开状态。")
    out = {"sketch": target or "(活动草图)",
           "status": code,
           "status_meaning": CONSTRAINED_STATUS.get(code),
           "fully_defined": code == 3,
           "under_constrained": code == 2}
    if _p(step, ctx, "dims", True):
        dims = [d for d in _all_dimensions(sw)
                if target is None or str(d.get("feature")) == target
                or f"@{target}@" in str(d.get("full_name"))]
        if target is None:
            dims = _all_dimensions(sw)
        out["n_dims"] = len(dims)
        if dims:
            out["dims"] = [{"full_name": d.get("full_name"), "value_mm": d.get("value_mm")}
                           for d in dims]
    if code == 2 and out.get("n_dims") == 0:
        # ★ 2026-10-02：标注已**封存** —— 这条提示原来让人去调 `fully_define`/`dimension`
        #   （两个都封了），等于把使用者往坑里带。现在改成"这是**预期状态**"。
        out["hint"] = ("欠约束且一个尺寸都没有 —— **这是当前阶段的预期状态**"
                       "（尺寸标注已于 2026-10-02 封存，见 sw_ops.SEALED_REASON）。"
                       "几何正确性看 check_bbox / check_mass；尺寸改由后续"
                       "「图纸绘制」环节在**工程图**里标。")
    # ---- ★ 口径闸门：草图必须是「真尺寸」画出来的 ----
    #   用户口径（2026-09-25）：**参数标注是草图的关键操作**。没标尺寸的草图在
    #   图里能随意拖动、改型就变形，而且后面出工程图时「模型项目」带不出任何尺寸。
    #   ⇒ 这里给一个可断言的闸门，让「有没有标尺寸」变成硬验收项而不是靠自觉。
    min_dims = _p(step, ctx, "expect_min_dims")
    if min_dims is not None:
        got = out.get("n_dims")
        if got is None:
            got = len([d for d in _all_dimensions(sw)
                       if target is None or str(d.get("feature")) == target
                       or f"@{target}@" in str(d.get("full_name"))])
            out["n_dims"] = got
        if int(got) < int(min_dims):
            raise SwError(
                f"草图 {out['sketch']} 的真尺寸不够：只有 {got} 个，要求 ≥ {int(min_dims)} 个。\n"
                "  ★ 尺寸标注**已于 2026-10-02 封存**（`dim_auto` / `dimension` / "
                "`fully_define` 全部禁用，见 sw_ops.SEALED_REASON）——"
                "当前阶段请不要用 `expect_min_dims` 当闸门（生成器里统一写 0）。\n"
                "  几何正确性改由 check_bbox / check_mass 兜；"
                "尺寸改由后续「图纸绘制」环节在**工程图**里标。")
        out["expect_min_dims_ok"] = True
    efd = _p(step, ctx, "expect_fully_defined")
    if efd is not None:
        if bool(out["fully_defined"]) != bool(efd):
            raise SwError(
                f"草图 {out['sketch']} 完全定义状态不符：实际 {out['fully_defined']}"
                f"（{out['status_meaning']}），要求 {bool(efd)}。")
        out["expect_fully_defined_ok"] = True
    exp = _p(step, ctx, "expect")
    if exp is not None:
        exp_code = exp
        if isinstance(exp, str):
            rev = {v.lower(): k for k, v in CONSTRAINED_STATUS.items()}
            key = exp.strip().lower()
            if key in rev:
                exp_code = rev[key]
            elif key.isdigit():
                exp_code = int(key)
            else:
                raise SwError(f"expect 看不懂: {exp!r}。可用："
                              f"{', '.join(sorted(set(CONSTRAINED_STATUS.values())))}")
        if int(exp_code) != code:
            raise SwError(
                f"草图约束状态不符：实际 {code}（{CONSTRAINED_STATUS.get(code)}），"
                f"期望 {exp_code}（{CONSTRAINED_STATUS.get(int(exp_code))}）。")
        out["expect_ok"] = True
    return out


@op("check_mass", "校验质量属性", "verify",
    "读质量/体积/表面积。回归时用来比对「几何是不是同一件东西」——"
    "光看特征数不够，改了尺寸也能让特征数不变。",
    api=["IModelDocExtension.CreateMassProperty2"],
    params={"expect_mass_g": {"type": "float", "required": False,
                              "desc": "期望质量(克)，给了就比对"},
            "tol": {"type": "float", "required": False, "default": 0.02,
                    "desc": "质量的相对容差"},
            "expect_volume_mm3": {"type": "float", "required": False,
                                  "desc": "期望体积(mm³)。★★ **镜像/布尔类判据"
                                          "必须用它而不是质量**：质量依赖材料密度"
                                          "（未赋材料时取 1000kg/m³）⇒ 跨机器"
                                          "不可移植；体积是纯几何量"},
            "vol_tol": {"type": "float", "required": False, "default": 0.005,
                        "desc": "体积的相对容差"}},
    requires=("doc_open",), verified=True)
def op_check_mass(sw, step, ctx):
    # ★★ fresh=True（2026-10-12）：先重建再读。体积非 0 但陈旧时旧逻辑会直接采信，
    #   于是「加厚/切除后马上量」量到的可能是**上一步**的体积。见 SwSession.mass_props。
    mp = sw.mass_props(fresh=True)
    mp["bbox_m"] = sw.bounding_box()
    # ★★ 2026-10-02：**读失败必须报错，不能当成 0**。实体不可见/抑制时
    #   `CreateMassProperty2` 不报错、回 0 质量 —— 与 `GetBodies2(0,True)` 读到空同源
    #   （见 `sw_connect.bodies()`）。真机踩过：报告里写着 `mass=0`，看着像"空零件"，
    #   其实特征数 18、实体 1 个。放它过去 = 又一个"假绿灯"。
    if mp.get("suspect"):
        raise SwError(
            f"质量属性读不到：{mp['suspect']}。"
            f"这不是「质量 0」而是**读失败**（实体可见性/重建态问题，见 sw_connect.bodies）。"
            f"排查：先 `check_feature_count`/`save_as` 确认实体在、再 `rebuild` 重读。")
    exp = _p(step, ctx, "expect_mass_g")
    # ★ 无论给不给期望值，都把实测值**回带**（2026-10-02）：
    #   `--calibrate` 模式**不给** expect_mass_g（只测不判），这时报告里必须有
    #   实测质量/体积，否则标定跑完拿不到数据、只能再烧一轮真机。
    actual_g = None
    if "mass_kg" in mp:
        actual_g = mp["mass_kg"] * 1000.0
        mp["actual_mass_g"] = round(actual_g, 4)
        if "volume_m3" in mp:
            mp["volume_mm3"] = round(float(mp["volume_m3"]) * 1e9, 4)
    if exp is not None and actual_g is not None:
        tol = float(_p(step, ctx, "tol", 0.02) or 0.02)
        if abs(actual_g - float(exp)) > abs(float(exp)) * tol:
            raise SwError(f"质量超差：实际 {actual_g:.3f}g，期望 {exp}g（容差 {tol:.0%}）。"
                          f"常见原因：没赋材料（密度取默认 1000kg/m³）")
    # ★★ 2026-10-03 加 `expect_volume_mm3`：**镜像类判据必须用体积，不能用质量**。
    #   原因：质量要材料密度（没赋材料时取默认 1000kg/m³）⇒ 不同机器上
    #   同一个 `expect_mass_g` 不可移植；而体积是纯几何量。
    #   典型用途（坑表 §100）：「建一半 + 镜像」每镜像一个孔，体积就少一个孔的量
    #   —— `CreateMirrorFeature` 返回 Feature 对象、err=None、GetErrorCode=0
    #   却什么都没建（§96②），**只有体积能抓住**。
    exp_v = _p(step, ctx, "expect_volume_mm3")
    if exp_v is not None:
        if "volume_mm3" not in mp:
            raise SwError(
                "体积读不到，不能校验 expect_volume_mm3"
                f"（mass_props 报 suspect={mp.get('suspect')!r}）")
        v = float(mp["volume_mm3"])
        tol_v = float(_p(step, ctx, "vol_tol", 0.005) or 0.005)
        if abs(v - float(exp_v)) > abs(float(exp_v)) * tol_v:
            raise SwError(
                f"体积超差：实际 {v:.3f}mm³，期望 {exp_v}mm³"
                f"（容差 {tol_v:.2%}）。"
                f"★ 镜像类操作最常见的失败就是**什么都没建但返回值像成功**"
                f"（坑表 §96②）；体积不变 ⇒ 镜像没生效。")
    return mp


@op("check_volume_delta", "校验体积**变化量**（去料 / 加料判据）", "verify",
    "断言「本步体积读数 − 上一次读数」≈ 期望变化量。\n"
    "★★ 为什么必须单独有这个 op：`check_mass` 比的是**绝对体积 ± 相对容差** ——\n"
    "  它**分不清**「加厚整体偏高 5%」和「切除真的切掉了」。而实体去料\n"
    "  （拉伸切除 / 旋转切除 / 加厚成壳…）最该证明的恰恰是「**这一刀到底切掉多少**」。\n"
    "  真机踩过：那 5% 的绝对偏差会把「没切到」和「切对了」一起盖住。\n"
    "★ 语义：**首次调用（或 `baseline: true`）只记基准、不判定**；之后每次调用\n"
    "  都对**上一次**读数做差。`expect_mm3` 为负 = 体积应**下降**。\n"
    "★ 容差：优先 `tol_mm3`（**绝对 mm³**）；否则 `tol`（相对上次体积的比例）。\n"
    "usage: {\"op\": \"check_volume_delta\", \"expect_mm3\": -283900, \"tol_mm3\": 17000}\n"
    "  ★ Δ≈0 ⇒ **这一刀没切到材料**（静默失败，铁律②）；Δ 差一个量级 ⇒ 方向/终止条件错。",
    api=["IModelDocExtension.CreateMassProperty2"],
    params={"expect_mm3": {"type": "float", "unit": "mm3", "required": False,
                           "desc": "期望的**体积变化量**(mm³)，负 = 体积下降"},
            "tol_mm3": {"type": "float", "unit": "mm3", "required": False,
                        "desc": "**绝对**容差(mm³)，推荐用这个"},
            "tol": {"type": "float", "required": False, "default": 0.01,
                    "desc": "相对上次体积的比例容差（给了 tol_mm3 则忽略）"},
            "baseline": {"type": "bool", "required": False, "default": False,
                         "desc": "true = 只记基准，不判定"}},
    # ★ 不依赖任何前置状态：首次调用即记基准（同理 has_feature 之类不参与）
    requires=("doc_open",), verified=True)
def op_check_volume_delta(sw, step, ctx):
    # ★★ fresh=True：先重建再读。本 op 的**全部意义**就在「Δ = 本次 − 上次」，
    #   读数若滞后一个特征，Δ 就整体错位（现场：连切 10 条，2 条 Δ 为正）。
    mp = sw.mass_props(fresh=True)
    if mp.get("suspect"):
        raise SwError(f"质量属性读不到：{mp['suspect']}（check_volume_delta 无法判定）")
    if "volume_m3" not in mp:
        raise SwError("体积读不到（mass_props 无 volume_m3），check_volume_delta 无法判定")
    v = float(mp["volume_m3"]) * 1e9
    prev = ctx.get("_vol_prev")
    ctx["_vol_prev"] = v
    out: dict = {"volume_mm3": round(v, 4),
                 "prev_mm3": None if prev is None else round(float(prev), 4)}
    if mp.get("refreshed_from") is not None:
        # 重建前的旧读数与重建后不同 ⇒ 之前那次读的是**陈旧值**（记录下来备查）
        out["refreshed_from_mm3"] = round(float(mp["refreshed_from"]) * 1e9, 4)
    if prev is None:
        out["note"] = "首次调用：已记基准，未判定"
        return out
    delta = v - float(prev)
    out["delta_mm3"] = round(delta, 4)
    if bool(_p(step, ctx, "baseline", False)):
        out["note"] = "baseline=true：已重记基准，未判定"
        return out
    exp = _p(step, ctx, "expect_mm3")
    if exp is None:
        out["note"] = "没给 expect_mm3：只记录变化量，未判定"
        return out
    tol_abs = _p(step, ctx, "tol_mm3")
    if tol_abs is not None:
        allow = abs(float(tol_abs))
        how = f"绝对容差 {allow:g}mm³（tol_mm3）"
    else:
        r = float(_p(step, ctx, "tol", 0.01) or 0.01)
        allow = abs(float(prev)) * r
        how = f"相对 {r:.2%} × 上次 {float(prev):g} ⇒ {allow:g}mm³"
    out["expect_mm3"] = float(exp)
    out["tol_used_mm3"] = round(allow, 4)
    out["how"] = how
    if abs(delta - float(exp)) > allow + 1e-9:
        raise SwError(
            f"体积**变化量**超差：本步 Δ={delta:+.3f}mm³，期望 {float(exp):+.3f}mm³，"
            f"差 {abs(delta - float(exp)):.3f} > {allow:.3f}mm³。\n"
            f"  （上次 {float(prev):.3f} → 本次 {v:.3f}）\n"
            f"  ★ Δ≈0 ⇒ **这一刀没切到材料**（去料 op 的经典静默失败，铁律②）；\n"
            f"  ★ Δ 与期望差一个量级 ⇒ 方向 / 终止条件不对（如双向贯穿被静默降级成单向）。")
    return out


@op("check_body", "校验体数与面积（曲面族判据）", "verify",
    "★ **曲面建模的唯一硬判据** —— 曲面件**没有体积、没有质量**，\n"
    "  `check_mass` 对它只会回 0（那是「没有体积」不是「建空了」），\n"
    "  `check_bbox` 更是**连曲面和实体都分不出来**（两者都回同一个盒子）。\n"
    "  所以每一步曲面都要过这个：**曲面体数 + 实体数 + 总面积**。\n"
    "★ 面积是**逐面累加**得到的（`IFace2.GetArea`）—— `IBody2` 上**没有** `GetArea`\n"
    "  （tlb 里查过，别照别的 CAD 写）；体分型靠 `IBody2.GetType()`\n"
    "  （`swBodyType_e`：0=实体、1=曲面体）。\n"
    "★ 用法（面积相对容差，默认 1%）：\n"
    "      {\"op\": \"check_body\", \"expect_sheet\": 1, \"expect_solid\": 0,\n"
    "       \"expect_area_mm2\": 8000}\n"
    "  「加了厚」这种**体数该变**的步骤，最该用它 —— 只涨特征不涨实体是经典静默失败（铁律①）。",
    api=["IPartDoc.GetBodies2", "IBody2.GetType", "IFace2.GetArea"],
    params={"expect_sheet": {"type": "int", "required": False,
                             "desc": "期望的**曲面体**数（给了就精确比对）"},
            "expect_solid": {"type": "int", "required": False,
                             "desc": "期望的**实体**数（给了就精确比对）"},
            "expect_area_mm2": {"type": "float", "required": False,
                                "desc": "期望的总面积(mm²)，给了就按 area_tol 比对"},
            "area_tol": {"type": "float", "required": False, "default": 0.01,
                         "desc": "面积的相对容差"},
            "min_sheet": {"type": "int", "required": False, "desc": "曲面体数下限"},
            "min_solid": {"type": "int", "required": False, "desc": "实体数下限"}},
    requires=("doc_open",), verified=True)
def op_check_body(sw, step, ctx):
    bs = _body_stat(sw)
    exp_sheet = _p(step, ctx, "expect_sheet")
    exp_solid = _p(step, ctx, "expect_solid")
    exp_area = _p(step, ctx, "expect_area_mm2")
    min_sheet = _p(step, ctx, "min_sheet")
    min_solid = _p(step, ctx, "min_solid")
    # ★ 一个都没给 = 只测不判（标定用）。这不算错，但要说清楚。
    if exp_sheet is None and exp_solid is None and exp_area is None \
            and min_sheet is None and min_solid is None:
        bs["expect_ok"] = None
        bs["note"] = "没给任何期望值 —— 只回带实测值，没有判定"
        return bs
    if exp_sheet is not None and bs["n_sheet"] != int(exp_sheet):
        raise SwError(
            f"曲面体数不符：实际 {bs['n_sheet']}，期望 {exp_sheet}。\n"
            f"  ★ 若实际是 0：这一步**根本没造出曲面**（特征树可能涨了 —— 铁律①）。\n"
            f"  ★ 若实际比期望多：多出来的那张面没被缝合/删掉。")
    if exp_solid is not None and bs["n_solid"] != int(exp_solid):
        raise SwError(
            f"实体数不符：实际 {bs['n_solid']}，期望 {exp_solid}。\n"
            f"  ★ 「曲面还没收口」时实体数就该是 0，别当成失败。")
    if min_sheet is not None and bs["n_sheet"] < int(min_sheet):
        raise SwError(f"曲面体数不足：实际 {bs['n_sheet']}，下限 {min_sheet}")
    if min_solid is not None and bs["n_solid"] < int(min_solid):
        raise SwError(f"实体数不足：实际 {bs['n_solid']}，下限 {min_solid}")
    if exp_area is not None:
        if bs.get("area_mm2") is None:
            raise SwError("面积读不到（一个面都没枚举到）—— 不能校验 expect_area_mm2")
        a = float(bs["area_mm2"])
        tol = float(_p(step, ctx, "area_tol", 0.01) or 0.01)
        if abs(a - float(exp_area)) > abs(float(exp_area)) * tol:
            raise SwError(
                f"面积超差：实际 {a:.4f}mm²，期望 {exp_area}mm²（容差 {tol:.2%}）。\n"
                f"  ★ 面积对不上而体数对得上 ⇒ 形状/尺寸错了（拉伸深度、半径、角度）。")
    bs["expect_ok"] = True
    return bs


@op("check_faces", "校验面数（曲面 / 分割线判据）", "verify",
    "数零件里的**面数**（可选连带按面积降序的分布），是 `split_line` / 剪裁这类\n"
    "「**不改变体数与总面积、只改变拓扑**」的特征唯一能抓住的判据。\n"
    "★ 为什么需要它：`check_body` 看的是体数 + 总面积 —— 一条**投影分割线**\n"
    "  把一张面劈成两张，**体数不变、总面积几乎不变**（只多一道划痕的误差），\n"
    "  于是 `check_body` 对它是**假绿**。面数才会从 1 变 2（2026-10-11 真机实测）。\n"
    "★ `surfaces` 默认 **True**（含曲面体）—— 分割线的目标通常是曲面，\n"
    "  不带上曲面体会数出 0 个面。\n"
    "★ 面积一律以 **mm²** 打出（`IFace2.GetArea` 原始单位是 m²，这里 ×1e6）。\n"
    "★★ `areas` 是拿**实际面积按降序**去比前 N 个 ⇒ 期望值**必须从大到小**给；\n"
    "   顺序反了会「数值都对、就是红」（2026-10-12 真机踩过）。",
    api=["IBody2.GetFaces", "IFace2.GetArea"],
    params={"expect": {"type": "int", "required": False, "desc": "期望面数（给了就精确比对）"},
            "min": {"type": "int", "required": False, "desc": "面数下限"},
            "surfaces": {"type": "bool", "required": False, "default": True,
                         "desc": "是否连曲面体的面一起数（分割线场景必须 True）"},
            "areas": {"type": "list", "required": False,
                      "desc": "期望的各面面积（mm²，**按降序**取前 N 个比对）"},
            "area_tol": {"type": "float", "required": False, "default": 0.02,
                         "desc": "面积相对容差（配合 areas 用）"}},
    requires=("doc_open",), verified=True)
def op_check_faces(sw, step, ctx):
    fs = _faces(sw, include_surfaces=bool(_p(step, ctx, "surfaces", True)))
    areas = sorted([f["area"] * 1e6 for f in fs if f.get("area")], reverse=True)
    out = {"n_faces": len(fs), "areas_mm2": [round(a, 4) for a in areas]}
    exp = _p(step, ctx, "expect")
    mn = _p(step, ctx, "min")
    if exp is not None and len(fs) != int(exp):
        raise SwError(f"面数不符：实际 {len(fs)}，期望 {exp}。\n"
                      f"  ★ 分割线 / 剪裁类特征只能靠**面数**判断是否真的生效（返回值不可信）。\n"
                      f"  实测各面面积(mm²)：{[round(a, 2) for a in areas]}")
    if mn is not None and len(fs) < int(mn):
        raise SwError(f"面数 {len(fs)} < 下限 {mn}。"
                      f"实测各面面积(mm²)：{[round(a, 2) for a in areas]}")
    exp_areas = _p(step, ctx, "areas")
    if exp_areas:
        tol = float(_p(step, ctx, "area_tol", 0.02) or 0.02)
        want = [float(x) for x in exp_areas]
        got = areas[:len(want)]
        bad = [(g, e) for g, e in zip(got, want) if e <= 0 or abs(g - e) / e > tol]
        if bad:
            raise SwError(f"面面积不符（按降序前 {len(want)} 个）：实际 "
                          f"{[round(g, 4) for g in got]}，期望 {want}（容差 {tol:.1%}）。\n"
                          f"  ★ 对不上 ⇒ 分割线没划在预期位置 / 剪裁没剪掉预期的那块。")
        out["areas_ok"] = True
    return out


@op("check_bbox", "校验包围盒", "verify",
    "读包围盒尺寸（mm）。比质量更直接能判断「齿顶圆直径对不对」。\n"
    "★ 不要换成 IModelDoc2.GetPartBox：实测它给的盒子比实体大（Ø24 的实体回 ±12.3124）。\n"
    "★★★ **`tol` 的语义陷阱**（2026-10-02 真机发现，很值钱）：`tol` 是**相对比例**，\n"
    "   实际容差 = `max(|期望| × tol, 0.2mm)`。于是计划里写 `tol: 0.2` 想表达「±0.2mm」的地方，\n"
    "   在 Ø113 的齿轮上实际成了 **±22.6mm** —— 校验器比作者以为的松 100 倍，\n"
    "   **报 True 什么都不说明**（齿轮轴期望 34.8 / 实测 34.527 / 却报 True 就是这么来的）。\n"
    "   ⇒ 三种模式，按需选，语义都写进结果里：\n"
    "      `tol_mm: 0.35`  —— **绝对 mm**（★ 推荐，与人的直觉一致）\n"
    "      `tol: 0.02`     —— 相对比例（兼容旧写法）\n"
    "      `mode: record`  —— **只记录不判定**（图形本身让 `GetBodyBox` 虚报时用，\n"
    "                        如螺旋扫掠面/螺纹 —— 以前是靠写 `tol: 99.0` 硬绕过）\n"
    "   ★ 结果**永远**带回每个轴的 `tol_used`（含实际容差与算法），容差不再不可见。",
    api=["IPartDoc.GetBodies2", "IBody2.GetBodyBox"],
    params={"expect": {"type": "list", "required": False,
                       "desc": "[dx,dy,dz] mm，给了就比对"},
            "tol_mm": {"type": "float", "unit": "mm", "required": False,
                       "desc": "★ **绝对容差（mm）**，推荐用这个。差 > 它才报错"},
            "tol": {"type": "float", "required": False, "default": 0.01,
                    "desc": "**相对比例**（★ 不是 mm！）：实际容差 = max(|期望|×tol, 0.2mm)"},
            "mode": {"type": "str", "required": False,
                     "desc": "record = 只记录不判定（GetBodyBox 对 B 样条面虚报时用）"}},
    requires=("doc_open",), verified=True)
def op_check_bbox(sw, step, ctx):
    box = sw.bounding_box()
    if not box:
        # ★★ 2026-10-02：这个报错以前只有一句话，真机踩到时**非常误导** ——
        #   `GetBodies2(0, True)`（只取可见实体）遇到实体被抑制就回空，
        #   于是一份明明有 18 个特征 / 1 个实体的文档被报成「零件可能是空的」。
        #   现在把「取实体诊断」一起打出来：哪一档取的、回几个。
        q = getattr(sw, "last_body_query", None)
        raise SwError(
            "读不到包围盒（零件可能是空的）。\n"
            f"  取实体诊断 last_body_query = {q}\n"
            "  ★ 若 `(-1, False)` 也回 0 而 `check_feature_count` 不为 0，"
            "说明实体处于**抑制/不可见态**（常见于某一步「选不中草图」失败之后），"
            "**不是**零件空 —— 先 `rebuild` 再重读，别改判据。")
    size = [round((box[i + 3] - box[i]) * 1000.0, 3) for i in range(3)]
    exp = _p(step, ctx, "expect")
    mode = str(_p(step, ctx, "mode", "") or "").lower()
    rec_only = mode in ("record", "record_only", "info")
    out: dict = {"size_mm": size, "box_m": box}
    q = getattr(sw, "last_body_query", None)
    if q:
        # 证据：这一份读数是用哪种取法拿到的（排查"读数在说谎"时唯一能对质的东西）
        out["body_query"] = q
    if exp:
        tol_abs = _p(step, ctx, "tol_mm")
        rel = _p(step, ctx, "tol")
        used = []
        for got, w0, ax in zip(size, [float(v) for v in exp], "XYZ"):
            w = abs(w0)
            if rec_only:
                allow, how = None, "记录（不判定）"
            elif tol_abs is not None:
                allow = abs(float(tol_abs))
                how = f"绝对容差 {allow:g}mm（tol_mm）"
            else:
                r = float(rel) if rel not in (None, 0) else 0.01
                allow = max(w * r, 0.2)
                how = (f"相对 {r:.1%} × {w:g} ⇒ **{allow:.3f}mm**（tol 是比例不是 mm！）")
            used.append({"axis": ax, "expect_mm": w0, "actual_mm": got,
                         "delta_mm": round(got - w0, 4),
                         "tol_mm": None if allow is None else round(allow, 4),
                         "how": how})
        out["mode"] = "record" if rec_only else ("abs" if tol_abs is not None else "rel")
        out["tol_used"] = used
        if not rec_only:
            bad = [u for u in used
                   if u["tol_mm"] is not None and abs(u["delta_mm"]) > u["tol_mm"] + 1e-9]
            if bad:
                u = bad[0]
                other = "；".join(f"{x['axis']} {x['actual_mm']}/{x['expect_mm']}"
                                  f"（容差 {x['tol_mm']}）" for x in used)
                raise SwError(
                    f"包围盒 {u['axis']} 方向超差：实际 {u['actual_mm']}mm，"
                    f"期望 {u['expect_mm']}mm，差 {u['delta_mm']:+g}mm > 容差 {u['tol_mm']}mm。\n"
                    f"  容差算法：{u['how']}\n"
                    f"  三个轴：{other}")
    return out


@op("check_cyl_radius", "校验圆柱面半径", "verify",
    "列出模型上所有**圆柱面**的半径（mm），可选跟期望值比对。\n"
    "★★ 涉及**螺纹/螺旋面**的零件，判「外圆有没有被撑大」必须用本 op，\n"
    "    **不能**用 `check_bbox`（2026-09-25 真机实测）：\n"
    "   螺纹的螺旋面是 **B 样条面**（`ISurface.Identity()==4006`），\n"
    "   `IBody2.GetBodyBox` 对它报的是**未裁剪曲面**的盒子 —— Ø3 光杆做完 M3×0.5 后\n"
    "   包围盒 Y 会从 3.0 虚涨到 **3.4641（=2√3，恰好是对边宽=轴径的正六边形对角）**，\n"
    "   X 向却纹丝不动；删掉螺纹特征立刻回 3.0；逐面扫描发现凸起的面全是 B 样条、\n"
    "   没有一块大平面；`SaveBMP` 抓视口一看，实体就是个**干净圆柱螺纹杆**。\n"
    "   ⇒ 包围盒虚报，几何是对的。圆柱面半径（本例恒为 1.5）才是可信判据。",
    api=["IBody2.GetFirstFace", "IFace2.GetNextFace", "IFace2.GetSurface",
         "ISurface.IsCylinder", "ISurface.CylinderParams", "IFace2.GetArea"],
    params={"expect_max": {"type": "float", "unit": "mm", "required": False,
                           "desc": "期望的最大圆柱面半径（给了就比对）"},
            "expect_count": {"type": "int", "required": False,
                             "desc": "期望的圆柱面个数（给了就比对）"},
            "tol": {"type": "float", "required": False, "default": 0.02}},
    requires=("doc_open",), verified=True)
def op_check_cyl_radius(sw, step, ctx):
    cyls = _cylinder_radii(sw)
    rs = [d["r_mm"] for d in cyls if "r_mm" in d]
    out = {"cylinders": cyls, "count": len(cyls), "max_r_mm": max(rs) if rs else None,
           "min_r_mm": min(rs) if rs else None}
    tol = float(_p(step, ctx, "tol", 0.02) or 0.02)
    exp_max = _p(step, ctx, "expect_max")
    if exp_max is not None:
        if out["max_r_mm"] is None:
            raise SwError("期望有圆柱面，但一个都没找到。")
        if abs(out["max_r_mm"] - float(exp_max)) > tol:
            raise SwError(
                f"最大圆柱面半径超差：实际 {out['max_r_mm']}mm，期望 {exp_max}mm"
                f"（容差 {tol}mm）。\n"
                "  常见原因：螺纹/凸台把外圆撑大了，或者选错了目标边。")
    exp_n = _p(step, ctx, "expect_count")
    if exp_n is not None and int(out["count"]) != int(exp_n):
        raise SwError(f"圆柱面个数不符：实际 {out['count']}，期望 {exp_n}")
    return out


@op("check_diagnostics", "校验参数诊断", "verify",
    "把上一步（gear_profile 之类）算出的工程诊断打出来/卡住。"
    "用于「宁可停下问人，也不要建出一个根切的齿轮」。",
    params={"fail_on": {"type": "str", "required": False, "default": "error",
                        "desc": "error / warn / none"}},
    requires=(), verified=True)
def op_check_diagnostics(sw, step, ctx):
    diags = ctx.get("gear_diagnostics") or []
    level = _p(step, ctx, "fail_on", "error")
    if level != "none":
        bad = [d for d in diags if d["level"] == level]
        if bad:
            raise SwError("工程诊断未通过：\n  " + "\n  ".join(
                f"[{d['code']}] {d['msg']}" for d in bad))
    return {"diagnostics": diags}


@op("lookup_standard", "查 GB 标准件尺寸（离线，不碰 SolidWorks）", "std",
    "★★ 需求 5 的落地：标准件尺寸**只有一个权威来源** —— `assets/gb_standard.json`，\n"
    "   由 `sw_gb.py` 读出来。**不要上网搜**（搜到的是二手表，公差/对边宽/头高经常串行），\n"
    "   也不要凭记忆填（M8 的对边宽 13 还是 14、M3 盘头 dk 5.5 还是 5.6 都栽过）。\n"
    "★★ 口径（用户 2026-09-25 拍板）：\n"
    "   · 比赛零件（一级减速器）= **跟图纸**，图纸没标再查表；用户自己的参照件优先于表；\n"
    "   · 自主设计的零件 = **跟 GB**。\n"
    "★★ 最值钱的用法：**让计划自证数字没抄错**。把查到的值写进 `expect`，\n"
    "   对不上就当场抛 —— 比事后拿卡尺量便宜得多。\n"
    "```\n"
    '  {"id":"g","op":"lookup_standard","family":"hex_bolt","spec":"M8",\n'
    '   "expect":{"s":13,"k":5.3,"pitch":1.25}}\n'
    "```\n"
    "★ 本 op **纯数据**，不开文档、不改模型、不需要前置状态。\n"
    "  真要查表也可以直接命令行：`python sw_gb.py --row hex_bolt M8`。",
    api=[],
    params={"family": {"type": "str", "required": False,
                       "desc": "族名，如 hex_bolt / hex_nut / parallel_key / deep_groove_bearing"},
            "spec": {"type": "str", "required": False,
                     "desc": "规格，如 M8 / 8x7 / 6204 / 4（圆锥销小端）"},
            "what": {"type": "str", "required": False, "default": "row",
                     "desc": "row(整行) / pitch / tap_drill / profiles / "
                             "bearing / bearings_for / taper_pin / fit / module / "
                             "length / hole / cheatsheet"},
            "length": {"type": "float", "required": False,
                       "desc": "taper_pin 用：销长度 L（算大端 D=d+L/50）"},
            "kind": {"type": "str", "required": False, "default": "bolt",
                     "desc": "length 用：bolt=螺栓系列 / screw=螺钉系列"},
            "hole_class": {"type": "str", "required": False, "default": "H7",
                           "desc": "fit 用：孔公差带，目前只支持 H 系"},
            "shaft_class": {"type": "str", "required": False, "default": "k6",
                            "desc": "fit 用：轴公差带 g6/h6/k6/n6/p6"},
            "series": {"type": "str", "required": False, "default": "medium",
                       "desc": "hole 用：fine / medium / coarse"},
            "section": {"type": "str", "required": False,
                        "desc": "cheatsheet 用：留空=全表，或写族名/节名"},
            "expect": {"type": "dict", "required": False,
                       "desc": "列名→期望值；给了就逐项比对，抄错数字当场抛"},
            "search": {"type": "str", "required": False, "desc": "按中文名/英文名/GB 号模糊找族"},
            "template": {"type": "str", "required": False,
                         "desc": "打印建模模板，如 slotted_pan_screw"},
            "tol": {"type": "float", "required": False, "default": 0.001}},
    requires=(), verified=True)
def op_lookup_standard(sw, step, ctx):
    fam = _p(step, ctx, "family")
    spec = _p(step, ctx, "spec")
    what = str(_p(step, ctx, "what", "row") or "row")
    tpl = _p(step, ctx, "template")
    key = _p(step, ctx, "search")
    if tpl:
        t = sw_gb.template(str(tpl))
        if not t:
            raise SwError(f"没有模板 {tpl!r}。可用：{', '.join(sw_gb.templates())}")
        return {"template": str(tpl), "body": t}
    if key:
        hits = sw_gb.search(str(key))
        if not hits:
            raise SwError(f"没找到匹配 {key!r} 的标准件族。可用：{', '.join(sw_gb.families())}")
        return {"matches": [{"family": k, "cn": c, "gb": g, "confidence": cf}
                            for k, c, g, cf in hits]}
    # ★ 这些支**不需要 family**，必须先处理 —— 否则 `what=pitch` 会被下面那句
    #   "需要 family" 拦掉（探针 gb_hex_bolt 第一轮就栽在这）。
    if what == "pitch":
        if not spec:
            raise SwError("what=pitch 需要 spec（如 M8）")
        return {"spec": str(spec), "pitch_coarse": sw_gb.pitch(str(spec)),
                "pitch_fine": sw_gb.pitch_fine_options(str(spec))}
    if what == "tap_drill":
        if not spec:
            raise SwError("what=tap_drill 需要 spec（如 M8）")
        return {"spec": str(spec), "tap_drill": sw_gb.tap_drill(str(spec))}
    if what == "profiles":
        return {"external": sw_gb.thread_profile("external"),
                "internal": sw_gb.thread_profile("internal")}
    if what == "hole":
        # ★ 通孔查表：不传 family 时 spec 是螺纹规格（M8），走 thread.clearance_hole
        if not spec:
            raise SwError("what=hole 需要 spec（如 M8）")
        return {"spec": str(spec), "series": str(_p(step, ctx, "series", "medium")),
                "hole": sw_gb.clearance_hole(str(spec),
                                             str(_p(step, ctx, "series", "medium")))}
    if what == "bearing":
        if not spec:
            raise SwError("what=bearing 需要 spec（如 6204）")
        try:
            return {"spec": str(spec), "row": sw_gb.bearing(str(spec)),
                    "gb": sw_gb.gb_number("deep_groove_bearing")}
        except KeyError as e:
            raise SwError(str(e)) from None
    if what == "bearings_for":
        if not spec:
            raise SwError("what=bearings_for 需要 spec = 内径（如 20）")
        got = sw_gb.bearings_by_bore(float(spec))
        if not got:
            raise SwError(f"内径 {spec} 没有可选轴承（表里 10~70）")
        return {"bore": float(spec), "options": got}
    if what == "taper_pin":
        if not spec:
            raise SwError("what=taper_pin 需要 spec = 小端直径（如 4）")
        try:
            out = {"pin": sw_gb.taper_pin(str(spec)), "gb": sw_gb.gb_number("taper_pin")}
        except KeyError as e:
            raise SwError(str(e)) from None
        ln = _p(step, ctx, "length")
        if ln is not None:
            out["big_end_D"] = sw_gb.taper_pin_big_end(float(spec), float(ln))
            out["formula"] = "D = d + L/50"
        return out
    if what == "module":
        if not spec:
            raise SwError("what=module 需要 spec = 模数（如 2）")
        m = float(spec)
        return {"module": m, "standard": sw_gb.is_standard_module(m),
                "advice": sw_gb.module_advice(m),
                "series": sw_gb.module_series()}
    if what == "length":
        if not spec:
            raise SwError("what=length 需要 spec = 目标长度（如 63）")
        kind = str(_p(step, ctx, "kind", "bolt") or "bolt")
        try:
            return {"kind": kind, "target": float(spec),
                    "up": sw_gb.nearest_length(kind, spec, "up"),
                    "down": sw_gb.nearest_length(kind, spec, "down"),
                    "near": sw_gb.nearest_length(kind, spec, "near"),
                    "exact": sw_gb.is_standard_length(kind, spec)}
        except KeyError as e:
            raise SwError(str(e)) from None
    if what == "fit":
        h = str(_p(step, ctx, "hole_class", "H7") or "H7")
        s = str(_p(step, ctx, "shaft_class", "k6") or "k6")
        # spec 也可以写成 "20" / "20 H7 k6"（空白分隔）
        parts = str(spec or "").split() if spec else []
        if not parts:
            raise SwError("what=fit 需要 spec = 公称尺寸（如 20，或 '20 H7 k6'）")
        nominal = float(parts[0])
        if len(parts) > 1:
            h = parts[1]
        if len(parts) > 2:
            s = parts[2]
        try:
            return sw_gb.fit(nominal, h, s)
        except KeyError as e:
            raise SwError(str(e)) from None
    if what == "cheatsheet":
        sec = _p(step, ctx, "section")
        return {"markdown": sw_gb.cheatsheet(str(sec) if sec else None)}
    if not fam:
        raise SwError("lookup_standard 需要 family（或 search / template / what=…）。\n"
                      f"  可用族：{', '.join(sw_gb.families())}\n"
                      "  可用 what：row / pitch / tap_drill / profiles / hole / bearing / "
                      "bearings_for / taper_pin / module / length / fit / cheatsheet\n"
                      "  例：{\"op\":\"lookup_standard\",\"family\":\"hex_bolt\",\"spec\":\"M8\"}")
    if not spec:
        raise SwError(f"lookup_standard 需要 spec。{fam} 可用："
                      f"{', '.join(sw_gb.specs(str(fam)))}")
    try:
        r = sw_gb.row(str(fam), str(spec))
    except KeyError as e:
        raise SwError(str(e)) from None
    f = sw_gb.family(str(fam))
    out = {"family": str(fam), "gb": f.get("gb", ""), "cn": f.get("cn", ""),
           "confidence": f.get("confidence", ""), "row": r}
    exp = _p(step, ctx, "expect")
    if isinstance(exp, dict) and exp:
        tol = float(_p(step, ctx, "tol", 0.001) or 0.001)
        bad = []
        for col, want in exp.items():
            got = r.get(col)
            if got is None:
                bad.append(f"{col}: 表里没这一列（有 {', '.join(r)}）")
                continue
            same = (abs(float(got) - float(want)) <= tol
                    if isinstance(got, (int, float)) and not isinstance(got, bool)
                    and str(want).replace(".", "", 1).replace("-", "", 1).isdigit()
                    else str(got) == str(want))
            if not same:
                bad.append(f"{col}: 计划里写 {want}，表里是 {got}")
        if bad:
            raise SwError(
                f"{f.get('cn', fam)} {spec} 与 GB 表（{f.get('gb', '')}）**对不上**：\n  "
                + "\n  ".join(bad)
                + "\n  → 要么改计划，要么改 assets/gb_standard.json（后者请说明依据）。")
        out["expect_ok"] = True
    return out


# ============================================================ 执行器


def _sealed_exempt(step: dict) -> bool:
    """封存 op 的**放行开关** —— 只给自检/诊断脚本用，正常计划永远拿不到。

    两条通道（任一条即可）：
      · step 里写 `"allow_sealed": true`   —— 计划作者显式声明「我知道它被封了」
      · 环境变量 `YOYO_ALLOW_SEALED=1`     —— `_diag/` 脚本 & 回归用

    ★ 为什么留这个口子而不是直接删代码：封存的是**入口**，不是**知识**。
      重开那天还得靠这些代码；现在要保证的是「**普通计划里调不起来**」。
    """
    if step.get("allow_sealed"):
        return True
    return os.environ.get("YOYO_ALLOW_SEALED", "").strip() not in ("", "0", "false", "False")


def run_op(sw, step: dict, ctx: dict) -> dict:
    """执行一个步骤。step 至少要有 'op' 键。"""
    name = step.get("op")
    if name not in OPS:
        raise SwError(f"未知操作 '{name}'。可用：{sorted(OPS)}")
    spec = OPS[name]
    # ★★ 封存闸门（2026-10-02）：被封存的 op 在**任何**执行路径上都调不起来 ——
    #   `sw_plan.execute` 与 `sw_probe.run_plan_in_session` 都走这里，所以一处拦全部。
    if spec.sealed and not _sealed_exempt(step):
        raise SwError(
            f"操作 '{name}'（{spec.cn}）**已封存**，计划里不允许使用。\n"
            f"  封存理由：{spec.sealed_reason}\n"
            f"  ⇒ 要标尺寸请等「图纸绘制」环节（在工程图里标）；\n"
            f"  ⇒ 自检/诊断确需临时放行：环境变量 YOYO_ALLOW_SEALED=1，"
            f"或在该 step 上写 allow_sealed: true。")
    fn = spec.fn
    if fn is None:
        raise SwError(f"操作 '{name}' 只有规格没有实现")
    result = fn(sw, step, ctx) or {}
    if step.get("id"):
        ctx.setdefault("outputs", {})[step["id"]] = result
    return result


def catalog() -> str:
    """生成可读的操作目录（给 AI 当索引，也给 reference 文档用）。

    这份文本是 references/10-operations.md 的正文，由 sw_ops.py 生成。
    **不要手工改那个 md** —— 改了会被 sw_selftest 的漂移检查抓出来。
    """
    lines = [
        "<!-- 本文件由 scripts/sw_ops.py 的 catalog() 自动生成，请勿手工修改。",
        "     改操作定义请改 sw_ops.py，然后跑：",
        "       python sw_ops.py --write-md ../references/10-operations.md",
        "     再跑 python sw_selftest.py 确认没有漂移。 -->",
        "",
        "# 操作手册（SolidWorks 建模动作 → API 调用序列）",
        "",
        f"共 {len(OPS)} 个操作。每个操作列出：参数、前置条件、以及**内部实际调用的 API**。",
        "「前置」用的是状态机名词："
        + "、".join(f"`{k}`={v}" for k, v in STATES.items()) + "。",
        "",
        "长度参数一律**毫米**、角度一律**度**；op 内部负责换算成 SolidWorks 要的米/弧度。",
        "",
    ]
    for cat, title in CATS:
        items = [o for o in OPS.values() if o.cat == cat]
        if not items:
            continue
        lines.append(f"\n## {title}（`{cat}`）\n")
        for o in sorted(items, key=lambda x: x.name):
            flag = "已实测" if o.verified else "待实测"
            if o.sealed:
                flag = "⛔ 已封存"
            lines.append(f"### `{o.name}` — {o.cn}  ·  {flag}")
            lines.append("")
            if o.sealed:
                lines.append("> **已封存，不要在计划里使用。** 理由：" +
                             o.sealed_reason.replace("\n", " "))
                lines.append("")
            lines.append(f"```\n{o.signature()}\n```")
            lines.append("")
            for ln in o.doc.split("\n"):
                lines.append(ln if not ln.strip() else f"{ln}  ")
            lines.append("")
            if o.requires:
                lines.append("**前置**：" + req_label(o.requires) + "  ")
            if o.effect:
                lines.append("**产生**：" + "、".join(f"`{s}`" for s in o.effect) + "  ")
            if o.one_of:
                for grp in o.one_of:
                    lines.append("**至少给一个**：" + "、".join(f"`{g}`" for g in grp) + "  ")
            if o.params:
                lines.append("")
                lines.append("| 参数 | 类型 | 单位 | 必填 | 说明 |")
                lines.append("|---|---|---|---|---|")
                for pname, ps in o.params.items():
                    req = "否" if not ps.get("required", True) or "default" in ps else "是"
                    dflt = f"（默认 {ps['default']}）" if "default" in ps else ""
                    lines.append(
                        f"| `{pname}` | {ps.get('type', 'any')} | {ps.get('unit', '')} | "
                        f"{req} | {(ps.get('desc') or '')}{dflt} |")
            if o.api:
                lines.append("")
                lines.append("**调用**：`" + "` → `".join(o.api) + "`")
            lines.append("")
    return "\n".join(lines)


@op("ref_plane", "参考基准面", "ref",
    "在**选中的面/基准面**上按距离偏移出一个新基准面。\n"
    "典型用途：轴身从 z=0 拉出后，螺纹段的草图要画在 z=-100 处 —— 就得先偏移一个面。\n"
    "建好后用它的**名字**（一般叫「基准面1」）配合 select_plane 选中。\n"
    "★ 偏移方向由 flip 控制：选中面的法向朝哪边，正值就往哪边偏。",
    api=["IModelDoc2.CreatePlaneAtOffset3"],
    params={"offset": {"type": "float", "unit": "mm", "desc": "偏移距离"},
            "flip": {"type": "bool", "required": False, "default": False,
                     "desc": "反向偏移"},
            "name": {"type": "str", "required": False}},
    requires=("doc_open",),
    verified=True)
def op_ref_plane(sw, step, ctx):
    if sw.selected_count() == 0:
        raise SwError("建参考基准面前必须先选中一个平面或基准面"
                      "（前一步用 select_plane）")
    before = {n for t, n in sw.feature_names() if t == "RefPlane"}
    member(sw.model, "CreatePlaneAtOffset3",
           mm(float(_p(step, ctx, "offset"))),
           bool(_p(step, ctx, "flip", False)), True)
    after = [n for t, n in sw.feature_names() if t == "RefPlane"]
    fresh = [n for n in after if n not in before]
    if not fresh:
        raise SwError("基准面没建出来 —— 检查前一步选中的是不是真的平面/基准面")
    name = _p(step, ctx, "name") or fresh[-1]
    if _p(step, ctx, "name"):
        f = typed(sw.model.FeatureByName(fresh[-1]), "IFeature")
        with contextlib.suppress(Exception):
            f.Name = _p(step, ctx, "name")
    ctx.setdefault("feature_names", []).append(name)
    # 把新基准面选上：紧接着的 sketch_begin 就能直接画，不必再 select_plane。
    # 名字在中文/英文界面下不一样（「基准面1」/「Plane1」），让调用方猜名字太脆。
    sw.clear_selection()
    sw.select(name, "plane")
    return {"plane": name, "offset": _p(step, ctx, "offset")}


@op("polyline", "折线/自定义多边形", "sketch",
    "按点列依次画直线，可自动闭合。点坐标是**草图坐标（mm）**，不是三维坐标。\n"
    "一次画出完整截面（螺纹牙型、非标轮廓），比堆一串单段 line 干净得多。\n"
    "★ 和「正多边形 polygon」的区别：polygon 是正 n 边形（给边数和外接圆半径），\n"
    "  polyline 是你把每个点的坐标都算好交进来 —— 螺纹牙型这类非规则截面用它。\n"
    "★ 每段都是**独立图元**，只有端点严格首尾相接时 SolidWorks 才认成 1 个轮廓。",
    api=["ISketchManager.CreateLine"],
    params={"points": {"type": "list", "desc": "[[x1,y1],[x2,y2],...]，单位 mm"},
            "close": {"type": "bool", "required": False, "default": True,
                      "desc": "末点是否连回首点"}},
    requires=("sketch_open",),
    verified=True)
def op_polyline(sw, step, ctx):
    raw = _p(step, ctx, "points") or []
    if len(raw) < 2:
        raise SwError(f"多边形至少要 2 个点，收到 {len(raw)} 个")
    pts = [(float(p[0]), float(p[1])) for p in raw]
    seq = pts + [pts[0]] if _p(step, ctx, "close", True) else pts
    sm = _sm(sw)
    made = 0
    for (x1, y1), (x2, y2) in zip(seq, seq[1:]):
        s = sm.CreateLine(mm(x1), mm(y1), 0, mm(x2), mm(y2), 0)
        if s is not None:
            ctx.setdefault("segments", []).append(s)
            made += 1
    if made != len(seq) - 1:
        raise SwError(f"折线只画出 {made}/{len(seq) - 1} 段 —— 有点重合了？")
    return {"points": len(pts), "segments": made, "closed": bool(_p(step, ctx, "close", True))}


# ------------------------------------------------------- 几何关系（规范 §3 第③步）
#
# ★★ 为什么必须有这一节（2026-10-02 规范核对发现的系统性缺口）：
#   《07 尺寸标注规范》§3 的标注顺序第③步是「**先加几何关系**（水平、竖直、相切、
#   同心、中点、对称、重合）」，但技能里**只有 `fully_define` 这一个入口**，
#   而它是"官方自动补关系+尺寸"的黑盒，实测不收敛（見 §54）。
#   ⇒ 用 `polyline` 画的轮廓**一条关系都没有**：4 条边 = 4 个独立图元 = 16 自由度，
#     端点是靠**坐标巧合**才被认成一个轮廓的。后果实测（D:/out/review/probe_all.json）：
#       9 件零件的草图**全部** `UnderConstrained(2)` —— 规范要的"完全定义"一件没到；
#       套筒的尺寸被逐段标成 `13 / 3.5 / 13 / 3.5`（重复值 ⇒ 冗余）。
#   ⇒ 补上这个 op，§3 第③步才算真正可执行。
#
# 约束字符串来自 `IModelDoc2.SketchAddConstraints` 的官方 Remarks（已核）：
#   sgCOINCIDENT / sgHORIZONTAL(2D) / sgVERTICAL(2D) / sgPARALLEL / sgPERPENDICULAR
#   / sgTANGENT / sgCONCENTRIC / sgCOLINEAR / sgSYMMETRIC / sgATMIDDLE / sgFIXED …
#   2D 草图另有 `sgHORIZONTAL2D` / `sgVERTICAL2D` 变体 —— 两个都试，哪个成算哪个。

def _sketch_point_objs(sw) -> list:
    """取当前草图的**全部点对象**（`ISketch.GetSketchPoints2`）。

    ★ 必须拿**对象**而不是按坐标选：重合的两个端点坐标完全相同，
      `SelectByID2("", "EXTSKETCHPOINT", x, y, z, …)` 分不出是哪一个，
      连调两次也只会选中同一个（选择集去重）。用对象级 `Select4` 才能分别选上。
    """
    sk = _isketch_of(sw)
    if sk is None:
        return []
    pts, _ = _call(member, sk, "GetSketchPoints2")
    return [p for p in list(pts or []) if p is not None]


def _add_coincident(sw, tol: float = 1e-6) -> tuple:
    """把**坐标重合的端点**两两加 `sgCOINCIDENT`。返回 (成功组数, 明细)。"""
    groups: dict = {}
    for p in _sketch_point_objs(sw):
        c = _pt_mm(p)
        if not c:
            continue
        groups.setdefault((round(c[0] / tol), round(c[1] / tol)), []).append((c, p))
    n, info = 0, []
    for key, g in groups.items():
        if len(g) < 2:
            continue
        sw.clear_selection()
        sel = 0
        for _c, p in g:
            _call(member, typed(p, "ISketchPoint"), "Select4", True, None)
            sel = sw.selected_count()
        tag = f"({g[0][0][0]:g}, {g[0][0][1]:g})"
        if sel < 2:
            info.append(f"{tag} 处有 {len(g)} 个点，但只选上 {sel} 个 ⇒ 跳过")
            continue
        _call(member, sw.model, "SketchAddConstraints", "sgCOINCIDENT")
        n += 1
        info.append(f"{tag}: 合并 {sel} 个重合点")
    sw.clear_selection()
    return n, info


def _add_hv(sw, segs: list) -> tuple:
    """给**非构造**的直线段按实际方向补 `sgHORIZONTAL/VERTICAL`。返回 (水平数, 垂直数, 明细)。"""
    h = v = 0
    info: list = []
    for s in segs:
        if _is_construction(s):
            continue
        g = _seg_geom(sw, s)
        if g.get("kind") != "line":
            continue
        a, b = g.get("start_mm"), g.get("end_mm")
        if not a or not b:
            continue
        dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
        if max(dx, dy) < 1e-9:
            continue                       # 零长度段，跳过
        names = ("sgHORIZONTAL2D", "sgHORIZONTAL") if dy <= dx else \
                ("sgVERTICAL2D", "sgVERTICAL")
        sw.clear_selection()
        ok, err = _select_segment_ex(s, append=False)
        if sw.selected_count() != 1:
            info.append(f"段({a[0]:g},{a[1]:g})→({b[0]:g},{b[1]:g}) 选不中"
                        f"（Select4→{ok} err={err}）⇒ 跳过")
            continue
        done = None
        for nm in names:
            _call(member, sw.model, "SketchAddConstraints", nm)
            done = nm
        if dy <= dx:
            h += 1
        else:
            v += 1
        info.append(f"段({a[0]:g},{a[1]:g})→({b[0]:g},{b[1]:g}) 加 {done}")
    sw.clear_selection()
    return h, v, info


@op("sk_relation", "加几何关系（草图）", "sketch",
    "★★ 《07 尺寸标注规范》§3 第③步「**先加几何关系，再标尺寸**」的执行 op。\n"
    "   规范的原话：「几何关系是**免费**锁定自由度的；先加关系可以大幅减少所需尺寸，\n"
    "   从根源上避免重复标注。」\n"
    "★ 为什么需要它：`polyline` 只调 `CreateLine`，画出来的轮廓**一条关系都没有** ——\n"
    "  端点只是坐标巧合。实测（2026-10-02）：这样画的 4 边形标多少尺寸都到不了\n"
    "  「完全定义」（9 件零件草图全是 `UnderConstrained(2)`）。\n"
    "★ `rel` 三种模式：\n"
    "    · `all`（默认）—— 先补重合、再补水平/竖直，一次到位；\n"
    "    · `coincident` —— 只把坐标重合的端点两两 `sgCOINCIDENT`；\n"
    "    · `h_v` —— 只给非构造直线按实际方向补 `sgHORIZONTAL/VERTICAL`；\n"
    "    · `fix` —— 把 `point` 指定的那个点 `sgFIXED`（★ 只锁**点**，别锁图元 ——\n"
    "      §54 实测：锁整个圆会变 `OverConstrained` 且尺寸被锁死）。\n"
    "★ 只写关系、不写尺寸：标尺寸仍走 `dim_auto` / `dimension`（规范要求的顺序）。\n"
    "★ 返回 `status_after` —— 关系加完草图到哪一态，用它判断还差几个定位自由度。",
    api=["ISketch.GetSketchPoints2", "ISketchSegment.Select4",
         "ISketchPoint.Select4", "IModelDoc2.SketchAddConstraints"],
    params={"rel": {"type": "str", "required": False, "default": "all",
                    "desc": "all / coincident / h_v / fix"},
            "tol": {"type": "float", "unit": "mm", "required": False, "default": 1e-6,
                    "desc": "判定两点重合的容差"},
            "point": {"type": "list", "required": False,
                      "desc": "rel=fix 时要点住的点坐标 [x, y]（mm）；不给则取离原点最近的点"}},
    requires=("sketch_open",), verified=True)
def op_sk_relation(sw, step, ctx):
    mode = str(_p(step, ctx, "rel", "all") or "all").strip().lower()
    tol = float(_p(step, ctx, "tol", 1e-6) or 1e-6)
    out: dict = {"mode": mode}
    out["status_before"] = _constrained_status(sw)
    out["status_before_meaning"] = CONSTRAINED_STATUS.get(out["status_before"])
    segs = [s for s in _active_sketch_segments(sw) if s is not None]
    if not segs:
        raise SwError("当前草图里没有图元 —— 先画轮廓再 sk_relation。")
    added = {"coincident": 0, "horizontal": 0, "vertical": 0, "fixed": 0}
    detail: list = []
    if mode in ("all", "coincident", "重合"):
        n, info = _add_coincident(sw, tol)
        added["coincident"] = n
        detail.extend(info)
    if mode in ("all", "h_v", "hv", "水平竖直", "horizontal"):
        h, v, info = _add_hv(sw, segs)
        added["horizontal"] = h
        added["vertical"] = v
        detail.extend(info)
    if mode in ("fix", "fixed", "锁定"):
        want = _p(step, ctx, "point")
        cand = None
        best = None
        for p in _sketch_point_objs(sw):
            c = _pt_mm(p)
            if not c:
                continue
            if want and isinstance(want, (list, tuple)) and len(want) >= 2:
                d = abs(c[0] - float(want[0])) + abs(c[1] - float(want[1]))
            else:
                d = abs(c[0]) + abs(c[1])
            if best is None or d < best:
                best, cand = d, (c, p)
        if cand is None:
            detail.append("fix：没找到可锁的点")
        else:
            c, p = cand
            sw.clear_selection()
            _call(member, typed(p, "ISketchPoint"), "Select4", False, None)
            sel = sw.selected_count()
            if sel == 1:
                _call(member, sw.model, "SketchAddConstraints", "sgFIXED")
                added["fixed"] = 1
                detail.append(f"锁点 ({c[0]:g}, {c[1]:g})（偏差 {best:.4g}）")
            else:
                detail.append(f"fix：点 ({c[0]:g}, {c[1]:g}) 只选上 {sel} 个 ⇒ 跳过")
        sw.clear_selection()
    out["added"] = added
    out["detail"] = detail
    out["status_after"] = _constrained_status(sw)
    out["status_after_meaning"] = CONSTRAINED_STATUS.get(out["status_after"])
    out["fully_defined"] = out["status_after"] == 3
    if out["status_after"] == 2:
        out["hint"] = ("仍欠定义 ⇒ 还差**定位自由度**：型面相对原点/中心线的位置没有尺寸锁住。"
                       "规范 §5 #2 要求「每个轮廓都有定位尺寸」，这一步归 `dim_auto`/`dimension`。")
    return out



# ============================================================ 螺旋 / 扫掠
#
# 这一组是**螺纹类零件**的基础设施 —— 丝杠、螺栓、蜗杆、螺旋槽都走同一条路：
#     在端面画一个与轴同径的圆 → helix（螺旋线）
#     → 在**过轴线且含径向+轴向**的平面上画牙型 → cut_swept（沿螺旋线扫掠切除）
#
# 为什么不做一个「一键螺纹」op：牙型（三角/梯形/圆弧/锯形）各行业差得远，
# 螺距、牙高、牙底宽、牙顶间隙的算法也不一样。把牙型交给调用方（模板里画），
# 比在 op 里写死一堆行业参数更好维护，也更容易接非标规格。


@op("helix", "螺旋线", "feature",
    "在**选中的圆**上生成螺旋线。调用前必须先选中一个圆（草图圆或圆形边线）。\n"
    "螺纹的典型用法：在轴端面画一个与轴**同径**的圆 → 选中它 → helix。\n"
    "定义方式二选一：pitch+height（螺纹最常用）或 height+revolution。\n"
    "★ 螺旋线**起点**决定了牙型草图要画在哪儿：默认起点在圆的 0° 位置（+X 方向）。",
    api=["IModelDoc2.InsertHelix"],
    params={
        "pitch": {"type": "float", "unit": "mm", "required": False, "desc": "螺距"},
        "height": {"type": "float", "unit": "mm", "required": False, "desc": "螺旋段长度"},
        "revolution": {"type": "float", "required": False, "desc": "圈数（与 height 配用）"},
        "clockwise": {"type": "bool", "required": False, "default": False},
        "reversed": {"type": "bool", "required": False, "default": False},
        "start_angle": {"type": "float", "unit": "deg", "required": False, "default": 0},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",),
    effect=("helix",),
    verified=True)
def op_helix(sw, step, ctx):
    C = sw.C
    pitch = _p(step, ctx, "pitch")
    height = _p(step, ctx, "height")
    rev = _p(step, ctx, "revolution")
    if height is None:
        raise SwError("螺旋线必须给 height（螺旋段长度）")
    if pitch is not None:
        hdef = C("swHelixDefinedByHeightAndPitch")
        h, p, rev = mm(float(height)), mm(float(pitch)), float(rev or 0)
    elif rev is not None:
        hdef = C("swHelixDefinedByHeightAndRevolution")
        h, p = mm(float(height)), 0.0
    else:
        raise SwError("螺旋线还需要 pitch（螺距）或 revolution（圈数）之一")

    if sw.selected_count() == 0 or _p(step, ctx, "from_sketch", True):
        # ★★ **草图编辑态内**建螺旋线（2026-10-02 弹簧垫圈实测踩出来的唯一可行路径）：
        #   模型态下**根本选不中草图里的圆边** —— 实测 7 种方式全灭
        #   （SelectByID2 空名+EDGE / 空名+SKETCHSEGMENT / 切点 / 45°点 / 换 mark=0 /
        #     按名字"圆1"/"Arc1"/"Sketch1"），返回 False、选中数 0，一次不差。
        #   ⇒ 办法是**别退出草图**：`circle` 画完后用 `ISketchSegment.Select4` 选中它，
        #     直接在草图里 `InsertHelix`（螺旋线会作为**顶层 Helix 特征**生成，特征树可见）。
        #   `from_sketch=false` 可退回"必须已有选择"的老行为。
        if ctx.get("sketch_open") and ctx.get("segments"):
            sw.clear_selection()
            for sg in ctx["segments"]:
                with contextlib.suppress(Exception):
                    typed(sg, "ISketchSegment").Select4(True, None)
    if sw.selected_count() == 0:
        raise SwError(
            "生成螺旋线前必须先选中一个圆 —— InsertHelix 只认当前选择，不接受坐标。\n"
            "  推荐顺序：select_plane(轴端面) → sketch_begin → circle(与轴同径)\n"
            "            → **直接在草图内 helix（不要先 sketch_end）**\n"
            "  ★ 模型态下 `select_face`/`SelectByID2` **选不中草图里的圆边**（实测 7 种全灭）。")
    # ★★ 草图内插螺旋线 ⇒ **SolidWorks 会把螺旋线"提取"成独立的 Helix 特征并把草图删掉**
    #   （实测：`sketch_begin → circle → helix` 之后，特征树里已经没有那个 ProfileFeature）。
    #   如果不把 ctx 里的草图状态同步成"已关闭"，后面计划里的 `sketch_end` / `sketch_begin`
    #   会**全部错位**：sketch_end 去关一个不存在的草图、sketch_begin 在已关闭的文档上开图 ——
    #   最终 `swept_boss` 拿到的是**上一张残留草图**，扫出个圆筒还以为成功了（§85）。
    if ctx.get("sketch_open"):
        ctx["sketch_open"] = False
        ctx["sketch_consumed_by_helix"] = True
    # ★ 别信 InsertHelix 的返回值：实测它**每次都回 None**，螺旋线却实实在在建出来了
    #   （特征树里躺着 Helix 特征）。和 Select4 一个脾气 —— 一律以**特征树**为判据。
    #   探针实测：5 种选择/参数组合全部"返回 None"，5 条螺旋线全部创建成功。
    before = {n for t, n in sw.feature_names() if t == "Helix"}
    member(sw.model, "InsertHelix",
           bool(_p(step, ctx, "reversed", False)),      # Reversed 反向
           bool(_p(step, ctx, "clockwise", False)),     # Clockwise 顺时针
           bool(_p(step, ctx, "tapered", False)),       # Tapered 锥形
           # ★★ `Outward` 原来硬编码 True（=向外/涡状），会让螺旋线的**实际半径大于**画出来的圆：
           #   弹簧垫圈实测 bbox X 12.753 vs 理论 12.1（多 0.65）⇒ 扫掠跟着偏。
           #   改成参数、默认 **False**（贴着画的圆走），要涡状线再显式开。
           bool(_p(step, ctx, "outward", False)),
           hdef, h, p, rev, 0.0,
           deg(float(_p(step, ctx, "start_angle", 0) or 0)))
    fresh = [n for t, n in sw.feature_names() if t == "Helix" and n not in before]
    if not fresh:
        raise SwError(
            "螺旋线创建失败 —— 特征树里没有新增 Helix 特征。常见原因：\n"
            "  · 没选中圆（InsertHelix 只认当前选择，不接受坐标）\n"
            "  · 螺距大于圆的周长\n"
            "  · height 与 pitch/revolution 对不上")
    name = _p(step, ctx, "name") or fresh[-1]
    if _p(step, ctx, "name"):
        hf = typed(sw.model.FeatureByName(fresh[-1]), "IFeature")
        with contextlib.suppress(Exception):
            hf.Name = _p(step, ctx, "name")
    ctx["last_helix"] = name
    ctx.setdefault("feature_names", []).append(name)
    return {"helix": name, "pitch": _p(step, ctx, "pitch"),
            "height": _p(step, ctx, "height"), "revolution": rev}


# 路径（螺旋线）在 SelectByID2 里的类型串，SolidWorks 各版本叫法不一，按序试。
_HELIX_TYPES = ("REFERENCECURVES", "BODYFEATURE", "SKETCH")


@op("cut_swept", "扫掠切除", "feature",
    "沿**路径**（螺旋线/曲线）扫掠**轮廓**（草图）做切除 —— 螺纹、油槽、螺旋槽都靠它。\n"
    "op 自己按 mark 选：**轮廓 mark=1、路径 mark=4**（SolidWorks 扫掠的固定约定）。\n"
    "失败会自动降级重试：换 PathAlign、开 BAdvancedSmoothing、改 UseAutoSelect。",
    api=["IFeatureManager.InsertCutSwept4"],
    params={
        "profile": {"type": "str", "required": False,
                    "desc": "轮廓草图名，不给就用刚画完的那张"},
        "path": {"type": "str", "required": False, "desc": "路径特征名（螺旋线），不给用刚建的"},
        "twist": {"type": "int", "required": False, "default": 0,
                  "desc": "swTwistControlType_e，0=沿路径"},
        "thin": {"type": "bool", "required": False, "default": False},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",),
    verified=True)
def op_cut_swept(sw, step, ctx):
    prof = _p(step, ctx, "profile") or ctx.get("last_sketch")
    path = _p(step, ctx, "path") or ctx.get("last_helix")
    if not prof:
        raise SwError("扫掠切除需要轮廓草图名（profile）")
    if not path:
        raise SwError("扫掠切除需要路径（path）—— 一般是先用 helix 建的螺旋线")
    fm = _fm(sw)
    tw = int(_p(step, ctx, "twist", 0) or 0)
    thin = bool(_p(step, ctx, "thin", False))
    vol0 = _volume(sw)

    # 候选：先按标准 mark 组合，再放宽让 SolidWorks 自己认。
    cands = [
        dict(pm=1, hm=4, auto=False, sm=False, tag="标准 mark 轮廓1/路径4"),
        dict(pm=1, hm=4, auto=True, sm=True, tag="+自动选择+高级平滑"),
        dict(pm=0, hm=4, auto=True, sm=True, tag="轮廓 mark0/路径 mark4+自动选择"),
    ]
    notes: list[str] = []
    feat = None
    used = ""
    for c in cands:
        sw.clear_selection()
        if not sw.select(prof, "sketch", mark=c["pm"]):
            raise SwError(f"选不中轮廓草图 '{prof}'")
        hit = False
        for t in _HELIX_TYPES:
            if sw.select(path, t, append=True, mark=c["hm"]):
                hit = True
                break
        if not hit:
            notes.append(f"{c['tag']}：选不中路径 '{path}'")
            continue
        tree0 = {n for _t, n in sw.feature_names()}
        with contextlib.suppress(Exception):
            fm.InsertCutSwept4(
                True,                       # 1  Propagate
                False,                      # 2  Alignment
                tw,                         # 3  TwistCtrlOption
                True,                       # 4  KeepTangency
                bool(c["sm"]),              # 5  BAdvancedSmoothing
                0, 0,                       # 6  StartMatchingType  7  EndMatchingType
                thin,                       # 8  IsThinBody
                0.0, 0.0,                   # 9  Thickness1  10 Thickness2
                0,                          # 11 ThinType
                0,                          # 12 PathAlign
                False,                      # 13 UseFeatScope
                bool(c["auto"]),            # 14 UseAutoSelect
                0.0,                        # 15 TwistAngle
                True,                       # 16 BMergeSmoothFaces
                True, False, False)         # 17 AssemblyFeatureScope
                                            # 18 AutoSelectComponents
                                            # 19 PropagateFeatureToParts
        sw.clear_selection()
        # ★ 同 InsertHelix：InsertCutSwept4 也常年回 None，别拿它当判据 ——
        #   以**特征树有没有新增特征**为准，再用体积变化确认真的咬到材料了。
        fresh = [n for _t, n in sw.feature_names() if n not in tree0]
        if not fresh:
            notes.append(f"{c['tag']}：特征树里没有新增特征（扫掠没生成）")
            continue
        f = typed(sw.model.FeatureByName(fresh[-1]), "IFeature")
        ec = 0
        with contextlib.suppress(Exception):
            ec = int(member(f, "GetErrorCode"))
        if ec:
            notes.append(f"{c['tag']}：特征重建失败 {ec}"
                         f"（{FEATURE_ERROR_NAMES.get(ec, '未知')}）")
            _drop_feature(sw, f)
            continue
        if vol0 is not None:
            vol1 = _volume(sw)
            if vol1 is not None and abs(vol1 - vol0) < 1e-12:
                notes.append(f"{c['tag']}：特征成功但**体积没变**（没切到材料）")
                _drop_feature(sw, f)
                continue
        feat, used = f, c["tag"]
        break

    if feat is None:
        detail = "".join(f"    · {x}\n" for x in notes)
        raise SwError(
            f"扫掠切除失败，{len(cands)} 个方案全部没成：\n" + detail
            + "  排查：① 轮廓草图有没有穿过路径的**起点**（扫掠要求轮廓在路径起点处）\n"
              "        ② 轮廓平面是否垂直于路径在该点的切向\n"
              "        ③ 螺旋线是否真的建出来了（feature_names 里看得到吗）")
    name = _p(step, ctx, "name") or str(member(feat, "Name"))
    if _p(step, ctx, "name"):
        with contextlib.suppress(Exception):
            feat.Name = _p(step, ctx, "name")
    ctx.setdefault("feature_names", []).append(name)
    ctx["last_feature"] = name
    ctx["last_cut"] = name
    return {"feature": name, "profile": prof, "path": path,
            "variant": used, "notes": notes or None}


#: 特征类型名（`IFeature.GetTypeName2()`）→ **选择类型**（`SelectByID2` 的第 2 参）。
#:
#: ★★★ **必须真机核对白名单**（2026-10-11 连踩两次）：
#:   ① `InsertCurveFile`（「通过 XYZ 点的曲线」）建出来的曲线类型名是 **`CurveInFile`**，
#:      不在 `Curve`/`RefCurve` 里 ⇒ 白名单漏了它，蓝线照样盖满曲面；
#:   ② **`IFeature.Visible` 是只读的** —— 设值抛
#:      `AttributeError: Property 'IFeature.Visible' can not be set.`
#:      （对 CurveInFile 和 ProfileFeature **都**如此）⇒ 靠 `Visible=False` 隐藏
#:      **一条都没藏到**，而日志照样打印「已隐藏」（**假绿**：只证明函数跑过）。
#:   ⇒ 正解 = **选中 + `BlankRefGeom` / `BlankSketch`**（tlb 里都是 0 参 void，
#:     作用于**当前选择集**）。判据必须落在「**藏了几个**」。
CURVE_TYPES = ("CurveInFile", "ImportedCurve", "RefCurve", "CompositeCurve",
               "Curve", "Helix", "Spline", "SplineOnSurface")
SKETCH_TYPES = ("ProfileFeature", "3DProfileFeature", "Sketch")
#: 兼容旧名（有别的脚本/文档按这个名字引用）
CONSTRUCTION_TYPES = SKETCH_TYPES + CURVE_TYPES


def hide_construction_geometry(sw) -> dict:
    """隐藏**构造用**几何（草图 / 参考曲线 / 螺旋线），只留实体。

    ★ 交付与自查截图前**必须**调：草图 —— 尤其 3D 草图（放样的截面 / 引导线）——
      是**蓝色线条**，会把实体盖住，看图的人分不清「模型边界」和「画图的辅助线」。
      2026-10-11 实测：CRH380CL 车头 S5 的截图被 9 个截面 + 2 条引导线的蓝线糊满，
      肉眼看不出风挡 / 侧窗的开口形状。

    ★★ 两条正解（真机双证，见 `_diag/exp_hide3.py`）：
      · 曲线：`SelectByID2(名, "REFERENCECURVES", …)` 全选 → `BlankRefGeom()`
      · 草图：`SelectByID2(名, "SKETCH", …)`          全选 → `BlankSketch()`
      ★ **别用 `IFeature.Visible = False`** —— 只读，静默无效。
    """
    m = sw.model
    groups = [("REFERENCECURVES", CURVE_TYPES, "BlankRefGeom"),
              ("SKETCH", SKETCH_TYPES, "BlankSketch")]
    hidden: list[str] = []
    detail: dict[str, int] = {}
    for stype, types, blank in groups:
        items = [n for t, n in sw.feature_names() if t in types]
        if not items:
            continue
        sw.clear_selection()
        picked = 0
        for n in items:
            with contextlib.suppress(Exception):
                if sw.ext.SelectByID2(n, stype, 0, 0, 0, True, 0, None, 0):
                    picked += 1
        # ★ 判据：**选择集里真的有了**才去 Blank（Blank 是 0 参、作用于选择集，
        #   选不中就会变成「静默什么都不做」）
        sel = 0
        with contextlib.suppress(Exception):
            sel = sw.selmgr().GetSelectedObjectCount2(-1)
        if sel <= 0:
            detail[stype] = 0
            continue
        with contextlib.suppress(Exception):
            member(m, blank)
        detail[stype] = sel
        hidden.extend(items)
    sw.clear_selection()
    return {"refs_hidden": True, "groups": detail, "features_hidden": hidden}


@op("hide_refs", "隐藏参考几何与草图", "doc",
    "隐藏**草图 / 参考曲线 / 螺旋线**这类构造几何，只留实体。\n"
    "截图和交付前调一下 —— 否则螺旋线（几百圈的蓝色曲线）会盖满螺纹、\n"
    "放样的截面/引导线（`InsertCurveFile` 曲线）会糊满曲面，截图里根本看不清几何。\n"
    "可见性会跟着文档一起保存，所以这一步是「永久」的。\n"
    "★★ **别用 `IFeature.Visible=False`**（只读，静默无效）—— 正解是\n"
    "  「按类型全选 + `BlankRefGeom`(曲线) / `BlankSketch`(草图)」，见\n"
    "  `hide_construction_geometry()` 的注释与 `references/30-pitfalls.md` §137④。\n"
    "★ 判据落在「**藏了几个**」：结果里的 `groups` 若为空 = 白名单没匹配上（假绿）。",
    api=["IModelDocExtension.SelectByID2", "IModelDoc2.BlankRefGeom",
         "IModelDoc2.BlankSketch", "ISelectionMgr.GetSelectedObjectCount2"],
    requires=("doc_open",),
    verified=True)
def op_hide_refs(sw, step, ctx):
    return hide_construction_geometry(sw)


# ============================================================ 装配体（D1 基础）
#
# 三步法落地（同 M1 零件层）：① 抽签名 → ② 写 op（verified 先 False）→ ③ 真机双证。
#
# 签名**全部**来自 `sw_apisig.py --tlb`（2026-10-02 真机抽取，别照旧文档写）：
#   IAssemblyDoc.AddComponent5(CompName, ConfigOption, NewConfigName,
#                              UseConfigForPartReferences, ExistingConfigName, X, Y, Z)
#   IAssemblyDoc.CreateMateData(Type) -> IMateFeatureData
#   IAssemblyDoc.CreateMate(MateData) -> IFeature
#   IAssemblyDoc.FixComponent() / UnfixComponent()      ← **零参数，作用于当前选择**
#   IAssemblyDoc.GetComponents(ToplevelOnly) -> Variant / GetComponentCount(ToplevelOnly)
#   ISelectionMgr.GetSelectedObject6(Index, Mark) / GetSelectedObjectCount2(Mark)
#   IMateFeatureData 各族：EntitiesToMate / MateAlignment / Distance / Angle / ...
#
# ★★ 四条**别照旧文档/照记忆写代码**的实测结论（详见 references/30-pitfalls.md §94）：
#
# ① `AddMate` / `AddMate2` / `AddMate3` / `AddMate4` / `AddMate5` **全部已废弃**，
#    官方原文：`Obsolete. Superseded by IAssemblyDoc::CreateMate.`
#    ⇒ 本文件**一个都不用**，只走 `CreateMateData` + `CreateMate`。
#
# ② `swMateAlign_e` 里流传最广的三个名字 `swAlignSAME` / `swAlignAGAINST` /
#    `swAlignNONE` **已被官方标 Obsolete 并写明 "Do not use"** ——
#    枚举页脚注还给了一句 `These values have been changed to correct improper
#    mate alignment mapping.`（映射在某版被改过，旧名就是那次改动的牺牲品）。
#    正确值是 `swMateAlignALIGNED(0)` / `swMateAlignANTI_ALIGNED(1)` /
#    `swMateAlignCLOSEST(2)`。⇒ 本文件只认这三个名字。
#
# ③ ★★ `CreateMate` 的 Remarks 里，**只有带星号的配合类型支持「预选实体」**：
#      支持预选（有 *）：COINCIDENT / CONCENTRIC / PARALLEL / PERPENDICULAR / TANGENT /
#                       LOCK / GEAR / CAMFOLLOWER / HINGE / LINEARCOUPLER /
#                       RACKPINION / SCREW / SLOT / UNIVERSALJOINT
#      **不支持**：      DISTANCE / ANGLE / SYMMETRIC / PROFILECENTER / WIDTH
#    ⇒ **距离与角度配合必须显式写 `data.EntitiesToMate`**，光靠"先选中再 CreateMate"
#      对这两种类型**一定建不出来**（而且多半是静默的）。
#    本文件的做法：**一律显式设 EntitiesToMate**（预选照做，但不当唯一凭据）——
#    官方原文是 "and/or pre-select entities"，对支持预选的类型不冲突，
#    对不支持的类型是唯一的活路。**一条代码路径覆盖全部类型，少一个分支就少一个假绿灯**。
#
# ④ `FixComponent()` / `UnfixComponent()` **零参数**，作用于**当前选择集**
#    ⇒ 必须先选中组件；而且`IAssemblyDoc.FixComponent` 不会返回成败
#    ⇒ 判据只能是 **`IComponent2.IsFixed` 真的变了**（见 `_components_fixed`）。
#
# ⚠ 另一条**不是 API、是 SolidWorks 行为**的坑：**第一个插入的组件默认被固定**。
#   所以"插完就能配合"是假象 —— 第一件不用再 Fix，但要固定第二件/后面的，
#   否则每加一个配合，SolidWorks 都可能挪动那些**没被固定**的组件。

#: 支持的配合类型 → 常量名 / 数据接口名（接口名用来做官方的 "cast to mate-specific"）。
ASM_MATES: dict[str, dict] = {
    "coincident":     {"const": "swMateCOINCIDENT",    "iface": "ICoincidentMateFeatureData"},
    "concentric":     {"const": "swMateCONCENTRIC",    "iface": "IConcentricMateFeatureData"},
    "parallel":       {"const": "swMatePARALLEL",      "iface": "IParallelMateFeatureData"},
    "perpendicular":  {"const": "swMatePERPENDICULAR", "iface": "IPerpendicularMateFeatureData"},
    "tangent":        {"const": "swMateTANGENT",       "iface": "ITangentMateFeatureData"},
    "distance":       {"const": "swMateDISTANCE",      "iface": "IDistanceMateFeatureData"},
    "angle":          {"const": "swMateANGLE",         "iface": "IAngleMateFeatureData"},
    "lock":           {"const": "swMateLOCK",          "iface": "ILockMateFeatureData"},
    "symmetric":      {"const": "swMateSYMMETRIC",     "iface": "ISymmetricMateFeatureData"},
    "profile_center": {"const": "swMatePROFILECENTER", "iface": "IProfileCenterMateFeatureData"},
    "width":          {"const": "swMateWIDTH",         "iface": "IWidthMateFeatureData"},
}

#: 配合对齐三态（★ 只用这三个名字 —— 旧名 Obsolete，见头注②）。
ASM_MATE_ALIGN = {
    "aligned": "swMateAlignALIGNED",
    "anti_aligned": "swMateAlignANTI_ALIGNED",
    "closest": "swMateAlignCLOSEST",
}

#: ★ **不支持预选实体**的配合类型 ⇒ 不显式设 EntitiesToMate 一定建不出来（见头注③）。
ASM_MATES_NO_PRESELECT = frozenset({"distance", "angle", "symmetric",
                                    "profile_center", "width"})


@op("new_assembly", "新建装配体", "asm",
    "新建一个空装配体 —— 后续 `insert_component` 往里放零件。\n"
    "模板优先用 `sw_env.find_templates()` 找到的 `.asmdot`；找不到时**不报错**，\n"
    "退回零参数的 `ISldWorks.NewAssembly()`（用 SolidWorks 当前默认装配体模板）——\n"
    "因为 `.asmdot` 不一定落在模板搜索目录里，\"找不到就罢工\"会让整条装配体链路\n"
    "在别人的机器上直接不可用。\n"
    "★ 建完会**核一遍文档类型**（GetType == swDocASSEMBLY），模板给错当场炸。",
    api=["ISldWorks.NewDocument", "ISldWorks.NewAssembly"],
    params={"template": {"type": "str", "required": False,
                         "desc": "装配体模板 .asmdot；不给则自动查找"}},
    effect=("doc_open", "asm_open"), verified=True)
def op_new_assembly(sw, step, ctx):
    m = sw.new_assembly(_p(step, ctx, "template"))
    if not sw.is_assembly:
        t = None
        with contextlib.suppress(Exception):
            t = member(m, "GetType")
        raise SwError(f"新建出来的**不是装配体**（GetType={t}，期望 "
                      f"{sw.C('swDocASSEMBLY')}）—— 模板大概率给错了")
    ctx["feature_names"] = []
    return {"title": member(m, "GetTitle"), "is_assembly": True}


@op("insert_component", "插入组件", "asm",
    "把一个已有 `.SLDPRT`/`.SLDASM` 放到装配体里的 (x, y, z) 处。\n"
    "★ 坐标是**全局毫米**（op 内部换算成 API 要的米）；零件的**原点**会落在该点。\n"
    "★★ 判据 = **组件数真的涨了**，不是「AddComponent5 没报错」——\n"
    "   项目铁律第一条：返回值会说谎（见 references/30-pitfalls.md §1）。\n"
    "★ `config` 给零件配置名；不给就用零件**最后保存**的配置（ConfigOption=0）。\n"
    "★ 注意 SolidWorks 行为：**第一个插入的组件默认被固定**（IsFixed=True）。",
    api=["IAssemblyDoc.AddComponent5"],
    params={"path": {"type": "str", "desc": "零件/子装配体文件路径"},
            "x": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "y": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "z": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "config": {"type": "str", "required": False, "desc": "零件配置名"},
            "name": {"type": "str", "required": False,
                     "desc": "插入后把组件改名成它（便于后续按名选中）"}},
    requires=("asm_open",), effect=("has_component",), verified=True)
def op_insert_component(sw, step, ctx):
    path = str(_p(step, ctx, "path") or "")
    if not path:
        raise SwError("insert_component 必须给 path")
    if not Path(path).exists():
        raise SwError(f"要插入的文件不存在: {path}")
    # ★★ 路径必须转成反斜杠再喂 COM（2026-10-02 真机确诊，见 sw_connect.win_path）：
    #    `AddComponent5` 喂 `D:/out/x.SLDPRT` 会**静默返回 None**（组件不进去、
    #    不抛异常、无错误码），喂 `D:\out\x.SLDPRT` 才成功。
    #    对比 `open_doc`/`save_as` 因为走 `str(Path(...))` 天然是反斜杠所以没暴露。
    com_path = sw_connect.win_path(path)
    xyz = [mm(float(_p(step, ctx, k, 0) or 0)) for k in ("x", "y", "z")]
    cfg = str(_p(step, ctx, "config") or "")
    opt = _C(sw, "swAddComponentConfigOptions_CurrentSelectedConfig", 0)
    asm = sw.asm
    n0 = sw.component_count()
    names0 = {c.get("name") for c in sw.components()}
    # ★★ 2026-10-03 真机：`AddComponent5` 会**瞬时失败**（不抛异常、无错误码、
    #   组件数不涨），同一会话同一零件时好时坏。判据既然是"组件数涨了"，
    #   那没涨就**重试**（间隔 COM_RETRY_S），仍不涨才报错。
    #   `attempts` 会写进返回值 ⇒ 抖动次数在报告里看得见，不会被悄悄吞掉。
    _comp, err, attempts = None, None, 0
    n1 = n0
    for attempts in range(1, COM_RETRY + 1):
        _comp, err = _call(member, asm, "AddComponent5", com_path, opt, "",
                           False, cfg, *xyz)
        n1 = sw.component_count()
        if n1 > n0:
            break
        with contextlib.suppress(Exception):
            sw.clear_selection()
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)
    # ★★ 判据：组件数必须涨（返回值/异常都只是线索）
    if n1 <= n0:
        raise SwError(
            f"AddComponent5 没把组件插进去（组件数 {n0} → {n1}，**已重试 "
            f"{attempts} 次**）。\n"
            f"  路径  : {com_path}\n"
            f"  配置  : {cfg or '(零件最后保存的配置)'}\n"
            f"  落点  : {[round(v * 1000, 3) for v in xyz]} mm\n"
            f"  排查  : ① 路径分隔符 —— 必须反斜杠（COM 不认正斜杠，且**静默失败**）；"
            f"② 文件是不是被 SolidWorks 锁着（~$ 锁文件）；"
            f"③ 路径里的非 ASCII 字符；④ 零件是不是有报错/未重建；"
            f"⑤ ★ **重试 {attempts} 次都失败** ⇒ 多半不是本 op 的问题，"
            f"是 SolidWorks COM 层的瞬时/会话故障 —— 先跑 "
            f"`scripts/sw_session_check.py` 确认会话健康"
            + (f"\n  COM 报错：{err}" if err else ""))
    fresh = [c for c in sw.components() if c.get("name") not in names0]
    rec = fresh[-1] if fresh else {}
    want = _p(step, ctx, "name")
    orig = rec.get("name")
    renamed_from = None
    if want:
        # ★ 先留住原名再改 —— 以前这里写的是 `rec.get("name") if not want else None`，
        #   即**只有没改名时**才填 `renamed_from`，语义正好反了（改完名就再也看不到原名）。
        # ★★ `_rename_component` 回的是**实际全名**（带实例号后缀，如 `地基-1`）——
        #   那是后续「按名选中组件」唯一能用的名字（光基名 `地基` 选不中，实测）。
        actual = _rename_component(sw, orig, str(want))
        renamed_from = orig
        rec["name"] = actual
    return {"component": rec.get("name"), "path": rec.get("path") or path,
            "fixed": rec.get("fixed"), "config": rec.get("config"),
            "component_count": n1, "renamed_from": renamed_from,
            # ★ 抖动次数透明化：>1 说明 SolidWorks COM 层失败过、重试才成
            #   （2026-10-03 真机：同一零件同一会话时好时坏）。不记这一条，
            #   "重试"就会变成一个看不见的、可能掩盖真实问题的动作。
            "attempts": attempts}


def _rename_component(sw, old: str | None, new: str) -> str:
    """把某个组件改名，**读回真名确认**（不信赋值语句）。返回**实际生效的全名**。

    ★★ 三条官方/实测硬约束（`IComponent2::Name2`，2026-10-02 真机验证）：

        ① **组件必须处于「选中」状态** —— 否则赋值静默无效；
        ② **`swExtRefUpdateCompNames` 必须是 false** —— 为 true 时改名**直接失败**。
           本机实测该开关**默认 True** ⇒ 不改它就是"赋值不报错、读回还是旧名"。
           修法 = `temp_pref` 临时关掉再还原。
        ③ ★★ **实例号后缀是本件固有属性，改名只换「基名」，后缀不动**：
           实测 `box-1` --改名"地基"--> **`地基-1`** --再改名"上块"--> **`上块-1`**；
           插第二个同款零件得到 `box-2`（后缀按**零件实例**递增，跟改名无关）。
           ⇒ 断言口径 = 读回的名字应当是 `<新基名>-<实例号>` 而不是光秃秃的 `<新基名>`。
             以前断言写成 `got != new` ⇒ **把成功的改名判成失败**（真机踩到）。

    ★ 改名失败时**必须报错**（不能静默）：组件名是后续「按名选中」的唯一抓手，
      名字没改掉 ⇒ 后面所有按名的操作都会找不到对象 —— 越早炸越好。
    """
    if not old:
        raise SwError("拿不到刚插入的组件名，无法改名")
    objs = {}
    for c in sw.component_objects():
        nm = None
        with contextlib.suppress(Exception):
            nm = member(c, "Name2") or member(c, "Name")
        if nm:
            objs[nm] = c
    c = objs.get(old)
    if c is None:
        raise SwError(f"改名时找不到组件 '{old}'；当前有 {sorted(objs)}")

    # ① 选中该组件（官方要求）。用 sw.select_component —— 它的判据是**选中数**，
    #    因为 SelectByID2 对组件会**谎报 False**（见 sw_connect.select_component）。
    if not sw.select_component(old):
        raise SwError(f"选中组件 '{old}' 失败（选中数 0）")

    err = None
    try:
        # ② swExtRefUpdateCompNames 为 true 时改名**必失败** ⇒ 临时关掉，读完还原。
        with sw.temp_pref(("swExtRefUpdateCompNames", False)):
            c.Name2 = new
    except Exception as e:                                    # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    got = None
    with contextlib.suppress(Exception):
        got = member(c, "Name2")
    # ③ 实例号后缀由 SolidWorks 维护：接受 `<new>` 或 `<new>-<数字>`。
    base_ok = got is not None and (
        str(got) == new or re.fullmatch(re.escape(new) + r"-\d+", str(got)) is not None)
    if not base_ok:
        raise SwError(f"组件改名没生效（期望 '{new}' 或 '{new}-<实例号>'，读回 {got!r}）"
                      f"；排查：① 组件是否选中；② swExtRefUpdateCompNames 是否被别处改回 True"
                      + (f"；COM 报错：{err}" if err else ""))
    return got


def _feat_typed_name(feat) -> tuple[str, str]:
    """容错读一个特征的 (类型, 名字)。

    ★ 面特征（COM 类名 `_FxFeat`）与退化对象**没有** Name/GetTypeName2 是正常情况
      （§123 ⇒ §v1.4.42 的老坑），取不到就返回空串，绝不上抛。
    """
    t = nm = ""
    with contextlib.suppress(Exception):
        t = str(member(feat, "GetTypeName2") or "")
    with contextlib.suppress(Exception):
        nm = str(member(feat, "Name") or "")
    return t, nm


def _find_feature_objs(sw, name: str, only_mates: bool = False) -> list[tuple]:
    """按名字找特征对象，返回 `[(IFeature, 类型, 名字, 深度, 是否在配合文件夹内)]`。

    ★ 为什么自己写一份遍历而不用 `feature_tree()`：这里**只能**下探「配合」文件夹
      （配合恰好只在顶层 MateGroup 里），**绝不能**递归进 58 个组件的子特征 ——
      那是 §121 记录的热路径毒药（数千节点 ×3 次 COM）。
    """
    hits: list[tuple] = []

    def _scan_mate_folder(folder, depth: int, in_mate: bool) -> None:
        c = typed(member(folder, "GetFirstSubFeature"), "IFeature")
        while c is not None:
            t, nm = _feat_typed_name(c)
            if nm == name:
                hits.append((c, t, nm, depth, in_mate or t in sw.MATE_GROUP_TYPES))
            if t in sw.MATE_GROUP_TYPES:                # 嵌套「配合」子文件夹
                _scan_mate_folder(c, depth + 1, True)
            with contextlib.suppress(Exception):
                c = typed(member(c, "GetNextSubFeature"), "IFeature")
                continue
            c = None

    f = typed(member(sw.model, "FirstFeature"), "IFeature")
    while f is not None:
        t, nm = _feat_typed_name(f)
        if not only_mates and nm == name:
            hits.append((f, t, nm, 0, False))
        if t in sw.MATE_GROUP_TYPES:
            _scan_mate_folder(f, 1, True)
        with contextlib.suppress(Exception):
            f = typed(member(f, "GetNextFeature"), "IFeature")
            continue
        f = None
    return hits


@op("delete_feature", "删除特征 / 配合（按名）", "asm",
    "按名字把一个特征（或一条**配合**）从特征树里**删掉**。\n"
    "\n"
    "★ 为什么必须有这个 op：\n"
    "  · **撤销一条「把组件拖走了」的配合**。装配复刻的现场：一条 coincident/同心\n"
    "    配合『建成功了』（`CreateMate` 有返回）但把已验证摆位的组件**转走 30mm**\n"
    "    （§120① 选错面 ⇒ 拖走组件）。这条配合留着就是错的 —— 必须删掉，\n"
    "    并把组件摆回去（配 `set_component_transform` 重放）。\n"
    "  · 单纯把 `save_as` 前的站位改回来**没用**：配合还在，重建（rebuild）时\n"
    "    求解器会再拖一次。**要先删配合，再摆位。**\n"
    "\n"
    "★ 判据（返回值会说谎，§五条铁律①）：**删完重新遍历特征树，名字必须真的没了**；\n"
    "  同时回报删完的配合总数（浅遍历，只数顶层「配合」文件夹）。\n"
    "\n"
    "★★ 只下探「配合」文件夹，不下探组件（§121 热路径毒药）。\n"
    "\n"
    "★ 命名冲突：配合名可能与特征名（草图/拉伸）重名 ⇒ 命中多个直接报错，\n"
    "  用 `kind='mate'` 限定只在配合文件夹里找。",
    api=["IFeature.GetTypeName2", "IFeature.Select2", "IModelDoc2.EditDelete"],
    params={"name": {"type": "str", "required": True, "desc": "要删的特征/配合名（如 '重合31'）"},
            "kind": {"type": "str", "required": False, "default": "any",
                     "desc": "any | mate（只在配合文件夹里找）| feature（只在顶层找）"}},
    requires=("doc_open",), effect=(), verified=True)
def op_delete_feature(sw, step, ctx):
    name = str(_p(step, ctx, "name") or "").strip()
    if not name:
        raise SwError("delete_feature 必须给 name")
    kind = str(_p(step, ctx, "kind") or "any").lower()
    if kind not in ("any", "mate", "feature"):
        raise SwError(f"kind 只认 any/mate/feature，实得 {kind!r}")

    hits = _find_feature_objs(sw, name, only_mates=(kind == "mate"))
    if kind == "feature":
        hits = [h for h in hits if not h[4]]
    if not hits:
        mate_names = [f["name"] for f in sw.mate_features()][:8]
        raise SwError(f"特征树里找不到名为 '{name}' 的对象（kind={kind}）。"
                      f"配合名前 8 个：{mate_names} —— 配合在顶层「配合」文件夹里，"
                      f"注意 `CreateMate` 回的**是特征名**（如 '重合31'），不是计划里的 id")
    if len(hits) > 1:
        raise SwError(f"'{name}' 有 {len(hits)} 个命中（深度 {[h[3] for h in hits]}，"
                      f"类型 {[h[1] for h in hits]}）—— 用 kind='mate'/'feature' 限定")

    feat, typ, nm, _depth, in_mate = hits[0]
    sw.clear_selection()
    sel_ok = False
    with contextlib.suppress(Exception):
        sel_ok = bool(feat.Select2(False, 0))
    if not sel_ok:                                       # 退路：按类型名走 SelectByID2
        for t in (("MATE",) if (in_mate or kind == "mate")
                  else ("BODYFEATURE", "SKETCH", "REFPLANE")):
            if sw.select(name, t):
                sel_ok = True
                break
    if not sel_ok:
        raise SwError(f"选中 '{name}' 失败（类型 {typ or '?'}）⇒ 不敢删。"
                      f"排查：① 该特征是否被压缩；② 是否在当前激活文档里")
    member(sw.model, "EditDelete")
    sw.clear_selection()

    left = _find_feature_objs(sw, name, only_mates=(kind == "mate"))
    if kind == "feature":
        left = [h for h in left if not h[4]]
    if left:
        raise SwError(f"`EditDelete` 之后 '{name}' **还在树里**（还剩 {len(left)} 个）"
                      f"—— 删除没生效（可能被压缩/只读/被别的特征引用）")
    return {"name": name, "feature_type": typ, "in_mate_folder": in_mate,
            "deleted": True, "mates_left": len(sw.mate_features())}


@op("rename_component", "组件改名（独立 op）", "asm",
    "把装配体里某个组件**改名**（同名多实例场景的唯一区分手段）。\n"
    "\n"
    "★ 为什么必须有这个 op（2026-10-05 真机结论）：\n"
    "  · 装配一批**同一个零件文件的多个实例**（6 根螺栓 / 6 个螺母）时，\n"
    "    `add_mate` 的实体选择器要靠 `component` 限定组件（否则紧固件与孔壁\n"
    "    只差 0.5mm，甚至螺母内孔 Ø8 与螺栓杆 Ø8 **完全同半径** ⇒ 静默选错面）；\n"
    "  · 而按**零件路径**限定时，同一路径会命中 N 个组件 ⇒ 解析报「有歧义」；\n"
    "  · 按**名字**限定是唯一出路 ⇒ 必须先把每个实例改成**唯一名**。\n"
    "\n"
    "★★ 时序硬约束：`SetTransformAndSolve3` 会把组件名**重置回默认名**\n"
    "   （真机：`箱座-1` → `1-箱座-2`）。所以改名必须排在**所有** \n"
    "   `set_component_transform` **之后**，否则白改。\n"
    "\n"
    "★ 两种指代方式：\n"
    "    · `component` —— 名字（基名/全名），走 `resolve_component`；\n"
    "    · `path` + `index` —— **同名多实例**专用：把 `path` 命中的组件按\n"
    "      `GetComponents` 顺序（= 插入顺序）排序，取第 `index` 个（0 起）。\n"
    "      ★ 这一路绕开名字（被重置过、不确定），是批量装配的推荐写法。\n"
    "★★ 判据 = `IComponent2.Name2` **读回**是 `<新名>` 或 `<新名>-<实例号>`\n"
    "   （实例号后缀是 SolidWorks 维护的、改不掉，见 `_rename_component`）。",
    api=["IComponent2.Name2", "IModelDocExtension.SelectByID2", "IAssemblyDoc.GetComponents"],
    params={"component": {"type": "str", "required": False, "desc": "组件名（与 path 二选一）"},
            "path": {"type": "str", "required": False,
                     "desc": "零件路径；同名多实例时配合 index 用"},
            "index": {"type": "int", "required": False, "default": 0,
                      "desc": "path 命中多个时取第几个（0 起，按插入顺序）"},
            "name": {"type": "str", "required": True, "desc": "新的组件名（基名）"}},
    requires=("asm_open", "has_component"), verified=True)
def op_rename_component(sw, step, ctx):
    new = str(_p(step, ctx, "name") or "")
    if not new:
        raise SwError("rename_component 必须给 name")
    path = _p(step, ctx, "path")
    cur = None
    if path is not None:
        w = sw_connect.win_path(str(path)).lower()
        allc = [c for c in sw.components() if c.get("path")]
        hits = [c for c in allc if str(c["path"]).lower() == w]
        if not hits:                       # 退一步：后缀匹配（盘符大小写/相对路径差异）
            hits = [c for c in allc if str(c["path"]).lower().endswith(w.lstrip("\\"))]
        if not hits:
            raise SwError(f"按 path='{path}' 找不到任何组件；当前路径有 "
                          f"{sorted({c.get('path') for c in allc})}")
        i = int(_p(step, ctx, "index", 0) or 0)
        if not (0 <= i < len(hits)):
            raise SwError(f"index={i} 越界：path 命中 {len(hits)} 个组件 "
                          f"（{sorted(c.get('name') for c in hits)}）")
        cur = hits[i].get("name")
    else:
        want = str(_p(step, ctx, "component") or "")
        if not want:
            raise SwError("rename_component 必须给 component 或 path")
        full, _c, cands = sw.resolve_component(want)
        if full is None:
            have = [c.get("name") for c in sw.components() if c.get("name")]
            if cands:
                raise SwError(f"组件名 '{want}' **有歧义**，匹配到 {cands} —— "
                              f"同名多实例请改用 path+index 写法")
            raise SwError(f"组件名对不上：'{want}'。当前组件：{have}")
        cur = full
    actual = _rename_component(sw, cur, new)
    return {"renamed_from": cur, "name": actual, "requested": new, "ok": True}


def _components_named(sw, step, ctx) -> list[str]:
    """从 step 取要操作的组件名表。`components` 可为 'all' / 单个名字 / 名字列表。

    ★ 三种写法（走 `sw.resolve_component`，按此优先级）：
        · **零件路径**（`…/1-箱座.SLDPRT`）—— **最稳**：名字会被 `SetTransformAndSolve3`
          重置回默认名，路径不会；跨多步指向同一组件时**优先用它**
        · **全名**（`地基-1`）
        · **唯一的基名**（`地基`）
      解析成**全名**后返回（下游按名选中**只认全名**，真机实测）。
    ★ 解析不出 ⇒ 报错并列出真名；**有歧义**（两个组件基名相同）⇒ 也报错 ——
      **绝不猜**（猜错就是静默改错组件）。
    """
    which = _p(step, ctx, "components", "all")
    have = [c.get("name") for c in sw.components() if c.get("name")]
    if which in (None, "all", "*", []):
        return have
    if isinstance(which, str):
        which = [which]
    out: list[str] = []
    for w in which:
        full, c, cands = sw.resolve_component(str(w))
        if c is None:
            if cands:
                raise SwError(f"组件名 '{w}' **有歧义**，匹配到 {cands} —— "
                              f"请写**全名**（形如 `{cands[0]}`）")
            raise SwError(f"组件名对不上：'{w}'。当前装配体里只有 {have}")
        out.append(full)
    return out


def _set_components_fixed(sw, step, ctx, fixed: bool) -> dict:
    """固定/浮动组件。★ 判据 = **IsFixed 真的变了**（FixComponent 不返回成败）。"""
    names = _components_named(sw, step, ctx)
    if not names:
        raise SwError("装配体里没有组件可操作（先 insert_component）")
    before = {c.get("name"): bool(c.get("fixed")) for c in sw.components()}
    already = [n for n in names if before.get(n) == fixed]
    todo = [n for n in names if before.get(n) != fixed]
    if todo:
        sw.clear_selection()
        # ★ 判据 = **选中数**，不是 SelectByID2 的返回值 —— 它对组件会**谎报 False**
        #   （实测：返回 False 但实际选中 1 个）。见 sw_connect.select_component。
        missed = [n for n in todo if not sw.select_component(n, append=True)]
        cnt = sw.selected_count()
        if missed or cnt != len(todo):
            sw.clear_selection()
            raise SwError(f"选组件失败：没命中 {missed}，选中数 {cnt} ≠ 目标数 {len(todo)}。"
                          f"组件真名见 sw.components()（形如 `零件1-1`，"
                          f"**必须带实例号后缀**：光基名选不中，实测）")
        _r, err = _call(member, sw.asm, "FixComponent" if fixed else "UnfixComponent")
        sw.clear_selection()
        after = {c.get("name"): bool(c.get("fixed")) for c in sw.components()}
        wrong = [n for n in todo if after.get(n) != fixed]
        if wrong:
            raise SwError(
                f"{'FixComponent' if fixed else 'UnfixComponent'} 调完了但状态没变："
                f"{ {n: (before.get(n), after.get(n)) for n in wrong} }（期望 fixed={fixed}）"
                + (f"；COM 报错：{err}" if err else ""))
    return {"fixed": fixed, "changed": todo, "already_ok": already,
            "components": [c for c in sw.components() if c.get("name") in names]}


@op("fix_component", "固定组件", "asm",
    "把组件固定（Fixed）—— 位置锁死，加配合时不会被动漂。\n"
    "★ `FixComponent()` **零参数**，作用于当前选择集；所以本 op 先按名字把组件选中。\n"
    "★★ 判据 = `IComponent2.IsFixed` **真的变了**（该 API 不返回成败）。\n"
    "★ SolidWorks 行为：**第一个插入的组件默认已被固定** ⇒ 本 op 对它会报"
    "「already_ok」而不是失败（重复固定是无害的，但要说清楚）。",
    api=["IAssemblyDoc.FixComponent", "IModelDocExtension.SelectByID2",
         "IComponent2.IsFixed"],
    params={"components": {"type": "list", "required": False, "default": "all",
                           "desc": "'all' / 组件名 / 名字列表（真名形如 `零件1-1`）"}},
    requires=("asm_open", "has_component"), verified=True)
def op_fix_component(sw, step, ctx):
    return _set_components_fixed(sw, step, ctx, True)


@op("float_component", "浮动组件", "asm",
    "把组件改成浮动（Unfix）—— 位置交给配合来决定。\n"
    "★ 建装配体的标准顺序：第一件固定当地基，其余全部浮动后靠配合定位。\n"
    "★★ 判据 = `IComponent2.IsFixed` **真的变了**（同 fix_component）。",
    api=["IAssemblyDoc.UnfixComponent", "IModelDocExtension.SelectByID2",
         "IComponent2.IsFixed"],
    params={"components": {"type": "list", "required": False, "default": "all"}},
    requires=("asm_open", "has_component"), verified=True)
def op_float_component(sw, step, ctx):
    return _set_components_fixed(sw, step, ctx, False)


@op("set_component_transform", "设定组件变换", "asm",
    "把一个组件**直接摆到指定的位姿**（平移 + 旋转），不经过配合求解。\n"
    "\n"
    "★ 为什么需要它（2026-10-05）：配合实体是按**装配体全局坐标**拾取的\n"
    "  （`_select_asm_face_at`）。要拾取\"箱盖上的剖分面\"，就得先知道**箱盖在哪儿**\n"
    "  —— 而箱盖落位本来是配合求解的结果 ⇒ **鸡生蛋**。\n"
    "  解法：先把组件摆到已知位姿（它天然满足后面要加的配合），再按坐标拾取、加配合。\n"
    "  这不是\"绕开配合\"：配合依然是约束定义，只是把 SolidWorks 求解器本来要做的\n"
    "  初始摆位**显式做一次**，使坐标可算。\n"
    "\n"
    "★ `array` = **`IMathTransform.ArrayData` 的 16 个数原样**（前 9 个旋转、\n"
    "  第 10~12 个平移，**平移单位是米**）。从别的装配体/文件里读到的位姿可以**逐字复制**\n"
    "  ⇒ 零换算、零约定风险（旋转的存储约定在本机无法从文档确认，抄原值最稳）。\n"
    "  给了 `array` 时 `xyz_mm` 只**覆盖**平移部分、`rotate_*` 会被忽略。\n"
    "\n"
    "★ 便利写法（不写 array 时）：`xyz_mm`（平移，mm）+ 可选 `rotate_axis`\n"
    "  （`\"X\"/\"Y\"/\"Z\"` 或 `[x,y,z]`，**全局坐标系**下的轴）与 `angle_deg`。\n"
    "  不写 `rotate_*` = 保持组件当前的旋转、只挪平移。\n"
    "\n"
    "★ 判据不是 `SetTransformAndSolve3` 的返回值（它可能说谎），而是\n"
    "  **读回 `IComponent2.GetBox` 并跟期望比较** —— `check_bbox` 里做同一件事。\n"
    "  本 op 至少会读回一次变换，确认**平移分量真的写进去了**。",
    api=["IComponent2.SetTransformAndSolve3", "IMathTransform.ArrayData",
         "IComponent2.Transform2", "IComponent2.GetBox"],
    params={"component": {"type": "str", "required": True, "desc": "组件名（基名或全名）"},
            "array": {"type": "list", "required": False,
                      "desc": "IMathTransform.ArrayData 原样 16 个数（平移单位**米**）"},
            "xyz_mm": {"type": "list", "required": False,
                       "desc": "[x,y,z] 平移（mm，装配体全局）"},
            "rotate_axis": {"type": "str", "required": False,
                            "desc": "'X'/'Y'/'Z' 或 [x,y,z]（全局轴）"},
            "angle_deg": {"type": "float", "required": False, "default": 90.0},
            "this_config": {"type": "bool", "required": False, "default": True,
                            "desc": "只作用于当前配置（对应 SetTransformAndSolve3 第 2 参）"}},
    requires=("asm_open", "has_component"), verified=True,
    limits=("`IMathUtility.CreateTransform*` 在本机**整个不可用**（一律 com_error "
            "-2147417851「服务器出现意外情况」）⇒ 实现改为「改写组件现有 "
            "`Transform2.ArrayData`」再 `SetTransformAndSolve3`",
            "`ArrayData` 必须包成 `VARIANT(VT_ARRAY|VT_R8)`；直接赋 tuple/list "
            "**不报错但把平移写成 0**（静默）"))
def op_set_component_transform(sw, step, ctx):
    want = str(_p(step, ctx, "component") or "")
    if not want:
        raise SwError("set_component_transform 必须给 component")
    full, comp, cands = sw.resolve_component(want)
    if full is None:
        have = [c.get("name") for c in sw.components() if c.get("name")]
        if cands:
            raise SwError(f"组件名 '{want}' **有歧义**，匹配到 {cands} —— 请写全名")
        raise SwError(f"组件名对不上：'{want}'。当前装配体里只有 {have}")

    arr = _p(step, ctx, "array")
    if arr is not None:
        arr = [float(v) for v in arr]
        if len(arr) < 12:
            raise SwError(f"array 至少要 12 个数（9 旋转 + 3 平移，单位米），实得 {len(arr)}")
        arr = (arr + [0.0] * 16)[:16]
    else:
        cur = _xform_of(comp)
        if cur is None:
            raise SwError(f"读不到组件 '{full}' 的当前变换（`IComponent2.Transform2`），"
                          f"没法在保持旋转的前提下只改平移 —— 请改用 `array` 写法")
        Rcur, tcur = cur
        xyz = _p(step, ctx, "xyz_mm")
        t = ([float(v) / 1000.0 for v in xyz] if xyz is not None else list(tcur))
        ax = _p(step, ctx, "rotate_axis")
        if ax:
            if isinstance(ax, str):
                a = ax.strip().upper().lstrip("+")
                vec = {"X": (1.0, 0.0, 0.0), "Y": (0.0, 1.0, 0.0),
                       "Z": (0.0, 0.0, 1.0)}.get(a)
                if vec is None:
                    raise SwError(f"rotate_axis 只认 'X'/'Y'/'Z' 或 [x,y,z]，实得 {ax!r}")
            else:
                vec = tuple(float(v) for v in ax)
            if len(vec) != 3 or math.dist(vec, (0, 0, 0)) < 1e-9:
                raise SwError(f"rotate_axis 非法：{ax!r}")
            ang = math.radians(float(_p(step, ctx, "angle_deg", 90.0) or 0.0))
            # 罗德里格斯公式：绕**单位轴**转 R_new（作用在全局坐标上）
            n = tuple(v / math.dist(vec, (0, 0, 0)) for v in vec)
            c_, s_ = math.cos(ang), math.sin(ang)
            K = [[0.0, -n[2], n[1]], [n[2], 0.0, -n[0]], [-n[1], n[0], 0.0]]
            I3 = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]
            Rr = [[c_ * I3[i][j] + (1 - c_) * n[i] * n[j] + s_ * K[i][j]
                   for j in range(3)] for i in range(3)]
            # 新旋转 = Rr · Rcur（都作用在全局坐标上）
            Rnew = [[sum(Rr[i][k] * Rcur[k][j] for k in range(3)) for j in range(3)]
                    for i in range(3)]
        else:
            Rnew = [list(r) for r in Rcur]
        # ★ SW 的 ArrayData 存的是 R 的**转置**（行主序）—— 见 _to_local / _xform_of。
        #   本 op 对外说的是「作用在全局坐标上的 R」，所以这里转置回去。
        arr = [Rnew[j][i] for i in range(3) for j in range(3)] + list(t) + [1.0, 0.0, 0.0, 0.0]

    # ★★★ 2026-10-05 真机确诊：**`IMathUtility.CreateTransform*` 在本机一律报
    #   `com_error (-2147417851, '服务器出现意外情况')`** —— `CreateTransform(array)`、
    #   `CreateTransformRotateAxis(pt,vec,ang)` 全试过（VARIANT(VT_ARRAY|VT_R8) 与
    #   普通 tuple/list 都试过），无一成功。**这条路整个不能用**，别再试了。
    #   ⇒ 唯一可行写法：拿组件**已有的** `Transform2`（一个活着的 IMathTransform），
    #     改它的 `ArrayData` 属性，再交给 `SetTransformAndSolve3`。真机已验证。
    xf = member(comp, "Transform2")
    if xf is None:
        xf, xerr = _call(member, comp, "GetTransform")
        if xf is None:
            raise SwError(f"拿不到组件的 `Transform2`/`GetTransform`（{xerr}）—— "
                          f"没有可改写的 IMathTransform 对象，设不了位姿")
    # ★★ `ArrayData` 赋 tuple/list **不抛异常但把平移写成 0**（真机实测：
    #   读回 `[1,0,0, 0,1,0, 0,0,1, 0,0,0]`）⇒ 与 `EntitiesToMate` 同一类静默坑。
    #   必须 `VARIANT(VT_ARRAY|VT_R8)`，且**写完立刻读回比对**。
    try:
        xf.ArrayData = sw_connect.variant_r8(arr)
    except Exception as e:                                    # noqa: BLE001
        raise SwError(f"写 `IMathTransform.ArrayData` 失败（{type(e).__name__}: {e}）") from None
    got_arr = None
    with contextlib.suppress(Exception):
        got_arr = [float(v) for v in (member(xf, "ArrayData") or [])]
    if got_arr is None or len(got_arr) < 12:
        raise SwError(f"写完 `ArrayData` 读不回（实得 {got_arr!r}）")
    if max(abs(a - b) for a, b in zip(got_arr, arr)) > 1e-9:
        raise SwError(
            f"`ArrayData` 赋值**静默失效**：期望 {[round(v, 8) for v in arr]}\n"
            f"                        读回 {[round(v, 8) for v in got_arr]}\n"
            f"  ★ 根因通常是没包 `VARIANT(VT_ARRAY|VT_R8)`（直接赋 tuple/list "
            f"会把平移写成 0，且不报错）")
    tc = bool(_p(step, ctx, "this_config", True))
    ok, err = _call(member, comp, "SetTransformAndSolve3", xf, tc)
    # ---- 判据：读回平移分量（不信返回值） ----
    back = _xform_of(comp)
    got = [round(v * 1000.0, 4) for v in (back[1] if back else [])]
    want_t = [round(v * 1000.0, 4) for v in arr[9:12]]
    if back is None:
        raise SwError("`SetTransformAndSolve3` 调完了但**读不回**变换（Transform2 回空）"
                      + (f"；COM 报错：{err}" if err else ""))
    if max(abs(a - b) for a, b in zip(got, want_t)) > 1e-3:
        raise SwError(f"设变换没生效：平移期望 {want_t}mm，读回 {got}mm"
                      f"（返回值={ok}）" + (f"；COM 报错：{err}" if err else ""))
    box = sw.component_boxes().get(full)
    return {"component": full, "array_applied": [round(v, 8) for v in arr],
            "translation_mm_readback": got, "set_returned": bool(ok),
            "box_mm": box}


def _mate_entities(o1, o2):
    """把两个实体包成 `IMateFeatureData.EntitiesToMate` 要的 **VARIANT 数组**。

    ★★ 为什么必须显式编组（2026-10-02 真机确诊，A 级坑 —— 比"返回值说谎"更狠）：

        `EntitiesToMate` 在 `sldworks.tlb` 里的声明是 **Variant**：
            ICoincidentMateFeatureData.EntitiesToMate  [0 参数] -> Variant   （get）
            ICoincidentMateFeatureData.EntitiesToMate  [1 参数] -> void      （put <arg0>: Variant）

        pywin32 在**没有参数类型信息**时，把 Python 的 tuple / list 编组成 VARIANT
        会**静默丢掉里面的 COM 对象** —— **不抛异常**，读回长度就是 0。
        真机实测三种写法的「读回长度」：

            data.EntitiesToMate = (o1, o2)                                  → 0   ✗
            data.EntitiesToMate = [o1, o2]                                  → 0   ✗
            data.EntitiesToMate = VARIANT(VT_ARRAY|VT_DISPATCH, [o1, o2])   → 2   ✓

        ⇒ 必须显式构造 `VT_ARRAY | VT_DISPATCH`。

    ★ 这条的伪装性极强：`coincident` / `parallel` / `perpendicular` 三个
      **支持预选**的类型，就算 EntitiesToMate 是空的也照样能建出来
      ⇒ 「三个类型通过」会让人误以为「这条路径没问题」，
      直到遇到 **distance / angle / concentric**（不支持预选）才集体暴雷。
      **「部分通过」比「全不通过」更难查** —— 因为它给了一个假的安全感。

    ★ 离线场景要能降级：没装 pywin32 的机器上（离线自检）`import pythoncom` 会失败。
      那种情况返回普通 list 即可（假对象不需要真 VARIANT），**绝不因此把主流程炸掉**。
      真机上若万一降级成 list ⇒ `op_add_mate` 里的**读回守卫**会立刻报错
      （读回长度 ≠ 2），不会静默。
    """
    try:
        import pythoncom
        from win32com.client import VARIANT
    except Exception:                                            # noqa: BLE001
        return [o1, o2]
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [o1, o2])


def _seq_len(v) -> int:
    """数一个「像数组的东西」的长度（VARIANT / list / tuple / None 都吃）。

    ★ 为什么要它：`EntitiesToMate` 读回的是 **win32com 的 VARIANT**，
      `len()` 直接吃未必行（要先 `.value`）；离线假对象存的可能是普通 list。
      ⇒ 两种都试。**取不到就回 -1**（调用方把 -1 当"没读出来"处理，不要当成 0）。
    """
    if v is None:
        return 0
    for cand in (v, getattr(v, "value", None)):
        if cand is None:
            continue
        try:
            return len(cand)
        except Exception:                                        # noqa: BLE001
            continue
    return -1


#: 判「组件被挪动了」的容差（mm）。配合本来就要把组件摆到位 ⇒ 挪动**本身不是错**，
#: 但**没预期的**挪动会让后续按坐标取的点全部作废 ⇒ 必须报出来（见 `_moved_boxes`）。
MOVE_TOL_MM = 1e-3


def _moved_boxes(before: dict, after: dict, tol: float = MOVE_TOL_MM) -> list[dict]:
    """比较两次 `SwSession.component_boxes()`，报出**位移超过 tol 的**组件。

    返回按**最大位移降序**的表：
        [{"name": "上块-2", "d": [dx,dy,dz,dx,dy,dz], "dmax_mm": 27.16,
          "before": [80,-15,5,120,15,15], "after": [31.559,-30.322,-5,80,12.712,5]}]

    ★★ 为什么要有它（2026-10-03 真机）：`asm_mate` 探针在 m2 报「配合实体没选全，
      没命中 [{'type':'FACE','point':[80,0,10]}]」—— 报错指向**取点**，
      真根因却是 **m1 把上块翻走了**（`align=aligned` 且两面法线相反）⇒
      报错方向完全错。建配合**前后各读一次包围盒**是当场看见"动了"的唯一手段。

    ★ 只比**同名**组件；新增/消失的组件不算"挪动"（那是组件数变化，归
      `check_component_count` 管）。缺包围盒（读不到）的组件跳过，不猜。
    """
    out: list[dict] = []
    for n, b in (before or {}).items():
        a = (after or {}).get(n)
        if not a or len(a) != 6 or len(b) != 6:
            continue
        d = [round(float(a[i]) - float(b[i]), 6) for i in range(6)]
        dmax = max(abs(v) for v in d)
        if dmax <= tol:
            continue
        out.append({"name": n, "d": d, "dmax_mm": round(dmax, 4),
                    "before": list(b), "after": list(a)})
    out.sort(key=lambda x: -x["dmax_mm"])
    return out


@op("add_mate", "添加配合", "asm",
    "在**两个实体**（面/边/轴/点）之间加一个配合。\n"
    "\n"
    "★ 走**现代** API：`CreateMateData(类型)` → 设属性 → `CreateMate(data)`。\n"
    "  旧路径 `AddMate`~`AddMate5` **全部已废弃**（见本节头注①），一个都不用。\n"
    "\n"
    "★ `entities` 必须是**两个**选择器，写法同 `_pick`：\n"
    "    {\"type\": \"FACE\", \"point\": [x, y, z]}   ← 全局毫米坐标射线拾取（最实用，面没名字）\n"
    "    {\"name\": \"Face12@零件1-1@装配体1\", \"type\": \"FACE\"}  ← 显式名字\n"
    "  两个实体都用 **mark=1** 选中（`AddMate5` 的 Remarks 给的标记表：除\n"
    "  CAMFOLLOWER(8)/WIDTH(16) 外，其它配合类型都用 mark 1）。\n"
    "\n"
    "★★ 关键：**一律显式写 `data.EntitiesToMate`**。因为官方 Remarks 里只有带星号的\n"
    "  类型支持「预选实体」，而 **distance / angle / symmetric / profile_center / width\n"
    "  不在其中** —— 那几种光靠\"先选中\"是建不出来的（且多半静默）。\n"
    "★★★ 而 `EntitiesToMate` 是 **Variant（SAFEARRAY）** 类型：直接赋 Python 的\n"
    "  tuple/list 会被 pywin32 **静默丢掉**（**不抛异常**、读回长度 0）⇒ 必须用\n"
    "  `VARIANT(VT_ARRAY|VT_DISPATCH, [o1, o2])`（本文件 `_mate_entities`）。\n"
    "  ★ 这条伪装性极强：`coincident/parallel/perpendicular` **支持预选**，\n"
    "  EntitiesToMate 空着也能建 ⇒ 会以为「这条路没问题」，直到 \n"
    "  `distance/angle/concentric` 集体暴雷。**「部分通过」比「全不通过」更难查**。\n"
    "★ `distance` 单位 mm、`angle` 单位**度**（op 内部转米/弧度）。\n"
    "\n"
    "★★★ `align` 三态（只用 `aligned` / `anti_aligned` / `closest`；旧名 Obsolete，见头注②）\n"
    "   —— **默认 `closest`，别乱写 `aligned`**（真机实测，2026-10-03）：\n"
    "     · `closest`     = 就近。求解器**按当前姿态**挑一个对齐方向 ⇒ **组件不动**。UI 默认行为。\n"
    "     · `anti_aligned`= 两法线**相反**（两个面\"脸对脸\"）。本件当前姿态就是相反时 ⇒ 也不动。\n"
    "     · `aligned`     = 两法线**同向**。★ 当两配合面当前法线**相反**（例如\"上件的下表面\"\n"
    "       贴\"下件的上表面\"：+Z vs −Z）时，为满足\"同向\"求解器会**把整个组件翻转 180°**，\n"
    "       而且重合配合只约束 1 个平移 + 1 个转动 ⇒ 剩 2 个转动自由度**被随机填**，\n"
    "       于是组件还会**绕轴自旋**。真机实测一个 40×30×10 的块被翻成\n"
    "       `[31.559, -30.322, -5.0, 80.0, 12.712, 5.0]`（凹陷进另一件里了）。\n"
    "   ⇒ 后果：**后续所有按坐标拾取的取点全部作废**（面飞了），报出来的却是\n"
    "     \"配合实体没选全/没命中\" —— 一个**指向错误方向**的报错。\n"
    "   ⇒ 所以本 op 默认 `closest`（不改变现有姿态）；只有**明确要翻转/要对齐**时才写 `aligned`。\n"
    "★★ 判据 = **特征树真的长了新节点**（不信 CreateMate 的返回值）。\n"
    "★★★ 真机边界（2026-10-03，5 个变体全灭）：**同一文档里第二条配合必然静默失效**\n"
    "   —— `CreateMate` 返回 feature 对象、`err=None`、`GetErrorCode=0`，\n"
    "   但**特征树不长节点、配合数不涨**。换类型(distance/parallel)、前面\n"
    "   `ForceRebuild3(False)`、sleep 0.6s + `ClearSelection2` 都救不回来；\n"
    "   第一条永远成功。⇒ **返回值、错误码、GetErrorCode 三样全都报成功**，\n"
    "   只有配合数/特征树抓得住它。边界由 `assets/probes/asm_mate2.json`（XFAIL）\n"
    "   钉住。★ 真实减速器装配要建几十条配合 ⇒ 这是装配阶段的**一号阻塞**。",
    api=["IAssemblyDoc.CreateMateData", "IAssemblyDoc.CreateMate",
         "IModelDocExtension.SelectByID2", "ISelectionMgr.GetSelectedObject6"],
    params={"kind": {"type": "str",
                     "desc": "配合类型：" + " / ".join(sorted(ASM_MATES))},
            "entities": {"type": "list",
                         "desc": "两个实体选择器 [{type,point|name}, {...}]"},
            "align": {"type": "str", "required": False, "default": "closest",
                      "desc": "aligned / anti_aligned / closest（★ 默认 closest = 不改变组件"
                              "当前姿态；aligned 会把法线相反的两个面**翻转**组件）"},
            "distance": {"type": "float", "unit": "mm", "required": False,
                         "desc": "kind=distance 时的距离"},
            "angle": {"type": "float", "unit": "deg", "required": False,
                      "desc": "kind=angle 时的角度"},
            "flip": {"type": "bool", "required": False, "default": False,
                     "desc": "距离/角度配合：换到另一个满足条件的位置"},
            "lock_rotation": {"type": "bool", "required": False, "default": False,
                              "desc": "kind=concentric：锁住转动"},
            "name": {"type": "str", "required": False, "desc": "配合特征改名"}},
    requires=("asm_open", "has_component"), effect=("has_mate",), verified=True)
def op_add_mate(sw, step, ctx):
    kind = str(_p(step, ctx, "kind") or "").strip().lower()
    if kind not in ASM_MATES:
        raise SwError(f"不认识的配合类型 '{kind}'。可用：{sorted(ASM_MATES)}")
    m = ASM_MATES[kind]
    ents = _p(step, ctx, "entities")
    if not isinstance(ents, (list, tuple)) or len(ents) != 2:
        raise SwError(f"`entities` 必须是**正好两个**实体选择器（实得 {ents!r}）。"
                      f"配合就是两个实体之间的关系，1 个或 3 个都不成立")
    mtype = sw.C(m["const"])
    if mtype is None:
        raise SwError(f"取不到常量 {m['const']}（查 assets/sw_enums.json；"
                      f"或跑 sw_docbuild.py 重建枚举表）")
    # ---- 1) 清选择 → 选两个实体 ----
    # ★★ 2026-10-03：坐标取点**不再走 `SelectByID2` 的相机射线**。
    #   射线只能选中**朝向相机**的面（真机实测：默认等轴测视图下，上块的 -X 面与
    #   +Y 面**恒选不中**，+X/-Y/底面选得中）⇒ 报错会说"没命中"，而真实原因是
    #   "这个面背着相机"。⇒ 改走 `_select_asm_face_at`（枚举面 + 平面距离判据
    #   + `IEntity.Select4`），**与视角无关**。射线只作为最后兜底。
    #
    # ★★ 2026-10-05：整段选面 + `CreateMate` 包进**重试**（`COM_RETRY` 次）。
    #   真机证据：同一份计划、同一条 `concentric`（垫圈内孔↔螺栓杆），
    #   前一条（螺栓↔孔）成、这一条报「CreateMate 没有产生任何新特征」，
    #   而**选面信息明明是干净的**（两个不同组件的面、距点 0.000mm）。
    #   这与本项目已记录的 COM 瞬时失败（`AddComponent5`/`SelectByID2`
    #   "时好时坏、不抛异常、计数不涨"）是同一类 ⇒ **没长特征就重选重建**，
    #   重试次数写进返回值 `attempts`，日志里看得见（不是"掩盖错误"）。
    sw.clear_selection()
    missed, how = [], []

    def _select_pair():
        """清选择 → 按坐标/名字选两个实体 → 返回 (o1, o2)。失败直接抛。"""
        sw.clear_selection()
        missed.clear()
        how.clear()
        # ★ 两个实体**复用同一份面索引**（枚举一遍面要几秒；一条配合里两个实体共用，
        #   且循环内不挪动组件 ⇒ 索引不会过期）。见 `_asm_face_index_cached`。
        idx0 = _asm_face_index_cached(sw)
        for it in ents:
            pt = it.get("point") if isinstance(it, dict) else None
            # ★★ `"component"` = **限定只在某个组件里找面**（2026-10-05 加）。
            #   为什么必须：螺栓杆 Ø8 外圆与它穿过的 Ø9 孔壁只差 0.5mm，只按坐标找
            #   「点到面距离最小」时两者几乎并列 ⇒ **选哪个看枚举顺序** ⇒ 静默选错组件
            #   （配合照样建得上，只是配合的是错的东西）。
            #   紧固件类配合（螺栓↔孔、垫圈↔杆、螺母↔杆）**必须**写 component。
            only = None
            if isinstance(it, dict) and it.get("component"):
                _full, _c, _cands = sw.resolve_component(str(it["component"]))
                if _full is None:
                    sw.clear_selection()
                    raise SwError(
                        f"配合实体里写的 component='{it['component']}' 解析不到"
                        + (f"（**有歧义**，候选 {_cands}）" if _cands else
                           f"（当前组件：{[c.get('name') for c in sw.components()]})"))
                only = frozenset({_full})
            if pt:
                ok, info = _select_asm_face_at(sw, pt, append=True, mark=1,
                                               only=only, idx0=idx0)
                if not ok and only is None:
                    # 兜底：仍试一次射线（万一某些版本的面名/枚举路径不通）
                    # ★ 限定了组件时**不走**射线兜底 —— 射线不看 component，
                    #   会选到别的组件上去（那正是这一步要防的事）。
                    ok = _pick(sw, it, mark=1, append=True)
                    info = "射线兜底" if ok else info
                if ok:
                    how.append(info + (f"［限定组件 {sorted(only)}］" if only else ""))
                    continue
                missed.append(f"{it} —— {info}")
            elif not _pick(sw, it, mark=1, append=True):
                missed.append(str(it))
        if missed:
            sw.clear_selection()
            raise SwError(f"配合实体没选全，没命中：{missed}。\n"
                          f"  point 必须是**全局毫米坐标**且落在目标面内部（别落在棱上）。\n"
                          f"  ★ 判据是「点到该面所在平面的距离 ≤ 3mm」+「面内最近」，"
                          f"所以**与当前视图无关**（旧实现走相机射线，背着相机的面永远选不中）。\n"
                          f"  排查：① 坐标算的是哪个组件的位置（配合可能已把组件挪走 ⇒ "
                          f"看本 op 上一步的 `moved`/`measured_components`）；"
                          f"② 点是不是落在**棱**上（棱上归属不唯一，换到面中心）；"
                          f"③ 组件是不是**子装配体**（本枚举只下探一层）")
        sel = sw.selmgr()
        cnt = int(member(sel, "GetSelectedObjectCount2", -1) or 0)
        if cnt != 2:
            sw.clear_selection()
            raise SwError(f"选中数 {cnt} ≠ 2 —— 选择集不干净，不敢往下建配合")
        _o1 = member(sel, "GetSelectedObject6", 1, -1)
        _o2 = member(sel, "GetSelectedObject6", 2, -1)

        if _o1 is None or _o2 is None:
            sw.clear_selection()
            raise SwError("选中数对了但取不出实体对象（GetSelectedObject6 回空）")
        return _o1, _o2

    # ---- 2) 重试环：选面 → CreateMateData → 设实体 → CreateMate ----
    # ★★ 2026-10-05 真机：同一条 concentric 时报「CreateMate 没有产生任何新特征」，
    #   而选面信息干净（两个不同组件的面、距点 0.000mm）⇒ 这与本项目已记录的
    #   COM 瞬时失败（AddComponent5 / SelectByID2 "时好时坏、不抛异常、计数不涨"）
    #   是同一类。既然判据是**读回**（特征树涨没涨），没涨就**重选重建**；
    #   重试次数写进返回值 `attempts`，日志里看得见（不是"掩盖错误"）。
    asm = sw.asm
    notes: list[str] = []
    fresh, feat, err = None, None, None
    attempts = 0
    o1 = o2 = None
    for attempts in range(1, COM_RETRY + 1):
        o1, o2 = _select_pair()
        data = member(asm, "CreateMateData", int(mtype))
        if data is None:
            raise SwError(f"CreateMateData({m['const']}) 回空 —— 该类型可能不支持这条路径")
        data = typed(data, m["iface"]) or data       # 官方措辞：cast 到 mate-specific 接口
        try:
            # ★★ 必须用 _mate_entities 显式编组：直接赋 Python tuple/list 会**静默失效**
            #    （不抛异常、读回长度 0）。见 _mate_entities 的文档。
            data.EntitiesToMate = _mate_entities(o1, o2)
        except Exception as e:                                    # noqa: BLE001
            sw.clear_selection()
            raise SwError(
                f"设 EntitiesToMate 失败（{type(e).__name__}: {e}）。\n"
                f"  ★ kind={kind} " +
                ("**不支持预选实体**（见 op 文档头注③）⇒ 这一步是唯一的路，必须成。"
                 if kind in ASM_MATES_NO_PRESELECT else
                 "该类型支持预选，理论上可以先选中再建 —— 但本 op 一律显式设，"
                 "所以这里失败说明数据类型不对（cast 没成功）。")) from None
        # ★ 读回验证：赋值**不抛异常**不等于**写进去了**（见 _mate_entities）。
        n_back = -1
        with contextlib.suppress(Exception):
            n_back = _seq_len(data.EntitiesToMate)
        if n_back != 2:
            sw.clear_selection()
            raise SwError(
                f"EntitiesToMate 赋值**静默失效**（读回长度 {n_back}，期望 2）。\n"
                f"  ★ 根因：该属性是 **Variant（SAFEARRAY）**，直接赋 Python tuple/list\n"
                f"    会被 pywin32 丢掉、且**不报错** ⇒ 必须用 win32com 的\n"
                f"    VARIANT(VT_ARRAY|VT_DISPATCH, [o1, o2])（本文件 _mate_entities）。\n"
                f"  ★ 这类『部分类型能成』的坑最难查：coincident/parallel/perpendicular\n"
                f"    支持预选，EntitiesToMate 空着也能建 ⇒ 会让人以为这条路没问题。")
        align = str(_p(step, ctx, "align", "closest") or "closest").strip().lower()
        if align not in ASM_MATE_ALIGN:
            sw.clear_selection()
            raise SwError(f"不认识的 align '{align}'。可用：{sorted(ASM_MATE_ALIGN)}"
                          f"（★ 旧名 swAlignSAME/AGAINST/NONE 已被官方标 Obsolete，不接受）")
        av = sw.C(ASM_MATE_ALIGN[align])
        try:
            data.MateAlignment = int(av if av is not None else 0)
        except Exception as e:                                    # noqa: BLE001
            # 有些类型（perpendicular/lock/width）没有 MateAlignment —— 只有**显式**要求
            # 非默认对齐时才算真错，否则记一条 note 放过。
            # ★ 判据跟默认值对齐：默认是 closest ⇒ 只有用户**显式**写了别的才报错。
            msg = f"{type(e).__name__}: {e}"
            if align != "closest":
                sw.clear_selection()
                raise SwError(f"kind={kind} 设 MateAlignment={align} 失败（{msg}）——"
                              f"该配合类型可能没有对齐属性；去掉 align 或换类型") from None
            notes.append(f"该类型没有 MateAlignment（{msg}），已按默认对齐（closest）")
        if kind == "distance":
            dist = _p(step, ctx, "distance")
            if dist is None:
                sw.clear_selection()
                raise SwError("kind=distance 必须给 distance（mm）")
            dv = mm(float(dist))
            data.Distance = dv
            # ★ Remarks：不给上下限时把两个限值都设成 Distance 本身
            with contextlib.suppress(Exception):
                data.MaximumDistance = dv
                data.MinimumDistance = dv
        elif kind == "angle":
            ang = _p(step, ctx, "angle")
            if ang is None:
                sw.clear_selection()
                raise SwError("kind=angle 必须给 angle（度）")
            data.Angle = deg(float(ang))
        elif kind == "concentric" and _p(step, ctx, "lock_rotation"):
            with contextlib.suppress(Exception):
                data.LockRotation = True
        if _p(step, ctx, "flip"):
            with contextlib.suppress(Exception):
                data.FlipDimension = True
        # ---- 3) 建配合，判据 = CreateMate 回了新特征 + 组件到底动没动 ----
        # ★★★ 2026-10-06 性能大修（真机实测，两轮）：本段原来每条 add_mate 要调
        #   ① 两次整棵 `feature_tree()`（会把**每个组件的子特征**都走一遍：58 组件装配体
        #      ⇒ 数千节点、每节点 3 次 COM）；② 改浅遍历后又发现 `mate_count()` 本身就是
        #      `mate_features()` → 仍是 O(#配合) 的整树浅走，而本段每步要调 3~4 次
        #      （mates0 / mates1 / 复查 / unknown_mate_types）。
        #   ⇒ 真机单条配合 **300s**、120 条 **10 小时+**，且**低 CPU**（COM 编组等待）
        #      —— 看着像"卡死"，其实是慢。
        #   ⇒ 热路径只留：**CreateMate 返回的特征名**（+ 其 `GetErrorCode`）当「建上了没」
        #      的判据，并保留 `component_boxes()` 前后各一次（它便宜：58 件 ~0.3s）用于
        #      `moved` 判据。**配合总数**由计划末端 `check_mate_count` 把关，
        #      **站位**由 `box2_*` 全量把关 —— 单步不再做 O(#配合) 的计数。
        boxes0 = sw.component_boxes()          # ★ 建之前读一次（见 _moved_boxes 的文档）
        # ★★ 2026-10-07：`CreateMate` 的返回**有两种形状** —— 单个 `IFeature`，
        #   或 `(IFeature, errcode)` 二元组（不同 SW 绑定方式/版本）。而 `_call` 自己已经
        #   返回 `(结果, 错误串)` ⇒ 把结果直接当 feat 会在"二元组"那一型上拿到 tuple，
        #   后面 `member(feat, "Name")` **静默失败** ⇒ **误判成"没建上"**并白重试。
        #   （真机 run13 建成了 80 条、离线假件却全红，根因就是这个形状差异。）
        _res, err = _call(member, asm, "CreateMate", data)
        feat = _res[0] if isinstance(_res, tuple) else _res
        sw.clear_selection()
        boxes1 = sw.component_boxes()          # ★ 建之后再读一次
        new_name = ""
        if feat is not None:
            with contextlib.suppress(Exception):
                new_name = str(member(feat, "Name") or "")
        # 判据：CreateMate 明确回了新特征名 = "建上了"。
        fresh = [("Mate", new_name)] if new_name else []
        if fresh:
            break
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)
    if not fresh:
        raise SwError(
            f"CreateMate 没有产生任何新特征（配合没建上）。kind={kind}，"
            f"常量 {m['const']}={mtype}，align={align}，**已重试 {attempts} 次**\n"
            f"  ★ 最可能的三个原因：\n"
            f"   ① 两个实体选对了吗？（点必须落在**不同组件的**面上）\n"
            f"   ② 组件是否被固定得太死？（第一个组件默认 Fixed，"
            f"两块都固定时有些配合会解不出来 —— 试 float_component）\n"
            f"   ③ 该配合**冗余**？组件已经靠别的配合满足了这个关系，"
            f"SolidWorks 会拒绝再建一条（这时把它删掉才是对的）\n"
            f"  ★ 上一次选中的面（取证）：{how}"
            + (f"\n  ★ COM 报错：{err}" if err else ""))
    fec = 0
    if feat is not None:
        with contextlib.suppress(Exception):
            fec = int(member(feat, "GetErrorCode"))
    if fec:
        raise SwError(f"配合特征重建失败 {fec}"
                      f"（{FEATURE_ERROR_NAMES.get(fec, '未知')}）")
    out = {"kind": kind, "mate": m["const"], "align": align,
           "new_features": [list(x) for x in fresh],
           "notes": notes or None,
           "attempts": attempts,
           # ★ 取证：两个实体到底是"哪个组件的哪个面" —— 配合出问题时要能回溯
           "picked": how}
    # ★ 2026-10-06：**不再**在单步里 `mate_count()` 复查配合数（见本段头注②：那是
    #   O(#配合) 的整树浅走，是单条配合 300s 的主因之一）。「到底多没多一条配合」交给
    #   计划末端的 `check_mate_count`，「有没有把组件挪错」交给配合后的 `box2_*`
    #   全量站位校验 —— 端到端判据都还在，单步只留便宜的前后包围盒（`moved`）。
    # ★★ 「有效性」判据（真机 2026-10-03）：配合会把组件**挪走**，甚至翻转+自旋。
    #    挪动本身不是错（配合就是干这个的），但**没预期的**挪动会让后续按坐标取的点
    #    全部作废 —— 而那边报出来的却是"取点没命中"（指向错误方向）。
    #    所以这里**当场把位移报出来**；对齐用 aligned 时额外点名（那就是翻转的元凶）。
    # ★★★ 但必须区分「**没动**」与「**测不到**」：`IComponent2.GetBox` 读空时
    #    `component_boxes()` 回空表，两次都空 ⇒ `_moved_boxes` 也空 ⇒ 看起来"没动"。
    #    那是**假绿灯**（本项目第 84 条：校验器"通过"时也要怀疑校验器自己）。
    #    ⇒ 报 `measured`（成功读到包围盒的组件数），为 0 而装配体明明有组件 ⇒ 明确告警。
    measured = len(boxes0)
    n_comp = 0
    with contextlib.suppress(Exception):
        n_comp = sw.component_count()
    out["measured_components"] = measured
    if measured == 0 and n_comp > 0:
        out["boxes_unavailable"] = (
            f"★ 读不到任何组件的包围盒（`IComponent2.GetBox` 回空）⇒ "
            f"**没法判断组件有没有被挪动**。装配体里明明有 {n_comp} 个组件。"
            f"这不是「没动」，是「测不到」—— 别把它当成配合安全的证据。")
    moved = _moved_boxes(boxes0, boxes1)
    if moved:
        out["moved"] = moved
        out["moved_max_mm"] = moved[0]["dmax_mm"]
        if align == "aligned":
            notes.append(
                "★★ 有组件被移动/翻转了（见 moved）。align=aligned 在两个配合面法线**相反**"
                "（典型的\"上件下表面贴下件上表面\"：+Z vs −Z）时会**把组件翻 180°**，"
                "而重合类配合只约束 1 平移 + 1 转动 ⇒ 剩 2 个转动自由度会被**随机填**、"
                "于是还会**绕轴自旋**。⇒ 后续按坐标取的点很可能已作废。"
                "若这次翻转不是有意为之，把 align 改回 closest。")
            out["notes"] = notes
    # ★ 2026-10-06：原来这里每条配合都调一次 `unknown_mate_features()`（= 又一次
    #   O(#配合) 的整树浅走）—— 已移出热路径。类型名单的旁证由计划末端
    #   `check_mate_count`（它也会调 `unknown_mate_features()`）统一给出，够用。
    want = _p(step, ctx, "name")
    if want and feat is not None:
        with contextlib.suppress(Exception):
            feat.Name = str(want)
    return out


@op("check_component_count", "校验组件数", "verify",
    "数装配体里的组件个数。`toplevel_only` 默认 True（只数顶层，不含子装配体里的）。\n"
    "★ 判据直接来自 `IAssemblyDoc.GetComponentCount`，不是数特征树 —— 数树会把\n"
    "  子装配体里的零件也算进去。",
    api=["IAssemblyDoc.GetComponentCount"],
    params={"expect": {"type": "int", "required": False, "desc": "精确值（给它就比精确）"},
            "min": {"type": "int", "required": False, "default": 1},
            "max": {"type": "int", "required": False},
            "toplevel_only": {"type": "bool", "required": False, "default": True}},
    requires=("asm_open",), verified=True)
def op_check_component_count(sw, step, ctx):
    tlo = bool(_p(step, ctx, "toplevel_only", True))
    n = sw.component_count(toplevel_only=tlo)
    exp = _p(step, ctx, "expect")
    lo = int(_p(step, ctx, "min", 1) or 0)
    hi = _p(step, ctx, "max")
    if exp is not None:
        ok = n == int(exp)
    else:
        ok = n >= lo and (hi is None or n <= int(hi))
    comps = sw.components(toplevel_only=tlo)
    if not ok:
        raise SwError(f"组件数校验失败：实际 {n}，期望 "
                      + (f"{int(exp)}" if exp is not None else
                         f"[{lo}, {hi if hi is not None else '∞'}]")
                      + f"。当前组件：{[c.get('name') for c in comps]}")
    return {"component_count": n, "ok": True,
            "components": [c.get("name") for c in comps]}


@op("check_mate_count", "校验配合数", "verify",
    "数装配体里的配合个数。\n"
    "★ 按**结构**数，不是按类型名白名单数：「配合」文件夹（`MateGroup`）的\n"
    "   **直接子节点**就是配合 ⇒ 新增/未知类型**不会漏数**。\n"
    "★★ 为什么改掉白名单口径：白名单**必须穷举**，而 SolidWorks 的类型名有意外\n"
    "   （真机实测 `distance` 叫 **`MateDistanceDim`**，带 `Dim` 后缀；照直觉写\n"
    "   `MateDistance` 就会**静默漏数**）。穷举不全 = 假绿灯，本项目不接受。\n"
    "★ `MATE_FEATURE_TYPES`（已确认名单）降级为**旁证**：碰到不在名单里的类型名，\n"
    "   本 op 会在结果里 `unknown_types` **报出来**（不是错误），便于补名单。\n"
    "★ 只数「配合」文件夹的**直接**子节点；不含文件夹本身（`MATE_GROUP_TYPES` 扣掉）。\n"
    "★ 会遍历**每一个**「配合」文件夹（含**嵌套**子文件夹）—— 每个配合恰好归属一个文件夹，\n"
    "   **不漏不重**。\n"
    "★ 数目对不上时（或想看**完整**特征树）用 `scripts/sw_asm_tree.py` 打印核对 ——\n"
    "   它递归输出含子特征的树 + 组件表，是装配体的第一诊断工具。",
    api=["IModelDoc2.FirstFeature", "IFeature.GetFirstSubFeature"],
    params={"expect": {"type": "int", "required": False},
            "min": {"type": "int", "required": False, "default": 1},
            "max": {"type": "int", "required": False}},
    requires=("asm_open",), verified=True)
def op_check_mate_count(sw, step, ctx):
    n = sw.mate_count()
    exp = _p(step, ctx, "expect")
    lo = int(_p(step, ctx, "min", 1) or 0)
    hi = _p(step, ctx, "max")
    ok = (n == int(exp)) if exp is not None else (n >= lo and (hi is None or n <= int(hi)))
    feats = sw.mate_features()
    mates = [f["name"] for f in feats]
    unknown = sorted({f["type"] for f in sw.unknown_mate_features()})
    if not ok:
        raise SwError(f"配合数校验失败：实际 {n}，期望 "
                      + (f"{int(exp)}" if exp is not None else
                         f"[{lo}, {hi if hi is not None else '∞'}]")
                      + f"。当前配合：{mates}")
    return {"mate_count": n, "ok": True, "mates": mates,
            "types": sorted({f["type"] for f in feats}),
            "unknown_types": unknown or None}


@op("check_interference", "干涉检查", "verify",
    "对装配体做**干涉检查**（`IAssemblyDoc.InterferenceDetectionManager`）。\n"
    "★ 判据 = `GetInterferenceCount()`，**不是**\"UI 里看着贴合\"。\n"
    "★ `treat_coincident` 默认 **False** —— 面与面**重合**（贴面配合）**不算干涉**；\n"
    "  否则每一条贴合配合都会被报成干涉 ⇒ **告警疲劳**，真干涉会被淹没。\n"
    "  只有\"专门查零间隙/过盈\"时才打开它（`check_zero_clearance` 场景）。\n"
    "★ `include_multibody` 默认 **True** —— 单件多实体（轴承内圈/外圈/滚珠）之间的\n"
    "  干涉也算进去（它默认是关的，关着就会**漏报**）。\n"
    "★ 报出**干涉对数** + **参与组件名** + 每对的**干涉体积(mm³)**（取得到就报）。\n"
    "★ `allow` 是\"已声明且合理\"的干涉**白名单**：每项是 2 个组件名（可写基名，\n"
    "  实例号后缀会被忽略）。命中的对不再计入失败，但**会原样列在 `allowed_hits`**\n"
    "  —— 白名单的作用是\"可见地放过\"，不是\"让它消失\"。给 `allow` 时必须同时给 `why`。\n"
    "★★ `expect` 比 `allow` 强一档（2026-10-05 加）：逐对声明**参照值**，例如\n"
    "  `[{\"components\":[\"BASE\",\"COVER\"],\"volume_mm3\":9.7467,\"tol_pct\":15}]`。\n"
    "  **为什么需要**：白名单只保证\"出现的是已声明的对\"，**不保证干涉量没变**。\n"
    "  配合选错面（或位置微移）会让同一对干涉的**体积**从 9.75 变成别的数 ——\n"
    "  白名单照样放行。`expect` 会把\"体积对不上\"判成 FAIL，并报出参照值/实测值/偏差%。\n"
    "  同时**反向检查**：参照里有、实测没有的对，也算 FAIL（说明姿态和参照不一致）。\n"
    "★ `expect_zero` 默认 True：白名单之外的干涉数 > 0 即 FAIL。",
    api=["IAssemblyDoc.InterferenceDetectionManager",
         "IInterferenceDetectionMgr.GetInterferenceCount",
         "IInterferenceDetectionMgr.GetInterferenceComponents",
         "IInterferenceDetectionMgr.GetInterferences"],
    params={"expect_zero": {"type": "bool", "required": False, "default": True,
                            "desc": "True ⇒ 白名单外不得有任何干涉"},
            "treat_coincident": {"type": "bool", "required": False, "default": False,
                                 "desc": "把\"面重合\"也算干涉（查零间隙时打开）"},
            "include_multibody": {"type": "bool", "required": False, "default": True,
                                  "desc": "单件多实体之间的干涉也算（默认 True）"},
            "allow": {"type": "list", "required": False,
                      "desc": "已声明合理的干涉对，每项 2 个组件名"},
            "expect": {"type": "list", "required": False,
                       "desc": "逐对参照值：[{components:[a,b], volume_mm3, tol_pct?}]；"
                               "比 allow 强，会校验干涉体积"},
            "why": {"type": "str", "required": False,
                    "desc": "给 allow / expect 时**必须**写：为什么这些干涉是合理的"}},
    requires=("asm_open", "has_component"), verified=True)
def op_check_interference(sw, step, ctx):
    # ★★★ 本 op **必须排在计划最后**（2026-10-08 真机确诊，坑表 §128①）：
    #   跑过 `GetInterferences()` 之后，本会话的**编辑**动作会集体静默失效 ——
    #   `IFeature.Select2` 一律回 False、`IModelDoc2.EditDelete` 调了没反应
    #   ⇒ 之后的「删配合 / 删特征 / 选面建配合」全部失败，而**返回值照样是 True**
    #   （只能靠"回查特征树名字还在不在"发现）。
    #   真机定位：`_diag/delete_probe2.py` 分阶段探针 —— A~D 阶段的删除都成功，
    #   **E 阶段（跑过一次干涉检测之后）起的删除全失败**。
    #   ⇒ 与 §121「低 CPU 无进展」是同族：**"命令进行中"状态下 COM API 静默失效**。
    #   ⇒ 顺序固定：**先做完所有编辑 + 保存，再查干涉**（`finalize.py` 就是这么排的）。
    asm = sw.asm
    mgr, err = _call(member, asm, "InterferenceDetectionManager")
    if mgr is None:
        raise SwError(f"取不到 `InterferenceDetectionManager`（{err}）—— "
                      f"该 API 只在**装配体打开**时才有（当前 is_assembly="
                      f"{sw.is_assembly}）")
    tc = bool(_p(step, ctx, "treat_coincident", False))
    mb = bool(_p(step, ctx, "include_multibody", True))
    for name, val in (("TreatCoincidenceAsInterference", tc),
                      ("IncludeMultibodyPartInterferences", mb),
                      ("ShowIgnoredInterferences", False)):
        with contextlib.suppress(Exception):
            setattr(mgr, name, val)
    n, nerr = _call(member, mgr, "GetInterferenceCount")
    n = int(n or 0)
    # ---- 每对干涉：组件名（基名去实例号）+ 体积 ----
    def _base(nm: str) -> str:
        return re.sub(r"-\d+$", "", str(nm or ""))or str(nm or "")
    pairs: list[dict] = []
    raw = None
    with contextlib.suppress(Exception):
        raw = member(mgr, "GetInterferences")
    for it in (raw or []):
        pn, vol = [], None
        # ★★ `IInterference` **没有 ITypeInfo**（`dir()` 是空的、`GetTypeInfo` 报
        #   "无效索引"），只能靠 `GetIDsOfNames` 探名。真机探出的真实成员：
        #       `Components`(memid 3, 属性) / `IGetComponents`(memid 5) /
        #       `GetComponentCount`(memid 4) / `Volume`(memid 2)
        #   ★ 官方文档里的 **`GetComponents()` 在本版根本不存在** —— 照文档写会
        #     得到空组件名表（`AttributeError` 被 suppress 吞掉 ⇒ 静默只剩体积）。
        #   ⇒ 属性名是 `Components`，取不到再退 `IGetComponents(count)`。
        cs = None
        for acc in ("Components", "GetComponents", "GetInterferingComponents"):
            with contextlib.suppress(Exception):
                cs = member(it, acc)
                if cs is not None:
                    break
        if cs is None:
            with contextlib.suppress(Exception):
                cs = member(it, "IGetComponents", int(member(it, "GetComponentCount") or 0))
        with contextlib.suppress(Exception):
            for c in (cs or []):
                nm = None
                with contextlib.suppress(Exception):
                    nm = member(c, "Name2") or member(c, "Name")
                if nm:
                    pn.append(str(nm))
        with contextlib.suppress(Exception):
            vol = round(float(member(it, "Volume")) * 1e9, 4)
        pairs.append({"components": pn, "bases": [_base(x) for x in pn],
                      "volume_mm3": vol})
    if not pairs:                       # GetInterferences 不通时退回组件清单
        comps: list[str] = []
        with contextlib.suppress(Exception):
            for c in (member(mgr, "GetInterferenceComponents") or []):
                nm = None
                with contextlib.suppress(Exception):
                    nm = member(c, "Name2") or member(c, "Name")
                if nm:
                    comps.append(str(nm))
        if comps:
            pairs.append({"components": sorted(comps),
                          "bases": [_base(x) for x in comps], "volume_mm3": None,
                          "note": "（GetInterferences 没取到明细，这里是**参与组件清单**，"
                                  "不是逐对明细）"})
    allow = _p(step, ctx, "allow") or []
    expect = _p(step, ctx, "expect") or []
    why = _p(step, ctx, "why")
    if (allow or expect) and not why:
        raise SwError("给了 `allow` / `expect` 就**必须**写 `why` —— "
                      "放过干涉是要留证据的，不能只写名单")
    allow_sets = [frozenset(_base(x) for x in (a if isinstance(a, (list, tuple)) else [a]))
                  for a in allow]
    allowed_hits, violations = [], []
    for p in pairs:
        key = frozenset(p["bases"])
        (allowed_hits if key in allow_sets else violations).append(p)
    # ---- ★★ `expect`：逐对参照值（比 allow 强一档，会校验干涉**体积**）----
    exp_rows, exp_viol = [], []
    if expect:
        exp_map = {}
        for e in expect:
            if not isinstance(e, dict):
                raise SwError(f"`expect` 每项必须是对象，实得 {e!r}")
            cs = e.get("components") or []
            if len(cs) != 2:
                raise SwError(f"`expect.components` 必须是 2 个组件名，实得 {cs!r}")
            exp_map[frozenset(_base(x) for x in cs)] = e
        seen: set = set()
        for p in pairs:
            key = frozenset(p["bases"])
            e = exp_map.get(key)
            if e is None:
                continue
            seen.add(key)
            ev, tol = e.get("volume_mm3"), float(e.get("tol_pct", 10.0) or 10.0)
            av = p.get("volume_mm3")
            dev = None
            if ev is not None and av is not None and float(ev) != 0.0:
                dev = round((av - float(ev)) / abs(float(ev)) * 100.0, 3)
            ok = (ev is None or av is None or dev is None or abs(dev) <= tol)
            row = {"components": p["components"], "ref_mm3": ev, "got_mm3": av,
                   "dev_pct": dev, "tol_pct": tol, "ok": ok}
            exp_rows.append(row)
            if not ok:
                exp_viol.append(f"{'+'.join(p['bases'])}：参照 {ev}mm³，实测 {av}mm³"
                                f"（偏差 {dev}% > 容差 {tol}%）")
        for key, e in exp_map.items():
            if key in seen:
                continue
            exp_viol.append(f"{'+'.join(sorted(key))}：参照里有（{e.get('volume_mm3')}mm³），"
                            f"**实测没有** ⇒ 姿态与参照不一致")
    out = {"interference_count": n, "pairs": pairs or None,
           "treat_coincident": tc, "include_multibody": mb,
           "allowed_hits": allowed_hits or None,
           "allowed_why": why or None,
           "expect_rows": exp_rows or None,
           "expect_violations": exp_viol or None,
           "violations": violations or None}
    if nerr is not None:
        out["notes"] = [f"GetInterferenceCount 报错：{nerr}"]
    if exp_viol:
        raise SwError("干涉检查**与参照值对不上**：\n  · " + "\n  · ".join(exp_viol[:8])
                      + "\n  排查：① 配合选中的面是不是**错的那个**（视觉上\"差不多了\"）；"
                      f"② 组件被配合挪走了（`add_mate` 的 `moved`）；"
                      f"③ 零件几何与参照件本来就不同 ⇒ 那是零件问题。")
    if bool(_p(step, ctx, "expect_zero", True)) and violations:
        det = "；".join("+".join(p["components"]) +
                        (f"（{p['volume_mm3']}mm³）" if p["volume_mm3"] is not None else "")
                        for p in violations[:8])
        raise SwError(f"干涉检查**不通过**：{len(violations)} 对未声明的干涉 —— {det}。\n"
                      f"  排查：① 配合选的面/对齐方向对不对（`align=closest` 别乱用 aligned）；"
                      f"② 组件是不是**被配合挪过**（看上一步 `add_mate` 的 `moved`）；"
                      f"③ 零件本身的几何是不是就冲突（在**参照装配体**里同样会冲突 ⇒ 那是"
                      f"零件/图纸问题，不是装配问题）。")
    return out


@op("check_component_box", "校验组件站位", "verify",
    "把一个命名组件的**装配体全局包围盒**跟期望值比 —— 验证\"它真的被放到该在的位置\"。\n"
    "★ 为什么不能只看配合数/mate_count：配合建上了 ≠ **位置对**（配合可能解成翻转/自旋，\n"
    "  或者选中的是两个\"形状相似但位置错\"的面，见坑表 §94/§96）。\n"
    "  包围盒是**与配合语义无关**的独立证据。\n"
    "★ 两种期望写法（都给 mm，**装配体全局坐标**，非零件自身坐标）：\n"
    "    · `expect`      = `[x0,y0,z0,x1,y1,z1]` 完整包围盒（min 角 + max 角）\n"
    "    · `expect_size` = `[dx,dy,dz]`        只比**尺寸**（不比朝向/位置）\n"
    "  两个都给时**都校验**。\n"
    "★ `tol_mm` 默认 0.5mm（真机 `IComponent2.GetBox` 精度足够；别默认到 5mm 去盖误差）。\n"
    "★ 组件名可写**基名**（`箱盖`）或全名（`箱盖-1`）；有歧义会报错，**绝不猜**。",
    api=["IComponent2.GetBox", "IAssemblyDoc.GetComponents"],
    params={"component": {"type": "str", "required": True,
                          "desc": "组件名（基名或全名）"},
            "expect": {"type": "list", "required": False,
                       "desc": "[x0,y0,z0,x1,y1,z1] 全局包围盒（mm）"},
            "expect_size": {"type": "list", "required": False,
                            "desc": "[dx,dy,dz] 尺寸（mm）"},
            "tol_mm": {"type": "float", "required": False, "default": 0.5}},
    requires=("asm_open", "has_component"), verified=True)
def op_check_component_box(sw, step, ctx):
    want = str(_p(step, ctx, "component") or "")
    if not want:
        raise SwError("check_component_box 必须给 component")
    full, _obj, cands = sw.resolve_component(want)
    if full is None:
        have = [c.get("name") for c in sw.components() if c.get("name")]
        if cands:
            raise SwError(f"组件名 '{want}' **有歧义**，匹配到 {cands} —— 请写全名")
        raise SwError(f"组件名对不上：'{want}'。当前装配体里只有 {have}")
    boxes = sw.component_boxes()
    box = boxes.get(full)
    if box is None:
        raise SwError(f"读不到组件 '{full}' 的包围盒（`IComponent2.GetBox` 回空）；"
                      f"能读到的有 {sorted(boxes)}")
    size = [round(box[3] - box[0], 4), round(box[4] - box[1], 4), round(box[5] - box[2], 4)]
    tol = float(_p(step, ctx, "tol_mm", 0.5) or 0.5)
    exp = _p(step, ctx, "expect")
    exp_size = _p(step, ctx, "expect_size")
    if exp is None and exp_size is None:
        raise SwError("要给 `expect`（6 个数）或 `expect_size`（3 个数）之一")
    out = {"component": full, "box_mm": box, "size_mm": size, "tol_mm": tol}
    bad: list[str] = []
    if exp is not None:
        e = [float(v) for v in exp]
        if len(e) != 6:
            raise SwError(f"expect 必须是 6 个数 [x0,y0,z0,x1,y1,z1]（实得 {len(e)}）")
        d = [round(a - b, 4) for a, b in zip(box, e)]
        out["expect"] = e
        out["diff_mm"] = d
        if max(abs(v) for v in d) > tol:
            bad.append(f"包围盒差 {d}（容差 ±{tol}）")
    if exp_size is not None:
        es = [float(v) for v in exp_size]
        if len(es) != 3:
            raise SwError(f"expect_size 必须是 3 个数（实得 {len(es)}）")
        ds = [round(a - b, 4) for a, b in zip(size, es)]
        out["expect_size"] = es
        out["size_diff_mm"] = ds
        if max(abs(v) for v in ds) > tol:
            bad.append(f"尺寸差 {ds}（容差 ±{tol}）")
    if bad:
        raise SwError(f"组件 '{full}' 站位校验失败：" + "；".join(bad)
                      + f"\n  实测包围盒 {box}\n  实测尺寸   {size}")
    out["ok"] = True
    return out


@op("asm_face_probe", "装配体取面体检（坐标→面）", "verify",
    "**只读**地把「全局坐标能不能取到面」这件事摊开：\n"
    "  ① 每个组件一行 —— 局部包围盒 lbox / 全局包围盒 gbox / 判定出的坐标约定 `mode` /\n"
    "     判别误差 `mode_err`（**角点探针**：把全局盒 8 角点变换回局部再与 lbox 比，\n"
    "     挑偏差最小的约定）；\n"
    "  ② 给 `points` 时，对每个点报出：落在哪个组件、该组件用的 mode、换算后的局部坐标、\n"
    "     是否在 lbox 内、最近面距离多少 —— 用来回答「为什么这个点取不到面」。\n"
    "★★ 为什么必须有这个 op：`_asm_face_index` 的约定判别一旦挑错，取点会被**静默**映射到\n"
    "   **镜像位置**，报出来却是「点不在任何面上 / 坐标大概不是全局坐标」——**报错方向完全错**。\n"
    "   这个 op 把判别过程和误差**打印出来**，让「约定错」和「坐标错」当场分开。",
    api=["IComponent2.GetBox", "IComponent2.Transform2", "IFace2.GetClosestPointOn",
         "IFace2.GetBox"],
    params={"points": {"type": "json", "required": False,
                       "desc": "要试的点列表 [[x,y,z],...]（装配体全局 mm）"},
            "toplevel_only": {"type": "bool", "required": False, "default": True,
                              "desc": "是否只下探一层组件"}},
    requires=("asm_open",), verified=True)
def op_asm_face_probe(sw, step, ctx):
    toplevel = bool(_p(step, ctx, "toplevel_only", True))
    idx = _asm_face_index(sw, toplevel)
    rows = []
    src = idx[0].get("mode_source") if idx else None
    votes = idx[0].get("votes") if idx else None
    print(f"  组件面索引：{len(idx)} 个组件；约定来源 = {src}"
          + (f"，判别确定组件的投票 {votes}" if votes else ""))
    for rec in idx:
        lb = rec["lbox"]
        errmm = None if rec["mode_err"] is None else rec["mode_err"] * 1000.0
        alt = None if rec.get("mode_err_alt") is None else rec["mode_err_alt"] * 1000.0
        rows.append({"comp": rec["comp"], "mode": rec["mode"],
                     "mode_local": rec.get("mode_local"),
                     "mode_mismatch": bool(rec.get("mode_mismatch")),
                     "mode_source": rec.get("mode_source"),
                     "mode_err_mm": None if errmm is None else round(errmm, 4),
                     "mode_err_alt_mm": None if alt is None else round(alt, 4),
                     "mode_soft": bool(rec.get("mode_soft")),
                     "n_faces": len(rec["faces"]),
                     "lbox_mm": None if lb is None else [round(v * 1000, 3) for v in lb]})
        print(f"    · {rec['comp']:<28} mode={rec['mode']} "
              f"err={'-' if errmm is None else f'{errmm:.4f}':>9}"
              f"/alt={'-' if alt is None else f'{alt:.4f}':>9}mm "
              f"{'SOFT ' if rec.get('mode_soft') else '     '}"
              f"{'MISMATCH(local=%s) ' % rec['mode_local'] if rec.get('mode_mismatch') else ''}"
              f"faces={len(rec['faces']):<4} "
              f"lbox={'?' if lb is None else [round(v * 1000, 1) for v in lb]}")
    # 约定判别可疑的组件单独列出来（判据与 `_select_asm_face_at` 一致）
    bad = [r["comp"] for r in rows
           if r["mode_err_mm"] is not None and r["mode_err_mm"] > 2.0]
    soft = [r["comp"] for r in rows if r["mode_soft"]]
    mism = [f"{r['comp']}(逐组件判过 mode={r['mode_local']})" for r in rows
            if r["mode_mismatch"]]
    if src and src.startswith("per-component"):
        print(f"  ★★★ **没有任何组件能确定约定**（{src}）—— 取点全部不可信，"
              f"先加一个「盒子不对称 + 90° 倍数摆放」的组件再判")
    if mism:
        print(f"  ★ 逐组件判断与全局投票**不一致**（已按全局约定统一，"
              f"这里列出来是为了可见）：{mism}")
    if bad:
        print(f"  ★★ 盒级探针误差大（> 2mm，说明该零件「旋转盒 vs 旋转实体」差异大，"
              f"探针对它没有判别力；以全局投票 + 配合后站位为准）：{bad}")
    if soft:
        print(f"  ★ 判别余量小（两种约定误差接近 < 0.5mm ⇒ 该组件不携带约定信息）：{soft}")
    pts = _p(step, ctx, "points") or []
    pt_rows = []
    for p in pts:
        pm = [float(v) for v in p]
        pm_m = [mm(v) for v in pm]
        hit = []
        for rec in idx:
            if rec["xf"] is None or rec["lbox"] is None:
                continue
            lp = _to_local(rec["xf"][0], rec["xf"][1], pm_m, rec["mode"])
            lb = rec["lbox"]
            inside = (lb[0] - 1e-3 <= lp[0] <= lb[3] + 1e-3
                      and lb[1] - 1e-3 <= lp[1] <= lb[4] + 1e-3
                      and lb[2] - 1e-3 <= lp[2] <= lb[5] + 1e-3)
            if not inside:
                continue
            best_d, best_fe = None, None
            for k, fe in enumerate(rec["faces"]):
                with contextlib.suppress(Exception):
                    cp = member(fe, "GetClosestPointOn", lp[0], lp[1], lp[2])
                    if cp is not None and len(cp) >= 3:
                        d = math.dist([float(v) for v in cp[:3]], lp)
                        if best_d is None or d < best_d:
                            best_d, best_fe = d, fe
            hit.append({"comp": rec["comp"], "mode": rec["mode"],
                        "local_mm": [round(v * 1000, 3) for v in lp],
                        "nearest_face_kind": _face_kind(best_fe) if best_fe is not None else None,
                        "nearest_mm": None if best_d is None else round(best_d * 1000, 4)})
        hit.sort(key=lambda h: (h["nearest_mm"] is None, h["nearest_mm"] or 0))
        pt_rows.append({"point": pm, "candidates": hit})
        print(f"  点 {pm} → 落在 {len(hit)} 个组件的局部盒内"
              + (f"，最近：{hit[0]['comp']} 距面 {hit[0]['nearest_mm']}mm"
                 f"（mode={hit[0]['mode']}，{hit[0]['nearest_face_kind']}面，"
                 f"局部 {hit[0]['local_mm']}）" if hit else
                 "（**一个都没有** ⇒ 坐标不是全局？或约定挑错见上）"))
    return {"components": rows, "mode_source": src, "mode_votes": votes,
            "mode_suspect": bad or None, "mode_soft": soft or None,
            "mode_mismatch": mism or None, "points": pt_rows}


@op("inspect_assembly", "装配体体检", "verify",
    "只读地把装配体现状打出来：组件表（名字/路径/配置/固定/抑制）+ 完整特征树\n"
    "（**含子特征** —— 配合在「配合」文件夹的子特征里，只看顶层看不到）。\n"
    "★ 这是「装配体为什么装不上」的第一诊断工具：先看有几个组件、各自固定没固定。",
    api=["IAssemblyDoc.GetComponents", "IModelDoc2.FirstFeature",
         "IFeature.GetFirstSubFeature"],
    params={"tree": {"type": "bool", "required": False, "default": True,
                     "desc": "是否连同完整特征树一起打印"}},
    requires=("asm_open",), verified=True)
def op_inspect_assembly(sw, step, ctx):
    comps = sw.components()
    tree = sw.feature_tree() if bool(_p(step, ctx, "tree", True)) else []
    print(f"  组件 {len(comps)} 个（GetComponentCount="
          f"{sw.component_count()}）：")
    for c in comps:
        print(f"    · {c.get('name'):<28} fixed={c.get('fixed')} "
              f"suppressed={c.get('suppressed')} config={c.get('config')}")
        print(f"      路径 {c.get('path')}")
    if tree:
        print("  特征树（含子特征，前 60 条）：")
        for f in tree[:60]:
            print("    " + "  " * f["depth"] + f"[{f['type']}] {f['name']}")
    return {"components": comps, "feature_tree": tree,
            "component_count": sw.component_count(),
            "mate_types_calibrated": bool(sw.MATE_FEATURE_TYPES)}


# ============================================================ M1 零件层补全
#
# 本节 op 的落地流程严格走 references/50-training.md 的三步法：
#   ① 抽签名  python scripts/sw_apisig.py --sig IFeatureManager.FeatureRevolve2
#   ② 写 op   本文件（verified 先标 False）
#   ③ 真机双证 跑 assets/plans/_probe_*.json → sw_shot 截图 + sw_inspect 读数 → 才改 True
#
# ⚠ 抽签名时纠正了 4 个**矩阵文档里写错的 API 名**。别照旧文档写代码：
#   · 拔模     不是 IFeatureManager.InsertDraft        → InsertMultiFaceDraft
#   · 筋       不是 IFeatureManager.InsertRib（它零参数）→ IModelDoc2.InsertRib2（9 参）
#   · 草图镜像 不是 ISketchManager.SketchMirror        → IModelDoc2.SketchMirror（零参数命令）
#   · 直槽     不是 ISketchManager.CreateStraightSlot  → ISketchManager.CreateSketchSlot
#   另外 FeatureRevolveCut2 已被官方标为废弃（用 FeatureRevolve2 + IsCut），
#   AddMate3 也已废弃（用 AddMate4）—— 详见 references/30-pitfalls.md §24。


def _C(sw, name: str, fallback: int = 0) -> int:
    """取常量值。**取不到不抛异常**，退回 fallback ——
    一个枚举名拼错不该让整个 op 崩掉（常量表来源见 sw_connect.load_constants_from_tlb）。"""
    with contextlib.suppress(Exception):
        v = sw.C(name)
        if v is not None:
            return int(v)
    return fallback


def _call(fn, *args, **kwargs):
    """调一个 COM 方法，**把异常抓下来**而不是吞掉。返回 (结果, 错误串或 None)。

    为什么要它：M1 新写的 op 一度把 API 调用包在 `contextlib.suppress(Exception)` 里，
    于是「参数传错 / 对象为空 / 选择不对」这类**真错误全被吞了**，只剩一句
    「没生成特征」——真机排查时白跑了好几轮。凡是「调 API 看它成不成」的地方，
    都该用它，并把 error 记进 notes/异常里。
    """
    try:
        return fn(*args, **kwargs), None
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


#: ★★ COM 重试次数与间隔（2026-10-03 真机加的）。
#: 背景：同一个 `AddComponent5` / `SelectByID2` 调用，**同一会话、同一零件**，
#:   实测**时好时坏** —— 09:47 插不进 `fresh.SLDPRT`，09:52 同一文件插得进；
#:   同一次 `add_mate` 里第一个取点 (20,0,0) 命中、第二个 (80,0,10) 不命中，
#:   而下一个探针里两个都命中。**不抛异常、不给错误码、组件数/选中数不涨。**
#: ⇒ 这是 SolidWorks COM 层的**瞬时失败**，不是"参数写错了"。
#:    既然本项目的判据一律是**读回**（组件数涨没涨 / 选中数涨没涨），
#:    那正确的做法就是：**没涨就重试**，重试仍不涨才报错。
#: ★ 不是"掩盖错误"：重试次数会写进返回值（`attempts`），日志里看得见。
#: ★ 间隔给到 0.35s：真机上一次失败的 AddComponent5 之后，UI 线程需要一点时间。
COM_RETRY = 3
COM_RETRY_S = 0.35


def _pick(sw, item, mark: int = 0, append: bool = True) -> bool:
    """按名字或**坐标**选中一个实体。item 两种写法：

        "Sketch1"                                  → 按名字选
        {"type": "EDGE", "point": [0, 0, 12]}      → 坐标射线拾取（脚本里最实用，边/面没名字）
        {"name": "上视基准面", "type": "PLANE"}     → 显式名字 + 类型

    坐标一律 mm，且必须是**全局坐标**（不是草图坐标）。
    坐标拾取带 swSelectOptionExtensive，落在棱上/面上都能命中。

    ★★ 2026-10-03：一次失败**不代表点错了**，会重试 `COM_RETRY` 次。
      真机上 `SelectByID2` 存在瞬时失败（同一点、同会话，有时命中有时不命中），
      而"点算错了"和"这次没打中"报出来是同一句话 ⇒ 只能靠重试区分。
    """
    if isinstance(item, str):
        item = {"name": item}
    typ = str(item.get("type") or "FACE").upper()
    nm = item.get("name") or ""
    pt = item.get("point")
    if pt:
        x, y, z = (mm(float(v)) for v in pt)
        opt = _C(sw, "swSelectOptionExtensive", 1)
    else:
        x = y = z = 0.0
        opt = 0
    for _ in range(COM_RETRY):
        with contextlib.suppress(Exception):
            if bool(sw.ext.SelectByID2(nm, typ, x, y, z, append, mark, None, opt)):
                return True
        # ★ 失败时不留残 selection：下一次 append 会叠加到脏选择集上
        if not append:
            with contextlib.suppress(Exception):
                sw.clear_selection()
        time.sleep(COM_RETRY_S)
    return False


def _pick_all(sw, items, mark: int = 0) -> tuple[int, list]:
    """批量拾取，返回 (命中数, 没命中的项)。"""
    hit, miss = 0, []
    for it in items or []:
        if _pick(sw, it, mark=mark, append=True):
            hit += 1
        else:
            miss.append(it)
    return hit, miss


# ------------------------------------------------- 实体 / 边 / 面 枚举与「坐标最近邻」选择
#
# 为什么单开一节：fillet / chamfer / shell / 阵列 这类 op 的成败**几乎全在选择上**，
# 而原来的「按相机射线拾取」不可复现 —— 换个视角、换个窗口比例，同一份计划就选不中。
# 这里改成**直接遍历几何体、按坐标最近邻匹配对象**，再调 `IEntity.Select4` 选中：
# 不经过相机，同样的计划在任何视图下结果一致（2026-09-25，M2）。
#
# 坐标一律 **mm**（对外），内部换算成米；边/面对象自身没有名字，只能靠坐标认领。

def _bodies(sw) -> list:
    """当前零件的全部**实体**（IBody2）。

    ★★ 2026-10-02：改成**只走 `sw.bodies()`**（唯一取实体入口）。理由是这个文件里
      原来同时存在两套取法（本函数 `(-1, False)` / 别处 `(0, True)`），
      而 `(0, True)` 是「只取可见」的变体 —— 实体一被抑制就回空，
      于是 `check_bbox` 报「零件可能是空的」（真机踩过，见 `sw_connect.bodies()`）。
      单一入口之后，回退链（实体含隐藏 → 全体含隐藏 → 只可见）只维护一份。
    ★ 语义差别：原来这里是 `-1`（含曲面体），现在是 `(0, False)`（只实体）优先；
      纯实体零件两者**逐位相同**，只有"实体 + 曲面体"混合件会少掉曲面体 ——
      对一个实体建模流程来说那正是想要的。
    """
    out: list = []
    with contextlib.suppress(Exception):
        for b in sw.bodies():
            be = typed(b, "IBody2")
            if be is not None:
                out.append(be)
    return out


def _edge_mid(edge) -> list | None:
    """边的中点（全局坐标，**米**）。

    两个端点顶点取平均；**闭环边**（圆柱口那种）没有顶点，退到曲线参数中点。
    这一步是「按坐标认领边」的全部依据 —— 拿不到中点的边会被静默跳过。
    """
    pts: list = []
    for meth in ("GetStartVertex", "GetEndVertex"):
        with contextlib.suppress(Exception):
            v = typed(member(edge, meth), "IVertex")
            p = member(v, "GetPoint") if v is not None else None
            if p is not None and len(p) >= 3:
                pts.append([float(p[0]), float(p[1]), float(p[2])])
    if len(pts) == 2:
        return [(pts[0][i] + pts[1][i]) / 2.0 for i in range(3)]
    if pts:
        return pts[0]
    with contextlib.suppress(Exception):
        cp = member(edge, "GetCurveParams2")
        cur = typed(member(edge, "GetCurve"), "ICurve")
        if cur is not None and cp is not None and len(cp) >= 2:
            ev = member(cur, "Evaluate2", (float(cp[0]) + float(cp[1])) / 2.0, 0)
            if ev is not None and len(ev) >= 3:
                return [float(ev[0]), float(ev[1]), float(ev[2])]
    return None


def _edges(sw) -> list:
    """全部边，每条带中点。返回 [{"obj": IEdge, "mid": [x,y,z] 米}, …]。"""
    out: list = []
    for b in _bodies(sw):
        with contextlib.suppress(Exception):
            for e in (member(b, "GetEdges") or ()):
                ee = typed(e, "IEdge")
                if ee is not None:
                    out.append({"obj": ee, "mid": _edge_mid(ee)})
    return out


def _faces(sw, include_surfaces: bool = False) -> list:
    """全部面，每个带面积/法向/包围盒中心。返回 [{"obj","area","normal","box"}, …]。

    法向量在后期绑定下是**属性 vs 方法**的歧义点（`IFace2.Normal` 有 get 也有 put），
    拿不到就留空 —— 下面的选择逻辑会把没有法向的面跳过，而不是抛错。

    ★★ `include_surfaces`（2026-10-11 新增，默认 **False**）：默认只走 `_bodies(sw)`
      ——**只有实体**。曲面零件（`thicken`/`surface_kit` 之前的状态）用默认值会拿到
      **空列表**（"一个面都没有"）。要数/选**曲面**上的面（`split_line` 的目标常是曲面），
      必须 `include_surfaces=True`（走 `sw.bodies(swAllBodies)`）。
      ★ 默认值保持 False 是为了不动 `fillet`/`chamfer`/`shell` 的既有行为。
    """
    out: list = []
    if include_surfaces:
        bodies = [typed(b, "IBody2")
                  for b in sw.bodies(body_type=int(sw.C("swAllBodies")))]
    else:
        bodies = _bodies(sw)
    for b in bodies:
        if b is None:
            continue
        with contextlib.suppress(Exception):
            for f in (member(b, "GetFaces") or ()):
                fe = typed(f, "IFace2")
                if fe is None:
                    continue
                rec = {"obj": fe, "area": 0.0, "normal": None, "box": None}
                with contextlib.suppress(Exception):
                    rec["area"] = float(member(fe, "GetArea"))
                with contextlib.suppress(Exception):
                    n = member(fe, "Normal")
                    if isinstance(n, (tuple, list)) and len(n) >= 3:
                        rec["normal"] = [float(n[0]), float(n[1]), float(n[2])]
                with contextlib.suppress(Exception):
                    bx = member(fe, "GetBox")
                    if bx is not None and len(bx) >= 6:
                        rec["box"] = [float(x) for x in bx[:6]]
                out.append(rec)
    return out


def _select_obj(obj, append: bool) -> bool:
    """选中一个几何对象（边/面）。`Select4` 定义在 IEntity 上，IEdge/IFace2 都继承它。

    ★★★ **选面/选边一律走这里，别直接 `member(face, "Select4", append, None)`**
    （2026-10-08 真机确诊，坑表 §128②）：`IComponent2.GetBody().GetFaces()` 回的是
    **无类型信息**的 dispatch，对它直接调 `Select4(Append, Callout)` 会回
    `pywintypes.com_error: (-2147352571, '类型不匹配。', None, 2)`
    （`DISP_E_TYPEMISMATCH`，报错栈是 `File "<COMObject GetFaces>", line 2, in Select4`）
    —— 因为 `Callout` 是 `ICallout` 接口指针，pywin32 在**没有类型信息**时把 `None` 编组不过去。
    本函数里的 `typed(obj, "IEntity")` + suppress 重试就是为了吃掉这一条；写在外面等于白给。
    """
    if obj is None:
        return False
    for target in (obj, typed(obj, "IEntity")):
        if target is None:
            continue
        with contextlib.suppress(Exception):
            if member(target, "Select4", append, None):
                return True
    return False


# ============================================================ 装配体面的枚举与选取
#
# ★★ 为什么零件层那套 `_faces()` 在装配体里不能用（2026-10-03 真机确诊）：
#   `_faces()` 走 `sw.bodies()` —— 那是**零件** API，对 .SLDASM **恒为空**。
#   而坐标拾取 `SelectByID2` 也不能凑合：它是**相机射线拾取**，**只能选中朝向
#   相机���面**。真机实测（默认等轴测视图，看的是 +X / -Y / +Z）：
#       上块 -X 面 (80,*,*)   → **恒选不中**（背面）
#       上块 +Y 面 (100,15,10)→ **恒选不中**（背面）
#       上块 +X / -Y / 底面     → 选得中
#   ⇒ "点算错了"和"这个面背着相机"报出来是同一句话。
#   ⇒ 装配体的坐标选面**必须**走"枚举 + IEntity.Select4"（与视角无关），
#      这正是零件层 2026-09-25（M2）已经做对的事，这里只是补上装配体这一层。
#
# 坐标换算：组件的面在**零件局部坐标**里，要参与"全局坐标最近邻"必须先过
# `IComponent2.Transform2`（MathTransform.ArrayData，16 个数：3×3 旋转 + 3 平移）。
# ★ 旋转矩阵的**行/列约定**在 COM 文档里没写清，且写错的话结果是"面全跑到
#   镜像位置"—— 所以这里**两种约定都算、用组件的全局包围盒自检挑对的那个**
#   （`IComponent2.GetBox` 是装配体全局坐标，可以当下标尺）。

def _xform_of(c) -> tuple[list, list] | None:
    """取组件的刚体变换。返回 `(R, t)`：R 是 3×3 行主序列表，t 是 3 元平移（米）。

    `IComponent2.Transform2` 是**属性**、返回 `MathTransform`；回退 `GetTransform`。
    `MathTransform.ArrayData` 是 16 个 double：前 9 个旋转、后 3 个平移。
    """
    m = None
    for api in ("Transform2", "GetTransform"):
        with contextlib.suppress(Exception):
            v = member(c, api)
            if v is not None:
                m = v
                break
    if m is None:
        return None
    arr = None
    for api in ("ArrayData", "GetArrayData"):
        with contextlib.suppress(Exception):
            v = member(m, api)
            if v is not None:
                arr = [float(x) for x in (v if isinstance(v, (list, tuple)) else [])]
                break
    if not arr or len(arr) < 12:
        return None
    R = [arr[0:3], arr[3:6], arr[6:9]]
    t = arr[9:12]
    return R, t


def _face_kind(face) -> str:
    """面所属**曲面类型**（口径与 `sw_faces.py` 一致，含"未识别"这一档）。

    ★ 为什么必须能报出来（坑表 §120⑪）：真螺纹的螺纹面在本机
    `IsPlane/IsCylinder/IsCone/IsTorus/IsSphere/IsRevolved` **全是 False**
    ⇒ 只剩 `Identity()` 的数字（本机螺纹 = 4006）。看不到这一档，
    "取点取到了螺纹上"就完全不可见 —— 而它导致的失败是
    「同心配合被拒建」，报错完全指不到点。
    """
    srf = None
    with contextlib.suppress(Exception):
        srf = typed(member(face, "GetSurface"), "ISurface")
    if srf is None:
        return "?"
    for api, tag in (("IsPlane", "平面"), ("IsCylinder", "圆柱"),
                     ("IsCone", "圆锥"), ("IsTorus", "环面"),
                     ("IsSphere", "球"), ("IsRevolved", "回转"),
                     ("IsSwept", "扫掠"), ("IsExtrusion", "拉伸面"),
                     ("IsBSurface", "样条面")):
        with contextlib.suppress(Exception):
            if bool(member(srf, api)):
                return tag
    with contextlib.suppress(Exception):
        return f"未识别曲面(Identity={int(member(srf, 'Identity'))})"
    return "?未识别"


def _to_local(R, t, p, mode: int) -> list[float]:
    """装配体**全局**坐标 → 零件**局部**坐标（`p` 与返回值单位都是米）。

    `mode` 取 0/1 两种约定（COM 文档没写清 `ArrayData` 是行主序还是列主序；
    写错的后果是"点被映射到镜像位置"⇒ 面选不中或选错面，而且**不报错**）。
    ★ 约定由**运行时自检**定下来（见 `_asm_face_index`），不靠猜。
    """
    q = [p[i] - t[i] for i in range(3)]
    if mode == 0:      # 局部 = Rᵀ · (全局 − t)
        return [R[0][0] * q[0] + R[1][0] * q[1] + R[2][0] * q[2],
                R[0][1] * q[0] + R[1][1] * q[1] + R[2][1] * q[2],
                R[0][2] * q[0] + R[1][2] * q[1] + R[2][2] * q[2]]
    # 局部 = R · (全局 − t)
    return [R[0][0] * q[0] + R[0][1] * q[1] + R[0][2] * q[2],
            R[1][0] * q[0] + R[1][1] * q[1] + R[1][2] * q[2],
            R[2][0] * q[0] + R[2][1] * q[1] + R[2][2] * q[2]]


def _asm_face_index(sw, toplevel_only: bool = True) -> list[dict]:
    """装配体里每个组件的 `{comp, faces, fbox, lbox, xf, mode, mode_err}`（坐标：米）。

    ★★ 三个真机踩出来的点（缺一个就**静默**选不中，所以写在最前面）：
      ① 取实体用 **`IComponent2.GetBody()`** —— **没有 `GetBody2`**。
         照 `IModelDoc2` 的 `GetBodies2` 想当然，会得到"组件取不到面"，
         而**不抛异常**（签名以 `sldworks.tlb` 为准：只有 `GetBody` / `IGetBody`）。
      ② `IFace2.GetClosestPointOn(x, y, z)` 收的是**零件局部坐标**，不是装配体
         全局坐标。证据（零件局部 x∈[0,40] 的盒子）：
             全局 (80,0,10)   → 回 **40.000mm**（= 80 − 40）
             全局 (100,0,5)   → 回 **60.000mm**
             全局 (120,0,10)  → 回 **80.000mm**
             全局 (50,50,50)  → 回 **45.826mm**（= √(10²+20²+40²)）
         逐位吻合 ⇒ 它把全局坐标当局部坐标算距离。**必须先做全局→局部逆变换**。
      ③ `ArrayData` 的旋转约定没文档 ⇒ 两种都试，用"点必须落进零件局部包围盒"
         当自检（`_asm_face_index` 里的 `mode`）。
      ★★★ ③ 的**判别探针必须用包围盒的 8 个角点，不能用盒心**（2026-10-05 真机改）：
        旧实现拿**盒心**当探针 —— 当"盒心在两种约定下映射后都落在 lbox 内"时**无法判别**，
        于是静默挑 `mode=0`；而真机里 `26-A型圆形销` / `12-箱盖` 恰好是 **mode=1**，
        盒心又恰好两种都过 ⇒ 挑错约定 ⇒ 之后每次按坐标取点都被映射到**镜像位置**，
        报出来却是「点不在任何面上 / 坐标大概不是全局坐标」——**报错方向完全错**。
        角点探针的判据 = 「把全局包围盒 8 角点变换回局部后的包围盒」与真实局部包围盒
        `lbox` 的**最大偏差最小**；误差写进 `mode_err`（挑错的典型量级是 5~15mm，
        挑对的通常 < 0.05mm），因此**可以事后核对**，不再是"静默"。
    """
    out: list[dict] = []
    for c in sw.component_objects(toplevel_only):
        nm = None
        with contextlib.suppress(Exception):
            nm = str(member(c, "Name2") or member(c, "Name"))
        body = None
        for api in ("GetBody", "IGetBody"):
            with contextlib.suppress(Exception):
                body = member(c, api)
                if body is not None:
                    break
        if body is None:
            continue
        faces = []
        with contextlib.suppress(Exception):
            faces = [typed(f, "IFace2") for f in (member(body, "GetFaces") or ())]
        faces = [f for f in faces if f is not None]
        if not faces:
            continue
        # ★ 逐面局部包围盒（米）：用来在取点时**先按盒筛掉绝大多数面**再算距离。
        #   没有它时，一个 201 面的零件每取一个点就要调 201 次 GetClosestPointOn
        #   （真机单条配合 123s）；有了它通常只剩几个面要算。
        fbox: list = []
        lbox = None
        with contextlib.suppress(Exception):
            boxes = [member(f, "GetBox") or [] for f in faces]
            for b in boxes:
                if len(b) >= 6:
                    fbox.append([float(v) for v in b])
                else:
                    fbox.append(None)
            good = [b for b in fbox if b]
            if good:
                lbox = [min(b[0] for b in good), min(b[1] for b in good), min(b[2] for b in good),
                        max(b[3] for b in good), max(b[4] for b in good), max(b[5] for b in good)]
        xf = _xform_of(c)
        rec = {"comp": nm, "faces": faces, "fbox": fbox, "lbox": lbox, "xf": xf,
               "mode": 0, "mode_err": None, "mode_err_alt": None, "mode_soft": None}
        if xf is not None and lbox is not None:
            gbox = None
            with contextlib.suppress(Exception):
                gb = member(c, "GetBox", False, False)
                if gb is not None and len(gb) == 6:
                    gbox = [float(v) for v in gb]
            if gbox is not None:
                # ★★★ 角点探针（2026-10-05 修）：把**全局包围盒 8 个角点**变换回局部，
                #   取"得到的局部包围盒"与真实 lbox 偏差最小的那个约定。
                #   旧实现只拿**盒心**当探针 ⇒ 盒心两种约定都落在 lbox 内时**无法判别**，
                #   静默挑 mode=0（真机：定位销/箱盖都要 mode=1）⇒ 之后取点全被映射到
                #   镜像位置，报「点不在任何面上」——报错方向完全错。见 30-pitfalls §121。
                # ★ 两个约定都算误差，把**余量**也记下来（`mode_soft`）：
                #   零件是"轴对称 + 非 90° 倍数摆放"时（六角头螺栓绕杆轴转 30°），
                #   盒级探针的判别力会变弱（两种约定误差接近）⇒ 必须**可见**，不能静默。
                errs = {}
                for mode in (0, 1):
                    lo = [1e18, 1e18, 1e18]
                    hi = [-1e18, -1e18, -1e18]
                    for ix in (0, 3):
                        for iy in (1, 4):
                            for iz in (2, 5):
                                lp = _to_local(xf[0], xf[1],
                                               [gbox[ix], gbox[iy], gbox[iz]], mode)
                                for i in range(3):
                                    lo[i] = min(lo[i], lp[i])
                                    hi[i] = max(hi[i], lp[i])
                    errs[mode] = max(max(abs(lo[i] - lbox[i]), abs(hi[i] - lbox[i + 3]))
                                     for i in range(3))
                best = 0 if errs[0] <= errs[1] else 1
                other = 1 - best
                rec["mode"] = best
                rec["mode_err"] = round(errs[best], 6)
                rec["mode_err_alt"] = round(errs[other], 6)
                rec["mode_soft"] = (errs[other] - errs[best]) < mm(0.5)
                rec["_errs"] = errs
        out.append(rec)
    # ---------------- ★★★ 全局约定投票（2026-10-05 加）----------------
    # `ArrayData` 的存取约定是**COM API 的全局性质**，不是"每个组件各自的属性"。
    #   逐组件各判一个的后果（真机实测）：垫圈/箱盖/定位销/部分螺栓判成 mode=1 且误差 0，
    #   而螺母判成 **mode=0**（误差 4~6mm）—— 一个 API 不可能同时有两种约定
    #   ⇒ 必然有一类判错，而且**报错方向完全错**（会说"坐标不是全局坐标"）。
    #   根因：几何对称 + 非 90° 倍数摆放时（六角螺母绕自身轴转 30°），
    #   盒级探针的两种误差都被"旋转盒子 vs 旋转实体"的差撑大，**谁小谁大没有意义**。
    # ⇒ 正确做法：只用**判别确定**的组件投票。判据 = `min(err0,err1) < 0.05mm`
    #   **且** `|err0 − err1| > 0.05mm`（两个约定必须真的可区分；
    #   恒等变换 R=I 时 err0 == err1，**它不携带任何信息**，必须排除，否则是噪声票）。
    #   然后把**同一个**约定施加给全部组件。判错/冲突都**可见**，不静默。
    decisive = [r for r in out
                if r.get("mode_err") is not None and r.get("mode_err_alt") is not None
                and min(r["mode_err"], r["mode_err_alt"]) < mm(0.05)
                and abs(r["mode_err"] - r["mode_err_alt"]) > mm(0.05)]
    if decisive:
        votes = {0: 0, 1: 0}
        for r in decisive:
            votes[r["mode"]] += 1
        gm = 0 if votes[0] >= votes[1] else 1
        src = "global-vote"
        if votes[0] and votes[1]:
            src = "global-vote(CONFLICT)"
    else:
        gm, src = None, "per-component(NO DECISIVE VOTE)"
    for r in out:
        if gm is not None:
            r["mode_local"] = r["mode"]          # 逐组件自己的判断（留证）
            r["mode"] = gm
            r["mode_mismatch"] = bool(r["mode_local"] != gm)
            if r.get("_errs"):
                r["mode_err"] = round(r["_errs"][gm], 6)
        r["mode_source"] = src
        r["votes"] = {str(k): v for k, v in votes.items()} if decisive else None
        r.pop("_errs", None)
    return out


#: `_asm_face_index` 的**跨调用缓存**。键 = (文档标题, 组件包围盒签名)。
#: ★ 为什么要缓存：枚举一次要把每个组件的每个面 `GetBox` 一遍（20+ 件 ≈ 上千次 COM），
#:   而 `add_mate` 每次取点都要枚举 —— 41 条配合就是 82 次枚举，真机单条 100s 量级，
#:   一轮装配要跑一两个小时。**配合不改变站位时索引完全一样** ⇒ 缓存安全。
#: ★ 安全性：签名里含**每个组件的全局包围盒**，只要配合挪动了任何组件，签名就变、缓存作废；
#:   文档标题也进签名 ⇒ 换文档必失效。**绝不按"距离上次多久"这种理由复用。**
_ASM_IDX_CACHE: dict = {"key": None, "idx": None}


def _asm_idx_key(sw):
    boxes = sw.component_boxes()
    t = None
    with contextlib.suppress(Exception):
        t = member(sw.model, "GetTitle")
    # ★ 量子化到 **0.1mm**（米制 round(...,4)）而不是 1e-6（0.001mm）——
    #   真机实测：1e-6 时**同一条配合前后**包围盒读数有亚微米的浮点噪声，
    #   于是签名每两次调用就变一次 ⇒ 索引被反复重建（单次 ~40s），
    #   41 条配合白花二十多分钟。0.1mm 量子化后签名稳定。
    #   取舍（必须写清）：组件若只挪动 **<0.05mm**，本函数会认为"没动"而复用旧索引，
    #   此时索引里的 `xf` 有同量级偏差 ⇒ 换算出的局部坐标偏 <0.05mm。
    #   取面容差是 3mm，且点取在面内部，故不影响选面；
    #   **真机判据仍是「配合后 `check_component_box` 全零位移」**（坑表 §120⑦），
    #   所以哪怕这里判断失误，也会在站位校验上暴露。
    return (str(t), len(boxes),
            tuple(sorted((k, tuple(round(float(v), 4) for v in vs))
                         for k, vs in boxes.items())))


def _asm_face_index_cached(sw, toplevel_only: bool = True) -> list[dict]:
    key = _asm_idx_key(sw)
    if _ASM_IDX_CACHE.get("key") == key and _ASM_IDX_CACHE.get("idx"):
        return _ASM_IDX_CACHE["idx"]
    idx = _asm_face_index(sw, toplevel_only)
    _ASM_IDX_CACHE["key"], _ASM_IDX_CACHE["idx"] = key, idx
    return idx


def _select_asm_face_at(sw, point_mm, append: bool = True, tol_mm: float = 3.0,
                        mark: int = 0, only: frozenset | None = None,
                        idx0: list | None = None) -> tuple[bool, str]:
    """按**装配体全局坐标**选中装配体里的一个面（**与视角无关**）。

    `only` = 只在这些**组件全名**里找（`None` = 全部组件）。

    ★★ 为什么需要 `only`（2026-10-05 真机设计）：判据是「点到面所在平面的距离最小
      且 ≤ tol」。**螺栓杆 Ø8 的外圆柱面与它穿过的 Ø9 孔内壁只差 0.5mm** ——
      只按坐标找，两个面到点的距离几乎一样，选哪个取决于枚举顺序 ⇒ **静默选错组件**，
      而报出来的是"配合建上了"（选错面照样能建配合，只是配合的是错的东西）。
      ⇒ 紧固件类配合**必须**限定组件。`add_mate` 的实体写法支持 `"component"` 字段。

    判据链（每一步都可用真机数据核对，见 `_asm_face_index` 的 ①②③）：
      ① 用 `IComponent2.Transform2` 做**全局 → 局部**逆变换；
      ② 落在该组件的**局部包围盒**外 ⇒ 不是这个组件的面，跳过；
      ③ `IFace2.GetClosestPointOn(lx, ly, lz)` 给面上最近点，**距离最小且 ≤ tol**
         的那个就是目标面；
      ④ `IEntity.Select4` 选中（判据是**选中数**，不信返回值）。

    ★ 与 `SelectByID2` 的**相机射线**完全不同：射线只能选中**朝向相机**的面
      （真机实测：默认等轴测视图下，上块的 −X 面与 +Y 面**恒选不中**，
      而 +X / −Y / 底面选得中 —— "点算错了"和"面背着相机"报出来是同一句话）。
    ★ 绝不"勉强选一个"：一个都不合格就回 False，并报出最近的那个差多少毫米。
    """
    p = [mm(float(v)) for v in point_mm]
    idx = idx0 if idx0 is not None else _asm_face_index_cached(sw)
    if not idx:
        return False, ("装配体里一个组件的面都没枚举到"
                       "（`IComponent2.GetBody()` 取不到？子装配体只下探一层）")
    tol = mm(tol_mm)
    best, best_d = None, None
    n_faces = 0
    # ★★ 约定可信度：约定本身是**全局投票**定出来的（见 `_asm_face_index` 末段）。
    #   · 投票里出现 per-component / CONFLICT ⇒ **约定本身没定下来**，取点全部不可信；
    #   · 某组件的"逐组件判断"与全局约定不一致（`mode_mismatch`）⇒ 它对约定无判别力
    #     （六角件绕轴转 30° 时误差都被撑大），但已被统一到全局约定 ⇒ **仅供参考**，
    #     不当成错误。真正裁判是「配合后组件有没有被挪走」（坑表 §120⑦）。
    conv_bad: list[str] = []
    conv_soft: list[str] = []
    for rec in idx:
        msrc = str(rec.get("mode_source") or "")
        if msrc.startswith("per-component") or "CONFLICT" in msrc:
            conv_bad.append(f"约定来源={msrc}（投票 {rec.get('votes')}）")
        elif rec.get("mode_mismatch"):
            conv_soft.append(f"{rec['comp']}(逐组件判 mode={rec['mode_local']}"
                             f"/全局 mode={rec['mode']})")
        if only is not None and rec["comp"] not in only:
            continue
        xf, lbox = rec["xf"], rec["lbox"]
        if xf is None or lbox is None:
            continue
        # ★ 坐标约定判别失败（角点探针误差大 / 判别余量小）⇒ 这个组件的取点**不可信**，
        #   报出来而不是静默映射到镜像位置（见 `_asm_face_index` 头注 ③）。
        lp = _to_local(xf[0], xf[1], p, rec["mode"])
        if not (lbox[0] - 1e-3 <= lp[0] <= lbox[3] + 1e-3
                and lbox[1] - 1e-3 <= lp[1] <= lbox[4] + 1e-3
                and lbox[2] - 1e-3 <= lp[2] <= lbox[5] + 1e-3):
            continue                      # 点不在这个组件的局部包围盒内
        # ★ 先按**逐面局部包围盒**粗筛，只有可能命中的面才去调 GetClosestPointOn ——
        #   真机上这一步把单条配合从 123s 压到几秒（大件 200+ 面，逐面调用是主要开销）。
        mgn = tol + 1e-3
        for k, fe in enumerate(rec["faces"]):
            fb = rec["fbox"][k] if k < len(rec["fbox"]) else None
            if fb is not None and not (fb[0] - mgn <= lp[0] <= fb[3] + mgn
                                       and fb[1] - mgn <= lp[1] <= fb[4] + mgn
                                       and fb[2] - mgn <= lp[2] <= fb[5] + mgn):
                continue
            n_faces += 1
            d = None
            with contextlib.suppress(Exception):
                cp = member(fe, "GetClosestPointOn", lp[0], lp[1], lp[2])
                if cp is not None and len(cp) >= 3:
                    d = math.dist([float(v) for v in cp[:3]], lp)
            if d is not None and (best_d is None or d < best_d):
                best, best_d = (rec["comp"], fe), d
    if best is None or best_d is None:
        return False, (f"全局点 {list(point_mm)} 落在**所有组件的局部包围盒之外** —— "
                       f"坐标大概不是装配体全局坐标（枚举到 {len(idx)} 个组件 / "
                       f"{n_faces} 个面**通过了逐面盒筛选**）"
                       + (f"；★ 本次**限定组件** {sorted(only)} ⇒ 也可能是点其实属于别的组件"
                          if only else "")
                       + (f"；★★★ 坐标约定**没定下来**：{conv_bad} ⇒ 取点会被映射到"
                          f"镜像位置，先解决约定，别再调坐标" if conv_bad else "")
                       + (f"；★ 逐组件判断与全局约定不一致（仅参考）：{conv_soft[:6]}"
                          if conv_soft else ""))
    comp, face = best
    if best_d > tol:
        return False, (f"点 {list(point_mm)} 离最近的面还有 **{best_d * 1000:.3f}mm**"
                       f"（容差 {tol_mm}mm，组件 {comp}，该面是"
                       f"{_face_kind(face)}面）—— 点不在任何面上："
                       f"检查坐标是不是按装配体全局写的"
                       f"（★ 若这个距离是「零点几毫米」，多半是**取到了邻近的别的面**，"
                       f"例如螺纹面 —— 见 30-pitfalls §120⑪）"
                       + (f"；★★★ 坐标约定**没定下来**：{conv_bad}（取点会被映射到"
                          f"**镜像位置** ⇒ 报「点不在面上」而真因是**约定**，"
                          f"见 30-pitfalls §120①）" if conv_bad else "")
                       + (f"；★ 逐组件判断与全局约定不一致（仅参考）：{conv_soft[:6]}"
                          if conv_soft else ""))
    if not _select_obj(face, append):
        return False, (f"面已找到（组件 {comp}，距点 {best_d * 1000:.3f}mm）"
                       f"但 `IEntity.Select4` 没选中")
    info = (f"组件 {comp} 的面[{_face_kind(face)}]，距点 {best_d * 1000:.3f}mm")
    if mark:
        # ★ `IEntity::Select4` **没有 mark 参数**，而配合实体必须带 mark。
        #   ⇒ 选中之后拿**面名**再用 `SelectByID2` 走一遍（名字与坐标/视角无关）。
        fname = ""
        with contextlib.suppress(Exception):
            fname = str(member(face, "GetName") or "")
        if fname:
            with contextlib.suppress(Exception):
                sw.clear_selection()
                if sw.ext.SelectByID2(fname, "FACE", 0.0, 0.0, 0.0, False, mark,
                                      None, 0):
                    return True, info + f"（按面名 {fname} 带 mark={mark}）"
    return True, info


_DIRS = {"+X": (1, 0, 0), "-X": (-1, 0, 0), "+Y": (0, 1, 0), "-Y": (0, -1, 0),
         "+Z": (0, 0, 1), "-Z": (0, 0, -1)}


def _select_edges(sw, spec, tol_mm: float = 8.0, mark: int = 0) -> tuple[int, list]:
    """按规格选边，返回 (选中数, 没匹配上的项)。

    spec 四种写法：
        "all" / "*"            → 全部边
        [[x,y,z], …]           → 每项挑「中点离它最近」的那条边
        [{"point":[…], "tol":5}] → 同上，可单独放宽容差
        [{"dir":"+Z"}]         → 法向近似该方向的边（先按方向筛，再取最近邻）

    **容差是刻意的**：最近的那条边如果离目标点超过 tol，就判为「没匹配上」而不是
    「勉强选一条」—— 勉强的结果是不报错但倒错地方。宁可让计划失败得响一点。

    `mark` 非 0 时走 `SelectByID2`（`IEntity.Select4` **没有 mark 参数**）——
    阵列的方向参考必须 Mark=1，所以那条路绕不开。注意：**要选哪条边仍然是
    由坐标最近邻定死的**，射线只是拿该边的中点当靶，因此依旧与视角无关。
    """
    edges = _edges(sw)
    if not edges:
        return 0, ["（这个零件里一条边都没有）"]
    if isinstance(spec, str) and spec.strip() in ("*", "all", "全部", "all_edges"):
        return sum(1 for e in edges if _select_obj(e["obj"], True)), []
    hit, miss = 0, []
    for item in (spec or []):
        d = item if isinstance(item, dict) else {}
        pt = d.get("point") or (item if isinstance(item, (list, tuple)) else None)
        tol = float(d.get("tol", tol_mm))
        if not pt:
            miss.append(item)
            continue
        tgt = [mm(float(v)) for v in pt]
        cands = edges
        nrm = _DIRS.get(str(d.get("dir", "")).upper())
        if nrm is not None:
            keep = []
            for e in cands:
                n = e.get("normal")
                if n and sum(a * b for a, b in zip(n, nrm)) > 0.9:
                    keep.append(e)
            cands = keep or cands
        best, bd = None, None
        for e in cands:
            if not e["mid"]:
                continue
            dd = math.dist(e["mid"], tgt)
            if bd is None or dd < bd:
                best, bd = e, dd
        if best is None or (bd is not None and bd > mm(tol)):
            miss.append(item)
            continue
        if mark:
            mid_mm = [v * 1000.0 for v in best["mid"]] if best["mid"] else None
            ok = bool(mid_mm) and _pick(sw, {"type": "EDGE", "point": mid_mm},
                                        mark=mark, append=True)
        else:
            ok = _select_obj(best["obj"], True)
        if ok:
            hit += 1
        else:
            miss.append(item)
    return hit, miss


def _ref_axis_names(sw) -> list[str]:
    """特征树里所有**参考基准轴**的名字。`InsertAxis2` 建的轴类型名是 `RefAxis`。"""
    out: list[str] = []
    for t, nm in sw.feature_names():
        if t in ("RefAxis", "Axis") and nm not in out:
            out.append(nm)
    return out


def _select_ref_axis(sw, name: str = "", mark: int = 1, append: bool = True) -> bool:
    """选中一根参考基准轴。

    ★ 真机实测（2026-09-25，M2）：不能只写 `SelectByID2("Axis1","AXIS")` ——
      `IModelDoc2.InsertAxis2` 建出来的轴按**界面语言**自动命名（中文「基准轴1」），
      "Axis1" 只在英文模板下存在。所以先按名字试，再退到「特征树里的 RefAxis 逐个试」。

    ★★ `append` 默认 **True**，这个默认值改过一次，代价是两个阵列 op 整整查了半天：
      `SelectByID2` 的 Append=False 会**清空已有选择集**。阵列的顺序是
      「先选种子特征(Mark=4)，再选方向参考(Mark=1)」，如果选参考时 Append=False，
      种子特征就被冲掉了 —— 表现是「选上了、不报错、但什么都不建」。
      排查方式就是**把选择集 dump 出来**：`GetSelectedObjectCount2` 只剩 1 项（那条轴）
      ⇒ 一眼看出种子没了。**任何"多选"的 API 之后都该 dump 一次选择集。**
    """
    cands: list[str] = []
    if name:
        cands.append(str(name))
    for nm in _ref_axis_names(sw):
        if nm not in cands:
            cands.append(nm)
    for nm in cands:
        if sw.ext.SelectByID2(nm, "AXIS", 0, 0, 0, append, mark, None, 0):
            return True
    return False


def _select_face_marked(sw, f, mark: int, append: bool) -> bool:
    """选一个 `_faces()` 记录里的面，**带 mark**。

    ★★ 为什么需要它（2026-10-11 真机确诊，坑表 §135）：`IEntity.Select4(Append, Callout)`
      **没有 mark 参数**，而某些特征（分割线 `InsertSplitLineProject`）要求
      「**面 mark=1**」才认。带 mark 的唯一通路是
      `SelectByID2("", "FACE", x, y, z, append, mark, …)` —— 拿面 `GetBox` 的中心当坐标。
      ★ `GetBox` 单位是**米**（与 `_faces` 里 bbox 一致），`_pick` 要 mm ⇒ ×1000。
      `Select4` 仍留兜底（坐标拾取可能因视角 / 面重合打空）。
    """
    if f is None:
        return False
    box = f.get("box")
    if box and len(box) >= 6:
        c = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
        if _pick(sw, {"type": "FACE", "point": [v * 1000.0 for v in c]},
                 mark=mark, append=append):
            return True
    return _select_obj(f.get("obj"), append)


def _select_faces(sw, spec, tol_mm: float = 8.0, mark: int = 0,
                  surfaces: bool = False) -> tuple[int, list]:
    """按规格选面，返回 (选中数, 没匹配上的项)。

    spec 三种写法：
        "all" / "*"            → 全部面
        ["+Z", "-Z"]           → 方向字符串：取法向匹配里**面积最大**的那个面
                                 （箱体抽壳就靠它：'+Z' 就是顶面）
        [[x,y,z], …]           → 坐标：先射线拾取（最可靠），失败再退到
                                 「包围盒中心最近邻」

    `mark`：`SelectByID2` 的选择标记，默认 0（fillet/chamfer/shell 等旧调用行为不变）。
    ★ `split_line` 靠它区分 **草图 mark=4 / 面 mark=1**（见 §135，唯一可用组合）。
    `surfaces`：是否连**曲面体**的面一起纳入（默认 False = 只实体）。
    ★ 目标是曲面（`split_line` 投影到车头曲面上）时必须 True，否则"一个面都匹配不上"。
    """
    if isinstance(spec, str) and spec.strip() in ("*", "all", "全部", "all_faces"):
        faces = _faces(sw, include_surfaces=surfaces)
        return sum(1 for f in faces if _select_face_marked(sw, f, mark, True)), []
    faces = None
    hit, miss = 0, []
    for item in (spec or []):
        nrm = _DIRS.get(str(item).upper()) if isinstance(item, str) else None
        if nrm is not None:
            if faces is None:
                faces = _faces(sw, include_surfaces=surfaces)
            best, ba = None, None
            for f in faces:
                n = f.get("normal")
                if not n or sum(a * b for a, b in zip(n, nrm)) <= 0.9:
                    continue
                if ba is None or f["area"] > ba:
                    best, ba = f, f["area"]
            if best is None or not _select_face_marked(sw, best, mark, True):
                miss.append(item)
            else:
                hit += 1
            continue
        pt = item.get("point") if isinstance(item, dict) else item
        tol = float(item.get("tol", tol_mm)) if isinstance(item, dict) else tol_mm
        if not pt:
            miss.append(item)
            continue
        if _pick(sw, {"type": "FACE", "point": pt}, mark=mark, append=True):
            hit += 1
            continue
        if faces is None:
            faces = _faces(sw, include_surfaces=surfaces)
        tgt = [mm(float(v)) for v in pt]
        best, bd = None, None
        for f in faces:
            box = f.get("box")
            if not box:
                continue
            c = [(box[i] + box[i + 3]) / 2.0 for i in range(3)]
            dd = math.dist(c, tgt)
            if bd is None or dd < bd:
                best, bd = f, dd
        if best is not None and bd is not None and bd <= mm(tol) \
                and _select_face_marked(sw, best, mark, True):
            hit += 1
        else:
            miss.append(item)
    return hit, miss


def _fname(f) -> str:
    """特征名（读不到就回一个中性占位，**别让取名失败把整步带崩**）。"""
    with contextlib.suppress(Exception):
        n = member(f, "Name")
        if n:
            return str(n)
    return "Feature"


def _select_by_spec(sw, step, ctx, key: str, notes: list, kind: str = "edge") -> bool:
    """按 `key` 参数里的规格选边/面，返回「选择集非空」。

    抽出来是为了让 `fillet` / `chamfer`（都吃 `edges`）**共用同一套选择语义** ——
    否则两个 op 迟早会漂：一个支持 "all"、另一个忘了，计划就得按 op 分别写。

    kind="edge" 走 `_select_edges`；kind="face" 走 `_select_faces`。
    """
    spec = _p(step, ctx, key)
    if spec:
        sw.clear_selection()
        tol = float(_p(step, ctx, "tol", 8) or 8)
        picker = _select_edges if kind == "edge" else _select_faces
        hit, miss = picker(sw, spec, tol)
        if miss:
            notes.append(f"没匹配上的{kind}：{miss}")
        if not hit:
            sw.clear_selection()
            return False
    return sw.selected_count() > 0


def _sketch_segments(sw, sketch_name: str) -> list:
    """取某张草图里的全部图元对象（ISketchSegment）。"""
    out: list = []
    with contextlib.suppress(Exception):
        f = typed(sw.model.FeatureByName(sketch_name), "IFeature")
        sk = typed(member(f, "GetSpecificFeature2"), "ISketch")
        for s in (member(sk, "GetSketchSegments") or ()):
            out.append(typed(s, "ISketchSegment"))
    return out


def _select_centerline_in(sw, sketch_name: str) -> bool:
    """在草图里找**第一条中心线**（构造几何的直线）并直接选中它。

    为什么不靠名字选中心线：草图图元名在不同语言界面下可能是 `Line1`，也可能是
    `直线1` —— 靠名字选极不稳（这是「选了轴却旋转出错」的隐藏原因）。
    直接拿图元对象调 `Select4` 最稳；代价是 **Select4 没有 mark 参数**，
    所以它只作为按名字选失败后的兜底方案。
    """
    for seg in _sketch_segments(sw, sketch_name):
        if _seg_type(seg) == 0 and _is_construction(seg):
            return _select_segment(seg, append=True)
    return False


def _select_axis(sw, name: str, mark: int = 1) -> bool:
    """选中旋转轴。草图中心线是 EXTSKETCHSEG，参考轴是 AXIS —— 两种都要试。
    mark 取多少见各 op 说明（FeatureRevolve2 官方写 4/16，本机 revolve_boss 实测 1 也行）。"""
    for typ in ("AXIS", "EXTSKETCHSEG", "SKETCHSEGMENT"):
        if sw.ext.SelectByID2(name, typ, 0, 0, 0, True, mark, None, 0):
            return True
    return False


def _active_sketch_segments(sw) -> list:
    """**当前正在编辑的草图**里的全部图元对象。

    ★ 为什么要有它：`ctx["segments"]` 里存的是各画图 op 的**返回值**，
      而 `CreateCenterRectangle` / `CreateCircleByRadius` 这类方法在晚期绑定下
      可能返回 None（同 §19 的"返回值集体说谎"）。于是「等距/镜像选不上图元」——
      早先还以为是自己没画上。改为**直接问草图要图元**，就跟返回值无关了。
    """
    out: list = []
    with contextlib.suppress(Exception):
        sk = member(_sm(sw), "ActiveSketch")
        for s in (member(sk, "GetSketchSegments") or ()):
            out.append(typed(s, "ISketchSegment"))
    return out


def _seg_type(seg) -> int:
    """草图图元类型（ISketchSegment.GetType）。0=直线，1=圆，2=弧……（-1=取不到）"""
    with contextlib.suppress(Exception):
        return int(member(seg, "GetType"))
    return -1


def _is_construction(seg) -> bool:
    """该图元是不是**构造几何**（中心线就是构造几何的直线）。"""
    with contextlib.suppress(Exception):
        return bool(member(seg, "ConstructionGeometry"))
    return False


def _active_centerline(sw):
    """**当前正在编辑的草图**里第一条中心线（构造几何的直线）的 ISketchSegment。

    判据：GetType()==0 且 ConstructionGeometry —— **与界面语言无关**（§27）。
    正在编辑时只拿得到 ActiveSketch，拿不到 FeatureByName，所以不能复用
    `_select_centerline_in`；选它用 Select4（无 mark）。
    """
    for seg in _active_sketch_segments(sw):
        if _seg_type(seg) == 0 and _is_construction(seg):
            return seg
    return None


def _seg_count(sw) -> int:
    """当前草图里的图元数。

    用来验证「转换实体引用 / 等距 / 镜像」到底画没画上 ——
    这几个 API 都回布尔，而布尔在晚期绑定下不可信（同 SaveAs/SaveBMP 的毛病）。
    """
    n = 0
    with contextlib.suppress(Exception):
        sk = member(_sm(sw), "ActiveSketch")
        for _s in (member(sk, "GetSketchSegments") or ()):
            n += 1
    return n


def _body_count(sw) -> int:
    """实体（body）个数。镜像实体/布尔类 op 的必要判据。

    ★★ 唯一取实体入口 `sw.bodies()` —— 别在这里自己写 `GetBodies2(0, True)`
      （只取可见；实体被抑制会数成 0，把"有实体"误判成"没实体"）。
    """
    with contextlib.suppress(Exception):
        return len(sw.bodies())
    return 0


def _read_material(sw) -> str | None:
    """读当前零件材料名。读不到回 None（不报错）。

    ★ 真机踩坑（2026-09-25）：`GetMaterialPropertyName2` 有**两个出参**，晚期绑定下
      返回值是 **tuple `(材料名, 材料库名)`**，不是 str！早先按 `isinstance(v, str)` 判断
      ⇒ 永远读成 None ⇒ 把「材料其实设上了」误报成「材料没设上」，白查了一轮。
      这里把 tuple / 单串 / MaterialIdName 三种形态都认了。
    """
    cfgs: list[str] = [""]
    with contextlib.suppress(Exception):
        cfgs = [str(c) for c in (sw.model.GetConfigurationNames() or [])] or [""]
    part = typed(sw.model, "IPartDoc")
    for cfg in cfgs:
        for obj in (part, sw.model):
            v, _err = _call(member, obj, "GetMaterialPropertyName2", cfg, None)
            if isinstance(v, (tuple, list)):
                v = v[0] if v else None
            if isinstance(v, str) and v.strip():
                return v
    # 兜底：MaterialIdName 形如 'solidworks materials|普通碳钢|9'
    mid, _err = _call(member, sw.model, "MaterialIdName")
    if isinstance(mid, str) and "|" in mid:
        parts = mid.split("|")
        if len(parts) >= 2 and parts[1].strip():
            return parts[1]
    return None


def _new_feature(sw, tree0: set, skip=()) -> Any | None:
    """特征树前后 diff，取出新增的特征对象（**不信任何返回值**的落地写法）。

    `skip` 里可以塞「本来就会被吸收掉的名字」，比如被特征吃掉的草图。
    """
    fresh = [n for _t, n in sw.feature_names() if n not in tree0 and n not in skip]
    if not fresh:
        return None
    f = typed(sw.model.FeatureByName(fresh[-1]), "IFeature")
    ec = 0
    with contextlib.suppress(Exception):
        ec = int(member(f, "GetErrorCode"))
    if ec:
        return ("ERR", ec, f)
    return f


def _finish(sw, feat, step, ctx, extra: dict) -> dict:
    """统一收尾：改名、登记到 ctx、返回结果。"""
    name = _p(step, ctx, "name") or str(member(feat, "Name"))
    if _p(step, ctx, "name"):
        with contextlib.suppress(Exception):
            feat.Name = _p(step, ctx, "name")
    ctx.setdefault("feature_names", []).append(name)
    ctx["last_feature"] = name
    return {"feature": name, **extra}


# ------------------------------------------------------------ 草图点

@op("point", "草图点", "sketch",
    "在草图里画一个点。\n"
    "★ **异型孔向导的前置**：孔位就是这样一个个点定出来的"
    "（`hole_wizard` 吃的是含点的草图，不是含圆的草图）。",
    api=["ISketchManager.CreatePoint"],
    params={"x": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "y": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "z": {"type": "float", "unit": "mm", "required": False, "default": 0},
            "points": {"type": "list", "required": False,
                       "desc": "一次画多个：[[x,y], [x,y], ...]（给了就忽略 x/y/z）"}},
    requires=("sketch_open",), verified=True)
def op_point(sw, step, ctx):
    pts = _p(step, ctx, "points")
    if pts:
        seq = pts
    else:
        seq = [[_p(step, ctx, "x", 0) or 0, _p(step, ctx, "y", 0) or 0,
                _p(step, ctx, "z", 0) or 0]]
    n = 0
    for p in seq:
        p = list(p) + [0, 0, 0]
        s = None
        with contextlib.suppress(Exception):
            s = _sm(sw).CreatePoint(mm(float(p[0])), mm(float(p[1])), mm(float(p[2])))
        if s is not None:
            ctx.setdefault("segments", []).append(s)
            n += 1
    if n == 0:
        raise SwError("点没画上（CreatePoint 返回 None）—— 确认草图已开始（sketch_begin）")
    return {"points": n}


# ------------------------------------------------------------ 旋转切除

@op("revolve_cut", "旋转切除", "feature",
    "绕中心线/基准轴旋转草图轮廓做**切除** —— 轴上的退刀槽、挡圈槽、套筒内孔都靠它。\n"
    "走 FeatureRevolve2（官方已把 FeatureRevolveCut2 标为废弃）+ `IsCut=True`；\n"
    "参数顺序与 revolve_boss 完全一致，只差这一个开关。\n"
    "★ **轴必须显式给**：不选轴时 SolidWorks 会退化成「绕轮廓自身」旋转，"
    "结果完全不是你要的，而且它**不报错**。",
    api=["IFeatureManager.FeatureRevolve2"],
    params={
        "profile": {"type": "str", "required": False, "desc": "轮廓草图名，不给就用刚画完的那张"},
        "axis": {"type": "str", "required": False,
                 "desc": "中心线名（如 Line1）。**不给也行** —— 会自动抓草图里第一条中心线"},
        "angle": {"type": "deg", "required": False, "default": 360},
        "reverse_dir": {"type": "bool", "required": False, "default": False},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",), verified=True)
def op_revolve_cut(sw, step, ctx):
    prof = _p(step, ctx, "profile") or ctx.get("last_sketch")
    axis = _p(step, ctx, "axis")
    if not prof:
        raise SwError("旋转切除需要轮廓草图名（profile）")
    ang = deg(float(_p(step, ctx, "angle", 360) or 360))
    rev = bool(_p(step, ctx, "reverse_dir", False))
    vol0 = _volume(sw)
    notes: list[str] = []
    feat, used = None, ""
    # 候选：① 按名字选轴（mark 试 1/4/16，本机 revolve_boss 实测 1 可用）
    #      ② 兜底 —— 直接在草图里抓**第一条中心线对象**（不看名字，绕开语言差异）
    cands: list[tuple[str, int | None]] = [("轴名", m) for m in (1, 4, 16)]
    cands.append(("草图内中心线对象", None))
    for tag, marker in cands:
        sw.clear_selection()
        if not sw.select(prof, "sketch", mark=0):
            raise SwError(f"选不中轮廓草图 '{prof}'")
        if marker is None:
            if not _select_centerline_in(sw, prof):
                notes.append(f"{tag}：草图 {prof} 里找不到中心线"
                             "（旋转轴必须是**中心线**，普通直线不行）")
                continue
            used_desc = tag
        else:
            if not axis:
                continue
            if not _select_axis(sw, axis, marker):
                notes.append(f"{tag} '{axis}' mark={marker}：选不中")
                continue
            used_desc = f"轴名 '{axis}' mark={marker}"
        tree0 = {n for _t, n in sw.feature_names()}
        # 20 参，与 op_revolve_boss 同一张表，只有第 4 个 IsCut 不同。
        with contextlib.suppress(Exception):
            _fm(sw).FeatureRevolve2(
                True, True, False, True,                  # SingleDir, IsSolid, IsThin, **IsCut**
                rev, False,                               # ReverseDir, BothDirectionUpToSameEntity
                _C(sw, "swEndCondBlind", 0), 0,           # Dir1Type, Dir2Type
                ang, 0.0,                                 # Dir1Angle, Dir2Angle
                False, False, 0.0, 0.0,                   # OffsetReverse1/2, OffsetDistance1/2
                0, 0.0, 0.0,                              # ThinType, 薄壁厚1/2
                True, False, True)                        # Merge, UseFeatScope, UseAutoSelect
        sw.clear_selection()
        got = _new_feature(sw, tree0, skip=(prof,))
        if got is None:
            notes.append(f"{used_desc}：特征树没新增（旋转没生成）")
            continue
        if isinstance(got, tuple):                        # 建了但带错误码
            notes.append(f"{used_desc}：特征重建失败 {got[1]}"
                         f"（{FEATURE_ERROR_NAMES.get(got[1], '未知')}）")
            _drop_feature(sw, got[2])
            continue
        if vol0 is not None:
            vol1 = _volume(sw)
            if vol1 is not None and vol1 >= vol0 - 1e-12:
                notes.append(f"{used_desc}：特征成功但**体积没减少**"
                             "（没切到材料，轮廓多半在实体外面）")
                _drop_feature(sw, got)
                continue
        feat, used = got, used_desc
        break
    if feat is None:
        raise SwError(
            f"旋转切除失败，{len(cands)} 个方案都没成：\n"
            + "".join(f"    · {x}\n" for x in notes)
            + "  排查：① 草图里到底有没有**中心线**（用 centerline 画的，不是普通直线）\n"
              "        ② 轮廓是否穿过中心线（穿轴必失败）\n"
              "        ③ 轮廓闭合吗（先跑 check_sketch）")
    return _finish(sw, feat, step, ctx,
                   {"axis": axis or "(草图内中心线)", "variant": used,
                    "volume_before": vol0, "volume_after": _volume(sw),
                    "notes": notes or None})


# ------------------------------------------------------------ 草图：转换实体 / 等距 / 镜像

@op("sk_convert", "转换实体引用", "sketch",
    "把已有实体的**边/面/环**投影进当前草图（省掉重画轮廓，箱体/端盖大量复用）。\n"
    "必须先 sketch_begin。选择用**坐标射线拾取** —— 脚本里没有鼠标，"
    "「点一下那条边」等价于 SelectByID2 到边上的一个全局坐标。\n"
    "★ 坑：转换出来的图元**不带原约束**，实体改了它不跟着变。",
    api=["ISketchManager.SketchUseEdge3"],
    params={
        "select": {"type": "list", "required": True,
                   "desc": '要投影的对象，如 [{"type":"FACE","point":[0,0,12]}]'},
        "chain": {"type": "bool", "required": False, "default": True,
                  "desc": "True=整条链一起转（推荐）；False=只转选中那一段"},
        "inner_loops": {"type": "bool", "required": False, "default": False,
                        "desc": "选面时是否连内环一起转"},
    },
    requires=("sketch_open",), verified=True)
def op_sk_convert(sw, step, ctx):
    items = _p(step, ctx, "select") or []
    if not items:
        raise SwError('sk_convert 需要 select，例如 [{"type":"FACE","point":[0,0,12]}]')
    sw.clear_selection()
    hit, miss = _pick_all(sw, items, mark=0)
    if hit == 0:
        raise SwError(f"一个都没选中：{miss}\n"
                      "  坐标是**全局 mm 坐标**（不是草图坐标），必须正好落在实体的边/面上。")
    before = _seg_count(sw)
    ok = False
    with contextlib.suppress(Exception):
        ok = bool(member(_sm(sw), "SketchUseEdge3",
                         bool(_p(step, ctx, "chain", True)),
                         bool(_p(step, ctx, "inner_loops", False))))
    sw.clear_selection()
    after = _seg_count(sw)
    added = after - before
    # ★ 它回的布尔不能全信 —— 以**图元数变化**为准（同 SaveBMP 的毛病）。
    if added <= 0:
        raise SwError(
            f"转换实体引用没产出图元（选中 {hit}/{len(items)}，图元数 {before}→{after}）。\n"
            "  排查：① 忘了 sketch_begin？② 坐标不在实体上？\n"
            "        ③ 要投影的边与草图平面异面/平行（投影会退化成一条线或干脆失败）")
    return {"selected": hit, "missed": miss or None, "returned": ok,
            "segments_before": before, "segments_after": after, "added": added}


@op("sk_offset", "等距实体", "sketch",
    "把**草图里已有的图元**按距离等距一份（壁厚/台阶最常用）。\n"
    "★ `Offset` 在 SolidWorks 内部是**米**；本 op 收 mm，转换在外层做掉了。\n"
    "★ 负距离 = 往反方向等距，比去调『反向』开关省事。",
    api=["ISketchManager.SketchOffset2"],
    params={
        "dist": {"type": "mm", "required": True, "desc": "等距距离，负值=反向"},
        "both": {"type": "bool", "required": False, "default": False, "desc": "两侧同时等距"},
        "chain": {"type": "bool", "required": False, "default": True},
        "cap": {"type": "str", "required": False, "default": "swSkOffsetNoCaps",
                "desc": "swSkOffsetCapEndType_e：NoCaps / LineCaps / ArcCaps"},
        "make_construction": {"type": "str", "required": False, "default": "none",
                              "desc": "none / orig / offs / both —— 哪些转成构造几何"},
        "add_dimensions": {"type": "bool", "required": False, "default": False},
        "select": {"type": "str", "required": False, "default": "created",
                   "desc": "created=本次画的图元（默认）；all=草图里全部"},
    },
    requires=("sketch_open",), verified=True)
def op_sk_offset(sw, step, ctx):
    dist = _p(step, ctx, "dist")
    if dist is None:
        raise SwError("sk_offset 需要 dist（mm）")
    sw.clear_selection()
    if _p(step, ctx, "select", "created") == "all":
        member(sw.ext, "SelectAll")
        n_sel = sw.selected_count()
    else:
        n_sel = _segments_select(
            sw, [s for s in ctx.get("segments", []) if s is not None])
        if n_sel == 0:
            # 兜底：画图 op 的返回值可能是 None，直接问草图要图元
            n_sel = _segments_select(sw, _active_sketch_segments(sw))
    if n_sel == 0:
        raise SwError("等距前要先有图元可选 —— 草图是空的吗？"
                      "等距的输入是**草图图元**（circle/rect/line…），不是实体的边。")
    cap = _C(sw, str(_p(step, ctx, "cap", "swSkOffsetNoCaps")), 0)
    mk = {"none": 0, "orig": 1, "offs": 2, "both": 3}.get(
        str(_p(step, ctx, "make_construction", "none")).lower(), 0)
    before = _seg_count(sw)
    ok = False
    with contextlib.suppress(Exception):
        ok = bool(member(_sm(sw), "SketchOffset2",
                         mm(float(dist)),
                         bool(_p(step, ctx, "both", False)),
                         bool(_p(step, ctx, "chain", True)),
                         cap, mk,
                         bool(_p(step, ctx, "add_dimensions", False))))
    sw.clear_selection()
    after = _seg_count(sw)
    if after - before <= 0:
        raise SwError(
            f"等距没产出图元（图元数 {before}→{after}）。\n"
            "  排查：① 输入图元选了没（不要拿实体的边当输入）\n"
            "        ② dist 是不是 0；③ 等距后是否自相交（大圆等距过头会失败）")
    return {"dist": float(dist), "returned": ok,
            "segments_before": before, "segments_after": after,
            "added": after - before}


@op("sk_mirror", "草图镜像", "sketch",
    "把选中的草图图元关于一条**中心线**镜像（箱体/端盖的对称结构常用）。\n"
    "★ API 是 IModelDoc2.SketchMirror —— 零参数命令，全靠**先选好**。\n"
    "centerline 默认 'auto'：直接在当前草图里找**第一条中心线对象**并选中 ——\n"
    "  这是 §27 的落地（靠名字选中心线会因语言界面而失败）。\n"
    "  给具体名字时才走 SelectByID2（必要时用，例如草图里有多条中心线）。",
    api=["IModelDoc2.SketchMirror"],
    params={
        "centerline": {"type": "str", "required": False, "default": "auto",
                       "desc": "auto=自动取当前草图第一条中心线；或给中心线名"},
        "select": {"type": "str", "required": False, "default": "created",
                   "desc": "created=本次画的图元；all=全部"},
    },
    requires=("sketch_open",), verified=True)
def op_sk_mirror(sw, step, ctx):
    cl = _p(step, ctx, "centerline", "auto") or "auto"
    auto = str(cl).lower() in ("auto", "centerline", "自动", "")
    before = _seg_count(sw)
    notes: list[str] = []

    # 候选：(标签, 选中心线的回调)。auto 优先 —— 对象级选中不受界面语言影响。
    tries: list[tuple[str, object]] = []
    if auto:
        cl_seg = _active_centerline(sw)
        if cl_seg is not None:
            tries.append(("自动取中心线对象(Select4)",
                          lambda: _select_segment(cl_seg, append=True)))
        else:
            notes.append("auto：当前草图里没找到中心线（GetType==0 且构造几何）——"
                         "先用 centerline op 画一条")
    for cl_mark in (0, 1, 4):
        tries.append((f"按名字 '{cl}' mark={cl_mark}",
                      (lambda m=cl_mark: any(
                          sw.ext.SelectByID2(cl, t, 0, 0, 0, True, m, None, 0)
                          for t in ("EXTSKETCHSEG", "SKETCHSEGMENT", "AXIS")))))

    def pick_entities() -> int:
        """选中「要被镜像的图元」。默认只选**非构造几何**图元 ——
        这样中心线（构造几何）不会同时被当成被镜像对象和镜像轴（§27 的双重角色坑）。"""
        if _p(step, ctx, "select", "created") == "all":
            member(sw.ext, "SelectAll")
            return 1
        segs = [s for s in _active_sketch_segments(sw) if s is not None]
        own = [s for s in segs if not _is_construction(s)]
        if own:
            return _segments_select(sw, own)
        # 全是构造几何时退回「所有图元」，免得直接判定草图为空
        return _segments_select(sw, segs)

    for tag, pick_cl in tries:
        sw.clear_selection()
        if pick_entities() == 0:
            raise SwError("镜像前要先有图元 —— 草图是空的吗？")
        if not pick_cl():
            notes.append(f"{tag}：选不中中心线")
            continue
        with contextlib.suppress(Exception):
            member(sw.model, "SketchMirror")
        sw.clear_selection()
        if _seg_count(sw) > before:
            return {"centerline": cl, "variant": tag,
                    "segments_before": before, "segments_after": _seg_count(sw),
                    "notes": notes or None}
        notes.append(f"{tag}：图元数没变（镜像没生效）")
    raise SwError(
        "草图镜像失败，所有候选都没成：\n"
        + "".join(f"    · {x}\n" for x in notes)
        + "  排查：① 草图里有没有真的画上中心线（centerline op，是构造几何直线）\n"
          "        ② 被镜像的图元选上了没（不要拿后画的中心线当被镜像对象）\n"
          "        ③ 兜底：镜像本质是「对称复制」，点表可以直接算，"
          "必要时在 Python 侧镜像坐标重画一遍")


# ------------------------------------------------------------ 扫描凸台

@op("swept_boss", "扫描凸台", "feature",
    "沿**路径**扫掠**轮廓**长出实体 —— 箱体筋板、通气塞头部、弯管都靠它。\n"
    "轮廓 mark=1、路径 mark=4（SolidWorks 扫掠的固定约定），与 cut_swept 对称。\n"
    "属于 InsertProtrusionSwept4（20 参，比 cut 版多 Merge / 圆形轮廓三个参数）。\n"
    "★★ 最容易翻的一条：**轮廓所在平面必须与路径垂直**，且轮廓要**穿过路径起点**。\n"
    "  把轮廓草图和路径草图都画在同一个基准面上（例如都画在前视面）是**必失败**的 ——\n"
    "  真机实测 3 个候选全灭就是这个原因。正确做法：路径在前视面（XY）沿 +X，\n"
    "  轮廓就画在右视面（YZ）的原点处。",
    api=["IFeatureManager.InsertProtrusionSwept4"],
    params={
        "profile": {"type": "str", "required": False, "desc": "轮廓草图名，不给用刚画完的"},
        "path": {"type": "str", "required": False, "desc": "路径名（螺旋线/曲线）"},
        "twist": {"type": "int", "required": False, "default": 0, "desc": "swTwistControlType_e"},
        "thin": {"type": "bool", "required": False, "default": False},
        "circular": {"type": "bool", "required": False, "default": False,
                     "desc": "True=圆形截面轮廓（给直径即可，不用画轮廓草图）"},
        "diameter": {"type": "mm", "required": False, "desc": "circular=True 时的截面直径"},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",), verified=True)
def op_swept_boss(sw, step, ctx):
    prof = _p(step, ctx, "profile") or ctx.get("last_sketch")
    path = _p(step, ctx, "path") or ctx.get("last_helix")
    circular = bool(_p(step, ctx, "circular", False))
    if not path:
        raise SwError("扫描凸台需要路径（path）—— 一般是先建的螺旋线或曲线")
    if not prof and not circular:
        raise SwError("扫描凸台需要轮廓草图（profile），或改用 circular=True 给 diameter")
    tw = int(_p(step, ctx, "twist", 0) or 0)
    thin = bool(_p(step, ctx, "thin", False))
    dia = float(_p(step, ctx, "diameter", 0) or 0)
    vol0 = _volume(sw)
    notes: list[str] = []
    feat, used = None, ""
    for c in (dict(pm=1, hm=4, auto=False, sm=False, tag="标准 mark 轮廓1/路径4"),
              dict(pm=1, hm=4, auto=True, sm=True, tag="+自动选择+高级平滑"),
              dict(pm=0, hm=4, auto=True, sm=True, tag="轮廓 mark0/路径 mark4+自动选择")):
        sw.clear_selection()
        if prof and not sw.select(prof, "sketch", mark=c["pm"]):
            raise SwError(f"选不中轮廓草图 '{prof}'")
        hit = False
        for t in _HELIX_TYPES:
            if sw.select(path, t, append=True, mark=c["hm"]):
                hit = True
                break
        if not hit:
            notes.append(f"{c['tag']}：选不中路径 '{path}'")
            continue
        tree0 = {n for _t, n in sw.feature_names()}
        _res, err = _call(_fm(sw).InsertProtrusionSwept4,
                          True,                       # 1  Propagate
                          False,                      # 2  Alignment
                          tw,                         # 3  TwistCtrlOption
                          True,                       # 4  KeepTangency
                          bool(c["sm"]),              # 5  BAdvancedSmoothing
                          0, 0,                       # 6  StartMatchingType  7  EndMatchingType
                          thin,                       # 8  IsThinBody
                          0.0, 0.0,                   # 9  Thickness1  10 Thickness2
                          0,                          # 11 ThinType
                          0,                          # 12 PathAlign
                          True,                       # 13 Merge（凸台才有的，切除版没这个参数）
                          False,                      # 14 UseFeatScope
                          bool(c["auto"]),            # 15 UseAutoSelect
                          0.0,                        # 16 TwistAngle
                          True,                       # 17 BMergeSmoothFaces
                          circular,                   # 18 CircularProfile
                          mm(dia),                    # 19 CircularProfileDiameter
                          0)                          # 20 Direction
        sw.clear_selection()
        got = _new_feature(sw, tree0, skip=(prof,))
        if got is None:
            notes.append(f"{c['tag']}：特征树没新增"
                         + (f"（COM 报错：{err}）" if err else ""))
            continue
        if isinstance(got, tuple):
            notes.append(f"{c['tag']}：特征重建失败 {got[1]}"
                         f"（{FEATURE_ERROR_NAMES.get(got[1], '未知')}）")
            _drop_feature(sw, got[2])
            continue
        if vol0 is not None:
            vol1 = _volume(sw)
            if vol1 is not None and abs(vol1 - vol0) < 1e-12:
                notes.append(f"{c['tag']}：特征成功但**体积没变**")
                _drop_feature(sw, got)
                continue
        feat, used = got, c["tag"]
        break
    if feat is None:
        raise SwError(
            "扫描凸台失败，3 个方案都没成：\n"
            + "".join(f"    · {x}\n" for x in notes)
            + "  排查：① 轮廓是否穿过路径**起点**（扫掠要求轮廓在路径起点处）\n"
              "        ② 轮廓平面是否垂直于路径在该点的切向\n"
              "        ③ 路径真的建出来了吗（feature_names 里看得到吗）")
    return _finish(sw, feat, step, ctx,
                   {"profile": prof, "path": path, "variant": used,
                    "volume_before": vol0, "volume_after": _volume(sw),
                    "notes": notes or None})


# ------------------------------------------------------------ 工程特征：拔模 / 筋 / 镜像实体

@op("draft", "拔模", "feature",
    "给面加拔模角（箱座/箱盖分箱面 1°/3°/4°/10°）。\n"
    "API 是 **InsertMultiFaceDraft**（不是矩阵里写的 InsertDraft —— 那个不存在）。\n"
    "★ **选择标记是硬约定**（官方文档明写，凭记忆绝对写不对）：\n"
    "     拔模方向（平面/基准面/边/两顶点） mark = **1**\n"
    "     要做拔模的**面**                  mark = **2**\n"
    "     拔模**边**                        mark = **4**\n"
    "`prop` 用 swDraftFacePropagationType_e：None/Tangent/AllLoops/InnerLoops/OuterLoops。",
    api=["IFeatureManager.InsertMultiFaceDraft"],
    params={
        "angle": {"type": "deg", "required": True, "desc": "拔模角"},
        "faces": {"type": "list", "required": False,
                  "desc": '拔模面：坐标拾取 [{"point":[x,y,z]}, ...]'},
        "edges": {"type": "list", "required": False,
                  "desc": '拔模边（edge_draft=True 时用）'},
        "direction": {"type": "str", "required": False,
                      "desc": "中性面/拔模方向（基准面名）。不给就用 faces 里第一个面的相对面"},
        "edge_draft": {"type": "bool", "required": False, "default": False,
                       "desc": "True=按边拔模（选边）；False=按面拔模"},
        "flip": {"type": "bool", "required": False, "default": False},
        "prop": {"type": "str", "required": False, "default": "swFacePropNone",
                 "desc": "swDraftFacePropagationType_e 成员名"},
        "step_draft": {"type": "bool", "required": False, "default": False},
        "body_draft": {"type": "bool", "required": False, "default": False},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",), verified=True)
def op_draft(sw, step, ctx):
    ang = _p(step, ctx, "angle")
    if ang is None:
        raise SwError("draft 需要 angle（拔模角，度）")
    faces = _p(step, ctx, "faces") or []
    edges = _p(step, ctx, "edges") or []
    edge_draft = bool(_p(step, ctx, "edge_draft", False))
    targets = edges if edge_draft else faces
    if not targets:
        raise SwError(f"draft 需要 {'edges' if edge_draft else 'faces'}（坐标拾取列表）")
    t_mark = 4 if edge_draft else 2
    prop = _p(step, ctx, "prop", "swFacePropNone")
    prop = int(prop) if str(prop).isdigit() else _C(sw, str(prop), 0)
    # ★★ 建特征调用重试（2026-10-12，坑表 §146）：`InsertMultiFaceDraft` 与
    #   fillet/chamfer 同款**间歇性静默失败**；且原来它被 `contextlib.suppress(Exception)`
    #   包住 —— 那是**判据盲区**（"函数跑过"≠"生效了"）。改走 `_call` 留证 + 特征树 diff 判定，
    #   没新增特征就重选重调。
    got, err, attempts, sel_ok = None, None, 0, False
    for attempts in range(1, COM_RETRY + 1):
        sw.clear_selection()
        # 先选拔模方向（mark 1）—— 必须是**平面类**对象（基准面或平面）
        d = _p(step, ctx, "direction")
        if d:
            if not sw.select(d, "plane", mark=1):
                if not _pick(sw, {"name": d, "type": "PLANE"}, mark=1, append=True):
                    if attempts < COM_RETRY:
                        time.sleep(COM_RETRY_S)
                        continue
                    raise SwError(f"选不中拔模方向/中性面 '{d}'")
        hit, miss = _pick_all(sw, targets, mark=t_mark)
        if hit == 0:
            if attempts < COM_RETRY:
                time.sleep(COM_RETRY_S)
                continue
            raise SwError(f"拔模面/边一个都没选中：{miss}\n"
                          "  坐标是全局 mm 坐标，必须正好落在面上/棱上。")
        sel_ok = True
        tree0 = {n for _t, n in sw.feature_names()}
        _res, err = _call(member, _fm(sw), "InsertMultiFaceDraft",
                          deg(float(ang)),
                          bool(_p(step, ctx, "flip", False)),
                          edge_draft, prop,
                          bool(_p(step, ctx, "step_draft", False)),
                          bool(_p(step, ctx, "body_draft", False)))
        sw.clear_selection()
        got = _new_feature(sw, tree0)
        if got is not None:
            break
        if attempts < COM_RETRY:
            time.sleep(COM_RETRY_S)
    if got is None:
        raise SwError(
            "拔模没生成特征。这个 op 是全项目最易失败的之一，逐条排查：\n"
            "  ① 拔模方向给的是**平面**类对象吗（基准面/平面，不是边）\n"
            "  ② 要做拔模的面与中性面是否相接（不相接拔不出来）\n"
            "  ③ 角度是否过大（面被拔穿 → 重建失败）\n"
            "  ④ prop 换成 swFacePropTangent 或 swFacePropNone 再试\n"
            "  ⑤ 兜底：改走「用带拔模的拉伸」（FeatureExtrusion3 的拔模参数），见 30-pitfalls\n"
            + (f"  （COM 报错：{err}）\n" if err else "")
            + f"  （已重试 {attempts} 次，选择集 = {'非空' if sel_ok else '空'}）")
    if isinstance(got, tuple):
        ec = got[1]
        _drop_feature(sw, got[2])
        raise SwError(f"拔模特征重建失败 {ec}（{FEATURE_ERROR_NAMES.get(ec, '未知')}）—— "
                      "角度太大把面拔穿了，减小 angle 或换个中性面")
    return _finish(sw, got, step, ctx,
                   {"angle": float(ang), "targets": hit, "missed": miss or None,
                    "edge_draft": edge_draft, "prop": prop, "attempts": attempts})


@op("rib", "筋", "feature",
    "加强筋（箱体图上 5 处）。\n"
    "★★ 真机实测（2026-09-25，见 30-pitfalls §35）：现行 API 是\n"
    "   **IFeatureManager.InsertRib**（10 参、返回 void）；\n"
    "   已被官方标 Obsolete 的 `IModelDoc2.InsertRib2`（9 参）只作兜底。\n"
    "   这两条方法在文档页上**参数都抓不出来**（见 sw_apisig --gaps），\n"
    "   真签名是用 `sw_apisig.py --tlb` 从 sldworks.tlb 里挖的：\n"
    "     InsertRib(Is2Sided, ReverseThicknessDir, Thickness, ReferenceEdgeIndex,\n"
    "               ReverseMaterialDir, IsDrafted, DraftOutward, DraftAngle,\n"
    "               IsNormToSketch, IsDraftedFromWall) → void\n"
    "   （InsertRib2 只少最后一个 IsDraftedFromWall）\n"
    "★★★ 最容易踩的坑：**IsNormToSketch 必须是 False**。\n"
    "   给 True → **静默无操作**：不报错、不建特征、体积不变。\n"
    "   「9 组参数全灭」的真凶就是它，厚度/材料方向/选择标记都无关\n"
    "   （mark 0/1/4 都能建）。所以本 op 的变体顺序是「用户值 → False → True」，\n"
    "   正常情况下第一次就中。\n"
    "★ 怎么验证：返回 void，没法靠返回值判定 ⇒ 用**特征树新增 + 体积增加**。\n"
    "   体积增量应≈「开口轮廓与实体围出的那块面积 × 筋厚」——\n"
    "   实测 L 形支座：三角形 150 mm² × 6 mm = 900 mm³，分毫不差。\n"
    "   ⚠ 体积没增加 = 筋长到实体外面了，本 op 会**删掉重试**下一个变体。\n"
    "★ `ReferenceEdgeIndex` 是「草图里的哪条边」的**下标**（0 起），"
    "它决定材料朝哪边长，比 ReverseMaterialDir 更直观 —— 先试 0、1、2。",
    api=["IFeatureManager.InsertRib", "IModelDoc2.InsertRib"],
    params={
        "sketch": {"type": "str", "required": False, "desc": "筋的轮廓草图（不闭合的开口草图）"},
        "thickness": {"type": "mm", "required": True, "desc": "筋厚，通常 = 壁厚 0.8 倍"},
        "both_sides": {"type": "bool", "required": False, "default": False,
                       "desc": "True=两侧对称长厚"},
        "reverse_thickness": {"type": "bool", "required": False, "default": False},
        "reverse_material": {"type": "bool", "required": False, "default": False},
        "ref_edge_index": {"type": "int", "required": False, "default": 0,
                           "desc": "草图里用作材料方向基准的边的下标；配合 reverse_material 轮试"},
        "drafted": {"type": "bool", "required": False, "default": False},
        "draft_outward": {"type": "bool", "required": False, "default": False},
        "draft_angle": {"type": "deg", "required": False, "default": 0},
        "normal_to_sketch": {"type": "bool", "required": False, "default": False,
                             "desc": "★ 真机实测 True 会**静默无操作**，默认 False，别改"},
        "drafted_from_wall": {"type": "bool", "required": False, "default": False,
                              "desc": "仅 IFeatureManager.InsertRib 有的第 10 个参数"},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",), verified=True)
def op_rib(sw, step, ctx):
    sk = _p(step, ctx, "sketch") or ctx.get("last_sketch")
    th = _p(step, ctx, "thickness")
    if not sk:
        raise SwError("rib 需要 sketch（筋的轮廓草图名）")
    if th is None:
        raise SwError("rib 需要 thickness（mm）")
    vol0 = _volume(sw)
    notes: list[str] = []

    is2sided = bool(_p(step, ctx, "both_sides", False))
    rev_th = bool(_p(step, ctx, "reverse_thickness", False))
    drafted = bool(_p(step, ctx, "drafted", False))
    outward = bool(_p(step, ctx, "draft_outward", False))
    angle = deg(float(_p(step, ctx, "draft_angle", 0) or 0))
    from_wall = bool(_p(step, ctx, "drafted_from_wall", False))
    idx0 = int(_p(step, ctx, "ref_edge_index", 0) or 0)
    rev0 = bool(_p(step, ctx, "reverse_material", False))
    norm_pref = bool(_p(step, ctx, "normal_to_sketch", False))
    thick = mm(float(th))

    # 变体顺序：已知能成的排最前，避免"建了又删"。
    norm_order = [norm_pref] + [v for v in (False, True) if v != norm_pref]
    combos = (dict(idx=idx0, rev=rev0), dict(idx=idx0, rev=not rev0),
              dict(idx=idx0 + 1, rev=rev0))
    variants = []
    for norm in norm_order:
        for c in combos:
            variants.append((
                f"IFeatureManager.InsertRib 边#{c['idx']}"
                + ("/反向" if c["rev"] else "") + f"/法向={norm}",
                (lambda n=norm, cc=c: _call(
                    member, _fm(sw), "InsertRib", is2sided, rev_th, thick,
                    int(cc["idx"]), bool(cc["rev"]), drafted, outward, angle,
                    bool(n), from_wall))))
    for c in combos:                       # 兜底：老接口（8 参，无 IsNormToSketch）
        variants.append((
            f"IModelDoc2.InsertRib(旧,8参) 边#{c['idx']}"
            + ("/反向" if c["rev"] else ""),
            (lambda cc=c: _call(
                member, sw.model, "InsertRib", is2sided, rev_th, thick,
                int(cc["idx"]), bool(cc["rev"]), drafted, outward, angle))))

    feat, used = None, ""
    for label, fn in variants:
        sw.clear_selection()
        if not sw.select(sk, "sketch", mark=0):
            raise SwError(f"选不中筋草图 '{sk}'")
        tree0 = {n for _t, n in sw.feature_names()}
        _res, err = fn()
        sw.clear_selection()
        got = _new_feature(sw, tree0, skip=(sk,))
        if got is None:
            notes.append(f"{label}：特征树没新增（静默无操作）"
                         + (f"（COM 报错：{err}）" if err else ""))
            continue
        if isinstance(got, tuple):
            notes.append(f"{label}：重建失败 {got[1]}"
                         f"（{FEATURE_ERROR_NAMES.get(got[1], '未知')}）")
            _drop_feature(sw, got[2])
            continue
        if vol0 is not None:
            vol1 = _volume(sw)
            if vol1 is not None and vol1 <= vol0 + 1e-12:
                notes.append(f"{label}：特征成功但**体积没增加**（筋长到实体外面了）")
                _drop_feature(sw, got)
                continue
        feat, used = got, label
        break
    if feat is None:
        raise SwError(
            "加筋失败，所有方案都没成：\n"
            + "".join(f"    · {x}\n" for x in notes)
            + "  排查：① 筋草图必须是**开口**的（两端搭在实体上），闭合草图不行\n"
              "        ② 草图两端要真的碰到实体（悬空长不出筋）\n"
              "        ③ 草图最好画在**实体的面**上；画在穿过实体内部的基准面上不稳\n"
              "        ④ 兜底：筋 = 三角形剖面的 MidPlane 拉伸，"
              "直接用 extrude_boss 更可控（推荐兜底路线）")
    return _finish(sw, feat, step, ctx,
                   {"thickness": float(th), "variant": used,
                    "volume_before": vol0, "volume_after": _volume(sw),
                    "notes": notes or None})


@op("mirror_body", "镜像实体", "feature",
    "关于一个平面镜像**实体/特征/面**（箱体左右对称结构批量搞定）。\n"
    "★ 选择标记（官方文档明写）：实体=**256**、面=**128**、特征=**1**、镜像面/平面=**2**。\n"
    "  与已有的 `mirror`（特征级镜像）区别：这个可以镜像**整个实体**。",
    api=["IFeatureManager.InsertMirrorFeature2"],
    params={
        "plane": {"type": "str", "required": True, "desc": "镜像面（基准面名或平面）"},
        "bodies": {"type": "bool", "required": False, "default": True,
                   "desc": "True=镜像实体（配合 merge=False 出第二个实体）"},
        "body_point": {"type": "list", "required": False,
                       "desc": "落在这个实体**表面上**的坐标，用来射线选中该实体"},
        "features": {"type": "list", "required": False, "desc": "要镜像的特征名列表"},
        "faces": {"type": "list", "required": False, "desc": "要镜像的面（坐标拾取）"},
        "geometry_pattern": {"type": "bool", "required": False, "default": False,
                             "desc": "True=只镜像特征几何（快）；False=重解整个特征（稳，实测这条能建成）"},
        "merge": {"type": "bool", "required": False, "default": True,
                  "desc": "True=镜像结果与原件合并成一体；False=多出一个实体"},
        "knit": {"type": "bool", "required": False, "default": True},
        "scope": {"type": "int", "required": False, "default": 0,
                  "desc": "swFeatureScope_e，仅 IM2 兜底路径用"},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",), verified=True)
def op_mirror_body(sw, step, ctx):
    plane = _p(step, ctx, "plane")
    if not plane:
        raise SwError("mirror_body 需要 plane（镜像面）")
    bodies = bool(_p(step, ctx, "bodies", True))
    feats = _p(step, ctx, "features") or []
    faces = _p(step, ctx, "faces") or []
    body_pt = _p(step, ctx, "body_point")
    sw.clear_selection()
    got_any = False
    # ★ 顺序有讲究：`_select_feature` 内部是 append=False（会清空选择），
    #   所以按特征名选必须**放在最前面**，后面几项才用 append=True 往上叠。
    for fn in feats:
        if _select_feature(sw, fn, 1):
            got_any = True
    # ★ 实体怎么选：SW2024 的 API 里**没有** SelectAllByType（CHM 里查无此名，实测也调不到），
    #   所以只能靠「点在实体表面上」的射线拾取。要镜像整个实体就给 body_point。
    if bodies and body_pt:
        if _pick(sw, {"type": "SOLIDBODY", "point": body_pt}, mark=256, append=True):
            got_any = True
    if faces:
        n, _miss = _pick_all(sw, faces, mark=128)
        got_any = got_any or n > 0
    if not got_any:
        raise SwError(
            "mirror_body 没有可镜像的对象。给三者之一：\n"
            "  · features=['拉伸1', ...]  按**特征名**镜像（最稳，推荐）\n"
            "  · faces=[{\"point\":[x,y,z]}]  按面镜像\n"
            "  · body_point=[x,y,z] + bodies=True  镜像整个实体"
            "（坐标要落在这个实体的**表面上**——\n"
            "    SolidWorks 不支持按类型批量选实体，SW2024 里没有 SelectAllByType）")
    if not sw.select(plane, "plane", append=True, mark=2):
        if not _pick(sw, {"name": plane, "type": "PLANE"}, mark=2, append=True):
            raise SwError(f"选不中镜像面 '{plane}'")
    merge = bool(_p(step, ctx, "merge", True))
    geom = bool(_p(step, ctx, "geometry_pattern", False))
    knit = bool(_p(step, ctx, "knit", True))
    scope = int(_p(step, ctx, "scope", 0) or 0)
    b0 = _body_count(sw)
    vol0 = _volume(sw)
    part = typed(sw.model, "IPartDoc")

    def reselect() -> None:
        """每次试下一个 API 前重新选一遍 —— 失败的调用会吃掉当前选择。"""
        sw.clear_selection()
        for fn in feats:
            _select_feature(sw, fn, 1)
        if bodies and body_pt:
            _pick(sw, {"type": "SOLIDBODY", "point": body_pt}, mark=256, append=True)
        if faces:
            _pick_all(sw, faces, mark=128)
        if not sw.select(plane, "plane", append=True, mark=2):
            _pick(sw, {"name": plane, "type": "PLANE"}, mark=2, append=True)

    # ★★ 真机实测（2026-09-25）：`IFeatureManager.InsertMirrorFeature2` 在这台机器上
    #    **永远返回空、也建不出特征**（选择状态已确认正确：特征 mark=1 / 平面 mark=2，
    #    scope 0/1/2、BMerge True/False、选择顺序正反 全试过，共 8 组全灭）。
    #    而 **`IPartDoc.InsertMirrorFeature(GeometryPattern)` 一调就出「镜向1」** ——
    #    宏录制出来的也是这条。所以把它排第一，IM2 只作兜底。
    #   ⚠ GeometryPattern 两种模式都要试：实测 False（重解整个特征）能建成，
    #     True（只镜像特征几何）**建不出来却也不报错** —— 所以不能只赌一个值。
    # ★ 顺序按**镜像对象**决定（真机实测，见 30-pitfalls §34.1）：
    #   · 镜像**实体** → `IFeatureManager.InsertMirrorFeature` 才是能用的那条
    #     （`IPartDoc.InsertMirrorFeature` 在这条路上静默不建）；
    #   · 镜像**特征** → 只有 `IPartDoc.InsertMirrorFeature` 会建出特征
    #     （但常报 51「草图外部参考失效」，所以要继续轮试）。
    #   ⚠ GeometryPattern 两种模式都要试：False（重解整个特征）可能建坏、
    #     True（只镜像特征几何）可能静默不建 —— 不能只赌一个值。
    cands = []
    if bodies and not feats:
        cands.append(("IFeatureManager.InsertMirrorFeature(镜像实体)",
                      lambda: member(_fm(sw), "InsertMirrorFeature",
                                     True, geom, merge, knit)))
    for g in (geom, not geom):
        cands.append((f"IPartDoc.InsertMirrorFeature(gp={g})",
                      (lambda gg=g: member(part, "InsertMirrorFeature", gg))))
    cands.append(("IFeatureManager.InsertMirrorFeature",
                  lambda: member(_fm(sw), "InsertMirrorFeature",
                                 bool(bodies), geom, merge, knit)))
    cands.append(("IFeatureManager.InsertMirrorFeature2",
                  lambda: member(_fm(sw), "InsertMirrorFeature2",
                                 bool(bodies), geom, merge, knit, scope)))
    notes: list[str] = []
    got, used = None, ""
    for label, fn in cands:
        reselect()
        tree0 = {n for _t, n in sw.feature_names()}
        _res, err = _call(fn)
        sw.clear_selection()
        cand = _new_feature(sw, tree0)
        if cand is None:
            notes.append(f"{label}：特征树没新增" + (f"（{err}）" if err else ""))
            continue
        if isinstance(cand, tuple):
            # ★ 建出来了但**重建失败** —— 这不等于这条路走不通，只是这一组参数不行。
            #   必须删掉、继续试下一个变体，不能直接 raise
            #   （实测 gp=False 会报 51「草图外部参考失效」，而紧接着的 gp=True 就成了）。
            notes.append(f"{label}：特征建出来了但重建失败 {cand[1]}"
                         f"（{FEATURE_ERROR_NAMES.get(cand[1], '未知')}）")
            _drop_feature(sw, cand[2])
            continue
        got, used = cand, label
        break
    if got is None:
        raise SwError(
            "镜像没生成特征，三个 API 都没成：\n"
            + "".join(f"    · {x}\n" for x in notes)
            + "  排查（**先看你刚才选中的到底是什么** —— 可用 "
              "SelectionManager.GetSelectedObjectMark 复核选择状态）：\n"
              "  ① 镜像面选上了吗（mark 必须是 2）\n"
              "  ② 要镜像的东西选上了吗（特征=1 / 面=128 / 实体=256）\n"
              "  ③ 实体与镜像面**相交**时镜像会失败 —— 换一个不相交的基准面")
    if isinstance(got, tuple):
        _drop_feature(sw, got[2])
        raise SwError(f"镜像特征重建失败 {got[1]}"
                      f"（{FEATURE_ERROR_NAMES.get(got[1], '未知')}）")
    return _finish(sw, got, step, ctx,
                   {"api": used, "bodies_before": b0, "bodies_after": _body_count(sw),
                    "volume_before": vol0, "volume_after": _volume(sw),
                    "merged": merge, "notes": notes or None})


# ------------------------------------------------------------ 孔：异型孔向导 / 螺纹线

@op("hole_wizard", "异型孔向导", "feature",
    "异型孔向导 —— **参数最多的一个 op（27 个）**，箱体的沉孔/过孔全靠它。\n"
    "★★ 三种「给孔位」的方式（2026-09-25 真机实测，见 30-pitfalls §33）——\n"
    "  之前 9 组参数全灭，就死在「选错了目标」：\n"
    "    ① `points` = **孔位的全局坐标列表**（推荐，一次可出多个孔）\n"
    "       孔位点要先画在**实体面**上的草图里；本 op 用\n"
    "       `SelectByID2('', 'EXTSKETCHPOINT', x, y, z, …)` 逐个选中（第 2 个起 append）\n"
    "       → 调一次 HoleWizard5 → **一个特征里含全部孔**（标准形态）。\n"
    "    ② `face` = 面上一个全局坐标点 → 只出**一个**孔（孔位=拾取点）。\n"
    "    ③ `sketch` = 选整张草图 —— **实测选上了也建不出来**，仅作兜底。\n"
    "★★ 尾部 5 个布尔必须照**官方示例**给 `False, True, True, True, True, False`；\n"
    "   照「参数说明」自己推的 `False, False, True, False, True, False` 建不出东西。\n"
    "★★★ **只有「完全贯穿」能稳定建成**（真机矩阵实测，2026-09-25）：\n"
    "  ✅ `swWzdHole`+`swStandardGBScrewClearances`(356)+`'M8'`+`swEndCondThroughAll`\n"
    "     → 去料 4433 mm³ = 2 × **Ø8.4** × 40mm（GB/T 5277 细系列 M8 过孔），分毫不差\n"
    "  ✅ `swWzdCounterBore`+`swStandardGBHexagonHeadBoltsGB`(376)+`'M8'`+贯通 → 柱沉孔\n"
    "  ❌ 盲孔（`swEndCondBlind`，depth 给不给都一样）—— 8 组参数全灭\n"
    "  ❌ **螺纹孔**（`swWzdTap` 359 / 锥管螺纹 3 全灭，size 试过 M8 / M8x1.25、\n"
    "     螺纹等级试过 1B/2B）—— 换规格串、换等级、换盲/通都没用\n"
    "  ⇒ 结论：**要「螺纹孔」就拆成两步** —— `hole_wizard` 出过孔（按螺纹\n"
    "     小径/中径给 size）+ `thread` 加装饰螺纹线。出图效果等价，且两步都是实测通的\n"
    "     （螺塞 M10×1 → 过孔 + 装饰螺纹线，正是本 skill 给减速器的推荐链路）。\n"
    "  ⚠ 验收一定要看**去料体积对不对**：`swStandardGBDrillSizes`(355)+`'Ø9'`\n"
    "     会「特征成功但只啃掉 227 mm³」—— 规格串没被认出来，退化成超小孔。\n"
    "关键三参（CHM 原文）：\n"
    "  `generic`   → swWzdGeneralHoleTypes_e：0=柱形沉孔 1=锥形沉头 2=直孔 4=螺纹孔 3=锥管螺纹\n"
    "  `standard`  → swWzdHoleStandards_e：**国标 = 13**（swStandardGB），ISO=8\n"
    "  `fastener`  → swWzdHoleStandardFastenerTypes_e，国标相关的有：\n"
    "               355=钻模孔(直孔) 356=螺栓过孔 376=六角头螺栓 359=螺纹孔 358=螺纹底孔\n"
    "  `size`      → 规格字符串，如 'M8'（**过孔用 356 + 'M8' 最稳**）\n"
    "  `end_type`  → swEndConditions_e（1=完全贯穿 0=盲孔 …）\n"
    "Value1~Value12 是孔型的细节参数（沉孔直径/深度、螺纹深度…），随孔型而变，"
    "用 values 直接按顺序喂。",
    api=["IFeatureManager.HoleWizard5"],
    params={
        "points": {"type": "list", "required": False,
                   "desc": "孔位**全局坐标**列表 [[x,y,z],...]；孔位点须已画在实体面的草图里"},
        "face": {"type": "list", "required": False,
                 "desc": "面上一个全局坐标点 [x,y,z]（单孔，孔位=拾取点）"},
        "sketch": {"type": "str", "required": False,
                   "desc": "孔位草图名（兜底路径，实测不好使）"},
        "generic": {"type": "str", "required": False, "default": "swWzdHole",
                    "desc": "swWzdGeneralHoleTypes_e 成员名或数字"},
        "standard": {"type": "str", "required": False, "default": "swStandardGB",
                     "desc": "swWzdHoleStandards_e 成员名或数字；国标=13"},
        "fastener": {"type": "str", "required": False, "default": "0",
                     "desc": "swWzdHoleStandardFastenerTypes_e 成员名或数字"},
        "size": {"type": "str", "required": True, "desc": "规格，如 'M8'"},
        "end_type": {"type": "str", "required": False, "default": "swEndCondBlind"},
        "diameter": {"type": "mm", "required": False, "default": 0},
        "depth": {"type": "mm", "required": False, "default": 0,
                  "desc": "盲孔深度；⚠ 盲孔实测建不出来，只作参数完整性保留"},
        "length": {"type": "mm", "required": False, "default": 0},
        "values": {"type": "list", "required": False, "desc": "Value1~Value12，按顺序给（mm）"},
        "thread_class": {"type": "str", "required": False, "default": "",
                         "desc": "螺纹等级 1B/2B/3B，仅 ANSI 英寸标准用"},
        "reverse_dir": {"type": "bool", "required": False, "default": False},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",), verified=True)
def op_hole_wizard(sw, step, ctx):
    size = _p(step, ctx, "size")
    if not size:
        raise SwError("hole_wizard 需要 size（规格，如 'M8'）")
    pts = _p(step, ctx, "points") or []
    face_pt = _p(step, ctx, "face")
    sk = _p(step, ctx, "sketch") or ctx.get("last_sketch")

    def _num(v, default=None):
        if v is None:
            return default
        s = str(v)
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
        c = _C(sw, s, None)
        return default if c is None else c

    generic = _num(_p(step, ctx, "generic", "swWzdHole"), _C(sw, "swWzdHole", 2))
    standard = _num(_p(step, ctx, "standard", "swStandardGB"), _C(sw, "swStandardGB", 13))
    fastener = _num(_p(step, ctx, "fastener", 0), 0)
    endc = _resolve_end_cond(sw.C, _p(step, ctx, "end_type", "swEndCondBlind"))
    vals = [mm(float(v)) for v in (_p(step, ctx, "values") or [])]
    vals += [0.0] * (12 - len(vals))
    vol0 = _volume(sw)
    # ★ 尾部布尔照**官方示例**（HoleWizard5 帮助里那条 VBA）给。
    #   自己按「参数说明」推的那套实测建不出任何东西。
    #   ⚠ 注意 RevDir 已经在上面的 args 里给过了，这里只剩**5 个**：
    #     FeatureScope, AutoSelect, AssemblyFeatureScope, AutoSelectComponents,
    #     PropagateFeatureToParts。曾经多写一个 RevDir ⇒
    #     `TypeError: takes from 1 to 28 positional arguments but 29 were given`。
    TAIL = (True, True, True, True, False)
    args = (generic, standard, fastener, str(size), endc,
            mm(float(_p(step, ctx, "diameter", 0) or 0)),
            mm(float(_p(step, ctx, "depth", 0) or 0)),
            mm(float(_p(step, ctx, "length", 0) or 0)),
            *vals[:12],
            str(_p(step, ctx, "thread_class", "") or ""),
            bool(_p(step, ctx, "reverse_dir", False)))
    opt = _C(sw, "swSelectOptionExtensive", 1)

    def sel_points() -> int:
        """按**全局坐标**逐个选孔位点（第 2 个起 append）—— 一次出多个孔的关键。"""
        sw.clear_selection()
        n = 0
        for i, pt in enumerate(pts):
            x, y, z = (mm(float(v)) for v in pt)
            if sw.ext.SelectByID2("", "EXTSKETCHPOINT", x, y, z, i > 0, 0, None, opt):
                n += 1
        return n

    def sel_face() -> int:
        sw.clear_selection()
        x, y, z = (mm(float(v)) for v in face_pt)
        return 1 if sw.ext.SelectByID2("", "FACE", x, y, z, False, 0, None, opt) else 0

    def sel_sketch(mark: int) -> int:
        sw.clear_selection()
        return 1 if sw.select(sk, "sketch", mark=mark) else 0

    cands: list[tuple[str, object]] = []
    if pts:
        cands.append((f"选 {len(pts)} 个孔位点", sel_points))
    if face_pt:
        cands.append(("选面（单孔）", sel_face))
    if sk:
        for mk in (0, 1, 2):
            cands.append((f"选草图 mark={mk}", lambda m=mk: sel_sketch(m)))
    if not cands:
        raise SwError(
            "hole_wizard 需要孔位：给 `points`（孔位全局坐标列表）或 `face`（面上一点）。\n"
            "  孔位点要先画在**实体的面**上：select_face → sketch_begin → point → sketch_end，\n"
            "  再把这些点的**全局坐标**写进 points。画在穿过实体内部的基准面上会静默失败。")

    notes: list[str] = []
    for label, prep in cands:
        n = prep()
        if not n:
            notes.append(f"{label}：一个都没选中")
            continue
        tree0 = {x for _t, x in sw.feature_names()}
        _res, err = _call(_fm(sw).HoleWizard5, *args, *TAIL)
        sw.clear_selection()
        got = _new_feature(sw, tree0, skip=tuple([sk] if sk else ()))
        if got is None:
            notes.append(f"{label}：没生成特征" + (f"（COM 报错：{err}）" if err else ""))
            continue
        if isinstance(got, tuple):
            notes.append(f"{label}：重建失败 {got[1]}"
                         f"（{FEATURE_ERROR_NAMES.get(got[1], '未知')}）")
            _drop_feature(sw, got[2])
            continue
        return _finish(sw, got, step, ctx,
                       {"generic": generic, "standard": standard,
                        "fastener": fastener, "size": size, "end_type": endc,
                        "variant": label, "picked": n,
                        "volume_before": vol0, "volume_after": _volume(sw),
                        "notes": notes or None})
    raise SwError(
        f"异型孔向导没生成特征（generic={generic} standard={standard} "
        f"fastener={fastener} size={size!r} end={endc}）：\n"
        + "".join(f"    · {x}\n" for x in notes)
        + "  排查：① **孔位点要落在实体的面**上（不是穿实体内部的基准面）—— 最常翻在这\n"
          "        ② 孔位点用**模型全局坐标**选（mm），取自你画点时的位置\n"
          "        ③ **只支持「完全贯穿」**：盲孔、螺纹孔实测都建不出来\n"
          "           · 要过孔：generic=swWzdHole + fastener=swStandardGBScrewClearances + 'M8'\n"
          "           · 要沉孔：generic=swWzdCounterBore + fastener=swStandardGBHexagonHeadBoltsGB\n"
          "           · 要螺纹孔：**拆成 过孔 + thread（装饰螺纹线）** 两步，别再试 swWzdTap\n"
          "        ④ size 写法要跟标准匹配（过孔用 'M8'；'Ø9' 会被误解析成超小孔）")


@op("thread", "装饰螺纹线", "param",
    "加装饰螺纹线（螺塞 M10、通气塞、螺母 M8 —— 出图时螺纹要有表示）。\n"
    "★ 这是**装饰**：只画线、不改几何，质量属性不受影响。\n"
    "★★ 要给**圆柱端面的圆形边**的坐标，**不是**圆柱面！实测（2026-09-25）：\n"
    "  选圆柱面 → API 返回 None（什么都没建）；选圆边 → 返回一个 Feature 并真的建上。\n"
    "  mark 用 0 即可（实测 0/128/1 都行）。\n"
    "★ 装饰螺纹线**不会出现在 FirstFeature 特征链上**（挂「注解」文件夹），\n"
    "  所以别指望特征树 diff 能验证它 —— 本 op 改为认返回值。\n"
    "★ 参数语义（CHM）：Standard 用 **swCosmeticStandardType_e**（国标=13）；"
    "EndType 用 **swCosmeticEndConditions_e**（0=盲 1=到下一面 2=贯穿）；"
    "Note 是出图时显示的标注文字。",
    api=["IFeatureManager.InsertCosmeticThread3"],
    params={
        "select": {"type": "list", "required": True,
                   "desc": "螺纹所在的面/边（坐标拾取），或圆柱面的坐标"},
        "size": {"type": "str", "required": True, "desc": "规格字符串，如 'M10'"},
        "diameter": {"type": "mm", "required": True, "desc": "螺纹公称直径"},
        "standard": {"type": "str", "required": False, "default": "swStandardType_StandardGB",
                     "desc": "swCosmeticStandardType_e；国标=13（swStandardType_StandardGB）"},
        "standard_type": {"type": "str", "required": False, "default": "Die",
                          "desc": "螺纹类型字符串（'Die' / 'Tapped' 等），随标准而变"},
        "end_type": {"type": "str", "required": False, "default": "swEndConditionBlind",
                     "desc": "swCosmeticEndConditions_e"},
        "depth": {"type": "mm", "required": False, "default": 0},
        "note": {"type": "str", "required": False, "desc": "标注文字，不给就用 size"},
        "name": {"type": "str", "required": False},
    },
    requires=("doc_open",), verified=True)
def op_thread(sw, step, ctx):
    items = _p(step, ctx, "select") or []
    size = _p(step, ctx, "size")
    dia = _p(step, ctx, "diameter")
    if not items:
        raise SwError("thread 需要 select（螺纹所在面的坐标，如 [{\"point\":[0,0,10]}]）")
    if not size or dia is None:
        raise SwError("thread 需要 size（如 'M10'）和 diameter（mm）")
    sw.clear_selection()
    hit, miss = _pick_all(sw, items, mark=128)
    if hit == 0:
        # 兜底：改按面选（有些情况下边比面好选）
        hit, miss = _pick_all(sw, items, mark=0)
    if hit == 0:
        raise SwError(f"螺纹面一个都没选中：{miss}\n"
                      "  坐标要落在**圆柱面上**（不是在轴线上）")
    std = _p(step, ctx, "standard", "swStandardType_StandardGB")
    std = int(std) if str(std).isdigit() else _C(sw, str(std), 13)
    depth = mm(float(_p(step, ctx, "depth", 0) or 0))
    end_given = _p(step, ctx, "end_type")
    if end_given is None:
        # ★ depth=0 + Blind 会生成"零长度盲螺纹"，SolidWorks 直接不建 —— 实测踩到。
        #   不指定端条件时，按有没有深度自动选贯通/盲。
        endc0 = _C(sw, "swEndConditionThrough" if depth <= 0 else "swEndConditionBlind", 2)
    else:
        endc0 = (int(end_given) if str(end_given).isdigit()
                 else _C(sw, str(end_given), 2))
    st0 = str(_p(step, ctx, "standard_type", "Die") or "Die")
    # StandardType 是个**自由的类型名字符串**，CHM 里没给取值表 —— 只能轮试。
    cands = [(st0, endc0),
             (st0, _C(sw, "swEndConditionThrough", 2)),
             ("Tapped", endc0),
             ("", endc0)]
    notes: list[str] = []
    # ★★ 真机实测（2026-09-25，见 30-pitfalls §29）—— 两个反直觉的硬事实：
    #   1) 目标是**圆形边**，不是圆柱面！选圆柱面时 API 直接返回 None（什么都不建）；
    #      选圆边时返回一个 Feature（Name='装饰螺纹线1'，GetTypeName2='CosmeticThread'）。
    #   2) 装饰螺纹线**不在 FirstFeature→GetNextFeature 链上**（挂在「注解」文件夹下），
    #      所以特征树 diff（`_new_feature`）**永远抓不到它** —— 验证只能认返回值。
    #      这正是「明明建上了却报没生成」的根因。
    targets = (("EDGE", 0), ("EDGE", 128), ("FACE", 0), ("FACE", 128))
    for st, ec in cands:
        for typ, mk in targets:
            sw.clear_selection()
            sel: list = []
            for it in items:
                item = dict(it) if isinstance(it, dict) else {"name": str(it)}
                item["type"] = typ
                sel.append(item)
            if _pick_all(sw, sel, mark=mk)[0] == 0:
                notes.append(f"{typ} mark={mk} standard_type={st!r} end={ec}：选不中目标")
                continue
            tree0 = {n for _t, n in sw.feature_names()}
            res, err = _call(_fm(sw).InsertCosmeticThread3,
                             std, st, str(size), mm(float(dia)), ec, depth,
                             str(_p(step, ctx, "note", "") or size))
            sw.clear_selection()
            got = _new_feature(sw, tree0)          # 一般抓不到（见上 2)）
            if res is None and got is None:
                notes.append(f"{typ} mark={mk} standard_type={st!r} end={ec}：返回空"
                             + (f"（{err}）" if err else ""))
                continue
            feat = res if res is not None else got
            out = _finish(sw, feat, step, ctx,
                          {"size": size, "standard": std, "standard_type": st,
                           "end_type": ec, "diameter": float(dia), "depth": depth,
                           "selected": f"{typ} mark={mk}",
                           "note": str(_p(step, ctx, "note", "") or size),
                           "verified_by": "返回的 Feature 对象（装饰螺纹线不进特征链）",
                           "notes": notes or None})
            return out
    raise SwError(
        f"装饰螺纹线没生成（standard={std} size={size!r}）。4 个候选都试过了：\n"
        + "".join(f"    · {x}\n" for x in notes)
        + "  排查：① `standard_type` 是**自由字符串**，CHM 没给取值表 ——"
          "去 SW 界面插入一次螺纹线，看『类型』下拉框里显示什么（Die/Tapped/…）\n"
          "        ② 选中的是不是**圆柱面/圆边**（平面加不上螺纹）\n"
          "        ③ 兜底：用『异型孔向导的螺纹孔』（hole_wizard + fastener=359）"
          "自带螺纹表示，不用单独加螺纹线")


# ------------------------------------------------------------ 材料

@op("material", "指定材料", "param",
    "给当前零件指定材料 —— 表 1 里每一件都要指定，出 BOM 时靠它。\n"
    "★ 只对**零件**有效（装配体没有材料属性）。\n"
    "★ 这个方法在 CHM 里是 `Sub` 形式，签名抽取器抓不到参数；实参顺序是从官方 Remarks 的\n"
    "  示例反推的：`SetMaterialPropertyName2(ConfigName, Database, Name)`。\n"
    "★ Database 留空 = 用默认的 'solidworks materials'；ConfigName 留空 = 当前配置（实测可行）。\n"
    "★★ 材料名要用**材料库里真实存在的名字**。中文界面下库里存的是**中文名**：\n"
    "  '普通碳钢' / '灰铸铁' / '合金钢' / 'AISI 1020 钢，冷轧' …（不是 'Alloy Steel'）。\n"
    "  别猜 —— 直接读 `lang/<语言>/sldmaterials/*.sldmat`（**UTF-16** 的 XML，\n"
    "  用 read_text(encoding='utf-16') 再抓 `<material name=\"...\">`），\n"
    "  或调 `ISldWorks.GetMaterialDatabases()` 拿库路径。",
    api=["IPartDoc.SetMaterialPropertyName2", "IPartDoc.GetMaterialPropertyName2"],
    params={
        "name": {"type": "str", "required": True,
                 "desc": "材料名，如 '灰铸铁' / '普通碳钢' / '合金钢' / 'Alloy Steel'"},
        "config": {"type": "str", "required": False, "default": "", "desc": "配置名，空=当前配置"},
        "database": {"type": "str", "required": False, "default": "",
                     "desc": ".sldmat 文件名，空=默认材料库"},
    },
    requires=("doc_open",), verified=True)
def op_material(sw, step, ctx):
    name = _p(step, ctx, "name")
    if not name:
        raise SwError("material 需要 name（材料名，如 '灰铸铁'）")
    cfg = _p(step, ctx, "config", "") or ""
    db = _p(step, ctx, "database", "") or ""
    before = _read_material(sw)
    part = typed(sw.model, "IPartDoc")
    err = None
    try:
        member(part, "SetMaterialPropertyName2", cfg, db, name)
    except Exception as e:                                    # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
    # ★ Sub 没有返回值，只能**读回来核对**。读不到也别急着报错 ——
    #   有些材料库名/材料名不匹配时它会静默不生效。
    after = _read_material(sw)
    ok = bool(after) and (str(after).strip() == str(name).strip())
    out = {"material": after, "requested": name, "changed": after != before}
    if not ok:
        out["warning"] = (
            f"材料没设上（现在读到的是 {after!r}）。常见原因：\n"
            f"  ① 材料库里没有叫 '{name}' 的材料 —— 换用库里真实存在的名字"
            "（英文库里是 'Alloy Steel'/'Gray Cast Iron'）\n"
            "  ② 该配置名不存在（config 传了但模型里没有这个配置）\n"
            "  ③ 工具 > 选项 > 文件位置 > 材料数据库 没配")
        if err:
            out["warning"] += f"\n  ④ COM 调用本身报错：{err}"
    return out


# ============================================================ 机读 schema 导出

# ★「已知边界」集中表。为什么不在 71 个装饰器里各写一遍：一处一改容易漏，
#   而且已经有好几条是从真机血泪里换来的、必须能被 Agent 看到（见 70-direction-review.md §2-④）。
#   这里的每一条都对应 references/30-pitfalls.md 里的一个坑号。
EXTRA_LIMITS = {
    "hole_wizard": ("只有「完全贯穿」稳（探针覆盖）；盲孔 / 螺纹孔 / 沉头孔**未验证**",
                    "要沉孔用「柱形沉头孔」类型时务必量体积确认真的去了料"),
    "chamfer": ("只有 swChamferAngleDistance(1) 真去料；swChamferEqualDistance(16) "
                "**静默只加特征名、体积不变**（六变体实测，§46）",
                "edges 走坐标最近邻匹配，容差给大了会选到邻居边"),
    "thread": ("**装饰**螺纹线（不画牙、不去料）。要真切出螺纹用 real_thread",
               "必须先设 Diameter/Pitch 的 *Override 开关，否则静默无操作"),
    "real_thread": ("走 SweepThread 生成真实螺纹，**会真切掉材料**（体积必须变）",
                    "长径比过大时扫掠可能失败；比装饰螺纹慢得多"),
    "fully_define": ("★★ 2026-10-01 实测**为 no-op**：rc=0 Success 但 GetConstrainedStatus "
                     "一直是 2(UnderConstrained)，8 种草图形状 × 6 组参数 × AddToDB 时机 "
                     "× 窗口可见性全部试过，连居中矩形都不过",
                     "与 2026-09-25 复盘记录的 2→3 **不可复现**（那份凭据 assets/review/m3_probe.json "
                     "仍在，但结论要按「未复现」对待）",
                     "★ 要守「草图必须用真尺寸画」这条口径，请用 dim_auto + "
                     "check_sketch_constraint(expect_min_dims=N)"),
    "dim_auto": ("回转体半剖面（识别出**圆柱面**）才走**尺寸链**；圆/弧/多边形轮廓"
                 "退回逐个标注（2026-10-02 实测：4 段折线矩形标上 **4 条**）",
                 "★ 尺寸链靠**圆柱面**定位 ⇒ 必须排在 `revolve_boss` **之后**"
                 "（转之前没有圆柱面，会静默退回逐个标）",
                 "★ 会**退出再重进草图** ⇒ 计划里后面必须再补一次 `sketch_end`",
                 "标完**不是**完全定义 —— 尺寸只约束大小，定位还得靠关系/锚点",
                 "name-probing 兜底路径很慢（实测 80s+），非必要别在大计划里反复调"),
    "sk_relation": ("只实现了 **coincident / 水平 / 竖直 / fix** 四类；"
                    "相切/同心/对称/共线需按需扩展（`SketchAddConstraints` 的字符串表已核）",
                    "★ `fix` 只锁**点**：锁整个图元会变 OverConstrained 且尺寸被锁死（§54）",
                    "★ 关系**不产生尺寸** —— 规范 §3 的顺序是先关系后尺寸，"
                    "标尺寸仍要走 `dim_auto`/`dimension`",
                    "★ 2026-10-02 实测：补完关系后草图**仍可能是 UnderConstrained(2)** —— "
                    "因为还缺「型面到原点/中心线的**定位尺寸**」",
                    "判据看 `status_after`，不要看返回个数"),
    "sketch_fillet": ("一次只倒**两条**线段（且依赖 ctx.segments 的记录顺序）",
                      "四角全倒要调四次；矩形+圆角拼跑道形不稳，宁可显式画线+弧"),
    "arc3p": ("点序 = **起点 / 终点 / 弧上点**，不是「起点/中点/终点」——"
              "写成后者只画出 90° 弧、拼不成闭环",
              "三点共线直接返回 None"),
    "rib": ("加筋 IsNormToSketch=False 是必需的，不给会静默无操作"),
    "swept_boss": ("路径必须与轮廓平面相交；自相交路径失败",
                   "轮廓要 Mark=0、路径 Mark=1"),
    "extrude_boss": ("两头对称必须用 swEndCondMidPlane，**不能给负深度**",
                     "depth 单位 mm（op 内部换算）；贯通类可省 depth"),
    "shell": ("抽壳失败常见于壁厚 >= 最小曲率半径处",),
    "save_as": ("SaveAs 返回值**会说谎**；判据是文件真的出现 + 文档路径变了"),
    "select_face": ("按坐标/方向选面，容差过大在箱体那种多面件上会选错",),
    "shaft_profile": ("端面倒角是**向外**的：总长 = Σl + 2×ec（不是从轴里切掉）",),
    "check_mass": ("material 未指定时密度按 1000 kg/m³ 算 ⇒ 质量≈体积。"
                   "要跟图纸质量对账必须先统一材料口径（见 reducer.params open_questions Q4）"),
    "mirror_body": ("镜像实体在**多实体**零件里才明显；单实体要配合 merge 才成形",),
    "lookup_standard": ("唯一权威来源是 assets/gb_standard.json",
                        "**禁止联网搜、禁止在代码里另抄一份表**（sw_gear 曾自留 HEX_BOLT_TABLE 与 JSON 打架）",
                        "改 JSON 后必须跑 sw_gb.py --write-md 重生速查表，否则自检失败"),
}
for _n, _ls in EXTRA_LIMITS.items():
    if _n in OPS and not OPS[_n].limits:
        OPS[_n].limits = tuple(_ls)
del _n, _ls


def schema() -> dict:
    """★ 机读 op 导出 —— 让 Agent 不读 Markdown 也能列 op / 查边界。

    与 `catalog()` 的分工：`catalog()` 给人读（Markdown 手册）；
    `schema()` 给程序读（JSON，含 `verified` 与 `limits` 两个**结构化**字段）。
    以前 `verified` 只是个布尔、**没有「已知边界」字段** ——
    `hole_wizard` 建不出盲孔这件事只写在正文里，Agent 读不到或读了不算数。

    漂移守卫：`sw_selftest.py` 会断言 `assets/op_schema.json` 与这里的输出一致
    （**改了 op 忘了重新导出会当场失败**，和 GB 速查表同一套机制）。
    """
    ops = []
    for o in sorted(OPS.values(), key=lambda x: (x.cat, x.name)):
        params = []
        for k, p in o.params.items():
            unit = p.get("unit")
            params.append({
                "name": k,
                "type": p.get("type", "any"),
                "unit": unit,
                "required": bool(p.get("required", False)),
                "default": p.get("default"),
                "desc": p.get("desc", ""),
            })
        ops.append({
            "name": o.name, "cn": o.cn, "cat": o.cat,
            "doc": o.doc,
            "signature": o.signature(),
            "params": params,
            "requires": [list(x) if isinstance(x, (tuple, list)) else [x]
                         for x in o.requires],   # ★ 每项都是**列表**：长度 1 = AND；
                                                 #   长度 >1 = **OR 组**（至少满足一个）
            "effect": list(o.effect),
            "one_of": [list(g) for g in o.one_of],
            "api": list(o.api),
            "verified": bool(o.verified),
            "limits": list(o.limits),
            # ★★ 封存字段进机读导出 —— Agent 读 `op_schema.json` 时**看不到**封存，
            #   就会照旧把 dim_auto 写进计划（然后被 run_op 拦下、白跑一轮）。
            "sealed": bool(o.sealed),
            "sealed_reason": o.sealed_reason if o.sealed else "",
        })
    return {
        "schema": "yoyo.opschema/1",
        "generated_by": "scripts/sw_ops.py --schema",
        "note": "本文件由 schema() 自动生成，请勿手工修改。改 op 请改 sw_ops.py 后重跑导出。",
        "n_ops": len(ops),
        "n_verified": sum(1 for o in ops if o["verified"]),
        "n_with_limits": sum(1 for o in ops if o["limits"]),
        "n_sealed": sum(1 for o in ops if o["sealed"]),
        "categories": sorted({o["cat"] for o in ops}),
        "states": STATES,
        "ops": ops,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="SolidWorks 操作目录")
    ap.add_argument("--cat", default=None,
                    help="只看某一类：doc/ref/sketch/feature/param/std/verify")
    ap.add_argument("--write-md", default=None, help="把完整目录写到指定 md 文件")
    ap.add_argument("--schema", default=None, nargs="?", const="",
                    help="★ 导出机读 schema；给路径就写文件，不给就打印")
    ap.add_argument("--limits", default=None, help="只看某个 op 的已知边界")
    a = ap.parse_args()
    if a.limits:
        o = OPS.get(a.limits)
        if o is None:
            print(f"没有 op {a.limits!r}", file=sys.stderr)
            raise SystemExit(2)
        print(f"{o.name}（{o.cn}）verified={o.verified}")
        for i, t in enumerate(o.limits or ("（未记录已知边界）",), 1):
            print(f"  {i}. {t}")
    elif a.schema is not None:
        text = json.dumps(schema(), ensure_ascii=False, indent=2)
        if a.schema:
            import pathlib
            dst = pathlib.Path(a.schema)
            # ★★ 防"占位"（2026-10-10 真机踩过）：`_build/api.json` 这个名字是
            #   **接口签名库**的保留位（`sw_docbuild.py` 的产物）。op 机读导出
            #   绝不能写到这里 —— 写过一次就把签名库覆盖了，之后
            #   `sw_apisig.py --sig/--find/--gaps` 全废，且报错指不到根因。
            #   op schema 的**正规落点**是 `assets/op_schema.json`。
            if dst.name.lower() == "api.json":
                print(
                    f"★ 拒绝写入 {dst}：`api.json` 是**接口签名库**的保留名"
                    f"（sw_docbuild.py 的产物）。\n"
                    f"  op 机读导出的正规落点是 `assets/op_schema.json`；"
                    f"要重建签名库请跑 `python scripts/sw_apisig.py --build`。",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            dst.write_text(text + "\n", encoding="utf-8")
            sc = schema()
            print(f"已写出 {a.schema}（{sc['n_ops']} op / verified {sc['n_verified']} / "
                  f"有边界的 {sc['n_with_limits']}）")
        else:
            print(text)
    elif a.write_md:
        import pathlib
        pathlib.Path(a.write_md).write_text(catalog() + "\n", encoding="utf-8")
        print(f"已写出 {a.write_md}")
    elif a.cat:
        for o in sorted([x for x in OPS.values() if x.cat == a.cat], key=lambda x: x.name):
            print(f"{o.name:24} {o.cn:16} {o.signature()}")
    else:
        print(catalog())
        print(f"\n共 {len(OPS)} 个操作，"
              f"已实测 {sum(1 for o in OPS.values() if o.verified)} 个")
