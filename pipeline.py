#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""成片流水线 —— 打标 → 脚本 → 配音 → 对齐 → 合成

设计原则
--------------------------------------------------------------------------------
1) 每个环节都能在「没有 API Key / 没有网络」时降级运行，不让人卡在第一步。
   - 打标：本地视觉特征（亮度/色调/细节/运动）始终算；配了视觉模型再加语义标签
   - 脚本：配了 LLM 用它写；没配用模板骨架，同样出 5 个角度
   - 配音：优先 edge-tts（带逐句时间戳），不可用时回落 Windows SAPI
2) 配音时间轴靠 edge-tts 的 SentenceBoundary 事件一次拿到，不靠估算。
3) 对齐是「按句子时长找镜头」而不是「把素材拼满」，找不到就定格，不算失败。
4) 合成用一条 filter_complex 走完，字幕用 ASS 烧录（libass）。
"""
from __future__ import annotations

import base64
import codecs
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ---------------------------------------------------------------- 基础设施

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "_work"
PIPE = WORK / "pipe"
FRAMES = PIPE / "frames"
OUT = PIPE / "out"
ZIP_DIR = PIPE / "zip"
DESUB_DIR = PIPE / "desub"
POOL_PATH = PIPE / "pool.json"
CFG_PATH = ROOT / "pipeline.json"

for _d in (PIPE, FRAMES, OUT, ZIP_DIR):
    _d.mkdir(parents=True, exist_ok=True)

FFMPEG: str | None = None
NO_WINDOW = 0
_lock = threading.Lock()

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".ts", ".m4v", ".wmv", ".mpg", ".mpeg", ".m2ts"}

# 去硬字幕用的本机工具（Video Subtitle Remover），可在 pipeline.json 的 desub.root 改
DEFAULT_VSR = r"D:\Tools\VSR"

EDGE_VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "label": "晓晓 · 女 · 亲和自然", "gender": "女"},
    {"id": "zh-CN-XiaoyiNeural", "label": "晓伊 · 女 · 年轻活泼", "gender": "女"},
    {"id": "zh-CN-YunxiNeural", "label": "云希 · 男 · 清朗", "gender": "男"},
    {"id": "zh-CN-YunjianNeural", "label": "云健 · 男 · 浑厚解说", "gender": "男"},
    {"id": "zh-CN-YunyangNeural", "label": "云扬 · 男 · 新闻播报", "gender": "男"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "label": "晓北 · 女 · 东北口音", "gender": "女"},
    {"id": "zh-CN-shaanxi-XiaoniNeural", "label": "晓妮 · 女 · 陕西口音", "gender": "女"},
    {"id": "zh-HK-HiuMaanNeural", "label": "曉曼 · 女 · 粤语", "gender": "女"},
]

DEFAULT_CFG = {
    "llm": {
        "base_url": "", "api_key": "", "model": "",
        "vision_model": "", "bypass_proxy": True,
        # 本地模型一键启动用（llama.cpp）
        # local_extra 说明：
        #   -c 32768  上下文。agent 客户端（带 system prompt + 工具定义）单次就要 9K+，
        #             8192 会直接报 exceed_context_size_error，必须给足。
        #   KV 量化 q8_0 是必需的：32K 的 f16 KV 要 3GB，16GB 卡装不下。
        #   -ub 32    必须为 32。llama.cpp 默认 512 会让 --moe-cache-mib 完全不生效
        #             （实测同一模型 512 下 15 tok/s，32 下 45 tok/s）。
        "local_dir": "", "local_model": "", "local_port": 9931,
        "local_extra": ("-c 32768 -ub 32 -cmoe --moe-cache-mib 10240 "
                        "--cache-type-k q8_0 --cache-type-v q8_0 --parallel 1"),
    },
    "tts": {"engine": "edge", "voice": "zh-CN-XiaoxiaoNeural", "rate": "+8%", "gap": 0.08},
    "video": {
        "w": 1080, "h": 1920, "fps": 30, "crf": 21, "preset": "veryfast",
        "fit": "cover", "shot": 2.2, "min_shot": 1.0, "max_shot": 4.5,
        "subtitle": True, "sub_font": "Microsoft YaHei", "sub_size": 62,
        "sub_margin": 0.17, "sub_outline": 3.2, "hook": True,
        "bgm": "", "bgm_volume": 0.10,
        "bgm_dirs": [], "bgm_mode": "one", "bgm_fade": 0.8,
    },
    # 去硬字幕：调用本机 VSR（D:\Tools\VSR）自带 Python 跑命令行模式
    "desub": {"root": DEFAULT_VSR},
}


def configure(ffmpeg: str | None, no_window: int = 0) -> None:
    global FFMPEG, NO_WINDOW
    FFMPEG = ffmpeg
    NO_WINDOW = no_window


# ---------------------------------------------------------------- 配置读写

def load_cfg() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        if CFG_PATH.exists():
            got = json.loads(CFG_PATH.read_text(encoding="utf-8"))
            for sec, val in got.items():
                if isinstance(val, dict) and isinstance(cfg.get(sec), dict):
                    cfg[sec].update(val)
                else:
                    cfg[sec] = val
    except Exception:
        pass
    return cfg


def save_cfg(new: dict) -> dict:
    cfg = load_cfg()
    for sec, val in (new or {}).items():
        if sec not in cfg:
            continue
        if isinstance(val, dict) and isinstance(cfg[sec], dict):
            for k, v in val.items():
                if k == "api_key" and v == "__KEEP__":
                    continue
                cfg[sec][k] = v
        else:
            cfg[sec] = val
    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def public_cfg(cfg: dict | None = None) -> dict:
    cfg = json.loads(json.dumps(cfg or load_cfg()))
    key = cfg["llm"].get("api_key") or ""
    ok, why = llm_ready(cfg)
    url, _, _ = _llm_endpoint(cfg)
    cfg["llm"]["ready"] = ok
    cfg["llm"]["ready_why"] = why
    cfg["llm"]["local"] = is_local_url(url)
    cfg["llm"]["api_key_set"] = bool(key)
    if cfg["llm"]["local"] and key in ("", "sk-local"):
        cfg["llm"]["api_key"] = ""      # 本地端点不需要 Key，别显示一堆星号让人误会
    else:
        cfg["llm"]["api_key"] = ("*" * 8 + key[-4:]) if len(key) > 8 else ("*" * len(key))
    cfg["voices"] = EDGE_VOICES
    cfg["has_edge"] = _has_edge()
    return cfg


def _has_edge() -> bool:
    try:
        import edge_tts  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 进程工具

def run(args: list[str], timeout: int = 300, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    if not FFMPEG:
        raise RuntimeError("未找到 ffmpeg")
    return subprocess.run([FFMPEG] + [str(a) for a in args], capture_output=True, text=True,
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW,
                          cwd=str(cwd) if cwd else None)


def media_duration(path: str | Path) -> float:
    try:
        r = run(["-hide_banner", "-i", str(path)], timeout=60)
        txt = (r.stderr or "") + (r.stdout or "")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    return 0.0


def _kbps(size: int, dur: float) -> int:
    """平均码率（视频+音频）：体积 / 时长。视频已渲染完，只做除法，不额外探流。"""
    try:
        if size and dur and dur > 0.05:
            return int(round(size * 8 / dur / 1000.0))
    except Exception:
        pass
    return 0


def media_size(path: str | Path) -> tuple[int, int]:
    try:
        r = run(["-hide_banner", "-i", str(path)], timeout=60)
        txt = (r.stderr or "") + (r.stdout or "")
        m = re.search(r"Video:.*?,\s*(\d{2,5})x(\d{2,5})", txt)
        if m:
            return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 0, 0


def has_audio(path: str | Path) -> bool:
    try:
        r = run(["-hide_banner", "-i", str(path)], timeout=60)
        return "Audio:" in ((r.stderr or "") + (r.stdout or ""))
    except Exception:
        return False


# ---------------------------------------------------------------- 去硬字幕（VSR）

DESUB_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".m4v", ".wmv", ".ts"}


def desub_root(cfg: dict | None = None) -> Path:
    cfg = cfg or load_cfg()
    raw = str((cfg.get("desub") or {}).get("root") or DEFAULT_VSR).strip().strip('"').strip("'")
    return Path(raw)


def desub_info(cfg: dict | None = None) -> dict:
    """探测 VSR：自带 Python + resources/run_with_area.py 是否都在，顺带读它的算法与检测开关。"""
    root = desub_root(cfg)
    py = root / "Python" / "python.exe"
    entry = root / "resources" / "run_with_area.py"
    mode, detect = "", None
    try:
        txt = (root / "resources" / "backend" / "config.py").read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^MODE\s*=\s*InpaintMode\.(\w+)", txt, re.M)
        mode = m.group(1) if m else ""
        m2 = re.search(r"^STTN_SKIP_DETECTION\s*=\s*(\w+)", txt, re.M)
        detect = (m2.group(1) == "False") if m2 else None
    except Exception:
        pass
    return {
        "ok": py.exists() and entry.exists(),
        "root": str(root), "python": str(py), "entry": str(entry),
        "mode": mode, "detect": detect,
        "out_dir": str(PIPE / "desub"),
    }


def _is_desubbed(p: Path) -> bool:
    return "_no_sub" in p.stem.lower()


def desub_collect(folders: list | None = None, ids: list | None = None) -> list[str]:
    """收集要去字幕的源片：选中的素材 + 文件夹里递归扫到的视频，都已处理过的跳过。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(p: Path) -> None:
        try:
            rp = str(p.resolve())
        except Exception:
            rp = str(p)
        if rp in seen or not p.is_file() or _is_desubbed(p):
            return
        seen.add(rp)
        out.append(rp)

    if ids:
        want = set(ids)
        for it in pool_list():
            if it.get("id") in want:
                add(Path(it["path"]))
    for d in folders or []:
        raw = str(d).strip().strip('"').strip("'")
        root = Path(raw) if raw else None
        if not root or not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() in DESUB_EXTS:
                add(f)
    return out


def _desub_index_path() -> Path:
    return PIPE / "desub" / "index.json"


def _desub_index() -> dict:
    try:
        return json.loads(_desub_index_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _desub_save_index(idx: dict) -> None:
    try:
        _desub_index_path().parent.mkdir(parents=True, exist_ok=True)
        _desub_index_path().write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def desub_dst(src: Path, out_dir: Path, index: dict) -> Path:
    """给源文件定一个稳定的输出名：同名冲突时拼上级目录，再冲突加序号；映射存 index.json，
    这样同一个文件重跑还能认出自己，不会越跑越多。"""
    key = str(src)
    got = index.get(key)
    if got and (out_dir / got).exists():
        return out_dir / got
    base = src.stem + "_no_sub"
    used = {v for k, v in index.items() if k != key}
    cands = [out_dir / (base + ".mp4"), out_dir / ("%s_%s.mp4" % (src.parent.name, base))]
    for i in range(2, 50):
        cands.append(out_dir / ("%s_%s_%d.mp4" % (src.parent.name, base, i)))
    for c in cands:
        if not c.exists() and c.name not in used:
            index[key] = c.name
            _desub_save_index(index)
            return c
    index[key] = cands[0].name
    return cands[0]


def _desub_one(src: Path, dst: Path, py: Path, cwd: Path, area, job: dict,
               idx: int, total: int, detect: bool = True) -> Path:
    """跑一个文件：VSR 只会写到源目录的 <名字>_no_sub.mp4，跑完再搬进 _work/pipe/desub。"""
    arg = "none" if not area else "%d,%d,%d,%d" % tuple(area)
    env = dict(os.environ)
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
                "QPT_MODE": "Running", "QPT_COLOR": "False",
                "VSR_ROOT": str(cwd.parent)})
    proc = subprocess.Popen([str(py), str(ROOT / "vsr_runner.py"), str(src), arg, "1" if detect else "0"],
                            cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, creationflags=NO_WINDOW)
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    tail = ""
    try:
        while True:
            if job.get("cancel"):
                try:
                    proc.kill()
                except Exception:
                    pass
                raise JobCancelled()
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            text = dec.decode(chunk)
            got = re.findall(r"(\d{1,3})%\|", text)
            if got:
                pct = min(100, int(got[-1]))
                job["progress"] = (idx + pct / 100.0) / max(1, total)
                job["message"] = "[%d/%d] %s  %d%%" % (idx + 1, total, src.name, pct)
            tail = re.sub(r"\s+", " ", (tail + " " + text))[-400:]
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    code = proc.wait()
    if code != 0:
        raise RuntimeError("VSR 退出码 %d：%s" % (code, tail[-140:] or "无输出"))
    made = src.with_name(src.stem + "_no_sub" + src.suffix)
    if not made.exists():
        made = src.with_name(src.stem + "_no_sub.mp4")
    if not made.exists():
        raise RuntimeError("没等到输出文件（源目录里没有 %s_no_sub.mp4）" % src.stem)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if made.resolve() != dst.resolve():
        if dst.exists():
            dst.unlink(missing_ok=True)
        shutil.move(str(made), str(dst))
    return dst


def desub_list() -> list[dict]:
    """已经去完字幕的文件（按时间新→旧）。"""
    out: list[dict] = []
    try:
        for p in sorted(DESUB_DIR.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "size": st.st_size, "mtime": st.st_mtime})
    except Exception:
        pass
    return out


def desub_add(names: list | None = None) -> dict:
    """把去字幕产物加入素材池（已有的跳过）。名字为空就加全部。"""
    targets = ([DESUB_DIR / Path(str(n)).name for n in names] if names
               else sorted(DESUB_DIR.glob("*.mp4")))
    pool = _pool_load()
    added = skipped = bad = 0
    for f in targets:
        if not f.is_file():
            bad += 1
            continue
        try:
            d = media_duration(f)
        except Exception:
            d = 0.0
        if d <= 0.05:
            bad += 1
            continue
        pid = _clip_id(str(f.resolve()), 0.0, d)
        if pid in pool["items"]:
            skipped += 1
            continue
        pool["items"][pid] = {
            "id": pid, "path": str(f.resolve()), "name": f.name,
            "start": 0.0, "end": round(d, 3), "dur": round(d, 3),
            "src_kind": "desub", "video_token": "", "index": 1,
            "tags": [], "feats": {}, "tagged": False, "text": "",
            "added": time.time(),
        }
        added += 1
    if added:
        _pool_save(pool)
    return {"added": added, "skipped": skipped, "bad": bad, "total": len(targets)}


def desub_delete(names: list | None = None) -> dict:
    """删除去字幕产物：进回收站，同时从素材池和 index 里摘掉。"""
    real: list[Path] = []
    seen = set()
    for n in names or []:
        p = DESUB_DIR / Path(str(n)).name
        if p.suffix.lower() != ".mp4" or not p.is_file() or p.name in seen:
            continue
        seen.add(p.name)
        real.append(p)
    if not real:
        raise RuntimeError("没有可删除的文件")

    want = set()
    for p in real:
        try:
            want.add(str(p.resolve()))
        except OSError:
            pass

    res = trash([str(p) for p in real])
    failed = {str(Path(x).name) for x in (res.get("failed") or [])}
    deleted = [p.name for p in real if p.name not in failed]

    pool_removed = 0
    if deleted:
        pool = _pool_load()
        drop = []
        for pid, it in pool["items"].items():
            try:
                if str(Path(it.get("path") or "").resolve()) in want:
                    drop.append(pid)
            except OSError:
                continue
        for pid in drop:
            pool["items"].pop(pid, None)
        if drop:
            _pool_save(pool)
            pool_removed = len(drop)

        idx = _desub_index()
        keep = {k: v for k, v in idx.items() if v not in set(deleted)}
        if len(keep) != len(idx):
            _desub_save_index(keep)

    return {"ok": True, "deleted": len(deleted), "failed": sorted(failed),
            "pool_removed": pool_removed}


