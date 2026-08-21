"""Core business logic for the Website Lead Finder agent.

Provides:
- Google Places API (New) text search for businesses
- DuckDuckGo web enrichment (owner / decision-maker info)
- SQLite lead storage
- Local Ollama text generation (UI/UX prompts + outreach emails)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests

ROOT_DIR = Path(__file__).resolve().parent.parent
DB_PATH = ROOT_DIR / "leads.db"

PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_GEOCODE_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
OSM_USER_AGENT = "LeadFinderAgent/0.1 (local web-design prospecting tool)"

# Maps common business-type words to OpenStreetMap tag pairs for Overpass queries
OSM_TAG_MAP: list[tuple[tuple[str, ...], tuple[str, str]]] = [
    (("dental", "dentist"), ("amenity", "dentist")),
    (("clinic", "doctor", "physician"), ("amenity", "doctors")),
    (("clinic",), ("amenity", "clinic")),
    (("hospital",), ("amenity", "hospital")),
    (("pharmacy", "chemist", "medical store"), ("amenity", "pharmacy")),
    (("gym", "fitness"), ("leisure", "fitness_centre")),
    (("gym", "sports"), ("leisure", "sports_centre")),
    (("yoga",), ("leisure", "fitness_centre")),
    (("cafe", "coffee", "caf\u00e9"), ("amenity", "cafe")),
    (("restaurant", "food", "dine"), ("amenity", "restaurant")),
    (("bakery",), ("shop", "bakery")),
    (("bar", "pub"), ("amenity", "bar")),
    (("hotel",), ("tourism", "hotel")),
    (("guest house", "guesthouse", "lodge"), ("tourism", "guest_house")),
    (("school",), ("amenity", "school")),
    (("college", "institute", "university", "coaching"), ("amenity", "college")),
    (("coaching", "tution", "tuition"), ("amenity", "prep_school")),
    (("bank",), ("amenity", "bank")),
    (("atm",), ("amenity", "atm")),
    (("salon", "saloon", "haircut", "beauty", "parlour"), ("shop", "hairdresser")),
    (("beauty", "spa"), ("leisure", "spa")),
    (("supermarket", "grocery", "kirana"), ("shop", "supermarket")),
    (("medical", "lab", "diagnostic"), ("healthcare", "laboratory")),
    (("petrol", "fuel", "gas station"), ("amenity", "fuel")),
    (("travel", "agency"), ("shop", "travel_agency")),
    (("insurance",), ("office", "insurance")),
    (("accountant", "chartered", "ca firm", "tax"), ("office", "accountant")),
    (("lawyer", "advocate", "legal", "law firm"), ("office", "lawyer")),
    (("estate", "property", "real estate"), ("office", "estate_agent")),
    (("it company", "software", "tech"), ("office", "it")),
]


def parse_search_query(query: str) -> dict[str, Any] | None:
    """Parse a Google-style query ('clinic in wardha') into type + location."""
    q = (query or "").strip()
    if not q:
        return None

    # Fast path: "<type> in|near|at|around <place>"
    parts = re.split(r"\s+(?:in|near|at|around)\s+", q, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return {
            "business_type": parts[0].strip(" \"'"),
            "location": parts[1].strip(" \"'"),
            "via": "pattern",
        }

    # Comma path: "wardha, clinic"
    if "," in q:
        a, b = q.split(",", 1)
        a, b = a.strip(), b.strip()
        if a and b:
            return {"business_type": b, "location": a, "via": "comma"}

    # AI path for anything else ("wardha clinics", misspellings…)
    try:
        raw = generate_text(
            f'Split this search into a business category and a city/area. '
            f'Fix spelling. Reply ONLY JSON: {{"business_type":"...","location":"..."}}\n'
            f'SEARCH: "{q}"'
        )
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if m:
            d = json.loads(m.group(0))
            bt, loc = str(d.get("business_type", "")).strip(), str(d.get("location", "")).strip()
            if bt and loc:
                return {"business_type": bt, "location": loc, "via": "ai"}
    except Exception:
        pass

    # Last resort: whole string is the type; no location found
    return {"business_type": q, "location": "", "via": "none"}


def _osm_tags_for(business_type: str) -> list[tuple[str, str]]:
    bt = (business_type or "").lower()
    pairs: list[tuple[str, str]] = []
    for keys, tag in OSM_TAG_MAP:
        if any(k in bt for k in keys) and tag not in pairs:
            pairs.append(tag)
    return pairs


def ai_expand_search(business_type: str, location: str) -> dict[str, Any] | None:
    """Ask the LLM to understand the search: fix typos, normalize the
    category, and suggest related business categories worth including.

    Returns {corrected_type, corrected_location, related:[...]} or None on
    any failure (the caller then falls back to static tag mapping).
    """
    prompt = f"""Understand this business search and reply with ONLY compact JSON:
{{"corrected_type": "...", "corrected_location": "...", "related": ["...", "..."]}}

