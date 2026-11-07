"""Print status and rejection reason for each pair replay diagnostic (scratch, reads files only)."""
import glob
import json

for path in sorted(glob.glob("/home/opc/gate_experiment/out/r3*_*.diag.json")):
    d = json.load(open(path))
    name = path.split("/")[-1].replace(".diag.json", "")
    failure = d.get("failure") or {}
    reason = (d.get("generation", {}).get("rejection_metadata") or {}).get("reason")
    print(name, d.get("status"), failure.get("exception_type"), reason)
