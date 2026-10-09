# Hide & Seek region packs

Map data for the Hide & Seek companion app, prepared ahead of time for every region in the
world so games don't depend on busy public map servers.

Each pack holds one region's transit stations, the places used by matching and measuring
questions (museums, parks, hospitals…), lines (transit routes, coastlines, water, borders)
and administrative boundaries. A pack is a raw-deflated SQLite database with one row per
OpenStreetMap element, indexed by position, so the app reads only what is near a game.

## How it's built

A GitHub Action runs once a month (or by hand, optionally for a few regions):

1. `build_all.py plan` splits Geofabrik's regions (countries, or states and provinces for
   large countries) into balanced batches.
2. Each batch downloads its extracts from Geofabrik, cuts them down with `osmium tags-filter`
   (`prefilter.py` has the same filter for local use) and builds packs with `build_pack.py`.
3. The packs and `index.json`, the list the app reads, are published as a release.

To build one region locally:

```bash
python3 -m venv .venv && .venv/bin/pip install osmium==4.3.1
.venv/bin/python build_all.py build out us-washington
```

The filters in `build_pack.py` must match the app's own map queries, so a game gives the
same answers with or without a pack.

## Data

Map data © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, available
under the [Open Database License](https://opendatacommons.org/licenses/odbl/). Extracts from
[Geofabrik](https://download.geofabrik.de/).