SEARCH: "{business_type}" in "{location}"

Rules:
- Fix spelling/mistakes in both fields (e.g. "denatl clinc" -> "dental clinic", "jaipr" -> "Jaipur").
- corrected_type: the clean, standard business category in English lowercase.
- related: 2-5 nearby categories a customer of this business would also care about \
(e.g. dental clinic -> ["doctor", "hospital", "pharmacy"]). Empty list if none.
- No commentary, no markdown fences — JSON only."""
    try:
        raw = generate_text(prompt).strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(0))
        out = {
            "corrected_type": str(data.get("corrected_type") or business_type).strip(),
            "corrected_location": str(data.get("corrected_location") or location).strip(),
            "related": [str(x).strip() for x in (data.get("related") or [])][:5],
        }
        return out
    except Exception:
        return None


def _nominatim_bbox(location: str) -> list[str] | None:
    """Geocode a location string to an OSM bounding box [s, n, w, e]."""
    resp = requests.get(
        NOMINATIM_GEOCODE_URL,
        params={"q": location, "format": "jsonv2", "limit": 1},
        headers={"User-Agent": OSM_USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return data[0]["boundingbox"] if data else None


def _search_overpass(
    business_type: str,
    location: str,
    max_results: int,
    extra_tags: list[tuple[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Structured POI search via the free Overpass API (no key needed).

    Much better business coverage than plain Nominatim text search because it
    queries OSM's tagged amenity/shop/leisure/office data directly.
    """
    bbox = _nominatim_bbox(location)
    if not bbox:
        return []
    s, n, w, e = (float(x) for x in bbox)
    # Pad tiny bounding boxes (landmark-level matches) so city searches get enough area
    min_span = 0.12
    if n - s < min_span:
        mid, half = (n + s) / 2, min_span / 2
        s, n = mid - half, mid + half
    if e - w < min_span:
        mid, half = (e + w) / 2, min_span / 2
        w, e = mid - half, mid + half

    tag_pairs = list(dict.fromkeys((extra_tags or []) + _osm_tags_for(business_type)))
    if not tag_pairs:
        return []  # caller falls back to Nominatim text search

    union = "\n".join(
        f'  node["{k}"="{v}"]({s},{w},{n},{e});\n'
        f'  way["{k}"="{v}"]({s},{w},{n},{e});'
        for k, v in tag_pairs[:10]
    )
    query = f"[out:json][timeout:40];\n(\n{union}\n);\nout center tags {min(max_results * 3 + 40, 160)};"

    last_exc: Exception | None = None
    data = None
    for ov_url in OVERPASS_URLS:
        try:
            resp = requests.post(
                ov_url,
                data={"data": query},
                headers={"User-Agent": OSM_USER_AGENT},
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as exc:
            last_exc = exc
            continue
    if data is None:
        raise RuntimeError(f"All Overpass mirrors failed: {last_exc}")

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for el in resp.json().get("elements", []):
        tags = el.get("tags") or {}
        name = (tags.get("name") or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())

        street = " ".join(
            x for x in (
                tags.get("addr:housenumber"),
                tags.get("addr:street"),
            ) if x
        )
        city_bits = ", ".join(
            x for x in (
                tags.get("addr:suburb") or tags.get("addr:neighbourhood"),
                tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village"),
                tags.get("addr:state"),
            ) if x
        )
        address = ", ".join(x for x in (street, city_bits) if x) or f"{name}, {location}"

        btype = tags.get("amenity") or tags.get("shop") or tags.get("leisure") \
            or tags.get("office") or tags.get("tourism") or tags.get("healthcare") \
            or business_type

        results.append({
            "place_id": f"osm_{el.get('type', 'n')}_{el.get('id')}",
            "name": name,
            "business_type": str(btype).replace("_", " ").title(),
            "address": address,
            "phone": tags.get("phone") or tags.get("contact:phone") or tags.get("mobile"),
            "website": tags.get("website") or tags.get("contact:website"),
            "rating": None,
            "reviews": None,
            "maps_uri": None,
        })
        if len(results) >= max_results:
            break
    return results

PLACES_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.nationalPhoneNumber,places.websiteUri,places.rating,"
    "places.userRatingCount,places.primaryTypeDisplayName,places.googleMapsUri"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id TEXT UNIQUE,
    name TEXT NOT NULL,
    business_type TEXT,
    address TEXT,
    phone TEXT,
    website TEXT,
    rating REAL,
    reviews INTEGER,
    maps_uri TEXT,
    search_location TEXT,
    tier TEXT,
    score INTEGER DEFAULT 0,
    score_reasons TEXT,
    pitch_note TEXT,
    enrichment_json TEXT,
    uiux_prompt TEXT,
    email_draft TEXT,
    analyzed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

# India tier-1 metros — everything else is treated as tier 2/3 (higher opportunity)
TIER_1_CITIES = {
    "mumbai", "delhi", "new delhi", "bangalore", "bengaluru", "hyderabad",
    "chennai", "kolkata", "pune", "ahmedabad",
}


def detect_tier(location: str) -> str:
    loc = (location or "").lower()
    if any(city in loc for city in TIER_1_CITIES):
        return "Tier 1"
    return "Tier 2/3"


def compute_lead_score(lead: dict[str, Any]) -> dict[str, Any]:
    """Rule-based pitch-chance score (0-100) from available business signals."""
    score = 30  # baseline: every SMB is a potential client
    reasons: list[str] = []

    if not lead.get("website"):
        score += 25
        reasons.append("No website found — high need (+25)")
    else:
        score += 5
        reasons.append("Has a website — redesign pitch only (+5)")

    reviews = lead.get("reviews") or 0
    if reviews >= 200:
        score += 15
        reasons.append(f"Well-established ({reviews} reviews) (+15)")
    elif reviews >= 50:
        score += 8
        reasons.append(f"Established ({reviews} reviews) (+8)")

    rating = lead.get("rating") or 0
    if rating >= 4.3:
        score += 10
        reasons.append(f"Strong reputation ({rating}) (+10)")
    elif rating >= 3.5:
        score += 5
        reasons.append(f"Decent reputation ({rating}) (+5)")

    if lead.get("phone"):
        score += 5
        reasons.append("Phone available — directly contactable (+5)")

    tier = lead.get("tier") or detect_tier(lead.get("search_location", ""))
    if tier == "Tier 2/3":
        score += 10
        reasons.append("Tier 2/3 location — less competition, growing market (+10)")

    score = min(score, 100)
    return {"score": score, "tier": tier, "reasons": reasons}


def score_label(score: int) -> str:
    if score >= 70:
        return "High"
    if score >= 45:
        return "Medium"
    return "Low"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    try:
        conn.execute("ALTER TABLE leads ADD COLUMN batch_id TEXT")
    except Exception:
        pass
    return conn


def get_latest_batch_id() -> str | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT batch_id FROM leads WHERE batch_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["batch_id"] if row else None


# ---------------------------------------------------------------------------
# Google Places API (New) — text search
# ---------------------------------------------------------------------------

def _search_osm(business_type: str, location: str, max_results: int) -> list[dict[str, Any]]:
    """Free fallback search via OpenStreetMap Nominatim (no API key required)."""
    params = {
        "q": f"{business_type} in {location}",
        "format": "jsonv2",
        "addressdetails": 1,
        "extratags": 1,
        "limit": min(max_results + 10, 40),
    }
    resp = requests.get(
        NOMINATIM_SEARCH_URL, params=params,
        headers={"User-Agent": OSM_USER_AGENT}, timeout=30,
    )
    resp.raise_for_status()
    results: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for it in resp.json():
        tags = it.get("extratags") or {}
        name = (it.get("name") or "").strip() or (
            (it.get("display_name") or "").split(",")[0].strip()
        )
        if not name or name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        btype = it.get("addresstype") or it.get("type") or business_type
        results.append({
            "place_id": f"osm_{it.get('osm_type', 'n')}_{it.get('osm_id')}",
            "name": name,
            "business_type": str(btype).replace("_", " ").title(),
            "address": it.get("display_name"),
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "website": tags.get("website") or tags.get("contact:website"),
            "rating": None,
            "reviews": None,
            "maps_uri": None,
        })
        if len(results) >= max_results:
            break
    return results


def search_businesses(business_type: str, location: str, max_results: int = 20) -> dict[str, Any]:
    """Search for a business type in a location and store new leads.

    Uses Google Places API when a key is configured in .env; automatically
    falls back to the free OpenStreetMap Nominatim service if the key is
    missing OR rejected (401/403), so search never breaks.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "").strip()
    use_google = bool(api_key) and len(api_key) > 10

    # --- AI understanding: fix typos, normalize category, find related ones ---
    ai_note = None
    related_used: list[str] = []
    ai = ai_expand_search(business_type, location)
    if ai:
        eff_type = ai["corrected_type"] or business_type
        eff_loc = ai["corrected_location"] or location
        related_used = ai["related"]
        if (
            eff_type.lower() != business_type.lower()
            or eff_loc.lower() != location.lower()
        ):
            ai_note = f"Understood as \u201c{eff_type}\u201d in \u201c{eff_loc}\u201d"
        elif related_used:
            ai_note = None
    else:
        eff_type, eff_loc = business_type, location

    batch_id = f"b_{int(time.time() * 1000)}"

    normalized: list[dict[str, Any]] = []
    source = "google_places"
    google_note = ""

    if use_google:
        body = {
            "textQuery": f"{eff_type} in {eff_loc}",
            "pageSize": min(max_results, 20),
        }
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": PLACES_FIELD_MASK,
        }
        try:
            resp = requests.post(
                PLACES_TEXT_SEARCH_URL, json=body, headers=headers, timeout=30
            )
            resp.raise_for_status()
            for p in resp.json().get("places", [])[:max_results]:
                if not p.get("id"):
                    continue
                normalized.append({
                    "place_id": p["id"],
                    "name": p.get("displayName", {}).get("text", "Unknown"),
                    "business_type": p.get("primaryTypeDisplayName", {}).get("text", business_type),
                    "address": p.get("formattedAddress"),
                    "phone": p.get("nationalPhoneNumber"),
                    "website": p.get("websiteUri"),
                    "rating": p.get("rating"),
                    "reviews": p.get("userRatingCount"),
                    "maps_uri": p.get("googleMapsUri"),
                })
        except requests.RequestException as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = ""
            try:
                detail = resp.json().get("error", {}).get("message", "")
            except Exception:
                pass
            if code in (401, 403):
                google_note = (
                    f"Google rejected the API key ({code}). "
                    "Used free OpenStreetMap instead \u2014 get a valid key at "
                    "console.cloud.google.com (keys start with 'AIza')."
                )
            elif code == 400 and "API key not valid" in detail:
                google_note = (
                    f"Invalid API key. Used free OpenStreetMap instead. ({detail[:140]})"
                )
            else:
                return {"status": "error", "message": f"Places API failed: {exc} {detail}"}
    else:
        google_note = "No Google key configured — using free OpenStreetMap."

    if not normalized:
        source = "openstreetmap"
        try:
            try:
                related_pairs = [
                    t for r in related_used for t in _osm_tags_for(r)
                ]
                normalized = _search_overpass(
                    eff_type, eff_loc, max_results, extra_tags=related_pairs
                )
            except Exception:
                normalized = []  # Overpass down → Nominatim still runs below

            # Always merge Nominatim results too — maximizes total businesses found
            try:
                nom = _search_osm(eff_type, eff_loc, max_results)
            except Exception:
                nom = []
            seen_keys = {
                n["place_id"] for n in normalized
            } | {n["name"].lower()[:40] for n in normalized}
            for item in nom:
                key_id = item["place_id"]
                key_name = item["name"].lower()[:40]
                if key_id in seen_keys or key_name in seen_keys:
                    continue
                seen_keys.update((key_id, key_name))
                normalized.append(item)
            normalized = normalized[:max_results]
        except Exception as exc:
            msg = f"OpenStreetMap search failed: {exc}"
            if google_note:
                msg += f" | {google_note}"
            return {"status": "error", "message": msg}

    saved, skipped = [], 0
    with _connect() as conn:
        for item in normalized:
            pid = item["place_id"]
            existing = conn.execute(
                "SELECT id FROM leads WHERE place_id = ?", (pid,)
            ).fetchone()
            if existing:
                skipped += 1
                continue
            item["search_location"] = location
            cur = conn.execute(
                """INSERT INTO leads
                   (place_id, name, business_type, address, phone, website,
                    rating, reviews, maps_uri, search_location, tier,
                    score, score_reasons, batch_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item["place_id"],
                    item["name"],
                    item["business_type"],
                    item["address"],
                    item["phone"],
                    item["website"],
                    item["rating"],
                    item["reviews"],
                    item["maps_uri"],
                    location,
                    detect_tier(location),
                    0,
                    None,
                    batch_id,
                ),
            )
            scored = compute_lead_score(item)
            conn.execute(
                "UPDATE leads SET tier = ?, score = ?, score_reasons = ? WHERE id = ?",
                (scored["tier"], scored["score"], json.dumps(scored["reasons"]), cur.lastrowid),
            )
            saved.append(
                {"lead_id": cur.lastrowid, "name": item["name"], "score": scored["score"]}
            )

    result: dict[str, Any] = {
        "status": "ok",
        "source": source,
        "batch_id": batch_id,
        "understood": {"type": eff_type, "location": eff_loc},
        "related_used": related_used,
        "found": len(normalized),
        "new_leads_saved": len(saved),
        "already_in_db": skipped,
        "leads": [
            {"lead_id": s["lead_id"], "name": s["name"], "score": s["score"]}
            for s in saved
        ],
    }
    if ai_note:
        result["ai_note"] = ai_note
    if source == "openstreetmap":
        result["note"] = (
            google_note
            or "Searched via free OpenStreetMap (phones/ratings often missing). "
            "Add a Google Places API key to .env for richer data."
        )
    return result



