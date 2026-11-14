# -*- coding: utf-8 -*-
"""成片体检：黑边 / 字幕溢出 / 静音断档 / 音量 / 首尾黑场 / 码率。

思路跟设计类 skill 里的 svg_audit 一样：规范不写在文档里，而是能被检查出来。
渲染完自动跑一遍（进缓存），成片列表里直接看到分数，点开能看每一项为什么扣分。

缓存：_work/pipe/qc/<md5(文件名)>.json，用 mtime + size 判失效。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

# 打分权重：一条 bad 扣 22，一条 warn 扣 8
W_BAD, W_WARN = 22, 8


def _find_ffmpeg() -> str | None:
    root = Path(__file__).resolve().parent
    for c in (root / ".venv" / "Scripts" / "ffmpeg.exe", root / ".venv" / "Scripts" / "ffmpeg"):
        if c.exists():
            return str(c)
    import shutil
    return shutil.which("ffmpeg")


def _pipe():
    import pipeline          # 延迟导入：pipeline.configure() 之后才有 FFMPEG
    if not getattr(pipeline, "FFMPEG", None):        # 命令行直接跑 qc.py 时也能用
        f = _find_ffmpeg()
        if f:
            pipeline.configure(f, 0x08000000 if os.name == "nt" else 0)
    return pipeline


# ---------------------------------------------------------------- 缓存

def cache_dir() -> Path:
    d = Path(_pipe().PIPE) / "qc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_file(path: Path) -> Path:
    h = hashlib.md5(path.name.encode("utf-8")).hexdigest()[:12]
    return cache_dir() / (h + ".json")


def _read_cache(path: Path) -> dict | None:
    f = _cache_file(path)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(path: Path, qc: dict) -> None:
    try:
        st = path.stat()
        _cache_file(path).write_text(json.dumps(
            {"name": path.name, "out": str(path), "mtime": st.st_mtime,
             "size": st.st_size, "qc": qc}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def cached(path: str | Path) -> dict | None:
    """只读缓存，不跑 ffmpeg。列表页用它出徽标。"""
    p = Path(path)
    c = _read_cache(p)
    if not c:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    if c.get("size") != st.st_size or abs(float(c.get("mtime") or 0) - st.st_mtime) > 1:
        return None
    return c.get("qc")


def digest(path: str | Path) -> dict | None:
    qc = cached(path)
    if not qc:
        return None
    return {"score": qc.get("score"), "level": qc.get("level"),
            "warn": qc.get("warn"), "bad": qc.get("bad")}


# ---------------------------------------------------------------- 流信息

def _streams(path: Path) -> dict:
    """一条 ffmpeg -i 拿到时长 / 分辨率 / 帧率 / 音轨，不额外解码。"""
    pipe = _pipe()
    info = {"dur": 0.0, "w": 0, "h": 0, "fps": 0.0, "audio": False, "sr": 0, "ch": 0}
    try:
        r = pipe.run(["-hide_banner", "-i", str(path)], timeout=60)
    except Exception:
        return info
    txt = (r.stderr or "") + (r.stdout or "")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", txt)
    if m:
        info["dur"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"Stream #\d+:\d+.*?: Video:.*?,\s*(\d{2,5})x(\d{2,5})", txt)
    if m:
        info["w"], info["h"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s+fps", txt)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"Stream #\d+:\d+.*?: Audio:.*?,\s*(\d+)\s*Hz,\s*([^,\n]+)", txt)
    if m:
        info["audio"] = True
        info["sr"] = int(m.group(1))
        info["ch"] = m.group(2).strip()
    return info


# ---------------------------------------------------------------- 画面：黑边 + 黑场

def _picture_pass(path: Path) -> tuple[tuple, list]:
    """一次解码同时跑 cropdetect（黑边）和 blackdetect（首尾黑场）。"""
    pipe = _pipe()
    args = ["-hide_banner", "-nostats", "-i", str(path), "-an",
            "-vf", "cropdetect=limit=24:round=2:reset=0,blackdetect=d=0.12:pix_th=0.10",
            "-f", "null", "-"]
    try:
        r = pipe.run(args, timeout=420)
    except Exception:
        return (0, 0, 0, 0), []
    txt = (r.stderr or "") + (r.stdout or "")
    crops = [(int(a), int(b), int(c), int(d))
             for a, b, c, d in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", txt)]
    crops = [c for c in crops if c[0] >= 32 and c[1] >= 32]      # 起手那几帧是 0:0:0:0
    main = Counter(crops).most_common(1)[0][0] if crops else (0, 0, 0, 0)
    blacks = []
    for a, b, d in re.findall(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)", txt):
        blacks.append((float(a), float(b), float(d)))
    return main, blacks


# ---------------------------------------------------------------- 声音：响度 + 静音

def _audio_pass(path: Path) -> dict:
    """一次解码同时跑 silencedetect（静音段）和 loudnorm（响度/峰值）。"""
    pipe = _pipe()
    args = ["-hide_banner", "-nostats", "-i", str(path), "-vn",
            "-af", "silencedetect=noise=-35dB:d=0.35,loudnorm=print_format=json",
            "-f", "null", "-"]
    out = {"lufs": None, "tp": None, "lra": None, "silences": []}
    try:
        r = pipe.run(args, timeout=420)
    except Exception:
        return out
    txt = (r.stderr or "") + (r.stdout or "")
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", txt)
    if blocks:
        try:
            d = json.loads(blocks[-1])
            for k, key in (("input_i", "lufs"), ("input_tp", "tp"), ("input_lra", "lra")):
                v = str(d.get(k, "")).strip()
                if v and v.lower() not in ("-inf", "inf", "nan"):
                    out[key] = float(v)
        except Exception:
            pass
    cur = None
    for line in txt.splitlines():
        m = re.search(r"silence_start:\s*(-?[\d.]+)", line)
        if m:
            cur = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*(-?[\d.]+)", line)
        if m and cur is not None:
            out["silences"].append((max(0.0, cur), float(m.group(1)), float(m.group(1)) - cur))
            cur = None
    if cur is not None:                       # 一直静到片尾
        out["silences"].append((max(0.0, cur), None, None))
    return out


# ---------------------------------------------------------------- 字幕

def _text_px(s: str, size: float) -> float:
    """估一行字在 ASS 里的宽度（中文字宽=1em，ASCII≈0.55em）。"""
    w = 0.0
    for ch in s:
        o = ord(ch)
        if o < 128:
            w += size * (0.30 if ch == " " else 0.55)
        elif 0x4E00 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFF60:
            w += size
        else:
            w += size * 0.72
    return w


def _sec(tc: str) -> float:
    m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", tc.strip())
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def _ass_scan(ass_path: str | Path, W: int, H: int) -> dict | None:
    p = Path(ass_path)
    if not p.exists():
        return None
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    styles: dict[str, dict] = {}
    for line in txt.splitlines():
        if not line.startswith("Style:"):
            continue
        f = line[6:].split(",")
        if len(f) < 22:
            continue
        try:
            styles[f[0].strip()] = {
                "size": float(f[2]), "outline": float(f[16]),
                "ml": float(f[19]), "mr": float(f[20]), "mv": float(f[21]),
                "align": f[18].strip(),
            }
        except ValueError:
            continue
    rows = []
    for line in txt.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        f = line[9:].split(",", 9)
        if len(f) < 10:
            continue
        rows.append({"start": _sec(f[1]), "end": _sec(f[2]),
                     "style": f[3].strip(), "text": f[9]})
    worst = {"over": 0.0, "line": "", "style": ""}
    longest = 0.0
    lines_max = 1
    flash = 0
    for r in rows:
        st = styles.get(r["style"]) or styles.get("Sub") or {"size": 62, "outline": 3,
                                                            "ml": 60, "mr": 60, "mv": 0, "align": "2"}
        avail = max(1.0, W - st["ml"] - st["mr"] - 2 * st["outline"])
        body = re.sub(r"\{[^}]*\}", "", r["text"])
        parts = [x for x in re.split(r"\\N|\\n", body)]
        lines_max = max(lines_max, len(parts))
        for seg in parts:
            w = _text_px(seg, st["size"])
            longest = max(longest, w)
            if w > avail and w > worst["over"]:
                worst = {"over": w, "line": seg, "style": r["style"], "avail": avail}
        if 0 < (r["end"] - r["start"]) < 0.7:
            flash += 1
    # 句间空档（BGM 会盖住静音检测，这里用字幕间隔判断有没有断档）
    gaps = []
    rows.sort(key=lambda x: x["start"])
    for a, b in zip(rows, rows[1:]):
        g = b["start"] - a["end"]
        if g > 0.4:
            gaps.append((a["end"], b["start"], g))
    head = rows[0]["start"] if rows else 0.0
    tail = rows[-1]["end"] if rows else 0.0
    return {"count": len(rows), "worst": worst, "longest": longest,
            "lines_max": lines_max, "flash": flash, "gaps": gaps,
            "head": head, "tail": tail, "styles": styles}


# ---------------------------------------------------------------- 组装

def _it(key, label, level, msg, hint="", value=""):
    return {"key": key, "label": label, "level": level,
            "msg": msg, "hint": hint, "value": value}


def _audit_uncached(path: Path, cfg: dict | None) -> dict:
    cfg = cfg or _pipe().load_cfg()
    v = cfg.get("video") or {}
    info = _streams(path)
    dur = info["dur"] or _pipe().media_duration(path)
    W, H = info["w"], info["h"]
    items: list[dict] = []
    metrics = {"dur": round(dur, 2), "w": W, "h": H, "fps": round(info["fps"], 2)}
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    kbps = _pipe()._kbps(size, dur)
    metrics["kbps"] = kbps
    metrics["size"] = size
    metrics["audio"] = info["audio"]

    # 1) 分辨率 / 帧率
    ew, eh = int(v.get("w") or 1080), int(v.get("h") or 1920)
    if W and (W, H) != (ew, eh):
        items.append(_it("res", "分辨率", "warn",
                         f"成片是 {W}×{H}，配置是 {ew}×{eh}",
                         "平台对竖屏有固定尺寸要求；多半是改了配置没重渲染", f"{W}×{H}"))
    else:
        items.append(_it("res", "分辨率", "ok", f"{W}×{H}，和配置一致", "", f"{W}×{H}"))

    # 2) 音轨
    if not info["audio"]:
        items.append(_it("audio", "音轨", "bad", "成片里没有音轨", "带货片没声音等于废片，重新渲染"))
    else:
        items.append(_it("audio", "音轨", "ok", f"{info['sr']}Hz {info['ch']}", "",
                         f"{info['sr']}Hz"))

    # 3) 黑边
    crop, blacks = _picture_pass(path)
    if W and H and crop[0]:
        cw, ch, cx, cy = crop
        lost_w = max(0.0, (W - cw) / W)
        lost_h = max(0.0, (H - ch) / H)
        if lost_w > 0.02 or lost_h > 0.02:
            side = []
            if lost_w > 0.02:
                side.append("左右各 %.0f%%" % (lost_w * 50))
            if lost_h > 0.02:
                side.append("上下各 %.0f%%" % (lost_h * 50))
            lvl = "bad" if max(lost_w, lost_h) > 0.08 else "warn"
            items.append(_it("bar", "黑边", lvl, "画面周围有黑边：" + "，".join(side),
                             "素材比例和画布不一致，compose 里的 cover 裁切没生效", f"{cw}×{ch}"))
            metrics["bar"] = round(max(lost_w, lost_h) * 100, 1)
        else:
            items.append(_it("bar", "黑边", "ok", "满屏，没有黑边", "", f"{cw}×{ch}"))
            metrics["bar"] = 0
    else:
        items.append(_it("bar", "黑边", "info", "没测出来（cropdetect 无输出）"))

    # 4) 首尾黑场
    if blacks:
        head_black = max([d for a, b, d in blacks if a < 0.2] or [0.0])
        tail_black = max([d for a, b, d in blacks if dur and b > dur - 0.35] or [0.0])
        if head_black > 0.4 or tail_black > 0.4:
            where = "片头 %.1fs" % head_black if head_black > 0.4 else "片尾 %.1fs" % tail_black
            items.append(_it("black", "首尾黑场", "warn", f"{where}是黑画面",
                             "第一个镜头可能没对上素材起点，或者被 delogo 框糊成黑块"))
        else:
            items.append(_it("black", "首尾黑场", "ok", "首尾没有黑场"))
    else:
        items.append(_it("black", "首尾黑场", "ok", "没有黑场"))

    # 5) 音量
    au = _audio_pass(path)
    metrics["lufs"] = au["lufs"]
    metrics["tp"] = au["tp"]
    lufs, tp = au["lufs"], au["tp"]
    if lufs is None:
        items.append(_it("loud", "音量", "info", "没测出响度（音轨可能全静音）"))
    else:
        if lufs < -22:
            lvl, msg = ("bad" if lufs < -28 else "warn"), "整体偏小：%.1f LUFS" % lufs
            hint = "手机外放基本听不清，配音音量或 BGM 压得太狠"
        elif lufs > -12:
            lvl, msg = "warn", "整体偏大：%.1f LUFS" % lufs
            hint = "短视频平台一般 -16 ~ -14 LUFS，太大会被压得更糊"
        else:
            lvl, msg, hint = "ok", "%.1f LUFS，正常区间" % lufs, ""
        if tp is not None and tp > -0.8:
            lvl = "bad" if tp > -0.3 else ("warn" if lvl == "ok" else lvl)
            msg += "；峰值 %.1f dBTP" % tp
            hint = hint or "峰值太高会削波（爆音），合成时降一点或加限幅"
        items.append(_it("loud", "音量", lvl, msg, hint,
                         "%.1f LUFS / %.1f dBTP" % (lufs, tp if tp is not None else 0)))

    # 6) 静音段
    sil = au["silences"]
    metrics["silence"] = round(sum((d or 0) for _, _, d in sil), 2)
    if not info["audio"]:
        items.append(_it("silence", "静音段", "info", "没有音轨，不用查静音"))
        sil = []
    dead_head = dead_tail = 0.0
    inner = []
    for a, b, d in sil:
        if b is None:                                  # 静到片尾
            dead_tail = max(0.0, dur - a)
            continue
        if a <= 0.05:
            dead_head = d
        elif dur and b >= dur - 0.15:
            dead_tail = d
        elif d >= 1.2:
            inner.append((a, b, d))
    if dead_head > 0.8 or dead_tail > 1.0 or inner:
        bits = []
        if dead_head > 0.8:
            bits.append("开头静 %.1fs" % dead_head)
        if dead_tail > 1.0:
            bits.append("结尾静 %.1fs" % dead_tail)
        if inner:
            bits.append("%d 处中间断档（最长 %.1fs）" % (len(inner), max(d for _, _, d in inner)))
        lvl = "bad" if (inner and max(d for _, _, d in inner) > 2.5) else "warn"
        items.append(_it("silence", "静音段", lvl, "；".join(bits),
                         "开头空等最容易掉完播率；结尾拖静音会被平台判「无内容」"))
    else:
        if info["audio"]:
            items.append(_it("silence", "静音段", "ok", "没有明显静音空缺"))

    # 7) 字幕
    ass = ""
    side = path.with_suffix(".json")
    if side.exists():
        try:
            ass = str((json.loads(side.read_text(encoding="utf-8")) or {}).get("ass") or "")
        except Exception:
            ass = ""
    a = _ass_scan(ass, W, H) if ass else None
    if not ass:
        items.append(_it("sub", "字幕", "info", "这条没有字幕文件记录（合成时没烧字幕）"))
    elif not Path(ass).exists():
        items.append(_it("sub", "字幕", "info", "字幕文件已不在（项目记录被清过），没法查字幕",
                         "成片里字幕是烧进去的，画面本身不受影响"))
    elif not a or not a["count"]:
        items.append(_it("sub", "字幕", "warn", "字幕文件是空的", "ass 存在但没有一句 Dialogue"))
    else:
        over = a["worst"]["over"]
        avail = a["worst"].get("avail") or 0
        metrics["sub"] = a["count"]
        if over:
            pct = (over / avail - 1) * 100 if avail else 0
            lvl = "bad" if pct > 8 else "warn"
            items.append(_it("sub", "字幕", lvl,
                             "有行超宽 %.0f%%：「%s」" % (pct, a["worst"]["line"][:16]),
                             "超出安全区会被裁掉（平台右侧还有按钮遮挡），把每行字数调小或字号调低",
                             "%d 字" % len(a["worst"]["line"])))
        else:
            items.append(_it("sub", "字幕", "ok",
                             "%d 句，最长一行 %.0fpx（可用 %.0fpx）" % (a["count"], a["longest"],
                                                                     W - 120),
                             "", "%d 句" % a["count"]))
        # 节奏：首句太晚 / 中间空太久 / 字幕闪太快
        if a["head"] > 1.0:
            items.append(_it("lead", "开场", "warn", "开场 %.1fs 才出第一句字幕" % a["head"],
                             "前 1 秒是黄金位，画面先动、声音要跟上"))
        big = [g for g in a["gaps"] if g[2] >= 1.5]
        if big:
            items.append(_it("gap", "句间空档", "warn",
                             "%d 处空档 ≥1.5s（最长 %.1fs，%.1fs 处）" % (len(big), max(g[2] for g in big),
                                                                     max(big, key=lambda g: g[2])[0]),
                             "观众会以为视频卡了；要么补 BGM 高光，要么把句子往前挪"))
        if a["flash"]:
            items.append(_it("flash", "字幕速度", "warn",
                             "%d 句停留 <0.7s" % a["flash"], "来不及看完，尤其是带数字的句子"))
        if dur and a["tail"] > dur + 0.2:
            items.append(_it("tail", "字幕时长", "warn",
                             "最后一句字幕超出片尾 %.1fs" % (a["tail"] - dur), "会被硬切掉半句"))
        if a["lines_max"] > 3:
            items.append(_it("lines", "字幕行数", "warn", "最多 %d 行" % a["lines_max"],
                             "竖屏字幕超过 3 行就压住画面了"))

    # 8) 码率
    if dur and kbps:
        if kbps < 1000:
            lvl, msg = "bad", "码率只有 %dkbps" % kbps
        elif kbps < 1800:
            lvl, msg = "warn", "码率偏低 %dkbps" % kbps
        else:
            lvl, msg = "ok", "%dkbps" % kbps
        items.append(_it("kbps", "码率", lvl, msg,
                         "1080×1920 建议 2500kbps 以上，太低一放大就糊" if lvl != "ok" else "", "%dkbps" % kbps))

    bad = sum(1 for i in items if i["level"] == "bad")
    warn = sum(1 for i in items if i["level"] == "warn")
    score = max(0, 100 - bad * W_BAD - warn * W_WARN)
    level = "bad" if bad else ("warn" if warn else "ok")
    return {"ok": True, "name": path.name, "out": str(path), "ts": _now(),
            "score": score, "level": level, "bad": bad, "warn": warn,
            "items": items, "metrics": metrics}


def _now() -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M")


def audit(path: str | Path, cfg: dict | None = None, force: bool = False) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))
    if not force:
        c = cached(p)
        if c:
            return c
    qc = _audit_uncached(p, cfg)
    _write_cache(p, qc)
    return qc


def audit_safe(path: str | Path, cfg: dict | None = None, force: bool = False) -> dict:
    try:
        return audit(path, cfg, force)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "name": Path(path).name, "error": str(exc)[:180],
                "score": 0, "level": "warn", "bad": 0, "warn": 0, "items": [], "metrics": {}}


def qc_worker(names: list, job: dict, cfg: dict | None = None, force: bool = True) -> None:
    """批量体检：job 里报进度，单条失败不影响其它条。"""
    pipe = _pipe()
    cfg = cfg or pipe.load_cfg()
    n = max(1, len(names))
    done = 0
    res = []
    for name in names:
        if job.get("cancel"):
            job["state"] = "cancel"
            return
        while job.get("pause") and not job.get("cancel"):
            job["state"] = "paused"
            import time
            time.sleep(0.4)
        job["state"] = "running"
        job["message"] = "[%d/%d] %s" % (done + 1, n, name)
        p = Path(pipe.OUT) / name
        if not p.exists():
            res.append({"ok": False, "name": name, "error": "文件不在了"})
        else:
            res.append(audit_safe(p, cfg, force=force))
        done += 1
        job["done"] = done
        job["progress"] = done / n
    job["result"] = {"items": res}
    job["state"] = "done"
    job["progress"] = 1.0
    ok = [x for x in res if x.get("ok")]
    avg = round(sum(x["score"] for x in ok) / len(ok)) if ok else 0
    job["message"] = "体检完成 %d/%d 条，平均 %.0f 分%s" % (
        len(ok), len(names), avg,
        ("，%d 条有问题" % sum(1 for x in ok if x["level"] != "ok")) if any(x.get("level") != "ok" for x in ok) else "，全部通过")


# ---------------------------------------------------------------- 命令行

def _main(argv: list[str]) -> int:
    import pipeline as pipe
    pipe.configure(None)
    if not argv:
        outs = sorted(Path(pipe.OUT).glob("*.mp4"), key=lambda p: p.stat().st_mtime)[-3:]
        argv = [str(p) for p in outs]
    for a in argv:
        q = audit_safe(a, None, force=True)
        print("\n=== %s  %s 分 (%s)" % (q.get("name"), q.get("score"), q.get("level")))
        for it in q.get("items") or []:
            mark = {"ok": "OK  ", "warn": "WARN", "bad": "BAD ", "info": "·   "}.get(it["level"], "    ")
            print("  %s %-6s %s" % (mark, it["label"], it["msg"]))
        if q.get("error"):
            print("  ! " + q["error"])
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
