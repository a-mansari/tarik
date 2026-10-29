"""The Emails graph's routing: the threads claimed, each in its own run (Fil d'emails): acknowledged, answered,
the refused sender's manager told, the reply sent, the thread finished; a follow-up resumes Traitement then Dossier."""

import asyncio

import pytest

from engine import emails, runs
from engine.calls import ToolFailure
from engine.tools.invoices.pipeline import NOTHING

THREAD = {
    "thread_id": "<m1@x>",
    "session_id": "s1",
    "follow_up": False,
    "subject": "Re: Factures",
    "message": "Voici les factures.",
    "files": [],
    "to": "chef@cabinet.ma",
    "in_reply_to": "<m1@x>",
    "references": "<m1@x>",
    "message_ids": ["<m1@x>"],
}

NODES = {"traitement": "suite_documents", "dossier": "suite_dossiers"}  # the follow-up step each workflow runs in


@pytest.fixture
def thread(steps, monkeypatch):
    """The thread's tools faked (steps), the agent replying, the resumed workflows saying what ``resumed`` holds."""
    asked, resumed = [], {"traitement": "Factures traitees.", "dossier": "Dossier termine."}

    async def answered(cabinet, session, message, files, agent, skipped):
        asked.append(message)
        await asyncio.sleep(resumed.get("seconds", 0))
        if isinstance(resumed.get("agent"), Exception):
            raise resumed["agent"]
        return "Bien recu."

    async def nested(name, given, cabinet, session, thread=None, node=None):
        steps.calls.append((f"{name} resume", given))
        if name == "fil":
            return await emails.FIL.ainvoke(given)
        if isinstance(resumed[name], Exception):
            raise resumed[name]
        assert given["call"]["mode"] == "resume" and node == NODES[name]
        return {"message": resumed[name]}

    monkeypatch.setattr(emails, "call", steps.call)
    monkeypatch.setattr(emails, "answered", answered)

    async def within(cabinet, function, argument):  # the setting read, or the progress email sent as a step
        if function is emails.behaviour:
            return resumed.get(argument, 0)
        return await steps.call(cabinet, function.__name__, thread_id=argument)

    monkeypatch.setattr(emails, "within", within)
    monkeypatch.setattr(runs, "nested", nested)
    steps.asked, steps.resumed = asked, resumed
    return steps


def fil(steps, **changed):
    return asyncio.run(emails.FIL.ainvoke({"cabinet": "c1", "thread": {**THREAD, **changed}, "done": []}))


@pytest.mark.functionality
def test_a_new_thread_is_acknowledged_answered_sent_then_finished(thread):
    thread.answer("thread_refusal", {"refused": False})
    thread.answer("send_email", {"message_id": "<r1@x>"})
    out = fil(thread)
    assert thread.names == ["acknowledge_thread", "thread_refusal", "send_email", "finish_thread"]
    assert thread.asked == ["Objet : Factures\n\nVoici les factures."]
    assert thread.args("send_email")[0]["body"] == "Bien recu." and out["sent"] == "Envoyé à chef@cabinet.ma."
    finished = thread.args("finish_thread")[0]
    assert (finished["sent"], finished["reason"], finished["sent_id"]) == (True, "", "<r1@x>")
    assert out["done"] == [{"thread_id": "<m1@x>", "sent": True, "reason": ""}]


@pytest.mark.communication
def test_an_acknowledgement_not_sent_leaves_the_answer_to_go_on(thread):
    thread.answer("acknowledge_thread", ToolFailure("hors liste"))
    thread.answer("thread_refusal", {"refused": False})
    out = fil(thread)
    assert out["acknowledged"] == "Pas envoyé : hors liste"
    assert thread.names == ["acknowledge_thread", "thread_refusal", "send_email", "finish_thread"]


@pytest.mark.communication
def test_a_long_answer_tells_the_client_it_is_still_going_every_few_minutes(thread):
    thread.resumed.update(progress=0.001, seconds=0.2)  # 0.06 s between progress emails
    thread.answer("thread_refusal", {"refused": False})
    out = fil(thread)
    assert thread.names.count("send_progress") >= 2 and thread.args("send_progress")[0] == {"thread_id": "<m1@x>"}
    assert thread.names[-3:] == ["thread_refusal", "send_email", "finish_thread"] and out["sent"]


@pytest.mark.communication
def test_no_progress_email_when_the_cabinet_set_none_or_one_fails(thread):
    thread.resumed["seconds"] = 0.05
    thread.answer("thread_refusal", {"refused": False})
    fil(thread)
    assert "send_progress" not in thread.names
    thread.calls.clear()
    thread.resumed["progress"] = 0.0005
    thread.answer("send_progress", ToolFailure("hors liste"))
    assert fil(thread)["sent"] and "send_progress" in thread.names  # the reply still sent


@pytest.mark.functionality
def test_a_setting_not_read_still_lets_the_agent_answer(thread, monkeypatch):
    async def down(cabinet, function, argument):
        raise RuntimeError("postgres down")

    monkeypatch.setattr(emails, "within", down)
    thread.answer("thread_refusal", {"refused": False})
    assert fil(thread)["sent"]


