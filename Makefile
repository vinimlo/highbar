.PHONY: test lint format docs-dev docs-build docs-preview docs-install docs-up docs-down

# Python project
test:
	uv run pytest -v

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

# Documentation
docs-install:
	cd site && npm install

docs-dev:
	cd site && npm run dev

docs-build:
	cd site && npm run build

docs-preview:
	cd site && npm run preview

# Documentation (Docker)
docs-up:
	docker compose up --build -d

docs-down:
	docker compose down
