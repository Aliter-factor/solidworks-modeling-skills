#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_connect.py —— 与 SolidWorks 建立 COM 连接，并包一层「不容易踩坑」的会话对象。

这一层只做四件事，不做任何建模业务：
    1. 连上 SolidWorks（已开就复用，没开就启动）
    2. 解析常量（swEndConditions_e 之类）—— 优先读本机 sldworks.tlb，保证版本正确
    3. 管理文档（新建/打开/保存/关闭）+ 抑制所有会阻塞自动化的弹窗
    4. 单位换算 + 选择 + 重建 + 质量属性这些「每次建模都要用」的动作

几条硬性经验（写死在代码里，别改）：
    · **锁**：SolidWorks API 的值一律用**米**、角度用**弧度**。界面显示 mm 是障眼法。
      所以本模块提供 mm()/deg() 两个换算函数，业务代码里禁止出现裸数字长度。
    · **弹窗是自动化的头号杀手**：只要有一个模态框弹出来，脚本就永久卡死。
      进 Session 时一律关掉「修改尺寸时弹输入框」等开关（见 suppress_prompts）。
    · **选择即传参**：SolidWorks 的特征 API 不吃参数对象，吃「当前选中的东西 + 标记位」。
      所以每个特征动作之前必须先 select，且 Mark 号必须对（拉伸草图 mark=0、阵列方向 mark=1 …）。
    · **COM 调用很慢**：批量画草图前要 SetAddToDB(True)，画完再 (False)，
      官方文档明确说这能避开推断/自动约束并显著提速。sw_ops 里已经这么用了。
    · **方法还是属性？晚期绑定下靠猜必错**：SolidWorks 拒绝 pywin32 生成 makepy
      （报 "can not automate the makepy process"），所以只能晚期绑定。此时 pywin32
      拿不到类型信息，第一次访问某成员会**先按「取属性」试一次**：
        - 成功 → 该成员在本进程内**永远**是属性，写 `GetTitle()` 就炸
          `TypeError: 'str' object is not callable`（错误信息完全指不到根因）；
        - 失败 → pywin32 记成方法，`NewDocument(...)` 这种带参调用才正常。
      实测 `GetTitle` / `GetPathName` / `RevisionNumber` 都是**属性**。
      所以零参成员一律走 cx()，不要直接加括号。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sw_env  # noqa: E402
import sw_naming  # noqa: E402  （纯函数模块，不反向依赖本文件，不会循环）

MM = 0.001          # 1 mm = 0.001 m
DEG = 3.141592653589793 / 180.0

_ASSETS = Path(__file__).resolve().parent.parent / "assets"
_CACHE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "YoYo" / "swcache"

# 连不上/调用失败时抛出，带「人话 + 原始错误」双份信息
class SwError(RuntimeError):
    def __init__(self, msg: str, detail: Any = None):
        super().__init__(msg + (f"\n  原始错误: {detail!r}" if detail is not None else ""))
        self.detail = detail


# swFeatureError_e —— 特征「重建失败」的原因码。非 0 就是错的。
# ★ 这是识别「特征创建成功、形状却是错的」的**唯一**信号：
#   特征照样进模型树、op 的返回值也非 None，实物却没被切透/没被拉出来。
#   典型：键槽只开一半、切除只啃掉一层皮。
FEATURE_ERROR_NAMES: dict[int, str] = {
    0: "OK", 1: "未知错误",
    30: "拉伸结果不相交", 31: "找不到终止面", 32: "几何条件不合法",
    33: "切除轮廓既开放又闭合", 34: "切除轮廓非法（自相交/重叠）",
    35: "开放切除轮廓非法",
    36: "凸台轮廓既开放又闭合", 37: "凸台轮廓非法",
    49: "特征已废弃", 50: "特征已过时", 51: "草图外部参考失效",
}


def mm(v: float) -> float:
    """毫米 → SolidWorks 内部单位（米）。"""
    return float(v) * MM


def deg(v: float) -> float:
    """度 → 弧度。"""
    return float(v) * DEG


def variant_r8(values) -> Any:
    """把 Python 浮点序列包成 VT_ARRAY|VT_R8 的 VARIANT。

    ★ 这是让 CreateSpline3 / CreateSpline2 **真正建出图元**的唯一方式。

    踩坑记录（真机 probe19/probe20）：CreateSpline3 的 PointData 在类型库里是
    "Optional Null"（=VARIANT），直接传 Python list/tuple 时 pywin32 编组出来的
    数组元素类型不对，SolidWorks 的表现是 —— **不报错、不抛异常、返回非 None，
    但草图里图元数恒为 0**。也就是「样条静默消失」，报错完全指不到根因。
    显式声明成双精度数组后就正常了。

    同理，点数也有上限：整圈 1100 点的样条会被丢弃（草图直接消失），
    拆到 660 点以内没问题。见 references/30-pitfalls.md。
    """
    import pythoncom
    from win32com.client import VARIANT
    return VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, [float(v) for v in values])


# --------------------------------------------------------------- 成员访问

def _oleobj_of(obj: Any):
    """拿到 CDispatch 底下的原始 IDispatch（没有就返回 None）。"""
    return getattr(obj, "_oleobj_", None)


def _invoke_as_method(obj: Any, name: str, args: tuple):
    """绕过 pywin32 的属性/方法判定，强制按 INVOKE_FUNC 调用。

    仅在「该成员已被 pywin32 解析成属性，但我们确实要传参调用」时用。
    """
    ole = _oleobj_of(obj)
    if ole is None:
        raise SwError(f"{name}: 拿不到底层 IDispatch，无法按方法调用")
    dispid = ole.GetIDsOfNames(0, name)
    return ole.Invoke(dispid, 0, 1, 1, *args)   # 1 = INVOKE_FUNC, bResultWanted


def _is_com_object(v: Any) -> bool:
    """是不是一个 COM 对象（而不是字符串/数字/方法）。

    这里必须单独判，不能靠 callable()：pywin32 把任何返回的 IDispatch 都包成
    CDispatch，而 **CDispatch 自身是可调用的**（调它等于调默认成员）。
    所以 `callable(obj.SketchManager)` 恒为 True —— 早期版本据此把它当成方法
    去「调用」，结果撞上 ISketchManager 没有默认成员，报「找不到成员」。
    """
    return hasattr(v, "_oleobj_") or hasattr(v, "_oleobj")


def win_path(p: Any) -> str:
    """把路径规范成 **SolidWorks 内部一致的反斜杠形式**（`D:\\out\\x.SLDPRT`）。

    ★★ 为什么必须有这个函数（2026-10-02 真机确诊，A 级坑）：

        **同一条 COM 通道上，各 API 对正斜杠的容忍度不一样。**

        · `ISldWorks.OpenDoc6` / `IModelDocExtension.SaveAs` —— **宽容**，
          喂 `D:/out/x.SLDPRT` 照样工作（内部自己规范化）。
        · `IAssemblyDoc.AddComponent5`  —— **不宽容**：
          喂正斜杠 **静默返回 None**（组件没进装配体、**不抛异常、无错误码**）；
          喂反斜杠才成功。同一现象在 `ISldWorks.GetOpenDocumentByName` 上也复现
          （正斜杠回 None / 反斜杠回文档）。
        · `IComponent2.GetPathName` 回的是**反斜杠**形式 ⇒ 拿它当输入再去比
          `os.path.normcase(os.path.abspath(...))` 时，正反斜杠混用又会出问题。

        本项目此前的习惯是"Windows 路径一律用正斜杠，Bash 会自动转"（见用户级
        约定）—— 那条只对**文件系统与 shell**成立，**对 COM 不成立**。
        ⇒ 凡是**送给 SolidWorks 当字符串**的路径，一律先过本函数。

    ⚠ 它只做分隔符规范化，**不做大小写/绝对化**（那些交给调用方，因为
      "相对路径"在这里有语义，比如 SolidWorks 的"当前目录"未必是 cwd）。
    """
    s = os.fspath(p) if not isinstance(p, str) else p
    return s.replace("/", "\\")


def member(obj: Any, name: str, *args):
    """取成员（必要时调用），把「属性 vs 方法」的歧义吃掉。

    用法：
        member(doc, "GetTitle")                 → 'Part1'
        member(doc, "GetType")                  → 1
        member(doc, "Extension")                → 扩展对象（COM 对象，原样返回）
        member(doc, "ForceRebuild3", False)     → 带参调用（就是方法）

    为什么需要它：见文件头的「方法还是属性」那条。同一个名字在不同机器/不同
    首次访问顺序下可能被 pywin32 解析成不同种类，写死括号或写死不写括号都会翻车。

    名字叫 member 而不是更短的缩写，是因为 sw_ops 里已经有 cx/cy（圆心坐标）
    这类局部变量，短的会撞名。
    """
    try:
        v = getattr(obj, name)
    except AttributeError as e:
        raise SwError(f"对象上没有成员 {name!r}", e)

    if _is_com_object(v):
        # 属性返回的 COM 对象。要传参说明调用方写错了（没人给属性传参）。
        if args:
            return _invoke_as_method(obj, name, args)
        return v
    if callable(v):
        return v(*args)
    if args:
        raise SwError(f"{name} 是个属性（值 {v!r}），不接受参数")
    return v


# --------------------------------------------------------------- 类型绑定
#
# 上面的 member() 只能「兜住」属性/方法的歧义，但兜不住参数类型：
# 例如 SelectByID2 的第 8 个参数 Callout 是接口指针，不喂类型信息时
# SolidWorks 会回 DISP_E_TYPEMISMATCH。根治办法是把 sldworks.tlb 里的
# ITypeInfo 交给 pywin32（dynamic.Dispatch(typeinfo=...) 等价物），
# 让它按真实签名建 mapFuncs/propMap。

_TLB = None                 # 已加载的 PyITypeLib
_TLB_NAME2IDX: dict[str, int] = {}
_TLB_PATH: str | None = None
_OLEREPR: dict[str, Any] = {}   # iface -> DispatchItem（构建一次很贵，缓存共用）