def desub_zip(names: list | None = None, max_count: int = 200) -> dict:
    """把去字幕的视频打成一个 zip 方便下载复用。视频已压缩，用 STORED 直接拼。"""
    if names:
        files = [DESUB_DIR / Path(str(n)).name for n in names]
    else:
        files = sorted(DESUB_DIR.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True)
    real = [f for f in files if f.is_file() and f.suffix.lower() == ".mp4"]
    if not real:
        raise RuntimeError("还没有去字幕的视频")
    if len(real) > max_count:
        raise RuntimeError("一次最多打包 %d 个，先挑一部分吧" % max_count)
    total = sum(f.stat().st_size for f in real if f.exists())
    name = "去字幕_%s_%d条.zip" % (time.strftime("%m%d_%H%M%S"), len(real))
    target = ZIP_DIR / name
    tmp = target.with_suffix(".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for f in real:
                z.write(f, arcname=f.name)
        tmp.replace(target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    _out_zip_prune()
    return {"ok": True, "name": name, "count": len(real), "size": total,
            "zip_size": target.stat().st_size,
            "url": "/api/pipe/zip?name=" + urllib.parse.quote(name)}


def desub_worker(videos, cfg: dict, job: dict, opts: dict | None = None) -> dict:
    """批量去字幕。opts: mode=auto|box, box=[上,下,左,右] 百分比, add_pool, force。"""
    opts = opts or {}
    info = desub_info(cfg)
    if not info["ok"]:
        raise RuntimeError("没找到 VSR：%s（检查 pipeline.json 里的 desub.root）" % info["root"])
    py, cwd = Path(info["python"]), Path(info["entry"]).parent
    out_dir = DESUB_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    mode = str(opts.get("mode") or "auto").lower()
    try:
        box = [float(x) for x in (opts.get("box") or [72, 96, 2, 98])[:4]]
    except Exception:
        box = [72, 96, 2, 98]
    add_pool = bool(opts.get("add_pool", True))
    force = bool(opts.get("force"))

    total = len(videos)
    made = skipped = 0
    failed: list[dict] = []
    skipped_names: list[str] = []      # 已经存在、被跳过的也要补进素材池
    index = _desub_index()
    base_detect = mode != "box"
    for i, raw in enumerate(videos):
        try:
            job_gate(job)
        except JobCancelled:
            job["message"] = "已停止：新生成 %d，跳过 %d，失败 %d" % (made, skipped, len(failed))
            job["state"] = "cancel"
            return {"made": made, "skipped": skipped, "failed": failed, "cancelled": True}
        src = Path(raw)
        dst = desub_dst(src, out_dir, index)
        job["message"] = "[%d/%d] %s" % (i + 1, total, src.name)
        if dst.exists() and not force:
            skipped += 1
            skipped_names.append(dst.name)
            job["done"] = i + 1
            job["progress"] = (i + 1) / max(1, total)
            continue
        area, det = None, base_detect
        if mode == "box":
            w, h = media_size(src)
            if w and h:
                area = (max(0, int(h * box[0] / 100)), min(h, int(h * box[1] / 100)),
                        max(0, int(w * box[2] / 100)), min(w, int(w * box[3] / 100)))
                if area[1] <= area[0] + 2 or area[3] <= area[2] + 2:
                    area = None          # 比例填反了就退回自动检测，别硬画一条线
        if area is None:
            det = True                   # 没有可用区域就只能走逐帧检测
        try:
            got = _desub_one(src, dst, py, cwd, area, job, i, total, det)
            made += 1
            if add_pool:
                d = media_duration(got)
                if d > 0.05:
                    add_clip(str(got), 0.0, d, name=got.name, src_kind="desub")
            job["results"].append({"src": str(src), "out": str(got), "ok": True})
        except JobCancelled:
            # 停止时正在跑的那一个不算失败，也不留下半成品
            try:
                src.with_name(src.stem + "_no_sub" + src.suffix).unlink(missing_ok=True)
            except Exception:
                pass
            if add_pool and skipped_names:
                desub_add(skipped_names)
            job["message"] = "已停止：新生成 %d，跳过 %d" % (made, skipped)
            job["state"] = "cancel"
            return {"made": made, "skipped": skipped, "failed": failed, "cancelled": True}
        except Exception as exc:  # noqa: BLE001
            failed.append({"src": str(src), "error": str(exc)})
            job["results"].append({"src": str(src), "ok": False, "error": str(exc)})
        job["done"] = i + 1
        job["progress"] = (i + 1) / max(1, total)

    added_skip = 0
    if add_pool and skipped_names:
        added_skip = desub_add(skipped_names).get("added", 0)
    job["message"] = "去字幕完成：新生成 %d，跳过 %d，失败 %d%s" % (
        made, skipped, len(failed), ("，补入素材池 %d 个" % added_skip) if added_skip else "")
    if failed and not made and not skipped:
        raise RuntimeError("全部失败：" + str(failed[0].get("error") or "")[:140])
    return {"made": made, "skipped": skipped, "failed": failed}


def _short(text: str, n: int = 220) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip()
    return text if len(text) <= n else text[:n] + "…"


# ---------------------------------------------------------------- 素材池

def _pool_load() -> dict:
    """读素材库。读失败时先重试、再退备份，绝不静默返回空池——
    否则调用方一保存就会把空池写回去，素材全丢。"""
    last = None
    for _ in range(3):
        try:
            if not POOL_PATH.exists():
                break
            txt = POOL_PATH.read_text(encoding="utf-8")
            d = json.loads(txt)
            if isinstance(d, dict) and isinstance(d.get("items"), dict):
                return d
            last = "格式不对"
        except FileNotFoundError:
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.06)
    bak = POOL_PATH.with_suffix(".bak")
    try:
        if bak.exists():
            d = json.loads(bak.read_text(encoding="utf-8"))
            if isinstance(d, dict) and isinstance(d.get("items"), dict) and d["items"]:
                return d
    except Exception:
        pass
    if last:
        try:
            POOL_PATH.with_suffix(".bad").write_text(
                POOL_PATH.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        except Exception:
            pass
    return {"items": {}}


def _pool_save(pool: dict) -> None:
    with _lock:
        tmp = POOL_PATH.with_name("pool.%d.%d.tmp" % (os.getpid(), threading.get_ident()))
        tmp.write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")
        try:
            if POOL_PATH.exists() and POOL_PATH.stat().st_size > 4:
                shutil.copy2(POOL_PATH, POOL_PATH.with_suffix(".bak"))
        except Exception:
            pass
        os.replace(tmp, POOL_PATH)


def _clip_id(path: str, start: float, end: float) -> str:
    raw = f"{Path(path).resolve()}|{start:.3f}|{end:.3f}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def add_clip(path: str, start: float, end: float, name: str = "", src_kind: str = "file",
             video_token: str = "", index: int = 1) -> dict:
    path = str(Path(path).resolve())
    pid = _clip_id(path, start, end)
    pool = _pool_load()
    old = pool["items"].get(pid)
    if old:
        old["name"] = name or old.get("name")
        old["src_kind"] = src_kind or old.get("src_kind")
        return old
    dur = max(0.0, round(end - start, 3))
    item = {
        "id": pid, "path": str(path), "name": name or Path(path).name,
        "start": round(start, 3), "end": round(end, 3), "dur": dur,
        "src_kind": src_kind, "video_token": video_token, "index": index,
        "tags": [], "feats": {}, "tagged": False, "text": "",
        "added": time.time(),
    }
    pool["items"][pid] = item
    _pool_save(pool)
    return item


def collect_videos(folders: list, recursive: bool = True) -> list:
    """把多个文件夹里的视频收集成一个去重列表（同一个文件出现在两个目录里只算一次）。"""
    seen, out = set(), []
    for f in folders:
        root = Path(f)
        if not root.exists() or not root.is_dir():
            continue
        it = root.rglob("*") if recursive else root.glob("*")
        for p in it:
            try:
                if not (p.is_file() and p.suffix.lower() in VIDEO_EXT):
                    continue
                key = str(p.resolve()).lower()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
    out.sort(key=lambda p: (str(p.parent).lower(), p.name.lower()))
    return out


def scan_folders(folders: list, job: dict | None = None, recursive: bool = True) -> dict:
    """一次扫多个文件夹。不存在的目录跳过并记进 bad，全部不存在才报错。"""
    dirs = []
    for f in folders or []:
        f = str(f).strip().strip('"').strip("'").rstrip("\\/")
        if f and f not in dirs:
            dirs.append(f)
    if not dirs:
        raise RuntimeError("请填素材文件夹路径")
    good = [d for d in dirs if Path(d).is_dir()]
    bad = [d for d in dirs if d not in good]
    if not good:
        raise RuntimeError("这些目录都不存在：%s" % "；".join(bad[:3]))

    files = collect_videos(good, recursive)
    if job:
        job["total"] = len(files)
        job["message"] = "%d 个目录，共 %d 个视频，开始加入…" % (len(good), len(files))

    added, skipped, existed = 0, 0, 0
    have = set(_pool_load()["items"].keys())
    for i, p in enumerate(files):
        if job:
            job_gate(job)
        try:
            d = media_duration(p)
        except Exception:
            d = 0.0
        if d <= 0.02:
            skipped += 1
            continue
        pid = _clip_id(str(p.resolve()), 0.0, d)
        add_clip(str(p), 0.0, d, name=p.name, src_kind="folder")
        if pid in have:
            existed += 1
        else:
            have.add(pid)
            added += 1
        if job:
            job["done"] = i + 1
            job["progress"] = (i + 1) / max(1, len(files))
            job["message"] = "%d/%d  %s" % (i + 1, len(files), p.name[:28])
    return {"added": added, "existed": existed, "total": len(files), "skipped": skipped,
            "folders": good, "bad": bad}


def scan_folder(folder: str, job: dict | None = None, recursive: bool = True) -> dict:
    return scan_folders([folder], job, recursive)


def add_scenes_of(video: dict) -> int:
    """把一个已检测视频的每个镜头作为素材加入池。"""
    n = 0
    for sc in video.get("scenes") or []:
        add_clip(video["path"], float(sc["start"]), float(sc["end"]),
                 name="%s#%02d" % (video["name"], sc["i"]), src_kind="scene",
                 video_token=video["token"], index=int(sc["i"]))
        n += 1
    return n


def pool_list(only_tagged: bool = False) -> list[dict]:
    pool = _pool_load()
    items = []
    for it in pool["items"].values():
        if not Path(it["path"]).exists():
            continue
        if only_tagged and not it.get("tagged"):
            continue
        items.append(it)
    items.sort(key=lambda x: (x["path"], x["start"]))
    return items


def pool_clear(untagged_only: bool = False) -> int:
    pool = _pool_load()
    keep = {}
    for k, v in pool["items"].items():
        if untagged_only and v.get("tagged"):
            keep[k] = v
    n = len(pool["items"]) - len(keep)
    pool["items"] = keep
    _pool_save(pool)
    return n


def pool_drop(pid: str) -> bool:
    pool = _pool_load()
    if pid in pool["items"]:
        del pool["items"][pid]
        _pool_save(pool)
        return True
    return False


def pool_drop_many(ids) -> int:
    """批量移出素材池，返回真正删掉的条数。"""
    pool = _pool_load()
    n = 0
    for i in ids or []:
        if i in pool["items"]:
            del pool["items"][i]
            n += 1
    if n:
        _pool_save(pool)
    return n


def pool_restore(items: list) -> int:
    """把移出素材池的记录放回去（撤销误删 / 去掉重）。已有的不覆盖，文件不在的跳过。"""
    pool = _pool_load()
    n = 0
    for it in items or []:
        if not isinstance(it, dict):
            continue
        pid = it.get("id")
        if not pid or pid in pool["items"] or not Path(it.get("path") or "").exists():
            continue
        pool["items"][pid] = it
        n += 1
    if n:
        _pool_save(pool)
    return n


def pool_health(items: list[dict] | None = None) -> dict:
    """素材体检：看景别和画面大类够不够撑一条片子。

    只统计、不改数据，结论放在 advice 里给前端显示。
    景别缺口比"标签准不准"更致命：190 条里 179 条特写，怎么对齐都是一路怼脸，
    这不是打分算法能救的，得靠补拍。
    """
    items = pool_list() if items is None else items
    total = len(items)
    out = {"total": total, "tagged": 0, "semantic": 0, "avg_dur": 0.0,
           "shot": [], "top": [], "advice": []}
    if not total:
        out["advice"].append({"level": "tip", "text": "素材池是空的，先扫描一个文件夹加素材。"})
        return out

    out["tagged"] = sum(1 for i in items if i.get("tagged"))
    out["semantic"] = sum(1 for i in items if i.get("semantic"))
    out["avg_dur"] = round(sum(float(i.get("dur") or 0) for i in items) / total, 1)

    shot_count = {w: 0 for w in SHOT_WORDS}
    shot_count["未标景别"] = 0
    for it in items:
        tags = set(it.get("tags") or [])
        hit = next((w for w in SHOT_WORDS if w in tags), "")
        shot_count[hit or "未标景别"] += 1
    out["shot"] = [{"k": k, "n": n, "pct": round(n * 100 / total, 1)}
                   for k, n in shot_count.items()]

    cat: dict[str, int] = {}
    for it in items:
        for t in _scene_tags(it):
            cat[t] = cat.get(t, 0) + 1
    out["top"] = [{"k": k, "n": n, "pct": round(n * 100 / total)}
                  for k, n in sorted(cat.items(), key=lambda x: -x[1])[:6]]

    pct = {r["k"]: r["pct"] for r in out["shot"]}
    spec = pct.get("特写", 0.0)
    wide = pct.get("全景", 0.0) + pct.get("远景", 0.0)
    mid = pct.get("中景", 0.0) + pct.get("近景", 0.0)
    adv = out["advice"]

    if spec > 65 and total >= 15:
        adv.append({"level": "warn",
                    "text": "特写占 %d%%，成片容易一路怼脸、节奏单调，建议补些中景和全景。" % spec})
    if wide < 8 and total >= 15:
        adv.append({"level": "bad",
                    "text": "全景/远景只有 %d 条（%d%%）——口播讲到场景、摆盘、整体效果时会挑不到画面，"
                            "只能拿特写凑。" % (shot_count["全景"] + shot_count["远景"], wide)})
    if mid < 12 and total >= 15:
        adv.append({"level": "tip",
                    "text": "中景只有 %d 条（%d%%），镜头之间缺过渡，容易特写硬切特写。"
                            % (shot_count["中景"] + shot_count["近景"], mid)})
    if out["top"] and out["top"][0]["pct"] >= 60:
        adv.append({"level": "tip",
                    "text": "画面大类集中在「%s」（%d%%），成片看着会有点单一。"
                            % (out["top"][0]["k"], out["top"][0]["pct"])})
    if out["tagged"] < total:
        adv.append({"level": "tip",
                    "text": "还有 %d 条没打标，对齐时只能按时长凑，可能挑到不相关的画面。"
                            % (total - out["tagged"])})
    if not adv:
        adv.append({"level": "ok", "text": "景别和画面大类都够用，可以直接出片。"})
    return out


# ---------------------------------------------------------------- 素材去重

DEDUP_BITS = 16        # 结构指纹分辨率：16×16 的横向梯度符号 → 256 位
DEDUP_NEAR = 12        # 允许 12/256 位不同（≈5%）；再松就会把不同镜头误判成同一条
DEDUP_COLOR = 32.0     # 平均色差（0~255）。纯色/黑场靠结构位分不开，用颜色兜一道
DEDUP_OVERLAP = 0.85   # 同一视频里，短区间被长区间盖住超过这个比例就算重复


def fp_from_frames(paths: list[Path]) -> dict | None:
    """由若干帧图算画面指纹：每帧 16×16 梯度符号（256 位）+ 平均 BGR。

    16×16 的梯度比 8×8 稳：同一段画面转码、轻微调色后仍然对得上，
    而不同镜头（哪怕都是"厨房特写"）基本不会撞。
    镜头切割器导出前的去重也复用它，保证两边判重口径一致。
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return None
    hs, cs = [], []
    for p in paths:
        im = cv2.imread(str(p))
        if im is None:
            continue
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (DEDUP_BITS + 1, DEDUP_BITS), interpolation=cv2.INTER_AREA)
        bits = np.packbits((g[:, 1:] > g[:, :-1]).reshape(-1))
        hs.append(bits.tobytes().hex())
        cs.append([int(x) for x in cv2.mean(im)[:3]])
    if not hs:
        return None
    return {"v": 1, "h": hs, "c": cs}


def _dedup_fp(item: dict) -> dict | None:
    """算一个素材的画面指纹；指纹回存到素材记录里，重跑去重不用再抽帧。"""
    return fp_from_frames(grab_frames(item, 2, width=160, prefix="h"))


def fp_dist(a: dict, b: dict) -> tuple[int, float]:
    """公开版指纹距离，给 app 侧导出前去重用。"""
    return _fp_dist(a, b)


def _fp_dist(a: dict, b: dict) -> tuple[int, float]:
    """两张指纹的差异：(结构位不同的个数, 平均色差)。两帧取平均，
    单帧对不上（片段内部有变化）不至于直接判成两个镜头。"""
    n = min(len(a["h"]), len(b["h"]))
    if n <= 0:
        return 999, 999.0
    ham = sum(bin(int(a["h"][i], 16) ^ int(b["h"][i], 16)).count("1") for i in range(n)) / n
    col = sum(sum(abs(x - y) for x, y in zip(a["c"][i], b["c"][i])) / 3.0
              for i in range(min(len(a["c"]), len(b["c"])))) / max(1, min(len(a["c"]), len(b["c"])))
    return int(round(ham)), round(col, 1)


def _keep_rank(it: dict) -> tuple:
    """一组重复里留哪条：已打标且有语义标签 > 已打标 > 更长 > 更早入库。"""
    return (1 if it.get("tagged") else 0,
            1 if _scene_tags(it) else 0,
            round(float(it.get("dur") or 0), 1),
            -float(it.get("added") or 0))


def _brief(it: dict) -> dict:
    return {"id": it["id"], "name": it.get("name") or Path(it["path"]).name,
            "dur": round(float(it.get("dur") or 0), 1),
            "start": round(float(it.get("start") or 0), 1),
            "file": Path(it["path"]).name,
            "tags": [t for t in (it.get("tags") or []) if t not in CV_LABELS][:4]}


def pool_dedup(items: list[dict] | None = None, job: dict | None = None) -> dict:
    """找重复素材，只出报告不改数据；真正移出由前端的「移出」按钮调 pool_drop_many。

    三类重复：
      1) 同源重叠——同一个视频的切片区间互相盖住（手动切片 / 去字幕复用最多）
      2) 重复文件——同一段素材在库里存了两份（不同路径、画面完全一致）
      3) 画面近似——重拍或近似镜头，结构指纹够近就算一组
    """
    pool = _pool_load()
    items = [it for it in (items if items is not None else pool["items"].values())
             if Path(it.get("path") or "").exists()]
    total = len(items)
    out = {"checked": total, "groups": [], "dup_count": 0, "saved": 0.0, "no_fp": 0}
    if total < 2:
        return out

    # 1) 指纹（已有缓存直接用）
    dirty = False
    for i, it in enumerate(items):
        if job:
            job_gate(job)
            job["done"] = i + 1
            job["progress"] = 0.85 * (i + 1) / total
            job["message"] = "算画面指纹 %d/%d  %s" % (i + 1, total, (it.get("name") or "")[:22])
        fp = it.get("fp")
        if not (isinstance(fp, dict) and fp.get("v") == 1 and fp.get("h")):
            fp = _dedup_fp(it)
            if fp:
                it["fp"] = fp
                dirty = True
            elif "fp" in it:
                it.pop("fp", None)
                dirty = True
        if not fp:
            out["no_fp"] += 1

    # 2) 先按「同一文件的时间区间重叠」分组，剩下的再按画面比
    groups: list[dict] = []          # {"keep": item, "dupes": [item], "reason": str, "dist": int}
    taken: set[str] = set()
    ordered = sorted(items, key=_keep_rank, reverse=True)

    by_path: dict[str, list[dict]] = {}
    for it in ordered:
        try:
            by_path.setdefault(str(Path(it["path"]).resolve()), []).append(it)
        except OSError:
            continue
    for same in by_path.values():
        if len(same) < 2:
            continue
        for idx, keep in enumerate(same):
            if keep["id"] in taken:
                continue
            for other in same[idx + 1:]:
                if other["id"] in taken or other["id"] == keep["id"]:
                    continue
                ov = min(float(keep["end"]), float(other["end"])) - max(float(keep["start"]), float(other["start"]))
                short = min(float(keep.get("dur") or 0), float(other.get("dur") or 0)) or 0.01
                if ov > 0.15 and ov >= DEDUP_OVERLAP * short:
                    taken.add(other["id"])
                    g = next((x for x in groups if x["keep"] is keep), None)
                    if g is None:
                        g = {"keep": keep, "dupes": [], "reason": "同源重叠", "dist": 0}
                        groups.append(g)
                    g["dupes"].append(other)

    kept_fp: list[dict] = []
    for it in ordered:
        if it["id"] in taken or not it.get("fp"):
            continue
        hit = None
        for k in kept_fp:
            if k["keep"]["id"] in taken:
                continue
            ham, col = _fp_dist(it["fp"], k["keep"]["fp"])
            ok = ham <= (DEDUP_NEAR + 4 if len(it["fp"]["h"]) < 2 or len(k["keep"]["fp"]["h"]) < 2 else DEDUP_NEAR)
            if ok and col <= DEDUP_COLOR:
                hit = (k, ham, col)
                break
        if hit:
            k, ham, col = hit
            taken.add(it["id"])
            same_file = (str(Path(it["path"]).resolve()) == str(Path(k["keep"]["path"]).resolve()))
            k["dupes"].append(it)
            if ham == 0 and not same_file:
                k["reason"] = "重复文件"
            elif not k.get("reason"):
                k["reason"] = "画面近似"
            k["dist"] = min(k.get("dist") or 99, ham)
            k["color"] = min(k.get("color") or 99, col)
        else:
            kept_fp.append({"keep": it, "dupes": [], "reason": "", "dist": 0})

    # 3) 整理输出（同源重叠的组和画面近似的组合在一起）
    for g in groups + kept_fp:
        if not g["dupes"]:
            continue
        if not g.get("reason"):
            g["reason"] = "画面近似"
        out["groups"].append({
            "reason": g["reason"], "dist": g.get("dist") or 0, "color": g.get("color") or 0,
            "keep": _brief(g["keep"]),
            "dupes": [_brief(x) for x in g["dupes"]],
        })
        out["dup_count"] += len(g["dupes"])
        out["saved"] = round(out["saved"] + sum(float(x.get("dur") or 0) for x in g["dupes"]), 1)
    out["groups"].sort(key=lambda g: -len(g["dupes"]))
    out["saved"] = round(out["saved"], 1)

    if dirty:
        _pool_save(pool)
    return out


def dedup_worker(items: list[dict], job: dict) -> None:
    """去重任务体：结果挂在 job["dedup"] 上给前端画报告。"""
    job["message"] = "开始对比 %d 条素材…" % len(items)
    res = pool_dedup(items, job)
    job["dedup"] = res
    n = res.get("dup_count") or 0
    job["progress"] = 1.0
    if res.get("no_fp") and res["no_fp"] >= len(items):
        job["message"] = "抽帧失败了，画面没法比——检查 ffmpeg 是否可用"
    elif n:
        job["message"] = "找到 %d 条重复（可省 %.1fs 时长）" % (n, res.get("saved") or 0)
    else:
        job["message"] = "没有发现重复素材"


# ---------------------------------------------------------------- 删除（走回收站）

def _trash_win(paths: list[str]) -> bool:
    """调 Shell 的 SHFileOperation 把文件/目录丢进回收站。失败返回 False。"""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
    buf = ctypes.create_unicode_buffer("\0".join(paths) + "\0\0", len("\0".join(paths)) + 2)
    op = SHFILEOPSTRUCTW(None, FO_DELETE, ctypes.cast(buf, wintypes.LPCWSTR), None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI,
                         False, None, None)
    fn = ctypes.windll.shell32.SHFileOperationW
    fn.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    fn.restype = ctypes.c_int
    rc = fn(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted


def trash(paths: list) -> dict:
    """把一批文件/目录移到回收站。逐个删 + 失败重试，避免一条失败就连累整批。"""
    real, seen = [], set()
    for p in paths or []:
        if not p:
            continue
        q = Path(str(p))
        try:
            if not q.exists():
                continue
            key = str(q.resolve()).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        real.append(q)
    if not real:
        return {"ok": True, "count": 0, "size": 0, "paths": [], "failed": []}
    if os.name != "nt":
        raise RuntimeError("回收站只支持 Windows")

    size = 0
    for q in real:
        try:
            if q.is_file():
                size += q.stat().st_size
            else:
                size += sum(f.stat().st_size for f in q.rglob("*") if f.is_file())
        except Exception:
            pass

    done, failed = [], []
    for q in real:
        ok = False
        for _ in range(3):            # 刚渲染完的文件偶尔还被句柄占着，等一下再试
            try:
                if _trash_win([str(q)]):
                    ok = True
                    break
            except Exception:
                pass
            time.sleep(0.25)
        (done if ok else failed).append(str(q))
    return {"ok": not failed, "count": len(done), "size": size,
            "paths": done, "failed": failed}


def _out_zip_prune(keep: int = 8, hours: float = 6.0) -> None:
    """清掉旧的打包文件，只留最近几个，别把磁盘堆满。"""
    try:
        now = time.time()
        files = sorted(ZIP_DIR.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for i, p in enumerate(files):
            try:
                if i >= keep or now - p.stat().st_mtime > hours * 3600:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def out_zip(names: list, max_count: int = 300) -> dict:
    """把选中的成片打成一个 zip。视频本身已压缩，用 STORED 直接拼，不费 CPU。"""
    real: list[Path] = []
    seen = set()
    for n in names or []:
        p = OUT / Path(str(n)).name          # 只认文件名，挡掉 ../ 之类的穿越
        if p.suffix.lower() != ".mp4" or not p.is_file():
            continue
        key = p.name.lower()
        if key in seen:
            continue
        seen.add(key)
        real.append(p)
    if not real:
        raise RuntimeError("没有可打包的成片（可能已经被删掉了）")
    if len(real) > max_count:
        raise RuntimeError("一次最多打包 %d 条，分两批下载吧" % max_count)
    total = 0
    for p in real:
        try:
            total += p.stat().st_size
        except Exception:
            pass
    name = "成片_%s_%d条.zip" % (time.strftime("%m%d_%H%M%S"), len(real))
    target = ZIP_DIR / name
    tmp = target.with_suffix(".part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
            for p in real:
                z.write(p, arcname=p.name)     # 包内保持原文件名，解压不会乱
        tmp.replace(target)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    _out_zip_prune()
    return {"ok": True, "name": name, "count": len(real), "size": total,
            "zip_size": target.stat().st_size,
            "url": "/api/pipe/zip?name=" + urllib.parse.quote(name)}


def out_delete(name: str) -> dict:
    """删一条成片：mp4 + 同名 json + 它的分镜表/字幕/配音；项目没人用了就一起清掉。"""
    name = Path(str(name or "")).name
    if not name.lower().endswith(".mp4"):
        raise RuntimeError("只接受成片文件名")
    f = OUT / name
    if not f.exists():
        raise RuntimeError("成片已经不在了：%s" % name)

    targets = [f]
    meta = {}
    side = f.with_suffix(".json")
    if side.exists():
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        targets.append(side)

    pdir, idx = None, None
    if meta.get("ass"):
        ap = Path(str(meta["ass"]))
        pdir = ap.parent
        m = re.search(r"sub_(\d+)\.ass$", ap.name)
        idx = m.group(1) if m else None
        if ap.exists():
            targets.append(ap)
    csv = meta.get("shots_csv")
    if csv and Path(str(csv)).exists():
        targets.append(Path(str(csv)))
    if pdir and idx:
        ad = pdir / ("audio_%s" % idx)
        if ad.is_dir():
            targets.append(ad)

    # project.json 一律保留：它只有几 KB，却是第 2/3 步继续改脚本、重出片的依据。
    # 以前「最后一条成片删掉就清项目」导致页面报「项目不存在」；要清项目请到项目列表里显式删。
    res = trash(targets)
    if str(f) in (res.get("failed") or []):
        raise RuntimeError("成片本身没删掉（可能正在播放或被占用），先关掉预览再试")
    res["name"] = name
    res["project_left"] = True          # 项目记录一律保留，方便继续改脚本/重出片
    return res


# ---------------------------------------------------------------- 任务控制

class JobCancelled(Exception):
    """用户在界面上点了「停止」。"""


def job_gate(job: dict) -> None:
    """每个处理单元开始前调一次：暂停就原地等，停止就抛 JobCancelled。
    长任务（打标 / 去字幕 / 批量出片）都在单元边界过这道闸，所以「暂停」是
    「这一条做完就停」，不是把正在跑的 ffmpeg 冻住。"""
    while job.get("pause") and not job.get("cancel"):
        if job.get("state") != "paused":
            job["state"] = "paused"
            job["message"] = "已暂停（当前这一条做完就停）"
        time.sleep(0.3)
    if job.get("cancel"):
        raise JobCancelled()
    if job.get("state") == "paused":
        job["state"] = "running"


# ---------------------------------------------------------------- ① 打标

def _grab_frame(src: str, t: float, dst: Path, width: int = 360) -> bool:
    try:
        r = run(["-v", "error", "-y", "-ss", "%.3f" % max(0.0, t), "-i", src,
                 "-frames:v", "1", "-vf", "scale=%d:-2" % width, "-q:v", "4", str(dst)], timeout=60)
        return dst.exists() and dst.stat().st_size > 600
    except Exception:
        return False


def grab_frames(item: dict, count: int = 3, width: int = 360, prefix: str = "f") -> list[Path]:
    """抽 count 帧。width/prefix 分开缓存：本地小图（s*）和云端大图（f*）互不覆盖。"""
    base = FRAMES / item["id"]
    base.mkdir(parents=True, exist_ok=True)
    dur = max(0.05, item["dur"])
    times = [item["start"] + dur * r for r in ([0.18, 0.5, 0.82] if count >= 3 else [0.34, 0.72][:count])]
    outs: list[Path] = []
    for i, t in enumerate(times[:count]):
        dst = base / ("%s%d.jpg" % (prefix, i))
        if not dst.exists():
            _grab_frame(item["path"], t, dst, width)
        if dst.exists():
            outs.append(dst)
    return outs


def ensure_thumb(item_id: str, width: int = 320) -> Path | None:
    """懒生成素材缩略图，供素材池列表用。"""
    pool = _pool_load()
    it = pool["items"].get(item_id)
    if not it:
        return None
    base = FRAMES / item_id
    base.mkdir(parents=True, exist_ok=True)
    dst = base / "t.jpg"
    if dst.exists() and dst.stat().st_size > 500:
        return dst
    t = float(it.get("start") or 0) + max(0.05, float(it.get("dur") or 0)) * 0.35
    return dst if _grab_frame(it["path"], t, dst, width) else None


def visual_feats(frames: list[Path]) -> dict:
    try:
        import cv2
        import numpy as np
    except Exception:
        return {}
    imgs = []
    for p in frames:
        im = cv2.imread(str(p))
        if im is not None:
            imgs.append(im)
    if not imgs:
        return {}
    gray = [cv2.cvtColor(i, cv2.COLOR_BGR2GRAY) for i in imgs]
    hsv = [cv2.cvtColor(i, cv2.COLOR_BGR2HSV) for i in imgs]
    bright = float(np.mean([g.mean() for g in gray]))
    sat = float(np.mean([h[:, :, 1].mean() for h in hsv])) / 255 * 100
    mean_bgr = np.mean([i.reshape(-1, 3).mean(0) for i in imgs], 0)
    warm = float(mean_bgr[2] - mean_bgr[0])
    edge = float(np.mean([cv2.Canny(g, 80, 170).mean() for g in gray]))
    motion = 0.0
    if len(gray) >= 2:
        a = gray[0].astype("float32")
        b = gray[-1].astype("float32")
        motion = float(np.abs(b - a).mean())

    labels = []
    labels.append("明亮" if bright > 150 else ("偏暗" if bright < 70 else "中间调"))
    if warm > 12:
        labels.append("暖色调")
    elif warm < -12:
        labels.append("冷色调")
    else:
        labels.append("中性色")
    labels.append("高饱和" if sat > 55 else ("低饱和" if sat < 22 else "正常饱和"))
    labels.append("细节丰富" if edge > 26 else ("主体突出" if edge < 12 else "中等细节"))
    labels.append("动态" if motion > 12 else ("轻微变化" if motion > 3 else "静止"))
    return {"bright": round(bright, 1), "sat": round(sat, 1), "warm": round(warm, 1),
            "edge": round(edge, 1), "motion": round(motion, 1), "labels": labels}


VISION_PROMPT = """你在为一条带货短视频整理素材库。下面是同一个镜头的 3 帧（开头/中间/结尾）。
用中文短词描述这个镜头，只输出 JSON，不要解释：
{"scene":"场景（如 卧室/厨房/户外/纯色背景/工厂车间）","subject":"画面主体（如 收纳盒/人手/包装盒）","action":"发生了什么（如 开箱/展示细节/压重物测试/无人静止）","shot":"特写|中景|全景","mood":"干净/温馨/专业/热闹","has_text":true或false}"""


LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"}


def is_local_url(url: str) -> bool:
    """判断是不是本机/内网模型端点（这类端点通常不校验 Key）。"""
    try:
        host = (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in LOCAL_HOSTS:
        return True
    return (host.startswith("192.168.") or host.startswith("10.")
            or host.startswith("172.16.") or host.endswith(".local"))


def _llm_endpoint(cfg: dict) -> tuple[str, str, str]:
    llm = cfg.get("llm") or {}
    base = (llm.get("base_url") or "").strip().rstrip("/")
    key = (llm.get("api_key") or "").strip()
    model = (llm.get("model") or "").strip()
    if base and not base.endswith("/v1") and "/chat/completions" not in base:
        base = base + "/v1"
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/chat/completions"
    return url, key, model


def llm_ready(cfg: dict) -> tuple[bool, str]:
    """能不能调大模型。本地端点允许不填 Key。"""
    url, key, _ = _llm_endpoint(cfg)
    if not url:
        return False, "未填 Base URL"
    if not key and not is_local_url(url):
        return False, "未填 API Key"
    return True, ""


def _models_url(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if "/chat/completions" in base:
        base = base.split("/chat/completions")[0]
    if base.endswith("/v1"):
        return base + "/models"
    return base + "/v1/models"


_VISION_HINTS = ("vl", "vision", "llava", "minicpm-v", "internvl", "glm-4v", "pixtral")
_MODELS_CACHE: dict[str, tuple[float, list]] = {}
_MODELS_LOCK = threading.Lock()

# 视觉能力实测缓存：base_url -> (时间, 能不能吃图)
# 本地 llama.cpp 常见情况是模型名带 VL 但启动时没带 --mmproj，光看名字会误判
_VISION_PROBE: dict[str, tuple[float, bool]] = {}
_VISION_LOCK = threading.Lock()
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")

# 本机常见本地推理服务的默认端口
LOCAL_PORTS = [
    ("llama.cpp", 9931), ("llama.cpp", 8080), ("Ollama", 11434),
    ("LM Studio", 1234), ("vLLM", 8000), ("KoboldCpp", 5001),
]


def list_models(base_url: str, timeout: int = 6, refresh: bool = False) -> list[dict]:
    """探测 OpenAI 兼容端点的模型列表，并判断每个模型能不能看图。"""
    url = _models_url(base_url)
    if not url:
        return []
    now = time.time()
    with _MODELS_LOCK:
        hit = _MODELS_CACHE.get(url)
    if hit and not refresh and now - hit[0] < 120:
        return hit[1]
    out: list[dict] = []
    try:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer sk-local"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        raw = j.get("data") or j.get("models") or []
        for m in raw:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or m.get("model") or m.get("name") or "")
            if not mid:
                continue
            mods = ((m.get("architecture") or {}).get("input_modalities")
                    or (m.get("meta") or {}).get("input_modalities") or [])
            if mods:
                vision = "image" in mods
            else:
                vision = any(h in mid.lower() for h in _VISION_HINTS)
            out.append({"id": mid, "vision": vision})
    except Exception:
        out = []
    with _MODELS_LOCK:
        _MODELS_CACHE[url] = (now, out)
    return out


def _probe_vision(cfg: dict, ttl: float = 600) -> bool:
    """真发一张 1x1 图试试，比看模型名字可靠得多。结果缓存 ttl 秒。"""
    url, key, model = _llm_endpoint(cfg)
    if not url:
        return False
    now = time.time()
    with _VISION_LOCK:
        hit = _VISION_PROBE.get(url)
    if hit:
        # 成功缓存久一点；失败只缓存 60 秒 —— 否则模型重启后要等 10 分钟才恢复语义打标
        if now - hit[0] < (ttl if hit[1] else 60):
            return hit[1]
    if not key:
        key = "sk-local"
    body = {"model": model or "x", "max_tokens": 1, "messages": [{"role": "user", "content": [
        {"type": "text", "text": "."},
        {"type": "image_url",
         "image_url": {"url": "data:image/png;base64," + base64.b64encode(_PNG_1X1).decode()}}]}]}
    ok = False
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key})
        handlers = []
        if (cfg.get("llm") or {}).get("bypass_proxy", True):
            handlers.append(urllib.request.ProxyHandler({}))
        with urllib.request.build_opener(*handlers).open(req, timeout=90) as r:
            r.read()
        ok = True
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read()[:800].decode("utf-8", "replace")
        except Exception:
            pass
        low = detail.lower()
        # 明确说“不支持图片”才算不支持；其它错误（比如上下文、参数）保守判为不支持
        ok = not ("not supported" in low or "mmproj" in low or "image input" in low)
    except Exception:
        ok = False
    with _VISION_LOCK:
        _VISION_PROBE[url] = (now, ok)
    return ok


def vision_supported(cfg: dict) -> bool:
    """这个端点能不能拿来做画面打标。显式填了视觉模型名就按填的算。"""
    llm = cfg.get("llm") or {}
    if (llm.get("vision_model") or "").strip():
        return True
    url, _, model = _llm_endpoint(cfg)
    if not url:
        return False
    if is_local_url(url):
        # 本地必须实测：Qwen3-VL 不带 --mmproj 时服务端会直接 500
        return _probe_vision(cfg)
    models = list_models(llm.get("base_url") or "")
    if models:
        for m in models:
            if not model or m["id"] == model:
                return bool(m["vision"])
        return False
    return any(h in model.lower() for h in _VISION_HINTS)


def scan_local(timeout: float = 1.2) -> list[dict]:
    """扫一遍本机常见的本地推理服务端口，返回可用端点及其模型。"""
    found = []
    for label, port in LOCAL_PORTS:
        base = "http://127.0.0.1:%d" % port
        models = list_models(base, timeout=timeout, refresh=True)
        if models:
            found.append({"label": label, "port": port, "base_url": base,
                          "models": models})
    return found


# ---------------------------------------------------------------- 本地模型启停
# 目标：点一下就把 llama-server 拉起来，不用单独开一个黑窗口、不用记参数。

LOCAL_STATE: dict = {"proc": None, "pid": 0, "t0": 0.0, "port": 0, "log": ""}
LOCAL_LOCK = threading.Lock()

_LLAMA_DIRS = [
    r"D:\llama.cpp", r"C:\llama.cpp", r"D:\llama", r"C:\llama",
    r"D:\AI\llama.cpp", r"C:\AI\llama.cpp",
]


def _split_args(s: str) -> list[str]:
    if not (s or "").strip():
        return []
    try:
        import shlex
        return [t.strip('"').strip("'") for t in shlex.split(s, posix=False) if t.strip()]
    except Exception:
        return s.split()


def local_cfg() -> dict:
    llm = load_cfg().get("llm") or {}
    try:
        port = int(llm.get("local_port") or 9931)
    except Exception:
        port = 9931
    return {
        "dir": str(llm.get("local_dir") or "").strip().strip('"'),
        "model": str(llm.get("local_model") or "").strip().strip('"'),
        "port": port,
        "extra": str(llm.get("local_extra") or "").strip(),
    }


def _get_json(url: str, timeout: float = 1.5):
    req = urllib.request.Request(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def local_health(port: int | None = None, timeout: float = 1.2) -> bool:
    port = port or local_cfg()["port"]
    try:
        j = _get_json("http://127.0.0.1:%d/health" % port, timeout)
        return str((j or {}).get("status", "")).lower() == "ok"
    except Exception:
        return False


def _tail(path: str, n: int = 6) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 6000))
            txt = f.read().decode("utf-8", "replace")
        lines = [x for x in txt.splitlines() if x.strip()]
        return " / ".join(lines[-n:])[:600]
    except Exception:
        return ""


def _is_mmproj(p: Path) -> bool:
    """视觉投影层（mmproj）不能单独当模型加载，选集时要排除。"""
    return "mmproj" in p.name.lower()


def _mm_family(name: str) -> str:
    """把模型名归一成「族名」：去掉 mmproj 标记、量化后缀与扩展名。
    Qwen3-VL-30B-A3B-abliterated-Q4_K_M.gguf          → qwen3-vl-30b-a3b-abliterated
    Qwen3-VL-30B-A3B-abliterated-mmproj-Q8_0.gguf     → qwen3-vl-30b-a3b-abliterated
    Qwen3-Coder-30B-A3B-abliterated-Q4_K_M.gguf       → qwen3-coder-30b-a3b-abliterated
    """
    n = name.lower()
    n = re.sub(r"\.gguf$", "", n)
    n = re.sub(r"[-_.]?mmproj[-_.]?", "-", n)
    n = re.sub(r"[-_.]q\d[\w.]*$", "", n)   # -q4_k_m / -q8_0 这类量化尾巴
    return n.strip("-_ .")


def _find_mmproj(model: str | Path) -> str:
    """给主模型找配套的视觉投影层。找不到 / 不配套都返回空串。

    以前只要目录里有 mmproj 就塞进去，于是 Qwen3-Coder 被配上了 Qwen3-VL 的 mmproj，
    llama-server 直接 `mismatch between text model (n_embd = 2048) and mmproj (n_embd = 8192)`
    退出。现在先比族名，族名对不上就不加 —— 宁可只当文本模型用，也不要起不来。
    """
    try:
        p = Path(model)
        d = p.parent
        if not d.is_dir():
            return ""
        cands = [q for q in d.glob("*.gguf") if _is_mmproj(q)]
        if not cands:
            return ""
        key = _mm_family(p.name)
        def same(q: Path) -> bool:
            qk = _mm_family(q.name)
            if not qk or not key:
                return False
            return qk == key or qk.startswith(key) or key.startswith(qk)
        hit = [q for q in cands if same(q)]
        if not hit:
            # 名字完全对不上时，只有当目录里只有一个 mmproj、且主模型名字明显是视觉模型才勉强用它
            looks_vl = re.search(r"(^|[-_])(vl|vision)([-_]|$)", p.name.lower()) is not None
            if len(cands) == 1 and looks_vl:
                hit = cands
            else:
                return ""
        return str(max(hit, key=lambda q: 1 if "q8" in q.name.lower() else 0))
    except Exception:
        return ""


def _extra_has_mmproj(extra: str) -> bool:
    return "--mmproj" in (extra or "")


def find_llama() -> dict:
    """定位 llama-server.exe 和要加载的 gguf（留空就挑 models 里最大的）。"""
    c = local_cfg()
    exe = ""
    base_dir = ""
    d = c["dir"]
    if d:
        p = Path(d)
        if p.is_file() and p.name.lower().endswith(".exe"):
            exe, base_dir = str(p), str(p.parent)
        elif p.is_dir():
            for rel in ("bin/llama-server.exe", "llama-server.exe",
                        "build/bin/Release/llama-server.exe", "build/bin/llama-server.exe"):
                q = p / rel
                if q.exists():
                    exe, base_dir = str(q), str(p)
                    break
    if not exe:
        for base in _LLAMA_DIRS:
            p = Path(base)
            if not p.is_dir():
                continue
            for rel in ("bin/llama-server.exe", "llama-server.exe",
                        "build/bin/Release/llama-server.exe"):
                q = p / rel
                if q.exists():
                    exe, base_dir = str(q), str(p)
                    break
            if exe:
                break
    model = c["model"]
    if model and not Path(model).is_file():
        model = ""
    if not model and exe:
        roots = [Path(base_dir) / "models" if base_dir else None,
                 Path(exe).resolve().parent, Path(exe).resolve().parent.parent / "models"]
        for root in roots:
            if not root or not root.is_dir():
                continue
            cands = sorted(root.glob("*.gguf"), key=lambda x: x.stat().st_size, reverse=True)
            cands = [q for q in cands if not _is_mmproj(q)]
            if cands:
                # 有配套视觉投影层的主模型优先：一个进程同时能写文案 + 看画面
                with_v = [q for q in cands if _find_mmproj(q)]
                model = str((with_v or cands)[0])
                break
    mmproj = ""
    if model and not _extra_has_mmproj(c["extra"]):
        mmproj = _find_mmproj(model)
    return {"ok": bool(exe and model), "exe": exe, "model": model,
            "mmproj": mmproj, "port": c["port"], "extra": c["extra"], "dir": base_dir}


def _pid_on_port(port: int) -> int:
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                           text=True, errors="replace", timeout=12,
                           creationflags=NO_WINDOW)
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[1].endswith(":%d" % port):
                return int(parts[4])
    except Exception:
        pass
    return 0


def local_status() -> dict:
    c = local_cfg()
    port = c["port"]
    proc = LOCAL_STATE.get("proc")
    alive = proc is not None and (proc.poll() is None)
    ready = local_health(port)
    info = find_llama()
    out = {
        "ok": True, "ready": ready, "alive": alive, "port": port,
        "pid": proc.pid if alive else 0,
        "elapsed": round(time.time() - LOCAL_STATE["t0"], 1) if (alive and LOCAL_STATE.get("t0")) else 0.0,
        "exe": info["exe"], "model": Path(info["model"]).name if info["model"] else "",
        "mmproj": Path(info["mmproj"]).name if info.get("mmproj") else "",
        "vision": bool(info.get("mmproj")),
        "dir": info["dir"], "found": info["ok"],
    }
    if ready:
        m = list_models("http://127.0.0.1:%d" % port, timeout=2, refresh=True)
        out["models"] = m
    else:
        log = LOCAL_STATE.get("log") or ""
        if log:
            out["tail"] = _tail(log, 4)
        if proc is not None and proc.poll() is not None:
            out["exit"] = proc.returncode
            out["tail"] = _tail(log, 8)
    return out


def _spawn_detached(args: list[str], cwd: str, logpath: str):
    """把 llama-server 拉成独立进程：关掉本工具它也不会跟着死。"""
    base = (0x00000008 | 0x08000000) if os.name == "nt" else 0  # DETACHED_PROCESS | CREATE_NO_WINDOW
    last = None
    # CREATE_BREAKAWAY_FROM_JOB：父进程在 Job 里时也能脱离。某些 Job 不允许，失败就退回普通方式
    extras = (0x01000000, 0) if os.name == "nt" else (0,)
    for extra in extras:
        fh = open(logpath, "wb")
        try:
            return subprocess.Popen(args, stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=cwd,
                                    creationflags=base | extra, close_fds=True)
        except Exception as exc:  # noqa: BLE001
            last = exc
        finally:
            try:
                fh.close()
            except Exception:
                pass
    raise last if last else RuntimeError("启动失败")


def start_local() -> dict:
    c = local_cfg()
    port = c["port"]
    if local_health(port):
        return {"ok": True, "state": "ready", "already": True, "port": port}
    with LOCAL_LOCK:
        proc = LOCAL_STATE.get("proc")
        if proc is not None and proc.poll() is None:
            return {"ok": True, "state": "starting", "port": LOCAL_STATE.get("port") or port}
        info = find_llama()
        if not info["ok"]:
            return {"ok": False, "port": port,
                    "error": "没找到 llama-server.exe 或 .gguf 模型。请在下面填 llama.cpp 目录（如 D:\\llama.cpp）"}
        # 不传 --no-webui：llama-server 默认就带内置对话界面，浏览器打开
        # http://127.0.0.1:<端口> 就能直接聊；传了 --no-webui 根路径会 404
        base = [info["exe"], "-m", info["model"],
                "--port", str(port), "--host", "127.0.0.1"]
        extra = _split_args(info["extra"])
        if not any(a in ("--parallel", "-np") or a.startswith("--parallel=") or a.startswith("-np=")
                       for a in extra):
            # 多槽（默认 4）会让 MoE 模型每个请求都掉到 1~2 tok/s：
            # 4 个槽各 8192 ctx 的 KV 一起挤显存，生成速度比单槽慢一个数量级。
            # 打标/写稿都是串行的，单槽反而最快。
            extra += ["--parallel", "1"]
        args = list(base)
        if info.get("mmproj"):
            args += ["--mmproj", info["mmproj"]]
        args += extra
        PIPE.mkdir(parents=True, exist_ok=True)
        logf = PIPE / "llama.log"
        try:
            proc = _spawn_detached(args, str(Path(info["exe"]).parent), str(logf))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "启动失败：%s" % exc, "port": port}
        LOCAL_STATE.update({"proc": proc, "pid": proc.pid, "t0": time.time(),
                            "port": port, "log": str(logf)})

    def wait(proc, secs: float = 3.5):
        """True=已就绪，False=进程已退出，None=还在加载。"""
        t0 = time.time()
        while time.time() - t0 < secs:
            if local_health(port, timeout=0.6):
                return True
            if proc.poll() is not None:
                return False
            time.sleep(0.3)
        return None

    mmproj = info.get("mmproj") or ""
    state = wait(proc)
    if state is False and mmproj:
        tail = _tail(str(logf), 6)
        # 模型和视觉投影层不配套（换模型后最常见）时 llama-server 会直接退出；
        # 去掉 --mmproj 再起一次，至少文本能用，不至于整个功能不可用。
        if "mismatch between text model" in tail or "failed to load multimodal" in tail:
            mmproj = ""
            logf = PIPE / "llama.log"
            try:
                proc = _spawn_detached(list(base) + extra, str(Path(info["exe"]).parent), str(logf))
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": "启动失败：%s" % exc, "port": port}
            LOCAL_STATE.update({"proc": proc, "pid": proc.pid, "t0": time.time(),
                                "port": port, "log": str(logf)})
            state = wait(proc)
    if state is False:
        return {"ok": False, "port": port, "exit": proc.returncode,
                "error": "llama-server 启动后立即退出：%s" % _tail(str(logf), 3)}

    out = {"ok": True, "state": "ready" if state else "starting", "port": port, "pid": proc.pid,
           "model": Path(info["model"]).name,
           "mmproj": Path(mmproj).name if mmproj else "",
           "vision": bool(mmproj), "exe": info["exe"]}
    if not mmproj and info.get("mmproj"):
        out["note"] = "视觉投影层和模型不配套，已按纯文本启动；要看图请换回同系列模型"
    return out


def stop_local() -> dict:
    port = local_cfg()["port"]
    proc = LOCAL_STATE.get("proc")
    killed = False
    if proc is not None and proc.poll() is None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=15, creationflags=NO_WINDOW)
            else:
                proc.terminate()
            killed = True
        except Exception:
            pass
    LOCAL_STATE.update({"proc": None, "pid": 0, "t0": 0.0})
    if not killed:
        pid = _pid_on_port(port)
        if pid:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=15, creationflags=NO_WINDOW)
                killed = True
            except Exception:
                pass
    time.sleep(0.4)
    return {"ok": True, "killed": killed, "ready": local_health(port, timeout=0.8), "port": port}


def llm_chat(messages: list, cfg: dict, model: str = "", max_tokens: int = 1500,
             temperature: float = 0.75, timeout: int = 150) -> str:
    url, key, model0 = _llm_endpoint(cfg)
    if not url:
        raise RuntimeError("未配置大模型（在「设置」里填 Base URL；本地模型不用填 Key）")
    local = is_local_url(url)
    if not key:
        if not local:
            raise RuntimeError("未配置 API Key（在「设置」里填）")
        key = "sk-local"
    if local:
        # 本地模型首字延迟高（prefill 慢），超时给宽一点
        timeout = max(timeout, 300)
    body = {
        "model": model or model0 or "deepseek-chat",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + key,
    })
    handlers = []
    if (cfg.get("llm") or {}).get("bypass_proxy", True):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read()[:400].decode("utf-8", "replace")
        raise RuntimeError("接口返回 %s：%s" % (e.code, detail))
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if local:
            # 读超时和「端口根本没开」是两回事，别让人以为是服务没起来
            if "timed out" in msg.lower() or "timeout" in msg.lower():
                raise RuntimeError(
                    "本地模型 %d 秒内没返回结果（模型太慢或被别的任务占满了），稍后再试" % timeout)
            raise RuntimeError("连不上本地模型 %s（llama-server 起来了吗？）：%s" % (url, msg))
        raise RuntimeError("连接失败：%s" % e)
    try:
        j = json.loads(raw)
        return j["choices"][0]["message"]["content"] or ""
    except Exception:
        raise RuntimeError("返回格式异常：%s" % _short(raw, 200))


def _json_from(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("没有 JSON")
    return json.loads(m.group(0))


def vision_tag(frames: list[Path], cfg: dict) -> dict:
    if not frames:
        return {}
    parts = []
    for i, p in enumerate(frames):
        b = base64.b64encode(p.read_bytes()).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}})
    content = [{"type": "text", "text": VISION_PROMPT}] + parts
    model = (cfg["llm"].get("vision_model") or "").strip()
    txt = llm_chat([{"role": "user", "content": content}], cfg, model=model,
                   max_tokens=260, temperature=0.2, timeout=90)
    return _json_from(txt)


def tag_worker(items: list[dict], cfg: dict, job: dict, force: bool = False) -> None:
    ready, why = llm_ready(cfg)
    use_vision = bool(ready and vision_supported(cfg))
    state = {"on": use_vision, "fail": 0}
    job["total"] = len(items)
    job["vision"] = use_vision
    if use_vision:
        job["message"] = "本地视觉特征 + 视觉模型语义标签"
    elif ready:
        job["message"] = "本地视觉特征（当前模型不支持看图，跳过语义标签）"
    else:
        job["message"] = "本地视觉特征（未配大模型：%s）" % why
    done = 0
    lock = threading.Lock()
    errors = 0
    results: dict[str, dict] = {}

    def flush() -> None:
        """把已完成的结果写回素材池（调用方需持锁）。
        以前只在整批结束时写一次：435 张图要跑近一小时，中途刷新页面或断开连接
        就白干，素材池里还是 0 已打标。现在每 8 条落一次盘。"""
        if not results:
            return
        pool = _pool_load()
        for pid, got in results.items():
            cur = pool["items"].get(pid)
            if not cur:
                continue
            cur.update(got)
            cur["tagged"] = True
            cur["tagged_at"] = time.time()
        _pool_save(pool)
        results.clear()

    try:
        _url, _, _ = _llm_endpoint(cfg)
    except Exception:  # noqa: BLE001
        _url = ""
    _local = bool(_url) and is_local_url(_url)
    # 本地模型 prefill 慢（MoE 跑在 CPU 上，约 40 tok/s），图越少越小越快：
    # 3 张 360px ≈ 800 tokens ≈ 20 秒/张；2 张 224px ≈ 300 tokens ≈ 4 秒，标签质量基本没差。
    _shots = 2 if (use_vision and _local) else 3
    _width = 224 if (use_vision and _local) else 360
    _prefix = "s" if (use_vision and _local) else "f"

    def one(it: dict) -> None:
        nonlocal done, errors
        try:
            frames = grab_frames(it, _shots, _width, _prefix)
            feats = visual_feats(frames)
            tags = list(feats.get("labels") or [])
            text = ""
            if state["on"] and frames and (force or not it.get("tagged")):
                try:
                    vt = vision_tag(frames, cfg)
                    for k, v in vt.items():
                        if k == "has_text":
                            if v:
                                tags.append("画面有文字")
                            continue
                        if isinstance(v, str) and v.strip():
                            tags.append(v.strip())
                    text = " ".join(str(v) for k, v in vt.items() if k != "has_text")
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors += 1
                        state["fail"] += 1
                        if state["fail"] >= 2 and state["on"]:
                            state["on"] = False
                            job["vision"] = False
                            job.setdefault("warn", []).append(
                                "视觉模型连不上/不支持看图，已改回只用本地视觉特征：%s"
                                % _short(str(exc), 100))
                        elif state["fail"] == 1:
                            job.setdefault("warn", []).append(
                                "语义打标失败（重试中）：%s" % _short(str(exc), 100))
            if not tags and feats.get("labels"):
                tags = list(feats["labels"])
            with lock:
                results[it["id"]] = {
                    "tags": list(dict.fromkeys(tags)), "feats": feats,
                    "text": text or " ".join(dict.fromkeys(tags)),
                    "semantic": bool(text),
                }
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors += 1
                job.setdefault("warn", []).append("%s: %s" % (it["name"][:16], _short(str(exc), 80)))
        finally:
            with lock:
                done += 1
                job["done"] = done
                job["progress"] = done / max(1, len(items))
                job["message"] = "打标 %d/%d" % (done, len(items))
                if done % 8 == 0:
                    flush()

    # 本地端点串行跑：并发只会把显存和算力抢成 1.5 tok/s；
    # 云端 API 有真正的并行能力，4 路能把 435 张图的打标时间砍到 1/4。
    workers = 1 if (not use_vision or not _url or _local) else 4
    if workers == 1 and use_vision:
        job["message"] = "打标（本地模型串行，约 3~5 秒/张）"
    cancelled = False
    if workers == 1:
        for it in items:
            if job.get("cancel"):
                cancelled = True
                break
            try:
                job_gate(job)
            except JobCancelled:
                cancelled = True
                break
            one(it)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, items))

    with lock:
        flush()

    job["result"] = {"tagged": len(items), "errors": errors, "semantic": state["on"]}
    if cancelled:
        job["message"] = "已停止：完成 %d/%d 个" % (done, len(items))
        job["state"] = "cancel"
        return
    job["message"] = "打标完成 %d 个素材%s" % (
        len(items), "" if state["on"] else "（仅视觉特征，语义标签需配视觉模型）")
    job["state"] = "done"
    job["progress"] = 1.0


# ---------------------------------------------------------------- ② 脚本

ANGLES = {
    # —— 经典五式：转化最稳，适合大多数品类 ——
    "pain": {"label": "痛点切入", "hint": "先讲麻烦，再给解法", "group": "classic"},
    "compare": {"label": "对比实测", "hint": "跟替代品放一起比", "group": "classic"},
    "spec": {"label": "参数硬核", "hint": "用数字建立信任", "group": "classic"},
    "avoid": {"label": "避坑提醒", "hint": "教人别买错", "group": "classic"},
    "scene": {"label": "场景代入", "hint": "放进真实使用场景", "group": "classic"},
    # —— 更多风格：换语气、换人称、换切入逻辑，专门拉开重复率 ——
    "story": {"label": "亲身自述", "hint": "第一人称，用了多久、变化在哪", "group": "more"},
    "price": {"label": "算账划算", "hint": "算单次成本，不喊便宜", "group": "more"},
    "qa": {"label": "问答答疑", "hint": "自问自答，专治评论区疑问", "group": "more"},
    "crowd": {"label": "口碑回购", "hint": "别人怎么说、有没有回头客", "group": "more"},
    "unbox": {"label": "开箱第一眼", "hint": "拆箱、上手、第一反应", "group": "more"},
    "howto": {"label": "用法教程", "hint": "教怎么用怎么吃，顺手带产品", "group": "more"},
    "gift": {"label": "送礼场景", "hint": "送长辈送朋友，讲面子和实用", "group": "more"},
    "season": {"label": "时令当季", "hint": "挂上季节、节日、天气", "group": "more"},
    "counter": {"label": "反常识", "hint": "先说一个和直觉相反的结论", "group": "more"},
}

CPS = 4.6  # 中文口播近似语速：字/秒（rate≈+8%）


def _split_points(raw) -> list[str]:
    if isinstance(raw, list):
        parts = raw
    else:
        parts = re.split(r"[\n;；,，、]+", str(raw or ""))
    return [p.strip(" 　·-—") for p in parts if str(p).strip(" 　·-—")]


def _sent(text: str, hint: str) -> dict:
    return {"text": text.strip(), "hint": hint}


def template_script(angle: str, product: dict, target: float) -> dict:
    name = product.get("name") or "这个产品"
    pts = _split_points(product.get("points")) or ["用料扎实", "用起来省心"]
    pain = product.get("pain") or "买回来用两次就闲置"
    aud = product.get("audience") or "家里东西多的人"
    price = product.get("price") or ""
    offer = product.get("offer") or ""
    proof = product.get("proof") or ""

    if re.fullmatch(r"[\d.]+", price.strip()):
        price = price.strip() + "元"
    p0 = pts[0]
    p1 = pts[1] if len(pts) > 1 else "细节做得比同价位好"
    p2 = pts[2] if len(pts) > 2 else ""

    H = "特写"
    S = "中景"
    W = "全景"

    if angle == "pain":
        sents = [
            _sent("%s，是不是%s？" % (aud, pain), W + " 场景"),
            _sent("我前后换了三个，最后只留下%s。" % name, H + " 产品 主体"),
            _sent("它最直接的地方是%s。" % p0, H + " 细节 特写"),
            _sent("%s，这点对%s很关键。" % (p1, aud), S + " 使用 手"),
            _sent("用完不用收，随手一放就是原样。", S + " 场景"),
        ]
        cta = "价格%s，%s，点下面看看。" % (price or "不贵", offer) if (price or offer) else "点下面看看，不合适再退。"
        sents.append(_sent(cta, H + " 产品 包装"))
    elif angle == "compare":
        sents = [
            _sent("同样%s，为什么有人用得顺、有人一直换？" % (name), S + " 对比 产品"),
            _sent("我把便宜的和%s放一起用了两周。" % name, W + " 并列 产品"),
            _sent("先说差别：便宜的那个%s。" % "一压就变形", H + " 对比 细节"),
            _sent("%s，%s。" % (name, p0), H + " 细节 特写"),
            _sent("%s，用久了也不会松。" % p1, S + " 使用 手"),
            _sent("贵的那点点钱，其实省在后面。", S + " 场景"),
        ]
        cta = "%s，链接在下面。" % (offer or "现在有活动")
        sents.append(_sent(cta, H + " 产品 包装"))
    elif angle == "spec":
        sents = [
            _sent("不讲虚的，直接把%s的参数摆出来。" % name, H + " 产品 正面"),
            _sent("第一，%s。" % p0, H + " 细节 特写"),
            _sent("第二，%s。" % p1, S + " 使用 手"),
        ]
        if p2:
            sents.append(_sent("第三，%s。" % p2, H + " 细节"))
        if proof:
            sents.append(_sent("%s，这个不是我说的。" % proof, S + " 证明 材料"))
        sents.append(_sent("参数摆在这，自己判断值不值。", S + " 产品"))
        cta = "%s，点下面看详情。" % (price or "价格在链接里")
        sents.append(_sent(cta, H + " 产品"))
    elif angle == "avoid":
        sents = [
            _sent("买%s，这三个坑我踩过。" % name, S + " 产品"),
            _sent("第一个坑，只看%s，不看%s。" % ("价格", p1.split("，")[0][:6]), H + " 细节"),
            _sent("第二个坑，买回来尺寸不对，塞不进柜子。", W + " 场景 柜子"),
            _sent("第三个坑，材质薄，用两次就塌。", H + " 细节"),
            _sent("%s把这三个都躲开了：%s。" % (name, p0), H + " 产品 特写"),
            _sent("%s，这是我能确认的部分。" % p1, S + " 使用"),
        ]
        cta = "别急着下单，先看完%s。" % (offer or "参数")
        sents.append(_sent(cta, S + " 产品"))
    elif angle == "story":
        sents = [
            _sent("说说我自己用%s这段时间。" % name, S + " 产品 手"),
            _sent("一开始也没抱期望，%s。" % pain, W + " 场景"),
            _sent("用了一个月，最明显的是%s。" % p0, H + " 细节 特写"),
            _sent("现在它固定放在厨房台面，伸手就能拿。", S + " 场景"),
            _sent("%s，这个变化我自己能感觉到。" % p1, S + " 使用 手"),
        ]
        cta = "想试的自己看，%s。" % (offer or "链接在下面")
        sents.append(_sent(cta, H + " 产品"))
    elif angle == "price":
        sents = [
            _sent("别急着看标价，先算一下单次成本。", S + " 产品"),
            _sent("它卖%s，能用很久。" % (price or "这个价"), H + " 产品 标签"),
            _sent("%s，所以不是只能干一件事。" % p1, S + " 使用"),
            _sent("%s，不容易坏，这点最省钱。" % p0, H + " 细节"),
            _sent("摊到每天，其实就一顿早饭的钱。", S + " 场景"),
        ]
        cta = "%s，点下面看现在的价。" % (offer or "有活动的时候更合适")
        sents.append(_sent(cta, H + " 产品 包装"))
    elif angle == "qa":
        sents = [
            _sent("评论区问得最多的三个问题，我一次说清。", S + " 产品"),
            _sent("第一，%s，这点可以放心。" % p0, H + " 细节"),
            _sent("第二，%s，正常用没问题。" % p1, S + " 使用 手"),
            _sent("第三，值不值？%s。" % (price or "看你用得频不频繁"), S + " 场景"),
            _sent("还有别的疑问，评论区问我。", S + " 产品"),
        ]
        cta = "想直接看细节的，链接在下面。"
        sents.append(_sent(cta, H + " 产品"))
    elif angle == "crowd":
        sents = [
            _sent("这个%s，我身边几乎人手一个。" % name, W + " 场景 多件"),
            _sent("有人买第二次，是给家里人也带了一个。", S + " 产品"),
            _sent("大家提得最多的还是%s。" % p0, H + " 细节 特写"),
            _sent("%s，回头客专门说过这点。" % p1, S + " 使用 手"),
            _sent("不好用的话，不会有人回购。", S + " 场景"),
        ]
        cta = "%s，点下面看大家怎么说。" % (offer or "现在下单的人多")
        sents.append(_sent(cta, H + " 产品"))
    elif angle == "unbox":
        sents = [
            _sent("刚从箱子里拿出来，先看第一眼。", W + " 开箱 桌面"),
            _sent("包装不花，但%s。" % p0, H + " 包装 特写"),
            _sent("上手第一感觉：%s。" % p1, S + " 手 产品"),
            _sent("细节我凑近看一下，边缘是干净的。", H + " 细节 特写"),
            _sent("第一眼，我给它的评价是：比图好看。", S + " 产品"),
        ]
        cta = "想看细节的，点下面。"
        sents.append(_sent(cta, H + " 产品"))
    elif angle == "howto":
        sents = [
            _sent("很多人拿到%s，其实用错了。" % name, S + " 产品 手"),
            _sent("第一步，%s。" % p0, H + " 步骤 特写"),
            _sent("第二步，%s。" % p1, S + " 手 演示"),
            _sent("这样弄，%s。" % (p2 or "省事一半"), S + " 使用"),
            _sent("就差这一步，用起来差别很明显。", H + " 细节"),
        ]
        cta = "%s，照着做就行。" % (offer or "链接放在下面")
        sents.append(_sent(cta, H + " 产品"))
    elif angle == "gift":
        sents = [
            _sent("过节送人，最怕送出去人家不用。", W + " 场景 礼盒"),
            _sent("%s当礼物，%s。" % (name, p0), H + " 产品 包装"),
            _sent("拿得出手，也不至于让人有负担。", S + " 送礼 手"),
            _sent("长辈、同事都能送，不挑人。", S + " 场景"),
            _sent("%s，收的人也会问在哪买。" % p1, H + " 细节"),
        ]
        cta = "%s，赶在节前下单。" % (offer or "礼盒在下面")
        sents.append(_sent(cta, H + " 包装"))
    elif angle == "season":
        sents = [
            _sent("天气一转，%s就该拿出来了。" % name, W + " 季节 场景"),
            _sent("这个时候买，%s。" % (offer or "价格也合适"), S + " 产品"),
            _sent("%s，应季的时候最明显。" % p0, H + " 细节 特写"),
            _sent("%s，过这一阵就不好挑。" % p1, S + " 使用 手"),
            _sent("当季的东西，不用犹豫太久。", S + " 场景"),
        ]
        cta = "点下面，%s。" % (offer or "看看还有没有货")
        sents.append(_sent(cta, H + " 产品"))
    elif angle == "counter":
        sents = [
            _sent("关于%s，有个说法其实是反的。" % name, S + " 产品"),
            _sent("很多人以为越贵越好，关键其实在%s。" % p0.split("，")[0][:10], H + " 细节"),
            _sent("我把两种都试了一遍，差的不是价格。", W + " 对比 桌面"),
            _sent("%s，这才是影响体验的地方。" % p1, H + " 细节 特写"),
            _sent("想明白这一点，就不会买错。", S + " 使用"),
        ]
        cta = "%s，自己看一眼就懂。" % (offer or "链接在下面")
        sents.append(_sent(cta, H + " 产品"))
    else:  # scene
        sents = [
            _sent("早上七点，%s开始忙。" % aud, W + " 清晨 场景"),
            _sent("%s就放在这，伸手就能拿到。" % name, S + " 场景 产品"),
            _sent("%s，所以不用弯腰、不用翻。" % p0, H + " 细节 手"),
            _sent("晚上收的时候更省事，%s。" % p1, S + " 使用 手"),
            _sent("一天两次，用了一年还是这样。", S + " 场景"),
            _sent("%s，是真的每天都在用。" % name, H + " 产品 特写"),
        ]
        cta = "%s，点下面就能看到。" % (offer or "有活动的时候再买")
        sents.append(_sent(cta, H + " 产品 包装"))

    # 按目标时长裁剪：留 hook + CTA，中间按字数预算取舍
    budget = max(40, int(target * CPS))
    head, body, tail = sents[0], sents[1:-1], sents[-1]
    picked = [head]
    used = len(head["text"])
    for s in body:
        if used + len(s["text"]) + len(tail["text"]) <= budget:
            picked.append(s)
            used += len(s["text"])
    picked.append(tail)
    title = "%s｜%s" % (name, p0.split("，")[0][:12])
    return {"angle": angle, "angle_label": ANGLES[angle]["label"], "title": title,
            "sentences": picked}


def script_prompt(product: dict, angles: list[str], target: float, tags: list[str]) -> str:
    pts = _split_points(product.get("points"))
    return """你是带货短视频的口播编剧。写 %d 条不同角度的口播脚本。

