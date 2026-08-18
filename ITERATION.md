# Iteration log

What changed after listening to calls, and what it did.

Almost nothing here was findable by reading code. Every entry below started with either
an error in the terminal or something that sounded wrong in a recording — which is why
the first real call was made as early as possible, before the scenarios and the analysis
tooling were finished.

| # | Found by | Problem | Change | Effect |
| --- | --- | --- | --- | --- |
| 1 | Terminal error, first call attempt | `CERTIFICATE_VERIFY_FAILED`, call died mid-connection | Pin the TLS context to certifi | First successful call |
| 2 | Listening to call 1 | Sounded robotic; opened by demanding office hours | Persona rewrite, better voices, semantic VAD, better transcription | Call 2 flowed naturally and caught a real bug itself |
| 3 | Watching call 2 run long | Hit the 300s cost cap chasing a correction | Wrap up once the goal is met | Calls now end at 2–3 min via the model's own hang-up |
| 4 | Listening to call 2 | Female voice playing a male patient | Match voice to persona; correct a wrong-name greeting | Voice and persona coherent; identity held across all later calls |
| 5 | Reading call 3's transcript | The barge-in test never fired | Let a scenario override the turn-taking rule | Interruption attempted in call 4 |
| 6 | Noticing missing MP3s | `main.py` crashed right after dialling | Fix the crash; name recordings after transcripts; add `fetch` | Audio downloads automatically and pairs with its transcript |
| 7 | Reading call 4's transcript | Bot answered the Spanish notice in Spanish | Name the recording explicitly in the persona | Clean English openings afterwards |
| 8 | Listening to calls 3–4 | Long pause before every reply | Instrument the delay, raise VAD eagerness | **~1.5s average reply delay, measured** |
| 9 | Reviewing `analyze.py` output | False positives in the findings | Canonicalise extracted values before diffing | 26 candidates → 20, survivors all real |

---

## The three worth explaining

### The reply delay — heard first, then measured

After calls 3 and 4 I noticed an audible hole between the receptionist finishing and the
bot answering. It was not visible in the transcripts, and the first attempt to measure it
was wrong: turns recorded only where speech *started*, so the apparent gap included
however long the agent had been speaking.

The fix was to instrument it properly. `input_audio_buffer.speech_stopped` carries
`audio_end_ms`, so every bot turn now records `reply_delay_ms` — the wait the caller
actually experienced — and prints it live during the call.

With that in place, `semantic_vad` eagerness went from `medium` to `high`. Medium waits to
be confident a turn has ended, and that caution was the pause.

| | Before | After |
| --- | --- | --- |
| Reply delay | Audible, unmeasured | **1.4–1.6s average, 2.8s worst** across postop, refill, insurance |

This is a genuine trade-off, not a free win: the very first call had the opposite problem,
talking over the greeting. Eagerness `high` reintroduced a mild version of that on one
call in three. Left as-is — a caller who leaves multi-second holes sounds less human than
one who occasionally starts half a beat early.

### The voice that did not match the patient

Voices were originally chosen purely on audio quality — the docs recommend `marin` and
`cedar`, so both went in. Listening to call 2 made the problem obvious: `marin` reads
female, and it was playing Daniel Reyes, who then confirmed "yes, I'm Daniel" in a woman's
voice.

Fixed by picking voice per persona and rebalancing two personas so the split stayed even
rather than seven callers sharing one voice.

Worth recording because the cause was a real trade-off rather than a typo: optimising for
one dimension quietly broke another, and only listening surfaced it.

### Where the analysis tooling was wrong

The first `analyze.py` run produced 26 candidates, and several were noise: `"March 4,
1991"` versus `"March 4th, 1991"` flagged as a mismatch, carrier phrases left in extracted
values so `"Krzysztof Wojcik"` never matched itself, and five identically-scheduled calls
counted as five different office hours.

Extraction now returns bare values in a canonical form, and comparison ignores ordinal
suffixes. That dropped it to 20, and revealed that **office hours are actually consistent
across every call that stated them** — a bug that was assumed and turned out not to exist.

The per-call triage also missed the strongest self-contradiction in the whole set: the
8:15am booking against 9:00am opening hours. That was found by reading. The honest
summary is that the mechanical passes are reliable and the LLM judgement pass is not,
which is why `BUGS.md` is curated by hand.

---

## A/B comparison

Same scenario, before and after the persona rewrite:

- **Before:** `recordings/20260817-215732-new_knee.mp3` — talks over the greeting, opens by
  demanding office hours, repeats the same question three times in six seconds
- **After:** `recordings/20260817-221635-new_knee.mp3` — waits for the greeting, states its
  reason, asks about hours mid-call, and spots that an 8:15am booking conflicts with 9:00am
  opening hours

The early recordings are kept deliberately rather than replaced.

---

## Known, not fixed

- **Barge-in fires late.** The interruption in `reschedule` lands after the agent's turn
  rather than mid-sentence. Interruption is attempted but not convincingly timed.
- **Occasional clipped opening.** Eagerness `high` sometimes starts a beat into the
  greeting. Accepted deliberately — see the trade-off above.
- **Caller ID collision.** All calls come from one number, as the submission requires, so
  the clinic greets every persona as whoever called last. The caller now corrects it,
  which turned an artifact into a probe of identity handling.
