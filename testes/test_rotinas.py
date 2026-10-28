"""Rotinas sem internet: uazapi e banco simulados."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from bot.rotinas import (criar_agendador, montar_resumo, rodar_lembretes, rodar_resumos, rodar_retornos,
                         texto_lembrete, texto_retorno)

SP = ZoneInfo("America/Sao_Paulo")
AGORA = datetime(2026, 9, 29, 10, 0, tzinfo=SP)  # terça


class Uazapi:
    def __init__(self, falhar_para=()):
        self.enviadas, self._falhar = [], set(falhar_para)

    def __call__(self, numero, texto):
        if numero in self._falhar:
            raise RuntimeError("uazapi fora do ar")
        self.enviadas.append((numero, texto))
        return f"wa{len(self.enviadas)}"


class RepoFalso:
    def __init__(self, lembretes=(), retornos=(), resumos=(), dados=None):
        self._lembretes, self._retornos, self._resumos = list(lembretes), list(retornos), list(resumos)
        self.dados = dados or {}
        self.desfeitos, self.proativas = [], []

    def reservar_lembretes(self): return self._lembretes
    def desfazer_lembrete(self, aid): self.desfeitos.append(("lembrete", aid))
    def reservar_retornos(self): return self._retornos
    def desfazer_retorno(self, pid, anterior): self.desfeitos.append(("retorno", pid, anterior))
    def reservar_resumos(self): return self._resumos
    def desfazer_resumo(self, nid, anterior): self.desfeitos.append(("resumo", nid, anterior))
    def dados_resumo(self, nid, fuso): return self.dados
    def registrar_proativa(self, nid, pid, texto, motivo, wa_id): self.proativas.append((pid, motivo, wa_id))


LEMBRETE = {"agendamento_id": "ag1", "inicio": datetime(2026, 9, 30, 10, 0, tzinfo=SP), "paciente_id": "p1",
            "nutri_id": "n1", "primeiro_nome": "Ray", "telefone": "5547900000024", "nutri_nome": "Ana Souza"}


# ---------------------------------------------------------------------
# Textos
# ---------------------------------------------------------------------
def test_texto_lembrete_amanha():
    t = texto_lembrete("Ray", "Ana Souza", datetime(2026, 9, 30, 10, 0, tzinfo=SP), AGORA)
    assert t == ("Olá, Ray! Passando para lembrar da sua consulta com a Dra. Ana Souza amanhã, 30/09 às 10:00. "
                 "Você confirma a sua presença? (Responda 'Sim' para confirmar ou avise se precisar remarcar)")


def test_texto_lembrete_perto_da_meia_noite_nao_diz_amanha_errado():
    # 23:50 de terça; consulta quinta 00:10 (24h20 depois) -> não é "amanhã"
    t = texto_lembrete("Ray", "Ana", datetime(2026, 10, 1, 0, 10, tzinfo=SP), datetime(2026, 9, 29, 23, 50, tzinfo=SP))
    assert "quinta, 01/10 às 00:10" in t


def test_lembrete_em_utc_vira_horario_de_brasilia():
    inicio_utc = datetime(2026, 9, 30, 13, 0, tzinfo=ZoneInfo("UTC"))  # 10:00 em Brasília
    assert "às 10:00" in texto_lembrete("Ray", "Ana", inicio_utc, AGORA)


def test_texto_retorno():
    assert texto_retorno("Ray", "Ana Souza").startswith("Olá, Ray! Já faz um tempinho desde a sua última consulta com a Dra. Ana Souza.")


# ---------------------------------------------------------------------
# Rotina 1: lembrete 24 h
# ---------------------------------------------------------------------
def test_lembrete_envia_e_registra_no_historico():
    repo, zap = RepoFalso(lembretes=[LEMBRETE]), Uazapi()
    assert rodar_lembretes(repo, zap, AGORA) == 1
    assert zap.enviadas[0][0] == "5547900000024" and "amanhã, 30/09 às 10:00" in zap.enviadas[0][1]
    assert repo.proativas == [("p1", "lembrete 24 h", "wa1")] and repo.desfeitos == []


def test_lembrete_que_falha_desfaz_a_reserva():
    outro = {**LEMBRETE, "agendamento_id": "ag2", "telefone": "5511999990000"}
    repo, zap = RepoFalso(lembretes=[LEMBRETE, outro]), Uazapi(falhar_para={"5547900000024"})
    assert rodar_lembretes(repo, zap, AGORA) == 1                 # o segundo sai mesmo com o primeiro falhando
    assert repo.desfeitos == [("lembrete", "ag1")]
    assert [p[0] for p in repo.proativas] == ["p1"] and zap.enviadas[0][0] == "5511999990000"


# ---------------------------------------------------------------------
# Rotina 2: cobrança de retorno
# ---------------------------------------------------------------------
RETORNO = {"paciente_id": "p1", "nutri_id": "n1", "primeiro_nome": "Ray", "telefone": "5547900000024",
           "nutri_nome": "Ana Souza", "anterior": None}


def test_retorno_envia_e_registra():
    repo, zap = RepoFalso(retornos=[RETORNO]), Uazapi()
    assert rodar_retornos(repo, zap) == 1
    assert "Vamos agendar o seu retorno" in zap.enviadas[0][1]
    assert repo.proativas == [("p1", "cobrança de retorno", "wa1")]


def test_retorno_que_falha_volta_a_data_anterior():
    anterior = datetime(2026, 8, 1, tzinfo=SP)
    repo = RepoFalso(retornos=[{**RETORNO, "anterior": anterior}])
    assert rodar_retornos(repo, Uazapi(falhar_para={"5547900000024"})) == 0
    assert repo.desfeitos == [("retorno", "p1", anterior)] and repo.proativas == []


# ---------------------------------------------------------------------
# Rotina 3: resumo das 8h
# ---------------------------------------------------------------------
DADOS = {
    "respondidas": 12,
    "encaminhadas": [{"hora": "14:32", "paciente": "Ray", "motivo": "sintoma", "resumo": "Relata enjoo depois do almoço."},
                     {"hora": "18:05", "paciente": "Maria Silva", "motivo": "suplemento", "resumo": ""}],
    "consultas_hoje": [{"hora": "15:00", "paciente": "Ray", "tipo": "retorno", "google_event_id": "ev1"}],
    "avisos_hoje": 2, "avisos_semana": 5,
}


def test_montar_resumo():
    t = montar_resumo("Ana Souza", DADOS, date(2026, 9, 29), [{"hora": "09:00", "titulo": "Retorno Maria (47) 9..."}])
    assert t.startswith("*Bom dia, Ana!* Resumo do assistente (ter 29/09)")
    assert "🟡 *Precisam de você (2)*" in t
    assert "• *Ray* (14:32): Relata enjoo depois do almoço." in t
    assert "• *Maria Silva* (18:05): Suplemento." in t                                       # sem resumo, usa o motivo
    assert "📅 *Hoje: 2 consulta(s)*\n• 09:00 Retorno Maria (47) 9... (sua agenda)\n• 15:00 Ray (retorno)" in t
    assert "🤖 *O assistente cuidou (24 h):* 12 dúvida(s) respondida(s) · 2 convite(s) de retorno hoje (5 na semana)" in t
    # o que precisa dela vem antes da agenda, e a agenda antes dos números
    assert t.index("Precisam de você") < t.index("Hoje:") < t.index("O assistente cuidou")


def test_resumo_poe_urgentes_primeiro():
    enc = [{"hora": "08:10", "paciente": "Bia", "motivo": "troca de alimento", "resumo": "Quer trocar o arroz."},
           {"hora": "21:40", "paciente": "Caio", "motivo": "urgente: sintoma", "resumo": "Tontura e vômito desde ontem."},
           {"hora": "22:00", "paciente": "Duda", "motivo": "check-in semanal: alerta", "resumo": ""}]
    t = montar_resumo("Ana", {**DADOS, "encaminhadas": enc}, date(2026, 9, 29))
    assert "🔴 *Precisam de você (3)*" in t
    assert t.index("🔴 *Caio*") < t.index("🔴 *Duda*") < t.index("*Bia*")


def test_resumo_de_dia_vazio():
    t = montar_resumo("Ana", {"respondidas": 0, "encaminhadas": [], "consultas_hoje": [], "avisos_hoje": 0, "avisos_semana": 0},
                      date(2026, 9, 29))
    assert "✅ Nada pendente para você." in t and "📅 Nenhuma consulta hoje." in t
    assert "convite" not in t   # sem número zerado poluindo


def test_resumo_limita_encaminhadas():
    enc = [{"hora": "10:00", "paciente": f"P{i}", "motivo": "x", "resumo": "y"} for i in range(13)]
    t = montar_resumo("Ana", {**DADOS, "encaminhadas": enc}, date(2026, 9, 29))
    assert "*Precisam de você (13)*" in t and "e mais 3 no painel." in t and "*P10*" not in t


class Calendario:
    def __init__(self, eventos=None, erro=False):
        self._eventos, self._erro = eventos or [], erro

    def listar_eventos(self, cal_id, inicio, fim):
        if self._erro:
            raise RuntimeError("503")
        return self._eventos


NUTRI = {"nutri_id": "n1", "nome": "Ana Souza", "telefone_aviso": "5551900000022", "fuso_horario": "America/Sao_Paulo",
         "google_calendar_id": "cal-1", "anterior": None}


def test_resumo_envia_para_a_nutri_com_eventos_manuais_sem_duplicar():
    cal = Calendario([{"id": "ev1", "titulo": "Consulta – Ray", "inicio": datetime(2026, 9, 29, 15, 0, tzinfo=SP)},   # do bot
                      {"id": "m1", "titulo": "Retorno Maria", "inicio": datetime(2026, 9, 29, 9, 0, tzinfo=SP)}])
    repo, zap = RepoFalso(resumos=[NUTRI], dados=DADOS), Uazapi()
    assert rodar_resumos(repo, zap, cal, datetime(2026, 9, 29, 8, 0, tzinfo=SP)) == 1
    numero, texto = zap.enviadas[0]
    assert numero == "5551900000022" and "09:00 Retorno Maria (sua agenda)" in texto
    assert texto.count("15:00") == 1


def test_resumo_sai_mesmo_com_google_fora_do_ar():
    repo, zap = RepoFalso(resumos=[NUTRI], dados=DADOS), Uazapi()
    assert rodar_resumos(repo, zap, Calendario(erro=True), datetime(2026, 9, 29, 8, 0, tzinfo=SP)) == 1


def test_resumo_que_falha_desfaz_reserva():
    repo = RepoFalso(resumos=[NUTRI], dados=DADOS)
    assert rodar_resumos(repo, Uazapi(falhar_para={"5551900000022"}), None, datetime(2026, 9, 29, 8, 0, tzinfo=SP)) == 0
    assert repo.desfeitos == [("resumo", "n1", None)]


# ---------------------------------------------------------------------
# Agendador
# ---------------------------------------------------------------------
def test_agendador_tem_as_tres_rotinas_em_sao_paulo():
    ag = criar_agendador(RepoFalso(), Uazapi())
    jobs = {j.id: j for j in ag.get_jobs()}
    assert set(jobs) == {"lembrete_24h", "cobranca_retorno", "resumo_8h", "checkin_semanal", "fim_acompanhamento"}
    assert "hour='10'" in str(jobs["fim_acompanhamento"].trigger) and "minute='30'" in str(jobs["fim_acompanhamento"].trigger)
    assert jobs["checkin_semanal"].trigger.interval == timedelta(minutes=15)
    assert str(ag.timezone) == "America/Sao_Paulo"
    assert jobs["lembrete_24h"].trigger.interval == timedelta(minutes=15)
    resumo = str(jobs["resumo_8h"].trigger)
    assert "day_of_week='mon-fri'" in resumo and "hour='8'" in resumo and "minute='0'" in resumo
    assert "hour='10'" in str(jobs["cobranca_retorno"].trigger)
    # próximo resumo a partir de sexta 09:00 é segunda 08:00
    prox = jobs["resumo_8h"].trigger.get_next_fire_time(None, datetime(2026, 10, 2, 9, 0, tzinfo=SP))
    assert prox == datetime(2026, 10, 5, 8, 0, tzinfo=SP)


def test_app_so_liga_rotinas_quando_pedido():
    from fastapi.testclient import TestClient
    from whatsapp.app import Config, criar_app

    class Cli:
        def enviar_texto(self, n, t): return "x"

    app = criar_app(Config("eco", "https://x", "tok", "segredo-bem-comprido-123"), cliente=Cli())
    assert TestClient(app).get("/saude").json()["rotinas"] == []
