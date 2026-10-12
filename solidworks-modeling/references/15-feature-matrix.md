# SolidWorks 特征 / 工具 · 全量包装矩阵

> 用户要求："solidworks 所有的特征或工具的包装和工具制作"。
> 本文把 SolidWorks 的特征与工具按 **12 个族**拆全，逐项标：API、关键参数、**现状**、优先级、坑。
> 配套：`10-operations.md`（已落地的 op 详表，自动生成）、`50-training.md`（新增能力的流程）。

**状态图例**
| 记号 | 含义 |
|---|---|
| ✅ | 已有 op，**真机跑通**（`verified: True`） |
| 🔶 | 已有 op，未经真机验证 |
| ⬜ | 待做（P0=本项目必需 / P1=强烈建议 / P2=可缓） |
| ➖ | 本项目（一级减速器）用不到，明确不做 |
| 🔍 | **API 签名待用 `sw_docbuild.py` 从本机 CHM 现场抽取** —— 不凭记忆写 |

> **为什么有 🔍**：SolidWorks API 有 1.4 万个方法、900 多个枚举。凭记忆写签名 = 编造。
> 本机每个 SW 安装都自带完整 API 帮助（`api/HelpViewer`），`sw_docbuild.py` 能抽成结构化 JSON。
> **任何标 🔍 的项，动手第一件事是抽签名，不是写代码。**

---

## ⚠ 勘误：抽签名后纠正的 8 处（2026-09-25）

**这一节是全表最该看的部分。** M1 落地时先跑了
`python scripts/sw_apisig.py --build -i IFeatureManager ISketchManager IModelDoc2 ...`，
结果发现下表里**有 8 个 API 名是我凭印象写的，全是错的**。幸好没照着写代码。

| 我原先写的（错） | 实际存在的（对） | 怎么发现的 |
|---|---|---|
| `IFeatureManager.InsertDraft` | **`InsertMultiFaceDraft`** / `DraftXpertChange` | `--find Draft` 搜不到 `InsertDraft` |
| `IFeatureManager.InsertRib`（当有参用） | `IFeatureManager.InsertRib()` **零参数**，真货在 **`IModelDoc2.InsertRib2`（9 参）** | `--gaps` 报"参数没解析出来"，去 CHM 读原文 |
| `ISketchManager.SketchMirror` | **`IModelDoc2.SketchMirror()`**（零参数命令） | `--iface ISketchManager` 全表里没有 |
| `ISketchManager.CreateStraightSlot` / `CreateArcSlot` | **`CreateSketchSlot`**（一个方法管直槽/弧槽） | 同上 |
| `ISketchManager.SketchLinearPattern` | **`CreateLinearSketchStepAndRepeat`**（圆周版是 `CreateCircularSketchStepAndRepeat`） | 同上 |
| `ISketchManager.SketchChamfer` | **`CreateChamfer`**（草图圆角是 `CreateFillet` ✓） | 同上 |
| `ISketchManager.SketchAddConstraints` | **不在 ISketchManager**（约束挂在 `ISketch` 上，需另抽） | 同上 |
| `IAssemblyDoc.AddMate3` | **已废弃 → `AddMate4`** | `--sig` 的说明字段写着 "Obsolete" |

另外三处**不是错、但实测确认的**：

- `FeatureRevolveCut2` 官方已标废弃 → 用 `FeatureRevolve2`，靠 **`IsCut=True`** 一个开关区分
  凸台/切除（20 个参数，与 `revolve_boss` 完全同一张表）。M1 已按新写法实现 `revolve_cut`。
- `ISldWorks`/`IModelDocExtension` 里**没有 `SelectAllByType`**（CHM 全文搜不到，实测也调不到）。
  → 「按类型批量选实体」这条路在 SW2024 走不通，选实体只能靠**坐标射线拾取**。
- 有一类页面（`Sub` 形式，如 `IPartDoc.SetMaterialPropertyName2`）**签名解析器抓不到参数** ——
  `sw_apisig.py --gaps` 会专门列出来。遇到它们**不要靠猜**，去页面 Remarks 的示例里反推
  （`SetMaterialPropertyName2(ConfigName, Database, Name)` 就是这么定下来的）。

> **教训（已写进硬规则）**：矩阵里凡是标 🔍 的，**抽完签名再动手**；
> 勘误这一节本身就是「不凭记忆写 API」这条规则的价值证明 —— 一次抽出 8 个错。

---

## A. 文件与文档（10）

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 坑 |
|---:|---|---|---|:---:|:---:|---|
| A1 | 新建零件 | `ISldWorks.NewDocument` | 模板路径、单位 mm | ✅ `new_part` | — | 模板名各机不同，留空用默认 |
| A2 | 打开文档 | `ISldWorks.OpenDoc6` | 路径、类型、静默、只读 | ✅ `open` | — | 类型猜错要重试；`~$` 锁文件要跳过 |
| A3 | 保存/另存 | `IModelDocExtension.SaveAs` | 路径、覆盖 | ✅ `save_as` | — | **返回值骗人**：目标被占用时"成功但不落盘"→ 校验 `GetPathName` + mtime + 实体数 |
| A4 | 关闭文档 | `ISldWorks.CloseDoc` | 标题 | ✅ `close` | — | 关的是**活动文档**，先核对路径再关 |
| A5 | 强制重建 | `IModelDoc2.EditRebuild3` | — | ✅ `rebuild` | — | 建完特征必须重建一次再量 |
| A6 | 缩放全屏 | `IModelDoc2.ViewZoomtofit2` | — | ✅ `zoom_fit` | — | 截图前必调，否则可能拍到空白 |
| A7 | **新建装配体** | `ISldWorks.NewDocument`(ASM) | 模板 | ⬜ | **P0** | M3 起点 |
| A8 | **新建工程图** | `ISldWorks.NewDocument`(DRW) | 模板、图幅 | ⬜ | **P0** | M4 起点 |
| A9 | **导出 STEP / DXF / PDF** | `IModelDocExtension.SaveAs` 带格式 | 格式枚举、选项 | ⬜ | **P1** | `swSaveAsVersion_e` 已有；PDF 需 `swExportDataFileType_e` |
| A10 | 打包 Pack&Go | `ISldWorks.PackAndGo` | 目标目录 | ⬜ | P2 | 交付用，可选 |
| A11 | 输入 STEP/IGES | `ISldWorks.LoadFile4` | 路径、导入选项 | ⬜ | P2 | 复用外部模型时用 |

