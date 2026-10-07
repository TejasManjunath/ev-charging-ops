"""Phase 0 go/no-go check.

Answers three questions before any real code gets written:
  1. Which operators are publishing live status right now, and how fresh is it?
  2. What does one live snapshot look like (structure, status values)?
  3. Does the historical archive download, and what is in it?

Run from the repo root:
    python -m evops.check_feeds                 # one recent day of archive
    python -m evops.check_feeds --week 2026-38  # a whole ISO week instead
    python -m evops.check_feeds --skip-live     # archive only

Writes a summary to reports/feed_check.md. Paste that file back into the chat.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from evops import config

CHUNK_ROWS = 500_000  # archive files are read in chunks so big files stay safe


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def http_get(url: str, stream: bool = False) -> requests.Response:
    resp = requests.get(
        url,
        headers={"User-Agent": config.USER_AGENT},
        timeout=config.TIMEOUT_S,
        stream=stream,
    )
    resp.raise_for_status()
    return resp


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def md_table(df: pd.DataFrame) -> str:
    """Small markdown table without the optional `tabulate` dependency."""
    if df.empty:
        return "_(empty)_"
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 1. Sources and freshness
# --------------------------------------------------------------------------- #
def check_sources(now: datetime) -> tuple[str, bool]:
    items = http_get(config.SOURCES_URL).json().get("items", [])
    rows = []
    for s in items:
        updated = parse_ts(s.get("realtime_data_updated_at"))
        age_min = round((now - updated).total_seconds() / 60) if updated else None
        rows.append(
            {
                "source": s.get("uid"),
                "name": s.get("name"),
                "realtime_status": s.get("realtime_status"),
                "realtime_age_min": age_min,
                "fresh": "yes"
                if age_min is not None and age_min <= config.STALE_AFTER_MIN
                else "no",
                "licence": s.get("attribution_license") or "not stated",
            }
        )
    df = pd.DataFrame(rows).sort_values("realtime_age_min", na_position="last")
    df["realtime_age_min"] = df["realtime_age_min"].astype("Int64")
    n_fresh = int((df["fresh"] == "yes").sum())
    ok = n_fresh >= 3
    text = (
        f"{n_fresh} of {len(df)} sources have live data newer than "
        f"{config.STALE_AFTER_MIN} minutes.\n\n{md_table(df)}"
    )
    return text, ok


# --------------------------------------------------------------------------- #
# 2. One live snapshot
# --------------------------------------------------------------------------- #
def structure(obj, depth: int = 0, max_depth: int = 5, indent: str = "") -> list[str]:
    """Outline of a JSON object: keys and types, first list element only."""
    lines: list[str] = []
    if depth > max_depth:
        return lines
    if isinstance(obj, dict):
        for k, v in list(obj.items())[:25]:
            kind = type(v).__name__
            extra = f" [{len(v)} items]" if isinstance(v, list) else ""
            lines.append(f"{indent}{k}: {kind}{extra}")
            lines += structure(v, depth + 1, max_depth, indent + "  ")
    elif isinstance(obj, list) and obj:
        lines += structure(obj[0], depth + 1, max_depth, indent)
    return lines


def count_status_values(obj, counter: Counter) -> None:
    """Count every string value stored under a key whose name contains 'status'."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if "status" in k.lower():
                if isinstance(v, str):
                    counter[(k, v)] += 1
                elif isinstance(v, dict) and isinstance(v.get("value"), str):
                    counter[(k, v["value"])] += 1
            count_status_values(v, counter)
    elif isinstance(obj, list):
        for item in obj:
            count_status_values(item, counter)


def check_live(now: datetime) -> tuple[str, bool]:
    resp = http_get(config.REALTIME_URL)
    raw = resp.content
    config.LIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = config.LIVE_DIR / f"realtime_{now:%Y%m%dT%H%M%SZ}.json.gz"
    out.write_bytes(gzip.compress(raw))

    data = json.loads(raw)
    counter: Counter = Counter()
    count_status_values(data, counter)
    status_df = pd.DataFrame(
        [{"field": k, "value": v, "count": c} for (k, v), c in counter.most_common(30)]
    )
    outline = "\n".join(structure(data)[:80])
    ok = bool(counter)
    text = (
        f"Downloaded {human_bytes(len(raw))}, saved to `{out.relative_to(config.ROOT)}`.\n\n"
        f"**Status values found:**\n\n{md_table(status_df)}\n\n"
        f"**JSON outline (first 80 lines):**\n\n```\n{outline}\n```"
    )
    return text, ok


# --------------------------------------------------------------------------- #
# 3. Historical archive
# --------------------------------------------------------------------------- #
def download(url: str, target: Path) -> int:
    if target.exists() and target.stat().st_size > 0:
        print(f"  already downloaded: {target.name}")
        return target.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    with http_get(url, stream=True) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        with open(tmp, "wb") as fh:
            for block in resp.iter_content(chunk_size=1 << 20):
                fh.write(block)
                done += len(block)
                if total:
                    print(f"\r  {human_bytes(done)} / {human_bytes(total)}", end="")
        print()
    tmp.replace(target)
    return target.stat().st_size


