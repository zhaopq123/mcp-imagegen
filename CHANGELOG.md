# 更新日志

所有显著改动记录在此文件。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Added
- V2：单 Provider（Agnes AI）双模型三工具
  - `generate_image`：文生图，batch 1–4 服务端拆分
  - `generate_video`：文生视频 4–12 秒
  - `query_video`：查询视频任务状态
- 并发槽池与排队（超时返 busy）
- 硬/软熔断保护
- 产物持久化转存（`persist=true`）
- 全链路超时预算嵌套不等式自检
- 优雅停机（SIGTERM → 在途排空 → 组件收尾）
- systemd 单元（Type=notify + watchdog）与安装手册
- 424 项单测 + e2e 验收脚本

### Changed
- 上游模型 ID 改为可配置（`AGNES_IMAGE_MODEL` / `AGNES_VIDEO_MODEL`），不再硬编码
- 生图 read 档超时 30s → 60s（D5，4K 档实测余量）

### Fixed
- `test_tool.py` 7 个用例改为兼容 fastmcp 2.x 私有 API 与 4.x 公开 API
