"""
Cookflow – Image Service
Generiert und cacht Rezeptbilder über Replicate (SDXL).
Fallback auf Gradient-Placeholder im Frontend.
"""

import hashlib
import logging
import os
import threading
import time
import urllib.request
import json
from pathlib import Path
from queue import Queue
from typing import Optional

logger = logging.getLogger("cookflow.images")

# ─────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────

IMAGES_DIR = Path(__file__).parent / "images"
IMAGES_DIR.mkdir(exist_ok=True)

ARCHIVE_DIR = Path(__file__).parent / "images" / "archive"
ARCHIVE_DIR.mkdir(exist_ok=True)

def _get_token() -> str:
    """Lazy Token-Laden – wird erst bei Bedarf aus Environment gelesen."""
    return os.getenv("REPLICATE_API_TOKEN", "")

# FLUX Schnell – schnell, gute Qualität (~$0.003/Bild, ~2 Sek)
REPLICATE_MODEL = "black-forest-labs/flux-schnell"


# ─────────────────────────────────────────────
# Stabiler Bild-Key
# ─────────────────────────────────────────────

def recipe_image_key(recipe_id: int, name: str) -> str:
    """Deterministischer Key für ein Rezeptbild.
    Basiert auf recipe_id für Eindeutigkeit + Name für Lesbarkeit."""
    return str(recipe_id)


def image_path(recipe_id: int) -> Path:
    """Dateipfad für ein gecachtes Rezeptbild."""
    return IMAGES_DIR / f"{recipe_id}.jpg"


def image_exists(recipe_id: int) -> bool:
    """Prüft ob ein Bild bereits gecacht ist."""
    return image_path(recipe_id).exists()


def archive_current_images(keep_ids: list = None):
    """Verschiebt aktuelle Bilder ins Archiv.

    keep_ids: Liste von IDs die NICHT archiviert werden sollen.
    """
    import shutil
    import time

    keep = set(keep_ids or [])
    timestamp = int(time.time())

    for f in IMAGES_DIR.glob("*.jpg"):
        try:
            fid = int(f.stem)
            if fid not in keep:
                dest = ARCHIVE_DIR / f"{timestamp}_{f.name}"
                shutil.move(str(f), str(dest))
                logger.info(f"Bild archiviert: {f.name} → archive/{dest.name}")
        except ValueError:
            pass


def get_all_image_ids():
    """Gibt alle verfügbaren Bild-IDs zurück (aktive + Archiv)."""
    ids = set()
    for f in IMAGES_DIR.glob("*.jpg"):
        try:
            ids.add(int(f.stem))
        except ValueError:
            pass
    return sorted(ids)


def get_archive_image_paths():
    """Gibt alle archivierten Bildpfade zurück."""
    return sorted(ARCHIVE_DIR.glob("*.jpg"))


# ─────────────────────────────────────────────
# Prompt Builder
# ─────────────────────────────────────────────

def build_image_prompt_fallback(name: str, cuisine: str = "", ingredients: list = None, description: str = "", diet_type: str = "") -> str:
    """Fallback-Prompt falls Claude nicht erreichbar ist."""
    diet_adj = ""
    if diet_type and diet_type.lower() in ("vegan", "vegetarisch"):
        diet_adj = f"{diet_type} "
    return (
        f"Overhead food photography of {diet_adj}{name}, "
        f"complete dish on ceramic plate, wooden table, natural daylight, "
        f"warm tones, editorial food magazine, photorealistic"
    )


def build_image_prompt_with_claude(name: str, cuisine: str = "", ingredients: list = None, description: str = "", diet_type: str = "") -> str:
    """Nutzt Claude Haiku um einen präzisen englischen Bildprompt zu generieren."""
    import anthropic

    # Key aus Environment oder .env laden
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return build_image_prompt_fallback(name, cuisine, ingredients, description, diet_type)

    # Zutaten als Liste
    ing_list = ""
    if ingredients:
        names = [i.get("name", "") for i in ingredients if isinstance(i, dict) and i.get("name")]
        if names:
            ing_list = ", ".join(names)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user", "content": f"""You are writing a prompt for an AI image generator (FLUX). Describe this dish visually in English. Write ONE short paragraph (max 2 sentences) describing exactly what this finished dish looks like on a plate — colors, textures, arrangement, garnishes. Be very specific and concrete.

