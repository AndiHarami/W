"""
Smoke-Test v3: Playwright. Echter Browser im Hintergrund.
F5/Shape löst die Challenge automatisch wenn JS ausgeführt wird.

Test 1: Produktseite-HTML laden, data-max suchen
Test 2: Filial-API aus dem Browser-Kontext heraus aufrufen
        (nutzt automatisch die Session-Cookies vom Browser)
"""
import json
from playwright.sync_api import sync_playwright


PRODUCT_URL = (
    "https://www.rossmann.de/de/baby-und-spielzeug-amigo-pokemon-booster-nr-1/"
    "p/0820650250170"
)
DAN = "516372"
SEARCH = "offenbach"


def line():
    print("=" * 60)


with sync_playwright() as p:
    print("Starte headless Chromium...")
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/148.0.0.0 Safari/537.36"
        ),
        locale="de-DE",
        viewport={"width": 1440, "height": 900},
    )
    page = context.new_page()

    print(f"Lade Produktseite (kann 10-20 Sek dauern wg. Challenge)...")
    try:
        page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=45000)
        print("  → DOM geladen")
        # warte bis alle Netzwerk-Aktivität abgeklungen ist
        page.wait_for_load_state("networkidle", timeout=20000)
        print("  → Netzwerk-Idle erreicht")
    except Exception as e:
        print(f"  ⚠️ Timeout/Fehler beim Laden: {e}")
        # trotzdem weiter probieren

    print()
    line()
    print("TEST 1: Produktseite-HTML & Online-Stock")
    line()

    html = page.content()
    print(f"HTML-Größe: {len(html)} Zeichen")

    if "Client Challenge" in html or len(html) < 5000:
        print("❌ Challenge nicht gelöst (oder Page zu klein)")
        page.screenshot(path="debug_page.png", full_page=False)
        print("→ Screenshot in debug_page.png")
    else:
        all_max_inputs = page.locator("input[data-max]").all()
        print(f"Gefundene 'input[data-max]' Elemente: {len(all_max_inputs)}")

        if all_max_inputs:
            print(f"\n✅ HTML enthält data-max!")
            print("Erste 8 Werte (Hauptprodukt sollte ganz oben sein):")
            for i, inp in enumerate(all_max_inputs[:8]):
                val = inp.get_attribute("data-max")
                # Versuche herauszufinden ob's das Hauptprodukt ist
                form = inp.locator("xpath=ancestor::form[1]")
                form_id = form.get_attribute("id") if form.count() else None
                ctx = "(Hauptprodukt?)" if form_id == "addToCartForm" else ""
                print(f"  [{i + 1}] data-max={val} {ctx}")
        else:
            print("⚠️ Kein data-max gefunden – Produkt evtl. ausverkauft oder Selektor anders?")

        # Suche nach Ausverkauft-Markern
        lower = html.lower()
        sold_out = sum(lower.count(m) for m in ["ausverkauft", "nicht verfügbar"])
        in_stock = sum(lower.count(m) for m in ["in den warenkorb", "addtocart"])
        print(f"\nText-Indikatoren: 'ausverkauft' x{sold_out}, 'in den warenkorb' x{in_stock}")

        # Speichere HTML für Debug
        with open("debug_v3.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("→ Volle HTML in debug_v3.html gespeichert")

    print()
    line()
    print("TEST 2: Filial-API via Browser-fetch")
    line()

    api_url = f"https://www.rossmann.de/storefinder/.rest/store?dan={DAN}&q={SEARCH}"

    js = """
    async (url) => {
        const r = await fetch(url, {
            headers: {
                'Accept': 'application/json, text/javascript, */*; q=0.01',
                'X-Requested-With': 'XMLHttpRequest'
            }
        });
        return { status: r.status, text: await r.text() };
    }
    """

    try:
        result = page.evaluate(js, api_url)
        print(f"HTTP-Status: {result['status']}")
        response_text = result["text"]
        print(f"Response-Größe: {len(response_text)} Zeichen")

        if result["status"] == 200:
            try:
                data = json.loads(response_text)
                stores = data.get("store", [])
                print(f"\n✅ ERFOLG – {len(stores)} Filialen:")
                for s in stores:
                    stock = s["productInfo"][0]["stock"]
                    marker = "🟢" if int(stock) > 0 else "⚪"
                    pickup = "[Abholstation]" if s.get("pickupStation") else ""
                    print(f"  {marker} {s['street']}, {s['postcode']} {s['city']}: "
                          f"stock={stock} {pickup}")
            except json.JSONDecodeError as e:
                print(f"❌ JSON-Parse-Fehler: {e}")
                print(f"Erste 300 Zeichen: {response_text[:300]}")
        else:
            print(f"❌ Unerwarteter Status")
            print(f"Erste 300 Zeichen: {response_text[:300]}")
    except Exception as e:
        print(f"❌ EXCEPTION: {type(e).__name__}: {e}")

    browser.close()


print()
line()
print("FERTIG – schick die komplette Ausgabe an Claude")
line()
