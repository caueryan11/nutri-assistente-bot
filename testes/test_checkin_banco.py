"""Check-in semanal contra o Postgres de verdade (transação desfeita no final).

Rodar:  RODAR_TESTES_BANCO=1 python -m pytest testes/test_checkin_banco.py
"""

import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from testes.test_area_banco import MIGRACOES
from testes.test_rotinas_banco import RAIZ, ConexaoTransacao

pytestmark = pytest.mark.skipif(os.environ.get("RODAR_TESTES_BANCO") != "1", reason="defina RODAR_TESTES_BANCO=1")

N = "efefefef-1111-0000-0000-000000000001"
P = "efefefef-2222-0000-0000-0000000000{:02d}"
SP = ZoneInfo("America/Sao_Paulo")


@pytest.fixture(scope="module")
def db():
    import psycopg2
    from dotenv import dotenv_values

    conn = psycopg2.connect(dotenv_values(RAIZ / ".env")["DATABASE_URL"])
    db = ConexaoTransacao(conn)
    for m in (*MIGRACOES, "20260930120000_importar_plano.sql", "20260930150000_checkin.sql"):
        db.executar((RAIZ / "supabase" / "migrations" / m).read_text(encoding="utf-8"))
    agora = datetime.now(SP)
    hora = max(agora - timedelta(hours=1), agora.replace(hour=0, minute=0))
    config = {"dia": agora.isoweekday(), "hora": hora.strftime("%H:%M")}
    db.executar("insert into public.nutricionistas (id, nome, checkin_config) values (%s, 'Nutri Teste Check-in', %s::jsonb)",
                (N, json.dumps(config)))
    for i, (consent, acomp, ativo) in enumerate([(True, True, True), (True, False, True), (False, True, True),
                                                  (True, True, False)], start=1):
        db.executar("insert into public.pacientes (id, nutricionista_id, nome, telefone, consentimento_em, "
                    "acompanhamento_desde, ativo) values (%s, %s, %s, %s, case when %s then now() end, "
                    "case when %s then current_date end, %s)",
                    (P.format(i), N, f"Paciente{i} Checkin", f"55119990066{i:02d}", consent, acomp, ativo))
    yield db
    conn.rollback()
    conn.close()


def test_reserva_so_quem_esta_no_acompanhamento_uma_vez_por_semana(db):
    from bot.rotinas import RepositorioRotinasPostgres

    rot = RepositorioRotinasPostgres(db)
    nossos = [c for c in rot.reservar_checkins() if c["nutri_id"] == N]
    assert [c["paciente_id"] for c in nossos] == [P.format(1)]            # sem acomp., sem aceite e inativo ficam fora
    assert nossos[0]["primeiro_nome"] == "Paciente1" and nossos[0]["nutri_nome"] == "Nutri Teste Check-in"
    assert [c for c in rot.reservar_checkins() if c["nutri_id"] == N] == []   # mesma semana: não repete
    rot.desfazer_checkin(nossos[0]["checkin_id"])
    assert len([c for c in rot.reservar_checkins() if c["nutri_id"] == N]) == 1  # desfeito: pode reenviar


def test_resposta_peso_anterior_e_evolucao(db):
    from whatsapp.area_repo import RepositorioAreaPostgres
    from whatsapp.repositorio import RepositorioPostgres

    bot = RepositorioPostgres.__new__(RepositorioPostgres)
    bot._db = db
    db.executar("insert into public.checkins (nutricionista_id, paciente_id, semana, status, peso_kg, adesao) "
                "values (%s, %s, current_date - 7, 'respondido', 71.0, 7)", (N, P.format(1)))
    pendente = bot.checkin_pendente(P.format(1))
    assert pendente and pendente["adesao"] is None and pendente["lembrete_enviado"] is False
    assert bot.peso_anterior(P.format(1), pendente["semana"]) == 71.0
    bot.salvar_checkin(pendente["id"], {"adesao": 9, "fome": "normal", "intestino": "normal", "peso_kg": 70.2,
                                        "alerta": False, "status": "respondido", "coisa": "ignorada"})
    assert bot.checkin_pendente(P.format(1)) is None
    area = RepositorioAreaPostgres(db)
    ev = area.evolucao(N, P.format(1))
    assert [c["adesao"] for c in ev] == [7, 9] and float(ev[-1]["peso_kg"]) == 70.2
    assert ev[-1]["status"] == "respondido"
    lista = {p["id"]: p for p in area.pacientes(N)}
    assert lista[P.format(1)]["ultima_adesao"] == 9 and lista[P.format(2)]["acompanhamento_desde"] is None


def test_ligar_e_desligar_acompanhamento(db):
    from whatsapp.area_repo import RepositorioAreaPostgres

    area = RepositorioAreaPostgres(db)
    assert area.definir_acompanhamento(N, P.format(2), True) is True
    assert area.paciente(N, P.format(2))["acompanhamento_desde"] is not None
    antes = area.paciente(N, P.format(1))["acompanhamento_desde"]
    area.definir_acompanhamento(N, P.format(1), True)                      # religar não muda a data
    assert area.paciente(N, P.format(1))["acompanhamento_desde"] == antes
    area.definir_acompanhamento(N, P.format(2), False)
    assert area.paciente(N, P.format(2))["acompanhamento_desde"] is None


def test_consultorio_guarda_config_do_checkin(db):
    from whatsapp.area_repo import RepositorioAreaPostgres

    area = RepositorioAreaPostgres(db)
    area.salvar_consultorio(N, {"checkin_config": {"dia": 1, "hora": "08:30"}})
    assert area.consultorio(N)["checkin_config"] == {"dia": 1, "hora": "08:30"}
