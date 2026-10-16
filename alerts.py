# -*- coding: utf-8 -*-
"""تنبيهات تيليجرام لأسهم البيني (Penny stocks) في كل الجلسات.
يشتغل على GitHub Actions: يفحص السوق كل N ثانية ويرسل أي سهم يطابق alerts_config.json
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
        "float_shares_outstanding", "market_cap_basic", "RSI"]


def load_cfg() -> dict:
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "alerts_config.json"), encoding="utf-8") as f:
        return json.load(f)


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


def build_payload(cfg: dict, session: str) -> dict:
    chg, vol, _ = SESSIONS[session]
    f = [
        {"left": "type", "operation": "equal", "right": "stock"},
        {"left": "typespecs", "operation": "has", "right": ["common"]},
        {"left": "exchange", "operation": "in_range", "right": cfg["exchanges"]},
        {"left": "close", "operation": "in_range", "right": [cfg["price_min"], cfg["price_max"]]},
        {"left": chg, "operation": "greater", "right": cfg["min_change_pct"]},
        {"left": vol, "operation": "greater", "right": cfg["min_volume"][session]},
    ]
    if cfg.get("mcap_max_million"):
        f.append({"left": "market_cap_basic", "operation": "less", "right": cfg["mcap_max_million"] * 1e6})
    if cfg.get("float_max_million"):
        f.append({"left": "float_shares_outstanding", "operation": "less", "right": cfg["float_max_million"] * 1e6})
    if cfg.get("rvol_min"):
        f.append({"left": "relative_volume_10d_calc", "operation": "greater", "right": cfg["rvol_min"]})
    if cfg.get("rsi_min") is not None:
        f.append({"left": "RSI", "operation": "greater", "right": cfg["rsi_min"]})
    if cfg.get("rsi_max") is not None:
        f.append({"left": "RSI", "operation": "less", "right": cfg["rsi_max"]})
    return {
        "filter": f, "options": {"lang": "en"}, "markets": ["america"],
        "symbols": {"query": {"types": []}, "tickers": []},
        "columns": COLS,
        "sort": {"sortBy": chg, "sortOrder": "desc"},
        "range": [0, 50],
    }


def scan(cfg: dict, session: str) -> list:
    r = requests.post(SCAN_URL, json=build_payload(cfg, session), timeout=30,
                      headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    chg, vol, _ = SESSIONS[session]
    out = []
    for item in r.json().get("data", []):
        d = dict(zip(COLS, item["d"]))
        d["_session"], d["_chg"], d["_vol"] = session, d.get(chg), d.get(vol)
        out.append(d)
    return out


def millions(x):
    return "غير متوفر" if x is None else f"{x / 1e6:,.1f}M"


def format_msg(d: dict) -> str:
    now = datetime.now(ET).strftime("%H:%M:%S")
    rsi, rvol, vol = d.get("RSI"), d.get("relative_volume_10d_calc"), d.get("_vol")
    return "\n".join([
        f"🚨 {d['name']} — {d.get('description') or ''}",
        f"الجلسة: {SESSIONS[d['_session']][2]}",
        f"السعر: ${d['close']:.4f}",
        f"التغير: +{d['_chg']:.1f}%",
        f"الحجم: {'غير متوفر' if vol is None else f'{int(vol):,}'}",
        f"الحجم النسبي: {'غير متوفر' if rvol is None else f'{rvol:.1f}x'}",
        f"Float: {millions(d.get('float_shares_outstanding'))}",
        f"القيمة السوقية: {millions(d.get('market_cap_basic'))}",
        f"RSI: {'غير متوفر' if rsi is None else f'{rsi:.0f}'}",
        f"وقت الفحص (نيويورك): {now}",
        f"https://www.tradingview.com/chart/?symbol={d['name']}",
    ])


def send(token: str, chat_id: str, text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
        timeout=20,
    )
    if not r.ok:
        print("Telegram error:", r.status_code, r.text[:200])


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

    started = time.time()
    last_alert = {}   # الرمز -> نسبة التغير وقت آخر تنبيه
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
                prev = last_alert.get(sym)
                if prev is None or chg >= prev + cfg.get("realert_step", 30):
                    send(token, chat_id, format_msg(d))
                    last_alert[sym] = chg
                    sent += 1
                    if sent >= cfg.get("max_alerts_per_scan", 5):
                        break
            print(now.strftime("%H:%M:%S"), session, "تنبيهات مرسلة:", sent)
        except Exception as e:  # لا نوقف الحلقة بسبب خطأ مؤقت
            print("خطأ:", e)
        time.sleep(cfg.get("interval_seconds", 30))
    return 0


if __name__ == "__main__":
    sys.exit(main())
