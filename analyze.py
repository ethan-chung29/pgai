"""First-pass bug triage over call transcripts.

Produces *candidates*, not conclusions. BUGS.md is written by hand from these -- the
point of this script is to narrow the search space, especially for contradictions
that span calls and are impractical to spot by reading ten transcripts in a row.

    python analyze.py

Three passes:
  A  per-call triage            LLM judgement, one call per transcript
  B  cross-call consistency     LLM extracts, code diffs
  C  readback accuracy          LLM extracts, code compares to the known record
"""

import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from scenarios import SCENARIOS

load_dotenv()

MODEL = os.getenv("ANALYSIS_MODEL", "gpt-4o")
TRANSCRIPT_DIR = Path(os.getenv("TRANSCRIPT_DIR", "transcripts"))
ANALYSIS_DIR = Path("analysis")

# Fields compared across calls. The clinic name is separate from the hours so a
# wrong practice name shows up as its own contradiction rather than as five
# differently-worded schedules.
CONSISTENCY_FIELDS = [
    "practice_name",
    "office_hours",
    "weekend_availability",
    "location",
    "insurance_accepted",
]

client = OpenAI()

# The transcripts come from whisper running on 8kHz phone audio, which misreads
# things. A transcription error blamed on the clinic's agent is a false positive
# that would discredit the whole bug report, so the rules below are deliberately
# strict about what counts as evidence.
TRIAGE_RULES = """You are auditing a call between a patient and a medical clinic's AI \
receptionist. Report only defects in the RECEPTIONIST's behaviour.

Report only:
- The receptionist contradicted itself, or violated a policy it had just stated.
- It committed to something impossible or unsafe.
- It missed a clinically urgent situation and treated the call as routine.
- It captured a detail wrong, or dead-ended the caller without resolving anything.

Never report:
- Phrasing, tone, filler words, punctuation, or speech-synthesis artifacts.
- Anything about the PATIENT's behaviour. The patient is a test script.
- Guesses about backend systems that the transcript cannot show.
- Anything resting on a garbled line. These transcripts contain transcription
  errors; if a finding depends on exact wording that looks garbled, skip it.

Every finding must quote the receptionist verbatim. No quote means no finding.
An empty list is a perfectly good answer.

Severity:
  high    - patient safety, or an impossible/unsafe commitment
  medium  - task failure, wrong data captured, self-contradiction
  low     - real but harmless

Return JSON: {"findings": [{"title", "severity", "at", "quote", "problem", "expected"}]}
where "at" is the [mm:ss] stamp of the quoted turn."""


def load_transcripts() -> list[dict]:
    if not TRANSCRIPT_DIR.exists():
        sys.exit(f"No {TRANSCRIPT_DIR}/ directory. Place some calls first.")
    files = sorted(TRANSCRIPT_DIR.glob("*.json"))
    if not files:
        sys.exit(f"No transcripts in {TRANSCRIPT_DIR}/.")
    return [{**json.loads(f.read_text()), "file": f.name} for f in files]


def render(transcript: dict) -> str:
    return "\n".join(
        f"[{t['at']}] {t['speaker']}: {t['text']}" for t in transcript["turns"]
    )


def ask_json(system: str, user: str) -> dict:
    response = client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return json.loads(response.choices[0].message.content)


def pass_a_triage(transcripts: list[dict]) -> list[dict]:
    """Per-call judgement."""
    findings = []
    for transcript in transcripts:
        print(f"  triaging {transcript['file']}")
        result = ask_json(TRIAGE_RULES, render(transcript))
        for finding in result.get("findings", []):
            findings.append(
                {
                    **finding,
                    "source": "per-call",
                    "scenario": transcript["scenario"],
                    "transcript": transcript["file"],
                }
            )
    return findings


EXTRACT_RULES = """Extract only what the RECEPTIONIST explicitly stated in this call.

Return JSON with these keys, using null for anything not stated:
  practice_name, office_hours, weekend_availability, location,
  insurance_accepted, patient_name_readback, patient_dob_readback,
  patient_phone_readback

Return the bare value and nothing else. Strip every carrier phrase: "I have your
name as Maria Alvarez" becomes "Maria Alvarez". Never include the words the
receptionist wrapped around the value.

Normalise so that two calls saying the same thing produce identical strings:
  practice_name    the clinic name only, e.g. "Pivot Point Orthopedics"
  office_hours     "Mon 9:00-16:00; Tue 9:00-16:00; Wed 12:00-19:00" - 24-hour,
                   three-letter days, ascending, semicolon separated, closed
                   days omitted
  location         street address only, no clinic name
  weekend_availability   exactly "open" or "closed"
  patient_dob_readback   "Month D, YYYY" e.g. "March 4, 1991" - matching the
                         form the patient records use, so a comparison is fair
  patient_phone_readback digits only, no punctuation
  patient_name_readback  the name exactly as spoken, no normalising

If the receptionist never stated something, the value is null. Never infer or
guess a value that was not said aloud."""