def find_tlb() -> str | None:
    """定位本机 sldworks.tlb。找不到也不致命（会退化成无类型信息模式）。"""
    global _TLB_PATH
    if _TLB_PATH is not None:
        return _TLB_PATH or None
    cands: list[str] = []
    with contextlib.suppress(Exception):
        root = sw_env.find_install()
        if root:
            cands.append(os.path.join(root, "sldworks.tlb"))
    cands.append(os.environ.get("SW_TLB", ""))
    import glob
    for pat in (r"C:\SW*\SOLIDWORKS\sldworks.tlb",
                r"D:\SW*\SOLIDWORKS\sldworks.tlb",
                r"C:\Program Files\SOLIDWORKS*\sldworks.tlb"):
        cands.extend(sorted(glob.glob(pat)))
    for c in cands:
        if c and os.path.isfile(c):
            _TLB_PATH = c
            return c
    _TLB_PATH = ""
    return None


def _load_tlb():
    """加载类型库并建 接口名 -> 索引 映射（只做一次）。"""
    global _TLB, _TLB_NAME2IDX
    if _TLB is not None:
        return _TLB
    import pythoncom
    path = find_tlb()
    if not path:
        _TLB = False
        return None
    try:
        tl = pythoncom.LoadTypeLib(path)
    except Exception:                                           # noqa: BLE001
        _TLB = False
        return None
    idx: dict[str, int] = {}
    for i in range(tl.GetTypeInfoCount()):
        try:
            ti = tl.GetTypeInfo(i)
            nm = ti.GetDocumentation(-1)[0]
        except Exception:                                       # noqa: BLE001
            continue
        if nm and nm not in idx:
            idx[nm] = i
    _TLB, _TLB_NAME2IDX = tl, idx
    return tl


def has_interface(iface: str) -> bool:
    """类型库里有没有这个接口（没有就别 bind，白费功夫）。"""
    _load_tlb()
    return iface in _TLB_NAME2IDX


def _olerepr_for(iface: str):
    """按接口名取（并缓存）DispatchItem —— 方法/属性/参数类型的权威依据。"""
    if iface in _OLEREPR:
        return _OLEREPR[iface]
    tl = _load_tlb()
    if not tl:
        return None
    i = _TLB_NAME2IDX.get(iface)
    if i is None:
        return None
    import win32com.client.build as combuild
    try:
        ti = tl.GetTypeInfo(i)
        rep = combuild.DispatchItem(ti, ti.GetTypeAttr(), None, 0)
    except Exception:                                           # noqa: BLE001
        return None
    _OLEREPR[iface] = rep
    return rep


def typed(obj: Any, iface: str):
    """把 COM 对象重包成「带类型信息」的 dispatch；做不到就原样返回。

    这是整个接口层的关键：见文件头「方法还是属性」那条。
    """
    if obj is None:
        return None
    # ★★ 非 COM 对象（离线自检里的假对象、或已经绑好类型的包装）**原样返回**。
    #   为什么必须这条：本机装了 SolidWorks ⇒ `_olerepr_for()` 有货 ⇒ 原来会把
    #   **普通 Python 对象**也再包一层 CDispatch，于是 `f.GetFirstSubFeature()`
    #   会去调 COM 的 `InvokeTypes`，报
    #   `'_FxFeat' object has no attribute 'InvokeTypes'`。
    #   这条守卫让「离线测试用假对象」与「真机用 COM」能共存同一份代码。
    if not _is_com_object(obj):
        return obj
    rep = _olerepr_for(iface)
    if rep is None:
        return obj
    ole = getattr(obj, "_oleobj_", None)
    src = ole if ole is not None else obj
    try:
        from win32com.client.dynamic import CDispatch
        return CDispatch(src, rep, iface)
    except Exception:                                           # noqa: BLE001
        return obj


# --------------------------------------------------------------- 常量解析


def load_constants_from_tlb(sw_root: Path) -> dict[str, int]:
    """直接读 SolidWorks 的类型库拿常量值。

    为什么不用 win32com.client.constants：那个依赖 makepy 的全局缓存，
    多版本共存时会串味，而且失败时报错完全指不到根因。直接 LoadTypeLib 最稳。
    """
    import pythoncom  # pywin32

    tlb_path = None
    for name in ("swconst.tlb", "sldworks.tlb", "swconst.TLB"):
        p = sw_root / name
        if p.exists():
            tlb_path = p
            break
    if tlb_path is None:
        raise SwError(f"{sw_root} 下找不到 swconst.tlb / sldworks.tlb")
    tlb = pythoncom.LoadTypeLib(str(tlb_path))
    out: dict[str, int] = {}
    for i in range(tlb.GetTypeInfoCount()):
        try:
            ti = tlb.GetTypeInfo(i)
            ta = ti.GetTypeAttr()
        except pythoncom.com_error:
            continue
        for j in range(getattr(ta, "cVars", 0)):
            try:
                vd = ti.GetVarDesc(j)
            except pythoncom.com_error:
                continue
            try:
                name = ti.GetDocumentation(vd.memid)[0]
            except Exception:                                   # noqa: BLE001
                continue
            if not name:
                continue
            val = vd.value
            if isinstance(val, tuple):     # VARIANT 元组，取第一个
                val = val[0]
            if isinstance(val, int):
                out[name] = val
    return out


class Consts:
    """常量表。优先 tlb（准），退回内置 JSON（全但可能缺值），最后返回 None 并告警。

    注意：**不允许**在这里硬编码数值。缺值时要报出来，让调用方显式 r'...' 或用别的方式，
    因为 SolidWorks 各版本之间个别常量是会动的。
    """

    def __init__(self, sw_root: Path | None, strict: bool = False):
        self.source = "none"
        self.table: dict[str, int] = {}
        self.missing: set[str] = set()
        if sw_root is not None:
            try:
                self.table = load_constants_from_tlb(sw_root)
                self.source = "tlb"
            except Exception as e:                              # noqa: BLE001
                print(f"[warn] 从类型库读常量失败，退回内置表: {e}", file=sys.stderr)
        if not self.table:
            jf = _ASSETS / "sw_enums.json"
            if jf.exists():
                data = json.loads(jf.read_text(encoding="utf-8"))
                flat: dict[str, int] = {}
                for _enum, body in data.items():
                    for mname, m in body.get("members", {}).items():
                        if m.get("value") is not None:
                            flat[mname] = m["value"]
                self.table = flat
                self.source = "assets"
        if strict and not self.table:
            raise SwError("常量表为空：类型库读不到、assets/sw_enums.json 也不在")

    def __call__(self, name: str) -> int:
        if name in self.table:
            return self.table[name]
        if name in self.missing:
            raise SwError(f"常量 {name} 在 {self.source} 表里没有值")
        self.missing.add(name)
        raise SwError(
            f"常量 {name} 没有数值（来源={self.source}）。"
            f"用 sw_docbuild.py 重建 assets/sw_enums.json，"
            f"或在 SolidWorks 里查该枚举后显式传整数"
        )

    def get(self, name: str, default: int | None = None) -> int | None:
        return self.table.get(name, default)

    def enum(self, enum_name: str) -> dict[str, int | None]:
        """查一个枚举的全部成员（从内置 JSON，带中文检索用）。"""
        jf = _ASSETS / "sw_enums.json"
        if not jf.exists():
            return {}
        data = json.loads(jf.read_text(encoding="utf-8"))
        body = data.get(enum_name)
        if not body:
            return {}
        return {k: v.get("value") for k, v in body["members"].items()}


# --------------------------------------------------------------- 会话


