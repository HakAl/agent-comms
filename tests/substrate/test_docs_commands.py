"""Every command the docs show is real, complete, and exercised.

The fenced ``sh`` blocks of README.md, CONTRIBUTING.md, AGENTS.md and every
docs/*.md are read. Each simple command (continuations joined, comments
stripped, unquoted ``;``/``&&``/``||``/``|`` split, redirections and leading
``NAME=VALUE`` assignments dropped, quoted text kept whole) whose program is
one of the four installed console scripts is checked twice:

1. It parses. ``agent-comms`` lines go through the real argparse parser and
   the command module's ``preflight`` (against a scratch admin token, so the
   credential passes and the cross-argument rules are enforced);
   ``agent-comms-monitor`` through ``monitor.parse_args``;
   ``agent-comms-mcp`` against the options its module declares (read from
   the source, since mcp_server.py is contract-governing and builds its
   parser inside ``main``), with argparse's value rules and ``main``'s
   absolute ``--home`` rule; ``agent-comms-seat`` against its documented
   shape ``<actor-id> [--] <runtime argv...>``.
2. Its command path (program plus subcommand chain, ``admin <verb>`` and
   ``custody <runtime>`` included) is exercised: by a call in a test under
   tests/ that runs the entry point (``cli.run([...])`` or an imported
   ``run``/``main``, ``monitor.main([...])``, ``mcp_server.main()``, a
   ``subprocess.run``-style call given an argv holding ``-m
   agent_comms.<module>``, an argv bound to a name earlier in the same
   block, or a test helper that passes its own arguments into one of those;
   all found through the AST, never by substring), or by a line of
   scripts/gates/install-smoke.sh, the release walkthrough CI runs. A string
   that merely mentions a subcommand (an unused list, a comment, a
   parser-only ``parse_args``, a printed or asserted argv) is not evidence.

A program named later in a line (``claude mcp add ... -- .../agent-comms-mcp
--actor-id X``, ``uv run agent-comms ...``, the runtime argv of
``agent-comms-seat``) starts a nested command that is checked the same way;
only file-handling programs (``rm``, ``cd``, ``git``, ...) may name our
programs as plain arguments. ``make <target>`` must be a Makefile target.
``python -m agent_comms.<module>`` must import, define ``main`` and, when
the module has ``build_parser``, parse the rest of the line.

A failure names the document, the line and the command, so the fix is a
test, a walkthrough step or a doc correction, never an exception list here.
"""

from __future__ import annotations

import tests.isolation  # noqa: F401  # scratch-home guard; keep above agent_comms imports

import argparse
import ast
import contextlib
import importlib
import io
import os
import re
import shlex
import shutil
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from agent_comms import cli, monitor, paths
from agent_comms.schema import ValidationError

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "scripts" / "gates" / "install-smoke.sh"
PROGRAMS = ("agent-comms", "agent-comms-mcp", "agent-comms-monitor", "agent-comms-seat")
# Programs that may take one of ours as a plain file argument without that
# being a nested command.
FILE_PROGRAMS = frozenset({"rm", "ls", "cd", "cat", "mkdir", "git", "which", "command", "echo", "ln", "cp", "mv", "chmod", "test", "uv", "pipx"})
# Front words that are not the command.
WRAPPERS = frozenset({"env", "exec", "nohup", "time"})
FENCE_LANGUAGES = frozenset({"sh", "bash"})
# Options that take a value in the argv lists tests build; everything else
# starting with "-" is skipped alone when the subcommand is looked for.
VALUE_OPTIONS = frozenset({"--db", "-m", "--python", "--extra", "--home", "--actor-id"})
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Shell control operators, longest first.
OPERATORS = ("&&", "||", ";", "|", "&", "(", ")")
SEPARATORS = frozenset({"&&", "||", ";", "|", "&"})
# ``<placeholder>`` is an argument the reader fills in, not an input redirection.
PLACEHOLDER = re.compile(r"<[^\s<>]+>")
REDIRECTION_OPERATOR = re.compile(r"&>>?|[<>]{1,2}&?")
# ``uv run`` options this checker understands (flags, and options taking a value).
UV_RUN_FLAGS = frozenset({
    "--frozen", "--locked", "--no-sync", "--isolated", "--no-project", "--offline", "--no-dev", "--all-extras",
    "--no-editable", "--exact", "--active", "--no-active", "--no-cache", "--no-config", "--no-progress",
    "--native-tls", "-q", "--quiet", "-v", "--verbose", "--managed-python", "--no-managed-python",
})
UV_RUN_VALUE_OPTIONS = frozenset({
    "--project", "--directory", "--extra", "--group", "--only-group", "--no-group", "--with", "--with-editable",
    "--with-requirements", "--package", "--python", "-p", "--env-file", "--index", "--default-index",
    "--find-links", "-f", "--cache-dir", "--config-file", "--resolution", "--prerelease", "--exclude-newer",
    "--python-platform", "--python-preference",
})
# How a test runs an entry point: ``<module alias>.<function>([argv])`` and
# ``python -m <module>`` argv literals.
ENTRY_NAMES = {"cli": "agent-comms", "mcp_server": "agent-comms-mcp", "monitor": "agent-comms-monitor", "seat": "agent-comms-seat"}
ENTRY_MODULES = {f"agent_comms.{alias}": program for alias, program in ENTRY_NAMES.items()}
ENTRY_FUNCTIONS = frozenset({"run", "main"})
# Calls that execute an argv list (so a ``-m agent_comms.<module>`` argv in
# their arguments runs); printing or asserting on such a list does not.
EXEC_CALLEES = frozenset({"subprocess.run", "subprocess.Popen", "subprocess.check_output", "subprocess.check_call", "subprocess.call"})


@dataclass
class DocCommand:
    doc: Path
    line: int
    text: str
    program: str
    argv: list[str]


@dataclass
class MakeCommand:
    doc: Path
    line: int
    text: str
    targets: list[str]


@dataclass
class ModuleCommand:
    doc: Path
    line: int
    text: str
    module: str
    argv: list[str]


