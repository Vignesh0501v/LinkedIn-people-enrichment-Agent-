"""Redis-backed persistence for `MappingTemplate` (see app/mapping.py and
doc/TRD_LinkedIn_Enrichment_Pipeline.md, "Data model" -> "MappingTemplate").

Each template is stored as a JSON blob under the key
`mapping_template:{template.id}` (see `KEY_PREFIX` below). The JSON payload
is simply `{"id": ..., "source_label": ..., "field_mappings": {...}}`, i.e.
a direct serialization of the dataclass's fields -- no custom encoding.
"""

import json

import redis

from app.config import load_config
from app.mapping import MappingTemplate

KEY_PREFIX = "mapping_template:"


def _key(template_id: str) -> str:
    return f"{KEY_PREFIX}{template_id}"


def _to_json(template: MappingTemplate) -> str:
    return json.dumps(
        {
            "id": template.id,
            "source_label": template.source_label,
            "field_mappings": template.field_mappings,
        }
    )


def _from_json(raw: str | bytes) -> MappingTemplate:
    data = json.loads(raw)
    return MappingTemplate(
        id=data["id"],
        source_label=data["source_label"],
        field_mappings=data["field_mappings"],
    )


class TemplateStore:
    def __init__(self, client: "redis.Redis | None" = None):
        self._client = client if client is not None else redis.Redis.from_url(load_config().redis_url)

    def save(self, template: MappingTemplate) -> None:
        self._client.set(_key(template.id), _to_json(template))

    def get(self, template_id: str) -> MappingTemplate | None:
        raw = self._client.get(_key(template_id))
        if raw is None:
            return None
        return _from_json(raw)

    def list_all(self) -> list[MappingTemplate]:
        templates: list[MappingTemplate] = []
        for key in self._client.scan_iter(match=f"{KEY_PREFIX}*"):
            raw = self._client.get(key)
            if raw is None:
                continue
            templates.append(_from_json(raw))
        return templates
