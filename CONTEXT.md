# Unity VR backend

This context describes the domain language shared by the Unity VR training backend.

## Speech recognition

**Speech stream**:
A single participant utterance sent live from Unity for Vietnamese transcription. The current deployment permits one active speech stream.
_Avoid_: Recording upload, conversation

**Final transcript**:
The Vietnamese text recognized from a completed speech stream. It is produced once after the participant ends the utterance, rather than revised while the participant speaks.
_Avoid_: Partial transcript, live caption

## Career guidance

## Language

**Career profile**:
The participant's scored questionnaire dimensions, including interests, abilities, and personal characteristics.
_Avoid_: Assessment result, interest list

**Profile dimension**:
A scored part of a career profile classified as an interest, ability, trait, or other relevant characteristic. Interest dimensions are the main basis for career suggestions.
_Avoid_: Career criterion, result category

**Career suggestion**:
One of at most five distinct occupations proposed for the participant to explore based on their career profile. Suggestions are ranked by match percentage and are provisional guidance, not a decision or guarantee.
_Avoid_: Required career, career verdict, job the user should follow

**Match percentage**:
An integer from 0 through 100 that expresses the estimated fit between a career profile and one career suggestion. It is not a probability or a calibrated prediction of success.
_Avoid_: Success probability, confidence score

**Simulation**:
A playable VR experience related to an occupation. Simulation availability does not limit which careers may be suggested.
_Avoid_: Career, job

**Confirmed finding**:
A relatively well-supported behaviour in the participant's questionnaire and VR results, without implying an absolute strength.
_Avoid_: Guaranteed strength, universally good behaviour

**Emerging finding**:
A potential skill suggested by one or both sources that still needs exploration or confirmation in another setting.
_Avoid_: Proven strength, hidden talent

**Development finding**:
An optional skill area supported by evidence of low VR performance or a meaningful gap with self-assessment. A profile can have no development findings.
_Avoid_: Required weakness, bad behaviour

## Sales training

**Sales simulation**:
The sales gameplay within the Unity VR training system. It includes an initial sales interaction and a returning-customer conversation.
_Avoid_: Sale game, sales game, Unity VR training system

**Returning-customer conversation**:
The second part of the sales simulation, in which the participant responds to Lan, a dissatisfied returning customer.
_Avoid_: Sale Part 2, difficult-customer game

**Dialogue state**:
The current point in the returning-customer conversation: which customer concerns remain open, the facts Lan has disclosed, the concern she is raising and its hint level.
_Avoid_: A single player utterance, the final sales score

**Customer concern**:
One of four things Lan needs settled during the returning-customer conversation: feeling heard, understanding why the shoes hurt, having a suitable remedy, and trusting the problem will not recur. Concerns can be resolved in any order; Lan raises the lowest-numbered one still open.
_Avoid_: Active objective, phase

**Hint level**:
How plainly Lan states the missing part of the customer concern she is raising, from a general complaint to saying directly what she needs, always in a customer's words.
_Avoid_: Tutorial hint, answer prompt

**Customer speech**:
Synthesized Vietnamese audio through which Lan delivers a customer reply during the returning-customer conversation. The corresponding customer text remains the authoritative reply.
_Avoid_: TTS response, voice output, audio reply

**Sales evidence**:
An independently recognized player act tied to a particular utterance and the dialogue state before it. Uncertain recognition remains unresolved and does not count as an accepted act.
_Avoid_: Model opinion, customer reply

**Evidence ledger**:
The ordered record of sales evidence for one returning-customer conversation, from which every turn rating and the final sales assessment are derived.
_Avoid_: Turn log, transcript history

**Sales rubric component**:
A skill demonstrated in its required conversation context and credited once for the returning-customer conversation, whenever in the evidence ledger that context holds.
_Avoid_: Good-turn count, trust score

**Outstanding promise**:
An unauthorized commitment the participant has made and has not withdrawn. Withdrawing it resolves the commitment but does not erase its earlier violation.
_Avoid_: Policy violation count
