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
    if now_ny.weekday() >=
