PY ?= .venv/bin/python

.PHONY: install check test web demo docker

install:
	python3 -m venv .venv
	$(PY) -m pip install -r requirements.txt
	cd web && npm ci
	@command -v soffice >/dev/null || echo "LibreOffice not found: DOCX files are read from their embedded images only (brew install --cask libreoffice)"

check: test web

test:
	$(PY) -m pytest -q

web:
	cd web && npm run lint && npm test && npm run build

demo:
	DB_PATH=data/demo.sqlite $(PY) -m fixtures.demo_data

docker:
	docker build -t borehole-atlas .
	docker run --rm -p 8000:8000 borehole-atlas
