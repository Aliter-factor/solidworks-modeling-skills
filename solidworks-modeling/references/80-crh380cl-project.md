# 80 · CRH380CL 曲面建模项目计划书

> 立项：2026-10-09。状态：**S0~S6 全部完成 + R-A 几何精修（2026-10-12）**（能力建设 + 鼻锥造型 +
> 收口成实体 + 细节/风挡边界 + 交付渲染与建模报告；S5 已真机落到真车头几何 `plan_s5.json`，
> S6 报告已出；**R-A：风挡/侧窗形状升级为真车估计，`plan_s5.json` 45/45**）。
> 本文是**计划书**，不是成果报告 —— 没做到的事一律写"未做"。
> **所有 API 签名都是 2026-10-09 从本机 `C:\SW2024\SOLIDWORKS\sldworks.tlb` 直抽的**
> （`scripts/sw_apisig.py --tlb …`），不是凭记忆写的 —— 这是本技能的第一条纪律（§三步训练法）。

---

## 0. 一句话

**目标**：用本技能（SolidWorks COM 自动化）完成 **CRH380CL（和谐号动车组）车头+车体的曲面建模**，
产出可用的 `.SLDPRT` 实体模型 + 多视图渲染。

**为什么这件事与减速器项目不同**：减速器是**规则回转体 + 平面箱体**，
用的全是拉伸/旋转/切除/孔/阵列（已有的 86 个 op 够用）；
CRH380CL 的灵魂是**流线型自由曲面** —— 本技能目前**曲面能力为 0**，
所以这个项目的**第一件事不是建模，是先把曲面 op 训练出来**。

---

## 1. 目标与范围

| 项 | 内容 |
|---|---|
| 主目标 | CRH380CL 车头（流线型鼻锥）+ 一段车体，建成**单一实体**（或曲面+加厚） |
| 交付物 | `.SLDPRT` 模型；4 视图渲染（`sw_shot`）；建模报告（判据 + 截图） |
| 明确**不做**（本阶段） | 转向架/受电弓/车门内饰/涂装贴图；整车 16 编组；动力学 |
| 精度口径 | **外形相似 + 关键尺寸可参数化**；不追求工程级复刻（无官方图纸时不可能） |
| 判定基准 | ① 截面站位的宽/高采样对得上设计曲线；② 模型是**单一实体**；③ 体量（体积/表面积）与估算吻合；④ 截图人眼复核 |

> ★ 口径必须先说清：**"像不像"是审美判断，"截面尺寸对不对"才是工程判断。**
> 本项目的判据全部落在**后者**（可量化、可复现），"像不像"只作为最后一关的人眼复核（§五条铁律⑤）。

---

## 2. 已知输入与"未知"（诚实清单）

| 类别 | 状态 |
|---|---|
| 车头外形尺寸（车长/车宽/车高/鼻锥长） | ⚠️ **未定** —— 无官方图纸。需用户提供图纸/照片，或按公开资料**声明为近似值**并写进 `params` |
| 截面控制曲线 | ⚠️ 未定 —— 由 S1 阶段按"参数化截面族"生成（见 §4） |
| 车体断面（等截面段） | ⚠️ 未定 —— 同上 |
| 车头分面拓扑（几块曲面 + 怎么过渡） | ✅ **已定**（见 §4，不依赖尺寸） |
| 曲面 API 签名 | ✅ **已定**（见 §3，已从 tlb 抽全） |
| SolidWorks 环境 | ✅ 本机 SW2024 可用，已有 86 op 全部真机双证 |

> ★★★ **纪律（来自本技能最贵的教训）**：**尺寸没定就先建，一定返工。**
> 顺序永远是 **图纸/数据 → 参数表 → 模型**（见 `30-pitfalls.md` §118）。
> ⇒ S0/S1 阶段（能力 + 单曲面片）**可以不等尺寸**；S2 起（车头造型）**必须先落 `params.json`**，
> 并且它必须是**唯一数据源**（`gb_standard.json` 那条"只存一份"的纪律同样适用）。

---

## 3. 曲面能力清单（真签名 · 2026-10-09 抽自 sldworks.tlb）

### 3.1 建面（造出曲面片）

| 能力 | 真 API | 参数 | 用途 / 关键坑 |
|---|---|---|---|
| **拉伸曲面** | `IModelDoc2.FeatureExtruRefSurface` | **15 参**：`Sd, Flip, Dir, T1, T2, D1, D2, Dchk1, Dchk2, Ddir1, Ddir2, Dang1, Dang2, OffsetReverse1, OffsetReverse2` | 单方向/双向、可带拔模（`Dchk/Dang`）。★ 15 个位置参数，**必须逐位对齐**（§三步训练法：不要靠记忆） |
| **旋转曲面** | `IModelDoc2.InsertRevolvedRefSurface` | **4 参**：`Angle, ReverseDir, Angle2, RevType` | 回转类外形（鼻锥若走回转体） |
| **放样曲面** ★主力 | `IModelDoc2.InsertLoftRefSurface2` | **6 参**：`Closed, KeepTangency, ForceNonRational, TessToleranceFactor, StartMatchingType, EndMatchingType` | **车头造型的主力**（多截面渐变）。★ `StartMatchingType/EndMatchingType` 用来**对齐截面起点** —— 起点没对齐会**扭曲**（不是报错） |
| **扫描曲面** | `IModelDoc2.InsertSweepRefSurface2` | **6 参**：`Propagate, TwistCtrlOption, KeepTangency, ForceNonRational, StartMatchingType, EndMatchingType` | 沿路径扫掠（已有实体版 `swept_boss` 的经验可复用：**轮廓面必须垂直于路径**） |
| **平面区域** | `IModelDoc2.InsertPlanarRefSurface` | **0 参**（**预选**边界） | 封口用（**S4 已落地**）。★★★ **必须选 `EDGE`（边），不能选曲线特征** —— 选特征会**静默失败**（§133①） |
| **填充曲面** | `IFeatureManager.InsertFillSurface2` | **6 参**：`Resolutions, Options, VPatchBoundaries, VCurvatureControlType, VFaces, VConstraintCurves` | 补破洞；可加曲率约束 |
| **直纹曲面** | `IFeatureManager.InsertRuledSurfaceFromEdge2` | **11 参**：`Type, Length, FlipPullDir, FlipDir, TrimAndSew, Angle, CoordInput, X, Y, Z, BConnectSurface` | 从边生成（拔模/垂直/相切型）。★ `TrimAndSew` 可**顺手缝上** |
| **等距曲面** | `IModelDoc2.InsertOffsetSurface` | **2 参**：`Thickness, Reverse` | 从已有面偏移 |
| **自由形** | `IFeatureManager.InsertFreeform` | **6 参**：`Continuity0..3, Symmetric0, Symmetric1` + `SetFreeformCurveData/SetFreeformBoundaryContinuity` 配套 | 手动捏形（**精度不可控，本项目只当兜底**） |
| 中面 | `IModelDoc2.InsertMidSurfaceExt` | `Placement, KnitFlag` | 本项目不用 |

