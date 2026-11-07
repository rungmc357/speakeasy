"""Default fake tasks must never discover or call a model installed on the test host."""
import pytest
from speakeasy import router


@pytest.mark.parametrize("name,args", [
    ("polish_request", ("Write a garden newsletter",)),
    ("working_status", ("Write a garden newsletter",)),
    ("plan_steps", ("Write a garden newsletter",)),
    ("result_shape", ("Write a garden newsletter", "The draft is ready. " * 30)),
])
def test_default_metadata_helpers_never_ask_the_host_for_a_model(monkeypatch, name, args):
    asked = []

    def forbidden_client():
        asked.append(name)
        raise AssertionError("A fake task must not discover the test host's providers")

    monkeypatch.setattr(router.host_mod, "aux_client", forbidden_client)
    assert getattr(router, name)(*args) is None
    assert asked == []
