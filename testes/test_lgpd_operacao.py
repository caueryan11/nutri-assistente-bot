"""Revogação de consentimento, página do termo, retenção e monitor do WhatsApp (sem internet)."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from bot.assistente import Resultado
from bot.comercial import ResultadoLead
from bot.rotinas import criar_agendador, rodar_retencao
from testes.test_comercial import RepoComLeads, app_leads
from testes.test_whatsapp import SEGREDO, TOKEN, URL, ClienteFalso, RepoFalso, evento
from whatsapp import consentimento
from whatsapp.app import Config, criar_app
from whatsapp.monitor import MonitorWhatsApp, enviar_alerta
from whatsapp.uazapi import ClienteUazapi


# ---------------------------------------------------------------------
# Revogação
# ---------------------------------------------------------------------
@pytest.mark.parametrize("texto", ["PARAR", "parar.", "Sair", "stop", "Revogo meu consentimento", "não quero mais receber mensagens"])
def test_eh_revogacao(texto):
    assert consentimento.eh_revogacao(texto)


@pytest.mark.parametrize("texto", ["posso sair da dieta hoje?", "parar de comer doce ajuda?", "cancelar a consulta",
                                   "aceito", ""])
def test_nao_e_revogacao(texto):
    assert not consentimento.eh_revogacao(texto)


class RepoRevogavel(RepoFalso):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.pac.status_consentimento = "aceito" if self.pac.consentimento_em else "pendente"
        self.revogacoes = []

    def registrar_revogacao(self, paciente_id):
        if self.pac.status_consentimento == "revogado":
            return False
        self.pac.consentimento_em, self.pac.status_consentimento = None, "revogado"
        self.revogacoes.append(paciente_id)
        return True

    def registrar_consentimento(self, paciente_id, versao_termo):
        ok = super().registrar_consentimento(paciente_id, versao_termo)
        if ok:
            self.pac.status_consentimento = "aceito"
        return ok


def app_bot(repo, url_publica=""):
    cliente, chamadas = ClienteFalso(), []

    def responder_falso(pergunta, *a, **k):
        chamadas.append(pergunta)
        return Resultado(acao="responder", mensagem_paciente="Pode sim!")

    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, url_publica=url_publica),
                    cliente=cliente, repo=repo, taco=object(), responder_fn=responder_falso)
    return TestClient(app), cliente, chamadas


def test_paciente_revoga_e_bot_para():
    repo = RepoRevogavel()
    c, cliente, chamadas = app_bot(repo)
    c.post(URL, json=evento("PARAR", messageid="R1"))
    assert repo.revogacoes == ["p1"] and chamadas == []
    (pac, texto), (nutri, aviso) = cliente.enviadas
    assert pac == "5511999990001" and "revogado" in texto and "Ana Souza" in texto
    assert nutri == "5511988880000" and "Maria Silva" in aviso and "revogou" in aviso.lower()
    assert repo.entradas == set()                       # nada da mensagem foi gravado

    # depois de revogar: dúvida não chama a IA nem recebe pedido de aceite
    c.post(URL, json=evento("Posso trocar o arroz?", messageid="R2"))
    assert chamadas == [] and len(cliente.enviadas) == 2
    # PARAR de novo: nada acontece
    c.post(URL, json=evento("parar", messageid="R3"))
    assert len(cliente.enviadas) == 2

    # "Aceito" reativa
    c.post(URL, json=evento("Aceito", messageid="R4"))
    assert repo.pac.status_consentimento == "aceito" and cliente.enviadas[-1][1] == consentimento.CONFIRMACAO
    c.post(URL, json=evento("Posso trocar o arroz?", messageid="R5"))
    assert chamadas == ["Posso trocar o arroz?"]


def test_paciente_pendente_que_pede_para_parar_nao_recebe_mais_pedidos():
    repo = RepoRevogavel(consentimento=False)
    c, cliente, _ = app_bot(repo)
    c.post(URL, json=evento("oi", messageid="P1"))
    assert "Aceito" in cliente.enviadas[0][1]            # pedido do termo
    c.post(URL, json=evento("sair", messageid="P2"))
    assert repo.pac.status_consentimento == "revogado"
    c.post(URL, json=evento("oi de novo", messageid="P3"))
    assert len(cliente.enviadas) == 3                     # pedido + confirmação + aviso à nutri; nada depois


def test_confirmacao_do_aceite_ensina_a_revogar():
    assert "PARAR" in consentimento.CONFIRMACAO


def test_pedido_usa_a_pagina_termo_do_app():
    repo = RepoRevogavel(consentimento=False)
    c, cliente, _ = app_bot(repo, url_publica="https://app.exemplo/")
    c.post(URL, json=evento("oi"))
    assert "https://app.exemplo/termo" in cliente.enviadas[0][1]


def test_lead_que_pede_para_parar_vira_desistiu_e_pode_voltar():
    repo, chamadas = RepoComLeads(), []

    def atender(texto, lead, nutri, **kw):
        chamadas.append(texto)
        return ResultadoLead(acao="responder", mensagem="Oi!")

    c, cliente = app_leads(repo, atender)
    c.post(URL, json=evento("parar", messageid="L1"))
    assert repo.leads["5511999990001"]["status"] == "desistiu" and chamadas == []
    assert cliente.enviadas == [("5511999990001", consentimento.CONFIRMACAO_SAIDA_LEAD)]
    c.post(URL, json=evento("parar", messageid="L2"))
    assert len(cliente.enviadas) == 1                      # não repete
    c.post(URL, json=evento("Oi, quero marcar consulta", messageid="L3"))
    assert chamadas == ["Oi, quero marcar consulta"] and repo.leads["5511999990001"]["status"] == "em_triagem"


# ---------------------------------------------------------------------
# Página do termo
# ---------------------------------------------------------------------
def test_pagina_do_termo():
    c, _, _ = app_bot(RepoRevogavel())
    r = c.get("/termo")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert f"Versão {consentimento.VERSAO_TERMO}" in r.text and "PARAR" in r.text and "180 dias" in r.text
    assert "fora do Brasil" in r.text and "art. 18" in r.text


# ---------------------------------------------------------------------
# Retenção
# ---------------------------------------------------------------------
def test_rodar_retencao_usa_os_prazos():
    class Repo:
        def limpar_antigos(self, dp, dl):
            self.prazos = (dp, dl)
            return {"mensagens": 3, "lead_mensagens": 2, "leads": 1}

    repo = Repo()
    assert rodar_retencao(repo, 180, 90) == {"mensagens": 3, "lead_mensagens": 2, "leads": 1}
    assert repo.prazos == (180, 90)


def test_prazo_de_retencao_curto_demais_e_recusado():
    with pytest.raises(ValueError):
        criar_app(Config("eco", "u", TOKEN, SEGREDO, retencao_dias=7), cliente=ClienteFalso())


def test_agendador_com_retencao_e_monitor():
    monitor = MonitorWhatsApp(lambda: {"conectado": True, "estado": "connected"}, lambda t: None)
    ag = criar_agendador(object(), lambda *a: None, retencao=(180, 90), monitor=monitor)
    assert "hour='3'" in str(ag.get_job("retencao").trigger) and "minute='30'" in str(ag.get_job("retencao").trigger)
    assert ag.get_job("monitor_whatsapp") is not None
    assert criar_agendador(object(), lambda *a: None).get_job("retencao") is None


# ---------------------------------------------------------------------
# Monitor do WhatsApp
# ---------------------------------------------------------------------
def _uazapi(resposta):
    def tratar(req):
        assert req.url.path == "/instance/status" and req.headers["token"] == "tok"
        if isinstance(resposta, Exception):
            raise resposta
        return resposta
    return ClienteUazapi("https://x.uazapi.com", "tok", http=httpx.Client(transport=httpx.MockTransport(tratar)))


def test_status_da_uazapi():
    ok = httpx.Response(200, json={"instance": {"status": "connected"}, "status": {"connected": True}})
    assert _uazapi(ok).status() == {"conectado": True, "estado": "connected"}
    fora = httpx.Response(200, json={"instance": {"status": "disconnected"}, "status": {"connected": False}})
    assert _uazapi(fora).status() == {"conectado": False, "estado": "disconnected"}
    assert "token inválido" in _uazapi(httpx.Response(401, json={"error": "Invalid token"})).status()["estado"]
    assert "sem resposta" in _uazapi(httpx.ConnectError("x")).status()["estado"]


def test_monitor_avisa_quando_cai_repete_e_quando_volta():
    estados = iter([True, False, False, False, True, True])
    agora = [datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc)]
    alertas = []
    m = MonitorWhatsApp(lambda: (lambda c: {"conectado": c, "estado": "connected" if c else "disconnected"})(next(estados)),
                        alertas.append, agora=lambda: agora[0])
    m.conferir()                                   # conectado: sem alerta
    assert alertas == [] and m.resumo()["conectado"] is True
    agora[0] += timedelta(minutes=10); m.conferir()  # caiu
    assert len(alertas) == 1 and "fora do ar" in alertas[0]
    agora[0] += timedelta(minutes=10); m.conferir()  # continua fora: não repete ainda
    assert len(alertas) == 1
    agora[0] += timedelta(hours=6); m.conferir()     # 6 h depois: repete
    assert len(alertas) == 2
    agora[0] += timedelta(minutes=10); m.conferir()  # voltou
    assert "reconectado" in alertas[-1] and m.resumo()["desde"] == agora[0].isoformat()
    agora[0] += timedelta(minutes=10); m.conferir()
    assert len(alertas) == 3


def test_monitor_ja_comeca_fora_avisa_e_alerta_com_erro_nao_quebra():
    def falha(texto):
        raise RuntimeError("ntfy fora")
    m = MonitorWhatsApp(lambda: {"conectado": False, "estado": "token inválido"}, falha)
    assert m.conferir()["conectado"] is False       # não levanta


@pytest.mark.parametrize("url, esperado", [
    ("https://ntfy.sh/meu-topico", ("content", None)),
    ("https://discord.com/api/webhooks/1/abc", ("json", {"content": "oi"})),
    ("https://hooks.slack.com/services/T/B/X", ("json", {"text": "oi"})),
])
def test_formatos_de_alerta(url, esperado):
    recebidos = []

    def tratar(req):
        recebidos.append(req)
        return httpx.Response(200)

    enviar_alerta(url, "oi", http=httpx.Client(transport=httpx.MockTransport(tratar)))
    (req,) = recebidos
    if esperado[0] == "json":
        import json
        assert json.loads(req.content) == esperado[1]
    else:
        assert req.content == "oi".encode() and req.headers["title"] == "Nutri Assistente"


def test_sem_url_de_alerta_nao_envia():
    enviar_alerta("", "oi", http=None)  # não tenta rede


def test_saude_mostra_o_whatsapp():
    class UazapiComStatus(ClienteFalso):
        def status(self):
            return {"conectado": False, "estado": "token inválido"}

    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO), cliente=UazapiComStatus(),
                    repo=RepoRevogavel(), taco=object())
    c = TestClient(app)
    assert c.get("/saude").json()["whatsapp"] == {"conectado": None, "estado": "não conferido", "desde": None}


def test_evento_de_conexao_confere_na_hora_e_alerta():
    estados = iter([{"conectado": True, "estado": "connected"}, {"conectado": False, "estado": "disconnected"}])
    alertas = []

    class UazapiComStatus(ClienteFalso):
        def status(self):
            return next(estados)

    import whatsapp.monitor as mon
    original = mon.enviar_alerta
    mon.enviar_alerta = lambda url, texto, http=None: alertas.append(texto)
    try:
        app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, alerta_url="https://ntfy.sh/x"),
                        cliente=UazapiComStatus(), repo=RepoRevogavel(), taco=object())
        c = TestClient(app)
        assert c.get("/saude").json()["alerta"] is True
        evento_conexao = {"EventType": "connection", "token": TOKEN, "instanceName": "nutri-ana"}
        assert c.post(URL, json=evento_conexao).json() == {"ok": True, "conexao": True}   # 1ª conferência: conectado
        c.post(URL, json=evento_conexao)                                                  # 2ª: caiu -> alerta
        assert len(alertas) == 1 and "fora do ar" in alertas[0]
        assert c.post(URL, json={**evento_conexao, "token": "outro"}).status_code == 401  # token errado não dispara
    finally:
        mon.enviar_alerta = original