### 3.2 修面（把曲面控制在想要的边界内）

| 能力 | 真 API | 参数 | 关键坑 |
|---|---|---|---|
| **剪裁曲面** ★ | `IFeatureManager.PreTrimSurface` → `PostTrimSurface` | `PreTrimSurface(4)`: `BMutualTrimIn, BSplitSystemIn, BSplitLinearIn, BRemovePickedIn`；`PostTrimSurface(1)`: `BSewSurfaceIn` | 老式的 `IModelDoc2.InsertCutSurface(Flip, KeepPieceIndex)`（2 参）也在，但**保留哪一片靠序号**，不可靠 ⇒ **优先走 `Pre/Post` 成对调用** |
| **延展曲面** | `IModelDoc2.InsertExtendSurface` | **3 参**：`ExtendLinear, EndCondition, Distance` | 补边界的缺口 |
| 取消剪裁 | `IFeatureManager.InsertUntrimSurface2` | **5 参**：`FaceUntrimType, EdgeUntrimType, Distance, BMerge, BTrimOppositeSide` | 剪错了回退 |

### 3.3 收口（曲面 → 实体）

| 能力 | 真 API | 参数 | 关键坑 |
|---|---|---|---|
| **缝合曲面** ★ | `IFeatureManager.InsertSewRefSurface` | **5 参**：`UseGapFilters, TryToFormSolid, MergeEntities, KnitTolerance, MaxValueForGapRange` | ★★ `TryToFormSolid=True` ⇒ **缝成实体**（本项目的收口方式）—— ★★★ 但**必须同时 `UseGapFilters=True`**，否则**只缝成曲面**（`GetType()=1`、0 自由边，很像成了，§134①）；`KnitTolerance` 是缝合容差 —— **太大=把不该连的连上，太小=缝不上**，必须显式给并记录 |
| **加厚** ★ | `IFeatureManager.FeatureBossThicken` | **7 参**：`Thickness, Direction, FaceIndex, FillVolume, Merge, UseFeatScope, UseAutoSelect` | ★★ 加厚是**最脆的一步**：曲面局部曲率半径 < 厚度 ⇒ 失败（自交）。**对策见 §6** |
| 加厚（切） | `IFeatureManager.FeatureCutThicken` | **6 参**（少一个 `Merge`） | 从实体内挖 |
| 替代面 | `IModelDoc2.InsertFeatureReplaceFace` | 0 参（预选） | 曲面替换实体面 |

### 3.4 曲线与草图（造型的"骨架"）

| 能力 | 真 API | 参数 | 为什么需要 |
|---|---|---|---|
| **3D 草图** ★ | `ISketchManager.Insert3DSketch(UpdateEditRebuild)` | 1 参 | 车头的引导线天然是**空间曲线**，平面草图做不到 |
| **通过 XYZ 点的曲线** ★★ | `InsertCurveFileBegin()` → `InsertCurveFilePoint(X,Y,Z)`×N → `InsertCurveFileEnd()` | 逐点喂 | **本项目最关键的一个能力**：可以**由参数方程直接生成**流线（左/右/上/下四条轮廓线 + N 条截面线）—— 比手工草图可控 100 倍 |
| 组合曲线 | `IModelDoc2.InsertCompositeCurve` | 0 参（预选） | 把多段拼成一条引导线 |
| 分割线 | `IModelDoc2.InsertSplitLineProject(IsDirectional, FlipDir)` | 2 参（预选 草图+面） | **在鼻锥曲面上画风挡/车窗边界**，再据此剪裁 |
| 投影曲线 | ⚠️ **tlb 里没找到独立 API**（`InsertProjectedCurve` / `SketchProject` 均无） | — | ⇒ **改用 `InsertCurveFilePoint` 直接生成 + `InsertSplitLineProject`**（见 §6 风险 R4） |
| 边界曲面 | ⚠️ **tlb 里没有 `InsertBoundarySurface` / `InsertBoundaryBoss`** | — | ⇒ **放样曲面（`InsertLoftRefSurface2`）是主力**；边界曲面这条路**暂列为"未确认"**，不写进承诺（§6 风险 R5） |

> ★ **本节的意义**：把"能不能做"从"猜"变成"查过 tlb"。上面每一行都有出处，
> 重抽一次的命令：
> ```bash
> export APPDATA="C:\\Users\\OMEN\\AppData\\Roaming"
> python scripts/sw_apisig.py --tlb IModelDoc2.InsertLoftRefSurface2 IFeatureManager.InsertSewRefSurface
> python scripts/sw_apisig.py --tlb-find Surface Loft Sew Thicken
> ```

---

## 4. 建模策略

### 4.1 分面（拓扑先定，尺寸后补）

车头 + 车体分成 **5 块曲面**（这是**不依赖尺寸**的结构设计）：

```
        ┌────────── 车体（等截面，拉伸曲面即可）──────────┐
   侧面 ─┤                                              ├─ 侧面
        └──── 侧墙过渡（放样）──┬── 车顶弧面（放样）──┬────┘
                                │                      │
                          前风挡（填充/放样）      鼻锥（放样，多截面渐变）
                                └────────┬─────────────┘
```

