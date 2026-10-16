# -*- coding: utf-8 -*-
"""تنبيهات تيليجرام لأسهم البيني (Penny stocks) في كل الجلسات.
يشتغل على GitHub Actions: يفحص السوق كل N ثانية ويرسل أي سهم يطابق alerts_config.json
وضع scan_test: فحص واحد بشروط مخففة يرسل لك النتيجة أو سبب الخطأ.
"""
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

SCAN_URL = "https://scanner.tradingview.com/america/scan"
ET = ZoneInfo("America/New_York")
SESSIONS = {
    "premarket": ("premarket_change", "premarket_volume", "قبل الافتتاح"),
    "regular": ("change", "volume", "الجلسة العادية"),
    "postmarket": ("postmarket_change", "postmarket_volume", "بعد الإغلاق"),
}
COLS = ["name", "description", "close", "change", "volume", "premarket_change", "premarket_volume",
        "postmarket_change", "postmarket_volume", "relative_volume_10d_calc",
        "float_shares_outstanding", "market_cap_basic", "RSI", "update_mode"]


def load_cfg() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "alerts_config.json"), encoding="utf-8") as f:
        return json.load(f)


def tv_headers() -> dict:
    h = {"User-Agent": "Mozilla/5.0"}
    ck = os.environ.get("TV_COOKIE", "").strip()   # اختياري: كوكي حساب TradingView للبيانات اللحظية
    if ck:
        h["Cookie"] = ck
    return h


def data_mode(d: dict) -> str:
    m = d.get("update_mode")
    if not m:
        return "غير معروف"
    if "delayed" in m:
        digits = "".join(ch for ch in m if ch.isdigit())
        mins = int(digits) // 60 if digits else 0
        return f"⏱ متأخرة {mins} دقيقة" if mins else "⏱ متأخرة"
    return "⚡ لحظية"


def active_session(now: datetime):
    """الجلسة الحالية بتوقيت نيويورك."""
    m = now.hour * 60 + now.minute
    if m < 4 * 60 or m >= 20 * 60:
        return None
    if m < 9 * 60 + 30:
        return "premarket"
    if m < 16 * 60:
        return "regular"
    return "postmarket"


def per_session(val, session, default=0):
    """القيمة ممكن تكون رقم واحد أو قاموس لكل جلسة."""
    if isinstance(val, dict):
        return val.get(session, default)
    return default if val is None else val


def build_payload(cfg: dict, session: str, relaxed: bool = False) -> dict:
    chg, vol, _ = SESSIONS[session]
    f = [
        {"left": "type", "operation": "equal", "right": "stock"},
        {"left": "typespecs", "operation": "has", "right": ["common"]},
        {"left": "exchange", "operation": "in_range", "right": cfg["exchanges"]},
        {"left": "close", "operation": "in_range", "right": [cfg["price_min"], cfg["price_max"]]},
    ]
    if not relaxed:
        f.append({"left": chg, "operation": "greater", "right": cfg["min_change_pct"]})
        f.append({"left": vol, "operation": "greater", "right": per_session(cfg["min_volume"], session)})
        if cfg.get("mcap_max_million"):
            f.append({"left": "market_cap_basic", "operation": "less", "right": cfg["mcap_max_million"] * 1e6})
        if cfg.get("float_max_million"):
            f.append({"left": "float_shares_outstanding", "operation": "less",
                      "right": cfg["float_max_million"] * 1e6})
        rv = per_session(cfg.get("rvol_min"), session)
        if rv:
            f.append({"left": "relative_volume_10d_calc", "operation": "greater", "right": rv})
        if cfg.get("rsi_min") is not None:
            f.append({"left": "RSI", "operation": "greater", "right": cfg["rsi_min"]})
        if cfg.get("rsi_max") is not None:
            f.append({"left": "RSI", "operation": "less", "right": cfg["rsi_max"]})
    return {
        "filter": f, "options": {"lang": "en"}, "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": COLS,
        "sort": {"sortBy": chg if not relaxed else "volume", "sortOrder": "desc"},
        "range": [0, 50 if not relaxed else 5],
    }


def scan(cfg: dict, session: str, relaxed: bool = False) -> list:
    r = requests.post(SCAN_URL, json=build_payload(cfg, session, relaxed), timeout=30,
                      headers=tv_headers())
    if not r.ok:
        raise RuntimeError(f"الماسح رد {r.status_code}: {r.text[:300]}")
    js = r.json()
    if isinstance(js, dict) and js.get("error"):
        raise RuntimeError(f"الماسح رجّع خطأ: {str(js['error'])[:300]}")
    chg, vol, _ = SESSIONS[session]
    out = []
    for item in js.get("data", []):
        d = dict(zip(COLS, item["d"]))
        d["_session"], d["_chg"], d["_vol"] = session, d.get(chg), d.get(vol)
        out.append(d)
    return out


def millions(x):
    return "غير متوفر" if x is None else f"{x / 1e6:,.1f}M"


def format_msg(d: dict) -> str:
    now = datetime.now(ET).strftime("%H:%M:%S")
    rsi, rvol, vol, chg = d.get("RSI"), d.get("relative_volume_10d_calc"), d.get("_vol"), d.get("_chg")
    return "\n".join([
        f"🚨 {d['name']} — {d.get('description') or ''}",
        f"الجلسة: {SESSIONS[d['_session']][2]}",
        f"نوع البيانات: {data_mode(d)}",
        f"السعر: ${d['close']:.4f}",
        f"التغير: {'غير متوفر' if chg is None else f'{chg:+.1f}%'}",
        f"الحجم: {'غير متوفر' if vol is None else f'{int(vol):,}'}",
        f"الحجم النسبي: {'غير متوفر' if rvol is None else f'{rvol:.1f}x'}",
        f"Float: {millions(d.get('float_shares_outstanding'))}",
        f"القيمة السوقية: {millions(d.get('market_cap_basic'))}",
        f"RSI: {'غير متوفر' if rsi is None else f'{rsi:.0f}'}",
        f"وقت الفحص (نيويورك): {now}",
        f"https://www.tradingview.com/chart/?symbol={d['name']}",
    ])


