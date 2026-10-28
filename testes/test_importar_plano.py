"""Importação do plano por PDF/foto: leitura (Claude simulado), painel e WhatsApp da nutri."""

import copy

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from bot.importar_plano import ErroImportacao, Importacao, importar, mesclar_anamnese, tipo_do_arquivo
from bot.metricas import gerar_token
from testes.test_area_nutri import BASE, P1, RepoAreaFalso
from testes.test_metricas import SEG_PAINEL, RepoMetricasFalso
from testes.test_unidade import ClienteFalso as ClaudeFalso, _chamada, _resposta
from testes.test_whatsapp import SEGREDO, TOKEN, URL, ClienteFalso, RepoFalso, evento
from whatsapp.app import Config, criar_app
from whatsapp.importacao import Pendentes, ler_legenda

PDF = b"%PDF-1.4 plano de teste"
JPG = b"\xff\xd8\xff\xe0 foto"
BRUTO = {
    "eh_plano_alimentar": True, "paciente_nome": "Joana Pereira",
    "refeicoes": [{"nome": "Café da manhã", "horario": "07:00", "itens": [
        {"alimento": "Pão integral", "quantidade": "2 fatias (50 g)", "substituicoes": ["Tapioca (45 g)"]},
        {"alimento": "Ovo mexido", "quantidade": "2 unidades", "substituicoes": []}]},
        {"nome": "Almoço", "horario": "", "itens": [
            {"alimento": "Frango | grelhado", "quantidade": "150g", "substituicoes": ["Tilápia; 150 g"]}]}],
    "observacoes_gerais": ["Beber 2 L de água"],
    "anamnese": {"alergias": ["camarão"], "intolerancias": [], "restricoes": [], "nao_gosta": ["jiló"]},
    "avisos": ["texto cortado no rodapé"],
}


def _claude(bruto=BRUTO):
    return ClaudeFalso([_resposta(_chamada("registrar_plano", copy.deepcopy(bruto)))])


# ---------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------
def test_tipo_pelo_conteudo():
    assert tipo_do_arquivo(PDF) == "application/pdf" and tipo_do_arquivo(JPG) == "image/jpeg"
    assert tipo_do_arquivo(b"\x89PNG\r\n\x1a\n...") == "image/png"
    assert tipo_do_arquivo(b"PK\x03\x04 word") is None


def test_importar_pdf():
    claude = _claude()
    imp = importar(PDF, "application/pdf", cliente=claude)
    bloco = claude.pedidos[0]["messages"][0]["content"][0]
    assert bloco["type"] == "document" and bloco["source"]["media_type"] == "application/pdf"
    assert "tool_choice" not in claude.pedidos[0]                     # Sonnet 5.5 não aceita forçar
    assert claude.pedidos[0]["tools"][0]["strict"] is True
    cafe, almoco = imp.plano["refeicoes"]
    assert cafe["horario"] == "07:00" and "horario" not in almoco
    assert almoco["itens"][0] == {"alimento": "Frango / grelhado", "quantidade": "150g", "substituicoes": ["Tilápia, 150 g"]}
    assert imp.paciente_nome == "Joana Pereira" and imp.anamnese == {"alergias": ["camarão"], "nao_gosta": ["jiló"]}
    assert imp.avisos == ["texto cortado no rodapé"] and imp.total_itens == 3
    assert imp.itens_sem_gramas == ["Ovo mexido"]
    assert imp.plano["observacoes_gerais"] == ["Beber 2 L de água"]


def test_importar_foto_usa_bloco_de_imagem():
    claude = _claude()
    importar(JPG, None, cliente=claude)
    assert claude.pedidos[0]["messages"][0]["content"][0]["type"] == "image"


def test_sem_chamar_a_ferramenta_pede_de_novo():
    texto = _resposta(type("T", (), {"type": "text", "text": "Vou transcrever."})(), stop="end_turn")
    claude = ClaudeFalso([texto, _resposta(_chamada("registrar_plano", copy.deepcopy(BRUTO)))])
    assert importar(PDF, cliente=claude).total_itens == 3 and len(claude.pedidos) == 2


@pytest.mark.parametrize("dados, trecho", [(b"", "vazio"), (b"PK\x03\x04docx", "Word"), (b"%PDF" + b"0" * (11 * 1024 * 1024), "10 MB")],
                         ids=["vazio", "word", "grande"])
def test_arquivos_recusados(dados, trecho):
    with pytest.raises(ErroImportacao, match=trecho):
        importar(dados, cliente=_claude())


def test_arquivo_que_nao_e_plano():
    with pytest.raises(ErroImportacao, match="não parece ser um plano"):
        importar(PDF, cliente=_claude({**BRUTO, "eh_plano_alimentar": False, "refeicoes": []}))


