import os
import re
import html
import json
import time
import datetime
from zoneinfo import ZoneInfo
import requests
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit.components.v1 as components
import xml.etree.ElementTree as ET

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import yfinance as yf
except ImportError:
    yf = None

PL_TZ = ZoneInfo("Europe/Warsaw")
NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

st.set_page_config(
    page_title="SP500 SIGNAL AI PRO — Master Edition v4 (Fala 30 PKT + Pełny Audyt)",
    page_icon="⚡",
    layout="wide"
)

KEYS_CACHE_FILE = ".saved_api_keys.json"

def load_cached_keys():
    data = {
        "is_demo": True,
        "cap_key": "",
        "cap_id": "",
        "cap_pass": "",
        "openai_key": "",
        "remember_on_disk": True
    }
    for k, env_name in [
        ("cap_key", "CAPITAL_API_KEY"),
        ("cap_id", "CAPITAL_IDENTIFIER"),
        ("cap_pass", "CAPITAL_PASSWORD"),
        ("openai_key", "OPENAI_API_KEY")
    ]:
        if env_name in os.environ and os.environ[env_name]:
            data[k] = os.environ[env_name]
        else:
            try:
                if env_name in st.secrets and st.secrets[env_name]:
                    data[k] = st.secrets[env_name]
            except Exception:
                pass

    if os.path.exists(KEYS_CACHE_FILE):
        try:
            with open(KEYS_CACHE_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                for k in data:
                    if saved.get(k) is not None and saved.get(k) != "":
                        data[k] = saved[k]
        except Exception:
            pass
    return data

def save_cached_keys(is_demo, cap_key, cap_id, cap_pass, openai_key, remember_on_disk=True):
    payload = {
        "is_demo": is_demo,
        "cap_key": cap_key.strip(),
        "cap_id": cap_id.strip(),
        "cap_pass": cap_pass.strip(),
        "openai_key": openai_key.strip(),
        "remember_on_disk": remember_on_disk
    }
    st.session_state["saved_keys"] = payload
    if remember_on_disk:
        try:
            with open(KEYS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f)
        except Exception:
            pass
    else:
        if os.path.exists(KEYS_CACHE_FILE):
            try:
                os.remove(KEYS_CACHE_FILE)
            except Exception:
                pass

if "saved_keys" not in st.session_state:
    st.session_state["saved_keys"] = load_cached_keys()

def trigger_beep_alarm(signal_type="LONG"):
    freq1 = 880 if signal_type == "LONG" else 520
    freq2 = 1175 if signal_type == "LONG" else 390
    html_code = f"""
    <script>
    (function() {{
        try {{
            const AudioCtx = window.AudioContext || window.webkitAudioContext;
            const ctx = new AudioCtx();
            const playTone = (freq, start, duration) => {{
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'sawtooth';
                osc.frequency.setValueAtTime(freq, ctx.currentTime + start);
                gain.gain.setValueAtTime(0.25, ctx.currentTime + start);
                gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + start + duration);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start(ctx.currentTime + start);
                osc.stop(ctx.currentTime + start + duration);
            }};
            playTone({freq1}, 0.0, 0.18);
            playTone({freq2}, 0.22, 0.18);
            playTone({freq1}, 0.44, 0.30);
        }} catch (e) {{}}
    }})();
    </script>
    """
    components.html(html_code, height=0, width=0)

def clean_html_text(raw_text):
    if not raw_text:
        return ""
    text = html.unescape(str(raw_text))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

@st.cache_data(ttl=120)
def fetch_full_macro_news_and_sentiment():
    feeds = [
        ("Investing Central Banks (FED/BOJ)", "https://www.investing.com/rss/news_14.rss"),
        ("Investing Economy & Macro", "https://www.investing.com/rss/news_95.rss"),
        ("Investing Stock Market", "https://www.investing.com/rss/news_25.rss"),
        ("CNBC US Markets & Economy", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258"),
        ("MarketWatch Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories")
    ]
    articles = []
    feed_status = []
    seen_titles = set()

    bearish_words = [
        "hawkish", "rate hike", "inflation", "selloff", "sell-off", "drop", "falls", "plunge",
        "slump", "recession", "war", "tariff", "fear", "tightening", "yields rise", "lower",
        "decline", "caution", "weak", "deficit", "concerns", "losses", "down", "bearish", "boj"
    ]
    bullish_words = [
        "dovish", "rate cut", "rally", "surge", "gains", "record high", "optimism", "beat",
        "growth", "stimulus", "easing", "bullish", "rebound", "soars", "jumps", "higher",
        "strong", "recovery", "cooling inflation", "soft landing"
    ]

    bull_score = 0
    bear_score = 0

    for source_name, url in feeds:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=5)
            if r.status_code == 200:
                root = ET.fromstring(r.content)
                count_ok = 0
                for item in root.findall(".//item")[:5]:
                    title_el = item.find("title")
                    desc_el = item.find("description")
                    pub_el = item.find("pubDate")
                    title = clean_html_text(title_el.text if title_el is not None else "")
                    desc = clean_html_text(desc_el.text if desc_el is not None else "")
                    pub = clean_html_text(pub_el.text if pub_el is not None else "")

                    if not title or title in seen_titles:
                        continue
                    seen_titles.add(title)
                    count_ok += 1

                    combined_lower = f"{title} {desc}".lower()
                    item_bull = sum(1 for w in bullish_words if w in combined_lower)
                    item_bear = sum(1 for w in bearish_words if w in combined_lower)
                    bull_score += item_bull
                    bear_score += item_bear

                    if item_bear > item_bull:
                        impact = "🔴 Pro-SHORT"
                    elif item_bull > item_bear:
                        impact = "🟢 Pro-LONG"
                    else:
                        impact = "⚪ Neutralny"

                    articles.append({
                        "source": source_name,
                        "title": title,
                        "description": desc[:320] + ("..." if len(desc) > 320 else "") if desc else "Brak rozszerzonego opisu w kanale RSS.",
                        "pubDate": pub,
                        "impact": impact
                    })
                feed_status.append(f"✅ {source_name}: OK ({count_ok} art.)")
            else:
                feed_status.append(f"❌ {source_name}: HTTP {r.status_code}")
        except Exception as e:
            feed_status.append(f"⚠️ {source_name}: Błąd sieci ({type(e).__name__})")

    if bear_score >= bull_score + 2:
        overall = "🔴 BARDZIEJ NA SHORT (Przewaga negatywnych/jastrzębich informacji z rynków)"
        bias_code = -1
    elif bull_score >= bear_score + 2:
        overall = "🟢 BARDZIEJ NA LONG (Przewaga pozytywnych/gołębich informacji z rynków)"
        bias_code = 1
    else:
        overall = "⚪ NEUTRALNY / MIESZANY (Brak jednostronnego uderzenia w wiadomościach — graj czysto według wykresu)"
        bias_code = 0

    return articles[:15], feed_status, overall, bias_code, bull_score, bear_score

def check_us_macro_window():
    now_ny = datetime.datetime.now(NY_TZ)
    if now_ny.weekday() >= 5:
        return False, "Weekend"
    hm = now_ny.hour * 60 + now_ny.minute
    windows = [
        (8 * 60 + 25, 8 * 60 + 40, "Publikacja danych makro USA (08:30 ET)"),
        (9 * 60 + 28, 9 * 60 + 38, "Otwarcie giełdy w Nowym Jorku NYSE (09:30 ET)"),
        (9 * 60 + 57, 10 * 60 + 8, "Dane z gospodarki USA (10:00 ET)"),
        (13 * 60 + 55, 14 * 60 + 15, "Okno komunikatu FED / FOMC (14:00 ET)")
    ]
    for start_m, end_m, label in windows:
        if start_m <= hm <= end_m:
            return True, label
    return False, f"Czas NY: {now_ny.strftime('%H:%M')} ET (Brak blokady danych)"

class CapitalComClient:
    def __init__(self, api_key, identifier, password, is_demo=True):
        self.base_url = "https://demo-api-capital.backend-capital.com" if is_demo else "https://api-capital.backend-capital.com"
        self.api_key = api_key
        self.identifier = identifier
        self.password = password

    def get_session_tokens(self, force_new=False):
        if not self.api_key or not self.identifier or not self.password:
            return None, None, "Brak wpisanych kluczy Capital.com"

        cache = st.session_state.get("cap_session_cache", {})
        now_ts = time.time()
        if (not force_new and cache.get("cst") and cache.get("token")
                and cache.get("base_url") == self.base_url
                and (now_ts - cache.get("ts", 0)) < 480):
            return cache["cst"], cache["token"], "✅ Sesja aktywna (Cache)"

        url = f"{self.base_url}/api/v1/session"
        headers = {"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"}
        payload = {"identifier": self.identifier, "password": self.password, "encryptedPassword": False}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=8)
            if r.status_code == 200:
                cst = r.headers.get("CST")
                token = r.headers.get("X-SECURITY-TOKEN")
                st.session_state["cap_session_cache"] = {
                    "cst": cst,
                    "token": token,
                    "base_url": self.base_url,
                    "ts": now_ts
                }
                return cst, token, "✅ Zalogowano do Capital.com"
            return None, None, f"❌ Błąd logowania Capital.com ({r.status_code})"
        except Exception as e:
            return None, None, f"❌ Błąd sieci Capital.com: {e}"

    def fetch_candles(self, epic="US500", resolution="MINUTE", max_bars=180):
        cst, token, msg = self.get_session_tokens(force_new=False)
        if not cst or not token:
            return pd.DataFrame(), None, msg

        url = f"{self.base_url}/api/v1/prices/{epic}"
        headers = {"X-CAP-API-KEY": self.api_key, "CST": cst, "X-SECURITY-TOKEN": token}
        params = {"resolution": resolution, "max": max_bars}

        try:
            r = requests.get(url, headers=headers, params=params, timeout=8)
            if r.status_code == 401:
                cst, token, msg = self.get_session_tokens(force_new=True)
                if not cst:
                    return pd.DataFrame(), None, msg
                headers = {"X-CAP-API-KEY": self.api_key, "CST": cst, "X-SECURITY-TOKEN": token}
                r = requests.get(url, headers=headers, params=params, timeout=8)

            if r.status_code != 200:
                return pd.DataFrame(), None, f"Błąd pobierania świec ({r.status_code})"

            data = r.json()
            prices = data.get("prices", [])
            if not prices:
                return pd.DataFrame(), None, "Brak świec z Capital.com"

            rows = []
            last_spread = 0.0
            for p in prices:
                bid_c = p["closePrice"]["bid"]
                ask_c = p["closePrice"]["ask"]
                last_spread = round(ask_c - bid_c, 2)
                t_utc = pd.to_datetime(p["snapshotTimeUTC"], utc=True)
                t_pl = t_utc.tz_convert(PL_TZ).tz_localize(None)
                rows.append({
                    "Time": t_pl,
                    "Open": (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2.0,
                    "High": (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2.0,
                    "Low": (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2.0,
                    "Close": (bid_c + ask_c) / 2.0,
                    "Volume": p.get("lastTradedVolume", 0)
                })
            df = pd.DataFrame(rows)
            df.set_index("Time", inplace=True)
            return df, last_spread, "🟢 NA ŻYWO: Capital.com API (Jedna sesja)"
        except Exception as e:
            return pd.DataFrame(), None, f"Wyjątek API: {e}"

def fetch_fallback_data(resolution="MINUTE"):
    if yf is None:
        return pd.DataFrame(), 0.5, "❌ Brak biblioteki yfinance"
    tf_map = {"MINUTE": "1m", "MINUTE_5": "5m", "MINUTE_15": "15m", "HOUR": "1h"}
    interval = tf_map.get(resolution, "5m")
    period = "2d" if interval == "1m" else "5d"
    try:
        df = yf.download("^GSPC", period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.dropna(inplace=True)
        idx = pd.to_datetime(df.index)
        if idx.tz is None:
            idx = idx.tz_localize(NY_TZ)
        df.index = idx.tz_convert(PL_TZ).tz_localize(None)
        return df.tail(180), 0.6, "🟡 TRYB AWARYJNY: Yahoo Finance (Sprawdź klucze Capital.com)"
    except Exception as e:
        return pd.DataFrame(), 0.6, f"Błąd fallbacku: {e}"

def detect_signals_and_waves(df_main, df_htf=None, trade_style="🔥 FALA 30 PKT (Potrzymaj pozycję)", sensitivity="Tylko Pewniaki (Filtr Konsolidacji)", spread_pts=0.6):
    df = df_main.copy().sort_index()
    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()

    delta = df["Close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = (100 - (100 / (1 + rs))).fillna(50)

    high_low = df["High"] - df["Low"]
    high_close = np.abs(df["High"] - df["Close"].shift())
    low_close = np.abs(df["Low"] - df["Close"].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean().bfill()

    # Punkt-w-czasie trend wyższego interwału (BEZ PODGLĄDANIA PRZYSZŁOŚCI!)
    if df_htf is not None and not df_htf.empty and len(df_htf) > 15:
        htf = df_htf.copy().sort_index()
        htf["HTF_EMA9"] = htf["Close"].ewm(span=9, adjust=False).mean()
        htf["HTF_EMA21"] = htf["Close"].ewm(span=21, adjust=False).mean()
        htf["HTF_Bias"] = np.where(htf["HTF_EMA9"] > htf["HTF_EMA21"], 1, np.where(htf["HTF_EMA9"] < htf["HTF_EMA21"], -1, 0))
        merged = pd.merge_asof(
            df[["Close"]],
            htf[["HTF_Bias"]],
            left_index=True,
            right_index=True,
            direction="backward"
        )
        df["HTF_Bias"] = merged["HTF_Bias"].fillna(0).astype(int)
    else:
        df["HTF_Bias"] = 0

    current_htf_bias = int(df["HTF_Bias"].iloc[-1]) if not df.empty else 0

    df["Signal"] = 0
    df["Pattern"] = ""
    df["ArrowPrice"] = np.nan
    df["EntryPrice"] = np.nan
    df["SL"] = np.nan
    df["TP1"] = np.nan
    df["TP"] = np.nan

    is_wave_30 = "30 PKT" in trade_style
    wick_ratio = 1.5 if sensitivity == "Wysoka (Więcej strzałek)" else 1.85
    min_cooldown = 6 if is_wave_30 else (2 if sensitivity == "Wysoka (Więcej strzałek)" else 4)
    last_sig_idx = -20

    # Pętla analizuje wyłącznie ZAMKNIĘTE świece (do len(df) - 1)
    for i in range(5, len(df) - 1):
        if (i - last_sig_idx) < min_cooldown:
            continue

        o, h, l, c = df["Open"].iloc[i], df["High"].iloc[i], df["Low"].iloc[i], df["Close"].iloc[i]
        po, ph, pl, pc = df["Open"].iloc[i - 1], df["High"].iloc[i - 1], df["Low"].iloc[i - 1], df["Close"].iloc[i - 1]
        ema9, ema21, ema50 = df["EMA9"].iloc[i], df["EMA21"].iloc[i], df["EMA50"].iloc[i]
        ema9_prev3 = df["EMA9"].iloc[i - 3]
        rsi = df["RSI"].iloc[i]
        atr = max(df["ATR"].iloc[i], 0.5)
        bar_htf_bias = int(df["HTF_Bias"].iloc[i])

        body = abs(c - o)
        rng = max(h - l, 0.01)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        # Filtr Anty-Konsolidacyjny
        ema_gap = abs(ema9 - ema21)
        ema_slope = ema9 - ema9_prev3
        is_chop = (ema_gap < atr * 0.12) and (abs(ema_slope) < atr * 0.18)

        if sensitivity != "Wysoka (Więcej strzałek)" and is_chop:
            continue

        close_near_high = ((c - l) / rng) >= 0.60
        close_near_low = ((h - c) / rng) >= 0.60

        bull_pinbar = (lower_wick >= body * wick_ratio) and (lower_wick >= rng * 0.45) and close_near_high
        bull_engulfing = (pc < po) and (c > o) and (c > po) and (body >= atr * 0.35)
        bull_impulse = (c > o) and (body > atr * 0.80) and (c > ema9) and (po <= ema9)
        bull_wave_break = (c > o) and (c > ema9) and (ema9 > ema21) and (df["EMA9"].iloc[i - 2] <= df["EMA21"].iloc[i - 2])

        bear_pinbar = (upper_wick >= body * wick_ratio) and (upper_wick >= rng * 0.45) and close_near_low
        bear_engulfing = (pc > po) and (c < o) and (c < po) and (body >= atr * 0.35)
        bear_impulse = (c < o) and (body > atr * 0.80) and (c < ema9) and (po >= ema9)
        bear_wave_break = (c < o) and (c < ema9) and (ema9 < ema21) and (df["EMA9"].iloc[i - 2] >= df["EMA21"].iloc[i - 2])

        trend_up = (ema9 > ema21 and ema_slope >= 0) or (rsi < 33 and close_near_high)
        trend_down = (ema9 < ema21 and ema_slope <= 0) or (rsi > 67 and close_near_low)

        if sensitivity == "Tylko Pewniaki (Filtr Konsolidacji)" and bar_htf_bias != 0:
            trend_up = trend_up and (bar_htf_bias == 1 or rsi < 30)
            trend_down = trend_down and (bar_htf_bias == -1 or rsi > 70)

        next_open = float(df["Open"].iloc[i + 1])
        half_spread = float(spread_pts or 0.6) / 2.0

        if (bull_pinbar or bull_engulfing or bull_impulse or bull_wave_break) and trend_up and rsi < 70:
            df.iloc[i, df.columns.get_loc("Signal")] = 1
            df.iloc[i, df.columns.get_loc("ArrowPrice")] = l - (atr * 0.40)
            real_entry = round(next_open + half_spread, 2)
            df.iloc[i, df.columns.get_loc("EntryPrice")] = real_entry

            if is_wave_30:
                swing_low = float(df["Low"].iloc[max(0, i - 5):i + 1].min())
                sl_dist = np.clip(max(real_entry - swing_low + atr, atr * 3.5), 8.0, 14.0)
                df.iloc[i, df.columns.get_loc("SL")] = round(real_entry - sl_dist, 2)
                df.iloc[i, df.columns.get_loc("TP1")] = round(real_entry + 15.0, 2)
                df.iloc[i, df.columns.get_loc("TP")] = round(real_entry + 30.0, 2)
                df.iloc[i, df.columns.get_loc("Pattern")] = "🔥 FALA 30 PKT LONG"
            else:
                df.iloc[i, df.columns.get_loc("SL")] = round(min(l, real_entry - atr) - (atr * 0.35), 2)
                df.iloc[i, df.columns.get_loc("TP1")] = round(real_entry + (atr * 1.2), 2)
                df.iloc[i, df.columns.get_loc("TP")] = round(real_entry + (atr * 2.0), 2)
                df.iloc[i, df.columns.get_loc("Pattern")] = "Objęcie (LONG)" if bull_engulfing else ("Pinbar (LONG)" if bull_pinbar else "Impuls (LONG)")
            last_sig_idx = i

        elif (bear_pinbar or bear_engulfing or bear_impulse or bear_wave_break) and trend_down and rsi > 30:
            df.iloc[i, df.columns.get_loc("Signal")] = -1
            df.iloc[i, df.columns.get_loc("ArrowPrice")] = h + (atr * 0.40)
            real_entry = round(next_open - half_spread, 2)
            df.iloc[i, df.columns.get_loc("EntryPrice")] = real_entry

            if is_wave_30:
                swing_high = float(df["High"].iloc[max(0, i - 5):i + 1].max())
                sl_dist = np.clip(max(swing_high - real_entry + atr, atr * 3.5), 8.0, 14.0)
                df.iloc[i, df.columns.get_loc("SL")] = round(real_entry + sl_dist, 2)
                df.iloc[i, df.columns.get_loc("TP1")] = round(real_entry - 15.0, 2)
                df.iloc[i, df.columns.get_loc("TP")] = round(real_entry - 30.0, 2)
                df.iloc[i, df.columns.get_loc("Pattern")] = "🔥 ZJAZD 30 PKT SHORT"
            else:
                df.iloc[i, df.columns.get_loc("SL")] = round(max(h, real_entry + atr) + (atr * 0.35), 2)
                df.iloc[i, df.columns.get_loc("TP1")] = round(real_entry - (atr * 1.2), 2)
                df.iloc[i, df.columns.get_loc("TP")] = round(real_entry - (atr * 2.0), 2)
                df.iloc[i, df.columns.get_loc("Pattern")] = "Objęcie (SHORT)" if bear_engulfing else ("Pinbar (SHORT)" if bear_pinbar else "Impuls (SHORT)")
            last_sig_idx = i

    return df, current_htf_bias

def evaluate_signals_history(df):
    results = []
    for i in range(len(df) - 1):
        sig = int(df["Signal"].iloc[i])
        if sig == 0:
            continue
        sig_time = str(df.index[i].strftime("%H:%M"))
        entry_time = str(df.index[i + 1].strftime("%H:%M"))
        entry = float(df["EntryPrice"].iloc[i])
        sl = float(df["SL"].iloc[i])
        tp1 = float(df["TP1"].iloc[i])
        tp = float(df["TP"].iloc[i])
        pat = df["Pattern"].iloc[i]

        status = "⏳ W GRZE (Czeka na ruch)"
        pnl = 0.0
        max_fav = 0.0
        hit_tp1 = False

        for j in range(i + 1, len(df)):
            hj = float(df["High"].iloc[j])
            lj = float(df["Low"].iloc[j])
            if sig == 1:
                fav = hj - entry
                if fav > max_fav:
                    max_fav = fav
                if hj >= tp1:
                    hit_tp1 = True
                if lj <= sl and hj >= tp:
                    status = "❌ STOP LOSS (Konflikt w 1 świecy)"
                    pnl = round(sl - entry, 2)
                    break
                elif lj <= sl:
                    if hit_tp1:
                        status = "🟢 +15 PKT (Zabezpieczone TP1)"
                        pnl = round(tp1 - entry, 2)
                    else:
                        status = "❌ STOP LOSS"
                        pnl = round(sl - entry, 2)
                    break
                elif hj >= tp:
                    status = "✅ PEŁNE TP (+30 PKT!)" if abs(tp - entry) >= 25 else "✅ WESZŁO (TP)"
                    pnl = round(tp - entry, 2)
                    break
            else:
                fav = entry - lj
                if fav > max_fav:
                    max_fav = fav
                if lj <= tp1:
                    hit_tp1 = True
                if hj >= sl and lj <= tp:
                    status = "❌ STOP LOSS (Konflikt w 1 świecy)"
                    pnl = round(entry - sl, 2)
                    break
                elif hj >= sl:
                    if hit_tp1:
                        status = "🟢 +15 PKT (Zabezpieczone TP1)"
                        pnl = round(entry - tp1, 2)
                    else:
                        status = "❌ STOP LOSS"
                        pnl = round(entry - sl, 2)
                    break
                elif lj <= tp:
                    status = "✅ PEŁNE TP (-30 PKT ZJAZD!)" if abs(entry - tp) >= 25 else "✅ WESZŁO (TP)"
                    pnl = round(entry - tp, 2)
                    break

        if status.startswith("⏳"):
            cur_c = float(df["Close"].iloc[-1])
            pnl = round((cur_c - entry) if sig == 1 else (entry - cur_c), 2)

        results.append({
            "Świeca sygnału": sig_time,
            "Wejście o godz.": entry_time,
            "Kierunek": "🟢 LONG" if sig == 1 else "🔴 SHORT",
            "Typ Sygnału": pat,
            "Wejście (ze spreadem)": round(entry, 2),
            "Stop Loss": sl,
            "Cel 1 (TP1)": tp1,
            "Cel Główny (TP2)": tp,
            "Maks. ruch w dobrą stronę": f"+{max_fav:.1f} pkt",
            "Wynik": status,
            "Punkty (PnL)": pnl
        })
    return pd.DataFrame(results)

saved = st.session_state["saved_keys"]

with st.sidebar:
    st.title("⚡ Master Edition v4")
    st.caption("Fala 30 PKT • Pełne Newsy • Audyt v3+v4")

    with st.expander("🔑 Klucze API (Laptop)", expanded=not bool(saved.get("cap_key"))):
        with st.form("api_login_form", clear_on_submit=False):
            is_demo_in = st.checkbox("Konto DEMO Capital.com", value=saved.get("is_demo", True))
            cap_key_in = st.text_input("Capital API Key:", value=saved.get("cap_key", ""), type="password")
            cap_id_in = st.text_input("Capital Login (Email):", value=saved.get("cap_id", ""))
            cap_pass_in = st.text_input("Capital Hasło API:", value=saved.get("cap_pass", ""), type="password")
            openai_key_in = st.text_input("OpenAI API Key (do czatu):", value=saved.get("openai_key", ""), type="password")
            remember_in = st.checkbox("💾 Zapamiętaj klucze na tym laptopie", value=saved.get("remember_on_disk", True))

            login_submitted = st.form_submit_button("🔐 ZALOGUJ I ZAPISZ", use_container_width=True, type="primary")

        if login_submitted:
            st.session_state.pop("cap_session_cache", None)
            save_cached_keys(is_demo_in, cap_key_in, cap_id_in, cap_pass_in, openai_key_in, remember_on_disk=remember_in)
            st.success("Zapisano klucze! Odświeżam połączenie...")
            st.rerun()

        if saved.get("cap_key") and saved.get("cap_id"):
            st.caption(f"📌 Zalogowany: `{saved.get('cap_id')}`")
            if st.button("🗑️ Wyloguj / Wyczyść klucze", use_container_width=True):
                st.session_state.pop("cap_session_cache", None)
                save_cached_keys(True, "", "", "", "", remember_on_disk=False)
                st.rerun()

    st.markdown("---")
    st.subheader("1. Styl Gry (Dla Koleżanki)")
    trade_style = st.radio(
        "Wybierz cel zysku na pozycję:",
        [
            "🔥 FALA 30 PKT (Potrzymaj pozycję — Cel 15 / 30 pkt)",
            "⚡ Szybki Scalp (Krótkie ruchy 2–4 pkt wg ATR)"
        ],
        index=0,
        help="W trybie FALA 30 PKT bot szuka mocnych ruchów i rysuje na wykresie poziomy +15 pkt (TP1) oraz +30 pkt (TP2)."
    )
    resolution = st.selectbox(
        "Interwał wykresu:",
        ["MINUTE", "MINUTE_5", "MINUTE_15"],
        index=0,
        help="Do łapania fali 30 pkt świetny jest MINUTE (widać wczesny start) lub MINUTE_5 (mniej szumu)."
    )
    sensitivity = st.radio(
        "Filtr strzałek:",
        ["Tylko Pewniaki (Filtr Konsolidacji)", "Standard (Z filtrem płaskich EMA)", "Wysoka (Więcej strzałek)"],
        index=0
    )
    max_spread = st.slider("Maks. Spread (pkt):", 0.2, 2.5, 0.9, 0.1)
    block_macro_window = st.checkbox("🛑 Ostrzegaj w oknach publikacji danych USA (08:30/10:00/14:00 ET)", value=True)

    st.subheader("2. Dźwięk i Odświeżanie")
    sound_enabled = st.toggle("🔊 Piszczenie przy nowym sygnale", value=True)
    if st.button("🔊 Testuj Dźwięk", use_container_width=True):
        trigger_beep_alarm("LONG")
        st.toast("Test głośnika!")

    auto_refresh = st.toggle("🔄 Auto-odświeżanie na żywo", value=True)
    refresh_sec = st.slider("Co ile sekund:", 5, 30, 5)

st.title("🎯 SP500 SIGNAL AI PRO — Łowca Fal 30 PKT & Pełna Analiza Newsów")

client = CapitalComClient(
    saved.get("cap_key", ""),
    saved.get("cap_id", ""),
    saved.get("cap_pass", ""),
    is_demo=saved.get("is_demo", True)
)
df_raw, current_spread, source_status = client.fetch_candles(epic="US500", resolution=resolution, max_bars=180)

htf_map = {"MINUTE": "MINUTE_5", "MINUTE_5": "MINUTE_15", "MINUTE_15": "HOUR"}
htf_res = htf_map.get(resolution, "MINUTE_15")
df_htf, _, _ = client.fetch_candles(epic="US500", resolution=htf_res, max_bars=90)

if df_raw.empty:
    df_raw, current_spread, source_status = fetch_fallback_data(resolution)
    df_htf, _, _ = fetch_fallback_data(htf_res)

if df_raw.empty or len(df_raw) < 25:
    st.error(f"Nie udało się pobrać świec: {source_status}")
    st.stop()

df, htf_bias = detect_signals_and_waves(
    df_raw, df_htf, trade_style=trade_style, sensitivity=sensitivity, spread_pts=current_spread or 0.6
)
eval_df = evaluate_signals_history(df)
articles, feed_status, news_sentiment_str, news_bias_code, bull_sc, bear_sc = fetch_full_macro_news_and_sentiment()
in_macro_window, macro_window_label = check_us_macro_window()

closed_candle = df.iloc[-2]
live_candle = df.iloc[-1]
current_price = float(live_candle["Close"])
current_atr = float(closed_candle["ATR"])
current_rsi = float(closed_candle["RSI"])

recent_window = df.tail(60)
recent_high = float(recent_window["High"].max())
recent_low = float(recent_window["Low"].min())
drop_from_high = round(recent_high - current_price, 1)
rally_from_low = round(current_price - recent_low, 1)

if news_bias_code == -1:
    st.error(f"📰 **SENTYMENT WIADOMOŚCI (Pełne opisy RSS):** {news_sentiment_str} *(Wynik analizy treści: {bear_sc} negatywnych vs {bull_sc} pozytywnych)*")
elif news_bias_code == 1:
    st.success(f"📰 **SENTYMENT WIADOMOŚCI (Pełne opisy RSS):** {news_sentiment_str} *(Wynik analizy treści: {bull_sc} pozytywnych vs {bear_sc} negatywnych)*")
else:
    st.info(f"📰 **SENTYMENT WIADOMOŚCI (Pełne opisy RSS):** {news_sentiment_str} *(Wynik analizy treści: {bull_sc} pozytywnych vs {bear_sc} negatywnych)*")

if block_macro_window and in_macro_window:
    st.warning(f"⚠️ **UWAGA — OKNO DANYCH USA:** Trwa `{macro_window_label}`! Możliwe gwałtowne szarpnięcia o 20–40 punktów w obie strony.")

active_sig = int(closed_candle["Signal"])
active_pattern = closed_candle["Pattern"]
sig_time = str(df.index[-2])
spread_ok = (current_spread is None) or (current_spread <= max_spread)

live_invalidated = False
if active_sig == 1 and float(live_candle["Low"]) <= float(closed_candle["SL"]):
    live_invalidated = True
elif active_sig == -1 and float(live_candle["High"]) >= float(closed_candle["SL"]):
    live_invalidated = True

if "last_beep_candle" not in st.session_state:
    st.session_state.last_beep_candle = None

if active_sig != 0 and spread_ok and not live_invalidated:
    if st.session_state.last_beep_candle != sig_time:
        if sound_enabled:
            trigger_beep_alarm("LONG" if active_sig == 1 else "SHORT")
        st.session_state.last_beep_candle = sig_time

wins = len(eval_df[eval_df["Wynik"].str.startswith("✅") | eval_df["Wynik"].str.startswith("🟢")]) if not eval_df.empty else 0
losses = len(eval_df[eval_df["Wynik"].str.startswith("❌")]) if not eval_df.empty else 0
total_closed = wins + losses
win_rate = round((wins / total_closed) * 100, 1) if total_closed > 0 else 0.0
total_pts = round(eval_df["Punkty (PnL)"].sum(), 2) if not eval_df.empty else 0.0

tab_main, tab_stats, tab_news = st.tabs([
    "📊 Wykres & Cel 30 Punktów",
    f"🏆 Tabela Wyników ({wins}W / {losses}L — {win_rate}%)",
    f"📰 Pełne Wiadomości & Status Źródeł ({len(articles)})"
])

with tab_main:
    if active_sig == 1 and spread_ok and not live_invalidated:
        sl_p, tp1_p, tp2_p = closed_candle["SL"], closed_candle["TP1"], closed_candle["TP"]
        st.success(
            f"### 🟢 SYGNAŁ NA WZROST: {active_pattern} — WCHODZISZ TERAZ W LONG!\n"
            f"Świeca `{df.index[-2].strftime('%H:%M')}` zamknęła się sygnałem! **Wchodzisz po:** `{current_price:.2f}` | "
            f"**Stop Loss:** `{sl_p:.2f}` | **Przystanek TP1 (+15 pkt):** `{tp1_p:.2f}` | **Cel Główny TP2 (+30 pkt):** `{tp2_p:.2f}`"
        )
    elif active_sig == -1 and spread_ok and not live_invalidated:
        sl_p, tp1_p, tp2_p = closed_candle["SL"], closed_candle["TP1"], closed_candle["TP"]
        st.error(
            f"### 🔴 SYGNAŁ NA SPADEK: {active_pattern} — WCHODZISZ TERAZ W SHORT!\n"
            f"Świeca `{df.index[-2].strftime('%H:%M')}` zamknęła się sygnałem! **Wchodzisz po:** `{current_price:.2f}` | "
            f"**Stop Loss:** `{sl_p:.2f}` | **Przystanek TP1 (-15 pkt):** `{tp1_p:.2f}` | **Cel Główny TP2 (-30 pkt ZJAZD):** `{tp2_p:.2f}`"
        )
    else:
        if not eval_df.empty:
            last_row = eval_df.iloc[-1]
            st.info(
                f"⏱️ **STATUS OSTATNIEJ POZYCJI (ze świecy `{last_row['Świeca sygnału']}`):** "
                f"**{last_row['Kierunek']}** od ceny `{last_row['Wejście (ze spreadem)']}` ➔ "
                f"Cel TP1: `{last_row['Cel 1 (TP1)']}` | **Cel Główny (30 pkt): `{last_row['Cel Główny (TP2)']}`** ➔ "
                f"Status: **{last_row['Wynik']}** (Bieżący wynik: `{last_row['Punkty (PnL)']:+.2f} pkt`, Maks. ruch: `{last_row['Maks. ruch w dobrą stronę']}`)"
            )
        else:
            st.info("⚪ **CZEKAJ NA SYGNAŁ FALI** — Rynek w konsolidacji, filtr chroni przed wejściem w płaski szum.")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Cena US500", f"{current_price:.2f}")
    c2.metric("Zjazd od szczytu (60 św.)", f"-{drop_from_high:.1f} pkt", delta=f"Brakuje {max(0.0, 30.0 - drop_from_high):.1f} pkt do -30" if drop_from_high < 30 else "🔥 ZJAZD 30 PKT ZROBIONY!")
    c3.metric("Odbicie od dołka (60 św.)", f"+{rally_from_low:.1f} pkt", delta=f"Brakuje {max(0.0, 30.0 - rally_from_low):.1f} pkt do +30" if rally_from_low < 30 else "🔥 RAKIETA 30 PKT ZROBIONA!")
    c4.metric("Spread / ATR", f"{current_spread or 0.0:.2f} / {current_atr:.2f}")
    c5.metric(f"Trend wyższy ({htf_res})", "🟢 WZROSTOWY" if htf_bias > 0 else ("🔴 SPADKOWY" if htf_bias < 0 else "⚪ PŁASKI"))
    c6.metric("Skuteczność (Wykres)", f"{win_rate}% ({wins}W/{losses}L)", delta=f"{total_pts:+.2f} pkt")

    st.caption(f"📡 Źródło danych: **{source_status}** | 🕒 Czas lokalny PL: `{datetime.datetime.now(PL_TZ).strftime('%H:%M:%S')}` | 🗽 {macro_window_label}")

    df_plot = df.tail(95)
    long_s = df_plot[df_plot["Signal"] == 1]
    short_s = df_plot[df_plot["Signal"] == -1]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df_plot.index, open=df_plot["Open"], high=df_plot["High"],
        low=df_plot["Low"], close=df_plot["Close"], name="US500",
        increasing_line_color="#00e676", decreasing_line_color="#ff1744"
    ))
    fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["EMA9"], line=dict(color="#ffea00", width=1.3), name="EMA 9"))
    fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["EMA21"], line=dict(color="#2979ff", width=1.6), name="EMA 21"))
    fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["EMA50"], line=dict(color="#ab47bc", width=1.2, dash="dot"), name="EMA 50 (Fala)"))

    if not long_s.empty:
        fig.add_trace(go.Scatter(
            x=long_s.index, y=long_s["ArrowPrice"], mode="markers+text",
            marker=dict(symbol="triangle-up", size=18, color="#00e676", line=dict(color="white", width=1.2)),
            text=["▲ LONG" for _ in range(len(long_s))], textposition="bottom center",
            name="Sygnał LONG"
        ))
    if not short_s.empty:
        fig.add_trace(go.Scatter(
            x=short_s.index, y=short_s["ArrowPrice"], mode="markers+text",
            marker=dict(symbol="triangle-down", size=18, color="#ff1744", line=dict(color="white", width=1.2)),
            text=["▼ SHORT" for _ in range(len(short_s))], textposition="top center",
            name="Sygnał SHORT"
        ))

    if not eval_df.empty:
        last_sig_row = eval_df.iloc[-1]
        entry_lvl = float(last_sig_row["Wejście (ze spreadem)"])
        sl_lvl = float(last_sig_row["Stop Loss"])
        tp1_lvl = float(last_sig_row["Cel 1 (TP1)"])
        tp2_lvl = float(last_sig_row["Cel Główny (TP2)"])
        fig.add_hline(y=entry_lvl, line_dash="dash", line_color="#ffffff", annotation_text=f"Wejście: {entry_lvl:.2f}", annotation_position="right")
        fig.add_hline(y=sl_lvl, line_dash="dot", line_color="#ff1744", annotation_text=f"SL: {sl_lvl:.2f}", annotation_position="right")
        fig.add_hline(y=tp1_lvl, line_dash="dot", line_color="#00e676", annotation_text=f"TP1 (15 pkt): {tp1_lvl:.2f}", annotation_position="right")
        fig.add_hline(y=tp2_lvl, line_dash="solid", line_color="#00c853", annotation_text=f"🎯 CEL 30 PKT (TP2): {tp2_lvl:.2f}", annotation_position="right")

    fig.update_layout(height=540, template="plotly_dark", xaxis_rangeslider_visible=False, margin=dict(l=15, r=85, t=35, b=15))
    st.plotly_chart(fig, use_container_width=True)

