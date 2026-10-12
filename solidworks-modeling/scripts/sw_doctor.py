#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_doctor.py —— 一条命令跑完所有自检，直接给「这套东西能不能用」的结论。

给谁用
------
给**接手这个技能的另一个智能体/另一个人**用。不用读代码、不用猜哪步该跑什么，
先跑这一条命令就知道：

    python scripts/sw_doctor.py            # 离线体检（不需要 SolidWorks！）
    python scripts/sw_doctor.py --real     # 追加真机端到端（需要 SolidWorks + pywin32）
    python scripts/sw_doctor.py --json     # 机器可读，供代理判断

检查项
------
  1. 解释器：Python 版本、位数、pywin32 是否可导入
  2. 环境：SolidWorks 装在哪、版本、位数是否匹配、零件模板是否可用
  3. 离线回归：sw_selftest.py 全量断言（**不碰 SolidWorks**，任何机器都能跑）
  4. 国标数据：gb_standard.json 抽检 + 随包速查表快照是否同步（**离线权威表**）
  4b. 减速器参数表：交叉校核 C1~C10 全过 + 机读 op 导出与代码同步（**项目真源**）
  5. 模板校验：6 个模板逐个生成计划并静态校验
  6. （--real）真机端到端：连接冒烟 + 真建一个长方体 + 量包围盒

退出码：0 = 全部通过（真机项被跳过不算失败）；1 = 有失败项。
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
TEMPLATES = ["box", "shaft", "bolt", "flange", "gear", "leadscrew"]


_USER_SITE_ADDED: list[str] = []


def user_site_packages() -> list[str]:
    """列出**用户级 site-packages** 目录（`%APPDATA%\\Python\\Python*`）。

    ★★★ 为什么要这个（坑表 §104①，2026-10-03 真踩）：
      本机 `pymupdf` / `rapidocr_onnxruntime` / `onnxruntime` / `PIL` / `numpy`
      / `cv2` / `pywin32` **全都装在用户级 site-packages**，而它
      **不在默认 `sys.path` 上** ⇒ 裸 `find_spec` 一律返回 None
      ⇒ 误判成「没装」⇒ 我甚至去**手工解 PDF 流**白绕很久。

    ★ 与项目记忆里 COM 脚本必做的 `export APPDATA=...` **同源**。
    """
    import glob as _g
    cands: list[str] = []
    bases = {os.environ.get("APPDATA") or "",
             os.path.expandvars(r"%APPDATA%"),
             os.path.join(os.path.expanduser("~"), "AppData", "Roaming")}
    for base in bases:
        if not base:
            continue
        cands += _g.glob(os.path.join(base, "Python", "Python*",
                                       "site-packages"))
    return [c for c in cands if os.path.isdir(c)]


def ensure_user_site() -> list[str]:
    """把用户级 site-packages 加进 `sys.path`（幂等），返回实际加进去的。"""
    added = []
    for c in user_site_packages():
        if c not in sys.path:
            sys.path.append(c)
            added.append(c)
    _USER_SITE_ADDED.extend(added)
    return added


def has_module(name: str) -> bool:
    """模块能不能 import —— **先补用户级 site-packages 再判**。

    ★ 这是全项目**唯一**的依赖探测入口。同一模块在两处用两种方式探测
      就会出现「`[7]` 说 Pillow PASS、`[2]` 说 FAIL」这种**自相矛盾**
      （2026-10-03 真发生过），而自相矛盾的体检比没有体检更糟。
    """
    if importlib.util.find_spec(name) is not None:
        return True
    ensure_user_site()
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:                                   # noqa: BLE001
        return False


class Report:
    def __init__(self, as_json: bool = False) -> None:
        self.rows: list[dict] = []
        self.notes: list[str] = []
        self.tmpdirs: list[Path] = []
        self.as_json = as_json
        self.skipped = 0

    def add(self, name: str, ok: bool | None, detail: str = "") -> None:
        """ok=None 表示跳过（环境不满足，不算失败）。"""
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        self.rows.append({"name": name, "status": status, "detail": detail})
        if ok is None:
            self.skipped += 1
        if not self.as_json:
            print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))

    def note(self, msg: str) -> None:
        self.notes.append(msg)
        if not self.as_json:
            print(f"      提示：{msg}")

    def section(self, title: str) -> None:
        if not self.as_json:
            print(f"\n{title}")

    @property
    def failed(self) -> list[dict]:
        return [r for r in self.rows if r["status"] == "FAIL"]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.rows if r["status"] == "PASS")