# ---------------------------------------------------------------------------
# DuckDuckGo enrichment (free, no key)
# ---------------------------------------------------------------------------

def enrich_contact(business_name: str, location: str) -> dict[str, Any]:
    """Web-search for owner / founder / decision-maker info about a business."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # fallback package name

    queries = [
        f'"{business_name}" {location} owner OR founder OR director',
        f'"{business_name}" {location} contact email phone',
    ]
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with DDGS() as ddgs:
            for q in queries:
                for r in ddgs.text(q, max_results=6):
                    url = r.get("href") or r.get("url")
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    results.append(
                        {
                            "title": r.get("title"),
                            "url": url,
                            "snippet": r.get("body"),
                        }
                    )
    except Exception as exc:
        return {"status": "error", "message": f"Web enrichment failed: {exc}", "results": []}

    return {"status": "ok", "results": results[:10]}


# ---------------------------------------------------------------------------
# Local Ollama generation
# ---------------------------------------------------------------------------

def generate_with_ollama(prompt: str, system: str | None = None) -> str:
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "gemma4")
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "stream": False}
    if system:
        payload["system"] = system
    resp = requests.post(f"{base}/api/generate", json=payload, timeout=600)
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


GEMINI_MODEL_CHAIN = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
]

_RETRYABLE_CODES = {429, 500, 502, 503, 504}


def generate_with_gemini(prompt: str, system: str | None = None) -> str:
    """Cloud LLM path — Google AI Studio free tier works (no Ollama needed).

    Retries transient errors (429/5xx) and falls back through the model chain
    so a single overloaded model never breaks analysis.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey or set LLM_PROVIDER=ollama."
        )
    configured = os.environ.get("GEMINI_MODEL", "").strip()
    models = ([configured] if configured else []) + [
        m for m in GEMINI_MODEL_CHAIN if m != configured
    ]

    last_err: Exception | None = None
    for model in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent"
        )
        payload: dict[str, Any] = {"contents": [{"parts": [{"text": prompt}]}]}
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        for attempt in range(2):
            try:
                resp = requests.post(
                    url, params={"key": api_key}, json=payload, timeout=180
                )
                resp.raise_for_status()
                try:
                    return resp.json()["candidates"][0]["content"]["parts"][0][
                        "text"
                    ].strip()
                except (KeyError, IndexError) as exc:
                    raise RuntimeError(
                        f"Gemini returned an unexpected response: "
                        f"{str(resp.json())[:200]}"
                    ) from exc
            except requests.RequestException as exc:
                last_err = exc
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code not in _RETRYABLE_CODES:
                    raise
                time.sleep(2 * (attempt + 1))

    raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")


