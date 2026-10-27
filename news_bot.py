"""
HABER MODU (sadece bilgi): Haber cikinca ilgili yeni Solana tokenlerini bulur,
SIKI guvenlik kontrolunden gecenleri Telegram'a yollar. Alim/satim YAPMAZ.

Calistirma: python news_bot.py   (GitHub Actions'ta her turda bir kez)
"""
import os, re, json, time, html, statistics, requests
from urllib.parse import quote
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

CFG = {
    "headline_max_age_min": 90,
    "max_headlines": 80,
    "max_keywords_total": 60,
    "max_keywords_per_headline": 5,
    "auto_keywords": True,
    "fresh_token_limit": 90,
    "token_max_age_hours": 12,
    "token_min_age_min": 15,        # cok taze (ilk dakikalar) tokenleri gosterme
    "token_before_news_min": 60,
    "min_liquidity": 5_000,
    "min_txns_h1": 20,
    "require_social": True,         # X veya Telegram linki sart
    "min_lp_locked_pct": 90,        # LP kilidi/yakimi sart (rug pull korumasi)
    "max_top_holder_pct": 30,
    "max_insider_pct": 20,
    "celeb_max_top_holder_pct": 10,  # unlu/marka adli tokenlere ekstra siki
    "celeb_max_top10_pct": 30,
    "reject_cache_hours": 6,
    "tokens_per_headline": 3,
    "max_messages_per_run": 6,
    "silent_mode": False,           # False: Telegram'a sinyal at VE kaydet/olc; True: sadece kaydet
    "report_min_signals": 5,        # rapor icin 1 saatlik verisi olan en az sinyal
    "report_every_hours": 24,
}

WATCH = ["Trump", "Elon", "Musk", "Tesla", "SpaceX", "Neuralink", "Grok", "xAI",
         "OpenAI", "Nvidia", "Starship", "Putin", "Powell", "Fed", "DOGE"]

# Unlu/marka isimleri: bunlarla cikan tokenlerin cogu sahtedir
CELEBS = ["mrbeast", "elon", "musk", "trump", "melania", "biden", "obama", "putin",
          "zelensky", "kanye", "drake", "messi", "ronaldo", "swift", "kardashian",
          "zuckerberg", "bezos", "vitalik", "buterin", "binance", "tesla", "spacex",
          "openai", "nvidia", "google", "disney", "nike", "ye", "cz"]

STOP = {"This", "That", "With", "From", "After", "Before", "About", "Over", "Into",
        "Will", "Says", "Said", "What", "When", "Where", "Which", "Their", "They",
        "More", "Most", "Than", "Have", "Here", "Just", "New", "News", "Live",
        "Today", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
        "Sunday", "January", "February", "March", "April", "June", "July", "August",
        "September", "October", "November", "December", "Reuters", "Associated", "Press",
        "Times", "Post", "Journal", "Video", "Photos", "Watch", "Update", "Updates",
        "President", "States", "United", "Government", "Report", "Reports", "Week", "Year"}

# Sirdan/surekli yazilan siradan basliklari ele
JUNK = re.compile(
    r"\bhow (much|many|old|tall|to)\b|\bwho (is|are|was)\b|\bwhat (to know|is|are)\b|"
    r"\bnet worth\b|\bexplained\b|\bhoroscope\b|\bthings to know\b|\bwhere to watch\b|"
    r"\bvs\.?\s|\blive (updates|blog|stream|score)\b|\bhighlights\b|\bobituary\b|\bdies\b",
    re.I)

GN = "https://news.google.com/rss"
TOPICS = ["WORLD", "NATION", "BUSINESS", "TECHNOLOGY", "ENTERTAINMENT", "SPORTS", "SCIENCE", "HEALTH"]
FEEDS = (
    [f"{GN}?hl=en-US&gl=US&ceid=US:en"]
    + [f"{GN}/headlines/section/topic/{t}?hl=en-US&gl=US&ceid=US:en" for t in TOPICS]
    + [f"{GN}/search?q=" + "+OR+".join(WATCH) + "+when:3h&hl=en-US&gl=US&ceid=US:en",
       "https://trends.google.com/trending/rss?geo=US",
       "http://feeds.bbci.co.uk/news/world/rss.xml",
       "https://www.coindesk.com/arc/outboundfeeds/rss/"]
)