class SwSession:
    """一次自动化作业的上下文。用法：

        with SwSession.open(visible=True) as sw:
            sw.new_part()
            ...
            sw.save_as(r"D:\\out\\gear.SLDPRT")
    """

    def __init__(self, app, sw_root: Path | None, late_bound: bool):
        self.app = app
        self.root = sw_root
        self.late_bound = late_bound
        self.C = Consts(sw_root)
        self._cmd_depth = 0
        self._prompt_backup: list[tuple[Any, Any]] = []
        # 最近一次「取实体」用的参数与结果（诊断用；见 bodies()）。
        self.last_body_query: dict[str, Any] = {}
        # 最近一次 save_as 的数实体结果 + 当时是不是装配体（见 save_as()）。
        # ★ 装配体没有"实体"，bodies() 恒为 0 —— 这两个字段必须成对看，
        #   否则会把"装配体"误报成"空文档"（2026-10-03 真机假警告）。
        self.last_save_bodies: int | None = None
        self.last_save_is_asm: bool = False

    # ---- 生命周期 ----

    @classmethod
    def open(cls, visible: bool = True, late_bound: bool = False,
             start_if_missing: bool = True) -> "SwSession":
        root = sw_env.find_install()
        if not sw_env.pywin32_available():
            raise SwError(
                "没装 pywin32，无法走 COM。\n"
                f"  当前 Python: {sys.executable}\n"
                f"  安装：\"{sys.executable}\" -m pip install pywin32 "
                "-i https://pypi.tuna.tsinghua.edu.cn/simple"
            )
        bits = sw_env.sw_bits(root)
        if bits and bits != sw_env.python_bits():
            raise SwError(
                f"位数不匹配：SolidWorks {bits} 位 / Python {sw_env.python_bits()} 位。"
                "必须换同位数的 Python，否则 Dispatch 直接失败。"
            )
        import pythoncom
        import win32com.client as wc
        pythoncom.CoInitialize()        # 非主线程里调 COM 必须
        app = None
        early = False
        if not late_bound:
            try:
                app = wc.gencache.EnsureDispatch("SldWorks.Application")
                early = True
            except Exception as e:                              # noqa: BLE001
                print(f"[warn] 早期绑定失败，改晚期绑定: {e}", file=sys.stderr)
        if app is None:
            try:
                app = wc.Dispatch("SldWorks.Application")
            except Exception as e:                              # noqa: BLE001
                raise SwError("连不上 SolidWorks（Dispatch(\"SldWorks.Application\") 失败）", e)
        # 没有类型信息就补上。SolidWorks 从不实现 IProvideClassInfo，
        # 所以 EnsureDispatch 必然失败、这一支基本必走（实测 SW2024）。
        if not getattr(getattr(app, "_olerepr_", None), "mapFuncs", None):
            app = typed(app, "ISldWorks")
            early = False
        sess = cls(app, root, late_bound=not early)
        with contextlib.suppress(Exception):
            app.Visible = bool(visible)
        with contextlib.suppress(Exception):
            app.UserControl = True      # 允许用户同时手动操作（不设的话偶发吞事件）
        return sess

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.app.CommandInProgress = False

    def __enter__(self) -> "SwSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 属性 ----

    @property
    def version(self) -> str:
        with contextlib.suppress(Exception):
            return f"{member(self.app, 'RevisionNumber')}"     # 例如 32.5.0 → SW2024 是 32
        return "unknown"

    # ---- 界面语言（需求 2：命名统一的前提）----

    def current_language(self) -> str:
        """SolidWorks 界面语言的**原始串**，如 `'chinese-simplified'`。

        ★ 真机实测（2026-09-25，M3）：`ISldWorks.GetCurrentLanguage()` 回的是
          **字符串**，不是枚举整数。按某些文档/示例猜成 `swLanguage_e` 的整数
          （比如断言 == 1）会永远不成立，而且不报错 —— 又是一个静默坑。
        """
        with contextlib.suppress(Exception):
            return str(member(self.app, "GetCurrentLanguage"))
        return "unknown"

    def language_code(self) -> str:
        """规范化的语言码：'cn' / 'en' / 其它原样（见 sw_naming.raw_to_lang）。"""
        return sw_naming.raw_to_lang(self.current_language())

    @property
    def model(self):
        """当前活动文档。**已绑定类型信息**（方法/属性/参数类型都按真实签名）。"""
        m = self.app.ActiveDoc
        if m is None:
            raise SwError("当前没有活动文档。先 new_part() / open_doc()。")
        return typed(m, "IModelDoc2")

    # ---- 子对象访问器 ----
    # 每一次拿子对象都必须过 typed()：子对象是 pywin32 新包的 dispatch，
    # 类型信息会丢，不过一遍就会退回「靠猜」的状态（见文件头）。

    @property
    def ext(self):
        """IModelDocExtension：选择、另存、自定义属性、文档级首选项都在这。"""
        return typed(member(self.model, "Extension"), "IModelDocExtension")

    def skm(self):
        """ISketchManager：所有草图图元的入口。"""
        return typed(member(self.model, "SketchManager"), "ISketchManager")

    def fm(self):
        """IFeatureManager：所有特征的入口。"""
        return typed(member(self.model, "FeatureManager"), "IFeatureManager")

    def selmgr(self):
        """ISelectionMgr：查询当前选择。"""
        return typed(member(self.model, "SelectionManager"), "ISelectionMgr")

    def view(self):
        """当前视图（重绘/缩放）。注意不是 IModelDoc2 上的成员。"""
        return typed(member(self.model, "ActiveView"), "IModelView")

    def feat(self, name: str):
        """按名字取特征，Python 侧拿到的是已绑定 IFeature 的对象。"""
        return typed(self.model.FeatureByName(name), "IFeature")

    @property
    def is_part(self) -> bool:
        with contextlib.suppress(Exception):
            return member(self.model, "GetType") == self.C("swDocPART")
        return False

    @property
    def is_assembly(self) -> bool:
        with contextlib.suppress(Exception):
            return member(self.model, "GetType") == self.C("swDocASSEMBLY")
        return False

    @property
    def asm(self):
        """当前文档按 **IAssemblyDoc** 用（装配体专有 API 全在它上面）。

        ★ 为什么要有类型守卫：`IAssemblyDoc` 的方法在零件文档上**不一定报错**，
        有的会静默返回空（例如 `GetComponents` 回 None）—— 那会让"装配体里没有组件"
        和"我在改一个零件"看起来完全一样。宁可在这里炸得响一点。
        """
        m = self.model
        if not self.is_assembly:
            doc_type = None
            with contextlib.suppress(Exception):
                doc_type = member(m, "GetType")
            raise SwError(
                f"当前文档不是装配体（标题 {member(m, 'GetTitle')}，GetType={doc_type}）。"
                f"先 new_assembly() 或 open_doc('*.SLDASM')。")
        return typed(m, "IAssemblyDoc")

    # ---- 弹窗抑制 ----

    @contextlib.contextmanager
    def suppress_prompts(self):
        """关掉所有会阻塞脚本的对话框，退出时尽量还原。

        必关的是 swInputDimValOnCreate —— 不然每次画带尺寸的图元都弹输入框，脚本卡死。
        """
        toggles = [
            ("swInputDimValOnCreate", False),      # 画图元时不弹「修改尺寸」框
            ("swAutoShowPropertyManager", False),  # 不自动弹 PropertyManager
            ("swAutoSizePropertyManager", False),
        ]
        saved: list[tuple[Any, Any]] = []
        for name, want in toggles:
            cid = self.C.get(name)
            if cid is None:
                continue
            try:
                old = self.app.GetUserPreferenceToggle(cid)
                saved.append((cid, old))
                self.app.SetUserPreferenceToggle(cid, want)
            except Exception:                                   # noqa: BLE001
                pass
        try:
            yield self
        finally:
            for cid, old in saved:
                with contextlib.suppress(Exception):
                    self.app.SetUserPreferenceToggle(cid, old)

    @contextlib.contextmanager
    def temp_pref(self, *pairs):
        """临时改一批**用户偏好开关**，退出时按原值还原。

        为什么单拎出来（2026-10-02 真机确诊）：
            `IComponent2::Name2` 的官方 Remarks 明确写：
            「Before executing a name change, this property checks the
              **swExtRefUpdateCompNames** setting. **If it is true, then the name
              change fails**; if false, then the name change succeeds.」
            实测本机该开关**默认 True**（`GetUserPreferenceToggle(18)` = True）
            ⇒ 组件改名**静默失败**：赋值不报错，读回还是旧名（`box-1`）。
            这正是"返回值会说谎"的又一种形态 —— 连异常都没有。

        用法：
            with sw.temp_pref(("swExtRefUpdateCompNames", False)):
                c.Name2 = "地基"

        ★ 必须还原：这是**机器级**用户设置，改了不还原会污染用户环境
          （下一次手工改名也会莫名其妙失败）。找不到常量名就跳过该条，不报错 ——
          还原是尽力而为，不能因为"某个常量在别的版本里不存在"就把主流程炸掉。
        """
        saved: list[tuple[Any, Any]] = []
        for name, want in pairs:
            cid = self.C.get(name)
            if cid is None:
                continue
            try:
                old = bool(self.app.GetUserPreferenceToggle(cid))
                if old != bool(want):
                    self.app.SetUserPreferenceToggle(cid, bool(want))
                saved.append((cid, old))
            except Exception:                                   # noqa: BLE001
                pass
        try:
            yield self
        finally:
            for cid, old in saved:
                with contextlib.suppress(Exception):
                    self.app.SetUserPreferenceToggle(cid, old)

    @contextlib.contextmanager
    def command_in_progress(self):
        """包住一段连续操作：期间 SW 不接受手工打断，也能少刷界面、更快。"""
        self._cmd_depth += 1
        with contextlib.suppress(Exception):
            self.app.CommandInProgress = True
        try:
            yield self
        finally:
            self._cmd_depth -= 1
            if self._cmd_depth <= 0:
                with contextlib.suppress(Exception):
                    self.app.CommandInProgress = False

    # ---- 文档 ----

    def new_part(self, template: str | None = None, units_mm: bool = True):
        """新建零件。模板不给就从系统默认里找（gb_part.prtdot）。"""
        if template is None:
            tpl = sw_env.find_templates(self.root).get("part")
            if not tpl:
                raise SwError(
                    "找不到零件模板。请在 SolidWorks 的 工具>选项>系统选项>默认模板 "
                    "里指定，或显式传 template=..."
                )
            template = tpl
        cid = self.C.get("swDocPART")
        model = self.app.NewDocument(template, 0, 0, 0)
        if model is None:
            raise SwError(f"NewDocument 返回空，模板可能无效: {template}")
        if units_mm:
            self.set_units_mm()
        return model

    def new_assembly(self, template: str | None = None, units_mm: bool = True):
        """新建装配体。

        模板不给就从系统默认里找（`gb_asm.asmdot` 之类）。★ 找不到时**不报错**，
        退回 `ISldWorks.NewAssembly()`（**零参数**，用 SolidWorks 当前默认装配体模板）——
        因为 `.asmdot` 不一定落在 `sw_env.TEMPLATE_DIR_HINTS` 覆盖的目录里，
        "找不到模板"就罢工会让整条装配体链路在别人的机器上直接不可用。
        （对比 `new_part`：零件模板 `gb_part.prtdot` 几乎必然存在，所以那边直接报错。）
        """
        if template:
            model = self.app.NewDocument(template, 0, 0, 0)
            src = template
        else:
            tpl = sw_env.find_templates(self.root).get("assembly")
            if tpl:
                model = self.app.NewDocument(tpl, 0, 0, 0)
                src = tpl
            else:
                model = member(self.app, "NewAssembly")
                src = "ISldWorks.NewAssembly()（未找到 .asmdot，用 SldWorks 默认模板）"
        if model is None:
            raise SwError(f"新建装配体失败（模板来源：{src}）。"
                          f"请在 工具>选项>默认模板 里指定装配体模板，或显式传 template=...")
        if units_mm:
            self.set_units_mm()
        return model

    def set_units_mm(self) -> None:
        """把文档单位设成 MMGS（毫米·克·秒）。注意：这只影响**显示与尺寸输入**，
        API 里的长度仍然一律按米算。"""
        mmgs = self.C.get("swMMGS") or 2
        for setter, key in (("SetUserPreferenceIntegerValue", "swUnitsLinear"),):
            cid = self.C.get(key)
            if cid is None:
                continue
            with contextlib.suppress(Exception):
                getattr(self.app, setter)(cid, mmgs)
        with contextlib.suppress(Exception):
            member(self.model, "SetUserPreferenceToggle",
                   self.C.get("swUseSystemUnits") or 0, False)

    def open_doc(self, path: str | os.PathLike, read_only: bool = False):
        p = str(Path(path))
        if not Path(p).exists():
            raise SwError(f"文件不存在: {p}")
        opts = int(self.C.get("swOpenDocOptions_Silent") or 1)
        if read_only:
            opts |= int(self.C.get("swOpenDocOptions_ReadOnly") or 2)
        errs = 0
        warns = 0
        # ★★ `OpenDoc6` 在 pywin32 下返回的是 **tuple** `(model, Errors, Warnings)`
        #   （末尾两个是 by-ref 出参），**不是** model 本身。
        #   原代码 `model = self.app.OpenDoc6(...)` 拿到 tuple 后，
        #   后面 `if model is None` 永远不成立 ⇒ **打开失败/未激活都不会报错**，
        #   而调用方基本都不用返回值 ⇒ 这个缺陷一直没被发现。
        res = self.app.OpenDoc6(p, self.C("swDocPART"), opts, "", errs, warns)
        model = res[0] if isinstance(res, tuple) else res
        if model is None:
            # 可能是装配体/工程图，换个类型再试
            res = self.app.OpenDoc6(p, 2, opts, "", errs, warns)
            model = res[0] if isinstance(res, tuple) else res
        if model is None:
            raise SwError(f"打开失败: {p}")
        # ★★ 打开 ≠ 激活：`swOpenDocOptions_Silent` 下文档**不会成为活动文档**，
        #   而本类所有读操作（`self.model` = `app.ActiveDoc`）都挂在活动文档上。
        #   实测 2026-10-02：连读 3 个标准件，`sw_inspect` 三次都报出第 1 个
        #   （26-A型圆形销）—— 差点据此把螺母判成"26 个锥面的怪件"，
        #   又差点把「六角头螺栓」判成光杆。**读错文件不是慢一点，是全错。**
        #   `sw_profile --docs a b c` 批量取证同样走这条路径（同样可疑，见坑表 §83）。
        title = str(member(model, "GetTitle") or "")
        if title:
            self.app.ActivateDoc3(title, False, 0)
            cur = str(member(self.app.ActiveDoc, "GetTitle") or "")
            if cur != title:
                raise SwError(
                    f"打开后没能激活文档：要 {title!r}，活动文档却是 {cur!r}。\n"
                    f"  不报错的危害：后续所有读操作都会读到**错的文件**，"
                    f"取证结论整个错掉（不是慢一点，是全错）。")
        return model

    def doc_identity(self) -> dict:
        """当前活动文档的**身份指纹**：路径 / 标题 / 特征数 / 实体数 / 体积(m³)。

        ★★ 2026-10-02 真机踩到（本轮最贵的一个坑）：读一份 **235 张面**的参照件
          （10-通气塞）时，**活动文档在中途被别人换掉了** —— 用户自己的
          SolidWorks 会话里冒出一份 `零件26`（特征是 底盘板/履带左/履带右），
          于是接下来的 `bounding_box()` 回 **146.08×72.30×16.00**（通气塞真值
          35×23×23）、`mass_props()` 回 0 —— 而 `GetTitle` 还写着 `16-螺塞`。
          16-螺塞 同样中招：Z 向从 15.0 变成 90.0，特征表里多出 `配重`/`发动机罩`。
          ⇒ 后果不是"慢一点"，是**读数整片来自另一份文件**且**完全不报错**。
          ⇒ 因此凡是"连读一串东西再下结论"的流程，都必须**读前读后各取一次指纹**，
            不一致就作废（见 `sw_profile._one` / `sw_refscan.scan_one`）。
        ★ 指纹里放 `feature_count` + `body` 数 + 体积：光比路径/标题**抓不到**
          螺塞那一型（路径和标题都对，内容却换了）。
        """
        out: dict = {}
        with contextlib.suppress(Exception):
            m = self.app.ActiveDoc
            out["title"] = str(member(m, "GetTitle") or "")
            out["path"] = os.path.normcase(os.path.abspath(str(member(m, "GetPathName") or "")))
        with contextlib.suppress(Exception):
            out["features"] = self.feature_count()
        with contextlib.suppress(Exception):
            out["bodies"] = len(self.bodies())
        with contextlib.suppress(Exception):
            v = self.mass_props().get("volume_m3")
            out["volume_m3"] = round(float(v), 12) if v is not None else None
        return out

    @staticmethod
    def identity_diff(a: dict, b: dict) -> list[str]:
        """两份指纹的差异（人话）。空列表 = 同一份文档的同一状态。"""
        keys = ("title", "path", "features", "bodies", "volume_m3")
        return [f"{k}: {a.get(k)!r} → {b.get(k)!r}" for k in keys
                if a.get(k) != b.get(k)]

    def ensure_doc(self, ident: dict) -> bool:
        """确认活动文档还是 `ident` 那一份；不是就按标题抢回来；抢不回回 False。

        ★ 抢回来只对"活动文档漂到别的已打开文档"有效；对"文档内容被换掉"
          （螺塞那一型）无能为力 —— 那种情况**必须**让调用方作废这次读数。
        """
        if not ident:
            return True
        if not self.identity_diff(ident, self.doc_identity()):
            return True
        title = ident.get("title")
        if not title:
            return False
        with contextlib.suppress(Exception):
            self.app.ActivateDoc3(title, False, 0)
        return not self.identity_diff(ident, self.doc_identity())

    def _close_if_open(self, path: str, keep_title: str = "") -> int:
        """把 SolidWorks 里「路径正好等于 path」的**其它**文档关掉（不保存）。

        ★ 为什么非要这一步：SaveAs 到「已被另一个已打开文档占用的路径」时，
          SolidWorks 会**返回成功但根本不落盘** —— 实测 `SaveAs` 回 True、
          文件 mtime 纹丝不动、而报告里写着 saved。这就是「脚本说通过，
          文件夹里却是上一次的旧零件」的根因（同一个路径连跑两次必踩）。
        ★★ 2026-10-03 修：`keep_title` 由调用方**显式传进来**。
          原来这里读 `self.model`（= 动态的 `ActiveDoc`）当"要保命的那一份"——
          而 `save_as` 正是**先钉住文档、再调本方法**，顺序一换就变成
          「用还没钉住的动态引用去决定关谁」。现在语义明确：**调用方owns 哪一份，
          本方法就不许关哪一份**。
        ★ 注意：关文档会让 SolidWorks **换掉活动文档** —— 所以调用方
          绝不能在这之后继续用 `self.model`（见 `save_as` 的注释）。
        """
        want = os.path.normcase(os.path.abspath(path))
        closed = 0
        if not keep_title:
            with contextlib.suppress(Exception):
                keep_title = str(member(self.model, "GetTitle"))
        with contextlib.suppress(Exception):
            for d in (member(self.app, "GetDocuments") or ()):
                try:
                    cur = os.path.normcase(os.path.abspath(str(member(d, "GetPathName"))))
                    title = str(member(d, "GetTitle"))
                except Exception:                              # noqa: BLE001
                    continue
                if cur == want and title != keep_title:
                    self.app.CloseDoc(title)
                    closed += 1
        return closed

    def save_as(self, path: str | os.PathLike, overwrite: bool = True) -> str:
        p = str(Path(path))
        if not overwrite and Path(p).exists():
            raise SwError(f"目标已存在（overwrite=False）: {p}")
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        # ★★★ 2026-10-03 修（真机）：**先把文档钉住**，后面一律用这个引用。
        #   原因：`self.model` 是**动态属性**（每次重新读 `ActiveDoc`），而
        #   `_close_if_open` 会**关掉另一份文档** —— 关文档会让 SolidWorks 把
        #   活动文档**换成别的**！于是后面的 `SaveAs` 和"落盘校验"都在
        #   **操作/验证另一份文档**，报出来的是「SaveAs 返回成功，但文档仍指向
        #   (未保存)」—— 而真正的问题是"存错人了"。
        #   ⇒ 这就是 §84 的另一个面：**判据在验证别人**。
        #   （`_close_if_open` 当初是为了修"同名文件被占 ⇒ 静默不落盘"，
        #     结果引入了这一条 —— 真机才会暴露，静态看两处都对。）
        doc = self.model
        doc_title = ""
        with contextlib.suppress(Exception):
            doc_title = str(member(doc, "GetTitle"))
        self._close_if_open(p, keep_title=doc_title)
        t0 = time.time()
        opts = int(self.C.get("swSaveAsOptions_Silent") or 1)
        ext = typed(member(doc, "Extension"), "IModelDocExtension")
        if ext is None:
            raise SwError(f"取不到 {doc_title or '(当前文档)'} 的 IModelDocExtension")
        ok = ext.SaveAs(p, 0, opts, None, 0, 0)
        if not ok:
            raise SwError(f"保存失败: {p}")
        # ★ 别信 SaveAs 的返回值（DISP 的 [out] 参数在晚期绑定下也读不到 errs/warns）。
        #   必须落盘验证两件事：模型认领的路径对不对、文件是不是真的新写了。
        #   ★ 两处都读**钉住的那份文档**，不是 `self.model`。
        real = ""
        with contextlib.suppress(Exception):
            real = str(member(doc, "GetPathName"))
        if os.path.normcase(os.path.abspath(real)) != os.path.normcase(os.path.abspath(p)):
            raise SwError(
                f"SaveAs 返回成功，但文档仍指向 {real or '(未保存)'} —— 实际没落盘。\n"
                f"  目标  : {p}\n"
                f"  文档  : {doc_title or '(未知)'}\n"
                "  常见原因：① 该路径已被另一个已打开的同名文档占用"
                "（本方法会先关掉它）；② 存盘过程中活动文档被换掉"
                "（此时报的就是本条 —— 存错人了）。")
        if not Path(p).exists():
            raise SwError(f"SaveAs 返回成功但文件不存在: {p}")
        if os.path.getmtime(p) < t0 - 2.0:
            raise SwError(f"SaveAs 返回成功但文件没被更新（mtime 早于本次调用）: {p}")
        # ★ 路径对、文件新，**还不代表里面有东西**。实测遇到过"保存成功、
        #   打开却是空零件（0 个实体）"的情况 —— 单独看路径和 mtime 完全抓不到。
        #   这里顺手数一下实体，把结论留在返回里，让人第一时间看见。
        # ★★ 2026-10-03 修：装配体**没有"实体"这个概念**（`bodies()` 是零件 API，
        #   对 .SLDASM 恒为 0）⇒ 原来对装配体也报"一个实体都没有"是**假警告**，
        #   会让人以为保存失败。装配体的"有没有东西"要看**组件数**。
        is_asm = False
        with contextlib.suppress(Exception):
            is_asm = bool(member(doc, "GetType") == self.C("swDocASSEMBLY"))
        self.last_save_is_asm = is_asm
        n_body = 0
        with contextlib.suppress(Exception):
            # ★ 走唯一入口 bodies()（回退链 + 记录 last_body_query）。
            #   真机踩过：同一份文档这里数到 1 个实体，而别处用 (0, True) 数到 0。
            n_body = 0 if is_asm else len(self.bodies())
        self.last_save_bodies = n_body
        return p

    def close_doc(self, save: bool = False) -> None:
        title = None
        with contextlib.suppress(Exception):
            title = member(self.model, "GetTitle")
        if title:
            self.app.CloseDoc(title)

    # ---- 选择 ----

    # 常用选择类型名 → 枚举名（sw_ops 里 select 时可以直接写中文语义）
    # SelectByID2 的 Type 参数是**字符串字面量**（"PLANE"/"FACE"/…），
    # 不是 swSelDATUMPLANES 这类枚举值 —— 传枚举名会静默选不中。
    SEL = {
        "plane": "PLANE", "face": "FACE", "edge": "EDGE",
        "vertex": "VERTEX", "sketch": "SKETCH", "axis": "AXIS",
        "point": "DATUMPOINT", "origin": "EXTSKETCHPOINT",
        "solidbody": "SOLIDBODY", "component": "COMPONENT",
        "dimension": "DIMENSION", "refsurface": "REFSURFACE",
        "sketchseg": "EXTSKETCHSEG", "sketchpoint": "EXTSKETCHPOINT",
        "body": "SOLIDBODY", "annotation": "ANNOTATION",
    }

    def select(self, name: str, sel_type: str = "plane", x: float = 0.0,
               y: float = 0.0, z: float = 0.0, append: bool = False,
               mark: int = 0) -> bool:
        """按名字选对象。名字是 SolidWorks 内部名（'前视基准面' / 'Front Plane' /
        'Sketch1' / 面的话是坐标点）。中文界面的基准面名字是中文，两种都试。"""
        typ = self.SEL.get(sel_type, sel_type)
        ext = self.ext
        if not append:
            member(self.model, "ClearSelection2", True)
        ok = ext.SelectByID2(name, typ, mm(x), mm(y), mm(z), append, mark, None, 0)
        if not ok:
            for alias in _aliases(name):
                if ext.SelectByID2(alias, typ, mm(x), mm(y), mm(z), append, mark, None, 0):
                    return True
        return bool(ok)

    def select_plane(self, which: str, mark: int = 0) -> bool:
        return self.select(which, "plane", mark=mark)

    def plane_names(self) -> list[str]:
        """列出本文档里默认基准面的**真名**。

        中文模板下叫「前视基准面/上视基准面/右视基准面」，英文模板下叫
        "Front Plane"/…。别硬编码，先问文档。
        """
        out: list[str] = []
        for t, nm in self.feature_names():
            if t == "RefPlane":
                out.append(nm)
        return out

    def feature_names(self) -> list[tuple[str, str]]:
        """列出 (类型名, 名字)。

        排查「这个零件里到底有什么」的第一手工具。两个必须知道的点：
          · 草图的特征类型名是 **ProfileFeature**，不是 "Sketch"；
            基准面是 **RefPlane**；原点是 OriginProfileFeature。
          · 每个 feature 都要过 typed(IFeature)：不然 `f.Name` 会踩上
            「属性被当成方法」的坑，拿到的不是名字。
        """
        out: list[tuple[str, str]] = []
        f = typed(member(self.model, "FirstFeature"), "IFeature")
        guard = 0
        while f is not None and guard < 1000:
            guard += 1
            t = ""
            with contextlib.suppress(Exception):
                t = member(f, "GetTypeName2")
            with contextlib.suppress(Exception):
                out.append((t, member(f, "Name")))
            f = typed(member(f, "GetNextFeature"), "IFeature")
        return out

    # ---- 装配体 ----
    #
    # ★★ 配合怎么数 —— 2026-10-02 真机**重新设计**：从「穷举名字」改成「按结构」。
    #
    #   旧做法：`MATE_FEATURE_TYPES` 白名单穷举所有配合类型名，`mate_count()` 数命中的。
    #   为什么必须改掉：白名单**必须穷举**，而 SolidWorks 的类型名**有意外**。
    #   真机实测（本章程本轮实测值）：
    #       kind=coincident     → `MateCoincident`
    #       kind=parallel       → `MateParallel`
    #       kind=perpendicular  → `MatePerpendicular`
    #       kind=distance       → **`MateDistanceDim`**  ← 带 `Dim` 后缀！
    #                             不是想当然的 `MateDistance`（照直觉写就漏数）
    #   穷举不全 ⇒ **静默漏数**（报告里少一个配合，看不出来）。
    #   而"静默错"正是本项目最不能接受的失败形态 —— 所以宁可换设计，不赌穷举。
    #
    #   新做法：**按结构数** = 「配合」文件夹（`MateGroup`）的**直接子节点**。
    #     · 不需要知道任何类型名 ⇒ 不会漏数（新类型自动被数到）；
    #     · 文件夹不是配合 ⇒ 排除文件夹类型（`MATE_GROUP_TYPES`）；
    #     · **嵌套**子文件夹也遍历（SolidWorks 允许在「配合」文件夹下再建文件夹）
    #       ⇒ 每个配合恰好归属一个文件夹，**不漏不重**；
    #     · `MATE_FEATURE_TYPES` 降级为**旁证**：已真机确认过的名字，
    #       命中就正常，命中不了就进 `unknown_mate_features()` **报出来** ——
    #       这是"数目对了"之外的第二道防线（碰到没见过的类型名值得看一眼）。
    MATE_FEATURE_TYPES: tuple[str, ...] = (
        "MateCoincident", "MateParallel", "MatePerpendicular", "MateDistanceDim",
    )
    #: 「配合」文件夹的类型名（不是配合本身）。
    MATE_GROUP_TYPES: tuple[str, ...] = ("MateGroup",)

    def feature_tree(self, max_nodes: int = 4000, max_depth: int = 40) -> list[dict]:
        """**含子特征**的完整特征树。

        ★ 为什么必须有它：`feature_names()` 只走 `FirstFeature → GetNextFeature`（顶层）。
        而装配体的**配合在「配合」文件夹的子特征里** ⇒ 只看顶层会看到 1 个 MateGroup
        就以为"一个配合都没有"。零件侧同理：某些特征（如阵列的实例）也不在顶层。

        返回 `[{"type","name","depth","suppressed"}, ...]`，深度优先、父在前。

        ★★ 2026-10-04 真机踩到（坑表 §115）：**必须同时限「深度」**。
          原实现只判 `len(out) < max_nodes`，但**递归发生在计数增长之前**：
          某些件（箱座 1-箱座.SLDPRT）的 `GetFirstSubFeature` 链**自指/成环**
          ⇒ 每层只 append 一个节点、却一直往下递归 ⇒ 在 `max_nodes` 远未用尽前
          **先撞 Python 递归上限**（`RecursionError: maximum recursion depth exceeded`，
          栈里 986 层 `walk`）。⇒ 加 `max_depth` 硬闸门；撞到就**如实报告**，
          不要静默截断成"完整树"。

        ★★★ 2026-10-04 同时修正**遍历语义**（这才是"成环"的真根因）：
          **子特征必须用 `GetFirstSubFeature` → `GetNextSubFeature` 遍历**，
          用 `GetNextFeature` 会**走回主链**（把后面的顶层特征当成"子特征"再收一次，
          再递归进去 ⇒ 同一特征在 depth 37/38/39 反复出现、树被吹成 4000 节点）。
          原来 `sw_asm_tree.py` 只跑装配体、顶层浅，没暴露；箱座一跑就现形。

        ★★★ 2026-10-04 还要**按名字断环**（上面那条只是必要条件，不是充分条件）：
          SolidWorks 里「草图 ↔ 消费它的特征」**互为子特征**
          （实测 箱座：`草图11 → 凸台-拉伸4 → 草图11 → …`，depth 37/38/39 无限循环）。
          `GetNextSubFeature` 挡不住它。⇒ 用一个 `seen` 名字集合**只收一次**：
          同一名字再遇到就跳过（既不重复 append，也不递归进去）。
          ⇒ 返回的树**每个特征只出现一次**（首次出现的深度），装配体的配合
          （名字唯一）不受影响。
        """
        out: list[dict] = []
        seen: set[str] = set()
        truncated = False

        def walk(f, depth: int) -> None:
            nonlocal truncated
            while f is not None and len(out) < max_nodes:
                if depth >= max_depth:
                    truncated = True
                    return
                t = nm = ""
                with contextlib.suppress(Exception):
                    t = member(f, "GetTypeName2")
                with contextlib.suppress(Exception):
                    nm = member(f, "Name")
                key = str(nm)
                if key and key in seen:
                    f = typed(member(f, "GetNextFeature"), "IFeature")
                    continue                      # ★ 断环：这个特征已经收过了
                if key:
                    seen.add(key)
                sup = None
                with contextlib.suppress(Exception):
                    sup = bool(member(f, "IsSuppressed"))
                out.append({"type": t, "name": nm, "depth": depth,
                            "suppressed": sup})
                # 子特征链：GetFirstSubFeature → GetNextSubFeature（**不是** GetNextFeature）
                # ★ 容错（2026-10-04 修）：**面特征**（COM 类名 `_FxFeat`）与部分
                #   退化对象**没有** `GetFirstSubFeature`/`GetNextSubFeature` 成员 ——
                #   逐特征调用会抛 `AttributeError`，让整棵树遍历中断。
                #   没有子特征是**正常**情况（不是错误），所以整段 suppress。
                with contextlib.suppress(Exception):
                    sub = typed(member(f, "GetFirstSubFeature"), "IFeature")
                    while sub is not None and len(out) < max_nodes:
                        walk(sub, depth + 1)
                        sub = typed(member(sub, "GetNextSubFeature"), "IFeature")
                with contextlib.suppress(Exception):
                    f = typed(member(f, "GetNextFeature"), "IFeature")

        walk(typed(member(self.model, "FirstFeature"), "IFeature"), 0)
        if truncated:
            with contextlib.suppress(Exception):
                import sys as _sys
                print(f"  [warn] feature_tree 撞到 max_depth={max_depth}"
                      f"（子特征链可能自指）—— 已截断，共 {len(out)} 节点",
                      file=_sys.stderr)
        return out

    def mate_features(self) -> list[dict]:
        """当前装配体里的**配合** = 每个「配合」文件夹（`MateGroup`）的**直接子节点**。

        ★ 按**结构**取，不是按类型名白名单取 —— 理由见本节头注（穷举会漏数）。
        ★ 遍历**每一个** `MateGroup`（含**嵌套**子文件夹 —— SolidWorks 允许在
          「配合」文件夹下再建文件夹），各取自己的**直接**子节点。
          每个配合恰好归属一个文件夹 ⇒ **不漏不重**。
          ★ 取样时**跳过**文件夹类型本身（`MATE_GROUP_TYPES`）—— 否则会多数一个。
        """
        out: list[dict] = []

        # ★★★ 2026-10-06 性能大修（真机实测）：原实现用**整棵 `feature_tree()`** 找配合
        #   文件夹。而 `feature_tree()` 会把**每个组件的子特征**都走一遍（58 组件装配体
        #   ⇒ 数千节点、每节点 3 次 COM）⇒ 单次 `mate_features()` 实测 ~60s；而
        #   `op_add_mate` 每条配合要调它 **3~4 次**（mates0 / mates1 / 复查 / unknown）
        #   ⇒ 单条配合 **~300s**、120 条要 **10 小时+**，且**低 CPU**（COM 编组等待）
        #   —— 看上去像"卡死"，其实是慢。
        #   ⇒ 改成**浅遍历**：只看**顶层特征**找出「配合」文件夹，再取它的**直接子节点**
        #   （含嵌套子文件夹，递归），**绝不**下探组件内部。节点数从数千降到
        #   ≈ (#配合 + 顶层件)。**语义与原来完全一致**（都是 MateGroup 直接子节点，
        #   不漏不重），只是不再走整棵树。
        def _sub_chain(node) -> list:
            """取**同层子特征**链：`GetFirstSubFeature` → `GetNextSubFeature`。

            ★ 容错（2026-10-07）：**面特征**（COM 类名 `_FxFeat`）与部分退化对象**没有**
              这两个成员，逐特征调用会抛 `AttributeError` **让整棵遍历中断**
              —— 与 `feature_tree` 在 v1.4.42 修过的是同一条坑（见其上方注释）。
              **没有子特征是正常情况**（不是错误），所以取到哪算哪、绝不往上抛。
            """
            res: list = []
            try:
                c = typed(member(node, "GetFirstSubFeature"), "IFeature")
            except Exception:                                    # noqa: BLE001
                return res
            while c is not None and len(res) < 100000:
                res.append(c)
                nxt = None
                try:
                    nxt = typed(member(c, "GetNextSubFeature"), "IFeature")
                except Exception:                                # noqa: BLE001
                    nxt = None
                c = nxt
            return res

        def _collect(folder, depth: int) -> None:
            for c in _sub_chain(folder):
                t = nm = ""
                with contextlib.suppress(Exception):
                    t = member(c, "GetTypeName2")
                with contextlib.suppress(Exception):
                    nm = member(c, "Name")
                if t in self.MATE_GROUP_TYPES:
                    _collect(c, depth + 1)                 # 嵌套「配合」子文件夹
                else:
                    sup = None
                    with contextlib.suppress(Exception):
                        sup = bool(member(c, "IsSuppressed"))
                    out.append({"type": t, "name": nm, "depth": depth,
                                "suppressed": sup})

        f = typed(member(self.model, "FirstFeature"), "IFeature")
        while f is not None:
            t = ""
            with contextlib.suppress(Exception):
                t = member(f, "GetTypeName2")
            if t in self.MATE_GROUP_TYPES:
                _collect(f, 1)
            f = typed(member(f, "GetNextFeature"), "IFeature")
        return out

    def mate_count(self) -> int:
        """配合个数（按结构数，见 `mate_features`）。"""
        return len(self.mate_features())

    def unknown_mate_features(self) -> list[dict]:
        """配合中**类型名不在 `MATE_FEATURE_TYPES`**（已确认名单）的那些。

        ★ "数目对了"之外的第二道防线：碰到没见过的类型名就报出来，便于补名单。
          **不是错误**（新类型照样被 `mate_count` 数到），只是一个提示。
        ★ `MATE_FEATURE_TYPES` 为空时回空表（"还没标定" ≠ "全是异常"）。
        """
        if not self.MATE_FEATURE_TYPES:
            return []
        return [f for f in self.mate_features()
                if f["type"] not in self.MATE_FEATURE_TYPES]

    def component_objects(self, toplevel_only: bool = True) -> list[Any]:
        """组件的 **IComponent2 对象**表（要改名/改状态时用；只看信息用 `components()`）。"""
        arr = member(self.asm, "GetComponents", 1 if toplevel_only else 0)
        return [c for c in (typed(x, "IComponent2") for x in (arr or []))
                if c is not None]

    def components(self, toplevel_only: bool = True) -> list[dict]:
        """组件表（只读，不选不动）。

        `IAssemblyDoc.GetComponents(ToplevelOnly)`；`GetComponentCount` 同参。
        每项：`name`（`Name2`，形如 `零件1-1`）/ `path` / `config` / `fixed` /
        `suppressed` / `visible`。
        """
        out: list[dict] = []
        for c2 in self.component_objects(toplevel_only):
            comp: dict[str, Any] = {}
            for key, api in (("name", "Name2"), ("path", "GetPathName"),
                             ("config", "ReferencedConfiguration"),
                             ("title", "DisplayTitle")):
                with contextlib.suppress(Exception):
                    comp[key] = member(c2, api)
            for key, api in (("fixed", "IsFixed"), ("suppressed", "IsSuppressed")):
                with contextlib.suppress(Exception):
                    comp[key] = bool(member(c2, api))
            with contextlib.suppress(Exception):
                comp["visible"] = member(c2, "Visible")
            if not comp.get("name"):
                with contextlib.suppress(Exception):
                    comp["name"] = member(c2, "Name")
            out.append(comp)
        return out

    def component_count(self, toplevel_only: bool = True) -> int:
        with contextlib.suppress(Exception):
            return int(member(self.asm, "GetComponentCount",
                              1 if toplevel_only else 0) or 0)
        return 0

    def component_boxes(self, toplevel_only: bool = True) -> dict[str, list]:
        """`{组件全名: 包围盒}`，包围盒是**全局毫米** `[x0,y0,z0,x1,y1,z1]`。

        ★★★ 为什么必须有它（2026-10-03 真机）：`add_mate` 建的配合**会把组件挪走**
          —— 甚至**翻转 + 自旋**（`align=aligned` 且两配合面法线相反时）。
          被挪走之后，后续所有"按坐标拾取面"的取点**全部作废**，
          而报出来的却是"配合实体没选全/没命中" —— **指向错误方向的报错**。
          ⇒ 唯一能在当场看见"动了"的手段就是**建配合前后各读一次包围盒**。
          ★ 所以 `op_add_mate` 用它产出一个 `moved` 字段（本 op 的"有效性"判据之一）。

        ★ 签名坑：`IComponent2.GetBox(IncludeRefPlanes: bool, IncludeSketches: bool)`
          —— **不是** `IAssemblyDoc.GetBox(Option: int)`。给组件版传整数会**静默读空**
          （不报错、回 None，见 §95 附带）。
        """
        out: dict[str, list] = {}
        for c in self.component_objects(toplevel_only):
            nm = None
            with contextlib.suppress(Exception):
                nm = member(c, "Name2") or member(c, "Name")
            if not nm:
                continue
            with contextlib.suppress(Exception):
                arr = member(c, "GetBox", False, False)
                vals = [float(v) * 1000.0 for v in (arr or [])]
                if len(vals) == 6:
                    out[str(nm)] = [round(v, 6) for v in vals]
        return out

    def resolve_component(self, name: str, toplevel_only: bool = True):
        """把用户写的名字解析成**唯一的全名 + IComponent2**（解析不了回 `(None, None)`）。

        ★★ 为什么需要它（2026-10-02 真机）：组件真名是 `<基名>-<实例号>`
          （`地基-1` / `box-2`），**光基名 `SelectByID2("box", "COMPONENT")` 选不中**。
          但"实例号"是 SolidWorks 的实现细节 —— 让用户/计划去写 `地基-1` 既啰嗦
          又**脆**（同一零件插两次，编号随插入顺序变）。所以这里做两级解析：

            ① **全名精确相等** → 直接用（`零件1-1`）；
            ② 否则按**基名**（去掉结尾的 `-<数字>`）匹配：
                 · 恰好 1 个 → 用它（`地基` → `地基-1`）；
                 · **≥2 个 → 歧义，回 (None, 候选表)** —— **绝不猜**（猜错就是静默改错件）；
                 · 0 个 → (None, None)。

        ★ 返回**三元组** `(全名, IComponent2, 候选表)` —— 三种情况：
            `("地基-1", <IComponent2>, [])`   命中（唯一）
            `(None, None, ["box-1","box-2"])`  **歧义**（候选表非空）
            `(None, None, [])`                没命中

        ★★ 为什么不用两元组：第一版把候选表放在**第二个槽**，于是
          「歧义」和「没命中」都表现为 `c is None` 之外的一种形态 ——
          调用方写 `if c is None:` 判不出来，把候选表当成了组件对象，
          结果**静默改错组件**（本段就是踩过才这么写的，自检已把三种形态钉住）。
        """
        want = str(name or "").strip()
        if not want:
            return None, None, []
        comps = self.component_objects(toplevel_only)
        named: list[tuple[str, Any]] = []
        for c in comps:
            nm = None
            with contextlib.suppress(Exception):
                nm = member(c, "Name2") or member(c, "Name")
            if nm:
                named.append((str(nm), c))
        # ⓪ ★★ **按零件路径匹配**（2026-10-05 加）：名字是**可变的**（能被改名、
        #   还会被 `SetTransformAndSolve3` **重置回默认名**），而**路径不可变**。
        #   真机现场：`insert_component` 把组件改名成 `箱座-1`，随后
        #   `set_component_transform` 一步之后它变回默认 `1-箱座-2` ——
        #   于是下一步 `fix_component("箱座")` 报「组件名对不上」。
        #   ⇒ 计划里凡是要**跨多步**指向同一个组件，写它的 `.SLDPRT` 路径最稳。
        if want.lower().endswith((".sldprt", ".sldasm")) or ("/" in want or "\\" in want):
            w = want.replace("/", "\\").lower()
            phits = []
            for nm, c in named:
                p = None
                with contextlib.suppress(Exception):
                    p = member(c, "GetPathName")
                if p and str(p).replace("/", "\\").lower() == w:
                    phits.append((nm, c))
            if len(phits) == 1:
                return phits[0][0], phits[0][1], []
            if phits:
                return None, None, [nm for nm, _ in phits]
            return None, None, []
        for nm, c in named:                       # ① 全名精确
            if nm == want:
                return nm, c, []
        hits = [(nm, c) for nm, c in named if re.sub(r"-\d+$", "", nm) == want]
        if len(hits) == 1:                        # ② 基名唯一
            return hits[0][0], hits[0][1], []
        if hits:
            return None, None, [nm for nm, _ in hits]   # 歧义：把候选列出来
        return None, None, []                     # 没命中

    def find_component(self, name: str, toplevel_only: bool = True):
        """按名字找一个 `IComponent2` 对象（找不到 / 歧义回 None）。

        ★ 接受**全名**（`零件1-1`）或**唯一的基名**（`地基`）—— 见 `resolve_component`。
        ★ 必须用**带实例号的全名**才能选中：实测 `SelectByID2("box", "COMPONENT")`
          **选不中**（选中数 0），`SelectByID2("box-2", ...)` 才行。
          组件名的实例号后缀由 SolidWorks 自己维护（改名只换基名、后缀不动）。
        ★ 想区分「歧义」和「没命中」就直接用 `resolve_component`（三类信息都在）。
        """
        return self.resolve_component(name, toplevel_only)[1]

    def select_component(self, name: str, append: bool = False,
                         mark: int = 0, toplevel_only: bool = True) -> bool:
        """选中一个组件，回**是否真的选中了**。

        ★★ 为什么不能直接用 `SelectByID2`（2026-10-02 真机确诊，A 级坑）：

            `ext.SelectByID2("box-2", "COMPONENT", ...)` **返回 False，
            但实际"选中数"是 1** —— 它**谎报失败**。
            而 `IComponent2::Select4(False, None, False)` 返回 **True 且真的选中**。

            ⇒ 判据必须是**选中数**（`selected_count()`），**绝对不能**信
              `SelectByID2` 的返回值。这条正好是"It's not the API, it's the return
              value""的又一个实例（见坑表 §1）—— 只不过这次连"组件选择"这么基础的
              操作都不例外。

        实现顺序：先用 `Select4`（返回值可信、无需名字），失败再退到
        `SelectByID2` 并用**选中数增量**判定 —— 两条路都只看选中数。
        ★ 名字可以是**全名**（`地基-1`）或**唯一的基名**（`地基`）：退路用的
           `SelectByID2` **必须喂全名**，所以这里过一遍 `resolve_component`。
        ★ 名字**歧义**（两个组件基名相同）时直接报错 —— 不猜，见 `resolve_component`。
        """
        full, c, cands = self.resolve_component(name, toplevel_only)
        if c is None and cands:
            raise SwError(f"组件名 '{name}' **有歧义**，匹配到 {cands} —— "
                          f"请写**全名**（形如 `{cands[0]}`）。"
                          f"★ 本项目不替你挑一个：猜错就是静默改错组件。")
        if not append:
            self.clear_selection()
        n0 = self.selected_count() if append else 0
        if c is not None:
            with contextlib.suppress(Exception):
                member(c, "Select4", append, None, False)
            if self.selected_count() > n0:
                return True
        # 退路：按名字选（返回值不可信，只看选中数）—— 名字要用**全名**
        typ = self.SEL.get("component", "COMPONENT")
        with contextlib.suppress(Exception):
            return_bool = self.ext.SelectByID2(full or str(name or ""), typ,
                                               0.0, 0.0, 0.0, append, mark, None, 0)
        return self.selected_count() > n0

    def last_sketch_name(self) -> str | None:
        """最后创建的那个草图的名字（草图特征类型名是 ProfileFeature）。"""
        name = None
        for t, nm in self.feature_names():
            if t in ("ProfileFeature", "Sketch"):
                name = nm
        return name

    def sketch_contour_count(self, name: str) -> int | None:
        """某个草图里的「轮廓」数量。**拉伸前最值钱的一个体检指标。**

        ==1 → 草图是一个闭合轮廓，拉伸才有戏。
        >1  → 那就是 N 个互相独立的轮廓（拉伸会失败或拉伸出一堆碎块）。
              实测：110 段图元（55 条齿廓样条 + 55 段齿根弧）会被判成 **55** 个轮廓
              —— SolidWorks 会把圆弧起点吸附到已有端点，但**不会**把样条起点吸过去。
              详见 references/30-pitfalls.md「多段图元拼不成一个轮廓」。
        """
        with contextlib.suppress(Exception):
            f = typed(self.model.FeatureByName(name), "IFeature")
            sk = typed(member(f, "GetSpecificFeature2"), "ISketch")
            return int(member(sk, "GetSketchContourCount"))
        return None

    def select_face_at(self, x: float, y: float, z: float, mark: int = 0) -> bool:
        """在坐标 (x,y,z) mm 处选面。面的名字就是坐标，必须给对点。"""
        return self.select("", "face", x=x, y=y, z=z, append=False, mark=mark)

    def clear_selection(self) -> None:
        with contextlib.suppress(Exception):
            member(self.model, "ClearSelection2", True)

    def selected_count(self) -> int:
        with contextlib.suppress(Exception):
            return self.selmgr().GetSelectedObjectCount2(-1)
        return 0

    # ---- 通用 ----

    def rebuild(self, force: bool = False) -> bool:
        try:
            return bool(member(self.model, "ForceRebuild3", not force) if force
                        else member(self.model, "EditRebuild3"))
        except Exception as e:                                  # noqa: BLE001
            raise SwError("重建失败（通常是约束冲突或特征报错）", e)

    def zoom_fit(self) -> None:
        with contextlib.suppress(Exception):
            member(self.view(), "ViewZoomtofit2")

    def feature_count(self) -> int:
        n = 0
        f = member(self.model, "FirstFeature")
        while f is not None:
            n += 1
            f = member(f, "GetNextFeature")
        return n

    def _raw_mass_props(self) -> dict | None:
        """原始质量属性读取（无重试、无怀疑判定）。拿不到对象回 None。"""
        mext = self.ext
        mp = None
        with contextlib.suppress(Exception):
            mp = member(mext, "CreateMassProperty2")
        if mp is None:
            with contextlib.suppress(Exception):
                mp = member(mext, "CreateMassProperty")
        if mp is None:
            return None
        out: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            mp.UseSystemUnits = True
        for key, attr in (("mass_kg", "Mass"), ("volume_m3", "Volume"),
                          ("area_m2", "SurfaceArea")):
            with contextlib.suppress(Exception):
                out[key] = getattr(mp, attr)
        with contextlib.suppress(Exception):
            c = mp.CenterOfMass
            if c:
                out["center_of_mass_m"] = list(c)
        return out

    def mass_props(self, fresh: bool = False) -> dict:
        """质量属性（每处都会用到，作为回归测试的比对基准）。

        ★★ 2026-10-02 真机踩到：实体处于**不可见/抑制**态时，
          `CreateMassProperty2` **不报错**、直接回 **0 质量 / 0 体积 / 0 面积**
          —— 和 `bounding_box()` 读到空是**同一个病根**（见 `bodies()` 的说明）。
          看起来像"零件是空的"，其实是"这一种读法读不到"：同一份文档的
          `check_feature_count` 数到 18 个特征、`save_as` 数到 1 个实体。
        ★ 修法：读数可疑（体积缺失或为 0）时先确认**实体到底在不在**（`self.bodies()`），
          有实体就 `EditRebuild3()` 再重读一次；仍为 0 则在结果里打 `suspect`。
        ★★ **绝不把"读不到"当 0 用** —— `sw_ops._volume` 已经写明这条：
          "不要用 0 冒充读不到：切除之后要靠体积变化判断有没有咬到材料"。
          调用方看到 `suspect` 应当报错/跳过，而不是相信 `mass_kg = 0`。

        ★★★ `fresh=True`（2026-10-12 新增）：**先 `EditRebuild3()` 再读**，并且
          **重建前后各读一次**；两次不一致时以**重建后**为准，返回里带
          `refreshed_from` 记录旧值。为什么必须加：上面的旧逻辑只在「体积读成 0」
          时才补重建 —— 体积**非 0 但陈旧**时会被**直接采信**，于是
          **一次切除的效应会出现在下一步的读数里**（= 差一个特征的滞后）。
          现场（`_diag/plans/plan_grid.json`，连切 10 条 100×590 条带）：
          10 条里有 **2 条 Δ 为正**（切了料、体积却**涨**了 13.7 万 mm³），
          且「窗口整块」与「同区域网格条」两处总去除量差 **1.5 倍**
          （4.86e6 vs 3.77e6）—— 典型「读数在说谎」。
          ⇒ **凡是拿体积变化量当判据的地方（去料 op 的守门、check_volume_delta）
            都必须 `fresh=True`。**
        """
        before = self._raw_mass_props()
        if fresh:
            with contextlib.suppress(Exception):
                self.rebuild()
            after = self._raw_mass_props()
            if after is not None:
                b0 = None if before is None else before.get("volume_m3")
                a0 = after.get("volume_m3")
                if a0 is not None:
                    if b0 is not None and abs(float(a0) - float(b0)) > 1e-15:
                        after["refreshed_from"] = float(b0)
                    return after
        out = before
        if out is None:
            return {"error": "CreateMassProperty 不可用"}
        if out.get("volume_m3"):
            return out
        have = len(self.bodies())
        if have <= 0:
            out["suspect"] = "体积为 0 且一个实体都取不到（零件可能确实是空的）"
            return out
        with contextlib.suppress(Exception):
            self.rebuild()
        again = self._raw_mass_props() or {}
        if again.get("volume_m3"):
            again["rebuilt"] = True
            return again
        out["suspect"] = (f"有 {have} 个实体但体积读成 0（重建后仍为 0）"
                          f"—— 读数在说谎，别把 mass_kg=0 当真")
        out["bodies_n"] = have
        return out

    def bodies(self, verbose: bool = False, body_type: int | None = None) -> list:
        """零件的**实体**（lst of IBody2），尽量取全。**全项目唯一的取实体入口。**

        `body_type`（2026-10-09 加，曲面建模需要）：`None` = 走下面的回退链（默认，行为不变）；
        给 `-1` / `0` / `1` = **只取那一类**（`swAllBodies` 全部体 / `swSolidBody` 实体 /
        `swSheetBody` 曲面体，见 `swBodyType_e`），含隐藏。
        ★★ **为什么必须开这个口子**：默认回退链的**第一档就是 `(0, False)` 只要实体**
        ⇒ 一个**纯曲面零件**（只有 sheet body）会由第二档 `(-1, False)` 命中，
        于是「这零件里到底是实体还是曲面体」「加厚之后曲面还剩下没有」**读不出区别** ——
        而这两件事正是曲面建模每一步的验收判据（`_body_stat` 靠它）。
        ★ 仍然只有本方法一处直接调 `GetBodies2` —— 自检的源码级守卫不放宽。

        ★★ 2026-10-02 真机踩到（很隐蔽）：`GetBodies2(0, **True**)` 是
          **只取可见实体** 的变体。某次 `extrude_cut` 选草图失败之后，实体
          处于不可见/抑制态 ⇒ 这个调用回 **空**，于是 `check_bbox` 报
          「读不到包围盒（零件可能是空的）」、`check_cyl_radius` 报「一个圆柱面都没有」、
          `check_mass` 的质量/体积回 0 —— 而同一份文档的 `check_feature_count` 明明数到 **18 个特征**、
          `save_as` 也数到 **1 个实体**。
          ⇒ **读数在说谎**，看起来像"零件空了"，其实是"这一种取法取不到"。
          这和 `sw_ops` 里早写下的那条教训是同一个坑：
            「拿全部实体走 `GetBodies2(-1, False)`；**别用** `GetBodies2(0, True)`
              —— 多实体/隐藏时会漏。」

        参数语义（`IPartDoc::GetBodies2(BodyType, bVisibleOnly)`）：
            BodyType：`-1` = 全部（含曲面体）、`0` = 只要实体、`1` = 只要曲面体
            bVisibleOnly：**`True` = 只要可见的**（★ 就是它漏东西）

        顺序（**先要"对的"，再退到"兜底的"**）：
            ① `(0, False)`  —— 实体、含隐藏（正解）
            ② `(-1, False)` —— 全部体、含隐藏（多实体/曲面体时的兜底）
            ③ `(0, True)`   —— 只可见（**最后兜底**，走到这说明前面都空）
        命中哪一档记在 `self.last_body_query` 里 —— 排查"读数在说谎"时，
        这是唯一能对质的东西（`check_bbox` 会把它带进结果和报错）。
        """
        explicit = body_type is not None
        # ★ 统一成**一条链**：显式指定 = 只有一档；默认 = 三级回退。
        #   这样 `GetBodies2` 全文件仍然**只有一处调用**（源码级自检守卫 COUNT==1 不放宽）。
        chain = ((int(body_type), False),) if explicit else ((0, False), (-1, False), (0, True))
        tried: list = []
        for args in chain:
            try:
                bs = self.model.GetBodies2(*args)
                n = len(list(bs)) if bs else 0
            except Exception:                                   # noqa: BLE001
                n = -1
            tried.append((args, n))
            if n > 0:
                self.last_body_query = {"args": list(args), "n": n, "tried": tried}
                if verbose:
                    print(f"[bodies] 命中 {args} → {n} 个（回退链 {tried}）")
                return list(bs)
        # 全空。★ 显式指定时也要把「问的是哪一档」记下来 ——
        #   排查「明明有曲面体却读成 0」时，这是唯一能对质的东西。
        self.last_body_query = {"args": (list(chain[0]) if explicit else None),
                               "n": 0, "tried": tried}
        if verbose:
            print(f"[bodies] 全空（回退链 {tried}）")
        return []

    def bounding_box(self) -> list[float] | None:
        """零件的**实体**包围盒（米）：[xmin,ymin,zmin,xmax,ymax,zmax]。

        ★ 不要用 `IModelDoc2.GetPartBox()`：实测它给出的盒子比实体大 ——
          丝杠（实体 Ø24）GetPartBox 回 ±12.3124、GetBodyBox 回 ±12.0。
          多出的 0.3124mm 会让 check_bbox 误报，而"数值只差一点点"的坑最难查。
          走实体自己的 `IBody2.GetBodyBox`，多个实体时取并集。
        ★★ 取实体必须走 `self.bodies()`（从全到窄回退）—— 详见 `bodies()` 的说明：
          曾经用 `GetBodies2(0, True)`（只可见）⇒ 实体一被抑制就整片读数变空。
        """
        try:
            boxes = []
            for b in self.bodies():
                bb = member(typed(b, "IBody2"), "GetBodyBox")
                if bb:
                    boxes.append(list(bb))
            if not boxes:
                return None
            return [min(b[0] for b in boxes), min(b[1] for b in boxes),
                    min(b[2] for b in boxes),
                    max(b[3] for b in boxes), max(b[4] for b in boxes),
                    max(b[5] for b in boxes)]
        except Exception:                                       # noqa: BLE001
            return None


