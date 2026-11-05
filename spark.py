# -*- coding: utf-8 -*-
"""كاشف الشرارة لأسهم الـ Float الصغير.

الفكرة:
1) قائمة صباحية بأسهم Float أقل من N مليون (بيانات ثابتة، التأخير ما يهم فيها).
2) نراقب هذي القائمة فقط كل بضع ثواني، ونرسل تنبيه لما تظهر "ريحة السيولة":
   قفزة حجم مفاجئة، والسعر لسا قريب من القاع، وما طلع كثير.
3) بدون سيل رسائل: فترة إحماء صامتة، وتنبيه واحد لكل سهم، وحد أقصى بالدقيقة وباليوم.

المصادر: alpaca (مفاتيح API) أو tradingview (كوكي حسابك).
وضع PROBE=true: يفحص جودة البيانات ويرسل تقرير بدون تنبيهات (للتأكد قبل الدفع/الاعتماد).
"""
import copy
import json
import os
import re
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")
SCAN_URL = "https://scanner.tradingview.com/america/scan"
ALPACA_URL = "https://data.alpaca.markets/v2/stocks/snapshots"

DEFAULTS = {
    "source": "alpaca",              # alpaca | tradingview
    "alpaca_feed": "iex",            # iex (مجاني) | sip (مدفوع، كل البورصات)
    "universe": {
        "float_min_million": 0.05, "float_max_million": 5,
        "price_min": 0.1, "price_max": 10,
        "exchanges": ["NASDAQ", "NYSE", "AMEX"],
        "max_symbols": 800, "refresh_minutes": 60,
    },
    "poll_seconds": 5,
    "warmup_minutes": 5,
    "spark": {
        "window_seconds": 60, "baseline_minutes": 30,
        "spike_x": 6, "baseline_floor_shares": 1500,
        "min_window_shares": 8000, "min_window_dollar": 2500,
        "min_change_pct": 1, "max_change_pct": 40,
        "max_above_low_pct": 30, "require_rising": True,
    },
    "alerts": {"rearm_extra_pct": 30, "max_per_poll": 3, "max_per_minute": 3,
               "max_per_day": 10, "min_score": 40},
    "sec_check": True,
    "heartbeat_minutes": 60,
    "exit_after_close": True,        # على GitHub Actions: true. على سيرفر دايم: false
    "max_run_minutes": 350,          # حد تشغيلة GitHub Actions (0 = بدون حد)
}


def NOW() -> float:
    return time.time()


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cfg() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "spark_config.json")
    user = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            user = json.load(f)
    return deep_merge(DEFAULTS, user)


def session_of(now_et: datetime):
    m = now_et.hour * 60 + now_et.minute
    if m < 4 * 60 or m >= 20 * 60:
        return None
    if m < 9 * 60 + 30:
        return "premarket"
    if m < 16 * 60:
        return "regular"
    return "postmarket"


def tv_headers() -> dict:
    h = {"User-Agent": "Mozilla/5.0"}
    ck = os.environ.get("TV_COOKIE", "").strip()
    if ck:
        h["Cookie"] = ck
    return h


def parse_ts(s):
    """وقت ISO من Alpaca (فيه نانوثواني أحياناً) -> datetime UTC."""
    if not s:
        return None
    s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ------------------------------------------------------------------ قائمة الأسهم (Float صغير)
def build_universe(cfg: dict) -> list:
    u = cfg["universe"]
    f = [
        {"left": "type", "operation": "equal", "right": "stock"},
        {"left": "typespecs", "operation": "has", "right": ["common"]},
        {"left": "exchange", "operation": "in_range", "right": u["exchanges"]},
        {"left": "close", "operation": "in_range", "right": [u["price_min"], u["price_max"]]},
        {"left": "float_shares_outstanding", "operation": "in_range",
         "right": [u["float_min_million"] * 1e6, u["float_max_million"] * 1e6]},
    ]
    cols = ["name", "description", "exchange", "close", "float_shares_outstanding"]
    payload = {"filter": f, "options": {"lang": "en"}, "markets": ["america"],
               "symbols": {"query": {"types": []}, "tickers": []}, "columns": cols,
               "sort": {"sortBy": "volume", "sortOrder": "desc"}, "range": [0, u["max_symbols"]]}
    r = requests.post(SCAN_URL, json=payload, headers=tv_headers(), timeout=30)
    if not r.ok:
        raise RuntimeError(f"فشل بناء القائمة ({r.status_code}): {r.text[:200]}")
    out = []
    for item in r.json().get("data", []):
        d = dict(zip(cols, item["d"]))
        sym = d.get("name") or ""
        if re.fullmatch(r"[A-Z]{1,5}", sym) and d.get("float_shares_outstanding"):
            out.append({"sym": sym, "exch": d.get("exchange") or "NASDAQ", "name": d.get("description") or "",
                        "float": float(d["float_shares_outstanding"])})
    return out


