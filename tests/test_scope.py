# -*- coding: utf-8 -*-
"""素材范围隔离自测：假池子 + 假项目，不起服务、不碰真实数据。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline as pipe

CASHEW = r"D:\素材\腰果\素材"
PERSIM = r"D:\素材\柿饼\素材"


def item(i, folder, name):
    p = folder + "\\" + name
    return {"id": i, "path": p, "name": name, "dur": 6.0, "start": 0.0,
            "index": 0, "tags": ["展示细节"], "tagged": True, "fp": ""}


ITEMS = [
    item("a1", CASHEW, "c1.mp4"), item("a2", CASHEW, "c2.mp4"),
    item("a3", CASHEW, "c3.mp4"),
    item("b1", PERSIM, "p1.mp4"), item("b2", PERSIM, "p2.mp4"),
]
pipe.pool_list = lambda only_tagged=False: [dict(x) for x in ITEMS]

CFG = pipe.load_cfg()


def mk_script(folders=None, with_plan=False):
    s = {"title": "腰果｜原声直剪",
         "tts": {"sentences": [
             {"text": "腰果又大又香", "start": 0.0, "end": 3.0, "hint": "展示细节"},
             {"text": "买两袋更划算", "start": 3.0, "end": 6.0, "hint": "展示细节"},
         ], "total": 6.0}}
    if folders:
        s["folders"] = list(folders)
    if with_plan:
        s["plan"] = {"clips": [{"path": PERSIM + "\\p1.mp4", "want": 2.0,
                                "item_id": "b1", "sentence": 0, "text": ""}]}
    return s


def dirs_of(plan):
    return sorted({str(Path(c["path"]).parent) for c in plan["clips"]})


ok = True
def check(name, cond, extra=""):
    global ok
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra and not cond else ""))
    ok = ok and bool(cond)


print("1) 脚本自带范围 → 只在腰果里挑")
p = {"id": "t", "folders": [PERSIM]}          # 项目范围故意设成柿饼，验证脚本范围优先
s = mk_script([CASHEW])
plan = pipe.align(p, s, CFG)
check("全在腰果目录", dirs_of(plan) == [CASHEW], str(dirs_of(plan)))
check("范围已固化到脚本", s.get("folders") == [CASHEW], str(s.get("folders")))

print("2) 换镜头不跑出范围")
for i in range(len(plan["clips"])):
    cur = pipe.swap_clip(p, s, i, CFG)
    check("镜头 %d 换后仍在腰果" % i, str(Path(cur["path"]).parent) == CASHEW, cur["path"])

print("3) 脚本没范围 → 用项目范围")
s2 = mk_script()
p2 = {"id": "t2", "folders": [CASHEW]}
plan2 = pipe.align(p2, s2, CFG)
check("全在腰果目录", dirs_of(plan2) == [CASHEW], str(dirs_of(plan2)))

print("4) 范围全无、名字也认不出商品 → 直接报错，不拿整池")
s4 = mk_script()
s4["title"] = "成片"
try:
    pipe.align({"id": "t3"}, s4, CFG)
    check("应当报错", False)
except RuntimeError as exc:
    check("报了明确的错", "素材来源" in str(exc), str(exc))

print("4b) 老脚本标题里带品名 → 按品名锁范围并标记 guessed")
s4b = mk_script()          # title = 腰果｜原声直剪
plan4b = pipe.align({"id": "t5"}, s4b, CFG)
check("锁到腰果", dirs_of(plan4b) == [CASHEW], str(dirs_of(plan4b)))
check("标记为猜的", plan4b["stats"]["guessed"] is True)

print("5) 老脚本没范围但已有镜头 → 换镜头只在同目录")
s3 = mk_script(with_plan=True)
try:
    pipe.swap_clip({"id": "t4"}, s3, 0, CFG)
    check("柿饼目录只有 1 条，应当换不动", False)
except RuntimeError as exc:
    check("提示同目录没别的素材", "目录" in str(exc), str(exc))

print("6) 给了范围但目录无效 → 不退回全池")
check("scope_items 返回空", pipe.scope_items(ITEMS, [r"D:\不存在的目录\素材"]) == [])

print("7) 没有范围的手动调用仍可用（素材池整体浏览）")
check("空范围返回全部", len(pipe.scope_items(ITEMS, [])) == 5)

print("\n结果：" + ("全部通过" if ok else "有失败项"))
sys.exit(0 if ok else 1)
