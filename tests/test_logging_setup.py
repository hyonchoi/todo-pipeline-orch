import logging
import re
from pathlib import Path
from hermes_pipeline.logging_setup import configure, new_tick_id, set_tick_id

def test_new_tick_id_is_ulid_like():
    tid = new_tick_id()
    assert re.fullmatch(r"[0-9A-Z]{26}", tid)

def test_configure_writes_to_file(tmp_path):
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7)
    set_tick_id("01TESTTICKULID0000000000AA")
    logging.getLogger("hermes_pipeline.test").info("hello world")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "hello world" in text
    assert "tick_id=01TESTTICKULID0000000000AA" in text

def test_tick_id_absent_when_unset(tmp_path):
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7)
    set_tick_id(None)
    logging.getLogger("hermes_pipeline.test").info("standalone")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "standalone" in text
    assert "tick_id=" not in text or "tick_id=-" in text
