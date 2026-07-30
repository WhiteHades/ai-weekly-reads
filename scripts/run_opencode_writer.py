from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from project_paths import CONFIG, ROOT
from utils import parse_date, read_json


DEFAULT_CONFIG = CONFIG / "writer.json"
FALLBACK_CONFIG = CONFIG / "writer.example.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run OpenCode to write a daily or Friday edition.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--issue-id", required=True)
    parser.add_argument("--kind", required=True, choices=("daily", "friday", "weekly", "one-shot"))
    parser.add_argument("--lane", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--published", required=True, help="Publication date as YYYY-MM-DD.")
    parser.add_argument("--output", required=True, help="Output path relative to the Knowledge root, or absolute.")
    parser.add_argument("--source", action="append", default=[], help="Source path relative to the Knowledge root. Repeatable.")
    parser.add_argument("--context-file", action="append", default=[], help="Optional extra context file for the writer.")
    parser.add_argument("--dry-run", action="store_true", help="Print command and prompt without running OpenCode.")
    parser.add_argument("--keep-session", action="store_true", help="Do not delete the OpenCode session after the run.")
    parser.add_argument("--skip-output-check", action="store_true", help="Record source usage even if the output file is missing.")
    args = parser.parse_args()

    if not args.source:
        raise SystemExit("At least one --source is required so the edition remains auditable.")
    published = _published_date(args.published)
    settings = _load_settings(args.config)
    knowledge_root = _resolve_path(settings.get("knowledge_root", "../../.."), ROOT)
    prompt = _build_prompt(args, settings, published, knowledge_root)
    title = f"ai-weekly-writer:{args.issue_id}"
    command = _opencode_command(settings, knowledge_root, title, prompt)
    source_usage_command = _source_usage_command(args, published)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "opencode_command": command,
                    "opencode_command_shell": shlex.join(command),
                    "source_usage_command": source_usage_command,
                    "source_usage_command_shell": shlex.join(source_usage_command),
                    "prompt": prompt,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    before_sessions = _list_sessions()
    result = subprocess.run(command, text=True, capture_output=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)

    after_sessions = _list_sessions()
    new_session_ids = _new_session_ids(before_sessions, after_sessions, title)
    try:
        if result.returncode != 0:
            raise SystemExit(result.returncode)
        if not args.skip_output_check:
            _require_output(args.output, knowledge_root)
        subprocess.run(source_usage_command, check=True)
    finally:
        if _should_delete_session(settings, args.keep_session):
            _delete_sessions(new_session_ids)


def _load_settings(path: Path) -> dict[str, Any]:
    config_path = path if path.exists() else FALLBACK_CONFIG
    return read_json(config_path, {})


def _published_date(value: str) -> str:
    parsed = parse_date(value)
    if parsed is None:
        raise SystemExit(f"Invalid --published date: {value!r}")
    return parsed.isoformat()


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _build_prompt(args: argparse.Namespace, settings: dict[str, Any], published: str, knowledge_root: Path) -> str:
    source_lines = "\n".join(f"- {source}" for source in args.source)
    context_lines = "\n".join(f"- {context_file}" for context_file in args.context_file) or "- none"
    profile_notes = "\n".join(f"- {note}" for note in settings.get("profile_notes", [])) or "- Use clear, source-grounded prose."
    return f"""Write an AI Weekly Reads edition.

Edition metadata:
- issue_id: {args.issue_id}
- kind: {args.kind}
- lane: {args.lane}
- title: {args.title}
- published: {published}
- output_path: {args.output}
- working_directory: {knowledge_root}

Required sources:
{source_lines}

Additional context files:
{context_lines}

Writing profile:
{profile_notes}

Instructions:
- Read every required source before writing.
- Use only the listed sources and context files for source-grounded claims.
- Write the final Markdown edition to exactly `{args.output}`.
- Include a short "Sources Used" section listing every required source path exactly as provided.
- Do not update source ledgers, continuity ledgers, git state, public site files, or source-note frontmatter. The wrapper handles metadata after this run succeeds.
"""


def _opencode_command(settings: dict[str, Any], knowledge_root: Path, title: str, prompt: str) -> list[str]:
    command = ["opencode", "run", "--dir", str(knowledge_root), "--title", title, "--format", "json"]
    model = settings.get("model")
    if model:
        command.extend(["--model", str(model)])
    variant = settings.get("variant")
    if variant:
        command.extend(["--variant", str(variant)])
    agent = settings.get("agent")
    if agent:
        command.extend(["--agent", str(agent)])
    command.append(prompt)
    return command


def _source_usage_command(args: argparse.Namespace, published: str) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts" / "source_usage.py"),
        "add",
        "--issue-id",
        args.issue_id,
        "--kind",
        args.kind,
        "--lane",
        args.lane,
        "--title",
        args.title,
        "--published",
        published,
        "--output",
        args.output,
    ]
    for source in args.source:
        command.extend(["--source", source])
    return command


def _require_output(output: str, knowledge_root: Path) -> None:
    output_path = Path(output).expanduser()
    if not output_path.is_absolute():
        output_path = knowledge_root / output_path
    if not output_path.exists():
        raise SystemExit(f"OpenCode completed but did not create output file: {output_path}")


def _list_sessions() -> list[dict[str, Any]]:
    result = subprocess.run(
        ["opencode", "session", "list", "--format", "json", "--max-count", "50"],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return _session_objects(parsed)


def _session_objects(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        sessions: list[dict[str, Any]] = []
        for item in value:
            sessions.extend(_session_objects(item))
        return sessions
    if isinstance(value, dict):
        sessions = [value] if _session_id(value) else []
        for key in ("sessions", "data", "items", "result"):
            child = value.get(key)
            if child is not None:
                sessions.extend(_session_objects(child))
        return sessions
    return []


def _new_session_ids(before: list[dict[str, Any]], after: list[dict[str, Any]], title: str) -> list[str]:
    before_ids = {_session_id(session) for session in before}
    new_ids = [_session_id(session) for session in after if _session_id(session) not in before_ids]
    titled_ids = [_session_id(session) for session in after if session.get("title") == title]
    return sorted({session_id for session_id in [*new_ids, *titled_ids] if session_id})


def _session_id(session: dict[str, Any]) -> str | None:
    value = session.get("id") or session.get("sessionID") or session.get("session_id")
    return str(value) if value else None


def _should_delete_session(settings: dict[str, Any], keep_session: bool) -> bool:
    return bool(settings.get("delete_session_after_run", True)) and not keep_session


def _delete_sessions(session_ids: list[str]) -> None:
    if not session_ids:
        print("No new OpenCode session ID found to delete.", file=sys.stderr)
        return
    for session_id in session_ids:
        subprocess.run(["opencode", "session", "delete", session_id], check=False)


if __name__ == "__main__":
    main()
