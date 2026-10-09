#!/usr/bin/env python3
"""Builds a Hide & Seek region pack from an OpenStreetMap extract.

A pack holds, for one region, the same elements the app would otherwise ask the public
Overpass servers for, in Overpass's own JSON shape, so the app processes both the same way:

  stations    transit stops per transit mode
  places      point categories (museums, parks, …), with Overpass-style centers
  lines       transit routes, high-speed rail, coastline, named water, admin borders
  boundaries  administrative boundaries, for play areas and "same division" questions

The filters below must match the selectors in HideSeek/Model/Rules.swift and the queries in
HideSeek/Services/Services.swift.

Usage: build_pack.py <extract.osm.pbf> <region id> <region name> <coverage.poly> <out dir>
Data © OpenStreetMap contributors, ODbL.
"""
import datetime
import json
import math
import os
import re
import sqlite3
import sys
import zlib

import osmium

PACK_VERSION = 2
# Detail kept on lines and outlines, in meters. Small enough not to change answers.
LINE_TOLERANCE_M = 5

TRANSIT_ROUTES = {"train", "subway", "light_rail", "tram", "monorail"}
ADMIN_LEVELS = range(2, 11)


# ---------------------------------------------------------------- filters

def no_disused(t):
    return "disused" not in t


def station_ok(t):
    return t.get("station") not in ("disused", "proposed", "construction")


def mode_matches(kind, t):
    """Transit modes, as TransitMode.selectors. kind is 'n', 'w' or 'r'."""
    modes = []
    if (t.get("railway") in ("station", "halt") and no_disused(t) and station_ok(t)) or \
            (t.get("public_transport") == "station" and t.get("station") == "subway" and no_disused(t)):
        modes.append("rail")
    if kind == "n" and t.get("railway") == "tram_stop":
        modes.append("tram")
    if kind == "n" and (t.get("highway") == "bus_stop" or
                        (t.get("public_transport") == "platform" and t.get("bus") == "yes")):
        modes.append("bus")
    if t.get("amenity") == "ferry_terminal":
        modes.append("ferry")
    return modes


def not_honorary(value):
    return value is None or "honorary" not in value


def categories_matching(kind, t):
    """Point categories, as PlaceCategory.selectors."""
    found = []
    if t.get("aeroway") == "aerodrome" and "iata" in t:
        found.append("airport")
    if t.get("railway") == "station":
        found.append("railStation")
    if kind == "n" and t.get("natural") == "peak":
        found.append("mountain")
    if t.get("leisure") == "park":
        found.append("park")
    if t.get("tourism") == "theme_park":
        found.append("amusementPark")
    if t.get("tourism") == "zoo":
        found.append("zoo")
    if t.get("tourism") == "aquarium":
        found.append("aquarium")
    if t.get("leisure") == "golf_course":
        found.append("golfCourse")
    if t.get("tourism") == "museum":
        found.append("museum")
    if t.get("amenity") == "cinema":
        found.append("movieTheater")
    if t.get("amenity") == "hospital":
        found.append("hospital")
    if t.get("amenity") == "library":
        found.append("library")
    if (t.get("office") == "diplomatic" and not_honorary(t.get("diplomatic")) and not_honorary(t.get("consulate"))) or \
            (t.get("amenity") == "embassy" and not_honorary(t.get("diplomatic"))):
        found.append("consulate")
    return found


KEPT_TAGS = {"name", "ref", "route", "natural", "admin_level", "boundary", "iata", "railway", "station"}


def slim(tags):
    return {k: v for k, v in tags.items() if k in KEPT_TAGS}


# ---------------------------------------------------------------- geometry

def r6(x):
    return round(x, 6)


