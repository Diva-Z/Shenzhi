# 沈知 (ShenZhi) 维护日志（精炼版）

> 这里保留面向开发和发布的高信号摘要。更详细记录见 `CHANGES-完整存档.md`。

---

## 2026-06-13 GitHub 模板化发布

目标：把晨风、海洋、沈知三个人格作为可复用模板提交到 GitHub，让新用户 clone 后安装模板、补配置即可运行。

改动：

- `templates/personas/`
  - 新增 `shenzhi`、`chenfeng`、`haiyang` 三套人格模板。
  - 每套只包含 `AGENT.md`、`USER.md`、空的 `MEMORY.md`。
  - 不包含 `.env`、API key、Telegram token、微信凭证、followup 状态、SQLite 记忆库、向量索引、日志或备份。
- `scripts/install_persona_templates.py`
  - 新增模板安装脚本，默认复制模板到 `~/cow/personas`。
  - 支持 `--write-configs` 从 `config-template.json` 生成 `config.json`、`config-chenfeng.json`、`config-haiyang.json`。
  - 已有人格或配置默认跳过，避免覆盖用户本地数据。
- `templates/README.md`
  - 说明模板内容、安装方式和不会提交的真实运行数据。
- `README.md`
  - 重写为当前 ShenZhi 项目说明，覆盖多人格、主控端、模板安装、配置、启动、记忆机制和平台限制。

验证：

- 模板目录安全扫描没有发现真实 key、token、凭证路径、`.env`、SQLite 数据库或运行状态文件。
- `scripts/install_persona_templates.py` 通过内存编译语法检查。
- `git diff --check` 通过。

---

## 2026-06-13 多气泡发送兜底拆分

问题：模型有时只在第一处写 `[MSG]`，后续同段里继续写多句话或用空行换段，发送层不会继续拆分，导致两句话挤在同一个气泡里。

改动：

- `common/message_splitter.py`
  - 新增统一气泡拆分函数。
  - 优先按 `[MSG]` 拆分；短纯文本片段再按空行、换行和中文句末标点做保守兜底。
  - URL、图片/视频标记、Markdown 列表/引用/代码块不自动拆分。
- `channel/chat_channel.py`
  - 通用文本发送前使用统一拆分函数。
- `channel/weixin/weixin_channel.py`
  - 微信直发层复用同一拆分规则。
- `tests/test_message_splitter.py`
  - 覆盖 `[MSG] + 空行问句`、普通短句、无句末标点、URL/Markdown 不拆等场景。

---

## 2026-06-13 人格备份、删除与恢复

- 主控端新增“加载备份人格”。
- 删除人格时可选择“备份后删除”或“直接删除”。
- 新增完整人格备份格式 `*-persona-*.zip`。
- 新增 `/api/persona/backups` 和 `/api/persona/restore`。
- 删除和恢复时会处理配置、日志入口、微信凭证路径和 Web 端口，避免复用旧扫码状态。

---

## 2026-06-13 人格删除与编辑分页

- 人格卡片新增删除入口，默认人格不显示删除按钮。
- 删除确认必须输入人格 ID。
- 编辑弹窗拆成运行配置、个性档案、原始人设三页。
- 顶部标签可直接点击切换。

---

## 2026-06-13 主控端暖色角色卡改版

- 首页改为暖色纸感背景、顶部状态栏、左侧陪伴互动面板和固定底栏。
- 人格列表改为角色卡，展示头像、名称、ID、渠道、模型、访问/追问间隔和操作按钮。
- 新增 `/api/persona/{id}/avatar`，支持 `master/assets/avatar-{id}.png|jpg|jpeg|webp`。
- 修正卡片被左侧互动面板和装饰便签遮挡的问题。

---

## 2026-06-13 新建人格低摩擦填写与一键生成

- 新建人格第一页增加关系、AI 性格、说话风格、相处规则快捷标签。
- 每组标签支持自定义添加。
- 引导表单 / Markdown 编辑旁新增生成模型、API Base、API Key。
- 一键生成前强制校验 AI 名字、用户名字、关系、表情使用、AI 性格、说话风格和生成 API 配置。
- 生成配置同步到第二页运行配置。
- 后端新增 `/api/persona/generate`，调用用户显式填写的 OpenAI-compatible `chat/completions`。

---

## 2026-06-13 原文回忆层

- `agent/memory/conversation_store.py` 新增 `search_messages()` 和 `load_message_context()`。
- 新增 `conversation_search` 和 `conversation_get` 工具。
- prompt 明确区分长期摘要记忆和原始聊天原文回忆。
- 文档补充原文回忆说明。

---

## 2026-06-12 结构化个性化档案 v1

- 新增 `PROFILE.json`，记录关系阶段、称呼、边界、禁忌、风格滑块、主动性、渠道差异等。
- prompt 构建时注入结构化个性化档案。
- 主控端支持编辑个性档案。

---

## 2026-06-12 独白泄漏与 `[SKIP]` 控制标记治理

- 增强独白过滤器。
- 发送、Web 流式、历史入库、历史读取全链路过滤纯独白和纯控制标记。
- 修复 `[MSG]` 前置心理活动会被当作第一条气泡发送的问题。

---

## 2026-06-11 GitHub 首次发布与项目改名

- 项目整理为 `ShenZhi`。
- CLI 统一为 `shenzhi`。
- README、pyproject、文档、gitignore 和贡献说明重写。
- 真实配置、日志、凭证和备份从仓库排除。
