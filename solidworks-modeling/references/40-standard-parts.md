# 40 · 标准件公式与尺寸表

所有公式都在 `scripts/sw_gear.py` 里实现，**可以直接调用来核对数字**，不用手算。

## ★★ 标准件尺寸只有一个权威来源：`assets/gb_standard.json`

**不要再上网搜标准件尺寸，也不要凭记忆填。** 搜索出来的是各家互相抄的二手表，
公差、对边宽、头高经常串行；凭记忆填也栽过（M8 的对边宽 13 还是 14、
M3 盘头 `dk` 5.5 还是 5.6 —— 这类"差一点点"最难查）。

| 入口 | 用途 |
|---|---|
| `scripts/sw_gb.py` | 纯函数查询，**不依赖 SolidWorks**，可以随时单独跑来核对 |
| op `lookup_standard` | 在计划里查表，并**自证没抄错**（见下） |
| `references/41-gb-cheatsheet.md` | **全表只读快照**（387 行，含每族每一行规格值）—— 想一次看全就读它 |
| 命令行 | `python sw_gb.py --list` / `--row hex_bolt M8` / `--pick parallel_key 25 shaft_range` / `--fit 20 H7 k6` / `--cheatsheet` |

覆盖 **15 个族**：螺纹螺距（粗/细）+ 攻丝底孔 + 通孔、六角头螺栓/螺母、内六角圆柱头、
开槽盘头/沉头/圆柱头、十字槽盘头、平垫圈、弹簧垫圈、普通平键、圆锥销、圆柱销、
深沟球轴承（6 系列 64 型号）、弹性挡圈、双头螺柱。
另有四张**非族**表：长度系列（GB/T 3106）、齿轮模数（GB/T 1357）、
极限与配合偏差（GB/T 1800.2，单位 **μm**）、螺栓通孔（GB/T 5277）。

```bash
python scripts/sw_gb.py --cheatsheet            # 一次打全表成 Markdown
python scripts/sw_gb.py --write-md references/41-gb-cheatsheet.md   # 重新生成随包快照
```

> ★ 改了 `gb_standard.json` 之后**必须**重新生成 `41-gb-cheatsheet.md`，
> 否则 `sw_selftest.py` 的「速查表快照」断言会失败（它比 `updated=` 字段）。

**每族带 `confidence` 字段**：`high` = 反复核对过；`check` = 常用值没错，
但**冷门规格落刀前请再翻一遍机械设计手册**。这是自报的把握度，别当它是保证。

### 口径（用户 2026-09-25 拍板）

- **比赛零件（一级减速器）= 跟图纸**。图纸没标再查表。
- **用户自己的参照件（`.SLDPRT`）优先于本表** —— 他手上的零件才是"事实"。
- **自主设计的零件 = 跟 GB**。

### 最值钱的用法：让计划自证数字

把查到的值写进 `expect`，对不上就当场抛。比事后拿卡尺量便宜得多：

```json
{"id":"g","op":"lookup_standard","family":"hex_bolt","spec":"M8",
 "expect":{"d":8,"pitch":1.25,"s":13,"k":5.3}}
```

实测例子见探针 `assets/probes/gb_hex_bolt.json`（自建 M8×30 六角头螺栓，**21/21 步全绿**）；
另一支 `assets/probes/gb_tables.json`（**19/19 步全绿**）把 15 族的查询面整个过一遍。

### ⚠️ 本轮不用 SolidWorks Toolbox

用户拍板：**本轮功能开发阶段不用 Toolbox**，标准件自己按本表建，Toolbox 只当冗余。
理由：① Toolbox 依赖本机安装的数据库，换台机器就没了，比赛现场风险不可控；
② 它生成的是配置驱动的零件，脚本改型很别扭；
③ 图纸/参照件和 Toolbox 默认值不一定一致，混用会出现"同一颗 M8 两种对边宽"。

`scripts/sw_toolbox.py` 的入口保留着，但**需要人在计划里显式声明**才算要用。

### 螺纹一律是**真实螺纹线**（去料），不是装饰线

用户拍板：`螺纹：补真实螺纹线`。用 op `real_thread`（特征类型 `SweepThread`）。
装饰螺纹线（`InsertCosmeticThread3`）只在不关心几何、只要工程图标注时才用。

- 螺距取本表 `thread.pitch_coarse`；profile 取 `thread.profile_file`
  （外螺纹 `Metric Die.SLDLFP`、内螺纹 `Metric Tap.SLDLFP`）。
- **判据用 `check_cyl_radius`，不要用 `check_bbox`** —— 螺旋面是 B 样条面，
  包围盒会虚报（详见 30-pitfalls「螺纹三条」）。

