"""
Smoke-Test v2: Mit curl-cffi statt requests.
Imitiert echte Chrome-TLS-Fingerprints – umgeht oft Bot-Detection.

Test 1: Filial-API
Test 2: Produktseite-HTML
"""
from curl_cffi import requests as cffi_requests
from bs4 import BeautifulSoup


def line(char="="):
    print(char * 60)


# Session, damit Cookies zwischen Calls erhalten bleiben
session = cffi_requests.Session(impersonate="chrome")

# Erstmal die Homepage besuchen — das setzt Session-Cookies + löst evtl. Challenge
print("Vorbereitung: Homepage besuchen für Session-Cookies...")
try:
    warmup = session.get("https://www.rossmann.de/de/", timeout=15)
    print(f"  Status: {warmup.status_code}, Größe: {len(warmup.text)} Zeichen")
    print(f"  Cookies erhalten: {len(session.cookies)}")
except Exception as e:
    print(f"  ❌ Warmup-Fehler: {e}")

print()
# ---------------------------------------------------------------
# TEST 1: Filial-API
# ---------------------------------------------------------------
line()
print("TEST 1: Filial-API")
line()

url = "https://www.rossmann.de/storefinder/.rest/store"
params = {"dan": "516372", "q": "offenbach"}

extra_headers = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.rossmann.de/de/baby-und-spielzeug-amigo-pokemon-booster-nr-1/p/0820650250170",
}

try:
    r = session.get(url, params=params, headers=extra_headers, timeout=15)
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
        print(f"\n❌ FEHLER – noch immer geblockt")
        print(f"Erste 300 Zeichen: {r.text[:300]}")
except Exception as e:
    print(f"\n❌ EXCEPTION: {type(e).__name__}: {e}")


# ---------------------------------------------------------------
# TEST 2: Produktseite-HTML
# ---------------------------------------------------------------
print()
line()
print("TEST 2: Produktseite-HTML")
line()

product_url = (
    "https://www.rossmann.de/de/baby-und-spielzeug-amigo-pokemon-booster-nr-1/"
    "p/0820650250170"
)

try:
    r = session.get(product_url, timeout=20)
    print(f"HTTP-Status: {r.status_code}")
    print(f"Content-Type: {r.headers.get('Content-Type', '?')}")
    print(f"Response-Größe: {len(r.text)} Zeichen")

    text = r.text
    text_lower = text.lower()

    if "client challenge" in text_lower or "_fs-ch-" in text:
        print("\n❌ Immer noch Challenge-Page")
        with open("debug_v2.html", "w", encoding="utf-8") as f:
            f.write(text)
        print("→ siehe debug_v2.html")
    elif r.status_code != 200:
        print(f"\n❌ Status {r.status_code}")
    else:
        soup = BeautifulSoup(text, "html.parser")
        amount_inputs = soup.find_all("input", attrs={"data-max": True})
        sold_out_count = sum(text_lower.count(m) for m in [
            "ausverkauft", "nicht verfügbar", "nicht mehr lieferbar"
        ])
        in_stock_count = sum(text_lower.count(m) for m in [
            "in den warenkorb", "addtocart"
        ])

        print(f"\nIndikatoren in der HTML:")
        print(f"  data-max Attribute: {len(amount_inputs)}")
        print(f"  'ausverkauft' Treffer: {sold_out_count}")
        print(f"  'in den warenkorb' Treffer: {in_stock_count}")

        if amount_inputs:
            print(f"\n✅ HTML enthält data-max — wir können parsen!")
            print("Erste 5 data-max Werte:")
            for i, inp in enumerate(amount_inputs[:5]):
                form = inp.find_parent("form")
                ctx = "Hauptprodukt" if form and form.get("id") == "addToCartForm" else "Ähnliches Produkt"
                print(f"  [{i + 1}] data-max={inp.get('data-max')}  ({ctx})")
        else:
            print("\n⚠️ Echte HTML, aber kein data-max gefunden — bitte debug.html prüfen")
            with open("debug_v2.html", "w", encoding="utf-8") as f:
                f.write(text)

except Exception as e:
    print(f"\n❌ EXCEPTION: {type(e).__name__}: {e}")


print()
line()
print("FERTIG – schick die Ausgabe an Claude")
line()
