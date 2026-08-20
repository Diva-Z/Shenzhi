"""cow health - System health and memory-state diagnostics.

Reads the on-disk memory pipeline state and the shared long-term SQLite index
(``index.db`` holds the memory chunks/files, the conversation sessions/messages
and the identity tables), then reports a health snapshot. Everything is read
straight from disk so the command works whether or not an instance is running.
"""

import json
import sqlite3
from pathlib import Path

import click

from cli.utils import get_project_root


def _load_conf():
    """Return the config accessor, importing the project package lazily."""
    import sys
    sys.path.insert(0, get_project_root())
    from config import conf, load_config
    load_config()
    return conf


def _resolve_base(persona):
    """Resolve the persona/workspace root that owns the memory directory.

    Mirrors agent/memory/config._default_workspace(): an active persona roots
    the workspace under ``{agent_workspace}/personas/{persona}/``.
    """
    from common.utils import expand_path
    conf = _load_conf()
    workspace = Path(expand_path(conf().get("agent_workspace", "~/cow")))
    persona = persona or (conf().get("active_persona") or "").strip()
    base = workspace / "personas" / persona if persona else workspace
    return base, (persona or "(default)")


def _count(db_path, sql):
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _build_report(persona):
    base, persona_label = _resolve_base(persona)
    memory_dir = base / "memory"
    long_term_dir = memory_dir / "long-term"
    # Single shared DB: memory chunks/files + conversation sessions/messages +
    # identity tables all live in long-term/index.db.
    index_db = long_term_dir / "index.db"

    report = {"persona": persona_label, "workspace": str(base)}

    # Pipeline dedup hashes (.memory_pipeline_state.json) + daily flush progress
    # (.daily_flush_state.json). These are two separate files.
    state_file = memory_dir / ".memory_pipeline_state.json"
    flush_file = memory_dir / ".daily_flush_state.json"
    if state_file.exists() or flush_file.exists():
        ps = {"committed_hashes": 0, "last_flush_date": "", "failed_dates": []}
        try:
            if state_file.exists():
                state = json.loads(state_file.read_text(encoding="utf-8"))
                ps["committed_hashes"] = len(state.get("trim_flushed_hashes", []))
            if flush_file.exists():
                flush = json.loads(flush_file.read_text(encoding="utf-8"))
                ps["last_flush_date"] = flush.get("last_flush_date", "") or ""
                ps["failed_dates"] = flush.get("failed_dates", []) or []
            report["pipeline_state"] = ps
        except Exception as e:
            report["pipeline_state"] = {"error": str(e)}
    else:
        report["pipeline_state"] = None

    # Conversation store stats (sessions/messages).
    if index_db.exists():
        try:
            report["conversations"] = {
                "session_count": _count(index_db, "SELECT COUNT(*) FROM sessions"),
                "total_messages": _count(index_db, "SELECT COUNT(*) FROM messages"),
                "db_size_bytes": index_db.stat().st_size,
            }
        except Exception as e:
            report["conversations"] = {"error": str(e)}

        # Memory index stats (chunks/files) live in the same DB.
        try:
            report["memory_index"] = {
                "chunk_count": _count(index_db, "SELECT COUNT(*) FROM chunks"),
                "file_count": _count(index_db, "SELECT COUNT(*) FROM files"),
                "db_size_bytes": index_db.stat().st_size,
            }
        except Exception as e:
            report["memory_index"] = {"error": str(e)}

        # Identity stats (identities/bindings) also share the DB.
        try:
            report["identity"] = {
                "identity_count": _count(index_db, "SELECT COUNT(*) FROM identities"),
                "binding_count": _count(index_db, "SELECT COUNT(*) FROM identity_bindings"),
            }
        except Exception as e:
            report["identity"] = {"error": str(e)}
    else:
        report["conversations"] = None
        report["memory_index"] = None
        report["identity"] = None

    # Daily memory files (YYYY-MM-DD.md).
    daily_files = list(memory_dir.glob("????-??-??.md")) if memory_dir.exists() else []
    report["daily_memory_files"] = len(daily_files)
    if daily_files:
        report["latest_daily"] = sorted(daily_files)[-1].stem

    return report


def _print_human(report):
    click.echo(f"\n{'=' * 50}")
    click.echo("  Shenzhi Health Report")
    click.echo(f"  Persona: {report['persona']}")
    click.echo(f"  Workspace: {report['workspace']}")
    click.echo(f"{'=' * 50}\n")

    ps = report.get("pipeline_state")
    if ps and "error" not in ps:
        click.echo("  Memory Pipeline:")
        click.echo(f"    Committed hashes: {ps['committed_hashes']}")
        click.echo(f"    Last flush date:  {ps['last_flush_date'] or 'never'}")
        failed = ps.get("failed_dates", [])
        click.echo(f"    Failed dates:     {', '.join(failed) if failed else 'none'}")
    else:
        click.echo("  Memory Pipeline: not initialized")

    conv = report.get("conversations")
    if conv and "error" not in conv:
        click.echo("\n  Conversations:")
        click.echo(f"    Sessions: {conv['session_count']}")
        click.echo(f"    Messages: {conv['total_messages']}")
        click.echo(f"    DB size:  {conv['db_size_bytes'] / 1024:.1f} KB")

    mi = report.get("memory_index")
    if mi and "error" not in mi:
        click.echo("\n  Memory Index:")
        click.echo(f"    Chunks: {mi['chunk_count']}")
        click.echo(f"    Files:  {mi['file_count']}")
        click.echo(f"    DB size: {mi['db_size_bytes'] / 1024:.1f} KB")

    ident = report.get("identity")
    if ident and "error" not in ident:
        click.echo("\n  Identity:")
        click.echo(f"    Identities: {ident['identity_count']}")
        click.echo(f"    Bindings:   {ident['binding_count']}")

    click.echo(f"\n  Daily memory files: {report.get('daily_memory_files', 0)}")
    if report.get("latest_daily"):
        click.echo(f"  Latest daily: {report['latest_daily']}")
    click.echo("")


@click.command()
@click.option("--persona", default="", help="Persona name (default: active persona).")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON.")
def health(persona, json_output):
    """Show system health and memory state."""
    report = _build_report(persona)
    if json_output:
        click.echo(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_human(report)