def get_llm_provider() -> str:
    return os.environ.get("LLM_PROVIDER", "ollama").strip().lower()


def generate_text(prompt: str, system: str | None = None) -> str:
    """Dispatch to the configured LLM backend: 'ollama' (local) or 'gemini' (cloud)."""
    if get_llm_provider() == "gemini":
        return generate_with_gemini(prompt, system)
    return generate_with_ollama(prompt, system)


def build_uiux_prompt(lead: dict[str, Any], enrichment: list[dict] | None = None) -> str:
    context_lines = [
        f"- Business name: {lead.get('name')}",
        f"- Type: {lead.get('business_type')}",
        f"- Location: {lead.get('address') or lead.get('search_location')}",
        f"- Phone: {lead.get('phone')}",
        f"- Current website: {lead.get('website') or 'None found'}",
        f"- Rating: {lead.get('rating')} ({lead.get('reviews')} reviews)",
    ]
    if enrichment:
        snippets = "\n".join(
            f"  - {r.get('title')}: {(r.get('snippet') or '')[:200]}"
            for r in enrichment[:5]
        )
        context_lines.append(f"- Web findings:\n{snippets}")

    system = (
        "You are an expert UI/UX designer. You produce precise, ready-to-use "
        "design prompts that another AI or designer can execute to build a "
        "complete website demo."
    )
    user = f"""Create a detailed UI/UX prompt for designing a modern website demo for this business.
The prompt must specify: design style/theme, color palette (with hex codes), typography,
page structure (home, about, services, gallery, contact), key sections per page,
imagery style, mobile responsiveness notes, and any special features suited to this business type.

Business details:
{chr(10).join(context_lines)}

Output only the final UI/UX design prompt as a single well-structured brief."""

    return generate_text(user, system)