@dataclass
class Problem:
    doc: Path
    line: int
    text: str
    message: str


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)


@dataclass
class Token:
    text: str
    operator: bool = False


def doc_paths(repo: Path) -> list[Path]:
    return [repo / "README.md", repo / "CONTRIBUTING.md", repo / "AGENTS.md", *sorted((repo / "docs").glob("*.md"))]


def shell_blocks(doc: Path):
    """Yield ``(first_line_number, lines)`` for every fenced sh/bash block."""
    inside = False
    start = 0
    lines: list[str] = []
    for number, raw in enumerate(doc.read_text().splitlines(), 1):
        stripped = raw.strip()
        if not inside:
            if stripped.startswith("```"):
                language = stripped[3:].strip().split(" ")[0]
                if language in FENCE_LANGUAGES:
                    inside, start, lines = True, number + 1, []
            continue
        if stripped.startswith("```"):
            yield start, lines
            inside = False
            continue
        lines.append(raw)
    if inside:
        raise AssertionError(f"{doc}: unterminated fence opened at line {start - 1}")


def logical_lines(first_line: int, lines: list[str]):
    """Join backslash continuations; yield ``(line_number, text)``."""
    buffer: list[str] = []
    buffer_start = 0
    for offset, raw in enumerate(lines):
        text = raw.strip()
        if not buffer:
            buffer_start = first_line + offset
        if text.endswith("\\"):
            buffer.append(text[:-1].strip())
            continue
        buffer.append(text)
        yield buffer_start, " ".join(part for part in buffer if part)
        buffer = []
    if buffer:
        yield buffer_start, " ".join(part for part in buffer if part)


def matching_paren(text: str, start: int) -> int:
    """Index of the ``)`` closing the ``(`` at ``start``, skipping quoted text."""
    depth = 0
    quote: str | None = None
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    raise ValueError("No closing parenthesis")


def tokenize(text: str) -> list[Token]:
    """Split one logical line into words and unquoted control operators.

    Quoted text stays inside its word, so a subject ``"a > b"`` or ``"|"`` is
    an argument; ``$(...)`` travels with its word; a ``<placeholder>`` is a
    word; an unquoted redirection and its target (``>file``, ``2>&1``,
    ``< in``) are dropped; ``#`` starts a comment.
    """
    tokens: list[Token] = []
    word: list[str] = []
    in_word = False
    quoted = False
    drop_word = False  # the word being read is a redirection target

    def flush() -> None:
        nonlocal word, in_word, quoted, drop_word
        if in_word:
            if not drop_word:
                tokens.append(Token("".join(word)))
            word, in_word, quoted, drop_word = [], False, False, False

    index = 0
    while index < len(text):
        char = text[index]
        if char in " \t":
            flush()
            index += 1
        elif char == "#" and not in_word:
            break
        elif char == "'":
            end = text.find("'", index + 1)
            if end < 0:
                raise ValueError("No closing quotation")
            word.append(text[index + 1:end])
            in_word = quoted = True
            index = end + 1
        elif char == '"':
            index += 1
            in_word = quoted = True
            while True:
                if index >= len(text):
                    raise ValueError("No closing quotation")
                char = text[index]
                if char == '"':
                    index += 1
                    break
                if char == "\\" and index + 1 < len(text):
                    word.append(text[index + 1])
                    index += 2
                elif text.startswith("$(", index):
                    end = matching_paren(text, index + 1)
                    word.append(text[index:end + 1])
                    index = end + 1
                else:
                    word.append(char)
                    index += 1
        elif char == "\\" and index + 1 < len(text):
            word.append(text[index + 1])
            in_word = True
            index += 2
        elif text.startswith("$(", index):
            end = matching_paren(text, index + 1)
            word.append(text[index:end + 1])
            in_word = True
            index = end + 1
        elif char == "<" and (placeholder := PLACEHOLDER.match(text, index)):
            word.append(placeholder.group(0))
            in_word = True
            index = placeholder.end()
        elif char in "<>" or text.startswith("&>", index):
            if in_word and not quoted and "".join(word).isdigit():
                word, in_word = [], False  # the descriptor of ``2>&1``
            else:
                flush()
            operator = REDIRECTION_OPERATOR.match(text, index).group(0)
            index += len(operator)
            if operator.endswith("&") and index < len(text) and text[index].isdigit():
                while index < len(text) and text[index].isdigit():
                    index += 1
            else:
                drop_word = True
        elif char in ";&|()":
            flush()
            operator = next(op for op in OPERATORS if text.startswith(op, index))
            tokens.append(Token(operator, operator=True))
            index += len(operator)
        else:
            word.append(char)
            in_word = True
            index += 1
    flush()
    return tokens


def simple_commands(tokens: list[Token]) -> list[list[str]]:
    commands: list[list[str]] = [[]]
    for token in tokens:
        if token.operator:
            if token.text in SEPARATORS:
                commands.append([])
            continue
        commands[-1].append(token.text)
    return [command for command in commands if command]


def strip_front(tokens: list[str]) -> list[str]:
    while tokens and (ASSIGNMENT.match(tokens[0]) or tokens[0] in WRAPPERS):
        tokens = tokens[1:]
    return tokens


def program_name(token: str) -> str:
    match = re.fullmatch(r"\$\(command -v (\S+)\)", token)
    if match:
        token = match.group(1)
    return token.rsplit("/", 1)[-1]


def seat_runtime(argv: list[str]) -> list[str]:
    """The runtime argv ``agent-comms-seat <actor-id> [--] ...`` execs."""
    runtime = argv[1:]
    return runtime[1:] if runtime[:1] == ["--"] else runtime


def unwrap_uv_run(tokens: list[str]) -> list[str] | str:
    """The command ``uv run [options] [--] <command ...>`` runs, or a message
    naming an option this checker does not know (so the line is reported
    rather than silently skipped)."""
    index = 2
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        name = token.split("=", 1)[0]
        if name in UV_RUN_VALUE_OPTIONS:
            index += 1 if "=" in token else 2
        elif name in UV_RUN_FLAGS:
            index += 1
        else:
            return f"uv run option {name} is not in the checker's option tables (UV_RUN_FLAGS, UV_RUN_VALUE_OPTIONS)"
    return tokens[index:]


