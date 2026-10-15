"""EV Charging Operations: live dashboard for public charging in Germany.

Run from the repo root:
    streamlit run app/streamlit_app.py

Live data comes from MobiData BW (refreshed at most every 10 minutes);
history comes from app_data/, built by `python -m evops.app_data`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import pydeck as pdk
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from evops.live import fetch_live  # noqa: E402

APP_DATA = ROOT / "app_data"
FAST_KW = 50
# Colours: status colours for station state, one blue for neutral series
GOOD, SERIOUS, CRITICAL, QUIET, BLUE = "#0ca30c", "#ec835a", "#d03b3b", "#a3a29c", "#2a78d6"
CLOSED = "#8c96a8"
STATE_COLOURS = {"OK": GOOD, "Partly down": SERIOUS, "Fully down": CRITICAL, "Feed quiet": QUIET,
                 "Scheduled closure": CLOSED}
NOT_JUDGED = ["Feed quiet", "Scheduled closure"]   # states left out of outage figures

REPO_URL = "https://github.com/TejasManjunath/ev-charging-ops"

st.set_page_config(page_title="EV Charging Operations · Germany", page_icon="⚡", layout="wide")


def full_width(element, *args, **kwargs):
    """Full-width chart/table on both newer (width=) and older (use_container_width=) Streamlit."""
    try:
        return element(*args, width="stretch", **kwargs)
    except Exception:
        return element(*args, use_container_width=True, **kwargs)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@st.cache_data(ttl=600, show_spinner="Fetching live status of every station…")
def load_live():
    status, stations, meta = fetch_live()
    df = status.merge(stations, on=["station_id", "source"], how="left")
    df["power_kw"] = (df["max_electric_power"] / 1000).round(0)
    df["state"] = "OK"
    df.loc[df["out_of_service"] > 0, "state"] = "Partly down"
    df.loc[(df["out_of_service"] > 0) & (df["out_of_service"] == df["live_points"]), "state"] = "Fully down"
    df.loc[df["outdated"].fillna(False), "state"] = "Feed quiet"
    return df, meta


@st.cache_data
def load_history():
    days = pd.read_parquet(APP_DATA / "station_days.parquet")
    summary = pd.read_parquet(APP_DATA / "daily_summary.parquet")
    meta = json.loads((APP_DATA / "meta.json").read_text())
    risk = pd.read_parquet(APP_DATA / "risk.parquet") if (APP_DATA / "risk.parquet").exists() else None
    closures = (pd.read_parquet(APP_DATA / "closures.parquet")
                if (APP_DATA / "closures.parquet").exists() else pd.DataFrame(columns=["station_id", "bow"]))
    hub_profile = (pd.read_parquet(APP_DATA / "hub_profile.parquet")
                   if (APP_DATA / "hub_profile.parquet").exists() else None)
    return days, summary, meta, risk, closures, hub_profile


live, live_meta = load_live()
days, summary, hist_meta, risk, closures, hub_profile = load_history()
n_hist_days = days["day"].nunique()
chronic = days.groupby("station_id")["any_out_of_service"].sum().rename("days_down")
live = live.merge(chronic, on="station_id", how="left")
# Scheduled closures (e.g. supermarket chargers switched off at night and on Sundays):
# out of service in a 3-hour block of the week the station is normally closed in
now_berlin = pd.Timestamp(live_meta["fetched_at"]).tz_convert("Europe/Berlin")
bow_now = now_berlin.dayofweek * 8 + now_berlin.hour // 3
closed_now = set(closures.loc[closures["bow"] == bow_now, "station_id"])
live.loc[live["station_id"].isin(closed_now) & (live["out_of_service"] > 0)
         & (live["state"] != "Feed quiet"), "state"] = "Scheduled closure"

# --------------------------------------------------------------------------- #
# Sidebar filters
# --------------------------------------------------------------------------- #
st.sidebar.header("Filters")
top_ops = live["operator_name"].value_counts().head(40).index.tolist()
operators = st.sidebar.multiselect("Operator", top_ops, placeholder="All operators")
city = st.sidebar.text_input("City contains", placeholder="e.g. Dortmund")
fast_only = st.sidebar.checkbox(f"Fast chargers only (≥ {FAST_KW} kW)")

view = live
if operators:
    view = view[view["operator_name"].isin(operators)]
if city:
    view = view[view["city"].fillna("").str.contains(city, case=False)]
if fast_only:
    view = view[view["power_kw"] >= FAST_KW]

st.sidebar.markdown("---")
st.sidebar.markdown(
    "**About**  \n"
    "Built by Tejas Manjunath, MSc Business Intelligence & Data Science (AI), ISM Dortmund.  \n"
    "Python · pandas · Parquet · Streamlit, on open MobiData BW data.  \n"
    f"[Code, method and data-quality notes on GitHub]({REPO_URL})"
)
st.sidebar.caption(
    f"Live data fetched {pd.Timestamp(live_meta['fetched_at']).tz_convert('Europe/Berlin'):%d %b %H:%M} "
    f"(Berlin time), cached for 10 minutes. History: {hist_meta['station_days_from']} to "
    f"{hist_meta['archive_last_day']}."
)

# --------------------------------------------------------------------------- #
# Header and KPIs
# --------------------------------------------------------------------------- #
st.title("EV charging operations · Germany")
st.caption("Live status of public charging stations that publish real-time data, "
           "with outages ranked by capacity lost and stale feeds kept apart.")
with st.expander("How to read this"):
    st.markdown(
        "- **Needs attention now** lists stations with charge points out of service, largest charging "
        "capacity offline first, so a dead fast-charging hub outranks a few slow wallboxes.\n"
        "- **Days down** shows how many of the last 14 days the station had an outage: a full bar is a "
        "chronic problem, an empty one is new today.\n"
        "- **Feed quiet** means the operator's data feed has stopped updating. Those stations' status "
        "is unknown, so they are kept out of the outage list instead of being counted as broken.\n"
        "- **Scheduled closure** means the station is switched off at a time it is normally closed, such as "
        "supermarket chargers at night and on Sundays. That is not a fault, so it is not counted as one.\n"
        "- **Likely to fail this week** is a machine-learning forecast (LightGBM) of which working stations "
        "will report an outage in the next 7 days, tested on weeks the model never saw.\n"
        "- **Fast hubs about to fill up** is a second forecast: which fast-charging hubs with a free charge point "
        "now are most likely to have none in about an hour, from their live state and usual weekly pattern.\n"
        "- Figures cover only stations that publish live status; live data refreshes every 10 minutes."
    )

fresh = view[~view["state"].isin(NOT_JUDGED)]
points = fresh["live_points"].sum()
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Stations reporting live", f"{len(view):,}")
c2.metric("Charge points (fresh feeds)", f"{points:,}")
c3.metric("Available", f"{fresh['available'].sum() / max(points, 1):.0%}")
c4.metric("Out of service", f"{fresh['out_of_service'].sum() / max(points, 1):.1%}")
c5.metric("Stations with quiet feed", f"{(view['state'] == 'Feed quiet').mean():.0%}",
          help="The platform flags these feeds as outdated, so their status can't be trusted. "
               "They are excluded from the outage list.")

# --------------------------------------------------------------------------- #
# Needs attention now
# --------------------------------------------------------------------------- #
st.subheader("Needs attention now")
st.caption("Stations with charge points out of service and a fresh feed, ranked by estimated "
           "capacity offline (points down × the station's maximum power).")
outages = fresh[fresh["out_of_service"] > 0].copy()
outages["kw_offline"] = outages["out_of_service"] * outages["power_kw"].fillna(0)
outages = outages.sort_values(["kw_offline", "out_of_service"], ascending=False)
table = pd.DataFrame({
    "Operator": outages["operator_name"],
    "City": outages["city"],
    "Address": outages["address"],
    "Status": outages["state"],
    "Points down": outages["out_of_service"].astype(str) + " of " + outages["live_points"].astype(str),
    "Max power": outages["power_kw"],
    "Capacity offline": outages["kw_offline"],
    "Days down": outages["days_down"].fillna(0).astype(int),
    "Station ID": outages["station_id"].astype(str),
}).head(50)
full_width(
    st.dataframe, table, hide_index=True, height=390,
    column_config={
        "Max power": st.column_config.NumberColumn(format="%d kW"),
        "Capacity offline": st.column_config.NumberColumn(format="%d kW",
                                                          help="Points down × the station's maximum power"),
        "Days down": st.column_config.ProgressColumn(
            format=f"%d of {n_hist_days}", min_value=0, max_value=n_hist_days, width="medium",
            help=f"Days with at least one charge point out of service, last {n_hist_days} archive days"),
    },
)

# --------------------------------------------------------------------------- #
# Likely to fail this week (Phase 3 forecast)
# --------------------------------------------------------------------------- #
if risk is not None:
    st.subheader("Likely to fail this week")
    ev = hist_meta.get("risk_eval", {})
    tested = ""
    if ev:
        tested = (f" In tests on weeks the model never saw, {ev['test_summer']['prec@100']:.0%} (August) and "
                  f"{ev['test_september']['prec@100']:.0%} (September) of the 100 stations it ranked riskiest "
                  f"had an outage within 7 days, against {ev['test_summer']['base_rate']:.0%} and "
                  f"{ev['test_september']['base_rate']:.0%} of all stations.")
    st.caption(
        "Stations that are working right now, ranked by the forecast chance of an outage in the next 7 days "
        f"(LightGBM, as of {hist_meta.get('risk_as_of', '?')}).{tested} It mostly flags stations with "
        "repeated recent faults; first-time failures are hard to foresee."
    )
    working = fresh[fresh["state"] == "OK"].merge(risk, on="station_id", how="inner")
    working = working.sort_values("risk", ascending=False).head(50)
    rtable = pd.DataFrame({
        "Operator": working["operator_name"],
        "City": working["city"],
        "Address": working["address"],
        "Chance of outage": (working["risk"] * 100).round(0),
        "Outages, last 21 days": working["outages_21d"],
        "Last outage (before forecast)": working["days_since_out"].map(
            lambda d: "none in 21 days" if d > 21 else ("< 1 day" if d < 1 else f"{d:.0f} days")),
        "Max power": working["power_kw"],
        "Station ID": working["station_id"].astype(str),
    })
    full_width(
        st.dataframe, rtable, hide_index=True, height=390,
        column_config={
            "Chance of outage": st.column_config.ProgressColumn(
                format="%d%%", min_value=0, max_value=100, width="medium",
                help="Forecast probability of at least one charge point out of service in the next 7 days"),
            "Max power": st.column_config.NumberColumn(format="%d kW"),
        },
    )

# --------------------------------------------------------------------------- #
# Fast hubs: full in an hour? (Phase 4 forecast)
# --------------------------------------------------------------------------- #
MODEL_NEXT_HOUR = ROOT / "models" / "next_hour_lgbm.txt"


@st.cache_data(ttl=600, show_spinner=False)
def next_hour_scores(fetched_at: str):
    from evops.next_hour import score_live
    return score_live(live, hub_profile, MODEL_NEXT_HOUR, at=fetched_at)


if hub_profile is not None and MODEL_NEXT_HOUR.exists():
    st.subheader("Fast hubs about to fill up")
    nh = hist_meta.get("next_hour_eval", {})
    tested = ""
    if nh:
        tested = (f" For hubs with a free charge point now, it ranks better than the hub's usual pattern alone "
                  f"(AUC {nh['test_august']['lgbm_auc_not_full']:.2f} vs {nh['test_august']['profile_auc_not_full']:.2f} "
                  f"in August tests, {nh['test_october']['lgbm_auc_not_full']:.2f} vs "
                  f"{nh['test_october']['profile_auc_not_full']:.2f} in October).")
    st.caption(
        f"Fast-charging hubs (≥ {FAST_KW} kW, 2+ charge points) that still have a free charge point, ranked by how "
        f"likely they are to have none about an hour from now. LightGBM on each hub's live state and its usual "
        f"pattern by hour of the week.{tested} Full hubs are rare, so most hubs here will still have a free point: "
        "read it as a ranking, not an alarm."
    )
    scored = next_hour_scores(live_meta["fetched_at"])
    scored = scored[scored["station_id"].isin(view["station_id"])]
    st.caption(f"Fast hubs with no free charge point right now: **{int(scored['full_now'].sum()):,}** of "
               f"{len(scored):,} (left out of the list below).")
    top = scored[scored["full_now"] == 0].sort_values("p_full_1h", ascending=False).head(30).merge(
        live[["station_id", "operator_name", "city", "address"]], on="station_id", how="left")
    htable = pd.DataFrame({
        "Operator": top["operator_name"],
        "City": top["city"],
        "Address": top["address"],
        "Free now": top["available"].astype(int).astype(str) + " of " + top["live_points"].astype(int).astype(str),
        "Full-in-1-hour score": (top["p_full_1h"] * 100).round(0),
        "Usually full at this hour": (top["prof_full_next"] * 100).round(0),
        "Max power": top["power_kw"],
    })
    full_width(
        st.dataframe, htable, hide_index=True, height=390,
        column_config={
            "Full-in-1-hour score": st.column_config.ProgressColumn(
                format="%d", min_value=0, max_value=100, width="medium",
                help="Model score for no free charge point about 60 minutes from now. The model is overconfident at "
                     "the top: in tests, hubs scoring above 50 were full an hour later 36-45% of the time."),
            "Usually full at this hour": st.column_config.NumberColumn(
                format="%d%%", help="How often this hub had no free charge point in that hour of the week, last 3 weeks"),
            "Max power": st.column_config.NumberColumn(format="%d kW"),
        },
    )

# --------------------------------------------------------------------------- #
# Map and quiet feeds
# --------------------------------------------------------------------------- #
left, right = st.columns([3, 2])
with left:
    st.subheader("Map")
    shown = st.multiselect("Show", list(STATE_COLOURS), default=["Partly down", "Fully down", "Feed quiet"])
    pts = view[view["state"].isin(shown) & view["lat"].notna()].copy()
    pts["colour"] = pts["state"].map(lambda s: [int(STATE_COLOURS[s][i:i + 2], 16) for i in (1, 3, 5)] + [190])
    layer = pdk.Layer("ScatterplotLayer", pts, get_position="[lon, lat]", get_fill_color="colour",
                      get_radius=600, radius_min_pixels=2, radius_max_pixels=8, pickable=True)
    st.pydeck_chart(pdk.Deck(
        layers=[layer],
        initial_view_state=pdk.ViewState(latitude=51.2, longitude=10.4, zoom=5),
        tooltip={"text": "{operator_name}\n{city}, {address}\n{state}: {out_of_service} of {live_points} down"},
    ), height=480)
    st.caption("  ·  ".join(f"<span style='color:{c}'>●</span> {s}" for s, c in STATE_COLOURS.items()),
               unsafe_allow_html=True)

with right:
    st.subheader("Feeds gone quiet")
    st.caption("Operators with the most stations whose feed is flagged outdated. "
               "Their status is unknown, not necessarily broken.")
    quiet = (view.assign(is_quiet=view["state"].eq("Feed quiet").astype(int))
             .groupby("operator_name")
             .agg(stations=("station_id", "size"), quiet=("is_quiet", "sum")))
    quiet = quiet[quiet["quiet"] > 0].sort_values("quiet", ascending=False).head(10)
    quiet["share"] = quiet["quiet"] / quiet["stations"]
    if quiet.empty:
        st.info("No quiet feeds in the current filter.")
    else:
        fig = go.Figure(go.Bar(
            x=quiet["quiet"][::-1], y=quiet.index[::-1], orientation="h", marker_color=QUIET,
            text=[f"{q:,} ({s:.0%})" for q, s in zip(quiet["quiet"][::-1], quiet["share"][::-1])],
            textposition="outside", cliponaxis=False,
            hovertemplate="%{y}: %{x} quiet stations<extra></extra>",
        ))
        fig.update_layout(height=480, margin=dict(l=0, r=20, t=10, b=30),
                          xaxis=dict(title="stations with a quiet feed", range=[0, quiet["quiet"].max() * 1.35]),
                          plot_bgcolor="rgba(0,0,0,0)")
        full_width(st.plotly_chart, fig)

# --------------------------------------------------------------------------- #
# Station drill-down
# --------------------------------------------------------------------------- #
st.subheader("Station history")
options = outages.head(50)
if options.empty:
    st.info("No outages in the current filter.")
else:
    labels = {f"{r.operator_name} · {r.city} · {r.address} ({r.out_of_service} of {r.live_points} down) #{r.station_id}":
              r.station_id for r in options.itertuples()}
    sid = labels[st.selectbox("Station (from the list above)", list(labels))]
    h = days[days["station_id"] == sid].sort_values("day")
    if h.empty:
        st.info("This station is not in the archive's last days.")
    else:
        fig = go.Figure()
        fig.add_scatter(x=h["day"], y=h["available_share"] * 100, name="Available", line=dict(color=GOOD, width=2))
        fig.add_scatter(x=h["day"], y=h["out_of_service_share"] * 100, name="Out of service",
                        line=dict(color=CRITICAL, width=2))
        fig.update_layout(height=320, margin=dict(l=0, r=0, t=10, b=30), yaxis=dict(title="% of charge points", range=[0, 100]),
                          legend=dict(orientation="h", y=1.1), hovermode="x unified", plot_bgcolor="rgba(0,0,0,0)")
        full_width(st.plotly_chart, fig)

# --------------------------------------------------------------------------- #
# Network history
# --------------------------------------------------------------------------- #
with st.expander("Network history and data quality"):
    s = summary.copy()
    fig = go.Figure()
    for col, name, colour in [("share_available", "Available", GOOD), ("share_occupied", "Occupied", BLUE),
                              ("share_out_of_service", "Out of service", CRITICAL), ("share_unknown", "Unknown", QUIET)]:
        fig.add_scatter(x=s["day"], y=s[col] * 100, name=name, stackgroup="one", line=dict(width=0.5, color=colour))
    window = hist_meta.get("model_window") or {}
    if window:
        fig.add_vrect(x0=window["start"], x1=window["end"], fillcolor="rgba(0,0,0,0.06)", line_width=0,
                      annotation_text="model training window", annotation_position="top left")
    fig.update_layout(height=340, margin=dict(l=0, r=0, t=20, b=30), yaxis=dict(title="% of live charge points", range=[0, 100]),
                      legend=dict(orientation="h", y=1.12), hovermode="x unified", plot_bgcolor="rgba(0,0,0,0)")
    full_width(st.plotly_chart, fig)
    st.caption("Before July, many operators reported most charge points as 'unknown', so only "
               "30 June – 14 August is used for modelling. See notebooks/01_data_quality.ipynb.")

st.markdown("---")
st.caption(
    'Data: MobiData BW (NVBW), "Gebündelte Daten E-Ladesäulen Baden-Württemberg", '
    "[mobidata-bw.de](https://mobidata-bw.de/dataset/e-ladesaulen), licence "
    "[dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0). Includes data from EnBW AG and Bundesnetzagentur "
    "(CC BY 4.0). Aggregated and modified. Not affiliated with any operator."
)
