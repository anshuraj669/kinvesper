.PHONY: help dev dev-aws test seed

help:  ## List the commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'

dev:  ## Install dependencies and run locally (uses Ollama if it's running)
	@./scripts/dev.sh

dev-aws:  ## Run locally against the deployed AWS stack (DynamoDB, SNS) with Ollama
	@./scripts/run_local_aws.sh

test:  ## Run the app and infrastructure tests
	uv run --group infra pytest tests infra/tests

seed:  ## Reset local data to the demo household
	uv run kinvesper --seed
