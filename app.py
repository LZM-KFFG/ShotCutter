#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""镜头切割器 —— PySceneDetect + ffmpeg 的本地可视化外壳

只监听 127.0.0.1，视频不上传外网。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote, quote

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
WORK = ROOT / "_work"
UPLOADS = WORK / "uploads"
THUMBS = WORK / "thumbs"
EXPORTS = WORK / "exports"
PROXIES = WORK / "proxies"
for _p in (UPLOADS, THUMBS, EXPORTS, PROXIES):
    _p.mkdir(parents=True, exist_ok=True)

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv", ".mpg", ".mpeg", ".m2ts"}

MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".ass": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".zip": "application/zip",
}


def find_ffmpeg() -> str | None:
    cands = [ROOT / ".venv" / "Scripts" / "ffmpeg.exe", ROOT / ".venv" / "Scripts" / "ffmpeg",
             # 免安装包：ffmpeg 放在 tools/ 或跟 python 运行时放一起
             ROOT / "tools" / "ffmpeg.exe", ROOT / "python" / "ffmpeg.exe",
             ROOT / "ffmpeg.exe"]
    which = shutil.which("ffmpeg")
    if which:
        cands.append(Path(which))
    for c in cands:
        if c and Path(c).exists():
            return str(c)
    return None


FFMPEG = find_ffmpeg()

NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

import pipeline as pipe  # noqa: E402

pipe.configure(FFMPEG, NO_WINDOW)

import director as dirw  # noqa: E402  （一键导演，依赖 pipe 的 ffmpeg 配置）
import qc as qcmod       # noqa: E402  （成片体检：黑边/字幕/静音/音量）