【产品】%s
【卖点】%s
【目标人群】%s
【用户痛点】%s
【价格/优惠】%s
【信任背书】%s

【可用画面标签】%s
（写 visual 时只能从上面这些标签里挑，挑不到就写"产品特写"）

【要求】
1. 每条目标时长 %d 秒，中文口播约 %.1f 字/秒，所以每条总字数控制在 %d 字左右（±15%%）。
2. 风格分别是：%s
3. 不同风格之间要真的不一样：人称、开头方式、论证顺序、收尾都要换，不能只替换几个词，也不要把同几句重复用。
4. 开头 3 秒必须抓住人：用提问、反常识、或具体数字，禁止"大家好""今天给大家推荐"。
5. 口语，短句，一句一个意思，不要书面语，不要感叹号堆砌。
6. 功效不得夸大，不得出现"最""第一""根治""治疗""百分百"这类词。
7. 每条最后一句是行动指令（去哪里、做什么），不要写"点赞关注"。
8. visual 写具体景别加主体，例如"特写 果肉""中景 手拿""全景 桌面"；同一条里不要每句都写同一个词。

只输出 JSON 数组，不要解释：
[{"angle":"角度key","title":"内部标题","sentences":[{"text":"口播句子","visual":"画面标签"}]}]
角度key 只能是：%s
""" % (
        len(angles), product.get("name") or "", "、".join(pts) or "", product.get("audience") or "",
        product.get("pain") or "", product.get("price") or product.get("offer") or "",
        product.get("proof") or "", "、".join(tags[:60]) or "（素材库为空）",
        int(target), CPS, int(target * CPS),
        "、".join("%s（%s）" % (ANGLES[a]["label"], ANGLES[a].get("hint") or "") for a in angles),
        ", ".join(angles))


def _parse_scripts(raw: str, chunk: list[str]) -> list[dict]:
    """把大模型返回的 JSON 数组解析成脚本列表，角度按本批顺序兜底。"""
    raw = (raw or "").strip()
    m = re.search(r"\[.*\]", raw, re.S)
    arr = json.loads(m.group(0) if m else raw)
    used, out = set(), []
    for i, s in enumerate(arr):
        sents = [x for x in (s.get("sentences") or []) if str(x.get("text", "")).strip()]
        if not sents:
            continue
        ang = s.get("angle")
        if ang not in chunk:
            ang = next((a for a in chunk if a not in used), chunk[i % len(chunk)])
        if ang in used:
            continue
        used.add(ang)
        out.append({
            "angle": ang,
            "angle_label": ANGLES[ang]["label"],
            "title": s.get("title") or ANGLES[ang]["label"],
            "sentences": [{"text": str(x["text"]).strip(), "hint": str(x.get("visual") or "")}
                          for x in sents],
        })
    return out


def gen_scripts(product: dict, angles: list[str], target: float, cfg: dict,
                extra_tags: list[str] | None = None, use_llm: bool = True,
                job: dict | None = None) -> dict:
    """返回 {scripts:[...], mode:"llm"|"template", note:""}"""
    angles = [a for a in (angles or list(ANGLES)) if a in ANGLES] or list(ANGLES)
    tags = extra_tags or []
    if use_llm and llm_ready(cfg)[0]:
        # 一次要 10 多条脚本时，模型的输出长度和质量都会掉，分批问；
        # 每批 3 条：单批 20~30 秒，点「停止」最多等这么久就能收手
        chunks = [angles[i:i + 3] for i in range(0, len(angles), 3)]
        out, ok, err = [], 0, ""
        try:
            for chunk in chunks:
                if job is not None:
                    job_gate(job)      # 停止会在「当前这一批角度写完」后生效
                try:
                    txt = llm_chat([{"role": "user", "content": script_prompt(product, chunk, target, tags)}],
                                   cfg, max_tokens=3200, temperature=0.85, timeout=180)
                    got = _parse_scripts(txt, chunk)
                    if got:
                        out.extend(got)
                        ok += 1
                    else:
                        out.extend(template_script(a, product, target) for a in chunk)
                except Exception as exc:  # noqa: BLE001
                    err = _short(str(exc), 160)
                    out.extend(template_script(a, product, target) for a in chunk)
        except JobCancelled:
            # 已经写完的批次留着，别因为点了停止就白跑
            if out:
                return {"scripts": out, "mode": "llm", "cancelled": True,
                        "note": "已停止：保留了已生成的 %d 条" % len(out)}
            raise
        if job is not None and job.get("cancel"):
            if out:
                return {"scripts": out, "mode": "llm", "cancelled": True,
                        "note": "已停止：只保留了已生成的 %d 条" % len(out)}
            raise JobCancelled()
        if ok:
            got = {s["angle"] for s in out}
            for a in angles:
                if a not in got:
                    out.append(template_script(a, product, target))
            order = {a: i for i, a in enumerate(angles)}
            out.sort(key=lambda s: order.get(s["angle"], 999))
            note = "由大模型生成" + ("（分 %d 批）" % len(chunks) if len(chunks) > 1 else "")
            if ok < len(chunks):
                note += "，%d 批失败已用模板补" % (len(chunks) - ok)
            return {"scripts": out, "mode": "llm", "note": note}
        return {"scripts": [template_script(a, product, target) for a in angles],
                "mode": "template", "note": "大模型调用失败，已用模板兜底：%s" % err}
    return {"scripts": [template_script(a, product, target) for a in angles],
            "mode": "template", "note": "未配置大模型，用模板骨架生成"}


# ---------------------------------------------------------------- ③ 配音

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _tts_edge(text: str, voice: str, rate: str, out_mp3: Path) -> list[dict]:
    import asyncio

    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    import edge_tts

    async def go():
        c = edge_tts.Communicate(text, voice, rate=rate)
        buf = bytearray()
        bounds = []
        async for ch in c.stream():
            if ch.get("type") == "audio":
                buf.extend(ch["data"])
            elif ch.get("type") in ("SentenceBoundary", "WordBoundary"):
                bounds.append({"type": ch["type"], "text": ch.get("text") or "",
                               "offset": (ch.get("offset") or 0) / 1e7,
                               "duration": (ch.get("duration") or 0) / 1e7})
        return bytes(buf), bounds

    audio, bounds = asyncio.run(go())
    if not audio:
        raise RuntimeError("edge-tts 没有返回音频")
    out_mp3.write_bytes(audio)
    return bounds


def _sapi_tts(sentences: list[str], out_wav: Path, workdir: Path, rate: int = 2) -> list[float]:
    """Windows 离线兜底：逐句合成，拼接，返回每句时长。"""
    ps = workdir / "tts.ps1"
    ps.write_text(r"""
