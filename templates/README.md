# 人格模板

这里保存可随仓库分发的三个人格模板：

- `shenzhi`：沈知
- `chenfeng`：晨风
- `haiyang`：海洋

每个模板只包含：

- `AGENT.md`：AI 人设、背景故事、说话风格和对话示例
- `USER.md`：模板中的用户画像和相处偏好
- `MEMORY.md`：空长期记忆骨架

没有提交的内容：

- API key、Telegram token、微信登录凭证
- SQLite 记忆库、向量索引、daily dream
- followup 状态、日志、pid、备份包

## 安装

在仓库根目录执行：

```powershell
python scripts/install_persona_templates.py --write-configs
```

脚本会把模板复制到 `~/cow/personas`，并在当前目录生成可编辑的配置文件：

- `config.json`：沈知，默认 Web 渠道，适合先跑通
- `config-chenfeng.json`：晨风，默认微信渠道
- `config-haiyang.json`：海洋，默认 Telegram 渠道

生成后补齐模型 API key 和渠道凭证，再启动对应实例。
