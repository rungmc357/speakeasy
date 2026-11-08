"""SPK-231 R3: a live status line names the site ("Reading Yelp"), never a bare domain. Invented fixtures."""
from speakeasy import live_status as L


def _item(n, text, kind, at):
    return {"id": f"a{n}", "text": text, "kind": kind, "at": at, "status": "done"}


def _state(*items):
    return {"v": 1, "next": len(items) + 1, "next_f": 1, "items": list(items), "findings": [], "touched": [],
            "edits": {}, "sources": [], "trials": None, "diff_preview": None}


def test_site_names_from_hosts():
    assert L.site_name("www.yelp.com") == "Yelp"
    assert L.site_name("yelp.com") == "Yelp"
    assert L.site_name("github.com") == "GitHub"
    assert L.site_name("news.ycombinator.com") == "Hacker News"
    assert L.site_name("docs.example.com") == "Example"
    assert L.site_name("shop.acme-tools.co.uk") == "Acme Tools"
    assert L.site_name("www.rei.com") == "REI"


def test_a_line_says_the_site_not_its_domain_or_path():
    assert L.sited("Reading yelp.com/biz/corner-noodle-bar") == "Reading Yelp"
    assert L.sited("Opening www.yelp.com") == "Opening Yelp"
    assert L.sited("Reading https://www.opentable.com/r/x and github.com/acme/app") == "Reading OpenTable and GitHub"
    assert L.sited("Editing store.py and router.py") == "Editing store.py and router.py"      # files stay files
    assert L.sited("Emailed pat@example.com") == "Emailed pat@example.com"                    # never half an address


def test_the_deterministic_live_line_names_the_site():
    now = 5000.0
    got = L.deterministic(_state(_item(1, "Read www.yelp.com/biz/corner-noodle-bar", "browse", now - 5)), now=now)
    assert got["now"] == "Reading Yelp"


def test_the_side_models_line_names_the_site_too():
    got = L.parse_model('{"now": "Comparing jackets on rei.com and amazon.com", "so_far": "Read two reviews"}')
    assert got["now"] == "Comparing jackets on REI and Amazon"