@pytest.mark.functionality
def test_an_agent_that_cannot_answer_sets_the_thread_aside(thread):
    thread.resumed["agent"] = RuntimeError("model down")
    out = fil(thread)
    assert thread.names == ["acknowledge_thread", "finish_thread"]
    assert out["done"] == [
        {
            "thread_id": "<m1@x>",
            "sent": False,
            "reason": "Je n'ai pas pu répondre pour le moment. J'ai mis ce fil de côté, sans réponse au client.",
        }
    ]


@pytest.mark.security
def test_a_refused_sender_gets_nothing_and_their_manager_is_told(thread):
    found = {"refused": True, "sender": "x@gmail.com", "subject": "Factures", "reason": "pas du cabinet"}
    thread.answer("thread_refusal", found)
    out = fil(thread)
    assert thread.names == ["acknowledge_thread", "thread_refusal", "notify_manager"]
    assert thread.args("notify_manager") == [
        {"sender": "x@gmail.com", "subject": "Factures", "reason": "pas du cabinet"}
    ]
    assert "sent" not in out and out["done"] == []


@pytest.mark.security
def test_a_manager_not_told_still_leaves_the_sender_without_a_reply(thread):
    thread.answer("thread_refusal", {"refused": True, "sender": "x", "subject": "y", "reason": "z"})
    thread.answer("notify_manager", ToolFailure("aucun responsable"))
    out = fil(thread)
    assert out["manager"] == "Pas prévenu : aucun responsable" and "send_email" not in thread.names


@pytest.mark.security
@pytest.mark.parametrize(
    "failure, reason", [(ToolFailure("illisible"), "illisible"), (KeyError(1), "Erreur (KeyError).")]
)
def test_a_refusal_not_read_ends_the_thread_unanswered(thread, failure, reason):
    thread.answer("thread_refusal", failure)
    out = fil(thread)
    assert thread.names == ["acknowledge_thread", "thread_refusal", "finish_thread"]
    assert out["done"] == [{"thread_id": "<m1@x>", "sent": False, "reason": reason}]


@pytest.mark.functionality
def test_a_reply_not_sent_finishes_the_thread_with_why(thread):
    thread.answer("thread_refusal", {"refused": False})
    thread.answer("send_email", ToolFailure("SMTP refuse"))
    out = fil(thread)
    assert thread.names[-2:] == ["send_email", "finish_thread"]
    assert out["done"] == [{"thread_id": "<m1@x>", "sent": False, "reason": "SMTP refuse"}]


@pytest.mark.functionality
def test_a_follow_up_resumes_traitement_then_dossier_and_sends_both_summaries(thread):
    out = fil(thread, follow_up=True)
    assert thread.names == ["traitement resume", "dossier resume", "send_email", "finish_thread"]
    opening = "J'ai poursuivi le traitement de vos documents. Voici où il en est.\n\n"
    assert out["reply"] == opening + "Factures traitees.\n\nDossier termine."
    assert out["summaries"] == ["Factures traitees."] and out["done"][0]["sent"] is True


@pytest.mark.communication
def test_a_follow_up_leaves_out_a_workflow_that_had_nothing_to_resume(thread):
    thread.resumed["traitement"] = NOTHING
    out = fil(thread, follow_up=True)
    opening = "J'ai poursuivi le traitement de vos documents. Voici où il en est.\n\n"
    assert out["reply"] == opening + "Dossier termine." and out["summaries"] == []


@pytest.mark.functionality
def test_an_error_resuming_traitement_ends_the_thread_without_running_dossier(thread):
    thread.resumed["traitement"] = ToolFailure("base indisponible")
    out = fil(thread, follow_up=True)
    assert thread.names == ["traitement resume", "finish_thread"]
    assert out["done"] == [{"thread_id": "<m1@x>", "sent": False, "reason": "base indisponible"}]


@pytest.mark.functionality
def test_an_error_resuming_dossier_ends_the_thread_unsent(thread):
    thread.resumed["dossier"] = RuntimeError("x")
    out = fil(thread, follow_up=True)
    assert thread.names == ["traitement resume", "dossier resume", "finish_thread"]
    assert out["done"] == [{"thread_id": "<m1@x>", "sent": False, "reason": "Erreur (RuntimeError)."}]


@pytest.mark.functionality
def test_no_thread_claimed_runs_no_thread(thread):
    thread.answer("claim_threads", {"threads": []})
    out = asyncio.run(emails.graph.ainvoke({"cabinet": "c1"}))
    assert thread.names == ["claim_threads"] and out.get("done", []) == []


@pytest.mark.technical
def test_each_thread_claimed_runs_on_its_own_and_one_failing_leaves_the_others(thread):
    second = {**THREAD, "thread_id": "<m2@x>", "session_id": "s2"}
    thread.answer("claim_threads", {"threads": [THREAD, second]})
    thread.answer("thread_refusal", {"refused": False})
    thread.answer("finish_thread", RuntimeError("db"), {"status": "ok"})
    out = asyncio.run(emails.graph.ainvoke({"cabinet": "c1"}))
    assert thread.names.count("fil resume") == 2 and thread.names.count("finish_thread") == 2
    assert len(out["done"]) == 1
