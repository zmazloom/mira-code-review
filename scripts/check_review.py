"""Read-only review configuration and connectivity checks; never print secrets."""

import os
from urllib.parse import urlparse

import httpx

from mira.config import load_config
from mira.dashboard.models_config import llm_config_for

config = llm_config_for("review", load_config(os.getenv("MIRA_CONFIG")).llm)
print("GitHub token present:", bool(os.getenv("GITHUB_TOKEN")))
print("Model provider:", config.provider)
print("Model host:", urlparse(config.base_url).hostname)
print("Model key variable:", config.api_key_env)
print("Model key present:", bool(os.getenv(config.api_key_env)) if config.api_key_env else "Not required")

checks = [("GitHub authentication", "https://api.github.com/user", os.getenv("GITHUB_TOKEN", ""))]
if urlparse(config.base_url).hostname == "openrouter.ai":
    checks.append(("OpenRouter authentication", "https://openrouter.ai/api/v1/auth/key", os.getenv(config.api_key_env, "")))
with httpx.Client(timeout=15) as client:
    for name, url, token in checks:
        if not token:
            print(name + ": token missing")
            continue
        try:
            response = client.get(url, headers={"Authorization": "Bearer " + token})
            print(name + " HTTP:", response.status_code)
        except Exception as exc:
            print(name + " connectivity:", type(exc).__name__)
