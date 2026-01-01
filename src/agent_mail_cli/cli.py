"""Agent Mail CLI - Progressive disclosure wrapper for mcp-agent-mail."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from .client import AgentMailClient, AgentMailConfig, AgentMailError

# Session tracking directory
SESSIONS_DIR = Path(os.path.expanduser("~/.config/agent-mail/sessions"))

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


# --- Local session tracking (file-based) ---

def _project_hash(project_key: str) -> str:
    """Generate a short hash for project path to use as directory name."""
    return hashlib.sha256(project_key.encode()).hexdigest()[:12]


def _session_file(project_key: str, agent_name: str) -> Path:
    """Get path to session file for an agent."""
    return SESSIONS_DIR / _project_hash(project_key) / f"{agent_name}.json"


def _read_session(project_key: str, agent_name: str) -> dict[str, Any] | None:
    """Read session data for an agent, returns None if no session or expired."""
    session_file = _session_file(project_key, agent_name)
    if not session_file.exists():
        return None
    try:
        data = json.loads(session_file.read_text())
        expires_at = datetime.fromisoformat(data.get("expires_at", "").replace("Z", "+00:00"))
        if expires_at < datetime.now(timezone.utc):
            # Session expired, clean up
            session_file.unlink(missing_ok=True)
            return None
        return data
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def _write_session(project_key: str, agent_name: str, ttl_seconds: int = 300) -> dict[str, Any]:
    """Create or update session file with TTL."""
    session_file = _session_file(project_key, agent_name)
    session_file.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    # Walk up process tree to find stable ancestor (Claude Code, not ephemeral shell)
    # Chain is typically: Claude Code -> shell -> agent-mail
    # We want grandparent (2 levels up) or higher
    stable_pid = os.getpid()
    try:
        import subprocess
        # Get parent's parent (grandparent) - should be Claude Code
        ppid = os.getppid()
        result = subprocess.run(["ps", "-o", "ppid=", "-p", str(ppid)], capture_output=True, text=True)
        if result.returncode == 0:
            grandparent = int(result.stdout.strip())
            if grandparent > 1:
                stable_pid = grandparent
    except Exception:
        stable_pid = os.getppid()  # Fallback to parent

    data = {
        "agent": agent_name,
        "project": project_key,
        "started_at": now.isoformat(),
        "expires_at": (now + __import__("datetime").timedelta(seconds=ttl_seconds)).isoformat(),
        "pid": stable_pid,
    }

    # Check if we're updating an existing session
    existing = _read_session(project_key, agent_name)
    if existing:
        data["started_at"] = existing.get("started_at", data["started_at"])

    session_file.write_text(json.dumps(data, indent=2))
    return data


def _clear_session(project_key: str, agent_name: str) -> bool:
    """Clear session file for an agent."""
    session_file = _session_file(project_key, agent_name)
    if session_file.exists():
        session_file.unlink()
        return True
    return False


def _check_session_conflict(project_key: str, agent_name: str) -> dict[str, Any] | None:
    """Check if another session is active for this agent. Returns session data if conflict."""
    session = _read_session(project_key, agent_name)
    if not session:
        return None

    # Check if it's our own process (allow re-registration in same session)
    if session.get("pid") == os.getpid():
        return None

    # Check if the process is still running
    pid = session.get("pid")
    if pid:
        try:
            os.kill(pid, 0)  # Check if process exists
        except OSError:
            # Process doesn't exist, clear stale session
            _clear_session(project_key, agent_name)
            return None

    return session


def _format_session_expiry(session: dict[str, Any]) -> str:
    """Format how long until session expires."""
    try:
        expires_at = datetime.fromisoformat(session.get("expires_at", "").replace("Z", "+00:00"))
        delta = expires_at - datetime.now(timezone.utc)
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            return f"{seconds // 60}m"
        else:
            return f"{seconds // 3600}h"
    except (ValueError, TypeError):
        return "?"


# --- End session tracking ---


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


@session_app.command("heartbeat")
def session_heartbeat(
    agent: Annotated[str, typer.Argument(help="Agent name")],
    project: ProjectOption = None,
    ttl: Annotated[int, typer.Option("--ttl", help="Session TTL in seconds")] = 300,
    as_json: JsonOption = False,
):
    """Extend session TTL to prevent expiry.

    Call this periodically (e.g., every 2-3 minutes) to keep the session alive.
    Typically invoked by a hook.

    Example (in a hook):
        agent-mail session heartbeat OliveStream --ttl 300
    """
    try:
        project_key = get_project_key(project)
        session = _read_session(project_key, agent)

        if not session:
            if as_json:
                print(json.dumps({"error": "no_session", "agent": agent}, indent=2))
            else:
                err_console.print(f"[yellow]⚠[/yellow] No active session for {agent}")
            raise typer.Exit(1)

        # Extend the session
        updated = _write_session(project_key, agent, ttl_seconds=ttl)

        if as_json:
            print(json.dumps(updated, indent=2, default=str))
        else:
            console.print(f"[green]✓[/green] Session extended for [cyan]{agent}[/cyan] (TTL: {ttl}s)")
    except typer.Exit:
        raise
    except Exception as e:
        handle_error(e)


@session_app.command("status")
def session_status(
    agent: Annotated[Optional[str], typer.Argument(help="Agent name (omit to list all)")] = None,
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """Check session status for an agent or list all active sessions."""
    try:
        project_key = get_project_key(project)
        project_hash = _project_hash(project_key)
        sessions_dir = SESSIONS_DIR / project_hash

        if agent:
            # Single agent status
            session = _read_session(project_key, agent)
            if session:
                if as_json:
                    print(json.dumps(session, indent=2, default=str))
                else:
                    expires_in = _format_session_expiry(session)
                    console.print(f"[green]●[/green] [cyan]{agent}[/cyan] active (PID {session.get('pid')}, expires in {expires_in})")
            else:
                if as_json:
                    print(json.dumps({"agent": agent, "status": "inactive"}, indent=2))
                else:
                    console.print(f"[dim]○[/dim] [cyan]{agent}[/cyan] no active session")
        else:
            # List all sessions for this project
            sessions = []
            if sessions_dir.exists():
                for session_file in sessions_dir.glob("*.json"):
                    agent_name = session_file.stem
                    session = _read_session(project_key, agent_name)
                    if session:
                        sessions.append(session)

            if as_json:
                print(json.dumps(sessions, indent=2, default=str))
            else:
                if not sessions:
                    console.print("[dim]No active sessions[/dim]")
                else:
                    table = Table(title="Active Sessions")
                    table.add_column("Agent", style="cyan")
                    table.add_column("PID")
                    table.add_column("Expires In", style="yellow")
                    table.add_column("Started", style="dim")
                    for s in sessions:
                        table.add_row(
                            s.get("agent", "?"),
                            str(s.get("pid", "?")),
                            _format_session_expiry(s),
                            s.get("started_at", "?")[:19],
                        )
                    console.print(table)
    except Exception as e:
        handle_error(e)


@session_app.command("end")
def session_end(
    agent: Annotated[str, typer.Argument(help="Agent name")],
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """End a session, releasing the lock for other agents.

    Call this when done working to allow other sessions to resume as this agent.
    """
    try:
        project_key = get_project_key(project)
        cleared = _clear_session(project_key, agent)

        if as_json:
            print(json.dumps({"agent": agent, "cleared": cleared}, indent=2))
        else:
            if cleared:
                console.print(f"[green]✓[/green] Session ended for [cyan]{agent}[/cyan]")
            else:
                console.print(f"[dim]No active session for {agent}[/dim]")
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


@app.command("inbox-status")
def inbox_status(
    project: ProjectOption = None,
    agent: Annotated[
        Optional[str],
        typer.Option("--agent", "-a", help="Agent name (omit for project-wide recent activity)"),
    ] = None,
    recent_minutes: Annotated[
        int,
        typer.Option("--recent-minutes", help="Recent window for project-wide activity (only when --agent is omitted)"),
    ] = 60,
    since: Annotated[
        Optional[str],
        typer.Option("--since-ts", help="ISO timestamp to compute new-since counts (per-agent only)"),
    ] = None,
    urgent: Annotated[bool, typer.Option("--urgent", help="Only urgent/high messages")] = False,
    as_json: JsonOption = False,
):
    """Check inbox status (counts/timestamps only) for hooks and quick reminders."""
    try:
        client = get_client()
        project_key = get_project_key(project)

        recent_seconds: int | None = None
        if agent is None:
            recent_seconds = max(1, int(recent_minutes) * 60)

        result = client.inbox_status(
            project_key=project_key,
            agent_name=agent,
            since_ts=since,
            urgent_only=urgent,
            recent_seconds=recent_seconds,
        )

        if as_json:
            output_result(result, as_json=True)
            return

        scope = (result or {}).get("scope")
        if scope == "agent":
            unread = int((result or {}).get("unread_count") or 0)
            if unread <= 0:
                return
            console.print("")
            console.print(f"[bold]📬[/] You have [bold]{unread}[/] unread message(s) in this project.")
            if since and "new_since_count" in (result or {}):
                console.print(f"[dim]New since {since}:[/] {int((result or {}).get('new_since_count') or 0)}")
            console.print(f"[dim]Check inbox:[/] agent-mail inbox {result.get('agent_name', agent)} --project {project_key}")
            console.print("")
            return

        # Project-wide recent activity mode
        recent = int((result or {}).get("recent_message_count") or 0)
        if recent <= 0:
            return
        console.print("")
        console.print(f"[bold]📬[/] There are [bold]{recent}[/] recent message(s) in this project.")
        console.print("[dim]Check your inbox:[/] agent-mail inbox <your-agent-name> --project " + project_key)
        console.print("")
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
def _format_time_ago(ts_str: str) -> str:
    """Format timestamp as human-readable time ago."""
    from datetime import datetime, timezone
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - ts
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return "just now"
        elif seconds < 3600:
            mins = seconds // 60
            return f"{mins}m ago"
        elif seconds < 86400:
            hours = seconds // 3600
            return f"{hours}h ago"
        else:
            days = seconds // 86400
            return f"{days}d ago"
    except Exception:
        return ts_str[:19] if ts_str else "?"


def _find_resumable_agent(client: AgentMailClient, project_key: str) -> dict[str, Any] | None:
    """Find the most recently active non-deleted agent for resumption."""
    agents = client.list_agents(project_key)
    # Filter out deleted agents and sort by last_active_ts descending
    active_agents = [
        a for a in agents
        if not a.get("name", "").startswith("Deleted-")
        and a.get("contact_policy") != "block_all"
    ]
    if not active_agents:
        return None
    # Sort by last_active_ts descending
    active_agents.sort(
        key=lambda a: a.get("last_active_ts", ""),
        reverse=True
    )
    return active_agents[0]


@app.command()
def register(
    program: Annotated[str, typer.Option(help="Agent program name")] = "claude-code",
    model: Annotated[str, typer.Option(help="Model identifier")] = "claude-opus-4-5-20251101",
    name: Annotated[Optional[str], typer.Option("--name", help="Resume as existing agent (must match exactly)")] = None,
    as_agent: Annotated[Optional[str], typer.Option("--as", help="Resume as existing agent (alias for --name)")] = None,
    resume: Annotated[bool, typer.Option("--resume", "-r", help="Resume as most recently active agent")] = False,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force registration even if session conflict detected")] = False,
    ttl: Annotated[int, typer.Option("--ttl", help="Session TTL in seconds (use heartbeat to extend)")] = 300,
    task: Annotated[str, typer.Option(help="Task description")] = "",
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """Register an agent in the project.

    First registration (name auto-assigned):
        agent-mail register --task "working on feature X"

    Resume as specific agent:
        agent-mail register --as OliveStream --task "continuing work"

    Resume most recent agent:
        agent-mail register --resume --task "continuing work"

    Session tracking: A local session file is created to detect conflicts.
    Use 'agent-mail session heartbeat' to extend the session TTL.
    """
    try:
        client = get_client()
        project_key = get_project_key(project)
        # Ensure project exists first
        client.ensure_project(project_key)

        # Determine agent name: --as takes precedence, then --name, then --resume
        effective_name = as_agent or name

        if resume and not effective_name:
            # Auto-detect most recent agent
            recent = _find_resumable_agent(client, project_key)
            if recent:
                effective_name = recent["name"]
                if not as_json:
                    console.print(
                        f"[dim]Resuming as[/dim] [cyan]{effective_name}[/cyan] "
                        f"[dim](last active: {_format_time_ago(recent.get('last_active_ts', ''))})[/dim]"
                    )
            else:
                if not as_json:
                    console.print("[yellow]No previous agents found, creating new registration[/yellow]")

        # Check for session conflict before registering
        if effective_name and not force:
            conflict = _check_session_conflict(project_key, effective_name)
            if conflict:
                expires_in = _format_session_expiry(conflict)
                if as_json:
                    print(json.dumps({
                        "error": "session_conflict",
                        "agent": effective_name,
                        "conflict_pid": conflict.get("pid"),
                        "expires_in": expires_in,
                    }, indent=2))
                    raise typer.Exit(1)
                else:
                    err_console.print(
                        f"[red]✗[/red] Session conflict: [cyan]{effective_name}[/cyan] has an active session "
                        f"(PID {conflict.get('pid')}, expires in {expires_in})"
                    )
                    err_console.print(f"[dim]  Use --force to take over the session[/dim]")
                    raise typer.Exit(1)

        result = client.register_agent(
            project_key=project_key,
            program=program,
            model=model,
            name=effective_name,
            task_description=task,
        )

        agent_name = result.get("name", "?")

        # Create/update session file
        _write_session(project_key, agent_name, ttl_seconds=ttl)

        if as_json:
            result["session_ttl"] = ttl
            print(json.dumps(result, indent=2, default=str))
        else:
            is_new = effective_name is None
            if is_new:
                console.print(f"[green]✓[/green] Registered as [cyan bold]{agent_name}[/cyan bold]")
                console.print(f"[dim]  To resume later: agent-mail register --as {agent_name}[/dim]")
            else:
                console.print(f"[green]✓[/green] Resumed as [cyan bold]{agent_name}[/cyan bold]")
            if task:
                console.print(f"[dim]  Task: {task}[/dim]")
            console.print(f"[dim]  Session TTL: {ttl}s (use 'agent-mail session heartbeat {agent_name}' to extend)[/dim]")
    except typer.Exit:
        raise
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


def _run_bd_command(args: list[str], project_key: str) -> dict[str, Any] | None:
    """Run a beads (bd) command and return parsed JSON output."""
    import subprocess
    try:
        result = subprocess.run(
            ["bd"] + args + ["--json"],
            capture_output=True,
            text=True,
            cwd=project_key,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass
    return None


@app.command()
def context(
    agent: Annotated[str, typer.Argument(help="Agent name to get context for")],
    project: ProjectOption = None,
    as_json: JsonOption = False,
):
    """Get full context for resuming work as an agent.

    Aggregates information from multiple sources:
    - Agent profile and recent activity
    - Inbox messages (unread count)
    - Pending acknowledgements
    - File reservations
    - Beads issues (assigned, blocked, mentions)

    Use this after resuming to understand where you left off.

    Example:
        agent-mail register --resume
        agent-mail context OliveStream
    """
    try:
        client = get_client()
        project_key = get_project_key(project)

        # Gather all context in parallel-ish (sequential for now, could be async)
        context_data: dict[str, Any] = {
            "agent": None,
            "attention_needed": {
                "unread_messages": 0,
                "pending_acks": 0,
                "blocked_tasks": 0,
            },
            "messages": {
                "unread": [],
                "pending_acks": [],
            },
            "files": {
                "reserved": [],
            },
            "beads": {
                "in_progress": [],
                "blocked": [],
            },
        }

        # 1. Agent profile
        try:
            profile = client.whois(project_key, agent, include_recent_commits=True)
            context_data["agent"] = {
                "name": profile.get("name"),
                "last_active": profile.get("last_active_ts"),
                "task_description": profile.get("task_description"),
                "recent_commits": profile.get("recent_commits", [])[:3],
            }
        except Exception:
            context_data["agent"] = {"name": agent, "error": "Could not fetch profile"}

        # 2. Inbox
        try:
            inbox = client.fetch_inbox(project_key, agent, limit=10)
            context_data["messages"]["unread"] = [
                {
                    "id": m.get("id"),
                    "from": m.get("from"),
                    "subject": m.get("subject"),
                    "importance": m.get("importance"),
                    "age": _format_time_ago(m.get("created_ts", "")),
                }
                for m in inbox
            ]
            context_data["attention_needed"]["unread_messages"] = len(inbox)
        except Exception:
            pass

        # 3. Pending acks
        try:
            acks = client.list_acks_pending(project_key, agent, limit=10)
            context_data["messages"]["pending_acks"] = [
                {
                    "id": a.get("id"),
                    "from": a.get("sender"),
                    "subject": a.get("subject"),
                }
                for a in acks
            ]
            context_data["attention_needed"]["pending_acks"] = len(acks)
        except Exception:
            pass

        # 4. File reservations
        try:
            reservations = client.list_file_reservations(project_key, active_only=True)
            agent_reservations = [r for r in reservations if r.get("agent") == agent]
            context_data["files"]["reserved"] = [
                {
                    "pattern": r.get("path_pattern"),
                    "expires_in": _fmt_delta(r.get("expires_ts", "")),
                }
                for r in agent_reservations
            ]
        except Exception:
            pass

        # 5. Beads integration (if available)
        bd_in_progress = _run_bd_command(["list", "--assignee", agent, "--status", "in_progress"], project_key)
        if bd_in_progress and isinstance(bd_in_progress, list):
            context_data["beads"]["in_progress"] = [
                {
                    "id": i.get("id"),
                    "title": i.get("title"),
                    "priority": i.get("priority"),
                }
                for i in bd_in_progress[:5]
            ]

        bd_blocked = _run_bd_command(["list", "--assignee", agent, "--status", "blocked"], project_key)
        if bd_blocked and isinstance(bd_blocked, list):
            context_data["beads"]["blocked"] = [
                {
                    "id": i.get("id"),
                    "title": i.get("title"),
                    "blocked_by": i.get("blocked_by", []),
                }
                for i in bd_blocked[:5]
            ]
            context_data["attention_needed"]["blocked_tasks"] = len(bd_blocked)

        if as_json:
            print(json.dumps(context_data, indent=2, default=str))
        else:
            # Rich formatted output
            agent_info = context_data["agent"]
            console.print(f"\n[bold cyan]═══ Context for {agent_info.get('name', agent)} ═══[/bold cyan]")

            if agent_info.get("task_description"):
                console.print(f"[dim]Task:[/dim] {agent_info['task_description']}")
            if agent_info.get("last_active"):
                console.print(f"[dim]Last active:[/dim] {_format_time_ago(agent_info['last_active'])}")

            # Attention needed summary
            attn = context_data["attention_needed"]
            attn_items = []
            if attn["unread_messages"]:
                attn_items.append(f"{attn['unread_messages']} unread message(s)")
            if attn["pending_acks"]:
                attn_items.append(f"{attn['pending_acks']} pending ack(s)")
            if attn["blocked_tasks"]:
                attn_items.append(f"{attn['blocked_tasks']} blocked task(s)")

            if attn_items:
                console.print(f"\n[yellow bold]⚠ Attention needed:[/yellow bold] {', '.join(attn_items)}")

            # Messages
            if context_data["messages"]["unread"]:
                console.print("\n[bold]📬 Unread Messages[/bold]")
                for m in context_data["messages"]["unread"][:5]:
                    imp = f"[red](!)[/red] " if m.get("importance") == "high" else ""
                    console.print(f"  {imp}From [green]{m['from']}[/green]: {m['subject']} [dim]({m['age']})[/dim]")

            # File reservations
            if context_data["files"]["reserved"]:
                console.print("\n[bold]📁 Reserved Files[/bold]")
                for r in context_data["files"]["reserved"]:
                    console.print(f"  {r['pattern']} [dim](expires in {r['expires_in']})[/dim]")

            # Beads
            if context_data["beads"]["in_progress"]:
                console.print("\n[bold]📋 Beads: In Progress[/bold]")
                for b in context_data["beads"]["in_progress"]:
                    console.print(f"  [{b['id']}] {b['title']} [dim](P{b.get('priority', '?')})[/dim]")

            if context_data["beads"]["blocked"]:
                console.print("\n[bold red]🚫 Beads: Blocked[/bold red]")
                for b in context_data["beads"]["blocked"]:
                    blocked_by = ", ".join(b.get("blocked_by", [])) if b.get("blocked_by") else "unknown"
                    console.print(f"  [{b['id']}] {b['title']} [dim](by {blocked_by})[/dim]")

            # Recent commits
            if agent_info.get("recent_commits"):
                console.print("\n[bold]📝 Recent Commits[/bold]")
                for c in agent_info["recent_commits"][:3]:
                    console.print(f"  [dim]{c.get('hexsha', '')[:7]}[/dim] {c.get('summary', '')[:60]}")

            console.print()

    except Exception as e:
        handle_error(e)


@app.command()
def delete(
    agents: Annotated[list[str], typer.Argument(help="Agent name(s) to delete")],
    project: ProjectOption = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Delete even with unread messages/reservations")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Check dependencies without deleting")] = False,
    as_json: JsonOption = False,
):
    """Soft-delete one or more agents (rename to Deleted-*).

    This is a soft delete: agents are renamed to 'Deleted-N' and blocked.
    Use 'purge' afterward to permanently remove soft-deleted agents.

    Examples:
        agent-mail delete OliveStream
        agent-mail delete Agent1 Agent2 Agent3
        agent-mail delete --dry-run Agent1 Agent2

    Checks for unread messages and active file reservations before deletion.
    Use --force to delete anyway, or --dry-run to just check dependencies.
    """
    try:
        client = get_client()
        project_key = get_project_key(project)
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        for agent in agents:
            try:
                if dry_run:
                    deps = client.agent_dependencies(project_key, agent)
                    deps["agent"] = agent
                    results.append(deps)
                    if not as_json:
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
                    result = client.delete_agent(project_key, agent, force=force, dry_run=False)
                    results.append(result)
                    if not as_json:
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
                error_info = {"agent": agent, "error": str(e)}
                errors.append(error_info)
                if not as_json:
                    err_console.print(f"[red]✗[/red] Failed to delete '{agent}': {e}")

        if as_json:
            output = {"results": results}
            if errors:
                output["errors"] = errors
            print(json.dumps(output, indent=2))
            if errors:
                raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as e:
        handle_error(e)


@app.command()
def purge(
    project: ProjectOption = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show what would be purged without deleting")] = False,
    as_json: JsonOption = False,
):
    """Permanently remove all soft-deleted agents and their orphaned messages.

    After using 'delete', agents are renamed to 'Deleted-*' but remain in the database.
    Use 'purge' to hard-delete these agents and any messages they sent.
    """
    try:
        client = get_client()
        project_key = get_project_key(project)
        result = client.purge_deleted_agents(project_key, dry_run=dry_run)

        if as_json:
            print(json.dumps(result, indent=2))
        else:
            if result.get("dry_run"):
                if result["purged_agents"] == 0:
                    console.print("[dim]No soft-deleted agents to purge[/dim]")
                else:
                    console.print(f"[yellow]Would purge:[/yellow]")
                    console.print(f"  • {result['purged_agents']} agent(s): {', '.join(result['agents'])}")
                    console.print(f"  • {result['purged_messages']} orphaned message(s)")
            else:
                if result["purged_agents"] == 0:
                    console.print("[dim]No soft-deleted agents to purge[/dim]")
                else:
                    console.print(f"[green]✓[/green] Purged {result['purged_agents']} agent(s) and {result['purged_messages']} message(s)")
                    if result["agents"]:
                        console.print(f"[dim]  Agents: {', '.join(result['agents'])}[/dim]")
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
