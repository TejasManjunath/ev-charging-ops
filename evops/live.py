"""Phase 2: the live state of every station, in the same shape as the archive.

Source: MobiData BW's GeoServer layer "charge_points", the layer the daily
archive is a snapshot of. One request returns every station (about 100,000
rows, 23 MB), so there is no paging. We drop the federal register's static
rows and the Swiss stations, which leaves about 36,000 German stations with
live status.

Two timestamps exist, and they mean different things:
- status_last_updated: when the station's status last CHANGED. A charger
  that has been "available" for six hours has an old timestamp but is not
  stale.
- realtime_data_outdated: the platform's own flag that the operator's feed
  has stopped updating. This is the staleness signal.

Run from the repo root:
    python -m evops.live            # fetch, summarise, save data/live/latest.parquet
"""

from __future__ import annotations

import io
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from evops import config
from evops.ingest import EXCLUDED_SOURCES, STATUS_COLS, parse_coordinates

WFS_URL = os.getenv(
    "EVOPS_WFS_BASE",
    "https://api.mobidata-bw.de/geoserver/MobiData-BW/ows"
    "?service=WFS&version=2.0.0&request=GetFeature&typeName=MobiData-BW%3Acharge_points",
)
LIVE_DIR = config.DATA_DIR / "live"
LATEST_PATH = LIVE_DIR / "latest.parquet"

# Ask the server to skip static and Swiss rows (smaller download). If the
# server ignores the filter, the same rows are dropped locally below.
CQL_FILTER = " AND ".join(f"source <> '{s}'" for s in sorted(EXCLUDED_SOURCES))

LIVE_STATES = ["available", "charging", "reserved", "out_of_order", "inoperative", "unknown"]


def download(use_filter: bool = True) -> tuple[pd.DataFrame, float, int]:
    params = {"outputFormat": "csv"}
    if use_filter:
        params["CQL_FILTER"] = CQL_FILTER
    t0 = time.time()
    r = requests.get(WFS_URL, params=params, headers={"User-Agent": config.USER_AGENT},
                     timeout=config.TIMEOUT_S)
    r.raise_for_status()
    if not r.content.lstrip().startswith(b"FID"):
        # GeoServer answers errors with XML and HTTP 200
        raise ValueError(f"Not a CSV response: {r.text[:200]}")
    df = pd.read_csv(io.BytesIO(r.content), low_memory=False)
    return df, time.time() - t0, len(r.content)


def fetch_raw() -> tuple[pd.DataFrame, dict]:
    """Download the live layer; fall back to an unfiltered download if needed."""
    try:
        df, secs, size = download(use_filter=True)
        used_filter = True
    except (requests.HTTPError, ValueError):
        df, secs, size = download(use_filter=False)
        used_filter = False
    meta = {"seconds": round(secs, 1), "megabytes": round(size / 1e6, 1),
            "rows_downloaded": len(df), "server_filter": used_filter}
    return df, meta


def to_frames(raw: pd.DataFrame, fetched_at: datetime) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (status, stations), matching the archive's processed format."""
    df = raw[~raw["source"].isin(EXCLUDED_SOURCES)].copy()
    if "station_id" not in df.columns:
        df["station_id"] = df["id"]          # same quirk as pre-June archive files

    status = pd.DataFrame({
        "station_id": df["station_id"].astype("int32"),
        "source": df["source"].astype("category"),
        "fetched_at": pd.Timestamp(fetched_at),
        "status_changed_at": pd.to_datetime(df.get("status_last_updated"), utc=True,
                                            errors="coerce", format="mixed"),
    })
    for raw_col, clean in STATUS_COLS.items():
        status[clean] = pd.to_numeric(df[raw_col], errors="coerce").fillna(0).astype("int16")
    flag = df.get("realtime_data_outdated", pd.Series(pd.NA, index=df.index)).astype(str).str.lower()
    status["outdated"] = flag.map({"true": True, "false": False}).astype("boolean")
    status["live_points"] = status[LIVE_STATES].sum(axis=1).astype("int16")
    status["out_of_service"] = (status["out_of_order"] + status["inoperative"]).astype("int16")
    status = status[status["live_points"] > 0].reset_index(drop=True)

    stations = df[["station_id", "source", "name", "operator_name", "address", "postal_code",
                   "city", "max_electric_power"]].copy()
    stations["station_id"] = stations["station_id"].astype("int32")
    stations["postal_code"] = (stations["postal_code"].astype(str)
                               .str.replace(r"\.0$", "", regex=True).replace("nan", None))
    stations = stations.join(parse_coordinates(df["geometry"]).set_axis(stations.index))
    stations = stations[stations["station_id"].isin(status["station_id"])].reset_index(drop=True)
    return status, stations


def fetch_live() -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """One call for the dashboard: (status, stations, meta)."""
    fetched_at = datetime.now(timezone.utc)
    raw, meta = fetch_raw()
    status, stations = to_frames(raw, fetched_at)
    meta.update({"fetched_at": fetched_at.isoformat(timespec="seconds"),
                 "stations": len(status), "live_points": int(status["live_points"].sum())})
    return status, stations, meta


def main() -> int:
    status, stations, meta = fetch_live()
    print(f"Fetched {meta['rows_downloaded']:,} rows ({meta['megabytes']} MB) in {meta['seconds']}s, "
          f"server-side filter {'worked' if meta['server_filter'] else 'not used'}")
    print(f"{meta['stations']:,} German stations with live status, {meta['live_points']:,} charge points\n")

    totals = status[LIVE_STATES].sum()
    for state in LIVE_STATES:
        print(f"  {state:<13}{totals[state]:>9,}  ({totals[state] / totals.sum():.1%})")
    print(f"\n  feed flagged outdated: {status['outdated'].mean():.1%} of stations")

    worst = (status[status["out_of_service"] > 0]
             .merge(stations[["station_id", "operator_name", "city"]], on="station_id")
             .sort_values(["out_of_service", "live_points"], ascending=False).head(10))
    print("\nMost charge points out of service right now:")
    for r in worst.itertuples():
        print(f"  {r.out_of_service:>3} of {r.live_points:<3} {r.operator_name} · {r.city}")

    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    status.merge(stations, on=["station_id", "source"], how="left").to_parquet(LATEST_PATH, index=False)
    print(f"\nSaved {LATEST_PATH} (overwritten each run)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
