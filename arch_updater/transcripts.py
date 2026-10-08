from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


CODEX_ROOT = Path.home() / ".codex" / "sessions"
# Claude Code names a project directory after the path it was opened in, slashes turned to dashes.
CLAUDE_ROOT = Path.home() / ".claude" / "projects" / str(Path.home()).replace("/", "-")
PACMAN_LOG = Path("/var/log/pacman.log")
FAILURE_VAULT = Path(__file__).resolve().parent / "data" / "failure-modes.json"
UUID_RE = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
SECRET_RE = re.compile(
    r"(?i)(?P<key>[A-Z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY)[A-Z0-9_]*)"
    r"(?P<sep>\s*=\s*|\s+)(?P<value>[^\s;]+)"
)
AUTH_RE = re.compile(r"(?i)(authorization:\s*(?:bearer|basic)\s+)[^\s'\"]+")
URL_AUTH_RE = re.compile(r"(https?://)[^/@\s:]+:[^/@\s]+@")
MUTATION_OUTPUT_RE = re.compile(
    r"(?i)\b(?:upgrading|installing|removing)\s+[A-Za-z0-9]"
    r"|\[ALPM\]\s+(?:upgraded|installed|removed)\s+"
    r"|:: Processing package changes"
    r"|Update complete",
)


@dataclass
class Action:
    source: str
    session_id: str
    transcript: str
    timestamp: str | None
    call_id: str
    kind: str
    command: str
    command_sha256: str
    success: bool | None
    changed: bool
    evidence: str
    failure_modes: list[str]
    signals: list[str]


def redact(text: str) -> str:
    text = SECRET_RE.sub(lambda match: f"{match.group('key')}{match.group('sep')}<REDACTED>", text)
    text = AUTH_RE.sub(r"\1<REDACTED>", text)
    return URL_AUTH_RE.sub(r"\1<REDACTED>@", text)


def command_digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest()


def failure_evidence(text: str) -> tuple[list[str], list[str]]:
    modes: list[str] = []
    if FAILURE_VAULT.exists():
        with FAILURE_VAULT.open(encoding="utf-8") as handle:
            for mode in json.load(handle).get("modes", []):
                if re.search(mode["match"], text):
                    modes.append(mode["id"])
    signal_pattern = re.compile(
        r"(?i)(error|failed|failure|warning|are in conflict|conflicting dependencies|not found|not available|password is required|unknown download protocol)"
    )
    ignored = re.compile(
        r"(?i)(Warning: truncated output|Skipping verification|Using existing \$srcdir|Error while writing index.*No debugging symbols)"
    )
    ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x1b]*(?:\x1b\\|\x07)|\(B)")

    def diagnostic_lines(value: str) -> Iterable[str]:
        for raw_line in value.splitlines():
            raw_line = raw_line.strip()
            if raw_line.startswith("{"):
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("output"), str):
                    yield from diagnostic_lines(payload["output"])
                    continue
            yield raw_line

    signals: list[str] = []
    for line in diagnostic_lines(text):
        clean = " ".join(redact(ansi.sub("", line)).split())
        if clean.lower().startswith(("looking for conflicting packages", "checking for file conflicts")):
            continue
        if clean and not ignored.search(clean) and signal_pattern.search(clean) and clean not in signals:
            signals.append(clean[:500])
        if len(signals) >= 12:
            break
    return sorted(set(modes)), signals


def _split_shell(command: str) -> Iterable[str]:
    for segment in re.split(r"(?:\r?\n|&&|\|\||;)", command):
        segment = segment.strip()
        if segment:
            yield segment


def _tokens(segment: str) -> list[str]:
    try:
        tokens = shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return []
    while tokens and tokens[0] in {"if", "then", "do", "time", "command", "builtin"}:
        tokens.pop(0)
    if tokens and tokens[0] == "env":
        tokens.pop(0)
        while tokens and "=" in tokens[0] and not tokens[0].startswith("-"):
            tokens.pop(0)
    if tokens and Path(tokens[0]).name in {"sudo", "pkexec", "doas"}:
        tokens.pop(0)
        while tokens and tokens[0].startswith("-"):
            option = tokens.pop(0)
            if option in {"-u", "--user", "-g", "--group"} and tokens:
                tokens.pop(0)
    return tokens


def _short_command(command: str) -> str:
    clean = " ".join(redact(command).split())
    return clean if len(clean) <= 500 else f"{clean[:497]}..."


