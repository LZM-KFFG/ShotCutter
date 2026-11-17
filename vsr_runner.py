# -*- coding: utf-8 -*-
"""已废弃 —— VSR 已升级到 v1.4.0，统一入口为 <VSR_ROOT>\\resources\\run_with_area.py。

v1.1.1 时代这里做两件事：给 find_continuous_ranges 之类的空列表补保护、
按次切换 STTN_SKIP_DETECTION。v1.4.0 的入口脚本已经内置这些行为
（空检测有保护、detect 0/1 直接映射到 sttn-auto / sttn-det），
所以本文件只做参数转交，避免旧调用方报错。

用法不变： <VSR>\\Python\\python.exe vsr_runner.py <视频> <区域|none> [detect 0|1]
"""
import os
import subprocess
import sys


def main() -> int:
    root = os.environ.get("VSR_ROOT") or r"D:\Tools\VSR140"
    res = os.path.join(root, "resources")
    entry = os.path.join(res, "run_with_area.py")
    py = os.path.join(root, "Python", "python.exe")
    if not os.path.isfile(entry):
        print("[ERROR] 找不到新版入口：%s" % entry)
        return 2
    env = dict(os.environ)
    env.setdefault("QPT_MODE", "Running")
    return subprocess.call([py, entry] + sys.argv[1:], cwd=res, env=env)


if __name__ == "__main__":
    sys.exit(main())
