# Patient voice bot — Pretty Good AI engineering challenge

An automated caller that phones the clinic's assessment line, holds a real conversation as
an orthopedic patient, records and transcribes both sides, and surfaces bugs in the AI
receptionist on the other end.

Twilio places the call and streams the audio to a local bridge, which relays it to the
OpenAI Realtime API playing the patient. Ten scenarios, one per call, each with a fixed
patient record so the receptionist's mistakes are provable from the transcript instead of
arguable.

**Deliverables:** [BUGS.md](BUGS.md) — the curated bug report. [ITERATION.md](ITERATION.md)
— what was heard, changed, and re-heard. [ARCHITECTURE.md](ARCHITECTURE.md) — why it is
built this way. [WALKTHROUGH.md](WALKTHROUGH.md) — one call end to end.

---

## Project structure

```
pgai/
├── main.py              CLI: places calls through Twilio, waits, downloads recordings
├── server.py            The bridge: Twilio ⇄ OpenAI Realtime, transcript writer
├── scenarios.py         Patient personas + fixed patient records (the only file to edit
│                        when adding a scenario)
├── analyze.py           Three-pass bug triage over transcripts → analysis/
│
├── transcripts/         One .txt (readable) + .json (structured) per call
├── recordings/          Dual-channel .mp3 per call, one speaker per channel
├── analysis/            findings.json, consistency.md — machine output, candidates only
│
├── BUGS.md              Curated bug report (hand-written from analysis + audio)
├── ITERATION.md         Iteration log: what was heard, changed, re-heard
├── ARCHITECTURE.md      Design decisions and their costs
├── WALKTHROUGH.md       One call, start to finish
│
├── requirements.txt
└── .env.example         Copy to .env and fill in
```

Files in `transcripts/` and `recordings/` share a stem — `20260818-183853-urgent` — so audio
pairs with text at a glance. The bug report cites both.

### The four modules

| File | Role | Depends on |
| --- | --- | --- |
| `main.py` | Driver. Builds inline TwiML, dials, polls for terminal state, fetches the MP3. Never touches audio. | `scenarios.py` (names only) |
| `server.py` | The live path. One `CallSession` per call: audio pumps, barge-in bookkeeping, hang-up tool, transcript. | `scenarios.py` (persona text) |
| `scenarios.py` | Data + prompt construction. `BASE_PERSONA` + patient record + goal → session instructions. | nothing |
| `analyze.py` | Offline. Reads `transcripts/*.json`, writes `analysis/`. Never runs during a call. | `scenarios.py` (records, to diff readbacks against) |

`scenarios.py` is the only shared dependency and it imports nothing from the project. The
live path and the analysis path never touch each other.

---

## Architecture

### How it works

`main.py` places an outbound call through Twilio with the TwiML inlined into the API
request, so there is no inbound webhook route anywhere in the project. That TwiML contains a
single `<Connect><Stream>` verb pointing at `wss://<ngrok-host>/media-stream`, plus a
`<Parameter>` naming the scenario. When the assessment line answers, Twilio opens a
WebSocket to `server.py` and starts pushing the clinic agent's audio as base64 mu-law frames.

`server.py` opens a second WebSocket to the OpenAI Realtime API, configures it with a patient
persona built from `scenarios.py`, and then runs two coroutines that pump audio in each
direction for the life of the call. The model hears the receptionist and answers in speech
directly; nothing in between converts text to audio or back.

Transcripts of both sides fall out of the Realtime API as a side effect — the model's own
output transcript for the patient, and recognition on the input buffer for the receptionist —
and get written to `transcripts/` stamped `[mm:ss]` from the start of the call. Twilio
records the call separately and `main.py` downloads the MP3 once the call reaches a terminal
state.

```
  main.py ────── REST: calls.create(twiml=…, record=dual) ──────► Twilio
     │                                                              │
     │                                                              │ dials
     ▼                                                              ▼
  poll status ◄───────────────────────────────────────────  assessment line
     │                                                       (AI receptionist)
     │ on "completed"                                             ▲ │
     ▼                                                            │ │ audio
  recordings/*.mp3                                                │ ▼
                                                            ┌───────────────┐
                        ngrok tunnel                        │    Twilio     │
                  wss://<host>/media-stream ◄───────────────┤ media stream  │
                             │                              └───────────────┘
                             ▼
                       ┌──────────────────────────────┐
                       │  server.py — CallSession     │
                       │                              │
                       │  pump_twilio_to_openai  ──►  │  base64 mu-law, forwarded as-is
                       │  pump_openai_to_twilio  ◄──  │  no decode, no resample
                       │  handle_barge_in             │
                       │  hang_up (model-invoked)     │
                       │  save_transcript             │
                       └──────────────┬───────────────┘
                                      │ wss (TLS via certifi)
                                      ▼
                       ┌──────────────────────────────┐
                       │  OpenAI Realtime API         │
                       │  gpt-realtime, semantic_vad  │
                       │  persona ← scenarios.py      │
                       │  speech in → speech out      │
                       └──────────────────────────────┘
                                      │
                                      ▼
                          transcripts/*.{txt,json}
                                      │
                                      ▼
                          analyze.py ──► analysis/
```

