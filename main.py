"""Places test calls to the Pretty Good AI assessment line.

Run the bridge first (see README), then:
    python main.py list
    python main.py call new_knee
    python main.py call all
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, VoiceResponse

from scenarios import SCENARIOS

load_dotenv()

# The assessment line, hard-coded on purpose. Everything dials through
# place_call(), which refuses any other destination, so a mistyped env var
# cannot put a call through to a stranger.
TEST_NUMBER = "+18054398008"

# Cost guard: a wedged call can't bill past this.
MAX_CALL_SECONDS = 300

PORT = int(os.getenv("PORT", 5050))
RECORDING_DIR = Path(os.getenv("RECORDING_DIR", "recordings"))
TRANSCRIPT_DIR = Path(os.getenv("TRANSCRIPT_DIR", "transcripts"))


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        sys.exit(f"Missing {name}. Copy .env.example to .env and fill it in.")
    return value


def strip_scheme(url: str) -> str:
    return url.removeprefix("https://").removeprefix("http://").rstrip("/")


def discover_ngrok_host() -> str | None:
    """Ask the running ngrok for its own hostname.

    Free-tier ngrok issues a new host every restart, so a hand-copied
    PUBLIC_HOST goes stale constantly - and a stale host fails as silence on a
    connected call, which is an expensive way to find out.
    """
    try:
        response = requests.get("http://localhost:4040/api/tunnels", timeout=2)
        tunnels = response.json().get("tunnels", [])
    except (requests.RequestException, ValueError):
        return None

    for tunnel in tunnels:
        url = tunnel.get("public_url", "")
        if url.startswith("https://"):
            return strip_scheme(url)
    return None


def public_host() -> str:
    """The bare host for the wss:// stream URL. Explicit config wins."""
    configured = strip_scheme((os.getenv("PUBLIC_HOST") or "").strip())
    if configured and "xxxx" not in configured:
        return configured

    discovered = discover_ngrok_host()
    if discovered:
        print(f"Using running ngrok tunnel: {discovered}")
        return discovered

    sys.exit(
        "No PUBLIC_HOST set and no ngrok running.\n"
        "Start the tunnel:  ngrok http 5050"
    )


def check_bridge_running():
    """Fail before spending money if the bridge isn't up to answer the stream."""
    try:
        requests.get(f"http://localhost:{PORT}/health", timeout=3).raise_for_status()
    except requests.RequestException:
        sys.exit(
            f"No bridge on localhost:{PORT}.\n"
            "Start it first:  python server.py   (and make sure ngrok points at it)"
        )


def build_twiml(scenario: str) -> str:
    """Inline TwiML, so outbound calls need no public webhook route at all.

    server.py reads the scenario back out of start.customParameters.
    """
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=f"wss://{public_host()}/media-stream")
    stream.parameter(name="scenario", value=scenario)
    response.append(connect)
    return str(response)


def place_call(client: Client, scenario: str) -> str:
    # The destination is deliberately not configurable - not by env var, not by
    # flag. There is no input path that can point this at another number.
    call = client.calls.create(
        to=TEST_NUMBER,
        from_=require_env("TWILIO_FROM_NUMBER"),
        twiml=build_twiml(scenario),
        record=True,
        recording_channels="dual",
        time_limit=MAX_CALL_SECONDS,
    )
    print(f"Dialling {TEST_NUMBER} as '{scenario}' -> {call.sid}")
    return call.sid


def wait_for_call(client: Client, call_sid: str) -> str:
    """Block until Twilio reports the call reached a terminal state."""
    terminal = {"completed", "busy", "failed", "no-answer", "canceled"}
    while True:
        status = client.calls(call_sid).fetch().status
        if status in terminal:
            print(f"Call {status}.")
            return status
        time.sleep(3)


def download_recording(client: Client, call_sid: str, stem: str, wait: bool = True):
    """Save a call's recording as recordings/<stem>.mp3.

    The stem matches the transcript's filename so a reviewer can pair audio with
    text at a glance - the bug report cites both.
    """
    account_sid = require_env("TWILIO_ACCOUNT_SID")
    auth_token = require_env("TWILIO_AUTH_TOKEN")

    delays = (3, 5, 8, 13, 21) if wait else (0,)
    for delay in delays:
        recordings = client.recordings.list(call_sid=call_sid)
        if recordings:
            break
        time.sleep(delay)
    else:
        print(f"  no recording yet for {stem}")
        return False

    RECORDING_DIR.mkdir(exist_ok=True)
    url = f"https://api.twilio.com{recordings[0].uri.replace('.json', '.mp3')}"
    response = requests.get(url, auth=(account_sid, auth_token), timeout=60)
    response.raise_for_status()

    path = RECORDING_DIR / f"{stem}.mp3"
    path.write_bytes(response.content)
    print(f"  saved {path}")
    return True


def transcripts_missing_audio():
    """Pair each transcript with its recording, yielding the ones not downloaded."""
    for path in sorted(TRANSCRIPT_DIR.glob("*.json")):
        if (RECORDING_DIR / f"{path.stem}.mp3").exists():
            continue
        call_sid = json.loads(path.read_text()).get("call_sid")
        if call_sid:
            yield call_sid, path.stem


def run_scenario(client: Client, scenario: str):
    call_sid = place_call(client, scenario)
    status = wait_for_call(client, call_sid)
    if status != "completed":
        print(f"Call ended as '{status}' - no recording to fetch.")
        return

    # The bridge names the transcript, so look it up rather than guessing.
    for sid, stem in transcripts_missing_audio():
        if sid == call_sid:
            download_recording(client, call_sid, stem)
            return
    print("Transcript not found yet; run 'python main.py fetch' shortly.")


def cmd_list(_args):
    width = max(len(key) for key in SCENARIOS)
    for key, scenario in SCENARIOS.items():
        print(f"  {key:<{width}}  {scenario['label']}")


def cmd_call(args):
    check_bridge_running()
    client = Client(require_env("TWILIO_ACCOUNT_SID"), require_env("TWILIO_AUTH_TOKEN"))

    if args.scenario == "all":
        targets = list(SCENARIOS)
    elif args.scenario in SCENARIOS:
        targets = [args.scenario]
    else:
        sys.exit(f"Unknown scenario '{args.scenario}'. Try: python main.py list")

    for index, scenario in enumerate(targets, start=1):
        print(f"\n=== [{index}/{len(targets)}] {scenario} ===")
        try:
            run_scenario(client, scenario)
        except TwilioRestException as error:
            # One bad call shouldn't abandon the other nine.
            print(f"Twilio error on '{scenario}': {error.msg}")
        if index < len(targets):
            time.sleep(10)


def cmd_fetch(_args):
    """Download any recording whose transcript exists but whose audio does not."""
    client = Client(require_env("TWILIO_ACCOUNT_SID"), require_env("TWILIO_AUTH_TOKEN"))
    pending = list(transcripts_missing_audio())
    if not pending:
        print("Every transcript already has its recording.")
        return
    print(f"Fetching {len(pending)} recording(s):")
    for call_sid, stem in pending:
        download_recording(client, call_sid, stem, wait=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="show available scenarios").set_defaults(
        func=cmd_list
    )
    subparsers.add_parser("fetch", help="download any missing recordings").set_defaults(
        func=cmd_fetch
    )

    call_parser = subparsers.add_parser("call", help="place a call")
    call_parser.add_argument("scenario", help="scenario name, or 'all'")
    call_parser.set_defaults(func=cmd_call)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