$ErrorActionPreference = 'Stop'
$out = $args[0]
$rate = [int]$args[1]
$txtfile = $args[2]
$v = New-Object -ComObject SAPI.SpVoice
# 自动挑一个中文音色，挑不到就用默认
try{
  $vs = $v.GetVoices()
  for ($i=0; $i -lt $vs.Count; $i++) {
    $vo = $vs.Item($i)
    $desc = $vo.GetDescription()
    if ($desc -match 'Chinese|Huihui|Kangkang|Yaoyao|中文') { $v.Voice = $vo; break }
  }
}catch{}
$v.Rate = $rate
$fs = New-Object -ComObject SAPI.SpFileStream
$fs.Format.Type = 22
$fs.Open($out, 3, $false)
$v.AudioOutputStream = $fs
$text = [IO.File]::ReadAllText($txtfile, [Text.Encoding]::UTF8)
$v.Speak($text)
$fs.Close()
""", encoding="utf-8")
    dur_list: list[float] = []
    parts: list[Path] = []
    for i, s in enumerate(sentences):
        txt = workdir / ("s%d.txt" % i)
        txt.write_text(s, encoding="utf-8")
        wav = workdir / ("s%02d.wav" % i)
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps),
               str(wav), str(rate), str(txt)]
        subprocess.run(cmd, capture_output=True, timeout=120)
        if not wav.exists() or wav.stat().st_size < 200:
            raise RuntimeError("系统语音合成失败：%s" % s[:20])
        dur_list.append(media_duration(wav))
        parts.append(wav)
    lst = workdir / "list.txt"
    lst.write_text("\n".join("file '%s'" % str(p.resolve()).replace("\\", "/").replace("'", "'\\''")
                             for p in parts), encoding="utf-8")
    r = run(["-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(out_wav)], timeout=300)
    if not out_wav.exists():
        raise RuntimeError("拼接音频失败：%s" % _short(r.stderr, 200))
    return dur_list


def tts_worker(project: dict, script: dict, cfg: dict, job: dict) -> None:
    pdir = project_dir(project["id"])
    adir = pdir / ("audio_%d" % int(script.get("idx", 0)))
    adir.mkdir(parents=True, exist_ok=True)
    # 空句子（还没写的行）不送去配音，否则会多出一段空口播、对齐时白占一个镜头
    sents = [s for s in script["sentences"] if str(s.get("text") or "").strip()]
    if not sents:
        raise RuntimeError("这条脚本还没有文字，先写点内容再配音")
    texts = [s["text"] for s in sents]
    voice = (cfg["tts"].get("voice") or "zh-CN-XiaoxiaoNeural")
    rate = (cfg["tts"].get("rate") or "+0%")
    gap = float(cfg["tts"].get("gap") or 0.0)
    engine = (cfg["tts"].get("engine") or "edge").lower()

    job_gate(job)
    job["message"] = "合成配音…"
    job["progress"] = 0.15
    note = ""
    mode = engine
    try:
        if engine == "edge" and _has_edge():
            mp3 = adir / "tts.mp3"
            full = "\n".join(texts)
            bounds = _tts_edge(full, voice, rate, mp3)
            timings = _map_bounds(texts, bounds, media_duration(mp3))
            audio = mp3
        else:
            out_wav = adir / "tts.wav"
            durs = _sapi_tts(texts, out_wav, adir)
            timings, t = [], 0.0
            for i, d in enumerate(durs):
                timings.append({"text": texts[i], "start": round(t, 3), "end": round(t + d, 3)})
                t += d
            audio = out_wav
            mode = "sapi"
            note = "使用了 Windows 离线语音，音色较机械；装好 edge-tts 并联网可换真人感音色"
    except Exception as exc:  # noqa: BLE001
        if engine == "edge":
            job["message"] = "在线配音失败，切离线语音…"
            out_wav = adir / "tts.wav"
            durs = _sapi_tts(texts, out_wav, adir)
            timings, t = [], 0.0
            for i, d in enumerate(durs):
                timings.append({"text": texts[i], "start": round(t, 3), "end": round(t + d, 3)})
                t += d
            audio = out_wav
            mode = "sapi"
            note = "在线配音失败（%s），已用 Windows 离线语音兜底" % _short(str(exc), 80)
        else:
            raise

    total = media_duration(audio)
    job["progress"] = 0.9
    if gap > 0 and mode == "sapi":
        pass
    # 画面提示一起存进时间轴：对齐时按句选素材全靠它，不带就得回头查脚本
    for t, src in zip(timings, sents):
        if not t.get("hint"):
            t["hint"] = str(src.get("hint") or "").strip()
    script["tts"] = {
        "audio": str(audio), "engine": mode, "voice": voice, "rate": rate,
        "sentences": timings, "total": round(total, 3), "note": note,
    }
    project_save(project)
    job["result"] = {"total": round(total, 3), "sentences": len(timings), "engine": mode, "note": note,
                     "engine_label": ("edge-tts 在线" if mode == "edge" else "Windows 离线语音")}
    job["message"] = "配音完成：%d 句 / %.1f 秒" % (len(timings), total)
    job["state"] = "done"
    job["progress"] = 1.0


def _map_bounds(texts: list[str], bounds: list[dict], audio_dur: float) -> list[dict]:
    """把 edge-tts 的逐句时间戳映射回我们的句子列表。

    edge-tts 自己断句，可能和我们的句子不是一一对应（有人句子被合并或拆开），
    所以按「字符位置区间重叠」来对齐，而不是按序号硬配。
    """
    if not bounds:
        raise RuntimeError("没有拿到时间戳")
    heads = [b for b in bounds if b["type"] == "SentenceBoundary"] or bounds
    full = _norm("\n".join(texts))
    # 每句在全文里的字符区间
    spans, cur = [], 0
    for t in texts:
        n = len(_norm(t))
        spans.append((cur, cur + n))
        cur += n
    # 每个 boundary 的字符区间（用游标搜索定位）
    bspans, cursor = [], 0
    for b in heads:
        bt = _norm(b["text"])
        if not bt:
            continue
        pos = full.find(bt, max(0, cursor - 2))
        if pos < 0:
            pos = full.find(bt)
        if pos < 0:
            continue
        bspans.append((pos, pos + len(bt), b["offset"], b["offset"] + b["duration"]))
        cursor = pos + len(bt)
    out = []
    for i, (s0, s1) in enumerate(spans):
        hit = [b for b in bspans if b[0] < s1 and b[1] > s0]
        if hit:
            st = min(b[2] for b in hit)
            en = max(b[3] for b in hit)
        else:  # 兜底：按字符比例估算
            frac = s0 / max(1, len(full))
            st = frac * audio_dur
            en = st + (s1 - s0) / CPS
        out.append({"text": texts[i], "start": round(st, 3), "end": round(en, 3)})
    # 单调化 + 空隙补到下一句开头
    for i in range(len(out)):
        if i and out[i]["start"] < out[i - 1]["start"]:
            out[i]["start"] = out[i - 1]["end"]
        if out[i]["end"] <= out[i]["start"]:
            out[i]["end"] = out[i]["start"] + 0.6
    if out:
        out[-1]["end"] = min(out[-1]["end"], max(audio_dur, out[-1]["start"] + 0.3)) if audio_dur else out[-1]["end"]
    return out


def tts_all_worker(project: dict, cfg: dict, job: dict, force: bool = False) -> None:
    """给所有脚本一次配完音。已经配好的跳过（force=True 才重做）。"""
    scripts = project.get("scripts") or []
    if not scripts:
        raise RuntimeError("还没有脚本，先在第 2 步生成脚本")
    total = len(scripts)
    job["total"] = total
    job["done"] = 0
    made = skipped = 0
    failed: list[dict] = []
    for i, s in enumerate(scripts):
        job_gate(job)
        label = s.get("angle_label") or ("脚本 %d" % (i + 1))
        if not force and _voice_ok(s):
            skipped += 1
            job["done"] = i + 1
            job["progress"] = (i + 1) / total
            job["message"] = "[%d/%d] %s 已有配音，跳过" % (i + 1, total, label)
            continue
        job["message"] = "[%d/%d] %s 配音中…" % (i + 1, total, label)
        try:
            sub = _SubJob(lambda f, i=i: job.__setitem__(
                "progress", min(0.999, (i + max(0.0, min(1.0, f))) / total)))
            tts_worker(project, s, cfg, sub)
            made += 1
        except JobCancelled:
            job["message"] = "已停止：新配 %d 条，跳过 %d 条" % (made, skipped)
            job["state"] = "cancel"
            return
        except Exception as exc:  # noqa: BLE001
            failed.append({"idx": i, "angle": label, "error": _short(str(exc), 160)})
        job["done"] = i + 1
        job["progress"] = (i + 1) / total

    job["message"] = "配音完成：新配 %d 条，跳过 %d 条%s" % (
        made, skipped, ("，%d 条失败" % len(failed)) if failed else "")
    job["result"] = {"made": made, "skipped": skipped, "failed": failed}
    job["state"] = "done"
    job["progress"] = 1.0


# ---------------------------------------------------------------- ④ 对齐

SHOT_WORDS = ("特写", "近景", "中景", "全景", "远景")
# 本地视觉特征打的曝光/色彩标签：选片时几乎没有区分度，不能跟"厨房""特写"同权
CV_LABELS = {"明亮", "偏暗", "中间调", "暖色调", "冷色调", "中性色", "高饱和", "低饱和",
             "正常饱和", "细节丰富", "主体突出", "中等细节", "动态", "轻微变化", "静止"}


def _keys(hint: str) -> list[str]:
    raw = re.split(r"[\s/、,，·]+", hint or "")
    out = []
    for k in raw:
        k = k.strip()
        if len(k) >= 2:
            out.append(k)
    return out


def _sem_tags(item: dict) -> set:
    return {t for t in (item.get("tags") or []) if t not in CV_LABELS}


MOOD_WORDS = {"干净", "温馨", "专业", "热闹", "自然"}
GENERIC_TAGS = set(SHOT_WORDS) | MOOD_WORDS | {"画面有文字"}


def _scene_tags(item: dict) -> set:
    """真正区分画面的词（场景/主体/动作），不算景别和情绪形容词。"""
    return _sem_tags(item) - GENERIC_TAGS


def _blob(item: dict) -> str:
    return (item.get("text") or "") + " " + " ".join(item.get("tags") or [])


def _candidates(items: list[dict], keys: list[str]) -> list[dict]:
    """先挑"内容对得上"的素材，一条都没有（素材里没这个主体/动作）才退回全池。

    以前内容词和景别词同权相加，"中景 翻炒"会挑中一个中景的菌包：景别对了、画面跑题。
    带货片里画面跑题比景别差一号严重得多，所以内容优先、景别当加分项。
    """
    other = [k for k in keys if k not in SHOT_WORDS]
    if not other:
        return items
    hit = [it for it in items if any(k in _blob(it) for k in other)]
    return hit or items


def _score(item: dict, keys: list[str], usage: dict, want: float, last: dict | None,
           used_sem: set | None = None) -> float:
    """给一个素材打分。

    景别单独加权：讲细节配个全景、讲场景配个特写，比配错场景更出戏，所以
    hint 要的景别对上了给最高分，明显是别的景别直接扣分。
    场景/主体/动作按普通关键词算；同一句里画面很像的镜头再压一点分，
    避免一条 5 秒的口播下面连着三个"厨房+木耳+展示细节"。
    """
    sem = _sem_tags(item)
    scene = _scene_tags(item)
    blob = _blob(item)
    s = 0.0
    want_shot = {k for k in keys if k in SHOT_WORDS}
    s += sum(2.2 for k in keys if k not in SHOT_WORDS and k in blob)
    if want_shot:
        if want_shot & sem:
            s += 4.2
        elif sem & set(SHOT_WORDS):
            s -= 2.2
    if used_sem:
        # 只有一个共同词（比如都是"木耳"）是正常的，重合两个以上说明画面很接近
        s -= min(2.4, 1.2 * max(0, len(used_sem & scene) - 1))
    avail = item.get("dur") or 0.0
    if avail >= want:
        s += 0.9
    else:
        s -= min(3.2, (want - avail) / max(0.15, want) * 3.2)
    s -= usage.get(item["id"], 0) * 2.6
    if last:
        if item["path"] == last["path"] and abs(item["index"] - last.get("index", 0)) <= 1:
            s -= 1.8
        if item["id"] == last["id"]:
            s -= 4.0
    return s


def align(project: dict, script: dict, cfg: dict, seed: int | None = None) -> dict:
    tts = script.get("tts") or {}
    sents = tts.get("sentences") or []
    if not sents:
        raise RuntimeError("还没有配音，先做配音这一步")
    items = pool_list()
    if not items:
        raise RuntimeError("素材库是空的，先在「素材」里加入素材")

    v = cfg["video"]
    target_shot = float(v.get("shot") or 2.2)
    min_shot = float(v.get("min_shot") or 1.0)
    max_shot = float(v.get("max_shot") or 4.5)
    total = float(tts.get("total") or sents[-1]["end"])

    usage: dict[str, int] = {}
    clips: list[dict] = []
    last = None
    base_seed = hash(script.get("title", "")) & 0xFFFF
    rnd = random.Random(base_seed if seed is None else (base_seed ^ (int(seed) * 7919)) & 0x7FFFFFFF)

    for si, s in enumerate(sents):
        st, en = float(s["start"]), float(s["end"])
        if si + 1 < len(sents):
            en = min(en, float(sents[si + 1]["start"]))
        en = max(st + 0.35, en)
        dur = en - st
        n = max(1, int(round(dur / target_shot)))
        while dur / n > max_shot and n < 14:
            n += 1
        while n > 1 and dur / n < min_shot:
            n -= 1
        seg = dur / n
        # 配音时间轴里有 hint 就用它；老项目没存，按文本回脚本句子里找
        keys = _keys(s.get("hint") or _hint_of(script, si, s.get("text", "")))
        picks = _candidates(items, keys)
        first = True
        used_sem: set = set()
        for k in range(n):
            pick = None
            best = -1e9
            for it in picks:
                sc = _score(it, keys, usage, seg, last, used_sem)
                sc += rnd.random() * 0.35
                if sc > best:
                    best, pick = sc, it
            if pick is None:
                break
            usage[pick["id"]] = usage.get(pick["id"], 0) + 1
            used_sem |= _scene_tags(pick)
            avail = pick["dur"]
            if avail >= seg:
                room = avail - seg
                ss = round(pick["start"] + (rnd.uniform(0, room) if room > 0.25 else room / 2), 3)
                src = round(seg, 3)
            else:
                ss = round(pick["start"], 3)
                src = round(avail, 3)
            clips.append({
                "sentence": si, "text": s["text"], "start": round(st + k * seg, 3),
                "dur": round(seg, 3), "kind": "video", "path": pick["path"],
                "ss": ss, "src": src, "want": round(seg, 3),
                "item_id": pick["id"], "item_name": pick["name"],
                "tags": pick.get("tags") or [], "first": first,
            })
            last = pick
            first = False

    # 时长校准：按绝对时间轴重算，末尾不足的部分并进最后一镜
    acc = 0.0
    for c in clips:
        c["start"] = round(acc, 3)
        acc += c["dur"]
    diff = round(total - acc, 3)
    if clips and abs(diff) > 0.02:
        if diff > 0:
            clips[-1]["dur"] = round(clips[-1]["dur"] + diff, 3)
            clips[-1]["want"] = clips[-1]["dur"]
        else:
            while clips and -diff > 0.05 and clips[-1]["dur"] + diff > 0.3:
                clips[-1]["dur"] = round(clips[-1]["dur"] + diff, 3)
                clips[-1]["want"] = clips[-1]["dur"]
                break

    plan = {
        "clips": clips, "total": round(sum(c["dur"] for c in clips), 3),
        "audio": tts.get("audio"), "shot": target_shot,
        "stats": {
            "clips": len(clips), "materials": len({c["item_id"] for c in clips}),
            "reuse": max(usage.values()) if usage else 0,
            "pool": len(items),
        },
        "at": time.time(),
    }
    script["plan"] = plan
    return plan


def swap_clip(project: dict, script: dict, clip_index: int, cfg: dict) -> dict:
    """把某个镜头换成候选里的下一个。"""
    plan = script.get("plan") or {}
    clips = plan.get("clips") or []
    if not (0 <= clip_index < len(clips)):
        raise RuntimeError("镜头序号越界")
    cur = clips[clip_index]
    items = pool_list()
    keys = _keys(_hint_of(script, cur.get("sentence", 0), cur.get("text", "")))
    ranked = sorted(items, key=lambda it: -_score(it, keys, {}, cur["want"], None))
    ids = [it["id"] for it in ranked]
    try:
        pos = ids.index(cur["item_id"])
    except ValueError:
        pos = -1
    nxt = ranked[(pos + 1) % len(ranked)] if ranked else None
    if not nxt:
        raise RuntimeError("没有别的素材可换")
    avail = nxt["dur"]
    seg = cur["want"]
    if avail >= seg:
        cur["ss"] = round(nxt["start"] + max(0.0, (avail - seg) / 2), 3)
        cur["src"] = round(seg, 3)
    else:
        cur["ss"] = round(nxt["start"], 3)
        cur["src"] = round(avail, 3)
    cur["path"], cur["item_id"], cur["item_name"] = nxt["path"], nxt["id"], nxt["name"]
    cur["tags"] = nxt.get("tags") or []
    script["plan"] = plan
    return cur


def _hint_of(script: dict, si: int, text: str = "") -> str:
    """取某一镜对应的画面提示。

    配音时间轴里的句子序号和脚本句子序号可能已经错位（删句、空句被跳过都会），
    所以先按文本原样匹配，匹配不到再退回按序号取。
    """
    sents = script.get("sentences") or []
    want = (text or "").strip()
    if want:
        for s in sents:
            if (s.get("text") or "").strip() == want:
                return str(s.get("hint") or "").strip()
    try:
        return str(sents[si].get("hint") or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- ⑤ 合成

# ---------------------------------------------------------------- BGM 库

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac", ".wma", ".opus"}
_BGM_DUR: dict = {}


def bgm_dirs(cfg: dict | None = None) -> list[str]:
    """配置里登记的音乐目录（允许多个，也允许是一个用换行/分号分隔的字符串）。"""
    cfg = cfg or load_cfg()
    raw = cfg["video"].get("bgm_dirs") or []
    if isinstance(raw, str):
        raw = re.split(r"[\r\n;]+", raw)
    out = []
    for d in raw:
        d = str(d).strip().strip('"').strip("'")
        if d and d not in out:
            out.append(d)
    return out


def bgm_list(cfg: dict | None = None) -> list[dict]:
    """扫 BGM 目录列曲目；时长按 (路径, 大小) 缓存，刷新时不用每个都重新解析。"""
    cfg = cfg or load_cfg()
    items, seen = [], set()
    for d in bgm_dirs(cfg):
        root = Path(d)
        if not root.is_dir():
            continue
        try:
            files = list(root.rglob("*"))
        except OSError:
            continue
        for f in files:
            try:
                if not (f.is_file() and f.suffix.lower() in AUDIO_EXT):
                    continue
                st = f.stat()
                key = str(f.resolve()).lower()
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            ck = (key, st.st_size)
            dur = _BGM_DUR.get(ck)
            if dur is None:
                try:
                    dur = media_duration(f)
                except Exception:
                    dur = 0.0
                _BGM_DUR[ck] = dur
            items.append({"path": str(f), "name": f.name, "dir": str(root),
                          "size": st.st_size, "dur": round(dur, 2)})
    items.sort(key=lambda x: (x["name"].lower(), x["path"].lower()))
    return items


def bgm_pick(cfg: dict | None = None) -> str:
    """按配置决定这条成片用哪首 BGM，返回路径（空字符串 = 不用）。"""
    cfg = cfg or load_cfg()
    v = cfg["video"]
    mode = (v.get("bgm_mode") or "one").strip()
    cur = (v.get("bgm") or "").strip()
    if mode == "none":
        return ""
    if mode == "random":
        items = bgm_list(cfg)
        if items:
            return random.choice(items)["path"]
        return cur if cur and Path(cur).exists() else ""
    if cur and Path(cur).exists():
        return cur
    items = bgm_list(cfg)          # 选了库但没指具体曲目，退化成第一首，别让人以为没生效
    return items[0]["path"] if items else ""


def _ass_escape(t: str) -> str:
    return t.replace("{", "（").replace("}", "）").replace("\\", "／")


def _wrap(text: str, per_line: int = 13) -> str:
    text = _ass_escape(text)
    lines = [text[i:i + per_line] for i in range(0, len(text), per_line)] or [text]
    return r"\N".join(lines)


def _tc(sec: float) -> str:
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def build_ass(project: dict, script: dict, cfg: dict) -> Path:
    v = cfg["video"]
    W, H = int(v["w"]), int(v["h"])
    size = int(v.get("sub_size") or 62)
    font = v.get("sub_font") or "Microsoft YaHei"
    mv = int(H * float(v.get("sub_margin") or 0.17))
    outline = float(v.get("sub_outline") or 3.0)
    out = project_dir(project["id"]) / ("sub_%d.ass" % int(script.get("idx", 0)))
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: %d
PlayResY: %d
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,%s,%d,&H00FFFFFF,&H000000FF,&H00141414,&HA0000000,-1,0,0,0,100,100,0.5,0,1,%.1f,1.4,2,60,60,%d,1
Style: Hook,%s,%d,&H0000E1FF,&H000000FF,&H00141414,&HA0000000,-1,0,0,0,100,100,1.2,0,1,%.1f,2.0,8,60,60,%d,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" % (W, H, font, size, outline, mv, font, int(size * 1.1), outline + 0.6, int(H * 0.10))
    rows = []
    for s in (script.get("tts") or {}).get("sentences") or []:
        txt = _wrap(s["text"], 13 if W <= 1080 else 15)
        rows.append("Dialogue: 0,%s,%s,Sub,,0,0,0,,%s" % (_tc(s["start"]), _tc(s["end"]), txt))
    if v.get("hook"):
        hook_txt = str(script.get("title") or "")
        if not hook_txt:
            pname = str((project.get("product") or {}).get("name") or "")
            hook_txt = "｜".join(x for x in (pname, script.get("angle_label") or "") if x)
        if hook_txt:
            rows.insert(0, "Dialogue: 1,%s,%s,Hook,,0,0,0,,%s" % (
                _tc(0), _tc(3.2), _wrap(hook_txt[:24], 13)))
    out.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    return out


def _voice_ok(script: dict) -> bool:
    a = (script.get("tts") or {}).get("audio")
    try:
        return bool(a) and Path(a).exists()
    except OSError:
        return False


def compose_worker(project: dict, script: dict, cfg: dict, job: dict, variant: str = "") -> None:
    v = cfg["video"]
    W, H, FPS = int(v["w"]), int(v["h"]), int(v["fps"])
    plan = script.get("plan") or {}
    clips = plan.get("clips") or []
    audio = plan.get("audio") or (script.get("tts") or {}).get("audio")
    if not clips:
        raise RuntimeError("还没有分镜，先做对齐")
    if not _voice_ok(script):
        # 配音文件被删了（例如「删除成片」连带清理了 audio_N）——这里自动补一次，
        # 不要让人看到 ffmpeg 的 "No such file or directory"
        job["message"] = "配音文件不见了，重新配音…"
        tts_worker(project, script, cfg, {"message": "", "progress": 0.0, "total": 1})
        audio = (script.get("tts") or {}).get("audio")
        plan["audio"] = audio
        script["plan"] = plan
    if not audio:
        raise RuntimeError("还没有配音，先做对齐")
    pdir = project_dir(project["id"])
    ass = build_ass(project, script, cfg) if v.get("subtitle", True) else None
    ass_name = ass.name if ass else None

    total = round(sum(float(c["dur"]) for c in clips), 3)
    job["total"] = len(clips)
    job["message"] = "准备合成 %d 个镜头 / %.1f 秒" % (len(clips), total)

    inputs: list[str] = []
    chains: list[str] = []
    fit = "increase" if (v.get("fit") or "cover") == "cover" else "decrease"
    for i, c in enumerate(clips):
        want = round(float(c["dur"]), 3)
        src = round(float(c.get("src") or want), 3)
        ss = max(0.0, float(c.get("ss") or 0.0))
        inputs += ["-ss", "%.3f" % ss, "-t", "%.3f" % max(0.1, src + 0.25), "-i", str(Path(c["path"]).resolve())]
        slow = 1.0
        if src > 0.05 and want > src * 1.02:
            slow = min(1.32, want / src)
        chain = ("[%d:v]setpts=PTS-STARTPTS,fps=%d,"
                 "scale=%d:%d:force_original_aspect_ratio=%s,"
                 "crop=%d:%d,setsar=1" % (i, FPS, W, H, fit, W, H))
        # 一键出片自动抹掉源片烧进去的硬字幕（delogo，实时速度）。
        # 配在 pipeline.json 的 director.delogo，格式 x:y:w:h，按**源片分辨率**给；
        # 没配就是空串，下面这段等于不存在，不影响原来的五步流程。
        _dl = str(((cfg.get("director") or {}).get("delogo") or "")).strip()
        if _dl:
            # 放在 scale+crop 之后：这样框的坐标永远按输出画布算，
            # 素材分辨率不一时才不会报 "Logo area is outside of the frame"。
            chain = chain.replace(",setsar=1", ",setsar=1,delogo=%s" % _dl, 1)
        if slow > 1.02:
            chain += ",setpts=PTS*%.4f,fps=%d" % (slow, FPS)
        chain += ",tpad=stop_mode=clone:stop_duration=%.3f" % max(0.0, want - src * slow + 0.2)
        chain += ",trim=duration=%.3f,setpts=PTS-STARTPTS,format=yuv420p[v%d]" % (want, i)
        chains.append(chain)

    ai = len(clips)
    inputs += ["-i", str(Path(audio).resolve())]
    bgm = ""
    try:
        bgm = bgm_pick(cfg)
    except Exception:
        bgm = (v.get("bgm") or "").strip()
    if not (bgm and Path(bgm).exists()):
        bgm = ""
    if bgm:
        inputs += ["-stream_loop", "-1", "-i", bgm]

    cat = "".join("[v%d]" % i for i in range(len(clips)))
    filt = chains + [
        "%sconcat=n=%d:v=1:a=0[vcat]" % (cat, len(clips)),
    ]
    if ass_name:
        filt.append("[vcat]ass=%s[vout]" % ass_name)
        vmap = "[vout]"
    else:
        vmap = "[vcat]"

    if bgm:
        vol = float(v.get("bgm_volume") or 0.1)
        fade = max(0.0, min(5.0, float(v.get("bgm_fade") or 0)))
        chain = "[%d:a]volume=%.3f,atrim=0:%.3f,asetpts=N/SR/TB" % (ai + 1, vol, total)
        if fade > 0.05 and total > fade * 2:
            chain += ",afade=t=in:st=0:d=%.2f" % fade
            chain += ",afade=t=out:st=%.2f:d=%.2f" % (total - fade, fade)
        filt.append(chain + "[bg]")
        filt.append("[%d:a]volume=1.0[voice]" % ai)
        # normalize=0：不让 amix 把口播也砍半（默认 normalize=1 会按输入数均分音量）
        filt.append("[voice][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
                    "alimiter=limit=0.95[aout]")
    else:
        filt.append("[%d:a]alimiter=limit=0.95[aout]" % ai)

    # 文件名前缀优先用当前品名：换品名重新写稿后，新成片名跟着变，
    # 不再一直是建项目时那次填的名字
    disp = ((project.get("product") or {}).get("name")
            or project.get("name") or "成片")
    out = _unique_out("%s_%s%s_%s.mp4" % (_safe(disp),
                                          _safe(script.get("angle_label") or "脚本"),
                                          ("_" + _safe(variant)) if variant else "",
                                          time.strftime("%m%d_%H%M%S")))
    job_gate(job)
    job["message"] = "渲染中…"
    args = (["-v", "error", "-y"] + inputs +
            ["-filter_complex", ";".join(filt),
             "-map", vmap, "-map", "[aout]",
             "-t", "%.3f" % total,
             "-c:v", "libx264", "-preset", str(v.get("preset") or "veryfast"),
             "-crf", str(int(v.get("crf") or 21)), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
             "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", str(out)])
    proc = subprocess.Popen([FFMPEG] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, errors="replace", bufsize=1, cwd=str(pdir),
                            creationflags=NO_WINDOW)
    assert proc.stdout is not None
    for line in proc.stdout:
        if job.get("cancel"):
            try:
                proc.kill()
            except Exception:
                pass
            try:
                out.unlink(missing_ok=True)     # 别留下半截成片
            except Exception:
                pass
            raise JobCancelled()
        line = line.strip()
        if line.startswith("out_time_us=") and total > 0:
            try:
                t = int(line.split("=", 1)[1]) / 1_000_000.0
            except ValueError:
                continue
            job["progress"] = min(0.99, t / total)
            job["done"] = min(len(clips), int(job["progress"] * len(clips)))
            job["message"] = "渲染 %d%%　%s" % (int(job["progress"] * 100), "%.1f/%.1f 秒" % (t, total))
    code = proc.wait()
    if code != 0 or not out.exists():
        err = (proc.stderr.read() if proc.stderr else "") or "退出码 %d" % code
        raise RuntimeError("ffmpeg 失败：%s" % _short(err, 400))

    d = media_duration(out)
    size = out.stat().st_size
    result = {
        "out": str(out), "name": out.name, "dur": round(d, 2), "clips": len(clips),
        "size": size, "kbps": _kbps(size, d),
        "shots_csv": str(_write_shotlist(project, script)),
        "ass": str(ass) if ass else "",
        "bgm": Path(bgm).name if bgm else "",
    }
    try:  # 同目录放一份元数据，重启后「最近成片」也能显示时长
        out.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    job["result"] = result
    job["message"] = "完成：%s（%.1f 秒，%s）" % (out.name, d, _mb(out.stat().st_size))
    job["state"] = "done"
    job["progress"] = 1.0


def _mb(n: int) -> str:
    return "%.1f MB" % (n / 1048576) if n > 1048576 else "%.0f KB" % (n / 1024)


# ---------------------------------------------------------------- ⑥ 批量出片

class _SubJob(dict):
    """子步骤（配音/合成）会往 job 里写 progress 和 message，
    包一层转发到批量任务当前那一段进度里。"""

    def __init__(self, on_progress, on_message=None):
        super().__init__()
        self._on_p = on_progress
        self._on_m = on_message

    def __setitem__(self, k, v):
        super().__setitem__(k, v)
        try:
            if k == "progress" and self._on_p:
                self._on_p(float(v))
            elif k == "message" and self._on_m:
                self._on_m(str(v))
        except Exception:
            pass


def batch_worker(project: dict, idxs: list, cfg: dict, job: dict,
                 opts: dict | None = None) -> None:
    """把选中的脚本一条条走完 配音 → 分镜 → 合成，一次出多条成片。

    opts: variants 每个脚本出几条（重新随机分镜）、redo_tts 重做配音、
          realign 强制重新分镜（默认 True；已有分镜且没重配音时可选 False 保留手调结果）
    """
    opts = opts or {}
    scripts = project.get("scripts") or []
    if not scripts:
        raise RuntimeError("还没有脚本，先在第 2 步生成脚本")

    want = []
    for x in (idxs or []):
        try:
            want.append(int(x))
        except (TypeError, ValueError):
            continue
    valid = [i for i in want if 0 <= i < len(scripts)] or list(range(len(scripts)))

    variants = max(1, min(20, int(opts.get("variants") or 1)))
    redo_tts = bool(opts.get("redo_tts"))
    realign = bool(opts.get("realign", True))
    unit_total = len(valid) * variants
    job["total"] = unit_total
    job["done"] = 0
    job["results"] = []
    made, failed = [], []
    done = 0
    stop = False

    for k, i in enumerate(valid):
        s = scripts[i]
        label = s.get("angle_label") or ("脚本 %d" % (i + 1))
        sb, sp = k / float(len(valid)), 1.0 / float(len(valid))

        def head(frac, msg, k=k, label=label, sb=sb, sp=sp):
            job["progress"] = min(0.995, sb + sp * max(0.0, min(1.0, frac)))
            job["message"] = "[%d/%d 脚本 · %s] %s" % (k + 1, len(valid), label, msg)

        # 配音：每条脚本只做一次（第一个版本之前）
        try:
            if redo_tts or not _voice_ok(s):
                head(0.01, "配音中…")
                tts_worker(project, s, cfg,
                           _SubJob(lambda f: head(0.01 + 0.14 * f, "配音中…")))
                head(0.15, "配音 %.1f 秒" % float((s.get("tts") or {}).get("total") or 0))
        except Exception as exc:  # noqa: BLE001
            for v in range(variants):
                failed.append({"idx": i, "angle": label, "variant": v + 1,
                               "stage": "配音", "error": _short(str(exc), 200)})
                done += 1
            job["done"] = done
            continue

        vspan = 0.85 / variants
        if stop:
            break
        for v in range(variants):
            try:
                job_gate(job)
            except JobCancelled:
                stop = True
                break
            vbase = 0.15 + vspan * v
            tag = ("V%d" % (v + 1)) if variants > 1 else ""

            def vprog(frac, msg, v=v, vbase=vbase, label=label, tag=tag, k=k):
                job["progress"] = min(0.995, sb + sp * (vbase + vspan * max(0.0, min(1.0, frac))))
                job["message"] = "[%d/%d 脚本 · %s%s] %s" % (
                    k + 1, len(valid), label, (" " + tag) if tag else "", msg)

            try:
                has_plan = bool((s.get("plan") or {}).get("clips"))
                if realign or v > 0 or not has_plan:
                    vprog(0.02, "选镜头…")
                    align(project, s, cfg, seed=None if v == 0 else (i + 1) * 100 + v)
                    project_save(project)
                n_clip = len((s.get("plan") or {}).get("clips") or [])
                vprog(0.28, "渲染 %d 个镜头…" % n_clip)
                sub = _SubJob(lambda f: vprog(0.28 + 0.72 * f, "渲染 %d%%" % int(f * 100)))
                compose_worker(project, s, cfg, sub, variant=tag)
                r = sub.get("result") or {}
                made.append({"idx": i, "angle": label, "variant": v + 1,
                             "name": r.get("name"), "out": r.get("out"), "dur": r.get("dur"),
                             "size": r.get("size"), "clips": r.get("clips"),
                             "bgm": r.get("bgm")})
                if r.get("name"):
                    s.setdefault("outs", []).append(r["name"])
                    project_save(project)
                job["results"] = list(made)
                vprog(1.0, "完成 %s" % (r.get("name") or ""))
            except Exception as exc:  # noqa: BLE001
                failed.append({"idx": i, "angle": label, "variant": v + 1,
                               "stage": "合成", "error": _short(str(exc), 200)})
            done += 1
            job["done"] = done

    job["batch"] = {"made": len(made), "failed": len(failed),
                    "variants": variants, "scripts": len(valid), "failures": failed}
    job["results"] = made
    if stop:
        job["message"] = "已停止：已出 %d 条，其余没做" % len(made)
        job["state"] = "cancel"
        return
    if not made:
        raise RuntimeError("一条都没出来：%s" % (failed[0]["error"] if failed else "未知原因"))
    job["message"] = "批量完成：%d 条成片，%d 条失败（%d 个脚本 × 每脚本 %d 版）" % (
        len(made), len(failed), len(valid), variants)
    job["state"] = "done"
    job["progress"] = 1.0



def _unique_out(filename: str) -> Path:
    """批量合成时同一秒可能撞名，撞了就加 _2 _3，不要覆盖已渲染的成片。"""
    p = OUT / filename
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    for n in range(2, 200):
        q = OUT / ("%s_%d%s" % (stem, n, suf))
        if not q.exists():
            return q
    return OUT / ("%s_%d%s" % (stem, int(time.time() * 1000) % 100000, suf))


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s or "")[:28] or "成片"


def _write_shotlist(project: dict, script: dict) -> Path:
    p = project_dir(project["id"]) / ("shotlist_%d.csv" % int(script.get("idx", 0)))
    rows = ["序号,起,止,时长,口播,素材,标签"]
    for i, c in enumerate((script.get("plan") or {}).get("clips") or [], 1):
        rows.append("%d,%.2f,%.2f,%.2f,%s,%s,%s" % (
            i, c["start"], c["start"] + c["dur"], c["dur"],
            '"%s"' % c["text"].replace('"', "''"),
            '"%s"' % c["item_name"], '"%s"' % "/".join(c.get("tags") or [])[:60]))
    p.write_text("\ufeff" + "\n".join(rows), encoding="utf-8")
    return p


# ---------------------------------------------------------------- 项目存储

def project_dir(pid: str) -> Path:
    d = PIPE / "projects" / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_path(pid: str) -> Path:
    return project_dir(pid) / "project.json"


def project_save(p: dict) -> dict:
    with _lock:
        p["updated"] = time.time()
        project_path(p["id"]).write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def project_load(pid: str) -> dict | None:
    f = project_path(pid)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def project_new(name: str = "") -> dict:
    pid = uuid.uuid4().hex[:10]
    p = {
        "id": pid,
        "name": name or ("成片 " + time.strftime("%m-%d %H:%M")),
        "created": time.time(), "product": {}, "steps": {},
        "scripts": [], "active": 0,
    }
    project_save(p)
    return p


def project_list() -> list[dict]:
    out = []
    root = PIPE / "projects"
    if not root.exists():
        return out
    for d in sorted(root.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        f = d / "project.json"
        if not f.exists():
            continue
        try:
            p = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        scripts = p.get("scripts") or []
        out.append({
            "id": p["id"], "name": p.get("name"), "updated": p.get("updated"),
            "scripts": len(scripts),
            "has_tts": [bool(s.get("tts")) for s in scripts],
            "has_plan": [bool(s.get("plan")) for s in scripts],
        })
    return out


def project_delete(pid: str) -> bool:
    d = PIPE / "projects" / pid
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


# ---------------------------------------------------------------- 任务包装

def spawn(kind: str, fn, *args, **kwargs) -> dict:
    """由 app.py 注入的 job 工厂；这里只做兜底定义。"""
    raise RuntimeError("spawn 未初始化")
