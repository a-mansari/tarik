"""Run the tests that cover what changed, in a throwaway tarik-mcp container on the working copy
(the running services are left alone; Postgres and Vault must be up). From the repository:

  python3 mcp/tests/run.py                tests covering the files changed since the last commit, new files included
  python3 mcp/tests/run.py --since main   ... changed since main
  python3 mcp/tests/run.py --all          every test
  python3 mcp/tests/run.py -m security    every test of one category: security, functionality, communication,
                                          technical, style, live (with --since: only those covering the change)
  Any other argument goes to pytest (-x, -k name, a test file...).

A test covers a file when it imports it, directly or through what it imports, or through a conftest.py
on its path. The files no import reaches are mapped in OTHER. A change to what every test runs on
(EVERYTHING) runs them all. Live tests (the real n8n and model) run only with -m live."""

import ast
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
MCP = TESTS.parent  # /app in the container
REPO = MCP.parent
EVERYTHING = {MCP / "Dockerfile", MCP / "pytest.ini", MCP / "tests/run.py", REPO / "docker-compose.yml"}
STYLE = TESTS / "style/test_consistency.py"  # lints every Python file, and checks the workflows against their scripts


def other(path):
    """The tests a file no import reaches is covered by, or the file standing in for it."""
    if path == MCP / "tarik/core/cabinet.sql":
        return [MCP / "tarik/core/cabinets.py"]  # the tables cabinets.py creates
    if path.is_relative_to(REPO / "workflows"):
        return [STYLE, TESTS / "n8n/test_conversation.py"]
    if path == REPO / "n8n/patch.js":
        return list((TESTS / "n8n").glob("test_*.py"))
    return []


def changed(since):
    git = lambda *args: subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout.split()
    return {REPO / path for path in {*git("diff", "--name-only", since), *git("ls-files", "--others", "--exclude-standard")}}


def imported(path):
    """The repository's Python files ``path`` imports at its top (an import inside a function or a fixture
    is the test's own business: a test that runs the code it imports that way imports it at the top too)."""
    found = set()
    for node in ast.parse(path.read_text()).body:
        names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else \
            [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)] if isinstance(node, ast.ImportFrom) and node.module else []
        for name in names:
            for base in (MCP, path.parent):  # tarik.x, server, tests.fakes; or a test's neighbour (isolation)
                module = base.joinpath(*name.split("."))
                found |= {candidate for candidate in (module.with_suffix(".py"), module / "__init__.py") if candidate.is_file()}
    return found


def covered(test):
    """Every repository file ``test`` runs: its imports, theirs, and the conftest.py files on its path."""
    todo = [test, *(folder / "conftest.py" for folder in test.parents if folder.is_relative_to(TESTS) and (folder / "conftest.py").is_file())]
    seen = set()
    while todo:
        path = todo.pop()
        if path not in seen:
            seen.add(path)
            todo.extend(imported(path))
    return seen


def selected(files):
    tests = sorted(TESTS.rglob("test_*.py"))
    if files & EVERYTHING:
        return tests
    files = files | {stand_in for path in files for stand_in in other(path)}
    chosen = {test for test in tests if covered(test) & files}
    if any(path.suffix == ".py" for path in files):
        chosen.add(STYLE)
    return sorted(chosen)


def main(args):
    since = "HEAD"
    if "--since" in args:
        at = args.index("--since")
        since = args[at + 1]
        del args[at:at + 2]
    if "--all" in args or ("-m" in args and since == "HEAD"):  # a category: all of its tests
        if "--all" in args:
            args.remove("--all")
        tests = [TESTS]
    else:
        files = changed(since)
        tests = selected(files)
        print(f"{len(files)} file(s) changed since {since}: {len(tests)} test file(s) cover them")
        if not tests:
            return 0
    command = ["docker", "compose", "run", "--rm", "--no-deps", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-v", f"{MCP}:/app", "-v", f"{REPO / 'workflows'}:/workflows:ro", "tarik-mcp",
               "python", "-m", "pytest", "-p", "no:cacheprovider", *(str(test.relative_to(MCP)) for test in tests), *args]
    code = subprocess.run(command, cwd=REPO).returncode
    return 0 if code == 5 else code  # 5: the files chosen hold no test of the category asked (live tests, by default)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
