"""Agy's model list and how to reach a model in its terminal picker.

``agy models`` prints one row per model and effort ("claude-opus-5-5-high", "Claude Opus 5.5
(High)"). Agy's ``/model`` picker lists each model once, with an effort slider for the models
that have several rows. ``model_choices`` groups the rows the way the picker does (a model and
its efforts), ``find_row`` turns a choice back into a row, and ``swap_plan`` turns a row into what
to type and where to put the slider.
"""
from typing import Any

import re

EFFORT_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max")
TIERED_LABEL = re.compile(r"^(?P<base>.+) \((?P<tier>[A-Za-z]+)\)$")


def parse_models(text: str) -> list[dict[str, str]]:
    """The rows of ``agy models`` output: tab-separated id and label, after a progress line."""
    rows = []
    for line in text.splitlines():
        model_id, separator, label = line.partition("\t")
        if separator and model_id.strip() and label.strip():
            rows.append({"id": model_id.strip(), "label": label.strip()})
    return rows


def swap_plan(label: str, rows: list[dict[str, str]]) -> dict[str, Any]:
    """What to search for, and the effort slider's length and position, to reach ``label``."""
    base, tier = _tier({"label": label})
    levels = sorted({level for name, level in map(_tier, rows) if name == base and level}, key=_level_order)
    effort_index = levels.index(tier) if tier in levels else 0
    return {"search": base, "levels": len(levels) if tier in levels else 1, "effort_index": effort_index}


def _tier(row: dict[str, str]) -> tuple[str, str]:
    """A row's model name and effort level ("Claude Opus 5.5", "high"); no level for a row without one."""
    match = TIERED_LABEL.match(row["label"])
    return (match["base"], match["tier"].lower()) if match else (row["label"], "")


def model_slug(row: dict[str, str]) -> str:
    """The row's id without its effort ("claude-opus-5-5-high" -> "claude-opus-5-5")."""
    tier = _tier(row)[1]
    return row["id"][: -len(tier) - 1] if tier and row["id"].endswith(f"-{tier}") else row["id"]


def _level_order(level: str) -> int:
    return EFFORT_ORDER.index(level) if level in EFFORT_ORDER else len(EFFORT_ORDER)


def model_choices(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The models of ``agy models`` as the picker shows them: each once, with its efforts, lowest first."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        base, tier = _tier(row)
        group = groups.setdefault(model_slug(row), {"id": model_slug(row), "label": base, "efforts": []})
        if tier and tier not in group["efforts"]:
            group["efforts"].append(tier)
    for group in groups.values():
        group["efforts"].sort(key=_level_order)
    return list(groups.values())


def find_row(rows: list[dict[str, str]], model: str, effort: str) -> dict[str, str] | None:
    """The row for a chosen model and effort. A model alone (or a full row id, as an earlier send
    stored it) means that row, else the medium one, else the first."""
    exact = next((row for row in rows if row["id"] == model), None)
    siblings = [row for row in rows if model_slug(row) == model]
    if effort:
        return next((row for row in siblings if _tier(row)[1] == effort.lower()), None)
    return exact or next((row for row in siblings if _tier(row)[1] == "medium"), siblings[0] if siblings else None)