# ------------------------------------------------------------------ المصادر
class AlpacaSource:
    name = "alpaca"

    def __init__(self, cfg):
        self.key = os.environ.get("ALPACA_KEY", "").strip()
        self.secret = os.environ.get("ALPACA_SECRET", "").strip()
        self.feed = cfg.get("alpaca_feed", "iex")
        self.bar = {}          # sym -> (وقت الشمعة, حجمها)
        self.trade_ages = []

    def ready(self):
        return bool(self.key and self.secret)

    def status(self):
        if self.feed == "iex":
            return "Alpaca IEX ⚡ لحظي لكن من بورصة IEX فقط (جزء صغير من حجم السوق)"
        return f"Alpaca {self.feed.upper()} ⚡ لحظي"

    def parse(self, sym, s):
        lt = s.get("latestTrade") or {}
        p = lt.get("p")
        if not p:
            return None
        prev = (s.get("prevDailyBar") or {}).get("c")
        chg = (p / prev - 1) * 100 if prev else None
        mb = s.get("minuteBar") or {}
        t, v = mb.get("t"), mb.get("v")
        inc = None
        if t is not None and v is not None:
            last = self.bar.get(sym)
            if last is not None:
                inc = max(0.0, float(v) - last[1]) if last[0] == t else float(v)
            self.bar[sym] = (t, float(v))
        ts = parse_ts(lt.get("t"))
        if ts is not None:
            self.trade_ages.append((datetime.now(timezone.utc) - ts).total_seconds())
        return {"price": float(p), "chg": chg, "inc": inc, "low_hint": mb.get("l")}

    def fetch(self, universe, session, now_et):
        self.trade_ages = []
        syms = [u["sym"] for u in universe]
        chunks = [syms[i:i + 100] for i in range(0, len(syms), 100)]
        hdr = {"APCA-API-KEY-ID": self.key, "APCA-API-SECRET-KEY": self.secret}

        def one(chunk):
            r = requests.get(ALPACA_URL, params={"symbols": ",".join(chunk), "feed": self.feed},
                             headers=hdr, timeout=20)
            if r.status_code in (401, 403):
                raise RuntimeError(f"Alpaca رفض الطلب ({r.status_code}): تأكد من المفاتيح ومن صلاحية feed={self.feed}")
            if not r.ok:
                raise RuntimeError(f"Alpaca رد {r.status_code}: {r.text[:200]}")
            js = r.json()
            return js.get("snapshots", js) if isinstance(js, dict) else {}

        out = {}
        with ThreadPoolExecutor(max_workers=4) as ex:
            for data in ex.map(one, chunks):
                for sym, snap in data.items():
                    o = self.parse(sym, snap)
                    if o:
                        out[sym] = o
        return out