def extract_claims(transcripts: list[dict]) -> dict[str, dict]:
    claims = {}
    for transcript in transcripts:
        print(f"  extracting {transcript['file']}")
        claims[transcript["file"]] = ask_json(EXTRACT_RULES, render(transcript))
    return claims


def pass_b_consistency(claims: dict[str, dict]) -> list[dict]:
    """Cross-call contradictions. The LLM extracted; the diffing is plain code."""
    findings = []

    for field in CONSISTENCY_FIELDS:
        by_answer = defaultdict(list)
        for filename, claim in claims.items():
            value = claim.get(field)
            if value:
                by_answer[str(value).strip()].append(filename)

        if len(by_answer) > 1:
            findings.append(
                {
                    "title": f"Receptionist gave different answers for {field}",
                    "severity": "medium",
                    "source": "cross-call",
                    "problem": (
                        f"Across {sum(len(v) for v in by_answer.values())} calls the "
                        f"receptionist stated {len(by_answer)} different values for "
                        f"{field}. At most one can be correct."
                    ),
                    "expected": f"The same {field} on every call.",
                    "answers": {a: sorted(f) for a, f in by_answer.items()},
                }
            )
    return findings


def pass_c_readback(transcripts: list[dict], claims: dict[str, dict]) -> list[dict]:
    """Did the receptionist repeat our details back correctly?

    Each scenario has a fixed patient record, so this is a string comparison
    rather than a judgement call.
    """
    checks = [
        ("patient_name_readback", "full_name", "name"),
        ("patient_dob_readback", "dob", "date of birth"),
        ("patient_phone_readback", "phone", "callback number"),
    ]
    findings = []

    for transcript in transcripts:
        record = SCENARIOS.get(transcript["scenario"], {}).get("patient", {})
        claim = claims.get(transcript["file"], {})

        for claim_key, record_key, label in checks:
            stated, truth = claim.get(claim_key), record.get(record_key)
            if not stated or not truth:
                continue
            if normalise(stated) != normalise(truth):
                findings.append(
                    {
                        "title": f"Receptionist read back the wrong {label}",
                        "severity": "medium",
                        "source": "readback",
                        "scenario": transcript["scenario"],
                        "transcript": transcript["file"],
                        "problem": f"Caller gave '{truth}'; receptionist said '{stated}'.",
                        "expected": f"The {label} repeated back as '{truth}'.",
                    }
                )
    return findings


def normalise(value: str) -> str:
    """Loose match. Punctuation, casing, and "4th" vs "4" are not bugs -- only a
    genuinely different value is."""
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", str(value).lower())
    return "".join(c for c in text if c.isalnum())


def write_consistency_table(claims: dict[str, dict]):
    lines = ["# Cross-call consistency", "",
             "| Call | " + " | ".join(CONSISTENCY_FIELDS) + " |"]
    lines.append("| --- " * (len(CONSISTENCY_FIELDS) + 1) + "|")

    for filename in sorted(claims):
        cells = [str(claims[filename].get(f) or "-").replace("|", "/")
                 for f in CONSISTENCY_FIELDS]
        lines.append(f"| {filename} | " + " | ".join(cells) + " |")

    (ANALYSIS_DIR / "consistency.md").write_text("\n".join(lines) + "\n")


def main():
    transcripts = load_transcripts()
    print(f"Loaded {len(transcripts)} transcripts.\n")

    ANALYSIS_DIR.mkdir(exist_ok=True)

    print("Pass A - per-call triage")
    findings = pass_a_triage(transcripts)

    print("\nPass B/C - extracting stated claims")
    claims = extract_claims(transcripts)

    findings += pass_b_consistency(claims)
    findings += pass_c_readback(transcripts, claims)

    write_consistency_table(claims)
    (ANALYSIS_DIR / "findings.json").write_text(json.dumps(findings, indent=2))

    by_severity = defaultdict(int)
    for finding in findings:
        by_severity[finding.get("severity", "unknown")] += 1

    print(f"\n{len(findings)} candidates: {dict(by_severity)}")
    print(f"Wrote {ANALYSIS_DIR}/findings.json and {ANALYSIS_DIR}/consistency.md")
    print("\nThese are candidates. Curate BUGS.md by hand, and check any quote")
    print("against the audio before citing it.")


if __name__ == "__main__":
    main()
