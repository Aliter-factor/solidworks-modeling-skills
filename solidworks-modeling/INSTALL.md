# 安装与验收（给接手的人 / 另一个智能体看）

这是一个 **WorkBuddy Skill**：把自然语言或图纸参数翻译成一份声明式建模计划（JSON），
离线校验通过后，再驱动本机 SolidWorks 通过 COM 建出 `.SLDPRT`。

> 不是 SolidWorks 插件，不需要装进 SolidWorks。它是一组独立 Python 脚本，
> 通过 **pywin32 后期绑定**调用 SolidWorks 的 COM API。

---

## 1. 安装

把整个 `solidworks-modeling/` 目录放到技能目录下即可，**不用编译、不用安装依赖到系统**：

| 范围 | 放置位置 |
|---|---|
| 个人（推荐） | `~/.workbuddy/skills/solidworks-modeling/`（Windows：`C:\Users\<你>\.workbuddy\skills\`） |
| 项目内共享 | `<项目>/.workbuddy/skills/solidworks-modeling/` |

放好后，智能体读 `SKILL.md` 就知道怎么用；人读这份文件就够跑起来。

技能自带的脚本**只用 Python 标准库**（`sw_*.py`），只有真机建模和截图才需要额外依赖：

```bash
python -m pip install pywin32 Pillow -i https://pypi.tuna.tsinghua.edu.cn/simple
```

- `pywin32` —— 调 SolidWorks COM 必需
- `Pillow` —— 只用于把截图 BMP 转成 PNG / 拼总览图，可选

---

## 2. 验收：先跑这一条

```bash
python scripts/sw_doctor.py
```

**离线体检，不需要 SolidWorks、不需要 pywin32** —— 换任何机器、任何 CI 都能跑。
它会依次检查解释器、SolidWorks 环境、**1765 项离线断言**、**国标数据抽检**、
**减速器参数表交叉校核 + 机读 op 导出**、6 个模板的静态校验，
最后给一句「可用 / 不可用」的结论，退出码 0/1。

通过的样子：

```
[1] 解释器与依赖          Python 3.13 / 64 位 / pywin32 可导入
[3] 离线回归              sw_selftest.py 全量断言 — 通过 1765 项，失败 0 项
[4] 国标数据              gb_standard.json — 15 族 / 191 行；速查表快照与 JSON 同步
[4b] 减速器参数表         35 个零件 / 交叉校核 C1~C11 全过；参数化进度 9/35 件；op_schema.json 与代码同步
[5] 模板离线校验          6 个模板生成 + 静态校验 — 全部通过
     结论：可用 —— 20/21 通过，跳过 1 项
```

再看真机能不能建：

```bash
python scripts/sw_doctor.py --real      # 会启动 SolidWorks，约 3~5 分钟
```

它做四件事：连接冒烟 → 真建一个 80×40×10 的长方体 → 读包围盒核对尺寸 → 截图存 PNG。
四项全过，说明这条链路是真的通了（而不是"脚本没报错"）。

`--real` 的产物都落在临时目录里，跑完会自动关文档并清理；
SolidWorks 会**留着开着**（方便你直接看模型），它的 `~$` 锁文件可能让当次删不干净 ——
没关系，下一次体检开头会自动扫掉（`--keep-temp` 可全程保留产物以便排查）。

---

## 3. 手动跑一遍（想知道细节时）

```bash
# ① 取骨架
python scripts/sw_plan.py --template gear > my_gear.json

# ② 离线校验（几百毫秒，不碰 SolidWorks）
python scripts/sw_plan.py my_gear.json --check

# ③ 执行
python scripts/sw_plan.py my_gear.json --run --report out/report.json

