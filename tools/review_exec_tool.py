"""Least-privilege executable inspection tool for read-only review workers."""

from __future__ import annotations

import json
import os
import selectors
import shlex
import signal
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Optional

from tools.registry import registry, tool_error


class ReviewCommandDenied(ValueError):
    """The requested command is outside the review inspection allowlist."""


_GIT_READ_SUBCOMMANDS = frozenset(
    {"diff", "grep", "log", "rev-list", "rev-parse", "show", "status"}
)
_HASH_COMMANDS = frozenset({"sha256sum", "shasum"})
_SHELL_OPERATOR_CHARS = frozenset(";&|<>`\n\r")
_MAX_TIMEOUT_SECONDS = 900
_MAX_OUTPUT_BYTES = 100_000
_IS_WINDOWS = os.name == "nt"
_TRUSTED_EXECUTABLES = {
    "git": ("/usr/bin/git", "/bin/git"),
    "shasum": ("/usr/bin/shasum",),
    "sha256sum": ("/usr/bin/sha256sum", "/bin/sha256sum"),
}
_GIT_SAFE_OPTIONS = frozenset(
    {
        "--cached", "--check", "--decorate", "--exit-code", "--name-only",
        "--no-color", "--no-decorate", "--no-patch", "--oneline", "--porcelain",
        "--short", "--show-toplevel", "--stat", "--summary", "--unified",
        "--untracked-files",
    }
)
_GIT_SAFE_OPTION_PREFIXES = (
    "--format=", "--max-count=", "--unified=", "--untracked-files=", "-n",
)


def _deny(message: str) -> None:
    raise ReviewCommandDenied(message)


def validate_review_command(command: str) -> list[str]:
    """Parse and return argv only for an explicitly read-only inspection command."""
    if not command or any(char in command for char in _SHELL_OPERATOR_CHARS) or "$(" in command:
        _deny("shell operators, substitutions, and redirection are not allowed")
    try:
        argv = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ReviewCommandDenied("command has invalid quoting") from exc
    if not argv:
        _deny("command is empty")
    if Path(argv[0]).name != argv[0]:
        _deny("executable paths are selected by review_exec, not the caller")

    executable = argv[0]
    if executable == "git":
        if len(argv) < 2 or argv[1] not in _GIT_READ_SUBCOMMANDS:
            _deny("git subcommand is not read-only")
        for arg in argv[2:]:
            if not arg.startswith("-") or arg == "--":
                continue
            if arg in _GIT_SAFE_OPTIONS or any(
                arg.startswith(prefix) and len(arg) > len(prefix)
                for prefix in _GIT_SAFE_OPTION_PREFIXES
            ):
                continue
            _deny("Git option is outside the read-only inspection allowlist")
    elif executable == "shasum":
        if argv[1:3] != ["-a", "256"]:
            _deny("shasum is limited to SHA-256")
    elif executable == "sha256sum":
        if any(arg.startswith("-") for arg in argv[1:]):
            _deny("sha256sum options are outside the inspection surface")
    else:
        _deny(f"executable {executable!r} is not in the inspection allowlist")
    return argv


def _workspace_root() -> Path:
    raw = os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip()
    if not raw:
        raise ReviewCommandDenied("review execution requires a Kanban workspace")
    root = Path(raw).expanduser()
    resolved = root.resolve(strict=True)
    if root.absolute() != resolved or not resolved.is_dir():
        raise ReviewCommandDenied("workspace must be an existing non-symlink directory")
    return resolved


def _bounded_workdir(workdir: Optional[str]) -> Path:
    root = _workspace_root()
    raw = Path(workdir).expanduser() if workdir else root
    candidate = raw.resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReviewCommandDenied("workdir must remain inside the assigned workspace") from exc
    if raw.absolute() != candidate or not candidate.is_dir():
        raise ReviewCommandDenied("workdir must be an existing non-symlink directory")
    return candidate


def _trusted_executable(name: str) -> str:
    for raw in _TRUSTED_EXECUTABLES.get(name, ()):
        candidate = Path(raw)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return str(resolved)
    raise ReviewCommandDenied(f"trusted executable {name!r} is unavailable")


def _workspace_operand(root: Path, cwd: Path, raw: str) -> None:
    candidate = Path(raw).expanduser()
    lexical = candidate if candidate.is_absolute() else cwd / candidate
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ReviewCommandDenied(
            "file operands must be existing paths inside the workspace"
        ) from exc
    if lexical.absolute() != resolved or not resolved.is_file():
        raise ReviewCommandDenied("file operands must be regular non-symlink files")


def _validate_operands(argv: list[str], root: Path, cwd: Path) -> None:
    if argv[0] in _HASH_COMMANDS:
        start = 3 if argv[0] == "shasum" else 1
        if len(argv) <= start:
            _deny("hash commands require at least one workspace file")
        for operand in argv[start:]:
            _workspace_operand(root, cwd, operand)
        return
    if "--" in argv[2:]:
        separator = argv.index("--", 2)
        if separator == len(argv) - 1:
            _deny("Git path separator requires a workspace operand")
        for operand in argv[separator + 1:]:
            _workspace_operand(root, cwd, operand)


