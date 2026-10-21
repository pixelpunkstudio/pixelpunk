"""Pixel Punk Studio social publisher: publishes the scheduled Pinterest pins and Instagram posts that are due.

Runs on GitHub every 15 minutes (.github/workflows/social.yml) so the PC can be off; the PC's scripts/social.py
plans, renders and schedules, and imports this file for the shared rules. Everything it needs is in this
repository: content.yaml (texts), state.json (status, planned and published times, links), the images under
media/. Missed slots are re-planned first (catch_up), never published in a burst.

Secrets (GitHub repository secrets; on the PC, the Windows variables in LOCAL_NAMES): PINTEREST_APP_ID,
PINTEREST_APP_SECRET, PINTEREST_REFRESH_TOKEN, PINTEREST_SANDBOX_TOKEN, META_PAGE_TOKEN, IG_USER_ID.

    python _social/runner.py            publish what is due (and re-plan what was missed)
    python _social/runner.py --check    connections only, nothing published
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

HERE = Path(__file__).resolve().parent
SITE = HERE.parent  # the website repository
CONTENT, STATE, LOG = HERE / "content.yaml", HERE / "state.json", HERE / "log.txt"
SITE_URL = "https://pixelpunkstudio.shop/"
TZ = ZoneInfo("America/New_York")
# A daily cap, hours apart, a book not twice in a row (same ideas as the Big Bang Pixels scheduler). New York hours,
# picked to fall in the afternoon and evening in Switzerland.
RULES = {
    "pinterest": {"times": ["14", "11", "8"], "per_day": 3, "gap_hours": 3, "same_book_days": 4},
    "instagram": {"times": ["12", "9", "13"], "per_day": 1, "gap_hours": 4, "same_book_days": 6, "every_days": 2},
}
GRACE = dt.timedelta(minutes=45)  # a slot found this late is still published as planned; later = missed
LOCAL_NAMES = {"PINTEREST_APP_ID": "PINTEREST_PPS_APP_ID", "PINTEREST_APP_SECRET": "PINTEREST_PPS_APP_SECRET",
               "PINTEREST_SANDBOX_TOKEN": "PINTEREST_PPS_SANDBOX_TOKEN", "META_PAGE_TOKEN": "META_PAGE_TOKEN_PIXELPUNK",
               "IG_USER_ID": "IG_USER_ID_PIXELPUNK"}


def secret(name: str) -> str:
    v = os.environ.get(name, "")
    if not v and sys.platform == "win32":  # on the PC
        if name == "PINTEREST_REFRESH_TOKEN":
            p = Path.home() / ".pixelpunk" / "pinterest.json"
            v = json.loads(p.read_text(encoding="utf-8")).get("refresh_token", "") if p.exists() else ""
        else:
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as h:
                    v = winreg.QueryValueEx(h, LOCAL_NAMES.get(name, name))[0]
            except OSError:
                pass
    return v.strip()


def log(msg: str):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now(TZ):%Y-%m-%d %H:%M} NY  {msg}\n")


def items() -> dict:
    return {i["key"]: i for i in yaml.safe_load(CONTENT.read_text(encoding="utf-8"))["items"]}


def load_state() -> dict:
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def save_state(st: dict):
    STATE.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")


def images_of(it: dict) -> list[Path]:
    if it["platform"] == "instagram":
        return [SITE / "media" / "social" / "instagram" / f"{it['key']}-{n}.jpg" for n in (1, 2, 3)]
    return [SITE / it["image"]] if it.get("image") else [SITE / "media" / "social" / "pins" / f"{it['key']}.jpg"]


def when_of(s: dict) -> dt.datetime | None:
    """The moment an item went out (published) or is planned (scheduled), in New York time."""
    v = s.get("published_at") or s.get("when")
    try:
        return dt.datetime.fromisoformat(v).astimezone(TZ) if v else None
    except ValueError:
        return None


# ---------------------------------------------------------------- slots

def assign(platform: str, keys: list[str], st: dict, its: dict, earliest: dt.datetime | None = None,
           salt: dict | None = None) -> dict:
    """Date and time for each key: earliest day that keeps the daily cap, the gap and the book spacing, within
    the item's season, at one of the platform's hours with a minute that looks human (stable per item).
    Published and scheduled items both count, so a catch-up never crowds a day. `earliest`: first allowed moment
    (default: tomorrow); `salt` ({key: text}) changes an item's minute when it is moved after a missed day."""
    r = RULES[platform]
    taken, books = {}, {}  # day -> [datetimes], day -> {book}
    for k, s in st.items():
        if k in keys or its.get(k, {}).get("platform") != platform:
            continue
        if s.get("status") in ("scheduled", "published") and when_of(s):
            t = when_of(s)
            taken.setdefault(t.date(), []).append(t)
            books.setdefault(t.date(), set()).add(its[k]["book"])
    out = {}
    now = dt.datetime.now(TZ)
    earliest = earliest or dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time(0), tzinfo=TZ)
    for k in sorted(keys, key=lambda k: (its[k].get("priority", 9), k)):
        it = its[k]
        d = max(earliest.date(), dt.date.fromisoformat(str(it["start"])) if it.get("start") else earliest.date())
        until = dt.date.fromisoformat(str(it["until"])) if it.get("until") else None
        minute = hashlib.sha256((k + (salt or {}).get(k, "")).encode()).digest()[0] % 60
        while True:
            if until and d > until:
                out[k] = None
                break
            near = [d + dt.timedelta(days=x) for x in range(-r["same_book_days"] + 1, r["same_book_days"])]
            spaced = all(it["book"] not in books.get(x, set()) for x in near)
            if platform == "instagram" and taken:
                spaced = spaced and all(abs((d - x).days) >= r.get("every_days", 1) for x in taken)
            if len(taken.get(d, [])) < r["per_day"] and spaced:
                slot = next((t0 for t0 in (dt.datetime(d.year, d.month, d.day, int(h), minute, tzinfo=TZ) for h in r["times"])
                             if t0 >= earliest and all(abs(t0 - t) >= dt.timedelta(hours=r["gap_hours"])
                                                       for t in taken.get(d, []))), None)
                if slot:
                    taken.setdefault(d, []).append(slot)
                    books.setdefault(d, set()).add(it["book"])
                    out[k] = slot
                    break
            d += dt.timedelta(days=1)
    return out


