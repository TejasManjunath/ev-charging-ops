"""Phase 2, step 1: find the best way to get the LIVE state of every station.

Tests three endpoints and writes reports/live_probe.md:

  A. The GeoServer "charge_points" layer as CSV. The daily archive is a
     snapshot of this layer, so if it works, live and history share one format.
  B. The DATEX II realtime feed: how many sites per request, and which paging
     parameters it accepts.
  C. The OCPI 3.0 EVSE endpoint: page size and paging headers.

Run from the repo root:
    python -m evops.probe_live

It downloads at most a few tens of MB and makes about 12 requests.
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import requests

from evops import config

import os

WFS_BASE = os.getenv(
    "EVOPS_WFS_BASE",
    "https://api.mobidata-bw.de/geoserver/MobiData-BW/ows"
    "?service=WFS&version=2.0.0&request=GetFeature&typeName=MobiData-BW%3Acharge_points",
)
WFS_CSV = WFS_BASE + "&outputFormat=csv"
WFS_HITS = WFS_BASE + "&resultType=hits"
DATEX_RT = config.REALTIME_URL
OCPI_EVSES = f"{config.API_BASE}/ocpi/3.0/evses"

# Columns the archive (and so the rest of the pipeline) relies on
ARCHIVE_COLS = [
    "station_id", "source", "name", "operator_name", "postal_code", "city", "geometry",
    "max_electric_power", "chargepoint_available_count", "chargepoint_charging_count",
    "chargepoint_reserved_count", "chargepoint_outoforder_count",
    "chargepoint_inoperative_count", "chargepoint_unknown_count", "chargepoint_static_count",
    "realtime_data_outdated", "status_last_updated",
]
PAGING_HEADERS = ["link", "x-total-count", "x-limit", "x-offset", "x-pagination", "content-length"]


def get(url: str, **params):
    t0 = time.time()
    r = requests.get(url, params=params or None, headers={"User-Agent": config.USER_AGENT},
                     timeout=config.TIMEOUT_S)
    return r, time.time() - t0


def headers_of(r) -> dict:
    return {k: v for k, v in r.headers.items() if k.lower() in PAGING_HEADERS}


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_(empty)_"
    cols = [str(c) for c in df.columns]
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join("" if pd.isna(v) else str(v) for v in r) + " |"
             for r in df.itertuples(index=False)]
    return "\n".join(rows)


# --------------------------------------------------------------------------- #
# A. GeoServer charge_points layer
# --------------------------------------------------------------------------- #
def probe_wfs(now: datetime) -> str:
    out = []
    hits, _ = get(WFS_HITS)
    matched = None
    if hits.ok:
        txt = hits.text
        for key in ("numberMatched=\"", "numberOfFeatures=\""):
            if key in txt:
                matched = txt.split(key, 1)[1].split("\"", 1)[0]
                break
    out.append(f"Features the server says exist (resultType=hits): **{matched}**")

    r, secs = get(WFS_CSV)
    out.append(f"CSV download: HTTP {r.status_code}, {len(r.content) / 1e6:.1f} MB in {secs:.0f}s")
    if not r.ok:
        out.append(f"Body start: `{r.text[:300]}`")
        return "\n\n".join(out)

    df = pd.read_csv(io.BytesIO(r.content), low_memory=False)
    out.append(f"Rows: **{len(df):,}**, columns: {len(df.columns)}")
    missing = [c for c in ARCHIVE_COLS if c not in df.columns]
    out.append("Archive columns missing from the live layer: "
               + (", ".join(f"`{c}`" for c in missing) if missing else "**none**"))
    out.append("All columns: " + ", ".join(f"`{c}`" for c in df.columns))

    if "source" in df.columns:
        src = df.groupby("source").size().rename("rows").reset_index().sort_values("rows", ascending=False)
        out.append("Rows per source:\n\n" + md_table(src))

    for col in ("status_last_updated", "last_updated", "datastore_updated_at"):
        if col in df.columns:
            ts = pd.to_datetime(df[col], utc=True, errors="coerce", format="mixed")
            if ts.notna().any():
                age = (now - ts).dt.total_seconds() / 60
                out.append(
                    f"`{col}`: newest {ts.max()}, median age {age.median():.0f} min, "
                    f"{(age <= 30).mean():.0%} of rows updated in the last 30 min"
                )

    status_cols = [c for c in df.columns if c.startswith("chargepoint_") and c.endswith("_count")]
    if status_cols:
        sums = df[status_cols].sum().astype(int).rename("charge points").reset_index()
        out.append("Charge points by status right now:\n\n" + md_table(sums))

    out.append("First 2 rows:\n\n" + md_table(df.head(2).astype(str)))
    return "\n\n".join(out)


# --------------------------------------------------------------------------- #
# B. DATEX II realtime
# --------------------------------------------------------------------------- #
def count_sites(payload) -> int | None:
    try:
        pub = payload["payload"]["aegiEnergyInfrastructureStatusPublication"]
        return len(pub["energyInfrastructureSiteStatus"])
    except (KeyError, TypeError):
        return None


def probe_datex() -> str:
    rows = []
    first_ids = {}
    for label, params in [
        ("default", {}),
        ("limit=1000", {"limit": 1000}),
        ("limit=1000&offset=1000", {"limit": 1000, "offset": 1000}),
        ("offset=100", {"offset": 100}),
        ("page=2", {"page": 2}),
    ]:
        try:
            r, secs = get(DATEX_RT, **params)
            body = r.json() if r.ok else None
            n = count_sites(body) if body else None
            first = None
            if n:
                first = body["payload"]["aegiEnergyInfrastructureStatusPublication"][
                    "energyInfrastructureSiteStatus"][0].get("reference", {}).get("idG")
            first_ids[label] = first
            extra_keys = sorted(set(body.keys()) - {"payload"}) if isinstance(body, dict) else []
            rows.append({"request": label, "http": r.status_code, "sites": n, "first site id": first,
                         "seconds": round(secs, 1), "paging headers": json.dumps(headers_of(r)),
                         "other top-level keys": ", ".join(extra_keys)})
        except Exception as exc:  # report, don't stop
            rows.append({"request": label, "http": type(exc).__name__})
    note = ("If 'first site id' changes between requests, that parameter really pages; "
            "if 'sites' grows with limit=1000, the default page size is the cap.")
    return note + "\n\n" + md_table(pd.DataFrame(rows))


# --------------------------------------------------------------------------- #
# C. OCPI 3.0 EVSEs
# --------------------------------------------------------------------------- #
def probe_ocpi() -> str:
    rows = []
    for label, params in [("default", {}), ("limit=1000", {"limit": 1000}),
                          ("offset=1000&limit=1000", {"offset": 1000, "limit": 1000})]:
        try:
            r, secs = get(OCPI_EVSES, **params)
            body = r.json() if r.ok else None
            data = body.get("data", body.get("items")) if isinstance(body, dict) else body
            n = len(data) if isinstance(data, list) else None
            keys = sorted(body.keys()) if isinstance(body, dict) else []
            rows.append({"request": label, "http": r.status_code, "items": n, "seconds": round(secs, 1),
                         "paging headers": json.dumps(headers_of(r)), "body keys": ", ".join(keys)})
            if label == "default" and isinstance(data, list) and data:
                sample = json.dumps(data[0])[:600]
        except Exception as exc:
            rows.append({"request": label, "http": type(exc).__name__})
    text = md_table(pd.DataFrame(rows))
    if "sample" in locals():
        text += f"\n\nFirst item (truncated):\n\n```json\n{sample}\n```"
    return text


def main() -> int:
    now = datetime.now(timezone.utc)
    sections = []
    for title, fn in [("A. GeoServer charge_points layer (CSV)", lambda: probe_wfs(now)),
                      ("B. DATEX II realtime feed", probe_datex),
                      ("C. OCPI 3.0 EVSEs", probe_ocpi)]:
        print(f"{title} ...")
        try:
            sections.append(f"## {title}\n\n{fn()}")
            print("  done")
        except Exception as exc:
            sections.append(f"## {title}\n\n**Failed:** `{type(exc).__name__}: {exc}`")
            print(f"  FAILED: {exc}")
    report = f"# Live feed probe, {now:%Y-%m-%d %H:%M} UTC\n\n" + "\n\n".join(sections)
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / "live_probe.md"
    path.write_text(report, encoding="utf-8")
    print(f"\nReport written to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
