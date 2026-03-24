#!/usr/bin/env python3
"""
FreeGPT Agent — Claude Code-like capabilities powered by ChatGPT.

Extends FreeGPT with an agent loop that gives ChatGPT the ability to:
  - Execute shell commands (bash)
  - Read, write, and edit files
  - Search for files (glob) and search file contents (grep)
  - Browse directory structures

ChatGPT emits structured tool calls in fenced code blocks. This agent
parses them, executes locally with safety checks, and feeds results back
automatically until ChatGPT has completed the task.

Usage:
    python3 freegpt_agent.py                    # Interactive agent mode
    python3 freegpt_agent.py --visible          # Show browser for login
    python3 freegpt_agent.py --auto-approve     # Skip confirmation prompts
    python3 freegpt_agent.py --workdir /path    # Set working directory
"""

import sys
import os
import subprocess
import glob as glob_module
import re
import time
import signal
import argparse
import shutil
import json

# Import everything from freegpt
from freegpt import (
    ChatGPTSession, _Spinner, _banner, _wrap_print, _save_history,
    _read_multiline, PROFILE_DIR, HISTORY_FILE,
    BOLD, DIM, GREEN, CYAN, YELLOW, RED, MAGENTA, RESET,
)

# ── System Prompt ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert software engineer working as a coding agent. You have access to tools that let you interact with the user's local machine. You can read files, write files, edit files, run shell commands, and search the codebase.

## Available Tools

To use a tool, emit a fenced code block with `tool:TOOLNAME` as the language tag. You MUST use exactly this format.

### bash — Run a shell command
```tool:bash
ls -la /home/user/project
```

### read_file — Read a file's contents
```tool:read_file
/path/to/file.py
```

### write_file — Create or overwrite a file
```tool:write_file
path: /path/to/file.py
---
file contents here
```

### edit_file — Find and replace text in a file
```tool:edit_file
path: /path/to/file.py
---old
text to find (exact match)
---new
replacement text
```

### glob — Find files matching a pattern
```tool:glob
**/*.py
```

### grep — Search file contents with regex
```tool:grep
pattern: TODO|FIXME
path: /home/user/project
```

## Rules
- Use tools to explore before making changes. Read files before editing them.
- You can chain multiple tool calls in a single response.
- After each tool call, you'll receive the output. Use it to inform your next action.
- When you're done with a task and have no more tool calls, respond normally to the user.
- For destructive operations (deleting files, overwriting important files), explain what you're about to do.
- Keep file edits minimal and focused. Don't rewrite entire files when a small edit suffices.
- When running bash commands, prefer simple, safe commands. Avoid `rm -rf` on broad paths.
- The working directory is: {workdir}
"""

# ── Tool Parser ──────────────────────────────────────────────────────────────

TOOL_PATTERN = re.compile(
    r'```tool:(\w+)\s*\n(.*?)```',
    re.DOTALL
)

def parse_tool_calls(response_text):
    """Extract tool calls from ChatGPT's response.

    Returns list of (tool_name, tool_body) tuples.
    """
    calls = []
    for match in TOOL_PATTERN.finditer(response_text):
        tool_name = match.group(1).strip()
        tool_body = match.group(2).strip()
        calls.append((tool_name, tool_body))
    return calls

def strip_tool_blocks(response_text):
    """Remove tool call blocks from response text, leaving only prose."""
    cleaned = TOOL_PATTERN.sub("", response_text).strip()
    # Collapse multiple blank lines
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned

# ── Tool Executors ───────────────────────────────────────────────────────────

MAX_OUTPUT_CHARS = 15000  # Truncate large outputs to keep context manageable

def _truncate(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n\n... ({len(text) - limit} characters truncated) ...\n\n" + text[-half:]

def tool_bash(body, workdir):
    """Execute a shell command."""
    cmd = body.strip()
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=60, cwd=workdir,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if result.returncode != 0:
            output += f"\n[exit code: {result.returncode}]"
        return _truncate(output) if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "[error: command timed out after 60 seconds]"
    except Exception as e:
        return f"[error: {e}]"

def tool_read_file(body, workdir):
    """Read a file's contents."""
    path = body.strip()
    if not os.path.isabs(path):
        path = os.path.join(workdir, path)
    try:
        with open(path, "r") as f:
            content = f.read()
        if not content:
            return "(file is empty)"
        # Add line numbers
        lines = content.splitlines()
        numbered = "\n".join(f"{i+1:>5}  {line}" for i, line in enumerate(lines))
        return _truncate(numbered)
    except FileNotFoundError:
        return f"[error: file not found: {path}]"
    except IsADirectoryError:
        return f"[error: {path} is a directory, not a file]"
    except PermissionError:
        return f"[error: permission denied: {path}]"
    except Exception as e:
        return f"[error: {e}]"