class TVSource:
    name = "tradingview"
    CORE = ["close", "volume", "change", "update_mode"]
    EXT = ["premarket_close", "premarket_volume", "premarket_change",
           "postmarket_close", "postmarket_volume", "postmarket_change"]
    MAP = {"premarket": ("premarket_close", "premarket_volume", "premarket_change"),
           "regular": ("close", "volume", "change"),
           "postmarket": ("postmarket_close", "postmarket_volume", "postmarket_change")}

    def __init__(self, cfg):
        self.prev = {}
        self.use_ext = True
        self.ext_failed = False
        self.n_stream = self.n_delayed = 0
        self.cookie = bool(os.environ.get("TV_COOKIE", "").strip())

    def ready(self):
        return True

    def status(self):
        if self.n_stream + self.n_delayed == 0:
            return "TradingView (لسا ما فحصت نوع البيانات)"
        ck = "بالكوكي" if self.cookie else "بدون كوكي"
        return f"TradingView {ck}: ⚡ {self.n_stream} لحظي / ⏱ {self.n_delayed} متأخر"

    def _post(self, tickers, cols):
        payload = {"symbols": {"tickers": tickers, "query": {"types": []}}, "columns": cols,
                   "markets": ["america"], "range": [0, len(tickers)]}
        return requests.post(SCAN_URL, json=payload, headers=tv_headers(), timeout=30)

    def fetch(self, universe, session, now_et):
        tickers = [f"{u['exch']}:{u['sym']}" for u in universe]
        cols = self.CORE + (self.EXT if self.use_ext else [])
        r = self._post(tickers, cols)
        if r.status_code == 400 and self.use_ext:
            self.use_ext, self.ext_failed = False, True
            cols = self.CORE
            r = self._post(tickers, cols)
        if not r.ok:
            raise RuntimeError(f"الماسح رد {r.status_code}: {r.text[:200]}")
        pc, vc, cc = self.MAP[session]
        out, ns, nd = {}, 0, 0
        for item in r.json().get("data", []):
            sym = item["s"].split(":")[-1]
            d = dict(zip(cols, item["d"]))
            mode = d.get("update_mode") or ""
            if "delayed" in mode:
                nd += 1
            elif mode:
                ns += 1
            price = d.get(pc) or d.get("close")
            if not price:
                continue
            cum = d.get(vc)
            chg = d.get(cc) if d.get(cc) is not None else d.get("change")
            prev = self.prev.get(sym)
            inc = None
            if prev and prev[0] == session and cum is not None and prev[1] is not None:
                inc = max(0.0, float(cum) - prev[1])
            self.prev[sym] = (session, None if cum is None else float(cum))
            out[sym] = {"price": float(price), "chg": chg, "inc": inc, "low_hint": None}
        self.n_stream, self.n_delayed = ns, nd
        return out


def make_source(cfg):
    return TVSource(cfg) if cfg["source"] == "tradingview" else AlpacaSource(cfg)


