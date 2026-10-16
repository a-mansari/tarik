"""Build workflows/tarik-modification.json: changing an existing dossier's fields.

The email's sender must have the dossier (the agent's own chat has no sender), then update_dossier
changes only the fields given, and only to values the client wrote. The run's summary is the answer."""

from steps import CABINET_INPUT, CALL, SENDER, Workflow

FIELDS = [
    "name",
    "folder_type",
    "ice",
    "if_number",
    "rc",
    "patente",
    "tsc",
    "capital",
    "cnss",
    "street",
    "street2",
    "city",
    "zip",
]
UPDATE = "Modification (update_dossier)"

flow = Workflow("tarik-modification")
flow.trigger(
    session_id="string",
    dossier_id="number",
    **dict.fromkeys(FIELDS, "string"),
    skip_ice_if_check="boolean",
    client_message="string",
    **CABINET_INPUT,
)
sender = flow.check_sender([220, 300], [440, 300])
arguments = ", ".join(f"{field}: {CALL}.{field} ?? ''" for field in FIELDS)
flow.mcp(
    UPDATE,
    "update_dossier",
    (
        f"{{ dossier_id: {CALL}.dossier_id, {arguments}, skip_ice_if_check: {CALL}.skip_ice_if_check === true, "
        f"client_message: {CALL}.client_message || '', session_id: {CALL}.session_id }}"
    ),
    [660, 300],
)
flow.connect("Appel", SENDER)
flow.connect(sender, UPDATE, 0)
flow.to_record(UPDATE, 0, 1)
flow.record_and_summary([900, 300], [220, 600])
flow.save("TarikModif000001", "Tarik - Modification")
