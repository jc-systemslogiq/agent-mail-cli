"""Agent Mail CLI - Progressive disclosure wrapper for mcp-agent-mail."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from .client import AgentMailClient, AgentMailConfig, AgentMailError

app = typer.Typer(
    name="agent-mail",
    help="Multi-agent coordination CLI. Communicate with other agents via messages and coordinate file access.",
    no_args_is_help=True,
)

# Subcommands
session_app = typer.Typer(help="Session management commands")
contacts_app = typer.Typer(help="Contact management commands")
file_reservations_app = typer.Typer(help="Inspect file reservations")
acks_app = typer.Typer(help="Review acknowledgement status")

app.add_typer(session_app, name="session")
app.add_typer(contacts_app, name="contacts")
app.add_typer(file_reservations_app, name="file_reservations")
app.add_typer(acks_app, name="acks")

console = Console()
err_console = Console(stderr=True)


def get_project_key(project: str | None) -> str:
    """Get project key from argument or auto-detect from PWD."""
    if project:
        return str(Path(project).resolve())
    return os.getcwd()


def get_client() -> AgentMailClient:
    """Get configured client."""
    return AgentMailClient(AgentMailConfig.from_env())


def output_result(result: dict | list, as_json: bool) -> None:
    """Output result in requested format."""
    if as_json:
        print(json.dumps(result, indent=2, default=str))
    else:
        rprint(result)


def handle_error(e: Exception) -> None:
    """Handle and display error."""
    if isinstance(e, AgentMailError):
        err_console.print(f"[red]Error:[/red] {e}")
        if e.data:
            err_console.print(f"[dim]Details: {e.data}[/dim]")
    else:
        err_console.print(f"[red]Error:[/red] {e}")
    raise typer.Exit(1)


# Global options
ProjectOption = Annotated[
    Optional[str],
    typer.Option("--project", "-p", help="Project path (default: current directory)"),
]
JsonOption = Annotated[
    bool, typer.Option("--json", "-j", help="Output as JSON for parsing")
]


# Session commands
@session_app.command("start")
def session_start(
    project: ProjectOption = None,
    program: Annotated[str, typer.Option(help="Agent program name")] = "claude-code",
    model: Annotated[str, typer.Option(help="Model identifier")] = "claude-opus-4-5-20251101",
    name: Annotated[Optional[str], typer.Option(help="Agent name (auto-generated if omitted)")] = None,
    task: Annotated[str, typer.Option(help="Task description")] = "",
    as_json: JsonOption = False,
):
    """Bootstrap a session: ensure project, register agent, fetch inbox."""
    try:
        client = get_client()
        result = client.start_session(
            human_key=get_project_key(project),
            program=program,
            model=model,
            agent_name=name,
            task_description=task,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


# Messaging commands
@app.command()
def send(
    to: Annotated[list[str], typer.Option("--to", "-t", help="Recipient agent name(s)")],
    subject: Annotated[str, typer.Option("--subject", "-s", help="Message subject")],
    body: Annotated[str, typer.Option("--body", "-b", help="Message body (Markdown)")],
    sender: Annotated[str, typer.Option("--from", "-f", help="Sender agent name")],
    project: ProjectOption = None,
    cc: Annotated[Optional[list[str]], typer.Option(help="CC recipients")] = None,
    importance: Annotated[str, typer.Option(help="Message importance")] = "normal",
    ack: Annotated[bool, typer.Option("--ack", help="Request acknowledgement")] = False,
    thread: Annotated[Optional[str], typer.Option(help="Thread ID to continue")] = None,
    as_json: JsonOption = False,
):
    """Send a message to other agents."""
    try:
        client = get_client()
        result = client.send_message(
            project_key=get_project_key(project),
            sender_name=sender,
            to=to,
            subject=subject,
            body_md=body,
            cc=cc,
            importance=importance,
            ack_required=ack,
            thread_id=thread,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


@app.command()
def reply(
    message_id: Annotated[int, typer.Argument(help="Message ID to reply to")],
    body: Annotated[str, typer.Option("--body", "-b", help="Reply body (Markdown)")],
    sender: Annotated[str, typer.Option("--from", "-f", help="Sender agent name")],
    project: ProjectOption = None,
    to: Annotated[Optional[list[str]], typer.Option(help="Override recipients")] = None,
    cc: Annotated[Optional[list[str]], typer.Option(help="CC recipients")] = None,
    as_json: JsonOption = False,
):
    """Reply to a message."""
    try:
        client = get_client()
        result = client.reply_message(
            project_key=get_project_key(project),
            message_id=message_id,
            sender_name=sender,
            body_md=body,
            to=to,
            cc=cc,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


@app.command()
def inbox(
    agent: Annotated[str, typer.Argument(help="Agent name to fetch inbox for")],
    project: ProjectOption = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max messages")] = 20,
    urgent: Annotated[bool, typer.Option("--urgent", help="Only urgent messages")] = False,
    since: Annotated[Optional[str], typer.Option(help="ISO timestamp to fetch since")] = None,
    bodies: Annotated[bool, typer.Option("--bodies", help="Include message bodies")] = False,
    as_json: JsonOption = False,
):
    """Fetch inbox messages for an agent."""
    try:
        client = get_client()
        result = client.fetch_inbox(
            project_key=get_project_key(project),
            agent_name=agent,
            limit=limit,
            urgent_only=urgent,
            since_ts=since,
            include_bodies=bodies,
        )
        if as_json:
            print(json.dumps(result, indent=2, default=str))
        else:
            if not result:
                console.print("[dim]No messages[/dim]")
            else:
                table = Table(title="Inbox")
                table.add_column("ID", style="cyan")
                table.add_column("From", style="green")
                table.add_column("Subject")
                table.add_column("Importance", style="yellow")
                table.add_column("Date", style="dim")
                for msg in result:
                    table.add_row(
                        str(msg.get("id", "")),
                        msg.get("from", ""),
                        msg.get("subject", ""),
                        msg.get("importance", ""),
                        msg.get("created_ts", "")[:19] if msg.get("created_ts") else "",
                    )
                console.print(table)
    except Exception as e:
        handle_error(e)


@app.command()
def ack(
    message_id: Annotated[int, typer.Argument(help="Message ID to acknowledge")],
    agent: Annotated[str, typer.Option("--agent", "-a", help="Agent name")],
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """Acknowledge a message."""
    try:
        client = get_client()
        result = client.acknowledge_message(
            project_key=get_project_key(project),
            agent_name=agent,
            message_id=message_id,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Search query (FTS5 syntax)")],
    project: ProjectOption = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max results")] = 20,
    as_json: JsonOption = False,
):
    """Search messages by content."""
    try:
        client = get_client()
        result = client.search_messages(
            project_key=get_project_key(project),
            query=query,
            limit=limit,
        )
        if as_json:
            print(json.dumps(result, indent=2, default=str))
        else:
            if not result:
                console.print("[dim]No results[/dim]")
            else:
                for msg in result:
                    console.print(
                        f"[cyan]{msg.get('id')}[/cyan] | "
                        f"[green]{msg.get('from', '')}[/green] | "
                        f"{msg.get('subject', '')} | "
                        f"[dim]{msg.get('created_ts', '')[:19] if msg.get('created_ts') else ''}[/dim]"
                    )
    except Exception as e:
        handle_error(e)


@app.command()
def thread(
    thread_id: Annotated[str, typer.Argument(help="Thread ID to view/summarize")],
    project: ProjectOption = None,
    summarize: Annotated[bool, typer.Option("--summarize", "-s", help="Get AI summary")] = False,
    examples: Annotated[bool, typer.Option("--examples", help="Include example messages")] = False,
    as_json: JsonOption = False,
):
    """View or summarize a thread."""
    try:
        client = get_client()
        result = client.summarize_thread(
            project_key=get_project_key(project),
            thread_id=thread_id,
            include_examples=examples,
            llm_mode=summarize,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


# File reservation commands
@app.command()
def reserve(
    paths: Annotated[list[str], typer.Argument(help="File paths/patterns to reserve")],
    agent: Annotated[str, typer.Option("--agent", "-a", help="Agent name")],
    project: ProjectOption = None,
    ttl: Annotated[int, typer.Option(help="Time-to-live in seconds")] = 3600,
    shared: Annotated[bool, typer.Option("--shared", help="Non-exclusive reservation")] = False,
    reason: Annotated[str, typer.Option(help="Reason for reservation")] = "",
    as_json: JsonOption = False,
):
    """Reserve file paths for exclusive or shared access."""
    try:
        client = get_client()
        result = client.reserve_paths(
            project_key=get_project_key(project),
            agent_name=agent,
            paths=paths,
            ttl_seconds=ttl,
            exclusive=not shared,
            reason=reason,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


@app.command()
def release(
    agent: Annotated[str, typer.Option("--agent", "-a", help="Agent name")],
    paths: Annotated[Optional[list[str]], typer.Argument(help="Paths to release (all if omitted)")] = None,
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """Release file reservations."""
    try:
        client = get_client()
        result = client.release_reservations(
            project_key=get_project_key(project),
            agent_name=agent,
            paths=paths,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


@app.command()
def renew(
    agent: Annotated[str, typer.Option("--agent", "-a", help="Agent name")],
    project: ProjectOption = None,
    extend: Annotated[int, typer.Option(help="Seconds to extend")] = 1800,
    as_json: JsonOption = False,
):
    """Renew file reservations."""
    try:
        client = get_client()
        result = client.renew_reservations(
            project_key=get_project_key(project),
            agent_name=agent,
            extend_seconds=extend,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


# Agent management commands
@app.command()
def register(
    program: Annotated[str, typer.Option(help="Agent program name")] = "claude-code",
    model: Annotated[str, typer.Option(help="Model identifier")] = "claude-opus-4-5-20251101",
    name: Annotated[Optional[str], typer.Option(help="Agent name (auto-generated if omitted)")] = None,
    task: Annotated[str, typer.Option(help="Task description")] = "",
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """Register an agent in the project."""
    try:
        client = get_client()
        project_key = get_project_key(project)
        # Ensure project exists first
        client.ensure_project(project_key)
        result = client.register_agent(
            project_key=project_key,
            program=program,
            model=model,
            name=name,
            task_description=task,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


@app.command()
def whoami(
    agent: Annotated[str, typer.Argument(help="Agent name to look up")],
    project: ProjectOption = None,
    commits: Annotated[bool, typer.Option("--commits", help="Include recent commits")] = True,
    as_json: JsonOption = False,
):
    """Get information about an agent."""
    try:
        client = get_client()
        result = client.whois(
            project_key=get_project_key(project),
            agent_name=agent,
            include_recent_commits=commits,
        )
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


@app.command()
def delete(
    agent: Annotated[str, typer.Argument(help="Agent name to delete")],
    project: ProjectOption = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Delete even with unread messages/reservations")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Check dependencies without deleting")] = False,
    as_json: JsonOption = False,
):
    """Delete an agent from the project.

    Checks for unread messages and active file reservations before deletion.
    Use --force to delete anyway, or --dry-run to just check dependencies.
    """
    try:
        client = get_client()
        project_key = get_project_key(project)

        if dry_run:
            # Just check dependencies
            deps = client.agent_dependencies(project_key, agent)
            if as_json:
                print(json.dumps(deps, indent=2))
            else:
                if deps["can_delete"]:
                    console.print(f"[green]✓[/green] Agent '{agent}' can be safely deleted")
                else:
                    console.print(f"[yellow]⚠[/yellow] Agent '{agent}' has dependencies:")
                    if deps["unread_messages"]:
                        console.print(f"  • {deps['unread_messages']} unread message(s)")
                    if deps["active_reservations"]:
                        console.print(f"  • {deps['active_reservations']} active file reservation(s)")
                if deps["sent_messages"]:
                    console.print(f"[dim]  • {deps['sent_messages']} sent message(s) will be orphaned[/dim]")
        else:
            # Actually delete
            result = client.delete_agent(project_key, agent, force=force, dry_run=False)
            if as_json:
                print(json.dumps(result, indent=2))
            else:
                console.print(f"[green]✓[/green] Deleted agent '{agent}'")
                if result["released_reservations"]:
                    console.print(f"  • Released {result['released_reservations']} file reservation(s)")
                if result["removed_recipient_entries"]:
                    console.print(f"  • Removed from {result['removed_recipient_entries']} message recipient(s)")
                if result["removed_links"]:
                    console.print(f"  • Removed {result['removed_links']} contact link(s)")
                if result["orphaned_sent_messages"]:
                    console.print(f"[dim]  • {result['orphaned_sent_messages']} sent message(s) now orphaned[/dim]")
    except Exception as e:
        handle_error(e)


# Contacts subcommands
@contacts_app.command("list")
def contacts_list(
    agent: Annotated[str, typer.Argument(help="Agent name")],
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """List contacts for an agent."""
    try:
        client = get_client()
        result = client.list_contacts(
            project_key=get_project_key(project),
            agent_name=agent,
        )
        if as_json:
            print(json.dumps(result, indent=2, default=str))
        else:
            if not result:
                console.print("[dim]No contacts[/dim]")
            else:
                for contact in result:
                    console.print(contact)
    except Exception as e:
        handle_error(e)


# Health check
@app.command()
def health(as_json: JsonOption = False):
    """Check server health."""
    try:
        client = get_client()
        result = client.health_check()
        output_result(result, as_json)
    except Exception as e:
        handle_error(e)


# --- Database-backed commands (fast, no HTTP) ---

def _fmt_delta(expires_ts: str) -> str:
    """Format time delta from now to expiry."""
    from datetime import datetime, timezone
    try:
        # Parse ISO timestamp
        exp = datetime.fromisoformat(expires_ts.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = exp - now
        total = int(delta.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        h, r = divmod(total, 3600)
        m, s = divmod(r, 60)
        return f"{sign}{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return "?"


# File reservations subcommands
@file_reservations_app.command("active")
def file_reservations_active(
    project: Annotated[str, typer.Argument(help="Project path or slug")],
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max reservations")] = 100,
    as_json: JsonOption = False,
):
    """List active file reservations with expiry countdowns."""
    try:
        client = get_client()
        rows = client.list_file_reservations(project, active_only=True, limit=limit)
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                console.print("[dim]No active reservations[/dim]")
            else:
                table = Table(title=f"Active File Reservations — {project}")
                table.add_column("ID", style="cyan")
                table.add_column("Agent", style="green")
                table.add_column("Pattern")
                table.add_column("Exclusive")
                table.add_column("Expires")
                table.add_column("In", style="yellow")
                for r in rows:
                    table.add_row(
                        str(r["id"]),
                        r["agent"],
                        r["path_pattern"],
                        "yes" if r["exclusive"] else "no",
                        r["expires_ts"][:19] if r["expires_ts"] else "",
                        _fmt_delta(r["expires_ts"]) if r["expires_ts"] else "",
                    )
                console.print(table)
    except Exception as e:
        handle_error(e)


@file_reservations_app.command("soon")
def file_reservations_soon(
    project: Annotated[str, typer.Argument(help="Project path or slug")],
    minutes: Annotated[int, typer.Option("--minutes", "-m", help="Minutes threshold")] = 30,
    as_json: JsonOption = False,
):
    """Show file reservations expiring soon."""
    try:
        client = get_client()
        rows = client.list_file_reservations(project, active_only=True, expiring_within_minutes=minutes, limit=500)
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                console.print(f"[dim]No reservations expiring within {minutes} minutes[/dim]")
            else:
                table = Table(title=f"Reservations Expiring Soon — {project}")
                table.add_column("ID", style="cyan")
                table.add_column("Agent", style="green")
                table.add_column("Pattern")
                table.add_column("Expires In", style="red")
                for r in rows:
                    table.add_row(
                        str(r["id"]),
                        r["agent"],
                        r["path_pattern"],
                        _fmt_delta(r["expires_ts"]) if r["expires_ts"] else "",
                    )
                console.print(table)
    except Exception as e:
        handle_error(e)


@file_reservations_app.command("list")
def file_reservations_list(
    project: Annotated[str, typer.Argument(help="Project path or slug")],
    all_: Annotated[bool, typer.Option("--all", "-a", help="Include released")] = False,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max reservations")] = 100,
    as_json: JsonOption = False,
):
    """List file reservations for a project."""
    try:
        client = get_client()
        rows = client.list_file_reservations(project, active_only=not all_, limit=limit)
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                console.print("[dim]No reservations[/dim]")
            else:
                table = Table(title=f"File Reservations — {project}")
                table.add_column("ID", style="cyan")
                table.add_column("Agent", style="green")
                table.add_column("Pattern")
                table.add_column("Exclusive")
                table.add_column("Expires")
                table.add_column("Released")
                for r in rows:
                    table.add_row(
                        str(r["id"]),
                        r["agent"],
                        r["path_pattern"],
                        "yes" if r["exclusive"] else "no",
                        r["expires_ts"][:19] if r.get("expires_ts") else "",
                        r["released_ts"][:19] if r.get("released_ts") else "",
                    )
                console.print(table)
    except Exception as e:
        handle_error(e)


# Acks subcommands
@acks_app.command("pending")
def acks_pending(
    project: Annotated[str, typer.Argument(help="Project path or slug")],
    agent: Annotated[str, typer.Argument(help="Agent name")],
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max messages")] = 20,
    as_json: JsonOption = False,
):
    """List messages requiring acknowledgement that are still pending."""
    try:
        client = get_client()
        rows = client.list_acks_pending(project, agent, limit)
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                console.print("[dim]No pending acknowledgements[/dim]")
            else:
                table = Table(title=f"Pending Acks for {agent}")
                table.add_column("ID", style="cyan")
                table.add_column("From", style="green")
                table.add_column("Subject")
                table.add_column("Importance", style="yellow")
                table.add_column("Date", style="dim")
                for r in rows:
                    table.add_row(
                        str(r["id"]),
                        r.get("sender", ""),
                        r["subject"],
                        r["importance"],
                        r["created_ts"][:19] if r.get("created_ts") else "",
                    )
                console.print(table)
    except Exception as e:
        handle_error(e)


@acks_app.command("overdue")
def acks_overdue(
    project: Annotated[str, typer.Argument(help="Project path or slug")],
    agent: Annotated[str, typer.Argument(help="Agent name")],
    hours: Annotated[int, typer.Option("--hours", "-h", help="Age threshold in hours")] = 24,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max messages")] = 20,
    as_json: JsonOption = False,
):
    """List ack-required messages older than threshold without acknowledgement."""
    try:
        client = get_client()
        rows = client.list_acks_overdue(project, agent, hours=hours, limit=limit)
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                console.print(f"[dim]No overdue acknowledgements (threshold: {hours}h)[/dim]")
            else:
                table = Table(title=f"Overdue Acks for {agent} (>{hours}h)")
                table.add_column("ID", style="cyan")
                table.add_column("From", style="green")
                table.add_column("Subject")
                table.add_column("Importance", style="red")
                table.add_column("Date", style="dim")
                for r in rows:
                    table.add_row(
                        str(r["id"]),
                        r.get("sender", ""),
                        r["subject"],
                        r["importance"],
                        r["created_ts"][:19] if r.get("created_ts") else "",
                    )
                console.print(table)
    except Exception as e:
        handle_error(e)


# Convenience alias for list-acks
@app.command("list-acks")
def list_acks(
    project: ProjectOption = None,
    agent: Annotated[str, typer.Option("--agent", "-a", help="Agent name")] = "",
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max messages")] = 10,
    as_json: JsonOption = False,
):
    """List messages requiring acknowledgement for an agent."""
    if not agent:
        err_console.print("[red]Error:[/red] --agent is required")
        raise typer.Exit(1)
    try:
        client = get_client()
        rows = client.list_acks_pending(get_project_key(project), agent, limit)
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                console.print("[dim]No pending acknowledgements[/dim]")
            else:
                for r in rows:
                    console.print(
                        f"[cyan]{r['id']}[/cyan] | "
                        f"[green]{r.get('sender', '')}[/green] | "
                        f"{r['subject']} | "
                        f"[yellow]{r['importance']}[/yellow]"
                    )
    except Exception as e:
        handle_error(e)


# List agents command
@app.command("list-agents")
def list_agents(
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """List agents in a project."""
    try:
        client = get_client()
        rows = client.list_agents(get_project_key(project))
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                console.print("[dim]No agents[/dim]")
            else:
                table = Table(title="Agents")
                table.add_column("Name", style="cyan")
                table.add_column("Task")
                table.add_column("Last Active", style="dim")
                for r in rows:
                    table.add_row(
                        r["name"],
                        r["task_description"][:40] + "…" if len(r.get("task_description", "")) > 40 else r.get("task_description", ""),
                        r["last_active_ts"][:19] if r.get("last_active_ts") else "",
                    )
                console.print(table)
    except Exception as e:
        handle_error(e)


# List projects command
@app.command("list-projects")
def list_projects(
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max projects")] = 100,
    as_json: JsonOption = False,
):
    """List known projects."""
    try:
        client = get_client()
        rows = client.list_projects(limit)
        if as_json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            if not rows:
                console.print("[dim]No projects[/dim]")
            else:
                table = Table(title="Projects")
                table.add_column("ID", style="cyan")
                table.add_column("Slug")
                table.add_column("Human Key")
                table.add_column("Created", style="dim")
                for r in rows:
                    table.add_row(
                        str(r["id"]),
                        r["slug"][:30] + "…" if len(r["slug"]) > 30 else r["slug"],
                        r["human_key"][:40] + "…" if len(r["human_key"]) > 40 else r["human_key"],
                        r["created_at"][:19] if r.get("created_at") else "",
                    )
                console.print(table)
    except Exception as e:
        handle_error(e)


if __name__ == "__main__":
    app()
