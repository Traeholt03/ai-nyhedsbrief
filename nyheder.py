#!/usr/bin/env python3
"""
AI-nyheder hver morgen.
Lektion 1: Hent de nyeste AI-artikler fra RSS-feeds.
Lektion 3: Send dem som én besked til Telegram.
Lektion 4: Lad Claude skrive et dansk overblik først, og send links bagefter.

Kør:  .venv/bin/python nyheder.py           (sender til Telegram)
      .venv/bin/python nyheder.py --test    (printer kun, sender ikke)
"""
import gzip
import html
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# --- 1. Kilderne: navn -> RSS-adresse ---------------------------------------
FEEDS = {
    # AI-labs: modeludgivelser og forskning direkte fra kilden
    "OpenAI": "https://openai.com/news/rss.xml",
    "Anthropic": "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",  # uofficielt spejl; Anthropic har intet RSS
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
    # Tekniske skribenter og nyhedsbreve
    "Simon Willison": "https://simonwillison.net/atom/everything/",
    "Latent Space": "https://www.latent.space/feed",
    "Interconnects": "https://www.interconnects.ai/feed",
    "Import AI": "https://importai.substack.com/feed",
    "Sebastian Raschka": "https://magazine.sebastianraschka.com/feed",
    # Hvad udviklere diskuterer. HN's egen søge-API (Algolia) i stedet for hnrss.org, der ofte gav 502.
    # Den giver JSON, ikke RSS, så den har sin egen funktion: artikler_fra_hn()
    "Hacker News": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=50",
}

# Kun HN-historier, hvis titel nævner AI. \b = ordgrænse, så "AI" ikke matcher inde i fx "MAIL"
HN_FILTER = re.compile(r"\bAI\b|\b(LLMs?|GPT|OpenAI|Anthropic|Claude|Gemini|DeepMind)\b", re.IGNORECASE)

TIMER_TILBAGE = 72      # tekniske kilder skriver sjældnere; sendt.json forhindrer gentagelser
CLAUDE_MODEL = "claude-opus-5"
MAX_PR_KILDE = 5        # så én kilde ikke fylder hele beskeden

# RSS og Atom bruger "navnerum" i XML. Atom-tags hedder fx {http://www.w3.org/2005/Atom}entry
ATOM = "{http://www.w3.org/2005/Atom}"


