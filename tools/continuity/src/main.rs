use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};

use anyhow::{Context, Result, bail};
use clap::{Parser, Subcommand, ValueEnum};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use time::format_description::FormatItem;
use time::format_description::well_known::Rfc3339;
use time::macros::format_description;
use time::{Date, OffsetDateTime};

const DATE_FORMAT: &[FormatItem<'_>] = format_description!("[year]-[month]-[day]");

#[derive(Debug, Parser)]
#[command(version, about = "Manage per-lane continuity for AI Weekly Reads", long_about = None)]
struct Cli {
    #[arg(long, default_value = "output/_metadata/continuity.json")]
    ledger: PathBuf,

    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Print compact continuity context for a lane.
    Context {
        #[arg(long)]
        lane: String,

        #[arg(long, default_value_t = 3)]
        limit: usize,

        /// Include only entries published before this date or timestamp.
        #[arg(long)]
        before: Option<String>,
    },

    /// Append or update an entry in a lane.
    Add {
        #[arg(long)]
        lane: String,

        #[arg(long)]
        title: String,

        #[arg(long)]
        kind: EntryKind,

        #[arg(long)]
        path: String,

        /// Published date or RFC3339 timestamp. Defaults to today's UTC date.
        #[arg(long)]
        published: Option<String>,

        #[arg(long)]
        summary: String,

        #[arg(long)]
        id: Option<String>,

        #[arg(long = "source")]
        sources: Vec<String>,

        #[arg(long = "next-question")]
        next_questions: Vec<String>,
    },
}

#[derive(Clone, Debug, Serialize, Deserialize, ValueEnum)]
#[serde(rename_all = "kebab-case")]
enum EntryKind {
    Daily,
    FridaySpecial,
    Manual,
}

impl std::fmt::Display for EntryKind {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let value = match self {
            Self::Daily => "daily",
            Self::FridaySpecial => "friday-special",
            Self::Manual => "manual",
        };
        formatter.write_str(value)
    }
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Ledger {
    #[serde(default)]
    lanes: BTreeMap<String, Lane>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Lane {
    #[serde(default)]
    entries: Vec<Entry>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct Entry {
    id: String,
    sequence: u64,
    lane: String,
    kind: EntryKind,
    title: String,
    path: String,
    published: String,
    summary: String,
    #[serde(default)]
    sources: Vec<String>,
    #[serde(default)]
    previous_id: String,
    #[serde(default)]
    next_id: String,
    #[serde(default)]
    next_questions: Vec<String>,
    created: String,
    updated: String,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let mut ledger = read_ledger(&cli.ledger)?;

    match cli.command {
        Command::Context {
            lane,
            limit,
            before,
        } => {
            let context = context_for_lane(&mut ledger, &lane, limit, before.as_deref())?;
            print!("{context}");
        }
        Command::Add {
            lane,
            title,
            kind,
            path,
            published,
            summary,
            id,
            sources,
            next_questions,
        } => {
            let entry = add_entry(
                &mut ledger,
                AddEntry {
                    lane,
                    title,
                    kind,
                    path,
                    published,
                    summary,
                    id,
                    sources,
                    next_questions,
                },
            )?;
            let id = entry.id.clone();
            write_ledger(&cli.ledger, &ledger)?;
            println!("{id}");
        }
    }

    Ok(())
}

#[derive(Debug)]
struct AddEntry {
    lane: String,
    title: String,
    kind: EntryKind,
    path: String,
    published: Option<String>,
    summary: String,
    id: Option<String>,
    sources: Vec<String>,
    next_questions: Vec<String>,
}

fn read_ledger(path: &Path) -> Result<Ledger> {
    if !path.exists() {
        return Ok(Ledger::default());
    }

    let file = File::open(path).with_context(|| format!("failed to open {}", path.display()))?;
    let reader = BufReader::new(file);
    serde_json::from_reader(reader).with_context(|| format!("failed to parse {}", path.display()))
}

fn write_ledger(path: &Path, ledger: &Ledger) -> Result<()> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("failed to create {}", parent.display()))?;
    }

    let tmp_path = path.with_extension("json.tmp");
    {
        let file = File::create(&tmp_path)
            .with_context(|| format!("failed to create {}", tmp_path.display()))?;
        let mut writer = BufWriter::new(file);
        serde_json::to_writer_pretty(&mut writer, ledger)
            .with_context(|| format!("failed to write {}", tmp_path.display()))?;
        writer.write_all(b"\n")?;
        writer.flush()?;
    }
    fs::rename(&tmp_path, path).with_context(|| {
        format!(
            "failed to replace {} with {}",
            path.display(),
            tmp_path.display()
        )
    })?;
    Ok(())
}

