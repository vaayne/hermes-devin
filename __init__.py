"""Devin / Windsurf Cascade provider — talks the Connect-RPC + protobuf API
directly, no external gateway. Protocol ported from devin-gateway
(https://github.com/CaiJingLong/devin-gateway).

Credentials resolve in order:
  1. DEVIN_API_KEY env var ($HERMES_HOME/.env is loaded into the environment)
  2. Devin CLI login — .local/share/devin/credentials.toml (windsurf_api_key)
  3. devin-gateway login — .devin-gateway/token
File lookups probe every home Hermes uses: the process home, $HERMES_HOME,
and the profile home $HERMES_HOME/home — so a login run inside an agent shell
session still resolves for the gateway.
DEVIN_BASE_URL overrides the API endpoint (default https://server.codeium.com;
also auto-read from the Devin CLI credential store's api_server_url).
"""

import logging
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

from ._client import DevinCascadeClient, bridge_credentials_to_env, resolve_credentials
from ._cascade import DEVIN_API_URL, discover_models

logger = logging.getLogger(__name__)

# Hermes resolves api_key providers from env vars only; mirror the Devin CLI /
# devin-gateway credential stores into DEVIN_API_KEY / DEVIN_BASE_URL so
# `hermes --provider devin`, doctor and the model picker all work after a
# single `devin auth login`. Explicit env vars always win (setdefault inside).
try:
    bridge_credentials_to_env()
except Exception:
    logger.debug("devin: credential bridging skipped", exc_info=True)


class DevinProfile(ProviderProfile):
    """Devin Cascade — Connect-RPC wire, custom client, live model catalog."""

    def create_client(self, **client_kwargs: Any) -> Any:
        return DevinCascadeClient(**client_kwargs)

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        key = (api_key or "").strip()
        base = (base_url or "").strip()
        if not key or not base:
            file_token, file_base = resolve_credentials()
            key = key or file_token
            base = base or file_base or ""
        if not key:
            return None
        try:
            models = discover_models(key, base or DEVIN_API_URL, timeout=timeout)
        except Exception as exc:
            logger.debug("devin: model discovery failed: %s", exc)
            return None
        return [m["id"] for m in models] or None


