SP ?= .
DB ?= $(SP)/sim.db

.PHONY: help sim sim-fast sim-stop schema ingest eval test clean

help:
	@echo "sim        continuous simulator, 20/s, until Ctrl+C"
	@echo "sim-fast   flat out, 60 seconds"
	@echo "schema     apply db/migrations to \$$PG_DSN"
	@echo "ingest     build the index from corpus/ and the platform docs"
	@echo "eval       run the question set, keyword mode"
	@echo "test       pytest"

sim:
	python sim/simulate.py --db $(DB)

sim-fast:
	python sim/simulate.py --rate 0 --for 60s --db $(DB)

schema:
	@test -n "$(PG_DSN)" || (echo "set PG_DSN"; exit 1)
	psql "$(PG_DSN)" -f db/migrations/001_schema.sql

ingest:
	python -m app.ingest.pipeline corpus/ ../microfinance-microservices/docs/

eval:
	python eval/run_eval.py --mode keyword

test:
	pytest -q

clean:
	rm -f *.db *.db-wal *.db-shm
	find . -name __pycache__ -type d -exec rm -rf {} +
