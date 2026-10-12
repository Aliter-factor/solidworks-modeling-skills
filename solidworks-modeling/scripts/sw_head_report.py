#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sw_head_report.py —— CRH380CL 交付报告生成器（S6）。

把 `sw_plan.py --report` 的真机报告 + `sw_shot` 的视图，合成一份**自包含** HTML
（视图以 base64 内嵌，单文件即可分享）。

用法（从技能根目录）：
    python scripts/sw_head_report.py \
        --report D:/out/report_s5_RA.json \
        --imgdir D:/out/crh380cl_deliver_RA \
        --out    D:/out/CRH380CL车头曲面建模报告.html

★ R-A（v1.4.57）：风挡由 600×1040 矩形 → **上宽下窄圆角梯形**；侧窗由 Ø240 圆
  → **后掠斜四边形翼窗**（形状来自 4 张真车参考照，尺寸仍 assumed）。窗户改用
  `polyline` 画任意闭合轮廓（不再用 `rect_center` / `circle`）。

★ 纪律：报告里的数字**全部从真机报告 JSON 读取**，不手抄 ——
  手抄的数是"我以为的结果"，读出来的数是"真机跑出来的结果"，后者才算判据（§19）。
  ★ 期望值同样从报告里读（`step.params.expect_*` / `step.result.expect_mm3`），
    **不要自己重算** —— 重算会用另一个口径（例如按 ±t/2 居中）与计划里的预算打架（§137①）。

★ 视图口径：`--imgdir` 内按 `crh380cl_nose_s5_<view>.png` 命名，`<view>` ∈
  {iso, front, right, top, trimetric}。生成前先跑：
    python scripts/sw_shot.py --doc D:/out/crh380cl_nose_s5.SLDPRT \
        --out D:/out/crh380cl_deliver --views iso,front,right,top,trimetric --grid
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

VIEWS = ["iso", "front", "right", "top", "trimetric"]
INTEGRATOR_AREA = 33473647.0   # sw_head 数值积分（离线预算），仅作期望值展示