def test_erro_da_api_vira_mensagem_para_a_nutri():
    erro = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    with pytest.raises(ErroImportacao, match="indisponível"):
        importar(PDF, cliente=ClaudeFalso([erro]))


def test_mesclar_anamnese_soma_sem_repetir():
    assert mesclar_anamnese({"alergias": ["Camarão"], "observacoes": "x"}, {"alergias": ["camarão", "amendoim"]}) == {
        "alergias": ["Camarão", "amendoim"], "intolerancias": [], "restricoes": [], "nao_gosta": [], "observacoes": "x"}


# ---------------------------------------------------------------------
# Legenda e espera
# ---------------------------------------------------------------------
@pytest.mark.parametrize("texto, nome, telefone", [
    ("Maria Silva 51 99999-0000", "Maria Silva", "5551999990000"),
    ("Plano da maria da silva - (47) 90000-0024", "Maria da Silva", "5547900000024"),
    ("é da Maria Silva, 11 99999-0001", "Maria Silva", "5511999990001"),
    ("Paciente: João Pedro Alves, whatsapp +55 11 91234-5678", "João Pedro Alves", "5511912345678"),
    ("51999990000", None, "5551999990000"),
    ("segue o plano", None, None),
])
def test_ler_legenda(texto, nome, telefone):
    assert ler_legenda(texto) == (nome, telefone)


def test_pendentes_expiram():
    agora = [0.0]
    p = Pendentes(agora=lambda: agora[0])
    p.guardar_arquivo("n1", PDF, "application/pdf")
    assert p.tem_arquivo("n1")
    agora[0] = 31 * 60
    assert not p.tem_arquivo("n1") and p.pegar_arquivo("n1") is None
    p.guardar_paciente("n1", "Maria Silva", "5551999990000")
    assert p.pegar_paciente("n1") == ("Maria Silva", "5551999990000") and p.pegar_paciente("n1") is None


# ---------------------------------------------------------------------
# Painel: upload
# ---------------------------------------------------------------------
class RepoAreaComRascunho(RepoAreaFalso):
    def __init__(self):
        super().__init__()
        self.rascunhos = []

    def salvar_rascunho(self, nutri_id, pid, conteudo, origem, avisos):
        self.rascunhos.append((nutri_id, pid, origem, avisos))
        self.dados[nutri_id][pid].update(rascunho=conteudo, rascunho_avisos=avisos, rascunho_origem=origem,
                                         rascunho_em=__import__("datetime").datetime(2026, 9, 30, 10, 0))
        return True

    def descartar_rascunho(self, nutri_id, pid):
        return self.dados[nutri_id][pid].pop("rascunho", None) is not None

    def paciente_por_telefone(self, nutri_id, telefone):
        from whatsapp.repositorio import variantes_telefone
        return next(({"id": p["id"], "nome": p["nome"], "ativo": p["ativo"]} for p in self.dados.get(nutri_id, {}).values()
                     if p["telefone"] in variantes_telefone(telefone)), None)


def _imp():
    return importar(PDF, cliente=_claude())


def app_importacao(area, importar_fn=None, cliente=None, repo=None):
    cliente = cliente or ClienteFalso()
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, painel_segredo=SEG_PAINEL,
                           url_publica="https://app.exemplo"),
                    cliente=cliente, repo=repo or RepoFalso(), taco=object(), metricas=RepoMetricasFalso(), area=area,
                    importar_fn=importar_fn or (lambda dados, mime: _imp()))
    return TestClient(app), cliente


