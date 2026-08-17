# Architecture

## How it works

`main.py` places an outbound call through Twilio with the TwiML inlined into the API
request, so there is no inbound webhook route anywhere in the project. That TwiML contains
a single `<Connect><Stream>` verb pointing at `wss://<ngrok-host>/media-stream`, plus a
`<Parameter>` naming the scenario. When the assessment line answers, Twilio opens a
WebSocket to `server.py` and starts pushing the clinic agent's audio as base64 mu-law
frames. `server.py` opens a second WebSocket to the OpenAI Realtime API, configures it with
a patient persona built from `scenarios.py`, and then runs two coroutines that pump audio
in each direction for the life of the call. The model hears the receptionist and answers in
speech directly; nothing in between converts text to audio or back. Transcripts of both
sides fall out of the Realtime API as a side effect — the model's own output transcript for
the patient, and Whisper on the input buffer for the receptionist — and get written to
`transcripts/` stamped `[mm:ss]` from the start of the call. Twilio records the call
separately and `main.py` downloads the MP3 once the call reaches a terminal state.

## Why this shape

**Speech-to-speech instead of STT → LLM → TTS.** The obvious alternative is a pipeline:
Deepgram or Whisper for recognition, a text model for reasoning, ElevenLabs for speech. It
gives more control over each stage and it is easier to swap one component. It also adds a
serialization point at every boundary — nothing can start until the previous stage has
finished — and it throws away all the timing and prosody information that tells you *when a
person has stopped talking*. On a phone call that information is the difference between a
conversation and two answering machines taking turns. Since the challenge grades voice
quality before it reads any code, latency and turn-taking were worth more than modularity.
The cost is real: swapping the recognizer now means changing providers wholesale, and there
is no text checkpoint to inspect between hearing and speaking.

**Mu-law all the way through.** Twilio streams `audio/x-mulaw` at 8kHz, and the Realtime API
accepts and emits `audio/pcmu`, which is the same encoding. So audio is forwarded as
received, in both directions, with no decode, resample, or re-encode step. `forward_audio`
passes the model's base64 payload straight into a Twilio media frame. This removes the
single most obvious source of added latency and CPU, and it is the reason there is no audio
library in `requirements.txt`.

**Barge-in needs explicit bookkeeping.** Twilio buffers audio ahead of playback, so when the
receptionist starts talking over the bot, the model has already "said" more than the caller
actually heard. Left alone, the model's history diverges from reality and it starts
answering questions nobody asked. `handle_barge_in` measures how much audio genuinely played
(`latest_media_ts - response_start_ts`), truncates the model's own record of its turn to
that point, and clears Twilio's buffer. The `mark` events exist purely to track what has
actually drained.

**The model decides when to hang up.** A `hang_up` tool is exposed to the Realtime session
and the persona is told to call it after saying goodbye. Ending on the model's judgement
produces a call that closes like a conversation rather than one cut off by a timer. Twilio's
`time_limit` remains as a cost backstop, not the intended path.

## Testing strategy

The clinic's published site has no hours, providers, or insurance list, so most factual
claims the agent makes cannot be checked against an external source. Every scenario is
therefore built so failures are **provable from the transcript alone**: the persona always
asks for office hours early and always asks for a readback before hanging up. That makes ten
calls into ten samples of the same questions, so `analyze.py` can diff answers across calls
and compare readbacks against the fixed patient record in `scenarios.py` — turning "did it
get the date of birth right?" into a string comparison instead of an opinion.