def build_outreach_email(lead: dict[str, Any], contact_name: str | None = None) -> str:
    has_site = bool(lead.get("website"))
    system = (
        "You are a polite, concise B2B outreach writer for a web design agency. "
        "Write emails that are short, specific, and never spammy."
    )
    user = f"""Draft a professional outreach email offering website design services.

Business: {lead.get('name')}
Type: {lead.get('business_type')}
Location: {lead.get('address')}
Contact person: {contact_name or 'Unknown'}
Current website: {lead.get('website') or 'They do NOT appear to have a website'}

Rules:
- Subject line first, then blank line, then the email body.
- Max 150 words in the body.
- Mention one concrete benefit tailored to their business type.
{f"- Note their current site ({lead['website']}) could be improved." if has_site else "- Emphasize they are missing out on customers by not having a website."}
- End with a soft call to action (quick call or reply).
- Use [Your Name] and [Your Agency] placeholders for signature.
- If contact person is unknown, use a friendly generic greeting."""

    return generate_text(user, system)


def build_pitch_note(lead: dict[str, Any], enrichment: list[dict] | None = None) -> str:
    """Short AI assessment of the chance to win this client and how to pitch."""
    system = (
        "You are a pragmatic sales strategist for a web design agency "
        "targeting Indian SMBs. Be realistic and concise."
    )
    findings = ""
    if enrichment:
        findings = "\n".join(
            f"- {(r.get('snippet') or '')[:150]}" for r in (enrichment[:4])
        )
    user = f"""Assess this business as a web-design client prospect.

Business: {lead.get('name')} ({lead.get('business_type')})
Location: {lead.get('address') or lead.get('search_location')} — City tier: {lead.get('tier')}
Rating: {lead.get('rating')} from {lead.get('reviews')} reviews
Website: {lead.get('website') or 'NONE'}
Web findings:
{findings or '- none'}

In under 120 words output:
1. CHANCE: High/Medium/Low to win them as a client and why.
2. NEED: their biggest website need based on business type.
3. PITCH ANGLE: one sentence on exactly what to say to them."""

    return generate_text(user, system)


