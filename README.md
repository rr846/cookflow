# Easy Cook

KI-gestützter veganer Wochenplaner mit Picnic-Integration.

---

## Setup (lokal)

### 1. Voraussetzungen

- Python 3.9+
- [Anthropic API Key](https://console.anthropic.com/)

### 2. Installation

```bash
cd EasyCook
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. API Key eintragen

```bash
cp .env.example .env
# .env öffnen und ANTHROPIC_API_KEY eintragen
```

### 4. Starten

```bash
uvicorn app:app --reload
```

App läuft unter [http://localhost:8000](http://localhost:8000)

---

## Picnic-Zugangsdaten

Picnic-E-Mail und Passwort werden **in der App** unter ⚙️ Einstellungen eingetragen – nicht in der `.env`.

---

## Deployment auf Railway

1. Repo auf GitHub pushen
2. Neues Projekt auf [railway.app](https://railway.app) anlegen
3. GitHub-Repo verbinden
4. Unter **Variables** eintragen: `ANTHROPIC_API_KEY=sk-ant-...`
5. Railway erkennt das `Procfile` automatisch und startet den Server

---

## Dateistruktur

```
EasyCook/
├── app.py              – FastAPI Backend
├── frontend/
│   └── index.html      – Single-Page App
├── requirements.txt
├── Procfile            – Für Railway
├── .env                – API Key (nicht committen)
└── .env.example        – Vorlage
```
