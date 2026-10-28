# -*- coding: utf-8 -*-
"""在 ShotCutter 里驱动 VSR 去字幕，不修改 D:\\Tools\\VSR 里的任何文件。

为什么不用 VSR 自带的 resources\\run_with_area.py：
1) 它无法按次切换「跳过字幕检测」，固定区域模式也得走一遍 OCR，白白慢一倍；
2) VSR 在「这一帧没检测到字幕」时会踩 `numbers[0]` 的 IndexError 直接退出，
   整条流程就断了。这里给它补一层空列表保护，没检测到就原样输出。

用法（必须用 VSR 自带的 Python 跑）：
    <VSR>\\Python\\python.exe vsr_runner.py <视频> <区域|none> [detect 0|1]
      区域   "ymin,ymax,xmin,xmax"，像素坐标；none = 全帧自动检测
      detect 1 = 逐帧找字幕（默认），0 = 跳过检测、整块重绘固定区域
环境变量 VSR_ROOT 可覆盖 D:\\Tools\\VSR。
"""
import multiprocessing
import os
import sys


def main() -> int:
    args = sys.argv[1:]
    sys.argv = [sys.argv[0]]          # 后面的库会 parse argv，先清干净
    if not args:
        print("usage: vsr_runner.py <video> <area|none> [detect 0|1]")
        return 2

    video = args[0]
    area_s = args[1] if len(args) > 1 else "none"
    detect = (args[2] if len(args) > 2 else "1").strip() != "0"

    root = os.environ.get("VSR_ROOT") or r"D:\Tools\VSR"
    res = os.path.join(root, "resources")
    if not os.path.isdir(res):
        print("[ERROR] VSR resources not found: %s" % res)
        return 2
    sys.path.insert(0, res)
    os.chdir(res)

    # 空检测结果会让 VSR 抛 IndexError，这里兜住：没找到字幕就当作没有，原样写回
    from backend.main import SubtitleDetect, SubtitleRemover

    for name in ("find_continuous_ranges", "find_continuous_ranges_with_same_mask", "filter_and_merge_intervals"):
        orig = getattr(SubtitleDetect, name, None)
        if orig is None:
            continue

        def make(fn):
            def safe(arg, *a, **kw):
                if not arg:
                    return []
                return fn(arg, *a, **kw)
            return staticmethod(safe)

        setattr(SubtitleDetect, name, make(orig))

    area = None
    if area_s.strip().lower() not in ("none", "null", ""):
        area = tuple(int(float(x)) for x in area_s.split(","))

    print("video=%s" % video)
    print("sub_area(ymin,ymax,xmin,xmax)=%s" % (str(area) if area else "None（全帧自动检测）"))
    print("detect=%s" % detect)

    sd = SubtitleRemover(video, sub_area=area)
    # __init__ 里会 importlib.reload(config)，所以开关必须在构造之后再设；
    # main.py 里是 `import config`（顶层模块，不是 backend.config），这里必须拿同一个对象
    import config as vsr_config
    vsr_config.STTN_SKIP_DETECTION = not detect
    print("STTN_SKIP_DETECTION=%s" % vsr_config.STTN_SKIP_DETECTION)
    sd.run()
    return 0


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn")
    sys.exit(main())
