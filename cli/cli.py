"""ShenZhi CLI entry point."""

import sys

import click
from cli import __version__


def _make_console_output_safe():
    """Keep CLI output from crashing on legacy consoles (e.g. Windows GBK).

    Status lines use glyphs like ``✓`` / ``●`` that a GBK console cannot encode.
    Without this, ``stop``'s final "✓ stopped" echo raises UnicodeEncodeError,
    which aborts ``restart`` *after* the stop but *before* the start — leaving
    the instance down. Keep the console's native encoding so Chinese text still
    renders correctly; only soften the error handler so an un-encodable symbol
    degrades to '?' instead of raising.
    """
    safe_handlers = ("replace", "ignore", "backslashreplace", "xmlcharrefreplace")
    for stream in (sys.stdout, sys.stderr):
        try:
            reconfigure = getattr(stream, "reconfigure", None)
            # 'strict' raises; 'surrogateescape' also raises on a real non-GBK
            # glyph like ✓. Only leave already-permissive handlers untouched.
            if reconfigure is not None and (getattr(stream, "errors", None) or "") not in safe_handlers:
                reconfigure(errors="replace")
        except Exception:
            pass


_make_console_output_safe()
from cli.commands.skill import skill
from cli.commands.process import start, stop, restart, update, status, logs
from cli.commands.context import context
from cli.commands.install import install_browser
from cli.commands.knowledge import knowledge
from cli.commands.master_cmd import master


HELP_TEXT = """Usage: shenzhi COMMAND [ARGS]...

  ShenZhi CLI - Manage your ShenZhi instance.

Commands:
  help     Show this message.
  version  Show the version.
  start    Start ShenZhi.
  stop     Stop ShenZhi.
  restart  Restart ShenZhi.
  update   Update ShenZhi and restart.
  status   Show ShenZhi running status.
  logs     View ShenZhi logs.
  master   Start the master console (persona management web UI).
  skill    Manage ShenZhi skills.
  knowledge  Manage knowledge base.
  install-browser  Install browser tool (Playwright + Chromium).

Tip: Memory index management lives in chat — send /memory status or
/memory rebuild-index to the running agent."""


class CowCLI(click.Group):

    def format_help(self, ctx, formatter):
        formatter.write(HELP_TEXT.strip())
        formatter.write("\n")

    def parse_args(self, ctx, args):
        if args and args[0] == 'help':
            click.echo(HELP_TEXT.strip())
            ctx.exit(0)
        return super().parse_args(ctx, args)


@click.group(cls=CowCLI, invoke_without_command=True, context_settings=dict(help_option_names=[]))
@click.pass_context
def main(ctx):
    """ShenZhi CLI - Manage your ShenZhi instance."""
    if ctx.invoked_subcommand is None:
        click.echo(HELP_TEXT.strip())


@main.command()
def version():
    """Show the version."""
    click.echo(f"shenzhi {__version__}")


@main.command(name='help')
@click.pass_context
def help_cmd(ctx):
    """Show this message."""
    click.echo(HELP_TEXT.strip())


main.add_command(skill)
main.add_command(start)
main.add_command(stop)
main.add_command(restart)
main.add_command(update)
main.add_command(status)
main.add_command(logs)
main.add_command(context)
main.add_command(knowledge)
main.add_command(install_browser)
main.add_command(master)


if __name__ == '__main__':
    main()
