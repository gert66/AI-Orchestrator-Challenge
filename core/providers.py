"""Profile-driven provider routing for Brain, Worker and Reviewer.

The active profile (core.provider_registry.load_profile) maps every role to
a provider. Brain and Reviewer are routed per role: ``anthropic`` runs the
Claude CLI, ``openai`` runs Codex in a read-only sandbox. There is no
structured Gemini Brain/Reviewer adapter, so ``google`` (and ``auto``) for
those roles is a configuration failure, never a silent Claude fallback.

The Worker default comes from the profile (anthropic->claude, openai->codex,
google->gemini, auto->auto); an explicit job ``worker_provider`` still wins.
Worker ``auto`` routing is deterministic:
- auto LOW/NORMAL: spend prepaid Claude when quota trajectory has headroom; otherwise Gemini 3.8 Flash
- auto HIGH or reviewer/repair escalation: Claude Sonnet directly
- explicit gemini/claude/codex remain selectable for controlled jobs

Provider choice never owns Git safety, evidence, review or commits. Those stay in
core.machine and core.gitsafe.
"""
from core.claude_runner import ClaudeResult, DEFAULT_CLAUDE_BIN, DEFAULT_TIMEOUT_SECONDS, run_claude
from core.codex_runner import DEFAULT_CODEX_BIN, run_codex
from core.gemini_runner import DEFAULT_MODEL as DEFAULT_GEMINI_MODEL, run_gemini
from core.provider_registry import load_profile, provider_for_role

WORKER_PROVIDERS = ("auto", "claude", "gemini", "codex")
DEFAULT_WORKER_PROVIDER = "auto"
WORKER_COMPLEXITIES = ("low", "normal", "high")
DEFAULT_WORKER_COMPLEXITY = "normal"
HIGH_WORKER_MODEL = "sonnet"
# Profile provider name -> Worker provider name used by job config and routing.
PROFILE_WORKER_PROVIDERS = {"anthropic": "claude", "openai": "codex", "google": "gemini", "auto": "auto"}
# Codex sandboxes per role: Brain and Reviewer must never edit.
ROLE_SANDBOXES = {"brain": "read-only", "reviewer": "read-only", "worker": "workspace-write"}


class ProviderError(ValueError):
    """Raised for invalid provider configuration (never for a failed run)."""


def profile_worker_provider(profile=None):
    """Worker provider name (auto|claude|gemini|codex) implied by the active profile."""
    profile = load_profile() if profile is None else profile
    return PROFILE_WORKER_PROVIDERS[provider_for_role(profile, "worker")]


def _provider_failure(binary, cwd, prompt, message, provider="claude"):
    result = ClaudeResult(
        argv=[str(binary)], cwd=str(cwd), exit_code=1, stdout="", stderr=message,
        duration_seconds=0.0, timed_out=False, structured=True, schema_requested=True,
        parsed_json=None, prompt_chars=len(prompt), error=message,
    )
    result.provider = provider
    result.fallback_events = []
    result.provider_attempts = []
    result.retries = 0
    return result


def _attach_route(result, route, account_router):
    if route is not None:
        result.quota_route = {key: route.get(key) for key in ("account", "bucket", "model")}
        if getattr(result, "error", None) is None:
            account_router.record_activity(result.quota_route)
    return result


