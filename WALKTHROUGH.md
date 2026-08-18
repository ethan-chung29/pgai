# Walkthrough

How a call actually works, end to end, and why it is built this way.
[ARCHITECTURE.md](ARCHITECTURE.md) covers the design decisions in brief; this is the
mechanical detail behind them.

## The three moving parts

| Component | Role |
| --- | --- |
| **Twilio** | Owns the phone number, places the call, records it |
| **`server.py`** | The bridge. Holds one connection to Twilio and one to OpenAI |
| **`main.py`** | The CLI. Starts a call, waits for it to end, downloads the audio |

`main.py` never touches audio. It is the remote control; `server.py` is the machine.
An **ngrok** tunnel gives Twilio a public address that reaches the bridge on localhost.

## One call, start to finish

1. **`python main.py call new_knee`.**

2. **Preflight.** [`check_bridge_running`](main.py) hits `localhost:5050/health`. A call
   that connects to a dead bridge still costs money, so this fails first and free.

3. **Find the tunnel.** [`discover_ngrok_host`](main.py) asks ngrok's local API for its
   own hostname, because the free tier issues a new one on every restart.

4. **Build the TwiML.** Twilio's XML instruction language, inlined into the API request
   so no inbound webhook route exists anywhere in the project:

   ```xml
   <Connect>
     <Stream url="wss://xxxx.ngrok-free.dev/media-stream">
       <Parameter name="scenario" value="new_knee"/>
     </Stream>
   </Connect>
   ```

   The `<Parameter>` is how the bridge learns which patient to play.

5. **Dial.** The destination is a module constant, not configurable by env var or flag —
   there is no input path that can point this at another number. `recording_channels="dual"`
   puts each speaker on its own channel; `time_limit` caps the cost of a wedged call.

6. **Twilio connects** and sends a `start` event carrying the call SID and the scenario.

7. **The bridge opens its second socket** to the Realtime API and configures the session:
   persona text from `scenarios.py`, voice, audio format, turn detection, and the
   `hang_up` tool.

8. **Two coroutines pump audio** for the life of the call:
   - `pump_twilio_to_openai` — forwards the receptionist's audio, ~20ms per chunk
   - `pump_openai_to_twilio` — forwards the model's speech back

   Whichever finishes first ends the call; the other is cancelled rather than left
   waiting on a socket nobody will write to again.

9. **The model hangs up.** It calls the `hang_up` tool, and the bridge waits two seconds
   for the closing line to drain out of Twilio's buffer before ending the call.

10. **The transcript is written** in a `finally` block, so a crash mid-call cannot lose it.

11. **`main.py` downloads the MP3**, named identically to the transcript.

## What "one turn" actually means

There is no speech-to-text or text-to-speech in the conversation path.

Twilio streams mu-law 8kHz. The Realtime API accepts `audio/pcmu` — the same encoding —
so audio is forwarded exactly as received, in both directions, with no decode, resample,
or re-encode. `gpt-realtime` consumes audio tokens natively and emits audio tokens
directly. Nothing becomes text on the way through.

A separate transcription model runs alongside and writes the log files. Nothing in the
conversation reads them. Disable it and the bot behaves identically — you just lose the
evidence. This is also why transcripts contain errors the bot never made, and why every
quote in [BUGS.md](BUGS.md) is checked against the recording first.

There is also no discrete "turn" in the protocol. The session is continuous; a turn is
just an interval that voice-activity detection decided was one.

## Three things that are harder than they look

**Barge-in.** Twilio buffers audio ahead of playback, so when the receptionist interrupts,
the model has already "said" more than the caller actually heard. Left alone its history
diverges from reality and it answers questions nobody asked. `handle_barge_in` measures
what genuinely played, truncates the model's record of its own turn to that point, and
clears Twilio's buffer.

**Timestamps.** Stamping a turn when its transcription arrives puts it in the wrong place,
because recognition finishes at a different delay for each side. Each side now reports
where its speech actually began — `audio_start_ms` for the receptionist, first outbound
audio for the bot. Bug reports cite these stamps against the MP3, so they have to land.

**Reply delay.** `speech_stopped` gives the moment the receptionist stopped talking, so
every bot turn records `reply_delay_ms`: the wait the caller actually experienced. Turn
detection is tuned against that number rather than against impressions.

## Finding bugs without ground truth

The clinic publishes no hours, providers, or insurance list, so most claims the agent
makes cannot be checked against an external source. Every scenario is therefore built so
failures are provable from the transcript alone:

1. **Self-contradiction** — states a policy, then violates it
2. **Cross-call inconsistency** — same question, different answers on different calls
3. **Readback errors** — compared against the fixed patient record in `scenarios.py`

Every persona asks for office hours mid-call and for a readback near the end, so all ten
calls double as consistency samples. [`analyze.py`](analyze.py) then does the diffing in
plain code — the LLM only extracts what was said.

## The loop

```
run a call -> listen -> find what broke -> change scenarios.py or server.py -> commit -> run again
```

Every commit records what a real call taught us. Nothing important here was visible from
reading code: TLS with no root certificates, a caller that demanded office hours before
saying why it rang, a female voice playing a male patient, multi-second holes before every
reply. All of it needed a phone to actually ring.
