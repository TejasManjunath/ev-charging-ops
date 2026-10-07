# EV Charging Network Operations

A near-real-time operations tool for public EV charging in Germany. It monitors charger status, flags stations that behave unusually, and predicts which stations will have no free connector in the next hour.

**Status:** Phase 1 complete: ingestion pipeline and data-quality analysis. Live dashboard link goes here in Phase 2.

## Who it's for

A charging-network operations manager who opens it in the morning and needs to know:

1. Which stations need attention now?
2. Which stations behave unlike their normal pattern?
3. Which stations will be full in the next hour?
4. Where is capacity persistently tight?

## Data

Open data from [MobiData BW](https://mobidata-bw.de/dataset/e-ladesaulen): live connector status (DATEX II / OCPI) and a daily archive since 28 January 2026. The archive covers about 36,000 stations across Germany, with counts of available, charging, out-of-order and unknown charge points per station and snapshot. How often each station is recorded varies over time, from every 5 minutes to every 2.5 hours. No scraping, no personal data.

## Findings

From [`notebooks/01_data_quality.ipynb`](notebooks/01_data_quality.ipynb), on 248 days (28 Jan – 2 Oct 2026):

1. **Live status was unreliable before July.** "Unknown" peaked at 86% of charge points on 19 May 2026; 93 of 248 days exceed 15% unknown and are excluded from modelling, even where coverage was good.
2. **Only 46 days are fit for an hourly model: 30 June – 14 August 2026.** That is the one stretch with Germany-wide coverage (≥ 20,000 stations), at least hourly capture and ≤ 15% unknown status.
3. **A fixed panel of 23,357 stations** (68% of the 34,118 seen in that window) reports on at least 90% of its days. Models use only this panel, so operators joining or leaving the feed don't look like changes in demand.

In that window, 7.9% of live charge points were out of service on an average day.

## Run it

```bash
pip install -r requirements.txt
python -m evops.check_feeds                 # checks the feeds, writes reports/feed_check.md
python -m evops.ingest --start 2026-01-28   # downloads and converts the full archive (~100 MB of Parquet)
jupyter nbconvert --to notebook --execute --inplace notebooks/01_data_quality.ipynb
```

The notebook writes `daily_summary.parquet`, `model_window.json` and `station_panel.parquet` to `data/processed/` for the later phases.

## Project structure

```
evops/            pipeline code (config, ingestion, cleaning, models)
app/              Streamlit dashboard
notebooks/        data-quality analysis
reports/          generated reports
data/             downloaded data (git-ignored)
```

## Limits

- Models train on 46 summer days at hourly resolution. Winter behaviour (cold weather, holidays) is not in the training data, so forecasts may not carry over to winter without retraining.
- Only stations whose operators publish live status are included; "static" charge points (no live status) are excluded from availability figures.
- The feed shows whether a connector is occupied, not how many drivers wanted one, so the project ranks saturated stations rather than estimating how many chargers to build.

## Decisions log

- 2026-10-04: Chose MobiData BW over scraping app data: open licence, live status and a daily archive.
- 2026-10-04: Archive check: Germany-wide (Swiss stations dropped); snapshot time = `datastore_updated_at`, because `status_last_updated` is stamped at export time. One day = 85 MB CSV, 1.2 MB Parquet.
- 2026-10-04: Excluded `bnetza_api` (static register, no live status; dumped ~58k static rows on 14–15 Sep). Capture frequency per station changes over the archive (~90/day early Aug, ~10/day mid-Aug to 27 Sep, ~26/day from 28 Sep), logged per day in `data/processed/manifest.csv`; models must resample to a fixed hourly grid.
- 2026-10-05: Modelling window 30 Jun – 14 Aug 2026 (46 days). Rule: ≥ 20,000 stations, ≥ 24 snapshots per station, ≤ 15% unknown status, gaps ≤ 2 days. Station panel: 23,357 stations present on ≥ 90% of window days.
- 2026-10-05: The notebook summarises the archive one day at a time with pandas + pyarrow instead of DuckDB, after Windows Smart App Control blocked DuckDB's compiled library. Same results, no special dependencies.

## Attribution

Data: MobiData BW (NVBW), "Gebündelte Daten E-Ladesäulen Baden-Württemberg", https://mobidata-bw.de/dataset/e-ladesaulen, licence [dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0). Includes data from EnBW AG and Bundesnetzagentur (CC BY 4.0). Data was aggregated and modified for this project.