def _review_env() -> dict[str, str]:
    env = {
        "HOME": "/dev/null",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "TERM": "dumb",
    }
    if os.name == "nt":
        for key in ("SystemRoot", "WINDIR"):
            if os.environ.get(key):
                env[key] = os.environ[key]
    return env


def _kill_process_group(proc: subprocess.Popen, pgid: Optional[int] = None) -> None:
    """Terminate the whole spawned group even after its leader has exited."""
    target = pgid if pgid is not None else proc.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(target, sig)
        except ProcessLookupError:
            return
        except OSError:
            pass
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    os.killpg(target, 0)
                except ProcessLookupError:
                    return
                except OSError:
                    break
                time.sleep(0.05)


def _run_bounded(argv: list[str], *, cwd: Path, timeout: int) -> tuple[int, str]:
    kwargs = {
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "env": _review_env(),
    }
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, **kwargs)
    pgid = proc.pid if os.name == "posix" else None
    if proc.stdout is None:  # defensive fallback and test-double support
        return proc.wait(timeout=timeout), ""

    chunks: deque[bytes] = deque()
    kept = 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _mask in selector.select(min(0.1, remaining)):
                stream = key.fileobj
                descriptor = stream if isinstance(stream, int) else stream.fileno()
                chunk = os.read(descriptor, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                chunks.append(chunk)
                kept += len(chunk)
                while kept > _MAX_OUTPUT_BYTES:
                    excess = kept - _MAX_OUTPUT_BYTES
                    head = chunks[0]
                    if len(head) <= excess:
                        kept -= len(chunks.popleft())
                    else:
                        chunks[0] = head[excess:]
                        kept -= excess
            if proc.poll() is not None and not selector.get_map():
                break
        output = b"".join(chunks).decode("utf-8", errors="replace")
        return int(proc.returncode), output
    except BaseException:
        _kill_process_group(proc, pgid)
        try:
            proc.wait(timeout=1)
        except subprocess.SubprocessError:
            pass
        raise
    finally:
        selector.close()
        proc.stdout.close()


def review_exec(command: str, *, workdir: Optional[str] = None, timeout: int = 300) -> str:
    """Execute one allowlisted inspection command without a shell."""
    try:
        if _IS_WINDOWS:
            raise ReviewCommandDenied("UNVERIFIED_PENDING_WINDOWS_CI")
        argv = validate_review_command(command)
        root = _workspace_root()
        cwd = _bounded_workdir(workdir)
        _validate_operands(argv, root, cwd)
        executable = _trusted_executable(argv[0])
        if argv[0] == "git":
            subcommand = argv[1]
            args = argv[2:]
            argv = [
                executable,
                "-c", "core.fsmonitor=false",
                "-c", "core.untrackedCache=false",
                "-c", "core.hooksPath=/dev/null",
                "--no-pager", subcommand,
            ]
            if subcommand == "diff":
                argv.extend(("--no-ext-diff", "--no-textconv"))
            argv.extend(args)
        else:
            argv[0] = executable
        bounded_timeout = max(1, min(int(timeout), _MAX_TIMEOUT_SECONDS))
        returncode, output = _run_bounded(argv, cwd=cwd, timeout=bounded_timeout)
        return json.dumps(
            {"success": returncode == 0, "output": output, "exit_code": returncode},
            ensure_ascii=False,
        )
    except (ReviewCommandDenied, OSError, ValueError, subprocess.SubprocessError) as exc:
        return tool_error(str(exc))


REVIEW_EXEC_SCHEMA = {
    "name": "review_exec",
    "description": (
        "Run one bounded, allowlisted read-only repository inspection command in the assigned "
        "Kanban workspace. Supports read-only Git queries and SHA-256 hashing of workspace files. "
        "No project code, plugins, test configuration, shell operators, file writes, package "
        "installation, Git mutation, process control, or Hermes runtime/config control."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Single allowlisted inspection command."},
            "workdir": {
                "type": "string",
                "description": "Existing non-symlink directory inside the assigned workspace.",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_TIMEOUT_SECONDS,
                "default": 300,
            },
        },
        "required": ["command"],
    },
}


def _handle_review_exec(args: dict, **_kwargs) -> str:
    return review_exec(
        str(args.get("command", "")),
        workdir=args.get("workdir"),
        timeout=args.get("timeout", 300),
    )


registry.register(
    name="review_exec",
    toolset="review-exec",
    schema=REVIEW_EXEC_SCHEMA,
    handler=_handle_review_exec,
    emoji="🔎",
    max_result_size_chars=_MAX_OUTPUT_BYTES,
)
