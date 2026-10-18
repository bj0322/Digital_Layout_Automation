# KLayout app wrapper: klayout_app -b -r verify_conn.py -rd repo=<repo> -rd jsonfile=<json> -rd gds=<gds>
import json, sys
sys.path.insert(0, repo)  # noqa: F821
from dla import gds as G
c = G.verify_conn(json.load(open(jsonfile)), gds)  # noqa: F821
print("probes: %d" % c["probes"])
print("no metal at probe: %d" % len(c["missing"]))
for m in c["missing"][:10]:
    print("   ", m)
print("opens : %d" % len(c["opens"]))
for n, k in list(c["opens"].items())[:10]:
    print("   ", n, "split into", k)
print("shorts: %d" % len(c["shorts"]))
for s in c["shorts"][:10]:
    print("   ", s[:6])
print("CONNECTIVITY: %s" % ("PASS" if c["ok"] else "FAIL"))
