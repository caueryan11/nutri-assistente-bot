"""Métricas: texto do relatório semanal, link do painel, rota /painel e agendador (sem banco)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from bot.assistente import Resultado
from bot.metricas import (Metricas, gerar_token, link_painel, montar_relatorio_semanal, reais,
                          rodar_relatorios_semanais, validar_token)
from bot.rotinas import criar_agendador
from testes.test_whatsapp import TOKEN, SEGREDO, URL, ClienteFalso, RepoFalso, evento
from whatsapp.app import Config, criar_app

SP = ZoneInfo("America/Sao_Paulo")
SEG_PAINEL = "segredo-do-painel-com-mais-de-32-caracteres"
SEMANA = Metricas(dias=7, duvidas_respondidas=42, duvidas_fora_horario=17, duvidas_encaminhadas=5, novos_leads=8,
                  leads_convertidos=3, primeiras_consultas=3, retornos_agendados=4, retornos_recuperados=2,
                  valor_consulta=250.0)


# ---------------------------------------------------------------------
# Números
# ---------------------------------------------------------------------
def test_receita_e_conversao():
    assert SEMANA.consultas_agendadas == 7
    assert SEMANA.receita_estimada == 1750.0
    assert SEMANA.taxa_conversao == 37.5
    assert Metricas(dias=7).taxa_conversao is None and Metricas(dias=7).receita_estimada is None


def test_reais():
    assert reais(1750) == "1.750,00" and reais(99.9) == "99,90" and reais(1234567.891) == "1.234.567,89"


# ---------------------------------------------------------------------
# Relatório semanal
# ---------------------------------------------------------------------
def test_relatorio_semanal_completo():
    texto = montar_relatorio_semanal(SEMANA, "https://app.exemplo/painel/tok",
                                     datetime(2026, 9, 21, tzinfo=SP), datetime(2026, 9, 27, tzinfo=SP))
    assert texto == (
        "📊 *Seu Consultório na Semana - Nutri Assistente* (21/09 a 27/09)\n"
        "\n"
        "💬 *Atendimento no WhatsApp:*\n"
        "• 42 dúvidas de dieta resolvidas pelo assistente (17 fora do horário comercial)\n"
        "• 5 casos clínicos encaminhados\n"
        "\n"
        "📅 *Agenda e Novos Pacientes:*\n"
        "• 8 novos interessados atendidos\n"
        "• 3 primeiras consultas agendadas\n"
        "• 2 retornos resgatados após 35+ dias\n"
        "\n"
        "💰 *Impacto Estimado na sua Agenda:*\n"
        "• Cerca de R$ 1.750,00 em consultas viabilizadas ou mantidas!\n"
        "\n"
        "👉 Acesse seu painel completo aqui: https://app.exemplo/painel/tok"
    )


def test_relatorio_sem_valor_sem_link_e_semana_vazia():
    sem_valor = montar_relatorio_semanal(Metricas(dias=7, primeiras_consultas=1), None)
    assert "Cadastre o valor da consulta" in sem_valor and "painel" not in sem_valor and "R$" not in sem_valor
    vazia = montar_relatorio_semanal(Metricas(dias=7, valor_consulta=250), None)
    assert "Nenhuma consulta marcada pelo assistente" in vazia and "fora do horário" not in vazia


class RepoMetricasFalso:
    def __init__(self, reservas=None, falha=False):
        self.reservas = reservas if reservas is not None else [
            {"nutri_id": "n1", "nome": "Ana Souza", "telefone_aviso": "5511988880000", "fuso_horario": "America/Sao_Paulo",
             "anterior": None}]
        self.falha, self.desfeitos, self.pedidos = falha, [], []

    def reservar_semanais(self):
        r, self.reservas = self.reservas, []
        return r

    def desfazer_semanal(self, nutri_id, anterior):
        self.desfeitos.append((nutri_id, anterior))

    def metricas(self, nutri_id, dias, fuso="America/Sao_Paulo", detalhes=False):
        self.pedidos.append((nutri_id, dias, detalhes))
        if self.falha:
            raise RuntimeError("banco fora")
        m = Metricas(**{**SEMANA.__dict__, "dias": dias})
        if detalhes:
            m.ultimas_encaminhadas = [{"quando": datetime(2026, 9, 28, 21, 5, tzinfo=SP), "paciente": "Maria <b>",
                                       "motivo": "pediu troca de suplemento"}]
            m.ultimas_consultas = [{"quando": datetime(2026, 10, 1, 10, 0, tzinfo=SP), "paciente": "Joana",
                                    "tipo": "primeira", "marcada_em": datetime(2026, 9, 28, 9, 0, tzinfo=SP)}]
        return m

    def nutri(self, nutri_id):
        return {"id": "n1", "nome": "Ana Souza", "fuso_horario": "America/Sao_Paulo"} if nutri_id == "n1" else None


def test_rodar_relatorio_envia_para_a_nutri_com_link():
    repo, enviadas = RepoMetricasFalso(), []
    n = rodar_relatorios_semanais(repo, lambda num, txt: enviadas.append((num, txt)),
                                  link=lambda nid: f"https://x/painel/{nid}", agora=datetime(2026, 9, 28, 8, 30, tzinfo=SP))
    assert n == 1 and repo.pedidos == [("n1", 7, False)]
    numero, texto = enviadas[0]
    assert numero == "5511988880000" and "(21/09 a 27/09)" in texto and texto.endswith("https://x/painel/n1")
    # segunda chamada no mesmo dia: nada reservado, nada enviado
    assert rodar_relatorios_semanais(repo, lambda *a: enviadas.append(a)) == 0 and len(enviadas) == 1


def test_rodar_relatorio_com_falha_desfaz_a_reserva():
    repo = RepoMetricasFalso(falha=True)
    assert rodar_relatorios_semanais(repo, lambda *a: None) == 0
    assert repo.desfeitos == [("n1", None)]


def test_agendador_tem_relatorio_semanal_segunda_0830():
    ag = criar_agendador(object(), lambda *a: None, metricas=RepoMetricasFalso())
    job = ag.get_job("relatorio_semanal")
    assert job is not None and "day_of_week='mon'" in str(job.trigger) and "hour='8'" in str(job.trigger)
    assert "minute='30'" in str(job.trigger)
    assert criar_agendador(object(), lambda *a: None).get_job("relatorio_semanal") is None


# ---------------------------------------------------------------------
# Link do painel
# ---------------------------------------------------------------------
def test_token_valido_expira_e_nao_aceita_adulteracao():
    tok = gerar_token("n1", SEG_PAINEL, validade_dias=8, agora=1_000_000)
    assert validar_token(tok, SEG_PAINEL, agora=1_000_000 + 7 * 86400) == "n1"
    assert validar_token(tok, SEG_PAINEL, agora=1_000_000 + 9 * 86400) is None      # expirou
    assert validar_token(tok, "outro-segredo-com-mais-de-32-caracteres!!") is None
    nid, exp, sig = tok.split(".")
    assert validar_token(f"n2.{exp}.{sig}", SEG_PAINEL, agora=1_000_000) is None     # outra nutri
    assert validar_token(f"n1.{int(exp) + 999999}.{sig}", SEG_PAINEL, agora=1_000_000) is None  # esticou validade
    assert validar_token("lixo", SEG_PAINEL) is None and validar_token("", SEG_PAINEL) is None


def test_link_painel_precisa_de_url_e_segredo():
    assert link_painel("https://app.exemplo/", "n1", SEG_PAINEL).startswith("https://app.exemplo/painel/n1.")
    assert link_painel("", "n1", SEG_PAINEL) is None and link_painel("https://x", "n1", "") is None


# ---------------------------------------------------------------------
# Rota /painel
# ---------------------------------------------------------------------
def app_painel(repo_metricas=None, painel_segredo=SEG_PAINEL, repo=None):
    cliente = ClienteFalso()
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, painel_segredo=painel_segredo,
                           url_publica="https://app.exemplo"),
                    cliente=cliente, repo=repo or RepoFalso(), taco=object(), responder_fn=lambda *a, **k: None,
                    metricas=repo_metricas or RepoMetricasFalso())
    return TestClient(app), cliente


def test_painel_mostra_numeros_graficos_e_tabelas():
    repo = RepoMetricasFalso()
    c, _ = app_painel(repo)
    r = c.get(f"/painel/{gerar_token('n1', SEG_PAINEL)}")
    assert r.status_code == 200
    html = r.text
    assert "Painel de Ana Souza" in html and "R$ 1.750,00" in html and ">38%<" in html
    assert "3 de 8 novos interessados" in html
    assert '"duvidas": [42, 5]' in html and '"consultas": [3, 4, 2]' in html
    assert "28/09 21:05" in html and "pediu troca de suplemento" in html and "Primeira consulta" in html
    assert "Maria &lt;b&gt;" in html                              # escapa o que vem do banco
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in r.headers["x-robots-tag"]
    assert repo.pedidos == [("n1", 7, True)]


def test_painel_30_dias():
    repo = RepoMetricasFalso()
    c, _ = app_painel(repo)
    assert c.get(f"/painel/{gerar_token('n1', SEG_PAINEL)}?dias=30").status_code == 200
    assert repo.pedidos == [("n1", 30, True)]
    assert c.get(f"/painel/{gerar_token('n1', SEG_PAINEL)}?dias=365").status_code == 400


def test_painel_token_invalido_ou_nutri_inativa_404():
    repo = RepoMetricasFalso()
    c, _ = app_painel(repo)
    assert c.get("/painel/qualquer-coisa").status_code == 404
    assert c.get(f"/painel/{gerar_token('n1', 'segredo-errado-com-mais-de-32-caracteres')}").status_code == 404
    assert c.get(f"/painel/{gerar_token('n9', SEG_PAINEL)}").status_code == 404     # nutri inativa/inexistente
    assert repo.pedidos == []


def test_painel_desligado_sem_segredo():
    c, _ = app_painel(painel_segredo="")
    assert c.get(f"/painel/{gerar_token('n1', SEG_PAINEL)}").status_code == 404


def test_segredo_curto_recusado():
    import pytest

    with pytest.raises(ValueError):
        app_painel(painel_segredo="curto")


# ---------------------------------------------------------------------
# Comando "painel" pelo WhatsApp
# ---------------------------------------------------------------------
def test_nutri_pede_painel_pelo_numero_de_aviso():
    repo = RepoFalso(cadastrado=False)
    c, cliente = app_painel(repo=repo)
    c.post(URL, json=evento("Painel", sender_pn="5511988880000@s.whatsapp.net", chatid="5511988880000@s.whatsapp.net"))
    ((numero, texto),) = cliente.enviadas
    assert numero == "5511988880000" and "https://app.exemplo/painel/n1." in texto
    token = texto.rsplit("/painel/", 1)[1]
    assert validar_token(token, SEG_PAINEL) == "n1"


def test_painel_de_outro_numero_nao_manda_link():
    repo = RepoFalso()  # paciente cadastrado pedindo "painel" vai para o bot normal
    chamadas = []
    cliente = ClienteFalso()
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, painel_segredo=SEG_PAINEL,
                           url_publica="https://app.exemplo"),
                    cliente=cliente, repo=repo, taco=object(), metricas=RepoMetricasFalso(),
                    responder_fn=lambda p, *a, **k: chamadas.append(p) or Resultado(acao="responder", mensagem_paciente="oi"))
    TestClient(app).post(URL, json=evento("painel"))
    assert chamadas == ["painel"] and all("/painel/" not in t for _, t in cliente.enviadas)
