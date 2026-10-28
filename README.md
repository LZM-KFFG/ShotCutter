# ShotCutter · 本地带货短视频流水线

把「素材切片 → 打标 → 口播脚本 → 配音 → 分镜对齐 → 合成导出」串成一条五步向导。
起一个本地服务、开一个网页就能跑完，素材不出本机。

> 私有项目，仅供本人使用，未授权转载或商用。

---

## 它能做什么

| 环节 | 说明 |
|---|---|
| **切片** | `镜头切割器` 页（`/`）用 PySceneDetect 按镜头切分长视频，导出到源目录下的 `镜头切片/`；流水线第 1 步扫描这些片段（整段视频也能直接加，会当作一个镜头），支持多文件夹、递归扫描、指纹去重 |
| **去硬字幕** | 调本机 VSR（默认 `D:\Tools\VSR`）把烧进画面的字幕抹掉再进池：可选「自动检测」逐帧找字幕，或「固定底部区域」整块重绘（快很多）；支持整文件夹批量，也能只处理勾选的素材 |
| **打标** | 先用 OpenCV 算亮度 / 色调 / 饱和度 / 细节 / 动态等视觉特征；接了视觉大模型后再加「场景 / 主体 / 动作 / 景别 / 氛围」语义标签，分镜匹配会准很多 |
| **脚本** | 内置 **14 种口播角度**（痛点切入、对比实测、参数硬核、避坑提醒、场景代入、亲身自述、算账划算、问答答疑、口碑回购、开箱第一眼、用法教程、送礼场景、时令当季、反常识），按商品信息生成带停顿与镜头提示的口播稿 |
| **配音** | edge-tts 逐句合成（默认 `zh-CN-XiaoxiaoNeural`，语速 +8%），拿每句真实时长；断网可切 Windows SAPI 兜底 |
| **分镜** | 装箱式对齐：按目标镜头时长把每句拆成 1~n 个镜头（受最短/最长镜头约束），再按语义标签打分挑素材；批量出片时每条用不同种子重新挑，同一脚本能出互不相同的版本 |
| **合成** | ffmpeg 渲染 1080×1920 / 30fps / H.264 + AAC，ASS 字幕（微软雅黑、描边、底部留白）；支持 BGM 循环、淡入淡出、音量与限幅 |
| **批量出片** | 脚本 × 版本一次排队，逐条显示进度；配音缺失自动补配，失败不影响其他条 |
| **成片管理** | 列表显示时长 / 体积 / 码率 / 镜头数，点表头排序；多选打包 zip 下载；删除走回收站，不动原始素材 |

除「可选的云端大模型」和「edge-tts 在线合成」外，全流程在本机完成。

---

## 环境要求

- **Windows 10 / 11**（回收站删除、SAPI 兜底、后台常驻启动都用到 Windows API）
- **Python 3.10+**（代码用了 `str | None` 这类新语法，开发和验证环境为 3.13）
- **ffmpeg**：仓库不附带，见下面「安装」第 2 步
- 可选：**llama.cpp** + 一个视觉模型（本地打标 / 本地写脚本，省钱且不联网）
- 可选：**VSR**（Video Subtitle Remover，自带 Python + CUDA 版 torch）用于去硬字幕，默认路径 `D:\Tools\VSR`
- 可选：能上外网（edge-tts 走微软在线接口；纯 SAPI 则不需要）

---

## 安装

**1. 建虚拟环境并装依赖**

```bat
cd /d D:\ShotCutter
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

**2. 放好 ffmpeg。** 仓库不附带二进制，但 `imageio-ffmpeg` 里有一份，复制到 `Scripts` 下即可（程序优先找 `.venv\Scripts\ffmpeg.exe`，其次找 PATH 里的 `ffmpeg`）：

```bat
.venv\Scripts\python.exe -c "import imageio_ffmpeg, shutil; shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), r'.venv\Scripts\ffmpeg.exe')"
```

**3. 验证**

```bat
.venv\Scripts\ffmpeg.exe -version
```

> 若系统里已有 ffmpeg 并想省掉这一步，把它的目录加进 PATH 也行；程序启动时会在日志里打印用的是哪一个。

---

## 启动

| 方式 | 命令 | 特点 |
|---|---|---|
| 前台 | 双击 `start.bat`，或 `.venv\Scripts\python.exe app.py` | 黑窗口关掉即停，适合临时用 |
| **常驻** | 双击 `serve.bat` | 用 `launch.py` 以脱离终端的方式起进程，窗口自动关闭，服务继续跑 |
| 指定端口 | `.venv\Scripts\python.exe app.py 9000 --no-browser` | 默认 8777，被占用时自动往后找（最多 +20） |

启动后浏览器会自动打开：

- `http://127.0.0.1:8777/` —— **镜头切割器**（只做切片，快速试参数）
- `http://127.0.0.1:8777/pipeline` —— **流水线**（五步向导，主界面）

