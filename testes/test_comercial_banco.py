"""SQL dos leads contra o Postgres de verdade, numa transação desfeita no final.

Rodar:  RODAR_TESTES_BANCO=1 python -m pytest testes/test_comercial_banco.py
A migração comercial é aplicada dentro da transação. Nada fica gravado.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from testes.test_rotinas_banco import RAIZ, ConexaoTransacao

pytestmark = pytest.mark.skipif(os.environ.get("RODAR_TESTES_BANCO") != "1", reason="defina RODAR_TESTES_BANCO=1")

N = "dddddddd-1111-0000-0000-000000000000"
TEL = "5551999990077"


@pytest.fixture(scope="module")
def repo():
    import psycopg2
    from dotenv import dotenv_values

    from whatsapp.repositorio import RepositorioPostgres

    conn = psycopg2.connect(dotenv_values(RAIZ / ".env")["DATABASE_URL"])
    db = ConexaoTransacao(conn)
    for m in ("20260929120000_consentimento.sql", "20260929140000_rotinas.sql", "20260929160000_comercial.sql"):
        db.executar((RAIZ / "supabase" / "migrations" / m).read_text(encoding="utf-8"))
    db.executar("insert into public.nutricionistas (id, nome, telefone_aviso, whatsapp_phone_number_id, atende_convenio, "
                "convenios_aceitos, valor_consulta) values (%s, 'Nutri Teste Leads', '5511900000000', 'inst-teste-leads', "
                "true, '{Unimed}', 250)", (N,))
    r = RepositorioPostgres.__new__(RepositorioPostgres)
    r._db = db
    yield r
    conn.rollback()
    conn.close()


def test_nutri_traz_dados_comerciais(repo):
    n = repo.nutri_por_instancia("inst-teste-leads")
    assert n.atende_convenio is True and n.convenios_aceitos == ["Unimed"] and float(n.valor_consulta) == 250


def test_lead_e_criado_uma_vez_e_atualizado(repo):
    a = repo.lead(N, TEL)
    b = repo.lead(N, TEL)
    assert a["id"] == b["id"] and a["status"] == "em_triagem"
    repo.atualizar_lead(a["id"], {"nome": "Joana", "objetivo": "emagrecimento", "coisa_estranha": "x"})
    assert repo.lead(N, TEL)["objetivo"] == "emagrecimento"


def test_mensagens_do_lead_sem_duplicar(repo):
    lead = repo.lead(N, TEL)
    assert repo.registrar_lead_mensagem(lead["id"], "entrada", "wa-lead-1", "oi") is True
    assert repo.registrar_lead_mensagem(lead["id"], "entrada", "wa-lead-1", "oi") is False
    repo.registrar_lead_mensagem(lead["id"], "saida", "wa-lead-2", "Olá!")
    repo.registrar_lead_mensagem(lead["id"], "entrada", "wa-lead-3", "quanto custa?")
    assert repo.historico_lead(lead["id"], "wa-lead-3") == [("entrada", "oi"), ("saida", "Olá!")]


def test_converter_cria_paciente_consulta_e_marca_lead(repo):
    from agenda.servico import HorarioOcupado

    lead = repo.lead(N, TEL)
    ini = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    pid = repo.converter_lead(lead["id"], N, "Joana Pereira", TEL, ini, ini + timedelta(hours=1), "ev-teste", "Unimed")

    pac = repo.paciente(N, TEL)
    assert pac.id == pid and pac.nome == "Joana Pereira" and pac.consentimento_em is None  # termo vem depois
    ag = repo._db.executar("select tipo, status, convenio, google_event_id from public.agendamentos where paciente_id = %s",
                           (pid,), retorno="um", como_dict=True)
    assert dict(ag) == {"tipo": "primeira", "status": "confirmado", "convenio": "Unimed", "google_event_id": "ev-teste"}
    l = repo._db.executar("select status, paciente_id::text as pid from public.leads where id = %s", (lead["id"],),
                          retorno="um", como_dict=True)
    assert l["status"] == "convertido" and l["pid"] == pid
    # convertido não volta para triagem
    repo.atualizar_lead(lead["id"], {"status": "em_triagem"})
    assert repo.lead(N, TEL)["status"] == "convertido"

    # outro lead no mesmo horário: o banco recusa e nada é gravado
    outro = repo.lead(N, "5551999990088")
    repo._db.executar("savepoint s")
    with pytest.raises(HorarioOcupado):
        repo.converter_lead(outro["id"], N, "Outra Pessoa", "5551999990088", ini, ini + timedelta(hours=1), "ev-2", "")
    repo._db.executar("rollback to savepoint s")
    assert repo.paciente(N, "5551999990088") is None


def test_resumo_conta_leads(repo):
    from bot.rotinas import RepositorioRotinasPostgres

    rot = RepositorioRotinasPostgres.__new__(RepositorioRotinasPostgres)
    rot._db = repo._db
    d = rot.dados_resumo(N, "America/Sao_Paulo")
    assert d["leads_novos"] == 2 and d["leads_convertidos"] == 1
