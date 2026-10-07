"""Phase 1: download the daily archive and turn it into compact Parquet.

For each day it downloads the CSV (10-300 MB) into memory, keeps the status
counts, drops Swiss and register-only stations, and writes ~1-5 MB of Parquet.
Nothing raw touches the disk unless you pass --keep-raw. Raw files left over
from earlier runs are reused and then deleted. It also keeps a station table
(name, operator, location, power).

Run from the repo root:
    python -m evops.ingest --days 7                          # last 7 complete days
    python -m evops.ingest --start 2026-08-10 --end 2026-10-02
    python -m evops.ingest --days 7 --keep-raw               # keep the CSVs too

Already-converted days are skipped, so you can stop and resume any time.

What the archive is (checked on the 2026-10-02 file):
- One row = one station at one snapshot, with counts per status.
- How often a station is captured changes over time: ~90 times a day in early
  August 2026, ~10 a day from mid-August to 27 September, ~26 a day (about
  hourly) from 28 September. data/processed/manifest.csv records it per day.
- Station IDs are stable across days.
- Use `datastore_updated_at` as the snapshot time. The columns
  `status_last_updated` and `last_updated` are stamped at export time,
  not at snapshot time, so they are ignored.
- Coverage is Germany-wide (plus Swiss stations, which are dropped).
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from datetime import date, timedelta

import pandas as pd
import requests

from evops import config
from evops.check_feeds import download

STATUS_COLS = {
    "chargepoint_available_count": "available",
    "chargepoint_charging_count": "charging",
    "chargepoint_reserved_count": "reserved",
    "chargepoint_outoforder_count": "out_of_order",
    "chargepoint_inoperative_count": "inoperative",
    "chargepoint_unknown_count": "unknown",
    "chargepoint_static_count": "static",
}
STATION_COLS = [
    "station_id", "source", "name", "operator_name", "address",
    "postal_code", "city", "geometry", "max_electric_power",
]
# opendata_swiss: Swiss stations, out of scope.
# bnetza_api: the federal register; static records only, no live status. On
# 2026-09-14/15 it dumped ~58,000 static rows into the archive.
EXCLUDED_SOURCES = {"opendata_swiss", "bnetza_api"}
STATUS_DIR = config.PROCESSED_DIR / "status"
STATIONS_PATH = config.PROCESSED_DIR / "stations.parquet"
MANIFEST_PATH = config.PROCESSED_DIR / "manifest.csv"

# Germany's bounding box, used to catch swapped or broken coordinates.
DE_LAT = (47.0, 55.2)
DE_LON = (5.8, 15.1)


def day_path(day: date):
    return STATUS_DIR / f"date={day.isoformat()}.parquet"


def to_status_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "ts": pd.to_datetime(df["datastore_updated_at"], utc=True, format="mixed"),
            "station_id": df["station_id"].astype("int32"),
            "source": df["source"].astype("category"),
        }
    )
    for raw, clean in STATUS_COLS.items():
        out[clean] = pd.to_numeric(df[raw], errors="coerce").fillna(0).astype("int16")
    # `realtime_data_outdated` only exists in files from late September 2026.
    if "realtime_data_outdated" in df.columns:
        flag = df["realtime_data_outdated"].astype(str).str.lower()
        out["outdated"] = flag.map({"true": True, "false": False}).astype("boolean")
    else:
        out["outdated"] = pd.array([pd.NA] * len(df), dtype="boolean")
    return out.sort_values(["station_id", "ts"]).reset_index(drop=True)


def parse_coordinates(geometry: pd.Series) -> pd.DataFrame:
    xy = geometry.astype(str).str.extract(r"POINT \(([-\d.]+) ([-\d.]+)\)").astype(float)
    lon, lat = xy[0], xy[1]

    def inside(la, lo):
        return la.between(*DE_LAT) & lo.between(*DE_LON)

    ok = inside(lat, lon)
    swapped = ~ok & inside(lon, lat)
    out = pd.DataFrame({"lat": lat.where(ok), "lon": lon.where(ok)})
    out.loc[swapped, "lat"] = lon[swapped]
    out.loc[swapped, "lon"] = lat[swapped]
    out["coord_fixed"] = swapped
    return out


def to_station_frame(df: pd.DataFrame, day: date) -> pd.DataFrame:
    st = df[STATION_COLS].drop_duplicates("station_id", keep="last").copy()
    st = st.join(parse_coordinates(st.pop("geometry")).set_axis(st.index))
    st["postal_code"] = (
        st["postal_code"].astype(str).str.replace(r"\.0$", "", regex=True).replace("nan", None)
    )
    st["station_id"] = st["station_id"].astype("int32")
    st["first_seen"] = pd.Timestamp(day)
    st["last_seen"] = pd.Timestamp(day)
    return st


def update_stations(new: pd.DataFrame) -> None:
    """Merge one day's stations into the station table.

    Attributes come from the most recent day seen; first_seen is the earliest.
    """
    if STATIONS_PATH.exists():
        old = pd.read_parquet(STATIONS_PATH)
        both = pd.concat([old, new], ignore_index=True)
        first = both.groupby("station_id")["first_seen"].min()
        latest = both.sort_values("last_seen").drop_duplicates("station_id", keep="last")
        merged = latest.drop(columns="first_seen").merge(first, on="station_id")
    else:
        merged = new
    merged.to_parquet(STATIONS_PATH, index=False)


def update_manifest(day: date, status: pd.DataFrame) -> None:
    """One row per day: how many stations and how often each was captured.

    Capture frequency changes over the archive's life (from every ~15 minutes
    to every ~2.5 hours), so this file decides which days are fit for models.
    """
    n_stations = status["station_id"].nunique()
    row = pd.DataFrame(
        [{
            "day": day.isoformat(),
            "rows": len(status),
            "stations": n_stations,
            "snapshots_per_station": round(len(status) / max(n_stations, 1), 1),
            "sources": status["source"].nunique(),
            "has_outdated_flag": bool(status["outdated"].notna().any()),
        }]
    )
    if MANIFEST_PATH.exists():
        old = pd.read_csv(MANIFEST_PATH)
        row = pd.concat([old[old["day"] != day.isoformat()], row], ignore_index=True)
    row.sort_values("day").to_csv(MANIFEST_PATH, index=False)


def fetch_to_memory(url: str) -> io.BytesIO:
    """Download into RAM instead of onto disk, to spare the SSD."""
    resp = requests.get(url, headers={"User-Agent": config.USER_AGENT}, timeout=config.TIMEOUT_S)
    resp.raise_for_status()
    return io.BytesIO(resp.content)


def ingest_day(day: date, keep_raw: bool, force: bool = False) -> str:
    if day_path(day).exists() and not force:
        return "skipped (done)"
    raw = config.ARCHIVE_DIR / f"day_{day:%Y%m%d}.csv.gz"
    if raw.exists() and raw.stat().st_size > 0:
        source = raw                      # left over from an earlier run: reuse it
    elif keep_raw:
        download(config.archive_day_url(day.isoformat()), raw)
        source = raw
    else:
        source = fetch_to_memory(config.archive_day_url(day.isoformat()))

    # Files before 2026-06-19 have no `station_id`; their `id` holds the same value.
    usecols = (
        list(STATUS_COLS)
        + ["datastore_updated_at", "realtime_data_outdated", "id"]
        + STATION_COLS
    )
    df = pd.read_csv(source, compression="gzip", usecols=lambda c: c in usecols, low_memory=False)
    if "station_id" not in df.columns:
        df["station_id"] = df["id"]
    df = df[~df["source"].isin(EXCLUDED_SOURCES)]

    status = to_status_frame(df)
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = day_path(day).with_suffix(".part")
    status.to_parquet(tmp, index=False, compression="zstd")
    tmp.replace(day_path(day))
    update_stations(to_station_frame(df, day))
    update_manifest(day, status)

    if not keep_raw:
        raw.unlink(missing_ok=True)
    n = status["station_id"].nunique()
    return f"{len(status):,} rows, {n:,} stations, {len(status) / max(n, 1):.1f} snapshots/station"


def date_range(args) -> list[date]:
    if args.start:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else date.today() - timedelta(days=2)
    else:
        end = date.today() - timedelta(days=2)  # newest file that is reliably complete
        start = end - timedelta(days=args.days - 1)
    first = date.fromisoformat(config.ARCHIVE_START)
    start = max(start, first)
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="number of recent complete days (default 7)")
    ap.add_argument("--start", help="first day, YYYY-MM-DD (overrides --days)")
    ap.add_argument("--end", help="last day, YYYY-MM-DD (default: two days ago)")
    ap.add_argument("--keep-raw", action="store_true", help="keep the downloaded CSV files")
    ap.add_argument("--force", action="store_true", help="re-process days that are already done")
    args = ap.parse_args(argv)

    days = date_range(args)
    print(f"Ingesting {len(days)} day(s): {days[0]} to {days[-1]}")
    failures = []
    for i, day in enumerate(days, 1):
        t0 = time.time()
        try:
            msg = ingest_day(day, args.keep_raw, args.force)
        except requests.HTTPError as exc:
            msg = f"FAILED: HTTP {exc.response.status_code}"
            failures.append(day)
        except Exception as exc:  # keep going; report at the end
            msg = f"FAILED: {type(exc).__name__}: {exc}"
            failures.append(day)
        print(f"[{i}/{len(days)}] {day}: {msg} ({time.time() - t0:.0f}s)")

    print(f"\nDone. {len(days) - len(failures)} ok, {len(failures)} failed.")
    if failures:
        print("Failed days (re-run the same command to retry):", ", ".join(map(str, failures)))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
