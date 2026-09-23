# HackAlem AI: two case solutions

This repository holds two independent solutions built by our team at the HackAlem AI
hackathon. Each lives in its own folder with its own code, tests, Docker setup and
README, and runs on its own. The folders share nothing at runtime.

| Project | Case | Folder | README |
|---|---|---|---|
| **ZHEL.ai** | Agentic AI for an hourly 24–48 h power forecast of the Shelek wind farm | [zhel-ai/](zhel-ai/) | [zhel-ai/README.md](zhel-ai/README.md) |
| **OrgDiff** | AI agent: analysis of organisational structure and functions | [check-theory-kazakhtelecom/](check-theory-kazakhtelecom/) | [check-theory-kazakhtelecom/README.md](check-theory-kazakhtelecom/README.md) |

Each project README follows the eleven sections required by the organisers: problem and
users, what is implemented, user flow, technologies, architecture, installation, a test
scenario the jury can repeat, data, limitations and deployment.

## ZHEL.ai

An agent that issues an hourly P10 / P50 / P90 power forecast for the next 48 hours for
each of the two turbines of the Shelek wind farm. It uses only the weather forecasts
published by the issue time, and replays all of February 2026 as if each forecast were
made on its own day. No API keys and no network are needed after the first build.

```bash
cd zhel-ai
cp .env.example .env
make demo              # then open http://localhost:3000, log in as admin / admin
```

## OrgDiff

An agent that compares the "before" and "after" document sets of a reorganisation
(.docx, .pdf, .xlsx) and reports changed units, lost, moved and duplicated functions and
conflicts of interest, with a document, clause and verbatim quote behind every finding.
Runs without a key; an OpenAI key switches on model verification.

```bash
cd check-theory-kazakhtelecom
cp .env.example .env           # OPENAI_API_KEY is optional
docker compose up -d --build   # then open http://localhost:8765
```

The two stacks use different ports (ZHEL.ai: 3000, 8000, 8010, 8020, 5432; OrgDiff: 8765)
and can run at the same time.

## Repository layout

```text
zhel-ai/                      wind power forecast: backend, ML service, dashboard, data
check-theory-kazakhtelecom/   organisational structure and functions analysis
.github/workflows/            CI: ci.yml and audit.yml for ZHEL.ai, orgdiff.yml for OrgDiff
rules/                        hackathon rules summary
```
