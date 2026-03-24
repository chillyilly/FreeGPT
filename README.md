# FreeGPT

**Interactive CLI for ChatGPT via the public website** no API key required. Includes a basic chat client and an **agent mode** that gives ChatGPT Claude Code-like capabilities: reading files, writing code, running shell commands, and searching your codebase - all executed locally through an automated tool loop.

```
╔══════════════════════════════════════════════════╗
║  FreeGPT - ChatGPT via the public website        ║
║  No API key required                             ║
╚══════════════════════════════════════════════════╝

You > What is the capital of France?

  Thinking  ·∙ ◈ ∙●

ChatGPT:
  The capital of France is Paris.

You > _
```

---

## Two Modes

### `freegpt.py` - Chat Client
Simple conversational interface. Type messages, get responses. Supports multi-turn conversations, session persistence, and clipboard copy.

### `freegpt_agent.py` - Coding Agent
Gives ChatGPT the ability to interact with your local machine through 6 tools. ChatGPT decides which tools to use, this agent executes them locally and feeds results back automatically - looping until the task is complete.

```
╔══════════════════════════════════════════════════╗
║  FreeGPT Agent - AI coding assistant             ║
║  ChatGPT + local tools (read/write/bash/search)  ║
╚══════════════════════════════════════════════════╝

  Working directory: /home/user/myproject
  Auto-approve: OFF (will prompt for writes)

You > read main.py and add error handling to the database connection

  tool:read_file
  │ main.py

  │    1  import psycopg2
  │    2
  │    3  def connect():
  │    4      conn = psycopg2.connect("dbname=mydb")
  │    5      return conn
  └─ done

  tool:edit_file
  │ path: main.py
  │ ---old
  │ def connect():
  │     conn = psycopg2.connect("dbname=mydb")
  │     return conn
  │ ---new
  │ def connect():
  │     try:
  │         conn = psycopg2.connect("dbname=mydb")
  │         return conn
  │     except psycopg2.Error as e:
  │         print(f"Database connection failed: {e}")
  │         return None
  Execute? (y/n/a=approve all): y
  │ [edited main.py: replaced 64 chars with 173 chars]
  └─ done

ChatGPT:
  I've added a try/except block around the database connection that catches
  psycopg2 errors and returns None instead of crashing.
```

---

## How It Works

Both modes use **Playwright** (headless Firefox) with **anti-detection stealth patches** to interact with the ChatGPT web interface exactly as a normal browser would.

1. A stealth Firefox instance navigates to `chatgpt.com`
2. On first run, a browser window opens for you to log in with your account
3. Your session (cookies, localStorage) is saved to `~/.freegpt/browser_profile/`
4. On subsequent runs, the saved session is reused - no login needed
5. Messages are typed into ChatGPT's ProseMirror editor and submitted via the send button
6. Responses are tracked by their unique `data-message-id` to avoid confusing turns
7. A spiral animation displays while waiting for responses

### Stealth Measures

The browser session applies anti-detection techniques to avoid triggering CAPTCHA:

- `navigator.webdriver = false` - hides automation flag
- Fake browser plugins (real browsers have 3+, headless has 0)
- Spoofed WebGL vendor/renderer strings
- Consistent locale, timezone, and color scheme
- Randomized viewport dimensions
- Persistent browser profile (returning user, not a fresh bot)
- Homepage visit before target navigation (natural browsing pattern)
- Automatic stale lock file cleanup on launch

---

## Installation

```bash
git clone https://github.com/chillyilly/freegpt.git
cd freegpt

# Install dependencies
pip install playwright playwright-stealth
playwright install firefox
```

### Dependencies

| Package | Purpose |
|---------|---------|
| `playwright` | Headless browser automation |
| `playwright-stealth` | Anti-detection patches (webdriver, plugins, WebGL, etc.) |

Python 3.8+ required. No other dependencies - all tool execution uses the Python standard library.

---

## Usage

### First Run (Login)

```bash
python3 freegpt.py --visible
```

The `--visible` flag opens a browser window so you can log in to your ChatGPT account. After logging in, your session is saved automatically. Future runs use the saved session headlessly.

### Chat Mode

```bash
python3 freegpt.py
```

Simple conversational interface. Type messages, get responses.

