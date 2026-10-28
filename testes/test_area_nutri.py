"""Área da nutri: plano em texto, validações e rotas (banco simulado)."""

import copy
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from bot.metricas import gerar_token
from bot.plano_texto import de_texto, mesclar_extras, para_texto
from testes.test_metricas import SEG_PAINEL, RepoMetricasFalso
from testes.test_unidade import PLANO
from testes.test_whatsapp import SEGREDO, TOKEN, ClienteFalso, RepoFalso
from whatsapp.app import Config, criar_app
from whatsapp.area_repo import TelefoneDuplicado
from whatsapp.painel import consultorio_do_form, form_do_consultorio, lista, normalizar_telefone, valor_reais

P1 = "11111111-1111-1111-1111-111111111111"
P_OUTRA = "22222222-2222-2222-2222-222222222222"


# ---------------------------------------------------------------------
# Plano em texto
# ---------------------------------------------------------------------
def test_plano_de_exemplo_vai_e_volta_sem_perder_nada():
    plano, erros = de_texto(para_texto(PLANO))
    assert erros == []
    assert plano["refeicoes"] == PLANO["refeicoes"] and plano["observacoes_gerais"] == PLANO["observacoes_gerais"]


def test_texto_livre_da_nutri():
    texto = """#Café da manhã 7h30
- Pão francês | 1 unidade (50 g) | tapioca (45 g);  2 fatias de pão integral (50 g)
Café sem açúcar

# Lanche
Iogurte natural | 1 pote (170 g)

observações
• Beber 2 L de água
"""
    plano, erros = de_texto(texto)
    assert erros == []
    cafe, lanche = plano["refeicoes"]
    assert cafe["nome"] == "Café da manhã" and cafe["horario"] == "07:30"
    assert cafe["itens"][0] == {"alimento": "Pão francês", "quantidade": "1 unidade (50 g)",
                                "substituicoes": ["tapioca (45 g)", "2 fatias de pão integral (50 g)"]}
    assert cafe["itens"][1] == {"alimento": "Café sem açúcar", "quantidade": "", "substituicoes": []}
    assert "horario" not in lanche and plano["observacoes_gerais"] == ["Beber 2 L de água"]


@pytest.mark.parametrize("texto, trecho", [
    ("Arroz | 100 g", "antes da primeira refeição"),
    ("# Almoço 25:00\nArroz", "horário inválido"),
    ("# Almoço\n", "sem itens"),
    ("", "pelo menos uma refeição"),
    ("# Almoço\nA | b | c | d", "no máximo duas barras"),
])
def test_erros_do_plano(texto, trecho):
    _, erros = de_texto(texto)
    assert any(trecho in e for e in erros), erros


def test_mesclar_extras_mantem_id_taco():
    antigo = {"refeicoes": [{"nome": "Almoço", "itens": [{"alimento": "Arroz integral cozido", "quantidade": "100 g",
                                                           "substituicoes": [], "id_taco": 1}]}]}
    novo, _ = de_texto("# Almoço 12:00\nArroz integral cozido | 120 g")
    assert mesclar_extras(novo, antigo)["refeicoes"][0]["itens"][0]["id_taco"] == 1


# ---------------------------------------------------------------------
# Validações
# ---------------------------------------------------------------------
@pytest.mark.parametrize("entrada, saida", [("(51) 99999-0000", "5551999990000"), ("+55 47 90000-0024", "5547900000024"),
                                            ("4733334444", "554733334444"), ("123", None), ("", None)])
def test_normalizar_telefone(entrada, saida):
    assert normalizar_telefone(entrada) == saida


def test_lista_e_valor():
    assert lista("amendoim, Camarão\ncamarão; lactose") == ["amendoim", "Camarão", "lactose"]
    assert valor_reais("R$ 1.250,50") == (Decimal("1250.50"), None)
    assert valor_reais("250")[0] == Decimal("250.00") and valor_reais("")[0] is None
    assert valor_reais("abc")[1] and valor_reais("-5")[1]