## 渐开线圆柱齿轮（直齿 / 斜齿统一按端面换算）

输入：法向模数 `mn`、齿数 `z`、螺旋角 `beta`（0=直齿）、法向压力角 `alpha_n`（默认 20°）、
变位系数 `x`、齿顶高系数 `ha*=1`、顶隙系数 `c*=0.25`。

| 量 | 公式 | 说明 |
|---|---|---|
| 端面模数 | `mt = mn / cosβ` | |
| 端面压力角 | `αt = atan(tanαn / cosβ)` | |
| 分度圆 | `d = mt·z` | 直齿时 = `mn·z` |
| 基圆 | `db = d·cosαt` | |
| 齿顶圆 | `da = d + 2·mn·(ha* + x)` | 注意用 **mn** 不是 mt |
| 齿根圆 | `df = d − 2·mn·(ha* + c* − x)` | |
| 端面分度圆齿厚 | `st = mt·(π/2 + 2x·tanαn)` | |
| 半齿厚角 | `ψp = st / d` | 极角，从齿中心线量起 |
| 渐开线齿廓极角 | `φ(r) = ψp + inv(αt) − inv(αr)`，`αr = acos(rb/r)` | `inv(t)=tan t − t` |
| 齿顶弧齿厚 | `sa = da·(ψp + inv(αt) − inv(αa))` | 太小 → 齿顶变尖 |

**三个自检点**（`sw_selftest.py` 里就是这么验的）：

- `r = d/2`（分度圆）→ `φ = ψp`
- `r = db/2`（基圆）→ `φ` 最大
- `r = da/2`（齿顶圆）→ `φ` 最小

拿 `mn=2, z=55` 验：`d=110`、`da=114`、`df=105`、`db=103.366`、`sa≈1.562`。

### 校验规则（不要跳过）

| 判据 | 公式 | 级别 |
|---|---|---|
| 根切 | `x < ha* − z·sin²αn / 2` | error |
| 不变位最小齿数 | `z_min = 2ha* / sin²αn ≈ 17.1`（α=20°） | |
| 齿顶变尖 | `sa ≤ 0` error；`sa < 0.25·mn` warn | |
| 齿根圆在基圆内 | `df < db` → 齿根段用径向直线补 | info |

`z=12` 的补救办法：加变位。`x ≥ 1 − z·sin²α/2`，`z=12` 时约需 `x ≥ 0.298`。
本技能 `GearParams(mn=2, z=12, x=0.5)` 就不报根切了。

### 齿廓怎么变成 SolidWorks 草图

`GearParams.tooth_sector(k)` 每个齿返回三段：

1. `tooth` — 从齿根起，经右齿廓 → 齿顶弧 → 左齿廓 → 回到齿根，**一条样条**（`CreateSpline3`）
2. `root` — 齿根圆弧，从本齿左齿根连到下一齿右齿根（`Create3PointArc`）
3. `tip` — 齿顶弧中间点（两端点已在 `tooth` 里，不重复）

按 `[tooth_0, root_0, tooth_1, root_1, …]` 拼接就是**整圈一条闭合轮廓**。
这是 `gear_profile` op 干的事。

**取点密度**：`n_flank=8`（单侧）在齿数 55 时约 110 个图元，够用且快。
齿数少（如 z<20）可以加大到 12 保证渐开线形状。

### 坐标约定（改代码前必看）

- 原点 = 轴心，角度以 **+x 轴为 0、逆时针为正**（与 SolidWorks 草图一致）
- 第 k 个齿的中心线方向 = `2πk/z + 90°`
- `_phi()` 收的是**半径**不是直径 —— 传直径是最容易犯的错（曾经错过一次）

## 阶梯轴（旋转体）

`stepped_shaft([{d, l}, ...])` 返回半剖面点（绕 x 轴回转，y = 半径）。
建法：草图里画半剖面 + `centerline` 作轴 → `revolve_boss(angle=360)`。

要点：剖面**必须闭合到轴线**（最后补 `(x,0)` 和 `(0,0)`），
且轮廓不能跨过中心线，否则旋转失败。

## 平键键槽（GB/T 1095）

`keyway_profile(bore, "8x3.5")`：宽 × 毂槽深。常用值：

| 轴径 d | 键宽×键高 | 毂槽深 t2 |
|---|---|---|
| 17–22 | 6×6 | 2.8 |
| 22–30 | 8×7 | 3.3 |
| 30–38 | 10×8 | 3.3 |
| 38–44 | 12×8 | 3.3 |
| 44–50 | 14×9 | 3.8 |

