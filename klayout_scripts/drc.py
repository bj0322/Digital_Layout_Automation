# KLayout app wrapper: klayout_app -b -r drc.py -rd repo=<repo> -rd tech=<tech.json> -rd gds=<gds> -rd top=<cell>
import sys
sys.path.insert(0, repo)  # noqa: F821  (-rd variables)
from dla import gds as G
from dla.tech import load
v = G.drc(gds, top, load(tech))  # noqa: F821
for k, n in sorted(v.items()):
    print("%6d  %s" % (n, k))
print("DRC: %s" % ("CLEAN" if not v else "%d violations" % sum(v.values())))