def catch_up(st: dict, its: dict, now: dt.datetime) -> list[str]:
    """Items whose time passed while nothing ran: missed and upcoming items are re-planned together by priority from
    now on (same rules), so missed ones come back first and nothing goes out in a burst. Past its season: expired."""
    moved = []
    for platform in RULES:
        sched = [k for k, s in st.items() if s.get("status") == "scheduled" and its.get(k, {}).get("platform") == platform
                 and when_of(s)]
        missed = [k for k in sched if when_of(st[k]) < now - GRACE]
        if not missed:
            continue
        upcoming = [k for k in sched if k not in missed and when_of(st[k]) > now + dt.timedelta(minutes=20)]
        stamp = now.strftime("%Y%m%d%H")
        slots = assign(platform, missed + upcoming, st, its, earliest=now + dt.timedelta(minutes=20),
                       salt={k: stamp for k in missed})
        for k in upcoming:
            if slots.get(k) and slots[k].isoformat() != st[k]["when"]:
                st[k].setdefault("first_planned", st[k]["when"])
                st[k]["when"] = slots[k].isoformat()
        for k in missed:
            s, old = st[k], st[k]["when"]
            s.setdefault("first_planned", old)
            s.setdefault("history", []).append({"missed": old, "noticed": now.isoformat(timespec="minutes"),
                                                "moved_to": slots[k].isoformat() if slots.get(k) else None})
            if slots.get(k):
                s["when"] = slots[k].isoformat()
                s["moves"] = s.get("moves", 0) + 1
                log(f"missed {k} ({old[:16]}) -> moved to {slots[k]:%Y-%m-%d %H:%M}")
            else:
                s.update(status="expired", note="missed and its season is over")
                log(f"missed {k} ({old[:16]}) -> expired (season over)")
            moved.append(k)
    return moved


# ---------------------------------------------------------------- Pinterest

PIN_API = {False: "https://api.pinterest.com/v5", True: "https://api-sandbox.pinterest.com/v5"}
_pin_token = {}


def pin_tokens() -> list[tuple[str, bool]]:
    """Tokens to try, in order: the sign-in token (renewed from the refresh token), then the app's Sandbox token,
    which publishes real pins while the app is on trial access."""
    out = []
    if "oauth" not in _pin_token and secret("PINTEREST_REFRESH_TOKEN"):
        basic = base64.b64encode(f"{secret('PINTEREST_APP_ID')}:{secret('PINTEREST_APP_SECRET')}".encode()).decode()
        r = requests.post(PIN_API[False] + "/oauth/token", timeout=30,
                          headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                          data={"grant_type": "refresh_token", "refresh_token": secret("PINTEREST_REFRESH_TOKEN")})
        _pin_token["oauth"] = r.json().get("access_token", "") if r.ok else ""
    if _pin_token.get("oauth"):
        out.append((_pin_token["oauth"], False))
    if secret("PINTEREST_SANDBOX_TOKEN"):
        out.append((secret("PINTEREST_SANDBOX_TOKEN"), True))
    return out


