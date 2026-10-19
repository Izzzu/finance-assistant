# Testing and Evaluation

How Local Accountant is tested, and how accurate the local AI is.

## Automated tests

```bash
uv run pyright              # type check: strict for app/ and start.py, standard for tests/
uv run pytest               # automated tests (Docling for real, simulated AI)
uv run pytest -m live -s    # also asks your real local AI to read all synthetic statements
```

The code is strongly typed. Shared types live in `app/domain.py`: TypedDicts for statement
transactions, reader results, CSV column mappings, scan status, undo tokens and categories.
pyright runs in strict mode on the app, and CI fails on any type error.

The automated tests cover:
- parsing (Swiss amount and date formats)
- the statement reader: Docling runs for real, and a simulated AI gives the correct JSON or
  makes typical mistakes on purpose (invented rows, made-up balances, merged rows, a failing page).
  The checks must catch every one of them.
- categorizer with a simulated AI (rules, transfers, double-check, Ollama offline,
  malformed answers)
- reports
- API and security: host check, cross-site protection, non-local AI refused, and a
  **no-data-leaves** test that runs a full scan with the internet blocked

CI runs the suite on macOS, Windows and Linux with Python 3.10 and 3.13.

## Categorization accuracy with the real model

`tests/eval_llm.py` runs the real local model on labelled, made-up Swiss-style
descriptions. It needs Ollama running and is not part of `pytest`.

```bash
uv run python tests/eval_llm.py
```

It measures:
- **Correct when it didn't ask:** of the items the AI categorized without asking you, the
  share that is right. The target is ≥ 95%, because a wrong silent guess is worse than a question.
- **Asked you:** how often an item goes to *To review*.

Results with `qwen3:8b` on a Mac. These were measured on an earlier set of 62 cases; the
set now has 64, so re-run the script to refresh them:

| Setup | Correct when it didn't ask | Asked you | Payments to people asked |
|---|---|---|---|
| AI alone, single pass | 84% | 8% | 3/3 |
| AI alone + double-check | 94% | 23% | 3/3 |
| As shipped (starter rules + double-check) | 98%* | 15% | 3/3 |

\*Optimistic: some starter-rule merchants also appear in the test set. For merchants the
starter rules don't know, expect about 94%. The more you answer with *remember*, the more
merchants become rules.

## Checking on your real statements

1. Run `uv run start.py` and scan one month from each bank.
2. Check each file's status. **✅ balances match** means every row was read correctly.
3. If a file shows ⚠️, it has a difference, or it shows "no transactions found". Share
   only what the status line says, for example "PostFinance ⚠️ −45.20", never the file.
