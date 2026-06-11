# 沈知 (ShenZhi)

> 多人格 AI 伴侣平台 —— 每个人格都是一个有独立人设、独立记忆、独立 bot 的"人"，
> 在微信 / Telegram / Web 上与你长期相处。

基于开源项目 [CowAgent](https://github.com/zhayujie/CowAgent)（MIT）二次开发，
在其 Agent 框架之上重构为「AI 伴侣」形态：人格系统、多实例编排、拟人化对话机制与分层记忆。

---

## ✨ 特性

### 🎭 人格系统（Persona）

一个人格 = **人设 + 用户设定 + 长期记忆** 三位一体，互相完全隔离：

```
~/cow/personas/{name}/
├── AGENT.md        # TA 是谁：身份、背景故事、性格、说话风格、对话示例
├── USER.md         # TA 眼中的你：身份、习惯、相处规则
├── MEMORY.md       # 长期记忆（每轮注入，随关系演变）
└── memory/         # 每日日记 + 向量索引 + 对话历史（SQLite）
```

- 人格之间记忆**严格隔离**，绝不串台；切换/并行运行都无缝衔接
- 新建人格零代码：建目录放两个 markdown，或直接用主控端表单生成

### 🖥 主控端（Master Console）

`shenzhi master` 启动，浏览器打开 `http://localhost:9990`：

- **人格管理**：引导表单（名字/关系/背景/性格/说话风格/表情习惯/few-shot）一键生成人设，或直接粘贴 Markdown；随时在线编辑、保存并重启
- **实例编排**：一个人格 = 一个独立 bot 进程，启动/停止/重启，**最多 3 个并行**
- **状态总览**：运行状态、渠道、追问间隔、微信登录状态实时刷新
- **微信扫码**：登录二维码直接在网页弹窗显示，自动刷新
- **日志查看** 与各实例原生控制台直达

### 📱 多渠道

| 渠道 | 说明 |
|---|---|
| **微信** | 腾讯官方 ilink bot（非灰色协议，无封号风险），扫码登录，凭证持久化 |
| **Telegram** | 标准 Bot API，长轮询 + 断线自愈 watchdog，支持语音收发 |
| **Web** | 自带完整控制台：聊天、模型/API key 配置、记忆与知识库管理、调度任务、日志 |

### 💬 拟人化对话

- **`[MSG]` 多气泡**：一次回复拆成多条短消息间隔发出，像真人连发
- **追问机制**：你长时间不回复，TA 会自然地追问一句——延迟可按人格配置（如 10 分钟或 3 小时），带时间感知（上午聊午饭、下午追问会改口"中午吃了啥"），不质问不粘人
- **主动问候**：cron 定时任务，TA 会在固定时间主动找你说话（热加载，改配置即生效）
- **语音**：发语音回语音、发文字回文字（DashScope STT/TTS，多种音色）
- **多模态**：发图片 TA 能看懂并回应（取决于所选模型）

### 🧠 分层记忆

| 层 | 机制 |
|---|---|
| 工作上下文 | 最近 N 轮对话直接在场（默认 30 轮，溢出自动摘要） |
| 每日整合 | 每晚把当天对话蒸馏成日记；**关机漏跑会在下次启动自动补做** |
| 长期记忆 | MEMORY.md 永久事实层，每轮注入；向量 + 关键词混合检索（DashScope embedding） |

每条消息即时落盘 SQLite——**随时关机不丢数据**，适合个人电脑非 7×24 运行。

### 🔧 工程化

- 多实例进程管理（`--instance`），pid/日志/配置按实例隔离
- Telegram polling watchdog（主动探活，不误重启）、微信二维码登录抗网络抖动
- 追问状态持久化（重启不丢计时）、端口冲突自动处理
- 全配置驱动：换模型/换 key/调追问节奏，改 JSON 即可

---

## 🚀 快速开始

```powershell
# 1. 创建并激活 conda 虚拟环境（推荐，Python 3.11）
conda create -n shenzhi python=3.11 -y
conda activate shenzhi

# 2. 安装依赖
pip install -r requirements.txt
pip install -e .          # 注册 shenzhi 命令

# 3. 配置
copy config-template.json config.json
# 编辑 config.json：填入模型 API key、选择渠道（weixin / telegram / web）

# 4. 启动
shenzhi start             # 默认实例
shenzhi master            # 主控端（管理人格与实例）
```

> 不用 conda 也可以：任意 Python 3.10+ 的 venv 均可，后续命令一致。
> 日常使用建议把启动命令写成脚本（激活环境 → `cd` 到项目目录 → `shenzhi start`），开机双击即可。

新建更多人格：打开主控端 → 「＋ 新建人格」→ 填表单 → 启动。
每个人格会得到独立的 `config-{name}.json` 与 `personas/{name}/` 目录。

## 📋 CLI

```
shenzhi start|stop|restart|status|logs [--instance <name>]
shenzhi master [--port 9990] [--stop]
shenzhi skill / knowledge / install-browser ...
```

## ⚙️ 关键配置（config.json / config-{name}.json）

| 字段 | 说明 |
|---|---|
| `bot_type` / `model` | 模型供应商与型号（两者需匹配） |
| `channel_type` | `"weixin"` / `"telegram"` / 逗号分隔多渠道 |
| `active_persona` | 该实例承载的人格（对应 `personas/` 目录名） |
| `followup_first_sec` / `followup_repeat_sec` | 追问延迟区间 `[min, max]` 秒，按实例独立 |
| `telegram_token` | TG bot token（每个人格一个独立 bot） |
| `weixin_credentials_path` | 微信凭证路径（多微信实例必须各自独立） |
| `web_console` / `web_port` | 该实例的 Web 控制台开关与端口 |

模型、语音、embedding、记忆窗口等完整配置见 `config-template.json` 与 `docs/`。

## ⚠️ 平台限制

- **Telegram**：每个人格需在 @BotFather 创建独立 bot；中国大陆网络需自备代理
- **微信**：ilink bot 与**第一个扫码的微信号永久 1:1 绑定**；不支持发送语音气泡
- **人设修改**：AGENT.md / USER.md / config 改动需重启该实例生效（主控端有"保存并重启"）

## 📄 许可与致谢

[MIT License](LICENSE)。基于 [zhayujie/CowAgent](https://github.com/zhayujie/CowAgent) 开发，
保留原作者版权声明；Agent 框架、渠道接入、Web 控制台等基础能力来自上游项目，特此致谢。