def tool_write_file(body, workdir):
    """Write content to a file."""
    # Parse: first line is "path: /path/to/file", then "---", then content
    lines = body.split("\n")
    path = ""
    content_start = 0
    for i, line in enumerate(lines):
        if line.strip().startswith("path:"):
            path = line.split(":", 1)[1].strip()
        if line.strip() == "---":
            content_start = i + 1
            break

    if not path:
        return "[error: no path specified. Use 'path: /path/to/file' on the first line]"

    if not os.path.isabs(path):
        path = os.path.join(workdir, path)

    content = "\n".join(lines[content_start:])

    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        line_count = content.count("\n") + 1
        return f"[wrote {line_count} lines to {path}]"
    except Exception as e:
        return f"[error: {e}]"

def tool_edit_file(body, workdir):
    """Find and replace text in a file."""
    lines = body.split("\n")
    path = ""
    old_text = ""
    new_text = ""
    section = "header"  # header -> old -> new

    for line in lines:
        if section == "header":
            if line.strip().startswith("path:"):
                path = line.split(":", 1)[1].strip()
            elif line.strip() == "---old":
                section = "old"
        elif section == "old":
            if line.strip() == "---new":
                section = "new"
            else:
                old_text += ("" if not old_text else "\n") + line
        elif section == "new":
            new_text += ("" if not new_text else "\n") + line

    if not path:
        return "[error: no path specified]"
    if not old_text:
        return "[error: no old text specified (use ---old section)]"

    if not os.path.isabs(path):
        path = os.path.join(workdir, path)

    try:
        with open(path, "r") as f:
            content = f.read()

        if old_text not in content:
            return (f"[error: old text not found in {path}. "
                    f"Make sure the text matches exactly, including whitespace.]")

        new_content = content.replace(old_text, new_text, 1)
        with open(path, "w") as f:
            f.write(new_content)
        return f"[edited {path}: replaced {len(old_text)} chars with {len(new_text)} chars]"
    except FileNotFoundError:
        return f"[error: file not found: {path}]"
    except Exception as e:
        return f"[error: {e}]"

def tool_glob(body, workdir):
    """Find files matching a glob pattern."""
    pattern = body.strip()
    try:
        if not os.path.isabs(pattern):
            pattern = os.path.join(workdir, pattern)
        matches = sorted(glob_module.glob(pattern, recursive=True))
        if not matches:
            return "(no files matched)"
        result = "\n".join(matches[:200])
        if len(matches) > 200:
            result += f"\n... and {len(matches) - 200} more"
        return result
    except Exception as e:
        return f"[error: {e}]"

def tool_grep(body, workdir):
    """Search file contents with regex."""
    lines = body.strip().split("\n")
    pattern = ""
    search_path = workdir
    for line in lines:
        if line.strip().startswith("pattern:"):
            pattern = line.split(":", 1)[1].strip()
        elif line.strip().startswith("path:"):
            search_path = line.split(":", 1)[1].strip()
            if not os.path.isabs(search_path):
                search_path = os.path.join(workdir, search_path)
        elif not pattern:
            pattern = line.strip()

    if not pattern:
        return "[error: no search pattern specified]"

    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", "--include=*.js", "--include=*.ts",
             "--include=*.go", "--include=*.rs", "--include=*.java",
             "--include=*.c", "--include=*.h", "--include=*.cpp",
             "--include=*.rb", "--include=*.sh", "--include=*.yaml",
             "--include=*.yml", "--include=*.json", "--include=*.toml",
             "--include=*.md", "--include=*.txt", "--include=*.html",
             "--include=*.css", "--include=*.sql",
             "-E", pattern, search_path],
            capture_output=True, text=True, timeout=15,
        )
        output = result.stdout.strip()
        if not output:
            return f"(no matches for pattern: {pattern})"
        return _truncate(output)
    except subprocess.TimeoutExpired:
        return "[error: search timed out]"
    except FileNotFoundError:
        return "[error: grep not found on system]"
    except Exception as e:
        return f"[error: {e}]"