def extract(doc: Path, line: int, text: str, tokens: list[str]):
    """Yield the commands to check in one simple command, or nothing."""
    tokens = strip_front(tokens)
    if not tokens:
        return
    program = program_name(tokens[0])
    if program == "uv" and tokens[1:2] == ["run"]:
        wrapped = unwrap_uv_run(tokens)
        if isinstance(wrapped, str):
            yield Problem(doc, line, text, wrapped)
        else:
            yield from extract(doc, line, text, wrapped)
        return
    if program in PROGRAMS:
        yield DocCommand(doc, line, text, program, tokens[1:])
        if program == "agent-comms-seat":
            yield from extract(doc, line, text, seat_runtime(tokens[1:]))
        return
    if program == "make":
        yield MakeCommand(doc, line, text, [t for t in tokens[1:] if not t.startswith("-") and "=" not in t])
        return
    if "-m" in tokens[1:]:
        index = tokens.index("-m", 1)
        if index + 1 < len(tokens) and tokens[index + 1].startswith("agent_comms"):
            yield ModuleCommand(doc, line, text, tokens[index + 1], tokens[index + 2:])
            return
    for index, token in enumerate(tokens[1:], 1):
        if program_name(token) not in PROGRAMS:
            continue
        pathlike = "/" in token or token.startswith("$(")
        if not pathlike and tokens[index - 1] != "--":
            continue  # a bare word, such as the server name in ``claude mcp add agent-comms``
        if program in FILE_PROGRAMS:
            return
        yield from extract(doc, line, text, tokens[index:])
        return


def documented_commands(docs: list[Path]):
    for doc in docs:
        for first_line, lines in shell_blocks(doc):
            for line, text in logical_lines(first_line, lines):
                try:
                    tokens = tokenize(text)
                except ValueError as exc:
                    raise AssertionError(f"{doc}:{line}: cannot tokenize {text!r}: {exc}") from exc
                for command in simple_commands(tokens):
                    yield from extract(doc, line, text, command)


# --- parsing the real surfaces -------------------------------------------------


class CommandError(Exception):
    pass


def parse_or_raise(parser: argparse.ArgumentParser, argv: list[str]) -> argparse.Namespace:
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            return parser.parse_args(argv)
    except SystemExit as exc:
        message = stderr.getvalue().strip().splitlines()
        raise CommandError(message[-1] if message else f"exit {exc.code}") from None


def subcommand_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def nested_choices(subparser: argparse.ArgumentParser) -> list[str] | None:
    """The second-level names of a subcommand: a nested subparser or a
    positional with choices (``admin <verb>``, ``custody <runtime>``)."""
    nested = subcommand_choices(subparser)
    if nested:
        return sorted(nested)
    for action in subparser._actions:
        if not action.option_strings and action.choices and action.dest != "help":
            return sorted(action.choices)
    return None


# Preflights that check only the credential and the argument combination, so
# they can run on a documented command line. ``import-mailbox``'s preflight
# plans the import against the named source file and is left to its tests.
ARGUMENT_PREFLIGHTS = frozenset({"admin", "rerender-spawn"})

ROOT_PARSER = cli.build_parser()
TOP_LEVEL = subcommand_choices(ROOT_PARSER)
NESTED = {name: choices for name, sub in TOP_LEVEL.items() if (choices := nested_choices(sub))}


def command_path(program: str, tokens: list[str | None]) -> str | None:
    """The command path an argv exercises, or None when it has no subcommand."""
    if program != "agent-comms":
        return program
    skip = False
    path: list[str] = []
    for token in tokens:
        if skip:
            skip = False
            continue
        if token is None:
            continue
        if token.startswith("-"):
            skip = token.split("=", 1)[0] in VALUE_OPTIONS and "=" not in token
            continue
        if not path:
            if token in TOP_LEVEL:
                path.append(token)
                if token not in NESTED:
                    break
            continue
        if token in NESTED[path[0]]:
            path.append(token)
        break
    if not path or (path[0] in NESTED and len(path) == 1):
        return None
    return " ".join([program, *path])


def check_agent_comms(argv: list[str]) -> str:
    args = parse_or_raise(ROOT_PARSER, argv)
    path = command_path("agent-comms", argv)
    if path is None:
        raise CommandError("no complete subcommand")
    module = cli.COMMANDS[args.command]
    preflight = getattr(module, "preflight", None)
    if preflight is not None and args.command in ARGUMENT_PREFLIGHTS:
        try:
            preflight(args)
        except ValidationError as exc:
            raise CommandError(str(exc)) from None
    return path


MCP_SOURCE = (REPO / "agent_comms" / "mcp_server.py").read_text()
# ``main`` rejects a relative ``--home`` after parsing; the adapter mirrors
# that rule for as long as the source has it.
MCP_HOME_ABSOLUTE = "os.path.isabs(args.home)" in MCP_SOURCE


def mcp_options() -> tuple[dict[str, bool], set[str]]:
    """``{option: takes_value}`` and the required options of agent-comms-mcp,
    read from the source so the contract-governing module is not changed."""
    tree = ast.parse(MCP_SOURCE)
    options: dict[str, bool] = {}
    required: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
            continue
        names = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("-")]
        if not names:
            continue
        keywords = {k.arg: k.value for k in node.keywords}
        action = keywords.get("action")
        takes_value = not (isinstance(action, ast.Constant) and action.value in {"store_true", "store_false", "count"})
        for name in names:
            options[name] = takes_value
        flag = keywords.get("required")
        if isinstance(flag, ast.Constant) and flag.value is True:
            required.update(names)
    options.setdefault("-h", False)
    options.setdefault("--help", False)
    return options, required


MCP_OPTIONS, MCP_REQUIRED = mcp_options()


