"""Consultas SQL das rotinas contra o Postgres de verdade, numa transação desfeita no final.

Rodar:  RODAR_TESTES_BANCO=1 python -m pytest testes/test_rotinas_banco.py
Usa DATABASE_URL do .env. A migração das rotinas é aplicada dentro da mesma transação,
então funciona antes ou depois de ela estar no banco. Nada fica gravado.
"""

import os
import pathlib

import pytest

RAIZ = pathlib.Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.environ.get("RODAR_TESTES_BANCO") != "1", reason="defina RODAR_TESTES_BANCO=1")

N = "cccccccc-1111-0000-0000-000000000000"
P = "cccccccc-2222-0000-0000-0000000000{:02d}"  # pacientes de teste


class ConexaoTransacao:
    """Mesmo executar() de bot.banco.Conexao, mas sem autocommit (tudo é desfeito no fim)."""

    def __init__(self, conn):
        import psycopg2.extras

        self._conn, self._dict = conn, psycopg2.extras.RealDictCursor

    def executar(self, sql, params=(), *, retorno=None, como_dict=False):
        with self._conn.cursor(cursor_factory=self._dict if como_dict else None) as cur:
            cur.execute(sql, params)
            return cur.fetchone() if retorno == "um" else cur.fetchall() if retorno == "todos" else None


@pytest.fixture(scope="module")
def banco():
    import psycopg2
    from dotenv import dotenv_values

    conn = psycopg2.connect(dotenv_values(RAIZ / ".env")["DATABASE_URL"])
    db = ConexaoTransacao(conn)
    for m in ("20260929120000_consentimento.sql", "20260929140000_rotinas.sql", "20260929160000_comercial.sql"):
        db.executar((RAIZ / "supabase" / "migrations" / m).read_text(encoding="utf-8"))
    db.executar("insert into public.nutricionistas (id, nome, telefone_aviso) values (%s, 'Nutri Teste Rotinas', '5511900000000')", (N,))

    def paciente(n, consentimento=True, ultima=None, aviso=None):
        db.executar(
            "insert into public.pacientes (id, nutricionista_id, nome, telefone, consentimento_em, ultima_consulta_em, ultimo_aviso_retorno_em) "
            "values (%s, %s, %s, %s, case when %s then now() end, now() - %s::interval, now() - %s::interval)",
            (P.format(n), N, f"Paciente {n}", f"55119990000{n:02d}", consentimento, ultima, aviso))

    def consulta(n, quando, status="confirmado", lembrete=False):
        db.executar(
            "insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim, status, lembrete_enviado_em) "
            "values (%s, %s, now() + %s::interval, now() + %s::interval + interval '10 minutes', %s, case when %s then now() end)",
            (N, P.format(n), quando, quando, status, lembrete))

    # lembretes
    paciente(1); consulta(1, "24 hours")                                  # entra
    paciente(2); consulta(2, "24 hours 10 minutes", lembrete=True)          # já avisado
    paciente(3); consulta(3, "26 hours")                                   # fora da janela
    paciente(4, consentimento=False); consulta(4, "24 hours 20 minutes")   # sem consentimento
    paciente(5); consulta(5, "24 hours 5 minutes", status="cancelado")     # cancelada
    # retornos
    paciente(11); consulta(11, "-40 days")                                  # entra (consulta do bot)
    paciente(12, ultima="40 days")                                          # entra (preenchido pela nutri)
    paciente(13, ultima="40 days"); consulta(13, "5 days")                  # já tem retorno marcado
    paciente(14, ultima="40 days", aviso="10 days")                         # avisado há pouco
    paciente(15, ultima="20 days")                                          # cedo demais
    paciente(16, ultima="70 days")                                          # sumido demais (fase 2)
    paciente(17, ultima="40 days", aviso="31 days")                         # entra (aviso antigo)
    paciente(18, consentimento=False, ultima="40 days")                     # sem consentimento
    paciente(19); consulta(19, "-40 days", status="faltou")                 # faltou não conta como consulta
    yield db
    conn.rollback()
    conn.close()


@pytest.fixture(scope="module")
def repo(banco):
    from bot.rotinas import RepositorioRotinasPostgres

    return RepositorioRotinasPostgres(banco)


def ids(linhas, chave="paciente_id"):
    return {l[chave] for l in linhas if l[chave].startswith("cccccccc")}


def test_lembretes_seleciona_so_quem_deve_e_reserva(repo):
    primeira = repo.reservar_lembretes()
    assert ids(primeira) == {P.format(1)}
    assert ids(repo.reservar_lembretes()) == set()          # reservado: não sai de novo


def test_retornos(repo):
    assert ids(repo.reservar_retornos()) == {P.format(11), P.format(12), P.format(17)}
    assert ids(repo.reservar_retornos()) == set()           # no máximo 1 por 30 dias


def test_resumo_reserva_uma_vez_por_dia_e_dados(repo, banco):
    nutris = [n for n in repo.reservar_resumos() if n["nutri_id"] == N]
    assert len(nutris) == 1 and nutris[0]["telefone_aviso"] == "5511900000000"
    assert [n for n in repo.reservar_resumos() if n["nutri_id"] == N] == []
    pid = P.format(1)
    for acao, quando in (("responder", "1 hour"), ("responder", "3 hours"), ("encaminhar", "2 hours"), ("responder", "30 hours")):
        banco.executar("insert into public.mensagens (nutricionista_id, paciente_id, direcao, conteudo, acao, motivo, resumo_para_nutri, criado_em) "
                       "values (%s, %s, 'saida', 'x', %s, 'sintoma', 'Relata enjoo', now() - %s::interval)", (N, pid, acao, quando))
    banco.executar("insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim, status) values "
                   "(%s, %s, date_trunc('day', now() at time zone 'America/Sao_Paulo') at time zone 'America/Sao_Paulo' + interval '23 hours',"
                   " date_trunc('day', now() at time zone 'America/Sao_Paulo') at time zone 'America/Sao_Paulo' + interval '23 hours 50 minutes', 'confirmado')",
                   (N, P.format(15)))
    d = repo.dados_resumo(N, "America/Sao_Paulo")
    assert d["respondidas"] == 2                                            # a de 30 h atrás fica de fora
    assert [(e["paciente"], e["resumo"]) for e in d["encaminhadas"]] == [("Paciente 1", "Relata enjoo")]
    assert [c["paciente"] for c in d["consultas_hoje"]] == ["Paciente 15"]
    assert d["avisos_hoje"] == 3 and d["avisos_semana"] == 3                # os 3 retornos reservados acima


def test_desfazer_reservas(repo, banco):
    repo.desfazer_retorno(P.format(11), None)
    assert banco.executar("select ultimo_aviso_retorno_em from public.pacientes where id = %s", (P.format(11),), retorno="um")[0] is None
