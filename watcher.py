"""watcher"""
import hashlib
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

PRODUCTS = json.loads(os.environ.get("PRODUCTS_JSON", "[]"))
LOCATIONS = json.loads(os.environ.get("LOCATIONS_JSON", "[]"))

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


def _key(url: str) -> str:
    """Kurzer Hash-Schlüssel damit state.json keine Klartext-URLs enthält."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def _strip_transient(product_state: dict) -> dict:
    """Entfernt Klartext-Felder aus dem online-Block bevor state gespeichert wird."""
    online = {
        k: v for k, v in product_state["online"].items()
        if k not in ("name", "image_url")
    }
    return {"online": online, "stores": product_state["stores"]}


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


def _truncate_lines(lines: list, max_chars: int) -> str:
    """Discord-Embed-Field-Value max 1024 Zeichen. Trunkiert sauber."""
    out = []
    total = 0
    for i, line in enumerate(lines):
        # Reserve Platz für "_… und X weitere_"
        suffix = f"\n_… und {len(lines) - i} weitere_"
        if total + len(line) + 1 + len(suffix) > max_chars:
            out.append(suffix.lstrip("\n"))
            break
        out.append(line)
        total += len(line) + 1
    return "\n".join(out)


def _parse_stock(raw) -> tuple[int, bool]:
    """Parse stock string like '5+' or '0'. Returns (int, is_plus)."""
    s = str(raw or "0").strip()
    is_plus = s.endswith("+")
    digits = s.rstrip("+")
    return (int(digits) if digits.isdigit() else 0, is_plus)


def check_stores(page: Page, dan: str, location: str) -> list:
    """Query store API. Returns list of stores with stock info."""
    tmpl = os.environ.get("STORE_API_URL_TEMPLATE", "")
    api_url = tmpl.format(dan=dan, location=location)
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
        log(f"  api {result['status']}")
        return []

    try:
        data = json.loads(result["text"])
    except json.JSONDecodeError:
        log(f"  api json err")
        return []

    out = []
    for s in data.get("store", []):
        # nimm immer den ersten productInfo-Eintrag (es gibt eigentlich nur einen)
        info = (s.get("productInfo") or [{}])[0]
        stock, is_plus = _parse_stock(info.get("stock"))
        out.append({
            "id": s["id"],
            "name": f"{s['street']}, {s['postcode']} {s['city']}",
            "stock": stock,
            "stock_plus": is_plus,
            "is_pickup": s.get("pickupStation", False),
        })
    return out


# =============================================================
# DIFF
# =============================================================

def diff_product(old: dict, new: dict) -> list:
    """
    Vergleicht alten und neuen Zustand. Notification feuert NUR bei:
      - Online: ausverkauft -> verfügbar
      - Online: Stock-Anstieg
      - Filiale: ausverkauft/nicht-getrackt -> verfügbar
      - Filiale: Stock-Anstieg bei bereits verfügbarer Filiale
    NICHT bei "verschwunden" oder neuer Filiale im Radius ohne Stock.
    """
    events = []
    new_available_stores = [s for s in new["stores"] if s["stock"] > 0]

    if not old:
        # Erste Erfassung – nur benachrichtigen wenn JETZT verfügbar
        if new["online"]["available"]:
            events.append(("online_new", new["online"]))
        if new_available_stores:
            events.append(("store_initial", new_available_stores))
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

    newly_available = []
    increased = []
    for s in new["stores"]:
        if s["stock"] <= 0:
            continue
        old_s = old_stores.get(s["id"])
        old_st = (old_s or {}).get("stock", 0)
        if old_st <= 0:
            newly_available.append(s)
        elif s["stock"] > old_st:
            increased.append({"old": old_st, "new": s["stock"], "store": s})

    if newly_available:
        events.append(("store_new", newly_available))
    if increased:
        events.append(("store_increase", increased))

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

    # Filialen – ALLE zeigen, getrennt nach verfügbar/ausverkauft
    if stores:
        available = sorted([s for s in stores if s["stock"] > 0], key=lambda s: -s["stock"])
        sold_out = [s for s in stores if s["stock"] <= 0]

        if available:
            lines = []
            for s in available:
                pickup = " 📦" if s["is_pickup"] else ""
                stock_label = f"{s['stock']}+" if s.get("stock_plus") else str(s["stock"])
                lines.append(f"🟢 {s['name']} – **{stock_label} Stück**{pickup}")
            fields.append({
                "name": f"Verfügbar ({len(available)})",
                "value": _truncate_lines(lines, 1024),
                "inline": False,
            })

        if sold_out:
            lines = [f"⚪ {s['name']}" for s in sold_out]
            fields.append({
                "name": f"Ausverkauft ({len(sold_out)})",
                "value": _truncate_lines(lines, 1024),
                "inline": False,
            })

    embed = {
        "title": title[:256],  # Discord limit
        "url": product_url,
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "watcher"},
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
        return 1
    if not PRODUCTS:
        log("❌ PRODUCTS_JSON Umgebungsvariable fehlt oder leer.")
        return 1
    if not LOCATIONS:
        log("❌ LOCATIONS_JSON Umgebungsvariable fehlt oder leer.")
        return 1

    log(f"start ({len(PRODUCTS)} items)")
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

        for idx, product in enumerate(PRODUCTS):
            log(f"item {idx}")
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

                # Status
                on_ok = 1 if online["available"] else 0
                log(f"  on={on_ok} stores={len(stores_all)}")

                # Diff & Notification (Hash-Schlüssel statt URL)
                pk = _key(product["url"])
                old_product_state = old_state.get(pk)
                events = diff_product(old_product_state, product_state)

                if events:
                    log(f"  ev {len(events)}")
                    embed = build_embed(
                        online.get("name") or product["name"],
                        product["url"],
                        online,
                        stores_all,
                        events,
                    )
                    send_discord(embed)
                else:
                    log("  ok")

                new_state[pk] = _strip_transient(product_state)

            except Exception as e:
                log(f"  err {type(e).__name__}")
                # alten State behalten falls vorhanden – verhindert false positives
                pk = _key(product["url"])
                if old_state.get(pk):
                    new_state[pk] = old_state[pk]

        browser.close()

    save_state(new_state)
    log("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
