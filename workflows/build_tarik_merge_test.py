"""Build workflows/tarik-merge-test.json: "Tarik - Test Merge", which checks n8n still runs the node made by
Workflow.at_the_end after every other one (tests/n8n/test_at_the_end.py runs it; re-run after each n8n upgrade).

Branches, an item per number, a step failing on some, a second level, and the last node at the top of the editor,
where n8n's order of branches alone would run it first."""

from steps import Workflow

flow = Workflow("tarik-merge-test")
flow.node("Appel", "n8n-nodes-base.manualTrigger", 1, [0, 300], {})
flow.code("Un element par nombre", [220, 300], "return [1, 2, 3, 4, 5].map(n => ({ json: { n } }));")
flow.route("Pair ?", "$json.n % 2", 2, [440, 300])
flow.code("Pair", [660, 200], "for (let i = 0; i < 2e7; i++);\nreturn $input.all();")
flow.node(
    "Impair",
    "n8n-nodes-base.code",
    2,
    [660, 400],
    {"jsCode": "throw new Error('impair');"},
    onError="continueErrorOutput",
)
flow.code("Apres l'erreur", [880, 500], "for (let i = 0; i < 2e7; i++);\nreturn $input.all();")
flow.code("Deuxieme niveau", [880, 200], "return $input.all();")
flow.code("Dernier", [300, -400], "return [{ json: { ran: true } }];")
flow.connect("Appel", "Un element par nombre")
flow.connect("Un element par nombre", "Pair ?")
flow.connect("Pair ?", "Pair", 0)
flow.connect("Pair ?", "Impair", 1)
flow.connect("Impair", "Apres l'erreur", 1)
flow.connect("Pair", "Deuxieme niveau")
flow.at_the_end("Dernier", [100, -400])
flow.save(
    "TarikMergeTest01",
    "Tarik - Test Merge",
    {"executionOrder": "v1", "callerPolicy": "workflowsFromSameOwner", "availableInMCP": True},
)