def check_mcp(argv: list[str]) -> str:
    seen: set[str] = set()
    index = 0
    while index < len(argv):
        token = argv[index]
        if not token.startswith("-"):
            raise CommandError(f"agent-comms-mcp takes no positional argument: {token!r}")
        name, inline, value = token.partition("=")
        if name not in MCP_OPTIONS:
            raise CommandError(f"unrecognized argument: {name}")
        seen.add(name)
        if MCP_OPTIONS[name]:
            if not inline:
                index += 1
                if index >= len(argv) or argv[index].startswith("-"):
                    raise CommandError(f"argument {name}: expected one argument")
                value = argv[index]
            if name == "--home" and MCP_HOME_ABSOLUTE and not os.path.isabs(value):
                raise CommandError(f"--home {value!r} is not an absolute path")
        elif inline:
            raise CommandError(f"argument {name}: ignored explicit argument {value!r}")
        index += 1
    missing = MCP_REQUIRED - seen
    if missing:
        raise CommandError(f"the following arguments are required: {', '.join(sorted(missing))}")
    return "agent-comms-mcp"


def check_seat(argv: list[str]) -> str:
    if not argv or argv[0].startswith("-") or not seat_runtime(argv):
        raise CommandError("usage: agent-comms-seat <actor-id> [--] <runtime argv...>")
    return "agent-comms-seat"


def check_monitor(argv: list[str]) -> str:
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            monitor.parse_args(argv)
    except SystemExit as exc:
        message = stderr.getvalue().strip().splitlines()
        raise CommandError(message[-1] if message else f"exit {exc.code}") from None
    return "agent-comms-monitor"


CHECKERS = {
    "agent-comms": check_agent_comms,
    "agent-comms-mcp": check_mcp,
    "agent-comms-monitor": check_monitor,
    "agent-comms-seat": check_seat,
}


def make_targets(repo: Path) -> set[str]:
    targets = set()
    for line in (repo / "Makefile").read_text().splitlines():
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", line)
        if match:
            targets.add(match.group(1))
    return targets


def check_module(command: ModuleCommand) -> None:
    try:
        module = importlib.import_module(command.module)
    except ImportError as exc:
        raise CommandError(f"{command.module} does not import: {exc}") from None
    if not callable(getattr(module, "main", None)):
        raise CommandError(f"{command.module} has no main()")
    build_parser = getattr(module, "build_parser", None)
    if callable(build_parser):
        parse_or_raise(build_parser(), command.argv)


# --- what the tests and the walkthrough exercise --------------------------------

SPLICE = object()  # where a helper splices its own ``*args`` into an argv literal
Bindings = dict[str, list]
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def constant_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def literal_argv(node: ast.AST, bindings: Bindings, splice: str | None = None) -> list | None:
    """The argv a test passes: a list/tuple literal (strings, None for
    anything else), a name bound to one at this point, ``list(<name>)``, or
    the helper's own ``*<splice>`` (as SPLICE); None when it is none of those."""
    if isinstance(node, ast.Name):
        if node.id == splice:
            return [SPLICE]
        return bindings.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "list" and len(node.args) == 1:
        return literal_argv(node.args[0], bindings, splice)
    if not isinstance(node, (ast.List, ast.Tuple)):
        return None
    argv: list = []
    for elt in node.elts:
        if isinstance(elt, ast.Starred):
            argv.extend(literal_argv(elt.value, bindings, splice) or [None])
        else:
            argv.append(constant_string(elt))
    return argv


def entry_programs(tree: ast.AST) -> dict[str, str]:
    """Names a test calls an entry point by: ``cli.run`` / ``monitor.main``
    through the module alias, ``run`` through ``from agent_comms.cli import
    run [as ...]``; ``{"cli.run": "agent-comms", "cli_run": "agent-comms"}``."""
    names = {f"{alias}.{function}": program for alias, program in ENTRY_NAMES.items() for function in ENTRY_FUNCTIONS}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ENTRY_MODULES and alias.asname:
                    names.update({f"{alias.asname}.{f}": ENTRY_MODULES[alias.name] for f in ENTRY_FUNCTIONS})
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                dotted = f"{node.module}.{alias.name}"
                if dotted in ENTRY_MODULES:
                    names.update({f"{alias.asname or alias.name}.{f}": ENTRY_MODULES[dotted] for f in ENTRY_FUNCTIONS})
                elif node.module in ENTRY_MODULES and alias.name in ENTRY_FUNCTIONS:
                    names[alias.asname or alias.name] = ENTRY_MODULES[node.module]
    return names


def callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if isinstance(func.value, ast.Name):
            return f"{func.value.id}.{func.attr}"
        return func.attr
    return ""


def module_argv(argv: list | None) -> tuple[str, list] | None:
    """A ``python -m agent_comms.<module> ...`` argv literal."""
    if not argv:
        return None
    for index, element in enumerate(argv[:-1]):
        if element == "-m" and argv[index + 1] in ENTRY_MODULES:
            return ENTRY_MODULES[argv[index + 1]], argv[index + 2:]
    return None


def execution(call: ast.Call, programs: dict[str, str], bindings: Bindings, splice: str | None = None) -> tuple[str, list] | None:
    """A call that runs one of our entry points: an entry function with its
    argv (empty when the argv cannot be read, which still runs the program
    but names no subcommand), or an executing call given a module argv."""
    name = callee_name(call)
    program = programs.get(name)
    if program is not None:
        argv = literal_argv(call.args[0], bindings, splice) if call.args else []
        return program, argv or []
    if name in EXEC_CALLEES:
        for arg in call.args:
            hit = module_argv(literal_argv(arg, bindings, splice))
            if hit:
                return hit
    return None


def expression_calls(node: ast.AST):
    """Calls in an expression subtree, not descending into nested scopes."""
    if isinstance(node, SCOPE_NODES):
        return
    if isinstance(node, ast.Call):
        yield node
    for child in ast.iter_child_nodes(node):
        yield from expression_calls(child)


