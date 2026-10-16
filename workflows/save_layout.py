"""Save the workflows' node positions, as arranged in n8n's editor (their current drafts), to layout.json: the
builders apply it, so a rebuild keeps the layout. Run on the host, then rebuild the workflows:
python3 workflows/save_layout.py"""

import json
import subprocess
from pathlib import Path

HERE = Path(__file__).parent
QUERY = "select json_object_agg(id, nodes) from workflow_entity where id like 'Tarik%'"


def main():
    """Read every Tarik workflow's nodes from n8n's database and write their positions, by workflow id and node name."""
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        f'psql -U "$POSTGRES_USER" -d "$N8N_POSTGRES_DB" -At -c "{QUERY}"',
    ]
    nodes = json.loads(subprocess.run(command, cwd=HERE.parent, check=True, capture_output=True, text=True).stdout)
    layout = {workflow: {node["name"]: node["position"] for node in found} for workflow, found in sorted(nodes.items())}
    (HERE / "layout.json").write_text(json.dumps(layout, indent=2, ensure_ascii=False) + "\n")
    print(", ".join(f"{workflow}: {len(positions)} nodes" for workflow, positions in layout.items()))


if __name__ == "__main__":
    main()
