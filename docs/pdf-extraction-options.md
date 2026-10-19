# Reading statements without per-bank rules: options (October 2026)

> **Outcome (built 2026-10-16).** The shipped reader differs from the recommendation below.
> Its design is a mix of B and D:
> - **Docling** turns each page into Markdown, from the PDF's text layer (no OCR).
> - The **text model** (`qwen3:8b`, temperature 0, fixed seed) turns each page's Markdown into
>   JSON rows plus the opening and closing balance.
> - **Generic checks** follow: every amount and running balance must be printed in the text
>   layer, and opening + movements = closing, or the running balances must follow row by row.
>
> Not built: the vision model (C), the second method with row-by-row agreement, and the
> date check against the text layer. The rule parser (A) was not kept as a fallback; it
> survives only as a test helper (`tests/reference_rules.py`). Docling's output is cached
> in memory per file hash; the stored rows are deduplicated by hash, so a re-scan adds
> nothing. The bake-off's results aren't recorded here. The current design is in
> [ARCHITECTURE.md](ARCHITECTURE.md).
>
> The text below is the analysis as written before the rebuild. "Current approach" there
> means the old rule-based reader.

## Goal

Extract transactions from **any** bank's PDF layout:
- **Local only.** Nothing is sent to cloud services.
- **License fits MIT.** Tools and models must be permissively licensed.
- **Verifiable.** Results are checked against the statement's own balances.

## Why the current approach struggles

`pdf_import.py` reads the PDF's text layer and applies layout rules: find the table header,
assign amounts to columns by x-position, attach following lines as description, and so on.

Each new layout breaks a different assumption. Examples:
- the date printed once per day
- left-aligned column headers
- balance shown only on the last row of a day
- `1 200.00` split at the space

Patching rule by rule doesn't scale to "any bank".

## The options

| # | Approach | How it works | Strengths | Weaknesses | License | Local cost |
|---|---|---|---|---|---|---|
| A | **Rules on the text layer** (today) | pdfplumber words + layout heuristics | Fast (<1 s), deterministic, exact digits | Brittle on new layouts | MIT | none |
| B | **Docling** (IBM, LF AI) | Layout analysis + table-structure model (TableFormer) turns PDF tables into cells. A tiny LLM call maps column headers to roles (date/credit/debit/…), then rows are built deterministically. | Structure-agnostic tables, runs on CPU, deterministic after mapping, exact digits from the text layer | Multi-line descriptions and date-less rows still need merging logic; statements that aren't real tables | Code MIT; models Apache-2.0 / CDLA-Permissive-2.0 | pip + PyTorch (~1–2 GB), ~2–10 s/page |
| C | **Vision LLM reads the page** (`qwen3-vl:8b` via Ollama) | Render page → image. The model returns JSON rows (date, value date, description, money in/out, balance) plus opening/closing balance. **Text-anchored:** the PDF's own text is given alongside the image, so digits come from the text layer, not from pixels. | Handles any layout, multi-line rows, date-less rows; understands "this is a balance line" | Slower; not bit-for-bit deterministic; can misread or invent rows, so it needs strict verification | Apache-2.0 | 6.1 GB model, ~30–90 s/page on a Mac |
| D | **OCR model to Markdown, then a text LLM** (`glm-ocr` → `qwen3:8b`) | Small document-OCR model turns each page/table into Markdown; the text LLM converts the Markdown table to JSON | Smaller vision model (2.7 GB), good table recognition | Two models, two error sources; same verification needs as C | GLM-OCR: MIT (weights), Apache-2.0 (code) | 2.7 GB + existing qwen3:8b |
| E | **Classic OCR** (Tesseract / RapidOCR) | Pixels → words with positions | Needed only for **scanned** PDFs | Gives the same word list as the text layer, so it doesn't solve structure | Apache-2.0 | small |

**Excluded for licensing:**
- **MinerU** model weights: AGPL-3.0.
- **Marker / Surya:** model weights carry usage restrictions. Re-check if they come up again.
- **Gemma, Llama vision:** custom, non-OSI licenses.

## Recommendation: "AI reads, rules verify"

Make the reader structure-agnostic and keep it **checkable**:

1. **Extract with two independent methods.**
   - **B (Docling tables)** is the deterministic primary method when the PDF has real tables.
   - **C (qwen3-vl:8b, text-anchored)** is the layout-agnostic method that works on anything.
2. **Verify with generic rules.** These are the same for every bank, with no per-bank code:
   - every amount and date must exist in the page's text layer, so hallucinated rows are rejected
   - running balance continuity, and opening + movements = closing
   - **agreement:** rows that both methods found identically are ✅; differences are flagged
     on that row for you to confirm with ✎
3. **Determinism where it matters.**
   - Temperature 0 and a fixed seed.
   - The result is stored per file hash, so re-scanning the same file gives the same rows.
   - The balance check, not the model, decides ✅ or ⚠️.
4. **The current rules (A)** stay as a fast third opinion and as an offline fallback.

## Proposed bake-off (before rebuilding)

Score A, B, C and D on:
- the synthetic statements in `tests/statements.py`, including the English PostFinance layout
- **your real statements, run locally.** A scoring script prints only metrics (rows found,
  balance check, agreement between methods, seconds per page), with no names or amounts.

Downloads needed, all local:

| Item | Size | Used by |
|---|---|---|
| `qwen3-vl:8b` | 6.1 GB | C |
| `glm-ocr` | 2.7 GB | D |
| `docling` + PyTorch | ~1–2 GB, pinned and hash-checked | B |

Choose by: balance check passes on real statements, then speed.

## Sources

- [Docling: MIT, LF AI & Data](https://simple-repository.app.cern.ch/project/docling)
- [Granite-Docling, Apache-2.0](https://www.ibm.com/granite/docs/models/docling)
- [Docling technical report](https://arxiv.org/abs/2408.09869v1)
- [Ollama: qwen3-vl](https://ollama.com/library/qwen3-vl)
- [Ollama: glm-ocr](https://ollama.com/library/glm-ocr)
- [Best open-source OCR models 2026 (Roboflow)](https://roboflow.com/blog/best-open-source-ocr-models)
- [Open-source VLMs 2026 (BentoML)](https://www.bentoml.com/blog/multimodal-ai-a-guide-to-open-source-vision-language-models)
- [MinerU2.5 paper](https://arxiv.org/pdf/2509.22186)
- [ParseBench (document parsing benchmark)](https://arxiv.org/pdf/2604.08538)
- License checks: Hugging Face model cards and GitHub license API, 2026-10-16
