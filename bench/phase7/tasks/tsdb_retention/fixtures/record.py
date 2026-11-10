"""Setup only (deleted afterwards): turn tsdbgen's output into the post-run check's expectations."""
import json
import sys

rows = [line.split() for line in open(sys.argv[1]) if line.strip()]
print(json.dumps({"blocks": {p.rsplit("/", 1)[1]: s for p, s in rows}, "ballast": "/srv/pgdata/base.img"}))
