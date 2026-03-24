#!/usr/bin/env python3
"""
FreeGPT — Interactive CLI for ChatGPT via the public website.

Uses a stealth headless browser to communicate with chatgpt.com directly,
bypassing the need for an API key. Persists login sessions across runs
via a saved browser profile.

Usage:
    python3 freegpt.py              # Launch interactive session
    python3 freegpt.py --visible    # Show the browser window (useful for first login)
    python3 freegpt.py --reset      # Clear saved session and re-login
"""

import sys
import os
import time
import json
import shutil
import argparse
import textwrap
import signal
import threading
import re

PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".freegpt", "browser_profile")
HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".freegpt", "history.json")

# ── Spinner ──────────────────────────────────────────────────────────────────

class _Spinner:
    """Spiral animation while waiting for response."""
    @staticmethod
    def _generate_frames():
        import math
        width = 11
        center = width // 2
        n_frames = 32
        frames = []
        for f in range(n_frames):
            angle = (2 * math.pi * f) / n_frames
            buf = [" "] * width
            for arm_offset in [0, math.pi]:
                a = angle + arm_offset
                x = center + math.sin(a) * (center - 1)
                ix = max(0, min(width - 1, int(round(x))))
                depth = (math.cos(a) + 1) / 2
                ch = "●" if depth > 0.6 else "∙" if depth > 0.3 else "·"
                buf[ix] = ch
                trail_x = ix - (1 if math.sin(a) > 0 else -1)
                if 0 <= trail_x < width and buf[trail_x] == " ":
                    buf[trail_x] = "∙" if depth > 0.5 else "·" if depth > 0.2 else " "
            hub = "◇◈◆◈"
            buf[center] = hub[f % len(hub)]
            frames.append("".join(buf))
        return frames

    def __init__(self, message=""):
        self._message = message
        self._stop = threading.Event()
        self._thread = None
        self._frames = self._generate_frames()

    def start(self, message=None):
        if message:
            self._message = message
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        idx = 0
        n = len(self._frames)
        while not self._stop.is_set():
            frame = self._frames[idx % n]
            sys.stdout.write(f"\033[2K\r  {self._message}  {frame}")
            sys.stdout.flush()
            idx += 1
            self._stop.wait(0.08)
        sys.stdout.write("\033[2K\r")
        sys.stdout.flush()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)

# ── Terminal Helpers ─────────────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[38;5;114m"
CYAN = "\033[38;5;80m"
YELLOW = "\033[38;5;222m"
RED = "\033[38;5;203m"
MAGENTA = "\033[38;5;176m"
RESET = "\033[0m"

def _banner():
    print(f"""
{CYAN}╔══════════════════════════════════════════════════╗
║  {BOLD}FreeGPT{RESET}{CYAN} — ChatGPT via the public website         ║
║  No API key required                              ║
╚══════════════════════════════════════════════════╝{RESET}
""")

def _wrap_print(text, prefix="", width=None):
    """Print text with word wrapping."""
    if width is None:
        try:
            width = os.get_terminal_size().columns - 4
        except OSError:
            width = 76
    width = max(40, width)
    for line in text.splitlines():
        if not line.strip():
            print()
            continue
        wrapped = textwrap.fill(line, width=width, initial_indent=prefix,
                                subsequent_indent=" " * len(prefix))
        print(wrapped)

# ── Browser Session ──────────────────────────────────────────────────────────