DS = "https://api.dexscreener.com"
RC = "https://api.rugcheck.xyz/v1"
STATE_FILE = "news_state.json"
UA = {"User-Agent": "Mozilla/5.0"}


def get_json(url):
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("istek hatasi:", e)
    return None


def load_state():
    s = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
    s.setdefault("sent", [])
    s.setdefault("rejected", {})
    return s


def save_state(s):
    s["sent"] = s["sent"][-500:]
    cutoff = time.time() - CFG["reject_cache_hours"] * 3600
    s["rejected"] = {m: t for m, t in s["rejected"].items() if t > cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1)


def send_telegram(html_msg):
    tok, chat = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT_ID")
    if not (tok and chat):
        print("(Telegram ayari yok)")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": html_msg, "parse_mode": "HTML",
                            "disable_web_page_preview": "true"}, timeout=10)
    except Exception as e:
        print("telegram hatasi:", e)


def fetch_headlines():
    out, now = [], time.time()
    for url in FEEDS:
        try:
            r = requests.get(url, timeout=15, headers=UA)
            root = ET.fromstring(r.content)
        except Exception as e:
            print("feed hatasi:", url[:50], e)
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            try:
                ts = parsedate_to_datetime(it.findtext("pubDate")).timestamp()
            except Exception:
                continue
            if not title or (now - ts) / 60 > CFG["headline_max_age_min"]:
                continue
            if " - " in title:
                title = title.rsplit(" - ", 1)[0]
            if JUNK.search(title):
                continue
            out.append({"title": title, "link": link, "ts": ts})
    seen, uniq = set(), []
    for h in sorted(out, key=lambda x: -x["ts"]):
        if h["title"] not in seen:
            seen.add(h["title"])
            uniq.append(h)
    return uniq[:CFG["max_headlines"]]


def keywords_for(title):
    kws = [w for w in WATCH if re.search(r"\b" + re.escape(w) + r"\b", title, re.I)]
    have = {k.lower() for k in kws}
    if CFG["auto_keywords"]:
        words = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", title)[1:]
        words += re.findall(r"\b[A-Z]{3,6}\b", title)
        for w in words:
            if w not in STOP and w.lower() not in have:
                have.add(w.lower())
                kws.append(w)
    return kws