IMPORTANT RULES:
- Describe ONLY the actual food, no people, no hands, no text, no watermarks, no logos
- If the dish is {diet_type or 'any'} diet, make sure the description matches (e.g. vegan = NO eggs, NO meat, NO dairy visible)
- Focus on what IS visible, not what's absent

Dish: {name}
Cuisine: {cuisine or 'International'}
Diet: {diet_type or 'any'}
Description: {description or 'n/a'}
Ingredients: {ing_list or 'n/a'}

Reply ONLY with the visual description, nothing else."""}]
        )
        visual_desc = msg.content[0].text.strip()
        logger.info(f"Claude Bildprompt für '{name}': {visual_desc[:80]}...")

        return (
            f"Overhead food photography: {visual_desc} "
            f"Complete dish on ceramic plate, wooden table, natural daylight, "
            f"warm tones, sharp focus, editorial food magazine, photorealistic"
        )
    except Exception as e:
        logger.warning(f"Claude Prompt-Generierung fehlgeschlagen: {e}")
        return build_image_prompt_fallback(name, cuisine, ingredients, description, diet_type)


def build_batch_prompts_with_claude(recipes: list, diet_type: str = "") -> dict:
    """Generiert Bildprompts für ALLE Rezepte in EINEM Claude-Call.

    Returns: Dict {recipe_id: prompt_string}
    """
    import anthropic
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    result = {}
    if not api_key or not recipes:
        for r in recipes:
            rid = r.get("id", 0)
            result[rid] = build_image_prompt_fallback(
                r.get("name", ""), r.get("cuisine", ""),
                r.get("ingredients", []), r.get("description", ""), diet_type
            )
        return result

    # Alle Rezepte in einem Prompt bündeln
    dish_list = []
    for i, r in enumerate(recipes):
        ings = r.get("ingredients", [])
        if isinstance(ings, str):
            try: ings = json.loads(ings)
            except: ings = []
        ing_names = [x.get("name","") for x in ings if isinstance(x, dict) and x.get("name")]
        dish_list.append(
            f"{i+1}. {r.get('name','')} | Cuisine: {r.get('cuisine','')} | "
            f"Ingredients: {', '.join(ing_names[:5])}"
        )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": f"""You are writing prompts for an AI image generator (FLUX). For each dish below, write ONE sentence describing exactly what the finished dish looks like on a plate — colors, textures, arrangement. Be specific and concrete.

RULES:
- Describe ONLY the food, no people, hands, text, watermarks
- Diet is: {diet_type or 'any'}. If vegan: NO eggs, meat, dairy visible
- Focus on what IS visible

Dishes:
{chr(10).join(dish_list)}

