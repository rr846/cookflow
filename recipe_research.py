"""
Cookflow – Recipe Research Service
Recherchiert wöchentlich neue Rezeptinspirationen via Web-Suche + Claude.
Baut eine wachsende Inspirations-Datenbank auf.
"""

import json
import logging
import os
import sqlite3
import urllib.request
import urllib.parse
from datetime import datetime, date
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger("cookflow.research")

DB_PATH = Path(__file__).parent / "meal_planner.db"

# Saisonale Zutaten für Deutschland
SEASONAL_INGREDIENTS = {
    1:  ["Grünkohl", "Feldsalat", "Rosenkohl", "Pastinake", "Schwarzwurzel", "Chicorée"],
    2:  ["Grünkohl", "Feldsalat", "Rosenkohl", "Topinambur", "Lauch", "Rotkohl"],
    3:  ["Bärlauch", "Spinat", "Radieschen", "Rhabarber", "Porree", "Frühlingszwiebeln"],
    4:  ["Spargel", "Bärlauch", "Spinat", "Radieschen", "Rhabarber", "Kohlrabi"],
    5:  ["Spargel", "Erdbeeren", "Kohlrabi", "Mangold", "Blumenkohl", "Fenchel"],
    6:  ["Kirschen", "Johannisbeeren", "Zucchini", "Bohnen", "Erbsen", "Brokkoli"],
    7:  ["Tomaten", "Paprika", "Aubergine", "Heidelbeeren", "Pfirsiche", "Gurke"],
    8:  ["Tomaten", "Mais", "Pflaumen", "Brombeeren", "Paprika", "Bohnen"],
    9:  ["Kürbis", "Pilze", "Birnen", "Trauben", "Sellerie", "Rote Bete"],
    10: ["Kürbis", "Äpfel", "Quitten", "Maronen", "Grünkohl", "Schwarzwurzel"],
    11: ["Grünkohl", "Rosenkohl", "Feldsalat", "Pastinake", "Steckrübe", "Topinambur"],
    12: ["Grünkohl", "Rosenkohl", "Feldsalat", "Rotkohl", "Maronen", "Chicorée"],
}

# Küchen für die Recherche
CUISINES = [
    "Italienisch", "Asiatisch", "Mexikanisch", "Indisch", "Mediterran",
    "Japanisch", "Thai", "Koreanisch", "Vietnamesisch", "Persisch",
    "Griechisch", "Nahöstlich", "Deutsch", "Französisch", "Spanisch",
]

# Recherche-Themen pro Durchlauf
RESEARCH_TOPICS = [
    "trending vegan recipes {season} {year}",
    "best seasonal {cuisine} recipes {month}",
    "creative plant-based dinner ideas {season}",
    "easy weeknight vegan meals {cuisine} style",
    "impressive vegan recipes restaurant quality",
    "comfort food vegan recipes {season}",
    "healthy meal prep vegan {month} {year}",
    "unique vegan recipes with {ingredient}",
]


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def get_current_season() -> str:
    month = date.today().month
    if month in (3, 4, 5):
        return "Frühling"
    elif month in (6, 7, 8):
        return "Sommer"
    elif month in (9, 10, 11):
        return "Herbst"
    else:
        return "Winter"


def get_season_english() -> str:
    month = date.today().month
    if month in (3, 4, 5):
        return "spring"
    elif month in (6, 7, 8):
        return "summer"
    elif month in (9, 10, 11):
        return "autumn"
    else:
        return "winter"


def get_seasonal_ingredients() -> List[str]:
    return SEASONAL_INGREDIENTS.get(date.today().month, [])


def get_existing_names() -> List[str]:
    """Alle schon bekannten Rezeptnamen um Duplikate zu vermeiden."""
    conn = get_db()
    rows = conn.execute("SELECT DISTINCT name FROM recipe_inspirations").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def get_inspiration_count() -> int:
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) as cnt FROM recipe_inspirations").fetchone()["cnt"]
    conn.close()
    return count


def _web_search(query: str, num_results: int = 5) -> List[str]:
    """Einfache Web-Suche über DuckDuckGo Lite (kein API-Key nötig)."""
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://lite.duckduckgo.com/lite/?q={encoded}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Cookflow/1.0 Recipe Research Bot"
        })
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="ignore")

        # Einfaches Parsing der Ergebnisse
        results = []
        import re
        # DuckDuckGo Lite zeigt Ergebnisse als Links
        links = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*class="result-link"', html)
        snippets = re.findall(r'<td class="result-snippet">(.*?)</td>', html, re.DOTALL)

        for i, snippet in enumerate(snippets[:num_results]):
            clean = re.sub(r'<[^>]+>', '', snippet).strip()
            if clean:
                results.append(clean)

        return results
    except Exception as e:
        logger.warning(f"Web-Suche fehlgeschlagen für '{query}': {e}")
        return []


