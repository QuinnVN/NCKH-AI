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
from app.sales_concerns import score_session
from app.sales_customer_lines import select_customer_line
from app.sales_rubric import VERSIONS, VIOLATIONS, evaluate, normalized_labels, outcome, remaining, supports_versions
from app.sales_openrouter import customer_text_rejections

# A model that timed out produced no assessment. The turn is retried from its
# checkpoint instead of being answered with a clarification or fallback line.
TIMEOUT_CODES = {"classifier_timeout", "arbitrator_timeout", "writer_timeout", "openrouter_timeout"}

TERMINAL_REPLIES = {
    "good": "Giờ chị thấy yên tâm hơn. Chị hài lòng, cảm ơn em, chị chào em nhé.",
    "considering": "Chị hiểu em đã làm những gì có thể. Hôm nay chị dừng ở đây, nhưng vẫn chưa thực sự hài lòng.",
    "bad": "Chị không nói chuyện với em nữa. Gọi quản lý ra đây cho chị.",
}


def public_fields(state: Mapping) -> dict:
    return {**{name: state.get(name) for name in VERSIONS},
            "assessmentStatus": state.get("assessmentStatus", "pending"),
            "maxTurns": state.get("maxTurns", 8), "remainingTurns": remaining(state),
            "supplementalTurnGranted": state.get("supplementalTurnGranted", False),
            "evaluableTurnCount": state.get("evaluableTurnCount", 0),
            "dialogueTurnCount": state.get("dialogueTurnCount", state.get("evaluableTurnCount", 0)),
            "resolutionAccepted": state.get("resolutionAccepted", False),
            "endingReason": state.get("endingReason")}


def plan_reply(before: Mapping, state: dict, result: dict, labels: Mapping) -> dict:
    """Plan Lan's reply: ending, reaction to conduct, fact answers, open concern."""
    yes = lambda key: labels.get(key, {}).get("status") == "true"
    turn = (state.get("customerState") or {}).get("lastTurn", {})
    known = sorted(before.get("investigationEvidence", []))
    answers = list(result.get("disclosedFactIds", []))
    recent = [item.get("customerText", "") for item in before.get("turns", [])[-3:] if item.get("customerText")]
    hostile = bool(before.get("badResponseCount") or result["turnQuality"] == "bad" or state.get("policyViolations"))
    intent, topic, content = "raise_concern", None, []
    if result.get("conversationComplete"):
        intent = "ending"
        if state.get("assessmentReviewItems") or state.get("assessmentStatus") == "needs-review":
            content = ["ending_review"]
        elif result.get("endingReason") == "exchange_accepted":
            content = ["ending_exchange_accepted"]
        else:
            content = ["ending_" + outcome(state)["trustState"]]
    elif yes("refusesRemedy"):
        intent, content = "refusal_challenge", ["refusal_challenge"]
    elif yes("abuse") or yes("disrespect"):
        # A separate uncertain label must not hide a clearly recognized insult.
        intent, content = "warning", ["respect_warning"]
    elif yes("beggingWithoutExplanation"):
        intent, content = "pressure_challenge", ["pressure_challenge"]
    elif state.get("unresolvedPromises"):
        intent, content = "promise_challenge", ["promise_challenge"]
    elif result.get("assessmentUncertain") or result["turnQuality"] == "uncertain":
        # Classification uncertainty follows successful STT; it is not evidence
        # that the microphone failed or that Lan could not hear the player.
        intent = "clarify_meaning"
        issues = set((result.get("clarificationReason") or "").split(","))
        topic = ("refusal" if "refusesRemedy" in issues else "manager" if "managerEscalation" in issues else
                 "acknowledgment" if "acknowledgment" in issues else "general")
        content = ["clarify_refusal"] if topic == "refusal" else []
    elif turn.get("challengeShownNow"):
        intent, content = "trust_challenge", ["trust_challenge"]
    elif turn.get("repeatedQuestion"):
        intent = "repeated_question"
    hint = turn.get("hintLevel", 0)
    disposition = ("irritated" if result["turnQuality"] == "bad" else
                   "receptive" if turn.get("resolvedConcerns") or answers else
                   "irritated" if hint >= 2 else "guarded")
    return dict(intent=intent, clarifyTopic=topic, concern=turn.get("statedConcern"),
                missingPart=turn.get("missingPart"), hintLevel=hint,
                emotion="firm" if disposition == "irritated" else "warmer" if disposition == "receptive" else "cautious",
                customerDisposition=disposition, fallbackText="", allowedNewFacts=answers, knownFacts=known,
                requiredPhrases=[], requiredFactIds=answers, requiredContent=content,
                avoidReplyTexts=recent, ending=result.get("endingReason"), hostile=hostile,
                unresolvedPromises=state.get("unresolvedPromises", []))



