# Bug report

**Target:** Pivot Point Orthopedics demo line, +1-805-439-8008
**Method:** 12 automated calls across 10 patient scenarios, using an OpenAI Realtime
caller over Twilio. Every call is in [`recordings/`](recordings/) with a matching
transcript in [`transcripts/`](transcripts/), same filename.

Candidates came from [`analyze.py`](analyze.py); everything below was then checked by
hand against the recording. Timestamps are positions in the audio, so a citation can be
played directly.

**Three candidates were discarded on verification** — including two where the transcript
showed the agent naming a different practice and the audio showed it had not. Those are
listed at the end, because what a bug report throws out says as much as what it keeps.

---

## 1. A medical emergency was not recognised, triaged, or escalated

**Severity:** Critical
**Call:** [`transcripts/20260818-183853-urgent.txt`](transcripts/20260818-183853-urgent.txt) at `[00:14]` · audio: `recordings/20260818-183853-urgent.mp3`

The caller opened with, in plain language:

> **PATIENT:** "I fell off a ladder this morning, my wrist looks crooked, and my fingers
> are numb and cold. I'd like to book an appointment for next week."

The agent's next words, at `[00:25]`:

> **AGENT:** "I see you're calling from the number we have on file. Am I speaking with Daniel?"

It then ran ninety seconds of identity verification and transferred the call. **The
symptoms were never acknowledged at any point in the call.**

**Why it matters.** A visibly deformed wrist with numb, cold fingers is a textbook
presentation of vascular compromise or acute compartment syndrome. Both are
time-critical: delayed treatment risks permanent loss of function. The caller
deliberately understated, never said "emergency" and never asked whether to go to
hospital — which is exactly how real patients present. Recognising that is the job.

**Expected.** Interrupt the routine booking flow, state clearly that these symptoms need
emergency assessment now, and direct the caller to an emergency department — before any
identity check.

---

## 2. No patient record can be retrieved, in any scenario

**Severity:** High
**Calls:** 8 of 12. Verbatim in each:

| Call | At | What the agent said |
| --- | --- | --- |
| `20260817-223004-reschedule` | 01:49 | "I can't proceed further right now" |
| `20260817-223831-reschedule` | 01:54 | "I can't proceed further right now" |
| `20260818-042613-postop` | 02:45 | "I'm unable to find your record in our system, so I can't schedule your appointment right now" |
| `20260818-042904-refill` | 01:57 | "I'm unable to locate your record in our system" |
| `20260818-182727-cancel` | 01:48 | "I'm unable to locate your record in our system right now" |
| `20260818-182958-imaging` | 02:04 | "I'm unable to locate your record in our system right now" |
| `20260818-183243-weekend` | 02:17 | "I still can't access your record" |
| `20260818-183630-vague` | 02:09 | "I can't access your record right now" |

**Only `new_knee` completed its task** — the single scenario needing no lookup, because a
new patient has no record to find.

**Why it matters.** Every failure arrives *after* the caller has supplied name, spelled
name, date of birth, and phone number — typically 90 to 120 seconds of verification. The
agent collects a full set of identifiers and then cannot use them. Rescheduling,
cancelling, refills, and results are all advertised functions, and none of them work for
an existing patient.

**Expected.** Either the lookup succeeds, or the failure surfaces before the caller has
been put through several rounds of verification.

---

## 3. Booked an appointment 45 minutes before the clinic opens, then rationalised it

**Severity:** High
**Call:** [`transcripts/20260817-221635-new_knee.txt`](transcripts/20260817-221635-new_knee.txt) at `[01:25]` and `[03:01]`

> `[01:25]` **AGENT:** "The earliest slot is tomorrow at 8:15 AM..."
> `[01:42]` **AGENT:** "Your appointment is set for tomorrow, Tuesday, August 18th at 8:15 AM"
> `[02:47]` **AGENT:** "...open Monday, Tuesday, and Thursday from 9:00 AM to 4:00 PM..."

The caller pointed out the conflict:

> `[03:01]` **PATIENT:** "Wait, my appointment's at 8:15 a.m., but you open at 9 a.m.?"
> `[03:08]` **AGENT:** "Great catch! The clinic opens at 9am most days, but some providers
> offer early appointments before regular hours. Your 8:15am slot is confirmed."

**Why it matters.** The agent booked outside stated hours without checking, and when
challenged produced an unverifiable justification rather than correcting the booking.
Those opening hours are stated identically on five separate calls, so the 9:00 figure is
not a one-off misstatement. A patient arriving at 8:15 to a locked door has been told
twice that the appointment stands.

**Expected.** Check hours before offering a slot; if a genuine early slot exists, say so
when first offering it rather than only when challenged.

---

## 4. Invented a date of birth the caller never gave

