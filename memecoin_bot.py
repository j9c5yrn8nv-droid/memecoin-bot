"""
Solana memecoin tarayici + PAPER TRADING botu.
Gercek para harcamaz, cuzdan baglamaz. Sadece sinyal uretir ve sanal islem yapar.

Kurulum:  pip install requests
Calistir: python memecoin_bot.py
Telegram uyari (opsiyonel): TG_TOKEN ve TG_CHAT_ID ortam degiskenlerini ayarla.
"""
import os, time, json, requests

# ---------------- AYARLAR ----------------
CFG = {
    "scan_interval": 60,        # saniye
    "start_cash": 1000.0,       # sanal USD
    "trade_size": 50.0,         # islem basina sanal USD
    "max_positions": 5,
    "slippage": 0.015,          # %1.5 simule kayma (giris ve cikista)
    "take_profit": 0.30,        # +%30
    "stop_loss": -0.15,         # -%15
    "max_hold_hours": 24,
    "liq_drop_exit": 0.40,      # likidite %40 duserse rug sinyali, cik
    # piyasa filtreleri
    "min_age_hours": 24,
    "max_age_hours": 24 * 14,
    "min_liquidity": 50_000,
    "min_volume_24h": 100_000,
    "min_txns_24h": 500,
    "h1_change_range": (0, 30),     # saatlik degisim % (pompaya girme)
    "min_buy_sell_ratio_h1": 1.05,
    # guvenlik filtreleri
    "max_top_holder_pct": 20,
    "max_top10_pct": 45,
    "min_lp_locked_pct": 70,
}
DS = "https://api.dexscreener.com"
RC = "https://api.rugcheck.xyz/v1"
STATE_FILE = "paper_state.json"


def get(url):
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("istek hatasi:", e)
    return None


def notify(msg):
    print(msg)
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    if tok and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": msg}, timeout=10)
        except Exception:
            pass


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"cash": CFG["start_cash"], "positions": [], "closed": [], "seen": []}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def best_pair(mint):
    pairs = get(f"{DS}/tokens/v1/solana/{mint}")
    if not pairs:
        return None
    pairs = [p for p in pairs if p.get("priceUsd")]
    if not pairs:
        return None
    return max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0))


def candidates():
    profiles = get(f"{DS}/token-profiles/latest/v1") or []
    return list({p["tokenAddress"] for p in profiles if p.get("chainId") == "solana"})


def market_ok(p):
    liq = (p.get("liquidity") or {}).get("usd", 0)
    vol = (p.get("volume") or {}).get("h24", 0)
    tx = p.get("txns", {}).get("h24", {})
    txs = tx.get("buys", 0) + tx.get("sells", 0)
    h1 = p.get("txns", {}).get("h1", {})
    buys, sells = h1.get("buys", 0), h1.get("sells", 0)
    ch = p.get("priceChange", {})
    created = p.get("pairCreatedAt")
    if not created:
        return False, "yas bilinmiyor"
    age_h = (time.time() * 1000 - created) / 3_600_000
    if not CFG["min_age_hours"] <= age_h <= CFG["max_age_hours"]:
        return False, f"yas {age_h:.0f}s"
    if liq < CFG["min_liquidity"]:
        return False, "likidite dusuk"
    if vol < CFG["min_volume_24h"] or txs < CFG["min_txns_24h"]:
        return False, "hacim/islem dusuk"
    lo, hi = CFG["h1_change_range"]
    if not lo <= ch.get("h1", 0) <= hi:
        return False, f"h1 degisim {ch.get('h1', 0)}%"
    if ch.get("m5", 0) < 0:
        return False, "son 5dk dusus"
    if sells and buys / sells < CFG["min_buy_sell_ratio_h1"]:
        return False, "satis baskisi"
    return True, "ok"