Reply with ONLY numbered descriptions, one per line:
1. [description]
2. [description]
..."""}]
        )

        lines = msg.content[0].text.strip().split('\n')
        for i, r in enumerate(recipes):
            rid = r.get("id", 0)
            desc = ""
            # Zeile für dieses Rezept finden
            for line in lines:
                line = line.strip()
                if line.startswith(f"{i+1}.") or line.startswith(f"{i+1})"):
                    desc = line.split(".", 1)[-1].strip() if "." in line else line
                    break

            if desc:
                result[rid] = (
                    f"Overhead food photography: {desc} "
                    f"Complete dish on ceramic plate, wooden table, natural daylight, "
                    f"warm tones, sharp focus, editorial food magazine, photorealistic"
                )
            else:
                result[rid] = build_image_prompt_fallback(
                    r.get("name",""), r.get("cuisine",""),
                    r.get("ingredients",[]), r.get("description",""), diet_type
                )

        logger.info(f"Batch-Prompts für {len(result)} Rezepte generiert")
        return result

    except Exception as e:
        logger.warning(f"Batch-Prompt fehlgeschlagen: {e}")
        for r in recipes:
            rid = r.get("id", 0)
            result[rid] = build_image_prompt_fallback(
                r.get("name",""), r.get("cuisine",""),
                r.get("ingredients",[]), r.get("description",""), diet_type
            )
        return result


def build_negative_prompt() -> str:
    """Negative Prompt für konsistente Qualität."""
    return (
        "illustration, cartoon, drawing, painting, sketch, digital art, "
        "3d render, fantasy, unrealistic, blurry, low quality, watermark, "
        "text, logo, oversaturated, neon colors, plastic looking food, "
        "stock photo watermark, shutterstock, getty images, istock, adobe stock, "
        "copyright text, credit text, website URL, photographer name, "
        "hands, fingers, people, human, utensils in hand"
    )


# ─────────────────────────────────────────────
# Replicate API (synchron, ohne SDK)
# ─────────────────────────────────────────────

def _call_replicate(prompt: str, negative_prompt: str, retries: int = 3) -> Optional[str]:
    """Ruft Replicate API auf und gibt die Bild-URL zurück.

    Nutzt FLUX Schnell über die offizielle Models-API.
    Retry bei 429 (Rate Limit) mit exponentieller Wartezeit.
    """
    if not _get_token():
        logger.warning("REPLICATE_API_TOKEN nicht gesetzt – keine Bildgenerierung.")
        return None

    headers = {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
        "Prefer": "wait",  # Synchrones Warten auf Ergebnis
    }

    payload = json.dumps({
        "input": {
            "prompt": prompt,
            "aspect_ratio": "4:3",
            "num_outputs": 1,
            "output_format": "jpg",
            "output_quality": 95,
            "prompt_upsampling": True,
        }
    }).encode("utf-8")

    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                f"https://api.replicate.com/v1/models/{REPLICATE_MODEL}/predictions",
                data=payload,
                headers=headers,
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=120)
            data = json.loads(resp.read().decode("utf-8"))

            status = data.get("status", "")
            output = data.get("output")

            if status == "succeeded" and output:
                if isinstance(output, list) and len(output) > 0:
                    return output[0]
                elif isinstance(output, str):
                    return output

            if status in ("starting", "processing"):
                return _poll_prediction(data.get("id"))

            logger.error(f"Replicate Fehler: status={status}, error={data.get('error')}")
            return None

        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = (attempt + 1) * 10  # 10s, 20s, 30s
                logger.warning(f"Rate Limit (429) – warte {wait}s (Versuch {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            logger.error(f"Replicate API Fehler: {e}")
            return None
        except Exception as e:
            logger.error(f"Replicate API Fehler: {e}")
            return None

    return None


def _poll_prediction(prediction_id: str, max_attempts: int = 30) -> Optional[str]:
    """Pollt eine Replicate-Prediction bis sie fertig ist."""
    if not prediction_id:
        return None

    import time

    headers = {
        "Authorization": f"Bearer {_get_token()}",
    }

    for _ in range(max_attempts):
        time.sleep(2)
        try:
            req = urllib.request.Request(
                f"https://api.replicate.com/v1/predictions/{prediction_id}",
                headers=headers,
            )
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read().decode("utf-8"))

            status = data.get("status", "")
            if status == "succeeded":
                output = data.get("output")
                if isinstance(output, list) and len(output) > 0:
                    return output[0]
                return output if isinstance(output, str) else None
            elif status == "failed":
                logger.error(f"Replicate Prediction fehlgeschlagen: {data.get('error')}")
                return None
        except Exception as e:
            logger.error(f"Replicate Polling Fehler: {e}")
            continue

    logger.error(f"Replicate Prediction Timeout nach {max_attempts} Versuchen")
    return None


def _download_image(url: str, path: Path) -> bool:
    """Lädt ein Bild von einer URL herunter und speichert es."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Cookflow/1.0"})
        resp = urllib.request.urlopen(req, timeout=60)
        if resp.status == 200:
            data = resp.read()
            if len(data) > 500:  # Mindestgröße für ein echtes Bild
                path.write_bytes(data)
                return True
    except Exception as e:
        logger.error(f"Bild-Download Fehler: {e}")
    return False


