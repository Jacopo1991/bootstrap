"""Shared builder routing and boost capacity policy (no runtime side effects)."""
import json
from datetime import datetime, timezone

TIERS = ("routine", "hard", "design")
ROUTING = {
    "codex": {"default": "gpt-6-luna", "hard": "gpt-6.1-sol", "design": "gpt-6-astra"},
    "claude": {"default": "sonnet", "hard": "fable", "design": "opus"},
}
UNBOOSTED = {"codex": "gpt-6.1-sol", "claude": "opus"}
# Top models run only for their specialty tier, whoever names them.
TOP_ONLY = {"gpt-6-astra": "design", "fable": "hard", "claude-fable-5-1": "hard"}


class RoutingParseError(ValueError):
    """Invalid user supplied routing policy or tier."""


def policy(home):
    path = home / ".config/task/routing.json"
    try:
        overrides = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError as error:
        raise RoutingParseError(f"{path}: invalid JSON") from error
    if not isinstance(overrides, dict) or not isinstance(overrides.get("models", {}), dict):
        raise RoutingParseError(f"{path}: expected an object with a models object")
    result = {vendor: dict(values) for vendor, values in ROUTING.items()}
    for vendor, values in overrides.get("models", {}).items():
        if vendor not in result or not isinstance(values, dict):
            raise RoutingParseError(f"{path}: invalid vendor policy")
        for key, value in values.items():
            if key not in result[vendor] or not isinstance(value, str) or not value:
                raise RoutingParseError(f"{path}: invalid model policy")
            result[vendor][key] = value
    extra = overrides.get("extra_lanes", 2)
    if type(extra) is not int or extra < 0:
        raise RoutingParseError(f"{path}: extra_lanes must be a nonnegative integer")
    return result, extra


def active(home):
    path = home / ".local/state/task/boost.json"
    try:
        values = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    result = {}
    if not isinstance(values, dict):
        return result
    for vendor, until in values.items():
        try:
            when = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if vendor in ROUTING and when.tzinfo and datetime.now(timezone.utc) < when:
                result[vendor] = until
        except (ValueError, AttributeError):
            pass
    return result


def guard(tier, model):
    """Refuse a top model outside its tier; no flag overrides this."""
    if TOP_ONLY.get(model, tier) != tier:
        raise RoutingParseError(
            f"{model} is for Tier: {TOP_ONLY[model]} tasks only (this one is {tier}); use gpt-6-luna "
            "by default or gpt-6.1-sol for hard (Claude: sonnet, opus). --model and --no-pace do not override this.")


def route(home, vendor, tier, explicit=None, boosted=True):
    model = pick(home, vendor, tier, explicit, boosted)
    guard(tier, model)
    return model


def pick(home, vendor, tier, explicit, boosted):
    if tier not in TIERS:
        raise RoutingParseError(f"Invalid tier: {tier}")
    models, _ = policy(home)
    values = models[vendor]
    if explicit:
        return explicit
    if tier == "routine":
        return values["default"]
    if vendor == "codex" and tier == "hard":
        return "gpt-6.1-sol"
    if not boosted:
        return values["default"]
    return values[tier] if vendor in active(home) else UNBOOSTED[vendor]


def lane_limits(home, slots):
    _, extra = policy(home)
    boosts = active(home)
    return {vendor: min(slots, 1 + (extra if vendor in boosts else 0)) for vendor in ROUTING}
