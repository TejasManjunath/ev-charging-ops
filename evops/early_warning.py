"""Phase 3: outage early warning.

Question: of the stations that are working right now, which will report an
outage in the next 7 days?

Why this design
- Capture frequency in the archive changes from every few minutes (summer) to
  every ~2.5 hours (mid-August to late September). To make summer and autumn
  data comparable, every station is sampled ONCE per 3-hour block (the first
  snapshot in the block). Features and labels are built on that grid, so a model
  trained on dense summer data sees the same kind of input when scoring sparse
  autumn data.
- Scheduled closures are not outages. Many chargers at supermarkets (Lidl alone
  runs ~900 in the feed) are switched off when the store closes, at night and on
  Sundays, and the feed reports them as out of service. For each station, a
  3-hour block of the week (e.g. Sunday 12:00-15:00) counts as "scheduled closed"
  if it was out of service at least 75% of the time over the lookback, while the
  station is clearly open at other times. Because sparse periods of the archive
  barely sample the night, stations also inherit their operator's pooled pattern
  (e.g. all Lidl stations together) unless their own data shows them open then.
  Outages in scheduled-closed blocks are ignored in features and labels.
- Label: an UNEXPECTED outage (a charge point out of order or inoperative outside
  scheduled closures) seen in at least 2 blocks of the following 7 days, so a
  single blip doesn't count.
  Only stations that look fine at the cutoff are scored: the task is to predict
  NEW outages, not to repeat the current ones.
- Validation is time-based only. Models are trained on earlier cutoffs and
  tested on later, unseen weeks, including a separate test in September, under
  a different capture regime, with a model trained only on summer data.

Run from the repo root:
    python -m evops.early_warning evaluate   # backtests, writes reports/early_warning.md
    python -m evops.early_warning predict    # trains on all labelled data, writes app_data/risk.parquet
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from evops import config

STATUS_DIR = config.PROCESSED_DIR / "status"
APP_DATA = config.ROOT / "app_data"
MODEL_DIR = config.ROOT / "models"
REPORT_PATH = config.REPORTS_DIR / "early_warning.md"
CACHE_DIR = os.getenv("EVOPS_BLOCK_CACHE")   # optional: cache per-day block files (off by default, saves SSD writes)

TZ = "Europe/Berlin"
COUNTS = ["available", "charging", "reserved", "out_of_order", "inoperative", "unknown"]
BLOCK = "3h"
BLOCKS_PER_DAY = 8
LOOKBACK_DAYS = 21
HORIZON_DAYS = 7
MIN_LABEL_BLOCKS = 2        # outage blocks in the horizon that count as "had an outage"
MIN_OBS_LAST7 = 0.3         # share of the last 7 days' blocks a station must be observed in
MIN_OBS_HORIZON = 0.3       # same for the label window
CLOSED_MIN_SHARE = 0.75     # block of week out of service at least this often -> scheduled closed
OPEN_MAX_SHARE = 0.40       # ... provided the station has blocks of week it is clearly open in
MIN_BOWS = 3                # at least this many closed AND open blocks of week
MAX_OUT_SHARE_7 = 0.5       # stations down more than this share of last week are chronic, not scored
MIN_OPERATOR_STATIONS = 20  # operators this big can lend their pooled closing pattern to their stations

FEATURES = [
    "out_share_21", "out_share_7", "out_share_1", "full_out_share_21", "oos_point_share_7",
    "out_starts_per100", "longest_out_run", "days_since_out",
    "use_7", "use_21", "use_change", "unknown_share_7",
    "live_points", "max_power_kw", "operator_out_share_7", "closed_share",
]
FEATURE_LABELS = {
    "out_share_21": "Share of time with an outage, last 21 days",
    "out_share_7": "Share of time with an outage, last 7 days",
    "out_share_1": "Share of time with an outage, last day",
    "full_out_share_21": "Share of time fully down, last 21 days",
    "oos_point_share_7": "Share of charge points out of service, last 7 days",
    "out_starts_per100": "New outages started per 100 observations, last 21 days",
    "longest_out_run": "Longest outage, last 21 days (3-hour blocks)",
    "days_since_out": "Days since last outage",
    "use_7": "Usage, last 7 days",
    "use_21": "Usage, last 21 days",
    "use_change": "Change in usage, last 7 vs 21 days",
    "unknown_share_7": "Share of time with unknown status, last 7 days",
    "live_points": "Charge points at the station",
    "max_power_kw": "Maximum power (kW)",
    "operator_out_share_7": "Operator's network-wide outage share, last 7 days",
    "closed_share": "Share of the week with scheduled closures",
}


# --------------------------------------------------------------------------- #
# 3-hour block grid
# --------------------------------------------------------------------------- #
def blocks_one_day(path: Path) -> pd.DataFrame:
    cols = ["ts", "station_id", "source", *COUNTS]
    try:  # the outdated flag only exists in files from mid-September on
        df = pd.read_parquet(path, columns=cols + ["outdated"])
    except Exception:
        df = pd.read_parquet(path, columns=cols)
        df["outdated"] = pd.NA
    live = df[COUNTS].sum(axis=1)
    df = df[live > 0]
    df = df.assign(block=df["ts"].dt.tz_convert(TZ).dt.floor(BLOCK))
    df = df.sort_values("ts").drop_duplicates(["station_id", "block"], keep="first")
    live = df[COUNTS].sum(axis=1)
    oos = df["out_of_order"] + df["inoperative"]
    out = pd.DataFrame({
        "station_id": df["station_id"].astype("int32"),
        "block": df["block"],
        "live": live.astype("int16"),
        "oos": oos.astype("int16"),
        "in_use": (df["charging"] > 0),
        "unknown_share": (df["unknown"] / live).astype("float32"),
        "outdated": df["outdated"].astype("boolean"),
    })
    out["outage"] = out["oos"] > 0
    out["full_out"] = out["oos"] >= out["live"]
    return out.reset_index(drop=True)


def load_blocks(start: str, end: str) -> pd.DataFrame:
    parts = []
    for d in pd.date_range(start, end).strftime("%Y-%m-%d"):
        path = STATUS_DIR / f"date={d}.parquet"
        if not path.exists():
            continue
        cached = Path(CACHE_DIR) / f"date={d}.parquet" if CACHE_DIR else None
        if cached and cached.exists():
            parts.append(pd.read_parquet(cached))
            continue
        b = blocks_one_day(path)
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            b.to_parquet(cached, index=False)
        parts.append(b)
    b = pd.concat(parts, ignore_index=True)
    b["bow"] = (b["block"].dt.dayofweek * BLOCKS_PER_DAY + b["block"].dt.hour // 3).astype("int8")
    # a day file can hold a few snapshots of the neighbouring day: keep one row per station and block
    return b.drop_duplicates(["station_id", "block"]).sort_values(["station_id", "block"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Features and labels at one cutoff
# --------------------------------------------------------------------------- #
def _closed_bows(s: pd.DataFrame, key: str) -> pd.DataFrame:
    """Blocks of week closed for each `key` whose pattern has both closed and open blocks."""
    n_closed = s[s["mean"] >= CLOSED_MIN_SHARE].groupby(key).size()
    n_open = s[s["mean"] <= OPEN_MAX_SHARE].groupby(key).size()
    sched = n_closed.index[(n_closed >= MIN_BOWS) & (n_open.reindex(n_closed.index).fillna(0) >= MIN_BOWS)]
    return s[s[key].isin(sched) & (s["mean"] >= CLOSED_MIN_SHARE)][[key, "bow"]]


def scheduled_closures(past: pd.DataFrame, stations: pd.DataFrame) -> pd.DataFrame:
    """(station_id, bow) pairs that are scheduled closed, learned from the lookback only."""
    s = past.groupby(["station_id", "bow"])["outage"].agg(["mean", "size"]).reset_index()
    own = _closed_bows(s[s["size"] >= 2], "station_id")

    # operator-level pattern, pooled over all of the operator's stations
    ops = stations.set_index("station_id")["operator_name"]
    p = past.assign(operator_name=past["station_id"].map(ops)).dropna(subset=["operator_name"])
    big = p.groupby("operator_name")["station_id"].nunique()
    p = p[p["operator_name"].isin(big.index[big >= MIN_OPERATOR_STATIONS])]
    o = p.groupby(["operator_name", "bow"])["outage"].agg(["mean", "size"]).reset_index()
    op_closed = _closed_bows(o, "operator_name")
    inherited = (pd.DataFrame({"station_id": p["station_id"].unique()})
                 .assign(operator_name=lambda d: d["station_id"].map(ops))
                 .merge(op_closed, on="operator_name")[["station_id", "bow"]])
    # ... unless the station's own data shows it open in that block
    open_own = s[(s["size"] >= 2) & (s["mean"] <= OPEN_MAX_SHARE)][["station_id", "bow"]].assign(is_open=True)
    inherited = inherited.merge(open_own, on=["station_id", "bow"], how="left")
    inherited = inherited[inherited["is_open"].isna()][["station_id", "bow"]]

    closed = pd.concat([own, inherited]).drop_duplicates()
    return closed.assign(closed=True)


def mark_unexpected(df: pd.DataFrame, closed: pd.DataFrame) -> pd.DataFrame:
    df = df.merge(closed, on=["station_id", "bow"], how="left")
    df["closed"] = df["closed"].eq(True)
    df["outage"] = df["outage"] & ~df["closed"]
    df["full_out"] = df["full_out"] & ~df["closed"]
    df.loc[df["closed"], "oos"] = 0
    return df


def features_at(b: pd.DataFrame, cutoff: str, stations: pd.DataFrame, with_label: bool = True) -> pd.DataFrame:
    c = pd.Timestamp(cutoff, tz=TZ)
    past = b[(b["block"] < c) & (b["block"] >= c - pd.Timedelta(days=LOOKBACK_DAYS))]
    closed = scheduled_closures(past, stations)
    past = mark_unexpected(past, closed).sort_values(["station_id", "block"])
    last7 = past[past["block"] >= c - pd.Timedelta(days=7)]
    last1 = past[past["block"] >= c - pd.Timedelta(days=1)]
    g = past.groupby("station_id")

    # outage episodes: an outage block after a non-outage block (on the observed sequence)
    prev = g["outage"].shift(fill_value=False)
    starts = (past["outage"] & ~prev).groupby(past["station_id"]).sum()
    # longest consecutive run of outage blocks
    run_id = (past["outage"] != prev).cumsum()
    runs = past[past["outage"]].groupby(["station_id", run_id[past["outage"]]]).size()
    longest = runs.groupby(level=0).max() if len(runs) else pd.Series(dtype=float)
    last_out = past[past["outage"]].groupby("station_id")["block"].max()
    ok7 = last7[~last7["outage"] & ~last7["closed"]]

    f = pd.DataFrame({
        "obs_21": g.size(),
        "obs_7": last7.groupby("station_id").size(),
        "out_share_21": g["outage"].mean(),
        "out_share_7": last7.groupby("station_id")["outage"].mean(),
        "out_share_1": last1.groupby("station_id")["outage"].mean(),
        "full_out_share_21": g["full_out"].mean(),
        "oos_point_share_7": (last7["oos"] / last7["live"]).groupby(last7["station_id"]).mean(),
        "out_starts_per100": 100 * starts / g.size(),
        "longest_out_run": longest,
        "days_since_out": (c - last_out).dt.total_seconds() / 86400,
        "use_7": ok7.groupby("station_id")["in_use"].mean(),
        "use_21": past[~past["outage"] & ~past["closed"]].groupby("station_id")["in_use"].mean(),
        "unknown_share_7": last7.groupby("station_id")["unknown_share"].mean(),
        "live_points": g["live"].max(),
        # state in the last observed open (not scheduled-closed) block before the cutoff
        "now_outage": past[~past["closed"]].groupby("station_id")["outage"].last(),
        "now_outdated": g["outdated"].last(),
    })
    f["longest_out_run"] = f["longest_out_run"].fillna(0)
    f["days_since_out"] = f["days_since_out"].fillna(LOOKBACK_DAYS + 1)
    f["out_share_1"] = f["out_share_1"].fillna(f["out_share_7"])
    f["use_change"] = f["use_7"] - f["use_21"]
    f["closed_share"] = closed.groupby("station_id").size().reindex(f.index).fillna(0) / (7 * BLOCKS_PER_DAY)
    f = f.join(stations.set_index("station_id")[["operator_name", "max_power_kw"]], how="left")
    op = last7.join(stations.set_index("station_id")["operator_name"], on="station_id")
    f["operator_out_share_7"] = f["operator_name"].map(op.groupby("operator_name")["outage"].mean())

    # who gets scored: observed enough recently, working now and mostly working last week
    # (stations down most of the week are chronic cases for the outage list, not a forecast),
    # feed not flagged outdated
    keep = (f["obs_7"] >= MIN_OBS_LAST7 * 7 * BLOCKS_PER_DAY) & ~f["now_outage"].astype("boolean").fillna(True).astype(bool) \
        & (f["out_share_7"] < MAX_OUT_SHARE_7) & ~f["now_outdated"].astype("boolean").fillna(False).astype(bool)
    f = f[keep].copy()
    f["cutoff"] = c

    if with_label:
        fut = b[(b["block"] >= c) & (b["block"] < c + pd.Timedelta(days=HORIZON_DAYS))]
        fut = mark_unexpected(fut, closed)
        fg = fut.groupby("station_id")
        f["obs_h"] = fg.size()
        f["y"] = (fg["outage"].sum() >= MIN_LABEL_BLOCKS).astype(float)
        f = f[f["obs_h"] >= MIN_OBS_HORIZON * HORIZON_DAYS * BLOCKS_PER_DAY]
        f["y"] = f["y"].astype(int)
    return f.reset_index()


def station_table() -> pd.DataFrame:
    s = pd.read_parquet(config.PROCESSED_DIR / "stations.parquet",
                        columns=["station_id", "operator_name", "max_electric_power"])
    s = s.drop_duplicates("station_id", keep="last")
    s["max_power_kw"] = s["max_electric_power"] / 1000
    return s[["station_id", "operator_name", "max_power_kw"]]


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def fit_lgbm(train: pd.DataFrame):
    import lightgbm as lgb
    model = lgb.LGBMClassifier(
        n_estimators=400, learning_rate=0.03, num_leaves=31, min_child_samples=50,
        subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
        random_state=0, verbose=-1,
    )
    model.fit(train[FEATURES], train["y"])
    return model


def fit_logistic(train: pd.DataFrame):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.impute import SimpleImputer
    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                         LogisticRegression(max_iter=2000)).fit(train[FEATURES], train["y"])


def scores(y: np.ndarray, p: np.ndarray, ks=(100, 500)) -> dict:
    from sklearn.metrics import average_precision_score, roc_auc_score
    order = np.argsort(-p, kind="stable")
    out = {"n": len(y), "base_rate": y.mean(), "auc": roc_auc_score(y, p), "ap": average_precision_score(y, p)}
    for k in ks:
        top = y[order[:k]]
        out[f"prec@{k}"] = top.mean()
        out[f"recall@{k}"] = top.sum() / max(y.sum(), 1)
    return out


def evaluate_block(model_fns: dict, train: pd.DataFrame, test: pd.DataFrame) -> pd.DataFrame:
    rows = []
    y = test["y"].to_numpy()
    rule = test["out_share_21"].to_numpy() + 1e-3 * test["out_starts_per100"].to_numpy()
    rows.append({"model": "Rule: share of time down, last 21 days", **scores(y, rule)})
    rule2 = test["out_starts_per100"].to_numpy() - 1e-3 * test["days_since_out"].to_numpy()
    rows.append({"model": "Rule: how often it failed, last 21 days", **scores(y, rule2)})
    for name, fn in model_fns.items():
        m = fn(train)
        p = m.predict_proba(test[FEATURES])[:, 1]
        rows.append({"model": name, **scores(y, p)})
    return pd.DataFrame(rows)


def cutoffs(start: str, end: str, every_days: int) -> list[str]:
    return [d.strftime("%Y-%m-%d") for d in pd.date_range(start, end, freq=f"{every_days}D")]


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def evaluate() -> int:
    from sklearn.inspection import permutation_importance

    st = station_table()
    b = load_blocks("2026-06-30", "2026-10-08")
    print(f"blocks: {len(b):,} station-blocks, {b['station_id'].nunique():,} stations")
    fns = {"Logistic regression": fit_logistic, "LightGBM": fit_lgbm}

    def frame(cs):
        return pd.concat([features_at(b, c, st) for c in cs], ignore_index=True)

    # A) summer: train on 6 cutoffs, test on the following unseen week
    train_a = frame(cutoffs("2026-07-21", "2026-07-31", 2))
    test_a = frame(["2026-08-07"])
    res_a = evaluate_block(fns, train_a, test_a)
    # B) September, other capture regime: summer-trained model, unseen weeks
    test_b = frame(["2026-09-10", "2026-09-17", "2026-09-24"])
    res_b = evaluate_block(fns, train_a, test_b)
    # C) never-failed stations in test A (no outage in the last 21 days): the hard case
    never = test_a[test_a["out_share_21"] == 0]
    res_c = evaluate_block(fns, train_a, never)

    model = fit_lgbm(train_a)
    imp = permutation_importance(model, test_a[FEATURES], test_a["y"], scoring="roc_auc",
                                 n_repeats=5, random_state=0)
    importance = (pd.DataFrame({"feature": FEATURES, "auc_drop": imp.importances_mean})
                  .sort_values("auc_drop", ascending=False))

    summary = {
        "train_rows": len(train_a), "train_cutoffs": cutoffs("2026-07-21", "2026-07-31", 2),
        "test_summer": res_a.to_dict("records"), "test_september": res_b.to_dict("records"),
        "test_never_failed": res_c.to_dict("records"),
        "importance": importance.to_dict("records"),
    }
    write_report(summary, res_a, res_b, res_c, importance, train_a, test_a, test_b, never)
    MODEL_DIR.mkdir(exist_ok=True)
    (MODEL_DIR / "early_warning_eval.json").write_text(json.dumps(summary, indent=2, default=float))
    print(res_a.round(3).to_string(index=False))
    print(res_b.round(3).to_string(index=False))
    print(res_c.round(3).to_string(index=False))
    print(importance.round(4).to_string(index=False))
    return 0


def fmt_table(df: pd.DataFrame) -> str:
    cols = ["model", "auc", "ap", "prec@100", "prec@500", "recall@500"]
    head = "| Model | AUC | Avg. precision | Precision, top 100 | Precision, top 500 | Recall, top 500 |"
    lines = [head, "|---|---|---|---|---|---|"]
    for r in df[cols].itertuples(index=False):
        lines.append(f"| {r[0]} | {r[1]:.3f} | {r[2]:.3f} | {r[3]:.0%} | {r[4]:.0%} | {r[5]:.0%} |")
    return "\n".join(lines)


def write_report(summary, res_a, res_b, res_c, importance, train_a, test_a, test_b, never) -> None:
    def get(res, model, metric):
        return float(res.loc[res["model"] == model, metric].iloc[0])
    lg, fr = "LightGBM", "Rule: how often it failed, last 21 days"
    conclusion = (
        f"LightGBM has the best AUC in both tests, but only slightly ahead of a simple rule: rank stations by "
        f"how often they failed in the last 21 days (AUC {get(res_a, lg, 'auc'):.3f} vs {get(res_a, fr, 'auc'):.3f} "
        f"in August, {get(res_b, lg, 'auc'):.3f} vs {get(res_b, fr, 'auc'):.3f} in September). Repeated recent "
        f"faults are by far the strongest signal, and first-time failures are barely predictable "
        f"(AUC {get(res_c, lg, 'auc'):.3f}). The practical value is a ranked list of stations with intermittent "
        f"faults: of the 100 stations ranked riskiest, {get(res_a, lg, 'prec@100'):.0%} (August) and "
        f"{get(res_b, lg, 'prec@100'):.0%} (September) had an outage within a week, against a base rate of "
        f"{get(res_a, lg, 'base_rate'):.0%} and {get(res_b, lg, 'base_rate'):.0%}."
    )
    imp_lines = ["| Signal | AUC lost when shuffled |", "|---|---|"]
    for r in importance.head(8).itertuples(index=False):
        imp_lines.append(f"| {FEATURE_LABELS[r.feature]} | {r.auc_drop:.3f} |")
    text = f"""# Outage early warning: evaluation

