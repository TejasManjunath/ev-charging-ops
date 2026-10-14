"""Phase 3, first attempt: silent-failure detection. Kept as a documented negative result.

RESULT (Oct 2026): did not validate, so it is not used in the dashboard. Idle spells the
method scored as very unlikely ended with someone charging 84-87% of the time, the same as
ordinary idle spells (83%), and flagged stations were no more likely to report an outage
the following week than similar unflagged ones. Without repair records there is no way to
tell a silent failure from a quiet day. See the README's decisions log; the project moved
to evops/early_warning.py instead.

A "silent failure" is a station whose feed says everything is fine (no charge
point out of service) while nobody charges there for far longer than its own
usual pattern makes plausible. Typical causes are a broken payment terminal,
a blocked bay or a faulty cable: problems the status feed never reports.

Method
1. hourly():   per station and hour (Berlin time): in use?  status OK?  outage?
               Plus how often its reported state changed, used to spot frozen feeds.
2. profiles(): each station's probability of being in use at each hour of the
               week (Mon 00:00 ... Sun 23:00), learned on a training period and
               shrunk towards the station's overall rate when data is thin.
3. idle_spells(): runs of consecutive hours with status OK but no charging. Each
               spell gets a surprise score, the -log10 probability of seeing that
               many idle hours if the station behaved normally:
                   score = sum over the spell's hours of -log10(1 - p_hour)
               A score of 3 means "about a 1-in-1,000 chance under normal use".
               Hours are not truly independent, so the threshold is calibrated
               on data rather than read as an exact probability.

Frozen feeds are excluded: if no station of a source changed state all day,
the source is treated as frozen for that day, because an unchanging feed looks
exactly like an idle charger.

Run from the repo root:
    python -m evops.silent_failure
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from evops import config

STATUS_DIR = config.PROCESSED_DIR / "status"
OUT_DIR = config.PROCESSED_DIR / "anomaly"
TZ = "Europe/Berlin"
COUNTS = ["available", "charging", "reserved", "out_of_order", "inoperative", "unknown"]
PRIOR_STRENGTH = 4      # pseudo-observations pulling a sparse hour-of-week cell to the station average
MAX_GAP_H = 3           # missing hours tolerated inside one idle spell


# --------------------------------------------------------------------------- #
# 1. Hourly table
# --------------------------------------------------------------------------- #
def hourly_one_day(path: Path, panel_ids: np.ndarray) -> pd.DataFrame:
    df = pd.read_parquet(path, columns=["ts", "station_id", "source", *COUNTS])
    df = df[df["station_id"].isin(panel_ids)]
    live = df[COUNTS].sum(axis=1)
    df = df[live > 0].copy()
    df["live"] = live[live > 0]
    df["oos"] = df["out_of_order"] + df["inoperative"]
    df = df.sort_values(["station_id", "ts"])
    # did the reported state change since this station's previous snapshot?
    sig = df[COUNTS].astype("int64").to_numpy()
    same_station = df["station_id"].to_numpy()[1:] == df["station_id"].to_numpy()[:-1]
    changed = np.zeros(len(df), dtype=bool)
    changed[1:] = same_station & (sig[1:] != sig[:-1]).any(axis=1)
    df["changed"] = changed
    df["hour"] = df["ts"].dt.tz_convert(TZ).dt.floor("h")
    df["unknown_share"] = df["unknown"] / df["live"]

    g = df.groupby(["station_id", "hour"], sort=False)
    out = g.agg(
        source=("source", "first"),
        snaps=("ts", "size"),
        max_charging=("charging", "max"),
        max_oos=("oos", "max"),
        unknown_share=("unknown_share", "mean"),
        live=("live", "max"),
        changes=("changed", "sum"),
    ).reset_index()
    out["in_use"] = out["max_charging"] > 0
    out["outage"] = out["max_oos"] > 0
    out["status_ok"] = ~out["outage"] & (out["unknown_share"] < 0.5)
    out["date"] = pd.Timestamp(path.stem.split("=")[1])
    return out[["station_id", "source", "hour", "date", "snaps", "live", "in_use",
                "outage", "status_ok", "changes"]]


def hourly(start: str, end: str, panel_ids: np.ndarray) -> pd.DataFrame:
    days = pd.date_range(start, end).strftime("%Y-%m-%d")
    parts = []
    for d in days:
        p = STATUS_DIR / f"date={d}.parquet"
        if p.exists():
            parts.append(hourly_one_day(p, panel_ids))
    h = pd.concat(parts, ignore_index=True)
    h["source"] = h["source"].astype("category")
    # frozen feed: no station of the source changed state during the whole day
    src_day = h.groupby(["source", "date"], observed=True)["changes"].sum()
    frozen = src_day[src_day == 0].reset_index()[["source", "date"]].assign(frozen=True)
    h = h.merge(frozen, on=["source", "date"], how="left")
    h["frozen"] = h["frozen"].fillna(False).astype(bool)
    h["how"] = (h["hour"].dt.dayofweek * 24 + h["hour"].dt.hour).astype("int16")
    return h


# --------------------------------------------------------------------------- #
# 2. Weekly profiles
# --------------------------------------------------------------------------- #
def profiles(train: pd.DataFrame) -> pd.DataFrame:
    """P(in use) per station and hour of week, shrunk towards a station-specific prior.

    The prior keeps the network's daily shape (busy evenings, quiet nights) but
    scales it to the station's own overall usage level.
    """
    t = train[train["status_ok"] & ~train["frozen"]]
    net_shape = t.groupby("how")["in_use"].mean()
    net_shape = net_shape / net_shape.mean()
    station_rate = t.groupby("station_id")["in_use"].mean().rename("station_rate")
    cell = t.groupby(["station_id", "how"]).agg(uses=("in_use", "sum"), obs=("in_use", "size")).reset_index()
    cell = cell.merge(station_rate, on="station_id")
    cell["prior"] = (cell["station_rate"] * cell["how"].map(net_shape)).clip(0, 0.98)
    cell["p"] = (cell["uses"] + PRIOR_STRENGTH * cell["prior"]) / (cell["obs"] + PRIOR_STRENGTH)
    # complete grid: hours of week a station was never observed in get the prior
    grid = pd.MultiIndex.from_product([station_rate.index, range(168)], names=["station_id", "how"]).to_frame(index=False)
    grid = grid.merge(station_rate, on="station_id")
    grid["prior"] = (grid["station_rate"] * grid["how"].map(net_shape).fillna(1)).clip(0, 0.98)
    prof = grid.merge(cell[["station_id", "how", "p"]], on=["station_id", "how"], how="left")
    prof["p"] = prof["p"].fillna(prof["prior"]).clip(0, 0.98)
    return prof[["station_id", "how", "p", "station_rate"]]


# --------------------------------------------------------------------------- #
# 3. Idle spells
# --------------------------------------------------------------------------- #
def idle_spells(test: pd.DataFrame, prof: pd.DataFrame) -> pd.DataFrame:
    """One row per idle spell: consecutive status-OK, unused hours of one station."""
    t = test[~test["frozen"]].merge(prof[["station_id", "how", "p"]], on=["station_id", "how"], how="inner")
    t = t.sort_values(["station_id", "hour"]).reset_index(drop=True)
    idle = t["status_ok"] & ~t["in_use"]
    gap_h = t.groupby("station_id")["hour"].diff().dt.total_seconds().div(3600)
    # a new spell starts when the hour is not idle, the station changes, or the gap is too long
    breaks = ~idle | gap_h.isna() | (gap_h > MAX_GAP_H) | (~idle).shift(fill_value=True)
    spell_id = breaks.cumsum()
    t = t[idle].assign(spell=spell_id[idle], surprise=-np.log10(1 - t.loc[idle, "p"]))
    s = t.groupby("spell").agg(
        station_id=("station_id", "first"),
        start=("hour", "first"),
        end=("hour", "last"),
        hours=("hour", "size"),
        expected_busy_hours=("p", "sum"),
        score=("surprise", "sum"),
    ).reset_index(drop=True)
    s["end"] = s["end"] + pd.Timedelta(hours=1)
    return s


def main() -> int:
    panel = pd.read_parquet(config.PROCESSED_DIR / "station_panel.parquet", columns=["station_id"])
    window = json.loads((config.PROCESSED_DIR / "model_window.json").read_text())
    h = hourly(window["start"], window["end"], panel["station_id"].to_numpy())
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    h.to_parquet(OUT_DIR / "hourly.parquet", index=False)
    print(f"hourly: {len(h):,} station-hours, {h['station_id'].nunique():,} stations, "
          f"{h['frozen'].mean():.1%} in frozen-feed days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
