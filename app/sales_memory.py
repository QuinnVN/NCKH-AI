"""Authored customer facts and committed dialogue memory, separate from scores."""
from __future__ import annotations

import copy
import re
import unicodedata
from collections.abc import Mapping

MEMORY_VERSION = "sales-memory-v1"
QUESTION_FACTS = (
    ("receiptRequest", "receipt_status"), ("purchaseQuestion", "purchase_history"),
    ("conditionQuestion", "shoe_condition"), ("socksQuestion", "socks"),
    ("previousShoesQuestion", "previous_shoes"), ("productQuestion", "product_details"),
    ("budgetQuestion", "budget"), ("packagingQuestion", "packaging_status"),
    ("medicalQuestion", "medical_history"), ("durationQuestion", "use_duration"),
)
RECALL_FACTS = {"walkingRecall": "walking_routine", "fitRecall": "fit_condition",
                "preferenceRecall": "lighter_preference"}
OPENING_FACTS = ("purchase_history", "complaint", "prior_advice")
UNKNOWN_TEXT = {
    "product_details": "Chị chưa nhớ rõ mã mẫu và giá của đôi giày này.",
    "budget": "Chị chưa nói mức tiền có thể chi thêm cho lần đổi này.",
    "packaging_status": "Chị chưa kiểm tra lại hộp và tem của đôi giày này.",
    "medical_history": "Chị không rõ nguyên nhân liên quan đến sức khỏe, chị chỉ biết mang đôi này thì đau chân.",
}


def initialize_memory(session: dict) -> None:
    from app.sales_openrouter import FACTS
    # The profile is private knowledge. Only the opening facts are already
    # shared with the salesperson; private facts do not grant rubric credit.
    facts = {fact: {"text": text, "source": "scenario", "certainty": "known",
                    "shared": fact in OPENING_FACTS, "sharedTurnId": "opening" if fact in OPENING_FACTS else None}
             for fact, text in FACTS.items()}
    for fact, text in UNKNOWN_TEXT.items():
        facts[fact] = {"text": text, "source": "unspecified", "certainty": "unknown",
                       "shared": False, "sharedTurnId": None}
    session["customerMemory"] = {"version": MEMORY_VERSION, "facts": facts,
        "actions": {"inspection": "not_requested", "trial": "not_requested", "handover": "not_requested",
                    "exchange": "not_offered"},
        "requests": {}, "priorSaleLinked": False}
    # Part 1 supplies these when the prior sale is linked.
    for fact in ("walking_routine", "lighter_preference"):
        facts[fact].update(source="unspecified", certainty="unknown")
    session["investigationEvidence"] = sorted(fact for fact in OPENING_FACTS if fact in facts)


def _plain(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
                   if unicodedata.category(c) != "Mn")