> 页面上有改动看不到时按 **Ctrl+F5** 强制刷新；旧页面跑的是旧 JS。

---

## 五步流程

| 步骤 | 你要做的 | 产出 |
|---|---|---|
| **1 素材** | 粘文件夹路径（可多行）→ 扫描加入 → 打标；有硬字幕的先用上方「去硬字幕」卡片处理 | `_work/pipe/pool.json` 素材池 + 缩略图 |
| **2 脚本** | 填品名 / 受众 / 卖点 / 痛点 / 价格 / 信任点，勾选角度 → 生成 | `project.json` 里的 scripts（含每句的镜头提示） |
| **3 配音** | 点「生成配音」，逐句合成并试听 | `audio_N.wav` + 每句起止时间 |
| **4 分镜** | 点「自动对齐」，按语义标签给每句挑镜头 | 镜头清单 CSV + ASS 字幕 |
| **5 成片** | 渲染导出；需要多个版本就「批量出片」 | `_work/pipe/out/*.mp4` + 同名 sidecar `.json` |

**素材池的两个细节**：同一段素材按「路径 + 起止时间」指纹去重，重复扫描不会重复加入；多选模式下可全选 / 反选 / 只选未打标，然后批量删除。

**批量出片**：选好脚本和版本数（如 2 条脚本 × 3 版本 = 6 条），每条用不同随机种子重新挑镜头，文件名带 `_V1` `_V2` 区分；某条缺配音会自动补，失败的会列出来不影响其他条。

**BGM**：设置页里可选「不加 / 随机 / 指定」，音乐库支持多个目录；合成时用 `-stream_loop -1` 循环、`afade` 淡入淡出、`amix=normalize=0` + `alimiter`，保证人声不被压小、也不削波。

---

## 设置项

设置页保存到根目录 `pipeline.json`（**不入库**，含本机路径）。没有这个文件时全部走代码里的默认值。

| 分组 | 关键字段 |
|---|---|
| `llm` | `base_url` / `api_key` / `model` / `vision_model`：任意 OpenAI 兼容接口；`local_dir` / `local_port` / `local_extra`：本地 llama.cpp 一键启停 |
| `tts` | `engine`（`edge` / `sapi`）、`voice`、`rate`、`gap` |
| `video` | `w` `h` `fps` `crf` `preset` `fit`、镜头时长 `shot` / `min_shot` / `max_shot`、字幕 `subtitle` `sub_font` `sub_size` `sub_margin` `sub_outline`、`hook` |
| `video.bgm*` | `bgm_mode`（none / random / one）、`bgm_dirs`、`bgm_volume`、`bgm_fade` |
| `desub` | `root`：VSR 安装目录，默认 `D:\Tools\VSR`；里面要有 `Python\python.exe` 和 `resources\backend` |

**本地模型**：设置页「一键启动」会调用 `llama-server.exe`，自动挑带视觉能力的模型并配上 `mmproj`，端口默认 9931；启动后会真发一张小图验证「能不能看图」。默认参数 `-c 8192 -ub 512 -cmoe --moe-cache-mib 7000` 是实测比较稳的组合——**不要手写 `-ngl` 把显存塞满**，那样会触发 CPU 回退，速度掉到几十分之一。热缓存下次启动约 4 秒。

---

## 目录结构

```
ShotCutter/
├─ app.py            # 后端：标准库 http.server，页面路由 + JSON 接口 + 任务轮询 + Range 视频流
├─ pipeline.py       # 流水线：切片 / 打标 / 写稿 / 配音 / 对齐 / 合成 / 批量 / 打包
├─ launch.py         # 脱离终端常驻启动（DETACHED_PROCESS + CREATE_BREAKAWAY_FROM_JOB）
├─ vsr_runner.py     # 驱动 VSR 去字幕（按次切换检测开关、兜住空检测的崩溃）
├─ start.bat         # 前台启动
├─ serve.bat         # 双击 = launch.py
├─ requirements.txt
├─ web/
│  ├─ index.html     # 镜头切割器（单页）
│  └─ pipe.html      # 流水线五步向导（单页）
├─ samples/          # 示例素材（一段含 8 个镜头的长视频 + 切好的 8 个镜头）
├─ README.md
├─ pipeline.json     # 本机配置（运行时生成，不入库）
└─ _work/            # 全部运行产物（不入库）
   ├─ pipe/pool.json             # 素材池
   ├─ pipe/projects/<id>/        # 项目：project.json、sub_N.ass、shotlist_N.csv、audio_N.wav
   ├─ pipe/out/*.mp4 + .json     # 成片与 sidecar 记录（时长、体积、码率、镜头数、BGM）
   ├─ pipe/desub/                # 去字幕产物（*_no_sub.mp4 + index.json 记录源→输出）
   ├─ pipe/zip/                  # 批量下载打包结果（保留最近 8 个 / 6 小时）
   └─ thumbs/                    # 素材缩略图
```

