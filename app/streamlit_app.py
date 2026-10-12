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
STATE_COLOURS = {"OK": GOOD, "Partly down": SERIOUS, "Fully down": CRITICAL, "Feed quiet": QUIET}

st.set_page_config(page_title="EV Charging Operations · Germany", layout="wide")


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
    return days, summary, meta


live, live_meta = load_live()
days, summary, hist_meta = load_history()
n_hist_days = days["day"].nunique()
chronic = days.groupby("station_id")["any_out_of_service"].sum().rename("days_down")
live = live.merge(chronic, on="station_id", how="left")

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
           "with outages ranked by impact and stale feeds kept apart.")

fresh = view[view["state"] != "Feed quiet"]
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
st.caption(f"Stations with charge points out of service and a fresh feed, ranked by impact: "
           f"points down, counted double at fast chargers (≥ {FAST_KW} kW). "
           f"'Days down' = days with an outage in the last {n_hist_days} archive days.")
outages = fresh[fresh["out_of_service"] > 0].copy()
outages["impact"] = outages["out_of_service"] * (1 + (outages["power_kw"] >= FAST_KW))
outages = outages.sort_values(["impact", "out_of_service"], ascending=False)
table = outages.head(50).assign(
    down=lambda d: d["out_of_service"].astype(str) + " of " + d["live_points"].astype(str),
    days_down=lambda d: d["days_down"].fillna(0).astype(int).astype(str) + f" of {n_hist_days}",
)[["operator_name", "city", "address", "down", "power_kw", "days_down", "station_id"]]
table.columns = ["Operator", "City", "Address", "Points down", "Max kW", "Days down", "Station ID"]
full_width(st.dataframe, table, hide_index=True, height=360)

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
