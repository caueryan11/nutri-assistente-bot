"""Consultas de métricas contra o Postgres de verdade, numa transação desfeita no final.

Rodar:  RODAR_TESTES_BANCO=1 python -m pytest testes/test_metricas_banco.py
As migrações comercial e de métricas são aplicadas dentro da transação. Nada fica gravado.
"""

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from testes.test_rotinas_banco import RAIZ, ConexaoTransacao

pytestmark = pytest.mark.skipif(os.environ.get("RODAR_TESTES_BANCO") != "1", reason="defina RODAR_TESTES_BANCO=1")

N = "eeeeeeee-1111-0000-0000-000000000000"
P = "eeeeeeee-2222-0000-0000-0000000000{:02d}"
SP = ZoneInfo("America/Sao_Paulo")


def _dias_da_semana():
    """Um dia útil e um dia de fim de semana entre 1 e 6 dias atrás (sempre existem)."""
    hoje = datetime.now(SP).date()
    dias = [hoje - timedelta(days=i) for i in range(1, 7)]
    return next(d for d in dias if d.isoweekday() <= 5), next(d for d in dias if d.isoweekday() >= 6)


@pytest.fixture(scope="module")
def repo():
    import psycopg2
    from dotenv import dotenv_values

    from bot.metricas import RepositorioMetricasPostgres

    conn = psycopg2.connect(dotenv_values(RAIZ / ".env")["DATABASE_URL"])
    db = ConexaoTransacao(conn)
    for m in ("20260929120000_consentimento.sql", "20260929140000_rotinas.sql", "20260929160000_comercial.sql",
              "20260929180000_metricas.sql"):
        db.executar((RAIZ / "supabase" / "migrations" / m).read_text(encoding="utf-8"))
    db.executar("insert into public.nutricionistas (id, nome, telefone_aviso, valor_consulta) "
                "values (%s, 'Nutri Teste Métricas', '5511900000077', 250)", (N,))
    for i in range(1, 6):
        db.executar("insert into public.pacientes (id, nutricionista_id, nome, telefone, consentimento_em) "
                    "values (%s, %s, %s, %s, now())", (P.format(i), N, f"Paciente{i} Teste", f"55119990077{i:02d}"))

    util, fds = _dias_da_semana()

    def em(dia, hora):
        return datetime(dia.year, dia.month, dia.day, hora, 0, tzinfo=SP)

    def msg(pac, quando, acao=None, motivo=None):
        db.executar("insert into public.mensagens (nutricionista_id, paciente_id, direcao, tipo, conteudo, acao, motivo, criado_em) "
                    "values (%s, %s, 'saida', 'texto', 'x', %s, %s, %s)", (N, P.format(pac), acao, motivo, quando))

    msg(1, em(util, 10), "responder")                                    # dentro do horário
    msg(1, em(util, 22), "responder")                                    # fora: noite
    msg(2, em(fds, 11), "responder")                                     # fora: fim de semana
    msg(2, em(util, 10), "responder", "consentimento aceito (termo v1.0)")  # não é dúvida
    msg(3, datetime.now(SP) - timedelta(days=20), "responder")           # só nos 30 dias
    msg(1, em(util, 15), "encaminhar", "pediu troca de suplemento")
    msg(3, em(fds, 9), "encaminhar", "sintoma")
    msg(4, em(util, 9), None, "lembrete 24 h")                           # proativa: não conta
    msg(4, datetime.now(SP) - timedelta(days=5), None, "cobrança de retorno")

    def consulta(pac, dias_frente, criado_ha, tipo="retorno", status="confirmado", origem="bot"):
        db.executar("insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim, tipo, status, origem, criado_em) "
                    "values (%s, %s, now() + %s * interval '1 day', now() + %s * interval '1 day' + interval '1 hour', "
                    "%s, %s, %s, now() - %s * interval '1 day')",
                    (N, P.format(pac), dias_frente, dias_frente, tipo, status, origem, criado_ha))

    consulta(4, 3, 3)                                  # retorno depois da cobrança: resgatado
    consulta(5, 4, 2)                                  # retorno sem cobrança
    consulta(1, 5, 2, tipo="primeira")                 # primeira consulta
    consulta(2, 6, 1, tipo="primeira", status="cancelado")  # cancelada: não conta
    consulta(3, 7, 1, origem="nutri")                  # marcada pela nutri: não conta
    consulta(5, 8, 20, tipo="primeira")                # só nos 30 dias

    for i, (status, criado_ha) in enumerate([("convertido", 1), ("em_triagem", 2), ("aguardando_escolha_horario", 3),
                                             ("desistiu", 20)]):
        db.executar("insert into public.leads (nutricionista_id, telefone, status, criado_em) "
                    "values (%s, %s, %s, now() - %s * interval '1 day')", (N, f"55519990077{i:02d}", status, criado_ha))
    yield RepositorioMetricasPostgres(db)
    conn.rollback()
    conn.close()


def test_metricas_7_dias(repo):
    m = repo.metricas(N, 7)
    assert (m.duvidas_respondidas, m.duvidas_fora_horario, m.duvidas_encaminhadas) == (3, 2, 2)
    assert (m.novos_leads, m.leads_convertidos, m.taxa_conversao) == (3, 1, 33.3)
    assert (m.primeiras_consultas, m.retornos_agendados, m.retornos_recuperados) == (1, 2, 1)
    assert m.valor_consulta == 250.0 and m.receita_estimada == 750.0
    assert m.ultimas_encaminhadas == [] and m.ultimas_consultas == []    # só com detalhes=True


def test_metricas_30_dias(repo):
    m = repo.metricas(N, 30)
    assert m.duvidas_respondidas == 4 and m.novos_leads == 4 and m.primeiras_consultas == 2
    assert m.receita_estimada == 1000.0


def test_detalhes_para_o_painel(repo):
    m = repo.metricas(N, 7, detalhes=True)
    assert [e["motivo"] for e in m.ultimas_encaminhadas] and {e["motivo"] for e in m.ultimas_encaminhadas} == {
        "pediu troca de suplemento", "sintoma"}
    assert all(e["paciente"].startswith("Paciente") for e in m.ultimas_encaminhadas)
    assert sorted(c["tipo"] for c in m.ultimas_consultas) == ["primeira", "retorno", "retorno"]
    assert m.ultimas_consultas[0]["marcada_em"] >= m.ultimas_consultas[-1]["marcada_em"]


def test_nutri_e_nutri_inexistente(repo):
    assert repo.nutri(N)["nome"] == "Nutri Teste Métricas"
    assert repo.nutri("eeeeeeee-9999-0000-0000-000000000000") is None


def test_reserva_semanal_uma_vez_por_dia(repo):
    primeira = [r for r in repo.reservar_semanais() if r["nutri_id"] == N]
    assert len(primeira) == 1 and primeira[0]["anterior"] is None
    assert [r for r in repo.reservar_semanais() if r["nutri_id"] == N] == []
    repo.desfazer_semanal(N, None)
    assert [r["nutri_id"] for r in repo.reservar_semanais() if r["nutri_id"] == N] == [N]
