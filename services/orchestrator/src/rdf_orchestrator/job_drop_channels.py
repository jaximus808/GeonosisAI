"""TDM-108 job-drop channels: one small adapter per social network. Plain REST over ``httpx`` (no SDKs).

Every adapter has the same shape::

    ch.name                     "discord" | "bluesky" | "x" | "telegram"
    ch.size                     card size it posts ("square" | "wide")
    ch.configured -> bool       its credentials are set (a channel is OFF unless they are)
    ch.text(post) -> str        the post text for this network (limits applied)
    ch.post(http, post, png)    -> PostResult; raises ChannelError (message never carries a secret)

``post`` is a ``DropPost``: the task facts, the channel's tracked link and the cost note. The caller
(``job_drops.Dropper``) decides whether to call ``post`` at all (mode, rate limits, dedup).

Secrets: tokens and passwords are ``SecretStr`` on the settings object and only leave it inside an
``Authorization`` header or a request body. ``ChannelError`` messages are built from status codes and the
API's own error code, never from ``str(exc)`` of an httpx error (that string contains the request URL,
and Telegram puts its bot token in the URL).

API notes (checked against the official docs, Oct 2026):
  * Discord: ``POST /api/v10/channels/{id}/messages`` with ``Authorization: Bot <token>``, multipart
    ``payload_json`` + ``files[0]``; the embed image is ``attachment://card.png``. Bot needs View Channel,
    Send Messages, Embed Links, Attach Files. 429 -> wait ``retry_after`` s and retry. A webhook URL
    (``RDF_DROP_DISCORD_WEBHOOK``) is the alternative path (``?wait=true``, same multipart body).
  * Bluesky: ``com.atproto.server.createSession`` (app password) -> ``com.atproto.repo.uploadBlob`` (<= 1 MB)
    -> ``com.atproto.repo.createRecord`` of an ``app.bsky.feed.post`` with an ``app.bsky.embed.images``
    embed and ``app.bsky.richtext.facet#link`` facets (UTF-8 byte offsets). Text <= 300 graphemes.
  * X: API v2, OAuth 1.0a user context. ``POST /2/media/upload`` (multipart, ``media_category=tweet_image``)
    then ``POST /2/tweets`` with ``media.media_ids``. Pay-per-use: Post create $0.015, Post create with a
    URL $0.200 (docs.x.com pricing, Oct 2026), so by default the link lives only in the card (QR + short link).
  * Telegram: ``POST /bot<token>/sendPhoto`` multipart (chat_id, photo, caption <= 1024 chars).
  * Instagram: not built (needs a professional account and Meta app review).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote, urlparse

import httpx
from pydantic import SecretStr

X_COST_POST_USD = 0.015
X_COST_POST_WITH_URL_USD = 0.200
BSKY_MAX_GRAPHEMES = 300
BSKY_MAX_BLOB = 1_000_000
TELEGRAM_MAX_CAPTION = 1024
DISCORD_MAX_RETRIES = 2
MAX_RETRY_WAIT_S = 10.0


class ChannelError(RuntimeError):
    """A failed post. ``str(e)`` is safe to log (no secrets, no URLs with tokens)."""

    def __init__(self, msg: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


@dataclass
class DropPost:
    task_id: int
    kind: str  # "Recovery takeover"
    title: str
    instructions: str
    price: str  # "$3.50"
    price_note: str
    minutes: int
    robot: str
    where: str  # "From your phone" | "At the robot"
    link: str  # tracked link for this channel (…/j/<task>/<channel>)
    count: int = 1
    access_code: str | None = None
    test_mode: bool = True
    sample: bool = False


@dataclass
class PostResult:
    post_id: str
    post_url: str | None = None
    cost_estimate_usd: float = 0.0
    note: str = ""
    extra: dict = field(default_factory=dict)


def _secret(v: SecretStr | str | None) -> str:
    if v is None:
        return ""
    return v.get_secret_value() if isinstance(v, SecretStr) else str(v)


def _safe_http_error(e: Exception) -> ChannelError:
    if isinstance(e, httpx.TimeoutException):
        return ChannelError("timed out", retryable=True)
    if isinstance(e, httpx.HTTPError):
        return ChannelError(f"network error ({type(e).__name__})", retryable=True)
    return ChannelError(type(e).__name__)


def _api_error(r: httpx.Response, network: str) -> ChannelError:
    code = ""
    try:
        j = r.json()
        if isinstance(j, dict):
            code = str(j.get("code") or j.get("error") or j.get("error_code") or j.get("title") or "")[:60]
            msg = j.get("message") or j.get("description") or j.get("detail") or ""
            if isinstance(msg, str) and msg:
                code = f"{code} {msg[:120]}".strip()
    except ValueError:
        pass
    return ChannelError(f"{network} HTTP {r.status_code}{': ' + code if code else ''}", status=r.status_code,
                        retryable=r.status_code in (429, 500, 502, 503, 504))


def _retry_after(r: httpx.Response) -> float:
    try:
        j = r.json()
        v = j.get("retry_after") if isinstance(j, dict) else None
        if v is None and isinstance(j, dict):
            v = (j.get("parameters") or {}).get("retry_after")  # telegram
    except ValueError:
        v = None
    if v is None:
        v = r.headers.get("retry-after")
    try:
        return max(0.0, float(v))
    except (TypeError, ValueError):
        return 1.0


def _first_sentence(text: str, limit: int) -> str:
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    for sep in (". ", "; ", ", "):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[: i + 1].rstrip(",;")
    return cut.rsplit(" ", 1)[0] + "..."


def headline(p: DropPost) -> str:
    """Numbers first: "$3.50 test mode. Recovery takeover, about 2 min, from your phone." """
    money = f"{p.price} test mode" if p.test_mode else p.price
    many = f"{p.count} paid tasks at {money} each" if p.count > 1 else f"Paid task: {money}"
    return f"{many}. {p.kind}, about {p.minutes} min, {p.where.lower()}."


def access_line(p: DropPost) -> str:
    return f"Code for this task: {p.access_code}." if p.access_code else ""


CLAIMED_TEXT = "Claimed, no longer available."


Sleep = Callable[[float], None]


# --------------------------------------------------------------------------- Discord

class Discord:
    name = "discord"
    size = "square"

    def __init__(self, s: Any, *, sleep: Sleep = time.sleep) -> None:
        self.s = s
        self.sleep = sleep

    @property
    def via(self) -> str | None:
        if _secret(self.s.discord_bot_token) and (self.s.rdf_drop_discord_channel_id or "").strip():
            return "bot"
        if _secret(self.s.rdf_drop_discord_webhook):
            return "webhook"
        return None

    @property
    def configured(self) -> bool:
        return self.via is not None

    def text(self, p: DropPost) -> str:
        lines = [headline(p)]
        if not p.link:
            lines.append("Link coming soon.")
        if p.sample:
            lines.insert(0, "Sample post from GeonosisAI. There is no task behind it.")
        if access_line(p):
            lines.append(access_line(p))
        return "\n".join(lines)

    def payload(self, p: DropPost, *, buttons: bool) -> dict:
        desc = _first_sentence(p.instructions, 300)
        embed = {
            "title": p.title[:256],
            **({"url": p.link} if p.link else {}),
            "description": desc,
            "color": self.s.discord_color,
            "fields": [
                {"name": "Pay", "value": f"{p.price}{' (test mode)' if p.test_mode else ''}", "inline": True},
                {"name": "Time", "value": f"About {p.minutes} min", "inline": True},
                {"name": "Where", "value": f"{p.where} · {p.robot}", "inline": True},
            ],
            "image": {"url": "attachment://card.png"},
            "footer": {"text": f"GeonosisAI · task #{p.task_id}" + (" · test mode" if p.test_mode else "")},
        }
        body: dict[str, Any] = {
            "content": self.text(p)[:2000],
            "embeds": [embed],
            "attachments": [{"id": 0, "filename": "card.png", "description": f"{p.kind}: {p.title}"[:1024]}],
            "allowed_mentions": {"parse": []},  # never ping @everyone from a task title
        }
        if buttons and p.link.startswith("https://"):
            body["components"] = [{"type": 1, "components": [
                {"type": 2, "style": 5, "label": f"Claim for {p.price}", "url": p.link}]}]
        return body

    def post(self, http: httpx.Client, p: DropPost, png: bytes) -> PostResult:
        via = self.via
        if via is None:
            raise ChannelError("discord not configured")
        if via == "bot":
            channel = re.sub(r"\D", "", self.s.rdf_drop_discord_channel_id or "")
            url = f"{self.s.rdf_drop_discord_api.rstrip('/')}/channels/{channel}/messages"
            headers = {"Authorization": f"Bot {_secret(self.s.discord_bot_token)}",
                       "User-Agent": self.s.discord_user_agent}
            body = self.payload(p, buttons=True)
        else:
            url = _secret(self.s.rdf_drop_discord_webhook)
            url += ("&" if "?" in url else "?") + "wait=true"
            headers = {"User-Agent": self.s.discord_user_agent}
            body = self.payload(p, buttons=False)
        data = {"payload_json": json.dumps(body)}
        for attempt in range(DISCORD_MAX_RETRIES + 1):
            try:
                r = http.post(url, headers=headers, data=data, files={"files[0]": ("card.png", png, "image/png")})
            except Exception as e:  # noqa: BLE001
                raise _safe_http_error(e) from None
            if r.status_code == 429 and attempt < DISCORD_MAX_RETRIES:
                self.sleep(min(_retry_after(r), MAX_RETRY_WAIT_S))
                continue
            if r.status_code >= 300:
                raise _api_error(r, "discord")
            j = r.json()
            mid, cid, gid = str(j.get("id") or ""), str(j.get("channel_id") or ""), j.get("guild_id")
            post_url = f"https://discord.com/channels/{gid}/{cid}/{mid}" if gid and cid and mid else None
            return PostResult(post_id=mid, post_url=post_url, cost_estimate_usd=0.0, note=f"discord {via}",
                              extra={"channel_id": cid, "via": via})
        raise ChannelError("discord rate limited", status=429, retryable=True)

    # -- lifecycle: the message follows its task
    def _message(self, rec: dict) -> tuple[str, dict]:
        extra = rec.get("extra") or {}
        mid = re.sub(r"\D", "", str(rec.get("post_id") or ""))
        if not mid:
            raise ChannelError("discord: no message id stored for this post")
        if (extra.get("via") or self.via) == "bot":
            cid = re.sub(r"\D", "", str(extra.get("channel_id") or self.s.rdf_drop_discord_channel_id or ""))
            return (f"{self.s.rdf_drop_discord_api.rstrip('/')}/channels/{cid}/messages/{mid}",
                    {"Authorization": f"Bot {_secret(self.s.discord_bot_token)}", "User-Agent": self.s.discord_user_agent})
        hook = _secret(self.s.rdf_drop_discord_webhook).split("?", 1)[0].rstrip("/")
        if not hook:
            raise ChannelError("discord webhook not configured")
        return f"{hook}/messages/{mid}", {"User-Agent": self.s.discord_user_agent}

    def _call(self, http: httpx.Client, method: str, url: str, headers: dict, body: dict | None = None) -> httpx.Response:
        for attempt in range(DISCORD_MAX_RETRIES + 1):
            try:
                r = http.request(method, url, headers=headers, json=body)
            except Exception as e:  # noqa: BLE001
                raise _safe_http_error(e) from None
            if r.status_code == 429 and attempt < DISCORD_MAX_RETRIES:
                self.sleep(min(_retry_after(r), MAX_RETRY_WAIT_S))
                continue
            return r
        raise ChannelError("discord rate limited", status=429, retryable=True)

    def mark_claimed(self, http: httpx.Client, rec: dict, p: DropPost) -> str:
        """Edit the post: title kept, "Claimed, no longer available", steel; QR card, Claim button and code gone."""
        url, headers = self._message(rec)
        body = {"content": CLAIMED_TEXT, "attachments": [], "components": [], "allowed_mentions": {"parse": []},
                "embeds": [{"title": p.title[:256], "description": CLAIMED_TEXT,
                            "color": getattr(self.s, "discord_steel_color", 0x8F9CA5),
                            "footer": {"text": f"GeonosisAI · task #{p.task_id}"}}]}
        r = self._call(http, "PATCH", url, headers, body)
        if r.status_code == 404:
            return "gone"
        if r.status_code >= 300:
            raise _api_error(r, "discord edit")
        return "edited"

    def remove(self, http: httpx.Client, rec: dict) -> str:
        url, headers = self._message(rec)
        r = self._call(http, "DELETE", url, headers)
        if r.status_code == 404:
            return "gone"
        if r.status_code >= 300:
            raise _api_error(r, "discord delete")
        return "deleted"


# --------------------------------------------------------------------------- Bluesky

def graphemes(text: str) -> int:
    """Grapheme count, conservative: combining marks join the previous character; everything else counts
    one. (Over-counts some emoji ZWJ sequences, which only makes us trim earlier. Our posts use no emoji.)"""
    t = unicodedata.normalize("NFC", text)
    return sum(1 for ch in t if not unicodedata.combining(ch) and ch not in "‍️")


def link_facets(text: str, links: list[str]) -> list[dict]:
    out = []
    b = text.encode("utf-8")
    for link in links:
        if not link:
            continue
        i = b.find(link.encode("utf-8"))
        if i < 0:
            continue
        out.append({"index": {"byteStart": i, "byteEnd": i + len(link.encode("utf-8"))},
                    "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}]})
    return out


class Bluesky:
    name = "bluesky"
    size = "wide"

    def __init__(self, s: Any) -> None:
        self.s = s
        self._session: tuple[str, str, float] | None = None  # (did, accessJwt, created_at)

    @property
    def configured(self) -> bool:
        return bool((self.s.rdf_drop_bsky_handle or "").strip() and _secret(self.s.rdf_drop_bsky_app_password))

    def text(self, p: DropPost) -> str:
        head = headline(p)
        if p.sample:
            head = "Sample post, no task behind it. " + head
        tail = [x for x in (access_line(p), f"Claim it: {p.link}" if p.link else "Link coming soon.") if x]
        fixed = "\n".join(tail)
        room = BSKY_MAX_GRAPHEMES - graphemes(head) - graphemes(fixed) - 2
        middle = _first_sentence(p.title.rstrip(".") + ".", max(0, room)) if room > 20 else ""
        parts = [head] + ([middle] if middle else []) + [fixed]
        text = "\n".join(parts)
        while graphemes(text) > BSKY_MAX_GRAPHEMES and len(parts) > 2:
            parts.pop(1)
            text = "\n".join(parts)
        return text

    def _xrpc(self, method: str) -> str:
        return f"{self.s.rdf_drop_bsky_pds.rstrip('/')}/xrpc/{method}"

    def _login(self, http: httpx.Client, force: bool = False) -> tuple[str, str]:
        if self._session and not force and time.time() - self._session[2] < 60 * 60:
            return self._session[0], self._session[1]
        try:
            r = http.post(self._xrpc("com.atproto.server.createSession"),
                          json={"identifier": self.s.rdf_drop_bsky_handle.strip(),
                                "password": _secret(self.s.rdf_drop_bsky_app_password)})
        except Exception as e:  # noqa: BLE001
            raise _safe_http_error(e) from None
        if r.status_code >= 300:
            raise _api_error(r, "bluesky login")
        j = r.json()
        self._session = (j["did"], j["accessJwt"], time.time())
        return self._session[0], self._session[1]

    def post(self, http: httpx.Client, p: DropPost, png: bytes) -> PostResult:
        did, jwt = self._login(http)
        img, mime = png, "image/png"
        if len(img) > BSKY_MAX_BLOB:
            img, mime = _to_jpeg_under(png, BSKY_MAX_BLOB), "image/jpeg"
        try:
            r = http.post(self._xrpc("com.atproto.repo.uploadBlob"), content=img,
                          headers={"Authorization": f"Bearer {jwt}", "Content-Type": mime})
            if r.status_code == 401:
                did, jwt = self._login(http, force=True)
                r = http.post(self._xrpc("com.atproto.repo.uploadBlob"), content=img,
                              headers={"Authorization": f"Bearer {jwt}", "Content-Type": mime})
        except ChannelError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _safe_http_error(e) from None
        if r.status_code >= 300:
            raise _api_error(r, "bluesky uploadBlob")
        blob = r.json()["blob"]
        text = self.text(p)
        w, h = _png_size(png)
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            "langs": ["en"],
            "facets": link_facets(text, [p.link]),
            "embed": {"$type": "app.bsky.embed.images", "images": [
                {"alt": f"{p.kind}: {p.title}. Pays {p.price}. Scan the QR code to claim.", "image": blob,
                 "aspectRatio": {"width": w, "height": h}}]},
        }
        try:
            r = http.post(self._xrpc("com.atproto.repo.createRecord"), headers={"Authorization": f"Bearer {jwt}"},
                          json={"repo": did, "collection": "app.bsky.feed.post", "record": record})
        except Exception as e:  # noqa: BLE001
            raise _safe_http_error(e) from None
        if r.status_code >= 300:
            raise _api_error(r, "bluesky createRecord")
        uri = r.json().get("uri", "")
        rkey = uri.rsplit("/", 1)[-1] if uri else ""
        handle = self.s.rdf_drop_bsky_handle.strip().lstrip("@")
        return PostResult(post_id=uri, post_url=f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else None,
                          cost_estimate_usd=0.0, note="bluesky")

    # -- lifecycle. Bluesky posts can't be edited, so a claimed task's post is deleted (a reopen reposts).
    def remove(self, http: httpx.Client, rec: dict) -> str:
        m = re.fullmatch(r"at://([^/]+)/([^/]+)/([^/]+)", str(rec.get("post_id") or ""))
        if not m:
            raise ChannelError("bluesky: no post uri stored for this post")
        did, jwt = self._login(http)
        try:
            r = http.post(self._xrpc("com.atproto.repo.deleteRecord"), headers={"Authorization": f"Bearer {jwt}"},
                          json={"repo": m.group(1), "collection": m.group(2), "rkey": m.group(3)})
        except Exception as e:  # noqa: BLE001
            raise _safe_http_error(e) from None
        if r.status_code == 400 and "NotFound" in r.text:
            return "gone"
        if r.status_code >= 300:
            raise _api_error(r, "bluesky deleteRecord")
        return "deleted"

    def mark_claimed(self, http: httpx.Client, rec: dict, p: DropPost) -> str:
        return self.remove(http, rec)


# --------------------------------------------------------------------------- X

_URLISH = re.compile(r"(https?://\S+|\b[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}\b(/\S*)?)", re.I)  # what X auto-links


def oauth1_header(method: str, url: str, *, consumer_key: str, consumer_secret: str, token: str, token_secret: str,
                  params: dict[str, str] | None = None, nonce: str | None = None, ts: int | None = None) -> str:
    """RFC 5849 HMAC-SHA1 ``Authorization`` header. JSON and multipart bodies are not signed (per spec only
    form-encoded bodies are), so ``params`` is only the query string here."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(ts if ts is not None else int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    u = urlparse(url)
    base_url = f"{u.scheme}://{u.netloc}{u.path}"
    allp = {**(params or {}), **oauth}
    enc = lambda s: quote(str(s), safe="~-._")  # noqa: E731
    norm = "&".join(f"{k}={v}" for k, v in sorted((enc(k), enc(v)) for k, v in allp.items()))
    base = "&".join([method.upper(), enc(base_url), enc(norm)])
    key = f"{enc(consumer_secret)}&{enc(token_secret)}"
    sig = base64.b64encode(hmac.new(key.encode(), base.encode(), hashlib.sha1).digest()).decode()
    oauth["oauth_signature"] = sig
    return "OAuth " + ", ".join(f'{enc(k)}="{enc(v)}"' for k, v in sorted(oauth.items()))