def search_tokens(kw, cache):
    if kw in cache:
        return cache[kw]
    data = get_json(f"{DS}/latest/dex/search?q={requests.utils.quote(kw)}") or {}
    pairs = [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
    cache[kw] = pairs
    return pairs


def fresh_pairs():
    profiles = get_json(f"{DS}/token-profiles/latest/v1") or []
    mints = list(dict.fromkeys(p["tokenAddress"] for p in profiles
                               if p.get("chainId") == "solana"))[:CFG["fresh_token_limit"]]
    best = {}
    for i in range(0, len(mints), 30):
        data = get_json(f"{DS}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []
        for p in data:
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd", 0)
            if m not in best or liq > (best[m].get("liquidity") or {}).get("usd", 0):
                best[m] = p
    return list(best.values())


def has_social(p):
    socials = (p.get("info") or {}).get("socials") or []
    return any(str(s.get("type", "")).lower() in ("twitter", "x", "telegram") and s.get("url")
               for s in socials)


def is_celeb(p):
    text = (p["baseToken"].get("name", "") + " " + p["baseToken"].get("symbol", "")).lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    return any((c in text) if len(c) >= 4 else (c in words) for c in CELEBS)


def is_new_reaction(p, kw, news_ts):
    created = p.get("pairCreatedAt")
    if not created:
        return False
    age_h = (time.time() * 1000 - created) / 3_600_000
    if age_h > CFG["token_max_age_hours"] or age_h * 60 < CFG["token_min_age_min"]:
        return False
    if created < (news_ts - CFG["token_before_news_min"] * 60) * 1000:
        return False
    name = (p["baseToken"].get("name", "") + " " + p["baseToken"].get("symbol", "")).lower()
    if kw.lower() not in name:
        return False
    if CFG["require_social"] and not has_social(p):
        return False
    liq = (p.get("liquidity") or {}).get("usd", 0)
    h1 = p.get("txns", {}).get("h1", {})
    return liq >= CFG["min_liquidity"] and (h1.get("buys", 0) + h1.get("sells", 0)) >= CFG["min_txns_h1"]


SIGNALS_FILE = "signals.json"
CHECKPOINTS = [("15m", 15), ("1h", 60), ("6h", 360), ("24h", 1440)]


def load_signals():
    if os.path.exists(SIGNALS_FILE):
        with open(SIGNALS_FILE) as f:
            return json.load(f)
    return []


def save_signals(sigs):
    with open(SIGNALS_FILE, "w") as f:
        json.dump(sigs[-2000:], f, indent=1)


def record_signal(sigs, h, p, metrics, celeb):
    ch = p.get("priceChange", {})
    sigs.append({
        "mint": p["baseToken"]["address"], "symbol": p["baseToken"].get("symbol", "?"),
        "name": p["baseToken"].get("name", "?"), "headline": h["title"][:120],
        "t": time.time(), "entry": float(p["priceUsd"]),
        "entry_liq": p["liquidity"]["usd"], "celeb": celeb,
        "age_min": round((time.time() * 1000 - p["pairCreatedAt"]) / 60000),
        "h1": ch.get("h1", 0), **metrics, "cp": {},
    })


def best_pairs_by_mint(mints):
    out = {}
    for i in range(0, len(mints), 30):
        data = get_json(f"{DS}/tokens/v1/solana/{','.join(mints[i:i + 30])}") or []
        for p in data:
            m = p["baseToken"]["address"]
            liq = (p.get("liquidity") or {}).get("usd", 0)
            if m not in out or liq > (out[m].get("liquidity") or {}).get("usd", 0):
                out[m] = p
    return out


def update_signals(sigs):
    """Vakti gelen kontrol noktalarinda (15dk/1s/6s/24s) guncel fiyati kaydeder."""
    now = time.time()
    due = {}
    for s in sigs:
        todo = [n for n, mins in CHECKPOINTS if n not in s["cp"] and now - s["t"] >= mins * 60]
        if todo:
            due[s["mint"]] = todo
    if not due:
        return 0
    pairs = best_pairs_by_mint(list(due))
    n_upd = 0
    for s in sigs:
        for name in due.get(s["mint"], []):
            p = pairs.get(s["mint"])
            liq = (p.get("liquidity") or {}).get("usd", 0) if p else 0
            rugged = (p is None) or liq < 0.2 * s["entry_liq"]
            price = float(p["priceUsd"]) if p and p.get("priceUsd") else 0
            s["cp"][name] = {"ratio": round(price / s["entry"], 4) if s["entry"] else 0,
                             "liq": liq, "rugged": bool(rugged)}
            n_upd += 1
    return n_upd


def stat_line(label, group, cp):
    rows = [s["cp"][cp] for s in group if cp in s["cp"]]
    if not rows:
        return None
    n = len(rows)
    up = sum(1 for r in rows if not r["rugged"] and r["ratio"] >= 1.0) / n * 100
    big = sum(1 for r in rows if not r["rugged"] and r["ratio"] >= 1.3) / n * 100
    rug = sum(1 for r in rows if r["rugged"]) / n * 100
    med = statistics.median([0 if r["rugged"] else r["ratio"] for r in rows])
    return f"{label} (n={n}): giris ustu %{up:.0f}, +%30 %{big:.0f}, rug/cokus %{rug:.0f}, medyan x{med:.2f}"


def build_report(sigs):
    have_1h = [s for s in sigs if "1h" in s["cp"]]
    lines = [f"<b>HABER MODU OLCUM RAPORU (sessiz mod)</b>",
             f"Toplam kayitli sinyal: {len(sigs)} | 1s verisi olan: {len(have_1h)}", ""]
    for name, _ in CHECKPOINTS[1:]:
        ln = stat_line(f"{name} sonra", sigs, name)
        if ln:
            lines.append(ln)
    lines.append("")
    groups = [("Unlu adli", [s for s in have_1h if s["celeb"]]),
              ("Unlu degil", [s for s in have_1h if not s["celeb"]]),
              ("Likidite <15k", [s for s in have_1h if s["entry_liq"] < 15000]),
              ("Likidite >=15k", [s for s in have_1h if s["entry_liq"] >= 15000]),
              ("Yas <60dk", [s for s in have_1h if s["age_min"] < 60]),
              ("Yas >=60dk", [s for s in have_1h if s["age_min"] >= 60])]
    lines.append("1 saatlik kirilim:")
    for label, g in groups:
        ln = stat_line(label, g, "1h")
        if ln:
            lines.append(ln)
    lines.append("")
    lines.append("Not: Giris fiyati sinyal anindaki fiyattir. Gercekte mesaji gorup alana kadar "
                 "fiyat degisir, yani gercek sonuclar bundan KOTU olur. Yatirim tavsiyesi degil.")
    return "\n".join(html.escape(l, quote=False).replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
                     for l in lines)


def maybe_report(sigs, state):
    have_1h = [s for s in sigs if "1h" in s["cp"]]
    if len(have_1h) < CFG["report_min_signals"]:
        return
    if time.time() - state.get("last_report", 0) < CFG["report_every_hours"] * 3600:
        return
    send_telegram(build_report(sigs))
    state["last_report"] = time.time()
    print("olcum raporu gonderildi")


def safety(mint, celeb=False):
    """(kritik_sorunlar, notlar, metrikler). LP kilidi/yakimi ZORUNLU."""
    r = get_json(f"{RC}/tokens/{mint}/report")
    if not r:
        return ["RugCheck verisi yok"], [], {}
    bad, notes = [], []
    if r.get("rugged"):
        bad.append("RugCheck: rug pull olmus")
    if r.get("mintAuthority"):
        bad.append("mint yetkisi acik")
    if r.get("freezeAuthority"):
        bad.append("freeze yetkisi acik")
    if any(x.get("level") == "danger" for x in r.get("risks") or []):
        bad.append("RugCheck danger riski")
    holders = r.get("topHolders") or []
    pcts = sorted([h.get("pct", 0) for h in holders], reverse=True)
    top1, top10 = (pcts[0] if pcts else 0), sum(pcts[:10])
    insider = sum(h.get("pct", 0) for h in holders if h.get("insider"))
    notes.append(f"en buyuk cuzdan %{top1:.0f}, top10 %{top10:.0f}, insider %{insider:.0f}")
    max_top1 = CFG["celeb_max_top_holder_pct"] if celeb else CFG["max_top_holder_pct"]
    if top1 > max_top1:
        bad.append(f"buyuk cuzdan %{top1:.0f}")
    if celeb and top10 > CFG["celeb_max_top10_pct"]:
        bad.append(f"top10 %{top10:.0f}")
    if insider > CFG["max_insider_pct"]:
        bad.append(f"insider cuzdanlar %{insider:.0f}")
    locks = [(m.get("lp") or {}).get("lpLockedPct", 0) for m in r.get("markets") or []]
    lock = max(locks) if locks else 0
    notes.append(f"LP kilit/yakim %{lock:.0f}")
    if lock < CFG["min_lp_locked_pct"]:
        bad.append(f"LP kilitli degil (%{lock:.0f})")
    return bad, notes, {"top1": round(top1, 1), "top10": round(top10, 1),
                        "insider": round(insider, 1), "lock": round(lock, 1)}


def links_of(p):
    info = p.get("info") or {}
    out = [("Web", w["url"]) for w in info.get("websites") or [] if w.get("url")]
    out += [(str(s.get("type", "link")).capitalize(), s["url"])
            for s in info.get("socials") or [] if s.get("url")]
    return out


def build_message(h, picks):
    e = html.escape
    x_news = f"https://x.com/search?q={quote(h['title'][:100])}&f=live"
    lines = [f"<b>HABER:</b> <a href=\"{e(h['link'])}\">{e(h['title'])}</a>"
             f" | <a href=\"{e(x_news)}\">X'te haberi ara</a>", ""]
    for p, notes, celeb, _ in picks:
        mint = p["baseToken"]["address"]
        ch = p.get("priceChange", {})
        age_min = (time.time() * 1000 - p["pairCreatedAt"]) / 60000
        lines.append(f"<b>{e(p['baseToken'].get('name', '?'))} (${e(p['baseToken'].get('symbol', '?'))})</b>")
        if celeb:
            lines.append("DIKKAT: Unlu/marka adiyla cikan token. Bunlarin cogu sahte ya da rug pull.")
        lines.append(f"Yas: {age_min:.0f} dk | Likidite: {p['liquidity']['usd']:,.0f}$ | 1s: {ch.get('h1', 0)}%")
        lines.append("Guvenlik: " + e("; ".join(notes)))
        lines.append(f"CA (dokununca kopyalanir; FOMO aramasina yapistir):\n<code>{e(mint)}</code>")
        x_ca = f"https://x.com/search?q={quote(mint)}&f=live"
        x_tk = f"https://x.com/search?q={quote('$' + p['baseToken'].get('symbol', ''))}&f=live"
        lines.append(f"<a href=\"{e(p.get('url', ''))}\">Grafik</a>"
                     f" | <a href=\"{e(x_ca)}\">X'te CA ara</a>"
                     f" | <a href=\"{e(x_tk)}\">X'te ticker ara</a>" +
                     "".join(f" | <a href=\"{e(u)}\">{e(n)}</a>" for n, u in links_of(p)))
        lines.append("")
    lines.append("Not: Resmi oldugu DOGRULANMADI, kopya tokenler cok yaygin. "
                 "Yatirim tavsiyesi degil, alim yapilmadi.")
    return "\n".join(lines)


def main():
    state = load_state()
    sigs = load_signals()
    upd = update_signals(sigs)
    print(f"olcum: {len(sigs)} kayitli sinyal, {upd} kontrol noktasi guncellendi")
    heads = fetch_headlines()
    print(f"{len(heads)} taze haber bulundu")
    cache, sent_now, total_kw = {}, 0, 0
    fresh = fresh_pairs()
    print(f"ters tarama icin {len(fresh)} taze token")
    now = time.time()
    for h in heads:
        if sent_now >= CFG["max_messages_per_run"]:
            break
        kws = keywords_for(h["title"])
        if not kws:
            continue
        cands = {}

        def consider(p, kw):
            mint = p["baseToken"]["address"]
            if mint in state["sent"] or mint in state["rejected"] or not is_new_reaction(p, kw, h["ts"]):
                return
            if mint not in cands or p["liquidity"]["usd"] > cands[mint]["liquidity"]["usd"]:
                cands[mint] = p

        if total_kw < CFG["max_keywords_total"]:
            for kw in kws[:CFG["max_keywords_per_headline"]]:
                total_kw += 1
                for p in search_tokens(kw, cache):
                    consider(p, kw)
        for p in fresh:
            for kw in kws:
                consider(p, kw)

        ranked = sorted(cands.values(), key=lambda p: -p["liquidity"]["usd"])
        picks, names = [], set()
        for p in ranked[:CFG["tokens_per_headline"] * 3]:
            mint = p["baseToken"]["address"]
            key = p["baseToken"].get("symbol", "").lower()
            if key in names:        # ayni isimli kopyalar: sadece en likit olan
                continue
            celeb = is_celeb(p)
            bad, notes, metrics = safety(mint, celeb)
            if bad:
                state["rejected"][mint] = now
                print(f"elendi {p['baseToken']['symbol']}: {', '.join(bad)}")
                continue
            names.add(key)
            picks.append((p, notes, celeb, metrics))
            if len(picks) >= CFG["tokens_per_headline"]:
                break
        if picks:
            for p, _, celeb, metrics in picks:      # her durumda olcum icin kaydet
                record_signal(sigs, h, p, metrics, celeb)
            if CFG["silent_mode"]:
                print("kaydedildi (sessiz):", h["title"][:70], "->",
                      ", ".join(p["baseToken"]["symbol"] for p, *_ in picks))
            else:
                send_telegram(build_message(h, picks))
                print("gonderildi + kaydedildi:", h["title"][:70])
            state["sent"] += [p["baseToken"]["address"] for p, *_ in picks]
            sent_now += 1
    save_signals(sigs)
    maybe_report(sigs, state)
    save_state(state)
    print("bitti, gonderilen mesaj:", sent_now)


if __name__ == "__main__":
    main()
