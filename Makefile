# Replaces the four-command sequence in the README with named targets, so the
# stage dependency order is recorded in one place rather than in prose.
# Windows: run these under Git Bash, or invoke the underlying python commands directly.

DB ?= data/processed/nhs.duckdb
PY ?= python

.PHONY: help install all pipeline download-dental etl dq cp corr tier3 reporting views brief fixture demo test test-hermetic lint docker docker-reporting dashboard clean

help:
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-14s %s\n", $$1, $$2}'

install:  ## Install runtime + dev dependencies and the project itself
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -e ".[dev]"

download-dental:  ## Fetch NHSBSA dental months over HTTP (no browser). A&E/RTT: python -m etl.download (needs the ingest extra)
	$(PY) -m etl.download --datasets dental

etl:  ## Stage 0 — load the four raw datasets into DuckDB (unchanged files are skipped; FORCE=1 reloads all)
	$(PY) etl/run_all.py $(if $(FORCE),--force,)

dq: etl  ## Stage 1 — data quality drift checks
	$(PY) dq/run_all.py

cp: dq  ## Stage 2 — SPC baseline + PELT changepoint detection
	$(PY) cp/run_all.py

corr: cp  ## Stage 3 — cross-dataset correlation (Tiers 1 and 2)
	$(PY) corr/run_all.py

pipeline: corr  ## Run stages 0-3 end to end

all: pipeline reporting  ## Stages 0-3, then load PostgreSQL and apply the views (needs .env)

tier3: corr  ## Stage 3 Tier 3 — needs network: one NHS ODS API call per trust
	$(PY) corr/reference/trust_to_icb.py
	$(PY) corr/tier3_signal_driven.py

reporting:  ## Full-refresh the PostgreSQL "reporting" schema from $(DB) (needs .env, see .env.example)
	$(PY) reporting/load_postgres.py $(DB)

views:  ## Create/refresh the reporting.* Power BI views from sql/ (run `make reporting` first)
	$(PY) reporting/apply_views.py

test:  ## Full suite (data-dependent tests run only if the DuckDB file exists)
	$(PY) -m pytest -q -ra

test-hermetic:  ## Only the tests that need no data — what CI actually exercises
	cd /tmp && $(PY) -m pytest "$(CURDIR)/tests" -q

lint:  ## ruff
	$(PY) -m ruff check .

brief:  ## Regenerate docs/executive_findings.md from $(DB) (every figure comes from a query; run after `make pipeline`)
	$(PY) reporting/generate_executive_brief.py $(DB)

fixture:  ## Build the small synthetic DuckDB used by CI -> data/processed/fixture.duckdb (try: make reporting DB=data/processed/fixture.duckdb)
	$(PY) tests/fixtures/build_fixture_db.py data/processed/fixture.duckdb

demo:  ## 60-second demo: build the synthetic fixture and serve the dashboard on it (no downloads, no database needed)
	$(PY) tests/fixtures/build_fixture_db.py data/processed/fixture.duckdb
	NHS_DB_PATH=data/processed/fixture.duckdb $(PY) -m streamlit run app/dashboard.py

docker:  ## Build the image
	docker build -t nhs-monitoring-copilot:latest .

docker-reporting:  ## Run the pipeline, then load a containerised PostgreSQL (docker compose profile "reporting")
	docker compose --profile reporting up --build postgres reporting

dashboard:  ## Serve the Streamlit dashboard against $(DB)
	$(PY) -m streamlit run app/dashboard.py

clean:  ## Remove caches and generated plots (never touches data/)
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache
