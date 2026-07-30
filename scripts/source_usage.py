from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from project_paths import METADATA
from utils import parse_date, read_json, write_json


LEDGER_VERSION = 1
DEFAULT_LEDGER = METADATA / "source_usage.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Track source files used by generated editions.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Add or update one generated edition.")
    add_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    add_parser.add_argument("--issue-id", required=True)
    add_parser.add_argument("--kind", required=True, choices=("daily", "friday", "weekly", "one-shot"))
    add_parser.add_argument("--lane", required=True)
    add_parser.add_argument("--title", required=True)
    add_parser.add_argument("--published", required=True, help="Publication date as YYYY-MM-DD.")
    add_parser.add_argument("--output", required=True, help="Generated Markdown or EPUB path.")
    add_parser.add_argument(
        "--source",
        action="append",
        default=[],
        help="Source file path used by this edition. Repeat for multiple sources.",
    )
    add_parser.set_defaults(func=add_entry)

    context_parser = subparsers.add_parser("context", help="Print recent usage and unused sources as JSON.")
    context_parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    context_parser.add_argument("--lane")
    context_parser.add_argument("--limit", type=int, default=10)
    context_parser.add_argument(
        "--source-root",
        type=Path,
        help="Optional source-note directory to scan for unused Markdown files.",
    )
    context_parser.set_defaults(func=print_context)

    args = parser.parse_args()
    args.func(args)


def add_entry(args: argparse.Namespace) -> None:
    if not args.source:
        raise SystemExit("At least one --source is required so the edition remains auditable.")
    published = _published_date(args.published)
    ledger = _load_ledger(args.ledger)
    now = _now()
    normalized_sources = _unique(_normalize_path(source) for source in args.source)
    entry = {
        "issue_id": args.issue_id,
        "kind": args.kind,
        "lane": args.lane,
        "title": args.title,
        "published": published,
        "output_path": _normalize_path(args.output),
        "sources": normalized_sources,
        "created_at": now,
        "updated_at": now,
    }
    entries = ledger["entries"]
    for index, existing in enumerate(entries):
        if existing.get("issue_id") == args.issue_id:
            entry["created_at"] = existing.get("created_at", now)
            entries[index] = entry
            break
    else:
        entries.append(entry)
    ledger["entries"] = _sort_entries(entries)
    write_json(args.ledger, ledger)
    print(json.dumps(entry, indent=2, ensure_ascii=False))


def print_context(args: argparse.Namespace) -> None:
    ledger = _load_ledger(args.ledger)
    entries = [entry for entry in ledger["entries"] if _matches_lane(entry, args.lane)]
    used_sources = _used_sources(entries)
    payload: dict[str, Any] = {
        "version": LEDGER_VERSION,
        "lane": args.lane,
        "entry_count": len(entries),
        "recent_entries": _sort_entries(entries)[-args.limit :],
        "used_sources": list(used_sources.values()),
    }
    if args.source_root:
        all_sources = _scan_source_root(args.source_root)
        payload["source_root"] = str(args.source_root)
        payload["all_source_count"] = len(all_sources)
        payload["unused_sources"] = [source for source in all_sources if source not in used_sources]
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def _load_ledger(path: Path) -> dict[str, Any]:
    raw = read_json(path, {"version": LEDGER_VERSION, "entries": []})
    if raw.get("version") != LEDGER_VERSION:
        raise SystemExit(f"Unsupported source usage ledger version in {path}: {raw.get('version')!r}")
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise SystemExit(f"Invalid source usage ledger in {path}: entries must be a list")
    return {"version": LEDGER_VERSION, "entries": entries}


def _published_date(value: str) -> str:
    parsed = parse_date(value)
    if parsed is None:
        raise SystemExit(f"Invalid --published date: {value!r}")
    return parsed.isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _unique(values: Any) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _sort_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: (entry.get("published", ""), entry.get("lane", ""), entry.get("issue_id", "")))


def _matches_lane(entry: dict[str, Any], lane: str | None) -> bool:
    return lane is None or entry.get("lane") == lane


def _used_sources(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    used: dict[str, dict[str, Any]] = {}
    for entry in _sort_entries(entries):
        for source in entry.get("sources", []):
            used[source] = {
                "path": source,
                "last_used": entry.get("published"),
                "issue_id": entry.get("issue_id"),
                "lane": entry.get("lane"),
            }
    return dict(sorted(used.items()))


def _scan_source_root(root: Path) -> list[str]:
    if not root.exists():
        raise SystemExit(f"Source root not found: {root}")
    if not root.is_dir():
        raise SystemExit(f"Source root must be a directory: {root}")
    prefix = root.name
    return sorted(
        f"{prefix}/{path.relative_to(root).as_posix()}"
        for path in root.rglob("*.md")
        if path.is_file()
    )


if __name__ == "__main__":
    main()
