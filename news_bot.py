"""
HABER MODU (sadece bilgi): Haber cikinca ilgili yeni Solana tokenlerini bulur,
guvenlik kontrolunden gecenleri Telegram'a yollar. Alim/satim YAPMAZ.

Calistirma: python news_bot.py   (GitHub Actions'ta her turda bir kez)
"""
import os, re, json, time, html, requests
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

CFG = {
    "headline_max_age_min": 90,   # bu kadar dakikadan eski haberlere bakma
    "max_headlines": 80,
    "max_keywords_total": 60,
    "max_keywords_per_headline": 5,   # API aramasi icin sinir
    "auto_keywords": True,        # basliktaki ozel isimleri de anahtar kelime yap
    "token_max_age_hours": 12,    # haberden sonra cikan YENI tokenler
    "token_before_news_min": 60,  # haberden en fazla 60dk once cikmis olabilir
    "min_liquidity": 5_000,
    "min_txns_h1": 20,
    "max_top_holder_pct": 30,
    "tokens_per_headline": 3,
    "max_messages_per_run": 6,
    "fresh_token_limit": 90,      # ters tarama: son profil alan token sayisi
}

WATCH = ["Trump", "Elon", "Musk", "Tesla", "SpaceX", "Neuralink", "Grok", "xAI",
         "OpenAI", "Nvidia", "Starship", "Putin", "Powell", "Fed", "DOGE"]

STOP = {"This", "That", "With", "From", "After", "Before", "About", "Over", "Into",
        "Will", "Says", "Said", "What", "When", "Where", "Which", "Their", "They",
        "More", "Most", "Than", "Have", "Here", "Just", "New", "News", "Live",
        "Today", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
        "Sunday", "January", "February", "March", "April", "June", "July", "August",
        "September", "October", "November", "December", "Reuters", "Associated", "Press",
        "Times", "Post", "Journal", "Video", "Photos", "Watch", "Update", "Updates",
        "President", "States", "United", "Government", "Report", "Reports", "Week", "Year"}

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
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"sent": []}


def save_state(s):
    s["sent"] = s["sent"][-500:]
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
            if " - " in title:  # Google News: "Baslik - Kaynak"
                title = title.rsplit(" - ", 1)[0]
            out.append({"title": title, "link": link, "ts": ts})
    seen, uniq = set(), []
    for h in sorted(out, key=lambda x: -x["ts"]):
        if h["title"] not in seen:
            seen.add(h["title"])
            uniq.append(h)
    return uniq[:CFG["max_headlines"]]


def keywords_for(title):
    """Basliktaki tum anahtar kelimeler (watchlist + ozel isimler + BUYUK HARF kisaltmalar)."""
    kws = [w for w in WATCH if re.search(r"\b" + re.escape(w) + r"\b", title, re.I)]
    have = {k.lower() for k in kws}
    if CFG["auto_keywords"]:
        words = re.findall(r"\b[A-Z][a-zA-Z]{3,}\b", title)[1:]  # ilk kelime cumle basi
        words += re.findall(r"\b[A-Z]{3,6}\b", title)           # NASA, FIFA, GPT gibi
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
    """Son profil alan Solana tokenleri (ters tarama icin). Haberden bagimsiz toplanir."""
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


def is_new_reaction(p, kw, news_ts):
    created = p.get("pairCreatedAt")
    if not created:
        return False
    age_h = (time.time() * 1000 - created) / 3_600_000
    if age_h > CFG["token_max_age_hours"]:
        return False
    if created < (news_ts - CFG["token_before_news_min"] * 60) * 1000:
        return False
    name = (p["baseToken"].get("name", "") + " " + p["baseToken"].get("symbol", "")).lower()
    if kw.lower() not in name:
        return False
    liq = (p.get("liquidity") or {}).get("usd", 0)
    h1 = p.get("txns", {}).get("h1", {})
    return liq >= CFG["min_liquidity"] and (h1.get("buys", 0) + h1.get("sells", 0)) >= CFG["min_txns_h1"]


