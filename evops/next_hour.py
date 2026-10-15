"""Phase 4: will this fast-charging hub be full in an hour?

Question: for fast-charging hubs (at least 50 kW and 2 charge points), will there be
no free charge point about 60 minutes from now? Full hubs mean queues for drivers
and lost revenue for the operator.

Design
- Inputs are limited to what the live dashboard actually has: ONE live snapshot of
  each hub plus a small precomputed profile (how often the hub is full at each hour of
  the week). No trailing history, so the same features can be computed live.
- "Full" = no available charge point, at least one charging, and not fully out of
  service (a dead hub is an outage, not demand). Snapshots with unknown status or a feed
  flagged outdated are left out.
- Label: the hub's state in the snapshot nearest to 60 minutes later (within 20 min).
- Baselines a model has to beat: "how often this hub is full at that hour of the week"
  (profile), and the stronger rule "if it's full now it stays full, otherwise use the
  profile" (persistence plus profile).
- Validation is time-based: train on July, test on 1-14 August and on October (other
  season, other capture regime). The number that matters most is for hubs that are NOT
  full now: predicting that a full hub stays full is easy.

Run from the repo root:
    python -m evops.next_hour evaluate   # backtests, writes reports/next_hour.md
    python -m evops.next_hour predict    # trains the final model, writes the hub profile for the app
"""

from __future__ import annotations

import argparse
import json
import sys

import numpy as np
import pandas as pd

from evops import config

STATUS_DIR = config.PROCESSED_DIR / "status"
APP_DATA = config.ROOT / "app_data"
MODEL_DIR = config.ROOT / "models"
REPORT_PATH = config.REPORTS_DIR / "next_hour.md"
TZ = "Europe/Berlin"
COUNTS = ["available", "charging", "reserved", "out_of_order", "inoperative", "unknown"]
MIN_KW = 50
MIN_POINTS = 2
HORIZON = pd.Timedelta(minutes=60)
TOLERANCE = pd.Timedelta(minutes=20)
PRIOR = 4   # pseudo-observations shrinking a thin hour-of-week cell towards the hub's overall rate

FEATURES = [
    "live_points", "available", "charging", "oos", "occ_share", "free_share", "full_now",
    "hour", "dow", "power_kw",
    "prof_full_now", "prof_full_next", "prof_occ_next", "hub_full_rate",
]
FEATURE_LABELS = {
    "live_points": "Charge points reporting", "available": "Free charge points now",
    "charging": "Charge points in use now", "oos": "Charge points out of service now",
    "occ_share": "Share in use now", "free_share": "Share free now", "full_now": "Full now",
    "hour": "Hour of day", "dow": "Day of week", "power_kw": "Maximum power",
    "prof_full_now": "How often full at this hour of the week",
    "prof_full_next": "How often full in the next hour of the week",
    "prof_occ_next": "Typical share in use in the next hour of the week",
    "hub_full_rate": "How often full overall",
}


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
def hub_ids() -> np.ndarray:
    s = pd.read_parquet(config.PROCESSED_DIR / "stations.parquet", columns=["station_id", "max_electric_power"])
    return s.loc[s["max_electric_power"] >= MIN_KW * 1000, "station_id"].unique()


def add_state(df: pd.DataFrame) -> pd.DataFrame:
    """Counts -> state columns. Works on archive rows and on the live frame alike."""
    df = df.copy()
    df["live_points"] = df[COUNTS].sum(axis=1)
    df["oos"] = df["out_of_order"] + df["inoperative"]
    df["occ_share"] = (df["charging"] + df["reserved"]) / df["live_points"]
    df["free_share"] = df["available"] / df["live_points"]
    df["full_now"] = ((df["available"] == 0) & (df["charging"] + df["reserved"] > 0)
                      & (df["oos"] < df["live_points"])).astype("int8")
    return df


def usable(df: pd.DataFrame) -> pd.Series:
    outdated = df["outdated"].astype("boolean").fillna(False).astype(bool) if "outdated" in df else False
    return (df["live_points"] >= MIN_POINTS) & (df["unknown"] == 0) & ~outdated


