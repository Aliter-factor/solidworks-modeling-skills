# 50 · 官方练习题库（Practice Problems）作为训练/评测语料

> 语料来源：[SOLIDWORKS Education · Practice Problems](https://www.solidworks.com/solution/education/practice-problems)
> 本地副本：`C:\Users\OMEN\Downloads\d\PracticeProblems_ALL`（可用环境变量 `YOYO_PRACTICE_ROOT` 覆盖）
> 索引：`assets/practice/index.json`（由 `scripts/sw_practice.py --scan` 生成）
> 定位：**评测 / 能力盘点语料**，不是"再喂给模型一批数据"。

---

## 1. 为什么这份语料对我们特别合手

因为题库里**每道题自带正确答案**：

| PDF 里的字段 | 对我们的意义 |
|---|---|
| 正交/轴测**图纸视图**（带尺寸） | 这就是"输入"——读图建模 |
| `Volume: N mm3` | ★ **判卷靶值**，正是 `check_mass` 需要的靶 |
| Suggested Sketch Tools / Features、`HINT:` | 参考解法路径（可以**故意不看**，用来测泛化） |
| Difficulty / Certification / Duration | 难度分层与门槛（CSWA=基础关，CSWP=进阶关） |

⇒ 「读图 → 出 `yoyo.swplan/1` → 真机建模 → 比体积」这条链路的**每一环我们都已具备**
（`sw_plan` / `sw_probe` / `check_mass`），只差一个把题库串起来的**判卷壳**。
域内 38 个减速器参照件是"我们自己项目的题"，题库是"通用题海"——两者互补。

---

## 2. 实测清点（2026-10-02，非估计）

```
题目记录 376 条（= 344 份 PDF + 32 条 zip 记录）
  带机读体积靶值   308   ← 我们现在的判卷覆盖面
  缺靶值            68   ← 其中 32 条是 zip 记录（与同题 PDF 重复）
关卡 18 关，每关题量：

  L1  Basic Sketch and Extrusion          20 题   靶值 20/20
  L2  Sketch Tools / End Conditions       20 题   靶值 20/20
  L3  Global Variables / Sketch Patterns   8 题   靶值  8/8
  L4  Extrude Cut / Fillet / Chamfer      70 题   靶值 70/70   ← 最大存量
  L5  Reference Geometry                  15 题   靶值 15/15
  L6  Revolve Boss / Cut                  20 题   靶值 20/20   ← 我们的新射程
  L7  Feature Patterns                    48 题   靶值 48/48
  L8  Sweep Boss / Cut                    14 题   靶值 14/14
  L9  Assemblies / Mates                  26 题   靶值  0/26   ← 装配线，另立项
  L10 CSWA Exam Level                     19 题   靶值 19/19
  L11 Hole Wizard                         12 题   靶值  8/12
  L12 Draft                                9 题   靶值  8/9
  L13 Shell                               13 题   靶值 13/13
  L14 Rib                                  9 题   靶值  6/9
  L15 Configurations                      17 题   靶值  4/17   ← 体积在表里
  L16 Global Variables / Equations         7 题   靶值  0/7
  L17 Assembly Tools                      14 题   靶值  2/14
  L18 CSWP Exam Level                     35 题   靶值 33/35

单位制：MMGS 323 / IPS 20（★ 20 道英寸题——是**故意留着**的泛化测试面）
认证：  CSWA 250 / CSWP 93 / 未标 33
难度：  Easy 185 / Medium 118 / Hard 22 / Moderate 18
体积原单位：mm3 296 / in3 11 / cm3 1（索引已统一换算为 `volume_mm3`）

"没有数字靶值"的原因（**如实记因，不糊成 null**，索引里的 `volume_kind`）：
  value 308 · see_table 11 · none 15 · assembly 7 · multi_config 2 · see_drawing 1
```

**一句话**：`L1~L8 + L10 + L13` 共 **247 道题，靶值 100% 齐**——这就是第一阶段的训练面。

---

## 3. 能力盘点：题库 vs 现有 op（真实覆盖，不含乐观估计）

现有 **80 个 op**（`sw_ops.py`）。按关卡对照：

| 关 | 主题 | 现有覆盖 | 缺口 |
|---|---|---|---|
| L1 | 基础草图 + 拉伸 | `line/rect_center/rect_corner/circle/sketch_begin/sketch_end` + `extrude_boss` | — |
| L2 | 草图工具 / 终止条件 | 同上 + 草图圆角/剪裁 | `extrude_boss` 的**终止条件**（到面/到顶点/两侧对称）需逐条验证 |
| L3 | 全局变量 / 草图阵列 | `sketch_circular_pattern` `sk_mirror` `sk_offset` | ★ **全局变量/链接数值**缺 |
| L4 | 拉伸切除 / 圆角 / 倒角 | `extrude_cut` `fillet` `chamfer` | — |
| L5 | 参考几何 | `ref_plane` `plane_offset` `axis_from_planes` | — |
| L6 | 旋转凸台/切除 | `revolve_boss` `revolve_cut` `arc3p` `centerline` `polyline` | — ★ 本版刚补齐（`sw_profile` 反解型面） |
| L7 | 特征阵列 | `linear_pattern` `circular_pattern` `mirror` `mirror_body` | 阵列的**跳过实例/实例驱动**待验 |
| L8 | 扫描 | `swept_boss` `cut_swept` `helix` | 扫描的**扭转/沿路径**变体待验 |
| L9 | 装配 / 配合 | — | ★★ **完全缺**（无 mate / 无装配体 op） |
| L10 | CSWA 综合 | 上述合集 | — |
| L11 | 异型孔向导 | `hole_wizard` | **已知边界**：建不出盲孔/螺纹孔（见 `--limits hole_wizard`） |
| L12 | 拔模 | `draft` | — |
| L13 | 抽壳 | `shell` | — |
| L14 | 筋 | `rib` | 已知静默坑：`IsNormToSketch=False` 等（pitfalls） |
| L15 | 配置 | — | ★★ **完全缺**（无 configuration op） |
| L16 | 全局变量 / 方程式 | — | ★★ **完全缺** |
| L17 | 装配工具 | — | ★★ 同 L9 |
| L18 | CSWP 综合 | 上述合集 | — |

**结论**：零件线（L1~L8、L10~L14、L18）**基本在射程内**，缺的是少数**终止条件/阵列变体**与**全局变量**；
**装配线（L9/L17）与配置（L15/L16）是三个真空洞**，需专门立项。

---

## 4. 分阶段计划

### 阶段 0 · 语料固化（**本版已完成**）
- [x] `scripts/sw_practice.py` 扫描器 + `assets/practice/index.json`
- [x] 单位识别（mm3/in3/cm3，统一换算）+ 缺靶值记因（`volume_kind`）
- [x] `--task <题号>` 出任务单（含靶值 + 空计划骨架，**不给解法**）
- [ ] 把语料目录登记进 `INSTALL.md`（外部依赖，不入包）

### 阶段 1 · 判卷壳（下一步做）
新增 `scripts/sw_practice_run.py`：
1. `--task 6.1` 取任务单 → `--plan <手写/Agent产出的 .swplan>`
2. 离线 `sw_spec` 过 lint → 真机 `sw_probe` 跑
3. 读回体积/包围盒 → 与 `expect.volume_mm3` 比（默认 ±1%）
4. 出一张**成绩单** `D:/out/practice/score_<题号>.json`：
   `{pass, volume_expected, volume_actual, dev_pct, failed_step, notes}`
5. **批量模式**：`--suite L6 --limit 5` 连跑一批，汇总通过率

> 判据同项目口径：**不猜、量出来**。体积对不上就是错了（漏特征 / 读错尺寸 / 布尔方向反）。

### 阶段 2 · 用题库**倒逼**现有 op 的可靠性（价值最高）
题库的最大用处不是"多建几个件"，而是**用它把现有 op 打穿**。
本项目已经有过先例：正是减速器 `cover1` 那种非单调回转型面，
把 `dim_auto` 在真机上"弹模态框挂死"这个隐藏缺陷炸了出来（见 §80）。

计划：对 L1/L4/L6/L7/L10 各取 5 题做**金标准试跑**，凡失败**按"是 op 缺陷还是读图错"分类归档**，
op 缺陷立刻修 + 补进 `sw_selftest.py` 的反例。

### 阶段 3 · 按需扩 op（由题库缺口驱动，不预先造）
优先级由「题量 × 缺口」定：
1. **装配 mates**（L9 26 题 + L17 14 题 = 40 题）→ 恰好也是我们**减速器装配体**的必需能力
2. **配置**（L15 17 题）→ 减速器"双配置/多规格"也用得上
3. **全局变量 / 方程式**（L3 + L16 = 15 题）→ 参数化改型的底座
4. **终止条件 / 阵列变体**（L2/L7 的剩余部分）

### 阶段 4 · 回归基线
把已通关的题**固化成回归集**（题号 + 计划 + 期望体积），
每次改 op 后重跑，防止"修 A 坏 B"。

---

## 5. 边界与风险（必须写清）

- **版权**：这是 Dassault 的教学资料（页脚明确 "Cannot be reproduced for sale nor incorporated"）。
  ⇒ 仅作**本地训练/评测**用，**不进发行的 zip 包**、不再分发。索引里只存**元数据与文件路径**，不内嵌图纸。
- **单位**：20 道 IPS 题。建模前必须 `Units: IPS`（否则体积差 16387 倍）。索引已记 `units`。
- **靶值笔误**：`7.5` 印成 `cm3` 但数值量级像 `mm3`（已标 `volume_suspect`）⇒ 用前人工裁决。
- **多配置题**（`volume_kind=see_table`，11 道）：靶值在图/表里，需要表格解析，**先不做**。
- **装配题**没有单一零件体积 ⇒ 判据得换成"装配质量/干涉/配合数"，另设计。
- **别让语料挤掉主线**：减速器建模是当前任务，本语料**只在阶段 1/2 的判卷壳里用**，不作主线。

---

## 6. 常用命令

```bash
export PY_PRACTICE="C:/Users/OMEN/.workbuddy/binaries/python/envs/yoyo/Scripts/python.exe"   # 带 pymupdf
$PY_PRACTICE scripts/sw_practice.py --stats                       # 清点
$PY_PRACTICE scripts/sw_practice.py --list --level 6              # 列 L6 的题
$PY_PRACTICE scripts/sw_practice.py --list --cert CSWA --only-volume --limit 20
$PY_PRACTICE scripts/sw_practice.py --task 6.1 --out-dir D:/out/practice   # 出任务单
```
