# 20 · 建模计划 DSL（`yoyo.swplan/1`）

计划文件是一份**声明式的建模意图**，能被离线校验、再一次性执行。
它存在的意义：AI 和人可以只讨论「做什么」，不用同时操心「SolidWorks 怎么调」。

## 最小骨架

```json
{
  "schema": "yoyo.swplan/1",
  "meta":   { "name": "直齿圆柱齿轮", "units": "mm" },
  "params": { "mn": 2.0, "z": 55, "b": 20.0 },
  "steps": [
    { "id": "n",  "op": "new_part" },
    { "id": "p",  "op": "select_plane", "plane": "Front" },
    { "id": "s1", "op": "sketch_begin" },
    { "id": "g",  "op": "gear_profile", "mn": "{{ params.mn }}", "z": "{{ params.z }}" },
    { "id": "e1", "op": "sketch_end" },
    { "id": "x1", "op": "extrude_boss", "depth": "{{ params.b }}",
      "end_cond": "swEndCondMidPlane", "name": "GearBody" },
    { "id": "v1", "op": "check_bbox",
      "expect": ["{{ params.mn*(params.z+2) }}", "{{ params.mn*(params.z+2) }}",
                 "{{ params.b }}"] },
    { "id": "sv", "op": "save_as", "path": "D:\\out\\gear.SLDPRT" }
  ]
}
```

## 字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema` | 建议 | 固定 `yoyo.swplan/1` |
| `meta.name` | 否 | 计划名，出现在报告里 |
| `meta.units` | 否 | 一律 `mm`；op 参数就是毫米，写别的会误导 |
| `params` | 是 | 自由键值，供 `{{ }}` 表达式引用 |
| `steps` | 是 | 步骤数组，**顺序即执行顺序** |
| `steps[].id` | 建议 | 步骤标识，用于报错定位；重复会被拦下 |
| `steps[].op` | 是 | 操作名，必须是 `10-operations.md` 里有的 |
| `steps[].note` | 否 | 备注，执行时忽略 |

## 参数表达式

参数值可以是字面量，也可以是 `{{ 表达式 }}`：

```json
"depth": 20
"depth": "{{ params.b }}"
"depth": "{{ params.b * 1.2 }}"
"r":     "{{ params.s/2/cos(pi/6) }}"          ← 六角头外接圆半径
"expect": ["{{ params.od }}", "{{ params.od }}", "{{ params.t }}"]
```

规则：

- 表达式里能用 `params`（也支持 `P.` 简写）和 `math` 的全部函数（`pi`、`cos`、`sqrt`…）。
- **属性访问和下标都行**：`params.mn` 与 `params["mn"]` 等价。
- **只有纯算术**。内建函数、导入、属性链全被禁掉 —— 计划文件不是代码执行入口。
  写了 `__import__` 之类的会被校验器拦下。
- 参数名拼错会明确指出「当前可用参数：…」，不会静默变 0。

## 状态机：顺序校验的原理

每个 op 声明自己**要求什么状态**（`requires`）和**产生什么状态**（`effect`）。
状态只有四个词：

| 状态 | 含义 |
|---|---|
| `doc_open` | 有活动零件文档 |
| `sketch_open` | 正在编辑草图 |
| `sketch_closed` | 草图已关闭，可以做特征 |
| `has_feature` | 已有至少一个实体特征 |

`sketch_open` / `sketch_closed` 互斥。校验器按顺序走一遍 steps，任何一步
要求的条件不满足就报错，并打印**当时的状态集合**。

所以这些都会被离线拦下，不用去浪费 SolidWorks 的时间：

- 拉伸一个还没 `sketch_end` 的草图 → 「顺序错误：extrude_boss（拉伸凸台）需要 草图已关闭」
- 没 `new_part` 就 `select_plane` → 「需要 有活动零件文档」
- 计划结束时草图还开着 → 「计划结束时草图还开着（缺 sketch_end）」
- `extrude_cut` 但还没有任何实体 → 「需要 已有至少一个实体特征」

## 三种运行方式

```bash
python sw_plan.py plan.json --check      # 静态校验 + 顺序检查（秒级，开发时用这个）
python sw_plan.py plan.json --dry-run    # 额外求值表达式 + 打印每步的 API 序列
python sw_plan.py plan.json --run        # 真连 SolidWorks 执行，逐步计时，出报告
python sw_plan.py --template gear        # 打印现成骨架（gear/shaft/bolt/flange/box）
```

`--run` 的行为：

- **先跑一遍 `--check`**，不过就拒绝执行（免得把时间浪费在开软件上）
- 每步计时并打印，失败时报告「第几步、哪个 op、API 序列是什么」
- 报告可写到 `--report run.json`
- 默认遇错即停（`--continue-on-error` 可改）

## 写计划的检查清单

1. `new_part` 或 `open` 是第一步
2. 每个 `sketch_begin` 都有配对的 `sketch_end`
3. 每个草图只养一个特征
4. 特征之前有 `select_plane` 或明确的 `profile` 名字
5. 阵列/镜像给出了正确的参考名（轴、面）——这些名字来自上一步的 `name` 参数或
   `axis_from_planes`，不能瞎写
6. 至少有一个 `check_*`（否则警告）
7. 有 `save_as`（否则警告）
8. 尺寸用 `{{ params.xxx }}` 而不是硬编码 —— 这样才能改一个参数重建整个族

## 参数化的意义

把尺寸抽到 `params` 之后，同一份计划就是一个「零件族生成器」：

```bash
python -c "
import json,sw_plan
p=json.load(open('gear.json'))
for z in (30,45,55,80):
    p['params']['z']=z
    p['steps'][-2]['path']=f'D:\\\\out\\\\gear_z{z}.SLDPRT'
    r=sw_plan.validate(p,evaluate=True)
    print(z, 'OK' if r.ok else r.errors[:1])
"
```

（`gear_profile` 里 `strict=True` 会在根切等硬错误时直接中止 —— 这正是我们想要的：
宁可停下问人，也不要静默生成一个根切的齿轮。）
