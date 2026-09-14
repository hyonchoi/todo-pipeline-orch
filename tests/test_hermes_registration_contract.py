"""Optional live Hermes registration-barrier contract test."""

from tests.support.hermes_contract import live_hermes_registration_barrier_contract


def test_live_hermes_registration_barrier_contract(tmp_path):
    """Hermes must keep an unassigned ready barrier and its child nonspawnable."""
    live_hermes_registration_barrier_contract(tmp_path)