#### Chat Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a new conversation |
| `/model` | Show current ChatGPT model |
| `/history` | Show conversation history for this session |
| `/copy` | Copy last response to clipboard (`xclip` or `xsel`) |
| `/clear` | Clear the terminal screen |
| `/help` | Show available commands |
| `/quit` | Exit FreeGPT |
| `Ctrl+C` | Exit FreeGPT |

#### Multi-Line Input

End a line with `\` to continue on the next line:

```
You > Write a function that \
  ... takes a list and \
  ... returns the sum

ChatGPT:
  def sum_list(lst):
      return sum(lst)
```

#### Chat CLI Arguments

| Argument | Description |
|----------|-------------|
| `--visible` | Show the browser window (required for first login) |
| `--reset` | Clear saved session and force re-login |
| `--debug` | Verbose debug output |

---

### Agent Mode

```bash
python3 freegpt_agent.py --workdir ~/myproject
```

Gives ChatGPT access to 6 local tools. Describe a task and ChatGPT will autonomously read files, make edits, run commands, and search your code until the task is complete.

#### Agent Tools

| Tool | Description | Approval Required |
|------|-------------|:-:|
| `bash` | Execute shell commands (60s timeout) | Yes |
| `read_file` | Read file contents with line numbers | No |
| `write_file` | Create or overwrite a file | Yes |
| `edit_file` | Find and replace text in a file | Yes |
| `glob` | Find files matching a pattern | No |
| `grep` | Search file contents with regex | No |

ChatGPT emits tool calls in structured fenced blocks:

````
```tool:bash
ls -la src/
```

```tool:read_file
src/main.py
```

```tool:edit_file
path: src/main.py
---old
def connect():
    conn = db.connect()
---new
def connect():
    try:
        conn = db.connect()
    except Exception as e:
        log.error(f"Connection failed: {e}")
        return None
```
````

The agent parses these, executes locally, and sends results back to ChatGPT for the next iteration - up to 25 auto-loop iterations per task.

#### Agent Commands

| Command | Description |
|---------|-------------|
| `/new` | Start a new conversation |
| `/model` | Show current model |
| `/workdir` | Show or change working directory |
| `/approve` | Toggle auto-approve mode on/off |
| `/history` | Show conversation history |
| `/clear` | Clear terminal screen |
| `/help` | Show available commands |
| `/quit` | Exit |

#### Agent CLI Arguments

| Argument | Description |
|----------|-------------|
| `--visible` | Show browser window (for login) |
| `--reset` | Clear saved session |
| `--debug` | Verbose output (shows agent loop iterations) |
| `--auto-approve` | Skip confirmation prompts for write operations |
| `--workdir PATH` | Set working directory (default: current directory) |

#### Safety

Write operations (`bash`, `write_file`, `edit_file`) require user confirmation before execution:

```
  🔧 tool:edit_file
  │ path: src/config.py
  │ ---old
  │ DEBUG = True
  │ ---new
  │ DEBUG = False
  Execute? (y/n/a=approve all): y
```

**Dangerous command detection** - commands matching these patterns get an extra red warning:

- `rm -rf` with broad paths
- `sudo`, `mkfs`, `dd of=/dev/`
- `curl | bash`, `wget | sh`
- `git push --force`, `git reset --hard`
- `reboot`, `shutdown`
- `chmod -R 777`
- `systemctl stop/disable`

Typing `a` at the approval prompt enables auto-approve for the rest of the session (non-dangerous operations only). The `--auto-approve` flag enables this from the start.

#### How the Agent Loop Works

```
User describes task
    │
    ▼
System prompt injected (tells ChatGPT about tools)
    │
    ▼
Message sent to ChatGPT ──────────────────────────┐
    │                                               │
    ▼                                               │
ChatGPT responds with tool calls + prose            │
    │                                               │
    ▼                                               │
Agent parses tool:xxx blocks from response          │
    │                                               │
    ▼                                               │
Safety check → prompt user if write/dangerous       │
    │                                               │
    ▼                                               │
Execute locally, capture output                     │
    │                                               │
    ▼                                               │
Format results, send back to ChatGPT ──────────────┘
    │
    (loops until ChatGPT responds with
     no tool calls = task complete)
