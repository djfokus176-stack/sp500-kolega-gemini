import os
import re
import html
import json
import time
import datetime
import requests
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit.components.v1 as components
import xml.etree.ElementTree as ET

try:
    from zoneinfo import ZoneInfo
    PL_TZ = ZoneInfo("Europe/Warsaw")
    NY_TZ = ZoneInfo("America/New_York")
    LON_TZ = ZoneInfo("Europe/London")
    TOK_TZ = ZoneInfo("Asia/Tokyo")
except Exception:
    PL_TZ = datetime.timezone(datetime.timedelta(hours=1))
    NY_TZ = datetime.timezone(datetime.timedelta(hours=-4))
    LON_TZ = datetime.timezone(datetime.timedelta(hours=0))
    TOK_TZ = datetime.timezone(datetime.timedelta(hours=9))

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import yfinance as yf
except ImportError:
    yf = None

st.set_page_config(
    page_title="SP500 SIGNAL AI PRO — Master Edition v6 (Auto-Exit + Samo-Audyt + Rynki)",
    page_icon="⚡",
    layout="wide"
)

KEYS_CACHE_FILE = ".saved_api_keys.json"
ARCHIVE_FILE = "archiwum_sygnalow_sp500.json"

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
                json.dump(payload, f, indent=2)
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

