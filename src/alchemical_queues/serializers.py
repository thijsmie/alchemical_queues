"""Pluggable (de)serialization for queue/response payloads."""

import json
import pickle
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Generic, Type, TypeVar

if TYPE_CHECKING:
    from pydantic import BaseModel

T = TypeVar("T")
PydanticModel = TypeVar("PydanticModel", bound="BaseModel")


class Serializer(ABC, Generic[T]):
    """Turns a queue entry's data into `bytes` for storage and back.

    Implement this to control how `AlchemicalQueue`/`AlchemicalTaskQueue`
    store the `item` passed to `put()` (and, for `AlchemicalTaskQueue`, the
    `response` passed to `respond()`). Pass an instance via the
    `serializer=` argument of
    [AlchemicalQueues.get][alchemical_queues.AlchemicalQueues.get] and
    friends.
    """

    @abstractmethod
    def dumps(self, obj: T) -> bytes:
        """Serialize `obj` to `bytes` for storage."""

    @abstractmethod
    def loads(self, data: bytes) -> T:
        """Deserialize `data` back into the original object."""


class PickleSerializer(Serializer[Any]):
    """Serializes with `pickle`. This is the default serializer, matching
    alchemical_queues' historical behavior: any pickle-able object is
    accepted, with no schema or type checking."""

    def dumps(self, obj: Any) -> bytes:
        return pickle.dumps(obj)

    def loads(self, data: bytes) -> Any:
        return pickle.loads(data)  # noqa: S301


class JsonSerializer(Serializer[T]):
    """Serializes with the standard library `json` module. `obj` must be
    JSON-serializable (dicts, lists, strings, numbers, bools, `None`)."""

    def dumps(self, obj: T) -> bytes:
        return json.dumps(obj).encode("utf-8")

    def loads(self, data: bytes) -> T:
        return json.loads(data.decode("utf-8"))


class PydanticSerializer(Serializer[PydanticModel]):
    """Serializes a single [pydantic](https://docs.pydantic.dev/) model type
    to/from its JSON representation. Requires `pydantic` to be installed
    (`pip install alchemical_queues[pydantic]`).

    Args:
        model (Type[PydanticModel]): the pydantic model class every entry
            put through the queue is an instance of.
    """

    def __init__(self, model: Type[PydanticModel]) -> None:
        self._model = model

    def dumps(self, obj: PydanticModel) -> bytes:
        return obj.model_dump_json().encode("utf-8")  # type: ignore[attr-defined]

    def loads(self, data: bytes) -> PydanticModel:
        return self._model.model_validate_json(data)  # type: ignore[attr-defined]
