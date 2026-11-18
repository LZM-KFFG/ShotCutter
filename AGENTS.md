# AGENTS.md

给 AI 编码代理（Claude Code / Codex / Cursor / Copilot 等）的项目说明。人类读者请看 [README](README.md)；从零安装见 [docs/INSTALL.md](docs/INSTALL.md)。

## 这是什么

Windows 本机的短视频出片工具，浏览器就是界面（`http://127.0.0.1:8777`）。一条链路：

```
素材 → PySceneDetect 切镜头 → VSR 擦硬字幕 → video2x 超分补帧
     → faster-whisper 转写口播 → 挑句/写稿 → 分镜对齐 → ffmpeg 合成 + 成片体检
```

后端是 Python 标准库 `http.server`（没有 Flask/FastAPI），前端是原生 HTML/CSS/JS（没有构建步骤、没有 npm）。

## 硬约束（违反会直接出错）

1. **只用项目虚拟环境的解释器**：`.venv\Scripts\python.exe`（Windows）。
   系统 Python 跑 `app.py` 会报「没装 faster-whisper」——依赖只装在 `.venv` 里。
2. **不要动 `_work/`，也不要提交它**。这是运行时数据：素材池 `_work/pipe/pool.json`、项目 `projects/`、成片 `out/`、ASR 缓存 `asr/`、日志。`_work/` 和 `pipeline.json`（含本机路径/模型路径）都在 `.gitignore` 里。
3. **自动挑素材必须带范围**。素材池是全局的，几个商品混在一起；出片只允许用项目/脚本记录的目录：
   - `pipe.scope_items(items, folders)`：按目录过滤；
   - `pipe.script_scope(script, project, extra)`：取这条脚本的素材范围（脚本 → 项目 → 兜底目录 → 反推）；
   - `pipe.align()` / `pipe.swap_clip()` / `director.director_worker()` 内部已经守范围。
   新增任何「自动从池子里挑素材」的逻辑，必须复用这几个，**不要在范围为空时退回整个池子**——那是历史上「柿饼素材混进腰果片子」的根因。
4. **素材池写入有保护**。`pipeline._pool_save()` 遇到「非空池被写空」会抛错并记 `_work/pipe/pool-guard.log`；真要清空走 `pool_clear()`（内部 `allow_empty=True`）。不要绕过它。
5. **子进程输出用 `app._decode_line()` 解码**。Windows 控制台/工具输出常是 GBK，直接 `utf-8` 解会乱码。
6. **前端没有构建**。改 `web/*.html` 直接刷新即生效；不要引入 npm、打包器、CDN 依赖（离线可用是卖点）。
7. **不要写死本机路径、模型路径、密钥**。配置走 `pipeline.json`（首次保存设置时生成）或环境变量。

## 目录与职责

| 路径 | 职责 |
|---|---|
| `app.py` | HTTP 服务、全部接口（`do_GET` / `do_POST` 里按 `u.path` 分发）、静态文件、任务队列 `JOBS`、文件选择框、退出 |
| `pipeline.py` | 素材池、切片/导出、分镜 `align/swap_clip`、字幕 ASS、渲染合成、BGM、打包、去重、打标、本地模型管理 |
| `director.py` | 一键出片导演：素材体检分流（口播/空镜）→ 转写 → 挑句 → 时间轴 → 画面匹配 → 生成脚本 |
| `qc.py` | 成片体检规则（黑边、字幕超宽、静音段、音量、首尾黑场、码率） |
| `vsr_runner.py` | 字幕去除工具（VSR）的命令行封装 |
| `launch.py` / `launcher.py` | 启动入口（找端口、开浏览器）/ 打包成 exe 的桌面启动器 |
| `pack.py` | 打包脚本（便携版 `--exe` / 免安装版 `--only-embed`） |
| `web/` | `home` 首页、`index` 切割器、`sub` 字幕去除、`hd` 超分、`one` 一键出片、`pipe` 成片流水线 |
| `tests/test_scope.py` | 素材范围隔离的回归测试（唯一测试文件） |

运行时目录（`_work/pipe/`）：`pool.json` 素材池、`projects/` 项目、`out/` 成片、`asr/` 转写缓存、`desub/` 去字幕记录、`zip/` 打包临时。

## 跑起来

```bat
install.bat                        :: 建 .venv + 装 requirements（首次 3~8 分钟）
start.bat                          :: 前台启动，浏览器开 127.0.0.1:8777
.venv\Scripts\python.exe app.py 8777 --no-browser   :: 等价的手工启动（调试用这个）
```

端口被占用会自动往后顺延，实际端口看控制台。`serve.bat` 是后台启动版（跑 `launch.py`）。

ffmpeg 查找顺序：`.venv\Scripts\ffmpeg.exe` → `tools\ffmpeg.exe` → 项目根目录 → PATH。

## 改代码的常见动作

**加一个页面**：`web/xxx.html` → `app.py` 的 `do_GET` 加 `elif u.path in ("/xxx", "/xxx.html")` → 首页 `web/home.html` 加入口。

**加一个接口**：GET 写在 `do_GET`，参数从 `q`（`parse_qs`）取；POST 写在 `do_POST`，参数从 `payload`（已解析的 JSON）取，返回统一用 `self._json({...})`，出错用 `self._json({"error": "..."}, 400)`。长任务用 `new_job(kind, total)` + `threading.Thread(target=_run_job, ...)`，前端用 `pollJob` 轮询 `/api/job?id=`。

**改分镜挑片**：只改 `pipeline.align()` 里的评分 `_score()` / 候选 `_candidates()`，或者 `_scope_*` 相关函数。改完跑 `tests/test_scope.py`，并注意 `align()` 的 `used` 参数是批量渲染跨版本共享的「已用素材」计数（保证几条片子不重复用同一批镜头）。

**改安全/权限**：这个服务只监听 `127.0.0.1`，没有登录体系（单人本机工具）。如果要暴露到局域网，必须先自己做鉴权，不要直接把 `--host 0.0.0.0` 打开。

## 测试

```bat
.venv\Scripts\python.exe tests\test_scope.py
```

覆盖：脚本范围优先、换镜头不越界、无范围时报错、老项目按品名猜范围、无效目录不退回全池、同一脚本多版本不能同一批镜头。

改 `pipeline.py` / `director.py` 的分镜、素材池、范围相关逻辑后**必须跑这个**。其它模块没有自动化测试，改了要手点对应页面。

## 提交约定

- commit message 用 `fix:` / `feat:` / `docs:` / `refactor:` + 中文描述，说清「原来什么问题 → 现在怎么做」
- 不提交 `_work/`、`pipeline.json`、`dist/`、`__pycache__/`
- 代码里注释用中文，解释「为什么」而不是「做什么」

## 许可证与贡献

AGPL-3.0。项目保留商业双许可的可能，因此**外部代码 PR 必须先签署 [CLA](CLA.md)**，未签署的 PR 不会合并；Issue 和 bug 反馈不受限制。
