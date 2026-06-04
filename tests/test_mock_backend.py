import pytest

from saral.tools import mock_backend as mb
from saral.tools.mock_backend import ToolError


def setup_function():
    mb._reset_state()


def test_get_claim_status_ok():
    claim = mb.get_claim_status("CLM2001")
    assert claim.status == "under_review"


def test_get_claim_status_not_found():
    with pytest.raises(ToolError):
        mb.get_claim_status("CLM9999")


def test_get_policy_details_ok():
    policy = mb.get_policy_details("pol1001")  # case-insensitive
    assert policy.holder_user_id == "U1001"


def test_update_contact_idempotent():
    r1 = mb.update_contact("U1001", "mobile", "9000000000", idempotency_key="k1")
    r2 = mb.update_contact("U1001", "mobile", "9111111111", idempotency_key="k1")
    assert r1.ok and r2.ok
    # same key -> same result, second value NOT applied
    assert r1.changed == r2.changed
    assert r2.changed["new"] == "9000000000"


def test_update_contact_invalid_field():
    with pytest.raises(ToolError):
        mb.update_contact("U1001", "name", "X", idempotency_key="k2")


def test_raise_ticket_idempotent():
    t1 = mb.raise_ticket("U1002", "EMI issue", "body", idempotency_key="t1")
    t2 = mb.raise_ticket("U1002", "EMI issue", "body", idempotency_key="t1")
    assert t1.ticket_id == t2.ticket_id
