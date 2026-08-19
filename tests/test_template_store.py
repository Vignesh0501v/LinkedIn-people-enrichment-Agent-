import fnmatch

from app.mapping import MappingTemplate
from app.template_store import TemplateStore


class FakeRedis:
    """Minimal in-memory stand-in for the `redis.Redis` methods TemplateStore
    actually calls (`set`, `get`, `scan_iter`). Mirrors this repo's pattern of
    injecting a fake/stub transport instead of hitting a real server
    (see app/tavily_search.py's `transport` param).
    """

    def __init__(self):
        self._data: dict[str, bytes] = {}

    def set(self, key: str, value: str) -> None:
        self._data[key] = value.encode("utf-8")

    def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    def scan_iter(self, match: str | None = None):
        for key in list(self._data.keys()):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


def _template(template_id: str, source_label: str = "Source A") -> MappingTemplate:
    return MappingTemplate(
        id=template_id,
        source_label=source_label,
        field_mappings={"company": "Company Name", "email": "Email"},
    )


def test_save_then_get_round_trip():
    store = TemplateStore(client=FakeRedis())
    template = _template("tpl-1")

    store.save(template)
    fetched = store.get("tpl-1")

    assert fetched == template


def test_get_missing_id_returns_none():
    store = TemplateStore(client=FakeRedis())

    assert store.get("does-not-exist") is None


def test_list_all_returns_multiple_saved_templates():
    store = TemplateStore(client=FakeRedis())
    template_a = _template("tpl-a", source_label="Source A")
    template_b = _template("tpl-b", source_label="Source B")

    store.save(template_a)
    store.save(template_b)

    all_templates = store.list_all()

    assert len(all_templates) == 2
    assert {t.id for t in all_templates} == {"tpl-a", "tpl-b"}
    assert template_a in all_templates
    assert template_b in all_templates