fn context_for_lane(
    ledger: &mut Ledger,
    lane_name: &str,
    limit: usize,
    before: Option<&str>,
) -> Result<String> {
    let Some(lane) = ledger.lanes.get_mut(lane_name) else {
        return Ok(format!(
            "# Continuity: {lane_name}\n\nNo previous entries in this lane.\n"
        ));
    };

    refresh_lane_links(lane)?;
    let before_date = before.map(parse_date).transpose()?;
    let mut entries: Vec<&Entry> = lane
        .entries
        .iter()
        .filter(|entry| {
            before_date.is_none_or(|date| entry_date(entry).is_ok_and(|published| published < date))
        })
        .collect();
    entries.sort_by_key(|entry| (entry_date(entry).unwrap_or(Date::MIN), entry.sequence));

    let selected = entries.into_iter().rev().take(limit).collect::<Vec<_>>();
    if selected.is_empty() {
        return Ok(format!(
            "# Continuity: {lane_name}\n\nNo previous entries in this lane.\n"
        ));
    }

    let mut lines = vec![format!("# Continuity: {lane_name}"), String::new()];
    for entry in selected.into_iter().rev() {
        lines.push(format!("## {}", entry.title));
        lines.push(format!("- id: {}", entry.id));
        lines.push(format!("- sequence: {}", entry.sequence));
        lines.push(format!("- kind: {}", entry.kind));
        lines.push(format!("- published: {}", entry.published));
        if !entry.previous_id.is_empty() {
            lines.push(format!("- previous: {}", entry.previous_id));
        }
        if !entry.next_id.is_empty() {
            lines.push(format!("- next: {}", entry.next_id));
        }
        if !entry.path.is_empty() {
            lines.push(format!("- path: {}", entry.path));
        }
        if !entry.summary.is_empty() {
            lines.push(format!("- summary: {}", entry.summary));
        }
        if !entry.next_questions.is_empty() {
            lines.push("- next questions:".to_string());
            for question in &entry.next_questions {
                lines.push(format!("  - {question}"));
            }
        }
        lines.push(String::new());
    }

    Ok(format!("{}\n", lines.join("\n").trim_end()))
}

fn add_entry(ledger: &mut Ledger, input: AddEntry) -> Result<&Entry> {
    if input.lane.trim().is_empty() {
        bail!("lane must not be empty");
    }
    if input.title.trim().is_empty() {
        bail!("title must not be empty");
    }
    if input.path.trim().is_empty() {
        bail!("path must not be empty");
    }

    let published = normalize_published(input.published.as_deref())?;
    let now = now_timestamp()?;
    let id = input
        .id
        .unwrap_or_else(|| stable_entry_id(&input.lane, &input.kind, &input.path, &input.title));
    let lane = ledger.lanes.entry(input.lane.clone()).or_default();
    let sequence = lane
        .entries
        .iter()
        .find(|entry| entry.id == id)
        .map_or_else(|| next_sequence(lane), |entry| entry.sequence);
    let created = lane
        .entries
        .iter()
        .find(|entry| entry.id == id)
        .map_or_else(|| now.clone(), |entry| entry.created.clone());

    let entry = Entry {
        id: id.clone(),
        sequence,
        lane: input.lane,
        kind: input.kind,
        title: input.title,
        path: input.path,
        published,
        summary: input.summary,
        sources: clean_values(input.sources),
        previous_id: String::new(),
        next_id: String::new(),
        next_questions: clean_values(input.next_questions),
        created,
        updated: now,
    };

    if let Some(index) = lane.entries.iter().position(|existing| existing.id == id) {
        lane.entries[index] = entry;
    } else {
        lane.entries.push(entry);
    }
    refresh_lane_links(lane)?;
    lane.entries
        .iter()
        .find(|entry| entry.id == id)
        .context("entry disappeared after insertion")
}

