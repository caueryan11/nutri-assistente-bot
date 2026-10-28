"""Níveis de consulta (simples / com acompanhamento), aviso de fim e campanha de reativação (sem banco)."""

from datetime import date, datetime
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from bot.acompanhamento import rodar_fim_acompanhamento, texto_fim
from bot.assistente import Resultado, responder
from bot.reativacao import TEXTO_PADRAO, ler_data, ler_lista, montar_mensagem, rodar_campanhas
from testes.test_agenda import CalendarioFalso, CONFIG
from testes.test_area_nutri import BASE, RepoAreaFalso
from testes.test_metricas import SEG_PAINEL, RepoMetricasFalso
from testes.test_unidade import PACIENTE, PLANO, ClienteFalso as ClaudeFalso, _chamada, _resposta, taco  # noqa: F401
from testes.test_whatsapp import SEGREDO, TOKEN, URL, ClienteFalso, RepoFalso, evento
from whatsapp import consentimento
from whatsapp.app import Config, criar_app

HOJE = date(2026, 9, 30)


def _final(texto, sugeridos=(), acao="responder", id_="f"):
    return _chamada("enviar_resposta", {"acao": acao, "mensagem_paciente": texto, "motivo": "m" if acao == "encaminhar" else "",
                                        "resumo_para_nutri": "r" if acao == "encaminhar" else "",
                                        "alimentos_sugeridos": list(sugeridos)}, id_)


# ---------------------------------------------------------------------
# Modo só agenda no bot
# ---------------------------------------------------------------------
def test_so_agenda_tira_ferramentas_de_plano_e_barra_sugestao(taco):
    claude = ClaudeFalso([_resposta(_final("Pode trocar por batata-doce!", ["batata-doce"], id_="f1")),
                          _resposta(_final("Vou passar para a Ana Souza; quer ver um horário de retorno?",
                                           acao="encaminhar", id_="f2"))])
    r = responder("Posso trocar o arroz?", PACIENTE, PLANO, taco=taco, cliente=claude, nome_nutri="Ana Souza",
                  somente_agenda="sem_acompanhamento")
    nomes = [f["name"] for f in claude.pedidos[0]["tools"]]
    assert "consultar_taco" not in nomes and "resumo_do_plano" not in nomes and "enviar_resposta" in nomes
    assert "faz parte do acompanhamento" in claude.pedidos[0]["system"][0]["text"]
    assert "só cuida da agenda" in claude.pedidos[1]["messages"][-1]["content"][0]["content"]
    assert r.acao == "encaminhar"


class RepoNiveis(RepoFalso):
    def __init__(self, acompanhamento=False, plano=True, **kw):
        super().__init__(plano=plano, **kw)
        self.nutri.modelo_acompanhamento = "por_paciente"
        self.nutri.google_calendar_id = "cal-1"
        self.nutri.agenda_config = CONFIG
        self.pac.acompanhamento_ativo = acompanhamento

    def consultas_futuras(self, paciente_id):
        return []


def _app(repo, cliente=None, reativacao=None, calendario=True):
    chamadas = []

    def responder_falso(pergunta, paciente, plano, **kw):
        chamadas.append({"pergunta": pergunta, "plano": plano, **kw})
        return Resultado(acao="responder", mensagem_paciente="ok")

    cliente = cliente or ClienteFalso()
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO), cliente=cliente, repo=repo,
                    taco=object(), responder_fn=responder_falso, calendario=CalendarioFalso() if calendario else None,
                    reativacao=reativacao)
    return TestClient(app), cliente, chamadas


def test_paciente_sem_acompanhamento_vai_em_modo_agenda():
    c, _, chamadas = _app(RepoNiveis(acompanhamento=False))
    c.post(URL, json=evento("Posso trocar o arroz?"))
    assert chamadas[0]["somente_agenda"] == "sem_acompanhamento" and chamadas[0]["agenda"] is not None


def test_paciente_com_acompanhamento_tem_o_bot_completo():
    c, _, chamadas = _app(RepoNiveis(acompanhamento=True))
    c.post(URL, json=evento("Posso trocar o arroz?"))
    assert "somente_agenda" not in chamadas[0]