def simplify(points, tolerance_m=LINE_TOLERANCE_M):
    """Douglas-Peucker on [(lat, lon)], measured in local meters."""
    if len(points) < 3:
        return points
    lat0 = math.radians(points[0][0])
    kx, ky = 111_320 * math.cos(lat0), 110_540
    xy = [(lon * kx, lat * ky) for lat, lon in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        far, far_d = a, 0.0
        for i in range(a + 1, b):
            px, py = xy[i]
            d = math.hypot(px - ax, py - ay) if length == 0 else abs(dy * px - dx * py + bx * ay - by * ax) / length
            if d > far_d:
                far, far_d = i, d
        if far_d > tolerance_m:
            keep[far] = True
            stack += [(a, far), (far, b)]
    return [p for p, k in zip(points, keep) if k]


def geometry(points):
    return [{"lat": r6(lat), "lon": r6(lon)} for lat, lon in simplify(points)]


def bbox_center(points):
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return {"lat": r6((min(lats) + max(lats)) / 2), "lon": r6((min(lons) + max(lons)) / 2)}


# ---------------------------------------------------------------- reading

def read_relations(pbf):
    """Pass 1: relations we need, and which ways and nodes they use."""
    relations, need_ways, need_nodes = {}, set(), set()
    for r in osmium.FileProcessor(pbf, osmium.osm.RELATION):
        t = dict(r.tags)
        purposes = set()
        if t.get("type") == "route" and t.get("route") in TRANSIT_ROUTES:
            purposes.add("transit")
        if t.get("boundary") == "administrative" and t.get("name") and t.get("admin_level", "").isdigit() \
                and int(t["admin_level"]) in ADMIN_LEVELS:
            purposes.add("boundary")
        if t.get("natural") == "water" and t.get("name"):
            purposes.add("water")
        if categories_matching("r", t) or mode_matches("r", t):
            purposes.add("point")
        if not purposes:
            continue
        members = [(m.type, m.ref, m.role) for m in r.members]
        relations[r.id] = (purposes, t, members)
        for kind, ref, _ in members:
            if kind == "w":
                need_ways.add(ref)
            elif kind == "n":
                need_nodes.add(ref)
    return relations, need_ways, need_nodes


class Elements:
    """Pass 2: nodes and ways, with locations. Untagged nodes are dropped in C++ before
    they reach Python, which is most of the file."""

    def __init__(self, need_ways, need_nodes):
        self.need_ways, self.need_nodes = need_ways, need_nodes
        self.way_points = {}
        self.node_points = {}
        self.stations = {m: [] for m in ("rail", "tram", "bus", "ferry")}
        self.places = {}
        self.lines = {"highSpeedRail": [], "coastline": [], "water": []}

    def read(self, pbf):
        locations = osmium.index.create_map("flex_mem")
        processor = osmium.FileProcessor(pbf, osmium.osm.NODE | osmium.osm.WAY) \
            .with_locations(locations) \
            .with_filter(osmium.filter.EmptyTagFilter().enable_for(osmium.osm.NODE))
        for obj in processor:
            if obj.is_node():
                self.node(obj)
            else:
                self.way(obj)
        # Relation member nodes may be untagged (prefilter.py strips their tags), so they
        # never reach node(); their positions are in the location index either way.
        for nid in self.need_nodes:
            if nid not in self.node_points:
                try:
                    location = locations.get(nid)
                except KeyError:
                    continue
                if location.valid():
                    self.node_points[nid] = (location.lat, location.lon)

    def point_element(self, kind, oid, t, lat=None, lon=None, center=None):
        e = {"type": {"n": "node", "w": "way", "r": "relation"}[kind], "id": oid, "tags": slim(t)}
        if center:
            e["center"] = center
        else:
            e["lat"], e["lon"] = r6(lat), r6(lon)
        return e

    def node(self, n):
        if not n.location.valid():
            return
        lat, lon = n.location.lat, n.location.lon
        if n.id in self.need_nodes:
            self.node_points[n.id] = (lat, lon)
        t = dict(n.tags)
        for mode in mode_matches("n", t):
            self.stations[mode].append(self.point_element("n", n.id, t, lat, lon))
        for category in categories_matching("n", t):
            self.places.setdefault(category, []).append(self.point_element("n", n.id, t, lat, lon))

    def way(self, w):
        needed = w.id in self.need_ways
        if not needed and not w.tags:
            return
        points = [(nd.lat, nd.lon) for nd in w.nodes if nd.location.valid()]
        if not points:
            return
        if needed:
            self.way_points[w.id] = points
        t = dict(w.tags)
        if not t:
            return
        modes, categories = mode_matches("w", t), categories_matching("w", t)
        if modes or categories:
            center = bbox_center(points)
            for mode in modes:
                self.stations[mode].append(self.point_element("w", w.id, t, center=center))
            for category in categories:
                self.places.setdefault(category, []).append(self.point_element("w", w.id, t, center=center))
        if len(points) < 2:
            return
        if t.get("railway") == "rail" and t.get("highspeed") == "yes":
            self.lines["highSpeedRail"].append(self.line(w.id, t, points))
        if t.get("natural") == "coastline":
            self.lines["coastline"].append(self.line(w.id, t, points))
        if t.get("natural") == "water" and t.get("name"):
            self.lines["water"].append(self.line(w.id, t, points))

    @staticmethod
    def line(wid, t, points):
        return {"type": "way", "id": wid, "tags": slim(t), "geometry": geometry(points)}


def is_complete(members, ways, roles=("outer", "inner", "")):
    """Whether every outline piece is in the extract. Boundaries cut off at the region's
    edge would give wrong inside tests, so the app asks the server for those instead."""
    return all(ref in ways for kind, ref, role in members if kind == "w" and role in roles)


def relation_element(rid, t, members, ways, nodes, roles=("outer", "inner", "")):
    out = []
    for kind, ref, role in members:
        if kind == "w" and role in roles and ref in ways and len(ways[ref]) > 1:
            out.append({"type": "way", "ref": ref, "role": role, "geometry": geometry(ways[ref])})
        elif kind == "n" and (role.startswith("stop") or role.startswith("platform")) and ref in nodes:
            lat, lon = nodes[ref]
            out.append({"type": "node", "ref": ref, "role": role, "lat": r6(lat), "lon": r6(lon)})
    return {"type": "relation", "id": rid, "tags": slim(t), "members": out}


def read_poly(path):
    """Geofabrik .poly coverage, as rings of [lat, lon]; holes are ignored."""
    rings, ring, in_ring, hole = [], [], False, False
    with open(path) as f:
        lines = [l.strip() for l in f if l.strip()]
    for line in lines[1:]:
        if line == "END":
            if in_ring:
                if not hole and ring:
                    rings.append(simplify(ring, 500))
                ring, in_ring = [], False
            continue
        if not in_ring:
            in_ring, hole = True, line.startswith("!")
            continue
        lon, lat = map(float, line.split()[:2])
        ring.append((lat, lon))
    return [[[r6(lat), r6(lon)] for lat, lon in r] for r in rings]


def main():
    pbf, region, name, poly, out_dir = sys.argv[1:6]
    relations, need_ways, need_nodes = read_relations(pbf)
    print(f"relations kept: {len(relations)}", file=sys.stderr)

    second = Elements(need_ways, need_nodes)
    second.read(pbf)

    lines = {"transitLine": [], "highSpeedRail": second.lines["highSpeedRail"],
             "coastline": second.lines["coastline"], "water": second.lines["water"]}
    boundaries = []
    for rid, (purposes, t, members) in relations.items():
        if "transit" in purposes:
            lines["transitLine"].append(relation_element(rid, t, members, second.way_points, second.node_points))
        if "boundary" in purposes and is_complete(members, second.way_points):
            boundaries.append(relation_element(rid, t, members, second.way_points, {}))
        if "water" in purposes:
            lines["water"].append(relation_element(rid, t, members, second.way_points, {}))
        if "point" in purposes:
            points = [p for kind, ref, _ in members for p in
                      (second.way_points.get(ref, []) if kind == "w" else
                       [second.node_points[ref]] if kind == "n" and ref in second.node_points else [])]
            if points:
                center = bbox_center(points)
                for mode in mode_matches("r", t):
                    second.stations[mode].append(second.point_element("r", rid, t, center=center))
                for category in categories_matching("r", t):
                    second.places.setdefault(category, []).append(second.point_element("r", rid, t, center=center))

    os.makedirs(out_dir, exist_ok=True)
    built = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    db_path = os.path.join(out_dir, f"{region}.sqlite")
    if os.path.exists(db_path):
        os.remove(db_path)
    write_database(db_path, {
        "version": str(PACK_VERSION),
        "region": region,
        "name": name,
        "built": built,
        "attribution": "© OpenStreetMap contributors, ODbL 1.0",
        "coverage": json.dumps(read_poly(poly), separators=(",", ":")),
    }, second.stations, second.places, lines, [b for b in boundaries if b["members"]])

    # The download is the database, raw-deflated; the app unpacks it once when installing.
    path = os.path.join(out_dir, f"{region}.hspack")
    packer = zlib.compressobj(9, zlib.DEFLATED, -15)
    with open(db_path, "rb") as source, open(path, "wb") as target:
        while chunk := source.read(1 << 20):
            target.write(packer.compress(chunk))
        target.write(packer.flush())
    print(json.dumps({"region": region, "built": built, "bytes": os.path.getsize(path), "database_bytes": os.path.getsize(db_path),
                      "stations": {k: len(v) for k, v in second.stations.items()},
                      "places": {k: len(v) for k, v in second.places.items()},
                      "lines": {k: len(v) for k, v in lines.items()},
                      "boundaries": sum(1 for b in boundaries if b["members"])}))
    os.remove(db_path)


def element_points(e):
    """Every coordinate of an element, for its bounding box."""
    if "lat" in e:
        return [(e["lat"], e["lon"])]
    if "center" in e:
        return [(e["center"]["lat"], e["center"]["lon"])]
    points = [(p["lat"], p["lon"]) for p in e.get("geometry", [])]
    for m in e.get("members", []):
        points += [(p["lat"], p["lon"]) for p in m.get("geometry", [])]
        if "lat" in m:
            points.append((m["lat"], m["lon"]))
    return points


def bounds(e):
    points = element_points(e)
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    return min(lats), max(lats), min(lons), max(lons)


def write_database(path, meta, stations, places, lines, boundaries):
    """One row per element, indexed by position, so the app reads only what is near a game."""
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE points(category TEXT NOT NULL, lat REAL NOT NULL, lon REAL NOT NULL, element TEXT NOT NULL);
        CREATE TABLE lines(category TEXT NOT NULL, south REAL, north REAL, west REAL, east REAL, element TEXT NOT NULL);
        CREATE TABLE boundaries(id INTEGER PRIMARY KEY, level INTEGER, south REAL, north REAL, west REAL, east REAL,
                                element TEXT NOT NULL);
    """)
    def text(e):
        return json.dumps(e, separators=(",", ":"), ensure_ascii=False)
    db.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    rows = []
    for prefix, groups in (("station", stations), ("place", places)):
        for key, elements in groups.items():
            for e in elements:
                lat, lon = element_points(e)[0]
                rows.append((f"{prefix}:{key}", lat, lon, text(e)))
    db.executemany("INSERT INTO points VALUES (?, ?, ?, ?)", rows)
    rows = []
    for key, elements in lines.items():
        for e in elements:
            if element_points(e):
                rows.append((key, *bounds(e), text(e)))
    db.executemany("INSERT INTO lines VALUES (?, ?, ?, ?, ?, ?)", rows)
    db.executemany("INSERT INTO boundaries VALUES (?, ?, ?, ?, ?, ?, ?)",
                   [(b["id"], int(b["tags"].get("admin_level", 0)), *bounds(b), text(b)) for b in boundaries])
    db.executescript("""
        CREATE INDEX points_place ON points(category, lat);
        CREATE INDEX lines_place ON lines(category, south);
        CREATE INDEX boundaries_place ON boundaries(level, south);
    """)
    db.commit()
    db.execute("VACUUM")
    db.close()


if __name__ == "__main__":
    main()
