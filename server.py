"""Bridges a Twilio phone call to the OpenAI Realtime API.

Twilio streams the clinic agent's audio in over a WebSocket; we forward it to the
Realtime API, which replies as our patient persona, and we stream that audio back.
Both sides are mu-law 8kHz, so audio passes through untranscoded.
"""

import asyncio
import json
import os
import ssl
from datetime import datetime, timezone
from pathlib import Path

import certifi
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket
from fastapi.websockets import WebSocketDisconnect
from twilio.rest import Client

from scenarios import SCENARIOS, build_instructions, get_voice

load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
REALTIME_MODEL = os.getenv("REALTIME_MODEL", "gpt-realtime")
VOICE = os.getenv("REALTIME_VOICE", "alloy")
PORT = int(os.getenv("PORT", 5050))
TRANSCRIPT_DIR = Path(os.getenv("TRANSCRIPT_DIR", "transcripts"))
DEFAULT_SCENARIO = next(iter(SCENARIOS))

# Trust certifi's CA bundle explicitly rather than whatever the interpreter was
# configured with. A python.org install on macOS ships with no root certificates
# until you run "Install Certificates.command", and the resulting failure looks
# like a mid-call crash rather than a setup problem.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

twilio_client = Client(
    os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
)

HANG_UP_TOOL = {
    "type": "function",
    "name": "hang_up",
    "description": (
        "End the phone call. Call this only after you have said a closing line "
        "out loud and your goal is resolved or clearly cannot be met."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Short note on why the call ended.",
            }
        },
        "required": ["reason"],
    },
}

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/media-stream")
async def media_stream(twilio_ws: WebSocket):
    await twilio_ws.accept()

    call = CallSession()
    url = f"wss://api.openai.com/v1/realtime?model={REALTIME_MODEL}"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    try:
        async with websockets.connect(
            url, additional_headers=headers, ssl=SSL_CONTEXT
        ) as openai_ws:
            pumps = [
                asyncio.create_task(call.pump_twilio_to_openai(twilio_ws, openai_ws)),
                asyncio.create_task(call.pump_openai_to_twilio(twilio_ws, openai_ws)),
            ]
            # Whichever side finishes first ends the call. Waiting for both would
            # hang when the model hangs up, because the Twilio socket then sits
            # idle until something else closes it.
            done, pending = await asyncio.wait(
                pumps, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()  # re-raise, so failures are logged not swallowed
    except Exception as error:
        print(f"Call ended abnormally: {error!r}")
    finally:
        # Transcripts are a deliverable. Never lose one to a crash mid-call.
        call.save_transcript()


class CallSession:
    """Per-call state: stream identity, barge-in bookkeeping, and transcript."""

    def __init__(self):
        self.stream_sid = None
        self.call_sid = None
        self.scenario = DEFAULT_SCENARIO
        self.turns = []

        # Barge-in bookkeeping. Twilio buffers audio we send ahead of playback,
        # so on interruption we must tell the model how much was actually heard.
        self.latest_media_ts = 0
        self.response_start_ts = None
        self.last_assistant_item = None
        self.marks = []

    @staticmethod
    def stamp(ms: int) -> str:
        seconds = ms // 1000
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def record_turn(self, speaker: str, text: str):
        """Stamp turns against Twilio's media clock, not wall time.

        Bug reports cite [mm:ss] and a reviewer checks it against the MP3, so the
        stamp has to be in the recording's own time base. Wall time would include
        dial and setup before audio began, and drift from there.

        Caveat that survives this fix: transcription completes after the speech
        it describes, so a stamp marks roughly where a turn *ended*.
        """
        text = (text or "").strip()
        if not text:
            return
        at_ms = self.latest_media_ts
        self.turns.append(
            {
                "at_ms": at_ms,
                "at": self.stamp(at_ms),
                "speaker": speaker,
                "text": text,
            }
        )
        print(f"[{self.stamp(at_ms)}] {speaker}: {text}")

    async def configure_session(self, openai_ws):
        await openai_ws.send(
            json.dumps(
                {
                    "type": "session.update",
                    "session": {
                        "type": "realtime",
                        "model": REALTIME_MODEL,
                        "output_modalities": ["audio"],
                        "instructions": build_instructions(self.scenario),
                        "audio": {
                            "input": {
                                "format": {"type": "audio/pcmu"},
                                "transcription": {"model": "whisper-1"},
                                "turn_detection": {
                                    "type": "server_vad",
                                    "threshold": 0.5,
                                    "prefix_padding_ms": 300,
                                    "silence_duration_ms": 700,
                                },
                            },
                            "output": {
                                "format": {"type": "audio/pcmu"},
                                # Per-scenario, so ten calls don't sound like one
                                # synthetic person phoning back ten times.
                                "voice": get_voice(self.scenario, VOICE),
                            },
                        },
                        "tools": [HANG_UP_TOOL],
                        "tool_choice": "auto",
                    },
                }
            )
        )

    async def pump_twilio_to_openai(self, twilio_ws, openai_ws):
        try:
            async for message in twilio_ws.iter_text():
                data = json.loads(message)
                event = data.get("event")

                if event == "start":
                    start = data["start"]
                    self.stream_sid = start["streamSid"]
                    self.call_sid = start["callSid"]
                    scenario = start.get("customParameters", {}).get("scenario")
                    if scenario not in SCENARIOS:
                        print(f"Unknown scenario {scenario!r}, using {DEFAULT_SCENARIO}")
                        scenario = DEFAULT_SCENARIO
                    self.scenario = scenario
                    print(f"Stream started: {self.scenario} ({self.call_sid})")
                    await self.configure_session(openai_ws)

                elif event == "media":
                    self.latest_media_ts = int(data["media"]["timestamp"])
                    await openai_ws.send(
                        json.dumps(
                            {
                                "type": "input_audio_buffer.append",
                                "audio": data["media"]["payload"],
                            }
                        )
                    )

                elif event == "mark":
                    if self.marks:
                        self.marks.pop(0)

                elif event == "stop":
                    break
        except WebSocketDisconnect:
            print("Twilio disconnected.")
        finally:
            if openai_ws.state.name == "OPEN":
                await openai_ws.close()

    async def pump_openai_to_twilio(self, twilio_ws, openai_ws):
        try:
            async for raw in openai_ws:
                event = json.loads(raw)
                kind = event.get("type")

                if kind == "response.output_audio.delta":
                    await self.forward_audio(twilio_ws, event)

                elif kind == "input_audio_buffer.speech_started":
                    await self.handle_barge_in(twilio_ws, openai_ws)

                elif kind == "conversation.item.input_audio_transcription.completed":
                    self.record_turn("CLINIC_AGENT", event.get("transcript"))

                elif kind == "response.output_audio_transcript.done":
                    self.record_turn("PATIENT_BOT", event.get("transcript"))

                elif kind == "response.function_call_arguments.done":
                    if event.get("name") == "hang_up":
                        await self.hang_up(event)
                        break

                elif kind == "error":
                    print("Realtime API error:", json.dumps(event))
        except websockets.exceptions.ConnectionClosed:
            print("Realtime connection closed.")

    async def forward_audio(self, twilio_ws, event):
        await twilio_ws.send_json(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": event["delta"]},
            }
        )

        item_id = event.get("item_id")
        if item_id and item_id != self.last_assistant_item:
            self.last_assistant_item = item_id
            self.response_start_ts = self.latest_media_ts

        await twilio_ws.send_json(
            {
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": "chunk"},
            }
        )
        self.marks.append("chunk")

    async def handle_barge_in(self, twilio_ws, openai_ws):
        """Clinic agent started talking while we were mid-sentence."""
        if not (self.marks and self.response_start_ts is not None):
            return

        heard_ms = self.latest_media_ts - self.response_start_ts
        if self.last_assistant_item and heard_ms > 0:
            await openai_ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": self.last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": heard_ms,
                    }
                )
            )

        await twilio_ws.send_json({"event": "clear", "streamSid": self.stream_sid})
        self.marks.clear()
        self.last_assistant_item = None
        self.response_start_ts = None

    async def hang_up(self, event):
        try:
            args = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        print(f"Bot ending call: {args.get('reason', 'no reason given')}")

        # Let the closing line finish playing out of Twilio's buffer.
        await asyncio.sleep(2)
        if not self.call_sid:
            return
        try:
            twilio_client.calls(self.call_sid).update(status="completed")
        except Exception as error:
            # Falling through here just means Twilio's time_limit ends the call
            # instead. Not worth losing the transcript over.
            print(f"Could not end call via API: {error!r}")

    def save_transcript(self):
        if not self.turns:
            print("No transcript captured.")
            return

        TRANSCRIPT_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        stem = TRANSCRIPT_DIR / f"{stamp}-{self.scenario}"

        # Recognition of the two sides completes independently, so events can
        # arrive out of order. Sort by audio position to restore the real sequence.
        turns = sorted(self.turns, key=lambda t: t["at_ms"])

        stem.with_suffix(".json").write_text(
            json.dumps(
                {
                    "call_sid": self.call_sid,
                    "scenario": self.scenario,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "turns": turns,
                },
                indent=2,
            )
        )

        lines = [f"Scenario: {self.scenario}", f"Call SID: {self.call_sid}", ""]
        lines += [f"[{t['at']}] {t['speaker']}: {t['text']}" for t in turns]
        stem.with_suffix(".txt").write_text("\n".join(lines) + "\n")

        print(f"Saved transcript: {stem}.txt")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