def test_consultorio_do_form():
    form = {"valor_consulta": "250,00", "atende_convenio": "1", "convenios_aceitos": "Unimed, Bradesco Saúde",
            "politica_reembolso": "Recibo com CRN.", "apresentacao_servico": "", "telefone_aviso": "(51) 90000-0022",
            "dia_1": "08:00-12:00, 14:00-18:00", "dia_3": "9:00-12:00", "dia_6": "",
            "duracao_min": "50", "antecedencia_horas": "12", "janela_dias": "14"}
    dados, erros = consultorio_do_form(form)
    assert erros == []
    assert dados["valor_consulta"] == Decimal("250.00") and dados["atende_convenio"] is True
    assert dados["convenios_aceitos"] == ["Unimed", "Bradesco Saúde"] and dados["apresentacao_servico"] is None
    assert dados["telefone_aviso"] == "5551900000022"
    assert dados["agenda_config"] == {"duracao_min": 50, "antecedencia_horas": 12, "janela_dias": 14,
                                      "atendimento": {"1": ["08:00-12:00", "14:00-18:00"], "3": ["09:00-12:00"]}}
    # e volta para o formulário
    f = form_do_consultorio({**dados, "valor_consulta": dados["valor_consulta"]})
    assert f["valor_consulta"] == "250,00" and f["dia_1"] == "08:00-12:00, 14:00-18:00" and f["dia_2"] == ""


def test_consultorio_com_erros():
    _, erros = consultorio_do_form({"dia_2": "12:00-08:00, manhã", "duracao_min": "5", "antecedencia_horas": "x",
                                    "janela_dias": "14", "telefone_aviso": "123"})
    texto = " ".join(erros)
    assert "12:00-08:00" in texto and "manhã" in texto and "Duração" in texto and "Antecedência" in texto
    assert "avisos inválido" in texto


# ---------------------------------------------------------------------
# Rotas
# ---------------------------------------------------------------------
class RepoAreaFalso:
    def __init__(self):
        self.dados = {
            "n1": {P1: {"id": P1, "nome": "Maria Silva", "telefone": "5511999990001", "ativo": True,
                        "status_consentimento": "aceito", "consentimento_em": datetime(2026, 9, 1),
                        "anamnese": {"alergias": ["amendoim"]}, "plano": copy.deepcopy(PLANO),
                        "vigente_desde": date(2026, 9, 1)}},
            "n2": {P_OUTRA: {"id": P_OUTRA, "nome": "Paciente de Outra Nutri", "telefone": "5511988887777",
                             "ativo": True, "status_consentimento": "aceito", "anamnese": {}, "plano": None,
                             "vigente_desde": None}},
        }
        self.consultorios = {"n1": {"nome": "Ana Souza", "valor_consulta": Decimal("200.00"), "atende_convenio": False,
                                    "convenios_aceitos": [], "agenda_config": {}, "google_calendar_id": "cal"}}
        self.salvos = []

    def pacientes(self, nutri_id):
        return [{**p, "tem_plano": bool(p.get("plano")), "tem_anamnese": bool(p.get("anamnese")),
                 "proxima_consulta": None} for p in self.dados.get(nutri_id, {}).values()]

    def paciente(self, nutri_id, pid):
        p = self.dados.get(nutri_id, {}).get(pid)
        return copy.deepcopy(p) if p else None

    def criar_paciente(self, nutri_id, nome, telefone):
        if any(p["telefone"] == telefone for p in self.dados.get(nutri_id, {}).values()):
            raise TelefoneDuplicado()
        pid = "33333333-3333-3333-3333-333333333333"
        self.dados.setdefault(nutri_id, {})[pid] = {"id": pid, "nome": nome, "telefone": telefone, "ativo": True,
                                                     "status_consentimento": "pendente", "anamnese": {}, "plano": None,
                                                     "vigente_desde": None}
        return pid

    def atualizar_paciente(self, nutri_id, pid, nome, telefone, ativo):
        self.dados[nutri_id][pid].update(nome=nome, telefone=telefone, ativo=ativo)
        return True

    def salvar_anamnese(self, nutri_id, pid, anamnese):
        self.dados[nutri_id][pid]["anamnese"] = anamnese
        return True

    def salvar_plano(self, nutri_id, pid, conteudo):
        self.salvos.append((nutri_id, pid, conteudo))
        self.dados[nutri_id][pid]["plano"] = conteudo
        return True

    def excluir_paciente(self, nutri_id, pid):
        return self.dados[nutri_id].pop(pid, None) is not None

    def consultorio(self, nutri_id):
        return self.consultorios.get(nutri_id)

    def salvar_consultorio(self, nutri_id, dados):
        self.consultorios[nutri_id].update(dados)


