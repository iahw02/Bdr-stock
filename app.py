# -*- coding: utf-8 -*-
"""استراتيجية فيصل - فحص وفرز الأسهم الأمريكية
فحص السوق كامل: التقسيم العكسي، المؤشرات الفنية (RSI/MACD)، الاقتراض، الدعم.
"""
import io
import ftplib
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
import requests
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

st.set_page_config(page_title="استراتيجية فيصل", page_icon="📈", layout="centered")

# ----------------------------------------------------------------- الشكل
st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700&display=swap');
html, body, [class*="st-"], .stApp { font-family: 'Tajawal', sans-serif; }
.stApp { direction: rtl; background: #fbfefc; }
.block-container { padding-top: 1.2rem; max-width: 760px; }
h1, h2, h3, label, p, div[data-testid="stMarkdownContainer"] { text-align: right; }
.hero { background: linear-gradient(135deg,#e3f5ea,#cfeedd); border-radius: 18px;
        padding: 22px 20px; margin-bottom: 14px; border: 1px solid #bfe3cf; }
.hero h1 { margin: 0 0 6px 0; font-size: 1.6rem; color: #17382b; }
.hero span { color: #4d6b5c; font-size: .85rem; }
div[data-testid="stExpander"] { border: 1px solid #bfe3cf; border-radius: 14px; background: #f4fbf7; }
div[data-testid="stExpander"] summary { background: #cdeedd; border-radius: 14px; font-weight: 700; }
.stButton > button { width: 100%; border-radius: 12px; font-weight: 700; padding: .6rem; border: none; }
.stButton > button[kind="primary"] { background: #3fbf7f; color: #0e2a1c; }
div[data-testid="stDataFrame"] { direction: ltr; }
</style>
<div class="hero">
  <h1>استراتيجية فيصل - فحص وفرز الأسهم</h1>
  <span>التقسيم العكسي · المؤشرات الفنية · الاقتراض · متابعة الفحص المباشر</span>
</div>
""",
    unsafe_allow_html=True,
)

# ----------------------------------------------------------------- المؤشرات
def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + ru / rd.replace(0, np.nan))


def macd_line(close: pd.Series) -> pd.Series:
    return close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()


def support_stats(df: pd.DataFrame, window: int, band: float):
    """الدعم = أدنى سعر في نافذة البحث. نحسب كم جلسة لمس أدناها نطاق ±band من الدعم."""
    w = df.tail(window)
    if len(w) < window:
        return None
    sup = float(w["Low"].min())
    if sup <= 0:
        return None
    touches = int((w["Low"] <= sup * (1 + band)).sum())
    last = float(df["Close"].iloc[-1])
    return sup, touches, (last / sup - 1) * 100


# ----------------------------------------------------------------- البيانات
TF = {
    "يومي": ("1d", "1y"),
    "أسبوعي": ("1wk", "3y"),
    "ساعة": ("1h", "60d"),
}


@st.cache_data(ttl=86400, show_spinner=False)
def load_universe() -> pd.DataFrame:
    """قائمة كل الأسهم الأمريكية من NASDAQ Trader."""
    base = "https://www.nasdaqtrader.com/dynamic/SymDir/"
    nas = pd.read_csv(io.StringIO(requests.get(base + "nasdaqlisted.txt", timeout=40).text), sep="|")
    oth = pd.read_csv(io.StringIO(requests.get(base + "otherlisted.txt", timeout=40).text), sep="|")
    nas = nas.rename(columns={"Symbol": "symbol", "Security Name": "name"})
    nas["market"] = "Nasdaq"
    oth = oth.rename(columns={"ACT Symbol": "symbol", "Security Name": "name"})
    oth["market"] = oth["Exchange"].map({"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe"})
    df = pd.concat([nas[["symbol", "name", "market", "ETF", "Test Issue"]],
                    oth[["symbol", "name", "market", "ETF", "Test Issue"]]])
    df = df.dropna(subset=["symbol"])
    df = df[(df["ETF"] == "N") & (df["Test Issue"] == "N")]
    df = df[df["symbol"].astype(str).str.fullmatch(r"[A-Z]{1,5}")]
    bad = r"warrant|right|unit|preferred|depositary|notes|debenture"
    df = df[~df["name"].str.contains(bad, case=False, regex=True, na=False)]
    return df.drop_duplicates("symbol").reset_index(drop=True)[["symbol", "name", "market"]]


@st.cache_data(ttl=3600, show_spinner=False)
def load_shortable() -> dict:
    """الأسهم المتاحة للاقتراض (قائمة Interactive Brokers العامة)."""
    try:
        ftp = ftplib.FTP("ftp3.interactivebrokers.com", timeout=40)
        ftp.login("shortstock")
        buf = io.BytesIO()
        ftp.retrbinary("RETR usa.txt", buf.write)
        ftp.quit()
    except Exception:
        return {}
    out = {}
    for line in buf.getvalue().decode(errors="ignore").splitlines():
        if not line or line.startswith("#") or line.startswith("BOF") or line.startswith("EOF"):
            continue
        p = line.split("|")
        if len(p) < 8:
            continue
        digits = re.sub(r"[^0-9]", "", p[7])
        if not digits:
            continue
        try:
            fee = float(p[6])
        except ValueError:
            fee = None
        out[p[0]] = {"avail": int(digits), "fee": fee}
    return out


def download(tickers: list, tf: str) -> dict:
    interval, period = TF[tf]
    data = yf.download(tickers, period=period, interval=interval, group_by="ticker",
                       auto_adjust=True, threads=True, progress=False)
    res = {}
    if data is None or data.empty:
        return res
    if isinstance(data.columns, pd.MultiIndex):
        lvl0 = set(data.columns.get_level_values(0))
        for t in tickers:
            if t in lvl0:
                d = data[t].dropna(subset=["Close"])
                if len(d):
                    res[t] = d
    else:
        d = data.dropna(subset=["Close"])
        if len(d) and len(tickers) == 1:
            res[tickers[0]] = d
    return res


def get_info(sym: str) -> dict:
    out = {"mcap": None, "reverse_split": None}
    try:
        t = yf.Ticker(sym)
        try:
            fi = t.fast_info
            out["mcap"] = fi.get("marketCap") or fi.get("market_cap")
        except Exception:
            pass
        sp = t.splits
        if sp is not None and len(sp):
            rev = sp[sp < 1]
            if len(rev):
                out["reverse_split"] = rev.index[-1].tz_localize(None) if rev.index.tz else rev.index[-1]
    except Exception:
        pass
    return out


MCAP = {
    "أي قيمة": (0, float("inf")),
    "مايكرو (أقل من 300 مليون)": (0, 3e8),
    "صغيرة (300 مليون - 2 مليار)": (3e8, 2e9),
    "متوسطة (2 - 10 مليار)": (2e9, 1e10),
    "كبيرة (أكثر من 10 مليار)": (1e10, float("inf")),
}
PRICE = {
    "أي سعر": (0, float("inf")),
    "أقل من 1$": (0, 1),
    "1$ - 5$": (1, 5),
    "5$ - 20$": (5, 20),
    "20$ - 50$": (20, 50),
    "أكثر من 50$": (50, float("inf")),
}
LOC = {
    "الكل": (0, 1),
    "قريب من القاع (الثلث الأدنى)": (0, 1 / 3),
    "المنتصف": (1 / 3, 2 / 3),
    "قريب من القمة (الثلث الأعلى)": (2 / 3, 1),
}

# ----------------------------------------------------------------- الفحص
def evaluate(sym: str, df: pd.DataFrame, c: dict):
    if len(df) < 40:
        return None
    close = df["Close"]
    last = float(close.iloc[-1])
    if not (c["pmin"] <= last <= c["pmax"]):
        return None
    hi, lo = float(df["High"].max()), float(df["Low"].min())
    pos = (last - lo) / (hi - lo) if hi > lo else 0.5
    if not (c["loc"][0] <= pos <= c["loc"][1]):
        return None
    r = float(rsi(close).iloc[-1])
    m = float(macd_line(close).iloc[-1])
    if c["use_rsi"] and not (c["rsi_lo"] <= r <= c["rsi_hi"]):
        return None
    if c["use_macd"] and not (c["macd_lo"] <= m <= c["macd_hi"]):
        return None
    sup = touches = rise = None
    if c["use_sup"]:
        s = support_stats(df, c["window"], 0.02)
        if not s:
            return None
        sup, touches, rise = s
        if touches < c["tests"] or rise > c["max_rise"]:
            return None
    return {
        "الرمز": sym, "السعر": round(last, 3), "RSI": round(r, 1), "MACD": round(m, 3),
        "الدعم": None if sup is None else round(sup, 3),
        "اختبارات الدعم": touches,
        "الارتفاع عن الدعم %": None if rise is None else round(rise, 1),
    }


SCAN_URL = "https://scanner.tradingview.com/america/scan"
TV_SUFFIX = {"يومي": "", "أسبوعي": "|1W", "ساعة": "|60"}
TV_EXCH = {"Nasdaq": "NASDAQ", "NYSE": "NYSE", "NYSE American": "AMEX"}
INF = float("inf")


def _rng(field, lo, hi):
    return {"left": field, "operation": "in_range", "right": [lo, hi if hi != INF else 1e12]}


def tv_scan(c: dict) -> list:
    """فحص كل السوق بطلب واحد من ماسح TradingView (ثانية أو ثانيتين)."""
    sfx = TV_SUFFIX[c["tf"]]
    f = [
        {"left": "type", "operation": "equal", "right": "stock"},
        {"left": "typespecs", "operation": "has", "right": ["common"]},
    ]
    if c["market"] != "الكل":
        f.append({"left": "exchange", "operation": "in_range", "right": [TV_EXCH[c["market"]]]})
    else:
        f.append({"left": "exchange", "operation": "in_range", "right": ["NASDAQ", "NYSE", "AMEX"]})
    if c["pmin"] > 0 or c["pmax"] != INF:
        f.append(_rng("close", c["pmin"], c["pmax"]))
    if c["mcap"] != (0, INF):
        f.append(_rng("market_cap_basic", c["mcap"][0], c["mcap"][1]))
    if c["use_rsi"]:
        f.append(_rng("RSI" + sfx, c["rsi_lo"], c["rsi_hi"]))
    if c["use_macd"]:
        f.append(_rng("MACD.macd" + sfx, c["macd_lo"], c["macd_hi"]))
    if c["pm_chg"] > 0:
        f.append({"left": "premarket_change", "operation": "greater", "right": c["pm_chg"]})
    if c["pm_vol"] > 0:
        f.append({"left": "premarket_volume", "operation": "greater", "right": c["pm_vol"]})
    if c["rvol"] > 0:
        f.append({"left": "relative_volume_10d_calc", "operation": "greater", "right": c["rvol"]})
    cols = ["name", "description", "close", "RSI" + sfx, "MACD.macd" + sfx, "market_cap_basic",
            "premarket_change", "premarket_volume", "relative_volume_10d_calc",
            "float_shares_outstanding", "price_52_week_high", "price_52_week_low"]
    payload = {
        "filter": f, "options": {"lang": "en"}, "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": cols,
        "sort": {"sortBy": "premarket_change" if c["pm_chg"] > 0 else "volume", "sortOrder": "desc"},
        "range": [0, 600],
    }
    r = requests.post(SCAN_URL, json=payload, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    out = []
    for item in r.json().get("data", []):
        d = dict(zip(cols, item["d"]))
        out.append(d)
    return out


def _num(x, nd=2, div=1.0):
    return None if x is None else round(float(x) / div, nd)


def run_scan(c: dict):
    prog = st.progress(0.1, text="فحص السوق...")
    try:
        rows = tv_scan(c)
    except Exception as e:
        prog.empty()
        st.error(f"تعذّر الاتصال بماسح السوق: {e}")
        return

    # موقع السعر ضمن نطاق 52 أسبوع
    kept = []
    for d in rows:
        hi, lo, px = d.get("price_52_week_high"), d.get("price_52_week_low"), d.get("close")
        if c["loc"] != (0, 1) and hi and lo and px and hi > lo:
            pos = (px - lo) / (hi - lo)
            if not (c["loc"][0] <= pos <= c["loc"][1]):
                continue
        kept.append(d)
    rows = kept

    # الاقتراض
    short = {}
    if c["borrow"]:
        prog.progress(0.4, text="فلتر الاقتراض...")
        short = load_shortable()
        if not short:
            st.warning("تعذّر جلب قائمة الاقتراض الآن، تم تخطي هذا الفلتر.")
        else:
            lo, hi = c["avail_min"], c["avail_max"] or INF
            rows = [d for d in rows if d["name"] in short and lo <= short[d["name"]]["avail"] <= hi]

    # الدعم: تحميل بيانات الأسهم الناجية فقط (عدد قليل = سريع)
    results = []
    syms = [d["name"] for d in rows][:300]
    hist = {}
    if c["use_sup"] and syms:
        prog.progress(0.7, text=f"اختبار الدعم لـ {len(syms)} سهم...")
        try:
            hist = download(syms, c["tf"])
        except Exception:
            hist = {}

    infos = {}
    if c["use_rs"] and rows:
        prog.progress(0.9, text="التقسيم العكسي...")
        with ThreadPoolExecutor(max_workers=16) as ex:
            for sym, inf in zip(syms, ex.map(get_info, syms)):
                infos[sym] = inf
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=c["rs_days"])

    for d in rows[:300]:
        sym = d["name"]
        sup = touches = rise = None
        if c["use_sup"]:
            df = hist.get(sym)
            s = support_stats(df, c["window"], 0.02) if df is not None else None
            if not s:
                continue
            sup, touches, rise = s
            if touches < c["tests"] or rise > c["max_rise"]:
                continue
        rs = infos.get(sym, {}).get("reverse_split")
        if c["use_rs"] and (rs is None or pd.Timestamp(rs) < cutoff):
            continue
        row = {
            "الرمز": sym, "الشركة": d.get("description") or "",
            "السعر": _num(d.get("close"), 3),
            "Premarket %": _num(d.get("premarket_change"), 1),
            "حجم Premarket": None if d.get("premarket_volume") is None else int(d["premarket_volume"]),
            "الحجم النسبي": _num(d.get("relative_volume_10d_calc"), 1),
            "RSI": _num(d.get("RSI" + TV_SUFFIX[c["tf"]]), 1),
            "MACD": _num(d.get("MACD.macd" + TV_SUFFIX[c["tf"]]), 3),
            "القيمة السوقية (مليون)": _num(d.get("market_cap_basic"), 1, 1e6),
            "Float (مليون)": _num(d.get("float_shares_outstanding"), 2, 1e6),
        }
        if c["use_sup"]:
            row.update({"الدعم": round(sup, 3), "اختبارات الدعم": touches,
                        "الارتفاع عن الدعم %": round(rise, 1)})
        if short.get(sym):
            row["المتاح للاقتراض"] = short[sym]["avail"]
        if rs is not None:
            row["تقسيم عكسي"] = str(pd.Timestamp(rs).date())
        results.append(row)

    st.session_state.results = results
    st.session_state.done = True
    prog.empty()


def show_results():
    res = st.session_state.get("results")
    if not res:
        if st.session_state.get("done"):
            st.info("ما فيه أسهم تطابق الشروط الحالية. جرّب توسّع النطاقات.")
        return
    df = pd.DataFrame(res)
    df["الشارت"] = "https://www.tradingview.com/chart/?symbol=" + df["الرمز"]
    st.subheader(f"النتائج ({len(df)})")
    st.dataframe(
        df, use_container_width=True, hide_index=True,
        column_config={"الشارت": st.column_config.LinkColumn("الشارت", display_text="فتح")},
    )
    st.download_button("تحميل النتائج CSV", df.drop(columns=["الشارت"]).to_csv(index=False).encode("utf-8-sig"),
                       "results.csv", "text/csv")


# ----------------------------------------------------------------- الواجهة
mode = st.radio("الوضع", ["فحص السوق", "تحليل سهم معين"], horizontal=True, label_visibility="collapsed")

if mode == "فحص السوق":
    st.header("إعدادات الفحص")

    with st.expander("01 البيانات الأساسية", expanded=True):
        market = st.selectbox("السوق", ["الكل", "Nasdaq", "NYSE", "NYSE American"])
        mcap_k = st.selectbox("القيمة السوقية", list(MCAP))
        price_k = st.selectbox("السعر", list(PRICE))
        loc_k = st.selectbox("موقع السعر (ضمن نطاق 52 أسبوع)", list(LOC))
        pm_chg = st.number_input("أقل تغير Premarket (%)", 0.0, 1000.0, 0.0, 5.0, format="%.1f",
                                 help="0 = بدون فلتر. مثال: 20 يعني بس الأسهم اللي طالعة 20% فأكثر قبل الافتتاح")
        pm_vol = st.number_input("أقل حجم Premarket (سهم)", 0, 1_000_000_000, 0, 10000)
        rvol = st.number_input("أقل حجم نسبي (x)", 0.0, 1000.0, 0.0, 1.0, format="%.1f")

    with st.expander("02 التقسيم العكسي"):
        use_rs = st.checkbox("فقط الأسهم اللي سوّت تقسيم عكسي", value=False)
        rs_days = st.number_input("خلال آخر (يوم)", 30, 1500, 365, 30)

    with st.expander("03 المؤشرات الفنية"):
        use_rsi = st.checkbox("RSI (14)", value=True)
        use_macd = st.checkbox("MACD", value=True)
        tf = st.selectbox("الإطار الزمني", list(TF))
        rsi_lo = st.number_input("RSI من", 0.0, 100.0, 23.0, 1.0, format="%.2f")
        rsi_hi = st.number_input("RSI إلى", 0.0, 100.0, 30.0, 1.0, format="%.2f")
        macd_lo = st.number_input("MACD من", -100.0, 100.0, -2.0, 0.1, format="%.2f")
        macd_hi = st.number_input("MACD إلى", -100.0, 100.0, 0.0, 0.1, format="%.2f")

    with st.expander("04 أسهم الشورت والدعم"):
        borrow = st.checkbox("فلتر الأسهم المتاحة للاقتراض", value=True)
        avail_min = st.number_input("الحد الأدنى للأسهم المتاحة", 0, 100_000_000, 0, 1000)
        avail_max = st.number_input("الحد الأقصى للأسهم المتاحة (0 = بدون حد)", 0, 100_000_000, 50000, 1000)
        use_sup = st.checkbox("اختبار الدعم", value=True)
        tests = st.number_input("اختبارات الدعم (جلسات)", 1, 50, 5)
        window = st.number_input("جلسات البحث", 3, 120, 10)
        max_rise = st.number_input("أقصى ارتفاع عن الدعم (%)", 0.0, 500.0, 20.0, 1.0, format="%.2f",
                                   help="أقصى نسبة يكون فيها السعر الحالي فوق الدعم")
        st.caption("الدعم: أدنى سعر في جلسات البحث، ويُحسب عدد الجلسات اللي وصل فيها أدنى سعر إلى نطاق ±2% منه.")

    cfg = dict(
        market=market, mcap=MCAP[mcap_k], pmin=PRICE[price_k][0], pmax=PRICE[price_k][1], loc=LOC[loc_k],
        use_rs=use_rs, rs_days=rs_days, use_rsi=use_rsi, use_macd=use_macd, tf=tf,
        rsi_lo=rsi_lo, rsi_hi=rsi_hi, macd_lo=macd_lo, macd_hi=macd_hi,
        borrow=borrow, avail_min=avail_min, avail_max=avail_max,
        use_sup=use_sup, tests=tests, window=window, max_rise=max_rise,
        pm_chg=pm_chg, pm_vol=pm_vol, rvol=rvol,
    )

    auto = st.checkbox("تحديث تلقائي مباشر", value=False)
    every = st.number_input("كل (ثانية)", 10, 600, 30, 5) if auto else 30
    start = st.button("▶ بدء الفحص", type="primary")
    if st.button("■ إيقاف الفحص"):
        st.session_state.live = False
    if st.button("↺ إعادة تعيين"):
        for k in ("results", "names", "done", "live"):
            st.session_state.pop(k, None)
        st.rerun()

    if start:
        st.session_state.live = bool(auto)
    if start or st.session_state.pop("rescan", False):
        run_scan(cfg)
    show_results()
    if st.session_state.get("live"):
        st.caption(f"تحديث مباشر كل {every} ثانية · اضغط إيقاف للتوقف")
        time.sleep(every)
        st.session_state.rescan = True
        st.rerun()

else:
    st.header("تحليل سهم معين")
    sym = st.text_input("رمز السهم الأمريكي", placeholder="مثال: AAPL").strip().upper()
    tf = st.selectbox("الإطار الزمني", list(TF), key="tf_single")
    if st.button("تحليل", type="primary") and sym:
        with st.spinner("جاري التحليل..."):
            data = download([sym], tf)
            if sym not in data:
                st.error("ما لقيت بيانات لهذا الرمز.")
            else:
                df = data[sym]
                close = df["Close"]
                last = float(close.iloc[-1])
                prev = float(close.iloc[-2]) if len(close) > 1 else last
                info = get_info(sym)
                try:
                    full = yf.Ticker(sym).info
                except Exception:
                    full = {}
                st.subheader(f"{sym} · {full.get('longName', '')}")
                st.markdown("### بيانات الشركة والسعر")
                c1, c2 = st.columns(2)
                c1.metric("آخر سعر", f"{last:.3f}", f"{(last / prev - 1) * 100:.2f}%")
                c2.metric("القيمة السوقية (مليون)", "-" if not info["mcap"] else f"{info['mcap'] / 1e6:,.0f}")
                c1.metric("أعلى سعر في الفترة", f"{df['High'].max():.3f}")
                c2.metric("أدنى سعر في الفترة", f"{df['Low'].min():.3f}")
                if full.get("sector"):
                    st.write(f"القطاع: {full.get('sector')} · {full.get('industry', '')}")
                if info["reverse_split"] is not None:
                    st.write(f"آخر تقسيم عكسي: {pd.Timestamp(info['reverse_split']).date()}")
                short = load_shortable().get(sym)
                if short:
                    st.write(f"المتاح للاقتراض: {short['avail']:,} سهم")
                st.markdown("### المؤشرات الفنية")
                m1, m2 = st.columns(2)
                m1.metric("RSI (14)", f"{rsi(close).iloc[-1]:.1f}")
                m2.metric("MACD", f"{macd_line(close).iloc[-1]:.3f}")
                s = support_stats(df, 10, 0.02)
                if s:
                    st.markdown("### الدعم (آخر 10 جلسات)")
                    s1, s2, s3 = st.columns(3)
                    s1.metric("الدعم", f"{s[0]:.3f}")
                    s2.metric("اختبارات الدعم", s[1])
                    s3.metric("الارتفاع عن الدعم", f"{s[2]:.1f}%")
        st.markdown("### الشارت التفاعلي — TradingView")
        components.html(
            f"""
<div style="height:560px"><div id="tv" style="height:100%"></div></div>
<script src="https://s3.tradingview.com/tv.js"></script>
<script>new TradingView.widget({{autosize:true,symbol:"{sym}",interval:"D",timezone:"Etc/UTC",
theme:"light",style:"1",locale:"ar_AE",studies:["RSI@tv-basicstudies","MACD@tv-basicstudies"],
container_id:"tv"}});</script>""",
            height=580,
        )