def run(cmd: list[str], env: dict | None = None, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"


def last_meaningful_line(text: str, width: int = 150) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1][:width] if lines else ""


# 报错行优先：脚本尾部常混着无害的 [warn]，直接取最后一行会把它当成失败原因
ERR_HINTS = ("错误", "失败", "Error", "error", "Traceback", "SwError", "无法", "超差")


def err_line(text: str, width: int = 200) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    bad = [l for l in lines if any(h in l for h in ERR_HINTS) and not l.startswith("[warn]")]
    if bad:
        return bad[-1][:width]
    clean = [l for l in lines if not l.startswith("[warn]")]
    return (clean[-1] if clean else (lines[-1] if lines else ""))[:width]


# ------------------------------------------------------------------ 各项检查


def sec_interpreter(rep: Report) -> None:
    rep.section("[1] 解释器与依赖")
    v = sys.version_info
    rep.add("Python >= 3.9", v >= (3, 9), f"{sys.version.split()[0]}")
    bits = 64 if sys.maxsize > 2 ** 32 else 32
    rep.add("Python 是 64 位", bits == 64,
            f"{bits} 位" + ("" if bits == 64 else "（必须与 SolidWorks 同位数，否则 COM 连不上）"))
    rep.add("脚本依赖仅标准库", True, f"解释器：{sys.executable}")

    _pil = has_module("PIL")
    rep.add("Pillow 可导入（截图转 PNG 用）", _pil,
            "已装" if _pil else
            "未装 → 只影响截图转 PNG / 拼总览图（不影响建模）")


def sec_env(rep: Report) -> dict | None:
    rep.section("[2] SolidWorks 环境")
    sys.path.insert(0, str(HERE))
    try:
        import sw_env
    except Exception as exc:  # noqa: BLE001
        rep.add("导入 sw_env", False, repr(exc))
        return None
    info = sw_env.report()
    rep.add("找到 SolidWorks 安装", info["installed"],
            info["install_dir"] or "未找到 → 设环境变量 SW_ROOT=<安装目录>")
    if info["installed"]:
        rep.add("SolidWorks 版本", info["sw_bits"] is not None,
                f"{info['version']} / {info['sw_bits']} 位")
        rep.add("位数与 Python 匹配", info["bits_match"] is not False,
                f"SW {info['sw_bits']} vs Python {info['python_bits']}")
        rep.add("零件模板可用", bool(info["templates"].get("part")),
                info["templates"].get("part") or "缺 *.prtdot，NewDocument 会弹框卡死")
        out_dir = sw_env.out_dir()
        rep.add("产出目录可写", True, f"{out_dir}（用 YOYO_SW_OUT 覆盖）")

    # pywin32 只有"要用真机"时才必需：没装 SolidWorks 的机器上它无关紧要（算跳过）
    has_pywin32 = has_module("win32com")
    if not info["installed"]:
        rep.add("pywin32 可导入", None, "跳过：本机没有 SolidWorks，装 pywin32 也无用")
    else:
        if has_pywin32 and _USER_SITE_ADDED:
            _w = ("已装（★ 在**用户级 site-packages**，本函数已补进 "
                  "`sys.path`；裸 `find_spec` 看不到 —— 坑表 §104①）")
        elif has_pywin32:
            _w = "已装"
        else:
            _w = (f"未装 → {sys.executable} -m pip install pywin32 "
                  f"-i https://pypi.tuna.tsinghua.edu.cn/simple")
        rep.add("pywin32 可导入", has_pywin32, _w)
        if not has_pywin32:
            rep.note("没有 pywin32：离线功能（校验器 / 几何算法 / 宏翻译）照常可用，"
                     "只是不能真机建模。")
    for p in sw_env.problems(info):
        rep.note(p)
    return info


