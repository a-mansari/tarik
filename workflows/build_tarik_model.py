"""Build workflows/tarik-model.json: "Tarik - Model", the model's door for tarik-mcp's tools (ask_model).

The tools never hold the model's key: they post their request (the Anthropic message format) to this webhook,
and n8n sends it on with the agents' own credential, the one set in n8n's UI. Changing the provider: its
credential and its address in "Modele", the agents' model in n8n. Nothing is saved of a run: the requests hold
the clients' pages."""

from steps import Workflow

flow = Workflow("tarik-model")
flow.node(
    "Demande",
    "n8n-nodes-base.webhook",
    2,
    [0, 300],
    {
        "httpMethod": "POST",
        "path": "tarik-model",
        "responseMode": "responseNode",
        "options": {},
        # Only tarik-mcp: its X-Tarik-Webhook header carries the secret both share (Vault MAIL_WEBHOOK_SECRET).
        "authentication": "headerAuth",
    },
    webhookId="8b6f2e41-3c7a-4d95-a1e8-6f0b9c2d4e73",
    credentials={"httpHeaderAuth": {"id": "TarikMailWebhook1", "name": "Tarik mail webhook"}},
)
flow.node(
    "Modele",
    "n8n-nodes-base.httpRequest",
    4.2,
    [220, 300],
    {
        "method": "POST",
        # A fixed address: the request never chooses where the key goes.
        "url": "https://api.minimax.io/anthropic/v1/messages",
        "authentication": "predefinedCredentialType",
        "nodeCredentialType": "minimaxApi",
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "anthropic-version", "value": "2023-06-01"}]},
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify($json.body) }}",
        "options": {
            "timeout": 290000,  # under ask_model's 300 s (MODEL_TIMEOUT)
            # The provider's status and answer go back as they are: ask_model reads its errors.
            "response": {"response": {"fullResponse": True, "neverError": True}},
        },
    },
    credentials={"minimaxApi": {"id": "MiniMaxNative001", "name": "MiniMax"}},
)
flow.node(
    "Reponse",
    "n8n-nodes-base.respondToWebhook",
    1.1,
    [440, 300],
    {
        "respondWith": "json",
        "responseBody": "={{ JSON.stringify($json.body) }}",
        "options": {"responseCode": "={{ $json.statusCode }}"},
    },
)
flow.connect("Demande", "Modele")
flow.connect("Modele", "Reponse")
flow.save(
    "TarikModel000001",
    "Tarik - Model",
    {
        "executionOrder": "v1",
        "saveDataSuccessExecution": "none",
        "saveDataErrorExecution": "none",
        "saveManualExecutions": False,
    },
)
