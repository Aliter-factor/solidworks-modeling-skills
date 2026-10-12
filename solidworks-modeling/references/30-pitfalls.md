# 30 · 坑与约束（照这个避开，能省掉大部分调试时间）

以下都是会造成「报错信息完全指不到根因」或「静默出错」的地方，按杀伤力排序。

## 1. 单位：API 里全是米和弧度

SolidWorks 界面上显示 mm，但 **API 的长度参数一律是米、角度一律是弧度**。  
官方文档在参数说明里写得很清楚（`Depth of extrusion for first end in meters`），  
但很容易忽略。

本技能的 op 参数一律**毫米 / 度**，换算在 `sw_ops` 内部统一做（`mm()` / `deg()`）。  
所以：

- 写计划时**不要**自己乘 1000
- 调 `sw_connect` 的底层方法（`select`、`select_face_at`）时参数也是 mm
- 只有直接写 `sw.model.FeatureManager.FeatureExtrusion3(...)` 这类裸调用才需要自己换算

反例：`gears` 的齿顶圆标注 `da = 114`，如果误当米传进去会得到 114 米的齿轮，  
SolidWorks 会报「几何过大」或直接崩，报错完全指不到单位问题。

## 2. 宏录制出来的枚举是**数字**不是名字

SolidWorks 录制宏时，`swEndConditions_e.swEndCondMidPlane` 常常被记成 `6`。  
如果翻译/执行时把 `"6"` 当枚举名去查表，会静默拿到默认值 —— 表现为  
「深度明明给了 20，结果拉伸成 0mm 不报错」。

处理方式（已在代码里）：

- `sw_ops._resolve_end_cond()` 先判断是不是数字，是就直接用
- `sw_macro_to_plan.reverse_enum()` 用 `assets/sw_enums.json` 把数字反查回名字

## 3. 弹窗 = 永久卡死

自动化最怕模态对话框。必关的开关：

| 开关                      | 为什么                             |
| ----------------------- | ------------------------------- |
| `swInputDimValOnCreate` | 不然每次画带尺寸的图元都弹「修改尺寸」框            |
| 默认模板未配置                 | `NewDocument` 会弹「选择模板」框，然后永远等不到 |

`sw_connect.SwSession.suppress_prompts()` 负责前一个；  
后一个用 `sw_env.find_templates()` 提前解析模板路径，`new_part()` 里会报错提示。

**保护做法**：跑之前把 SolidWorks 里没保存的文档存一下。  
自动化一旦卡在弹窗上，那个进程只能强杀。

## 4. 选择是隐式全局状态，mark 号错了报「无效的参考」

SolidWorks 特征 API 不接收「轮廓对象」，它只看**当前选中的东西 + mark 号**。  
典型症状：明明选中了草图，`FeatureExtrusion3` 还是返回 `None`。

必记的 mark（详见 `00-overview.md`）：

- 轮廓草图 → 0
- 阵列种子特征 → 4；阵列方向参考 → 1
- 镜像特征 → 4；镜像面 → 2

**另一个坑**：临时轴（临时基准轴）用 `SelectByID2` 按名字选不中。  
要阵列就先 `axis_from_planes(plane1="Top", plane2="Right")` 显式建一根基准轴。

## 5. `AddToDB` / `DisplayWhenAdded`：不设会慢一个量级

官方文档明确写：`ISketchManager::AddToDB(True)` 让图元直接写库，  
**跳过推断、自动约束、网格吸附**，显著提速；`DisplayWhenAdded(False)` 进一步减少刷新。

代价：**画完必须 `AddToDB(False)`**，否则 SolidWorks 一直处于这个模式，后续特征行为异常。  
`sketch_begin` 打开、`sketch_end` 关闭，本技能已经配对好了。

## 6. 轮廓必须闭合且不自交

`FeatureExtrusion3` / `FeatureCut4` 返回 `None` 的头号原因：

- 草图轮廓没闭合（漏了一段线、或两点没重合）
- 多个轮廓**粘连**（共用边界）→ 判定为自相交
- 轮廓里混入了构造线之外的开放曲线
- **两个闭环互相重叠**（见 6.5）

`GetSketchContourCount` 是最值钱的体检指标（`sketch_end` 会打出来）：  
`0` = 一个闭合轮廓都没有，拉伸必挂；`1` = 干净；`>1` = **多数情况正常**  
（外轮廓+孔+孔阵天生就是多个轮廓），只有在拉伸报错时才需要怀疑。

### 6.1 多段图元的端点**不会**自动粘合 —— 样条尤其

实测（probe21/22，z=55 齿轮）：

| 画法                       | 图元数     | 轮廓数    | 拉伸 |
| ------------------------ | ------- | ------ | -- |
| 55 条齿廓样条 + 55 段齿根弧（交替创建） | 110     | **55** | 失败 |
| 先建全部 55 条样条，再建全部 55 段弧   | 110     | **55** | 失败 |
| 一整圈 **1 条**闭合样条 / 纯折线    | 1 / 715 | **1**  | 成功 |

结论：**圆弧的起点会吸附到已存在的端点，样条的端点不会**。  
两条独立曲线"坐标看起来重合"不等于 SolidWorks 认为它们相连。  
想让多段拼成一个轮廓，只有两条路：

1. 显式加重合约束（`SketchAddConstraints("sgCOINCIDENT")`，要先选中两个端点）；
2. **干脆只用一个图元** —— 整圈一条闭合样条，或整圈一串首尾相接的直线。

本技能选第 2 条（`gear_profile` 的 `mode=polyline`）。

### 6.2 `CreateSpline3` 的点表必须包成 `VARIANT(VT_ARRAY|VT_R8)`

这是最难查的一个坑，因为**它不报错**：函数返回非 None，但草图里图元数恒为 0，  
退出草图时空草图被 SolidWorks 自动丢弃，现象就是"草图凭空消失"。

```python
# ✗ 静默不画：pywin32 把 list 编组成元素类型不对的 SAFEARRAY
sm.CreateSpline3([x1, y1, 0, x2, y2, 0, ...], None, None, False, None)

# ✓ 正确
from win32com.client import VARIANT
import pythoncom
sm.CreateSpline3(VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_R8, flat), None, None, False, None)
```

类型库里 `PointData` 写作 `Optional Null`（= VARIANT），所以 pywin32 拿不到  
"这是双精度数组"的信息，只能猜。用 `sw_connect.variant_r8()` 包一下即可。  
`CreateSpline2` / `CreateSpline` 同理。

顺带：`CreateSpline3` 的签名是  
`CreateSpline3(PointData, Surfs, Direction, SimulateNaturalEnds, Status)` ——  
**第 4 参是 `SimulateNaturalEnds`，没有"闭合"参数**。  
想要闭合曲线，只能让点表末点 == 首点。

### 6.3 样条点数有上限

整圈 1100 点的样条会被静默丢弃（草图直接消失）；拆到 **660 点以内**没问题  
（probe23：441 / 661 点都能建、都是 1 个轮廓）。真要极细的齿形就用折线。

### 6.4 折线方案要让点表**没有连续重复点**

`sw_gear.full_outline()` 在「齿 → 齿根弧」和「齿根弧 → 下一齿」两处端点**重复**  
（z=55 时列表内部有 109 处重复 + 1 处首尾绕回）。拿它逐点连直线会生出  
**110 条零长度线段**，SolidWorks 静默丢弃（实测 1100 点只建出 990 条）。

画图必须用 `sw_gear.outline_closed()` —— 它把连续重复点去掉（715 个互异点），  
`closed=True` 时再补一个首点用于闭合。715 个点 → 715 条线，**一条不多一条不少**。

两种轮廓的画法取舍：

|                     | 图元数       | 几何精度                                | 适合       |
| ------------------- | --------- | ----------------------------------- | -------- |
| `mode=polyline`（默认） | 715（z=55） | 内接真实渐开线，**实测包围盒偏差 <0.06mm**         | 出图、加工、装配 |
| `mode=spline`       | 1         | 插值**向外过冲约 0.2mm**（da 114.0 → 114.4） | 只看效果、渲染  |

### 6.5 两个闭环重叠 = 自相交（键槽的典型死法）

「齿轮外轮廓 + 中心孔 + 键槽矩形」画在同一张草图 → 3 个轮廓 → 拉伸失败，  
报"必须是一个闭合且不自交的轮廓"。原因是**键槽矩形横跨孔壁**，  
和孔圆形成了两个互相重叠的闭环。

正确做法：**键槽单独开一张草图 + 单独切除**（`keyway_rect` op）。

```json
{"op": "gear_profile", "bore": 20},                 // 只画外轮廓 + 孔
{"op": "extrude_boss", "depth": 20},
{"op": "select_plane", "plane": "Front"},
{"op": "sketch_begin"},
{"op": "keyway_rect", "bore": 20, "spec": "6x2.8"},
{"op": "sketch_end"},
{"op": "extrude_cut", "end_cond": "swEndCondThroughAllBoth"}
```

用双向贯穿（`swEndCondThroughAllBoth` = 9）就不必纠结 `reverse_dir` 该给哪个值。

## 7. 根切、齿顶变尖这类工程问题要**在建模前**拦

`z < 17`（α=20°、不变位）必然根切；齿顶圆弧齿厚 `sa < 0.25m` 属于齿顶过尖。  
这两个都是「建得出来但零件是废的」，必须在校验阶段报出来。

`sw_gear.GearParams.diagnostics()` 返回三级：

- `error`：根切、齿顶变尖、齿数 <6、齿根圆 ≤0 —— `strict=True` 时直接中止
- `warn`：齿顶偏薄、不能确定的问题
- `info`：`df < db`（齿根在基圆内，用径向直线补）这类告知性信息

**不要**把 error 降级成 warn 图省事。宁可停下问人。

## 8. 质量属性依赖材料

`CreateMassProperty2` 读出来的质量默认按密度 1000 kg/m³（没赋材料时）。  
所以 `check_mass(expect_mass_g=...)` 比对失败，第一嫌疑是**没赋材料**，不是几何错了。  
要么先赋材料，要么用 `check_bbox` 校验几何。

## 9. 性能：别一条条调

- 每个 API 调用都是一次 COM 往返，慢
- 画草图前开 `AddToDB`（见 5）
- 关掉界面刷新：`sw_connect` 的 `command_in_progress()` 上下文管理器会把  
  `ISldWorks.CommandInProgress` 置 True，期间 SolidWorks 不响应手工操作，也更快
- 齿轮这种几十条样条的几何，是「生成一条写一条」里最慢的一类；  
  `n_flank=8` 时 z=55 约 110 个图元，正常在几秒内

## 10. 位数必须匹配

64 位 SolidWorks 只能被 64 位 Python 通过 COM 调用。混用会直接  
`Dispatch` 失败，报错也不提位数。`sw_env.report()['bits_match']` 会检查。

顺带：`pywin32` 没装时 `import win32com` 就失败。装法：

```
<你的python.exe> -m pip install pywin32 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

（本机实测：pip 默认源不通，必须加 `-i` 走国内镜像。）

## 11. 非主线程里用 COM 要 `CoInitialize`

在后台线程里驱动 SolidWorks（比如 YoYo 的能力进程），每个线程都要  
`pythoncom.CoInitialize()`，退出时 `CoUninitialize()`。  
`SwSession.open()` 里已经调了。

## 12. 别信扩展名和「看起来的」类型

本项目的另一条经验（来自图纸扫描模块）：有文件名叫 `.DXF` 其实是 DWG。  
同理，`open_doc()` 里如果按 `swDocPART` 打不开，会自动用类型 2（装配体）再试一次。

## 13. `.swp` 不是源码

`.swp` 是 OLE2 复合文档，里面是 VBA 的 **p-code（已压缩 + 令牌化）**，  
不是可读代码，没法可靠还原成源码。要用宏训练技能，必须从  
VBA 编辑器（Alt+F11）里导出 `.bas` 或直接复制粘贴。

## 14. 出错时的定位手法

1. `--dry-run` 先看 API 序列是否和预期一致（很多「建模失败」其实是顺序写错了）
2. 执行失败时报告会给出**该步的 API 序列**
3. 还不行就**在 SolidWorks 里手工做一遍并录制宏**，对照宏里的参数  
   —— 宏是唯一不会撒谎的接口说明书（见 `50-training.md`）
4. `IFeatureManager` 的方法在 2024 里不少标着 `Obsolete`，  
   但都还能用；新写法走 `IFeatureManager.CreateFeature` + 对应的  
   `I*FeatureData` 接口。本技能优先用旧的、签名稳定的那批，  
   因为它们的参数含义在官方帮助里有逐条说明，便于核对。

## 15. 晚期绑定三连坑（这一节值得反复看）

SolidWorks 装了 `makepy` 会被拒（`can not automate the makepy process`），  
所以**只能走 pywin32 的晚期绑定**。晚期绑定下有三件事会咬人：

### 15.1 「这到底是方法还是属性」

pywin32 首次访问某成员时先当属性试：拿到了值就把它**缓存成属性**，  
之后写 `doc.GetTitle()` 就报 `'str' object is not callable`；  
拿不到就当方法。同一个名字在不同机器/不同首次访问顺序下结果可能不同。

`GetTitle` / `GetPathName` / `RevisionNumber` / `Extension` 都是**属性**。  
本技能的 `sw_connect.member(obj, name, *args)` 把这个歧义吃掉了，一律用它。

### 15.2 `callable()` 判断不了 COM 对象

pywin32 把任何返回的 IDispatch 都包成 `CDispatch`，而 **CDispatch 自身可调用**  
（调它等于调默认成员）。于是 `callable(doc.SketchManager)` 恒为 True，  
早期代码据此把它"调用"了一下，撞上 `ISketchManager` 没有默认成员，  
报「找不到成员」。判断要用 `sw_connect._is_com_object()`（看 `_oleobj_`）。

### 15.3 拿到的子对象必须过一遍 `typed(obj, "I接口名")`

`CreateCircleByRadius` / `CreateLine` 返回的是**未绑定的 IDispatch**，  
pywin32 不知道 `Select4(Append, Data)` 的 `Data` 是接口指针。  
实测：

```python
seg.Select4(True, None)                        # ✗ com_error -2147352571「类型不匹配」
typed(seg, "ISketchSegment").Select4(True, None)  # ✓ True，选中数 1
```

这个坑的表现是"草图圆周阵列永远报种子图元选不上"。  
根因和解法是同一个：**所有从 SW 拿到的子对象，用之前都过一遍 `typed()`**。  
`typed()` 会把 `sldworks.tlb` 的 ITypeInfo 喂给 pywin32  
（`build.DispatchItem(typeinfo, attr, None, 0)` → `dynamic.CDispatch(oleobj, rep, iface)`），  
之后方法/属性/参数类型全部按真实签名解析。  
`sw_memberkind.py` 就是靠同一份类型库提取成员种类和权威签名的。

### 15.4 `Select4` 的**返回值**不可信

晚期绑定下它经常回 `None`，看着像没选上其实选上了。  
判断"选上没有"一律数 `ISelectionMgr.GetSelectedObjectCount2(-1)`。

## 16. 参数个数必须和类型库一字不差

少传或多传一个都会回 `DISP_E_PARAMNOTOPTIONAL`（报错文字是"非选择性的参数"），  
且报错完全不提哪个参数有问题。写之前先查权威签名：

```bash
python scripts/sw_memberkind.py --sig FeatureCut4          # 27 参
python scripts/sw_memberkind.py --sig FeatureExtrusion3    # 23 参
python scripts/sw_memberkind.py --sig FeatureRevolve2      # 20 参
python scripts/sw_memberkind.py --iface ISketchSpline      # 列一个接口的全部成员
python scripts/sw_memberkind.py --query CircularPattern    # 只知道名字时模糊找
```

`assets/sw_member_kind.json` 是整库导出（505 接口 / 8163 成员 / 2241 属性），  
离线也能查。

## 17. `SelectByID2` 的两个反直觉点

1. **`Type` 参数是字符串字面量**，不是枚举名：  
   写 `"PLANE"` / `"FACE"` / `"SKETCH"` / `"AXIS"`；  
   写 `swSelDATUMPLANES` 这类枚举名会**静默选不中**。
2. **`append=True` 是"切换"语义** —— 已经选中的对象会被**取消**选中。  
   这就是"刚画完草图紧接着拉伸却报选不中草图"的根因。  
   要追加选择用 `append=False` + 先清选择，或者干脆用 `mark` 号区分。

另外，草图图元（圆/线）的 `GetName()` 返回的是**中文本地化名**（如"圆弧1"），  
所以按名字选草图图元不可靠 —— 走 15.3 的 `typed()` + `Select4`。

## 18. `CreateCenterRectangle` 是「中心点 + 一个角点」

不是两个对角点。按对角点传会画出一个只有一半大、还整体偏移的矩形  
（实测 w=80 量出来 160）。这个错离线测不出来，只能量包围盒。  
`sw_gear.keyway_profile()` 返回的是**四个角点**，用之前要自己换算中心  
（见 `op_keyway_rect`）。

同类签名陷阱：

- `CreateCircularSketchStepAndRepeat` 第一个参数是  
  **种子到阵列中心的距离**（不是阵列中心到原点的距离）；  
  `DeleteInstances` 是**布尔**不是字符串。
- 拉伸切除的 `reverse_dir` 决定往哪边咬料，方向错了报的是  
  「轮廓未闭合」这种完全指错方向的错。实测不出料就翻它。


## 19. 返回值集体说谎 —— 「建出来了」和「返回了值」是两件事

这一节是本轮调试最贵的教训。后期绑定下，**下面这些调用返回什么都别信**：

| 调用                                                  | 实际行为                                | 唯一可信的验证方式                                          |
| --------------------------------------------------- | ----------------------------------- | -------------------------------------------------- |
| `IModelDoc2.InsertHelix`                            | 建成功也**返回 None**                     | 前后特征树 diff（有没有新的 `Helix` 类型特征）                     |
| `IFeatureManager.InsertCutSwept4`                   | 同上，返回 None                          | 前后特征树 diff                                         |
| `IFeatureManager.FeatureCut4` / `FeatureExtrusion3` | 建出来是坏的也**不返回 None**                 | `IFeature.GetErrorCode() != 0` + 体积变化              |
| `IModelDoc2.SaveAs`                                 | 目标被占用时**返回成功但不落盘**                  | `GetPathName()` + 文件 mtime                         |
| `IModelDoc2Ext.Select4` / `SelectByID2`             | 回 `False` 其实选中了 / 回 `True` 其实没选中    | 用 `SelectionManager.GetSelectedObjectCount2(-1)` 数 |
| `IPartDoc.GetPartBox`                               | 盒子**偏大**（Ø24 实体回 ±12.3124，真值 ±12.0） | `GetBodies2` + `IBody2.GetBodyBox` 取并集             |
| `ISketchPoint.GetCoords`                            | 直接回 `None`（`[out]` 参数编组不出来）         | 用 `X` / `Y` / `Z` **属性**逐个读                        |

**通用结论**：写完一个特征，用「**特征树前后 diff + GetErrorCode + 体积/包围盒变化**」三件套确认，  
而不是看调用的返回值。`sw_connect.bounding_box()` 已经换成 `GetBodyBox`；  
`op_helix` / `op_cut_swept` 已经改成 diff 法；`save_as` 已经回读 `GetPathName`。

```python
# 特征树 diff 的标准写法
def _tree(sw):
    m = sw.model
    f = member(m, "FirstFeature")
    out = []
    while f:
        n = member(f, "Name")
        t = member(f, "GetTypeName")
        out.append((n, t))
        f = member(f, "GetNextFeature")
    return out

before = len(_tree(sw))
run_the_call()
after = _tree(sw)
new = [x for x in after if x not in before]      # 建出来了才有新特征
```

## 20. `GetTypeName2()` 回 `"ICE"` 是**正常**的，不是错误

Instant3D 建出来的拉伸/切除特征，`IFeature.GetTypeName2()` 会回字符串 `"ICE"`。  
一度被误判成"特征报错"，白白排查了半天。

- 真实类型用**旧的** `IFeature.GetTypeName()`（回 `"Extrusion"` / `"Cut"` / `"Helix"` …）。
- 是不是坏特征，**只看 `GetErrorCode()`**（`swFeatureError_e`，0 = OK）。  
  `sw_connect.FEATURE_ERROR_NAMES` 里有常用码：30=拉伸结果不相交、34=切除轮廓非法…
- 别把 `ICE` 加进"错误类型"黑名单。

## 21. 「完全贯穿两者 / 两侧对称」会被静默降级成单向

**键槽只开了一半**的根因。`FeatureCut4`（27 参）/ `FeatureExtrusion3`（23 参）里：

- `Sd`（single direction）这个参数在很多录制出来的示例里是 `True`；
- 只看 `end_cond = swEndCondThroughAllBoth` 而 `Sd=True` 时，SolidWorks **不报错**，  
  而是老老实实按**单向完全贯穿**执行 —— 于是键槽只啃了半边，特征树里也显示正常。

规则：只要方向语义是"两侧"（`ThroughAllBoth` / `MidPlane`），**必须把 `Sd` 显式置 `False`**。

```python
BOTH = {"swEndCondThroughAllBoth", "swEndCondMidPlane"}
sd0 = not (both_directions or (base_end_cond in BOTH))   # ← 别写 not both_directions
```

`op_extrude_cut` / `op_extrude_boss` 已经按 `_extrude_variants()` 做**候选方案轮询**：  
主方案 → 单向完全贯穿+反向 → 双向完全贯穿-两者 → 两侧对称±100mm，  
每轮用 `GetErrorCode` + 体积变化筛，谁通过用谁，并把选中的 `variant` 记进报告。

## 22. 截图自查：不设 `ViewDisplayShaded()` 会拍到线框

`SaveBMP` 只会把**当前显示状态**拍下来。如果文档停在"隐藏线可见/线架图"，  
截出来的图里根本没实体（顺带把几百圈螺旋线全画上，糊成一片）—— 看着像模型是空的。

`sw_shot.py` 截图前固定做三件事：

```python
member(m, "ViewDisplayShaded")        # ① 强制着色显示
member(m, "ShowNamedView2", name, id) # ② 定视角
member(m, "ViewZoomtofit2")           # ③ 适配窗口
member(m, "SaveBMP", str(bmp), w, h)  # ④ 落盘
# ⑤ SaveBMP 返回不可信 → 校验文件大小 > 2048 字节
```

配套的 `sw_inspect.py` 负责"读数"：特征树（类型/错误码/`type_legacy`）、  
草图图元数与轮廓数、逐点坐标、拉伸定义（`end_cond` / `depth2` / `reverse`）、  
质量与包围盒。**「截图看形状 + inspect 看数值」两条腿都要**，缺一条就会在  
"看着对但其实差 0.3mm"或者"数值对但倒角方向反了"上翻车。

## 23. Toolbox：能开文件，但**选不出规格**

`C:\SOLIDWORKS Data\browser\<标准>\<类别>\<子类>\*.sldprt` 是 Toolbox 库本体  
（本机 19 个标准：GB / ISO / DIN / JIS / Ansi Metric …），文件**能直接打开**：

```bash
python scripts/sw_toolbox.py --list                       # 列标准
python scripts/sw_toolbox.py --find bolt                   # 搜文件
python scripts/sw_toolbox.py --open "<sldprt 路径>" --out D:\out\x.SLDPRT
```

但**规格（M8×20 这种）不是配置**：打开后只有 `Default` 和 `PreviewCfg` 两个配置，  
真正的规格是 Toolbox 运行时从它自己的数据库里算出来的。官方那套  
`ISldWorks.GetAddInObject("{ED783340-…}")` 在后期绑定下**返回 None**，  
`InsertSmartFastener` 在 app 对象上也取不到。

**结论**：标准件走**参数化模板**（bolt / gear / leadscrew）可靠且可复现；  
Toolbox 只当"查阅真实尺寸的参考库"用 —— 打开对文件量一下，把数字抄进计划参数。

## 24. 凭记忆写 API 名，一次错 8 个（M1 实测）

M1 开工第一件事是按三步法抽签名，结果发现**矩阵里凭印象写的 API 名错了 8 处**：

| 凭印象写的                                  | 实际存在的                                                               |
| -------------------------------------- | ------------------------------------------------------------------- |
| `InsertDraft`                          | `InsertMultiFaceDraft`                                              |
| `InsertRib`（当有参用）                      | `IModelDoc2.InsertRib2`（9 参）；`IFeatureManager.InsertRib()` 是**零参数** |
| `ISketchManager.SketchMirror`          | `IModelDoc2.SketchMirror()`                                         |
| `CreateStraightSlot` / `CreateArcSlot` | `CreateSketchSlot`                                                  |
| `SketchLinearPattern`                  | `CreateLinearSketchStepAndRepeat`                                   |
| `SketchChamfer`                        | `CreateChamfer`                                                     |
| `SketchAddConstraints`                 | 不在 `ISketchManager`（约束挂在 `ISketch`）                                 |
| `AddMate3`                             | 已废弃 → `AddMate4`                                                    |

**只错名字还算好的**，更贵的是"名字对、参数个数错"——那是 §16 那类静默失败。  
所以流程定死：**任何标 🔍 的项，先 `sw_apisig.py --sig`，再写代码**。

### 24.1 `--gaps`：签名**没解析出来**的成员，不能当签名用

有一类页面用的是 `Sub` 而不是 `Function`（例如 `IPartDoc.SetMaterialPropertyName2`），  
解析器抓不到参数表，`--gaps` 会把它们列出来。遇到它们**别猜**，去页面 Remarks 的  
示例里反推 —— 我就是从 `PartDoc.SetMaterialPropertyName2 "AddHoles", "…sldmat", "Alloy Steel"`  
这行示例定出 `(ConfigName, Database, Name)` 三个参数的。

同理 `IModelDoc2.InsertRib2` 也是从 `Sub InsertRib2( _ ByVal Is2Sided As …` 的原文里  
一个参数一个参数抄下来的（9 个）。

### 24.2 `sw_docbuild.py` 曾经会**整份覆盖** `api.json`

`IFeatureManager` 一个接口就 284 个成员，`IAssemblyDoc` / `IDrawingDoc` 各有几百，  
实际都是**分批抽**。而批次之间常跨界（先抽特征、再抽装配、再抽工程图）——  
老版本每次从零写，**第二批把第一批整个覆盖**（白跑一轮）。  
已改成 `load_prev()` 增量合并：本次只覆盖抽到的那些键。  
`sw_apisig.py --build` 也走同一条路。

### 24.3 SW2024 里**没有** `SelectAllByType`

想"按类型批量选实体"（`swSelSOLIDBODIES = 76`）时找不到方法：  
CHM 全文搜 `SelectAllByType` / `SelectByType` **零结果**，实测也调不到。  
→ 脚本里选实体只能靠**坐标射线拾取**：`SelectByID2("", "SOLIDBODY", x, y, z, …)`  
且坐标必须**落在实体表面上**。`mirror_body` 的 `body_point` 参数就是为这个加的。

## 25. 选择 **mark** 是硬约定，文档不写就一定会错

SolidWorks 的特征命令靠 `SelectByID2` 的第 7 个参数（`mark`）区分"这个选择是干什么用的"。  
**同一个面，当轮廓选和当拔模面选，mark 不一样；写错就不报错地失败或做错东西。**  
M1 从 CHM 里抄下来的三张表（这几张表是这一节的全部价值，抄下来别再猜）：

| 命令                                               | 选择标记                                                       |
| ------------------------------------------------ | ---------------------------------------------------------- |
| `FeatureRevolve2`（旋转凸台/切除）                       | 草图 = **0**；轴 = **4 或 16**；到面/到顶点 = 32；受影响实体 = 1            |
| `InsertMultiFaceDraft`（拔模）                       | 拔模方向（面/基准面/边/两顶点）= **1**；要拔模的**面** = **2**；拔模**边** = **4** |
| `InsertMirrorFeature2`（镜像）                       | 特征 = **1**；面 = **128**；**实体 = 256**；镜像面 = **2**            |
| 扫掠（`InsertCutSwept*` / `InsertProtrusionSwept*`） | 轮廓 = **1**；路径 = **4**                                      |

> 实测补一句：本机 `revolve_boss` 用 **mark=1** 选轴也能跑通（`FeatureRevolve2` 的  
> 官方文档写 4/16）。所以 `revolve_cut` 的候选序列是 `1 → 4 → 16 → 草图内中心线对象`，  
> **不赌哪一个**。这也是"变体轮询"存在的意义：文档和实测不一致时，两个都试。

## 26. `InsertProtrusionSwept4` 与 `InsertCutSwept4` 参数**不同**

两个长得像双胞胎，参数表不一样 —— 照抄会错位：

|                          | 参数个数 | 差异                                                                                   |
| ------------------------ | ---- | ------------------------------------------------------------------------------------ |
| `InsertCutSwept4`        | 19   | 有 `UseFeatScope`/`UseAutoSelect`，**没有** `Merge`                                      |
| `InsertProtrusionSwept4` | 20   | 第 13 个是 **`Merge`**，末尾还多 `CircularProfile` / `CircularProfileDiameter` / `Direction` |

`CircularProfile=True` + 给直径，就**不用画轮廓草图**了（截面直接用圆）——  
通气塞那种圆柱头可以省一张草图。

## 27. 草图图元名随**界面语言**变，别拿名字选中心线

草图实体在中文界面下可能叫 `直线1`，英文下是 `Line1`；硬编码名字注定在某台机器上失败。  
`revolve_cut` 的做法是双保险：先按名字试三种 mark，全败就  
**在草图里遍历图元、按 `GetType()==0`（直线）+ `ConstructionGeometry==True`（构造几何）  
抓到第一条中心线，直接 `Select4` 选它** —— 完全不看名字。  
代价是 `Select4` 没有 `mark` 参数，所以它只能当兜底、不能当主路径。

## 28. 草图**自动名**同样是坑：给每张草图 `name`

`sketch_end` 现在支持 `name` 参数（M1 新增）。理由和 §27 一样：  
后续 op 的 `profile` / `path` / `sketch` 参数都按名字引用，而草图自动名  
（`Sketch1` / `草图1`）随语言和版本变 —— 命名之后计划才是**跨机器可复现**的。

## 29. 装饰螺纹线：选**圆边**，不是圆柱面；而且它**不在特征链上**

`InsertCosmeticThread3` 有两个反直觉的硬事实（2026-09-25 真机实测，各试了 84 组）：

- **选圆柱面 → 直接返回 `None`，什么都不建。** 选圆柱端面的**圆形边** → 返回一个  
  `Feature`（`Name='装饰螺纹线1'`、`GetTypeName2='CosmeticThread'`），并且真的建上了。  
  `mark` 试了 0 / 128 / 1，**都行**（这条命令不看 mark）。
- 装饰螺纹线**不在 `FirstFeature → GetNextFeature` 链上**（它挂在「注解」文件夹下），  
  所以 `GetFeatureCount()` 不变、特征树 diff 也抓不到它。  
  ⚠ 用「特征树有没有新增」去验证螺纹线，会得到「明明建上了却报没生成」的假失败。  
  **验证只能认返回值：`ret is not None`。**（注意 COM 对象真值语义不可信，写 `is not None` 别写 `if ret`。）

## 30. 材料：`GetMaterialPropertyName2` 回的是**元组**，不是字符串

```python
v = part.GetMaterialPropertyName2("", None)   # → ('普通碳钢', 'solidworks materials')  ← 元组！
```

晚期绑定下两个出参被包成 tuple。早先写成 `isinstance(v, str)` ⇒ **永远读成 None** ⇒  
把「材料其实设上了」误报成「材料没设上」，白查一轮。稳妥读法（`_read_material` 已实现）：

```python
if isinstance(v, (tuple, list)):  v = v[0] if v else None
```

另外两条：

- `ConfigName` 传**空串**是行的（实测 `''` 和 `'默认'` 都成功），不必非要配置名。
- `Name` 必须是**材料库里真实存在的名字**。中文界面下库里是**中文名**（`普通碳钢` /  
  `灰铸铁` / `合金钢` / `AISI 1020 钢，冷轧` …），不是 `Alloy Steel`。  
  别猜 —— 直接读库：`lang/<语言>/sldmaterials/*.sldmat` 是 **UTF-16** 的 XML，  
  `grep` 找不到；用 `read_text(encoding="utf-16")` 再抓 `<material name="...">`。  
  库路径用 `ISldWorks.GetMaterialDatabases()` 拿（返回 8.3 短路径，正常）。
- 兜底读法：`IModelDoc2.MaterialIdName` 形如 `'solidworks materials|普通碳钢|9'`，  
  按 `|` 切开取第 2 段就是材料名。

## 31. 别用 `contextlib.suppress(Exception)` 包 API 调用 —— 真错误会被吞掉

M1 新 op 初版把每个 COM 调用都包在 `with contextlib.suppress(Exception):` 里。  
后果：参数个数错、对象为空、选择不对 —— 这些**真错误全被吞了**，  
只剩一句「没生成特征」，真机排查白跑三轮。  
改用 `_call()`（本仓 `sw_ops._call`）：返回 `(结果, 错误串或 None)`，把错误写进 `notes` / 异常消息。  
**凡「调 API 看它成不成」的地方都该用它**；`suppress` 只留给「失败也无所谓」的清理动作  
（`ClearSelection`、`GetErrorCode` 之类）。

## 32. `{{ }}` 内嵌表达式曾经不解析 → 磁盘上长出字面量目录

`resolve()` 最初只处理「整个值就是一个 `{{ ... }}`」，不处理**内嵌**：

```json
"path": "{{ params.out }}/probe_draft.SLDPRT"     // 旧版：整串被当字面路径！
```

于是 9 个探针全在 `save_as` 失败，而且磁盘上**真的建出一个名为 `{{ params.out }}` 的目录**。  
修复：`_EXPR_RE.sub(...)` 做内嵌替换（整值仍走 `fullmatch` 以求值类型）。  
另加防线：`save_as` 里若 `path` 仍含 `{{` / `}}` 就**报错**，绝不让它落盘成字面量。  
（教训：会「静默走坏路」的参数，值得在入口处加一道硬校验。）

⚠ 排查时还要记住一条：**同一轮里对同一个文件发多次 Edit 会互相覆盖**（后写覆盖先写），  
改代码/JSON 要一次一处、改完即验。

## 33. 异型孔向导：**「孔位怎么给」比 27 个参数重要得多**

`HoleWizard5` 参数最多，但真正决定成败的是**先选什么**。实测矩阵（2026-09-25）：

| 选什么                                      | 结果                                   |
| ---------------------------------------- | ------------------------------------ |
| 选**整张草图**（mark 0/1/2/4 全试）               | 选中成功，**API 返回 None，建不出任何东西**         |
| 选**面**（`SelectByID2("","FACE",x,y,z,…)`） | ✅ 建成，但只出**一个**孔（孔位=拾取点）              |
| 选**孔位点**（`EXTSKETCHPOINT`，模型空间坐标）        | ✅ 建成；**多个点 append 后调一次 → 一个特征里含全部孔** |

另外两条同样关键：

- 尾部 5 个布尔要照**官方示例**给：`False, True, True, True, True, False`。  
  照「参数说明」自己推导出来的 `False, False, True, False, True, False` 建不出东西 ——  
  **参数说明不足以还原可用的调用，必须找官方 Example**。
- 孔位点必须画在**实体的面**上（`select_face` → `sketch_begin` → `point` → `sketch_end`），  
  画在穿过实体内部的基准面（如前视面）上会静默失败。
- 官方两条示例（都在帮助里）：  
  *Insert Hole Wizard Slot and Hole Example (VBA)* 用**选面**；  
  *Get and Add Sketch Points in Hole Wizard Feature Example (VBA)* 用选**点**  
  （并特意提到「要把草图空间里的点变换到模型空间」）。

### 33.1 参数表（真签名来自 `sldworks.tlb`，共 **27** 个）

```
HoleWizard5(GenericHoleType, StandardIndex, FastenerTypeIndex, SSize, EndType,
            Diameter, Depth, Length, Value1..Value12,
            ThreadClass, RevDir, FeatureScope, AutoSelect,
            AssemblyFeatureScope, AutoSelectComponents, PropagateFeatureToParts)
```

数错个数就会得到 `TypeError: takes from 1 to 28 positional arguments but 29 were given`  
—— 曾经把 `RevDir` 写重复，尾部多出一个布尔，卡了整整一轮。

### 33.2 「特征建出来了」≠「成功」：`_new_feature` 的第二种失败

真机上报的是 **重建失败 1**：特征确实出现在树上，但状态是坏的。  
**关键**：这种情况必须 `删掉 → 试下一个变体`，不能直接抛错终止。  
（`mirror_body` 当时就是在这里写错了，白白浪费一次机会。）

### 33.3 孔型参数：只有**过孔 + 完全贯穿**这一组是稳的（实测）

| 孔型 / 规格 / 终止                                                                      | 结果                                                            |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `swWzdHole` + `swStandardGBScrewClearances`(356) + `'M8'` + `swEndCondThroughAll` | ✅ 去料 4433 mm³ = 2 × **Ø8.4** × 40mm（GB/T 5277 细系列 M8 过孔），分毫不差 |
| `swWzdHole` + `swStandardGBDrillSizes`(355) + `'Ø9'` + 贯通                         | ⚠ 特征成功、体积几乎没动（去料 227 mm³）—— 规格串没被认出来，退化成了超小孔。**别用**           |
| `swWzdTap`(4) + `swStandardGBTappedHole`(359) + 贯通                                | ❌ 重建失败 1                                                      |
| 盲孔（`swEndCondBlind`）而 `depth` 没给                                                  | ❌ 静默不成孔 —— `depth` 默认是 0，**盲孔必须显式给 depth**                    |

**所以：验收孔的时候一定要看「去料体积」对不对**，光看特征名出现是不够的  
（`DrillSizes` 那一行就是活生生的例子：特征建出来了，孔却几乎是空的）。

## 34. 镜像：`IPartDoc.InsertMirrorFeature` 能用，`IM2` 建不出来

同一件事有三个 API，实测结论**和文档直觉相反**：

| API                                                                           | 结果                                                                  |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| `IFeatureManager.InsertMirrorFeature2(bMirrorBody, geom, merge, knit, scope)` | ❌ 8 组组合（mark 1/4 × merge T/F × scope 0/1/2 × 选择顺序正反）**全部返回空、建不出特征** |
| `IPartDoc.InsertMirrorFeature(GeometryPattern) -> Bool`                       | ✅ **一调就出「镜向1」**（宏录制的也是这条）                                           |
| `IFeatureManager.InsertMirrorFeature(4 参)`                                    | 备用兜底                                                                |

选择状态是**对的**（用 `SelectionManager.GetSelectedObjectMark` 复核过：特征 `mark=1`、  
镜像面 `mark=2`，类型码 22=swSelBODYFEATURES、4=swSelPLANE）——  
所以这不是「选错了」，而是**这条 API 在这个组合下不好使**。

### 34.1 关键区分：**镜像「特征」和镜像「实体」走的是两条不同的 API**（实测）

| 镜像对象   | 选择                        | 能用的 API                                                               | 结果                                          |
| ------ | ------------------------- | --------------------------------------------------------------------- | ------------------------------------------- |
| **实体** | `body_point` + `mark=256` | `IFeatureManager.InsertMirrorFeature(bMirrorBody, geom, merge, knit)` | ✅ 实体 1→2、体积正好翻倍、包围盒 80×20×10（x∈[−40,40] 对称） |
| **实体** | 同上                        | `IPartDoc.InsertMirrorFeature`                                        | ❌ 特征树没新增（静默）                                |
| **特征** | 特征名 + `mark=1`            | `IPartDoc.InsertMirrorFeature`                                        | ⚠ **建出特征但重建失败 51**（草图外部参考失效）                |
| **特征** | 同上                        | `IFeatureManager.InsertMirrorFeature` / `IM2`                         | ⚠ 同样是 51                                    |

**镜像「特征」报 51 的原因**：草图挂在默认基准面（前视面）上，重解时要重新推导  
草图的定位参考，这条链在这台机器上走不通。**要镜像特征 → 改用 `body_point` 走实体镜像**，  
或者干脆把那个特征换成「独立实体」再造。

### 34.2 教训：重建失败要**继续试下一个变体**，不是直接抛错

`mirror_body` 一开始在第一次拿到「重建失败」时就直接 `raise` 了 ——  
而正确做法是 `删掉这个坏特征 → continue`。改掉之后，同一份探针从  
「4 个变体一个都没试完」变成「第 3 个变体一次成功」。  
**这条对每个 op 都成立**：`got is None`（没建）和 `重建失败`（建坏了）  
都只是「这个变体不行」，都不该终止轮询。

## 35. 加筋：真正的开关是 `IsNormToSketch`，而且**必须是 False**

筋（Rib）在 M1 里卡了最久 —— 前后 20 多组参数「全灭」，而且**一声不响**：  
不抛异常、不建特征、体积一点不变。三个 API 的血缘关系是：

| API                         | 参数     | 状态             |
| --------------------------- | ------ | -------------- |
| `IModelDoc2.InsertRib`      | 8      | Obsolete       |
| `IModelDoc2.InsertRib2`     | 9      | Obsolete       |
| `IFeatureManager.InsertRib` | **10** | 现行，返回 **void** |

`IFeatureManager.InsertRib` 在文档页上**参数根本抓不出来**（`sw_apisig --gaps` 会列到它），  
真签名是从 `sldworks.tlb` 挖的 —— 比 `InsertRib2` 多一个 `IsDraftedFromWall`：

```
InsertRib(Is2Sided, ReverseThicknessDir, Thickness, ReferenceEdgeIndex,
          ReverseMaterialDir, IsDrafted, DraftOutward, DraftAngle,
          IsNormToSketch, IsDraftedFromWall) -> void
```

**真凶**：`IsNormToSketch = True` → 静默无操作。换成 `False` 立刻出「筋1」。  
跟筋厚、材料方向、草图选择标记（mark 0/1/4 都能建）、实体是 MidPlane 还是盲拉  
**全都无关** —— 前面所有轮次都在错误的地方找原因。

验证只能靠**特征树新增 + 体积增加**（返回 void）。而且体积增量要对得上几何：  
L 形支座那条斜线，开口轮廓与实体围出三角形 150 mm² × 筋厚 6 mm = **900 mm³**，  
实测体积增量正好 900 mm³ —— 这才是「真建出来了」而不是「多了个特征名」。

## 36. 签名抓不出来时，去读 `sldworks.tlb` —— 它是**编译器级**的事实

CHM 抓取器对 `Sub` 形式 / 跨行声明无能为力，`--gaps` 一次能列出几十个成员。  
这类方法**不能靠文档猜参数**，但也不用真机试 —— 类型库里写得清清楚楚：

```
python scripts/sw_apisig.py --tlb IFeatureManager.InsertRib      # 精确查
python scripts/sw_apisig.py --tlb-find Rib                       # 模糊搜
```

输出参数个数、名字、类型、可选数、返回类型，**离线可跑、不用开 SolidWorks**。  
本机实测它一次性揭示了：`InsertRib` 是 10 参（不是 9）、`HoleWizard5` 是 **27** 参  
（尾部 ThreadClass/RevDir/FeatureScope/AutoSelect/AssemblyFeatureScope/  
AutoSelectComponents/PropagateFeatureToParts）、`IPartDoc.InsertMirrorFeature`  
只收 1 个布尔且**返回 Boolean 而不是 Feature**（所以镜像必须靠特征树 diff 验证）。

排坑顺序建议：`--sig`（看说明/Remarks）→ **`--tlb`（对数）** → 写 op → 真机探针。

## 37. 三个「API 名字写错」的 op —— 它们从来没真跑通过

M2 上手第一件事是给 `fillet` / `chamfer` / `shell` 写探针，结果三个全炸，  
而且**炸的原因跟建模思路无关，纯粹是 API 名字/参数对不上**。这三个 op 之前的  
`verified=False` 是诚实的 —— 它们一次都没成功过。

| op        | 原来写的                                            | 真相（来自 `--tlb`）                                                                      |
| --------- | ----------------------------------------------- | ----------------------------------------------------------------------------------- |
| `shell`   | `IFeatureManager.InsertFeatureShell`            | **这个成员根本不存在**。真身是 `IModelDoc2.InsertFeatureShell(Thickness, Outward)`，而且**返回 void** |
| `chamfer` | `InsertFeatureChamfer(类型,0,0,0,d,d,角度,0,0)` 9 个 | **8 个参数**，顺序是 `(Options, ChamferType, Width, Angle, OtherDist, VCD1, VCD2, VCD3)`   |
| `fillet`  | `FeatureFillet3(...)` 12 个                      | `FeatureFillet3` 是 **14 参**；改用最短的 `FeatureFillet`（7 参）                              |

`ISketchManager.AddDimension` 也是同一类：**不存在**，所以 `dimension` op  
每次调用都在抛 `AttributeError`。能用的只有  
`ISketchManager.AddAlongXDimension / AddAlongYDimension` 和  
`IModelDoc2.AddDimension2 / IModelDocExtension.AddDimension`。

**教训**：op 的 `api=[...]` 字段是**声明**，不是事实。写 op 时把 `--tlb` 的输出  
对上再落笔，比事后排查「为什么没反应」便宜得多。

## 38. 返回 void 的 API 只能用特征树 diff —— 而它原来在拿返回值判空

`IModelDoc2.InsertFeatureShell` 返回 `void`。原实现是  
`f = _fm(sw).InsertFeatureShell(...)`；这个方法**连属性都不存在**，  
所以 `_fm(sw)` 上取 `InsertFeatureShell` 直接抛，永远走不到判空那一步。  
即使走到了，`void` 也只会给 `None` ⇒ 一律判成「失败了」。

**规矩**：`--tlb` 里返回类型是 `void` 的，一律这么写 ——  
`_call(...)` 拿异常 + `_new_feature(sw, tree0)` 拿特征 + `_volume(sw)` 拿去料量。  
三层缺一不可，因为 `void` 什么都不告诉你。

## 39. 「选边」不要靠相机射线拾取 —— 用坐标最近邻直接认领对象

原来的 `fillet` / `chamfer` / 阵列的注释写着「必须先选中边（用坐标选面再选边）」，  
实际只能靠 `SelectByID2` 射线拾取 —— **换个视角、换个窗口比例，同一份计划就选不中**，  
而且失败方式是「静默选到别的边」，倒圆倒错地方还不报错。

现在改成：遍历 `IBody2.GetEdges()` → 每条边算**中点**（两端顶点取平均；  
圆柱口那种闭环边没有顶点，退到曲线参数中点）→ 挑离目标坐标最近的一条  
→ `IEntity.Select4(True, None)` 选中。全程不经过相机，**同样的计划在任何视图下结果一致**。

配套的三条设计决定：

- **容差必须存在**：最近那条边离目标点超过 `tol`（默认 8mm）就判「没匹配上」并报错。  
  不勉强选一条 —— 勉强选中的后果是「不报错但倒错地方」，比报错贵得多。
- `fillet` 与 `chamfer` **共用同一个 `_select_by_spec`**：否则两个 op 的选择语义迟早漂。
- 面支持 `"+Z"` 这种方向串（取法向匹配里面积最大的那个），箱体抽壳的顶面就是 `"+Z"`。

**顺带一条**：`FEATURE_ERROR_NAMES` 之类字典要留给「轮试」。轮试时**每次都要重新选一遍** ——  
选择集会被 SolidWorks 消费掉，不重选的话第 2 个变体必然假失败（这个坑在 `mirror_body`  
上已经吃过一次）。

## 40. 草图特征的类型名是 `ProfileFeature`，不是 `"Sketch"`

`list_dimensions` 里有一句 `if include_sketch or tname != "Sketch"`，  
本意是「`include_sketch=False` 时跳过草图」。但草图特征的真实类型名是  
**`ProfileFeature`**（这在本技能 30-pitfalls 里早就写过），条件**恒为真**  
⇒ 这个参数**从来没生效过**。

**教训**：拿类型名做判据的地方，值要来自真机 `feature_names()` 的输出，  
而不是「听起来应该叫什么」。`GetTypeName2()` 回 `ICE`（Instant3D）也是同一类陷阱。

## 41. 参数化改型：别逼用户预先知道尺寸名

`set_dimension` 原来只吃 `full_name: 'D1@Sketch1@Part1.SLDPRT'` —— 可是  
「先把尺寸名查出来再写进第二个计划」在单份计划里根本做不到（`resolve()` 只认  
`params.*`，没有跨步骤引用）。于是加上两种**自寻址**方式：

- `sketch: "DiscSk"` + `index: 0`  → 该草图第 N 个尺寸（最常用）
- `match: "D1@"`                   → 子串匹配，必须**正好命中 1 个**

命中 0 个或 2 个以上都**抛错并把候选清单打出来** —— 宁可停下来，也不猜。

另外一条硬事实：**AddToDB 模式下 SolidWorks 不推断约束、不自动加尺寸**。  
矩形四条边是四根独立直线，改其中一条长度**不会**带动其他三条。  
所以「标注 + 改尺寸」这条参数化路线只对**圆/弧这类单图元**天然成立，  
多图元的要靠真正的约束（那就得关掉 AddToDB，代价是慢一个量级）。

## 42. 镜像特征的 mark 是 **1**，不是 4 —— 文档写反了

`IFeatureManager.InsertMirrorFeature` 的公开说法是「要镜像的特征选 Mark=4、  
镜像面选 Mark=2」。照这个写，**8 种组合全部静默无操作**：不抛异常、不建特征、  
体积一点不变，只在选择集里看到 `mark=4 type=22`（22 = swSelBODYFEATURES），  
看起来「选对了」。

真机把 mark 全扫一遍才知道：

| 种子特征 Mark | 镜像面 Mark  | 结果                |
| --------- | --------- | ----------------- |
| **1**     | 2         | **建出「镜向1」，错误码 0** |
| 4         | 2 / 1 / 0 | 无新增               |
| 1         | 1 / 0     | （未扫到，1+2 已成功）     |

同一件事在 `mirror_body`（实体镜像）上也有镜像版：那条路要求 **Mark=128**。  
所&#x4EE5;**「要镜像的东西」该用哪个 mark，是每套 API 各不相同的**，  
不能从一条 API 推另一条。

**方法论**：这不是「猜参数」能解决的，是「穷举 mark」。写法上要满足两条 ——  
① 每次轮试**重新选一遍**（选择集会被 SW 消费掉）；  
② 把 `SelectionManager.GetSelectedObjectMark` 打出来存档，否则  
「无新增但不报错」这种失败根本无从下手。

顺带：这个零件的切除特征类型名被 `GetTypeName2()` 报成 `ICE`  
（Instant3D 类型，**不是错误**，真类型看 `GetTypeName()`）——  
mark 扫描时特征名照样能按 `Name` 选到，不受影响。


## 43. `SelectByID2` 的 `Append=False` 会**清空整个选择集** —— 阵列 op 卡最久的真凶

两个阵列 op（`circular_pattern` / `linear_pattern`）在 M2 里怎么调都不建特征：  
不抛异常、不报错、特征树不动。中途走过三条冤枉路 ——

1. 以为 mark 用错了 → 穷举 18 种 (种子 mark × 方向 mark) 组合，**全灭**；
2. 以为 API 太老 → 发现 `FeatureCircularPattern5` 确实是 **Obsolete**，  
   改走官方指的 `CreateFeature` + `ICircularPatternFeatureData`，结果  
   `AccessSelections` 抛 `com_error(-2147417851 服务器出现意外情况)`；
3. 换宏录制那条 `IModelDoc2.FeatureCirPattern` —— **建出特征了**（错误码 51），  
   但换成参考轴又什么都不建。

真正的根因在最后一步才露头：**把选择集 dump 出来**。

```
选择集: ['mark=1 type=5']        ← 只有一根轴！种子特征没了
```

`_select_ref_axis()` 里 `SelectByID2(..., Append=False, ...)` —— 我为了"干净"写死了  
`False`。而阵列的调用顺序是「先选种子特征 (Mark=4)，**再**选方向参考 (Mark=1)」，  
于是第二步把第一步的选择**整片清掉**了。特征标记全对、API 全对，  
就是选择集里只剩一项。

改成 `Append=True` 后：`circular_pattern` 立刻 17/17 通过，`linear_pattern`  
换成参考轴也 16/16 通过。

**可复用的三条规矩**：

- 凡是「多选」的 API（阵列、镜像、扫描、放样、拔模…），选第二个对象时  
  `Append` **必须是 True**。`Select4` 的 `Append` 参数同理。
- **调完多选，dump 一次选择集**：  
  `SelectionManager.GetSelectedObjectCount2(-1)` + `GetSelectedObjectMark(i)`。  
  数量不对就是选择集被冲了 —— 这一眼比读十遍参数说明有用。  
  （镜像那边也是靠 dump 才发现 `mark=4 type=22` 其实选对了，问题在别处。）
- 函数参数用 `append: bool = True` 做默认值时，是**刻意的**；如果哪天要改成  
  `False`，先想清楚调用方是不是"先选别的再选这个"的顺序。

## 44. 附带产出：阵列该走哪条 API（本机实测的优先级）

| 路线                                                                                   | 结果                                                                            |
| ------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------- |
| `IFeatureManager.FeatureCircularPattern5` / `FeatureLinearPattern5`                  | CHM 标 **Obsolete**；18 种 mark 组合**全部静默无操作**                                    |
| `IFeatureManager.CreateDefinition` + `ICircularPatternFeatureData` + `CreateFeature` | `AccessSelections` 抛 `com_error(-2147417851)`；直接设 `PatternFeatureArray` 也抛同一条 |
| `IModelDoc2.FeatureCirPattern` / `FeatureLinearPattern`                              | **✅ 能用**（录宏录出来的就是这条，返回 void，靠特征树 diff 判成败）                                    |

**方向参考**：用基准轴（`axis_from_planes`）干净通过；用**实体边**能建出特征但  
**重建报错 51**。所以线性阵列的 `direction` 优先给轴名，`direction_point` 只作备选。

**`FeatureCirPattern` 的 `Spacing` 是「相邻实例夹角」，不是「总角度」** ——  
跟新版签名的语义**正好相反**。整圈 n 等分 = 总角 / n。这条写错了不会报错，  
只会阵列出错误的分布，所以 op 内部统一收「总角度」，自己换算。

**`InsertAxis2` 建的轴名字是界面语言决定的**（中文「基准轴1」），不是调用方给的  
`Axis1`。`axis_from_planes` 现在会**真的改名**并把真名放进返回值；  
选轴也要退到「特征树里所有 `RefAxis` 逐个试」，不能只按名字选。

## 45. 已知边界：`CustomPropertyManager` 要的是**配置名**，中文版不叫 "Default"

`IModelDocExtension.CustomPropertyManager(ConfigName)` 传 `"Default"`，  
在中文界面的 SolidWorks 上**返回 None**，然后炸在下一行：  
`'NoneType' object has no attribute 'Add3'` —— 报错指向 `Add3`，  
根因却在一层之外的配置名。

正确做法是问文档要活动配置的名字（`ConfigurationManager.ActiveConfiguration.Name`，  
中文版回「默认」），`set_custom_property` 现在 `config` 留空即自动解析。

**这类"配置名/界面语言相关"的入参是中文版用户的专属坑**：  
基准面（前视/上视/右视）、配置（默认）、基准轴（基准轴1）、  
草图自动名（草图1）—— 一律**问文档要真名**，别硬编码英文默认值。

## 46. `InsertFeatureChamfer`：距离填哪个槽由 ChamferType 决定，**只有 1 号真的去料**

`chamfer` 一直"能建出特征、`GetErrorCode` = 0"，但**一丁点料都不去**（体积一字不变）。  
真机上把 6 个变体逐个试过、每次量体积，唯一能走通的只有一条：

| 变体                                                       | 结果                         |
| -------------------------------------------------------- | -------------------------- |
| `swChamferAngleDistance`(1) + `Width`=d + `Angle`=θ      | **ΔV = −360.0 mm³** ✅ 唯一成功 |
| `swChamferEqualDistance`(16) + `Width`=d                 | ΔV = 0，静默只加特征名 ❌           |
| `swChamferEqualDistance`(16) + `OtherDist`=d             | ΔV = 0 ❌                   |
| `swChamferEqualDistance`(16) + `OtherDist`=d + `Angle`=θ | ΔV = 0 ❌                   |
| `swChamferAngleDistance`(1) + `OtherDist`=d + `Angle`=θ  | **特征根本不生成** ❌              |

（−360 mm³ 与手算 2×½×3×3×40 = 360 一字不差，说明这条路是真的在切。）

**官方参数说明在这里会把人气死**：`api-signatures.md` 里抄的原文写着  
"`OtherDist`: If ChamferType is `swChamferEqualDistance`, then you can specify a single value…"，  
照它填 `OtherDist` 在 SW2024 上**实测完全不生效**。  
结论：45° 的等距倒角本来就等于角度-距离型，**统一走 `swChamferAngleDistance`(1)**，  
即 `(Options, 1, d, θ, 0, 0, 0, 0)`，**别再用 16 号**。

★ 比结论更重要的是这类错的长相：**参数个数对、类型对、错误码是 0**，  
唯一能识破的方式是**量几何量**（体积/质量），而不是读返回值（同 §31、§38）。

## 47. 去料 op 的「体积没减少」必须是**硬失败**，不能只写进 notes

发现 §46 的原因不是探针报错，而是**探针通过了**。  
`fillet`/`chamfer` 本来都写了这句判断，但只是 `notes.append("体积没减少…")`，  
而 notes **不影响通过与否** —— 于是一个"什么都没干"的倒角拿到了 `verified=True`，  
在验收表上躺了一整轮。

**规则**：凡是**去料** op（圆角/倒角/抽壳/切除），只要  
`(体积前 − 体积后) < 1 mm³` 就是**失败**，直接 `raise SwError`。  
"特征建出来了"永远不等于"做对了"。

## 48. `_volume()` 的单位是 **m³** —— 阈值和文案最容易在这里栽

`_volume()` / `mass_props()["volume_m3"]` 给的是 **m³**（op 返回的  
`volume_before/volume_after` 也一律按 m³，别单独改一个）。把阈值按 mm³ 的直觉写成  
`v1 > v0 - 1e-6`，`1e-6 m³ = **1000 mm³**` —— 一个正常的 360 mm³ 倒角会被判成  
"没去料"，反过来白报错；文案里写 `{v0:.3f} mm³` 更离谱，23640 mm³ 显示成 `0.000`，  
看起来像"读不到体积"。

现在的写法：阈值 `(v0 - v1) * 1e9 < 1.0`（按 mm³ 想），文案 `{v0 * 1e9:.2f} mm³`。  
**只要出现 `mm³` 字样，就必须有一个 `* 1e9` 配着它。**

## 49. 常量名拼错会**静默退到默认值** —— 已用离线断言兜住

`C.get("swChamferEqualDistance", 16)` 这种写法有个隐蔽的坑：  
**名字写错/不存在时不会报错，只会安静地用默认值**，  
于是参数悄悄变成 0 或别的错数，而没有任何提示。  
（本例里 `swChamferEqualDistance` 这个名字是**对的**、值 16 也没错，  
坑在参数槽位；但这个写法本身确实会吞掉拼写错误。）

`sw_selftest.py` 现在有一条硬断言：扫描 `sw_ops/sw_gear/sw_plan` 里所有  
`C.get("swXxx")` 字面量，**每一个都必须在 `assets/sw_enums.json` 里查得到**。  
加了它以后再也不会出现"常量名写错但一路静默"。

---

## 螺纹三条（M3，2026-09-25 真机实测，跑了 diag_thread1~11）

这三条都是"参数静默失效"家族的新成员，和 §19、§46、§49 同源：**返回值对、错误码 0、  
特征也建出来了，但东西没做对**。

### 50. `IThreadFeatureData.Size` 是彻底的 no-op

写 `"M10"` / `"M6"` 都被**静默丢弃**：Ø10 圆柱配 M10 与配 M6，**体积减少一模一样**。  
落库后 `GetDefinition().Size` 还会回一个莫名其妙的值（实测 `"M1.2x0.25"`）。  
真正驱动几何的是 **`Diameter` + `Pitch`**，所以 `real_thread` **故意不提供 size 参数**。

### 51. `Diameter` / `Pitch` 只有在 `*Override` 标志**已为 True** 时才写得进去

这是本轮最贵的一条。设置顺序反了，值会被**静默忽略**：

```
O1（先标志后值）：DiameterOverride=True → Diameter=0.003  → 建出 SweepThread，ΔV=−6.921 mm³  ✓
O2（先值后标志）：Diameter=0.003 → DiameterOverride=True  → 读回来还是 0.0，
                 于是螺距退到默认 10mm，套在 Ø3 的轴上不合法 ⇒ CreateFeature
                 **什么都不建、也不报错**，ΔV=0.000  ✗
```

`real_thread` 里那个 `want` 字典的**顺序是硬约束**（`DiameterOverride` 必须排在  
`Diameter` 前面），改顺序就会出现"特征不见了"这种毫无线索的失败。

### 52. 必须选**圆边**，不是圆柱面；`AccessSelections` 报错不致命

- 选**面**时 `CreateFeature` 回 `None`、什么都不建。目标用**该圆边的圆心坐标**给。
- `AccessSelections` 在晚期绑定下**必定**抛 `com_error(-2147417851)`，  
  但**不致命** —— 选择已经在文档里了，`CreateFeature` 照样吃得到。吞掉、记录、不算失败。
- 顺序也是硬约束：**先 `AccessSelections`、再设参数**。反过来特征建不出来  
  （`AccessSelections` 会重置数据，这也解释了 `Size` 为什么会被"吃掉"）。
- `_thread_profile_dir()` 不能"问 SolidWorks 要默认值"：新建的 `IThreadFeatureData`  
  上 `Type` 是**空串**，只有特征真正落库后 `GetDefinition()` 才读得到全路径。  
  只能按版本号推（`RevisionNumber` N ↔ 年份 1992+N）。


## 53. ★ 螺纹会让**包围盒虚报** —— 判"牙顶齐不齐轴径"必须换判据

**这条最坑，因为它长得像"几何错了"。**

现象：Ø3 光杆做完 M3×0.5 后，包围盒从 `[3.0, 3.0, 22.0]` 变成  
`[3.0, **3.4641**, 22.0]`。

- `3.4641 = 2√3`，恰好是「对边宽 = 轴径」的**正六边形对角**尺寸，看起来像多长出一圈棱；
- 而且**只有 Y 向长，X 向纹丝不动** —— 极像真的出错。

追查链（照这个顺序走，省得重来）：

| 步 | 手段                                 | 结论                                                                                                                                |
| - | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| ① | `GetBodies2(0, True)` 数实体          | 只有 **1 个**实体 ⇒ 不是"多了个怪实体"                                                                                                         |
| ② | 抑制/删除螺纹特征再看盒子                      | `SetSuppression2` 在晚期绑定下**静默不生效**（`IsSuppressed` 一直 false，状态码 0/1/2/3 都试过）；改用 `EditDelete` 删掉特征 → 盒子**立刻回到 3.0**、体积复原 ⇒ 确是螺纹特征引起的 |
| ③ | 逐个面扫（`GetFirstFace`/`GetNextFace`） | 凸出的 3 个面 `ISurface.Identity()` 全是 **4006（B 样条）**；主圆柱面半径**仍是 1.5**、它自己的盒子也仍是 ±1.5；全程**没有大块平面**（真长六棱面会有 6 个平面，实测只有 0.019mm² 的碎面）    |
| ④ | `IModelDoc2.SaveBMP` 抓视口看图         | **实体就是个干净螺纹杆**，外形是标准圆柱，没有凸棱                                                                                                       |

⇒ **`IBody2.GetBodyBox` / `IFace2.GetBox` 对 B 样条面报的是「未裁剪曲面」的盒子，  
几何是对的、盒子在撒谎。**

**规则**：凡涉及**螺纹/螺旋面**（B 样条面），判据一律用  
**圆柱面半径不变式** `check_cyl_radius`（`real_thread` 内部也已强制：  
建螺纹前后取最大圆柱面半径，涨超 0.02mm 就抛），**不要用 `check_bbox`**。

顺带两条可复用的手艺：

- **`IModelDoc2.SaveBMP(路径, 宽, 高)` 能把 SolidWorks 视口直接存图**，  
  不用截屏、不依赖窗口激活 —— 数值吵不出结论时就该看图（BMP 用 Pillow 转 PNG 即可）。
- **`IFeature.SetSuppression2` 在晚期绑定下不可信**：要"暂时去掉一个特征"就  
  `Select2` + `EditDelete`，别指望抑制（本次实测 4 个状态码全都没生效）。

## 54. 草图约束：`AddToDB` 不影响约束；`FullyDefineSketch` 的返回码也会撒谎

- **`AddToDB` 与约束无关**（原先的假设是错的）：`diag_addtodb.py` 实测 True/False 两种  
  取值下 `GetConstrainedStatus` **都是 2（欠定义）**。改默认值是白改。
- `ISketchManager.FullyDefineSketch(...)` **返回 `0 Success`，草图却仍是欠定义** ——  
  当草图**没有锚到原点**时它就是不收敛。所以 `fully_define` op **必须读回**  
  `ISketch.GetConstrainedStatus()`（3 = 完全定义），`require=true` 时不等于 3 直接抛。  
  把矩形锚到原点后：状态 2→3、尺寸自动出现、`set_dimension` 40→60 生效。
- `GetSketchSegments` 在**活动草图**上读不到东西，没法直接量"几何有没有动"；  
  可以拿 **`set_dimension` 能不能驱动几何**当"约束够不够"的替代判据。

## 55. 特征名中英互转：**基准面不能用缩写匹配**

`sw_names.json` 是命名权威表，`sw_naming.match_auto()` 只认 `{n}` 模板名  
（`草图1` / `Sketch1`）。于是**基准面（`上视基准面`）会被误判成"显式命名"而跳过归一**。  
修法：基准面单独走 `match_default_plane()` —— **只匹配官方中/英全名**  
（`上视基准面` / `Top Plane`），**不匹配 `Top` / `Front` 这类缩写**，  
否则用户自己起的名字会被误改。  
（同一个 probe 里还栽在"期望值写错"上：在**上视基准面**上拉伸，深度落在**全局 Y**，  
不是 Z —— 期望盒子应该是 `[20, 5, 20]` 而不是 `[20, 20, 5]`。自己算错期望值最费时间。）


## 56. ★ 尺寸"标了却看不见"：**放置点比 API 重要得多**

用户现场原话：**「还是没显示尺寸参数」**，附一张草图截图 —— 圆画好了、`dimension`  
跑过了、包围盒也会跟着变，但视口里**就是没有尺寸**。

根因不在 API，在**尺寸文字放在哪**。旧实现固定把文字放在 `(0,0,0)`：

```
AddAlongXDimension(0, 0, 0)        → 尺寸确实建出来了（D1，读回 30），
                                     但文字压在**原点**上；
                                     而 `circle(cx=0, cy=0)` 的圆心也在原点
                                     ⇒ 文字 + 原点标记 + 圆心点叠成一团灰影。
AddDiameterDimension2(19.5,19.5)   → 清清楚楚。
```

`diag_dim1` 把五种标注 API × 原点/偏置两种放置全跑了一遍，结论：

- **五种 API 都能建出尺寸、值也都对**（`AddAlongX`→30 直径 / `AddRadial`→15 半径 /  
  `AddDiameter`→30 / `AddDimension2`→30）—— 所以**换 API 解决不了这个问题**；
- 原点放置 → 全部"看不见"；偏置放置 → 全部可见；
- 只有放大截图才看清「Ø30」原来真的在原点标记里。

修法（`sw_ops.op_dimension`）：

1. 按 `ISketchSegment.GetType()` 挑标注方式：圆/弧 → `AddDiameterDimension2`、  
   直线 → `AddDimension2`；
2. **自动算放置点**：圆 = 圆心 + `(r+m, r+m)`；直线 = 中点 + 法向 `m`；  
   `m` 默认 `max(4, 0.3×尺寸)`；
3. 加**硬断言**：自动放置点落在圆内就抛 —— 把这个 bug 类型钉死，防止改坏公式后  
   又悄悄退回"看不见"。

> **教训**：这是"数值全绿、界面全错"的典型。`check_bbox` / `set_dimension` 全都通过，  
> 因为几何真的是对的 —— **只有截图能证伪**。所以 `screenshot` 是 `verify` 类 op，  
> 探针里必须在**草图还开着的时候**截一张（见 §60）。

## 57. 想"完全定义"：**只能固定圆心点，不能固定图元本身**

`diag_dim5` 四条路径实跑对照（Ø30 圆 + 直径尺寸，圆心在原点）：

| 做法                                      | 结果                                               |
| --------------------------------------- | ------------------------------------------------ |
| 圆心点 + `SketchAddConstraints("sgFIXED")` | **3 FullyConstrained**，直径仍可驱动（Ø30→Ø40 读回 40.0）✓  |
| 圆心点 + `sgCOINCIDENT`（单个实体）              | 2 UnderConstrained（无效，重合要两个实体）                   |
| `ISketch.ConstrainAll()`                | 2 UnderConstrained（返回 0，但什么也没做）                  |
| `ISketch.AutoDimension2(1,0,0,0,0)`     | 2 UnderConstrained（返回 1，什么也没加）                   |
| **整个圆** + `sgFIXED`                     | **4 OverConstrained**，直径被**锁死**（改 40 读回 **30**）✗ |

所以 `dimension(anchor: true)` 的实现是「选圆心点 → `sgFIXED`」，  
**绝不是**把图元整个固定。返回值里带 `status_before/after` 和 `fully_defined`，  
照旧**读回状态码才算数**（`ConstrainAll` / `AutoDimension2` 的返回码全在说谎）。

## 58. `Point1@Origin` **选不中** —— 拿不到原点实体

本来想走最正统的「圆心点 + 原点 重合」，`diag_dim4` 把能想到的选法全试了一遍：

```
sw.select("Point1@Origin", "origin"        , append=True)  → False
sw.select("Point1@Origin", "EXTSKETPOINT"  , append=True)  → False
sw.select("Point1@Origin", "sketchpoint"   , append=True)  → False
sw.select("Point1@Origin", "origin"        , append=False) → False
Extension.SelectByID2("Point1@Origin", "EXTSKETPOINT", 0,0,0, True, 0, None, 0) → False
```

**五种变体全部返回 False**，而且 `append=True` 失败时还会把已有的选择**清空**  
（选中数从 1 掉到 0）—— 比单纯失败更坑。

⇒ 结论：**本机取不到草图里的原点实体**，`sgCOINCIDENT` 这条路走不通，  
改用 §57 的 `sgFIXED`。另外注意选择状态**不是确定性的**：同一段代码两次跑，  
一次选中 2 个、一次 0 个 —— 凡是要靠选择的约束，都要**数一下选中个数再动手**。

## 59. `EditRebuild3` 会**把活动草图退出编辑**

在草图编辑状态里调 `IModelDoc2.EditRebuild3` 重建 —— 视口里草图会从  
蓝色（编辑中）变成灰色细线，`GetActiveSketch2` 变 `None`，**尺寸随之隐藏**。  
`diag_dim2` 就是被这个骗了：以为"改值把尺寸弄没了"。

想在**不退出草图**的前提下重建/刷新，用 `IModelDoc2.EditSketch`；  
纯刷新视图用 `IModelView.GraphicsRedraw2`。

## 60. 草图**关闭后**尺寸默认不显示（截图时机）

尺寸只在**编辑草图时**画出来。`sketch_end` 之后同样一张图，尺寸就没了 ——  
`diag_dim1` 里六张 `*_sketchclosed.png` 的 md5 全都一样，就是这个原因。

所以验收"尺寸可见"必须在 `sketch_end` **之前**截图。探针 `smart_dim.json`  
的 `shot_open` 步骤放在 `check_sketch_constraint` 之后、`sketch_end` 之前，就是这个道理。

## 61. 盲孔切除**原先没有任何方向备选**（齿轮环形凹槽真机栽在这）

`_extrude_variants` 早年只给「完全贯穿」类加了备选：主方案失败就换  
`单向完全贯穿+反向` / `双向完全贯穿两者` / `两侧对称±100mm`。  
**盲孔（`swEndCondBlind`）一个备选都没有** —— 主方案返回 `None` 就直接抛。

而「草图平面骑在实体上、方向却朝着空气」恰恰是**盲切最常见的失败**。  
减速器大齿轮的环形凹槽（Ø48↔Ø94，8.5 深）就是这样：  
一直等到补了 `备选1 盲孔+反向` 才切上。

★ 补备选时的**红线**：盲孔的备选**只准翻方向，绝不准改终止条件**。  
把盲孔"兜底"成完全贯穿会把零件切穿，而现有的「体积必须变小」这道闸门  
**照样放行**（体积确实变小了）—— 于是得到一个"成功"的错零件。宁可失败得响一点。

```
# scripts/sw_ops.py  _extrude_variants()
elif base == BLIND and depth > 0:
    out.append(one(BLIND, True, not rev, depth, 0, 0.0, "备选1 盲孔+反向"))
```

## 62. 报错别把「轮廓数 > 1」一律说成错误

`_extrude` 失败时的提示原本写「实测该草图有 N 个独立轮廓（正常应为 1 个）」，  
接着就引导去看"多段图元没粘合"。

**这话对环形件是错的**：环形凹槽、法兰、带孔板、垫圈天生就是  
2 个以上**嵌套**轮廓，SolidWorks 照常拉伸。齿轮探针第一次失败时，  
这条错误提示把方向问题（真正原因）指成了轮廓问题，白绕一圈。

改后的提示先声明"多轮廓本身多半是正常的"，只在报「未闭合/自相交」时才怀疑端点未粘合  
或两个闭环**重叠**。

## 63. `check_sketch` 只能在 `sketch_end` **之后**调

它只能按名字走 `FeatureByName` 读**已闭合**的草图；  
草图还开着的时候既没有名字可查、也没有 `last_sketch` —— 直接报  
「没给草图名，也拿不到「刚画完的草图」」。齿轮探针第一次就栽在步骤顺序上。

（对照：`check_sketch_constraint` 草图开着时读的是**活动草图**，反而是要在  
`sketch_end` 之前调才能验"完全定义"。这两个 op 的时机要求正好相反。）

## 64. 从**参照件**反推结构：读圆柱面站位 + 平面站位，别读草图图元

用户给的口径是"跟图纸/参照件"，而本机图纸目录里只有成品 SLDPRT。  
把成品当口径时，**这两条路才走得通**：

- **逐圆柱面** `ISurface.IsCylinder` + `CylinderParams`（半径）+ `IFace2.GetBox`  
  → **完整**圆柱面的盒子是精确的 ⇒ 轴向分量就是这一级的**起止站位**，  
  一整张阶梯轴台阶表就出来了（参照件 20-轴 → 7 级 + 两端倒角，总长 144 分毫不差）。
- **逐平面** `ISurface.IsPlane` + `PlaneParams`（法向/站位）+ `GetArea`  
  → 齿轮的**辐板厚**就是这么定死的：凹槽底面在 Z=±4.5（面积 5130.22 = π/4(94²−48²)）⇒  
  辐板 9.0 厚、两侧各挖 8.5。单看圆柱面永远看不出这一层。

**不要**去读草图图元：晚期绑定下 `ISketch.GetLines2` 回空、  
`GetArcs2` 回一堆 float（`'float' object has no attribute 'InvokeTypes'`）。  
`ref_profile.py` 就是这么白跑一趟的。

★ 半圆/局部圆柱面的 `GetBox` 是**不能信**的（只覆盖那段弧），  
所以判半径一律用 `CylinderParams`，盒子只用来取**轴向**站位。

**★ 已封装成工具**：`python scripts/sw_faces.py --doc xx.SLDPRT [--json out.json]`  
（v1.4.2 新增，只读）。它一次打出上面三张表 —— ①圆柱面台阶表 ②平面站位表 ③锥面清单 ——  
做箱体/箱盖这种面数多的大件时不必再临时写取证脚本。

## 65. 端部倒角是**外扩**：总长 = Σl + 2×chamfer

`shaft_profile` 的 `end_chamfer` 第一版按「内切」实现（倒角吃掉段表两头各 ec），  
结果总长只有 Σl = 140，对不上参照件的 144。**几何本身没错，是画法定义错了**，  
肉眼根本看不出来，只有量总长才发现。

正确定义：段表里的 `l` 只算**圆柱段**长度，45° 倒角额外各加 ec ⇒ 总长 = Σl + 2ec。  
参照件 20-轴 就是 140 + 2×2 = 144。

## 66. 闭环圆边**没有顶点** → 按坐标选它很脆

`_edge_mid` 对带顶点的边取两端点平均（稳）；对**闭环边**（圆柱口那种）  
只能退到 `ICurve.Evaluate2((t0+t1)/2)` —— **曲线参数中点**，  
位置取决于参数化起点，不可预测。

所以"画完再调 `chamfer` 给端面倒角"这条路人不可靠。  
正确做法：**把倒角画进剖面**（`shaft_profile` 的 `end_chamfer` 就是这么实现的）。

## 67. `zoom_fit` 一次可能用**过期包围盒**（长而细的件只装一半）

144×38 的输出轴，`sketch_begin → shaft_profile → zoom_fit → screenshot`  
拍出来只有左半截，右侧全在画外。几何没问题 —— `ViewZoomtofit2` 那次用的是  
**过期**的包围盒。连着调两次就正常。

宽高比越极端（细长轴、薄板）越容易触发。截图证据不理想时先怀疑这个，  
别去改几何。（`screenshot` 是纯视觉 op，验不了数值，但视觉证据站不住脚时  
人会误判成"模型没建对"。）


## 68. ★★ 尺寸要**分两套落点**：横向跨竖线、纵向跨轴线+母线（v1.4.2 真机血泪）

阶梯轴半剖面（`dim_auto_shaft` 探针）标尺寸时，**单图元的  
`AddAlongXDimension` / `AddAlongYDimension` 会互相顶掉、甚至挂错图元**：

- 同一根母线先挂 X（台阶定位）、再挂 Y（到轴距）⇒ **后挂的被静默拒绝**。  
  实测 seg3 先 `AddAlongX` 得 20，再 `AddAlongY` 想设 15 ⇒ 值停在 20。
- 更要命的是**重进过草图之后**（`revolve_boss` 后 `dim_auto` 会自己退出再重进），  
  `AddAlongYDimension` 会把尺寸建在**邻居图元**上：  
  seg3 的调用返回 `before=20.0`（那是 seg4 的值！），  
  于是 `SetSystemValue3(15)` 静默失败、半径停在 20，  
  最终 `check_bbox` X = 39.983 而不是 50。
- 反过来 seg4 的 `AddAlongX` 读到的是 seg3 的 Y（15）。  
  **返回值和 `SetSystemValue3` 都不会报错** —— 只能靠量几何（`check_bbox`）发现。

**正解**：两套尺寸落点彻底分开，都用 `AddDimension2` **跨图元**量距：

| 尺寸     | 量什么                   | 文字放哪                      |
| ------ | --------------------- | ------------------------- |
| 总长     | 左端面**竖线** ↔ 右端面**竖线** | 轮廓上方最外圈                   |
| 台阶定位   | 基准端**竖线** ↔ 台阶**竖线**  | 轮廓上方里圈（与总长平行、再抬一个 margin） |
| 半径 → Ø | **轴线** ↔ 该段**母线**     | 轮廓右侧                      |

只有拿不到竖线 / 轴线时才退回单图元 `AddAlong*`（保留降级路径）。  
实测四条值 `50 / 30 / 10 / 15`，且每条 `value_mm_before` = 目标值（不再串邻居）。

**判据**：`AddDimension2` 量两条**平行线**之间的间距时语义是确定的  
（`GetType()==0` 线性尺寸）；量两条**水平线**会退化成弧长（`before=1570.79=100π`）——  
所以"半径"必须拿**轴线（水平）+ 母线（水平）** 量 Y 间距，而不是拿两条轮廓线瞎试。

**衍生的坑**：`_span_pair` 取竖线时要**排除构造几何/中心线**，  
否则"轴线那条线"（`axis=True` 且非构造）和"中心线"（构造）会混淆。

## 69. 尺寸链的**落点分离**顺带解决"文字叠字"

`AddDimension2` 的文字点就是它返回的尺寸的放置点，  
**不同尺寸给同一个点会叠在一起**（截图里 `30` 和 `50` 会重叠）。

做法：总长 margin 大、台阶定位 margin 小、半径放右侧 —— 天然分层。  
`_between_place()` 里 `alongX` 用 `max(top, cy) + margin`、`alongY` 用右侧，  
就是这个用途。

## 70. ★ 用户口径：**「不是每个尺寸都标」**（图纸习惯 > 逐段标全）

用户给了图纸习惯截图：阶梯轴半剖面只标 `20`（台阶定位）/ `10`+`15`（半径）/ `50`（总长），  
**中间段长一个都不出现**。这不是省事，是**正确性**：  
逐段把 6 条线长全标上 ⇒ Σ(前5段) = 总长 ⇒ **过定义**，  
SW 会**静默拒标**（op 报 `n_dimmed=6`，模型里只剩 1 个尺寸）。

尺寸链规则（回转体半剖面）：

1. **总长**只标一次（跨两端面）；
2. **台阶定位**每条台阶竖线一次（相对基准端）；中间段长 = 派生量 ⇒ 不标；
3. 各段**半径/直径**一次（轴线↔母线）。

判据仍是**圆柱面**：母线（半径≈圆柱面半径）参与定位+半径；  
端面（近似圆/样条）只参与总长 —— 齿轮那种采样渐开线没有圆柱面，天然不误标。

## 71. ★ 对账要比**尺寸集合**，不比朝向（人工那套的坐标系朝向是习惯，不是规格）

2026-10-02 打环形件族时踩出来的，5 件同族圆环：

|                | 套筒           | 调整环1            | 调整环2            | 密封圈             | 密封圈2            |
| -------------- | ------------ | --------------- | --------------- | --------------- | --------------- |
| 参照件包围盒         | `[13,37,37]` | **`[47,3,47]`** | **`[62,3,62]`** | **`[46,46,4]`** | **`[33,33,4]`** |
| 我们（Front 平面回转） | `[13,37,37]` | `[3,47,47]`     | `[3,62,62]`     | `[4,46,46]`     | `[4,33,33]`     |

**体积全部吻合到 0.002%**，却有 3 件包围盒"对不上" —— 因为参照件的旋转轴  
落在**不同的世界轴**上（套筒在 X、调整环在 Y、密封圈在 Z）。人工建模时各自  
随手选了活动平面，就留下了各不相同的坐标系。

结论：**坐标系朝向是建模习惯，不是零件规格**。同一个圆环，轴朝 X 还是朝 Z  
是同一件东西（旋转一下而已）。按轴逐一比，会把"坐标系不同"误判成"零件不同" ——  
那正是"校验器退化成误报"。

改法（`sw_reducer.reconcile`）：包围盒按 **`sorted(dims)` 的集合**比，容差不变；  
朝向差异另作**提示**输出（`orientation.same=False` + 两组轴向值），不参与判定。

★ **警告：改成旋转不变 ≠ 放行**。必须同时配反例钉住（`sw_selftest` 的 `(d4)` 节）：  
外径差 1mm → 拦；体积差 5% → 拦；只有"尺寸集合相同、仅朝向不同"才放行。

## 72. ★ 反向取证要**两个独立来源互相印证**（环形件的内孔就是这么定下来的）

环形件的参数表一开始只有外径 `D` + 厚度 `t`，**没有内孔**。内孔是**功能尺寸**  
（轴颈配合面），**绝不能按"看起来像的整数"猜** —— 猜错了整套装配都是错的。

两个**互相独立**的来源，都说同一个数，才敢写进参数表：

1. **面取证**：`sw_faces.py` 读参照件的圆柱面 → 台阶表给出 Ø30 / Ø37 / Ø52 / Ø30 / Ø20；
2. **体积核对**：解析式 `V = π/4 (D² − d²) t` 算出来，与 `reference.json` 的实测体积比。

结果 5 件全部吻合 **0.0002% ~ 0.002%**（例：套筒 4788.57 vs 4788.60）。  
两个来源一致 ⇒ 参数表才落笔，`source: "measured"` 有据。

★ **反面同样重要**：同目录里 `25-挡油环`、`2-垫片1` 也"像环"，但面表显示一个是  
**台阶 + 两个锥面**（Ø44 L3 + Ø20.3 L3），另一个解析圆环差 **5%**（754 vs 716.3）  
⇒ 两件**都没有**收进这一族，只在 `_measured_faces` 里留了实测数据待查。

这就是用户那句「不需要再上网查找到不正确的标准」的同款纪律：  
**宁可少收一件，不可硬套一个"差不多"的值** —— 硬套出来的计划会"看起来全绿"。

## 73. ★ 按"键名"在**整份文件**里找条目，是危险的（同名键会出现在多个块里）

事故（2026-10-02）：写了个小补丁把 `cover2`/`cover4` 的 `status` 改成 `modeled`，  
用的是 `out.find('"cover2":')` 定位。**结果先命中的是 `covers` 块里的 `"cover2":`**  
（那个块在 `parts` 之前），于是顺着往下找 `"status": "todo"`，改到了 `parts` 里  
**第一行 status 还是 todo 的零件** —— 实际改的是 `taper_pin_4x18` 和 `oil_sling`。

后果特别隐蔽：JSON 依然合法、脚本还打印了 `[OK]`、任务"看起来成功了"，  
而**两个真该改的没改、两个不该改的被改了**。肉眼看 diff 都不一定发现。

改法：定位**必须限定在目标区块的行区间内**（先找 `  "parts": {` 到下一个顶层键），  
并且**写完立即回读比对**（`json.loads` 再逐项断言）。  
`_diag/_applied/_patch_status.py` 现在是这个写法，且回读不一致就返回非零。  
（★ 一次性补丁脚本用完全部**归档到 `_diag/_applied/`** —— 留在原地被误重跑一次就会**二次应用**，  
把已经改好的参数再改一遍。这不是整洁问题，是安全边界。）

★ 这次的"回读"真的当场抓到了错位（打印了 `★ 不一致`）——  
**写脚本改数据，一定要有一步"改完回读"**，否则你只有"脚本没报错"这一个薄弱信号。

## 74. ★ 反例必须**点名检查号**；否则新增检查会把老反例"救活"成假绿灯

`sw_reducer._selftest` 原来的判据是"被拦住了就行"（`not r["ok"]`）。  
加了 C11（端盖两表一致）之后，C6 的老反例（改 `covers.cover2.D`）  
**顺带**触发了 C11 —— "拦住了"依然成立，于是**测试还是绿的，  
但它已经不能证明 C6 还在干活了**。这叫假绿灯：校验器悄悄死了，测试还在鼓掌。

改法三条：

1. 反例表从 `(说明, 改动)` 升级成 **`(说明, 应拦截的检查号, 改动)`**（`sw_reducer.MUTANTS`）；
2. 判据改成"**指定的那条**必须出现在失败集合里" —— 被别人拦下算**串台**，同样判失败；
3. C6 的反例改成**两张表一起改**（这样 C11 不出声，C6 才是唯一能拦它的）。

同一批还补了一条**代码↔文档漂移守卫**：代码里已经有 C1~~C11，参数表 `checks` 块  
却停在 C1~~C8（C9/C10/C11 只在代码里）。`sw_selftest` 现在双向断言  
`code_ids == doc_ids`，**新增一条检查忘了登记，自检当场失败** ——  
和 `op_schema.json`、GB 速查表是同一套机制。

★ 通用口径：**任何一个"守卫"都必须见过它失败一次**才算数。  
本次两条守卫都用"故意破坏再跑一遍"验证过（删 C11 → 报缺；塞 C99 → 报多余）。


## 75. ★ 草图状态：**别用颜色当判据**；`expect_min_dims` 会被**从动尺寸**骗过

2026-10-02 对着 SolidWorks **2024 官方帮助**逐条核对（核对表见  
`docs/07-SolidWorks建模-尺寸标注规范.md` §10/§11），三条落地结论：

**① 官方唯一判据是状态栏文字，颜色有两套口径、不能拿来自动判定。**

- 《草图状态规则》原文：「草图的状态显示于 SOLIDWORKS 窗口底端的状态栏上。」
- 《草图几何体状态》图元状况着色：悬空=**褐**、从动=**灰**、项目冲突（冗余尺寸/多余关系）=**黄**、  
  欠定义=**蓝**、完全定义=**黑**、无效=**黄**、项目无法解出=**红**。
- 但《草图选项》里"实体点"着色方案又是「黑=完全定义、蓝=欠定义、**红=过定义**、绿=选定」。  
  ⇒ **过定义在不同显示方案下既有黄也有红**。所以：  
  · 别写"粉色 = 过定义"（**官方无此说法**）；  
  · **别用截图判色当自动验收**（同一状态两种颜色，判定必翻车）；  
  · 自动判据只有一条：`ISketch.GetConstrainedStatus`（映射表见 `docs/07-*` §11.2）。

**② `expect_min_dims` 的假绿灯：从动尺寸也计入 `n_dims`。**  
官方《自动切换为从动尺寸》原文：「如果您标注一个可能导致过定义草图的**封闭轮廓**草图，  
则尺寸**自动切换为从动尺寸**。」——**连"将尺寸设为从动"对话框都不弹**。  
于是可能出现：`dim_auto` 标了 N 个尺寸、`expect_min_dims=N` 通过、状态栏也是"完全定义"，  
但其中某个尺寸是**从动的**（只读、不参与求解）⇒ **改它不会驱动几何**，  
"参数化"这个卖点被静默掏空。这正是 §6.5「40+10+45 却仍完全定义」的同源现象。

- 待验证的补法：`IDisplayDimension.DrivenState`（`swDimensionDrivenState_e`）  
  / 尺寸右键"属性→从动"的读回路径，给 `check_sketch_constraint` 加  
  `expect_all_driving: true` 之类的闸门。**加之前必须真机实测**（见 §74 的守卫口径）。
- 目前口径：`expect_min_dims` 只保证"有 N 个尺寸"，**不保证都是驱动尺寸**，文档里别写过头。

**③ 「必须先完全定义才能建特征」是**本项目自定口&#x5F84;**，不是官方要求。**  
《草图状态规则》原文：「有了 SOLIDWORKS 软件，在使用草图生成特征前，您不需要完全标注或定义草图。」  
官方只提供开关 **系统选项 > 草图 > 使用完全定义草图**（约束的是"生成特征前"，不是"退出草图前"）。  
对外解释 skill 为何强制 `expect_fully_defined` 时，要说清这是**本项目的更严口径**，别声称官方强制。

## 76. ★ 规范核对（2026-10-02）：9 件草图**全部** UnderConstrained；链式口径在"离轴矩形环"上会退化

核完官方帮助、把《07 尺寸标注规范》里可判定的部分固化成 `scripts/sw_spec.py`（规则 S1~S8）之后，  
回头审**已建成的功能**，跑出三类问题。分辨一下"哪类能从计划上看出来"很重要：  
只有 ① 是计划层的（所以 lint 抓得到），②③ **必须真机跑**才发现。

### ① 轴**根本没有标注步骤**（难怪 lint 一跑就报 S2/S3）

`_shaft_plan` 只做了 `check_sketch`（图元/轮廓数），**没有 `dim_auto`、也没有约束闸门** ——  
违反「草图必须用真尺寸画」：这根轴改一个尺寸会乱动，出工程图也带不出尺寸。  
→ 补 `dim_auto(chain=auto)`（排在 revolve 之后）+ `check_sketch_constraint(expect_min_dims=3)`。

### ② `polyline` 一条几何关系都不加，&#x4F46;**"补重合"是无效动作**

`op_polyline` 只循环调 `CreateLine`，一个关系也不加 —— 规范 §3 第③步「先加几何关系」  
此前**没有对应的 op**（只有 `fully_define` 这个不收敛的黑盒）。于是新写了 `sk_relation`。  
真机实测（探针 `spec_relations`，19/19 通过）反而证伪了两个想当然：

- `rel=coincident` → **`added=0`**：`ISketch.GetSketchPoints2` 里**没有坐标重复的点**  
  ⇒ SolidWorks 已经把相邻端点**合并成同一个点**了。"重合"这条关系**不用补**。
- `rel=h_v` → 4 条关系（2 水平 2 垂直）**全部加上**，但约束状态**仍然是 2 UnderConstrained**。
- `rel=fix`（锁离原点最近的顶点）**也不动状态**。

⇒ 结论：这类型面靠"补关系"到不了完全定义 —— 缺的是**定位尺寸**  
（型面相对原点/中心线的位置，规范 §5 #2）。`sk_relation` 的价值是**把 §3 第③步落地**，  
不是"一键完全定义"。

### ③ `dim_auto(chain=auto)` 在**离轴矩形环**上退化 —— 我差点把它当改进引进去

把环形件/闷盖族的标注从 `chain=off` 改成 `chain=auto` 并挪到 `revolve` 之后  
（理由看上去很正当：§70 要求走尺寸链，而链式要靠识别圆柱面 ⇒ 必须先转）。  
**真机一跑就翻车**（探针 `spec_relations`）：

| 口径                | 计划要标                        | 实际落上                  | 草图里读回               |
| ----------------- | --------------------------- | --------------------- | ------------------- |
| `chain=auto`（转之后） | 3 条：总长 13 + 半径 15 + 半径 18.5 | **1 条**（`n_dimmed=1`） | 4 个尺寸**值全是 13.0**   |
| `chain=off`（转之前）  | 逐段                          | 4 条                   | 13 / 3.5 / 13 / 3.5 |

⇒ **回退到 `chain=off`**，并把"为什么只能回退 + 实测数字"写进 step 的 `deviation` 字段。  
`sw_spec` 的 S4 据此改成：**`chain=off` 必须带 `deviation` 说明；不写才判错** ——  
这就是"偏差要标注，不能假装没有"。

★ 同一次实测还**证伪了一个容易过度推广的结论**：`dim_auto(chain=auto)` 在**阶梯轴**  
（贴轴半剖面）上是**能用**的（探针 `dim_auto_shaft` 落 5 条、含半径 10/15）。  
退化的只是**离轴矩形环**。**别拿一个型面的失败去否定整个机制。**

### ④ 遗留缺口（已登记，不在 lint 里假装通过）

- **环形件的径向口径不对**：`chain=off` 标出来的是**边长**（t 与壁厚 3.5），  
  而 t=13 + 壁厚=3.5 **推不出 OD=37 / ID=30** ⇒ `set_dimension` 根本改不了外径/内径。  
  规范 §5 #4 要的是「圆环/套类统一标**直径**（内径、外径）」，需要「尺寸跨中心线 → Ø」这条新路。
- **9 件草图全部 `UnderConstrained(2)`**：缺「型面到原点/中心线的定位尺寸」。
- 两条都进 `references/70-direction-review.md` 待办 #10 / #11。

★ 教训：**"符合规范"不能靠读代码判断，也不能靠"我换了个更规范的写法"。**  
本次三类问题里 ① 静态可判（lint 抓到），②③ 必须真机跑；  
而 ③ 恰恰是"越改越像规范、实际是退步"，只有真机读数能拦下来。

## 77. ★★ 同一个量被**标了两三遍**：`chain=off` 逐段标 + 尺寸**计数**也重复（用户截图驱动）

用户发来两张 SolidWorks 截图：一张矩形环的 `13` 出现在上下两条长边、`3.50` 出现在左右两条短边；  
一张台阶型面的 `5` 出现在两条竖线上 —— **同一个数字标了多遍**。  
把真机读数捞出来（探针报告里的 `check_sketch_constraint.dims`），确认比图上更严重：

| 件         | 实测尺寸值列表                  | 重复情况                 |
| --------- | ------------------------ | -------------------- |
| 套筒 spacer | `13, 13, 3.5, 13, 3.5`   | **13 标了 3 遍**、3.5 两遍 |
| 调整环1/2    | `3, 3, 5, 3, 5`          | 3 三遍、5 两遍            |
| 密封圈1/2    | `4, 4, 8/6.5, 4, 8/6.5`  | 同上                   |
| 平键        | `12, 12, 5, 12, 5`       | 12 三遍、5 两遍           |
| 端盖2/4     | 9 条里含 11.5×2、4.0×2、3.0×2 | 重复                   |

**两个叠加的病因，必须都治：**

### ① 真的重复标注：判据是"**同向 + 同投影区间**"，不是"同长度"

`chain=off` 逐个图元标。**矩形环的对边等长且投影区间完全重合**（上下边都跨 x∈[0,13]）、  
台阶型面的对称竖线同理 ⇒ 同一个量标两遍。  
⇒ 新增 `_dim_signature(geom, as_, value)`（`caliber + 方向 + 投影区间`）+ `_off_dim_goal()`：  
同签名的第二个起 `skipped`。  
★ **判据不能退化成"同长度"** —— 阶梯轴两段**等长但位置不同**的台阶是两个量，必须都留。  
自检 `test_dim_dedup` 直接调生产函数（不抄逻辑）钉住两头：  
矩形环 4 段 → 2 条；等长**异位**台阶 → **4 段全留**。  
（写这条测试时我自己的预期错了一次：两条竖线 y 区间相同 ⇒ 合并是对的 —— 改的是**测试**，不是代码。）

### ② 计数也重复：同一条尺寸给两种全名 ⇒ `n_dims` 虚高，闸门形同虚设

SolidWorks 对同一条尺寸会给两种全名：`D1@轴半剖面@零件99.Part` 与 `D1@轴半剖面`。  
`_all_dimensions` 直接拿 `full_name` 当键 ⇒ **同一条算两次**。  
后果不是"看着丑"，而是 `expect_min_dims` 这道闸门被**虚高的计数**骗过（轴实测 n=15 就是这么来的）。  
⇒ 改用「尺寸名@特征名」归一化键去重，并**保留带文档名的全名**（`@特征@` 过滤还要用它）。

## 78. ★★★ `dim_auto(chain=auto)` 会**打崩 SolidWorks**，且它的收尾是坏的（2026-10-02 真机）

接着 §77 往下查轴的 `dim_auto(chain=auto)`（v1.4.7 加的），真机连跑挖出**两个确定性故障**，  
都在 `chain=auto` 这条路上。**两个都不是"偶尔"，是每次都发生。**


### ① 兜底把尺寸打在空选择上 ⇒ 进程崩

`balance`（总长）/`station`（台阶定位）分支：先试"跨两根竖线"的 `AddDimension2`，  
取不到配对就**退回单图元 `AddAlongXDimension`**。退回前**不看选中结果**：

```
_select_one_strict(sw, ctx, hi["index"])            # ← 返回值被丢掉
got, err = _call(..., "AddAlongXDimension", ...)     # ← 打在空选择上
```

轴的型面两端有 45° 倒角 ⇒ 端面是**斜线**，`_span_pair` 找不到端面竖线（`i1/i2=None`）  
⇒ 必走兜底。而 `geoms` 索引是**重进草图之前**读的：`chain=auto` 会调  
`_remeasure_revolve_feature`（`EditSketch → InsertSketch(False) → EditSketch`），  
重进后图元被重建、**索引失效** ⇒ 选不中 ⇒ 标注打在空选择上 ⇒  
`com_error: (-2147023170 RPC_S_CALL_FAILED)`，紧接 `-2147023174 RPC 服务器不可用`。

> ★ **表现为"`check_bbox` 读不到包围盒（零件可能是空的）"，极易误判成"实体被隐藏"。**  
> 真相是 **SolidWorks 进程已经死了**。判据：崩后连 `GetBodies2` 都返回 **None**  
> （调用本身失败），而不是"返回 0 个实体"。  
> ★ 崩在 API 内部，**异常接不住**（`com_error` 只是被 `_call` 收成字符串）。  
> 唯一办法是**别做这次调用**。

**修复**：`balance` / `station` 的兜底改成"**先确认真的选中了**（`ok_sel` 且  
`GetSelectedObjectCount2(-1)==1`），没选中就 `skipped.append` + `continue`"。  
少标一条 = 能被 `expect_min_dims` 发现的**降级**；崩进程 = 整批回归**全灭**。  
（`span_radius` 的兜底**本来就有**这道检查 ⇒ 它不崩 —— 反证判据选对了。）

### ② 就算没崩，收尾也是坏的：实体被"临时压制"

补上护栏后连跑两次，`dim_auto` **都 OK（28s，落 14 条）**，但 `check_bbox` **仍失败**，两次一致。  
原因：`chain=auto` 必须排在 `revolve` 之后（靠圆柱面识别回转体）⇒ 它一定走  
`_remeasure_revolve_feature` 的"退出→重进草图"，跑完**草图留在打开态** ⇒  
`_bodies()` 回 0（**进草图编辑态时实体被临时压制** —— 与 §58/§60 同源）  
⇒ 下游 `check_bbox` 读不到。计划里补的 `s3 sketch_end` **收不干净**（重进是嵌套的）。

### 结论：轴的标注**退回环形件/闷盖同形**（`chain=off` 前置 + 明示偏差）

轴的 `chain=auto` 要同时满足三件脆弱的事：取到端面见证线、安全重进草图、重进后还能读回实体。  
**三件都脆。** 而环形件/闷盖那 7 件一件都不需要（型面离轴、标注排在 `sketch_end` 之前、  
全程不进草图编辑态），已真机验通。  
⇒ 轴改回同形：`dim_auto(chain="off")` 排在 `sketch_end` **之前**，  
`check_sketch_constraint` 排在 `sketch_end` **之后**、`revolve` **之前**；按 S4 补 `deviation`。  
⇒ **本版（1.4.8）没有任何交付件使用 `chain=auto`**。该路保留但**已隔离**（不再有交付件走它），  
待"端面见证 + 重进草图收尾"分别修好再启用。

★ 教训（比 §76 更硬）：**"能跑通一次"不等于"能用"。**  
`chain=auto` 在阶梯轴上（探针 `dim_auto_shaft`）曾经落过 5 条尺寸 —— 那只是  
"**那一次恰好的见证线配对成功**"。**判据必须落到"连跑两次一致"**，  
否则会把"偶发成功"当能力引进去 —— 这正是 §76 ③ 那个坑的第二次现形。

## 79. ★★ 去重改了口径，**闸门阈值也得跟着重标**；而"去重"差点被当成元凶

修完 §77（同量去重）+ §78（`balance/station` 护栏）后跑 9 件真机回归：**8 件失败**，  
但**一条都不是崩溃** —— 全是 `check_sketch_constraint` 报  
「真尺寸不够：只有 N 个，要求 ≥ M 个」：环/套筒/密封圈**只有 1**（闸门 ≥4）、  
端盖 6（≥8）、平键 2（≥4）。

### ① 阈值是按**去重前**的虚高数标定的 ⇒ 把"变干净"报成了"回归"

那三个 `expect_min_dims` 当初是**实测**标定的 —— 但测的是**去重前**的数：  
环 4（t 与壁厚**各重复 2 次**）、盖 8、键 4。去重把重复项拿掉后，**真实量数**是 2 / 6 / 2。  
闸门还在按旧数要，于是失败。**改回真实量数即可**（环 2 / 盖 6 / 键 2）。

★ 教训：**闸门阈值是"实测值"，它和被测量的口径是绑死的。**  
一旦改了"标什么 / 去重怎么算"，就必须**重新实测 + 重标全部阈值**，  
否则同时得到两个坏结果：真的回归被噪声淹没、假的提升被当回归去修。  
这次靠两条线索认出来：**失败全是同一条闸门** + **数字恰好是旧数的一半左右**。  
⇒ **不看失败的具体内容，就会去乱改去重逻辑**（那才是真退步）。

### ② 真正让环只剩 1 条的，是"去重把**构造几何**也算进去了"

`_off_dim_goal` 的签名去重**没先排除构造几何**：环形的 `centerline` 与型面"长"  
**同长、同向、同区间**（都从 x=0 起），它的签名先占了位 ⇒ 轮到真正的型面长边时  
被判成"重复"跳过 ⇒ 草图里只剩"高"一条（`n_dimmed=1`）。  
修法 = 在算签名**之前** `if g.get("construction"): skipped + continue`。  
修后环落 `[3.0, 5.0]`（长 + 壁厚），自检 `test_dim_dedup` ③ 钉住这条。

★ 这个 bug 和 ① **症状完全一样**（"条数少了"），但**病因相反**：  
① 是阈值过时（改阈值），② 是真丢尺寸（改去重）。  
⇒ **必须看"落在图上的是哪几条、值是多少"**，不能只看"够不够 N 条"。  
`skipped` 里的 `why` 字段就是为这一刻准备的 —— 它直接写着"与已标图元重复（3，同向同区间）"，  
一眼能看出被跳过的是"长"，从而定位到中心线。

## 80. ★★★ 逐段标注会弹**模态框**把自动化挂死、并把型面**改坏** ⇒ 标注能力已封存

> 这是 §77~§79 那条线（"同量标了两三遍"）**最终的结局**：  
> 不是"再修一版标注"，而是**把标注整块关掉**。

### 症状：不是报错，是**卡住**

给 `端盖1`（透盖，**非单调**型面：法兰 → 台阶 → 轴承孔 → 毡圈槽 → 通孔，轴向进进出出）  
跑 `dim_auto(chain=off)`，用户看到 **SolidWorks 弹出确认框、然后界面卡死**；  
Python 侧拿到的是：

```
pywintypes.com_error: (-2147417848, '被调用的对象已与其客户端断开连接。')
```

`-2147417848 = 0x80010108 = RPC_E_DISCONNECTED`。

★ **这个异常名极具误导性**：它长得像"对象生命周期"问题（§"返回值会说谎"那一类），  
真因却是**一个没人点的模态对话框** —— 只要 SW 在等用户回答，COM 通道就是断的。  
判据：**SW 进程还在、但 `GetActiveObject('SldWorks.Application')` 报  
"操作无法使用"** —— 这就是"进程活着、消息泵被模态框占住"的指纹。

### 决定性实验：把 `dim_auto` 剥掉，同一型面 **100% 成功**

`_diag/diag_cover1_revolve.py`（**开发目录，不进发行包**；复现方法见下）  
只画「中心线 + 折线(open) + `arc3p`」，**跳过 dim_auto**，直接回转：

```
[A] 共 19 个图元 …… [A] revolve OK -> {'feature': 'F_A'}     ← 型面本身没问题
[B] 折线 close=True（无弧，对照）…… [B] revolve OK             ← 也不是圆弧的锅
```

而线上那条（`dim_auto` 在内）在 `revolve_boss` 报  
「草图必须有中心线/轴，且轮廓不能穿过轴」—— 连报错都指错地方。

⇒ **结论：几何是对的，坏在"标注"这一步。** 而且坏法有两种：

1. **挂死**（模态框，见上）；
2. **改坏几何**：型面被尺寸驱动挪位（截图上实体扭曲、读出 `10.0075` 这种歪值）。

### 为什么会弹框

`chain=off` 是**逐个图元**标。非单调型面上"同一半径在不相邻的轴向区间出现两次"，  
于是 SW 认为**尺寸冲突/过定义**，弹框问"要不要继续"。矩形环（§76/§79 那些件）形状简单、  
没触发；**非单调回转体必触发** —— 所以这条**只在端盖这种件上现形**，  
前面 9 件"跑绿"并没有证明标注可用。

### 处置：**标注与建模解耦**（2026-10-02，用户决策）

- **建模阶段一条尺寸都不标。** 几何正确性靠 `check_bbox` / `check_cyl_radius` /  
  `check_mass`（透盖族的 `check_mass` 用 **Pappus 2π·r̄·A 从型面现算** —— 型面错一处立刻红）。
- 尺寸改由后&#x7EED;**「图纸绘制」**&#x73AF;节在**工程图**里标（**不在建模草图里标**）。
- 顺带收益：真机回归从 **11 件 ≈ 30 分钟** 掉到 **11 件 146 秒**（`dim_auto` 原本单件 150~190s）。

### 封存的**实现**（三道闸门，不只写在文档里）

| 层   | 位置                                       | 作用                                                        |
| --- | ---------------------------------------- | --------------------------------------------------------- |
| 执行期 | `sw_ops.OpSpec.sealed` + `sw_ops.run_op` | 任何执行路径（`sw_plan` / `sw_probe`）都拦，报错带封存理由                  |
| 计划期 | `sw_spec` 的 **S3**（2026-10-02 **含义翻转**）  | 从"必须有标注"翻成"**禁止**标注"；`SEALED_DIM_OPS` 与 op 的 `sealed` 同口径 |
| 证据层 | `assets/probes/_sealed/`                 | 主题是标注的 8 条探针退役；**仍做离线校验**（挡接口漂移），但默认不进真机跑                 |

**临时放行**（只给自检/诊断）：step 写 `"allow_sealed": true`，或环境变量 `YOYO_ALLOW_SEALED=1`。  
**为什么不直接删代码**：封存的是**入口**，不是**知识**。S4/S5/S8 那套尺寸链口径  
（chain=auto 才读得到圆柱面、离轴矩形环会退化、径向口径要写明）一条都不能丢 ——  
重开那天全靠它。所以自检里那两条反例**从"删掉标注"改写成"塞进标注"**，判据本身不动。

### 重开条件（写清楚，免得下次又踩）

1. 找到一条**不弹模态框**的标注路径（先补几何关系再标 / 拦截确认 / 关掉交互式确认）；
2. 在真机上把**全部**零件重新跑绿 —— 特别是**非单调型面**那几件（它们才是试金石）。


## 81. ★★ 从参照件反推回转轮廓时，**偏心圆**会被静默当成回转母线 ⇒ 拼出假轮廓

**症状**：`sw_profile.py` 对某些件"跑得通"（不报错、链看着是闭合的），  
但 Pappus 体积对不上参照件 —— 或者更糟，**链压根合不上**，  
报出一串"悬空元素"，而**没有任何错误信息说清为什么**。

**实例（2026-10-02，解 D0 级 5 件时一次撞上两个）**：

| 件     | 真相                                      | 误读出来的假轮廓                                                      |
| ----- | --------------------------------------- | ------------------------------------------------------------- |
| 2-垫片1 | Ø34/Ø14 环 ×1 厚 + **3×Ø4 偏心通孔**（分布圆 Ø24） | 5 条边：3 横 2 竖 —— **奇数边，根本闭不了环**；链只剩 1 段，4 段悬空                  |
| 3-反光片 | 平盘 + 球冠反光面 + 偏心孔                        | 球冠被折成 `[14.1967,1.5]→[16.98,1.5]` 的假直线（r=1.5 是**球半径**，不是回转半径） |

**病因**：`_mark_xr()` 直接取 `(c[ax], r)` —— **只用了圆心的轴向分量和半径**，  
完全没管圆心**离回转轴有多远**。回转体的面边界圆圆心必然在轴上（偏移 ≈ 0）；  
偏心圆（不在轴上的孔 / 球冠 / 凸台 / 键槽底）的半径**根本不是**回转半径。  
于是每个偏心孔都往轮廓里塞一条假母线。

**为什么这个错特别危险**：它**不报错**。链合不上时只列"悬空元素个数"，  
很容易被读成"这件的型面比较复杂"，而不是"工具读错了"。  
**只有 Pappus 体积对上参照件才是唯一可靠判据。**

**修法（`sw_profile.py`，两层）**：

1. **过滤**：边界圆按 `_off_axis(mark, ax) = hypot(非轴向分量)` 分组，  
   偏移 > `tol`（默认 0.05mm）的一律**不进轮廓**；
2. **出声**：偏心圆记进 `off_axis` 列表（**里面就有"孔的分布圆半径 + 孔径 + 轴向站位"**），  
   并在 `extract()` 汇总出 `revolvable` / `blockers`。**不是纯回转体就明确判"不可回转"**，  
   让 `sw_reducer._profile_plan` 拒建 —— 宁可判错方向，也不要交一份假轮廓。

**实测判据（偏心圆的副产品反而成了线索）**：

```
2-垫片1     环 Ø34/Ø14 ×1 = 753.98  − 3·π·2²·1 = 716.28   vs 参照 716.3   ✔ 0.003%
4-油面透视片  环 Ø34/Ø4  ×1 = 895.35  − 2·π·2²·1 = 870.22   vs 参照 870.2   ✔ 0.002%
```

⇒ **偏心特征解出来正好用来算"该减几个孔"** —— 孔数由**体积差**定，  
不要靠数标记个数（同一条边会挂在两张面上，个数会重复计数）。

**回归保护**：`sw_selftest.py` 的 `[2f]` 用**人造 face 记录**把四种情况钉住  
（轴上圆正常折元素 / 偏心圆被记下且不混进元素 / 含偏心⇒`revolvable=False` /  
纯圆环⇒`revolvable=True` 且 Pappus 精确）。**同时验证"不许误伤"** ——  
34-套筒 / 22-端盖2 / 19-端盖1 / 28-端盖3 **四件已知全绿的必须仍是 ✔**  
（实测：加了守卫之后四件判定与体积**一字不变**，仍 0.0000~0.0006%）。

---

## 82. ★★ 「回转 + 孔圆周阵列」：端面草图走不通，**基准面草图 + 特征级阵列**才是对的

§81 判出垫片1 / 油面透视片"不是纯回转体"，几何规格也全解开了，就差一个生成器。  
按直觉写「回转出环 → 在端面上画孔 → 圆周阵列 → 切除」，**真机第一步就卡住**。

### ① `sketch_circular_pattern` 在**面**草图上选不中种子

```
[15/23] cp  sketch_circular_pattern  FAIL  圆周阵列前必须先画种子图元
```

报错文案写"ctx.segments 为空"，其实 `circle` 明明画成功了、`ctx.segments` 也有 1 个 ——  
真正的失败是 `_segments_select` 返回 0（`Select4` 之后 `GetSelectedObjectCount2(-1)` 数出来是 0）。  
★ **文案会误导**：看到"segments 为空"别去查 `circle`，要查"选中数是不是 0"。

**换成基准面草图 + 特征级 `circular_pattern` 就通了**，而且直接复用了已真机验证过的探针  
`assets/probes/circular_pattern.json`（Ø60 圆盘 + Ø8 偏心孔 ×6，形状完全同构）：

|      | 端面草图 + 草图级阵列                 | **基准面草图 + 特征级阵列**                           |
| ---- | ---------------------------- | ------------------------------------------- |
| 选面   | 要 `select_face` 射线打点（选不中就全废） | `select_plane` 一个名字搞定                       |
| 草图原点 | 落在哪不确定（会随面的参数化变）             | 就是原点，`(pitch_r, 0)` 距离=分布圆半径                |
| 阵列   | 草图级，选不中种子                    | 特征级 `circular_pattern`（`FeatureCirPattern`） |
| 结论   | ❌ 建不出来                       | ✅ 23/23 步通过                                 |

**面的选法**（回转轴 = 全局 **X**，`check_bbox` 按轴序比对且过就是证据）：

- 孔轴 = X ⇒ 孔位草图开在 **Right 基准面**（法向 X），平面骑在 1mm 厚的环**中间**；
- 阵列轴 = X ⇒ **`Front ∩ Top`**（Front=x-y 法向 z，Top=x-z 法向 y）；
- 切孔必须 `ThroughAllBoth` + `both_directions` —— 骑在材料中间，单向穿不透。

### ② `revolve_boss` 漏声明 `effect=("has_feature",)`

第一次 dry-run 就被自己的静态校验拦下：

```
X [cp] …（另一次）X [cut] 顺序错误：extrude_cut 需要 已有至少一个实体特征，
  但此刻的状态是 {doc_open, sketch_closed}
```

`extrude_boss` 声明了 `effect=("has_feature",)`，**`revolve_boss` 没声明**。  
静态状态机因此不知道"回转那一步已经建出实体了"。  
★ **只有同时用到"回转 + 切除"的件才会暴露** —— 之前 12 件全是"回转到底"，谁也没碰这条链路。  
⇒ 凡产生实体的 op 都要声明 effect；`revolve_cut` 目前也缺（还没被触发），一并记着。

### ③ `sw_spec` S1 在**多草图**计划里取错了草图

S1（"凡 revolve 的草图必须先画中心线"）在 `profile` 为空时  
（本族所有 `revolve_boss` 都不写 `profile`）退化成 `sketches[-1]` ——  
**整份计划的最后一张草图**。垫片1 的 `revolve` 在第 8 步，而最后一张草图是第 17 步才开的"孔位"：

```
[ERR] S1 · [gasket1] revolve(垫片1) 用的草图 '垫片1孔位' 没有画中心线
```

真实原因：`revolve` 用的是**当前草图**（`sketch_end` 退出时已把它选上），  
即 revolve 之前**紧邻**那张。修法（先找"ops 里含这个 revolve 步"的草图，再兜底 `end<i`）：

```python
target = next((sk for sk in sketches if any(st is s for st in sk.ops)), None)
if target is None:
    cand = [sk for sk in sketches if sk.end is not None and sk.end < i]
    target = cand[-1] if cand else None
```

★ 查草图归属要**双向**都能用：按"名字相等"会撞同名块（§73），  
按"end < i"会漏掉"同一张草图被关两次"的情形（回转体 `sketch_end → revolve → dim_auto → sketch_end`）。

### ④ S7「锚住原点」要多认一种正当途径

孔位草图的图元（孔）**故意不在原点上**（要偏到分布圆上），所以 S7 会判违规。  
正解不是"再补一条过原点的中心线"——`centerline` 会进 `ctx.segments`，  
在草图级阵列里会被一起阵列。两条正当锚定途径：

1. 中心线过原点（本族采用：给阵列一条可见参考 + 过 S7）；
2. **草图内以原点为中心的圆周阵列**（阵列中心=原点 ⇒ 整张草图被钉死，比"点落在原点"更强）。  
   ⇒ 规则要跟着真实几何走，别逼生成器去做无意义的 workaround。

### ⑤ 计划里**不要**用 `{{ vars.x.y }}` 取前序步的真名

曾想让 `circular_pattern` 的 `axis` 引用 `{{ vars.ax.axis }}`（免得中文界面改名对不上），  
结果 `--dry-run` 直接失败：`表达式无法求值: {{vars.ax.axis}} ('dict' object has no attribute 'axis')`。  
★ **「计划能离线校验」是 `yoyo.swplan/1` 的核心约定**，为了一点健壮性换掉它不划算。  
`axis_from_planes` 本来就会找到新建的 `RefAxis` 并**真的改名**成 `Axis1`（探针已验证），  
写死即可；万一改名失败，`circular_pattern` 会报"选不中阵列轴"并提示真名，仍可诊断。  
⇒ **note 里可以写 `{{ vars.* }}` 当反面教材，自检只查步骤参数**。

### ⑥ 两次 `check_mass` 缺一不可

`m0`（回转后、无孔）= 753.98 / 895.35，`m1`（打孔后）= **716.2831 / 870.2212**（参照 716.3 / 870.2，差 **0.0024%**）。  
只测一次的话，"环建错 + 孔补回来"会互相抵消而看不出来。

**回归保护**：`sw_selftest.py` 的 `[2g]`（正例 + 6 条反例 + S1 归属回归 + effect 声明守卫），  
探针 `reducer_gasket1.json` / `reducer_glass.json`。

---

## 83. ★★★ `open_doc` **打开 ≠ 激活**：`OpenDoc6` 返回 tuple + Silent 不切换活动文档 ⇒ 读到上一份文件

2026-10-02 做 GB 标准件取证时踩的。**这是"返回值会说谎"里最隐蔽的一个**：  
不报错、不超时、结果格式完全正常 —— 只是**内容全错**。

### ① 症状：`sw_inspect --doc A` 读出的是 B

连着读 6 个标准件，`sw_inspect` 两次都报出**第 1 个**（`26-A型圆形销.SLDPRT`）。  
差点据此下三个错误结论：

- 螺母 M10 = "26 个锥面、0 平面、0 圆柱的怪件"（真值：8 平面 + 1 圆柱 + 3 B 样条 + 14 锥面）；
- 「六角头螺栓 M8×65」= "根本没有六角头"（真值：主轴向搞错了，见 ②）。

### ② 两个原因叠在一起

**(a) `OpenDoc6` 在 pywin32 下返回的是 tuple** `(model, Errors, Warnings)` ——  
末尾两个是 by-ref 出参。原代码：

```python
model = self.app.OpenDoc6(p, ..., errs, warns)   # model 其实是 tuple！
if model is None:                                 # tuple 永远不是 None ⇒ 永不成立
    ...
return model                                      # 调用方基本都不用返回值
```

⇒ **打开失败/类型不对都不会报错**，这个分支等于没有。

**(b) `swOpenDocOptions_Silent` 下"打开"不等于"激活"。**  
`self.model` 是 `app.ActiveDoc`（动态属性），文档没被激活 ⇒ 所有读操作  
（`sw_inspect` / `sw_faces` / `sw_shot` / `sw_profile`）继续挂在**上一份**文档上。  
只有"进程刚起、打开第一个文档"时碰巧正确 —— 所以单文档场景永远看不出问题。

### ③ 修法（`sw_connect.open_doc`）

```python
res  = self.app.OpenDoc6(p, ..., errs, warns)
model = res[0] if isinstance(res, tuple) else res      # ★ 先解包
if model is None: ...
title = str(member(model, "GetTitle") or "")
if title:
    self.app.ActivateDoc3(title, False, 0)              # ★ 显式激活
    cur = str(member(self.app.ActiveDoc, "GetTitle") or "")
    if cur != title:
        raise SwError(...)                              # ★ 读回核对，不符就停
```

★ **核对不能省**：`GetTitle` 走 `member()`（按方法调）。直接写 `model.GetTitle`  
会拿到 **bound method** 而非字符串，`if title:` 恒真、`!=` 恒成立 ⇒ 变成"永远报错"。

### ④ 误读的第二坑：`sw_faces --axis` 默认 **X**

修好激活后重读螺母，`--axis` 默认 0(X) 仍报"0 圆柱、0 平面" ——  
但那不是"没有"，是**按主轴筛选后的结果**：螺母轴是 **Z**（内孔 Ø10 沿 Z、端面 Z=±4.2）。  
显式 `--axis 2` 立刻读到台阶表（Ø10.000，L=7.410）与平面站位表（Z=±4.200，面积 90.76×2）。  
⇒ 参照件在 SW 里的**朝向是建模者随手选的**（与 §"朝向是习惯不是规格"同源），  
**读任何"按轴筛选"的表之前，先确认主轴**。

### ⑤ ★ `mass_props` 的 Volume 对**含螺纹**的件不可信（待进一步确认）

| 件              | 含螺纹     | mass_props 实测 | 解析                               | 差           |
| -------------- | ------- | ------------- | -------------------------------- | ----------- |
| 17-平垫圈 10      | 否       | 428.33        | π/4(22²−11²)·1.5 = 427.6         | **0.17%** ✅ |
| 26-A型圆形销       | 否       | 232.61        | π/4·4²·19 = 238.8（含倒角）           | ✅ 合理        |
| 9-螺钉M3×10      | 外螺纹     | 108.84        | 盘头+杆 ≈ 125                       | 偏小但同量级      |
| **11-螺母 M10**  | **内螺纹** | **970.73**    | 六角 s≈16 × 8.4 − Ø10 孔 ≈ **4900** | **差 5 倍** ❌ |
| 13-六角头螺栓 M8×65 | 外螺纹     | 4370.68       | 六角头 2327 + 杆 3267 = 5594         | 差 20% ❌     |

⇒ 螺母的 26 个面里，**14 个锥面 + 3 个 `ident4006`（B 样条 = 螺纹螺旋面）**，  
疑似这类面让 `CreateMassProperty2` 的 Volume 明显偏小。  
★ **实践口径：含螺纹/复杂 B 样条面的件不要拿 `check_mass` 对账**，  
改用 `check_bbox` + `check_cyl_radius` + **解析体积自校验**；  
对账基准用**自己按国标表算的解析值**，不要用参照件的 `volume_mm3`。  
（**待查**：到底是 B 样条面导致，还是 `MirrorPattern` 造成的重叠实体被去重。）

### ⑥ 回归：既有型面取证**没有被污染**

`sw_profile --docs` 批量同样走 `open_doc`，所以必须回归。修好后重提 5 件：

| 件      | 重提 Pappus  | params 存档  | 结论       |
| ------ | ---------- | ---------- | -------- |
| 19-端盖1 | 24360.638  | 24360.638  | **逐位相同** |
| 28-端盖3 | 17210.2991 | 17210.2991 | **逐位相同** |
| 25-挡油环 | 3845.0974  | 3845.0974  | **逐位相同** |

⇒ v1.4.10 的型面取证有效（当时是逐件单独调用，恰好是"能对"的那种情况）。  
**教训仍在**：批量入口必须配一条"读到的标题 == 期望的标题"的守卫，  
`open_doc` 里的读回核对就是这条守卫的落点。

---

## 84. 标准件族（螺母/螺栓/垫圈）：**按 GB 表建**，以及两个"自己算错"的地方

2026-10-02 做 GB 标准件族时踩的。**最大的坑不是 SolidWorks，是"我以为的形状"。**

### ① ★ 螺栓的六角**只占 0→k 那一段**，k→L 是圆杆

第一次写体积公式时不分形状，一律按"六角柱 L 高 − 通孔"算：

```
错公式：(3√3/2)·s²·L − π/4·d²·L = 2.598×13²×25 − π/4×64×25 = 9720 mm³
真机实测：3317 mm³
```

`check_mass` 立刻炸：**实际 3.317g，期望 9.7202g**。

★ **这里要反过来想**：校验器报错时，先怀疑**校验器自己**。独立手算一遍  
「六角头 2.598×13²×5.3 = 2326.7 + 圆杆 π/4·8²×(25−5.3) = 990.4 ⇒ **3317.1**」，  
与实测**逐位吻合** ⇒ 几何是对的，公式错了。若照着"校验器说错"去改模型，会把对的改成错的。

正确公式：

```
螺母 hex_nut  ：(3√3/2)s²·m − π/4·d²·m
螺栓 hex_bolt ：(3√3/2)s²·k + π/4·d²·(L−k)      ← 六角只有 k 高
```

已抽成 `sw_reducer._hex_volume(s, d, kind, h1, h2)` 并在自检里钉死（含反例）。

### ② ★ 正六边形：**对边宽 = 外接圆半径**，包围盒是 [2s, √3·s, t]

正六边形边长 = 半径 ⇒ 对边宽 s = R。顶点 `(R·cos k60°, R·sin k60°)`。  
顶点摆在 +X 上时包围盒 = **[2s, √3·s, t]**（如 s=16 → 32 × 27.71），  
**不是** `[s, s, t]`。参照件实测 18.841×15.389 说明它的六角**摆放角不同且带圆角**，  
所以 `check_bbox` 只对解析期望，**不与参照件对账**。

### ③ ★ 标准件**按 GB 表建** ⇒ 与参照件有**预期差异**，要显式豁免、不许放宽阈值

17-平垫圈 10：国标 **GB/T 97.1 d1 10.5 / d2 20 / h 2**；参照件实测 **Ø22/Ø11/1.5**  
（体积 428.33 = π/4(22²−11²)·1.5），像**美制 3/8"**。  
解析 455.14 vs 参照 428.30 ⇒ **差 6.27%**，自检的"解析体积 vs 参照件"红了。

★ 修法是**加显式豁免名单 + 把理由写进断言文本**，**不是**把 `dpct < 0.05` 改成 `< 0.07` ——  
后者会让真正的参数填错也一起放行。同类要登记的还有：  
螺母 M10 参照件 18.841×15.389（对边比国标小、外形带圆角）；  
「六角头螺栓 M8×65」参照件实测 bbox 19.588×16.0×70.6，**截图看是光杆+圆端、根本没有六角头**。

### ④ 螺栓两次拉伸**都从 Front 平面起**，靠 `merge` 并成一体

不要为了"把杆挪到 Z=k"去 `plane_offset` + `{{ vars.* }}` 引用返回的面名 ——  
那会让**离线求值失败**，而"计划能离线校验"是 `yoyo.swplan/1` 的核心约定（§82）。  
做法：① 六角头 0→k；② 同心圆杆 0→L（0→k 段完全被头包住），`merge=True` 自动并体。  
真机 18/18 步通过，bbox 与体积不变 ⇒ merge 可靠。

### ⑤ 六角头草图**没有图元点在原点** ⇒ 过不了 S7

螺母那张草图有同心圆（圆心在原点）天然锚定；**实心六角头的 6 个顶点都不在原点**。  
正解不是改规则，而是**补一条过原点的中心线**（构造线，`S7` 认它）；  
中心线**不参与轮廓** ⇒ 不影响 `extrude_boss`（它选的是草图，不是图元），真机已验证。

### ⑥ 取证：GB 表能一次给出全部关键尺寸

建这批件时 `sw_faces --axis 2` 读螺母 M10：**内孔 Ø10.000**、端面 **Z=±4.200**（厚 8.4）  
—— 与 GB/T 6170（s16/m8.4/d10）**三项全对上**，唯独外接圆 Ø18.841 > 公称对角 18.475（多圆角）。  
所以**国标表 + 参照件取证两者交叉**，才能说清"哪一项是标准的、哪一项是作者加了料"。

### ⑦ 「圆锥销」其实是圆柱销 —— 图纸与参照件互相矛盾

`26-圆锥销 φ4×18`（GB/T 117，锥度 1:50）在参数表里写着"圆锥"，  
但参照件 `26-A型圆形销.SLDPRT` 实测 bbox **[19, 4, 4]** 是**恒定 Ø4**、体积 232.61  
≈ π/4·4²·19 − 倒角；而且**文件名"A型圆形销"是 GB/T 119.1 圆柱销**，压根不是圆锥销。  
⇒ 已列入 `open_questions` 待裁决；当前口径 = **照参照件建**（装配能对上），并把矛盾写进 `_measured_faces`。

**回归保护**：`sw_selftest.py` 的 `[2h]`（六边形几何 + 两种体积公式与反例 + 计划结构 +  
GB 来源断言 + 平垫圈豁免）。

---

## 85. 弹簧垫圈（螺旋扫掠）：**模型态选不中草图圆边**已解，但 `InsertHelix` **圈数不可控**仍卡着

2026-10-02 做 14-弹簧垫圈 8（GB/T 93）时踩的。**链路打通了，尺寸没对上** ⇒ 件没进库。

### ① ★ 模型态下**根本选不中草图里的圆边**（七种方式全灭）

`InsertHelix` 只认"当前选择里的圆"，而 `helix` op 的老建议是  
`… → sketch_end → 选中该圆 → helix`。**第二步做不到**：草图关闭后，  
`SelectByID2` 各种写法全部返回 False、选中数 0：

| 试法                                            | 结果          |
| --------------------------------------------- | ----------- |
| 空名 + `"EDGE"` + 切点 (5.05,0,0) + Extensive     | False / 0   |
| 空名 + `"EDGE"` + 45° 点                         | False / 0   |
| 空名 + `"SKETCHSEGMENT"` + 45° 点                | False / 0   |
| 空名 + `"EDGE"` + 靠近圆心 + mark=0                 | False / 0   |
| 按名字 `"圆1"` / `"Arc1"` / `"圆 1"` / `"Sketch1"` | 全 False / 0 |

⇒ **正解：别退出草图**。在 `sketch_begin → circle` 之后**直接** `InsertHelix`，  
用 `ISketchSegment.Select4(True, None)` 选中 `ctx["segments"]` 里的圆。  
螺旋线会作为**顶层 `Helix` 特征**生成（特征树里能看到「螺旋线/涡状线1」）。  
已把这个能力做进 `helix` op（`from_sketch` 默认 True），原"必须已有选择"的路径仍可用。  
★ `InsertHelix` **返回 None 但特征真在** —— 老毛病，判据只能看特征树。


### ② ★ 剩下的卡点：圈数与设定对不上

要 2 圈（`height=4 / pitch=2`），真机出来 **2.516 圈**：

| 量                         | 期望        | 实测              |
| ------------------------- | --------- | --------------- |
| 体积（刚体扫掠 = 截面积 × 路径长，解析精确） | 254.3 mm³ | **320.0**（+26%） |
| bbox X（外径 d1+2b）          | 12.1      | **12.753**      |

由体积反推：320 ÷ 4（截面积）= 80 mm 路径长 ⇒ 80/31.79 = **2.516 圈**。

**已排除的三个原因**（各改一次重跑，**数值一个字节都没变**）：

1. `InsertHelix` 的 **Outward**（第 4 参）—— 硬编码 `True` 看着可疑，改 `False` 无变化；
2. `swept_boss` 的 **twist** —— 0（`swTwistControlFollowPath`，截面跟路径转）  
   改 1（`swTwistControlKeepNormalConstant`，直纹）无变化；
3. **截面畸变** —— 由体积反推截面积仍是 4.0 ⇒ 矩形 2×2 没变形。

⇒ 怀疑对象转向 `InsertHelix` 的**第 9 个参数 `TotalHeights`**（当前传 `0.0`）  
与 `HelixDefinedBy` 常量是否真的取到（`C()` 名字拼错会**静默退到 0**）。

### ③ 为什么不把件先入库

`_spring_washer_plan` 骨架**保留**（几何、扫掠、解析体积公式都对），  
但**没注册进 `BUILDERS`** —— 注册等于对外承诺"能建"，而真机跑不过。  
`params.washer_s8._measured_faces.solved.blocked` 记了症状/已排除项/下一步；  
**自检里不假装它通过**（探针也不会生成，因为没有注册 ⇒ 不会被"每件有生成器必有探针"扫到）。

### ④ 顺带一条：参照件本身也不是标准形状

`14-弹簧垫圈 8` &#x6709;**「伸缩 / 压缩」两个配置**，ref 指的&#x662F;**「伸缩」**：  
bbox [12.284, 4.984, 14.206] 说明螺距 ≈ 7.1、2 圈**圈间有缝**（不是平螺旋），  
体积 106.27。GB 平螺旋圈间相切时解析体积 ≈ 254 ⇒ **差 2.4 倍**。  
⇒ 即便修好圈数，**也不会与参照件对账**（参照件画的是装配示意形状，不是规格）。

---

## 86. 扫掠件两步踩坑：**草图内 `helix` 会吃掉草图** + 「特征树最后一个」不是刚建的那张

弹簧垫圈（§85 的续篇）。**用户看图指出"逻辑错了、扫出来是个圆筒"是对的**，最后定位到**两个**独立问题。

### ① 症状：`swept_boss` 成功、体积也"接近"，但形状是**圆筒**

特征树暴露真相：截面草图里只有 **1 个圆弧**，而我明明画的是 `rect_center` 矩形。  
⇒ 扫掠拿到的是**上一张残留草图**（只含螺旋草图里那个圆），于是扫成圆筒。

### ② 根因 A：`sketch_end` 按「特征树最后一个 `ProfileFeature`」改名 ⇒ 会改错对象

`last_sketch_name()` 找的是**特征树遍历顺序里的最后一张草图**，而  
**遍历顺序 ≠ 创建顺序**。文档里有 2 张以上草图时，它会把名字安到**另一张**头上。

★ **我为这个去改 `op_sketch_end`（改成"退出前抓 `sm.ActiveSketch`"）—— 踩了更大的坑**：  
真机回归里**螺母 / 垫片1 直接坏掉**（`extrude_boss` 报「选不中草图 草图1」，改名没生效）。  
⇒ **已回滚**。教训三条：

1. `last_sketch_name()` 只在"文档里有 ≥2 张草图"时才选错，而**那个场景可以被别的办法消掉**；
2. **为局部问题去动"所有草图共用"的逻辑，风险远大于收益**；
3. 真要动共享逻辑，**必须先跑全量真机回归**，不能只跑新件。

### ③ 根因 B（真凶）：**草图内插 `helix` ⇒ SolidWorks 把草图删掉**

`sketch_begin → circle → helix` 之后，特征树里**已经没有那个 `ProfileFeature`** ——  
螺旋线被"提取"成独立的 `Helix` 特征，草图随之消失。  
但 `ctx["sketch_open"]` 仍是 `True` ⇒ 后面计划里的 `sketch_end` / `sketch_begin` **全部错位**：  
去关一张不存在的草图、在已关闭的文档上开图 ⇒ 矩形画进了**残留草图** ⇒ 圆筒。

**修法两处**：

- `op_helix`：草图内插入后把 `ctx["sketch_open"] = False`（草图已被 SW 消费）；
- 生成器：**不再多写那一步 `sketch_end`**，并且不再 `select_face` 选圆边（模型态选不中，§85）。

修完特征树恢复正常：`草图1` + `Helix` + `截面` + `Sweep` 四个都在，  
**bbox 外径 12.1 ✔、体积 253.825 vs 解析 254.34（0.2%）** ⇒ 圈数/螺距/截面全对。  
★ 顺带说明：我先前判的"圈数 2.516"是**误判** —— 那是截面成圆导致截面积不是 4，反推全错。

### ④ `GetBodyBox` 对**螺旋扫掠面**虚报（同"螺纹虚报"）

外径报 12.1（对），**轴向报 6.0（真值 4.0，虚 50%）**、另一向 12.7（真值 12.1）。  
⇒ 扫掠/螺纹类件的判据**只能用 `check_mass`**（刚体扫掠体积 = 截面积 × 路径长 是**精确**公式），  
`check_bbox` 降级成"只记录不判定"（`tol=99`），期望值写进 note 供人查。

### ⑤ 静态校验要认识这条规则

`sw_spec._split` 新增：`helix` 之后的草图段标记 `consumed_by_helix`，  
**不再要求它有 `sketch_end`**（要求了会误报"草图没有 sketch_end"）。  
另：截面草图加一条**过原点的径向中心线**（既是 S7 锚定依据，也是这段扫掠的径向参考）。

---

## 87. 板类件（垫片2）：**端面面积反解** + `Top` 的法向是 **Y** 不是 Z

### ① ★ 可复用取证法：端面面积反解出"看不见的轮廓"

参照件没有 2D 图纸时，**端面面积 + 已知圆柱面**能唯一定出剩余轮廓：

```
7-垫片2  46×46×2 实心板 = 4232 mm³，而实测 V = 2587.03  ⇒ 挖掉 39%，不是实心板
        sw_faces --axis 2 ⇒ 4 级圆柱 Ø3.500 贯穿、圆心 (±18,±18)
        端面面积 1293.5155 ⇒ 方窗面积 = 46² − 1293.5155 − 4·π/4·3.5² = 784.00 = 28×28
        复核 (46²−28²−4·π/4·3.5²)×2 = 2587.03 = 参照件 ✔ 逐位吻合
```

⇒ **不用猜、也不必逐条读边**。凡"外形尺寸对、体积对不上"的板类件都走这条路。

### ② ★★ `Top` 基准面的法向是 **Y**（不是 Z）—— 板会立起来

第一版用 `select_plane "Top"` 画板面 ⇒ 拉伸沿 **Y** ⇒ bbox `[46, 2, 46]`（厚度跑到 Y 上），  
`check_bbox` 当场抓住。三个面的法向（记住，别再踩）：

| 基准面       | 法向    | 板类该用谁           |
| --------- | ----- | --------------- |
| Front（前视） | **Z** | ✅ 板面 XY，薄板厚度沿 Z |
| Top（上视）   | **Y** | ❌ 会让板立在 XZ      |
| Right（右视） | **X** | ❌               |

⇒ 板类一律 **Front**；这样 bbox 与参照件朝向一致，对账时不用解释旋转。

### ③ 板类判据要加 `expect_count`

`check_cyl_radius(expect_count=4, expect_max=1.75)`：**只看 bbox 的话，  
"4 个 Ø3.5"和"1 个 Ø7"给出同一个 46×46×2** ⇒ 孔数/孔径必须单独钉。  
（`count` 数的是**面**，4 个贯穿孔各一个圆柱面 ✔）

### ④ 一次拉伸 vs 拉伸+切除

垫片2 的 6 个轮廓（外框 + 方窗 + 4 孔）**一次 `extrude_boss` 即可**（与参照件一致）。  
★ 别拆成"拉伸板 + 切窗 + 切 4 孔"：多 6 个特征、多 6 次体积对账，还多一轮失败面。

---

## 88. 「同构就泛化，别复制」+ **装配级约束（模数）必须结构化**，以及"有据的错"怎么报

2026-10-02 做 8-窥视孔盖 + 落实用户「齿轮模数必须相同」要求时踩的。

### ① 同构件：泛化参数，别复制一份族

7-垫片2（方窗 28×28）与 8-窥视孔盖（**圆孔 Ø11**）形状完全同构：  
`矩形外框 + 中心开口 + 4 个角孔`，6 个轮廓一次拉伸。差别只在 `window_shape`。  
⇒ `_plate_plan` 加一个分支即可（`square` 读 `window` / `circle` 读 `window_d`）。  
★ **别为"只是某个参数不同"另写一个族** —— 两个族意味着两套判据、两个探针、  
两处会各自漂移的 note，而它们的几何判据其实**是同一套**。

### ② ★★ 圆柱面判据**必须由几何算出来**，不能按某一件写死

垫片2 的判据是 `expect_count=4, expect_max=1.75`。直接抄给窥视孔盖 → 当场炸：

```
最大圆柱面半径超差：实际 5.5mm，期望 1.75mm
```

**中央 Ø11 孔本身就是一个圆柱面（r=5.5）**；而**方窗不是圆柱面**。  
⇒ 同一组数字在两件上一个对一个错：

```python
n_cyl = n + (1 if shape == "circle" else 0)   # 方窗 4 / 圆孔 5
r_max = win/2 if shape == "circle" else hd/2  # 1.75 / 5.5
```

★ 又是「校验器报错先怀疑校验器自己」：报错的是**我的期望写死了**，不是模型错了。  
★ 顺带：**"某种特征产生几个面"不是常数**（方窗 0 个 / 圆孔 1 个），  
凡是"数面"的判据都要跟着几何算，别拿某一件的数当通例。

### ③ ★★★ 装配级约束（模数一致）写在 note 里 = 没写

用户硬要求：「齿轮轴和大齿轮的模数必须相同，不然装配的时候不能啮合」。

**这类错最难防，因为它是"每个零件单看都对"的错**：  
两轮各自的 `d/da/df` 全都算得通、`check_bbox`/`check_cyl_radius` 全过，  
只有装到中心距 70 的箱体里才发现根本转不动。而本项目里 `m` **可能出现在三处**  
（`drive.m`、`gears.*`、`meta.gear_train.module`），任一处单独改都会让两轮悄悄漂移。

⇒ 三件事一起做：

1. **唯一真源**：`meta.gear_train`（m / z₁ / z₂ / a / α / β / 两轮 da 与 d）；
2. **零件上不许自带**：删掉 `parts.gear_shaft.m` / `parts.wheel.m`，  
   只留 `gear_role`；守卫 = 这两个件上出现 `m`/`z`/`module` 任一键即判违规；
3. **交叉校核 C12**：`gear_train ↔ drive` 一致 + 零件无自带 m/z +  
   `a = m(z₁+z₂)/2`（必须落在箱体中心距 70±0.08 内）+ `da₂/2 < a`（装得进两轴之间）。

★ **"中心距自洽"是唯一真正能拦住装配错的判据** ——  
`a` 和 `m`/`z` 各自都对，但 `a ≠ m(z₁+z₂)/2` 时啮合不上（那才叫"模数没对齐"）。

### ④ ★★ "有据的错"要**分两级**报，不能一律 hard 也不能一律放行

C12 上线后立刻报 FAIL：`小齿轮 z₁=15 < 17 且不变位 ⇒ 根切`。  
取证 `sw_faces --axis 0` 读齿轮轴齿根圆 = **Ø25.000**，而  
`df = 30 − 2×2×1.25 = **25.000**` —— **只有 x=0 逐位吻合**（x=0.123 时 df=25.49）。  
⇒ **参照件本身就是根切齿**（教学模型为凑 z₁+z₂=70 拿整数中心距 70），不是参数抄错。

处置：

| 类别             | 判据                                                      | 级别             |
| -------------- | ------------------------------------------------------- | -------------- |
| 模数/齿数漂移、中心距不自洽 | 无条件                                                     | **`hard`（必拦）** |
| 根切等工程取舍        | **必须有** `undercut.known` + `reference_evidence` + 已登记 Q | `open`（留痕放行）   |
| 同上但**没有**实物证据  | 缺任一项                                                    | **仍判 `hard`**  |

★ 关键在第三行：**没有证据就不许降级**。否则"改参数改到报错就压掉"  
会变成常规操作，真错也跟着一起过。降级信息必须写进 detail（含证据出处）。  
建件时 `sw_gear` 的 error 级诊断要**显式**降 warn，并在报告里留痕，不许静默吞。

### ⑤ 端面面积反解（复用 §87 的手法）

`46² − 1982.48 − 4·π/4·3.5² = 95.0332 = π/4·11²` ⇒ 中央开口 **Ø11.000**。  
**是算出来的，不是看图看出来的** —— 看图分不出"Ø11 圆孔"与"11×11 方孔"的面积差（0.3%）。

---

## 89. 齿轮族：齿顶**不是圆柱面** / `MidPlane`×`plane_offset` 配对 / 弹簧垫圈是**两态**

2026-10-02 做 33-大齿轮（m=2 z=55）+ 用户提醒弹簧垫圈两态时踩的。

### ① ★★★ 齿顶圆**不是圆柱面** ⇒ `check_cyl_radius` 不能判齿顶

```
最大圆柱面半径超差：实际 47.0mm，期望 57.0mm
```

`check_cyl_radius` 只枚举 `ISurface.IsCylinder()` 的面。齿轮的齿顶圆是  
**z 段渐开线拼出来的面**（`gear_profile` 用 polyline 一次成形，z=55 ⇒ 716 点），  
**一个圆柱面都没有** ⇒ 最大圆柱面其实是**凹槽底**（Ø94/2 = 47）。

| 判据                 | 能验什么                 | 不能验什么          |
| ------------------ | -------------------- | -------------- |
| `check_bbox`       | **齿顶圆 da**（外廓最大处）、齿宽 | 齿根、孔径          |
| `check_cyl_radius` | 中心孔、轮毂、凹槽底这些**真圆柱面** | **齿顶/齿根（渐开线）** |
| `check_mass`       | 整体 + 齿隙（依赖采样密度）      | —              |

⇒ **啮合的决定性尺寸 da 交给 `check_bbox`**；圆柱面判据钉住真圆柱面。  
★ 真机记录里也要**写明这条**（`cylinders.note`），否则下次有人看到"expect_max=47"  
会以为搞错了、顺手改回 57。

### ② ★★ `MidPlane` 与 `plane_offset` **必须成对**

| 拉伸方式             | 实体 Z 范围      | `plane_offset` 偏移量 |
| ---------------- | ------------ | ------------------ |
| `MidPlane`（总深居中） | [−b/2, +b/2] | **b/2**            |
| 单向（`Blind`）      | [0, +b]      | **b**              |

配错的后果很隐蔽：MidPlane + 偏移 b ⇒ 平面跑到 Z=+26，**骑在实体外面**  
⇒ 该面上的 `extrude_cut` 主方案 + 备选 1 **全部返回 None**，  
而错误文案只说"切除失败"，看不出是平面位置问题。  
★ 抄探针时要注意：探针 `gear_m2z55.json` 用的是**单向拉伸 + offset=b**，  
把 `extrude_boss` 改成 MidPlane 却不改 offset 就会炸。  
⇒ 判据：偏移量是 b 还是 b/2，取决于**实体是否居中**，不是取决于"想在哪切"。

### ③ ★ 弹簧垫圈是**两态**：展开态（画图） vs 压平态（工作）

产品图/参照件画的是**展开态**（pitch≈7.1、圈间有缝），装配时被**压平**。  
GB/T 93 平螺旋标准型圈间相切（pitch = 料宽 b），**自由高 == 压平高**：

```
H = s + (n−1)·b = 2 + 1×2 = 4.0 = 表值 H=4.0  ✔
```

⇒ 按标准型建的**就是压平态 = 工作态**，不需要另外建展开态。

★ 这条等式是**独立判据**：完全不依赖 `InsertHelix` 真生成了几圈，  
n 填错立刻对不上（比"用体积反推圈数"直接得多 —— 上一轮我正是靠体积反推误判成"2.516 圈"）。

**★ 顺带：参照件那个「伸缩」配置哪个态都不是**（刚体扫掠 V = b·s·L）：

| 态       | pitch   | 圈数       | 解析 V   | 实测         |
| ------- | ------- | -------- | ------ | ---------- |
| 压平（标准型） | 2.0     | 2        | 255.85 | 我们 253.825 |
| 展开（产品图） | 7.1     | 2        | 908.27 | —          |
| 参照件「伸缩」 | 7.1（反推） | **0.84** | —      | **106.27** |

`L = 106.27/4 = 26.57mm`，一圈路径长 31.79 ⇒ **只有 0.84 圈**。  
⇒ 它是**画图示意**（让图纸上看得见螺旋形），**不是规格也不是工作态** ⇒ 不对账。  
★ 由此改写 `meta.authority`：**弹簧垫圈这类"有工作态/示意态之别"的件，  
标准型优先于参照件实测**（工作态才是设计意图）；其余件仍「图纸 > 参照件 > 国标」。

### ④ ★ 凹槽的环形关系：内径 = 轮毂，外径 = rim（我第一版写反了）

守卫写成 `rim_id < hub_od`（把 Ø94 当内径），参照件一跑就  
`ValueError: 凹槽外径 Ø94.0 必须小于轮毂 Ø48.0`。  
**凹槽是环形带：内径 = 轮毂 hub_od、外径 = rim_id** ⇒ 必须 `hub_od < rim_id`。  
★ 平面图上看"挖一个凹槽"分不出谁是内谁是外，**按面积反解 + 按语义命名**才靠谱。

### ⑤ 参数表带 `source` 的字段是 dict，传下去前要拆裸值

`keyway` 存成 `{"v": "10x3.3", "source": "measured"}`，直接传下去报  
`could not convert string to float: "{'v': '10"` —— 报错信息完全看不出是哪来的。  
★ 凡 `q["xxx"]` 是 `{"v":..., "source":...}` 形状的**字符串**字段，  
都要 `str(q[k].get("v") if isinstance(q[k], dict) else q[k])`。  
（数值字段已有 `v()` 助手；字符串字段没有 ⇒ 加这条。）

### ⑥ ★ 先怀疑自己的算术，再怀疑别人

我一度口算 `df = 110 − 2×2×1.0 = 107.5`，判"参照件齿根被切深 2.5mm"，  
差点把参照件当成不合规件去"纠正"。实际 `sw_gear` 用标准 **c*=0.25**：  
`df = m·z − 2m(ha\*+c\*) = 110 − 2×2×1.25 = **105.0**`，与实测 **Ø105.000 逐位吻合**。  
⇒ **"实测与理论不符"时，先确认自己用的是哪套系数/口径**（c\*、ha\*、变位 x）  
再去动别人的数据；标准件的系数错一个就是几毫米。

---

## 90. ★★★ 根切**只在小齿轮**：「改大齿轮模数」是错解；啮合是**单件判据查不出**的一类错

2026-10-02 用户反馈：「当时我建的时候他们就没啮合上」并建议改大齿轮模数。  
这是本项目**最值得记住的一次事故**——两个零件**各自**所有判据全过，装一起才炸。

### ① ★★ 技术澄清：根切只与**齿数少的那个轮**有关

根切 = 标准齿条刀具切齿时，齿根部位已被切到基圆之外 ⇒ 齿根变弱、渐开线被截断。  
**只发生在小齿轮（齿数少的那轮）**。所以：

- ❌ 「改**大齿轮**的模数」**解决不了根切**；
- ❌ 而且两轮模数**必须相同**（否则中心距对不上，更装不上）——  
  改大齿轮模数会**同时破坏**用户上一轮明确要求的"模数一致"。
- ✔ **唯一干净解 = 提高小齿轮齿数**（或加正变位，但变位会改中心距）。

20° 压力角、ha*=1、x=0 的最小齿数：

```
z_min = ceil(2·ha*/sin²α) = ceil(2/0.11698) = **18**
```

（`sw_gear` 还会给**精确到小数**的 `x_min`：`x_min = ha* − z·sin²α/2`，  
z=15 时是 **0.1227**，报"至少需要 x=0.123"——比"z 太小"更可操作。）

### ② ★★★ 关键决策：**改齿数、不改模数** —— 因为 `z₁+z₂` 不变则**中心距不变**

`a = m(z₁+z₂)/2`。只要保持 `z₁+z₂ = 70`：

```
z₁ 15→18,  z₂ 55→52   ⇒   a = 2×70/2 = **70**（不变）
```

⇒ **箱体中心距 70±0.08、轴承座、端盖全部不受影响**。  
若走"改模数"路线：`a = m'(z₁+z₂)/2 ≠ 70` ⇒ 箱体/轴承座/端盖/箱座箱盖**全要重做**。

| 方案      | 齿数             | 中心距    | 箱体      | 结论                       |
| ------- | -------------- | ------ | ------- | ------------------------ |
| 参照件     | 15/55          | 70     | 不动      | ✘ 小齿轮根切                  |
| **改齿数** | **18/52**      | **70** | **不动**  | ✔ **采纳**                 |
| 改模数     | 15/55, m=2.286 | 70     | 不动      | △ 能算，但 m 超出 GB/T 1357 系列 |
| 改模数     | 15/55, m=3     | 105    | **全重做** | ✘                        |

★ 另有一条：`z₁=17` 只差 `x=0.006` 就够不上不变位，**最容易被误判成"行"**，  
实际 `sw_gear` 仍报 error。⇒ **判据用 `sw_gear.diagnostics()` 的 `level=="error"`，  
不要自己按 z 猜临界值。**

### ③ ★★ 齿宽也偏大：26 > 10m

参照件齿宽 26（m=2 ⇒ 10m=20）。齿宽超过 `10m` 属偏宽，加剧"顶不进/顶死"。  
重新设计取 **b = 20 = 10m**（上限，承载最好）。★ 齿宽**与齿数无关**，  
所以这一项**不必跟着改**，但顺手修正是对的。

### ④ ★★★ 「啮合」必须是一条**独立**判据，不能指望单件检查

这次事故的本质：**两个零件单看全对**（bbox ✔ 圆柱面 ✔ 质量 ✔ 特征数 ✔），  
错在**放到一起**才暴露。⇒ 至少要有一条离线检查专门查"啮合"：

| 查什么       | 判据                          | 缺了会怎样                |
| --------- | --------------------------- | -------------------- |
| 模数一致      | `mt₁ == mt₂`                | 中心距对不上               |
| **中心距自洽** | `a == m(z₁+z₂)/2`           | ★ **这才是"模数对不对"的真判据** |
| **无根切**   | `z₁ ≥ z_min` 或 `x₁ ≥ x_min` | 齿根被切掉，强度不够/干涉        |
| 齿顶不干涉     | `da₁/2 + df₂/2 ≤ a`         | 顶死                   |
| 顶隙        | `(a − da₂/2) − df₁/2 > 0`   | 咬死                   |
| 齿宽        | `b ≤ 10m`                   | 承载不均、顶不进             |
| **装配相位**  | 大齿轮偏移 `180/z₂`              | ★ **齿对齿顶死**          |

★ 顶隙那一项**在参照件与新方案里都是 0.5mm（= 2c*）**，  
所以"装不上"**不是**顶隙问题 ⇒ 只有根切能解释。**先算清是哪一项**再动手。

### ⑤ ★★ 相位：「每个零件都合格、装一起才炸」的第二类

两轮啮合必须**齿对槽**。若两轮齿廓都从「一个齿中心线」（都在 +X）开始，  
就是**齿对齿**顶死。装配时须让**一轮偏移半个齿距**：

```
大齿轮偏移 = 180/z₂ = 180/52 = **3.4615°**（小齿轮保持 0°）
```

★ 这是**离线查不出**的（每个零件单独看都合格），只有**装配对账**能验。  
本项目 `yoyo.swplan/1` 目前只到零件层 ⇒ 已把它写成 `sw_mesh.py` 的显式提示 +  
`gear_train.redesign.remaining_risk`，等装配层落地时直接取用。

### ⑥ ★ 工程做法：重新设计后，**对账豁免要显式、要写理由**

齿数一改，大齿轮齿顶 Ø114 → Ø108，与参照件**差 6mm** ⇒ 参照件对账必然红。  
★ 修法是**加显式豁免机制**（`reference_check` 里检测  
`gear_train.redesign_2026_10_02` 点名的件 → 改报 `info` 并打印差多少、为什么），  
**不是**把容差 `0.5` 调成 `6.0` —— 后者会让真错一起放行。

### ⑦ ★ `check_mass` 的标定值会随设计作废 ⇒ 需要"标定模式"

齿数/齿宽一改，齿隙体积就变 ⇒ 旧的 `expect_mass_g`（136.56）**作废**。  
但解析式算不出齿隙（取决于 `gear_profile` 采样密度），所以只能真机量。  
⇒ 新增 `--calibrate`：**故意跳过 `check_mass` 生成计划**，真机跑完从报告量实测值回填。  
（第一版让它直接 `ValueError`，报"请手工去掉 check_mass"——不够可执行；  
改成开关后才顺手。**报错信息要写"下一步怎么做"**。）

---

## 91. ★★★ 齿轮轴的齿廓**必须拉伸成实体，不能切除**；取证段表**不能只取"最长的一段"**

2026-10-02 做 23-齿轮轴（z₁=18 重新设计后）踩的。**同一版里连错两次，都很典型。**

### ① ★★★ 最大的错：以为"齿根圆柱已在轴身里 ⇒ 切掉外面就是齿"

**错误推理**：`shaft_profile` 的段表里有一段 Ødf₁（齿根圆）⇒ 回转后那段就是齿根圆柱  
⇒ 再用 `gear_profile` 画齿廓、沿轴向**切除**齿根外的材料 ⇒ 得到齿。

**为什么错**：**齿根圆比齿顶圆细**。

```
df₁ = m·z₁ − 2.5m = 2×18 − 5 = **31**
da₁ = m(z₁+2)     = 2×20     = **40**
```

⇒ 齿顶那圈 Ø40 的材料**从来不存在**，**切无可切**。

真机表现（**最难查的一种失败**）：

```
revolve_boss    OK      ← 特征建出来了
gear_profile    OK      ← 271 点齿廓画出来了
extrude_cut     OK      ← 切除"成功"了
check_feature_count  OK  ← 特征数正常
check_bbox      FAIL    bbox Y/Z = 30.838（≈Ø31），齿顶 40 根本没出现
                且 bbox X = 25.196（只剩齿部那一截，轴身整段消失）
```

⇒ `extrude_cut` + `ThroughAllBoth` 从 x=0 双向切，把**齿部以外的轴身也切了**，  
剩下的正好是"齿根圆柱被削薄 0.16mm"的那截。

**正解**（与大齿轮**完全同一套路**）：

1. 轴身段表里齿部那段用**细轴**（Ø24 = 齿根侧台阶直径，必须 < da）
2. `plane_offset` 把齿廓草图面开到齿部轴段起点
3. **`extrude_boss` + `merge=True`** 把整圈齿廓**拉伸成实体**并与轴身并体
4. 齿根圆与齿顶圆**由齿廓实体自然形成**，不用单独建柱面

⇒ 判据也跟着变：**齿根与齿顶都不是圆柱面**（都是渐开线的一部分），  
`check_cyl_radius` 只能钉轴台阶 Ø24，齿顶/齿根仍由 `check_bbox` 判（同 §89）。

★ 通用教训：**"切掉 A 之外的材料得到 B"这个句式要先验证 A 确实包住 B**。  
这里 A（齿根 Ø31）**不包住** B（齿顶 Ø40）—— 而这只要看一眼两个直径的大小关系就能发现。

### ② ★★ 取证段表**不能只取"每个半径最长的一段"**

第一版 `shaft_steps` 用"按半径分组、每组取最长"取段表，**漏了 4 段**：

```
Ø24 @ x 0→8      Ø18 @ x 8→11     Ø20 @ x 11→26     Ø6 @ x 71.8→75
```

根因：**"每个半径有多段"是常态**（对称台阶），只取最长的会丢。

★ **正确口径**：按半径分组后**合并相邻区间**，或直接列出**每个半径的所有区间**。  
判据：**做完必须做覆盖检查** —— 把所有段按 x 排序，看首尾是否连续、有无空档。  
本轮就是靠"覆盖检查"发现 5 处空档，才进而发现那 5 段**锥面**  
（其中 x48→71.8 那一段长 23.6mm，是整根轴最粗的过渡），  
`shaft_profile` 画不出 ⇒ 登记成 Q8 简化项。

### ③ ★ 简化项要**显式登记 + 挂 open_question**，不许悄悄省

参照件齿轮轴有 **5 段锥面**（含 23.6mm 大锥）+ M14 螺纹，而  
`shaft_profile` 只支持"圆柱段 + 端部倒角"。当前处置：

| 简化项      | 影响                                          | 登记在                                  |
| -------- | ------------------------------------------- | ------------------------------------ |
| 5 段锥面不建  | 总长 111.24 vs 参照 158.24（**不与参照件对账 bbox 总长**） | `_simplified.cones_dropped` + **Q8** |
| M14 螺纹不建 | 建光杆 Ø14（与螺母/螺栓同口径）                          | `_simplified.thread_not_built`       |
| 齿根退刀收成直角 | 对啮合无影响（那段在齿廓之外）                             | `_simplified.root_runout_flat`       |

**不简化**的：齿根圆 Ø31、齿顶 Ø40、齿宽 20、以及全部功能段直径。  
自检 `[2l]` 会查"简化项存在 + 挂了 Q8"，**不许哪天悄悄省掉**。

### ④ 判据要钉"**齿部轴段必须比齿顶细**"

新增守卫：齿部轴段直径 ≥ 齿顶圆 ⇒ 生成器**直接 ValueError**。  
因为那是"齿顶无料可凸"的等价的坏状态，在离线阶段就拦住，  
而不是等真机跑出一堆 OK 却形状不对（本次就是真机才发现的）。

---

## 92. ★★ 拿到**正式图纸**后的三个动作：先定标、段界取尺寸链断点、放大 10× 读公差

**背景**：齿轮轴（序号23）终于拿到正式图纸（此前只有成品 SLDPRT，靠反推错了三处）。  
图纸右上参数表 **m=2.0 / z=15 / α=20°** —— 直接推翻了上一轮"重新设计 z₁=18"的方案（见 §90）。  
**教训：图纸 > 参照件反推 > 国标**这条口径是对的，但"没有图纸时按反推重建"的成果，  
拿到图纸后要**主动作废**，不要因为"已经跑绿了"就留着。

### ① 先定标，再量长度（我第一个坑就踩在这）

我用"轴上最左的暗像素"当左端点 → 量出总长 172mm（图纸标 158）。  
**那是 Ø20m6 的尺寸线**，画在零件左端面之外 26px 处。  
正确做法：**用图纸自己的总长尺寸线定标** —— 长水平尺寸线的两个 arrow 端点跨度 = 该尺寸真值。  
这次 `y=647` 的「158」线跨度 1015px ⇒ **6.42 px/mm**，与竖直方向由 Ø20/Ø24/Ø34 三个圆标定的  
**6.40 px/mm** 互相印证（差值 0.3% 属截图各向异性）。

### ② 段界 = **尺寸链断点**，不是所有图形边

一根轴的图形边远多于它的"结构段"：  
这次图形边有 162/271/290/342/360/541/560/611/630/727/830/1087/1177（13 条），  
但尺寸链断点只有 **162 / 290 / 611 / 727**（「20 | 50 | 18」三段的界）。  
271（越程槽）、342/360（30° 齿顶倒锥）、541/560、630 全是**段内特征**。  
★ 判据：**链断点两边都有尺寸标注**；段内特征只有图形线、尺寸线落在别的层上。

### ③ 尺寸标注必须**放大 10 倍以上**再读数

- 键槽宽公差我看了三遍才读对：**6 上偏差 0 / 下偏差 −0.030**（= **N9，正常配合**）。  
  此前记录成"+0.03/+0.1"，**完全反了**（把负号读没了、把 0.030 读成 0.03+0.1）。  
  ⇒ 与 GB/T 1095 的 6×6 平键轴槽规定一致，可交叉验证。
- 「4×Ø11」= **槽宽 4 × 槽径 Ø11**（螺纹退刀槽），不是"4 处 Ø11"。  
  佐证：像素上该槽跨度 25.5px = 4.0mm，且其右侧到轴端 64.5px = 10.0mm（M14 旋合长）。
- 「14」≠ M14 旋合长：它 **1087→1177 = 90px = 14.0mm**，等于「退刀槽 4 + 螺纹 10」。  
  ⇒ 若把 14 当成螺纹长，整条链会差 4mm。

### ④ 尺寸链闭合校验是**最后一道闸门**，必须做

这次链条：20(=17+3) + 50(=8+34+8) + 18(=3+15) + **16（图纸漏标）** + 54(=40+14) = **158** ✔

- 「50」跨越两个 Ø24 定位肩外端面 ⇒ **50 = 8+34+8**，齿部实体长 34（不是 50）。
- 「20」「18」把 3mm 越程槽**包在里面**（17+3 / 3+15）。
- 第 8 段 Ø18×16 图纸**漏标**，是靠总长 158 反求出来的 —— 这类缺项要在报告里点名。

### ⑤ 1:20 锥的读法：**锥度 = Δd/ΔL**，不是斜度

参照件实测锥段 42→80（38mm，Ø18→Ø16）⇒ Δd/ΔL = 1:19 ≈ **1:20** ✔（锥度定义）。  
图纸上那条斜线的**画法**斜率接近 1:10（即按 Δr/ΔL 画的），与标称 1:20 不符 ——  
**以文字标注为准，不以图形斜率为准**；参照件的实体数据是第三个独立佐证。  
★ 另外锥的**直径方向**：是 **Ø18→Ø16**（与前一节 Ø18 连续），不是 Ø20→Ø18。

### ⑥ z=15 的根切：图纸确实没标变位

z_min = 2h_a*/sin²α = **17.1** > 15。参照件实测齿根 Ø25 = m(z−2.5) **逐位吻合** ⇒ 它真的没变位。  
⇒ 结论修正：**这张图纸的原始设计就是"（不满足无根切条件的）标准齿廓"**。  
我们的口径：**按图纸原文复刻**（比赛要求"齿廓允许近似"），把根切作为**已知偏差显式登记**，  
不擅自改齿数/模数。若要真正无根切，须由用户决定补正变位 x≥0.118（会影响 d/da/df 三个尺寸）。

### ⑦ 可复用的"图纸像素测量"套路（无需 CAD）

```
1) 定标：找最长的水平尺寸线（本次 y=647 的 158 线），跨度/标注值 = px/mm
2) 轮廓：对每行做「长水平 run mask」（run>=22px），逐列取 y 最小的命中点 = 上轮廓
3) 台阶：逐列取 y 最大/最小 → 跳变处 = 直径变化；再对每列做竖直 run>=25 找**端面/台阶竖线**
4) 取舍：只把「两侧都有标注」的竖直边当段界（②）
5) 校验：Σ段长 == 总长标注，否则回到 1) 检查定标
```

★ 与 `sw_faces` 的关系：图纸法给**名义值**，`sw_faces` 给**实体实测值**，两者互为反例。  
本次正是靠"图纸 158 ↔ 参照件 158.241"确认了定标正确。

## 93. ★★★ 构建路径**没有**文档指纹守卫（比读错更危险）；「文字承诺已登记」必须有人核对

### ① ★★★ 读路径守住了，**写路径一条没有** —— 会静默污染用户的模型

**真机证据（2026-10-02）**：连续两次在 `sw_profile` 读参照件时抓到"活动文档被别人换掉"。  
第二次拿到了**真凶的名字**：

```
title: '10-通气塞.SLDPRT' → 'excavator.SLDPRT'
path:  'c:\学习软件\一级减速器\10-通气塞.sldprt' → 'd:\out\excavator.sldprt'
features: 28 → 49    bodies: 1 → 2    volume_m3: 6.7126e-06 → 2.0177e-03
```

**根因**：这套脚本是挂在**用户那个"活着"的 SolidWorks 会话**里干活的  
（`SwSession.open()` 复用已运行实例），用户随时可能切走活动文档；  
而**所有读/写操作都挂在 `app.ActiveDoc` 上**。参照件目录里那 19 个 `~$*.SLDPRT`  
锁文件就是"会话里开着很多东西"的现场痕迹。

**为什么读路径守住还不够**：构建路径（`sw_plan.execute` → `sw_ops.run_op`）当时  
**一条守卫都没有**。漂移后：

- `sketch_begin` / `extrude_boss` 会往**别人的模型**里加特征；
- `save_as` 会把**别人那份文档**存成我们的零件 —— 而它现有的三道验  
  （路径指向目标 / 文件新写了 / 里面不是空零件）**全都会通过**，  
  因为那三道只看"**存到哪了**"，从不看"**存的是谁**"。

**修法（两个必须区分的设计点）**：

| 场景   | 指纹字段                                            | 理由                                                |
| ---- | ----------------------------------------------- | ------------------------------------------------- |
| 读参照件 | `title/path/features/bodies/volume_m3`（**全字段**） | 内容**不该变**；只比 title/path 抓不到"路径标题都对、内容却被换"         |
| 构建期  | **只有 `title/path`**                             | 特征数/实体数/体积**本来就在变**（每一步都在加特征）⇒ 用全字段会把**每一步**都判成漂移 |

- `sw_plan._doc_pin` 在 `new_part` / `open` 之后钉指纹（`_DOC_PIN_OPS` 是显式常量）；
- `sw_plan._check_doc_pin` 在**每一步动手之前**核对：漂了先按标题抢回（瞬时切换能救回来），  
  抢不回**直接中止**并报出「期望/实际/原因/解法」；
- 逃生口 `--allow-doc-switch`（默认**开**守卫）；报告里留 `doc_pin` / `doc_switch_guard` 便于事后追责；
- ★★ **两条构建路径都要接**：`sw_plan.execute` 与 `sw_probe.run_plan_in_session`。  
  漏一条就等于"守卫只在一条路上有效"（同封存闸门"一处拦全部"的道理）。  
  自检 `[2q]` 用 `inspect.getsource` 钉住这两个接线点 —— **防"写了守卫但没接线"**。

**反例（必须点名，否则"用全字段"会把构建判死）**：`[2q]` 检查 6 = 同一份文档、  
特征从 4 涨到 9、体积 0→1.66e-6 ⇒ **必须放行**；检查 7 = 真漂移 + 抢不回 ⇒ **必须抛错**。

### ② ★★ 「已列入 open_questions」是一句**没人管的话**

`parts.taper_pin_4x18._measured_faces.note` 里写着  
"**已列入 open_questions 待用户裁决**"，而 `open_questions` 里  
**根本没有这一条**（只有 Q1~Q11，没有圆锥销/圆柱销）。

同一类漂移的反面就是 §"简化项要显式登记 + 挂 open_question"那条守卫 —— 所以  
**不要另发明一套，直接复用**：

```json
"taper_pin_4x18": { "_simplified": { "taper_vs_cylindrical": {
    "status": "简化（按参照件建 = 圆柱销 Ø4×19；图纸要圆锥销 GB/T 117，两者矛盾）",
    "open_question": "Q12" } } }
```

`check_simplified_registry` 随即会因"Q12 不存在"拦住 `build_plan` ⇒ 文字承诺变可核对。  
自检 `[2r]` 检查 4：**把 Q12 抽掉，守卫必须报错**（反例点名）。

★ 口径提醒：`meta.authority` = 「图纸 > 参照件实测 > 国标校核」。  
本件**照的是参照件、不是图纸** ⇒ 这本身就是一次**口径偏离**，必须有人认领  
（同平垫圈那次的教训：**"按标准建件"这个直觉本身就会踩口径**）。

### ③ ★ 型面族（`_profile_plan`）的**第三条分支**：轮廓靠**中心线收口**

型面族原先只覆盖两种闭合方式：纯直线自闭合（`close=True`）、  
直线串交给圆弧收口（`polyline_close=False + arcs`）。26-圆锥销是**第三种**：

```
line  (-5.2127, 2.0) -> (12.7873, 2.0)    母线 Ø4×18
arc   (12.7873, 2.0) -> (13.2873, 0.0)    右端球冠 R4.25
arc   (-5.7127, 0.0) -> (-5.2127, 2.0)    左端球冠 R4.25
```

**两个端点都落在轴线上（r=0）** ⇒ 轮廓**靠中心线收口**（`closed_via_axis=True`），  
既不是自闭合环、也不是"圆弧把开口缝上"。发射顺序仍是 `polyline → arc3p → arc3p`  
（`sw_profile.plan_hint` 的输出直接用就是对的 —— 它已经处理了"把圆弧转到链尾"）。

**球冠的两个自检**（都在 `sw_profile` 里）：

- 只有 **1 个边界圆** ⇒ 极点取"面的轴向包围盒里离边界圆更远的那一端"，  
  再用球面方程反算 r 必须 ≈ 0，**算不出 0 就拒发射**；
- ★ **球心必须在轴上**（球面 vs 环面的分水岭：环面剖面圆心离轴 = 主半径，  
  球面圆心在轴上 = 球心）；本件 R4.25 与冠高 0.5 互相印证：  
  `R = (a²+h²)/(2h) = (2²+0.5²)/(2·0.5) = 4.25` ✔

**这一型最省事的自校验**：拿**该件自己的 `mass_props`** 当对账基准，而不是  
"参照件实测体积" —— 前者是同一次真机读取，证据链更短。  
本件 型面 Pappus 232.6078 / 该件 mass_props 232.609 / 建出来的真机 232.6087  
⇒ 三个独立来源两两差 ≤0.009%。

### ④ ★ 完工清单（这一版又漏了一项，被回归逮住）

真机跑通后**必须**做四件事，少一件就会被回归/盘点抓到：

1. `status: "modeled"` 手动回写（**不会自己跟上**）；
2. 补 `_measured_faces.built`（体积/包围盒/圆柱面/对账结论）—— ★ 数字**从 run.json 里取**，不手抄；
3. ★ **补同名探针** `assets/probes/reducer_<part>.json`  
   （`sw_reducer --plan <part> --out <探针> --no-out`）—— 本轮就漏了这条，  
   被"每件有生成器的零件都有同名探针"那条断言拦下；
4. 给这件**自己的**回归用例（含反例）。

### ⑤ ★★ 校验分档：`PENDING` ≠ `FAIL` —— 不然体检入口会「永远报不可用」

**症状**：`sw_doctor`（面向用户的"一条命令出结论"入口）报  
**「结论：不可用 —— 1 项失败」**，而那一项其实是**设计变更导致的真机记录成代作废**  
（大齿轮 z52→z55、齿轮轴 z18→z15）。这些断言在**真机重跑之前永远不可能变绿**。

**为什么必须分档**：混在 `FAIL` 里 ⇒ 入口**永远**红 ⇒ **告警疲劳**，  
真正的新回归会被这 6 条淹没。（这正是"一条命令出结论"这个设计的命门。）

**修法**：`sw_selftest.pending()` 第三档。

| 档         | 含义                                 | 退出码 |
| --------- | ---------------------------------- | --- |
| `PASS`    | 过                                  | 0   |
| `SKIP`    | **这台机器上验不了**（没装目标软件/未请求 `--real`）  | 0   |
| `PENDING` | **验得了，但要先真机重跑**（记录已 `_superseded`） | 0   |
| `FAIL`    | 真回归                                | 1   |

★ **判据必须写死，否则它就退化成被明令禁止的"把错误降级成警告"**：  
**只有记录显式标了 `_superseded`**（= 已有人判定它是旧版、并写明了重跑方法）才允许降档；  
**没有这个标记的一律算 `FAIL`**。自检 `[2s]` 用源码扫描钉住（"每个 `pending(` 调用点  
往上 4 行内必须有 `_superseded` 门禁"），并配反例。

★ **检查器也要有反例 + 误伤回归**：`[2s]` 第一版把 `def pending(` **定义行**和  
测试函数自身也算成调用点 ⇒ **自己报了 7 个误报**。  
⇒ 检查器要同时证明两件事：**该报的能报出来**、**不该报的不许误报**（两者缺一不可）。

★ 顺带把 `sw_doctor` 从"只看退出码"改成**把 PENDING 显眼地列出来**（`rep.note`）——  
分档的目的是让它们**可见**，不是让它们**消失**。

---

## 94. 装配体（`.SLDASM`）四条**与流传文档矛盾**的实测结论

来源：`python scripts/sw_apisig.py --tlb`（2026-10-02 真机抽取）。  
**四条全部写反了的地方，恰好都是"教程/记忆里最常见"的写法。**  
离线回归在 `sw_selftest.py` 的 `[2t]` 段（不需要开 SolidWorks 就能验）。

### ① `AddMate` ~ `AddMate5` **全部已废弃**，只走 `CreateMateData` + `CreateMate`

官方原文：`Obsolete. Superseded by IAssemblyDoc::CreateMate.`  
⇒ 代码里**一个 `AddMate*` 调用都不许有**（自检扫非注释行钉住）。  
正确路径：

```python
data = asm.CreateMateData(mate_type)      # mate_type 是 swMateXXX_e 的值
data = cast_to_mate_specific_iface(data)  # 官方措辞："cast to the appropriate interface"
# ★★ EntitiesToMate 是 VARIANT（SAFEARRAY）—— **不能直接赋 tuple/list**！
#    直接赋会被 pywin32 **静默丢掉**（不抛异常、读回长度 0）。见 ③ 与 §95。
data.EntitiesToMate = VARIANT(VT_ARRAY | VT_DISPATCH, [ent1, ent2])
data.MateAlignment  = swMateAlignALIGNED
feat = asm.CreateMate(data)               # ★ 判据 = 特征树真的长了新节点
```

### ② `swMateAlign_e` 里流传最广的三个名字**已被官方标 Obsolete 并写明 "Do not use"**

`swAlignSAME` / `swAlignAGAINST` / `swAlignNONE` —— 枚举页脚注：  
`These values have been changed to correct improper mate alignment mapping.`  
（映射在某版被改过，旧名就是那次改动的牺牲品。）  
**正确值**：`swMateAlignALIGNED(0)` / `swMateAlignANTI_ALIGNED(1)` / `swMateAlignCLOSEST(2)`。

### ③ ★★ 只有**带星号**的配合类型支持"预选实体"

`IAssemblyDoc::CreateMate` 的 Remarks 明确：只有带 `*` 的类型支持 **pre-select entities**。

| 支持预选（有 `*`）                                                                                                                                                   | 不支持（**必须显式设 `EntitiesToMate`**）                          |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| COINCIDENT / CONCENTRIC / PARALLEL / PERPENDICULAR / TANGENT / LOCK / GEAR / CAMFOLLOWER / HINGE / LINEARCOUPLER / RACKPINION / SCREW / SLOT / UNIVERSALJOINT | **DISTANCE / ANGLE / SYMMETRIC / PROFILECENTER / WIDTH** |

⇒ 那五种光靠"先选中再建"**一定建不出来**（而且多半是**静默**的）。  
本项目做法：**一律显式设 `EntitiesToMate`**（预选照做，但不当唯一凭据）——  
一条代码路径覆盖全部类型，**少一个分支就少一个假绿灯**。

★★ **但 `EntitiesToMate` 本身还有个更深的坑**：它是 `Variant`（SAFEARRAY），  
`data.EntitiesToMate = (o1, o2)` / `= [o1, o2]` 会被 pywin32 **静默丢掉**  
（不抛异常、读回长度 0）。必须  
`VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_DISPATCH, [o1, o2])`。**详见 §95 ①**。

配合的选中**标记（mark）**：按 `AddMate5` 的 Remarks 表，除  
`swMateCAMFOLLOWER(8)` / `swMateWIDTH(16)` 外，其它类型都用 **mark 1**。

### ④ `FixComponent()` / `UnfixComponent()` 零参数、不返回成败

· **零参数** ⇒ 作用于**当前选择集**，必须先按名字选中组件；  
· **不返回成败** ⇒ 判据只能是 **`IComponent2.IsFixed` 真的变了**。  
自检有反例：`FixComponent` 静默无操作时，op **必须**报「状态没变」。

★ **另一条不是 API、是 SolidWorks 行为的坑**：**第一个插入的组件默认被固定**  
（`IsFixed=True`）。所以"插完就能配合"是假象 —— 第一件不用再 Fix，  
但固定其余组件，否则每加一个配合，SolidWorks 都可能挪动那些**没被固定**的组件。

### 附带：配合在特征树的**子特征**里 ⇒ `feature_names()` 看不见

· `feature_names()` 只走 `FirstFeature → GetNextFeature`（**顶层**）；  
· 装配体的配合在「**配合**」文件夹（`MateGroup`）的**子特征**里  
⇒ 只看顶层**永远只有一个 MateGroup**，看起来像"一个配合都没有"。  
· ⇒ 必须用 `GetFirstSubFeature` 遍历（`sw_connect.feature_tree()`）。  
· ★ `MateGroup` 是**文件夹不是配合** ⇒ `mate_count()` **显式扣掉**  
（`MATE_GROUP_TYPES`）；否则会**多数一个**。  
· ★★ **数配合的口径在 2026-10-02 真机后重新设计：从「穷举类型名」改成「按结构」**。  
旧做法：`MATE_FEATURE_TYPES` 白名单穷举所有配合类型名，`mate_count()` 数命中的。  
**为什么必须改**：白名单**必须穷举**，而 SolidWorks 的类型名**有意外** ——  
真机实测 `distance` 建出来的特征叫 **`MateDistanceDim`**（带 `Dim` 后缀！），  
照直觉写 `MateDistance` 就**静默漏数**（报告里少一个配合，看不出来）。  
新做法：**按结构数** = 「配合」文件夹（`MateGroup`）的**直接子节点**  
⇒ 不需要知道任何类型名，**新类型自动被数到**，不会漏。  
`MATE_FEATURE_TYPES` 降级为**旁证**（`unknown_mate_features()` 把没见过的  
名字报出来，**不是错误**）—— 这是"数目对了"之外的第二道防线。  
★ 仍只取**直接**子节点：嵌在子文件夹里的配合**不算**。

### 类型守卫（`asm` 属性）

`IAssemblyDoc` 的方法在**零件文档**上**不一定报错** —— 有的会**静默返回空**  
（例如 `GetComponents` 回 None）。于是"装配体里没有组件"和"我在改一个零件"  
看起来完全一样。⇒ 取 `asm` 前**必须核 `GetType == swDocASSEMBLY(2)`**  
（零件 `swDocPART = 1`），不匹配就**炸得响一点**（报错里带标题 + `GetType`）。

---

## 95. ★★★ 装配体真机五条「静默失败」—— 2026-10-02 现场标定

来源：`scripts/sw_asm_tree.py`（现场搭最小装配体 + 递归打完整特征树）配合  
`_diag/diag_insert_component.py` / `diag_component_name.py` / `diag_mate_entities.py`  
等**变量隔离**脚本，真机 SolidWorks 2024 + pywin32 后期绑定。

**共同点：五条全都不抛异常、不返回错误码、不打日志。**  
唯一能抓住它们的手段是**读回**（读回值 / 计数 / 特征树），  
而"读回"要能被写进判据 —— 所以离线回归（`sw_selftest.py` `[2t]`）为**每一条都配了反例夹具**，  
夹具能复现"静默"，缺陷才可能在没有 SolidWorks 的机器上被抓到。

### ① ★★★ `IMateFeatureData.EntitiesToMate` 是 VARIANT —— 直接赋 tuple/list **静默丢掉**

`sldworks.tlb` 里的声明就是 `Variant`（不是 `IDispatch[]`）：

```
ICoincidentMateFeatureData.EntitiesToMate  [0 参数] -> Variant   (get)
ICoincidentMateFeatureData.EntitiesToMate  [1 参数] -> void      (put <arg0>: Variant)
```

pywin32 在**没有参数类型信息**时，把 Python 的 tuple / list 编组成 VARIANT 会  
**静默丢掉里面的 COM 对象**。真机实测三种写法的「读回长度」：

| 写法                                                                 | 读回长度    |
| ------------------------------------------------------------------ | ------- |
| `data.EntitiesToMate = (o1, o2)`                                   | **0** ✗ |
| `data.EntitiesToMate = [o1, o2]`                                   | **0** ✗ |
| `data.EntitiesToMate = VARIANT(VT_ARRAY \| VT_DISPATCH, [o1, o2])` | **2** ✓ |

★★ **这条的伪装性极强**：`coincident` / `parallel` / `perpendicular`  
**支持预选**，EntitiesToMate 空着也照样能建出来 ⇒  
「三个类型通过」会让人以为「这条路径没问题」，  
直到遇到 **`distance` / `angle` / `concentric`**（不支持预选）才**集体暴雷**。  
**「部分通过」比「全不通过」更难查** —— 因为它给了一个假的安全感。

⇒ 本项目做法（`sw_ops._mate_entities`）：  
① 一律构造 `VARIANT(VT_ARRAY | VT_DISPATCH, [o1, o2])`；  
② **赋值后立刻读回**，长度 ≠ 2 就报错并点明根因（`op_add_mate` 的读回守卫）；  
③ 离线（无 pywin32）降级回普通 list —— **降级不炸主流程**，真机上万一降级会被守卫抓住。  
④ `_seq_len()` 取不到长度时回 **-1**（= "没读出来"），**绝不能当成 0**，否则守卫被骗过去。

### ② ★★ `AddComponent5` 的路径**只认反斜杠** —— 正斜杠**静默返回 None**

```
asm.AddComponent5("D:/out/x.SLDPRT", ...)   →  返回 None，组件**没进去**，无异常、无错误码
asm.AddComponent5("D:\out\x.SLDPRT", ...)  →  成功
```

★ **同一条 COM 族里路径规范化行为不一致**：`OpenDoc6` / `SaveAs` 因为代码里走  
`str(Path(...))`（Windows 下天然是反斜杠）所以**一直没暴露**。  
本项目此前习惯"Windows 路径一律正斜杠"—— 那只对**文件系统与 shell**成立，**对 COM 不成立**。

⇒ 新增 `sw_connect.win_path(p)`：把路径规范成 SolidWorks 内部一致的反斜杠形式，**幂等**。  
`op_insert_component` 喂 COM 前**必须**过它。（`GetOpenDocumentByName` 同证。）

### ③ ★★ 组件改名两条硬约束：**必须选中** + **`swExtRefUpdateCompNames` 必须 false**

`IComponent2::Name2` 官方 Remarks 原文：

> Before executing a name change, this property checks the **swExtRefUpdateCompNames**  
> setting. **If it is true, then the name change fails**; if false, then the name change succeeds.

真机实测本机该开关**默认 True**（`GetUserPreferenceToggle(18)` = True）⇒  
组件改名**静默失败**：赋值不报错、**读回还是旧名**。这正是"返回值会说谎"的又一种形态 ——  
**连异常都没有**。

⇒ 修法（`sw_ops._rename_component`）：  
① 先 `sw.select_component(old)` 把组件**选中**（官方要求）；  
② `with sw.temp_pref(("swExtRefUpdateCompNames", False)):` 临时关掉、**读完还原**  
（这是**机器级**用户设置，改了不还原会污染用户环境）；  
③ **读回核对**，不符就报错（名字没改掉 ⇒ 后面所有按名操作都会找不到对象，越早炸越好）。

### ④ ★★ 组件名 = `<基名>-<实例号>`，后缀是本件**固有**属性

真机实测：`box-1` --改名"地基"--> **`地基-1`** --再改名"上块"--> **`上块-1`**；  
插第二个同款零件得到 `box-2`（后缀按**零件实例**递增，**跟改名无关**）。

⇒ 两条推论：  
· **改名只换基名，后缀不动** ⇒ 断言口径必须是 `<新基名>` **或** `<新基名>-<数字>`。  
★ 第一版断言写成 `got != new ⇒ 失败`，**把成功的改名判成了失败**（真机踩到）。  
· **按名选中必须用带实例号的全名** —— `SelectByID2("box", "COMPONENT")` **选不中**（选中数 0），  
`SelectByID2("box-2", ...)` 才行。组件名的权威来源 = `IComponent2::Name2`。  
· `op_insert_component` 回的是**实际全名**（`renamed_from` 给的是原名），下游按名操作一律用它。

### ⑤ ★★ `SelectByID2` 对组件**谎报 False** —— 判据只能是**选中数**

真机实测：

```
ext.SelectByID2("box-2", "COMPONENT", ...)   → 返回 False，但**实际选中 1 个**
IComponent2::Select4(False, None, False)     → 返回 True，且真的选中
```

⇒ **绝对不能信 `SelectByID2` 的返回值**，判据必须是  
`ISelectionMgr::GetSelectedObjectCount2` 的**增量**。  
实现（`sw_connect.select_component`）：先用 `Select4`（返回值可信、无需名字），  
失败再退到 `SelectByID2` 并用**选中数增量**判定 —— 两条路都只看选中数。

★ 这是坑表 §1（"It's not the API, it's the return value"）的又一个实例，  
只不过这次连**组件选择**这么基础的操作都不例外。

### 附带：`IComponent2.GetBox` 与 `IAssemblyDoc.GetBox` **签名不同**

```
IComponent2.GetBox(IncludeRefPlanes: bool, IncludeSketches: bool)   ← 组件用这个
IAssemblyDoc.GetBox(Option: int)                                    ← 装配体用这个
```

★ 给组件版传整数会**静默读空**（不报错）。诊断脚本踩过一次。

### 附带：`AddComponent5` 的 XYZ 是**相对包围盒**的，不是原点

官方 Remarks：`X, Y, and Z are relative to the bounding box of the component.`  
实测传 `z=0` 落到包围盒 **z ∈ [-5, 5]**（即**中心**对齐 0，不是原点对齐 0）。

### 方法论：这五条教了我们什么

1. **"没报错"≠"成功了"**。这五条没有一条抛异常 ⇒ **凡是有"返回值/开关/类型"参与的地方，  
   都必须有一条读回判据**。本项目**新 op 必须加"有效性"一条**（铁律不变）。
2. **"部分通过"最危险**。三条配合类型能建、两条不能 —— 如果不做类型矩阵就不容易发现。  
   ⇒ 遇到"有的能成有的不能成"，先怀疑**共用的那段代码**（这里就是 `EntitiesToMate`）。
3. **同一条 COM 族内的行为不一致是常态**（路径分隔符）。⇒ 别把一个 API 的宽容度  
   推广到同族其它 API，**逐个真机验**。
4. **"离线可判"和"真机才能判"要分清楚**。类型名（`MateDistanceDim`）、  
   实例号后缀、开关默认值 —— 这些**只能真机**；而"赋值后要读回"、  
   "判据用计数不用返回值"这类**口径**，离线就能钉死。**两件事分开做**，  
   别把"离线写不出真机结论"当成"离线没什么可测的"。

---

## 96. ★★★ 装配体真机标定第二轮：会话会退化、`SelectByID2` 是**相机射线**、第二条配合必然失效

来源：`assets/probes/asm_basics.json` / `asm_mate.json` / `asm_mate2.json` +  
`_diag/diag_mate_align.py` / `diag_insert_api.py` / `diag_saveas.py` /  
`diag_boxes_and_pick.py` / `diag_distance_mate.py` / `diag_second_mate.py`，  
真机 SolidWorks 2024 SP0.1 + pywin32 后期绑定，2026-10-03。  
本轮把 `verified` 从 **72/80 推到 80/80**（8 个装配体 op 全部转正）。

### ① ★★★ `SelectByID2` 的坐标拾取是**相机射线** —— 只能选中朝向相机的面

`IModelDocExtension::SelectByID2(Name, Type, X, Y, Z, ...)` 传坐标时，  
SolidWorks 是**沿相机方向打射线**，不是"在模型空间里找最近的面"。

真机实测（默认等轴测视图，看的是 **+X / −Y / +Z** 三个方向）：

| 取点                         | 所在面       | 结果         |
| -------------------------- | --------- | ---------- |
| (20, 0, 0)                 | 地基 +X     | ✓          |
| (20, −8, −2)               | 地基 +X     | ✓          |
| **(80, 0, 10)**            | **上块 −X** | **✗ 恒选不中** |
| **(80, −8, 10)**           | **上块 −X** | **✗**      |
| **(80, 0, 7) / (80,0,13)** | **上块 −X** | **✗**      |
| (120, 0, 10)               | 上块 +X     | ✓          |
| (0, 0, 5)                  | 地基 +Z     | ✓          |
| (100, 0, 5)                | 上块 −Z     | ✓          |
| (100, −15, 10)             | 上块 −Y     | ✓          |
| **(100, 15, 10)**          | **上块 +Y** | **✗**      |

**背面（−X / +Y）的面一个都选不中，而且 m1 建配合前后完全一样** ⇒ 与配合、  
与组件位置都无关，纯粹是"这个面背着相机"。

★ **伪装性**：`add_mate` 报的是「配合实体没选全，没命中：`[{'type':'FACE','point':[80,0,10]}]`」  
—— 一句**指向坐标算错**的报错，而真因是"面背着相机"。你会去检查算式，  
检查一百遍也对不上。

⇒ **修法**（`sw_ops._select_asm_face_at` + `_asm_face_index`）：  
**枚举组件的面 + 距离判据 + `IEntity.Select4`**，与视角无关。  
零件层 2026-09-25（M2）早就做对过这件事（`_faces()` + `_select_obj()`），  
**装配体层缺这一套**，本轮补上。三个必须踩准的点：

1. **取组件实体用 `IComponent2.GetBody()`** —— **没有 `GetBody2`**。  
   照 `IModelDoc2.GetBodies2` 想当然 ⇒ "组件取不到面"，而**不抛异常**。  
   （签名以 `sldworks.tlb` 为准：`GetBody()` 与 `IGetBody()`。）
2. **`IFace2.GetClosestPointOn(x, y, z)` 收的是「零件局部坐标」，不是装配体全局坐标。**  
   证据（零件局部 x∈[0,40] 的盒子）：
   | 传入的全局点                                                            | 回的距离                            |
   | ----------------------------------------------------------------- | ------------------------------- |
   | (80, 0, 10)                                                       | **40.000 mm**（= 80−40）          |
   | (100, 0, 5)                                                       | **60.000 mm**                   |
   | (120, 0, 10)                                                      | **80.000 mm**                   |
   | (50, 50, 50)                                                      | **45.826 mm**（= √(10²+20²+40²)） |
   | (20, 0, 0)                                                        | 0.000 mm（本来就落在面上）               |
   | 逐位吻合 ⇒ 它把全局坐标当局部坐标算距离。\*\*必须先用                                    |                                 |
   | `IComponent2.Transform2`（`MathTransform.ArrayData`）做全局→局部逆变换。\*\* |                                 |
3. **`ArrayData` 的旋转约定官方文档没写清**（行主序还是列主序）⇒  
   **两种都试**，用"逆变换后的点必须落进零件局部包围盒"当**运行时自检**。  
   写错的后果是"点被映射到镜像位置"⇒ 选不中或选错面，**且不报错**。

修完后真机读回：`picked=['组件 地基-1 的面，距点 0.000mm', '组件 上块-2 的面，距点 0.000mm']`  
—— 两个面（含背面）**都精确命中**。

### ② ★★★ 同一文档里**第二条配合必然静默失效**（本机 SW 2024）

变体矩阵（`_diag/diag_second_mate.py`，每个变体都新建干净装配体、手写建配合流程）：

| 变体 | 第二条                 | 中间动作                             | 结果                          |
| -- | ------------------- | -------------------------------- | --------------------------- |
| —  | distance，**不先建 m1** | —                                | ✔ `MateDistanceDim`，配合数 0→1 |
| H  | coincident          | 无                                | ✘ `新特征=[]`，配合数 1→1          |
| I  | distance            | `ForceRebuild3(False)`           | ✘                           |
| J  | distance            | `sleep 0.6s` + `ClearSelection2` | ✘                           |
| K  | distance            | `ForceRebuild3` + sleep          | ✘                           |
| L  | distance            | 只 `ClearSelection2`              | ✘                           |

**每次失败都长这样**：`EntitiesToMate` 读回 = 2（写入成功）、  
`Distance` 读回 = 60.0mm（读得对）、`CreateMate` 返回 **feature 对象**、  
`err=None`、`GetErrorCode=0`、**特征树不长节点、配合数不涨**。

★ **三样判据全都报成功**（返回值 / 错误码 / GetErrorCode）⇒ 判据**只能**是  
配合数或特征树。这正是本项目铁律"返回值会说谎"的最强版本。

⇒ 边界用 `assets/probes/asm_mate2.json`（`meta.expect_fail` = XFAIL）钉住，  
**故意失败**、不计入套件失败，但**保持可复现**：哪天解掉了，`sw_probe` 报 XPASS  
并提醒删掉 `expect_fail`。  
★ **现实影响**：真实减速器装配要建几十条配合 —— 这是装配阶段的**一号阻塞**，  
必须在做装配之前解决（候选方向：`CreateMate` 之后强制 `Rebuild`、  
改用 `FeatureManager.InsertFeatureMate`、或每条配合换一个文档/一次 `ExitApp` 重连）。

### ③ ★★ `align=aligned` 在两配合面法线相反时**翻转 + 自旋**组件（§95 的续）

真机三态对照（`_diag/diag_mate_align.py`，40×30×10 的块从 `[80,-15,5,120,15,15]`）：

| align          | 配合后                                          | 结论                        |
| -------------- | -------------------------------------------- | ------------------------- |
| `aligned`      | `[31.559, -30.322, -5.0, 80.0, 12.712, 5.0]` | **翻 180° 且绕轴自旋**（还陷进另一件里） |
| `anti_aligned` | 原样                                           | 零位移                       |
| `closest`      | 原样                                           | 零位移                       |

两法线相反（上件下表面 −Z 贴下件上表面 +Z）时，`aligned`（同向）逼求解器翻转；  
而重合类配合只约束 1 平移 + 1 转动 ⇒ 剩 2 个转动自由度**被随机填** ⇒ 还会自旋。

⇒ **默认值从 `aligned` 改成 `closest`**（= 就近、保持当前姿态，UI 默认行为），  
并给 `add_mate` 加了 **`moved` 字段**：建配合**前后各读一次组件包围盒**  
（`SwSession.component_boxes()`，用 `IComponent2.GetBox(False, False)`），  
报出「谁被挪了多少、前后位置」。这是本 op &#x7684;**"有效性"判据**。

★ **顺带修掉一个假绿灯**：`component_boxes()` 读空时前后都空 ⇒ `moved` 为空  
⇒ 看起来"没动"。现在区分「没动」与「**测不到**」：回报 `measured_components`，  
为 0 而装配体明明有组件就明确告警。**校验器"通过"时也要怀疑校验器自己**（§84 的另一面）。

### ④ ★★ `self.model` 是**动态属性** ⇒ `_close_if_open` 会让 `save_as` 存错文档

`SwSession.model` 是 `@property`，每次重新读 `app.ActiveDoc`。而  
`save_as` 里 `self._close_if_open(p)` **会关掉另一份文档** ⇒ 关文档让 SolidWorks  
**换掉活动文档** ⇒ 随后的 `self.ext.SaveAs` 和"落盘校验"都在**操作/验证另一份文档**。

现场表现：`SaveAs 返回成功，但文档仍指向 (未保存) —— 实际没落盘`。  
真正的问题是"存错人了"，而报错指向落盘。

⇒ `_close_if_open(path, keep_title=...)`：要保命的那一份由**调用方显式传进来**  
（原来它自己读动态的 `self.model` 决定关谁，顺序一换就错）。  
`save_as` 一进来就 `doc = self.model` **钉住**，`Extension`、`GetPathName`、  
`GetType()` 全部走这个引用。**这就是「判据在验证别人」。**

### ⑤ ★★★ COM 调用会**随机瞬时失败** —— 读回判据 + 重试

同一个 `AddComponent5`、**同一会话、同一零件**，时好时坏：不抛异常、无错误码、  
组件数不涨。真机见过 09:47 插不进 `fresh.SLDPRT`、09:52 同一文件插得进。

隔离实验（`_diag/diag_first_asm.py`）证明了它**与文件无关**：  
同一进程里 `box.SLDPRT` 三个装配体全 FAIL、刚建的 `fresh.SLDPRT` 却 OK；  
十分钟后 `fresh.SLDPRT` 也开始 FAIL ⇒ 是**会话级抖动**，不是路径/零件问题。

⇒ 既然判据一律是读回，"没涨"就该**重试**：  
`COM_RETRY = 3` / `COM_RETRY_S = 0.35`，用于 `op_insert_component` 与 `_pick`。  
`insert_component` 的返回值带 **`attempts`** ⇒ 抖动次数在报告里看得见，  
不会被悄悄吞掉（否则"重试"就变成一个可能掩盖真实问题的动作）。  
重试用尽才报错，且报错点名「已重试 N 次」+「先跑 `scripts/sw_session_check.py`」。

### ⑥ ★★ 新增 `scripts/sw_session_check.py`：会话健康度**端到端冒烟**

2026-10-03 的教训：会话退化时，一次探针失败会让人去改**本来正确的 op**。  
所以**跑探针之前先验会话**：新建零件→拉伸→存盘→新建装配体→插 2 个组件→  
建 1 条重合配合→读配合数→存装配体，每一环都用**读回**当判据。  
三档结论：`HEALTHY` / `DEGRADED`（基础建模可用、插组件不可用）/ `BROKEN`，  
退出码 0/1/2。`sw_probe.py --preflight` 会在不健康时**直接中止、不跑任何探针**。

★★ 验证会话必须**真的插组件**：只验 `NewDocument` / `GetComponentCount` 的话，  
⑤ 那种故障一个都抓不到（实测这两种调用在退化后**仍然正常**）。

★ 收尾靠**打开文档的快照差集**（跑之前有什么 − 现在还多出什么 = 本轮新建的），  
**不靠"追踪自己开过的标题"** —— 因为 `save_as` 之后标题会变  
（`零件1` → `session_check.SLDPRT`），按旧标题关会漏掉它，漏掉 = 下一轮撞上  
"同名文件被打开"。每轮用**唯一临时子目录**，不与上一轮撞名。

### ⑦ ★ `AddComponent5` 的 8 参数签名是对的（别被"返回 None"骗去改签名）

7 个或 6 个参数会抛 `com_error (-2147352561, '非选择性的参数。')`，  
8 个参数才被接受 —— 说明签名正确，**问题不在参数**。  
`ret=None` 在成功时也可能是常态（`CreateMate` 同理），**返回值一律不作判据**。

### 方法论（本轮六条发现合起来）

1. **"点没命中"和"面背着相机"报出来是同一句话** ⇒ 报错文案必须能区分，  
   或者干脆**换一条与视角无关的判据**。别在算式上找bug。
2. **坐标系是最容易静默错一层的地方**（全局 vs 局部）——  
   本轮的 `GetClosestPointOn` 就是差这一层，而它**不报错**，只给你一个看起来  
   合理的距离（40mm / 60mm / 80mm 全部"像模像样"）。  
   ⇒ 拿到可疑的数字先**反推验证**（本轮：80−40=40、100−40=60，一一对上才敢下结论）。
3. **旋转矩阵约定没文档就两种都试 + 运行时自检**，别赌一种。
4. **"部分通过"仍然危险**：① 修好取面后 m1 立刻成功、② 但 m2 又暴露出  
   "第二条配合"的边界 —— 每修好一层就露出下一层，这是正常节奏，  
   关键是**每层都留可复现的探针**。
5. **判据和"测不到"必须分开**：`moved` 为空可能是"没动"，也可能是"读不到"。
6. **重试要留痕**（`attempts` 字段），否则抖动被静默吞掉，日后没人知道它抖过。

---

## 97. ★★★ 多面体（棱柱/支架/箱体）识别与重建 —— 现有工具的盲区与两个新工具

起因：用户拿 CSWA 校赛支架（多面体）测试时发现"这类零件处理和识别训练不足"。  
核查后的结论是准确的：`sw_faces.py` 给三张表（圆柱台阶 / 平面站位 / 锥面清单）、  
`sw_profile.py` 抽**回转半剖面** —— 两者都是**绕一根轴**的思维，  
**对多面体零件只能给出"一堆平面的面积"，说不出「这是一件什么样的棱柱」**。

新增两个工具（`scripts/sw_faceset.py` / `scripts/sw_prism.py`），  
全链已在真机走通并**互证**（见文末）。

### ① ★★★ `ISurface.PlaneParams` 的第 4 个数 `d` **不可信** —— 必须自己算

真机实测（`8-窥视孔盖`）：

| 面     | 实体位置          | `PlaneParams` 回的 `(n, d)` | 按 `n·x=d` 推     | `GetBox` 说的 |
| ----- | ------------- | ------------------------- | --------------- | ----------- |
| Face3 | **x = +23mm** | `n=(-1,0,0), d=+0.023`    | x = **−23mm** ✗ | x=+23 ✓     |
| Face5 | x = −23mm     | `n=(+1,0,0), d=-0.023`    | x=−23 ✓         | x=−23 ✓     |
| Face1 | z = +1.5mm    | `n=(0,0,-1), d=0.0`       | z=0 ✗           | z=+1.5 ✓    |

⇒ `d` 与它自己给的法向**不自洽**，而且**不同面的符号约定不一致**。  
`sw_faceset.py` 的读数自检显示：**箱座 86 个平面面里 69 个差 > 0.1mm，最大差 66mm**；  
窥视孔盖 6 个面里 3 个不对。CSWA 支架 14 个面里 12 个不对（**之前只是碰巧对**）。

⇒ **修法**：`d = n · center`，其中 `center` 取自  
`IFace2.GetClosestPointOn(0,0,0)`（**保证落在面上**，且与 `normal` 一样可靠）。  
工具会打印「`d` 与自算值差 > 0.1mm 的面有几个」，把这件事**变成可见的**。  
★ 教训：**"返回了一个看起来合理的数字"不等于"这个数字对"** ——  
66mm 的偏差也曾是个"合理量级的数"。拿到可疑读数先**反推验证**  
（`80−40=40`、`100−40=60` 逐位对上了，才敢下结论）。

### ② ★★★ 平面族必须按「法向**轴**」聚，且要带**一组站位**

三种聚法，三种错法（这一条我连踩两次）：

| 聚法                            | 后果                     |
| ----------------------------- | ---------------------- |
| 按**共面**（n 与 d 都同）             | 板的两面被拆成两族 ⇒ **板厚算不出来** |
| 按「n 与 d 都同」且合并时翻转 d           | 族代表与成员不自相矛盾 ⇒ 厚度同样丢    |
| ★ 按**法向轴**分组，族内记 `stations[]` | 板厚/台阶 = 相邻站位差 ✓        |

`sw_faceset.build_families()` 用第三种。板厚/台阶尺寸直接从 `thicknesses_mm` 读。

### ③ ★★★ "是棱柱"必须用**体积判据**，不能只看侧壁数

第一版判据是「侧壁数 ≥ 3」。真机结果：**`1-箱座`（201 个面）在 6 个方向上全部  
被判成"棱柱"** —— 因为面一多，任何方向都凑得出 3 个以上侧壁。  
这是典型&#x7684;**"部分通过"型假绿灯**：判据形同虚设，还给出很自信的结论。

⇒ 加**体积判据**：棱柱体积 = 截面面积 × 长度，  
`|V − A·L| / V ≤ 1%` 才算棱柱。箱座上这条立刻不成立（偏差 58%~161%）⇒ 正确否决。

★★ **附带抓到我自己的一个 2× 错误**：截面面积最初把**两个端面**都加进��  
⇒ 理论体积正好是实际的两倍 ⇒ 偏差**恰好 100.00%**。  
**"整 100%"这个整数本身就是信号** —— 真机第一次跑出来就是它。  
现在截面面积取「**某一个站位**上那组盖板面积之和的最大值」（端面被拆成多片时也对）。

### ④ ★★ 判据要有三档：`prism` / `weak` / `not_prism`

没有体积数据时**只报 `weak`（证据不足）**，不报"是棱柱"。  
宁可说"证据不足"，也不给一个假的肯定 —— 这与本项目"没标定就不许回 0"同源。

### ⑤ ★★★ 截面提取：每条几何边被**两个相邻面各报一次**

真机（CSWA 支架）：12 个侧壁面 → 48 条边 → 丢掉 24 条平行于挤出轴的竖棱  
→ 剩 **24 条**，而正确答案是 **12 条**。

★★ **危险之处**：不去重&#x4E5F;**"看起来是成功的"** —— 24 条能接出 12 个 2 段环、  
余边 0、不报任何错。**环数有了、形状全错**，比直接失败难查得多。  
⇒ `sw_prism.dedup_edges()`：按两端点（**双向**比，因为 `SenseFlag` 会翻转）去重。

### ⑥ ★★ 整圆孔在截面里起止点**相同** ⇒ 会被"竖棱"规则误丢

孔是多面体零件最常见的特征。判据必须**按曲线类型**而不是"投影是否退化成一点"：

| 边的曲线       | 圆/线轴 vs 挤出轴 | 结论                                 |
| ---------- | ----------- | ---------------------------------- |
| `IsCircle` | 圆轴 **∥** d  | 截面里的**整圆孔** ⇒ 保留（用圆心当代表点）          |
| `IsCircle` | 圆轴 ⟂ d      | 侧看是一条线段 ⇒ 当 `line`                 |
| `IsCircle` | 起止点不同       | 截面里的**圆弧/圆角** ⇒ `arc`（**半径要单独汇总**） |
| `IsLine`   | 线向 **∥** d  | 竖棱 ⇒ **丢**                         |
| `IsLine`   | 线向 ⟂ d      | 轮廓边 ⇒ 保留                           |

★ 漏了"轴向平行于挤出轴的**圆柱面**"这一类侧壁面 ⇒ 孔**完全不出现**，  
而报告里"闭合环 1 个、未接上 0 条"看着完全正常。

### ⑦ ★★ 三个单位/量纲 bug（只有拿解析值对账才抓得到）

| 位置               | 症状                           | 真相                          |
| ---------------- | ---------------------------- | --------------------------- |
| `classify_loops` | `signed_area_mm2` 报 `0.0008` | 存的是 **m²**，字段名写着 mm²（差 1e6） |
| 同上               | `perimeter_mm` 报 `0.2681`    | 存的是 **m**                   |
| `emit_plan`      | `check_bbox` 期望值顺序错位         | `u/v/d` 正交基没映射到 (X,Y,Z)     |

★ 教训：字段名带 `_mm` / `_mm2` 时，**赋值处必须乘上换算**；  
自检里要有一两条**解析值对账**（`800mm²`、`46×46` 这种手算能得的数）。

### ⑧ ★★ 签名必须**平移不变**，否则对账给出假失败

重建件往往建在**基准面**上，与参照件差一个纯平移（`sw_prism --emit-plan`  
把差值记在 meta 的 `origin_shift_mm`）。用**绝对站位**做签名 ⇒ 平移 1.5mm 就被判  
"形状不一致"。

⇒ `signature(relative=True)`（默认）用**相邻站位差**；`compare()` 比**包围盒尺寸**  
（max−min）而不是绝对 min/max，**中心偏移单列成"信息"**&#x4E0D;进判定。  
★ 位置与形状是两件事，别混在一张 PASS/FAIL 表里。

### ⑨ ★ 斜挤出方向**明确拒绝**出计划

CSWA 支架的 65° 斜筋意味着截面可能落在斜面上。`select_plane` 只能选基准面  
⇒ `emit_plan` 在轴不与任何全局轴平行时**不生成建模步骤**，并在 meta 里说明原因。  
★ **宁可少给一份计划，也不要给一份会画在错平面上的计划。**

### ⑩ ★ 新工具的层次划分：读数据 / 判据 / 出图

| 层                              | 靠什么保证                                                                                    |
| ------------------------------ | ---------------------------------------------------------------------------------------- |
| 读数据（`GetSurface` / `GetCurve`） | 只能真机验（随版本变）                                                                              |
| **判据**（聚族 / 体积 / 接环 / 去重 / 签名） | **纯函数** ⇒ `sw_faceset.self_test()` / `sw_prism.self_test()` + `sw_selftest.py [2u]` 离线钉死 |
| 出图（`--emit-plan`）              | 离线 `sw_plan --check` + 真机跑一遍 + `--compare` 对账                                            |

★ 本轮所有真 bug（①③④⑤⑥⑦⑧）**全部出在判据层** ⇒ 不离线钉就等于没有防线。  
两个工具都提供 `--self-test`（不开 SolidWorks 就能跑），并被 `sw_selftest.py [2u]` 调用。

### 全链真机互证（2026-10-03）

参照件 `8-窥视孔盖` → 识别 → 截面 → 计划 → 重建 → 对账：

```
sw_faceset  体积判据：棱柱沿 Z、截面积 1982.5 mm²、体积偏差 0.000%
sw_prism    截面：外轮廓 46×46（周长 184、面积 2116）+ 孔 Ø11 与 4×Ø3.5 @(±18,±18)
            独立算截面积 = 2116 − 95.03 − 4×9.62 = 1982.49   ← 与上面逐位吻合
sw_plan     --check 通过、0 个警告
真机        --run 13/13 步全过（含自校验 check_bbox）
sw_faceset  --compare：体积 5947.4 vs 5947.4（0.000%）、尺寸 [46,46,3] 逐项 0.0000mm、
            面集签名 0 项差异 ⇒ **形状一致**（中心偏移 [0,0,1.5] = 纯平移，符合预期）
```

CSWA 支架：识别出**沿 X 的棱柱、12 侧壁、厚 28**（对上图纸的 28）；  
4 个平面族里第 4 族法向 `(0, −0.4225, −0.9064)` ⇒ `atan(0.9063/0.4226) = 65.0°`  
（**图纸上那个 65° 斜筋角被从参照件反推出来**）；截面 12 段、面积 2519.29 mm²，  
与端面面积算出的 2519.3 吻合；顶点里的 8 / 24 / 16 与 25°（= 65° 补角）都对上图纸。

---

## 98. ★★★ 对称件「建一半 + 镜像」与装配定位面 —— 决策层（`scripts/sw_symmetry.py`）

起因：用户提出两条建模策略，原话：

> 「对于复杂的对称件可以尝试多种方案，可以把基准面作为对称中心面，建一半的特征  
> 然后再用对称命令把特征对称过去……我们提供的工具越多，agent 一次完成建模的概率越大」

> 「如果建模时直接选取正确的基准面进行建模，在装配的时候就可以加快装配效率，  
> 减少冗余操作和出错的概率，提高成功率」

核查结论：op 层其实**已经有** `mirror`（按特征名 + 基准面镜像）、`mirror_body`、  
`sk_mirror`（草图图元镜像），都 `verified=True`。缺的是**上游决策层** ——  
没人告诉 agent「这件件关于哪个平面对称、该建哪一半、哪个面是装配定位面」。  
本节记录新工具 `sw_symmetry.py` 的设计，以及**五个真 bug**（全部不抛异常、  
全部直接改变建模策略）。

### ① ★★★ 对称面站位**不能取包围盒中点** —— 差 0.04mm 就让对称度从 97% 掉到 0

真机（`C:/学习软件/一级减速器/1-箱座.SLDPRT`）：

```
包围盒  x ∈ [-52.994, +52.915]   ⇒  中点 = -0.0395mm
而面积配对给出的真实对称面是   x = 0：
    d = -52.0mm   面积 4418.1mm²
    d = +52.0mm   面积 4418.1mm²      ← 逐位相等 ⇒ 这一对互为镜像
```

用中点算，对称度 **0.00%**（真机实得）；用 `x=0` 算，**97.33%**。

★ **这就是「复杂对称件最容易丢特征」的机制**：  
站位偏 0.04mm ⇒ 每个面的镜像都偏 0.04mm ⇒ **一个都配不上**  
⇒ 报「不对称」⇒ 计划去**建全件**（而不是建一半 + 镜像）  
⇒ 特征数翻倍、草图容易画歪、改一个尺寸要改两遍。  
★ 而且**报告看不出原因** —— 只写"对称度 0.00%"，  
不会说"你的对称面找偏了 0.04mm"。

⇒ 修法（`best_symmetry`）：候选站位分四类，**按可信度从高到低**试：  
① **面积相等的站位对**的中点（证据最硬）  
② 相邻两站的中点  
③ 单站  
④ 包围盒中点（兜底）  
取对称度最高者。报告里**同时给出包围盒中点与最优站位的差**，  
差得多就明说「中点不是对称面，别拿它当镜像面」。

### ② ★★★ 「面积相等的对」**不要求相邻** —— 最自然但错误的假设

第一版只试"相邻两站的中点"。箱座的 `d=-52` 与 `d=+52` **不相邻**  
（中间夹着 `−26 / −20 / +45 / +48 / +50` 五个不对称站位：凸台、沉台、螺柱座）  
⇒ 真相压根不在候选里 ⇒ 报出 `−20`（`−26/−20` 那对的中点）⇒ 又是 0.00%。

★ "取相邻两站"是很自然的直觉（板厚类对称确实相邻），但**复杂件上不成立**。  
⇒ 补第一类候选：**任意两站**的中点，只保留**面积接近**的那些  
（`|·|` 相对差 ≤ `SYM_AREA_REL_TOL`）—— 面积不等的面镜像过去也对不上，  
试了也是白试。这一步把候选从 O(n²) 压到"少数几个面积相等的对"。

### ③ ★★★ 配对时**两侧面积都要计入** —— 一个会计错误改变了建模策略

第一版找到配对 `(A, B)` 后只累加 `a` 的面积 ⇒ `B` 那一份落进 `unmatched`  
⇒ 一个**完全对称**的盒子只有 **92.11%**（`unmatched=300mm²`）。

后果很实在：若阈值附近，这个低估会让"对称件"**过不了 90% 阈值**  
⇒ 被判成"不对称" ⇒ **建模策略整个反过来**（建全件 vs 建一半 + 镜像）。

⇒ 配对时 `matched += a_area + b_area`，配对表里记 `a_area_mm2` / `b_area_mm2`。

### ④ ★★ "镜像后落回自己"必须拆成 `on_plane` 与 `cross_plane` 两类

第一版只分"自己配自己"一类，结果一个完全对称的盒子只有 92%，而且  
**Y±/Z± 四个侧壁被误报成"中面上的面"**。

拆开才对：

| 类别            | 判据                    | 建模含义                                      |
| ------------- | --------------------- | ----------------------------------------- |
| `on_plane`    | 位置在对称面上 **且法向 ⊥ 对称面** | 镜像会**重叠** ⇒ **只能建一次**（箱体中面上的加强筋端面、分箱面贴合面） |
| `cross_plane` | 位置在对称面上但**法向 ∥ 对称面**  | 是**侧壁**，自己就关于中面对称 ⇒ **整片建出来即可，不用镜像**      |

★ 判错这一类 ⇒ 计划会把四个侧壁也只建一半 ⇒ 建模必错。

### ⑤ ★★ 装配定位面的方向必须按**位置**定，不能按**法向**定

`mount_faces` 第一版按法向分 6 组。一个 40×30×10 的盒子只出 **3** 条  
（X+/X− 归成一组、取了面积大的那个，小的丢掉），而真实答案是 **6** 条。

原因与 §97 ① 同源：SolidWorks 里两个相对面的 `normal` **可能同向**  
（取决于建模顺序与面的朝向约定）。**位置是几何事实，不受朝向约定影响。**

⇒ ① 用法向定**轴**（±法向算同一根轴）；② 用**面中心相对包围盒中心的位置**定符号。

### ⑥ ★★ 判据用**面积**而不是"面对个数"

同样是"1 个面配不上"：一个大面（5000mm²）把对称度拉到 **43.18%**，  
20 个小面（合计占 0.3%）只拉到 **99.74%** —— 权重差 3 个数量级。  
数面个数会让"小特征全对、大面错"的情况看起来很对称。

### 真机验证（2026-10-03）

| 件                              | 面数  | 对称面       | 对称度           | 结论                                                              |
| ------------------------------ | --- | --------- | ------------- | --------------------------------------------------------------- |
| `D:/out/cswa_1_2_final.SLDPRT` | 14  | **X = 0** | **100.00%**   | `keep=1 面 / mirrored=1 面 / 侧壁 12 面` ⇒ **建一半 + 一个 `mirror`**     |
| `1-箱座.SLDPRT`                  | 201 | **X = 0** | **97.33%**    | `keep=89 面 / mirrored=87 面` ⇒ 建一半 + `mirror`（用户原话：「箱座通过中心对称拉伸」） |
| `1-箱座.SLDPRT`                  | 201 | —         | 0.00%（用包围盒中点） | ★ 反例：中点 −0.0395 ≠ 对称面 0.000                                     |

**装配定位面**（`1-箱座`）：

```
X正 Face26  9020.9mm²  18.7%   站位 -20.0mm
X负 Face25  9020.9mm²  18.7%   站位 -20.0mm      ← 左右两面逐位相等，与 X=0 对称吻合
Y负 Face29 18465.5mm²  38.3%   站位 -40.0mm      ← 底面（贴合箱座安装面）
```

CSWA 支架：`X±` 各 2519.3mm²（= `sw_faceset` 独立算出的截面积 2519.3）⇒  
与用户图上标的「红色 = 从左/右装」完全对上。

### 方法论

1. **决策层的 bug 比执行层贵**。执行层错了，一步失败看得见；  
   决策层错了，后面每一步都"成功"，但产出是错的建模策略。
2. **"很自然的直觉"在复杂件上不成立**（相邻两站）。先问"这个假设在真机数据上  
   成立吗"，再写代码。
3. **会计错误也会改变策略**（③ 少加一半面积 → 判成不对称）。  
   凡是"两侧配对"的判据，都要单独验一次两侧都算进去了。
4. **报告要能解释"为什么不对"**。只报"对称度 0.00%"没用；  
   报出"包围盒中点差 0.04mm"才能让人立刻知道该改什么。
5. **离线判据 + 真机读数**（与 §97 同源）：本节 5 个 bug 全在判据层，  
   所以 `sw_symmetry --self-test` 有 22 条断言，并被 `sw_selftest.py [2v]` 调用。

---

## 99. ★★★ 语义分段层（`scripts/sw_segment.py`）——「按什么顺序建、哪段该镜像」

起因：用户第三条策略，原话：

> 「大齿轮我们就可以拆分成三部分，一部分齿轮主体可以通过两侧对称进行拉伸，  
> 然后切除减重槽的时候两侧是对称的，我们就可以切除一半然后进行对称命令完成  
> 另一半的特征建设」

前三层解决「几何上是什么」（`sw_faceset` 平面族/棱柱 → `sw_prism` 截面 →  
`sw_symmetry` 对称面/半边/装配定位面），这一层解决「**该怎么建**」。  
读面清单 → 按**几何证据**归类成功能段 → 每段给「基准面 / 是否镜像 / 建议 op / 为什么这么判」。

### ★★ 先说取证结果：**`33-齿轮-m=2,z=55` 根本没有减重槽**

真机读数（`sw_faceset.read_faces`，350 个面）：

```
类型分布  cylinder 115 / plane 9 / cone 116 / other 110
圆柱半径谱 Ø32(1) / Ø48(2) / Ø94(2) / Ø105(55) / Ø114(55)
9 个平面：Face56·57  z=±13  Ø32 区域 1802mm²  （两端凸台端面）
         Face168·169 z=±13 Ø22 区域  484mm²
         Face177·179 z=±4.5 Ø94 区域 5130mm²  （腹板，厚 9mm）
         Face170 x=+4.779 / Face172 x=−5.221 / Face173 y=17.745  （键槽 10×8）
other 110 个面积**全是 123.5mm²**、cone 116 个半锥角**全是 0°**
      ⇒ 那是**渐开线齿廓**（IsPlane/IsCylinder/IsCone/IsSphere/IsTorus 全 false）
```

⇒ 半径谱里**没有任何成组（≥4 个同半径）的中间半径圆柱面** ⇒ `pocket = 0`。  
真实分段 = `rim(110面/2半径组) + web + hub×2 + bore + keyway`。

★ **「本该有却没有的段」必须报出来**（工具会明写「没有减重槽/腹板孔」）。  
一个"本该有却没有"和一个"错判"同样重要：它会让人以为建模漏了，  
或者以为工具坏了。沉默 = 让下一位读者自己猜。

### ★★ 补记（用户澄清后）：`33-齿轮` 是**腹盘式**齿轮，减重是**环形腔**不是孔阵列

用户原话：「齿轮那可能是腹盘，我说错了」。

按半径分组补上 **z 跨度**后，结构完全清楚（真机实测）：

```
R=16.000  1 个  z∈[-11,+11]   中心孔 Ø32
R=24.000  2 个  z∈[-11,+11]   ★ 腹板两侧的环形减重**腔**（Ø48）
R=47.000  2 个  z∈[-11,+11]   ★ 腹盘外缘 Ø94（与 Face177/179 腹板面对上）
R=52.500 55 个  z∈[-13,+13]   齿（两半齿高不同 ⇒ 一组两个半径）
R=57.000 55 个  z∈[-12,+12]   齿
```

⇒ **腹盘式（web）齿轮**：`中心孔 → Ø48 减重腔（两侧对称）→ Ø94 腹盘
  （z=±4.5，厚 9mm）→ Ø114 齿圈`。

#### ★★★ 这暴露了 `pocket` 判据的一个盲区，而且错的方向&#x662F;**「报了假没」**

第一版的 `pocket` 要求「**成组**（≥4 个同半径）+ 半径介于孔与齿圈之间」——  
那是为了认**孔阵列**（腹板上开一圈减重孔）。而腹盘式齿轮的减重是  
**1~2 个大直径环形腔** ⇒ 成组数不足 ⇒ 上一版明写「**没有减重槽**」。

★ 那个"没有"是**假的**。这与坑表 §99 开头那条是**同一类错误的反方向**：  
· 上一轮：**报了"有"、其实没有**（55 个齿被当成减重槽）  
· 这一轮：**报了"没有"、其实有但形态不同**（Ø48 减重腔被当成不存在）

⇒ **「报『没有』」本身也必须有依据**。单一判据的阈值不成立时，  
要**换判据再问一次**，而不是直接下"没有"的结论。  
`unknown` 与 `absent` 一样需要证据。

⇒ 新增 `relief` 段：轴 ∥ 主轴、数量 `1~POCKET_MAX_FACES(3)`、  
半径介于孔与轮缘之间、轴向跨度明显大于局部板厚 ⇒ 环形减重腔。  
**两侧对称 ⇒ 切一半 + 镜像**（这正是用户原始设想的那种形态）。  
修完：大齿轮分段从 4 段 → **7 段**，覆盖率 **26.6% → 39.7%**。

★ 附带一条工程教训：补 `relief` 时，那条 `if not pockets: notes.append("没有减重槽")`  
被**整段覆盖**掉了 —— 而"报没有"正是我们要防的错误方向。  
⇒ 于是自检加了一条**反向断言**：「只有中心孔+齿时**才**说没有减重」。  
凡是"报否定结论"的代码路径，都要有对应的正例断言守着。

### ① ★★★ 轮缘要收**全部**半径组 —— 否则 55 个齿被误报成减重槽

真机齿有**两组**：R57（齿顶）与 R52.5（另一侧），两组都 ≥ 0.85×外径。  
第一版循环里 `break` 掉了第二组 ⇒ 它落进「半径介于中心孔与齿圈之间且成组」  
的 pocket 判据 ⇒ **55 个齿被标成"减重槽"**，还建议对它 `mirror`。

⇒ 轮缘收**所有**满足阈值的组（记 `n_groups` / `radii_mm`），  
pocket 判据再显式排除「半径已被收进轮缘」的组。

### ② ★★★ 主轴必须**按面积投票**，不能取"最小半径圆柱面"

真机箱座 88 个圆柱面里最小的是 **Ø2.5 油标孔**（一个小孔），  
**它的轴与箱座主轴垂直**。第一版拿它定主轴，那件是**蒙对的**。

⇒ 改按「所有圆柱面按轴分组，**总圆柱面积最大**的那根轴」投票。  
主轴上的孔/沉孔/轴承座孔面积最大 —— 这是几何事实，不依赖"哪个孔最小"。

### ③ ★★ 归类**覆盖率**必须可见 —— 漏 90% 却报"分好了"

真机箱座（201 面）第一版只分成 4 段、**覆盖 3% 的面积**  
（86 个平面里绝大多数是箱壁/筋/轴承座孔壁，全没归类）  
而报告看着完全正常。

⇒ ① 补 `shell`（箱壁/侧壁）段 = 法向 ⊥ 主轴的平面；  
② 报告给**归类覆盖率** = 已归类面积/总面积，低于 50% 时**明写**  
「余下是曲面/斜面，要知道自己漏了什么」。  
修完覆盖率 **3% → 54.6%**，`shell` 一段就占 46.0%。

★ 「看起来成功」的危险变体：段数、段名、基准面、镜像建议**全都齐备**，  
只是**漏了九成**。覆盖率是唯一能一眼看出这件事的指标（§96⑤ 同源）。

### ④ ★★★ 键槽判据绕了三轮才找对：**站位要沿法向投影**

真机键槽 = `Face170`(x=+4.779, n=+X) + `Face172`(x=−5.221, n=−X) + `Face173`(槽底)。  
键宽 = 4.779 + 5.221 = **10.000mm**（键 10×8 逐位对上）。

三轮错法：

1. **「面本身细长比」**：槽壁是**零厚度**的平面（box 的 x 跨度 = 0）  
   ⇒ 细长比恒为 0 ⇒ 永远认不出来。
2. **用 `d = n·c` 的差**：两片法向**相反** ⇒ `d` 也反号 ⇒ 两片 `d` **都是 +5.0**  
   ⇒ `abs(d1−d2)=0`（§97① 同源：相对面的 `d` 符号约定不一致）。
3. **用 `center` 算站位**：`center` 是 `GetClosestPointOn(0,0,0)` 的结果，  
   落在面上但**不在对称位置**（真机两片都在 y≈+15.5）⇒ 也是错的。
4. **「沿 box 三维里最厚的那一维」**：`x` 跨度 0 < `y` 跨度 4.4 ⇒ 挑中 y  
   ⇒ 两片中点都是 15.544 ⇒ 键宽 0。

⇒ 正确做法：**沿该面法向投影 `GetBox` 的中点**（与法向符号无关，取绝对值），  
键宽 = 两片站位**之和**。  
★ 教训：**"哪个轴"比"用哪个字段"更根本**。前三轮都在换字段，  
第四轮才想到"槽壁恰好是零厚度的那片"⇒ 沿最厚维必然挑错。

### ⑤ ★★ 键槽必须**贴着主轴** —— 否则轴承座孔壁被当成键槽

真机箱座上「间隙 18.0mm」那对平行面是**轴承座孔壁**（Ø47/Ø40），不是键槽。  
判据加一条：两片到主轴的距离都 ≤ 12mm（键是装在轴上的）。  
（键槽宽度量级也收窄到 5~16mm，真机键 10、8 都在内。）

### ⑥ ★★ 建模顺序按**依赖**，不是按面积

```
bore(中心孔) → keyway(键槽) → shell(箱壁) → web(腹板) → hub(轮毂) → pocket(减重槽) → rim(轮缘)
```

★ **按面积从大到小建是错的**：齿是面积最大的段，但它依赖主体已存在。  
真机大齿轮：齿 4155mm² > 腹板 5130mm²？不，腹板更大；  
但**箱座**里 `shell` 65967mm² 是绝对最大段，它必须**最先**建。

### 真机验证（2026-10-03）

| 件                | 面   | 主轴    | 对称轴      | 覆盖率       | 分段结论                                                                                |
| ---------------- | --- | ----- | -------- | --------- | ----------------------------------------------------------------------------------- |
| `33-齿轮-m=2,z=55` | 350 | Z     | Z(99.8%) | 26.6%     | web(建一半+镜像) / rim 110面2组(整件) / bore Ø32 / keyway 10.0mm / shell 3面；**明写「没有减重槽」**    |
| `1-箱座`           | 201 | **X** | X(97.3%) | **54.6%** | shell 56面 46.0%(**建一半+镜像**) / web 9020.9mm²(建一半+镜像) / bore / hub×2；**无键槽**（孔壁被正确排除） |

离线：`sw_segment --self-test` 21 条断言全绿；`sw_selftest.py [2w]` 把  
「两组半径都收」「没有的段要报」「覆盖率可见」「面积投票」  
「站位沿法向投影」「op 都要存在」全部钉死。回归 2859 → **2881**。

### 方法论

1. **"看起来成功"最危险的形式是"字段齐备但漏了九成"** ⇒ 任何分段/分类工具  
   都必须给**覆盖率**。
2. **"没有某类"与"错判"同等重要**，要显式报出来。
3. **蒙对比错更危险**：那件"最小半径圆柱面正好轴向一致"是运气；  
   换个件就错，而且不会有人发现。判据要选**不依赖巧合**的那条。
4. **"用哪个字段"之前先问"沿哪个轴"** —— 键槽三轮错法都栽在轴上。
5. 分类判据**尽量只依赖"几何事实"**（面积、位置、半径），  
   少依赖"哪个最小/哪个最大"这种在复杂件上会变的量。

---

## 100. ★★★ `mirror`（特征级镜像）的两个硬前提 —— 2026-10-03 排查 90 分钟的结论

起因：为 #61「箱座建一半 + mirror」探路，先问「`mirror` 能不能一次镜像**多个**特征  
（箱座要镜像的不止一个特征）」，结果撞出一串意外。

### 排查过程（值得原样记下来，因为它示范了该怎么查）

| 步骤 | 做法                                                           | 结果                                          |
| -- | ------------------------------------------------------------ | ------------------------------------------- |
| 1  | 自写诊断：3 个特征 + 1 个面全选上（选择集 = 4，`BODYFEATURE` 类型正确）             | `InsertMirrorFeature` 返回 None、**无新特征**、体积不变 |
| 2  | 变体矩阵：特征数 1/2/3 × `BGeometryPattern` True/False × 4参/5参版      | **12 种全灭**                                  |
| 3  | 怀疑会话退化 → 跑 `sw_session_check.py`                             | **HEALTHY 8/8**（但它没有 mirror 能力项 ← 缺口）       |
| 4  | 怀疑镜像面选错（用了 `Front`）                                          | 换成 `Right` 仍全灭                              |
| 5  | 怀疑 `IFeatureManager` 句柄取得不对（`sw.fm()` vs `typed(model,IFM)`） | 两个**不是同一对象**，但**两个都失败**                     |
| 6  | 怀疑"多个文档并存" ⇒ 逐轮清理后单跑                                         | 干净单轮**仍失败**                                 |
| 7  | ★ 把同一段步骤**落成 `.py` 文件**再跑（怀疑 `python -c` 内联的问题）              | ★★ **成功了**，体积 11214.6 → 10429.2             |

第 7 步为什么成：`sw_probe` 跑同一个探针一直 15/15 通过，而我内联写的一直失败。  
**同样的 op 序列、同样的会话、差别只在"代码怎么送进 Python"** ——  
所以真正的差异不在 SolidWorks，而在**我的内联脚本和探针文件之间的某一处**。  
第 8 步把它定死了（下表）。

### ① ★★★ 镜像面必须**穿过实体**，否则 8 种组合全灭

第 8 步：同一段代码，只换板的画法：

| 板                                | 圆              | 镜像面 `Right` | 结果                                   |
| -------------------------------- | -------------- | ----------- | ------------------------------------ |
| `rect(-30,-10, 30,10)` **中平面拉伸** | `cx=20, cy=0`  | 穿过实体        | ✔ `Mirror1`，体积 11214.6 → **10429.2** |
| `rect(0,0, 40,30)` **中平面拉伸**     | `cx=20, cy=15` | 穿过实体        | ✘ 8 种组合全灭                            |
| `rect(0,0, 40,30)` **单向拉伸**      | `cx=20, cy=15` | 穿过实体        | ✘ 8 种组合全灭                            |

★ 关键不是"单向还是中平面"，而是**板是否关于镜像面对称**：  
`x1=-30` 那块板横跨 `x=0`（`Right` 面正好从中间切开）⇒ 能镜像；  
`x1=0` 那块板**整个在 +X 侧**，`Right` 面只切到它的**边界**（不是内部）  
⇒ 镜像结果与原件重合 ⇒ SolidWorks 什么都不建。

⇒ **「建一半 + 镜像」的前提是那一半真的只占镜像面的一侧**。  
这不是 op 的限制，是**几何前提** —— 但 op 现在的报错完全没提这一条  
（只列 8 种组合的组合名 + 四条泛泛的排查建议）。

⇒ **建议给 `mirror` 的报错补一条**：  
「镜像面必须**穿过实体**（那一半要真的在它一侧）。  
你的板 x∈[0,40] 而镜像面是 x=0 ⇒ 只切到边界 ⇒ 镜像结果与原件重合。」  
—— 这正是 §84「报错要能指出根因」的又一次应用。

### ② ★★ 一次镜像**多个特征**：本机未验证成功（能力未知，不是"不行"）

第 1~2 步里，选中数确实是 2/3/4（特征 + 面都对），但**没有一次能建出特征**。  
⚠ 由于第 ① 条的坑（我的测试件不满足"镜像面穿过实体"），  
**这条结论是"未验证"，不是"不支持"** —— 不能写成"一次只能镜像一个"。  
⇒ 要下结论必须先造一个**满足前提**的多特征件（板横跨 x=0）再测。  
⇒ **给 `sw_session_check.py` 补一条 mirror 能力项**（现在它只测  
part/assembly/insert_component/mate，**没测 mirror** ⇒  
「会话健康」并不覆盖镜像能力）。

### ③ ★★ 诊断脚本的收尾必须按「**无路径**」认领自己的文档

`diag_mirror_count.py` 按路径白名单关文档 ⇒ `new_part` 建的文档**还没存盘、  
路径是空字符串** ⇒ 一个都没关掉 ⇒ 5 个 dirty 文档累积在会话里。

★ 后果不只是"下次撞名"：多个 dirty 文档并存时，本项目**误判过一次会话退化**  
（`AddComponent5` 插不进 + `InsertMirrorFeature` 全灭），  
而真正的原因只是**文档没关干净**。  
⇒ 收尾判据要用「**跑之前的文档标题集合**」做**差集**（`sw_session_check.py`  
已经是这个做法，`_diag/` 下那批脚本还没有），或者按「无路径 = 我的」认领。

### ④ ★★ `read_only=True` 打开的参照件，关掉时**仍被标 dirty**

用 `sw_faceset` / `sw_symmetry` / `sw_segment` 读 `C:/学习软件/一级减速器/*.SLDPRT`  
之后，文档列表里出现 `33-齿轮-m=2,z=55.SLDPRT` 且 `GetSaveFlag() = True`。  
`GetPathName` 有值、`GetSaveFlag` 为真 ⇒ **照"dirty 就跳过"的逻辑永远关不掉**。  
⇒ 建议 `read_only` 打开的文档记进会话的「只读清单」，  
收尾时**按清单强制关**（它们本来就不该被改）。

### 方法论

1. **同样的 op 序列 + 同样的会话 + 一个成功一个失败** ⇒ 先查  
   「**代码是怎么送进去的**」（内联 vs 文件），再查 SolidWorks。  
   我在前 6 步一直怀疑 SolidWorks，方向错了。
2. **"12 种组合全灭"不等于"不支持"** —— 先确认**前提是否满足**。  
   我的 12 种组合全灭是因为**几何不满足前提**，不是 API 不支持。  
   ★ **能力边界与前提不满足，要分开说**。
3. **诊断脚本的收尾也是判据的一部分**：没关干净的文档会污染下一轮，  
   而且症状（会话退化）**与真正的会话退化一模一样**。
4. **会话健康检查要有覆盖清单** —— `sw_session_check.py` 说 HEALTHY 8/8，  
   但它压根没测 mirror ⇒ "健康"这个词只对它测过的能力成立。

### ⑤ ✅ 正解：满足前提后，「逐个镜像 N 个特征」**完全可行**（真机 2026-10-03）

`_diag/diag_mirror_multi_ok.py`（板 x∈[-30,30] **横跨** Right 面 + 中平面拉伸）：

```
孔数   成功   镜像特征              体积倍率   理论倍率
 1     1/1    ['镜向1']              0.9786     1.0209（对不上？）
 2     2/2    ['镜向1','镜向2']      0.9563     1.0419
 3     3/3    ['镜向1','镜向2','镜向3'] 0.9330  1.0628
```

★ 体积倍率**逐个递减**（0.9786 / 0.9563 / 0.9330）⇒ 每镜像一个孔，体积就少  
一个孔的量。**「建一半 + 镜像」对多个特征反复执行是可行的** ——  
这正是箱座（`sw_segment` 判出 `shell` 89 面 + `web` 要建一半 + 镜像）的前提。

⚠ 「理论倍率」那列我写反了（镜像**减**体积，倍率应 < 1），  
但**实测那列是对的**。留个提醒：**自己算的期望值要先看方向对不对**，  
别因为"算错了但实测自洽"就以为判据没问题。

⇒ 新增探针 `assets/probes/mirror_half.json`（**19/19 真机通过**）：  
板 60×40×10 + 3 个 Ø8 孔 → `mirror` 一次 → **体积 22492.04 → 20984.07 mm³**  
（3 孔 → 6 孔，理论值逐位吻合）。判据用 `check_mass` 的  
**`expect_volume_mm3`**（新参数）。

### ⑥ ★★ 镜像类判据必须用**体积**，不能用质量

`check_mass` 新增 `expect_volume_mm3` + `vol_tol`：  
质量依赖材料密度（未赋材料时取 1000kg/m³）⇒ **跨机器不可移植**；  
体积是纯几何量。镜像失败时 `CreateMirrorFeature` 返回 Feature 对象、  
`err=None`、`GetErrorCode=0`（§96②）⇒ **只有体积能抓住**。

### ⑦ ★★ `sw_session_check.py` 补了 **mirror 能力项**（8/8 → 9/9）

★ 排查过程中它报 **HEALTHY 8/8**，但它**压根没测 mirror** ——  
「会话健康」这个词只对它测过的能力成立（§96⑤ 的另一个面）。  
新增项测**完整闭环**：建横跨镜像面的板 → 切孔 → 镜像 → **体积必须变小**。  
现在报 `能力：part / assembly / insert_component / **mirror**`，  
真机 **9/9 HEALTHY，`mirror=可用`**。

---

## 101. ★★★ 「建一半 + 镜像」端到端真机验证（CSWA 支架）

`#61` 的目标：证明「参照件 → 读数 → 建一半 → 镜像 → 对账」这条链**闭合**。  
`mirror_half.json` 探针只用合成几何证明「机制可用」，  
这一节用**真件的真实截面**重建整件并与参照件逐位对账。

### 参照件读数（`D:/out/cswa_1_2_final.SLDPRT`，真机）

```
面 14 个（全是平面）       体积 = 70540.1623 mm³
bbox = [-14.0, 0.0, -78.0, 14.0, 58.0, 0.0]     尺寸 [28, 58, 78]
对称面 = X = 0（对称度 100.00%）
  · 12 个侧壁面**全部** x∈[-14, +14]  ⇒ 横跨对称面 ⇒ **整片建，不能只建一半**
  · 只有两端盖板 Face10(x=+14) / Face11(x=−14) 分居两侧 ⇒ **互为镜像**
棱柱判定：沿 X，12 侧壁 / 2 盖板 / 长 28mm，**体积偏差 0.0000%**
截面（`sw_prism` 抽）：12 段闭合环，面积 2519.29 mm²，周长 268.07mm
```

### 结果

```
建一半后体积 = 35270.08 mm³（理论 2519.29 × 14 = 35270.06）
镜像后体积   = 70540.16 mm³（参照件 70540.1623，**差 0.0000%**）
sw_faceset --compare：
  [PASS] 体积            0.000%
  [PASS] 包围盒尺寸      [28,58,78] vs [28,58,78]（逐项 0.0000mm）
  [PASS] 面集签名        0 项差异
  ⇒ **形状一致**（中心偏移 [0,0,78] = 纯平移）
```

### ① ★★★ 草图画在**哪个基准面**决定朝向 —— 差一个轴而体积/尺寸集全对

第一版把截面画在 **Front** 面（法向 Z）⇒ 拉伸沿 +Z ⇒ 产物 bbox `[116, 78, 14]`，  
参照件是 `[28, 58, 78]` ⇒ **转置了一个轴**。

★★ **体积逐位吻合（0.000%）、三个尺寸的集合也一样，只有朝向不同** ——  
只看体积/尺寸**集合**完全发现不了，**只有 `check_bbox`（逐轴有序）能抓住**。  
这就是 `sw_prism.emit_plan` 里那条 `check_bbox` 自校验存在的理由。

### ② ★★★ 换基准面要**交换截面的 (u,v)**

`Right` 面（法向 X）上实测：**u ↔ Z、v ↔ Y**。  
截面 `u∈[0,58]`、`v∈[-78,0]` 直接画上去 ⇒ 落成 **Y=78、Z=58**（又反了）。  
⇒ 必须 `(u', v') = (v, u)` 交换。

★ 通用规则：**截面坐标系的 (u,v) 与目标基准面的草图坐标轴**不是同一对，  
而且映射**依基准面而变**（Right: **u→−Z**、v→±Y；Front: u→+X、v→+Y；Top: u→+X、v→−Z）。
★ 符号必须实测钉死（2026-10-04 探针：Front 凸台 + Right 面 u=70 凸台 ⇒ Z 跨度 85 ⇒ u→−Z）；
  旧注「u↔Z」没写符号，半联轴器外廓刀照 +Z 画 ⇒ 整条轮廓落进 Z 负半轴空气，切除四方案全灭。  
⇒ `sw_prism --emit-plan` 已经处理了这个（按 `AXIS_TO_PLANE` 映射），  
**手写计划时必须自己再核一遍**。

### ③ ★★ `check_bbox` 必须放在**拉伸之后**

第一版把它放在 `extrude_boss` **之前** ⇒ 报「读不到包围盒（零件可能是空的）」。  
★ 看着像 op 坏了，其实是**顺序**错：拉伸前确实没有实体。  
⇒ 判据 op 的位置也是计划的一部分，别默认"检查放最后"。

### ④ ★★★ 面集签名必须是**规范形**（canonical form）

这一条是本节最隐蔽的：**形状明明一致，签名却报差异**。

| 症状                                                                         | 根因                       |
| -------------------------------------------------------------------------- | ------------------------ |
| `t[8.0, 17.37, 16.725, 7.905, 8.0]` vs `t[8.0, 7.905, 16.725, 17.37, 8.0]` | 厚度列表**未排序** ⇒ 同一组站位、顺序不同 |
| `t[]` vs `t[0.0]`                                                          | 单站位（无厚度）**两种写法**         |
| `d@0` vs `t[0.0]`                                                          | 单站位在两处分支写了不同的 tag        |

⇒ 修法（`signature(relative=True)`）：① 厚度列表 `sorted({...})`；  
② 空列表统一写成 `t[0.0]`；③ 删掉 `d@0` 那个分支（同一件事不该有两种写法）。

★ **"同一形状"要求签名是规范形**：任何"集合相等但顺序/表示不同"的情况  
都会变成假失败。而假失败会让人**以为重建错了**，进而去改本来正确的代码。

### 方法论

1. ★★★ **判据要能区分「集合相等」与「序列相等」**。做对账/去重/比对时，  
   先问「这是**集合**语义还是**序列**语义」—— 用 `set` 就别忘了排序输出。
2. ★★ **"体积对 + 尺寸集合对"仍然可能是错的**：朝向（哪个尺寸在哪个轴上）  
   只有**逐轴有序**的判据能验。
3. ★★ **判据 op 的位置属于计划**：放在实体存在之前就会给出  
   「读不到」这种指向错误方向的报错。
4. ★ **对账工具自己要先在真件上跑通一次** —— 它的"假失败"会污染  
   上游所有结论（本节浪费了两轮才确认是签名的问题不是重建的问题）。

---

## 102. ★★★ 「先验压过图纸」—— 用人工正确件校准 agent 的判断（CSWA）

起因：用户指出「**CSWA 不是完全对称件**」，并给出他自己建的  
`D:/Desktop/零件CSWA.SLDPRT` 作参照。核查后**结论：完全成立**，  
而且暴露了一类比任何单点 bug 都严重的错误。

### ★★★ 真机对照（同一件 CSWA 支架，两个版本）

|               | **用户手建的**（正确）                                                              | **agent 建的**（错）       |
| ------------- | -------------------------------------------------------------------------- | --------------------- |
| 面数            | **20**（18 平面 + **2 圆柱**）                                                   | **14**（全平面）           |
| 体积            | 71835.1344 mm³                                                             | 70540.1623 mm³        |
| 尺寸            | **78 × 63.536 × 28**                                                       | 28 × 58 × 78          |
| 特征树           | `草图2` + `凸台-拉伸1` + **`凸台-拉伸2`(ICE)** + `草图4` + **`凸台-拉伸3`(ICE)** = **3 段** | 1 草图 + 1 拉伸 = **1 段** |
| `sw_symmetry` | **三方向全不对称**：70.46% / 60.04% / 58.82%                                       | **100.00%**「对称件」      |

★ **那两个圆柱面 = 2 个 Ø6 孔**，轴向 `(−0.42, −0.91, 0)`  
—— 就是 **65° 斜筋方向**的**法向孔**（孔轴与斜筋面法向同向），  
圆心 `(61.457, 62.268, 7.0)`，两个孔 z 分别是 4~~10 与 18~~24。  
⇒ **agent 版本一个孔都没有**（14 个面全是平面）。

★ **用户手建的按图纸分 3 段拉伸**（两次 `凸台-拉伸` 类型是 **ICE** = 凸台-基体，  
是在**已有实体上继续拉伸**）；agent 版本**一段拉伸**。  
⇒ 分段数不对 ⇒ 特征就丢。

### ① ★★★ 最严重的错误类型：**先验压过了图纸**

agent 的推理链是：

```
"多面体零件" → "sw_faceset 判它是棱柱" → "sw_symmetry 判它 100% 对称"
  → "那就建一半 + mirror"（用户上一轮的建议）→ 重建出一个"对称的"件
```

**整条链的每一环都"成功"，而结果是错的** ——  
因为"对称"这个先验是从**用户的一句话**（"对称件可以建一半再镜像"）来的，  
不是从**图纸**读出来的。

★★ **判据全绿 ≠ 结论正确**：`sw_symmetry` 报 100% 是**如实**的  
（那个件确实 100% 对称）—— 错的是**那个件本身不该长成那样**。  
⇒ 校验器只保证"我算的和模型里的一致"，**不保证"模型和图纸一致"**。  
这与 §84「判据在验证别人」的另一面：**判据链首端（图纸 → 参数）没有判据**。

⇒ **修法方向（还没做）**：**图纸/参照件才是唯一的真值来源**。  
「建一半 + 镜像」这类**策略**必须在**已经确认对称之后**才能用；  
而"是否对称"要**从图纸标注读**（对称符号、中心线、"均布"等），  
**不能从既有模型反推** —— 既有模型可能是上一次错的产物。

### ② ★★ 尺寸也错了：58 vs 63.5（差 5.5mm），而且**轴序也反了**

用户 78×63.536×28 vs agent 28×58×78：  
不只是"某维差 5.5mm"，**整组三个尺寸的对应关系都不一样**  
（agent 把 78 放在 X、58 放 Y、28 放 Z；正确的是 78/63.5/28）。  
⇒ 与 §101① 同一个陷阱：**体积逐位对、尺寸集合对，仍然可能是错的**，  
只有**逐轴有序**的判据能验。  
★ 这次的错误源头是**参照件本身建错** ⇒ 上游错了，下游全对也没用。

### ③ ★★ 公称尺寸 / 图纸特征的识别：本次暴露的缺口

用户原话：「看来还是**公称尺寸的识别和图纸特征判定**的问题」。

本项目目前**没有**的���力：

- **Ø6 孔**：轴向是 65° 斜筋的法向 ⇒ 只有"轴向匹配某个平面族法向"  
  才能把孔归到那个筋上。`sw_faceset` 已经能读出平面族法向 `(0.9063, −0.4226, 0)`  
  与孔轴 `(−0.42, −0.91, 0)`（夹角 ≈ 0°）⇒ **数据齐了，缺的是「匹配判据」**。
- **公称尺寸 → 实际尺寸**：Ø6 是**公称**，实体上可能是 Ø6 孔（间隙孔）  
  或 Ø5.5（基孔）—— 没有图纸标注就**分不出来**。这类必须能查标准表。
- **图纸特征判定**：哪两个面之间是"孔"、哪条棱是"筋"、  
  哪块平面是"分型/贴合面" —— 现在全靠面积/法向猜。

### ④ ★★ 又一个"接口静默失败"

量化每个特征贡献时用了「逐个抑制 → 读体积」，  
结果 **`SuppressFeature` 对这 5 个特征全部返回失败**（体积纹丝不动）。  
★ 特征树里明明有 `草图2`/`凸台-拉伸1`…，名字也拿得到，  
但**抑制接口不响应** —— 又一个"不抛异常、不报错、只是没用"的例子。  
⇒ 与 §100⑤ 同类。**判据的失效方式往往是"静默无效"而不是"报错"。**

### 方法论（本节最重要的一条）

★ **「策略建议」不能变成「先验」**。  
用户说"对称件可以建一半再镜像"是对的**策略**；  
但**先要有一件"经过图纸核对"的模型**，才知道它是不是对称件。  
⇒ 正确顺序：**图纸 → 参数 → 模型 → 再用策略优化**。  
我这次把顺序倒过来了：用策略去**推断**几何。

★ **参照件要选对**：`cswa_1_2_final.SLDPRT` 是 agent 早前自己建的  
（00:01），拿它当"参照件"再重建 = **自己抄自己**，  
错误会被完整继承并放大。  
⇒ **参照件必须来自可信来源**（用户手建 / 图纸 / 标准件），  
且用前先核对（面数、体积、是否有预期特征）。

---

## 103. ★★★ 「孔 vs 凸台 vs 槽」—— 圆柱面必须读**凹凸性**，光看轴向一定错

起因：2026-10-03 用户校准（附截图标注）：

> 「很明显这里红色是向上拉伸的突起吧？根本不是孔，绿色的也是槽啊，  
> 说明现在的判定还有问题。」

**结论：完全成立。** 上一轮（§102）我加的 `holes_with_normal_holes()`  
只用了「孔轴 ⟂ 承载面」这一条判据 ⇒ 把 CSWA 上**2 个 Ø6 凸台**  
报成了「法向孔」。

### ① ★★★ 根因：圆柱面**本身**分不出孔与凸台

|          | 孔       | 凸台               |
| -------- | ------- | ---------------- |
| 面类型      | 圆柱面     | 圆柱面（**一样**）      |
| 轴与基面     | 垂直      | 垂直（**一样**）       |
| 直径       | Ø6      | Ø6（**可能一样**）     |
| **材料在哪** | 圆柱**外** | 圆柱**内** ← ★ 唯一区别 |

⇒ **「轴 ⊥ 基面」这条判据对凸台同样成立** ⇒ 它根本没有区分力。  
★ 这是**判据选错层级**的典型：轴向是**姿态**，凹凸是**拓扑**。

### ② ★★ 正解：曲率判据 `n·r`（真机 2026-10-03 实测）

取**真正的面法线** n（`ISurface.Evaluate` + `IFace2.FaceInSurfaceSense`  
修正朝向），与**径向** r（面上点相对圆柱轴的横向分量）点积：

```
n·r < 0  ⇒  法线指向轴心  ⇒  材料在外  ⇒  孔（凹）
n·r > 0  ⇒  法线背离轴心  ⇒  材料在内  ⇒  凸台（凸）
```

★ 官方依据（`sw_apisig --sig IFace2.FaceInSurfaceSense`）：  
"the normal vector of faces are directed **away from the body material**"  
⇒ 面法线总是背离材料，所以**必须靠曲率**才能反推材料在哪一侧。

**CSWA 实测**（用户在截图里标红的两个）：

```
Face17  Ø6.000  长7.9736  n·r = +1.0000  ⇒ 凸台
Face19  Ø6.000  长7.9736  n·r = +1.0000  ⇒ 凸台
```

**四个真件交叉验证**（判据不是只会说"凸台"）：

| 件              | 圆柱面 | 孔      | 凸台    | 判读是否合理                            |
| -------------- | --- | ------ | ----- | --------------------------------- |
| `零件CSWA`（用户手建） | 2   | 0      | **2** | ✔ 截图里就是两个向上拉伸的突起                  |
| `1-箱座`         | 88  | **68** | 20    | ✔ 孔=轴承座孔 Ø70/Ø55/Ø47；凸台=Ø4 油标孔外侧  |
| `6-小盖`         | 8   | 7      | **1** | ✔ **同一个件里** Ø34 外圆=凸台、Ø14/Ø4/Ø6=孔 |
| `8-窥视孔盖`       | 5   | **5**  | 0     | ✔ Ø11 中心孔 + 4 个 Ø3.5 螺孔           |

★ `6-小盖` 那行最有说服力：**同一件里**外圆判成凸台、内孔判成孔  
⇒ 曲率判据真的在区分材料内外，不是碰巧。

### ③ ★★★ 槽（slot）：第一版判据报了 **16 个假阳性**

第一版：「**任意两片**近似平行的细长平面，间距 0.5~60mm」。  
真机 CSWA 上直接报 **16 个**「槽」。  
★ 根因：**L 形箱体的外轮廓天然有几十对平行面**（相对的两块大平板、  
台阶的两级面…）⇒ 「全局两两配对」这个判据**松到没有区分力**。

⇒ **教训：判据必须用「局部、成组」的证据，不能用「全局两两配对」。**

**正解（真机读数反推）**：槽 = **同一个平面族内**，**站位相近**  
（间距 = 槽深）、**面积几乎相等**、**包围盒几乎重合**的**两片平行面**。

真机 CSWA 的斜筋族（法向 `(0.9063, −0.4226, 0)`）：

```
面      站位mm    bbox 跨度mm      面积mm2
Face6   41.883    15.21 × 32.63    1008.0    ← 槽壁
Face8   36.883    15.21 × 32.63    1008.0    ← 槽壁
⇒ 站位差 = 5.000mm = 槽深（用户截图绿框那片）
Face10  21.883    24.24 × 52.00    1090.1    ← 斜筋**外表面**，无配对 ⇒ 不是槽
```

★ `build_families` 其实**早就算出了** `thicknesses_mm = [15.0, 5.0]` ——  
**5.0 就是槽深**。数据一直在那儿，是判据没去用。

三条判据的分工（每条排掉一类假阳性）：

| 判据                            | 排掉什么              |
| ----------------------------- | ----------------- |
| ① **同族**（法向一致）                | 不同朝向的平行面          |
| ② **面积几乎相等**（≤2%）             | 相对的两块大平板（面积常差很多）  |
| ③ **包围盒几乎重合**（≤5%）            | 台阶的两级面（bbox 大小不同） |
| ④ **站位差 = 槽深**，0.3~40mm 且 > 0 | 同一个面被重复记录         |

⇒ 真机从 **16 个假阳性 → 1 个真槽**（与截图人工判读一致）。

### ④ ★★ 「平面的凹凸」判不出来 —— 别硬套

`face_convexity` 对平面返回 `flat` 并说明「平面判不出凹凸，槽要靠  
`classify_slots`」。一个平面的法线方向**既可能是外表面也可能是槽壁**，  
**必须靠拓扑**（有没有配对面）。⇒ 不硬判。

### ⑤ ★★★ 三个 API 陷阱（都踩过）

1. **`IsCylinder` 是 `ISurface` 上的属性**（用 `getattr`），  
   **不是 `IFace2` 上的方法** ⇒ 写成 `member(face,"IsCylinder")` 全 false，  
   一度让我以为「这个件没有圆柱面」。
2. **`ISurface.Evaluate(u,v,0,0)` 返回**扁平数组 `[px,py,pz, nx,ny,nz]`  
   （0 阶导时长度 6）⇒ **不能**按 `[[x,y,z],[nx,ny,nz]]` 解构。
3. ★★ **`IFace2.GetName()` 对未命名面返回 `None`**，而 `read_faces` 的  
   `Face1..N` 是**按枚举序号**起的名 ⇒ **两套命名完全不同** ⇒  
   任何「按名字反查面」的实现都会**永远找不到**。  
   ⇒ 正解：**`read_faces` 直接把 `IFace2`/`ISurface` 挂在 rec 上**  
   （`rec["_face2"]` / `rec["_surf"]`），从此只认这份引用。  
   ⇒ 同源的一条：`build_families` 的 `fam["faces"]` 存的是  
   **面名字符串**（不是面对象）⇒ `fam["faces"][0].get(...)` 直接  
   `AttributeError`。**先看数据结构再写代码。**

### ⑥ ★★ 夹具必须**复制真机读数**，不要自己造

槽的自检夹具我第一版自己造 bbox，把两片在 X/Y 上错开  
⇒ `_same_shape` 判「包围盒不重合」⇒ **连槽都认不出来**。  
改成照真机绝对读数（`Face6`/`Face8` 的真实 bbox）后一次通过。

★ 断言失败时**先分清是「夹具错了」还是「判据错了」** ——  
本轮至少 3 次是夹具问题（键槽、槽深、bbox 错位），  
却每次都先怀疑判据，白绕了好几轮。

### 方法论

1. ★★★ **判据要选对「层级」**：姿态（轴向、法向）分不出的时候，  
   要往上一层找**拓扑**（材料在内还是外）。
2. ★★★ **「两两配对」是最容易写出假阳性的判据形状** ——  
   先问「这个件天然会有多少对符合条件的东西？」。
3. ★★ **交叉验证要挑「同一件里正反都有」的样本**：  
   `6-小盖`（外圆=凸台 + 内孔=孔）比 `窥视孔盖`（全是孔）有说服力得多。
4. ★★ **报「没找到」必须说「不等于没有」**：槽的判据要求两片壁都完整，  
   被圆角/倒角打断的槽会漏判。

---

## 104. ★★★ 外部真值从哪来 —— 训练材料里的 `Volume` 补上了判据链首端

起因：用户给出训练材料  
`C:/Users/OMEN/Downloads/d/PracticeProblems_ALL`（**18 级 / 344 张 PDF / 32 个 zip**），  
目标是打通「读图 → 提取标注 → 定建模顺序与基准面」。

### ① ★★★ 环境：能力早就装好了，只是 **`APPDATA` 没指对**

★ 本轮最大的弯路：先认定「本机没有 PDF 库」并花了不少时间手工切 PDF 流。  
真相是：

```bash
export APPDATA="C:/Users/OMEN/AppData/Roaming"
/c/Python313/python.exe -c "import pymupdf, rapidocr_onnxruntime; print('OK')"
```

`pymupdf 1.27.2.2` / `rapidocr_onnxruntime 1.2.3` / `onnxruntime` / `PIL` /  
`numpy` / `cv2` / `pypdf` **全都装在用户级 site-packages**，只是**不在默认  
`sys.path` 上**。⇒ 与项目记忆里那条 COM 脚本的坑**同源**：  
**`APPDATA` 指向错了 ⇒ 所有 import 报"没装"。**

★★ 教训：**「库没装」这个判断要先排除「路径没指对」**，  
否则会去手工实现一遍别人已经写好的东西（我手工解 ASCII85/Flate 纯浪费）。

### ② ★★★ 训练材料的 PDF 里**不只有图纸，还有题目说明**（决定性发现）

```
Practice Level (Level 10)
Units: MMGS        Difficulty Level: Easy     Certification: CSWA
Duration (min): 10
Design Intent:
  o The part's origin location is indicated in red.
  o The part is symmetric.
  o All sketches are fully defined.
Suggested Sketch Tools / Features
  Sketch: Lines, Centerlines, Circles, Slots
  Features: Extrude Boss, Extrude Cut, Fillets and Chamfers
  Reference Geometry: Planes
HINT: Use a Slot for the center cut.
Volume: 202594 mm3          ← ★★★
```

⇒ **`Volume` 是每道**零件**题的精确目标体积（mm³）**。  
这正是 §102 那个缺口：**此前 agent 的所有判据只保证「我算的和模型里一致」，  
没有任何外部真值**；拿自己早前建的件当参照件 = 自己抄自己。  
⇒ **344 份里 297 份带精确目标体积** —— 判据链首端第一次有了外部答案。

⇒ `Design Intent` 是**人写的建模意图**（对称 / 草图全定义 / 原点位置），  
等于「这道题该怎么做」的权威提示。

### ③ ★★ 必须分清「文本层」与「图纸本体」

|          | 是什么                                                | 怎么读                   |
| -------- | -------------------------------------------------- | --------------------- |
| 文本层      | **题目说明**（Design Intent / Features / Volume）        | `page.get_text()` 精确  |
| **图纸本体** | **位图**（`get_drawings()` 只有 3 个、`get_images()` 6 个） | ★ 尺寸数字烧在图里，**必须 OCR** |

⇒ **两个工具，各管一半，不要指望一个全干**：  
`sw_exercise.py`（题面元信息）+ `yoyo.ocr` / RapidOCR（尺寸标注）。

### ④ ★★★ 三类题要用**不同的真值**，不能一律要体积

| 类型           | 等级                 | 真实可用/总数                                                  | 真值形式                                |
| ------------ | ------------------ | -------------------------------------------------------- | ----------------------------------- |
| **零件**       | 01-08 / 10-14 / 18 | Level04 **70/70**、18 **33/33**、10 **19/19**、02 **20/20** | **`target_volume_mm3`**             |
| **装配体**      | 09                 | **0/16**                                                 | 组件数 / 配合数（题面是 "Insert … at Origin"） |
| **配置 / 方程式** | 15 / 16 / 17       | **0/13** / 1/6 / 2/7                                     | 配置数 / 方程解                           |

★ 这个分布本身就是「`Volume` 可靠」的证据：**零件类几乎 100% 有**，  
装配体/配置类**系统性没有** ⇒ **「没读到 Volume」不是提取失败，  
而是这类题本来就没有单件体积**。  
⇒ 别把它一律当 bug（§99③ 的老教训）。

### ⑤ ★★ 判据集必须带 `cannot_check`（本工具的重点）

```
== PracticeProblems_10_1_ENG.pdf（Level 10，难度 Easy，10 分钟）==
可自动核对的判据：
   ✔ 体积 == 202594 mm³（相对容差 0.20%）
   ✔ 包围盒**逐轴有序**对（只看尺寸集合会被转置骗过，§101①）
   ✔ 对称性：题面说 symmetric ⇒ 用 sw_symmetry 复核（★ 别反过来当先验，§102）
   ✔ 建议特征：Extrude Boss, Extrude Cut, Fillets and Chamfers
   ✔ 题面 HINT：Use a Slot for the center cut.
★ 验不了的（别当成「已验」）：
   ✘ ★ **体积是标量**：「少建一个孔 + 多建一块料」也能对上 ⇒ 体积通过≠特征对
   ✘ ★ **视图布局读不到** ⇒ 无法确定草图画在哪个基准面 ⇒ 朝向仍可能错
```

★★ **「只验了体积」被当成「全验了」，是比「没验」更危险的失败模式。**  
体积是**标量**：少建一个孔 + 多建一块料可以对上。  
⇒ `expect`（能验的）与 `cannot_check`（验不了的）**必须一起给**。

### ⑥ ★★ 视图布局**只能从图纸位图读**（实测 0/120）

全库搜 `view / isometric / front / top / right`：  
文本层里只有 7 处零散出现（`Front Plane` 之类），**原文含 `view` 的 0/120 份**。  
⇒ **视图信息只存在于图纸位图里** ⇒ 要它**必须 OCR**，没有别的路。  
★ 这也是「agent 看得懂图纸」这个难点的一个**具体定位**：  
缺的正是「**哪张视图是基准、尺寸属于哪个视图**」这条信息链。

### ⑦ ★★★ 三个解析陷阱（都踩了）

1. **`doc[0]` 会抛 `IndexError: page 0 not in document`**  
   —— `Level11_HoleWizard/PracticeProblems_11_8_ENG.pdf` 是 **0 页坏文件**  
   ⇒ **必须先判 `page_count == 0`**，否则**一个坏文件会打崩整批**  
   （我这版第一轮就因为 `try/except` 跳过它而让 `Volume` 统计全为 0，  
   差点误判成"提取全部失败"）。
2. ★★ **heredoc 会把正则里的 `\s` `\d` 吃掉一层**（变 `/s` `/d`）  
   ⇒ 同一个正则在单文件测试里中、批量里全不中，我一度以为数据有问题  
   ⇒ **正则一律写成模块级 `re.compile(r"...")` 常量，并落成 `.py` 文件**  
   （项目记忆里早有这条，本轮又犯了一次）。  
   ⇒ ★★ **更根本的修法：正则不要复制到第二处**。`sw_doctor` 里我自己  
   重写了一遍 `Volume` 的正则 ⇒ 又中一次招 ⇒ 体检报「读到目标体积 0」。  
   **正解：`from sw_exercise import RE_VOLUME`（单一来源）**。  
   「同一个正则写在两个文件里」本身就是缺陷 —— 改一处忘一处必然走样。  
   ⇒ ★★★ 顺带一个**更坏**的错法：改用 POSIX 字符类  
   `[[:space:]]` 企图绕开反斜杠。**Python 的 `re` 不支持它**  
   （那是别的正则引擎的语法）⇒ 它被当成**嵌套集合**  
   ⇒ 抛 `FutureWarning: Possible nested set` 且**永远不匹配**  
   ⇒ 照样报「读到目标体积 0」，而且更难查（有警告但极易被忽略）。  
   ⇒ 试没试对，看有没有 `FutureWarning`；有就是语法不被支持。
3. **条目会被硬换行切断**：
   ```
   o The part's origin location is indicated
   in red.                    ← 续行，没有 `o` 前缀
   Features: Extrude Boss, Extrude Cut, Fillets
   and Chamfers               ← 续行
   ```
   ⇒ 规则：**没有条目前缀的行 = 上一条的续行**。  
   第一版没做 ⇒ `"…is indicated"` 和 `"in red"` 成了两条 ——  
   ★ **看起来像识别错了，其实是切分错。**

### 8. `level` 正则别去匹配 `"Practice Problems"`

真文本是 **`Exam Level (Level 10)`**；而 `Practice Problems` 这个词  
被 PDF 的**版权行截断**成 `Practice Prob`（撞在 "incorporated." 后面）。  
⇒ 匹配 `Exam\s+Level\s*\(([^)]*)\)`。

### 方法论

1. ★★★ **「我需要的外部真值在哪」要先去找，不要先假设它不存在** ——  
   本轮差点因为"PDF 读不了"而放弃，其实真值一直印在题面上。
2. ★★★ **能力缺失的三种形态要分清**：真没装 / 装了但路径没指对 / 装了但接口不对。  
   **先排第二种**，否则会去手工重造。
3. ★★★ **判据集要显式列出「验不了什么」**。否则「只验了体积」会被当成  
   「全验了」—— 那比「没验」更危险。
4. ★★ **「某字段没有」要先看它是不是**系统性地没有\*\*（装配体/配置类  
   本来就没有单件体积）⇒ 分布规律本身就是判别依据。

---

## 105. ★★★ 「尺寸 → 视图」—— 归属判据踩了**三版**才对（第一跳）

起因：接上训练材料后，「让 agent 看得懂图纸」被定位到
**尺寸 → 哪个视图 → 哪个基准面** 这条信息链（§104⑥ 实测题面文本层里
原文含 `view` 的是 **0/120** 份 ⇒ 视图信息只在**位图**里）。
第一张验证图选 `Level01/1_1`（最简单、尺寸少、三个视图齐）。

### 真机基准（对着图人工数出来的，判据只能对这个）
```
Top View（右上）  5 个：20 12 12 20 28
Front View（左下）8 个：75 20 30 12 16 ⌀12 16 60
Isometric（右侧）0 个  ← ★ 一个尺寸都没有
```

### ① 视图必须先「切区域」，再「挂尺寸」—— 顺序不能反

我**先做尺寸空间聚类**，把网格从 ×1 调到 ×8 才勉强凑出 `[3,1,2,5]`
（真值 Top 5 / Front 8）⇒ **调不出来**。
看叠加图才明白：**「空白带」不是可靠依据** ——
Front 自己的 `12` 与 `75` 之间就有 **240px 空白**。

⇒ 正解：**视图 = 彩色填充的连通域**。
工程图里**视图主体有填充色**（棕/蓝/灰），而**尺寸线 / 轮廓线 / 文字**
是纯黑或白底 ⇒ 饱和度阈值一刀切开。
实测 `Level01/1_1` 检出 **3/3** 命中（Top/Front/Iso）。

★ `SAT_MIN=28`（`max(rgb)-min(rgb)`）+ 连通域 + **合并间隙 26px**
（一个视图常被黑色轮廓线切成好几块）。

### ② ★★★★ 归属判据错了**三版**，每一版的错法都不一样

| 版 | 判据 | 真机为什么错 |
|---|---|---|
| 1 | **最近视图** | Top bbox 右界 x=732，它的 `28`(x=884) 到 Top **152px**、到 Iso **148px** ⇒ 148 vs 152 **几乎一样** |
| 2 | **单轴 y 区间** | Top y∈[101,423]，**Iso y∈[101,990] 把 Top 整个包住** ⇒ `20/28` 同时命中，第一个命中是 Iso |
| 3 | **双轴距离最小** | 同上，Iso 仍 109 < Top 111 |
| **4** | ★ **先分组，组内归属** | ✔ Iso 独立成分组，`28` **不可能**归它 |

★★ **根因不是「距离算错」，而是「跨组比较」本身没有意义**：
**轴测图不是正投影视图**，它没有「上下对齐」关系，
它的 y 区间会横跨整张图 ⇒ 与它算距离永远能被它赢。

⇒ 正解 `group_views()`：**按「跨轴区间是否重叠」把视图分组**
（Top 与 Front 的 x 区间**完全相同** [221,733] ⇒ 同组；Iso x∈[1033,1550] ⇒ 独立），
**归属只在组内进行**。

★★ 顺带一个反直觉的教训：分组判据**只加「跨轴重叠」这一条**就够，
**不要**再要求「沿布局轴的跨度相近」—— 我第一版加了，全错：
真机 Top y 跨度 242、Front 638（**差 396**）⇒ 被拆成两组，
而它们**本来就该同组**（三视图的宽可以差很多，**x 才是对齐基准**）。

### ③ ★★★ 单遍 OCR 的结果**不可复现** ⇒ 必须多遍投票

同一张 `Level01/1_1`，四个放大倍数：
```
zoom=4 → 12 个尺寸（漏 75）        zoom=5 → 11 个：**全对**
zoom=6 → 10 个（漏一个 20）        zoom=8 → 把 16 误读成 -12
```
⇒ ★ **没有任何单一倍数可靠** ⇒ **一次 OCR 的结果不能当事实**。
★ 与 §100⑤「判据失效常是静默无效」同源：**OCR 漏读不会报错**。

⇒ `multi_pass_dims()`：多 zoom 渲染 → **归一化坐标**下按位置聚类
（容差 0.012）→ 只保留 **≥2 遍一致**的值；**单票的单独列出（不静默丢掉）**。
真机：**33 票 / 13 簇 ⇒ 采纳 11**（= Top 5 + Front 6，**与人工读数一致**），
2 个单票被显式标为可疑。

### ④ ★★ `⌀` 会丢 ⇒ 不靠 OCR 补，靠几何还原

真机 OCR 读出 `⌀12` 的 **`⌀` 被引线划过切掉了** ⇒ 只剩 `12`。
⇒ **少了 `⌀`，agent 会把「孔」建成「方槽」—— 形状直接错**。
这是"读不懂图纸"最贵的后果。

文本层能还原的（`normalize_dim`，可离线单测）：
| OCR 文本 | 还原 | 判据 |
|---|---|---|
| `-20-` | `20` linear | **尾部也有 `-`** ⇒ 尺寸线污染，剥掉 |
| `-0.05` | **`-0.05`** 保留符号 | **只有前导 `-`** ⇒ 真公差，**不能剥**（剥了符号就反了） |
| `⌀12` `Ø12` `ø8` | `diameter` | 前缀符号 |
| `R5` | `radius` | 前缀符号 |
| `2×⌀6` | `diameter` + `count=2` | 阵列特征 |
| `Front View` `CSWA` | **不是尺寸** | 含 ≥2 个连续字母 |

★ 第一版把 `-0.05` 也剥成 `0.05` ⇒ **断言写成 `== 0.05` 反而"通过"了**
  —— ★ **断言自己错，害我以为判据坏了**。期望值应该是 `-0.05`。
  ⇒ 与 §104⑥ 的教训同源：**断言写错会掩盖真问题**。

⚠ **`⌀` 的几何兜底还没做**（引线检测）—— 工具现在会报
`kind='linear'` 而**不是**假装认出了直径 ⇒ 上层必须知道这是「符号未知」。

### ⑤ ★★ 视图检出必须**独立于尺寸**

Isometric **一个尺寸都没有** ⇒ 靠「有尺寸」发现视图会**静默漏掉轴测**，
而轴测恰恰是判别 3D 形状的关键（各视图给尺寸、**轴测给形状**）。
⇒ 工具**显式统计**「无尺寸的视图数」并**保留它们**。

### ⑥ ★ 还没做：视图**身份**确认

工具只给**位置候选名**（`top`/`front`/`right`/`iso`），并显式声明
「身份要靠尺寸语义 + 投影对齐确认，**没做**（不假装能做）」。
★ 这是刻意留的坑：把「Top 的尺寸是长×宽、Front 的含高度与孔」
这类**语义判据**补上，才是下一跳。

### 方法论

1. ★★★ **「分组后再比较」是一类通用判据**：
   先按**结构**（对齐关系）分组，再在组内做**相似度**比较。
   跨组比较往往没有意义（轴测图 vs 正投影视图）。
2. ★★★ **单次测量的结果不能当事实**（OCR/采样/随机过程都算）。
   多遍 + 投票 + **把单票显式列出**，比"调参数到看起来对"可靠得多。
3. ★★★ **调参数调不出来的时候，是判据选错了，不是参数没调好**。
   我在网格 ×1~×8 上白调，而正解是**换判据**（分组）。
4. ★★ **断言写错会掩盖真问题**：`-0.05` 断言写成 `== 0.05` 时，
   一个「剥符号」的真 bug 被当成通过。

---

## 106. ★★★ 「基准样本选错」—— 判据在 1/6 的题上有效，我拿它当标准

§105 做出「尺寸 → 视图」之后，我拿 `Level01/1_1` 当**唯一基准**，
判据全对（Top 5 / Front 7，与人工读数一致），于是以为这关过了。
**批量跑 6 张题时才发现：5 张直接崩、检出数全是 1。**

### ① ★★★ 根因：**视图填充不一定是彩色的**

| 题 | 彩色占比 `sat>28` | **灰阶填充占比** | 性质 |
|---|---|---|---|
| **`1_1`**（我拿它当基准的） | **25.84%** | 4.56% | **彩色填充** |
| `1_2` | **0.11%** | **19.78%** | **灰色填充** |
| `1_3` | 0.19% | 17.88% | 灰色填充 |
| `1_5` | 0.11% | 14.84% | 灰色填充 |
| `1_8` | 0.25% | 33.19% | 灰色填充 |
| `1_12` | 0.12% | 28.81% | 灰色填充 |

⇒ ★★ **只有 1/6 是彩色填充**。我拿它当基准 ⇒ 判据**只在 1/20 的题上有效**。
⇒ 「彩色」不是视图的**本质特征**，「**有填充**」才是。
⇒ 正解：**双通道** —— 先试彩色（更干净），面积占比不足则**退回灰阶**，
  并在 `views_raw` 里带 `mode` 字段、**报告显式说走的是哪条通道**。

### ② ★★★ 批量跑才暴露的**崩溃**

`assign_dims_to_views` 里 `max(groups, key=len)` 在 `groups == []` 时抛
`ValueError: max() iterable argument is empty` ⇒ **5/6 张题直接崩**。
★ 第一版**只在一张图上验过**，所以完全没发现。
⇒ 修：没有视图区域时**全部尺寸报「未归属」**（`nearest_view=None`），
  **不硬挂、不崩**。

### ③ ★★ 合并间隙**必须是图幅的比例**，不是绝对像素

第一版 `merge_gap = 26`（px）。真机 `Level01/1_2`：
```
zoom2: gap=0/10 ⇒ 4 个区域 ✔      gap=26 ⇒ 连成 1 个 ✘
```
⇒ 26px 恰好跨过了视图间的空白带。
⇒ 改 `MERGE_GAP_FRAC = 0.006`（按图幅算）。
★★ 但**改成比例也没解决** —— 见 ④。

### ④ ⚠ 未解决：**水印把视图连成一片**（已知局限，已在报告里显式提示）

`Level01/1_2` 的图纸上有淡灰色大字水印 `SOLIDWORKS`。
zoom4 实测**连 `gap=1` 都合并成 1 个区域** ⇒ 水印横跨全图，
把 4 个视图连成一片。
⇒ 待做：① 按**连通域的暗度方差**区分「实心填充」与「水印」
  （水印是**渐变**的、实心填充是**均匀**的）；
  ② 或先做**水印检测**（超大 bbox + 极低对比）再排除。
⇒ **当前做法**：`mode='gray'` 时报告打
  「⚠ 走的是灰阶通道 —— 注意水印可能被算进视图」，
  并且「只检出 1 个视图却读到 N 个尺寸」时也打警告
  ⇒ ★ **不静默**，让读的人知道结论可信度（§99③ 的老教训）。

### ⑤ ★★ 那张线框图还暴露了一个新事实：**剖视图**

`Level01/1_2` 有 **4 个视图**：左上轴测、右上 **`SECTION A-A`（剖视，带视图名）**、
左下 Front、右下**带 `65°` 斜线的剖面轮廓**。
⇒ ★ **剖视图带名字**，而普通三视图**不带**（实测题面文本层里 `view` 0/120）
⇒ 下一跳「视图身份确认」时，**剖视图是唯一有名字线索的** ⇒ 可作锚点。

### 方法论（本节最值钱的一条）

1. ★★★ **基准样本必须问「它代表多少」**。
   我拿第一张就当标准，**没看第二张长什么样**。
   ⇒ 拿到样本先**至少看 3~5 张**，统计特征的**分布**，
   **别用「这一张有」当「这类都有」**。
2. ★★★ **判据要在多个样本上验，否则等于没验**。
   单样本全绿 ≠ 判据成立；**批量跑一遍的成本极低，收益极高**
   （5 分钟就抓到「只在 1/6 上有效」+「5/6 直接崩」）。
3. ★★ **阈值类参数要问「它的量纲是什么」**：`26px` 是量纲错误，
   同一批图尺寸差 2 倍就会跨过边界 ⇒ 阈值应该**相对图幅**。
4. ★★ **崩溃也是判据缺陷**。「只在 1 张图上跑过」的代码必然藏着
   `max([])` 这类边界崩溃 —— 它们平时不发生，一批量就现形。

## §107 训练题 10_1 端到端 + 用户方法论「视图分割→比例尺→等比还原」(2026-10-03)

### ① ★★★ `arc3p` 的 `p3` 是「弧上一点」，不是「弧深/矢高」
「与 z=25 的边相切、弧深 h、R25 的弓形」: 弦端 x=±sqrt(R²-(R-h)²),
**`p3 = (0, 25-h)`** —— 不是 `(0, h)`！
- 真机连错两次：v3 写 `(0,12.5)`+弦端 21.93（弧深还是 12.5，只是弦长了）；
  v4 写 `(0,13)` ⇒ **弧深变成了 12**（体积 205990 比 v2 还大，才暴露）。
- 为什么静默：三点定圆**总能**算出一个圆，草图合法、特征成功、无任何报错，
  只有 `check_mass` 的体积对不上才知道 —— **体积判据是唯一能抓住它的闸门**。
- ★ 正确自查式：`p3_z = 边界z - 弧深`；圆心 z = 边界z + R（外凸弓形）。
- v2 的 `p3=(0,12.5)` 碰巧对（25-12.5=12.5 且 R=25 自洽），**巧合绿灯更危险**。

### ② ★★★ 计划缺「基体拉伸」= 空零件上切除，4 方案全灭
`new_part` 后直接 `extrude_cut`：FeatureCut4 返回 None ×4（盲孔/贯穿/反向/对称全试遍）。
报错只暗示「草图/方向/平面」，**没说"根本没有实体"**。
⇒ 写计划的肌肉记忆：**第一步永远是 boss/revolve 出基体，再谈切除**。

### ③ ★★★ 「比例 1:1」的标题栏是**陷阱**：每个视图必须独立标定
半联轴器教学图（用户截图）实测：
- 端面视图 Ø50 外圆 = 235px ⇒ 0.2128 mm/px
- 左下剖视 78 长 = 308px ⇒ **0.2532 mm/px**（差 19%！）
⇒ 跨视图继承比例尺必错。工具 `sw_viewtrace.py` 的 `--calib` 就是**按视图给**的。
- 标定优先级：圆直径（Hough/minEnclosingCircle）> 长直线段 > bbox
  （bbox 会混入标注引线，实测端面 bbox 高 264px > 直径 235px，混了 10JS9 引线）。

### ④ 新工具 `scripts/sw_viewtrace.py`（用户方法论的落地）
流程：`--crop` 先裁视图 → `--dehatch`（HoughLinesP 剔 45° 剖面线，候选 49→28）
→ `--thin-filter`（开运算去细线：尺寸线~1px vs 粗实线~2.5px，5 个候选）
→ `--hough` 圆检测 / `--calib`+`--measure` 量点对 → `--pick` 出轮廓 mm 点列。
- ★ **剖面线（hatch）是剖视图轮廓提取的天敌**：findContours 会把
  轮廓+剖面线+尺寸线连成一片大杂烩（#0 area 占满全图）。先去 hatch 再提轮廓。
- ★ 定位是「给 agent 当眼睛」：工具出**候选叠加图**（编号+颜色），agent 看图
  选 `--pick`；工具出 mm 换算，agent 核对标注。不追求全自动一键。
- 残留：Hough 圆在细线圆上中心漂移（检不出 r=117 外圆）⇒ 圆标定先用
  「bbox 宽（凸圆左右极点）」或 agent 指点。

### ⑤ 10_1「锤头」件的体积对账进展（真机）
- v2（弧深 12.5, R25 相切, 槽 36x16x12.5, R3 四棱）: 真机 **204713.94**
  与解析 204713.8 **逐位吻合** ⇒ 建模链路完全正确。
- 距官方 202594 差 2120 (1.05%) ⇒ dV/dh 反推弧深 = 12.985 ≈ 13；
  v5（h=13, p3=(0,±12)）解析预期 202530.5（残差 63 待解释，0.031%）。
- 双弧十字切除模型**体积上不可能**（角部重叠上限 23438 < 所需 36267）⇒
  「单弧贯穿」由体积反证锁定 —— **体积不只是校验，还是结构选择的判据**。

## §108 半联轴器 v7 —— 用户纠偏「整体圆柱 + 圆形切面耳朵」+ Right 面符号 + 哨兵定位法 (2026-10-04)

### ① ★★★ 读剖视图先判**剖切方向**——「俯视 A-A」的视图竖直方向 = 横向 X
半联轴器我第一版把黄框剖面当**竖直轴向剖面**（34H8 竖直 ⇒ 耳板上下布置），
用户一句「键槽应该夹在两个小耳朵之间」点破：该剖面实为**水平剖切**（从上往下看），
视图竖直方向是横向 X ⇒ 34H8 沿 X ⇒ **耳朵在 3/9 点方位、键槽 12 点正好夹在中间**。
判据：①端面视图键槽恒在 12 点；②剖面上下壁 9mm 连续无槽（键槽不在 12/6 壁上）；
③3D 渲染 Ø13 孔口周围是**曲面** ⇒ 耳板外侧面保留圆柱面。三证合一才敢转 90°。

### ② ★★★ 毛坯形态：整体圆柱 Ø50×78，右段是「铣出来」的不是「拼上去」
v6 用「圆柱39 + 全高块39」拼：过渡区（Z39..48）多出方形角材料（数值积分 ≈7,500mm³ 量级），
且耳板成平板——视觉「像两件粘的」，体积对不上。v7 改**圆柱毛坯 + 外廓一刀切除**：
一把刀（外矩形 + R6 半圆缺口×2 − R13 耳）沿 X 向 ThroughAll 双向，
耳朵/板侧/R6 过渡/R13 圆耳全部自然成形，右段外表面保持圆柱面。
★ 板类/叉类件优先想「毛坯是什么、铣掉什么」，别按「拼块」分解——拼块会发明图纸没有的材料。

### ③ ★★ Right 面草图 **u→−Z**（探针实测）——± 符号没钉 = 整刀切空气
10 秒探针计划：Front 凸台(Z0..10) + Right 面 (u=70,0) 10×10 凸台 ⇒ 读 bbox Z 跨度：
75 ⇒ u→+Z；**85 ⇒ u→−Z（本机实测）**。同 sketches 上的圆心也一样
（Ø13 耳孔 cx=−65、Ø10 cx=−13 才落到 +Z）。凡换基准面画「有符号位置」的轮廓，
先跑这个探针或故意让 check_bbox 抓一次实际值。

### ④ ★★ fillet 选边点必须实打实落在材料边上
(0,±13,40) 看似在叉口顶角上，但该点在 r16 腔开口内（腔在 Y=13 处 X=±9.33），
tol=3 恰好抓到**腔口圆边**（凸缘）⇒ 圆角反而去料被守门抓住。
正确选法：凹角边被腔开口**分成 4 段**（|X|∈[9.33,17]），选 4 个角点、tol 收到 2。

### ⑤ 哨兵逐级对账 = 定位「顺序缺陷」的唯一手段
v6 六个 check_mass 哨兵（tol 放宽只记不拦）一次跑完，逐级 dV 与解析对表：
m3 实测 −1,390.10 = 解析 35.63×**39**（不是 ×40）⇒ 当场暴露「块后建回填 1mm 实体塞」。
真机体积对不上解析时，别猜哪刀切错——插哨兵，让数据指出是哪一步开始分叉。

### ⑥ 本件完工口径
D:/out/coupling_half.SLDPRT，64 步全绿（哨兵 ±0.2% 钉值），终值 **57,440.54mm³**
（数值 CSG 0.07% 吻合）。近似声明：键槽长 40=腔深（图纸未注）；C1 未注倒角未建；
Ø10 只穿一壁（黄框剖面仅一带缺口）。

## §109 半联轴器 v8 —— 「图纸圆角在模型里放不下」= 几何判据错的强信号 (2026-10-04)

### ① ★★★ 图纸标注的圆角在整个模型里**找不到能放的位置** ⇒ 先查几何，别调圆角参数
v8 把叉口从 34×26 矩形槽改成 **Ø34 圆孔**后，f1(R8) 反复报 `圆角重建失败 12`。
逐候选实测（r=8 / r=1 各试一遍，全部在**同一份健康会话**上）：

| 候选边 | R8 | R1 | 结论 |
|---|---|---|---|
| Ø34 孔口弧（Z=40, r=17） | 失败 12 | 能过（但**去料** ΔV=−2.18 ⇒ 其实是凸棱） | R8 太大 |
| 板侧 Y=±13 与圆柱面交线(X=±21.35) | 失败 12 | 能过 ΔV=−14.18 | R2 起就失败 51 |
| 叉口内竖角(X=±10.95,Y=±13) | 失败 12 | **也失败 12** | 根本不能倒 |
| 对照：Ø50 外棱（普通外棱） | — | R2 正常 ΔV=−132.4 | **会话健康** |

⇒ 本版几何里**没有任何位置容得下 R8**。根因不是圆角参数，而是：
**Ø32 腔与 Ø34 叉口孔共轴、在同一 Z=40 面交汇** ⇒ 平底只剩 **1mm 宽环带**（π(17²−16²)≈103mm²）。
v7 的 R8 之所以成功，是因为它的叉口是 **34×26 矩形槽**、底面是 **7.7mm 宽平底**
（|X|∈[9.33,17]，腔口圆把它切成 4 段，中点 (±13.17,±13,40)）。
★ 判据：**「图纸特征放不下」先查几何**（量台阶宽度 / 槽形），再决定圆角。
★ 旁证：对照组能过 ⇒ 排除「会话退化」；同一半径在 4 处全灭 ⇒ 排除「这一条边选错了」。
★★★ **本条后来被⑥推翻了一半**：上面的"无处可放"**只在"键槽先切"的前提下成立**。
   把打断孔口圆环的键槽**移到 R8 之后**，同一条边、同一组参数就**一次成功**。
   ⇒ 修正后的顺序是：**先查特征顺序（这条边是不是被后来的切除打断了），再查几何**。

### ② ★★ `_edge_mid` 对近半圆弧会退化成「两端点平均」⇒ 两段弧中点**完全相同**
Ø34 孔口圆被键槽切成两段弧，`_edge_mid` = (起顶点+终顶点)/2，两段弧端点互为反向
⇒ 两段弧的 mid **都是 (0,16.2481,40)** ⇒ 坐标选边只能命中其中一条、无法区分。
补救：`propagate=true`（切线延伸）会把共享顶点的另一半弧带上；但注意它**停在不切线处**。
取值办法：写探针 dump 每条边的 起/终顶点 + `ICurve.CircleParams`(圆心+半径)，
一眼分清「两段弧 / 两同心圆」。**别只看 `_edge_mid`**。

### ③ ★★ 无图（模型不支持看图）时，用 **ASCII 降采样**读图纸与渲染图
`ascii_views.py` 把视图区域按块最大值降采样成 ~150 列字符画（`#`/`+`/`.`），
能直接读出轮廓是圆、是矩形、有没有内孔——比猜强得多。
配合 `sw_drawing.analyze()` 先分视图区域（本例检出 3 个 + 右下角还有第 4 块），
OCR 只做交叉验证（本例 26 项里 **R6/R8/Ø34/Ø32 全部漏读**，只读到 48/39/78/40/13/10JS9）。
★ 阈值 `crop < thr` 里的 thr 要按分位数取，绝对阈值会把浅色填充整片吃掉。

### ④ ★ 晚期绑定下 `GetTitle` / `ActiveDoc` 是**属性**，加括号就炸
探针里写 `sw.app.ActiveDoc.GetTitle()` ⇒ `TypeError: 'str' object is not callable`
（错误信息完全指不到根因）。零参成员一律走 `sw_connect.member(obj, "GetTitle")`；
`ActiveDoc` 直接用；枚举边/面用 `sw_ops._edges(sw)/_faces(sw)`（与 fillet 选边同一套算法）。

### ⑤ 本版完工口径（v8）
`D:/out/coupling_half_v8.SLDPRT`，62 步全绿，终值 **58,716.96mm³**，bbox 50×50×78，max r=25。
三处用户纠偏已改：①Ø10 弃偏置面改基准 Right 面盲切 26（红框）
②外廓过渡改「R6弧+Z=竖直段」（绿框）③叉口 34×26 → **Ø34 圆孔**（箭头）。
★ **R8 在 v8 里不建**（当时判"无处可放"，见①）；**真因是切除顺序，见⑥** —— v8g 已建成。

### ⑥ ★★★ 「圆角选不中」的真因是**切除顺序**：把打断圆环的切除挪到圆角之后
① 的"无处可放"是**误判的一半**。真链条：
1. v8 里键槽(`e3`)排在 Ø34 孔(`e4`)**之前** ⇒ 孔口整圆被键槽切成**两段弧**；
2. `_edge_mid` 对两段弧退化（②）⇒ 两段弧 mid **都是 (0,16.2481,40)** ⇒
   坐标选边 `[17,0,40]` 必然命中的是**凸顶那条弧**（material=add 会去料/失败）；
3. 换别的候选边也全灭 ⇒ 得出"几何放不下"的**错结论**。

**破解**：把**键槽块整体移到 f1(R8) 之后**（`coupling_half_v8g.json`，步序
`…e5→m5→f1→m6→[键槽 p3…e3]→m3b→…`），孔口回到**单条 R=17 整圆**：
```
f1 = {"op":"fillet","r":8,"edges":[{"point":[17.0,0.0,40],"tol":1.0}],
      "propagate":true,"material":"add"}
```
⇒ **一次成功**，ΔV=**+130.39**（与"无键槽对照模型"单独实测的 +130.45 吻合到 **0.06mm³**）。

★ 判据（写进会话原则）：**圆角要吃到的边被"后来的切除"打断 ⇒ 顺序错了，不是几何错了**。
   特征顺序：**先建要靠它取边的特征，再切会打断这条边的槽/孔**。
★ **验证手法（最值钱的一步）**：造一个**去掉打断特征**的对照模型
   （`plan_v8_nokey.json` → `零件25`，41 步即出），在它上面**单独测这条圆角**。
   若这里成功、正式版失败 ⇒ 坐实"顺序问题"，**别再调圆角参数**。
   本例对照模型上 `rim R8 add prop: OK dV=+130.45` 直接指向结论。

### ⑦ 交付口径 v8g（**推荐版**，含 R8）
`D:/out/coupling_half_v8g.SLDPRT`，**63 步全绿**，终值 **58,847.41mm³**，bbox 50×50×78，max r=25。
= v8(58,716.96) **+ R8(+130.45)** —— 键槽只改了顺序、几何未变（切掉量对得上）。

逐级体积对账（**全部真机钉值，tol=0.002 相对**，系统自洽）：
```
m1  153152.6419  毛坯 Ø50×78
m2  120982.7331  Ø32 腔深 40
m4   86481.7626  Ø34 叉口孔（键槽未切）
m5   63006.3338  外廓切除
f1      (OK)     R8 叉口根部圆角  ΔV=+130.39
m6   63136.7200  R8 后
m3b  61710.9715  键槽后切（切掉 1425.7485 = v8 键槽量，**完全相同**）
m7   59559.6336  Ø13 耳孔
m8   58847.4097  Ø10 孔（终值）
v3   58847.4097  ★ 与 m8 一致
```
★ 三处用户纠偏全部消除：红框 Ø10 孔落体、绿框外廓 R6弧+竖直段、箭头 Ø34 圆孔；
  R8 亦按用户答复"叉口根部圆角"建成。
★ 版本关系：**v8g ⊃ v8**（v8 无 R8、键槽先切）。要 R8 就用 v8g（推荐）；只在无键槽顺序约束下复现旧版才用 v8。
★ 中间体积的**口径陷阱**：v8 的 m5(61580.585) 是"**键槽已切**"值，直接抄进键槽后移版会**误报超差**
  —— 那不是几何错，是**钉子钉错了位置**。改顺序后所有中间钉值都要**重新真机取**。

### ⑧ v8h —— 键槽按图纸改为**轴向贯穿全长**（用户 2026-10-04 确认）
用户截图（端面青框，宽 ≈10 ≈ 10JS9）指出「**键槽在图纸是完全贯穿的**」；
v8g 的键槽是 `depth=40` **盲切**（只贯穿 Ø32 腔段），端面上根本看不见 ⇒ 错。
改法只有一处：`end_cond=swEndCondThroughAll + both_directions=True`（轴向 Z 0..78 全贯通），
槽宽/深度不变（`X∈[−5,5]`，`Y 14→19.3`，即 Ø32 段深 3.3）。

- **63 步全绿，f1(R8) 依然成立**（11.9s）—— 键槽贯穿会把 Z=40 孔口 12 点那段 R8 **切掉一部分**，
  但 R8 先建、SolidWorks 正常处理「圆角被后续切除切断」⇒ **无需特殊处理**。
- 终值 **58,804.47mm³**（= v8g 58,847.41 − 多切 42.94）；键槽总切量 1468.6913（v8g 盲切 40 只有 1425.7485）。
- 逐级：m1 153152.6419 / m2 120982.7331 / m4 86481.7626 / m5 63006.3338 / m6 63136.72（R8 后）
  / m3b 61668.0287 / m7 59516.6907 / m8 = v3 **58804.4669**；bbox 50×50×78、max r=25。
- 产物 `D:/out/coupling_half_v8h.SLDPRT`；报告 `probe_v8h.json`。
- ★ 顺带二次验证⑦的口径陷阱：**e3 之前**的钉值一字不改就能过，**e3 之后**必须全部重取。
- ★ 端面视图可直接判"贯穿与否"：**贯穿 ⇒ 端面上看得见键槽缺口；盲切 ⇒ 看不见**（最省事的验收判据）。
- ★ 待确认：图纸上键槽**径向**是否也要切穿外圆（截图青框上缘接近外圆，但用户答"轴向"）——
  本版径向仍止于 `r=19.3`（Ø32 段深 3.3、Ø34 段深 2.3）。

## §110 减速器 · 小盖（第 28 件）—— 三条「口径 / 状态」坑 (2026-10-04)

### ① ★★ 半剖面里画了倒角/锥口 ⇒ **回转后 ≠ 纯环体积**
小盖回转半剖面 6 点里含「孔口锥口 45°×1.5」与「外缘倒角 45°×0.5」。
m0（回转后校验）挂的是**纯环** `π/4(34²−14²)·7 = 5277.876` ⇒ 真机实测 **5211.640** 报超差；
差 `66.24 = 锥口 53.014 + 外缘倒角 13.155`。
★★ **5211.640 正好等于 `sw_profile` 报的 Pappus 体积** —— 两个独立算法撞同一个数，
  ⇒ **不是模型错，是钉子钉错了位置**（口径）。判据：**先怀疑期望值没算全，再怀疑几何**。
★ 口径规则：**"X 之后"的体积必须把 X 之前所有已生效的几何都算进去**（含画在草图里的倒角/锥口）。

### ② ★★ 分步体积要按**当时几何**算 —— 沉孔时通孔还没切
顺序「回转 → 沉孔 → 通孔」；m1（沉孔后）我挂的是 `环 − (Ø6²−Ø4²) 环带 = 5070.334`，
真机实测 **4957.171**，差 `113.16 = 3×π/4·Ø4²·3`。
⇒ **那一刻 Ø4 还没切**，沉孔切掉的是**整块 Ø6×3 = 254.469**，不是环带 141.372。
★ 规则：**分步 check 的期望值 = 到那一步为止的真实几何**，不能引用"最终态"的分解式。
★ 两种分解的**总和相同** ⇒ 终值一直是对的，**只有中间那一步会红** ——
  这正是"中间 check 不是多余的"的证据（它抓住了分解口径，而不是几何）。

### ③ ★ 同一基准面开**第二张**草图前必须重新 `select_plane`
小盖在 Right 面上先后开「沉孔位」「通孔位」两张草图；第二张直接 `sketch_begin` ⇒
报 `开始草图前必须先选一个基准面/平面（用 select_plane）`。
根因：上一个特征（切除 / 圆周阵列）**清空了选择集**。
★ 与 §109⑥ 同源（都是"顺序/状态"）：**特征 op 之后，选择集不再是你以为的那样**。

### ④ 交付口径（小盖）
`D:/out/_train/reducer/6-cap_small-34x14x7.SLDPRT`，**32 步全绿**；
`--reconcile` 包围盒 **7×34×34 差 0.0000**、体积 **4806.375 vs 参照 4806.400（0.0005%）**。
进度 **27/35 → 28/35**。剩余 7 件：箱座/箱盖（等 Q1 朝向）、轴承 6204/6206（等 Q3 形态）、
通气塞/螺塞（卡 Q11 真螺纹）、反光片（卡 sphere 面）。
★ 项目约定（回归会拦）：**每新增一个生成器，必须配一个同名探针** `assets/probes/reducer_<part>.json`
  —— 忘了会在 `sw_selftest.py` 「每件有生成器的零件都有同名探针」这一项直接红。

## §111 减速器 · 反光片（第 29 件）—— 推翻旧判据 + 斜槽建法 + S7 锚定口径 (2026-10-04)

### ① ★★★ 「卡 sphere 面」是一条**错的判据** —— 球冠 = 圆弧母线回转，不需要 sphere 专属 op
反光片被前面几轮记为"卡 sphere 面（本机不支持球面）"，**从未动手**。
真机 dump 面曲面参数后真相：它是**纯回转体** ——
球冠（R14.875/R17，球心在原点）只是半剖面里**一段圆弧母线**，
用 `op_polyline(close=False)` + `op_arc3p`（**起点/终点/弧上点**顺序）收口 + `op_revolve_boss` 即可。
★ 规则：**"卡 xx 能力"这类结论，必须先用 `sw_faces` dump 到真实曲面参数再下** ——
  这次 dump 一次就推翻了攒了几轮的阻塞项。**没有 dump 的"缺能力"只是猜测。**

### ② ★★ 两个对称斜槽 = **两次独立 `revolve_cut`**；`circular_pattern(count=2, angle=360)` 建不出
Ø3 斜槽（轴与 X 轴成 **±17.5°**、各切 **15.15mm³**）本以为是"特征级圆周阵列 2 个"，
真机实测 `circular_pattern` **报「圆周阵列没生成特征」**。
改成**两次独立 `revolve_cut`**（各画一条**斜中心线** + 从轴延伸到 r=1.5 的矩形）即通。
★ 教训：`circular_pattern` 只对"**有实体的特征** + 合理阵列轴"稳；**回转切除**这种
  本来就"绕自身轴转"的特征，再叠加圆周阵列常常建不出来 ⇒ 对称件**优先两次独立特征**。

### ③ ★★★ `revolve_cut` 静默失败的根因：`last_sketch_name()` **取错草图**
现象：**单侧单独跑永远成功**；连着跑第二个必败，报「**特征成功但体积没减少**」。
- 先怀疑几何 ⇒ 不行（`slots[1]` 单独能切 15.15，几何无误）；
- 再试 `op_mirror` 8 组全 FAIL「特征树没新增」；
- 真根因：`revolve_cut` **不写 `profile`** 时走 `ctx["last_sketch"]`，
  而它是 `op_sketch_end` 里由 `last_sketch_name()`（= 特征树里**最后一个 ProfileFeature**）定的。
  前面几张草图已被各自特征**消费并嵌套进子节点**，遍历顺序一变就**取错草图** ⇒ 切到别的轮廓上 ⇒ 体积不变。
★ 修法：**`revolve_cut` 显式传 `profile`**（本件写 `"profile": sk`）。
★ 判据：**"回到第一条草图就能过"是这类顺序 bug 的典型指纹**。

### ④ ★★ S7 锚定：离轴回转轴补「**落在草图原点的构造点**」，**别补中心线**
斜槽回转轴**天生离轴**（到 X 轴 r≈0.926，且与 X 轴成 17.5°）⇒ **轴永远过不了原点**，S7 三选一全不满足。
★ 补一个 `op_point(x=0, y=0)`（构造点落草图原点）满足"图元点落在原点"。
★ **千万不能改成补一条中心线**：`revolve_cut` 的兜底 `_select_centerline_in` 抓的是
  **草图里第一条中心线**当旋转轴 ⇒ 多一条会把轴抓错（后果是静默切错，或体积不变）。
  **点不是线，不会污染轴选择** ⇒ 这是唯一安全解。

### ⑤ ★★ S7「中心线锚定」有**两条口径，必须取并集**
- (a) 历史口径：中心线**躺在 x 轴上**（`y1=y2=0`）—— 项目里键槽件就是这么画的
  （`gear_shaft` 的 `键槽` 草图 `(108.75,0)-(138.75,0)`，note 明写"要求中心线落在 y=0 上"）；
- (b) 规则文本口径：中心线**过原点**（原点落在线段上）。
★ 坑：原实现**只认 (a)** ⇒ 把"竖直过原点的中心线"（`/assets/probes/revolve_cut.json` 的
  `(0,-25)-(0,25)`）判成没锚住 —— **实现比规则文本更严 = 假阳性**。
★ 教训：**收紧判据前先批量回跑**。我一度把 (a) 换成"必须过原点"，当场**误伤已交付的 `gear_shaft`**
  ⇒ S7 从 2 红变 1 红（换了个件红）。正确做法是**取并集（严格超集）**：0 回归 + 补上文本口径。

### ⑥ ★ lint 小坑：`sw_spec.sketch_geometry` 把 `point` 当 `circle` 读 `cx`/`cy`
`op_point` 的真实参数是 **`x`/`y`**（和 `points` 列表），但 lint 里
`elif op in ("circle", "point"): cx,cy = ...` ⇒ **点的坐标永远读不到** ⇒ 拿点当原点锚时 S7 判"没锚住"。
★ 规则：**离线校验器读 op 参数必须按 op 自己的声明**（`sw_ops` 里的 `params`），
  不能想当然按"长得像的另一个 op"读 —— 否则检查器自己制造假阳性。

### ⑦ 交付口径（反光片）
`D:/out/_train/reducer/3-reflector-34x4.125.SLDPRT`，**44 步全绿**（新增 2 个 `org*` 锚点）；
`--reconcile` 包围盒 **4.125×34×34 差 0.0000**、体积 **1762.7106 vs 参照 1762.706（0.0003%）**；
体积逐级自洽：m0 `1868.4161`（Pappus 手算 1868.403，差 0.0007%）→ m1 `1793.0179`（3×Ø4）
→ rcut0 `1777.8644`（−15.1535）→ rcut1 `1762.7106`（−15.1538）。
进度 **28/35 → 29/35**。剩余 6 件：箱座/箱盖（等 Q1 朝向 + `mirror` 能力）、
轴承 6204/6206（等 Q3 形态）、通气塞/螺塞（卡 Q11 真螺纹）。
★ 反光片补丁脚本（`patch_params_reflector.py` / `patch_reflector_built.py`）**已归档 `_applied/`**。
★ 参数表是 **CRLF + indent=2** ⇒ 尽量**定点文本插入**。
  ★ **更正（2026-10-04 实测）**：Windows + CPython 下 `open(p,'w')` 默认 `newline=None`，
    `\n` 会被转成 `os.linesep` = **CRLF** ⇒ `json.dump(..., indent=2)` **不会**把 CRLF 变成 LF
    （实测 CRLF=3624 / LF=3624，全 CRLF）。真正会踩的是**显式传 `newline=''` 或 `newline='\n'`**。
    但 `json.dump` 仍会**重排格式**（缩进/空格/数字 repr）⇒ 大文件优先定点插入，
    小改动可整体 dump + **回查换行符计数**。

## §112 减速器 · 螺塞 + 通气塞（第 30/31 件）—— 真螺纹绕路 + 六角尺寸 bug + 锥倒角体积 (2026-10-04)

### ① ★★★ 「卡 Q11 真螺纹」是一条**错的判据** —— M10 螺纹用 `helix`+`cut_swept` 一次建成
螺塞/通气塞被记为"卡 Q11 真螺纹（`real_thread` 建不出特征）"，**从未动手**。
真相：**真螺纹有现成绕路** —— `assets/plans/leadscrew.json`（丝杠）23 步全绿 `failed:null` 就是铁证。
两件的**唯一共同阻塞**就是真螺纹，且都可用**扫掠**建。
★ **工艺哨兵**（先花 1 分钟验最小工艺，再写正式件）：
  `probe_thread.plan.json`（M10×1.0，轴 = 全局 X）**22/22 步全绿 45s**：
  ```
  Right 面画 Ø10 圆（**留在草图里**，不 sketch_end）→ helix(pitch=1.0, height=4.5)
  → Front 面画牙槽 polyline + 过原点中心线 → cut_swept
  ```
  体积 **942.478 → 914.337（减 28.14）**；解析核对 0.222mm² × 132mm ≈ 29.4（差 4%）⇒ **真咬料**。
★ 规则：**"卡 xx 能力"下结论前，先 grep 现有 plans/probes 里有没有已经跑通的同类配方**。
  Q11 的真正结论应该是"`real_thread` 这个 **op** 有 bug"，**不是**"本机建不出螺纹"。

### ② ★★★ 六角件「**对边宽 s** vs **外接圆半径 r**」是两回事 —— 本项目最贵的术语混淆
`_hex_plan` 把 gb 表的 `s`（对边宽）**直接当外接圆半径**传给 `_hex_vertices`，
  ⇒ 六角**大 √3 倍**（M8 螺栓对边本该 13，实建 **22.5**）。
- `gb_standard.json::families.hex_bolt._note` **早就写着**「建模时 `r = s/2/cos30°`」——**代码没照它做**；
- `_hex_volume` 的解析式 `(3√3/2)s²` **同步放大** ⇒ **`check_mass` 一路绿灯**；
- `sw_selftest.py` 的断言**也按错的语义写**（"外接圆半径 = 对边宽"）⇒ **测试成了 bug 的帮凶**。
★ 这正是 **§102** 的教科书案例：**判据只保证「算的和模型一致」，不保证「模型和图纸一致」**。
★ 修：`r = s/√3`；面积 `(√3/2)s²`；bbox `[2s/√3, s, thick]`；**测试断言同步改**；
  受影响 4 件（nut_m8/nut_m10/bolt_m8x25/bolt_m8x65）**重跑并覆盖正式产物**。
★ 数量自校验：作参照的**六角底面环面积** = 六角面积 − 内切圆面积
  （螺塞：37.0087 = (√3/2)·14² − π/4·13² = 169.74 − 132.73 ✓ 逐位）——
  **凡是"某张面的面积与某组尺寸逐位吻合"，那组尺寸就是铁证**，比 bbox 可靠。
  同理：**"顶面是 Ø14 圆 = 六角内切圆"直接点名了 30° 锥倒角的起始半径**。

### ③ ★★ 锥倒角的切除量 **≠** 「锥面外环形旋转体」的体积 —— 差 40 倍
螺塞六角头的 30° 锥倒角，一开始拿"锥面外侧那块**环形旋转体**"的体积当切除量
  ⇒ `cham_v = 410.3`，而**真值 ≈ 9.0**（真机反推 8.98）⇒ **高估 40 倍**，
  把一条本该通过的 `check_mass` 判红（"体积超差：实际 1695.156，期望 1293.785"）。
★ 根因：那个环的径向范围 **[r_cone, R+3] 远超六角截面（≤ R）**，
  两者真正相交的只是**六个角**那一小块。
★ 正解（`_hex_chamfer_volume`）：六个"角"的**解析面积**（极坐标；φ0 = arcsin(f0/2) − 60°，
  f0 = √3·R/r_cone）× 沿轴**数值积分**。自校：r_cone = 7（内切圆）⇒ 6 角 = 15.80
  = 六角 169.74 − Ø14 圆 153.94 ✓。
★ **判据**：任何"切除量"都要问一句——**切除体与实体真正相交的是哪一块**？
  "旋转体体积"只在**切除体完全落在实体内**时才等于切除量。

### ④ ★★ `ref_plane`（基于**标准基准面**偏移）是安全的定位法
六角头要坐在 x = 9.0 的平面上，但**不能**用"引用前序实体面名 + `plane_offset`"
  （那是 `{{ vars.* }}`，会让**离线求值失败**，见 `_hex_plan` 头注的口径）。
★ 正解：`select_plane(Right)` → `ref_plane(offset=9.0)` —— `CreatePlaneAtOffset3`
  基于**选中的标准基准面**偏移，**不引用任何前序实体面名** ⇒ 计划仍可离线校验。
  本件两处用到：螺塞六角头（x=9.0）、通气塞螺纹起点（x=22.0），**方向均正确**（偏移沿面法向 +X）。
★ 实测：`ref_plane` 会把新基准面**自动选上**，紧接着 `sketch_begin` 即可画，不必再 `select_plane`。

### ⑤ ★★ `helix` 的螺旋起点可以在**任意 x** —— 牙槽位置的 x 只需与起点一致
通气塞螺纹杆在 x∈[22, 34]，用 `ref_plane(offset=22)` 把螺旋的圆挪到 x=22。
★ 牙槽草图画在 **Front 面**（过轴），**牙槽点的 x 必须落在螺旋起点处**（x≈22），
  而 **S7 锚定靠"过原点的中心线"**（`(0,0)-(0,R+1)`）—— **两者解耦**：
  中心线管锚定、牙槽管位置（同 `_hex_plan` 螺栓的做法）。
★ 螺塞则更省事：**螺旋直接从 x=0（Right 面）起、height=6.0**，
  x∈[0,1.5] 是端锥（r 3.5→5），牙槽（r 4.39~5.5）**只咬 Ø10 杆那 4.5 段**——空切不咬料，无需偏移。

### ⑥ ★ 含螺纹件的 `bbox` **只记录不判定**是合法例外
`GetBodyBox` 对**螺旋扫掠面（B 样条）会虚报**（同 §坑表 2026-09-25 螺纹虚报）。
⇒ `plug.bb2`（最终、含螺纹）保留 `mode: "record"`；而 `plug.bb1`（六角头后、全平面）与
  `breather.bb0`（全圆柱/锥面）**必须判定**（`tol_mm: 0.05`）。
★ `sw_selftest.py` 的 `rec_users` 白名单已相应放宽为 `{washer_s8.bb, plug.bb2}`
  （**只许这两类**：螺旋扫掠件、含真螺纹件），其余一律判定。

### ⑦ 交付口径（螺塞 + 通气塞）
- **螺塞** `16-plug-af14xL15.SLDPRT` **43/43 全绿**：回转 685.6526（= 参照 profile Pappus）
  → 六角柱后 1704.0985 → 锥倒角后 **1695.156**（解析 1695.1625，差 **0.006**）
  → 螺纹后 1665.53（−29.6）；bb 六角 **[15, 14, 16.166]** ⇒ 对边宽 14 精确。
- **通气塞** `10-breather-D23xL35.SLDPRT` **34/34 全绿**：主体 7218.66、中心孔切除 **293.87**
  （= 解析 π/4·4²·23 + π/3·2²·1.1547 逐位）、螺纹切除 77.30（12 长）；bb **[35, 23, 23]**。
- 滚花（108 脊 / 235 张面）**简化为 Ø23 光柱**，登记 `parts.breather._simplified.knurl` + **Q13**。
- 进度 **29/35 → 31/35**。剩余 4 件：箱座/箱盖（等 Q1 朝向 + `mirror` 能力）、轴承 6204/6206（等 Q3 形态）。
★ 诊断脚本（`summarize_faces.py` 等）用完归档 `_applied/`；
  `sw_reducer.py` 改动前备份 `sw_reducer.py.bak_20261004_hexfix`。
★ 进度统计口径：`sw_selftest.py` 的「实测进度」走 **op 覆盖数**（现 80/80），
  零件数进度是**人读** `params.parts[*].status`（2026-10-04 起为 **33 件**）。

---

## §113 减速器 · 深沟球轴承 6204 / 6206（第 32/33 件）—— 球的回转轴 + 圆周阵列的适用边界 (2026-10-04)

**主题：把最难的 D5 件从「卡 Q3 形态」变成两件 95/95（改后 111/111）全绿的实体。**

### ① ★★ 球心离旋转轴 ⇒ 绕轴旋转半圆**得不到球**（本轮最贵的一条）
套圈都是回转体（绕 X 轴转半剖面 ✔），很容易顺手把滚珠也当回转体 —— **错**。
球只对**过球心的轴**对称。滚珠球心在分布半径上（6204：R16.75），**不在 X 轴上**
⇒ 绕 X 轴旋转那个半圆得到的是**环面状的东西，不是球**。
★ 正解：回转轴取 **「过球心的水平中心线」**（球心所在平面里 y = pitch_r·cosθ），
  半圆直径**贴在这条中心线上**、绕它转 → 球 ✔（哨兵实测体积 164.630 = 4/3π·3.4³）。
★ 一般化：**"是回转体"必须连着说"绕哪根轴"**；换一根轴就不成立了。

### ② ★★ `circular_pattern` 对「会产生**独立 body** 的种子」**静默无产出**
想 8 个滚珠一次阵列 —— **真机失败**：op 返回 OK（特征树里真有"滚珠阵列"），
  **体积却一点不涨**（7982.487 → 7982.487；bbox 与 body 数也没变）。
★ 对照实验（同一 op、同一套 mark，只换种子）：
  · 圆盘上**切**一个孔绕 Z 阵列 6 个 → ✔（既有已验证探针）
  · 圆盘上**凸出**的凸台绕 Z 阵列 3 个 → ✔（体积 = 28274+3×1256.64 精确通过）
  · **孤立球**绕轴阵列 2 个 → ✗ · 外圈 + **独立球**阵列 8 个 → ✗
  ⇒ 判据：**阵列实例必须与已有材料"合并"才有效**；实例与主体/彼此**不相交**
    （会生成新 body）时，特征建了等于没建 —— **成功要判体积，不能只看返回值**。
★ 规避：**不用阵列**，改 **Z 次独立 `revolve_boss`** —— 每个球放在一张
  **z 偏移基准面**上（`select_plane(Front)` → `ref_plane(offset=|z|, flip=z<0)`）。
  ★ `ref_plane` 与 `plane_offset` 不同：**它不处理负偏移**（`CreatePlaneAtOffset3` 的
  Offset 是距离量）⇒ 负 z 必须 `flip=True`。
★ 相位取 **22.5° + 360k/Z**：保证没有一个球落在 y=0（球心若在旋转轴上，轮廓会穿轴 ⇒ 回转失败）。

### ③ ★ 怎么判定一根基准轴的**方向**（顺手记个手法）
拿一个**偏心特征**绕它阵列，看 bbox 哪两个轴变了。实测：x=20 处一个沿 Z 的圆柱，
  绕 `axis_from_planes(Front, Top)` 阵列 2 个 ⇒ **X 宽仍 6（17..23）、Y/Z 变** ⇒ 轴 = X ✔
  （即 Front 面法向 Z、Top 面法向 Y ⇒ 交线 X；与 `mirror` 的 plane→轴映射一致）。
★ 顺带纠正一个自己的预期：`equal_spacing` 下 **spacing = 总角/个数** ⇒ count=2/angle=180
  给出实例在 **0°/90°**，不是 0°/180°（这不是 bug，是"n 个实例均布在总角内"）。

### ④ 沟道几何与体积账（本件口径）
· 台肩→沟底落差 = (球径 − 径向间隙)/2（6204：(6.8−4.615)/2 = **1.0927**，两侧对称）。
· 沟道弧取 **保形弧 R = 球半径 + 0.05 游隙**（GB 轴承实际做法；参照件用更浅的
  非保形槽 R≈5.4/6.4）—— 保形弧既像真轴承，又保证**滚珠与套圈不互相吞并**（留游隙）。
· 验收：bbox `[B,D,D]`（全平面/圆柱/球面 ⇒ GetBodyBox 可信）**判定**；
  体积用**解析值**逐级钉住：6204 外圈 7817.845 → +内圈 12237.461 → +8球 **13554.550** ✔

### ⑤ ★★ 已声明简化差异：**别让它对账永远报红**（新增 `_reconcile` 机制）
轴承省略保持架 + 铆钉 ⇒ 体积天然比参照件少 ~296.6mm³（**2.14%**）> 2% 默认容差
  ⇒ `--reconcile` 永远报红 ⇒ **告警疲劳，真回归会被淹掉**（同 sw_doctor 的 PENDING 分档口径）。
★ 新增 `parts.<part>._reconcile = {"vol_tol_pct": 2.5, "why": "..."}`：
  **必须写明 why**，否则 `reconcile()` 直接判失败 —— **绝不允许静默放宽**
  （把"没建对"降级成"已声明"是最坏的）。放宽后的原因会**打印在结论上方**（可见，不消失）。
★ 这层只是**二次对账**；主要门禁是计划里每步 `check_mass` 的 **0.5%** 容差。

### ⑥ 交付口径
- `24-bearing-6204-20x47x14.SLDPRT`、`30-bearing-6206-30x62x16.SLDPRT`：**各 111/111 全绿**
  （95 几何步 + 8 构造点 + 8 约束闸门；改前为 95/95）。两件 bbox 差 **0.0000**、
  最大圆柱半径 = D/2、体积逐级吻合解析值。
- 参数**实测**（不是凭记忆）：滚珠 6204 = 8×Ø6.8 @R16.75、6206 = 8×Ø7.8 @R23.0；
  台肩/OD/孔写入 `parts.bearing_*._measured_faces.solved`。
- 保持架/铆钉不建 + 沟道保形弧 → `_simplified` + **Q14**；**Q3（单件 vs 子装配体）仍开放**。
- 进度 **31/35 → 33/35**。剩余 **2 件**：箱座 / 箱盖（等 Q1 朝向 + `mirror` 能力）。

### ⑦ ★ 两张"项目口径"闸门会自动抓你（S2 / S7），别等自检才想起
- **S2**：每张草图后面必须有 `check_sketch_constraint` —— 8 个滚珠草图漏了 ⇒ 自检报 16 处。
- **S7**：每张草图必须"锚住原点"（图元点落原点 / 中心线过原点 / 以原点为心的阵列）。
  滚珠草图 **球心离轴、中心线不过原点** ⇒ 必须按 §111 补一个**落在原点的构造点**
  （`point(0,0)`），**不能用中心线代**（会被 `revolve_boss` 抓去当旋转轴）。
★ 两条都是**离线秒级**能查的，写生成器时顺手加上，比真机跑完再回来补省一轮 2 分钟。

## §114 减速器 · 箱座 / 箱盖取证 + Q1 朝向实测（第 34/35 件，2026-10-04）

**主题：怎么把「装配体朝向」从推断变成实测；两件箱体几何；以及又一个「自动判据互相打架」的坑。**

### ① ★★★ 判「X 是轴向还是轴间方向」不要靠算，去扫装配体（`probe_asm_axis.py`）

Q1 悬了好几轮：箱体 230 到底是「轴间方向」还是「轴向」。推理都指向「230=轴间」，
但那是**推断**。做法：打开 `减速器装配体.SLDASM`，读每个组件的**全局包围盒**：
- 轴类件的包围盒**最长边** = **全局轴向**（两根轴一致才算数）；
- 两轴包围盒中心之差，去掉沿轴分量 ⇒ **垂直面内的偏置 = 轴间方向**，其**模 = 中心距**；
- 组件包围盒三边里 230 / 104 落在哪根轴 ⇒ 判读。

真机结果：轴向 = 全局 X；轴间方向 = 全局 **Z**，中心距 **0.0700 m = 70.00 mm**（残差 0）；
箱座/箱盖的 **230 边都在 Z** ⇒ **230 = 轴间方向**，104 = 轴向。✅

### ② ★★ `IComponent2.GetBox` 返回的是**米**，且是**全局**盒（不是局部盒）

两个连带坑：
- **单位是米**：0.11 是 110mm。打印时四舍五入到 2 位会让 `105.908` 显示成 `0.11`，
  看着像"对不上零件文件"，其实是同一组数。
- **是全局盒**：拿它**反推局部轴**是错的（我把 `R·e_longest` 当轴向用过，方向就偏了）。
  要局部朝向得看变换矩阵的旋转列，别拿全局盒凑。

### ③ ★★ 变换矩阵的**平移**不一定落在轴线上

交叉验证时发现两轴组件的变换平移差在垂直面内与「包围盒中心法」**不一致**
（差 0.73 m）—— 因为**组件原点**根本不在轴线上（零件原点在角上）。
⇒ 判轴线位置**用包围盒中心**（轴线对称件），变换平移只作**辅助**；
两者打架时以**几何量**为准，别以矩阵为准。

### ④ ★★★ 两个工具的「自动判主轴」会互相打架 ⇒ 箱体扫面**必须显式 `--axis`**

`sw_faces` 把 **箱座**主轴判成 **Z（错）**，`sw_segment` 判成 **X（对）**。
根因：`sw_faces` 按**圆柱面数量**投票，被 11 个 Ø2.5/Ø4 螺栓销孔（轴 ∥ Z）带偏；
箱座的**功能轴**是 X（轴承孔 Ø62/Ø47 都在 X 上，面积最大）。
⇒ **同一零件，两个工具给出不同的"主轴"是可能的**，别以为它们一致。
箱体类件扫面**一律显式指定**：箱座 `--axis 0`（轴向=X），箱盖 `--axis 2`（轴向=Z）。

### ⑤ ★★ 箱座/箱盖**原生朝向不一致** —— 别拿一件的坐标系套另一件

| 件 | 原生 X | 原生 Y | 原生 Z | 装配 |
|---|---|---|---|---|
| `1-箱座` | 轴向 (105.908) | 竖直 (82.764) | 轴间 (230) | 无需旋转 |
| `12-箱盖` | 轴间 (230) | 竖直 (70.112) | 轴向 (104.019) | **绕 Y 转 90°** |

⇒ 扫面 `--axis` 不同（座 0 / 盖 2）；**装配阶段箱盖要转**，否则转到反的。

### ⑥ 箱体实测几何（建模直接依据）

**箱座**（201 面 / 566713 mm³，对称面 x=0 97.3%）：中空箱体（104×80×230，壁≈8）+ 底座板
**104×180**（底面 y=-40，4×Ø9 安装孔 @x=±33.7, z=23/157）+ 剖分法兰 y=+40（6×M8 孔 Ø9 + Ø46 凸台）
+ 两处轴承塔（孔凸台面在 x=±20，9020.9mm²）+ **4 条加强筋**（z 两片 67.5/72.5 与 137.237/142.237，5mm 厚）
+ 油标 Ø14 / 放油 Ø8.5 / 纵向油道 4×Ø4。**轴承孔轴心全在 y=40**：
**z=70 处 Ø62（6206 输出轴）、z=140 处 Ø47（6204 输入轴）⇒ 中心距 70 逐位吻合**；
端盖座 Ø70 / Ø55 在 x∈[±45,±48]（深 3）。

**箱盖**（139 面 / 355625 mm³）：顶罩 = **双半圆隆起（figure-8）r≈70/64 与 62/56**，
剖分面 y≈0，孔轴心 (X=34.52,Y=0) 与 (X=-35.48,Y=0) ⇒ 中心距 70。

### ⑦ ★ 补丁脚本内嵌 `json.dumps(..., indent=…)` 会**污染行尾**

定点文本插入本该保 CRLF，但 `json.dumps` 生成的是**裸 `\n`** ⇒ 插进去后全文件混行尾
（LF 计数 > CRLF）。⇒ 补丁收尾**必须全文件归一化** `t.replace('\r\n','\n').replace('\n','\r\n')`
并**回查三个计数相等**。别只看"解析得动"就当没事。

## §115 减速器 · 箱座「三层主体」建模口径（用户定稿，第 34 件，2026-10-04）

**主题：薄壁箱体类零件的特征树顺序 —— 不是"想怎么建"，是"抽壳特征逼出来的"。**

### ① 口径本体

箱座按**抽壳约束**排，分三层主体，**其余特征全部基于这三层加工**：

| 层 | 名称 | 尺寸 | 由谁建 | 为什么在这一层 |
|---|---|---|---|---|
| **层1** | 箱体壁（薄壁箱体） | 104×180 圆角矩形 R20，壁 6 | `e0` 主坯 + **`sh` 抽壳**（移除顶面） | ★ **必须最先** —— `Shell` 特征要求先有实体；层2/层3 都长在它上面 |
| **层2** | 顶板（剖分法兰） | 104×230 R23 − 腔口 74×168 R6，厚 2.764 | `e2` 顶法兰 | 装在层1 的开口上 |
| **层3** | 底板（底壁加厚） | 内腔底面加厚到底厚 10 | `e1` 内腔底加厚 | 装在层1 的腔底上 |

**细节（轴承座 R37/R43、轴承孔/沉孔、法兰 6×Ø9、底脚 4×Ø9、油标、放油）全部排在三层之后。**

### ② 为什么不能"先建细节再抽壳"

先挖轴承座再抽壳 ⇒ 抽壳会把轴承座一起掏空（薄壁模式对**所有**面生效）。
先抽壳再挖 ⇒ 挖的是已经成型的腔体，行为完全确定。
⇒ **主体顺序由"哪个特征有前置依赖"决定，不由"哪个先想到"决定。**

### ③ `meta.layers` 显式登记，别只写在注释里

每层记 `size` / `built_by`（特征 id 列表）/ `why_first`（为什么必须这个顺序）。
理由：注释会被人手改掉，结构化字段能被 `sw_selftest` 的规范检查抓到。

### ④ 用户的三视图读图口径（同期定稿，长期有效）

**俯视图取最大长宽 → 主视图取高度 → 拆成几层主体 → 其他特征都基于这三层加工。**
主视图有剖面线 ⇒ 螺孔在内部；用不同颜色框出每层特征，确认结构后再排特征树。
（此前把"主体"和"特征树搭建顺序"混为一谈，导致顺序错乱 —— 这条口径就是纠正。）

## §116 每孔一特征：两个半圆外圆可能重叠（箱座轴承座，2026-10-04)

四张半圆草图（z=65 R37 / z=135 R43，各左右两片）如果合并成**一张草图里的 4 个轮廓**再拉伸，
两半圆的外圆在 Z 方向**互相重叠**（z=65+R37=102 > z=135−R43=92，重叠 10mm）⇒ 轮廓自交 ⇒ 草图建不出来。
⇒ **每个孔一个独立特征**（`boR0/boR1/boL0/boL1`），各自的 `MidPlane` 拉伸互不干扰。
孔本身（`bore`，两个半圆在**同一张**草图）可以合并 —— 因为它们半径小、互不相交。

## §117 拉伸之后草图已关闭 ⇒ 二次拉伸前必须**重新 `select_plane``

`extrude` 会关闭当前草图。此后直接 `sketch_begin` 会拿到**空/错误**的面。
凡是"先切一段、再在同一面上切一段"的孔（Ø9 + Ø18 沉孔、油标 + 沉孔、放油 + 沉孔），
第二段前都要补一步 `p7b` 式 `select_plane`。

## §118 ★★★ 「体积中性」几何 bug —— `check_mass` 的盲区（箱盖，2026-10-04)

**这是本项目至今最贵的一条判据教训。**

### 现象

箱盖窥视孔凸台的 `check_mass` 一直**绿**，但真机测出的净增量与解析差 **18%**
（真机 29669.28 vs 解析 24771.42，差 4898）。

### 根因

内腔两个半圆的**圆心**被写成 `(cx, tf)`、弦落在 `y = tf`（`tf` = 顶板厚），
正确应是 `(cx, 0)`、弦落在 `y = 0`。

**半圆沿 Y 平移不改变截面积** ⇒ 体积**逐位相同** ⇒ `check_mass` 全程假绿。
但顶壁厚从 **6** 变成 **3.236**（几何已经错了）。

### 为什么分段探针能抓住

`check_mass` 只给**累计**体积；两处误差抵消。**分段探针**（`check_mass` 不给期望值、只测）
能读出"这一步涨了多少"，把偏差定位到**具体特征**：
探针实测净增 29669.52，与真机 29669.28 差 0.0008% ⇒ 一次坐实根因在"内腔圆心"，不在锥台公式。

### 修完的连带修正

改回 `(cx, 0)` 后，旧 `v_bore` 的"中段吃法兰 y∈[0,tf]"项**前提失效**
（腔体对剖分面敞开后天穹顶壳下缘 y_in ≥ 56，中段无料可吃）⇒ `v_bore` 只留端壁项，`m3` 期望同步改。

### 铁律

- **`check_mass` 只保证"算的和模型一致"，不保证"模型和图纸一致"**（§102）。
- ★★ **本条是 §102 的反例**：改错了几何，判据**连"算的和模型一致"都不保证** ——
  因为两个错误在体积上**恰好抵消**。
- ⇒ **判据之外的验证不能省**：分段探针（定位到特征）+ 真机（定位到成因）。
- ⇒ **"体积没变"不等于"几何没变"**。凡是"某个改动理论上只影响位置不影响量"，
  都要额外问一句：**这个等价关系真的成立吗？**

## §119 加强筋：用 `extrude_boss` 而不是 `op_rib`（箱座，2026-10-04）

### ① 为什么不用 `InsertRib`

`op_rib` 需要 `ref_edge_index`（手动选一条边）+ 法向消歧 + `both_sides` / `drafted` 组合，
变量多且**静默失败**率高（选错边不报错，只是方向/厚度不对）。
加强筋是**直棱柱**（矩形截面沿 X 平扫）⇒ 用 `MidPlane` 拉伸**行为完全确定、体积可解析**，
能直接被 `check_mass` 卡住。

### ② 草图要**外延进实体**，避开零厚度相切

筋的 Y 范围从腔底（−30）到剖分面（40），这两个面**恰好都是既有实体边界**。
若草图正好画在边界上 ⇒ 布尔**零厚度相切** ⇒ 特征失败或行为不确定。
⇒ **上下各外延 1mm**（Y ∈ [−31, 41]）进入底板/法兰，边界不在切面上。
外延部分落在实体内 ⇒ **净增体积不变**（这点必须自己算，别假设）。

### ③ ★★ X 跨度必须**分两侧各拉一次**，不能在中间面一次拉通

筋从座内端面 `x=20` 连到外壁 `x=52`。
- 错：在 `Right` 面（x=0）一次 `MidPlane` 深 104 ⇒ X ∈ [−52, 52]，
  **内腔中央 X∈[0,20] 也会长出筋**，白增 `2×20×70×5 = 14000 mm³`。
- 对：在 `x=±36` 各建偏移面，`MidPlane` 深 32 ⇒ X ∈ [±20, ±52]。

体积差 14000mm³ 在 1% 容差（≈5300）下**必然被抓到**，但那时特征树已经脏了
—— 还是在**生成阶段**就想清楚，别等真机报警。

### ④ 净增体积要**逐段扣**

- 腔内段 Y∈[−30,40]：矩形 5×70 **减去**轴承座在该 Z 带的截面积
  （★ 座在 |u|<2.5 的 Y 范围 [40−36.9, 40] **完全被筋的 Y 范围包住**
  ⇒ 筋在**孔道范围内不新增料**，`v_bores` 公式不受影响）；
- 顶段 Y∈[40,41] 咬进法兰：腔口半宽 37 ⇒ 只有 X∈[20,37] 是新增；
- 底段 Y∈[−31,−30] 落在底板内 ⇒ **零增量**（不计）。

真机净增 **15953.627** vs 解析 **15953.63** ⇒ 逐位吻合。

## §120 装配体：坐标取面的「约定判别」+ 组件变换 + 干涉判据（箱盖×箱座子集装配，2026-10-05）

> 场景：拿学生原件 `1-箱座.SLDPRT` / `12-箱盖.SLDPRT` 当基体，装配 22 个组件
> （箱座 + 箱盖 + 4×M8x65 + 2×M8x25 + 6 垫圈 + 6 螺母 + 2 定位销）、41 条配合、
> 159 步真机计划。本节的每条都是**真机上先报错、再 dump 数据才定位到的**。

### ① ★★★ 取面：坐标「约定判别」必须用**包围盒 8 角点**，不能用盒心

`ArrayData` 到底是行主序还是列主序，COM 文档没写 ⇒ 代码里留了两种约定
（mode 0 = `Rᵀ·(p−t)`、mode 1 = `R·(p−t)`），靠"把全局包围盒变换回局部、必须落进
零件局部包围盒 `lbox`"来自检。**旧实现拿"盒心"当探针。**

结果：`26-A型圆形销` / `12-箱盖` 的真约定是 **mode=1**，而它们的**盒心在两种约定下
都落进 `lbox`** ⇒ 自检**无法判别** ⇒ 静默挑了 `mode=0` ⇒ 之后每次按坐标取点都被
映射到**镜像位置** ⇒ 报出来的是

```
点 [25.0, 43.5, 194.0] 离最近的面还有 3.514mm（容差 3.0mm，组件 COVER-1）
全局点 [21.0, 43.5, 194.0] 落在所有组件的局部包围盒之外 —— 坐标大概不是装配体全局坐标
```

**两句话都把矛头指向"坐标算错了"，而真因是"约定挑错了"——报错方向完全错。**
我按"坐标算错了"去核了半天坐标，全对。

修法 = 把全局盒的 **8 个角点**变换回局部，取"变换出来的局部包围盒"与真实 `lbox`
**偏差最小**的那个约定（误差量级：挑对 `<0.05mm`、挑错 `5~15mm`）。修后同一个点：

```
点 [25.0, 43.5, 194.0] → PINA-1 距面 0.0mm（mode=1，局部 [9.287, -2.0, 0.0]）
                       → COVER-1 距面 0.0mm（mode=1，局部 [-89.476, 3.5, 25.0]）
```

★ 通用教训：**判别探针必须落在「两种假设下结果不同」的位置上。**
盒心是**对称中心**，对"旋转约定"天生不敏感 ⇒ 拿它判别旋转约定 = 必然失败。
（同 §113① "球心离旋转轴 ⇒ 绕该轴转半圆得不到球"，都是"探针位置选在对称点上"。）

### ② ★ 盒级探针在「轴对称零件 × 非 90° 倍数摆放」上判别力弱 ⇒ 必须记录余量

六角头螺栓在装配体里**绕自身杆轴转了 30°**（六方朝向不同）。此时
`AABB(R·body) ≠ R·AABB(body)`，两种约定的误差会**接近**（真机：`B65A-1` 主 7.659mm、
备选同量级）⇒ 判别力弱，可能挑错。
⇒ `_asm_face_index` 记录 `mode_err`（主）/ `mode_err_alt`（备选）/ `mode_soft`
（**余量 < 0.5mm** 即置位），`asm_face_probe` 把它打出来。
**判别力弱时的最终裁判不是探针，而是「配合后组件有没有被挪走」（见 ⑦）。**

### ③ ★ 新 op `asm_face_probe`：把"判别过程"打印出来，让「约定错」与「坐标错」当场分开

只读 dump：每个组件一行（`lbox` / `mode` / `mode_err` / `mode_err_alt` / `mode_soft`），
再对给定的 `points` 报出"落在哪个组件、换算成什么局部坐标、最近面多少毫米"。
**这类"缺能力/取不到面"的结论，先 dump 再下**（同 §111①）。

### ④ ★★ 性能：逐面 `GetBox` 粗筛 + 跨调用索引缓存 —— 单条配合 123s → 几秒

`_asm_face_index` 要为每个组件的每个面取一次 `IFace2.GetBox`（箱座 201 面 + 箱盖 139 面
≈ 上千次 COM）。两个优化：
- **取点时先按"逐面局部包围盒"粗筛**，只有可能命中的几个面才调 `GetClosestPointOn`；
- **索引跨调用缓存**，键 = `(文档标题, 每个组件的全局包围盒)`。★ 签名里**必须**包含
  "组件站位"：配合一旦挪动任何组件，签名即变、缓存作废。**绝不按"距离上次多久"复用。**
效果：`m01_seam` 从 123s 降到 1~2s。

### ⑤ ★★ 命名时序：`insert_component(name=X)` 之后再摆位 ⇒ 名字被**重置**

真机探针（决定性）：

```
insert_component(name="B65A") → 组件名 B65A-1
set_component_transform(...)  → 组件名被重置成 "13-六角头螺栓 M8x65-2"（还吃掉一个实例号）
```

⇒ **改名必须排在所有 `set_component_transform` 之后**（否则白改）。
另外 `set_component_transform(component=<零件路径>)` 在**同名多实例**时报"有歧义"
（`resolve_component` 按路径匹配会返回多个）⇒ **摆位一律用自动默认名**
`<文件基名>-<插入序号>`（插入顺序确定 ⇒ 名字确定），最后再统一 `rename_component` 成唯一名。

### ⑥ ★ 同一文档里**多条配合可行**（推翻 §96② 的旧结论）

旧坑表写"第二条配合必然失效"。真机探针 `mate_multi.py`：baseline / 取回文档重开
两种走法各 4 条配合**全部建成**（`mate_count` 4/4、装配体落盘后取回仍是 4）。
⇒ 该结论作废，别再为此绕路。

### ⑦ ★★ 装配的**主判据** = 配合之后重跑一遍全部组件站位校验

配合前组件就已按参照站位摆好，且每条配合都用 `align=closest`
⇒ **正确的配合不会挪动任何组件**（配合是"已经满足"的）。
反过来，某条配合**选中了错的面**（六角头各面、同轴圆柱面这类"看起来差不多"的），
SolidWorks 会为了让那个面满足关系**把组件拖走** ⇒ 紧跟着的
`check_component_box` 立刻报出偏差。
⇒ 它同时是"取面约定判别力弱"（②）的**端到端裁判**：
**别在探针上吵，看配合之后组件动没动。**

### ⑧ ★★ 干涉判据分两档：`allow`（白名单）与 `expect`（逐对参照值）

- `allow` = "已声明且合理"的干涉对 ⇒ **可见地放过**（仍列在 `allowed_hits`）。给 `allow` 必须给 `why`。
- `expect` = 逐对**参照值**（`{components:[a,b], volume_mm3, tol_pct}`）
  ⇒ 校验干涉**体积**，并**反向检查**"参照里有、实测没有"。
  **为什么白名单不够**：配合选错面/位置微移会让同一对的干涉**体积**从 9.75 变成别的数，
  白名单照样放行 ⇒ **`allow` 只保证"出现的是已声明的对"，不保证"量没变"。**

★★ 取参照值时**必须按「基名对」聚合**：SolidWorks 会把**同一对组件**的干涉
**拆成多条**。真机实例（参照装配体 减速器装配体.SLDASM）：

```
('13-六角头螺栓 M8x65-6','15-螺母 M8-10-2-1')  拆成 7 条：
    3.6362 + 3.6355×4 + 3.127 + 2.9626 = 24.2678 mm³（这才是这一对的值）
('12-箱盖-1','13-六角头螺栓 M8x65-1')         拆成 6 条：合计 1.2066 mm³/对
```

⇒ 拿**单条**去比会得出"参照里没有这个值"的**假结论**。本次子集装配的参照聚合值：
`箱座↔箱盖 9.7467`、`箱座↔销 55.8311 / 55.8404`（两销）、`螺栓↔螺母 24.2678`、
`箱盖↔螺栓 1.2066/对`。

### ⑨ 附带：取组件实体只有 `IComponent2.GetBody()`

**没有 `GetBody2`**（照 `IModelDoc2.GetBodies2` 想当然会静默得到"组件取不到面"）。
签名以 `sldworks.tlb` 为准：只有 `GetBody` / `IGetBody`。`IInterference` 也一样 ——
**没有 `ITypeInfo`**，`GetComponents()` 在本版**不存在**，属性名是 `Components`。

### ⑩ 取面时**必须限定组件**（`component` 字段）

判据是"点到面距离最小且 ≤ 容差"。**螺栓杆 Ø8 外圆与它穿过的 Ø9 孔壁只差 0.5mm** ⇒
只按坐标找，两个面几乎并列，选哪个看**枚举顺序** ⇒ **静默选错组件**，
而报出来的是"配合建上了"（选错面照样能建配合，只是配合的是错的东西）。
⇒ 紧固件类配合（螺栓↔孔、垫圈↔杆、螺母↔杆）一律写 `"component"`。

### ⑪ ★★★ 同心/同轴配合的取点必须避开**螺纹面** —— 真螺纹件先 dump「光杆段」范围

**现象**：`kind=concentric`（弹簧垫圈内孔 ↔ 螺栓杆）报

```
CreateMate 没有产生任何新特征（配合没建上）。kind=concentric，常量 swMateCONCENTRIC=1
```

**加了 3 次重试仍失败**（所以它不是 COM 瞬时失败）。取证 `picked` 给出关键数字：

```
组件 W65A-1 的面，距点 0.000mm      ← 垫圈内孔，正常
组件 B65A-1 的面，距点 0.346mm      ← ★ 螺栓那个点离面有 0.346mm，不是 0
```

**dump 螺栓面数据**（`fa/13-六角头螺栓 M8x65.json`）才看清：

| 面 | kind | 局部 z 范围 |
|---|---|---|
| 0 | `cyl` r=4.0（光杆 Ø8） | 被螺纹**截断**，实际 [5.6, 53.7] |
| 19/20/21 | `ident4006`（**未识别曲面 = 螺纹/扫掠面**） | [53.7, 70.6] |
| 18 | `plane`（螺纹收尾） | [53.73, 54.6] |

即 **该件真的建了螺纹**。而垫圈/螺母所在高度折算到螺栓局部是 **z≈56.6 / 61.1** ⇒
**正好落在螺纹上** ⇒ 取到的是螺纹面 ⇒「圆柱面 ↔ 螺纹面」的同心配合被 SolidWorks
**拒建**，但它报出来的是「没产生新特征 / 两个实体选对了吗」——**完全不提"面选错了"**。

**对照证据**（同一批配合内）：螺栓侧取点在 z=43.1（光杆）时距面 **0.000mm** 且**一次成功**。
两者一比，0.346mm 就是"牙深半量级"的铁证（M8 牙深 0.676mm）。

**修法**：★ **同心配合只要求"同轴"，取点不必落在垫圈/螺母所在的那个高度** ——
一律取**光杆段中部**。计划里把这个区间写死并加 assert 守卫：

```python
SHANK_Z = {"B65": 30.0, "B25": 10.0}
PLAIN_Z = {"B65": (5.6, 53.7), "B25": (5.6, 13.7)}   # 来自 fa/*.json 的 dump
assert PLAIN_Z[k][0] < SHANK_Z[k] < PLAIN_Z[k][1]
```

**通用教训**：
- `kind` 不是 `plane/cyl/cone/sphere/torus` 时要**立刻警觉**（`ident4006` = 未识别曲面，
  本机螺纹面就是它）。
- 报错文案会把人带偏（"没产生新特征"听起来像选面/固定问题）⇒ **必须把
  `picked`（哪个组件的哪个面、距点多少毫米）打进错误信息里**，否则只能靠猜。
  「距点 0.000mm」= 面选对了；「距点零点几毫米」= 选到了邻近的**别的**面。

### ⑫ ★ 重试（`COM_RETRY`）与"取证"必须配套

给 `add_mate` 的「选面 + CreateMate」加了 3 次重试（处置 COM 瞬时失败，
`attempts` 写进返回值）。但 ⑪ 那种**确定性失败**重试 3 次也不会成 ——
所以报错里**同时**给出 `attempts` 和 `picked`：一眼区分
「瞬时失败（重试就好了）」与「面选错了（重试 100 次也没用）」。
**重试不是万能的，重试 + 取证才是。**

### ⑬ 索引缓存的签名要按"物理意义"量化，别按浮点精度

`_asm_face_index` 的缓存键曾用 `round(..., 6)`（米制 = 0.001mm）量化组件包围盒。
真机后果：同一条配合前后读数有**亚微米级浮点噪声** ⇒ 签名几乎每次都变 ⇒
索引被反复重建（单次 ~40s），41 条配合白花二十多分钟。
⇒ 量子化到 **0.1mm**（`round(...,4)`）。取舍：组件只挪动 <0.05mm 时会误判"没动"、
复用旧索引（`xf` 有同量级偏差）——**取面容差 3mm，不影响选面**；
且真机判据是「配合后 `check_component_box` 全零位移」，失误会在那里暴露。
**量化粒度要跟着"判据的容差"走，不是跟着数据类型走。**

### ⑭ ★★ 同一个孔，换个**周向角**最近面就从「圆柱」变成「螺纹」—— 取点必须验"最近面是谁 + 余量"

螺母内孔壁上**既有圆柱面又有真螺纹面**（`Identity=4006` 未识别曲面，见 ⑪）。
取点 `local=(4.032, 2.116, z=-3.4)`（相对孔心 (-1.47,2.115) 是 θ=0、r=5.5）：
在**承压平面**选点是对的，但**内孔**选点在同一高度换 θ 结果完全不同：

| 取点（螺母局部） | 最近面 | 距点 |
|---|---|---|
| θ=0  r=4.0 `[2.532, 2.116, 0]` | `ident4006`（**螺纹**） | 0.303mm |
| 同上 | 圆柱面 | 0.350mm ⇒ **螺纹略近 ⇒ 取到螺纹 ⇒ 同心被拒** |
| θ=180 r=4.0 `[-5.47, 2.115, 0]` | **圆柱面** | **0.002mm**（其余面 ≥0.1mm）⇒ 无歧义 |

⇒ 报出来的是「CreateMate 没有产生任何新特征」，**完全看不出是"取到了螺纹"**。
**通用做法**：紧固件取点后**报出"最近面类型 + 距点"**，并要求
① 最近面**就是**目标面类型、② 与次近面有**可辨余量**（不能是 0.303 vs 0.350 这种并列）。
`asm_face_probe` / `add_mate` 的 `picked` 就是干这个的（⑪⑫）。

### ⑮ ★★★ 源装配体里「组件本身座偏」⇒ 同心配合把它**拖走**，而由**参照 xform** 算的取点随即失效

真机现场（run6）：`m14_D`（同心：螺母内孔 ↔ 螺栓杆）**建上了**，但把**螺母 D** 从
参照位置拖走 **1.08mm**（实测包围盒偏差 `(-0.679, 0, +0.844)`，其余 21 件全 <0.002）。
后果：后一步 `m15_D` 的取点是用**参照 xform** 算的（`gp()`）⇒ 映射到螺母 D 的**新**
局部坐标 `(5.112, 2.041, -3.4)` ⇒ 落到孔口倒角锥面（0.306）上，**略近于**承压平面
（0.353）⇒ 重合配合被拒。

**怎么坐实的**（两条独立证据）：
1. **离线轴线解析式**：算「螺栓杆轴 − 螺母孔轴」的**垂直分量** δ⊥ =
   `(-0.6762, 0, +0.8424)`mm，与真机实测漂移**吻合到 0.003mm** ⇒ 是**参照数据本身**
   就偏（源文件的装配误差），不是配合算错；
2. **对照 21 件**：只有螺母 D 偏 ⇒ 排除"约定/全局坐标"问题。

**修法（要点：让"摆位"和"取点"用同一个位姿）**：把螺母 D 的参照位姿**预修正** δ⊥
（`PLACE_FIX`），于是
- ① `m14_D` 变成"已经满足"⇒ **不再拖拽组件** ⇒ ⑰ 的端到端判据保持有效；
- ② `m15_D` 取点回到安全区（离倒角 0.727mm，与其余 5 件一致）；
- ③ `box_/box2_` 用**修正后**的期望盒，22 件仍可零位移核对。

★★ **最容易漏的一步**：`xf()`（摆位）修了，`gp()`（取点）**也必须**叠加同一个
`PLACE_FIX` —— 否则"把组件摆到 A 处、却按旧位置 B 处取点"，**照样错**。
**判据**：用离线 `to_local(修正后 xform, 计划取点)` 反算回局部坐标，必须等于预期的
局部坐标（本例 6 件螺母的 `m14` 都回 `[-5.47,2.115,0]`、`m15` 都回 `[4.032,2.116,-3.4]`）。
**改完位姿口径一定要这样反算一遍**，别信"我两处都改了"。

### ⑯ ★★★ 会话退化的一种新形态：`AddComponent5` 对**磁盘路径**静默回 None（已打开的零件却正常）

**现象**（2026-10-05，装配计划卡在第 2 步）：`insert_component` 对**任何**零件
（中文路径 / `D:\out\` ASCII 路径都试过）**一律**失败 —— 不抛异常、无错误码、
组件数 `0→0`；**重试 3 次、3 种 ConfigOption、显式给配置名（`默认`/`Default`）全无效**；
而同一时刻 `OpenDoc6` 打开**同一个文件**完全正常，`NewDocument`/`GetComponentCount`
也都正常。**重启 SolidWorks 也没好**。

**干净态取证**（先关掉所有文档）：
```
new_assembly → 插「未打开」的件            FAIL（0→0）
open 该件    → 再插                       FAIL（活动文档变成零件）
open 全部零件 → new_assembly → 逐个插      全部 OK（0→1→…→7）
```
⇒ 当时这条会话**只认已在会话里打开的零件**。

★★ **但它会自愈**：约半小时后（跑成功一轮装配之后）再测**同一台机器同一份代码**，
磁盘路径插入**又正常了**。⇒ 它**不是**"本机不支持"，而是**会话退化**的一个新变种
（与 `sw_session_check.py` 头注的 2026-10-03「AddComponent5 一律静默回 None」同族，
但**只影响磁盘路径**、且能恢复）。

**两条对策（都保留）**：
1. **稳健绕过**：把要插的零件**先 `open` 进会话，再 `new_assembly`，再插**。
   `open` 会切换活动文档，所以必须放在 `new_assembly` **之前**（放在插之前会让活动
   文档变成零件 ⇒ 报"当前文档不是装配体"）。
   ★ `sw_plan` 的文档指纹守卫（`_DOC_PIN_OPS` 含 `open`）**不会误伤**：每个 `open`
   之后指纹就更新成那个零件，下一步核对的是**上一个** open 的结果 ⇒ 不判漂移。
2. **体检**：`sw_session_check.py` 原来的「插 2 个组件」插的是**自己刚建、还开着**的
   零件 ⇒ 这种故障下**仍报 HEALTHY（假绿）**。已新增一项
   「插『未打开』的零件（磁盘路径）」+ `insert_from_disk` 能力位。
   **体检不覆盖"更严的那条路径"= 体检本身成了假绿灯**（同 §102）。

### ⑰ ★★ `IComponent2.Name2` 的改名**不随保存持久化** —— 会话内有效、重开即还原

**现象**：计划把 22 个组件都改成唯一名（`BASE` / `COVER` / `B65A` …），**run 内**
`v_tree` 读回**确实**是这些名字；但 `save_as` 落盘后**重新打开**，名字**全变回默认**
（`1-箱座-2` / `13-六角头螺栓 M8x65-5` / `15-螺母 M8-10-2-10` …）。

**决定性实验**（`probe_rename_persist.py`）：
```
打开成品            → 组件名 14-弹簧垫圈 8-压缩-1
rename → ZZTEST     → 读回 ZZTEST-1（成功）
save_as 到临时文件 → 关闭 → 重新打开
                    → 组件名回到 1-箱座-1（**改名没了**）
```

**影响面（必须分清）**：
- **配合不受影响**：重开成品仍是 **22 组件 / 41 配合** —— 配合用的是
  `PersistentReferenceID`（对组件实例），**不靠名字**。✅
- **改名仍然必须做**：会话内 `add_mate` 的 `component=` 限定**靠它**（同名多实例下
  按路径限定会报"有歧义"、按坐标限定会静默选错面 —— 见 §120⑩）。
- ⇒ 一句话：**改名是"会话内的脚手架"，不是"交付物的一部分"**。别在交付说明里
  声称"文件里的组件叫 BASE/COVER"。

**要做到"名字真进文件"**：`Name2` 这条路不行（本机实测只影响会话）。得走
FeatureManager 里改组件实例名的那条 API，或接受默认名。**别把"读回 = 新名"
当成"已持久化"** —— 读回只证明会话内改了。

---

## §121 ★★★ 整机装配（58 组件 / 120 配合）："低 CPU 无进展" **不是卡死**，是 COM 编组慢（2026-10-06）

### ① 教训：我凭 CPU≈0 连杀 **3 个正常运行**的 run（白丢 ~2 小时）
- 现场：run 跑着跑着**日志不再前进**；实测 `SLDWORKS` CPU 增量 **0.3s/25s**、`python` **0.0s/20s**；
  `SendMessageTimeout(WM_NULL)` 返回 **1**、`IsHungAppWindow=False`（SW 界面活着）；
  顶层**可见窗口只有主框架**（无模态对话框）；进程仍是 running。⇒ 我判定"COM 死锁"，杀了。
- **真相**：SolidWorks COM 编组 + pywin32 `dynamic.Dispatch` 包装都是**"等待型"调用**
  —— 单次几毫秒~几十毫秒的**纯等待，CPU 几乎不涨**；一步里有上千次这种调用 ⇒
  一步几分钟、CPU 仍 ≈0。**"CPU≈0" 完全不能当挂起证据。**
- 判据（按可靠度排序）：
  1. **`faulthandler` 看门狗（决定性）**：`faulthandler.dump_traceback_later(45, repeat=True)`。
     若连续多次打出的栈**是同一帧**、**且**期间**没有新日志行** ⇒ 真卡；
     若栈在变 / 日志在长 ⇒ **只是慢**。（包装器 `_diag/run_fh.py`。）
  2. `SendMessageTimeout(hwnd, WM_NULL, 0,0, SMTO_ABORTIFHUNG, 3000)` 非 0 ⇒ SW 界面没卡
     （但**推不出**"自动化调用没在跑" —— 消息泵活着 ≠ API 线程在动）。
  3. 顶层可见窗口只有主框架（无对话框）⇒ 排除"弹窗阻塞"。
- **另一类"假挂起"**：后台任务把 stdout 接**管道**、消费端不持续读 ⇒ 写满缓冲后进程
  **阻塞在 `write`**（同样"无 CPU、无进展"）。⇒ **长跑一律 `> 文件 2>&1`**，用 `tail` 看进度。

### ② 真正的大头：**每个 op 都整树枚举**（`feature_tree` / `mate_features` / `components`）
- `feature_tree()` **递归进每个组件的子特征**（58 组件 ⇒ 数千节点 × 每节点 3 次 COM）。
- ★ 关键陷阱：`mate_features()` / `mate_count()` **原本就是拿 `feature_tree()` 实现的**
  （取的是浅层但遍历的仍是整棵树）。而 `op_add_mate` 每条配合要调 `mate_count()`
  **3~4 次**（mates0 / mates1 / 复查 / unknown）+ `feature_tree()` **2 次**。
- 实测单条配合 **311s**；120 条 ⇒ **10 小时+**（且低 CPU，看着像卡死）。
- **修法（已落地）**：
  · `SwSession.mate_features()` 改**浅遍历** —— 只扫**顶层特征**找「配合」文件夹，取
    **直接子节点**（递归嵌套文件夹），**绝不下探组件**；语义不变（仍是 MateGroup 直接子节点）。
  · `op_add_mate` 热路径**移除** `mates0/mates1` 复查、`unknown_mate_features()`、
    `out["mate_count"]`。单步只留 **`CreateMate` 返回的特征名 + `GetErrorCode`** 当"建上了没"，
    以及**前后各一次 `component_boxes()`**（58 件 ≈ 0.3s 空闲时）当 `moved`。
  · **端到端判据一个不少**：「多没多一条配合」→ 计划末端 `check_mate_count`；
    「挪没挪错」→ 配合后 `box2_*` **全量站位校验**。
- 结果：单条配合 **311s → 26s**（首条 164s 含一次 `_asm_faces` 索引构建）；
  整机 453 步从"10 小时+"降到 ~1 小时。
- **一般规律**：O(全树 / 全组件) 的枚举**必须移出每步热路径**；验证要"**末端一次性**"，
  别"每步都做"（单步只留便宜证据）。

### ③ 复刻参照装配体 144 条配合的口径
- `add_mate` 只支持 **coincident / concentric / parallel** ⇒ 参照里 tangent/gear/width
  （要边 / 4 实体）与**打到装配体基准面**的配合**跳过**（站位另由 `ArrayData` + `box_*` 保证）。
- **弱约束件**（螺塞、3×螺钉 M3×22、两密封圈）：参照里只有相切 / 装配体基准面 ⇒
  **自造** concentric + coincident 补充。
- **干涉白名单**：从 `ref_interf_pairs.json` 的 no_coincident **聚合种类对自动展开**成
  逐实例 `allow`（别手写）；配套 `expect` 声明"量"。

### ④ 复现 / 取证工具（已落 `D:/out/_diag/`，可复用）
- `run_fh.py` 看门狗运行器（长跑必用）；`probe_boxhang.py` 只读探针（挂起时对着**已开文档**几秒复现）；
- `_ping_sw.py`（`SendMessageTimeout` + `IsHungAppWindow`）、`_winlist.py` / `_viswin.py`
  （枚举 SW 进程窗口找对话框，`EnumWindows` + `pid` 过滤）、`_shot.py`（PIL `ImageGrab` 截屏看真实界面）。

## §122 ★★★ 中止即丢成果 + 退出即挂死：自动化客户端驱动的长任务必须"存盘前置 + 抢救 + 硬退出"（2026-10-07）

整机装配 run13（58 组件/120 配合）在 **step 384 硬失败中止**后，暴露了两个**互相掩盖**的问题，
合起来让 **43 分钟的建模成果无声消失**、进程还挂了 3 小时不退出。

### ① 症状
- run13 日志停在 `失败于 s322c_S322A（add_mate）`，**进程之后 3 小时不退出**：
  CPU 不涨（≈0）、日志不再增长、**faulthandler 也不再打点**（说明主线程持着 GIL 卡在 C/COM 调用里，
  看门狗线程拿不到 GIL 打印）；后台任务因此**永远不结束**（在任务系统里显示为 failed）。
- 复查 SolidWorks：`GetDocuments` 返回 **[]** —— 那份 80 条配合的装配体**没了**（没保存）。

### ② 根因（两层，缺一条都不会这么惨）
1. **中止路径丢弃成果** —— 直觉上 `execute()` 的 `finally: if not keep_open: sw.close()`
   在 `keep_open=True`（默认）时**不关文档**，看着"留在 SW 里很安全"。**错**：
   `sw_plan` 是 SolidWorks 的**自动化客户端**；本进程一退出、COM 引用释放，
   **由本进程拉起的 SolidWorks 会随之退出**，内存里那份"特征都建好了、只是还没走到
   `save_as`"的模型**直接消失**。⇒ **"文档还开着"≠"成果保住了"。**
2. **退出挂死** —— `main()` 走完抛 `SystemExit` ⇒ 进入**解释器退出流程** ⇒
   pywin32 要逐个释放**上千个** COM 引用 ⇒ **挂死**（无 CPU、无日志、GIL 被占）。
   ⇒ 长跑进程"跑完了但不结束"，在 CI / 后台任务里等价于**失败**。

### ③ 修法（三条，已落地）
1. **存盘前置**（计划侧）：把 `save_as` 放到**校验之前**。因为 `check_component_box`
   **超差会抛异常中止**（`op_check_component_box` 走 `SwError`）⇒ 若把 save 放末尾，
   一条站位超差就丢掉**全部**建模成果。先存一次，随后任何校验失败都只需**补跑校验那几步**。
2. **`--salvage-out` 中止抢救**（`sw_plan.py` 新增）：硬失败时**先把当前文档存盘再收手**；
   未显式给路径时**自动派生** = 计划里最后一个 `save_as` 的路径 + `_salvage` 后缀
   （`_auto_salvage_path(plan, ctx)`，用 `sw_ops._p` 解析 `{{ params.out }}`）。
   这是**唯一**能对抗"SW 随客户端退出"的办法 —— **存盘必须抢在进程退出之前**。
3. **运行器 `os._exit()`**（`_diag/run_fh.py`）：抓 `SystemExit` 后 `os._exit(code)`
   跳过 atexit / GC / COM 拆除，直接结束进程。
   **代价**：SW 会随最后一个自动化客户端断开而退出 ⇒ 所以第 1、2 条是它的前提。

### ④ 一般规律（跨项目）
**任何"由自动化客户端驱动的长任务"都要假设：客户端一死，目标应用就带着未保存状态一起消失。**
⇒ ① 关键节点**显式存盘**（别只在末尾存一次）；② 中止路径**抢救存盘**；③ 收尾**硬退出**绕过 COM 拆除。
另外：**"期望值写死"的校验在有 optional 步骤的计划里会必然失败** ——
`check_mate_count` 原写 `expect=MATE_N`(120)，而 ref*/L4 共 **79 条是 optional**、
真机上跳过是**预期行为** ⇒ 实际数必然 <120 ⇒ 校验必红 ⇒ 触发中止 ⇒ 再叠加 §①② 丢成果。
**改用 `min=<必建下限>`**（本例 41 = 主体定位/紧固），把"允许跳过"和"必须建成"分开表达；
全量清单仍在报告的 `result.mates` 里，可逐条与定义对账。

### ⑤ 本次取证工具（可复用）
- `_diag/analyze_run13.py`：解析步骤行（`[i/N] id op STATUS 秒s`），按 **id 前缀聚合**
  总数/OK/SKIP/FAIL/总耗时/均耗时 + 最慢 N 步 + 所有 FAIL。
  ⇒ 一眼看出"真正耗时的是主体 41 条配合（21.7min）、ref* 72 条只 6.9min"，
  和"中止点 = 唯一一条 FAIL"。（正则要写 `\[(\d+)/(\d+)\]\s+(\S+)\s+(\S+)\s+(OK|FAIL|SKIP...)`
  —— 步骤行**行首有缩进空格**，`^\[` 匹配不到。）

### ⑥ 顺带钉死的一条几何事实：`s322c` 在**几何上必然失败**（不是选面 bug）
小盖只有 **7mm 厚** ⇒ 螺钉实体点局部 x 必须 ∈ `[-15.4, -8.4]`（否则离线自检报越界）；
而该区间内 **shank 全长都是真螺纹面** —— 真机 dump（`fa2/5-螺钉M3x22.json`）显示
`ident4006` 三块覆盖 `x∈[-12.48, 9.44]`、面积 121.5+46.6+122.3 mm²，仅剩一条 **2.7mm**
的浅光杆（face0, r=1.5, area 25.3mm²）⇒ 取点**必中螺纹面**。
**螺纹面没有可用的旋转轴 ⇒ 同心配合无解。** ⇒ 该条正确处置是**标 `optional` 允许跳过**
（它是"自造补充"，参照装配体里本来就没有），而不是反复调点。
> 教训：**"该配合建不上"要先分"几何无解"和"选面选错"** —— 前者改判据（optional），
> 后者才改点。分不清时按 §111① 先 dump 真实面数据。

## §123 ★★ 改了生产代码的遍历路径 ⇒ **必须重跑全量回归**：假件的保真度是"局部"的（2026-10-07）

一次性能大修（§121 把 `mate_features()` 从"整树"改成"浅遍历"）后，**离线回归一跑就红两处**，
而且都是"真机已经跑通、所以看不出问题"的那种。

### ① 新路径会依赖**假件没实现**的真 API（最难自查）
- `mate_features()` 浅遍历直接用 **`GetNextSubFeature`** 走同层子特征链。而假件 `_FxFeat`
  **没有实现这个方法**（真 `IFeature` 一定有）。
- **为什么以前一直没暴露**：旧的 `feature_tree()` 在子特征循环里是
  `walk(sub); sub = GetNextSubFeature(sub)` —— `walk()` **自己的 while 循环用
  `GetNextFeature` 推进**，于是**兄弟链是靠 `GetNextFeature` 兜出来的**，
  `GetNextSubFeature` 一旦抛异常就被 `suppress` 吞掉、**看起来一切正常**。
  ⇒ 假件的缺失被**另一条代码路径**掩盖了。
- ⇒ **规律**：假件（test double）的保真度是**按"已有加载路径"局部成立的**。
  你新加一条遍历/取值路径，就可能踩到假件从来没被要求实现的方法。
  **只有全量回归能在离线阶段抓到它**（真机跑通**不能**代替 —— 真机上那个方法存在）。
- **两手修**：(a) 生产代码里把子特征链抽成 `_sub_chain()` **统一容错**（`_FxFeat`/退化对象
  没有这两个成员是**正常**情况，取到哪算哪、绝不往上抛）—— 这与 `feature_tree` 在 v1.4.42
  修过的是**同一条坑**，说明它值得写成公共工具而不是各写一份；
  (b) **把假件补真**（实现 `GetNextSubFeature`），假件才等价于 `IFeature`。

### ② `_call` 的"双层包装"会在**返回形状不同**的对象上静默误判
- `op_add_mate` 写 `feat, err = _call(member, asm, "CreateMate", data)`。`_call` 自己已经
  返回 `(结果, 错误串)`；若 `CreateMate` **本身**又返回 `(IFeature, errcode)` 二元组，
  就变成 `((IFeature, errcode), None)` ⇒ `feat` 是 **tuple** ⇒ 后面
  `member(feat, "Name")` 抛异常被 `suppress` 吞掉 ⇒ `fresh` 空 ⇒ **判成"没建上"并白重试 3 次**。
- **表现极具误导性**：真机 run13 **建成了 80 条配合**（真 API 回单个 `IFeature`），
  离线假件却**全红**。看日志只会以为"假件坏了"，其实是**生产代码没兼容第二种返回形状**。
- ⇒ 修法：`_res, err = _call(...)`；`feat = _res[0] if isinstance(_res, tuple) else _res`。
  ⇒ **规律**：凡是"`_call` + 多返回"的组合，都要**显式拆一层**；判据不要建立在
  "我以为它返回什么形状"上（§五条铁律①"返回值会说谎"的另一种形态）。
- ★ 顺带：**断言要跟着新口径走**。`op_add_mate` 不再回报 `mate_count`（§121 把它移出热路径）
  ⇒ 回归里两处 `r["mate_count"]` 断言要改成"`CreateMate` 回了新特征名 + 会话侧数得 1"。
  **改了接口就同步改断言，否则回归会红在"过期的期望"上**（红得没意义 = 告警疲劳）。

## §124 ★★★ 复刻配合「建成功」≠「建对了」：一条选错面会**级联**污染后续所有取点（2026-10-07）

整机装配 run14（58 组件 / 120 配合 / 453 步）一路 `add_mate OK 计数 = 80、FAIL = 0`，
却在**最后一条**站位校验 `box2_EYECOV` 上硬失败：
`组件 'EYECOV-1' 站位校验失败：包围盒差 [34.0, -22.6506, -3.9937, -9.0, 19.6954, 3.4732]`。

### ① 先问「到底有几条配合挪了组件」，别只看失败那一件
`op_add_mate` 的返回里本来就有 **`moved`** 字段（建配合前后各读一次 `component_boxes`）。
把 434 条日志按 `moved_max_mm > 0.6` 滤一遍 ⇒ **只有 3 条**：

| 步骤 | 文件里的配合名 | 被拖走的组件 | before → after |
|---|---|---|---|
| `ref30` | `复刻30-重合34`（箱盖 ↔ 垫片2） | `7-垫片2-1` | `[-23,101.51,91.24,23,111.47,136.89]` → `[14,65.07,84.82,16,118.36,138.10]`（**37.0mm**） |
| `ref32` | `复刻32-重合36`（垫片2 ↔ 窥视孔盖） | `8-窥视孔盖-1` | `[-23,103.48,91.59,23,114.42,137.41]` → `[11,80.83,87.59,14,134.12,140.88]`（**34.0mm**） |
| `plug_c` | `同心螺塞-座放油孔` | `16-螺塞-1` | 0.75mm（同心吸附） |

⇒ **`moved` 不是日志，是判据**（§120① 「正确配合不挪组件，选错面会拖走」的可执行版）。

### ② ★ 根因链：**一条配合的错误会改掉后续所有「按坐标取面」的语义**
- `ref30`（箱盖 ↔ 垫片2）先选错面 ⇒ 把**垫片2**转走。垫片2 的 AABB 从 `[46, 9.96, 45.65]`
  （y 向法向）变成 **`[2, 53.289, 53.289]`（x 向法向）**。
- `ref32` 的取点是 `SHP2` 的 `[14, 107.4739, 114.2387]` —— 在**原位**上它落在垫片2 内部，
  但垫片2 已被拖成 x∈[14,16] ⇒ 这个点**恰好落在新的 x=14 侧面上** ⇒ `_select_asm_face_at`
  命中**另一个面**（距离 0.000、报告完全"正常"） ⇒ coincident 把**窥视孔盖也转了 90°**。
- ⇒ **后面所有按全局坐标取面的步骤，都建立在"前面没有把组件拖走"之上。**
  上游一条拖走 ⇒ 下游一批取点集体失效，而报出来的却是"点落在所有组件局部盒之外 / 没命中"
  —— **指错方向**（§asm_face_probe 头注同一条）。
- **判读经验**：垫片2 与窥视孔盖被贴成**共面**（两者 AABB 都成 `[t, 53.289, 53.289]`），
  看上去"配合很成功"。**AABB 尺寸与参照对不上（哪怕只是旋转）就是姿态错，不是"配合到位"。**
  （本例 `53.289 > 46`：旋转后的 AABB 边长**可以**超过零件自身的最大边长 —— 上界是空间对角线
  `sqrt(46²+9.96²+45.65²)=65.5`，别拿"AABB 不可能变大"去否掉测量值。）

### ③ 处置：删配合 + 摆回 —— **顺序不能反**
只把 `set_component_transform` 重放回去**没用**：配合还在，**rebuild 时求解器会再拖一次**。
必须先删配合、再摆位。为此新增 op **`delete_feature(name, kind)`**：
- 按名删特征/配合；`kind='mate'` 限定只在顶层「配合」文件夹里找；
- 判据 = **删完重新遍历特征树，名字必须真的没了**（不信 `EditDelete` 的返回值，§五条铁律①）；
- 只下探「配合」文件夹、**不下探组件**（§121 热路径毒药）。

★ **坑：`CreateMate` 回的名字 ≠ 文件里的名字。** 当场它回的是**自动名**（`重合30`），
而 `op_add_mate` 随后会把配合**改名为计划里 `name` 指定的名字**（`复刻30-重合34`）
⇒ **存盘后按自动名去删必然"找不到"**。删之前先用 `mate_features()` 问真名
（op 的报错信息里就顺手打了前 8 个配合名 —— 报错要能自证）。

### ④ 为什么可以不重跑整机：损坏**局部且可逆**
- 摆位阶段 58 件的 `box_*` **全部**与参照汇编的期望盒吻合 ⇒ 几何一开始是对的；
- 只有 3 条配合挪了组件 ⇒ 删掉它们、把 3 件摆回去就回到与参照一致的状态；
- 而 `rename_component` / `set_component_transform` 两步的**源名都是零件默认名**
  （重开后 `Name2` **回退成默认名**，§120⑰）⇒ 这两类步骤可以**逐字重放**。
⇒ 外科修补计划 **128 步 / ~15min** 顶掉重跑的 **2.2 小时**（`_diag/gen_repair_plan.py` + `run_repair.py`）。

### ⑤ 一般规律（跨项目）
1. **"建成功"只证明 API 没报错，不证明关系正确** —— 必须配一个独立的几何判据（这里是 `moved`）。
2. **有依赖顺序的批量作业里，任何一步的副作用都要当成后续步骤的前置条件**（取点尤其敏感）。
3. **能"撤销 + 重放"就别"整体重跑"** —— 前提是幂等步骤（绝对量写入，如 `ArrayData`）与
   可跳过的验证步骤要分开设计。设计计划时先问：**这一步能不能单独重放？**

## §125 ★★ `ShowNamedView2(Name, ViewId)` 传 -1 **不报错但不切视图**（2026-10-07）

给整机装配体渲染 4 视图（iso / front / right / top）时写
`member(md, "ShowNamedView2", "*Isometric", -1)`，日志 4 行全是 ✔，
**4 张 PNG 逐字节相同**（md5 全等）—— 视图压根没换。

- 根因：`ViewId` 必须是**标准视图 ID**，传 -1 静默无效：
  `1=前视 2=后视 3=左视 4=右视 5=俯视 6=仰视 7=等轴测 8=二测 9=三测`。
- ⇒ **判据要落在产物上**：截图这种"没有可靠返回值可查"的动作，
  判据 = **4 张图的 md5 互不相同** —— `ShowNamedView2` 没抛异常 **≠** 视图切了
  （§五条铁律① "返回值会说谎" 的又一形态）。
- ★ 顺序：**切视图 → `GraphicsRedraw2()` → `ViewZoomtofit2()` → `SaveBMP`**。
  少了 redraw 会抓到切换前的帧。
- 落地：`_diag/render_views2.py` —— 带 **md5 自检**的渲染器（自报 "4 张里互不相同的张数 = n/4"）。
- ★ 顺带一条：报告里嵌图时 glob 要**收紧**（`shots/reducer_assembly_full_*.png`），
  否则会把上一轮（22 件子集）的同名系列图一起嵌进来，报告自相矛盾。

## §126 ★★★ 齿轮副干涉归因：「建法不同」≠「齿形不对」—— 先把**位置**量出来（2026-10-07）

整机装配报齿轮副干涉 241.79 mm³（3 区 133.52 + 91.17 + 17.10）。用户判断
「一体齿轮轴的齿形是用**不同方法**做的（切齿槽+圆周阵列 vs 整廓拉伸）⇒ 重画齿形即可」。
**实测否掉了这个判断**：两齿形都是正确的 m=2 标准渐开线，真因是**装配位置**。

### 判据链（每一步都可独立复算，别跳步）
1. **齿形对不对**：`gear_mesh_sim.py` 用**实体三角网格**取真实 r(θ) 轮廓，
   与标准渐开线对比 ⇒ 大齿轮齿槽面积 13.7617 vs 标准 13.7762（−0.1%）；
   齿轮轴 r(θ) 的 FFT 主谐波 **k=15**（干净 15 齿）。
2. **能否啮合**：真实齿轮轴 × 标准大齿轮 @ a=70 ⇒ 最小干涉 0.0275 mm²（≈0.7mm³/26mm 宽）
   ⇒ 齿形互相兼容，**不是**根因。
3. **位置对不对**：★ 关键。箱座轴承孔轴心在 (Y,Z)=(40,70)/(40,140) ⇒ **70.000 精确**；
   `20-轴` / `19-端盖1` 的 `GetBox` 中心都在 (40,70) ✔；
   但 **`33-齿轮` 的轴心在 (38.66, 71.30)** ⇒ 偏 **1.867mm**。
   ⇒ 齿轮副**有效中心距 = 68.711mm**（应 70.000，差 −1.289）。
4. **量级对不对**：a=68.71 时标准 m=2 副最小干涉 ≈150mm³/26mm 宽 ⇒ 与实测 242mm³ 同量级。

### 三条可复用的硬教训
- ★★★ **SolidWorks `IMathTransform.ArrayData` 的旋转是 `Rᵀ`**：零件向量 v → 全局 = **Rᵀ·v**
  （行向量约定）。**把它当 `R·v` 用，会算出「两平行轴互相垂直」这种荒谬结论**，
  然后一路怀疑数据/API（我就在这上面绕了好几圈）。
  ⇒ 遇到"几何上不可能"的结论，**先换约定重算一遍**，别急着怀疑数据。
- ★★★ `IComponent2.SetTransformAndSolve3(xf)` **少传第 2 个参数（`this_config: bool`）
  会让 COM 调用失败** —— 被 `contextlib.suppress` 吞掉 ⇒ 现象是"**组件死活不动**"，
  极易误判成"配合锁死/API 不支持"。正确：`SetTransformAndSolve3(xf, True)`（返回 True）。
  ★ 且**判据只能是"读回 `GetBox` 真变了"**，不是返回值（§五条铁律①）。
  ★ 组件被配合约束时，即便调用成功、`ArrayData` 读回也改了，**求解器仍会把它弹回原位** ⇒
  "搬不动"= 它被配合管着，下一步该去查**配合**，不是继续调 API。
- ★ `shutil.copyfile(src, dst)` 在 **SolidWorks 已打开 dst** 时报 `PermissionError` —
  先 `sw.app.CloseAllDocuments(True)` 再复制（探针脚本很容易踩）。
- 归因方法学：**"某个零件自身是否同心"要单独量**——在**零件坐标系**里分别拟合
  齿顶圆中心与中心孔中心。大齿轮实测齿顶圆中心离零件原点 0.0012mm ⇒ 零件自身同心，
  责任在装配体。（这一步把"零件缺陷"和"装配错位"彻底分开。）
- 参考装配体里大齿轮的定位配合 = `同心4`(20-轴↔33-齿轮) + `重合5` + `重合9`，
  另有 `宽度1/2`(MateWidth) 与 `齿轮配合4`(MateGearDim) —— **后两者本 skill 的
  `add_mate` 不支持**（只支持 coincident/concentric/parallel）⇒ 复刻时会跳过。
  查参考配合用 `_diag/asm_ref_mates.json`（144 条，含 comp/face/global 点）。

## §127 ★★★ 齿轮副不能啮合：真因是**零件自身的孔偏心**，且**中心距与相位是两个独立因子**（2026-10-08）

§126 把责任指到"装配体里大齿轮偏轴 1.867mm"。**继续往下挖一层发现：偏轴不是装配造成的，
是零件 `33-齿轮-m=2,z=55.SLDPRT` 自己带的** —— 齿圈中心在零件原点 `(0,0)`，而
**Ø32 中心孔（`草图3`，切除-拉伸1 用的轮廓）的圆心画在 `(-0.2208,-1.8552)`**，
偏了 **1.8552mm**。装上 Ø32 轴颈后，孔套轴 ⇒ 齿圈被顶离轴心 ⇒ 有效中心距 68.711（应 70）。
★ 判据：把**参考装配体**（`减速器装配体.SLDASM`）也量一遍 ⇒ 中心距 **68.7113**、
齿轮副干涉 **241.7928mm³ 逐位相同** ⇒ **缺陷在零件本身**，与我方装配无关（这条排除法最省事）。

### 修复配方（`_diag/redo_wheel_fix.py`）：平移孔 + 绕孔心转键槽
```python
# ① 平移（4 参！单位米）：把选中图元从 (StartX,StartY) 移到 (EndX,EndY)
member(md, "SketchModifyTranslate", 0.0, 0.0, DX, DY)          # DX,DY = 0.0002208, 0.0018552
# ② 旋转（3 参）：Center 是**草图系**坐标（米），Angle 是**弧度**，逆时针为正
member(md, "SketchModifyRotate", CX, CY, math.radians(-2.1818))  # CX,CY = -0.0002208,-0.0018552
```
- ★★★ **`SketchModifyTranslate` 会把草图的坐标系一起挪走**（草图内坐标**读回不变**），
  所以紧接的 `SketchModifyRotate` **必须绕"平移前的草图坐标里的孔心"** `(-0.2208,-1.8552)` 转，
  写 `(0,0)` 是绕**旧孔心**转 ⇒ 孔被甩偏 **0.0711mm**（第一次修就中招，只能从备份重做）。
  ⇒ 顺序不能换：**先平移、再按原坐标绕孔心旋转**。
- 签名是从 `sldworks.tlb` 读出来的（`_diag/tlb_probe.py`）：`SketchModifyTranslate` **4 参**、
  `SketchModifyRotate` **3 参**；`InsertSketch` 退出用 `sw.skm.InsertSketch(True)` 或 `md.InsertSketch()`（**0 参**）。
- ★★ **草图图形读回是"旧值"**：转完草图后 `ISketchSegment.GetCenterPoint2` 仍是 `(-0.2208,-1.8552)`，
  但**磁盘文件已经变了**。⇒ **判据只能落在模型面上**（见下），**绝不能信草图读回**。
- 判据（都落在**模型**上）：
  - `ISurface.CylinderParams` = **[原点(3), 轴向(3), 半径]**（**半径在最后**）
    ⇒ Ø32 面孔轴心 `|offset| < 0.005mm`；
  - `ISurface.PlaneParams` = **[法线(3), 原点(3)]**（**法线在前**！）
    ⇒ 键槽侧壁法线角 ≈ `-2.1818°(mod 180)`。★ 读 `PlaneParams` 时把 `pp[2]`（法线 z 分量）
    当半径/原点用过一次，全废；判定水平键槽面用 `abs(pp[2])<0.02`（法线在 XY 平面内）。

### ★★★ 最贵的一条：**中心距对了 ≠ 能啮合**，还要对**相位**
- 只修孔（中心距 68.711→70.000）后干涉**反而从 241.79 升到 323.09mm³**。
  ★ 这**不是"修坏了"**，而是**相位（齿对齿）错**被放大：修孔前"偏轴"这个错误
  恰好让齿在某个位置"错开了一点"；孔归位后齿顶齿、全齿干涉。
  ⇒ **看到干涉变大先别回滚**，分因子查：`抑制角向配合 → 绕轴扫 δ`（`phase_scan_free.py`）
  ⇒ 本例 δ=2.182° 时降到 3.08mm³ ⇒ 确认是相位 ⇒ 把键槽草图转 -2.1818°。
- **最终结果**：中心距 **70.0000**、齿轮副干涉 **0.499mm³**（3 处 0.366/0.128/0.005，纯齿侧微隙）。
- ★★★ **独立复核中心距只能量"真实轴线"，不能量"组件原点"**（原点不在轴上的假设会作弊）：
  取两齿轮各自的**圆柱面**（33 用 Ø32 孔 r=16；23 用任一轴颈 r=17），
  `全局 = Rᵀ·(local_mm) + t_mm`，再算两直线的**垂直距离**
  （`_diag/probe_axes_asm.py`）⇒ **70.0000mm**，与组件原点法一致，双证据闭环。
  ⇒ 顺带确认了 §126 的 `Rᵀ` 约定（`ArrayData[0:9]` 行主序，平移 `[9:12]` **单位米**）。

### 判定流程（可复用）
1. 干涉量级 ⇒ 先怀疑**位置**（§126）；2. 位置 ⇒ 先查**中心距**（量**零件自身**孔/轴是否同心）；
3. 中心距对 ⇒ 再查**相位**（抑制角向配合扫 δ）；4. 两者都对 ⇒ 干涉应降到**亚 mm³**量级。
- ★ 反例对照很便宜：**参考装配体量一遍**就能把"零件缺陷"和"装配错位"彻底分开。

## §128 ★★★ 修「平面↔圆柱」退化 coincident：7 条 err=51 一次全绿；两个**会话级**陷阱让整段卡死（2026-10-08）

一级减速器自建装配遗留 **7 条 `err=51` 配合**（`重合27/29/34/35/37/38/39`）。
本轮一次全修：**7 → 0**，中心距 **70.0000** 不变、58 组件**零位移**、齿轮副干涉 **0.499mm³ 不变**。
真正卡住进度的**不是**配合逻辑，而是下面两个"**会话一旦进入这个状态就不能再编辑**"的坑。

### ① ★★★ `InterferenceDetectionManager.GetInterferences()` 会**污染会话** ⇒ 之后 `Select2`/`EditDelete` 全失效

- 现象：在"编辑前基线"里顺手跑了一次干涉检测（想拿齿轮副干涉做基线），
  之后**所有** `IFeature.Select2` 回 `False`、`IModelDoc2.EditDelete` 调了没反应
  —— 删配合**全部静默失败**（回查 `left=True`），日志里只有 `sel=False,cnt=0`。
- 定位（`_diag/delete_probe2.py` 分阶段探针）：同一会话里 A~D 阶段的删除**都成功**，
  **E 阶段（跑过 `GetInterferences()` 之后）起的删除全失败** ⇒ 根因锁定干涉检测。
- ⇒ **干涉检测只能放在会话的最后**。收口脚本 `finalize.py` 的顺序天然正确：
  `判据（含干涉）→ Save3 → 渲染`，**之后不再有任何编辑动作**。
  ⇒ 与 §121 的"低 CPU 无进展"是同一族：**"命令进行中"状态下 API 静默失效**（返回值说谎，§铁律①）。

### ② ★★★ `IComponent2.GetBody().GetFaces()` 返回的 `IFace2` **必须 `typed` 后才能 `Select4`**

- 现象：`member(face, "Select4", True, None)` →
  `pywintypes.com_error: (-2147352571, '类型不匹配。', None, 2)`（`DISP_E_TYPEMISMATCH`），
  报错栈是 `File "<COMObject GetFaces>", line 2, in Select4`。
- 根因：`GetFaces()` 回的是**无类型信息**的 late-binding dispatch；
  `Select4(Append, Callout)` 的 `Callout` 是 `ICallout` 接口指针，
  pywin32 在**没有类型信息**时把 `None` 编组不过去 ⇒ 类型不匹配。
- 修法：`typed(f, "IFace2")`（技能 `sw_ops._asm_face_index` 第 7559 行**早就这么做**了 ——
  我这次是自己另写枚举时漏了这一步）；选面统一走 **`sw_ops._select_obj(face, append)`**
  （内部会 `typed(obj,"IEntity")` 再 `Select4`），**别直接 `member(face,"Select4",...)`**。
- ★ 一般规律：**凡「拿到 COM 子对象 → 立刻调带接口指针参数的方法」都必须先 typed**。
  这条的伪装性在于**它只在装配体枚举路径上出现**，零件层 `_faces()` 早就 typed 过，所以不复发。

### ③ 配方：定位 → 删 → 按**共面平面**重建（`_diag/fix_mates_apply.py`）

**归因**：`err=51` 的字面解释是"外部参考失效"，但**真因是配合实体选错面**：
7 条全是 `MateCoincident` 的**退化配对「平面 ↔ 圆柱面」** —— 平面与圆柱没有"重合面"这种关系
（只有相切/交线）⇒ 几何不可满足 ⇒ 求解器报失效。**参照装配体对应处全是「平面↔平面」**。

**关键观测：错的只有一侧。** 逐条 dump 两个实体后发现 **"保留侧"本身就是平面**（`1-箱座-2`/
`32-调整环2-2`/`3-反光片-2`/`5-螺钉M3x22-*`），被选错的是**另一侧**（`31-端盖4`/`2-垫片1`/`6-小盖`
上本该选平面、却选成了圆柱）。⇒ 修法 = **只换错的那一侧**，去掉侧平面在伙伴件上的**共面平面**。

**四步（每条都"动一条验一条"）**：
1. **找替代面**：伙伴件上枚举平面面，判据 = 法线平行（`|dot| > 0.999`）**且**点到该平面的距离
   `< 0.02mm`。本例 27/29/34/35 唯一解、37/38/39 各 **3 块**共面（同一 `z` 平面被切成 3 个分区面）。
2. **删**：`IFeature.Select2(False, 0)` + `IModelDoc2.EditDelete`，
   判据 = **回查特征树名字真的没了**（返回值会说谎）；COM 会瞬时退化 ⇒ **带重试**（`k1..k4`）。
   ★ `IMateEntity2` **只有 getter、没有 setter** ⇒ **不能改配合实体，只能删了重建**。
3. **重建**：两侧都**重新枚举**面（删完旧指针不可靠 —— 组件没动 ≠ 面指针还在），
   `_select_obj` 各选一个 → `CreateMateData(swMateCOINCIDENT)` → `_mate_entities(o1,o2)`（§坑：
   必须显式 `VT_ARRAY|VT_DISPATCH`）→ `MateAlignment = swMateAlignCLOSEST` → `CreateMate`。
4. **验（三条同时满足才收）**：
   - 删后**零位移**（`component_boxes` 逐件比）；
   - 选中的两个面**到目标无限平面的距离 = 0**（★ 防静默选错面：选错照样建得上，§124）；
   - 新建配合 `GetErrorCode() == 0` **且**重建后**零位移**。

**★ 同一伙伴件上的多条配合要各用一块不同分区面**：37/38/39 都在 `6-小盖` 的同一 `z` 平面上，
三条都引用同一张面 = **冗余配合**（多半能解，但语义不对、也可能报 err）。
用一个 `usage[组件全名] -> 已用几块` 的计数器依次分配（`min(count, len(cands)-1)` 兜底）。

**结果**：`重合27/29/34/35/37/38/39` → `修正27/29/34/35/37/38/39`，全部 `err=0`；
重开装配体独立复核：**失效配合 0 条**、中心距 `70.0000`、干涉 `0.499mm³`、4 视图 md5 `4/4` 互不相同。

### ④ 流程层面（本次省了最多时间的一条）
- **"验证模式"与"落盘模式"分离**：脚本默认**不保存**（全绿才提示"未传 `--apply` ⇒ 不保存"），
  先跑一遍看判据全绿，再 `--apply` 重跑落盘 ⇒ 改错了最多丢一次关档，**不动磁盘**。
- **前置校验不通过就 `os._exit(2)`，一个字节都不改**：7 条里只要有 1 条找不到共面替代面就整体放弃，
  绝不做"改一半"。改之前已有备份（`_backup/reducer_assembly_full_修配合前_*.SLDASM`）。

---

## §129 ★★★ 曲面收口的两个"静默失败"命门：**体特征必须 `mark=1`** + **多目标必须 `append=True`**（2026-10-09）

**背景**：曲面族第一天要打通"曲面 → 实体"（`thicken`）与"多张面缝成一张 / 缝成实体"（`surface_knit`）。
`thicken` **怎么都不成**：不抛异常、返回 `None`、特征树不涨、体数不变、体积不变。
按"换 API 路线"的思路扫了 **23 个变体**，全绿变红：

| 维度 | 扫过的取值 |
|---|---|
| API 路线 | `IFeatureManager.FeatureBossThicken`(7参) / `IModelDoc2.FeatureBossThicken`(3参) / `FeatureBossThicken2`(4参) / `IModeler.ThickenSheet` |
| 选什么 | 曲面特征名 / 全部面 / 单个平面 / `SHEETBODY` / `SURFACEBODY` |
| `Direction` | 0 / 1 / 2（`swThickenDirection_e` Side1/Side2/Both） |
| `FaceIndex` | 0 / 1 / -1 |
| 厚度 | 0.5 / 3 / 20 / 50 mm |
| 其它 | `Merge` T/F、`UseFeatScope`、`UseAutoSelect`、加厚前 `ForceRebuild3` |

**根因（一行，出自官方帮助）**：

> `IFeatureManager.FeatureBossThicken` — **"The surface must be selected with a mark of 1."**

此前一直传 **`mark=0`** 或 **`mark=4`**（后者是宏录制里常见的"特征选择"标记）。
**`mark` 不是可选装饰，是这一步的开关**；传错时 SolidWorks **完全静默**。

- ★ **对照实验证明关键在 mark，不在类型字符串**：`SURFACEBODY` + mark=1 ✅、
  `BODYFEATURE` + mark=1 ✅（两种都成）；mark=0/4 一律失败。
- ★ 官方论坛范例正是：`SelectByID2(body.Name, "SURFACEBODY", 0,0,0, False, 1, Nothing, 0)`。
- ★★★ **同一个坑在缝合上重演**：`IFeatureManager.InsertSewRefSurface` 的官方 Remarks 是
  **同一句话**（"Make selections using `IModelDoc2Extension::SelectByID2` with a **mark number of 1**"）；
  官方论坛上就有人因此一直拿到 `null`。
  ⇒ 归纳成**通用规矩**：**凡「对*体*做特征」的 API，选择一律 `mark=1`。**
  （注意与草图类 op 的既有教训**不冲突**：`_select_feature` 那边说的是"别用 append 切换语义"，
   见其文档；两者管的是不同的事。）

**第二个命门：多目标选择的 `append`**。
`SelectByID2(..., append, ...)` 的 `append=False` 是**清掉已有选择再选**。
多目标时若每个都传 `False`，**只有最后一个留在选择集里** —— 症状与上面一模一样：
**特征建出来了、体数死活不合并、不报任何错**。

- 真机实测（缝合两个**共边**平面）：append 全 `False` ⇒ 曲面体 `2→2`；
  **第二个改 `True`** ⇒ `2→1`。
- ⇒ 统一走 `sw_ops._select_surface_bodies()`（**起头 `False` 清场、其余 `True` 追加**）。

**第三个坑：缝合合并的是「共边」不是「共面」**。
最初用例是"两根共享一个面的方管" —— 缝了**不合并**（特征却建出来了）。
换成"两个**共边**的平面"立刻 `2→1`。
⇒ 判据要选对几何关系：**共面 ≠ 能缝**。已写进 `surface_knit` 的报错提示。

### ④ 方法论（本次最值钱的一条）

**当"换 API 路线"连续多次无效时，要回头查官方 Remarks 里的前置条件，
而不是继续在同一维度上加变体。**
23 个变体全部无效，恰恰说明"问题不在我扫的那几个维度里" ——
真正缺的 `mark=1` 不是参数、不是 API、不是几何，而是**选择语义**，
它在官方帮助的一句话里写着，且**违反时不报错**。
⇒ 判据仍然只能靠**读回**（体数 / 面积 / 体积），这也是本技能一贯的做法。

### ⑤ 顺带验到的硬数字（回归基准）

| 用例 | 手算 | 实测 |
|---|---|---|
| 60×40 矩形曲面拉伸 40 | 周长 200×40 = **8000mm²** | 8000.0（4 个平面） |
| 方管加厚 3mm：side1 / side2 / both | 25440 / 22560 / 48000 mm³ | 逐位吻合 |
| 矩形 x∈[20,30],y∈[0,40] 绕竖轴 360° | **5000π = 15707.9633mm²** | 逐位吻合（4 面 = 2 圆柱 + 上下环面） |
| 两个共边平面缝合 | 2400+1200 = 3600mm²，体 2→1 | 逐位吻合 |

> ★ 旋转那条特别值钱：面积命中 **5000π 而非 4000π** ⇒ 证明"旋转曲面出来的是**闭合壳、带端环面**"。
> 只按侧面估会少 1000π（12566 vs 15708，**差 25%**），是个明确的判据分叉点。


## §130 ★★★ S1 曲线骨架：**读数写错 3 倍** + **活动草图名拿到变换矩阵** + 一个 `verified=True` 的**假绿**（2026-10-09 晚）

S1 目标是把参数方程算出的点列直接喂给 SolidWorks（`curve_xyz` + `check_curve` + `sketch3d_begin/end`）。
四个 op 全部真机双证了，但过程中**每条判据都先写错过一次** —— 下面是逐条真机取证。

### ① 曲线读回的三个硬事实（写错任何一个，判据都会误判）

| 事实 | 我一开始以为 | **真机实测** |
|---|---|---|
| 特征类型名 | `REFERENCECURVES` | **`CurveInFile`**（`GetTypeName2()`；`REFERENCECURVES` 只是 `SelectByID2` 的串） |
| `GetPointCount()` | **点数** | **坐标个数 = 3 × 点数**（13 点回 **39**、3 点回 **9**） |
| `PointArray` 形态/单位 | n 个三元组 | **平铺 3n 个 double**、单位**米** |

首轮探针的报错就是这一条逼出来的，而且报得**很诚实**：
`点数组无法归一化（count=39，raw 类型 tuple）—— 读法不对，别当成『曲线点不对』`。
★ 如果当初判据写成"返回 True 即通过"，这三条**一条也不会被发现**。

**硬数字（回归基准）**：13 点参数化空间曲线（`x=40cos t, y=25sin t, z=10t/π`）
读回 **13 点、最大偏差 0.0mm**；闭合曲线 3 点 → 读回 **4 点**（首尾重合）。
点单位是**米**：官方 VBA 范例 `12.7mm` 写作 `0.0127`，本 op 内部乘 `mm()`。

### ② 活动草图名：**不能套 `typed(..., 'IFeature')`**

```
member(member(sw.model,"GetActiveSketch2"), "Name")            → '3D草图1'   ✅
member(typed(active,"IFeature"), "Name")                        → (1.0,0.0,...,0.0)  ❌ 16 个浮点
member(typed(active,"IFeature"), "GetTypeName2")                → com_error「非选择性的参数」
member(typed(active,"ISketch"),  "GetType")                     → 9   ✅
```
`GetActiveSketch2()` 回的是一条 **ISketch** 的 IDispatch（不是 IFeature）；
套错接口后 pywin32 按**错误的 DISPID** 解析 `Name`，拿到的其实是
`ISketch.ModelToSketchTransform`（**16 个浮点的 4×4 矩阵**）。
⇒ 于是"草图名"变成一个矩阵字符串，后面按这个名字查特征**必然找不到** ——
而症状看起来像"草图是空的"。**根因只是取名字的方式错了。**

### ③ 3D 草图的三个平台事实

1. **树里的类型名**：3D 草图 = **`3DProfileFeature`**，2D 草图才叫 `ProfileFeature`
   ⇒ 用 `ProfileFeature` 去筛**永远筛不到 3D 草图**（`_existing_3d_sketches` 一开始就是这么错的）。
2. **`Insert3DSketch` 会立刻建特征**（不像 2D 草图要等退出）⇒ tree0 必须在**调用之前**记，
   记晚了退出时 diff 就是空的、改不了名。
3. ★★★ **官方契约只承诺**：『Inserts a new 3D sketch **or closes the active sketch**』
   —— **没有**承诺"打开已有草图去编辑"。**同一台机器、同一份代码**实测到过**两种相反行为**：
   一次**打开了已有 3D 草图**（树不涨、活动草图变成它），另一次**新建了第二张**（`3D草图2`）。
   ⇒ 结论：**别依赖它编辑已有草图**；改成**按读回判定，绝不预测** ——
   `mode='new'` 发现进了已有草图就**报错**（否则两批引导线会悄悄并进同一张，且不报错）；
   `mode='edit'` 发现新建了就报错。**建模口径：所有 3D 引导实体放进同一张 3D 草图。**

### ④ ★★★ 一个 `verified=True` 的**假绿**：`op_spline` 漏了 `variant_r8()`

本技能早就记过这条（`sw_connect.variant_r8` 的注释 + 矩阵表 B7）：
`CreateSpline3` 的点表**必须**包成 `VARIANT(VT_ARRAY|VT_R8)`，否则
**不报错、不抛异常、返回非 None，但草图里图元数恒为 0**（「样条静默消失」）。

而 `op_spline` 一直传的是**裸 Python list**，却挂着 `verified=True`。
为什么没人发现：**在 `sketch3d.json` 之前，没有任何探针点名过 `op_spline`**
（`sw_selftest` 只检查"未实测的 op 必须有探针"，**已标 verified 的 op 不被点名也不报**）。
发现路径也很典型：3D 草图里只有一条样条 ⇒ 草图特征**整个不进树**、改名静默失败 ⇒
`sketch3d_end` 报"改好了"、`check_sketch` 报"找不到"。

⇒ 两条修法都落到了代码里：
1. `op_spline` 改用 `variant_r8(flat)`（并把它写进 op 文档，作为**不可选**项）；
2. `sketch3d_end` 的改名**加读回校验**，失败时明确提示"草图可能一个图元都没有（样条漏了 variant_r8）"。

> ★★ 这条是本轮**最值钱的教训**：`verified=True` 会**漂**。
> 判据是"有没有一个**点名它的探针**"，不是"当初验过"。
> ⇒ 新增/改动 op 后，**先确认有探针点名它**，再谈 verified。

### ⑤ `check_sketch` 的判据盲区：**"找不到草图"与"空草图"都报 0**

```python
with contextlib.suppress(Exception):     # 旧写法
    f = typed(sw.model.FeatureByName(name), "IFeature")
    ...                                   # name 写错 → 异常被吞 → n_seg=0
```
症状：报"图元数不符：实际 0"，让人一路去查"图元为什么没画上"，
**真因只是名字不对**（改名没生效）。⇒ 已改成**显式抛错**：

> `找不到草图特征 'X' —— 注意：这**不是**「草图是空的」，是**名字不对**。`

同一条纪律与 §128 的"干涉检测只能放最后"同源：**判据里不许有 `suppress`**。
（顺带补了 `expect_segments` 与 `dump_points`：后者读回直线段端点的**全局 mm**，
是 3D 草图的硬判据 —— 一段纯沿 Z 的线在 2D 草图里根本建不出来。）

### ⑥ 本轮硬数字（S1 回归基准）

| 用例 | 手算/期望 | 实测 |
|---|---|---|
| 13 点参数化空间曲线（`40cos t, 25sin t, 10t/π`） | 读回 13 点、偏差 < 0.01mm | **13 点，最大偏差 0.0mm**，类型 `CurveInFile` |
| 3 点闭合曲线（`closed=True`） | 读回 4 点、首尾重合 | **4 点，start=end=(0,0,0)** |
| 3D 草图 2 条直线（含纯 Z 段） | 端点读回 (0,0,0)→(0,0,50)、(0,0,50)→(50,0,50) | **逐位吻合**（z 生效） |
| 3D 草图 1 条 3D 样条（5 点，z 0→40） | 图元数 1、类型 SPLINE | **1 个 SPLINE** |

★ **方法论**（与 §129 一脉相乘）：换 API 路线连续无效 / 或读回"看起来合理但数不对"时，
**回头查官方 Remarks 与类型库**，并把"读数怎么读"当成**独立的、会写错的知识**去真机钉死 ——
本轮 6 条坑里有 4 条是"**读法**错"，不是"操作错"。


## §131 ★★ 运行器三坑：全量回归**必被时长上限掐** + `| tail` **丢光输出** + 进程**挂死**的识别（2026-10-10）

全量回归（`sw_probe.py` 不带 `--plans`）会 glob `assets/probes/*.json` = **85 个探针**。
真机上跑一次 ≈ **1 小时**，而单条后台命令有**外部时长上限**（实测在 **14m49s** 被掐）。

### 三个坑与对策

| 现象 | 真因 | 对策（已落到代码） |
|---|---|---|
| 跑完没有任何输出、报告文件**根本不存在** | ① `... \| tail -70` 的 **stdout 被块缓冲**，进程一被掐**缓冲区全丢**；② 报告**只在全部跑完时写一次** | ① 用 `python -u` 或直接 `> 文件`；② **`sw_probe` 改增量写盘**：每跑完一个探针写一次，带 `planned` + `partial` |
| 一次 85 个必被掐，**永远跑不全** | 探针太多、多数是重型件（`check_mass`/`circular_pattern` 各 3~5s） | **按族分块**：`--glob 'assets/probes/reducer_*.json'`，单块跑得完再跑下一块 |
| 看似还在跑，其实**挂死** | SW 侧不可见模态框 / COM 死锁（本机实测卡在 `reducer_cap_small` 的第 14 步附近） | **识别信号**：日志文件 `mtime` **几分钟不前进** + `python` 与 `SLDWORKS` 的 **CPU 都不再涨**（各 +几秒/5min）。⇒ 停任务、`Stop-Process` 清 python、**重启 SW**（会话已不可信，§120） |

### ★ 分块要按**重量**，不能按**数量**

真机实测单只耗时差 **40 倍**：`reducer_housing_base` **246 步 / 545s**、`reducer_housing_cover`
**112 步 / 162s**，而 `probe_v8_iso` **27 步 / 13s**、`coupling_half_v8g_sentinel` **64 步 / 40s**。
⇒ 分块时**先看步数**（`--list` 零成本列出来），**重型件单独成块**（每块 1~2 只），
轻量件可以 7~8 只一块。否则"7 个一块"里塞一只箱座就 9 分钟，白等。

### ★ 关键鉴别：**块缓冲** ≠ **挂死**

重定向到文件时 Python 的 stdout 是**块缓冲**（默认 8KB），所以"日志不动"**不能**直接判挂死 ——
要**同时看 CPU 是否还在涨 + 文件 mtime**：
- mtime 不动、但 CPU 仍在涨 ⇒ 只是在缓冲，**别打扰**；
- mtime 不动 **且** CPU 也不涨（持续 >2min）⇒ **真挂死**，按上表处置。

★ 顺带记一条：`sw_probe.py` 的 `--list` **不碰 SolidWorks**（`load_plans` 之后立即返回），
可用来先看总数/预览步骤，零成本。



## §132 ★★★ 放样曲面 + 引导线：**两个静默失败** —— 选择集被清空 & 引导线太稀（2026-10-11）

背景：CRH380CL 车头 S2 单曲面片（3 截面 + 2 引导线，`assets/crh380cl/plan_s2.json`）。
两个坑的**共同点**：`InsertLoftRefSurface2` **全都"成功"返回**，区别只在于**建出来的是什么东西**。

### ① 选择集被清空 ⇒ 一张 **y 恒为 0 的退化曲面**（判据全绿、几何全错）

- 真因：`_select_profiles()` **每次调用都 `sw.clear_selection()`**。而 `op_surface_loft` 里
  分三次调用它（截面 `mark=1` / 引导线 `mark=2` / 中心线 `mark=4`）⇒
  **选引导线时把刚选好的截面全清掉**，最后进放样的只有 2 条引导线。
- 症状（极具欺骗性）：**特征树涨了**、`check_body` 报 **1 曲面体 / 0 实体**（判据全绿），
  只有面积露出马脚：**10.46 m² vs 真值 33.6 m²**。
- 定位手段（★ 记下来）：`IFace2.GetTessTriangles` 拿顶点包络 —— 退化曲面 **y 恒为 0**、
  只有 **16 个三角片**（正常 1400+）；`IBody2.GetBodyBox` 的 y 同样退化成 `[0,0]`。
- 修法：`_select_profiles(..., clear=False)`，**只在第一组清场**。
- 护栏（已落到 op）：放样前 `selmgr().GetSelectedObjectCount2(-1)`，
  必须 = `截面 + 引导线 + 中心线`，少一个**当场拦下**。
- ★★★ 铁律：**截面 / 引导线 / 中心线共享同一个选择集**，
  任何一组"清场"都会毁掉全部 —— 与 §129 的 `append=True` 是同一根命脉的两端。

### ② 引导线太稀 ⇒ 找不到交点 ⇒ **特征树不涨、不报错**

- 现象：8 点的引导线**全部失败**（3 截面/1 引导线/不同角度/伸出首末截面，五种变体全灭）；
  把引导线**加密到 24 点**（与截面同量级）→ **一次成功**。
- 机理：引导线是**通过 XYZ 点的曲线 = 样条**。点稀时样条在中间截面处**偏离**零点几毫米，
  于是它**不再与截面曲线相交** ⇒ SolidWorks 放弃了这次放样，而且**什么都不说**。
- 修法：引导线点数 **≥ 截面点数/2**，且 **每个截面站位都必须落一个引导线点**（精确相交）。
  `scripts/sw_head.py` 的 `guide_u_list()` 就是按这条写的（每段再细分 8 份）。
- 副产物：引导线样条会**过冲**，把曲面顶出去一点 —— 探针实测 X/Y **41.04 vs 40（+2.6%）**；
  车头 S3 只偏 **0.083mm**（截面多 ⇒ 过冲被压制）。⇒ 判据要给引导线留余量（探针 bbox 容差 1.5mm）。
- ★ 新增探针 **`assets/probes/surface_loft_guides.json`** 专门点名这个分支
  （3 圆截面 + 2 引导线，面积对解析解 4805.7127）—— 原来那只 `surface_loft` 探针
  **只测了 `sections`、没测 `guides`**，所以这个分支坏了整整一天没被发现（§130④ 的同一课）。

### ③ 判据设计的教训：**别拿"全部站位"去验"部分截面"**

- 首轮把 **9 个站位**的截面全拿去测贴合度，中间 6 个偏差 **31.7mm** ⇒ 误判"不通过"。
- 放样曲面**只保证过它用到的那些截面**。判据必须对着**实际参与放样的集合**（从 plan 读 `SEC_*`）。
- ★ 反向利用：**没参与的站位偏差**正好是"**截面数够不够**"的量化指标 ——
  3 截面 **31.7mm** → 9 截面 **0.006mm**，直接指导 S2→S3 的加密决策。

### ④ 好用的硬判据：`IFace2.GetClosestPointOn`

- 对截面上的每个点求**曲面上最近点**，距离应 ≈ 0 ⇒ **曲面确实过该截面**（逐站宽高自然成立）。
- 比"按 x 分箱量宽高范围"**硬得多**：分箱会把箱内极值算进来，实测虚高 **20~70mm**（假超差）。
- ★ 返回的是 **5 元组**（前 3 个是坐标，单位**米**），别按 3 元组解包。




## §133 ★★★ 平面区域封口：**必须选 EDGE，选特征静默失败** + 一个"初值状态"造成的**假 requires**（2026-10-11）

**背景**：S4 收口要把放样曲面的**开口**封上（S2 已证：**曲面放样不出端盖**），
新增 op `surface_planar`（`IModelDoc2.InsertPlanarRefSurface`，**0 参**、全靠**预选**边界）。

### ① 选错层级 = 静默失败（与 `thicken`/`surface_knit` 的 `mark` 坑**同族**）

对照实验（`_diag/exp_planar.py`）：同一条**闭合圆曲线**（`curve_xyz`，同一几何），

| 选什么 | API 返回 | 特征树 | 面积 |
|---|---|---|---|
| `REFERENCECURVES`（曲线**特征**） | `False` | **不涨** | 无 |
| 它自己的 `EDGE`（按坐标落在曲线上） | 成功 | **+1** | 出现 |

⇒ **该 API 要的是「边」（EDGE），不是曲线特征**。0 参 API 的"预选"语义比想象中**窄**。
⇒ 修法：`surface_planar` **首选** `SelectByID2("", "EDGE", x,y,z, …, swSelectOptionExtensive)`，
落点从 `edge_at` 或**参考曲线首点**取；特征级（SKETCH→REFERENCECURVES→BODYFEATURE）**只作兜底**。

### ② ★★★ "初值状态"让 `requires` 变成假绿（最值钱的一条）

`surface_planar` 的边界**两种来源**：参考曲线（`curve_xyz`）**或**一条**草图边**（整圆），
于是 `requires` 应该是 **OR**。但状态机里 `INITIAL_STATE = {"sketch_closed"}` ——
**`sketch_closed` 开局就为真**（它只表示"此刻没有草图开着"）⇒
把它当 OR 的一项 = **恒满足 = 假 requires**（等于没约束）。

⇒ 修法（本轮落地）：
1. 新增**真实状态** **`has_sketch`** —— 只有 `sketch_end` 之后才置位（表达"**画过**草图"）；
2. `surface_planar.requires = (("has_curve", "has_sketch"),)`；
3. 给 `requires` **加 OR 支持**：元素可以是**元组 = OR 组**（组内至少满足一个），
   判断走 `sw_ops.missing_requires()`，文本渲染走 `sw_ops.req_label()`；
4. `sw_selftest` 新增守卫：**OR 组至少 2 个备选**；状态名合法性检查**先摊平**
   （`sw_ops.flat_requires()`），否则会把元组本身当成状态名判不合法。

⇒ ★★ **通用教训**：**写 `requires` 之前先问"这个状态是不是初值"**。
   初值状态（`sketch_closed`）表达不了"做过某事"，拿它当前置 = **没约束**。
   （附带：这也解释了为什么 `feature` 类 op 一律要用 `has_feature` 这种**累积态**，
   而不是 `sketch_closed`。）

### ③ 样条封盖的 bbox 过冲（沿用 §132 的账）

闭合的**通过点曲线**（样条）封盖：面积 **1256.4637** vs 真圆 πr²=**1256.6371**
（−0.17，0.014%）—— 这是"**样条 ≠ 圆弧**"的**真实几何差**，不是 bug；
bbox 同样过冲（**40.46 vs 40**）。
⇒ 探针 bbox 容差给到 **0.6mm**；**要逐位等于 πr² 就用真圆草图封盖**。

⇒ 新探针 **`assets/probes/surface_planar.json`**（6 步，闭合曲线 → 平面曲面，
面积对 πr²）。★ 注意它**只走曲线路径**；**草图边路径**由 `surface_knit_solid` 探针覆盖
（那条用 `edge_at` 显式给点）。




## §134 ★★★ 曲面收口成**实体**：`form_solid=True` 少了 `gap_filters=True` 就**静默不成实体**（+ 两个放样几何坑）（2026-10-11）

**背景**：S4 第二条路线 = 封盖 + `surface_knit(form_solid=True)` 缝成实体。

### ① 命门：`form_solid=True` **必须**配 `gap_filters=True`

真机对照实验（`_diag/exp_knit.py` 夹具 = 放样筒 + 两端平面盖，**共 3 张面**，
且已确认边界**闭合**）：

| `gap_filters` | `form_solid` | 结果 |
|---|---|---|
| **False** | True | 3 体 → 1 体，但**仍是曲面**（`IBody2.GetType()` = **1**）；**0 条自由边**（拓扑闭合）⇒ "看着缝上了" |
| **True** | True | 3 体 → **1 实体**（`GetType()` = **0**） |

- ★ 与**容差无关**：`KnitTolerance` 取 **0.001 / 0.01 / 0.1** 全都"只缝成面"，
  只有翻 `gap_filters` 才成实体。复现 **2/2**。
- ⇒ `surface_knit.gap_filters` 默认 **False → True**（旧默认会让 `form_solid` **名不副实**）。
- ★★★ 最险的地方：**症状是"成功了"** —— 体数确实少了（3→1）、自由边 **0 条**（拓扑上是个闭合壳），
  只有 `GetType()` 说它**还是曲面**。
  ⇒ **判据必须问"是实体还是曲面"，不能只问"缝没缝上"**（§铁律①的又一次现形）。

### ② 放样几何坑 A：**通过点曲线的多截面放样，不带引导线会"鼓"出来**

- 3 个真圆截面（through-points 曲线）**不加引导线**放样 ⇒ 曲面**鼓出**：
  bbox **±23.18**（应 20）、面积 **4509.60** vs 解析 **4805.71**（**−6.2%**）。
- ⇒ 多条截面时**要么给引导线，要么保证几何本身无中段过冲**（见 ③）。

### ③ 放样几何坑 B：**两个完全相同的截面放样 ≠ 圆柱**

- 两段**相同半径**的通过点圆曲线放样，中段**缩到 r/√2**（体积掉到 **2/3**）。
- ⇒ "两端一样"**推不出**"中间一样"。简单直筒 / 圆台，**用草图真圆最稳**
  （面积 / 体积逐位吻合，也不会有引导线样条过冲）。

### ④ 结果（S4 两条路线都真机双证）

| 路线 | 计划 | 判据（实测） |
|---|---|---|
| **knit 成实体** | `assets/crh380cl/plan_s4_knit.json`（23 步） | 3 面 → **1 实体**；体积 **19.1142 m³** vs 解析 19.1107（**+0.019%**）；盒 [4500, 3265.166, 3890.285] |
| **thicken 成壳** | `assets/crh380cl/plan_s4_shell.json`（20 步） | side2 **3mm**；体积 **0.10184 m³** vs 解析 0.09953（**+2.32%**） |

★ 新探针 **`assets/probes/surface_knit_solid.json`**（19 步，**故意不传 `gap_filters`** 以锁默认值）——
原来那只 `surface_knit` 探针**只测了 `form_solid=False`**，所以这条分支坏了同样没人知道
（**同一课：判据是"有没有点名这个分支的探针"**，见 §130④）。


## §135 分割线 / 剪裁曲面：三条「选择集」级静默失败

> 2026-10-11。目标 = S5 细节（在车头曲面上划风挡 / 车窗边界）。
> **分割线已打通**（新 op `split_line`，探针 12/12）；**剪裁曲面（S12）未打通**，负面结论一并记下。

### ① 分割线：唯一可用组合 = **草图 mark=4 → 面 mark=1**

- `IModelDoc2.InsertSplitLineProject(IsDirectional, FlipDir)`（**2 参、返回 void**）。
- 真机对照（8 组）：**只有「先选草图 mark=4、再选目标面 mark=1」建了特征**。
  其余（草图m1+面m4 / 草图m1+面m1 / 面先选 / 只选草图 / 全 mark=0）
  **全部** `SelectByID2` 返回 True、选择集看着是满的，但 API **不建特征、不报错**。
- ★ 与 `thicken`「体必须 mark=1」同族：**0/2 参 void 型特征的 mark 语义只能真机对照试**，
  别照搬别的特征。
- 判据：**面数**（一条投影线把一张面**劈成两张**：1→2）+ 特征树 +1。
  ★★ **面积基本不变**（7853.98→7853.47，仅 0.007%）⇒ `check_body` 对它是**假绿**；
  为此新增判据 op **`check_faces`**（数面 + 按面积降序分布）。

### ② 分割线：**草图平面不能与目标面平行**

- 草图 ∥ 目标面 ⇒ 投影方向与目标面平行、**无交点** ⇒ 同样**静默失败**（void + 树不涨）。
- 实测：目标面 z=20，草图放 **Top**（法线 Z、∥）**失败**；放 **Front**（法线 Y、⊥）**成功**，复现 2/2。
- ⇒ 让草图平面**斜交或垂直**于目标面（投影方向要能「扫」过目标面）。
- ★ 排查教训：诊断脚本里 `select_plane` 的参数名写错（`which` 被**静默忽略**）⇒
  草图实际画在 Front 平面、"意外成功"；改对成 Top 后全失败 ——
  **「参数名写错被忽略」会让实验结论整个反过来**。对照实验前先确认参数真的生效
  （`sw_plan --check` 会报「不认识参数 X（会被忽略）」）。

### ③ `SelectByID2("", "SURFACEBODY", 点, append=True)` —— **选择集非空时恒失败**

- 实测：选前 1 项 → 拾取 **False** → **选后 0 项**（把已有选择**清掉**了！）；空集下才 True。
  ⇒ 多选流程里用坐标选曲面体**永远失败**（且会毁掉前面的选择）。
- 同一点改用 **`FACE`** 类型：选前 2 → 选后 3 ✓。**坐标拾取用 `FACE`，不要用 `SURFACEBODY`**。
- （按**名字**选曲面体 `SelectByID2("名", "SURFACEBODY", 0,0,0, append=True, …)` 正常。）

### ④ 剪裁曲面（S12）**未打通** —— 负面结论 + 已排除项

- `IFeatureManager.PreTrimSurface(4 参)` 会**清空选择集**（实测 2~3 → 0）。
- Pre **之后**：`SelectByID2` 坐标拾取**完全失效**（连空集上都 False）；
  `IModelDoc.MultiSelectByRay`（7 个数 = 起点3+方向3+半径，`swSelFACES=2`）也 False。
- 已试组合（`_diag/exp_trim*.py`）：① Pre/Post 的 mark 1/2、0/0、2/1 + keep mark 0/4；
  ② `BSplitSystemIn` T/F；③ `BRemovePickedIn` T/F；④ tool/target 顺序互换；
  ⑤ 老式 `InsertCutSurface(Flip, KeepPieceIndex)`（基准面 / 曲面 + 4 种 mark）；
  ⑥ keep 在 Pre 前 / 后；⑦ `SolidForTrim=False`。**全部无产出**（`Pre` 回 True、Post 无特征）。
- ★ 下次续攻的入口：官方 VBA 范例（2013）顺序 = 「选 tool → **Pre** → 坐标选被剪区 →
  `PostTrimSurface(True)`」，但该顺序在 **SW2024** 下 Pre 会清空选择集 ⇒
  关键是找「**Pre 之后仍能被 API 接受的选择方式**」。可试：`ISelectionMgr` 系 API、
  `InsertUntrimSurface2` 对照，或**换完全不同的路**（先 `thicken` 成实体、再用
  `extrude_cut`/`cut_swept` 剪 —— 实体侧的选择语义已完全掌握）。
- 另记：`IFeatureManager.SolidForTrim`（0 参 get / 1 参 set）真实存在，可作后续变量。


## §136 ★★★ 换路做剪裁（S12 替代路线）成功：**曲面 → 加厚 → 实体侧挖洞**
## （+ 草图轴映射标定法 + 状态机漏配 + 一次「会话被拖垮」的取证）

> 2026-10-11。S12 剪裁曲面（§135④）在本机 SW2024 下走不通 ⇒ 换路：
> **`曲面 → thicken 成实体壳 → extrude_cut 挖洞`**。
> 真机双证：探针 `assets/probes/shell_window_cut.json` **23/23 通过（27.91s）**，
> 体积减少量**逐位吻合**。新探针同时锁住下面 ②③④ 三条约定。

### ① 替代路线成立：判据「体积减少量 = πr²·t」是**精确**的，不是近似

- 几何：Front 平面 60×40 矩形 → 曲面拉伸 40 ⇒ 方管（面积 8000mm²）
  → `thicken side1 3mm` ⇒ **25440mm³**（S0 的回归基准，逐位重现）；
- Right 平面画 r=10 圆 → 「完全贯穿-两者」沿 X **挖穿两侧壁**
  ⇒ 去掉 **2 × π·10²·3 = 1884.9556** ⇒ 剩 **23555.0444mm³**（实测逐位相同）。
- ★ **为什么是"精确"**：圆柱轴 ∥ 板法向 ⇒ 去掉量 = πr²·t，**与曲面曲率无关**。
  比「剪掉的那块面积对得上」硬得多 —— 体积本来就是**去料类 op 的铁律②**。
- ⇒ **S5「剪掉多余部分」的诉求已用实体侧路线达成**；`PreTrimSurface` 那条路可彻底放弃。

### ② 草图平面 → 全局坐标的**轴映射**：必须用变换矩阵标定；`ISketchPoint` 回的是**局部**坐标

- 挖洞圆心必须落在曲面正中，而曲面**只朝草图平面的一侧长**（见③）⇒ 圆心坐标取决于平面映射。
- ★★★ **`ISketchPoint.X/Y/Z` 是「草图局部」坐标**：在 Front / Top / Right 三个基准面上
  各画一条 +v 线段，读回**一模一样**的 `[0,0,0]→[0,10,0]`（诊断 3 实测）。
  拿它标定 ⇒ 会把「三个平面映射不同」误判成「映射相同」，然后孔就挖在空气里。
- 正解：读 **`ISketch.ModelToSketchTransform`**（`IMathTransform.ArrayData` = **16 个 double**，
  `[0..8]` 旋转 3×3、`[9..11]` 平移、`[12]` 比例）。实测三个基准面：

  | 平面 | 草图 +u | 草图 +v | 法向 +n |
  |---|---|---|---|
  | **Front** | +X | +Y | +Z |
  | **Top** | +X | **−Z** | +Y |
  | **Right** | **−Z** | +Y | +X |

  ⇒ Right 平面上「要全局 Z = +20」就得写 `u = -20`
  （探针里那个看着很怪的 `circle cx=-20` 就是这么来的）。

### ③ 两条会**静默**的约定

- **`surface_extrude` 的 `both_directions` 被忽略**：主方案恒 `Sd=True`（单向前），
  所以曲面**只朝草图平面一侧长**。想要"两侧对称"得靠**几何摆位**
  （先把草图平面 offset 到中间），别指望这个开关 —— 它不报错、也不生效。
  （`end_cond="swEndCondMidPlane"` 实测同样无效，仍走单向前。）
- **`check_bbox` 的 `tol` 是比例不是 mm**：实际容差 = `max(|期望| × tol, 0.2mm)`，
  齿轮上曾比作者以为的松 **100 倍**。★ 一律用 **`tol_mm`**（绝对 mm）。

### ④ 状态机漏配：`thicken` 没声明 `effect` ⇒「曲面 → 加厚 → 切除」在**静态校验**就被拦下

- 症状：`sw_plan --check` 报
  「`[cut]` 顺序错误：`extrude_cut` 需要**已有至少一个实体特征**，
  但此刻的状态是 `{doc_open, has_sketch, has_surface, sketch_closed}`」。
- 真因：`thicken` 写的是 `requires=("has_surface",)`，**后面没有 `effect=`** ——
  可「加厚」**一定**产生实体（它本质是"生成凸台"）。补上 `effect=("has_feature",)` 即通。
- ★ 与 `revolve_boss` 漏声明 `has_feature`（见该 op 里的长注释）是**同一类**漏配。
  **判别法**：这个 op 之后，**下一个 op 的 `requires` 里有没有出现一个"本该已经为真"的状态？**
  有 ⇒ 就是这里漏了 `effect`。

### ⑤ 一次「会话被拖垮」：反复建文档不关 + `| tail` 吞输出 ⇒ 探针**卡死 19 分钟**

- 现象：`sw_probe` 一起跑就毫无输出；python 进程存活 **19 分钟**，但 CPU 只有 **0.078s**
  ⇒ **是阻塞，不是计算**。
- 取证：`Get-Process` 显示 SLDWORKS **`Responding = False`**、工作集 **4.2GB**、
  标题 `[零件67 *]` —— 前面十来个诊断脚本**每次 `new_part` 都不关文档**，
  攒了几十个未保存零件，SW 被拖垮（0.078s CPU + "Not Responding" 是决定性证据）。
- ★★ 两个**放大器**：
  1. **`... 2>&1 | tail -N` 会把整段输出吞掉**：stdout 走管道是**块缓冲**，
     进程被杀时缓冲区全丢，只漏出 stderr 那一行。
     ⇒ **长任务一律重定向到日志文件**（`> x.log 2>&1`），要看就 `tail` **文件**。
  2. **`--preflight`（`sw_session_check`）会再建一批文档** ——
     在已经被拖垮的会话上就是压垮骆驼的那根草。
- 处置：`Stop-Process -Name SLDWORKS -Force`（**客户端脚本先杀不掉它** ——
  杀了 python 之后 SW 仍 `Responding=False`，说明卡在 SW 自身），
  重开**全新会话**再跑 ⇒ **同一份计划 23/23，27.91s**。
- ★★ **结论：「探针卡死」先怀疑会话、别先怀疑 op。**
  判据 = 「客户端 CPU ≈ 0 **且** SLDWORKS `Responding=False`」。
  纯 Python 的离线校验（`sw_plan --check` / `sw_selftest`）不受影响，可以先跑它们把 op 摘清。


## §137 ★★★ S5 落到真车头：换路剪裁**完整落地** + 三条新命门
## （数值积分器 2× 错 / 体积**变化量**判据 / 构造几何白名单的假绿）

> 2026-10-11。把 §136 的换路（`曲面 → thicken 成实体壳 → extrude_cut 挖洞`）
> 从「方管探针」落到**真车头参数化几何**上：`scripts/sw_head.py --stage s5` →
> `assets/crh380cl/plan_s5.json`（44 步）。真机 **43/43 全过**，
> 两处开口的**体积变化量**与参数曲面预算对得上（差 2.6~2.9%）。

### ① 落地的几何口径（★ 全部 `assumed`，写进 `params.json` 的 `windows` 块）

- **侧窗**：`Top` 基准面画 r=120 圆 → 沿 Y **双向完全贯穿** ⇒ 左右对称两个圆窗。
  圆心 z **自动取该站位断面中心 zc** —— 那里壁面近似竖直（|Δz|≤r 内壁面 y 变化 <1mm），
  所以去除量 ≈ 2πr²t；实测 Δ=−275788 vs 预算 −283901（**+2.9%** 是壁面沿 x 的斜度）。
- **前风挡**：`Right` 基准面画 600×1040 矩形 → 沿 X **完全贯穿** ⇒ 鼻锥前上方开口。
  z 带 `[1500,2100]`：顶面 z_top(x) 从 x=0 的 1460 沿 x 升高，**出带即止** ⇒
  开口自动止于前段（约 x≲1400），底面（z≈360）远在带外 ⇒ **不会切穿底板**。
  实测 Δ=−4582903 vs 预算 −4702874（**+2.6%**）。
- ★ 判据分工：**侧窗**用「数值积分 vs 2πr²t」；**风挡**用「数值积分 vs 实测 Δ」。
  两者的 **2.6~2.9% 同向偏差**来自「壳是单侧加厚（`side2`），而积分模型按 ±t/2 居中」——
  知道来源，就不会误判成「op 有问题」。

### ② ★★ 数值积分器：**中心差分写成「前向差 ÷ 2h」⇒ 面积正好差 2×**

- 症状：`shell_removed_mm3` 算出来的**全表面面积**是解析面积的一半（16.74 vs 33.18 m²），
  于是所有「去除量」预算都偏小一半 —— 拿它当期望值会**把对的判成错**。
- 真因（`sw_head._surf_pt_normal`）：算 ∂r/∂u 时分子取 `P(u) − P(u−h)`（跨 **h**），
  却除以 `(uhi − ulo) = 2h` ⇒ 该分量只有一半。
- 修法：分子分母跨**同一段** `P(u+h) − P(u−h)` / `(uhi − ulo)`。
- ★ 修完 **数值 ∫area = 33473647**，**真机读回 33474857** ⇒ 相对差 **0.0036%**。
  这就是「积分器对不对」的硬判据，已经写进 `sw_head.selfcheck()` 第 ⑥ 条
  （生成计划时就离线拦下，**不用等真机**）。
- ★★ **教训**：写数值积分要**先跟解析式对一次**（这里 `lateral_area_mm2` 就是现成的对照），
  否则「自己算错」会被当成「模型不对」。

### ③ ★★★ `check_mass` 判不出「这一刀切掉了多少」⇒ 需要 **`check_volume_delta`**（新 op）

- `check_mass` 比的是**绝对体积 ± 相对容差**。真机数据：壳体积实测 **101842194**，
  预算 **99529651**（**+2.3%**，在 8% 容差内）。
- ⇒ 若只用 `check_mass` 检查切除，那 2~5% 的绝对偏差**会把「没切到」和「切对了」一起盖住**。
  而**实体去料最该证明的就是「这一刀切掉了多少」**（去料类 op 必量体积 —— 铁律②）。
- 新 op **`check_volume_delta`**：首次调用记基准，之后每次与**上一次**读数做差；
  `expect_mm3` 为负 = 体积应下降；容差优先用**绝对** `tol_mm3`。
  真机结果：`Δ=−275788.4`（期望 −283900.6，容差 22712）、
  `Δ=−4582902.7`（期望 −4702873.5，容差 564345）⇒ 两刀都**独立可判定**。
- ★ 判据设计口诀：**「绝对量」检验模型整体对不对，「变化量」检验这一步动作有没有发生。**
  两者都要，缺一个就有盲区。

### ④ ★★★ 隐藏构造几何：`IFeature.Visible` **是只读的** ⇒ 靠它隐藏**一条都藏不到**（三层假绿）

- 为了让交付截图看得清开口，S5 计划里加了 `hide_refs` 再截图。
  跑完日志明明打印「已隐藏草图/曲线等构造几何」，**可截图上 9 个截面 + 2 条引导线的蓝线照样糊满曲面**。
- 真机取证（`_diag/exp_hide.py`、`_diag/exp_hide2.py` —— 打印 `GetTypeName2()` 与逐步结果）：

  | 层 | 事实 | 造成的假绿 |
  |---|---|---|
  | ① 类型名 | `InsertCurveFile`（「通过 XYZ 点的曲线」）的类型名是 **`CurveInFile`**，不在 `Curve`/`RefCurve` 白名单里 | 「一个都没匹配上」 |
  | ② 机制 | **`IFeature.Visible` 只读**：设值抛 `AttributeError: Property 'IFeature.Visible' can not be set.`（`CurveInFile` **和** `ProfileFeature` 都如此） | 就算匹配上了也**一条都藏不到** |
  | ③ 异常 | ①②都被 `contextlib.suppress(Exception)` 吞掉，函数**正常返回** | 日志打印「已隐藏」= 只证明**函数跑过** |

- ★★★ **正解**（tlb 里查到、`_diag/exp_hide3.py` 真机双证，`Blank*` 都是 **0 参 void、作用于选择集**）：
  - **曲线**：`SelectByID2(名, "REFERENCECURVES", …)` 全选 → `IModelDoc2.BlankRefGeom()`
  - **草图**：`SelectByID2(名, "SKETCH", …)`           全选 → `IModelDoc2.BlankSketch()`
  - ⇒ `hide_construction_geometry()` 已重写；截图从「蓝线糊满」变成**开口看得清清楚楚**。
- ★★ 三条延伸（都值钱）：
  1. **「执行了」和「生效了」是两件事** —— 白名单式隐藏的判据必须落在
     「**藏了几个 / 选择集里真的有了**」，`features_hidden` 为空就报警（已加进 `sw_shot.py`）。
  2. `with contextlib.suppress(Exception)` 包住 API 调用是**双刃剑**：
     它让脚本「不崩」，也让**只读属性设值失败**这种硬错误**彻底消失**。
     凡是 suppress 过的段落，判据要落在**它的副作用**上，不能落在「没抛异常」。
  3. ★ 我一度按「第一次诊断的截图里看不到草图」判定「草图藏成功了」——
     **那是错的**：那张图是**俯视**，画在 **Top 平面**上的圆/矩形在俯视里**正好是侧视（一条线）**，
     本来就看不见。⇒ **「看不到」不等于「被隐藏了」**，取证的视角必须能真正show出目标。

### ⑤ 复用清单（下次做「曲面壳开洞」直接照抄）

```
曲面 → thicken(side2, t)              # 得实体壳
     → 面画圆/矩形（注意基准面轴映射，见 §136②）
     → extrude_cut(swEndCondThroughAllBoth / ThroughAll)
判据：check_body(实体 1) + check_faces(面数↑)
      + check_volume_delta(Δ ≈ 解析/数值预算)   ← §137③
     + 截图（先 hide_refs，且**核对隐藏计数**）   ← §137④
```

## §138 ★★ S6 交付：两条「工具自身的坑」—— 测试要测**被测函数本身**；保留名不许被占地

> 2026-10-11。S6 交付时给 `sw_shot` / `screenshot` 加了「自动裁剪空白」；
> 收口时又发现上一轮把 `_build/api.json`（API 签名库）**覆盖**成了 op 机读导出。
> 两个都是**工具层**的错（不是建模的错），但都会**静默**骗人。

### ① ★★★ 独立试验脚本 ≠ 集成进工具的函数（裁剪守卫极性写反）
- 现场：给 `sw_shot` 加 `autocrop_content()`。先写了独立试验脚本 `_diag/croptest.py`
  （自带一份**长得像**的实现）→ 五张真机图**全部裁剪成功**，于是集成进 `sw_shot._to_png`。
- 结果：集成后**一张都没裁**（1400×1050 原样输出，md5 与"不裁"时**逐位相同**）。
- 真因：集成版多了一道守卫，**极性写反**：
```python
# ✗ 两维都"小于 97%"就返回原图 —— 恰恰在**需要裁**的时候触发
if (x1-x0) < 0.97*w and (y1-y0) < 0.97*h: return im
# ✓ 只有两维都"几乎没削掉"才不动
if (x1-x0) > 0.97*w and (y1-y0) > 0.97*h: return im
```
- ★★ 教训：**试验脚本通过 ≠ 被测函数通过**。试验脚本是**另一个实现**，
  它没有那道守卫，所以**永远不会替你发现守卫写反了**。
  ⇒ 裁裁剪、测解析、测任何"工具函数"，一律 `from sw_shot import autocrop_content`
  **直接测被测对象**；**不要复制一份"长得像"的**。
- ★ 裁剪口径：SolidWorks 渲染是**白底白模** ⇒ **按颜色差裁剪必失效**
  （模型与背景都接近 250）。判据要落在**结构**上：背景是平滑渐变（局部梯度≈0），
  模型有轮廓边 + 明暗交界边 ⇒ 用 **`|∂x|+|∂y|` 阈值**定位内容框；
  并设两道守卫（内容几乎盖满 ⇒ 不裁；两维几乎没削 ⇒ 不裁）——
  **宁可留白，也不裁掉模型**。

### ② ★★★ `_build/api.json` 是**签名库**的保留名，op 导出不许占地
- 现场：上一轮为拿机读 schema，顺手跑了
  `python scripts/sw_ops.py --schema _build/api.json` ⇒ 把 `sw_docbuild.py` 生成的
  **接口签名库**覆盖成了 `yoyo.opschema/1`。
- 后果：`sw_apisig.py --sig/--find/--iface/--gaps` 全废，报「不是 API 签名库」；
  更阴的是 **`sw_docbuild.py --build` 会「增量合并」旧文件**（`load_prev`），
  于是把 op-schema 的**字符串当接口**写进去，最后在 `stats["members"]` 处抛
  `TypeError: string indices must be integers` —— **报错完全指不到根因**。
- 三道篱笆（都已落进代码）：
  1. `sw_apisig.load_api()`：读进来**先验形状**，不成就**明说"这是 sw_ops --schema 的产物"**；
  2. `sw_docbuild.load_prev()`：**同样验形状**，不成形状就**按空原地重建**，不参与增量合并；
  3. `sw_ops.py --schema`：目标名是 `api.json` 时**直接拒绝**（exit 2），
     并指路 `assets/op_schema.json`（op 导出的正规落点）。
- ★ 修完：`--build` 重建 ⇒ **73 接口 / 5137 成员 / 926 枚举**；`sw_selftest` 全绿。
- ★ 记住**两个产物的正规落点**：op 机读导出 = `assets/op_schema.json`；
  API 签名库 = `_build/api.json`（+ `_build/INDEX.md`）。**同名不同物，别混。**

### ③ 复用清单（S6 交付）
```
交付渲染：   sw_shot --views iso,front,right,top,trimetric --grid   # 自动裁空白 + 自动藏构造几何
计划内截图： screenshot(path=..., crop=true)                        # 与上面同一个裁剪切面
重建签名库： sw_apisig.py --build                                    # 重建 _build/api.json
op 机读导出：sw_ops.py --schema assets/op_schema.json                # ★ 不要指向 _build/api.json
```

## §139 ★★★ R-A 精修：把开口做成真车形状（新增「预算 = 0 是假绿」与「新组合先探针」两条）

> 2026-10-12。用户给了 4 张 CRH380CL **真车照**（正脸 / 站台 3-4 / 侧视×2），做 R-A 几何精修：
> 风挡 `矩形` → **上宽下窄圆角梯形**；驾驶室侧窗 `Ø240 圆` → **后掠斜四边形**；
> 鼻锥收窄指数按照片微调（`half_width` 1.60→1.52、`half_height` 1.30→1.35）。
> 手法：`polyline`（任意闭合折线）+ `round_poly` 圆角近似，替掉 `rect_center`/`circle`。

### ① ★★★ 「预算 = 0」不是「通过」，是**没有判据** —— 轴序写错 ⇒ 不切也绿
- 现场：风挡换成多边形后，`windshield_removed_mm3` 的掩膜写成
  `_poly_mask([(p[1], p[0]) ...])`（把 (y,z) 当成 (z,y)），而调用侧是 `inside(y, z)`
  ⇒ 掩膜的包围盒早退**永远 False** ⇒ **去除量恒 0**。
- ★ 后果链（这才是毒的地方）：0 原样传成
  `check_volume_delta(expect_mm3=0, tol_mm3=0)` ⇒ 真机上**根本没切也算"通过"**。
- 两道篱笆（已进 `sw_head.selfcheck`）：⑦ 轮廓**有向面积 > 0**（点序自交/反向会让圆角朝外凸）；
  ⑧ **去除量预算必须 > 0**。并已**注入轴序 bug 复现红灯**验证过（改回错版 ⇒ 自检报
  「风挡去除量预算 = 0.0 mm³（应为正）」）。
- ★★ 可迁移：**凡是「期望值恰好为 0」的判据，先问一句「它能不能区分『做了』和『没做』？」**
  不能 ⇒ 那不是判据，是灯坏了。

### ② ★★ `polyline` + `extrude_cut` 是**新组合** ⇒ 先跑探针，别拿整条 S5 当小白鼠
- `polyline` 此前**从未和 `extrude_cut` 一起用过**（既往只有 `polygon` 做六角头、`circle`/`rect` 做切除）。
- 探针 `assets/probes/poly_window_cut.json` **21/21**：方管（体积 25440mm³）→ 右视面
  **44 点圆角梯形**折线 → 双贯穿挖两片壁 ⇒ Δ = **−1581.1531mm³**
  （解析 `2·A·t` = −1581.1561，**0.0002%**）⇒ 折线闭合、FeatureCut4 认轮廓、圆角短线段不自交，全部坐实。
- ★ 复用口径：**方管/平板 + 轴 ⊥ 壁面 ⇒ 去掉量 = A·t 精确**（与轮廓曲率无关）——
  比圆窗那条 `πr²t` 更通用，判据写法照抄。

### ③ ★★ 会话退化会**伪装成「计划有 bug」**（老坑新脸）
- 首次跑新探针在 `thicken` 报「这些要加厚的对象选不中：['Tube1']」；
  **同一份计划重跑 ⇒ 21/21 全绿**。且 `_diag/dbg_surfsel.py` 直接 dump 真机数据证明
  `SelectByID2("Tube1","SURFACEBODY",mark=1)` 本来就返回 **True**、`_body_stat` 有 1 个曲面体。
- ⇒ 还是那句（§122 / §128 家族）：**「同一份计划时绿时红」⇒ 先重跑 + 先 dump 真机数据，
  别先怀疑 op、更别先改 op**。这次要是直接去改 `thicken`，就把好代码改坏了。

### ④ R-A 的**口径**：照片给「形状」，不给「尺寸」
- 4 张照片只能定 **形状特征**（风挡 = 上宽下窄圆角梯形；翼窗 = 后掠斜四边形）与**比例**；
  它们**没有正交视图、没有标尺、有透视畸变** ⇒ **不能反求绝对尺寸**。
- 所以 `params.json` 里这些字段的 `_source` 仍写 `assumed`，只是出处升级为「照片依据的形状估计」，
  并注明「精确拟合需先在照片上标定比例尺（如用车体高 3890 当尺）」。
- ★ 复用手法：**圆角多边形 = `round_poly(凸多边形, r)` 折线近似** —— 离线可算面积、可判自交；
  **不要用 `sketch_fillet`** 去倒角（那个要选中两条线、对选中集敏感，§102 家族）。

### ⑤ 复用清单（R-A）
```
风挡轮廓： sw_head.windshield_outline(ws)  -> [(y,z)...]  # 上宽下窄圆角梯形；Right 面草图取 (−z, y)
翼窗轮廓： sw_head.wing_outline(wg)        -> [(x,z)...]  # 后掠斜四边形；Top 面草图取 (x, −z)
出图切片： polyline(points=[...], close=True) + extrude_cut(profile=...)   # 取代 rect_center/circle
预算：     shell_removed_mm3(...) 包一个 _poly_mask(...)   # ★ 掩膜轴序必须与调用侧的 (a,b) 一致
探针：     assets/probes/poly_window_cut.json               # polyline + extrude_cut 回归
```

---

## §140 ★★★ `mass_props()` 会返回**陈旧**读数 —— 「体积变化量」类判据整体错位（含 1.58× 假超差）

**症状**（2026-10-12）：同一块壳（3mm 薄壳、体积 236237920.9mm³）上连切 10 条
100×590mm 的条带，全部 `extrude_cut(through-all-both)`，10 条里
**有 2 条 Δ 为「正」**（切了料、体积反而涨了 136742.8mm³）；而且
「窗口整块」（面积 498195mm²）报 Δ=−4857654，**同区域**网格条（合计面积更大 590000mm²）
总和却只有 −3765081 —— 两处差 **1.5 倍**，一个大、一个小，**互不相容**。

**根因**：`SwSession.mass_props()` 只在**体积读成 0** 时才补一次 `EditRebuild3()` 重读
（那是为「实体不可见 ⇒ 读成 0」准备的）。体积**非 0 但陈旧**时，
它**直接采信** ⇒ **一次特征的效应会出现在下一步的读数里**（差一个特征的滞后）。
`CreateMassProperty2` 会返回上一次重建时的缓存值，而且**不报错、不返回 suspect**。

**修法**（已落地 v1.4.59）：
- `mass_props(fresh=True)` —— **先 `EditRebuild3()`、再读**；重建**前后各读一次**，
  不一致时以重建后为准并在返回里带 `refreshed_from`（旧值）。
- **凡是拿体积变化量当判据的地方一律 `fresh=True`**：
  `sw_ops._volume()`（去料 op 的「有没有咬到材料」守门）、`check_volume_delta`、`check_mass`。

**排查顺序（这次的教训，比坑本身值钱）**：拿到一个「Δ 超差 N 倍」时，**先证读数、再证几何**。
我这次一路先去查几何 —— 查轮廓（`polyline` 点序/闭合）、查切除语义
（`both_directions` 是否被静默降级）、查积分模型、查壳厚、查壁的层数 —— 全查完才发现
**读数本身在说谎**。真正立得住的两步是：
1. **平板探针**（`_diag/gen_wingcut_probe.py`）：同一张 44 点圆角折线 + `through-all-both`
   在 400mm 厚平板上切 ⇒ Δ=−199277952.8 vs 解析 199277952.8，**差 <1%**
   ⇒ **轮廓与切除语义都对**，嫌疑指向别处。
2. **参数模型离线积分**（`_diag/para_prismvol.py`）：V_prism=3015181.3（≙ 竖直双壁 6.05mm）
   ⇒ 「该切多少」是稳定的。
   ⇒ 两者都指向「读数」之后，才动手改 `mass_props`。

★ 可迁移的判断：**「读数不是掷骰子」这件事本身需要被证明**。
连切多条、Δ 出现**符号错误**（切料而体积涨）= 读数问题的**充分证据**，别再往下猜几何。

---

## §141 ★★★ 鼻尖**线性收敛 ⇒ 尖锥（刀锋）**；要「圆收缩」

**症状**（用户看图提的）：鼻尖是一个**刀锋**，不是圆的。

**根因**：`top` / `bottom` / `half_width` 三条一维轮廓**各自线性地**收敛到同一个点
`(x=0, z=1000)` ⇒ 断面尺寸沿 x **近似线性增长** ⇒ 曲面在鼻尖附近是一个**圆锥**
（母线是直线）⇒ 侧视/俯视都是尖角。用户要的是**曲率有限**的圆头。

**修法**：`tip_round_len_mm`（圆收缩段长 `xc`，默认 1500）+ `tip_z_mm`（鼻尖点高度）。
在 `x < xc` 段把三条参数换成 **1/4 椭圆「圆帽」**：
```
s = x/xc ,  q(s) = √(1 − (1−s)²)          # q(0)=0 且 q'(0)=∞ ⇒ 鼻尖切线**竖直**
a_c  = a(xc)·q
zt_c = tz + (zt(xc) − tz)·q
zb_c = tz − (tz − zb(xc))·q
w = s²(3 − 2s)                            # w(1)=1、w'(1)=0
x<xc : 参数 = (1−w)·圆帽 + w·原轮廓         # ⇒ 在 xc 处**值 + 斜率都连续**，不产生折痕
x≥xc : 原轮廓不动
```
- `xc=0` ⇒ 关闭，逐位回到旧的尖锥行为（方便 A/B 对照）。
- 实测：x=100 处半宽 **45.1 → 278.9**、x=1000 处 **514.2 → 572.8**；**x≥1450 完全重合**。
- 真机复核：放样仍**单一曲面**（面积 79265985.0mm²，+1.13%）、`thicken` 仍成功
  （壳 240189753mm³）、S4_knit 单实体 56695137.8mm³、bbox 仍在容差内。
  ⇒ **圆帽不影响 `surface_loft` / `thicken`**，但**改完必须重跑全量**（放样对截面形状敏感）。

★ 可迁移：**断面尺寸沿轴向的「增长律」决定尖端的观感**。
线性 ⇒ 锥（尖）；幂律 `x^p`（p<1）⇒ 尖但圆润；`√x`（=椭球帽）⇒ **圆头**。
只改「收敛到什么尺寸」不改「怎么收敛」，尖端永远是折线感。

---

## §142 ★ `open` 打开**已有实体**的零件 ⇒ 状态机不认识 `has_feature`

**症状**：`plan = [open(已有零件), ..., extrude_cut]` 静态校验直接拒绝，
报「extrude_cut 需要已有至少一个实体特征，但此刻的状态是 {doc_open, has_sketch, sketch_closed}」。

**根因**：`open` 的 `effect` 只有 `doc_open` —— 它**不声明**该文档里已经有没有实体
（打开一个新零件和打开一个装满特征的零件，声明是一样的）。

**绕法**：**在同一份计划里把实体重新建出来**（本次就是这么做探针的：
`s4_shell` 的步骤 + 探针切条，不分两个计划）。

**已修（2026-10-11，v1.4.58）**：`sw_plan.py` 的离线状态机对 `open` 增加**按扩展名推断**
—— `.sldasm` ⇒ 补 `asm_open`+`has_component`（原有）；`.sldprt` ⇒ 补 `has_feature`（新）。
两条都**显式发 warning**（推断出来的绿灯必须自己声明）。★ 残留假绿：若 `open` 的是**纯曲面**
零件，补 `has_feature` 是错的（真机 `extrude_cut` 会失败，纯曲面件要 `thicken` + `has_surface`）
—— 故 warning 文案点明「只当参照/改实体件用」。想彻底消歧，应给 `open` 加显式参数
（`has_bodies` / `surface_only`），由计划作者声明而不是靠扩展名猜。

---

## §143 ★★★ `open` **不会从磁盘重载已打开的文档** —— 顺序探针会静默累积状态（假读数的总根）

**症状**：同一份「打开已有壳 + 切一刀」的探针，跑第二遍、第三遍时**基准体积逐次变小**、
同一条切口量出的 Δ 忽大忽小（本次：同一条 x4100–4110 薄片，2.63e6 vs 3.77e6）。

**根因**：`ISldWorks.OpenDoc6` 对**已经在会话里打开的**路径，返回的是**内存里的那个文档**
（含此前所有**未保存**的修改），**不重新读盘**。于是：
```
ladder2 跑完（10 刀未保存）→ 文档还在内存里
quads    open 同一个路径 → 拿到的是 ladder2 切过的文档 → base = 240189753.5 − 1281470
xslab    open 同一个路径 → 拿到的是 quads 切过的文档 → base = 238908283 − 3415980
```
实测基准：`slabvis 240189753.5`（唯一干净）→ `quads 238908283.3`（−1281470≡ladder2 十刀和）
→ `xslab 235492303.2`（−3415980≡quads 四象限和）。**逐位对得上 = 铁证。**

**判据/做法**（三条，缺一不可）：
1. **`open` 前先清空**：`while ActiveDoc: close_doc(save=False)`，逼它从磁盘加载。
   （探针脚本必须做这一步；否则「第二遍」的结论全是脏的。）
2. **每条探针必须回带基准体积**并**断言它等于期望的净壳**（如 240189753.5）。
   只写 Δ 不够 —— Δ 对了基准错了照样错。本次就是靠 base 三连递减才揪出来。
3. ★ **顺序探针若共享同一文档，前序切口会与新切口重叠** ⇒ 后来的 Δ **偏小**（因为那
   块料已经被前序切掉了）。**要对比就一次一条、或每条都从干净文档重开。**

★ 可迁移：**「读数不可复现」先怀疑「对象不是你以为的那个对象」**，别先怀疑算法。
本次绕了两小时几何（轮廓/积分/厚度/折叠）才发现是 `open` 拿错了文档。

---

## §144 ★★ 解析积分器的**网格分辨率**必须显著小于工具区域（否则静默读 0 或aliasing）

**现场**：`shell_removed_mm3(nu=400, nphi=320)` 对一条**10mm 宽**的 x 薄片求值 ⇒ 返回 **0.0**
（相邻的 3700/3900/4500 都非零）。一度误判成「解析面在 x≈4100 有空隙」。

**根因**：积分按 `u=(i+0.5)/nu` 均匀取样 ⇒ `Δx = 8600/400 = 21.5mm`。
**10mm 的窄条整个落在两条网格线之间** ⇒ 一次都没采到 ⇒ 恒 0。
（`x in [4100,4110]` 共 0 个采样点；`[4000,4400]` 有 6080 个 —— 不是面有空隙，是网格跳过了。）

**判据**：**网格步长 < 工具区域最小宽度 / 3**。窄目标要么加密网格，要么改成「按工具区域
自适应布点」。**恒 0 是「没采到」的典型签名**，不是几何结论。

---

## §145 ★ 翼窗（side window）体积「超差 1.41×」—— 真壳在 x≈4000–4300 存在**局部掠射**（未闭环）

**症状**：S5（13 截面新壳，净体积 240189753.5）用真翼窗（x 3600–4600 / z 1454–2045，
沿 Y 双贯穿）切，`check_volume_delta` 实测 **Δ=−4332403.5**，解析 **−3079403.5**（1.41×）。

**已排除**（都做了真机实验）：
- **不是读数问题**：切完另存 `winprobe_rect` 后**关光文档重开**核对，体积逐位吻合（§143 的脏
  文档陷阱已排除，清爽加载 base=240189753.5）。
- **不是轮廓/切除语义**：同一张折线在**平板探针**上差 <1%；薄膜片 Δ 与解析方向一致。
- **不是截面数不够**：把 `s4_shell` 加密到 **25 截面**重跑，四象限比值反而更差
  （x4000–4200 那块从 14× 涨到 **29×**）⇒ 不是「站位太稀」的插值过冲。
- **不是截面本身斜**：x=4100 的截面右侧 `(y,z)` 逐点近**竖直**（Δy 7~21 / Δz 240）。

**已测到（四象限，同一张 400×300 窗）**：x3800–4000 比值 **0.72 / 0.84**（正常）；
x4000–4200 比值 **14.45 / 4.96**（掠射）。⇒ **病灶锁定在 x≈4000–4300 的一小段表面**，
那里沿 Y 的「等效料长」远大于 2×3mm，而**总容积仍然只有 3mm 的平均壁**（总误差 0.03%）
⇒ 必然是**局部表面法向异常**（掠射），被「总面积守恒」掩盖。

**下一步（未做）**：把 x≈4000–4300 那段单独抽出来，用 `IFace2.GetClosestPointOn` / 面法向
dump 出**真机面的法向**与解析面对比，定位是 `surface_loft`（两站位间的样条）还是
**两条引导线**把该段顶成了近水平面。**★ 在那之前 S5 的翼窗判据不成立**（1.41× 远超 8% 容差）。

★ 可迁移：**「总容积对」不能证明「局部形状对」** —— 掠射区多切的料可以被别处的薄区抵消。
判据要么落在**局部**（分块量），要么先证明**表面法向处处贴合理想面**。



## §146 ★★★ `fillet` / `chamfer` / `draft`：建特征调用**间歇性静默失败**（要重试）+ 拾取点**落在面边界**会选错对象（2026-10-12）

第一阶段"把 COM 全部封装好"全量复核时，三个老 op 一起挂了。**先别改参数** —— 两个独立原因叠在一起。

### A. `FeatureFillet` / `InsertFeatureChamfer` / `InsertMultiFaceDraft` 会**间歇性**返回"没建特征"

症状（真机、方块 + 4 条棱/4 个面）：

- 选择集**数得出来**：`GetSelectedObjectCount2(-1)` = 4，三种选择方式（`IEdge.Select4` /
  `SelectByID2` mark=0/2/4 / 直接逐条 `_select_obj`）**都 = 4**；
- 调用**返回 `None`、不抛异常**（`err=None`）、**特征树不涨、体积一字不变**；
- **绕过全部技能封装**（`win32com.client.dynamic.Dispatch` 直接调）**一样失败**
  ⇒ 不是类型绑定层；TLB 签名核对过，7/8/6 参全对 ⇒ 不是参数个数；
  尾参 `None / 0 / 0.0 / Empty / () / [0] / ''` 全试过 ⇒ 不是 Variant 取值；
  `FeatureFillet` / `FeatureFillet2` / `FeatureFillet3` / 旧的 `IModelDoc.FeatureFillet`
  （`FeatureFillet2` 甚至回 `1`）**全都不建特征**；`suppress_prompts` 关掉也失败；
- **同一会话里连试 8 次 ⇒ 8/8 成功**；另一次会话里连试 7 个变体 ⇒ 7/7 失败。
  ⇒ 这是**同一类 COM 瞬时失败**（本文件 `_pick` 的 `SelectByID2`、`AddComponent5` 早就有
  `COM_RETRY`），只是**从来没加在"建特征"这一步上**。

**修**：三个 op 的建特征调用都套上**基于特征树 diff 的重试**（没新增特征就重选、重调，
最多 `COM_RETRY` 次），次数如实写进结果 `attempts`。

★ 可迁移：**"选好了"不等于"建出来了"**。`GetSelectedObjectCount2` 是真话，但
`FeatureFillet` 的返回值 / 不抛异常都不算证据 —— 判据只能是**特征树新增 + 几何量**。
凡是"调一次 API 期待它建东西"的地方，都该按 `_new_feature` 的读回判据 + 重试来写。

### B. `draft` 还多一层：拾取点落在**面的边界棱**上 ⇒ 选到相邻面/棱

修好重试后 `fillet`/`chamfer` 过了，`draft` 仍 3 次全灭。逐项排除后定位到**探针的取点**：

- 方块是 `rect_center(40×40)` + `extrude_boss(depth 20)`，**默认是单向盲拉伸 ⇒ Z∈[0,20]**；
  而探针的拾取点写在 **Z=±10**（按"中面拉伸"的直觉写的）⇒ `Z=-10` 三个点**落在实体外**，
  只剩 4 个点命中 ⇒ 仍然是 4 个面，所以"看着没错"，但语义已经歪了。
- 把拉伸改成 `swEndCondMidPlane`（Z∈[-10,10]）后**仍然失败** —— 因为这时 `Z=±10`
  正好落在侧面的**上/下棱**上：`_pick` 走的是 `swSelectOptionExtensive`，
  边界点上它会挑到**相邻面或棱**，于是 `InsertMultiFaceDraft` 拿到的不是"4 个侧面"。
- **对照组**：同一脚本里用**面中心** `[±20,0,0] / [0,±20,0]` 取点 ⇒
  `dir_mark=1 + face_mark=2` **一次成功**（`feat=<IFeature>`）。mark 对照也一并做了：
  `dir=1/face=2` ✅；`dir=1/face=1` ❌；`dir=1/face=4` ❌；`dir=2` ❌（`COM_RETRY` 里报
  `-2147417851 服务器出现意外情况`）。

**修**：探针取点改成**面中心**（并保留中面拉伸让几何对称）。

★ 可迁移：**拾取点要落在面的"内部"，不要落在棱/顶点上** ——
`swSelectOptionExtensive` 在边界处"就近选"的结果可能与你的直觉不同，
而它**不报错**，只是把结果静静换成另一个对象。
"4 个点命中了 4 个面"这种**计数对上但对象错了**的情形，只有**看几何**（面法向/面积）才抓得到。

### C. 判别手续费（下次直接用）

1. `_edges` / `_faces` 打印 mid / normal / box，**确认取点落在目标对象内部**；
2. 选完立刻 `GetSelectedObjectCount2(-1)` **并**核对**选中对象的类型与法向**（别只数个数）；
3. 建特征失败时，**先看是不是"没重试"** —— 本项目所有"读回判据"的调用点都该带 `COM_RETRY`；
4. 探针挂死 20 分钟、python CPU ≈ 0 ⇒ **阻塞**（多半是没在 `suppress_prompts` 里跑，
   SW 弹了模态框）。此时 `Stop-Process` 重开，不要在脚本里等。


## §147 ★★ `SetTransformAndSolve3` 会把**组件名重置为默认名** ⇒ 计划里"按名取组件"的步骤顺序变了（2026-10-12）

现场（探针 `asm_ops.json`）：插入两个组件并各自改名（`地基-1` / `上块-2`，`check_component_count`
读回也是这两个名字），随后 `set_component_transform` 动 `上块` ——
再想按名字取 `地基` 就报：

```
组件名对不上：'地基'。当前装配体里只有 ['probe_asm_ops_box-3', 'probe_asm_ops_box-4']
```

即 **`IComponent2.Name2` 设过的名字在 `SetTransformAndSolve3` 之后被还原成默认名**
（`<零件文件主干>-N`，而且序号还往前跳了）。这跟 §128① 记的
「`swExtRefUpdateCompNames=True` 时改名静默失败」是**同一条链的下游**：
改名能不能"留住"，取决于 SW 什么时候重建外部参考。

**可执行的结论（写计划时按这个顺序排）**：
1. 所有**按组件名**取用的步骤 —— `check_component_box` / `check_component_count` /
   `rename_component` / `asm_face_probe` —— **必须排在所有 `set_component_transform` 之前**；
2. `set_component_transform` 之后**不要再按名字取组件**（要取就先用 `resolve_component`
   读回当前真名，或干脆把 transform 放到计划的最后）；
3. 若确实需要"改名 + 移动"两件事都留痕：**先改名、校验读回、再 transform**，
   并且把 transform 之后的检查改成**几何量**（包围盒/干涉）而不是名字。

★ 可迁移：**"名字"是 SW 里最不稳的标识**（语言、重建、外部参考都会动它）。
计划的步骤顺序应当**把'按名字找东西'的步骤全部前置**，把"会触发重建的操作"后置 ——
这与 §109 的「顺序原则」是同一件事。

## §148 ★★★ 改尺寸后**必须强制重建**：`EditRebuild3` **不会**把新尺寸算进几何（2026-10-12）

现场（探针 `naming_cn.json`）：`set_dimension(D1@草图1, 60)` 返回 `ok=True`、
`rebuild=true`，紧接着 `check_bbox` 期望 `[60,30,10]` 却**还是 `[40,30,10]`**。
回读尺寸本身也没报错 —— 于是形成"改了、也重建了、但几何没动"的**假绿**。

### 真机取证（`_diag/dbg_setdim.py` / `dbg_setdim2.py`）

分三层量，才发现谎在哪一层：

| 观测点 | 结果 |
| --- | --- |
| `SetSystemValue3(60, which=2, None)` 返回 | `0`（成功） |
| `IParameter.SystemValue` 读回 | **`0.06`（新值已写入！）** |
| `EditRebuild3()` 之后包围盒 | **`40`（纹丝不动）** |
| `ForceRebuild3(False/True)` 之后包围盒 | **`60`（生效）** |
| `sw.rebuild(force=True)`（= `ForceRebuild3(False)`） | **`60`（生效）** |

⇒ **存储值改了、几何没改**。这不是"没选对对象"，是"重建方式不对"。

### 结论

- **改尺寸 → 读几何** 的链路里，**只能用 `ForceRebuild3`**（= 旧 `sw.rebuild(force=True)`）；
  `EditRebuild3`（= `sw.rebuild()`，正常重建 / 对应界面 Ctrl+B）**不会重新生成被驱动草图**。
- 与 `whichConfigurations` **无关**：`0/1/2/3` 四个值都能把值写进去，四种都"成功"，
  真正的差异只在**重建方式**。⇒ 别再花时间调那个参数。
- 已修：`op_set_dimension` 收尾改 `sw.rebuild(force=True)`，并把 `rebuild_mode:"force"`
  与 `value_mm_after` 一起写进返回值（判据落在**读回值**上，不落在"调用没抛异常"上）。
- **创建期**的尺寸 op（`op_dimension` / `_dim_span` / `_dim_between` / `op_dim_auto`）
  **不需要**改 —— 它们在**草图打开时**加尺寸，几何本来就画在那个值上，
  真正的再生成由随后的 `sketch_end` + `extrude` 完成；此时也不该在活动草图里强重建。

★ 可迁移（③④ 是铁律①「返回值会说谎」的新面孔）：
1. **"改了值" 与 "几何变了" 是两件事** —— 改完必须**从几何读回**（包围盒/体积），
   不能拿 `SetSystemValue3` 的返回码或 `SystemValue` 读回当"生效"。
2. **重建有强弱之分**：`EditRebuild3`（Ctrl+B）省事、`ForceRebuild3`（Ctrl+Q）彻底。
   凡是"数据驱动的值"（尺寸、配置、外部参考）变了，默认按**强重建**处理。
3. 一个 `ok=True` 的操作链，中间任何一环静默无效果，整条链就是假绿 ——
   **判据必须挂在链路末端的效果上**（与 §146「选好了≠建出来了」同源）。

## §149 ★★ `check_sketch` 在**正在编辑的草图**里拿不到草图（`sketch_begin` 阶段还没定名）（2026-10-12）

现场（核心套件）：`sk_relation` / `sketch_circular_pattern` 两个探针在**草图还开着**时调
`check_sketch`（不给草图名），报 **「没给草图名，也拿不到『刚画完的草图』」**。

原因：`op_check_sketch` 的名字来源只有 `sketch` 参数或 `ctx["last_sketch"]`，而
**`ctx["last_sketch"]` 是 `sketch_end` 才写的** —— `sketch_begin` 阶段 SolidWorks 还**没给草图定名**
（名字在退出草图时才落库），于是 ctx 里是空的。

★ 关键事实：**正在编辑的草图特征其实已经在特征树里**（只是 ctx 不知道名字）。
⇒ 补一条**活动草图**兜底：`_active_sketch_name(sw)`（`GetActiveSketch2()` 裸取 Name；
★ 别 `typed(...,'IFeature')`，会拿到变换矩阵 —— 见 §136①）。

```python
name = _p(step, ctx, "sketch") or ctx.get("last_sketch") or _active_sketch_name(sw)
```

★ 姊妹 op `check_sketch_constraint` **本来就有**活动草图分支（靠 `ctx["sketch_open"]`），
所以只有 `check_sketch` 中招 —— **同一族 op 里"有的分支有的没有"是最容易漏的形态**，
写新 op 时要跟同族对齐。
★ 可迁移：**"刚画完的草图"在草图打开时并不存在于 ctx** —— 凡是"要拿当前草图"的 op，
都要**三级兜底**（显式名 → 上下文名 → 活动草图）。

## §150 ★★★ `split_line`：投影线**必须离草图原点 ≥ ~1mm**（过心线 / 同心圆一律静默不建特征）（2026-10-12）

现场（核心套件）：探针 `split_line` 的 `split_line` 步报「分割线没生成特征」。
`InsertSplitLineProject` **返回 void**、**不抛异常**、**特征树不涨** —— §146A 同族的静默失败。

### 排除链（都不是）

- 不是 mark/顺序（§135 的草图 mark=4 → 面 mark=1 组合**原样**）；
- 不是选错对象（`GetSelectedObjectCount2(-1)` = 2，草图 + 面）；
- 不是重试能救（加了 §146A 式特征树 diff 重试 **3 次仍全灭** ⇒ **不是瞬时失败，是确定性**）；
- 不是"开曲线 vs 闭曲线"（把首尾重复点去掉、真闭环 **一样失败**）；
- 不是"边界是样条 vs 真圆"（换成**草图真圆 r=50** 建的平面曲面，**一样失败**）。

### 真机扫描（每个都全新零件，`_diag/dbg_split4.py`）

切刀 = Front 上一条 `x∈[-60,60]`、`y=常数` 的线段（投影到 z=20 的圆面）：

| 切刀 | 结果 |
| --- | --- |
| 过心弦 **y=0** | ✗ 静默不建特征 |
| **y=0.5** | ✗ 静默不建特征 |
| **y=1** | ✅ 成功（面数 1→2） |
| y=2 / 5 / 8 / 10 | ✅ 成功 |
| 过心 **45°** 弦 | ✗ |
| 沿 Y 的过心线（x=0） | ✗ |
| **同心圆** r=20 / 30 / 45（`dbg_split6.py`） | ✗ 全灭 |

⇒ 判据：**投影轮廓必须离「草图原点」有 ≥ ~1mm 的间隙，且不得把原点围在里面**。
0.5→1 是**陡坎**（不是渐变），所以别指望"差一点点也行"。

### 处置

- 探针 `split_line` 的切刀 **y=0 → y=10**（远离原点、仍横穿圆面），判据改**弦长解析式**：
  小弓形 `R²·arccos(d/R) − d·√(R²−d²)`、其余块 = `A − 小弓形`（d=10、R=√(A/π)）⇒ 实测吻合 ~0.03%。真机 **12/12 通过**。
- ★ 附带：`check_faces` 的 `areas` 是**按降序**比的 ⇒ 期望值**必须从大到小**给，
  否则"数值没错、顺序反了"也会红（本次就红了一次）。

★ 可迁移：**"静默失败"不等于"瞬时失败"** —— §146A 的重试能救瞬时失败，
但**救不了几何本身非法**。判别法：**重试 N 次仍稳定复现 ⇒ 立刻转去查几何**，
而不是继续加重试次数。本案就是"加了重试仍红"才把方向从 COM 转向几何的。

## §151 ★★ 核心套件复跑的"会话拖垮"假红 + `os._exit` 类重启纪律（2026-10-12）

现场：同一天两次跑**同一份 51 探针套件**，红的东西**不一样**：
先红 `asm_ops`/`naming_cn`，后一轮红 `real_thread`×2 / `check_sketch`×2 / `split_line`，
还夹着「体积读成 **0.000mm³**」「`draft` 选不中面且耗时 **13.5s**」「`sketch_end` 从 0.35s 变成 3.2s」。

⇒ **"探针红了"第一件事不是改代码，是判断会话是否被拖垮**：
- 信号：体积恒 0、单步耗时突增数倍、选择类 op 集体失灵；
- 病因：**诊断脚本反复 `new_part` 却不关文档**（§136⑤）—— 我自己的 `_diag/dbg_*.py` 就犯了这条；
- 处置：`Stop-Process -Name SLDWORKS -Force` 重开新会话，再复跑。本案重开即恢复。

★ 纪律：**`_diag` 脚本里只要 `new_part`，就必须在结尾 `close`（或整段用一次性会话 + 强杀）**；
"跑完不留文档"要写进脚本模板，而不是靠记性。

## §152 ★★★ 探针**不要 `open` 外部构建产物** —— 会阻塞（SW 停在「查看中」）（2026-10-12）

现场：核心套件每到 `shot_crop` 就**卡死**（两次复现）。取证：
python 客户端 **CPU 8 秒零增长**（= 阻塞，不是在算）＋ SW `Responding=True`、
标题 `[crh380cl_nose_s5.SLDPRT [查看中]]`。日志停在探针标题、连第一步都没打。

病因：该探针第一句是 `{"op":"open","path":"D:/out/crh380cl_nose_s5.SLDPRT"}` ——
它 `open` 的是一个**构建产物**（另一个流程的输出），不是受控夹具。
产物一旦缺失/损坏/状态异常，`IModelDoc2` 的打开链就会**不返回**。

**处置（两件事）**：
1. **探针自包含**：改成 `new_part → 建模 → save_as → close → open` 回到自己刚存的文件；
   ★ 这样 `open` 的判据还能落到**重开后包围盒仍对**上（比"没报错"强）。
2. `op_save_as` 补 `path.parent.mkdir(parents=True, exist_ok=True)` ——
   与 `screenshot` 一致，否则"存到一个新文件夹"会失败，而计划作者以为是建模问题。

★ 可迁移：**测试夹具必须由测试自己造**。凡是 `open`/`import`/读外部路径的探针，
都要先问"**这个文件谁保证它在、且是好的**"——答不上来就自己造。
★ 且注意：`shot_crop` 是 `open` / `hide_refs` 的**唯一**覆盖者 ⇒
**"因为跑不动就删掉"会静默丢掉覆盖**（`com_matrix` 会显示 `uncovered` 变多）。



