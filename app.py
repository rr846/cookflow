"""
Easy Cook – FastAPI Backend (Neuaufbau)
"""

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from typing import List, Dict, Any

import sqlite3
import json
import os
import uuid
from pathlib import Path
from datetime import date, timedelta
from dotenv import load_dotenv
import anthropic
import image_service

load_dotenv()

# ─────────────────────────────────────────────
# System-Prompt
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """
Du bist ein Profi-Koch und Ernährungswissenschaftler.
Deine Aufgabe ist es, alltagstaugliche, geschmacklich starke und ernährungsphysiologisch
sinnvolle Rezepte und Gerichtsempfehlungen zu entwickeln. Du kombinierst kulinarische
Qualität mit fundierter, nüchterner Ernährungskompetenz.

Zentrale Präferenzachsen:
- schnell vs raffiniert
- gesund vs cheat

Bedeutung der Präferenzachsen:
- schnell: kurze Kochzeit, wenige Schritte, wenig Aufwand, einfache Techniken
- raffiniert: mehr Tiefe, bessere Textur, feinere Aromatik, etwas mehr handwerklicher Anspruch
- gesund: hohe Nährstoffdichte, gute Sättigung, ausgewogene Makros, eher leichte Gerichte
- cheat: emotional, indulgent, besonders lecker, mehr Komfort- und Genussfaktor

Grundregeln:
- Denke immer in vollständigen, realistisch kochbaren Mahlzeiten
- Priorisiere Geschmack, Umsetzbarkeit, Nährwert und Einfachheit
- Vermeide unlogische Zutatenkombinationen und unnötig exotische Zutaten
- Bevorzuge Zutaten, die in typischen Supermärkten gut verfügbar sind
- Achte auf klare Mengen, sinnvolle Portionierung und realistische Zubereitung
- Vermeide Hype, Mythen und extreme Ernährungsdogmen

Kulinarische Qualitätsregel:
Rezepte sollen durch besondere Details überzeugen – ein cleverer Twist, eine spannende
Würzung, ein gutes Topping, eine besondere Textur. Der Charakter des Gerichts soll
erkennbar sein, ohne es unnötig kompliziert zu machen.

Wichtige Systemregel:
Nutze standardisierte, eindeutig benannte Zutaten. Vermeide Duplikate in Zutatenlisten.
Formuliere Mengen klar und konsistent. Bevorzuge gut verfügbare Produkte.
Denke bei mehreren Gerichten an sinnvolle Zutatenüberschneidungen.

Stil: sachlich, klar, kompakt, kompetent, nicht belehrend, keine langen Einleitungen.
"""

# ─────────────────────────────────────────────
# Präferenz-Labels
# ─────────────────────────────────────────────

SPEED_LABELS  = {1: "sehr schnell", 2: "schnell", 3: "ausgewogen",
                 4: "raffiniert",   5: "deutlich raffiniert"}
HEALTH_LABELS = {1: "sehr gesund",  2: "gesund",  3: "ausgewogen",
                 4: "comfort",      5: "deutlich comfort"}
DIET_LABELS   = {
    "alles":        "",
    "vegetarisch":  "vegetarische",
    "vegan":        "vollständig vegane",
    "pescetarisch": "pescetarische",
    "flexitarisch": "flexitarische (überwiegend pflanzliche)",
}

# ─────────────────────────────────────────────
# Datenbank
# ─────────────────────────────────────────────