# 中文/英文基准面名互转。
#
# ★ 2026-09-25（需求 2）起，**权威表搬到 `assets/sw_names.json`**，由 `sw_naming`
#   提供；这张硬编码表退化成「sw_naming 不可用时的兜底」。原因：中英名不只影响
#   选择，还影响**自动命名**（草图1 ↔ Sketch1、基准轴1 ↔ Axis1），必须放一起管，
#   否则「同一份计划在两种界面下产出不同特征名」这个坑永远堵不上。
_ALIAS = {
    "前视基准面": ["Front Plane", "前视基准面"],
    "前视": ["Front Plane", "前视基准面"],
    "Front": ["Front Plane", "前视基准面"],
    "上视基准面": ["Top Plane", "上视基准面"],
    "上视": ["Top Plane", "上视基准面"],
    "Top": ["Top Plane", "上视基准面"],
    "右视基准面": ["Right Plane", "右视基准面"],
    "右视": ["Right Plane", "右视基准面"],
    "Right": ["Right Plane", "右视基准面"],
    "原点": ["Origin", "原点"],
    "Origin": ["Origin", "原点"],
}


def _aliases(name: str) -> list[str]:
    """任意中/英别名 → 一串候选真名。优先查 `assets/sw_names.json`。"""
    out: list[str] = []
    with contextlib.suppress(Exception):
        out = sw_naming.all_aliases(name)
    if not out:
        out = _ALIAS.get(name, [])
    return [a for a in out if a != name]


