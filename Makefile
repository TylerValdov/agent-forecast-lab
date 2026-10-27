PY ?= python

.PHONY: install test lint data baselines pilot main live-predict live-resolve report

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -q

lint:
	ruff check src tests

data:
	forecast-lab data refresh

baselines:
	forecast-lab baselines

pilot:
	forecast-lab run configs/experiments/pilot_v1.yaml --exploratory

main:
	forecast-lab run configs/experiments/main_v1.yaml

live-predict:
	forecast-lab live predict

live-resolve:
	forecast-lab live resolve

report:
	forecast-lab report