# ④ 自查：截图看形状 + 读数看数值
python scripts/sw_shot.py    --doc D:/out/gear.SLDPRT --out D:/out/shots --views iso,front,right --grid
python scripts/sw_inspect.py --doc D:/out/gear.SLDPRT --json D:/out/insp.json
```

可用模板：`box` / `shaft` / `bolt` / `flange` / `gear` / `leadscrew`。

---

## 4. 环境变量（都不设也能跑）

| 变量 | 作用 | 不设时的默认 |
|---|---|---|
| `SW_ROOT` | SolidWorks 安装目录，**装到非默认路径时必须设** | 自动扫 `C:\SW20xx`、`C:\Program Files\SOLIDWORKS Corp` 等 |
| `YOYO_SW_OUT` | 产物落盘目录 | `D:/out`；该盘不存在则退到 `<当前目录>/out` |
| `APPDATA` | **只在"pywin32 装在用户级"时需要显式导出** | 系统默认 |

关于 `APPDATA`：如果 `pip install` 时没用管理员权限，pywin32 会装进
`%APPDATA%\Python\Python3xx\site-packages`，而这个路径**只在你显式给出 `APPDATA` 时才进 `sys.path`**。
症状是"明明装了却 `import win32com` 失败"：

```bash
export APPDATA="C:\Users\<你>\AppData\Roaming"
python scripts/sw_doctor.py
```

---

## 5. 副作用与安全（跑之前请知道）

这个技能会**真的操作 SolidWorks 和硬盘**，不是沙箱演示：

| 行为 | 说明 |
|---|---|
| 启动/连接 SolidWorks | 通过 COM 连本机已装或新起的 SolidWorks 进程 |
| 新建/打开/保存零件 | `save_as` **默认覆盖同名文件**（`overwrite=True`） |
| 删除特征 | 只在"自己刚建出来且报错"时回滚删除该特征，不动你原有的特征 |
| 写文件 | 产物 `.SLDPRT`、截图 `.png`、报告 `.json` 写到你指定的目录 |
| 网络 | **无**。脚本不联网、不下载、不上传，纯本机 COM + 文件 IO |

调用面可以直接自己复核（三条命令，都很快）：

```bash
grep -rn "requests\|urllib\|socket" scripts/    # 无输出 → 确认没有任何网络调用
grep -rn "subprocess" scripts/                  # 只有 sw_docbuild 调 Windows 自带的 expand.exe 解 CHM
grep -rn "unlink\|rmtree\|os.remove" scripts/   # 只有 sw_shot 删自己产生的 .bmp 中间文件
grep -rn "EditDelete\|DeleteAll" scripts/       # 只有"回滚自己刚建坏的特征"这一处
```

几个刻意的设计，避免它"乱来"：

- **弹窗即失败**：自动化里任何 SolidWorks 模态框都会永久卡死脚本，所以模板/命令都不会主动弹框。
- **不跳过校验**：`--check` 必须在 `--run` 之前过；校验器拦的是顺序错、单位错、参数缺、齿轮根切。
- 跑完记得关掉 SolidWorks，别留后台进程。

---

## 6. 常见问题

| 症状 | 原因 / 处理 |
|---|---|
| `import win32com` 失败 | 见上面 `APPDATA` 一节 |
| 连不上 / `DISP_E_TYPEMISMATCH` | Python 位数和 SolidWorks 不一致（必须同为 64 位） |
| `NewDocument` 卡住 | 没配零件模板：SolidWorks 里 工具 > 选项 > 系统选项 > 默认模板 指定一个 |
| 脚本说成功但文件夹没新文件 | `SaveAs` 到被占用路径会"返回成功但不落盘"，看 `sw_inspect` 的 `GetPathName` |
| 建出来的形状不对但没报错 | 先看 `references/30-pitfalls.md` §19~23（返回值会说谎、方向静默降级） |
| 键槽/切除只做了一半 | §21：`end_cond=ThroughAllBoth` 必须同时把 `Sd` 置 `False` |

---

## 7. 目录结构

```
solidworks-modeling/
├── SKILL.md                     ← 智能体入口（工作流、硬规则、状态）
├── INSTALL.md                   ← 本文件（人看的安装与验收）
├── CHANGELOG.md                 ← 版本变更
├── references/
│   ├── 00-overview.md           两条路线、调用顺序总则、选择标记表
│   ├── 10-operations.md         op 全表（71 个，由 sw_ops.py --write-md 自动生成）
│   ├── 15-feature-matrix.md     SW 特征/工具全量矩阵（12 族，标 API / 现状 / 坑）
│   ├── 20-dsl.md                计划 JSON 的字段与状态机
│   ├── 30-pitfalls.md           76 个坑（出事先翻这里）
│   ├── 40-standard-parts.md     ★ 国标入口：GB 尺寸表用法 + 齿轮/键槽/螺栓/法兰/轴承公式
│   ├── 41-gb-cheatsheet.md      ★ 国标速查全表（387 行，离线只读快照，自动生成）
│   ├── 50-training.md           新增 op 的三步法（录宏→翻译→回归）
│   ├── 60-reducer-project.md    一级减速器自动化建模方案
│   ├── 70-direction-review.md   开工前的方向对账
│   └── api-signatures.md        底层 API 签名（由本机类型库抽取）
├── scripts/
│   ├── sw_env.py                环境探测（**纯标准库**）
│   ├── sw_doctor.py             ★ 一条命令出结论（离线可跑；--real 追加真机）
│   ├── sw_plan.py               计划校验 / 执行 / 模板
│   ├── sw_ops.py                op 表（COM 接口层）；--write-md 重新生成 10-operations.md
│   ├── sw_connect.py            连接会话、晚期绑定封装
│   ├── sw_gear.py               几何算法（**纯数学，不依赖 SolidWorks**）
│   ├── sw_gb.py                 ★ 国标查询（**纯函数，不依赖 SolidWorks**）；
│   │                              --cheatsheet 打全表 / --write-md 生成 41 速查表
│   ├── sw_shot.py               截图自查
│   ├── sw_inspect.py            读数自查
│   ├── sw_faces.py              ★ 参照件取证（只读）：圆柱台阶表 / 平面站位表 / 锥面表
│   ├── sw_refscan.py            批量扫参照件目录出基准
│   ├── sw_reducer.py            ★ 减速器参数表：交叉校核 / 计划生成 / 与参照件对账
│   ├── sw_toolbox.py            Toolbox 参考库浏览
│   ├── sw_macro_to_plan.py      宏(.bas) → 计划
│   ├── sw_docbuild.py           从本机帮助文档重建 API 知识库
│   └── sw_selftest.py           全量回归（1765 项，离线、不碰 SolidWorks）
└── assets/
    ├── probes/                  43 个真机探针（sw_probe.py 跑）
    ├── plans/                   6 个模板的黄金样例计划
    ├── reports/                 真机跑出来的报告（备查）
    ├── reducer/                 ★ 减速器唯一真源：reducer.params.json + reference.json
    ├── op_schema.json           ★ 机读 op 导出（80 op / 19 带 limits，防漂移）
    ├── gb_standard.json         ★ 国标**唯一权威来源**（15 族 191 行 + 4 张非族表）
    ├── sw_enums.json            926 个枚举（从本机类型库抽取）
    └── sw_member_kind.json      成员是方法还是属性（晚期绑定的关键元数据）