def sec_offline(rep: Report) -> None:
    rep.section("[3] 离线回归（不碰 SolidWorks）")
    rc, out = run([PY, str(HERE / "sw_selftest.py")], timeout=300)
    stat = ""
    for line in out.splitlines():
        if "通过" in line and "项" in line:
            stat = line.strip()
    rep.add("sw_selftest.py 全量断言", rc == 0, stat or err_line(out))
    # ★ 「待重跑」不是失败，但**必须显眼** —— 它是"设计改了、真机记录还没重跑"，
    #   不看它就会以为这些件的数值判据还是有效的（记录其实是旧版）。
    if "待重跑" in stat:
        rep.note("★ 有真机记录**待重跑覆盖**（设计变更导致记录成代作废）—— "
                 "不是回归；跑 `sw_selftest.py` 看「待重跑清单」里的件，"
                 "真机重跑后回写 `_measured_faces.built` 即清。")


def sec_gb(rep: Report) -> None:
    """[4] 国标数据（离线权威表）—— 用户要求「打包好，方便 ai 直接查询」。

    这一节单独列出来，是为了让体检报告**一眼看到国标是齐的、且和快照没漂移**。
    """
    rep.section("[4] 国标数据（离线权威表，不联网）")
    try:
        sys.path.insert(0, str(HERE))
        import sw_gb  # noqa: PLC0415
    except Exception as e:                                        # noqa: BLE001
        rep.add("sw_gb 可导入", False, str(e))
        return
    try:
        d = sw_gb.load()
        fams = d.get("families", {})
        n_rows = sum(len(v.get("rows", [])) for v in fams.values())
        rep.add("gb_standard.json 可解析", True,
                f"revision={d.get('revision')} updated={d.get('updated')} "
                f"{len(fams)} 族 / {n_rows} 行规格")
        # 权威值抽检（改错立刻暴露）
        b = sw_gb.row("hex_bolt", "M8")
        rep.add("抽检：六角头 M8", b.get("s") == 13 and abs(b.get("dw", 0) - 11.6) < 0.05,
                f"s={b.get('s')} k={b.get('k')} dw={b.get('dw')}")
        rep.add("抽检：轴承 6204", sw_gb.bearing("6204")["D"] == 47, "20×47×14")
        rep.add("抽检：Ø20 H7/k6", sw_gb.fit(20, "H7", "k6")["hole"]["es_um"] == 21,
                "H7=+21/0 μm，过渡配合")
    except Exception as e:                                        # noqa: BLE001
        rep.add("国标抽检", False, f"{type(e).__name__}: {e}")
        return
    # 随包速查表快照：必须存在且与 JSON 同步
    snap = HERE.parent / "references" / "41-gb-cheatsheet.md"
    if not snap.is_file():
        rep.add("速查表快照存在", False, f"缺少 {snap}")
        return
    txt = snap.read_text(encoding="utf-8")
    sync = (f"updated={d.get('updated')}" in txt) and ("6204" in txt) and ("M8=1.25" in txt)
    rep.add("速查表快照与 JSON 同步", sync,
            f"{snap.name}，{len(txt.splitlines())} 行"
            + ("" if sync else "（★ 改了 JSON 忘了 `--write-md` 重新生成）"))


