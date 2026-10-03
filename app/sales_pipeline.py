"""Checkpointed HTTP turn pipeline. Network failures never assess the player."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import time
import wave
from dataclasses import replace
from typing import Any, Mapping

from app.config import get_settings
from app.sales_persuasion import decode_sales_audio, is_silent_wav
from app.sales_rubric import VERSIONS, VIOLATIONS, evaluate, normalized_labels, outcome, remaining, supports_versions
from app.sales_openrouter import FACTS as FACT_TEXT

TERMINAL_REPLIES = {
    "good": "Giờ chị thấy yên tâm hơn. Chị hài lòng, cảm ơn em.",
    "considering": "Chị hiểu em đã làm những gì có thể. Hôm nay chị dừng ở đây, nhưng vẫn chưa thực sự hài lòng.",
    "bad": "Chị không nói chuyện với em nữa. Gọi quản lý ra đây cho chị.",
}


def public_fields(state: Mapping) -> dict:
    return {**{name: state.get(name) for name in VERSIONS},
            "assessmentStatus": state.get("assessmentStatus", "pending"),
            "maxTurns": state.get("maxTurns", 8), "remainingTurns": remaining(state),
            "supplementalTurnGranted": state.get("supplementalTurnGranted", False),
            "evaluableTurnCount": state.get("evaluableTurnCount", 0),
            "endingReason": state.get("endingReason")}


def plan_reply(before: Mapping, state: dict, result: dict, labels: Mapping) -> dict:
    from app.sales_returning_customer import MANDATORY_CHALLENGE, UNAUTHORIZED_PROMISE_CHALLENGE
    yes = lambda key: labels.get(key, {}).get("status") == "true"
    phase = int(before["phase"])
    known = set(before.get("investigationEvidence", []))
    facts = []
    intent = "clarification" if result["turnQuality"] == "uncertain" else "continue"
    fallback = "Chị chưa rõ ý em, em nói rõ cách xử lý đôi giày này được không?"
    required = []
    required_content = []
    if result["turnQuality"] == "uncertain":
        fallback = "Chị chưa nghe rõ ý em, em có thể làm rõ điều vừa nói không?"
    elif result.get("conversationComplete"):
        intent = "ending"
        rating = outcome(state)["customerRating"]
        fallback = TERMINAL_REPLIES[rating]
        required_content = ["ending_" + outcome(state)["trustState"]]
    elif state.get("unresolvedPromises"):
        intent, fallback = "promise_challenge", UNAUTHORIZED_PROMISE_CHALLENGE
        required_content = ["promise_challenge"]
    elif phase == 3 and state["phase"] == 4:
        intent, fallback = "trust_challenge", MANDATORY_CHALLENGE
        required_content = ["trust_challenge"]
    elif yes("abuse") or yes("profanityOrInsult") or yes("disrespect") or yes("condescending"):
        intent, fallback = "warning", "Chị thấy cách em nói thiếu tôn trọng, em có thể trao đổi bình tĩnh hơn không?"
    elif yes("repeatedQuestion") and not yes("confirmation"):
        intent, fallback = "repeated_question", "Chị đã trả lời điều đó rồi, em cần làm rõ phần nào nữa?"
    elif phase == 2:
        topics = (("painLocationQuestion", "pain_location"), ("painTimingQuestion", "late_discomfort"),
                  ("walkingQuestion", "walking_routine"), ("useOrDurationQuestion", "walking_routine"),
                  ("fitQuestion", "fit_condition"), ("fitConditionOrPreferenceQuestion", "fit_condition"),
                  ("preferenceQuestion", "lighter_preference"), ("appearanceQuestion", "appearance"))
        facts = list(dict.fromkeys(fact for label, fact in topics if yes(label)))[:2]
        if facts:
            intent, fallback = "fact_disclosure", " ".join(FACT_TEXT[fact] for fact in facts)
    if intent == "continue":
        if phase == 1 and state["phase"] == 2:
            fallback = "Chị ghi nhận lời em, nhưng chị muốn hiểu vì sao đôi giày này gây đau khi đi lại."
        elif phase == 2 and state["phase"] == 3:
            fallback = "Chị hiểu vì sao đôi này chưa hợp, vậy tiệm có phương án xử lý thế nào?"
        elif phase == 3 and "exchangeOffer" in state.get("objectiveEvidence", {}).get("3", {}):
            fallback = "Chị nghe phương án đổi rồi, em có thể nói rõ điều kiện và cách thử đôi mới không?"
    result["disclosedFactIds"] = [fact for fact in facts if fact not in known]
    state["investigationEvidence"] = sorted(known | set(facts))
    return dict(intent=intent, emotion="firm" if result["turnQuality"] == "bad" else "cautious",
                fallbackText=fallback, allowedNewFacts=result["disclosedFactIds"], knownFacts=sorted(known),
                requiredPhrases=required, requiredFactIds=facts, requiredContent=required_content, question=None,
                ending=result.get("endingReason"), unresolvedPromises=state.get("unresolvedPromises", []))


async def process_turn(session_id, request, *, store, transcriber, classifier=None, writer=None):
    from app.sales_returning_customer import _now, _turn_record, _LOCKS
    async with _LOCKS.setdefault(session_id, asyncio.Lock()):
        state = await store.get(session_id)
        if state is None:
            raise KeyError("session_not_found")
        if state.get("diagnosticsDeleted"):
            raise RuntimeError("diagnostics_deleted")
        audio = decode_sales_audio(request.audio)
        request_hash = hashlib.sha256(audio).hexdigest()
        pending = _turn_record(session_id, request, request_hash, int(state["phase"]))
        state, cached = await store.begin_turn(session_id, request.turn_id, request_hash, pending, audio)
        if cached is not None:
            return cached
        checkpoint = state.get("turnCheckpoints", {}).get(request.turn_id, {})
        if checkpoint and checkpoint.get("requestHash") != request_hash:
            raise ValueError("turnId was already used with different audio")
        checkpoint.setdefault("requestHash", request_hash)
        checkpoint.setdefault("receivedAtUtc", _now())
        started = time.monotonic()
        async def save_checkpoint():
            await store.save_checkpoint(session_id, request.turn_id, checkpoint)
        result = dict(pending, **public_fields(state))
        try:
            if not supports_versions(state):
                raise RuntimeError("pipeline_version_unsupported")
            path = store._turn_path(session_id, request.turn_id)
            # All-zero PCM is a device/upload failure, never player silence.
            with wave.open(io.BytesIO(audio), "rb") as source:
                frames = source.readframes(source.getnframes())
            if not any(frames):
                raise RuntimeError("microphone_no_signal")
            capture = request.capture
            if capture is not None and capture.device_ready and capture.permission_granted and not capture.speech_detected:
                result.update(status="silent", accepted=False, turnQuality="neutral", playerResponseRating=None,
                              customerText="Chị chưa nghe em nói, em có thể nói lại không?", conversationComplete=False)
                def commit_silence(current):
                    current.get("turnCheckpoints", {}).pop(request.turn_id, None)
                    current["silenceCount"] += 1
                    if current["silenceCount"] >= 2:
                        current.update(status="awaitingCompletion", endingReason="second_silence")
                        result.update(conversationComplete=True, endingReason="second_silence",
                                      customerText="Chị dừng trao đổi ở đây, chị muốn gặp quản lý.")
                        current["finalCustomerText"] = result["customerText"]
                _, result = await store.finalize_turn(session_id, request.turn_id, result, commit_silence)
                return result
            await save_checkpoint()
            if "transcript" not in checkpoint:
                if is_silent_wav(path):
                    raise RuntimeError("microphone_no_signal")
                deadline = get_settings().sherpa_timeout_seconds
                try:
                    if hasattr(transcriber, "transcribe_with_metadata"):
                        data = await asyncio.wait_for(transcriber.transcribe_with_metadata(path), deadline)
                        checkpoint.update(transcript=data.text.strip(), sttMetadata={"language": data.language,
                                          "provider": data.provider, "model": data.model, "version": data.version})
                    else:
                        checkpoint["transcript"] = (await asyncio.wait_for(transcriber.transcribe(path), deadline)).strip()
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("transcription_timeout") from exc
                except Exception as exc:
                    if getattr(exc, "code", None) in {"transcription_unavailable", "transcription_failed", "audio_read_failed"}:
                        raise RuntimeError(exc.code) from exc
                    raise RuntimeError("transcription_failed") from exc
                if not checkpoint["transcript"]:
                    # An empty STT result is not evidence of intentional silence.
                    raise RuntimeError("transcription_empty")
                checkpoint["transcribedAtUtc"] = _now()
                await save_checkpoint()
            transcript = checkpoint["transcript"]
            if "classification" not in checkpoint:
                if classifier is None:
                    from app.sales_openrouter import JevSalesClassifier
                    classifier = JevSalesClassifier(settings=replace(get_settings(), sales_jev_timeout_seconds=state["jevDeadlineSeconds"]))
                promise_labels = {code: name for name, code in VIOLATIONS.items()}
                context = {"objective": state["phase"], "knownFacts": state.get("investigationEvidence", []),
                           "unresolvedPromiseTypes": [promise_labels[code] for code in state.get("unresolvedPromises", []) if code in promise_labels],
                           "challengeShown": state.get("challengeShown", False),
                           "history": [{"player": item.get("transcript", ""), "lan": item.get("customerText", "")}
                                       for item in state.get("turns", [])[-3:]], **VERSIONS}
                try:
                    classification = await asyncio.wait_for(classifier.classify(transcript, context), state["jevDeadlineSeconds"])
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("classifier_timeout") from exc
                # A response without a label object is an infrastructure failure.
                if not isinstance(classification, Mapping) or not isinstance(classification.get("labels"), Mapping):
                    raise RuntimeError("classifier_invalid")
                checkpoint["classification"] = dict(classification)
                checkpoint["classifiedAtUtc"] = _now()
                await save_checkpoint()
            labels = normalized_labels(checkpoint["classification"])
            if "candidate" not in checkpoint:
                candidate, decision = evaluate(state, labels, request.turn_id)
                plan = plan_reply(state, candidate, decision, labels)
                checkpoint.update(candidate=candidate, decision=decision, plan=plan, plannedAtUtc=_now())
                await save_checkpoint()
            candidate, decision, plan = checkpoint["candidate"], checkpoint["decision"], checkpoint["plan"]
            if "written" not in checkpoint:
                written = {"customerText": plan["fallbackText"], "metadata": {}, "fallbackUsed": True}
                try:
                    if writer is None:
                        from app.sales_openrouter import QwenSalesWriter
                        writer = QwenSalesWriter(settings=replace(get_settings(), sales_writer_timeout_seconds=state["writerDeadlineSeconds"]))
                    written = await asyncio.wait_for(writer.write(plan, transcript, state.get("turns", [])[-3:]), state["writerDeadlineSeconds"])
                    from app.sales_openrouter import validate_customer_text
                    # Production adapter owns validation; injected writers also pass it.
                    if not validate_customer_text(written.get("customerText", ""), plan, transcript):
                        written = {"customerText": plan["fallbackText"], "metadata": written.get("metadata", {}), "fallbackUsed": True}
                except Exception:
                    written = {"customerText": plan["fallbackText"], "metadata": {}, "fallbackUsed": True}
                checkpoint.update(written=written, writtenAtUtc=_now())
                await save_checkpoint()
            written = checkpoint["written"]
            result.update(decision, **public_fields(candidate), status="accepted", accepted=True,
                          transcript=transcript, customerText=written["customerText"],
                          playerResponseRating=decision["turnQuality"] if decision["turnQuality"] in ("good", "bad") else None,
                          activeObjective=candidate["phase"], objectiveActiveDuringTurn=state["phase"],
                          turnAssessment=labels, classifierMetadata=checkpoint["classification"].get("metadata", {}),
                          writerMetadata=written.get("metadata", {}), fallbackUsed=written.get("fallbackUsed", False),
                          policyViolations=[event["code"] for event in candidate.get("policyViolations", []) if event["turnId"] == request.turn_id],
                          processingDurationSeconds=time.monotonic() - started, createdAtUtc=_now())
            def commit(current):
                # Copy only gameplay mutations; never restore deleted diagnostics.
                keys = ("phase", "assessmentStatus", "clarificationCount", "clarificationIssue", "clarificationIssues", "rubricComponents",
                        "objectiveEvidence", "completedObjectives", "unresolvedPromises", "policyViolations",
                        "evaluableTurnCount", "supplementalTurnGranted", "endingReason", "status", "challengeShown",
                        "challengeAnswered", "investigationEvidence")
                for key in keys:
                    if key in candidate:
                        current[key] = copy.deepcopy(candidate[key])
                if decision["turnQuality"] != "uncertain":
                    current["acceptedTurnCount"] += 1
                if decision["turnQuality"] in ("good", "bad"):
                    count = "goodResponseCount" if decision["turnQuality"] == "good" else "badResponseCount"
                    current[count] += 1
                current["turnIds"].append(request.turn_id)
                current["turns"].append(copy.deepcopy(result))
                current.setdefault("evidenceLedger", []).append({"turnId": request.turn_id, "transcriptRef": request.turn_id,
                    "objective": state["phase"], "labels": labels, "contextVersion": VERSIONS["scenarioVersion"],
                    "factsKnownBefore": state.get("investigationEvidence", []),
                    "unresolvedPromiseTypesBefore": [name for name, code in VIOLATIONS.items()
                                                     if code in state.get("unresolvedPromises", [])],
                    "challengeShownBefore": state.get("challengeShown", False),
                    "turnQuality": decision["turnQuality"]})
                current.get("turnCheckpoints", {}).pop(request.turn_id, None)
            _, result = await store.finalize_turn(session_id, request.turn_id, result, commit)
            return result
        except Exception as exc:
            safe = {"pipeline_version_unsupported", "classifier_timeout", "classifier_invalid", "transcription_empty", "transcription_timeout",
                    "transcription_unavailable", "transcription_failed", "audio_read_failed", "microphone_no_signal", "diagnostics_deleted"}
            adapter_codes = {"missing_openrouter_key", "openrouter_timeout", "openrouter_network_error",
                             "invalid_openrouter_response", "invalid_decisions_answers"}
            code = str(exc) if str(exc) in safe else getattr(exc, "code", "pipeline_unavailable")
            if code not in safe | adapter_codes:
                code = "openrouter_http_error" if isinstance(code, str) and code.startswith("openrouter_http_") else "pipeline_unavailable"
            result.update(status="failed", accepted=False, error={"code": code}, processingDurationSeconds=time.monotonic() - started)
            if code == "diagnostics_deleted":
                raise RuntimeError(code) from exc
            _, result = await store.finalize_turn(session_id, request.turn_id, result)
            return result


async def complete(session_id, request, *, store):
    from app.sales_returning_customer import _LOCKS, _now
    async with _LOCKS.setdefault(session_id, asyncio.Lock()):
        state = await store.get(session_id)
        if state is None:
            raise KeyError("session_not_found")
        if state.get("completionStatus") == "completed":
            return state
        if not supports_versions(state):
            raise RuntimeError("pipeline_version_unsupported")
        if state.get("diagnosticsDeleted"):
            raise RuntimeError("diagnostics_deleted")
        if state.get("assessmentStatus") == "needs-review" or state.get("clarificationCount") or state.get("pendingTurns") or state.get("turnCheckpoints"):
            return state
        if not state.get("acceptedTurnCount") and state.get("silenceCount", 0) < 2:
            return state
        if state.get("status") != "awaitingCompletion":
            state["endingReason"] = "stopped_early"
        state.update(outcome(state), status="finished", assessmentStatus="completed", completionStatus="completed",
                     completionId=request.completion_id, completionReason=state.get("endingReason") or request.reason,
                     completedAtUtc=_now())
        rating = state["customerRating"]
        terminal_turns = [turn for turn in state.get("turns", []) if turn.get("conversationComplete") and turn.get("endingReason") == state.get("endingReason")]
        state["finalCustomerText"] = (terminal_turns[-1]["customerText"] if terminal_turns else
                                      state.get("finalCustomerText") or TERMINAL_REPLIES[rating])
        return await store.save(state)
