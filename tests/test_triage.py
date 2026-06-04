from saral.agents.triage import classify
from saral.schemas import IntentType, Language


def test_english_information():
    r = classify("What does my health policy cover?")
    assert r.language == Language.EN
    assert any(i.type == IntentType.INFORMATION for i in r.intents)


def test_hindi_claim_status():
    r = classify("मेरे क्लेम का स्टेटस क्या है")
    assert r.language == Language.HI
    assert any(i.action == "get_claim_status" for i in r.intents)


def test_hinglish_update_contact():
    r = classify("mera mobile number update karo to 9876512345")
    assert r.language == Language.HINGLISH
    assert any(i.action == "update_contact" for i in r.intents)
    assert r.entities.get("mobile") == "9876512345"


def test_mixed_intent_is_detected():
    r = classify("What does my policy cover and please update my mobile number")
    assert r.is_mixed
    kinds = {i.type for i in r.intents}
    assert IntentType.INFORMATION in kinds and IntentType.ACTION in kinds


def test_entity_extraction():
    r = classify("Check claim CLM2001 on policy POL1001, email me at a@b.com")
    assert r.entities["claim_id"] == "CLM2001"
    assert r.entities["policy_id"] == "POL1001"
    assert r.entities["email"] == "a@b.com"
