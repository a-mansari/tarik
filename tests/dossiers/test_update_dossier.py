"""Changing an existing dossier: only to values the client wrote, never another dossier's ICE, IF or name."""

import pytest

from engine.tools.core.app import ToolError
from engine.tools.dossiers import identity as onboarding

CURRENT = {
    "name": "Gecafisc",
    "folder_type": "company",
    "ice": "000055632000078",
    "vat": "40246200",
    "rc": "229139",
    "pat": False,
    "tsc": False,
    "capital": 0.0,
    "cnss_number": False,
    "street": "Avenue Tantan",
    "street2": False,
    "city": "CASABLANCA",
    "zip": False,
}


@pytest.fixture
def experio(monkeypatch):
    calls = {"write": [], "others": []}

    def odoo_call(model, method, *args, **kw):
        if method == "read":
            return [{k: v for k, v in CURRENT.items() if k in kw["fields"]}]
        if method == "search_read":
            return calls["others"]
        raise AssertionError(method)

    monkeypatch.setattr(onboarding, "odoo_call", odoo_call)
    monkeypatch.setattr(onboarding, "odoo_json", lambda model, method, ids, values: calls["write"].append(values))
    monkeypatch.setattr(onboarding, "require_dossier", lambda dossier_id: "Gecafisc")
    monkeypatch.setattr(
        onboarding, "dossiers", lambda: [{"id": 557, "name": "Gecafisc"}, {"id": 25, "name": "CAFE EPSOM"}]
    )
    monkeypatch.setattr(onboarding, "dry_run", lambda: False)
    monkeypatch.setattr(onboarding, "client_messages", lambda session_id, message: [message])
    return calls


@pytest.mark.functionality
def test_the_fields_the_client_wrote_are_changed(experio):
    result = onboarding.update_dossier(
        557,
        rc="331245",
        city="Rabat",
        capital=100000,
        client_message="Pour Gecafisc : nouveau RC 331245, ville Rabat, capital 100 000 DH.",
    )
    assert experio["write"] == [{"rc": "331245", "city": "Rabat", "capital": 100000.0}]
    assert "RC : 229139 -> 331245" in result["message"] and "ville : CASABLANCA -> Rabat" in result["message"]


@pytest.mark.security
def test_a_value_the_client_did_not_write_is_left_alone(experio):
    result = onboarding.update_dossier(557, rc="331245", city="Marrakech", client_message="Changez le RC en 331245.")
    assert experio["write"] == [{"rc": "331245"}]
    assert "Non modifie, le client ne l'a pas ecrit : ville" in result["message"]


@pytest.mark.functionality
def test_an_ice_sent_as_a_number_keeps_its_zeros(experio):
    onboarding.update_dossier(557, ice=8123456000011, client_message="Le nouvel ICE est 008123456000011.")
    assert experio["write"] == [{"ice": "008123456000011"}]


@pytest.mark.functionality
def test_experios_placeholder_ice_is_taken_when_the_client_writes_it(experio):
    onboarding.update_dossier(557, ice="1111", client_message="Changer l'ICE du dossier a 1111")
    assert experio["write"] == [{"ice": "1111"}]


@pytest.mark.security
def test_another_dossiers_ice_is_refused(experio):
    experio["others"] = [{"id": 25, "name": "CAFE EPSOM"}]
    result = onboarding.update_dossier(557, ice="008123456000011", client_message="ICE 008123456000011")
    assert result["status"] == "stop" and "CAFE EPSOM" in result["message"] and experio["write"] == []


@pytest.mark.security
def test_another_dossiers_name_is_refused(experio):
    result = onboarding.update_dossier(557, name="Cafe Epsom", client_message="Renommez Gecafisc en Cafe Epsom.")
    assert result["status"] == "stop" and experio["write"] == []


@pytest.mark.security
def test_the_type_needs_the_clients_words(experio):
    onboarding.update_dossier(557, folder_type="person", client_message="C'est en fait une personne physique.")
    assert experio["write"] == [{"folder_type": "person"}]
    experio["write"].clear()
    result = onboarding.update_dossier(557, folder_type="person", client_message="Changez le type du dossier.")
    assert experio["write"] == [] and result["status"] == "stop"


@pytest.mark.security
def test_skipping_the_registry_check_is_the_clients_confirmed_request_only(experio, monkeypatch):
    monkeypatch.setattr(onboarding, "confirmed", lambda session_id, message, *parts: False)
    result = onboarding.update_dossier(
        557, ice="008123456000011", skip_ice_if_check=True, client_message="ICE 008123456000011, sans verification svp"
    )
    assert result["status"] == "question" and "sans cette verification" in result["message"] and experio["write"] == []
    monkeypatch.setattr(onboarding, "confirmed", lambda session_id, message, *parts: "Gecafisc" in parts)
    onboarding.update_dossier(557, ice="008123456000011", skip_ice_if_check=True, client_message="ICE 008123456000011")
    assert experio["write"] == [{"ice": "008123456000011", "ignore_ice_if_verification": True}]


@pytest.mark.security
def test_nothing_is_written_in_test_mode(experio, monkeypatch):
    monkeypatch.setattr(onboarding, "dry_run", lambda: True)
    result = onboarding.update_dossier(557, rc="331245", client_message="RC 331245")
    assert experio["write"] == [] and "serait modifie : RC : 229139 -> 331245" in result["message"]


@pytest.mark.communication
def test_a_call_without_fields_says_how_to_call_again(experio):
    with pytest.raises(ToolError, match="rappelle Experio_modifier_dossier"):
        onboarding.update_dossier(557, client_message="Modifiez le dossier Gecafisc.")


@pytest.mark.functionality
def test_an_unchanged_value_is_not_written(experio):
    result = onboarding.update_dossier(557, rc="229139", client_message="RC 229139")
    assert result["status"] == "stop" and experio["write"] == []
