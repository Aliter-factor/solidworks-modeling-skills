#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_naming.py —— 命名的唯一权威：把「界面语言相关的自动名」翻译成「规范名」。

## 为什么需要它（需求 2）

SolidWorks 给特征自动起的名字**跟界面语言绑死**：

    中文界面：草图1  凸台-拉伸1  切除-拉伸1  圆角1  倒角1  螺纹线1  基准轴1
    英文界面：Sketch1  Boss-Extrude1  Cut-Extrude1  Fillet1  Chamfer1  Thread1  Axis1

于是同一个建模计划在两种界面下跑，产出的特征名不一样 —— 计划里写的
`profile: "Sketch1"` 在中文机上直接找不到东西。这就是「特征名中英文混乱」的病根。

## 三条策略（2026-09-25 用户拍板）

1. **主名 = 中文**（`primary: "cn"`）。所有规范名按中文表生成。
2. **只改「看得出是自动名」的**。名字能匹配某语言模板 + 数字（如 `Sketch1`、
   `凸台-拉伸3`）才动；显式命名的（`Rect40`、`箱体轮廓`）一律不碰 ——
   绝不覆盖人的命名。
3. **中英双向都认**。`resolve()` 无论给 `Front` / `前视基准面` / `Front Plane`
   都能落到文档里真实存在的那个名字上。

## 本模块是**纯函数**，不 import sw_connect

因为 sw_connect 要在自己的 `_aliases()` 里用这里的别名表；如果这里再 import
回去就会循环。语言检测需要 COM，放在 `SwSession.current_language()` 里，
这里只负责把 `"chinese-simplified"` 这种原始串映射成 `"cn"`。

## 地面真值

`assets/sw_names.json` 的 cn 模板不是编的，是从
`C:/学习软件/一级减速器` 36 件真实零件 + `5-螺钉M3x22` 实扫出来的：
实见 `草图1 / 草图9 / 草图13 / 旋转2 / 切除-拉伸1 / 凸台-拉伸3 / 倒角2 / 螺纹线1`。

命令行速查：
    python scripts/sw_naming.py --resolve Front
    python scripts/sw_naming.py --kind Extrusion --index 2 --lang cn
    python scripts/sw_naming.py --match Sketch7
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAMES_PATH = ROOT / "assets" / "sw_names.json"


