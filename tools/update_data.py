#!/usr/bin/env python3
"""Докачивает минутные котировки Dukascopy (3-минутные свечи) и новости Forex Factory.

Запускается GitHub Actions раз в день. Только стандартная библиотека Python.
"""
import concurrent.futures as cf
import datetime as dt
import json
import lzma
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SYMBOLS = [("EURUSD", "EURUSD"), ("GER40", "DEUIDXEUR")]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
DUKA = os.environ.get("REPLAY_DUKA_BASE", "https://datafeed.dukascopy.com")
FF = os.environ.get("REPLAY_FF_BASE", "https://www.forexfactory.com")


def get(url, tries=5):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, b""
            last = e
        except Exception as e:  # сеть, таймаут
            last = e
        time.sleep(0.8 * (a + 1))
    raise RuntimeError(f"{url}: {last}")


# ---------- компактный формат котировок ----------
def decode_series(text):
    lines = text.split("\n")
    name, sc, t, pc = lines[0].strip().split(",")
    sc, t, pc = int(sc), int(t) - 180, int(pc)
    bars = []
    for ln in lines[1:]:
        if not ln:
            continue
        p = ln.split(",")
        t += (int(p[0]) if p[0] else 1) * 180
        o = pc + int(p[1])
        b = (t, o, o + int(p[2]), o - int(p[3]), o + int(p[4]))
        pc = b[4]
        bars.append(b)
    return name, sc, bars


def encode_series(name, sc, bars):
    t0, o0 = bars[0][0], bars[0][1]
    out = [f"{name},{sc},{t0},{o0}"]
    pt, pc = t0 - 180, o0
    for t, o, h, l, c in bars:
        d = (t - pt) // 180
        out.append(f"{'' if d == 1 else d},{o - pc},{h - o},{o - l},{c - o}")
        pt, pc = t, c
    return "\n".join(out)


def decode_bi5(raw, day_start):
    if not raw:
        return []
    data = lzma.decompress(raw, format=lzma.FORMAT_ALONE)
    out, cur = [], -1
    for i in range(0, len(data) - 23, 24):
        sec, o, c, l, h = struct.unpack(">5i", data[i:i + 20])
        (v,) = struct.unpack(">f", data[i + 20:i + 24])
        if v == 0 and o == c and l == h and o == l:
            continue
        ts = day_start + sec
        bk = ts // 180 * 180
        if bk != cur:
            cur = bk
            out.append([bk, o, h, l, c])
        else:
            b = out[-1]
            b[2] = max(b[2], h)
            b[3] = min(b[3], l)
            b[4] = c
    return [tuple(b) for b in out]


def fetch_day(sym, day):
    url = f"{DUKA}/datafeed/{sym}/{day.year:04d}/{day.month - 1:02d}/{day.day:02d}/BID_candles_min_1.bi5"
    st, raw = get(url)
    if st == 404:
        return []
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp())
    return decode_bi5(raw, start)


def update_quotes():
    msgs = []
    yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    for name, duka in SYMBOLS:
        path = os.path.join(DATA, name + ".txt")
        nm, sc, bars = decode_series(open(path, encoding="utf-8").read())
        last_t = bars[-1][0]
        day = dt.datetime.fromtimestamp(last_t, dt.timezone.utc).date()
        days = []
        while day <= yesterday:
            if day.weekday() != 5:  # суббота без торгов
                days.append(day)
            day += dt.timedelta(days=1)
        with cf.ThreadPoolExecutor(4) as ex:
            results = list(ex.map(lambda d: _safe(fetch_day, duka, d), days))
        added = 0
        for d, res in zip(days, results):
            if isinstance(res, Exception):
                msgs.append(f"{name}: {d} не скачался ({res}), остановился")
                break
            for b in res:
                if b[0] > last_t:
                    bars.append(b)
                    last_t = b[0]
                    added += 1
        if added:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(encode_series(nm, sc, bars))
            os.replace(tmp, path)
        msgs.append(f"{name}: +{added} свечей, данные до "
                    f"{dt.datetime.fromtimestamp(last_t, dt.timezone.utc):%Y-%m-%d %H:%M} UTC")
    return msgs


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:
        return e


# ---------- новости ----------
STATE = re.compile(r"calendarComponentStates\[1\]\s*=\s*\{\s*days:\s*")
MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_ff_month(html):
    m = STATE.search(html)
    if not m or html[m.end():m.end() + 1] != "[":
        raise ValueError("на странице нет календаря (возможно, проверка Cloudflare)")
    i = m.end()
    depth, j, in_s = 0, i, False
    while j < len(html):
        c = html[j]
        if in_s:
            if c == "\\":
                j += 1
            elif c == '"':
                in_s = False
        elif c == '"':
            in_s = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                break
        j += 1
    days = json.loads(html[i:j + 1])
    out = []
    for d in days:
        for e in d.get("events", []):
            imp = {"high": 3, "medium": 2}.get(e.get("impactName"))
            if e.get("currency") not in ("EUR", "USD") or not imp:
                continue
            out.append([e["dateline"], e["currency"], imp, e.get("name", ""), e.get("timeLabel", ""),
                        e.get("actual", ""), e.get("forecast", ""), e.get("previous", "")])
    return out


def parse_weekly(raw):
    out = []
    for w in json.loads(raw):
        imp = {"high": 3, "medium": 2}.get(str(w.get("impact", "")).lower())
        if w.get("country") not in ("EUR", "USD") or not imp:
            continue
        try:
            t = int(dt.datetime.fromisoformat(w["date"]).timestamp())
        except Exception:
            continue
        out.append([t, w["country"], imp, w.get("title", ""), "", "", w.get("forecast", ""), w.get("previous", "")])
    return out


def merge(old, fresh):
    if not fresh:
        return old
    lo, hi = min(e[0] for e in fresh), max(e[0] for e in fresh)
    keep = [e for e in old if not (lo <= e[0] <= hi)]
    return sorted(keep + fresh, key=lambda e: e[0])


def update_news():
    path = os.path.join(DATA, "news.json")
    events = json.load(open(path, encoding="utf-8"))
    now = dt.date.today()
    first = dt.date(now.year, now.month, 1)
    months = [(first.replace(day=1) - dt.timedelta(days=1)).replace(day=1), first,
              (first + dt.timedelta(days=32)).replace(day=1)]
    got, fails = 0, 0
    for m in months:
        try:
            st, raw = get(f"{FF}/calendar?month={MON[m.month - 1]}.{m.year}", tries=2)
            ev = parse_ff_month(raw.decode("utf-8", "replace"))
            events = merge(events, ev)
            got += len(ev)
        except Exception as e:
            fails += 1
            print("Forex Factory:", m, e, file=sys.stderr)
        time.sleep(0.6)
    if fails:
        try:
            st, raw = get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", tries=2)
            ev = parse_weekly(raw)
            if ev:
                lo, hi = min(e[0] for e in ev), max(e[0] for e in ev)
                # события, у которых уже есть факт, не затираем
                have = {(e[0], e[3]) for e in events if lo <= e[0] <= hi and e[5]}
                events = merge([e for e in events if not (lo <= e[0] <= hi) or (e[0], e[3]) in have],
                               [e for e in ev if (e[0], e[3]) not in have]) if have else merge(events, ev)
                got += len(ev)
        except Exception as e:
            print("Недельный календарь:", e, file=sys.stderr)
    if got:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    return [f"Новости: {got} событий обновлено" if got else "Новости: Forex Factory не отдал календарь"]


if __name__ == "__main__":
    for line in update_quotes() + update_news():
        print(line)
