"""Public API for the C# knowledge graph generator."""

from .builder import KnowledgeGraphBuilder
from .core import (
    ClassInfo,
    classify_type,
    generic_args,
    generic_root,
    parse_cs_file,
    parse_cshtml_file,
    parse_csproj,
    parse_route_registrations,
    parse_solution,
    split_top_level,
    to_kebab,
    type_contract_key,
    vc_tag_to_pascal,
)

__all__ = [
    "ClassInfo",
    "KnowledgeGraphBuilder",
    "classify_type",
    "generic_args",
    "generic_root",
    "parse_cs_file",
    "parse_cshtml_file",
    "parse_csproj",
    "parse_route_registrations",
    "parse_solution",
    "split_top_level",
    "to_kebab",
    "type_contract_key",
    "vc_tag_to_pascal",
]
