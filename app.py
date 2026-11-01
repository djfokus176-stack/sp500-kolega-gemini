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
    NY_TZ = datetime.timezone(datetime.timedelta(hours=-5))
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
        tmp_file = ARCHIVE_FILE + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(archive_list, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, ARCHIVE_FILE)
    except Exception:
        pass

if "persistent_archive" not in st.session_state:
    st.session_state["persistent_archive"] = load_persistent_archive()

def trigger_beep_alarm(signal_type="LONG"):
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
            if (ctx.state === 'suspended') {{ ctx.resume(); }}
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

def get_global_markets_schedule():
    now_pl = datetime.datetime.now(PL_TZ)
    now_ny = datetime.datetime.now(NY_TZ)
    now_lon = datetime.datetime.now(LON_TZ)
    now_tok = datetime.datetime.now(TOK_TZ)

    is_weekend = now_ny.weekday() >= 5

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

    lon_hm = now_lon.hour * 60 + now_lon.minute
    lon_open_hm = 8 * 60
    lon_close_hm = 16 * 60 + 30
    if not is_weekend and lon_open_hm <= lon_hm < lon_close_hm:
        mins_left = lon_close_hm - lon_hm
        eu_status = f"🟢 OTWARTY (Zamyka za {mins_left // 60}h {mins_left % 60}m — o 17:30 PL)"
    else:
        eu_status = "🔴 ZAMKNIĘTY (Sesja Europy: 09:00–17:30 PL)"

    if is_weekend:
        cfd_status = "🔴 PRZERWA WEEKENDOWA (Otwiera w nocy z niedzieli na poniedziałek o 00:00 PL)"
    elif 17 * 60 <= ny_hm < 18 * 60:
        mins_to = 18 * 60 - ny_hm
        cfd_status = f"🟡 PRZERWA TECHNICZNA 60 MIN (Wznawia handel za {mins_to} min)"
    else:
        cfd_status = "🟢 OTWARTY 24/5 (Handel na US500 CFD aktywny non-stop)"

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

@st.cache_data(ttl=120, show_spinner=False)
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
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=4)
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
            r = requests.post(url, json=payload, headers=headers, timeout=6)
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
            r = requests.get(url, headers=headers, params=params, timeout=6)
            if r.status_code == 401:
                cst, token, msg = self.get_session_tokens(force_new=True)
                if not cst:
                    return pd.DataFrame(), None, msg
                headers = {"X-CAP-API-KEY": self.api_key, "CST": cst, "X-SECURITY-TOKEN": token}
                r = requests.get(url, headers=headers, params=params, timeout=6)

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
            r = requests.get(url, headers=headers, timeout=5)
            if r.status_code == 200:
                positions = r.json().get("positions", [])
                return [p for p in positions if p.get("market", {}).get("epic") == epic]
        except Exception:
            pass
        return []

    def open_demo_position(self, direction="BUY", size=1.0, stop_level=None, profit_level=None, epic="US500"):
        if self.get_open_positions(epic=epic):
            return False, "⚠️ Pozycja US500 jest już otwarta na koncie."
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
            r = requests.post(url, json=payload, headers=headers, timeout=6)
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
                    r = requests.delete(f"{self.base_url}/api/v1/positions/{deal_id}", headers=headers, timeout=6)
                    if r.status_code == 200:
                        closed_count += 1
                except Exception:
                    pass
        return True, f"💰 Automatycznie zamknięto {closed_count} pozycji US500 w zysku/SL!"