@pytest.fixture
def area():
    return RepoAreaFalso()


@pytest.fixture
def cliente(area):
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, painel_segredo=SEG_PAINEL,
                           url_publica="https://app.exemplo"),
                    cliente=ClienteFalso(), repo=RepoFalso(), taco=object(), metricas=RepoMetricasFalso(), area=area)
    return TestClient(app)


BASE = "/painel/" + gerar_token("n1", SEG_PAINEL)


def _post(c, caminho, dados):
    return c.post(BASE + caminho, content=urlencode(dados), follow_redirects=False,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})


def test_abas_e_lista_de_pacientes(cliente):
    geral = cliente.get(BASE)
    assert geral.status_code == 200 and f'href="{BASE}/pacientes"' in geral.text
    r = cliente.get(BASE + "/pacientes")
    assert r.status_code == 200 and "Maria Silva" in r.text and "Paciente de Outra Nutri" not in r.text
    assert r.headers["cache-control"] == "no-store" and "form-action 'self'" in r.headers["content-security-policy"]


def test_nao_ve_paciente_de_outra_nutri(cliente):
    assert cliente.get(f"{BASE}/pacientes/{P_OUTRA}").status_code == 404
    assert _post(cliente, f"/pacientes/{P_OUTRA}/anamnese", {"alergias": "x"}).status_code == 404
    assert cliente.get(f"{BASE}/pacientes/nao-e-uuid").status_code == 404


def test_link_invalido_em_todas_as_telas(cliente):
    for caminho in ("/pacientes", f"/pacientes/{P1}", "/consultorio"):
        assert cliente.get("/painel/token-falso" + caminho).status_code == 404
    assert cliente.post("/painel/token-falso/consultorio", content="valor_consulta=1").status_code == 404


def test_cadastrar_paciente(cliente, area):
    r = _post(cliente, "/pacientes/novo", {"nome": "João Pereira", "telefone": "(51) 98888-1111"})
    assert r.status_code == 303 and r.headers["location"].endswith("?aviso=criado")
    novo = area.paciente("n1", "33333333-3333-3333-3333-333333333333")
    assert novo["telefone"] == "5551988881111"
    pagina = cliente.get(r.headers["location"])
    assert "Paciente cadastrado" in pagina.text and "Aguardando aceite" in pagina.text


def test_cadastro_com_erro_e_duplicado(cliente):
    r = _post(cliente, "/pacientes/novo", {"nome": "João", "telefone": "12"})
    assert r.status_code == 400 and "nome completo" in r.text and "WhatsApp inválido" in r.text
    r = _post(cliente, "/pacientes/novo", {"nome": "Outra Maria", "telefone": "11 99999-0001"})
    assert r.status_code == 400 and "já existe um paciente" in r.text


def test_pagina_do_paciente_mostra_plano_e_conflitos(cliente):
    r = cliente.get(f"{BASE}/pacientes/{P1}")
    assert r.status_code == 200 and "# Café da manhã 07:00" in r.text and "Ativo desde 01/09/2026" in r.text
    assert 'value="amendoim"' in r.text


