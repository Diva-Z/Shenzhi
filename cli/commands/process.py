"""cow start/stop/restart/status/logs - Process management commands."""

import json
import os
import sys
import subprocess
import time
from typing import Optional

import click

from cli.utils import get_project_root, load_config_json

_IS_WIN = sys.platform == "win32"


def _is_terminal_only() -> bool:
    """Whether terminal is the only configured channel.

    Terminal needs an interactive stdin/tty, which is incompatible with the
    background daemon mode (stdout/stdin detached). When terminal is the only
    channel, `start` must run in the foreground so it can own the tty.
    """
    channel = load_config_json().get("channel_type", "")
    if isinstance(channel, str):
        names = [c.strip() for c in channel.split(",") if c.strip()]
    elif isinstance(channel, (list, tuple)):
        names = [str(c).strip() for c in channel if str(c).strip()]
    else:
        names = []
    return names == ["terminal"]


def _instance_suffix(instance: Optional[str]) -> str:
    """Empty for the default instance, '-<name>' otherwise. Lets a second
    persona run as its own bot/process with isolated pid/log/config files."""
    if not instance or instance == "default":
        return ""
    return f"-{instance}"


def _get_pid_file(instance: Optional[str] = None):
    return os.path.join(get_project_root(), f".shenzhi{_instance_suffix(instance)}.pid")


def _get_log_file(instance: Optional[str] = None):
    return os.path.join(get_project_root(), f"shenzhi{_instance_suffix(instance)}.out")


def _get_config_file(instance: Optional[str] = None) -> str:
    """Config filename for an instance: config.json for default,
    config-<name>.json otherwise."""
    if not instance or instance == "default":
        return "config.json"
    return f"config-{instance}.json"


LOG_ROTATE_BYTES = 10 * 1024 * 1024  # rotate at 10 MB on start
LOG_ROTATE_KEEP = 3  # shenzhi.out.1 (newest) .. shenzhi.out.3 (oldest)


