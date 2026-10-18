# KLayout app wrapper: klayout_app -b -r build_gds.py -rd repo=<repo> -rd jsonfile=<json> -rd stdgds=<std.gds> -rd out=<gds>
import json, sys
sys.path.insert(0, repo)  # noqa: F821  (-rd variables)
from dla import gds
w, h = gds.build_gds(json.load(open(jsonfile)), stdgds, out)  # noqa: F821
print("wrote %s: %.1f x %.1f um" % (out, w, h))  # noqa: F821