# Static catalog from devin-gateway's src/models.ts — shown only when the live
# GetCliModelConfigs fetch fails (no token, offline). Live catalog is canonical.
_FALLBACK_MODELS = (
    "claude-5-fable-low", "claude-5-fable-medium", "claude-5-fable-high",
    "claude-5-fable-xhigh", "claude-5-fable-max",
    "claude-opus-4-6", "claude-opus-4-6-1m",
    "claude-opus-4-7-low", "claude-opus-4-7-medium", "claude-opus-4-7-high",
    "claude-opus-4-7-xhigh", "claude-opus-4-7-max",
    "claude-opus-4-8-low", "claude-opus-4-8-medium", "claude-opus-4-8-high",
    "claude-opus-4-8-xhigh", "claude-opus-4-8-max",
    "claude-opus-4-8-low-fast", "claude-opus-4-8-medium-fast",
    "claude-opus-4-8-high-fast", "claude-opus-4-8-xhigh-fast", "claude-opus-4-8-max-fast",
    "claude-sonnet-4-6", "claude-sonnet-4-6-1m",
    "claude-sonnet-5-low", "claude-sonnet-5-medium", "claude-sonnet-5-high",
    "claude-sonnet-5-xhigh", "claude-sonnet-5-max",
    "deepseek-v4",
    "gemini-3-1-pro-low", "gemini-3-1-pro-high",
    "gemini-3-5-flash-minimal", "gemini-3-5-flash-low",
    "gemini-3-5-flash-medium", "gemini-3-5-flash-high",
    "MODEL_GOOGLE_GEMINI_3_0_FLASH_MINIMAL", "MODEL_GOOGLE_GEMINI_3_0_FLASH_LOW",
    "MODEL_GOOGLE_GEMINI_3_0_FLASH_MEDIUM", "MODEL_GOOGLE_GEMINI_3_0_FLASH_HIGH",
    "glm-5-2", "glm-5-2-none", "glm-5-2-max",
    "glm-5-2-1m", "glm-5-2-none-1m", "glm-5-2-max-1m",
    "MODEL_GPT_5_2_NONE", "MODEL_GPT_5_2_LOW", "MODEL_GPT_5_2_MEDIUM",
    "MODEL_GPT_5_2_HIGH", "MODEL_GPT_5_2_XHIGH",
    "gpt-5-3-codex-low", "gpt-5-3-codex-medium", "gpt-5-3-codex-high", "gpt-5-3-codex-xhigh",
    "gpt-5-3-codex-low-priority", "gpt-5-3-codex-medium-priority",
    "gpt-5-3-codex-high-priority", "gpt-5-3-codex-xhigh-priority",
    "gpt-5-4-none", "gpt-5-4-low", "gpt-5-4-medium", "gpt-5-4-high", "gpt-5-4-xhigh",
    "gpt-5-4-none-priority", "gpt-5-4-low-priority", "gpt-5-4-medium-priority",
    "gpt-5-4-high-priority", "gpt-5-4-xhigh-priority",
    "gpt-5-4-mini-low", "gpt-5-4-mini-medium", "gpt-5-4-mini-high", "gpt-5-4-mini-xhigh",
    "gpt-5-5-none", "gpt-5-5-low", "gpt-5-5-medium", "gpt-5-5-high", "gpt-5-5-xhigh",
    "gpt-5-5-none-priority", "gpt-5-5-low-priority", "gpt-5-5-medium-priority",
    "gpt-5-5-high-priority", "gpt-5-5-xhigh-priority",
    "gpt-5-6-luna-none", "gpt-5-6-luna-low", "gpt-5-6-luna-medium",
    "gpt-5-6-luna-high", "gpt-5-6-luna-xhigh", "gpt-5-6-luna-max",
    "gpt-5-6-luna-none-priority", "gpt-5-6-luna-low-priority",
    "gpt-5-6-luna-medium-priority", "gpt-5-6-luna-high-priority",
    "gpt-5-6-luna-xhigh-priority", "gpt-5-6-luna-max-priority",
    "gpt-5-6-sol-none", "gpt-5-6-sol-low", "gpt-5-6-sol-medium",
    "gpt-5-6-sol-high", "gpt-5-6-sol-xhigh", "gpt-5-6-sol-max",
    "gpt-5-6-sol-none-priority", "gpt-5-6-sol-low-priority",
    "gpt-5-6-sol-medium-priority", "gpt-5-6-sol-high-priority",
    "gpt-5-6-sol-xhigh-priority", "gpt-5-6-sol-max-priority",
    "gpt-5-6-terra-none", "gpt-5-6-terra-low", "gpt-5-6-terra-medium",
    "gpt-5-6-terra-high", "gpt-5-6-terra-xhigh", "gpt-5-6-terra-max",
    "gpt-5-6-terra-none-priority", "gpt-5-6-terra-low-priority",
    "gpt-5-6-terra-medium-priority", "gpt-5-6-terra-high-priority",
    "gpt-5-6-terra-xhigh-priority", "gpt-5-6-terra-max-priority",
    "grok-4-5-low", "grok-4-5-medium", "grok-4-5-high",
    "kimi-k2-6", "kimi-k2-7",
    "nemotron-3-ultra-nvfp4",
    "swe-1-6", "swe-1-6-fast", "swe-1-7", "swe-1-7-lightning",
)


devin = DevinProfile(
    name="devin",
    aliases=("devin-gateway", "windsurf", "cascade"),
    display_name="Devin (Cascade)",
    description="Devin/Windsurf Cascade — direct Connect-RPC, Devin CLI auth",
    signup_url="https://github.com/CaiJingLong/devin-gateway",
    env_vars=("DEVIN_API_KEY", "DEVIN_BASE_URL"),
    base_url=DEVIN_API_URL,
    auth_type="api_key",
    # Catalog is a Connect-RPC (GetCliModelConfigs), not GET {base}/models —
    # the doctor probe would 404. fetch_models() still feeds the picker.
    supports_health_check=False,
    default_aux_model="gpt-5-4-mini-low",
    fallback_models=_FALLBACK_MODELS,
)

register_provider(devin)