# --------------------------------------------------------------- CLI

def _cli() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="SolidWorks 连接自检")
    ap.add_argument("--connect", action="store_true", help="真的连一下（会启动 SolidWorks）")
    ap.add_argument("--invisible", action="store_true",
                    help="隐藏 SolidWorks 窗口后再连（默认可见；常驻实例无法事后隐藏）")
    ap.add_argument("--late", action="store_true", help="强制晚期绑定")
    ap.add_argument("--selftest", action="store_true",
                    help="端到端冒烟：新建零件→画圆→拉伸→读质量→关闭")
    a = ap.parse_args()

    rep = sw_env.report()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    for p in sw_env.problems(rep):
        print("  !", p)

    if not (a.connect or a.selftest):
        return
    sw = SwSession.open(visible=not a.invisible, late_bound=a.late)
    print(f"\n已连接。版本号(主版本)={sw.version}  常量来源={sw.C.source}"
          f"  常量条数={len(sw.C.table)}")
    if not sw.C.table:
        print("  ! 常量表是空的，建模前必须先解决")

    if a.selftest:
        with sw.suppress_prompts(), sw.command_in_progress():
            m = sw.new_part()
            print("  新建零件 OK:", member(m, "GetTitle"))
            print("  文档里的基准面真名:", sw.plane_names() or "(没找到)")
            if not sw.plane_names():
                print("  全部特征:", sw.feature_names())
            sm = sw.skm()
            if not sw.select_plane("Front"):
                raise SwError("选不中前视基准面。看上面列出的真名，改 _ALIAS 表。")
            sm.InsertSketch(True)
            sm.AddToDB = True
            sm.CreateCircleByRadius(0, 0, 0, mm(25))
            sm.AddToDB = False
            sm.InsertSketch(True)
            f = member(sw.fm(), "FeatureExtrusion3",
                       True, False, False, sw.C("swEndCondBlind"), 0, mm(10), 0,
                       False, False, False, False, 0, 0, False, False, False, False,
                       True, False, False, sw.C("swStartSketchPlane"), 0, False)
            print("  拉伸 OK:", f is not None)
            print("  质量:", sw.mass_props())
            print("  包围盒:", sw.bounding_box())
        sw.close()


if __name__ == "__main__":
    _cli()
