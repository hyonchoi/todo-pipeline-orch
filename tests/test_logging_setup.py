import logging
import re
from pathlib import Path
from hermes_pipeline.logging_setup import configure, new_tick_id, set_tick_id

def test_configure_with_debug_level(tmp_path):
    """configure() with level=DEBUG allows DEBUG messages through stderr."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7, level=logging.DEBUG)
    set_tick_id("01TESTTICKULID0000000000BB")
    logging.getLogger("hermes_pipeline.test2").debug("debug message")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "debug message" in text

def test_configure_default_level_is_info(tmp_path):
    """Default level is INFO — DEBUG messages should not appear."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7)
    set_tick_id("01TESTTICKULID0000000000CC")
    logging.getLogger("hermes_pipeline.test3").debug("should not appear")
    logging.getLogger("hermes_pipeline.test3").info("should appear")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "should appear" in text
    assert "should not appear" not in text

def test_verbose_logger_gated_by_default(tmp_path):
    """pipeline.verbose logger is WARNING by default — INFO messages hidden."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7)
    set_tick_id("01TESTTICKULID0000000000DD")
    vlog = logging.getLogger("pipeline.verbose")
    vlog.info("verbose info message")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "verbose info message" not in text

def test_verbose_logger_enabled_at_info(tmp_path):
    """When root level is INFO and verbose logger level is set to INFO, verbose messages appear."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7, level=logging.INFO)
    set_tick_id("01TESTTICKULID0000000000EE")
    vlog = logging.getLogger("pipeline.verbose")
    vlog.setLevel(logging.INFO)  # Simulates --verbose flag
    vlog.info("verbose info message enabled")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "verbose info message enabled" in text

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

def test_debug_mode_enables_verbose(tmp_path):
    """When --debug is used, both DEBUG and verbose messages should appear."""
    log_path = tmp_path / "pipeline.log"
    configure(log_path, retention_days=7, level=logging.DEBUG)
    set_tick_id("01TESTTICKULID0000000000FF")
    # Simulate --debug also enabling verbose logger (as main() does)
    logging.getLogger("pipeline.verbose").setLevel(logging.INFO)
    vlog = logging.getLogger("pipeline.verbose")
    vlog.info("verbose message")
    logging.getLogger("hermes_pipeline.debug_test").debug("debug message")
    for h in logging.getLogger().handlers:
        h.flush()
    text = log_path.read_text()
    assert "verbose message" in text
    assert "debug message" in text
