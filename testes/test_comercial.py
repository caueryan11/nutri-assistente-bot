"""Atendimento comercial (leads): Claude simulado, agenda simulada e webhook sem internet."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from agenda.servico import AgendaNutri, HorarioOcupado
from bot.comercial import NutriComercial, ResultadoLead, atender_lead, montar_system, verificar_convenio
from bot.rotinas import montar_resumo
from testes.test_agenda import CONFIG, CalendarioFalso
from testes.test_unidade import ClienteFalso as ClaudeFalso, _chamada, _resposta
from testes.test_whatsapp import TOKEN, SEGREDO, URL, ClienteFalso, RepoFalso, evento
from whatsapp import consentimento
from whatsapp.app import Config, criar_app

SP = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 9, 29, 9, 0, tzinfo=SP)  # terça, 09:00

COM_UNIMED = NutriComercial("Ana Souza", atende_convenio=True, convenios_aceitos=["Unimed", "Bradesco Saúde"],
                            valor_consulta=250, politica_reembolso="Emitimos recibo com CRN para reembolso.",
                            apresentacao_servico="Consulta de 1 h com plano individual e acompanhamento no WhatsApp.")
SO_PARTICULAR = NutriComercial("Ana Souza", atende_convenio=False, valor_consulta=250,
                               politica_reembolso="Emitimos recibo com CRN para reembolso.")


def _final(texto, acao="responder"):
    return _chamada("enviar_resposta", {"acao": acao, "mensagem_paciente": texto, "motivo": "", "resumo_para_nutri": ""}, "fim")


def _resultado_da_ferramenta(pedido, nome_id):
    """Conteúdo (json) do tool_result que o bot devolveu ao modelo."""
    for m in pedido["messages"]:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("tool_use_id") == nome_id:
                    return json.loads(b["content"])
    raise AssertionError(f"sem tool_result para {nome_id}")


# ---------------------------------------------------------------------
# Convênio (a comparação é do código, não do modelo)
# ---------------------------------------------------------------------
def test_convenio_aceito_tolera_caixa_acento_e_complemento():
    assert verificar_convenio(COM_UNIMED, "UNIMED Porto Alegre")["aceito"] is True
    assert verificar_convenio(COM_UNIMED, "bradesco saude")["aceito"] is True
    assert verificar_convenio(COM_UNIMED, "plano Unimed")["nome_na_lista"] == "Unimed"


def test_convenio_fora_da_lista_oferece_particular_com_reembolso():
    r = verificar_convenio(COM_UNIMED, "Amil")
    assert r["aceito"] is False and "reembolso" in r["orientacao"]


def test_nutri_so_particular_nunca_aceita_convenio():
    r = verificar_convenio(SO_PARTICULAR, "Unimed")
    assert r["atende_convenio"] is False and r["aceito"] is False
    assert "recibo" in r["orientacao"] and r["politica_reembolso"]


def test_system_traz_valor_e_convenios():
    texto = montar_system(COM_UNIMED, {"nome": "Joana"}, True, AGORA)[0]["text"]
    assert "R$ 250,00" in texto and "Unimed, Bradesco Saúde" in texto
    assert "Não dê orientação nutricional" in texto
    assert "Joana" in montar_system(COM_UNIMED, {"nome": "Joana"}, True, AGORA)[1]["text"]


# ---------------------------------------------------------------------
# Cenário 1: "Vocês atendem Unimed?" (nutri aceita Unimed)
# ---------------------------------------------------------------------
def test_cenario1_unimed_aceito():
    claude = ClaudeFalso([
        _resposta(_chamada("consultar_convenio", {"convenio": "Unimed"}, "c1")),
        _resposta(_final("Atendemos Unimed, sim! 😊 Qual é o seu objetivo com o acompanhamento?")),
    ])
    atualizados = []
    r = atender_lead("Oi, vocês atendem Unimed?", {}, COM_UNIMED, cliente=claude, agora=AGORA,
                     atualizar=atualizados.append)
    saida = _resultado_da_ferramenta(claude.pedidos[1], "c1")
    assert saida["aceito"] is True and saida["nome_na_lista"] == "Unimed"
    assert r.acao == "responder" and "Unimed" in r.mensagem and r.convertido is None
    assert {"convenio_informado": "Unimed"} in atualizados
    # sem agenda: o modelo não recebe as ferramentas de marcar
    assert "marcar_primeira_consulta" not in [f["name"] for f in claude.pedidos[0]["tools"]]


# ---------------------------------------------------------------------
# Cenário 2: nutri só particular -> recibo para reembolso
# ---------------------------------------------------------------------
def test_cenario2_particular_com_recibo_para_reembolso():
    claude = ClaudeFalso([
        _resposta(_chamada("atualizar_lead", {"nome": "Carla", "objetivo": "emagrecimento"}, "a1"),
                  _chamada("consultar_convenio", {"convenio": "Unimed"}, "c1")),
        _resposta(_final("Carla, o atendimento é particular (R$ 250,00), e você recebe recibo para pedir reembolso à Unimed.")),
    ])
    atualizados, lead = [], {}
    r = atender_lead("Sou a Carla, quero emagrecer. Tenho Unimed", lead, SO_PARTICULAR, cliente=claude,
                     agora=AGORA, atualizar=atualizados.append)
    saida = _resultado_da_ferramenta(claude.pedidos[1], "c1")
    assert saida["aceito"] is False and "recibo" in saida["orientacao"]
    assert "reembolso" in r.mensagem
    assert {"nome": "Carla", "objetivo": "emagrecimento"} in atualizados and lead["nome"] == "Carla"


# ---------------------------------------------------------------------
# Cenário 3: escolhe horário -> vira paciente com evento no Google
# ---------------------------------------------------------------------
def _agenda_para(cal, convertidos, ocupado=False):
    def agenda_para(nome, convenio):
        def gravar(ini, fim, evento):
            if ocupado:
                raise HorarioOcupado()
            convertidos.append({"nome": nome, "convenio": convenio, "inicio": ini, "evento": evento})
        return AgendaNutri(cal, "cal-1", CONFIG, "America/Sao_Paulo", nome, "5551999990002",
                           gravar=gravar, agora=lambda: AGORA, titulo="Primeira consulta")
    return agenda_para


def test_cenario3_escolhe_horario_e_vira_paciente():
    cal, convertidos, atualizados = CalendarioFalso(), [], []
    claude = ClaudeFalso([
        _resposta(_chamada("listar_horarios_disponiveis", {"periodo": "manha"}, "h1")),
        _resposta(_chamada("marcar_primeira_consulta", {"nome": "Joana Pereira", "data_hora_iso": "2026-09-30T10:00",
                                                        "convenio_usado": "particular"}, "m1")),
        _resposta(_final("Prontinho, Joana! Primeira consulta marcada para qua 30/09 às 10:00.")),
    ])
    r = atender_lead("Quero marcar, pode ser quarta às 10h. Joana Pereira", {}, COM_UNIMED, cliente=claude,
                     agora=AGORA, agenda_para=_agenda_para(cal, convertidos), atualizar=atualizados.append)
    horarios = _resultado_da_ferramenta(claude.pedidos[1], "h1")["horarios"]
    assert horarios and all(h["inicio"] < "2026-09-30T12:00" or h["inicio"][:10] > "2026-09-30" for h in horarios)
    assert {"status": "aguardando_escolha_horario"} in atualizados
    # evento no Google + conversão gravada
    (evento,) = cal.eventos.values()
    assert evento["titulo"] == "Primeira consulta – Joana Pereira"
    assert evento["ini"] == datetime(2026, 9, 30, 10, 0, tzinfo=SP)
    assert convertidos == [{"nome": "Joana Pereira", "convenio": "particular",
                            "inicio": datetime(2026, 9, 30, 10, 0, tzinfo=SP), "evento": "ev1"}]
    assert r.convertido == {"nome": "Joana Pereira", "descricao": "qua 30/09 às 10:00",
                            "inicio": "2026-09-30T10:00", "convenio": "particular"}


def test_marcar_sem_nome_completo_pede_o_nome():
    cal, convertidos = CalendarioFalso(), []
    claude = ClaudeFalso([
        _resposta(_chamada("marcar_primeira_consulta", {"nome": "Joana", "data_hora_iso": "2026-09-30T10:00",
                                                        "convenio_usado": "particular"}, "m1")),
        _resposta(_final("Me passa seu nome completo?")),
    ])
    r = atender_lead("quarta 10h", {}, COM_UNIMED, cliente=claude, agora=AGORA, agenda_para=_agenda_para(cal, convertidos))
    assert _resultado_da_ferramenta(claude.pedidos[1], "m1")["marcado"] is False
    assert cal.eventos == {} and convertidos == [] and r.convertido is None


def test_horario_ocupado_no_banco_desfaz_evento_e_nao_converte():
    cal, convertidos = CalendarioFalso(), []
    claude = ClaudeFalso([
        _resposta(_chamada("marcar_primeira_consulta", {"nome": "Joana Pereira", "data_hora_iso": "2026-09-30T10:00",
                                                        "convenio_usado": "Unimed"}, "m1")),
        _resposta(_final("Esse horário acabou de ser ocupado. Posso te oferecer outro?")),
    ])
    r = atender_lead("quarta 10h", {}, COM_UNIMED, cliente=claude, agora=AGORA,
                     agenda_para=_agenda_para(cal, convertidos, ocupado=True))
    assert cal.cancelados == ["ev1"] and cal.eventos == {} and r.convertido is None


def test_erro_da_api_encaminha_para_a_nutri():
    import anthropic
    import httpx

    erro = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    r = atender_lead("oi", {}, COM_UNIMED, cliente=ClaudeFalso([erro]), agora=AGORA)
    assert r.acao == "encaminhar" and r.fallback and "Ana Souza" in r.mensagem


# ---------------------------------------------------------------------
# Webhook: número desconhecido vira lead
# ---------------------------------------------------------------------
class RepoComLeads(RepoFalso):
    def __init__(self, **kw):
        super().__init__(cadastrado=False, **kw)
        self.nutri.google_calendar_id = "cal-1"
        self.nutri.agenda_config = CONFIG
        self.leads, self.lead_msgs, self.conversoes = {}, [], []

    def lead(self, nutri_id, telefone):
        return self.leads.setdefault(telefone, {"id": f"l{len(self.leads) + 1}", "nome": None, "objetivo": None,
                                                "convenio_informado": None, "status": "em_triagem"})

    def atualizar_lead(self, lead_id, campos):
        for l in self.leads.values():
            if l["id"] == lead_id:
                l.update(campos)

    def registrar_lead_mensagem(self, lead_id, direcao, wa_id, conteudo):
        if wa_id and any(m[2] == wa_id for m in self.lead_msgs):
            return False
        self.lead_msgs.append((lead_id, direcao, wa_id, conteudo))
        return True

    def historico_lead(self, lead_id, antes_de_wa_id, limite=10):
        return [(d, c) for l, d, w, c in self.lead_msgs if l == lead_id and w != antes_de_wa_id and c]

    def converter_lead(self, lead_id, nutri_id, nome, telefone, inicio, fim, evento, convenio):
        self.conversoes.append((lead_id, nome, telefone, evento, convenio))
        self.atualizar_lead(lead_id, {"status": "convertido", "nome": nome})
        return "p-novo"


def app_leads(repo, atender, calendario=None, comercial=True):
    cliente = ClienteFalso()
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, comercial=comercial), cliente=cliente, repo=repo,
                    taco=object(), responder_fn=lambda *a, **k: None, atender_lead_fn=atender, calendario=calendario)
    return TestClient(app), cliente


def test_numero_desconhecido_vai_para_o_comercial():
    repo, chamadas = RepoComLeads(), []

    def atender(texto, lead, nutri, **kw):
        chamadas.append((texto, nutri, kw))
        return ResultadoLead(acao="responder", mensagem="Oi! Atendemos Unimed, sim.")

    c, cliente = app_leads(repo, atender)
    repo.nutri.atende_convenio, repo.nutri.convenios_aceitos = True, ["Unimed"]
    c.post(URL, json=evento("Vocês atendem Unimed?"))
    (texto, nutri, kw), = chamadas
    assert texto == "Vocês atendem Unimed?" and nutri.convenios_aceitos == ["Unimed"]
    assert kw["agenda_para"] is None  # sem Google conectado
    assert cliente.enviadas == [("5511999990001", "Oi! Atendemos Unimed, sim.")]
    assert [m[1] for m in repo.lead_msgs] == ["entrada", "saida"]
    # evento repetido não responde de novo
    c.post(URL, json=evento("Vocês atendem Unimed?"))
    assert len(cliente.enviadas) == 1


def test_comercial_desligado_nao_responde_numero_desconhecido():
    repo, chamadas = RepoComLeads(), []
    c, cliente = app_leads(repo, lambda *a, **k: chamadas.append(1), comercial=False)
    c.post(URL, json=evento("oi"))
    assert chamadas == [] and cliente.enviadas == [] and repo.leads == {}


def test_historico_do_lead_vai_para_o_modelo():
    repo, historicos = RepoComLeads(), []

    def atender(texto, lead, nutri, historico=None, **kw):
        historicos.append(historico)
        return ResultadoLead(acao="responder", mensagem=f"resposta a {texto}")

    c, _ = app_leads(repo, atender)
    c.post(URL, json=evento("oi", messageid="A1"))
    c.post(URL, json=evento("quanto custa?", messageid="A2"))
    assert historicos[1] == [{"role": "user", "content": "oi"}, {"role": "assistant", "content": "resposta a oi"}]


def test_mensagem_do_numero_de_aviso_da_nutri_e_ignorada():
    repo, chamadas = RepoComLeads(), []
    c, cliente = app_leads(repo, lambda *a, **k: chamadas.append(1))
    repo.nutri.telefone_aviso = "5511999990001"
    c.post(URL, json=evento("teste"))
    assert chamadas == [] and cliente.enviadas == [] and repo.leads == {}


def test_lead_ja_convertido_nao_e_atendido_pelo_comercial():
    repo, chamadas = RepoComLeads(), []
    repo.lead("n1", "5511999990001")["status"] = "convertido"
    c, cliente = app_leads(repo, lambda *a, **k: chamadas.append(1))
    c.post(URL, json=evento("oi"))
    assert chamadas == [] and cliente.enviadas == []


def test_audio_de_lead_pede_texto_sem_chamar_a_ia():
    repo, chamadas = RepoComLeads(), []
    c, cliente = app_leads(repo, lambda *a, **k: chamadas.append(1))
    c.post(URL, json=evento("", messageType="AudioMessage"))
    assert chamadas == [] and "por texto" in cliente.enviadas[0][1]


def test_encaminhamento_de_lead_avisa_a_nutri():
    repo = RepoComLeads()
    c, cliente = app_leads(repo, lambda *a, **k: ResultadoLead(
        acao="encaminhar", mensagem="Vou passar para a Ana.", motivo="quer desconto", resumo_para_nutri="Pede desconto."))
    c.post(URL, json=evento("faz desconto?"))
    assert cliente.enviadas[0] == ("5511999990001", "Vou passar para a Ana.")
    assert cliente.enviadas[1][0] == "5511988880000" and "Pede desconto." in cliente.enviadas[1][1] and "wa.me/5511999990001" in cliente.enviadas[1][1]


def test_cenario3_pelo_webhook_converte_manda_termo_e_avisa_a_nutri():
    """O modelo simulado lista horários e marca o primeiro: evento no Google, lead convertido,
    termo de consentimento para o novo paciente e aviso para a nutri."""
    repo, cal = RepoComLeads(), CalendarioFalso()

    def atender(texto, lead, nutri, *, agenda_para, atualizar, **kw):
        atualizar({"objetivo": "ganho de massa"})
        lead["objetivo"] = "ganho de massa"
        livre = agenda_para("Novo", "").ver_horarios_livres()["horarios"][0]
        r = agenda_para("Joana Pereira", "Unimed").marcar_consulta(livre["inicio"])
        assert r["marcado"]
        return ResultadoLead(acao="responder", mensagem=f"Marcado: {r['descricao']}",
                             convertido={"nome": "Joana Pereira", "descricao": r["descricao"],
                                         "inicio": r["inicio"], "convenio": "Unimed"})

    c, cliente = app_leads(repo, atender, calendario=cal)
    c.post(URL, json=evento("quero marcar"))
    (evento_google,) = cal.eventos.values()
    assert evento_google["titulo"] == "Primeira consulta – Joana Pereira"
    assert repo.conversoes == [("l1", "Joana Pereira", "5511999990001", "ev1", "Unimed")]
    assert repo.leads["5511999990001"]["status"] == "convertido"
    paciente, termo, aviso = cliente.enviadas
    assert paciente[1].startswith("Marcado:")
    assert termo == ("5511999990001", consentimento.pedido(""))
    assert aviso[0] == "5511988880000" and "Novo paciente" in aviso[1] and "ganho de massa" in aviso[1]


# ---------------------------------------------------------------------
# Resumo das 8h
# ---------------------------------------------------------------------
def test_resumo_mostra_novos_contatos():
    texto = montar_resumo("Ana Souza", {"leads_novos": 3, "leads_convertidos": 1}, date(2026, 9, 29))
    assert "3 novo(s) contato(s), 1 marcou(aram) consulta" in texto
    assert "novo(s) contato(s)" not in montar_resumo("Ana Souza", {}, date(2026, 9, 29))
