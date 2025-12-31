# Agent Mail CLI

CLI wrapper for `mcp-agent-mail` server with progressive disclosure for token efficiency.

## Why?

MCP servers load **all tool definitions** at session start, regardless of usage. The `mcp-agent-mail` server has 28 tools with verbose descriptions (~2,500+ tokens).

This CLI replaces those 28 MCP tools with a single `Bash(agent-mail:*)` permission, reducing context overhead to ~50 tokens. Claude discovers commands via `--help` only when needed.

## Installation

```bash
# Install with pipx (recommended)
pipx install ~/projects/ai/agent-mail-cli

# Or with pip
pip install ~/projects/ai/agent-mail-cli
```

## Configuration

Config is stored in `~/.config/agent-mail/`:

```bash
# Store bearer token (required when server uses auth)
mkdir -p ~/.config/agent-mail
echo "YOUR_TOKEN_HERE" > ~/.config/agent-mail/token

# Optional: additional settings in config file
echo "url=http://127.0.0.1:8765/mcp/" > ~/.config/agent-mail/config
echo "timeout=30" >> ~/.config/agent-mail/config
```

Environment variables override config files when set:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_MAIL_URL` | `http://127.0.0.1:8765/mcp/` | Server URL |
| `AGENT_MAIL_TOKEN` | *(from ~/.config/agent-mail/token)* | Bearer token |
| `AGENT_MAIL_TIMEOUT` | `30` | Request timeout in seconds |

## Usage

### Discovery

```bash
agent-mail --help              # List all commands
agent-mail send --help         # Detailed usage for send
agent-mail session --help      # Session management commands
```

### Session Bootstrap

```bash
# Start a session (auto-registers agent, fetches inbox)
agent-mail session start

# With custom agent name
agent-mail session start --name "BlueLake" --task "Working on auth"
```

### Messaging

```bash
# Send a message
agent-mail send --to GreenCastle --from BlueLake \
  --subject "Plan review" --body "Please check the API changes"

# Reply to a message
agent-mail reply 123 --from BlueLake --body "Looks good, approved!"

# Fetch inbox
agent-mail inbox BlueLake
agent-mail inbox BlueLake --limit 5 --urgent --bodies

# Acknowledge a message
agent-mail ack 123 --agent BlueLake

# Search messages
agent-mail search "authentication"

# View/summarize thread
agent-mail thread TKT-123 --summarize
```

### File Reservations

```bash
# Reserve files
agent-mail reserve "api/src/*.js" --agent BlueLake --ttl 7200

# Release reservations
agent-mail release --agent BlueLake

# Renew reservations
agent-mail renew --agent BlueLake --extend 3600
```

### Agent Management

```bash
# Register agent
agent-mail register --name BlueLake --task "Refactoring auth"

# Get agent info
agent-mail whoami BlueLake

# List contacts
agent-mail contacts list BlueLake
```

### Health Check

```bash
agent-mail health
```

## JSON Output

Add `--json` to any command for machine-readable output:

```bash
agent-mail inbox BlueLake --json
agent-mail search "error" --json | jq '.[] | .subject'
```

## Claude Code Integration

1. **Remove MCP server** from `~/.claude/settings.json` (delete `mcp-agent-mail` from `mcpServers`)

2. **Add CLI permission**:
   ```json
   {
     "permissions": {
       "allow": ["Bash(agent-mail:*)"]
     }
   }
   ```

3. **Update CLAUDE.md** (optional):
   ```markdown
   ## Agent Mail CLI
   Multi-agent coordination: `agent-mail --help`
   ```

## Server Setup

The CLI connects to an `mcp-agent-mail` server. Run it in Docker:

```bash
# Generate a token (save it for the CLI config)
TOKEN=$(openssl rand -hex 32)
echo "$TOKEN" > ~/.config/agent-mail/token

# Run with token auth (required for non-localhost access)
docker run -d --name agent-mail \
  --restart unless-stopped \
  -p 8765:8765 \
  -e HTTP_BEARER_TOKEN="$TOKEN" \
  -v ~/.mcp_agent_mail_git_mailbox_repo:/data/mailbox \
  mcp-agent-mail

# Or without auth (localhost only, dev environments)
docker run -d --name agent-mail \
  --restart unless-stopped \
  -p 8765:8765 \
  -e HTTP_RBAC_ENABLED=false \
  -v ~/.mcp_agent_mail_git_mailbox_repo:/data/mailbox \
  mcp-agent-mail
```

## License

MIT