def load_persistent_archive():
    if os.path.exists(ARCHIVE_FILE):
        try:
            with open(ARCHIVE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []

def save_persistent_archive(archive_list):
    try:
        with open(ARCHIVE_FILE, "w", encoding="utf-8") as f:
            json.dump(archive_list, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

if "persistent_archive" not in st.session_state:
    st.session_state["persistent_archive"] = load_persistent_archive()

def trigger_beep_alarm(signal_type="LONG"):
    """Odtwarza dźwięk dla otwarcia (LONG/SHORT) lub specjalny dźwięk KASA dla zamknięcia zysku (EXIT)."""
    if signal_type == "EXIT":
        f1, f2, f3 = 1046, 1318, 1568
    elif signal_type == "LONG":
        f1, f2, f3 = 880, 1175, 880
    else:
        f1, f2, f3 = 520, 390, 520

    html_code = f"""
    <script>
    (function() {{
        try {{
            const AudioCtx = window.AudioContext || window.webkitAudioContext;
            const ctx = new AudioCtx();
            const playTone = (freq, start, duration) => {{
                const osc = ctx.createOscillator();
                const gain = ctx.createGain();
                osc.type = 'triangle';
                osc.frequency.setValueAtTime(freq, ctx.currentTime + start);
                gain.gain.setValueAtTime(0.28, ctx.currentTime + start);
                gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + start + duration);
                osc.connect(gain);
                gain.connect(ctx.destination);
                osc.start(ctx.currentTime + start);
                osc.stop(ctx.currentTime + start + duration);
            }};
            playTone({f1}, 0.0, 0.16);
            playTone({f2}, 0.19, 0.16);
            playTone({f3}, 0.38, 0.32);
        }} catch (e) {{}}
    }})();
    </script>
    """
    components.html(html_code, height=0, width=0)

# ==========================================
# ZEGAR RYNKÓW ŚWIATOWYCH (KIEDY OTWIERA I ZAMYKA RYNKI)
# ==========================================
def get_global_markets_schedule():
    now_pl = datetime.datetime.now(PL_TZ)
    now_ny = datetime.datetime.now(NY_TZ)
    now_lon = datetime.datetime.now(LON_TZ)
    now_tok = datetime.datetime.now(TOK_TZ)

    is_weekend = now_ny.weekday() >= 5

    # 1. Wall Street / NYSE (09:30 - 16:00 ET)
    ny_hm = now_ny.hour * 60 + now_ny.minute
    ny_open_hm = 9 * 60 + 30
    ny_close_hm = 16 * 60
    ny_open_dt = now_ny.replace(hour=9, minute=30, second=0).astimezone(PL_TZ)
    ny_close_dt = now_ny.replace(hour=16, minute=0, second=0).astimezone(PL_TZ)

    if not is_weekend and ny_open_hm <= ny_hm < ny_close_hm:
        mins_left = ny_close_hm - ny_hm
        ny_status = f"🟢 OTWARTY (Zamyka za {mins_left // 60}h {mins_left % 60}m — o {ny_close_dt.strftime('%H:%M')} PL)"
    else:
        if not is_weekend and ny_hm < ny_open_hm:
            mins_to = ny_open_hm - ny_hm
            ny_status = f"🔴 ZAMKNIĘTY (Otwiera za {mins_to // 60}h {mins_to % 60}m — o {ny_open_dt.strftime('%H:%M')} PL)"
        else:
            ny_status = f"🔴 ZAMKNIĘTY (Sesja główna USA: {ny_open_dt.strftime('%H:%M')}–{ny_close_dt.strftime('%H:%M')} PL)"

    # 2. Londyn / Europa (08:00 - 16:30 UK)
    lon_hm = now_lon.hour * 60 + now_lon.minute
    lon_open_hm = 8 * 60
    lon_close_hm = 16 * 60 + 30
    if not is_weekend and lon_open_hm <= lon_hm < lon_close_hm:
        mins_left = lon_close_hm - lon_hm
        eu_status = f"🟢 OTWARTY (Zamyka za {mins_left // 60}h {mins_left % 60}m — o 17:30 PL)"
    else:
        eu_status = "🔴 ZAMKNIĘTY (Sesja Europy: 09:00–17:30 PL)"

    # 3. Kontrakty US500 CFD (Capital.com 24/5 z przerwą techniczną 17:00-18:00 ET)
    if is_weekend:
        cfd_status = "🔴 PRZERWA WEEKENDOWA (Otwiera w nocy z niedzieli na poniedziałek o 00:00 PL)"
    elif 17 * 60 <= ny_hm < 18 * 60:
        mins_to = 18 * 60 - ny_hm
        cfd_status = f"🟡 PRZERWA TECHNICZNA 60 MIN (Wznawia handel za {mins_to} min)"
    else:
        cfd_status = "🟢 OTWARTY 24/5 (Handel na US500 CFD aktywny non-stop)"

    # 4. Tokio / Azja (09:00 - 15:00 JST)
    tok_hm = now_tok.hour * 60 + now_tok.minute
    if not is_weekend and (9 * 60 <= tok_hm < 15 * 60):
        asia_status = "🟢 OTWARTY (Sesja Azjatycka trwa)"
    else:
        asia_status = "🔴 ZAMKNIĘTY (Sesja Azji: 01:00–07:00 PL)"

    return {
        "us500_cfd": cfd_status,
        "nyse_cash": ny_status,
        "europe": eu_status,
        "asia": asia_status,
        "ny_open_pl": ny_open_dt.strftime("%H:%M"),
        "ny_close_pl": ny_close_dt.strftime("%H:%M")
    }

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

# ==========================================
# KLIENT API CAPITAL.COM (ŚWIECE + AUTO-TRADING DEMO)
# ==========================================
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

    def fetch_candles(self, epic="US500", resolution="MINUTE", max_bars=220):
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

    def get_open_positions(self, epic="US500"):
        cst, token, _ = self.get_session_tokens(force_new=False)
        if not cst or not token:
            return []
        url = f"{self.base_url}/api/v1/positions"
        headers = {"X-CAP-API-KEY": self.api_key, "CST": cst, "X-SECURITY-TOKEN": token}
        try:
            r = requests.get(url, headers=headers, timeout=6)
            if r.status_code == 200:
                positions = r.json().get("positions", [])
                return [p for p in positions if p.get("market", {}).get("epic") == epic]
        except Exception:
            pass
        return []

    def open_demo_position(self, direction="BUY", size=1.0, stop_level=None, profit_level=None, epic="US500"):
        cst, token, msg = self.get_session_tokens(force_new=False)
        if not cst or not token:
            return False, msg
        url = f"{self.base_url}/api/v1/positions"
        headers = {"X-CAP-API-KEY": self.api_key, "CST": cst, "X-SECURITY-TOKEN": token, "Content-Type": "application/json"}
        payload = {
            "epic": epic,
            "direction": direction,
            "size": float(size),
            "guaranteedStop": False
        }
        if stop_level is not None:
            payload["stopLevel"] = round(float(stop_level), 2)
        if profit_level is not None:
            payload["profitLevel"] = round(float(profit_level), 2)
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=8)
            if r.status_code == 200:
                deal_ref = r.json().get("dealReference", "OK")
                return True, f"✅ Auto-Otwarcie {direction} na Capital.com (Ref: {deal_ref})"
            return False, f"❌ Błąd otwarcia ({r.status_code}): {r.text[:120]}"
        except Exception as e:
            return False, f"❌ Wyjątek otwarcia: {e}"

    def close_demo_positions(self, epic="US500"):
        cst, token, msg = self.get_session_tokens(force_new=False)
        if not cst or not token:
            return False, msg
        open_pos = self.get_open_positions(epic=epic)
        if not open_pos:
            return True, "Brak otwartych pozycji US500 na koncie."
        closed_count = 0
        headers = {"X-CAP-API-KEY": self.api_key, "CST": cst, "X-SECURITY-TOKEN": token}
        for p in open_pos:
            deal_id = p.get("position", {}).get("dealId")
            if deal_id:
                try:
                    r = requests.delete(f"{self.base_url}/api/v1/positions/{deal_id}", headers=headers, timeout=8)
                    if r.status_code == 200:
                        closed_count += 1
                except Exception:
                    pass
        return True, f"💰 Automatycznie zamknięto {closed_count} pozycji US500 w zysku/SL!"

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
        return df.tail(220), 0.6, "🟡 TRYB AWARYJNY: Yahoo Finance (Sprawdź klucze Capital.com)"
    except Exception as e:
        return pd.DataFrame(), 0.6, f"Błąd fallbacku: {e}"

# ==========================================
# MODUŁ SAMO-AUDYTU I UCZENIA SIĘ NA BŁĘDACH
# ==========================================
def run_self_learning_audit(archive_list):
    default_params = {
        "min_atr_mult": 0.15,
        "min_slope_mult": 0.20,
        "rsi_long_max": 72.0,
        "rsi_short_min": 28.0,
        "dynamic_exit_min_profit": 7.0,
        "lessons": ["🔍 System zbiera pierwsze zamknięte pozycje — aktywne są bazowe progi Żelaznego Trendu EMA 50."],
        "overall_grade": "⏳ ZBIERANIE DANYCH",
        "good_count": 0,
        "bad_count": 0
    }
    if not archive_list:
        return default_params

    closed = [
        x for x in archive_list
        if x.get("status", "").startswith("✅")
        or x.get("status", "").startswith("💰")
        or x.get("status", "").startswith("🟢")
        or x.get("status", "").startswith("❌")
    ]
    if not closed:
        return default_params

    good_signals = [x for x in closed if float(x.get("pnl_pkt", 0)) > 0]
    bad_signals = [x for x in closed if float(x.get("pnl_pkt", 0)) <= 0]
    win_rate = (len(good_signals) / len(closed)) * 100.0

    lessons = []
    min_atr_mult = 0.15
    min_slope_mult = 0.20
    rsi_long_max = 72.0
    rsi_short_min = 28.0
    dynamic_exit_min_profit = 7.0

    last_3 = closed[-3:]
    last_3_losses = sum(1 for x in last_3 if float(x.get("pnl_pkt", 0)) <= 0)
    if last_3_losses >= 2:
        min_atr_mult = 0.22
        min_slope_mult = 0.28
        lessons.append("🛡️ **Korekta po błędach (Ostatnie 3 zagrania):** Wykryto 2 straty w ostatnich 3 zamkniętych pozycjach. Bot automatycznie zaostrzył filtr konsolidacji (+40% wyższy wymóg nachylenia EMA).")
    else:
        lessons.append("✅ **Bieżąca seria stabilna:** Ostatnie zamknięte pozycje wchodzą poprawnie — brak blokady bezpieczeństwa.")

    gave_back = [x for x in bad_signals if float(x.get("max_ruch_pkt", 0)) >= 6.0]
    if gave_back:
        dynamic_exit_min_profit = 5.5
        lessons.append(f"💰 **Nauka z oddanych zysków:** {len(gave_back)} błędnych pozycji było już na plusie (+6..12 pkt), ale rynek zawrócił przed TP. Bot obniżył próg automatycznego zamykania zysku (`💰 ZAMKNIJ ZYSK`) do **+5.5 pkt**!")

    long_trades = [x for x in closed if "LONG" in x.get("kierunek", "")]
    short_trades = [x for x in closed if "SHORT" in x.get("kierunek", "")]
    if len(long_trades) >= 2:
        l_wr = sum(1 for x in long_trades if float(x.get("pnl_pkt", 0)) > 0) / len(long_trades) * 100.0
        if l_wr < 45.0:
            rsi_long_max = 65.0
            lessons.append(f"📉 **Korekta LONG ({l_wr:.0f}% trafień):** Sygnały LONG wchodziły słabiej — bot obniżył maksymalne RSI dla LONG z 72 do **65**, aby nie kupować na szczytach.")
    if len(short_trades) >= 2:
        s_wr = sum(1 for x in short_trades if float(x.get("pnl_pkt", 0)) > 0) / len(short_trades) * 100.0
        if s_wr < 45.0:
            rsi_short_min = 35.0
            lessons.append(f"📉 **Korekta SHORT ({s_wr:.0f}% trafień):** Sygnały SHORT wchodziły słabiej — bot podniósł minimalne RSI dla SHORT z 28 do **35**, aby nie sprzedawać w samym dołku.")

    grade = "🏆 WZOROWA" if win_rate >= 65 else ("🟢 DOBRA" if win_rate >= 50 else "🔧 AKTYWNA KOREKTA BŁĘDÓW")
    return {
        "min_atr_mult": min_atr_mult,
        "min_slope_mult": min_slope_mult,
        "rsi_long_max": rsi_long_max,
        "rsi_short_min": rsi_short_min,
        "dynamic_exit_min_profit": dynamic_exit_min_profit,
        "lessons": lessons,
        "overall_grade": f"{grade} ({win_rate:.1f}% dobrych sygnałów)",
        "good_count": len(good_signals),
        "bad_count": len(bad_signals)
    }

# ==========================================
# SILNIK CYKLU ŻYCIA POZYCJI: OTWIERA ➔ PROWADZI ➔ ZAMYKA W ZYSKU
# ==========================================
def detect_signals_and_waves(df_main, df_htf=None, trade_style="🔥 FALA 30 PKT (Trzymaj + Auto-Zamykanie Zysku)", sensitivity="Tylko Pewniaki (Filtr Konsolidacji)", spread_pts=0.6, learned_params=None):
    if learned_params is None:
        learned_params = run_self_learning_audit([])

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
    df["EntryTime"] = ""
    df["SL"] = np.nan
    df["TP1"] = np.nan
    df["TP"] = np.nan
    df["ExitSignal"] = 0          # 1 = 💰 ZAMKNIJ ZYSK, -1 = 🛑 STOP LOSS
    df["ExitPrice"] = np.nan
    df["ExitLabel"] = ""

    is_wave_30 = "30 PKT" in trade_style
    wick_ratio = 1.4 if sensitivity == "Wysoka (Więcej strzałek)" else 1.7
    min_atr_mult = float(learned_params.get("min_atr_mult", 0.15))
    min_slope_mult = float(learned_params.get("min_slope_mult", 0.20))
    rsi_long_max = float(learned_params.get("rsi_long_max", 72.0))
    rsi_short_min = float(learned_params.get("rsi_short_min", 28.0))
    dyn_profit_trig = float(learned_params.get("dynamic_exit_min_profit", 7.0)) if is_wave_30 else 3.5

    active_pos = None
    last_exit_idx = -10

    # TWARDA BLOKADA: Pętla analizuje wyłącznie ZAMKNIĘTE świece (do len(df) - 1)
    for i in range(5, len(df) - 1):
        o, h, l, c = float(df["Open"].iloc[i]), float(df["High"].iloc[i]), float(df["Low"].iloc[i]), float(df["Close"].iloc[i])
        po, ph, pl, pc = float(df["Open"].iloc[i - 1]), float(df["High"].iloc[i - 1]), float(df["Low"].iloc[i - 1]), float(df["Close"].iloc[i - 1])
        ema9, ema21, ema50 = float(df["EMA9"].iloc[i]), float(df["EMA21"].iloc[i]), float(df["EMA50"].iloc[i])
        ema9_prev3 = float(df["EMA9"].iloc[i - 3])
        ema21_prev3 = float(df["EMA21"].iloc[i - 3])
        rsi = float(df["RSI"].iloc[i])
        atr = max(float(df["ATR"].iloc[i]), 0.5)
        bar_htf_bias = int(df["HTF_Bias"].iloc[i])

        # KROK A: JEŚLI MAMY OTWARTĄ POZYCJĘ — SPRAWDŹ CZY ZAMKNĄĆ W ZYSKU LUB SL
        if active_pos is not None:
            p_sig = active_pos["sig"]
            p_entry = active_pos["entry"]
            p_sl = active_pos["sl"]
            p_tp1 = active_pos["tp1"]
            p_tp2 = active_pos["tp2"]

            if p_sig == 1:
                fav = h - p_entry
                if fav > active_pos["max_fav"]:
                    active_pos["max_fav"] = fav
                if h >= p_tp1:
                    active_pos["hit_tp1"] = True

                if l <= p_sl:
                    if active_pos["hit_tp1"]:
                        df.iloc[i, df.columns.get_loc("ExitSignal")] = 1
                        df.iloc[i, df.columns.get_loc("ExitPrice")] = p_tp1
                        df.iloc[i, df.columns.get_loc("ExitLabel")] = f"💰 ZAMKNIJ ZYSK (+{p_tp1 - p_entry:.1f} pkt)"
                    else:
                        df.iloc[i, df.columns.get_loc("ExitSignal")] = -1
                        df.iloc[i, df.columns.get_loc("ExitPrice")] = p_sl
                        df.iloc[i, df.columns.get_loc("ExitLabel")] = f"🛑 STOP LOSS ({p_sl - p_entry:.1f} pkt)"
                    active_pos = None
                    last_exit_idx = i
                    continue

                if h >= p_tp2:
                    df.iloc[i, df.columns.get_loc("ExitSignal")] = 1
                    df.iloc[i, df.columns.get_loc("ExitPrice")] = p_tp2
                    df.iloc[i, df.columns.get_loc("ExitLabel")] = f"🎯 PEŁNE TP (+{p_tp2 - p_entry:.1f} pkt!)"
                    active_pos = None
                    last_exit_idx = i
                    continue

                cur_open_pnl = c - p_entry
                if active_pos["max_fav"] >= dyn_profit_trig and cur_open_pnl >= 3.5:
                    reversal_down = (c < ema9 and c < o) or (active_pos["max_fav"] >= 14.0 and cur_open_pnl <= active_pos["max_fav"] * 0.68)
                    if reversal_down:
                        df.iloc[i, df.columns.get_loc("ExitSignal")] = 1
                        df.iloc[i, df.columns.get_loc("ExitPrice")] = round(c, 2)
                        df.iloc[i, df.columns.get_loc("ExitLabel")] = f"💰 ZAMKNIJ ZYSK (+{cur_open_pnl:.1f} pkt)"
                        active_pos = None
                        last_exit_idx = i
                        continue

            else:  # SHORT
                fav = p_entry - l
                if fav > active_pos["max_fav"]:
                    active_pos["max_fav"] = fav
                if l <= p_tp1:
                    active_pos["hit_tp1"] = True

                if h >= p_sl:
                    if active_pos["hit_tp1"]:
                        df.iloc[i, df.columns.get_loc("ExitSignal")] = 1
                        df.iloc[i, df.columns.get_loc("ExitPrice")] = p_tp1
                        df.iloc[i, df.columns.get_loc("ExitLabel")] = f"💰 ZAMKNIJ ZYSK (+{p_entry - p_tp1:.1f} pkt)"
                    else:
                        df.iloc[i, df.columns.get_loc("ExitSignal")] = -1
                        df.iloc[i, df.columns.get_loc("ExitPrice")] = p_sl
                        df.iloc[i, df.columns.get_loc("ExitLabel")] = f"🛑 STOP LOSS (-{p_sl - p_entry:.1f} pkt)"
                    active_pos = None
                    last_exit_idx = i
                    continue

                if l <= p_tp2:
                    df.iloc[i, df.columns.get_loc("ExitSignal")] = 1
                    df.iloc[i, df.columns.get_loc("ExitPrice")] = p_tp2
                    df.iloc[i, df.columns.get_loc("ExitLabel")] = f"🎯 PEŁNE TP (+{p_entry - p_tp2:.1f} pkt!)"
                    active_pos = None
                    last_exit_idx = i
                    continue

                cur_open_pnl = p_entry - c
                if active_pos["max_fav"] >= dyn_profit_trig and cur_open_pnl >= 3.5:
                    reversal_up = (c > ema9 and c > o) or (active_pos["max_fav"] >= 14.0 and cur_open_pnl <= active_pos["max_fav"] * 0.68)
                    if reversal_up:
                        df.iloc[i, df.columns.get_loc("ExitSignal")] = 1
                        df.iloc[i, df.columns.get_loc("ExitPrice")] = round(c, 2)
                        df.iloc[i, df.columns.get_loc("ExitLabel")] = f"💰 ZAMKNIJ ZYSK (+{cur_open_pnl:.1f} pkt)"
                        active_pos = None
                        last_exit_idx = i
                        continue

            continue

        # KROK B: BRAK OTWARTEJ POZYCJI — SZUKAMY NOWEGO SYGNAŁU WEJŚCIA
        if (i - last_exit_idx) < 2:
            continue

        body = abs(c - o)
        rng = max(h - l, 0.01)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        ema_gap = abs(ema9 - ema21)
        ema_slope = ema9 - ema9_prev3
        ema21_slope = ema21 - ema21_prev3
        is_chop = (ema_gap < atr * min_atr_mult) and (abs(ema_slope) < atr * min_slope_mult)

        if sensitivity != "Wysoka (Więcej strzałek)" and is_chop:
            continue

        close_near_high = ((c - l) / rng) >= 0.55
        close_near_low = ((h - c) / rng) >= 0.55

        bull_pinbar = (lower_wick >= body * wick_ratio) and (lower_wick >= rng * 0.40) and close_near_high and (c > o)
        bull_engulfing = (pc < po) and (c > o) and (c > po) and (body >= atr * 0.35)
        bull_pullback = (l <= ema21) and (c > ema9) and (c > o) and close_near_high
        bull_wave_break = (c > o) and (c > ema9) and (ema9 > ema21) and (df["EMA9"].iloc[i - 2] <= df["EMA21"].iloc[i - 2])

        bear_pinbar = (upper_wick >= body * wick_ratio) and (upper_wick >= rng * 0.40) and close_near_low and (c < o)
        bear_engulfing = (pc > po) and (c < o) and (c < po) and (body >= atr * 0.35)
        bear_pullback = (h >= ema21) and (c < ema9) and (c < o) and close_near_low
        bear_wave_break = (c < o) and (c < ema9) and (ema9 < ema21) and (df["EMA9"].iloc[i - 2] >= df["EMA21"].iloc[i - 2])

        trend_up = (ema9 > ema21) and (c > ema50) and (ema_slope > 0) and (ema21_slope >= 0)
        trend_down = (ema9 < ema21) and (c < ema50) and (ema_slope < 0) and (ema21_slope <= 0)

        if sensitivity == "Tylko Pewniaki (Filtr Konsolidacji)" and bar_htf_bias != 0:
            trend_up = trend_up and (bar_htf_bias == 1)
            trend_down = trend_down and (bar_htf_bias == -1)

        next_open = float(df["Open"].iloc[i + 1])
        next_time_str = str(df.index[i + 1].strftime("%Y-%m-%d %H:%M:%S"))
        half_spread = float(spread_pts or 0.6) / 2.0

        if (bull_pinbar or bull_engulfing or bull_pullback or bull_wave_break) and trend_up and (42 <= rsi <= rsi_long_max):
            df.iloc[i, df.columns.get_loc("Signal")] = 1
            df.iloc[i, df.columns.get_loc("ArrowPrice")] = l - (atr * 0.40)
            real_entry = round(next_open + half_spread, 2)
            df.iloc[i, df.columns.get_loc("EntryPrice")] = real_entry
            df.iloc[i, df.columns.get_loc("EntryTime")] = next_time_str

            if is_wave_30:
                swing_low = float(df["Low"].iloc[max(0, i - 5):i + 1].min())
                sl_dist = np.clip(max(real_entry - swing_low + atr, atr * 3.5), 8.0, 14.0)
                sl_val = round(real_entry - sl_dist, 2)
                tp1_val = round(real_entry + 15.0, 2)
                tp2_val = round(real_entry + 30.0, 2)
                pat_name = "🔥 OTWARCIE LONG (Fala 30 pkt)"
            else:
                sl_val = round(min(l, real_entry - atr) - (atr * 0.35), 2)
                tp1_val = round(real_entry + (atr * 1.5), 2)
                tp2_val = round(real_entry + (atr * 3.0), 2)
                pat_name = "⚡ OTWARCIE LONG (Scalp)"

            df.iloc[i, df.columns.get_loc("SL")] = sl_val
            df.iloc[i, df.columns.get_loc("TP1")] = tp1_val
            df.iloc[i, df.columns.get_loc("TP")] = tp2_val
            df.iloc[i, df.columns.get_loc("Pattern")] = pat_name
            active_pos = {"sig": 1, "entry": real_entry, "sl": sl_val, "tp1": tp1_val, "tp2": tp2_val, "max_fav": 0.0, "hit_tp1": False}

        elif (bear_pinbar or bear_engulfing or bear_pullback or bear_wave_break) and trend_down and (rsi_short_min <= rsi <= 58):
            df.iloc[i, df.columns.get_loc("Signal")] = -1
            df.iloc[i, df.columns.get_loc("ArrowPrice")] = h + (atr * 0.40)
            real_entry = round(next_open - half_spread, 2)
            df.iloc[i, df.columns.get_loc("EntryPrice")] = real_entry
            df.iloc[i, df.columns.get_loc("EntryTime")] = next_time_str

            if is_wave_30:
                swing_high = float(df["High"].iloc[max(0, i - 5):i + 1].max())
                sl_dist = np.clip(max(swing_high - real_entry + atr, atr * 3.5), 8.0, 14.0)
                sl_val = round(real_entry + sl_dist, 2)
                tp1_val = round(real_entry - 15.0, 2)
                tp2_val = round(real_entry - 30.0, 2)
                pat_name = "🔥 OTWARCIE SHORT (Zjazd 30 pkt)"
            else:
                sl_val = round(max(h, real_entry + atr) + (atr * 0.35), 2)
                tp1_val = round(real_entry - (atr * 1.5), 2)
                tp2_val = round(real_entry - (atr * 3.0), 2)
                pat_name = "⚡ OTWARCIE SHORT (Scalp)"

            df.iloc[i, df.columns.get_loc("SL")] = sl_val
            df.iloc[i, df.columns.get_loc("TP1")] = tp1_val
            df.iloc[i, df.columns.get_loc("TP")] = tp2_val
            df.iloc[i, df.columns.get_loc("Pattern")] = pat_name
            active_pos = {"sig": -1, "entry": real_entry, "sl": sl_val, "tp1": tp1_val, "tp2": tp2_val, "max_fav": 0.0, "hit_tp1": False}

    return df, current_htf_bias

def sync_and_evaluate_persistent_archive(df, resolution):
    archive = st.session_state.get("persistent_archive", [])
    existing_ids = {item.get("id") for item in archive}
    now_clock = datetime.datetime.now(PL_TZ).strftime("%Y-%m-%d %H:%M:%S")

    for i in range(len(df) - 1):
        sig = int(df["Signal"].iloc[i])
        if sig == 0:
            continue
        candle_ts = str(df.index[i].strftime("%Y-%m-%d %H:%M:%S"))
        entry_ts = str(df.index[i + 1].strftime("%Y-%m-%d %H:%M:%S"))
        sig_id = f"{resolution}_{candle_ts}"

        if sig_id not in existing_ids:
            archive.append({
                "id": sig_id,
                "interwal": resolution,
                "swieca_sygnalu": candle_ts,
                "moment_wejscia": entry_ts,
                "zapisano_zegar": now_clock,
                "potwierdzenie": "✅ Po zamknięciu świecy",
                "kierunek": "🟢 LONG" if sig == 1 else "🔴 SHORT",
                "sig_num": sig,
                "formacja": str(df["Pattern"].iloc[i]),
                "wejscie": float(df["EntryPrice"].iloc[i]),
                "sl": float(df["SL"].iloc[i]),
                "tp1": float(df["TP1"].iloc[i]),
                "tp2": float(df["TP"].iloc[i]),
                "entry_atr": round(float(df["ATR"].iloc[i]), 2),
                "entry_rsi": round(float(df["RSI"].iloc[i]), 1),
                "max_ruch_pkt": 0.0,
                "hit_tp1": False,
                "status": "⏳ W GRZE",
                "ocena_bota": "⏳ W TRAKCIE",
                "pnl_pkt": 0.0,
                "cena_wyjscia": 0.0,
                "czas_rozliczenia": "-"
            })
            existing_ids.add(sig_id)

    for item in archive:
        if item.get("interwal") != resolution:
            continue
        st_txt = item.get("status", "")
        if st_txt.startswith("✅") or st_txt.startswith("❌") or st_txt.startswith("🟢") or st_txt.startswith("💰"):
            item["ocena_bota"] = "✅ DOBRY SYGNAŁ (Zysk)" if float(item.get("pnl_pkt", 0)) > 0 else "❌ BŁĘDNY SYGNAŁ (Strata)"
            continue

        entry_dt = pd.to_datetime(item["moment_wejscia"])
        sub_df = df[df.index >= entry_dt]
        if sub_df.empty:
            continue

        sig = int(item["sig_num"])
        entry = float(item["wejscie"])
        sl = float(item["sl"])
        tp1 = float(item["tp1"])
        tp2 = float(item["tp2"])
        max_fav = float(item.get("max_ruch_pkt", 0.0))
        hit_tp1 = bool(item.get("hit_tp1", False))

        closed_sub = sub_df.iloc[:-1] if len(sub_df) > 1 else sub_df.iloc[:0]

        for ts_idx, row in closed_sub.iterrows():
            hj = float(row["High"])
            lj = float(row["Low"])
            cj = float(row["Close"])
            oj = float(row["Open"])
            ema9_j = float(row["EMA9"])
            ts_str = str(ts_idx.strftime("%H:%M:%S"))

            if sig == 1:
                fav = round(hj - entry, 2)
                if fav > max_fav:
                    max_fav = fav
                if hj >= tp1:
                    hit_tp1 = True
                if lj <= sl and hj >= tp2:
                    item["status"] = "❌ STOP LOSS (Konflikt w 1 św.)"
                    item["pnl_pkt"] = round(sl - entry, 2)
                    item["cena_wyjscia"] = sl
                    item["czas_rozliczenia"] = ts_str
                    break
                elif lj <= sl:
                    if hit_tp1:
                        item["status"] = "🟢 +15 PKT (Zabezpieczony TP1)"
                        item["pnl_pkt"] = round(tp1 - entry, 2)
                        item["cena_wyjscia"] = tp1
                    else:
                        item["status"] = "❌ STOP LOSS"
                        item["pnl_pkt"] = round(sl - entry, 2)
                        item["cena_wyjscia"] = sl
                    item["czas_rozliczenia"] = ts_str
                    break
                elif hj >= tp2:
                    item["status"] = "✅ PEŁNE TP2 (+30 PKT!)"
                    item["pnl_pkt"] = round(tp2 - entry, 2)
                    item["cena_wyjscia"] = tp2
                    item["czas_rozliczenia"] = ts_str
                    break
                cur_pnl = cj - entry
                if max_fav >= 7.0 and cur_pnl >= 3.5 and ((cj < ema9_j and cj < oj) or (max_fav >= 14.0 and cur_pnl <= max_fav * 0.68)):
                    item["status"] = f"💰 AUTO-ZAMKNIĘCIE W ZYSKU (+{cur_pnl:.1f} pkt)"
                    item["pnl_pkt"] = round(cur_pnl, 2)
                    item["cena_wyjscia"] = round(cj, 2)
                    item["czas_rozliczenia"] = ts_str
                    break
            else:
                fav = round(entry - lj, 2)
                if fav > max_fav:
                    max_fav = fav
                if lj <= tp1:
                    hit_tp1 = True
                if hj >= sl and lj <= tp2:
                    item["status"] = "❌ STOP LOSS (Konflikt w 1 św.)"
                    item["pnl_pkt"] = round(entry - sl, 2)
                    item["cena_wyjscia"] = sl
                    item["czas_rozliczenia"] = ts_str
                    break
                elif hj >= sl:
                    if hit_tp1:
                        item["status"] = "🟢 +15 PKT (Zabezpieczony TP1)"
                        item["pnl_pkt"] = round(entry - tp1, 2)
                        item["cena_wyjscia"] = tp1
                    else:
                        item["status"] = "❌ STOP LOSS"
                        item["pnl_pkt"] = round(entry - sl, 2)
                        item["cena_wyjscia"] = sl
                    item["czas_rozliczenia"] = ts_str
                    break
                elif lj <= tp2:
                    item["status"] = "✅ PEŁNE TP2 (-30 PKT ZJAZD!)"
                    item["pnl_pkt"] = round(entry - tp2, 2)
                    item["cena_wyjscia"] = tp2
                    item["czas_rozliczenia"] = ts_str
                    break
                cur_pnl = entry - cj
                if max_fav >= 7.0 and cur_pnl >= 3.5 and ((cj > ema9_j and cj > oj) or (max_fav >= 14.0 and cur_pnl <= max_fav * 0.68)):
                    item["status"] = f"💰 AUTO-ZAMKNIĘCIE W ZYSKU (+{cur_pnl:.1f} pkt)"
                    item["pnl_pkt"] = round(cur_pnl, 2)
                    item["cena_wyjscia"] = round(cj, 2)
                    item["czas_rozliczenia"] = ts_str
                    break

        item["max_ruch_pkt"] = max_fav
        item["hit_tp1"] = hit_tp1

        if item["status"].startswith("⏳"):
            cur_c = float(df["Close"].iloc[-1])
            item["pnl_pkt"] = round((cur_c - entry) if sig == 1 else (entry - cur_c), 2)
            item["cena_wyjscia"] = round(cur_c, 2)
            item["ocena_bota"] = "⏳ W GRZE (Zarabia)" if item["pnl_pkt"] > 0 else "⏳ W GRZE"
        else:
            item["ocena_bota"] = "✅ DOBRY SYGNAŁ (Zysk)" if float(item.get("pnl_pkt", 0)) > 0 else "❌ BŁĘDNY SYGNAŁ (Strata)"

    st.session_state["persistent_archive"] = archive
    save_persistent_archive(archive)
    return pd.DataFrame(archive)

saved = st.session_state["saved_keys"]

with st.sidebar:
    st.title("⚡ Master Edition v6")
    st.caption("Auto-Zamykanie Zysku • Samo-Audyt • Godziny Rynków")

    with st.expander("🔑 Klucze API (Zapis na dysku PC)", expanded=not bool(saved.get("cap_key"))):
        with st.form("api_login_form", clear_on_submit=False):
            is_demo_in = st.checkbox("Konto DEMO Capital.com", value=saved.get("is_demo", True))
            cap_key_in = st.text_input("Capital API Key:", value=saved.get("cap_key", ""), type="password")
            cap_id_in = st.text_input("Capital Login (Email):", value=saved.get("cap_id", ""))
            cap_pass_in = st.text_input("Capital Hasło API:", value=saved.get("cap_pass", ""), type="password")
            openai_key_in = st.text_input("OpenAI API Key (do czatu):", value=saved.get("openai_key", ""), type="password")
            remember_in = st.checkbox("💾 Zapamiętaj klucze na dysku PC", value=saved.get("remember_on_disk", True))

            login_submitted = st.form_submit_button("🔐 ZALOGUJ I ZAPISZ", use_container_width=True, type="primary")

        if login_submitted:
            st.session_state.pop("cap_session_cache", None)
            save_cached_keys(is_demo_in, cap_key_in, cap_id_in, cap_pass_in, openai_key_in, remember_on_disk=remember_in)
            st.success("Zapisano klucze! Odświeżam...")
            st.rerun()

        if saved.get("cap_key") and saved.get("cap_id"):
            st.caption(f"📌 Zalogowany: `{saved.get('cap_id')}`")
            if st.button("🗑️ Wyloguj / Wyczyść klucze", use_container_width=True):
                st.session_state.pop("cap_session_cache", None)
                save_cached_keys(True, "", "", "", "", remember_on_disk=False)
                st.rerun()

    st.markdown("---")
    st.subheader("1. Styl Gry i Samo-Audyt")
    trade_style = st.radio(
        "Wybierz cel zysku na pozycję:",
        [
            "🔥 FALA 30 PKT (Trzymaj + Auto-Zamykanie Zysku)",
            "⚡ Szybki Scalp (Krótkie ruchy + Szybki Exit)"
        ],
        index=0
    )
    resolution = st.selectbox("Interwał wykresu:", ["MINUTE", "MINUTE_5", "MINUTE_15"], index=0)
    sensitivity = st.radio(
        "Filtr strzałek:",
        ["Tylko Pewniaki (Filtr Konsolidacji)", "Standard (Z filtrem płaskich EMA)", "Wysoka (Więcej strzałek)"],
        index=0
    )
    enable_self_learning = st.toggle("🧠 Samo-Audyt (Bot sam uczy się na błędach z archiwum)", value=True)
    max_spread = st.slider("Maks. Spread (pkt):", 0.2, 2.5, 0.9, 0.1)

    st.markdown("---")
    st.subheader("2. Automat Giełdowy (Opcjonalnie)")
    auto_trade_demo = st.toggle(
        "🤖 Auto-Trading DEMO Capital.com (Sam otwiera i sam zamyka pozycje)",
        value=False,
        help="Gdy włączone, bot przy sygnale OTWARCIE sam wyśle zlecenie na konto DEMO Capital.com, a przy sygnale 💰 ZAMKNIJ ZYSK sam zamknie pozycję!"
    )
    trade_size = st.number_input("Wielkość pozycji DEMO (kontrakty):", min_value=0.1, max_value=50.0, value=1.0, step=0.5)

    st.markdown("---")
    st.subheader("3. Dźwięk i Odświeżanie")
    sound_enabled = st.toggle("🔊 Piszczenie (Wejście + Zamknięcie Zysku)", value=True)
    c_b1, c_b2 = st.columns(2)
    if c_b1.button("🔊 Test Wejścia", use_container_width=True):
        trigger_beep_alarm("LONG")
    if c_b2.button("💰 Test Zysku", use_container_width=True):
        trigger_beep_alarm("EXIT")

    auto_refresh = st.toggle("🔄 Auto-odświeżanie na żywo", value=True)
    refresh_sec = st.slider("Co ile sekund:", 5, 30, 5)

st.title("🎯 SP500 SIGNAL AI PRO — Auto-Zamykanie Zysku + Samo-Audyt + Zegar Rynków")

# 1. Zegar Rynków Światowych na górze ekranu
mkt_sched = get_global_markets_schedule()
m_c1, m_c2, m_c3, m_c4 = st.columns(4)
m_c1.info(f"**🇺🇸 US500 CFD (Capital.com):**\n\n{mkt_sched['us500_cfd']}")
m_c2.info(f"**🗽 Giełda USA (Wall Street NYSE):**\n\n{mkt_sched['nyse_cash']}")
m_c3.info(f"**🇪🇺 Europa (Londyn / Frankfurt):**\n\n{mkt_sched['europe']}")
m_c4.info(f"**🇯🇵 Azja (Tokio / Nikkei):**\n\n{mkt_sched['asia']}")

client = CapitalComClient(
    saved.get("cap_key", ""),
    saved.get("cap_id", ""),
    saved.get("cap_pass", ""),
    is_demo=saved.get("is_demo", True)
)
df_raw, current_spread, source_status = client.fetch_candles(epic="US500", resolution=resolution, max_bars=220)

htf_map = {"MINUTE": "MINUTE_5", "MINUTE_5": "MINUTE_15", "MINUTE_15": "HOUR"}
htf_res = htf_map.get(resolution, "MINUTE_15")
df_htf, _, _ = client.fetch_candles(epic="US500", resolution=htf_res, max_bars=90)

if df_raw.empty:
    df_raw, current_spread, source_status = fetch_fallback_data(resolution)
    df_htf, _, _ = fetch_fallback_data(htf_res)

if df_raw.empty or len(df_raw) < 25:
    st.error(f"Nie udało się pobrać świec: {source_status}")
    st.stop()

# 2. Uruchomienie Samo-Audytu na dotychczasowym archiwum
audit_info = run_self_learning_audit(st.session_state.get("persistent_archive", [])) if enable_self_learning else run_self_learning_audit([])

# 3. Detekcja otwarć ORAZ automatycznych zamknięć w zysku
df, htf_bias = detect_signals_and_waves(
    df_raw, df_htf, trade_style=trade_style, sensitivity=sensitivity, spread_pts=current_spread or 0.6, learned_params=audit_info
)
archive_df = sync_and_evaluate_persistent_archive(df, resolution)
audit_info = run_self_learning_audit(st.session_state.get("persistent_archive", []))

articles, feed_status, news_sentiment_str, news_bias_code, bull_sc, bear_sc = fetch_full_macro_news_and_sentiment()
in_macro_window, macro_window_label = check_us_macro_window()

closed_candle = df.iloc[-2]
live_candle = df.iloc[-1]
closed_candle_time = df.index[-2]
live_candle_time = df.index[-1]

current_price = float(live_candle["Close"])
current_atr = float(closed_candle["ATR"])
current_rsi = float(closed_candle["RSI"])

now_pl = datetime.datetime.now(PL_TZ)
sec_left_in_min = 60 - now_pl.second

recent_window = df.tail(60)
recent_high = float(recent_window["High"].max())
recent_low = float(recent_window["Low"].min())
drop_from_high = round(recent_high - current_price, 1)
rally_from_low = round(current_price - recent_low, 1)

st.caption(
    f"🔒 **BLOKADA ŚWIECY W TRAKCIE RYSOWANIA:** Świeca `{live_candle_time.strftime('%H:%M')}` dopiero się tworzy "
    f"(zostało `{sec_left_in_min} sek.`). Bot ocenia wyłącznie zamkniętą świecę `{closed_candle_time.strftime('%H:%M')}` | "
    f"🧠 **Samo-Audyt Bota:** `{audit_info['overall_grade']}`"
)

if news_bias_code == -1:
    st.error(f"📰 **SENTYMENT WIADOMOŚCI (Pełne opisy RSS):** {news_sentiment_str} *(Negatywne: {bear_sc} vs Pozytywne: {bull_sc})*")
elif news_bias_code == 1:
    st.success(f"📰 **SENTYMENT WIADOMOŚCI (Pełne opisy RSS):** {news_sentiment_str} *(Pozytywne: {bull_sc} vs Negatywne: {bear_sc})*")
else:
    st.info(f"📰 **SENTYMENT WIADOMOŚCI (Pełne opisy RSS):** {news_sentiment_str} *(Pozytywne: {bull_sc} vs Negatywne: {bear_sc})*")

active_sig = int(closed_candle["Signal"])
active_pattern = closed_candle["Pattern"]
exit_sig = int(closed_candle["ExitSignal"])
exit_label = str(closed_candle["ExitLabel"])
sig_time = str(closed_candle_time)
spread_ok = (current_spread is None) or (current_spread <= max_spread)

if "last_event_candle" not in st.session_state:
    st.session_state.last_event_candle = None

# Obsługa alarmu dźwiękowego oraz opcjonalnego Auto-Tradingu na koncie DEMO Capital.com
if st.session_state.last_event_candle != sig_time:
    if exit_sig == 1:
        if sound_enabled:
            trigger_beep_alarm("EXIT")
        if auto_trade_demo and saved.get("cap_key"):
            ok_close, msg_close = client.close_demo_positions(epic="US500")
            st.toast(msg_close)
        st.session_state.last_event_candle = sig_time
    elif active_sig != 0 and spread_ok:
        if sound_enabled:
            trigger_beep_alarm("LONG" if active_sig == 1 else "SHORT")
        if auto_trade_demo and saved.get("cap_key"):
            dir_api = "BUY" if active_sig == 1 else "SELL"
            ok_open, msg_open = client.open_demo_position(
                direction=dir_api,
                size=trade_size,
                stop_level=closed_candle["SL"],
                profit_level=closed_candle["TP"],
                epic="US500"
            )
            st.toast(msg_open)
        st.session_state.last_event_candle = sig_time

if not archive_df.empty:
    wins = len(archive_df[archive_df["ocena_bota"].str.startswith("✅")])
    losses = len(archive_df[archive_df["ocena_bota"].str.startswith("❌")])
    in_play = len(archive_df[archive_df["status"].str.startswith("⏳")])
    total_closed = wins + losses
    win_rate = round((wins / total_closed) * 100, 1) if total_closed > 0 else 0.0
    total_pts = round(archive_df["pnl_pkt"].sum(), 2)
else:
    wins, losses, in_play, total_closed, win_rate, total_pts = 0, 0, 0, 0, 0.0, 0.0

tab_main, tab_archive, tab_audit, tab_news = st.tabs([
    "📊 Wykres: Otwiera ➔ Zamyka w Zysku",
    f"🗄️ Archiwum: Dobry czy Błędny ({wins} Dobrych / {losses} Błędnych — {win_rate}%)",
    "🧠 Samo-Audyt AI (Nauka na Błędach)",
    "🌍 Godziny Rynków & Wiadomości FED"
])

with tab_main:
    if exit_sig == 1:
        st.warning(
            f"### 💰 AUTOMATYCZNE ZAMKNIĘCIE POZYCJI W ZYSKU: `{exit_label}`!\n"
            f"Świeca `{closed_candle_time.strftime('%H:%M')}` zamknęła falę i zabezpieczyła zysk po cenie `{closed_candle['ExitPrice']:.2f}`! "
            f"**Koleżanka zamyka pozycję z zyskiem i czeka na kolejny sygnał otwarcia.**"
        )
    elif active_sig == 1 and spread_ok:
        sl_p, tp1_p, tp2_p = closed_candle["SL"], closed_candle["TP1"], closed_candle["TP"]
        entry_p = closed_candle["EntryPrice"]
        st.success(
            f"### 🟢 OTWARCIE POZYCJI LONG ({active_pattern}) — ŚWIECA `{closed_candle_time.strftime('%H:%M')}` ZAMKNIĘTA!\n"
            f"**Wchodzisz na otwarciu nowej świecy (`{live_candle_time.strftime('%H:%M')}`) po:** `{entry_p:.2f}` | "
            f"**Stop Loss:** `{sl_p:.2f}` | **TP1 (+15 pkt):** `{tp1_p:.2f}` | **TP2 (+30 pkt):** `{tp2_p:.2f}` "
            f"*(Bot sam pokaże znacznik `💰 ZAMKNIJ ZYSK`, gdy fala zacznie zawracać!)*"
        )
    elif active_sig == -1 and spread_ok:
        sl_p, tp1_p, tp2_p = closed_candle["SL"], closed_candle["TP1"], closed_candle["TP"]
        entry_p = closed_candle["EntryPrice"]
        st.error(
            f"### 🔴 OTWARCIE POZYCJI SHORT ({active_pattern}) — ŚWIECA `{closed_candle_time.strftime('%H:%M')}` ZAMKNIĘTA!\n"
            f"**Wchodzisz na otwarciu nowej świecy (`{live_candle_time.strftime('%H:%M')}`) po:** `{entry_p:.2f}` | "
            f"**Stop Loss:** `{sl_p:.2f}` | **TP1 (-15 pkt):** `{tp1_p:.2f}` | **TP2 (-30 pkt ZJAZD):** `{tp2_p:.2f}` "
            f"*(Bot sam pokaże znacznik `💰 ZAMKNIJ ZYSK`, gdy zjazd zacznie zawracać!)*"
        )
    else:
        if not archive_df.empty:
            last_row = archive_df.iloc[-1]
            if last_row["status"].startswith("⏳"):
                st.info(
                    f"⏳ **POZYCJA AKTYWNA W GRZE (`{last_row['kierunek']}` od `{last_row['wejscie']:.2f}`):** "
                    f"Bieżący wynik na żywo: **`{last_row['pnl_pkt']:+.2f} pkt`** (Maks. ruch w dobrą stronę: `+{last_row['max_ruch_pkt']:.1f} pkt`). "
                    f"Bot pilnuje świec — gdy ruch zacznie zawracać, wyświetli **`💰 ZAMKNIJ ZYSK`**!"
                )
            else:
                st.info(
                    f"✅ **OSTATNIA POZYCJA ZAMKNIĘTA (`{last_row['kierunek']}` z godz. `{last_row['swieca_sygnalu'][-8:-3]}`):** "
                    f"Status: **{last_row['status']}** | Ocena: **{last_row['ocena_bota']}** (`{last_row['pnl_pkt']:+.2f} pkt`). "
                    f"Czekamy na nowe potwierdzone otwarcie."
                )
        else:
            st.info("⚪ **CZEKAJ NA OTWARCIE POZYCJI** — Rynek w konsolidacji.")

    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Cena US500", f"{current_price:.2f}")
    c2.metric("Zjazd od szczytu (60 św.)", f"-{drop_from_high:.1f} pkt", delta=f"Do -30 pkt brakuje: {max(0.0, 30.0 - drop_from_high):.1f}")
    c3.metric("Odbicie od dołka (60 św.)", f"+{rally_from_low:.1f} pkt", delta=f"Do +30 pkt brakuje: {max(0.0, 30.0 - rally_from_low):.1f}")
    c4.metric("Spread / ATR", f"{current_spread or 0.0:.2f} / {current_atr:.2f}")
    c5.metric(f"Trend wyższy ({htf_res})", "🟢 WZROSTOWY" if htf_bias > 0 else ("🔴 SPADKOWY" if htf_bias < 0 else "⚪ PŁASKI"))
    c6.metric("Dobrych vs Błędnych", f"{win_rate}% ({wins}W/{losses}L)", delta=f"{total_pts:+.2f} pkt")

    df_plot = df.tail(95)
    long_s = df_plot[df_plot["Signal"] == 1]
    short_s = df_plot[df_plot["Signal"] == -1]
    exit_win_s = df_plot[df_plot["ExitSignal"] == 1]
    exit_sl_s = df_plot[df_plot["ExitSignal"] == -1]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=df_plot.index, open=df_plot["Open"], high=df_plot["High"],
        low=df_plot["Low"], close=df_plot["Close"], name="US500",
        increasing_line_color="#00e676", decreasing_line_color="#ff1744"
    ))
    fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["EMA9"], line=dict(color="#ffea00", width=1.3), name="EMA 9"))
    fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["EMA21"], line=dict(color="#2979ff", width=1.6), name="EMA 21"))
    fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["EMA50"], line=dict(color="#ab47bc", width=1.2, dash="dot"), name="EMA 50 (Trend)"))

    if not long_s.empty:
        fig.add_trace(go.Scatter(
            x=long_s.index, y=long_s["ArrowPrice"], mode="markers+text",
            marker=dict(symbol="triangle-up", size=18, color="#00e676", line=dict(color="white", width=1.2)),
            text=["▲ OTWARCIE LONG" for _ in range(len(long_s))], textposition="bottom center",
            name="Otwarcie LONG"
        ))
    if not short_s.empty:
        fig.add_trace(go.Scatter(
            x=short_s.index, y=short_s["ArrowPrice"], mode="markers+text",
            marker=dict(symbol="triangle-down", size=18, color="#ff1744", line=dict(color="white", width=1.2)),
            text=["▼ OTWARCIE SHORT" for _ in range(len(short_s))], textposition="top center",
            name="Otwarcie SHORT"
        ))
    if not exit_win_s.empty:
        fig.add_trace(go.Scatter(
            x=exit_win_s.index, y=exit_win_s["ExitPrice"], mode="markers+text",
            marker=dict(symbol="diamond", size=16, color="#ffd600", line=dict(color="black", width=1.5)),
            text=exit_win_s["ExitLabel"], textposition="bottom center",
            textfont=dict(color="#ffd600", size=11),
            name="💰 Auto-Zamknięcie w Zysku"
        ))
    if not exit_sl_s.empty:
        fig.add_trace(go.Scatter(
            x=exit_sl_s.index, y=exit_sl_s["ExitPrice"], mode="markers+text",
            marker=dict(symbol="x", size=14, color="#ff5252", line=dict(color="white", width=1.2)),
            text=exit_sl_s["ExitLabel"], textposition="top center",
            name="🛑 Zamknięcie SL"
        ))

    fig.add_annotation(
        x=live_candle_time,
        y=float(live_candle["High"]),
        text="⏳ Świeca w trakcie (Bot czeka)",
        showarrow=True,
        arrowhead=2,
        ax=0,
        ay=-30,
        font=dict(color="#ffea00", size=11),
        bgcolor="rgba(0,0,0,0.7)"
    )

    if not archive_df.empty:
        last_sig_row = archive_df.iloc[-1]
        entry_lvl = float(last_sig_row["wejscie"])
        sl_lvl = float(last_sig_row["sl"])
        tp1_lvl = float(last_sig_row["tp1"])
        tp2_lvl = float(last_sig_row["tp2"])
        fig.add_hline(y=entry_lvl, line_dash="dash", line_color="#ffffff", annotation_text=f"Wejście: {entry_lvl:.2f}", annotation_position="right")
        fig.add_hline(y=sl_lvl, line_dash="dot", line_color="#ff1744", annotation_text=f"SL: {sl_lvl:.2f}", annotation_position="right")
        fig.add_hline(y=tp1_lvl, line_dash="dot", line_color="#00e676", annotation_text=f"TP1 (15 pkt): {tp1_lvl:.2f}", annotation_position="right")
        fig.add_hline(y=tp2_lvl, line_dash="solid", line_color="#00c853", annotation_text=f"🎯 CEL 30 PKT (TP2): {tp2_lvl:.2f}", annotation_position="right")

    fig.update_layout(height=550, template="plotly_dark", xaxis_rangeslider_visible=False, margin=dict(l=15, r=85, t=35, b=15))
    st.plotly_chart(fig, use_container_width=True)

