"""F-15 structured logging + secret redaction。"""

from __future__ import annotations

import io
import json
import logging
import pathlib
import tempfile
import unittest

from runtime.observability import (REDACTED, clear_registered_secrets, configure_logging,
                                   logging_posture, log_event, redact_text, register_secret,
                                   reset_logging, sanitize)


class StructuredEventTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        configure_logging(level="INFO", stream=self.stream)

    def tearDown(self) -> None:
        reset_logging()
        clear_registered_secrets()

    def _records(self) -> list[dict]:
        return [json.loads(line) for line in self.stream.getvalue().splitlines() if line.strip()]

    def test_event_has_stable_fields(self) -> None:
        log_event("runtime", "startup", runtime_id="rt-1", run_id="run-1", mode="REPLAY",
                  bind_host="127.0.0.1")
        record = self._records()[0]
        for key in ("ts", "level", "event", "component", "runtime_id", "run_id", "mode"):
            with self.subTest(key=key):
                self.assertIn(key, record)
        self.assertEqual(record["event"], "startup")
        self.assertEqual(record["component"], "runtime")
        self.assertEqual(record["level"], "INFO")
        self.assertIsInstance(record["ts"], int)

    def test_reason_code_is_present_when_given(self) -> None:
        log_event("api", "auth_failure", level=logging.WARNING, reason_code="AUTH_TOKEN_MISSING", path="/x")
        record = self._records()[0]
        self.assertEqual(record["reason_code"], "AUTH_TOKEN_MISSING")
        self.assertEqual(record["level"], "WARNING")

    def test_default_level_and_posture(self) -> None:
        posture = logging_posture()
        self.assertEqual(posture.level, "INFO")
        self.assertEqual(posture.sink, "stream")
        self.assertTrue(posture.structured)
        self.assertFalse(posture.bounded)


class RedactionTest(unittest.TestCase):
    def setUp(self) -> None:
        clear_registered_secrets()

    def tearDown(self) -> None:
        clear_registered_secrets()
        reset_logging()

    def test_sensitive_keys_are_redacted(self) -> None:
        cleaned = sanitize({"token": "abc", "api_key": "xyz", "nested": {"authorization": "Bearer q"},
                            "ok": "value"})
        self.assertEqual(cleaned["token"], REDACTED)
        self.assertEqual(cleaned["api_key"], REDACTED)
        self.assertEqual(cleaned["nested"]["authorization"], REDACTED)
        self.assertEqual(cleaned["ok"], "value")

    def test_registered_secret_never_appears_anywhere(self) -> None:
        register_secret("TOPSECRET-123")
        stream = io.StringIO()
        configure_logging(level="INFO", stream=stream)
        log_event("api", "auth_failure", note="token=TOPSECRET-123 was rejected",
                  detail="TOPSECRET-123", signature="abc")
        output = stream.getvalue()
        self.assertNotIn("TOPSECRET-123", output)
        self.assertIn(REDACTED, output)

    def test_signature_query_and_bearer_are_redacted(self) -> None:
        register_secret("tok-xyz")
        text = redact_text("POST /order?signature=deadbeef&x=1 Authorization: Bearer tok-xyz")
        self.assertNotIn("deadbeef", text)
        self.assertNotIn("tok-xyz", text)
        self.assertIn(REDACTED, text)

    def test_exception_message_is_sanitized(self) -> None:
        register_secret("LEAKME")
        stream = io.StringIO()
        configure_logging(level="INFO", stream=stream)
        try:
            raise RuntimeError("boom LEAKME")
        except RuntimeError:
            log_event("api", "unexpected_exception", level=logging.ERROR, reason_code="X")
            logging.getLogger("probex.api").error("unexpected_exception", exc_info=True,
                                                  extra={"event": "unexpected_exception",
                                                         "component": "api", "fields": {}})
        record = json.loads(stream.getvalue().splitlines()[-1])
        self.assertEqual(record["exception_type"], "RuntimeError")
        self.assertNotIn("LEAKME", record["exception_message"])
        self.assertIn(REDACTED, record["exception_message"])


class FileSinkTest(unittest.TestCase):
    def tearDown(self) -> None:
        reset_logging()
        clear_registered_secrets()

    def test_rotating_file_sink_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posture = configure_logging(level="INFO", file_path=str(pathlib.Path(tmp) / "probex.log"),
                                        file_max_bytes=1024, backup_count=2)
            self.assertTrue(posture.bounded)
            self.assertEqual(posture.sink, "file")
            log_event("runtime", "startup", runtime_id="rt-1")
            self.assertTrue((pathlib.Path(tmp) / "probex.log").exists())

    def test_file_sink_without_size_is_unbounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            posture = configure_logging(level="INFO", file_path=str(pathlib.Path(tmp) / "probex.log"))
            self.assertFalse(posture.bounded)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