def load_snapshots(start: str, end: str, hubs: np.ndarray) -> pd.DataFrame:
    parts = []
    for d in pd.date_range(start, end).strftime("%Y-%m-%d"):
        path = STATUS_DIR / f"date={d}.parquet"
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path, columns=["ts", "station_id", *COUNTS, "outdated"])
        except Exception:
            df = pd.read_parquet(path, columns=["ts", "station_id", *COUNTS])
        df = df[df["station_id"].isin(hubs)]
        df = add_state(df)
        df = df[usable(df)]
        parts.append(df[["ts", "station_id", "live_points", "available", "charging", "oos",
                         "occ_share", "free_share", "full_now"]])
    s = pd.concat(parts, ignore_index=True).drop_duplicates(["station_id", "ts"])
    local = s["ts"].dt.tz_convert(TZ)
    s["hour"] = local.dt.hour.astype("int8")
    s["dow"] = local.dt.dayofweek.astype("int8")
    s["how"] = (s["dow"] * 24 + s["hour"]).astype("int16")
    s["hour_slot"] = local.dt.floor("h")
    return s.sort_values(["station_id", "ts"]).reset_index(drop=True)


def hourly_sample(s: pd.DataFrame) -> pd.DataFrame:
    """One snapshot per hub and hour (the first), so dense and sparse periods weigh alike."""
    return s.drop_duplicates(["station_id", "hour_slot"], keep="first")


def with_label(sample: pd.DataFrame, s: pd.DataFrame) -> pd.DataFrame:
    left = sample.assign(target_ts=sample["ts"] + HORIZON).sort_values("target_ts")
    right = s[["station_id", "ts", "full_now"]].rename(columns={"ts": "label_ts", "full_now": "y"}).sort_values("label_ts")
    m = pd.merge_asof(left, right, left_on="target_ts", right_on="label_ts", by="station_id",
                      direction="nearest", tolerance=TOLERANCE)
    return m.dropna(subset=["y"]).assign(y=lambda d: d["y"].astype(int))


# --------------------------------------------------------------------------- #
# Hour-of-week profile
# --------------------------------------------------------------------------- #
def build_profile(sample: pd.DataFrame) -> pd.DataFrame:
    rate = sample.groupby("station_id")["full_now"].mean().rename("hub_full_rate")
    cell = (sample.groupby(["station_id", "how"])
            .agg(n=("full_now", "size"), full=("full_now", "sum"), occ=("occ_share", "mean")).reset_index())
    grid = pd.MultiIndex.from_product([rate.index, range(168)], names=["station_id", "how"]).to_frame(index=False)
    p = grid.merge(cell, on=["station_id", "how"], how="left").merge(rate, on="station_id")
    p[["n", "full"]] = p[["n", "full"]].fillna(0)
    p["p_full"] = (p["full"] + PRIOR * p["hub_full_rate"]) / (p["n"] + PRIOR)
    occ_mean = sample.groupby("station_id")["occ_share"].mean()
    p["occ"] = p["occ"].fillna(p["station_id"].map(occ_mean))
    out = p[["station_id", "how", "p_full", "occ", "hub_full_rate"]].copy()
    for c in ("p_full", "occ", "hub_full_rate"):
        out[c] = out[c].astype("float32").round(3)
    out["how"] = out["how"].astype("int16")
    return out


def make_features(df: pd.DataFrame, profile: pd.DataFrame, power_kw: pd.Series | None = None) -> pd.DataFrame:
    """df needs station_id, the state columns from add_state, hour, dow, how."""
    f = df.copy()
    f["how_next"] = ((f["how"] + 1) % 168).astype("int16")
    now = profile[["station_id", "how", "p_full"]].rename(columns={"p_full": "prof_full_now"})
    nxt = profile[["station_id", "how", "p_full", "occ"]].rename(
        columns={"how": "how_next", "p_full": "prof_full_next", "occ": "prof_occ_next"})
    rate = profile.drop_duplicates("station_id")[["station_id", "hub_full_rate"]]
    f = (f.merge(now, on=["station_id", "how"], how="left")
          .merge(nxt, on=["station_id", "how_next"], how="left")
          .merge(rate, on="station_id", how="left"))
    if power_kw is not None:
        f["power_kw"] = f["station_id"].map(power_kw)
    return f


def station_power() -> pd.Series:
    s = pd.read_parquet(config.PROCESSED_DIR / "stations.parquet", columns=["station_id", "max_electric_power"])
    return (s.drop_duplicates("station_id", keep="last").set_index("station_id")["max_electric_power"] / 1000)