with tab_archive:
    st.subheader("🗄️ Trwałe Archiwum: Co bot postawił, kiedy zamknął i czy sygnał był DOBRY czy BŁĘDNY")
    a_c1, a_c2, a_c3, a_c4 = st.columns(4)
    a_c1.metric("Wszystkie sygnały", len(archive_df))
    a_c2.metric("✅ DOBRE SYGNAŁY (Zysk)", f"{wins}", delta=f"{win_rate}% trafień")
    a_c3.metric("❌ BŁĘDNE SYGNAŁY (Strata)", f"{losses}", delta_color="inverse")
    a_c4.metric("Łączny bilans punktów", f"{total_pts:+.2f} pkt")

    if not archive_df.empty:
        for col_default, default_val in [("ocena_bota", "⏳ W TRAKCIE"), ("cena_wyjscia", 0.0)]:
            if col_default not in archive_df.columns:
                archive_df[col_default] = default_val

        display_cols = [
            "ocena_bota", "swieca_sygnalu", "moment_wejscia", "kierunek",
            "formacja", "wejscie", "cena_wyjscia", "sl", "tp1", "tp2",
            "max_ruch_pkt", "status", "pnl_pkt", "czas_rozliczenia"
        ]
        df_show = archive_df[display_cols].iloc[::-1].rename(columns={
            "ocena_bota": "Czy dobrze obstawił?",
            "swieca_sygnalu": "Świeca sygnałowa",
            "moment_wejscia": "Otwarcie pozycji",
            "kierunek": "Kierunek",
            "formacja": "Typ sygnału",
            "wejscie": "Cena Otwarcia",
            "cena_wyjscia": "Cena Zamknięcia",
            "sl": "Stop Loss",
            "tp1": "TP1 (15 pkt)",
            "tp2": "TP2 (30 pkt)",
            "max_ruch_pkt": "Maks. zysk w trakcie (pkt)",
            "status": "Jak zamknięto?",
            "pnl_pkt": "Wynik (PnL pkt)",
            "czas_rozliczenia": "Godz. zamknięcia"
        })
        st.dataframe(df_show, use_container_width=True)

        b_col1, b_col2 = st.columns(2)
        csv_bytes = df_show.to_csv(index=False).encode("utf-8-sig")
        b_col1.download_button(
            "📥 Pobierz Archiwum do Excela (CSV)",
            data=csv_bytes,
            file_name=f"archiwum_sp500_{now_pl.strftime('%Y%m%d_%H%M')}.csv",
            mime="text/csv",
            use_container_width=True
        )
        if b_col2.button("🗑️ Wyczyść Trwałe Archiwum (Reset od zera)", use_container_width=True):
            st.session_state["persistent_archive"] = []
            save_persistent_archive([])
            st.rerun()
    else:
        st.info("Archiwum jest puste — poczekaj na pierwszy potwierdzony sygnał.")

