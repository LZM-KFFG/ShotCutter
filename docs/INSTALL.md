# 安装教程

从一台干净的 Windows 机器开始，到能出第一条成片。全程约 10 分钟（首次装依赖 3~8 分钟）。

> 只想快速跑起来：装好 Python 和 ffmpeg → 双击 `install.bat` → 双击 `start.bat`。

## 1. 前置条件

| 需要 | 说明 |
|---|---|
| **Windows 10 / 11** | 目前只支持 Windows（依赖 ffmpeg 的可执行文件与 CUDA DLL） |
| **Python 3.12 或 3.13** | [python.org 下载](https://www.python.org/downloads/windows/)，安装时**必须勾选 `Add python.exe to PATH`** |
| **ffmpeg** | 见下面第 2 步，没有它什么都做不了 |
| 磁盘空间 | 源码 + 依赖约 4 GB；素材、成片另算 |

显卡是可选的：有 NVIDIA 显卡时转写走 GPU（快约 4 倍），没有就自动回落 CPU。

检查 Python 装好没有：按 `Win+R` 输入 `cmd`，执行

```bat
python --version
```

出现 `Python 3.12.x` / `3.13.x` 即可。提示「不是内部或外部命令」说明没勾 PATH，重装 Python 并勾上。

## 2. 装 ffmpeg

最省事的做法（推荐）：

1. 去 [ffmpeg 的 Windows 构建](https://github.com/BtbN/FFmpeg-Builds/releases) 下载 `ffmpeg-master-latest-win64-gpl.zip`
2. 解压，进 `bin\` 目录，把 **`ffmpeg.exe`、`ffprobe.exe`** 两个文件拷进项目的 `.venv\Scripts\` 目录
   （这个目录要装完依赖才有，先做第 3 步再回来拷也可以）

程序的查找顺序是：`.venv\Scripts\ffmpeg.exe` → `tools\ffmpeg.exe` → 项目根目录 → 系统 PATH。放哪都行，但要放对。

> 用的是 GPL 构建也没关系，本项目本身以 AGPL-3.0 发布。如果要二次分发打包好的版本，注意一并满足 ffmpeg 的许可证要求。

## 3. 拿到代码

**方式 A：源码（推荐，更新最方便）**

```bat
git clone https://github.com/LZM-KFFG/ShotCutter.git
cd ShotCutter
```

没有 git 的话，在 [Releases](https://github.com/LZM-KFFG/ShotCutter/releases) 页下载 `Source code (zip)` 解压也行。

**方式 B：免安装版**

自带 Python 运行时，不需要装 Python。体积约 1.5~2.6 GB，目前放在网盘分发（Release 页有链接的话直接下；没有就按方式 A）。

## 4. 安装依赖

双击 **`install.bat`**。它会做三件事：

1. 创建虚拟环境 `.venv`
2. 按 `requirements.txt` 装依赖（PySceneDetect、OpenCV、faster-whisper、edge-tts 等，含约 1.2 GB 的 CUDA 库）
3. 检查 ffmpeg 是否就位

看到「安装完成」就成功了。窗口里的报错别忽略，按下面「常见问题」处理。

手工等价命令：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> **不要**用系统的 `python` 或 `pip` 直接装到全局，也不要用系统 Python 启动程序——依赖只在 `.venv` 里。

## 5. 启动

双击 **`start.bat`**，浏览器会自动打开 `http://127.0.0.1:8777`。

- 端口被占用时会自动往后顺延（8778、8779…），实际地址看控制台第一行
- 关掉黑色控制台窗口 = 停止服务；页面右下角也有「退出服务」
- 想让服务留在后台、窗口自动关掉，用 `serve.bat`

启动后页面顶部应该看到两个绿色的 chip：**引擎**、**ffmpeg**。变红就是环境有问题。

## 6. 可选组件（不装也能用）

| 组件 | 用途 | 怎么接 |
|---|---|---|
| **VSR**（video-subtitle-remover） | 擦除视频里的硬字幕 | 下载后把路径填进 `pipeline.json` 的 `desub.root`，指向含 `Python\python.exe` 和 `resources\run_with_area.py` 的目录 |
| **video2x** | 超分 + 补帧 | 同上，填 `upscale.root`（指向含 `tools\video2x.exe` 的目录），或直接把文件夹放到 `tools\VideoUpscaler` |
| **本地大模型** | 写稿、挑句、画面打标 | 用 [llama.cpp](https://github.com/ggml-org/llama.cpp) 起一个 OpenAI 兼容服务，在页面「设置」里填地址和模型名；也可以点设置里的「启动本地模型」按钮自动拉起。视觉打标需要带 `--mmproj` 的 VL 模型 |

`pipeline.json` 第一次在页面里保存设置时自动生成，里面有本机路径，已被 `.gitignore` 忽略，不要提交。

## 7. 验证装好了

按顺序点一遍，每步都能跑通就说明环境没问题：

1. **镜头切割器** → 拖一个 mp4 进去 → 出缩略图、能勾选导出 → 点「导出选中片段」得到文件
2. **字幕去除** → 填一个带字幕的视频文件夹 → 点「抽一帧预览」能看到画面（没装 VSR 会提示缺引擎，属正常）
3. **一键出片** → 填素材文件夹 → 出方案 → 渲染一条
4. **成片流水线** → 素材页能看到素材池、打标按钮可用

## 8. 常见问题

| 现象 | 原因与处理 |
|---|---|
| 提示「没装转录引擎 faster-whisper」 | 用了系统 Python 启动。必须用 `start.bat` 或 `.venv\Scripts\python.exe app.py` |
| 页面顶部 ffmpeg 变红 | ffmpeg 没找到。按第 2 步把 `ffmpeg.exe`、`ffprobe.exe` 放进 `.venv\Scripts\` |
| `pip install` 卡住 / 超时 | 换国内源：`.venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple` |
| 端口 8777 被占用 | 程序会自动顺延，看控制台打印的实际地址；或先关掉占用的进程 |
| 控制台中文乱码 | 用 `start.bat`（内含 `chcp 65001`）；程序内部对子进程日志做了 UTF-8/GBK 自适应 |
| 转写很慢、GPU 没吃上 | 任务管理器默认看 3D 引擎，CUDA 在「Cuda / Compute」。用 `nvidia-smi` 确认；日志第一行会打印实际设备 |
| 抽帧是全黑 | 素材可能没下载完整（体积 ≈ 时长 × 码率 ÷ 8）。程序会自动退到能解码的时间点并提示 |
| 打标只有亮度/色彩，没有「特写」「场景」 | 本地视觉模型没起来。设置里点「启动本地模型」，或用带 mmproj 的 VL 模型 |
| 出片报「范围里没有素材」 | 第 1 步的素材文件夹还是上一个商品的。改成当前这批素材的目录 |

## 9. 更新与卸载

**更新**：

```bat
git pull
install.bat        :: 依赖有变化时再跑一次
```

**卸载**：直接删掉整个项目目录即可。程序不写注册表、不装系统服务；素材和成片都在你自己的目录里（`_work/pipe/out/` 是成片，`镜头切片/` 在素材旁边）。

## 10. 打包给别的机器

```bat
.venv\Scripts\python.exe pack.py --exe          :: 便携安装包（目标机需 Python，约 1.5 GB）
.venv\Scripts\python.exe pack.py --only-embed   :: 免安装版（自带 Python 运行时，约 2.6 GB）
```

产物在 `dist/`。两个包都不含 `_work`（素材池/项目/成片），也不含 VSR 和 video2x。
