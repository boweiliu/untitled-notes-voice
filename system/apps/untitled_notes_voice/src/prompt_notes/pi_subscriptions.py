"""pi-dev's subscriptions: which pi account a new chat runs on, and the models each one offers.

On the chat app, a pi account holds one provider's credential (its ``auth.json``). The notes app
calls that account a subscription and always offers the three below, in this order, whether or not
the machine has them yet (one not set up is shown as not connected). OpenCode is one slot for
either of its plans: Zen (pay per use, provider ``opencode``) or Go (the subscription, ``opencode-go``). Any other pi account on the
machine (an OpenRouter key, say) is offered after them under its own name.

Claude runs through the Claude Code add-on for pi (``claude-code/...`` models, set up by
``deploy/pi_subscriptions/setup_pi_subscriptions.py``), so its turns count against the Claude plan
rather than API billing. The "Claude subscription (shared)" account (``meridian/...`` models, set up by
the same script's ``claude-shared``) runs the same Claude login through one shared bridge for the
whole machine, which keeps no Claude process alive between model steps; it is offered under its own
name after the three. Codex is pi's ChatGPT models (``openai-codex/...``) on a Codex CLI login the
machine already has (``codex-subscription-login.ts``), or on pi's own ChatGPT sign-in; OpenCode is a
key pasted into the chat app.
"""
import re

from prompt_notes import retired_models

SUBSCRIPTIONS = (
    {"id": "opencode", "label": "OpenCode", "providers": ("opencode", "opencode-go")},
    {"id": "claude", "label": "Claude", "providers": ("claude-code",)},
    {"id": "codex", "label": "Codex (ChatGPT)", "providers": ("openai-codex",)},
)
NOT_CONNECTED = "Not set up on this machine"
# An OpenCode account is named for the plan its key is on.
PROVIDER_LABELS = {"opencode": "OpenCode Zen", "opencode-go": "OpenCode Go"}
# pi's thinking ladder in pi's own order (pi-ai's getSupportedThinkingLevels walks it this way).
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
# The add-on's "default" is an alias for whichever model Claude Code calls its default; the real
# models are listed beside it, so the alias is left out.
HIDDEN_MODELS = {("claude-code", "default")}
# Providers whose models are Claude's own: the Claude Code add-on and the shared bridge (Meridian).
CLAUDE_PROVIDERS = ("claude-code", "meridian")
# Claude Code's own model picker order: families most capable first, newest version first within one.
CLAUDE_FAMILY_ORDER = ("fable", "opus", "sonnet", "haiku")
# OpenRouter offers several hundred models; its subscription lists only these modern coding
# models, in this order (picked 2026-11-04 from pi's OpenRouter catalog and public coding scores).
OPENROUTER_MODELS = (
    "anthropic/claude-fable-5.1",
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5",
    "openai/gpt-6-astra",
    "openai/gpt-6-sol",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3.8-flash",
    "moonshotai/kimi-k3",
    "z-ai/glm-5.3",
    "deepseek/deepseek-v4-pro-0813",
    "deepseek/deepseek-v4.1-flash",
    "qwen/qwen3.8-max-0902",
    "x-ai/grok-4.7",
    "minimax/minimax-m3",
)
CLAUDE_ID = re.compile(r"^claude-(?P<family>[a-z]+)-(?P<major>\d+)(?:-(?P<minor>\d))?(?:-\d{8})?$")


def supported_thinking_levels(model: dict) -> list[str]:
    """pi's ``getSupportedThinkingLevels``: a model's effort levels from its reasoning flag and level map.

    A non-reasoning model has only "off". Otherwise the map is a sparse override: a null entry
    turns a level off, "xhigh" and "max" are on only when mapped, and every other level is on.
    """
    if not model.get("reasoning"):
        return ["off"]
    level_map = model.get("thinkingLevelMap") or {}
    return [
        level for level in THINKING_LEVELS
        if not (level in level_map and level_map[level] is None)
        and not (level in ("xhigh", "max") and level not in level_map)
    ]