DB_PATH = "meal_planner.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id TEXT DEFAULT 'default',
            key     TEXT NOT NULL,
            value   TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS weekly_plans (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    TEXT    DEFAULT 'default',
            week_start TEXT    NOT NULL,
            persons    INTEGER DEFAULT 2,
            status     TEXT    DEFAULT 'pending',
            created_at TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id     INTEGER NOT NULL,
            name        TEXT    NOT NULL,
            description TEXT,
            cuisine     TEXT    DEFAULT '',
            prep_time   INTEGER DEFAULT 0,
            cook_time   INTEGER DEFAULT 0,
            servings    INTEGER DEFAULT 2,
            ingredients TEXT    DEFAULT '[]',
            steps       TEXT    DEFAULT '[]',
            status          TEXT    DEFAULT 'pending',
            favorite        INTEGER DEFAULT 0,
            estimated_total REAL    DEFAULT 0,
            nutrition       TEXT    DEFAULT '{}',
            created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (plan_id) REFERENCES weekly_plans(id)
        )
    """)

    # Rezept-Archiv – alle jemals generierten Rezepte für schnellen Abruf
    c.execute("""
        CREATE TABLE IF NOT EXISTS recipe_archive (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            original_id     INTEGER,
            name            TEXT    NOT NULL,
            description     TEXT,
            cuisine         TEXT    DEFAULT '',
            prep_time       INTEGER DEFAULT 0,
            cook_time       INTEGER DEFAULT 0,
            servings        INTEGER DEFAULT 2,
            ingredients     TEXT    DEFAULT '[]',
            steps           TEXT    DEFAULT '[]',
            estimated_total REAL    DEFAULT 0,
            nutrition       TEXT    DEFAULT '{}',
            image_filename  TEXT    DEFAULT '',
            archived_at     TEXT    DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrationen
    for col, coltype, default in [
        ("favorite", "INTEGER", "0"),
        ("estimated_total", "REAL", "0"),
        ("nutrition", "TEXT", "'{}'"),
        ("day", "TEXT", "''"),
    ]:
        try:
            c.execute(f"ALTER TABLE recipes ADD COLUMN {col} {coltype} DEFAULT {default}")
            conn.commit()
        except Exception:
            pass

    # Migration: user_id zu weekly_plans hinzufügen
    try:
        c.execute("ALTER TABLE weekly_plans ADD COLUMN user_id TEXT DEFAULT 'default'")
        conn.commit()
    except Exception:
        pass

    conn.commit()
    conn.close()


# ─────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────

def current_week_start() -> str:
    today = date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def row_to_recipe(r) -> Dict[str, Any]:
    return {
        "id":          r["id"],
        "name":        r["name"],
        "description": r["description"] or "",
        "cuisine":     r["cuisine"]     or "",
        "prep_time":   r["prep_time"],
        "cook_time":   r["cook_time"],
        "servings":    r["servings"],
        "ingredients": json.loads(r["ingredients"]),
        "steps":       json.loads(r["steps"]),
        "status":         r["status"],
        "favorite":       bool(r["favorite"]) if "favorite" in r.keys() else False,
        "estimated_total": r["estimated_total"] if "estimated_total" in r.keys() else 0,
        "nutrition":      json.loads(r["nutrition"]) if "nutrition" in r.keys() and r["nutrition"] else {},
        "day":            r["day"] if "day" in r.keys() else "",
    }


def get_setting(key: str, default: str = "", user_id: str = "default") -> str:
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE user_id=? AND key=?", (user_id, key)).fetchone()
    conn.close()
    return row["value"] if row else default


# ─────────────────────────────────────────────
# Claude – Rezeptgenerierung
# ─────────────────────────────────────────────

def generate_with_claude(
    persons:               int,
    exclude_names:         list = [],
    count:                 int = 4,
    preferred_cuisines:    list = [],
    preferred_ingredients: list = [],
    speed_refinement:      int = 3,
    health_comfort:        int = 3,
    diet_type:             str = "alles",
    allergies:             list = [],
    nutrition_focus:       list = [],
    pantry_items:          list = [],
) -> list:

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY fehlt in .env")

    client = anthropic.Anthropic(api_key=api_key)

    speed_text  = SPEED_LABELS.get(speed_refinement, "ausgewogen")
    health_text = HEALTH_LABELS.get(health_comfort,  "ausgewogen")

    # Alle jemals generierten Rezepte ausschließen für maximale Varianz
    conn_hist = get_db()
    all_past = [r["name"] for r in conn_hist.execute("SELECT DISTINCT name FROM recipes").fetchall()]
    conn_hist.close()
    all_excluded = list(set(exclude_names + all_past))

    exclusion_block = ""
    if all_excluded:
        exclusion_block = f"\n⚠️  Diese Rezepte NIEMALS verwenden (bereits bekannt): {', '.join(all_excluded)}"

    cuisine_block = ""

    ingredient_block = ""
    if preferred_ingredients:
        ingredient_block = (
            f"\n🥬 Gewünschte Zutaten: {', '.join(preferred_ingredients)}. "
            "Diese Zutaten sollen wenn möglich in den Rezepten vorkommen."
        )

    pantry_block = ""
    if pantry_items:
        pantry_block = (
            f"\n🏠 Im Kühlschrank vorhanden: {', '.join(pantry_items)}. "
            "WICHTIG: Baue diese Zutaten bevorzugt in die Rezepte ein, damit sie verbraucht werden. "
            "Mindestens die Hälfte der Rezepte sollte eine oder mehrere dieser Zutaten verwenden."
        )

    allergy_block = ""
    if allergies:
        allergy_block = (
            f"\n🚫 Allergien/Unverträglichkeiten: {', '.join(allergies)}. "
            "Diese Zutaten und Produkte, die diese enthalten, MÜSSEN vollständig vermieden werden."
        )

    nutrition_block = ""
    if nutrition_focus:
        focus_map = {
            "protein": "Proteinreich (mind. 25g Protein pro Portion)",
            "low_carb": "Kohlenhydratarm (max. 30g Kohlenhydrate pro Portion)",
            "fiber": "Ballaststoffreich (mind. 10g Ballaststoffe pro Portion)",
            "vitamins": "Vitaminreich (viel frisches Gemüse, Obst, Kräuter)",
        }
        focus_texts = [focus_map.get(f, f) for f in nutrition_focus]
        nutrition_block = (
            f"\n🎯 Nährwert-Fokus: {', '.join(focus_texts)}. "
            "Die Rezepte sollen diese Nährwertziele besonders berücksichtigen."
        )

    diet_text = DIET_LABELS.get(diet_type, "")
    diet_clause = f" {diet_text}" if diet_text else ""

    import random

    # Pool an Gerichtstypen – pro Generierung werden zufällige gewählt
    DISH_TYPES = [
        "Suppe/Eintopf", "Salat (sättigend)", "Pfannengericht", "Ofengericht",
        "Bowl", "Wrap/Taco/Burrito", "Auflauf/Gratin", "Risotto/Pilaw",
        "Nudeln/Pasta", "Gefülltes Gemüse", "Flammkuchen/Pizza/Tarte",
        "Curry", "Stir-Fry/Wok", "Burger/Sandwich", "Rösti/Puffer/Fritter",
        "Gnocchi/Knödel/Dumpling", "Shakshuka/Eiergericht", "Quiche/Frittata",
        "Schmortopf", "Ramen/Pho/Nudelsuppe", "Tacos/Quesadilla",
        "Falafel/Frittiertes", "Polenta-Gericht", "Lasagne/Cannelloni",
        "Sushi/Onigiri", "Galette/Crêpe", "Dal/Linsengericht",
        "Bruschetta/Crostini-Platte", "Empanadas/Teigtaschen",
    ]

    # Zufällige Küchen für maximale Länder-Varianz
    ALL_CUISINES = [
        "Italienisch", "Japanisch", "Mexikanisch", "Indisch", "Thai",
        "Koreanisch", "Griechisch", "Türkisch", "Marokkanisch", "Peruanisch",
        "Vietnamesisch", "Spanisch", "Libanesisch", "Äthiopisch", "Georgisch",
        "Karibisch", "Französisch", "Indonesisch", "Brasilianisch", "Deutsch",
        "Ungarisch", "Israelisch", "Persisch", "Skandinavisch", "Chinesisch",
    ]

    # Küchen-Auswahl: Nutzer-Präferenzen haben Vorrang
    if preferred_cuisines:
        # Alle Rezepte aus den bevorzugten Küchen, aber jede Küche max. 1x
        pool = preferred_cuisines[:]
        random.shuffle(pool)
        chosen_cuisines = []
        for i in range(count):
            chosen_cuisines.append(pool[i % len(pool)])
        random.shuffle(chosen_cuisines)
    else:
        # Keine Präferenz → zufällig aus dem gesamten Pool
        chosen_cuisines = random.sample(ALL_CUISINES, min(count, len(ALL_CUISINES)))

    chosen_types = random.sample(DISH_TYPES, min(count, len(DISH_TYPES)))
    assignments = "\n".join(
        f"- Rezept {i+1}: {chosen_cuisines[i]} / {chosen_types[i]}"
        for i in range(count)
    )

    prompt = f"""Erstelle exakt {count}{diet_clause} Rezepte für {persons} Personen.

🎲 PFLICHT-VORGABEN für jedes Rezept (Küche + Gerichtstyp):
{assignments}

Halte dich STRIKT an diese Zuordnungen. Jedes Rezept muss zur angegebenen Küche und zum Gerichtstyp passen.

⚙️ Präferenzen:
- Aufwand/Raffinesse: {speed_text} ({speed_refinement}/5)
- Gesundheit/Comfort: {health_text} ({health_comfort}/5)
{exclusion_block}{cuisine_block}{ingredient_block}{pantry_block}{allergy_block}{nutrition_block}

Wochentag-Zuordnung:
Verteile die Rezepte auf die Wochentage Montag bis Sonntag – genau ein Rezept pro Tag.
Gib für jedes Rezept den Wochentag im Feld "day" an.

Antworte AUSSCHLIESSLICH mit einem JSON-Array – kein Text davor oder danach.
Format:
[
  {{
    "name": "Kreativer Rezeptname",
    "cuisine": "Küche",
    "day": "Montag",
    "description": "Appetitanregende Beschreibung in 2–3 Sätzen.",
    "prep_time": 15,
    "cook_time": 25,
    "total_time": 40,
    "servings": {persons},
    "ingredients": [
      {{"name": "Zutat", "quantity": 400, "unit": "g", "estimated_price": 1.29}}
    ],
    "estimated_total": 8.50,
    "nutrition": {{
      "calories": 520,
      "protein": 18,
      "carbs": 62,
      "fat": 22,
      "fiber": 9
    }},
    "steps": [
      "Schritt 1.",
      "Schritt 2."
    ]
  }}
]

Erlaubte Einheiten: g, ml, EL, TL, Stück, Prise, Bund, Dose, Packung, Zehe, Scheibe

Preisschätzung: Schätze für jede Zutat einen realistischen Preis (estimated_price in €) basierend auf aktuellen deutschen Supermarktpreisen (REWE, EDEKA, Lidl). Berechne estimated_total als Summe aller Zutatenpreise pro Rezept. Preise als Dezimalzahl mit 2 Nachkommastellen.

Nährwerte: Schätze die Nährwerte PRO PORTION (nutrition-Objekt) realistisch: calories (kcal), protein (g), carbs (g), fat (g), fiber (g). Alle als ganze Zahlen.

WICHTIG: Führe NIEMALS Wasser, Salz, Pfeffer oder Öl zum Braten als eigene Zutaten auf. Diese sind in jedem Haushalt vorhanden. Nur spezielle Öle (Sesamöl, Trüffelöl etc.) oder spezielle Salze (Fleur de Sel etc.) sind erlaubt."""

    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        temperature=1.0,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )

    text = msg.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]

    return json.loads(text.strip())





