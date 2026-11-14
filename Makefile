VENV := .venv
PY   := $(VENV)/bin/python

.PHONY: help venv test up down logs bench clean

help:
	@echo "make test   run the unit tests (creates .venv on first use)"
	@echo "make up     build and start the stack"
	@echo "make down   stop the stack, keeping volumes"
	@echo "make logs   follow gateway logs"
	@echo "make bench  run the default benchmark sweep against a running stack"
	@echo "make clean  remove the test venv"

$(VENV):
	python3 -m venv $(VENV)
	$(PY) -m pip install --quiet --upgrade pip
	$(PY) -m pip install --quiet -r tests/requirements.txt

venv: $(VENV)

# Tests import the gateway package directly; no running stack or backend needed.
test: $(VENV)
	$(PY) -m pytest tests/ -q

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f gateway

bench: $(VENV)
	$(PY) benchmark/bench.py

clean:
	rm -rf $(VENV)
