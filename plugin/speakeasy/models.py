"""Which model a Speakeasy job runs on, and how hard it thinks (docs/API.md "Models").

Hermes owns the catalog and the switch. ``GET /api/model/options`` is the same provider/model
inventory its own pickers show; ``POST /v1/runs`` takes a per-request ``provider`` + ``model`` and
``model_options.reasoning`` ``{enabled, effort}``. Speakeasy only picks from that catalog, never
edits Hermes' config, and never changes how requests are routed.

What a run uses, first that applies:

1. the voice phrase on the request itself ("use the smart model for this"): that task only;
2. the chat's choice (``Use for this chat``: set from a session page, the New chat chip, or a message
   that carries ``model``/``reasoning``). A linked task (a chat Speakeasy can't write into) and a
   follow-up use their chat's choice too;
3. Speakeasy's default (settings ``jobs.model`` / ``jobs.reasoning``);
4. Hermes' own configuration (nothing is sent).

Model and reasoning resolve separately, so "High" for one chat keeps the default model.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# The levels a person can pick, weakest first ("ultra" is Hermes-internal, codex app-server only).
LEVELS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
LABELS = {"none": "Off", "minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High",
          "xhigh": "Extra high", "max": "Max"}
SHORT_LABELS = {"none": "Off", "minimal": "Min", "low": "Low", "medium": "Med", "high": "High",
                "xhigh": "XHigh", "max": "Max"}
DEFAULT_LEVELS: tuple[str, ...] = ("none", "low", "medium", "high")
CACHE_S = 600            # the catalog is re-read at most this often (or on ?refresh=1)
FAILED_RETRY_S = 30      # after a failed read, how soon to try again
PER_PROVIDER = 6         # models shown per provider (featured first); chosen/default ones always kept
MAX_ID = 300
FAST_HINT = re.compile(r"(haiku|mini|nano|flash|lite|luna|fast|turbo|small)", re.I)
# "use the smart model for this", "use the fast one", "switch to the smarter model", "on the quick model".
VOICE_RE = re.compile(
    r"\b(?:use|using|with|on|switch(?:ing)? to|try)\s+(?:the\s+|your\s+)?"
    r"(smart(?:er|est)?|clever(?:er)?|best|strong(?:er|est)?|big(?:ger)?|fast(?:er|est)?|quick(?:er|est)?|cheap(?:er)?)"
    r"\s+(?:model|one|brain|mode)\b(?:\s+(?:for\s+(?:this|that|it)(?:\s+one)?|on\s+(?:this|that)|here))?[,.;:!]?",
    re.I)
SMART_WORDS = ("smart", "clever", "best", "strong", "big")


class ChoiceError(ValueError):
    pass


# -- names ----------------------------------------------------------------------------------------

_CAPS = {"gpt": "GPT", "glm": "GLM", "mlx": "MLX", "ai": "AI", "llm": "LLM", "qwq": "QwQ", "it": "IT",
         "jang": "JANG", "v3": "V3", "v4": "V4", "oss": "OSS", "r1": "R1", "k2": "K2", "k3": "K3",
         "deepseek": "DeepSeek", "minimax": "MiniMax", "openai": "OpenAI", "xai": "xAI"}
_DATE = re.compile(r"^(?:20\d{6}|\d{4})$")


def friendly_name(model: str) -> str:
    """A model id as people say it: ``claude-opus-5-5`` -> Claude Opus 5.5, ``gpt-6.1-sol`` -> GPT-6.1 Sol,
    ``anthropic/claude-haiku-4.5`` -> Claude Haiku 4.5, a local weights path -> its folder name."""
    raw = (model or "").strip()
    one_m = raw.endswith("[1m]")
    base = raw[:-4] if one_m else raw
    base = base.rstrip("/").rsplit("/", 1)[-1] or raw
    tokens = [t for t in re.split(r"[-_\s]+", base) if t]
    out: list[str] = []
    for i, tok in enumerate(tokens):
        low = tok.lower()
        if _DATE.fullmatch(low) and i >= 2:
            continue  # a dated snapshot reads as the model
        if out and re.fullmatch(r"\d{1,2}", low) and re.fullmatch(r"[A-Za-z]?\d+", out[-1]):
            out[-1] = f"{out[-1]}.{low}"  # claude-opus-5-5 -> 5.5
            continue
        if low in _CAPS:
            out.append(_CAPS[low])
        elif re.fullmatch(r"\d+(?:\.\d+)*[a-z]?", low) or re.fullmatch(r"\d+(?:\.\d+)?[bkm]", low):
            out.append(low.upper() if low[-1:] in "bkm" and low[:-1].replace(".", "").isdigit() else low)
        else:
            out.append(tok[:1].upper() + tok[1:])
    name = " ".join(out)
    name = re.sub(r"^GPT (\d)", r"GPT-\1", name)  # GPT-6.1 Sol, as OpenAI writes it
    if one_m:
        name += " 1M"
    return name or raw


def level_label(level: str | None) -> str:
    return LABELS.get(level or "", "Default")


# -- reasoning levels per model -------------------------------------------------------------------

def reasoning_levels(provider: str, model: str, caps: dict[str, Any] | None) -> list[str]:
    """The levels a picker offers for one model: [] when it doesn't reason at all. From Hermes' own
    vocabulary tables when it has one for the route, else a plain Off/Low/Medium/High."""
    caps = caps if isinstance(caps, dict) else {}
    if caps.get("reasoning") is False:
        return []
    bare = (model or "").lower().rsplit("/", 1)[-1]
    levels: tuple[str, ...] | None = None
    try:
        from agent import reasoning_effort as R  # type: ignore
        if provider in {"openai-codex", "openai-api", "openai"} or bare.startswith("gpt-"):
            levels = R.codex_supported_efforts(model)
        elif provider.startswith("xai") or bare.startswith("grok"):
            m = re.search(r"grok-(\d+)\.(\d+)", bare)
            levels = R.XAI_GROK46_EFFORTS if m and (int(m[1]), int(m[2])) >= (4, 6) else R.XAI_LEGACY_EFFORTS
        elif "kimi" in bare or re.search(r"(?:^|[^a-z0-9])k3(?:[^a-z0-9]|$)", bare):
            levels = R.kimi_supported_efforts(model)
        elif "glm-5.2" in bare or "glm-5-2" in bare:
            levels = R.GLM52_EFFORTS
        elif "glm-5.3" in bare or "glm-5-3" in bare:
            levels = R.GLM53_EFFORTS
        elif "deepseek-v4" in bare:
            levels = R.DEEPSEEK_V4_EFFORTS
        elif "gemini-4" in bare:
            levels = R.GEMINI4_THINKING_LEVELS
    except Exception:
        levels = None
    if levels is None:
        if "claude" in bare and re.search(r"(opus|fable)-?[5-9]", bare):
            levels = ("none", "low", "medium", "high", "max")
        else:
            levels = DEFAULT_LEVELS
    out = [lvl for lvl in LEVELS if lvl in levels]
    if caps.get("can_disable_reasoning") is False:
        out = [lvl for lvl in out if lvl != "none"]
    return out


def clamp(level: str | None, supported: list[str]) -> str | None:
    """A level the model takes: itself, else the nearest weaker one (never more cost), else the
    weakest it has. None stays None; a model that doesn't reason takes none."""
    if level is None:
        return None
    if not supported:
        return None
    if level in supported:
        return level
    if level == "none":
        return None  # can't be switched off: leave it to the model's default
    idx = LEVELS.index(level) if level in LEVELS else 3
    weaker = [lvl for lvl in supported if lvl != "none" and LEVELS.index(lvl) < idx]
    if weaker:
        return weaker[-1]
    on = [lvl for lvl in supported if lvl != "none"]
    return on[0] if on else None


