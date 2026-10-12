#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sw_probe.py —— 批量真机探针：一次开 SolidWorks，把 N 个最小计划全跑一遍。

这是 `references/50-training.md` **三步法第 3 步**的落地工具。

    新 op 写完，**不能因为「离线校验通过」就标 verified** —— 必须真机跑通。
    一个 op 一个最小计划（`assets/probes/<op>.json`），跑通后：
        python scripts/sw_shot.py    --doc <产物> --out <目录> --grid    # 肉眼看
        python scripts/sw_inspect.py --doc <产物>                        # 数据看
    双证齐了才把 op 的 `verified` 改成 True，并把踩到的坑写进 30-pitfalls。

为什么要有这个脚本：每个计划单独调一次 `sw_plan.py --run` 会**每次冷启动 SolidWorks**
（10~15 s），10 个计划就是 2 分半纯等待。这里一次会话跑完所有计划，
计划之间只关闭文档、不关程序。

用法
----
    python scripts/sw_probe.py                               # 全部探针（assets/probes/*.json）
    python scripts/sw_probe.py --plans a.json b.json --out D:/out/probe.json
    python scripts/sw_probe.py --glob "assets/probes/sk_*.json"
    python scripts/sw_probe.py --keep-open                   # 跑完不关文档，方便人工看
    python scripts/sw_probe.py --preflight                    # ★ 先验会话健康再跑
    python scripts/sw_probe.py --list                        # 只列出有哪些探针