# ─────────────────────────────────────────────
# FastAPI App
# ─────────────────────────────────────────────

app = FastAPI(title="Easy Cook")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


# ─────────────────────────────────────────────
# Cookie-basierte User-ID
# ─────────────────────────────────────────────

COOKIE_NAME = "cookflow_uid"
COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 Jahr


class UserCookieMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        uid = request.cookies.get(COOKIE_NAME)
        new_cookie = False
        if not uid:
            uid = str(uuid.uuid4())[:12]
            new_cookie = True
        request.state.user_id = uid
        response = await call_next(request)
        if new_cookie:
            response.set_cookie(
                COOKIE_NAME, uid,
                max_age=COOKIE_MAX_AGE,
                httponly=False,
                samesite="lax",
            )
        return response


app.add_middleware(UserCookieMiddleware)


def get_uid(request: Request) -> str:
    return getattr(request.state, "user_id", "default")


# ─────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────

@app.get("/api/settings")
def api_get_settings(request: Request):
    uid = get_uid(request)
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings WHERE user_id=?", (uid,)).fetchall()
    if not rows:
        # Neue User: Defaults einfügen
        defaults = {
            "persons": "2", "preferred_cuisines": "[]", "preferred_ingredients": "[]",
            "speed_refinement": "3", "health_comfort": "3", "diet_type": "alles",
            "allergies": "[]", "onboarding_complete": "false", "nutrition_focus": "[]",
            "pantry_items": "[]",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings (user_id, key, value) VALUES (?, ?, ?)", (uid, k, v))
        conn.commit()
        rows = conn.execute("SELECT key, value FROM settings WHERE user_id=?", (uid,)).fetchall()
    conn.close()
    data = {r["key"]: r["value"] for r in rows}
    return data


@app.put("/api/settings")
def api_update_settings(request: Request, payload: Dict[str, Any]):
    uid = get_uid(request)
    allowed = {
        "persons", "preferred_cuisines", "preferred_ingredients",
        "speed_refinement", "health_comfort",
        "diet_type", "allergies", "onboarding_complete", "nutrition_focus", "pantry_items",
    }
    conn = get_db()
    c = conn.cursor()
    for key, val in payload.items():
        if key in allowed:
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
            c.execute(
                "INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, ?, ?)",
                (uid, key, str(val))
            )
    conn.commit()
    conn.close()
    return {"ok": True}


# ─────────────────────────────────────────────
# Wochenplan & Rezepte
# ─────────────────────────────────────────────

@app.get("/api/plan")
def api_get_plan(request: Request):
    uid = get_uid(request)
    week_start = current_week_start()
    conn = get_db()
    plan = conn.execute(
        "SELECT * FROM weekly_plans WHERE week_start=? AND user_id=?", (week_start, uid)
    ).fetchone()

    if not plan:
        conn.close()
        return {"plan": None, "recipes": []}

    recipes = conn.execute(
        "SELECT * FROM recipes WHERE plan_id=? ORDER BY id", (plan["id"],)
    ).fetchall()
    conn.close()

    result = {
        "plan": {
            "id":         plan["id"],
            "week_start": plan["week_start"],
            "persons":    plan["persons"],
            "status":     plan["status"],
        },
        "recipes": [row_to_recipe(r) for r in recipes],
    }

    _diet = get_setting("diet_type", "alles", uid)
    image_service.trigger_image_generation([dict(r) for r in recipes], diet_type=_diet)

    return result


@app.post("/api/plan/generate")
def api_generate_plan(request: Request):
    uid = get_uid(request)
    week_start = current_week_start()
    conn = get_db()
    c = conn.cursor()

    persons           = int(get_setting("persons", "2", uid))
    preferred_cuisines = json.loads(get_setting("preferred_cuisines",    "[]", uid))
    preferred_ings     = json.loads(get_setting("preferred_ingredients", "[]", uid))
    speed_refinement   = int(get_setting("speed_refinement", "3", uid))
    health_comfort     = int(get_setting("health_comfort",   "3", uid))
    diet_type          = get_setting("diet_type", "alles", uid)
    allergies          = json.loads(get_setting("allergies", "[]", uid))
    nutrition_focus     = json.loads(get_setting("nutrition_focus", "[]", uid))
    pantry_items        = json.loads(get_setting("pantry_items", "[]", uid))

    existing = c.execute(
        "SELECT * FROM weekly_plans WHERE week_start=? AND user_id=?", (week_start, uid)
    ).fetchone()

    if existing:
        plan_id = existing["id"]
        confirmed_ids = [r["id"] for r in c.execute(
            "SELECT id FROM recipes WHERE plan_id=? AND status='confirmed'", (plan_id,)
        ).fetchall()]

        # Alle nicht-bestätigten Rezepte ins Archiv kopieren
        old_recipes = c.execute(
            "SELECT * FROM recipes WHERE plan_id=? AND status!='confirmed'", (plan_id,)
        ).fetchall()
        for old_r in old_recipes:
            # Bild-Dateiname für Archiv-Zuordnung
            img_file = ""
            img_path = image_service.image_path(old_r["id"])
            if img_path.exists():
                img_file = f"{old_r['id']}.jpg"
            c.execute("""
                INSERT INTO recipe_archive
                    (original_id, name, description, cuisine, prep_time, cook_time,
                     servings, ingredients, steps, estimated_total, nutrition, image_filename)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                old_r["id"], old_r["name"], old_r["description"], old_r["cuisine"],
                old_r["prep_time"], old_r["cook_time"], old_r["servings"],
                old_r["ingredients"], old_r["steps"],
                old_r["estimated_total"] if "estimated_total" in old_r.keys() else 0,
                old_r["nutrition"] if "nutrition" in old_r.keys() else "{}",
                img_file,
            ))

        c.execute("DELETE FROM recipes WHERE plan_id=? AND status!='confirmed'", (plan_id,))
        conn.commit()
        # Alte Bilder ins Archiv verschieben (bestätigte behalten)
        image_service.archive_current_images(keep_ids=confirmed_ids)
        confirmed_count = c.execute(
            "SELECT COUNT(*) as cnt FROM recipes WHERE plan_id=? AND status='confirmed'",
            (plan_id,)
        ).fetchone()["cnt"]
        existing_names = [r["name"] for r in c.execute(
            "SELECT name FROM recipes WHERE plan_id=?", (plan_id,)
        ).fetchall()]
    else:
        c.execute(
            "INSERT INTO weekly_plans (user_id, week_start, persons) VALUES (?, ?, ?)",
            (uid, week_start, persons)
        )
        plan_id = c.lastrowid
        conn.commit()
        confirmed_count = 0
        existing_names  = []

    conn.close()

    need = 7 - confirmed_count
    if need <= 0:
        return api_get_plan(request)

    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        # Aufteilen in 2 parallele Batches
        batch_a = need // 2
        batch_b = need - batch_a
        common = dict(
            persons=persons, exclude_names=existing_names,
            preferred_cuisines=preferred_cuisines,
            preferred_ingredients=preferred_ings,
            speed_refinement=speed_refinement,
            health_comfort=health_comfort,
            diet_type=diet_type,
            allergies=allergies,
            nutrition_focus=nutrition_focus,
            pantry_items=pantry_items,
        )

        new_recipes = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(generate_with_claude, count=batch_a, **common),
                pool.submit(generate_with_claude, count=batch_b, **common),
            ]
            for f in as_completed(futures):
                new_recipes.extend(f.result())
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude-Fehler: {e}")

    conn = get_db()
    c = conn.cursor()
    for r in new_recipes:
        c.execute("""
            INSERT INTO recipes
                (plan_id, name, cuisine, description, prep_time, cook_time, servings, ingredients, steps, estimated_total, nutrition, day)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            plan_id,
            r["name"],
            r.get("cuisine", ""),
            r.get("description", ""),
            r.get("prep_time", 0),
            r.get("cook_time", 0),
            r.get("servings", persons),
            json.dumps(r.get("ingredients", []), ensure_ascii=False),
            json.dumps(r.get("steps",        []), ensure_ascii=False),
            r.get("estimated_total", 0),
            json.dumps(r.get("nutrition", {}), ensure_ascii=False),
            r.get("day", ""),
        ))
    conn.commit()

    # Gespeicherte Rezepte mit IDs laden für Bild-Download
    saved = conn.execute(
        "SELECT id, name, cuisine FROM recipes WHERE plan_id=?", (plan_id,)
    ).fetchall()
    conn.close()

    # Bilder im Hintergrund herunterladen
    image_service.trigger_image_generation([dict(r) for r in saved], diet_type=diet_type)

    return api_get_plan(request)


