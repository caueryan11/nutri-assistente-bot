"""Área da nutri contra o Postgres de verdade, numa transação desfeita no final.

Rodar:  RODAR_TESTES_BANCO=1 python -m pytest testes/test_area_banco.py
"""

import os
from decimal import Decimal

import pytest

from testes.test_rotinas_banco import RAIZ, ConexaoTransacao

pytestmark = pytest.mark.skipif(os.environ.get("RODAR_TESTES_BANCO") != "1", reason="defina RODAR_TESTES_BANCO=1")

N1 = "abababab-1111-0000-0000-000000000001"
N2 = "abababab-1111-0000-0000-000000000002"
MIGRACOES = ("20260929120000_consentimento.sql", "20260929140000_rotinas.sql", "20260929160000_comercial.sql",
             "20260929180000_metricas.sql", "20260929200000_anamnese.sql", "20260929220000_lgpd_operacao.sql",
             "20260930090000_area_nutri.sql")


@pytest.fixture(scope="module")
def area():
    import psycopg2
    from dotenv import dotenv_values

    from whatsapp.area_repo import RepositorioAreaPostgres

    conn = psycopg2.connect(dotenv_values(RAIZ / ".env")["DATABASE_URL"])
    db = ConexaoTransacao(conn)
    for m in MIGRACOES:
        db.executar((RAIZ / "supabase" / "migrations" / m).read_text(encoding="utf-8"))
    for n in (N1, N2):
        db.executar("insert into public.nutricionistas (id, nome) values (%s, 'Nutri Teste Área')", (n,))
    yield RepositorioAreaPostgres(db)
    conn.rollback()
    conn.close()


def _savepoint(area, nome):
    area._db.executar(f"savepoint {nome}")


def test_cadastro_isolado_por_nutri(area):
    from whatsapp.area_repo import TelefoneDuplicado

    pid = area.criar_paciente(N1, "Maria Teste Área", "5551999770001")
    assert [p["nome"] for p in area.pacientes(N1)] == ["Maria Teste Área"] and area.pacientes(N2) == []
    assert area.paciente(N2, pid) is None                          # outra nutri não enxerga
    assert area.atualizar_paciente(N2, pid, "Hack", "5551900000027", True) is False
    assert area.salvar_anamnese(N2, pid, {"alergias": ["x"]}) is False
    assert area.salvar_plano(N2, pid, {"refeicoes": []}) is False
    assert area.excluir_paciente(N2, pid) is False
    p = area.paciente(N1, pid)
    assert p["nome"] == "Maria Teste Área" and p["status_consentimento"] == "pendente" and p["plano"] is None
    _savepoint(area, "dup")
    with pytest.raises(TelefoneDuplicado):
        area.criar_paciente(N1, "Outra Pessoa", "5551999770001")
    area._db.executar("rollback to savepoint dup")


def test_anamnese_plano_e_historico(area):
    pid = area.pacientes(N1)[0]["id"]
    assert area.salvar_anamnese(N1, pid, {"alergias": ["amendoim"], "observacoes": "teste"}) is True
    assert area.salvar_plano(N1, pid, {"refeicoes": [{"nome": "Almoço", "itens": []}]}) is True
    assert area.salvar_plano(N1, pid, {"refeicoes": [{"nome": "Jantar", "itens": []}]}) is True
    p = area.paciente(N1, pid)
    assert p["anamnese"]["alergias"] == ["amendoim"] and p["plano"]["refeicoes"][0]["nome"] == "Jantar"
    ativos, total = area._db.executar(
        "select count(*) filter (where ativo), count(*) from public.planos_alimentares where paciente_id = %s",
        (pid,), retorno="um")
    assert (ativos, total) == (1, 2)                                # o anterior ficou no histórico
    lista = area.pacientes(N1)[0]
    assert lista["tem_plano"] and lista["tem_anamnese"]


def test_consultorio(area):
    area.salvar_consultorio(N1, {"valor_consulta": Decimal("250.00"), "atende_convenio": True,
                                 "convenios_aceitos": ["Unimed"], "telefone_aviso": "5551900000028",
                                 "agenda_config": {"duracao_min": 50, "atendimento": {"1": ["08:00-12:00"]}},
                                 "nome": "não pode mudar por aqui"})
    c = area.consultorio(N1)
    assert c["valor_consulta"] == Decimal("250.00") and c["convenios_aceitos"] == ["Unimed"]
    assert c["agenda_config"]["duracao_min"] == 50 and c["nome"] == "Nutri Teste Área"


def test_excluir_apaga_em_cascata(area):
    pid = area.pacientes(N1)[0]["id"]
    assert area.excluir_paciente(N1, pid) is True
    assert area._db.executar("select count(*) from public.planos_alimentares where paciente_id = %s", (pid,),
                             retorno="um")[0] == 0
    assert area.paciente(N1, pid) is None
