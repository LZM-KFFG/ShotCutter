"""一键导演：丢素材 → 出成片。

思路（重要）：不新造一套渲染。
把「从素材里剪出来的人声」做成一条音轨，配上重算过的句子时间轴，
塞进 script["tts"]，就长得和 tts_worker 的产物一模一样。
于是 align / build_ass / compose_worker / batch_worker 全部原样复用，
一键模式只是「第 2 步换了个作者」，不是另一条流水线。

    A 路线（口播素材）  转写 → 按产品挑句 → 剪出人声轨 → 伪装成 tts
    B 路线（空镜素材）  走原来的 视觉打标 → 写稿 → TTS
    混装                先用 probe 分流，再两条路一起编

单独跑一次自检：  .venv\\Scripts\\python.exe director.py --selftest
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pipeline as pipe

ASR_DIR = pipe.PIPE / "asr"
VOICE_NAME = "voice_direct.wav"


def _ensure_ffmpeg() -> None:
    """单独跑（不经过 app.py）时也要能找到 ffmpeg。

    app.py 启动时会调 pipe.configure，这里只是兜底，不覆盖已配置好的值。
    """
    if pipe.FFMPEG:
        return
    cands = [pipe.ROOT / ".venv" / "Scripts" / "ffmpeg.exe",
             pipe.ROOT / ".venv" / "Scripts" / "ffmpeg",
             shutil.which("ffmpeg")]
    for c in cands:
        if c and Path(str(c)).exists():
            pipe.configure(str(c), 0x08000000 if os.name == "nt" else 0)
            return


_ensure_ffmpeg()

# 口播素材的判定线：有人声轨，静音占比不到一半，而且**够长**
AROLL_SPEECH_MIN = 0.45
# 短于这个长度的片段装不下一句完整的话，只能当画面用。
# 不设这条会出事：1~2 秒的镜头切片里 silencedetect 找不到停顿，
# 有声占比算出来是 100%，整池切片全被判成「口播」。
AROLL_MIN_DUR = 6.0
# 句与句之间补的呼吸，太紧会像卡带
GAP = 0.10

SLOTS = ("hook", "pain", "sell", "proof", "cta")
SLOT_LABEL = {
    "hook": "开头钩子", "pain": "痛点", "sell": "卖点",
    "proof": "信任/证明", "cta": "行动号召",
}

CTA_WORDS = ("点下面", "链接", "下单", "购物车", "小黄车", "橱窗", "拍一件", "入手", "带走")
PAIN_WORDS = ("是不是", "总是", "每次", "老是", "烦", "踩坑", "后悔", "浪费", "难用")
PROOF_WORDS = ("用了", "半年", "一年", "实测", "自己", "回购", "回头客", "卖了", "评价")


# ---------------------------------------------------------------- 素材体检

def speech_ratio(path: str, dur: float | None = None, thr: int = -32,
                 min_sil: float = 0.35) -> float:
    """有声音的时长占比。用 silencedetect 量静音，剩下的都是有声段。

    比算音量均值靠谱：空镜常常有环境噪声（音量不低），但全是连续声，
    真正能区分「有人说话」的是「有没有停顿」。
    """
    dur = dur if dur is not None else pipe.media_duration(path)
    if dur <= 0.2:
        return 0.0
    args = [str(Path(path).resolve()), "-af",
            "silencedetect=noise=%ddB:d=%.2f" % (thr, min_sil),
            "-f", "null", "-"]
    try:
        r = pipe.run(args, timeout=max(30, int(dur * 3)))
    except Exception:  # noqa: BLE001
        return 0.0
    txt = (r.stderr or "") + (r.stdout or "")
    quiet = 0.0
    cur = None
    for line in txt.splitlines():
        m = re.search(r"silence_start:\s*([\d.]+)", line)
        if m:
            cur = float(m.group(1))
            continue
        m = re.search(r"silence_end:\s*([\d.]+)", line)
        if m and cur is not None:
            quiet += max(0.0, float(m.group(1)) - cur)
            cur = None
    if cur is not None:
        quiet += max(0.0, dur - cur)
    return max(0.0, min(1.0, 1.0 - quiet / dur))


def is_aroll(info: dict) -> bool:
    """按当前规则重算，不信任缓存里的结论。

    这样以后调门槛（比如 AROLL_MIN_DUR）不用清缓存重体检。
    """
    return (bool(info.get("has_audio"))
            and float(info.get("dur") or 0) >= AROLL_MIN_DUR
            and float(info.get("speech") or 0) >= AROLL_SPEECH_MIN)


def probe(path: str) -> dict:
    """一条素材的体检结果：多长、有没有声音、说了多久。"""
    dur = pipe.media_duration(path)
    has = pipe.has_audio(path)
    sr = speech_ratio(path, dur) if has else 0.0
    info = {"dur": round(dur, 3), "has_audio": bool(has),
            "speech": round(sr, 3), "at": time.time()}
    info["aroll"] = is_aroll(info)
    return info


def classify(items: list[dict] | None = None, job: dict | None = None,
             force: bool = False) -> dict:
    """把素材池分成「口播」和「空镜」两堆，结果写回 pool（下次不用重算）。"""
    items = items if items is not None else pipe.pool_list()
    pool = pipe._pool_load()
    aroll, broll, skip = [], [], 0
    if job:
        job["total"] = max(1, len(items))
    for i, it in enumerate(items):
        if job:
            pipe.job_gate(job)
            job["done"] = i
            job["progress"] = i / max(1, len(items))
            job["message"] = "体检素材 %d/%d：%s" % (i + 1, len(items), it.get("name", ""))
        if "原片" in (it.get("tags") or []):
            # 原片镜头是我们从口播里切出来的派生品，只给 align 挑画面用。
            # 把它也算进口播素材，第二次跑同一批就会把同一句话转录好几遍。
            continue
        info = (it.get("audio") or {}) if not force else {}
        if not info or "aroll" not in info:
            try:
                info = probe(it["path"])
            except Exception as exc:  # noqa: BLE001
                skip += 1
                info = {"dur": it.get("dur") or 0, "has_audio": False,
                        "speech": 0.0, "aroll": False, "err": str(exc)[:120]}
        else:
            info = dict(info)
            info["aroll"] = is_aroll(info)   # 规则改了也能就地纠正，不用重体检
        it["audio"] = info
        if it["id"] in pool["items"]:
            pool["items"][it["id"]]["audio"] = info
        (aroll if info.get("aroll") else broll).append(it)
    pipe._pool_save(pool)
    if job:
        job["progress"] = 1.0
        job["done"] = len(items)
        job["message"] = "口播 %d 条 / 空镜 %d 条%s" % (
            len(aroll), len(broll), ("，%d 条读不出来" % skip) if skip else "")
    return {"aroll": aroll, "broll": broll, "skipped": skip}


# ---------------------------------------------------------------- 转写

def asr_ready() -> tuple[bool, str]:
    try:
        import faster_whisper  # noqa: F401
        return True, ""
    except Exception:  # noqa: BLE001
        return False, ("没装转写引擎。装一条就行：\n"
                       ".venv\\Scripts\\pip install faster-whisper\n"
                       "（首次运行会自动下模型，small 约 250MB；要更准可以设 "
                       'director.json 里 asr_model 为 "medium"）')


def _asr_cfg(cfg: dict) -> dict:
    return (cfg.get("director") or {}) if isinstance(cfg, dict) else {}


def _prep_asr_env(d: dict) -> None:
    """下模型常被系统代理掐（502），和 edge-tts 一样清掉代理再走。

    国内网络可以设 director.asr_hf_endpoint = https://hf-mirror.com。
    """
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
              "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    ep = str(d.get("asr_hf_endpoint") or "").strip()
    if ep:
        os.environ.setdefault("HF_ENDPOINT", ep)


_MODEL: dict = {}


def _model(name: str, device: str, compute: str):
    """模型只加载一次。每次都重载要多等 5~10 秒。"""
    key = (name, device, compute)
    if key not in _MODEL:
        from faster_whisper import WhisperModel
        _MODEL[key] = WhisperModel(name, device=device, compute_type=compute)
    return _MODEL[key]


def _load_audio_16k(path: str):
    """用 ffmpeg 解码成 16k 单声道 float32，绕开 faster-whisper 自带的 av 解码。

    faster-whisper 1.2.1 调 av 时传了 metadata_errors，新版 av(19) 已经没这个参数，
    直接给它路径会 TypeError。喂数组既避开这个坑，解码质量也更可控。
    """
    import wave

    import numpy as np

    tmp = Path(tempfile.gettempdir()) / ("shotcutter_asr_%d.wav" % os.getpid())
    r = pipe.run(["-i", str(Path(path).resolve()), "-vn", "-ac", "1", "-ar", "16000",
                  "-c:a", "pcm_s16le", "-y", str(tmp)], timeout=900)
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError("抽音轨失败：%s" % pipe._short(r.stderr or "", 200))
    with wave.open(str(tmp), "rb") as w:
        raw = w.readframes(w.getnframes())
    try:
        tmp.unlink()
    except OSError:
        pass
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def _cache_path(path: str, model: str) -> Path:
    key = hashlib.md5(("%s|%s" % (Path(path).resolve(), model)).encode("utf-8")).hexdigest()[:14]
    ASR_DIR.mkdir(parents=True, exist_ok=True)
    return ASR_DIR / ("%s.json" % key)


def _core(t: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]", "", t or "")


def _drop_prompt_echo(sents: list[dict], prompt: str) -> list[dict]:
    """丢掉「把热词提示原样吐出来」的幻觉句。

    没人说话的片段（纯音乐、环境音）上，whisper 会拿 initial_prompt 当内容输出，
    实测几乎一字不差地返回「这段话在讲东北黑木耳、肉厚无根、泡发快、干净无沙。」。
    这种句子一旦被当成「口播」选进成片，就是一段没声音的画面配一行假字幕。
    """
    pc = _core(prompt)
    if not pc or not sents:
        return sents
    seed = pc[:12]          # whisper 是从提示词开头开始吐的，抓前 12 个字最灵
    keep = []
    for s in sents:
        c = _core(s["text"])
        if not c:
            continue
        if c in pc or (seed and seed in c):
            continue
        if len(c) >= 6 and difflib.SequenceMatcher(None, c, pc).ratio() >= 0.72:
            continue
        # 还有一种：把提示词片段反复念三遍（"泡发快干净无根"×3），比例不高但明显是幻觉
        for n in (6, 8, 10):
            grams = [c[i:i + n] for i in range(0, max(1, len(c) - n + 1))]
            if grams and max(grams.count(g) for g in set(grams)) >= 3:
                c = ""
                break
        if not c:
            continue
        keep.append(s)
    return keep


def transcribe_file(path: str, cfg: dict, job: dict | None = None,
                    force: bool = False) -> list[dict]:
    """转写一条素材，返回绝对时间轴的句子表（带停顿切分）。结果按文件缓存。"""
    d = _asr_cfg(cfg)
    model_name = str(d.get("asr_model") or "small")
    # 默认 CPU：Windows 上 CT2 走 GPU 要 cuBLAS 12 + cuDNN 9 的 DLL，
    # 没装会直接报 "cublas64_12.dll is not found"。想用显卡就装好这两个
    # 再把 director.asr_device 设成 "cuda"。
    device = str(d.get("asr_device") or "cpu")
    cache = _cache_path(path, model_name)
    if cache.exists() and not force:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("sentences"):
                return data["sentences"]
        except Exception:  # noqa: BLE001
            pass
    ok, why = asr_ready()
    if not ok:
        raise RuntimeError(why)
    _prep_asr_env(d)
    if job:
        job["message"] = "加载转写模型（%s）…" % model_name
    model = _model(model_name, device, str(d.get("asr_compute") or "int8"))
    if job:
        job["message"] = "抽音轨：%s" % Path(path).name
    audio = _load_audio_16k(path)
    if job:
        job["message"] = "转写：%s" % Path(path).name
    segments, _info = model.transcribe(
        audio,
        language=str(d.get("asr_lang") or "zh"),
        vad_filter=bool(d.get("asr_vad", True)),
        vad_parameters={"min_silence_duration_ms": int(d.get("asr_min_silence_ms") or 300)},
        beam_size=int(d.get("asr_beam") or 5),
        initial_prompt=str(d.get("asr_prompt") or ""),
    )
    out = []
    for s in segments:
        txt = (s.text or "").strip()
        if len(re.sub(r"[^\w\u4e00-\u9fff]", "", txt)) < 2:
            continue
        out.append({"text": txt, "start": round(float(s.start), 3), "end": round(float(s.end), 3)})
    out = clean_sentences(out)
    out = _drop_prompt_echo(out, str(d.get("asr_prompt") or ""))
    try:
        cache.write_text(json.dumps({"from": str(path), "model": model_name,
                                     "sentences": out}, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return out


def strip_punct(text: str) -> str:
    """竖屏字幕不留标点：断行靠换行，句号逗号在竖屏里又小又脏。

    A 路线的文本来自转写，有的句子顺稿加了标点、有的被挡下保持原样，
    不统一的话同一条片子里会出现「有的带逗号有的不带」，看着像 bug。
    """
    return re.sub(r"[，。、；：,;]", "", text or "").strip()


def _tidy_cn(text: str) -> str:
    """中文之间被 whisper 插的空格去掉，英文/数字之间的保留。"""
    t = re.sub(r"(?<=[\u4e00-\u9fff，。！？、；：])\s+(?=[\u4e00-\u9fff，。！？、；：])", "", text or "")
    return re.sub(r"\s{2,}", " ", t).strip()


def clean_sentences(sents: list[dict]) -> list[dict]:
    """去掉纯语气句、合并被切碎的半句。只动文本边界，不动时间。"""
    fillers = {"嗯", "啊", "哦", "呃", "这个", "那个", "就是说", "然后", "就是"}
    out = []
    for s in sents:
        t = _tidy_cn(s["text"]).strip().strip("，、 ")
        core = re.sub(r"[^\w\u4e00-\u9fff]", "", t)
        if not core or core in fillers or len(core) < 4:
            # 太短的多半是「嗯」「对」这种，直接丢
            continue
        if out and s["start"] - out[-1]["end"] < 0.12 and len(core) < 8:
            out[-1]["text"] = (out[-1]["text"].rstrip("，。") + "，" + t).strip("，")
            out[-1]["end"] = s["end"]
            continue
        out.append({"text": t, "start": s["start"], "end": s["end"]})
    return out


def polish_sentences(sents: list[dict], cfg: dict, job: dict | None = None,
                     use_llm: bool = True) -> tuple[list[dict], str]:
    """让大模型只修错别字和标点，不改语序、不合并、不删句。

    whisper 的错字直接影响字幕可读性（实测把「像耳朵」听成「向耳朵」，
    整句听岔成「所以幼民小人晚」）。这里做严格校验：句数必须一致、
    每句长度变化不超过 40%，否则整批回退原文——宁可留着错字，也不能让模型重写内容。
    """
    if not use_llm or not sents or not pipe.llm_ready(cfg)[0]:
        return sents, ""
    body = "\n".join("%d. %s" % (i + 1, s["text"]) for i, s in enumerate(sents))
    prompt = (
        "下面是直播口播的语音转写，可能有同音错字、断句错、缺标点。\n"
        "请只做三件事：改错别字、补/改标点、去掉重复的口头语。\n"
        "禁止：改写意思、合并句子、删句子、调整顺序。句数必须和原文一致。\n"
        '返回 JSON：{"lines":["第一句","第二句",...]}\n\n' + body
    )
    try:
        if job:
            job["message"] = "顺一遍字幕（修错别字）…"
        txt = pipe.llm_chat([{"role": "user", "content": prompt}], cfg,
                            max_tokens=max(600, len(sents) * 80), temperature=0.1, timeout=180)
        lines = [str(x).strip() for x in (pipe._json_from(txt).get("lines") or [])]
    except Exception as exc:  # noqa: BLE001
        return sents, "顺稿失败，用原文：%s" % str(exc)[:60]
    if len(lines) != len(sents):
        return sents, "顺稿句数变了（%d→%d），用原文" % (len(sents), len(lines))
    fixed = 0
    moved = 0
    out = []
    for s, t in zip(sents, lines):
        old = s["text"]
        # 只允许「错字换字」级别的改动：内容字符数基本不变、整体相似度高。
        # 实测模型会把下一句开头的几个字挪到上一句末尾凑通顺，
        # 那样字幕比语音多出几个字，音画立刻对不上——比留着错字严重得多。
        c_old = re.sub(r"[^\w\u4e00-\u9fff]", "", old)
        c_new = re.sub(r"[^\w\u4e00-\u9fff]", "", t or "")
        ratio = difflib.SequenceMatcher(None, c_old, c_new).ratio() if (c_old or c_new) else 1.0
        if (not c_new or abs(len(c_new) - len(c_old)) > 2 or ratio < 0.65
                or not 0.55 <= len(t) / max(6, len(old)) <= 1.5):
            moved += 1
            out.append(s)
            continue
        if t != old:
            fixed += 1
        ns = dict(s)
        ns["text"] = t
        out.append(ns)
    bits = []
    if fixed:
        bits.append("改了 %d 句" % fixed)
    if moved:
        bits.append("挡下 %d 句疑似挪字用原文" % moved)
    return out, ("顺稿 " + "，".join(bits)) if bits else "顺稿无改动"


def transcribe_all(aroll: list[dict], cfg: dict, job: dict | None = None) -> list[dict]:
    """把所有口播素材转成一张全局句子表，每句记住出自哪个素材。"""
    ok, why = asr_ready()
    if not ok:
        raise RuntimeError(why)          # 先拦住：别转一圈才发现引擎没装
    sents: list[dict] = []
    failed = 0
    for i, it in enumerate(aroll):
        if job:
            pipe.job_gate(job)
            job["done"] = i
            job["progress"] = i / max(1, len(aroll))
            job["message"] = "转写 %d/%d：%s" % (i + 1, len(aroll), it.get("name", ""))
        try:
            got = transcribe_file(it["path"], cfg, job)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if job:
                job["message"] = "跳过（%s）：%s" % (str(exc)[:80], it.get("name", ""))
            continue
        for s in got:
            st = float(s["start"]) + float(it.get("start") or 0)
            en = float(s["end"]) + float(it.get("start") or 0)
            sents.append({
                "text": s["text"], "start": round(st, 3), "end": round(en, 3),
                "item_id": it["id"], "path": it["path"], "name": it.get("name", ""),
            })
    if not sents and failed:
        raise RuntimeError("这 %d 条口播素材一条都没转写成功，先看日志里的第一条报错" % failed)
    sents.sort(key=lambda x: (x["path"], x["start"]))
    for i, s in enumerate(sents):
        s["sid"] = "s%03d" % i
        s["idx"] = i
    return sents


# ---------------------------------------------------------------- 挑句

def _score_heuristic(text: str, product: dict) -> tuple[float, str]:
    """没有大模型时的兜底打分：靠卖点词、价格、CTA、痛点词硬凑。"""
    t = text or ""
    pts = [p for p in re.split(r"[\n;；,，、]+", str(product.get("points") or "")) if p.strip()]
    s, why = 0.0, []
    hit = [p for p in pts if p.strip()[:6] and p.strip()[:6] in t]
    if hit:
        s += 5.0 * len(hit)
        why.append("卖点")
    if re.search(r"\d+\s*(元|块|折|ml|g|件|个|天|年)", t):
        s += 2.0
        why.append("参数")
    if any(w in t for w in CTA_WORDS):
        s += 6.0
        why.append("CTA")
    if any(w in t for w in PAIN_WORDS):
        s += 3.0
        why.append("痛点")
    if any(w in t for w in PROOF_WORDS):
        s += 3.0
        why.append("证明")
    if "？" in t or "?" in t:
        s += 2.0
        why.append("提问")
    n = len(t)
    if 8 <= n <= 30:
        s += 1.0
    elif n > 45:
        s -= 1.5
    return s, "、".join(why)


def rank_heuristic(sents: list[dict], product: dict, target: float,
                   max_n: int = 14, seed: int = 0) -> list[dict]:
    """保序挑句：按分数选出最有用的几句，但不动它们的先后顺序。

    seed>0 时给分数加抖动——出多条版本时靠这个换组合，
    否则同一批素材每次挑出来的句子一模一样，成片只是重渲染。
    """
    rnd = random.Random(seed * 7919 + 13)
    scored = []
    for s in sents:
        sc, why = _score_heuristic(s["text"], product)
        if seed:
            sc += rnd.random() * 1.8
        scored.append((sc, s, why))
    cta = [x for x in scored if "CTA" in x[2]]
    rest = [x for x in scored if x not in cta]
    rest.sort(key=lambda x: -x[0])
    budget = max(20.0, float(target))
    picked, used = [], 0.0
    for sc, s, why in rest:
        if len(picked) >= max_n - 1:
            break
        dur = max(0.4, s["end"] - s["start"])
        if used + dur > budget * 1.15:
            continue
        picked.append({"sid": s["sid"], "slot": _guess_slot(s["text"], why), "why": why, "score": sc})
        used += dur
    if cta:
        best = max(cta, key=lambda x: x[0])
        if all(p["sid"] != best[1]["sid"] for p in picked):
            picked.append({"sid": best[1]["sid"], "slot": "cta", "why": best[2], "score": best[0]})
    order = {s["sid"]: s["idx"] for s in sents}
    picked.sort(key=lambda p: order.get(p["sid"], 0))
    return picked


def _guess_slot(text: str, why: str) -> str:
    if "CTA" in why:
        return "cta"
    if "痛点" in why:
        return "pain"
    if "证明" in why:
        return "proof"
    if "卖点" in why or "参数" in why:
        return "sell"
    return "sell"


def rank_prompt(product: dict, sents: list[dict], target: float,
                vocab: list[str] | None = None, seed: int = 0) -> str:
    lines = []
    for s in sents:
        dur = s["end"] - s["start"]
        lines.append("[%s] %.1fs %s" % (s["sid"], dur, s["text"]))
    body = "\n".join(lines)
    alt = ""
    if seed:
        alt = ("\n这是同一批素材的第 %d 版：换一种剪法——钩子句换一句，"
               "卖点挑另一个角度，总时长可以略有出入。\n" % (seed + 1))
    vs = ""
    if vocab:
        vs = ("\n手头能用的空镜画面标签只有这些，visual 必须从里面挑 1~3 个，"
              "没有合适的就留空：\n%s\n" % "、".join(vocab[:60]))
    return f"""你是一条带货短视频的剪辑师。下面是从达人原始口播里转写出来的一堆句子（带编号和时长），
