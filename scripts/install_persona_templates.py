"""Install the bundled ShenZhi persona templates.

This script intentionally copies only shareable persona files. Runtime state,
credentials, logs, and memory databases stay outside the repository.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_PERSONAS_DIR = ROOT_DIR / "templates" / "personas"
DEFAULT_WORKSPACE = Path(os.path.expanduser("~/cow"))
PERSONAS = ("shenzhi", "chenfeng", "haiyang")

CONFIG_PRESETS = {
    "shenzhi": {
        "filename": "config.json",
        "overrides": {
            "active_persona": "shenzhi",
            "channel_type": "web",
            "web_console": True,
            "web_port": 9899,
            "followup_first_sec": [1500, 2100],
            "followup_repeat_sec": [3600, 5400],
            "followup_max_count": 6,
        },
    },
    "chenfeng": {
        "filename": "config-chenfeng.json",
        "overrides": {
            "active_persona": "chenfeng",
            "channel_type": "weixin",
            "web_console": False,
            "web_port": 9897,
            "weixin_credentials_path": "~/.weixin_cow_credentials_chenfeng.json",
            "followup_first_sec": [1800, 2400],
            "followup_repeat_sec": [3600, 7200],
            "followup_max_count": 4,
        },
    },
    "haiyang": {
        "filename": "config-haiyang.json",
        "overrides": {
            "active_persona": "haiyang",
            "channel_type": "telegram",
            "web_console": False,
            "web_port": 9898,
            "telegram_token": "",
            "telegram_proxy": "",
            "followup_first_sec": [240, 360],
            "followup_repeat_sec": [1800, 3600],
            "followup_max_count": 20,
        },
    },
}


def _selected_personas(values: list[str] | None) -> list[str]:
    if not values:
        return list(PERSONAS)
    selected: list[str] = []
    for value in values:
        for item in value.split(","):
            name = item.strip().lower()
            if name:
                selected.append(name)
    invalid = sorted(set(selected) - set(PERSONAS))
    if invalid:
        raise SystemExit(f"Unknown persona template: {', '.join(invalid)}")
    return list(dict.fromkeys(selected))


def install_personas(workspace: Path, personas: list[str], force: bool) -> None:
    target_root = workspace.expanduser() / "personas"
    target_root.mkdir(parents=True, exist_ok=True)

    for persona in personas:
        source = TEMPLATE_PERSONAS_DIR / persona
        target = target_root / persona
        if target.exists():
            if not force:
                print(f"skip persona {persona}: {target} already exists")
                continue
            shutil.rmtree(target)
        shutil.copytree(source, target)
        print(f"installed persona {persona}: {target}")


def write_config_presets(config_dir: Path, personas: list[str], force: bool) -> None:
    template_path = ROOT_DIR / "config-template.json"
    base_config = json.loads(template_path.read_text(encoding="utf-8"))
    config_dir.mkdir(parents=True, exist_ok=True)

    for persona in personas:
        preset = CONFIG_PRESETS[persona]
        target = config_dir / preset["filename"]
        if target.exists() and not force:
            print(f"skip config {target.name}: already exists")
            continue

        config = copy.deepcopy(base_config)
        config.update(preset["overrides"])
        target.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote config preset: {target}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=str(DEFAULT_WORKSPACE),
        help="ShenZhi runtime workspace, default: ~/cow",
    )
    parser.add_argument(
        "--persona",
        action="append",
        choices=PERSONAS,
        help="Install one persona. Repeat or omit to install all.",
    )
    parser.add_argument(
        "--force-personas",
        action="store_true",
        help="Overwrite existing target persona directories.",
    )
    parser.add_argument(
        "--write-configs",
        action="store_true",
        help="Also write config.json/config-*.json presets from config-template.json.",
    )
    parser.add_argument(
        "--config-dir",
        default=str(ROOT_DIR),
        help="Where to write config presets, default: repository root.",
    )
    parser.add_argument(
        "--force-configs",
        action="store_true",
        help="Overwrite existing config preset files.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    personas = _selected_personas(args.persona)
    install_personas(Path(args.workspace), personas, args.force_personas)
    if args.write_configs:
        write_config_presets(Path(args.config_dir), personas, args.force_configs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
