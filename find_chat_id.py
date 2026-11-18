#!/usr/bin/env python3
"""
Lektion 2: Find dit chat-ID.
Spørger Telegram: "hvilke beskeder har min bot modtaget?" og viser hvem der skrev.
"""
import json
import urllib.request
from pathlib import Path


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


token = læs_hemmeligheder().get("TELEGRAM_TOKEN")
if not token:
    raise SystemExit("Mangler TELEGRAM_TOKEN i hemmeligheder.env")

# getUpdates = "giv mig de beskeder, botten har modtaget"
url = f"https://api.telegram.org/bot{token}/getUpdates"
svar = json.load(urllib.request.urlopen(url, timeout=20))

if not svar.get("result"):
    raise SystemExit("Ingen beskeder fundet. Skriv 'hej' til botten igen og kør scriptet en gang til.")

for opdatering in svar["result"]:
    besked = opdatering.get("message") or {}
    chat = besked.get("chat", {})
    print(f"Chat-ID: {chat.get('id')}  (fra {chat.get('first_name', '')}, besked: {besked.get('text')!r})")