def test_modelo_todos_mantem_o_comportamento_antigo():
    repo = RepoNiveis(acompanhamento=False)
    repo.nutri.modelo_acompanhamento = "todos"
    c, _, chamadas = _app(repo)
    c.post(URL, json=evento("Posso trocar o arroz?"))
    assert "somente_agenda" not in chamadas[0]


def test_sem_plano_com_agenda_marca_consulta_em_modo_agenda():
    repo = RepoNiveis(acompanhamento=True, plano=False)
    c, _, chamadas = _app(repo)
    c.post(URL, json=evento("Quero marcar um retorno"))
    assert chamadas[0]["somente_agenda"] == "sem_plano" and chamadas[0]["plano"] == {"refeicoes": []}


def test_sem_agenda_e_sem_acompanhamento_encaminha_sem_ia():
    c, cliente, chamadas = _app(RepoNiveis(acompanhamento=False), calendario=False)
    c.post(URL, json=evento("Posso trocar o arroz?"))
    assert chamadas == [] and cliente.enviadas[1][0] == "5511988880000"


# ---------------------------------------------------------------------
# Aviso de fim do acompanhamento
# ---------------------------------------------------------------------
class RepoFim:
    def __init__(self):
        self.proativas, self.desfeitos = [], []

    def reservar_fim_acompanhamento(self):
        return [{"paciente_id": "p1", "nutri_id": "n1", "primeiro_nome": "Maria", "telefone": "5511999990001",
                 "acompanhamento_ate": date(2026, 10, 4), "nutri_nome": "Ana Souza", "anterior": None},
                {"paciente_id": "p2", "nutri_id": "n1", "primeiro_nome": "João", "telefone": "falha",
                 "acompanhamento_ate": date(2026, 10, 4), "nutri_nome": "Ana Souza", "anterior": None}]

    def desfazer_fim_acompanhamento(self, pid, anterior):
        self.desfeitos.append(pid)

    def registrar_proativa(self, n, pid, texto, motivo, wa):
        self.proativas.append((pid, motivo))


def test_aviso_de_fim_convida_ao_retorno():
    repo, enviadas = RepoFim(), []

    def enviar(numero, texto):
        if numero == "falha":
            raise RuntimeError()
        enviadas.append(texto)
        return "wa"

    assert rodar_fim_acompanhamento(repo, enviar) == 1
    assert "vai até 04/10" in enviadas[0] and "retorno" in enviadas[0]
    assert repo.proativas == [("p1", "fim do acompanhamento")] and repo.desfeitos == ["p2"]
    assert "Ana Souza" in texto_fim("Maria", "Ana Souza", date(2026, 10, 4))


# ---------------------------------------------------------------------
# Aceite só depois do pedido do termo
# ---------------------------------------------------------------------
def test_aceite_sem_ter_visto_o_termo_nao_vale():
    repo = RepoFalso(consentimento=False, termo_pedido=False)
    c, cliente, _ = _app(repo, calendario=False)
    c.post(URL, json=evento("Sim", messageid="S1"))
    assert repo.consentimentos == [] and "responda 'Aceito'" in cliente.enviadas[0][1]
    c.post(URL, json=evento("Aceito", messageid="S2"))
    assert repo.consentimentos == [("p1", "v1.0")]


# ---------------------------------------------------------------------
# Campanha de reativação
# ---------------------------------------------------------------------
def test_ler_data():
    assert ler_data("15/03/2026", HOJE) == date(2026, 3, 15) and ler_data("3/26", HOJE) is None
    assert ler_data("02/2026", HOJE) == date(2026, 2, 1) and ler_data("2026-01-10", HOJE) == date(2026, 1, 10)
    assert ler_data("10/10/2026", HOJE) is None and ler_data("31/02/2026", HOJE) is None and ler_data("15/03/26", HOJE) == date(2026, 3, 15)


