# Local Accountant

A private personal-finance assistant. Upload your bank statements and get monthly and
yearly reports of where your money went. An AI that runs **on your own computer**
(Ollama) reads and categorizes the transactions. Your data never leaves the machine.

## What it does

- Reads PDF statements from any bank or card issuer, and CSV exports. Each file is
  checked against the statement's own balances: ✅ or ⚠️.
- Categorizes spending with your remembered rules first, then the local AI. Anything
  unclear is marked **To review** for you to confirm.
- Remembers your answers, so the same merchant isn't asked about twice.
- Shows monthly and yearly reports with savings rate, answers questions about your spending,
  and exports everything to CSV.

## Run it locally

You need [uv](https://docs.astral.sh/uv/) and [Ollama](https://ollama.com), plus about
6 GB of free RAM and 2 GB of disk. Pull the default model once:

```bash
ollama pull qwen3:8b
```

Then start the app:

```bash
uv run start.py
```

The first run installs the dependencies and downloads Docling's document models (about
200 MB). The app then opens at <http://127.0.0.1:8765>.

## How to use it

1. **Upload** your PDF or CSV statements. To try it first, use the made-up statements in
   [`sample/`](sample).
2. Click **Scan**. Each file is read, checked (✅/⚠️) and categorized.
3. **Review** the items under *To review*. Keep *remember* ticked to teach the app.
4. **Read your reports** under Month / Year / All time. Change any category inline, or
   fix a misread row with ✎.

## Configuration

These are set in **Settings** in the app:

| Setting | Default | Notes |
|---|---|---|
| LLM URL | `http://127.0.0.1:11434` | Must be local. Non-local hosts are refused. |
| Model for categories & notes | `qwen3:8b` | Any model you have in Ollama |
| Model for reading statements | `qwen3:8b` | Turns each statement page into transactions. About 20–40 s per page with an 8B model. |
| Confidence threshold | `0.8` | Below this, a transaction is marked To review |
| Double-check AI answers | on | Asks the model twice; disagreement → To review. About 2× slower on first scans. |
| Main currency | `CHF` | |
| Exchange-rate download | on | ECB public rates. When off, manual rates are used. |

Environment variables (optional):

| Variable | Default | Notes |
|---|---|---|
| `FINANCE_DATA_DIR` | `./data` | Where the database and the cached ECB file live |
| `LOCAL_ACCOUNTANT_MODELS` | `./.models` | Where Docling's models are stored |
| `LOCAL_ACCOUNTANT_LLM_HOSTS` | (empty) | Comma-separated extra LLM hosts for server mode, e.g. `ollama`. Only private IPs or single-label names are accepted. |
| `LOCAL_ACCOUNTANT_ALLOWED_HOSTS` | (empty) | Comma-separated extra `Host` header values the web server accepts (server mode) |

Launcher options: `uv run start.py --port 9000 --no-browser`.

## More info

- [docs/EVAL.md](docs/EVAL.md): automated tests and the local AI's measured accuracy
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, scan flow, data model
- [docs/pdf-extraction-options.md](docs/pdf-extraction-options.md): how the statement reader was chosen

Released under the [MIT License](LICENSE). Exchange rates come from the European Central
Bank's euro foreign exchange reference rates; CHF cross rates are calculated by Local
Accountant.
