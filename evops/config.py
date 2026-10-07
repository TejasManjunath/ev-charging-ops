"""Central settings: endpoints, paths and thresholds.

Every URL can be overridden with an environment variable, which is handy for
testing against a local copy of the data.
"""

import os
from pathlib import Path

# --- Paths -----------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
LIVE_DIR = RAW_DIR / "live"
ARCHIVE_DIR = RAW_DIR / "archive"
PROCESSED_DIR = DATA_DIR / "processed"
REPORTS_DIR = ROOT / "reports"

# --- MobiData BW endpoints (open data, licence dl-de/by-2-0) -----------------
API_BASE = os.getenv("EVOPS_API_BASE", "https://api.mobidata-bw.de/ocpdb/api")
SOURCES_URL = os.getenv("EVOPS_SOURCES_URL", f"{API_BASE}/public/v1/sources")
REALTIME_URL = os.getenv(
    "EVOPS_REALTIME_URL", f"{API_BASE}/public/datex/v3.5/json/realtime"
)
STATIC_URL = os.getenv("EVOPS_STATIC_URL", f"{API_BASE}/public/datex/v3.5/json/static")

# Historical archive: dynamic status every 5 minutes since 28 Jan 2026.
ARCHIVE_BASE = os.getenv(
    "EVOPS_ARCHIVE_BASE", "https://mobidata-bw.de/daten/historisierung"
)
ARCHIVE_RESOURCE_ID = "89fb3a0b-cc42-48a6-b17a-8f1b46abccf1"
ARCHIVE_START = "2026-01-28"


def archive_day_url(day: str) -> str:
    """URL of one day's archive file. `day` is YYYY-MM-DD."""
    compact = day.replace("-", "")
    return f"{ARCHIVE_BASE}/tag/{compact[:4]}/{compact}_{ARCHIVE_RESOURCE_ID}.csv.gz"


def archive_week_url(year: int, week: int) -> str:
    """URL of one ISO calendar week's archive file."""
    return f"{ARCHIVE_BASE}/woche/{year}/{year}{week:02d}_{ARCHIVE_RESOURCE_ID}.csv.gz"


# --- HTTP etiquette -----------------------------------------------------------
USER_AGENT = "ev-charging-ops portfolio project (github.com/TejasManjunath)"
TIMEOUT_S = 60
# Never poll the live feed more often than this.
MIN_POLL_INTERVAL_S = 300

# --- Data-quality thresholds -------------------------------------------------
# A source whose realtime data is older than this is treated as stale.
STALE_AFTER_MIN = 30

ATTRIBUTION = (
    'Data: MobiData BW (NVBW), "Gebündelte Daten E-Ladesäulen Baden-Württemberg", '
    "https://mobidata-bw.de/dataset/e-ladesaulen, licence dl-de/by-2-0 "
    "(www.govdata.de/dl-de/by-2-0). Includes data from EnBW AG and "
    "Bundesnetzagentur (CC BY 4.0). Data was aggregated and modified for this project."
)
