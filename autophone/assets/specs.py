"""Stable AssetSpec declarations without type-specific store wrappers."""

from autophone.assets.store import AssetSpec


API_EXPECTATIONS_SPEC = AssetSpec(
    list_key="expectations",
    schema_version=1,
    editable_scalars=frozenset({"title", "note"}),
    editable_structs=frozenset({"expect", "forbid"}),
    id_prefix="P",
)
