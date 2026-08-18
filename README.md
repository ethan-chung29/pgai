# Patient voice bot — Pretty Good AI engineering challenge

An automated caller that phones the assessment line, holds a conversation as an orthopedic
patient, records and transcribes both sides, and surfaces bugs in the clinic's AI
receptionist.

Twilio places the call and streams the audio to a local bridge, which relays it to the
OpenAI Realtime API playing the patient. [ARCHITECTURE.md](ARCHITECTURE.md) covers why it is
built this way; [WALKTHROUGH.md](WALKTHROUGH.md) walks through a call end to end.

## Setup

**1. Accounts**

- **Twilio** — sign up, **upgrade from trial** (trial accounts can only dial numbers you
  have pre-verified, so they cannot reach the assessment line), and buy one voice-capable
  US number.
- **OpenAI** — create an API key with billing enabled.

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

That's all — `PUBLIC_HOST` can stay empty. The free tier issues a new host on every
restart, so rather than re-copying it each session, `main.py` asks the running ngrok for
its own hostname. Set `PUBLIC_HOST` explicitly only if you are tunnelling some other way;
an explicit value always wins.

## Running

Two terminals.

```bash
# terminal 1 — the bridge
python server.py

# terminal 2 — place calls
python main.py call all
```

Other commands:

```bash
python main.py list             # show the scenarios
python main.py call new_knee    # place one call
```

`main.py` checks the bridge is up before dialling, so a forgotten `server.py` costs nothing.

## Output

| Path | Contents |
| --- | --- |
| `transcripts/*.txt` | Readable transcript, every turn stamped `[mm:ss]` from call start |
| `transcripts/*.json` | Same, structured, consumed by `analyze.py` |
| `recordings/*.mp3` | Dual-channel recording, one speaker per channel |

## Finding bugs

```bash
python analyze.py
```

Writes `analysis/findings.json` and `analysis/consistency.md`. These are **candidates, not
conclusions** — [BUGS.md](BUGS.md) is curated by hand from them.

The script runs three passes: per-call triage for self-contradictions and missed
escalations; a cross-call diff that catches the receptionist giving different office hours
or insurance answers on different calls; and a readback check that compares repeated-back
details against the fixed patient record in `scenarios.py`.

One caveat that matters: transcripts come from speech recognition on 8kHz phone audio,
which misreads things. **Check any quote against the recording before citing it as a bug.**

## Scenarios

Ten calls covering scheduling, rescheduling, cancellation, refills, and
hours/location/insurance, plus edge cases. Each is a fixed patient with a fixed record, so
readback errors are checkable.

| Scenario | What it tests |
| --- | --- |
| `new_knee` | New patient, acute injury, wants first available |
| `postop` | Post-op follow-up; hard-to-spell name, primary readback test |
| `reschedule` | Moving an appointment; includes a deliberate interruption |
| `cancel` | Cancelling outright without accepting a reschedule |
| `refill` | Post-surgical pain medication — controlled-substance handling |
| `imaging` | Chasing MRI results and a referral |
| `insurance` | Hours, location, insurance, weekends — the consistency reference call |
| `weekend` | **Edge:** insists on a Sunday appointment |
| `vague` | **Edge:** unclear, rambling request |
| `urgent` | **Edge:** describes symptoms that should trigger escalation |

Adding one is a dict entry in `scenarios.py` — no other file changes.

## Safety

The destination number is a constant in `main.py` and is not configurable by env var or
flag. There is no input path that can point this at any number other than the assessment
line.
