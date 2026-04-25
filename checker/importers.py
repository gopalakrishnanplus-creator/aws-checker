from __future__ import annotations

from typing import Iterable

from .models import CheckDefinition, ManagedResource
from .seed_data import CHECK_DEFINITIONS, RESOURCE_SEEDS


def _definition_defaults(payload):
    return {
        "sort_order": payload["sort_order"],
        "category": payload["category"],
        "check_item": payload["check_item"],
        "what_to_verify": payload["what_to_verify"],
        "how_to_check": payload["how_to_check"],
        "success_criteria": payload["success_criteria"],
        "priority": payload["priority"],
        "frequency": payload["frequency"],
        "is_active": True,
    }


def sync_check_definitions(definitions: dict | None = None, replace: bool = False):
    definitions = definitions or CHECK_DEFINITIONS
    synced = 0
    for service_type, service_definitions in definitions.items():
        incoming_codes = {payload["code"] for payload in service_definitions}
        if replace:
            CheckDefinition.objects.filter(service_type=service_type).exclude(code__in=incoming_codes).delete()
        for payload in service_definitions:
            CheckDefinition.objects.update_or_create(
                service_type=service_type,
                code=payload["code"],
                defaults=_definition_defaults(payload),
            )
            synced += 1
    return synced


def sync_resources(resources: Iterable[dict] | None = None, replace: bool = False):
    resources = list(resources or RESOURCE_SEEDS)
    if replace:
        incoming_by_service = {}
        for payload in resources:
            incoming_by_service.setdefault(payload["service_type"], set()).add(payload["resource_identifier"])
        for service_type, identifiers in incoming_by_service.items():
            ManagedResource.objects.filter(service_type=service_type).exclude(
                resource_identifier__in=identifiers
            ).delete()
    synced = 0
    for payload in resources:
        lookup = {
            "service_type": payload["service_type"],
            "resource_identifier": payload["resource_identifier"],
        }
        defaults = {key: value for key, value in payload.items() if key not in lookup}
        ManagedResource.objects.update_or_create(**lookup, defaults=defaults)
        synced += 1
    return synced


def sync_seed_data():
    return {
        "check_definitions": sync_check_definitions(replace=True),
        "resources": sync_resources(replace=True),
    }
