import json

from speakeasy.settings import Settings


def _write(home, minutes):
    p = home / "speakeasy" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"idle_pause_minutes": minutes}))
    return p


def test_old_default_five_moves_to_fifteen_once(tmp_path):
    s = Settings(tmp_path)
    _write(tmp_path, 5)
    assert s.get()["idle_pause_minutes"] == 15
    assert json.loads(s.path.read_text())["idle_pause_minutes"] == 15
    s.patch({"idle_pause_minutes": 5})  # chosen afterwards: kept
    assert s.get()["idle_pause_minutes"] == 5


def test_other_values_are_left_alone(tmp_path):
    s = Settings(tmp_path)
    _write(tmp_path, 0)
    assert s.get()["idle_pause_minutes"] == 0
    s.patch({"idle_pause_minutes": 5})
    assert s.get()["idle_pause_minutes"] == 5


def test_new_install_defaults_to_fifteen(tmp_path):
    assert Settings(tmp_path).ensure()["idle_pause_minutes"] == 15