我要把它们剪成一条 {int(target)} 秒左右的成片。

产品信息：
- 品名：{product.get('name') or ''}
- 人群：{product.get('audience') or ''}
- 卖点：{product.get('points') or ''}
- 痛点：{product.get('pain') or ''}
- 价格：{product.get('price') or ''}
- 优惠：{product.get('offer') or ''}
- 信任点：{product.get('proof') or ''}

挑选规则（务必遵守）：
1. 只挑完整、能独立听懂的句子，不要挑半句话；不要挑需要上文才懂的指代句。
2. 结构按 钩子 → 痛点 → 卖点 → 证明 → 行动号召 来配；钩子一句、行动号召最多一句。
3. 总时长控制在 {max(10, int(target * 0.8))}~{int(target * 1.15)} 秒之间。
4. 卖点必须和产品信息对得上，不要硬凑。
5. 最多 {min(14, len(sents))} 句；同一件事只说一遍。
6. 每句再给一个 visual：这句话配什么画面最贴，2~4 个中文词，写「景别/主体/动作」，
   而不是抄句子原话。例如「特写 抽屉 拉出」。
7. 这些句子是语音转写来的，一定有错字。**读起来语义不通、像乱码、明显听岔的句子一律跳过**，
   哪怕它提到了卖点也不选——错乱的一句话放进成片，观众立刻划走。宁可少一句。
{vs}
返回 JSON，不要解释：
{{"picks":[{{"sid":"s003","slot":"hook","why":"一句就点出痛点","visual":"特写 台面 杂乱"}}]}}