# --------------------------------------------------------------------------- #
# Models and scoring
# --------------------------------------------------------------------------- #
def fit_lgbm(train: pd.DataFrame):
    import lightgbm as lgb
    m = lgb.LGBMClassifier(n_estimators=300, learning_rate=0.05, num_leaves=63, min_child_samples=100,
                           subsample=0.8, subsample_freq=1, colsample_bytree=0.8, random_state=0, verbose=-1)
    return m.fit(train[FEATURES], train["y"])


def fit_logistic(train: pd.DataFrame):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                         LogisticRegression(max_iter=2000)).fit(train[FEATURES], train["y"])


def scores(y, p) -> dict:
    from sklearn.metrics import average_precision_score, brier_score_loss, roc_auc_score
    p = np.asarray(p, dtype=float)
    out = {"n": int(len(y)), "base_rate": float(np.mean(y)), "auc": roc_auc_score(y, p),
           "ap": average_precision_score(y, p)}
    out["brier"] = brier_score_loss(y, np.clip(p, 0, 1)) if p.min() >= 0 and p.max() <= 1 else np.nan
    return out


def evaluate_set(train: pd.DataFrame, test: pd.DataFrame, models: dict) -> pd.DataFrame:
    preds = {
        "Rule: full now, otherwise usual rate": test["full_now"].to_numpy() + 1e-3 * test["prof_full_next"].fillna(0).to_numpy(),
        "Profile: how often full at that hour": test["prof_full_next"].fillna(test["hub_full_rate"]).to_numpy(),
    }
    for name, m in models.items():
        preds[name] = m.predict_proba(test[FEATURES])[:, 1]
    rows = []
    not_full = (test["full_now"] == 0).to_numpy()
    y = test["y"].to_numpy()
    slots = test["hour_slot"].to_numpy()
    for name, p in preds.items():
        a, b = scores(y, p), scores(y[not_full], p[not_full])
        # of the 20 free hubs ranked most likely to fill up, each hour: share that did
        nf = pd.DataFrame({"slot": slots[not_full], "p": p[not_full], "y": y[not_full]})
        top20 = nf.sort_values("p", ascending=False).groupby("slot").head(20)["y"].mean()
        rows.append({"model": name, "auc_all": a["auc"], "ap_all": a["ap"], "brier_all": a["brier"],
                     "auc_not_full": b["auc"], "ap_not_full": b["ap"], "top20_hit": top20,
                     "n": a["n"], "base_rate": a["base_rate"],
                     "n_not_full": b["n"], "base_rate_not_full": b["base_rate"]})
    return pd.DataFrame(rows)