with tab_audit:
    st.subheader("🧠 Moduł Samo-Audytu AI — Jak bot uczy się na własnych błędach")
    st.markdown(
        "Ten moduł przed każdym odświeżeniem wykresu **bada wszystkie zamknięte transakcje z Archiwum**, "
        "oddziela **✅ Dobre sygnały** od **❌ Błędnych sygnałów** i automatycznie koryguje czułość bota:"
    )
    au1, au2, au3, au4 = st.columns(4)
    au1.metric("Ocena Systemu", audit_info["overall_grade"])
    au2.metric("Próg zmienności ATR", f"{audit_info['min_atr_mult']:.2f}x ATR")
    au3.metric("Próg Auto-Zamykania Zysku", f"+{audit_info['dynamic_exit_min_profit']:.1f} pkt")
    au4.metric("Limity RSI (LONG / SHORT)", f"<{audit_info['rsi_long_max']:.0f} / >{audit_info['rsi_short_min']:.0f}")

    st.markdown("### 📋 Wnioski wyciągnięte przez bota z historii Twoich sygnałów:")
    for lesson in audit_info["lessons"]:
        st.markdown(f"- {lesson}")

with tab_news:
    st.subheader("🌍 Harmonogram Sesji Giełdowych (Czas Polski)")
    st.markdown(
        f"- **🇺🇸 Kontrakty US500 CFD (Capital.com):** Czynne całą dobę od poniedziałku `00:00` do piątku `23:00` (krótka przerwa techniczna codziennie między `22:00` a `23:00` czasu PL).\n"
        f"- **🗽 Giełda Kasowa Wall Street (NYSE / S&P 500 Cash):** Otwiera się o **`{mkt_sched['ny_open_pl']} PL`** i zamyka o **`{mkt_sched['ny_close_pl']} PL`** — wtedy są największe ruchy po **20–40 punktów**!\n"
        f"- **🇪🇺 Giełdy Europejskie (Londyn / Frankfurt):** Otwierają się o **`09:00 PL`** i zamykają o **`17:30 PL`**.\n"
        f"- **🇯🇵 Sesja Azjatycka (Tokio):** Trwa w nocy od **`01:00 PL`** do **`07:00 PL`**."
    )
    st.markdown("---")
    st.subheader("📡 Status kanałów informacyjnych RSS")
    st.write(" | ".join(feed_status))
    st.markdown("---")
    st.subheader("📰 Pełne wiadomości i opisy artykułów")
    for art in articles:
        with st.container():
            st.markdown(f"**{art['impact']} | [{art['source']}] {art['title']}** *(Publikacja: {art['pubDate']})*")
            st.caption(f"📄 **Treść / Opis:** {art['description']}")

