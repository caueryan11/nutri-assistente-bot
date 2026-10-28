"""Rascunho de resposta: o aviso traz a sugestão e a nutri aprova ("sim"), descarta ("não") ou escreve a versão dela.
Sem internet: cliente do WhatsApp, banco e IA simulados."""

from datetime import datetime
from types import SimpleNamespace as NS

import anthropic
import pytest
from fastapi.testclient import TestClient

from bot import rascunho as mod_rascunho
from bot.assistente import Resultado
from bot.rascunho import Rascunho
from testes.test_whatsapp import TOKEN, SEGREDO, URL, ClienteFalso, RepoFalso, evento
from whatsapp.app import Config, criar_app

NUTRI_AVISO = "5511988880000"
PACIENTE = "5511999990001"
SUGESTAO = Rascunho(resposta="Pode sim, Maria! Prefira o whey isolado, depois do almoço.",
                    fontes=[{"titulo": "Revisão sobre whey", "url": "https://pubmed.ncbi.nlm.nih.gov/123/"}],
                    atencao="Ela usa omeprazol: confira o horário.", confianca="media")


class RepoComRascunhos(RepoFalso):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.rascunhos = []
        self.saidas_completas = []

    def registrar_saida(self, nutri, paciente, r, wa_id):
        super().registrar_saida(nutri, paciente, r, wa_id)
        self.saidas_completas.append((paciente.id, r.motivo, r.mensagem_paciente))

    def criar_rascunho(self, nutri_id, paciente_id, pergunta, rascunho, aviso_wa_id):
        self.rascunhos.append({"id": f"r{len(self.rascunhos) + 1}", "paciente_id": paciente_id, "nome": self.pac.nome,
                               "primeiro_nome": self.pac.primeiro_nome, "telefone": self.pac.telefone,
                               "pergunta": pergunta, "resposta": (rascunho.resposta or None) if rascunho else None,
                               "versao_nutri": False, "status": "pendente", "expirado": False,
                               "aviso_wa_id": aviso_wa_id, "texto_enviado": None})

    def _com_consentimento(self, r):
        return {**r, "consentimento_em": self.pac.consentimento_em}

    def rascunho_por_aviso(self, nutri_id, aviso_wa_id):
        r = next((r for r in self.rascunhos if r["aviso_wa_id"] == aviso_wa_id), None)
        return self._com_consentimento(r) if r else None

    def rascunhos_pendentes(self, nutri_id):
        return [self._com_consentimento(r) for r in reversed(self.rascunhos) if r["status"] == "pendente"]

    def guardar_versao_nutri(self, rascunho_id, texto):
        r = next(r for r in self.rascunhos if r["id"] == rascunho_id)
        r["resposta"], r["versao_nutri"] = texto, True

    def decidir_rascunho(self, rascunho_id, status, texto_enviado):
        r = next(r for r in self.rascunhos if r["id"] == rascunho_id)
        if r["status"] != "pendente":
            return False
        r["status"], r["texto_enviado"] = status, texto_enviado
        return True


def app_rascunho(repo=None, sugestao=SUGESTAO, resultado=None, falhar=False):
    repo = repo or RepoComRascunhos()
    cliente, chamadas = ClienteFalso(), []

    def responder_falso(pergunta, paciente, plano, **kw):
        return resultado or Resultado(acao="encaminhar", mensagem_paciente="Passei para a Ana.",
                                      motivo="suplemento", resumo_para_nutri="Pergunta se pode tomar whey com gastrite.")

    def rascunho_falso(pergunta, resumo, primeiro_nome, plano, anamnese, nutri, **kw):
        chamadas.append(pergunta)
        if falhar:
            raise RuntimeError("API fora")
        return sugestao

    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO), cliente=cliente, repo=repo,
                    taco=object(), responder_fn=responder_falso, rascunho_fn=rascunho_falso)
    return TestClient(app), cliente, repo, chamadas


_seq = iter(range(10_000))


def da_nutri(texto, citada=""):
    """Mensagem vinda do WhatsApp de avisos da nutri (id novo a cada chamada)."""
    extra = {"messageid": f"NUTRI{next(_seq)}", "chatid": f"{NUTRI_AVISO}@s.whatsapp.net",
             "sender_pn": f"{NUTRI_AVISO}@s.whatsapp.net"}
    if citada:
        extra["quoted"] = citada
    return evento(texto, **extra)


def encaminhar(c, texto="posso tomar whey?", msg_id="PAC1"):
    c.post(URL, json=evento(texto, messageid=msg_id))


