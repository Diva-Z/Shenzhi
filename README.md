# 沈知 (ShenZhi)

沈知是基于 [CowAgent](https://github.com/zhayujie/CowAgent) 二次开发的多人格 AI 伴侣项目。它把原来的 Agent 框架改造成更适合长期陪伴的形态：一个人格对应一套独立人设、独立用户画像、独立长期记忆、独立运行配置和独立 bot 进程。

项目仍保留 CowAgent 的工具、记忆、Web 控制台和多渠道能力，并在此基础上增加了人格模板、主控端、多实例编排、追问节奏、原文回忆和拟人化消息拆分。

## 当前能力

- 多人格：`shenzhi`、`chenfeng`、`haiyang` 等人格可以并行运行，互不串记忆。
- 多渠道：支持 Web、Telegram、微信 ilink bot；同一人格也可以同时开启多个渠道。
- 主控端：`shenzhi master` 打开 `http://localhost:9990`，可统一启动、停止、编辑、备份、恢复和删除人格。
- 人格创建：主控端支持分页表单、关系/性格/说话风格/相处规则标签、自定义标签，以及用户自行填写 API 后的一键生成人设素材。
- 精确回忆：除 `MEMORY.md` 长期记忆外，每条原始对话会落 SQLite；需要核对原话时可用 `conversation_search` 和 `conversation_get` 工具读取原文上下文。
- 分气泡发送：优先按 `[MSG]` 拆分；模型漏写 `[MSG]` 时，会对短句、换行和空行做保守兜底拆分。
- 运行隔离：每个实例有自己的 `config-{name}.json`、pid、日志、追问状态和微信凭证路径。

## 内置人格模板

仓库内置三个可直接安装的人格模板：

| 人格 ID | 名称 | 默认用途 |
|---|---|---|
| `shenzhi` | 沈知 | 默认人格，建议先用 Web 渠道跑通 |
| `chenfeng` | 晨风 | 微信人格模板 |
| `haiyang` | 海洋 | Telegram 人格模板 |

模板位于 `templates/personas/`，每个人格只包含：

- `AGENT.md`：AI 的身份、人设、背景故事、性格、说话风格和对话示例。
- `USER.md`：模板中的用户画像、关系设定和相处偏好。
- `MEMORY.md`：空长期记忆骨架。

仓库不会上传 API key、Telegram token、微信登录凭证、SQLite 记忆库、向量索引、followup 状态、日志或备份包。clone 后需要自己补配置才能运行。

## 快速开始

```powershell
# 1. 克隆并进入项目
git clone https://github.com/Diva-Z/Shenzhi.git
cd Shenzhi

# 2. 创建环境，推荐 Python 3.11
conda create -n shenzhi python=3.11 -y
conda activate shenzhi

# 3. 安装依赖和 CLI
pip install -r requirements.txt
pip install -e .

# 4. 安装三个人格模板，并生成可编辑配置
python scripts/install_persona_templates.py --write-configs
```

脚本会把模板复制到 `~/cow/personas/`，并生成：

- `config.json`：沈知，默认 `web` 渠道，端口 `9899`。
- `config-chenfeng.json`：晨风，默认 `weixin` 渠道。
- `config-haiyang.json`：海洋，默认 `telegram` 渠道。

然后编辑这些配置文件，至少补齐：

| 字段 | 说明 |
|---|---|
| `bot_type` / `model` | 模型厂商和模型名，必须匹配。 |
| `{provider}_api_key` | 所选模型对应的 API key，例如 `deepseek_api_key`、`mimo_api_key`、`dashscope_api_key`。 |
| `{provider}_api_base` | 自定义 API base；不用自定义时保留默认。 |
| `channel_type` | `web`、`telegram`、`weixin`，或逗号分隔的多渠道。 |
| `telegram_token` | Telegram 人格需要填写独立 bot token。 |
| `telegram_proxy` | Telegram 在需要代理的网络环境中填写。 |
| `weixin_credentials_path` | 微信人格必须各自独立，避免复用旧登录凭证。 |
| `web_console` / `web_port` | 是否开启该人格自己的 Web 控制台和端口。 |
| `followup_first_sec` / `followup_repeat_sec` | 首次追问和再次追问的随机等待区间，单位秒。 |

## 启动

```powershell
# 默认人格，读取 config.json
shenzhi start

# 指定人格实例
shenzhi start --instance chenfeng
shenzhi start --instance haiyang

# 主控端
shenzhi master
```

常用命令：

```powershell
shenzhi start|stop|restart|status|logs [--instance <name>]
shenzhi master [--port 9990] [--stop]
```

主控端适合日常使用：打开后可点卡片启动/停止人格、扫码登录微信、查看日志、编辑配置、备份/直接删除人格、从备份加载人格。

## 人格目录

运行时人格数据默认在 `~/cow/personas/{id}/`：

```text
~/cow/personas/{id}/
├── AGENT.md        # 人设、背景故事、说话风格、few-shot
├── USER.md         # 用户画像和相处规则
├── MEMORY.md       # 长期事实记忆
├── PROFILE.json    # 结构化个性化档案
└── memory/         # 原始对话、日记、向量索引等运行数据
```

修改 `AGENT.md`、`USER.md`、`PROFILE.json` 或配置文件后，需要重启对应实例才会生效。`tasks.json` 这类调度任务支持热加载。

## 记忆机制

沈知不是把所有历史对话都塞进 prompt。当前分为四层：

| 层级 | 用途 |
|---|---|
| 工作上下文 | 最近若干轮对话直接进入 prompt。 |
| 原文对话库 | 每条消息即时写入 SQLite，需要核对原话时搜索并读取上下文。 |
| 每日整合 | 定时把当天对话蒸馏成日记；漏跑会在下次启动补做。 |
| 长期记忆 | `MEMORY.md` 保存稳定事实、偏好和关系变化，每轮都会注入。 |

因此它可以保存原始对话并在需要时检索，但不会在每轮都把全部历史逐字发送给模型。这样更稳定，也更省 token。

## 主控端说明

`shenzhi master` 是这个项目的核心操作入口：

- 首页展示所有人格卡片、渠道、模型、追问间隔和运行状态。
- 新建人格分为“人格与故事”和“运行配置”两部分，减少一页表单过载。
- 新建/编辑表单支持关系、性格、说话风格、相处规则的快捷标签和自定义标签。
- 一键生成人设素材时，页面会要求用户填写生成 API base、key 和模型；这些配置不会从环境变量偷取。
- 编辑人格分为运行配置、个性档案、原始人设三页，顶部标签可直接切换。
- 删除人格时可选择“备份后删除”或“直接删除”；备份人格可从主控端重新加载。

## 平台限制

- Telegram：每个人格建议使用独立 BotFather token；部分网络环境需要代理。
- 微信：ilink bot 与首次扫码的微信号长期绑定；不同人格要使用不同 `weixin_credentials_path`。
- Web：最适合先验证模型、人格和记忆逻辑，不需要外部 bot token。
- 模型：`bot_type` 和 `model` 必须匹配，换模型后要重启实例。

## 开发与提交

真实配置和运行数据已经被 `.gitignore` 排除，包括 `config.json`、`config-*.json`、日志、pid、备份和本地记忆数据库。提交前建议检查：

```powershell
git status --short
git diff --check
python -m pytest tests/test_message_splitter.py tests/test_monologue_filter.py -q
```

## 许可

MIT License。基础框架来自 [CowAgent](https://github.com/zhayujie/CowAgent)，本仓库保留原项目许可和致谢。
