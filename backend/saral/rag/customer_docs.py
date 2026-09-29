"""Templated per-customer documents for any customer, built from their structured rows.

Hero customers have hand-authored schedules and adjudication notes. Everyone else (sandbox
customers created at onboarding, thin customers, and any claim filed through the agent) would
otherwise have nothing personal to ground on. This renders a policy schedule per policy and a
status note per claim, in English and Hindi, from the core-system rows only — no facts beyond
the row and the product's standard terms (which match the generic corpus: 30-day grace
period, 30/60-day pre/post hospitalisation). An authored document for the same policy or claim
always wins; the generated one is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass

from saral.tools.schemas import ClaimStatus, Policy


@dataclass(frozen=True)
class GeneratedDoc:
    doc_id: str
    domain: str
    language: str
    about: frozenset[str]
    text: str


_PRODUCT = {
    "health": ("Health Insurance", "स्वास्थ्य बीमा"),
    "motor": ("Motor Insurance", "मोटर बीमा"),
    "term_life": ("Term Life Insurance", "टर्म लाइफ बीमा"),
    "personal_loan": ("Personal Loan", "पर्सनल लोन"),
}
_POLICY_STATUS = {
    "active": ("active (in force)", "सक्रिय (प्रभावी)"),
    "lapsed": ("lapsed — the premium was not paid within the grace period", "लैप्स — प्रीमियम अनुग्रह अवधि में नहीं भरा गया"),  # noqa: E501
    "cancelled": ("cancelled", "रद्द"),
}
CLAIM_STATUS = {
    "filed": ("filed", "दर्ज"),
    "under_review": ("under review", "समीक्षाधीन"),
    "approved": ("approved", "स्वीकृत"),
    "partially_approved": ("partially approved", "आंशिक रूप से स्वीकृत"),
    "rejected": ("rejected", "अस्वीकृत"),
    "settled": ("settled", "निपटाया गया"),
}


def inr(amount: float | None) -> str:
    """₹ with Indian digit grouping: 500000 -> ₹5,00,000."""
    if amount is None:
        return "—"
    n = int(round(amount))
    s = str(n)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return f"₹{s}"


def _terms_en(p: Policy) -> str:
    if p.product == "health":
        return (
            "## 2. Coverage\n\n"
            f"2.1 In-patient hospitalisation for admissions of 24 hours or more is covered up "
            f"to the Sum Insured of {inr(p.sum_assured_inr)} per policy year.\n\n"
            "2.2 Pre-hospitalisation expenses for 30 days and post-hospitalisation expenses for "
            "60 days related to the same illness are covered."
        )
    if p.product == "motor":
        return (
            "## 2. Coverage\n\n"
            "2.1 Own-damage to the insured vehicle from accident, fire, theft and natural "
            f"calamities is covered up to the Insured Declared Value of {inr(p.sum_assured_inr)}, "
            "along with third-party liability."
        )
    if p.product == "term_life":
        return (
            "## 2. Coverage\n\n"
            f"2.1 On the death of the life assured while the policy is in force, the Sum "
            f"Assured of {inr(p.sum_assured_inr)} is payable to the nominee."
        )
    return (
        "## 2. Repayment\n\n"
        "2.1 The loan is repaid in equated monthly instalments (EMIs). A missed EMI attracts a "
        "late-payment fee and may affect the credit score."
    )


def _terms_hi(p: Policy) -> str:
    if p.product == "health":
        return (
            "## 2. कवरेज\n\n"
            f"2.1 24 घंटे या उससे अधिक के अस्पताल में भर्ती का खर्च प्रति पॉलिसी वर्ष "
            f"{inr(p.sum_assured_inr)} की बीमा राशि तक कवर है।\n\n"
            "2.2 उसी बीमारी से जुड़े भर्ती से पहले के 30 दिन और छुट्टी के बाद के 60 दिन के खर्च "
            "कवर हैं।"
        )
    if p.product == "motor":
        return (
            "## 2. कवरेज\n\n"
            "2.1 दुर्घटना, आग, चोरी और प्राकृतिक आपदा से बीमित वाहन को हुआ नुकसान "
            f"{inr(p.sum_assured_inr)} के बीमित घोषित मूल्य तक कवर है, साथ ही थर्ड-पार्टी देयता भी।"
        )
    if p.product == "term_life":
        return (
            "## 2. कवरेज\n\n"
            f"2.1 पॉलिसी प्रभावी रहते हुए बीमित व्यक्ति की मृत्यु पर {inr(p.sum_assured_inr)} "
            "की बीमा राशि नामांकित व्यक्ति को देय है।"
        )
    return (
        "## 2. भुगतान\n\n"
        "2.1 लोन बराबर मासिक किस्तों (ईएमआई) में चुकाया जाता है। ईएमआई छूटने पर लेट-पेमेंट शुल्क "
        "लगता है और क्रेडिट स्कोर पर असर पड़ सकता है।"
    )


def _schedule_en(p: Policy) -> str:
    title, _ = _PRODUCT.get(p.product, (p.product, p.product))
    status, _ = _POLICY_STATUS.get(p.status, (p.status, p.status))
    premium = (
        f"3.1 The premium is {inr(p.premium_inr)} per year, due on {p.renewal_date}.\n\n"
        if p.premium_inr
        else ""
    )
    return (
        f"# {title} — Policy Schedule {p.policy_id}\n\n"
        f"**Policy No.:** {p.policy_id}\n"
        f"**Product:** {title}\n"
        f"**Status:** {status}\n"
        f"**Renewal / due date:** {p.renewal_date}\n\n"
        f"## 1. Policy status\n\n1.1 Policy {p.policy_id} is {status}.\n\n"
        f"{_terms_en(p)}\n\n"
        "## 3. Premium, grace period and lapse\n\n"
        f"{premium}"
        "3.2 A grace period of 30 days is allowed after the due date. If the premium is not "
        "paid within the grace period, the policy lapses and must be reinstated."
    )


def _schedule_hi(p: Policy) -> str:
    _, title = _PRODUCT.get(p.product, (p.product, p.product))
    _, status = _POLICY_STATUS.get(p.status, (p.status, p.status))
    premium = (
        f"3.1 प्रीमियम {inr(p.premium_inr)} प्रति वर्ष है, जिसकी देय तिथि {p.renewal_date} है।\n\n"
        if p.premium_inr
        else ""
    )
    return (
        f"# {title} — पॉलिसी अनुसूची {p.policy_id}\n\n"
        f"**पॉलिसी संख्या:** {p.policy_id}\n"
        f"**उत्पाद:** {title}\n"
        f"**स्थिति:** {status}\n"
        f"**नवीनीकरण / देय तिथि:** {p.renewal_date}\n\n"
        f"## 1. पॉलिसी की स्थिति\n\n1.1 पॉलिसी {p.policy_id} की स्थिति: {status}।\n\n"
        f"{_terms_hi(p)}\n\n"
        "## 3. प्रीमियम, अनुग्रह अवधि और लैप्स\n\n"
        f"{premium}"
        "3.2 देय तिथि के बाद 30 दिन की अनुग्रह अवधि मिलती है। इस अवधि में प्रीमियम न भरने पर "
        "पॉलिसी लैप्स हो जाती है और उसे फिर से चालू (रीइंस्टेट) कराना होता है।"
    )


def _claim_en(c: ClaimStatus) -> str:
    status, _ = CLAIM_STATUS.get(c.status, (c.status, c.status))
    amount = f"**Amount:** {inr(c.amount_inr)}\n" if c.amount_inr is not None else ""
    return (
        f"# Claim {c.claim_id} — Status Note\n\n"
        f"**Claim No.:** {c.claim_id}\n**Policy No.:** {c.policy_id}\n"
        f"**Status:** {status}\n{amount}**Last updated:** {c.last_updated}\n\n"
        f"## Assessor's note\n\nClaim {c.claim_id} is {status}. {c.note or ''}".rstrip()
    )


def _claim_hi(c: ClaimStatus) -> str:
    _, status = CLAIM_STATUS.get(c.status, (c.status, c.status))
    amount = f"**राशि:** {inr(c.amount_inr)}\n" if c.amount_inr is not None else ""
    return (
        f"# क्लेम {c.claim_id} — स्थिति टिप्पणी\n\n"
        f"**क्लेम संख्या:** {c.claim_id}\n**पॉलिसी संख्या:** {c.policy_id}\n"
        f"**स्थिति:** {status}\n{amount}**अंतिम अपडेट:** {c.last_updated}\n\n"
        f"## मूल्यांकन टिप्पणी\n\nक्लेम {c.claim_id} की स्थिति: {status}। {c.note or ''}".rstrip()
    )


def generate_documents(
    user_id: str,
    policies: list[Policy],
    claims: list[ClaimStatus],
    authored: set[str],
) -> list[GeneratedDoc]:
    """Schedules + claim notes for every policy / claim without an authored document."""
    docs: list[GeneratedDoc] = []
    for p in policies:
        if p.policy_id in authored:
            continue
        about = frozenset({p.policy_id})
        base = f"{user_id}/generated/{p.policy_id}_schedule"
        docs.append(GeneratedDoc(f"{base}.md", "policy_coverage", "en", about, _schedule_en(p)))
        docs.append(GeneratedDoc(f"{base}_hi.md", "policy_coverage", "hi", about, _schedule_hi(p)))
    for c in claims:
        if c.claim_id in authored:
            continue
        about = frozenset({c.claim_id, c.policy_id})
        base = f"{user_id}/generated/{c.claim_id}"
        docs.append(GeneratedDoc(f"{base}.md", "claims", "en", about, _claim_en(c)))
        docs.append(GeneratedDoc(f"{base}_hi.md", "claims", "hi", about, _claim_hi(c)))
    return docs
