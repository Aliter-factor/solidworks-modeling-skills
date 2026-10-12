# 已封存的探针（不要在这里跑）

这些探针的**主题就是尺寸标注**（`dimension` / `dim_auto` / `smart_dim` / `fully_define`），
而尺寸标注已于 **2026-10-02** 主动封存，所以它们暂时**跑不了**，也不该跑。

## 为什么封存

在**非单调回转型面**（端盖这类：法兰 → 台阶 → 轴承孔 → 毡圈槽 → 通孔，轴向进进出出）上
逐段标注时，SolidWorks 会弹出**模态确认框**（过定义 / 冲突尺寸的询问），自动化当场被挂死：

```
pywintypes.com_error: (-2147417848, '被调用的对象已与其客户端断开连接。')
```

用 `_diag/diag_cover1_dim.py` 剥掉 `dim_auto` 后重跑，同一型面**回转 100% 成功** ——
即：**几何是对的，坏在标注这一步**。而且被驱动坏之后实体会扭曲、读出 10.0075 这种歪值。

决策（用户）：**标注与建模解耦**。当前阶段只建几何；尺寸改由后续**图纸绘制**环节对接
（在**工程图**里标，而不是在建模草图里标）。

## 完整清单

| 文件 | 主题 | 退役原因 |
| --- | --- | --- |
| `dim_auto.json` | `dim_auto` 逐段标 | 主题即封存 op |
| `dim_auto_shaft.json` | `dim_auto` 台阶轴 | 同上 |
| `smart_dim.json` | `smart_dim`（= `dimension` 别名） | 同上 |
| `smart_dim_line.json` | `smart_dim` 直线 | 同上 |
| `dims.json` | `dimension` + `set_dimension` | 同上 |
| `spec_relations.json` | 几何关系 / 锚定对约束状态的影响 | 主题是标注链 |
| `sketch_constraint.json` | `fully_define` 能否收敛 | 主题即封存 op |
| `sketch_anchor_trap.json` | `fully_define` 撒谎的已知边界 | 同上 |

## 重开条件与方式

1. 找到一条**不弹模态框**的标注路径（例如：先补几何关系再标；
   或在弹框前用 API 拦截 / 关掉 SW 的交互式确认）。
2. 在真机上把**全部零件**重新跑绿（含非单调型面那几件）。
3. 把文件搬回 `assets/probes/`，并去掉计划里为放行写的 `allow_sealed: true`。

执行期也有两道闸门（不只是"搬文件"）：`sw_ops.OpSpec.sealed` + `sw_ops.run_op`；
计划期有 `sw_spec` 的 **S3**。临时放行：step 上写 `allow_sealed: true`，
或设环境变量 `YOYO_ALLOW_SEALED=1`。

## 封存期间的已知代价（记在明处）

- `set_dimension`（驱动**已有**尺寸的参数化能力）在新建计划里暂时没有尺寸可驱动 ——
  但只要模型里有尺寸（例如参照件、或工程图端建好的尺寸），它照样能用。
- 上述 8 条覆盖：标注 API 语义、`fully_define` 的已知边界、几何关系对约束状态的影响。
  重开前这段**没有回归保护**，改动相关代码要格外小心。
- 仍**保留在册**的两条（标注只是它们的"前置动作"，主体是别的事）：
  `naming_cn.json`（语言/命名）、`gear_m2z55.json`（渐开线齿廓 + 中心孔）——
  它们的标注 step 显式写了 `allow_sealed: true`，因为小样本形状不会触发模态框。
