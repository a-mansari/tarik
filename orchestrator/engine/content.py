"""What Tarik says and is told, kept apart from the code so the UI can show and edit it: the agent's definition
(content/agent.json: model, instructions, tools' descriptions, read tools) and the UI's plain words for each workflow,
step and status (content/labels.json). Read once per process."""

import json
from pathlib import Path

CONTENT = Path(__file__).resolve().parents[1] / "content"
AGENT = json.loads((CONTENT / "agent.json").read_text())
LABELS = json.loads((CONTENT / "labels.json").read_text())
