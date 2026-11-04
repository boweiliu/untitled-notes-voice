"""Destination preferences scoped by machine, provider, subscription and model."""
from copy import deepcopy
from typing import Any

PROVIDERS = {"claude", "codex", "agy", "pi-dev"}


def validate(value: object) -> None:
    if not isinstance(value, dict):
        raise ValueError("Invalid destination defaults")
    for name in ("machine", "machine_name"):
        if not isinstance(value.get(name, ""), str) or len(value.get(name, "")) > 120:
            raise ValueError("Invalid machine default")
    if "updated_at" in value and (isinstance(value["updated_at"], bool) or not isinstance(value["updated_at"], (int, float))):
        raise ValueError("Invalid defaults timestamp")
    def model_settings(settings: Any) -> None:
        if not isinstance(settings, dict) or not isinstance(settings.get("model", ""), str):
            raise ValueError("Invalid model default")
        efforts = settings.get("effort_by_model", {})
        if not isinstance(efforts, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in efforts.items()):
            raise ValueError("Invalid effort defaults")
    def machine_settings(settings: Any) -> None:
        if not isinstance(settings, dict) or settings.get("provider") not in PROVIDERS:
            raise ValueError("Choose a provider")
        providers = settings.get("by_provider")
        if not isinstance(providers, dict) or set(providers) - PROVIDERS:
            raise ValueError("Invalid provider defaults")
        for provider in providers.values():
            model_settings(provider)
            if not isinstance(provider.get("subscription", ""), str):
                raise ValueError("Invalid subscription default")
            old = provider.get("model_by_subscription", {})
            if not isinstance(old, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in old.items()):
                raise ValueError("Invalid subscription defaults")
            subscriptions = provider.get("by_subscription", {})
            if not isinstance(subscriptions, dict) or any(not isinstance(k, str) for k in subscriptions):
                raise ValueError("Invalid subscription defaults")
            for subscription in subscriptions.values():
                model_settings(subscription)
    if "by_machine" in value:
        if not isinstance(value["by_machine"], dict):
            raise ValueError("Invalid machine defaults")
        for machine, settings in value["by_machine"].items():
            if not isinstance(machine, str) or not machine or len(machine) > 120:
                raise ValueError("Invalid machine default")
            machine_settings(settings)
    else:
        machine_settings(value)


def normalize(value: dict[str, Any], current_machine: str = "") -> dict[str, Any]:
    """Bind legacy global choices once, to their saved machine, without rewriting the store."""
    result = deepcopy(value)
    if "by_machine" not in result:
        machine = result.get("machine") or current_machine
        result["by_machine"] = {}
        if machine:
            result["machine"] = machine
            result["by_machine"][machine] = {"provider": result.get("provider", "claude"),
                "by_provider": deepcopy(result.get("by_provider", {}))}
    for machine in result["by_machine"].values():
        for provider in machine.get("by_provider", {}).values():
            if "by_subscription" not in provider:
                subscriptions = {}
                remembered = provider.get("model_by_subscription", {})
                if provider.get("subscription") and provider.get("model"):
                    remembered = {provider["subscription"]: provider["model"], **remembered}
                for subscription, model in remembered.items():
                    subscriptions[subscription] = {"model": model, "effort_by_model": deepcopy(provider.get("effort_by_model", {}))}
                if subscriptions:
                    provider["by_subscription"] = subscriptions
    result["version"] = 2
    # Compatibility projection for a tab that has not refreshed yet. by_machine
    # is authoritative; legacy writes are bound to their selected machine only.
    selected = result["by_machine"].get(result.get("machine"))
    if selected is not None:
        result["provider"] = selected.get("provider", "claude")
        result["by_provider"] = deepcopy(selected.get("by_provider", {}))
    return result


def merge(existing: dict[str, Any], incoming: dict[str, Any], current_machine: str = "") -> dict[str, Any]:
    """A stale tab may update its own machine, but cannot erase another machine's choices."""
    saved = normalize(existing, current_machine)
    incoming = normalize(incoming, current_machine)
    scopes = {**incoming["by_machine"], **saved["by_machine"]}
    selected = incoming.get("machine")
    if selected in incoming["by_machine"]:
        scopes[selected] = incoming["by_machine"][selected]
    return normalize({**incoming, "by_machine": scopes}, current_machine)