def sec_reducer(rep: Report) -> None:
    """[4b] 减速器参数表 + 机读 op 导出 —— 项目级真源与它的一致性。

    `60-reducer-project.md` 的口径：35 个零件的跨件耦合值（中心距 70、中心高 80…）
    **只允许有一份**（`assets/reducer/reducer.params.json`）。这一节回答两件事：
      ① 参数表本身自洽 —— C1~C11 交叉校核 + R 系列对账全过；
      ② 机读 op 导出（`assets/op_schema.json`）没和 `sw_ops.py` 漂移。
    全离线，与开不开 SolidWorks 无关。
    """
    rep.section("[4b] 减速器参数表 + 机读 op 导出（项目真源）")
    try:
        sys.path.insert(0, str(HERE))
        import sw_ops        # noqa: PLC0415
        import sw_reducer    # noqa: PLC0415
    except Exception as e:                                        # noqa: BLE001
        rep.add("sw_reducer / sw_ops 可导入", False, str(e))
        return
    try:
        P = sw_reducer.load()
        parts = {k: x for k, x in P.get("parts", {}).items() if not k.startswith("_")}
        rep.add("reducer.params.json 可解析", True,
                f"{len(parts)} 个零件 / "
                f"{len([k for k in P.get('checks', {}) if not k.startswith('_')])} 条校核规则")
        r = sw_reducer.run_all(P)
        rep.add("交叉校核 C1~C11 + R 系列全过", r["ok"],
                f"{r['n_pass']} 通过 / {r['n_fail']} 硬失败 / {r['n_open']} 待图纸确认")
        # 参数化进度：已由参数表驱动生成计划的件数（"照项目建模"的真实进度表）
        n_b = len(sw_reducer.BUILDERS)
        modeled = sum(1 for x in parts.values() if x.get("status") == "modeled")
        _fam_names = (("环形族", "RING_FAMILY"),
                      ("闷盖族", "COVER_BLIND_FAMILY"),
                      ("型面驱动族", "PROFILE_FAMILY"))
        _fams = " / ".join(f"{cn} {len(getattr(sw_reducer, attr, ()))} 件"
                           for cn, attr in _fam_names
                           if getattr(sw_reducer, attr, ()))
        rep.add("参数化进度", n_b > 0,
                f"{n_b}/{len(parts)} 件有生成器（已对账 modeled = {modeled}）"
                f"；{_fams}共用生成器")
    except Exception as e:                                        # noqa: BLE001
        rep.add("减速器参数表校核", False, f"{type(e).__name__}: {e}")
        return
    # 机读 op 导出：必须存在且与 sw_ops.schema() 同步（改了 op 忘导出会当场失败）
    sp = ROOT / "assets" / "op_schema.json"
    if not sp.is_file():
        rep.add("机读 op 导出存在", False,
                f"缺少 {sp}（跑 `sw_ops.py --schema` 生成）")
        return
    onfile = json.loads(sp.read_text(encoding="utf-8"))
    fresh = sw_ops.schema()
    same = (onfile.get("n_ops") == fresh["n_ops"]
            and onfile.get("n_with_limits") == fresh["n_with_limits"]
            and {o["name"]: tuple(o["limits"]) for o in onfile.get("ops", [])}
            == {o["name"]: tuple(o["limits"]) for o in fresh["ops"]})
    rep.add("op_schema.json 与代码同步", same,
            f"{onfile.get('n_ops')} op / {onfile.get('n_with_limits')} 带已知边界"
            + ("" if same else "（★ 改了 op 忘了 `sw_ops.py --schema` 重新导出）"))


def sec_templates(rep: Report) -> None:
    rep.section("[5] 模板离线校验")
    tmp = Path(tempfile.mkdtemp(prefix="swdoctor_"))
    rep.tmpdirs.append(tmp)
    bad: list[str] = []
    try:
        for t in TEMPLATES:
            plan = tmp / f"{t}.json"
            rc, out = run([PY, str(HERE / "sw_plan.py"), "--template", t], timeout=180)
            if rc != 0:
                bad.append(t)
                continue
            plan.write_text(out, encoding="utf-8")
            rc2, out2 = run([PY, str(HERE / "sw_plan.py"), str(plan), "--check"], timeout=180)
            if rc2 != 0:
                bad.append(f"{t}({err_line(out2, 80)})")
        rep.add(f"{len(TEMPLATES)} 个模板生成 + 静态校验", not bad,
                "全部通过：" + " / ".join(TEMPLATES) if not bad else "失败：" + ", ".join(bad))
    finally:
        for f in tmp.glob("*"):
            f.unlink(missing_ok=True)
        tmp.rmdir()