class ChatGPTSession:
    """Manages a stealth browser session with chatgpt.com."""

    def __init__(self, headless=True, debug=False):
        self.headless = headless
        self.debug = debug
        self.browser = None
        self.context = None
        self.page = None
        self._pw = None
        self._playwright = None
        self._streaming_text = ""
        self._response_done = threading.Event()
        self._last_response = ""

    def _ensure_profile_dir(self):
        os.makedirs(os.path.dirname(PROFILE_DIR), exist_ok=True)
        os.makedirs(PROFILE_DIR, exist_ok=True)
        # Clean stale Firefox lock files from previous crashed sessions
        for lockfile in ("lock", ".parentlock"):
            p = os.path.join(PROFILE_DIR, lockfile)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def launch(self):
        """Launch the browser with stealth patches and persistent session."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(f"{RED}[!] playwright not installed. Run:{RESET}")
            print(f"    pip install playwright playwright-stealth")
            print(f"    playwright install firefox")
            sys.exit(1)

        self._ensure_profile_dir()

        self._pw = sync_playwright().start()

        # Use persistent context for session storage (cookies, localStorage)
        launch_args = {
            "headless": self.headless,
            "firefox_user_prefs": {
                "media.autoplay.default": 5,
                "media.autoplay.blocking_policy": 2,
            },
        }

        # Apply stealth patches
        stealth_obj = None
        try:
            from playwright_stealth import Stealth
            stealth_obj = Stealth(
                navigator_webdriver=True,
                navigator_plugins=True,
                navigator_languages=True,
                navigator_platform=True,
                navigator_vendor=True,
                navigator_user_agent=True,
                webgl_vendor=True,
                chrome_runtime=True,
                chrome_app=True,
                media_codecs=True,
                navigator_hardware_concurrency=True,
                iframe_content_window=True,
                navigator_permissions=True,
                sec_ch_ua=True,
                error_prototype=True,
            )
        except ImportError:
            if self.debug:
                print(f"{YELLOW}[warn] playwright-stealth not installed, "
                      f"running without stealth patches{RESET}")

        import random
        # Persistent context saves login state between runs
        self.context = self._pw.firefox.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=self.headless,
            viewport={
                "width": random.choice([1366, 1440, 1536]),
                "height": random.choice([768, 900, 864]),
            },
            locale="en-US",
            timezone_id="America/New_York",
            color_scheme="dark",
        )

        if stealth_obj:
            stealth_obj.apply_stealth_sync(self.context)
            if self.debug:
                print(f"{DIM}  Stealth patches applied{RESET}")

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def navigate_to_chat(self):
        """Navigate to chatgpt.com and handle login if needed."""
        spinner = _Spinner("Connecting to ChatGPT")
        spinner.start()

        try:
            self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                           timeout=30000)
            self.page.wait_for_timeout(3000)

            # Dismiss any overlays/modals
            self._dismiss_overlays()
            self.page.wait_for_timeout(2000)
        finally:
            spinner.stop()

        # Check if we're logged in
        if self._is_logged_in():
            print(f"{GREEN}[+] Session active — logged in{RESET}")
            self._dismiss_overlays()
            return True
        else:
            return self._handle_login()

    def _is_logged_in(self):
        """Check if we're on the chat interface (logged in)."""
        try:
            # Look for the message input area — present only when logged in
            has_input = self.page.evaluate("""() => {
                return !!(document.querySelector('textarea, [contenteditable="true"], #prompt-textarea') ||
                         document.querySelector('[data-testid="send-button"]'));
            }""")
            if has_input:
                return True

            # Check URL — /auth/login means not logged in
            url = self.page.url
            if "/auth/" in url or "login" in url:
                return False

            # Check for "Log in" or "Sign up" buttons
            has_login_btn = self.page.evaluate("""() => {
                const btns = document.querySelectorAll('button, a');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    if (t === 'log in' || t === 'sign up' || t === 'get started')
                        return true;
                }
                return false;
            }""")
            return not has_login_btn
        except Exception:
            return False

    def _handle_login(self):
        """Guide the user through login."""
        print()
        print(f"{YELLOW}[!] Not logged in to ChatGPT.{RESET}")
        print()

        if self.headless:
            print(f"  You need to log in. Options:")
            print(f"    1) Re-run with {BOLD}--visible{RESET} to see the browser and log in")
            print(f"    2) The browser window will open now for you to log in")
            print()

            # Relaunch in visible mode for login
            print(f"{CYAN}[*] Opening browser window for login...{RESET}")
            self.close()
            self.headless = False
            self.launch()
            self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                           timeout=30000)
            self.page.wait_for_timeout(3000)

        print(f"{BOLD}  Please log in to ChatGPT in the browser window.{RESET}")
        print(f"  Waiting for login to complete...")
        print()

        # Poll until logged in or timeout
        timeout = 300  # 5 minutes to log in
        start = time.time()
        while time.time() - start < timeout:
            self._dismiss_overlays()
            if self._is_logged_in():
                print(f"{GREEN}[+] Login successful!{RESET}")
                # Switch back to headless on next run (session is saved)
                self.page.wait_for_timeout(2000)
                self._dismiss_overlays()
                return True
            time.sleep(2)

        print(f"{RED}[!] Login timeout (5 minutes). Please try again.{RESET}")
        return False

    def _dismiss_overlays(self):
        """Dismiss cookie banners, modals, onboarding dialogs."""
        try:
            self.page.evaluate("""() => {
                // Dismiss common overlays
                const dismissTexts = ['dismiss', 'got it', 'okay', 'ok', 'close',
                                      'accept', 'continue', 'skip', 'no thanks',
                                      'maybe later', "i'm good", 'stay logged out'];
                const btns = document.querySelectorAll('button, [role="button"]');
                for (const btn of btns) {
                    const t = btn.textContent.trim().toLowerCase();
                    for (const dt of dismissTexts) {
                        if (t === dt || t.startsWith(dt)) {
                            btn.click();
                            break;
                        }
                    }
                }
                // Remove modal backdrops
                document.querySelectorAll('[class*="modal-backdrop"], [class*="overlay"]')
                    .forEach(el => {
                        if (el.style && el.style.position === 'fixed') el.remove();
                    });
            }""")
        except Exception:
            pass

    def _get_known_message_ids(self):
        """Get the set of all data-message-id values currently in the DOM."""
        try:
            return set(self.page.evaluate("""() => {
                return Array.from(
                    document.querySelectorAll('[data-message-id]')
                ).map(el => el.getAttribute('data-message-id'));
            }"""))
        except Exception:
            return set()

    def _get_response_by_id(self, msg_id):
        """Get the innerText of a specific message by its data-message-id."""
        try:
            return self.page.evaluate("""(mid) => {
                const el = document.querySelector('[data-message-id="' + mid + '"]');
                if (!el) return '';
                return el.innerText.trim();
            }""", msg_id)
        except Exception:
            return ""

    def _find_new_assistant_id(self, known_ids):
        """Find a new assistant message ID that wasn't in known_ids."""
        try:
            return self.page.evaluate("""(knownIds) => {
                const msgs = document.querySelectorAll(
                    '[data-message-author-role="assistant"][data-message-id]'
                );
                for (const m of msgs) {
                    const mid = m.getAttribute('data-message-id');
                    if (mid && !knownIds.includes(mid)) return mid;
                }
                return null;
            }""", list(known_ids))
        except Exception:
            return None

    def _is_generating(self):
        """Check if ChatGPT is still generating."""
        try:
            return self.page.evaluate("""() => {
                const btns = document.querySelectorAll('button');
                for (const b of btns) {
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    const testid = (b.getAttribute('data-testid') || '').toLowerCase();
                    if (label.includes('stop') || testid.includes('stop'))
                        return true;
                }
                return false;
            }""")
        except Exception:
            return False

    def send_message(self, message):
        """Type a message and send it to ChatGPT. Returns the response text."""
        self._dismiss_overlays()

        # Snapshot all existing message IDs before sending
        known_ids = self._get_known_message_ids()

        # Focus and fill the ProseMirror input
        # Click the input area first to ensure focus
        try:
            pm = self.page.locator("#prompt-textarea").first
            if pm.is_visible(timeout=2000):
                pm.click()
                self.page.wait_for_timeout(200)
        except Exception:
            pass

        self.page.evaluate("""(text) => {
            const pm = document.querySelector('#prompt-textarea');
            const ta = document.querySelector('textarea');
            if (pm) {
                pm.focus();
                pm.innerHTML = '<p>' + text + '</p>';
                pm.dispatchEvent(new Event('input', {bubbles: true}));
            } else if (ta) {
                ta.focus();
                ta.value = text;
                ta.dispatchEvent(new Event('input', {bubbles: true}));
            }
        }""", message)

        self.page.wait_for_timeout(400)

        # Click send button
        sent = False
        for selector in ['[data-testid="send-button"]',
                         'button[aria-label="Send prompt"]',
                         'button[aria-label="Send"]']:
            try:
                btn = self.page.locator(selector).first
                if btn.is_visible(timeout=1000):
                    btn.click()
                    sent = True
                    break
            except Exception:
                continue

        if not sent:
            self.page.keyboard.press("Enter")

        self.page.wait_for_timeout(500)
        return self._wait_for_response(known_ids)

    def _wait_for_response(self, known_ids, timeout=120):
        """Wait for a NEW assistant message that wasn't in known_ids.

        Tracks the specific message by its data-message-id to avoid
        confusing it with old messages from previous turns.
        """
        spinner = _Spinner("Thinking")
        spinner.start()

        start = time.time()
        new_msg_id = None
        last_text = ""
        stable_ticks = 0

        try:
            while time.time() - start < timeout:
                time.sleep(0.5)

                # Phase 1: find the new assistant message
                if not new_msg_id:
                    new_msg_id = self._find_new_assistant_id(known_ids)
                    if not new_msg_id:
                        # Be patient — first message on a fresh page can take
                        # a while before the assistant element appears in DOM
                        if time.time() - start > 30 and not self._is_generating():
                            break  # no response coming
                        continue

                # Phase 2: read text from this specific message
                text = self._get_response_by_id(new_msg_id)

                if not text:
                    continue

                # Phase 3: wait for text to stabilize + generation to finish
                if text == last_text:
                    stable_ticks += 1
                    if not self._is_generating() and stable_ticks >= 3:
                        break
                    if stable_ticks >= 12:
                        break
                else:
                    stable_ticks = 0
                    last_text = text
        finally:
            spinner.stop()

        if new_msg_id:
            return self._get_response_by_id(new_msg_id) or "(empty response)"
        return "(no response received)"

    def start_new_conversation(self):
        """Start a new conversation by clicking the new chat button."""
        try:
            self.page.evaluate("""() => {
                // Look for new chat button
                const btns = document.querySelectorAll('a, button');
                for (const b of btns) {
                    const t = b.textContent.trim().toLowerCase();
                    const label = (b.getAttribute('aria-label') || '').toLowerCase();
                    if (t === 'new chat' || label === 'new chat' ||
                        label.includes('new chat')) {
                        b.click();
                        return true;
                    }
                }
                return false;
            }""")
            self.page.wait_for_timeout(1500)
            self._dismiss_overlays()
            return True
        except Exception:
            # Fallback: navigate to root
            self.page.goto("https://chatgpt.com/", wait_until="domcontentloaded",
                           timeout=15000)
            self.page.wait_for_timeout(2000)
            self._dismiss_overlays()
            return True

    def get_model_info(self):
        """Try to detect which model is active."""
        try:
            return self.page.evaluate("""() => {
                // Look for model selector text
                const els = document.querySelectorAll(
                    '[class*="model"], [data-testid*="model"], button'
                );
                for (const el of els) {
                    const t = el.textContent.trim();
                    if (t.match(/GPT-[34]/i) || t.match(/ChatGPT/i))
                        return t;
                }
                return null;
            }""")
        except Exception:
            return None

    def close(self):
        """Close the browser."""
        try:
            if self.context:
                self.context.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

