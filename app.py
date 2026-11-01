# -*- coding: utf-8 -*-
"""
SP500 SIGNAL AI PRO — v7.1
===========================
Poprawki względem v6 (wynik audytu):
  * JEDEN silnik pozycji (run_engine) — wykres, archiwum, backtest i auto-trading korzystają z tych samych transakcji.
  * TP1 naprawiony: po TP1 SL jest realnie przesuwany na break-even (w symulacji i na koncie DEMO).
  * Koszty: pełny spread (wejście po ASK/BID, wyjście po BID/ASK), luki przy SL, wyjście dynamiczne na OPEN następnej świecy.
  * Silnik stanowy: każda zamknięta świeca jest przetwarzana RAZ (brak "przemalowywania" historii).
  * Archiwum w SQLite (atomowe, bezpieczne dla wielu kart) zamiast pliku JSON.
  * Auto-trading DEMO z zabezpieczeniami: tylko DEMO, tylko dane Capital.com, świeżość świecy, brak dubli,
    brak kumulowania pozycji, potwierdzenie zlecenia, SL/TP względne (stopDistance), zamykanie tylko własnej pozycji.
  * Limity ryzyka: max transakcji/dzień, dzienny limit straty, STOP awaryjny, limit czasu pozycji, filtr sesji, okna makro.
  * Backtest z kosztami (historia z Capital.com / awaryjnie Yahoo), expectancy, profit factor, max drawdown, wyniki wg godzin.
  * Samo-Audyt: reguły na próbie >= 30 transakcji (osobno dla trybu/interwału), parametry zapisywane przy każdej transakcji.
  * Klucze: zmienne środowiskowe / .streamlit/secrets.toml (bez zapisu haseł na dysk) + opcjonalne hasło do aplikacji.
  * Poprawne strefy czasowe (UTC w środku, DST), święta NYSE, wiadomości z regex i filtrem świeżości.

To narzędzie edukacyjne i NIE jest poradą inwestycyjną. Wyniki z przeszłości nie gwarantują przyszłych.
"""
import os
import re
import html
import json
import time
import hmac
import sqlite3
import inspect
import platform
import traceback
import datetime
import email.utils
import xml.etree.ElementTree as ET
from contextlib import closing

import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
import plotly.graph_objects as go
from dateutil.easter import easter

try:
    from defusedxml import ElementTree as SafeET  # bezpieczniejszy parser XML (opcjonalnie)
except Exception:  # pragma: no cover
    SafeET = ET

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

try:
    import yfinance as yf
except ImportError:  # pragma: no cover
    yf = None

# ==========================================================
# STREFY CZASOWE (bez cichego fallbacku na stałe przesunięcia!)
# ==========================================================
try:
    from zoneinfo import ZoneInfo
    PL_TZ = ZoneInfo("Europe/Warsaw")
    NY_TZ = ZoneInfo("America/New_York")
    LON_TZ = ZoneInfo("Europe/London")
    TOK_TZ = ZoneInfo("Asia/Tokyo")
    TZ_ERROR = None
except Exception as _tz_exc:  # na Windows brak pakietu tzdata
    TZ_ERROR = str(_tz_exc)
    PL_TZ = NY_TZ = LON_TZ = TOK_TZ = datetime.timezone.utc

# ==========================================================
# STAŁE
# ==========================================================
DB_FILE = "sp500_signal_v7.db"
EPIC = "US500"
INTERVAL_MIN = {"MINUTE": 1, "MINUTE_5": 5, "MINUTE_15": 15}
HTF_RULE = {"MINUTE": "5min", "MINUTE_5": "15min", "MINUTE_15": "1h"}
HTF_MIN = {"MINUTE": 5, "MINUTE_5": 15, "MINUTE_15": 60}

SENS_STRICT = "Tylko Pewniaki (Filtr Konsolidacji)"
SENS_STD = "Standard (Z filtrem płaskich EMA)"
SENS_HIGH = "Wysoka (Więcej strzałek)"
SENS_CODE = {SENS_STRICT: "STRICT", SENS_STD: "STD", SENS_HIGH: "HIGH"}
STYLE_WAVE = "🔥 FALA 30 PKT (TP1 + break-even + auto-zamykanie zysku)"
STYLE_SCALP = "⚡ Szybki Scalp (krótkie ruchy + szybki exit)"

DEFAULT_LP = {
    "min_atr_mult": 0.15,
    "min_slope_mult": 0.20,
    "rsi_long_max": 72.0,
    "rsi_short_min": 28.0,
    "dyn_trig": 7.0,
}
MIN_SAMPLE = 30  # minimalna próba zanim Samo-Audyt zmieni jakikolwiek parametr

# Okna publikacji danych makro USA (czas NY, minuty od północy)
MACRO_WINDOWS = [
    (8 * 60 + 25, 8 * 60 + 40),    # 08:30 ET dane makro
    (9 * 60 + 28, 9 * 60 + 38),    # 09:30 ET otwarcie NYSE
    (9 * 60 + 57, 10 * 60 + 8),    # 10:00 ET dane makro
    (13 * 60 + 55, 14 * 60 + 15),  # 14:00 ET FOMC
]

BEARISH_RE = re.compile(
    r"\b(hawkish|rate hikes?|hot inflation|inflation (?:surges?|accelerates?|jumps?)|sell-?offs?|plunges?|slumps?|"
    r"recession|tariffs?|tightening|yields? (?:rise|jump|surge|climb)|declines?|losses|bearish|falls?|drops?|"
    r"fears?|selloff|crash(?:es)?|downgrades?|layoffs?|war)\b", re.I)
BULLISH_RE = re.compile(
    r"\b(dovish|rate cuts?|rallies|rally|surges?|gains?|record highs?|optimism|beats?|stimulus|easing|bullish|"
    r"rebounds?|soars?|jumps?|recovery|cooling inflation|inflation (?:cools|eases|slows)|soft landing|upgrades?)\b", re.I)

IS_CLOUD = os.path.exists("/mount/src")  # Streamlit Community Cloud montuje repo tutaj
NEWS_MAX_AGE_H = 8  # wiadomości starsze niż 8 h nie liczą się do sentymentu


# ==========================================================
# POMOCNICZE
# ==========================================================
def stretch(fn):
    """Zgodność wersji Streamlit: width='stretch' (nowe) lub use_container_width=True (starsze)."""
    try:
        p = inspect.signature(fn).parameters
    except Exception:
        return {}
    w = p.get("width")
    if w is not None and isinstance(w.default, str):
        return {"width": "stretch"}
    if "use_container_width" in p:
        return {"use_container_width": True}
    return {}


