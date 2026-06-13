# 沈知 (ShenZhi) 改动记录

> 这里记录较完整的维护过程，便于后续继续排查和迭代。精炼摘要见 `CHANGES.md`。

---

## 2026-06-13 GitHub 模板化发布：晨风、海洋、沈知作为可复用人格模板

### 目标

把当前已经调好的三个核心人格“沈知 / 晨风 / 海洋”做成仓库内可分发模板。新用户从 GitHub clone 后，不需要手工从本机 `~/cow/personas` 拷贝人格数据，只需要执行安装脚本、补模型和渠道配置，就能启动对应人格。

同时保证模板发布不泄露真实运行数据：

- 不上传 API key、Telegram token、微信登录凭证；
- 不上传 `.env`；
- 不上传 SQLite 对话库、向量索引、daily dream、followup 状态；
- 不上传运行日志、pid、备份包。

### 模板目录

新增：

```text
templates/personas/
├── shenzhi/
│   ├── AGENT.md
│   ├── USER.md
│   └── MEMORY.md
├── chenfeng/
│   ├── AGENT.md
│   ├── USER.md
│   └── MEMORY.md
└── haiyang/
    ├── AGENT.md
    ├── USER.md
    └── MEMORY.md
```

处理原则：

- `AGENT.md` / `USER.md` 来自当前已经调过的人格模板；
- `MEMORY.md` 使用空长期记忆骨架，避免把真实对话沉淀上传到仓库；
- 未复制各人格目录中的 `.env`、`memory/`、`followup_state.json`、`weixin_followup_state.json` 等运行文件。

### 安装脚本

新增 `scripts/install_persona_templates.py`。

功能：

- 默认把三个人格模板复制到 `~/cow/personas`；
- 支持 `--persona shenzhi|chenfeng|haiyang` 只安装某一个人格；
- 已存在目标人格时默认跳过，避免覆盖用户数据；
- 需要覆盖时必须显式传 `--force-personas`；
- 传 `--write-configs` 时，会从 `config-template.json` 生成三份可编辑配置：
  - `config.json`：沈知，默认 Web 渠道；
  - `config-chenfeng.json`：晨风，默认微信渠道；
  - `config-haiyang.json`：海洋，默认 Telegram 渠道；
- 已存在配置时默认跳过，避免覆盖用户手填 key；
- 需要覆盖配置时必须显式传 `--force-configs`。

示例：

```powershell
python scripts/install_persona_templates.py --write-configs
python scripts/install_persona_templates.py --persona shenzhi --write-configs
```

### README 重写

重写 `README.md`，使它贴合当前项目，而不是继续停留在早期 CowAgent 改造说明。

新 README 覆盖：

- 当前项目定位；
- 多人格、多渠道、主控端、人格创建、原文回忆、分气泡发送等当前能力；
- 内置三个人格模板；
- clone 后的快速开始流程；
- `scripts/install_persona_templates.py --write-configs` 的使用方式；
- 需要补齐的配置字段；
- 启动默认人格、指定人格和主控端的命令；
- 运行时人格目录结构；
- 当前四层记忆机制；
- 主控端的备份、恢复、删除和一键生成说明；
- Telegram、微信、Web 和模型配置限制；
- 提交前检查建议。

### 模板说明文档

新增 `templates/README.md`，说明：

- 三个模板 ID；
- 每个模板包含哪些文件；
- 哪些真实运行数据不会进入仓库；
- 安装脚本如何使用；
- 安装后会生成哪些配置文件。

### 安全边界

本轮没有把当前真实 `config.json` / `config-*.json` 上传为模板，因为这些文件属于用户机器上的运行配置，且可能包含真实 key、token、渠道绑定和端口状态。

正确流程是：

1. 仓库保存人格模板；
2. 安装脚本从 `config-template.json` 生成空 key 配置；
3. 用户 clone 后自行补 key、token、proxy、微信凭证路径；
4. 真实运行数据继续由 `.gitignore` 排除。

### 验证

- 扫描 `templates/`，除模板 README 的说明文字外，没有发现 key、token、凭证路径、`.env`、SQLite 数据库或运行状态文件。
- `scripts/install_persona_templates.py` 通过内存编译语法检查。
- `git diff --check` 通过。

---

## 2026-06-13 多气泡发送兜底拆分

