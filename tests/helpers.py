import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "client.json"
CRM = ROOT / "samples" / "crm.json"


def client_raw():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def profile_raw():
    return json.loads(CRM.read_text(encoding="utf-8"))["client_demo_001"]