| # | 曲面块 | 建法 | 关键 |
|---|---|---|---|
| 1 | **鼻锥** | 多截面放样（`InsertLoftRefSurface2`） | 截面从鼻端小椭圆 → 逐站放大到车体断面；**截面数 ≥ 8 才不"棱"** |
| 2 | **车顶弧面** | 放样 / 直纹 | 与侧墙的过渡边要 **G1 连续**（放样时 `KeepTangency`） |
| 3 | **侧墙过渡** | 放样 | 由圆润渐变到平直 |
| 4 | **前风挡** | 填充曲面（`InsertFillSurface2`）或局部放样 | 边界由分割线给出 |
| 5 | **车体** | 拉伸曲面（`FeatureExtruRefSurface`） | 等截面，最简单 |

### 4.2 ★ 参数化截面族（本方案的核心）

**不手工画草图**，而是用**参数方程**生成截面 —— 这是能"改一个数全套重放"的前提：

1. 定义**一条母截面** `profile(t)`：`t∈[0,1]` 绕断面走一圈，给出 `(y, z)`（x = 车长方向）；
2. 定义**沿车长的调制函数** `shape(u)`：`u∈[0,1]`，控制"每个站位相对母截面的缩放/压扁/圆润度"；
3. 第 `i` 个站位的截面 = `profile(t) ⊙ shape(u_i)`（逐点算出 y,z）；
4. 用 `InsertCurveFileBegin/Point/End` 把每条截面线**直接喂给 SolidWorks**；
5. 引导线（左/右/上/下轮廓）同样由 `t=0/0.25/0.5/0.75` 处的点集生成。

**为什么必须这样**：
- 放样要求**所有截面的图元数一致、起点对齐** —— 参数化天然同构（每站都是同样的 N 个点、起点都在同一侧）；
- 手工画 N 条截面，起点一定对不齐 ⇒ 放样出来是**扭的**，而且**不报错**（`StartMatchingType` 也救不回来）；
- 改尺寸 = 改 `shape(u)` 的参数 ⇒ **全套截面自动重放**（与减速器 `reducer.params.json` 同一条纪律）。

---

## 5. 分阶段路线与验收判据

| 阶段 | 内容 | 验收判据（**可量化**） | 状态 |
|---|---|---|---|
| **S0 能力建设** | 曲面 op：`surface_extrude` / `surface_revolve` / `surface_knit` / `thicken`（+ 探针计划 + 真机双证） | 每个 op：特征树新增 + `GetErrorCode=0` + **几何量对得上手算**（拉伸曲面看面积/包围盒；加厚看**体积**）+ 截图复核 | ✅ **2026-10-09 完成** |
| **S1 曲线骨架** | `curve_xyz`（通过 XYZ 点的曲线）+ `sketch3d_begin/end`；生成 4 条轮廓线 + 8 条截面线 | 曲线数对得上；每条曲线的**端点坐标**与参数方程逐点吻合（<0.01mm） | ✅ **2026-10-09 完成**（偏差 0.0mm） |
| **S2 单曲面片** | `surface_loft`（放样曲面）：拿 3 条截面 + 2 条引导线做一段 | 生成的面是**单一曲面 body**；截几个位置量宽高，与设计曲线差 <0.5mm | ✅ **2026-10-11 完成**（3 截面 + 2 引导线，贴合偏差 **0.0009mm**） |
| **S3 车头造型** | 落 `crh380cl.params.json`（唯一数据源）→ 全截面放样 | 站位宽高采样全过；**曲面不自交**（用 `sw_faces` dump 面参数核对） | ✅ **2026-10-11 完成**（9 截面 + 2 引导线，贴合偏差 **0.0062mm**，盒 [4500,3265,3890] 逐位对） |
| **S4 收口** | `surface_knit`（缝合成实体）→ `thicken`（若要壳） | 结果必须是**单一实体**（body 数 = 1，不是多片曲面） | ✅ **2026-10-11 完成**（两条路线真机双证：**knit 实体 19.1142 m³ / 壳 0.10184 m³**；新增 op `surface_planar` 封盖） |
| **S5 细节** | 分割线 + 剪裁做风挡/车窗边界 | 剪裁掉的部分与预期吻合（去料类 ⇒ **铁律② 量体积**） | ✅ **2026-10-11 完成（真机双证）**：分割线 ✅（新 op `split_line`，探针 12/12）+ **剪裁换路达成** ✅（原 `PreTrimSurface` 废 → 「`thicken` 壳 + `extrude_cut` 挖穿」，探针 `shell_window_cut` **23/23**）→ **落到真车头** `plan_s5.json` **44/44**：侧窗 Δ=−275788（预算 −283901）、前风挡 Δ=−4582903（预算 −4702874），均差 **2.6~2.9%** |
| **S6 交付** | 渲染 4 视图 + 报告 | 4 张图 md5 互不相同（§125）；报告含判据表 + 截图 | ✅ **2026-10-11 完成**（5 视图 md5 互不相同 + 自包含 HTML 报告；交付渲染新增**自动裁空白**） |
| **R-A 几何精修** | 风挡/侧窗形状按真车参考照升级 | 去料 Δ 仍在预算容差内 + 轮廓自检（有向面积>0、预算>0） | ✅ **2026-10-12 完成**：风挡 `矩形`→**上宽下窄圆角梯形**、侧窗 `Ø240圆`→**后掠斜四边形**，改走 `polyline`+`extrude_cut`；`plan_s5.json` **45/45**；探针 `poly_window_cut` **21/21** |

### ★ S0 完成记录（2026-10-09）

| op | 探针 | 硬判据（实测 vs 手算） |
|---|---|---|
| `surface_extrude` | `surface_basics` | 60×40 矩形拉 40 ⇒ 方管 **8000mm²**（周长 200×40），4 个平面 |
| `surface_revolve` | `surface_revolve` | 矩形绕竖轴 360° ⇒ **5000π = 15707.9633mm²**，盒 60×40×60 |
| `thicken` | `surface_basics` / `surface_revolve` | 3mm：side1 **25440** / side2 **22560** / both **48000** mm³ |
| `surface_knit` | `surface_knit` | 两个共边平面 ⇒ 体 **2→1**，面积 3600mm² |

★★★ **S0 逼出来的两个"静默失败"命门（必读，见 `30-pitfalls.md` §129）**：
① `thicken` / `surface_knit` 的选择**必须 `mark=1`**（官方明文；传 0/4 会
**不报错、返回 None、特征树不涨** —— 为此扫了 23 个无效变体才定位到）；
② 多目标选择**必须 `append=True`**（否则只有最后一个留在选择集里，症状与①一样）。

