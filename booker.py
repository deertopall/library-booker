#!/usr/bin/env python3
"""
Library seat booker for Affluences (Universita Cattolica Roma).

Runs on a small server (not at home). Started a couple of minutes before
midnight Rome time, it waits for the booking window of (today + 2 days) to
open, picks the best seat that is free all day, and books it as 1-3 chained
bookings (the platform caps each booking at 4 hours).

Stdlib only (Python 3.9+). Never cancels or modifies anything.

Usage:
  booker.py                  # normal run (what cron/systemd calls)
  booker.py --dry-run        # does everything except the final POST
  booker.py --date 2026-11-24 --dry-run
  booker.py --fixture f.json --date 2026-11-19 --dry-run   # offline test

Config (environment variables, or a KEY=VALUE file via --env-file):
  LB_EMAIL          uni email used on the booking form
  LB_MATRICOLA      numero di matricola (goes in the "note" field)
  LB_DEVICE_TOKEN   value of the AffluencesTrustedDeviceToken cookie
                    (makes bookings auto-confirm; see README)
  LB_NTFY_TOPIC     optional ntfy.sh topic for push notifications
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")
API = "https://reservation.affluences.com/api"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")

START_MIN = 10 * 60         # hold a seat from 10:00
SPLIT_MIN = 13 * 60         # morning block ends at 13:00
MAX_BLOCK = 240             # platform max per booking (minutes)
OPEN_WAIT_SECONDS = 180     # how long to keep polling after midnight

# --- libraries ---------------------------------------------------------
LIBS = {
    "rossa": {"name": "Policlinico Sala Rossa", "type": 4993,
              "site": "1a3985b0-f580-46d3-a966-ac1109e7ac9a"},
    "polo": {"name": "Polo Giovanni XXIII", "type": 4990,
             "site": "a2eff52e-5047-4342-b3cd-2e2fe3f684de"},
    "bio": {"name": "Istituti Biologici", "type": 4985,
            "site": "bcabea9e-a612-4a8d-bf3d-db401312059c"},
}

ROSSA_ORDER = [68, 70, 72, 74, 78, 76, 89, 87, 85, 83, 81, 79, 73, 71, 69, 67]
ROSSA_WALL = {80, 82, 84, 86, 88, 90}          # back faces a wall: last resort
POLO_ORDER = [65, 66, 64, 2, 3, 70, 72, 74, 85, 87, 89,
              69, 71, 73, 86, 88, 90, 68, 83, 67, 84]
BIO_ORDER = [53, 54, 55, 56, 57, 58]


def rank_seat(lib, n):
    """Lower is better; None = never book this seat."""
    if lib == "rossa":
        if n in ROSSA_ORDER:
            return ROSSA_ORDER.index(n)
        if 67 <= n <= 90:
            return 300 + n if n in ROSSA_WALL else 100 + n
        if 49 <= n <= 66:
            return 500 + n
        return None
    if lib == "polo":
        if n in POLO_ORDER:
            return POLO_ORDER.index(n)
        if 67 <= n <= 90:
            return 100 + n
        return None
    if lib == "bio":
        if n in BIO_ORDER:
            return BIO_ORDER.index(n)
        if n > 25:
            return 100 + n
        return None
    return None


def primary_library(target):
    """Policlinico Sala Rossa every weekday (other libraries are only fallbacks)."""
    return "rossa"


# --- http --------------------------------------------------------------
def http(method, url, body=None, headers=None, timeout=10, tries=3):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"User-Agent": UA, "Accept": "application/json",
            "Origin": "https://affluences.com", "Referer": "https://affluences.com/"}
    if data is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    last = None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(txt) if txt.strip() else None)
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(txt)
            except ValueError:
                parsed = txt[:300]
            if e.code < 500 or method == "POST":     # never blindly retry a POST
                return e.code, parsed
            last = (e.code, parsed)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = (0, str(e))
            if method == "POST":
                return last
        time.sleep(0.4 * (i + 1))
    return last


def fetch_seats(lib, date, fixture=None):
    if fixture is not None:
        raw = fixture.get(lib, [])
    else:
        L = LIBS[lib]
        q = urllib.parse.urlencode({"date": date, "type": L["type"]})
        status, raw = http("GET", f"{API}/resources/{L['site']}/available?{q}")
        if status != 200 or not isinstance(raw, list):
            raise RuntimeError(f"availability {lib} {date}: HTTP {status} {str(raw)[:200]}")
    seats = []
    for r in raw:
        m = re.search(r"(\d+)\s*$", r.get("resource_name", ""))
        if not m:
            continue
        slots = []
        for h in r.get("hours") or []:
            hh, mm = h["hour"].split(":")[:2]
            slots.append((int(hh) * 60 + int(mm), h.get("state") == "available",
                          h.get("granularity") or 30))
        seats.append({"id": r["resource_id"], "num": int(m.group(1)), "slots": slots})
    return seats


# --- seat logic --------------------------------------------------------
def day_end(seats):
    ends = [m + g for s in seats for (m, _, g) in s["slots"]]
    return max(ends) if ends else None


def free_run(seat, start_min=None):
    """(start, end) of the contiguous free run starting at the first free slot >= START_MIN."""
    if start_min is None:
        start_min = START_MIN
    free = {m: g for (m, ok, g) in seat["slots"] if ok}
    starts = sorted(m for m in free if m >= start_min)
    if not starts:
        return None
    s = starts[0]
    e = s
    while e in free:
        e += free[e]
    return s, e


def rank_candidates(lib, seats, end):
    out = []
    for seat in seats:
        rk = rank_seat(lib, seat["num"])
        run = free_run(seat)
        if rk is None or run is None:
            continue
        s, e = run
        e = min(e, end)
        full = (e == end)
        tier = 0 if (full and s <= START_MIN) else (1 if full else 2)
        key = (tier, s if tier == 1 else 0, -(e - s) if tier == 2 else 0, rk)
        out.append((key, seat, s, e, full))
    out.sort(key=lambda t: t[0])
    return out


def split_blocks(s, e):
    cuts = [s]
    if s < SPLIT_MIN < e:
        cuts.append(SPLIT_MIN)
    cuts.append(e)
    blocks = []
    for a, b in zip(cuts, cuts[1:]):
        while b - a > MAX_BLOCK:
            blocks.append((a, a + MAX_BLOCK))
            a += MAX_BLOCK
        blocks.append((a, b))
    return blocks


def hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


# --- device token ------------------------------------------------------
def load_device_token(raw, email):
    """Cookie value (JSON list of {id, token, expiration}); returns (token, expiry_dt) or (None, None)."""
    if not raw:
        return None, None
    raw = raw.strip()
    for attempt in (raw, urllib.parse.unquote(raw)):
        try:
            arr = json.loads(attempt)
            break
        except ValueError:
            arr = None
    if arr is None:
        # a bare token string was pasted
        return raw, None
    if isinstance(arr, dict):
        arr = [arr]
    best = None
    for e in arr:
        try:
            exp = dt.datetime.fromisoformat(e["expiration"].replace("Z", "+00:00"))
        except Exception:
            exp = None
        if e.get("token") and (best is None or (exp and (best[1] is None or exp > best[1]))):
            best = (e["token"], exp)
    return best if best else (None, None)


# --- notifications -----------------------------------------------------
def notify(msg, title="Library booking", priority="default"):
    print(f"[notify] {title}: {msg}")
    topic = (os.environ.get("LB_NTFY_TOPIC") or "").strip()
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}", data=msg.encode(), method="POST",
            headers={"Title": title, "Priority": priority})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:                      # never fail the run over a notification
        print(f"[notify] failed: {e}")


# --- booking -----------------------------------------------------------
def reserve(rid, date, a, b, cfg, dry):
    body = {"auth_type": None, "email": cfg["email"], "date": date,
            "start_time": hhmm(a), "end_time": hhmm(b), "note": cfg["matricola"],
            "user_firstname": None, "user_lastname": None, "user_phone": None,
            "person_count": 1}
    if cfg.get("token"):
        body["user_device_token"] = cfg["token"]
    if dry:
        shown = dict(body, user_device_token="<set>" if cfg.get("token") else None)
        print(f"  DRY-RUN would POST /reserve/{rid}: {json.dumps(shown)}")
        return True, "dry-run", None
    status, resp = http("POST", f"{API}/reserve/{rid}", body=body, timeout=15)
    ok = status in (200, 201)                      # any 2xx = the server accepted it
    token = resp.get("reservation_token") if isinstance(resp, dict) else None
    detail = resp.get("success") if isinstance(resp, dict) and ok else resp
    if ok and not cfg.get("token"):
        detail = f"{detail} (needs email confirmation within 2h)"
    if status == 0:                                # timeout: it may or may not have gone through
        detail = f"UNKNOWN outcome, check Affluences: {resp}"
    return ok, f"HTTP {status} {detail}", token


def wait_for_open(lib, target, fixture):
    """Block until the target day has slots; returns the seat list."""
    if fixture is not None:
        return fetch_seats(lib, target, fixture)
    now = dt.datetime.now(ROME)
    midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour >= 12:                              # started before midnight: sleep until ~00:00
        wait = (midnight - now).total_seconds() - 0.5
        if wait > 0:
            print(f"sleeping {wait:.0f}s until midnight Rome")
            time.sleep(wait)
    deadline = time.time() + OPEN_WAIT_SECONDS + 5
    last_err = None
    while time.time() < deadline:
        try:
            seats = fetch_seats(lib, target)
            if seats and any(s["slots"] for s in seats):
                return seats
        except Exception as e:
            last_err = e
        time.sleep(0.3)
    raise RuntimeError(f"booking window for {target} did not open ({last_err})")


def run(args):
    now = dt.datetime.now(ROME)
    if args.date:
        target = dt.date.fromisoformat(args.date)
    else:
        target = (now + dt.timedelta(hours=2)).date() + dt.timedelta(days=2)
    if target.weekday() >= 5:
        print(f"{target} is a weekend: nothing to book")
        return 0

    email = os.environ.get("LB_EMAIL", "").strip()
    matricola = os.environ.get("LB_MATRICOLA", "").strip()
    if not (email and matricola):
        print("LB_EMAIL and LB_MATRICOLA must be set", file=sys.stderr)
        return 2
    token, exp = load_device_token(os.environ.get("LB_DEVICE_TOKEN"), email)
    cfg = {"email": email, "matricola": matricola, "token": token}
    warn = ""
    if not token:
        warn = " WARNING: no device token, bookings need email confirmation within 2h."
    elif exp:
        left = (exp - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400
        if left < 0:
            warn = " WARNING: device token EXPIRED, renew it (see README)."
            cfg["token"] = None
        elif left < 5:
            warn = f" Device token expires in {left:.1f} days, renew it soon."

    fixture = None
    if args.fixture:
        with open(args.fixture) as f:
            fixture = json.load(f)
    dry = args.dry_run

    primary = primary_library(target)
    print(f"target {target} ({target.strftime('%A')}), primary library: {LIBS[primary]['name']}")
    if args.no_wait:
        seats = fetch_seats(primary, target.isoformat(), fixture)
    else:
        seats = wait_for_open(primary, target.isoformat(), fixture)
    end = day_end(seats)
    if args.end:
        h, m = args.end.split(":")
        end = min(end, int(h) * 60 + int(m)) if end else None
    cands = rank_candidates(primary, seats, end)
    choice = None                                   # (lib, key, seat, s, e, full)
    if cands and cands[0][4]:
        k, seat, s, e, full = cands[0]
        choice = (primary, seat, s, e, full)
    else:
        for lib in [l for l in ("rossa", "polo", "bio") if l != primary]:
            try:
                other = fetch_seats(lib, target.isoformat(), fixture)
            except Exception as ex:
                print(f"  could not check {lib}: {ex}")
                continue
            oend = day_end(other)
            if args.end and oend:
                h, m = args.end.split(":")
                oend = min(oend, int(h) * 60 + int(m))
            oc = rank_candidates(lib, other, oend)
            if oc and oc[0][4]:
                k, seat, s, e, full = oc[0]
                choice = (lib, seat, s, e, full)
                end = oend
                break
        if choice is None and cands:
            k, seat, s, e, full = cands[0]
            choice = (primary, seat, s, e, False)

    if choice is None:
        notify(f"{target}: no usable seat in any library. Book manually!", priority="high")
        return 1

    lib, seat, s, e, full = choice
    blocks = split_blocks(s, e)
    print(f"chosen: {LIBS[lib]['name']} seat {seat['num']} {hhmm(s)}-{hhmm(e)} "
          f"(day ends {hhmm(end)}, full day: {full}); blocks: "
          + ", ".join(f"{hhmm(a)}-{hhmm(b)}" for a, b in blocks))

    results, failed = [], []
    for a, b in blocks:
        ok, detail, tok = reserve(seat["id"], target.isoformat(), a, b, cfg, dry)
        print(f"  {hhmm(a)}-{hhmm(b)}: {'OK' if ok else 'FAILED'} {detail}")
        (results if ok else failed).append((a, b, detail, tok))
        if not ok and not dry and not detail.startswith("HTTP 0"):
            # try another seat in the same library that is free for exactly this block
            alt_seats = fetch_seats(lib, target.isoformat(), fixture)
            for k2, s2, ss, ee, _ in rank_candidates(lib, alt_seats, day_end(alt_seats)):
                if s2["id"] == seat["id"]:
                    continue
                free = {m for (m, okk, g) in s2["slots"] if okk}
                if all(m in free for m in range(a, b, 30)):
                    ok2, d2, t2 = reserve(s2["id"], target.isoformat(), a, b, cfg, dry)
                    print(f"  retry seat {s2['num']}: {'OK' if ok2 else 'FAILED'} {d2}")
                    if ok2:
                        failed.pop()
                        results.append((a, b, f"seat {s2['num']} {d2}", t2))
                        break

    booked = ", ".join(f"{hhmm(a)}-{hhmm(b)}" for a, b, _, _ in results) or "nothing"
    covers = bool(results) and not failed and full
    msg = (f"{target.strftime('%a %d %b')}: {LIBS[lib]['name']} seat {seat['num']}, booked {booked}"
           f"{'' if covers else ' (NOT full day!)'}.{warn} Scan the QR within 30 min of start.")
    if dry:
        print("\n[dry run] " + msg)
        return 0
    notify(msg, priority="default" if covers else "high")
    return 0 if covers else 1


def load_env_file(path):
    with open(os.path.expanduser(path)) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="target date YYYY-MM-DD (default: today+2 after midnight)")
    ap.add_argument("--dry-run", action="store_true", help="never POST a booking")
    ap.add_argument("--fixture", help="JSON file of {lib: availability list} for offline tests")
    ap.add_argument("--no-wait", action="store_true", help="don't wait for midnight / window opening")
    ap.add_argument("--env-file", default="~/.library-booker.env")
    ap.add_argument("--start", help="override start time HH:MM (for tests)")
    ap.add_argument("--end", help="override end time HH:MM (for tests)")
    a = ap.parse_args()
    if a.start:
        h, m = a.start.split(":")
        START_MIN = int(h) * 60 + int(m)
    if os.path.exists(os.path.expanduser(a.env_file)):
        load_env_file(a.env_file)
    sys.exit(run(a))