def test_ler_lista_do_excel():
    texto = ("Nome\tTelefone\tÚltima consulta\n"
             "Maria Silva\t(51) 99999-0000\t15/03/2026\n"
             "João Pereira; 51 98888-1111; 02/2026\n"
             "Ana Costa, 11999990002\n"
             "Sem Telefone; 123\n"
             "Repetida; (51) 99999-0000\n"
             "Data Ruim; 51977776666; ontem\n")
    validos, erros = ler_lista(texto, HOJE)
    assert [v["nome"] for v in validos] == ["Maria Silva", "João Pereira", "Ana Costa", "Data Ruim"]
    assert validos[0] == {"nome": "Maria Silva", "telefone": "5551999990000", "ultima_consulta": date(2026, 3, 15)}
    assert validos[2]["telefone"] == "5511999990002" and validos[3]["ultima_consulta"] is None
    assert any("WhatsApp inválido" in e for e in erros) and any("repetido" in e for e in erros)
    assert any("não entendida" in e for e in erros)


def test_mensagem_sempre_tem_parar():
    m = montar_mensagem(TEXTO_PADRAO, "Maria", "Ana Souza")
    assert m.startswith("Oi, Maria! Aqui é o assistente da nutricionista Ana Souza") and "PARAR" in m
    assert montar_mensagem("Oi {nome}! Responda PARAR para sair.", "Jo", "X") == "Oi Jo! Responda PARAR para sair."


class RepoCampanha:
    def __init__(self):
        self.devolvidos, self.proativas, self.concluidas = [], [], 0
        self.envios = [{"envio_id": "e1", "tentativas": 1, "paciente_id": "p1", "nutri_id": "n1", "primeiro_nome": "Maria",
                        "telefone": "5511999990001", "nutri_nome": "Ana Souza", "mensagem": TEXTO_PADRAO},
                       {"envio_id": "e2", "tentativas": 3, "paciente_id": "p2", "nutri_id": "n1", "primeiro_nome": "João",
                        "telefone": "falha", "nutri_nome": "Ana Souza", "mensagem": TEXTO_PADRAO}]
        self.respostas, self.recentes = [], set()

    def reservar_envios(self, limite):
        r, self.envios = self.envios[:limite], self.envios[limite:]
        return r

    def devolver_envio(self, eid, tentativas):
        self.devolvidos.append((eid, tentativas))

    def concluir_campanhas(self):
        self.concluidas += 1

    def registrar_proativa(self, n, pid, texto, motivo, wa):
        self.proativas.append((pid, motivo, texto))

    def campanha_recente(self, pid):
        return pid in self.recentes

    def marcar_resposta(self, pid):
        self.respostas.append(pid)


def test_rodar_campanhas_envia_devolve_e_conclui():
    repo, enviadas = RepoCampanha(), []

    def enviar(numero, texto):
        if numero == "falha":
            raise RuntimeError()
        enviadas.append((numero, texto))
        return "wa"

    assert rodar_campanhas(repo, enviar, por_minuto=3) == 1
    assert enviadas[0][0] == "5511999990001" and "Oi, Maria!" in enviadas[0][1] and "PARAR" in enviadas[0][1]
    assert repo.proativas[0][:2] == ("p1", "campanha de reativação")
    assert repo.devolvidos == [("e2", 3)] and repo.concluidas == 1


def test_resposta_a_campanha_pede_termo_contextual_e_depois_mostra_horarios():
    repo = RepoFalso(consentimento=False, termo_pedido=False)
    camp = RepoCampanha()
    camp.recentes.add("p1")
    c, cliente, chamadas = _app(repo, reativacao=camp, calendario=False)
    c.post(URL, json=evento("SIM", messageid="C1"))
    assert repo.consentimentos == [] and camp.respostas == ["p1"]
    assert cliente.enviadas[0][1] == consentimento.pedido("", reativacao=True)
    c.post(URL, json=evento("Aceito", messageid="C2"))
    assert repo.consentimentos == [("p1", "v1.0")] and cliente.enviadas[-1][1] == consentimento.CONFIRMACAO_REATIVACAO
    assert chamadas == []