def _rotate_log(log_file: str):
    """Archive the log on start when it has grown past LOG_ROTATE_BYTES.

    shenzhi.out -> shenzhi.out.1 -> ... -> shenzhi.out.N, oldest dropped.
    Only called while the instance is stopped, so no writer holds the file.
    """
    try:
        if not os.path.exists(log_file) or os.path.getsize(log_file) < LOG_ROTATE_BYTES:
            return
        oldest = f"{log_file}.{LOG_ROTATE_KEEP}"
        if os.path.exists(oldest):
            os.remove(oldest)
        for i in range(LOG_ROTATE_KEEP - 1, 0, -1):
            src = f"{log_file}.{i}"
            if os.path.exists(src):
                os.replace(src, f"{log_file}.{i + 1}")
        os.replace(log_file, f"{log_file}.1")
        click.echo(f"Rotated large log to {os.path.basename(log_file)}.1")
    except OSError as e:
        click.echo(f"Log rotation skipped: {e}")


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process is still running (cross-platform)."""
    if _IS_WIN:
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL,
            )
            return str(pid) in out.decode(errors="ignore")
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


def _kill_pid(pid: int, force: bool = False):
    """Terminate a process by PID (cross-platform)."""
    if _IS_WIN:
        flag = "/F" if force else ""
        cmd = ["taskkill"]
        if force:
            cmd.append("/F")
        cmd.extend(["/PID", str(pid)])
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.kill(pid, sig)


def _read_pid(instance: Optional[str] = None) -> Optional[int]:
    pid_file = _get_pid_file(instance)
    if not os.path.exists(pid_file):
        return None
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
        if _is_pid_alive(pid):
            return pid
        os.remove(pid_file)
        return None
    except (ValueError, OSError):
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return None


def _write_pid(pid: int, instance: Optional[str] = None):
    with open(_get_pid_file(instance), "w") as f:
        f.write(str(pid))


def _remove_pid(instance: Optional[str] = None):
    pid_file = _get_pid_file(instance)
    if os.path.exists(pid_file):
        os.remove(pid_file)


@click.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't daemonize)")
@click.option("--no-logs", is_flag=True, help="Don't tail logs after starting")
@click.option("--instance", default=None, help="Named instance (loads config-<name>.json with its own pid/log). Default uses config.json.")
def start(foreground, no_logs, instance):
    """Start ShenZhi."""
    pid = _read_pid(instance)
    if pid:
        label = f" [{instance}]" if instance else ""
        click.echo(f"ShenZhi{label} is already running (PID: {pid}).")
        return

    root = get_project_root()
    app_py = os.path.join(root, "app.py")
    if not os.path.exists(app_py):
        click.echo("Error: app.py not found in project root.", err=True)
        sys.exit(1)

    # Resolve the instance config file and pass it to the child via SHENZHI_CONFIG.
    child_env = dict(os.environ)
    if instance:
        config_file = _get_config_file(instance)
        config_path = os.path.join(root, config_file)
        if not os.path.exists(config_path):
            click.echo(f"Error: config file '{config_file}' not found for instance '{instance}'.", err=True)
            sys.exit(1)
        child_env["SHENZHI_CONFIG"] = config_path

    python = sys.executable

    # Terminal-only setups need an interactive tty; force foreground so the
    # terminal channel can read stdin instead of fighting the shell over the tty.
    if not foreground and _is_terminal_only():
        foreground = True
        click.echo("Detected terminal-only channel, starting in foreground...")

    label = f" [{instance}]" if instance else ""
    if foreground:
        click.echo(f"Starting ShenZhi{label} in foreground...")
        if _IS_WIN:
            sys.exit(subprocess.call([python, app_py], cwd=root, env=child_env))
        else:
            os.environ.update(child_env)
            os.execv(python, [python, app_py])
    else:
        log_file = _get_log_file(instance)
        _rotate_log(log_file)
        click.echo(f"Starting ShenZhi{label}...")

        popen_kwargs = dict(cwd=root, env=child_env)
        if _IS_WIN:
            CREATE_NO_WINDOW = 0x08000000
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            )
        else:
            popen_kwargs["start_new_session"] = True

        with open(log_file, "a") as log:
            proc = subprocess.Popen(
                [python, app_py],
                stdout=log,
                stderr=log,
                **popen_kwargs,
            )
        _write_pid(proc.pid, instance)
        click.echo(click.style(f"✓ ShenZhi{label} started (PID: {proc.pid})", fg="green"))
        click.echo(f"  Logs: {log_file}")

        if not no_logs:
            click.echo("  Press Ctrl+C to stop tailing logs.\n")
            _tail_log(log_file)


@click.command()
@click.option("--instance", default=None, help="Named instance to stop (default uses config.json).")
def stop(instance):
    """Stop ShenZhi."""
    pid = _read_pid(instance)
    label = f" [{instance}]" if instance else ""
    if not pid:
        click.echo(f"ShenZhi{label} is not running.")
        return

    click.echo(f"Stopping ShenZhi{label} (PID: {pid})...")
    try:
        _kill_pid(pid)
        for _ in range(30):
            time.sleep(0.1)
            if not _is_pid_alive(pid):
                break
        else:
            _kill_pid(pid, force=True)
    except (ProcessLookupError, OSError):
        pass

    _remove_pid(instance)
    click.echo(click.style(f"✓ ShenZhi{label} stopped.", fg="green"))


@click.command()
@click.option("--no-logs", is_flag=True, help="Don't tail logs after restarting")
@click.option("--instance", default=None, help="Named instance to restart (default uses config.json).")
@click.pass_context
def restart(ctx, no_logs, instance):
    """Restart ShenZhi."""
    ctx.invoke(stop, instance=instance)
    time.sleep(1)
    ctx.invoke(start, no_logs=no_logs, instance=instance)


@click.command()
@click.pass_context
def update(ctx):
    """Update ShenZhi and restart."""
    root = get_project_root()

    # 1. Stop service first so git pull won't conflict with running code
    ctx.invoke(stop)

    # 2. Git pull
    if os.path.isdir(os.path.join(root, ".git")):
        click.echo("Pulling latest code...")
        ret = subprocess.call(["git", "pull"], cwd=root)
        if ret != 0:
            click.echo("Error: git pull failed.", err=True)
            sys.exit(1)
    else:
        click.echo("Not a git repository, skipping code update.")

    python = sys.executable
    req_file = os.path.join(root, "requirements.txt")

    if _IS_WIN:
        # On Windows, `cow.exe` (this process) locks the exe file, so
        # `pip install -e .` fails with WinError 5.  Write a small .bat
        # helper that waits for cow.exe to exit, then installs & starts.
        bat = os.path.join(root, "_cow_update.bat")
        lines = [
            "@echo off",
            "chcp 65001 >nul",
            "echo Waiting for cow.exe to exit...",
            "timeout /t 3 /nobreak >nul",
        ]
        if os.path.exists(req_file):
            lines.append(f'echo Installing dependencies...')
            lines.append(f'"{python}" -m pip install -r requirements.txt -q')
        lines += [
            "echo Reinstalling cow CLI...",
            f'"{python}" -m pip install -e . -q',
            "echo Starting ShenZhi...",
            f'"{python}" -m cli.cli start --no-logs',
            "echo.",
            "echo Update complete. You can close this window.",
            "pause >nul",
            "del \"%~f0\"",
        ]
        with open(bat, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        subprocess.Popen(
            ["cmd.exe", "/c", "start", "ShenZhi Update", "/wait", bat],
            cwd=root,
        )
        click.echo(click.style(
            "✓ Update script launched. Please follow the new window for progress.",
            fg="green"))
    else:
        # 3. Install dependencies
        if os.path.exists(req_file):
            click.echo("Installing dependencies...")
            subprocess.call(
                [python, "-m", "pip", "install", "-r", "requirements.txt", "-q"],
                cwd=root,
            )
        click.echo("Reinstalling cow CLI...")
        subprocess.call(
            [python, "-m", "pip", "install", "-e", ".", "-q"],
            cwd=root,
        )

        # 4. Start service
        click.echo("")
        time.sleep(1)
        ctx.invoke(start, no_logs=False)


@click.command()
@click.option("--instance", default=None, help="Named instance to inspect (default uses config.json).")
def status(instance):
    """Show ShenZhi running status."""
    from cli import __version__
    from cli.utils import load_config_json, get_cli_language, get_project_root

    # get_cli_language() calls ensure_sys_path(), which adds the project root
    # to sys.path. Import `common` only AFTER that, otherwise it fails with
    # ModuleNotFoundError when `cow` runs from outside the project dir.
    get_cli_language()  # resolve cow_lang so i18n.t reflects config
    from common import i18n
    _t = i18n.t

    pid = _read_pid(instance)
    label = f" [{instance}]" if instance else ""
    if pid:
        click.echo(click.style(f"● ShenZhi{label} is running (PID: {pid})", fg="green"))
    else:
        click.echo(click.style(f"● ShenZhi{label} is not running", fg="red"))

    click.echo(_t(f"  版本: v{__version__}", f"  Version: v{__version__}"))

    # Project path bound to this `cow` CLI — disambiguates which checkout the
    # command actually controls when the user has multiple clones.
    project_root = get_project_root()
    click.echo(_t(f"  路径: {project_root}", f"  Path: {project_root}"))

    if instance and instance != "default":
        # Named instances run from config-<name>.json, not config.json
        cfg = {}
        instance_cfg_path = os.path.join(project_root, _get_config_file(instance))
        try:
            with open(instance_cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            click.echo(click.style(f"  (config not found: {_get_config_file(instance)})", fg="yellow"))
    else:
        cfg = load_config_json()
    if cfg:
        channel = cfg.get("channel_type", "unknown")
        if isinstance(channel, list):
            channel = ", ".join(channel)
        click.echo(_t(f"  通道: {channel}", f"  Channel: {channel}"))
        click.echo(_t(f"  模型: {cfg.get('model', 'unknown')}", f"  Model: {cfg.get('model', 'unknown')}"))
        mode = "Chat" if cfg.get("agent") is False else "Agent"
        click.echo(_t(f"  模式: {mode}", f"  Mode: {mode}"))
        lang_label = "中文" if i18n.get_language() == "zh" else "English"
        click.echo(_t(f"  语言: {lang_label}", f"  Language: {lang_label}"))


@click.command()
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@click.option("--lines", "-n", default=50, help="Number of lines to show")
@click.option("--instance", default=None, help="Named instance whose logs to show (default uses config.json).")
def logs(follow, lines, instance):
    """View ShenZhi logs."""
    log_file = _get_log_file(instance)
    if not os.path.exists(log_file):
        click.echo("No log file found.")
        return

    if follow:
        _tail_log(log_file, lines)
    else:
        _print_last_lines(log_file, lines)


def _print_last_lines(file_path: str, n: int = 50):
    """Print the last N lines of a file (cross-platform)."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        for line in all_lines[-n:]:
            click.echo(line, nl=False)
    except Exception as e:
        click.echo(f"Error reading log file: {e}", err=True)


def _tail_log(log_file: str, lines: int = 50):
    """Follow log file output. Blocks until Ctrl+C (cross-platform)."""
    _print_last_lines(log_file, lines)

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)
            while True:
                line = f.readline()
                if line:
                    click.echo(line, nl=False)
                else:
                    time.sleep(0.3)
    except KeyboardInterrupt:
        pass