class X:
    name = "x"
    size = "wide"

    def __init__(self, s: Any) -> None:
        self.s = s

    @property
    def configured(self) -> bool:
        return all(_secret(v) for v in (self.s.rdf_drop_x_api_key, self.s.rdf_drop_x_api_secret,
                                        self.s.rdf_drop_x_access_token, self.s.rdf_drop_x_access_secret))

    def includes_link(self) -> bool:
        return bool(self.s.rdf_drop_x_include_link)

    def text(self, p: DropPost) -> str:
        """No URL unless RDF_DROP_X_INCLUDE_LINK=1 (a URL makes the post cost $0.200 instead of $0.015).
        Anything in the task text that X would turn into a link is removed for the same reason."""
        title = _URLISH.sub("", p.title).strip()
        lines = [("Sample post, no task behind it. " if p.sample else "") + headline(p)]
        if title:
            lines.append(_first_sentence(title.rstrip(".") + ".", 120))
        if access_line(p):
            lines.append(access_line(p))
        if not p.link:
            lines.append("Link coming soon.")
        else:
            lines.append(f"Claim it: {p.link}" if self.includes_link() else "Scan the QR code on the card to claim it.")
        return "\n".join(lines)[:280]

    def cost(self, text: str) -> float:
        return X_COST_POST_WITH_URL_USD if _URLISH.search(text) else X_COST_POST_USD

    def _auth(self, method: str, url: str) -> str:
        return oauth1_header(method, url, consumer_key=_secret(self.s.rdf_drop_x_api_key),
                             consumer_secret=_secret(self.s.rdf_drop_x_api_secret),
                             token=_secret(self.s.rdf_drop_x_access_token),
                             token_secret=_secret(self.s.rdf_drop_x_access_secret))

    def post(self, http: httpx.Client, p: DropPost, png: bytes) -> PostResult:
        api = self.s.rdf_drop_x_api.rstrip("/")
        up = f"{api}/2/media/upload"
        try:
            r = http.post(up, headers={"Authorization": self._auth("POST", up)},
                          data={"media_category": "tweet_image"}, files={"media": ("card.png", png, "image/png")})
        except Exception as e:  # noqa: BLE001
            raise _safe_http_error(e) from None
        if r.status_code >= 300:
            raise _api_error(r, "x media upload")
        j = r.json()
        media_id = str((j.get("data") or {}).get("id") or j.get("media_id_string") or "")
        if not media_id:
            raise ChannelError("x media upload returned no media id")
        text = self.text(p)
        tw = f"{api}/2/tweets"
        try:
            r = http.post(tw, headers={"Authorization": self._auth("POST", tw)},
                          json={"text": text, "media": {"media_ids": [media_id]}})
        except Exception as e:  # noqa: BLE001
            raise _safe_http_error(e) from None
        if r.status_code >= 300:
            raise _api_error(r, "x create post")
        pid = str((r.json().get("data") or {}).get("id") or "")
        cost = self.cost(text)
        return PostResult(post_id=pid, post_url=f"https://x.com/i/web/status/{pid}" if pid else None,
                          cost_estimate_usd=cost,
                          note=f"x pay-per-use: post ${cost:.3f}" + (" (has a URL)" if cost > X_COST_POST_USD else
                                                                     ", media upload not priced on the docs page"))

    # -- lifecycle. X posts can't be edited; on a claim the post stays up (deleting and reposting on a
    # release would bill twice). It is deleted when the task is finished.
    def mark_claimed(self, http: httpx.Client, rec: dict, p: DropPost) -> str:
        return "unsupported (X posts can't be edited; deleted when the task finishes)"

    def remove(self, http: httpx.Client, rec: dict) -> str:
        pid = re.sub(r"\D", "", str(rec.get("post_id") or ""))
        if not pid:
            raise ChannelError("x: no post id stored for this post")
        url = f"{self.s.rdf_drop_x_api.rstrip('/')}/2/tweets/{pid}"
        try:
            r = http.delete(url, headers={"Authorization": self._auth("DELETE", url)})
        except Exception as e:  # noqa: BLE001
            raise _safe_http_error(e) from None
        if r.status_code == 404:
            return "gone"
        if r.status_code >= 300:
            raise _api_error(r, "x delete post")
        return "deleted"