def target_names(node: ast.AST | None) -> set[str]:
    """Names a target expression binds (``a``, ``a, b``, ``[a, *b]``)."""
    if node is None:
        return set()
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}


def header_names(node: ast.AST) -> set[str]:
    """Names a compound statement binds before its body runs: a loop target,
    ``with ... as name``, ``except ... as name``, a walrus in its header."""
    found: set[str] = set()
    if isinstance(node, (ast.For, ast.AsyncFor)):
        found |= target_names(node.target)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            found |= target_names(item.optional_vars)
    elif isinstance(node, ast.ExceptHandler) and node.name:
        found.add(node.name)
    for _, value in ast.iter_fields(node):
        if isinstance(value, ast.expr):
            found |= {w.target.id for w in ast.walk(value) if isinstance(w, ast.NamedExpr) and isinstance(w.target, ast.Name)}
    return found


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def declared_names(node: ast.AST) -> set[str]:
    """Names a statement or pattern binds by declaration: a ``def`` or
    ``class`` name, an import's bound name (``import a.b`` binds ``a``,
    ``from m import x as y`` binds ``y``), a ``match`` capture (``case x``,
    ``case [*rest]``, ``case {**rest}``)."""
    if isinstance(node, (*FUNCTIONS, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Import):
        return {alias.asname or alias.name.partition(".")[0] for alias in node.names}
    if isinstance(node, ast.ImportFrom):
        return {alias.asname or alias.name for alias in node.names if alias.name != "*"}
    if isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
        return {node.name}
    if isinstance(node, ast.MatchMapping) and node.rest:
        return {node.rest}
    return set()


def assigned_names(nodes: list[ast.AST]) -> set[str]:
    """Every name a block of statements may bind, however it does it:
    assignment targets of any form, walrus targets, loop/with/except headers,
    and declarations (def, class, import, match capture)."""
    found: set[str] = set()
    for node in nodes:
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign):
                for target in inner.targets:
                    found |= target_names(target)
            elif isinstance(inner, (ast.AugAssign, ast.AnnAssign)):
                found |= target_names(inner.target)
            elif isinstance(inner, ast.NamedExpr):
                found |= target_names(inner.target)
            else:
                found |= header_names(inner) | declared_names(inner)
    return found


def block_calls(nodes: list[ast.AST], bindings: Bindings):
    """``(call, bindings)`` for the calls in a block of statements, in order,
    with the bindings as they stand at each call: a name assigned a literal
    argv is bound from then on; any other assignment unbinds it; a name a
    loop, ``with`` or ``except`` header binds is unbound for its body; a name
    bound anywhere in a nested block (if/for/with/try) is unbound after it,
    since which branch ran is not known. Nested scopes are skipped."""
    for node in nodes:
        if isinstance(node, SCOPE_NODES):
            continue
        blocks: list[list[ast.AST]] = []
        for _, value in ast.iter_fields(node):
            if isinstance(value, list) and value and isinstance(value[0], (ast.stmt, ast.ExceptHandler, ast.match_case)):
                blocks.append(value)
            elif isinstance(value, ast.AST):
                for call in expression_calls(value):
                    yield call, dict(bindings)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        for call in expression_calls(item):
                            yield call, dict(bindings)
        if isinstance(node, ast.Assign):
            argv = literal_argv(node.value, bindings) if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) else None
            for target in node.targets:
                for name in target_names(target):
                    bindings.pop(name, None)
            if argv is not None:
                bindings[node.targets[0].id] = argv
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            for name in target_names(node.target):
                bindings.pop(name, None)
        for name in header_names(node):
            bindings.pop(name, None)
        for block in blocks:
            yield from block_calls(block, dict(bindings))
        for name in assigned_names([n for block in blocks for n in block]):
            bindings.pop(name, None)


def declared_functions(tree: ast.Module):
    """``(class name or None, function)`` for the functions a call can name
    from anywhere in the module: module-level functions and the methods a
    module-level class declares directly. Nothing nested is a helper."""
    for node in tree.body:
        if isinstance(node, FUNCTIONS):
            yield None, node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, FUNCTIONS):
                    yield node.name, item


def function_scopes(nodes: list[ast.AST], class_name: str | None = None, local: frozenset[str] = frozenset()):
    """``(class name or None, local names, function)`` for every function in
    ``nodes`` at any depth: the class whose body (or method) it sits in, so
    ``self.<helper>`` resolves within it, and every name the function and its
    enclosing functions bind locally (parameters and everything
    ``assigned_names`` collects: assignments, headers, local defs, classes,
    imports, match captures), any of which shadows a module-level helper of
    that name."""
    for node in nodes:
        if isinstance(node, ast.ClassDef):
            yield from function_scopes(node.body, node.name, local)
        elif isinstance(node, FUNCTIONS):
            arguments = node.args
            parameters = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]
            inner = local | {a.arg for a in parameters if a is not None} | assigned_names(node.body)
            yield class_name, inner, node
            yield from function_scopes(node.body, class_name, inner)
        elif isinstance(node, (ast.stmt, ast.ExceptHandler, ast.match_case)):
            for _, value in ast.iter_fields(node):
                if isinstance(value, list) and value and isinstance(value[0], (ast.stmt, ast.ExceptHandler, ast.match_case)):
                    yield from function_scopes(value, class_name, local)


def helper_key(class_name: str | None, local: frozenset[str], callee: str) -> tuple[str | None, str] | None:
    """How a call names a helper: ``self.<method>`` inside a class, or a bare
    ``<function>`` that no local binding shadows; anything else is none."""
    if callee.startswith("self.") and class_name is not None:
        return class_name, callee[len("self."):]
    if "." not in callee and callee not in local:
        return None, callee
    return None