def find_column(columns, *needles: str) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for needle in needles:
        for low, original in lowered.items():
            if needle in low:
                return original
    return None


def summarise_archive(path: Path) -> tuple[str, bool]:
    n_rows = 0
    columns: list[str] = []
    dtypes: dict = {}
    sample = None
    per_source_day: Counter = Counter()
    status_counts: Counter = Counter()
    source_col = time_col = status_col = None

    for chunk in pd.read_csv(path, compression="gzip", chunksize=CHUNK_ROWS, low_memory=False):
        if sample is None:
            sample = chunk.head(3)
            columns = list(chunk.columns)
            dtypes = chunk.dtypes.astype(str).to_dict()
            source_col = find_column(columns, "source")
            time_col = find_column(columns, "datastore_updated_at", "updated", "timestamp", "time")
            status_col = find_column(columns, "status")
        n_rows += len(chunk)
        if source_col and time_col:
            days = pd.to_datetime(chunk[time_col], errors="coerce", utc=True).dt.date
            per_source_day.update(zip(chunk[source_col].fillna("unknown"), days))
        if status_col:
            status_counts.update(chunk[status_col].fillna("missing").astype(str))

    cols_df = pd.DataFrame({"column": columns, "dtype": [dtypes[c] for c in columns]})
    parts = [
        f"File size {human_bytes(path.stat().st_size)} compressed, {n_rows:,} rows.",
        f"Detected columns: source = `{source_col}`, time = `{time_col}`, status = `{status_col}`.",
        f"**Columns:**\n\n{md_table(cols_df)}",
        f"**First 3 rows:**\n\n{md_table(sample.astype(str)) if sample is not None else '_(none)_'}",
    ]
    if per_source_day:
        psd = pd.DataFrame(
            [{"source": s, "day": d, "rows": n} for (s, d), n in per_source_day.items()]
        )
        pivot = psd.pivot_table(
            index="source", columns="day", values="rows", aggfunc="sum", fill_value=0
        ).astype(int)
        pivot.columns = [str(c) for c in pivot.columns]
        parts.append(f"**Rows per source per day:**\n\n{md_table(pivot.reset_index())}")
    if status_counts:
        sc = pd.DataFrame(status_counts.most_common(15), columns=["status", "rows"])
        parts.append(f"**Status values:**\n\n{md_table(sc)}")
    return "\n\n".join(parts), n_rows > 0


def check_archive(week: str | None) -> tuple[str, bool]:
    if week:
        year, wk = (int(x) for x in week.split("-"))
        candidates = [(f"week {year}-{wk:02d}", config.archive_week_url(year, wk), f"week_{year}{wk:02d}.csv.gz")]
    else:
        # Try recent days, newest first; today's file is usually not ready yet.
        candidates = []
        for back in range(2, 9):
            d = (date.today() - timedelta(days=back)).isoformat()
            candidates.append((f"day {d}", config.archive_day_url(d), f"day_{d.replace('-', '')}.csv.gz"))

    errors = []
    for label, url, fname in candidates:
        print(f"  trying {label}: {url}")
        try:
            download(url, config.ARCHIVE_DIR / fname)
        except requests.HTTPError as exc:
            errors.append(f"{label}: HTTP {exc.response.status_code}")
            continue
        summary, ok = summarise_archive(config.ARCHIVE_DIR / fname)
        return f"Loaded {label} from `{url}`.\n\n{summary}", ok

    return "No archive file could be downloaded.\n\n" + "\n".join(f"- {e}" for e in errors), False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--week", help="ISO week to download instead of one day, e.g. 2026-38")
    ap.add_argument("--skip-live", action="store_true", help="skip the live snapshot")
    ap.add_argument("--skip-archive", action="store_true", help="skip the archive download")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    sections: list[tuple[str, str, bool | None]] = []

    steps = [("1. Sources and freshness", lambda: check_sources(now))]
    if not args.skip_live:
        steps.append(("2. Live snapshot", lambda: check_live(now)))
    if not args.skip_archive:
        steps.append(("3. Historical archive", lambda: check_archive(args.week)))

    for title, fn in steps:
        print(f"\n{title} ...")
        try:
            text, ok = fn()
        except Exception as exc:  # report every failure instead of stopping
            text, ok = f"**Failed:** `{type(exc).__name__}: {exc}`", False
        print("  OK" if ok else "  PROBLEM")
        sections.append((title, text, ok))

    verdict = "GO" if all(ok for _, _, ok in sections) else "CHECK THE PROBLEMS BELOW"
    report = [f"# Feed check, {now:%Y-%m-%d %H:%M} UTC", f"**Verdict: {verdict}**"]
    for title, text, ok in sections:
        report.append(f"## {title} — {'OK' if ok else 'PROBLEM'}\n\n{text}")
    report.append(f"---\n{config.ATTRIBUTION}")

    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = config.REPORTS_DIR / "feed_check.md"
    out.write_text("\n\n".join(report), encoding="utf-8")
    print(f"\nVerdict: {verdict}\nReport written to {out}")
    return 0 if verdict == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())
