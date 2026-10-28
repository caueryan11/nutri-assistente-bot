"""Webhook uazapi sem internet: cliente, banco e bot simulados."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from bot.assistente import Resultado
from whatsapp.app import Config, _historico_para_api, criar_app
from whatsapp.repositorio import Nutri, Paciente, variantes_telefone
from whatsapp import consentimento
from whatsapp.uazapi import extrair_mensagem

TOKEN = "token-da-instancia"
SEGREDO = "segredo-bem-comprido-123"
URL = f"/webhook/uazapi/{SEGREDO}"


def evento(texto="oi", **msg_extra):
    msg = {
        "messageid": "3EB0AAA111", "chatid": "5511999990001@s.whatsapp.net",
        "sender": "123456789@lid", "sender_pn": "5511999990001@s.whatsapp.net",
        "senderName": "Maria", "isGroup": False, "fromMe": False,
        "messageType": "Conversation", "text": texto, "wasSentByApi": False,
    }
    msg.update(msg_extra)
    return {"EventType": "messages", "owner": "5511900000000", "token": TOKEN,
            "BaseUrl": "https://exemplo.uazapi.com", "instanceName": "nutri-ana",
            "message": msg, "chat": {"phone": "+55 11 99999-0001", "wa_isGroup": False}}


class ClienteFalso:
    def __init__(self):
        self.enviadas = []

    def enviar_texto(self, numero, texto):
        self.enviadas.append((numero, texto))
        return f"wa-saida-{len(self.enviadas)}"


class RepoFalso:
    def __init__(self, consentimento=True, cadastrado=True, plano=True, termo_pedido=True):
        self.nutri = Nutri(id="n1", nome="Ana Souza", telefone_aviso="5511988880000")
        self.pac = Paciente(id="p1", nome="Maria Silva", primeiro_nome="Maria", telefone="5511999990001",
                            consentimento_em=datetime(2026, 9, 1) if consentimento else None,
                            termo_pedido_em=datetime(2026, 9, 28) if (termo_pedido and not consentimento) else None)
        self.cadastrado, self.tem_plano = cadastrado, plano
        self.entradas, self.saidas = set(), []
        self.consentimentos = []

    def nutri_por_instancia(self, instancia):
        return self.nutri if instancia == "nutri-ana" else None

    def paciente(self, nutri_id, telefone):
        return self.pac if self.cadastrado and telefone in variantes_telefone(self.pac.telefone) else None

    def registrar_entrada(self, nutri, paciente, msg):
        novo = msg.id not in self.entradas
        self.entradas.add(msg.id)
        return novo

    def plano_ativo(self, paciente_id):
        return {"refeicoes": []} if self.tem_plano else None

    def historico(self, paciente_id, antes_de_wa_id, limite=6):
        return [("entrada", "bom dia"), ("saida", "Bom dia, Maria!")]

    def registrar_saida(self, nutri, paciente, r, wa_id):
        self.saidas.append((r.acao, wa_id))

    def marcar_pedido_termo(self, paciente_id):
        self.pac.termo_pedido_em = self.pac.termo_pedido_em or datetime(2026, 9, 30)

    def registrar_consentimento(self, paciente_id, versao_termo):
        if self.pac.consentimento_em:
            return False
        self.pac.consentimento_em = datetime(2026, 9, 29)
        self.consentimentos.append((paciente_id, versao_termo))
        return True


def app_de_teste(modo="eco", repo=None, resultado=None):
    cliente = ClienteFalso()
    chamadas = []

    def responder_falso(pergunta, paciente, plano, **kw):
        chamadas.append({"pergunta": pergunta, **kw})
        return resultado or Resultado(acao="responder", mensagem_paciente="Pode sim, Maria!")

    app = criar_app(Config(modo, "https://exemplo.uazapi.com", TOKEN, SEGREDO),
                    cliente=cliente, repo=repo, taco=object(), responder_fn=responder_falso)
    return TestClient(app), cliente, chamadas


# ---------------------------------------------------------------------
# Segurança e filtros
# ---------------------------------------------------------------------
def test_segredo_errado_404():
    c, cliente, _ = app_de_teste()
    assert c.post("/webhook/uazapi/errado", json=evento()).status_code == 404
    assert cliente.enviadas == []


def test_token_errado_401():
    c, cliente, _ = app_de_teste()
    assert c.post(URL, json={**evento(), "token": "outro"}).status_code == 401
    assert cliente.enviadas == []


@pytest.mark.parametrize("extra", [{"fromMe": True}, {"isGroup": True}, {"wasSentByApi": True}])
def test_ignora_mensagens_que_nao_sao_de_paciente(extra):
    c, cliente, _ = app_de_teste()
    assert c.post(URL, json=evento(**extra)).json()["ignorado"] is True
    assert cliente.enviadas == []


def test_ignora_outros_eventos():
    c, cliente, _ = app_de_teste()
    assert c.post(URL, json={**evento(), "EventType": "messages_update"}).json()["ignorado"] is True


def test_config_exige_segredo_longo():
    with pytest.raises(ValueError):
        criar_app(Config("eco", "u", TOKEN, "curto"), cliente=ClienteFalso())


# ---------------------------------------------------------------------
# Eco
# ---------------------------------------------------------------------
def test_eco_responde_no_numero_certo_mesmo_com_lid():
    c, cliente, _ = app_de_teste()
    assert c.post(URL, json=evento("teste 1")).status_code == 200
    assert cliente.enviadas == [("5511999990001", "Eco: teste 1")]


def test_evento_repetido_processa_uma_vez():
    c, cliente, _ = app_de_teste()
    c.post(URL, json=evento())
    assert c.post(URL, json=evento()).json()["repetido"] is True
    assert len(cliente.enviadas) == 1


def test_eco_audio():
    c, cliente, _ = app_de_teste()
    c.post(URL, json=evento("", messageType="AudioMessage"))
    assert cliente.enviadas == [("5511999990001", "Eco: recebi uma mensagem do tipo audio.")]


# ---------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------
def test_bot_responde_e_registra():
    repo = RepoFalso()
    c, cliente, chamadas = app_de_teste("bot", repo)
    c.post(URL, json=evento("posso trocar o arroz?"))
    assert cliente.enviadas == [("5511999990001", "Pode sim, Maria!")]
    assert repo.saidas == [("responder", "wa-saida-1")]
    assert chamadas[0]["nome_nutri"] == "Ana Souza"
    assert chamadas[0]["historico"] == [{"role": "user", "content": "bom dia"},
                                        {"role": "assistant", "content": "Bom dia, Maria!"}]


def test_bot_encaminha_e_avisa_nutri():
    r = Resultado(acao="encaminhar", mensagem_paciente="A Ana vai te responder.",
                  motivo="sintoma", resumo_para_nutri="Relata enjoo.")
    c, cliente, _ = app_de_teste("bot", RepoFalso(), resultado=r)
    c.post(URL, json=evento("estou enjoada"))
    assert cliente.enviadas[0] == ("5511999990001", "A Ana vai te responder.")
    numero_nutri, aviso = cliente.enviadas[1]
    assert numero_nutri == "5511988880000" and "Relata enjoo." in aviso and "Maria Silva" in aviso
    assert aviso.startswith("🟡 *Responder · Maria Silva*") and "wa.me/5511999990001" in aviso


def test_encaminhamento_urgente_sai_em_vermelho_e_fica_marcado_para_o_resumo():
    repo = RepoFalso()
    r = Resultado(acao="encaminhar", mensagem_paciente="Já avisei a Ana.", motivo="sintoma",
                  resumo_para_nutri="Tontura e vômito desde ontem.", urgente=True)
    c, cliente, _ = app_de_teste("bot", repo, resultado=r)
    c.post(URL, json=evento("estou tonta"))
    aviso = cliente.enviadas[1][1]
    assert aviso.split("\n") == ["🔴 *Urgente · Maria Silva*", "Tontura e vômito desde ontem.",
                                 "👉 Fale com o paciente hoje: wa.me/5511999990001"]
    assert r.motivo == "urgente: sintoma"   # gravado assim: o resumo das 8h põe no topo


# número desconhecido: atendimento comercial, ver test_comercial.py


# ---------------------------------------------------------------------
# Consentimento LGPD
# ---------------------------------------------------------------------
def test_sem_consentimento_duvida_recebe_pedido_de_aceite_sem_chamar_a_ia():
    repo = RepoFalso(consentimento=False)
    c, cliente, chamadas = app_de_teste("bot", repo)
    c.post(URL, json=evento("Posso trocar o arroz por batata-doce?"))
    assert chamadas == []                                   # não chamou o Claude
    assert len(cliente.enviadas) == 1
    numero, texto = cliente.enviadas[0]
    assert numero == "5511999990001" and "responda 'Aceito'" in texto and "https://" in texto
    assert repo.consentimentos == [] and repo.entradas == set() and repo.saidas == []  # nada gravado


def test_sem_consentimento_audio_tambem_recebe_pedido():
    c, cliente, chamadas = app_de_teste("bot", RepoFalso(consentimento=False))
    c.post(URL, json=evento("", messageType="AudioMessage"))
    assert chamadas == [] and "responda 'Aceito'" in cliente.enviadas[0][1]


@pytest.mark.parametrize("texto", ["Aceito", "aceito", "ACEITO!", " Aceito. ", "Sim", "concordo", "Eu aceito", "Aceito 👍"])
def test_aceite_grava_e_confirma(texto):
    repo = RepoFalso(consentimento=False)
    c, cliente, chamadas = app_de_teste("bot", repo)
    c.post(URL, json=evento(texto))
    assert repo.consentimentos == [("p1", "v1.0")]
    assert cliente.enviadas == [("5511999990001", consentimento.CONFIRMACAO)]
    assert chamadas == []                                   # confirmação não usa IA
    assert repo.entradas == {"3EB0AAA111"} and repo.saidas == [("responder", "wa-saida-1")]  # prova do aceite


@pytest.mark.parametrize("texto", ["Sim, mas antes quero saber o que vocês fazem com meus dados",
                                   "não aceito", "aceito?", "o que é isso?"])
def test_resposta_que_nao_e_aceite_nao_grava(texto):
    repo = RepoFalso(consentimento=False)
    c, cliente, _ = app_de_teste("bot", repo)
    c.post(URL, json=evento(texto))
    assert repo.consentimentos == [] and "responda 'Aceito'" in cliente.enviadas[0][1]


def test_depois_do_aceite_a_proxima_mensagem_vai_para_o_bot():
    repo = RepoFalso(consentimento=False)
    c, cliente, chamadas = app_de_teste("bot", repo)
    c.post(URL, json=evento("Aceito"))
    c.post(URL, json=evento("Posso trocar o arroz?", messageid="3EB0BBB222"))
    assert len(chamadas) == 1 and chamadas[0]["pergunta"] == "Posso trocar o arroz?"


def test_com_consentimento_vai_direto_para_o_bot():
    repo = RepoFalso(consentimento=True)
    c, cliente, chamadas = app_de_teste("bot", repo)
    c.post(URL, json=evento("Posso trocar o arroz?"))
    assert len(chamadas) == 1 and repo.consentimentos == []
    assert cliente.enviadas == [("5511999990001", "Pode sim, Maria!")]


def test_termo_url_configurada_vai_no_pedido():
    cliente = ClienteFalso()
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, termo_url="https://nutri.exemplo/termo-v1"),
                    cliente=cliente, repo=RepoFalso(consentimento=False), taco=object())
    TestClient(app).post(URL, json=evento("oi"))
    assert "https://nutri.exemplo/termo-v1" in cliente.enviadas[0][1]


def test_bot_audio_encaminha_sem_chamar_ia():
    repo = RepoFalso()
    c, cliente, chamadas = app_de_teste("bot", repo)
    c.post(URL, json=evento("", messageType="AudioMessage"))
    assert chamadas == []
    assert "Ana Souza" in cliente.enviadas[0][1] and repo.saidas[0][0] == "encaminhar"


def test_bot_sem_plano_encaminha():
    c, cliente, chamadas = app_de_teste("bot", RepoFalso(plano=False))
    c.post(URL, json=evento("oi"))
    assert chamadas == [] and len(cliente.enviadas) == 2  # paciente + aviso à nutri


# ---------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------
def test_variantes_telefone_com_e_sem_nono_digito():
    v = variantes_telefone("5511999990001")
    assert "551199990001" in v and "+5511999990001" in v
    assert "5511999990001" in variantes_telefone("551199990001")


def test_telefone_nao_usa_lid():
    ev = evento()
    ev["message"].update(sender_pn=None, chatid="987@lid")
    ev["chat"]["phone"] = ""
    assert extrair_mensagem(ev) is None  # sem telefone resolvido: ignora em vez de chutar


def test_historico_comeca_em_user_e_nao_termina_em_user():
    h = _historico_para_api([("saida", "x"), ("entrada", "a"), ("entrada", "b"), ("saida", "c"), ("entrada", "d")])
    assert h == [{"role": "user", "content": "a\nb"}, {"role": "assistant", "content": "c"}]


def test_bot_marca_consulta_e_avisa_nutri():
    repo = RepoFalso()
    repo.nutri.google_calendar_id = "cal-1"
    r = Resultado(acao="responder", mensagem_paciente="Marcado: qua 30/09 às 10:00.",
                  agendamento={"marcado": True, "descricao": "qua 30/09 às 10:00"})
    c, cliente, chamadas = app_de_teste("bot", repo, resultado=r)
    c.app  # noqa
    c.post(URL, json=evento("quarta às 10"))
    assert cliente.enviadas[0] == ("5511999990001", "Marcado: qua 30/09 às 10:00.")
    assert cliente.enviadas[1][0] == "5511988880000" and "Consulta marcada" in cliente.enviadas[1][1]


def test_bot_recebe_agenda_so_se_nutri_tem_calendario():
    repo = RepoFalso()
    c, _, chamadas = app_de_teste("bot", repo)
    c.post(URL, json=evento("oi"))
    assert chamadas[0]["agenda"] is None


def test_saude_mostra_se_agenda_esta_ativa():
    c, _, _ = app_de_teste()
    assert c.get("/saude").json()["agenda"] is False


def test_credencial_google_invalida_nao_derruba_o_bot():
    app = criar_app(Config("eco", "https://exemplo.uazapi.com", TOKEN, SEGREDO, google_sa_b64="lixo-que-nao-e-base64="),
                    cliente=ClienteFalso())
    saude = TestClient(app).get("/saude").json()
    assert saude["ok"] is True and saude["agenda"] is False


@pytest.mark.parametrize("ag, trecho", [
    ({"tipo": "remarcado", "de": "qua 30/09 às 10:00", "para": "qui 01/10 às 08:00", "descricao": "qui 01/10 às 08:00"},
     "📅 *Consulta remarcada · Maria Silva*\nqua 30/09 às 10:00 → qui 01/10 às 08:00"),
    ({"tipo": "cancelado", "descricao": "qua 30/09 às 10:00"},
     "📅 *Consulta cancelada · Maria Silva*\nqua 30/09 às 10:00. O horário foi liberado"),
])
def test_aviso_de_remarcacao_e_cancelamento(ag, trecho):
    r = Resultado(acao="responder", mensagem_paciente="Feito.", agendamento=ag)
    c, cliente, _ = app_de_teste("bot", RepoFalso(), resultado=r)
    c.post(URL, json=evento("pode ser"))
    assert trecho in cliente.enviadas[1][1] and cliente.enviadas[1][0] == "5511988880000"