# ---------------------------------------------------------------------------
# Lead persistence helpers
# ---------------------------------------------------------------------------

def save_lead_analysis(
    lead_id: int,
    enrichment: dict[str, Any],
    uiux_prompt: str,
    email_draft: str,
    pitch_note: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """UPDATE leads
               SET enrichment_json = ?, uiux_prompt = ?, email_draft = ?,
                   pitch_note = COALESCE(?, pitch_note), analyzed = 1
               WHERE id = ?""",
            (json.dumps(enrichment), uiux_prompt, email_draft, pitch_note, lead_id),
        )


def get_all_leads(
    sort: str = "score",
    service: str | None = None,
    batch: str | None = None,
) -> list[dict[str, Any]]:
    order_by = {
        "score": "score DESC, rating DESC",
        "name": "name ASC",
        "rating": "rating DESC",
        "reviews": "reviews DESC",
        "newest": "id DESC",
    }.get(sort, "score DESC")

    query = f"""
        SELECT id, name, business_type, address, phone, website, rating,
               reviews, maps_uri, search_location, tier, score,
               score_reasons, pitch_note, uiux_prompt, email_draft,
               analyzed, created_at
        FROM leads
    """
    params: list[Any] = []
    wheres: list[str] = []
    if batch == "latest":
        latest = get_latest_batch_id()
        if latest:
            wheres.append("batch_id = ?")
            params.append(latest)
    elif batch:
        wheres.append("batch_id = ?")
        params.append(batch)
    if service and service.lower() not in ("all", ""):
        wheres.append("LOWER(business_type) LIKE ?")
        params.append(f"%{service.lower()}%")
    if wheres:
        query += " WHERE " + " AND ".join(wheres)
    query += f" ORDER BY {order_by}"

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_service_types() -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT business_type FROM leads WHERE business_type IS NOT NULL"
        ).fetchall()
        return sorted({r["business_type"] for r in rows})