def get_secret(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:
        v = st.secrets.get(name)
        if v:
            return str(v)
    except Exception:
        pass
    return default


def ts_str(ts):
    """Timestamp -> tekst UTC 'YYYY-MM-DD HH:MM:SS'."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S")


def ts_parse(s):
    t = pd.Timestamp(s)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def to_pl(s, fmt="%m-%d %H:%M"):
    if s is None or (not isinstance(s, str) and pd.isna(s)) or s in ("", "-"):
        return "-"
    try:
        return ts_parse(s).tz_convert(PL_TZ).strftime(fmt)
    except Exception:
        return str(s)


def clean_html_text(raw):
    if not raw:
        return ""
    text = html.unescape(str(raw))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ==========================================================
# BAZA DANYCH (SQLite) — archiwum + stan silnika
# ==========================================================
TRADE_COLS = [
    "id", "stream", "src", "interval", "mode", "direction", "signal_time", "entry_time", "entry", "sl0", "sl",
    "tp1", "tp2", "arrow", "pattern", "atr", "rsi", "params", "state", "status", "exit_time", "exit_price",
    "exit_reason", "pnl", "max_fav", "hit_tp1", "order_state", "deal_ref", "deal_id", "fill_price", "order_msg",
    "created_at",
]


def _conn():
    c = sqlite3.connect(DB_FILE, timeout=20)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return c


def init_db():
    with closing(_conn()) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS trades ("
            "id TEXT PRIMARY KEY, stream TEXT, src TEXT, interval TEXT, mode TEXT, direction INTEGER, "
            "signal_time TEXT, entry_time TEXT, entry REAL, sl0 REAL, sl REAL, tp1 REAL, tp2 REAL, arrow REAL, "
            "pattern TEXT, atr REAL, rsi REAL, params TEXT, state TEXT, status TEXT, exit_time TEXT, "
            "exit_price REAL, exit_reason TEXT, pnl REAL, max_fav REAL, hit_tp1 INTEGER, order_state TEXT, "
            "deal_ref TEXT, deal_id TEXT, fill_price REAL, order_msg TEXT, created_at TEXT)"
        )
        c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
        c.commit()


def db_query(sql, params=()):
    with closing(_conn()) as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def db_run(sql, params=()):
    with closing(_conn()) as c:
        cur = c.execute(sql, params)
        c.commit()
        return cur.rowcount


def db_set(tid, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    return db_run(f"UPDATE trades SET {cols} WHERE id=?", (*kw.values(), tid))


def db_get_trade(tid):
    rows = db_query("SELECT * FROM trades WHERE id=?", (tid,))
    return rows[0] if rows else None


def load_trades(where="1=1", params=()):
    rows = db_query(f"SELECT * FROM trades WHERE {where} ORDER BY entry_time", params)
    return pd.DataFrame(rows, columns=TRADE_COLS) if rows else pd.DataFrame(columns=TRADE_COLS)


def load_closed(stream=None):
    if stream:
        df = load_trades("state='CLOSED' AND stream=?", (stream,))
    else:
        df = load_trades("state='CLOSED'")
    if df.empty:
        return df
    df = df[df["exit_reason"] != "NO_DATA"].copy()
    df["pnl"] = df["pnl"].astype(float)
    return df.sort_values("exit_time").reset_index(drop=True)


def export_db_json():
    return json.dumps({"version": 7, "trades": db_query("SELECT * FROM trades ORDER BY entry_time"),
                       "kv": db_query("SELECT * FROM kv")}, ensure_ascii=False, indent=1, default=str)


def import_db_json(text):
    """Wczytuje kopię zapasową (INSERT OR IGNORE — istniejących rekordów nie nadpisuje). Zwraca liczbę dodanych transakcji."""
    data = json.loads(text)
    added = 0
    with closing(_conn()) as c:
        for r in data.get("trades", []):
            if not isinstance(r, dict) or not r.get("id"):
                continue
            cur = c.execute(f"INSERT OR IGNORE INTO trades ({','.join(TRADE_COLS)}) VALUES ({','.join('?' * len(TRADE_COLS))})",
                            [r.get(k) for k in TRADE_COLS])
            added += cur.rowcount
        for r in data.get("kv", []):
            if isinstance(r, dict) and r.get("key"):
                c.execute("INSERT OR IGNORE INTO kv (key, value) VALUES (?,?)", (r["key"], r.get("value")))
        c.commit()
    return added


# ==========================================================
# STATYSTYKI I SAMO-AUDYT
# ==========================================================
def compute_stats(df):
    empty = {"n": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
             "expectancy": 0.0, "profit_factor": 0.0, "max_dd": 0.0}
    if df is None or df.empty:
        return empty
    pnl = df["pnl"].astype(float)
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_w, gross_l = float(wins.sum()), float(-losses.sum())
    if gross_l > 0:
        pf = gross_w / gross_l
    else:
        pf = float("inf") if gross_w > 0 else 0.0
    eq = pnl.cumsum()
    max_dd = float((eq.cummax() - eq).max()) if len(eq) else 0.0
    return {
        "n": int(len(pnl)), "wins": int(len(wins)), "losses": int(len(losses)),
        "win_rate": round(len(wins) / len(pnl) * 100, 1), "total": round(float(pnl.sum()), 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "expectancy": round(float(pnl.mean()), 2), "profit_factor": pf, "max_dd": round(max_dd, 2),
    }


def fmt_pf(pf):
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def run_self_learning_audit(closed_df):
    """Proste reguły na próbie >= MIN_SAMPLE (to NIE jest uczenie maszynowe). Parametry trafiają tylko do NOWYCH transakcji."""
    lp = dict(DEFAULT_LP)
    stats = compute_stats(closed_df)
    n = stats["n"]
    if n < MIN_SAMPLE:
        return {"lp": lp, "stats": stats, "n": n, "grade": f"⏳ ZBIERANIE DANYCH ({n}/{MIN_SAMPLE})",
                "lessons": [f"🔍 Zebrano {n}/{MIN_SAMPLE} zamkniętych pozycji w tym trybie. Do tego czasu działają "
                            f"bazowe progi — przy tak małej próbie każda 'nauka' byłaby szumem."]}
    lessons = []
    last = closed_df.tail(MIN_SAMPLE)
    exp_last = float(last["pnl"].mean())
    if exp_last < -2.0:
        lp["min_atr_mult"], lp["min_slope_mult"] = 0.30, 0.36
        lessons.append(f"🛡️ Oczekiwana wartość z ostatnich {MIN_SAMPLE} transakcji: {exp_last:+.2f} pkt → "
                       f"filtr konsolidacji zaostrzony mocno (krok 2).")
    elif exp_last < 0:
        lp["min_atr_mult"], lp["min_slope_mult"] = 0.22, 0.28
        lessons.append(f"🛡️ Oczekiwana wartość z ostatnich {MIN_SAMPLE} transakcji: {exp_last:+.2f} pkt → "
                       f"filtr konsolidacji zaostrzony (krok 1).")
    else:
        lessons.append(f"✅ Oczekiwana wartość z ostatnich {MIN_SAMPLE} transakcji: {exp_last:+.2f} pkt → bazowe progi.")

    longs, shorts = closed_df[closed_df["direction"] == 1], closed_df[closed_df["direction"] == -1]
    if len(longs) >= 15 and longs["pnl"].mean() < 0:
        lp["rsi_long_max"] = 65.0
        lessons.append(f"📉 LONG ({len(longs)} transakcji, śr. {longs['pnl'].mean():+.2f} pkt) → max RSI dla LONG 72 → 65.")
    if len(shorts) >= 15 and shorts["pnl"].mean() < 0:
        lp["rsi_short_min"] = 35.0
        lessons.append(f"📉 SHORT ({len(shorts)} transakcji, śr. {shorts['pnl'].mean():+.2f} pkt) → min RSI dla SHORT 28 → 35.")

    gave_back = last[(last["pnl"] <= 0) & (last["max_fav"].astype(float) >= 6.0)]
    if len(gave_back) >= 0.3 * MIN_SAMPLE:
        lp["dyn_trig"] = 5.5
        lessons.append(f"💰 {len(gave_back)} z ostatnich {MIN_SAMPLE} strat było wcześniej na plusie ≥6 pkt → próg "
                       f"zamykania zysku obniżony do +5.5 pkt.")
    grade = "🟢 DODATNIA OCZEKIWANA WARTOŚĆ" if stats["expectancy"] > 0 else "🔧 UJEMNA OCZEKIWANA WARTOŚĆ"
    return {"lp": lp, "stats": stats, "n": n, "grade": f"{grade} ({stats['expectancy']:+.2f} pkt/transakcję)",
            "lessons": lessons}


# ==========================================================
# WSKAŹNIKI + ŚWIECE WYŻSZEGO INTERWAŁU (bez look-ahead)
# ==========================================================
def indicators(df_in):
    df = df_in.copy().sort_index()
    df = df[~df.index.duplicated(keep="last")]
    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()
    delta = df["Close"].diff()
    avg_g = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    avg_l = (-delta).clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    rs = avg_g / avg_l.where(avg_l != 0)
    rsi = 100 - 100 / (1 + rs)
    df["RSI"] = rsi.where(avg_l != 0, np.where(avg_g > 0, 100.0, 50.0)).fillna(50.0)
    prev_c = df["Close"].shift()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - prev_c).abs(), (df["Low"] - prev_c).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df


def compute_htf_bias(df, interval):
    """Trend wyższego interwału z DOMKNIĘTYCH świec (dostępnych w chwili zamknięcia świecy bazowej)."""
    htf = df["Close"].resample(HTF_RULE[interval], label="left", closed="left").last().dropna()
    e9, e21 = htf.ewm(span=9, adjust=False).mean(), htf.ewm(span=21, adjust=False).mean()
    bias = np.where(e9 > e21, 1, np.where(e9 < e21, -1, 0))
    right = pd.DataFrame({"avail": htf.index + pd.Timedelta(minutes=HTF_MIN[interval]), "bias": bias})
    left = pd.DataFrame({"close_time": df.index + pd.Timedelta(minutes=INTERVAL_MIN[interval])})
    merged = pd.merge_asof(left, right.sort_values("avail"), left_on="close_time", right_on="avail", direction="backward")
    return merged["bias"].fillna(0).astype(int).to_numpy()


def build_bars(df_main, interval):
    df = indicators(df_main)
    n = len(df)
    idx = df.index
    htf = compute_htf_bias(df, interval) if n > 30 else np.zeros(n, dtype=int)
    ny, pl = idx.tz_convert(NY_TZ), idx.tz_convert(PL_TZ)
    return {
        "df": df, "idx": idx, "n": n, "interval": interval,
        "o": df["Open"].to_numpy(float), "h": df["High"].to_numpy(float),
        "l": df["Low"].to_numpy(float), "c": df["Close"].to_numpy(float),
        "ema9": df["EMA9"].to_numpy(float), "ema21": df["EMA21"].to_numpy(float),
        "ema50": df["EMA50"].to_numpy(float), "rsi": df["RSI"].to_numpy(float),
        "atr": df["ATR"].to_numpy(float), "htf": htf,
        "ny_min": np.asarray(ny.hour * 60 + ny.minute), "ny_wd": np.asarray(ny.weekday),
        "pl_min": np.asarray(pl.hour * 60 + pl.minute), "pl_day": np.asarray(pl.strftime("%Y-%m-%d")),
    }


def in_macro(ny_min, ny_wd):
    if ny_wd >= 5:
        return False
    return any(a <= ny_min <= b for a, b in MACRO_WINDOWS)


# ==========================================================
# SILNIK POZYCJI — JEDNO ŹRÓDŁO PRAWDY (live, archiwum, backtest)
# ==========================================================
# Model wykonania (spójny dla wejść i wyjść):
#  * sygnał powstaje na ZAMKNIĘTEJ świecy j, wejście na OPEN świecy j+1 (LONG po ASK, SHORT po BID),
#  * pozycję LONG liczymy po BID (mid - spread/2), SHORT po ASK (mid + spread/2) -> pełny spread w wyniku,
#  * w jednej świecy SL ma pierwszeństwo przed TP (konserwatywnie), luka przez SL = wyjście po OPEN,
#  * po TP1 SL przechodzi na break-even (od NASTĘPNEJ świecy),
#  * wyjście dynamiczne / limit czasu: decyzja na zamknięciu świecy j, wykonanie na OPEN świecy j+1.
def new_state(stream, src="CAP", pos=None, last_exit=None, day_n=None, day_pnl=None):
    return {"stream": stream, "src": src, "pos": pos, "last_exit_time": last_exit,
            "day_n": day_n if day_n is not None else {}, "day_pnl": day_pnl if day_pnl is not None else {}}


def exit_status_text(reason, pnl):
    if reason == "SL":
        return f"🛑 STOP LOSS ({pnl:+.1f} pkt)"
    if reason == "BE":
        return f"🟡 BREAK-EVEN po TP1 ({pnl:+.1f} pkt)"
    if reason == "TP2":
        return f"🎯 PEŁNE TP2 ({pnl:+.1f} pkt)"
    if reason == "TIME":
        return f"⏱️ LIMIT CZASU ({pnl:+.1f} pkt)"
    return f"💰 ZAMKNIĘCIE ZYSKU ({pnl:+.1f} pkt)" if pnl > 0 else f"↩️ WYJŚCIE DYNAMICZNE ({pnl:+.1f} pkt)"


def run_engine(B, j_from, j_to, state, cfg, lp):
    """Przetwarza zamknięte świece j_from..j_to. Zwraca listę zdarzeń: OPEN / TP1 / EXIT."""
    events = []
    n, idx = B["n"], B["idx"]
    O, H, L, C = B["o"], B["h"], B["l"], B["c"]
    E9, E21, E50, RSI, ATR, HTF = B["ema9"], B["ema21"], B["ema50"], B["rsi"], B["atr"], B["htf"]
    hs = cfg["spread"] / 2.0
    ivl = pd.Timedelta(minutes=INTERVAL_MIN[B["interval"]])
    wave30, sens = cfg["wave30"], cfg["sensitivity"]
    wick_ratio = 1.4 if sens == SENS_HIGH else 1.7
    dyn_trig = float(lp["dyn_trig"]) if wave30 else 3.5
    be_off = max(1.0, cfg["spread"] + 0.3)
    max_hold = pd.Timedelta(hours=cfg["max_hold_hours"])
    params_json = json.dumps({"lp": lp, "wave30": wave30, "sens": SENS_CODE.get(sens, sens), "spread": cfg["spread"]})

    for j in range(max(j_from, 55), min(j_to, n - 2) + 1):
        o, h, l, c = O[j], H[j], L[j], C[j]
        pos = state["pos"]

        # ---------- A) zarządzanie otwartą pozycją ----------
        if pos is not None:
            d, entry = pos["dir"], pos["entry"]

            def ex(v, _d=d):  # cena po stronie wyjścia: LONG -> BID, SHORT -> ASK
                return v - hs if _d == 1 else v + hs

            op = d * (ex(o) - entry)
            fav = d * (ex(h if d == 1 else l) - entry)
            adv = d * (ex(l if d == 1 else h) - entry)
            cl = d * (ex(c) - entry)
            sl_p = d * (pos["sl"] - entry)
            tp1_p = d * (pos["tp1"] - entry)
            tp2_p = d * (pos["tp2"] - entry)
            exit_p, reason, exit_t = None, None, idx[j]

            if op <= sl_p:                       # luka przez SL -> wyjście po OPEN
                exit_p, reason = op, ("BE" if sl_p >= 0 else "SL")
            elif adv <= sl_p:                    # SL zawsze przed TP w tej samej świecy
                exit_p, reason = sl_p, ("BE" if sl_p >= 0 else "SL")
            elif fav >= tp2_p:
                exit_p, reason = tp2_p, "TP2"
            else:
                if fav > pos["max_fav"]:
                    pos["max_fav"] = fav
                if fav >= tp1_p and not pos["hit_tp1"]:
                    pos["hit_tp1"] = True
                    pos["sl"] = round(entry + d * be_off, 2)   # break-even od następnej świecy
                    events.append({"type": "TP1", "id": pos["id"], "new_sl": pos["sl"]})
                nxt_exit = d * (ex(O[j + 1]) - entry)
                reversal = (d * (c - E9[j]) < 0 and d * (c - o) < 0) or \
                           (pos["max_fav"] >= 14.0 and cl <= pos["max_fav"] * 0.68)
                if pos["max_fav"] >= dyn_trig and cl >= 3.5 and reversal:
                    exit_p, reason, exit_t = nxt_exit, "DYN", idx[j + 1]
                elif (idx[j] + ivl) - pos["entry_time"] >= max_hold:
                    exit_p, reason, exit_t = nxt_exit, "TIME", idx[j + 1]

            if exit_p is not None:
                pnl = round(float(exit_p), 2)
                events.append({
                    "type": "EXIT", "id": pos["id"], "exit_time": exit_t,
                    "exit_price": round(entry + d * exit_p, 2), "pnl": pnl, "reason": reason,
                    "status": exit_status_text(reason, pnl), "max_fav": round(float(pos["max_fav"]), 2),
                    "hit_tp1": bool(pos["hit_tp1"]),
                })
                dk = B["pl_day"][j]
                state["day_pnl"][dk] = state["day_pnl"].get(dk, 0.0) + pnl
                state["pos"], state["last_exit_time"] = None, exit_t
            continue

        # ---------- B) szukanie nowego wejścia ----------
        if j < cfg["entry_j_min"] or j + 1 >= n:
            continue
        let = state["last_exit_time"]
        if let is not None and idx[j] < let + cfg.get("cooldown_bars", 2) * ivl:
            continue
        if cfg["halt"]:
            continue
        dk = B["pl_day"][j + 1]
        if state["day_n"].get(dk, 0) >= cfg["max_trades_day"]:
            continue
        if state["day_pnl"].get(dk, 0.0) <= -cfg["daily_loss_limit"]:
            continue
        if cfg["session_on"] and not (cfg["session_start"] <= B["pl_min"][j + 1] < cfg["session_end"]):
            continue
        if cfg["macro_block"] and in_macro(B["ny_min"][j + 1], B["ny_wd"][j + 1]):
            continue

        po, pc = O[j - 1], C[j - 1]
        ema9, ema21, ema50, rsi = E9[j], E21[j], E50[j], RSI[j]
        atr = max(float(ATR[j]), 0.5)
        body, rng = abs(c - o), max(h - l, 0.01)
        upper, lower = h - max(o, c), min(o, c) - l
        slope9, slope21 = ema9 - E9[j - 3], ema21 - E21[j - 3]
        is_chop = (abs(ema9 - ema21) < atr * lp["min_atr_mult"]) and (abs(slope9) < atr * lp["min_slope_mult"])
        if sens != SENS_HIGH and is_chop:
            continue
        near_high, near_low = ((c - l) / rng) >= 0.55, ((h - c) / rng) >= 0.55

        bull = ((lower >= body * wick_ratio and lower >= rng * 0.40 and near_high and c > o) or
                (pc < po and c > o and c > po and body >= atr * 0.35) or
                (l <= ema21 and c > ema9 and c > o and near_high) or
                (c > o and c > ema9 and ema9 > ema21 and E9[j - 2] <= E21[j - 2]))
        bear = ((upper >= body * wick_ratio and upper >= rng * 0.40 and near_low and c < o) or
                (pc > po and c < o and c < po and body >= atr * 0.35) or
                (h >= ema21 and c < ema9 and c < o and near_low) or
                (c < o and c < ema9 and ema9 < ema21 and E9[j - 2] >= E21[j - 2]))
        trend_up = ema9 > ema21 and c > ema50 and slope9 > 0 and slope21 >= 0
        trend_dn = ema9 < ema21 and c < ema50 and slope9 < 0 and slope21 <= 0
        if sens == SENS_STRICT and HTF[j] != 0:
            trend_up, trend_dn = trend_up and HTF[j] == 1, trend_dn and HTF[j] == -1

        if bull and trend_up and 42 <= rsi <= lp["rsi_long_max"]:
            d = 1
        elif bear and trend_dn and lp["rsi_short_min"] <= rsi <= 58:
            d = -1
        else:
            continue
        if cfg.get("news_veto") and cfg.get("news_bias", 0) == -d:
            continue

        entry = round(O[j + 1] + d * hs, 2)           # LONG po ASK, SHORT po BID
        adv_ext = (entry - l) if d == 1 else (h - entry)
        if wave30:
            swing = (entry - L[max(0, j - 5):j + 1].min()) if d == 1 else (H[max(0, j - 5):j + 1].max() - entry)
            sl_dist = float(np.clip(max(swing + atr, atr * 3.5), 8.0, 14.0))
            tp1_dist, tp2_dist = 15.0, 30.0
            pattern = "🔥 OTWARCIE LONG (Fala 30 pkt)" if d == 1 else "🔥 OTWARCIE SHORT (Zjazd 30 pkt)"
        else:
            sl_dist = max(adv_ext, atr) + atr * 0.35
            tp1_dist, tp2_dist = atr * 1.5, atr * 3.0
            pattern = "⚡ OTWARCIE LONG (Scalp)" if d == 1 else "⚡ OTWARCIE SHORT (Scalp)"
        sl_v = round(entry - d * sl_dist, 2)
        tp1_v, tp2_v = round(entry + d * tp1_dist, 2), round(entry + d * tp2_dist, 2)
        tid = f"{state['stream']}|{ts_str(idx[j])}"
        trade = {
            "id": tid, "stream": state["stream"], "src": state["src"], "interval": B["interval"],
            "mode": cfg["mode_label"], "direction": d, "signal_time": ts_str(idx[j]), "entry_time": ts_str(idx[j + 1]),
            "entry": entry, "sl0": sl_v, "sl": sl_v, "tp1": tp1_v, "tp2": tp2_v,
            "arrow": round(float(l - atr * 0.4 if d == 1 else h + atr * 0.4), 2), "pattern": pattern,
            "atr": round(atr, 2), "rsi": round(float(rsi), 1), "params": params_json, "state": "OPEN",
            "status": "⏳ W GRZE", "max_fav": 0.0, "hit_tp1": 0, "order_state": "NONE",
        }
        state["pos"] = {"id": tid, "dir": d, "entry": entry, "sl": sl_v, "tp1": tp1_v, "tp2": tp2_v,
                        "max_fav": 0.0, "hit_tp1": False, "entry_time": idx[j + 1]}
        state["day_n"][dk] = state["day_n"].get(dk, 0) + 1
        events.append({"type": "OPEN", "trade": trade})
    return events


# ==========================================================
# KLIENT CAPITAL.COM
# ==========================================================
class CapitalClient:
    def __init__(self, api_key, identifier, password, is_demo=True):
        self.base_url = "https://demo-api-capital.backend-capital.com" if is_demo else "https://api-capital.backend-capital.com"
        self.api_key, self.identifier, self.password, self.is_demo = api_key, identifier, password, is_demo

    def session(self, force=False):
        if not (self.api_key and self.identifier and self.password):
            return None, None, "Brak kluczy Capital.com"
        cache = st.session_state.get("cap_session_cache", {})
        now = time.time()
        if (not force and cache.get("cst") and cache.get("base_url") == self.base_url and now - cache.get("ts", 0) < 480):
            return cache["cst"], cache["token"], "sesja z cache"
        try:
            r = requests.post(f"{self.base_url}/api/v1/session",
                              json={"identifier": self.identifier, "password": self.password, "encryptedPassword": False},
                              headers={"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"}, timeout=8)
            if r.status_code == 200:
                cst, token = r.headers.get("CST"), r.headers.get("X-SECURITY-TOKEN")
                st.session_state["cap_session_cache"] = {"cst": cst, "token": token, "base_url": self.base_url, "ts": now}
                return cst, token, "zalogowano"
            return None, None, f"Błąd logowania Capital.com ({r.status_code})"
        except Exception as e:
            return None, None, f"Błąd sieci Capital.com: {type(e).__name__}"

    def _request(self, method, path, params=None, body=None, timeout=8):
        for attempt in range(2):
            cst, token, msg = self.session(force=(attempt == 1))
            if not cst:
                return None, msg
            headers = {"X-CAP-API-KEY": self.api_key, "CST": cst, "X-SECURITY-TOKEN": token}
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                r = requests.request(method, self.base_url + path, headers=headers, params=params, json=body, timeout=timeout)
            except Exception as e:
                return None, f"Błąd sieci: {type(e).__name__}"
            if r.status_code == 401 and attempt == 0:
                continue
            return r, ""
        return None, "Błąd autoryzacji (401)"

    def fetch_candles(self, epic=EPIC, resolution="MINUTE", max_bars=500, start=None, end=None):
        params = {"resolution": resolution, "max": max_bars}
        if start is not None:
            params["from"] = start.strftime("%Y-%m-%dT%H:%M:%S")
        if end is not None:
            params["to"] = end.strftime("%Y-%m-%dT%H:%M:%S")
        r, err = self._request("GET", f"/api/v1/prices/{epic}", params=params)
        if r is None:
            return pd.DataFrame(), None, err
        if r.status_code != 200:
            return pd.DataFrame(), None, f"Błąd pobierania świec ({r.status_code})"
        rows, spread = [], None
        for p in r.json().get("prices", []):
            try:
                cb, ca = p["closePrice"]["bid"], p["closePrice"]["ask"]
                spread = round(ca - cb, 2)
                rows.append({
                    "Time": pd.to_datetime(p["snapshotTimeUTC"], utc=True),
                    "Open": (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2.0,
                    "High": (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2.0,
                    "Low": (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2.0,
                    "Close": (cb + ca) / 2.0, "Volume": p.get("lastTradedVolume", 0),
                })
            except (KeyError, TypeError):
                continue
        if not rows:
            return pd.DataFrame(), None, "Brak świec z Capital.com"
        df = pd.DataFrame(rows).set_index("Time").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return df, spread, "🟢 NA ŻYWO: Capital.com API"

    def fetch_history(self, epic, resolution, days):
        end = datetime.datetime.now(datetime.timezone.utc)
        t = end - datetime.timedelta(days=days)
        chunk = datetime.timedelta(minutes=960 * INTERVAL_MIN[resolution])  # <1000 świec na zapytanie
        frames = []
        while t < end:
            t2 = min(t + chunk, end)
            df, _, _ = self.fetch_candles(epic, resolution, 1000, t, t2)
            if not df.empty:
                frames.append(df)
            t = t2
            time.sleep(0.2)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames).sort_index()
        return out[~out.index.duplicated(keep="last")]

    def get_open_positions(self, epic=EPIC):
        """(lista, błąd). Lista = None, gdy nie udało się sprawdzić (wtedy NIE wolno handlować)."""
        r, err = self._request("GET", "/api/v1/positions", timeout=6)
        if r is None or r.status_code != 200:
            return None, err or f"HTTP {getattr(r, 'status_code', '?')}"
        return [p for p in r.json().get("positions", []) if p.get("market", {}).get("epic") == epic], ""

    def open_position(self, direction, size, stop_dist, profit_dist, epic=EPIC):
        body = {"epic": epic, "direction": direction, "size": float(size), "guaranteedStop": False,
                "stopDistance": round(float(stop_dist), 2), "profitDistance": round(float(profit_dist), 2)}
        r, err = self._request("POST", "/api/v1/positions", body=body)
        if r is None:
            return False, None, f"❌ {err}"
        if r.status_code == 200:
            return True, r.json().get("dealReference"), "zlecenie wysłane"
        return False, None, f"❌ Błąd otwarcia ({r.status_code}): {r.text[:120]}"

    def confirm_deal(self, ref):
        r, _ = self._request("GET", f"/api/v1/confirms/{ref}", timeout=6)
        if r is not None and r.status_code == 200:
            return r.json()
        return None

    def update_position(self, deal_id, stop_level, profit_level):
        r, err = self._request("PUT", f"/api/v1/positions/{deal_id}",
                               body={"stopLevel": float(stop_level), "profitLevel": float(profit_level)})
        if r is not None and r.status_code == 200:
            return True, "SL przesunięty"
        return False, f"❌ Nie udało się przesunąć SL ({err or getattr(r, 'status_code', '?')})"

    def close_position(self, deal_id):
        r, err = self._request("DELETE", f"/api/v1/positions/{deal_id}")
        if r is not None and r.status_code == 200:
            return True, "pozycja zamknięta"
        return False, f"❌ Nie udało się zamknąć ({err or getattr(r, 'status_code', '?')})"


@st.cache_data(ttl=30, show_spinner=False)
def fetch_fallback_data(resolution="MINUTE", period=None):
    if yf is None:
        return pd.DataFrame(), 0.6, "❌ Brak biblioteki yfinance"
    interval = {"MINUTE": "1m", "MINUTE_5": "5m", "MINUTE_15": "15m"}.get(resolution, "5m")
    period = period or ("2d" if interval == "1m" else "5d")
    try:
        df = yf.download("^GSPC", period=period, interval=interval, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        idx = pd.to_datetime(df.index)
        idx = idx.tz_localize(NY_TZ) if idx.tz is None else idx
        df.index = idx.tz_convert("UTC")
        return df, 0.6, "🟡 TRYB AWARYJNY: Yahoo Finance (indeks ^GSPC, nie CFD) — auto-trading zablokowany"
    except Exception as e:
        return pd.DataFrame(), 0.6, f"Błąd fallbacku: {e}"


@st.cache_data(ttl=1800, show_spinner=False)
def cached_history(_client, acct, resolution, days, bucket):
    return _client.fetch_history(EPIC, resolution, days)


# ==========================================================
# OBSŁUGA ZLECEŃ (auto-trading DEMO) — z zabezpieczeniami
# ==========================================================
def broker_open(client, tr, size):
    tid = tr["id"]
    # atomowe "zajęcie" zlecenia: chroni przed dublem (F5, druga karta)
    if db_run("UPDATE trades SET order_state='SENDING' WHERE id=? AND order_state='NONE'", (tid,)) != 1:
        return "ℹ️ Zlecenie dla tego sygnału zostało już obsłużone."
    positions, err = client.get_open_positions(EPIC)
    if positions is None:
        db_set(tid, order_state="SKIPPED", order_msg=f"nie sprawdzono pozycji: {err}")
        return f"⚠️ Zlecenie pominięte — nie mogę sprawdzić pozycji na koncie ({err})."
    if positions:
        db_set(tid, order_state="SKIPPED", order_msg="na koncie jest już pozycja US500")
        return "⚠️ Zlecenie pominięte — na koncie DEMO jest już otwarta pozycja US500."
    direction = "BUY" if tr["direction"] == 1 else "SELL"
    ok, ref, msg = client.open_position(direction, size, abs(tr["entry"] - tr["sl0"]), abs(tr["tp2"] - tr["entry"]))
    if not ok:
        db_set(tid, order_state="REJECTED", order_msg=msg[:200])
        return msg
    db_set(tid, order_state="PENDING", deal_ref=ref)
    conf = None
    for _ in range(4):
        conf = client.confirm_deal(ref)
        if conf:
            break
        time.sleep(0.4)
    if not conf:
        db_set(tid, order_state="UNCONFIRMED", order_msg="brak potwierdzenia — sprawdź konto ręcznie")
        return "⚠️ Brak potwierdzenia zlecenia — sprawdź konto DEMO ręcznie."
    if str(conf.get("dealStatus", "")).upper() == "ACCEPTED":
        deal_id = conf.get("dealId") or next((a.get("dealId") for a in conf.get("affectedDeals", []) if a.get("dealId")), None)
        db_set(tid, order_state="ACCEPTED", deal_id=deal_id, fill_price=conf.get("level"), order_msg="OK")
        return f"✅ DEMO: otwarto {direction} {size} (fill {conf.get('level')}, ref {ref})"
    reason = str(conf.get("rejectReason") or conf.get("dealStatus") or "odrzucone")
    db_set(tid, order_state="REJECTED", order_msg=reason[:200])
    return f"❌ DEMO: zlecenie odrzucone ({reason})"


def broker_move_stop(client, tid, new_sl):
    tr = db_get_trade(tid)
    if not tr or tr["order_state"] != "ACCEPTED" or not tr["deal_id"]:
        return None
    shift = (float(tr["fill_price"]) - float(tr["entry"])) if tr["fill_price"] else 0.0
    ok, msg = client.update_position(tr["deal_id"], round(new_sl + shift, 2), round(tr["tp2"] + shift, 2))
    return ("🟡 DEMO: SL przesunięty na break-even" if ok else msg)


def broker_close(client, tid):
    tr = db_get_trade(tid)
    if not tr or tr["order_state"] != "ACCEPTED" or not tr["deal_id"]:
        return None
    positions, err = client.get_open_positions(EPIC)
    if positions is None:
        return f"⚠️ Nie sprawdzono konta ({err}) — upewnij się ręcznie, że pozycja jest zamknięta."
    ids = {p.get("position", {}).get("dealId") for p in positions}
    if tr["deal_id"] in ids:
        ok, msg = client.close_position(tr["deal_id"])
        db_set(tid, order_state="CLOSED" if ok else "CLOSE_FAILED", order_msg=msg)
        return f"💰 DEMO: {msg}" if ok else msg
    db_set(tid, order_state="CLOSED_BROKER", order_msg="zamknięta po stronie brokera (SL/TP/ręcznie)")
    return "ℹ️ DEMO: pozycja była już zamknięta po stronie brokera."


def send_telegram(token, chat_id, text):
    if not token or not chat_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception:
        pass


def render_hidden_html(code):
    """st.components.v1.html jest wycofywany -> najpierw st.iframe (nowe wersje), potem stara metoda."""
    if hasattr(st, "iframe"):
        try:
            st.iframe(code, width=1, height=1)
            return
        except Exception:
            pass
    components.html(code, height=0, width=0)


def trigger_beep_alarm(kind="LONG"):
    f1, f2, f3 = {"EXIT": (1046, 1318, 1568), "LONG": (880, 1175, 880)}.get(kind, (520, 390, 520))
    render_hidden_html(f"""
    <script>(function(){{try{{const C=window.AudioContext||window.webkitAudioContext;const ctx=new C();
    if(ctx.resume){{ctx.resume();}}
    const t=(f,s,d)=>{{const o=ctx.createOscillator(),g=ctx.createGain();o.type='triangle';
    o.frequency.setValueAtTime(f,ctx.currentTime+s);g.gain.setValueAtTime(0.28,ctx.currentTime+s);
    g.gain.exponentialRampToValueAtTime(0.001,ctx.currentTime+s+d);o.connect(g);g.connect(ctx.destination);
    o.start(ctx.currentTime+s);o.stop(ctx.currentTime+s+d);}};
    t({f1},0,0.16);t({f2},0.19,0.16);t({f3},0.38,0.32);}}catch(e){{}}}})();</script>""")


# ==========================================================
# PRZETWARZANIE NA ŻYWO (każda zamknięta świeca dokładnie raz)
# ==========================================================
def process_live(B, stream, src, cfg, lp):
    idx, n = B["idx"], B["n"]
    jl = n - 2                                    # ostatnia ZAMKNIĘTA świeca
    events = []
    c = _conn()
    c.isolation_level = None
    try:
        c.execute("BEGIN IMMEDIATE")              # serializuje wiele kart/sesji
        row = c.execute("SELECT * FROM trades WHERE stream=? AND state='OPEN' ORDER BY entry_time DESC LIMIT 1",
                        (stream,)).fetchone()
        pos = None
        if row is not None:
            pos = {"id": row["id"], "dir": row["direction"], "entry": row["entry"], "sl": row["sl"], "tp1": row["tp1"],
                   "tp2": row["tp2"], "max_fav": row["max_fav"] or 0.0, "hit_tp1": bool(row["hit_tp1"]),
                   "entry_time": ts_parse(row["entry_time"])}
        kv = {r["key"]: r["value"] for r in c.execute("SELECT * FROM kv WHERE key IN (?,?)",
                                                      (f"last_proc:{stream}", f"last_exit:{stream}")).fetchall()}
        last_exit = ts_parse(kv[f"last_exit:{stream}"]) if kv.get(f"last_exit:{stream}") else None
        # liczniki dzienne (limit ryzyka) z całej bazy dla tego źródła danych
        cutoff = ts_str(pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=2))
        day_n, day_pnl = {}, {}
        for r in c.execute("SELECT entry_time, exit_time, pnl, state FROM trades WHERE src=? AND "
                           "(entry_time>=? OR exit_time>=?)", (src, cutoff, cutoff)).fetchall():
            if r["entry_time"]:
                k = ts_parse(r["entry_time"]).tz_convert(PL_TZ).strftime("%Y-%m-%d")
                day_n[k] = day_n.get(k, 0) + 1
            if r["state"] == "CLOSED" and r["exit_time"] and r["pnl"] is not None:
                k = ts_parse(r["exit_time"]).tz_convert(PL_TZ).strftime("%Y-%m-%d")
                day_pnl[k] = day_pnl.get(k, 0.0) + float(r["pnl"])
        state = new_state(stream, src, pos, last_exit, day_n, day_pnl)

        j_from, found = jl, False
        if kv.get(f"last_proc:{stream}"):
            lp_ts = ts_parse(kv[f"last_proc:{stream}"])
            k = int(idx.searchsorted(lp_ts))
            if k < n and idx[k] == lp_ts:
                j_from, found = k + 1, True
        if pos is not None:
            if pos["entry_time"] < idx[0]:        # brak danych do rozliczenia -> nie zgadujemy
                c.execute("UPDATE trades SET state='CLOSED', exit_reason='NO_DATA', status='❓ NIEROZLICZONA (brak danych)', "
                          "pnl=0 WHERE id=?", (pos["id"],))
                state["pos"] = None
            else:
                ej = int(idx.searchsorted(pos["entry_time"]))
                j_from = max(j_from, ej) if found else ej

        events = run_engine(B, j_from, jl, state, cfg, lp)

        applied = []
        for ev in events:
            if ev["type"] == "OPEN":
                t = {k: ev["trade"].get(k) for k in TRADE_COLS}
                t["created_at"] = ts_str(pd.Timestamp.now(tz="UTC"))
                cur = c.execute(f"INSERT OR IGNORE INTO trades ({','.join(TRADE_COLS)}) VALUES ({','.join('?' * len(TRADE_COLS))})",
                                [t[k] for k in TRADE_COLS])
                if cur.rowcount == 1:
                    applied.append(ev)
            elif ev["type"] == "TP1":
                cur = c.execute("UPDATE trades SET sl=?, hit_tp1=1 WHERE id=? AND hit_tp1=0", (ev["new_sl"], ev["id"]))
                if cur.rowcount == 1:
                    applied.append(ev)
            elif ev["type"] == "EXIT":
                cur = c.execute("UPDATE trades SET state='CLOSED', status=?, exit_time=?, exit_price=?, exit_reason=?, "
                                "pnl=?, max_fav=?, hit_tp1=? WHERE id=? AND state='OPEN'",
                                (ev["status"], ts_str(ev["exit_time"]), ev["exit_price"], ev["reason"], ev["pnl"],
                                 ev["max_fav"], int(ev["hit_tp1"]), ev["id"]))
                if cur.rowcount == 1:
                    applied.append(ev)
        if state["pos"] is not None:
            p = state["pos"]
            c.execute("UPDATE trades SET sl=?, max_fav=?, hit_tp1=? WHERE id=?",
                      (p["sl"], round(float(p["max_fav"]), 2), int(p["hit_tp1"]), p["id"]))
        c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)", (f"last_proc:{stream}", ts_str(idx[jl])))
        if state["last_exit_time"] is not None:
            c.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?,?)",
                      (f"last_exit:{stream}", ts_str(state["last_exit_time"])))
        c.execute("COMMIT")
        return applied
    except Exception:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        c.close()


# ==========================================================
# ZEGAR RYNKÓW (DST, święta NYSE)
# ==========================================================
def _nth(year, month, weekday, n):
    first = datetime.date(year, month, 1)
    return first + datetime.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last(year, month, weekday):
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - datetime.timedelta(days=1)
    return last - datetime.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d):
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def nyse_holidays(year):
    h = set()
    jan1 = datetime.date(year, 1, 1)
    if jan1.weekday() == 6:
        h.add(jan1 + datetime.timedelta(days=1))
    elif jan1.weekday() != 5:
        h.add(jan1)
    h.add(_nth(year, 1, 0, 3))                                   # MLK
    h.add(_nth(year, 2, 0, 3))                                   # Presidents
    h.add(easter(year) - datetime.timedelta(days=2))             # Wielki Piątek
    h.add(_last(year, 5, 0))                                     # Memorial
    h.add(_observed(datetime.date(year, 6, 19)))                 # Juneteenth
    h.add(_observed(datetime.date(year, 7, 4)))                  # Independence
    h.add(_nth(year, 9, 0, 1))                                   # Labor
    h.add(_nth(year, 11, 3, 4))                                  # Thanksgiving
    h.add(_observed(datetime.date(year, 12, 25)))                # Christmas
    return h


def nyse_close_minute(d):
    """None = giełda zamknięta; w przeciwnym razie minuta zamknięcia (960 = 16:00, 780 = 13:00)."""
    if d.weekday() >= 5 or d in nyse_holidays(d.year):
        return None
    thanksgiving = _nth(d.year, 11, 3, 4)
    if d == thanksgiving + datetime.timedelta(days=1):
        return 780
    if d.month == 12 and d.day == 24:
        return 780
    if d.month == 7 and d.day == 3 and datetime.date(d.year, 7, 4).weekday() < 5:
        return 780
    return 960


def markets_schedule():
    now = datetime.datetime.now(datetime.timezone.utc)
    ny, lon, tok = now.astimezone(NY_TZ), now.astimezone(LON_TZ), now.astimezone(TOK_TZ)

    def pl(dt):
        return dt.astimezone(PL_TZ).strftime("%H:%M")

    def pl_day(dt):
        p = dt.astimezone(PL_TZ)
        return f"{['pn', 'wt', 'śr', 'cz', 'pt', 'sb', 'nd'][p.weekday()]} {p.strftime('%H:%M')}"

    def hm(mins):
        return f"{mins // 60}h {mins % 60}m"

    m = ny.hour * 60 + ny.minute
    wd = ny.weekday()
    # US500 CFD: pn–pt 24h, przerwa 17:00–18:00 ET, weekend od pt 17:00 do nd 18:00 ET
    if wd == 5 or (wd == 4 and m >= 1020) or (wd == 6 and m < 1080):
        sun = (ny + datetime.timedelta(days=(6 - wd) % 7)).replace(hour=18, minute=0, second=0, microsecond=0)
        cfd = f"🔴 PRZERWA WEEKENDOWA (wznowienie: {pl_day(sun)} PL)"
    elif 1020 <= m < 1080:
        cfd = f"🟡 PRZERWA TECHNICZNA (wznowienie za {1080 - m} min)"
    else:
        cfd = "🟢 OTWARTY 24/5 (w święta godziny CFD mogą być skrócone — sprawdź w Capital.com)"

    close_m = nyse_close_minute(ny.date())
    o_dt = ny.replace(hour=9, minute=30, second=0, microsecond=0)
    c_dt = ny.replace(hour=(close_m or 960) // 60, minute=(close_m or 960) % 60, second=0, microsecond=0)
    if close_m is None:
        nyse = "🔴 ZAMKNIĘTY (weekend lub święto giełdowe)"
    elif 570 <= m < close_m:
        nyse = f"🟢 OTWARTY (zamyka za {hm(close_m - m)} — o {pl(c_dt)} PL)"
    elif m < 570:
        nyse = f"🔴 ZAMKNIĘTY (otwiera za {hm(570 - m)} — o {pl(o_dt)} PL)"
    else:
        nyse = "🔴 ZAMKNIĘTY (sesja zakończona)"

    lm = lon.hour * 60 + lon.minute
    l_o, l_c = lon.replace(hour=8, minute=0, second=0, microsecond=0), lon.replace(hour=16, minute=30, second=0, microsecond=0)
    if lon.weekday() < 5 and 480 <= lm < 990:
        eu = f"🟢 OTWARTY (zamyka za {hm(990 - lm)} — o {pl(l_c)} PL)"
    else:
        eu = f"🔴 ZAMKNIĘTY (sesja Europy {pl(l_o)}–{pl(l_c)} PL)"

    tm = tok.hour * 60 + tok.minute
    t_o, t_c = tok.replace(hour=9, minute=0, second=0, microsecond=0), tok.replace(hour=15, minute=30, second=0, microsecond=0)
    if tok.weekday() < 5 and (540 <= tm < 690 or 750 <= tm < 930):
        asia = "🟢 OTWARTY (sesja azjatycka trwa)"
    else:
        asia = f"🔴 ZAMKNIĘTY (sesja Tokio {pl(t_o)}–{pl(t_c)} PL)"
    return {"cfd": cfd, "nyse": nyse, "europe": eu, "asia": asia, "ny_open": pl(o_dt), "ny_close": pl(c_dt),
            "eu_open": pl(l_o), "eu_close": pl(l_c), "tok_open": pl(t_o), "tok_close": pl(t_c)}


# ==========================================================
# WIADOMOŚCI (RSS) — sentyment tylko z ostatnich godzin, słowa kluczowe z granicami słów
# ==========================================================
@st.cache_data(ttl=120)
def fetch_news():
    feeds = [
        ("Investing Central Banks", "https://www.investing.com/rss/news_14.rss"),
        ("Investing Economy", "https://www.investing.com/rss/news_95.rss"),
        ("Investing Stock Market", "https://www.investing.com/rss/news_25.rss"),
        ("CNBC US Markets", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
        ("MarketWatch Top", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ]
    articles, status, seen = [], [], set()
    bull_score = bear_score = 0
    now = datetime.datetime.now(datetime.timezone.utc)
    for source, url in feeds:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=5)
            if r.status_code != 200:
                status.append(f"❌ {source}: HTTP {r.status_code}")
                continue
            root = SafeET.fromstring(r.content)
            cnt = 0
            for item in root.findall(".//item")[:6]:
                g = lambda tag: clean_html_text((item.find(tag).text if item.find(tag) is not None else ""))
                title, desc, pub = g("title"), g("description"), g("pubDate")
                if not title or title in seen:
                    continue
                seen.add(title)
                cnt += 1
                age_h = None
                try:
                    age_h = (now - email.utils.parsedate_to_datetime(pub).astimezone(datetime.timezone.utc)).total_seconds() / 3600
                except Exception:
                    pass
                text = f"{title} {desc}"
                b, s = len(BULLISH_RE.findall(text)), len(BEARISH_RE.findall(text))
                fresh = age_h is not None and age_h <= NEWS_MAX_AGE_H
                if fresh:
                    bull_score += b
                    bear_score += s
                impact = "🔴 Pro-SHORT" if s > b else ("🟢 Pro-LONG" if b > s else "⚪ Neutralny")
                articles.append({"source": source, "title": title, "pub": pub, "impact": impact,
                                 "fresh": fresh, "description": (desc[:300] + "...") if len(desc) > 300 else (desc or "brak opisu")})
            status.append(f"✅ {source}: OK ({cnt} art.)")
        except Exception as e:
            status.append(f"⚠️ {source}: {type(e).__name__}")
    if bear_score >= bull_score + 2:
        overall, bias = "🔴 PRZEWAGA NEGATYWNYCH wiadomości (ostatnie godziny)", -1
    elif bull_score >= bear_score + 2:
        overall, bias = "🟢 PRZEWAGA POZYTYWNYCH wiadomości (ostatnie godziny)", 1
    else:
        overall, bias = "⚪ NEUTRALNIE / MIESZANIE (lub brak świeżych wiadomości)", 0
    return articles[:20], status, overall, bias, bull_score, bear_score


# ==========================================================
# BACKTEST
# ==========================================================
def run_backtest(df, interval, cfg, lp):
    B = build_bars(df, interval)
    state = new_state("BT", "BT")
    cfg = dict(cfg, entry_j_min=60, halt=False, news_veto=False)
    events = run_engine(B, 55, B["n"] - 2, state, cfg, lp)
    trades = {}
    for ev in events:
        if ev["type"] == "OPEN":
            trades[ev["trade"]["id"]] = dict(ev["trade"])
        elif ev["type"] == "EXIT":
            t = trades[ev["id"]]
            t.update(state="CLOSED", status=ev["status"], exit_time=ts_str(ev["exit_time"]), exit_price=ev["exit_price"],
                     exit_reason=ev["reason"], pnl=ev["pnl"], max_fav=ev["max_fav"], hit_tp1=int(ev["hit_tp1"]))
    return pd.DataFrame(list(trades.values())), B


def breakdown_by_hour(df):
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["Godzina (PL)"] = pd.to_datetime(d["entry_time"], utc=True).dt.tz_convert(PL_TZ).dt.hour
    g = d.groupby("Godzina (PL)")["pnl"].agg(["count", "sum", "mean"]).round(2)
    g.columns = ["Transakcji", "Suma pkt", "Średnio pkt"]
    return g.reset_index()


# ==========================================================
# INTERFEJS
# ==========================================================
def password_gate():
    pw = get_secret("APP_PASSWORD")
    if not pw or st.session_state.get("auth_ok"):
        return
    entered = st.text_input("🔒 Hasło do aplikacji", type="password")
    if entered and hmac.compare_digest(entered.encode(), pw.encode()):
        st.session_state["auth_ok"] = True
        st.rerun()
    st.stop()


def get_creds():
    if "creds" not in st.session_state:
        st.session_state["creds"] = {
            "is_demo": get_secret("CAPITAL_DEMO", "1").lower() not in ("0", "false", "no"),
            "cap_key": get_secret("CAPITAL_API_KEY"), "cap_id": get_secret("CAPITAL_IDENTIFIER"),
            "cap_pass": get_secret("CAPITAL_PASSWORD"), "openai_key": get_secret("OPENAI_API_KEY"),
            "tg_token": get_secret("TELEGRAM_BOT_TOKEN"), "tg_chat": get_secret("TELEGRAM_CHAT_ID"),
        }
    return st.session_state["creds"]


def build_trades_table(df):
    if df.empty:
        return df
    t = df.copy().iloc[::-1]
    def verdict(r):
        if r["state"] == "OPEN":
            return "⏳ W GRZE"
        if r["exit_reason"] == "NO_DATA":
            return "❓ NIEROZLICZONA"
        return "✅ DOBRY" if float(r["pnl"] or 0) > 0 else "❌ BŁĘDNY"
    out = pd.DataFrame({
        "Ocena": t.apply(verdict, axis=1),
        "Sygnał (PL)": t["signal_time"].map(to_pl), "Wejście (PL)": t["entry_time"].map(to_pl),
        "Kierunek": t["direction"].map({1: "🟢 LONG", -1: "🔴 SHORT"}), "Typ": t["pattern"],
        "Cena wejścia": t["entry"], "Cena wyjścia": t["exit_price"], "SL start": t["sl0"], "TP1": t["tp1"], "TP2": t["tp2"],
        "Maks. zysk": t["max_fav"], "Jak zamknięto": t["status"], "Wynik netto (pkt)": t["pnl"],
        "Zamknięcie (PL)": t["exit_time"].map(to_pl), "Zlecenie DEMO": t["order_state"],
    })
    return out


def main():
    st.set_page_config(page_title="SP500 SIGNAL AI PRO — v7.1", page_icon="⚡", layout="wide")
    if TZ_ERROR:
        st.error("Brak bazy stref czasowych (typowe na Windows). Zainstaluj: `pip install tzdata` i uruchom ponownie. "
                 f"Szczegóły: {TZ_ERROR}")
        st.stop()
    password_gate()
    if "db_ready" not in st.session_state:
        init_db()
        st.session_state["db_ready"] = True
    creds = get_creds()
    st.session_state.setdefault("log", [])

    # ------------------------- PASEK BOCZNY -------------------------
    with st.sidebar:
        st.title("⚡ SP500 Signal AI Pro v7.1")
        st.caption("Jeden silnik • TP1→break-even • limity ryzyka • backtest")
        with st.expander("🔑 Klucze API (tylko w pamięci sesji)", expanded=not bool(creds.get("cap_key"))):
            st.caption("Najlepiej ustaw je w zmiennych środowiskowych lub `.streamlit/secrets.toml` — wtedy nie trzeba ich wpisywać.")
            with st.form("api_form"):
                is_demo = st.checkbox("Konto DEMO Capital.com", value=creds["is_demo"])
                cap_key = st.text_input("Capital API Key", value=creds["cap_key"], type="password")
                cap_id = st.text_input("Capital login (email)", value=creds["cap_id"])
                cap_pass = st.text_input("Capital hasło API", value=creds["cap_pass"], type="password")
                oa_key = st.text_input("OpenAI API Key (czat)", value=creds["openai_key"], type="password")
                tg_token = st.text_input("Telegram bot token (alerty)", value=creds["tg_token"], type="password")
                tg_chat = st.text_input("Telegram chat id", value=creds["tg_chat"])
                if st.form_submit_button("🔐 Zastosuj", type="primary", **stretch(st.form_submit_button)):
                    st.session_state["creds"] = {"is_demo": is_demo, "cap_key": cap_key.strip(), "cap_id": cap_id.strip(),
                                                 "cap_pass": cap_pass.strip(), "openai_key": oa_key.strip(),
                                                 "tg_token": tg_token.strip(), "tg_chat": tg_chat.strip()}
                    st.session_state.pop("cap_session_cache", None)
                    st.rerun()

        st.subheader("1. Strategia")
        trade_style = st.radio("Cel zysku:", [STYLE_WAVE, STYLE_SCALP], index=0)
        resolution = st.selectbox("Interwał:", list(INTERVAL_MIN), index=0)
        sensitivity = st.radio("Filtr strzałek:", [SENS_STRICT, SENS_STD, SENS_HIGH], index=0)
        enable_learning = st.toggle("🧠 Samo-Audyt (reguły od 30 transakcji)", value=True)
        max_spread = st.slider("Maks. spread do wejścia (pkt):", 0.2, 2.5, 0.9, 0.1)
        max_hold_hours = st.number_input("Maks. czas pozycji (godz.):", 0.5, 48.0, 6.0, 0.5)

        st.subheader("2. Ryzyko i filtry")
        halt = st.toggle("⛔ STOP: nie otwieraj nowych pozycji", value=False, key="halt")
        max_trades_day = st.number_input("Maks. transakcji / dzień:", 1, 50, 6)
        daily_loss_limit = st.number_input("Dzienny limit straty (pkt):", 5.0, 500.0, 40.0, 5.0)
        session_on = st.toggle("🕘 Handluj tylko w oknie godzin (PL)", value=False)
        s_from = st.time_input("Od:", datetime.time(9, 0))
        s_to = st.time_input("Do:", datetime.time(22, 0))
        macro_block = st.toggle("📅 Blokuj wejścia w oknach danych makro USA", value=True, key="macro_block")
        news_veto = st.toggle("📰 Weto wiadomości (nie otwieraj pod prąd sentymentu)", value=False,
                              help="Słowa kluczowe to grube narzędzie — domyślnie wyłączone, nie da się go przetestować w backteście.")

        st.subheader("3. Auto-trading DEMO")
        auto_trade = st.toggle("🤖 Auto-trading DEMO Capital.com", value=False, key="auto_trade",
                               help="Działa tylko na koncie DEMO i tylko na danych z Capital.com.")
        trade_size = st.number_input("Wielkość pozycji (kontrakty):", 0.1, 50.0, 1.0, 0.5)

        st.subheader("4. Alerty i odświeżanie")
        sound_on = st.toggle("🔊 Dźwięk w przeglądarce", value=True)
        tg_on = st.toggle("📨 Alerty Telegram", value=bool(creds.get("tg_token")))
        if st.button("🔊 Test dźwięku", **stretch(st.button)):
            trigger_beep_alarm("LONG")
        auto_refresh = st.toggle("🔄 Auto-odświeżanie", value=True, key="auto_refresh")
        refresh_sec = st.slider("Co ile sekund:", 3, 30, 5)

    st.title("🎯 SP500 SIGNAL AI PRO — v7.1")
    sched = markets_schedule()
    mc = st.columns(4)
    mc[0].info(f"**🇺🇸 US500 CFD:**\n\n{sched['cfd']}")
    mc[1].info(f"**🗽 Wall Street (NYSE):**\n\n{sched['nyse']}")
    mc[2].info(f"**🇪🇺 Europa:**\n\n{sched['europe']}")
    mc[3].info(f"**🇯🇵 Azja:**\n\n{sched['asia']}")

    try:
        body(creds, locals())
        st.session_state["last_ok_run"] = datetime.datetime.now(PL_TZ).strftime("%H:%M:%S")
    except Exception as exc:  # błąd jednego przebiegu nie może zabić pętli na żywo
        st.error(f"⚠️ Błąd w tym przebiegu: {type(exc).__name__}: {exc} — bot spróbuje ponownie przy następnym odświeżeniu "
                 f"(ostatni udany przebieg: {st.session_state.get('last_ok_run', 'brak')}).")
        with st.expander("Szczegóły błędu (skopiuj, jeśli zgłaszasz problem)"):
            st.code(traceback.format_exc())

    if st.session_state.get("auto_refresh", True):
        time.sleep(refresh_sec)
        st.rerun()


def body(creds, ui):
    trade_style, resolution, sensitivity = ui["trade_style"], ui["resolution"], ui["sensitivity"]
    wave30 = trade_style == STYLE_WAVE
    interval_min = INTERVAL_MIN[resolution]
    auto_ok_flag = ui["auto_trade"]
    sched = ui["sched"]

    # ------------------------- DANE -------------------------
    client = CapitalClient(creds["cap_key"], creds["cap_id"], creds["cap_pass"], creds["is_demo"])
    src = "CAP"
    df_raw, current_spread, source_status = client.fetch_candles(EPIC, resolution, 500)
    cap_msg = source_status
    if df_raw.empty:
        df_raw, current_spread, source_status = fetch_fallback_data(resolution)
        src = "YF"
    if df_raw.empty or len(df_raw) < 80:
        st.error(f"Nie udało się pobrać wystarczającej liczby świec. Capital.com: **{cap_msg}** | Yahoo: {source_status}. "
                 f"Sprawdź klucze w Secrets (CAPITAL_API_KEY, CAPITAL_IDENTIFIER, CAPITAL_PASSWORD) i czy konto jest DEMO.")
        return
    if src == "YF":
        st.warning(f"Capital.com niedostępne: **{cap_msg}**. {source_status}. Dane Yahoo bywają opóźnione, więc nowe wejścia "
                   f"zwykle są blokowane (test świeżości).")
    if IS_CLOUD:
        st.caption("☁️ Streamlit Cloud: bot liczy sygnały tylko, gdy ta karta jest otwarta, a archiwum jest tymczasowe — "
                   "pobieraj kopię zapasową w zakładce Archiwum. Auto-trading DEMO wolisz uruchamiać lokalnie.")
    spread = float(current_spread) if current_spread else 0.6

    B = build_bars(df_raw, resolution)
    idx, n = B["idx"], B["n"]
    now_utc = pd.Timestamp.now(tz="UTC")
    live_age = (now_utc - idx[-1]).total_seconds()
    max_delay = max(25.0, 0.3 * interval_min * 60)
    fresh = live_age <= max_delay                       # wolno wejść: świeca otwarta przed chwilą
    stale = live_age > interval_min * 60 + 30           # dane faktycznie stare / rynek zamknięty
    spread_ok = spread <= ui["max_spread"]

    stream = f"{src}|{resolution}|{'W30' if wave30 else 'SCALP'}|{SENS_CODE[sensitivity]}"
    closed_stream = load_closed(stream)
    audit = run_self_learning_audit(closed_stream) if ui["enable_learning"] else run_self_learning_audit(pd.DataFrame(columns=TRADE_COLS))
    lp = audit["lp"]

    news_articles, news_status, news_text, news_bias, bull_sc, bear_sc = fetch_news()
    cfg = {
        "wave30": wave30, "sensitivity": sensitivity, "spread": spread, "max_hold_hours": float(ui["max_hold_hours"]),
        "halt": bool(ui["halt"]), "max_trades_day": int(ui["max_trades_day"]), "daily_loss_limit": float(ui["daily_loss_limit"]),
        "session_on": bool(ui["session_on"]), "session_start": ui["s_from"].hour * 60 + ui["s_from"].minute,
        "session_end": ui["s_to"].hour * 60 + ui["s_to"].minute, "macro_block": bool(ui["macro_block"]),
        "news_veto": bool(ui["news_veto"]), "news_bias": news_bias, "cooldown_bars": 2,
        "mode_label": "W30" if wave30 else "SCALP",
        "entry_j_min": (n - 2) if (fresh and spread_ok) else n + 10,   # nowe wejście tylko na świeżej, ostatniej świecy
    }

    events = process_live(B, stream, src, cfg, lp)

    # ------------------------- ZDARZENIA: zlecenia, dźwięk, alerty -------------------------
    auto_ok = bool(auto_ok_flag and creds["is_demo"] and src == "CAP")
    if auto_ok_flag and not creds["is_demo"]:
        st.error("⛔ Auto-trading zablokowany: odznaczono konto DEMO. Ten program handluje automatycznie wyłącznie na DEMO.")
    elif auto_ok_flag and src != "CAP":
        st.warning("⛔ Auto-trading wstrzymany: dane pochodzą z trybu awaryjnego (nie z Capital.com).")
    alarm = None
    for ev in events:
        msgs = []
        if ev["type"] == "OPEN":
            tr = ev["trade"]
            msgs.append(f"{'🟢 LONG' if tr['direction'] == 1 else '🔴 SHORT'} {tr['pattern']} @ {tr['entry']:.2f} | "
                        f"SL {tr['sl0']:.2f} | TP1 {tr['tp1']:.2f} | TP2 {tr['tp2']:.2f}")
            alarm = "LONG" if tr["direction"] == 1 else "SHORT"
            if auto_ok:
                msgs.append(broker_open(client, tr, ui["trade_size"]))
        elif ev["type"] == "TP1":
            msgs.append(f"🎯 TP1 osiągnięte — SL na break-even: {ev['new_sl']:.2f}")
            if auto_ok:
                m = broker_move_stop(client, ev["id"], ev["new_sl"])
                msgs.append(m) if m else None
        elif ev["type"] == "EXIT":
            msgs.append(f"{ev['status']} @ {ev['exit_price']:.2f}")
            alarm = "EXIT" if ev["pnl"] > 0 else alarm
            if auto_ok:
                m = broker_close(client, ev["id"])
                msgs.append(m) if m else None
        for m in msgs:
            st.toast(m)
            st.session_state["log"].append(f"{datetime.datetime.now(PL_TZ).strftime('%H:%M:%S')} — {m}")
        if ui["tg_on"] and msgs:
            send_telegram(creds["tg_token"], creds["tg_chat"], "\n".join(msgs))
    st.session_state["log"] = st.session_state["log"][-40:]
    if alarm and ui["sound_on"]:
        trigger_beep_alarm(alarm)

    # ------------------------- DANE DO WYŚWIETLANIA -------------------------
    all_tr = load_trades("stream=?", (stream,))
    closed_stream = load_closed(stream)
    st_stream = compute_stats(closed_stream)
    open_tr = all_tr[all_tr["state"] == "OPEN"]
    today_key = datetime.datetime.now(PL_TZ).strftime("%Y-%m-%d")
    cl_all = load_closed()
    today_pnl = 0.0
    if not cl_all.empty:
        mask = pd.to_datetime(cl_all["exit_time"], utc=True).dt.tz_convert(PL_TZ).dt.strftime("%Y-%m-%d") == today_key
        today_pnl = float(cl_all[mask & (cl_all["src"] == src)]["pnl"].sum())
    n_today = int((pd.to_datetime(all_tr["entry_time"], utc=True).dt.tz_convert(PL_TZ).dt.strftime("%Y-%m-%d") == today_key).sum()) if not all_tr.empty else 0

    closed_c, live_c = B["df"].iloc[-2], B["df"].iloc[-1]
    price = float(live_c["Close"])
    atr_now = float(closed_c["ATR"])
    htf_bias = int(B["htf"][-2])
    macro_now = in_macro(int(B["ny_min"][-1]), int(B["ny_wd"][-1]))
    now_pl_min = datetime.datetime.now(PL_TZ).hour * 60 + datetime.datetime.now(PL_TZ).minute

    if interval_min == 1 and ui["refresh_sec"] > 10:
        st.warning(f"⚠️ Odświeżanie co {ui['refresh_sec']} s jest za rzadkie dla M1 — sygnały spóźnione o >{int(max_delay)} s "
                   f"są pomijane (bot nie wchodzi 'po fakcie'). Ustaw ≤10 s.")
    st.caption(
        f"{source_status} | 🔒 Bot ocenia wyłącznie ZAMKNIĘTE świece (ostatnia: {closed_c.name.tz_convert(PL_TZ).strftime('%H:%M')} PL) | "
        f"🧠 Samo-Audyt: {audit['grade']} | strumień: `{stream}`"
    )
    blocks = []
    if ui["halt"]:
        blocks.append("⛔ STOP awaryjny")
    if stale:
        blocks.append("dane nieaktualne / rynek zamknięty")
    if not spread_ok:
        blocks.append(f"spread {spread:.2f} > limit {ui['max_spread']:.1f}")
    if ui["macro_block"] and macro_now:
        blocks.append("okno danych makro USA")
    if ui["session_on"] and not (cfg["session_start"] <= now_pl_min < cfg["session_end"]):
        blocks.append("poza oknem godzin")
    if n_today >= cfg["max_trades_day"]:
        blocks.append("limit transakcji na dziś")
    if today_pnl <= -cfg["daily_loss_limit"]:
        blocks.append("DZIENNY LIMIT STRATY")
    if blocks:
        st.warning("🚦 Nowe wejścia wstrzymane: " + " • ".join(blocks))
    if news_bias == -1:
        st.error(f"📰 {news_text} (negatywne: {bear_sc} vs pozytywne: {bull_sc}) — informacyjnie" + (" + WETO aktywne" if ui["news_veto"] else ""))
    elif news_bias == 1:
        st.success(f"📰 {news_text} (pozytywne: {bull_sc} vs negatywne: {bear_sc}) — informacyjnie" + (" + WETO aktywne" if ui["news_veto"] else ""))
    else:
        st.info(f"📰 {news_text} — informacyjnie")

    tab_main, tab_arch, tab_bt, tab_audit, tab_news = st.tabs(
        ["📊 Wykres i bieżąca pozycja", "🗄️ Archiwum", "🧪 Backtest", "🧠 Samo-Audyt", "🌍 Rynki i wiadomości"])

    # ======================= TAB: WYKRES =======================
    with tab_main:
        hs = spread / 2.0
        if not open_tr.empty:
            r = open_tr.iloc[-1]
            d = int(r["direction"])
            exit_side = price - hs if d == 1 else price + hs
            live_pnl = d * (exit_side - float(r["entry"]))
            box = st.success if d == 1 else st.error
            box(f"### ⏳ POZYCJA W GRZE: {'🟢 LONG' if d == 1 else '🔴 SHORT'} od {float(r['entry']):.2f} (wejście {to_pl(r['entry_time'], '%H:%M')} PL)\n"
                f"Wynik na żywo (po spreadzie): **{live_pnl:+.2f} pkt** | maks. zysk: +{float(r['max_fav'] or 0):.1f} | "
                f"SL: **{float(r['sl']):.2f}**{' (break-even po TP1)' if r['hit_tp1'] else ''} | TP1: {float(r['tp1']):.2f} | TP2: {float(r['tp2']):.2f} | "
                f"zlecenie DEMO: `{r['order_state']}`")
            if auto_ok and r["order_state"] == "ACCEPTED":
                pos_list, _ = client.get_open_positions(EPIC)
                if pos_list is not None and r["deal_id"] not in {p.get("position", {}).get("dealId") for p in pos_list}:
                    st.warning("⚠️ Bot zakłada otwartą pozycję, ale na koncie DEMO jej nie ma (zamknięta przez SL/TP/ręcznie). "
                               "Stan archiwum zsynchronizuje się przy następnym zdarzeniu silnika.")
        elif not closed_stream.empty:
            lr = closed_stream.iloc[-1]
            st.info(f"✅ Ostatnia pozycja zamknięta: {'LONG' if lr['direction'] == 1 else 'SHORT'} — **{lr['status']}**. Czekam na nowy sygnał.")
        else:
            st.info("⚪ Czekam na pierwszy sygnał (bot nie cofa się w przeszłość — pozycje liczy od uruchomienia).")

        c = st.columns(6)
        c[0].metric("Cena US500", f"{price:.2f}")
        c[1].metric("Spread / ATR", f"{spread:.2f} / {atr_now:.2f}")
        c[2].metric("Trend wyższy", "🟢 WZROSTOWY" if htf_bias > 0 else ("🔴 SPADKOWY" if htf_bias < 0 else "⚪ PŁASKI"))
        c[3].metric("Skuteczność (ten tryb)", f"{st_stream['win_rate']}% ({st_stream['wins']}W/{st_stream['losses']}L)")
        c[4].metric("Oczekiwana wartość", f"{st_stream['expectancy']:+.2f} pkt", delta=f"suma {st_stream['total']:+.1f}")
        c[5].metric("Dziś (PL)", f"{today_pnl:+.1f} pkt", delta=f"{n_today}/{cfg['max_trades_day']} transakcji", delta_color="off")

        dfp = B["df"].tail(95).copy()
        dfp.index = dfp.index.tz_convert(PL_TZ).tz_localize(None)
        fig = go.Figure()
        fig.add_trace(go.Candlestick(x=dfp.index, open=dfp["Open"], high=dfp["High"], low=dfp["Low"], close=dfp["Close"],
                                     name="US500", increasing_line_color="#00e676", decreasing_line_color="#ff1744"))
        fig.add_trace(go.Scatter(x=dfp.index, y=dfp["EMA9"], line=dict(color="#ffea00", width=1.3), name="EMA 9"))
        fig.add_trace(go.Scatter(x=dfp.index, y=dfp["EMA21"], line=dict(color="#2979ff", width=1.6), name="EMA 21"))
        fig.add_trace(go.Scatter(x=dfp.index, y=dfp["EMA50"], line=dict(color="#ab47bc", width=1.2, dash="dot"), name="EMA 50"))
        if not all_tr.empty:
            win_start = B["idx"][-95] if n >= 95 else B["idx"][0]
            vis = all_tr[pd.to_datetime(all_tr["signal_time"], utc=True) >= win_start].copy()
            if not vis.empty:
                vis["x"] = pd.to_datetime(vis["signal_time"], utc=True).dt.tz_convert(PL_TZ).dt.tz_localize(None)
                for dirn, color, sym, lab, pos_txt in [(1, "#00e676", "triangle-up", "▲ LONG", "bottom center"),
                                                       (-1, "#ff1744", "triangle-down", "▼ SHORT", "top center")]:
                    sub = vis[vis["direction"] == dirn]
                    if not sub.empty:
                        fig.add_trace(go.Scatter(x=sub["x"], y=sub["arrow"], mode="markers+text", text=[lab] * len(sub),
                                                 textposition=pos_txt, name=f"Otwarcie {lab[2:]}",
                                                 marker=dict(symbol=sym, size=16, color=color, line=dict(color="white", width=1))))
                ex_df = vis[vis["state"] == "CLOSED"]
                ex_df = ex_df[ex_df["exit_reason"] != "NO_DATA"]
                if not ex_df.empty:
                    ex_df = ex_df.assign(xx=pd.to_datetime(ex_df["exit_time"], utc=True).dt.tz_convert(PL_TZ).dt.tz_localize(None))
                    fig.add_trace(go.Scatter(x=ex_df["xx"], y=ex_df["exit_price"], mode="markers+text", text=ex_df["status"],
                                             textposition="bottom center", name="Zamknięcia",
                                             marker=dict(symbol="diamond", size=13, color=["#ffd600" if p > 0 else "#ff5252" for p in ex_df["pnl"]],
                                                         line=dict(color="black", width=1))))
        if not open_tr.empty:
            r = open_tr.iloc[-1]
            for y, col, dash, name in [(r["entry"], "#ffffff", "dash", "Wejście"), (r["sl"], "#ff1744", "dot", "SL"),
                                       (r["tp1"], "#00e676", "dot", "TP1"), (r["tp2"], "#00c853", "solid", "TP2")]:
                fig.add_hline(y=float(y), line_dash=dash, line_color=col, annotation_text=f"{name}: {float(y):.2f}",
                              annotation_position="right")
        fig.update_layout(height=560, template="plotly_dark", xaxis_rangeslider_visible=False, margin=dict(l=15, r=95, t=30, b=15))
        st.plotly_chart(fig, **stretch(st.plotly_chart))

        with st.expander("📜 Dziennik zdarzeń (ostatnie 40)"):
            for line in reversed(st.session_state["log"]):
                st.write(line)
            if not st.session_state["log"]:
                st.caption("Brak zdarzeń w tej sesji.")

    # ======================= TAB: ARCHIWUM =======================
    with tab_arch:
        scope = st.radio("Zakres:", ["Ten tryb (strumień)", "Wszystkie tryby"], horizontal=True)
        tdf = all_tr if scope.startswith("Ten") else load_trades()
        cdf = closed_stream if scope.startswith("Ten") else load_closed()
        s = compute_stats(cdf)
        a = st.columns(6)
        a[0].metric("Transakcje (zamknięte)", s["n"])
        a[1].metric("Skuteczność", f"{s['win_rate']}%", delta=f"{s['wins']}W / {s['losses']}L", delta_color="off")
        a[2].metric("Wynik netto", f"{s['total']:+.1f} pkt")
        a[3].metric("Oczekiwana wartość", f"{s['expectancy']:+.2f} pkt")
        a[4].metric("Profit factor", fmt_pf(s["profit_factor"]))
        a[5].metric("Maks. obsunięcie", f"{s['max_dd']:.1f} pkt")
        st.caption("Wyniki są NETTO po spreadzie (wejście po ASK/BID, wyjście po BID/ASK). Otwarte pozycje nie wchodzą do statystyk.")
        if not tdf.empty:
            if not cdf.empty:
                eq = go.Figure(go.Scatter(x=list(range(1, len(cdf) + 1)), y=cdf["pnl"].cumsum(), mode="lines", line=dict(color="#00e676")))
                eq.update_layout(height=220, template="plotly_dark", margin=dict(l=10, r=10, t=25, b=10),
                                 title="Krzywa kapitału (pkt, narastająco)")
                st.plotly_chart(eq, **stretch(st.plotly_chart))
            tbl = build_trades_table(tdf)
            st.dataframe(tbl, **stretch(st.dataframe))
            b1, b2 = st.columns(2)
            b1.download_button("📥 Pobierz CSV", tbl.to_csv(index=False).encode("utf-8-sig"),
                               file_name=f"archiwum_v7_{datetime.datetime.now(PL_TZ).strftime('%Y%m%d_%H%M')}.csv",
                               mime="text/csv", **stretch(st.download_button))
            with b2:
                confirm = st.checkbox("Potwierdzam usunięcie CAŁEGO archiwum")
                if st.button("🗑️ Wyczyść archiwum", disabled=not confirm, **stretch(st.button)):
                    db_run("DELETE FROM trades")
                    db_run("DELETE FROM kv")
                    st.rerun()
        else:
            st.info("Archiwum jest puste.")
        st.markdown("**💾 Kopia zapasowa archiwum**" + (" — w chmurze plik znika po restarcie, więc pobieraj ją regularnie." if IS_CLOUD else ""))
        k1, k2 = st.columns(2)
        k1.download_button("💾 Pobierz kopię (JSON)", export_db_json().encode("utf-8"),
                           file_name=f"sp500_backup_{datetime.datetime.now(PL_TZ).strftime('%Y%m%d_%H%M')}.json",
                           mime="application/json", **stretch(k1.download_button))
        up = k2.file_uploader("Wczytaj kopię (JSON)", type=["json"])
        if up is not None and k2.button("📤 Importuj kopię", **stretch(k2.button)):
            try:
                st.success(f"Zaimportowano {import_db_json(up.getvalue().decode('utf-8'))} nowych transakcji.")
            except Exception as exc:
                st.error(f"Nie udało się wczytać kopii: {type(exc).__name__}")

    # ======================= TAB: BACKTEST =======================
    with tab_bt:
        st.markdown("Backtest używa **tego samego silnika** co wersja na żywo, z kosztami (pełny spread). "
                    "Parametry Samo-Audytu są tu wyłączone (bazowe progi). To dowód tylko dla badanego okresu — nie gwarancja.")
        max_days = {"MINUTE": 10, "MINUTE_5": 30, "MINUTE_15": 60}[resolution]
        b1, b2 = st.columns(2)
        days = b1.slider("Ile dni historii:", 1, max_days, min(5, max_days))
        bt_spread = b2.number_input("Zakładany spread (pkt):", 0.2, 3.0, round(max(spread, 0.6), 1), 0.1)
        if st.button("▶️ Uruchom backtest", **stretch(st.button)):
            with st.spinner("Pobieram historię i liczę..."):
                hist = pd.DataFrame()
                if creds["cap_key"]:
                    hist = cached_history(client, creds["cap_id"] + str(creds["is_demo"]), resolution, days,
                                          int(time.time() // 1800))
                used = "Capital.com"
                if hist.empty:
                    period = {"MINUTE": "7d", "MINUTE_5": "30d", "MINUTE_15": "60d"}[resolution]
                    hist, _, _ = fetch_fallback_data(resolution, period)
                    used = "Yahoo Finance (^GSPC — to nie jest CFD US500, wynik orientacyjny)"
            if hist.empty or len(hist) < 120:
                st.error("Za mało danych historycznych do backtestu.")
            else:
                bt_cfg = dict(cfg, spread=float(bt_spread))
                trades_bt, _ = run_backtest(hist, resolution, bt_cfg, dict(DEFAULT_LP))
                done = trades_bt[trades_bt["state"] == "CLOSED"] if not trades_bt.empty else trades_bt
                st.session_state["bt_result"] = (done, used, len(hist), hist.index[0], hist.index[-1])
        if "bt_result" in st.session_state:
            done, used, nb, t0, t1 = st.session_state["bt_result"]
            st.caption(f"Źródło: {used} | świec: {nb} | okres: {to_pl(ts_str(t0), '%Y-%m-%d %H:%M')} → {to_pl(ts_str(t1), '%Y-%m-%d %H:%M')} (PL)")
            if done.empty:
                st.info("Brak zamkniętych transakcji w tym okresie przy bieżących filtrach.")
            else:
                done = done.sort_values("exit_time").reset_index(drop=True)
                bs = compute_stats(done)
                m = st.columns(6)
                m[0].metric("Transakcje", bs["n"])
                m[1].metric("Skuteczność", f"{bs['win_rate']}%")
                m[2].metric("Wynik netto", f"{bs['total']:+.1f} pkt")
                m[3].metric("Oczekiwana wartość", f"{bs['expectancy']:+.2f} pkt")
                m[4].metric("Profit factor", fmt_pf(bs["profit_factor"]))
                m[5].metric("Maks. obsunięcie", f"{bs['max_dd']:.1f} pkt")
                st.caption(f"Śr. wygrana {bs['avg_win']:+.2f} | śr. strata {bs['avg_loss']:+.2f}. "
                           f"{'⚠️ Mała próba (<30) — wnioski niewiarygodne.' if bs['n'] < 30 else ''}")
                eq = go.Figure(go.Scatter(x=list(range(1, len(done) + 1)), y=done["pnl"].cumsum(), mode="lines", line=dict(color="#00e676")))
                eq.update_layout(height=240, template="plotly_dark", margin=dict(l=10, r=10, t=25, b=10), title="Krzywa kapitału backtestu (pkt)")
                st.plotly_chart(eq, **stretch(st.plotly_chart))
                cc1, cc2 = st.columns(2)
                cc1.markdown("**Wg godziny wejścia (PL)**")
                cc1.dataframe(breakdown_by_hour(done), **stretch(cc1.dataframe))
                by_reason = done.groupby("exit_reason")["pnl"].agg(["count", "sum", "mean"]).round(2).reset_index()
                by_reason.columns = ["Powód wyjścia", "Transakcji", "Suma pkt", "Średnio pkt"]
                cc2.markdown("**Wg sposobu zamknięcia**")
                cc2.dataframe(by_reason, **stretch(cc2.dataframe))
                st.dataframe(build_trades_table(done), **stretch(st.dataframe))

    # ======================= TAB: SAMO-AUDYT =======================
    with tab_audit:
        st.markdown(f"Reguły działają dopiero od **{MIN_SAMPLE}** zamkniętych transakcji w danym trybie (osobno interwał/styl/filtr) "
                    "i zmieniają parametry tylko dla NOWYCH transakcji. To proste progi, nie uczenie maszynowe.")
        u = st.columns(4)
        u[0].metric("Ocena", audit["grade"])
        u[1].metric("Próg konsolidacji", f"{lp['min_atr_mult']:.2f}× ATR")
        u[2].metric("Próg zamykania zysku", f"+{lp['dyn_trig']:.1f} pkt")
        u[3].metric("RSI (LONG max / SHORT min)", f"{lp['rsi_long_max']:.0f} / {lp['rsi_short_min']:.0f}")
        for ls in audit["lessons"]:
            st.markdown(f"- {ls}")
        if not closed_stream.empty:
            st.markdown("**Statystyki tego trybu:**")
            st.json({k: (fmt_pf(v) if k == "profit_factor" else v) for k, v in audit["stats"].items()})

    # ======================= TAB: RYNKI I WIADOMOŚCI =======================
    with tab_news:
        st.subheader("🌍 Sesje giełdowe (czas polski, z uwzględnieniem zmiany czasu i świąt NYSE)")
        st.markdown(
            f"- **US500 CFD:** pn–pt 24 h, przerwa dzienna 17:00–18:00 ET, weekend od pt 17:00 do nd 18:00 ET.\n"
            f"- **Wall Street (NYSE):** dziś {sched['ny_open']}–{sched['ny_close']} PL.\n"
            f"- **Londyn:** dziś {sched['eu_open']}–{sched['eu_close']} PL.\n"
            f"- **Tokio:** dziś {sched['tok_open']}–{sched['tok_close']} PL (przerwa obiadowa 11:30–12:30 JST).")
        st.caption("Dziś jest różnica czasu między USA a Europą tylko w kilku tygodniach w roku (marzec i koniec października/początek listopada) — zegar liczy to automatycznie.")
        st.subheader("📡 Kanały RSS")
        st.write(" | ".join(news_status))
        st.subheader("📰 Wiadomości")
        for art in news_articles:
            st.markdown(f"**{art['impact']}{'' if art['fresh'] else ' (starsza)'} | [{art['source']}] {art['title']}** *({art['pub']})*")
            st.caption(f"📄 {art['description']}")

    with st.expander("🛠️ Diagnostyka (przydatna przy problemach na Streamlit Cloud)"):
        try:
            db_ok = os.access(os.path.dirname(os.path.abspath(DB_FILE)) or ".", os.W_OK)
            counts = db_query("SELECT stream, state, COUNT(*) AS n FROM trades GROUP BY stream, state")
            last_proc = db_query("SELECT key, value FROM kv WHERE key LIKE 'last_proc:%'")
        except Exception as exc:
            db_ok, counts, last_proc = f"błąd {type(exc).__name__}", [], []
        st.json({
            "wersje": {"python": platform.python_version(), "streamlit": st.__version__, "pandas": pd.__version__,
                       "numpy": np.__version__, "yfinance": getattr(yf, "__version__", None) if yf else None},
            "środowisko": {"chmura": IS_CLOUD, "baza": os.path.abspath(DB_FILE), "baza_zapisywalna": db_ok,
                           "strefy_czasowe": "OK" if not TZ_ERROR else TZ_ERROR},
            "dane": {"źródło": src, "komunikat_Capital": cap_msg, "świec": n,
                     "pierwsza_świeca_PL": str(idx[0].tz_convert(PL_TZ)), "ostatnia_świeca_PL": str(idx[-1].tz_convert(PL_TZ)),
                     "wiek_żywej_świecy_s": round(live_age, 1), "spread": spread, "klucze_Capital_ustawione": bool(creds["cap_key"]),
                     "konto_DEMO": creds["is_demo"]},
            "silnik": {"strumień": stream, "transakcje_wg_stanu": counts, "ostatnio_przetworzone": last_proc,
                       "ostatni_udany_przebieg": st.session_state.get("last_ok_run")},
        })

    # ======================= CZAT =======================
    st.markdown("---")
    st.subheader("🤖 Czat z analitykiem AI (wyjaśnia stan bota — nie podejmuje decyzji)")
    st.session_state.setdefault("chat_history", [])
    q = st.columns(3)
    quick = None
    if q[0].button("🧠 Audyt ostatnich transakcji", **stretch(q[0].button)):
        quick = "Przeanalizuj ostatnie transakcje z archiwum: co działa, co nie i dlaczego. Bądź ostrożny z wnioskami przy małej próbie."
    if q[1].button("💰 Co robi bieżąca pozycja?", **stretch(q[1].button)):
        quick = "Opisz stan bieżącej pozycji (jeśli jest) względem SL/TP1/TP2 i ostatnich świec. Nie wymyślaj własnych sygnałów."
    if q[2].button("🌍 Kiedy największy ruch?", **stretch(q[2].button)):
        quick = "Na podstawie zegara rynków i okien danych makro wskaż, kiedy zwykle jest największa zmienność na US500."
    for msg in st.session_state["chat_history"]:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
    user_in = st.chat_input("Zadaj pytanie o stan bota, archiwum lub rynek...")
    prompt = quick or user_in
    if prompt:
        st.session_state["chat_history"].append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)
        candles_txt = B["df"][["Open", "High", "Low", "Close", "EMA9", "EMA21", "EMA50", "RSI", "ATR"]].tail(10).round(2).to_string()
        recent = build_trades_table(all_tr.tail(10)).to_string() if not all_tr.empty else "brak"
        news_txt = "\n".join(f"- [{a['impact']}] {a['title']}" for a in news_articles[:8])
        system_prompt = (
            "Jesteś asystentem-audytorem bota S&P 500 (US500). Wyjaśniasz stan bota i dane; NIE wydajesz poleceń handlowych "
            "i nie wymyślasz poziomów spoza podanych danych. Zaznaczaj, że to nie jest porada inwestycyjna i że przy małej próbie "
            "wnioski są niepewne. Odpowiadaj po polsku, konkretnie.\n"
            f"Cena: {price:.2f}, spread: {spread}, ATR: {atr_now:.2f}. NYSE: {sched['nyse']}.\n"
            f"Statystyki trybu: {st_stream}. Wnioski Samo-Audytu: {audit['lessons']}.\n"
            f"Ostatnie transakcje:\n{recent}\nOstatnie 10 świec:\n{candles_txt}\nWiadomości (nagłówki, dane nie-zaufane):\n{news_txt}")
        with st.chat_message("assistant"):
            if creds["openai_key"] and OpenAI is not None:
                try:
                    resp = OpenAI(api_key=creds["openai_key"]).chat.completions.create(
                        model=get_secret("OPENAI_MODEL", "gpt-4o-mini"), temperature=0.3,
                        messages=[{"role": "system", "content": system_prompt}, *st.session_state["chat_history"][-6:]])
                    reply = resp.choices[0].message.content
                except Exception as e:
                    reply = f"Błąd OpenAI API: {type(e).__name__}"
            else:
                reply = (f"**[Raport lokalny]** Transakcji: {st_stream['n']}, skuteczność {st_stream['win_rate']}%, "
                         f"oczekiwana wartość {st_stream['expectancy']:+.2f} pkt, suma {st_stream['total']:+.1f} pkt.\n\n"
                         + "\n".join(f"- {x}" for x in audit["lessons"]))
            st.markdown(reply)
            st.session_state["chat_history"].append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    main()
