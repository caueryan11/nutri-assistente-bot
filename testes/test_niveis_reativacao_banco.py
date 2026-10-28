"""Níveis de acompanhamento e campanha de reativação contra o Postgres de verdade (transação desfeita no final).

Rodar:  RODAR_TESTES_BANCO=1 python -m pytest testes/test_niveis_reativacao_banco.py
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from testes.test_area_banco import MIGRACOES
from testes.test_rotinas_banco import RAIZ, ConexaoTransacao

pytestmark = pytest.mark.skipif(os.environ.get("RODAR_TESTES_BANCO") != "1", reason="defina RODAR_TESTES_BANCO=1")

N = "a1a1a1a1-1111-0000-0000-000000000001"
P = "a1a1a1a1-2222-0000-0000-0000000000{:02d}"
SP = ZoneInfo("America/Sao_Paulo")


@pytest.fixture(scope="module")
def db():
    import psycopg2
    from dotenv import dotenv_values

    conn = psycopg2.connect(dotenv_values(RAIZ / ".env")["DATABASE_URL"])
    db = ConexaoTransacao(conn)
    for m in (*MIGRACOES, "20260930120000_importar_plano.sql", "20260930150000_checkin.sql",
              "20260930170000_niveis_acompanhamento.sql", "20260930180000_reativacao.sql"):
        db.executar((RAIZ / "supabase" / "migrations" / m).read_text(encoding="utf-8"))
    db.executar("insert into public.nutricionistas (id, nome, whatsapp_phone_number_id, modelo_acompanhamento) "
                "values (%s, 'Nutri Teste Níveis', 'inst-teste-niveis', 'por_paciente')", (N,))

    def pac(i, **kw):
        campos = {"id": P.format(i), "nutricionista_id": N, "nome": f"Paciente{i} Niveis", "telefone": f"55119990055{i:02d}",
                  **kw}
        cols = ", ".join(campos)
        db.executar(f"insert into public.pacientes ({cols}) values ({', '.join(['%s'] * len(campos))})", tuple(campos.values()))

    hoje = datetime.now(SP).date()
    agora = datetime.now(SP)
    pac(1, consentimento_em=agora, acompanhamento_desde=hoje, acompanhamento_ate=hoje + timedelta(days=4))  # aviso de fim
    pac(2, consentimento_em=agora, acompanhamento_desde=hoje - timedelta(days=60), acompanhamento_ate=hoje - timedelta(days=1))  # vencido
    pac(3, consentimento_em=agora, acompanhamento_desde=hoje)                                               # sem data
    pac(4, ultima_consulta_em=agora - timedelta(days=120))                                                  # sumido (sem aceite)
    pac(5, ultima_consulta_em=agora - timedelta(days=20))                                                   # recente
    pac(6, ultima_consulta_em=agora - timedelta(days=200), status_consentimento="revogado")                 # pediu PARAR
    pac(7)                                                                                                    # sem data
    pac(8, ultima_consulta_em=agora - timedelta(days=150))                                                  # tem consulta futura
    db.executar("insert into public.agendamentos (nutricionista_id, paciente_id, inicio, fim, status) "
                "values (%s, %s, now() + interval '3 days', now() + interval '3 days 1 hour', 'confirmado')", (N, P.format(8)))
    yield db
    conn.rollback()
    conn.close()


def _bot(db):
    from whatsapp.repositorio import RepositorioPostgres

    r = RepositorioPostgres.__new__(RepositorioPostgres)
    r._db = db
    return r


def test_acompanhamento_ativo_e_modelo(db):
    bot = _bot(db)
    assert bot.nutri_por_instancia("inst-teste-niveis").modelo_acompanhamento == "por_paciente"
    ativo = {i: bot.paciente(N, f"55119990055{i:02d}").acompanhamento_ativo for i in (1, 2, 3, 4)}
    assert ativo == {1: True, 2: False, 3: True, 4: False}


def test_aviso_de_fim_uma_vez_por_ciclo(db):
    from bot.rotinas import RepositorioRotinasPostgres
    from whatsapp.area_repo import RepositorioAreaPostgres

    rot = RepositorioRotinasPostgres(db)
    nossos = [r for r in rot.reservar_fim_acompanhamento() if r["nutri_id"] == N]
    assert [r["paciente_id"] for r in nossos] == [P.format(1)]
    assert [r for r in rot.reservar_fim_acompanhamento() if r["nutri_id"] == N] == []
    # renovar começa um ciclo novo (e o aviso volta a valer quando chegar perto do fim)
    area = RepositorioAreaPostgres(db)
    area.definir_acompanhamento(N, P.format(1), "validade", 45)
    p = area.paciente(N, P.format(1))
    assert p["acompanhamento_ate"] == datetime.now(SP).date() + timedelta(days=45) or \
        p["acompanhamento_ate"] == date.today() + timedelta(days=45)
    area.definir_acompanhamento(N, P.format(2), "validade", 30)             # vencido: recomeça hoje
    p2 = area.paciente(N, P.format(2))
    assert p2["acompanhamento_desde"] >= date.today() - timedelta(days=1)
    area.definir_acompanhamento(N, P.format(3), "desligar")
    assert area.paciente(N, P.format(3))["acompanhamento_desde"] is None


def test_campanha_do_publico_ao_envio(db):
    from bot.reativacao import RepositorioReativacaoPostgres

    r = RepositorioReativacaoPostgres(db)
    imp = r.importar(N, [{"nome": "Nova Importada", "telefone": "5511900000034", "ultima_consulta": date(2025, 1, 10)},
                         {"nome": "Paciente4 Niveis", "telefone": "5511900000035", "ultima_consulta": date(2025, 1, 1)}])
    assert imp == {"criados": 1, "atualizados": 1}
    pub = r.publico(N, 90, False)
    nomes = set(pub["exemplos"])
    assert pub["total"] == 2 and nomes == {"Paciente4 Niveis", "Nova Importada"}      # 120 e ~600 dias; fora: recente, revogado, futura
    assert r.publico(N, 90, True)["total"] == 4    # + sem data: 7 e o 2 (acompanhamento vencido); 1 e 3 estão em acompanhamento
    cid, total = r.criar_campanha(N, "Oi {nome}!", 90, False)
    assert total == 2 and r.publico(N, 90, False)["total"] == 0                        # já recebeu: não entra de novo

    # envio respeita o horário comercial da nutri: força a janela para "agora"
    agora = datetime.now(SP)
    dentro = 1 <= agora.isoweekday() <= 6 and 9 <= agora.hour < 20
    envios = [e for e in r.reservar_envios(10) if e["nutri_id"] == N]
    if not dentro:
        assert envios == []
        return
    assert len(envios) == 2 and {e["mensagem"] for e in envios} == {"Oi {nome}!"}
    r.devolver_envio(envios[0]["envio_id"], envios[0]["tentativas"])                   # falhou: volta para a fila
    r.marcar_resposta(envios[1]["paciente_id"])
    assert r.campanha_recente(envios[1]["paciente_id"]) is True
    stats = [c for c in r.campanhas(N) if c["id"] == cid][0]
    assert (stats["enviados"], stats["pendentes"], stats["responderam"]) == (1, 1, 1)
    r.pausar(N, cid, True)
    assert [e for e in r.reservar_envios(10) if e["nutri_id"] == N] == []              # pausada não envia


def test_pedido_do_termo_registrado(db):
    bot = _bot(db)
    assert bot.paciente(N, "5511900000036").termo_pedido_em is None
    bot.marcar_pedido_termo(P.format(7))
    assert bot.paciente(N, "5511900000036").termo_pedido_em is not None
