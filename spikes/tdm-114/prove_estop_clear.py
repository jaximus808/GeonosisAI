"""TDM-114: soft e-stop the throwaway stack's sim robot, then clear it from the web route (operator key)."""
import glob, json, sys, time, urllib.request
from pathlib import Path
from rdf_common.spacetime_client import SpacetimeClient

root, stdb, db, orch = sys.argv[1:5]
tok = Path(glob.glob(f"{root}/**/tokens/orchestrator.token", recursive=True)[0])
key = (tok.parent / "operator.key").read_text().strip()
c = SpacetimeClient(stdb, db, tok.read_text().strip())
st = lambda: c.sql("SELECT * FROM robot WHERE name = 'sim-yam-mujoco'")[0]
rid = int(st()["id"])
c.call_reducer("issue_robot_command", rid, "soft_estop", json.dumps({"reason": "tdm114 proof"}), 0)
for _ in range(30):
    if st()["status"] == "estop": break
    time.sleep(0.5)
print("after soft_estop:", st()["status"])
req = urllib.request.Request(f"{orch}/robots/{rid}/estop/clear", data=b'{"by":"tdm114-proof"}', method="POST",
                             headers={"content-type": "application/json", "x-rdf-operator-key": key})
print("POST /estop/clear ->", urllib.request.urlopen(req, timeout=20).read().decode())
for _ in range(40):
    if st()["status"] != "estop": break
    time.sleep(0.5)
print("after clear:", st()["status"])
print("events:", [ (e["type"], e["actor"]) for e in c.sql("SELECT * FROM event_log") if e["type"] in ("robot.estop_clear_requested", "robot.estop_cleared")])