def pin_call(method: str, path: str, **kw) -> dict:
    errors = []
    for token, sandbox in pin_tokens():
        r = requests.request(method, PIN_API[sandbox] + path, timeout=60, headers={"Authorization": f"Bearer {token}"}, **kw)
        if r.ok:
            return r.json() if r.text else {}
        errors.append(f"{'sandbox' if sandbox else 'sign-in'} {r.status_code}: {r.text[:200]}")
    raise RuntimeError(f"{method} {path}: " + " | ".join(errors or ["no Pinterest token"]))


def publish_pin(it: dict) -> str:
    boards = {b["name"]: b["id"] for b in pin_call("GET", "/boards", params={"page_size": 100}).get("items", [])}
    if it["board"] not in boards:
        boards[it["board"]] = pin_call("POST", "/boards", json={"name": it["board"]})["id"]
    data = base64.b64encode(images_of(it)[0].read_bytes()).decode()
    made = pin_call("POST", "/pins", json={
        "board_id": boards[it["board"]], "title": it["title"][:100], "description": it["description"][:500],
        "link": it["link"], "alt_text": it.get("alt", "")[:500],
        "media_source": {"source_type": "image_base64", "content_type": "image/jpeg", "data": data}})
    return f"https://www.pinterest.com/pin/{made['id']}/"


# ---------------------------------------------------------------- Instagram (Graph API, page token)

def graph(method: str, path: str, **params) -> dict:
    params["access_token"] = secret("META_PAGE_TOKEN")
    r = requests.request(method, "https://graph.facebook.com/v21.0" + path, data=params if method == "POST" else None,
                         params=params if method == "GET" else None, timeout=60)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def ig_urls(it: dict) -> list[str]:
    return [SITE_URL + f.relative_to(SITE).as_posix() for f in images_of(it)]


def publish_ig(it: dict) -> str:
    ig = secret("IG_USER_ID")
    urls = ig_urls(it)
    for u in urls:  # the slides must be online (they are published with the website)
        requests.head(u, timeout=30).raise_for_status()
    children = [graph("POST", f"/{ig}/media", image_url=u, is_carousel_item="true")["id"] for u in urls]
    box = graph("POST", f"/{ig}/media", media_type="CAROUSEL", children=",".join(children), caption=it["caption"])["id"]
    for _ in range(30):
        if graph("GET", f"/{box}", fields="status_code").get("status_code") == "FINISHED":
            break
        time.sleep(4)
    media = graph("POST", f"/{ig}/media_publish", creation_id=box)["id"]
    return graph("GET", f"/{media}", fields="permalink").get("permalink", media)


# ---------------------------------------------------------------- run

def run() -> bool:
    st, its = load_state(), items()
    now = dt.datetime.now(TZ)
    changed = bool(catch_up(st, its, now))
    for platform, publish in (("pinterest", publish_pin), ("instagram", publish_ig)):
        due = sorted((when_of(s), k) for k, s in st.items() if s.get("status") == "scheduled"
                     and its.get(k, {}).get("platform") == platform and when_of(s) and now - GRACE <= when_of(s) <= now)
        if not due:
            continue
        k = due[0][1]  # one item per platform per run: the next one waits for its own time
        s = st[k]
        try:
            url = publish(its[k])
            s.update(status="published", url=url, published_at=now.isoformat(timespec="minutes"))
            log(f"published {k} -> {url}")
        except Exception as e:
            s.update(status="failed", error=str(e)[:300])
            log(f"FAILED {k}: {str(e)[:200]}")
        changed = True
    if changed:
        save_state(st)
    return changed


def check():
    """Connections only: nothing is created or published."""
    for token, sandbox in pin_tokens():
        r = requests.get(PIN_API[sandbox] + "/user_account", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        print(f"Pinterest {'sandbox' if sandbox else 'sign-in'} token:", r.status_code, r.json().get("username") if r.ok else r.text[:120])
    me = graph("GET", f"/{secret('IG_USER_ID')}", fields="username")
    print("Instagram:", me.get("username"))
    st = load_state()
    print("scheduled:", sum(1 for s in st.values() if s.get("status") == "scheduled"))


if __name__ == "__main__":
    check() if "--check" in sys.argv else print("changed" if run() else "nothing due")