# -- ids ------------------------------------------------------------------------------------------

def make_id(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def split_id(model_id: str) -> tuple[str, str]:
    provider, _, model = model_id.partition("/")
    return provider, model


# -- the catalog ----------------------------------------------------------------------------------

class Catalog:
    """Hermes' model inventory, read through its API server and kept for CACHE_S. ``fetch`` returns
    the ``/api/model/options`` payload; ``hermes_reasoning`` the effort Hermes' config uses."""

    def __init__(self, fetch: Callable[[], dict[str, Any]], hermes_reasoning: Callable[[], str | None]):
        self._fetch, self._hermes_reasoning = fetch, hermes_reasoning
        self._lock = threading.Lock()
        self._raw: dict[str, Any] | None = None
        self._at = 0.0
        self._failed_at = 0.0
        self._reasoning: str | None = None

    def raw(self, refresh: bool = False) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            fresh = self._raw is not None and now - self._at < CACHE_S
            if fresh and not refresh:
                return self._raw
            if not refresh and self._raw is None and self._failed_at and now - self._failed_at < FAILED_RETRY_S:
                return None
        try:
            payload = self._fetch()
            if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
                raise ValueError("no providers in the model options")
            try:
                effort = self._hermes_reasoning()
            except Exception:
                effort = None
        except Exception as exc:
            logger.info("speakeasy: Hermes' model list is unavailable (%s)", type(exc).__name__)
            with self._lock:
                self._failed_at = time.monotonic()
                return self._raw  # the last good one, if any
        with self._lock:
            self._raw, self._at, self._failed_at = payload, time.monotonic(), 0.0
            self._reasoning = effort if effort in LEVELS else None
            return self._raw

    def peek(self) -> dict[str, Any] | None:
        """The last catalog read, never fetching (for hot paths like a task's work object)."""
        with self._lock:
            return self._raw

    @property
    def hermes_reasoning(self) -> str | None:
        return self._reasoning

    def entries(self, raw: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """Every selectable model by id (all of a provider's models, not just the shown ones)."""
        raw = raw if raw is not None else self.raw()
        out: dict[str, dict[str, Any]] = {}
        for row in (raw or {}).get("providers") or []:
            if not isinstance(row, dict) or row.get("authenticated") is False:
                continue
            slug = str(row.get("slug") or "")
            if not slug or "/" in slug:
                continue
            caps = row.get("capabilities") if isinstance(row.get("capabilities"), dict) else {}
            unavailable = set(row.get("unavailable_models") or [])
            for model in row.get("models") or []:
                if not isinstance(model, str) or not model or model in unavailable:
                    continue
                mid = make_id(slug, model)
                if len(mid) > MAX_ID:
                    continue
                out[mid] = {"id": mid, "model": model, "provider": slug,
                            "provider_name": str(row.get("name") or slug), "name": friendly_name(model),
                            "reasoning_levels": reasoning_levels(slug, model, caps.get(model))}
        return out

    def hermes_default(self, raw: dict[str, Any] | None = None) -> str | None:
        raw = raw if raw is not None else self.raw()
        if not raw:
            return None
        provider, model = raw.get("provider"), raw.get("model")
        if isinstance(provider, str) and isinstance(model, str) and provider and model:
            return make_id(provider, model)
        return None


def _pick_fast(entries: dict[str, dict[str, Any]], default_id: str | None) -> str | None:
    """A sensible "fast" model: a quick sibling from the default's provider (Haiku, Mini, Flash...)."""
    if not default_id:
        return None
    provider, model = split_id(default_id)
    family = re.split(r"[-/.]", model.lower().rsplit("/", 1)[-1])[0]
    siblings = [e for e in entries.values() if e["provider"] == provider and FAST_HINT.search(e["model"])]
    same = [e for e in siblings if e["model"].lower().rsplit("/", 1)[-1].startswith(family)]
    pick = (same or siblings or [None])[0]
    return pick["id"] if pick else default_id


def presets(settings: dict[str, Any], entries: dict[str, dict[str, Any]] | None, hermes_default: str | None
            ) -> dict[str, dict[str, Any]]:
    """{"default", "fast", "smart"}: each {model, reasoning}; ``model`` may be None (Hermes decides).
    *entries* None = the catalog hasn't been read: saved ids are taken as they are."""
    jobs = settings.get("jobs") or {}

    def known(mid: Any) -> bool:
        return isinstance(mid, str) and bool(mid) and (entries is None or mid in entries)

    default_model = jobs.get("model") if known(jobs.get("model")) else None
    base = default_model or hermes_default
    fast = jobs.get("fast_model") if known(jobs.get("fast_model")) else _pick_fast(entries or {}, base)
    smart = jobs.get("smart_model") if known(jobs.get("smart_model")) else base

    def level(mid: str | None, want: str | None) -> str | None:
        return clamp(want, entries[mid]["reasoning_levels"]) if entries and mid in entries else want

    return {"default": {"model": default_model, "reasoning": jobs.get("reasoning") or None},
            "fast": {"model": fast, "reasoning": level(fast, jobs.get("fast_reasoning") or "low")},
            "smart": {"model": smart, "reasoning": level(smart, jobs.get("smart_reasoning") or "high")}}


def public(catalog: Catalog, settings: dict[str, Any], keep: set[str] | None = None,
           refresh: bool = False, served_default: str | None = None) -> dict[str, Any]:
    """``GET /voice/models``: the providers Hermes is signed in to with a short list each (featured or
    newest first, plus any model already in use), the defaults, and the fast/smart presets.

    ``served_default``: the model Hermes actually served Speakeasy's latest run that asked for no model
    (its reported runtime). When Speakeasy has no default of its own and that differs from the configured
    one (a fallback provider, a profile override), ``default`` names what really runs, with
    ``configured`` the model config.yaml names, so the chip never claims a model the work didn't use."""
    raw = catalog.raw(refresh=refresh)
    if raw is None:
        return {"available": False, "providers": [], "levels": [{"id": lvl, "label": LABELS[lvl],
                                                                "short": SHORT_LABELS[lvl]} for lvl in LEVELS]}
    entries = catalog.entries(raw)
    hermes_default = catalog.hermes_default(raw)
    pre = presets(settings, entries, hermes_default)
    wanted = set(keep or ()) | {p["model"] for p in pre.values() if p["model"]}
    if hermes_default:
        wanted.add(hermes_default)
    own_default = bool(pre["default"]["model"] or pre["default"]["reasoning"])
    really = served_default if (served_default and not own_default and served_default != hermes_default
                                and served_default in entries) else None
    if really:
        wanted.add(really)
    providers = []
    rows = [r for r in raw.get("providers") or [] if isinstance(r, dict)]
    rows.sort(key=lambda r: (not r.get("is_current"), not any(
        isinstance(m, str) and make_id(str(r.get("slug")), m) in wanted for m in r.get("models") or [])))
    for row in rows:
        slug = str(row.get("slug") or "")
        mine = [e for e in entries.values() if e["provider"] == slug]
        if not mine:
            continue
        order = {e["model"]: i for i, e in enumerate(mine)}
        featured = [m for m in row.get("featured_models") or [] if m in order]
        shown_models = (featured or [e["model"] for e in mine])[:PER_PROVIDER]
        shown_models += [e["model"] for e in mine if e["id"] in wanted and e["model"] not in shown_models]
        shown = []
        for model in shown_models:
            e = dict(entries[make_id(slug, model)])
            e["default"] = e["id"] == (pre["default"]["model"] or really or hermes_default)
            if e["id"] == hermes_default:
                e["hermes_default"] = True
            shown.append(e)
        providers.append({"id": slug, "name": str(row.get("name") or slug), "current": bool(row.get("is_current")),
                          "local": bool(row.get("is_user_defined")), "models": shown,
                          "more": max(0, len(mine) - len(shown))})
    default_id = pre["default"]["model"] or really or hermes_default
    default: dict[str, Any] = {"model": default_id, "reasoning": pre["default"]["reasoning"] or catalog.hermes_reasoning,
                               "source": "speakeasy" if own_default else "hermes"}
    if really:
        default.update(served=True, configured=hermes_default)
    return {
        "available": True,
        "providers": providers,
        "default": default,
        "hermes_default": {"model": hermes_default, "reasoning": catalog.hermes_reasoning},
        "fast": pre["fast"], "smart": pre["smart"],
        "levels": [{"id": lvl, "label": LABELS[lvl], "short": SHORT_LABELS[lvl]} for lvl in LEVELS],
    }


# -- choices --------------------------------------------------------------------------------------

def parse_choice(body: dict[str, Any], entries: Callable[[], dict[str, dict[str, Any]]]) -> dict[str, Any] | None:
    """``model`` / ``reasoning`` on a request body -> {model, reasoning} (None = absent). ``"default"`` or
    "" clears that half (the chat goes back to the default). Raises ChoiceError."""
    if "model" not in body and "reasoning" not in body:
        return None
    model, reasoning = body.get("model"), body.get("reasoning")
    choice: dict[str, Any] = {}
    if "model" in body:
        if model in (None, "", "default"):
            choice["model"] = None
        elif not isinstance(model, str) or len(model) > MAX_ID or "/" not in model:
            raise ChoiceError("model must be a model id from GET /voice/models")
        else:
            known = entries()
            if not known:
                raise ChoiceError("The model list on your Hermes isn't available right now. Try again in a moment.")
            if model not in known:
                raise ChoiceError("That model isn't one Hermes offers. Pick one from the list.")
            choice["model"] = model
    if "reasoning" in body:
        if reasoning in (None, "", "default"):
            choice["reasoning"] = None
        elif reasoning not in LEVELS:
            raise ChoiceError("reasoning must be one of: " + ", ".join(LEVELS) + " (or default)")
        else:
            choice["reasoning"] = reasoning
    if choice.get("model") and choice.get("reasoning"):
        levels = entries()[choice["model"]]["reasoning_levels"]
        if choice["reasoning"] not in levels:
            name = entries()[choice["model"]]["name"]
            raise ChoiceError(f"{name} doesn't take {LABELS[choice['reasoning']]} reasoning"
                              + (f" (it takes {', '.join(LABELS[lvl] for lvl in levels)})." if levels else "."))
    return choice


def voice_preset(text: str | None) -> tuple[str | None, str]:
    """``("smart"|"fast"|None, text without the phrase)`` for a spoken or typed request that asks for
    a model by kind: "use the smart model for this", "on the fast one". Never routes anything."""
    if not text:
        return None, text or ""
    found = list(VOICE_RE.finditer(text))
    if not found:
        return None, text
    m = found[-1]  # the newest words win (a request can carry what was said before it)
    word = m.group(1).lower()
    kind = "smart" if word.startswith(SMART_WORDS) else "fast"
    cleaned = (text[:m.start()] + text[m.end():]).strip(" ,.;:-")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if cleaned[:1].islower():
        cleaned = cleaned[:1].upper() + cleaned[1:]
    return kind, cleaned or text


def resolve(layers: list[tuple[str, dict[str, Any] | None]], entries: dict[str, dict[str, Any]] | None
            ) -> dict[str, Any] | None:
    """The first layer naming a model gives the model; the first naming a reasoning level gives that
    (clamped to what the model takes). ``layers``: (source, {model?, reasoning?, preset?}) strongest
    first. *entries*: the catalog, or None when it hasn't been read (ids are then taken as they are:
    Hermes clamps an effort a model doesn't take). Returns {model, reasoning, source, model_source,
    reasoning_source, preset?} or None when nothing is chosen."""
    model = reasoning = model_source = reasoning_source = preset = None
    for source, layer in layers:
        if not layer:
            continue
        wanted = layer.get("model")
        if model is None and isinstance(wanted, str) and "/" in wanted and (entries is None or wanted in entries):
            model, model_source = wanted, source
            preset = preset or layer.get("preset")
        if reasoning is None and layer.get("reasoning") in LEVELS:
            reasoning, reasoning_source = layer["reasoning"], source
            preset = preset or layer.get("preset")
    if model is None and reasoning is None:
        return None
    if model and reasoning and entries is not None:
        clamped = clamp(reasoning, entries[model]["reasoning_levels"])
        if clamped is None:
            reasoning = reasoning_source = None
        else:
            reasoning = clamped
    if model is None and reasoning is None:
        return None
    out = {"model": model, "reasoning": reasoning, "model_source": model_source,
           "reasoning_source": reasoning_source, "source": model_source or reasoning_source}
    if preset:
        out["preset"] = preset
    return out


def run_fields(choice: dict[str, Any] | None) -> dict[str, Any]:
    """What goes on ``POST /v1/runs`` for a resolved choice: ``provider`` + ``model`` and
    ``model_options.reasoning`` (Hermes' per-request overrides). {} = Hermes' own config."""
    if not choice:
        return {}
    out: dict[str, Any] = {}
    if choice.get("model"):
        provider, model = split_id(choice["model"])
        if provider and model:
            out["provider"], out["model"] = provider, model
    level = choice.get("reasoning")
    if level == "none":
        out["model_options"] = {"reasoning": {"enabled": False}}
    elif level in LEVELS:
        out["model_options"] = {"reasoning": {"enabled": True, "effort": level}}
    return out


def describe(record: dict[str, Any] | None, entries: dict[str, dict[str, Any]], hermes_default: str | None = None,
             hermes_reasoning: str | None = None) -> dict[str, Any] | None:
    """A run's model for clients: {id, name, provider, provider_name, reasoning, reasoning_label, source,
    served}. ``served`` = Hermes reported it ran on this model (its runtime), not just what was asked."""
    if not record:
        return None
    served = record.get("served") if isinstance(record.get("served"), dict) else None
    mid = None
    if served and served.get("provider") and served.get("model"):
        mid = make_id(served["provider"], served["model"])
    elif served and served.get("model"):
        # Only the model is known (a thread's session row): the catalog entry with that model, else bare.
        mid = next((eid for eid, e in entries.items() if e.get("model") == served["model"]), None) \
            or "/" + str(served["model"])
    mid = mid or record.get("model") or hermes_default
    if not mid:
        return None
    provider, model = split_id(mid)
    entry = entries.get(mid) or {}
    reasoning = record.get("reasoning") if record.get("reasoning") in LEVELS else None
    if reasoning is None and hermes_reasoning in LEVELS:
        # Nothing asked: Hermes' configured effort (clamped the way it would be for this model).
        levels = entry.get("reasoning_levels")
        reasoning = clamp(hermes_reasoning, levels) if levels is not None else hermes_reasoning
    out = {"id": mid, "name": entry.get("name") or friendly_name(model), "provider": provider,
           "provider_name": entry.get("provider_name") or provider,
           "reasoning": reasoning, "reasoning_label": LABELS.get(reasoning or "", None),
           "source": record.get("source") or "hermes", "served": bool(served)}
    if record.get("preset"):
        out["preset"] = record["preset"]
    return out
