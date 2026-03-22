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

# FLUX 1.1 Pro – hochwertige fotorealistische Bilder (~$0.04/Bild)
REPLICATE_MODEL = "black-forest-labs/flux-1.1-pro"


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
            messages=[{"role": "user", "content": f"""Describe this dish visually in English for an AI image generator. Write ONE short paragraph (max 2 sentences) describing exactly what this dish looks like on a plate — colors, textures, arrangement. Be specific and concrete.

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


def build_negative_prompt() -> str:
    """Negative Prompt für konsistente Qualität."""
    return (
        "illustration, cartoon, drawing, painting, sketch, digital art, "
        "3d render, fantasy, unrealistic, blurry, low quality, watermark, "
        "text, logo, oversaturated, neon colors, plastic looking food"
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
# Zentrale Queue – ein Worker, sequentiell
# ─────────────────────────────────────────────

_image_queue = Queue()
_queued_ids = set()
_worker_started = False
_worker_lock = threading.Lock()


def _worker():
    """Einziger Worker-Thread – verarbeitet Bilder nacheinander."""
    while True:
        item = _image_queue.get()
        try:
            check_or_generate(
                recipe_id=item["id"],
                name=item["name"],
                cuisine=item.get("cuisine", ""),
                ingredients=item.get("ingredients", []),
                description=item.get("description", ""),
                diet_type=item.get("diet_type", ""),
            )
        except Exception as e:
            logger.error(f"Worker Fehler für Rezept {item.get('id')}: {e}")
        finally:
            _queued_ids.discard(item.get("id"))
            _image_queue.task_done()
        time.sleep(5)  # 5s Pause → kein Rate Limit


def _ensure_worker():
    """Startet den Worker-Thread falls noch nicht laufend."""
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            _worker_started = True


def enqueue(recipe_data: dict):
    """Fügt ein Rezept zur Bildgenerierungs-Queue hinzu (dedupliziert)."""
    rid = recipe_data.get("id")
    if not rid or rid in _queued_ids or image_exists(rid):
        return
    if not _get_token():
        return
    _queued_ids.add(rid)
    _image_queue.put(recipe_data)
    _ensure_worker()


def trigger_image_generation(recipes: list, diet_type: str = ""):
    """Fügt alle Rezepte ohne Bild zur Queue hinzu."""
    for r in recipes:
        rid = r.get("id") if isinstance(r, dict) else r["id"]
        if rid and not image_exists(rid):
            ings = r.get("ingredients", [])
            if isinstance(ings, str):
                try:
                    ings = json.loads(ings)
                except Exception:
                    ings = []
            enqueue({
                "id": rid,
                "name": r.get("name", "") if isinstance(r, dict) else r["name"],
                "cuisine": r.get("cuisine", "") if isinstance(r, dict) else r["cuisine"],
                "description": r.get("description", "") if isinstance(r, dict) else r.get("description", ""),
                "ingredients": ings,
                "diet_type": diet_type,
            })
