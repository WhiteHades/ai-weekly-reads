# AI Weekly Continuity

Small Rust CLI for per-lane digest/article continuity.

It stores lane-local publication history in a JSON ledger. Other lanes can publish in between without changing a lane's previous/next chain.

## Commands

Print the latest lane context:

```bash
cargo run --manifest-path tools/continuity/Cargo.toml -- context --lane theology --limit 3
```

Append or update an entry:

```bash
cargo run --manifest-path tools/continuity/Cargo.toml -- add \
  --lane theology \
  --kind daily \
  --title "Why the argument matters" \
  --path "public/daily/2026-07-09-why-the-argument-matters.md" \
  --published 2026-07-09 \
  --summary "Introduces the problem and leaves the historical angle for later." \
  --source "Knowledge/02_sources/example.md" \
  --next-question "How did later authors handle the same premise?"
```

## Rules

- `published` is stored as `YYYY-MM-DD`.
- Entries are ordered per lane by `published`, then `sequence`.
- Adding the same entry ID updates the entry instead of duplicating it.
- `previous_id` and `next_id` are recalculated only inside the same lane.
