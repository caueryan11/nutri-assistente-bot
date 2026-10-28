"""Agenda sem internet: Google e banco simulados."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agenda.horarios import descrever, sugerir, todos_os_horarios
from agenda.servico import AgendaNutri, HorarioOcupado

SP = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 9, 29, 9, 0, tzinfo=SP)  # terça, 09:00
CONFIG = {"duracao_min": 60, "antecedencia_horas": 12, "janela_dias": 7,
          "atendimento": {"1": ["08:00-12:00"], "2": ["08:00-12:00"], "3": ["08:00-12:00", "14:00-16:00"],
                          "4": ["08:00-12:00"], "5": ["08:00-12:00"]}}


def h(dia, hora, minuto=0):
    return datetime(2026, 9, dia, hora, minuto, tzinfo=SP) if dia > 20 else datetime(2026, 10, dia, hora, minuto, tzinfo=SP)


# ---------------------------------------------------------------------
# horários
# ---------------------------------------------------------------------
def test_respeita_antecedencia_e_dias_de_atendimento():
    livres = todos_os_horarios(CONFIG, [], AGORA, SP)
    assert livres[0] == h(30, 8)                      # terça 09:00 + 12 h -> quarta 08:00
    assert all(t.isoweekday() <= 5 for t in livres)   # sem fim de semana
    assert h(30, 14) in livres and h(30, 15) in livres and h(30, 16) not in livres  # bloco da tarde de quarta


def test_desconta_ocupados_inclusive_parciais():
    ocupados = [(h(30, 9, 30), h(30, 10, 15))]         # evento da nutri no meio de dois horários
    livres = todos_os_horarios(CONFIG, ocupados, AGORA, SP)
    assert h(30, 9) not in livres and h(30, 10) not in livres and h(30, 11) in livres


def test_periodo_e_a_partir_de():
    livres = todos_os_horarios(CONFIG, [], AGORA, SP, a_partir_de=date(2026, 10, 1), periodo="manha")
    assert livres[0] == h(1, 8) and all(t.hour < 12 for t in livres)
    tarde = todos_os_horarios(CONFIG, [], AGORA, SP, periodo="tarde")
    assert tarde and all(t.isoweekday() == 3 and 14 <= t.hour < 16 for t in tarde)


def test_sugerir_espalha_por_dia():
    s = sugerir(todos_os_horarios(CONFIG, [], AGORA, SP), maximo=4, por_dia=2)
    assert len(s) == 4 and len({t.date() for t in s}) == 2


def test_descrever():
    assert descrever(h(30, 10)) == "qua 30/09 às 10:00"


# ---------------------------------------------------------------------
# serviço (Google + banco simulados)
# ---------------------------------------------------------------------
class CalendarioFalso:
    def __init__(self, ocupados=(), manuais=()):
        """manuais: eventos criados à mão pela nutri [(id, titulo, descricao, inicio, fim)]."""
        self.eventos, self.cancelados, self._ocupados = {}, [], list(ocupados)
        for eid, titulo, descricao, ini, fim in manuais:
            self.eventos[eid] = {"ini": ini, "fim": fim, "titulo": titulo, "descricao": descricao}

    def listar_eventos(self, calendar_id, inicio, fim):
        return [{"id": eid, "titulo": e["titulo"], "descricao": e["descricao"], "inicio": e["ini"], "fim": e["fim"]}
                for eid, e in self.eventos.items() if inicio <= e["ini"] < fim]

    def ocupados(self, calendar_id, inicio, fim):
        return self._ocupados + [(e["ini"], e["fim"]) for e in self.eventos.values()]

    def criar_evento(self, calendar_id, inicio, fim, titulo, descricao="", fuso="America/Sao_Paulo"):
        eid = f"ev{sum(1 for k in self.eventos if k.startswith('ev')) + 1}"
        self.eventos[eid] = {"ini": inicio, "fim": fim, "titulo": titulo, "descricao": descricao}
        return eid

    def cancelar_evento(self, calendar_id, evento_id):
        self.cancelados.append(evento_id)
        self.eventos.pop(evento_id, None)


def agenda(cal=None, gravar=None):
    gravados = []
    return AgendaNutri(cal or CalendarioFalso(), "cal-1", CONFIG, "America/Sao_Paulo", "Ray", "5547900000024",
                       gravar=gravar or (lambda i, f, e: gravados.append((i, f, e))), agora=lambda: AGORA), gravados


def test_ver_horarios_formata_para_o_bot():
    a, _ = agenda()
    r = a.ver_horarios_livres()
    assert r["horarios"][0] == {"inicio": "2026-09-30T08:00", "descricao": "qua 30/09 às 08:00"}
    assert len(r["horarios"]) <= 6


def test_marcar_cria_evento_e_grava():
    cal = CalendarioFalso()
    a, gravados = agenda(cal)
    r = a.marcar_consulta("2026-09-30T10:00")
    assert r["marcado"] and r["descricao"] == "qua 30/09 às 10:00"
    ev = cal.eventos["ev1"]
    assert ev["ini"] == h(30, 10) and ev["fim"] == h(30, 11) and "Ray" in ev["titulo"]
    assert gravados == [(h(30, 10), h(30, 11), "ev1")]
    # o mesmo horário não aparece mais e não pode ser marcado de novo
    assert "2026-09-30T10:00" not in [x["inicio"] for x in a.ver_horarios_livres()["horarios"]]
    assert a.marcar_consulta("2026-09-30T10:00")["marcado"] is False


@pytest.mark.parametrize("inicio", ["2026-09-30T10:30", "2026-10-03T10:00", "2026-09-29T10:00", "amanhã 10h"])
def test_marcar_recusa_horario_invalido(inicio):
    # fora da grade, sábado, antes da antecedência mínima, formato errado
    a, gravados = agenda()
    assert a.marcar_consulta(inicio)["marcado"] is False and gravados == []


def test_conflito_no_banco_desfaz_evento():
    cal = CalendarioFalso()

    def gravar(i, f, e):
        raise HorarioOcupado()

    a, _ = agenda(cal, gravar)
    r = a.marcar_consulta("2026-09-30T10:00")
    assert r["marcado"] is False and cal.cancelados == ["ev1"] and cal.eventos == {}


# ---------------------------------------------------------------------
# bot usando a agenda (API simulada)
# ---------------------------------------------------------------------
def test_bot_marca_e_devolve_agendamento():
    from testes.test_unidade import PACIENTE, PLANO, ClienteFalso, _chamada, _resposta
    from bot.assistente import responder

    a, gravados = agenda()
    final = {"acao": "responder", "mensagem_paciente": "Marcado: qua 30/09 às 10:00.", "motivo": "", "resumo_para_nutri": ""}
    cliente = ClienteFalso([
        _resposta(_chamada("marcar_consulta", {"inicio": "2026-09-30T10:00"})),
        _resposta(_chamada("enviar_resposta", final, "t2")),
    ])
    r = responder("Pode ser quarta às 10", PACIENTE, PLANO, taco=None, cliente=cliente, agenda=a)
    assert r.agendamento["descricao"] == "qua 30/09 às 10:00" and len(gravados) == 1
    nomes = [f["name"] for f in cliente.pedidos[0]["tools"]]
    assert "ver_horarios_livres" in nomes and "marcar_consulta" in nomes
    assert "## Agenda" in cliente.pedidos[0]["system"][0]["text"]


def test_bot_sem_agenda_nao_oferece_ferramentas_de_agenda():
    from testes.test_unidade import PACIENTE, PLANO, ClienteFalso, _chamada, _resposta
    from bot.assistente import responder

    final = {"acao": "encaminhar", "mensagem_paciente": "Avisei a Ana.", "motivo": "agenda", "resumo_para_nutri": "Quer marcar."}
    cliente = ClienteFalso([_resposta(_chamada("enviar_resposta", final))])
    responder("Quero marcar", PACIENTE, PLANO, taco=None, cliente=cliente)
    assert "marcar_consulta" not in [f["name"] for f in cliente.pedidos[0]["tools"]]


def test_google_fora_do_ar_nao_derruba_o_bot():
    from testes.test_unidade import PACIENTE, PLANO, ClienteFalso, _chamada, _resposta
    from bot.assistente import responder

    class Quebrado(CalendarioFalso):
        def ocupados(self, *a):
            raise RuntimeError("503")

    a, _ = agenda(Quebrado())
    final = {"acao": "encaminhar", "mensagem_paciente": "Avisei a Ana.", "motivo": "agenda fora", "resumo_para_nutri": "Quer marcar."}
    cliente = ClienteFalso([_resposta(_chamada("ver_horarios_livres", {})), _resposta(_chamada("enviar_resposta", final, "t2"))])
    r = responder("Quero marcar", PACIENTE, PLANO, taco=None, cliente=cliente, agenda=a)
    resultado = cliente.pedidos[1]["messages"][-1]["content"][0]
    assert resultado["is_error"] is True and "indisponível" in resultado["content"] and r.acao == "encaminhar"


# ---------------------------------------------------------------------
# remarcar e cancelar
# ---------------------------------------------------------------------
class BancoFalso:
    """Imita public.agendamentos de UM paciente."""

    def __init__(self):
        self.linhas = {}

    def gravar(self, ini, fim, evento):
        aid = f"ag{len(self.linhas) + 1}"
        self.linhas[aid] = {"id": aid, "inicio": ini, "fim": fim, "google_event_id": evento, "status": "confirmado"}

    def listar(self):
        return [dict(l) for l in self.linhas.values() if l["status"] == "confirmado" and l["inicio"] > AGORA]

    def cancelar(self, aid):
        self.linhas[aid]["status"] = "cancelado"


def agenda_com_banco(cal=None):
    banco, cal = BancoFalso(), cal or CalendarioFalso()
    a = AgendaNutri(cal, "cal-1", CONFIG, "America/Sao_Paulo", "Ray", "5547900000024", gravar=banco.gravar,
                    agora=lambda: AGORA, listar=banco.listar, cancelar_registro=banco.cancelar)
    return a, banco, cal


def test_minhas_consultas():
    a, banco, _ = agenda_com_banco()
    assert a.minhas_consultas()["consultas"] == [] and "encaminhe" in a.minhas_consultas()["aviso"]
    a.marcar_consulta("2026-10-01T09:00")
    a.marcar_consulta("2026-09-30T10:00")
    c = a.minhas_consultas()["consultas"]
    assert [x["descricao"] for x in c] == ["qua 30/09 às 10:00", "qui 01/10 às 09:00"]  # em ordem
    assert c[0]["agendamento_id"] == "ag2"


def test_remarcar_marca_o_novo_e_cancela_o_antigo():
    a, banco, cal = agenda_com_banco()
    a.marcar_consulta("2026-09-30T10:00")
    r = a.remarcar_consulta("ag1", "2026-10-01T08:00")
    assert r == {"remarcado": True, "de": "qua 30/09 às 10:00", "para": "qui 01/10 às 08:00", "inicio": "2026-10-01T08:00",
                 "manual": False}
    assert banco.linhas["ag1"]["status"] == "cancelado" and banco.linhas["ag2"]["status"] == "confirmado"
    assert list(cal.eventos) == ["ev2"] and cal.cancelados == ["ev1"]      # evento antigo sumiu do Google
    assert h(30, 10) in a._livres()                                         # horário antigo liberado


def test_remarcar_para_horario_ocupado_mantem_a_consulta_antiga():
    a, banco, cal = agenda_com_banco(CalendarioFalso(ocupados=[(h(1, 8), h(1, 9))]))
    a.marcar_consulta("2026-09-30T10:00")
    r = a.remarcar_consulta("ag1", "2026-10-01T08:00")
    assert r["remarcado"] is False
    assert banco.linhas["ag1"]["status"] == "confirmado" and list(cal.eventos) == ["ev1"] and cal.cancelados == []


def test_cancelar():
    a, banco, cal = agenda_com_banco()
    a.marcar_consulta("2026-09-30T10:00")
    assert a.cancelar_consulta("ag1") == {"cancelado": True, "descricao": "qua 30/09 às 10:00", "manual": False}
    assert banco.linhas["ag1"]["status"] == "cancelado" and cal.eventos == {}


def test_nao_mexe_em_consulta_que_nao_e_do_paciente():
    a, banco, cal = agenda_com_banco()
    assert a.cancelar_consulta("id-de-outro-paciente")["cancelado"] is False
    assert a.remarcar_consulta("id-de-outro-paciente", "2026-10-01T08:00")["remarcado"] is False
    assert cal.eventos == {}                                              # nem marcou o novo


def test_evento_ja_apagado_pela_nutri_ainda_cancela_no_banco():
    import httpx

    class JaApagado(CalendarioFalso):
        def cancelar_evento(self, calendar_id, evento_id):
            raise httpx.HTTPStatusError("gone", request=httpx.Request("DELETE", "x"), response=httpx.Response(410))

    a, banco, _ = agenda_com_banco(JaApagado())
    a.marcar_consulta("2026-09-30T10:00")
    assert a.cancelar_consulta("ag1")["cancelado"] is True and banco.linhas["ag1"]["status"] == "cancelado"


def test_bot_remarca_e_devolve_agendamento_para_aviso():
    from testes.test_unidade import PACIENTE, PLANO, ClienteFalso, _chamada, _resposta
    from bot.assistente import responder

    a, banco, _ = agenda_com_banco()
    a.marcar_consulta("2026-09-30T10:00")
    final = {"acao": "responder", "mensagem_paciente": "Remarcada para qui 01/10 às 08:00.", "motivo": "", "resumo_para_nutri": ""}
    cliente = ClienteFalso([
        _resposta(_chamada("remarcar_consulta", {"agendamento_id": "ag1", "novo_inicio": "2026-10-01T08:00"})),
        _resposta(_chamada("enviar_resposta", final, "t2")),
    ])
    r = responder("pode ser quinta às 8", PACIENTE, PLANO, taco=None, cliente=cliente, agenda=a)
    assert r.agendamento["tipo"] == "remarcado" and r.agendamento["de"] == "qua 30/09 às 10:00"
    nomes = [f["name"] for f in cliente.pedidos[0]["tools"]]
    assert {"minhas_consultas", "remarcar_consulta", "cancelar_consulta"} <= set(nomes)


# ---------------------------------------------------------------------
# eventos criados à mão pela nutri no Google
# ---------------------------------------------------------------------
from agenda.servico import telefone_no_texto  # noqa: E402


@pytest.mark.parametrize("texto", [
    "Consulta Ray (47) 90000-0024", "Retorno - 47900000024", "tel: +55 47 90000 0024",
    "Ray 4700000024 (sem o 9)", "obs / whats 55 47 9 0000-0024"])
def test_reconhece_telefone_do_paciente_em_qualquer_formato(texto):
    assert telefone_no_texto(texto, "5547900000024")


@pytest.mark.parametrize("texto", ["Consulta Ray", "Consulta 90000-0024 (sem DDD)", "(11) 90000-0025", "47 90000-0026"])
def test_nao_confunde_com_outro_numero_ou_sem_ddd(texto):
    assert not telefone_no_texto(texto, "5547900000024")


def agenda_com_manual(*manuais):
    cal = CalendarioFalso(manuais=manuais)
    banco = BancoFalso()
    a = AgendaNutri(cal, "cal-1", CONFIG, "America/Sao_Paulo", "Ray", "5547900000024", gravar=banco.gravar,
                    agora=lambda: AGORA, listar=banco.listar, cancelar_registro=banco.cancelar)
    return a, banco, cal


def test_minhas_consultas_inclui_evento_manual_com_telefone():
    a, banco, cal = agenda_com_manual(
        ("m1", "Consulta Ray (47) 90000-0024", "", h(1, 9), h(1, 10)),
        ("m2", "Consulta outra paciente", "(11) 91234-5678", h(1, 10), h(1, 11)),   # de outra pessoa
        ("m3", "Reunião", "", h(2, 9), h(2, 10)))                                   # sem telefone
    a.marcar_consulta("2026-09-30T10:00")
    c = a.minhas_consultas()["consultas"]
    assert [x["descricao"] for x in c] == ["qua 30/09 às 10:00", "qui 01/10 às 09:00"]
    assert c[1]["agendamento_id"] == "g:m1"


def test_evento_do_bot_nao_aparece_duplicado():
    a, banco, cal = agenda_com_manual()
    a.marcar_consulta("2026-09-30T10:00")    # descrição do evento do bot também tem o telefone
    assert len(a.minhas_consultas()["consultas"]) == 1


def test_remarcar_evento_manual():
    a, banco, cal = agenda_com_manual(("m1", "Consulta Ray (47) 90000-0024", "", h(1, 9), h(1, 10)))
    r = a.remarcar_consulta("g:m1", "2026-10-02T08:00")
    assert r["remarcado"] and r["manual"] is True and r["de"] == "qui 01/10 às 09:00"
    assert "m1" not in cal.eventos and cal.cancelados == ["m1"]                  # evento manual apagado
    assert [l["inicio"] for l in banco.linhas.values()] == [h(2, 8)]            # novo fica registrado no banco


def test_cancelar_evento_manual():
    a, banco, cal = agenda_com_manual(("m1", "Retorno", "whats 47 90000-0024", h(1, 9), h(1, 10)))
    r = a.cancelar_consulta("g:m1")
    assert r == {"cancelado": True, "descricao": "qui 01/10 às 09:00", "manual": True}
    assert cal.eventos == {} and banco.linhas == {}


def test_nao_cancela_evento_manual_de_outro_paciente():
    a, banco, cal = agenda_com_manual(("m2", "Consulta", "(11) 91234-5678", h(1, 10), h(1, 11)))
    assert a.cancelar_consulta("g:m2")["cancelado"] is False and "m2" in cal.eventos