★ 新增判据 op：**`check_body`**（曲面体数 / 实体数 / **总面积**）。
曲面**没有体积** —— `check_mass` 对它只会回 0、`check_bbox` 连曲面和实体都分不出来
⇒ **曲面每一步一律过它**。

### ★ S1 完成记录（2026-10-09，晚）

| op | 探针 | 硬判据（实测） |
|---|---|---|
| `curve_xyz`（通过 XYZ 点的曲线） | `curve_xyz` | 13 点参数化空间曲线（`40cos t, 25sin t, 10t/π`）**读回 13 点、最大偏差 0.0mm**；类型 `CurveInFile` |
| `curve_xyz`（`closed=True`） | `curve_xyz` | 3 点 ⇒ **读回 4 点**，首尾重合 `start = end = (0,0,0)` |
| `check_curve`（新判据 op） | `curve_xyz` | 逐点比对（**绝对 mm 容差**）+ 点数 + 端点 + 闭合性 |
| `sketch3d_begin` / `sketch3d_end` | `sketch3d` | 3D 草图 2 条直线，**端点读回 (0,0,0)→(0,0,50)、(0,0,50)→(50,0,50)** ⇒ **z 生效** |
| `spline`（3D） | `sketch3d` | 5 点 3D 样条 ⇒ 图元 **1 个 SPLINE**（修好了漏 `variant_r8()` 的**假绿**） |

★★★ **S1 逼出来的四条"读法/平台"坑（必读，见 `30-pitfalls.md` §130）**：
① 曲线读回：类型名 **`CurveInFile`**、`GetPointCount()` = **坐标个数 = 3×点数**、
`PointArray` = **平铺 tuple**（单位米）—— 写错任一条都会把"读法不对"误判成"曲线不对"；
② 活动草图名必须**裸取** `GetActiveSketch2().Name`（套 `typed(...,'IFeature')` 会拿到 **16 个浮点的变换矩阵**）；
③ 3D 草图树类型名 = **`3DProfileFeature`**；`Insert3DSketch` 官方只承诺『新建 **或** 关闭活动草图』
⇒ **不能**用来编辑已有 3D 草图（同机两种行为都实测到过）；
④ **`verified=True` 会漂**：`op_spline` 一直挂着 `verified=True` 却漏了 `variant_r8()`
（样条静默消失）—— 因为**从来没有任何探针点名过它**。判据是"**有没有点名它的探针**"，不是"当初验过"。

★ 新增判据 op：**`check_curve`**（点数 / 端点 / 逐点 / 闭合性，绝对 mm 容差）；
`check_sketch` 也补了 `expect_segments` 与 `dump_points`（读回直线段端点全局 mm，3D 草图的硬判据）。

### ★ S2 能力记录（2026-10-10）—— `surface_loft` 真机双证（**项目步骤未做**）

| op | 探针 | 硬判据（实测 vs 手算） |
|---|---|---|
| `surface_loft`（放样曲面，车头鼻锥主力） | `surface_loft`（13/13 一次通过） | 两圆截面 r=20@Z0 → r=10@Z50 ⇒ **面积 4805.7127mm²**（手算 30π·√2600 = 4805.712672，**逐位吻合**）、盒 **[40,40,50]** 三轴 delta **0**、**1 曲面体 / 0 实体**（⇒ 曲面放样**不出端盖**） |

★★★ **三个官方约定（写错就静默不出面，见 `15-feature-matrix.md` S3）**：
① 选择标记：截面 **mark=1**、引导线 **2**、中心线 **4**、起止相切矢量 **8 / 32**；
② **选择顺序 = 放样方向**；③ ★ **曲面放样的截面可以是开口的**（实体放样不行）。

★ **为什么这条对项目值钱**：R2 风险（截面起点不对齐 ⇒ 曲面扭曲，**不报错**）的正解是
**参数化截面族**（§4.2）；`surface_loft` 已能一次吃多个截面（本探针 2 个，
`guides`/`centerline` 通道也已留好）⇒ **S2/S3 的骨架可直接往上搭**。

### ★★★ S2 / S3 完成记录（2026-10-11）—— 车头鼻锥真机双证

**唯一数据源**：`assets/crh380cl/crh380cl.params.json`
（口径：车体宽 **3265** / 高 **3890** / 地板高 1260 = **中车长客官网**；头车长 26525 = 百度百科；
**鼻锥分段与收窄曲线 = 假设**，无公开三维数据 ⇒ 按 R1 只承诺"形似 + 参数化可调"）。
**生成器**：`scripts/sw_head.py` —— 参数方程 → 截面族 + 引导线 → `yoyo.swplan/1` 计划（改一个数、全套重放）。

| 阶段 | 计划 | 截面 | 硬判据（实测） |
|---|---|---|---|
| **S2** | `plan_s2.json`（11 步） | 3 × 24 点 + 2 引导线 | 单一**曲面体 1 / 实体 0**；盒 x[0,4500] y[±1632.58] z[0,3890]；面积 **33.61 m²**；**参与放样的 3 个截面贴合偏差 0.0009mm** |
| **S3** | `plan_s3.json`（17 步） | 9 × 24 点 + 2 引导线 | 同一曲面；面积 **33.475 m²**；**9 个截面全部贴合 ≤ 0.0062mm**（判据 <0.5mm）；端点断面 = 车体断面（3265×3890）逐位对 |

★ 形状复核：`sw_shot` 四视图（iso/front/right/top）确认 —— 从**小圆**（鼻端 940×1120）
平滑渐变到**大圆角矩形**（车体断面 3265×3890），是标准的流线型鼻锥形态。
★ **量化"截面数够不够"**：3 截面时**未参与站位**的贴合偏差 **31.7mm**；9 截面时收敛到 **0.006mm**
⇒ §4.1「截面数 ≥8 才不'棱'」拿到实测支持。
★★★ **S2 逼出来的两个静默失败**（详见 `30-pitfalls.md` **§132**）：
① **截面 / 引导线共享同一个选择集**，任何一组"清场"会把前面的**全清掉** ⇒ 放样只拿引导线建面，
   出来一张 **y 恒为 0 的退化曲面**（面积 10.46 vs 真值 33.6），而 `check_body` **照样报"1 曲面体"**；