def test_upload_vira_rascunho_e_nao_ativa():
    area = RepoAreaComRascunho()
    c, _ = app_importacao(area)
    r = c.post(f"{BASE}/pacientes/{P1}/plano/importar", files={"arquivo": ("plano.pdf", PDF, "application/pdf")},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("aviso=importado")
    (n, pid, origem, avisos), = area.rascunhos
    assert (n, pid, origem) == ("n1", P1, "pdf") and area.salvos == []            # nada ativado
    assert any("sem gramas" in a for a in avisos) and any("anamnese" in a for a in avisos)
    assert "camarão" in area.dados["n1"][P1]["anamnese"]["alergias"] and "amendoim" in area.dados["n1"][P1]["anamnese"]["alergias"]
    pagina = c.get(r.headers["location"]).text
    assert "Rascunho lido do arquivo" in pagina and "NÃO está ativo" in pagina and "texto cortado no rodapé" in pagina
    assert "# Café da manhã 07:00" in pagina and "Descartar rascunho" in pagina
    # conflito com a anamnese aparece na conferência? (camarão não está no plano: não)
    assert "conflita com a anamnese" not in pagina


def test_upload_com_erro_mostra_mensagem():
    def falha(dados, mime):
        raise ErroImportacao("formato não suportado: mande o plano em PDF ou foto")
    area = RepoAreaComRascunho()
    c, _ = app_importacao(area, importar_fn=falha)
    r = c.post(f"{BASE}/pacientes/{P1}/plano/importar", files={"arquivo": ("plano.docx", b"PK", "application/msword")})
    assert r.status_code == 400 and "formato não suportado" in r.text and area.rascunhos == []


def test_salvar_e_descartar_rascunho():
    area = RepoAreaComRascunho()
    c, _ = app_importacao(area)
    c.post(f"{BASE}/pacientes/{P1}/plano/importar", files={"arquivo": ("p.pdf", PDF, "application/pdf")})
    r = c.post(f"{BASE}/pacientes/{P1}/plano/descartar", content="plano=x", follow_redirects=False,
               headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 303 and "rascunho" not in area.dados["n1"][P1]


# ---------------------------------------------------------------------
# WhatsApp da nutri
# ---------------------------------------------------------------------
class UazapiComMidia(ClienteFalso):
    def __init__(self, dados=PDF):
        super().__init__()
        self.dados, self.baixados = dados, []

    def baixar_midia(self, message_id):
        self.baixados.append(message_id)
        return self.dados, "application/pdf"


def _doc(legenda="", mid="D1"):
    return evento(legenda, messageid=mid, messageType="DocumentMessage", sender_pn="5511988880000@s.whatsapp.net",
                  chatid="5511988880000@s.whatsapp.net")


def _texto_da_nutri(texto, mid):
    return evento(texto, messageid=mid, sender_pn="5511988880000@s.whatsapp.net", chatid="5511988880000@s.whatsapp.net")


def test_pdf_com_legenda_cadastra_paciente_e_cria_rascunho():
    area = RepoAreaComRascunho()
    c, uaz = app_importacao(area, cliente=UazapiComMidia(), repo=RepoFalso(cadastrado=False))
    c.post(URL, json=_doc("Joana Pereira 51 98888-1111"))
    novo = area.paciente_por_telefone("n1", "5551988881111")
    assert novo and novo["nome"] == "Joana Pereira"
    (_, pid, origem, avisos), = area.rascunhos
    assert pid == novo["id"] and origem == "whatsapp"
    lendo, resumo = [t for n, t in uaz.enviadas]
    assert "Lendo o plano" in lendo
    assert "Plano de *Joana Pereira* lido: 2 refeição(ões), 3 itens" in resumo and "Cadastro criado agora" in resumo
    assert "não está ativo" in resumo and f"https://app.exemplo/painel/n1." in resumo and f"/pacientes/{pid}" in resumo


def test_pdf_sem_legenda_pergunta_o_paciente_e_conclui_com_a_resposta():
    area = RepoAreaComRascunho()
    c, uaz = app_importacao(area, cliente=UazapiComMidia(), repo=RepoFalso(cadastrado=False))
    c.post(URL, json=_doc(""))
    assert "De qual paciente é?" in uaz.enviadas[-1][1] and area.rascunhos == []
    c.post(URL, json=_texto_da_nutri("é da Maria Silva, 11 99999-0001", "T1"))
    (_, pid, _, avisos), = area.rascunhos
    assert pid == P1                                               # paciente que já existia
    assert any("diferente do cadastro" in a for a in avisos)      # PDF diz Joana, cadastro diz Maria


def test_telefone_antes_do_pdf_tambem_funciona():
    area = RepoAreaComRascunho()
    c, uaz = app_importacao(area, cliente=UazapiComMidia(), repo=RepoFalso(cadastrado=False))
    c.post(URL, json=_texto_da_nutri("Maria Silva 11 99999-0001", "T0"))
    assert uaz.enviadas == []                                      # sozinho, o telefone não dispara nada
    c.post(URL, json=_doc("", mid="D2"))
    assert len(area.rascunhos) == 1 and area.rascunhos[0][1] == P1


def test_arquivo_de_paciente_nao_e_importado():
    area = RepoAreaComRascunho()
    uaz = UazapiComMidia()
    c, _ = app_importacao(area, cliente=uaz)
    c.post(URL, json=evento("", messageid="X1", messageType="DocumentMessage"))  # vem do paciente, não da nutri
    assert uaz.baixados == [] and area.rascunhos == []


def test_falha_na_leitura_avisa_a_nutri():
    def falha(dados, mime):
        raise ErroImportacao("o arquivo não parece ser um plano alimentar")
    area = RepoAreaComRascunho()
    c, uaz = app_importacao(area, importar_fn=falha, cliente=UazapiComMidia(), repo=RepoFalso(cadastrado=False))
    c.post(URL, json=_doc("Joana Pereira 51 98888-1111"))
    assert "não parece ser um plano" in uaz.enviadas[-1][1] and area.rascunhos == []
    assert area.paciente_por_telefone("n1", "5551988881111") is None   # não cadastra à toa
