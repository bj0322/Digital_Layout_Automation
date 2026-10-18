# KLayout app wrapper: klayout_app -b -r render.py -rd repo=<repo> -rd jsonfile=<json> -rd gds=<gds> -rd outdir=<dir>
import json, sys
sys.path.insert(0, repo)  # noqa: F821  (-rd variables)
from dla import gds as G
d = json.load(open(jsonfile))  # noqa: F821
G.render_png(gds, outdir + "/overview.png", d, width=800)  # noqa: F821
for name, box in d.get("zoom", {}).items():
    G.render_png(gds, outdir + "/%s.png" % name, d, box=box, width=1400)  # noqa: F821
print("done")
