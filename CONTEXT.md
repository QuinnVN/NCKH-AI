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

**Customer speech**:
Synthesized Vietnamese audio through which Lan delivers a customer reply during the returning-customer conversation. The corresponding customer text remains the authoritative reply.
_Avoid_: TTS response, voice output, audio reply
