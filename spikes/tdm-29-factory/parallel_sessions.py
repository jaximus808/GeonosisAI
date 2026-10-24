"""TDM-29 spike helper: N concurrent cloud sessions on one Droid Computer (parallel-session check).

Usage: .venv/bin/python parallel_sessions.py <computerId> [n=3]
"""
import concurrent.futures as cf
import sys
import time

from cloud_api_resume import MODEL, call, client, log, wait_idle


def one(cid: str, i: int) -> dict:
    with client() as c:
        s = call(c, "POST", "/sessions", json={"computerId": cid, "sessionSettings": {
            "model": MODEL, "reasoningEffort": "low", "tags": [{"name": "tdm-29-spike"}]}})
        sid = s.get("sessionId")
        if not sid:
            return {"i": i, "error": s}
        t0 = time.time()
        post = call(c, "POST", f"/sessions/{sid}/messages", json={"text": f"Reply with exactly: PARALLEL_OK_{i}"})
        st = wait_idle(c, sid, limit_s=240)
        return {"i": i, "session": sid, "post_status": post.get("status"),
                "final": st.get("status"), "s": round(time.time() - t0, 1)}


if __name__ == "__main__":
    cid, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 3
    with cf.ThreadPoolExecutor(n) as ex:
        results = list(ex.map(lambda i: one(cid, i), range(n)))
    log("parallel_result", n=n, results=results)
