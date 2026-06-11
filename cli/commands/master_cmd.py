"""shenzhi master - persona orchestration console (web UI)."""

import os
import subprocess
import sys
import time

import click

from cli.utils import get_project_root
from cli.commands.process import _is_pid_alive, _kill_pid

_IS_WIN = sys.platform == "win32"
PID_FILE = ".shenzhi-master.pid"
LOG_FILE = "shenzhi-master.out"


def _pid_path():
    return os.path.join(get_project_root(), PID_FILE)


def _read_master_pid():
    path = _pid_path()
    try:
        with open(path, "r") as f:
            pid = int(f.read().strip())
        if _is_pid_alive(pid):
            return pid
    except (OSError, ValueError):
        pass
    try:
        os.remove(path)
    except OSError:
        pass
    return None


@click.command()
@click.option("--port", default=9990, help="Master console port (default 9990).")
@click.option("--host", default="127.0.0.1", help="Bind address (default localhost only).")
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground.")
@click.option("--stop", is_flag=True, help="Stop the running master console.")
def master(port, host, foreground, stop):
    """Start the ShenZhi master console (persona management web UI)."""
    root = get_project_root()

    if stop:
        pid = _read_master_pid()
        if pid:
            _kill_pid(pid, force=True)
            try:
                os.remove(_pid_path())
            except OSError:
                pass
            click.echo(click.style("✓ 主控端已停止", fg="green"))
        else:
            click.echo("主控端未运行")
        return

    url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}"
    if _read_master_pid():
        click.echo(f"主控端已在运行: {url}")
        return

    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["SHENZHI_MASTER_HOST"] = host
    env["SHENZHI_MASTER_PORT"] = str(port)

    if foreground:
        sys.exit(subprocess.call([sys.executable, "-m", "master"], cwd=root, env=env))

    log_file = os.path.join(root, LOG_FILE)
    popen_kwargs = dict(cwd=root, env=env)
    if _IS_WIN:
        CREATE_NO_WINDOW = 0x08000000
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True
    with open(log_file, "a") as log:
        proc = subprocess.Popen([sys.executable, "-m", "master"],
                                stdout=log, stderr=log, **popen_kwargs)
    with open(_pid_path(), "w") as f:
        f.write(str(proc.pid))
    time.sleep(1.5)
    if not _is_pid_alive(proc.pid):
        click.echo(click.style(f"✗ 主控端启动失败，查看日志: {log_file}", fg="red"))
        sys.exit(1)
    click.echo(click.style(f"✓ 主控端已启动 (PID: {proc.pid})", fg="green"))
    click.echo(f"  地址: {url}")
    click.echo(f"  日志: {log_file}")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