### The stack, layer by layer

Five things are in play: the system under test, Twilio for telephony, the OpenAI Realtime
API for the patient, a local FastAPI process bridging the two, and a separate offline model
pass for triage.

#### 0. System under test — the Pretty Good AI reception line

`+1 805 439 8008`, hard-coded as `TEST_NUMBER` in [main.py:29](main.py#L29). An AI
receptionist for Pivot Point Orthopedics. Everything in this repo exists to call it, keep it
talking, and write down what it said.

Two properties shape the whole design. First, the line opens with a **recorded notice,
partly in Spanish**, offering a Spanish option — a recording, not a person, so the persona is
told explicitly to ignore it, press nothing, stay silent, and stay in English until a person
or agent greets it directly. Second, the clinic's published site lists **no hours, providers,
or insurance** — there is no ground truth to check answers against, which is why the whole
testing strategy is built on internal consistency instead (below).

The receptionist also sometimes greets the caller by the **wrong name**, recognising the
Twilio number from a previous call. The persona handles this: correct plainly, never accept
another patient's details.

#### 1. Telephony — Twilio

| Product | Used for | Where |
| --- | --- | --- |
| **Voice API** (`calls.create`) | Places the outbound call. TwiML inlined in the request, `record=True`, `recording_channels="dual"`, `time_limit=300`. | `place_call()` in [main.py](main.py#L111) |
| **TwiML** `<Connect><Stream>` | Points the call at `wss://<host>/media-stream`; a `<Parameter>` carries the scenario name. | `build_twiml()` in [main.py](main.py#L98) |
| **Media Streams** | Bidirectional WebSocket. Inbound: the receptionist's audio as base64 mu-law frames. Outbound: the patient's audio, plus `clear` on barge-in. | [server.py](server.py#L217) |
| **Call status polling** | Blocks until `completed` / `busy` / `failed` / `no-answer` / `canceled`. | `wait_for_call()` |
| **Recordings API** | Dual-channel MP3, one speaker per channel, downloaded with retry backoff. | `download_recording()` |
| **Python SDK** (`twilio>=9.0`) | REST client in both `main.py` and `server.py` (the latter only to end a call). | — |

Twilio Media Stream events consumed:

| Event | Handling |
| --- | --- |
| `start` | Capture `streamSid`, `callSid`, and `customParameters.scenario`; then configure the Realtime session. Unknown scenario falls back to the first. |
| `media` | Record `timestamp` (this is the clock everything is stamped against), forward payload as `input_audio_buffer.append`. |
| `mark` | Pop one mark — this is how the bridge knows what audio has actually drained out of Twilio's buffer. |
| `stop` | Break the pump. |

#### 2. The patient — OpenAI Realtime API

A second WebSocket, `wss://api.openai.com/v1/realtime?model=gpt-realtime`, opened per call
with a bearer token and a TLS context pinned to certifi's CA bundle.

| Setting | Value | Why |
| --- | --- | --- |
| `model` | `gpt-realtime` | Speech in, speech out, one hop |
| `output_modalities` | `["audio"]` | No text branch to render |
| input / output `format` | `audio/pcmu` | Same encoding Twilio speaks — no transcode |
| `turn_detection` | `semantic_vad`, `eagerness: high`, `interrupt_response: true` | Ends a turn on a finished *thought*, not on elapsed silence. `server_vad` (threshold 0.5, 700ms silence) is the fallback |
| `transcription.model` | `gpt-4o-transcribe` | `whisper-1` misreads 8kHz phone audio badly, and the bug report quotes these transcripts |
| `voice` | `cedar` / `marin` per scenario (`alloy` default) | Ten calls shouldn't sound like one person phoning back ten times |
| `tools` | `hang_up(reason)` | The model ends its own call |
| `instructions` | `build_instructions(scenario)` | `BASE_PERSONA` + patient record + goal |

Realtime events consumed:

| Event | Handling |
| --- | --- |
| `response.output_audio.delta` | Forward base64 straight into a Twilio media frame; stamp the start of a bot turn |
| `input_audio_buffer.speech_started` | Record where the agent's turn began (`audio_start_ms`), trigger barge-in handling |
| `input_audio_buffer.speech_stopped` | Record `audio_end_ms` — the reference point for measuring reply delay |
| `conversation.item.input_audio_transcription.completed` | Write a `CLINIC_AGENT` turn |
| `response.output_audio_transcript.done` | Write a `PATIENT_BOT` turn |
| `response.function_call_arguments.done` | If `hang_up`, let the closing line drain, end the call via the Twilio REST client, break |
| `error` | Log the raw event |

#### 3. The bridge — FastAPI + asyncio

`server.py` is a FastAPI app with exactly two routes:

- `GET /health` — polled by `main.py` before dialling, so a forgotten bridge costs nothing.
- `WebSocket /media-stream` — the whole call.

Per connection it constructs one `CallSession` holding stream identity, barge-in
bookkeeping, turn-start positions, and the transcript. Two `asyncio` tasks pump audio in
opposite directions; `asyncio.wait(..., FIRST_COMPLETED)` means whichever side finishes
first ends the call, and the transcript is written in a `finally` so a crash never costs a
deliverable.

Served by **uvicorn** (`uvicorn[standard]`), with the **`websockets`** library as the client
side for the OpenAI connection. No audio library anywhere — none is needed when nothing is
transcoded.

#### 4. Triage — a second, offline model

[analyze.py](analyze.py) runs after the calls, never during. It uses the standard OpenAI
Python SDK against **`gpt-4o`** (`ANALYSIS_MODEL`), asking for JSON on every pass. Passes B
and C put the model only on *extraction* and leave the comparison to plain Python, because
"these two calls said different things" should be a string diff, not an opinion.

#### 5. Supporting pieces

| Package | Role |
| --- | --- |
| `ngrok` (external binary) | Public TLS host for the media-stream WebSocket. `main.py` queries its local API at `localhost:4040` for the current hostname rather than relying on a hand-copied value |
| `certifi` | Explicit CA bundle for the OpenAI WebSocket — a python.org install on macOS has no roots until you run `Install Certificates.command`, and that failure looks like a mid-call crash |
| `python-dotenv` | Loads `.env` in all three entry points |
| `requests` | ngrok discovery, `/health` check, recording download |

#### Configuration

Required in `.env`: `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`TWILIO_FROM_NUMBER`.

Everything else has a working default:

| Variable | Default | Effect |
| --- | --- | --- |
| `PUBLIC_HOST` | *(auto-discovered from ngrok)* | Host for the `wss://` stream URL; an explicit value always wins |
| `PORT` | `5050` | Bridge port |
| `REALTIME_MODEL` | `gpt-realtime` | The patient |
| `REALTIME_VOICE` | `alloy` | Fallback voice; scenarios override |
| `TRANSCRIBE_MODEL` | `gpt-4o-transcribe` | Recognition on the receptionist's audio |
| `TURN_DETECTION` | `semantic` | `semantic_vad`; anything else selects `server_vad` |
| `VAD_EAGERNESS` | `high` | How fast the patient jumps in |
| `SILENCE_MS` | `700` | `server_vad` only |
| `ANALYSIS_MODEL` | `gpt-4o` | Triage model |
| `TRANSCRIPT_DIR` / `RECORDING_DIR` | `transcripts` / `recordings` | Output paths |

The destination number is deliberately **not** on this list — see [Safety](#safety).

### Why this shape

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
passes the model's base64 payload straight into a Twilio media frame. This removes the single
most obvious source of added latency and CPU, and it is the reason there is no audio library
in `requirements.txt`.

**No inbound webhook.** The TwiML is inlined into the `calls.create` request rather than
served from a route, and `server.py` reads the scenario back out of
`start.customParameters`. The only public surface is the media-stream WebSocket and a
`/health` check — there is no callback route to secure or keep in sync with the tunnel.

**Barge-in needs explicit bookkeeping.** Twilio buffers audio ahead of playback, so when the
receptionist starts talking over the bot, the model has already "said" more than the caller
actually heard. Left alone, the model's history diverges from reality and it starts answering
questions nobody asked. `handle_barge_in` measures how much audio genuinely played
(`latest_media_ts - response_start_ts`), truncates the model's own record of its turn to that
point, and clears Twilio's buffer. The `mark` events exist purely to track what has actually
drained.

**The model decides when to hang up.** A `hang_up` tool is exposed to the Realtime session
and the persona is told to call it after saying goodbye. Ending on the model's judgement
produces a call that closes like a conversation rather than one cut off by a timer. Twilio's
`time_limit` remains as a cost backstop, not the intended path.

**Turn boundaries are stamped by audio position, not arrival time.** Recognition of the two
sides completes independently and at unpredictable delays, so stamping a turn when its event
arrives puts it in the wrong place in the transcript. Turns are stamped where they began in
the audio and sorted by that position before writing. The bug report cites these stamps, so
they have to be right.

### Testing strategy

The clinic's published site has no hours, providers, or insurance list, so most factual
claims the agent makes cannot be checked against an external source. Every scenario is
therefore built so failures are **provable from the transcript alone**: the persona always
asks for office hours early and always asks for a readback before hanging up. That makes ten
calls into ten samples of the same questions, so `analyze.py` can diff answers across calls
and compare readbacks against the fixed patient record in `scenarios.py` — turning "did it
get the date of birth right?" into a string comparison instead of an opinion.

### Failure modes designed around

| Failure | Where it would show up | Handled by |
| --- | --- | --- |
| Stale ngrok host | Silence on a connected, billing call | `discover_ngrok_host()` asks the running tunnel |
| Bridge not running | Call connects to nothing | `/health` check before dialling |
| Missing root certificates | Looks like a mid-call crash | TLS context pinned to `certifi`'s bundle |
| Crash mid-call | Lost transcript, the deliverable | Transcript written in a `finally` |
| One socket closes first | Hang waiting on the other | First task to finish ends the call |
| Twilio error on one scenario | Nine other calls abandoned | Per-scenario `try` in `cmd_call` |
| Wedged call | Unbounded billing | `time_limit=300` |

Same material, in prose: [ARCHITECTURE.md](ARCHITECTURE.md). One call narrated end to end:
[WALKTHROUGH.md](WALKTHROUGH.md).

---

## Setup

**1. Accounts**

- **Twilio** — sign up, **upgrade from trial** (trial accounts can only dial pre-verified
  numbers, so they cannot reach the assessment line), and buy one voice-capable US number.
- **OpenAI** — an API key with billing enabled.

**2. Install**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
brew install ngrok            # macOS; see ngrok.com for other platforms
```

**3. Configure**

```bash
cp .env.example .env          # then fill in the values
```

`.env` is gitignored and must never be committed.

**4. Open the tunnel**

Twilio has to reach this machine, so run ngrok in its own terminal and leave it running:

```bash
ngrok config add-authtoken <token>    # one time, from dashboard.ngrok.com
ngrok http 5050
```

That's all — `PUBLIC_HOST` can stay empty. The free tier issues a new host on every restart,
so rather than re-copying it each session, `main.py` asks the running ngrok for its own
hostname. Set `PUBLIC_HOST` explicitly only if you are tunnelling some other way; an
explicit value always wins.

---

## Running

Two terminals.

```bash
# terminal 1 — the bridge
python server.py

# terminal 2 — place calls
python main.py call all
```

| Command | Does |
| --- | --- |
| `python main.py list` | Show the scenarios and their labels |
| `python main.py call new_knee` | Place one call |
| `python main.py call urgent weekend` | Place a named subset |
| `python main.py call all` | All ten, sequentially, 10s apart |
| `python main.py fetch` | Download any recording whose transcript exists but whose audio does not |

`main.py` checks the bridge's `/health` before dialling, so a forgotten `server.py` costs
nothing. One Twilio error doesn't abandon the remaining calls. `time_limit` is 300s as a
cost backstop — the intended path is the model calling its own `hang_up` tool after saying
goodbye.

### Output

| Path | Contents |
| --- | --- |
| `transcripts/*.txt` | Readable transcript, every turn stamped `[mm:ss]` from call start |
| `transcripts/*.json` | Same, structured, plus `call_sid` and reply delays — consumed by `analyze.py` |
| `recordings/*.mp3` | Dual-channel recording, one speaker per channel |

Timestamps are stamped at the audio position a turn *began*, not when recognition finished —
recognition completes at unpredictable delays, and the bug report cites these stamps.

---

## Scenarios

Ten calls covering scheduling, rescheduling, cancellation, refills, and
hours/location/insurance, plus three edge cases. Each is a fixed patient with a fixed
record, so readback errors are checkable by string comparison rather than judgement.

| Scenario | Patient | What it tests |
| --- | --- | --- |
| `new_knee` | Daniel Reyes | New patient, acute injury, wants first available. Needs a specific day and time before hanging up. |
| `postop` | Krzysztof Wojcik | Post-op follow-up. Name spelled out letter by letter — the primary readback capture test. |
| `reschedule` | Maria Alvarez | Moving a PT appointment; **deliberately interrupts** mid-answer once, then steers back. |
| `cancel` | Tom Whitfield | Cancelling outright, declines any reschedule. Needs a clear confirmation, not "I'll pass it along". |
| `refill` | Priya Nair | Post-surgical pain medication refill — controlled-substance policy probe. |
| `imaging` | Grace Osei | Chasing MRI results and a referral. Pushes past "someone will get back to you". |
| `insurance` | Brian Tulloch | Hours, location, insurance, weekends — **the consistency reference call**. |
| `weekend` | Anna Kowalski | **Edge:** insists on a Sunday appointment, asks three times. |
| `vague` | Barbara Turner | **Edge:** unclear, rambling; stays vague until asked to clarify twice. |
| `urgent` | Robert Chen | **Edge:** describes crooked wrist, numb cold fingers — never says "emergency". Tests whether escalation happens unprompted. |

### How a scenario is built

Every scenario is `BASE_PERSONA` + a patient record + a goal, assembled by
`build_instructions()`.

- **`BASE_PERSONA`** — shared. Every rule in it suppresses a specific default behaviour of
  an instruction-tuned model: don't offer to help, don't summarise, don't say "is there
  anything else", one or two sentences per turn, wait for them to finish, never claim to be
  an AI. Also handles the line's recorded Spanish notice (ignore it, stay silent, stay in
  English) and wrong-name greetings (correct plainly, never accept another patient's
  details).
- **`patient`** — the fixed record: name, DOB, callback number, insurance, sometimes
  pharmacy. Fixed rather than invented because invented details drift over a call, and a
  fixed record is what `analyze.py` diffs the readback against.
- **`goal`** — the reason for the call, and the one place a scenario may override the
  no-interrupting rule (`reschedule` does).
- **`voice`** — per scenario, so ten calls don't sound like one synthetic person phoning
  back ten times.

Two behaviours are baked into every persona so ten calls become ten samples of the same
questions: **ask for office hours** in the middle, and **ask for a readback** near the end.
That is what makes cross-call diffing possible.

### Adding one

A dict entry in [scenarios.py](scenarios.py). No other file changes.

```python
"my_scenario": {
    "label": "One line, shown by `main.py list`",
    "voice": "cedar",
    "patient": {
        "full_name": "...", "dob": "...", "phone": "...", "insurance": "...",
    },
    "goal": """What you want and what you refuse to leave without.""",
},
```

---

## Finding bugs

```bash
python analyze.py
```

Writes `analysis/findings.json` and `analysis/consistency.md`. These are **candidates, not
conclusions** — [BUGS.md](BUGS.md) is curated by hand from them.

| Pass | Method | Catches |
| --- | --- | --- |
| **A** — per-call triage | LLM judgement, one call at a time | Self-contradictions, missed escalations, unsafe commitments, dead-ends |
| **B** — cross-call consistency | LLM extracts claims, **code** diffs them | Different office hours / insurance answers given on different calls |
| **C** — readback accuracy | LLM extracts, **code** compares to `scenarios.py` | Wrong name, DOB, phone captured or read back |

Passes B and C put the LLM only on extraction and leave the comparison to code, because the
claim is "these two calls said different things" and that should be a string diff, not an
opinion. The triage prompt is deliberately strict: every finding must quote the receptionist
verbatim, and anything resting on a garbled line is skipped.

**Why this design.** The clinic's published site has no hours, providers, or insurance list,
so most factual claims cannot be checked against an external source. Every scenario is
therefore built so failures are provable from the transcript alone.

**One caveat that matters.** Transcripts come from speech recognition on 8kHz phone audio,
which misreads things. **Check any quote against the recording before citing it as a bug.**
Two findings were rejected on exactly this basis — see "Checked and not reported" in
[BUGS.md](BUGS.md).

---

## Safety

The destination number is a constant in [main.py](main.py#L29) and is **not** configurable
by env var or flag. Every call goes through `place_call()`, which takes no destination
argument. There is no input path that can point this at any number other than the assessment
line.

`MAX_CALL_SECONDS = 300` caps what a wedged call can bill. The persona never claims to be a
real person unprompted — but when asked directly it says yes, because a receptionist told
mid-call that it is talking to a test stops behaving like a receptionist, and that is the
thing under test.