@st.cache_data(ttl=30, show_spinner=False)
def fetch_fallback_data(resolution="MINUTE"):
    if yf is None:
        return pd.DataFrame(), 0.5, "❌ Brak biblioteki yfinance"
    tf_map = {"MINUTE": "1m", "MINUTE_5": "5m", "MINUTE_15": "15m", "HOUR": "1h"}
    interval = tf_map.get(resolution, "5m")
    period = "5d"
    try:
        tkr = yf.Ticker("^GSPC")
        df = tkr.history(period=period, interval=interval, timeout=6)
        if df is None or df.empty:
            return pd.DataFrame(), 0.6, "Brak danych z Yahoo Finance"
        df.dropna(inplace=True)
        idx = pd.to_datetime(df.index)
        if idx.tz is None:
            idx = idx.tz_localize(NY_TZ)
        df.index = idx.tz_convert(PL_TZ).tz_localize(None)
        return df[["Open", "High", "Low", "Close", "Volume"]].tail(220), 0.6, "🟡 TRYB AWARYJNY: Yahoo Finance (Sprawdź klucze Capital.com)"
    except Exception as e:
        return pd.DataFrame(), 0.6, f"Błąd fallbacku: {e}"

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
    df["ExitSignal"] = 0
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

    for i in range(5, len(df) - 1):
        o, h, l, c = float(df["Open"].iloc[i]), float(df["High"].iloc[i]), float(df["Low"].iloc[i]), float(df["Close"].iloc[i])
        po, ph, pl, pc = float(df["Open"].iloc[i - 1]), float(df["High"].iloc[i - 1]), float(df["Low"].iloc[i - 1]), float(df["Close"].iloc[i - 1])
        ema9, ema21, ema50 = float(df["EMA9"].iloc[i]), float(df["EMA21"].iloc[i]), float(df["EMA50"].iloc[i])
        ema9_prev3 = float(df["EMA9"].iloc[i - 3])
        ema21_prev3 = float(df["EMA21"].iloc[i - 3])
        rsi = float(df["RSI"].iloc[i])
        atr = max(float(df["ATR"].iloc[i]), 0.5)
        bar_htf_bias = int(df["HTF_Bias"].iloc[i])

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

                if l <= p_sl:
                    if active_pos["hit_tp1"]:
                        df.iloc[i, df.columns.get_loc("ExitSignal")] = 1
                        df.iloc[i, df.columns.get_loc("ExitPrice")] = p_sl
                        df.iloc[i, df.columns.get_loc("ExitLabel")] = f"💰 ZAMKNIJ ZYSK (+{p_sl - p_entry:.1f} pkt)"
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

                if h >= p_tp1 and not active_pos["hit_tp1"]:
                    active_pos["hit_tp1"] = True
                    active_pos["sl"] = round(p_entry + 1.0, 2)

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

            else:
                fav = p_entry - l
                if fav > active_pos["max_fav"]:
                    active_pos["max_fav"] = fav

                if h >= p_sl:
                    if active_pos["hit_tp1"]:
                        df.iloc[i, df.columns.get_loc("ExitSignal")] = 1
                        df.iloc[i, df.columns.get_loc("ExitPrice")] = p_sl
                        df.iloc[i, df.columns.get_loc("ExitLabel")] = f"💰 ZAMKNIJ ZYSK (+{p_entry - p_sl:.1f} pkt)"
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

                if l <= p_tp1 and not active_pos["hit_tp1"]:
                    active_pos["hit_tp1"] = True
                    active_pos["sl"] = round(p_entry - 1.0, 2)

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

def sync_and_evaluate_persistent_archive(df, resolution, learned_params=None):
    if learned_params is None:
        learned_params = {"dynamic_exit_min_profit": 7.0}
    dyn_trig = float(learned_params.get("dynamic_exit_min_profit", 7.0))

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
        cur_sl = round(entry + 1.0, 2) if (hit_tp1 and sig == 1) else (round(entry - 1.0, 2) if (hit_tp1 and sig == -1) else sl)

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
                if lj <= cur_sl:
                    if hit_tp1:
                        item["status"] = f"🟢 ZABEZPIECZONY BE (+{cur_sl - entry:.1f} pkt)"
                        item["pnl_pkt"] = round(cur_sl - entry, 2)
                        item["cena_wyjscia"] = cur_sl
                    else:
                        item["status"] = "❌ STOP LOSS"
                        item["pnl_pkt"] = round(cur_sl - entry, 2)
                        item["cena_wyjscia"] = cur_sl
                    item["czas_rozliczenia"] = ts_str
                    break
                elif hj >= tp2:
                    item["status"] = f"✅ PEŁNE TP2 (+{tp2 - entry:.1f} PKT!)"
                    item["pnl_pkt"] = round(tp2 - entry, 2)
                    item["cena_wyjscia"] = tp2
                    item["czas_rozliczenia"] = ts_str
                    break
                if hj >= tp1 and not hit_tp1:
                    hit_tp1 = True
                    cur_sl = round(entry + 1.0, 2)
                cur_pnl = cj - entry
                if max_fav >= dyn_trig and cur_pnl >= 3.5 and ((cj < ema9_j and cj < oj) or (max_fav >= 14.0 and cur_pnl <= max_fav * 0.68)):
                    item["status"] = f"💰 AUTO-ZAMKNIĘCIE W ZYS