@app.post("/api/recipes/{recipe_id}/confirm")
def api_confirm_recipe(recipe_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE recipes SET status='confirmed' WHERE id=?", (recipe_id,))
    r = c.execute("SELECT plan_id FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if r:
        all_s = [row["status"] for row in c.execute(
            "SELECT status FROM recipes WHERE plan_id=?", (r["plan_id"],)
        ).fetchall()]
        if all(s == "confirmed" for s in all_s):
            c.execute("UPDATE weekly_plans SET status='confirmed' WHERE id=?", (r["plan_id"],))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/recipes/{recipe_id}/unconfirm")
def api_unconfirm_recipe(recipe_id: int):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE recipes SET status='pending' WHERE id=?", (recipe_id,))
    r = c.execute("SELECT plan_id FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if r:
        c.execute("UPDATE weekly_plans SET status='pending' WHERE id=?", (r["plan_id"],))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/recipes/{recipe_id}/favorite")
def api_toggle_favorite(recipe_id: int):
    conn = get_db()
    recipe = conn.execute("SELECT favorite FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if not recipe:
        conn.close()
        raise HTTPException(status_code=404, detail="Rezept nicht gefunden")
    new_val = 0 if recipe["favorite"] else 1
    conn.execute("UPDATE recipes SET favorite=? WHERE id=?", (new_val, recipe_id))
    conn.commit()
    conn.close()
    return {"ok": True, "favorite": bool(new_val)}


@app.post("/api/reset")
def api_reset(request: Request):
    """Setzt Onboarding + alle Präferenzen zurück und löscht den aktuellen Plan."""
    uid = get_uid(request)
    conn = get_db()
    defaults = {
        "persons": "2",
        "preferred_cuisines": "[]",
        "preferred_ingredients": "[]",
        "speed_refinement": "3",
        "health_comfort": "3",
        "diet_type": "alles",
        "allergies": "[]",
        "nutrition_focus": "[]",
        "onboarding_complete": "false",
    }
    for key, value in defaults.items():
        conn.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, ?, ?)", (uid, key, value))
    week_start = current_week_start()
    plan = conn.execute("SELECT id FROM weekly_plans WHERE week_start=? AND user_id=?", (week_start, uid)).fetchone()
    if plan:
        fav_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM recipes WHERE plan_id=? AND favorite=1", (plan["id"],)
        ).fetchall()]
        # Nicht-favorisierte Rezepte ins Archiv kopieren
        old_recipes = conn.execute(
            "SELECT * FROM recipes WHERE plan_id=? AND favorite=0", (plan["id"],)
        ).fetchall()
        for old_r in old_recipes:
            img_file = f"{old_r['id']}.jpg" if image_service.image_path(old_r["id"]).exists() else ""
            conn.execute("""
                INSERT INTO recipe_archive
                    (original_id, name, description, cuisine, prep_time, cook_time,
                     servings, ingredients, steps, estimated_total, nutrition, image_filename)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                old_r["id"], old_r["name"], old_r["description"], old_r["cuisine"],
                old_r["prep_time"], old_r["cook_time"], old_r["servings"],
                old_r["ingredients"], old_r["steps"],
                old_r["estimated_total"] if "estimated_total" in old_r.keys() else 0,
                old_r["nutrition"] if "nutrition" in old_r.keys() else "{}",
                img_file,
            ))
        # Bilder archivieren, Rezepte löschen
        image_service.archive_current_images(keep_ids=fav_ids)
        conn.execute("DELETE FROM recipes WHERE plan_id=? AND favorite=0", (plan["id"],))
        conn.execute("DELETE FROM weekly_plans WHERE id=?", (plan["id"],))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/favorites")
@app.get("/api/archive")
def api_get_archive():
    """Gibt alle archivierten Rezepte zurück."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM recipe_archive ORDER BY archived_at DESC"
    ).fetchall()
    conn.close()
    return {"recipes": [{
        "id":              r["id"],
        "original_id":     r["original_id"],
        "name":            r["name"],
        "description":     r["description"] or "",
        "cuisine":         r["cuisine"] or "",
        "prep_time":       r["prep_time"],
        "cook_time":       r["cook_time"],
        "servings":        r["servings"],
        "ingredients":     json.loads(r["ingredients"]),
        "steps":           json.loads(r["steps"]),
        "estimated_total": r["estimated_total"],
        "nutrition":       json.loads(r["nutrition"]) if r["nutrition"] else {},
        "image_filename":  r["image_filename"],
        "archived_at":     r["archived_at"],
    } for r in rows]}


@app.get("/api/favorites")
def api_get_favorites(request: Request):
    uid = get_uid(request)
    conn = get_db()
    rows = conn.execute("""
        SELECT r.* FROM recipes r
        JOIN weekly_plans wp ON r.plan_id = wp.id
        WHERE r.favorite=1 AND wp.user_id=?
        ORDER BY r.created_at DESC
    """, (uid,)).fetchall()
    conn.close()
    return {"recipes": [dict(r) for r in rows]}


@app.post("/api/recipes/{recipe_id}/regenerate")
def api_regenerate_recipe(request: Request, recipe_id: int):
    uid = get_uid(request)
    conn = get_db()
    c = conn.cursor()
    recipe = c.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    if not recipe:
        conn.close()
        raise HTTPException(status_code=404, detail="Rezept nicht gefunden")

    plan = c.execute("SELECT * FROM weekly_plans WHERE id=?", (recipe["plan_id"],)).fetchone()
    other_names = [r["name"] for r in c.execute(
        "SELECT name FROM recipes WHERE plan_id=? AND id!=?",
        (recipe["plan_id"], recipe_id)
    ).fetchall()]
    conn.close()

    preferred_cuisines = json.loads(get_setting("preferred_cuisines",    "[]", uid))
    preferred_ings     = json.loads(get_setting("preferred_ingredients", "[]", uid))
    speed_refinement   = int(get_setting("speed_refinement", "3", uid))
    health_comfort     = int(get_setting("health_comfort",   "3", uid))
    diet_type          = get_setting("diet_type", "alles", uid)
    allergies          = json.loads(get_setting("allergies", "[]", uid))

    try:
        nr = generate_with_claude(
            plan["persons"], other_names, count=1,
            preferred_cuisines=preferred_cuisines,
            preferred_ingredients=preferred_ings,
            speed_refinement=speed_refinement,
            health_comfort=health_comfort,
            diet_type=diet_type,
            allergies=allergies,
        )[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Claude-Fehler: {e}")

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        UPDATE recipes
        SET name=?, cuisine=?, description=?, prep_time=?, cook_time=?,
            servings=?, ingredients=?, steps=?, status='pending'
        WHERE id=?
    """, (
        nr["name"], nr.get("cuisine", ""), nr.get("description", ""),
        nr.get("prep_time", 0), nr.get("cook_time", 0),
        nr.get("servings", plan["persons"]),
        json.dumps(nr.get("ingredients", []), ensure_ascii=False),
        json.dumps(nr.get("steps",        []), ensure_ascii=False),
        recipe_id,
    ))
    conn.commit()
    conn.close()
    return api_get_plan(request)


# ─────────────────────────────────────────────
# Zutaten aggregieren
# ─────────────────────────────────────────────

@app.get("/api/plan/ingredients")
def api_get_ingredients(request: Request):
    uid = get_uid(request)
    week_start = current_week_start()
    conn = get_db()
    plan = conn.execute(
        "SELECT * FROM weekly_plans WHERE week_start=? AND user_id=?", (week_start, uid)
    ).fetchone()
    if not plan:
        conn.close()
        return {"ingredients": []}

    recipes = conn.execute(
        "SELECT ingredients FROM recipes WHERE plan_id=? AND status='confirmed'",
        (plan["id"],)
    ).fetchall()
    conn.close()

    agg: Dict[str, dict] = {}
    for rec in recipes:
        for ing in json.loads(rec["ingredients"]):
            key = f"{ing['name'].strip().lower()}||{ing['unit']}"
            if key in agg:
                agg[key]["quantity"] += ing["quantity"]
                agg[key]["estimated_price"] = round(
                    agg[key].get("estimated_price", 0) + ing.get("estimated_price", 0), 2
                )
            else:
                agg[key] = dict(ing)

    return {"ingredients": list(agg.values())}




# ─────────────────────────────────────────────
# Rezeptbilder – Caching + Replicate
# ─────────────────────────────────────────────

@app.get("/api/images/available")
def api_available_images():
    """Gibt aktuelle + Archiv-Bilder zurück."""
    # Aktuelle Bilder (für Rezeptkarten + Hero)
    current_ids = image_service.get_all_image_ids()
    # Archiv-Pfade (für Ladekreis)
    archive_paths = [f"/api/images/archive/{p.name}" for p in image_service.get_archive_image_paths()]
    return {"ids": current_ids, "archive": archive_paths}


@app.get("/api/images/archive/{filename}")
def api_get_archive_image(filename: str):
    """Liefert ein archiviertes Bild."""
    path = image_service.ARCHIVE_DIR / filename
    if path.exists():
        return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(status_code=404)


@app.get("/api/images/{recipe_id}.jpg")
def api_get_image(recipe_id: int):
    """Liefert ein gecachtes Rezeptbild oder 404 als Fallback."""
    path = image_service.image_path(recipe_id)
    if path.exists():
        return FileResponse(path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Bild noch nicht verfügbar")


@app.post("/api/images/{recipe_id}/generate")
def api_generate_image(recipe_id: int):
    """On-Demand Bildgenerierung – fügt zur Queue hinzu statt sofort zu starten."""
    if image_service.image_exists(recipe_id):
        return {"ok": True, "cached": True}

    # Rezept aus DB laden und zur Queue hinzufügen
    conn = get_db()
    recipe = conn.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
    conn.close()
    if not recipe:
        raise HTTPException(status_code=404, detail="Rezept nicht gefunden")

    ingredients = json.loads(recipe["ingredients"]) if recipe["ingredients"] else []

    image_service.enqueue({
        "id": recipe_id,
        "name": recipe["name"],
        "cuisine": recipe["cuisine"] or "",
        "description": recipe["description"] or "",
        "ingredients": ingredients,
        "diet_type": get_setting("diet_type", "alles"),
    })
    return {"ok": True, "cached": False, "message": "In Warteschlange"}


# ─────────────────────────────────────────────
# REWE-Integration (Pepesto Oneshot API)
# ─────────────────────────────────────────────

import urllib.request


def _get_pepesto_key() -> str:
    load_dotenv()
    return os.getenv("PEPESTO_API_KEY", "").strip()


@app.post("/api/rewe/checkout")
def api_rewe_checkout(payload: Dict[str, Any]):
    """Erstellt einen REWE-Warenkorb via Pepesto Oneshot.

    Sendet die Zutatenliste als Text → bekommt redirect_url zurück.
    User klickt den Link → landet im REWE-Checkout mit allen Produkten.
    """
    ingredients = payload.get("ingredients", [])
    pepesto_key = _get_pepesto_key()

    if not pepesto_key:
        raise HTTPException(status_code=400, detail="PEPESTO_API_KEY nicht konfiguriert.")

    if not ingredients:
        raise HTTPException(status_code=400, detail="Keine Zutaten ausgewählt.")

    # Zutatenliste als Text zusammenbauen
    shopping_text = "\n".join(
        f"{ing.get('quantity', '')} {ing.get('unit', '')} {ing.get('name', '')}".strip()
        for ing in ingredients
        if ing.get("name")
    )

    try:
        request_data = json.dumps({
            "content_text": shopping_text,
            "supermarket_domain": "shop.rewe.de",
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://s.pepesto.com/api/oneshot",
            data=request_data,
            headers={
                "Authorization": f"Bearer {pepesto_key}",
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read().decode("utf-8"))

        redirect_url = data.get("redirect_url", "")
        if redirect_url:
            return {
                "ok": True,
                "redirect_url": redirect_url,
                "message": "Warenkorb erstellt! Klicke den Link um bei REWE auszuchecken.",
            }
        else:
            return {
                "ok": False,
                "error": "Kein Redirect-Link erhalten.",
                "debug": str(data)[:200],
            }

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise HTTPException(status_code=e.code, detail=f"Pepesto Fehler: {body}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"REWE-Checkout fehlgeschlagen: {e}")


# ─────────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────────

app.mount("/", StaticFiles(directory="frontend", html=True), name="static")

init_db()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