def safety(mint):
    """(kritik_sorunlar, notlar)"""
    r = get_json(f"{RC}/tokens/{mint}/report")
    if not r:
        return ["RugCheck verisi yok"], []
    bad, notes = [], []
    if r.get("mintAuthority"):
        bad.append("mint yetkisi acik")
    if r.get("freezeAuthority"):
        bad.append("freeze yetkisi acik")
    if any(x.get("level") == "danger" for x in r.get("risks") or []):
        bad.append("RugCheck danger riski")
    pcts = sorted([h.get("pct", 0) for h in r.get("topHolders") or []], reverse=True)
    if pcts:
        notes.append(f"en buyuk cuzdan %{pcts[0]:.0f}, top10 %{sum(pcts[:10]):.0f}")
        if pcts[0] > CFG["max_top_holder_pct"]:
            bad.append(f"buyuk cuzdan %{pcts[0]:.0f}")
    locks = [(m.get("lp") or {}).get("lpLockedPct", 0) for m in r.get("markets") or []]
    notes.append(f"LP kilit %{max(locks):.0f}" if locks else "LP kilit bilgisi yok")
    return bad, notes


def links_of(p):
    info = p.get("info") or {}
    out = [("Web", w["url"]) for w in info.get("websites") or [] if w.get("url")]
    out += [(str(s.get("type", "link")).capitalize(), s["url"])
            for s in info.get("socials") or [] if s.get("url")]
    return out


def build_message(h, picks):
    e = html.escape
    lines = [f"<b>HABER:</b> <a href=\"{e(h['link'])}\">{e(h['title'])}</a>", ""]
    for p, notes in picks:
        mint = p["baseToken"]["address"]
        ch = p.get("priceChange", {})
        age_min = (time.time() * 1000 - p["pairCreatedAt"]) / 60000
        lines.append(f"<b>{e(p['baseToken'].get('name', '?'))} (${e(p['baseToken'].get('symbol', '?'))})</b>")
        lines.append(f"Yas: {age_min:.0f} dk | Likidite: {p['liquidity']['usd']:,.0f}$ | 1s: {ch.get('h1', 0)}%")
        lines.append("Guvenlik: " + e("; ".join(notes)))
        lines.append(f"CA:\n<code>{e(mint)}</code>")
        lines.append(f"<a href=\"{e(p.get('url', ''))}\">Grafik</a>" +
                     "".join(f" | <a href=\"{e(u)}\">{e(n)}</a>" for n, u in links_of(p)))
        lines.append("")
    lines.append("Not: Kopya/sahte tokenler cok yaygin. Resmi oldugu DOGRULANMADI. "
                 "Yatirim tavsiyesi degil, alim yapilmadi.")
    return "\n".join(lines)


def main():
    state = load_state()
    heads = fetch_headlines()
    print(f"{len(heads)} taze haber bulundu")
    cache, sent_now = {}, 0
    total_kw = 0
    fresh = fresh_pairs()
    print(f"ters tarama icin {len(fresh)} taze token")
    for h in heads:
        if sent_now >= CFG["max_messages_per_run"]:
            break
        kws = keywords_for(h["title"])
        if not kws:
            continue
        cands = {}

        def consider(p, kw):
            mint = p["baseToken"]["address"]
            if mint in state["sent"] or not is_new_reaction(p, kw, h["ts"]):
                return
            if mint not in cands or p["liquidity"]["usd"] > cands[mint]["liquidity"]["usd"]:
                cands[mint] = p

        # 1) ileri arama: anahtar kelimeyi DexScreener'da ara (API butcesi sinirli)
        if total_kw < CFG["max_keywords_total"]:
            for kw in kws[:CFG["max_keywords_per_headline"]]:
                total_kw += 1
                for p in search_tokens(kw, cache):
                    consider(p, kw)
        # 2) ters tarama: taze tokenlerin adi basliktaki kelimelerle eslesiyor mu (API'siz)
        for p in fresh:
            for kw in kws:
                consider(p, kw)
        ranked = sorted(cands.values(), key=lambda p: -p["liquidity"]["usd"])[:CFG["tokens_per_headline"] * 2]
        picks = []
        for p in ranked:
            bad, notes = safety(p["baseToken"]["address"])
            if bad:
                print(f"elendi {p['baseToken']['symbol']}: {', '.join(bad)}")
                continue
            picks.append((p, notes))
            if len(picks) >= CFG["tokens_per_headline"]:
                break
        if picks:
            send_telegram(build_message(h, picks))
            print("gonderildi:", h["title"][:80])
            state["sent"] += [p["baseToken"]["address"] for p, _ in picks]
            sent_now += 1
    save_state(state)
    print("bitti, gonderilen mesaj:", sent_now)


if __name__ == "__main__":
    main()
