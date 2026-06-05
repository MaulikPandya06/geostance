# GeoStance

**GeoStance** is an open-source geopolitical intelligence platform that tracks how countries position themselves on global events — through official statements, diplomatic communications, and UN voting records. An interactive world map lets you explore stances, compare blocs, and generate AI-powered analysis reports.

Live at [geostance.in](https://geostance.in)

---

## Features

- **Interactive World Map** — visualise country stances (Support / Neutral / Oppose) on tracked geopolitical events as a heatmap
- **UN Voting Records** — browse UN resolutions with per-country vote breakdowns and colour-coded map overlays
- **Bloc Filtering** — filter the map by geopolitical groupings (NATO, BRICS, G77, etc.)
- **Voting Alignment** — compare voting alignment scores between any two countries
- **AI Stance Classification** — raw articles and official feeds are automatically classified using NVIDIA NIM LLMs
- **RAG Chatbot** — ask natural-language questions about a country's stance on any event; answers are grounded in source statements
- **Intelligence Reports** — generate a structured `.docx` analysis report for one or more UN resolutions (overview, voting behaviour, bloc alignments, trends, key themes, or a custom query)
- **Automated Ingestion Pipeline** — Celery beat tasks pull data from government RSS feeds and GDELT News every 15 minutes

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Django 6, Django REST Framework, PostgreSQL + pgvector |
| Task Queue | Celery + Redis |
| AI / LLM | NVIDIA NIM (free tier), RAG via pgvector HNSW index |
| Frontend | React 19, TypeScript, Vite |
| UI | Tailwind CSS, Framer Motion, Lucide React |
| Data Viz | D3.js, TopoJSON, Recharts |
| State | Zustand, TanStack Query |
| Deployment | Render (backend), Vercel (frontend) |

---

## Project Structure

```
geovoice/
├── backend/
│   ├── config/          # Django settings, URLs, WSGI/ASGI
│   └── core/
│       ├── models.py    # Country, Event, Statement, UNResolution, UNVote, ...
│       ├── views.py     # REST API endpoints
│       ├── tasks.py     # Celery ingestion & classification tasks
│       └── services/    # RAG service, summary service
│           utils/       # Report builder, LLM helpers
└── frontend/
    └── src/
        ├── features/    # Events panel, map, UN voting, Ask AI
        └── components/  # Layout, shared UI
```

---

## Getting Started

### Prerequisites

- Python 3.11+
- Node.js 20+
- PostgreSQL 15+ with the [pgvector](https://github.com/pgvector/pgvector) extension
- Redis

### Backend

```bash
cd backend
python -m venv env
source env/bin/activate        # Windows: env\Scripts\activate
pip install -r requirements.txt

# Copy and fill in your environment variables
cp .env.example .env

python manage.py migrate
python manage.py runserver
```

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local     # set VITE_API_URL=http://localhost:8000
npm run dev
```

### Celery Workers

```bash
# In the backend directory (with the venv active)
celery -A config worker -l info
celery -A config beat   -l info
```

---

## Environment Variables

| Variable | Description |
|---|---|
| `SECRET_KEY` | Django secret key |
| `DEBUG` | `True` in development |
| `DB_NAME / DB_USER / DB_PASSWORD / DB_HOST / DB_PORT` | PostgreSQL connection |
| `CELERY_BROKER_URL` | Redis URL (default `redis://localhost:6379/0`) |
| `NVIDIA_NIM_API_KEY` | API key for LLM classification and RAG |

---

## API Overview

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/events/` | List tracked geopolitical events |
| `GET` | `/api/events/<id>/heatmap/` | Country stance heatmap for an event |
| `GET` | `/api/events/<id>/resolutions/` | UN resolutions linked to an event |
| `GET` | `/api/un-resolutions/<id>/vote-map/` | Vote map data for a resolution |
| `GET` | `/api/un-votes/alignment/` | Voting alignment score between two countries |
| `POST` | `/api/un-resolutions/generate-report/` | Generate `.docx` intelligence report |
| `POST` | `/api/un-resolutions/ask/` | Ask AI a question about a resolution |
| `POST` | `/api/chatbot/` | RAG chatbot for country × event queries |

Interactive API docs are available at `/api/schema/swagger-ui/`.

---

## Data Sources

- **Government RSS feeds** — official Ministry of Foreign Affairs press releases (India, Russia, US, and others)
- **UN Digital Library** — UN resolution metadata, voting records, and meeting transcripts

---

## License

MIT