# ── CLI REPL ─────────────────────────────────────────────────────────────────

HELP_TEXT = f"""
{BOLD}Commands:{RESET}
  {CYAN}/new{RESET}         Start a new conversation
  {CYAN}/model{RESET}       Show current model
  {CYAN}/clear{RESET}       Clear terminal screen
  {CYAN}/history{RESET}     Show conversation history for this session
  {CYAN}/copy{RESET}        Copy last response to clipboard (requires xclip)
  {CYAN}/help{RESET}        Show this help message
  {CYAN}/quit{RESET}        Exit FreeGPT
  {CYAN}Ctrl+C{RESET}       Exit FreeGPT

{DIM}Just type your message and press Enter to send.
For multi-line input, end a line with \\ to continue on the next line.{RESET}
"""

def _save_history(history, path=HISTORY_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(history, f, indent=2)

def _read_multiline():
    """Read input that supports multi-line with trailing backslash."""
    lines = []
    prompt = f"{GREEN}You >{RESET} "
    continuation = f"{DIM}  ...{RESET} "
    while True:
        try:
            line = input(prompt if not lines else continuation)
        except EOFError:
            break
        if line.endswith("\\"):
            lines.append(line[:-1])
        else:
            lines.append(line)
            break
    return "\n".join(lines)

def main():
    parser = argparse.ArgumentParser(
        prog="freegpt",
        description="FreeGPT — Interactive CLI for ChatGPT via the public website",
    )
    parser.add_argument("--visible", action="store_true",
                        help="Show the browser window (useful for first login)")
    parser.add_argument("--reset", action="store_true",
                        help="Clear saved session and re-login")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose debug output")
    args = parser.parse_args()

    # Handle reset
    if args.reset:
        if os.path.exists(PROFILE_DIR):
            shutil.rmtree(PROFILE_DIR)
            print(f"[+] Session cleared.")
        else:
            print(f"[*] No saved session to clear.")

    _banner()

    headless = not args.visible

    session = ChatGPTSession(headless=headless, debug=args.debug)

    # Handle Ctrl+C gracefully
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
        print(f"    Make sure playwright and Firefox are installed:")
        print(f"    pip install playwright playwright-stealth")
        print(f"    playwright install firefox")
        sys.exit(1)
    spinner.stop()

    # Navigate and handle login
    if not session.navigate_to_chat():
        print(f"{RED}[!] Could not establish ChatGPT session. Exiting.{RESET}")
        session.close()
        sys.exit(1)

    # Detect model
    model = session.get_model_info()
    if model:
        print(f"{DIM}  Model: {model}{RESET}")

    print()
    print(f"{DIM}  Type /help for commands. Just type to chat.{RESET}")
    print()

    # Conversation loop
    history = []
    turn = 0

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
                print(f"{GREEN}[+] New conversation started{RESET}\n")
                continue

            elif cmd == "/model":
                model = session.get_model_info()
                print(f"  Model: {model or 'unknown'}\n")
                continue

            elif cmd == "/clear":
                os.system("clear" if os.name == "posix" else "cls")
                _banner()
                continue

            elif cmd == "/history":
                if not history:
                    print(f"  {DIM}(no messages yet){RESET}\n")
                else:
                    for entry in history:
                        role = entry["role"]
                        text = entry["text"]
                        if role == "user":
                            print(f"\n{GREEN}{BOLD}You:{RESET}")
                            _wrap_print(text, prefix="  ")
                        else:
                            print(f"\n{CYAN}{BOLD}ChatGPT:{RESET}")
                            _wrap_print(text, prefix="  ")
                    print()
                continue

            elif cmd == "/copy":
                if history and history[-1]["role"] == "assistant":
                    last = history[-1]["text"]
                    try:
                        import subprocess
                        proc = subprocess.Popen(["xclip", "-selection", "clipboard"],
                                                stdin=subprocess.PIPE)
                        proc.communicate(last.encode("utf-8"))
                        print(f"  {GREEN}Copied to clipboard{RESET}\n")
                    except FileNotFoundError:
                        try:
                            proc = subprocess.Popen(["xsel", "--clipboard", "--input"],
                                                    stdin=subprocess.PIPE)
                            proc.communicate(last.encode("utf-8"))
                            print(f"  {GREEN}Copied to clipboard{RESET}\n")
                        except FileNotFoundError:
                            print(f"  {YELLOW}Install xclip or xsel for clipboard support{RESET}\n")
                else:
                    print(f"  {DIM}(no response to copy){RESET}\n")
                continue

            elif cmd == "/help":
                print(HELP_TEXT)
                continue

            else:
                print(f"  {YELLOW}Unknown command: {cmd}. Type /help for commands.{RESET}\n")
                continue

        # Send message to ChatGPT
        turn += 1
        history.append({"role": "user", "text": user_input, "turn": turn})

        response = session.send_message(user_input)

        if response:
            history.append({"role": "assistant", "text": response, "turn": turn})
            print(f"\n{CYAN}{BOLD}ChatGPT:{RESET}")
            _wrap_print(response, prefix="  ")
            print()
        else:
            print(f"{RED}  [!] No response received. Try again.{RESET}\n")

    # Save history
    if history:
        _save_history(history)
        if args.debug:
            print(f"{DIM}  History saved to {HISTORY_FILE}{RESET}")

    session.close()

if __name__ == "__main__":
    main()
