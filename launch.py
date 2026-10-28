# -*- coding: utf-8 -*-
"""让服务脱离当前终端常驻后台。

用法：
    python launch.py            # 起在 8777（被占用就视为已在运行，直接退出）
    python launch.py 9000       # 换端口
    serve.bat                   # 双击等于上面第一条

为什么需要它：直接 `python app.py` 时进程挂在父 shell 的作业对象里，
终端一关、或者父进程被工具/任务管理器回收，服务就跟着死掉，
网页上所有请求变成 "Failed to fetch"（打包下载最容易撞上）。
这里用 DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB 起子进程，
父进程立刻退出，服务继续跑。
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE / ".venv" / "Scripts" / "python.exe"
if not PY.exists():
    PY = Path(sys.executable)

DETACHED = 0x00000008
NEWGRP = 0x00000200
BREAKAWAY = 0x01000000


def alive(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    port = 8777
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
    no_browser = "--no-browser" in sys.argv

    if alive(port):
        print("服务已经在运行：http://127.0.0.1:%d/" % port)
        if not no_browser:
            import webbrowser
            webbrowser.open("http://127.0.0.1:%d/" % port)
        return 0

    log = HERE / "_work" / "server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    out = open(log, "ab", buffering=0)
    cmd = [str(PY), str(HERE / "app.py"), "--no-browser", str(port)]
    flags = DETACHED | NEWGRP
    try:
        proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, close_fds=True,
                                creationflags=flags | BREAKAWAY)
    except OSError:
        # 有些环境不允许 breakaway，退化到普通后台进程
        proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, close_fds=True,
                                creationflags=flags)

    for _ in range(60):
        if alive(port) or proc.poll() is not None:
            break
        time.sleep(0.25)

    if alive(port):
        print("已启动：http://127.0.0.1:%d/   pid=%d" % (port, proc.pid))
        print("日志：%s" % log)
        if not no_browser:
            import webbrowser
            webbrowser.open("http://127.0.0.1:%d/" % port)
        return 0

    print("启动失败，看日志：%s" % log)
    return 1


if __name__ == "__main__":
    sys.exit(main())