def hent(url, forsøg=3):
    """Download en side og returnér den rå tekst (bytes). Prøver igen ved midlertidige fejl."""
    # Nogle sider afviser forespørgsler uden en "User-Agent", så vi siger hvem vi er
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ai-nyheder"})
    for nr in range(1, forsøg + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as svar:
                data = svar.read()
            break
        except urllib.error.HTTPError as fejl:
            # 4xx (fx 404) er vores fejl og bliver ikke bedre af at prøve igen; 5xx (fx 502) er serverens
            if fejl.code < 500 or nr == forsøg:
                raise
            print(f"⚠️  {url} gav {fejl.code}, prøver igen ...")
        except OSError as fejl:  # timeout og netfejl
            if nr == forsøg:
                raise
            print(f"⚠️  {url} fejlede ({fejl}), prøver igen ...")
        time.sleep(5 * nr)  # 5, så 10 sek. pause

    # Nogle servere (fx DeepMind) gzip-komprimerer svaret uopfordret. Gzip starter altid med bytes 1f 8b.
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def læs_dato(tekst):
    """Feeds skriver datoer i to formater. Prøv begge; returnér None hvis det fejler."""
    if not tekst:
        return None
    tekst = tekst.strip()
    try:
        return parsedate_to_datetime(tekst)                         # RSS: "Sat, 04 Oct 2026 06:00:00 +0000"
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(tekst.replace("Z", "+00:00"))  # Atom: "2026-10-04T06:00:00Z"
    except ValueError:
        return None


def ren_tekst(tekst, max_tegn=400):
    """Beskrivelser i feeds indeholder ofte HTML. Fjern tags og forkort."""
    tekst = re.sub(r"<[^>]+>", " ", tekst or "")
    tekst = html.unescape(re.sub(r"\s+", " ", tekst)).strip()
    return tekst[:max_tegn]


def artikler_fra(navn, url):
    """Hent ét feed og lav det om til en liste af {titel, link, tid, kilde}."""
    rod = ET.fromstring(hent(url))
    resultat = []

    # RSS-format: <item><title>…</title><link>…</link><pubDate>…</pubDate></item>
    for item in rod.iter("item"):
        resultat.append({
            "kilde": navn,
            "titel": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "tid": læs_dato(item.findtext("pubDate")),
            "beskrivelse": ren_tekst(item.findtext("description")),
        })

    # Atom-format: <entry><title>…</title><link href="…"/><updated>…</updated></entry>
    for entry in rod.iter(ATOM + "entry"):
        link = entry.find(ATOM + "link")
        resultat.append({
            "kilde": navn,
            "titel": (entry.findtext(ATOM + "title") or "").strip(),
            "link": link.get("href", "") if link is not None else "",
            "tid": læs_dato(entry.findtext(ATOM + "published") or entry.findtext(ATOM + "updated")),
            "beskrivelse": ren_tekst(entry.findtext(ATOM + "summary") or entry.findtext(ATOM + "content")),
        })

    return resultat


def artikler_fra_hn(navn, url):
    """Hent HN-forsiden fra Algolia (JSON) og behold kun historier om AI."""
    resultat = []
    for hit in json.loads(hent(url))["hits"]:
        if not HN_FILTER.search(hit.get("title") or ""):
            continue
        # "Ask HN" o.l. har ingen ekstern url; så linker vi til selve HN-tråden
        link = hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}"
        resultat.append({
            "kilde": navn,
            "titel": hit["title"].strip(),
            "link": link,
            "tid": læs_dato(hit.get("created_at")),
            "beskrivelse": f"{hit.get('points', 0)} point og {hit.get('num_comments', 0)} kommentarer på Hacker News. "
                           + ren_tekst(hit.get("story_text")),
        })
    return resultat


SENDT_FIL = Path(__file__).parent / "sendt.json"


def læs_sendt():
    """Hent listen over links, du allerede har fået (tom liste første gang)."""
    try:
        return json.loads(SENDT_FIL.read_text())
    except (FileNotFoundError, ValueError):
        return []


def gem_sendt(nyheder):
    """Tilføj dagens links til hukommelsen. Behold de seneste 2000, så filen ikke vokser for evigt."""
    links = læs_sendt() + [a["link"] for a in nyheder]
    SENDT_FIL.write_text(json.dumps(links[-2000:], indent=0))


def saml_nyheder():
    """Gå alle kilder igennem, filtrér på tid, spring allerede sendte over, sortér nyeste først."""
    grænse = datetime.now(timezone.utc) - timedelta(hours=TIMER_TILBAGE)
    alle, set_links = [], set(læs_sendt())  # start med det, du allerede har set

    for navn, url in FEEDS.items():
        try:
            læser = artikler_fra_hn if "hn.algolia.com" in url else artikler_fra
            artikler = læser(navn, url)
        except Exception as fejl:
            # Én død kilde må ikke vælte hele morgenbriefen
            print(f"⚠️  {navn} fejlede: {fejl}")
            continue

        friske = [a for a in artikler if a["tid"] and a["tid"] >= grænse and a["link"] not in set_links]
        friske.sort(key=lambda a: a["tid"], reverse=True)
        for a in friske[:MAX_PR_KILDE]:
            set_links.add(a["link"])
            alle.append(a)

    alle.sort(key=lambda a: a["tid"], reverse=True)
    return alle


# --- Lektion 4: Claude skriver overblikket ----------------------------------