---

## B. 草图（26）

`ISketchManager` 是主战场。**所有坐标单位在这里是米**，输入必须 `× MM`。

### B1 画图元（11）

| # | 工具 | API | 关键参数 | 现状 | 优先级 |
|---:|---|---|---|:---:|:---:|
| B1 | 直线 | `CreateLine` | x1,y1,z1,x2,y2,z2 | ✅ `line` | — |
| B2 | 中心线 | `CreateCenterLine` | 同上 | ✅ `centerline` | — |
| B3 | 圆（圆心+半径） | `CreateCircleByRadius` | cx,cy,cz,r | ✅ `circle` | — |
| B4 | 矩形（中心） | `CreateCenterRectangle` | 中心、角点 | ✅ `rect_center` | — |
| B5 | 矩形（对角） | `CreateCornerRectangle` | 两角 | ✅ `rect_corner` | — |
| B6 | 三点圆弧 | `Create3PointArc` | 三点 | ✅ `arc3p` | — |
| B7 | 样条曲线 | `CreateSpline3` | 点表 | ✅ `spline` | **点表必须包 `VARIANT(VT_ARRAY\|VT_R8)`**，否则静默不画 |
| B8 | 多边形 | `CreatePolygon` | 中心、外接圆、边数 | ✅ `polygon` | — |
| B9 | 折线（多点直连） | `CreateLine` 循环 | 点表 | ✅ `polyline` | 螺纹牙型/箱体轮廓用 |
| B10 | **直槽（键槽/油槽）** | `CreateStraightSlot` 🔍 | 两圆心、宽度 | ⬜ **P1** | 比"两圆弧+两直线"手拼稳，但需先抽签名 |
| B11 | 圆弧槽 | `CreateArcSlot` 🔍 | 中心、半径、宽度 | ⬜ P2 | 油沟用 |
| B12 | 椭圆 | `CreateEllipse` 🔍 | 中心、长短半轴 | ⬜ P2 | 油面透视片近似 |
| B13 | 点 | `CreatePoint` 🔍 | 坐标 | ⬜ P1 | 孔位辅助 |
| B14 | 文字 | `InsertSketchText` 🔍 | 文本、曲线 | ➖ | 本项目无刻字 |

### B2 草图编辑（9）

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 坑 |
|---:|---|---|---|:---:|:---:|---|
| B15 | **转换实体引用** | `SketchUseEdge3` 🔍 | 边、链 | ⬜ **P0** | 箱体/端盖大量复用已有轮廓，是省事关键 |
| B16 | **等距实体** | `SketchOffset2` 🔍 | 实体、距离、反向、双侧 | ⬜ **P1** | 壁厚/台阶 |
| B17 | 剪裁实体 | `SketchTrim` 🔍 | 模式（强力/边角/闭合） | ⬜ P1 | 齿轮齿廓清根 |
| B18 | 延伸 | `SketchExtend` 🔍 | 实体、终点 | ⬜ P2 | |
| B19 | 草图圆角 | `SketchFillet` | 半径、两实体 | ✅ `sketch_fillet` | — |
| B20 | 草图倒角 | `SketchChamfer` 🔍 | 距离、两实体 | ⬜ P2 | |
| B21 | 草图镜像 | `SketchMirror` 🔍 | 镜像线、实体 | ⬜ **P1** | 箱体/端盖对称结构 |
| B22 | 草图线性阵列 | `SketchLinearPattern` 🔍 | 方向、间距、个数 | ⬜ P1 | 螺栓孔排 |
| B23 | 草图圆周阵列 | `SketchCircularPattern` | 中心、个数、角度 | ✅ `sketch_circular_pattern` | — |
| B24 | 构造几何线 | `SketchAddConstraints` / 实体 `.ConstructionGeometry` | — | ⬜ P2 | |

### B3 标注与约束（5）

| # | 工具 | API | 关键参数 | 现状 | 优先级 |
|---:|---|---|---|:---:|:---:|
| B25 | 智能尺寸 | `AddDiameterDimension2` / `AddRadialDimension2` / `AddDimension2` / `AddAlongX·YDimension` | **文字放置点**、`as` 标注方式 | ✅ `dimension` / `smart_dim` | **★ 文字放在哪比用哪个 API 重要得多**：放在 `(0,0,0)` 时，圆心在原点（默认写法）⇒ 文字与原点标记叠成一团灰影，"标了却看不见"（§56）。已改为自动放到图元外 + 硬断言 |
| B25b | **批量真尺寸 / 尺寸链** | 同上 + `IFace2.GetSurface` / `ISurface.CylinderParams` | `chain=auto/off`、`kinds`、`anchor` | ✅ `dim_auto` | **★ 用户口径「不是每个尺寸都标」**：回转体半剖面按**尺寸链**只标 总长 + 台阶定位 + 各段 Ø，中间段长 = 派生量（标了会 Σ=总长 ⇒ 过定义、SW 静默拒标，§70）。**落点必须分两套**：横向跨竖线、纵向跨轴线+母线 —— 单图元 `AddAlong*` 在重进草图后会挂错图元（§68）。探针 `dim_auto_shaft` 27/27 |
| B26 | 修改尺寸值 | `IDimension.SetSystemValue3` | 新值、配置 | ✅ `set_dimension` | — |
| B27 | 列出草图尺寸 | 遍历 `IParameter` | — | ✅ `list_dimensions` | — |
| B28 | 几何关系（相等/共线/同心…） | `SketchAddConstraints` 🔍 | 关系枚举 | ⬜ P1 | 让草图"自适应"而非硬坐标 |
| B29 | 完全定义检查 + **自动完全定义** | `ISketch.GetConstrainedStatus` + `ISketchManager.FullyDefineSketch` | 约束位掩码（10 种，全开 1023） | ✅ `check_sketch_constraint` / `fully_define` | **返回码会说谎**：没锚到原点时返回 `0 Success` 而草图仍欠定义 → 必须读回状态（§54） |

---