def safety_ok(mint):
    r = get(f"{RC}/tokens/{mint}/report")
    if not r:
        return False, "rugcheck yanit yok"
    if r.get("mintAuthority") or r.get("freezeAuthority"):
        return False, "mint/freeze yetkisi acik"
    if any(x.get("level") == "danger" for x in r.get("risks") or []):
        return False, "rugcheck danger riski"
    holders = r.get("topHolders") or []
    pcts = sorted([h.get("pct", 0) for h in holders], reverse=True)
    if pcts and pcts[0] > CFG["max_top_holder_pct"]:
        return False, f"buyuk cuzdan %{pcts[0]:.0f}"
    if sum(pcts[:10]) > CFG["max_top10_pct"]:
        return False, f"top10 %{sum(pcts[:10]):.0f}"
    locks = [(m.get("lp") or {}).get("lpLockedPct", 0) for m in r.get("markets") or []]
    if not locks or max(locks) < CFG["min_lp_locked_pct"]:
        return False, "LP kilitli degil"
    return True, "ok"


def open_position(s, p):
    price = float(p["priceUsd"]) * (1 + CFG["slippage"])
    size = CFG["trade_size"]
    s["cash"] -= size
    pos = {
        "mint": p["baseToken"]["address"], "symbol": p["baseToken"]["symbol"],
        "entry": price, "qty": size / price, "size": size,
        "entry_liq": p["liquidity"]["usd"], "opened": time.time(),
    }
    s["positions"].append(pos)
    notify(f"SINYAL (paper giris): {pos['symbol']} @ {price:.8f}$\n"
           f"Likidite: {pos['entry_liq']:,.0f}$ | {p.get('url', '')}")


def manage_positions(s):
    for pos in list(s["positions"]):
        p = best_pair(pos["mint"])
        if not p:
            continue
        price = float(p["priceUsd"]) * (1 - CFG["slippage"])
        pnl = price / pos["entry"] - 1
        liq = (p.get("liquidity") or {}).get("usd", 0)
        hours = (time.time() - pos["opened"]) / 3600
        reason = None
        if pnl >= CFG["take_profit"]:
            reason = "kar al"
        elif pnl <= CFG["stop_loss"]:
            reason = "stop-loss"
        elif liq < pos["entry_liq"] * (1 - CFG["liq_drop_exit"]):
            reason = "likidite cekiliyor (rug riski)"
        elif hours >= CFG["max_hold_hours"]:
            reason = "sure doldu"
        if reason:
            value = pos["qty"] * price
            s["cash"] += value
            s["positions"].remove(pos)
            s["closed"].append({"symbol": pos["symbol"], "pnl_pct": round(pnl * 100, 2),
                                "pnl_usd": round(value - pos["size"], 2), "reason": reason})
            notify(f"CIKIS (paper): {pos['symbol']} {pnl*100:+.1f}% ({reason})")


def stats(s):
    c = s["closed"]
    wins = [x for x in c if x["pnl_usd"] > 0]
    total = sum(x["pnl_usd"] for x in c)
    rate = (len(wins) / len(c) * 100) if c else 0
    print(f"[RAPOR] nakit: {s['cash']:.2f}$ | acik: {len(s['positions'])} | "
          f"kapanan: {len(c)} | basari: %{rate:.0f} | toplam PnL: {total:+.2f}$")


def run_once(s):
    manage_positions(s)
    held = {p["mint"] for p in s["positions"]}
    for mint in candidates():
        if len(s["positions"]) >= CFG["max_positions"] or s["cash"] < CFG["trade_size"]:
            break
        if mint in held or mint in s["seen"]:
            continue
        p = best_pair(mint)
        if not p:
            continue
        ok, why = market_ok(p)
        if not ok:
            continue  # sonra tekrar bakilabilir, seen'e eklemiyoruz
        s["seen"].append(mint)
        ok, why = safety_ok(mint)
        if ok:
            open_position(s, p)
        else:
            print(f"elendi {p['baseToken']['symbol']}: {why}")
    s["seen"] = s["seen"][-500:]
    save_state(s)
    stats(s)


def main():
    s = load_state()
    if os.getenv("RUN_ONCE"):  # GitHub Actions modu: tek tur calis ve cik
        run_once(s)
        return
    notify("Paper trading botu basladi (gercek para YOK).")
    while True:
        try:
            run_once(s)
        except Exception as e:
            print("dongu hatasi:", e)
        time.sleep(CFG["scan_interval"])


if __name__ == "__main__":
    main()
