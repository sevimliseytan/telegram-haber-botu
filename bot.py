"""
Haber sitelerinin RSS akislarini takip edip yeni haberleri Telegram kanalina atan bot.
X API'si gerekmez, tamamen ucretsizdir.

Gerekli ortam degiskenleri:
  TELEGRAM_BOT_TOKEN   BotFather'in verdigi token
  TELEGRAM_CHAT_ID     Kanal adi (ornek: @kanaladi) ya da sayisal kanal ID'si
Istege bagli:
  ANAHTAR_KELIMELER    Virgulle ayrilmis kelimeler. Doluysa sadece bunlari iceren
                       haberler atilir. Ornek: "son dakika,Balıkesir,deprem"
  LOOP_SECONDS         Doluysa bot surekli calisir ve bu kadar saniyede bir kontrol
                       eder (sunucuda calistirmak icin). Bossa tek tur calisir.
  DRY_RUN              "1" ise Telegram'a gondermez, sadece ekrana yazar (deneme icin).
"""

import difflib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

# ------------------------------------------------------------------ KAYNAKLAR
# Kaynak eklemek/cikarmak icin bu listeyi duzenle. Basina # koyulan satir kapali olur.
SOURCES = [
    # Son dakika
    {"ad": "TRT Haber", "url": "https://www.trthaber.com/sondakika_articles.rss"},
    # Tum kategoriler (gundem, siyaset, ekonomi, spor, dunya...)
    {"ad": "Anadolu Ajansı", "url": "https://www.aa.com.tr/tr/rss/default?cat=guncel"},
    {"ad": "CNN Türk", "url": "https://www.cnnturk.com/feed/rss/all/news"},
    {"ad": "Habertürk", "url": "https://www.haberturk.com/rss"},
    {"ad": "Ensonhaber", "url": "https://www.ensonhaber.com/rss/ensonhaber.xml"},
    {"ad": "Sözcü", "url": "https://www.sozcu.com.tr/feeds-son-dakika"},
    {"ad": "NTV", "url": "https://www.ntv.com.tr/gundem.rss"},
    {"ad": "Haber7", "url": "https://i12.haber7.net/sondakika/newsstand/latest.xml"},
]

MAX_PER_SOURCE_PER_RUN = 15   # bot uzun sure kapali kalirsa kanal haberle dolmasin
SUMMARY_CHARS = 300           # haber ozetinin uzunlugu
SIMILARITY = 0.85             # bu orandan benzer basliklar ayni haber sayilir

# ------------------------------------------------------------------ AYARLAR
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
KEYWORDS = [k.strip().casefold() for k in os.environ.get("ANAHTAR_KELIMELER", "").split(",") if k.strip()]
LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "0") or 0)
DRY_RUN = os.environ.get("DRY_RUN") == "1"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; HaberKanaliBot/1.0)"}


# ------------------------------------------------------------------ durum dosyasi
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    state.setdefault("seen", {})
    state.setdefault("recent_titles", [])
    return state


