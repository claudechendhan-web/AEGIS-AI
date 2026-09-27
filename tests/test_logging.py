import io
import json
import logging

from observability.logging import configure_logging


def test_structured_logging_emits_json_with_event_fields() -> None:
    stream = io.StringIO()
    handler = configure_logging("INFO", stream)
    logger = logging.getLogger("tests.structured")

    logger.info(
        "request complete", extra={"event": "request_complete", "request_id": "r-1"}
    )

    payload = json.loads(stream.getvalue())
    assert payload["message"] == "request complete"
    assert payload["event"] == "request_complete"
    assert payload["request_id"] == "r-1"
    assert payload["level"] == "INFO"
    handler.flush()