fn refresh_lane_links(lane: &mut Lane) -> Result<()> {
    lane.entries
        .sort_by_key(|entry| (entry_date(entry).unwrap_or(Date::MIN), entry.sequence));

    let ids = lane
        .entries
        .iter()
        .map(|entry| entry.id.clone())
        .collect::<Vec<_>>();
    for (index, entry) in lane.entries.iter_mut().enumerate() {
        entry.previous_id = index
            .checked_sub(1)
            .and_then(|previous| ids.get(previous))
            .cloned()
            .unwrap_or_default();
        entry.next_id = ids.get(index + 1).cloned().unwrap_or_default();
    }
    Ok(())
}

fn clean_values(values: Vec<String>) -> Vec<String> {
    values
        .into_iter()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .collect()
}

fn next_sequence(lane: &Lane) -> u64 {
    lane.entries
        .iter()
        .map(|entry| entry.sequence)
        .max()
        .unwrap_or(0)
        + 1
}

fn stable_entry_id(lane: &str, kind: &EntryKind, path: &str, title: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!("{lane}:{kind}:{path}:{title}"));
    let digest = hasher.finalize();
    format!("{digest:x}").chars().take(16).collect()
}

fn normalize_published(value: Option<&str>) -> Result<String> {
    let date = match value.map(str::trim).filter(|value| !value.is_empty()) {
        Some(value) => parse_date(value)?,
        None => OffsetDateTime::now_utc().date(),
    };
    date.format(DATE_FORMAT)
        .context("failed to format published date")
}

fn entry_date(entry: &Entry) -> Result<Date> {
    parse_date(&entry.published)
}

fn parse_date(value: &str) -> Result<Date> {
    let trimmed = value.trim();
    if let Ok(date) = Date::parse(trimmed, DATE_FORMAT) {
        return Ok(date);
    }
    OffsetDateTime::parse(trimmed, &Rfc3339)
        .map(|datetime| datetime.date())
        .with_context(|| format!("invalid date or RFC3339 timestamp: {trimmed}"))
}

fn now_timestamp() -> Result<String> {
    OffsetDateTime::now_utc()
        .format(&Rfc3339)
        .context("failed to format current timestamp")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn links_are_lane_local_and_date_sorted() {
        let mut ledger = Ledger::default();
        add_entry(
            &mut ledger,
            AddEntry {
                lane: "theology".into(),
                title: "Part two".into(),
                kind: EntryKind::Daily,
                path: "theology-2.md".into(),
                published: Some("2026-07-13".into()),
                summary: "second".into(),
                id: Some("theology-2".into()),
                sources: vec![],
                next_questions: vec![],
            },
        )
        .unwrap();
        add_entry(
            &mut ledger,
            AddEntry {
                lane: "technology".into(),
                title: "Other lane".into(),
                kind: EntryKind::Daily,
                path: "tech.md".into(),
                published: Some("2026-07-10".into()),
                summary: "between theology parts".into(),
                id: Some("tech-1".into()),
                sources: vec![],
                next_questions: vec![],
            },
        )
        .unwrap();
        add_entry(
            &mut ledger,
            AddEntry {
                lane: "theology".into(),
                title: "Part one".into(),
                kind: EntryKind::Daily,
                path: "theology-1.md".into(),
                published: Some("2026-07-09".into()),
                summary: "first".into(),
                id: Some("theology-1".into()),
                sources: vec![],
                next_questions: vec![],
            },
        )
        .unwrap();

        let theology = ledger.lanes.get("theology").unwrap();
        assert_eq!(theology.entries[0].id, "theology-1");
        assert_eq!(theology.entries[0].next_id, "theology-2");
        assert_eq!(theology.entries[1].previous_id, "theology-1");
        assert_eq!(theology.entries[1].next_id, "");

        let technology = ledger.lanes.get("technology").unwrap();
        assert_eq!(technology.entries[0].previous_id, "");
        assert_eq!(technology.entries[0].next_id, "");
    }
}