INSTRUKS = """Du skriver Daniels tekniske morgenbrief om AI. Daniel bygger selv med LLM'er \
(Claude API, agenter, automation) og vil forstå teknologien, ikke forretningsnyhederne.

Du får en nummereret liste over nye artikler. Skriv et kort teknisk overblik på dansk:
- Start med én sætning om den vigtigste tekniske udvikling.
- Derefter 3-6 korte afsnit, ét pr. emne. Slå artikler om samme emne sammen.
- Prioritér: nye modeller (navn, størrelse, context window, benchmarks, open weights/licens), \
nye metoder og forskningsresultater, værktøjer og biblioteker, og praktiske erfaringer med at bygge med AI.
- Nedprioritér: finansiering, opkøb, personsager og forbrugerprodukter, medmindre der er en teknisk pointe.
- Bevar engelske fagtermer (fx RAG, fine-tuning, inference, MoE). Forklar kort i en bisætning, \
første gang en mere niche term optræder.
- Skriv konkret: hvad er nyt, hvordan virker det, og hvorfor betyder det noget for den, der bygger.
- Højst 250 ord i alt. Almindelig tekst uden markdown, ingen links, ingen overskrifter.
- Hold dig til det, der står i artiklerne. Gæt ikke på tal eller detaljer."""


def skriv_overblik(nyheder):
    """Send artiklerne til Claude og få et dansk overblik tilbage (eller None ved fejl)."""
    nøgle = læs_hemmeligheder().get("ANTHROPIC_API_KEY")
    if not nøgle or not nyheder:
        return None

    # Importeres her, så scriptet stadig kan sende links, selv hvis pakken mangler
    import anthropic

    artikelliste = "\n\n".join(
        f"[{i}] {a['titel']} ({a['kilde']})\n{a['beskrivelse']}"
        for i, a in enumerate(nyheder, 1)
    )

    client = anthropic.Anthropic(api_key=nøgle)
    svar = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        system=INSTRUKS,
        messages=[{"role": "user", "content": artikelliste}],
        output_config={"effort": "medium"},  # hvor grundigt Claude tænker; medium er rigeligt til et resumé
        # Hvis Claudes sikkerhedsfiltre afviser forespørgslen, prøver Anthropic automatisk en anden model
        extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
        extra_body={"fallbacks": "default"},
    )

    if svar.stop_reason == "refusal":
        return None
    # Svaret er en liste af "blokke" (tænkning, tekst, ...). Vi vil kun have teksten.
    tekst = "".join(blok.text for blok in svar.content if blok.type == "text").strip()
    return tekst or None


# --- Lektion 3: Telegram ---------------------------------------------------

def læs_hemmeligheder():
    """Læs NØGLE=værdi-linjer fra hemmeligheder.env (ignorér tomme linjer og #-kommentarer)."""
    fil = Path(__file__).parent / "hemmeligheder.env"
    værdier = {}
    for linje in fil.read_text().splitlines():
        linje = linje.strip()
        if linje and not linje.startswith("#") and "=" in linje:
            nøgle, værdi = linje.split("=", 1)
            værdier[nøgle.strip()] = værdi.strip()
    return værdier


def lav_overblik_besked(overblik):
    """Første besked: Claudes overblik. html.escape, fordi teksten er fri tekst fra en AI."""
    dato = datetime.now().strftime("%d/%m")
    return f"🤖 <b>AI-overblik {dato}</b>\n\n{html.escape(overblik)}"