## C. 参考几何（12）

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 坑 |
|---:|---|---|---|:---:|:---:|---|
| C1 | 选择基准面 | `SelectByID2` type=`"PLANE"` | 名字 | ✅ `select_plane` | — | 中文/英文名要轮试（`_ALIAS`） |
| C2 | 基准面（偏移） | `CreatePlaneAtOffset3` | 距离、翻转、自适应 | ✅ `ref_plane` | — | 3 参，已验 |
| C3 | 基准轴 | `InsertAxis2` / `CreateAxis` | 两基准面 | ✅ `axis_from_planes` | — | |
| C4 | 螺旋线 | `InsertHelix` | 螺距/高度/圈数/旋向/起始角/锥度 | ✅ `helix` | — | **返回值恒 None，别信**：用特征树 diff |
| C5 | **基准面（角度）** | `CreatePlaneAtAngle3` 🔍 | 边/面、角度、翻转 | ⬜ P2 | | 斜孔可用 |
| C6 | **基准面（垂直于曲线 / 过点）** | `CreatePlanePerCurveAndPassPoint3` 🔍 | 曲线、点 | ⬜ P1 | | 扫描路径起点面，替代"算偏移" |
| C7 | **基准点** | `InsertReferencePoint` 🔍 | 类型、参考 | ⬜ P1 | | 定位辅助 |
| C8 | **坐标系** | `InsertCoordinateSystem` 🔍 | 原点、轴向 | ⬜ **P0（M3）** | | **装配层用它钉死两轴位置** |
| C9 | 分割线 | `InsertSplitLineProject` 🔍 | 投影/轮廓 | ⬜ P2 | | 箱体曲面分区 |
| C10 | 组合曲线 | `InsertCompositeCurve` 🔍 | 多段曲线 | ⬜ P2 | | 扫描路径 |
| C11 | 投影曲线 | `InsertProjectedSketch2` 🔍 | 草图、面 | ⬜ P2 | | |
| C12 | 通过 XYZ 点的曲线 | `InsertCurveFilePoint` 🔍 | 点表 | ⬜ P2 | | 齿廓可视化备选 |
| C13 | 涡状线 | `InsertSpiral` 🔍 | — | ➖ | | 本项目无 |

---

## D. 凸台 / 基体（7）

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 坑 |
|---:|---|---|---|:---:|:---:|---|
| D1 | 拉伸凸台 | `FeatureExtrusion3`（23 参） | 深度、端条件、反向、双向、拔模、薄壁 | ✅ `extrude_boss` | — | **方向降级 bug**：不给 `both_directions` 时"两侧对称/完全贯穿两者"被静默降级成单向；**盲切原先无方向备选**（见 30-pitfalls §61） |
| D2 | 旋转凸台 | `FeatureRevolve2` | 角度、反向、方向1/2 | ✅ `revolve_boss` | — | 轴必须是中心线；**阶梯轴别一层层拉伸** — 用 `shaft_profile` 出半剖面再旋转（这才对得上参照件 `20-轴`） |
| D2b | 阶梯轴半剖面（专用） | 组合（折线 + 中心线） | 段表 d/l、两端 45° 倒角 | ✅ `shaft_profile` | — | 总长 = Σl + 2×end_chamfer（倒角是**外扩**，30-pitfalls §65）；闭环圆边选不准，倒角要画进剖面（§66） |
| D3 | 齿轮齿廓（专用） | 组合（样条/折线 + 圆周阵列） | m, z, α, 齿宽, 孔, 键槽 | ✅ `gear_profile` | — | 真机验证 **m=2 z=55**（715 图元/1 轮廓、da 实测 113.987/113.946、质量 136.56g）；模式 polyline/spline；**建议再加"三段圆弧近似"**（比赛指定近似画法） |
| D4 | **扫描凸台** | `InsertProtrusionSwept4` 🔍（与 `InsertCutSwept4` 同族） | 轮廓标记、路径标记、高级选项 | ⬜ **P1** | | 箱体筋板、通气塞 |
| D5 | **加厚** | `FeatureBossThicken2` 🔍 | 厚度、方向 | ⬜ P2 | | 薄壁件 |
| D6 | 放样凸台 | `InsertProtrusionBlend2` 🔍 | 轮廓组、引导线 | ⬜ P2 | | 锥形过渡 |
| D7 | 边界凸台 | `InsertProtrusionBlend3` 🔍 | 方向1/2 曲线 | ⬜ P2 | | |

---

## E. 切除（5）

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 坑 |
|---:|---|---|---|:---:|:---:|---|
| E1 | 拉伸切除 | `FeatureCut4`（27 参） | 深度、端条件、反向、双向 | ✅ `extrude_cut` | — | 同 D1 的方向降级坑；键槽"只开一半"就出在这 |
| E2 | 扫描切除 | `InsertCutSwept4`（19 参） | 轮廓标记 1 / 路径标记 4、自动选择、平滑 | ✅ `cut_swept` | — | **返回值恒 None**；3 套候选参数轮询 |
| E3 | **旋转切除** | `FeatureRevolveCut2` 🔍 | 角度、反向、方向1/2 | ⬜ **P0** | | 轴的退刀槽、挡圈槽、套筒 |
| E4 | 放样切除 | `InsertCutBlend` 🔍 | 轮廓组 | ⬜ P2 | | |
| E5 | 加厚切除 | `FeatureCutThicken2` 🔍 | 厚度 | ⬜ P2 | | |

---

