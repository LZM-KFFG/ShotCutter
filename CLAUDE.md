# CLAUDE.md

本项目的说明统一写在 [AGENTS.md](AGENTS.md)，请先读它——安装、目录职责、硬约束、常见改动、测试方式都在里面。

要点速查：

- 跑程序用 `.venv\Scripts\python.exe`，不要用系统 Python（依赖只装在 .venv 里）
- 不要读、改、提交 `_work/`（素材池/项目/成片）和 `pipeline.json`（本机路径）
- 自动挑素材必须走 `pipe.scope_items()` / `pipe.script_scope()`，禁止在范围为空时退回全池
- 改 `pipeline.py` / `director.py` 的分镜逻辑后，跑 `tests/test_scope.py`
- 前端零构建，直接改 `web/*.html`，不要引入 npm 或 CDN
- 许可证 AGPL-3.0；外部代码贡献需签 [CLA](CLA.md)