def build_runner(*, claude_bin=DEFAULT_CLAUDE_BIN, timeout=DEFAULT_TIMEOUT_SECONDS,
                  codex_bin=DEFAULT_CODEX_BIN, account_router=None, profile=None):
    """Return the provider-aware runner consumed by Machine.

    profile: a loaded provider profile dict (default: core.provider_registry.load_profile()).
    """
    if codex_bin is None or not str(codex_bin).strip():
        raise ProviderError("codex_bin: must be a non-empty string")
    profile = load_profile() if profile is None else profile
    role_providers = {role: provider_for_role(profile, role) for role in ("brain", "worker", "reviewer")}
    default_worker_provider = PROFILE_WORKER_PROVIDERS[role_providers["worker"]]

    def run_codex_role(role, prompt, *, cwd, json_schema, append_system_prompt_file=None):
        result = run_codex(
            prompt, cwd=cwd, timeout=timeout, codex_bin=codex_bin, json_schema=json_schema,
            append_system_prompt_file=append_system_prompt_file, sandbox=ROLE_SANDBOXES[role],
        )
        result.provider = "codex"
        return result

    def run_claude_role(role, prompt, *, cwd, json_schema, permission_mode, disallowed_tools,
                        model=None, effort=None, fallback_model=None, max_budget_usd=None,
                        append_system_prompt_file=None, force_model=None):
        selected_bin, selected_model, route = claude_bin, force_model or model, None
        if account_router is not None:
            route = account_router.select(role, requested_model=selected_model)
            if route is None:
                result = _provider_failure(
                    claude_bin, cwd, prompt,
                    "usage limit: quota router found no eligible Claude account",
                    provider="claude",
                )
                result.quota_route = None
                return result
            selected_bin = route["claude_bin"]
            if selected_model is None and route.get("model"):
                selected_model = route["model"]
        result = run_claude(
            prompt, cwd=cwd, timeout=timeout, claude_bin=selected_bin, json_schema=json_schema,
            permission_mode=permission_mode, disallowed_tools=disallowed_tools, model=selected_model,
            effort=effort, fallback_model=fallback_model, max_budget_usd=max_budget_usd,
            append_system_prompt_file=append_system_prompt_file,
        )
        result.provider = "claude"
        return _attach_route(result, route, account_router)

    def run(role, prompt, *, cwd, json_schema, permission_mode, disallowed_tools,
            model=None, effort=None, fallback_model=None, max_budget_usd=None,
            append_system_prompt_file=None, worker_provider=None,
            worker_complexity=DEFAULT_WORKER_COMPLEXITY, worker_escalated=False,
            worker_mode="read", allowed_paths=None, forbidden_paths=None):
        # The active profile supplies the Worker default; an explicit job
        # worker_provider (Machine always passes its persisted value) wins.
        provider = default_worker_provider if worker_provider is None else worker_provider
        complexity = str(worker_complexity or DEFAULT_WORKER_COMPLEXITY).lower()
        if complexity not in WORKER_COMPLEXITIES:
            return _provider_failure(claude_bin, cwd, prompt, f"invalid worker complexity: {complexity}")

        # Brain and Reviewer follow the profile route for their role. Only
        # Claude and Codex have structured read-only adapters; anything else
        # is a configuration failure rather than a hidden Claude dependency.
        if role != "worker":
            route = role_providers[role]
            if route == "anthropic":
                return run_claude_role(
                    role, prompt, cwd=cwd, json_schema=json_schema, permission_mode=permission_mode,
                    disallowed_tools=disallowed_tools, model=model, effort=effort,
                    fallback_model=fallback_model, max_budget_usd=max_budget_usd,
                    append_system_prompt_file=append_system_prompt_file,
                )
            if route == "openai":
                return run_codex_role(role, prompt, cwd=cwd, json_schema=json_schema,
                                      append_system_prompt_file=append_system_prompt_file)
            return _provider_failure(
                claude_bin, cwd, prompt,
                f"provider configuration error: profile {profile.get('profile')!r} routes {role} to {route!r}, "
                f"but only 'anthropic' and 'openai' have a structured {role} adapter; "
                f"edit the active profile roles.{role}",
                provider=route,
            )

        if provider == "codex":
            return run_codex_role(role, prompt, cwd=cwd, json_schema=json_schema,
                                  append_system_prompt_file=append_system_prompt_file)

        if provider == "claude":
            return run_claude_role(
                role, prompt, cwd=cwd, json_schema=json_schema, permission_mode=permission_mode,
                disallowed_tools=disallowed_tools, model=model, effort=effort,
                fallback_model=fallback_model, max_budget_usd=max_budget_usd,
                append_system_prompt_file=append_system_prompt_file,
            )

        if provider == "gemini":
            return run_gemini(
                prompt, cwd=cwd, json_schema=json_schema, timeout=min(timeout, 600),
                model=DEFAULT_GEMINI_MODEL, append_system_prompt_file=append_system_prompt_file,
                mode=worker_mode, allowed_paths=allowed_paths, forbidden_paths=forbidden_paths,
            )

        if provider != "auto":
            return _provider_failure(claude_bin, cwd, prompt, f"unknown worker provider: {provider}")

        # HIGH work and any repair after Reviewer escalation goes straight to Sonnet.
        if complexity == "high" or worker_escalated:
            result = run_claude_role(
                role, prompt, cwd=cwd, json_schema=json_schema, permission_mode=permission_mode,
                disallowed_tools=disallowed_tools, model=model, effort=effort,
                fallback_model=fallback_model, max_budget_usd=max_budget_usd,
                append_system_prompt_file=append_system_prompt_file, force_model=HIGH_WORKER_MODEL,
            )
            result.provider_route = "auto_high_claude_sonnet" if complexity == "high" else "auto_reviewer_escalation_claude_sonnet"
            return result

        # LOW/NORMAL are budget-aware. Prefer already-paid Claude capacity when
        # its weekly/daily/session trajectory has headroom; use Gemini when that
        # capacity needs to be conserved for later in the reset window.
        prefer_prepaid = bool(
            account_router is not None
            and hasattr(account_router, "prefer_prepaid")
            and account_router.prefer_prepaid("worker", requested_model=model)
        )
        if prefer_prepaid:
            claude = run_claude_role(
                role, prompt, cwd=cwd, json_schema=json_schema, permission_mode=permission_mode,
                disallowed_tools=disallowed_tools, model=model, effort=effort,
                fallback_model=fallback_model, max_budget_usd=max_budget_usd,
                append_system_prompt_file=append_system_prompt_file, force_model=HIGH_WORKER_MODEL,
            )
            claude.provider_route = f"auto_{complexity}_prepaid_claude_sonnet"
            return claude

        gemini = run_gemini(
            prompt, cwd=cwd, json_schema=json_schema, timeout=min(timeout, 600),
            model=DEFAULT_GEMINI_MODEL, append_system_prompt_file=append_system_prompt_file,
            mode=worker_mode, allowed_paths=allowed_paths, forbidden_paths=forbidden_paths,
        )
        gemini.provider_route = (
            f"auto_{complexity}_gemini_budget_guard" if account_router is not None
            else f"auto_{complexity}_gemini"
        )
        if gemini.exit_code == 0 and not getattr(gemini, "error", None):
            return gemini

        # Gemini Flex/Standard failed. Route the same bounded Worker call to Sonnet.
        claude = run_claude_role(
            role, prompt, cwd=cwd, json_schema=json_schema, permission_mode=permission_mode,
            disallowed_tools=disallowed_tools, model=model, effort=effort,
            fallback_model=fallback_model, max_budget_usd=max_budget_usd,
            append_system_prompt_file=append_system_prompt_file, force_model=HIGH_WORKER_MODEL,
        )
        events = list(getattr(gemini, "fallback_events", []) or [])
        events.append({"from": "gemini", "to": "claude-sonnet", "reason": getattr(gemini, "error", None) or "Gemini failed"})
        claude.fallback_events = events
        claude.provider_attempts = list(getattr(gemini, "provider_attempts", []) or []) + [
            {"provider": "claude", "model": HIGH_WORKER_MODEL, "service_tier": None}
        ]
        claude.retries = int(getattr(gemini, "retries", 0) or 0) + 1
        claude.provider_route = f"auto_{complexity}_gemini_to_claude_sonnet"
        claude.gemini_usage_before_fallback = dict(getattr(gemini, "gemini_usage", {}) or {})
        return claude

    run.quota_router = account_router
    run.profile = profile
    run.role_providers = role_providers
    run.default_worker_provider = default_worker_provider
    return run