st.markdown("---")
st.subheader("🤖 Czat z Analitykiem AI (Widzi świece, Auto-Exit, Samo-Audyt i pełne wiadomości)")

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

q1, q2, q3 = st.columns(3)
quick_p = None
if q1.button("🧠 Przeprowadź pełny audyt błędów bota z Archiwum", use_container_width=True):
    quick_p = "Przeanalizuj tabelę z naszego trwałego archiwum sygnałów. Które sygnały były dobre, a które błędne i dlaczego? Kiedy najlepiej zamykać pozycję w zysku?"
if q2.button("💰 Czy zamykać już bieżącą pozycję z zyskiem?", use_container_width=True):
    quick_p = "Sprawdź ostatnią pozycję oraz ostatnie świece i EMA9. Czy warto już zamknąć zysk, czy trzymać dalej na 30 punktów?"
if q3.button("🌍 Kiedy będzie największy ruch na rynku?", use_container_width=True):
    quick_p = "Sprawdź obecny zegar rynków światowych, sesję USA oraz wiadomości makro. Kiedy spodziewać się największej fali na US500?"

for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_in = st.chat_input("Zadaj pytanie (np. 'oceń czy bot dobrze zamyka pozycje w zysku')...")
final_prompt = quick_p or user_in

if final_prompt:
    st.session_state.chat_history.append({"role": "user", "content": final_prompt})
    with st.chat_message("user"):
        st.markdown(final_prompt)

    candles_txt = df[["Open", "High", "Low", "Close", "EMA9", "EMA21", "EMA50", "RSI", "ATR", "Signal", "ExitSignal"]].tail(10).to_string()
    news_full_txt = "\n".join([f"- [{a['impact']}] {a['title']} -> Opis: {a['description']}" for a in articles[:8]])
    recent_arch_txt = archive_df.tail(10).to_string() if not archive_df.empty else "Brak"

    system_prompt = f"""
    Jesteś głównym analitykiem i audytorem bota S&P 500 (US500).
    Aktualna cena: {current_price:.2f}, Spread: {current_spread} pkt, ATR: {current_atr:.2f}, RSI: {current_rsi:.1f}.
    Sesja giełdy USA (NYSE): {mkt_sched['nyse_cash']}.
    Statystyki z Trwałego Archiwum: {win_rate}% ({wins} dobrych sygnałów, {losses} błędnych sygnałów, {in_play} w grze, bilans: {total_pts:+.2f} pkt).
    Wnioski Samo-Audytu: {audit_info['lessons']}.
    Ostatnie 10 sygnałów z Trwałego Archiwum:
    {recent_arch_txt}
    Ostatnie 10 świec na wykresie:
    {candles_txt}
    Pełne nagłówki ORAZ OPISY artykułów makro/rynkowych:
    {news_full_txt}
    Odpowiadaj konkretnie po polsku:
    1. Ocena sytuacji: OTWIERAĆ (LONG/SHORT), TRZYMAĆ czy ZAMYKAĆ ZYSK.
    2. Audyt ostatnich dobrych i błędnych sygnałów z archiwum.
    3. Konkretne poziomy cenowe.
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
                f"**[Lokalny Raport Samo-Audytu & Rynków]**\n"
                f"- **Bilans Archiwum:** `{wins}` Dobrych sygnałów vs `{losses}` Błędnych sygnałów (`{win_rate}%`, łącznie `{total_pts:+.2f} pkt`).\n"
                f"- **Wnioski Samo-Audytu:** {' '.join(audit_info['lessons'])}\n"
                f"- **Status Giełdy USA (Wall Street):** {mkt_sched['nyse_cash']}"
            )
        st.markdown(reply)
        st.session_state.chat_history.append({"role": "assistant", "content": reply})

if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()
