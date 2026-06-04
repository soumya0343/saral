from saral.compliance import audit


def test_chain_verifies():
    entries = audit.build_chain(
        [("allow", "authz", "owns claim"), ("allow", "authz", "self-service")]
    )
    assert audit.verify_chain(entries)
    assert entries[0].hash_prev == audit.GENESIS
    assert entries[1].hash_prev == entries[0].hash_self


def test_tamper_breaks_chain():
    entries = audit.build_chain([("allow", "authz", "x"), ("block", "authz", "y")])
    # Mutate a reason without recomputing hashes -> chain must fail.
    entries[0].reason = "tampered"
    assert not audit.verify_chain(entries)


def test_reorder_breaks_chain():
    entries = audit.build_chain([("allow", "authz", "a"), ("block", "authz", "b")])
    entries.reverse()
    assert not audit.verify_chain(entries)
