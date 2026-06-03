.PHONY: help install skills run openrouter resume test clean clean-cache

# Sources (SVD/DTS/PDF) come from tlde.toml [sources]; this is just the request.
PROMPT  ?= Emulate the nRF52833 micro:bit v2
CONFIG  ?= tlde.toml

help:  ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install:  ## Install the package + dependencies
	uv sync

skills:  ## Install Renode skills into ~/.copilot/skills/
	@for skill in docs/skills/*.md; do \
	  name=$$(basename "$$skill" .md); \
	  mkdir -p ~/.copilot/skills/$$name; \
	  printf -- '---\nname: %s\ndescription: "%s"\n---\n' "$$name" "$$(sed -n '3p' "$$skill")" \
	    > ~/.copilot/skills/$$name/SKILL.md; \
	  cat "$$skill" >> ~/.copilot/skills/$$name/SKILL.md; \
	done
	@echo "Installed $$(ls docs/skills/*.md | wc -l) skills to ~/.copilot/skills/"

run:  ## Run the pipeline (default provider from tlde.toml)
	uv run tlde "$(PROMPT)" --config $(CONFIG)

# OpenRouter (open-weight models): put OPENROUTER_API_KEY in a .env file
# (auto-loaded, gitignored) or export it. Never commit secrets.
# Per-role open-weight model selection lives in examples/tlde.openrouter.toml.
openrouter:  ## Run on OpenRouter using open-weight models
	uv run tlde "$(PROMPT)" --config examples/tlde.openrouter.toml --provider openrouter

resume:  ## Re-run from a cached work plan (skips the Manager)
	uv run tlde "$(PROMPT)" --config $(CONFIG) --plan output/work_plan.json

test:  ## Run the test suite
	uv run pytest tests/ -q

clean-cache:  ## Remove the ingestion cache only
	rm -rf .tlde_cache

clean: clean-cache  ## Remove generated outputs and the ingestion cache
	rm -rf output
	@echo "Removed output/ and .tlde_cache/ (regenerated on the next run)"