# ---------------------------------------------------------------------
# Painel: níveis e reativação
# ---------------------------------------------------------------------
class RepoReativacaoPainel(RepoCampanha):
    def __init__(self):
        super().__init__()
        self.importados, self.criadas, self.pausas = [], [], []

    def importar(self, nutri_id, pessoas):
        self.importados += pessoas
        return {"criados": len(pessoas), "atualizados": 0}

    def publico(self, nutri_id, dias, sem_data):
        return {"total": 42 if dias == 90 else 0, "exemplos": ["Ana Costa", "João Pereira"]}

    def criar_campanha(self, nutri_id, mensagem, dias, sem_data):
        self.criadas.append((nutri_id, mensagem, dias, sem_data))
        return "c1", 42

    def campanhas(self, nutri_id):
        return [{"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "criada_em": datetime(2026, 9, 30, 10, 0),
                 "status": "enviando", "total": 42, "dias_sem_consulta": 90, "enviados": 30, "pendentes": 12,
                 "nao_enviados": 0, "responderam": 9, "agendaram": 5}]

    def pausar(self, nutri_id, cid, pausar):
        self.pausas.append((cid, pausar))


def _painel(reat):
    area = RepoAreaFalso()
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, painel_segredo=SEG_PAINEL,
                           url_publica="https://app.exemplo"),
                    cliente=ClienteFalso(), repo=RepoFalso(), taco=object(), metricas=RepoMetricasFalso(), area=area,
                    reativacao=reat)
    return TestClient(app), area


def _post(c, caminho, dados):
    return c.post(BASE + caminho, content=urlencode(dados), follow_redirects=False,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})


def test_painel_reativacao_importa_confere_e_envia():
    reat = RepoReativacaoPainel()
    c, _ = _painel(reat)
    pagina = c.get(BASE + "/reativacao")
    assert pagina.status_code == 200 and "Reativação de pacientes" in pagina.text
    assert "30 de 42" in pagina.text and "R$ 1.000,00" in pagina.text          # 5 agendaram × R$ 200
    r = _post(c, "/reativacao/importar", {"acao": "conferir", "lista": "Maria Silva; 51 99999-0000; 15/03/2026\nX; 1"})
    assert r.status_code == 200 and "1</b> paciente(s) prontos" in r.text and reat.importados == []
    r = _post(c, "/reativacao/importar", {"acao": "importar", "lista": "Maria Silva; 51 99999-0000; 15/03/2026"})
    assert reat.importados[0]["telefone"] == "5551999990000" and "Lista importada" in r.text
    r = _post(c, "/reativacao/campanha", {"acao": "conferir", "dias": "90", "mensagem": TEXTO_PADRAO})
    assert "42</b> paciente(s) vão receber" in r.text and "Oi, Maria!" in r.text and "PARAR" in r.text
    r = _post(c, "/reativacao/campanha", {"acao": "enviar", "dias": "90", "mensagem": TEXTO_PADRAO})
    assert r.status_code == 400 and "confirme que são seus pacientes" in r.text and reat.criadas == []
    r = _post(c, "/reativacao/campanha", {"acao": "enviar", "dias": "90", "mensagem": TEXTO_PADRAO, "confirmo": "1"})
    assert r.status_code == 303 and reat.criadas[0][2] == 90
    r = _post(c, "/reativacao/campanha", {"acao": "conferir", "dias": "90", "mensagem": "Oi! volta aí"})
    assert r.status_code == 400 and "{nome}" in r.text
    r = _post(c, "/reativacao/campanha/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/pausar", {})
    assert r.status_code == 303 and reat.pausas == [("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", True)]


def test_consultorio_salva_modelo_e_validade():
    c, area = _painel(RepoReativacaoPainel())
    r = _post(c, "/consultorio", {"modelo_acompanhamento": "por_paciente", "acompanhamento_dias": "30",
                                  "duracao_min": "60", "antecedencia_horas": "12", "janela_dias": "14"})
    assert r.status_code == 303
    assert area.consultorios["n1"]["modelo_acompanhamento"] == "por_paciente"
    assert area.consultorios["n1"]["acompanhamento_dias"] == 30
    r = _post(c, "/consultorio", {"modelo_acompanhamento": "por_paciente", "acompanhamento_dias": "500",
                                  "duracao_min": "60", "antecedencia_horas": "12", "janela_dias": "14"})
    assert r.status_code == 400