def dataset(feat_start: str, feat_end: str, profile: pd.DataFrame, hubs, power) -> pd.DataFrame:
    end_plus = (pd.Timestamp(feat_end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    s = load_snapshots(feat_start, end_plus, hubs)
    sample = hourly_sample(s)
    sample = sample[sample["ts"].dt.tz_convert(TZ) < pd.Timestamp(end_plus, tz=TZ)]
    d = with_label(sample, s)
    f = make_features(d, profile, power)
    return f[f["prof_full_next"].notna()]   # hubs without a profile can't be scored live either


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
TRAIN = ("2026-06-30", "2026-07-31")
TEST_AUG = ("2026-08-01", "2026-08-13")
PROFILE_OCT = ("2026-09-10", "2026-09-30")
TEST_OCT = ("2026-10-01", "2026-10-12")


def evaluate() -> int:
    from sklearn.inspection import permutation_importance
    hubs, power = hub_ids(), station_power()
    s_train = load_snapshots(*TRAIN, hubs)
    prof_summer = build_profile(hourly_sample(s_train))
    train = make_features(with_label(hourly_sample(s_train), s_train), prof_summer, power)
    test_aug = dataset(*TEST_AUG, prof_summer, hubs, power)
    prof_oct = build_profile(hourly_sample(load_snapshots(*PROFILE_OCT, hubs)))
    test_oct = dataset(*TEST_OCT, prof_oct, hubs, power)
    print(f"train {len(train):,} rows ({train['station_id'].nunique():,} hubs), "
          f"test Aug {len(test_aug):,}, test Oct {len(test_oct):,}")

    models = {"Logistic regression": fit_logistic(train), "LightGBM": fit_lgbm(train)}
    res_aug = evaluate_set(train, test_aug, models)
    res_oct = evaluate_set(train, test_oct, models)
    sub = test_aug.sample(min(len(test_aug), 200_000), random_state=0)
    imp = permutation_importance(models["LightGBM"], sub[FEATURES], sub["y"], scoring="roc_auc",
                                 n_repeats=3, random_state=0)
    importance = pd.DataFrame({"feature": FEATURES, "auc_drop": imp.importances_mean}).sort_values("auc_drop", ascending=False)

    summary = {"train_rows": len(train), "test_august": res_aug.to_dict("records"),
               "test_october": res_oct.to_dict("records"), "importance": importance.to_dict("records")}
    MODEL_DIR.mkdir(exist_ok=True)
    (MODEL_DIR / "next_hour_eval.json").write_text(json.dumps(summary, indent=2, default=float))
    write_report(res_aug, res_oct, importance, train, test_aug, test_oct)
    with pd.option_context("display.width", 200):
        print(res_aug.round(3).to_string(index=False))
        print(res_oct.round(3).to_string(index=False))
        print(importance.round(4).to_string(index=False))
    return 0


def fmt(res: pd.DataFrame) -> str:
    lines = ["| Model | AUC, all hubs | Avg. precision, all | AUC, hubs not full now | Avg. precision, not full now "
             "| Top 20 free hubs per hour: share that filled up |",
             "|---|---|---|---|---|---|"]
    for r in res.itertuples(index=False):
        lines.append(f"| {r.model} | {r.auc_all:.3f} | {r.ap_all:.3f} | {r.auc_not_full:.3f} | {r.ap_not_full:.3f} "
                     f"| {r.top20_hit:.1%} |")
    return "\n".join(lines)


def write_report(res_aug, res_oct, importance, train, test_aug, test_oct) -> None:
    def g(res, model, col):
        return float(res.loc[res["model"] == model, col].iloc[0])
    lg, pe, pr = "LightGBM", "Rule: full now, otherwise usual rate", "Profile: how often full at that hour"
    conclusion = (
        f"Across all hubs, the simple rule is already strong (AUC {g(res_aug, pe, 'auc_all'):.3f} in August) "
        f"and has the better average precision ({g(res_aug, pe, 'ap_all'):.3f} vs {g(res_aug, lg, 'ap_all'):.3f}), because "
        f"a hub that is full now stays full about half the time; "
        f"LightGBM reaches {g(res_aug, lg, 'auc_all'):.3f}. The useful case is hubs with a free charge point now: "
        f"there, 'full now' tells you nothing, the hub's usual pattern reaches AUC {g(res_aug, pr, 'auc_not_full'):.3f}, "
        f"and LightGBM {g(res_aug, lg, 'auc_not_full'):.3f} (August) and {g(res_oct, lg, 'auc_not_full'):.3f} "
        f"(October, other season and capture regime). "
        f"But full hubs are rare ({g(res_aug, lg, 'base_rate'):.1%} of hub-hours), so even the top of the list mostly "
        f"stays free: of the 20 free hubs ranked most likely to fill up each hour, {g(res_aug, lg, 'top20_hit'):.1%} did in "
        f"August (usual-pattern rule: {g(res_aug, pr, 'top20_hit'):.1%}) and {g(res_oct, lg, 'top20_hit'):.1%} in October "
        f"(rule: {g(res_oct, pr, 'top20_hit'):.1%}), so in October the model's top picks were no better than the rule's. "
        f"The forecast is useful as a ranking, not as a yes/no alarm, and its scores run high at the top."
    )
    imp = ["| Signal | AUC lost when shuffled |", "|---|---|"]
    imp += [f"| {FEATURE_LABELS[r.feature]} | {r.auc_drop:.3f} |" for r in importance.head(8).itertuples(index=False)]
    text = f"""# Next-hour forecast: evaluation

Generated by `python -m evops.next_hour evaluate`.

**Task.** For fast-charging hubs (at least {MIN_KW} kW and {MIN_POINTS} charge points), predict whether there will be no free
charge point about 60 minutes from now. Inputs are limited to one live snapshot of the hub plus its usual pattern by
hour of the week, because that is all the live dashboard has.

## Conclusion

{conclusion}

**Training data.** {len(train):,} hub-hours from {train['station_id'].nunique():,} hubs, 30 June to 31 July 2026.

## Test 1: 1-13 August 2026 ({len(test_aug):,} hub-hours, {test_aug['y'].mean():.1%} full an hour later)

{fmt(res_aug)}

## Test 2: 1-12 October 2026 ({len(test_oct):,} hub-hours, {test_oct['y'].mean():.1%} full an hour later)

Model still trained on July only. Hub profiles here come from 10-30 September, as they would in live use.

{fmt(res_oct)}

## What drives the predictions

{chr(10).join(imp)}
"""
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(text, encoding="utf-8")


def predict() -> int:
    """Final model on all labelled data; hub profile from the last 21 archive days for live use."""
    hubs, power = hub_ids(), station_power()
    files = sorted(STATUS_DIR.glob("date=*.parquet"))
    last = pd.Timestamp(files[-1].stem.split("=")[1])
    s_train = load_snapshots(*TRAIN, hubs)
    prof_summer = build_profile(hourly_sample(s_train))
    parts = [make_features(with_label(hourly_sample(s_train), s_train), prof_summer, power),
             dataset(*TEST_AUG, prof_summer, hubs, power)]
    prof_start = (last - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    recent = load_snapshots(prof_start, last.strftime("%Y-%m-%d"), hubs)
    prof_recent = build_profile(hourly_sample(recent))
    oct_start = max(pd.Timestamp("2026-09-28"), last - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
    parts.append(dataset(oct_start, (last - pd.Timedelta(days=1)).strftime("%Y-%m-%d"), prof_recent, hubs, power))
    train = pd.concat(parts, ignore_index=True)
    model = fit_lgbm(train)
    MODEL_DIR.mkdir(exist_ok=True)
    model.booster_.save_model(str(MODEL_DIR / "next_hour_lgbm.txt"))
    APP_DATA.mkdir(exist_ok=True)
    prof_recent.to_parquet(APP_DATA / "hub_profile.parquet", index=False, compression="zstd")

    meta_path = APP_DATA / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta["next_hour_profile"] = [prof_start, last.strftime("%Y-%m-%d")]
    ev_path = MODEL_DIR / "next_hour_eval.json"
    if ev_path.exists():
        ev = json.loads(ev_path.read_text())
        pick = lambda k, m: next(r for r in ev[k] if r["model"] == m)
        meta["next_hour_eval"] = {
            k: {"lgbm_auc_not_full": round(pick(k, "LightGBM")["auc_not_full"], 3),
                "profile_auc_not_full": round(pick(k, "Profile: how often full at that hour")["auc_not_full"], 3),
                "lgbm_auc_all": round(pick(k, "LightGBM")["auc_all"], 3),
                "persistence_auc_all": round(pick(k, "Rule: full now, otherwise usual rate")["auc_all"], 3)}
            for k in ("test_august", "test_october")}
    meta_path.write_text(json.dumps(meta, indent=2))
    print(f"next hour: trained on {len(train):,} rows; profile for {prof_recent['station_id'].nunique():,} hubs "
          f"from {prof_start} to {last.date()}")
    return 0


def score_live(live: pd.DataFrame, profile: pd.DataFrame, model_path, at=None) -> pd.DataFrame:
    """Score the dashboard's live frame. Returns hubs with their chance of being full in an hour."""
    import lightgbm as lgb
    df = add_state(live[["station_id", *COUNTS, "outdated", "power_kw"]])
    df = df[usable(df) & (df["power_kw"] >= MIN_KW)]
    now = pd.Timestamp(at).tz_convert(TZ) if at is not None else pd.Timestamp.now(tz=TZ)
    df = df.assign(hour=now.hour, dow=now.dayofweek, how=now.dayofweek * 24 + now.hour)
    f = make_features(df, profile)
    f = f[f["prof_full_next"].notna()]
    booster = lgb.Booster(model_file=str(model_path))
    f["p_full_1h"] = booster.predict(f[FEATURES])
    return f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["evaluate", "predict"])
    return evaluate() if ap.parse_args().command == "evaluate" else predict()


if __name__ == "__main__":
    sys.exit(main())
