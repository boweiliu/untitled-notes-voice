"""OpenCode models pi still offers but OpenCode has retired. Written by tools/find_retired_models.py;
rerun it rather than editing by hand.

Keys are pi's ``provider/model`` tags. A value is OpenCode's suggested replacement, or an empty
string when it named none (or no longer serves the model at all).
"""

# Checked 2026-11-03 against pi 0.87.1's catalog.
RETIRED = {
    'opencode-go/deepseek-v4-flash': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode-go/deepseek-v4-flash-vision-exp': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode-go/glm-5.1': 'glm-5.3',  # retired: Model glm-5.1 has been deprecated. Use glm-5.3 instead.
    'opencode-go/grok-4.6': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode-go/kimi-k2.6': 'kimi-k2.7-code',  # retired: Model kimi-k2.6 has been deprecated. Use kimi-k2.7-code instead.
    'opencode-go/mimo-v2.5': 'mimo-v2.6-flash',  # retired: Model mimo-v2.5 has been deprecated. Use mimo-v2.6-flash instead.
    'opencode-go/mimo-v2.5-pro': 'mimo-v2.6-pro',  # retired: Model mimo-v2.5-pro has been deprecated. Use mimo-v2.6-pro instead.
    'opencode-go/minimax-m2.7': 'minimax-m3.1-flash',  # retired: Model minimax-m2.7 has been deprecated. Use minimax-m3.1-flash instead.
    'opencode-go/qwen3.6-plus': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode-go/qwen3.7-max': 'qwen3.8-max',  # retired: Model qwen3.7-max has been deprecated. Use qwen3.8-max instead.
    'opencode/claude-sonnet-4': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode/deepseek-v4-flash': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode/deepseek-v4-flash-vision-exp': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode/gemini-3-flash': '',  # retired: Model gemini-3-flash has been deprecated.
    'opencode/gemini-3.1-pro': '',  # retired: Model gemini-3.1-pro has been deprecated.
    'opencode/ling-3.0-flash-fin-free': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode/muse-spark-1.2-contributor-free': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode/muse-spark-1.3-contributor-free': '',  # unavailable: Upstream request failed: Model is unavailable.
    'opencode/nemotron-3-ultra-free': '',  # unavailable: Upstream request failed: Model is unavailable.
}


def is_retired(provider: str, model_id: str) -> bool:
    return f"{provider}/{model_id}" in RETIRED


def replacement(tag: str) -> str:
    """The model OpenCode suggests instead of a retired one, as a pi tag, or an empty string."""
    provider, _, _ = tag.partition("/")
    return f"{provider}/{RETIRED[tag]}" if RETIRED.get(tag) else ""
