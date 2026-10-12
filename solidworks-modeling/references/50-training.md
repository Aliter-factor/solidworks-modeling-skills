# 50 · 训练与扩展：怎么教会它一个新特征

技能刚建好时只有 39 个 op。用户迟早会要「扫描」「放样」「钣金」「螺旋」这些。
下面是把它变强的标准流程 —— **核心思想是：不要靠记忆猜 SolidWorks 的参数，
让 SolidWorks 自己告诉你。**

## 为什么必须走宏

SolidWorks 有 1.4 万个 API 方法、900 多组枚举，而且很多方法有 20+ 个位置参数
（`FeatureExtrusion3` 有 23 个）。凭记忆写必然错，而且错了报错往往指向别处。

**录制宏是唯一不会撒谎的接口说明书**：宏里是 SolidWorks 自己生成的调用，
参数顺序、枚举、坐标全是对的。

## 三步训练法

### 第 1 步：手工做一遍 + 录宏

1. 打开 SolidWorks → `工具 > 宏 > 录制`
2. **只做要学的那一个特征**（不要一整套流程，一个特征一个 op）
3. 停止录制，保存成 `.swp`
4. 按 `Alt+F11` 打开 VBA 编辑器 → 双击模块 → `Ctrl+A` / `Ctrl+C`

> `.swp` 本身不能直接解析（里面是压缩过的 p-code，不是源码），
> 必须从 VBA 编辑器里把代码复制/导出成 `.bas`。

### 第 2 步：翻译成计划骨架

```bash
python sw_macro_to_plan.py --stdin < feature.bas        # 或直接给 .bas 文件
python sw_macro_to_plan.py feature.bas --out feature.json
```

翻译器会：

- 把米换算成毫米、弧度换算成度
- 把宏里记的**枚举数值**反查成名字（`6` → `swEndCondMidPlane`）
- 把 `SelectByID2(名字,"SKETCH")` 转成下一步拉伸的 `profile`
- `InsertSketch True` 是**切换**开关，按出现次数配对成 `sketch_begin` / `sketch_end`
- 处理 VBA 的数字类型后缀（`0#`、`1&`）
- 列出**所有没能翻译的调用** —— 这些是新 op 的候选

### 第 3 步：补 op + 加回归用例

1. 看 `--dry-run` 的输出，确认步骤序列和你要的动作一致
2. 缺失的调用 → 在 `scripts/sw_ops.py` 里加一个 op（模板见下）
3. 把完整流程写成一个计划，存到 `assets/plans/<名字>.json`
4. 在 `sw_plan.py` 的 `template()` 里加同一个模板（`sw_selftest` 会校验两者一致）
5. 跑 `python sw_selftest.py`，全绿才算完成
6. 有机会的话 `python sw_selftest.py --run` 真建一次

## 加一个新 op 的模板

```python
@op("sweep_boss", "扫描凸台", "feature",
    "沿路径扫描轮廓。**必须先选中轮廓草图（Mark=0）和路径（Mark=1）**。\n"
    "路径必须与轮廓平面相交；自相交路径会失败。",
    api=["IModelDocExtension.SelectByID2", "IFeatureManager.InsertProtrusionSweep2"],
    params={"profile": {"type": "str", "desc": "轮廓草图名"},
            "path": {"type": "str", "desc": "路径草图名"},
            "name": {"type": "str", "required": False}},
    requires=("sketch_closed", "has_feature"),
    effect=("has_feature",))
def op_sweep_boss(sw, step, ctx):
    ...
```

必填项的意义：

| 元素 | 作用 |
|---|---|
| `doc` | 写给下一个 AI 看的说明，把**坑**写进去（为什么、什么时候会失败）|
| `api` | 实际调用的 API 序列 —— 排错和教学都靠它 |
| `params` | 声明类型和单位；`unit="mm"/"deg"` 会被离线校验 |
| `requires` / `effect` | 状态机，让顺序错误在离线阶段就被拦住 |
| `one_of` | 参数组（如「给 r 或 d 之一」）|
| `verified` | 真在 SolidWorks 上跑通了才置 True |

写完记得重新生成操作手册并跑回归：

```bash
python sw_ops.py --write-md ../references/10-operations.md
python sw_selftest.py
```

## 加回归用例

`sw_selftest.py` 的五个测试组：

| 组 | 覆盖 |
|---|---|
| 1 操作表自洽 | 每个 op 有文档、状态名合法、单位合法、实现存在 |
| 2 几何 | 齿轮三个自检点、根切判据、斜齿换算、标准件表、反例抛错 |
| 3 正例计划 | `assets/plans/*.json` 全部通过校验 |
| 4 文档漂移 | CLI 模板 = 用例文件；操作手册 = `sw_ops.catalog()` |
| 5 反例计划 | 顺序错、缺参数、根切、拼错参数名、非法表达式**必须被拦下** |
| 6 宏翻译 | 预处理、切换配对、单位换算、枚举反查、结果自洽 |
| 7 真跑（`--run`）| 最小用例端到端跑一遍 |

**加新能力时优先往第 5 组加反例**。一个校验器如果没有反例测试，
很快就会退化成「永远返回通过」。

## 当前状态与待办

| 项目 | 状态 |
|---|---|
| 39 个 op | 规格齐全；`verified` 标记的是已按官方文档核对签名的，**尚未在真机全部跑通** |
| 齿轮几何 | 算法与自检已通过（离线） |
| 计划校验 | 435 项断言全绿 |
| 宏翻译 | 已用真实录制格式验证 |
| 真机端到端 | **未做** —— 本机缺 pywin32，装好后再跑 `sw_selftest.py --run` |

第一次真机运行时，最有价值的动作顺序：

1. `python sw_env.py` 把环境体检过掉（装 pywin32）
2. `python sw_connect.py --selftest` 做连接冒烟（新建零件 → 画圆 → 拉伸 → 读质量）
3. `python sw_plan.py --template box > box.json && python sw_plan.py box.json --run`
4. `python sw_plan.py --template gear > gear.json && python sw_plan.py gear.json --run`
5. 齿轮建出来后**人工看一眼**：齿形对不对、齿数对不对、齿宽对不对
6. 把跑通的 op 的 `verified` 改成 `True`，把没跑通的失败现象记回 `30-pitfalls.md`

## 与 YoYo 项目的关系

本技能的脚本是纯 Python 模块，可以直接被 YoYo 的 FastAPI 能力进程导入：

```python
# yoyo/plugins/solidworks.py（示意）
import sys
sys.path.insert(0, r"C:\Users\OMEN\.workbuddy\skills\solidworks-modeling\scripts")
import sw_plan, sw_ops, sw_gear

@app.post("/api/sw/plan/check")
def check(plan: dict):
    r = sw_plan.validate(plan, evaluate=True)
    return {"ok": r.ok, "errors": r.errors, "warnings": r.warnings}
```

**注意**：`SwSession` 用的是 COM，必须在能 `CoInitialize` 的线程里跑，
且 SolidWorks 会占据授权和界面焦点。建议放在独立线程 + 队列里，
不要让 HTTP 请求线程直接驱动建模（用户会看到窗口不停抢焦点）。

按 `docs/02-CAD集成方案` 的分级，这属于 **L2 程序接口层**，
优先级排在「离线读属性」之后 —— 因为实时建模会占授权且需要软件运行。
