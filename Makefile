.PHONY: help install run docker-build docker-run

help: ## Show available targets.
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

install: ## Create a venv and install dependencies.
	python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

run: ## Run the demo locally on :8000.
	./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

docker-build: ## Build the demo image.
	docker build -t adoption-risk-analyzer-demo .

docker-run: ## Run the demo image on :8000.
	docker run --rm -p 8000:8000 adoption-risk-analyzer-demo