建模：在孔的草图里加一个矩形（宽 = 键宽，从孔壁到 `r + t`），与孔**一起**切除。
`gear_profile` 的 `keyway` 参数已经这么做了。

## 六角头螺栓（GB/T 5782 / GB/T 5783）

`hex_bolt("M8", 65, thread_len=20)`，表内 M6–M30（**以下数字与 `gb_standard.json`
的 `hex_bolt` 族同源**，改一处必须改另一处）：

| 规格 | 对边宽 s | 头高 k | 对角 e | 粗牙螺距 |
|---|---|---|---|---|
| M6 | 10 | 4.0 | 11.05 | 1.0 |
| M8 | 13 | 5.3 | 14.38 | 1.25 |
| M10 | 16 | 6.4 | 17.77 | 1.5 |
| M12 | 18 | 7.5 | 20.03 | 1.75 |
| M16 | 24 | 10.0 | 26.75 | 2.0 |
| M20 | 30 | 12.5 | 33.53 | 2.5 |
| M24 | 36 | 15.0 | 39.98 | 3.0 |
| M30 | 46 | 18.7 | 51.28 | 3.5 |

建模顺序：

1. `polygon(n=6, r = s/2/cos(30°))` — **外接圆半径**不是 s/2！s 是对边宽
2. 拉伸 `k` → 六角头
3. 同一基准面画 `d` 的圆 → 拉伸 `L`
4. 头部两端 30° 倒角（`chamfer`）
5. **真实螺纹线**：`real_thread(standard="metric_die", diameter=d, pitch=粗牙螺距,
   end_condition="blind", depth=螺纹长度)`，`edges` 给杆端**圆边的圆心**。
   收尾用 `check_cyl_radius(expect_max=d/2)` —— **不要用 `check_bbox`**。

完整可跑的例子：`assets/probes/gb_hex_bolt.json`（M8×30，21/21 步真机全绿）。

## 开槽盘头螺钉（GB/T 67）—— 一字槽的草图结构

用户点名的"略复杂"那一个。**照着他自己的零件来**：
参照件 `C:\学习软件\一级减速器\5-螺钉M3x22.SLDPRT`，下面这条链是真机扫出来的
（`sw_inspect --json`），不是猜的：

| 特征 | 类型 | 作用 |
|---|---|---|
| 草图1 | ProfileFeature | 头部半剖轮廓（含 R 半球顶） |
| 旋转2 | Revolution | 旋转成头（`end_cond1=6` = 两侧对称/整圈） |
| 草图9 | ProfileFeature | 一字槽矩形 |
| 切除-拉伸1 | ICE（Instant3D 拉伸切除） | 槽切除，`end_cond1=1` + `reverse=true` = **两侧完全贯穿** |
| 草图13 | ProfileFeature | Ød 圆（杆） |
| 凸台-拉伸3 | ICE | 杆 Ød×22（`depth1_mm=22`） |
| 倒角2 | Chamfer | 杆端/头下倒角 |
| 螺纹线1 | SweepThread | ★ **真实螺纹线**（不是装饰线） |

| 规格 | dk | k | 槽宽 n | 槽深 t | 粗牙螺距 |
|---|---|---|---|---|---|
| M2.5 | 5.0 | 1.7 | 0.6 | 0.6 | 0.45 |
| M3 | 5.6 | 1.8 | 0.8 | 0.7 | 0.5 |
| M4 | 7.0 | 2.4 | 1.0 | 1.0 | 0.7 |
| M5 | 9.5 | 3.0 | 1.2 | 1.2 | 0.8 |
| M6 | 12.0 | 3.6 | 1.6 | 1.6 | 1.0 |
| M8 | 16.0 | 4.8 | 2.0 | 2.0 | 1.25 |

**三个坑**：

1. 槽是**两侧完全贯穿**（`end_cond1=1` + `reverse=true`），参照件里就是这么设的。
   单向拉伸只切半个槽是经典翻车点 —— `FeatureCut4` 会**静默降级成单向**（见 30-pitfalls）。
2. 槽矩形的长边要给足（如 `2×dk`）并居中，否则切不透会在头顶留一层皮。
3. 头部 R 半球顶用 `arc3p`，半径 = `dk/2`。

机器可读的完整步骤在 `gb_standard.json` 的 `templates.slotted_pan_screw`，
用 `lookup_standard(template="slotted_pan_screw")` 取。

## 圆板法兰

`flange(od, bore, thickness, bolt_circle, n_bolts, bolt_d)`，会校验：