def model_label(provider: str, model_id: str, name: str) -> str:
    """How a model is shown: "Opus 5.5" for Claude's models, OpenRouter's name without its
    "Vendor: " prefix, pi's own name otherwise."""
    if provider == "openrouter" and ": " in name:
        return name.partition(": ")[2]
    match = CLAUDE_ID.match(model_id) if provider in CLAUDE_PROVIDERS else None
    if match:
        version = match["major"] + (f".{match['minor']}" if match["minor"] else "")
        return f"{match['family'].capitalize()} {version}"
    return name or model_id


def claude_order(model: dict) -> tuple:
    """Sort key putting Claude's models in Claude Code's picker order (Fable, Opus, Sonnet, Haiku;
    newest first) and OpenRouter's in the order listed above; any other model keeps its place
    after them, in pi's order."""
    if model.get("provider") == "openrouter" and model.get("id") in OPENROUTER_MODELS:
        return (0, OPENROUTER_MODELS.index(model["id"]), 0)
    match = CLAUDE_ID.match(str(model.get("id"))) if model.get("provider") in CLAUDE_PROVIDERS else None
    if match is None:
        return (len(CLAUDE_FAMILY_ORDER) + 1, 0, 0)
    family = match["family"]
    rank = CLAUDE_FAMILY_ORDER.index(family) if family in CLAUDE_FAMILY_ORDER else len(CLAUDE_FAMILY_ORDER)
    return (rank, -int(match["major"]), -int(match["minor"] or 0))


def account_label(account: dict) -> str:
    label = str(account.get("label") or account.get("name") or account.get("id") or "pi account")
    return label.removesuffix(" (Pi)")


def build_choices(pi_accounts: list[dict], answers: dict[str, dict]) -> dict:
    """The subscriptions and their models, from the chat app's pi accounts and the machine's answers.

    ``pi_accounts`` are the chat app's account rows on the pi harness, in its order; ``answers``
    maps an account id to what ``pi_remote.py`` reported for it (its providers and pi's models).
    Each model's id is pi's ``provider/model`` tag, and it carries the subscription it belongs to.
    """
    subscriptions: list[dict] = []
    models: list[dict] = []
    taken: set[str] = set()
    for account in pi_accounts:
        answer = answers.get(account["id"]) or {}
        providers = [str(item) for item in answer.get("providers") or []]
        family = next((item for item in SUBSCRIPTIONS if set(item["providers"]) & set(providers)), None)
        if family is not None and family["id"] not in taken:
            held = next((PROVIDER_LABELS[item] for item in providers if item in PROVIDER_LABELS), "")
            subscription_id, label = str(family["id"]), held or str(family["label"])
        else:
            subscription_id, label = f"account:{account['id']}", account_label(account)
        taken.add(subscription_id)
        offered = [
            model for model in answer.get("models") or []
            # One provider per account folder: a provider the account does not hold (one signed in
            # through the environment, say) belongs to some other subscription.
            if (not providers or model.get("provider") in providers)
            and (model.get("provider"), model.get("id")) not in HIDDEN_MODELS
            # pi's catalog lags OpenCode: a model OpenCode retired would fail on the chat's first turn.
            and not retired_models.is_retired(str(model.get("provider")), str(model.get("id")))
            and (model.get("provider") != "openrouter" or model.get("id") in OPENROUTER_MODELS)
        ]
        subscriptions.append({
            "id": subscription_id, "label": label, "account_id": account["id"],
            "available": bool(offered),
            "detail": "" if offered else (answer.get("detail") or "No models on this account"),
        })
        # sorted() is stable, so models other than Claude's keep pi's own order.
        for model in sorted(offered, key=claude_order):
            models.append({
                "id": f"{model['provider']}/{model['id']}",
                "label": model_label(model["provider"], model["id"], str(model.get("name") or "")),
                "efforts": supported_thinking_levels(model),
                "subscription": subscription_id,
            })
    missing = [
        {"id": item["id"], "label": item["label"], "account_id": "", "available": False, "detail": NOT_CONNECTED}
        for item in SUBSCRIPTIONS if item["id"] not in taken
    ]
    order = {item["id"]: index for index, item in enumerate(SUBSCRIPTIONS)}
    subscriptions = sorted(subscriptions + missing, key=lambda item: order.get(item["id"], len(order)))
    return {"subscriptions": subscriptions, "models": models}