def lav_besked(nyheder):
    """Anden besked: alle links som Telegram-HTML, grupperet efter kilde."""
    dato = datetime.now().strftime("%d/%m")
    if not nyheder:
        return f"🤖 <b>AI-nyheder {dato}</b>\n\nIngen nye AI-artikler siden sidst."

    linjer = [f"🔗 <b>Links {dato}</b> ({len(nyheder)} artikler)"]
    for kilde in FEEDS:  # samme rækkefølge som listen øverst
        fra_kilde = [a for a in nyheder if a["kilde"] == kilde]
        if not fra_kilde:
            continue
        linjer.append(f"\n<b>{html.escape(kilde)}</b>")
        for a in fra_kilde:
            # html.escape: titler med fx "<" eller "&" ville ellers ødelægge formateringen
            linjer.append(f'• <a href="{html.escape(a["link"], quote=True)}">{html.escape(a["titel"])}</a>')
    return "\n".join(linjer)


def del_op(tekst, max_tegn=4000):
    """Telegram tillader max 4096 tegn pr. besked. Del ved linjeskift, så links ikke knækker."""
    dele, nu = [], ""
    for linje in tekst.split("\n"):
        if len(nu) + len(linje) + 1 > max_tegn:
            dele.append(nu)
            nu = ""
        nu += linje + "\n"
    dele.append(nu)
    return dele


def vent_på_net(vært="api.telegram.org", max_sekunder=300):
    """Vent til DNS virker. Mac'en er ofte ikke på nettet lige efter dvale."""
    start = time.monotonic()
    while True:
        try:
            socket.getaddrinfo(vært, 443)
            return
        except socket.gaierror:
            if time.monotonic() - start > max_sekunder:
                raise RuntimeError(f"Intet net efter {max_sekunder} sek.")
            print("⏳ Venter på net ...")
            time.sleep(10)


def post_med_forsøg(url, data, forsøg=3):
    """POST med op til 3 forsøg og stigende pause (5, 10 sek.) imellem."""
    for nr in range(1, forsøg + 1):
        try:
            return json.load(urllib.request.urlopen(url, data=data, timeout=20))
        except OSError as fejl:  # URLError og timeouts er begge OSError
            if nr == forsøg:
                raise
            print(f"⚠️  Telegram-forsøg {nr} fejlede ({fejl}), prøver igen ...")
            time.sleep(5 * nr)


def send_telegram(tekst):
    """POST beskeden til Telegrams sendMessage-endpoint."""
    h = læs_hemmeligheder()
    url = f"https://api.telegram.org/bot{h['TELEGRAM_TOKEN']}/sendMessage"
    for del_tekst in del_op(tekst):
        data = urllib.parse.urlencode({
            "chat_id": h["TELEGRAM_CHAT_ID"],
            "text": del_tekst,
            "parse_mode": "HTML",                 # så <b> og <a href> virker
            "disable_web_page_preview": "true",   # ellers fylder link-forhåndsvisning hele skærmen
        }).encode()
        svar = post_med_forsøg(url, data)
        if not svar.get("ok"):
            raise RuntimeError(f"Telegram afviste beskeden: {svar}")


if __name__ == "__main__":
    # Tidsstempel øverst i begge logfiler, så man kan se hvilken morgen en linje hører til
    overskrift = f"\n=== Kørsel {datetime.now():%Y-%m-%d %H:%M} ==="
    print(overskrift)
    print(overskrift, file=sys.stderr)
    vent_på_net()
    nyheder = saml_nyheder()

    try:
        overblik = skriv_overblik(nyheder)
    except Exception as fejl:
        # Fejler Claude, sender vi stadig linkene: hellere en halv brief end ingen
        print(f"⚠️  Overblik fejlede: {fejl}")
        overblik = None

    beskeder = []
    if overblik:
        beskeder.append(lav_overblik_besked(overblik))
    beskeder.append(lav_besked(nyheder))

    for besked in beskeder:
        if "--test" in sys.argv:
            print(besked, end="\n\n" + "-" * 40 + "\n\n")
        else:
            send_telegram(besked)
    if "--test" not in sys.argv:
        gem_sendt(nyheder)  # først når alt er sendt, ellers mister du artikler ved en fejl
        print(f"✅ Sendt {len(beskeder)} beskeder til Telegram")
