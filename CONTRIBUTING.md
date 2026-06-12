# Contributing to ShenZhi

ShenZhi is a multi-persona AI companion platform forked from CowAgent. The codebase keeps CowAgent's agent, channel, tool, skill, memory, and scheduler foundations, and adds persona-oriented runtime isolation plus the master console.

## Local Setup

Use Python 3.10 or newer. Python 3.11 is the recommended development version.

```bash
git clone <your-shenzhi-repo>
cd ShenZhi
pip install -r requirements.txt
pip install -e .
shenzhi start
```

For multi-persona work, use `config.json` for the default persona and `config-<name>.json` for named instances:

```bash
shenzhi start --instance chenfeng
shenzhi logs --instance chenfeng
shenzhi master
```

## Before Submitting Changes

- Keep persona memory and runtime state isolated by persona or instance.
- Do not log raw API keys, tokens, passwords, cookies, or credential paths.
- Add targeted tests for risky behavior, especially memory, filtering, scheduler, channel state, and master-console APIs.
- Avoid changing existing user data paths unless there is a migration plan.

## Project Notes

- `~/cow` remains the default user data directory for compatibility.
- The master console manages persona files under `personas/<id>/` and instance configs under `config-<id>.json`.
- The upstream CowAgent heritage is MIT licensed; keep license notices intact when modifying inherited code.