② **引导线太稀**（8 点）⇒ 样条在截面处偏离 ⇒ **特征树不涨、不报错**（加密到 24 点即成功）。
⇒ 两条都已**固化成护栏**（放样前数选择集 + 曲面 bbox 必须包住截面）
+ **新增探针 `assets/probes/surface_loft_guides.json`**（原来那只 `surface_loft` 只测了 `sections`，没测 `guides`）。

### ★★★ S4 完成记录（2026-10-11）—— 曲面**收口成实体**（两条路线真机双证）

**目标**：把开放的放样曲面（S2/S3 已证：**曲面放样不出端盖**）变成**单一实体**。
**新 op**：`surface_planar`（`IModelDoc2.InsertPlanarRefSurface`，0 参，**封盖**）。

| 路线 | 计划 | 硬判据（实测 vs 解析） |
|---|---|---|
| **A. knit 成实体** | `assets/crh380cl/plan_s4_knit.json`（**23 步**）：9 截面 + 2 引导线放样 → `surface_planar` 封 `CAP_0`/`CAP_8`（用 `SEC_0`/`SEC_8` 参考曲线）→ `surface_knit(form_solid=True)` | 3 面 → **1 实体**（sheet 0 / solid 1）；体积 **19.1142 m³** vs 解析 **19.1107 m³**（**+0.019%**）；盒 **[4500, 3265.166, 3890.285]** |
| **B. thicken 成壳** | `assets/crh380cl/plan_s4_shell.json`（**20 步**）：同一曲面 → `thicken(side2, 3mm)` | 体积 **0.10184 m³** vs 解析 **0.09953 m³**（**+2.32%**）；产物 `D:/out/crh380cl_nose_s4_shell.SLDPRT` |

★ 产物：`D:/out/crh380cl_nose_s4_solid.SLDPRT`（实体）/ `…_s4_shell.SLDPRT`（壳）。
★ 生成了 **S4 两分支**：`sw_head.py --stage s4_knit|s4_shell`（`close="knit"|"thicken"`）。

★★★ **S4 逼出来的三条命门（必读，见 `30-pitfalls.md` §133 / §134）**：
① **`surface_planar` 必须选 `EDGE`（边），选曲线**特征**⇒ 静默失败**（API 返回 False、不建特征）；
② **`surface_knit(form_solid=True)` 必须配 `gap_filters=True`**，否则**只缝成曲面**：
   体数 3→1、**0 条自由边**（拓扑闭合，很像成了），只有 `IBody2.GetType()` 说它是曲面（=1）——
   **判据必须问"是实体还是曲面"，不能只问"缝没缝上"**；
③ 放样几何两坑：**通过点曲线多截面无引导线会鼓出**（面积 −6.2%）、
   **两个相同截面放样 ≠ 圆柱**（中段缩到 r/√2）⇒ 简单直筒用**草图真圆**最稳。

★★ **状态机的一次自身修正**：`surface_planar` 的边界可来自**曲线或草图**，`requires` 本该是 OR，
但 `INITIAL_STATE` 里 **`sketch_closed` 开局即为真** ⇒ 拿它当 OR 项 = **假绿**。
⇒ 新增真实状态 **`has_sketch`**（`sketch_end` 置位）+ 给 `requires` **加 OR 组支持**
（`(("has_curve", "has_sketch"),)`；`sw_ops.missing_requires` / `flat_requires` / `req_label`）。
★ 教训：**写 `requires` 前先问"这个状态是不是初值"**。

★ 新探针：`surface_planar`（6 步）、`surface_knit_solid`（19 步，**故意不传 `gap_filters`** 锁默认值）。
★ op 表 **97/97 verified**；`sw_selftest` **4738/0**。

**每个阶段的硬纪律**（照搬减速器项目的血泪）：
1. **判据不落在返回值上** —— 落在特征树 diff、`GetErrorCode`、**几何量**（面参数/包围盒/体积）；
2. **去料/变厚类 op 必须量体积**（`thicken` 算"变厚"；`surface_extend` 算面积变化）；
3. **数值吵不出结论就看图**（`sw_shot` 截图人眼复核）；
4. **加 op 必须同时写反例**（`sw_selftest` 第 5 组）—— 没有反例的校验器会退化成"永远通过"；
5. ★★ **每个 op 必须有一个点名它的探针**（否则 `verified` 会漂成假绿，§130④）。

### ★★★ S5 完成记录（2026-10-11）—— 分割线打通 + 剪裁**换路**达成

| 子项 | op / API | 结果 |
|---|---|---|
| **分割线（S17）** | 新 op **`split_line`**（`IModelDoc2.InsertSplitLineProject`，2 参、返回 void） | ✅ **探针 `assets/probes/split_line.json` 12/12**：z=20 圆平面（7853.98mm²）被一条投影线**劈成 2 个半圆面**（各 3926.73mm²）；体数不变 |
| **剪裁曲面（S12）· 原路** | `IFeatureManager.PreTrimSurface` → `PostTrimSurface` | ⚠️ **废**：7 轮真机对照全无产出（`Pre` 清空选择集，其后拾取全失效）→ §135④ |
| **剪裁 · 换路** ✅ | **`thicken`（成实体壳）→ `extrude_cut`（贯穿挖洞）** | ✅ **探针 `assets/probes/shell_window_cut.json` 23/23（27.91s）**：方管 8000mm² → 加厚 side1 3mm ⇒ **25440mm³** → 贯穿切 r=10 ⇒ 去掉 **2×π·10²·3 = 1884.9556** ⇒ 剩 **23555.0444mm³**（**逐位吻合**） |

★ 新增判据 op **`check_faces`**（数面 + 按面积降序分布）：`check_body` 看体数与总面积，
而**投影分割线不改体数、总面积只差 0.007%** ⇒ 对它是**假绿**；只有面数会 1→2。

★★★ **`split_line` 的三个坑（`30-pitfalls.md` §135）**：
1. **唯一可用组合 = 草图 mark=4 → 面 mark=1**（其余 7 组 mark / 顺序全静默失败：返回 void、特征树不涨）；
2. **草图平面不能与目标面平行**（投影方向与面平行 ⇒ 无交点 ⇒ 静默失败）；
3. 目标面必须用**坐标**给（`_faces` 默认只枚举实体；曲面件用方向串 / all 会落空）。

