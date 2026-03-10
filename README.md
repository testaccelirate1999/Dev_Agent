# RAML Platform v5.0 — Google ADK Migration

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     FastAPI  :8001                          │
│  (raml_server.py — original REST API, now ADK-backed)       │
└────────────────────┬────────────────────────────────────────┘
                     │ ADK Runner
                     ▼
┌─────────────────────────────────────────────────────────────┐
│              Master Agent  (api_design_master)              │
│  model: claude-sonnet-4-6 via LiteLlm                       │
│                                                             │
│  Handles:                                                   │
│  • Routing / intent classification                          │
│  • General API design questions (no tools)                  │
│  • Session lifecycle (create/list/delete)                   │
│  • Delegates all RAML work → raml_specialist (AgentTool)    │
└────────────────────┬────────────────────────────────────────┘
                     │ AgentTool delegation
                     ▼
┌─────────────────────────────────────────────────────────────┐
│            RAML Specialist Agent  (raml_specialist)         │
│  model: claude-haiku-4-5-20251001 via LiteLlm               │
│                                                             │
│  Tools (always run in sequence):                            │
│  1. fetch_learned_rules   — Pinecone lesson memory          │
│  2. fetch_raml_context    — Pinecone RAG retriever          │
│  3. generate_raml_files   — Anthropic API (direct)          │
│  4. validate_raml_files   — static analysis (no LLM)        │
│  5. fix_raml_errors       — Anthropic API (if errors)       │
│  6. save_correction_lesson — background lesson extraction   │
│                                                             │
│  Always returns: summary text + full file contents          │
└─────────────────────────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│                  Shared Infrastructure                      │
│  • SessionStore  — disk-persisted file + history state      │
│  • RAGRetriever  — Pinecone + Voyage AI embeddings          │
│  • LessonMemory  — Pinecone "lessons" namespace             │
│  • AnypointPublisher — Anypoint Design Center push          │
└─────────────────────────────────────────────────────────────┘
```

## Project Structure

```
raml_adk/
├── agent.py                    ← ADK entry point (exports root_agent)
├── raml_server.py              ← FastAPI bridge (original REST API preserved)
├── requirements.txt
│
├── master_agent/
│   ├── __init__.py
│   ├── agent.py                ← Master LlmAgent definition
│   └── tools.py                ← Session management tools
│
├── raml_agent/
│   ├── __init__.py
│   ├── agent.py                ← RAML specialist LlmAgent definition
│   └── tools.py                ← All 8 RAML FunctionTools
│
└── shared/
    ├── __init__.py
    ├── session_store.py        ← RAMLSession + SessionStore (framework-agnostic)
    ├── raml_tools.py           ← Core logic (Anthropic SDK direct, no LangChain)
    ├── raml_prompts.py         ← Unchanged from v4
    ├── retriever.py            ← Unchanged from v4
    ├── lesson_memory.py        ← Unchanged from v4
    └── anypoint_publisher.py   ← Unchanged from v4
```

## What Changed vs v4

| Component | v4 (LangChain) | v5 (ADK) |
|-----------|---------------|----------|
| LLM calls | `langchain_anthropic.ChatAnthropic` | `anthropic.Anthropic` SDK (direct) |
| Agent framework | Custom `run()` generator | `google.adk.agents.LlmAgent` |
| Tool definition | Plain functions called imperatively | ADK `FunctionTool` (docstring = schema) |
| Multi-agent | Single agent | Master → RAML Specialist (AgentTool) |
| Model config | `ANTHROPIC_MODEL` | `MASTER_ANTHROPIC_MODEL` + `ANTHROPIC_MODEL` |
| FastAPI | Direct agent calls | ADK `Runner` → SSE bridge |
| Unchanged | `retriever.py`, `lesson_memory.py`, `raml_prompts.py`, `anypoint_publisher.py`, `session_store` logic |

## Environment Variables

```env
# Required
ANTHROPIC_API_KEY=sk-ant-...
PINECONE_API_KEY=...
PINECONE_INDEX_NAME=raml-knowledge-base
VOYAGE_API_KEY=...

# Model selection
ANTHROPIC_MODEL=claude-haiku-4-5-20251001      # RAML specialist (fast/cheap)
MASTER_ANTHROPIC_MODEL=claude-sonnet-4-6        # Master agent (smarter routing)

# Optional
RAML_OUTPUT_DIR=output
PINECONE_CLOUD=aws
PINECONE_REGION=us-east-1

# Anypoint (for push)
ANYPOINT_USERNAME=...
ANYPOINT_PASSWORD=...
ANYPOINT_ORG_ID=...
```

## Running

### FastAPI server (original UI)
```bash
pip install -r requirements.txt
uvicorn raml_server:app --reload --port 8001
```

### ADK dev UI (optional, for debugging agent behaviour)
```bash
# From raml_adk/ directory
adk web .
# → http://localhost:8000
```

### ADK API server (alternative to FastAPI)
```bash
adk api_server .
# → http://localhost:8000/run  (ADK native API)
```

### Terminal (quick test)
```bash
adk run . "Create an Orders REST API with pagination"
```

## API Endpoints (FastAPI — unchanged from v4)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| POST | `/sessions` | Create project session |
| GET | `/sessions` | List all sessions |
| DELETE | `/sessions/{id}` | Delete session |
| POST | `/sessions/{id}/chat` | **Chat (SSE stream)** |
| PUT | `/sessions/{id}/files/{path}` | Manual file edit |
| GET | `/sessions/{id}/files` | List files |
| GET | `/sessions/{id}/files/{path}` | Get file content |
| GET | `/sessions/{id}/download` | Download ZIP |
| POST | `/sessions/{id}/validate` | On-demand validation |
| POST | `/sessions/{id}/push` | Push to Anypoint |
| GET | `/lessons` | List learned lessons |
| DELETE | `/lessons/{id}` | Delete a lesson |

## SSE Event Types (unchanged from v4)

```json
{"type": "step_start", "label": "...", "tool": "..."}
{"type": "step_done",  "tool": "...", "summary": "..."}
{"type": "files",      "files": {...}, "changed_files": [...]}
{"type": "validation", "valid": true, "error_count": 0, ...}
{"type": "done",       "message": "...", "session": {...}}
{"type": "error",      "msg": "..."}
```

## RAML Specialist Response

The `raml_specialist` agent **always returns both**:
1. A text summary (message + validation status + explanation)
2. Full file contents (embedded in the `generate_raml_files` tool result)

This satisfies the requirement: "always return both (files dict + summary message)".
The master agent relays both to the user/UI.