with tab_stats:
    st.subheader("🏆 Uczciwa Tabela Wyników (Uwzględnia Spread i Cel 15 / 30 PKT)")
    st.caption("Wejście liczone na otwarciu kolejnej świecy + połowa spreadu. Jeśli pozycja dojdzie do +15 pkt (TP1), zysk jest zabezpieczony!")
    if not eval_df.empty:
        st.dataframe(eval_df.iloc[::-1], use_container_width=True)
    else:
        st.info("Brak sygnałów na pobranych świecach.")

with tab_news:
    st.subheader("📡 Status kanałów informacyjnych RSS")
    st.write(" | ".join(feed_status))
    st.markdown("---")
    st.subheader("📰 Pełne wiadomości i opisy artykułów (Nie tylko nagłówki!)")
    for art in articles:
        with st.container():
            st.markdown(f"**{art['impact']} | [{art['source']}] {art['title']}** *(Publikacja: {art['pubDate']})*")
            st.caption(f"📄 **Treść / Opis:** {art['description']}")

st.markdown("---")
st.subheader("🤖 Czat z Analitykiem AI (Widzi ostatnich 10 świec, pełne opisy newsów i falę 30 pkt)")

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

q1, q2, q3 = st.columns(3)
quick_p = None
if q1.button("🔥 Czy szykuje się zjazd/wzrost o 30 punktów?", use_container_width=True):
    quick_p = "Przeanalizuj ostatnie świece, średnie EMA9/21/50 oraz pełne opisy wiadomości. Czy jest szansa na ruch o 15-30 punktów i w którą stronę (SHORT czy LONG) koleżanka ma trzymać pozycję?"
