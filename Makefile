.PHONY: help dev dev-aws test seed login google-login claude-key twilio telegram telegram-local

help:  ## List the commands
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-10s %s\n", $$1, $$2}'

dev:  ## Install dependencies and run locally (uses Ollama if it's running)
	@./scripts/dev.sh

dev-aws:  ## Run locally against the deployed AWS stack (DynamoDB, SNS) with Ollama
	@./scripts/run_local_aws.sh

test:  ## Run the app and infrastructure tests
	uv run --group infra pytest tests infra/tests

seed:  ## Reset local data to the demo household
	uv run kinvesper --seed

login:  ## Create a family login (dashboard sign-in, Alexa account linking): make login EMAIL=you@example.com
	@./scripts/create_login.sh "$(EMAIL)"

google-login:  ## Store the Google OAuth client secret for "Continue with Google" (then deploy with -c googleClientId=...)
	@./scripts/set_google_login.sh

claude-key:  ## Store the Claude API key for the deployed voice agent (deploy with -c llmProvider=anthropic)
	@./scripts/set_claude_key.sh

twilio:  ## Store the Twilio credentials for urgent-alert phone calls (from .env, or asks)
	@./scripts/set_twilio.sh

telegram:  ## Connect the Telegram alerts bot to the deployed server (TELEGRAM_BOT_TOKEN in .env)
	@uv run python telegram/setup.py

telegram-local:  ## Relay the Telegram bot to the local server while it runs
	@uv run python telegram/poll.py
