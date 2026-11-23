#!/usr/bin/env python3
"""AI Crash Monitor – śledzi 6 sygnałów krachu AI i wysyła powiadomienia na Telegram.

Tryby:
  python monitor.py daily      – pełna ocena (ceny, H100, newsy, komendy, powiadomienia, dashboard)
  python monitor.py commands   – obsługa komend z Telegrama + newsy (pełna ocena tylko gdy trzeba)
  python monitor.py chatid     – pokazuje chat_id osób, które napisały do bota (konfiguracja)
  python monitor.py test       – wysyła testową wiadomość

Zmienne środowiskowe: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "state.json"
H100_PATH = ROOT / "data" / "h100.csv"
DASH_PATH = ROOT / "docs" / "data.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NY = ZoneInfo("America/New_York")
MANUAL_HINT = "bankrupt = bankructwo OpenAI/Anthropic, acquired = przejęcie OpenAI"

DEFAULT_STATE = {
    "initialized": False,
    "tg_offset": 0,
    "manual_events": {},   # {"bankrupt": [{"date": "YYYY-MM-DD", "note": "..."}]}
    "candidates": {},      # {id: {event, title, link, date, status}}
    "notified": [],        # klucze wysłanych powiadomień
    "failures": {},        # {"prices": n, "h100": n, "news": n}
    "failure_alerted": [],
}


# ───────────────────────── narzędzia ─────────────────────────

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    state = json.loads(json.dumps(DEFAULT_STATE))
    if STATE_PATH.exists():
        state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def today_ny() -> dt.date:
    return dt.datetime.now(NY).date()


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def pct(x: float) -> str:
    return f"{x * 100:.1f}%".replace(".", ",")


def money(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


# ───────────────────────── Telegram ─────────────────────────

def tg_api(method: str, **params):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", data=params, timeout=30)
    if not r.ok:
        print(f"[telegram] {method} błąd {r.status_code}: {r.text[:300]}")
        return None
    return r.json().get("result")


def tg_send(text: str) -> bool:
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not chat:
        print("──── [Telegram wyłączony – wiadomość] ────\n" + text + "\n──────────────")
        return True
    return tg_api("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                  disable_web_page_preview="true") is not None


# ───────────────────────── pobieranie danych ─────────────────────────

def fetch_prices(tickers: list[str]) -> dict[str, list[tuple[str, float]]]:
    """Dzienne ceny zamknięcia (skorygowane o splity, bez korekty o dywidendy), tylko zakończone sesje."""
    import yfinance as yf

    last_err = None
    for attempt in range(3):
        try:
            df = yf.download(tickers, period="max", auto_adjust=False, progress=False, threads=False)
            close = df["Close"]
            out = {}
            now = dt.datetime.now(NY)
            for t in tickers:
                s = close[t].dropna() if t in close else None
                if s is None or s.empty:
                    raise RuntimeError(f"brak danych dla {t}")
                pts = [(i.strftime("%Y-%m-%d"), float(v)) for i, v in s.items()]
                # odrzuć dzisiejszą, jeszcze niezakończoną sesję
                if pts and pts[-1][0] == now.date().isoformat() and (now.hour, now.minute) < (16, 30):
                    pts.pop()
                out[t] = pts
            return out
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[ceny] próba {attempt + 1} nieudana: {e}")
            time.sleep(15)
    raise RuntimeError(f"pobieranie cen nieudane: {last_err}")


def fetch_h100(cfg: dict) -> dict[str, float]:
    src = cfg["h100_source"]
    r = requests.get(src["url"], headers=UA, timeout=45)
    r.raise_for_status()
    txt = r.text.replace('\\"', '"')
    key = re.escape(src["series_key"])
    m = re.search(r'"key":"' + key + r'".*?"points":(\[.*?\])', txt, re.S)
    if not m:
        raise RuntimeError("nie znaleziono serii H100 na stronie SiliconData (zmiana struktury strony?)")
    pts = json.loads(m.group(1))
    out = {p["date"]: float(p["price"]) for p in pts}
    if not out:
        raise RuntimeError("pusta seria H100")
    return out


def load_h100() -> dict[str, float]:
    if not H100_PATH.exists():
        return {}
    out = {}
    for line in H100_PATH.read_text().splitlines()[1:]:
        if line.strip():
            date, price, *_ = line.split(",")
            out[date] = float(price)
    return out


def save_h100(series: dict[str, float]) -> None:
    lines = ["date,price"] + [f"{k},{v}" for k, v in sorted(series.items())]
    H100_PATH.write_text("\n".join(lines) + "\n")


def fetch_news(cfg: dict, state: dict) -> list[dict]:
    """Zwraca nowych kandydatów na zdarzenia korporacyjne (wymagają potwierdzenia)."""
    new = []
    for rule in cfg["news"]:
        q = urllib.parse.quote(rule["query"] + " when:3d")
        url = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        pat = re.compile(rule["pattern"], re.I)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            if not pat.search(title):
                continue
            cid = hashlib.sha1(title.lower().encode()).hexdigest()[:6]
            if cid in state["candidates"]:
                continue
            try:
                date = parsedate_to_datetime(item.findtext("pubDate")).date().isoformat()
            except Exception:  # noqa: BLE001
                date = today_ny().isoformat()
            cand = {"id": cid, "event": rule["event"], "title": title, "link": link,
                    "date": date, "status": "pending", "found": dt.datetime.utcnow().isoformat(timespec="minutes")}
            state["candidates"][cid] = cand
            new.append(cand)
    return new


# ───────────────────────── logika zdarzeń ─────────────────────────

def analyze_drawdown(points, threshold, warn_ratio, rearm_margin):
    """Liczy ATH (z cen zamknięcia, narastająco) i momenty przekroczenia progu spadku."""
    ath, ath_date = 0.0, None
    armed, warn_armed = True, True
    crossings, warn_crossings, below_days = [], [], []
    warn_thr = threshold * warn_ratio
    dd = 0.0
    for date, close in points:
        if close > ath:
            ath, ath_date = close, date
        dd = 1 - close / ath
        if dd >= threshold - 1e-12:
            below_days.append(date)
            if armed:
                crossings.append(date)
                armed = False
        elif dd < threshold - rearm_margin:
            armed = True
        if dd >= warn_thr - 1e-12:
            if warn_armed:
                warn_crossings.append(date)
                warn_armed = False
        elif dd < warn_thr - rearm_margin:
            warn_armed = True
    last_date, last_close = points[-1]
    return {
        "ath": ath, "ath_date": ath_date, "last_close": last_close, "last_date": last_date,
        "drawdown": dd, "threshold": threshold, "below": dd >= threshold - 1e-12,
        "crossings": crossings, "warn_crossings": warn_crossings, "below_days": below_days,
    }


def analyze_h100(series: dict[str, float], max_price: float, days: int, warn_price: float):
    """Szuka serii `days` kolejnych dni kalendarzowych z ceną ≤ max_price."""
    streak, prev = 0, None
    crossings, qualifying_days, warn_crossings = [], [], []
    warn_armed = True
    for date in sorted(series):
        price = series[date]
        consecutive = prev is not None and d(date) - d(prev) == dt.timedelta(days=1)
        if price <= max_price + 1e-9:
            streak = streak + 1 if consecutive or streak == 0 else 1
        else:
            streak = 0
        if streak >= days:
            qualifying_days.append(date)
            if streak == days:
                crossings.append(date)
        if price <= warn_price + 1e-9:
            if warn_armed:
                warn_crossings.append(date)
                warn_armed = False
        else:
            warn_armed = True
        prev = date
    last = max(series) if series else None
    return {"crossings": crossings, "qualifying_days": qualifying_days, "warn_crossings": warn_crossings,
            "streak": streak, "last_date": last, "last_price": series[last] if last else None}


def find_combo_episodes(occurrences: dict[str, list[str]], window: int, min_events: int,
                        until: dt.date, deadline: str | None = None):
    """Epizody, w których ≥ min_events różnych zdarzeń mieści się w oknie `window` dni (włącznie).

    Dzień X 'kwalifikuje się', gdy w oknie [X-window+1, X] wystąpiło ≥ min_events różnych zdarzeń.
    Kolejne kwalifikujące się dni tworzą jeden epizod."""
    dl = d(deadline) if deadline else None
    by_date: dict[dt.date, set] = {}
    for ev, dates in occurrences.items():
        for s in dates:
            x = d(s)
            if dl and x > dl:
                continue
            by_date.setdefault(x, set()).add(ev)
    if not by_date:
        return [], set()
    end = min(until, dl) if dl else until
    last_seen: dict[str, dt.date] = {}
    episodes, cur = [], None
    day = min(by_date)
    active_today: set = set()
    while day <= end:
        for ev in by_date.get(day, ()):
            last_seen[ev] = day
        lo = day - dt.timedelta(days=window - 1)
        active = {ev for ev, x in last_seen.items() if x >= lo}
        if len(active) >= min_events:
            if cur is None:
                cur = {"start": day.isoformat(), "end": day.isoformat(), "events": set(active),
                       "max_count": len(active), "count_dates": {len(active): day.isoformat()}}
                episodes.append(cur)
            else:
                cur["end"] = day.isoformat()
                cur["events"] |= active
                if len(active) > cur["max_count"]:
                    cur["max_count"] = len(active)
                    cur["count_dates"][len(active)] = day.isoformat()
        else:
            cur = None
        if day == end:
            active_today = active
        day += dt.timedelta(days=1)
    for ep in episodes:
        ep["events"] = sorted(ep["events"])
    return episodes, active_today


# ───────────────────────── ocena + powiadomienia ─────────────────────────

def evaluate(cfg: dict, state: dict, prices: dict, h100: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """Zwraca (dane dashboardu, lista alertów (klucz, data, tekst))."""
    mode = cfg["count_mode"]
    alerts = []
    events_out, tickers_out, series_out = [], [], {}
    occurrences: dict[str, list[str]] = {}
    hist_from = (today_ny() - dt.timedelta(days=cfg["dashboard_history_days"])).isoformat()
    h100_res = None

    for ev in cfg["events"]:
        eid, occ, details = ev["id"], [], []
        if ev["type"] == "drawdown":
            for t in ev["tickers"]:
                a = analyze_drawdown(prices[t], ev["threshold"], cfg["early_warning_ratio"], cfg["rearm_margin"])
                occ += a["crossings"] if mode == "crossing" else a["below_days"]
                for c in a["crossings"]:
                    alerts.append((f"ev:{eid}:{t}:{c}", c,
                                   f"🔴 <b>ZDARZENIE: {esc(ev['label'])}</b>\n{t} zamknął {c} na {money(next(p for dd_, p in prices[t] if dd_ == c))} USD "
                                   f"– spadek ≥{pct(ev['threshold'])} od ATH {money(a['ath'])} ({a['ath_date']})."))
                warn_thr = ev["threshold"] * cfg["early_warning_ratio"]
                for c in a["warn_crossings"]:
                    alerts.append((f"warn:{t}:{c}", c,
                                   f"🟡 Wczesne ostrzeżenie: {t} spadł ≥{pct(warn_thr)} od ATH (dzień {c}). "
                                   f"Próg zdarzenia: {pct(ev['threshold'])}."))
                tickers_out.append({
                    "ticker": t, "event": eid, "threshold": ev["threshold"], "last_close": a["last_close"],
                    "last_date": a["last_date"], "ath": a["ath"], "ath_date": a["ath_date"],
                    "drawdown": a["drawdown"], "below": a["below"],
                    "last_crossing": a["crossings"][-1] if a["crossings"] else None,
                })
                # seria do wykresu (ostatnie N dni): drawdown od narastającego ATH
                run_ath, dates, dds = 0.0, [], []
                for date, close in prices[t]:
                    run_ath = max(run_ath, close)
                    if date >= hist_from:
                        dates.append(date)
                        dds.append(round(-(1 - close / run_ath) * 100, 2))
                series_out[t] = {"dates": dates, "dd": dds}
                if a["below"]:
                    details.append(f"{t} obecnie {pct(a['drawdown'])} poniżej ATH")
        elif ev["type"] == "h100":
            h100_res = analyze_h100(h100, ev["max_price"], ev["days"], ev["warning_price"])
            occ = h100_res["crossings"] if mode == "crossing" else h100_res["qualifying_days"]
            for c in h100_res["crossings"]:
                alerts.append((f"ev:h100:{c}", c,
                               f"🔴 <b>ZDARZENIE: {esc(ev['label'])}</b>\nH100 (SDH100RT) ≤ {money(ev['max_price'])} USD/h "
                               f"przez {ev['days']} kolejnych dni (do {c})."))
            for c in h100_res["warn_crossings"]:
                alerts.append((f"warn:h100:{c}", c,
                               f"🟡 Wczesne ostrzeżenie: H100 spadło do {money(h100[c])} USD/h ({c}). "
                               f"Próg zdarzenia: ≤ {money(ev['max_price'])} przez {ev['days']} dni."))
            if h100_res["streak"]:
                details.append(f"{h100_res['streak']} dni z rzędu ≤ progu")
        elif ev["type"] == "manual":
            for m in state["manual_events"].get(eid, []):
                occ.append(m["date"])
                # powiadomienie o zdarzeniu ręcznym wysyła obsługa komendy
        occ = sorted(set(occ))
        occurrences[eid] = occ
        events_out.append({"id": eid, "label": ev["label"], "short": ev.get("short", eid), "type": ev["type"], "occurrences": occ,
                           "last_occurrence": occ[-1] if occ else None, "details": details})

    # data „dziś” = najnowsza data rynkowa lub dzisiejsza data NY
    asof = today_ny()
    episodes, active_now = find_combo_episodes(occurrences, cfg["window_days"], cfg["min_events"],
                                               asof, cfg.get("deadline"))
    window_lo = asof - dt.timedelta(days=cfg["window_days"] - 1)
    for e in events_out:
        in_window = [x for x in e["occurrences"] if d(x) >= window_lo]
        e["in_window"] = bool(in_window)
        e["window_dates"] = in_window
        e["window_expires"] = (d(in_window[-1]) + dt.timedelta(days=cfg["window_days"] - 1)).isoformat() if in_window else None
        e["occurrences_recent"] = [x for x in e["occurrences"] if x >= hist_from]
        e["occurrence_count"] = len(e["occurrences"])
        del e["occurrences"]
    labels = {e["id"]: e["label"] for e in events_out}
    for ep in episodes:
        for n, day in sorted(ep["count_dates"].items()):
            evs = "\n".join(f"• {esc(labels[x])}" for x in ep["events"])
            alerts.append((f"combo:{ep['start']}:{n}", day,
                           f"🚨🚨 <b>ALARM: {n} z {len(labels)} zdarzeń w oknie {cfg['window_days']} dni!</b>\n"
                           f"Epizod od {ep['start']}. Zdarzenia:\n{evs}"))

    dash = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "asof": asof.isoformat(),
        "config": {k: cfg[k] for k in ("window_days", "min_events", "deadline", "count_mode", "early_warning_ratio")},
        "combo": {
            "active_count": len(active_now), "active_events": sorted(active_now),
            "triggered_now": len(active_now) >= cfg["min_events"],
            "window_start": window_lo.isoformat(), "window_end": asof.isoformat(),
            "episodes": [{k: v for k, v in ep.items() if k != "count_dates"} for ep in episodes],
        },
        "events": events_out,
        "tickers": tickers_out,
        "series": series_out,
        "h100": {
            "dates": sorted(h100), "prices": [h100[k] for k in sorted(h100)],
            "last_price": h100_res["last_price"] if h100_res else None,
            "last_date": h100_res["last_date"] if h100_res else None,
            "streak": h100_res["streak"] if h100_res else 0,
        },
        "candidates": sorted([c for c in state["candidates"].values() if c["status"] == "pending"],
                             key=lambda c: c["date"], reverse=True),
        "manual_events": state["manual_events"],
    }
    return dash, alerts


def dispatch_alerts(cfg: dict, state: dict, alerts) -> int:
    notified = set(state["notified"])
    cutoff = today_ny() - dt.timedelta(days=cfg["notify_max_age_days"])
    sent = 0
    # najpierw pojedyncze zdarzenia, na końcu alarm łączony
    for key, date, text in sorted(alerts, key=lambda a: (a[0].startswith("combo"), a[1])):
        if key in notified:
            continue
        if not state["initialized"] or d(date) < cutoff:
            notified.add(key)  # historyczne – zapisz po cichu
            continue
        if tg_send(text):
            notified.add(key)
            sent += 1
    state["notified"] = sorted(notified)
    return sent


def status_text(dash: dict) -> str:
    c, cfg = dash["combo"], dash["config"]
    head = "🚨 ALARM AKTYWNY" if c["triggered_now"] else "📊 Status"
    lines = [f"<b>{head}</b> – {dash['asof']}",
             f"Zdarzenia w oknie {cfg['window_days']} dni: <b>{c['active_count']}/{cfg['min_events']}</b>", ""]
    for e in dash["events"]:
        mark = "🔴" if e["in_window"] else "⚪"
        extra = f" (ostatnio {e['last_occurrence']}, okno do {e['window_expires']})" if e["in_window"] else ""
        lines.append(f"{mark} {esc(e['label'])}{extra}")
    lines.append("")
    for t in dash["tickers"]:
        flag = "❗" if t["below"] else ""
        lines.append(f"{t['ticker']}: {money(t['last_close'])} | ATH {money(t['ath'])} | "
                     f"−{pct(t['drawdown'])} (próg −{pct(t['threshold'])}){flag}")
    h = dash["h100"]
    if h["last_price"] is not None:
        lines.append(f"H100: {money(h['last_price'])} USD/h ({h['last_date']}), dni ≤ progu z rzędu: {h['streak']}")
    if dash["candidates"]:
        lines.append(f"\n📰 Kandydaci z newsów do potwierdzenia: {len(dash['candidates'])} (/kandydaci)")
    return "\n".join(lines)


HELP = (
    "<b>Komendy</b>\n"
    "/status – aktualny stan wskaźników\n"
    "/kandydaci – newsy czekające na potwierdzenie\n"
    "/potwierdz ID [RRRR-MM-DD] – potwierdź newsa jako zdarzenie\n"
    "/odrzuc ID – odrzuć newsa\n"
    "/dodaj bankrupt|acquired RRRR-MM-DD [opis] – dodaj zdarzenie ręcznie\n"
    "/usun bankrupt|acquired RRRR-MM-DD – usuń zdarzenie ręczne\n"
    "/h100 CENA [RRRR-MM-DD] – ręczny odczyt H100 (gdy źródło nie działa)\n"
    "/test – testowe powiadomienie\n\n"
    f"({MANUAL_HINT}). Bot sprawdza komendy co ok. godzinę."
)


def add_manual(state, eid, date, note):
    lst = state["manual_events"].setdefault(eid, [])
    if not any(m["date"] == date for m in lst):
        lst.append({"date": date, "note": note})
        lst.sort(key=lambda m: m["date"])


def process_commands(cfg: dict, state: dict) -> bool:
    """Obsługuje komendy z Telegrama. Zwraca True, jeśli trzeba przeliczyć zdarzenia."""
    chat = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
    updates = tg_api("getUpdates", offset=state["tg_offset"], timeout=0) or []
    manual_ids = {e["id"] for e in cfg["events"] if e["type"] == "manual"}
    need_eval = False
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != chat:
            continue
        parts = (msg.get("text") or "").strip().split()
        if not parts:
            continue
        cmd, args = parts[0].split("@")[0].lower(), parts[1:]
        try:
            if cmd in ("/start", "/pomoc", "/help"):
                tg_send(HELP)
            elif cmd == "/status":
                if DASH_PATH.exists():
                    tg_send(status_text(json.loads(DASH_PATH.read_text(encoding="utf-8"))))
                else:
                    tg_send("Brak danych – pierwsza pełna ocena jeszcze się nie odbyła.")
            elif cmd == "/kandydaci":
                pend = [c for c in state["candidates"].values() if c["status"] == "pending"]
                if not pend:
                    tg_send("Brak kandydatów do potwierdzenia.")
                for c in sorted(pend, key=lambda c: c["date"])[-10:]:
                    tg_send(candidate_text(c))
            elif cmd in ("/potwierdz", "/potwierdź"):
                c = state["candidates"].get(args[0].lower())
                if not c:
                    tg_send(f"Nie znam kandydata {esc(args[0])}.")
                    continue
                date = args[1] if len(args) > 1 else c["date"]
                d(date)
                c["status"] = "confirmed"
                add_manual(state, c["event"], date, c["title"])
                need_eval = True
                tg_send(f"🔴 <b>Potwierdzono zdarzenie: {esc(label_of(cfg, c['event']))}</b> ({date}).")
            elif cmd in ("/odrzuc", "/odrzuć"):
                c = state["candidates"].get(args[0].lower())
                if c:
                    c["status"] = "rejected"
                    tg_send(f"Odrzucono {c['id']}.")
                else:
                    tg_send(f"Nie znam kandydata {esc(args[0])}.")
            elif cmd == "/dodaj":
                eid, date = args[0].lower(), args[1]
                if eid not in manual_ids:
                    tg_send(f"Nieznane zdarzenie. Dostępne: {', '.join(sorted(manual_ids))} ({MANUAL_HINT}).")
                    continue
                d(date)
                add_manual(state, eid, date, " ".join(args[2:]) or "dodane ręcznie")
                need_eval = True
                tg_send(f"🔴 <b>Dodano zdarzenie: {esc(label_of(cfg, eid))}</b> ({date}).")
            elif cmd in ("/usun", "/usuń"):
                eid, date = args[0].lower(), args[1]
                before = len(state["manual_events"].get(eid, []))
                state["manual_events"][eid] = [m for m in state["manual_events"].get(eid, []) if m["date"] != date]
                need_eval = True
                tg_send("Usunięto." if len(state["manual_events"][eid]) < before else "Nie znaleziono takiego wpisu.")
            elif cmd == "/h100":
                price = float(args[0].replace(",", "."))
                date = args[1] if len(args) > 1 else today_ny().isoformat()
                d(date)
                series = load_h100()
                series[date] = price
                save_h100(series)
                need_eval = True
                tg_send(f"Zapisano H100 = {money(price)} USD/h ({date}).")
            elif cmd == "/test":
                tg_send("✅ Test: powiadomienia działają.")
            else:
                tg_send("Nieznana komenda. /pomoc")
        except (IndexError, ValueError):
            tg_send("Błędne argumenty. /pomoc")
    return need_eval


def label_of(cfg, eid):
    return next(e["label"] for e in cfg["events"] if e["id"] == eid)


def candidate_text(c: dict) -> str:
    return (f"📰 <b>Możliwe zdarzenie</b> [{c['event']}] – {c['date']}\n{esc(c['title'])}\n{esc(c['link'])}\n\n"
            f"Potwierdź: /potwierdz {c['id']}\nOdrzuć: /odrzuc {c['id']}")


def record_failure(state: dict, source: str, err: Exception) -> None:
    n = state["failures"].get(source, 0) + 1
    state["failures"][source] = n
    print(f"[{source}] BŁĄD ({n} z rzędu): {err}")
    if n >= 2 and source not in state["failure_alerted"]:
        if tg_send(f"⚠️ Nie udało się pobrać danych <b>{source}</b> {n} razy z rzędu.\n{esc(str(err))[:500]}"):
            state["failure_alerted"].append(source)


def record_success(state: dict, source: str) -> None:
    state["failures"][source] = 0
    if source in state["failure_alerted"]:
        state["failure_alerted"].remove(source)
        tg_send(f"✅ Źródło <b>{source}</b> znowu działa.")


# ───────────────────────── tryby ─────────────────────────

def run_news(cfg, state):
    try:
        for c in fetch_news(cfg, state):
            tg_send(candidate_text(c))
        record_success(state, "news")
    except Exception as e:  # noqa: BLE001
        record_failure(state, "news", e)


def run_full(cfg: dict, state: dict) -> None:
    tickers = sorted({t for e in cfg["events"] if e["type"] == "drawdown" for t in e["tickers"]})
    h100 = load_h100()
    try:
        h100.update(fetch_h100(cfg))
        save_h100(h100)
        record_success(state, "h100")
    except Exception as e:  # noqa: BLE001
        record_failure(state, "h100", e)
    try:
        prices = fetch_prices(tickers)
        record_success(state, "prices")
    except Exception as e:  # noqa: BLE001
        record_failure(state, "prices", e)
        return  # bez cen nie da się rzetelnie ocenić – następny udany przebieg nadrobi zaległości
    dash, alerts = evaluate(cfg, state, prices, h100)
    first_run = not state["initialized"]
    sent = dispatch_alerts(cfg, state, alerts)
    DASH_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASH_PATH.write_text(json.dumps(dash, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    if first_run:
        eps = dash["combo"]["episodes"]
        hist = "\n".join(f"• {ep['start']} → {ep['end']}: {ep['max_count']} zdarzeń" for ep in eps[-8:])
        tg_send("✅ <b>Monitor uruchomiony.</b> Od teraz dostaniesz powiadomienie o każdym nowym zdarzeniu "
                f"i o alarmie {cfg['min_events']} w {cfg['window_days']} dni.\n\n" + status_text(dash)
                + (f"\n\nHistoryczne epizody (ostatnie):\n{hist}" if eps else "") + "\n\n/pomoc – lista komend")
        state["initialized"] = True
    print(f"Ocena zakończona. Wysłano powiadomień: {sent}. W oknie: {dash['combo']['active_count']}/{cfg['min_events']}")


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "daily"
    cfg = load_config()
    state = load_state()
    if mode == "chatid":
        for u in tg_api("getUpdates", timeout=0) or []:
            chat = (u.get("message") or {}).get("chat", {})
            print(f"chat_id={chat.get('id')}  ({chat.get('first_name', '')} {chat.get('username', '')})")
        return
    if mode == "test":
        print("OK" if tg_send("✅ Test: powiadomienia działają.") else "BŁĄD")
        return
    need_eval = process_commands(cfg, state)
    run_news(cfg, state)
    if mode == "daily" or need_eval or not state["initialized"]:
        run_full(cfg, state)
    save_state(state)


if __name__ == "__main__":
    main()