---

## 数据安全

- **素材只读**：切片、对齐、合成都不改动源文件。
- **删成片不删素材**：只删该条 mp4、sidecar、对应的字幕/分镜表和配音，且走 **Windows 回收站**（`FOF_ALLOWUNDO`），删错能捞回来。
- **共用项目不误删**：一个项目里有多条成片时，删其中一条不会动 `project.json` 和其他条的分镜文件。
- **项目记录不随成片消失**：删光成片后仍可回到第 2/3 步改脚本重出；万一记录丢了，前端会按页面上的脚本自动重建项目，并把刚才的请求重发一次。
- **不外传**：素材、脚本、成片都在本机；只有 edge-tts 会把「要合成的文字」发给微软接口，介意就切 SAPI 或本地 TTS。

---

## 常见问题

| 现象 | 处理 |
|---|---|
| 页面弹「连不上本地服务 / Failed to fetch」 | 服务进程没了。双击 `serve.bat` 重新起，再刷新页面 |
| 改了页面没反应 | Ctrl+F5；旧标签页仍在跑旧 JS |
| 批量下载失败 | 先确认服务活着；打包是同步的，几十条会等几秒 |
| 报「未找到 ffmpeg」 | 按安装第 2 步把 ffmpeg 复制到 `.venv\Scripts\`，或把系统 ffmpeg 加进 PATH |
| 本地模型很慢（< 5 tok/s） | 多半是启动参数把显存占满触发了 CPU 回退，去掉 `-ngl`，用默认 `local_extra` |
| 生成的视频在某些播放器打不开 | 导出统一 H.264 / yuv420p，兼容性最好；若素材本身是 HEVC，浏览器预览可能不支持，但成片没问题 |
| 配音没声音 / 报错 | edge-tts 需要联网；断网时在设置里把引擎切成 `sapi` |
| 去字幕提示「没找到 VSR」 | 路径不对：改 `pipeline.json` 的 `desub.root`，指向含 `Python\python.exe` 的那一层 |
| 去字幕很慢 | 正常：STTN 逐帧重绘，1080×1920 约 2~5 帧/秒。想快就用「固定底部区域」，它跳过逐帧检测 |
| 去字幕后画面糊成一片 | 「固定底部区域」的上下左右填大了，框到了画面主体；缩小到只盖住字幕那一条 |
| 端口被占用 | 程序会自动往后找端口，看启动日志里的实际地址 |

---

## 接口速查

后端是纯标准库 `http.server`，没有框架依赖。任务类接口返回 `{"job": "<id>"}`，前端轮询 `/api/job?id=` 拿 `state` / `progress` / `message`。

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/pipe/pool` `/config` `/projects` `/outputs` `/angles` | 读状态 |
| GET | `/api/pipe/out?name=` `/thumb?id=` `/audio?pid=&idx=` | 成片 / 缩略图 / 配音（支持 Range，可拖动进度） |
| POST | `/api/pipe/scan` | 扫描文件夹，把视频 / 已切好的片段加入素材池（`folders` 支持数组） |
| POST | `/api/pipe/tag` | 打标（`force` 为全量重打） |
| POST | `/api/pipe/script` | 生成口播稿 |
| POST | `/api/pipe/tts` `/align` `/compose` | 配音 / 分镜对齐 / 合成 |
| POST | `/api/pipe/batch` | 批量出片 |
| POST | `/api/pipe/output/zip` `/output/delete` | 打包 / 删除成片 |
| POST | `/api/pipe/desub` | 去字幕（`folders` 或 `ids`，`mode`=auto/box，`box`=上下左右百分比） |
| POST | `/api/pipe/llm/start` `/llm/stop` | 本地模型启停 |

---

## 说明

- 仅支持单机单用户使用，服务默认只监听 `127.0.0.1`。
- 代码按个人工作流演进，配置与目录结构可能随时调整，不保证向后兼容。
