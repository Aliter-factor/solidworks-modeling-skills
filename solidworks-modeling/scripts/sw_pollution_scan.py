"""`sw_pollution_scan.py` —— **只读**扫描：脚本有没有把杂散特征写进**别人的**文档。

什么时候要跑它：本项目是挂在**用户那个活着的 SolidWorks 会话**里干活的。
如果构建期间有人在同一个会话里切了活动文档，建模操作就会落到**别人的模型**上
（2026-10-03 真机踩到，见 CHANGELOG 1.4.24 ⑤ / 坑表 §95 附带）。
跑完一轮真机脚本后想确认"有没有污染别人的文件"，就跑这个。

它按**可指名的特征名**查（探针/计划里显式写过的名字），因为只有我们的脚本会造出这些名字：
    AsmBox / AsmBoxSk / MateBox / MateBoxSk / ReproBox / ReproBoxSk

★ 只读：只用 GetTitle / GetPathName / FirstFeature / GetFirstSubFeature / Name / GetTypeName2，
  **不激活、不修改、不保存、不关文档、不动选择集**。
★ 慢是正常的：每个 COM 调用都要等 SolidWorks 应答；开着十几个文档时可能几分钟。

用法：
    export APPDATA="C:\\Users\\OMEN\\AppData\\Roaming"     # 否则 import win32com 报"没装"
    /c/Python313/python.exe scripts/sw_pollution_scan.py
"""

from __future__ import annotations

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/scripts")
import sw_connect  # noqa: E402
from sw_connect import member, typed  # noqa: E402

# 探针里显式命名过的特征 —— 只有探针会造出这些名字
SUSPECT = ("AsmBox", "AsmBoxSk", "MateBox", "MateBoxSk", "ReproBox", "ReproBoxSk")


def feature_names(m, limit: int = 500) -> list[str]:
    """递归列某文档的全部特征名（只读）。"""
    out: list[str] = []
    try:
        f = typed(member(m, "FirstFeature"), "IFeature")
    except Exception:                                              # noqa: BLE001
        return out
    stack = [f]
    while stack and len(out) < limit:
        cur = stack.pop()
        while cur is not None and len(out) < limit:
            with contextlib.suppress(Exception):
                out.append(str(member(cur, "Name") or ""))
            with contextlib.suppress(Exception):
                sub = typed(member(cur, "GetFirstSubFeature"), "IFeature")
                if sub is not None:
                    stack.append(sub)
            cur = typed(member(cur, "GetNextFeature"), "IFeature")
    return out


def main() -> None:
    sw = sw_connect.SwSession.open(visible=True)
    docs = list(member(sw.app, "GetDocuments") or ())
    print(f"已打开文档 {len(docs)} 个 —— 逐个查有没有探针留下的特征（只读）\n")
    hit_any = False
    for d in docs:
        title = path = "?"
        with contextlib.suppress(Exception):
            title = str(member(d, "GetTitle") or "?")
        with contextlib.suppress(Exception):
            path = str(member(d, "GetPathName") or "(未落盘)")
        names = feature_names(d)
        hits = [n for n in names if any(n.startswith(s) for s in SUSPECT)]
        flag = "  ★★ 命中" if hits else ""
        print(f"  · {title}")
        print(f"      path  = {path}")
        print(f"      特征数 = {len(names)}{flag}")
        if hits:
            hit_any = True
            for h in hits:
                print(f"      ★ 残留特征：{h!r}")
    print()
    if hit_any:
        print("⇒ 结论：**有**文档被写入了探针的特征。")
        print("   修法：在对应文档里删除那些特征（或 Ctrl+Z 撤销）后重新保存。")
        print("   ★ 只删上面点名的那些特征；不认识的别碰。")
    else:
        print("⇒ 结论：**没有**在任何已打开文档里发现探针特征。")
        print("   注意：若相关文档在跑歪时**没被打开**或**已被关闭**，本扫描看不到它；")
        print("   也可以按名字在 D:/tmp/probe、D:/out 下搜文件确认。")


if __name__ == "__main__":
    main()
