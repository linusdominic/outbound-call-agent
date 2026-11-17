.PHONY: install model personas campaign simulate simulate-baseline report serve test lint

SET ?= heldout

install:
	uv sync

model: ## only for the voice demo: the Whisper base.en model, about 150 MB, into models/
	mkdir -p models
	curl -L --fail -o models/ggml-base.en.bin \
		https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin

personas: ## free: rebuild both sets of simulated contacts
	uv run callagent personas

campaign: ## free: which leads in data/leads.csv may be called right now, and why not
	uv run callagent campaign

simulate: ## paid, about 55 cents for the 200 held-out contacts: rules enforced in code
	uv run callagent simulate --set $(SET) --mode guarded

simulate-baseline: ## paid, about 60 cents: the same calls with the rules in the prompt only
	uv run callagent simulate --set $(SET) --mode prompt_only

report: ## free: rebuild results/report.md from the recorded calls
	uv run callagent evaluate --write

serve: ## talk to the agent on http://localhost:8030 (needs ANTHROPIC_API_KEY)
	uv run callagent serve

test: ## no network, microphone or API key needed
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