TOOLS = {
    "bash": tool_bash,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
    "edit_file": tool_edit_file,
    "glob": tool_glob,
    "grep": tool_grep,
}

# ── Safety ───────────────────────────────────────────────────────────────────

DANGEROUS_PATTERNS = [
    r'\brm\s+(-[rf]{1,3}\s+)?/',           # rm with absolute path
    r'\brm\s+-[rf]{2,}',                     # rm -rf / rm -fr
    r'\bmkfs\b',                              # format filesystem
    r'\bdd\b.*\bof=/',                        # dd writing to device
    r'>\s*/dev/sd',                            # redirect to block device
    r'\bchmod\s+-R\s+777\b',                  # world-writable recursive
    r'\bcurl\b.*\|\s*(bash|sh)',              # pipe curl to shell
    r'\bwget\b.*\|\s*(bash|sh)',              # pipe wget to shell
    r'\bsudo\b',                               # sudo commands
    r'\breboot\b',                             # reboot
    r'\bshutdown\b',                           # shutdown
    r'\bsystemctl\s+(stop|disable|mask)',      # stopping services
    r'\bgit\s+push\s+.*--force',              # force push
    r'\bgit\s+reset\s+--hard',                # hard reset
]

def is_dangerous(tool_name, body):
    """Check if a tool call is potentially dangerous."""
    if tool_name == "bash":
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, body):
                return True
    elif tool_name == "write_file":
        # Writing to system paths
        path_line = body.split("\n")[0]
        if any(p in path_line for p in ["/etc/", "/usr/", "/var/", "/root/",
                                         "/boot/", "/sys/", "/proc/"]):
            return True
    return False

def is_write_operation(tool_name):
    """Check if a tool modifies the filesystem."""
    return tool_name in ("bash", "write_file", "edit_file")

# ── Agent Display ────────────────────────────────────────────────────────────

def _print_tool_call(tool_name, body, index=0):
    """Display a tool call in the terminal."""
    icons = {
        "bash": "⚡", "read_file": "📄", "write_file": "✏️",
        "edit_file": "🔧", "glob": "🔍", "grep": "🔎",
    }
    icon = icons.get(tool_name, "🔧")
    print(f"\n  {icon} {BOLD}{CYAN}tool:{tool_name}{RESET}")

    # Show a preview of the body
    preview_lines = body.strip().splitlines()
    for line in preview_lines[:8]:
        print(f"  {DIM}│{RESET} {line}")
    if len(preview_lines) > 8:
        print(f"  {DIM}│ ... ({len(preview_lines) - 8} more lines){RESET}")

def _print_tool_result(output, tool_name):
    """Display tool output in the terminal."""
    if not output or output == "(no output)":
        print(f"  {DIM}└─ (no output){RESET}")
        return

    lines = output.splitlines()
    # Show first 15 lines, truncate rest
    show = min(len(lines), 15)
    for line in lines[:show]:
        print(f"  {DIM}│{RESET} {line[:120]}")
    if len(lines) > show:
        print(f"  {DIM}│ ... ({len(lines) - show} more lines){RESET}")
    print(f"  {DIM}└─ done{RESET}")

def _print_prose(text):
    """Print ChatGPT's non-tool response text."""
    text = text.strip()
    if text:
        print(f"\n{CYAN}{BOLD}ChatGPT:{RESET}")
        _wrap_print(text, prefix="  ")
        print()

# ── Agent Banner ─────────────────────────────────────────────────────────────

def _agent_banner():
    print(f"""
{MAGENTA}╔══════════════════════════════════════════════════╗
║  {BOLD}FreeGPT Agent{RESET}{MAGENTA} — AI coding assistant              ║
║  ChatGPT + local tools (read/write/bash/search)  ║
╚══════════════════════════════════════════════════╝{RESET}
""")

# ── Agent Help ───────────────────────────────────────────────────────────────