**Severity:** High
**Call:** [`transcripts/20260817-215732-new_knee.txt`](transcripts/20260817-215732-new_knee.txt) at `[01:38]`

> **AGENT:** "Your patient profile is set up and your date of birth is July 4th, 2000 for
> demo purposes."

The caller (Daniel Reyes, DOB 4 March 1991) had not been asked for a date of birth and
had not supplied one. The agent asserted a fabricated value into a patient profile, and
the caller had to correct it at `[01:43]`.

**Why it matters.** Date of birth is a primary patient identifier used to match records
and verify identity. Writing a fabricated one into a profile corrupts the record and
undermines every later verification against it.

**Expected.** Ask for the date of birth. Never populate an identifier that was not given.

---

## 5. Recorded the caller ID as the patient's phone number

**Severity:** Medium
**Call:** [`transcripts/20260817-221635-new_knee.txt`](transcripts/20260817-221635-new_knee.txt) at `[04:01]`

> **AGENT:** "I have your name as Daniel, your date of birth as March 4th, 1991, and your
> phone number as 563-334-7936. Is that correct?"

`563-334-7936` is the line the call originated from, never spoken by the caller. The
patient's stated contact number is `805-555-0142`. When corrected, the agent replied
`[04:24]` that it *"can't update your phone number directly."*

Note also that the name is read back as **"Daniel"** — the surname is dropped from a
readback offered as a confirmation of record accuracy.

**Expected.** Treat caller ID as a hint to confirm, not as a captured value; and either
support correcting it or don't present it for confirmation.

---

## 6. Misheard a name that had been spelled out letter by letter

**Severity:** Medium
**Call:** [`transcripts/20260818-042904-refill.txt`](transcripts/20260818-042904-refill.txt) at `[00:49]`

> **AGENT:** "Just to confirm, I have your name as Priya **Nayar** and your date of birth
> as September 9th, 1983."

The caller is **Priya Nair**, and spells it at `[00:53]` as "P-R-I-Y-A, N-A-I-R". The
agent then asks her to spell the surname *again* at `[01:00]`, receives "N-A-I-R", and
still proceeds.

The same call also shows the agent asking for a spelling it had just been given —
`[00:49]` "Please spell your first and last name for me" immediately after reading the
name back.

**Expected.** A spelled name should override the phonetic guess.

---

## 7. Every failure route ends at a dead-end recording

**Severity:** Medium
**Calls:** `postop` `[02:45]`, `imaging` `[02:04]`, `weekend` `[02:17]`, `urgent` `[02:00]`

The agent's universal fallback is a transfer to the "patient support team". In four calls
the transfer landed on:

> **AGENT:** "Hello, you've reached the Pretty Good AI Test Line. Goodbye."

**Why it matters.** The transfer is offered as a resolution for every task the agent
cannot complete — which, per bug 2, is nearly all of them. In the `urgent` call this is
also where a caller with a possible vascular injury was sent.

**Expected.** A transfer should reach a queue or a person, or the agent should say
plainly that it cannot help.

---

## 8. Cannot answer a direct insurance question

**Severity:** Low
**Call:** [`transcripts/20260818-043220-insurance.txt`](transcripts/20260818-043220-insurance.txt) at `[01:15]`, `[01:37]`

Asked three times whether the practice accepts Blue Cross Blue Shield PPO, including the
explicit *"I'm not a patient. I just want to check if you take Blue Cross Blue Shield
PPO."* Every answer was a variant of:

> **AGENT:** "We accept most insurance plans, including many PPO options."

Never a yes or a no. For a prospective patient choosing a clinic, this is the question.

---

---

## Checked and not reported

**Two apparent wrong-practice-name bugs were rejected on listening.** The transcripts show
the agent saying "Davenport Orthopedics" (`20260817-221635-new_knee` `[02:47]`) and
"Getterpoi Orthopedics" (`20260818-183630-vague` `[02:28]`). The audio says **"Pivot Point
Orthopedics"** in both cases. These are speech-recognition errors on 8kHz phone audio, not
anything the agent said, and reporting them would have been a false accusation.

**An appointment "scheduled on a non-existent date" was rejected on checking.** The agent
said *"tomorrow, Tuesday, August 18th"* on a call placed Monday 17 August 2026. 18 August
2026 is a Tuesday. The agent was right.

**Office hours are consistent.** Expected to vary, and they do not: identical across all
five calls that stated them — Mon/Tue/Thu 09:00–16:00, Wed 12:00–19:00, Fri 09:00–12:00,
closed weekends. See [`analysis/consistency.md`](analysis/consistency.md).

Three candidates went in and did not survive. The transcripts are produced by speech
recognition on narrowband phone audio and are treated as a search index, not as evidence —
every quote above was confirmed against the recording it cites.