def send(token: str, chat_id: str, text: str) -> bool:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
            timeout=20,
        )
        if not r.ok:
            print("Telegram error:", r.status_code, r.text[:200])
        return r.ok
    except Exception as e:
        print("Telegram exception:", e)
        return False


def run_scan_test(cfg, token, chat_id) -> int:
    """فحص واحد بشروط مخففة (السعر فقط) لنتأكد إن الماسح يشتغل وإن الرسائل توصل."""
    now = datetime.now(ET)
    session = active_session(now) or "regular"
    send(token, chat_id, f"🧪 اختبار الماسح بدأ. الجلسة: {SESSIONS[session][2]} · {now.strftime('%H:%M')} نيويورك")
    try:
        rows = scan(cfg, session, relaxed=True)
    except Exception as e:
        send(token, chat_id, f"❌ فشل الماسح:\n{e}")
        print("خطأ الاختبار:", e)
        return 0
    if not rows:
        send(token, chat_id, "⚠️ الماسح اشتغل لكن ما رجّع أي سهم بنطاق السعر الحالي. غيّر price_min/price_max.")
        return 0
    send(token, chat_id, f"✅ الماسح شغال ورجّع {len(rows)} أسهم (أعلى حجم في نطاق سعرك). هذي عينة:")
    for d in rows[:3]:
        send(token, chat_id, format_msg(d))
    mode = data_mode(rows[0])
    if "متأخرة" in mode:
        send(token, chat_id, f"⚠️ نوع بيانات الماسح: {mode}.\n"
                             "يعني أي تنبيه يوصلك يكون متأخر بنفس المقدار عن السوق الحقيقي. "
                             "هذا من المصدر المجاني، مو من البوت.")
    else:
        send(token, chat_id, f"✅ نوع بيانات الماسح: {mode}")
    # الآن نجرب شروطك الحقيقية ونخبرك بالعدد
    try:
        real = scan(cfg, session)
        send(token, chat_id, f"📊 بشروطك الحالية في هذي الجلسة: {len(real)} سهم يطابق.")
    except Exception as e:
        send(token, chat_id, f"❌ شروطك الحقيقية فيها مشكلة:\n{e}")
    return 0


def main() -> int:
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print("ناقص TELEGRAM_TOKEN أو TELEGRAM_CHAT_ID في أسرار GitHub")
        return 1
    cfg = load_cfg()

    if os.environ.get("TEST_MODE", "").lower() == "true":
        send(token, chat_id, "✅ التنبيهات شغالة. أي سهم بيني يطابق شروطك بيجيك هنا.")
        print("تم إرسال رسالة التجربة")
        return 0
    if os.environ.get("SCAN_TEST", "").lower() == "true":
        return run_scan_test(cfg, token, chat_id)

    started = time.time()
    last_alert = {}    # الرمز -> نسبة التغير وقت آخر تنبيه
    total_sent, scans_ok, scans_err = 0, 0, 0
    hb_every = cfg.get("heartbeat_minutes", 60) * 60
    last_hb, last_err_sent = time.time(), 0.0
    send(token, chat_id, "🟢 بدأ الفحص المباشر. لو ما جاك شي فمعناه ما فيه سهم يطابق شروطك حالياً، "
                         "وراح أرسل لك تقرير حالة كل ساعة.")
    print("بدأ الفحص...")
    while time.time() - started < 350 * 60:
        now = datetime.now(ET)
        if now.hour >= 20:
            print("انتهت جلسات اليوم")
            break
        session = active_session(now)
        if session is None or session not in cfg.get("sessions", list(SESSIONS)):
            time.sleep(cfg.get("interval_seconds", 30))
            continue
        try:
            sent = 0
            for d in scan(cfg, session):
                sym, chg = d["name"], d["_chg"]
                if chg is None:
                    continue
                prev = last_alert.get(sym)
                if prev is None or chg >= prev + cfg.get("realert_step", 30):
                    send(token, chat_id, format_msg(d))
                    last_alert[sym] = chg
                    sent += 1
                    if sent >= cfg.get("max_alerts_per_scan", 5):
                        break
            total_sent += sent
            scans_ok += 1
            print(now.strftime("%H:%M:%S"), session, "تنبيهات مرسلة:", sent)
        except Exception as e:  # لا نوقف الحلقة بسبب خطأ مؤقت
            scans_err += 1
            print("خطأ:", e)
            if time.time() - last_err_sent > 1800:   # نخبرك بالخطأ مرة كل نصف ساعة
                send(token, chat_id, f"⚠️ خطأ في الفحص (الجلسة: {SESSIONS[session][2]}):\n{e}")
                last_err_sent = time.time()
        if hb_every and time.time() - last_hb >= hb_every:
            send(token, chat_id, f"ℹ️ البوت شغال · {now.strftime('%H:%M')} نيويورك · "
                                 f"فحوصات ناجحة: {scans_ok} · فاشلة: {scans_err} · تنبيهات مرسلة: {total_sent}")
            last_hb = time.time()
        time.sleep(cfg.get("interval_seconds", 30))
    return 0


if __name__ == "__main__":
    sys.exit(main())
