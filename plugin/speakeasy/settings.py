"""Server-side settings and the Hermes-side values Speakeasy reads (never writes secrets here).

``<HERMES_HOME>/speakeasy/settings.json`` (0600) holds only non-secret preferences. The optional
OpenAI API key (``SPEAKEASY_OPENAI_API_KEY``, or ``OPENAI_API_KEY``) and the Hermes API server
key/port (``API_SERVER_KEY`` / ``API_SERVER_PORT``) are read through Hermes' own secret and env
readers (:func:`hermes_secret`); none is ever stored in settings or returned by a route.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Iterator

DEFAULT_PORT = 8795
DEFAULT_HERMES_API_PORT = 8642
OPENAI_KEY_NAME = "SPEAKEASY_OPENAI_API_KEY"
OPENAI_KEY_FALLBACK = "OPENAI_API_KEY"  # also accepted; earlier setup docs named it
XAI_KEY_NAME = "XAI_API_KEY"  # fallback for Grok voice when Hermes holds no xAI sign-in
GROK_BETA_OFF = ("Grok voice is a beta and it's off. Turn it on first: "
                 "`hermes voice config set beta.grok_voice true`")

DEFAULTS: dict[str, Any] = {
    "assistant_name": "Hermes",
    "user_name": "",
    "machine_description": "this Mac",
    "voice": {"provider": "codex", "voice": "", "codex_path": "", "max_call_minutes": 30},
    "idle_pause_minutes": 5,
    "instructions_extra": "",
    "hermes_profile": "",
    # target: the default destination; new_thread: open a thread there per task (where supported);
    # channels: extra opted-in destinations a new task is routed to by topic or by name.
    # mode: where work goes. "single" = only the home target; "home" = new work goes home, and
    # follow-ups can continue existing threads in the approved channels; "topic" = like "home",
    # plus new work is sorted into the approved channel whose description fits.
    "delivery": {"target": "none", "new_thread": False, "channels": [], "mode": "home"},
    "continuity": {"enabled": True},
    # A brief spoken update on long tasks. (Where a task went, e.g. a new thread, is always said.)
    "speech": {"progress": True},
    "brief": {"auto_refresh": True, "include_recent_voice": True},
    # Instant home control through Home Assistant (offered when Hermes has it set up). entities:
    # the devices it may use (None = not chosen yet: lights, thermostats and fans).
    "home_control": {"enabled": False, "entities": None},
    # Fast lanes. quick_answers: simple public-fact questions answered from one web search, without the
    # full agent. jev: "" (off) or a provider of TypeSafe's Jev decision model (venice | openrouter |
    # typesafe) that sorts each request in well under a second; the key comes from the profile .env.
    # home_place: a city for weather questions that don't name one ("what's the weather tomorrow").
    "fast_routing": {"quick_answers": True, "jev": "", "home_place": ""},
    # Off unless turned on. Lower the Mac's output volume to `level` % while the assistant speaks on a
    # call made from the Mac running Hermes; the old volume comes back after `hold_s` seconds without
    # its speech or when the call ends (macOS only).
    "audio_duck": {"enabled": False, "level": 30, "hold_s": 4.0},
    "image_roots": [],
    # Written by `hermes voice setup`: the URL other devices use to reach this server (a tailnet
    # HTTPS name when Tailscale was imported), reused by `hermes voice pair`.
    "server": {"advertised_url": "", "tailscale_name": ""},
    # Set by POST /voice/onboarding (or `hermes voice config set`) when the user confirmed them.
    "onboarding": {"names_set": False, "delivery_set": False, "home_offered": False},
    # Beta features, all off unless turned on. grok_voice: voice.provider may be "grok" (xAI realtime
    # voice through the plugin's audio relay).
    "beta": {"grok_voice": False},
}
DEFAULT_VOICES = {"codex": "cove", "openai": "marin", "grok": "Ara"}
# Voices each provider accepts; they do not overlap (the ChatGPT-sign-in voice model rejects API
# voice names). Keep in sync with mac/Sources/SpeakeasyCore/VoiceCatalog.swift.
PROVIDER_VOICES = {
    # The first nine predate GPT-Live 1.1; the rest arrived with it (Codex 0.163) and each was
    # verified on a live session. Porto and Selva are listed by Codex but the server refuses them.
    "codex": ("arbor", "ember", "cove", "spruce", "breeze", "sol", "vale", "juniper", "maple",
              "bubbie", "aube", "rivage", "haetsal", "hanul", "himari", "miko", "rindo", "sonna",
              "leher", "neer", "rio", "tinta", "viola"),
    "openai": ("alloy", "ash", "ballad", "cedar", "coral", "echo", "marin", "sage", "shimmer", "verse"),
    # xAI's built-in voices (names are case-insensitive here; sent capitalized).
    "grok": ("Ara", "Rex", "Sal", "Eve", "Leo"),
}
PROVIDERS = ("codex", "openai", "grok")

# Hermes `hermes send` target: platform, platform:chat_id, platform:chat_id:thread_id, platform:#name.
DELIVERY_TARGET_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}(?::(?:#[A-Za-z0-9_.-]{1,80}|[A-Za-z0-9_@+.=-]{1,128})(?::[A-Za-z0-9_.-]{1,64})?)?$")
MAX_CHANNELS = 8
CHANNEL_LABEL_RE = re.compile(r"^#?[^\x00-\x1f{}<>`#]{1,40}$")
NAME_RE = re.compile(r"^[^\x00-\x1f{}<>`]{0,40}$")
VOICE_RE = re.compile(r"^[A-Za-z0-9_-]{0,32}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]{0,64}$")


class SettingsError(ValueError):
    pass


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def valid_delivery_target(value: Any) -> bool:
    return isinstance(value, str) and (value == "none" or bool(DELIVERY_TARGET_RE.fullmatch(value)))


def validate(settings: dict[str, Any]) -> dict[str, Any]:
    """Return a clean copy or raise SettingsError naming the first bad field."""
    s = _merge(DEFAULTS, {k: v for k, v in settings.items() if k in DEFAULTS})
    if isinstance(s.get("speech"), dict):
        s["speech"].pop("acknowledge", None)  # retired: the voice model acknowledges on its own
    for key in ("assistant_name", "user_name"):
        if not isinstance(s[key], str) or not NAME_RE.fullmatch(s[key].strip()):
            raise SettingsError(f"{key} must be a short name (up to 40 characters)")
        s[key] = s[key].strip()
    if not s["assistant_name"]:
        s["assistant_name"] = DEFAULTS["assistant_name"]
    if not isinstance(s["machine_description"], str) or not 0 < len(s["machine_description"].strip()) <= 80:
        raise SettingsError("machine_description must be 1-80 characters")
    s["machine_description"] = s["machine_description"].strip()
    voice = s["voice"]
    if voice.get("provider") not in PROVIDERS:
        raise SettingsError("voice.provider must be codex, openai or grok")
    if not isinstance(voice.get("voice"), str) or not VOICE_RE.fullmatch(voice["voice"]):
        raise SettingsError("voice.voice must be a voice name")
    if not isinstance(voice.get("codex_path"), str) or len(voice["codex_path"]) > 512:
        raise SettingsError("voice.codex_path must be a path")
    if not isinstance(voice.get("max_call_minutes"), int) or not 1 <= voice["max_call_minutes"] <= 240:
        raise SettingsError("voice.max_call_minutes must be 1-240")
    if not isinstance(s["idle_pause_minutes"], int) or not 0 <= s["idle_pause_minutes"] <= 120:
        raise SettingsError("idle_pause_minutes must be 0-120")
    if not isinstance(s["instructions_extra"], str) or len(s["instructions_extra"]) > 1000:
        raise SettingsError("instructions_extra must be at most 1000 characters")
    if not isinstance(s["hermes_profile"], str) or not PROFILE_RE.fullmatch(s["hermes_profile"]):
        raise SettingsError("hermes_profile must be a profile name")
    s["delivery"] = validate_delivery(s["delivery"])
    s["home_control"] = validate_home_control(s["home_control"])
    s["server"] = validate_server(s["server"])
    s["audio_duck"] = validate_audio_duck(s["audio_duck"])
    fast = s["fast_routing"]
    if not isinstance(fast.get("quick_answers"), bool):
        raise SettingsError("fast_routing.quick_answers must be true or false")
    if fast.get("jev") not in {"", "venice", "openrouter", "typesafe"}:
        raise SettingsError("fast_routing.jev must be one of: venice, openrouter, typesafe (or empty for off)")
    if not isinstance(fast.get("home_place", ""), str) or len(fast.get("home_place", "")) > 80:
        raise SettingsError("fast_routing.home_place must be a city name")
    for group, keys in (("beta", ("grok_voice",)), ("continuity", ("enabled",)), ("speech", ("progress",)), ("brief", ("auto_refresh", "include_recent_voice")),
                        ("onboarding", ("names_set", "delivery_set", "home_offered"))):
        for key in keys:
            if not isinstance(s[group].get(key), bool):
                raise SettingsError(f"{group}.{key} must be true or false")
    roots = s["image_roots"]
    if not isinstance(roots, list) or len(roots) > 8 or not all(
            isinstance(r, str) and Path(r).expanduser().is_absolute() for r in roots):
        raise SettingsError("image_roots must be a list of absolute paths")
    return s


ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def validate_audio_duck(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("enabled"), bool):
        raise SettingsError("audio_duck.enabled must be true or false")
    level, hold = raw.get("level"), raw.get("hold_s")
    if isinstance(level, bool) or not isinstance(level, (int, float)) or not 0 <= level <= 100 or level != int(level):
        raise SettingsError("audio_duck.level must be a whole number from 0 to 100")
    if isinstance(hold, bool) or not isinstance(hold, (int, float)) or not 0.5 <= hold <= 60:
        raise SettingsError("audio_duck.hold_s must be 0.5 to 60 seconds")
    return {"enabled": raw["enabled"], "level": int(level), "hold_s": float(hold)}


def validate_home_control(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("enabled", False), bool):
        raise SettingsError("home_control.enabled must be true or false")
    entities = raw.get("entities")
    if entities is not None:
        if not isinstance(entities, list) or len(entities) > 150 or not all(
                isinstance(e, str) and ENTITY_ID_RE.fullmatch(e) for e in entities):
            raise SettingsError("home_control.entities must be a list of Home Assistant entity ids (up to 150)")
        entities = sorted(set(entities))
    return {"enabled": raw.get("enabled", False), "entities": entities}


def validate_channel(raw: Any) -> dict[str, Any]:
    """One opted-in delivery channel: {target, label, topic, new_thread}."""
    if not isinstance(raw, dict):
        raise SettingsError("delivery.channels entries must be objects")
    target, label, topic = raw.get("target"), raw.get("label"), raw.get("topic", "")
    if not valid_delivery_target(target) or target == "none":
        raise SettingsError("delivery.channels[].target must be a Hermes send target like discord:<chat_id>")
    if not isinstance(label, str) or not CHANNEL_LABEL_RE.fullmatch(label.strip()):
        raise SettingsError("delivery.channels[].label must be a short name like #work (up to 40 characters)")
    if not isinstance(topic, str) or len(topic.strip()) > 160 or any(c in topic for c in "{}<>`"):
        raise SettingsError("delivery.channels[].topic must be a plain description (up to 160 characters)")
    if not isinstance(raw.get("new_thread", False), bool):
        raise SettingsError("delivery.channels[].new_thread must be true or false")
    return {"target": target, "label": label.strip(), "topic": " ".join(topic.split()),
            "new_thread": raw.get("new_thread", False)}


ADVERTISED_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]{1,253}(?::\d{1,5})?/?$")


def validate_server(server: dict[str, Any]) -> dict[str, Any]:
    if set(server) - {"advertised_url", "tailscale_name"}:
        raise SettingsError(f"unknown setting: server.{sorted(set(server) - {'advertised_url', 'tailscale_name'})[0]}")
    url, name = server.get("advertised_url", ""), server.get("tailscale_name", "")
    if not isinstance(url, str) or (url and not ADVERTISED_URL_RE.fullmatch(url)):
        raise SettingsError("server.advertised_url must be an http(s) URL like https://host:8795")
    if not isinstance(name, str) or len(name) > 253 or any(c in name for c in " /{}<>`"):
        raise SettingsError("server.tailscale_name must be a host name")
    return {"advertised_url": url.rstrip("/"), "tailscale_name": name}


def validate_channels(raw: list[Any]) -> list[dict[str, Any]]:
    channels = [validate_channel(c) for c in raw]
    for field in ("target", "label"):
        values = [c[field].lower().lstrip("#") for c in channels]
        if len(values) != len(set(values)):
            raise SettingsError(f"delivery.channels: two channels have the same {field}")
    return channels


DELIVERY_MODES = ("single", "home", "topic")


def validate_delivery(delivery: dict[str, Any]) -> dict[str, Any]:
    """The delivery block. Settings from before channels existed (``new_thread_per_task``) migrate:
    that flag becomes ``new_thread`` on the default target."""
    delivery = dict(delivery)
    legacy = delivery.pop("new_thread_per_task", None)
    if legacy is not None and not isinstance(legacy, bool):
        raise SettingsError("delivery.new_thread_per_task must be true or false")
    if legacy is True and delivery.get("new_thread") is False:
        delivery["new_thread"] = True
    unknown = set(delivery) - {"target", "new_thread", "channels", "mode"}
    if unknown:
        raise SettingsError(f"unknown setting: delivery.{sorted(unknown)[0]}")
    if not valid_delivery_target(delivery.get("target")):
        raise SettingsError("delivery.target must be none or a Hermes send target like telegram or discord:<chat_id>")
    if not isinstance(delivery.get("new_thread"), bool):
        raise SettingsError("delivery.new_thread must be true or false")
    raw_channels = delivery.get("channels")
    if not isinstance(raw_channels, list) or len(raw_channels) > MAX_CHANNELS:
        raise SettingsError(f"delivery.channels must be a list of at most {MAX_CHANNELS} channels")
    channels = validate_channels(raw_channels)
    mode = delivery.get("mode", "home")
    if mode not in DELIVERY_MODES:
        raise SettingsError("delivery.mode must be single, home or topic")
    if delivery["target"] == "none":
        delivery["new_thread"] = False
    return {"target": delivery["target"], "new_thread": delivery["new_thread"], "channels": channels, "mode": mode}


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


class Settings:
    """Thread-safe settings file. `get()` always returns a validated full copy."""

    def __init__(self, hermes_home: Path):
        self.hermes_home = Path(hermes_home)
        self.path = self.hermes_home / "speakeasy" / "settings.json"
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.path.exists()

    def get(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        try:
            return validate(raw if isinstance(raw, dict) else {})
        except SettingsError:
            return copy.deepcopy(DEFAULTS)

    def save(self, settings: dict[str, Any]) -> dict[str, Any]:
        clean = validate(settings)
        if clean["voice"]["provider"] == "grok" and not grok_beta_on(clean):
            # Refused when this save would choose Grok, or turn the beta off under it. A hand-edited
            # file that already has both still loads and saves (calls refuse until the beta is on).
            old = self.get()
            if old["voice"]["provider"] != "grok":
                raise SettingsError(GROK_BETA_OFF)
            if grok_beta_on(old):
                raise SettingsError("voice.provider is grok: switch it to codex or openai before "
                                    "turning the Grok voice beta off")
        with self._lock:
            _write_private(self.path, json.dumps(clean, indent=2) + "\n")
        return clean

    def patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise SettingsError("JSON object required")
        unknown = set(patch) - set(DEFAULTS)
        if unknown:
            raise SettingsError(f"unknown setting: {sorted(unknown)[0]}")
        delivery = patch.get("delivery")
        if isinstance(delivery, dict) and "new_thread_per_task" in delivery and "new_thread" not in delivery:
            # An older app sends the pre-channels flag: it means new_thread on the default target.
            delivery = dict(delivery)
            delivery["new_thread"] = delivery.pop("new_thread_per_task")
            patch = {**patch, "delivery": delivery}
        return self.save(_merge(self.get(), patch))

    def ensure(self) -> dict[str, Any]:
        return self.get() if self.exists() else self.save({})


# -- dotted get/set for `hermes voice config` -------------------------------------------

def get_path(settings: dict[str, Any], dotted: str) -> Any:
    node: Any = settings
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise SettingsError(f"unknown setting: {dotted}")
        node = node[part]
    return node


def patch_for(dotted: str, raw: str) -> dict[str, Any]:
    """`voice.provider=openai` → {"voice": {"provider": "openai"}}; JSON values accepted."""
    try:
        value: Any = json.loads(raw)
    except ValueError:
        value = raw
    if isinstance(get_path(DEFAULTS, dotted), str) and not isinstance(value, str):
        value = raw
    out: dict[str, Any] = {}
    node = out
    parts = dotted.split(".")
    for part in parts[:-1]:
        node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    return out


# -- Hermes-side values ------------------------------------------------------------------

@contextlib.contextmanager
def profile_scope(hermes_home: Path) -> Iterator[None]:
    """Run Hermes' own config/secret readers for this profile.

    HTTP handler threads and the CLI do not carry the gateway's per-turn profile scope, so this binds
    the same two things Hermes binds for a turn: the profile home override (``hermes_constants``) and
    the profile's secret mapping as Hermes builds it (``agent.secret_scope.build_profile_secret_scope``:
    the profile's ``.env``, its external secret sources and any administrator-managed values).
    Speakeasy itself never opens ``.env`` or ``auth.json``.
    """
    try:
        from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope  # type: ignore
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override  # type: ignore
    except ImportError:  # outside Hermes (never in production): nothing to bind
        yield
        return
    home = Path(hermes_home)
    home_token = set_hermes_home_override(home)
    try:
        scope_token = set_secret_scope(build_profile_secret_scope(home), profile_home=str(home))
    except BaseException:
        reset_hermes_home_override(home_token)
        raise
    try:
        yield
    finally:
        reset_secret_scope(scope_token)
        reset_hermes_home_override(home_token)


def hermes_secret(hermes_home: Path, name: str) -> str:
    """A secret or env setting for this profile, through Hermes' sanctioned reader.

    ``hermes_cli.config.get_env_value_prefer_dotenv`` inside :func:`profile_scope`: the profile's own
    value first (what Hermes and earlier Speakeasy versions read), then the profile secret scope and
    the process environment through ``agent.secret_scope.get_secret``. Never logged or returned by a
    route.
    """
    try:
        from hermes_cli.config import get_env_value_prefer_dotenv  # type: ignore
    except ImportError:
        return os.environ.get(name, "").strip()
    try:
        with profile_scope(hermes_home):
            return str(get_env_value_prefer_dotenv(name) or "").strip()
    except Exception:  # an unreadable secret source must not take the voice server down
        return ""


def save_hermes_secret(hermes_home: Path, name: str, value: str) -> None:
    """Store NAME=value in this profile through Hermes' env writer (``hermes_cli.config.save_env_value``:
    atomic, private file mode, refuses administrator-managed keys). Raises ValueError when refused."""
    from hermes_cli.config import require_env_writable, save_env_value  # type: ignore
    with profile_scope(hermes_home):
        require_env_writable(name, "set")
        save_env_value(name, value)


def profile_env_defines(hermes_home: Path, name: str) -> bool:
    """True when this profile itself sets NAME (its ``.env``, as Hermes' ``load_env`` parses it),
    whatever the process environment holds. Setup uses it to add a value only when it is missing."""
    try:
        from hermes_cli.config import load_env  # type: ignore
    except ImportError:
        return False
    with profile_scope(hermes_home):
        return name in load_env()


def openai_api_key(hermes_home: Path) -> str:
    """The OpenAI API key for the API-key voice path: ``SPEAKEASY_OPENAI_API_KEY`` (documented), else
    ``OPENAI_API_KEY`` (accepted because earlier setup docs named it)."""
    return hermes_secret(hermes_home, OPENAI_KEY_NAME) or hermes_secret(hermes_home, OPENAI_KEY_FALLBACK)


def hermes_config(hermes_home: Path) -> dict[str, Any]:
    """This profile's config.yaml as written, through Hermes' reader (``hermes_cli.config.read_raw_config``).
    ``{}`` when missing or unreadable. Read-only: changes go through ``hermes_config.update``."""
    try:
        from hermes_cli.config import read_raw_config  # type: ignore
    except ImportError:
        read_raw_config = None
    try:
        if read_raw_config is not None:
            with profile_scope(hermes_home):
                data = read_raw_config()
        else:
            import yaml  # type: ignore
            data = yaml.safe_load((Path(hermes_home) / "config.yaml").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_hermes_config = hermes_config


def _api_server_block(cfg: dict[str, Any]) -> dict[str, Any]:
    """The api_server block wherever Hermes allows it (gateway.platforms, top-level platforms)."""
    merged: dict[str, Any] = {}
    gateway = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    for platforms in (gateway.get("platforms"), cfg.get("platforms")):
        block = platforms.get("api_server") if isinstance(platforms, dict) else None
        if isinstance(block, dict):
            extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
            merged = {**merged, **{k: v for k, v in block.items() if k != "extra"}, **extra}
    return merged


def is_this_machine(host: str) -> bool:
    """True when ``host`` is an address of this machine (loopback, or one of its own interfaces such
    as a Tailscale IP). Checked by binding to it, so traffic to it never leaves the machine."""
    import ipaddress
    import socket
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind((host, 0))
        return True
    except OSError:
        return False


def hermes_api_base(hermes_home: Path) -> str:
    """URL of this machine's Hermes API server.

    Port and host come from config.yaml (either platforms block) or the profile's env. A wildcard or
    missing host means loopback. A specific host is used only when it is one of this machine's own
    addresses (e.g. an API server bound to the Tailscale IP), so the key never crosses the network.
    """
    block = _api_server_block(_hermes_config(hermes_home))
    port: Any = block.get("port") or hermes_secret(hermes_home, "API_SERVER_PORT") or DEFAULT_HERMES_API_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_HERMES_API_PORT
    host = str(block.get("host") or hermes_secret(hermes_home, "API_SERVER_HOST") or "").strip().strip("[]")
    if host in {"", "0.0.0.0", "::", "*", "localhost"} or not is_this_machine(host):
        host = "127.0.0.1"
    shown = f"[{host}]" if ":" in host else host
    return f"http://{shown}:{port}"


# Oldest Codex whose realtime voice API matches what Speakeasy sends (older ones reject it).
MIN_CODEX_VERSION = (0, 150, 0)


def codex_version(binary: Path, runner: Any = None) -> tuple[int, int, int] | None:
    import subprocess
    run = runner or subprocess.run
    try:
        out = run([str(binary), "--version"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(x) for x in match.groups()) if match else None  # type: ignore[return-value]


def codex_candidates(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    seen: list[Path] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if directory:
            seen.append(Path(directory) / "codex")
    # The ChatGPT app ships Codex inside it (newer builds under codex-cli/bin, older at Resources/).
    # The older standalone Codex app (now folded into ChatGPT) ships the same CLI, and a migration
    # can leave it installed on its own; find_codex still picks the newest copy.
    for apps in (Path("/Applications"), home / "Applications"):
        seen += [apps / "ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
                 apps / "ChatGPT.app/Contents/Resources/codex",
                 apps / "Codex.app/Contents/Resources/codex"]
    seen += [
             home / ".hermes/speakeasy/codex/node_modules/.bin/codex",
             Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")]
    out: list[Path] = []
    for path in seen:
        if path.is_file() and os.access(path, os.X_OK) and path.resolve() not in {p.resolve() for p in out}:
            out.append(path)
    return out


def find_codex(configured: str = "", version_fn: Any = None, candidates: list[Path] | None = None) -> Path | None:
    """The configured Codex, else the newest one found (PATH, the ChatGPT app, a Speakeasy copy)."""
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() and os.access(path, os.X_OK) else None
    version_fn = version_fn or codex_version
    best: tuple[tuple[int, int, int], Path] | None = None
    for path in candidates if candidates is not None else codex_candidates():
        version = version_fn(path) or (0, 0, 0)
        if best is None or version > best[0]:
            best = (version, path)
    return best[1] if best else None


def codex_too_old(binary: Path | None, version_fn: Any = None) -> bool:
    if binary is None:
        return False
    version = (version_fn or codex_version)(binary)
    return version is not None and version < MIN_CODEX_VERSION


def grok_beta_on(settings: dict[str, Any]) -> bool:
    return bool((settings.get("beta") or {}).get("grok_voice"))


def default_voice(settings: dict[str, Any]) -> str:
    """The saved voice when the provider accepts it; otherwise that provider's default, so switching
    providers never leaves a call unable to start."""
    voice = settings["voice"]
    chosen, provider = voice.get("voice"), voice["provider"]
    if provider == "grok":  # xAI names are capitalized; accept any case
        return next((v for v in PROVIDER_VOICES["grok"] if v.lower() == str(chosen or "").lower()),
                    DEFAULT_VOICES["grok"])
    return chosen if chosen in PROVIDER_VOICES[provider] else DEFAULT_VOICES[provider]
