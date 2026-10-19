# Architecture

This document shows how Local Accountant is put together.

**Guiding rule:** your statements and transactions never leave the machine (or your own
server). While the app runs, the only outgoing internet request is a download of the public
ECB exchange-rate file, which contains no personal data. (On first run, setup also downloads
the dependencies and Docling's models once.)

## 1. Components

```mermaid
flowchart LR
    subgraph Device["Your computer (or your own home server)"]
        direction LR
        UI["Browser UI<br/>Upload · Report · Settings<br/><i>vanilla HTML/JS, no CDNs</i>"]

        subgraph App["Local Accountant (Python, FastAPI)"]
            direction TB
            API["server.py<br/>JSON API + security checks<br/>(Host check, custom header)"]
            JOB["pipeline.py<br/>background scan job"]
            PDF["pdf_import.py<br/>Docling → local AI → JSON<br/>generic checks"]
            CSV["importer.py<br/>CSV + shared date/amount parsing"]
            CAT["categorizer.py<br/>normalize → rules → transfers → LLM → To review"]
            REP["reports.py<br/>monthly · yearly · all time<br/>notes · Q&A · CSV export"]
            FX["fx.py<br/>ECB rates, cached"]
            LLMC["llm.py<br/>Ollama client<br/><b>localhost / allowlist only</b>"]
        end

        DB[("SQLite<br/>data/finance.db<br/>owner-only permissions")]
        MOD[(".models/<br/>Docling models<br/>used offline")]
        OLL["Ollama<br/>qwen3:8b<br/>127.0.0.1:11434"]
    end

    ECB["ECB public<br/>reference-rate file"]

    UI <-- "HTTP on 127.0.0.1:8765" --> API
    API --> JOB & REP & CAT
    JOB --> PDF & CSV & FX & CAT
    PDF --> MOD
    PDF -- "one request per page" --> LLMC
    CAT --> LLMC
    REP --> LLMC
    LLMC <--> OLL
    JOB & CAT & REP --> DB
    FX -- "GET whole file<br/>(no data sent)" --> ECB

    classDef ext fill:#fff4e5,stroke:#d08a00,color:#000
    classDef store fill:#e8f1ff,stroke:#3a6fd8,color:#000
    class ECB ext
    class DB,OLL store
```

| Module | Responsibility |
|---|---|
| `server.py` | Serves the UI and the JSON API. Security checks: validates the `Host` header (blocks DNS rebinding), requires a custom header on writes (blocks cross-site requests), and sets a strict Content-Security-Policy. `start.py` binds it to 127.0.0.1. Login for server mode is planned, not built. |
| `pdf_import.py` | Any bank, no per-bank rules. **Docling** turns each page into Markdown (layout and tables, from the PDF's own text layer, with models stored locally in `.models/`). The **local AI** turns each page into JSON transactions. **Generic checks** follow: every amount must be printed in the PDF (otherwise the row is dropped), made-up balances are removed, and opening + movements = closing or the running balances must follow row by row. Scanned PDFs without a text layer are rejected (no OCR yet). |
| `importer.py` | Imports CSV files (auto-detects encoding, delimiter, header and columns). Also holds the date and amount parsing shared with the PDF importer, e.g. `1'234.50` and `1 234,56`. |
| `pipeline.py` | The background scan job. Each file goes through: parse → save (deduplicated) → convert to CHF → categorize. The UI polls it for per-file progress. Also re-runs a file's balance check after you correct a row. |
| `categorizer.py` | Categorizes transactions: your rules (including ~40 starter Swiss merchants) first, then transfer pairing, then the local LLM. The LLM is asked twice ("double-check"); if its answers disagree, or confidence is below the threshold, the item goes To review. Remembering a rule never overwrites your manual choices. |
| `textutil.py` | Normalizes descriptions (removes card numbers, dates, boilerplate), does whole-word rule matching, and masks account numbers to their last 4 digits. |
| `reports.py` | Builds the monthly report (monthly costs, plus yearly costs listed separately), the yearly report (yearly costs, 12-month table, set-aside), the all-time view, LLM notes, Q&A and CSV export. |
| `fx.py` | Converts non-CHF amounts with ECB daily reference rates, using cached cross rates. |
| `llm.py` | Ollama client. It refuses any non-loopback or non-allowlisted host and ignores system proxies. |
| `db.py` | SQLite schema and migrations, default categories, default and starter rules, settings. |
| `domain.py` | Shared types (TypedDicts) used across the app. |
| `static/` | Web UI: plain HTML/CSS/JS, no external assets. |

Folders in the project:

| Folder | Contents |
|---|---|
| `app/` | The modules above. `start.py` (the launcher) sits next to it. |
| `tests/` | pytest suite, synthetic statement generators, simulated AI, live tests and the AI accuracy eval (see [EVAL.md](EVAL.md)) |
| `sample/` | Synthetic statement PDFs (made-up data): PostFinance-, Swisscard- and Wise-style, plus three "unseen" layouts |
| `docs/` | This file, [EVAL.md](EVAL.md), [pdf-extraction-options.md](pdf-extraction-options.md) |
| `data/` | Your database and the cached ECB file. Created on first run; never commit it. |
| `.models/` | Docling's models, downloaded once |

## 2. Scan flow (Upload → Scan → Report)

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant UI as Browser UI
    participant API as server.py
    participant J as pipeline (job)
    participant P as pdf_import
    participant C as categorizer
    participant L as Ollama (local)
    participant DB as SQLite

    You->>UI: Drop PDF/CSV statements
    You->>UI: Click "Scan"
    UI->>API: POST /api/scan (files)
    API->>J: start background job
    API-->>UI: job id
    loop each file
        J->>P: parse_pdf(bytes)
        P->>P: Docling: each page → Markdown (text layer, local models)
        loop each page
            P->>L: page Markdown → JSON rows + opening/closing balance
            L-->>P: rows
        end
        P->>P: drop rows whose amount isn't printed in the PDF
        P->>P: verify opening + movements = closing, or running balances
        P-->>J: rows, balance check, warnings
        J->>DB: save transactions (dedup by hash), file status ✅/⚠️
        Note over P: PDF bytes discarded, never stored
    end
    J->>J: convert to CHF (statement amount, ECB or manual rate)
    J->>C: categorize
    C->>DB: apply your rules (always trusted)
    C->>C: pair transfers between your accounts (→ To review as a suggestion)
    loop batches of ~15 merchants
        C->>L: descriptions + your category descriptions + examples (asked twice if double-check is on)
        L-->>C: merchant, category, confidence
    end
    C->>DB: confident → AI-categorized, otherwise To review
    UI->>API: GET /api/jobs/{id} (poll progress)
    API-->>UI: done
    UI->>API: GET /api/report/month/{YYYY-MM}
    API-->>UI: report
    UI->>API: GET /api/review
    API-->>UI: "To review" list
    UI-->>You: Report opens with To review on top
```

## 3. Review and correction loop

```mermaid
flowchart TD
    A["Transaction in report<br/>(To review or any row)"] --> B{"You pick a category"}
    B --> C["Prompt: 'Also apply to N similar and remember?'<br/>pattern editable"]
    C -- "yes" --> D["Save rule: pattern → category"]
    D --> E["Update matching transactions<br/>that are To review or AI-categorized"]
    C -- "no (one-off)" --> F["Update only this transaction"]
    E --> G["Status 👤 set by you<br/>(rules never overwrite it)"]
    F --> G
    G --> H["Report totals update instantly"]
    D --> I["Future scans: rule applies before the LLM"]
```

Each transaction moves through these statuses:

```mermaid
stateDiagram-v2
    [*] --> new: scanned
    new --> rule: matches your rule
    new --> llm: AI confident (≥ threshold)
    new --> review: AI unsure / person-to-person / Ollama offline
    review --> user: you choose a category
    llm --> user: you correct it
    rule --> user: you correct it
    review --> rule: a new rule matches
    llm --> rule: a new rule matches
    user --> user: only you change it
```

## 4. Data model

```mermaid
erDiagram
    CATEGORIES ||--o{ TRANSACTIONS : "categorizes"
    CATEGORIES ||--o{ RULES : "target of"
    IMPORTS ||--o{ TRANSACTIONS : "contains (undo = delete)"

    CATEGORIES {
        int id PK
        text name "Grocery, Eating Out, ... Yearly spendings"
        text kind "expense | income | transfer"
        text period "monthly | yearly (which report)"
        text description "guides the local LLM"
    }
    IMPORTS {
        int id PK
        text filename
        text bank "issuer name read from the statement"
        text account "bank + last 4 digits"
        text period_from
        text period_to
        text balance_check "ok | diff | none"
        real balance_diff
        real opening "statement balances, for re-checks"
        real closing
        int liability "1 = credit card"
        text warnings "JSON list"
        text imported_at
        int added
        int skipped "duplicates"
    }
    TRANSACTIONS {
        int id PK
        int import_id FK
        text account
        text date "YYYY-MM-DD"
        text description
        real amount "negative = money out"
        text currency
        real amount_chf
        real fx_rate
        text fx_source "same | statement | ecb | manual"
        text norm "normalized description"
        text merchant
        text merchant_key
        int category_id FK
        text status "new | rule | llm | review | user"
        int suggestion_id "AI suggestion when unsure"
        real confidence
        text source "llm | csv"
        real balance "running balance on the statement"
        real share "your part, e.g. 0.5"
        real orig_amount "as first read, if you corrected it"
        int edited
        text hash UK "dedup"
    }
    RULES {
        int id PK
        text pattern UK "normalized words, e.g. galaxus"
        int category_id FK
        text created_at
    }
    LLM_CACHE {
        text key PK "merchant key"
        text merchant
        int category_id
        real confidence
    }
    SETTINGS {
        text key PK
        text value "models, LLM URL, threshold, currency, fx, double-check"
    }
```

## 5. Trust boundaries

```mermaid
flowchart LR
    subgraph Trusted["Trusted: your device / your server"]
        UI2[Browser] --- APP[App] --- DB2[(SQLite)]
        APP --- OL[Ollama]
    end
    OtherSites["Other websites<br/>in your browser"] -. "blocked: Host check +<br/>custom header + no CORS" .-x APP
    LAN["Other devices<br/>on the network"] -. "blocked: bound to 127.0.0.1<br/>(planned server mode: login + HTTPS)" .-x APP
    APP -- "public rate file only" --> ECB2["ECB"]
    APP -. "refused: non-local LLM URL" .-x Cloud["Cloud LLM APIs"]
```
