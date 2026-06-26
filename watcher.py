"""
Rossmann Pokemon Watcher

Checkt regelmäßig eine Liste von Rossmann-Produkten auf:
  - Online-Verfügbarkeit (schema.org availability + Stock)
  - Filial-Verfügbarkeit (storefinder API, mehrere PLZ/Städte)

Bei Neu-Verfügbarkeit oder Stock-Steigerung wird eine Discord-Nachricht
mit Produktbild, Preis und Filialliste gesendet.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import Page, sync_playwright


# =============================================================
# KONFIGURATION
# =============================================================

PRODUCTS = [
    {
        "name": "Amigo Pokemon Booster Nr. 1",
        "url": "https://www.rossmann.de/de/baby-und-spielzeug-amigo-pokemon-booster-nr-1/p/0820650250170",
        "dan": "516372",
    },
    {
        "name": "Ideenwelt Amigo Pokemon TCG Mini-Tin",
        "url": "https://www.rossmann.de/de/ideenwelt-amigo-pokemon-tcg-mini-tin/p/4007396203073",
        "dan": "084175",
    },
    {
        "name": "Amigo Boosterpack Karmesin & Purpur – Ewige Rivalen",
        "url": "https://www.rossmann.de/de/baby-und-spielzeug-amigo-pokemon-boosterpack-karmesin-und-purpur---ewige-rivalen/p/0196214110779",
        "dan": None,  # Produkt nur online, kein Filial-Check möglich
    },
]

LOCATIONS = ["offenbach", "dreieich", "rödermark"]

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

STATE_FILE = Path(__file__).parent / "state.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/148.0.0.0 Safari/537.36"
)

# Nur Output, kein Discord-Senden – fürs Debugging
DRY_RUN = "--dry-run" in sys.argv


# =============================================================
# HELPER
# =============================================================

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("⚠️ state.json ist kaputt, starte mit leerem Zustand")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# =============================================================
# PAGE CHECKS
# =============================================================

def check_online(page: Page, url: str) -> dict:
    """Lädt Produktseite, liest Online-Verfügbarkeit aus schema.org Markup."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    # Warte auf das schema.org meta-Tag im DOM (state="attached" weil meta nie "visible")
    try:
        page.wait_for_selector(
            'meta[itemprop="availability"]',
            state="attached",
            timeout=10000,
        )
    except Exception:
        log("    ⚠️ Schema.org meta-Tag nicht gefunden in 10s")

    # Schema.org availability
    avail_meta = page.locator('meta[itemprop="availability"]').first
    availability = (
        avail_meta.get_attribute("content")
        if avail_meta.count() > 0
        else None
    )
    is_available = bool(availability and "InStock" in availability)

    # Preis
    price_meta = page.locator('meta[itemprop="price"]').first
    price = price_meta.get_attribute("content") if price_meta.count() > 0 else None

    # Bild (für Discord-Embed)
    img_meta = page.locator('meta[property="og:image"]').first
    image_url = img_meta.get_attribute("content") if img_meta.count() > 0 else None

    # Echter Produktname (statt aus unserer Config)
    title_meta = page.locator('meta[property="og:title"]').first
    real_name = title_meta.get_attribute("content") if title_meta.count() > 0 else None
    # "X online kaufen | rossmann.de" -> "X"
    if real_name and "|" in real_name:
        real_name = real_name.split("|")[0].strip()
    if real_name and real_name.endswith(" online kaufen"):
        real_name = real_name[: -len(" online kaufen")].strip()

    # Online-Stock: data-max am Hauptprodukt-Mengen-Input
    # (nicht in einem .rm-tile-product = Ähnliche-Produkte-Karussell)
    stock = None
    if is_available:
        stock_js = """
        () => {
            const inputs = document.querySelectorAll('input[data-max]');
            for (const inp of inputs) {
                if (!inp.closest('.rm-tile-product')) {
                    const v = parseInt(inp.dataset.max, 10);
                    if (!isNaN(v)) return v;
                }
            }
            return null;
        }
        """
        try:
            stock = page.evaluate(stock_js)
        except Exception:
            pass
    elif availability and ("SoldOut" in availability or "OutOfStock" in availability):
        stock = 0

    return {
        "available": is_available,
        "stock": stock,
        "price": price,
        "image_url": image_url,
        "name": real_name,
        "raw_availability": availability,
    }