# --------------------------------------------------------------------------- Telegram

class Telegram:
    name = "telegram"
    size = "square"

    def __init__(self, s: Any, *, sleep: Sleep = time.sleep) -> None:
        self.s = s
        self.sleep = sleep

    @property
    def configured(self) -> bool:
        return bool(_secret(self.s.rdf_drop_telegram_bot_token) and (self.s.rdf_drop_telegram_chat_id or "").strip())

    def text(self, p: DropPost) -> str:
        lines = [("Sample post, no task behind it. " if p.sample else "") + headline(p), p.title,
                 _first_sentence(p.instructions, 300)]
        if access_line(p):
            lines.append(access_line(p))
        lines.append(f"Claim it: {p.link}" if p.link else "Link coming soon.")
        return "\n".join(x for x in lines if x)[:TELEGRAM_MAX_CAPTION]

    def post(self, http: httpx.Client, p: DropPost, png: bytes) -> PostResult:
        url = f"{self.s.rdf_drop_telegram_api.rstrip('/')}/bot{_secret(self.s.rdf_drop_telegram_bot_token)}/sendPhoto"
        data: dict[str, str] = {"chat_id": self.s.rdf_drop_telegram_chat_id.strip(), "caption": self.text(p)}
        if p.link.startswith("https://"):
            data["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": f"Claim for {p.price}", "url": p.link}]]})
        for attempt in range(DISCORD_MAX_RETRIES + 1):
            try:
                r = http.post(url, data=data, files={"photo": ("card.png", png, "image/png")})
            except Exception as e:  # noqa: BLE001
                raise _safe_http_error(e) from None  # never str(e): the URL holds the bot token
            if r.status_code == 429 and attempt < DISCORD_MAX_RETRIES:
                self.sleep(min(_retry_after(r), MAX_RETRY_WAIT_S))
                continue
            if r.status_code >= 300:
                raise _api_error(r, "telegram")
            res = r.json().get("result") or {}
            mid = str(res.get("message_id") or "")
            uname = (res.get("chat") or {}).get("username")
            return PostResult(post_id=mid, post_url=f"https://t.me/{uname}/{mid}" if uname and mid else None,
                              note="telegram")
        raise ChannelError("telegram rate limited", status=429, retryable=True)

    # -- lifecycle. A caption edit would leave the QR card up, so a claimed task's message is deleted too
    # (a reopen reposts). Bots can delete their own channel messages.
    def remove(self, http: httpx.Client, rec: dict) -> str:
        mid = re.sub(r"\D", "", str(rec.get("post_id") or ""))
        if not mid:
            raise ChannelError("telegram: no message id stored for this post")
        url = f"{self.s.rdf_drop_telegram_api.rstrip('/')}/bot{_secret(self.s.rdf_drop_telegram_bot_token)}/deleteMessage"
        try:
            r = http.post(url, data={"chat_id": self.s.rdf_drop_telegram_chat_id.strip(), "message_id": mid})
        except Exception as e:  # noqa: BLE001
            raise _safe_http_error(e) from None  # never str(e): the URL holds the bot token
        if r.status_code == 400 and "not found" in r.text.lower():
            return "gone"
        if r.status_code >= 300:
            raise _api_error(r, "telegram deleteMessage")
        return "deleted"

    def mark_claimed(self, http: httpx.Client, rec: dict, p: DropPost) -> str:
        return self.remove(http, rec)


# --------------------------------------------------------------------------- helpers

def _png_size(png: bytes) -> tuple[int, int]:
    if png[:8] == b"\x89PNG\r\n\x1a\n":
        return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        return im.size


def _to_jpeg_under(png: bytes, limit: int) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(png)) as im:
        rgb = im.convert("RGB")
        for q in (92, 85, 78, 70, 60):
            out = io.BytesIO()
            rgb.save(out, format="JPEG", quality=q, optimize=True)
            if out.tell() <= limit:
                return out.getvalue()
        return out.getvalue()


CHANNELS = ("discord", "bluesky", "x", "telegram")


def build_channels(settings: Any, *, sleep: Sleep = time.sleep) -> dict[str, Any]:
    return {"discord": Discord(settings, sleep=sleep), "bluesky": Bluesky(settings), "x": X(settings),
            "telegram": Telegram(settings, sleep=sleep)}
