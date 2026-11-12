"""The suite never lets one test's sys.path / environment changes reach the next (SPK-447)."""
from __future__ import annotations

import os
import sys
import textwrap

import pytest

import process_state as PS

OTHER = (3, 99) if sys.version_info[:2] != (3, 99) else (3, 98)


def _foreign_site(tmp_path):
    return str(tmp_path / "env" / "lib" / f"python{OTHER[0]}.{OTHER[1]}" / "site-packages")


def test_hermes_bootstrap_did_not_switch_this_run_onto_another_environment():
    """Imported once by conftest with activation off: nothing on sys.path or PYTHONPATH belongs to
    another Python, and this interpreter's own site-packages are still there."""
    assert PS.foreign_site_entries() == []
    assert PS.foreign_site_entries(os.environ.get("PYTHONPATH", "").split(os.pathsep)) == []
    own = f"python{sys.version_info[0]}.{sys.version_info[1]}"
    assert any(own in p and p.rstrip("/").endswith("-packages") for p in sys.path)


def test_a_later_gateway_import_leaves_path_and_environment_alone(tmp_path, monkeypatch):
    pytest.importorskip("hermes_bootstrap")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # gateway.run loads the home's .env at import
    path, env = list(sys.path), dict(os.environ)
    try:
        import gateway.run  # noqa: F401  (the import that used to swap environments mid-run)
    except Exception as exc:  # this Hermes can't import its gateway here; nothing to check
        pytest.skip(f"gateway.run not importable: {exc}")
    assert PS.foreign_site_entries() == []
    assert os.environ.get("PYTHONPATH") == env.get("PYTHONPATH")
    assert [p for p in path if p not in sys.path and PS._own_site(p)] == []


def test_image_library_comes_from_this_interpreter():
    pil = pytest.importorskip("PIL")
    from PIL import Image  # noqa: F401  (needs the compiled _imaging built for this Python)
    assert not PS._foreign_site(os.path.dirname(os.path.dirname(pil.__file__)))


def _write_bootstrap(tmp_path, name, body):
    (tmp_path / "mods").mkdir(exist_ok=True)
    (tmp_path / "mods" / f"{name}.py").write_text(textwrap.dedent(body))


def test_inert_import_puts_path_hooks_and_environment_back(tmp_path, monkeypatch):
    foreign = _foreign_site(tmp_path)
    _write_bootstrap(tmp_path, "spk447_fake_bootstrap", f"""
        import os, sys
        sys.path[:] = [p for p in sys.path if not p.rstrip('/').endswith('-packages')]
        sys.path.insert(0, {foreign!r})
        sys.path_hooks.append(lambda path: (_ for _ in ()).throw(ImportError()))
        os.environ['PYTHONPATH'] = {foreign!r}
        os.environ['TMPDIR'] = {str(tmp_path)!r}
        os.environ.pop('HOME', None)
        LOADED = True
    """)
    monkeypatch.syspath_prepend(str(tmp_path / "mods"))
    path, hooks, env = list(sys.path), list(sys.path_hooks), dict(os.environ)
    assert PS.import_hermes_bootstrap_inert("spk447_fake_bootstrap") is True
    try:
        assert sys.modules["spk447_fake_bootstrap"].LOADED  # imported (so later imports are no-ops)
        assert sys.path == path and sys.path_hooks == hooks and dict(os.environ) == env
    finally:
        sys.modules.pop("spk447_fake_bootstrap", None)


def test_inert_import_switches_dependency_activation_off(tmp_path, monkeypatch):
    environments = pytest.importorskip("pm.environments")
    _write_bootstrap(tmp_path, "spk447_activating_bootstrap", """
        from pm.environments import activate_dependencies
        RESULT = activate_dependencies(None)  # the real one would select the install's environment
        FN = activate_dependencies
    """)
    monkeypatch.syspath_prepend(str(tmp_path / "mods"))
    real = environments.activate_dependencies
    try:
        PS.import_hermes_bootstrap_inert("spk447_activating_bootstrap")
        module = sys.modules["spk447_activating_bootstrap"]
        assert module.RESULT is None and module.FN is not real
        assert environments.activate_dependencies is real  # put back for anything else
    finally:
        sys.modules.pop("spk447_activating_bootstrap", None)


def test_missing_bootstrap_is_fine():
    assert PS.import_hermes_bootstrap_inert("spk447_no_such_bootstrap") is False


def test_restore_drops_foreign_entries_and_modules_loaded_from_them(tmp_path):
    foreign = _foreign_site(tmp_path)
    os.makedirs(foreign)
    with open(os.path.join(foreign, "spk447_foreign_mod.py"), "w") as f:
        f.write("X = 1\n")
    state = PS.ProcessState()
    sys.path.insert(0, foreign)
    os.environ["SPK447_LEAK"] = "1"
    import spk447_foreign_mod  # noqa: F401
    assert state.foreign_path_changes() == [f"added {foreign}"]
    state.restore()
    assert sys.path == state.path and "SPK447_LEAK" not in os.environ
    assert "spk447_foreign_mod" not in sys.modules


# Order within a file is fixed: the first test leaks the way Hermes' config loaders do (straight into
# os.environ, no monkeypatch), the second is the next test and must not see it. A leaked Hermes API
# address is what made the Codex handoff test fail only in a full run.
def test_a_leaks_environment_like_a_config_loader():
    os.environ["SPK447_API_SERVER_PORT"] = "8642"
    os.environ.pop("HERMES_DISABLE_LAZY_INSTALLS", None)


def test_b_the_next_test_starts_clean():
    assert "SPK447_API_SERVER_PORT" not in os.environ
    assert os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1"
