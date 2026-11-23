"""Shared data classifications, independent of account administration and ACLs."""

DATA_DOMAIN_LABELS = {
    "public": "公共",
    "operations": "运营",
    "finance": "财务",
    "product": "产品",
    "sales": "销售",
    "hr": "人事",
}


def normalize_data_domains(values: tuple[str, ...]) -> tuple[str, ...]:
    from .domain import ValidationError
    if (not isinstance(values, tuple) or len(values) > 1000
            or any(not isinstance(value, str) or value not in DATA_DOMAIN_LABELS for value in values)):
        raise ValidationError("data_domains must be a tuple of known data classification IDs")
    return tuple(sorted(set(values)))


def domains_allowed(resource_domains, access_domains) -> bool:
    """Unclassified data stays hidden from scoped accounts, including old data."""
    if access_domains is None:
        return True
    if (not isinstance(resource_domains, (tuple, list)) or len(resource_domains) > 1000
            or any(not isinstance(value, str) or value not in DATA_DOMAIN_LABELS
                   for value in resource_domains)):
        return False
    return bool(resource_domains) and ("public" in resource_domains
                                      or bool(set(resource_domains).intersection(access_domains)))