# ------------------------------------------------------------------ المحرك
class Engine:
    def __init__(self, cfg):
        self.c = cfg
        sp = cfg["spark"]
        self.W = sp["window_seconds"]
        self.B = sp["baseline_minutes"] * 60
        self.hist = defaultdict(deque)     # sym -> (وقت, أسهم جديدة)
        self.px = defaultdict(deque)       # sym -> (وقت, سعر)
        self.low = {}
        self.sent = {}                     # sym -> نسبة التغير وقت آخر تنبيه
        self.sent_times = deque()
        self.day_sent = 0
        self.day_syms = []
        self.t0 = None
        self.day = None

    def reset_day(self, day):
        self.day = day
        self.hist.clear(); self.px.clear(); self.low.clear(); self.sent.clear()
        self.sent_times.clear(); self.day_sent = 0; self.day_syms = []
        self.t0 = None

    def observe(self, sym, ts, o):
        if self.t0 is None:
            self.t0 = ts
        p = o["price"]
        cur = self.low.get(sym)
        self.low[sym] = p if cur is None else min(cur, p)
        if o.get("low_hint"):
            self.low[sym] = min(self.low[sym], float(o["low_hint"]))
        d = self.px[sym]
        d.append((ts, p))
        while d and d[0][0] < ts - self.W * 2:
            d.popleft()
        if o.get("inc") is not None:
            h = self.hist[sym]
            h.append((ts, o["inc"]))
            while h and h[0][0] < ts - self.B:
                h.popleft()

    def evaluate(self, sym, ts, o, flt):
        sp, al = self.c["spark"], self.c["alerts"]
        if self.t0 is None or ts - self.t0 < self.c["warmup_minutes"] * 60:
            return None
        chg, p = o.get("chg"), o["price"]
        if chg is None or not (sp["min_change_pct"] <= chg <= sp["max_change_pct"]):
            return None
        prev = self.sent.get(sym)
        if prev is not None and chg < prev + al["rearm_extra_pct"]:
            return None
        h = self.hist.get(sym)
        if not h:
            return None
        rec = sum(v for t, v in h if t > ts - self.W)
        if rec < sp["min_window_shares"] or rec * p < sp["min_window_dollar"]:
            return None
        old = sum(v for t, v in h if t <= ts - self.W)
        span = min(self.B, ts - self.t0) - self.W
        n_win = max(span / self.W, 1.0)
        mult = rec / max(old / n_win, sp["baseline_floor_shares"])
        if mult < sp["spike_x"]:
            return None
        low = self.low.get(sym, p)
        above = (p / low - 1) * 100 if low > 0 else 999.0
        if above > sp["max_above_low_pct"]:
            return None
        if sp.get("require_rising", True):
            pts = [x for t, x in self.px[sym] if t >= ts - self.W]
            if pts and p < pts[0]:
                return None
        fmax = self.c["universe"]["float_max_million"] * 1e6
        score = (min(mult, 30) / 30 * 40 + min(rec * p / 50000, 1) * 25
                 + max(0.0, 1 - above / sp["max_above_low_pct"]) * 15
                 + max(0.0, 1 - chg / sp["max_change_pct"]) * 10
                 + (max(0.0, (fmax - flt) / fmax) * 10 if flt else 0))
        return {"sym": sym, "price": p, "chg": chg, "above": above, "mult": mult, "rec": rec,
                "dollar": rec * p, "low": low, "score": int(round(score)), "float": flt}

    def step(self, obs, ts, floats):
        for sym, o in obs.items():
            self.observe(sym, ts, o)
        cands = []
        for sym, o in obs.items():
            c = self.evaluate(sym, ts, o, floats.get(sym))
            if c:
                cands.append(c)
        cands.sort(key=lambda c: -c["score"])
        al = self.c["alerts"]
        while self.sent_times and self.sent_times[0] < ts - 60:
            self.sent_times.popleft()
        out = []
        for c in cands:
            if c["score"] < al["min_score"]:
                continue
            if len(out) >= al["max_per_poll"]:
                break
            if len(self.sent_times) + len(out) >= al["max_per_minute"]:
                break
            if self.day_sent + len(out) >= al["max_per_day"]:
                break
            out.append(c)
        for c in out:
            self.sent[c["sym"]] = c["chg"]
            self.sent_times.append(ts)
            self.day_sent += 1
            self.day_syms.append(c["sym"])
        return out


# ------------------------------------------------------------------ الرسائل و SEC
def levels_for(c):
    p, low = c["price"], c["low"]
    stop = max(low * 0.97, p * 0.85)
    if stop >= p:
        stop = p * 0.95
    r = p - stop
    return {"stop": stop, "t1": p + 2 * r, "t2": p + 4 * r, "risk_pct": r / p * 100}


def format_alert(c, name, src_status, now_et=None):
    lv = levels_for(c)
    now_et = now_et or datetime.now(ET)
    fl = c.get("float")
    return "\n".join([
        f"⚡ شرارة: {c['sym']} — {name}",
        f"السعر: ${c['price']:.4f} ({c['chg']:+.1f}% من إغلاق أمس)",
        f"فوق القاع: {c['above']:.1f}%",
        f"قفزة الحجم: {c['mult']:.1f}x المعتاد (آخر دقيقة: {int(c['rec']):,} سهم ≈ ${c['dollar']:,.0f})",
        f"Float: {'غير متوفر' if not fl else f'{fl / 1e6:.2f}M'}",
        f"النقاط: {c['score']}/100",
        f"سيناريو حسابي (مو توصية): دخول ≈ {c['price']:.4f} · وقف {lv['stop']:.4f} (-{lv['risk_pct']:.1f}%)"
        f" · هدف1 {lv['t1']:.4f} · هدف2 {lv['t2']:.4f}",
        f"المصدر: {src_status.split(' ')[0]} · {now_et.strftime('%H:%M:%S')} نيويورك",
        f"https://www.tradingview.com/chart/?symbol={c['sym']}",
    ])


_SEC = {"map": None}
REG_FORMS = {"S-1", "S-1/A", "S-3", "S-3/A", "F-1", "F-1/A", "F-3", "F-3/A"}


