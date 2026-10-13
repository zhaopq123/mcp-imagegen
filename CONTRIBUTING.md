# 贡献指南

感谢愿意参与！

## 开始之前

1. 先跑通项目：见 [docs/wiki/快速开始](docs/wiki/快速开始.md)
2. 看 [docs/wiki/架构说明](docs/wiki/架构说明.md) 了解分层与设计约定
3. 提交 Issue 讨论你想做的事，避免重复劳动

## 提交代码流程

1. Fork 本仓库
2. 建分支：`git checkout -b feat/你的改动`
3. 写代码 + 补测试
4. 本地自检：
   ```bash
   ./venv/bin/python -m pytest -q        # 424 项单测
   ./venv/bin/python server.py --check   # 配置自检
   ```
5. 提交：`git commit -m "feat: 说明"`
6. 推送并发 PR

## 提交信息格式

```
类型: 一句话说明
```

| 类型 | 用途 |
|---|---|
| feat | 新功能 |
| fix | Bug 修复 |
| docs | 文档 |
| refactor | 重构（不改行为）|
| chore | 杂务（依赖、构建）|

## 代码约定

- **禁止引入线程**：全项目只用 asyncio（R2 铁律，有结构性单测把守）
- **配置不硬编码**：新增可调项必须进 `config.py` 的 `_ENV_SPEC`，含默认值
- **错误走契约**：对外返回统一 `[error] category=... retry=...` 契约行
- **超时遵守嵌套不等式**：新增超时项不能破坏 `read ≤ 分档 ≤ 总预算 < 210 < 220`

## PR 会被打回的情况

- 单测不过 / 配置自检不过
- 引入线程、硬编码配置、绕过契约直接抛异常
- PR 里带密钥或内网 IP
- 一个 PR 塞太多不相关改动
