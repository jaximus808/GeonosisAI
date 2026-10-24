"""TDM-29 spike helper: poll a Droid Computer's status + latest metrics sample to observe idle auto-pause."""
import json, os, sys, time
from pathlib import Path
import httpx
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE.parents[1] / ".env")
cid, minutes = sys.argv[1], float(sys.argv[2]) if len(sys.argv) > 2 else 45
c = httpx.Client(base_url="https://api.factory.ai/api/v0",
                 headers={"Authorization": f"Bearer {os.environ['FACTORY_API_KEY']}"}, timeout=60)
end = time.time() + minutes * 60
while time.time() < end:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    try:
        rec["status"] = c.get(f"/computers/{cid}").json().get("status")
        m = c.get(f"/computers/{cid}/metrics").json()
        rec["last_metric"] = m[-1]["timestamp"] if isinstance(m, list) and m else m
        rec["n_metrics"] = len(m) if isinstance(m, list) else None
    except Exception as e:  # keep polling
        rec["error"] = repr(e)[:200]
    with open(HERE / "out" / "computer_poll.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    time.sleep(60)
