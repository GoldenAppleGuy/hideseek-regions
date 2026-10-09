#!/usr/bin/env python3
"""Builds region packs for Geofabrik's regions and the catalog the app reads.

  build_all.py plan <batches> [--only id,id…]   print a JSON list of region batches,
                                                 balanced by download size, for a job matrix
  build_all.py build <out dir> <id,id…>          download, filter and build these regions
  build_all.py index <packs dir> <download URL>  write index.json from the built packs

Regions are Geofabrik's smallest extracts, which together cover the world. Data ©
OpenStreetMap contributors, ODbL; extracts from download.geofabrik.de.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = "https://download.geofabrik.de/index-v1.json"
AGENT = {"User-Agent": "hideseek-regions (+https://github.com/GoldenAppleGuy/hideseek-regions)"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=120) as response:
        return response.read()


# Regions to build: these continents, or any region under these countries.
CONTINENTS = {"Europe", "North America"}
COUNTRIES = {"japan", "australia", "new-zealand"}

# Combined extracts that only partly overlap the regions inside them, so the test below misses them.
COMBINED = {"alps"}


def combined(raw, leaves):
    """Leaf regions that are mostly made of other leaf regions, such as "dach" (Germany,
    Austria and Switzerland) or "us-south". Building them would duplicate those regions."""
    from shapely.geometry import shape
    from shapely.ops import unary_union
    from shapely.strtree import STRtree
    shapes = {f["properties"]["id"]: shape(f["geometry"]).buffer(0)
              for f in raw if f["properties"]["id"] in leaves and f.get("geometry")}
    ids = list(shapes)
    geoms = [shapes[i] for i in ids]
    tree = STRtree(geoms)
    found = set(COMBINED)
    for a in ids:
        outer = shapes[a]
        inner = [geoms[j] for j in tree.query(outer)
                 if ids[j] != a and geoms[j].area < outer.area and outer.intersection(geoms[j]).area > 0.8 * geoms[j].area]
        if inner and outer.intersection(unary_union(inner)).area > 0.5 * outer.area:
            found.add(a)
    return found


def regions():
    """Geofabrik's leaf regions, with a readable group such as "North America › United States"."""
    raw = json.loads(get(INDEX))["features"]
    features = [f["properties"] for f in raw]
    for p in features:
        # Some regions, such as US states ("us/new-york"), are named by slug and filed under
        # the continent; the part before the slash is their real parent.
        if "/" in p["id"]:
            p["parent"] = p["id"].split("/")[0]
            if p["name"] == p["id"]:
                p["name"] = p["id"].split("/")[1].replace("-", " ").title()
    by_id = {p["id"]: p for p in features}
    parents = {p.get("parent") for p in features}
    leaves = {p["id"] for p in features if p["id"] not in parents and "pbf" in p.get("urls", {})}
    skip = combined(raw, leaves)
    result = []
    for p in features:
        if p["id"] not in leaves or p["id"] in skip:
            continue
        chain, ancestors, parent = [], {p["id"]}, p.get("parent")
        while parent in by_id:
            chain.insert(0, by_id[parent]["name"])
            ancestors.add(parent)
            parent = by_id[parent].get("parent")
        if not (chain and chain[0] in CONTINENTS) and not (ancestors & COUNTRIES):
            continue
        pbf = p["urls"]["pbf"]
        result.append({"id": p["id"].replace("/", "-"), "name": p["name"], "group": " › ".join(chain) or None,
                       "pbf": pbf, "poly": pbf.replace("-latest.osm.pbf", ".poly")})
    return result


def size(url):
    request = urllib.request.Request(url, method="HEAD", headers=AGENT)
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.headers.get("Content-Length", 0))


def plan(batches, only):
    chosen = [r for r in regions() if not only or r["id"] in only]
    for r in chosen:
        r["bytes"] = size(r["pbf"])
    # Largest first into the lightest batch keeps the jobs about even.
    groups = [[] for _ in range(min(batches, len(chosen)))]
    weights = [0] * len(groups)
    for r in sorted(chosen, key=lambda r: -r["bytes"]):
        i = weights.index(min(weights))
        groups[i].append(r["id"])
        weights[i] += r["bytes"]
    print(json.dumps([",".join(g) for g in groups if g]))


def download(url, path):
    with urllib.request.urlopen(urllib.request.Request(url, headers=AGENT), timeout=600) as response, \
            open(path + ".part", "wb") as f:
        shutil.copyfileobj(response, f, 1 << 20)
    os.rename(path + ".part", path)


def prefilter(source, target):
    """osmium-tool where installed (build machines), else the pyosmium version."""
    if shutil.which("osmium"):
        expressions = subprocess.run([sys.executable, os.path.join(HERE, "prefilter.py"), "--expressions"],
                                     check=True, capture_output=True, text=True).stdout.split()
        subprocess.run(["osmium", "tags-filter", "--overwrite", "-o", target, source, *expressions], check=True)
    else:
        subprocess.run([sys.executable, os.path.join(HERE, "prefilter.py"), source, target], check=True)


def build(out_dir, ids):
    wanted = set(ids.split(","))
    cache = os.path.join(HERE, "cache")
    os.makedirs(cache, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    for r in regions():
        if r["id"] not in wanted:
            continue
        print(f"== {r['id']}", flush=True)
        pbf, filtered, poly = (os.path.join(cache, r["id"] + suffix) for suffix in (".osm.pbf", ".filtered.osm.pbf", ".poly"))
        download(r["pbf"], pbf)
        download(r["poly"], poly)
        prefilter(pbf, filtered)
        os.remove(pbf)
        result = subprocess.run([sys.executable, os.path.join(HERE, "build_pack.py"), filtered, r["id"], r["name"], poly, out_dir],
                                check=True, capture_output=True, text=True)
        os.remove(filtered)
        summary = json.loads(result.stdout.strip().splitlines()[-1])
        print(json.dumps(summary)[:300], flush=True)
        with open(os.path.join(out_dir, r["id"] + ".json"), "w") as f:
            json.dump({"region": r["id"], "name": r["name"], "group": r["group"],
                       "bytes": summary["bytes"], "built": summary["built"]}, f)


def index(packs_dir, base_url):
    entries = []
    for name in sorted(os.listdir(packs_dir)):
        if name.endswith(".json") and name != "index.json":
            with open(os.path.join(packs_dir, name)) as f:
                entry = json.load(f)
            entry["url"] = f"{base_url.rstrip('/')}/{entry['region']}.hspack"
            entries.append(entry)
    with open(os.path.join(packs_dir, "index.json"), "w") as f:
        json.dump({"version": 2, "regions": entries}, f, indent=1)
    print(f"index: {len(entries)} regions")


def main():
    command = sys.argv[1]
    if command == "plan":
        only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else set()
        plan(int(sys.argv[2]), only)
    elif command == "build":
        build(sys.argv[2], sys.argv[3])
    elif command == "index":
        index(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
