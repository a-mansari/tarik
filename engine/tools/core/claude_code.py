"""The Claude Code CLI as Tarik's model (MODEL_ADAPTER "claude-code", the Cabinets page): one `claude -p` per call,
its own tools and settings off, logged in with CLAUDE_CODE_OAUTH_TOKEN (`claude setup-token`, Vault
secret/tarik/orchestrator) or ANTHROPIC_API_KEY. The prompt goes in as Anthropic content blocks (stream-json), so
images and PDFs reach the model as they would through the API."""

import json
import os
import subprocess
import tempfile

from engine.tools.core import usage
from engine.tools.core.cabinets import setting
from engine.tools.core.config import MODEL_TIMEOUT

MODEL = "sonnet"  # shortcut: one model for the CLI, make it a setting when a cabinet needs another
EFFORT = "medium"  # set, not left to the CLI's default, which can change between versions


class ClaudeCodeError(RuntimeError):
    """The CLI failed: not installed, not logged in, out of quota, or no answer."""


class ClaudeCodeUnreadable(ClaudeCodeError):
    """The CLI answered, but not in the shape asked: no structured answer, or its retries at one spent."""


def ask(system, content, schema):
    """Return the CLI's answer to one user message ``content`` (Anthropic content blocks) under system prompt
    ``system``: the dict matching JSON ``schema``.
    Raises ClaudeCodeUnreadable when it gives no answer in that shape, ClaudeCodeError when the CLI is missing,
    times out or fails."""
    token = setting("CLAUDE_CODE_OAUTH_TOKEN", "")
    env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    message = {"type": "user", "message": {"role": "user", "content": content}}
    with tempfile.TemporaryDirectory() as empty:  # nothing of the server's for it to see
        prompt = os.path.join(empty, "system.md")  # a file: one argument may not pass 128 KiB, the prompt may
        with open(prompt, "w", encoding="utf-8") as file:
            file.write(system)
        command = [
            "claude", "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
            "--model", MODEL, "--effort", EFFORT, "--system-prompt-file", prompt, "--json-schema", json.dumps(schema),
        ]  # fmt: skip
        try:
            done = subprocess.run(
                command, input=json.dumps(message), capture_output=True, text=True, env=env, cwd=empty,
                timeout=MODEL_TIMEOUT,
            )  # fmt: skip
        except FileNotFoundError:
            raise ClaudeCodeError("the claude CLI is not installed in this image") from None
        except subprocess.TimeoutExpired:
            raise ClaudeCodeError(f"no answer from the claude CLI in {MODEL_TIMEOUT} s") from None
        except OSError as error:  # the schema too long for one argument, among others
            raise ClaudeCodeError(f"the claude CLI could not start: {error}") from None
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
