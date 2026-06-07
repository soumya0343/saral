import pytest

from saral.agents.triage import TriageAgent, classify
from saral.llm.base import LLMError
from saral.llm.sarvam import _map_lid
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


# --- Sarvam /text-lid mapping (TRD §12.2) ---


@pytest.mark.parametrize(
    "data,text,expected",
    [
        ({"language_code": "hi-IN", "script_code": "Latn"}, "mera claim", Language.HINGLISH),
        ({"language_code": "hi-IN", "script_code": "Deva"}, "मेरा क्लेम", Language.HI),
        ({"language_code": "en-IN", "script_code": "Latn"}, "hello", Language.EN),
        # Out-of-scope language falls back to a script heuristic (corpus is en/hi/hinglish only).
        ({"language_code": "ta-IN", "script_code": "Taml"}, "வணக்கம்", Language.EN),
        ({"language_code": "ta-IN", "script_code": "Deva"}, "मराठी", Language.HI),
        ({}, "no fields", Language.EN),
    ],
)
def test_map_lid(data, text, expected):
    assert _map_lid(data, text) == expected


# --- TriageAgent: Sarvam language detection + degraded fallback (TRD §15) ---


class _FakeSarvam:
    def __init__(self, *, available=True, lang=Language.HINGLISH, fail=False):
        self.available = available
        self._lang = lang
        self._fail = fail

    async def detect_language(self, text):
        if self._fail:
            raise LLMError("sarvam down")
        return self._lang


async def _run_with(monkeypatch, fake, message, *, llm_lang=True):
    agent = TriageAgent()
    agent._sarvam = fake
    monkeypatch.setattr(
        "saral.agents.triage.get_settings",
        lambda: type("S", (), {"triage_llm_language": llm_lang})(),
    )
    return await agent.run(message)


async def test_sarvam_language_overrides_deterministic(monkeypatch):
    # Deterministic regex would call this English; Sarvam says hinglish and wins.
    result, degraded = await _run_with(
        monkeypatch, _FakeSarvam(lang=Language.HINGLISH), "update my number"
    )
    assert result.language == Language.HINGLISH
    assert degraded is False
    # Intent + entity stay deterministic.
    assert any(i.action == "update_contact" for i in result.intents)


async def test_sarvam_failure_falls_back_and_flags_degraded(monkeypatch):
    result, degraded = await _run_with(
        monkeypatch, _FakeSarvam(fail=True), "मेरे क्लेम का स्टेटस क्या है"
    )
    assert degraded is True
    assert result.language == Language.HI  # deterministic detector served


async def test_no_sarvam_key_is_not_degraded(monkeypatch):
    result, degraded = await _run_with(
        monkeypatch, _FakeSarvam(available=False), "what does my policy cover"
    )
    assert degraded is False
    assert result.language == Language.EN


def test_capability_question_is_information_not_write():
    # "Can I file another claim?" ASKS ABOUT the action — must not trigger a write/step-up.
    for msg in (
        "kya main ek aur claim file kar sakti hu?",
        "can I file another claim?",
        "how do I update my number?",
    ):
        r = classify(msg)
        assert any(i.type == IntentType.INFORMATION for i in r.intents), msg
        assert not any(i.type == IntentType.ACTION for i in r.intents), msg


def test_real_write_still_action():
    assert any(i.action == "file_claim" for i in classify("file a claim").intents)
    assert any(
        i.action == "update_contact"
        for i in classify("update my mobile number to 9000000000").intents
    )