AGENT_HELP = f"""
{BOLD}Commands:{RESET}
  {CYAN}/new{RESET}          Start a new conversation
  {CYAN}/model{RESET}        Show current model
  {CYAN}/clear{RESET}        Clear terminal screen
  {CYAN}/history{RESET}      Show conversation history
  {CYAN}/workdir{RESET}      Show/change working directory
  {CYAN}/approve{RESET}      Toggle auto-approve mode
  {CYAN}/help{RESET}         Show this help message
  {CYAN}/quit{RESET}         Exit

{BOLD}How it works:{RESET}
  {DIM}Type a task (e.g., "read main.py and add error handling").
  ChatGPT will use tools to explore, edit, and test your code.
  You'll see each tool call and can approve/deny write operations.{RESET}
"""

# ── Agent Loop ───────────────────────────────────────────────────────────────

def agent_loop(session, workdir, auto_approve=False, debug=False):
    """Main agent REPL — sends messages, parses tool calls, executes, loops."""
    history = []
    turn = 0

    # Inject system prompt on first message
    system_injected = False

    while True:
        try:
            user_input = _read_multiline()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{DIM}Closing...{RESET}")
            break

        user_input = user_input.strip()
        if not user_input:
            continue

        # Handle commands
        if user_input.startswith("/"):
            cmd = user_input.lower().split()[0]

            if cmd in ("/quit", "/exit", "/q"):
                print(f"{DIM}Closing...{RESET}")
                break
            elif cmd == "/new":
                session.start_new_conversation()
                history = []
                turn = 0
                system_injected = False
                print(f"{GREEN}[+] New conversation started{RESET}\n")
                continue
            elif cmd == "/model":
                model = session.get_model_info()
                print(f"  Model: {model or 'unknown'}\n")
                continue
            elif cmd == "/clear":
                os.system("clear" if os.name == "posix" else "cls")
                _agent_banner()
                continue
            elif cmd == "/history":
                if not history:
                    print(f"  {DIM}(no messages yet){RESET}\n")
                else:
                    for entry in history:
                        role = entry["role"]
                        text = entry["text"][:200]
                        if role == "user":
                            print(f"  {GREEN}You:{RESET} {text}")
                        else:
                            print(f"  {CYAN}GPT:{RESET} {text}")
                    print()
                continue
            elif cmd == "/workdir":
                parts = user_input.split(None, 1)
                if len(parts) > 1:
                    new_dir = os.path.expanduser(parts[1])
                    if os.path.isdir(new_dir):
                        workdir = os.path.abspath(new_dir)
                        print(f"  {GREEN}Working directory: {workdir}{RESET}\n")
                    else:
                        print(f"  {RED}Not a directory: {new_dir}{RESET}\n")
                else:
                    print(f"  Working directory: {workdir}\n")
                continue
            elif cmd == "/approve":
                auto_approve = not auto_approve
                state = "ON" if auto_approve else "OFF"
                print(f"  Auto-approve: {BOLD}{state}{RESET}\n")
                continue
            elif cmd == "/help":
                print(AGENT_HELP)
                continue
            else:
                print(f"  {YELLOW}Unknown command: {cmd}. Type /help.{RESET}\n")
                continue

        # Prepend system prompt to the first user message
        if not system_injected:
            full_message = (SYSTEM_PROMPT.format(workdir=workdir) +
                            "\n\n---\n\nUser request:\n" + user_input)
            system_injected = True
        else:
            full_message = user_input

        turn += 1
        history.append({"role": "user", "text": user_input, "turn": turn})

        # Agent loop: send message, parse tools, execute, feed back, repeat
        current_message = full_message
        max_iterations = 25  # safety cap on auto-loop depth

        for iteration in range(max_iterations):
            response = session.send_message(current_message)

            if not response or response in ("(no response received)", "(empty response)"):
                print(f"{RED}  [!] No response received.{RESET}\n")
                break

            history.append({"role": "assistant", "text": response, "turn": turn})

            # Parse tool calls
            tool_calls = parse_tool_calls(response)

            # Print any prose (non-tool text)
            prose = strip_tool_blocks(response)
            if prose:
                _print_prose(prose)

            if not tool_calls:
                # No tools = ChatGPT is done, task complete
                if not prose:
                    # Response was only tool blocks with no prose and no calls parsed
                    # This might be a formatting issue — show raw response
                    _print_prose(response)
                break

            # Execute each tool call
            tool_outputs = []
            aborted = False

            for i, (tool_name, tool_body) in enumerate(tool_calls):
                _print_tool_call(tool_name, tool_body, i)

                if tool_name not in TOOLS:
                    output = f"[error: unknown tool '{tool_name}'. Available: {', '.join(TOOLS.keys())}]"
                    print(f"  {RED}{output}{RESET}")
                    tool_outputs.append((tool_name, output))
                    continue

                # Safety checks
                needs_approval = False
                if is_dangerous(tool_name, tool_body):
                    print(f"  {RED}{BOLD}⚠ DANGEROUS COMMAND DETECTED{RESET}")
                    needs_approval = True
                elif is_write_operation(tool_name) and not auto_approve:
                    needs_approval = True

                if needs_approval:
                    try:
                        answer = input(f"  {YELLOW}Execute? (y/n/a=approve all): {RESET}").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = "n"

                    if answer == "a":
                        auto_approve = True
                        print(f"  {DIM}Auto-approve enabled for this session{RESET}")
                    elif answer != "y":
                        output = "[skipped by user]"
                        print(f"  {DIM}└─ skipped{RESET}")
                        tool_outputs.append((tool_name, output))
                        continue

                # Execute the tool
                spinner = _Spinner(f"Running {tool_name}")
                spinner.start()
                try:
                    output = TOOLS[tool_name](tool_body, workdir)
                finally:
                    spinner.stop()

                _print_tool_result(output, tool_name)
                tool_outputs.append((tool_name, output))

            if aborted:
                break

            # Build the feedback message with all tool results
            feedback_parts = []
            for tool_name, output in tool_outputs:
                feedback_parts.append(f"Result of tool:{tool_name}:\n```\n{output}\n```")

            feedback_message = "\n\n".join(feedback_parts)
            history.append({"role": "tool_result", "text": feedback_message, "turn": turn})

            # Send tool results back to ChatGPT for next iteration
            if debug:
                print(f"\n  {DIM}[agent loop iteration {iteration + 1}, "
                      f"sending {len(tool_outputs)} result(s) back]{RESET}")

            current_message = feedback_message

        # End of agent loop for this turn
        if debug:
            print(f"  {DIM}[agent loop ended after {min(iteration + 1, max_iterations)} "
                  f"iteration(s)]{RESET}")

    # Save history
    if history:
        _save_history(history)

# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="freegpt-agent",
        description="FreeGPT Agent — AI coding assistant with local tool access",
    )
    parser.add_argument("--visible", action="store_true",
                        help="Show browser window (for login)")
    parser.add_argument("--reset", action="store_true",
                        help="Clear saved session")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose output")
    parser.add_argument("--auto-approve", action="store_true",
                        help="Auto-approve all write operations (use with caution)")
    parser.add_argument("--workdir", type=str, default=os.getcwd(),
                        help="Working directory (default: current directory)")
    args = parser.parse_args()

    if args.reset:
        if os.path.exists(PROFILE_DIR):
            shutil.rmtree(PROFILE_DIR)
            print(f"[+] Session cleared.")

    _agent_banner()

    workdir = os.path.abspath(os.path.expanduser(args.workdir))
    print(f"  {DIM}Working directory: {workdir}{RESET}")

    headless = not args.visible
    session = ChatGPTSession(headless=headless, debug=args.debug)

    def _sigint(sig, frame):
        print(f"\n{DIM}Closing...{RESET}")
        session.close()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    # Launch browser
    spinner = _Spinner("Launching browser")
    spinner.start()
    try:
        session.launch()
    except Exception as e:
        spinner.stop()
        print(f"{RED}[!] Failed to launch browser: {e}{RESET}")
        print(f"    pip install playwright playwright-stealth && playwright install firefox")
        sys.exit(1)
    spinner.stop()

    # Navigate and login
    if not session.navigate_to_chat():
        print(f"{RED}[!] Could not establish ChatGPT session.{RESET}")
        session.close()
        sys.exit(1)

    model = session.get_model_info()
    if model:
        print(f"  {DIM}Model: {model}{RESET}")

    approve_state = "ON" if args.auto_approve else "OFF (will prompt for writes)"
    print(f"  {DIM}Auto-approve: {approve_state}{RESET}")
    print()
    print(f"  {DIM}Type a task to start. /help for commands.{RESET}")
    print()

    agent_loop(session, workdir, auto_approve=args.auto_approve, debug=args.debug)

    session.close()

if __name__ == "__main__":
    main()
