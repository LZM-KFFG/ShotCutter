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
    cands = [ROOT / ".venv" / "Scripts" / "ffmpeg.exe", ROOT / ".venv" / "Scripts" / "ffmpeg"]
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


def _log_open(msg: str) -> None:
    try:
        with (WORK / "openfolder.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


def open_in_explorer(target: str | None) -> tuple[bool, str]:
    """在资源管理器中打开目录 / 定位文件。返回 (成功?, 实际路径或错误说明)。"""
    raw = (target or "").strip().strip('"').strip("'")
    p = Path(raw) if raw else EXPORTS
    try:
        p = p.expanduser()
        if not p.is_absolute():
            p = (ROOT / p)
        p = Path(os.path.normpath(str(p)))
    except Exception as exc:
        _log_open(f"FAIL 规范化路径出错 raw={raw!r} err={exc}")
        return False, f"路径无效：{raw}"

    if not p.exists():
        # 逐级回退到最近仍存在的父目录，比直接报错更有用
        q = p
        while q.parent != q and not q.exists():
            q = q.parent
        _log_open(f"MISS {p} -> fallback {q}")
        if not q.exists():
            return False, f"目录不存在：{p}"
        p = q

    target_s = str(p)
    is_dir = p.is_dir()
    # 目录直接进入；文件用 /select, 在父目录中选中
    cmds = []
    if is_dir:
        cmds.append(["explorer.exe", target_s])
    else:
        cmds.append(["explorer.exe", f"/select,{target_s}"])
    cmds.append(["cmd", "/c", "start", "", target_s])

    last_err = ""
    for argv in cmds:
        try:
            proc = subprocess.Popen(
                argv, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
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
            return True, target_s
        # explorer.exe 正常打开也返回 1，只有明显异常码才继续尝试下一种方式
        if rc in (0, 1):
            _log_open(f"OK   {target_s}  via={argv[0]} rc={rc} is_dir={is_dir}")
            return True, target_s
        last_err = f"{argv[0]} 退出码 {rc}"
        _log_open(f"FAIL {target_s}  via={argv[0]} rc={rc}")

    try:
        os.startfile(target_s)  # noqa: S606
        _log_open(f"OK   {target_s}  via=startfile")
        return True, target_s
    except Exception as exc:
        last_err = last_err or str(exc)
        _log_open(f"FAIL {target_s}  via=startfile err={exc}")
        return False, f"无法调起资源管理器（{last_err}）"


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


def _run_job(job: dict, fn, *args, **kwargs) -> None:
    """在线程里跑一个步骤，兜住异常，保证前端永远能收到终态。"""
    try:
        fn(*args, **kwargs)
        if job.get("state") == "running":
            job["state"] = "done"
            job["progress"] = 1.0
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


def export_one(video: dict, keep: list[int], out_dir: Path, mode: str, job: dict, base: int, total: int) -> None:
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
        job["progress"] = job["done"] / max(1, total)
        job["message"] = f"导出中 {job['done']}/{total}"


def export_worker(items: list[dict], mode: str, out_dir: str | None, job: dict) -> None:
    try:
        total = sum(len(it.get("keep", [])) for it in items)
        base = 0
        for it in items:
            video = VIDEOS.get(it.get("token", ""))
            keep = [int(i) for i in it.get("keep", []) if isinstance(i, (int, float))]
            if not video or not keep:
                continue
            target = Path(out_dir) if out_dir else Path(default_out_dir(video))
            try:
                export_one(video, keep, target, mode, job, base, total)
            except Exception as exc:  # noqa: BLE001
                job["files"].append({"path": "", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            base += len(keep)
        job["state"] = "done"
        ok = sum(1 for f in job["files"] if f["ok"])
        job["message"] = f"导出完成：{ok} 个文件"
    except Exception as exc:  # noqa: BLE001
        job["state"] = "error"
        job["error"] = str(exc)
    finally:
        job["elapsed"] = round(time.time() - job["t0"], 1)


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
  $r=$d.ShowDialog($owner)
  $owner.Close()
  if($r -eq [System.Windows.Forms.DialogResult]::OK){ Write-Output $d.SelectedPath }
  else{ Write-Output '__CANCEL__' }
}catch{
  Write-Output ('__ERROR__' + $_.Exception.Message)
}
"""


def pick_folder() -> dict:
    if not PICK_GUARD.acquire(blocking=False):
        return {"state": "busy", "path": "", "message": "已经有一个选择窗口打开了"}
    proc = None
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-STA", "-WindowStyle", "Hidden", "-Command", _PS_FOLDER],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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

    def _file(self, path: Path, ctype: str):
        if not path.exists():
            self.send_error(404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
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
        if u.path in ("/", "/index.html"):
            self._file(WEB / "index.html", "text/html; charset=utf-8")
        elif u.path == "/media":
            token = (q.get("token") or [""])[0]
            video = VIDEOS.get(token)
            if not video:
                self._json({"error": "视频不存在，请重新添加"}, 404)
                return
            src = proxy_path(video) if (q.get("proxy") or [""])[0] == "1" else Path(video["path"])
            self._stream(src)
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
        elif u.path in ("/pipeline", "/pipe.html"):
            self._file(WEB / "pipe.html", "text/html; charset=utf-8")
        elif u.path == "/api/pipe/config":
            self._json(pipe.public_cfg())
        elif u.path == "/api/pipe/desub/info":
            self._json(pipe.desub_info())
        elif u.path == "/api/pipe/angles":
            self._json({"items": [{"key": k, "label": v["label"], "hint": v.get("hint") or "",
                                   "group": v.get("group") or "classic"}
                                  for k, v in pipe.ANGLES.items()]})
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
            self._json({
                "ok": True, "items": items, "count": len(items),
                "tagged": sum(1 for i in items if i.get("tagged")),
                "semantic": sum(1 for i in items if i.get("semantic")),
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
                    items.append(it)
            except Exception:
                pass
            self._json({"ok": True, "items": items})
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
            threading.Thread(target=export_worker,
                             args=(items, payload.get("mode", "copy"), payload.get("out_dir"), job),
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
            ok, detail = open_in_explorer(payload.get("path"))
            if ok:
                self._json({"ok": True, "path": detail})
            else:
                self._json({"error": detail}, 404)
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
            self._json(pick_folder())
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
                job["message"] = "正在停止…"
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
            video = VIDEOS.get((payload.get("token") or "").strip())
            if not video:
                self._json({"error": "视频不存在，请重新添加"}, 404)
                return
            if not video.get("scenes"):
                self._json({"error": "这个视频还没检测出镜头"}, 400)
                return
            n = pipe.add_scenes_of(video)
            self._json({"ok": True, "added": n})
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
                                     list(dict.fromkeys(tags)), bool(use_llm))
                for i, s in enumerate(r["scripts"]):
                    s["idx"] = i
                    s["target"] = target
                p["scripts"] = r["scripts"]
                p["active"] = 0
                pipe.project_save(p)
                job["result"] = {"mode": r["mode"], "note": r["note"], "count": len(r["scripts"])}
                job["message"] = "生成 %d 条脚本（%s）" % (
                    len(r["scripts"]), "大模型" if r["mode"] == "llm" else "模板")

            threading.Thread(target=_run_job, args=(job, _go), daemon=True).start()
            self._json({"ok": True, "job": job["id"]})
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
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