```

Maximum 25 iterations per task. Bash commands timeout after 60 seconds. Large outputs are truncated to 15,000 characters to keep ChatGPT's context manageable.

---

## Session Management

Sessions are stored in `~/.freegpt/browser_profile/`. This directory contains Firefox profile data (cookies, localStorage) that keeps you logged in across runs.

```bash
# Reset session (force re-login on next run)
python3 freegpt.py --reset

# Or manually delete the profile
rm -rf ~/.freegpt/browser_profile/
```

Conversation history is saved to `~/.freegpt/history.json` on exit.

Both `freegpt.py` and `freegpt_agent.py` share the same browser profile - log in once and both modes work.

---

## File Structure

```
freegpt/
├── freegpt.py          # Core: browser session, chat client, stealth engine
├── freegpt_agent.py    # Agent: tool system, safety checks, auto-loop
└── README.md
```

`freegpt_agent.py` imports from `freegpt.py` - the browser session, spinner, terminal helpers, and REPL utilities are shared.

---

## Example Sessions

### Chat Mode

```
╔══════════════════════════════════════════════════╗
║  FreeGPT - ChatGPT via the public website        ║
║  No API key required                              ║
╚══════════════════════════════════════════════════╝

[+] Session active - logged in
  Model: ChatGPT

  Type /help for commands. Just type to chat.

You > Explain DNS SPF records in two sentences.

ChatGPT:
  An SPF (Sender Policy Framework) record is a DNS TXT record that specifies
  which mail servers are authorized to send email on behalf of a domain. When
  a receiving server gets an email, it checks the SPF record to verify the
  message came from an authorized server, helping prevent email spoofing.

You > /new
[+] New conversation started

You > What's 2+2?

ChatGPT:
  4.

You > /quit
Closing...
```

### Agent Mode

```
╔══════════════════════════════════════════════════╗
║  FreeGPT Agent - AI coding assistant              ║
║  ChatGPT + local tools (read/write/bash/search)  ║
╚══════════════════════════════════════════════════╝

  Working directory: /home/user/myproject
  Auto-approve: OFF (will prompt for writes)

You > find all python files and list any that import requests

  🔍 tool:glob
  │ **/*.py
  │ /home/user/myproject/src/api.py
  │ /home/user/myproject/src/scraper.py
  │ /home/user/myproject/tests/test_api.py
  └─ done

  🔎 tool:grep
  │ pattern: import requests
  │ path: /home/user/myproject
  │ /home/user/myproject/src/api.py:3:import requests
  │ /home/user/myproject/src/scraper.py:1:import requests
  └─ done

ChatGPT:
  Two files import requests:
  - src/api.py (line 3)
  - src/scraper.py (line 1)
```

---

## Troubleshooting

### "playwright not installed"
```bash
pip install playwright playwright-stealth
playwright install firefox
```

### "Could not find message input field"
ChatGPT may have changed their UI. Try:
- `--visible` to see what the browser sees
- `--reset` to clear stale session data
- Check if ChatGPT is experiencing an outage

### CAPTCHA appearing
The stealth patches handle most bot detection. If CAPTCHA persists:
- Use `--visible` and solve the CAPTCHA manually once
- The session will be saved and future runs won't trigger it
- Avoid running too many rapid sessions from the same IP

### Login doesn't persist
Make sure `~/.freegpt/browser_profile/` is writable. The persistent browser context stores cookies and localStorage there.

### "Firefox is already running"
A previous session crashed and left a lock file. FreeGPT auto-cleans these on launch, but you can also manually fix:
```bash
rm -f ~/.freegpt/browser_profile/lock ~/.freegpt/browser_profile/.parentlock
```

### Agent repeating old responses
The agent tracks responses by their unique `data-message-id` attribute to prevent confusing old and new messages. If issues persist, try `/new` to start a fresh conversation.

---

## Responsible Use

This tool is for **personal use with your own ChatGPT account**. It automates the same actions you'd perform manually in a browser - typing messages and reading responses. It does not bypass any paywalls or access restrictions beyond what your account allows.

The agent mode executes commands on **your local machine** with **your user permissions**. Review tool calls before approving, especially `bash` commands. The safety system flags dangerous patterns but cannot catch everything.

---

## License

MIT
