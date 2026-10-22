#!/usr/bin/env python3
"""Скачивает историю котировок и новостей по годам в data/hist/ (только недостающие годы).

Годы перечислены в data/hist/years.txt, например: 2015-2023
Приложение загружает эти файлы на устройство только по запросу пользователя.
"""
import concurrent.futures as cf
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update_data as u  # noqa: E402

HIST = os.path.join(u.DATA, "hist")
LOG = []


def log(msg):
    print(msg)
    LOG.append(f"{dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} {msg}")



def years_wanted():
    spec = open(os.path.join(HIST, "years.txt"), encoding="utf-8").read().strip()
    out = set()
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    return sorted(out, reverse=True)


def fetch_year(name, duka, year):
    days, d = [], dt.date(year, 1, 1)
    while d.year == year:
        if d.weekday() != 5:
            days.append(d)
        d += dt.timedelta(days=1)
    got = {}
    with cf.ThreadPoolExecutor(2) as ex:
        for day, r in zip(days, ex.map(lambda x: u._safe(u.fetch_day, duka, x), days)):
            if not isinstance(r, Exception):
                got[day] = r
    # повторяем неудачные дни по одному, с паузами: Dukascopy не любит частые запросы
    for attempt in range(3):
        left = [x for x in days if x not in got]
        if not left:
            break
        time.sleep(10 * (attempt + 1))
        for day in left:
            r = u._safe(u.fetch_day, duka, day)
            if not isinstance(r, Exception):
                got[day] = r
            time.sleep(1.5)
    left = [x for x in days if x not in got]
    if left:
        raise RuntimeError(f"{len(left)} дней не скачались, например {left[0]}")
    bars = [b for day in days for b in got[day]]
    bars.sort(key=lambda b: b[0])
    return bars


def fetch_news_year(year):
    events = []
    for m in range(12):
        st, raw = u.get(f"{u.FF}/calendar?month={u.MON[m]}.{year}", tries=3)
        events += u.parse_ff_month(raw.decode("utf-8", "replace"))
        time.sleep(0.5)
    lo = int(dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    hi = int(dt.datetime(year + 1, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    seen, out = set(), []
    for e in sorted(events, key=lambda e: e[0]):
        k = (e[0], e[1], e[3])
        if lo <= e[0] < hi and k not in seen:
            seen.add(k)
            out.append(e)
    return out


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def build_index():
    idx = {}
    for fn in sorted(os.listdir(HIST)):
        p = os.path.join(HIST, fn)
        if fn.endswith(".txt") and "-" in fn and fn != "years.txt":
            name, year = fn[:-4].rsplit("-", 1)
            nm, sc, bars = u.decode_series(open(p, encoding="utf-8").read())
            idx.setdefault(year, {})[name] = {"bytes": os.path.getsize(p), "bars": len(bars),
                                              "from": bars[0][0], "to": bars[-1][0]}
        elif fn.startswith("news-") and fn.endswith(".json"):
            year = fn[5:-5]
            idx.setdefault(year, {})["news"] = {"bytes": os.path.getsize(p),
                                                "events": len(json.load(open(p, encoding="utf-8")))}
    write(os.path.join(HIST, "index.json"), json.dumps(idx, ensure_ascii=False, separators=(",", ":")))
    if LOG:
        old = open(os.path.join(HIST, "log.txt"), encoding="utf-8").read().splitlines()[-200:] if os.path.exists(os.path.join(HIST, "log.txt")) else []
        write(os.path.join(HIST, "log.txt"), "\n".join(old + LOG) + "\n")
        LOG.clear()
    return idx


PARTS = os.path.join(HIST, "parts")


def fetch_month(duka, year, month):
    days, d = [], dt.date(year, month, 1)
    while d.month == month:
        if d.weekday() != 5:
            days.append(d)
        d += dt.timedelta(days=1)
    got = {}
    with cf.ThreadPoolExecutor(2) as ex:
        for day, r in zip(days, ex.map(lambda x: u._safe(u.fetch_day, duka, x), days)):
            if not isinstance(r, Exception):
                got[day] = r
    for attempt in range(2):
        left = [x for x in days if x not in got]
        if not left:
            break
        time.sleep(10 * (attempt + 1))
        for day in left:
            r = u._safe(u.fetch_day, duka, day)
            if not isinstance(r, Exception):
                got[day] = r
            time.sleep(1.5)
    left = [x for x in days if x not in got]
    if left:
        raise RuntimeError(f"{len(left)} дн. не скачались")
    return sorted((b for day in days for b in got[day]), key=lambda b: b[0])


def main():
    os.makedirs(PARTS, exist_ok=True)
    started = time.time()
    limit = float(os.environ.get("HIST_MINUTES", "25")) * 60
    build_index()
    years = years_wanted()
    # новости по годам качаются быстро — сначала они
    for year in years:
        npath = os.path.join(HIST, f"news-{year}.json")
        if not os.path.exists(npath):
            try:
                ev = fetch_news_year(year)
                write(npath, json.dumps(ev, ensure_ascii=False, separators=(",", ":")))
                log(f"Новости {year}: {len(ev)}")
            except Exception as e:
                log(f"Новости {year}: не скачались ({e})")
    for year in years:
        for name, duka in u.SYMBOLS:
            ypath = os.path.join(HIST, f"{name}-{year}.txt")
            none = os.path.join(HIST, f"{name}-{year}.none")
            if os.path.exists(ypath) or os.path.exists(none):
                continue
            for month in range(1, 13):
                part = os.path.join(PARTS, f"{name}-{year}-{month:02d}.txt")
                if os.path.exists(part):
                    continue
                if time.time() - started > limit:
                    log("Время вышло, продолжу в следующий запуск")
                    build_index()
                    return
                t0 = time.time()
                try:
                    bars = fetch_month(duka, year, month)
                    write(part, u.encode_series(name, 100000 if name == "EURUSD" else 1000, bars) if bars else "EMPTY")
                    log(f"{name} {year}-{month:02d}: {len(bars)} свечей за {time.time() - t0:.0f} с")
                except Exception as e:
                    log(f"{name} {year}-{month:02d}: ошибка за {time.time() - t0:.0f} с ({e})")
            parts = [os.path.join(PARTS, f"{name}-{year}-{m:02d}.txt") for m in range(1, 13)]
            if all(os.path.exists(x) for x in parts):
                bars = []
                for x in parts:
                    txt = open(x, encoding="utf-8").read()
                    if txt != "EMPTY":
                        bars += u.decode_series(txt)[2]
                if bars:
                    write(ypath, u.encode_series(name, 100000 if name == "EURUSD" else 1000, sorted(bars, key=lambda b: b[0])))
                    log(f"{name} {year}: год собран, {len(bars)} свечей")
                else:
                    write(none, "нет данных")
                    log(f"{name} {year}: у Dukascopy нет данных")
                for x in parts:
                    os.remove(x)
                build_index()
    build_index()


if __name__ == "__main__":
    main()
