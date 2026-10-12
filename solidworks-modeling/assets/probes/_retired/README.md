# 已退役的探针（不要在这里跑）

这些探针**不是 op 覆盖探针**，而是**同一件事的反复迭代 / 一次性调试快照**。
2026-10-12 第一阶段（"把 COM 全部封装好"）清理时从 `assets/probes/` 隔离到这里。

> 与 `_sealed/` 的区别：`_sealed/` 是**主题被主动封存**（尺寸标注，暂时跑不了）；
> 这里是**同一主题的冗余副本**，正确的那份仍在 `assets/probes/`。

## 退役清单与理由

| 文件 | 主题 | 退役理由 | 在册的正确版本 |
| --- | --- | --- | --- |
| `coupling_half.json` | 半联轴器 v7 | v7 是**被推翻的旧方案**（圆柱毛坯），已由 v8 系列取代 | `coupling_half_v8g` |
| `coupling_half_v8.json` | 半联轴器 v8 | v8 被 v8b/v8g 取代 | `coupling_half_v8g` |
| `coupling_half_v8b.json` | 半联轴器 v8b | **已知死路**（`s9` 外廓草图落进空气 ⇒ 切除四方案全灭），已标 `expect_fail` | `coupling_half_v8g` |
| `coupling_half_v8g_sentinel.json` | 半联轴器 v8g + 哨兵 | v8g 的**加哨兵调试副本** | `coupling_half_v8g` |
| `coupling_half_v8h.json` | 半联轴器 v8h | v8g 的**微调副本** | `coupling_half_v8g` |
| `probe_v8_iso.json` | v8 最小复现 | 为定位 v8 切除问题做的**一次性最小复现** | `coupling_half_v8g` |
| `probe_v8_iso2.json` | v8 复现 2 | 同上 | `coupling_half_v8g` |
| `probe_v8_iso3.json` | v8 复现 3 | 同上 | `coupling_half_v8g` |
| `train_10_1_h13.json` | 训练题 10_1 v3 | 弧深 13 的**中间变体** | `train_10_1` |
| `train_10_1_h13b.json` | 训练题 10_1 v4 | 同上 | `train_10_1` |
| `train_10_1_h13c.json` | 训练题 10_1 v5 | 同上 | `train_10_1` |
| `probe_right_axis.json` | Right 面草图 u 轴符号 | 一次性调试；结论已固化进坑表 §136① | （知识在 `references/30-pitfalls.md`） |

## 为什么可以放心退役

- **不丢 op 覆盖**：这 12 个探针用到的 op，全部被 `assets/probes/` 里其它探针覆盖
  （退役前已用脚本核对：退役后 100 个 op 覆盖率**不变**，仅 `dim_auto`/`dimension` 因封存未覆盖）。
- **不丢知识**：v8b 死路、v8 迭代顺序（v7→v8→v8b→v8g→v8h）等结论仍在
  `references/30-pitfalls.md` §109⑦，CHANGELOG 亦留痕。
- **可逆**：文件只被**移入**本目录，未删除；要恢复只需搬回上一层。

## 重开条件

只有在**重新研究半联轴器 / 训练题 10_1 的中间路线**时才需要，届时把对应文件
搬回 `assets/probes/`，并确认其 `expect_fail` 标记仍然正确。