def sec_headers():
    return {"User-Agent": ("Bdr-stock-screener " + os.environ.get("SEC_CONTACT", "").strip()).strip()}


def dilution_check(sym, days=30):
    """'عالي' (424B) / 'متوسط' (تسجيل طرح) / 'لا يوجد' / None إذا تعذّر."""
    try:
        if _SEC["map"] is None:
            r = requests.get("https://www.sec.gov/files/company_tickers.json", headers=sec_headers(), timeout=15)
            r.raise_for_status()
            _SEC["map"] = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in r.json().values()}
        cik = _SEC["map"].get(sym.upper())
        if not cik:
            return None
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=sec_headers(), timeout=15)
        r.raise_for_status()
        rec = r.json()["filings"]["recent"]
    except Exception:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    items = [f for f, d in zip(rec["form"], rec["filingDate"])
             if d >= cutoff and (f.startswith("424B") or f in REG_FORMS)]
    if any(f.startswith("424B") for f in items):
        return "عالي"
    return "متوسط" if items else "لا يوجد"


def send(token, chat_id, text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}, timeout=20)
        if not r.ok:
            print("Telegram error:", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("Telegram exception:", e)
        return False


# ------------------------------------------------------------------ وضع الفحص (PROBE)
def probe(cfg, token, chat_id, sleep=time.sleep):
    now = datetime.now(ET)
    sess = session_of(now) or "regular"
    send(token, chat_id, f"🧪 فحص جودة البيانات بدأ · المصدر: {cfg['source']} · الجلسة: {sess} · {now.strftime('%H:%M')} نيويورك")
    uni = build_universe(cfg)
    if not uni:
        send(token, chat_id, "⚠️ القائمة رجعت فاضية. وسّع نطاق السعر أو الـ Float في spark_config.json")
        return 0
    src = make_source(cfg)
    if not src.ready():
        send(token, chat_id, "❌ ناقصة مفاتيح المصدر في أسرار GitHub (ALPACA_KEY / ALPACA_SECRET).")
        return 1
    src.fetch(uni, sess, now)
    sleep(10)
    obs = src.fetch(uni, sess, datetime.now(ET))
    n_total, n_data = len(uni), len(obs)
    n_active = sum(1 for o in obs.values() if (o.get("inc") or 0) > 0)
    lines = [f"📋 قائمة Float أقل من {cfg['universe']['float_max_million']}M: {n_total} سهم",
             f"📡 المصدر: {src.status()}",
             f"✅ أسهم رجعت منها بيانات: {n_data} من {n_total}",
             f"📈 أسهم فيها تداول خلال آخر 10 ثواني: {n_active}"]
    ages = getattr(src, "trade_ages", None)
    if ages:
        ages = sorted(ages)
        lines.append(f"⏱ عمر آخر صفقة (وسيط): {ages[len(ages) // 2]:.0f} ثانية · أكبر عمر: {ages[-1]:.0f} ثانية")
    if cfg["source"] == "tradingview":
        if src.n_stream == 0 and src.n_delayed > 0:
            lines.append("❌ كل البيانات متأخرة. الكوكي ما يعطي بيانات لحظية (تأكد من الاشتراك ومن الكوكي).")
        elif src.n_stream > 0 and src.n_delayed > 0:
            lines.append("⚠️ بعض الأسهم لحظية وبعضها متأخر (غالباً بورصة غير مشمولة باشتراكك).")
        elif src.n_stream > 0:
            lines.append("✅ البيانات لحظية.")
        if getattr(src, "ext_failed", False):
            lines.append("ℹ️ أعمدة ما قبل/بعد الافتتاح غير مقبولة من الماسح، استخدمت الأعمدة العادية فقط.")
    top = sorted(obs.items(), key=lambda kv: -(kv[1].get("inc") or 0))[:5]
    for sym, o in top:
        chg = "?" if o.get("chg") is None else f"{o['chg']:+.1f}%"
        lines.append(f"  {sym}: ${o['price']:.4f} · {chg} · +{int(o.get('inc') or 0):,} سهم")
    send(token, chat_id, "\n".join(lines))
    return 0


# ------------------------------------------------------------------ التشغيل
def main(sleep=time.sleep) -> int:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("ناقص TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID")
        return 1
    cfg = load_cfg()
    if os.environ.get("PROBE", "").lower() == "true":
        try:
            return probe(cfg, token, chat_id, sleep)
        except Exception as e:
            send(token, chat_id, f"❌ فشل الفحص:\n{e}")
            return 1

    src = make_source(cfg)
    if not src.ready():
        send(token, chat_id, "❌ ناقصة مفاتيح المصدر في أسرار GitHub (ALPACA_KEY / ALPACA_SECRET).")
        return 1
    eng = Engine(cfg)
    uni, floats, names = None, {}, {}
    last_uni = last_hb = last_err = 0.0
    started = NOW()
    alerts_total, polls_ok, polls_err = 0, 0, 0
    announced, warned_delay = False, False
    poll = cfg["poll_seconds"]
    print("بدأ المراقبة...")
    while True:
        t_loop = NOW()
        now_et = datetime.now(ET)
        if cfg["max_run_minutes"] and t_loop - started > cfg["max_run_minutes"] * 60:
            print("وصلنا حد المدة")
            break
        sess = session_of(now_et)
        if sess is None:
            if cfg["exit_after_close"] and now_et.hour >= 20:
                break
            sleep(30)
            continue
        day = now_et.date().isoformat()
        if eng.day != day:
            if eng.day is not None:
                send(token, chat_id, f"📊 ملخص أمس: {eng.day_sent} تنبيه ({', '.join(eng.day_syms) or 'ولا سهم'})")
            eng.reset_day(day)
            uni = None
        try:
            if uni is None or t_loop - last_uni > cfg["universe"]["refresh_minutes"] * 60:
                uni = build_universe(cfg)
                last_uni = t_loop
                floats = {u["sym"]: u["float"] for u in uni}
                names = {u["sym"]: u["name"] for u in uni}
                if not announced:
                    send(token, chat_id,
                         f"🟢 بدأت المراقبة: {len(uni)} سهم (Float < {cfg['universe']['float_max_million']}M)\n"
                         f"المصدر: {src.status()}\n"
                         f"أول {cfg['warmup_minutes']} دقايق إحماء بدون تنبيهات، وبعدها أي سهم جديد تبدأ سيولته أرسله لك.")
                    announced = True
            obs = src.fetch(uni, sess, now_et)
            polls_ok += 1
            if cfg["source"] == "tradingview" and not warned_delay and src.n_delayed and not src.n_stream:
                send(token, chat_id, "⚠️ بيانات TradingView متأخرة (الكوكي ما يعطي بيانات لحظية). لا تعتمد على التنبيهات.")
                warned_delay = True
            for c in eng.step(obs, NOW(), floats):
                send(token, chat_id, format_alert(c, names.get(c["sym"], ""), src.status()))
                alerts_total += 1
                if cfg["sec_check"]:
                    lvl = dilution_check(c["sym"])
                    if lvl == "عالي":
                        send(token, chat_id, f"🔴 تحذير تخفيف على {c['sym']}: ملف طرح (424B) خلال 30 يوم")
                    elif lvl == "متوسط":
                        send(token, chat_id, f"🟠 تنبيه على {c['sym']}: تسجيل طرح (S-1/S-3/F-3) خلال 30 يوم")
        except Exception as e:
            polls_err += 1
            print("خطأ:", e)
            if NOW() - last_err > 1800:
                send(token, chat_id, f"⚠️ خطأ في المراقبة:\n{e}")
                last_err = NOW()
        hb = cfg["heartbeat_minutes"] * 60
        if hb and NOW() - last_hb >= hb:
            send(token, chat_id, f"ℹ️ شغال · {now_et.strftime('%H:%M')} نيويورك · فحوصات ناجحة {polls_ok} · "
                                 f"أخطاء {polls_err} · تنبيهات اليوم {eng.day_sent}")
            last_hb = NOW()
        sleep(max(0.0, poll - (NOW() - t_loop)))
    send(token, chat_id, f"🔴 توقف البوت. تنبيهات اليوم: {eng.day_sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
