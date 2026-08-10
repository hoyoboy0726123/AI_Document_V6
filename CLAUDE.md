# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Architecture

AI Document V6 is a document management + OCR + RAG Q&A system. Two independent processes:

- **`backend/`** — FastAPI app (`app.main:app`). Routers under `app/api/v1/`: `auth`, `documents`, `folders`, `metadata`, `rag`, `vector_search`, `tasks`, `admin`, `kg`, `agent`. Business logic lives in `app/services/` (`ocr_pipeline`, `pdf_processing`, `vector_store`, `ollama_client`, `ai`, `documents`, `users`, `system_config`, plus the KG/Agent/provider modules described below). SQLAlchemy ORM models in `app/models.py`; Pydantic schemas in `app/schemas.py`. Default DB is local SQLite (`backend/doc_management.db`); PostgreSQL is supported via `DATABASE_URL`.
- **`frontend/`** — React 18 + Vite + Ant Design (zh-TW locale) + zustand + React Router 6. Pages in `src/pages/`, route table in `src/App.jsx`. All API calls go through `/api` and are proxied by Vite to the backend.

Key cross-cutting pieces:

- **RAG pipeline**: `services/pdf_processing` extracts text (pdfminer.six for native PDFs, PaddleOCR via `services/ocr_pipeline` for image-based PDFs), chunks → `services/ollama_client` embeds via Ollama → `services/vector_store` writes to a single FAISS `IndexIDMap2` persisted at `./storage/faiss_index.bin`. Each `DocumentChunk` row stores its `faiss_id` so DB and FAISS stay in sync.
- **LLM provider abstraction** (`services/llm_provider/`): a provider-neutral `LLMProvider` Protocol (`base.py`) with `OllamaProvider` and `GeminiProvider` impls, selected by the factory in `__init__.py` (`get_llm_provider` / `get_embedding_provider`). Resolution order per kind (`llm` / `embedding` are independent): `system_configs` DB row → `.env` (`LLM_PROVIDER` / `EMBEDDING_PROVIDER`) → fallback `ollama`. Providers are cached singletons keyed by a config fingerprint; **call `llm_provider.invalidate()` after admin saves provider settings** (the admin endpoint and `main.py::apply_llm_overrides_from_db()` mutate the in-memory `settings` object). New Agent/KG code uses this abstraction; legacy call sites still use `ollama_client.get_client()` directly — migrate incrementally, don't rip them out.
- **Knowledge graph** (`services/kg_*`): extracts a spec-citation graph from ingested standards docs (ISO/IEC/MIL-STD/IEEE/ASTM/JIS/CNS/EN/UL/SAE). Flow: `kg_extractor` regex-extracts spec IDs and canonicalizes them → `kg_pipeline` upserts `KGEntity` nodes, then for each candidate spec-pair in a chunk asks the LLM (`kg_relations.classify_pair`) to label the relation → `kg_service` upserts `KGRelation` edges. Relation vocab is **closed**: `references | supersedes | defines | requires | derives_from` (`none` is dropped); edges below `KG_MIN_CONFIDENCE` (default 0.3) are dropped. LLM cost is bounded to `_MAX_PAIRS_PER_CHUNK` (8) pairs/chunk. Extraction runs as a `kg_extract` background task — automatically after ingest when `KG_AUTO_EXTRACT=True` (`services/documents.py`), or on demand via `POST /api/v1/kg/extract/{document_id}`. Re-running clears that doc's relations first (`delete_kg_for_document`) but keeps entities (shared across docs). Query/visualization API is `api/v1/kg` (`/graph`, `/entities/...`, `/stats`); frontend page is `KnowledgeGraphPage.jsx` (route `/knowledge-graph`).
- **ReAct agent** (`services/agent.py` + `services/agent_tools.py`): a step-loop agent that emits one strict-JSON action per turn (`{thought, action, action_input}` or `{thought, final_answer}`), executes a tool, appends the observation, and repeats up to `max_steps`. Tools (`agent_tools.TOOLS`) wrap RAG search + KG service + document lookup (`rag_search`, `spec_lookup`, `spec_references`, `spec_supersedes_chain`, `document_get`) — the LLM never sees raw SQL. Endpoint `POST /api/v1/agent/chat` streams steps as **SSE** (`event: thought|tool_call|observation|final|error|done`); the route opens its own `SessionLocal()` inside the generator (don't rely on request-scoped `db`) and persists the result via `services/conversations.append_message()` (returns the conversation id in the `done` event).
- **Background tasks**: long-running work (VL analysis, batch vectorization, KG extraction) is recorded in the `background_tasks` table and surfaced through `api/v1/tasks` + the frontend `TaskProgressBanner` / `TaskStatusContext`.
- **Per-user analysis state**: `DocumentUserAnalysis` and `UserConversation` keep PDF-analysis chat and Q&A history scoped to each user (the recent commit `25e07d8` added this isolation; do not regress to global state).
- **Auth**: JWT access tokens (short-lived) + refresh tokens stored in the `refresh_tokens` table so they can be revoked. `RefreshToken` rows are bound to a user and one-time-use on rotate.
- **Schema migrations**: **Alembic is in use** (`backend/alembic/versions/`). `main.py::run_db_migrations()` runs `alembic upgrade head` on startup; `ensure_schema_updates()` is only the legacy fallback for when alembic isn't packaged (frozen builds). **Add a new revision for every schema change — do not add `ALTER TABLE` to `ensure_schema_updates()`.** Current chain: `0001_baseline` → `0002_chunk_section_path` → `0003_conversations`. Migrations that touch data must be re-runnable (guard with an existence check) and the DB should be backed up first.
- **Conversation threads (V6)**: `conversations` (id PK, `user_id`, `title`, `messages` JSON, `is_pinned`) replaces `UserConversation`'s one-flat-list-per-user. All reads/writes go through `services/conversations.py` — the three streaming endpoints previously each had their own `flag_modified` append logic. `UserConversation` is deliberately left in place (not dropped) so the 0003 data migration can still be audited; drop it in a later revision once V6 is stable.
- **Default admin bootstrap**: `main.py::ensure_default_admin()` creates/repairs the user named `DEFAULT_ADMIN_USERNAME` on every startup using values from `.env`.

## Running locally

The canonical local launcher is `launch_AI_Document_V3.bat` (Windows). It opens two CMD windows:

- Backend: `cd backend && .venv\Scripts\python.exe -u -m uvicorn app.main:app --host 127.0.0.1 --port 8001`
- Frontend: `cd frontend && set VITE_API_TARGET=http://127.0.0.1:8001 && npm run dev -- --host --port 5175`

URLs: backend `http://127.0.0.1:8001`, frontend `http://localhost:5175`. Health check: `GET /health`.

**Port quirk** (do not "fix" without thinking): the launcher uses `8001`/`5175`, but `vite.config.js` defaults proxy `target` to `http://127.0.0.1:8000` and `package.json` `dev` script defaults to port `3000`. The launcher works only because it sets `VITE_API_TARGET` to override the proxy. Docker Compose uses the README's defaults (backend `8000`, frontend `3000`). README still documents the `8000`/`3000` pair — keep both modes working.

Backend setup (uv preferred):

```
cd backend
uv sync
copy .env_example .env       # then edit
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend setup:

```
cd frontend
npm install
npm run dev                  # defaults to 127.0.0.1:3000
npm run lint                 # eslint, --max-warnings 0
npm run build
```

Docker mode: `docker compose up --build` (frontend + backend only; Ollama stays on the host).

## Packaging a standalone .exe

`python build_and_package.py` (run from repo root) produces a single-file Windows executable: it builds the frontend, copies `frontend/dist` → `backend/frontend_dist`, runs `pip-audit`, then PyInstaller (`package.spec`) bundles everything into `dist/AI_Document_V3.exe`, finally zipping it to `AI_Document_V3_Packaged.zip` with a `checksum.txt`. The PyInstaller entrypoint is `backend/standalone_launcher.py`, which mounts the bundled SPA at `/` and **redirects all read/write paths (SQLite DB, `storage/`, FAISS index) to the directory next to the .exe** via env vars when frozen (`sys._MEIPASS` for read-only resources, `os.path.dirname(sys.executable)` for data). The frozen build serves both API and UI from `127.0.0.1:8000`. Note: `package.spec`'s `hiddenimports` list is hand-maintained — if a new top-level module isn't reached by static analysis from `app.main`, add it there or the frozen build will `ModuleNotFoundError` at runtime.

## Tests

There is no test framework wired up. The only smoke test is `backend/scripts/test_flow.py`, a `fastapi.testclient.TestClient` script that logs in as `admin` / `Admin@123` and walks the document/classification flow. Run it from `backend/` with `uv run python scripts/test_flow.py`. When adding tests, prefer pytest under `backend/tests/` and update `pyproject.toml`.

## Required environment

`backend/.env` is mandatory. `SECRET_KEY` is `Field(..., min_length=32)` in `app/core/config.py`, so the app **will not start** without it. The default admin bootstrap reads `DEFAULT_ADMIN_USERNAME` / `DEFAULT_ADMIN_PASSWORD` / `DEFAULT_ADMIN_EMAIL` from the same file.

Ollama is required for embeddings + LLM + VL. Default models (configurable in `.env`):

- LLM: `qwen3:8b`
- Vision: `qwen2.5vl:7b`
- Embedding: `quentinz/bge-large-zh-v1.5:latest` (note: `config.py` default differs from the `.env_example` which lists `qwen3-embedding:8b`)

Recommended deployment is **Ollama on the host machine, never in Docker** — the README explains why and gives the `host.docker.internal` / Linux host-IP wiring.

Provider selection and KG behavior are also config-driven (all overridable from the admin UI, which writes `system_configs` and re-applies on save):

- `LLM_PROVIDER` / `EMBEDDING_PROVIDER`: `ollama` (default) or `gemini`. `LLM_MODEL` / `EMBEDDING_MODEL` override the per-provider default model (empty → provider default).
- `GEMINI_API_KEY` (required when a provider is `gemini`), `GEMINI_LLM_MODEL` (default `gemini-2.5-flash`), `GEMINI_EMBED_MODEL` (default `text-embedding-004`). Vision/VL still runs through Ollama only.
- `KG_AUTO_EXTRACT` (default `True`) toggles automatic KG extraction after ingest; `KG_MIN_CONFIDENCE` (default `0.3`) is the edge confidence floor.

## Conventions worth knowing

- **Per-user data isolation** is a recent invariant. When you touch document analysis, conversations, or notes, scope by `user_id` (see `DocumentUserAnalysis`, `UserConversation`, `DocumentNote.user_id`).
- **OCR status field** on `Document` uses the values `not_needed | pending | processing | completed | failed | skipped`. Keep these strings stable — frontend filters depend on them.
- **VL analysis page limit** is gated by `MAX_PDF_ANALYSIS_PAGES` in config (defaults to 10) due to model context window — don't bypass.
- **PaddleOCR** is pinned to CPU mode with `enable_mkldnn=False` for stability; first-run downloads models (slow). Output schema changed in 3.4.0 (`rec_texts` / `rec_scores`); `services/ocr_pipeline.py` already handles this.
- **Frontend admin routes** (`/admin/*`) require `<PrivateRoute adminOnly>`. The role check is on `user.role === "admin"`.
- **Reasoning-tag stripping**: `services/ollama_client.py::_strip_reasoning_blocks` removes `<think>` / `<reasoning>` / etc. before returning to the frontend. If you add a new model that emits a different tag, extend that list rather than handling it in the UI.