def sec_real(rep: Report, info: dict | None) -> None:
    rep.section("[6] 真机端到端（会启动 SolidWorks）")
    if not info or not info.get("installed"):
        rep.add("真机端到端", None, "跳过：本机没找到 SolidWorks")
        return
    if importlib.util.find_spec("win32com") is None:
        rep.add("真机端到端", None, "跳过：没装 pywin32")
        return

    t0 = time.time()
    rc, out = run([PY, str(HERE / "sw_connect.py"), "--selftest"], timeout=900)
    rep.add("连接冒烟（连接→新建→画圆→拉伸→质量）", rc == 0,
            "通过" if rc == 0 else err_line(out))

    # 真建一个零件：长方体 80x40x10。
    # ★ 生成模板时**也要**带上 YOYO_SW_OUT：save_as 的路径在生成那一刻就写进计划里了，
    #   只给 --run 设环境变量是没用的 —— 会写到默认目录去（第一次就踩了这个）。
    tmp = Path(tempfile.mkdtemp(prefix="swdoctor_out_"))
    rep.tmpdirs.append(tmp)
    env = dict(os.environ, YOYO_SW_OUT=str(tmp))
    rc1, tpl = run([PY, str(HERE / "sw_plan.py"), "--template", "box"],
                   env=env, timeout=180)
    plan = tmp / "box.json"
    plan.write_text(tpl, encoding="utf-8")
    # 注意：JSON 里反斜杠是转义的，别拿字符串直接搜路径；解析出来再比
    try:
        saves = [s.get("path") for s in json.loads(tpl).get("steps", [])
                 if s.get("op") == "save_as"]
    except Exception:  # noqa: BLE001
        saves = []
    if not saves or not all(str(tmp) in str(s) for s in saves):
        rep.add("真机建模 box（80x40x10）", False,
                "save_as 路径没落到临时目录，跳过以免污染默认产出目录")
        rep.note(f"临时产物：{tmp}（跑完已清理，--keep-temp 可保留）")
        return
    rc2, out2 = run([PY, str(HERE / "sw_plan.py"), str(plan), "--run",
                     "--report", str(tmp / "box_report.json")], env=env, timeout=900)
    part = tmp / "box.SLDPRT"
    rep.add("真机建模 box（80x40x10）", rc2 == 0 and part.exists(),
            f"{part.name} {part.stat().st_size // 1024}KB" if part.exists()
            else err_line(out2))

    if part.exists():
        rc3, out3 = run([PY, str(HERE / "sw_inspect.py"), "--doc", str(part),
                         "--json", str(tmp / "box_insp.json")], env=env, timeout=600)
        size = None
        try:
            data = json.loads((tmp / "box_insp.json").read_text(encoding="utf-8"))
            box = data.get("bbox_mm") or data.get("box_mm")
            if box:
                size = [round(abs(box[i + 3] - box[i]), 2) for i in range(3)]
        except Exception:  # noqa: BLE001
            pass
        expect = [80.0, 40.0, 10.0]
        ok = bool(size) and all(abs(a - b) < 0.2 for a, b in zip(size, expect))
        rep.add("读数自查：包围盒 == 80x40x10", ok,
                f"实测 {size}" if size else f"未取到包围盒（{err_line(out3, 80)}）")
        rep.add("截图自查（SaveBMP → PNG）",
                _try_shot(part, tmp, env), "见临时目录 shots/")
    rep.note(f"真机段耗时 {time.time() - t0:.0f}s；临时产物：{tmp}"
             "（跑完会自动释放并清理，--keep-temp 可保留）")


def _try_shot(part: Path, tmp: Path, env: dict) -> bool:
    rc, _ = run([PY, str(HERE / "sw_shot.py"), "--doc", str(part),
                 "--out", str(tmp / "shots"), "--views", "iso"], env=env, timeout=600)
    pngs = list((tmp / "shots").glob("*.png")) if (tmp / "shots").is_dir() else []
    return rc == 0 and bool(pngs)


def _release_sw_docs() -> None:
    """关掉真机段留下的文档，释放文件占用。

    不关的话两点坏处：① 临时目录里的 .SLDPRT 被占着删不掉；
    ② 给用户留一个开着野文档的孤儿 SolidWorks 进程。
    """
    try:
        sys.path.insert(0, str(HERE))
        import sw_connect
        sw = sw_connect.SwSession.open(visible=True)
        sw.close_doc(save=False)
    except Exception:  # noqa: BLE001  释放失败不影响体检结论
        pass