def helper_templates(tree: ast.Module, programs: dict[str, str], module_bindings: Bindings) -> dict[tuple[str | None, str], tuple[str, list]]:
    """Helpers that pass their ``*args`` into an execution, keyed by their
    declaring class (None for a module-level function) and name:
    ``{("SetupTest", "run_cli"): (program, argv template with SPLICE), (None, "_cli"): ...}``."""
    templates: dict[tuple[str | None, str], tuple[str, list]] = {}
    for class_name, function in declared_functions(tree):
        if function.args.vararg is None:
            continue
        for call, bindings in block_calls(function.body, dict(module_bindings)):
            hit = execution(call, programs, bindings, splice=function.args.vararg.arg)
            if hit and SPLICE in hit[1]:
                templates[class_name, function.name] = hit
    return templates


def invocations(tree: ast.Module):
    """``(program, argv)`` for every place a test runs one of our entry points."""
    programs = entry_programs(tree)
    module_bindings: Bindings = {}
    scopes = [(None, frozenset(), list(block_calls(tree.body, module_bindings)))]
    templates = helper_templates(tree, programs, module_bindings)
    for class_name, local, function in function_scopes(tree.body):
        scopes.append((class_name, local, list(block_calls(function.body, dict(module_bindings)))))
    for class_name, local, calls in scopes:
        for call, bindings in calls:
            hit = execution(call, programs, bindings)
            if hit:
                yield hit
            key = helper_key(class_name, local, callee_name(call))
            template = templates.get(key) if key else None
            if template:
                program, argv = template
                own = [constant_string(arg) for arg in call.args]
                yield program, [part for element in argv for part in (own if element is SPLICE else [element])]


def exercised_paths(sources: list[Path], smoke: Path | None) -> set[str]:
    found: set[str] = set()
    if smoke is not None:
        for line in smoke.read_text().splitlines():
            try:
                tokens = shlex.split(line, comments=True)
            except ValueError:
                continue
            for index, token in enumerate(tokens):
                name = program_name(token)
                if name in PROGRAMS:
                    path = command_path(name, tokens[index + 1:])
                    if path:
                        found.add(path)
    for source in sources:
        for program, argv in invocations(ast.parse(source.read_text())):
            path = command_path(program, argv)
            if path:
                found.add(path)
    return found


def test_sources(repo: Path) -> list[Path]:
    return [source for source in sorted((repo / "tests").rglob("*.py")) if source.name != Path(__file__).name]


# --- the check ----------------------------------------------------------------


def check_docs(docs: list[Path], *, exercised: set[str], targets: set[str]) -> Report:
    report = Report()
    for command in documented_commands(docs):
        doc = command.doc.relative_to(REPO) if command.doc.is_relative_to(REPO) else command.doc.name
        where = f"{doc}:{command.line}"
        if isinstance(command, Problem):
            report.problems.append(f"{where}: `{command.text}`: {command.message}")
            continue
        if isinstance(command, MakeCommand):
            for target in command.targets:
                if target not in targets:
                    report.problems.append(f"{where}: `{command.text}`: make has no target {target!r}")
            report.checked.append(f"{where}: make {' '.join(command.targets)}")
            continue
        if isinstance(command, ModuleCommand):
            try:
                check_module(command)
            except CommandError as exc:
                report.problems.append(f"{where}: `{command.text}`: {exc}")
            report.checked.append(f"{where}: python -m {command.module}")
            continue
        try:
            path = CHECKERS[command.program](command.argv)
        except CommandError as exc:
            report.problems.append(f"{where}: `{command.text}`: {exc}")
            continue
        report.checked.append(f"{where}: {path}")
        if path not in exercised:
            report.problems.append(
                f"{where}: `{command.text}`: {path} is not exercised by a test under tests/ "
                "or by scripts/gates/install-smoke.sh; add one (or correct the doc)"
            )
    return report


class DocsCommandsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # Preflights verify the operator credential before their argument
        # rules; a scratch token (isolation already moved HOME) lets them run.
        root = paths.runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        cls.token_path = root / "admin-token"
        cls.token_path.write_text("docs-test-token\n")
        cls.token_path.chmod(0o600)
        cls.previous_token = os.environ.get("AGENT_COMMS_ADMIN_TOKEN")
        os.environ["AGENT_COMMS_ADMIN_TOKEN"] = "docs-test-token"
        cls.scratch = Path.home() / "docs-test"
        cls.scratch.mkdir(parents=True, exist_ok=True)
        cls.exercised = exercised_paths(test_sources(REPO), SMOKE)
        cls.targets = make_targets(REPO)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.previous_token is None:
            os.environ.pop("AGENT_COMMS_ADMIN_TOKEN", None)
        else:
            os.environ["AGENT_COMMS_ADMIN_TOKEN"] = cls.previous_token
        cls.token_path.unlink(missing_ok=True)
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def check(self, body: str, *, exercised: set[str] | None = None) -> Report:
        doc = self.scratch / f"{self._testMethodName}.md"
        doc.write_text("# Doc\n\n```sh\n" + body + "\n```\n")
        return check_docs(
            [doc],
            exercised=self.exercised if exercised is None else exercised,
            targets=self.targets,
        )

    def index(self, source: str) -> set[str]:
        """What a scratch test file is credited with exercising."""
        path = self.scratch / f"{self._testMethodName}.py"
        path.write_text(source)
        return exercised_paths([path], None)

    def test_every_documented_command_is_real_and_exercised(self) -> None:
        report = check_docs(doc_paths(REPO), exercised=self.exercised, targets=self.targets)
        self.assertTrue(report.checked, "no commands found in the docs' sh blocks")
        self.assertEqual(report.problems, [], "\n" + "\n".join(report.problems))

    def test_the_scan_covers_every_console_script(self) -> None:
        report = check_docs(doc_paths(REPO), exercised=self.exercised, targets=self.targets)
        seen = " ".join(report.checked)
        for program in PROGRAMS:
            self.assertIn(f": {program}", seen, f"{program} is not shown in any doc")

    def test_an_unknown_flag_is_reported_with_path_and_line(self) -> None:
        report = self.check("agent-comms doctor --no-such-flag")
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertRegex(
            report.problems[0],
            r"\.md:4: `agent-comms doctor --no-such-flag`: agent-comms: error: unrecognized arguments: --no-such-flag",
        )

    def test_a_missing_required_argument_is_reported(self) -> None:
        report = self.check("agent-comms bind-seat team-a-architect")
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("--client", report.problems[0])

    def test_preflight_rules_are_enforced(self) -> None:
        report = self.check("agent-comms admin settle-dispatch --from-actor-id a --dispatch-id d --dry-run")
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("--dry-run requires", report.problems[0])

    def test_an_unexercised_command_is_reported(self) -> None:
        report = self.check("agent-comms doctor", exercised=set())
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("agent-comms doctor is not exercised", report.problems[0])

    def test_nested_paths_are_distinguished(self) -> None:
        report = self.check(
            "agent-comms admin retry-spawn --dispatch-id d",
            exercised={"agent-comms admin dispatch"},
        )
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("agent-comms admin retry-spawn is not exercised", report.problems[0])

    def test_env_prefix_path_prefix_and_continuations_are_not_skipped(self) -> None:
        report = self.check(
            "AGENT_COMMS_HOME=/srv/agent-comms-home /opt/agent-comms/bin/agent-comms doctor \\\n    --bogus"
        )
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("--bogus", report.problems[0])

    def test_a_nested_mcp_command_is_checked(self) -> None:
        report = self.check(
            "claude mcp add agent-comms --scope local -- /abs/.venv/bin/agent-comms-mcp --actor-id a --bogus"
        )
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("--bogus", report.problems[0])
        report = self.check("claude mcp add agent-comms --scope local -- /abs/.venv/bin/agent-comms-mcp --db x")
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("--actor-id", report.problems[0])

    def test_mcp_value_rules_match_argparse_and_main(self) -> None:
        report = self.check("agent-comms-mcp --actor-id a --help=oops", exercised={"agent-comms-mcp"})
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("ignored explicit argument", report.problems[0])
        report = self.check("agent-comms-mcp --actor-id a --home relative", exercised={"agent-comms-mcp"})
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("not an absolute path", report.problems[0])
        report = self.check("agent-comms-mcp --actor-id=a --home /abs", exercised={"agent-comms-mcp"})
        self.assertEqual(report.problems, [], report.problems)

    def test_the_seat_runtime_argv_and_uv_run_are_checked(self) -> None:
        report = self.check(
            "agent-comms-seat a -- /opt/bin/agent-comms-mcp --actor-id a --bogus",
            exercised={"agent-comms-seat", "agent-comms-mcp"},
        )
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("--bogus", report.problems[0])
        report = self.check("uv run agent-comms doctor --bogus")
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("--bogus", report.problems[0])
        report = self.check(
            "agent-comms-seat a -- claude\nuv run --frozen --extra test python -m unittest\n"
            "uv run echo agent-comms doctor --bogus",
            exercised={"agent-comms-seat"},
        )
        self.assertEqual(report.problems, [], report.problems)
        self.assertEqual([c.split(": ", 1)[1] for c in report.checked], ["agent-comms-seat"])
        report = self.check(
            "uv run python -m agent_comms.no_such_module\nuv run --project x make no-such-target\n"
            "uv run --mystery agent-comms doctor"
        )
        self.assertEqual(len(report.problems), 3, report.problems)
        self.assertIn("does not import", report.problems[0])
        self.assertIn("no target 'no-such-target'", report.problems[1])
        self.assertIn("--mystery is not in the checker's option tables", report.problems[2])

    def test_monitor_cross_argument_rules_are_checked(self) -> None:
        report = self.check("agent-comms-monitor --check-heartbeat")
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("requires --human-actor-id", report.problems[0])

    def test_seat_shape_is_checked(self) -> None:
        report = self.check("agent-comms-seat team-a-architect")
        self.assertEqual(len(report.problems), 1, report.problems)
        self.assertIn("usage: agent-comms-seat", report.problems[0])

    def test_make_targets_and_module_lines_are_checked(self) -> None:
        report = self.check("make no-such-target\npython -m agent_comms.no_such_module")
        self.assertEqual(len(report.problems), 2, report.problems)
        self.assertIn("no target 'no-such-target'", report.problems[0])
        self.assertIn("does not import", report.problems[1])

    def test_placeholders_are_arguments_and_redirections_are_not(self) -> None:
        report = self.check(
            "agent-comms inbox <actor-id> >inbox.txt 2>&1\nagent-comms doctor <input.json\n"
            "agent-comms doctor > \"out dir/doctor.json\" && agent-comms version"
        )
        self.assertEqual(report.problems, [], report.problems)
        self.assertEqual(
            [c.split(": ", 1)[1] for c in report.checked],
            ["agent-comms inbox", "agent-comms doctor", "agent-comms doctor", "agent-comms version"],
        )

    def test_quoted_operators_stay_inside_their_argument(self) -> None:
        report = self.check(
            'agent-comms admin send --from-actor-id a --to b --subject "a > b" --body x\n'
            "agent-comms admin send --from-actor-id a --to b --subject '|' --body \"$(cat \\\"$f\\\")\"",
            exercised={"agent-comms admin send"},
        )
        self.assertEqual(report.problems, [], report.problems)
        self.assertEqual(len(report.checked), 2, report.checked)
        self.assertEqual(
            [t.text for t in tokenize('a "b > c" d>out 2>&1 | e')],
            ["a", "b > c", "d", "|", "e"],
        )

    def test_file_arguments_and_other_programs_are_skipped(self) -> None:
        report = self.check(
            "rm -rf agent-comms wheels\ngit clone https://example.invalid/agent-comms.git\n"
            'export AGENT_COMMS_ADMIN_TOKEN="$(cat "$root/admin-token")"\nuv sync'
        )
        self.assertEqual(report.problems, [])
        self.assertEqual(report.checked, [])

    def test_exercise_evidence_is_an_entry_point_call(self) -> None:
        credited = self.index(
            "import subprocess, sys\n"
            "from agent_comms import cli, mcp_server, monitor\n"
            "from agent_comms.cli import run as cli_run\n"
            "LAUNCHER = [sys.executable, '-m', 'agent_comms.seat']\n"
            "def _cli(*argv):\n"
            "    return cli_run(list(argv))\n"
            "class T:\n"
            "    def helper(self, *argv, db=True):\n"
            "        return cli.run([*(['--db', 'x'] if db else []), 'setup', *argv])\n"
            "    def test(self):\n"
            "        cli.run(['--db', 'x', 'doctor'])\n"
            "        monitor.main(['--once'])\n"
            "        mcp_server.main()\n"
            "        subprocess.run([*LAUNCHER, 'a', '--', 'claude'])\n"
            "        subprocess.run([sys.executable, '-m', 'agent_comms.cli', 'admin', 'retry-spawn', '--dispatch-id', 'd'])\n"
            "        self.helper('--project-root', 'p')\n"
            "        _cli('--db', str(self.db), 'import-mailbox', 'old.sqlite')\n"
            "        argv = ['--db', 'x', 'rerender-spawn', 'worker']\n"
            "        cli_run([*argv, '--apply'])\n"
        )
        self.assertEqual(
            credited,
            {"agent-comms doctor", "agent-comms-monitor", "agent-comms-mcp", "agent-comms-seat",
             "agent-comms admin retry-spawn", "agent-comms setup", "agent-comms import-mailbox",
             "agent-comms rerender-spawn"},
        )

    def test_mentions_without_an_invocation_are_not_evidence(self) -> None:
        credited = self.index(
            "import subprocess, sys\n"
            "from agent_comms import cli\n"
            "# agent_comms.mcp_server and monitor.main( are named in this comment only\n"
            "UNUSED = ['admin', 'settle-dispatch', '--dry-run']\n"
            "UNUSED_LAUNCHER = [sys.executable, '-m', 'agent_comms.monitor', '--once']\n"
            "def other(self):\n"
            "    argv = ['--db', 'x', 'demo']\n"
            "class T:\n"
            "    def invoke(self, *argv):\n"
            "        return cli.run(['--db', 'x', *argv])\n"
            "    def test(self, unrelated, actual, flag):\n"
            "        cli.build_parser().parse_args(['admin', 'retry-spawn', '--dispatch-id', 'd'])\n"
            "        cli.run(argv)\n"
            "        self.run_cli('doctor')\n"
            "        unrelated.invoke('doctor')\n"
            "        print([sys.executable, '-m', 'agent_comms.cli', 'doctor'])\n"
            "        self.assertEqual(actual, [sys.executable, '-m', 'agent_comms.cli', 'doctor'])\n"
            "        if flag:\n"
            "            branch = ['--db', 'x', 'doctor']\n"
            "        cli.run(branch)\n"
        )
        self.assertEqual(credited, set())

    def test_bindings_are_resolved_at_the_call(self) -> None:
        credited = self.index(
            "from agent_comms import cli\n"
            "def test(self, flag):\n"
            "    argv = ['version']\n"
            "    cli.run(argv)\n"
            "    argv = ['doctor']\n"
            "    argv = unknown()\n"
            "    cli.run(argv)\n"
            "    both = ['demo']\n"
            "    if flag:\n"
            "        both = ['inbox', 'a']\n"
            "    cli.run(both)\n"
            "    with x() as y:\n"
            "        inner = ['status', 'a']\n"
            "        cli.run(inner)\n"
            "    loop = ['--db', 'x', 'doctor']\n"
            "    for loop in rows():\n"
            "        cli.run(loop)\n"
            "    cli.run(loop)\n"
            "    with opened() as inner:\n"
            "        cli.run(inner)\n"
            "    pair = ['--db', 'x', 'doctor']\n"
            "    pair, other = ['version'], None\n"
            "    cli.run(pair)\n"
            "    chain = head = ['--db', 'x', 'doctor']\n"
            "    cli.run(chain)\n"
        )
        self.assertEqual(credited, {"agent-comms version", "agent-comms status"})

    def test_helpers_resolve_within_their_own_class(self) -> None:
        credited = self.index(
            "from agent_comms import cli\n"
            "class CliTests:\n"
            "    def invoke(self, *argv):\n"
            "        return cli.run(list(argv))\n"
            "    def test_real(self):\n"
            "        self.invoke('version')\n"
            "class OtherTests:\n"
            "    def invoke(self, *argv):\n"
            "        return argv\n"
            "    def test_data(self):\n"
            "        self.assertEqual(self.invoke('doctor'), ('doctor',))\n"
            "class NoHelper:\n"
            "    def test_other(self):\n"
            "        self.invoke('demo')\n"
            "    def test_local(self):\n"
            "        def invoke(*argv):\n"
            "            return cli.run(list(argv))\n"
            "        self.invoke('inbox', 'a')\n"
            "def outer():\n"
            "    def helper(*argv):\n"
            "        return cli.run(list(argv))\n"
            "def helper(*argv):\n"
            "    return argv\n"
            "def test_data():\n"
            "    assert helper('doctor') == ('doctor',)\n"
            "def shadowed(*argv):\n"
            "    return cli.run(list(argv))\n"
            "def test_shadow():\n"
            "    def shadowed(*argv):\n"
            "        return argv\n"
            "    shadowed('status', 'a')\n"
            "def test_parameter(shadowed=lambda *argv: argv):\n"
            "    shadowed('demo')\n"
            "def test_assigned():\n"
            "    shadowed = lambda *argv: argv\n"
            "    shadowed('wait', 'a')\n"
            "def test_import():\n"
            "    from builtins import tuple as shadowed\n"
            "    shadowed('doctor')\n"
            "def test_class():\n"
            "    class shadowed:\n"
            "        def __init__(self, *argv):\n"
            "            self.argv = argv\n"
            "    shadowed('inbox', 'a')\n"
            "def test_capture():\n"
            "    match (lambda *argv: argv):\n"
            "        case shadowed:\n"
            "            shadowed('status', 'a')\n"
        )
        self.assertEqual(credited, {"agent-comms version"})


if __name__ == "__main__":
    unittest.main()