- 螺栓孔不超出外缘：`BCD/2 + bolt_d/2 ≤ od/2 − 2`
- 螺栓孔不与内孔干涉：`BCD/2 − bolt_d/2 ≥ bore/2 + 2`

⚠️ `bolt_circle` 是**分布圆直径（BCD）**，比较时记得除以 2（第一版就错在这）。

建模：外圆拉伸 → 内孔切除 → 螺栓圆草图 + `sketch_circular_pattern` → 切除。

## 深沟球轴承（GB/T 276，简化）

`deep_groove_bearing("6204")` → 内径 d / 外径 D / 宽 B。表内 6204–6210。
轴承是标准件，建模只需内外圈 + 滚珠的简化体；装配可只做外轮廓。
本机用户零件库里已有 `24-深沟球轴承-6204.SLDPRT`、`30-深沟球轴承-6206.SLDPRT`。

## 梯形螺纹丝杠（GB/T 5796 近似）

模板 `--template leadscrew`，默认 **Tr24×5**：螺纹段长 200、两端轴头 Ø18×40，总长 280。

牙型不自查表，而是用**「中径处牙槽等宽」**这一条自洽构造 —— 换任何螺距都成立：

| 量 | 公式 | Tr24×5 |
|---|---|---|
| 牙高 | `H = 0.5·P` | 2.5 |
| 牙型角 | 两侧各 `15°`（共 30°） | — |
| 大径 | `d` | 24.0 |
| 小径 | `d₃ = d − 2H = d − P` | 19.0 |
| 大径处槽半宽 | `P/4 + (P/4)·tan15°` | 1.585 |
| 小径处槽半宽 | `P/4 − (P/4)·tan15°` | 0.915 |

牙型截面的 6 个点（径向 `r` × 轴向 `v`），从大径外侧起逆时针闭合：

```
(r_ext, v0 − half_o) → (r_out, v0 − half_o) → (r_in,  v0 − half_i)
→ (r_in, v0 + half_i) → (r_out, v0 + half_o) → (r_ext, v0 + half_o)
```

`r_ext = r_out + 1mm` 是为了保证扫掠体在圆柱面外侧，切得干净。
`v0` 是**螺纹段起点在草图 v 轴上的坐标**（见下）。

### 三条必须知道的对齐规则

1. **牙型草图画在「上视基准面」**（XZ 平面）：草图 `+v` 轴对应全局 **−Z**。
   螺纹段落在 `z ∈ [−Lth/2, +Lth/2]`，所以牙型要画在 `v0 = +Lth/2`。
   **映射画反了 → 扫掠找不到起始截面**，报错指向完全不相关的地方。
2. **螺旋线和牙型都从同一个「螺纹段起点面」出发**，两者天然对齐；
   起点面用 `ref_plane(offset = Lth/2, flip=True)` 从前视面偏出来。
3. **螺纹段毛坯必须用「给定深度」不能用「两侧对称」**：草图已经在 `z = −Lth/2` 上，
   再取中面会让整段跑到 `z ∈ [−Lth, 0]` —— 螺纹段整体偏到轴的一头。

顺序：轴身（两侧对称）→ 起点面 → 圆 Ø24 + 螺旋线 → 毛坯 Ø24×Lth（给定深度）
→ 上视面画牙型 → `cut_swept` → `hide_refs` → `check_bbox` → `save_as`。

`hide_refs` 是**必须的**：不隐藏的话几百圈螺旋线会盖满螺纹，截图里完全看不清牙型。

实测 Tr24×5×200：包围盒 24×24×280，体积 93,828 mm³（手算 ≈93,960，误差 0.14%）。

## 用法示例

```python
import sw_gear
import sw_gb                       # ★ GB 标准件尺寸表（纯函数）
g = sw_gear.GearParams(mn=2, z=55, b=20, bore=20, keyway="6x2.8")
print(g.summary())
for d in g.diagnostics():
    print(d["level"], d["code"], d["msg"])
print("整圈轮廓点数:", len(g.full_outline()))
print(sw_gear.hex_bolt("M8", 65, 20))
print(sw_gear.flange(od=160, bore=60, thickness=18, bolt_circle=0, n_bolts=6))

# 查标准件（不要上网搜、不要凭记忆）
print(sw_gb.pitch("M8"))                          # 1.25
print(sw_gb.tap_drill("M8"))                      # 6.8
print(sw_gb.row("hex_bolt", "M10"))               # 对边宽 16 / 头高 6.4 …
print(sw_gb.pick("parallel_key", 25, "shaft_range"))   # 轴径 25 → 8x7 平键
print(sw_gb.families(), sw_gb.templates())
```

这些函数**不依赖 SolidWorks**，可以随时单独跑来核对数字。