def sweep_old_temps(prefix: str = "swdoctor_") -> int:
    """扫掉上一次体检遗留的临时目录（锁已释放的才删得掉），返回清扫数量。"""
    n = 0
    for cand in Path(tempfile.gettempdir()).glob(prefix + "*"):
        if not cand.is_dir():
            continue
        shutil.rmtree(cand, ignore_errors=True)
        if not cand.exists():
            n += 1
    return n



def sec_drawing(rep: Report) -> None:
    """[7] 图纸读取 / OCR 依赖 —— ★ 关键是**两种 APPDATA 状态都探一次**。

    ★★★ 2026-10-03 踩的坑（坑表 §104①）：本机 `pymupdf` / `rapidocr` /
      `onnxruntime` / `PIL` / `numpy` / `cv2` **全都装好了**，但它们在
      **用户级 site-packages**，而 `find_spec` 默认**看不到**那一层
      ⇒ 一次误判「本机没有任何 PDF/OCR 库」，还去**手工解 PDF 流**
      （ASCII85 / Flate / `/Length` 间接引用），白绕很久。

    ⇒ 所以这一节**必须两个状态各探一次**：
      ① 当前环境（可能因为没设 `APPDATA` 而看不到）
      ② 把 `APPDATA` 指向 `%APPDATA%` 之后再看一次（= 项目记忆里
         COM 脚本必做的同一件事）
    ⇒ 只要②能 import，就报「装好了，只是 `APPDATA` 没指对」。
    """
    rep.section("[7] 图纸读取 / OCR 依赖")
    mods = ["pymupdf", "rapidocr_onnxruntime", "onnxruntime",
            "PIL", "numpy", "cv2"]

    # ★ 裸探一次（当前 sys.path），再走 `has_module`（会自动补用户级
    #   site-packages）—— **两次结果的差异本身就是最有价值的信息**：
    #   它直接告诉用户「库装好了，只是路径没指对」。
    raw = {}
    for m in mods:
        try:
            raw[m] = importlib.util.find_spec(m) is not None
        except Exception:                               # noqa: BLE001
            raw[m] = False
    for m in mods:
        if raw[m]:
            rep.add(m, True, "已装（当前环境直接可见）")
            continue
        ok = has_module(m)
        if ok:
            rep.add(m, True,
                    "★ 已装，但只在**用户级 site-packages** —— "
                    "本函数已补进 `sys.path`（坑表 §104①；"
                    "裸 `find_spec` 看不到 ⇒ 很容易误判成「没装」）")
        else:
            rep.add(m, None,
                    "未装（不阻塞：SolidWorks 建模不依赖它，"
                    "只有读图纸/OCR 才要）")

    # ---- 图纸读不读得动：拿一张真实练习题 PDF 试 ----
    try:
        import pymupdf                                   # noqa: PLC0415
    except Exception:                                   # noqa: BLE001
        rep.add("读练习题 PDF", None,
                "没装 pymupdf ⇒ `sw_exercise.py` 用不了（pip install pymupdf）")
        return
    root = os.environ.get("YOYO_EXERCISE_DIR") or \
        r"C://Users//OMEN//Downloads//d//PracticeProblems_ALL"
    pdfs = glob.glob(os.path.join(root, "**", "*.pdf"), recursive=True)
    if not pdfs:
        rep.add("读练习题 PDF", None,
                f"没找到练习题 PDF（设 YOYO_EXERCISE_DIR 指向目录）")
        return
    n_ok = n_vol = 0
    # ★★ 直接复用 `sw_exercise` 的正则常量，**不在这里重写**。
    #   两条理由（都是本轮真踩的）：
    #   ① heredoc 会把正则里的反斜杠吃掉一层（`\s` → `/s`）—— 我第一版
    #      写成 `Volume://s*...//s*mm3`，单文件能中、批量全不中；
    #   ② 改用 POSIX 字符类 `[[:space:]]` **更糟**：Python 的 `re` 不支持它
    #      （那是别的引擎的语法），它被当成**嵌套集合** ⇒ 有 FutureWarning
    #      且永远不匹配 ⇒ 报「读到目标体积 0」。
    #   ⇒ 正确做法：**单一来源**，需要什么就 import 什么。
    try:
        from sw_exercise import RE_VOLUME as pat           # noqa: PLC0415
    except Exception:                                       # noqa: BLE001
        pat = None
        rep.add("复用 sw_exercise 的正则", None,
                "import sw_exercise 失败 ⇒ 跳过体积真值自检")
    if pat is not None:
        rep.add("复用 sw_exercise 的正则", True,
                "单一来源，避免正则被复制到第二处后走样")
    for f in pdfs[:40]:
        if pat is None:
            break
        try:
            doc = pymupdf.open(f)
            if doc.page_count == 0:                      # ★ 坏文件会抛 IndexError
                doc.close()
                continue
            t = doc[0].get_text()
            doc.close()
        except Exception:                               # noqa: BLE001
            continue
        n_ok += 1
        if pat.search(t):
            n_vol += 1
    if pat is not None:
        rep.add("读练习题 PDF", n_ok > 0,
                f"抽 {min(40, len(pdfs))} 份：可读 {n_ok}，"
                f"其中**读到目标体积** {n_vol}"
                f"（★ 判据链首端的外部真值，坑表 §104②）")