## F. 工程特征（14）

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 坑 |
|---:|---|---|---|:---:|:---:|---|
| F1 | 圆角（定半径） | `FeatureFillet3` | 半径、边集、类型 | ✅ `fillet` | — | 边选取顺序敏感 |
| F2 | 倒角 | `InsertFeatureChamfer` | 距离/角度、边 | ✅ `chamfer` | — | `2×45°` 是轴类标配 |
| F3 | 抽壳 | `InsertFeatureShell` | 厚度、朝外、移除面 | ✅ `shell` | — | |
| F4 | **变半径 / 面圆角 / 完整圆角** | `FeatureFillet3` 扩展参数 | 控制点、半径曲线 | ⬜ **P1** | | 箱体大圆角过渡处需要 |
| F5 | **拔模** | `InsertDraft` / `IFeatureManager.InsertDraft*` 🔍 | 角、中性面、拔模面、分型方向 | ⬜ **P0（箱体）** | | 图纸标注 1°/3°/4°/10°；**本项目最易失败的 op 之一** |
| F6 | **筋** | `InsertRib` / `IFeatureManager.InsertRib` 🔍 | 厚度、方向、延伸、锥度 | ⬜ **P0（箱体）** | | 图上箱体 5 处加强筋 |
| F7 | 螺纹线（装饰 **+ 真实**） | `InsertCosmeticThread3` + `IFeatureManager.CreateDefinition(swFmSweepThread=87)` + `IThreadFeatureData` | 螺纹标准、规格、深度、螺距 | ✅ `thread`（装饰）/ `real_thread`（**真实去料**） | | **用户要的是真实螺纹线**；`Size` 是 no-op，`Diameter`/`Pitch` 必须先设 `*Override`（§50~53） |
| F8 | **镜像（实体）** | `InsertMirrorFeature2`(实体模式) 🔍 | 镜像面、实体 | ⬜ **P1** | | 箱体左右对称批量结构；已有的是"特征镜像" |
| F9 | 圆顶 | `InsertFeatureDome` 🔍 | 高度 | ⬜ P2 | | 通气塞球头 |
| F10 | 特型 | `InsertFreeform` 🔍 | 控制点 | ➖ | | |
| F11 | 弯曲 | `InsertFlexFeature` 🔍 | — | ➖ | | |
| F12 | 包覆 | `InsertWrapFeature` 🔍 | 草图、面、类型 | ➖ | | |
| F13 | 分割 / 组合 / 相交 | `InsertSplitBody` / `InsertCombineFeature` / `InsertIntersect` 🔍 | — | ⬜ P2 | | 箱体备选路线（拆分体方案 B） |
| F14 | 移动面 / 删除面 / 替换面 / 偏移面 | `InsertMoveFace3` / `DeleteFace` / `ReplaceFace2` 🔍 | — | ⬜ P2 | | 兜底修补 |

---

## G. 阵列与镜像（8）

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 坑 |
|---:|---|---|---|:---:|:---:|---|
| G1 | 圆周阵列（特征） | `FeatureCircularPattern4` | 轴、个数、角度、等间距 | ✅ `circular_pattern` | — | 轴承滚珠、端盖孔 |
| G2 | 线性阵列（特征） | `FeatureLinearPattern4` | 方向1/2、间距、个数 | ✅ `linear_pattern` | — | |
| G3 | 镜像（特征） | `InsertMirrorFeature2` | 镜像面、特征 | ✅ `mirror` | — | |
| G4 | 曲线驱动阵列 | `FeatureCurvePattern` 🔍 | 曲线、对齐 | ⬜ P2 | | |
| G5 | 草图驱动阵列 | `FeatureSketchDrivenPattern` 🔍 | 草图点 | ⬜ P2 | | 孔位不规律时好用 |
| G6 | 表格驱动阵列 | `FeatureLinearPattern4`(表) 🔍 | 坐标表 | ⬜ P2 | | |
| G7 | 填充阵列 | `FeatureFillPattern` 🔍 | 边界、间距 | ⬜ P2 | | |
| G8 | 变量阵列 | `FeatureVaryPattern*` 🔍 | — | ⬜ P2 | | |

---

## H. 孔（4）

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 坑 |
|---:|---|---|---|:---:|:---:|---|
| H1 | 键槽（专用） | 组合：草图 + 拉伸切除 | 孔径、键规格（宽×毂槽深） | ✅ `keyway_rect` | — | GB/T1095 数值已固化 |
| H2 | **异型孔向导** | `HoleWizard5` 🔍（族） | 类型（柱形沉孔/锥形沉头/螺纹孔）、标准、规格、深度、终止条件、位置草图 | ⬜ **P0** | | 箱体 `6×Ø9 沉孔Ø18深2`、`M10×1`、`M8`、`4×M3` 全靠它；**19~25 个参数，是参数最多的 op** |
| H3 | 简单直孔 | `InsertHole2` 🔍 | 直径、深度、终止 | ⬜ P1 | | 单一直孔比向导快 |
| H4 | 高级孔（孔系列） | `InsertHoleSeries` 🔍 | 多段孔型 | ⬜ P2 | | 轴承座孔台阶 |

---

## I. 装配（12）—— **本项目从 0 开始**

| # | 工具 | API | 关键参数 | 现状 | 优先级 | 备注 |
|---:|---|---|---|:---:|:---:|---|
| I1 | 新建装配体 | `ISldWorks.NewDocument`(ASM) | 模板 | ✅ `new_assembly` | **P0** | |
| I2 | 插入零部件 | `IAssemblyDoc.AddComponent5` | 路径、配置、位置、固定 | ✅ `insert_component` | **P0** | ★ 只认**反斜杠**路径、只认**已打开**的零件（⇒ 先 open 全部再插，§120） |
| I3 | **添加配合** | `IAssemblyDoc.CreateMateData` + `CreateMate` | 配合类型、两参考、对齐/反向、距离 | ✅ `add_mate` | **P0** | ★ `AddMate*` **全 Obsolete** ⇒ 走 `CreateMateData`+`CreateMate`；distance/angle **必须显式 `EntitiesToMate`**；只支持 coincident/concentric/parallel |
| I4 | 移动/旋转零部件 | `IComponent2.Transform2` 的 `ArrayData` | — | ✅ `set_component_transform`（+ `fix_component` / `float_component`） | P1 | ★ 用**绝对量写入**（幂等、可逐字重放），比 `MoveOrCopy` 稳 |
| I5 | 零部件阵列 | `IAssemblyDoc.*PatternComponent*` 🔍 | 源、方向/轴、个数 | ⬜ | P1 | 螺栓/垫圈成组 |
| I6 | 镜像零部件 | `IAssemblyDoc.MirrorComponents*` 🔍 | 镜像面 | ⬜ | P1 | 左右端盖成对 |
| I7 | **干涉检查** | `IAssemblyDoc.InterferenceDetectionManager` | 范围、忽略项 | ✅ `check_interference` | **P0** | ★★★ **`GetInterferences()` 会污染会话** ⇒ 之后 `Select2`/`EditDelete` **全静默失效** ⇒ 只能排在计划**最后**（§128①）；支持 `allow` 白名单 + `expect` 体积校核 |
| I8 | 间隙验证 | `IAssemblyDoc.ClearanceVerification` 🔍 | 最小间隙 | ⬜ | P1 | |
| I9 | 爆炸视图 | `IAssemblyDoc.CreateExplodedView*` 🔍 | 步骤 | ⬜ | P1 | 装配图/炫图用 |
| I10 | 装配体质量属性 | `IModelDocExtension.CreateMassProperty2` | — | 🔶 `check_component_count` / `check_component_box` 已可数件量盒 | P1 | 总重未做 |
| I11 | 材料明细表（装配层） | `IAssemblyDoc.GetBomTable*` 🔍 | 模板 | ⬜ | P1 | 与图纸层 BOM 共用数据 |
| I12 | 隐藏/显示零部件 | `IComponent2.Visible` 🔍 | — | ⬜ | P1 | 剖视/拍照 |
| I13 | 装配体特征（切除/孔） | 复用 E/H 族 + 范围选"所有零部件" | — | ⬜ | P2 | 装配后打销孔 |

