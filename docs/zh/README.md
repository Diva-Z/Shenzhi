# 沈知 ShenZhi

沈知是基于 CowAgent 二次开发的多人格 AI 伴侣平台。它保留上游 Agent、工具、技能、知识库、长期记忆、调度器和多渠道能力，并在此基础上加入：

- 多人格：每个人格独立 `AGENT.md`、`USER.md`、`MEMORY.md`。
- 多实例：`config.json` 运行默认人格，`config-<id>.json` 运行命名人格。
- 主控端：`shenzhi master` 管理人格创建、编辑、启动、停止、日志、微信二维码、记忆清理。
- 伴侣式对话：支持 MiMo、多气泡 `[MSG]`、追问、语音、多模态、分层记忆和每日记忆整合。

## 快速开始

推荐 Python 3.11，最低支持 Python 3.10。

```bash
pip install -r requirements.txt
pip install -e .
cp config-template.json config.json
shenzhi start
```

打开主控端：

```bash
shenzhi master
```

默认地址是 `http://127.0.0.1:9990`。

## 多人格实例

默认人格使用 `config.json`：

```bash
shenzhi start
shenzhi logs
```

命名人格使用 `config-<id>.json`：

```bash
shenzhi start --instance chenfeng
shenzhi logs --instance chenfeng
shenzhi stop --instance chenfeng
```

每个人格的数据默认在 `~/cow/personas/<id>/` 下，包括人设、用户设定、长期记忆、短期对话数据库、日记、向量索引和追问状态。

## 安全提示

- 主控端默认只监听本机；如果绑定到公网或局域网地址，请配置 `SHENZHI_MASTER_TOKEN` 或 `master_token`。
- 不要提交 `config.json`、`config-*.json`、`.env`、token、二维码登录凭据或个人记忆数据。
- 清除记忆前主控端会自动备份，但实例必须先停止。

## 上游来源

沈知继承自 CowAgent 的 MIT 开源代码。涉及上游通道、工具、技能、模型和记忆模块时，请保留原许可证说明。