# ------------------------------------------------------------------ 入口


def main() -> None:
    ap = argparse.ArgumentParser(description="SolidWorks 技能体检查（一条命令给结论）")
    ap.add_argument("--real", action="store_true", help="追加真机端到端（会启动 SolidWorks）")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--keep-temp", action="store_true",
                    help="保留临时目录（默认跑完自动清，避免反复体检堆积垃圾）")
    a = ap.parse_args()

    swept = sweep_old_temps()
    rep = Report(as_json=a.json)
    if swept and not a.json:
        print(f"（清扫了上次遗留的 {swept} 个临时目录）")
    if not a.json:
        print("=" * 62)
        print(" SolidWorks 建模技能 · 体检")
        print("=" * 62)

    sec_interpreter(rep)
    info = sec_env(rep)
    sec_offline(rep)
    sec_gb(rep)
    sec_reducer(rep)
    sec_templates(rep)
    sec_drawing(rep)
    if a.real:
        sec_real(rep, info)
    else:
        rep.section("[6] 真机端到端")
        rep.add("真机端到端", None, "未请求（加 --real 才跑）")

    if not a.keep_temp:
        if a.real:
            _release_sw_docs()
        leftover = []
        # SolidWorks 放掉文件句柄有一两秒延迟，删不掉就等一下再试
        for _d in rep.tmpdirs:
            for _ in range(6):
                shutil.rmtree(_d, ignore_errors=True)
                if not _d.exists():
                    break
                time.sleep(1.0)
            if _d.exists():
                leftover.append(str(_d))
        rep.tmpdirs = []
        if leftover:
            rep.note("有临时目录没删掉（仍被 SolidWorks 的 ~$ 锁文件占着；下次体检开头会自动清扫，也可手动删）："
                     + "，".join(leftover))

    total = len(rep.rows)
    if a.json:
        print(json.dumps({"passed": rep.passed, "failed": len(rep.failed),
                          "skipped": rep.skipped, "rows": rep.rows,
                          "notes": rep.notes,
                          "out_dir": str(_out_dir())}, ensure_ascii=False, indent=2))
    else:
        print("\n" + "=" * 62)
        if rep.failed:
            print(f" 结论：不可用 —— {len(rep.failed)} 项失败（共 {total} 项）")
            for r in rep.failed:
                print(f"   · {r['name']}：{r['detail']}")
        else:
            extra = f"，跳过 {rep.skipped} 项" if rep.skipped else ""
            print(f" 结论：可用 —— {rep.passed}/{total} 通过{extra}")
            if rep.skipped:
                print(" （跳过的是「没装 SolidWorks/pywin32」之类的环境项，不是缺陷）")
        print("=" * 62)

    sys.exit(1 if rep.failed else 0)


def _out_dir() -> Path:
    sys.path.insert(0, str(HERE))
    import sw_env
    return sw_env.out_dir()


if __name__ == "__main__":
    main()