> **★ 装配层 P0 已全部落地（2026-10-09 逐行核实，v1.4.47）**：I1/I2/I3/I7 四条 P0 **全部 ✅**，
> 并已用它们**整机交付**（58 组件 / 85 配合 / 零失效配合 / 站位零位移，
> 见 `60-reducer-project.md` 与 `30-pitfalls.md` §120~§128）。
> **I 族之外另建成 7 个装配 op**（矩阵原先没列，但整机收口全靠它们）：
> `rename_component`（组件改名）、`delete_feature`（按名删特征/配合）、
> `check_mate_count`（配合计数；★ 判据用 **min** 不用 expect）、
> `check_component_box`（站位校验，`box2_*`）、`check_component_count`、
> `inspect_assembly`、`asm_face_probe`（装配体面探针）。
> ⇒ **装配层合计 15 个 op，全部真机双证**。剩下的是**外围**（阵列/镜像/爆炸/BOM/间隙/显示），
> 不在本轮交付链路上。

---

## J. 工程图（18）—— **本项目从 0 开始**

> 工程图 API 是本项目**文档最少、最不可测**的一块。
> M4 第一步：`python scripts/sw_docbuild.py --interfaces IDrawingDoc IView IModelDocExtension --all`
> 先拿签名，再写 op。最小可行版 = 三视图 + 全剖 + 尺寸 + 导出 PDF。

| # | 工具 | API | 关键参数 | 现状 | 优先级 |
|---:|---|---|---|:---:|:---:|
| J1 | 新建工程图 | `ISldWorks.NewDocument`(DRW) + `IDrawingDoc` 🔍 | 模板、图幅（A1/A2） | ⬜ | **P0** |
| J2 | 图纸格式（图框/标题栏） | `IDrawingDoc.SetupSheet5/6` 🔍 | 格式文件、比例、投影类型 | ⬜ | **P0** |
| J3 | 标准三视图 | `IDrawingDoc.Create1stAngleViews2` / `Create3rdAngleViews2` 🔍 | 模型名 | ⬜ | **P0** |
| J4 | 模型视图 | `IDrawingDoc.CreateModelView` 等 🔍 | 朝向 | ⬜ | **P0** |
| J5 | **剖视图** | `IDrawingDoc.CreateSectionView*` 🔍 | 剖切线、深度、方向 | ⬜ | **P0** |
| J6 | 断面图 | `IDrawingDoc.CreateSectionViewAt*` 🔍 | — | ⬜ | P1 |
| J7 | 投影视图 | `IDrawingDoc.CreateUnfoldedView*` / `IView` 派生 🔍 | — | ⬜ | P1 |
| J8 | 局部放大视图 | `IDrawingDoc.CreateDetailView*` 🔍 | 圆心、比例 | ⬜ | P1 |
| J9 | 辅助视图 | `IDrawingDoc.CreateAuxiliaryView*` 🔍 | 参考边 | ⬜ | P2 |
| J10 | 断裂视图 | `IDrawingDoc.CreateBreakView*` 🔍 | — | ⬜ | P2 |
| J11 | **尺寸标注** | `IView.GetVisibleEntities2` + `IModelDoc2.AddDimension2` / 自动标注 `IView.AutoDimension`? 🔍 | — | ⬜ | **P0** |
| J12 | 中心线 / 中心符号线 | `IDrawingDoc.InsertCenterLine` / `InsertCenterMark` 🔍 | — | ⬜ | P1 |
| J13 | **表面粗糙度** | `IModelDocExtension.InsertSurfaceFinishSymbol3` 🔍 | 符号、值、位置 | ⬜ | **P1**（图纸满页 Ra） |
| J14 | 形位公差 | `InsertGtol` 🔍 | 符号、基准 | ⬜ | P1 |
| J15 | 基准特征符号 | `InsertDatumTag2` 🔍 | — | ⬜ | P1 |
| J16 | 剖面线 / 区域剖面线 | `IDrawingDoc.InsertHatch*` 🔍 | — | ⬜ | P2 |
| J17 | **明细表 BOM** | `IDrawingDoc.InsertBomTable4` 🔍 | 模板、锚点、层级 | ⬜ | **P0** |
| J18 | 零件序号（气泡） | `IDrawingDoc.AutoBalloon5` 🔍 | 样式 | ⬜ | **P0** |
| J19 | 注释 / 技术要求 | `IDrawingDoc.InsertNote` / `CreateText` 🔍 | 文本、锚点 | ⬜ | P1 |
| J20 | 导出 PDF | `IModelDocExtension.SaveAs` + PDF 选项 | — | ⬜ | **P0** |

---

## K. 数据与输出（7）

| # | 工具 | API | 关键参数 | 现状 | 优先级 |
|---:|---|---|---|:---:|:---:|
| K1 | 自定义属性 | `ICustomPropertyManager.Add3` | 名、值、类型 | ✅ `set_custom_property` | — |
| K2 | **指定材料** | `IPartDoc.SetMaterialPropertyName2` 🔍 | 库名、材料名 | ⬜ | **P0**（表1 逐件指定材料） |
| K3 | 读取材料 | `IPartDoc.GetMaterialPropertyName2` | — | ✅ `sw_refscan` | — |
| K4 | 配置管理 | `IConfigurationManager` 🔍 | 新增/激活/属性 | ⬜ | P2 |
| K5 | 设计表 | `IModelDoc2.InsertFamilyTableNew` 🔍 | Excel 表 | ⬜ | P2（标准件族用它很香） |
| K6 | 方程式 / 全局变量 | `IEquationMgr.Add3` | 名、表达式 | ⬜ | P2（也可全在 Python 侧算） |
| K7 | 单位系统 | `IModelDocExtension.SetUserPreference*` | — | ✅ `set_units_mm` | — |
| K8 | 批量转换 | 遍历 + `SaveAs` | — | ⬜ | P2（一次出全套 STEP） |