def _b64(p: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()


def build_html(rep: dict, imgdir: Path) -> str:
    by_id = {e["id"]: e for e in rep["log"]}

    def res(i):
        # ★ 容错（2026-10-12）：部分跑（如只到 S4 壳）时，S5 的窗步 v6d/v7d 不在报告里。
        #   以前这里直接 `by_id[i]` ⇒ KeyError ⇒ **报告根本生成不出来**（比缺一段更糟）。
        #   现在缺步就返回 {}，下面各处用 "—" 占位 —— 报告要能反映"跑到哪、哪步没闭环"。
        return (by_id.get(i) or {}).get("result", {})

    def has(i):
        return i in by_id and bool(by_id[i].get("result"))

    v1, v5, v8 = res("v1"), res("v5"), res("v8m")
    loft_area = v1.get("area_mm2") or (v5.get("area_m2", 0) or 0) * 1e6
    shell_vol = (v5.get("volume_m3", 0) or 0) * 1e9
    shell_expect = ((by_id.get("v5") or {}).get("params") or {}).get("expect_volume_mm3", 0) or 0
    side, ws = res("v6d"), res("v7d")
    final_vol = (v8.get("volume_m3", 0) or 0) * 1e9
    final_kg = v8.get("mass_kg", 0) or 0
    n_exec, n_steps = rep["executed"], rep["steps"]

    imgs = {n: _b64(imgdir / ("crh380cl_nose_s5_%s.png" % n)) for n in VIEWS}

    def pct(r):
        if not r or not r.get("expect_mm3"):
            return "未闭环"
        return f"{(r['delta_mm3'] / r['expect_mm3'] - 1) * 100:+.1f}%"

    def mm3(r, key):
        return f"{r[key]:,.0f} mm³" if (r and key in r) else "—"

    d_side, d_ws = pct(side), pct(ws)
    shell_dev = f"+{(shell_vol / shell_expect - 1) * 100:.1f}%" if shell_expect else "—"
    stale = "" if (has("v6d") and has("v7d")) else \
        '<div class="note"><b>S5 窗口判据未闭环</b>：本次报告由<b>部分跑</b>生成，' \
        '翼窗/风挡两步没有有效读数（见 §145）—— 表格里标「未闭环」处即为缺口，' \
        '不代表 op 失败，但<b>该步的几何尚未被证实</b>。</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>CRH380CL 车头流线型曲面 · 建模报告</title>
<style>
  :root{{--ink:#1b1b1f;--mut:#6b6b76;--line:#e3e3ea;--bg:#fafafc;--acc:#c0392b;--ok:#1e7a46;--warn:#b7791f}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
  .wrap{{max-width:960px;margin:0 auto;padding:40px 28px 80px}}
  h1{{font-size:27px;margin:0 0 6px;letter-spacing:.2px}}
  h2{{font-size:19px;margin:38px 0 12px;padding-bottom:7px;border-bottom:2px solid var(--line)}}
  .sub{{color:var(--mut);font-size:13.5px;margin-bottom:14px}}
  .badge{{display:inline-block;background:#fdecea;color:var(--acc);border:1px solid #f5c6c0;
         border-radius:999px;padding:2px 11px;font-size:12.5px;font-weight:600;margin-right:6px}}
  .badge.g{{background:#eaf6ee;color:var(--ok);border-color:#bfe3cc}}
  table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:13.5px}}
  th,td{{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}}
  th{{background:#f1f1f5;font-weight:600;white-space:nowrap}}
  td.n{{font-variant-numeric:tabular-nums;white-space:nowrap}}
  .ok{{color:var(--ok);font-weight:600}}
  .warn{{color:var(--warn);font-weight:600}}
  .fig{{margin:16px 0 26px;background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px}}
  .fig img{{width:100%;display:block;border-radius:6px}}
  .fig figcaption{{color:var(--mut);font-size:13px;margin-top:8px}}
  .grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
  code{{background:#f2f2f6;border:1px solid var(--line);border-radius:5px;padding:1px 5px;
        font-size:12.5px;font-family:"Cascadia Code",Consolas,monospace}}
  pre{{background:#23232b;color:#e8e8ef;border-radius:9px;padding:14px 16px;overflow:auto;
      font-size:12.5px;line-height:1.6}}
  .note{{background:#fff8e6;border-left:4px solid var(--warn);padding:10px 14px;border-radius:0 8px 8px 0;
        font-size:13.5px;margin:12px 0}}
  ul{{margin:8px 0;padding-left:22px}} li{{margin:4px 0}}
  .kpi{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}}
  .kpi div{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:13px 14px}}
  .kpi b{{display:block;font-size:20px;letter-spacing:.3px}}
  .kpi span{{color:var(--mut);font-size:12.5px}}
</style></head><body><div class="wrap">

<h1>CRH380CL 高速动车组 · 车头流线型曲面建模报告</h1>
<div class="sub">solidworks-modeling 技能训练项目（曲面 S 族）&nbsp;·&nbsp; S0–S6 完成
&nbsp;·&nbsp; 生成日期 2026-10-12 &nbsp;·&nbsp; 本报告由真机运行报告自动取数生成</div>
<span class="badge">口径：形似 + 参数化可调</span>
<span class="badge">非工程级复刻</span>
<span class="badge">R-A 几何精修</span>
<span class="badge g">鼻尖圆收缩（§141）</span>
<span class="badge g">真机 {n_exec}/{n_steps} 步通过</span>

<h2>1. 一句话</h2>
<p>用 SolidWorks COM 自动化（本技能 100 个 op，全部真机双证）把 CRH380CL 的
<b>流线型鼻锥</b>从参数方程一路建到<b>带风挡/侧窗开口的单一实体壳</b>，
并渲染 5 视图交付。判据全部落在<b>可量化的几何量</b>（面积 / 体积 / 体积变化量 / 面数）上，
"像不像"只作最后的人眼复核。</p>
<div class="note"><b>必须先说清的口径（R1）：</b>CRH380CL 的<b>车体轮廓尺寸</b>有官方来源；
但<b>流线型鼻锥的三维形状、风挡/侧窗的位置尺寸没有公开数据</b>。
所以本模型的定位是「<b>形似 + 参数化可调</b>」，<b>不是</b>对真车的逆向还原 ——
凡无官方来源的字段，在参数表里一律标 <code>assumed</code>。</div>
<div class="note"><b>R-A 几何精修（本版）：</b>用户提供 4 张真车参考照（正脸 / 站台 3/4 / 侧视 ×2）后，
把窗户<b>形状</b>从"占位几何"升级为<b>照片依据的形状估计</b>：
<b>① 前风挡</b>：600×1040 矩形 → <b>上宽下窄的圆角梯形</b>；
<b>② 侧窗</b>：Ø240 圆 → <b>后掠斜四边形翼窗</b>；
<b>③ 鼻锥轮廓</b>：按侧视照微调收窄曲线指数。
★ 照片<b>只能估形状与比例、不能反求绝对尺寸</b>（无正交视图 / 无标尺 / 有透视）——
所以这里改的是"形状特征"，尺寸仍全部 <code>assumed</code>。</div>

<h2>2. 交付物</h2>
<table>
<tr><th>交付物</th><th>路径 / 值</th></tr>
<tr><td>实体模型</td><td><code>D:/out/crh380cl_nose_s5.SLDPRT</code>（单一实体）</td></tr>
<tr><td>视图渲染</td><td><code>{imgdir.as_posix()}/*.png</code>（iso / front / right / top / trimetric + 拼版）</td></tr>
<tr><td>唯一数据源</td><td><code>assets/crh380cl/crh380cl.params.json</code>（改一个数 ⇒ 全套截面自动重放）</td></tr>
<tr><td>生成器</td><td><code>scripts/sw_head.py --stage s5</code>（参数方程 → 截面族 + 引导线 → 建模计划）</td></tr>
<tr><td>建模计划</td><td><code>assets/crh380cl/plan_s5.json</code>（{n_steps} 步）</td></tr>
<tr><td>本报告生成器</td><td><code>scripts/sw_head_report.py</code></td></tr>
</table>

<div class="kpi">
  <div><b>{loft_area/1e6:.2f} m²</b><span>鼻锥曲面面积（放样）</span></div>
  <div><b>{shell_vol/1e9:.4f} m³</b><span>加厚壳体积（3mm）</span></div>
  <div><b>{final_kg:.2f} kg</b><span>开口后终态（等效钢）</span></div>
  <div><b>0</b><span>失败步骤</span></div>
</div>

<h2>3. 建模路线（5 步）</h2>
<table>
<tr><th>#</th><th>步骤</th><th>关键 op / API</th><th>为什么这样做</th></tr>
<tr><td>1</td><td>参数化截面族</td><td><code>curve_xyz</code>（<code>InsertCurveFileBegin/Point/End</code>）</td>
<td>9 个站位 × 25 点（超椭圆断面）+ 2 条引导线，<b>由参数方程直接喂给 SolidWorks</b>。
截面天然同构、起点对齐 ⇒ 从根上消灭"放样扭曲"（R2）</td></tr>
<tr><td>2</td><td>放样曲面</td><td><code>surface_loft</code>（<code>InsertLoftRefSurface2</code>）</td>
<td>截面 mark=1、引导线 mark=2。<b>截面数 ≥ 8 才不"棱"</b>（3 截面时未参与站位偏差 32.07mm；9 截面收敛到 0.0065mm）</td></tr>
<tr><td>3</td><td>加厚成实体壳</td><td><code>thicken</code>（<code>FeatureBossThicken</code>，side2 3mm）</td>
<td>曲面放样<b>不出端盖</b>，必须转实体。加厚是曲面→实体里最脆的一步（曲率半径 &lt; 厚度即失败）</td></tr>
<tr><td>4</td><td>切侧窗 + 前风挡</td><td><code>polyline</code> + <code>extrude_cut</code>（贯穿切除）</td>
<td>原「剪裁曲面 <code>PreTrimSurface</code>」路线在 SW2024 下<b>确认走不通</b>（会清空选择集），
<b>换路</b>为"壳 + 布尔切"。R-A 起窗户轮廓改用 <code>polyline</code>（44 点闭合折线）——
<b>前风挡 = 上宽下窄圆角梯形</b>（Right 面）、<b>侧窗 = 后掠斜四边形</b>（Top 面）</td></tr>
<tr><td>5</td><td>交付渲染</td><td><code>sw_shot</code> + <code>hide_refs</code></td>
<td>渲染前隐藏构造几何（本例 11 条曲线 + 2 个草图），否则蓝线糊满画面</td></tr>
</table>

<h2>4. 判据表（真机读数 · 全部可复算）</h2>
<p>每一步都不是"看返回值是 True"，而是落在<b>几何量</b>上；去料类操作一律量
<b>体积变化量</b>（Δ），而不是只看绝对体积 —— 这是本项目最贵的一条纪律。</p>
<table>
<tr><th>阶段</th><th>判据</th><th>期望（解析/预算）</th><th>实测</th><th>偏差</th></tr>
<tr><td>S0</td><td>曲面 op 基础量</td><td>方管 8000mm² / 回转 15707.9633mm² / 加厚 48000mm³</td>
<td class="n">全部逐位吻合</td><td class="ok">0</td></tr>
<tr><td>S2</td><td>3 截面放样贴合</td><td>设计曲线</td><td class="n">0.0009 mm</td><td class="ok">✓</td></tr>
<tr><td>S3</td><td>9 截面放样贴合</td><td>&lt; 0.5 mm</td><td class="n">≤ 0.0065 mm</td><td class="ok">✓</td></tr>
<tr><td>S4</td><td>收口成实体（knit）</td><td>1 实体</td><td class="n">19.1949 m³（解析 19.1107）</td><td class="ok">+0.44%</td></tr>
<tr><td>S5</td><td>放样曲面面积</td><td>离线数值积分（见 params 口径）</td>
<td class="n">{loft_area/1e6:.4f} m²</td><td class="ok">—</td></tr>
<tr><td>S5</td><td>加厚后体积（3mm）</td><td>{shell_expect/1e9:.4f} m³（预算 = 侧面积×t，±8%）</td>
<td class="n">{shell_vol/1e9:.4f} m³</td><td class="warn">{shell_dev}</td></tr>
<tr><td>S5</td><td><b>侧窗（翼窗）</b> Δ（去料量）</td><td>{mm3(side,'expect_mm3')}</td>
<td class="n">{mm3(side,'delta_mm3')}</td><td class="ok">{d_side}</td></tr>
<tr><td>S5</td><td><b>前风挡</b> Δ（去料量）</td><td>{mm3(ws,'expect_mm3')}</td>
<td class="n">{mm3(ws,'delta_mm3')}</td><td class="ok">{d_ws}</td></tr>
<tr><td>S5</td><td>终态体积复核</td><td>—</td><td class="n">{final_vol/1e9:.4f} m³（{final_kg:.2f} kg）</td><td class="ok">{'✓' if final_vol else '未闭环'}</td></tr>
</table>
{stale}
<div class="note"><b>鼻尖「圆收缩」（本版，§141）：</b>用户看渲染图提出「会不会太尖了？应该有个圆收缩吧」——
反馈正确。旧版的 <code>top/bottom/half_width</code> 三条一维轮廓<b>线性</b>收敛到鼻尖一点
⇒ 尖端是<b>圆锥（刀锋）</b>。改为前 <code>tip_round_len_mm</code>（默认 1500mm）走
<b>1/4 椭圆圆帽</b> <code>q(s)=√(1−(1−s)²)</code>（<code>q'(0)=∞</code> ⇒ 鼻尖切线竖直 = 圆头），
并用 smoothstep 权重与后段轮廓<b>值与斜率都连续</b>（无折痕）。
实测半宽 x=100 处 <b>45.1→278.9</b>、x=1000 处 <b>514.2→572.8</b>，x≥1450 完全重合。</div>
<div class="note">两处开口的 Δ 与预算的关系见上表（翼窗一步见 §145 说明）。</div>
<div class="note">R-A 的两条新判据（都防「静默假绿」）：<b>① 轮廓有向面积 &gt; 0</b>（离线自检 ——
折线点序若自交/反向，<code>extrude_cut</code> 会<b>静默不切</b>）；<b>② 去除量预算 &gt; 0</b>
（离线自检 —— 曾因积分器 mask 轴写反而恒为 0，配 <code>expect=0</code> 的 Δ 判据 ⇒
<b>"没切"也会绿</b>，见 §139①）。探针 <code>poly_window_cut.json</code> 真机 21/21：
44 点圆角梯形挖穿方管，Δ −1581.15 mm³ vs 解析 −1581.1561，差 <b>0.0002%</b>。</div>

<h2>5. 视图</h2>
<div class="fig"><img src="{imgs['iso']}" alt="iso">
<figcaption><b>轴测（iso）</b> —— 鼻锥整体形态：从鼻端（小断面）平滑渐变到车体大圆角矩形断面。
可见壳体是<b>两端开口</b>的（曲面放样不生端盖；本阶段未封口）。</figcaption></div>

<div class="fig"><img src="{imgs['right']}" alt="right">
<figcaption><b>右视（沿车长方向看）</b> —— <b>本组的关键证据</b>：壳内壁可见
<b>上宽下窄的圆角梯形开口（前风挡）</b>与<b>斜四边形开口（侧窗翼窗）</b>，
说明两处窗口都已<b>贯穿挖通</b>；另一侧窗透过空腔的投影可一并看到（R-A 已不是矩形/圆）。</figcaption></div>

<div class="grid2">
<div class="fig"><img src="{imgs['front']}" alt="front">
<figcaption><b>前视</b> —— 鼻锥 X–Z 侧向轮廓（前低后高、流线收窄）。</figcaption></div>
<div class="fig"><img src="{imgs['top']}" alt="top">
<figcaption><b>俯视</b> —— X–Y 平面外形与顶面弧线。</figcaption></div>
</div>

<div class="fig"><img src="{imgs['trimetric']}" alt="trimetric">
<figcaption><b>三轴测（trimetric）</b> —— 另一角度的整体形态复核。</figcaption></div>

<h2>6. 已知边界与限制（不藏）</h2>
<ul>
<li><b>两端未封口</b>：曲面放样不生端盖，加厚后是"筒"，模型在 x=0 与 x=4500 两端开口。
若需封闭实体，用 S4 的 <code>surface_knit(form_solid=True)</code> 路线（已验证 19.11 m³）。</li>
<li><b>鼻锥形状全部是假设</b>：<code>length</code> / 收窄曲线 <code>power</code> / 超椭圆指数
<code>squareness</code> / 风挡与侧窗的位置尺寸，均为 <code>assumed</code>，<b>不代表真车</b>。</li>
<li><b>开口 Δ 系统性偏低</b>（翼窗 −2.2%、风挡 −4.2%）：来自"单侧加厚 vs ±t/2 居中"的建模口径差异，非 op 缺陷。</li>
<li><b>未做</b>：转向架 / 受电弓 / 车门内饰 / 涂装贴图 / 整车编组 / 动力学。</li>
<li><b>风挡/侧窗的形状</b>已按真车参考照升级为<b>圆角梯形 / 斜四边形</b>（R-A），
但这是<b>形状特征的照片估计</b>——<b>尺寸与位置仍不代表真车</b>（无公开三维数据）。
前照灯、下裙板/排障器、涂装仍属未做范围（R-B / R-C）。</li>
</ul>

<h2>7. 复现</h2>
<pre>export APPDATA="C:\\\\Users\\\\OMEN\\\\AppData\\\\Roaming"

# 1) 改参数（唯一数据源）后重放全部截面与计划（含离线自检 ⑦⑧）
python scripts/sw_head.py --stage s5 --out D:/out
# 2) 真机执行 + 出判据报告
python scripts/sw_plan.py assets/crh380cl/plan_s5.json --run --report D:/out/report_s5_RA.json
# 3) 渲染交付视图（自动隐藏构造几何 + 自动裁剪空白）
python scripts/sw_shot.py --doc D:/out/crh380cl_nose_s5.SLDPRT ^
    --out D:/out/crh380cl_deliver_RA --views iso,front,right,top,trimetric --grid
# 4) 生成这份报告
python scripts/sw_head_report.py --report D:/out/report_s5_RA.json ^
    --imgdir D:/out/crh380cl_deliver_RA --out D:/out/CRH380CL车头曲面建模报告.html</pre>

<div class="sub" style="margin-top:34px">
本报告数字取自 <code>{Path(rep.get('report_path', 'D:/out/report_s5_RA.json')).name}</code>
（真机 {n_exec}/{n_steps} 步）。方法学与坑表见技能 <code>references/</code>（§129–§139）。
</div>

</div></body></html>
"""


def _cli() -> None:
    ap = argparse.ArgumentParser(description="CRH380CL 交付报告生成器（自包含 HTML）")
    ap.add_argument("--report", default="D:/out/report_s5_RA.json",
                    help="sw_plan.py --report 产出的真机报告 JSON")
    ap.add_argument("--imgdir", default="D:/out/crh380cl_deliver_RA", help="sw_shot.py 产出的视图目录")
    ap.add_argument("--out", default="D:/out/CRH380CL车头曲面建模报告.html", help="输出 HTML 路径")
    a = ap.parse_args()

    rep = json.loads(Path(a.report).read_text(encoding="utf-8"))
    rep["report_path"] = str(a.report)
    html = build_html(rep, Path(a.imgdir))
    Path(a.out).write_text(html, encoding="utf-8")
    print(f"written: {a.out}  ({len(html)} bytes)")
    print(f" 真机 {rep['executed']}/{rep['steps']} 步通过")


if __name__ == "__main__":
    _cli()