async def process_turn(session_id, request, *, store, transcriber, classifier=None, writer=None, arbitrator=None):
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
        result = dict(pending, **public_fields(state), playerResponseRating=None)
        retry_error = None
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
                except Exception as exc:
                    if getattr(exc, "code", None) == "openrouter_timeout":
                        raise RuntimeError("classifier_timeout") from exc
                    raise
                # A response without a label object is an infrastructure failure.
                if not isinstance(classification, Mapping) or not isinstance(classification.get("labels"), Mapping):
                    raise RuntimeError("classifier_invalid")
                checkpoint["classification"] = dict(classification)
                checkpoint["classifiedAtUtc"] = _now()
                await save_checkpoint()
            if "resolvedClassification" not in checkpoint:
                from app.sales_arbitration import LunaSalesArbitrator, select_arbitration_labels, merge_adjudication
                selected = select_arbitration_labels(state, normalized_labels(checkpoint["classification"])) if state.get("lunaArbitrationEnabled") else []
                adjudication = {"labels": {}, "metadata": {"selectedLabels": selected, "skipped": not bool(selected)}}
                if selected:
                    arbitration_started = time.monotonic()
                    promise_labels = {code: name for name, code in VIOLATIONS.items()}
                    context = {"objective": state["phase"], "knownFacts": state.get("investigationEvidence", []),
                               "unresolvedPromiseTypes": [promise_labels[code] for code in state.get("unresolvedPromises", []) if code in promise_labels],
                               "history": state.get("turns", [])[-3:]}
                    try:
                        if arbitrator is None:
                            arbitrator = LunaSalesArbitrator(settings=replace(get_settings(), sales_luna_timeout_seconds=state["lunaDeadlineSeconds"]))
                        adjudication = await asyncio.wait_for(arbitrator.adjudicate(transcript, context, selected), state["lunaDeadlineSeconds"] + .25)
                    except Exception as exc:
                        adjudication = {"labels": {}, "metadata": {"errorCode": "arbitrator_timeout" if isinstance(exc, TimeoutError) else "arbitrator_failed",
                            "durationSeconds": time.monotonic() - arbitration_started}}
                    if (adjudication.get("metadata") or {}).get("errorCode") in TIMEOUT_CODES:
                        # Do not checkpoint a timed-out arbitration: the retry
                        # resumes here with STT and Jev already cached.
                        retry_error = dict(adjudication.get("metadata") or {}, stage="arbitration")
                        raise RuntimeError("arbitrator_timeout")
                checkpoint["arbitration"] = adjudication
                checkpoint["resolvedClassification"] = merge_adjudication(checkpoint["classification"], adjudication)
                checkpoint["arbitratedAtUtc"] = _now()
                await save_checkpoint()
            labels = normalized_labels(checkpoint["resolvedClassification"])
            if "candidate" not in checkpoint:
                candidate, decision = evaluate(state, labels, request.turn_id)
                plan = plan_reply(state, candidate, decision, labels)
                # One guarded selector supplies every fallback; nothing unchecked
                # can be committed as Lan's reply.
                selection = select_customer_line(plan, transcript)
                plan["fallbackText"] = selection["text"]
                checkpoint.update(candidate=candidate, decision=decision, plan=plan, plannedAtUtc=_now(),
                                  fallbackSelection={"candidateIndex": selection["candidateIndex"],
                                                     "exhausted": selection["exhausted"],
                                                     "rejectedCandidates": selection["rejectedCandidates"]})
                await save_checkpoint()
            candidate, decision, plan = checkpoint["candidate"], checkpoint["decision"], checkpoint["plan"]
            if "written" not in checkpoint:
                written = {"customerText": plan["fallbackText"], "metadata": {}, "fallbackUsed": True}
                writer_started = time.monotonic()
                try:
                    if writer is None:
                        from app.sales_openrouter import QwenSalesWriter
                        writer = QwenSalesWriter(settings=replace(get_settings(), sales_writer_timeout_seconds=state["writerDeadlineSeconds"]))
                    # The adapter owns the request deadline. Let it package timeout
                    # diagnostics before the watchdog cancels a stuck writer.
                    written = await asyncio.wait_for(writer.write(plan, transcript, state.get("turns", [])[-3:]), state["writerDeadlineSeconds"] + .25)
                    # Production adapter owns validation; injected writers also pass it.
                    rejected = customer_text_rejections(written.get("customerText", ""), plan, transcript)
                    if rejected:
                        metadata = dict(written.get("metadata") or {})
                        metadata.update(errorCode="writer_guard_rejected", rejectionCodes=rejected)
                        written = {"customerText": plan["fallbackText"], "metadata": metadata, "fallbackUsed": True}
                except Exception as exc:
                    written = {"customerText": plan["fallbackText"], "metadata": {
                        "errorCode": "writer_timeout" if isinstance(exc, TimeoutError) else "writer_failed",
                        "durationSeconds": time.monotonic() - writer_started}, "fallbackUsed": True}
                if (written.get("metadata") or {}).get("errorCode") in TIMEOUT_CODES:
                    # Retry the writer instead of committing a fallback line.
                    retry_error = dict(written.get("metadata") or {}, stage="writer")
                    raise RuntimeError("writer_timeout")
                if written.get("fallbackUsed"):
                    written["metadata"] = dict(written.get("metadata") or {}, fallbackSelection=checkpoint.get("fallbackSelection"))
                checkpoint.update(written=written, writtenAtUtc=_now())
                await save_checkpoint()
            written = checkpoint["written"]
            rating = decision["turnQuality"] if any(label.get("noul") is not None or label.get("source") == "luna"
                                                    for label in labels.values()) else None
            arbitration_error = (checkpoint.get("arbitration") or {}).get("metadata", {}).get("errorCode")
            assessment_error = None
            if arbitration_error:
                metadata = checkpoint["arbitration"]["metadata"]
                assessment_error = {"stage": "arbitration", "code": arbitration_error,
                                    "model": metadata.get("requestedModel"),
                                    "labels": metadata.get("selectedLabels", []),
                                    "durationSeconds": metadata.get("durationSeconds")}
                if rating == "uncertain":
                    # A failed model call is not evidence about the player.
                    rating = None
            result.update(decision, **public_fields(candidate), status="accepted", accepted=True,
                          transcript=transcript, customerText=written["customerText"],
                          playerResponseRating=rating, assessmentError=assessment_error,
                          activeObjective=candidate["phase"], objectiveActiveDuringTurn=state["phase"],
                          turnAssessment=labels, classifierMetadata=checkpoint["resolvedClassification"].get("metadata", {}),
                          writerMetadata=written.get("metadata", {}), fallbackUsed=written.get("fallbackUsed", False),
                          policyViolations=[event["code"] for event in candidate.get("policyViolations", []) if event["turnId"] == request.turn_id],
                          processingDurationSeconds=time.monotonic() - started, createdAtUtc=_now())
            def commit(current):
                # Copy only gameplay mutations; never restore deleted diagnostics.
                keys = ("phase", "assessmentStatus", "clarificationCount", "clarificationIssue", "clarificationIssues", "rubricComponents",
                        "objectiveEvidence", "completedObjectives", "unresolvedPromises", "policyViolations",
                        "evaluableTurnCount", "dialogueTurnCount", "assessmentReviewItems", "supplementalTurnGranted", "endingReason", "status", "challengeShown",
                        "challengeAnswered", "investigationEvidence", "resolutionAccepted", "customerState")
                for key in keys:
                    if key in candidate:
                        current[key] = copy.deepcopy(candidate[key])
                if not decision.get("assessmentUncertain", decision["turnQuality"] == "uncertain"):
                    current["acceptedTurnCount"] += 1
                if decision["turnQuality"] in ("good", "bad"):
                    count = "goodResponseCount" if decision["turnQuality"] == "good" else "badResponseCount"
                    current[count] += 1
                current["turnIds"].append(request.turn_id)
                current["turns"].append(copy.deepcopy(result))
                # The scored entry is the one evaluate() appended; scoring later
                # replays exactly this record.
                current.setdefault("evidenceLedger", []).append(copy.deepcopy(candidate["evidenceLedger"][-1]))
                current.get("turnCheckpoints", {}).pop(request.turn_id, None)
            _, result = await store.finalize_turn(session_id, request.turn_id, result, commit)
            return result
        except Exception as exc:
            safe = {"pipeline_version_unsupported", "classifier_timeout", "arbitrator_timeout", "writer_timeout", "classifier_invalid", "transcription_empty", "transcription_timeout",
                    "transcription_unavailable", "transcription_failed", "audio_read_failed", "microphone_no_signal", "diagnostics_deleted"}
            adapter_codes = {"missing_openrouter_key", "openrouter_timeout", "openrouter_network_error",
                             "invalid_openrouter_response", "invalid_decisions_answers"}
            code = str(exc) if str(exc) in safe else getattr(exc, "code", "pipeline_unavailable")
            if code not in safe | adapter_codes:
                code = "openrouter_http_error" if isinstance(code, str) and code.startswith("openrouter_http_") else "pipeline_unavailable"
            result.update(status="failed", accepted=False, error={"code": code}, processingDurationSeconds=time.monotonic() - started)
            # Timeouts keep the checkpoint; resubmitting the same turnId resumes
            # at the stage that timed out.
            result["retryTurn"] = code in TIMEOUT_CODES
            if result["retryTurn"]:
                stage = {"classifier_timeout": "classification", "arbitrator_timeout": "arbitration",
                         "writer_timeout": "writer"}.get(code, "classification")
                result["error"].update(stage=stage, message=f"{stage} model timed out; retrying turn")
                if retry_error:
                    result["error"]["details"] = {key: retry_error.get(key) for key in
                        ("requestedModel", "errorCode", "durationSeconds", "selectedLabels", "attempts") if key in retry_error}
                checkpoint.setdefault("timeoutRetries", []).append({"stage": stage, "code": code, "atUtc": _now()})
                await save_checkpoint()
            if code == "diagnostics_deleted":
                raise RuntimeError(code) from exc
            # This audio cannot be retried meaningfully. Retain the failed turn
            # diagnostics, but release its checkpoint so a fresh recording with
            # a new ID can proceed without spending or ending the conversation.
            recapture = code in {"transcription_empty", "transcription_failed", "audio_read_failed", "microphone_no_signal"}
            result["retryRecording"] = recapture
            def release_capture(current):
                current.get("turnCheckpoints", {}).pop(request.turn_id, None)
            _, result = await store.finalize_turn(session_id, request.turn_id, result, release_capture if recapture else None)
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
        if state.get("pendingTurns") or state.get("turnCheckpoints"):
            return state
        if not state.get("dialogueTurnCount", state.get("acceptedTurnCount", 0)) and state.get("silenceCount", 0) < 2:
            return state
        if state.get("status") != "awaitingCompletion":
            state["endingReason"] = "stopped_early"
        if state.get("assessmentReviewItems"):
            from app.sales_review import resolve_review
            state = await resolve_review(state, store)
        if state.get("assessmentReviewItems") or state.get("assessmentStatus") == "needs-review" or state.get("clarificationCount"):
            # Completing dialogue and completing assessment are separate actions.
            # Later clear turns cannot resolve uncertainty about an earlier act.
            state.update(status="awaitingCompletion", assessmentStatus="needs-review",
                         completionId=request.completion_id,
                         completionReason=state.get("endingReason") or request.reason)
            state.setdefault("dialogueEndedAtUtc", _now())
            return await store.save(state)
        # The same ledger fold that rated each turn decides the final result.
        scored = score_session(state)
        state.update(scored["outcome"], rubricComponents=scored["rubricComponents"],
                     policyViolations=scored["policyViolations"], unresolvedPromises=scored["unresolvedPromises"],
                     completedObjectives=scored["resolvedConcerns"], resolutionAccepted=scored["resolutionAccepted"],
                     challengeAnswered=4 in scored["resolvedConcerns"])
        state.update(status="finished", assessmentStatus="completed", completionStatus="completed",
                     completionId=request.completion_id, completionReason=state.get("endingReason") or request.reason,
                     completedAtUtc=_now())
        rating = state["customerRating"]
        terminal_turns = [turn for turn in state.get("turns", []) if turn.get("conversationComplete") and turn.get("endingReason") == state.get("endingReason")]
        state["finalCustomerText"] = (terminal_turns[-1]["customerText"] if terminal_turns else
                                      state.get("finalCustomerText") or TERMINAL_REPLIES[rating])
        return await store.save(state)