def link_prior_sale(session: dict, attempt: Mapping) -> None:
    """Bind a prior purchase only after explicit same-customer authorization."""
    identity = session.get("customerIdentity", {})
    if not identity.get("continuesPart1Customer"):
        return
    if (attempt.get("salesSessionId") != session.get("sessionId")
            or attempt.get("runId") != session.get("runId")
            or attempt.get("customerId") != identity.get("customerId")):
        raise ValueError("Part 1 context belongs to another customer, session or run")
    memory = session.get("customerMemory")
    if not memory or memory.get("priorSaleLinked"):
        return
    if session.get("turns") or session.get("pendingTurns") or session.get("turnCheckpoints"):
        raise ValueError("cannot change customer context after dialogue has started")
    needs = str(attempt.get("customerNeeds", ""))[:2000]
    normalized = _plain(needs)
    facts = memory["facts"]

    def shared(fact, text):
        facts[fact] = {"text": text, "source": "part1", "certainty": "known",
                       "shared": True, "sharedTurnId": "part1"}

    activities = []
    if "chay bo" in normalized:
        running = "chạy bộ"
        distance = re.search(r"(?:khoảng\s+)?\d+\s*(?:(?:đến|[-–])\s*\d+\s*)?(?:cây số|km|kilômét)", needs, re.I)
        if distance:
            running += " " + distance.group()
        if "moi sang" in normalized:
            running += " mỗi sáng"
        if "duong nhua" in normalized:
            running += " trên đường nhựa"
        activities.append(running)
    if "di bo" in normalized:
        activities.append(("thỉnh thoảng " if "thinh thoang di bo" in normalized else "") + "đi bộ")
    if activities:
        routine = "Chị dùng giày để " + ", và ".join(activities)
        if "moi sang" not in normalized and ("hang ngay" in normalized or "moi ngay" in normalized):
            routine += " hằng ngày"
        if "chay bo" not in normalized and "duong nhua" in normalized:
            routine += " trên đường nhựa"
        shared("walking_routine", routine + ".")
    if "nhe" in normalized:
        shared("lighter_preference", "Chị đã nói chị thích giày nhẹ.")
    # Budget at the original sale is not permission to pay a surcharge now.
    if "ngan sach" in normalized:
        original = next((part.strip() for part in re.split(r"[!?\n]|\.(?=\s|$)", needs)
                         if "ngan sach" in _plain(part)), "")
        memory["originalBudget"] = original[:300]
        if original:
            shared("budget", "Lần trước chị nói " + original.rstrip(".")
                   + "; chị chưa xác định khoản chi thêm cho lần đổi này.")
            facts["budget"]["certainty"] = "unknown"
    selected = attempt.get("selectedShoeId")
    shoe = next((item for item in attempt.get("availableShoes", []) if isinstance(item, Mapping)
                 and item.get("shoeId") == selected), None)
    if shoe:
        name = str(shoe.get("name") or selected)[:80]
        price = shoe.get("price")
        text = f"Chị mua mẫu {name}"
        if isinstance(price, int) and not isinstance(price, bool) and 0 < price < 1_000_000_000:
            text += f", giá {price} đồng"
        shared("product_details", text + ".")
        memory["purchasedShoe"] = {"name": name, "price": price,
                                   "details": str(shoe.get("details", ""))[:600]}
    for fact in ("use_duration", "previous_shoes", "original_missed_question"):
        facts[fact].update(source="unspecified", certainty="unknown")
    memory["priorSaleLinked"] = True
    session["investigationEvidence"] = sorted(set(session.get("investigationEvidence", [])) |
        {fact for fact, record in facts.items() if record["shared"]})


def scenario_context(context: Mapping) -> dict:
    """Scenario sent to models: policy, cause and the known fact texts only."""
    from app.sales_openrouter import SCENARIO
    scenario = copy.deepcopy(SCENARIO)
    texts = context.get("factTexts", {})
    scenario["facts"] = {fact: text[:600] for fact, text in texts.items()
                         if fact in scenario["facts"] and isinstance(text, str)} if isinstance(texts, Mapping) else {}
    return scenario


def fact_texts(session: Mapping, fact_ids=None) -> dict[str, str]:
    from app.sales_openrouter import FACTS
    records = (session.get("customerMemory") or {}).get("facts", {})
    selected = fact_ids if fact_ids is not None else records
    return {fact: str(records.get(fact, {}).get("text") or FACTS[fact])[:600]
            for fact in selected if fact in FACTS}


def model_context(session: Mapping) -> dict:
    """Only fact text and small dialogue state cross the model boundary."""
    memory = session.get("customerMemory") or {}
    known = session.get("investigationEvidence", [])
    return {"factTexts": fact_texts(session, known), "dialogueMemory": {
        "actions": copy.deepcopy(memory.get("actions", {})),
        "unknownFactIds": sorted(fact for fact, record in memory.get("facts", {}).items()
                                 if record.get("certainty") == "unknown"),
        "priorSaleLinked": bool(memory.get("priorSaleLinked")),
        "purchasedShoe": copy.deepcopy(memory.get("purchasedShoe", {})),
    }}


def bounded_model_memory(context: Mapping) -> dict:
    """Allowlist snapshots supplied to classifier and completion arbitration."""
    from app.sales_openrouter import FACTS
    known = context.get("knownFacts", [])
    texts = context.get("factTexts", {})
    texts = texts if isinstance(texts, Mapping) else {}
    known = known if isinstance(known, (list, tuple)) else []
    raw = context.get("dialogueMemory", {})
    raw = raw if isinstance(raw, Mapping) else {}
    actions = raw.get("actions", {})
    actions = actions if isinstance(actions, Mapping) else {}
    safe = {"actions": {key: value for key, value in actions.items()
                        if key in ("inspection", "trial", "handover", "exchange")
                        and value in ("not_requested", "requested", "consented", "deferred",
                                      "not_offered", "offered", "withdrawn", "accepted")},
            "priorSaleLinked": raw.get("priorSaleLinked") is True,
            "unknownFactIds": [fact for fact in raw.get("unknownFactIds", []) if fact in FACTS]
            if isinstance(raw.get("unknownFactIds"), (list, tuple)) else []}
    purchased = raw.get("purchasedShoe", {})
    if isinstance(purchased, Mapping):
        safe["purchasedShoe"] = {key: value[:600] if isinstance(value, str) else value
            for key, value in purchased.items() if key in ("name", "details", "price")
            and (isinstance(value, str) or (type(value) is int and 0 < value < 1_000_000_000))}
    return {"known_fact_texts": {fact: texts[fact][:600] for fact in known
                                if fact in FACTS and isinstance(texts.get(fact), str)},
            "dialogue_memory": safe}