句子清单：
{body}
"""


def _pad_picks(picks: list[dict], sents: list[dict], target: float) -> tuple[list[dict], int]:
    """挑完发现离目标时长差太多时，补几句「上下文相邻」的句子。

    让模型一次就吃满时长很难（实测目标 30 秒只给 16 秒）。补句不能随便挑，
    必须挑已选句子的前后邻句——原片里它们是连着说的，接上去叙事不断；
    跨段乱补会变成「跳着说话」。
    """
    by_sid = {s["sid"]: s for s in sents}
    order = [s["sid"] for s in sents]
    pos = {sid: i for i, sid in enumerate(order)}
    have = {p["sid"] for p in picks}
    dur = sum(max(0.0, by_sid[p["sid"]]["end"] - by_sid[p["sid"]]["start"])
              for p in picks if p["sid"] in by_sid)
    need = float(target) * 0.8 - dur
    if need <= 0.5:
        return picks, 0
    added = 0
    guard = 0
    while need > 0.5 and guard < 40:
        guard += 1
        cand = []
        for sid in list(have):
            i = pos.get(sid)
            if i is None:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(order) and order[j] not in have:
                    cand.append(order[j])
        if not cand:
            break
        # 优先补紧挨着的那一句；同距离时按原顺序
        pick_sid = min(cand, key=lambda x: (min(abs(pos[x] - pos[y]) for y in have if y in pos), pos[x]))
        s0 = by_sid[pick_sid]
        have.add(pick_sid)
        picks.append({"sid": pick_sid, "slot": "sell", "why": "补时长（相邻句）", "visual": ""})
        need -= max(0.0, s0["end"] - s0["start"])
        added += 1
    picks.sort(key=lambda p: pos.get(p["sid"], 0))
    return picks, added


def rank(sents: list[dict], product: dict, target: float, cfg: dict,
         job: dict | None = None, use_llm: bool = True,
         vocab: list[str] | None = None, seed: int = 0) -> tuple[list[dict], str]:
    """让大模型挑句；大模型不可用就退化到关键词打分。返回 (picks, 说明)。"""
    if not sents:
        raise RuntimeError("没有转写出任何句子，检查素材里到底有没有人声")
    if use_llm and pipe.llm_ready(cfg)[0]:
        try:
            if job:
                job["message"] = "大模型挑句…"
            txt = pipe.llm_chat([{"role": "user",
                                  "content": rank_prompt(product, sents, target, vocab, seed)}],
                                cfg, max_tokens=1600,
                                temperature=min(1.0, 0.3 + 0.22 * max(0, seed)), timeout=180)
            data = pipe._json_from(txt)
            raw = data.get("picks") or []
            known = {s["sid"] for s in sents}
            picks = []
            for p in raw:
                sid = str(p.get("sid") or "").strip()
                if sid in known and all(x["sid"] != sid for x in picks):
                    slot = str(p.get("slot") or "sell")
                    picks.append({"sid": sid, "slot": slot if slot in SLOTS else "sell",
                                  "why": str(p.get("why") or ""),
                                  "visual": str(p.get("visual") or "").strip()})
            if picks:
                order = {s["sid"]: s["idx"] for s in sents}
                picks.sort(key=lambda p: order.get(p["sid"], 0))
                picks, added = _pad_picks(picks, sents, target)
                return picks, "由大模型挑句（%d 句%s）" % (
                    len(picks), "，补 %d 句凑时长" % added if added else "")
        except Exception as exc:  # noqa: BLE001
            if job:
                job["message"] = "大模型挑句失败，改用关键词：%s" % str(exc)[:60]
    picks = rank_heuristic(sents, product, target, seed=seed)
    if not picks:
        raise RuntimeError("没挑出可用的句子（素材内容和产品信息都对不上？）")
    picks, added = _pad_picks(picks, sents, target)
    return picks, "关键词兜底挑句（%d 句%s）" % (
        len(picks), "，补 %d 句凑时长" % added if added else "")


def broll_vocab(broll: list[dict]) -> list[str]:
    """空镜身上已有的标签，甩给大模型当词表，挑出来的 visual 才匹配得上。

    空镜没打标时返回空列表——那时只能靠原片画面，别让模型瞎编画面词。
    """
    cnt: dict[str, int] = {}
    for it in broll:
        for t in (it.get("tags") or []):
            if not str(t).strip():
                continue
            cnt[str(t).strip()] = cnt.get(str(t).strip(), 0) + 1
    return [k for k, _ in sorted(cnt.items(), key=lambda x: -x[1])]


# ---------------------------------------------------------------- 造人声轨

def _cut(src: str, start: float, end: float, out: Path) -> bool:
    args = ["-ss", "%.3f" % max(0.0, start), "-to", "%.3f" % max(start + 0.2, end),
            "-i", str(Path(src).resolve()), "-vn", "-ac", "1", "-ar", "44100",
            "-c:a", "pcm_s16le", "-y", str(out)]
    try:
        r = pipe.run(args, timeout=180)
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0 and out.exists() and out.stat().st_size > 512


def build_voice(picks: list[dict], sents: list[dict], out_wav: Path,
                job: dict | None = None, gap: float = GAP) -> dict:
    """按挑出来的句子把口播剪成一条连续人声轨，同时算出新的时间轴。

    这里整个项目的关键：切掉废话之后，时间轴必须整体重算，
    否则字幕和画面会全部错位。
    """
    by_sid = {s["sid"]: s for s in sents}
    tmp = out_wav.parent / "_cuts"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    pieces, rows = [], []
    for i, p in enumerate(picks):
        if job:
            pipe.job_gate(job)
            job["done"] = i
            job["progress"] = i / max(1, len(picks)) * 0.8
        s = by_sid.get(p["sid"])
        if not s:
            continue
        f = tmp / ("c%03d.wav" % len(pieces))
        if not _cut(s["path"], s["start"], s["end"], f):
            continue
        pieces.append(f)
        rows.append({"k": len(rows), "text": s["text"], "slot": p.get("slot", "sell"),
                     "visual": p.get("visual") or "", "why": p.get("why") or "",
                     "from": s["name"], "src": s["path"],
                     "src_start": s["start"], "src_end": s["end"]})
    if not pieces:
        raise RuntimeError("一句话都没剪出来（素材文件还在吗？）")

    if job:
        job["message"] = "拼接人声轨（%d 段）…" % len(pieces)
    args, filt = [], []
    for i, f in enumerate(pieces):
        args += ["-i", str(f)]
        filt.append("[%d:a]apad=pad_dur=%.2f[a%d]" % (i, gap, i))
    chain = "".join("[a%d]" % i for i in range(len(pieces)))
    filt.append("%sconcat=n=%d:v=0:a=1[out]" % (chain, len(pieces)))
    args += ["-filter_complex", ";".join(filt), "-map", "[out]",
             "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1", "-y", str(out_wav)]
    r = pipe.run(args, timeout=600)
    if r.returncode != 0 or not out_wav.exists():
        raise RuntimeError("人声轨合成失败：%s" % pipe._short(r.stderr or "", 300))

    # 时间轴：每段实测时长 + 固定间隔
    cur = 0.0
    sentences = []
    for row, f in zip(rows, pieces):
        d = pipe.media_duration(f)
        sentences.append({"k": row.get("k"), "text": row["text"], "start": round(cur, 3),
                          "end": round(cur + d, 3), "slot": row["slot"],
                          "visual": row.get("visual") or "", "from": row["from"]})
        cur += d + gap
    total = pipe.media_duration(out_wav)
    expect = cur
    if total > 0 and abs(total - expect) > 0.25 and expect > 0:
        # ffmpeg 的 apad/concat 偶尔和实测差一点，按比例拉一次，字幕才不漂
        k = total / expect
        for s in sentences:
            s["start"] = round(s["start"] * k, 3)
            s["end"] = round(s["end"] * k, 3)
    shutil.rmtree(tmp, ignore_errors=True)
    if job:
        job["progress"] = 0.9
    return {"audio": str(out_wav), "sentences": sentences, "total": round(total, 3), "rows": rows}


# ---------------------------------------------------------------- 串起来

def make_script(project: dict, product: dict, voice: dict, picks: list[dict],
                note: str, reorder: str = "keep", visual_mix: float = 0.5) -> dict:
    """产出和 tts_worker 同构的 script，下游照常跑。

    visual_mix 决定「画面用原片还是插空镜」：
      0   全部用原片画面（口型对得上，但同一机位看到底，容易闷）
      0.5 隔一句插一个空镜（默认，信息量和同步感比较平衡）
      1   除开头钩子和结尾 CTA，其余全插空镜（最像正经带货片）
    钩子和 CTA 永远保留原片：这两处观众在盯人脸，切走会觉得很怪。
    """
    sents = voice["sentences"]
    rows = {r.get("k"): r for r in (voice.get("rows") or [])}
    mix = max(0.0, min(1.0, float(visual_mix)))
    stride = 0 if mix <= 0 else max(1, int(round(1.0 / mix)))
    out = []
    k = 0
    for i, s in enumerate(sents):
        slot = s.get("slot") or "sell"
        visual = (s.get("visual") or "").strip()
        tts_line = (slot == "extra") or not (s.get("from") or "")
        broll = False
        if stride and visual and slot not in ("hook", "cta"):
            broll = (k % stride == 0)
            k += 1
        if tts_line:
            # AI 补的旁白没有对应原片，只能靠画面标签挑空镜
            hint = visual or ""
        elif broll:
            hint = visual
        else:
            # 用这一刀的唯一指纹当 hint，align 只会挑回它自己
            row = rows.get(s.get("k")) or {}
            hint = (cut_token(row["src"], row["src_start"], row["src_end"])
                    if row else "原片")
        out.append({"text": s["text"], "hint": hint, "slot": slot,
                    "visual": visual, "from": s.get("from")})
    title = "%s｜原声直剪" % (product.get("name") or "成片")
    # 有原片来源的句子 = A 路线；align_cfg 靠这个标记决定「一句一镜」
    route = "source" if any((s.get("from") or "") for s in voice["sentences"]) else "script"
    return {
        "angle": "direct", "angle_label": "原声直剪", "title": title,
        "sentences": out,
        "source": "director",
        "route": route,
        "note": note,
        "reorder": reorder,
        "visual_mix": mix,
        "picks": [{k2: p.get(k2) for k2 in ("sid", "slot", "why", "visual")} for p in picks],
        "tts": {
            "audio": voice["audio"], "sentences": voice["sentences"],
            "total": voice["total"], "engine": "source", "at": time.time(),
        },
    }


def cut_token(src: str, start: float, end: float) -> str:
    """每一刀的唯一标签，给 align 精确指路。

    早先所有原片共用「原片」这个标签，池子里有多刀时 _candidates 会匹配到
    全部原片，随机挑一个——画面和语音就对不上了（口型、源片自带的硬字幕全错位）。
    """
    raw = "%s|%.3f|%.3f" % (str(Path(src).resolve()), start, end)
    return "原片" + hashlib.md5(raw.encode("utf-8")).hexdigest()[:6]


def register_source_clips(voice: dict) -> int:
    """把剪出来的每一句原片登记进素材池，align 才能把「原片画面」挑回来。"""
    n = 0
    for s in voice["sentences"]:
        row = next((r for r in voice.get("rows") or [] if r.get("k") == s.get("k")), None)
        if not row:
            continue
        try:
            pipe.add_clip(row["src"], row["src_start"], row["src_end"],
                          name=row.get("from") or Path(row["src"]).stem,
                          src_kind="aroll")
            it = pipe._pool_load()["items"].get(
                pipe._clip_id(str(Path(row["src"]).resolve()), row["src_start"], row["src_end"]))
            if it is not None:
                tok = cut_token(row["src"], row["src_start"], row["src_end"])
                tags = [t for t in (it.get("tags") or []) if t != "原片" and t != tok]
                it["tags"] = ["原片", tok] + tags
                it["text"] = (it.get("text") or "") + " 原片"
                it["tagged"] = True
                pool = pipe._pool_load()
                pool["items"][it["id"]] = it
                pipe._pool_save(pool)
                n += 1
        except Exception:  # noqa: BLE001
            continue
    return n


def items_in(folders: list | None) -> list[dict]:
    """只取这几个目录下的池子条目。

    一键模式必须按目录圈定范围：否则每跑一次都要把整个池子（可能几百条）
    重体检一遍，白等几分钟。
    """
    items = pipe.pool_list()
    if not folders:
        return items
    roots = []
    for f in folders:
        try:
            roots.append(str(Path(str(f)).resolve()).lower().rstrip("\\/"))
        except OSError:
            continue
    if not roots:
        return items
    out = []
    for it in items:
        p = str(it.get("path") or "").lower()
        if any(p.startswith(r + "\\") or p.startswith(r + "/") for r in roots):
            out.append(it)
    return out


def prepare_worker(job: dict, folders: list | None = None, cfg: dict | None = None) -> dict:
    """第 0 步：扫素材 + 体检分流 + 转写。产物落 pool 和 asr 缓存。"""
    cfg = cfg or pipe.load_cfg()
    if folders:
        res = pipe.scan_folders(folders, job)
        if job:
            job["message"] = "扫描完成：新增 %d 个" % res.get("added", 0)
    cls = classify(items_in(folders), job=job)
    if not cls["aroll"]:
        raise RuntimeError("这批素材里没有能听的人声（全被判成空镜）。"
                           "要么换成带口播的素材，要么走原来的写稿 + 配音流程")
    sents = transcribe_all(cls["aroll"], cfg, job)
    if not sents:
        raise RuntimeError("转写结果是空的，检查素材音轨")
    if job:
        job["state"] = "done"
        job["progress"] = 1.0
        job["message"] = "口播 %d 条 / 空镜 %d 条，共 %d 句可用" % (
            len(cls["aroll"]), len(cls["broll"]), len(sents))
    return {"aroll": len(cls["aroll"]), "broll": len(cls["broll"]), "sentences": len(sents)}


# ---------------------------------------------------------------- B 路线：AI 写稿 + 配音

def check_numbers(script: dict, product: dict) -> list[str]:
    """找出稿子里「产品信息里没有的数字」。

    大模型写带货稿很爱编具体参数（实测编出「每 100 克泡发后能涨到 320 克」），
    这种数字播出去是要担责的。不拦生成，但必须把可疑数字报给用户核对。
    """
    src = " ".join(str(product.get(k) or "") for k in
                   ("name", "audience", "points", "pain", "price", "offer", "proof"))
    known = set(re.findall(r"\d+", src))
    bad = []
    for x in (script.get("sentences") or []):
        for n in re.findall(r"\d+", str(x.get("text") or "")):
            if len(n) >= 2 and n not in known:
                bad.append(n)
    return sorted(set(bad))


def write_script(product: dict, cfg: dict, job: dict | None, target: float,
                 angle: str | None = None, use_llm: bool = True) -> tuple[dict, str]:
    """素材里没人声时，让大模型写一条口播稿。

    默认只写一个角度：14 个全写要 5 批请求（3~5 分钟），一键出片等不起。
    想换角度就传 angle，或在 pipeline.json 里设 director.b_angle。
    """
    want = angle or str(_asr_cfg(cfg).get("b_angle") or "pain")
    if want not in pipe.ANGLES:
        want = "pain"
    angles = [want]
    if job:
        job["message"] = "写口播稿…"
    r = pipe.gen_scripts(product, angles, target, cfg, [], bool(use_llm), job)
    scripts = r.get("scripts") or []
    if not scripts:
        raise RuntimeError("一条脚本都没写出来，检查商品信息是不是太空")
    s = dict(scripts[0])
    s["idx"] = 0
    s["target"] = target
    s["source"] = "script"
    note = str(r.get("note") or "")
    made_up = check_numbers(s, product)
    if made_up:
        s["check_numbers"] = made_up
        note = "%s；⚠ 稿子里的数字商品信息里没有，播前核对：%s" % (
            note, "、".join(made_up[:6]))
    return s, note


def tts_script(project: dict, script: dict, cfg: dict, job: dict | None) -> dict:
    """跑 edge-tts，把 tts.sentences + audio 填进 script（复用原 tts_worker）。"""
    pdir = pipe.project_dir(project["id"])
    pdir.mkdir(parents=True, exist_ok=True)
    if job:
        job["message"] = "配音 %d 句…" % len(script.get("sentences") or [])
        job["total"] = max(1, len(script.get("sentences") or []))
        job["done"] = 0
    dummy = {"message": "", "progress": 0.0, "total": 1, "state": "running",
             "cancel": False, "pause": False}
    _sub(job, pipe.tts_worker, project, script, cfg, job or dummy)
    if not (script.get("tts") or {}).get("audio"):
        raise RuntimeError("配音没生成音频（断网了？设置里把引擎切成 sapi 试试）")
    return script


def topup_voice(voice: dict, lines: list[str], cfg: dict, out_wav: Path,
                job: dict | None = None, gap: float = GAP) -> dict:
    """原声不够长时，用 TTS 补几句旁白接在最后。

    音色和原声不是一个人，听感是「达人讲完，解说接一句」，所以默认不开。
    只在明确要凑够时长时用。
    """
    t = cfg.get("tts") or {}
    vname = str(t.get("voice") or "zh-CN-XiaoxiaoNeural")
    rate = str(t.get("rate") or "+8%")
    tmp = out_wav.parent / "_topup"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    pieces = [Path(voice["audio"])]
    for i, line in enumerate(lines):
        if job:
            pipe.job_gate(job)
            job["message"] = "补旁白 %d/%d…" % (i + 1, len(lines))
        mp3 = tmp / ("t%d.mp3" % i)
        wav = tmp / ("t%d.wav" % i)
        pipe._tts_edge(line, vname, rate, mp3)
        r = pipe.run(["-i", str(mp3), "-ar", "44100", "-ac", "1",
                      "-c:a", "pcm_s16le", "-y", str(wav)], timeout=120)
        if r.returncode != 0 or not wav.exists():
            continue
        pieces.append(wav)

    if len(pieces) == 1:
        shutil.rmtree(tmp, ignore_errors=True)
        return voice

    args, filt = [], []
    for i, p in enumerate(pieces):
        args += ["-i", str(p)]
        filt.append("[%d:a]apad=pad_dur=%.2f[a%d]" % (i, gap, i))
    filt.append("%sconcat=n=%d:v=0:a=1[out]" % ("".join("[a%d]" % i for i in range(len(pieces))), len(pieces)))
    args += ["-filter_complex", ";".join(filt), "-map", "[out]",
             "-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1", "-y", str(out_wav)]
    r = pipe.run(args, timeout=600)
    if r.returncode != 0 or not out_wav.exists():
        shutil.rmtree(tmp, ignore_errors=True)
        return voice

    sentences = list(voice["sentences"])
    pos = pipe.media_duration(pieces[0])
    for i, line in enumerate(lines):
        f = tmp / ("t%d.wav" % i)
        if not f.exists():
            continue
        d = pipe.media_duration(f)
        pos += gap
        sentences.append({"text": line, "start": round(pos, 3), "end": round(pos + d, 3),
                          "slot": "extra", "visual": "", "from": ""})
        pos += d
    total = pipe.media_duration(out_wav)
    shutil.rmtree(tmp, ignore_errors=True)
    return {"audio": str(out_wav), "sentences": sentences, "total": round(total, 3),
            "rows": list(voice.get("rows") or [])}


def topup_lines(product: dict, need: float, cfg: dict, use_llm: bool = True) -> list[str]:
    """按还差的秒数要 1~3 句补充旁白。"""
    n = max(1, min(3, int(round(need / 4.0))))
    if use_llm and pipe.llm_ready(cfg)[0]:
        try:
            txt = pipe.llm_chat([{"role": "user", "content": (
                "写 %d 句带货口播的补充旁白，接在达人讲完之后，用来补足时长。\n"
                "产品：%s；卖点：%s；痛点：%s；价格：%s。\n"
                "要求：每句 12~22 字，口语，不要编号，不要「首先/其次」，"
                "不要重复上面可能已经说过的内容，直接给 JSON：{\"lines\":[\"...\",\"...\"]}"
                % (n, product.get("name") or "", product.get("points") or "",
                   product.get("pain") or "", product.get("price") or ""))}],
                cfg, max_tokens=300, temperature=0.8, timeout=120)
            data = pipe._json_from(txt)
            lines = [str(x).strip() for x in (data.get("lines") or []) if str(x).strip()]
            if lines:
                return lines[:n]
        except Exception:  # noqa: BLE001
            pass
    p = product.get("points") or ""
    return [("它还有一个好处：%s。" % p.split("，")[0].split(",")[0][:14]) if p
            else "细节做得比同价位扎实，用久了才知道。"][:n]


def align_cfg(cfg: dict, script: dict) -> dict:
    """给 align 用的配置副本。

    原声直剪必须「一句一个镜头」：画面和口型是同步的，一句被拆成两个 2 秒镜头后，
    第二个镜头会从别的原片里挑，嘴型立刻对不上。所以把目标镜头时长抬到
    比绝大多数句子都长，让 align 不再拆分。
    """
    if script.get("route") != "source":
        return cfg
    c = json.loads(json.dumps(cfg))
    v = c.setdefault("video", {})
    v["shot"] = max(float(v.get("shot") or 2.2), 5.5)
    v["max_shot"] = max(float(v.get("max_shot") or 4.5), 9.0)
    return c


def _sub(job: dict | None, fn, *a, **kw):
    """跑一个子任务，但不让它把整个 job 标成已完成。

    tag_worker / tts_worker 这类子步骤自己会写 job["state"]="done"，
    对它们单独调用是对的，塞进一键流程里就会让前端进度条提前变「完成」。
    """
    r = fn(*a, **kw)
    if job:
        job["state"] = "running"
    return r


# ---------------------------------------------------------------- 主流程

def _group_by_source(sents: list[dict]) -> list[dict]:
    """把句子按出自哪条素材分组，长的排前面。

    批量出片时「一条素材出 N 条片」和「一条素材只出一条」差别很大：
    不同素材出的片天然不一样，同一素材只能换挑法——所以优先按素材分。
    """
    buckets: dict[str, list] = {}
    for s in sents:
        buckets.setdefault(str(s.get("path") or ""), []).append(s)
    groups = []
    for path, arr in buckets.items():
        arr.sort(key=lambda x: float(x.get("start") or 0))
        groups.append({
            "path": path, "name": (arr[0].get("name") or Path(path).stem) if arr else "",
            "sents": arr,
            "dur": sum(max(0.0, float(s["end"]) - float(s["start"])) for s in arr),
        })
    groups.sort(key=lambda g: -g["dur"])
    return groups


def _take_groups(groups: list[dict], start: int, target: float) -> tuple[list[dict], int]:
    """从 start 开始取素材，凑到约 target×0.6 秒为止（素材太短就连着拼两条）。"""
    if not groups:
        return [], start
    goal = min(20.0, max(8.0, float(target) * 0.6))
    out, total, i = [], 0.0, start
    while i < len(groups):
        g = groups[i]
        out.extend(g["sents"])
        total += g["dur"]
        i += 1
        if total >= goal or len(out) >= 40:
            break
    return out, i


def director_worker(job: dict, project: dict, product: dict, target: float = 45.0,
                    cfg: dict | None = None, use_llm: bool = True,
                    reorder: str = "keep", folders: list | None = None,
                    visual_mix: float = 0.5, tag_broll: bool = True,
                    mode: str = "auto", topup: bool = False,
                    variants: int = 1, seed0: int = 0,
                    only_paths: list | None = None,
                    avoid_texts: list | None = None) -> dict:
    """一键主流程。

    mode  auto   口播够就走原声直剪，不够/没有就改走 AI 写稿 + 配音
          source 强制原声直剪（没口播会报错）
          script 强制 AI 写稿 + 配音（素材纯空镜时用这个）
    topup 走原声但时长明显不够时，用 TTS 补一两句旁白凑时长（默认关）
    variants 出几条（批量）：一批口播素材就各出一条原声直剪，不够的条数用 B 补；
             同一素材被复用时会换一套挑法（seed），不会出两条一样的
    seed0    挑法起点，「重挑」靠它换一套
    only_paths 只用这几条素材（重挑时锁定原方案的素材，不然会从第一条重新分配）
    avoid_texts 上一版用过的句子。重挑时排除掉，保证真的换了一套；
                排除后剩余时长不足目标一半就放弃排除（素材太少时不能硬凑）
    """
    cfg = cfg or pipe.load_cfg()
    if folders:
        pipe.scan_folders(folders, job)
    cls = classify(items_in(folders), job=job)
    aroll, broll = list(cls["aroll"]), list(cls["broll"])
    n = max(1, int(variants))

    # 先转写：只有转录出句子的素材才算「能用来原声直剪」。
    # 纯音乐/环境音也会被判成有人声（silencedetect 分不出说话和音乐），
    # 这类素材转完是 0 句，不能占原声名额，否则那一条会空跑。
    pnote = ""
    sents: list[dict] = []
    if aroll and mode in ("auto", "source"):
        # 把品名/卖点当热词喂给转写：直播里「秋木耳」「泡发」这种词听岔了，
        # 后面挑句和字幕全跟着错，热词偏置是最省事的一档修正。
        cfg = dict(cfg)
        cfg["director"] = dict(_asr_cfg(cfg))
        if (cfg["director"].get("asr_hotword", True)
                and not cfg["director"].get("asr_prompt")):
            pts = re.split(r"[\n;；,，、]+", str(product.get("points") or ""))
            words = [w.strip() for w in [product.get("name") or ""] + pts if w and w.strip()]
            if words:
                cfg["director"]["asr_prompt"] = "这段话在讲" + "、".join(words[:8]) + "。"
        if tag_broll and broll:
            untagged = [it for it in broll if not it.get("tagged")]
            if untagged and pipe.llm_ready(cfg)[0] and pipe.vision_supported(cfg):
                try:
                    _sub(job, pipe.tag_worker, untagged, cfg, job)
                except Exception as exc:  # noqa: BLE001
                    if job:
                        job["message"] = "空镜打标跳过（%s）" % str(exc)[:60]
        sents = transcribe_all(aroll, cfg, job)
        if _asr_cfg(cfg).get("asr_polish", True):
            sents, pnote = polish_sentences(sents, cfg, job, use_llm)

    groups = _group_by_source(sents)
    if only_paths:
        want = {str(Path(str(x)).resolve()).lower() for x in only_paths if str(x or "").strip()}
        keep = [g for g in groups if str(Path(g["path"]).resolve()).lower() in want]
        if keep:
            groups = keep
    # 路线分配：一组能用的口播就出一条原声片，不够的条数用 AI 写稿补。
    # 这样「一次丢一堆素材」时，讲产品的和只有画面的都能一起出片。
    if mode == "source":
        if not groups:
            raise RuntimeError("选了只用原声直剪，但这批素材没转出可用的人声")
        order = ["source"] * n
    elif mode == "script":
        order = ["script"] * n
    else:
        k = min(n, len(groups))
        order = ["source"] * k + ["script"] * (n - k)
    if job:
        job["message"] = "计划 %d 条：原声 %d / 配音 %d" % (
            len(order), order.count("source"), order.count("script"))

    if "script" in order:
        # 画面源：口播素材本身也能当画面用，和空镜一起参与匹配
        items = aroll + broll
        untagged = [it for it in items if not it.get("tagged")]
        if untagged:
            try:
                _sub(job, pipe.tag_worker, untagged, cfg, job)
            except Exception as exc:  # noqa: BLE001
                if job:
                    job["message"] = "打标跳过（%s）" % str(exc)[:60]

    pdir = pipe.project_dir(project["id"])
    pdir.mkdir(parents=True, exist_ok=True)
    angles = list(pipe.ANGLES)
    made = []
    cursor, seed = 0, max(0, int(seed0))
    note = ""
    for v, route in enumerate(order):
        if job:
            pipe.job_gate(job)
            job["message"] = "第 %d/%d 条（%s）" % (
                v + 1, len(order), "原声直剪" if route == "source" else "AI 写稿")
        if route == "source":
            if cursor >= len(groups):
                cursor, seed = 0, seed + 1     # 素材用完了就回到开头，换个挑法再出
            sub, cursor = _take_groups(groups, cursor, target)
            if avoid_texts:
                # 方案里存的是去标点后的字幕文本，候选还是原始转写，
                # 必须按「去掉标点后的内容」比对，否则永远匹配不上。
                seen = {_core(t) for t in avoid_texts if _core(t)}
                keep = [x for x in sub if _core(str(x.get("text") or "")) not in seen]
                # 判据不能按句数（8 句素材用了 6 句，剩 2 句永远过不了 5 句的线，
                # 重挑就等于没挑）。按剩余可用时长：还够凑出目标的一半就换。
                left = sum(max(0.0, float(x["end"]) - float(x["start"])) for x in keep)
                if len(keep) >= 3 and left >= float(target) * 0.5:
                    sub = keep
            # 重新编号：rank/build_voice 都用 sid，每组必须是独立的一套
            sub = [dict(s, sid="s%03d" % i, idx=i) for i, s in enumerate(sub)]
            picks, note = rank(sub, product, target, cfg, job, use_llm,
                               broll_vocab(broll), seed=seed)
            if pnote:
                note = "%s；%s" % (note, pnote)
            vname = VOICE_NAME if n <= 1 else ("voice_direct_%d.wav" % (v + 1))
            voice = build_voice(picks, sub, pdir / vname, job)
            if _asr_cfg(cfg).get("sub_strip_punct", True):
                for _s in voice["sentences"]:
                    _s["text"] = strip_punct(_s["text"])
            if topup and voice["total"] < float(target) * 0.7:
                need = float(target) - voice["total"]
                lines = topup_lines(product, need, cfg, use_llm)
                if lines:
                    up = topup_voice(voice, lines, cfg,
                                     pdir / ("voice_direct_up_%d.wav" % (v + 1)), job)
                    if up is not voice:
                        voice = up
                        note += "；补了 %d 句旁白" % len(lines)
            sc = make_script(project, product, voice, picks, note, reorder, visual_mix)
            sc["route"] = "source"
            sc["seed"] = seed
            sc["source_name"] = (sub[0].get("name") if sub else "") or ""
            sc["source_path"] = (sub[0].get("path") if sub else "") or ""
            register_source_clips(voice)
            made.append({"script": sc, "sentences": len(voice["sentences"]),
                         "total": voice["total"], "note": note, "route": route})
        else:
            ang = angles[(v + seed) % len(angles)]
            sc, note = write_script(product, cfg, job, target, ang, use_llm)
            tts_script(project, sc, cfg, job)
            sc["route"] = "script"
            sc["seed"] = v + seed
            sc["visual_mix"] = 0.0
            made.append({"script": sc, "sentences": len(sc.get("sentences") or []),
                         "total": (sc.get("tts") or {}).get("total") or 0.0,
                         "note": note, "route": route})
    script = made[0]["script"]
    note = made[0]["note"]

    scripts = project.get("scripts") or []
    idxs = []
    for m in made:
        m["script"]["idx"] = len(scripts)
        idxs.append(m["script"]["idx"])
        scripts.append(m["script"])
    project["scripts"] = scripts
    project["product"] = product
    if (product.get("name") or "").strip():
        project["name"] = product["name"].strip()
    project["active"] = idxs[0]
    pipe.project_save(project)

    out = {
        "script_idx": idxs[0], "script_idxs": idxs,
        "mode": order[0] if len(set(order)) == 1 else "mixed",
        "plan": order, "note": note,
        "aroll": len(aroll), "broll": len(broll),
        "source_clips": len(sents),
        "sentences": made[0]["sentences"], "total": made[0]["total"],
        "items": [{"route": m["route"], "sentences": m["sentences"],
                   "total": round(m["total"], 2)} for m in made],
    }
    if job:
        job["result"] = out
        job["state"] = "done"
        job["progress"] = 1.0
        job["message"] = "导演完成：%d 条（原声 %d / 配音 %d）%s" % (
            len(idxs), order.count("source"), order.count("script"),
            ("　" + note) if note else "")
    return out


# ---------------------------------------------------------------- 自检

def selftest() -> int:
    """不需要模型、不需要素材：造一段纯音，验证切片 + 拼接 + 时间轴。"""
    import wave

    work = pipe.PIPE / "_selftest"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    src = work / "tone.wav"
    # 生成 12 秒 440Hz 正弦，16k 单声道
    import math
    import struct
    with wave.open(str(src), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        for i in range(16000 * 12):
            w.writeframes(struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / 16000))))

    sents = [{"sid": "s000", "idx": 0, "text": "第一句", "path": str(src),
              "name": "tone", "start": 0.5, "end": 2.0},
             {"sid": "s001", "idx": 1, "text": "第二句", "path": str(src),
              "name": "tone", "start": 4.0, "end": 6.5},
             {"sid": "s002", "idx": 2, "text": "第三句", "path": str(src),
              "name": "tone", "start": 9.0, "end": 10.2}]
    picks = [{"sid": "s000", "slot": "hook"}, {"sid": "s001", "slot": "sell"},
             {"sid": "s002", "slot": "cta"}]
    voice = build_voice(picks, sents, work / VOICE_NAME, None)
    want = (2.0 - 0.5) + (6.5 - 4.0) + (10.2 - 9.0) + GAP * 3
    ok_dur = abs(voice["total"] - want) < 0.35
    ok_axis = all(s["end"] > s["start"] for s in voice["sentences"]) and \
        voice["sentences"][-1]["end"] <= voice["total"] + 0.05
    mono = all(voice["sentences"][i]["start"] >= voice["sentences"][i - 1]["end"] - 0.02
               for i in range(1, len(voice["sentences"])))
    ratio = speech_ratio(str(src))
    ok_probe = ratio > 0.9          # 纯音不该被判成静音
    print("切片+拼接时长  期望 %.2fs 实测 %.2fs  %s" % (want, voice["total"], "OK" if ok_dur else "FAIL"))
    print("时间轴单调递增  %s" % ("OK" if ok_axis and mono else "FAIL"))
    print("有声占比检测   %.2f  %s" % (ratio, "OK" if ok_probe else "FAIL"))
    print("句子：", " | ".join("%.2f-%.2f %s" % (s["start"], s["end"], s["text"])
                              for s in voice["sentences"]))
    shutil.rmtree(work, ignore_errors=True)
    good = ok_dur and ok_axis and mono and ok_probe
    print("自检", "通过" if good else "失败")
    return 0 if good else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    print(__doc__)
