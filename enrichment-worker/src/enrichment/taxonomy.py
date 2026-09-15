"""Category taxonomy and the attribute schema each category expects.

Keeping the taxonomy as data rather than as prompt text matters for two
reasons. It is what lets the extracted output be *validated* rather than
trusted -- an attribute outside the declared enum is a detectable error, not a
plausible-looking string that quietly enters the catalog. And it means adding a
category is a data change, reviewable by someone from the category team who
does not read Python.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AttributeSpec:
    name: str
    type: str                      # "string" | "integer" | "enum"
    values: tuple[str, ...] = ()   # for enum
    required: bool = False


# The union of every attribute any category uses. A single extraction call is
# made against this union and the result is then filtered to the attributes the
# chosen category actually declares. The alternative -- classify first, then
# extract with a category-specific schema -- is more precise but doubles
# latency and cost per listing, which is not worth it at this accuracy level.
ATTRIBUTES: dict[str, AttributeSpec] = {
    a.name: a for a in [
        AttributeSpec("fabric", "enum", ("cotton", "silk", "georgette", "chiffon", "rayon",
                                         "polyester", "linen", "denim", "wool", "blend", "net", "velvet")),
        AttributeSpec("material", "enum", ("plastic", "steel", "glass", "wood", "ceramic",
                                           "silicone", "aluminium", "brass", "fabric", "leather", "rubber")),
        AttributeSpec("color", "string"),
        AttributeSpec("pack_size", "integer"),
        AttributeSpec("size", "enum", ("XS", "S", "M", "L", "XL", "XXL", "XXXL", "free_size")),
        AttributeSpec("occasion", "enum", ("casual", "festive", "wedding", "party", "formal", "daily")),
        AttributeSpec("pattern", "enum", ("solid", "printed", "embroidered", "striped",
                                          "checked", "floral", "embellished")),
        AttributeSpec("sleeve_length", "enum", ("sleeveless", "short_sleeve", "three_quarter", "full_sleeve")),
        AttributeSpec("gender", "enum", ("women", "men", "unisex", "girls", "boys")),
        AttributeSpec("capacity_ml", "integer"),
        AttributeSpec("closure_type", "enum", ("zip", "button", "elastic", "drawstring", "hook", "lid", "none")),
        AttributeSpec("plating", "enum", ("gold", "silver", "rose_gold", "oxidised", "none")),
    ]
}


@dataclass(frozen=True)
class Category:
    slug: str
    label: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = field(default=())

    @property
    def all_attributes(self) -> tuple[str, ...]:
        return self.required + self.optional


CATEGORIES: dict[str, Category] = {c.slug: c for c in [
    Category("sarees", "Women's Ethnic > Sarees",
             required=("fabric", "color", "pack_size"),
             optional=("occasion", "pattern")),
    Category("kurtis", "Women's Ethnic > Kurtis",
             required=("fabric", "color", "size"),
             optional=("occasion", "pattern", "sleeve_length")),
    Category("tshirts", "Topwear > T-Shirts",
             required=("fabric", "color", "size"),
             optional=("gender", "sleeve_length", "pattern")),
    Category("jeans", "Bottomwear > Jeans",
             required=("color", "size"),
             optional=("gender", "fabric", "closure_type")),
    Category("bedsheets", "Home > Bedsheets",
             required=("fabric", "color", "pack_size"),
             optional=("pattern",)),
    Category("kitchen_storage", "Home > Kitchen & Storage",
             required=("material", "pack_size"),
             optional=("capacity_ml", "color", "closure_type")),
    Category("home_decor", "Home > Decor",
             required=("material", "color"),
             optional=("pack_size", "pattern")),
    Category("footwear", "Footwear",
             required=("color", "size"),
             optional=("gender", "material", "closure_type")),
    Category("jewellery", "Accessories > Jewellery",
             required=("material", "color"),
             optional=("plating", "occasion", "pack_size")),
    Category("mobile_accessories", "Electronics > Mobile Accessories",
             required=("material", "color"),
             optional=("pack_size",)),
]}

UNKNOWN_CATEGORY = "unknown"


def response_schema() -> dict[str, Any]:
    """JSON schema handed to the model for constrained decoding."""
    props: dict[str, Any] = {
        "category": {"type": "STRING", "enum": list(CATEGORIES.keys()) + [UNKNOWN_CATEGORY]},
        "normalized_title": {"type": "STRING"},
        "description": {"type": "STRING"},
    }
    for name, spec in ATTRIBUTES.items():
        if spec.type == "integer":
            props[name] = {"type": "INTEGER"}
        elif spec.type == "enum":
            props[name] = {"type": "STRING", "enum": list(spec.values)}
        else:
            props[name] = {"type": "STRING"}

    return {
        "type": "OBJECT",
        "properties": props,
        # Only these three are required. Marking every attribute required would
        # force the model to invent values for attributes the listing simply
        # does not mention, which is far worse than a missing field.
        "required": ["category", "normalized_title", "description"],
    }


def _normalise(value: Any) -> str:
    """Fold a value to a comparable key: lowercase, separators unified."""
    return str(value).strip().lower().replace(" ", "_").replace("-", "_")


def validate(category_slug: str, raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Filter and type-check extracted attributes against the category schema.

    Returns the accepted attributes and a list of human-readable problems. The
    problems are not thrown away: they are what the moderation service uses to
    decide that a listing needs a human, so an extraction that silently drops
    half its fields does not quietly become a published listing.
    """
    category = CATEGORIES.get(category_slug)
    if category is None:
        return {}, [f"unknown category '{category_slug}'"]

    accepted: dict[str, Any] = {}
    problems: list[str] = []

    for name in category.all_attributes:
        if name not in raw or raw[name] in (None, "", []):
            continue
        spec = ATTRIBUTES[name]
        value = raw[name]

        if spec.type == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                problems.append(f"{name}: expected integer, got {value!r}")
                continue
            if value <= 0:
                problems.append(f"{name}: must be positive, got {value}")
                continue
        elif spec.type == "enum":
            # Matching is case- and separator-insensitive, but the value stored
            # is the canonical one declared in the spec. Normalising only the
            # incoming value would reject "L" for a size enum whose members are
            # uppercase, while normalising both and storing the normalised form
            # would silently rewrite the taxonomy's own casing.
            canonical = {_normalise(v): v for v in spec.values}
            key = _normalise(value)
            if key not in canonical:
                problems.append(f"{name}: '{value}' is not one of {list(spec.values)}")
                continue
            value = canonical[key]
        else:
            value = str(value).strip()

        accepted[name] = value

    for name in category.required:
        if name not in accepted:
            problems.append(f"missing required attribute '{name}'")

    return accepted, problems