if q2.button("📰 Podsumuj pełne artykuły (LONG czy SHORT?)", use_container_width=True):
    quick_p = "Przeczytaj dokładnie tytuły ORAZ pełne opisy artykułów makro/FED/BOJ/Tech. Która strona ma przewagę: SHORT czy LONG na S&P 500?"
if q3.button("🛑 Gdzie ustawić bezpieczny Stop Loss?", use_container_width=True):
    quick_p = "Chcemy potrzymać pozycję dłużej pod ruch 20-30 punktów. Gdzie ustawić Stop Loss, żeby zwykły szum nas nie wyrzucił?"

for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_in = st.chat_input("Zadaj pytanie (np. 'trzymać tego shorta na 30 punktów czy zamykać?')...")
final_prompt = quick_p or user_in

if final_prompt:
    st.session_state.chat_history.append({"role": "user", "content": final_prompt})
    with st.chat_message("user"):
        st.markdown(final_prompt)

    candles_txt = df[["Open", "High", "Low", "Close", "EMA9", "EMA21", "EMA50", "RSI", "ATR", "Signal"]].tail(10).to_string()
    news_full_txt = "\n".join([f"- [{a['impact']}] {a['title']} -> Opis: {a['description']}" for a in articles[:8]])
    recent_eval_txt = eval_df.tail(5).to_string() if not eval_df.empty else "Brak"

    system_prompt = f"""
    Jesteś głównym analitykiem S&P 500 (US500) pomagającym w handlu na ruchy 15–30 punktów (swing-scalping).
    Aktualna cena: {current_price:.2f}, Spread: {current_spread} pkt, ATR: {current_atr:.2f}, RSI: {current_rsi:.1f}.
    Trend wyższego interwału ({htf_res}): {"WZROSTOWY" if htf_bias > 0 else ("SPADKOWY" if htf_bias < 0 else "PŁASKI")}.
    Zjazd od ostatniego szczytu (60 świec): -{drop_from_high:.1f} pkt | Odbicie od dołka: +{rally_from_low:.1f} pkt.
    Sentyment wiadomości: {news_sentiment_str}.
    Ostatnie 10 świec na wykresie:
    {candles_txt}
    Ostatnie 5 sygnałów z tabeli wyników:
    {recent_eval_txt}
    Pełne nagłówki ORAZ OPISY artykułów makro/rynkowych:
    {news_full_txt}
    Odpowiadaj konkretnie po polsku:
    1. Jednoznaczny kierunek (SHORT czy LONG lub CZEKAJ).
    2. Czy jest paliwo na ruch 15-30 punktów (na bazie świec i pełnych opisów artykułów).
    3. Konkretne poziomy: Wejście, Stop Loss, TP1 (15 pkt) i TP2 (30 pkt).
    """

    with st.chat_message("assistant"):
        openai_key = saved.get("openai_key", "")
        if openai_key and OpenAI is not None:
            try:
                ai_client = OpenAI(api_key=openai_key)
                resp = ai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{"role": "system", "content": system_prompt}, *st.session_state.chat_history[-6:]],
                    temperature=0.3
                )
                reply = resp.choices[0].message.content
            except Exception as e:
                reply = f"Błąd OpenAI API: {e}"
        else:
            reply = (
                f"**[Analiza Lokalna — Łowca Fal 30 PKT]**\n"
                f"- **Sentyment z pełnych opisów wiadomości:** {news_sentiment_str}\n"
                f"- **Ruch falowy (ostatnie 60 świec):** Zjazd od szczytu wynosi `-{drop_from_high:.1f} pkt`, a odbicie od dołka `+{rally_from_low:.1f} pkt`.\n"
                f"- **Wskazówka:** *(Wpisz klucz OpenAI w panelu bocznym, aby odblokować pełną analizę AI).* "
            )
        st.markdown(reply)
        st.session_state.chat_history.append({"role": "assistant", "content": reply})

if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()