---

## L. 检证（11）—— 本项目的"质量保证"层

| # | 工具 | API | 用途 | 现状 | 优先级 |
|---:|---|---|---|:---:|:---:|
| L1 | 特征数量 | 遍历 `FirstFeature`/`GetNextFeature` | 判断有没有建出来 | ✅ `check_feature_count` | — |
| L2 | 草图自检 | `GetSketchContours` / `GetSketchSegments` | 图元数/类型/轮廓数/闭合 | ✅ `check_sketch` | — |
| L3 | 质量属性 | `CreateMassProperty2` | 体积/质量/质心 | ✅ `check_mass` | — |
| L4 | 包围盒 | `IBody2.GetBodyBox` | 尺寸对不对 | ✅ `check_bbox` | — |
| L5 | 诊断 | 特征遍历 + `GetErrorCode` | 揪"建了但是坏的" | ✅ `check_diagnostics` | — |
| L6 | **截图自查** | `SaveBMP` + 视角 | 肉眼看 | ✅ `sw_shot.py --grid`（**自动裁空白**：结构边缘判据 + 双守卫，§138①） | — |
| L7 | 特征树读数 | 遍历 + `IExtrudeFeatureData2` | 端条件/深度/错误码 | ✅ `sw_inspect.py` | — |
| L8 | **参照件扫描** | 同 L3~L7 批量 | 导出验收基准 | ✅ `sw_refscan.py` | — |
| L9 | **干涉/间隙检查** | 同 I7 | 装配验收 | ⬜ | **P0（M3）** |
| L10 | 测量 | `IModelDocExtension.GetMeasure` 🔍 | 量中心距/中心高 | ⬜ | P1（验收 70/80 用它） |
| L11 | 剖面视图验证 | 复用 J5 | 看内部 | ⬜ | P2 |

---

## S. 曲面（17）—— **CRH380CL 项目新增（2026-10-09）**

> 立项与策略见 `80-crh380cl-project.md`。
> ★★ 本族**所有 API 签名都是 2026-10-09 从 `sldworks.tlb` 直抽的**（`--tlb` / `--tlb-find`）——
> 其中两条**当场被否掉**（见表末 ➖ 两行）：**别照别的 CAD 或记忆写曲面 API。**
> ★★★ **曲面与实体的判据完全不同**：曲面件**没有体积/质量**，只能看**曲面体数与面积**
> （`sw_ops._body_stat`，分型靠 `IBody2.GetType()`；`IBody2.IsSolid`/`GetArea` **不存在**）。
> ★ **2026-10-09 当天进度**：S1 / S2 / S10 / S11 **四个已真机双证**（`surface_extrude` /
> `surface_revolve` / `surface_knit` / `thicken`），配套判据 op **`check_body`**，
> 三个探针 `surface_basics` / `surface_revolve` / `surface_knit` 全绿。
> ★★ **2026-10-09 晚（S1 曲线骨架）追加**：**S14 3D 草图 / S15 通过 XYZ 点的曲线** 也已真机双证 ——
> op `sketch3d_begin` / `sketch3d_end` / `curve_xyz` + 判据 op `check_curve`，
> 探针 `curve_xyz`（**读回点最大偏差 0.0mm**）与 `sketch3d`（**z 端点读回 (0,0,0)→(0,0,50)**）全绿。
> ★★★ **S15 的判据只能读那 3 个硬事实**（2026-10-09 真机实测，容易写错）：
> ① 特征类型名 `GetTypeName2()` = **`CurveInFile`**（不是 `REFERENCECURVES`，那是 `SelectByID2` 的串）；
> ② `IFreePointCurveFeatureData.GetPointCount()` = **坐标个数 = 3 × 点数**（13 个点回 **39**！）；
> ③ `PointArray` = **平铺 tuple**（不是三元组数组），单位**米**。
> 任一写错，读回判据都会把"读法不对"误判成"曲线点不对"。
> ★★ **2026-10-10（S3 放样曲面）追加**：**S3 主力已真机双证** —— op `surface_loft`
> （`IModelDoc2.InsertLoftRefSurface2` 6 参，探针 `surface_loft` **13/13 一次通过**）：
> 两圆截面 r=20@Z0 → r=10@Z50，读回**面积 4805.7127 mm²** = 30π·√2600 **逐位吻合**、
> 盒 **[40,40,50]** 三轴 delta **0**、**1 曲面体 / 0 实体**（⇒ 曲面放样**不出端盖**，是判据分叉点）。
> ★★★ **S3 的三个官方约定（写错就静默不出面）**：① 截面 **mark=1**（引导线 2 / 中心线 4 /
> 起止相切矢量 8 / 32）；② **选择顺序 = 放样方向**；③ ★ **曲面截面可以是开口的**（实体放样不行）。
> ★★★ **选择标记是这两个 op 的命门**：`thicken` / `surface_knit` 的官方帮助都明写
> **"must be selected with a mark of 1"** —— 传 0/4 会**静默失败**（详见 30-pitfalls §129）。
> ★★★ **3D 草图三坑**（详见 30-pitfalls §130）：活动草图名要**裸取** `GetActiveSketch2().Name`
> （套 `typed(...,'IFeature')` 会拿到 16 个浮点的变换矩阵）；3D 草图的树类型名是
> **`3DProfileFeature`**（2D 才是 `ProfileFeature`）；`Insert3DSketch` 官方只承诺
> 『新建 **或** 关闭活动草图』⇒ **不能**用来编辑已有 3D 草图（同机两种行为都出现过）。

