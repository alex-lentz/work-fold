#!/usr/bin/env python3
"""work-fold CLI transport for Linux.

Faithful port of desktop/cli/work-fold-cli.jxa.js (the macOS transport) to a
standard-library-only Python 3 script, since Linux has no single guaranteed
scripting engine with built-in JSON support the way macOS has JavaScript for
Automation and Windows has PowerShell. Keep this file's protocol handling in
lockstep with the JXA and PowerShell shims -- they all speak the same
request/response file protocol documented in AGENTS.md.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

ACT_UNAVAILABLE_MESSAGE = "Open work-fold to run this command. Act commands need the work-fold app running."
ACT_MAX_MESSAGE_FILE_BYTES = 262144
ACT_GROUPS = {
    "chat", "chats", "files", "manage", "history", "search", "library",
    "tools", "apps", "routings", "pages", "staged",
}


class UsageError(Exception):
    exit_code = 2


class TransportTimeout(Exception):
    exit_code = 124


def main() -> int:
    if len(sys.argv) < 2:
        sys.stderr.write("work-fold: internal CLI invocation error (missing script directory).\n")
        return 1

    script_directory = sys.argv[1]
    command_arguments = sys.argv[2:]

    try:
        app_path = resolve_app_path(script_directory)
        state_root = resolve_state_root(app_path)
        cli_root = state_root / "cli"
        (cli_root / "requests").mkdir(parents=True, exist_ok=True)
        (cli_root / "responses").mkdir(parents=True, exist_ok=True)
        timeout_ms = resolve_timeout_ms()
        context = {"app_path": app_path, "cli_root": cli_root, "timeout_ms": timeout_ms}

        if is_act_command(command_arguments):
            # Every act family (Chats, files, History, Library, Spaces, tools,
            # apps, routings, pages, staged acts) rides the separately
            # versioned act lane and requires the per-launch token the
            # running app minted.
            act_token = read_act_token(cli_root)
            if not act_token:
                sys.stderr.write(f"work-fold: {ACT_UNAVAILABLE_MESSAGE}\n")
                return 6
            wait_plan = parse_wait_command(command_arguments)
            if wait_plan is not None:
                return run_wait_loop(context, wait_plan, act_token)
            argv, payload = prepare_act_arguments(command_arguments)
            outcome = perform_request(context, argv, act_token, payload)
            emit_outcome(outcome)
            return outcome["exitCode"]

        outcome = perform_request(context, command_arguments, None, None)
        emit_outcome(outcome)
        return outcome["exitCode"]
    except TransportTimeout as error:
        sys.stderr.write(f"work-fold: {error}\n")
        return TransportTimeout.exit_code
    except UsageError as error:
        sys.stderr.write(f"work-fold: {error}\n")
        return UsageError.exit_code
    except Exception as error:  # noqa: BLE001 - CLI boundary reports and exits
        sys.stderr.write(f"work-fold: {error}\n")
        return 1


def resolve_app_path(script_directory: str) -> Path:
    configured = os.environ.get("WORKFOLD_CLI_APP", "").strip()
    if configured:
        path = Path(configured)
        if not path.exists():
            raise RuntimeError(f"work-fold executable was not found at {path}.")
        return path
    bundled = Path(script_directory) / ".." / "work-fold"
    bundled = bundled.resolve()
    if not bundled.exists():
        raise RuntimeError(f"work-fold executable was not found at {bundled}.")
    return bundled


def resolve_state_root(app_path: Path) -> Path:
    configured = os.environ.get("WORKFOLD_CLI_STATE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(config_home) / "work-fold"


def resolve_timeout_ms() -> int:
    raw = os.environ.get("WORKFOLD_CLI_TIMEOUT_MS", "").strip()
    if not raw:
        return 120000
    try:
        value = int(raw)
    except ValueError as error:
        raise RuntimeError("WORKFOLD_CLI_TIMEOUT_MS must be an integer between 100 and 600000.") from error
    if value < 100 or value > 600000:
        raise RuntimeError("WORKFOLD_CLI_TIMEOUT_MS must be an integer between 100 and 600000.")
    return value


def is_act_command(command_arguments):
    positional = [token for token in command_arguments if token != "--json"]
    group = positional[0] if positional else ""
    if group in ACT_GROUPS:
        return True
    if group == "checks":
        return len(positional) < 2 or positional[1] != "status"
    return group == "spaces" and len(positional) >= 2 and positional[1] != "list"


def parse_wait_command(command_arguments):
    positional = [token for token in command_arguments if token != "--json"]
    group = positional[0] if positional else None
    if group not in ("chat", "manage", "checks") or len(positional) < 2 or positional[1] != "wait":
        return None
    json_output = "--json" in command_arguments
    space = ""
    task = ""
    timeout_seconds = 600
    index = 0
    while index < len(command_arguments):
        token = command_arguments[index]
        if token == "--space":
            space = command_arguments[index + 1] if index + 1 < len(command_arguments) else ""
            index += 2
            continue
        if token == "--task":
            task = command_arguments[index + 1] if index + 1 < len(command_arguments) else ""
            index += 2
            continue
        if token == "--timeout":
            raw = command_arguments[index + 1] if index + 1 < len(command_arguments) else ""
            try:
                timeout_seconds = int(raw)
            except ValueError:
                timeout_seconds = -1
            if timeout_seconds < 1 or timeout_seconds > 3600:
                raise UsageError("--timeout must be an integer between 1 and 3600 seconds.")
            index += 2
            continue
        if token in (group, "wait", "--json"):
            index += 1
            continue
        raise UsageError(f"Unknown option for {group} wait: {token}")

    if group in ("chat", "checks") and not space:
        raise UsageError("Act commands require an explicit --space <id-or-name>.")
    if group == "manage" and space:
        raise UsageError("The management scope does not take --space.")
    if not task:
        source = "checks run" if group == "checks" else f"{group} send"
        raise UsageError(f"Provide --task <id> from {source}.")
    return {"group": group, "space": space, "task": task, "timeoutSeconds": timeout_seconds, "json": json_output}


def run_wait_loop(context, plan, act_token):
    # Waiting is task-scoped: it follows the exact turn the send accepted, so
    # an older assistant message can never read as this turn's success.
    scope_argv = ["--space", plan["space"]] if plan["group"] in ("chat", "checks") else []
    status_argv = [plan["group"], "task" if plan["group"] == "checks" else "status"] + scope_argv + ["--task", plan["task"], "--json"]
    deadline = time.monotonic() + plan["timeoutSeconds"]
    while True:
        status = perform_request(context, status_argv, act_token, None)
        if status["exitCode"] != 0:
            emit_outcome(status)
            return status["exitCode"]
        try:
            state = json.loads(status["stdout"])["data"]["task"]["state"]
        except Exception as error:  # noqa: BLE001
            raise RuntimeError("work-fold returned an unreadable task status.") from error
        if state not in ("accepted", "running"):
            break
        if time.monotonic() >= deadline:
            sys.stderr.write(f"work-fold: {plan['group']} wait timed out after {plan['timeoutSeconds']}s.\n")
            return 7
        time.sleep(2)

    result_argv = [plan["group"], "result"] + scope_argv + ["--task", plan["task"]]
    if plan["json"]:
        result_argv.append("--json")
    result = perform_request(context, result_argv, act_token, None)
    emit_outcome(result)
    return result["exitCode"]


def prepare_act_arguments(command_arguments):
    argv = []
    payload = None
    index = 0
    while index < len(command_arguments):
        token = command_arguments[index]
        if token != "--message-file":
            argv.append(token)
            index += 1
            continue
        if index + 1 >= len(command_arguments):
            raise UsageError("--message-file requires a path.")
        if payload is not None:
            raise UsageError("--message-file may be provided only once.")
        message_path = Path(command_arguments[index + 1]).expanduser()
        if not message_path.is_absolute():
            message_path = Path.cwd() / message_path
        index += 2
        text = message_path.read_text(encoding="utf-8")
        if len(text.encode("utf-8")) > ACT_MAX_MESSAGE_FILE_BYTES:
            raise UsageError(f"--message-file exceeds {ACT_MAX_MESSAGE_FILE_BYTES} bytes.")
        payload = {"messageFile": text}
        argv.append("--message-from-payload")
    return argv, payload


def read_act_token(cli_root: Path):
    token_path = cli_root / "act-token.json"
    if not token_path.exists():
        return None
    try:
        record = json.loads(token_path.read_text(encoding="utf-8"))
        if record.get("version") != 1:
            return None
        token = record.get("actToken")
        if not isinstance(token, str):
            return None
        import re
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,256}", token):
            return None
        return token
    except Exception:  # noqa: BLE001
        return None


def perform_request(context, argv, act_token, payload):
    cli_root: Path = context["cli_root"]
    request_id = str(uuid.uuid4())
    request_path = cli_root / "requests" / f"{request_id}.json"
    response_path = cli_root / "responses" / f"{request_id}.json"
    temporary_request_path = cli_root / "requests" / f"{request_id}.{uuid.uuid4()}.tmp"

    try:
        if act_token:
            request = {
                "protocolVersion": 2,
                "lane": "act",
                "id": request_id,
                "argv": argv,
                "cwd": str(Path.cwd()),
                "createdAt": iso_now(),
                "actToken": act_token,
            }
            if payload is not None:
                request["payload"] = payload
        else:
            request = {
                "protocolVersion": 1,
                "id": request_id,
                "argv": argv,
                "cwd": str(Path.cwd()),
                "createdAt": iso_now(),
            }
        temporary_request_path.write_text(json.dumps(request), encoding="utf-8")
        os.rename(temporary_request_path, request_path)
        temporary_request_path = None

        subprocess.Popen(
            [str(context["app_path"]), "--work-fold-cli-request", request_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        deadline = time.monotonic() + context["timeout_ms"] / 1000
        while not response_path.exists():
            if time.monotonic() >= deadline:
                raise TransportTimeout(f"work-fold did not answer CLI request {request_id} within {context['timeout_ms']} ms.")
            time.sleep(0.05)

        response = json.loads(response_path.read_text(encoding="utf-8"))
        if response.get("protocolVersion") != 1:
            raise RuntimeError(f"work-fold returned unsupported CLI protocol version {response.get('protocolVersion')}.")
        if response.get("id") != request_id:
            raise RuntimeError("work-fold returned a CLI response with the wrong request id.")
        exit_code = response.get("exitCode")
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise RuntimeError("work-fold returned an invalid CLI exit code.")
        stdout = response.get("stdout")
        stderr = response.get("stderr")
        return {
            "exitCode": exit_code,
            "stdout": stdout if isinstance(stdout, str) else "",
            "stderr": stderr if isinstance(stderr, str) else "",
        }
    finally:
        for path in (temporary_request_path, request_path, response_path):
            if path is not None and path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass


def emit_outcome(outcome):
    if outcome["stdout"]:
        sys.stdout.write(outcome["stdout"])
        sys.stdout.flush()
    if outcome["stderr"]:
        sys.stderr.write(outcome["stderr"])
        sys.stderr.flush()


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int(time.time() * 1000) % 1000:03d}Z"


if __name__ == "__main__":
    sys.exit(main())
