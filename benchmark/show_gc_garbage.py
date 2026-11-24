#!/usr/bin/env python3
"""Pretty-print /debug/gc-garbage output. Separate file so the runner needs no heredoc."""
import json
import sys

d = json.load(open(sys.argv[1]))
print(f"  garbage objects: {d.get('garbage_objects')}")
print(f"  event_stream generators found in cycles: {d.get('event_stream_generators_in_cycles')}")
for f in d.get("findings", []):
    print(f"  -- {f['generator']}  frame_present={f['ag_frame_present']}")
    for m in f["cycle_members_referring_to_it"]:
        print(f"       held by: {m}")
census = d.get("type_census", {})
if census:
    print("  cycle type census (top 12):")
    for k, v in list(census.items())[:12]:
        print(f"       {v:>6}  {k}")
