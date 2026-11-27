"""Every shipped provider profile loads and is role-routable through core.provider_registry."""
import json

import pytest

from core import provider_registry

PROFILES = ("claude", "openai", "hybrid")


def _load(root, name):
    with open(root / "profiles" / f"{name}.json", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("name", PROFILES)
def test_profile_loads_and_routes_validate(root, name):
    profile = _load(root, name)
    assert profile["profile"] == name
    routes = provider_registry.validate_routes(profile["roles"])
    assert set(routes) == set(provider_registry.ROLES)


@pytest.mark.parametrize("name", PROFILES)
def test_each_role_maps_to_registry_provider(root, name):
    routes = _load(root, name)["roles"]
    for role in provider_registry.ROLES:
        assert routes[role] in provider_registry.PROVIDERS or routes[role] == "auto", (name, role, routes[role])


def test_single_provider_profiles_have_no_hidden_cross_provider_dependency(root):
    assert set(_load(root, "claude")["roles"].values()) == {"anthropic"}
    assert set(_load(root, "openai")["roles"].values()) == {"openai"}


def test_hybrid_profile_is_explicitly_mixed(root):
    roles = _load(root, "hybrid")["roles"]
    assert len(set(roles.values())) > 1


def test_validate_routes_rejects_missing_and_unknown():
    with pytest.raises(ValueError):
        provider_registry.validate_routes({"brain": "anthropic"})
    with pytest.raises(ValueError):
        provider_registry.validate_routes({"brain": "anthropic", "worker": "anthropic", "reviewer": "acme"})
    with pytest.raises(ValueError):
        provider_registry.validate_routes(
            {"brain": "anthropic", "worker": "anthropic", "reviewer": "anthropic", "janitor": "openai"}
        )