★★ **剪裁曲面（S12）原路的负面结论**（脚本 `_diag/exp_trim*.py`）：
`PreTrimSurface` 会**清空选择集**，且之后 `SelectByID2` 坐标拾取与 `IModelDoc.MultiSelectByRay`
**全部失效** ⇒ 官方范例顺序（选 tool → Pre → 坐标选被剪区 → Post）在 **SW2024** 下无法复现。
已排除：mark 1/2、0/0、2/1 × keep mark 0/4 × `BSplitSystemIn` T/F × `BRemovePickedIn` T/F ×
tool/target 互序 × 老式 `InsertCutSurface(Flip, KeepPieceIndex)` × keep 前置 × `SolidForTrim=False`。
⇒ **不再续攻该 API**，改走下方换路。

★★★ **换路（S5 判据落地）**：`曲面 → thicken 成实体壳 → extrude_cut 挖洞`
（脚本 `_diag/exp_window*.py`；探针 `assets/probes/shell_window_cut.json`）。
判据用**体积**（去料类 = 铁律②）而不是面积，且因为**圆柱轴 ∥ 板法向**，
去掉量 = **πr²·t 精确成立**（与曲面曲率无关）⇒ 比"剪掉面积对得上"硬得多。
顺带锁住三条约定（`30-pitfalls.md` §136）：
① 草图平面→全局的**轴映射**必须用 `ModelToSketchTransform` 标定
（`ISketchPoint.X/Y/Z` 是**局部**坐标，三个平面读数一模一样，拿它标定必错）；
② `surface_extrude` 的 **`both_directions` 被忽略**（曲面只朝一侧长）；
③ `check_bbox` 的 **`tol` 是比例不是 mm** ⇒ 一律用 `tol_mm`。
★ 并顺带修掉一个**状态机漏配**：`thicken` 未声明 `effect=("has_feature",)`，
导致「曲面→加厚→切除」链路在**静态校验**就被拦下（§136④）。

★ op 表 **99/99 verified**；`sw_selftest` **4798/0**。

### ★★★ S5 **真机落到鼻锥**（2026-10-11 下半场）—— `plan_s5.json` 44/44

换路不再停留在"方管探针"，而是**落到真车头参数化几何**上（`sw_head.py --stage s5`）。

| 步骤 | 判据 | 真机结果 |
|---|---|---|
| 9 截面放样（+2 引导线） | 曲面体 1 / 总面积 | 面积 **33474857 mm²** —— 与参数曲面数值积分 **33473647** 差 **0.0036%** |
| `thicken` side2 3mm | 实体 1 + 体积 ≈ 面积×t(±8%) | **101842194 mm³**（预算 99529651，**+2.3%**） |
| **侧窗**（Top 面 r=120 圆，沿 Y 双贯穿） | 体积**变化量** Δ ≈ 数值积分预算(±8%) | **Δ=−275788.4**（预算 −283900.6，差 **2.9%**） |
| **前风挡**（Right 面 600×1040 矩形，沿 X 完全贯穿） | 同上(±12%) | **Δ=−4582902.7**（预算 −4702873.5，差 **2.6%**） |
| 终值复核 | `check_mass` | **96983503 mm³** |

★ 两处开口的 Δ 都比预算**小 2.6~2.9%**，**同向** ⇒ 来自「壳是**单侧**加厚（`side2`），
而数值积分模型按 **±t/2 居中**」的建模差异 —— **知道来源**就不会误判成 op 有问题。

★★★ **本阶段新增的三条能力/命门**（详见 `30-pitfalls.md` **§137**）：

1. **新 op `check_volume_delta`**（体积**变化量**判据）：`check_mass` 只比「绝对体积 ± 相对容差」——
   壳体积本身就有 +2.3% 偏差，那 2~5% 的绝对偏差**会把「没切到」和「切对了」一起盖住**。
   ⇒ 去料类 op 必须另判「**这一刀切掉了多少**」。口诀：
   **绝对量**验模型整体、**变化量**验这一步动作有没有发生。
2. **数值积分器的 2× 错**（`sw_head._surf_pt_normal`）：「前向差 ÷ 2h」⇒ ∂r/∂u 少一半，
   全表面面积正好差 2×。★ 修完与真机读回差 **0.0036%**；
   并把「数值 ∫area ≈ 解析 lateral_area」写进 `selfcheck()` 第 ⑥ 条（**离线**就拦下）。
3. **`hide_refs` 之前是假绿**：`IFeature.Visible` **只读**（设值抛 `AttributeError`），
   加上白名单漏了曲线类型名 **`CurveInFile`**，两层都被 `contextlib.suppress` 吞掉 ⇒
   日志打印"已隐藏"而蓝线照样糊满截图。正解 = **按类型全选 + `BlankRefGeom`(曲线) /
   `BlankSketch`(草图)**，判据落在「**藏了几个**」（现报 `{'REFERENCECURVES': 11, 'SKETCH': 2}`）。

★ op 表 **100/100 verified**；`sw_selftest` **4809/0**；探针沿用 `shell_window_cut`（23/23）。

### ★★★ S6 完成记录（2026-10-11）—— 交付：5 视图 + 建模报告

| 交付物 | 路径 | 判据 |
|---|---|---|
| 建模报告 | `D:/out/CRH380CL车头曲面建模报告.html` | **自包含**（判据表 + 5 视图 base64 内嵌）；数字**从真机报告读**，不手抄 |
| 交付视图 | `D:/out/crh380cl_deliver/*.png` | **5 张 md5 互不相同**（§125）：iso / front / right / top / trimetric + 拼版 |
| 实体模型 | `D:/out/crh380cl_nose_s5.SLDPRT` | 单一实体（侧窗 + 前风挡开口） |

★★ **交付渲染新增「自动裁空白」**（`sw_shot.autocrop_content`，`screenshot` op 同步加 `crop`）：
`ViewZoomtofit2` fit 到**视窗**、`SaveBMP` 按**指定宽高**另存 ⇒ 长宽比不一致时模型只占画面 **45%**。
判据落**结构**不落颜色（白底白模 ⇒ 颜色差≈0）：`|∂x|+|∂y|` 阈值定位内容框 + 双守卫
（**盖满不裁 / 两维都没削不裁**）。效果：iso **1400×1050 → 635×695**。