# ─────────────────────────────────────────────
# Haupt-API: check_or_generate
# ─────────────────────────────────────────────

def check_or_generate(
    recipe_id: int,
    name: str,
    cuisine: str = "",
    ingredients: list = None,
    description: str = "",
    diet_type: str = "",
) -> Optional[Path]:
    """Prüft ob ein Bild existiert, generiert es sonst.

    Returns: Path zum Bild oder None wenn Generierung fehlschlägt.
    """
    path = image_path(recipe_id)

    # 1. Cache-Hit → sofort zurück
    if path.exists():
        return path

    # 2. Kein API-Token → kein Bild
    if not _get_token():
        logger.info(f"Kein REPLICATE_API_TOKEN – überspringe Bild für Rezept {recipe_id}")
        return None

    # 3. Generieren – Claude schreibt den Prompt
    logger.info(f"Generiere Bild für Rezept {recipe_id}: {name}")
    prompt = build_image_prompt_with_claude(name, cuisine, ingredients, description, diet_type)
    negative = build_negative_prompt()

    image_url = _call_replicate(prompt, negative)
    if not image_url:
        return None

    # 4. Herunterladen und speichern
    if _download_image(image_url, path):
        logger.info(f"Bild gespeichert: {path}")
        return path

    return None


# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# Batch-Generierung – schnell & parallel
# ─────────────────────────────────────────────

_generating = set()  # IDs die gerade generiert werden


def _generate_single(recipe_id: int, prompt: str):
    """Generiert ein einzelnes Bild mit fertigem Prompt."""
    path = image_path(recipe_id)
    if path.exists():
        return

    try:
        negative = build_negative_prompt()
        image_url = _call_replicate(prompt, negative)
        if image_url:
            _download_image(image_url, path)
            logger.info(f"Bild gespeichert: {recipe_id}")
    except Exception as e:
        logger.error(f"Bild-Generierung fehlgeschlagen für {recipe_id}: {e}")
    finally:
        _generating.discard(recipe_id)


def trigger_image_generation(recipes: list, diet_type: str = ""):
    """Generiert Bilder für alle Rezepte ohne Bild.

    1. Batch-Prompts via Claude (1 Call für alle)
    2. 3 parallele Replicate-Calls
    """
    if not _get_token():
        return

    # Nur Rezepte ohne Bild und nicht bereits in Generierung
    to_generate = []
    for r in recipes:
        rid = r.get("id") if isinstance(r, dict) else None
        if rid and not image_exists(rid) and rid not in _generating:
            ings = r.get("ingredients", [])
            if isinstance(ings, str):
                try: ings = json.loads(ings)
                except: ings = []
            to_generate.append({
                "id": rid,
                "name": r.get("name", ""),
                "cuisine": r.get("cuisine", ""),
                "description": r.get("description", ""),
                "ingredients": ings,
            })

    if not to_generate:
        return

    # Markieren als in Bearbeitung
    for r in to_generate:
        _generating.add(r["id"])

    def _batch_worker():
        # 1. Alle Prompts in einem Claude-Call
        prompts = build_batch_prompts_with_claude(to_generate, diet_type)

        # 2. Parallel generieren (max 3 gleichzeitig)
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            for r in to_generate:
                rid = r["id"]
                prompt = prompts.get(rid, "")
                if prompt:
                    pool.submit(_generate_single, rid, prompt)

    t = threading.Thread(target=_batch_worker, daemon=True)
    t.start()


def enqueue(recipe_data: dict):
    """Einzelnes Rezept zur Generierung hinzufügen (Fallback)."""
    rid = recipe_data.get("id")
    if not rid or image_exists(rid) or rid in _generating:
        return
    if not _get_token():
        return
    trigger_image_generation([recipe_data], recipe_data.get("diet_type", ""))