def _log_open(msg: str) -> None:
    try:
        with (WORK / "openfolder.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def _unlock_foreground() -> None:
    """解除 Windows 前台锁定：我们的服务没有自己的前台窗口，从这里调起的
    资源管理器会被系统压在用户正在用的窗口后面。先模拟一次 ALT 键按下/抬起，
    系统就认为「用户刚输入过」，允许把新窗口带到最上层。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.keybd_event(0x12, 0, 0, 0)        # ALT down
        user32.keybd_event(0x12, 0, 0x0002, 0)   # ALT up (KEYEVENTF_KEYUP)
    except Exception:  # noqa: BLE001
        pass


def open_in_explorer(target: str | None) -> dict:
    """在资源管理器中打开目录 / 定位文件。

    返回 {ok, path, fallback?/error}。fallback=True 表示原目标不存在、
    实际打开的是它最近的上级目录——前端要提示出来，否则就是「打开的文件夹不对」。
    """
    raw = (target or "").strip().strip('"').strip("'")
    p = Path(raw) if raw else EXPORTS
    try:
        p = p.expanduser()
        if not p.is_absolute():
            p = (ROOT / p)
        p = Path(os.path.normpath(str(p)))
    except Exception as exc:
        _log_open(f"FAIL 规范化路径出错 raw={raw!r} err={exc}")
        return {"ok": False, "error": f"路径无效：{raw}"}

    want = str(p)
    fell_back = False
    if not p.exists():
        # 逐级回退到最近仍存在的父目录，比直接报错更有用
        q = p
        while q.parent != q and not q.exists():
            q = q.parent
        _log_open(f"MISS {p} -> fallback {q}")
        if not q.exists():
            return {"ok": False, "error": f"目录不存在：{p}"}
        p = q
        fell_back = str(p) != want

    target_s = str(p)
    is_dir = p.is_dir()
    last_err = ""

    # 目录一定先走 ShellExecute：explorer.exe 如果带着 CREATE_NO_WINDOW 启动，
    # 它会把命令行参数丢掉，直接打开默认位置（桌面）——这就是「打开的文件夹不对」的真凶。
    if is_dir:
        try:
            _unlock_foreground()
            os.startfile(target_s)  # noqa: S606
            _log_open(f"OK   {target_s}  via=startfile(dir)")
            return {"ok": True, "path": target_s, "fallback": fell_back}
        except Exception as exc:  # noqa: BLE001
            last_err = f"startfile: {exc}"
            _log_open(f"FAIL {target_s}  via=startfile(dir) err={exc}")

    # 文件用 /select, 在父目录里选中；explorer 本身没有控制台，**绝不能带
    # STARTF_USESHOWWINDOW/SW_HIDE**——explorer 会照着这个 show 状态开窗，
    # 窗口直接被藏起来，看起来就是「点了没反应」。SW_HIDE 只用于 cmd 兜底。
    cmds = [["explorer.exe", f"/select,{target_s}"], ["cmd", "/c", "start", "", target_s]]
    _unlock_foreground()

    si = None
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0          # 只给 cmd 兜底用，藏掉它的控制台窗口

    for argv in cmds:
        try:
            proc = subprocess.Popen(
                argv, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                startupinfo=(si if argv[0] == "cmd" else None),
            )
        except Exception as exc:
            last_err = f"{argv[0]}: {exc}"
            _log_open(f"FAIL {target_s}  via={argv[0]} err={exc}")
            continue
        try:
            rc = proc.wait(timeout=2.5)
        except subprocess.TimeoutExpired:
            # 还活着 = explorer 正作为常驻 shell 服务，视为成功
            _log_open(f"OK   {target_s}  via={argv[0]} (running pid={proc.pid})")
            return {"ok": True, "path": target_s, "fallback": fell_back}
        # explorer.exe 正常打开也返回 1，只有明显异常码才继续尝试下一种方式
        if rc in (0, 1):
            _log_open(f"OK   {target_s}  via={argv[0]} rc={rc} is_dir={is_dir}")
            return {"ok": True, "path": target_s, "fallback": fell_back}
        last_err = f"{argv[0]} 退出码 {rc}"
        _log_open(f"FAIL {target_s}  via={argv[0]} rc={rc}")

    try:
        _unlock_foreground()
        os.startfile(target_s)  # noqa: S606
        _log_open(f"OK   {target_s}  via=startfile")
        return {"ok": True, "path": target_s, "fallback": fell_back}
    except Exception as exc:
        last_err = last_err or str(exc)
        _log_open(f"FAIL {target_s}  via=startfile err={exc}")
        return {"ok": False, "error": f"无法调起资源管理器（{last_err}）"}


VIDEOS: dict[str, dict] = {}
JOBS: dict[str, dict] = {}
PROXY_JOBS: dict[str, str] = {}
LOCK = threading.Lock()


def new_job(kind: str, total: int) -> dict:
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid,
        "kind": kind,
        "state": "running",
        "progress": 0.0,
        "done": 0,
        "total": max(1, total),
        "message": "准备中…",
        "results": [],
        "files": [],
        "error": "",
        "t0": time.time(),
        # 任务控制：cancel=停止，pause=暂停（在单元边界生效）
        "cancel": False,
        "pause": False,
    }
    with LOCK:
        JOBS[jid] = job
    return job


def push_log(job: dict, msg: str) -> None:
    job["message"] = msg


class _BatchJob(dict):
    """把单条渲染的进度折算进整批，其余字段直通真实 job。

    直接传真实 job：每条渲染都会把 progress 从 0 跑到 1，进度条来回跳。
    传副本又不行：compose 里 job_gate 读的 cancel/pause 必须实时反映
    /api/job/control 写进去的值，副本读不到。
    """

    def __init__(self, base: dict, k: int, n: int):
        self._base = base
        self._k = int(k)
        self._n = max(1, int(n))

    def __getitem__(self, key):
        return self._base[key]

    def __setitem__(self, key, val):
        if key == "progress":
            try:
                inner = min(1.0, max(0.0, float(val or 0)))
            except (TypeError, ValueError):
                inner = 0.0
            val = (self._k + inner) / self._n
        elif key == "message":
            val = "[%d/%d] %s" % (self._k + 1, self._n, val)
        self._base[key] = val

    def get(self, key, default=None):
        return self._base.get(key, default)

    def __contains__(self, key):
        return key in self._base

    def setdefault(self, key, default=None):
        return self._base.setdefault(key, default)

    def update(self, *a, **kw):
        for kk, vv in dict(*a, **kw).items():
            self[kk] = vv


def _run_job(job: dict, fn, *args, **kwargs) -> None:
    """在线程里跑一个步骤，兜住异常，保证前端永远能收到终态。"""
    try:
        fn(*args, **kwargs)
        if job.get("state") == "running":
            job["state"] = "done"
            job["progress"] = 1.0
    except pipe.JobCancelled:
        job["state"] = "cancel"
        msg = str(job.get("message") or "")
        if not msg or msg.startswith("正在停止"):
            job["message"] = "已停止"

    except Exception as exc:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(exc)
        job["message"] = "失败：%s" % exc
    finally:
        job["elapsed"] = round(time.time() - job["t0"], 1)


# ---------------------------------------------------------------- 检测

DETECTORS = {
    "content": {"label": "内容检测", "default": 27.0, "min": 5.0, "max": 90.0, "step": 0.5,
                "hint": "最经典，逐帧比对 HSL 差异。适合剪辑规整、硬切明确的素材。"},
    "adaptive": {"label": "自适应检测", "default": 3.0, "min": 1.0, "max": 8.0, "step": 0.1,
                 "hint": "推荐默认。在内容检测上加了滚动均值，运镜/手持抖动时误切更少。"},
    "threshold": {"label": "阈值（淡入淡出）", "default": 12.0, "min": 3.0, "max": 40.0, "step": 1.0,
                  "hint": "按平均亮度判断，专治淡入淡出、黑场过渡。叠化转场用它。"},
    "hist": {"label": "直方图", "default": 0.2, "min": 0.05, "max": 0.9, "step": 0.01,
             "hint": "YUV 直方图差值，对整体光照变化不敏感。"},
    "hash": {"label": "感知哈希", "default": 0.35, "min": 0.1, "max": 0.9, "step": 0.01,
             "hint": "pHashed 相似度，速度快，适合大批量粗筛。"},
}


def build_detector(name: str, threshold: float, min_len_frames: int, fps: float):
    from scenedetect.detectors import (
        AdaptiveDetector, ContentDetector, HashDetector, HistogramDetector, ThresholdDetector,
    )
    name = (name or "adaptive").lower()
    if name == "content":
        return ContentDetector(threshold=threshold, min_scene_len=min_len_frames)
    if name == "threshold":
        return ThresholdDetector(threshold=threshold, min_scene_len=min_len_frames)
    if name == "hist":
        return HistogramDetector(threshold=threshold, min_scene_len=min_len_frames)
    if name == "hash":
        return HashDetector(threshold=threshold, min_scene_len=min_len_frames)
    return AdaptiveDetector(adaptive_threshold=threshold, min_scene_len=min_len_frames)


def make_thumbs(token: str, path: str, scenes: list[dict], width: int = 400) -> None:
    import cv2
    need = [sc for sc in scenes if not (THUMBS / f"{token}_{thumb_key(sc['start'])}.jpg").exists()]
    if not need:
        return
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return
    for sc in need:
        target = THUMBS / f"{token}_{thumb_key(sc['start'])}.jpg"
        cap.set(cv2.CAP_PROP_POS_MSEC, (sc["start"] + 0.10) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(sc["start"] * sc.get("fps", 25)))
            ok, frame = cap.read()
        if not ok or frame is None:
            continue
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(target), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    cap.release()


def thumb_key(start: float) -> int:
    return int(round(start * 1000))


def timecode(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms >= 1000:
        ms = 0
        s += 1
        if s >= 60:
            s = 0
            m += 1
            if m >= 60:
                m = 0
                h += 1
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def build_scenes(video: dict, cuts: list) -> list[dict]:
    token = video["token"]
    fps = float(video["fps"])
    dur = float(video["duration"])
    clean: list[float] = []
    for c in cuts or []:
        try:
            c = float(c)
        except (TypeError, ValueError):
            continue
        if 0.04 < c < dur - 0.04:
            clean.append(round(c, 3))
    clean = sorted(set(clean))
    bounds = [0.0] + clean + [dur]
    scenes = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        scenes.append({
            "i": i + 1,
            "start": round(s, 3),
            "end": round(e, 3),
            "start_tc": timecode(s),
            "end_tc": timecode(e),
            "dur": round(e - s, 3),
            "fps": fps,
            "thumb": f"/thumb?token={token}&t={thumb_key(s)}",
        })
    video["cuts"] = clean
    video["scenes"] = scenes
    return scenes


def analyze_one(video: dict, params: dict, job: dict, slot: int) -> dict:
    from scenedetect import SceneManager, open_video

    path = video["path"]
    video_obj = open_video(path, backend="opencv")
    fps = float(video_obj.frame_rate)
    try:
        total_frames = int(video_obj.duration.frame_num)
    except Exception:
        total_frames = int(video_obj.duration.get_frames())

    name = params.get("detector", "adaptive")
    min_len_frames = max(1, int(round(float(params.get("min_len", 0.6)) * fps)))
    detector = build_detector(name, float(params.get("threshold", DETECTORS.get(name, DETECTORS["adaptive"])["default"])),
                              min_len_frames, fps)
    frame_skip = int(params.get("frame_skip", 0) or 0)

    sm = SceneManager()
    sm.add_detector(detector)

    state = {"n": 0}

    def cb(_img, frame_num):
        state["n"] += 1
        if state["n"] % 6 == 0 and total_frames:
            frac = min(0.99, getattr(frame_num, "frame_num", state["n"]) / total_frames)
            job["progress"] = (slot + frac) / max(1, job["total"])
            job["message"] = f"{video['name']}　检测中 {frac * 100:.0f}%"

    sm.detect_scenes(video=video_obj, show_progress=False, callback=cb, frame_skip=frame_skip)
    scene_list = sm.get_scene_list()

    duration = round(total_frames / fps, 3) if fps else 0.0
    video.update({
        "fps": round(fps, 4),
        "frames": total_frames,
        "duration": duration,
        "detector": name,
        "threshold": params.get("threshold"),
        "min_len": params.get("min_len"),
        "analyzed_at": time.time(),
    })
    cuts = [float(sc[0].seconds) for sc in scene_list[1:]]
    build_scenes(video, cuts)
    make_thumbs(video["token"], path, video["scenes"])
    return video


def analyze_worker(tokens: list[str], params: dict, job: dict) -> None:
    try:
        for slot, token in enumerate(tokens):
            video = VIDEOS.get(token)
            if not video:
                job["results"].append({"token": token, "ok": False, "error": "视频已失效，请重新添加"})
                continue
            try:
                push_log(job, f"{video['name']}　分析中…")
                analyze_one(video, params, job, slot)
                job["results"].append({"token": token, "ok": True, "video": video})
            except Exception as exc:  # noqa: BLE001
                job["results"].append({"token": token, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            job["done"] = slot + 1
            job["progress"] = (slot + 1) / max(1, job["total"])
        job["state"] = "done"
        job["message"] = "检测完成"
    except Exception as exc:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(exc)
    finally:
        job["elapsed"] = round(time.time() - job["t0"], 1)


# ---------------------------------------------------------------- 导出

def safe_stem(name: str) -> str:
    stem = Path(name).stem
    return re.sub(r'[\\/:*?"<>|]+', "_", stem).strip() or "clip"


def default_out_dir(video: dict) -> str:
    src = Path(video["path"])
    if video.get("uploaded"):
        return str(EXPORTS / safe_stem(video["name"]))
    return str(src.parent / "镜头切片" / safe_stem(video["name"]))


def export_one(video: dict, keep: list[int], out_dir: Path, mode: str, job: dict, base: int, total: int,
               p_from: float = 0.0) -> None:
    src = video["path"]
    ext = Path(src).suffix.lower() or ".mp4"
    if ext not in {".mp4", ".mkv", ".mov", ".webm"}:
        ext = ".mp4"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_stem(video["name"])
    scenes = video["scenes"]
    for n, idx in enumerate(keep):
        sc = scenes[idx]
        out = out_dir / f"{stem}_{sc['i']:03d}{ext}"
        cmd = [FFMPEG, "-v", "error", "-y", "-ss", f"{sc['start']:.3f}", "-i", src,
               "-t", f"{max(0.04, sc['dur']):.3f}", "-sn", "-map", "0:v:0", "-map", "0:a?"]
        if mode == "copy":
            cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
        else:
            cmd += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-c:a", "aac", "-b:a", "192k"]
        cmd.append(str(out))
        proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=1800)
        if proc.returncode != 0:
            job["files"].append({"path": str(out), "ok": False, "error": (proc.stderr or "").strip()[:300]})
        else:
            job["files"].append({"path": str(out), "ok": True,
                                 "size": out.stat().st_size if out.exists() else 0})
        job["done"] = base + n + 1
        job["progress"] = p_from + (1 - p_from) * job["done"] / max(1, total)
        job["message"] = f"导出中 {job['done']}/{total}"


def export_worker(items: list[dict], mode: str, out_dir: str | None, job: dict,
                  dedup: str | None = None) -> None:
    try:
        total = sum(len(it.get("keep", [])) for it in items)
        p_from = 0.0
        if dedup:
            job["message"] = "去重预检：抽帧算指纹…"
            try:
                report, kept = dedup_clips(items, dedup, job)
                items = [{"token": it["token"],
                          "keep": [i for i in it.get("keep", [])
                                   if (it["token"], int(i)) in kept]}
                         for it in items]
                total = sum(len(it["keep"]) for it in items)
                report["after"] = total
                job["dedup"] = report
                p_from = 0.5
                if not total:
                    job["state"] = "error"
                    job["error"] = "去重后没有可导出的片段"
                    job["message"] = "去重后没有可导出的片段"
                    return
            except Exception as exc:  # noqa: BLE001
                job["dedup"] = {"error": f"{type(exc).__name__}: {exc}", "skipped": []}
        base = 0
        for it in items:
            video = VIDEOS.get(it.get("token", ""))
            keep = [int(i) for i in it.get("keep", []) if isinstance(i, (int, float))]
            if not video or not keep:
                continue
            target = Path(out_dir) if out_dir else Path(default_out_dir(video))
            try:
                export_one(video, keep, target, mode, job, base, total, p_from)
            except Exception as exc:  # noqa: BLE001
                job["files"].append({"path": "", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            base += len(keep)
        job["state"] = "done"
        ok = sum(1 for f in job["files"] if f["ok"])
        skipped = len((job.get("dedup") or {}).get("skipped") or [])
        job["message"] = (f"导出完成：{ok} 个文件" + (f"（去重跳过 {skipped} 段）" if skipped else ""))
    except Exception as exc:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(exc)
    finally:
        job["elapsed"] = round(time.time() - job["t0"], 1)


# ---------------------------------------------------------------- 导出前去重

CLIP_FP = WORK / "clipfp"
# 去重强度：(结构位容差 /256, 平均色差 0~255)。数字越小越保守，留下的越多
DEDUP_LEVELS = {
    "conservative": (6, 24.0),
    "normal": (12, 32.0),
    "aggressive": (20, 44.0),
}
DEDUP_OVERLAP = 0.85   # 同一视频里短片段被长片段盖住超过这个比例即算重复


def _clip_frames(video: dict, sc: dict) -> list[Path]:
    """抽片段内的两帧小图（28% / 74% 处），落在本地缓存里，重跑去重不用再抽。"""
    raw = f"{video['path']}|{sc['start']:.3f}|{sc['end']:.3f}|{video.get('size')}"
    base = CLIP_FP / hashlib.md5(raw.encode("utf-8")).hexdigest()[:20]
    base.mkdir(parents=True, exist_ok=True)
    dur = max(0.05, float(sc["dur"]))
    outs: list[Path] = []
    for i, ratio in enumerate((0.28, 0.74)):
        dst = base / f"c{i}.jpg"
        # 只按「文件有内容」判断：纯色/暗场画面压出来只有 300 来字节，
        # 用几百字节的阈值会把合法的黑场帧当成抽帧失败。
        if not (dst.exists() and dst.stat().st_size >= 150):
            cmd = [FFMPEG, "-v", "error", "-y",
                   "-ss", f"{float(sc['start']) + dur * ratio:.3f}", "-i", video["path"],
                   "-frames:v", "1", "-vf", "scale=160:-2", "-q:v", "4", str(dst)]
            try:
                subprocess.run(cmd, capture_output=True, timeout=120, creationflags=NO_WINDOW)
            except Exception:  # noqa: BLE001
                pass
        if dst.exists() and dst.stat().st_size >= 150:
            outs.append(dst)
    return outs


def dedup_clips(items: list[dict], level: str = "normal",
                job: dict | None = None) -> tuple[dict, set]:
    """导出前的片段去重：返回（报告, 要保留的 (token, 片段序号) 集合）。

    两层判断：
      1) 同源重叠——同一个视频里两段区间互相盖住，直接算重复（不抽帧，零成本）
      2) 画面近似——抽两帧算 dHash + 平均色，够近就算同一段（同一条素材重拍/重切）
    每组留代表项：时长更长 > 起点更早（长的通常更完整，短的往往是它的子区间）。
    """
    clips: list[dict] = []
    for it in items:
        video = VIDEOS.get(it.get("token", ""))
        if not video:
            continue
        scenes = video.get("scenes") or []
        for idx in it.get("keep", []):
            if not isinstance(idx, (int, float)):
                continue
            idx = int(idx)
            if not (0 <= idx < len(scenes)):
                continue
            sc = scenes[idx]
            clips.append({"token": it["token"], "idx": idx, "video": video, "sc": sc,
                          "start": float(sc["start"]), "end": float(sc["end"]),
                          "dur": float(sc["dur"]),
                          "name": f"{safe_stem(video['name'])}_{sc['i']:03d}"})

    out = {"level": level, "before": len(clips), "after": len(clips),
           "skipped": [], "saved": 0.0, "no_fp": 0}
    if len(clips) < 2:
        return out, {(c["token"], c["idx"]) for c in clips}

    alive = [True] * len(clips)
    ordered = sorted(range(len(clips)), key=lambda k: (-clips[k]["dur"], clips[k]["start"]))

    def _drop(dup: int, keeper: int, reason: str, dist: int) -> None:
        alive[dup] = False
        out["skipped"].append({"name": clips[dup]["name"], "keep": clips[keeper]["name"],
                               "reason": reason, "dist": dist, "dur": clips[dup]["dur"]})
        out["saved"] += clips[dup]["dur"]

    # 1) 同源区间重叠
    for pos, a in enumerate(ordered):
        if not alive[a]:
            continue
        for b in ordered[pos + 1:]:
            if not alive[b]:
                continue
            if clips[b]["token"] != clips[a]["token"]:
                continue
            ov = min(clips[a]["end"], clips[b]["end"]) - max(clips[a]["start"], clips[b]["start"])
            short = min(clips[a]["dur"], clips[b]["dur"]) or 0.01
            if ov > 0.15 and ov >= DEDUP_OVERLAP * short:
                _drop(b, a, "同源重叠", 0)

    # 2) 画面近似
    tol_bits, tol_col = DEDUP_LEVELS.get(level, DEDUP_LEVELS["normal"])
    live = [k for k in ordered if alive[k]]
    fps: dict[int, dict] = {}
    for n, k in enumerate(live):
        if job:
            job["progress"] = 0.45 * (n + 1) / max(1, len(live))
            job["message"] = f"去重预检：抽帧比对 {n + 1}/{len(live)}　{clips[k]['name']}"
        fp = pipe.fp_from_frames(_clip_frames(clips[k]["video"], clips[k]["sc"]))
        if fp:
            fps[k] = fp
        else:
            out["no_fp"] += 1

    keepers: list[int] = []
    flat: dict[int, bool] = {}

    def _is_flat(k: int) -> bool:
        """纯色 / 暗场：梯度位几乎没有起伏，结构指纹等于没信息，只能靠颜色判。"""
        if k not in flat:
            ones = sum(bin(int(h, 16)).count("1") for h in fps[k]["h"])
            total = sum(len(h) * 4 for h in fps[k]["h"]) or 1
            ratio = ones / total
            flat[k] = ratio < 0.02 or ratio > 0.98
        return flat[k]

    for k in live:
        hit = None
        if k in fps:
            for m in keepers:
                if m not in fps:
                    continue
                ham, col = pipe.fp_dist(fps[k], fps[m])
                if ham > tol_bits or col > tol_col:
                    continue
                # 两边都没有结构可比时，颜色必须几乎一样才算重复
                if (_is_flat(k) or _is_flat(m)) and col > 10:
                    continue
                hit = (m, ham)
                break
        if hit:
            same_file = str(Path(clips[k]["video"]["path"]).resolve()) == \
                str(Path(clips[hit[0]]["video"]["path"]).resolve())
            _drop(k, hit[0], "完全一致" if hit[1] == 0 else "画面近似", hit[1])
            if hit[1] == 0 and not same_file:
                out["skipped"][-1]["reason"] = "重复文件"
        else:
            keepers.append(k)

    out["after"] = sum(1 for x in alive if x)
    out["saved"] = round(out["saved"], 1)
    if job:
        job["progress"] = 0.5
        job["message"] = (f"去重：{out['before']} 段 → {out['after']} 段"
                          if out["skipped"] else "去重：没有发现重复片段")
    kept = {(clips[k]["token"], clips[k]["idx"]) for k in range(len(clips)) if alive[k]}
    return out, kept


def edl_timecode(frames: int, fps: float) -> str:
    r = max(1, int(round(fps)))
    f = max(0, int(frames))
    h, f = divmod(f, r * 3600)
    m, f = divmod(f, r * 60)
    s, ff = divmod(f, r)
    return f"{h:02d}:{m:02d}:{s:02d}:{ff:02d}"


def write_edl(video: dict, keep: list[int], out_dir: Path) -> tuple[Path, int]:
    fps = float(video["fps"])
    stem = safe_stem(video["name"])
    lines = [f"TITLE: {stem}", "FCM: NON-DROP FRAME", ""]
    rec = 0
    rec_frames = 0
    for idx in keep:
        sc = video["scenes"][idx]
        fi = int(round(sc["start"] * fps))
        fo = int(round(sc["end"] * fps))
        ln = max(1, fo - fi)
        rec += 1
        lines.append(
            f"{rec:03d}  AX       V     C        {edl_timecode(fi, fps)} "
            f"{edl_timecode(fo, fps)} {edl_timecode(rec_frames, fps)} {edl_timecode(rec_frames + ln, fps)}"
        )
        rec_frames += ln
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}.edl"
    path.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return path, rec


# ---------------------------------------------------------------- 预览代理

# Chromium 内核能直接硬解的编码组合
OK_VCODECS = {"h264", "vp8", "vp9", "av1"}
OK_ACODECS = {"aac", "mp3", "opus", "vorbis", "flac", ""}

_PROBE_CACHE: dict[str, dict] = {}
_PROBE_LOCK = threading.Lock()


def probe_media(path: str) -> dict:
    """用 ffmpeg -i 的 stderr 读编码信息（imageio-ffmpeg 不带 ffprobe）。"""
    key = str(path)
    with _PROBE_LOCK:
        hit = _PROBE_CACHE.get(key)
    if hit:
        return hit
    info = {"v": "", "a": "", "pix": "", "dur": 0.0, "container": ""}
    if not FFMPEG:
        return info
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-i", key],
                           capture_output=True, text=True, errors="replace",
                           timeout=40, creationflags=NO_WINDOW)
        txt = (r.stderr or "") + (r.stdout or "")
    except Exception:
        txt = ""
    m = re.search(r"Input #0,\s*([^,]+(?:,[^,]+)*),\s*from", txt)
    if m:
        info["container"] = m.group(1).split(",")[0].strip().lower()
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if m:
        info["dur"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Video:\s*([A-Za-z0-9_]+)", txt)
    if m:
        info["v"] = m.group(1).lower()
    m = re.search(r"Audio:\s*([A-Za-z0-9_]+)", txt)
    if m:
        info["a"] = m.group(1).lower()
    vline = re.search(r"Video:\s*(.+)", txt)
    if vline:
        for tok in re.split(r"[\s,()/]+", vline.group(1)):
            tok = tok.strip(" ,()[]")
            if re.match(r"^(yuva|yuv|nv|p0|p2|p4|rgb|bgr|gbr|gray)", tok):
                info["pix"] = tok
                break
    with _PROBE_LOCK:
        _PROBE_CACHE[key] = info
    return info


def proxy_plan(video: dict) -> dict:
    """判断怎么让浏览器播得动：direct 直接播 / copyv 只转音频（无损秒级）/ transcode 720p 全转。"""
    info = probe_media(video["path"])
    v, a, pix, cont = info["v"], info["a"], info["pix"], info["container"]
    ext = Path(video.get("path", "")).suffix.lower()
    deep = any(k in pix for k in ("10", "12", "16")) or pix.startswith(("p0", "p2", "p4"))
    v_ok = (v in OK_VCODECS) and not deep
    a_ok = a in OK_ACODECS
    if cont == "mov":                       # mp4 / m4v / mov 家族
        cont_ok = True
    elif cont == "matroska":                # mkv 与 webm 同一个 demuxer，只有 webm 能播
        cont_ok = ext == ".webm"
    elif cont:
        cont_ok = False
    else:                                   # 探测失败就退回看扩展名
        cont_ok = ext in {".mp4", ".m4v", ".mov", ".webm"}
    if v_ok and a_ok and cont_ok:
        mode, why, eta = "direct", "", ""
    elif v_ok:
        mode = "copyv"
        if not cont_ok and a_ok:
            why = f"{ext.lstrip('.').upper() or '该'} 封装浏览器不认"
        elif not a_ok and cont_ok:
            why = f"音频是 {a.upper()}，浏览器解不了"
        else:
            why = f"{ext.lstrip('.').upper() or '该'} 封装 + {a.upper()} 音频都不支持"
        eta = "秒级完成，画面无损"
    else:
        mode = "transcode"
        why = (f"{v.upper()} 编码" + ("（10bit）" if deep else "") + "，浏览器不支持")
        eta = "需要转码，长片要等一会儿"
    return {"mode": mode, "v": v, "a": a, "pix": pix, "container": cont,
            "why": why, "eta": eta}


def proxy_key(video) -> str:
    """按「路径 + 大小 + 修改时间」指纹命名，同一素材删了再加、重启服务都能复用代理。"""
    p = str(video.get("path") if isinstance(video, dict) else video)
    try:
        st = os.stat(p)
        sig = f"{p}|{st.st_size}|{int(st.st_mtime)}"
    except Exception:
        sig = p
    return hashlib.md5(sig.encode("utf-8")).hexdigest()[:16]


def proxy_path(video) -> Path:
    return PROXIES / f"{proxy_key(video)}.mp4"


def proxy_ready(video) -> bool:
    p = proxy_path(video)
    try:
        return p.exists() and p.stat().st_size > 1024
    except Exception:
        return False


def proxy_worker(video: dict, job: dict, mode: str = "") -> None:
    """生成浏览器能播的预览代理。copyv：视频流直接复制只转音频；transcode：720p 全转。"""
    tmp = None
    try:
        if not FFMPEG:
            raise RuntimeError("未找到 ffmpeg，无法生成预览代理")
        plan = proxy_plan(video)
        mode = mode or plan["mode"]
        if mode == "direct":
            mode = "copyv"  # 已判定能播还走到这里，说明 original 有封装问题，复制流兜底
        out = proxy_path(video)
        tmp = out.with_suffix(".part.mp4")
        dur = float(video.get("duration") or plan.get("dur") or 0)
        job["mode"] = mode
        io_tail = ["-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(tmp)]
        if mode == "copyv":
            job["message"] = "快速封装中（画面无损）"
            cmd = ([FFMPEG, "-v", "error", "-y", "-i", video["path"],
                    "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ac", "2"] + io_tail)
        else:
            job["message"] = "转码 720p 预览代理…"
            # 限制长边 720：横屏缩到 1280x720，竖屏缩到 720x1280
            scale = ("scale=w='if(gt(iw,ih),min(1280,iw),-2)':h='if(gt(iw,ih),-2,min(1280,ih))'"
                     ":flags=bicubic")
            cmd = ([FFMPEG, "-v", "error", "-y", "-i", video["path"],
                    "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                    "-vf", scale,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2"] + io_tail)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time_us=") and dur > 0:
                try:
                    t = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                job["progress"] = min(0.99, t / dur)
                job["message"] = f"{'快速封装' if mode == 'copyv' else '转码预览'} {min(99, int(job['progress'] * 100))}%"
        code = proc.wait()
        if code != 0 or not tmp.exists():
            err = (proc.stderr.read() if proc.stderr else "") or "ffmpeg 退出码 %d" % code
            raise RuntimeError(err.strip()[:300])
        os.replace(tmp, out)
        job["progress"] = 1.0
        job["state"] = "done"
        job["message"] = "预览代理已就绪"
        job["proxy"] = True
    except Exception as exc:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(exc)
        try:
            if tmp:
                tmp.unlink(missing_ok=True)
        except Exception:
            pass
    finally:
        job["elapsed"] = round(time.time() - job["t0"], 1)


_PS_FOLDER = r"""
$ErrorActionPreference='Stop'
try{
  [Console]::OutputEncoding=[Text.Encoding]::UTF8
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $owner=New-Object System.Windows.Forms.Form
  $owner.FormBorderStyle='None'
  $owner.StartPosition='CenterScreen'
  $owner.Size=New-Object System.Drawing.Size(1,1)
  $owner.TopMost=$true
  $owner.ShowInTaskbar=$false
  $owner.Opacity=0.01
  $owner.Show()
  [System.Windows.Forms.Application]::DoEvents()
  $d=New-Object System.Windows.Forms.FolderBrowserDialog
  $d.Description='选择素材文件夹（里面放切片视频）'
  $d.ShowNewFolderButton=$false
  $start=$env:SHOTCUTTER_PICK_START
  if($start -and (Test-Path -LiteralPath $start -PathType Container)){ $d.SelectedPath=$start }
  $r=$d.ShowDialog($owner)
  $owner.Close()
  if($r -eq [System.Windows.Forms.DialogResult]::OK){ Write-Output $d.SelectedPath }
  else{ Write-Output '__CANCEL__' }
}catch{
  Write-Output ('__ERROR__' + $_.Exception.Message)
}
"""


def _nearest_dir(raw: str) -> str:
    """把用户填的路径收敛成一个真实存在的目录，用作选择窗口的起点。

    填的是文件就取它所在的目录，填的层级不存在就往上找；
    找不到就返回空，让对话框用它自己的默认位置。
    """
    t = str(raw or "").strip().strip('"').strip("'")
    if not t:
        return ""
    p = Path(t)
    for _ in range(8):
        try:
            if p.is_dir():
                return str(p)
            if p.exists():
                return str(p.parent)
        except OSError:
            pass
        if not p.parent or p.parent == p:
            break
        p = p.parent
    return ""


LASTDIR = pipe.PIPE / "lastdir.json"


def _last_dir() -> str:
    """上次用过的素材目录，选择窗口的兜底起点（不然永远跳回桌面）。"""
    try:
        d = json.loads(LASTDIR.read_text(encoding="utf-8")).get("dir") or ""
    except Exception:  # noqa: BLE001
        return ""
    return _nearest_dir(str(d))


def _preview_voice(pid: str, idx: int) -> tuple[str, str]:
    """给 BGM 混音试听找一条配音轨：优先指定项目，否则用最近一个有配音的项目。

    返回 (音频路径, 人话说明)。找不到返回 ("", "")。
    """
    label = ""
    p, s, _ = _pipe_get(pid, idx)
    a = ((s or {}).get("tts") or {}).get("audio") if s else None
    if a and Path(a).exists():
        nm = (p or {}).get("product", {}).get("name") or (p or {}).get("name") or "项目"
        return str(a), "「%s」的配音" % nm
    # 指定项目还没配音（或没指定）：退到最近一个有配音的项目
    root = pipe.PIPE / "projects"
    try:
        pjs = sorted(root.glob("*/project.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        pjs = []
    for f in pjs[:30]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for sc in d.get("scripts") or []:
            aa = ((sc.get("tts") or {}).get("audio") or "")
            if aa and Path(aa).exists():
                nm = (d.get("product") or {}).get("name") or d.get("name") or "项目"
                return str(aa), "最近一条「%s」的配音（当前方案还没渲染过）" % nm
    return "", ""


def _bgm_preview(bgm, voice: str, vol: float, fade: float, dur: float) -> Path:
    """按合成时的同一条滤镜链混一小段音频，返回 mp3 缓存文件。

    bgm 可以是本地文件路径，也可以是在线曲库的 https 直链（ffmpeg 直接拉流）。
    """
    vdur = pipe.media_duration(voice) or dur
    total = max(3.0, min(dur, vdur))
    try:
        vmtime = int(Path(voice).stat().st_mtime)
    except OSError:
        vmtime = 0
    key = "%s|%s|%.3f|%.2f|%.2f|%d" % (bgm, voice, vol, fade, total, vmtime)
    h = hashlib.md5(key.encode("utf-8", "replace")).hexdigest()[:16]
    cache = pipe.WORK / "bgm_prev"
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / ("mix_%s.mp3" % h)
    if out.exists() and out.stat().st_size > 1024:
        return out
    tmp = out.with_suffix(".tmp.mp3")
    # 和 compose 的混音完全一致：voice 直通 + bgm volume/淡入淡出 + amix(normalize=0) + alimiter
    chain = "[1:a]volume=%.3f,atrim=0:%.3f,asetpts=N/SR/TB" % (vol, total)
    if fade > 0.05 and total > fade * 2:
        chain += ",afade=t=in:st=0:d=%.2f" % fade
        chain += ",afade=t=out:st=%.2f:d=%.2f" % (total - fade, fade)
    filt = [chain + "[bg]",
            "[0:a]volume=1.0[voice]",
            "[voice][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95[aout]"]
    args = ["-v", "error", "-y", "-i", voice, "-stream_loop", "-1", "-i", str(bgm),
            "-filter_complex", ";".join(filt),
            "-map", "[aout]", "-t", "%.3f" % total,
            "-c:a", "libmp3lame", "-q:a", "4", str(tmp)]
    r = subprocess.run([pipe.FFMPEG] + args, capture_output=True, text=True,
                       timeout=120, creationflags=NO_WINDOW)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError((r.stderr or "").strip()[-300:] or "ffmpeg 混音失败")
    tmp.replace(out)
    return out


# ---------------------------------------------------------------- 在线 BGM 曲库（incompetech，CC-BY 4.0 免费可商用）
import urllib.request  # noqa: E402

INC_BASE = "https://incompetech.com/music/royalty-free"
INC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
_ONLINE = {"at": 0.0, "pieces": [], "genres": {}, "err": ""}
_ONLINE_TTL = 86400.0


def _http_open(url: str, headers: dict | None = None, timeout: int = 30):
    """先直连，失败再走系统代理（用户常开代理但软件不一定在跑）。"""
    hs = {"User-Agent": INC_UA}
    if headers:
        hs.update(headers)
    req = urllib.request.Request(url, headers=hs)
    try:
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout)
    except Exception:
        return urllib.request.urlopen(req, timeout=timeout)


def _online_catalog(force: bool = False) -> tuple[list, dict, str]:
    """拉取/缓存在线曲库目录（pieces.json + genre.json，内存缓存一天）。"""
    if not force and _ONLINE["pieces"] and time.time() - _ONLINE["at"] < _ONLINE_TTL:
        return _ONLINE["pieces"], _ONLINE["genres"], ""
    try:
        with _http_open(INC_BASE + "/pieces.json", timeout=40) as r:
            pieces = json.loads(r.read().decode("utf-8"))
        with _http_open(INC_BASE + "/genre.json", timeout=20) as r:
            genres = {str(g.get("id")): str(g.get("genre") or "") for g in json.loads(r.read().decode("utf-8"))}
        if not isinstance(pieces, list) or not pieces:
            raise RuntimeError("目录是空的")
    except Exception as exc:  # noqa: BLE001
        msg = "在线曲库拉取失败：%s" % exc
        _ONLINE["err"] = msg
        return _ONLINE["pieces"], _ONLINE["genres"], msg
    _ONLINE.update({"pieces": pieces, "genres": genres, "at": time.time(), "err": ""})
    return pieces, genres, ""


def _online_url(fn: str) -> str:
    return INC_BASE + "/mp3-royaltyfree/" + quote(fn)


def _online_find(fn: str) -> dict | None:
    for p in _ONLINE["pieces"]:
        if p.get("filename") == fn:
            return p
    return None


def _online_search(q: str, limit: int = 60) -> list[dict]:
    pieces, genres, _ = _online_catalog()
    words = [w for w in (q or "").lower().replace(",", " ").split() if w]
    out = []
    for p in pieces:
        genre = genres.get(str(p.get("genre")), "")
        hay = " ".join(str(p.get(k) or "") for k in ("title", "feel", "instruments", "bpm", "description")) + " " + genre
        hay = hay.lower()
        if words and not all(w in hay for w in words):
            continue
        m = re.match(r"(\d+):(\d+):(\d+)", str(p.get("length") or ""))
        dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))) if m else 0
        out.append({"title": str(p.get("title") or ""), "fn": str(p.get("filename") or ""),
                    "dur": dur, "feel": str(p.get("feel") or ""), "genre": genre,
                    "bpm": str(p.get("bpm") or ""), "instruments": str(p.get("instruments") or "")[:60]})
        if len(out) >= limit:
            break
    return out


def _online_download(fn: str) -> tuple[str, str]:
    """下载在线曲目入库。返回 (本地路径, 消息)。"""
    pieces, genres, err = _online_catalog()
    if err:
        raise RuntimeError(err)
    p = _online_find(fn)
    if not p:
        raise RuntimeError("曲目不在目录里：%s" % fn)
    dirs = pipe.bgm_dirs()
    if dirs:
        dest_dir = Path(dirs[0])
    else:                     # 一个目录都没加：建一个本地曲库目录并挂到配置上
        dest_dir = pipe.WORK / "bgm_lib"
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", str(p.get("title") or fn)).strip() or fn
    dest = dest_dir / ("%s - Kevin MacLeod.mp3" % safe)
    if dest.exists() and dest.stat().st_size > 1024:
        return str(dest), "之前下过，已在曲库里"
    tmp = dest.with_suffix(".part.mp3")
    try:
        with _http_open(_online_url(fn), timeout=180) as r, tmp.open("wb") as f:
            while True:
                buf = r.read(262144)
                if not buf:
                    break
                f.write(buf)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    if tmp.stat().st_size < 1024:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("下载的文件太小，可能失败了")
    tmp.replace(dest)
    if not dirs:              # 把新建的目录登记进配置，曲库列表能扫到
        cfg = pipe.load_cfg()
        cfg["video"]["bgm_dirs"] = [str(dest_dir)]
        pipe.save_cfg(cfg)
    # CC-BY 4.0 要求署名：在曲库目录里维护一份对照表
    credit = dest_dir / "BGM 署名对照.txt"
    line = "《%s》 · Music: %s by Kevin MacLeod (incompetech.com) · CC BY 4.0（把这句放进视频简介即可）" % (
        p.get("title"), p.get("title"))
    try:
        old = credit.read_text(encoding="utf-8") if credit.exists() else ""
        if line not in old:
            credit.write_text((old.rstrip() + "\n" + line + "\n").lstrip(), encoding="utf-8")
    except OSError:
        pass
    return str(dest), "已下载入库"


def remember_dir(path: str) -> None:
    d = _nearest_dir(path)
    if not d:
        return
    try:
        LASTDIR.parent.mkdir(parents=True, exist_ok=True)
        LASTDIR.write_text(json.dumps({"dir": d, "at": time.time()}, ensure_ascii=False),
                           encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


# ---------------- 拖拽文件夹：把「文件夹名」解析成本机路径 ----------------
# 浏览器出于安全不给拖入文件的完整路径，只给名字。
# 所以拖进来后端负责找：先在已知目录（含各层父目录）下面拼，拼不到就浅扫盘符。

_SKIP_SCAN = {"windows", "program files", "program files (x86)", "appdata", "$recycle.bin",
              "system volume information", "node_modules", "perflogs", "recovery",
              "config.msi", "msocache", "intel", "amd", "nvidia"}


def _scan_dirs(seeds: list, want: str, depth: int = 3,
               max_seen: int = 60000, budget_s: float = 6.0) -> list:
    """从 seeds 往下浅扫（BFS，限深限量限时），找名字等于 want 的目录。"""
    t0 = time.time()
    want_l = want.strip().strip("\\/").lower()
    hits: list = []
    seen = 0
    stack = [(Path(s), 0) for s in seeds]
    while stack and seen < max_seen and time.time() - t0 < budget_s:
        d, lv = stack.pop()
        if lv > depth:
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            seen += 1
            if seen >= max_seen or time.time() - t0 > budget_s:
                break
            nm = e.name
            low = nm.lower()
            if nm.startswith((".", "$")) or low in _SKIP_SCAN:
                continue
            try:
                if not e.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if low == want_l:
                hits.append(e.path)
            if lv < depth:
                stack.append((Path(e.path), lv + 1))
    return hits


def _mtime_of(p) -> float:
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return 0.0


def resolve_dropped(names, known) -> dict:
    names = [str(x).strip().strip("\\/") for x in (names or []) if str(x).strip()][:10]
    known = [str(x) for x in (known or []) if str(x).strip()][:50]
    roots: list = []

    def _add(p) -> None:
        try:
            if p and p.is_dir() and str(p) not in roots:
                roots.append(str(p))
        except OSError:
            pass

    # 已知目录和它们的各层父目录都算 root：拖进来的多半和上次用的目录是邻居
    for k in known + [_last_dir()]:
        p = Path(_nearest_dir(k) or "")
        while p and p != p.parent:
            _add(p)
            p = p.parent
        if p:
            _add(p)
    # 兜底扫描的种子：非系统盘的盘符根 + 用户主目录
    seeds = list(roots)
    for d in "DEFGHIJKLMNOPQRSTUVWXYZ":
        dr = Path(d + ":\\")
        if dr.is_dir():
            seeds.append(str(dr))
    home = Path.home()
    _add(home)
    if str(home) not in seeds:
        seeds.append(str(home))

    out: dict = {}
    for nm in names:
        hits: list = []
        want_l = nm.lower()
        for r in roots:                       # 先直接拼，绝大多数情况这一步就中
            cand = Path(r) / nm
            try:
                if cand.is_dir():
                    hits.append(str(cand))
            except OSError:
                pass
            if Path(r).name.lower() == want_l and r not in hits:
                hits.append(r)                # 上次用的目录本身就是要找的那个
        if not hits:
            hits = _scan_dirs(seeds, nm)
        uniq: list = []
        for h in hits:
            if h not in uniq:
                uniq.append(h)
        uniq.sort(key=_mtime_of, reverse=True)      # 最近动过的排前面
        out[nm] = uniq[:8]
    return {"ok": True, "found": out, "roots": len(roots)}


def pick_folder(start: str = "") -> dict:
    if not PICK_GUARD.acquire(blocking=False):
        return {"state": "busy", "path": "", "message": "已经有一个选择窗口打开了"}
    proc = None
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", _PS_FOLDER],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            # 起始目录用环境变量传，省得路径里的引号/中文在命令行上被吃掉
            env={**os.environ,
                 "SHOTCUTTER_PICK_START": _nearest_dir(start) or _last_dir()},
        )
        try:
            out_b, _ = proc.communicate(timeout=PICK_TIMEOUT)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
            return {"state": "timeout", "path": "", "message": "选择窗口没有响应，可直接粘贴文件夹路径"}
        lines = [ln.strip() for ln in out_b.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        if any(ln.startswith("__ERROR__") for ln in lines):
            return {"state": "error", "path": "", "message": next(ln[9:] for ln in lines if ln.startswith("__ERROR__"))}
        if not lines or "__CANCEL__" in lines:
            return {"state": "cancel", "path": "", "message": ""}
        remember_dir(lines[-1])
        return {"state": "ok", "path": lines[-1], "message": ""}
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "path": "", "message": "打开选择窗口失败：%s" % exc}
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        PICK_GUARD.release()


def _pipe_get(pid: str, idx: int | None = None) -> tuple[dict | None, dict | None, str]:
    """返回 (project, script, err)。"""
    p = pipe.project_load(pid or "")
    if not p:
        return None, None, "项目不存在"
    scripts = p.get("scripts") or []
    if idx is None:
        idx = int(p.get("active") or 0)
    if not scripts:
        return p, None, "还没有脚本"
    idx = max(0, min(len(scripts) - 1, int(idx)))
    return p, scripts[idx], ""


# ---------------------------------------------------------------- 系统文件选择

PICK_GUARD = threading.Lock()
PICK_TIMEOUT = 75

_PS_PICK = r"""
$ErrorActionPreference='Stop'
try{
  [Console]::OutputEncoding=[Text.Encoding]::UTF8
  Add-Type -AssemblyName System.Windows.Forms
  Add-Type -AssemblyName System.Drawing
  $owner=New-Object System.Windows.Forms.Form
  $owner.FormBorderStyle='None'
  $owner.StartPosition='CenterScreen'
  $owner.Size=New-Object System.Drawing.Size(1,1)
  $owner.TopMost=$true
  $owner.ShowInTaskbar=$false
  $owner.Opacity=0.01
  $owner.Show()
  [System.Windows.Forms.Application]::DoEvents()
  $d=New-Object System.Windows.Forms.OpenFileDialog
  $d.Multiselect=$true
  $d.Title='选择视频文件'
  $d.Filter='视频文件|*.mp4;*.mov;*.mkv;*.avi;*.webm;*.flv;*.ts;*.m4v;*.wmv;*.mpg;*.mpeg;*.m2ts;*.3gp|所有文件|*.*'
  $d.CheckFileExists=$true
  $d.RestoreDirectory=$true
  $r=$d.ShowDialog($owner)
  $owner.Close()
  if($r -eq [System.Windows.Forms.DialogResult]::OK){ foreach($f in $d.FileNames){ Write-Output $f } }
  else{ Write-Output '__CANCEL__' }
}catch{
  Write-Output ('__ERROR__' + $_.Exception.Message)
}
"""


# ================= 独立工具：视频变高清（video2x）/ 字幕去除（VSR） =================
# 两个工具都是一次一个任务的 GPU 重活，各自一个单例跟踪器，不进 JOBS 池。
def _upscale_root() -> Path:
    """视频变高清（video2x）的根目录。

    优先级：pipeline.json 的 upscale.root → 默认 D:\\VideoUpscaler →
    程序目录下的 tools\\VideoUpscaler（便携包就放这里）。都没有就返回默认值，
    页面会显示"未安装"，其它功能不受影响。
    """
    try:
        raw = str((pipe.load_cfg().get("upscale") or {}).get("root") or "").strip().strip('"')
    except Exception:  # noqa: BLE001
        raw = ""
    if raw:
        return Path(raw)
    default = Path(r"D:\VideoUpscaler")
    if default.exists():
        return default
    local = ROOT / "tools" / "VideoUpscaler"
    return local if local.exists() else default


VU_ROOT = _upscale_root()
V2X = VU_ROOT / "tools" / "video2x.exe"
VU_BIN = VU_ROOT / "tools" / "ffmpeg-9.0.2-essentials_build" / "bin"
VU_FFPROBE = VU_BIN / "ffprobe.exe"
VU_OUT = VU_ROOT / "output"

VSR_ROOT = Path(r"D:\Tools\VSR140")
VSR_PY = VSR_ROOT / "Python" / "python.exe"
VSR_RES = VSR_ROOT / "resources"
VSR_RUN = VSR_RES / "run_with_area.py"


def _fmt_hms(sec) -> str:
    sec = int(max(0, sec or 0))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%02d:%02d" % (m, s)


class ToolJob:
    """video2x / VSR 共用的单任务跟踪器。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.running = False
        self.phase = ""
        self.percent = 0.0
        self.frame = 0
        self.total = 0
        self.fps = 0.0
        self.lines: list[str] = []
        self.output = ""
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self.proc = None
        self.extra = {}

    def log(self, text):
        text = (text or "").rstrip()
        if not text:
            return
        with self.lock:
            self.lines.append(text)
            if len(self.lines) > 500:
                del self.lines[:200]

    def snapshot(self):
        with self.lock:
            elapsed = ""
            if self.started_at:
                elapsed = _fmt_hms((self.finished_at or time.time()) - self.started_at)
            eta = ""
            if self.fps > 0 and self.total > self.frame:
                eta = _fmt_hms((self.total - self.frame) / self.fps)
            return {
                "running": self.running, "phase": self.phase,
                "frame": self.frame, "total": self.total,
                "fps": round(self.fps, 2), "percent": round(self.percent, 1),
                "elapsed": elapsed, "eta": eta, "output": self.output,
                "error": self.error, "lines": self.lines[-160:],
                "extra": dict(self.extra),
            }


