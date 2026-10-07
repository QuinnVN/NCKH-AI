"""Backend-authored lines for Lan and the single fallback selector.

Every candidate goes through the same guard as Qwen's text. The writer adapter
and the turn pipeline both call select_customer_line(), so a fallback can never
bypass the repetition, fact or policy checks.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.sales_openrouter import FACTS, customer_text_rejections

MANDATORY_CHALLENGE = ("Nhưng lần trước em cũng tư vấn đôi này phù hợp với chị. "
                       "Làm sao chị biết lần này sẽ không gặp vấn đề tương tự?")

# concern -> missing part -> hint level -> lines. Each line is one sentence so
# it can follow a fact answer within the two-sentence limit.
CONCERN_LINES: dict[int, dict[str, tuple[tuple[str, ...], ...]]] = {
    1: {
        "acknowledgment": (
            ("Chị đang bực lắm, mang đôi giày này mà chân cứ đau mãi.",
             "Chị quay lại đây vì thấy rất khó chịu với đôi giày này.",
             "Chị bực thật sự, tiền bỏ ra rồi mà chân thì đau."),
            ("Em nói nhiều mà chưa hề để ý chị đang khó chịu thế nào.",
             "Chị thấy em chưa hiểu chị đã mệt mỏi thế nào vì đôi giày này.",
             "Nãy giờ em chưa nói được câu nào cho thấy em hiểu nỗi bực của chị."),
            ("Chị cần em hiểu là chị đang rất bực và khổ sở vì đôi giày này, trước khi nói gì khác.",
             "Điều chị muốn trước tiên là em thông cảm với chuyện chị bị đau chân.",
             "Chị chỉ mong em hiểu cho cảm giác của chị lúc này đã."),
        ),
        "problem_question": (
            ("Chân chị vẫn đau khi mang đôi này, chị không biết phải làm sao.",
             "Chị vẫn thấy khó chịu với đôi giày này lắm.",
             "Đôi giày này cứ làm chị đau chân, chị mệt mỏi lắm."),
            ("Em còn chưa hỏi chị bị làm sao mà đã tính cách xử lý rồi.",
             "Chị kể là chị đau mà em cũng không hỏi han gì thêm.",
             "Em chưa tìm hiểu chuyện của chị thế nào mà đã vội kết luận."),
            ("Chị muốn em hỏi chị xem đôi giày này làm chị khó chịu thế nào đã.",
             "Em hỏi chị xem chuyện gì đã xảy ra với đôi giày này đi, rồi hẵng tính.",
             "Chị cần em tìm hiểu vấn đề của chị trước khi đưa ra cách xử lý."),
        ),
    },
    2: {
        "walking": (
            ("Chị vẫn chưa hiểu sao đôi giày này lại làm chị khổ như vậy.",
             "Chị muốn biết vì sao mang đôi này lại khó chịu đến thế.",
             "Đôi giày này có vấn đề gì mà chị mang vào lại khổ thế."),
            ("Em vẫn chưa biết chị dùng đôi giày này vào việc gì mà đã muốn đổi.",
             "Em chưa hỏi chị mang giày này thế nào trong sinh hoạt thì sao biết được vấn đề.",
             "Em chưa hỏi chị thường dùng đôi giày này vào lúc nào, nhiều hay ít."),
            ("Chị cần em hỏi xem hằng ngày chị dùng đôi giày này ra sao rồi hẵng kết luận.",
             "Em phải hỏi chị mang giày này làm gì mỗi ngày thì mới hiểu được chứ.",
             "Chị muốn em tìm hiểu xem chị dùng giày nhiều đến đâu trước đã."),
        ),
        "fit": (
            ("Chị vẫn chưa rõ vì sao đôi này lại không hợp với chị.",
             "Chị chưa hiểu đôi giày này có vấn đề gì nữa.",
             "Chị vẫn thắc mắc không biết vấn đề ở chỗ nào."),
            ("Em chưa hỏi chị mang đôi giày này vào thấy thế nào mà đã kết luận rồi.",
             "Em còn chưa hỏi chị về cỡ giày hay cảm giác lúc mang.",
             "Em chưa hỏi chị từng mang loại giày nào trước đây cả."),
            ("Chị cần em hỏi xem đôi này mang có chật, có rộng hay có gì khác không.",
             "Chị muốn em hỏi về cỡ giày và loại giày chị quen mang đã.",
             "Em hỏi chị xem đôi giày cũ của chị thế nào thì mới so sánh được."),
        ),
        "cause": (
            ("Chị kể rồi, vậy theo em vì sao đôi này lại làm chị đau?",
             "Vậy rốt cuộc đôi giày này của chị có vấn đề ở đâu hả em?",
             "Nghe chị kể vậy rồi, em thấy vấn đề là gì?"),
            ("Em hỏi đủ chuyện rồi mà vẫn chưa nói cho chị biết vì sao đôi này không hợp.",
             "Chị đã kể hết rồi, em vẫn chưa giải thích được nguyên nhân cho chị.",
             "Em biết chị dùng giày thế nào rồi, sao vẫn chưa nói lý do chị bị đau?"),
            ("Chị cần em nói rõ vì sao đôi này không hợp với cách chị dùng giày hằng ngày.",
             "Em giải thích cho chị nguyên nhân đi, đôi này không hợp với nhu cầu của chị ở chỗ nào.",
             "Chị muốn nghe em nói rõ lý do đôi giày này không hợp với chị."),
        ),
    },
    3: {
        "offer": (
            ("Giờ chị hiểu rồi, vậy tiệm định xử lý chuyện này thế nào?",
             "Biết nguyên nhân rồi thì em tính giải quyết cho chị ra sao?",
             "Vậy bây giờ chị phải làm gì với đôi giày này đây em?"),
            ("Em nói nguyên nhân rồi mà chưa đưa ra cách nào cho chị cả.",
             "Em vẫn chưa đề xuất cho chị cách xử lý nào.",
             "Em hiểu vấn đề rồi thì phải có phương án cho chị chứ."),
            ("Chị muốn biết tiệm có cho chị đổi sang đôi khác không.",
             "Chị cần em nói rõ chị được đổi giày hay không.",
             "Em nói rõ cho chị biết tiệm có xử lý đổi giày cho chị không."),
        ),
        "conditions": (
            ("Đổi thì chị cần biết điều kiện thế nào đã.",
             "Muốn đổi thì chị phải làm sao, có điều kiện gì không em?",
             "Chị muốn biết đổi giày thì cần những gì."),
            ("Em nói đổi mà chưa nói với chị điều kiện đổi ra sao.",
             "Chị chưa biết được đổi theo điều kiện nào, em nói rõ giúp chị.",
             "Em chưa nói chị được đổi trong trường hợp nào."),
            ("Chị cần em nói rõ chính sách đổi của tiệm, thời hạn và điều kiện.",
             "Em nói cho chị biết tiệm cho đổi trong bao lâu và cần điều kiện gì.",
             "Chị muốn nghe rõ điều kiện đổi giày của tiệm trước khi đồng ý."),
        ),
        "lightweight": (
            ("Đổi thì được, nhưng đổi sang đôi nào cho hợp với chị đây?",
             "Chị sợ lại chọn nhầm một đôi không hợp nữa.",
             "Vậy em định giới thiệu cho chị đôi nào?"),
            ("Em chưa nói đôi mới khác đôi này ở điểm nào cho hợp với chị.",
             "Chị chưa thấy em đề xuất đôi nào hợp với cách chị dùng giày.",
             "Em nói đổi mà chưa nói chị nên đổi sang loại giày thế nào."),
            ("Chị cần một đôi nhẹ hơn, hợp với việc phải đi nhiều mỗi ngày.",
             "Em giới thiệu cho chị loại giày nhẹ, hợp để chị mang cả ngày được không?",
             "Chị muốn em chọn đôi nào nhẹ chân hơn đôi này cho chị."),
        ),
        "trial": (
            ("Chị vẫn lo đôi mới mang vào lại khó chịu như đôi này.",
             "Làm sao chị biết đôi mới hợp với chị hơn?",
             "Chị sợ mang về rồi lại phải quay lại lần nữa."),
            ("Em chưa cho chị cách nào để biết đôi mới có hợp hay không.",
             "Em giới thiệu đôi khác nhưng chị chưa có cách nào kiểm tra trước.",
             "Chị chưa được thử gì mà em đã bảo đôi mới hợp rồi."),
            ("Chị cần được mang thử và bước vài bước trong tiệm trước khi quyết định.",
             "Em cho chị thử đôi mới ngay ở đây xem có thoải mái không.",
             "Chị muốn kiểm tra xem đôi mới mang có êm chân không đã."),
        ),
    },
    4: {
        "responsibility": (
            ("Chị vẫn chưa yên tâm vì lần trước đã được tư vấn sai rồi.",
             "Lần trước em cũng nói chắc như vậy, nên chị còn ngại lắm.",
             "Chị vẫn nhớ lần trước được tư vấn thế nào nên khó tin ngay."),
            ("Em chưa nhận là lần trước tư vấn cho chị có thiếu sót.",
             "Em nói về đôi mới mà không nhắc gì đến lỗi tư vấn cho chị lần trước.",
             "Em vẫn chưa nhận trách nhiệm về lần tư vấn trước cho chị."),
            ("Chị cần em nhận là lần trước tiệm đã tư vấn không kỹ cho chị.",
             "Chị muốn nghe em thừa nhận lỗi tư vấn lần trước đã.",
             "Em phải nhận phần trách nhiệm của tiệm về lần trước thì chị mới tin được."),
        ),
        "explanation": (
            ("Chị vẫn chưa thấy đôi mới khác gì để chị yên tâm.",
             "Đôi mới này thì hơn đôi cũ của chị ở điểm nào hả em?",
             "Chị vẫn chưa chắc đôi mới có thực sự hợp hơn không."),
            ("Em chưa giải thích vì sao đôi mới hợp với cách chị dùng giày.",
             "Em giới thiệu đôi mới nhưng chưa nói vì sao nó hợp với chị hơn.",
             "Em vẫn chưa nói lý do đôi mới hợp với sinh hoạt của chị."),
            ("Chị cần em nói rõ đôi mới hợp với nhu cầu hằng ngày của chị ở điểm nào.",
             "Em giải thích giúp chị vì sao đôi mới phù hợp hơn với việc chị phải đi nhiều.",
             "Chị muốn hiểu rõ đôi mới giúp chị thoải mái hơn ra sao trong sinh hoạt hằng ngày."),
        ),
        "trial": (
            ("Chị vẫn sợ đôi mới rồi cũng như đôi cũ.",
             "Lần này chị không muốn mang về rồi mới biết là không hợp.",
             "Chị chưa chắc đôi mới sẽ ổn với chị."),
            ("Em chưa cho chị cách nào để kiểm chứng đôi mới trước khi mang về.",
             "Em nói hay nhưng chị chưa được thử đôi mới lần nào.",
             "Chị chưa có cách nào biết trước đôi mới có hợp hay không."),
            ("Chị cần được thử đôi mới ngay tại tiệm trước khi đồng ý.",
             "Em để chị mang thử đôi mới bước vài vòng trong tiệm đã.",
             "Chị muốn tự kiểm tra cảm giác khi mang đôi mới trước đã."),
        ),
    },
}

REPEATED_LINES = ("Chị kể chuyện đó với em rồi mà.",
                  "Cái đó chị đã nói với em rồi.",
                  "Chị trả lời em câu đó rồi.")

# Actions whose wrong meaning would change the outcome keep keyword checks, so
# each pool has four lines: a three-reply window can block at most three.
ACTION_LINES: dict[str, tuple[str, ...]] = {
    "refusal_challenge": (
        "Em từ chối đổi thì định xử lý vấn đề của chị thế nào?",
        "Em không đổi cho chị thì chị phải làm sao với đôi giày đau chân này?",
        "Nếu em từ chối như vậy thì chuyện của chị ai giải quyết?",
        "Em rút lại việc đổi giày thì em định giúp chị cách nào?"),
    "warning": (
        "Chị thấy cách em nói thiếu tôn trọng, em có thể trao đổi bình tĩnh hơn không?",
        "Em nói vậy là xúc phạm chị đấy, em nói chuyện lịch sự giúp chị.",
        "Chị không chấp nhận cách nói thiếu tôn trọng như vậy đâu em.",
        "Em bình tĩnh nói chuyện đàng hoàng với chị được không?"),
    "pressure_challenge": (
        "Em có cách xử lý nào khác thay vì bảo chị tiếp tục mang đôi này không?",
        "Chân chị đang đau mà em vẫn bảo chị mang thêm, vậy em giải quyết thế nào?",
        "Sao em cứ bảo chị tiếp tục mang mà không nói rõ vì sao?",
        "Chị phải mang thêm bao lâu nữa mới hết đau, em có cách nào khác không?"),
    "promise_challenge": (
        "Chị không thể nhận lời hứa như vậy, em có thể nói rõ cách kiểm tra đôi giày phù hợp hơn không?",
        "Lời hứa đó chị không yên tâm đâu, em kiểm tra cho chị bằng cách nào?",
        "Chị không chấp nhận lời hứa suông, em chứng minh đôi nào phù hợp với chị thế nào?",
        "Em đừng đưa lời hứa mà chị không thể tin, em kiểm chứng giúp chị thế nào?"),
    "trust_challenge": (
        MANDATORY_CHALLENGE,
        "Lần trước em cũng tư vấn chắc chắn như vậy, làm sao chị biết lần này đôi mới sẽ không lại làm chị đau?",
        "Trước đây em bảo đôi này hợp với chị, giờ làm sao chị biết đôi mới sẽ không gặp vấn đề tương tự?",
        "Lần trước em nói đôi này hợp, sao chị biết đôi mới không lặp lại vấn đề cũ?"),
}

CLARIFY_LINES: dict[str, tuple[str, ...]] = {
    "refusal": (
        "Em đang từ chối đổi hay sẽ tiếp tục kiểm tra và xử lý cho chị?",
        "Ý em là không đổi cho chị, hay em vẫn xử lý tiếp?",
        "Có phải em từ chối giải quyết chuyện của chị không?",
        "Em định rút lại việc đổi hay vẫn giúp chị tiếp?"),
    "manager": (
        "Chị muốn làm rõ: em đang đề nghị chị gặp quản lý hay em sẽ tiếp tục xử lý?",
        "Ý em là chị phải gặp quản lý, hay em tự giải quyết cho chị?",
        "Em định chuyển chị sang quản lý thật à, hay em vẫn lo việc này?"),
    "acknowledgment": (
        "Chị vẫn đang bực vì chuyện đôi giày này, em hiểu điều gì đang khiến chị bực?",
        "Em nói vậy là em hiểu chị đang khó chịu thế nào chưa?",
        "Chị muốn biết em có thật sự hiểu chị đang bực vì sao không?"),
    "general": (
        "Chị chưa rõ ý em, em giải thích cụ thể điều em định nói được không?",
        "Ý em là sao, em nói cụ thể hơn cho chị hiểu được không?",
        "Chị chưa hiểu em muốn nói gì, em nói rõ hơn giúp chị nhé?"),
}

ENDING_LINES: dict[str, tuple[str, ...]] = {
    "ending_exchange_accepted": (
        "Được, chị đồng ý đổi theo chính sách đó, mình xử lý như vậy nhé.",
        "Thôi được, chị chấp nhận đổi theo cách em nói, em làm như vậy cho chị.",
        "Ừ, chị đồng ý đổi giày, em đổi cho chị theo đúng chính sách nhé.",
        "Vậy chị đồng ý đổi theo chính sách của tiệm, em xử lý như vậy giúp chị."),
    "ending_restored": (
        "Giờ chị thấy yên tâm hơn, chị cảm ơn em, chị chào em nhé.",
        "Nghe em nói vậy chị yên tâm rồi, chị chào em nhé.",
        "Chị hài lòng với cách em xử lý, cảm ơn em, chị về đây.",
        "Em giải thích rõ ràng nên chị an tâm rồi, chị về thử đôi mới nhé."),
    "ending_partially_restored": (
        "Chị hiểu em đã cố gắng, nhưng chị vẫn chưa thực sự hài lòng, chị về trước nhé.",
        "Hôm nay chị dừng ở đây, chị vẫn cần suy nghĩ thêm.",
        "Chị sẽ cân nhắc thêm, giờ chị về trước đã.",
        "Chị vẫn còn lo một chút, để chị về suy nghĩ thêm nhé."),
    "ending_lost": (
        "Chị thấy hôm nay mình chưa giải quyết được gì, chị về trước đây.",
        "Nói mãi mà chuyện của chị vẫn chưa giải quyết được, chị dừng ở đây thôi.",
        "Chị không đồng ý với cách xử lý này, chị về trước nhé.",
        "Chị không chấp nhận cách giải quyết như vậy, chị về trước đây."),
    "ending_lost_hostile": (
        "Chị không nói chuyện với em nữa, gọi quản lý ra đây cho chị.",
        "Chị sẽ phản ánh với quản lý, chị dừng trao đổi ở đây.",
        "Cách em xử lý chị không chấp nhận được, chị về và sẽ báo quản lý.",
        "Chị dừng ở đây, chị sẽ gặp quản lý để phản ánh."),
    "ending_review": (
        "Chị dừng trao đổi ở đây, chị sẽ cân nhắc những điều em đã nói.",
        "Chị xin phép dừng ở đây, chị sẽ suy nghĩ thêm.",
        "Hôm nay chị về trước, để chị nghĩ thêm về chuyện này.",
        "Thôi chị dừng ở đây, chị cần thời gian để nghĩ lại."),
}

# In-role lines with no question and no shared clause of six or more words.
GENERIC_LINES = ("Chị vẫn chưa thấy chuyện của chị được giải quyết.",
                 "Chị vẫn còn lo về đôi giày này lắm.",
                 "Em nói tiếp đi, chị đang nghe đây.",
                 "Chị mong em xử lý chuyện này cho đàng hoàng.",
                 "Chị vẫn đang chờ em giải quyết chuyện đôi giày này.")
LAST_RESORT = "Chị vẫn đang chờ em giải quyết chuyện đôi giày này."


def fact_sentence(fact_ids: Any) -> str:
    """One sentence answering up to two facts, so a concern can follow it."""
    texts = [FACTS[fact].strip() for fact in (fact_ids or []) if fact in FACTS]
    if not texts:
        return ""
    sentence = texts[0].rstrip(".")
    for extra in texts[1:]:
        sentence += ", còn " + extra[0].lower() + extra[1:].rstrip(".")
    return sentence + "."


def concern_lines(concern: Any, part: Any, hint: Any) -> tuple[str, ...]:
    parts = CONCERN_LINES.get(concern if isinstance(concern, int) else -1, {})
    levels = parts.get(part, ())
    if not levels:
        return ()
    level = hint if isinstance(hint, int) and 0 <= hint < len(levels) else 0
    return levels[level]


def _join(prefix: str, core: str) -> str:
    return (prefix + " " + core).strip() if prefix else core


def candidates(plan: Mapping) -> list[str]:
    intent = plan.get("intent")
    prefix = fact_sentence(plan.get("requiredFactIds"))
    content = plan.get("requiredContent") or []
    content = [content] if isinstance(content, str) else list(content)
    concern = concern_lines(plan.get("concern"), plan.get("missingPart"), plan.get("hintLevel"))
    if intent == "ending":
        key = next((value for value in content if isinstance(value, str) and value.startswith("ending_")), "ending_review")
        if key == "ending_lost" and plan.get("hostile"):
            key = "ending_lost_hostile"
        return [_join(prefix, line) for line in ENDING_LINES.get(key, ENDING_LINES["ending_review"])]
    if intent in ACTION_LINES:
        return [_join(prefix, line) for line in ACTION_LINES[intent]]
    if intent == "clarify_meaning":
        topic = plan.get("clarifyTopic") if plan.get("clarifyTopic") in CLARIFY_LINES else "general"
        lines = [_join(prefix, line) for line in CLARIFY_LINES[topic]]
        return lines if topic == "refusal" else lines + [_join(prefix, line) for line in GENERIC_LINES]
    if intent == "repeated_question":
        lines = [_join(prefix, repeated + " " + line) for repeated in REPEATED_LINES for line in concern]
        return lines + [_join(prefix, line) for line in (*REPEATED_LINES, *GENERIC_LINES)]
    lines = [_join(prefix, line) for line in concern]
    if prefix:
        lines.append(prefix)
    return lines + [_join(prefix, line) for line in GENERIC_LINES]


def select_customer_line(plan: Mapping, transcript: str = "") -> dict:
    """First candidate that passes every writer guard, with rejection diagnostics."""
    rejected = []
    options = candidates(plan)
    for index, text in enumerate(options):
        codes = customer_text_rejections(text, plan, transcript)
        if not codes:
            return {"text": text, "candidateIndex": index, "rejectedCandidates": rejected, "exhausted": False}
        rejected.append({"index": index, "rejectionCodes": codes})
    # Unreachable for well-formed plans: the pools are sized against the
    # three-reply repetition window. Keep the session alive and flag it.
    return {"text": options[0] if options else LAST_RESORT, "candidateIndex": None,
            "rejectedCandidates": rejected, "exhausted": True}


def choose_customer_line(plan: Mapping, transcript: str = "") -> str:
    return select_customer_line(plan, transcript)["text"]