# ---------------------------------------------------------------------
# O aviso
# ---------------------------------------------------------------------
def test_aviso_traz_sugestao_fontes_e_como_responder():
    c, cliente, repo, chamadas = app_rascunho()
    encaminhar(c)
    assert cliente.enviadas[0] == (PACIENTE, "Passei para a Ana.")
    numero, aviso = cliente.enviadas[1]
    assert numero == NUTRI_AVISO and chamadas == ["posso tomar whey?"]
    assert aviso.startswith("🟡 *Responder · Maria Silva*\nPergunta se pode tomar whey com gastrite.")
    assert '💬 *Sugestão de resposta:*\n"Pode sim, Maria! Prefira o whey isolado, depois do almoço."' in aviso
    assert "📚 Revisão sobre whey (pubmed.ncbi.nlm.nih.gov/123)" in aviso
    assert "⚠️ Ela usa omeprazol: confira o horário." in aviso
    assert "Responda *sim* para enviar, *não* para descartar, ou escreva a sua versão." in aviso
    assert repo.rascunhos[0]["aviso_wa_id"] == "wa-saida-2" and repo.rascunhos[0]["status"] == "pendente"


def test_sem_evidencia_avisa_sem_sugestao():
    c, cliente, repo, _ = app_rascunho(sugestao=Rascunho(resposta="", atencao="Depende de exame de ferritina.",
                                                         confianca="sem_evidencia"))
    encaminhar(c)
    aviso = cliente.enviadas[1][1]
    assert "Sugestão de resposta" not in aviso and "💬 Sem sugestão de resposta: Depende de exame de ferritina." in aviso
    assert repo.rascunhos[0]["resposta"] is None   # linha existe: dá para responder citando o aviso


def test_falha_da_ia_nao_impede_o_aviso():
    c, cliente, repo, _ = app_rascunho(falhar=True)
    encaminhar(c)
    assert cliente.enviadas[1][1].startswith("🟡 *Responder · Maria Silva*") and "Sugestão" not in cliente.enviadas[1][1]
    assert len(repo.rascunhos) == 1


def test_midia_nao_gera_rascunho():
    c, cliente, repo, chamadas = app_rascunho()
    c.post(URL, json=evento("", messageType="AudioMessage", messageid="AUD1"))
    assert chamadas == [] and repo.rascunhos == [] and "áudio" in cliente.enviadas[1][1]


# ---------------------------------------------------------------------
# A decisão da nutri
# ---------------------------------------------------------------------
@pytest.mark.parametrize("resposta", ["sim", "Sim!", "pode enviar", "ok", "1"])
def test_sim_envia_a_sugestao_como_mensagem_da_nutri(resposta):
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    c.post(URL, json=da_nutri(resposta))
    assert cliente.enviadas[2] == (PACIENTE, "*Ana Souza:* Pode sim, Maria! Prefira o whey isolado, depois do almoço.")
    assert cliente.enviadas[3] == (NUTRI_AVISO, "✅ Enviado para Maria Silva.")
    assert repo.rascunhos[0]["status"] == "enviado"
    assert repo.saidas_completas[-1][:2] == ("p1", "resposta da nutri: sugestão aprovada")


def test_nao_descarta_e_nada_vai_para_o_paciente():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    c.post(URL, json=da_nutri("não"))
    assert [n for n, _ in cliente.enviadas[2:]] == [NUTRI_AVISO] and "descartada" in cliente.enviadas[2][1]
    assert repo.rascunhos[0]["status"] == "descartado"


def test_texto_citando_o_aviso_vai_direto_como_versao_dela():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    c.post(URL, json=da_nutri("Maria, espere o retorno para começarmos o whey, combinado?", citada="wa-saida-2"))
    assert cliente.enviadas[2] == (PACIENTE, "*Ana Souza:* Maria, espere o retorno para começarmos o whey, combinado?")
    assert cliente.enviadas[3] == (NUTRI_AVISO, "✅ Sua versão foi enviada para Maria Silva.")
    assert repo.rascunhos[0]["status"] == "editado"


def test_texto_sem_citar_pede_confirmacao_antes_de_enviar():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    c.post(URL, json=da_nutri("Maria, espere o retorno para começarmos o whey."))
    assert cliente.enviadas[2][0] == NUTRI_AVISO and cliente.enviadas[2][1].startswith("Vou enviar para Maria Silva:")
    assert len(cliente.enviadas) == 3                     # nada foi para o paciente ainda
    c.post(URL, json=da_nutri("sim"))
    assert cliente.enviadas[3] == (PACIENTE, "*Ana Souza:* Maria, espere o retorno para começarmos o whey.")
    assert repo.rascunhos[0]["status"] == "editado"


def test_texto_curto_sem_citar_nao_vira_resposta():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    c.post(URL, json=da_nutri("obrigada"))
    assert cliente.enviadas[2][0] == NUTRI_AVISO and "Não entendi" in cliente.enviadas[2][1]
    assert repo.rascunhos[0]["status"] == "pendente" and repo.rascunhos[0]["resposta"] == SUGESTAO.resposta


