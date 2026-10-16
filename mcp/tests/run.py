"""Run the tests that cover what changed, in a throwaway tarik-mcp container on the working copy
(the running services are left alone; Postgres and Vault must be up). From the repository:

  python3 mcp/tests/run.py                tests covering the files changed since the last commit, new files
                                          included
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
    if path == REPO / "pyproject.toml":
        return [STYLE]  # the code's style
    if path == REPO / "n8n/patch.js":
        return list((TESTS / "n8n").glob("test_*.py"))
    return []


def changed(since):
    """The repository's files (absolute Paths) changed since commit ``since``, untracked new ones included."""

    def git(*args):
        """Git's output for ``args``, split on whitespace."""
        return subprocess.run(
            ["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True
        ).stdout.split()

    paths = {*git("diff", "--name-only", since), *git("ls-files", "--others", "--exclude-standard")}
    return {REPO / path for path in paths}


def imported_names(node):
    """The dotted names an import statement ``node`` may import ("a.b" and, from it, "a.b.c"); [] for any other."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    return []


def imported(path):
    """The repository's Python files ``path`` imports at its top (an import inside a function or a fixture
    is the test's own business: a test that runs the code it imports that way imports it at the top too)."""
    found = set()
    for node in ast.parse(path.read_text()).body:
        for name in imported_names(node):
            for base in (MCP, path.parent):  # tarik.x, server, tests.fakes; or a test's neighbour (isolation)
                module = base.joinpath(*name.split("."))
                found |= {
                    candidate
                    for candidate in (module.with_suffix(".py"), module / "__init__.py")
                    if candidate.is_file()
                }
    return found


def covered(test):
    """Every repository file ``test`` runs: its imports, theirs, and the conftest.py files on its path."""
    conftests = [folder / "conftest.py" for folder in test.parents if folder.is_relative_to(TESTS)]
    todo = [test, *(conftest for conftest in conftests if conftest.is_file())]
    seen = set()
    while todo:
        path = todo.pop()
        if path not in seen:
            seen.add(path)
            todo.extend(imported(path))
    return seen


def selected(files):
    """The test files (sorted Paths) covering ``files``: all of them when one is in EVERYTHING; the style test
    whenever a Python file is among them."""
    tests = sorted(TESTS.rglob("test_*.py"))
    if files & EVERYTHING:
        return tests
    files = files | {stand_in for path in files for stand_in in other(path)}
    chosen = {test for test in tests if covered(test) & files}
    if any(path.suffix == ".py" for path in files):
        chosen.add(STYLE)
    return sorted(chosen)


def take_since(args):
    """The commit after --since in ``args`` (both taken out of it), else HEAD."""
    if "--since" not in args:
        return "HEAD"
    at = args.index("--since")
    since = args[at + 1]
    del args[at : at + 2]
    return since


def pytest_command(tests, args):
    """The docker compose command running pytest on ``tests`` with ``args``, in a throwaway tarik-mcp container."""
    # --build: the image the tests run in is the Dockerfile's as it is now (its dependencies), the code mounted over it.
    container = ["docker", "compose", "run", "--rm", "--build", "--no-deps", "-e", "PYTHONDONTWRITEBYTECODE=1"]
    mounts = ["-v", f"{MCP}:/app", "-v", f"{REPO / 'workflows'}:/workflows:ro", "-v", f"{REPO / 'docs'}:/docs:ro"]
    mounts += ["-v", f"{REPO / 'pyproject.toml'}:/pyproject.toml:ro", "tarik-mcp"]
    pytest = ["python", "-m", "pytest", "-p", "no:cacheprovider"]
    return [*container, *mounts, *pytest, *(str(test.relative_to(MCP)) for test in tests), *args]


def main(args):
    """Run the tests ``args`` (the command line's) ask for; pytest's exit code."""
    since = take_since(args)
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
    code = subprocess.run(pytest_command(tests, args), cwd=REPO).returncode
    # 5, no test collected: fine when only the change chose the files (their category was left out, live tests by
    # default); a failure when the tests were named (-k, a path), so a typo never passes.
    named = "-k" in args or any(arg.endswith(".py") or "tests/" in arg for arg in args)
    return 0 if code == 5 and not named else code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
