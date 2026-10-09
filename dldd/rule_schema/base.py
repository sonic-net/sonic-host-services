"""Shared primitives for strict, versioned DLDD input models."""

from __future__ import annotations

from typing import Dict, List, Union

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    WithJsonSchema,
)
from pydantic_core import PydanticCustomError
from typing_extensions import Annotated, TypeAliasType


UINT32_MAX = 0xFFFFFFFF
INT64_MIN = -(2**63)
UINT64_MAX = 2**64 - 1


class ContractModel(BaseModel):
    """Closed, strict base for fields owned by the DLDD contract."""

    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        validate_default=True,
        allow_inf_nan=False,
        frozen=True,
        hide_input_in_errors=True,
        loc_by_alias=True,
        revalidate_instances="always",
    )


NonEmptyString = Annotated[StrictStr, Field(min_length=1)]
PositiveInteger = Annotated[StrictInt, Field(ge=1, le=UINT32_MAX)]
NonNegativeInteger = Annotated[StrictInt, Field(ge=0, le=UINT32_MAX)]
NonNegativeSeconds = Annotated[StrictInt, Field(ge=0, le=UINT32_MAX)]
PositiveSeconds = Annotated[StrictInt, Field(ge=1, le=UINT32_MAX)]
SamplingInterval = Annotated[StrictInt, Field(ge=1, le=UINT32_MAX)]
JsonInteger = Annotated[StrictInt, Field(ge=INT64_MIN, le=UINT64_MAX)]


def _strict_float(value):
    # StrictFloat accepts integers, but the wire contract does not.
    if type(value) is not float:
        raise PydanticCustomError("float_type", "value must be a float")
    return value


FiniteFloat = Annotated[
    StrictFloat,
    BeforeValidator(_strict_float),
    Field(allow_inf_nan=False),
    # JSON Schema cannot distinguish a Python int from an integral float.
    WithJsonSchema(
        {
            "type": "number",
            "x-dldd-runtime-constraint": {
                "authority": "pydantic-runtime-contract",
                "kind": "python-float-type",
                "description": (
                    "The parsed value must be an actual finite Python float; "
                    "an integer cannot use this union branch."
                ),
            },
        },
        mode="validation",
    ),
]
FiniteNumber = Union[JsonInteger, FiniteFloat]


# Named recursion keeps the generated schema compact.
JsonValue = TypeAliasType(
    "JsonValue",
    Union[
        None,
        StrictStr,
        StrictBool,
        JsonInteger,
        FiniteFloat,
        List["JsonValue"],
        Dict[str, "JsonValue"],
    ],
)

NonNullJsonValue = TypeAliasType(
    "NonNullJsonValue",
    Union[
        StrictStr,
        StrictBool,
        JsonInteger,
        FiniteFloat,
        List[JsonValue],
        Dict[str, JsonValue],
    ],
)


def omitted_non_null_field():
    """Describe an omissible wire field for which explicit null is invalid.

    A default factory keeps the field out of JSON Schema's ``required`` list
    without publishing ``null`` as an accepted type.  Validation is skipped
    only for the internal omitted value; an explicitly supplied ``None`` still
    goes through the declared non-null annotation and fails.
    ``model_fields_set`` records whether the field was supplied.
    """

    return Field(default_factory=lambda: None, validate_default=False)