def test_dois_pendentes_e_texto_sem_citar_pede_para_citar():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c, msg_id="PAC1")
    encaminhar(c, "e creatina?", msg_id="PAC2")
    c.post(URL, json=da_nutri("Melhor conversarmos no retorno sobre suplementos."))
    assert "responda *citando* o aviso" in cliente.enviadas[-1][1]
    assert all(r["status"] == "pendente" and not r["versao_nutri"] for r in repo.rascunhos)


def test_sim_repetido_nao_envia_duas_vezes():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    c.post(URL, json=da_nutri("sim", citada="wa-saida-2"))
    c.post(URL, json=da_nutri("sim", citada="wa-saida-2"))
    para_paciente = [t for n, t in cliente.enviadas if n == PACIENTE]
    assert len(para_paciente) == 2                         # "Passei para a Ana." + a resposta, uma vez só
    assert "já foi resolvida" in cliente.enviadas[-1][1]


def test_paciente_que_mandou_parar_nao_recebe():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    repo.pac.consentimento_em = None
    c.post(URL, json=da_nutri("sim"))
    assert "saiu do assistente" in cliente.enviadas[-1][1] and repo.rascunhos[0]["status"] == "pendente"
    assert [n for n, _ in cliente.enviadas].count(PACIENTE) == 1


def test_sugestao_expirada_nao_e_enviada():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    repo.rascunhos[0]["expirado"] = True
    c.post(URL, json=da_nutri("sim"))
    assert "mais de 48 h" in cliente.enviadas[-1][1] and repo.rascunhos[0]["status"] == "pendente"


def test_sem_pendente_o_numero_de_aviso_segue_normal():
    c, cliente, repo, _ = app_rascunho()
    c.post(URL, json=da_nutri("sim"))
    assert cliente.enviadas == []


def test_legenda_de_plano_nao_e_tratada_como_resposta():
    c, cliente, repo, _ = app_rascunho()
    encaminhar(c)
    c.post(URL, json=da_nutri("Joana Pereira 11 98888-7777"))
    assert repo.rascunhos[0]["versao_nutri"] is False and len(cliente.enviadas) == 2


# ---------------------------------------------------------------------
# Gerador (bot/rascunho.py) com cliente da API simulado
# ---------------------------------------------------------------------
def _resp(blocos, stop="tool_use"):
    return NS(content=blocos, stop_reason=stop, usage=NS(input_tokens=100, output_tokens=20))


class ClienteApi:
    def __init__(self, respostas):
        self.respostas, self.chamadas = list(respostas), []
        self.messages = self

    def create(self, **kw):
        self.chamadas.append(kw)
        return self.respostas.pop(0)


def _busca(*urls):
    return [NS(type="server_tool_use", id="s1", name="web_search"),
            NS(type="web_search_tool_result", content=[NS(url=u) for u in urls])]


def _entrega(**entrada):
    return NS(type="tool_use", id="t1", name="entregar_rascunho", input=entrada)


def test_gerador_so_mantem_fontes_que_vieram_da_busca_e_de_dominio_confiavel():
    api = ClienteApi([_resp(_busca("https://pubmed.ncbi.nlm.nih.gov/111/", "https://blogfit.com/whey") + [_entrega(
        resposta="Pode sim.", confianca="alta", atencao="",
        fontes=[{"titulo": "A", "url": "https://pubmed.ncbi.nlm.nih.gov/111/"},     # ok
                {"titulo": "B", "url": "https://pubmed.ncbi.nlm.nih.gov/999/"},     # inventada
                {"titulo": "C", "url": "https://blogfit.com/whey"}])])])            # fora da lista
    r = mod_rascunho.gerar("posso tomar whey?", "suplemento", "Maria", None, None, "Ana", cliente=api)
    assert r.resposta == "Pode sim." and [f["titulo"] for f in r.fontes] == ["A"] and r.buscas == 1
    ferramentas = api.chamadas[0]["tools"]
    assert ferramentas[0]["type"] == "web_search_20260209" and "pubmed.ncbi.nlm.nih.gov" in ferramentas[0]["allowed_domains"]
    assert "Maria" not in str(ferramentas)                  # dado do paciente não vai na configuração da busca


def test_gerador_retoma_pause_turn_e_sem_evidencia_zera_resposta():
    api = ClienteApi([_resp(_busca("https://scielo.br/x"), stop="pause_turn"),
                      _resp([_entrega(resposta="algo", confianca="sem_evidencia", atencao="Falta exame.", fontes=[])])])
    r = mod_rascunho.gerar("tenho anemia, posso...?", "", "Maria", None, None, "Ana", cliente=api)
    assert len(api.chamadas) == 2 and r.resposta == "" and r.atencao == "Falta exame." and r.confianca == "sem_evidencia"


def test_gerador_devolve_none_se_a_api_falhar():
    class Quebrado:
        class messages:
            @staticmethod
            def create(**kw):
                raise anthropic.APIConnectionError(request=NS(method="POST", url="x", headers={}))
    assert mod_rascunho.gerar("x", "", "Maria", None, None, "Ana", cliente=Quebrado()) is None