Generated by `python -m evops.early_warning evaluate`.

**Task.** For every station that is working at a cutoff date (and was working most of the previous
week; stations down most of the week are chronic cases, handled by the live outage list), predict whether it will report an
unexpected outage (a charge point out of order or inoperative, seen in at least {MIN_LABEL_BLOCKS} three-hour
blocks) in the next {HORIZON_DAYS} days. Scheduled closures, such as supermarket chargers switched off at night
and on Sundays, are learned per station and don't count. All features use only the {LOOKBACK_DAYS} days
before the cutoff.

## Conclusion

{conclusion}

**Training data.** {len(train_a):,} station-cutoffs from 6 cutoff dates, 21 to 31 July 2026.

## Test 1: the following week (cutoff 7 Aug 2026, {len(test_a):,} stations, {test_a['y'].mean():.0%} had an outage)

{fmt_table(res_a)}

## Test 2: September, a different capture regime ({len(test_b):,} station-weeks from 3 cutoffs, {test_b['y'].mean():.0%} had an outage)

The model is still trained only on July data. In September the archive recorded each station only every
~2.5 hours instead of every few minutes, and coverage dropped. Features are built on a 3-hour grid so
both periods look alike to the model.

{fmt_table(res_b)}

## Test 3: stations with no outage in the previous 21 days ({len(never):,} stations, {never['y'].mean():.0%} had an outage)

