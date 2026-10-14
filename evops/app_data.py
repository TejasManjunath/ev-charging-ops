"""Phase 2: pack what the dashboard needs from the archive into app_data/.

The archive (data/processed/, ~100 MB) stays on your machine and out of git.
The dashboard only needs small summaries, written here to app_data/ (a few MB),
which IS committed, so the deployed app works without the archive.

Writes:
  app_data/daily_summary.parquet   network-wide state shares per day (from the notebook)
  app_data/station_days.parquet    per station and day, last 14 archive days:
                                   share of charge points available / out of service / unknown
  app_data/meta.json               model window and the archive's date range

Run from the repo root after ingesting new days:
    python -m evops.app_data
"""

from __future__ import annotations

import json
import shutil
import sys

import pandas as pd

from evops import config

DAYS = 14
LIVE = ["available", "charging", "reserved", "out_of_order", "inoperative", "unknown"]
APP_DATA = config.ROOT / "app_data"
STATUS_DIR = config.PROCESSED_DIR / "status"


def day_of(path):
    return pd.Timestamp(path.stem.split("=")[1])


def station_days(files) -> pd.DataFrame:
    parts = []
    for path in files:
        df = pd.read_parquet(path, columns=["station_id", *LIVE])
        df["live"] = df[LIVE].sum(axis=1)
        df = df[df["live"] > 0]
        df["out_of_service"] = df["out_of_order"] + df["inoperative"]
        g = df.groupby("station_id")
        out = pd.DataFrame({
            "available_share": g["available"].sum() / g["live"].sum(),
            "out_of_service_share": g["out_of_service"].sum() / g["live"].sum(),
            "unknown_share": g["unknown"].sum() / g["live"].sum(),
            "any_out_of_service": g["out_of_service"].max() > 0,
        }).reset_index()
        out["day"] = day_of(path)
        parts.append(out)
    sd = pd.concat(parts, ignore_index=True)
    for col in ("available_share", "out_of_service_share", "unknown_share"):
        sd[col] = sd[col].astype("float32").round(3)
    sd["station_id"] = sd["station_id"].astype("int32")
    return sd


def main() -> int:
    files = sorted(STATUS_DIR.glob("date=*.parquet"))
    if not files:
        print(f"No archive files in {STATUS_DIR}. Run python -m evops.ingest first.")
        return 1
    APP_DATA.mkdir(exist_ok=True)

    recent = files[-DAYS:]
    sd = station_days(recent)
    sd.to_parquet(APP_DATA / "station_days.parquet", index=False, compression="zstd")
    print(f"station_days: {len(sd):,} rows, {sd['station_id'].nunique():,} stations, "
          f"{day_of(recent[0]).date()} to {day_of(recent[-1]).date()}")

    summary = config.PROCESSED_DIR / "daily_summary.parquet"
    if summary.exists():
        shutil.copyfile(summary, APP_DATA / "daily_summary.parquet")
        print("daily_summary: copied")
    else:
        print("daily_summary: missing, run notebooks/01_data_quality.ipynb first")

    window_path = config.PROCESSED_DIR / "model_window.json"
    meta_path = APP_DATA / "meta.json"
    # keep keys written by other steps (e.g. the early-warning forecast)
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta |= {
        "archive_first_day": str(day_of(files[0]).date()),
        "archive_last_day": str(day_of(files[-1]).date()),
        "station_days_from": str(day_of(recent[0]).date()),
        "model_window": json.loads(window_path.read_text()) if window_path.exists() else None,
    }
    meta_path.write_text(json.dumps(meta, indent=2))

    total = sum(p.stat().st_size for p in APP_DATA.iterdir()) / 1e6
    print(f"app_data/ written: {total:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