def check_stores(page: Page, dan: str, location: str) -> list:
    """Fragt die Filial-API ab. Gibt nur Filialen mit stock > 0 zurück."""
    api_url = f"https://www.rossmann.de/storefinder/.rest/store?dan={dan}&q={location}"
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
    result = page.evaluate(js, api_url)
    if result["status"] != 200:
        log(f"    ⚠️ Filial-API Status {result['status']} für q={location}")
        return []

    try:
        data = json.loads(result["text"])
    except json.JSONDecodeError:
        log(f"    ⚠️ Filial-API: keine valide JSON-Response")
        return []

    out = []
    for s in data.get("store", []):
        for info in s.get("productInfo", []):
            stock = int(info.get("stock", 0))
            if stock > 0:
                out.append({
                    "id": s["id"],
                    "name": f"{s['street']}, {s['postcode']} {s['city']}",
                    "stock": stock,
                    "is_pickup": s.get("pickupStation", False),
                })
                break
    return out


# =============================================================
# DIFF
# =============================================================

def diff_product(old: dict, new: dict) -> list:
    """
    Vergleicht alten und neuen Zustand für ein Produkt.
    Returns Liste von Event-Tuples: ('online_new', payload) etc.
    """
    events = []

    if not old:
        # Erste Erfassung – nur benachrichtigen wenn JETZT verfügbar
        if new["online"]["available"]:
            events.append(("online_new", new["online"]))
        if new["stores"]:
            events.append(("store_initial", new["stores"]))
        return events

    # ---- Online ----
    old_on = old.get("online", {})
    new_on = new["online"]
    old_avail = old_on.get("available", False)
    new_avail = new_on["available"]
    old_stock = old_on.get("stock") or 0
    new_stock = new_on["stock"] or 0

    if not old_avail and new_avail:
        events.append(("online_new", new_on))
    elif old_avail and new_avail and new_stock > old_stock > 0:
        events.append(("online_increase", {"old": old_stock, "new": new_stock, "info": new_on}))

    # ---- Filialen ----
    old_stores = {s["id"]: s for s in old.get("stores", [])}
    new_stores = {s["id"]: s for s in new["stores"]}

    new_avail_stores = [s for sid, s in new_stores.items() if sid not in old_stores]
    increased_stores = [
        {"old": old_stores[sid]["stock"], "new": s["stock"], "store": s}
        for sid, s in new_stores.items()
        if sid in old_stores and s["stock"] > old_stores[sid]["stock"]
    ]

    if new_avail_stores:
        events.append(("store_new", new_avail_stores))
    if increased_stores:
        events.append(("store_increase", increased_stores))

    return events


# =============================================================
# DISCORD
# =============================================================

def build_embed(product_name: str, product_url: str, online: dict,
                stores: list, events: list) -> dict:
    """Baut Discord-Embed basierend auf den ausgelösten Events."""
    event_types = [e[0] for e in events]

    if "online_new" in event_types:
        title = f"🎉 {product_name} – JETZT ONLINE VERFÜGBAR"
        color = 0x00C853  # grün
    elif "store_new" in event_types or "store_initial" in event_types:
        title = f"🏪 {product_name} – jetzt in Filiale verfügbar"
        color = 0x00B0FF  # blau
    elif "online_increase" in event_types:
        old = next(e[1] for e in events if e[0] == "online_increase")
        title = f"📈 {product_name} – Online-Stock: {old['old']} → {old['new']}"
        color = 0xFFA000  # orange
    elif "store_increase" in event_types:
        title = f"📈 {product_name} – Filial-Stock gestiegen"
        color = 0xFFA000
    else:
        title = product_name
        color = 0x9E9E9E

    fields = []

    # Online-Status
    if online["available"]:
        price_str = f" – {str(online['price']).replace('.', ',')} €" if online["price"] else ""
        stock_str = f"{online['stock']} Stück" if online["stock"] else "verfügbar"
        fields.append({
            "name": "🟢 Online",
            "value": f"{stock_str}{price_str}",
            "inline": True,
        })
    else:
        fields.append({
            "name": "⚪ Online",
            "value": "ausverkauft",
            "inline": True,
        })

    # Filialen
    if stores:
        # nach Stockzahl sortieren absteigend, max 10
        sorted_stores = sorted(stores, key=lambda s: -s["stock"])[:10]
        lines = []
        for s in sorted_stores:
            pickup = " 📦" if s["is_pickup"] else ""
            lines.append(f"• {s['name']} – **{s['stock']} Stück**{pickup}")
        suffix = "" if len(stores) <= 10 else f"\n_… und {len(stores) - 10} weitere_"
        fields.append({
            "name": f"🏪 Filialen verfügbar ({len(stores)})",
            "value": "\n".join(lines) + suffix,
            "inline": False,
        })

    embed = {
        "title": title[:256],  # Discord limit
        "url": product_url,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Rossmann Pokemon Watcher"},
    }
    if online.get("image_url"):
        embed["thumbnail"] = {"url": online["image_url"]}
    return embed


