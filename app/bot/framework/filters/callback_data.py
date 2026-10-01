"""Typed callback data, packed into a Discord component `custom_id`.

Same wire format as aiogram (`prefix:field1:field2`), so every button in the port carries exactly
the string it carried on Telegram. Discord allows 100 characters where Telegram allowed 64 bytes, so
anything that fit before fits now.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from app.bot.framework.filters import Filter
from app.bot.framework.magic import MagicFilter

MAX_CALLBACK_LENGTH = 100


class CallbackDataException(Exception):
    pass


class CallbackData(BaseModel):
    if TYPE_CHECKING:
        __separator__: ClassVar[str]
        __prefix__: ClassVar[str]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        if "prefix" not in kwargs:
            raise ValueError(f"prefix required, usage: `class {cls.__name__}(CallbackData, prefix='my_callback')`")
        cls.__separator__ = kwargs.pop("sep", ":")
        cls.__prefix__ = kwargs.pop("prefix")
        if cls.__separator__ in cls.__prefix__:
            raise ValueError(f"Separator {cls.__separator__!r} can't be used in prefix {cls.__prefix__!r}")
        super().__init_subclass__(**kwargs)

    def _encode_value(self, key: str, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, enum.Enum):
            return str(value.value)
        if isinstance(value, UUID):
            return value.hex
        if isinstance(value, bool):
            return str(int(value))
        if isinstance(value, (int, str, float, Decimal, Fraction)):
            return str(value)
        raise ValueError(f"Attribute {key}={value!r} of type {type(value).__name__!r} can not be packed")

    def pack(self) -> str:
        result = [self.__prefix__]
        for key in type(self).model_fields:
            encoded = self._encode_value(key, getattr(self, key))
            if self.__separator__ in encoded:
                raise ValueError(f"Separator symbol {self.__separator__!r} can't be used in value {key}={encoded!r}")
            result.append(encoded)
        packed = self.__separator__.join(result)
        if len(packed) > MAX_CALLBACK_LENGTH:
            raise ValueError(f"Resulted callback data is too long! len({packed!r}) > {MAX_CALLBACK_LENGTH}")
        return packed

    @classmethod
    def unpack(cls, value: str) -> CallbackData:
        prefix, *parts = value.split(cls.__separator__)
        names = list(cls.model_fields)
        if len(parts) != len(names):
            raise TypeError(f"Callback data {cls.__name__!r} takes {len(names)} arguments but {len(parts)} were given")
        if prefix != cls.__prefix__:
            raise ValueError(f"Bad prefix ({prefix!r} != {cls.__prefix__!r})")
        payload: dict[str, Any] = {}
        for name, part in zip(names, parts, strict=True):
            field = cls.model_fields[name]
            if part == "" and not field.is_required() and field.default not in ("", PydanticUndefined):
                part = field.default
            payload[name] = part
        return cls(**payload)

    @classmethod
    def filter(cls, rule: MagicFilter | None = None) -> CallbackQueryFilter:
        return CallbackQueryFilter(callback_data=cls, rule=rule)


class CallbackQueryFilter(Filter):
    def __init__(self, *, callback_data: type[CallbackData], rule: MagicFilter | None = None) -> None:
        self.callback_data = callback_data
        self.rule = rule

    async def __call__(self, query: Any) -> bool | dict[str, Any]:
        data = getattr(query, "data", None)
        if not isinstance(data, str) or not data:
            return False
        try:
            callback_data = self.callback_data.unpack(data)
        except (TypeError, ValueError):
            return False
        if self.rule is None or self.rule.resolve(callback_data):
            return {"callback_data": callback_data}
        return False


__all__ = ["CallbackData", "CallbackDataException", "CallbackQueryFilter"]