def classify_command(command: str) -> str | None:
    """Classify actual package-manager mutations, not mentions in searches or logs."""
    for segment in _split_shell(command):
        tokens = _tokens(segment)
        if not tokens:
            continue
        exe = Path(tokens[0]).name
        args = tokens[1:]
        joined = " ".join(args)

        command_index = next(
            (index for index, arg in enumerate(args) if arg.startswith("-") and "c" in arg[1:]),
            None,
        )
        if exe in {"bash", "sh", "fish"} and command_index is not None:
            nested = args[command_index + 1] if command_index + 1 < len(args) else ""
            nested_kind = classify_command(nested)
            if nested_kind:
                return nested_kind

        if exe == "pacman":
            if "--print" in args or "--downloadonly" in args:
                continue
            flags = "".join(arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--"))
            if "S" in flags and not ({"u", "y"} & set(flags)) and ({"i", "s", "l", "g", "p"} & set(flags)):
                continue
            if "S" in flags and ("u" in flags or "y" in flags):
                return "repo-update"
            if "U" in flags:
                return "local-package-update"
            if "S" in flags or "--sync" in args:
                if any(re.fullmatch(r"linux(?:-[a-z0-9-]+)?(?:-headers)?", arg) for arg in args):
                    return "kernel-change"
                return "package-install"
            if "R" in flags or any(arg.startswith("--remove") for arg in args):
                return "package-remove"

        if exe in {"yay", "paru", "pikaur"}:
            if any(option in args for option in {"--print", "--gendb", "--help", "-h"}):
                continue
            flags = "".join(arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--"))
            if "S" in flags and not ({"u", "a", "y"} & set(flags)) and ({"i", "s", "l", "g", "p", "c"} & set(flags)):
                continue
            if "S" in flags and ("u" in flags or "a" in flags or "y" in flags):
                return "aur-update"
            if not args or "S" in flags or "--sync" in args:
                return "aur-package-install"

        if exe == "flatpak" and args and args[0] in {"update", "install", "uninstall"}:
            return f"flatpak-{args[0]}"
        if exe == "fwupdmgr" and args and args[0] in {"update", "install"}:
            return "firmware-update"
        if exe == "rustup" and args and args[0] in {"update", "toolchain"}:
            return "runtime-update"
        if exe == "pipx" and args and args[0].startswith("upgrade"):
            return "runtime-update"
        if exe in {"npm", "pnpm"} and args and args[0] in {"update", "upgrade"} and "-g" in joined:
            return "runtime-update"
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def pacman_change_times(path: Path = PACMAN_LOG) -> list[datetime]:
    if not path.exists():
        return []
    result: list[datetime] = []
    pattern = re.compile(r"^\[([^]]+)] \[ALPM] (?:upgraded|installed|removed) ")
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = pattern.match(line)
            if not match:
                continue
            parsed = _parse_time(match.group(1))
            if parsed:
                result.append(parsed)
    return result


def _pacman_near(timestamp: str | None, changes: list[datetime]) -> str | None:
    parsed = _parse_time(timestamp)
    if not parsed:
        return None
    for change in changes:
        if change.tzinfo is None and parsed.tzinfo is not None:
            change = change.replace(tzinfo=parsed.tzinfo)
        delta = change - parsed
        if timedelta(minutes=-2) <= delta <= timedelta(minutes=90):
            return change.isoformat()
    return None


def _result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_result_text(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"input_text", "output_text", "text"}:
            return str(value.get("text", ""))
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def _execution_session_ids(text: str) -> set[str]:
    result: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or "session_id" not in payload:
            continue
        if not ({"chunk_id", "output", "exit_code"} & payload.keys()):
            continue
        result.add(str(payload["session_id"]))
    return result


def _codex_command(payload: dict[str, Any]) -> str | None:
    if payload.get("type") == "function_call" and payload.get("name") == "exec_command":
        try:
            return json.loads(payload.get("arguments", "{}"))["cmd"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
    if payload.get("type") != "custom_tool_call" or payload.get("name") != "exec":
        return None
    source = payload.get("input", "")
    marker = "tools.exec_command("
    start = source.find(marker)
    if start < 0:
        return None
    candidate = source[start + len(marker) :]
    try:
        obj, _ = json.JSONDecoder().raw_decode(candidate)
    except json.JSONDecodeError:
        match = re.search(r'(?:"cmd"|cmd)\s*:\s*', candidate)
        if not match:
            return None
        try:
            command, _ = json.JSONDecoder().raw_decode(candidate[match.end() :])
        except json.JSONDecodeError:
            return None
        return command if isinstance(command, str) else None
    return obj.get("cmd") if isinstance(obj, dict) else None


def scan_codex(path: Path, changes: list[datetime]) -> list[Action]:
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, str] = {}
    session_id = path.stem.rsplit("-", 5)[-1]
    all_outputs: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = row.get("payload", {})
            if row.get("type") == "session_meta":
                session_id = payload.get("id") or payload.get("session_id") or session_id
            if row.get("type") == "event_msg" and payload.get("type") == "exec_command_end":
                call_id = payload.get("call_id", "")
                output = payload.get("aggregated_output") or "\n".join(
                    str(payload.get(key, "")) for key in ("stdout", "stderr")
                )
                exit_code = payload.get("exit_code")
                output = f'{output}\n{{"exit_code":{exit_code}}}'
                results[call_id] = output
                all_outputs.append(output)
            if row.get("type") != "response_item":
                continue
            command = _codex_command(payload)
            if command:
                call_id = payload.get("call_id") or payload.get("id", "")
                calls[call_id] = {"command": command, "timestamp": row.get("timestamp")}
            if payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                call_id = payload.get("call_id", "")
                output = _result_text(payload.get("output"))
                results[call_id] = output
                all_outputs.append(output)

    execution_outputs: dict[str, list[str]] = {}
    for output in all_outputs:
        for execution_id in _execution_session_ids(output):
            execution_outputs.setdefault(execution_id, []).append(output)

    actions: list[Action] = []
    for call_id, call in calls.items():
        command = call["command"]
        kind = classify_command(command)
        if not kind:
            continue
        direct_result = results.get(call_id, "")
        linked = [
            output
            for execution_id in _execution_session_ids(direct_result)
            for output in execution_outputs.get(execution_id, [])
        ]
        result = "\n".join([direct_result, *linked])
        exit_codes = [int(value) for value in re.findall(r'"exit_code"\s*:\s*(\d+)', result)]
        if exit_codes:
            success: bool | None = exit_codes[-1] == 0
        elif re.search(r"(?i)(?:Script failed|Process exited with code [1-9])", result):
            success = False
        elif re.search(r"(?i)(?:Script completed|Process exited with code 0)", result):
            success = True
        else:
            success = None
        log_match = _pacman_near(call["timestamp"], changes) if "package" in kind or kind.endswith("update") else None
        output_changed = bool(MUTATION_OUTPUT_RE.search(result))
        log_proves_silent_call = not result.strip() and log_match is not None
        changed = success is not False and (output_changed or log_proves_silent_call)
        evidence = "tool-output" if output_changed else f"pacman-log:{log_match}" if log_proves_silent_call else "attempt-only"
        failure_modes, signals = failure_evidence(result)
        actions.append(
            Action(
                source="codex",
                session_id=str(session_id),
                transcript=str(path),
                timestamp=call["timestamp"],
                call_id=call_id,
                kind=kind,
                command=_short_command(command),
                command_sha256=command_digest(command),
                success=success,
                changed=changed,
                evidence=evidence,
                failure_modes=failure_modes,
                signals=signals,
            )
        )
    return actions


def scan_claude(path: Path, changes: list[datetime]) -> list[Action]:
    calls: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    session_id = path.stem
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = row.get("sessionId") or row.get("session_id") or session_id
            content = row.get("message", {}).get("content", [])
            if not isinstance(content, list):
                continue
            if row.get("type") == "assistant":
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    command = block.get("input", {}).get("command")
                    if command:
                        calls[block.get("id", "")] = {"command": command, "timestamp": row.get("timestamp")}
            if row.get("type") == "user":
                tool_result = row.get("toolUseResult")
                interrupted = bool(tool_result.get("interrupted")) if isinstance(tool_result, dict) else False
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    results[block.get("tool_use_id", "")] = {
                        "text": _result_text(block.get("content")),
                        "error": bool(block.get("is_error")),
                        "interrupted": interrupted,
                    }

    actions: list[Action] = []
    for call_id, call in calls.items():
        command = call["command"]
        kind = classify_command(command)
        if not kind:
            continue
        result = results.get(call_id)
        text = result["text"] if result else ""
        success = None if result is None else not result["error"] and not result["interrupted"]
        log_match = _pacman_near(call["timestamp"], changes) if "package" in kind or kind.endswith("update") else None
        output_changed = bool(MUTATION_OUTPUT_RE.search(text))
        log_proves_silent_call = not text.strip() and log_match is not None
        changed = success is not False and (output_changed or log_proves_silent_call)
        evidence = "tool-output" if output_changed else f"pacman-log:{log_match}" if log_proves_silent_call else "attempt-only"
        failure_modes, signals = failure_evidence(text)
        actions.append(
            Action(
                source="claude",
                session_id=str(session_id),
                transcript=str(path),
                timestamp=call["timestamp"],
                call_id=call_id,
                kind=kind,
                command=_short_command(command),
                command_sha256=command_digest(command),
                success=success,
                changed=changed,
                evidence=evidence,
                failure_modes=failure_modes,
                signals=signals,
            )
        )
    return actions


def scan_all(codex_root: Path = CODEX_ROOT, claude_root: Path = CLAUDE_ROOT) -> dict[str, Any]:
    changes = pacman_change_times()
    actions: list[Action] = []
    codex_paths = list(sorted(codex_root.rglob("*.jsonl"))) if codex_root.exists() else []
    for path in codex_paths:
        actions.extend(scan_codex(path, changes))
    claude_paths = (
        [path for path in sorted(claude_root.glob("*.jsonl")) if UUID_RE.fullmatch(path.stem)]
        if claude_root.exists()
        else []
    )
    for path in claude_paths:
        actions.extend(scan_claude(path, changes))

    sessions: dict[tuple[str, str], dict[str, Any]] = {}
    for action in actions:
        key = (action.source, action.session_id)
        session = sessions.setdefault(
            key,
            {
                "source": action.source,
                "session_id": action.session_id,
                "transcript": action.transcript,
                "first_action_at": action.timestamp,
                "last_action_at": action.timestamp,
                "changed": False,
                "actions": [],
            },
        )
        session["changed"] = session["changed"] or action.changed
        session["last_action_at"] = action.timestamp or session["last_action_at"]
        session["actions"].append(asdict(action))

    ordered = sorted(
        sessions.values(), key=lambda item: (item.get("first_action_at") or "", item["source"], item["session_id"])
    )
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "scope": {
            "codex_root": str(codex_root),
            "claude_root": str(claude_root),
            "pacman_log": str(PACMAN_LOG),
        },
        "counts": {
            "codex_transcripts_scanned": len(codex_paths),
            "claude_transcripts_scanned": len(claude_paths),
            "sessions_with_candidates": len(ordered),
            "sessions_with_proven_changes": sum(1 for session in ordered if session["changed"]),
            "candidate_actions": len(actions),
            "proven_actions": sum(1 for action in actions if action.changed),
        },
        "sessions": ordered,
    }


def build_failure_evidence(report: dict[str, Any]) -> dict[str, Any]:
    modes: dict[str, list[dict[str, Any]]] = {}
    unclassified: list[dict[str, Any]] = []
    for session in report["sessions"]:
        for action in session["actions"]:
            evidence = {
                "source": session["source"],
                "session_id": session["session_id"],
                "timestamp": action["timestamp"],
                "kind": action["kind"],
                "command_sha256": action["command_sha256"],
                "changed": action["changed"],
                "signals": action["signals"],
            }
            for mode in action["failure_modes"]:
                modes.setdefault(mode, []).append(evidence)
            if action["signals"] and not action["failure_modes"]:
                unclassified.append(evidence)
    return {
        "generated_at": report["generated_at"],
        "source_report_counts": report["counts"],
        "modes": modes,
        "unclassified_signals": unclassified,
    }


def write_session_report(output_dir: Path) -> dict[str, Any]:
    report = scan_all()
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "update-sessions.json"
    markdown_path = output_dir / "UPDATE-SESSIONS.md"
    evidence_path = output_dir / "failure-evidence.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    evidence_path.write_text(
        json.dumps(build_failure_evidence(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "report": report,
        "json": str(json_path),
        "markdown": str(markdown_path),
        "failure_evidence": str(evidence_path),
    }


def render_markdown(report: dict[str, Any]) -> str:
    proven = [session for session in report["sessions"] if session["changed"]]
    lines = [
        "# Update transcript census",
        "",
        "A session appears here only when a structurally extracted package-manager action has mutation evidence from its tool result or a nearby pacman log entry.",
        "",
        f"Proven sessions: **{len(proven)}**. Candidate sessions retained in JSON: **{report['counts']['sessions_with_candidates']}**.",
        "",
        "| Time | Surface | Session | Proven actions | Kinds |",
        "|---|---|---|---:|---|",
    ]
    for session in proven:
        actions = [action for action in session["actions"] if action["changed"]]
        kinds = ", ".join(sorted({action["kind"] for action in actions}))
        session_id = session["session_id"]
        lines.append(
            f"| {session['first_action_at'] or '-'} | {session['source']} | `{session_id}` | {len(actions)} | {kinds} |"
        )
    lines.extend(["", "## Proven evidence", ""])
    for session in proven:
        lines.append(f"### {session['source']} `{session['session_id']}`")
        lines.append("")
        lines.append(f"Transcript: `{session['transcript']}`")
        lines.append("")
        for action in session["actions"]:
            if not action["changed"]:
                continue
            lines.append(f"- `{action['kind']}` at `{action['timestamp']}`: `{action['command']}`")
        lines.append("")
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "Read-only package queries and transcript text that merely mentions update commands are excluded. Commands without mutation evidence remain in the JSON report as attempted candidates.",
            "",
        ]
    )
    return "\n".join(lines)
