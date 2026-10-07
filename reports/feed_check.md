# Feed check, 2026-10-04 17:33 UTC

**Verdict: GO**

## 1. Sources and freshness — OK

10 of 12 sources have live data newer than 30 minutes.

| source | name | realtime_status | realtime_age_min | fresh | licence |
|---|---|---|---|---|---|
| datex2_chargecloud | Chargecloud Datex II | ACTIVE | 0 | yes | CC-0 |
| datex2_taubert | Taubert Consulting Datex II | ACTIVE | 0 | yes | CC-0 |
| datex2_tesla | Tesla Datex II | ACTIVE | 0 | yes | CC-0 |
| heilbronn_neckarbogen | Heilbronn Neckarbogen | ACTIVE | 0 | yes | not stated |
| datex2_e_clearing_net | e-clearing.net Datex II | ACTIVE | 1 | yes | CC-0 |
| datex2_ecomovement | EcoMovement Datex II | ACTIVE | 1 | yes | CC-0 |
| datex2_midorion | Midorion Datex II | ACTIVE | 1 | yes | CC-0 |
| eaaze_pbw | PBW | ACTIVE | 1 | yes | not stated |
| ochp_albwerk | Albwerk | ACTIVE | 1 | yes | not stated |
| opendata_swiss | OpenData Swiss | ACTIVE | 1 | yes | not stated |
| datex2_enbw | EnBW Datex II | ACTIVE | 1113 | no | CC BY 4.0 |
| bnetza_api | Bundesnetzagentur | PROVISIONED |  | no | CC BY 4.0 |

## 2. Live snapshot — OK

Downloaded 118.6 KB, saved to `data\raw\live\realtime_20261004T173310Z.json.gz`.

**Status values found:**

| field | value | count |
|---|---|---|
| status | unknown | 176 |
| status | available | 133 |
| status | outOfOrder | 26 |
| status | charging | 14 |
| status | inoperative | 2 |

**JSON outline (first 80 lines):**

```
payload: dict
  versionG: str
  modelBaseVersionG: str
  profileNameG: str
  profileVersionG: str
  aegiEnergyInfrastructureStatusPublication: dict
    lang: str
    publicationTime: str
    publicationCreator: dict
      country: str
      nationalIdentifier: str
    energyInfrastructureSiteStatus: list [100 items]
      reference: dict
        targetClass: str
        idG: str
        versionG: str
      lastUpdated: str
      energyInfrastructureStationStatus: list [1 items]
```

## 3. Historical archive — OK

Loaded day 2026-10-02 from `https://mobidata-bw.de/daten/historisierung/tag/2026/20261002_89fb3a0b-cc42-48a6-b17a-8f1b46abccf1.csv.gz`.

File size 84.9 MB compressed, 1,163,648 rows.

Detected columns: source = `source`, time = `datastore_updated_at`, status = `status_last_updated`.

**Columns:**

| column | dtype |
|---|---|
| _id | int64 |
| FID | object |
| id | int64 |
| source | object |
| name | object |
| operator_name | object |
| address | object |
| postal_code | object |
| city | object |
| state | float64 |
| chargepoint_available_count | int64 |
| chargepoint_charging_count | int64 |
| chargepoint_unknown_count | int64 |
| chargepoint_inoperative_count | int64 |
| chargepoint_outoforder_count | int64 |
| chargepoint_static_count | int64 |
| chargepoint_reserved_count | int64 |
| chargepoint_bike_count | float64 |
| max_electric_power | int64 |
| geometry | object |
| datastore_updated_at | object |
| _full_text | float64 |
| official_region_code | float64 |
| max_power_value | float64 |
| go_live_date | float64 |
| station_id | int64 |
| status_last_updated | object |
| realtime_data_outdated | object |
| last_updated | object |

**First 3 rows:**

| _id | FID | id | source | name | operator_name | address | postal_code | city | state | chargepoint_available_count | chargepoint_charging_count | chargepoint_unknown_count | chargepoint_inoperative_count | chargepoint_outoforder_count | chargepoint_static_count | chargepoint_reserved_count | chargepoint_bike_count | max_electric_power | geometry | datastore_updated_at | _full_text | official_region_code | max_power_value | go_live_date | station_id | status_last_updated | realtime_data_outdated | last_updated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 565692647 | charge_points.160830 | 160830 | opendata_swiss | PP Sporthalle 2 | eCarUp | Schulhausstrasse | 8762 | Schwanden | nan | 2 | 0 | 0 | 0 | 0 | 0 | 0 | nan | 22000 | POINT (9.069737 46.99319) | 2026-10-02 00:04:46+02 | nan | nan | nan | nan | 160830 | 2026-10-04T02:07:41.906Z | False | 2026-10-04T02:07:41.906Z |
| 565692648 | charge_points.160894 | 160894 | opendata_swiss | PP24 | eCarUp | Alter Schulhauspl. 3 | 8853 | Lachen | nan | 1 | 0 | 0 | 0 | 0 | 0 | 0 | nan | 22000 | POINT (8.850518 47.192888) | 2026-10-02 00:04:46+02 | nan | nan | nan | nan | 160894 | 2026-10-04T02:07:36.501Z | False | 2026-10-04T02:07:36.501Z |
| 565692649 | charge_points.160921 | 160921 | opendata_swiss | EV LINK 22kW - Hôtel ADRIATICA - clients | eCarUp | Rue Sautter 21 | 1205 | Genève | nan | 0 | 1 | 0 | 0 | 0 | 0 | 0 | nan | 22000 | POINT (6.149836 46.195081) | 2026-10-02 00:04:46+02 | nan | nan | nan | nan | 160921 | 2026-10-04T02:07:36.596Z | False | 2026-10-04T02:07:36.596Z |

**Rows per source per day:**

| source | 2026-10-01 | 2026-10-02 |
|---|---|---|
| datex2_chargecloud | 16283 | 191385 |
| datex2_e_clearing_net | 13662 | 160986 |
| datex2_ecomovement | 35814 | 427411 |
| datex2_enbw | 5158 | 82942 |
| datex2_midorion | 19 | 235 |
| datex2_taubert | 248 | 2844 |
| datex2_tesla | 654 | 14971 |
| eaaze_pbw | 169 | 1830 |
| heilbronn_neckarbogen | 48 | 537 |
| ochp_albwerk | 250 | 2831 |
| opendata_swiss | 17635 | 187736 |

**Status values:**

| status | rows |
|---|---|
| missing | 11319 |
| 2026-09-14T18:56:21Z | 5612 |
| 2026-09-30T22:48:59Z | 1035 |
| 2026-09-30T22:49:05Z | 805 |
| 2026-10-04T04:50:20Z | 772 |
| 2026-10-03T19:15:41Z | 749 |
| 2026-10-04T04:49:15Z | 683 |
| 2026-10-03T19:15:45Z | 670 |
| 2026-10-03T19:15:43Z | 659 |
| 2026-10-03T19:15:39Z | 619 |
| 2026-10-03T19:15:44Z | 600 |
| 2026-10-03T19:15:48Z | 591 |
| 2026-10-03T19:15:40Z | 589 |
| 2026-10-03T19:15:47Z | 580 |
| 2026-09-30T22:48:55Z | 575 |

---
Data: MobiData BW (NVBW), "Gebündelte Daten E-Ladesäulen Baden-Württemberg", https://mobidata-bw.de/dataset/e-ladesaulen, licence dl-de/by-2-0 (www.govdata.de/dl-de/by-2-0). Includes data from EnBW AG and Bundesnetzagentur (CC BY 4.0). Data was aggregated and modified for this project.