def get_pitch_summary(min_score: int = 45, limit: int = 8) -> dict[str, Any]:
    """Summary of how many businesses are worth pitching + top suggestions."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM leads").fetchone()["c"]
        pitchable = conn.execute(
            "SELECT COUNT(*) AS c FROM leads WHERE score >= ?", (min_score,)
        ).fetchone()["c"]
        analyzed = conn.execute(
            "SELECT COUNT(*) AS c FROM leads WHERE analyzed = 1"
        ).fetchone()["c"]
        by_type_rows = conn.execute(
            """SELECT business_type, COUNT(*) AS c FROM leads
               GROUP BY business_type ORDER BY c DESC LIMIT 10"""
        ).fetchall()
        top = conn.execute(
            """SELECT id, name, business_type, address, search_location, tier,
                      score, rating, reviews, website, analyzed
               FROM leads WHERE score >= ? ORDER BY score DESC LIMIT ?""",
            (min_score, limit),
        ).fetchall()

    return {
        "total_leads": total,
        "pitchable_leads": pitchable,
        "analyzed_leads": analyzed,
        "min_score": min_score,
        "by_business_type": [dict(r) for r in by_type_rows],
        "top_suggestions": [dict(r) for r in top],
    }


def get_lead(lead_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None:
            return None
        lead = dict(row)
        lead["enrichment"] = (
            json.loads(lead.pop("enrichment_json")) if lead.get("enrichment_json") else None
        )
        return lead


def delete_lead(lead_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Fast contact enrichment (no LLM — regex extraction, runs in seconds)
# ---------------------------------------------------------------------------

PHONE_RE = re.compile(
    r"(?:\+?91[\-\s]?)?[6-9]\d{9}"
    r"|\+?91[\-\s]\d{5}[\-\s]\d{5}"
    r"|0\d{2,4}[\-\s]?\d{6,8}"
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

DIRECTORY_DOMAINS = (
    "justdial.com", "indiamart.com", "sulekha.com", "tradeindia.com",
    "yellowpages", "practo.com", "lybrate.com", "magicpin", "nearbuy",
    "olx.in", "quikr.com",
)
SOCIAL_DOMAINS = ("facebook.com", "linkedin.com", "instagram.com", "twitter.com", "x.com")


def _classify_url(url: str) -> str:
    u = url.lower()
    if any(d in u for d in DIRECTORY_DOMAINS):
        return "directory"
    if any(d in u for d in SOCIAL_DOMAINS):
        return "social"
    return "other"


def quick_enrich(lead_id: int) -> dict[str, Any]:
    """Fast, LLM-free enrichment: directory listings, phones, emails,
    socials and an official-website guess for one lead. Runs in seconds."""
    lead = get_lead(lead_id)
    if lead is None:
        return {"status": "error", "message": f"Lead {lead_id} not found"}

    name = lead["name"]
    loc = lead.get("search_location") or ""
    queries = [
        f'"{name}" {loc} contact phone',
        f'"{name}" {loc} justdial OR indiamart OR sulekha',
        f'"{name}" {loc} official website',
    ]

    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    phones: set[str] = set()
    emails: set[str] = set()

    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            for q in queries:
                try:
                    rows = list(ddgs.text(q, max_results=6))
                except Exception:
                    continue
                for r in rows:
                    url = r.get("href") or r.get("url") or ""
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    text_blob = f"{r.get('title') or ''} {r.get('body') or ''}"
                    for m in PHONE_RE.findall(text_blob):
                        digits = re.sub(r"\D", "", m)
                        if 10 <= len(digits) <= 13:
                            phones.add(m.strip())
                    for m in EMAIL_RE.findall(text_blob):
                        if not any(x in m.lower() for x in ("example.", ".png", ".jpg")):
                            emails.add(m.lower())
                    kind = _classify_url(url)
                    sources.append(
                        {
                            "kind": kind,
                            "title": (r.get("title") or "")[:120],
                            "url": url,
                            "snippet": (r.get("body") or "")[:220],
                        }
                    )
    except Exception as exc:
        return {"status": "error", "message": f"Enrichment search failed: {exc}"}

    directories = [s for s in sources if s["kind"] == "directory"]
    socials = [s for s in sources if s["kind"] == "social"]
    others = [s for s in sources if s["kind"] == "other"]

    # Official website guess: first 'other' domain whose domain shares tokens with the name
    name_tokens = {t for t in re.findall(r"[a-z0-9]{3,}", name.lower())}
    website_guess = None
    if not lead.get("website"):
        for s in others:
            try:
                host = re.sub(r"^www\.", "", s["url"].split("/")[2])
            except Exception:
                continue
            host_tokens = set(re.findall(r"[a-z0-9]{3,}", host))
            if host_tokens & name_tokens:
                website_guess = s["url"]
                break
        if website_guess is None and others:
            website_guess = others[0]["url"]

    enrichment = {
        "kind": "quick",
        "phones": sorted(phones)[:4],
        "emails": sorted(emails)[:3],
        "directories": directories[:5],
        "socials": socials[:4],
        "related_sites": others[:6],
        "website_guess": website_guess,
        "sources_count": len(sources),
    }

    with _connect() as conn:
        conn.execute(
            """UPDATE leads
               SET enrichment_json = ?, analyzed = 1,
                   phone = COALESCE(NULLIF(phone,''), ?),
                   website = COALESCE(website, ?)
               WHERE id = ?""",
            (
                json.dumps(enrichment),
                (sorted(phones)[0] if phones else None),
                website_guess,
                lead_id,
            ),
        )

    return {"status": "ok", "lead_id": lead_id, "enrichment": enrichment}


def build_leads_context(limit: int = 200) -> str:
    """Compact JSON of all leads to inject into the chat prompt."""
    leads = get_all_leads(sort="score")[:limit]
    compact = [
        {
            "id": l["id"],
            "name": l["name"],
            "type": l["business_type"],
            "city": l["search_location"],
            "tier": l["tier"],
            "score": l["score"],
            "rating": l["rating"],
            "reviews": l["reviews"],
            "website": bool(l["website"]),
            "phone": l["phone"],
            "analyzed": bool(l["analyzed"]),
        }
        for l in leads
    ]
    return json.dumps(compact, ensure_ascii=False)


CHAT_SYSTEM_PROMPT = """You are the AI analyst inside a business lead-generation dashboard \
for a web design agency. You are given the full lead database as JSON.