Both rules have nothing to go on here, so they score as random.

{fmt_table(res_c)}

## What drives the predictions

Permutation importance on test 1: how much AUC drops when one signal is shuffled.

{chr(10).join(imp_lines)}

## How to read the numbers

- **AUC**: chance that a random station that did fail is ranked above one that didn't (0.5 = random, 1 = perfect).
- **Precision, top 100**: of the 100 stations ranked riskiest, the share that had an outage that week.
  This is what a maintenance team with capacity for 100 checks a week would see.
- **Recall, top 500**: share of all stations that failed that week that were in the top 500.
"""
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(text, encoding="utf-8")


def predict() -> int:
    st = station_table()
    files = sorted(STATUS_DIR.glob("date=*.parquet"))
    last_day = pd.Timestamp(files[-1].stem.split("=")[1])
    cutoff = (last_day + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    b = load_blocks("2026-06-30", last_day.strftime("%Y-%m-%d"))
    APP_DATA.mkdir(exist_ok=True)
    # train on every cutoff whose 7-day label window is complete
    last_train = (last_day - pd.Timedelta(days=HORIZON_DAYS - 1)).strftime("%Y-%m-%d")
    cs = cutoffs("2026-07-21", last_train, 2)
    train = pd.concat([features_at(b, c, st) for c in cs], ignore_index=True)
    model = fit_lgbm(train)
    now = features_at(b, cutoff, st, with_label=False)
    c = pd.Timestamp(cutoff, tz=TZ)
    closures = scheduled_closures(b[(b["block"] < c) & (b["block"] >= c - pd.Timedelta(days=LOOKBACK_DAYS))], st)
    closures[["station_id", "bow"]].to_parquet(APP_DATA / "closures.parquet", index=False)
    now["risk"] = model.predict_proba(now[FEATURES])[:, 1]
    out = now[["station_id", "risk", "out_starts_per100", "out_share_21", "days_since_out", "obs_21"]].copy()
    out["outages_21d"] = (out["out_starts_per100"] * out["obs_21"] / 100).round().astype(int)
    out = out.drop(columns=["out_starts_per100", "obs_21"])
    for c in ("risk", "out_share_21", "days_since_out"):
        out[c] = out[c].astype("float32").round(3)
    out.to_parquet(APP_DATA / "risk.parquet", index=False)
    MODEL_DIR.mkdir(exist_ok=True)
    model.booster_.save_model(str(MODEL_DIR / "early_warning_lgbm.txt"))

    meta_path = APP_DATA / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["risk_as_of"] = cutoff
    meta["risk_train_cutoffs"] = [cs[0], cs[-1]]
    eval_path = MODEL_DIR / "early_warning_eval.json"
    if eval_path.exists():
        ev = json.loads(eval_path.read_text())
        lgbm = {k: next(r for r in ev[k] if r["model"] == "LightGBM") for k in
                ("test_summer", "test_september", "test_never_failed")}
        meta["risk_eval"] = {k: {m: round(v[m], 3) for m in ("auc", "prec@100", "base_rate")} for k, v in lgbm.items()}
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"risk: {len(out):,} stations scored as of {cutoff}, trained on {len(train):,} rows "
          f"({cs[0]} to {cs[-1]}); top-100 mean risk {out['risk'].nlargest(100).mean():.0%}; "
          f"{closures['station_id'].nunique():,} stations with scheduled closures")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["evaluate", "predict"])
    args = ap.parse_args()
    return evaluate() if args.command == "evaluate" else predict()


if __name__ == "__main__":
    sys.exit(main())