★★★ **S6 逼出来的两条「工具自身的坑」**（详见 `30-pitfalls.md` **§138**）：
1. **集成版守卫极性写反**，而独立试验脚本却"成功"—— 因为那是**另一个实现**，没有那道守卫
   ⇒ 教训：**测工具函数必须 `import` 被测函数本身**，不要复制"长得像的"；
2. **`_build/api.json` 被 `sw_ops.py --schema` 占地** ⇒ 签名库全废，
   且 `sw_docbuild` 的增量合并会**把字符串当接口**、报错指不到根因
   ⇒ 加三道篱笆（两处**形状守卫** + `--schema` **拒绝写 `api.json`**）。

★ op 表 **100/100 verified**；`sw_selftest` **4818/0**；新探针 `assets/probes/shot_crop.json`（4/4）。

### ★★★ R-A 几何精修（2026-10-12，v1.4.57）—— 把"占位开口"做成真车形状

用户提供 4 张真车参考照（正脸 / 站台 3-4 / 侧视 ×2）。**照片只给形状与比例、不给绝对尺寸**
（无正交视图、无标尺、有透视）。据此做 R-A：

| 项 | 旧（占位） | 新（R-A，照片依据的形状估计） | 实现 |
|---|---|---|---|
| 前风挡 | 600×1040 **矩形** | **上宽下窄圆角梯形**（`z 1520~2140`、`y 420~560`、`r 85`） | Right 面 `polyline`（44 点闭合折线），沿 X 完全贯穿 |
| 驾驶室侧窗 | `Ø240` **圆** | **后掠斜四边形翼窗**（`x 1050~1330`、前高后低、`r 40`） | Top 面 `polyline`（44 点），沿 Y 双贯穿 |
| 鼻锥轮廓 | 收窄指数 `1.60/1.30` | 按侧视照微调 **`1.52/1.35`** | `half_width`/`half_height` 幂律 |

`polyline` + `round_poly`（**离线**圆角折线近似）取代 `rect_center`/`circle` —— **不用 `sketch_fillet`**
（对选中集敏感，§102 家族）。

★ S5-R A 真机结果（`report_s5_RA.json`，**45/45，0 warning**）：

| 步骤 | 判据 | 真机结果 |
|---|---|---|
| 9 截面放样 | 曲面体 1 / 总面积 | **33479875.5 mm²**（离线积分 33473647，+0.019%） |
| `thicken` side2 3mm | 实体 1 + 体积 ≈ 面积×t | **102446667.9 mm³**（预算 99528004.6，+2.9%） |
| **翼窗**（Top 面斜四边形，沿 Y 双贯穿） | 体积**变化量** Δ ≈ 预算 | **Δ=−397161.3**（预算 −406001.4，**−2.2%**） |
| **前风挡**（Right 面圆角梯形，沿 X 完全贯穿） | 同上 | **Δ=−4179121.9**（预算 −4362130.7，**−4.2%**） |
| 终值复核 | `check_mass` | **97870384.7 mm³（97.87 kg）** |

★ 两处开口 Δ 仍**同向偏低**（−2.2% / −4.2%），来源仍是「单侧加厚 vs ±t/2 居中」；
风挡更大是因为它落在**曲率更大的前上段**，两口径的差被曲率放大。
★ 风挡 cut 用「备选1 单向完全贯穿+反向」（**主方案 SolidWorks 返回 None** ⇒ 记 `variant`）。
★ 全套 stage 在改过鼻锥参数后**重跑复核**：S2 11/11、S3 17/17、S4_knit 23/23（19.19495 m³）、S4_shell 绿。

★★★ **R-A 逼出来的两条「判据自身的坑」**（详见 `30-pitfalls.md` **§139**）：
1. **积分器掩膜轴序写反 ⇒ 去除量预算恒 0**（`(p[1],p[0])` 当 `(p[0],p[1])`）⇒ 配 `expect=0` 的
   Δ 判据 ⇒ **"没切"也算绿**。两道离线篱笆：**⑦ 轮廓有向面积 > 0**、**⑧ 去除量预算 > 0**
   （已注入 bug 复现红灯验证）。
2. **`polyline`+`extrude_cut` 是新组合** ⇒ 先跑探针 `poly_window_cut.json`（**21/21**，Δ −1581.15
   vs 解析 −1581.1561，**0.0002%**）；且新探针首跑在 `thicken` 报选不中 ⇒ **原样重跑 21/21**
   （会话退化伪装成计划 bug，§122/§128 家族）。

★ 交付视图重渲染到 `D:/out/crh380cl_deliver_RA/`（5 视图 + 拼版，md5 互不相同）；
报告生成器 `sw_head_report.py` 同步升级（窗户形状 + R-A 说明块 + 新判据说明）。
★ `sw_selftest` 新增 **`[2z]` 车头开口轮廓/预算自检**（7 断言）：`poly_area` 符号 /
`round_poly` 不翻向 / 真实 s5 计划离线自检全过 / **注入「预算=0」与「轮廓反向」两种 bug 必须报红**。
⇒ `sw_selftest` **4847/0**（v1.4.57）。

---

## 6. 风险与对策

