"""Gallery and carousel photos share the vetted authenticated image pipeline."""
import json

import pytest

from speakeasy import text, now
from speakeasy.prompt import builder


def answer(items):
    block = [{"kind": "explore", "title": "Porch concepts", "sections": [
        {"label": "Concepts", "entity": "generation", "items": items}]}]
    return "Porch A and Porch B.\n```speakeasy-views\n" + json.dumps(block) + "\n```"


def test_six_picture_answer_keeps_order_and_hides_paths(tmp_path):
    paths = []
    for n in range(6):
        path = tmp_path / f"porch-{n}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
        paths.append(path)
    result = text.split_result("Six concepts.\n" + "\n".join(f"MEDIA:{p}" for p in paths), (tmp_path,))
    assert [c["path"] for c in result["cards"]] == list(map(str, paths))
    shown = json.dumps(text.public_result(result))
    assert str(tmp_path) not in shown and "MEDIA:" not in result["full"]
    assert [i["url"] for i in now._thumbs(result, "run_gallery")] == [
        f"/voice/card-image/run_gallery/{n}" for n in range(1, 7)]
    assert "six pictures per answer" in builder.VISUAL_RESULTS_RULE
    assert "per answer at most" not in builder.VISUAL_RESULTS_RULE


def test_carousel_local_render_is_numbered_and_deduped(tmp_path):
    path = tmp_path / "porch.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    result = text.split_result(answer([{"name": "Porch A", "image": str(path)},
                                       {"name": "Porch B", "image": str(path)}]), (tmp_path,))
    items = result["views"][0]["sections"][0]["items"]
    assert [i["image"] for i in items] == [1, 1]
    assert len(result["cards"]) == 1
    assert str(tmp_path) not in json.dumps(text.public_result(result))


@pytest.mark.parametrize("ref", ["/outside/porch.png", "https://127.0.0.1/a.png",
                                  "https://localhost/a.jpg", "https://host.internal/a.png"])
def test_rejected_carousel_photos_disappear(ref, tmp_path):
    result = text.split_result(answer([{"name": "Porch A", "photo_url": ref, "image": ref}]), (tmp_path,))
    item = result["views"][0]["sections"][0]["items"][0]
    assert "image" not in item and "photo_url" not in item
    assert ref not in json.dumps(text.public_result(result))


def test_carousel_catalog_photo_uses_authenticated_card_route():
    url = "https://catalog.example/porch.jpg"
    result = text.split_result(answer([{"name": "Porch A", "photo_url": url}]), ())
    item = result["views"][0]["sections"][0]["items"][0]
    assert item["image"] == 1 and "photo_url" not in item
    assert result["cards"] == [{"kind": "image", "name": "porch.jpg", "image_url": url}]