Your jobs:
1. FIX SPELLING: users often mistype searches (e.g. "dental clinc in jaipr" means \
"dental clinic in Jaipur"). Always understand the intent.
2. ANALYZE: answer questions about the businesses using ONLY the provided data — \
rank best pitch targets, explain scores, compare by city tier / reviews / missing websites.
3. RECOMMEND: suggest which businesses to contact first and why.
4. DRAFT: when asked, write short outreach emails or UI/UX design prompts for a \
specific lead (use its id/name/type/city).
5. SEARCH INTENT: if the user clearly wants to FIND new businesses \
(e.g. "find cafes in Pune"), reply briefly, then end your message with exactly this line:
ACTION: search | <corrected business type> | <corrected location>

Rules: be concise. Never invent data that is not in the JSON — say what is missing. \
Use markdown-lite (bold, dashes) for readability."""


def chat_with_ai(message: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """One fast Gemini call with the whole lead database as context."""
    context = build_leads_context()
    user_block = (
        f"LEAD DATABASE ({json.dumps({'count': 'see below'})}):\n{context}\n\n"
        f"USER MESSAGE:\n{message}"
    )
    contents = []
    for h in (history or [])[-6:]:
        role = "user" if h.get("role") == "user" else "model"
        contents.append({"role": role, "parts": [{"text": h.get("text", "")}]})
    contents.append({"role": "user", "parts": [{"text": user_block}]})

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    provider = get_llm_provider()

    if provider == "gemini" and api_key:
        models = [os.environ.get("GEMINI_MODEL") or GEMINI_MODEL_CHAIN[0]] + GEMINI_MODEL_CHAIN
        seen: list[str] = []
        for m in models:
            if m not in seen:
                seen.append(m)
        last_err: Exception | None = None
        payload: dict[str, Any] = {
            "systemInstruction": {"parts": [{"text": CHAT_SYSTEM_PROMPT}]},
            "contents": contents,
        }
        for model in seen:
            url = (
                "https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model}:generateContent"
            )
            for attempt in range(2):
                try:
                    resp = requests.post(
                        url, params={"key": api_key}, json=payload, timeout=120
                    )
                    resp.raise_for_status()
                    reply = resp.json()["candidates"][0]["content"]["parts"][0][
                        "text"
                    ].strip()
                    action = None
                    m = re.search(
                        r"ACTION:\s*search\s*\|\s*([^|]+)\|\s*(.+)", reply
                    )
                    if m:
                        action = {
                            "type": "search",
                            "business_type": m.group(1).strip(),
                            "location": m.group(2).strip(),
                        }
                        reply = reply[: m.start()].rstrip()
                    return {"status": "ok", "reply": reply, "action": action}
                except requests.RequestException as exc:
                    last_err = exc
                    code = getattr(getattr(exc, "response", None), "status_code", None)
                    if code not in _RETRYABLE_CODES:
                        raise RuntimeError(f"Gemini error: {exc}") from exc
                    time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"All Gemini models failed. Last error: {last_err}")

    # Local fallback
    prompt = CHAT_SYSTEM_PROMPT + "\n\n" + user_block
    return {"status": "ok", "reply": generate_with_ollama(prompt), "action": None}




def analyze_lead_pipeline(lead_id: int) -> dict[str, Any]:
    lead = get_lead(lead_id)
    if lead is None:
        return {"status": "error", "message": f"Lead {lead_id} not found"}

    enrichment = enrich_contact(lead["name"], lead["search_location"] or "")

    contact_name = None
    if enrichment.get("results"):
        first = enrichment["results"][0].get("title") or ""
        for token in (" - ", " | ", " · "):
            if token in first:
                candidate = first.split(token)[0].strip()
                if candidate and len(candidate) < 60 and lead["name"].lower() not in candidate.lower():
                    contact_name = candidate
                break

    uiux_prompt = build_uiux_prompt(lead, enrichment.get("results"))
    email_draft = build_outreach_email(lead, contact_name)

    pitch_note = None
    try:
        pitch_note = build_pitch_note(lead, enrichment.get("results"))
    except Exception as exc:
        pitch_note = f"(Pitch note generation failed: {exc})"

    save_lead_analysis(lead_id, enrichment, uiux_prompt, email_draft, pitch_note)

    return {
        "status": "ok",
        "lead": get_lead(lead_id),
        "likely_contact_person": contact_name,
    }