def save_state(state):
    for url in state["seen"]:
        state["seen"][url] = state["seen"][url][-500:]
    state["recent_titles"] = state["recent_titles"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------ RSS / Atom okuma
def local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def first_img_in_html(text):
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', text or "", re.I)
    return m.group(1) if m else None


def parse_date(value):
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        f = {"title": "", "link": "", "id": "", "date": None, "summary": "", "image": None}
        raw_html = ""
        for ch in el:
            name, text = local(ch.tag), (ch.text or "").strip()
            if name == "title":
                f["title"] = clean(text)
            elif name == "link":
                href = ch.get("href")
                if href and ch.get("rel", "alternate") == "alternate":
                    f["link"] = href
                elif text and not f["link"]:
                    f["link"] = text
            elif name in ("guid", "id"):
                f["id"] = text
            elif name in ("pubdate", "published", "updated", "date") and not f["date"]:
                f["date"] = parse_date(text)
            elif name in ("description", "summary", "encoded", "content", "spot"):
                raw_html = raw_html or text
                if not f["summary"]:
                    f["summary"] = clean(text)
            elif name == "image" and text.startswith("http") and not f["image"]:
                f["image"] = text
        # gorsel: enclosure, media:content, media:thumbnail, sonra metindeki <img>
        if not f["image"]:
            for ch in el.iter():
                name = local(ch.tag)
                url = ch.get("url") or ch.get("href")
                if not url:
                    continue
                ctype = (ch.get("type") or "") + (ch.get("medium") or "")
                is_enclosure = name == "enclosure" or (name == "link" and ch.get("rel") == "enclosure")
                if is_enclosure and "image" in ctype:
                    f["image"] = url
                    break
                if name in ("content", "thumbnail") and ("image" in ctype or re.search(r"\.(jpe?g|png|webp)(\?|$)", url, re.I)):
                    f["image"] = url
                    break
        if not f["image"]:
            f["image"] = first_img_in_html(raw_html)
        f["id"] = f["id"] or f["link"] or f["title"]
        if f["title"] and f["link"]:
            items.append(f)
    return items


def fetch(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return parse_feed(r.content)


# ------------------------------------------------------------------ yardimcilar
def norm_title(t):
    return re.sub(r"[^\w\s]", "", t.casefold()).strip()


def is_duplicate(title, recent):
    n = norm_title(title)
    return any(difflib.SequenceMatcher(None, n, r).ratio() >= SIMILARITY for r in recent)


def matches_keywords(item):
    if not KEYWORDS:
        return True
    hay = (item["title"] + " " + item["summary"]).casefold()
    return any(k in hay for k in KEYWORDS)


def shorten(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


def build_caption(item, source_name):
    title = html.escape(item["title"], quote=False)
    summary = item["summary"]
    if summary and norm_title(summary).startswith(norm_title(item["title"])):
        summary = summary[len(item["title"]):].strip()
    summary = html.escape(shorten(summary, SUMMARY_CHARS), quote=False) if summary else ""
    link = html.escape(item["link"], quote=True)
    parts = [f"<b>{title}</b>"]
    if summary:
        parts.append(summary)
    parts.append(f'<a href="{link}">Kaynak: {html.escape(source_name, quote=False)}</a>')
    return "\n\n".join(parts)


# ------------------------------------------------------------------ Telegram
def tg(method, payload):
    if DRY_RUN:
        print(f"[DENEME] {method}: {json.dumps(payload, ensure_ascii=False)[:500]}")
        return {}
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=payload, timeout=30)
        if r.ok:
            return r.json()
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        raise RuntimeError(f"Telegram hatasi {r.status_code}: {r.text}")
    raise RuntimeError("Telegram'a gonderilemedi")


def send(item, source_name):
    caption = build_caption(item, source_name)
    if item["image"]:
        try:
            tg("sendPhoto", {"chat_id": TG_CHAT, "photo": item["image"],
                             "caption": caption[:1024], "parse_mode": "HTML"})
            return
        except RuntimeError as e:
            print(f"Gorsel gonderilemedi, metin olarak atiliyor: {e}")
    tg("sendMessage", {"chat_id": TG_CHAT, "text": caption, "parse_mode": "HTML",
                       "disable_web_page_preview": False})


# ------------------------------------------------------------------ ana akis
def run_once():
    state = load_state()
    for src in SOURCES:
        url, name = src["url"], src["ad"]
        try:
            items = fetch(url)
        except Exception as e:
            print(f"[{name}] okunamadi: {e}")
            continue

        seen = state["seen"].get(url)
        if seen is None:
            # Kaynagin ilk okunusu: eski haberleri atmadan baslangic noktasi kaydedilir.
            state["seen"][url] = [i["id"] for i in items]
            state["recent_titles"] += [norm_title(i["title"]) for i in items]
            save_state(state)
            print(f"[{name}] ilk okuma: {len(items)} haber isaretlendi, bundan sonrakiler atilacak.")
            continue

        seen_set = set(seen)
        new = [i for i in items if i["id"] not in seen_set]
        if not new:
            print(f"[{name}] yeni haber yok.")
            continue

        # eskiden yeniye sirala
        if all(i["date"] for i in new):
            new.sort(key=lambda i: i["date"])
        else:
            new.reverse()

        skipped = new[:-MAX_PER_SOURCE_PER_RUN]
        for i in skipped:
            seen.append(i["id"])
        for item in new[-MAX_PER_SOURCE_PER_RUN:]:
            seen.append(item["id"])
            if not matches_keywords(item):
                continue
            if is_duplicate(item["title"], state["recent_titles"]):
                print(f"[{name}] benzeri zaten atildi, atlandi: {item['title']}")
                continue
            try:
                send(item, name)
                print(f"[{name}] gonderildi: {item['title']}")
                state["recent_titles"].append(norm_title(item["title"]))
            except Exception as e:
                print(f"[{name}] gonderilemedi: {item['title']} ({e})")
            save_state(state)
            time.sleep(3)  # Telegram kanal hiz siniri icin
        save_state(state)


def main():
    if not DRY_RUN and (not TG_TOKEN or not TG_CHAT):
        sys.exit("TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID tanimli olmali.")
    if LOOP_SECONDS > 0:
        print(f"Surekli mod: her {LOOP_SECONDS} saniyede bir kontrol.")
        while True:
            try:
                run_once()
            except Exception as e:
                print(f"Hata: {e}")
            time.sleep(LOOP_SECONDS)
    else:
        run_once()


if __name__ == "__main__":
    main()