HD_JOB = ToolJob()
SUB_JOB = ToolJob()
_HD_GPUS: list[dict] = []
_HD_GPU_LOCK = threading.Lock()


def _tool_capture(args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        return p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def tool_probe(path: str) -> dict:
    """用 VideoUpscaler 自带的 ffprobe 读视频信息（imageio-ffmpeg 不带 ffprobe）。"""
    if not VU_FFPROBE.exists():
        raise RuntimeError("找不到 ffprobe：%s" % VU_FFPROBE)
    p = Path(path.strip().strip('"'))
    if not p.is_file():
        raise RuntimeError("找不到这个文件：%s" % p)
    code, out = _tool_capture(
        [str(VU_FFPROBE), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,codec_name,nb_frames",
         "-show_entries", "format=duration,size", "-of", "json", str(p)])
    if code != 0:
        raise RuntimeError((out.strip() or "无法读取视频信息")[:300])
    data = json.loads(out)
    st = (data.get("streams") or [{}])[0]
    fmt = data.get("format") or {}
    num, _, den = (st.get("r_frame_rate") or "0/1").partition("/")
    try:
        fps = round(float(num) / float(den or 1), 3)
    except Exception:  # noqa: BLE001
        fps = 0.0
    duration = float(fmt.get("duration") or 0)
    frames = st.get("nb_frames")
    if not frames or not str(frames).isdigit():
        frames = int(duration * fps) if fps else 0
    return {"path": str(p), "name": p.name,
            "width": st.get("width", 0), "height": st.get("height", 0),
            "fps": fps, "codec": st.get("codec_name", ""),
            "duration": duration, "duration_text": _fmt_hms(duration),
            "size_mb": round(float(fmt.get("size") or 0) / 1048576, 1),
            "frames": int(frames)}


def hd_gpus(refresh: bool = False) -> list[dict]:
    global _HD_GPUS
    with _HD_GPU_LOCK:
        if refresh or not _HD_GPUS:
            code, out = _tool_capture([str(V2X), "-l"], timeout=25)
            gpus = []
            if code == 0:
                for line in out.splitlines():
                    m = re.match(r"^(\d+)\.\s+(.+)$", line.strip())
                    if m:
                        gpus.append({"index": int(m.group(1)), "name": m.group(2).strip()})
            if gpus:
                _HD_GPUS = gpus
        return list(_HD_GPUS)


def hd_build_command(p: dict, inp: str, out: str, phase: str) -> list[str]:
    processor = p.get("processor", "realesrgan")
    codec = p.get("codec", "hevc_nvenc")
    quality = int(p.get("quality", 20))
    device = str(int(p.get("device", 0)))
    if phase == "interp":
        cmd = [str(V2X), "-i", inp, "-o", out, "-p", "rife",
               "-m", str(int(p.get("interp_factor", 2))),
               "--rife-model", p.get("rife_model", "rife-v4.6")]
    else:
        cmd = [str(V2X), "-i", inp, "-o", out, "-p", processor]
        if processor == "realesrgan":
            cmd += ["--realesrgan-model", p.get("model", "realesr-animevideov3")]
        elif processor == "realcugan":
            cmd += ["--realcugan-model", p.get("cugan_model", "models-pro"),
                    "--realcugan-syncgap", str(int(p.get("syncgap", 3)))]
        elif processor == "libplacebo":
            cmd += ["--libplacebo-shader", p.get("shader", "anime4k-v4-a+a")]
        cmd += ["-s", str(int(p.get("factor", 3)))]
    cmd += ["-c", codec, "-d", device]
    if codec.endswith("_nvenc"):
        cmd += ["-e", "preset=" + p.get("nvenc_preset", "p5"), "-e", "cq=%d" % quality]
    else:
        cmd += ["-e", "crf=%d" % quality, "-e", "preset=" + p.get("x_preset", "medium")]
    return cmd


_V2X_PROG = re.compile(r"frame=(\d+)/(\d+)[^;]*;\s*fps=([\d.]+)")


def _decode_line(raw: bytes) -> str:
    """VSR / video2x 自己打的是 UTF-8（我们设了 PYTHONIOENCODING），
    但它们内部调 ffmpeg / 系统命令时输出的是 GBK，混在一起就会乱码。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        for enc in ("gbk", "cp936", "big5"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")


def _hd_pump(stream, phase: str):
    """video2x 的进度用 \\r 刷新，逐字符读。"""
    buf = b""
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        if chunk in (b"\r", b"\n"):
            line = _decode_line(buf)
            buf = b""
            m = _V2X_PROG.search(line)
            if m:
                cur, tot, fps = int(m.group(1)), int(m.group(2)), float(m.group(3))
                with HD_JOB.lock:
                    HD_JOB.frame, HD_JOB.total, HD_JOB.fps = cur, tot, fps
                    if tot:
                        share = 0.85 if HD_JOB.extra.get("interp_on") else 1.0
                        if phase == "upscale":
                            HD_JOB.percent = min(99.5, cur / tot * 100 * share)
                        else:
                            HD_JOB.percent = min(99.5, 85 + cur / tot * 15)
                continue
            if line.strip() and not line.strip().startswith("\x1b"):
                HD_JOB.log(line)
        else:
            buf += chunk
            if len(buf) > 20000:
                buf = b""


def _hd_exec(cmd: list[str], phase: str) -> int:
    HD_JOB.log("$ " + " ".join('"%s"' % c if " " in c else c for c in cmd))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW, bufsize=0)
    with HD_JOB.lock:
        HD_JOB.proc = proc
    _hd_pump(proc.stdout, phase)
    proc.wait()
    with HD_JOB.lock:
        HD_JOB.proc = None
    return proc.returncode


def _hd_output_ok(path: str) -> bool:
    """video2x 6.4.0 完成后退出时可能崩 0xC0000005 但文件是好的，必须验文件。"""
    if not os.path.exists(path) or os.path.getsize(path) < 2048:
        return False
    code, out = _tool_capture(
        [str(VU_FFPROBE), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration", "-of", "csv=p=0", path], timeout=60)
    return code == 0 and out.strip().replace(".", "", 1).isdigit()


def _hd_dur(path: str) -> float:
    code, out = _tool_capture(
        [str(VU_FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], timeout=60)
    try:
        return float(out.strip()) if code == 0 else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def hd_worker(p: dict):
    try:
        inp = p["input"]
        factor = int(p.get("factor", 3))
        interp = int(p.get("interp_factor", 1))
        base = os.path.splitext(os.path.basename(inp))[0]
        out = os.path.join(str(VU_OUT), "%s_%dx%s.mp4" % (
            base, factor, "f%d" % interp if interp > 1 else ""))
        os.makedirs(VU_OUT, exist_ok=True)
        with HD_JOB.lock:
            HD_JOB.reset()
            HD_JOB.running = True
            HD_JOB.started_at = time.time()
            HD_JOB.output = out
            HD_JOB.extra = {"interp_on": interp > 1}
            HD_JOB.phase = "放大处理中"
        tmp = None
        target = out
        if interp > 1:
            tmp = os.path.join(str(VU_OUT), ".tmp_" + base + ".mp4")
            target = tmp
        rc = _hd_exec(hd_build_command(p, inp, target, "upscale"), "upscale")
        if not _hd_output_ok(target):
            raise RuntimeError("放大阶段失败，退出码 %s，没有生成有效文件" % rc)
        if rc != 0:
            # video2x 正常写完后也可能崩在退出码上，但被取消的半成品时长会短一截，
            # 拿输出时长对比输入时长区分这两种情况。
            in_dur, out_dur = _hd_dur(inp), _hd_dur(target)
            if in_dur and out_dur < in_dur * 0.95:
                raise RuntimeError("放大阶段中断（退出码 %s），产物不完整" % rc)
            HD_JOB.log("提示：video2x 退出码 %s，但产物完整，按成功处理" % rc)
        if interp > 1:
            with HD_JOB.lock:
                HD_JOB.phase = "插帧处理中"
                HD_JOB.frame = HD_JOB.total = 0
                HD_JOB.percent = 85
            rc = _hd_exec(hd_build_command(p, tmp, out, "interp"), "interp")
            try:
                os.remove(tmp)
            except OSError:
                pass
            if not _hd_output_ok(out):
                raise RuntimeError("插帧阶段失败，退出码 %s，没有生成有效文件" % rc)
            if rc != 0:
                in_dur, out_dur = _hd_dur(tmp), _hd_dur(out)
                if in_dur and out_dur < in_dur * 0.95:
                    raise RuntimeError("插帧阶段中断（退出码 %s），产物不完整" % rc)
        with HD_JOB.lock:
            HD_JOB.percent = 100
            HD_JOB.phase = "已完成"
        HD_JOB.log("完成，输出：" + out)
    except Exception as exc:  # noqa: BLE001
        with HD_JOB.lock:
            HD_JOB.error = str(exc)
            HD_JOB.phase = "出错了"
        HD_JOB.log("错误：" + str(exc))
    finally:
        with HD_JOB.lock:
            HD_JOB.running = False
            HD_JOB.finished_at = time.time()


_TQDM_PCT = re.compile(r"(\d+)%\|")
_TQDM_CNT = re.compile(r"\b(\d+)/(\d+)\b")


def _sub_pump(stream):
    """VSR 的进度：tqdm 条（\\r 刷新）+ 阶段打印。
    找字幕阶段占前 50%，修复画面阶段占后 50%。"""
    buf = b""
    stage = ["find"]
    while True:
        chunk = stream.read(1)
        if not chunk:
            break
        if chunk in (b"\r", b"\n"):
            line = _decode_line(buf)
            buf = b""
            low = line.strip()
            if low.startswith("[Processing] start finding"):
                stage[0] = "find"
                with SUB_JOB.lock:
                    SUB_JOB.phase = "检测字幕位置（前半程）"
                continue
            if low.startswith("[Finished] Finished finding"):
                stage[0] = "remove"
                with SUB_JOB.lock:
                    SUB_JOB.phase = "AI 修复画面（后半程）"
                continue
            m = _TQDM_PCT.search(line)
            if m:
                pct = float(m.group(1))
                mc = _TQDM_CNT.search(line)
                with SUB_JOB.lock:
                    if SUB_JOB.phase.startswith("启动引擎"):
                        SUB_JOB.phase = "字幕检测与修复中"
                    if mc:
                        SUB_JOB.frame, SUB_JOB.total = int(mc.group(1)), int(mc.group(2))
                        if pct <= 0 and SUB_JOB.total:
                            pct = SUB_JOB.frame / SUB_JOB.total * 100
                    # v1.4.0 只有一个 0~100% 的总进度条，不再分「找字幕/修画面」两段
                    SUB_JOB.percent = min(99.5, pct)
                continue
            if low and not low.startswith("\x1b"):
                SUB_JOB.log(line)
        else:
            buf += chunk
            if len(buf) > 20000:
                buf = b""


_GPU_NAME: list[str] = []


def gpu_name() -> str:
    """本机 N 卡型号；没有就返回空串。结果缓存，别每次都调 nvidia-smi。"""
    if _GPU_NAME:
        return _GPU_NAME[0]
    name = ""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                           capture_output=True, text=True, errors="replace",
                           timeout=8, creationflags=NO_WINDOW)
        if r.returncode == 0 and (r.stdout or "").strip():
            name = r.stdout.strip().splitlines()[0].strip()
    except Exception:  # noqa: BLE001
        name = ""
    _GPU_NAME.append(name)
    return name


def sub_worker(inp: str, area: str):
    try:
        out = os.path.join(os.path.dirname(inp),
                           os.path.splitext(os.path.basename(inp))[0] + "_no_sub.mp4")
        started = time.time()
        # 上一次跑出的旧 _no_sub 文件还在的话，不能把它当成这次的结果
        stale_before = os.path.getmtime(out) if os.path.exists(out) else 0
        with SUB_JOB.lock:
            SUB_JOB.reset()
            SUB_JOB.running = True
            SUB_JOB.started_at = time.time()
            SUB_JOB.output = out
            SUB_JOB.phase = "启动引擎（首次加载模型较慢）"
        env = dict(os.environ)
        # QPT_MODE=Running：跳过 QPT 的重新部署循环（否则一卡就是 20 分钟）
        env.update({"QPT_MODE": "Running", "QPT_COLOR": "False",
                    "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        cmd = [str(VSR_PY), str(VSR_RUN), inp, area or "none"]
        SUB_JOB.log("$ " + " ".join('"%s"' % c if " " in c else c for c in cmd))
        _gpu = gpu_name()
        SUB_JOB.log("加速设备：" + (("GPU · " + _gpu) if _gpu
                                    else "CPU（没检测到 NVIDIA 显卡，会慢很多）"))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                cwd=str(VSR_RES), env=env,
                                creationflags=NO_WINDOW, bufsize=0)
        with SUB_JOB.lock:
            SUB_JOB.proc = proc
        _sub_pump(proc.stdout)
        proc.wait()
        with SUB_JOB.lock:
            SUB_JOB.proc = None
        if proc.returncode != 0 and not (
                os.path.exists(out) and os.path.getmtime(out) > started):
            raise RuntimeError("VSR 退出码 %s，没有生成新的输出文件" % proc.returncode)
        if not os.path.exists(out) or os.path.getmtime(out) <= stale_before:
            raise RuntimeError("处理结束但没有生成新的输出文件（预期：%s）" % out)
        with SUB_JOB.lock:
            SUB_JOB.percent = 100
            SUB_JOB.phase = "已完成"
        SUB_JOB.log("完成，输出：" + out)
    except Exception as exc:  # noqa: BLE001
        with SUB_JOB.lock:
            SUB_JOB.error = str(exc)
            SUB_JOB.phase = "出错了"
        SUB_JOB.log("错误：" + str(exc))
    finally:
        with SUB_JOB.lock:
            SUB_JOB.running = False
            SUB_JOB.finished_at = time.time()


def sub_frame(path: str, t: float) -> tuple[Path, float]:
    """抽一帧给前端画字幕区域框。

    会踩的坑：
    1) 有的片子色彩范围（yuvj）让 mjpeg 编码器直接拒绝 → 加 -strict unofficial / 先转 yuvj420p
    2) 素材没下载完整（partial file）时，靠后的秒数解不出来 → 依次退到更早的时间点
    """
    t = max(0.0, float(t))
    out = WORK / "sub_frames" / (hashlib.md5(f"{path}|{t:.2f}".encode("utf-8")).hexdigest()[:16] + ".jpg")
    if out.exists() and out.stat().st_size > 0:
        return out, t
    out.parent.mkdir(parents=True, exist_ok=True)

    def attempt(at: float, extra: list) -> str:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        args = [FFMPEG, "-y", "-ss", "%.2f" % at, "-i", path,
                "-frames:v", "1", "-q:v", "3"] + extra + [str(out)]
        r = subprocess.run(args, capture_output=True, creationflags=NO_WINDOW, timeout=90)
        if out.exists() and out.stat().st_size > 0:
            return ""
        return (r.stderr or b"").decode("utf-8", "replace")[-300:]

    err = ""
    for at in (t, max(0.0, t - 2.0), 1.0, 0.0):
        for extra in (["-strict", "unofficial"], ["-vf", "format=yuvj420p", "-strict", "unofficial"]):
            err = attempt(at, extra)
            if not err:
                return out, at
    hint = "抽帧失败，无法预览"
    low = err.lower()
    if "partial file" in low or "invalid nal" in low or "nothing was encoded" in low:
        hint += "：这个文件看起来没下载完整或已损坏（靠后的画面解不出来），换一条素材，或把抽帧时间拖到开头试试"
    elif "non full-range" in low:
        hint += "：这个片子的色彩范围特殊，编码器不认"
    elif err:
        hint += "：" + err.replace(chr(10), " ")[-120:]
    raise RuntimeError(hint)


def _kill_proc(proc) -> bool:
    if not proc:
        return False
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, creationflags=NO_WINDOW)
        return True
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
            return True
        except Exception:  # noqa: BLE001
            return False


def pick_files() -> dict:
    """弹系统选择框。返回 {state, paths, message}；state 为 ok / cancel / busy / timeout / error。"""
    if not PICK_GUARD.acquire(blocking=False):
        return {"state": "busy", "paths": [], "message": "已经有一个选择窗口打开了"}
    proc = None
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", _PS_PICK],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            out_b, err_b = proc.communicate(timeout=PICK_TIMEOUT)
        except subprocess.TimeoutExpired:
            # 对话框卡住（看不见/没人操作）时强制收掉，否则锁会一直占着
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
            return {"state": "timeout", "paths": [],
                    "message": "选择窗口没有响应，已自动关闭。可以再试一次，或直接在页面里粘贴视频路径"}
        out = out_b.decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if any(ln.startswith("__ERROR__") for ln in lines):
            msg = next(ln[9:] for ln in lines if ln.startswith("__ERROR__"))
            return {"state": "error", "paths": [], "message": "打开选择窗口失败：" + msg}
        if "__CANCEL__" in lines or not lines:
            if not lines:
                err = err_b.decode("utf-8", errors="replace").strip().splitlines()
                if err:
                    return {"state": "error", "paths": [], "message": "打开选择窗口失败：" + err[-1][:200]}
            return {"state": "cancel", "paths": [], "message": ""}
        return {"state": "ok", "paths": lines, "message": ""}
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "paths": [], "message": f"打开选择窗口失败：{exc}"}
    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        PICK_GUARD.release()


def register_path(path: str, uploaded: bool = False) -> dict | None:
    p = Path(path)
    if not p.exists() or not p.is_file():
        return None
    token = uuid.uuid4().hex[:10]
    info = {
        "token": token,
        "name": p.name,
        "path": str(p),
        "size": p.stat().st_size,
        "uploaded": uploaded,
        "scenes": [],
    }
    try:
        info["plan"] = proxy_plan(info)
        info["proxyReady"] = proxy_ready(info)
    except Exception:
        pass
    with LOCK:
        VIDEOS[token] = info
    return info


# ---------------------------------------------------------------- 方案 / 渲染

def _text_overlap(a: dict, b: dict) -> float:
    """两份脚本台词的重合度。用来判断「重挑」到底有没有换内容。

    素材短的时候（一条 25 秒的素材只有 8 句，第一版就全用完了）排除旧句后
    无句可挑，重挑会原样返回同一批句子——肉眼看就是「点了没反应」。
    """
    def key(s):
        t = s.get("tts") or {}
        return {str(x.get("text") or "").strip() for x in (t.get("sentences") or [])
                if str(x.get("text") or "").strip()}
    ka, kb = key(a), key(b)
    if not ka or not kb:
        return 0.0
    return len(ka & kb) / max(len(ka), len(kb))


def plan_view(p: dict) -> list[dict]:
    """把项目里的候选脚本整理成前端卡片要的样子。"""
    out = []
    for i, s in enumerate(p.get("scripts") or []):
        t = s.get("tts") or {}
        sents = t.get("sentences") or []
        out.append({
            "idx": i,
            "route": s.get("route") or "source",
            "angle": s.get("angle_label") or "",
            "total": round(float(t.get("total") or 0), 1),
            "count": len(sents),
            "source": s.get("source_name") or "",
            "seed": int(s.get("seed") or 0),
            "note": s.get("note") or "",
            "numbers": s.get("check_numbers") or [],
            "lines": [str(x.get("text") or "") for x in sents],
            "why": ((s.get("picks") or [{}])[0].get("why") or ""),
        })
    return out


def render_scripts(job: dict, p: dict, idxs: list, cfg: dict) -> tuple[list, list]:
    """按方案号逐条对齐 + 渲染。单条失败不拖垮整批。"""
    n = len(idxs)
    outs, fails = [], []
    scripts = p.get("scripts") or []
    for k, si in enumerate(idxs):
        pipe.job_gate(job)
        job["state"] = "running"
        if not (0 <= si < len(scripts)):
            fails.append({"idx": si, "err": "方案不存在"})
            continue
        s = scripts[si]
        acfg = dirw.align_cfg(cfg, s)
        try:
            job["message"] = "[%d/%d] 分镜…" % (k + 1, n)
            pipe.align(p, s, acfg, seed=None if k == 0 else k * 13 + 7)
            pipe.project_save(p)
            job["message"] = "[%d/%d] 渲染…" % (k + 1, n)
            pipe.compose_worker(p, s, acfg, _BatchJob(job, k, n),
                                variant=("V%d" % (k + 1)) if n > 1 else "")
            outs.append(job.get("result") or {})
        except pipe.JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            fails.append({"idx": si, "err": str(exc)[:180]})
            job["message"] = "[%d/%d] 失败：%s" % (k + 1, n, str(exc)[:60])
        job["done"] = k + 1
        job["total"] = n
    # 最后一条 compose 会把 state 写成 done，这里必须交回 running：
    # 后面还要跑体检，提前 done 会让前端收工、读到还没赋值的 result。
    if not job.get("cancel"):
        job["state"] = "running"
    return outs, fails


def qc_outputs(outs: list, job: dict, cfg: dict) -> None:
    """渲染完顺手体检一遍（结果进缓存，成片列表里直接看分数）。

    体检失败不影响成片，也不该让整批报错——单条兜住。
    """
    files = [str(o.get("out")) for o in outs if o.get("out")]
    if not files or job.get("cancel"):
        return
    job["state"] = "running"
    for k, f in enumerate(files):
        if job.get("cancel"):
            return
        job["message"] = "[%d/%d] 体检 %s…" % (k + 1, len(files), Path(f).name)
        qcmod.audit_safe(f, cfg, force=True)
    job["state"] = "running"


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ShotCutter/1.0"
    protocol_version = "HTTP/1.1"
    head_only = False

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    # ---------- helpers
    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.head_only:
            return
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str, extra: dict | None = None):
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if self.head_only:
            return
        self.wfile.write(data)

    def _stream(self, path: Path, attachment: str = ""):
        """支持 Range 的分块流式输出，浏览器才能拖动进度条。attachment 非空则按附件下载。"""
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        ctype = MIME.get(path.suffix.lower(), "application/octet-stream")
        start, end, partial = 0, size - 1, False

        rng = (self.headers.get("Range") or "").strip()
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)$", rng)
            if m:
                a, b = m.group(1), m.group(2)
                if not a and b:
                    start = max(0, size - int(b))
                elif a:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                partial = start != 0 or end != size - 1

        length = max(0, end - start + 1)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        if attachment:
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%s" % quote(attachment))
        self.end_headers()
        if self.head_only:
            return
        try:
            with path.open("rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    buf = f.read(min(262144, remain))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remain -= len(buf)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _body_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # ---------- GET
    def do_HEAD(self):  # noqa: N802
        self.head_only = True
        try:
            self.do_GET()
        finally:
            self.head_only = False

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path in ("/", "/home.html"):
            self._file(WEB / "home.html", "text/html; charset=utf-8")
        elif u.path in ("/cutter", "/index.html"):
            self._file(WEB / "index.html", "text/html; charset=utf-8")
        elif u.path in ("/sub", "/sub.html"):
            self._file(WEB / "sub.html", "text/html; charset=utf-8")
        elif u.path in ("/hd", "/hd.html"):
            self._file(WEB / "hd.html", "text/html; charset=utf-8")
        elif u.path == "/media":
            token = (q.get("token") or [""])[0]
            video = VIDEOS.get(token)
            if not video:
                self._json({"error": "视频不存在，请重新添加"}, 404)
                return
            src = proxy_path(video) if (q.get("proxy") or [""])[0] == "1" else Path(video["path"])
            self._stream(src)
        elif u.path == "/api/shutdown":
            # 给桌面版（ShotCutter.exe）用：网页页脚点「退出服务」就停掉
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                self._json({"error": "只允许本机操作"}, 403)
                return
            busy = any(j.get("state") == "running" for j in JOBS.values())
            if busy or HD_JOB.running or SUB_JOB.running:
                self._json({"error": "还有任务在跑，等它跑完再退出"}, 400)
                return
            self._json({"ok": True, "message": "服务正在退出"})
            threading.Timer(0.8, lambda: os._exit(0)).start()
        elif u.path == "/api/health":
            try:
                import scenedetect
                ver = scenedetect.__version__
            except Exception:
                ver = "未安装"
            self._json({
                "ok": True,
                "scenedetect": ver,
                "ffmpeg": Path(FFMPEG).name if FFMPEG else "未找到",
                "detectors": {k: {"label": v["label"], "default": v["default"], "min": v["min"],
                                  "max": v["max"], "step": v["step"], "hint": v["hint"]}
                              for k, v in DETECTORS.items()},
                "work": str(WORK),
            })
        elif u.path == "/thumb":
            token = (q.get("token") or [""])[0]
            key = (q.get("t") or [""])[0]
            if not re.fullmatch(r"[0-9a-f]{10}", token) or not key.isdigit():
                self.send_error(400)
                return
            self._file(THUMBS / f"{token}_{key}.jpg", "image/jpeg")
        elif u.path == "/api/probe":
            token = (q.get("token") or [""])[0]
            video = VIDEOS.get(token)
            if not video:
                self._json({"error": "视频已失效，请重新添加"}, 404)
                return
            plan = proxy_plan(video)
            jid = PROXY_JOBS.get(proxy_key(video))
            running = bool(jid and JOBS.get(jid, {}).get("state") == "running")
            self._json({"ok": True, "plan": plan, "proxyReady": proxy_ready(video),
                        "job": jid if running else None})
        elif u.path == "/api/job":
            jid = (q.get("id") or [""])[0]
            job = JOBS.get(jid)
            if not job:
                self._json({"error": "任务不存在"}, 404)
            else:
                self._json(job)
        elif u.path == "/api/hd/info":
            self._json({"ok": True, "video2x": V2X.exists(), "ffprobe": VU_FFPROBE.exists(),
                        "out": str(VU_OUT), "gpus": hd_gpus(),
                        "root": str(VU_ROOT)})
        elif u.path == "/api/hd/status":
            self._json(HD_JOB.snapshot())
        elif u.path == "/api/sub/info":
            self._json({"ok": True, "vsr": VSR_PY.exists() and VSR_RUN.exists(),
                        "root": str(VSR_ROOT), "gpu": gpu_name()})
        elif u.path == "/api/sub/status":
            self._json(SUB_JOB.snapshot())
        elif u.path == "/api/sub/frame":
            path = (q.get("path") or [""])[0]
            ts = (q.get("t") or ["0"])[0]
            if not Path(path).is_file():
                self._json({"error": "文件不存在"}, 404)
                return
            try:
                img, at = sub_frame(path, float(ts or 0))
                self._file(img, "image/jpeg", {"X-Frame-At": "%.2f" % at})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)
        elif u.path in ("/pipeline", "/pipe.html"):
            self._file(WEB / "pipe.html", "text/html; charset=utf-8")
        elif u.path in ("/one", "/one.html"):
            self._file(WEB / "one.html", "text/html; charset=utf-8")
        elif u.path == "/api/pipe/config":
            self._json(pipe.public_cfg())
        elif u.path == "/api/pipe/desub/info":
            self._json(pipe.desub_info())
        elif u.path == "/api/pipe/desub/list":
            items = pipe.desub_list()
            total = sum(i["size"] for i in items)
            self._json({"ok": True, "items": items, "count": len(items), "size": total})
        elif u.path == "/api/pipe/desub/file":
            name = (q.get("name") or [""])[0]
            f = pipe.DESUB_DIR / Path(name).name if name else None
            if f and f.exists() and f.is_file() and f.suffix.lower() == ".mp4":
                self._stream(f, attachment=f.name)
            else:
                self.send_error(404)
        elif u.path == "/api/pipe/angles":
            self._json({"items": [{"key": k, "label": v["label"], "hint": v.get("hint") or "",
                                   "group": v.get("group") or "classic"}
                                  for k, v in pipe.ANGLES.items()]})
        elif u.path in ("/api/pipe/sub/cues", "/api/pipe/sub/preview"):
            pid = (q.get("pid") or [""])[0]
            try:
                idx = int((q.get("idx") or ["0"])[0] or 0)
                at = float((q.get("t") or ["1.0"])[0] or 1.0)
            except ValueError:
                self._json({"error": "参数不对"}, 400)
                return
            p = pipe.project_load(pid)
            scripts = (p or {}).get("scripts") or []
            if not p or not (0 <= idx < len(scripts)):
                self._json({"error": "找不到这条方案"}, 404)
                return
            cfg = pipe.load_cfg()
            if u.path == "/api/pipe/sub/cues":
                plan = pipe.sub_plan(p, scripts[idx], cfg)
                self._json({"ok": True, "idx": idx,
                            "video": {"w": cfg["video"]["w"], "h": cfg["video"]["h"],
                                      "sub_max_chars": plan["max_chars"]},
                            "hook": plan["hook"], "cues": plan["cues"]})
                return
            try:
                img = pipe.sub_preview(p, scripts[idx], cfg, at)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
                return
            self._file(img, "image/jpeg")
        elif u.path == "/api/pipe/llm/local":
            try:
                found = pipe.scan_local()
            except Exception as exc:  # noqa: BLE001
                found = []
                self._json({"ok": False, "error": str(exc), "found": []})
                return
            self._json({"ok": True, "found": found})
        elif u.path == "/api/pipe/llm/status":
            self._json(pipe.local_status())
        elif u.path == "/api/pipe/llm/models":
            base = (q.get("base") or [""])[0] or (pipe.load_cfg()["llm"].get("base_url") or "")
            self._json({"ok": True, "models": pipe.list_models(base, refresh=True)})
        elif u.path == "/api/pipe/pool":
            items = pipe.pool_list()
            for it in items:               # 画面指纹只给去重用，别塞进列表响应
                it.pop("fp", None)
            self._json({
                "ok": True, "items": items, "count": len(items),
                "tagged": sum(1 for i in items if i.get("tagged")),
                "semantic": sum(1 for i in items if i.get("semantic")),
                "health": pipe.pool_health(items),
            })
        elif u.path == "/api/pipe/projects":
            self._json({"ok": True, "items": pipe.project_list()})
        elif u.path == "/api/pipe/project":
            p = pipe.project_load((q.get("id") or [""])[0])
            if not p:
                self._json({"error": "项目不存在"}, 404)
            else:
                self._json({"ok": True, "project": p})
        elif u.path == "/api/pipe/thumb":
            cid = (q.get("id") or [""])[0]
            if not re.fullmatch(r"[0-9a-f]{12}", cid):
                self.send_error(400)
                return
            f = pipe.ensure_thumb(cid)
            if not f:
                self.send_error(404)
            else:
                self._file(f, "image/jpeg")
        elif u.path == "/api/pipe/out":
            name = (q.get("name") or [""])[0]
            f = pipe.OUT / Path(name).name if name else None
            if f and f.exists() and f.is_file():
                self._stream(f)
            else:
                self.send_error(404)
        elif u.path == "/api/pipe/zip":
            name = (q.get("name") or [""])[0]
            f = pipe.ZIP_DIR / Path(name).name if name else None
            if f and f.exists() and f.is_file():
                self._stream(f, attachment=f.name)
            else:
                self.send_error(404)
        elif u.path == "/api/pipe/bgm/list":
            try:
                items = pipe.bgm_list()
                dirs = pipe.bgm_dirs()
                bad = [d for d in dirs if not Path(d).is_dir()]
                self._json({"ok": True, "items": items, "dirs": dirs, "bad": bad})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc), "items": []}, 400)
        elif u.path == "/api/pipe/bgm/audio":
            raw = (q.get("path") or [""])[0]
            f = Path(raw) if raw else None
            ok = False
            if f:
                try:
                    ok = f.is_file() and f.suffix.lower() in pipe.AUDIO_EXT
                except OSError:
                    ok = False
            if ok:
                self._stream(f)
            else:
                self.send_error(404)
        elif u.path == "/api/pipe/bgm/preview":
            # BGM 混音试听：配音轨 + BGM 按当前音量/淡入淡出混一小段，和成片里的听感一致
            # bgm= 本地路径，或 fn= 在线曲库文件名（转成直链交给 ffmpeg 拉流）
            bgm = (q.get("bgm") or [""])[0]
            ofn = (q.get("fn") or [""])[0]
            if ofn and not bgm:
                pieces, genres, oerr = _online_catalog()
                if oerr:
                    self._json({"ok": False, "error": oerr}, 502)
                    return
                if not _online_find(ofn):
                    self._json({"ok": False, "error": "曲目不在在线目录里"}, 404)
                    return
                bgm = _online_url(ofn)
            pid = (q.get("pid") or [""])[0]
            try:
                idx = int((q.get("idx") or ["0"])[0])
            except ValueError:
                idx = 0
            try:
                vol = max(0.0, min(2.0, float((q.get("vol") or ["0.1"])[0])))
            except ValueError:
                vol = 0.1
            try:
                fade = max(0.0, min(5.0, float((q.get("fade") or ["0"])[0])))
            except ValueError:
                fade = 0.0
            try:
                dur = max(5.0, min(60.0, float((q.get("dur") or ["20"])[0])))
            except ValueError:
                dur = 20.0
            if bgm.startswith("http://") or bgm.startswith("https://"):
                ok = True               # 在线直链：ffmpeg 拉流，不做本地校验
                f = None
            else:
                f = Path(bgm) if bgm else None
                ok = False
                if f:
                    try:
                        ok = f.is_file() and f.suffix.lower() in pipe.AUDIO_EXT
                    except OSError:
                        ok = False
            if not ok:
                self._json({"ok": False, "error": "先选一首 BGM 再试听混音"}, 400)
                return
            voice, vlabel = _preview_voice(pid, idx)
            if not voice:
                self._json({"ok": False,
                            "error": "还没有配音轨：先渲染一条成片（A/B 路线都行），再来调 BGM 音量"},
                           404)
                return
            try:
                out = _bgm_preview(bgm, voice, vol, fade, dur)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": "混音失败：%s" % exc}, 500)
                return
            try:
                data = out.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("X-Voice", quote(vlabel))
                self.send_header("Access-Control-Expose-Headers", "X-Voice")
                self.end_headers()
                if not self.head_only:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif u.path == "/api/pipe/bgm/online/search":
            qs = unquote(u.query)
            qq = dict(parse_qs(qs)).get("q", [""])[0].strip()
            refresh = dict(parse_qs(qs)).get("refresh", [""])[0] == "1"
            if refresh:
                _ONLINE.update({"pieces": [], "at": 0.0})
            items = _online_search(qq)
            if not _ONLINE["pieces"]:
                self._json({"ok": False, "error": _ONLINE["err"] or "在线曲库是空的"}, 502)
                return
            self._json({"ok": True, "items": items, "total": len(_ONLINE["pieces"])})
        elif u.path == "/api/pipe/bgm/online/audio":
            # 在线曲目流式代理（透传 Range，浏览器才能拖进度条、边下边播）
            fn = (q.get("fn") or [""])[0]
            pieces, genres, oerr = _online_catalog()
            if oerr:
                self._json({"ok": False, "error": oerr}, 502)
                return
            if not fn or not _online_find(fn):
                self._json({"ok": False, "error": "曲目不存在"}, 404)
                return
            try:
                headers = {}
                rng = (self.headers.get("Range") or "").strip()
                if rng:
                    headers["Range"] = rng
                resp = _http_open(_online_url(fn), headers=headers, timeout=90)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": "拉取失败：%s" % exc}, 502)
                return
            try:
                self.send_response(resp.status if hasattr(resp, "status") else 200)
                ct = resp.headers.get("Content-Type") or ""
                if "audio" not in ct and fn.lower().endswith(".mp3"):
                    ct = "audio/mpeg"          # 上游有时回 octet-stream，浏览器 <audio> 需要音频类型
                if ct:
                    self.send_header("Content-Type", ct)
                for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
                    v = resp.headers.get(h)
                    if v:
                        self.send_header(h, v)
                self.end_headers()
                if not self.head_only:
                    while True:
                        buf = resp.read(65536)
                        if not buf:
                            break
                        self.wfile.write(buf)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        elif u.path == "/api/pipe/audio":
            p, s, err = _pipe_get((q.get("pid") or [""])[0], (q.get("idx") or ["0"])[0])
            audio = (s or {}).get("tts", {}).get("audio")
            if not audio or not Path(audio).exists():
                self.send_error(404)
            else:
                self._stream(Path(audio))
        elif u.path == "/api/pipe/videos":
            out = []
            for v in VIDEOS.values():
                out.append({"token": v["token"], "name": v["name"],
                            "scenes": len(v.get("scenes") or []),
                            "duration": v.get("duration")})
            self._json({"ok": True, "videos": out})
        elif u.path == "/api/pipe/outputs":
            items = []
            try:
                for f in sorted(pipe.OUT.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[:200]:
                    st = f.stat()
                    it = {"name": f.name, "out": str(f), "size": st.st_size,
                          "mtime": st.st_mtime}
                    side = f.with_suffix(".json")
                    if side.exists():
                        try:
                            it.update({k: v for k, v in json.loads(side.read_text(encoding="utf-8")).items()
                                       if k in ("dur", "clips", "kbps", "shots_csv", "ass", "bgm")})
                        except Exception:
                            pass
                    # 老成片的记录里没有码率，用 体积/时长 现算（不占 CPU）
                    if not it.get("kbps"):
                        it["kbps"] = pipe._kbps(st.st_size, float(it.get("dur") or 0))
                    if not it.get("dur"):
                        it["dur"] = 0
                    it["qc"] = qcmod.digest(f)      # 只读缓存，没体检过就是 null
                    items.append(it)
            except Exception:
                pass
            self._json({"ok": True, "items": items})
        elif u.path == "/api/pipe/qc":
            name = Path((q.get("name") or [""])[0]).name
            force = (q.get("force") or [""])[0] in ("1", "true", "yes")
            f = pipe.OUT / name
            if not name or not f.exists():
                self._json({"error": "这条成片不在了：%s" % name}, 404)
                return
            self._json({"ok": True, "qc": qcmod.audit_safe(f, pipe.load_cfg(), force=force)})
        elif re.fullmatch(r"/[\w\-.]+\.(html|css|js|svg|png|ico)", u.path):
            f = WEB / u.path.lstrip("/")
            if f.exists() and f.is_file():
                ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
                         ".png": "image/png", ".ico": "image/x-icon"}.get(f.suffix, "application/octet-stream")
                self._file(f, ctype)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    # ---------- POST
    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/api/upload":
            name = unquote((q.get("name") or ["video.mp4"])[0])
            ext = Path(name).suffix.lower()
            if ext not in VIDEO_EXT:
                self._json({"error": f"不支持的格式 {ext}"}, 400)
                return
            size = int(self.headers.get("Content-Length") or 0)
            dest = UPLOADS / f"{uuid.uuid4().hex[:8]}_{re.sub(r'[^\w.\-\u4e00-\u9fff]+', '_', name)}"
            received = 0
            with open(dest, "wb") as fh:
                while received < size:
                    chunk = self.rfile.read(min(4 << 20, size - received))
                    if not chunk:
                        break
                    fh.write(chunk)
                    received += len(chunk)
            info = register_path(str(dest), uploaded=True)
            if not info:
                self._json({"error": "写入失败"}, 500)
                return
            info["name"] = name
            self._json({"ok": True, "video": info})
            return

        if u.path == "/api/pick":
            res = pick_files()
            if res["state"] != "ok":
                self._json({"ok": False, "state": res["state"],
                            "error": res["message"], "videos": []})
                return
            added = []
            for p in res["paths"]:
                info = register_path(p)
                if info:
                    added.append(info)
            if not added:
                self._json({"ok": False, "state": "error", "videos": [],
                            "error": "选中的文件无法读取（可能已被移动或没有权限）"})
                return
            self._json({"ok": True, "state": "ok", "videos": added})
            return

        payload = self._body_json()
        if u.path == "/api/pipe/bgm/online/download":
            fn = str(payload.get("fn") or "")
            if not fn:
                self._json({"ok": False, "error": "缺 fn"}, 400)
                return
            try:
                path, msg = _online_download(fn)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": "下载失败：%s" % exc}, 500)
                return
            self._json({"ok": True, "path": path, "message": msg})

        if u.path == "/api/addpath":
            raw = payload.get("paths")
            parts = raw if isinstance(raw, list) else re.split(r"[\r\n;；]+", str(raw or ""))
            added, bad = [], []
            for item in parts:
                s = str(item).strip().strip('"').strip("'").strip()
                if not s:
                    continue
                info = register_path(s)
                if info:
                    added.append(info)
                else:
                    bad.append(s)
            if not added:
                self._json({"ok": False, "videos": [], "failed": bad,
                            "error": "路径不存在或不是文件：" + (bad[0] if bad else "（空）")})
                return
            self._json({"ok": True, "videos": added, "failed": bad})
            return

        if u.path == "/api/analyze":
            tokens = [t for t in payload.get("tokens", []) if t in VIDEOS]
            if not tokens:
                self._json({"error": "没有可分析的视频"}, 400)
                return
            params = payload.get("params", {}) or {}
            job = new_job("analyze", len(tokens))
            threading.Thread(target=analyze_worker, args=(tokens, params, job), daemon=True).start()
            self._json({"ok": True, "job": job["id"]})
            return

        if u.path == "/api/export":
            if not FFMPEG:
                self._json({"error": "未找到 ffmpeg，无法导出"}, 500)
                return
            items = payload.get("items", [])
            total = sum(len(i.get("keep", [])) for i in items)
            if not total:
                self._json({"error": "没有选中任何片段"}, 400)
                return
            job = new_job("export", total)
            dedup = payload.get("dedup") or None
            if dedup not in DEDUP_LEVELS:
                dedup = None
            threading.Thread(target=export_worker,
                             args=(items, payload.get("mode", "copy"), payload.get("out_dir"), job, dedup),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"]})
            return

        if u.path == "/api/scenes":
            video = VIDEOS.get(payload.get("token", ""))
            if not video or not video.get("duration"):
                self._json({"error": "视频尚未检测"}, 400)
                return
            try:
                build_scenes(video, payload.get("cuts") or [])
                make_thumbs(video["token"], video["path"], video["scenes"])
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                return
            self._json({"ok": True, "video": video})
            return

        if u.path == "/api/edl":
            video = VIDEOS.get(payload.get("token", ""))
            if not video or not video.get("scenes"):
                self._json({"error": "没有可导出的片段"}, 400)
                return
            try:
                keep = [int(i) for i in payload.get("keep", []) if 0 <= int(i) < len(video["scenes"])]
                if not keep:
                    self._json({"error": "没有选中任何片段"}, 400)
                    return
                out = Path(payload["out_dir"]) if payload.get("out_dir") else Path(default_out_dir(video))
                path, n = write_edl(video, keep, out)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
                return
            self._json({"ok": True, "path": str(path), "n": n})
            return

        if u.path == "/api/proxy":
            video = VIDEOS.get(payload.get("token", ""))
            if not video:
                self._json({"error": "视频已失效，请重新添加"}, 404)
                return
            plan = proxy_plan(video)
            if proxy_ready(video):
                self._json({"ok": True, "job": None, "ready": True, "plan": plan})
                return
            prev = PROXY_JOBS.get(proxy_key(video))
            if prev and JOBS.get(prev, {}).get("state") == "running":
                self._json({"ok": True, "job": prev, "ready": False, "plan": plan})
                return
            job = new_job("proxy", 1)
            job["mode"] = plan["mode"]
            job["message"] = plan["eta"] or "生成预览代理…"
            PROXY_JOBS[proxy_key(video)] = job["id"]
            threading.Thread(target=proxy_worker, args=(video, job), daemon=True).start()
            self._json({"ok": True, "job": job["id"], "ready": False, "plan": plan})
            return

        if u.path == "/api/openfolder":
            res = open_in_explorer(payload.get("path"))
            if res.get("ok"):
                self._json(res)
            else:
                self._json({"error": res.get("error") or "打开失败"}, 404)
            return

        # ---------- 独立工具：视频变高清 ----------
        if u.path == "/api/tool/pick":
            res = pick_files()
            self._json({"ok": res["state"] == "ok", "state": res["state"],
                        "error": res["message"], "paths": res["paths"]})
            return

        if u.path == "/api/hd/probe":
            try:
                self._json({"ok": True, **tool_probe(payload.get("path", ""))})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/hd/start":
            if HD_JOB.running:
                self._json({"error": "已有高清任务在跑"}, 409)
                return
            if not V2X.exists():
                self._json({"error": "找不到 video2x：%s" % V2X}, 400)
                return
            inp = (payload.get("input") or "").strip().strip('"')
            if not os.path.isfile(inp):
                self._json({"error": "找不到这个文件：%s" % inp}, 400)
                return
            threading.Thread(target=hd_worker, args=(payload,), daemon=True).start()
            self._json({"ok": True})
            return

        if u.path == "/api/hd/cancel":
            ok = _kill_proc(HD_JOB.proc)
            self._json({"ok": ok})
            return

        # ---------- 独立工具：字幕去除 ----------
        if u.path == "/api/sub/probe":
            try:
                self._json({"ok": True, **tool_probe(payload.get("path", ""))})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/sub/start":
            if SUB_JOB.running:
                self._json({"error": "已有去字幕任务在跑"}, 409)
                return
            if not (VSR_PY.exists() and VSR_RUN.exists()):
                self._json({"error": "找不到 VSR：%s" % VSR_PY}, 400)
                return
            inp = (payload.get("input") or "").strip().strip('"')
            if not os.path.isfile(inp):
                self._json({"error": "找不到这个文件：%s" % inp}, 400)
                return
            area = (payload.get("area") or "").strip()
            if area and area.lower() not in ("none", "null") \
                    and not re.fullmatch(r"\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+", area):
                self._json({"error": "区域格式不对，应为 ymin,ymax,xmin,xmax"}, 400)
                return
            threading.Thread(target=sub_worker, args=(inp, area), daemon=True).start()
            self._json({"ok": True})
            return

        if u.path == "/api/sub/cancel":
            ok = _kill_proc(SUB_JOB.proc)
            self._json({"ok": ok})
            return

        if u.path == "/api/tool/play":
            f = (payload.get("path") or "").strip().strip('"')
            if not os.path.isfile(f):
                self._json({"error": "文件不存在：%s" % f}, 404)
                return
            _unlock_foreground()
            os.startfile(f)  # noqa: S606
            self._json({"ok": True, "path": f})
            return

        # ---------------- 成片流水线 ----------------
        if u.path == "/api/pipe/config":
            try:
                cfg = pipe.save_cfg(payload)
                self._json({"ok": True, "config": pipe.public_cfg(cfg)})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/pipe/dialog":
            self._json(pick_folder(str(payload.get("path") or payload.get("folder") or "")))
            return

        if u.path == "/api/pipe/resolve":
            self._json(resolve_dropped(payload.get("names") or [], payload.get("known") or []))
            return

        if u.path == "/api/pipe/llm/start":
            self._json(pipe.start_local())
            return

        if u.path == "/api/pipe/llm/stop":
            self._json(pipe.stop_local())
            return

        if u.path == "/api/job/control":
            jid = str(payload.get("id") or "")
            act = str(payload.get("action") or "")
            job = JOBS.get(jid)
            if not job:
                self._json({"error": "任务不存在（可能已经结束）"}, 404)
                return
            if act == "stop":
                job["cancel"] = True
                job["pause"] = False
                job["message"] = ("正在停止…（等当前这次请求返回）"
                                  if job.get("kind") in ("script", "tts") else "正在停止…")
            elif act == "pause":
                job["pause"] = True
            elif act == "resume":
                job["pause"] = False
                if job.get("state") == "paused":
                    job["state"] = "running"
            else:
                self._json({"error": "action 只能是 stop / pause / resume"}, 400)
                return
            self._json({"ok": True, "id": jid, "state": job.get("state"),
                        "cancel": bool(job.get("cancel")), "pause": bool(job.get("pause"))})
            return

        if u.path == "/api/pipe/testllm":
            try:
                cfg = pipe.load_cfg()
                txt = pipe.llm_chat([{"role": "user", "content": "只回复两个字：可用"}],
                                    cfg, max_tokens=24, temperature=0, timeout=40)
                self._json({"ok": True, "reply": (txt or "").strip().replace("\n", " ")[:60]})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/pipe/plan":
            # 只出方案、不渲染：先给几个方向，停下来等人挑
            raw = payload.get("folders")
            if isinstance(raw, (list, tuple)):
                folders = [str(x) for x in raw]
            else:
                folders = [payload.get("folder") or ""]
            norm = []
            for f in folders:
                for line in str(f).replace(";", "\n").splitlines():
                    line = line.strip().strip('"').strip("'")
                    if line and line not in norm:
                        norm.append(line)
            product = payload.get("product") or {}
            try:
                target = float(payload.get("target") or 45)
                count = max(1, min(10, int(payload.get("count") or payload.get("variants") or 3)))
                mix = max(0.0, min(1.0, float(payload.get("visual_mix", 0.5))))
                seed0 = max(0, int(payload.get("seed0") or 0))
                rep = payload.get("replace_idx", None)
                rep = None if rep in (None, "", "-1", -1) else int(rep)
            except (TypeError, ValueError):
                self._json({"error": "时长/条数/空镜比例填的不是数字"}, 400)
                return
            mode = str(payload.get("mode") or "auto").lower()
            if mode not in ("auto", "source", "script"):
                mode = "auto"
            use_llm = bool(payload.get("use_llm", True))
            topup = bool(payload.get("topup", False))
            raw_skip = payload.get("skip_paths") or []
            skip_paths = [str(x) for x in raw_skip] if isinstance(raw_skip, (list, tuple)) else []
            p = pipe.project_load(payload.get("pid") or "")
            if not p:
                p = pipe.project_new(product.get("name") or "一键成片")
            elif rep is None:
                p["scripts"] = []      # 重新出方案 = 换一批候选，不在旧的后面叠加
            cfg = pipe.load_cfg()
            job = new_job("plan", 100)

            def _plan(job=job, p=p, product=product, target=target, cfg=cfg,
                      use_llm=use_llm, norm=norm, mix=mix, mode=mode, topup=topup,
                      count=count, seed0=seed0, rep=rep, skip_paths=skip_paths):
                job["message"] = "体检素材…"
                old = None
                if rep is not None:
                    cur = p.get("scripts") or []
                    if 0 <= rep < len(cur):
                        old = cur[rep]
                # 重挑要锁定这一条原来用的素材，否则会从素材列表第一条重新分配，
                # 挑出来还是同一条片，等于白点
                only = [old.get("source_path")] if (old and old.get("source_path")) else None
                avoid = None
                if old:
                    avoid = [str(x.get("text") or "").strip()
                             for x in ((old.get("tts") or {}).get("sentences") or [])
                             if str(x.get("text") or "").strip()]
                base = len(p.get("scripts") or [])
                info = dirw.director_worker(job, p, product, target, cfg, use_llm,
                                            "keep", norm, mix, True, mode, topup,
                                            1 if rep is not None else count, seed0,
                                            only, avoid, skip_paths)
                job["state"] = "running"
                # 重挑但换不出新句子（这条素材的句子被第一版用光了）时，改走 AI 写稿换角度：
                # 用户点「重挑」是想看见一条不一样的，返回同一批句子等于没反应。
                if rep is not None and old:
                    fresh = (p.get("scripts") or [])[-1]
                    if _text_overlap(old, fresh) >= 0.7:
                        job["message"] = "素材句数不够，改用 AI 写稿换角度…"
                        # director_worker 自己会把 job 标成 done，第二次调用前必须复位：
                        # 不复位的话前端轮询立刻看到 done，读到的是空方案列表。
                        job["state"] = "running"
                        job["progress"] = 0.0
                        p["scripts"].pop()          # 撤掉这次挑的，下面会重新算
                        info = dirw.director_worker(job, p, product, target, cfg, use_llm,
                                                    "keep", norm, 0.0, True, "script",
                                                    topup, 1, seed0 + 1,
                                                    None, None, skip_paths)
                        job["state"] = "running"
                        fresh = (p.get("scripts") or [])[-1]
                        fresh["note"] = ((fresh.get("note") or "")
                                         + "；这条素材的句子第一版已用光，改用 AI 写稿换了个角度"
                                         ).strip("；")
                        info["switched"] = "script"
                # 把「本次新加的脚本」并回列表：重挑时替换掉旧的那一条，
                # 出方案时整体换掉。不能靠 pop()——切换路线后列表可能只剩一条。
                lst = p.get("scripts") or []
                new_ones = lst[base:]
                del lst[base:]
                if rep is not None:
                    if new_ones and 0 <= rep < len(lst):
                        lst[rep] = new_ones[-1]
                    elif new_ones:
                        lst.append(new_ones[-1])
                else:
                    lst[:] = new_ones
                for i, s in enumerate(lst):
                    s["idx"] = i
                p["scripts"] = lst
                p["active"] = 0
                pipe.project_save(p)
                job["result"] = {"pid": p["id"], "plans": plan_view(p), "info": info,
                                 "materials": (info or {}).get("materials") or []}
                job["state"] = "done"
                job["progress"] = 1.0
                job["message"] = "出了 %d 个方案，挑一个渲染" % len(p.get("scripts") or [])

            threading.Thread(target=_run_job, args=(job, _plan), daemon=True).start()
            self._json({"ok": True, "job": job["id"], "pid": p["id"]})
            return

        if u.path == "/api/pipe/render":
            p = pipe.project_load(payload.get("pid") or "")
            if not p:
                self._json({"error": "项目不存在"}, 404)
                return
            scripts = p.get("scripts") or []
            raw = payload.get("idxs", None)
            if raw in (None, ""):
                idxs = list(range(len(scripts)))
            elif isinstance(raw, (list, tuple)):
                idxs = [int(x) for x in raw]
            else:
                idxs = [int(raw)]
            idxs = [i for i in idxs if 0 <= i < len(scripts)][:20]
            if not idxs:
                self._json({"error": "没有可渲染的方案"}, 400)
                return
            cfg = pipe.load_cfg()
            job = new_job("render", len(idxs))

            def _run(job=job, p=p, idxs=idxs, cfg=cfg):
                outs, fails = render_scripts(job, p, idxs, cfg)
                qc_outputs(outs, job, cfg)
                job["result"] = {"pid": p["id"], "outs": outs, "failed": fails, "idxs": idxs}
                job["state"] = "done"
                job["progress"] = 1.0
                job["done"] = len(idxs)
                job["total"] = len(idxs)
                job["message"] = "出片 %d/%d 条%s" % (
                    len(outs), len(idxs), "，%d 条失败" % len(fails) if fails else "")

            threading.Thread(target=_run_job, args=(job, _run), daemon=True).start()
            self._json({"ok": True, "job": job["id"], "pid": p["id"], "count": len(idxs)})
            return

        if u.path == "/api/pipe/direct":
            raw = payload.get("folders")
            if isinstance(raw, (list, tuple)):
                folders = [str(x) for x in raw]
            else:
                folders = [payload.get("folder") or ""]
            norm = []
            for f in folders:
                for line in str(f).replace(";", "\n").splitlines():
                    line = line.strip().strip('"').strip("'")
                    if line and line not in norm:
                        norm.append(line)
            product = payload.get("product") or {}
            try:
                target = float(payload.get("target") or 45)
                variants = max(1, min(10, int(payload.get("variants") or 1)))
                mix = max(0.0, min(1.0, float(payload.get("visual_mix", 0.5))))
            except (TypeError, ValueError):
                self._json({"error": "时长/条数/空镜比例填的不是数字"}, 400)
                return
            use_llm = bool(payload.get("use_llm", True))
            mode = str(payload.get("mode") or "auto").lower()
            if mode not in ("auto", "source", "script"):
                mode = "auto"
            topup = bool(payload.get("topup", False))
            p = pipe.project_load(payload.get("pid") or "")
            if not p:
                p = pipe.project_new(product.get("name") or "一键成片")
            cfg = pipe.load_cfg()
            job = new_job("direct", 100)

            def _one(job=job, p=p, product=product, target=target,
                     variants=variants, mix=mix, use_llm=use_llm, norm=norm, cfg=cfg,
                     mode=mode, topup=topup):
                job["message"] = "先给素材做体检…"
                info = dirw.director_worker(job, p, product, target, cfg, use_llm,
                                            "keep", norm, mix, True, mode, topup,
                                            variants)
                # 现在每条版本都是一份独立脚本（A 换素材/换挑法，B 换角度）
                idxs = info.get("script_idxs") or [info["script_idx"]]
                outs, fails = render_scripts(job, p, idxs, cfg)
                qc_outputs(outs, job, cfg)
                n = len(idxs)
                job["result"] = {"pid": p["id"], "idx": info["script_idx"],
                                 "info": info, "outs": outs, "failed": fails}
                job["state"] = "done"
                job["progress"] = 1.0
                job["done"] = n
                job["total"] = n
                job["message"] = "出片 %d/%d 条（原声 %d / 配音 %d）%s" % (
                    len(outs), n, info.get("plan", []).count("source"),
                    info.get("plan", []).count("script"),
                    "，%d 条失败" % len(fails) if fails else "")
            threading.Thread(target=_run_job, args=(job, _one), daemon=True).start()
            self._json({"ok": True, "job": job["id"], "pid": p["id"]})
            return

        if u.path == "/api/pipe/scan":
            raw = payload.get("folders")
            if isinstance(raw, (list, tuple)):
                folders = [str(x) for x in raw]
            elif isinstance(raw, str) and raw.strip():
                folders = [raw]
            else:
                folders = [payload.get("folder") or ""]
            norm = []
            for f in folders:
                for line in str(f).replace(";", "\n").splitlines():
                    line = line.strip().strip('"').strip("'")
                    if line and line not in norm:
                        norm.append(line)
            if not norm:
                self._json({"error": "请填素材文件夹路径"}, 400)
                return
            job = new_job("scan", 100)

            def _scan(job=job, folders=norm):
                try:
                    job["message"] = "扫描 %d 个文件夹…" % len(folders)
                    res = pipe.scan_folders(folders, job)
                    job["result"] = res
                    # 记住这次扫的目录，下次「选择文件夹」从这里开始，别再跳回桌面
                    remember_dir(norm[0])
                    msg = "新增 %d 个素材（%d 个目录，共扫描 %d 个视频）" % (
                        res["added"], len(res["folders"]), res["total"])
                    if res.get("existed"):
                        msg += "，%d 个本来就在池里" % res["existed"]
                    if res.get("skipped"):
                        msg += "，跳过 %d 个读不出时长的" % res["skipped"]
                    if res.get("bad"):
                        msg += "；目录不存在已跳过：" + "、".join(res["bad"][:2])
                    job["message"] = msg
                    job["state"] = "done"
                    job["progress"] = 1.0
                except pipe.JobCancelled:
                    job["state"] = "cancel"
                    job["message"] = "已停止：已加入的素材都保留了"
                except Exception as exc:  # noqa: BLE001
                    job["state"] = "error"
                    job["error"] = str(exc)
                    job["message"] = "扫描失败：%s" % exc
                finally:
                    job["elapsed"] = round(time.time() - job["t0"], 1)

            threading.Thread(target=_scan, daemon=True).start()
            self._json({"ok": True, "job": job["id"]})
            return

        if u.path == "/api/pipe/addvideo":
            raw = payload.get("tokens")
            if isinstance(raw, (list, tuple)):
                toks = [str(x).strip() for x in raw if str(x).strip()]
            else:
                one = str(payload.get("token") or "").strip()
                toks = [one] if one else []
            if not toks:
                self._json({"error": "先选要导入的视频"}, 400)
                return
            added, used, skipped = 0, 0, []
            for t in toks:
                video = VIDEOS.get(t)
                if not video or not video.get("scenes"):
                    skipped.append(t)
                    continue
                added += pipe.add_scenes_of(video)
                used += 1
            if not used:
                self._json({"error": "这些视频都还没检测出镜头，先在切割器里检测一遍"}, 400)
                return
            self._json({"ok": True, "added": added, "videos": used,
                        "skipped": len(skipped)})
            return

        if u.path == "/api/pipe/desub/zip":
            try:
                self._json(pipe.desub_zip(payload.get("names") or None))
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/pipe/desub/delete":
            names = payload.get("names") or []
            if not names:
                self._json({"error": "先勾选要删的文件"}, 400)
                return
            try:
                self._json(pipe.desub_delete(names))
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/pipe/desub/pool":
            try:
                self._json(pipe.desub_add(payload.get("names") or None))
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/pipe/desub":
            folders = payload.get("folders") or []
            if isinstance(folders, str):
                folders = [folders]
            ids = payload.get("ids") or []
            try:
                videos = pipe.desub_collect(folders, ids)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
                return
            if not videos:
                self._json({"ok": True, "job": None, "message": "没有要处理的视频（已处理过的会跳过）"})
                return
            info = pipe.desub_info()
            if not info["ok"]:
                self._json({"error": "没找到 VSR：%s（检查 pipeline.json 的 desub.root）" % info["root"]}, 400)
                return
            job = new_job("desub", len(videos))
            job["message"] = "准备去字幕 %d 个视频…" % len(videos)
            opts = {"mode": payload.get("mode") or "auto",
                    "box": payload.get("box") or [72, 96, 2, 98],
                    "add_pool": payload.get("add_pool", True),
                    "force": bool(payload.get("force"))}
            threading.Thread(target=_run_job,
                             args=(job, pipe.desub_worker, videos, pipe.load_cfg(), job, opts),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"], "count": len(videos),
                        "out_dir": info["out_dir"]})
            return

        if u.path == "/api/pipe/tag":
            ids = payload.get("ids") or []
            force = bool(payload.get("force"))
            items = pipe.pool_list()
            if ids:
                want = set(ids)
                items = [i for i in items if i["id"] in want]
            elif not force:
                items = [i for i in items if not i.get("tagged")]
            if not items:
                self._json({"ok": True, "job": None, "message": "没有需要打标的素材"})
                return
            job = new_job("tag", len(items))
            threading.Thread(target=_run_job, args=(job, pipe.tag_worker, items, pipe.load_cfg(), job, force),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"], "count": len(items)})
            return

        if u.path == "/api/pipe/output/delete":
            try:
                res = pipe.out_delete(payload.get("name") or "")
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
                return
            self._json(res)
            return

        if u.path == "/api/pipe/output/zip":
            try:
                res = pipe.out_zip(payload.get("names") or [])
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
                return
            self._json(res)
            return

        if u.path == "/api/pipe/pool/restore":
            self._json({"ok": True, "restored": pipe.pool_restore(payload.get("items") or [])})
            return

        if u.path == "/api/pipe/pool/dedup":
            items = pipe.pool_list()
            if len(items) < 2:
                self._json({"ok": True, "job": None, "message": "素材不足 2 条，不用去重"})
                return
            job = new_job("dedup", len(items))
            threading.Thread(target=_run_job, args=(job, pipe.dedup_worker, items, job),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"], "count": len(items)})
            return

        if u.path == "/api/pipe/pool/clear":
            n = pipe.pool_clear(bool(payload.get("untagged_only")))
            self._json({"ok": True, "removed": n})
            return

        if u.path == "/api/pipe/pool/drop":
            ids = payload.get("ids")
            if isinstance(ids, (list, tuple)):
                self._json({"ok": True, "removed": pipe.pool_drop_many(ids)})
            else:
                self._json({"ok": pipe.pool_drop(payload.get("id") or "")})
            return

        if u.path == "/api/pipe/project":
            act = payload.get("action") or "get"
            if act == "new":
                p = pipe.project_new(payload.get("name") or "")
                self._json({"ok": True, "project": p})
                return
            if act == "delete":
                self._json({"ok": pipe.project_delete(payload.get("id") or "")})
                return
            pid = payload.get("id") or ""
            p = pipe.project_load(pid)
            if not p:
                self._json({"error": "项目不存在"}, 404)
                return
            if act == "save":
                for k in ("name", "product", "active"):
                    if k in payload:
                        p[k] = payload[k]
                if isinstance(payload.get("scripts"), list):   # 整份脚本覆盖（重建项目时用）
                    p["scripts"] = payload["scripts"]
                if payload.get("script") is not None:
                    idx = int(payload.get("idx") or 0)
                    if 0 <= idx < len(p.get("scripts") or []):
                        p["scripts"][idx].update(payload["script"])
                pipe.project_save(p)
            self._json({"ok": True, "project": p})
            return

        if u.path == "/api/pipe/script":
            p = pipe.project_load(payload.get("pid") or "")
            if not p:
                self._json({"error": "项目不存在"}, 404)
                return
            product = payload.get("product") or p.get("product") or {}
            p["product"] = product
            # 换品名重新写稿时，项目名跟着品名走，成片文件名才不会一直用旧名字
            new_name = (product.get("name") or "").strip()
            if new_name:
                p["name"] = new_name
            angles = payload.get("angles") or list(pipe.ANGLES)
            target = float(payload.get("target") or 30)
            use_llm = payload.get("use_llm", True)
            tags = []
            for it in pipe.pool_list():
                tags.extend(it.get("tags") or [])

            job = new_job("script", 1)

            def _go():
                job["message"] = "写脚本…"
                r = pipe.gen_scripts(product, angles, target, pipe.load_cfg(),
                                     list(dict.fromkeys(tags)), bool(use_llm), job)
                for i, s in enumerate(r["scripts"]):
                    s["idx"] = i
                    s["target"] = target
                p["scripts"] = r["scripts"]
                p["active"] = 0
                pipe.project_save(p)
                job["result"] = {"mode": r["mode"], "note": r["note"], "count": len(r["scripts"])}
                job["message"] = "生成 %d 条脚本（%s）" % (
                    len(r["scripts"]), "大模型" if r["mode"] == "llm" else "模板")
                if r.get("cancelled"):
                    job["state"] = "cancel"
                    job["message"] = r.get("note") or "已停止"

            threading.Thread(target=_run_job, args=(job, _go), daemon=True).start()
            self._json({"ok": True, "job": job["id"]})
            return

        if u.path == "/api/pipe/tts_all":
            p = pipe.project_load(payload.get("pid") or "")
            if not p:
                self._json({"error": "项目不存在"}, 404)
                return
            scripts = p.get("scripts") or []
            if not scripts:
                self._json({"error": "还没有脚本，先在第 2 步生成脚本"}, 400)
                return
            job = new_job("tts", len(scripts))
            job["message"] = "准备给 %d 条脚本配音…" % len(scripts)
            threading.Thread(target=_run_job,
                             args=(job, pipe.tts_all_worker, p, pipe.load_cfg(), job,
                                   bool(payload.get("force"))),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"], "count": len(scripts)})
            return

        if u.path == "/api/pipe/tts":
            p, s, err = _pipe_get(payload.get("pid"), payload.get("idx"))
            if err:
                self._json({"error": err}, 400)
                return
            job = new_job("tts", len(s.get("sentences") or []))
            threading.Thread(target=_run_job, args=(job, pipe.tts_worker, p, s, pipe.load_cfg(), job),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"]})
            return

        if u.path == "/api/pipe/align":
            p, s, err = _pipe_get(payload.get("pid"), payload.get("idx"))
            if err:
                self._json({"error": err}, 400)
                return
            try:
                plan = pipe.align(p, s, pipe.load_cfg())
                pipe.project_save(p)
                self._json({"ok": True, "plan": plan})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/pipe/swap":
            p, s, err = _pipe_get(payload.get("pid"), payload.get("idx"))
            if err:
                self._json({"error": err}, 400)
                return
            try:
                c = pipe.swap_clip(p, s, int(payload.get("clip") or 0), pipe.load_cfg())
                pipe.project_save(p)
                self._json({"ok": True, "clip": c})
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 400)
            return

        if u.path == "/api/pipe/compose":
            p, s, err = _pipe_get(payload.get("pid"), payload.get("idx"))
            if err:
                self._json({"error": err}, 400)
                return
            job = new_job("compose", max(1, len((s.get("plan") or {}).get("clips") or [])))
            threading.Thread(target=_run_job, args=(job, pipe.compose_worker, p, s, pipe.load_cfg(), job),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"]})
            return

        if u.path == "/api/pipe/qc":
            names = [Path(str(x)).name for x in (payload.get("names") or []) if str(x).strip()][:200]
            if not names:
                self._json({"error": "先在成片列表里选要体检的片子"}, 400)
                return
            force = bool(payload.get("force", True))
            job = new_job("qc", len(names))
            threading.Thread(target=_run_job,
                             args=(job, qcmod.qc_worker, names, job, pipe.load_cfg(), force),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"], "count": len(names)})
            return

        if u.path == "/api/pipe/batch":
            p = pipe.project_load(payload.get("pid") or "")
            if not p:
                self._json({"error": "项目不存在"}, 404)
                return
            scripts = p.get("scripts") or []
            if not scripts:
                self._json({"error": "还没有脚本，先在第 2 步生成"}, 400)
                return
            idxs = payload.get("idxs")
            if idxs in (None, "", "all"):
                idxs = list(range(len(scripts)))
            try:
                variants = max(1, min(8, int(payload.get("variants") or 1)))
            except (TypeError, ValueError):
                variants = 1
            opts = {"variants": variants,
                    "redo_tts": bool(payload.get("redo_tts")),
                    "realign": payload.get("realign", True)}
            job = new_job("batch", max(1, len(idxs) * variants))
            threading.Thread(target=_run_job,
                             args=(job, pipe.batch_worker, p, idxs, pipe.load_cfg(), job, opts),
                             daemon=True).start()
            self._json({"ok": True, "job": job["id"], "count": len(idxs) * variants})
            return

        self.send_error(404)


def already_running(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.2) as r:
            return json.loads(r.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def main() -> None:
    port = 8777
    for argv in sys.argv[1:]:
        if argv.isdigit():
            port = int(argv)
    if not FFMPEG:
        print("[警告] 未找到 ffmpeg，导出功能将不可用")

    if already_running(port) and "--no-browser" not in sys.argv:
        url = f"http://127.0.0.1:{port}/"
        print(f"\n  检测到已在运行：{url}\n  已在浏览器中打开，本窗口可关闭\n")
        webbrowser.open(url)
        time.sleep(1.5)
        return

    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print("没有可用端口")
        return

    url = f"http://127.0.0.1:{port}/"
    print(f"\n  镜头切割器已启动：{url}\n  关闭此窗口即停止服务\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    # 后台预热在线 BGM 目录（不挡启动；首次搜索不用等几十秒）
    threading.Thread(target=_online_catalog, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