| # | 工具 | API（真签名） | 关键参数 | 现状 | 优先级 | 用在 CRH380CL 的哪 |
|---:|---|---|---|:---:|:---:|---|
| S1 | 拉伸曲面 | `IModelDoc2.FeatureExtruRefSurface` | **15 参**：Sd, Flip, Dir, T1, T2, D1, D2, Dchk1, Dchk2, Ddir1, Ddir2, Dang1, Dang2, OffsetReverse1/2 | ✅ `surface_extrude`（探针 `surface_basics`：方管 8000mm² 手算逐位吻合） | **P0** | 车体等截面段 |
| S2 | 旋转曲面 | `IModelDoc2.InsertRevolvedRefSurface` | 4 参：Angle, ReverseDir, Angle2, RevType | ✅ `surface_revolve`（探针 `surface_revolve`：面积 **5000π** 手算逐位吻合） | P1 | 回转类外形 |
| S3 | **放样曲面** ★主力 | `IModelDoc2.InsertLoftRefSurface2` | 6 参：Closed, KeepTangency, ForceNonRational, TessToleranceFactor, **StartMatchingType, EndMatchingType** | ✅ `surface_loft`（探针 `surface_loft`：圆台面积读回 **4805.7127** = 30π·√2600 **逐位吻合**；盒 **[40,40,50]** 三轴 delta **0**；1 曲面体 / 0 实体 ⇒ 无端盖）<br>✅ 带引导线分支：探针 **`surface_loft_guides`**（3 圆截面 + 2 引导线，面积同解析解）。<br>★★ **引导线有两个"静默失败"**（选择集被清空 / 引导线太稀）→ 见 `30-pitfalls.md` §132 | **P0** | **车头鼻锥（多截面渐变）** |
| S4 | 扫描曲面 | `IModelDoc2.InsertSweepRefSurface2` | 6 参：Propagate, TwistCtrlOption, KeepTangency, ForceNonRational, 起/止匹配 | ⬜ 未做 | P1 | 沿路径造型 |
| S5 | 平面区域 | `IModelDoc2.InsertPlanarRefSurface` | **0 参**（预选封闭边界） | ✅ `surface_planar`（探针 `surface_planar`：闭合圆曲线 ⇒ 面积 **1256.4637** vs πr²=1256.6371；探针 `surface_knit_solid` 覆盖**草图边**路径）<br>★★★ **必须选 `EDGE`（边），选曲线特征会静默失败** → `30-pitfalls.md` §133 | P1 | 封口 |
| S6 | 填充曲面 | `IFeatureManager.InsertFillSurface2` | 6 参：Resolutions, Options, VPatchBoundaries, VCurvatureControlType, VFaces, VConstraintCurves | ⬜ 未做 | P1 | 前风挡 / 补洞 |
| S7 | 直纹曲面 | `IFeatureManager.InsertRuledSurfaceFromEdge2` | **11 参**：Type, Length, FlipPullDir, FlipDir, TrimAndSew, Angle, CoordInput, X, Y, Z, BConnectSurface | ⬜ 未做 | P2 | 拔模过渡面 |
| S8 | 等距曲面 | `IModelDoc2.InsertOffsetSurface` | 2 参：Thickness, Reverse | ⬜ 未做 | P2 | — |
| S9 | 自由形 | `IFeatureManager.InsertFreeform` | 6 参：Continuity0~3, Symmetric0/1（另配 `SetFreeformCurveData`） | ⬜ 未做 | P2 | 兜底捏形（精度不可控） |
| S10 | **缝合曲面** ★ | `IFeatureManager.InsertSewRefSurface` | 5 参：UseGapFilters, **TryToFormSolid**, MergeEntities, **KnitTolerance**(0.0001~0.1mm), MaxValueForGapRange | ✅ `surface_knit`（探针 `surface_knit`：共边两面 3600mm²、体 2→1）<br>✅ **缝成实体**分支：探针 **`surface_knit_solid`**（圆台封两盖 ⇒ **1 实体**，体积对 πh/3(R²+Rr+r²)）<br>★★★ **`TryToFormSolid=True` 必须配 `UseGapFilters=True`**，否则**只缝成曲面**（`GetType()=1`、0 自由边）→ §134① | **P0** | **曲面→实体收口** |
| S11 | **加厚** ★ | `IFeatureManager.FeatureBossThicken` | 7 参：Thickness, Direction, FaceIndex(**官方注明 Not used**), FillVolume, Merge, UseFeatScope, UseAutoSelect | ✅ `thicken`（探针 `surface_basics`/`surface_revolve`：25440 / 22560 / 48000 与手算逐位吻合） | **P0** | **曲面→实体（最常用）** |
| S12 | 剪裁曲面 ✅（**换路**） | 原路 `IFeatureManager.PreTrimSurface` → `PostTrimSurface` **废**；改走 **`thicken` → `extrude_cut`** | 4 参 + 1 参（老式 `InsertCutSurface` 2 参仍在） | ⚠️ 原 API **未打通**（7 轮真机全无产出：`Pre` 回 True 但**清空选择集**，其后坐标拾取 / `MultiSelectByRay` **全失效** → `30-pitfalls.md` §135④）<br>✅ **换路达成**：曲面 → `thicken` 成实体壳 → `extrude_cut` 挖穿，去掉量 = **πr²t 逐位吻合**（探针 `shell_window_cut` **23/23**，25440 → 23555.0444mm³）→ §136①<br>✅✅ **已落到真车头**：`crh380cl plan_s5.json` **45/45**（**R-A**：轮廓改用 `polyline`=44 点闭合折线 ⇒ 风挡=上宽下窄圆角梯形、侧窗=后掠斜四边形），翼窗 Δ=−397161（预算 −406001）/ 前风挡 Δ=−4179122（预算 −4362131），差 2.2~4.2%；探针 `poly_window_cut` 21/21（Δ −1581.15 vs 解析 −1581.1561，0.0002%）→ §137 / §139 | **P0** | 修边 / 风挡口 / 侧窗 |
| S13 | 延展曲面 | `IModelDoc2.InsertExtendSurface` | 3 参：ExtendLinear, EndCondition, Distance | ⬜ 未做 | P1 | 补边界缺口 |
| S14 | **3D 草图** ★ | `ISketchManager.Insert3DSketch` | 1 参：UpdateEditRebuild（**开关式**：进/出各调一次） | ✅ `sketch3d_begin` / `sketch3d_end`（探针 `sketch3d`：z 端点读回 **(0,0,0)→(0,0,50)**） | **P0** | 空间引导线 |
| S15 | **通过 XYZ 点的曲线** ★★ | `InsertCurveFileBegin()` → `InsertCurveFilePoint(X,Y,Z)`×N → `InsertCurveFileEnd()` | 逐点喂（**单位米**） | ✅ `curve_xyz` + 判据 op `check_curve`（探针 `curve_xyz`：13 点/4 点读回 **最大偏差 0.0mm**） | **P0** | **参数化流线骨架（本项目的根）** |
| S16 | 组合曲线 | `IModelDoc2.InsertCompositeCurve` | 0 参（预选） | ⬜ 未做 | P1 | 拼引导线 |
| S17 | 分割线 ✅ | `IModelDoc2.InsertSplitLineProject` | 2 参：IsDirectional, FlipDir（返回 **void**） | ✅ **`split_line`**（探针 `split_line` 12/12：圆面被投影线劈成 **2 个半圆面**，各 3926.73mm²）<br>★★★ **唯一可用组合 = 草图 mark=4 → 面 mark=1**（其余 7 组全静默失败）<br>★ 草图平面**不能与目标面平行**（投影无交点 ⇒ 静默失败）→ `30-pitfalls.md` §135 | P1 | 风挡 / 车窗边界 |
| ➖ | 投影曲线 | ⚠️ **tlb 里查无独立 API**（`InsertProjectedCurve` / `SketchProject` 均无） | — | ➖ 不存在 | — | ⇒ 改用 **S15 参数化生成 + S17 分割线** |
| ➖ | 边界曲面 | ⚠️ **tlb 里无 `InsertBoundarySurface` / `InsertBoundaryBoss`** | — | ➖ 不存在 | — | ⇒ **主力改用 S3 放样曲面** |