def research_recipes(
    cuisines: Optional[List[str]] = None,
    count_per_cuisine: int = 3,
    diet_type: str = "vegan",
) -> List[Dict]:
    """Führt eine Recherche-Runde durch und gibt strukturierte Rezeptinspirationen zurück."""

    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.error("Kein ANTHROPIC_API_KEY – Recherche nicht möglich.")
        return []

    client = anthropic.Anthropic(api_key=api_key)

    selected_cuisines = cuisines or CUISINES[:6]  # 6 Küchen pro Runde
    season = get_current_season()
    season_en = get_season_english()
    month = date.today().strftime("%B")
    year = date.today().year
    seasonal_ings = get_seasonal_ingredients()
    existing_names = get_existing_names()

    # Web-Recherche für aktuelle Trends
    search_results = []
    for i, topic_template in enumerate(RESEARCH_TOPICS[:4]):
        cuisine = selected_cuisines[i % len(selected_cuisines)]
        ingredient = seasonal_ings[i % len(seasonal_ings)] if seasonal_ings else "vegetables"
        topic = topic_template.format(
            season=season_en, cuisine=cuisine.lower(),
            month=month, year=year, ingredient=ingredient
        )
        results = _web_search(topic)
        if results:
            search_results.extend(results[:3])

    # Claude: Aus Recherche + Saisonwissen Inspirationen generieren
    web_context = "\n".join(f"- {r}" for r in search_results[:15]) if search_results else "Keine Web-Ergebnisse verfügbar."

    exclude_list = "\n".join(existing_names[-50:]) if existing_names else "Keine"

    prompt = f"""Du bist ein kreativer Koch und Rezeptentwickler. Erstelle {count_per_cuisine * len(selected_cuisines)} einzigartige, inspirierende Rezeptideen.

SAISON: {season} (Monat: {month} {year})
SAISONALE ZUTATEN die eingebaut werden sollen: {', '.join(seasonal_ings)}
ERNÄHRUNG: {diet_type}
KÜCHEN: {', '.join(selected_cuisines)}

WEB-RECHERCHE (aktuelle Trends):
{web_context}

BEREITS BEKANNTE REZEPTE (NICHT wiederholen):
{exclude_list}

REGELN:
- Erstelle {count_per_cuisine} Rezepte pro Küche
- Mindestens die Hälfte der Rezepte soll saisonale Zutaten prominent verwenden
- Rezepte sollen kreativ, ungewöhnlich aber realistisch kochbar sein
- Denke an interessante Texturen, überraschende Gewürzkombinationen, clevere Twists
- Keine Standardgerichte – jedes Rezept soll einen besonderen Aspekt haben
- Kurze, prägnante Beschreibungen die Appetit machen

Antworte AUSSCHLIESSLICH mit einem JSON-Array:
[{{
  "name": "Rezeptname",
  "description": "Appetitliche 1-2 Satz Beschreibung",
  "cuisine": "Küche",
  "season": "{season}",
  "tags": ["tag1", "tag2"],
  "key_ingredients": ["Zutat1", "Zutat2", "Zutat3"],
  "why_special": "Was macht dieses Rezept besonders?"
}}]"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()

        # JSON extrahieren
        import re
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if not match:
            logger.error("Keine JSON-Antwort von Claude bei Recherche")
            return []

        recipes = json.loads(match.group(0))
        logger.info(f"Recherche: {len(recipes)} Inspirationen generiert")
        return recipes

    except Exception as e:
        logger.error(f"Recherche fehlgeschlagen: {e}")
        return []


def save_inspirations(recipes: List[Dict]) -> int:
    """Speichert neue Inspirationen in die Datenbank. Gibt Anzahl gespeicherter zurück."""
    conn = get_db()
    existing = set(r["name"] for r in conn.execute("SELECT name FROM recipe_inspirations").fetchall())
    saved = 0

    for r in recipes:
        name = r.get("name", "").strip()
        if not name or name in existing:
            continue

        conn.execute("""
            INSERT INTO recipe_inspirations (name, description, cuisine, season, tags, ingredients, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            r.get("description", ""),
            r.get("cuisine", ""),
            r.get("season", get_current_season()),
            json.dumps(r.get("tags", []), ensure_ascii=False),
            json.dumps(r.get("key_ingredients", []), ensure_ascii=False),
            r.get("why_special", ""),
        ))
        existing.add(name)
        saved += 1

    conn.commit()
    conn.close()
    logger.info(f"Recherche: {saved} neue Inspirationen gespeichert (Gesamt: {len(existing)})")
    return saved


def run_research_round(cuisines: Optional[List[str]] = None, diet_type: str = "vegan") -> Dict:
    """Führt eine komplette Recherche-Runde durch: Suche → Claude → Speichern."""
    logger.info(f"Starte Recherche-Runde: Saison={get_current_season()}, Küchen={cuisines}")

    recipes = research_recipes(cuisines=cuisines, diet_type=diet_type)
    if not recipes:
        return {"ok": False, "message": "Keine Rezepte gefunden", "saved": 0}

    saved = save_inspirations(recipes)
    total = get_inspiration_count()

    return {
        "ok": True,
        "found": len(recipes),
        "saved": saved,
        "total_in_db": total,
        "season": get_current_season(),
        "message": f"{saved} neue Inspirationen gespeichert (Gesamt: {total})",
    }


def get_relevant_inspirations(
    cuisines: Optional[List[str]] = None,
    season: Optional[str] = None,
    limit: int = 20,
) -> List[Dict]:
    """Holt relevante Inspirationen aus der DB für die Plan-Generierung."""
    conn = get_db()

    query = "SELECT * FROM recipe_inspirations WHERE 1=1"
    params = []

    if cuisines:
        placeholders = ",".join("?" * len(cuisines))
        query += f" AND cuisine IN ({placeholders})"
        params.extend(cuisines)

    if season:
        query += " AND (season = ? OR season = '')"
        params.append(season)

    # Zufällige Auswahl + qualitativ hochwertige bevorzugen
    query += " ORDER BY quality DESC, RANDOM() LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [dict(r) for r in rows]
