#!/usr/bin/env python3
"""Cuts an OpenStreetMap extract down to what region packs use, before build_pack.py reads it.

Tag matching runs in C++, and the writer adds the nodes and ways that kept objects refer
to, so outlines and routes stay whole. The result is a few percent of the input, which
keeps the slower Python pass fast and small even for large countries.

Usage: prefilter.py <in.osm.pbf> <out.osm.pbf>
       prefilter.py --expressions    the same filter for `osmium tags-filter`
"""
import sys
import time

import osmium

# Every tag build_pack.py looks for. Exact checks (names, disused, honorary…) happen there.
TAGS = [
    ("railway", "station"), ("railway", "halt"), ("railway", "tram_stop"), ("railway", "rail"),
    ("public_transport", "station"), ("public_transport", "platform"),
    ("highway", "bus_stop"),
    ("amenity", "ferry_terminal"), ("amenity", "cinema"), ("amenity", "hospital"),
    ("amenity", "library"), ("amenity", "embassy"),
    ("aeroway", "aerodrome"), ("office", "diplomatic"),
    ("natural", "peak"), ("natural", "coastline"), ("natural", "water"),
    ("leisure", "park"), ("leisure", "golf_course"),
    ("tourism", "theme_park"), ("tourism", "zoo"), ("tourism", "aquarium"), ("tourism", "museum"),
    ("boundary", "administrative"),
    ("route", "train"), ("route", "subway"), ("route", "light_rail"), ("route", "tram"), ("route", "monorail"),
]


def main():
    if sys.argv[1] == "--expressions":
        print("\n".join(f"nwr/{key}={value}" for key, value in TAGS))
        return
    source, target = sys.argv[1], sys.argv[2]
    started = time.time()
    kept = 0
    with osmium.BackReferenceWriter(target, ref_src=source, overwrite=True) as writer:
        for obj in osmium.FileProcessor(source).with_filter(osmium.filter.TagFilter(*TAGS)):
            writer.add(obj)
            kept += 1
    print(f"prefilter kept {kept} objects in {time.time() - started:.0f} s", file=sys.stderr)


if __name__ == "__main__":
    main()