def test_salvar_anamnese(cliente, area):
    r = _post(cliente, f"/pacientes/{P1}/anamnese", {"alergias": "amendoim, camarão", "intolerancias": "lactose",
                                                      "restricoes": "", "nao_gosta": "beterraba", "observacoes": "Refluxo"})
    assert r.status_code == 303
    assert area.dados["n1"][P1]["anamnese"] == {"alergias": ["amendoim", "camarão"], "intolerancias": ["lactose"],
                                                 "restricoes": [], "nao_gosta": ["beterraba"], "observacoes": "Refluxo"}


def test_conferir_plano_nao_salva_e_mostra_conflito(cliente, area):
    area.dados["n1"][P1]["anamnese"] = {"intolerancias": ["lactose"]}
    r = _post(cliente, f"/pacientes/{P1}/plano", {"acao": "conferir",
                                                   "plano": "# Lanche 10:00\nIogurte natural | 1 pote\nMaçã | 1 unidade (130 g)"})
    assert r.status_code == 200 and area.salvos == []
    assert "conflita com a anamnese" in r.text and "Iogurte natural" in r.text
    assert "sem gramas" in r.text                       # "1 pote" não tem gramas


def test_salvar_plano(cliente, area):
    r = _post(cliente, f"/pacientes/{P1}/plano", {"acao": "salvar", "plano": "# Almoço 12:00\nArroz | 100 g"})
    assert r.status_code == 303
    (_, pid, conteudo), = area.salvos
    assert pid == P1 and conteudo["paciente"] == "Maria Silva" and conteudo["nutricionista"] == "Ana Souza"
    assert conteudo["refeicoes"] == [{"nome": "Almoço", "horario": "12:00",
                                      "itens": [{"alimento": "Arroz", "quantidade": "100 g", "substituicoes": []}]}]


def test_plano_com_erro_nao_salva(cliente, area):
    r = _post(cliente, f"/pacientes/{P1}/plano", {"acao": "salvar", "plano": "Arroz | 100 g"})
    assert r.status_code == 400 and area.salvos == [] and "primeira refeição" in r.text
    assert "Arroz | 100 g" in r.text                    # o texto digitado não se perde


def test_excluir_exige_nome(cliente, area):
    r = _post(cliente, f"/pacientes/{P1}/excluir", {"confirmacao": "maria"})
    assert r.status_code == 400 and P1 in area.dados["n1"]
    r = _post(cliente, f"/pacientes/{P1}/excluir", {"confirmacao": "  maria   silva "})
    assert r.status_code == 303 and P1 not in area.dados["n1"]


def test_dados_do_paciente_e_desativar(cliente, area):
    r = _post(cliente, f"/pacientes/{P1}/dados", {"nome": "Maria da Silva", "telefone": "11 99999-0001"})
    assert r.status_code == 303
    assert area.dados["n1"][P1]["nome"] == "Maria da Silva" and area.dados["n1"][P1]["ativo"] is False


def test_consultorio(cliente, area):
    r = cliente.get(BASE + "/consultorio")
    assert r.status_code == 200 and 'value="200,00"' in r.text
    r = _post(cliente, "/consultorio", {"valor_consulta": "280", "dia_2": "13:00-19:00", "duracao_min": "60",
                                        "antecedencia_horas": "24", "janela_dias": "21"})
    assert r.status_code == 303
    c = area.consultorios["n1"]
    assert c["valor_consulta"] == Decimal("280.00") and c["agenda_config"]["atendimento"] == {"2": ["13:00-19:00"]}
    r = _post(cliente, "/consultorio", {"valor_consulta": "abc", "duracao_min": "60", "antecedencia_horas": "24",
                                        "janela_dias": "21"})
    assert r.status_code == 400 and 'value="abc"' in r.text


def test_escapa_html(cliente, area):
    area.dados["n1"][P1]["nome"] = "<script>alert(1)</script> Silva"
    r = cliente.get(f"{BASE}/pacientes/{P1}")
    assert "<script>alert(1)</script>" not in r.text and "&lt;script&gt;" in r.text


def test_formulario_grande_demais(cliente):
    r = _post(cliente, f"/pacientes/{P1}/plano", {"plano": "x" * 250_000})
    assert r.status_code == 413