@lru_cache(maxsize=1)
def load() -> dict:
    with open(NAMES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def primary_lang() -> str:
    return str(load().get("primary") or "cn")


def raw_to_lang(raw: str | None, default: str | None = None) -> str:
    """把 `ISldWorks.GetCurrentLanguage()` 的原始串（如 'chinese-simplified'）
    映射成 'cn' / 'en' / 其它原样返回。"""
    if not raw:
        return default or primary_lang()
    key = str(raw).strip().lower()
    return str(load().get("languages", {}).get(key, key))


def lang_label(lang: str) -> str:
    return str(load().get("lang_label", {}).get(lang, lang))


# --------------------------------------------------------------- 类型 → 语义

def kind_of(type_name: str | None) -> str | None:
    """特征树的类型名 → 语义 kind（'sketch'/'fillet'…）。认不出返回 None。

    ★ 类型名有几个坑（都来自实扫，不是文档）：
      · 草图的类型名是 **ProfileFeature**，不是 "Sketch"；
      · 基准轴是 **RefAxis**；基准面是 **RefPlane**；
      · `GetTypeName2()` 对某些拉伸特征会回 **"ICE"**（Instant3D 类型名）——
        这时要改用 `GetTypeName()` 拿真类型，见 sw_ops 的调用处。
    """
    if not type_name:
        return None
    return load().get("type_to_kind", {}).get(str(type_name))


def is_never_rename(type_name: str | None) -> bool:
    return str(type_name or "") in load().get("never_rename", {})


def never_rename_hint(type_name: str | None) -> str:
    return str(load().get("never_rename", {}).get(str(type_name or ""), ""))


# --------------------------------------------------------------- 规范名模板

def kinds() -> dict:
    return load().get("kinds", {})


def template(kind: str, lang: str | None = None) -> str | None:
    """取某个 kind 在某语言下的模板，如 ('fillet','cn') → '圆角{n}'。"""
    lang = lang or primary_lang()
    rec = kinds().get(str(kind))
    if not rec:
        return None
    return rec.get(lang) or rec.get("cn") or rec.get("en")


def canonical(kind: str, index: int, lang: str | None = None) -> str:
    """生成规范名：canonical('fillet', 2, 'cn') → '圆角2'。"""
    tpl = template(kind, lang)
    if not tpl:
        raise KeyError(f"未知的特征类别 {kind!r}。可用：{', '.join(sorted(kinds()))}")
    return tpl.replace("{n}", str(int(index)))


def _tpl_regex(tpl: str) -> re.Pattern:
    head, sep, tail = tpl.partition("{n}")
    if not sep:
        return re.compile(f"^{re.escape(tpl)}$")
    return re.compile(r"^" + re.escape(head) + r"(\d+)" + re.escape(tail) + r"$")


@lru_cache(maxsize=256)
def _kind_regex(kind: str, lang: str) -> re.Pattern:
    return _tpl_regex(template(kind, lang) or "")


def split_auto(name: str | None, kind: str, lang: str | None = None) -> int | None:
    """判断 `name` 是不是某 kind 的自动名，是则返回其中的序号，否则 None。"""
    if not name:
        return None
    m = _kind_regex(str(kind), lang or primary_lang()).match(str(name))
    return int(m.group(1)) if m else None


def match_auto(name: str | None, lang: str | None = None) -> tuple[str, int] | None:
    """扫所有 kind，认出这个自动名属于谁：(kind, 序号)。认不出返回 None。

    跨语言都试（中英各一遍），因为同一台机器上可能混着两种名字 ——
    实测就有计划写死 "Boss-Extrude1" 跑到中文界面上找不到东西的情况。
    """
    if not name:
        return None
    for lg in ([lang] if lang else []) + ["cn", "en"]:
        for kind in kinds():
            idx = split_auto(name, kind, lg)
            if idx is not None:
                return kind, idx
    return None


def is_auto_name(name: str | None, lang: str | None = None) -> bool:
    return match_auto(name, lang) is not None


# --------------------------------------------------------------- 基准面/原点

def default_planes() -> dict:
    return load().get("default_planes", {})


def plane_aliases(which: str) -> list[str]:
    """给 `select_plane` 用：把 Front / 前视 / 前视基准面 等统一成一串候选真名。"""
    table = default_planes()
    key = str(which).strip()
    low = key.lower()
    for _k, rec in table.items():
        cands = [str(a) for a in rec.get("aliases", [])]
        cands += [rec.get("cn", ""), rec.get("en", "")]
        if key in cands or low in [c.lower() for c in cands]:
            out: list[str] = []
            for c in [rec.get("cn"), rec.get("en"), *rec.get("aliases", [])]:
                if c and c not in out:
                    out.append(str(c))
            return out
    return []


def plane_name(which: str, lang: str | None = None) -> str | None:
    """规范基准面名：plane_name('Front','cn') → '前视基准面'。

    ★ 2026-09-25 修：原实现用 `rec.get('cn') in aliases or rec.get('en') in aliases`
      在 `default_planes` 里线性扫 —— 依赖字典顺序、且 `and/or` 混用后语义含糊。
      现在改成「先用别名表定位 key，再取该语言的名字」，不依赖顺序。
    """
    for cand in plane_aliases(which) or []:
        key = match_default_plane(cand)
        if key:
            rec = default_planes().get(key) or {}
            return str(rec.get(lang or primary_lang()) or rec.get("cn") or rec.get("en"))
    return None


def all_aliases(name: str) -> list[str]:
    """任意名字 → 去掉自身后的候选别名（给 SelectByID2 兜底轮着试）。"""
    cands = plane_aliases(name)
    return [c for c in cands if c != name]


def match_default_plane(name: str | None) -> str | None:
    """是不是**自带的**基准面/原点？是则回 key（front/top/right/origin）。

    ★ 只按**官方中/英名**精确匹配（`前视基准面` / `Front Plane`），
      **不收** `Front` / `前视` 这类简称 —— 简称太容易撞上人自己起的名字
      （比如有人把自定义基准面就叫 "Front"），拿去自动改名会造成事故。

    ★★ 为什么需要它：自带基准面的名字**不在** `kinds` 的自动名模板里
      （它是固定名，不是 `基准面{n}`），所以 `match_auto()` 认不出它。
      2026-09-25 真机就踩了这个：把 `上视基准面` 改成 `Top Plane` 再跑
      `normalize_names(planes=true)`，它被判成「显式命名，不动」而跳过 ——
      自动名判定必须**两条路都走**。
    """
    if not name:
        return None
    key = str(name).strip()
    for k, rec in default_planes().items():
        if key in (rec.get("cn"), rec.get("en")):
            return k
    return None


# --------------------------------------------------------------- CLI

def _cli() -> int:
    ap = argparse.ArgumentParser(description="SolidWorks 命名表速查")
    ap.add_argument("--resolve", help="展开某个名字的所有别名（如 Front）")
    ap.add_argument("--kind", help="按 kind 生成规范名（如 fillet）")
    ap.add_argument("--index", type=int, default=1)
    ap.add_argument("--lang", default=None, help="cn / en（默认取主名）")
    ap.add_argument("--match", help="判断某名字是不是自动名")
    ap.add_argument("--type", help="按特征类型名查 kind（如 Extrusion）")
    ap.add_argument("--list", action="store_true", help="列出全部 kind 与中英模板")
    a = ap.parse_args()
    if a.resolve:
        print(f"{a.resolve} → {all_aliases(a.resolve) or '(不是基准面/原点)'}")
    if a.kind:
        print(f"{a.kind}[{a.index}] @{a.lang or primary_lang()} = "
              f"{canonical(a.kind, a.index, a.lang)}")
    if a.match:
        print(f"{a.match} → {match_auto(a.match)}")
    if a.type:
        print(f"{a.type} → kind={kind_of(a.type)}")
    if a.list:
        print(f"主名语言：{primary_lang()}（{lang_label(primary_lang())}）")
        for k, rec in sorted(kinds().items()):
            print(f"  {k:<18} cn={rec.get('cn',''):<14} en={rec.get('en','')}")
    if not any([a.resolve, a.kind, a.match, a.type, a.list]):
        ap.print_help()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_cli())
