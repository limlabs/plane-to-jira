import logging
import os
import sys

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .plane_client import PlaneClient
from .jira_client import JiraClient
from .migrate import Migrator, MigrationError

console = Console()


def _get_env(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "")
    if required and not val:
        console.print(f"[red]Error: {name} environment variable is required[/red]")
        sys.exit(1)
    return val


@click.group()
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose logging")
def cli(verbose: bool):
    """Migrate projects from Plane to JIRA."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
        format="%(message)s",
    )


@cli.command()
def list_projects():
    """List all projects in the Plane workspace."""
    plane = PlaneClient(
        base_url=_get_env("PLANE_BASE_URL"),
        api_token=_get_env("PLANE_API_TOKEN"),
        workspace_slug=_get_env("PLANE_WORKSPACE_SLUG"),
    )

    projects = plane.list_projects()
    table = Table(title="Plane Projects")
    table.add_column("ID")
    table.add_column("Identifier")
    table.add_column("Name")
    table.add_column("Members")

    for p in projects:
        table.add_row(
            p["id"],
            p["identifier"],
            p["name"],
            str(p.get("total_members", "?")),
        )

    console.print(table)


@cli.command()
@click.argument("plane_project_id")
@click.option(
    "--jira-key",
    default=None,
    help="JIRA project key to use (defaults to Plane identifier)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview what would be migrated without making changes",
)
@click.option(
    "-y", "--yes",
    is_flag=True,
    help="Skip confirmation prompts",
)
def migrate(plane_project_id: str, jira_key: str | None, dry_run: bool, yes: bool):
    """Migrate a Plane project to JIRA.

    PLANE_PROJECT_ID is the UUID of the Plane project to migrate.
    """
    plane = PlaneClient(
        base_url=_get_env("PLANE_BASE_URL"),
        api_token=_get_env("PLANE_API_TOKEN"),
        workspace_slug=_get_env("PLANE_WORKSPACE_SLUG"),
    )
    jira = JiraClient(
        base_url=_get_env("JIRA_BASE_URL"),
        email=_get_env("JIRA_EMAIL"),
        api_token=_get_env("JIRA_API_TOKEN"),
    )

    user_map = _parse_user_map(os.environ.get("USER_MAP", ""))
    migrator = Migrator(plane, jira, user_map=user_map)
    try:
        migrator.migrate_project(
            plane_project_id=plane_project_id,
            jira_project_key=jira_key,
            dry_run=dry_run,
            yes=yes,
        )
    except MigrationError as e:
        console.print(f"\n[bold red]Migration failed:[/bold red] {e}")
        sys.exit(1)


@cli.command()
def validate():
    """Validate connectivity to both Plane and JIRA."""
    console.print("Checking Plane connection...")
    try:
        plane = PlaneClient(
            base_url=_get_env("PLANE_BASE_URL"),
            api_token=_get_env("PLANE_API_TOKEN"),
            workspace_slug=_get_env("PLANE_WORKSPACE_SLUG"),
        )
        projects = plane.list_projects()
        console.print(f"[green]Plane OK[/green] - {len(projects)} projects found")
    except Exception as e:
        console.print(f"[red]Plane connection failed:[/red] {e}")

    console.print("Checking JIRA connection...")
    try:
        jira = JiraClient(
            base_url=_get_env("JIRA_BASE_URL"),
            email=_get_env("JIRA_EMAIL"),
            api_token=_get_env("JIRA_API_TOKEN"),
        )
        users = jira.get_all_users()
        console.print(f"[green]JIRA OK[/green] - {len(users)} users found")
    except Exception as e:
        console.print(f"[red]JIRA connection failed:[/red] {e}")


def _parse_user_map(raw: str) -> dict[str, str]:
    """Parse USER_MAP env var: 'plane@a.com=jira@b.com,plane2@a.com=jira2@b.com'"""
    if not raw.strip():
        return {}
    mapping = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" not in pair:
            continue
        plane_email, jira_email = pair.split("=", 1)
        mapping[plane_email.strip()] = jira_email.strip()
    return mapping


def main():
    load_dotenv()
    cli()
