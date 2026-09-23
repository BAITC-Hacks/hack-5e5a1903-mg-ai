# OrgDiff: AI agent for organisational structure and function analysis

Solution for the HackAlem AI case *"AI agent: analysis of organisational structure and
functions"*. Everything the jury needs is in this folder: code, tests, test data, Docker
files, run reports and a sample conclusion. Russian version with the engineering log:
[docs/README.ru.md](docs/README.ru.md).

Contents: [1. Project name](#1-project-name) · [2. Problem and users](#2-what-it-solves-and-for-whom) ·
[3. What is implemented](#3-what-is-implemented) · [4. How it works](#4-how-it-works-user-flow) ·
[5. Technologies](#5-technologies) · [6. Architecture](#6-architecture) · [7. Install and run](#7-installation-and-run) ·
[8. How to test](#8-how-to-test-a-scenario-the-jury-can-repeat) · [9. Data and integrations](#9-data-and-integrations) ·
[10. Limitations](#10-limitations) · [11. Deployment](#11-deployment) · [12. Value and roadmap](#12-value-originality-and-roadmap)

## 1. Project name

**OrgDiff**: an agent that compares the "before" and "after" document sets of a
reorganisation and explains what changed, with a document, clause number and verbatim
quote behind every statement.

## 2. What it solves and for whom

When a regulation on a division is re-issued (in the case: the Regulation on Internal
Audit, edition 8 → edition 9), an internal-audit employee has to answer by hand:

- which departments were created, kept or abolished, and how their staffing changed;
- which functions disappeared, were narrowed, moved to another role or merged;
- where two roles now perform the same function;
- where the new edition creates a potential conflict of interest.

A regulation of this kind has about 500 clauses on 30 pages, and numbering shifts between
editions, so line-by-line diff is useless and manual comparison takes days. OrgDiff does
the comparison in three minutes and produces a conclusion the responsible employee
reviews and signs. The users are internal-audit staff, the head of the division, and
anyone who maintains regulations on departments.

## 3. What is implemented

Every row names the requirement of the case, where it is implemented and the automated
check that proves it. All claims below are covered by the test suite: **40 tests** in
`tests/`, 39 run without an API key, 1 needs one.

| Case requirement | Status | Where in code | How to verify |
|---|---|---|---|
| Upload of "before" and "after" sets, several files per side | done | `app.py` (`/analyze`), `orgdiff/load.py::load_set` | `tests/test_report_and_app.py::test_web_app_end_to_end` |
| Input: Word (.docx) | done | `orgdiff/parse.py` | `tests/test_parse.py` |
| Input: PDF (text layer) | done: lines re-flowed into paragraphs, table of contents dropped | `orgdiff/load.py` | `tests/test_formats.py`, the control set in `testdata/` is PDF |
| Input: Excel (.xlsx) org-structure table | done | `orgdiff/structure.py::from_xlsx` | `tests/test_formats.py::test_xlsx_structure_matches_structure_from_text` |
| Structure preserved on parsing: clause number, section, nesting, owner, modality | done: composite ids such as `5.3.2.а`, owner inherited from the lead phrase | `orgdiff/parse.py::parse_paragraphs` | `tests/test_parse.py` |
| Every fragment keeps its address: file, section, clause | done | `orgdiff/pipeline.py::_clause` (`doc`, `section`, `id`, `text`) | `tests/test_control_set.py::test_lost_functions_are_detected_with_sources` |
| Long documents without context truncation | done by design: the model only ever sees one pair of clauses or a batch of up to 40 lead phrases, never a whole document | `orgdiff/verify.py`, `orgdiff/owners.py` | control set: 496 clauses per side, 74 model calls |
| Must-have 1: reorganised, kept, created units and positions, incl. staffing changes inside kept departments | done | `orgdiff/structure.py::structure_changes`, `unit_positions_*` | `tests/test_control_set.py::test_reorganisation_is_detected` |
| Must-have 2: lost functions, by meaning and not by string match; transfer is distinguished from loss | done: monotone alignment with merges, gap classes lost / partial / moved / merged / removed from owner | `orgdiff/align.py`, `orgdiff/analyze.py` | `tests/test_control_set.py`, `tests/test_align.py` |
| Must-have 2: partial losses with the missing part named and quoted | done, model verdict with a vote of three and mechanical quote gates | `orgdiff/verify.py::coverage_vote`, `orgdiff/analyze.py::apply_verification` | `tests/test_control_set.py::test_full_pipeline_with_model_meets_submission_bar` (needs key) |
| Must-have 3: duplicated functions across roles, both sources shown | done: clustering inside the new edition, confirmed by the model | `orgdiff/analyze.py::duplicates`, `orgdiff/verify.py::same_function` | `tests/test_control_set.py::test_duplicates_are_detected_with_both_sources` |
| Must-have 3: potential conflicts of interest | done: keyword search, then a model verdict risk / safeguard / mention with a quote gate | `orgdiff/analyze.py::conflicts_of_interest`, `orgdiff/verify.py::coi_assess` | `tests/test_control_set.py::test_conflict_of_interest_clause_is_found` |
| Must-have 4: a source for every finding, navigation from a finding to the clause in context | done: each reference in the report is a link to the clause on the document page | `app.py::link_sources`, `/job/{id}/doc/{side}` | `tests/test_report_and_app.py::test_web_app_end_to_end` |
| Must-have 5: final conclusion with all five blocks, sources and recommendations | done: Markdown and .docx, 8 sections | `orgdiff/report.py` | `tests/test_report_and_app.py::test_conclusion_has_all_required_sections_and_sources` |
| Constraint: the agent never states what the documents do not contain | done: a quote must be a substring of the clause; the "lost part" must be in the old clause and absent from the new one; a verdict that fails the gate changes nothing; findings are labelled as advisory | `orgdiff/verify.py::quote_ok`, `stems_present`; `orgdiff/report.py::DISCLAIMER` | `tests/test_guardrails.py` |
| Model answers validated by a JSON schema, not parsed by regex | done: `response_format` with `strict: true` on every call | `orgdiff/verify.py::_ask`, `orgdiff/owners.py`, `orgdiff/report.py::recommend` | code review; any schema violation raises before use |
| Model calls with an explicit timeout, failures become a readable message | done: `ORGDIFF_LLM_TIMEOUT`, 2 retries, error text per failure type | `orgdiff/llm.py` | `tests/test_report_and_app.py::test_llm_client_has_explicit_timeout` |
| Keys only from environment variables | done | `orgdiff/llm.py`, `app.py::load_env` | `.env.example` lists every variable, no values |
| UI: upload, visible progress, results by section, source next to each finding, readable error for a bad file | done | `app.py` | `tests/test_report_and_app.py::test_unparseable_upload_gives_a_clear_error` |
| Owner of a clause block extracted by the model, not by heuristics | done, on when a key is present | `orgdiff/owners.py` | control set: 33 clauses with a bogus heuristic owner corrected, section 5 unchanged |
| Optional: recommendations on redistributing functions and removing overlaps | done: model recommendations whose references are validated against the findings | `orgdiff/report.py::recommend` | sample in `reports/заключение_pdf_xlsx.md` |
| Optional: export of the conclusion to a file | done: .docx and JSON | `orgdiff/report.py::to_docx`, `/job/{id}/conclusion.docx` | `tests/test_report_and_app.py` |
| Works on documents it has never seen | done: generator of synthetic regulations with 8 mutation types and known expectations | `orgdiff/synth.py`, `synth_test.py` | `tests/test_synthetic.py` |
| Docker, CI | done: `Dockerfile`, `docker-compose.yml`, workflow in `ci/orgdiff.yml` | | `docker compose up`, section 8 |

Not implemented, see [10. Limitations](#10-limitations): structural conflict-of-interest
rules over the reporting tree, org charts as pictures, OCR, comparison with legislation and
other operators.

## 4. How it works (user flow)

1. Open `http://localhost:8765`. Two drop zones: the "before" set and the "after" set.
   Any mix of .docx, .pdf and .xlsx per side. The button "Взять контрольный комплект
   организаторов" fills both zones with the control set from `testdata/`.
2. Options: "Проверять спорные места моделью" (gpt-4.1-mini, about 3 minutes and $0.06
   for the control set) and "контрольный комплект" (compare with the known ground truth).
3. Press "Сравнить документы". The waiting page shows the stages, the model-call counter
   and a timer. A job survives a server restart: its status is on disk, and a job that
   was interrupted is reported as such instead of spinning forever. An unreadable file
   gives a readable error ("неподдерживаемый формат: x.txt").
4. The report opens with the key numbers, then the sections: structure changes, lost
   functions (before / after cards, the missing part highlighted), redistributed
   functions, new functions, duplicates, conflicts of interest, a function mapping
   table, audit information (model, calls, cost, verdicts blocked by the quote gate).
   Every reference like "до, п. 5.6.2" is a link to that clause on the document page,
   highlighted in context. Buttons: download the conclusion as .docx, download
   `result.json`.

The agent pipeline behind one click is a sequence of separate steps, each with its own
artefact, not one prompt:

| Step | What happens | Where |
|---|---|---|
| Parse | documents are cut into clauses with composite ids, section, owner and modality; PDF lines are re-flowed into paragraphs; the table of contents is dropped; with a key the owner and modality of each lead phrase are extracted by the model by a strict schema and cached | `load.py`, `parse.py`, `owners.py` |
| Extract structure | departments and positions from the .xlsx table, or from section 3 of the text; created / kept / abolished, staffing changes inside kept departments | `structure.py` |
| Measure similarity | TF-IDF over character n-grams, plus `text-embedding-3-large` when a key is present; embeddings cached on disk | `similarity.py` |
| Align | monotone dynamic programming (Vecalign-style) with 1:1, 1:2 and 2:1 links and gaps, so renumbering, splits and merges do not break the mapping | `align.py` |
| Classify | gaps and weak links become: lost, partially covered, moved, merged into a shared block, removed from an owner, new, added to an owner, modified; the owner is a set of roles, so "Director of DKKM" is covered by "Directors of departments"; a dropped significant word on a high-similarity link sends the pair to review | `analyze.py` |
| Verify | disputed pairs go to the model with a strict JSON schema and a vote of three calls; the lost part must be a quote of the old clause, absent from the new clause by word stems, and under 80% of the old clause; a verdict that fails the gate changes nothing and is counted as blocked | `verify.py` |
| Duplicates and conflicts | clusters of similar clauses across roles inside the new edition confirmed by the model; conflict-of-interest clauses classified as risk / safeguard / mention with a quote gate | `analyze.py`, `verify.py` |
| Conclude | the conclusion is assembled from the result without recomputing anything; recommendations from the model are kept only when their references match real findings; Markdown, .docx, HTML | `report.py`, `app.py` |

## 5. Technologies

- Python 3.12, FastAPI + Uvicorn. Server-rendered HTML with a little vanilla JavaScript,
  no frontend build step.
- numpy, scikit-learn (TF-IDF), pypdf, openpyxl, python-docx, python-multipart.
- OpenAI API: `gpt-4.1-mini` for verification, owner extraction, duplicates, conflicts
  of interest and recommendations; `text-embedding-3-large` for semantic similarity.
  The chat model is set by `ORGDIFF_MODEL`. `gpt-4.1`, `gpt-5-mini` and `gpt-5.1` were
  tried on the same control set and were slower and more literal on partial losses, so
  the default stays `gpt-4.1-mini`; the comparison is in `reports/`.
- uv (dependencies, `uv.lock`), ruff (lint and format), pytest, Docker and Docker Compose.

## 6. Architecture

```
browser ── HTTP ──► app.py (FastAPI, one process)
                      │  POST /analyze          saves files, starts a background thread
                      │  GET  /job/{id}/status  stages, model-call counter (also from disk)
                      │  GET  /job/{id}/report  HTML report, references link to /job/{id}/doc/{side}#c-<clause>
                      │  GET  /job/{id}/conclusion.docx, /job/{id}/result.json, /health
                      ▼
              orgdiff/pipeline.run_analysis(before, after)
   load.py ─► parse.py ─► similarity.py ─► align.py ─► analyze.py ─► verify.py ─► report.py
   docx/pdf    clauses,     tf-idf +         DP with      gap classes,    model +        markdown,
   xlsx        owners.py    embeddings       merges       duplicates,     quote gates    docx, recs
                                                          conflicts
   structure.py  org structure from xlsx or from section 3 of the text
   llm.py        one client factory: timeout, retries, readable error texts
   golden.py     ground truth of the control set (31 items), recall / precision
   synth.py      generator of synthetic regulations with known mutations
```

State on disk, nothing in a database: `.cache/jobs/<id>/` (uploads, `status.json`,
`result.json`, `report.html`, `conclusion.docx`), `.cache/emb.pkl` (embeddings),
`.cache/leads.json` (owner extraction). In Docker this is the `orgdiff-cache` volume.
The container runs as a non-root user and has a healthcheck on `/health`.

Files: `app.py` web UI · `run.py` reference run on the control set with metrics ·
`test_formats.py` PDF and xlsx parsers against docx · `synth_test.py` synthetic pairs ·
`make_testdata.py` builds the xlsx tables · `orgdiff/` the package · `tests/` pytest ·
`testdata/` control set as PDF and xlsx · `reports/` run reports and a sample conclusion ·
`ci/orgdiff.yml` GitHub Actions workflow · `docs/README.ru.md` Russian documentation.

## 7. Installation and run

Docker, recommended (Docker 24+ with Compose v2):

```bash
cd check-theory-kazakhtelecom
cp .env.example .env          # optionally put OPENAI_API_KEY there; leave empty to run without the model
docker compose up -d --build  # first build about 1 minute
curl localhost:8765/health    # {"status":"ok","model":"gpt-4.1-mini","llm":false,"jobs_running":0}
```

Open `http://localhost:8765`. Stop with `docker compose down`; analyses and caches stay
in the `orgdiff-cache` volume.

Locally with uv (Python 3.12, [uv](https://docs.astral.sh/uv/)):

```bash
cd check-theory-kazakhtelecom
uv sync            # creates .venv from uv.lock
uv run app.py      # http://localhost:8765
```

Modes:

- **Without a key**: TF-IDF similarity. Structure changes, full losses, transfers and
  lexical duplicates are found; partial losses are queued as "to be reviewed". Nothing
  leaves the machine.
- **With a key**: hybrid similarity, model verification, owner extraction, duplicate
  confirmation, conflict-of-interest verdicts and recommendations switch on
  automatically. The key is read from the environment, then from `.env` in this folder,
  then from `../.env`.

Environment variables (all in `.env.example`, none has a value there):

| Variable | Meaning | Default |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI key; empty means no model | empty |
| `ORGDIFF_MODEL` | chat model for verification and recommendations | `gpt-4.1-mini` |
| `ORGDIFF_LLM_TIMEOUT` | timeout of one model call, seconds | `60` |
| `ORGDIFF_PORT` | host port published by Docker Compose | `8765` |
| `ORGDIFF_HOST` | bind address of the server (`0.0.0.0` inside Docker) | `127.0.0.1` |

## 8. How to test (a scenario the jury can repeat)

### Scenario A: the case's own "simple verification", in the browser, 2 minutes, no key

1. `docker compose up -d --build`, open `http://localhost:8765`.
2. Click "Взять контрольный комплект организаторов", tick "контрольный комплект",
   press "Сравнить документы".
3. Compare the report with the known changes of the control set:

| Known change in the control set | Expected in the report |
|---|---|
| Reorganisation: departments created | ДИТААД, ДОА (после, п. 3.4) |
| Reorganisation: departments kept | ДККМ, ДНМ, with their changed staffing listed |
| Reorganisation: position abolished | Директор направления внутреннего аудита (до, п. 3.5) |
| Lost functions | 5.6.2, 5.6.3, 5.7.2 of edition 8, each with the nearest clause of edition 9 shown as "not covering" |
| Transfer, not loss | 5.4.4 (ДНМ) → 5.3.3 (directors of departments), section "Переносы" |
| Merged into a shared block, not loss | 5.7.3, 5.7.4, 5.7.5 are absent from "Потери" |
| Duplicates | 5.3.6 ~ 5.4.3, 5.3.12 ~ 5.4.9, 5.3.8 ~ 5.4.5, each with both clauses quoted |
| Conflict of interest | п. 4.4 of edition 9 (participation of the Chief Auditor in governing bodies of subsidiaries), sub-clauses 4.4.а and 4.4.б as safeguards |
| Sources | click any "до, п. …" or "после, п. …": the clause opens highlighted in the document page |
| Ground-truth block at the end | lexical checks 16/16 |

4. Tick "Проверять спорные места моделью" (needs a key) and run again. Now partial
   losses appear with the missing part quoted: 5.7.1 (proposals on assurance and
   consulting programmes), 5.5.5 → 5.5.3 (the "quarterly and annual" periodicity of
   ДККМ reports), 5.6.7 (narrowing from all BVA staff to department staff), 5.3.4.а and
   5.3.4.б. Duplicates 5.3.6 ~ 5.5.8 and 5.3.7 ~ 5.5.5 are confirmed by meaning. The
   "control set" block shows lexical 16/16, semantic 2/2, llm 10/10.

### Scenario B: the command line

```bash
uv run pytest -q                                    # 40 tests; the model test is skipped without a key
uv run ruff check . && uv run ruff format --check . # lint and format
uv run run.py --mode tfidf                          # reference run, prints "GOLDEN lexical 16/16", exit code 0
uv run run.py --mode hybrid --verify --tau-dup 0.5  # with the model, ~3 min, ~$0.05, prints recall and precision
uv run synth_test.py --n 16 --seed 7                # 16 synthetic pairs with known changes, report in reports/synth.md
```

`run.py` uses the organisers' .docx from `../TZ` when present and otherwise the same
documents as PDF from `testdata/`, so the folder is self-contained. Reports of the runs
below are in `reports/`.

### Measured results

Control set, 31 ground-truth items (`orgdiff/golden.py`): 5 structure, 3 full losses,
6 partial losses, 3 merges, 1 transfer, 5 duplicates and 1 conflict of interest,
plus 3 "must not be reported as lost" checks.

| Input | Configuration | Recall | Precision on reported losses | Cost per run |
|---|---|---|---|---|
| pdf + xlsx | no model (TF-IDF) | 27/31 (87%) | 3/4 (75%) | 0 |
| pdf + xlsx or docx | hybrid + gpt-4.1-mini, owners and conflicts by the model | 30–31/31 (97–100%) | 9/9 (100%) | $0.05–0.06 |

The latest verified run (`reports/gpt-4.1-mini-final.md`) scored 31/31 and 9/9; the run
behind the sample conclusion (`reports/заключение_pdf_xlsx.md`) scored 30/31 and 9/9.
One partial loss (5.3.4.а) can flip between runs because the model is non-deterministic;
the vote of three and the quote gate keep everything else stable.

Documents the pipeline never saw: `orgdiff/synth.py` builds regulations from a pool of
real wording with 3–6 departments and applies 3–6 known mutations of 8 types (loss,
partial loss, transfer, duplication, department created, department abolished,
paraphrase without change of meaning, insertion with renumbering). Part of the pairs is
converted to PDF.

| Run | Expected changes | Recall | False losses reported |
|---|---|---|---|
| 16 pairs, no model, 4 of them as PDF (`reports/synth-hybrid.md`) | 126 | 88% | 0 of 48 |
| 4 pairs, gpt-4.1-mini (`reports/synth-verified.md`) | 23 | 91% | 0 of 12 |

### CI

`ci/orgdiff.yml` runs lint, format check, the 39 key-free tests, the reference run and
a Docker smoke test over HTTP (upload of the control set, wait for the job, check
recall ≥ 85% and the .docx download). Copy it to `.github/workflows/orgdiff.yml` at the
repository root to activate; it triggers on changes under this folder.

## 9. Data and integrations

- Input data: the organisers' control set, the Regulation on Internal Audit, editions 8
  and 9, anonymised. `testdata/` holds the same documents converted to PDF and the
  org-structure tables built from section 3 of each edition as .xlsx
  (`make_testdata.py`). No production or personal data is used.
- External service: the OpenAI API only, and only when a key is set. What is sent: pairs
  of clause texts, batches of lead phrases, clusters of clauses, and the list of findings
  for recommendations. Never a whole document. Nothing is sent without a key.
- Storage: files under `.cache/`; no database and no other services.

## 10. Limitations

- Conflicts of interest are found where the text mentions them (keywords) and then
  classified by the model. There is no structural rule over the reporting tree, for
  example "the same person performs and controls a process", because the input
  documents do not describe subordination reliably enough to infer it.
- Org structure is read from the .xlsx table or from section 3 of the text. Org charts
  embedded as pictures are not read.
- Scanned PDFs without a text layer are not supported; there is no OCR.
- Without a key partial losses are queued as "to be reviewed" rather than decided, and
  recall on the control set drops to 87%.
- The model is non-deterministic. The reported findings are stable thanks to the vote of
  three and the quote gate, but the wording of recommendations differs between runs.
- Optional items of the case that are not done: comparison of functions with legislation
  and standards, and comparison with other operators by open data.
- The UI runs jobs in a thread of the single web process. Fine for one auditor, not for
  a shared installation with many parallel analyses.
- The parsers are tuned on Russian regulations with numbered clauses (`5.3.2`, `а.`,
  dashes). A document without clause numbering is parsed as flat paragraphs and loses
  owner inheritance.

## 11. Deployment

No public deployment. The service runs locally or on any host with Docker:
`docker compose up -d --build`, then `http://<host>:8765`.

## 12. Value, originality and roadmap

**Value.** The manual comparison of two editions of a 30-page regulation takes an auditor
days and still misses narrowings like "quarterly and annual" disappearing from a
reporting duty. OrgDiff turns it into a three-minute run whose every finding can be
checked in one click, and the conclusion is already in the shape the auditor would
write. The same run can be repeated for every regulation of every department after a
reorganisation, so the approach scales to a whole company's document base.

**What is original in the approach.**

- Coverage instead of matching: the question asked of the new edition is "is this
  function still covered somewhere", not "does this clause have a twin", so transfers,
  merges and rewordings are not reported as losses. The owner of a clause is a set of
  roles, which is what makes "Director of DKKM" covered by "Directors of departments".
- Sentence-alignment techniques from machine translation applied to regulations:
  monotone dynamic programming with 1:2 and 2:1 links survives renumbering, splits and
  merges without any rules about clause numbers.
- The model is used only where the text is ambiguous, and never trusted blindly: strict
  JSON schemas, a vote of three calls, and mechanical gates that require every quote to
  be a substring of the source and every "lost part" to be present in the old clause and
  absent from the new one. Blocked verdicts are counted and shown in the report.
- A generator of synthetic regulations with known mutations, so the pipeline is tested
  on documents it was never tuned on, and false losses are measured, not assumed.

**Roadmap.**

1. Structural conflict-of-interest rules over the reporting tree once the org structure
   carries subordination (the xlsx format already has a "reports to" column).
2. Mapping of functions to legislation and standards (the optional item of the case):
   the same coverage machinery with a third document set.
3. Batch mode over a whole document base and a comparison across companies by open
   data.
4. OCR for scanned PDFs and reading of org charts.
5. A local model behind the same `llm.py` interface for installations where documents
   must not leave the network.
