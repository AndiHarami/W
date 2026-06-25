"""
Smoke-Test: Kommt unser Python-Skript an Rossmanns API + Produktseiten ran?

Test 1: Filial-API (store?dan=...&q=...)  -> JSON mit Filialen erwartet
Test 2: Produktseite-HTML (für Online-Stock via data-max)  -> HTML erwartet
"""
import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

API_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.rossmann.de/",
}

PAGE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.7",
    "Upgrade-Insecure-Requests": "1",
}


def line(char="="):
    print(char * 60)


# ---------------------------------------------------------------
# TEST 1: Filial-API
# ---------------------------------------------------------------
line()
print("TEST 1: Filial-API")
line()

url = "https://www.rossmann.de/storefinder/.rest/store"
params = {"dan": "516372", "q": "offenbach"}

try:
    r = requests.get(url, params=params, headers=API_HEADERS, timeout=10)
    print(f"HTTP-Status: {r.status_code}")
    print(f"Content-Type: {r.headers.get('Content-Type', '?')}")
    print(f"Response-Größe: {len(r.text)} Zeichen")

    if r.status_code == 200 and "application/json" in r.headers.get("Content-Type", ""):
        data = r.json()
        stores = data.get("store", [])
        print(f"\n✅ ERFOLG – {len(stores)} Filialen zurück:")
        for s in stores:
            stock = s["productInfo"][0]["stock"]
            marker = "🟢" if int(stock) > 0 else "⚪"
            pickup = "[Abholstation]" if s.get("pickupStation") else ""
            print(f"  {marker} {s['street']}, {s['postcode']} {s['city']}: stock={stock} {pickup}")
    else:
        print(f"\n❌ FEHLER – unerwartete Antwort")
        print(f"Erste 300 Zeichen: {r.text[:300]}")
except Exception as e:
    print(f"\n❌ EXCEPTION: {type(e).__name__}: {e}")


# ---------------------------------------------------------------
# TEST 2: Produktseite-HTML
# ---------------------------------------------------------------
print()
line()
print("TEST 2: Produktseite-HTML (für Online-Stock)")
line()

product_url = (
    "https://www.rossmann.de/de/baby-und-spielzeug-amigo-pokemon-booster-nr-1/"
    "p/0820650250170"
)

try:
    r = requests.get(product_url, headers=PAGE_HEADERS, timeout=15)
    print(f"HTTP-Status: {r.status_code}")
    print(f"Content-Type: {r.headers.get('Content-Type', '?')}")
    print(f"Response-Größe: {len(r.text)} Zeichen")

    text = r.text
    text_lower = text.lower()

    cloudflare_block = any(m in text_lower for m in [
        "cf-chl", "challenge-platform", "checking your browser",
        "just a moment", "attention required"
    ])

    if cloudflare_block:
        print("\n❌ CLOUDFLARE-BLOCK ERKANNT")
        print("Plan-B nötig: Playwright statt requests")
    elif r.status_code != 200:
        print(f"\n❌ Unerwarteter Status {r.status_code}")
        print(f"Erste 300 Zeichen: {text[:300]}")
    else:
        soup = BeautifulSoup(text, "html.parser")

        amount_inputs = soup.find_all("input", attrs={"data-max": True})
        sold_out_markers = sum(text_lower.count(m) for m in [
            "ausverkauft", "nicht verfügbar", "nicht mehr lieferbar"
        ])
        in_stock_markers = sum(text_lower.count(m) for m in [
            "in den warenkorb", "addtocart"
        ])

        print(f"\nIndikatoren in der HTML:")
        print(f"  data-max Attribute: {len(amount_inputs)}")
        print(f"  'ausverkauft'/'nicht verfügbar' Treffer: {sold_out_markers}")
        print(f"  'in den warenkorb'/'addtocart' Treffer: {in_stock_markers}")

        if amount_inputs:
            print(f"\n✅ HTML enthält data-max — Online-Stock parsebar!")
            print("Erste 5 data-max Werte (Hauptprodukt + Ähnliche):")
            for i, inp in enumerate(amount_inputs[:5]):
                form_id = inp.find_parent("form")
                ctx = "Hauptprodukt?" if form_id and form_id.get("id") == "addToCartForm" else "weiteres"
                print(f"  [{i + 1}] data-max={inp.get('data-max')}  ({ctx})")
        else:
            print("\n⚠️ Kein data-max gefunden — HTML wird gespeichert für Analyse")
            with open("debug_product_page.html", "w", encoding="utf-8") as f:
                f.write(text)
            print("   → siehe debug_product_page.html")

except Exception as e:
    print(f"\n❌ EXCEPTION: {type(e).__name__}: {e}")


print()
line()
print("FERTIG – schick die komplette Ausgabe an Claude")
line()
