"""A response_serializer for AlchemicalTaskQueue, specific to the envelope
`tasks.Worker` itself responds with."""

import json
from typing import Any, Dict

from ..serializers import PickleSerializer, Serializer

_TAG_RESULT = b"\x01"
_TAG_ERROR = b"\x00"

_DEFAULT_RESULT_SERIALIZER: Serializer[Any] = PickleSerializer()


class TaskResultSerializer(Serializer[Dict[str, Any]]):
    """Pass this as `response_serializer` for a queue [tasks.Worker][alchemical_queues.tasks.Worker]
    runs, to control how a task's *success value* is serialized without
    giving up Worker's own `{"result": ...}`/`{"error": ..., "error_type": ...}`
    envelope (which [QueuedTask.result][alchemical_queues.tasks.QueuedTask.result]
    needs to tell success from failure).

    This is the "nested" case: `result_serializer` only ever sees the value
    your task handler returned, never the envelope around it. Failures are
    always a plain string message and class name, so they're serialized with
    `json` regardless of `result_serializer`.

    Args:
        result_serializer (Serializer): serializer for a task's success
            value (what you `return` from the task handler). Defaults to
            [PickleSerializer][alchemical_queues.serializers.PickleSerializer],
            matching previous behavior.
    """

    def __init__(
        self, result_serializer: Serializer[Any] = _DEFAULT_RESULT_SERIALIZER
    ) -> None:
        self._result_serializer = result_serializer

    def dumps(self, obj: Dict[str, Any]) -> bytes:
        if isinstance(obj, dict) and "error" in obj:
            return _TAG_ERROR + json.dumps(
                {"error": obj["error"], "error_type": obj.get("error_type")}
            ).encode("utf-8")

        value = obj["result"] if isinstance(obj, dict) and "result" in obj else obj
        return _TAG_RESULT + self._result_serializer.dumps(value)

    def loads(self, data: bytes) -> Dict[str, Any]:
        tag, payload = data[:1], data[1:]
        if tag == _TAG_ERROR:
            decoded = json.loads(payload.decode("utf-8"))
            return {"error": decoded["error"], "error_type": decoded.get("error_type")}
        return {"result": self._result_serializer.loads(payload)}
