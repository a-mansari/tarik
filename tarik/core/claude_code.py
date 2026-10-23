"""The Claude Code CLI as Tarik's model (MODEL_ADAPTER "claude-code", the Cabinets page): one `claude -p` per call,
its own tools and settings off, logged in with CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`, Vault
secret/tarik/orchestrator) or ANTHROPIC_API_KEY. The prompt goes in as Anthropic content blocks (stream-json), so
images and PDFs reach the model as they would through the API."""

import json
import os
import subprocess
import tempfile

from tarik.core.cabinets import setting
from tarik.core.config import MODEL_TIMEOUT

MODEL = "sonnet"  # shortcut: one model for the CLI, make it a setting when a cabinet needs another


class ClaudeCodeError(RuntimeError):
    """The CLI failed: not installed, not logged in, out of quota, or no answer."""


def ask(system, content, schema=None):
    """The CLI's answer to one user message ``content`` (Anthropic content blocks) under ``system``: its text, or the
    dict matching the JSON ``schema`` when one is given."""
    token = setting("CLAUDE_CODE_OAUTH_TOKEN", "")
    env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    command = [
        "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
        "--model", MODEL, "--system-prompt", system,
    ]  # fmt: skip
    if schema is not None:
        command += ["--json-schema", json.dumps(schema)]
    message = {"type": "user", "message": {"role": "user", "content": content}}
    with tempfile.TemporaryDirectory() as empty:  # nothing of the server's for it to see
        try:
            done = subprocess.run(
                command, input=json.dumps(message), capture_output=True, text=True, env=env, cwd=empty,
                timeout=MODEL_TIMEOUT,
            )  # fmt: skip
        except FileNotFoundError:
            raise ClaudeCodeError("the claude CLI is not installed in this image") from None
        except subprocess.TimeoutExpired:
            raise ClaudeCodeError(f"no answer from the claude CLI in {MODEL_TIMEOUT} s") from None
    result = result_event(done.stdout)
    if result is None:
        raise ClaudeCodeError(f"claude CLI exited {done.returncode}: {done.stderr[-500:] or done.stdout[-500:]}")
    if result.get("is_error"):
        raise ClaudeCodeError(f"claude CLI: {result.get('result') or result.get('subtype')}")
    if schema is not None:
        if not isinstance(result.get("structured_output"), dict):
            raise ClaudeCodeError(f"claude CLI gave no structured answer: {str(result.get('result'))[:300]}")
        return result["structured_output"]
    return result.get("result") or ""


def result_event(output):
    """The "result" event (dict) among the CLI's stream-json ``output`` lines, None without one."""
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
