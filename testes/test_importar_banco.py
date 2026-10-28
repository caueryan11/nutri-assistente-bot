"""Rascunho de plano importado, contra o Postgres de verdade (transação desfeita no final).

Rodar:  RODAR_TESTES_BANCO=1 python -m pytest testes/test_importar_banco.py
"""

import os

import pytest

from testes.test_area_banco import MIGRACOES
from testes.test_rotinas_banco import RAIZ, ConexaoTransacao

pytestmark = pytest.mark.skipif(os.environ.get("RODAR_TESTES_BANCO") != "1", reason="defina RODAR_TESTES_BANCO=1")

N1 = "cdcdcdcd-1111-0000-0000-000000000001"
N2 = "cdcdcdcd-1111-0000-0000-000000000002"


@pytest.fixture(scope="module")
def area():
    import psycopg2
    from dotenv import dotenv_values

    from whatsapp.area_repo import RepositorioAreaPostgres

    conn = psycopg2.connect(dotenv_values(RAIZ / ".env")["DATABASE_URL"])
    db = ConexaoTransacao(conn)
    for m in (*MIGRACOES, "20260930120000_importar_plano.sql"):
        db.executar((RAIZ / "supabase" / "migrations" / m).read_text(encoding="utf-8"))
    for n in (N1, N2):
        db.executar("insert into public.nutricionistas (id, nome) values (%s, 'Nutri Teste Importação')", (n,))
    yield RepositorioAreaPostgres(db)
    conn.rollback()
    conn.close()


def _planos(area, pid):
    return area._todos("select ativo, rascunho, origem, conteudo->'refeicoes'->0->>'nome' as primeira "
                       "from public.planos_alimentares where paciente_id = %s order by criado_em, rascunho", (pid,))


def test_rascunho_nao_vale_ate_ativar(area):
    pid = area.criar_paciente(N1, "Joana Teste Importação", "5551999660001")
    area.salvar_plano(N1, pid, {"refeicoes": [{"nome": "Antigo", "itens": []}]})
    assert area.salvar_rascunho(N2, pid, {"refeicoes": []}, "pdf", []) is False          # outra nutri
    assert area.salvar_rascunho(N1, pid, {"refeicoes": [{"nome": "Rascunho 1", "itens": []}]}, "whatsapp", ["a"]) is True
    assert area.salvar_rascunho(N1, pid, {"refeicoes": [{"nome": "Rascunho 2", "itens": []}]}, "whatsapp", ["b"]) is True
    p = area.paciente(N1, pid)
    assert p["plano"]["refeicoes"][0]["nome"] == "Antigo"                                 # o bot continua no antigo
    assert p["rascunho"]["refeicoes"][0]["nome"] == "Rascunho 2" and p["rascunho_avisos"] == ["b"]
    assert p["rascunho_origem"] == "whatsapp" and area.pacientes(N1)[0]["tem_rascunho"] is True
    # o bot (repositório do webhook) lê só o ativo
    from whatsapp.repositorio import RepositorioPostgres

    bot = RepositorioPostgres.__new__(RepositorioPostgres)
    bot._db = area._db
    assert bot.plano_ativo(pid)["refeicoes"][0]["nome"] == "Antigo"

    # ativar: rascunho some, plano novo ativo herda a origem, antigo vai para o histórico
    area.salvar_plano(N1, pid, {"refeicoes": [{"nome": "Conferido", "itens": []}]})
    assert [(r["ativo"], r["rascunho"], r["origem"], r["primeira"]) for r in _planos(area, pid)] == [
        (False, False, "painel", "Antigo"), (True, False, "whatsapp", "Conferido")]
    assert area.paciente(N1, pid)["rascunho"] is None


def test_descartar(area):
    pid = area.pacientes(N1)[0]["id"]
    area.salvar_rascunho(N1, pid, {"refeicoes": [{"nome": "X", "itens": []}]}, "pdf", [])
    assert area.descartar_rascunho(N2, pid) is False
    assert area.descartar_rascunho(N1, pid) is True and area.paciente(N1, pid)["rascunho"] is None


def test_busca_por_telefone_inclui_inativo(area):
    pid = area.pacientes(N1)[0]["id"]
    area.atualizar_paciente(N1, pid, "Joana Teste Importação", "5551999660001", False)
    achado = area.paciente_por_telefone(N1, "555199660001")                               # sem o 9
    assert achado["id"] == pid and achado["ativo"] is False
    assert area.paciente_por_telefone(N2, "5551999660001") is None


def test_rascunho_nunca_ativo(area):
    import psycopg2.errors

    pid = area.pacientes(N1)[0]["id"]
    area._db.executar("savepoint s")
    with pytest.raises(psycopg2.errors.CheckViolation):
        area._db.executar("insert into public.planos_alimentares (paciente_id, nutricionista_id, conteudo, ativo, rascunho) "
                          "values (%s, %s, '{}'::jsonb, true, true)", (pid, N1))
    area._db.executar("rollback to savepoint s")