退出码：0 = 全部通过；1 = 有失败（可直接拿去当 CI 门禁）；2 = 会话不健康、已中止。
"""

from __future__ import annotations

import argparse
import glob as globmod
import json
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sw_connect  # noqa: E402
import sw_env      # noqa: E402
import sw_ops      # noqa: E402
import sw_plan     # noqa: E402

PROBE_DIR = HERE.parent / "assets" / "probes"


# ---------------------------------------------------------------- 单计划

def run_plan_in_session(sw, plan: dict, stop_on_error: bool = True) -> dict:
    """在**已连上的**会话里跑一个计划。返回 {ok, steps, failed, log, seconds}。"""
    steps = plan.get("steps") or []
    # ★ 注入产出目录：探针计划里写 "{{ params.out }}/xxx.SLDPRT" 就能跨机器可移植，
    #   不用把 D:/out 硬编码进 JSON（环境变量 YOYO_SW_OUT 同样生效，见 sw_env.out_dir）。
    params = dict(plan.get("params") or {})
    params.setdefault("out", str(sw_env.out_dir() / "probe"))
    ctx = {"params": params, "vars": {}, "outputs": {},
           "feature_names": [],
           # ★ 构建期文档指纹守卫 —— 与 sw_plan.execute 同一套（见那里的长注释）。
           #   探针套件是**第二条建特征的路**，漏了它就等于"守卫只在一条路上有效"。
           "_doc_pin": None, "_allow_doc_switch": False}
    log: list[dict] = []
    failed = None
    t_all = time.time()

    for i, step in enumerate(steps):
        sid = step.get("id") or f"#{i}"
        entry = {"id": sid, "op": step.get("op")}
        t0 = time.time()
        try:
            sw_plan._check_doc_pin(sw, ctx, sid)                 # noqa: SLF001
            res = sw_ops.run_op(sw, step, ctx)
            if step.get("op") in sw_plan._DOC_PIN_OPS:            # noqa: SLF001
                ctx["_doc_pin"] = sw_plan._doc_pin(sw)           # noqa: SLF001
                entry["doc_pin"] = dict(ctx["_doc_pin"])
            entry["ok"] = True
            entry["seconds"] = round(time.time() - t0, 3)
            entry["result"] = sw_plan._jsonable(res)             # noqa: SLF001
        except Exception as e:                                   # noqa: BLE001
            entry["ok"] = False
            entry["seconds"] = round(time.time() - t0, 3)
            entry["error"] = str(e)
            entry["error_first_line"] = str(e).splitlines()[0]
            if not isinstance(e, sw_connect.SwError):
                entry["traceback"] = traceback.format_exc()
            spec = sw_ops.OPS.get(step.get("op"))
            if spec:
                entry["api_hint"] = list(spec.api)
            failed = entry
        log.append(entry)
        if not entry["ok"] and stop_on_error:
            break

    return {
        "name": (plan.get("meta") or {}).get("name") or "",
        "steps": len(steps),
        "executed": len(log),
        "ok": failed is None and len(log) == len(steps),
        "failed": failed,
        "seconds": round(time.time() - t_all, 2),
        "log": log,
    }


# ---------------------------------------------------------------- 主流程

def load_plans(args) -> list[tuple[str, dict]]:
    paths: list[str] = list(args.plans or [])
    for pat in (args.glob or []):
        paths += sorted(globmod.glob(pat))
    if not paths:
        paths = sorted(str(p) for p in PROBE_DIR.glob("*.json"))
    out = []
    for p in paths:
        pp = Path(p)
        if not pp.exists():
            print(f"  ! 找不到 {p}")
            continue
        out.append((str(pp), json.loads(pp.read_text(encoding="utf-8"))))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="批量真机探针（三步法第 3 步）")
    ap.add_argument("--plans", nargs="*", default=None, help="计划文件列表")
    ap.add_argument("--glob", nargs="*", default=None, help="按通配符找计划")
    ap.add_argument("--list", action="store_true", help="只列出探针，不执行")
    ap.add_argument("--out", default=None, help="结果报告 JSON 路径")
    ap.add_argument("--keep-open", action="store_true", help="跑完不关文档")
    ap.add_argument("--preflight", action="store_true",
                    help="跑之前先做**会话健康度**端到端冒烟；不健康就**直接中止**"
                         "（★ 别让会话故障伪装成 op 缺陷，见 sw_session_check.py）")
    ap.add_argument("--continue-on-error", action="store_true",
                    help="单个计划失败后继续跑下一个（默认也继续，但整体返回 1）")
    a = ap.parse_args()

    plans = load_plans(a)
    if not plans:
        raise SystemExit(f"没有找到任何探针计划（默认目录 {PROBE_DIR}）")

    if a.list:
        print(f"共 {len(plans)} 个探针：")
        for p, plan in plans:
            ops = " → ".join(s.get("op", "?") for s in plan.get("steps", []))
            print(f"  {Path(p).name:34} {(plan.get('meta') or {}).get('name', '')}")
            print(f"      {ops}")
        return

    if a.preflight:
        # ★★ 2026-10-03：SolidWorks 会话会在**跑到一半**时静默失效
        #   （`AddComponent5` 对任何零件回 None，而 NewDocument/GetComponentCount 全正常）。
        #   不先验会话，一次探针失败会让人去改**本来正确的 op**。
        import sw_session_check                              # noqa: PLC0415
        print("== 会话健康度预检 ==")
        _h = sw_session_check.run(tempfile.gettempdir())
        for _i in _h["checks"]:
            print(f"  [{'PASS' if _i['ok'] else 'FAIL'}] {_i['name']}"
                  + (f"  — {_i['detail']}" if _i["detail"] else ""))
            if _i["hint"]:
                print(f"         ★ {_i['hint']}")
        print(f"  结论：{_h['verdict']}（{_h['seconds']}s）")
        if _h["verdict"] != sw_session_check.VERDICT_OK:
            print("\n★★ 会话不健康 ⇒ **已中止，没有跑任何探针**。")
            print("  对策：重启 SolidWorks 再来。★ 不要去改 op —— "
                  "同会话的 NewDocument / GetComponentCount 都还正常，"
                  "坏的只是插组件这一环。")
            return 2
        print("  会话健康，继续。\n")

    sw = sw_connect.SwSession.open(visible=True)
    print(f"已连接 SolidWorks（主版本 {sw.version}，常量来源 {sw.C.source}）")
    print(f"待跑 {len(plans)} 个探针\n")

    results, t0 = [], time.time()

    def _dump_progress() -> None:
        """★ 增量写盘：每跑完一个探针就写一次报告。

        为什么要这样：全量套件有 **85 个探针、约 1 小时**跑不完，
        外部时长上限（真机实测 ~14m49s）会把进程掐掉 —— 旧行为是
        **全部结果一起丢、报告文件根本不存在**（2026-10-09 真机踩过）。
        改成增量后，被中断也能拿到**已跑完那部分**的完整结果，
        并且 `partial=True` 明确告诉你「这份报告不完整、别当全绿用」。
        """
        if not a.out:
            return
        ok = [r for r in results if r["ok"]]
        xf = [r for r in results if r.get("xfail")]
        xp = [r for r in results if r.get("xpass")]
        hard = [r for r in results if not r["ok"] and not r.get("xfail")]
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(
            {"total": len(results), "passed": len(ok),
             "xfailed": len(xf), "xpassed": len(xp), "failed": len(hard),
             "planned": len(plans), "partial": len(results) < len(plans),
             "results": results},
            ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        with sw.suppress_prompts():
            for path, plan in plans:
                tag = Path(path).stem
                print(f"--- {tag}  {(plan.get('meta') or {}).get('name', '')}")
                try:
                    r = run_plan_in_session(sw, plan)
                except Exception as e:                            # noqa: BLE001
                    r = {"name": tag, "ok": False, "steps": 0, "executed": 0,
                         "failed": {"error": f"{type(e).__name__}: {e}"}, "log": []}
                r["file"] = path
                r["tag"] = tag
                # ★ 预期失败（XFAIL）：有些探针**明知现在过不了**（API 在本版本已废、
                #   或者被 SolidWorks 的 COM 层挡住），但必须留在套件里 ——
                #   它是这个边界的**可复现证据**，删掉就变成"我们不知道自己不会这个"。
                #   plan.meta.expect_fail 写一句原因即可；只有**真失败**才让退出码变 1。
                xf = (plan.get("meta") or {}).get("expect_fail")
                if xf:
                    r["expect_fail"] = xf
                    r["xfail"] = not r["ok"]
                    if r["ok"]:
                        r["xpass"] = True
                results.append(r)
                for e in r["log"]:
                    mark = "OK  " if e["ok"] else "FAIL"
                    print(f"      [{e['id']}] {e['op']:<18} {mark} "
                          f"{e['seconds']:>6.2f}s"
                          + ("" if e["ok"] else f"  {e['error_first_line']}"))
                    if e["ok"] and isinstance(e.get("result"), dict):
                        for k in ("variant", "added", "warning", "notes"):
                            if e["result"].get(k):
                                v = e["result"][k]
                                v = v if isinstance(v, str) else json.dumps(
                                    v, ensure_ascii=False)
                                print(f"            · {k}: {v[:160]}")
                print(f"    => {'通过' if r['ok'] else '失败'}"
                      f"（{r['executed']}/{r['steps']} 步，{r.get('seconds', 0)}s）")
                if r.get("xfail"):
                    print(f"    -- 预期失败（XFAIL），不计入失败。已知边界：{r['expect_fail']}")
                elif r.get("xpass"):
                    print("    !! XPASS：这个探针被标成「预期失败」却**通过了** ——"
                          "\n       说明边界被解掉了：请删掉 meta.expect_fail 并把 op 的"
                          " verified 翻成 True。")
                print()
                _dump_progress()          # ★ 增量写盘：被外部时长上限掐掉也不丢结果
                if not a.keep_open:
                    try:
                        sw.close_doc(save=False)
                    except Exception:                             # noqa: BLE001
                        pass
    finally:
        if not a.keep_open:
            try:
                sw.close()
            except Exception:                                     # noqa: BLE001
                pass

    passed = [r for r in results if r["ok"]]
    xfail = [r for r in results if r.get("xfail")]
    xpass = [r for r in results if r.get("xpass")]
    hard = [r for r in results if not r["ok"] and not r.get("xfail")]
    print("=" * 62)
    line = (f"探针 {len(results)} 个：通过 {len(passed)}")
    if xfail:
        line += f"，预期失败 {len(xfail)}"
    line += f"，失败 {len(hard)}，总耗时 {time.time() - t0:.0f}s"
    print(line)
    for r in results:
        if r["ok"]:
            continue
        fl = r.get("failed") or {}
        mark = "--" if r.get("xfail") else "X "
        print(f"  {mark} {r['tag']:<33} {(fl.get('op') or ''):<18} "
              f"{fl.get('error_first_line', fl.get('error', ''))}")
    if xfail:
        print("\n预期失败（已记录边界，不算失败）：")
        for r in xfail:
            print(f"  · {r['tag']}: {r['expect_fail']}")
    if xpass:
        print("\n!! 以下探针标着「预期失败」却通过了 —— 边界已解，请翻 verified：")
        for r in xpass:
            print(f"  · {r['tag']}")

    if a.out:
        _dump_progress()
        if len(results) < len(plans):
            print(f"\n!! 报告已写入 {a.out}（**不完整**：{len(results)}/{len(plans)} —— "
                  f"有探针没跑完，别当全绿用）")
        else:
            print(f"\n报告已写入 {a.out}")

    # 退出码只看**真失败** —— XFAIL 是已记录的边界，不该把 CI 变红
    sys.exit(0 if not hard else 1)


if __name__ == "__main__":
    main()
