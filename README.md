# solidworks-modeling-skills

> 用自然语言 / 图纸参数 / 录制的宏，驱动本机 **SolidWorks 2024**（COM API）自动完成三维建模。

这是一个 **WorkBuddy / CodeBuddy Skill**。它把用户的建模需求翻译成一份**可离线校验的声明式建模计划**（`yoyo.swplan/1` JSON），离线校验通过后再交给 SolidWorks 执行，产出 `.SLDPRT` 实体模型或 `.SLDASM` 装配体。

它不是 SolidWorks 插件 —— 而是一组独立的 Python 脚本，通过 **pywin32 后期绑定**调用 SolidWorks 的 COM API。

---

## 仓库结构

```
solidworks-modeling/
├── SKILL.md          技能入口（智能体读它就知道怎么用）
├── INSTALL.md        安装与验收说明
├── CHANGELOG.md      版本流水
├── scripts/          38 个脚本：sw_connect / sw_ops / sw_probe / sw_selftest / sw_doctor …
├── references/       13 份文档：API 手册、op 手册、30-pitfalls.md（踩坑清单 150+ 条）
├── assets/          137 个文件：op schema、真机探针用例、GB 标准、参数表
└── plans/            13 个示例建模计划
```

## 环境要求

| 项 | 要求 |
|---|---|
| 系统 | Windows |
| SolidWorks | 2024（默认 `C:/SW2024/SOLIDWORKS/`，可用环境变量 `SW_ROOT` 覆盖） |
| Python | 3.13 + `pywin32`（离线体检不需要 pywin32） |
| 产物目录 | 默认 `D:/out/`（可用 `YOYO_SW_OUT` 覆盖） |

## 快速开始

```bash
# 1) 放进技能目录
#    个人级：~/.workbuddy/skills/solidworks-modeling/
#    项目级：<项目>/.workbuddy/skills/solidworks-modeling/

# 2) 离线体检 —— 不需要 SolidWorks、不需要 pywin32
python scripts/sw_doctor.py

# 3) 真机验收 —— 会启动 SolidWorks，约 3~5 分钟
python scripts/sw_doctor.py --real
```

## 设计要点

- **判据落在副作用上**：不看"函数跑过了 / 没抛异常"，只看特征树 diff + 几何量（体积、包围盒）。返回值会说谎。
- **静默失败家族**：`CreateFeature` 系调用会**间歇性静默失败**（返回 None、特征树不涨）⇒ 一律套"特征树 diff 重试"。
- **离线可校验**：任何建模计划先用 `sw_ops.py --schema` / 静态校验跑通，再上真机。
- **长跑健壮性**：SolidWorks 长跑会偶发原生崩溃 ⇒ 分批重启 + 重试。
- 全部踩坑沉淀见 `references/30-pitfalls.md`。

## 许可

本仓库**未附许可证，默认保留所有权利（All rights reserved）**。如希望以某种开源协议分发，请补充 `LICENSE` 文件。
