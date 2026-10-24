"""TDM-113 read-only Inkbox account probe. Prints what the account has (identities, phone numbers,
iMessage lines, triage number). Never prints the API key, and only the last 4 digits of numbers.
Sends nothing, creates nothing."""
import os
import sys
from pathlib import Path

from dotenv import dotenv_values
from inkbox import Inkbox

env = dotenv_values(Path(__file__).resolve().parents[3] / ".env")
key = env.get("INKBOX_API_KEY") or os.environ.get("INKBOX_API_KEY")
if not key:
    sys.exit("INKBOX_API_KEY not set")


def last4(n):
    n = str(n or "")
    return f"...{n[-4:]}" if n else "-"


ib = Inkbox(api_key=key)
try:
    w = ib.whoami()
    print("whoami:", type(w).__name__, {k: v for k, v in vars(w).items() if k in ("auth_type", "scope", "organization_id", "auth_subtype", "plan")})
except Exception as e:
    print("whoami error:", type(e).__name__, str(e)[:200])
try:
    ids = ib.list_identities()
    print(f"identities: {len(ids)}")
    for i in ids:
        d = vars(i)
        print("  -", d.get("agent_handle"), {k: d.get(k) for k in ("imessage_enabled", "status")},
              "phone:", last4(getattr(getattr(i, "phone_number", None), "number", None)))
except Exception as e:
    print("identities error:", type(e).__name__, str(e)[:200])
try:
    nums = ib.phone_numbers.list()
    print(f"phone numbers: {len(nums)}")
    for n in nums:
        d = vars(n)
        print("  -", last4(d.get("number")), {k: str(d.get(k)) for k in ("type", "status", "sms_status", "agent_handle", "agent_identity_id") if k in d})
except Exception as e:
    print("phone numbers error:", type(e).__name__, str(e)[:200])
try:
    lines = ib.imessages.list_numbers()
    print(f"imessage dedicated lines: {len(lines)}")
    for n in lines:
        print("  -", last4(n.number), n.status, n.agent_handle)
except Exception as e:
    print("imessage lines error:", type(e).__name__, str(e)[:200])
try:
    t = ib.imessages.get_triage_number()
    print("imessage triage number:", t.number, "connect command:", t.connect_command)
except Exception as e:
    print("triage error:", type(e).__name__, str(e)[:200])
try:
    subs = ib.webhooks.subscriptions.list()
    print(f"webhook subscriptions: {len(subs)}")
except Exception as e:
    print("subs error:", type(e).__name__, str(e)[:200])
