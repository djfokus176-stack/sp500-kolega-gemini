import os
import json
import time
import requests
import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit.components.v1 as components

# Opcjonalny import OpenAI oraz yfinance (jako fallback testowy)
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import yfinance as yf
except ImportError:
    yf = None

# ==========================================
# KONFIGURACJA STRONY STREAMLIT
# ==========================================
st.set_page_config(
    page_title="SP500 SIGNAL AI PRO - Wersja Kolega Gemini",
    page_icon="⚡",
    layout="wide"
)

KEYS_CACHE_FILE = ".saved_api_keys.json"

def load_cached_keys():
    """Wczytuje zapisane klucze z pliku tymczasowego lub st.secrets, żeby nie znikały po F5."""
    data = {
        "is_demo": True,
        "cap_key": "",
        "cap_id": "",
        "cap_pass": "",
        "openai_key": ""
    }
    # 1. Próba z env / st.secrets
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

    # 2. Próba z zapisanego pliku po kliknięciu "ZALOGUJ I ZAPISZ"
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

def save_cached_keys(is_demo, cap_key, cap_id, cap_pass, openai_key):
    """Zapisuje klucze na dysku serwera oraz w session_state."""
    payload = {
        "is_demo": is_demo,
        "cap_key": cap_key.strip(),
        "cap_id": cap_id.strip(),
        "cap_pass": cap_pass.strip(),
        "openai_key": openai_key.strip()
    }
    st.session_state["saved_keys"] = payload
    try:
        with open(KEYS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass

# Inicjalizacja kluczy w pamięci sesji
if "saved_keys" not in st.session_state:
    st.session_state["saved_keys"] = load_cached_keys()

# ==========================================
# MODUŁ DŹWIĘKOWY (PISZCZENIE W PRZEGLĄDARCE)
# ==========================================
def trigger_beep_alarm(signal_type="LONG"):
    """Generuje głośny sygnał dźwiękowy bezpośrednio w przeglądarce (Web Audio API)."""
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
        }} catch (e) {{
            console.log("Audio zablokowane przez przeglądarkę - kliknij w stronę.", e);
        }
    }})();
    </script>
    """
    components.html(html_code, height=0, width=0)


# ==========================================
# KLIENT API CAPITAL.COM (US500 CFD)
# ==========================================
class CapitalComClient:
    def __init__(self, api_key, identifier, password, is_demo=True):
        self.base_url = "https://demo-api-capital.backend-capital.com" if is_demo else "https://api-capital.backend-capital.com"
        self.api_key = api_key
        self.identifier = identifier
        self.password = password
        self.cst = None
        self.security_token = None

    def authenticate(self):
        if not self.api_key or not self.identifier or not self.password:
            return False, "Brak wpisanych kluczy Capital.com"
        url = f"{self.base_url}/api/v1/session"
        headers = {
            "X-CAP-API-KEY": self.api_key,
            "Content-Type": "application/json"
        }
        payload = {
            "identifier": self.identifier,
            "password": self.password,
            "encryptedPassword": False
        }
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=8)
            if r.status_code == 200:
                self.cst = r.headers.get("CST")
                self.security_token = r.headers.get("X-SECURITY-TOKEN")
                return True, "✅ Zalogowano do Capital.com API"
            return False, f"❌ Błąd logowania ({r.status_code}): Sprawdź API Key, Email lub Hasło API"
        except Exception as e:
            return False, f"❌ Błąd sieci Capital.com: {e}"

    def fetch_candles(self, epic="US500", resolution="MINUTE", max_bars=120):
        if not self.cst or not self.security_token:
            ok, msg = self.authenticate()
            if not ok:
                return pd.DataFrame(), None, msg

        url = f"{self.base_url}/api/v1/prices/{epic}"
        headers = {
            "X-CAP-API-KEY": self.api_key,
            "CST": self.cst,
            "X-SECURITY-TOKEN": self.security_token
        }
        params = {"resolution": resolution, "max": max_bars}

        try:
            r = requests.get(url, headers=headers, params=params, timeout=8)
            if r.status_code != 200:
                return pd.DataFrame(), None, f"Błąd pobierania świec ({r.status_code})"

            data = r.json()
            prices = data.get("prices", [])
            if not prices:
                return pd.DataFrame(), None, "Pusta lista świec z Capital.com"

            rows = []
            last_spread = 0.0
            for p in prices:
                bid_c = p["closePrice"]["bid"]
                ask_c = p["closePrice"]["ask"]
                last_spread = round(ask_c - bid_c, 2)
                rows.append({
                    "Time": pd.to_datetime(p["snapshotTimeUTC"]),
                    "Open": (p["openPrice"]["bid"] + p["openPrice"]["ask"]) / 2.0,
                    "High": (p["highPrice"]["bid"] + p["highPrice"]["ask"]) / 2.0,
                    "Low": (p["lowPrice"]["bid"] + p["lowPrice"]["ask"]) / 2.0,
                    "Close": (bid_c + ask_c) / 2.0,
                    "Volume": p.get("lastTradedVolume", 0),
                    "Bid": bid_c,
                    "Ask": ask_c
                })

            df = pd.DataFrame(rows)
            df.set_index("Time", inplace=True)
            return df, last_spread, "🟢 OK (Capital.com Live)"
        except Exception as e:
            return pd.DataFrame(), None, f"Wyjątek API: {e}"


def fetch_fallback_data(resolution="MINUTE"):
    """Tryb demonstracyjny (Yahoo Finance), gdy użytkownik nie wpisał jeszcze kluczy API."""
    if yf is None:
        return pd.DataFrame(), 0.5, "Zainstaluj yfinance lub podaj klucze Capital.com"
    tf_map = {"MINUTE": "1m", "MINUTE_5": "5m", "MINUTE_15": "15m", "MINUTE_30": "30m", "HOUR": "1h"}
    interval = tf_map.get(resolution, "5m")
    period = "1d" if interval == "1m" else "5d"
    try:
        df = yf.download("^GSPC", period=period, interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.dropna(inplace=True)
        return df.tail(120), 0.4, "🟡 Tryb Demo (Yahoo Finance — zaloguj się do Capital.com w panelu bocznym)"
    except Exception as e:
        return pd.DataFrame(), 0.5, f"Błąd danych: {e}"


# ==========================================
# SILNIK SCALPINGOWY: ŚWIECE + STRZAŁKI
# ==========================================
def detect_scalping_signals(df_main, df_htf=None, sensitivity="Standard"):
    df = df_main.copy()

    df["EMA9"] = df["Close"].ewm(span=9, adjust=False).mean()
    df["EMA21"] = df["Close"].ewm(span=21, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=50, adjust=False).mean()

    delta = df["Close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))
    df["RSI"] = df["RSI"].fillna(50)

    high_low = df["High"] - df["Low"]
    high_close = np.abs(df["High"] - df["Close"].shift())
    low_close = np.abs(df["Low"] - df["Close"].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean().bfill()

    htf_bias = 0
    if df_htf is not None and len(df_htf) > 25:
        htf_ema9 = df_htf["Close"].ewm(span=9, adjust=False).mean().iloc[-1]
        htf_ema21 = df_htf["Close"].ewm(span=21, adjust=False).mean().iloc[-1]
        htf_bias = 1 if htf_ema9 >= htf_ema21 else -1

    df["Signal"] = 0
    df["Pattern"] = ""
    df["ArrowPrice"] = np.nan

    wick_ratio = 1.6 if sensitivity == "Wysoka (Więcej strzałek)" else 2.0

    for i in range(2, len(df)):
        o, h, l, c = df["Open"].iloc[i], df["High"].iloc[i], df["Low"].iloc[i], df["Close"].iloc[i]
        po, ph, pl, pc = df["Open"].iloc[i - 1], df["High"].iloc[i - 1], df["Low"].iloc[i - 1], df["Close"].iloc[i - 1]
        ema9, ema21 = df["EMA9"].iloc[i], df["EMA21"].iloc[i]
        rsi = df["RSI"].iloc[i]
        atr = max(df["ATR"].iloc[i], 0.5)

        body = abs(c - o)
        rng = max(h - l, 0.01)
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        bull_pinbar = (lower_wick >= body * wick_ratio) and (lower_wick >= rng * 0.45) and (c >= o or lower_wick > upper_wick * 2.5)
        bull_engulfing = (pc < po) and (c > o) and (c > po) and (o <= pc) and (body > abs(pc - po) * 0.8)
        bull_impulse = (c > o) and (body > atr * 0.85) and (c > ema9) and (po <= ema9)

        bear_pinbar = (upper_wick >= body * wick_ratio) and (upper_wick >= rng * 0.45) and (c <= o or upper_wick > lower_wick * 2.5)
        bear_engulfing = (pc > po) and (c < o) and (c < po) and (o >= pc) and (body > abs(pc - po) * 0.8)
        bear_impulse = (c < o) and (body > atr * 0.85) and (c < ema9) and (po >= ema9)

        trend_up = (ema9 >= ema21) or (rsi < 38)
        trend_down = (ema9 <= ema21) or (rsi > 62)

        if sensitivity == "Tylko Pewniaki (M1 + M5 zgodne)" and htf_bias != 0:
            trend_up = trend_up and (htf_bias == 1)
            trend_down = trend_down and (htf_bias == -1)

        prev_sig = df["Signal"].iloc[i - 1]

        if (bull_pinbar or bull_engulfing or bull_impulse) and trend_up and rsi < 72 and prev_sig != 1:
            df.iloc[i, df.columns.get_loc("Signal")] = 1
            df.iloc[i, df.columns.get_loc("ArrowPrice")] = l - (atr * 0.35)
            if bull_engulfing:
                df.iloc[i, df.columns.get_loc("Pattern")] = "Objęcie Hossy (Engulfing)"
            elif bull_pinbar:
                df.iloc[i, df.columns.get_loc("Pattern")] = "Pinbar Wzrostowy (Młot)"
            else:
                df.iloc[i, df.columns.get_loc("Pattern")] = "Impuls Wzrostowy EMA"

        elif (bear_pinbar or bear_engulfing or bear_impulse) and trend_down and rsi > 28 and prev_sig != -1:
            df.iloc[i, df.columns.get_loc("Signal")] = -1
            df.iloc[i, df.columns.get_loc("ArrowPrice")] = h + (atr * 0.35)
            if bear_engulfing:
                df.iloc[i, df.columns.get_loc("Pattern")] = "Objęcie Bessy (Engulfing)"
            elif bear_pinbar:
                df.iloc[i, df.columns.get_loc("Pattern")] = "Pinbar Spadkowy (Gwiazda)"
            else:
                df.iloc[i, df.columns.get_loc("Pattern")] = "Impuls Spadkowy EMA"

    return df, htf_bias


# ==========================================
# PANEL BOCZNY: USTAWIENIA I PRZYCISK LOGOWANIA
# ==========================================
saved = st.session_state["saved_keys"]

with st.sidebar:
    st.title("⚡ Kolega Gemini PRO")
    st.caption("Scalping US500 • Strzałki • Alarm • Czat AI")

    with st.expander("🔑 Klucze API i Logowanie", expanded=not bool(saved.get("cap_key"))):
        with st.form("api_login_form", clear_on_submit=False):
            is_demo_in = st.checkbox("Konto DEMO Capital.com", value=saved.get("is_demo", True))
            cap_key_in = st.text_input("Capital API Key:", value=saved.get("cap_key", ""), type="password")
            cap_id_in = st.text_input("Capital Login (Email):", value=saved.get("cap_id", ""))
            cap_pass_in = st.text_input("Capital Hasło API:", value=saved.get("cap_pass", ""), type="password")
            openai_key_in = st.text_input("OpenAI API Key (do czatu):", value=saved.get("openai_key", ""), type="password")

            login_submitted = st.form_submit_button("🔐 ZALOGUJ I ZAPISZ KLUCZE", use_container_width=True, type="primary")

        if login_submitted:
            save_cached_keys(is_demo_in, cap_key_in, cap_id_in, cap_pass_in, openai_key_in)
            saved = st.session_state["saved_keys"]
            test_client = CapitalComClient(saved["cap_key"], saved["cap_id"], saved["cap_pass"], is_demo=saved["is_demo"])
            ok, auth_msg = test_client.authenticate()
            if ok:
                st.success(auth_msg)
            else:
                st.error(auth_msg)

        if saved.get("cap_key") and saved.get("cap_id"):
            st.caption(f"📌 Zapisany login: `{saved.get('cap_id')}`")
            if st.button("🗑️ Wyloguj / Wyczyść klucze", use_container_width=True):
                save_cached_keys(True, "", "", "", "")
                if os.path.exists(KEYS_CACHE_FILE):
                    os.remove(KEYS_CACHE_FILE)
                st.rerun()

    st.markdown("---")
    st.subheader("1. Parametry Scalpingu")
    resolution = st.selectbox(
        "Główny interwał wykresu:",
        ["MINUTE", "MINUTE_5", "MINUTE_15"],
        index=0,
        help="MINUTE = świece 1-minutowe, MINUTE_5 = 5-minutowe"
    )
    sensitivity = st.radio(
        "Czułość strzałek:",
        ["Standard", "Wysoka (Więcej strzałek)", "Tylko Pewniaki (M1 + M5 zgodne)"],
        index=0
    )
    max_spread = st.slider("Maksymalny akceptowalny Spread (pkt):", 0.2, 2.0, 0.8, 0.1)

    st.subheader("2. Dźwięk i Odświeżanie")
    sound_enabled = st.toggle("🔊 Piszczenie przy nowym sygnale", value=True)
    if st.button("🔊 Testuj Piszczenie (Sprawdź głośniki)", use_container_width=True):
        trigger_beep_alarm("LONG")
        st.toast("Odtworzono sygnał dźwiękowy!")

    auto_refresh = st.toggle("🔄 Auto-odświeżanie wykresu", value=False)
    refresh_sec = st.slider("Odświeżaj co (sekundy):", 5, 60, 15)


# ==========================================
# POBRANIE DANYCH I ANALIZA
# ==========================================
st.title("🎯 SP500 SIGNAL AI PRO — Wersja Kolega Gemini")

cap_key = saved.get("cap_key", "")
cap_id = saved.get("cap_id", "")
cap_pass = saved.get("cap_pass", "")
is_demo = saved.get("is_demo", True)
openai_key = saved.get("openai_key", "")

client = CapitalComClient(cap_key, cap_id, cap_pass, is_demo=is_demo)
df_raw, current_spread, source_status = client.fetch_candles(epic="US500", resolution=resolution, max_bars=120)

htf_res = "MINUTE_5" if resolution == "MINUTE" else "MINUTE_15"
df_htf, _, _ = client.fetch_candles(epic="US500", resolution=htf_res, max_bars=60)

if df_raw.empty:
    if cap_key or cap_id or cap_pass:
        st.warning(f"⚠️ Problem z połączeniem Capital.com: **{source_status}**. Przełączono tymczasowo na wykres awaryjny.")
    df_raw, current_spread, source_status = fetch_fallback_data(resolution)
    df_htf, _, _ = fetch_fallback_data(htf_res)

if df_raw.empty or len(df_raw) < 25:
    st.error(f"Nie udało się pobrać świec: {source_status}")
    st.stop()

df, htf_bias = detect_scalping_signals(df_raw, df_htf, sensitivity=sensitivity)

closed_candle = df.iloc[-2]
live_candle = df.iloc[-1]
current_price = float(live_candle["Close"])
current_atr = float(closed_candle["ATR"])
current_rsi = float(closed_candle["RSI"])

active_sig = int(closed_candle["Signal"])
active_pattern = closed_candle["Pattern"]
sig_time = str(df.index[-2])

if active_sig == 0 and int(live_candle["Signal"]) != 0:
    active_sig = int(live_candle["Signal"])
    active_pattern = f"{live_candle['Pattern']} (Na żywo!)"
    sig_time = str(df.index[-1])

spread_ok = (current_spread is None) or (current_spread <= max_spread)

# ==========================================
# OBSŁUGA ALARMU DŹWIĘKOWEGO
# ==========================================
if "last_beep_candle" not in st.session_state:
    st.session_state.last_beep_candle = None

if active_sig != 0 and spread_ok:
    if sound_enabled and st.session_state.last_beep_candle != sig_time:
        trigger_beep_alarm("LONG" if active_sig == 1 else "SHORT")
        st.session_state.last_beep_candle = sig_time

# ==========================================
# GÓRNY PANEL SYGNAŁOWY (CO GRAĆ TERAZ)
# ==========================================
if active_sig == 1 and spread_ok:
    sl_price = round(min(closed_candle["Low"], live_candle["Low"]) - (current_atr * 0.5), 2)
    risk_pts = max(current_price - sl_price, 1.2)
    tp_price = round(current_price + (risk_pts * 1.8), 2)
    st.success(
        f"### 🟢 STRZAŁKA W GÓRĘ: WCHODŹ LONG (KUP) NA US500!\n"
        f"**Formacja:** `{active_pattern}` | **Wejście:** `{current_price:.2f}` | "
        f"**Stop Loss:** `{sl_price:.2f}` (-{risk_pts:.1f} pkt) | **Take Profit:** `{tp_price:.2f}` (+{risk_pts*1.8:.1f} pkt)"
    )
elif active_sig == -1 and spread_ok:
    sl_price = round(max(closed_candle["High"], live_candle["High"]) + (current_atr * 0.5), 2)
    risk_pts = max(sl_price - current_price, 1.2)
    tp_price = round(current_price - (risk_pts * 1.8), 2)
    st.error(
        f"### 🔴 STRZAŁKA W DÓŁ: WCHODŹ SHORT (SPRZEDAJ) NA US500!\n"
        f"**Formacja:** `{active_pattern}` | **Wejście:** `{current_price:.2f}` | "
        f"**Stop Loss:** `{sl_price:.2f}` (-{risk_pts:.1f} pkt) | **Take Profit:** `{tp_price:.2f}` (+{risk_pts*1.8:.1f} pkt)"
    )
else:
    past_signals = df[df["Signal"] != 0]
    if not past_signals.empty:
        last_s = past_signals.iloc[-1]
        dir_txt = "🟢 LONG" if last_s["Signal"] == 1 else "🔴 SHORT"
        st.info(
            f"⚪ **CZEKAJ NA NOWĄ STRZAŁKĘ** — Na bieżącej świecy brak nowego sygnału. "
            f"Ostatnia strzałka na wykresie: **{dir_txt}** ({last_s['Pattern']}) przy cenie `{last_s['Close']:.2f}`."
        )
    else:
        st.info("⚪ **CZEKAJ** — Rynek bez wyraźnego układu świecowego.")

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Cena US500", f"{current_price:.2f}")
c2.metric("Spread (Bid/Ask)", f"{current_spread:.2f} pkt" if current_spread else "Brak", delta="OK" if spread_ok else "ZA DUŻY!", delta_color="normal" if spread_ok else "inverse")
c3.metric("Zmienność ATR (14)", f"{current_atr:.2f} pkt")
c4.metric("RSI (14)", f"{current_rsi:.1f}")
c5.metric(f"Trend wyższy ({htf_res})", "🟢 WZROSTOWY" if htf_bias >= 0 else "🔴 SPADKOWY")
st.caption(f"Źródło danych: **{source_status}**")

# ==========================================
# WYKRES ŚWIECOWY ZE STRZAŁKAMI
# ==========================================
df_plot = df.tail(80)
long_signals = df_plot[df_plot["Signal"] == 1]
short_signals = df_plot[df_plot["Signal"] == -1]

fig = go.Figure()

fig.add_trace(go.Candlestick(
    x=df_plot.index,
    open=df_plot["Open"],
    high=df_plot["High"],
    low=df_plot["Low"],
    close=df_plot["Close"],
    name="US500",
    increasing_line_color="#00e676",
    decreasing_line_color="#ff1744"
))

fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["EMA9"], line=dict(color="#ffea00", width=1.3), name="EMA 9 (Szybka)"))
fig.add_trace(go.Scatter(x=df_plot.index, y=df_plot["EMA21"], line=dict(color="#2979ff", width=1.6), name="EMA 21 (Trend)"))

if not long_signals.empty:
    fig.add_trace(go.Scatter(
        x=long_signals.index,
        y=long_signals["ArrowPrice"],
        mode="markers+text",
        marker=dict(symbol="triangle-up", size=18, color="#00e676", line=dict(color="white", width=1.5)),
        text=["▲ LONG" for _ in range(len(long_signals))],
        textposition="bottom center",
        textfont=dict(color="#00e676", size=11, family="Arial Black"),
        hovertext=long_signals["Pattern"],
        name="Sygnał LONG"
    ))

if not short_signals.empty:
    fig.add_trace(go.Scatter(
        x=short_signals.index,
        y=short_signals["ArrowPrice"],
        mode="markers+text",
        marker=dict(symbol="triangle-down", size=18, color="#ff1744", line=dict(color="white", width=1.5)),
        text=["▼ SHORT" for _ in range(len(short_signals))],
        textposition="top center",
        textfont=dict(color="#ff1744", size=11, family="Arial Black"),
        hovertext=short_signals["Pattern"],
        name="Sygnał SHORT"
    ))

fig.update_layout(
    height=540,
    template="plotly_dark",
    xaxis_rangeslider_visible=False,
    margin=dict(l=15, r=15, t=35, b=15),
    title=f"Wykres Scalpingowy US500 ({resolution}) — Strzałki na zamkniętych świecach",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
)

st.plotly_chart(fig, use_container_width=True)

# ==========================================
# CZAT-BOT AI PODPIĘTY POD ŚWIECE
# ==========================================
st.markdown("---")
st.subheader("🤖 Czat z Kolegą AI (Bot widzi powyższe świece i strzałki na żywo)")

if "chat_history" not in st.session_state:
    st.session_state.chat_history = []

q_col1, q_col2, q_col3 = st.columns(3)
quick_prompt = None
if q_col1.button("⚡ Oceń ostatnią strzałkę — wchodzić?", use_container_width=True):
    quick_prompt = "Oceń ostatnią strzałkę na wykresie i obecne świece. Czy warto teraz wchodzić w pozycję na scalping i gdzie dać ciasny Stop Loss?"
if q_col2.button("🕯️ Co mówią cienie (knoty) ostatnich 5 świec?", use_container_width=True):
    quick_prompt = "Przeanalizuj dokładnie korpusy i knoty ostatnich 5 świec na US500. Kto ma przewagę: kupujący czy sprzedający?"
if q_col3.button("🛑 Czy rynek nie jest w konsolidacji (szumie)?", use_container_width=True):
    quick_prompt = "Sprawdź ATR, spread oraz układ średnich EMA9/EMA21. Czy mamy czytelny ruch pod scalping, czy lepiej przeczekać szum?"

for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

user_input = st.chat_input("Napisz do bota (np. 'Wchodzić w ten ruch czy czekać na korektę do EMA9?')...")
final_prompt = quick_prompt or user_input

if final_prompt:
    st.session_state.chat_history.append({"role": "user", "content": final_prompt})
    with st.chat_message("user"):
        st.markdown(final_prompt)

    recent_table = df[["Open", "High", "Low", "Close", "EMA9", "EMA21", "RSI", "ATR", "Signal", "Pattern"]].tail(8).to_string()
    system_prompt = f"""
    Jesteś ekspertem od scalpingu na kontrakcie S&P 500 (US500 CFD na Capital.com).
    Analizujesz wykres na interwale {resolution}.
    Aktualna cena: {current_price:.2f}, Spread: {current_spread} pkt, ATR: {current_atr:.2f} pkt, RSI: {current_rsi:.1f}.
    Trend wyższego interwału ({htf_res}): {"WZROSTOWY" if htf_bias >= 0 else "SPADKOWY"}.
    Ostatnie 8 świec wraz z wykrytymi strzałkami (Signal: 1=LONG, -1=SHORT, 0=Brak):
    {recent_table}
    Odpowiadaj po żołniersku, krótko i konkretnie pod szybki scalping:
    1. Decyzja: WCHODZIĆ (LONG/SHORT) czy CZEKAJ.
    2. Co pokazują knoty i korpusy ostatnich świec.
    3. Dokładny poziom wejścia, Stop Loss i Take Profit w punktach S&P 500.
    """

    with st.chat_message("assistant"):
        if openai_key and OpenAI is not None:
            try:
                ai_client = OpenAI(api_key=openai_key)
                resp = ai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        *st.session_state.chat_history[-6:]
                    ],
                    temperature=0.3
                )
                reply = resp.choices[0].message.content
            except Exception as e:
                reply = f"⚠️ Błąd klucza OpenAI API: `{e}`"
        else:
            last_5 = df.tail(5)
            bull_candles = sum(last_5["Close"] > last_5["Open"])
            avg_range = (last_5["High"] - last_5["Low"]).mean()
            dir_advice = "🟢 **Przewaga popytu (LONG)**" if closed_candle["EMA9"] > closed_candle["EMA21"] else "🔴 **Przewaga podaży (SHORT)**"
            reply = (
                f"**[Analiza Lokalna Silnika Świec US500]**\n"
                f"- **Układ średnich i pędu:** {dir_advice} (EMA9 = `{closed_candle['EMA9']:.2f}`, EMA21 = `{closed_candle['EMA21']:.2f}`, RSI = `{current_rsi:.1f}`).\n"
                f"- **Zachowanie ostatnich 5 świec:** {bull_candles}/5 świec wzrostowych, średni rozmach świecy wynosi `{avg_range:.2f}` pkt (ATR: `{current_atr:.2f}` pkt).\n"
                f"- **Wskazówka:** {'Mamy aktywną strzałkę `' + active_pattern + '` — pilnuj ciasnego SL za knotem świecy!' if active_sig != 0 else 'Brak świeżej strzałki na zamknięciu ostatniej świecy — poczekaj na dojście ceny do EMA9 i wyraźny Pinbar lub Objęcie.'}\n\n"
                f"*(Wpisz klucz OpenAI w panelu bocznym i kliknij ZALOGUJ I ZAPISZ KLUCZE, aby odblokować pełny czat GPT).* "
            )
        st.markdown(reply)
        st.session_state.chat_history.append({"role": "assistant", "content": reply})

if auto_refresh:
    time.sleep(refresh_sec)
    st.rerun()
