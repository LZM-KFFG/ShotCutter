# -*- coding: utf-8 -*-
"""把 ShotCutter 打成可以拷去别的电脑的安装包。

用法（在本机跑一次）：
    .venv\\Scripts\\python.exe pack.py                # 生成 dist\\ShotCutter-portable
    .venv\\Scripts\\python.exe pack.py --zip          # 顺手压成 zip
    .venv\\Scripts\\python.exe pack.py --refresh      # 重新下载依赖 wheel

目标机器只需要：Windows + Python 3.10~3.13。双击「安装.bat」建环境（全程离线），
再双击「启动.bat」。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE / ".venv" / "Scripts" / "python.exe"
OUT = HERE / "dist" / "ShotCutter-portable"

CODE_FILES = ["app.py", "pipeline.py", "director.py", "qc.py", "vsr_runner.py",
              "launch.py", "launcher.py", "requirements.txt", "app.ico", "README.md",
              "start.bat", "serve.bat",
              # 分发二进制时附上协议与文档：AGPL 要求随包提供许可证
              "LICENSE", "CLA.md", "AGENTS.md"]
CODE_DIRS = ["web", "samples", "docs"]

SETUP_PY = r'''# -*- coding: utf-8 -*-
"""建虚拟环境 + 离线装依赖 + 放好 ffmpeg。由「安装.bat」调用。"""
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent


def ok(msg):
    print("  [OK] " + msg)


def main():
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更高，当前是 %s" % sys.version.split()[0])
        return 1
    print("Python %s，开始安装到 %s" % (sys.version.split()[0], HERE))

    venv_dir = HERE / ".venv"
    py = venv_dir / "Scripts" / "python.exe"
    if not py.exists():
        print("1/3 创建虚拟环境（约 10 秒）…")
        venv.EnvBuilder(with_pip=True, clear=False).create(str(venv_dir))
    ok("虚拟环境就绪")

    wheels = HERE / "wheels"
    if not wheels.is_dir():
        print("找不到 wheels 目录，没法离线安装")
        return 1
    print("2/3 安装依赖（离线，约 2~4 分钟，1.5GB 左右）…")
    cmd = [str(py), "-m", "pip", "install", "--no-index",
           "--find-links", str(wheels), "-r", str(HERE / "requirements.txt"),
           "--disable-pip-version-check"]
    if subprocess.call(cmd) != 0:
        print("依赖安装失败：把上面的报错发出来")
        return 1
    ok("依赖装好了")

    ff = HERE / "tools" / "ffmpeg.exe"
    if ff.exists():
        shutil.copy2(ff, venv_dir / "Scripts" / "ffmpeg.exe")
        ok("ffmpeg 就位")
    else:
        print("  [警告] 没找到 tools\\ffmpeg.exe，切割/合成会不可用")

    for d in ("_work/pipe", "_work/asr", "_work/exports"):
        (HERE / d).mkdir(parents=True, exist_ok=True)

    print("3/3 检查显卡转写…")
    probe = ("import sys;sys.path.insert(0,r'%s');"
             "import director;"
             "import ctranslate2;"
             "print('CUDA 设备数:', ctranslate2.get_cuda_device_count())" % HERE)
    subprocess.call([str(py), "-c", probe])

    print("\n安装完成。双击「启动.bat」开始用。")
    print("没有 N 卡的话，把 pipeline.json 里 director.asr_device 改成 cpu 即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

INSTALL_BAT = r'''@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   ShotCutter 安装
echo ============================================
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 "%~dp0setup_env.py"
  goto end
)
where python >nul 2>&1
if %errorlevel%==0 (
  python "%~dp0setup_env.py"
  goto end
)
echo 没找到 Python。
echo 先装一个 Python 3.12 或 3.13：https://www.python.org/downloads/windows/
echo 安装时记得勾选 "Add python.exe to PATH"，装完再双击本文件。
:end
echo.
pause
'''

START_BAT = r'''@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 还没安装，先双击「安装.bat」
  pause
  exit /b 1
)
".venv\Scripts\python.exe" app.py %*
pause
'''

README_TXT = r'''ShotCutter 便携安装包
======================

一、装什么
  1) Python 3.12 或 3.13（安装时勾选 Add python.exe to PATH）
     下载：https://www.python.org/downloads/windows/
  2) 别的都不用装，依赖和 ffmpeg 都在包里

二、怎么装
  双击「安装.bat」→ 等 1 分钟 → 看到"安装完成"即可
  装完双击「ShotCutter.exe」（或「启动.bat」），浏览器会自动打开 127.0.0.1:8777

三、以后搬去第三台电脑
  整个文件夹拷过去，删掉 .venv 目录，重新双击「安装.bat」即可
  （.venv 不能跨机器复制，这是 Python 的限制）

四、可选的两个工具（不装也能用其它功能）
  · 视频变高清：把 VideoUpscaler 文件夹放到本目录 tools\VideoUpscaler
    或者在 pipeline.json 的 upscale.root 填它的绝对路径
  · 字幕去除：把 VSR 解压到任意位置，在 pipeline.json 的 desub.root 填路径
  改完配置重启服务生效。

五、显卡转写
  包内已带 cuBLAS / cuDNN。有 N 卡时 pipeline.json 里保持
  director.asr_device = "cuda"；没 N 卡改成 "cpu"（会自动回落，不改也不报错）。
  首次转写要下 500MB 模型（走 hf-mirror 镜像），之后走本地缓存。

六、数据都在哪
  _work\        素材池、项目、成片、缓存（可整体备份/搬走）
  pipeline.json 配置（模型、画幅、字幕、BGM 目录）
'''

STEPS = r'''# -*- coding: utf-8 -*-
"""安装完成后的自检：依赖齐不齐、ffmpeg 在不在、能不能起服务。"""
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = HERE / ".venv" / "Scripts" / "python.exe"


def main():
    checks = []
    checks.append((".venv", PY.exists()))
    checks.append(("ffmpeg", (HERE / ".venv" / "Scripts" / "ffmpeg.exe").exists()))
    for name, ok in checks:
        print(("  [OK] " if ok else "  [缺失] ") + name)
    if not all(ok for _, ok in checks):
        return 1
    code = ("import sys;sys.path.insert(0,r'%s');import pipeline,director;"
            "print('依赖导入 OK')" % HERE)
    if subprocess.call([str(PY), "-c", code]) != 0:
        return 1
    print("全部就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

EMBED_VER = "3.13.14"
EMBED_URL = "https://www.python.org/ftp/python/%s/python-%s-embed-amd64.zip" % (EMBED_VER, EMBED_VER)
EMBED_OUT = HERE / "dist" / "ShotCutter-免安装"

EMBED_START = r'''@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "python\python.exe" (
  echo 运行时文件缺失：python\python.exe
  pause
  exit /b 1
)
echo 启动中，浏览器会自动打开（关闭本窗口即停止服务）
python\python.exe app.py %*
pause
'''

EMBED_README = r'''ShotCutter 免安装版（自带 Python 运行时）
========================================

双击「ShotCutter.exe」就能用：服务在后台启动，浏览器自动打开。
不需要安装 Python、不需要联网装依赖。
（「启动.bat」是备用入口，效果相同，只是会留一个黑窗口显示日志）

  浏览器地址：http://127.0.0.1:8777
  停止服务：网页页脚的「退出服务」，或运行 ShotCutter.exe --stop

目录说明
  python\        自带的 Python 运行时和全部依赖（别删）
  tools\ffmpeg.exe
  web\           页面
  _work\         素材池、项目、成片、缓存（要备份就备这个目录）
  pipeline.json  配置（模型、画幅、字幕、BGM 目录）

常见问题
1) 提示 8777 被占用：程序会自动往后找端口，看窗口里打印的地址
2) 想换电脑：整个文件夹拷过去即可，无需重新安装
3) 显卡转写：有 N 卡自动用显卡（首次转写会下 500MB 模型，走 hf-mirror）；
   没 N 卡把 pipeline.json 里 director.asr_device 改成 "cpu"
4) 可选工具（不装不影响其它功能）：
   视频变高清 → 解压到 tools\VideoUpscaler，或改 pipeline.json 的 upscale.root
   字幕去除   → 解压到任意位置，改 pipeline.json 的 desub.root
'''



def build_exe(quiet: bool = False) -> Path:
    """用 PyInstaller 打桌面启动器 exe（约 8MB，双击即用）。"""
    out = HERE / "dist" / "_exe"
    cmd = [str(PY), "-m", "PyInstaller", "--onefile", "--noconsole", "--clean",
           "--name", "ShotCutter", "--icon", str(HERE / "app.ico"),
           "--distpath", str(out),
           "--workpath", str(HERE / "_work" / "_pyi"),
           "--specpath", str(HERE / "_work" / "_pyi"),
           str(HERE / "launcher.py")]
    print("打包桌面启动器 exe…")
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    exe = out / "ShotCutter.exe"
    if r.returncode != 0 or not exe.exists():
        print(r.stdout[-1500:] if not quiet else "")
        print(r.stderr[-1500:] if not quiet else "")
        raise SystemExit("exe 打包失败")
    print("  exe OK（%.1f MB）" % (exe.stat().st_size / 1048576))
    return exe

def _download(url: str, dst: Path) -> None:
    import urllib.request  # noqa: PLC0415

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        print("  已有缓存：%s" % dst.name)
        return
    print("  下载 %s …" % dst.name)
    # urllib 会读系统代理；python.org 在国内一般直连就够
    urllib.request.urlretrieve(url, dst)


def build_embed(refresh_wheels: bool, zip_it: bool = False) -> int:
    """免安装版：python 运行时 + site-packages 全部塞进包内，双击即用。"""
    import zipfile  # noqa: PLC0415

    cache = HERE / "dist" / "_cache"
    zip_path = cache / ("python-%s-embed-amd64.zip" % EMBED_VER)
    _download(EMBED_URL, zip_path)

    if EMBED_OUT.exists():
        print("清空旧的免安装包…")
        shutil.rmtree(EMBED_OUT)
    EMBED_OUT.mkdir(parents=True)

    print("解压运行时…")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(EMBED_OUT / "python")

    # 打开 site（否则装进 Lib\site-packages 的包不会被 import）
    # ".." 是包根目录：嵌入式 Python 是隔离模式，不会自动把脚本所在目录加进 sys.path
    pth = next((EMBED_OUT / "python").glob("python*._pth"))
    lines = [ln for ln in pth.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    for need in ("Lib\\site-packages", ".."):
        if need not in lines:
            lines.append(need)
    lines = [ln for ln in lines if ln.strip() != "import site"] + ["import site"]
    pth.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  运行时 OK（%s）" % pth.name)

    site_pkgs = EMBED_OUT / "python" / "Lib" / "site-packages"
    site_pkgs.mkdir(parents=True, exist_ok=True)

    wheels = HERE / "dist" / "ShotCutter-portable" / "wheels"
    if refresh_wheels or not wheels.is_dir():
        print("先准备离线 wheel（复用便携包目录）…")
        tmp = HERE / "dist" / "_wheels_tmp"
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        cmd = [str(PY), "-m", "pip", "download", "-r", str(HERE / "requirements.txt"),
               "-d", str(tmp), "--only-binary=:all:", "--disable-pip-version-check"]
        if subprocess.call(cmd) != 0:
            raise SystemExit("wheel 下载失败")
        wheels = tmp

    print("把依赖装进运行时（离线，2~4 分钟）…")
    cmd = [str(PY), "-m", "pip", "install", "--no-index", "--find-links", str(wheels),
           "--target", str(site_pkgs), "-r", str(HERE / "requirements.txt"),
           "--disable-pip-version-check", "--no-compile"]
    if subprocess.call(cmd) != 0:
        raise SystemExit("依赖安装失败")

    print("拷贝代码…")
    for name in CODE_FILES:
        if name in ("start.bat", "serve.bat"):    # 免安装版用自己的启动脚本
            continue
        src = HERE / name
        if src.exists():
            copy_item(src, EMBED_OUT / name)
    for name in CODE_DIRS:
        src = HERE / name
        if src.exists():
            copy_item(src, EMBED_OUT / name)

    cfg = json.loads(json.dumps(_base_cfg()))
    (EMBED_OUT / "pipeline.json").write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    ff = HERE / ".venv" / "Scripts" / "ffmpeg.exe"
    if not ff.exists():
        raise SystemExit("本机没有 ffmpeg.exe")
    copy_item(ff, EMBED_OUT / "tools" / "ffmpeg.exe")
    copy_item(ff, EMBED_OUT / "python" / "ffmpeg.exe")   # 双保险：运行时目录也放一份

    if build_exe():
        copy_item(HERE / "dist" / "_exe" / "ShotCutter.exe", EMBED_OUT / "ShotCutter.exe")

    (EMBED_OUT / "启动.bat").write_text(EMBED_START, encoding="utf-8")
    (EMBED_OUT / "serve.bat").write_text(
        '@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\n'
        'python\\python.exe launch.py\r\n', encoding="utf-8")
    (EMBED_OUT / "使用说明.txt").write_text(EMBED_README, encoding="utf-8")

    for d in ("_work/pipe", "_work/asr", "_work/exports"):
        (EMBED_OUT / d).mkdir(parents=True, exist_ok=True)

    size = sum(f.stat().st_size for f in EMBED_OUT.rglob("*") if f.is_file())
    print("\n免安装包：%s（%.0f MB）" % (EMBED_OUT, size / 1048576))
    print("拷到新电脑 → 双击「启动.bat」→ 完成，不需要装任何东西")
    if zip_it:
        z = HERE / "dist" / "ShotCutter-免安装.zip"
        zip_dir(EMBED_OUT, z)
        print("zip：%s（%.0f MB）" % (z, z.stat().st_size / 1048576))
    return 0



def copy_item(src: Path, dst: Path) -> None:
    if src.is_dir():
        # 跳过调试残留（web/_p4.html 这种）和字节码缓存，别把开发垃圾打进包
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("_*", "__pycache__", "*.pyc"))
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _base_cfg() -> dict:
    """干净配置：不带本机路径，GPU 默认开、缺卡自动回落。"""
    sys.path.insert(0, str(HERE))
    import importlib  # noqa: PLC0415

    pipe = importlib.import_module("pipeline")
    cfg = json.loads(json.dumps(pipe.DEFAULT_CFG))
    d = cfg.setdefault("director", {})
    d.update({"asr_device": "cuda", "asr_compute": "float16",
              "asr_hf_endpoint": "https://hf-mirror.com"})
    cfg.setdefault("desub", {})["root"] = ""
    cfg.setdefault("upscale", {})["root"] = ""
    cfg.setdefault("video", {})["bgm_dirs"] = []
    cfg["video"]["delogo"] = ""
    return cfg


def write_base_config(dst: Path) -> None:
    (dst / "pipeline.json").write_text(
        json.dumps(_base_cfg(), ensure_ascii=False, indent=2), encoding="utf-8")
    print("  配置模板 OK")


def download_wheels(dst: Path, refresh: bool) -> None:
    wheels = dst / "wheels"
    if wheels.exists() and not refresh:
        print("  wheels 已存在，跳过（要重下加 --refresh）")
        return
    if wheels.exists():
        shutil.rmtree(wheels)
    wheels.mkdir(parents=True)
    print("下载依赖 wheel（走 pip 缓存，第一次会比较久）…")
    cmd = [str(PY), "-m", "pip", "download", "-r", str(HERE / "requirements.txt"),
           "-d", str(wheels), "--only-binary=:all:", "--disable-pip-version-check"]
    if subprocess.call(cmd) != 0:
        raise SystemExit("wheel 下载失败，检查网络后重试")
    total = sum(f.stat().st_size for f in wheels.iterdir())
    print("  wheels 共 %d 个文件 / %.0f MB" % (len(list(wheels.iterdir())), total / 1048576))


def zip_dir(src: Path, out: Path) -> None:
    print("压缩中…（几 GB，慢一点）")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for f in src.rglob("*"):
            if f.is_file():
                z.write(f, f.relative_to(src.parent))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="store_true", help="打包成 zip")
    ap.add_argument("--refresh", action="store_true", help="重新下载 wheel")
    ap.add_argument("--embed", action="store_true",
                    help="额外生成免安装版（自带 Python 运行时，双击即用）")
    ap.add_argument("--only-embed", action="store_true", help="只生成免安装版")
    ap.add_argument("--exe", action="store_true", help="同时打包桌面启动器 ShotCutter.exe")
    args = ap.parse_args()

    if not PY.exists():
        raise SystemExit("先在本机把 .venv 修好（找不到 %s）" % PY)

    if args.only_embed:
        return build_embed(args.refresh, args.zip)

    if OUT.exists():
        print("清空旧的 dist…")
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    print("拷贝代码…")
    for name in CODE_FILES:
        src = HERE / name
        if not src.exists():
            print("  [跳过] " + name)
            continue
        copy_item(src, OUT / name)
    for name in CODE_DIRS:
        src = HERE / name
        if src.exists():
            copy_item(src, OUT / name)
    print("  代码 OK")

    write_base_config(OUT)

    print("放 ffmpeg…")
    ff = HERE / ".venv" / "Scripts" / "ffmpeg.exe"
    if not ff.exists():
        raise SystemExit("本机没有 ffmpeg.exe，先跑一遍 start.bat 让它自动就位")
    copy_item(ff, OUT / "tools" / "ffmpeg.exe")
    print("  ffmpeg OK（%.0f MB）" % (ff.stat().st_size / 1048576))

    download_wheels(OUT, args.refresh)

    if args.exe:
        copy_item(build_exe(), OUT / "ShotCutter.exe")

    (OUT / "setup_env.py").write_text(SETUP_PY, encoding="utf-8")
    (OUT / "安装.bat").write_text(INSTALL_BAT, encoding="utf-8")
    (OUT / "启动.bat").write_text(START_BAT, encoding="utf-8")
    (OUT / "安装说明.txt").write_text(README_TXT, encoding="utf-8")
    (OUT / "_steps.py").write_text(STEPS, encoding="utf-8")

    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print("\n生成完毕：%s（%.0f MB）" % (OUT, size / 1048576))
    print("拷这个文件夹到新电脑 → 双击「安装.bat」→ 双击「启动.bat」")
    if args.zip:
        z = HERE / "dist" / "ShotCutter-portable.zip"
        zip_dir(OUT, z)
        print("zip：%s（%.0f MB）" % (z, z.stat().st_size / 1048576))
    if args.embed:
        print("")
        build_embed(args.refresh, args.zip)
    return 0


if __name__ == "__main__":
    sys.exit(main())
