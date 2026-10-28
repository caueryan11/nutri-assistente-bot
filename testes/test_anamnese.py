"""Anamnese: o bot nunca sugere o que o paciente não pode ou não gosta de comer (sem internet)."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from bot.anamnese import conflitos, conflitos_no_plano, limpar, mencoes_de_risco, texto_para_prompt
from bot.assistente import Resultado, responder
from testes.test_unidade import PACIENTE, PLANO, ClienteFalso, _chamada, _resposta, taco  # noqa: F401 (fixture)

ANAMNESE = {"alergias": ["amendoim", "camarão"], "intolerancias": ["lactose"], "nao_gosta": ["beterraba"],
            "restricoes": [], "observacoes": "Refluxo: evitar refeição volumosa à noite."}
MARIA = {**PACIENTE, "anamnese": ANAMNESE}


def _final(texto, sugeridos=(), acao="responder", id_="f"):
    return _chamada("enviar_resposta", {"acao": acao, "mensagem_paciente": texto, "motivo": "",
                                        "resumo_para_nutri": "", "alimentos_sugeridos": list(sugeridos)}, id_)


# ---------------------------------------------------------------------
# Conferência
# ---------------------------------------------------------------------
@pytest.mark.parametrize("alimento, item, categoria", [
    ("Pasta de amendoim", "amendoim", "alergia"),
    ("paçoca", "amendoim", "alergia"),
    ("Amendoins torrados", "amendoim", "alergia"),
    ("CAMARÕES ao alho", "camarão", "alergia"),
    ("1 copo de leite desnatado", "lactose", "intolerância"),
    ("queijo minas frescal", "lactose", "intolerância"),
    ("Salada de beterraba", "beterraba", "não gosta"),
])
def test_conflitos_encontrados(alimento, item, categoria):
    (c,) = conflitos([alimento], ANAMNESE)
    assert (c["item"], c["categoria"]) == (item, categoria)


@pytest.mark.parametrize("alimento", ["leite sem lactose", "leite de coco", "iogurte zero lactose", "banana prata",
                                      "arroz integral", "castanha de caju"])
def test_sem_conflito(alimento):
    assert conflitos([alimento], ANAMNESE) == []


def test_restricao_vegetariana_e_gluten():
    a = {"restricoes": ["vegetariano"], "intolerancias": ["glúten"]}
    assert {c["alimento"] for c in conflitos(["bife de patinho", "Pão francês", "pão sem glúten", "tofu"], a)} == {
        "bife de patinho", "Pão francês"}


def test_mencoes_de_risco_so_alergia_e_intolerancia():
    assert mencoes_de_risco("Evite o amendoim e o queijo, tá?", ANAMNESE) == ["amendoim", "lactose"]
    assert mencoes_de_risco("Pode trocar a beterraba pela cenoura", ANAMNESE) == []   # não gosta: não é risco


def test_anamnese_invalida_vira_vazia():
    assert limpar(None) == {"alergias": [], "intolerancias": [], "restricoes": [], "nao_gosta": []}
    assert limpar({"alergias": "amendoim", "nao_gosta": 3})["alergias"] == ["amendoim"]  # texto solto vira lista


def test_conflitos_no_plano_de_exemplo():
    itens = {c["alimento"] for c in conflitos_no_plano(PLANO, ANAMNESE)}
    assert "1 copo de leite desnatado (200 ml)" in itens and "Iogurte natural" in itens
    assert "Arroz integral cozido" not in itens


def test_prompt_com_e_sem_anamnese():
    com = texto_para_prompt(ANAMNESE, PLANO, "Maria")
    assert "Alergias: amendoim, camarão" in com and "Não gosta de: beterraba" in com and "Refluxo" in com
    assert "ATENÇÃO: o plano traz itens que conflitam" in com and "Iogurte natural" in com
    assert "Não cadastrada" in texto_para_prompt({}, PLANO, "Maria")


# ---------------------------------------------------------------------
# Laço do bot
# ---------------------------------------------------------------------
def test_sugestao_proibida_e_recusada_e_corrigida(taco):
    cliente = ClienteFalso([
        _resposta(_final("Pode trocar o iogurte por 1 colher de pasta de amendoim!", ["pasta de amendoim"], id_="f1")),
        _resposta(_final("Pode trocar o iogurte por 1 banana prata!", ["banana prata"], id_="f2")),
    ])
    r = responder("Posso trocar o iogurte do lanche?", MARIA, PLANO, taco=taco, cliente=cliente)
    assert r.mensagem_paciente == "Pode trocar o iogurte por 1 banana prata!" and r.bloqueios_anamnese == 1
    erro = cliente.pedidos[1]["messages"][-1]["content"][0]
    assert erro["is_error"] and "alergia: amendoim" in erro["content"]
    # a anamnese foi para o modelo, no bloco com cache
    assert "Alergias: amendoim, camarão" in cliente.pedidos[0]["system"][1]["text"]


def test_citar_alergenico_exige_confirmacao(taco):
    aviso = "Maria, como você tem alergia a camarão, esse prato não é indicado. Que tal o frango grelhado do plano?"
    cliente = ClienteFalso([_resposta(_final(aviso, ["peito de frango grelhado"], id_="f1")),
                            _resposta(_final(aviso, ["peito de frango grelhado"], id_="f2"))])
    r = responder("Posso comer strogonoff de camarão?", MARIA, PLANO, taco=taco, cliente=cliente)
    assert r.mensagem_paciente == aviso and r.passos == 2 and r.bloqueios_anamnese == 0
    assert "Confira que você NÃO está sugerindo" in cliente.pedidos[1]["messages"][-1]["content"][0]["content"]


def test_insistir_na_sugestao_proibida_acaba_encaminhando(taco):
    ruim = _resposta(_final("Pode tomar 1 copo de leite integral.", ["leite integral"]))
    r = responder("Posso tomar leite?", MARIA, PLANO, taco=taco, cliente=ClienteFalso([ruim] * 10))
    assert r.acao == "encaminhar" and r.fallback and "leite" not in r.mensagem_paciente.lower()


def test_nao_gosta_nunca_e_sugerido_mas_pode_ser_liberado_se_o_paciente_pedir(taco):
    sugere = ClienteFalso([_resposta(_final("Que tal salada de beterraba?", ["salada de beterraba"], id_="f1")),
                           _resposta(_final("Que tal salada de cenoura?", ["salada de cenoura"], id_="f2"))])
    r = responder("O que posso colocar na salada?", MARIA, PLANO, taco=taco, cliente=sugere)
    assert r.mensagem_paciente == "Que tal salada de cenoura?" and r.bloqueios_anamnese == 1
    libera = ClienteFalso([_resposta(_final("Pode sim!", ["salada de beterraba"]))])
    r = responder("Hoje quero salada de beterraba, pode?", MARIA, PLANO, taco=taco, cliente=libera)
    assert r.mensagem_paciente == "Pode sim!" and r.passos == 1


def test_alergia_nunca_e_liberada_nem_se_o_paciente_pedir(taco):
    cliente = ClienteFalso([_resposta(_final("Pode sim!", ["paçoca"], id_="f1")),
                            _resposta(_final("Paçoca não é indicada para você.", [], id_="f2")),
                            _resposta(_final("Paçoca não é indicada para você.", [], id_="f3"))])
    r = responder("Posso comer paçoca?", MARIA, PLANO, taco=taco, cliente=cliente)
    assert r.bloqueios_anamnese == 1 and "não é indicada" in r.mensagem_paciente


def test_sem_anamnese_nada_muda(taco):
    cliente = ClienteFalso([_resposta(_final("Pode sim, pasta de amendoim é uma boa!", ["pasta de amendoim"]))])
    r = responder("Posso comer pasta de amendoim?", PACIENTE, PLANO, taco=taco, cliente=cliente)
    assert r.acao == "responder" and r.passos == 1


# ---------------------------------------------------------------------
# Webhook: a anamnese do banco chega ao bot
# ---------------------------------------------------------------------
def test_webhook_passa_a_anamnese():
    from testes.test_whatsapp import URL, RepoFalso, evento

    from testes.test_whatsapp import SEGREDO, TOKEN, ClienteFalso as UazapiFalso
    from whatsapp.app import Config, criar_app

    repo = RepoFalso()
    repo.pac.anamnese = ANAMNESE
    chamadas = []
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO), cliente=UazapiFalso(), repo=repo,
                    taco=object(), responder_fn=lambda p, paciente, *a, **k: chamadas.append(paciente) or Resultado(
                        acao="responder", mensagem_paciente="ok"))
    TestClient(app).post(URL, json=evento("Posso comer amendoim?"))
    assert chamadas[0]["anamnese"] == ANAMNESE
