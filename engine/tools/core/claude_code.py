"""The Claude Code CLI as Tarik's model (MODEL_ADAPTER "claude-code", the Cabinets page): one `claude -p` per call,
its own tools and settings off, logged in with CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`, Vault
secret/tarik/orchestrator) or ANTHROPIC_API_KEY. The prompt goes in as Anthropic content blocks (stream-json), so
images and PDFs reach the model as they would through the API."""

import json
import os
import pathlib
import subprocess
import tempfile
import threading
import time

from engine.tools.core import usage
from engine.tools.core.cabinets import setting
from engine.tools.core.config import MODEL_TIMEOUT

MODEL = "sonnet"  # shortcut: one model for the CLI, make it a setting when a cabinet needs another
EFFORT = "medium"  # set, not left to the CLI's default, which can change between versions


# At most this many CLI processes at once in the server (each holds a few hundred MB), the agent's calls and the
# tools' vision calls together: more wait their turn, within their own timeout.
PROCESSES = threading.BoundedSemaphore(int(os.getenv("TARIK_MODEL_CALLS", "10")))
ARGUMENT = 128 * 1024  # Linux's limit on one argument (MAX_ARG_STRLEN), its final NUL included


# The CLI calls going on in a run tree, as (usage.ROOT, process), and the trees stopped (runs.stop) until they end:
# a stopped tree's processes are killed and it starts no other (a tool retrying, a call waiting for its slot).
RUNNING = set()
KILLED = set()
_LOCK = threading.Lock()


def kill(root):
    """Stop run tree ``root``'s CLI calls: those going on killed at once (their slots freed), any other refused until
    forget(``root``)."""
    with _LOCK:
        KILLED.add(root)
        for tree, process in RUNNING:
            if tree == root:
                process.kill()


def forget(root):
    """Let run tree ``root`` call the CLI again: it has ended (runs.start)."""
    with _LOCK:
        KILLED.discard(root)


class ClaudeCodeError(RuntimeError):
    """The CLI failed: not installed, not logged in, out of quota, busy or timed out."""


class ClaudeCodeStopped(ClaudeCodeError):
    """The call's run was stopped by hand (runs.stop): killed, or never started."""


class ClaudeCodeUnreadable(ClaudeCodeError):
    """No answer in the shape asked: none structured, the CLI's retries at one spent, or the schema too long."""


def ask(system, content, schema, timeout=MODEL_TIMEOUT):
    """Return the CLI's answer to one user message ``content`` (Anthropic content blocks) under system prompt
    ``system``: the dict matching JSON ``schema``, within ``timeout`` seconds (the wait for a free process included).
    Raises ClaudeCodeUnreadable when it gives no answer in that shape, ClaudeCodeError when the CLI is missing,
    busy, times out or fails."""
    token = setting("CLAUDE_CODE_OAUTH_TOKEN", "")
    env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    message = {"type": "user", "message": {"role": "user", "content": content}}
    shape = json.dumps(schema)  # ASCII: its length is its size in bytes
    if len(shape) >= ARGUMENT:  # checked before waiting for a process: the CLI takes the schema inline only
        raise ClaudeCodeUnreadable(f"the answer's schema ({len(shape)} bytes) is too long for the claude CLI")
    started = time.monotonic()
    if not PROCESSES.acquire(timeout=timeout):
        raise ClaudeCodeError(f"no claude CLI process free in {timeout:.0f} s")
    try:
        with tempfile.TemporaryDirectory() as empty:  # nothing of the server's for it to see
            prompt = os.path.join(empty, "system.md")  # a file: one argument may not pass 128 KiB, the prompt may
            command = [
                "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                "--model", MODEL, "--effort", EFFORT, "--system-prompt-file", prompt,
                "--json-schema", shape,
            ]  # fmt: skip
            try:
                pathlib.Path(prompt).write_text(system, encoding="utf-8")
                done = answered(command, json.dumps(message), env, empty, timeout - (time.monotonic() - started))
            except FileNotFoundError:
                raise ClaudeCodeError("the claude CLI is not installed in this image") from None
            except subprocess.TimeoutExpired:
                raise ClaudeCodeError(f"no answer from the claude CLI in {timeout:.0f} s") from None
            except OSError as error:  # the prompt not written, among others
                raise ClaudeCodeError(f"the claude CLI could not start: {error}") from None
    finally:
        PROCESSES.release()
    result = result_event(done.stdout)
    if result is None:
        raise ClaudeCodeError(f"claude CLI exited {done.returncode}: {done.stderr[-500:] or done.stdout[-500:]}")
    usage.cli_usage(result)  # an answer refused is billed too
    if result.get("subtype") == "error_max_structured_output_retries":
        raise ClaudeCodeUnreadable("claude CLI: no answer in the schema's shape after its retries")
    if result.get("is_error"):
        raise ClaudeCodeError(f"claude CLI: {result.get('result') or result.get('subtype')}")
    if not isinstance(result.get("structured_output"), dict):
        raise ClaudeCodeUnreadable(f"claude CLI gave no structured answer: {str(result.get('result'))[:300]}")
    return result["structured_output"]


def answered(command, given, env, cwd, timeout):
    """Run the CLI ``command`` on stdin ``given`` within ``timeout`` seconds (at least one) and return it ended
    (returncode, stdout, stderr), as subprocess.run would; kept in RUNNING meanwhile, under its run tree, so a stop
    kills it (a call outside any run is never stopped). Raises subprocess.TimeoutExpired once it is killed for its
    time, ClaudeCodeStopped when its run tree was stopped."""
    root = usage.ROOT.get()
    with _LOCK:
        if root in KILLED:
            raise ClaudeCodeStopped("claude CLI: its run was stopped")
        process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=cwd
        )  # started under the lock: a stop either sees it or comes before it
        held = (root, process)
        if root is not None:
            RUNNING.add(held)
    try:
        stdout, stderr = process.communicate(given, timeout=max(1, timeout))
    except BaseException:  # its time over, or anything else: never left running
        process.kill()
        process.communicate()
        raise
    finally:
        with _LOCK:
            RUNNING.discard(held)
    if root in KILLED:
        raise ClaudeCodeStopped("claude CLI: its run was stopped")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def result_event(output):
    """Return the "result" event (dict) among the CLI's stream-json ``output`` lines, None without one."""
    for line in output.splitlines():
        event = parsed(line)
        if event.get("type") == "result":
            return event
    return None


def parsed(line):
    try:
        event = json.loads(line)
    except ValueError:
        return {}
    return event if isinstance(event, dict) else {}
