"""Revogação e retenção contra o Postgres de verdade, numa transação desfeita no final.

Rodar:  RODAR_TESTES_BANCO=1 python -m pytest testes/test_lgpd_banco.py
"""

import os

import pytest

from testes.test_rotinas_banco import RAIZ, ConexaoTransacao

pytestmark = pytest.mark.skipif(os.environ.get("RODAR_TESTES_BANCO") != "1", reason="defina RODAR_TESTES_BANCO=1")

N = "ffffffff-1111-0000-0000-000000000000"
P = "ffffffff-2222-0000-0000-000000000001"


@pytest.fixture(scope="module")
def db():
    import psycopg2
    from dotenv import dotenv_values

    conn = psycopg2.connect(dotenv_values(RAIZ / ".env")["DATABASE_URL"])
    db = ConexaoTransacao(conn)
    for m in ("20260929120000_consentimento.sql", "20260929140000_rotinas.sql", "20260929160000_comercial.sql",
              "20260929180000_metricas.sql", "20260929200000_anamnese.sql", "20260929220000_lgpd_operacao.sql"):
        db.executar((RAIZ / "supabase" / "migrations" / m).read_text(encoding="utf-8"))
    db.executar("insert into public.nutricionistas (id, nome, telefone_aviso) values (%s, 'Nutri Teste LGPD', '5511900000088')", (N,))
    db.executar("insert into public.pacientes (id, nutricionista_id, nome, telefone, consentimento_em, versao_termo) "
                "values (%s, %s, 'Paciente LGPD', '5511900000030', now(), 'v1.0')", (P, N))
    yield db
    conn.rollback()
    conn.close()


def _repo(db):
    from whatsapp.repositorio import RepositorioPostgres

    r = RepositorioPostgres.__new__(RepositorioPostgres)
    r._db = db
    return r


def _status(db):
    return dict(db.executar("select consentimento_em, status_consentimento, consentimento_revogado_em, versao_termo "
                            "from public.pacientes where id = %s", (P,), retorno="um", como_dict=True))


def test_revogar_e_aceitar_de_novo(db):
    repo = _repo(db)
    assert repo.paciente(N, "5511900000030").status_consentimento == "aceito"
    assert repo.registrar_revogacao(P) is True
    s = _status(db)
    assert s["consentimento_em"] is None and s["status_consentimento"] == "revogado"   # o gatilho não desfaz
    assert s["consentimento_revogado_em"] is not None
    assert repo.registrar_revogacao(P) is False                                          # repetido
    assert repo.paciente(N, "5511900000030").status_consentimento == "revogado"
    assert repo.registrar_consentimento(P, "v1.0") is True
    s = _status(db)
    assert s["status_consentimento"] == "aceito" and s["consentimento_em"] is not None
    assert s["consentimento_revogado_em"] is not None                                    # histórico fica


def test_retencao_apaga_so_o_antigo(db):
    from bot.rotinas import RepositorioRotinasPostgres

    db.executar("insert into public.mensagens (nutricionista_id, paciente_id, direcao, tipo, conteudo, criado_em) values "
                "(%s, %s, 'entrada', 'texto', 'velha', now() - interval '200 days'), "
                "(%s, %s, 'entrada', 'texto', 'nova', now() - interval '10 days')", (N, P, N, P))
    for tel, status, dias in [("5551900000031", "em_triagem", 100), ("5551900000032", "em_triagem", 10),
                              ("5551900000033", "convertido", 100)]:
        db.executar("insert into public.leads (nutricionista_id, telefone, status, criado_em) "
                    "values (%s, %s, %s, now() - %s * interval '1 day')", (N, tel, status, dias))
        # o gatilho põe atualizado_em = now() no update; aqui o insert usa o default, então ajusta direto
    db.executar("alter table public.leads disable trigger leads_atualizado")
    db.executar("update public.leads set atualizado_em = criado_em where nutricionista_id = %s", (N,))
    db.executar("alter table public.leads enable trigger leads_atualizado")
    lead_velho = db.executar("select id from public.leads where telefone = '5551900000031'", retorno="um")[0]
    db.executar("insert into public.lead_mensagens (lead_id, direcao, conteudo, criado_em) "
                "values (%s, 'entrada', 'oi', now() - interval '100 days')", (lead_velho,))

    apagados = RepositorioRotinasPostgres(db).limpar_antigos(180, 90)
    assert apagados["mensagens"] >= 1 and apagados["lead_mensagens"] >= 1 and apagados["leads"] >= 1
    restantes = [r[0] for r in db.executar("select conteudo from public.mensagens where paciente_id = %s", (P,), retorno="todos")]
    assert restantes == ["nova"]
    tels = {r[0] for r in db.executar("select telefone from public.leads where nutricionista_id = %s", (N,), retorno="todos")}
    assert tels == {"5551900000032", "5551900000033"}   # parado há 100 dias sumiu; recente e convertido ficam