def send_discord(embed: dict) -> None:
    if DRY_RUN:
        log("  📨 [DRY-RUN] Würde Discord-Nachricht senden:")
        print(json.dumps({"embeds": [embed]}, indent=2, ensure_ascii=False))
        return

    try:
        r = requests.post(
            DISCORD_WEBHOOK,
            json={"embeds": [embed]},
            timeout=10,
        )
        if r.ok:
            log(f"  📨 Discord: HTTP {r.status_code}")
        else:
            log(f"  ❌ Discord HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"  ❌ Discord: {type(e).__name__}: {e}")


# =============================================================
# MAIN
# =============================================================

def main() -> int:
    if not DISCORD_WEBHOOK and not DRY_RUN:
        log("❌ DISCORD_WEBHOOK_URL Umgebungsvariable ist leer.")
        log("   Lokal: export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'")
        log("   Oder zum Testen ohne Senden:  python3 watcher.py --dry-run")
        return 1

    log(f"🚀 Rossmann Watcher startet ({len(PRODUCTS)} Produkte, Orte: {LOCATIONS})")
    if DRY_RUN:
        log("   (DRY-RUN-Modus – Discord-Nachrichten werden nur ausgegeben)")

    old_state = load_state()
    new_state = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=UA,
            locale="de-DE",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()

        for product in PRODUCTS:
            log(f"\n→ {product['name']}")
            try:
                online = check_online(page, product["url"])

                stores_all = []
                if product.get("dan"):
                    for loc in LOCATIONS:
                        stores_all.extend(check_stores(page, product["dan"], loc))
                    # Deduplizieren: gleiche Filiale aus mehreren Searches
                    deduped = {}
                    for s in stores_all:
                        existing = deduped.get(s["id"])
                        if not existing or s["stock"] > existing["stock"]:
                            deduped[s["id"]] = s
                    stores_all = list(deduped.values())

                product_state = {"online": online, "stores": stores_all}

                # Status loggen
                if online["available"]:
                    stock = online["stock"] or "?"
                    log(f"  online: ✅ {stock} Stück")
                else:
                    log(f"  online: ❌ {online.get('raw_availability', '?')}")
                log(f"  filialen mit stock: {len(stores_all)}")

                # Diff & Notification
                old_product_state = old_state.get(product["url"])
                events = diff_product(old_product_state, product_state)

                if events:
                    log(f"  📡 Events: {[e[0] for e in events]}")
                    embed = build_embed(
                        online.get("name") or product["name"],
                        product["url"],
                        online,
                        stores_all,
                        events,
                    )
                    send_discord(embed)
                else:
                    log("  (keine Änderung)")

                new_state[product["url"]] = product_state

            except Exception as e:
                log(f"  ❌ FEHLER: {type(e).__name__}: {e}")
                # alten State behalten falls vorhanden – verhindert false positives
                if old_state.get(product["url"]):
                    new_state[product["url"]] = old_state[product["url"]]

        browser.close()

    save_state(new_state)
    log("\n✅ Fertig.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