| # | 风险 | 后果 | 对策 |
|---|---|---|---|
| **R1** | **尺寸来源不可靠**（无官方图纸） | 建得很漂亮但**不是 CRH380CL** | ① 尺寸来源必须**显式登记**（图纸 / 公开资料 / 用户口述），写进 `params.json` 的 `authority` 字段；② 没有可靠来源时，**只交付"形似 + 参数化可调"**，报告里写清口径 |
| **R2** | **放样截面起点不对齐 ⇒ 曲面扭曲**（**不报错**） | 模型看着"怪"，但所有返回值都正常 | 用**参数化截面族**（§4.2）从根上消灭；再加一道判据：**逐站量宽高**（扭曲会让某个站的宽高反常） |
| **R3** | **加厚失败**（局部曲率半径 < 厚度） | `FeatureBossThicken` 报错或静默无产出 | ① 厚度先取小（如 2mm）、**优先加厚在"曲率最小处"**；② 失败就**先把曲率大的区域剪掉/分块加厚**；③ 实在不行**交付曲面（不转实体）** —— 但必须在报告里写明"未转实体" |
| **R4** | **投影曲线在 tlb 里没有独立 API** | "在曲面上投影一条曲线"这条常规路线不可用 | 改用 `InsertCurveFilePoint` **直接生成空间曲线**（反正我们是参数化生成的，本来就有 3D 坐标）+ `InsertSplitLineProject` 做面内分区 |
| **R5** | **边界曲面 API 未确认** | 若造型方案依赖它会卡住 | 已把主力换成**放样曲面**（签名已确认）。边界曲面只列为"待查"，**不写进承诺** |
| **R6** | **缝合容差选错**（`KnitTolerance`） | 太大=连错、太小=缝不上，**都不报"容差"这类明确错误** | 容差显式给值并**随计划记录**；缝合后**必须数 body 数**（`TryToFormSolid=True` 时 body 应=1） |
| **R7** | **STL/网格面数爆炸** | 后续渲染/仿真卡死 | 曲面阶次与截面点数**可控**（参数化天然可控）；导出前查面数 |
| **R8** | 会话污染（装配层已踩过） | 编辑动作静默失效 | 干涉检查一类的"只读但会占命令"的调用**排在最后**（§128①）；长任务 `save_as` 前置 + `--salvage-out`（§122） |

---

## 7. 与现有能力的复用关系

**能直接复用**（省掉从 0 开始）：
- `sw_plan` / `sw_ops` 的计划-执行框架、`sw_probe` 探针机制、`sw_selftest` 回归；
- `sw_faces` / `sw_faceset` / `sw_prism`：**面参数 dump**（`PlaneParams` / `CylinderParams`）—— 曲面的判据要落在这些上；
- `sw_shot` + `sw_inspect`：截图 + 读数**双证**；
- 坑表里**所有"返回值说谎"的教训**（§19~23、§120、§128）—— 曲面 API 只会更多，不会更少；
- `dim_auto` / `fully_define` 的**草图约束**经验 —— 3D 草图同样会欠约束。

**不能复用、必须新建**：
- 曲面特征 op（S 族）；
- **通过 XYZ 点生成曲线**的工具链（参数方程 → 点列 → SW 曲线）；
- 曲面的判据（"面是什么类型 + 连续性 + body 数"）—— 现有判据全是实体口径（体积/包围盒）。

---

## 8. 下一步

**S0 已完成（2026-10-09）**：4 个曲面 op 真机双证 + 判据 op `check_body` +
3 个探针全绿 + 坑表 §129 记下两个静默失败命门。

**S1 已完成（2026-10-09 晚）**：`curve_xyz` / `check_curve` / `sketch3d_begin` / `sketch3d_end`
四个 op 真机双证（**95/95 op 全真机验证**）+ 探针 `curve_xyz` / `sketch3d` 全绿 +
坑表 §130 记下四条"读法/平台"坑；顺带修掉 `op_spline` 漏 `variant_r8()` 的**假绿**。

**S2 / S3 已完成（2026-10-11）**：落 `crh380cl.params.json`（唯一数据源：车体尺寸取中车官网口径，
鼻锥分段为假设）+ 写生成器 `scripts/sw_head.py`（参数方程 → 截面族 + 引导线 → swplan）。
`plan_s2.json`（3 截面 + 2 引导线）与 `plan_s3.json`（9 截面 + 2 引导线）**真机双证**：
单一曲面体（sheet 1 / solid 0）、盒 **[4500, 3265, 3890]**、截面贴合偏差 **≤ 0.0062mm**、
四视图截图复核。顺带抓出 `surface_loft` 引导线的**两个静默失败**（`30-pitfalls.md` §132）
并固化成护栏 + 新增探针 `surface_loft_guides`。

**S4 已完成（2026-10-11）**：新增 op `surface_planar`（封盖）+ 两条收口路线真机双证 ——
`plan_s4_knit.json`（封盖 + `surface_knit(form_solid=True)` ⇒ **1 实体 19.1142 m³**）与
`plan_s4_shell.json`（`thicken` side2 3mm ⇒ **0.10184 m³**）。
顺带抓出**三条命门**（`30-pitfalls.md` **§133/§134**）：`surface_planar` 必须选 **EDGE**；
`form_solid=True` 必须配 **`gap_filters=True`**（否则静默只缝成面）；
放样"鼓出 / 两个相同截面≠圆柱"两坑。并**修正了状态机自身**：
新增状态 `has_sketch` + 给 `requires` 加 **OR 组支持**（`sketch_closed` 是初值 ⇒ 假绿）。

**S5 已完成（2026-10-11）**：分割线（op `split_line`，探针 12/12）+ **剪裁换路达成**
（`thicken` → `extrude_cut`，探针 `shell_window_cut` 23/23，去掉量 πr²t 逐位吻合）。
原 `PreTrimSurface` 路线经 7 轮真机确认作废（§135④）；顺带修正状态机 `thicken` 漏声明
`effect` 的漏配（§136④），并把「草图轴映射标定法 / `both_directions` 被忽略 /
`check_bbox` 的 tol 是比例」三条约定固化进新探针（§136②③）。

下一步按顺序：

1. ✅ **S5 已真机落到鼻锥**（2026-10-11）：`sw_head.py --stage s5` → `plan_s5.json` **44/44**，
   侧窗 + 前风挡两处开口，体积**变化量**与数值积分预算差 **2.6~2.9%**。
   ★ 风挡/侧窗的位置与尺寸**全部是 `assumed`**（无公开三维数据），已写进
   `crh380cl.params.json` 的 `windows` 块并逐字段标 `_source`（R1 口径）。
2. ✅ **S6 交付已完成**（2026-10-11）：5 视图（md5 互不相同，§125）+ 自包含建模报告；
   交付渲染新增**自动裁空白**（§138①）；顺手修掉 `_build/api.json` 被占地事故（§138②）。
3. 其余 S 族按 `15-feature-matrix.md` 的优先级推进（**延展曲面 S13** 尚未做）。
4. 本文档随阶段推进更新（**每阶段完成就改"状态"列**，不留"以为做了"的空间）。
