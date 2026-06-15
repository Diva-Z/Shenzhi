# 沈知 (ShenZhi) 维护日志（精炼版）

> 这里保留面向开发和发布的高信号摘要。更详细记录见 `CHANGES-完整存档.md`。

---

## 2026-06-15 追问 `[SKIP]` 污染历史 + 括号变体漏发（空白回复根因）

现象：沈知在正常聊天里频繁不回复（用户收到"空白"），偶尔还收到一条 `[[SKIP]]`。

根因一（污染）：追问 nudge 的「query」其实是系统指令，里面写着「该跳过就只输出 `[SKIP]`」。这条指令被当作普通 user turn **持久化进对话历史**，每轮作为最近上下文喂回模型，训练模型在普通消息上也吐 `[SKIP]`；发送层正确丢弃 `[SKIP]` → 用户侧一片空白。

根因二（括号变体漏过）：`control_marker_drop_reason` 先用去边界字符集剥掉全角 `【】`，导致 `【SKIP】` 变成裸 `SKIP` 后 `_SKIP_RE` 反而不匹配；而 `[[SKIP]]` 因半角 `[]` 不在字符集、剥掉内层后残留 `[]` 被当成正文 → 直接发给用户。

改动：

- `bridge/agent_bridge.py`
  - `_pre_persist_user_message` 跳过 `is_followup`（连同已有的 `is_scheduled_task`），不再把追问指令存进历史。
  - 运行结束后的持久化：followup 运行额外丢弃打头的 user turn（追问指令），只保留模型真正发出的 nudge 回复。
- `common/monologue_filter.py`
  - `_SKIP_RE` 兼容半/全角与嵌套括号：`[[SKIP]]`、`【SKIP】`、`【[SKIP]】`。
  - 把 SKIP 括号字符（`[]`/`【】`）移出「首次去边界字符集」，改为只在去掉 `[SKIP]` 之后再用 `_CONTROL_RESIDUAL_CHARS` 剥残留括号，避免提前吃掉括号导致漏匹配。
- `tests/test_monologue_filter.py`：补 `[[SKIP]]`/`【SKIP】`/`【[SKIP]】`/`[MSG]+[[SKIP]]` 应丢弃、以及含 `[SKIP]` 的正常解释不误丢。
- 配套：surgical 清掉沈知 / 潮汐历史里已有的追问指令和 `[[SKIP]]` 残留（备份后只删污染条目，保留真实对话）。

---

## 2026-06-15 独白过滤器加固（英文缩写 + 中文第三人称自述）

问题：晨风对话库里仍有大量「把推理独白当成回复正文」的历史（英文 `She's teasing him…`、中文 `…那他就直接定了…他觉得…`），每轮作为最近上下文喂回 prompt，模型照抄格式导致泄漏自我强化。根因是过滤器两处盲区：英文检测要求同时命中 reasoning 起手式 + 窄口径 meta 词，自然叙述（含 `She's`/`He'd` 缩写）漏过；中文检测依赖人格名或固定 marker，独白改用 `他/她` 代词自述时全部落空。

改动：

- `common/monologue_filter.py`
  - 新增 `_looks_like_english_reasoning`：以 CJK 占比判定「基本是英文」，长英文且第三人称/第一人称起手即判独白（中文人格不会真的用英文回复）。
  - 新增 `_looks_like_chinese_self_narration`：检测 `(他|她)(会|就|觉得|想…)` 第三人称自述，≥3 次或 ≥2 次且带分析措辞即判独白。
  - `_strip_full_english_reasoning` 丢弃条件从 `not _has_cjk` 改为 CJK 占比阈值，避免独白里夹个人格名就保命。
  - 两个检测接入 `_looks_like_reasoning`，发送 / 入库 / 历史读取全链路生效。
- `tests/test_monologue_filter.py`：补英文缩写独白、含人格名英文独白、中文第三人称自述、以及「正常回复里提到第三方"他"不应误删」四个用例。
- 配套清空晨风被污染的原始对话历史（运行数据，不入库）。

---

## 2026-06-15 孤立 `</think>` 标签泄漏与 CLI GBK 崩溃

问题一：用户侧仍会收到夹在正文里的裸 `</think>` 标签。根因是 `_filter_think_tags` 按「每个流式 delta」过滤，`<think>` 与配对的 `</think>` 常分散在不同 chunk：落单的 `<think>` 被「未闭合尾部」规则清掉，落单的 `</think>` 不匹配任何规则原样泄漏；message_end 的整体复扫此时已无开标签可配对。

问题二：`shenzhi restart` 在裸 GBK 控制台（未设 `PYTHONUTF8`）必崩。`stop` 末尾打印的 `✓`(U+2713) 不在 GBK 字符集，触发 `UnicodeEncodeError`，异常发生在 stop 之后、start 之前，导致实例被停掉却没拉起。

改动：

- `agent/protocol/agent_stream.py`：`_filter_think_tags` 在去成对块和未闭合尾部后，再清掉残留的孤立 `<think>` / `</think>` 标签，并放宽大小写与空白变体。
- `cli/cli.py`：入口处把 stdout/stderr 错误处理器改成 `replace`（保留控制台原生编码，中文照常），无法编码的状态符号降级为 `?` 而非抛异常；兼容 `strict` 与 `surrogateescape`。

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