def confirmed_facts(session: Mapping, labels: Mapping) -> list[str]:
    """A recall needs both a recognized current act and an existing shared fact."""
    known = set(session.get("investigationEvidence", []))
    return [fact for label, fact in RECALL_FACTS.items()
            if fact in known and labels.get(label, {}).get("status") == "true"]


def related_facts(session: Mapping, fact_ids) -> list[str]:
    """Some authored answers contain more than one atomic fact."""
    texts = fact_texts(session, fact_ids)
    related = []
    preference = _plain(texts.get("lighter_preference", ""))
    if "tat mong" in preference:
        related.append("socks")
    if "doi cu" in preference or "doi truoc" in preference:
        related.append("previous_shoes")
    fit = _plain(texts.get("fit_condition", ""))
    if "nguyen ven" in fit or "khong bi hong" in fit:
        related.append("shoe_condition")
    return related


def remember_volunteered_facts(state: dict, plan: Mapping, text: str, turn_id: str) -> list[str]:
    """Remember only allowed authored clauses actually spoken by Lan."""
    from app.sales_openrouter import _asserted_fact
    normalized = _plain(text)
    actions = state.get("customerMemory", {}).get("actions", {})
    content = plan.get("requiredContent") or []
    if "physical_deferral" in content:
        for key in ("inspection", "trial", "handover"):
            if actions.get(key) == "requested":
                actions[key] = "deferred"
    disclosed = []
    for fact in plan.get("allowedVolunteeredFacts", []):
        asserted = _asserted_fact(normalized, fact)
        if fact == "socks":
            asserted = bool(re.search(r"(?:thuong )?mang tat mong", normalized))
        elif fact == "previous_shoes":
            asserted = asserted or "nhe nhu doi cu" in normalized
        if fact == "shoe_condition":
            # Wearing the pair was already stated in the opening complaint.
            asserted = "nguyen ven" in normalized or "khong bi hong" in normalized
        record = state.get("customerMemory", {}).get("facts", {}).get(fact)
        if asserted and record and not record["shared"]:
            record.update(shared=True, sharedTurnId=turn_id)
            disclosed.append(fact)
    if disclosed:
        state["investigationEvidence"] = sorted(set(state["investigationEvidence"]) | set(disclosed))
        state["evidenceLedger"][-1]["volunteeredFactIds"] = disclosed
    return disclosed


def update_memory(state: dict, labels: Mapping, result: Mapping, turn_id: str) -> None:
    memory = state.get("customerMemory")
    if not memory:
        return
    yes = lambda label: labels.get(label, {}).get("status") == "true"
    for fact in result.get("disclosedFactIds", []):
        record = memory["facts"].get(fact)
        if record and not record["shared"]:
            record.update(shared=True, sharedTurnId=turn_id)
    if yes("refusesRemedy"):
        memory["actions"]["exchange"] = "withdrawn"
    if result.get("turnQuality") in ("bad", "uncertain"):
        return
    for label, fact in QUESTION_FACTS:
        if yes(label):
            memory["requests"][fact] = memory["requests"].get(fact, 0) + 1
    actions = memory["actions"]
    for label, key in (("inspectionRequest", "inspection"), ("fitOrWalkTrial", "trial"),
                       ("physicalActionRequest", "handover")):
        if yes(label):
            actions[key] = "requested"
    if yes("refusesRemedy"):
        actions["exchange"] = "withdrawn"
    elif yes("exchangeOffer"):
        actions["exchange"] = "offered"
    if result.get("endingReason") == "exchange_accepted":
        actions["exchange"] = "accepted"
