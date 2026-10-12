# deploy/：部署交付（systemd 单元、Caddyfile、部署 runbook）

| 项 | 内容 |
|---|---|
| 文档版本 | V2.0 |
| 日期 | 2026-10-10 |
| 维护 | Leo（文档工程师） |

## 项目简介（V2：Agnes 双模型三工具）

MCP 图片/视频生成 Server：单一上游 Agnes AI（`api.agnes-ai.cn`）双模型三工具，经 MCP streamable HTTP（Bearer 鉴权）对外服务，systemd 守护（Type=notify + watchdog 自愈）。

| 工具 | 上游模型 | 用途 |
|---|---|---|
| `generate_image` | agnes-image-2.5-flash | 文生图（prompt/size/batch_size/persist） |
| `generate_video` | agnes-video-2.5-flash | 文生视频（prompt/seconds/aspect_ratio/persist） |
| `query_video` | ——（任务表） | 凭 task_id 续查视频任务 |

V2 起硅基流动（SiliconFlow）Provider 及其全部变量已彻底删除（变更依据与验收口径见项目根《MCP图片视频生成Server_增量设计方案V2.md》）。

## 快速开始

1. 按 `install.md` §1–§8 安装：创建专用低权限用户与目录（**V2 新增 `VIDEO_DIR`**）→ 部署代码 + venv → 写 `/etc/mcp-imagegen/env`（两个必填密钥 `MCPACCESS_TOKEN` / `AGNES_TOKEN`，变量总表 25 项见 §5.1）→ 装载 systemd 单元 → 跑安装验证；
2. Agent 客户端按 `agent-config.md` 配置（Bearer + 超时 **≥210s** + 三工具参数表）；
3. 上线前过 `install.md` §13 上线检查清单（**V2 新增**：换正式 Agnes Key / 控制台核对 / 附录 A 实测项）。

## 目录结构

| 文件 | 用途 |
|---|---|
| `install.md` | 安装/更新/运维 runbook（环境变量总表、systemd 装载、上线前检查清单） |
| `agent-config.md` | Agent 客户端配置（三工具参数表、超时、错误契约、curl 三连自检） |
| `mcp-imagegen.service` | 主服务 systemd 单元（watchdog/熔断风暴保护/优雅排空） |
| `mcp-imagegen-disk-check.service` + `.timer` | 每日 04:30 磁盘水位巡检（V2 覆盖 images/ + videos/ 双目录，只告警不删） |
| `Caddyfile` | 可选前置反代（/mcp、/files、/healthz；读超时 220s 对齐全链） |

> 单元文件/Caddyfile 本轮（V2）经文档层面核对：**功能层无需改动**（新变量全部经同一 EnvironmentFile 注入）；注释层 4 处滞后清单见 `install.md` §7.1，待开发确认后下轮统一改。

## 版本记录

| 版本 | 日期 | 变更 |
|---|---|---|
| V1.0 | 2026-10-10 | 初版：S4 交付一句话索引（单工具生图） |
| V2.0 | 2026-10-10 | Agnes 双模型三工具：项目简介 / 快速开始（+VIDEO_DIR 与上线检查清单指引）/ 目录结构（双目录水位）；V1 单工具表述废止 |

下一步建议：以本目录为部署唯一入口——正式 Key 落位后按 `install.md` §13 上线闸门执行，附录 A 实测结果回填方案文档。