```

---

## 8. 边界（不要拿它做这些）

- 只驱动 **SolidWorks**。UG / Creo / Inventor 不适用。
- 真机链路在 **SolidWorks 2024** 上验证。更早版本 API 大体兼容，但
  `references/api-signatures.md` 是从本机类型库抽的，换版本建议用
  `sw_docbuild.py` 重建。
- Toolbox **不能**按规格生成零件（规格是运行时数据库算的，官方 API 在后期绑定下取不到），
  只能当尺寸参考库 —— 见 `30-pitfalls.md` §23。
- 螺纹目前是**扫掠切除实体牙型**；装饰螺纹线（`InsertCosmeticThread3`）只用于螺栓那类标准件。
- ⛔ **尺寸标注能力已封存（2026-10-02）** —— `dim_auto` / `dimension` / `smart_dim` /
  `fully_define` 四个 op **不可用**（计划里出现即被 `sw_spec` 的 S3 拦下，执行期被
  `run_op` 拦下）。真机复现：在**非单调回转型面**（端盖那类）上逐段标注会弹出
  **模态确认框**，把自动化挂死（`RPC_E_DISCONNECTED`）**并且把型面驱动改坏**。
  ⇒ 当前只建几何：**建模阶段一条尺寸都不标**，几何正确性靠 `check_bbox` /
  `check_cyl_radius` / `check_mass` 兜；尺寸改由后续**「图纸绘制」**环节在**工程图**里标。
  细节与重开条件见 `SKILL.md` 首节 与 `30-pitfalls.md` §80。
  自检/诊断确需临时放行：step 写 `"allow_sealed": true`，或设 `YOYO_ALLOW_SEALED=1`。
