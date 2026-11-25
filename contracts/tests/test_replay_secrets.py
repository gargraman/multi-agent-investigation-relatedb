"""ADR-006 E7/E8, S2 NFR-007: replay recordings never contain an API key.

Scans every file under ``data/replay/`` (absent today: recordings land at integration) for
OpenAI-style ``sk-`` key patterns and for header/auth material. Passes trivially when empty.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
REPLAY_ROOT = REPO_ROOT / "data" / "replay"

KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")  # look-behind: not "risk-..."
AUTH_PATTERN = re.compile(r"(?i)\"(authorization|api[_-]?key|x-api-key)\"\s*:")


def scan(root: Path) -> list[str]:
    hits: list[str] = []
    if not root.is_dir():
        return hits
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        text = path.read_text("utf-8", errors="replace")
        if KEY_PATTERN.search(text) or "sk-proj-" in text:
            hits.append(f"{path.relative_to(root)}: sk- key pattern")
        if AUTH_PATTERN.search(text):
            hits.append(f"{path.relative_to(root)}: auth header/key field")
    return hits


def test_no_api_keys_in_replay_recordings() -> None:
    assert scan(REPLAY_ROOT) == []


def test_scanner_catches_planted_key(tmp_path: Path) -> None:
    rec = tmp_path / "sigint-agent" / "prompt-injection" / "abc.json"
    rec.parent.mkdir(parents=True)
    rec.write_text('{"response": {"text": "sk-' + "A" * 24 + '"}}')
    (tmp_path / "ok.json").write_text('{"key_material": {"scenario_id": "x"}}')
    assert scan(tmp_path) == ["sigint-agent/prompt-injection/abc.json: sk- key pattern"]
    rec.write_text('{"headers": {"Authorization": "Bearer x"}}')
    assert scan(tmp_path) == ["sigint-agent/prompt-injection/abc.json: auth header/key field"]


def test_scan_ignores_words_ending_in_sk(tmp_path: Path) -> None:
    rec = tmp_path / "sigint-agent" / "baseline-corroboration" / "k.json"
    rec.parent.mkdir(parents=True)
    rec.write_text(
        '{"response": {"statement": "A high-risk-contact pattern; desk-officer review."}}'
    )
    assert scan(tmp_path) == []
