# -*- coding: utf-8 -*-
"""ShotCutter 桌面启动器 —— 打包成 ShotCutter.exe 后就是"双击即用"的软件形态。

双击做了什么：
  1. 找到包内的 Python 运行时（免安装版 python\\，或装好的 .venv\\）
  2. 以脱离终端的方式后台起 app.py（关掉启动器也不影响服务）
  3. 等健康检查通过，自动打开浏览器
  4. 自己退出，不留黑窗口

停止服务：网页页脚的「退出服务」，或命令行 `ShotCutter.exe --stop`。

为什么不做成单文件全量 exe：2.6GB 依赖（含 CUDA）每次启动都要解压到临时目录，
既慢又容易被杀毒软件拦。外壳 exe + 运行时目录才是这类本地工具的稳妥形态。
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

APP_NAME = "ShotCutter"
DEFAULT_PORT = int(os.environ.get("SHOTCUTTER_PORT") or 8777)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
DETACHED = 0x00000008
BREAKAWAY = 0x01000000


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


BASE = base_dir()
PID_FILE = BASE / "_work" / "service.pid"
LOG_FILE = BASE / "_work" / "launcher.log"


def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except Exception:  # noqa: BLE001
        pass


def tip(title: str, text: str) -> None:
    """没有控制台时用系统弹窗提示（不依赖 tkinter）。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)
    except Exception:  # noqa: BLE001
        log("%s: %s" % (title, text))


def find_runtime() -> Path | None:
    for rel in ("python/python.exe", ".venv/Scripts/python.exe", ".venv/bin/python"):
        p = BASE / rel
        if p.exists():
            return p
    return None


def healthy(port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def find_ready_port(port: int, seconds: float = 90.0) -> int | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        for p in range(port, port + 21):
            if healthy(p, 0.6):
                return p
        time.sleep(0.8)
    return None


def spawn(py: Path, port: int) -> subprocess.Popen:
    args = [str(py), "app.py", str(port), "--no-browser"]
    for flags in (CREATE_NO_WINDOW | DETACHED | BREAKAWAY, DETACHED, 0):
        try:
            p = subprocess.Popen(args, cwd=str(BASE), creationflags=flags,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 close_fds=True)
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(p.pid), encoding="utf-8")
            log("已启动服务进程 pid=%s port=%s" % (p.pid, port))
            return p
        except Exception as exc:  # noqa: BLE001
            log("启动失败（flags=%s）：%s" % (flags, exc))
    raise SystemExit("无法启动服务")


def _alive(pid: int) -> bool:
    """这个 pid 还活着吗（Windows 上 tasklist，其它平台用信号 0 探测）。"""
    if pid <= 4:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid], capture_output=True,
                             text=True, errors="replace", timeout=8).stdout
        return str(pid) in out
    except Exception:  # noqa: BLE001
        return False


def stop_service() -> bool:
    """停掉本包拉起的服务。

    优先用 pid 文件精确定位；只有拿不到 pid 时才按端口兜底，而且**只认目标端口**
    （以前会把 8777~8797 上一整段端口的所有实例都杀掉，同机跑多个副本时会误杀别人的服务）。
    """
    pids: list[int] = []
    try:
        if PID_FILE.exists():
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if _alive(pid):
                pids.append(pid)
    except Exception:  # noqa: BLE001
        pass
    if not pids and os.name == "nt":
        env_port = (os.environ.get("SHOTCUTTER_PORT") or "").strip()
        target = int(env_port) if env_port.isdigit() else DEFAULT_PORT
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                                 text=True, errors="replace", timeout=10).stdout
            for line in out.splitlines():
                if "LISTENING" not in line:
                    continue
                parts = line.split()
                if len(parts) < 5:
                    continue
                local, pid = parts[1], parts[-1]
                try:
                    port = int(local.rsplit(":", 1)[-1])
                except ValueError:
                    continue
                if port == target and pid.isdigit():
                    pids.append(int(pid))
        except Exception:  # noqa: BLE001
            pass
    done = False
    for pid in sorted(set(pids)):
        if pid <= 4:
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=10)
            done = True
            log("已停止 pid=%s" % pid)
        except Exception as exc:  # noqa: BLE001
            log("停止 pid=%s 失败：%s" % (pid, exc))
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return done


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    headless = "--headless" in args

    if "--stop" in args or "stop" in args:
        ok = stop_service()
        msg = "服务已停止" if ok else "没有发现正在运行的服务"
        log(msg)
        if not headless:
            tip(APP_NAME, msg)
        return 0

    py = find_runtime()
    if py is None:
        msg = ("找不到 Python 运行时。\n\n免安装版请确认 python\\python.exe 还在；\n"
               "安装版请先双击「安装.bat」。")
        log(msg)
        if not headless:
            tip(APP_NAME, msg)
        return 1

    # 已经跑着：直接打开页面
    for p in range(DEFAULT_PORT, DEFAULT_PORT + 21):
        if healthy(p, 0.5):
            log("服务已在 %d 运行，打开页面" % p)
            if not headless:
                webbrowser.open("http://127.0.0.1:%d" % p)
            else:
                print("READY http://127.0.0.1:%d" % p, flush=True)
            return 0

    spawn(py, DEFAULT_PORT)
    port = find_ready_port(DEFAULT_PORT)
    if port is None:
        msg = "服务 60~90 秒没有响应。\n\n看 _work\\launcher.log；\n也可能是端口被占用或配置报错。"
        log(msg)
        if not headless:
            tip(APP_NAME, msg)
        return 1

    log("服务就绪：http://127.0.0.1:%d" % port)
    if headless:
        print("READY http://127.0.0.1:%d" % port, flush=True)
    else:
        webbrowser.open("http://127.0.0.1:%d" % port)
        # 关掉启动器不影响服务：进程已 DETACHED
        time.sleep(0.3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