问题：沈知、潮汐等人格要求用 `[MSG]` 拆成短消息，但模型有时只在第一处写 `[MSG]`，后续同段里继续写两句话或用空行换段，发送层不会继续拆分，导致第二个气泡仍然显得拥挤。

改动：

- `common/message_splitter.py`
  - 新增统一文本气泡拆分函数。
  - 优先按 `[MSG]` 硬拆。
  - 每个短的纯文本片段再按空行、换行和中文句末标点做保守兜底拆分。
  - 对 URL、图片/视频标记、Markdown 列表/引用/代码块等内容不做自动拆分。
- `channel/chat_channel.py`
  - 通用文本发送前使用统一拆分函数。
- `channel/weixin/weixin_channel.py`
  - 微信直接发送层复用同一拆分函数，避免和通用发送层规则不一致。
- `tests/test_message_splitter.py`
  - 覆盖 `[MSG] + 空行问句`、普通短句、无句末标点、URL/Markdown 不拆等场景。

---

## 2026-06-13 主控端近期补齐

本日主控端围绕“多人格伴侣”做了一组连续补齐：

- 首页改成暖色角色卡界面，减少开发后台感；
- 修正卡片定位和装饰便签遮挡问题；
- 新建人格从单页改成两步；
- 新建人格增加关系、性格、说话风格、相处规则标签和自定义标签；
- 一键生成要求用户显式填写生成 API，不从环境变量或默认配置偷取 key；
- 编辑人格拆成运行配置、个性档案、原始人设三页；
- 顶部标签可直接切换编辑页；
- 新增删除整个人格；
- 删除时可选择备份后删除或直接删除；
- 新增加载备份人格；
- 复制人格时自动重命名标题和结构化称呼，避免新人格继续自称源人格；
- 修复卡片按钮“日志”误写成“状态记”。

---

## 2026-06-13 原文回忆层

在长期摘要记忆之外新增“精确原话回忆”：

- `conversation_store.search_messages()` 用关键词/短语搜索 SQLite 原始消息；
- `conversation_store.load_message_context()` 根据 `session_id + seq` 读取前后文；
- `conversation_search` 工具负责找候选原句；
- `conversation_get` 工具负责读取上下文；
- prompt 明确区分 `memory_search` 与 `conversation_search` / `conversation_get` 的用途。

设计原则：

- `MEMORY.md` 继续保存稳定事实、偏好、关系和规则；
- 需要核对“之前到底说过什么”时再读取原始对话；
- 先用 `LIKE + 文本抽取 + 简单评分`，暂不引入 FTS/向量化原文索引，降低破坏现有数据库的风险。

---

## 2026-06-12 结构化个性化档案 v1

新增 `PROFILE.json`，让每个人格除了 `AGENT.md` 和 `USER.md` 外，还能有结构化的个性化档案。

字段包括：

- 关系阶段；
- AI / 用户称呼；
- 边界和禁忌；
- 温暖度、幽默、调侃、逻辑感、主动性；
- 回复长度和表情密度；
- 当前 mood；
- 主动关怀开关、免打扰时间、每日上限、主动场景；
- Telegram / 微信 / Web 渠道差异；
- 其他偏好。

prompt 构建时会读取并渲染 `PROFILE.json`。文件不存在或损坏时不阻断启动。

---

## 2026-06-12 独白泄漏与控制标记治理

围绕模型把“心理活动/推理独白”泄漏到正式回复的问题做了全链路治理：

- 增强 `common/monologue_filter.py`；
- 发送前剥离独白；
- Web 流式输出过滤控制标记；
- 入库前过滤纯独白和纯 `[SKIP]`；
- 历史读取时再次清理污染内容；
- 修复 `[MSG]` 前置心理活动会被当作第一条气泡发送的问题。

目标是阻断“泄漏内容进入历史后被模型继续模仿”的循环。

---

## 2026-06-11 项目整理为 ShenZhi

- 项目从 CowAgent 的本地改造整理为 `ShenZhi`。
- CLI 统一为 `shenzhi`。
- README、pyproject、文档、gitignore 和贡献说明重写。
- 真实配置、日志、凭证、备份和本地运行数据从仓库排除。
- 首次发布到 GitHub：`https://github.com/Diva-Z/Shenzhi`。