> **★ 抽签名这一轮最值钱的产出不是"抽到了什么"，而是"否掉了什么"**：
> 投影曲线与边界曲面这两条**教科书上一定会有、实际 tlb 里查不到**的路，
> 如果不先查就照文档写 op，会得到"写完了、装不上、报错指向别处"—— 正是本技能反复踩的坑。
>
> **★★ 2026-10-09 的另一条产出：把"静默失败"从 23 个变体里逼到 1 个参数**。
> `thicken` 一开始怎么都不成（不报错、返回 None、特征树不涨、体数不变），
> 扫了 3 种 API × 各种选择方式 × Direction/FaceIndex/厚度组合共 23 个变体**全绿变红**；
> 最终根因是官方明文要求的 **mark=1**（我方一直传 0/4）。
> **教训：当"换 API 路线"连续多次无效时，要回头查官方 Remarks 里的前置条件，
> 而不是继续在同一维度上加变体。**（同样的 mark=1 在 `InsertSewRefSurface` 上重演了一次。）

---

## 统计与结论

| 族 | 总数 | ✅已验 | 🔶 | ⬜待做 | ➖不需 | **其中 P0** |
|---|---:|---:|---:|---:|---:|---:|
| A 文件文档 | 11 | 6 | 0 | 5 | 0 | 3 |
| B 草图 | 29 | 14 | 0 | 15 | 0 | 3 |
| C 参考几何 | 13 | 4 | 0 | 9 | 0 | 1 |
| D 凸台 | 7 | 3 | 0 | 4 | 0 | 0 |
| E 切除 | 5 | 2 | 0 | 3 | 0 | 1 |
| F 工程特征 | 14 | 3 | 0 | 11 | 3 | 3 |
| G 阵列镜像 | 8 | 3 | 0 | 5 | 0 | 0 |
| H 孔 | 4 | 1 | 0 | 3 | 0 | 1 |
| I 装配 | 13 | 5 | 1 | 7 | 0 | 0 |
| J 工程图 | 20 | 0 | 0 | 20 | 0 | 7 |
| K 数据输出 | 8 | 3 | 0 | 5 | 0 | 2 |
| L 检证 | 11 | 8 | 0 | 3 | 0 | 1 |
| **合计** | **143** | **48** | **1** | **95** | **3** | **26** |

> **⚠️ 口径说明（2026-09-25，v1.4.1）**：上表的 ✅ 是**按特征条目逐行人工核对**得到的，
> **最后一次全表审计在 M2**，合计数尚未把 M3 与 v1.4.1 的升级算进去。**op 表的真机进度以 op 表为准**
> （`references/10-operations.md`，现 **71/71**）。
> M3 已把这几条从 ⬜ 升到 ✅：`B29` 草图完全定义、`F7` 真实螺纹线（`SweepThread`）、
> 特征改名 / `GetCurrentLanguage`（A 族）、GB 标准件查表（K 族）。
> v1.4.1 又把 `B25` 智能尺寸做实（**文字自动放到图元外 + `anchor` 固定圆心点 ⇒ 完全定义**，见 §56~57）。
> **下次全表审计时请逐行重核再改合计数 —— 不要凭感觉改数字。**
>
> **★ 本轮（2026-10-09）只重核了 I 族（装配）**：逐行对着机读 `assets/op_schema.json`
> 与真机交付物核过 —— `I1/I2/I3/I7` 四条 **P0 全部落地**、`I4` 由 `set_component_transform` 达成、
> `I10` 部分达成 ⇒ ✅ **1 → 5**、⬜ **12 → 7**、**P0 4 → 0**（装配层已无可做 P0）。
> **合计行按本表自己的纪律未动**：该表自 M2 起就有 **48+1+95+3 = 147 ≠ 143** 的自洽性问题，
> 不能靠"把 I 族的增量加到合计上"把它掩盖过去 —— 留待**全表审计**逐族重核后再重算。

**读法**：
- 零件层（A/B/C/D/E/F/G/H/K/L）还缺 **9 个 P0**，补完 35 件里 D0~D3 + D6 全通
- **装配层 + 图纸层合计 32 个 op 是全新的**（I、J 两族），是本项目真正的工作量所在
- 26 个 P0 里，**18 个的 API 签名需要先抽**（标 🔍）—— 这是 M1/M3/M4 的第一件事

---

## 附：新增 op 的落地流程（三步法，引自 `50-training.md`）

1. **抽签名**：`python scripts/sw_docbuild.py --interfaces IFeatureManager IAssemblyDoc --all`
   → 拿到准确参数表，写进 `assets/`；**绝不凭记忆写 API**
2. **写 op + 注册**：按 5.1 的模板加进 `OPS`，标明 `verified: False`
3. **真机验证 → 转 verified**：写一条最小计划的 `assets/plans/_probe_<op>.json`，跑通 →
   `sw_shot.py` + `sw_inspect.py` 双证 → 才把 `verified` 改 True，并把"踩到的坑"写进 `30-pitfalls.md`

> 三步法里最容易被跳过、也最不能省的是第 3 步的**双证**（截图 + 读数）。
> 上一轮所有"返回值说谎"的坑，都是靠这一步揪出来的。
