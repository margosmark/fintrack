# fin_tracker

Self-hosted personal finance tracker for a Canadian user:

- **Spending tracking** across banks and credit cards via OFX/QFX and CSV import
- **Investments** (CAD + USD listings) with positions, dividend income analytics, future-value projections, security fundamentals, ETF/MF fund composition, and look-through exposure by sector/geography/asset class; LIRA/RRSP/TFSA/FHSA/RESP support
- **Canadian tax engine**: ACB pools, superficial loss, ROC, Schedule-3-style capital-gains reports
- **Reporting & graphs**: spending, cash flow, net-worth history, investment performance, dividend income, portfolio projections — CSV everywhere
- **Privacy mode**: full data export, encrypted browser backup, external-call lockdown
- **Local-first AI**: categorization via your own Ollama (or any OpenAI-compatible endpoint)

Blueprint docs live in [`docs/`](docs/) (`00` overview → `14` accounts). Milestones M1–M18 are implemented.

For the Android discovery lane, start with [`docs/49`](docs/49-android-companion-recommendation.md), then [`docs/50`](docs/50-android-native-capability-spike.md). Building the Android app and then a home-screen widget is approved: see [`docs/51`](docs/51-android-home-screen-widgets.md) for the binding widget boundaries (visibility toggle, *last checked* freshness, no production writes).

## Run it on your server

Requirements: Docker 24+ with the compose plugin. Ports used: **8080** (web), 8000 (api, optional to expose), 5432 (db — only if you want host-side psql).

```bash
git clone https://github.com/margosmark/fintrack.git
cd fintrack

cp .env.example .env
# EDIT .env — at minimum set a real JWT_SECRET (any long random string).
# Personal install? set SEED_DEMO_ACCOUNT=false so no demo tenant is created.

docker compose up -d --build
```

Open **http://your-server:8080** and register your account (or click "Explore the demo" — disable via `SEED_DEMO_ACCOUNT=false` + `docker compose up -d`).

> `DATABASE_URL` inside `docker-compose.yml` is pinned to the `db` container — the `.env` copy of it is only for host-side tooling/tests. Compose `environment` wins over `env_file`.

## Updating

```bash
git pull
docker compose up -d --build   # migrations run automatically on api start
```

## Backups

```bash
docker compose exec db pg_dump -U fintrack fintrack > fintrack-$(date +%F).sql
```

Restore: `cat backup.sql | docker compose exec -T db psql -U fintrack fintrack` (on a fresh volume).

## TLS / domain

The stack serves plain HTTP on 8080. Put a reverse proxy in front for HTTPS — e.g. Caddy:

```
money.example.com {
    reverse_proxy localhost:8080
}
```

## Development

Backend: FastAPI + SQLModel + Alembic (`backend/`), tests against real Postgres. Frontend: React + Vite + Tailwind (`frontend/`), `npm run dev` proxies `/api` to `localhost:8000`. CI runs ruff + pytest + tsc/eslint/build on every push. See `docs/13-agent-execution.md` for the full protocol.

## License

MIT — see [LICENSE](LICENSE). Third-party dependency licenses (backend and frontend lockfiles) are inventoried in [NOTICE](NOTICE), with restrictive-license flags called out there explicitly.
