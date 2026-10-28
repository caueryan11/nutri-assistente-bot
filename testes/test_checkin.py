"""Check-in semanal: leitura (Claude simulado), decisão, envio, webhook e painel."""

from datetime import date, datetime
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from bot.assistente import Resultado
from bot.checkin import Leitura, decidir, interpretar, rodar_checkins, texto_pergunta, validar_config
from testes.test_area_nutri import BASE, P1, RepoAreaFalso
from testes.test_metricas import SEG_PAINEL, RepoMetricasFalso
from testes.test_unidade import ClienteFalso as ClaudeFalso, _chamada, _resposta
from testes.test_whatsapp import SEGREDO, TOKEN, URL, ClienteFalso, RepoFalso, evento
from whatsapp.app import Config, criar_app

PENDENTE = {"id": "ck1", "semana": date(2026, 10, 4), "adesao": None, "fome": None, "intestino": None,
            "peso_kg": None, "comentario": None, "alerta": False, "lembrete_enviado": False}


def _d(campos, *, pendente=PENDENTE, peso_anterior=None, alerta=False, motivo="", extra=""):
    return decidir(Leitura(True, campos, alerta, motivo, extra), dict(pendente), peso_anterior, "Maria", "Ana Souza",
                   "5511999990001", "Maria Silva")


# ---------------------------------------------------------------------
# Decisão
# ---------------------------------------------------------------------
def test_resposta_completa_agradece_e_anima():
    d = _d({"adesao": 9, "fome": "normal", "intestino": "normal", "peso_kg": 71.5})
    assert d.salvar["status"] == "respondido" and d.salvar["adesao"] == 9 and d.salvar["peso_kg"] == 71.5
    assert "Anotei tudo" in d.mensagem and "continue assim" in d.mensagem and d.aviso_nutri is None


def test_adesao_baixa_acolhe_sem_alerta():
    d = _d({"adesao": 3, "fome": "muita", "intestino": "preso"})
    assert "Semanas assim acontecem" in d.mensagem and d.aviso_nutri is None and d.salvar["alerta"] is False


def test_falta_algo_pede_uma_vez_e_depois_fecha():
    d = _d({"adesao": 7})
    assert d.salvar["status"] == "enviado" and d.salvar["lembrete_enviado"] is True
    assert "como esteve a fome e como está o intestino" in d.mensagem
    # segunda resposta ainda incompleta: fecha mesmo assim, sem insistir
    d2 = _d({"fome": "normal"}, pendente={**PENDENTE, "adesao": 7, "lembrete_enviado": True})
    assert d2.salvar["status"] == "respondido" and d2.salvar["adesao"] == 7 and d2.salvar["fome"] == "normal"


def test_sintoma_alerta_a_nutri_e_orienta_192():
    d = _d({"adesao": 6, "fome": "pouca", "intestino": "solto"}, alerta=True, motivo="tontura e vômito")
    assert d.salvar["alerta"] is True and "tontura" in d.salvar["alerta_motivo"]
    assert "192" in d.mensagem and "Maria Silva" in d.aviso_nutri and "tontura" in d.aviso_nutri.lower()
    assert "adesão 6/10" in d.aviso_nutri


def test_variacao_de_peso_alerta():
    d = _d({"adesao": 8, "fome": "normal", "intestino": "normal", "peso_kg": 68.0}, peso_anterior=71.0)
    assert d.aviso_nutri and "-3,0 kg" in d.aviso_nutri
    assert _d({"adesao": 8, "fome": "normal", "intestino": "normal", "peso_kg": 70.0}, peso_anterior=71.0).aviso_nutri is None


def test_pergunta_extra_segue_para_o_bot():
    d = _d({"adesao": 8, "fome": "normal", "intestino": "normal"}, extra="posso trocar o arroz?")
    assert d.seguir_para_bot == "posso trocar o arroz?" and d.salvar["status"] == "respondido"


def test_validar_config():
    assert validar_config("1", "9:30") == ({"dia": 1, "hora": "09:30"}, None)
    assert validar_config("8", "10:00")[1] and validar_config("7", "22:00")[1] and validar_config("x", "10:00")[1]


def test_pergunta_do_checkin():
    t = texto_pergunta("Maria", "Ana Souza")
    assert "0 a 10" in t and "fome" in t and "intestino" in t and "peso" in t and "Ana Souza" in t


# ---------------------------------------------------------------------
# Leitura
# ---------------------------------------------------------------------
def test_interpretar_limpa_valores_invalidos():
    claude = ClaudeFalso([_resposta(_chamada("registrar_checkin", {
        "eh_resposta_checkin": True, "adesao": 12, "fome": "enorme", "intestino": "preso", "peso_kg": 72.46,
        "comentario": "semana corrida", "alerta": False, "alerta_motivo": "", "pergunta_extra": ""}))])
    l = interpretar("uns 12, fome enorme, intestino preso, 72,46kg", cliente=claude)
    assert l.eh_resposta and l.campos == {"intestino": "preso", "peso_kg": 72.5, "comentario": "semana corrida"}
    assert claude.pedidos[0]["tool_choice"] == {"type": "tool", "name": "registrar_checkin"}


# ---------------------------------------------------------------------
# Envio semanal
# ---------------------------------------------------------------------
class RepoRotinaCheckin:
    def __init__(self):
        self.proativas, self.desfeitos = [], []

    def reservar_checkins(self):
        return [{"checkin_id": "c1", "paciente_id": "p1", "nutri_id": "n1", "primeiro_nome": "Maria",
                 "telefone": "5511999990001", "nutri_nome": "Ana Souza"},
                {"checkin_id": "c2", "paciente_id": "p2", "nutri_id": "n1", "primeiro_nome": "João",
                 "telefone": "falha", "nutri_nome": "Ana Souza"}]

    def desfazer_checkin(self, cid):
        self.desfeitos.append(cid)

    def registrar_proativa(self, nutri_id, paciente_id, texto, motivo, wa_id):
        self.proativas.append((paciente_id, motivo, wa_id))


def test_rodar_checkins_envia_e_desfaz_falha():
    repo, enviadas = RepoRotinaCheckin(), []

    def enviar(numero, texto):
        if numero == "falha":
            raise RuntimeError("uazapi fora")
        enviadas.append((numero, texto))
        return "wa1"

    assert rodar_checkins(repo, enviar) == 1
    assert enviadas[0][0] == "5511999990001" and "check-in da semana" in enviadas[0][1]
    assert repo.proativas == [("p1", "check-in semanal", "wa1")] and repo.desfeitos == ["c2"]


# ---------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------
class RepoComCheckin(RepoFalso):
    def __init__(self, pendente=True, **kw):
        super().__init__(**kw)
        self.pendente = dict(PENDENTE) if pendente else None
        self.salvos, self.saidas_completas = [], []

    def checkin_pendente(self, paciente_id):
        return self.pendente if self.pendente and self.pendente.get("status", "enviado") == "enviado" else None

    def salvar_checkin(self, cid, campos):
        self.salvos.append((cid, campos))
        self.pendente.update(campos)

    def peso_anterior(self, paciente_id, antes_de):
        return 71.0

    def registrar_saida(self, nutri, paciente, r, wa_id):
        super().registrar_saida(nutri, paciente, r, wa_id)
        self.saidas_completas.append(r)


def app_checkin(repo, leitura, respostas_bot=None):
    cliente, chamadas = ClienteFalso(), []

    def responder_falso(pergunta, *a, **k):
        chamadas.append(pergunta)
        return Resultado(acao="responder", mensagem_paciente="Pode trocar por batata-doce.")

    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO), cliente=cliente, repo=repo,
                    taco=object(), responder_fn=responder_falso,
                    checkin_fn=lambda texto: leitura if not callable(leitura) else leitura(texto))
    return TestClient(app), cliente, chamadas


def test_resposta_do_checkin_nao_passa_pelo_bot():
    repo = RepoComCheckin()
    c, cliente, chamadas = app_checkin(repo, Leitura(True, {"adesao": 8, "fome": "normal", "intestino": "normal", "peso_kg": 70.5}))
    c.post(URL, json=evento("8, fome normal, intestino ok, 70,5"))
    assert chamadas == [] and repo.salvos[0][1]["status"] == "respondido"
    assert cliente.enviadas == [("5511999990001", cliente.enviadas[0][1])] and "Anotei tudo" in cliente.enviadas[0][1]
    r = repo.saidas_completas[0]
    assert r.acao is None and r.motivo == "check-in semanal"          # não conta como "dúvida respondida"


def test_duvida_durante_checkin_vai_para_o_bot():
    repo = RepoComCheckin()
    c, cliente, chamadas = app_checkin(repo, Leitura(False))
    c.post(URL, json=evento("Posso trocar o arroz por batata?"))
    assert chamadas == ["Posso trocar o arroz por batata?"] and repo.salvos == []


def test_resposta_e_duvida_juntas():
    repo = RepoComCheckin()
    c, cliente, chamadas = app_checkin(repo, Leitura(True, {"adesao": 9, "fome": "normal", "intestino": "normal"},
                                                     pergunta_extra="posso trocar o arroz?"))
    c.post(URL, json=evento("9, tudo normal. Posso trocar o arroz?"))
    assert repo.salvos and chamadas == ["posso trocar o arroz?"]
    assert "Anotei tudo" in cliente.enviadas[0][1] and cliente.enviadas[1][1] == "Pode trocar por batata-doce."


def test_alerta_do_checkin_avisa_a_nutri():
    repo = RepoComCheckin()
    c, cliente, _ = app_checkin(repo, Leitura(True, {"adesao": 5, "fome": "pouca", "intestino": "solto"}, True, "tontura"))
    c.post(URL, json=evento("5, sem fome, intestino solto, tive tontura"))
    (pac, t1), (nutri, t2) = cliente.enviadas
    assert "192" in t1 and nutri == "5511988880000" and "tontura" in t2.lower()
    assert repo.saidas_completas[0].acao == "encaminhar"


def test_sem_checkin_pendente_nao_chama_a_leitura():
    repo = RepoComCheckin(pendente=False)
    lidas = []
    c, _, chamadas = app_checkin(repo, lambda texto: lidas.append(texto) or Leitura(True))
    c.post(URL, json=evento("oi"))
    assert lidas == [] and chamadas == ["oi"]


def test_leitura_fora_do_ar_deixa_o_bot_responder():
    def falha(texto):
        raise RuntimeError("api")
    repo = RepoComCheckin()
    c, _, chamadas = app_checkin(repo, falha)
    c.post(URL, json=evento("8, tudo certo"))
    assert chamadas == ["8, tudo certo"]


# ---------------------------------------------------------------------
# Painel
# ---------------------------------------------------------------------
class RepoAreaCheckin(RepoAreaFalso):
    def __init__(self):
        super().__init__()
        self.dados["n1"][P1]["acompanhamento_desde"] = None
        self.dados["n1"][P1]["acompanhamento_ate"] = None
        self.consultorios["n1"]["acompanhamento_dias"] = 45
        self.checkins = [
            {"semana": date(2026, 9, 20), "status": "respondido", "adesao": 7, "fome": "normal", "intestino": "normal",
             "peso_kg": 72.0, "comentario": None, "alerta": False, "alerta_motivo": None},
            {"semana": date(2026, 9, 27), "status": "respondido", "adesao": 9, "fome": "pouca", "intestino": "preso",
             "peso_kg": 69.5, "comentario": "semana de viagem", "alerta": True, "alerta_motivo": "variação de peso"},
            {"semana": date(2026, 10, 4), "status": "enviado", "adesao": None, "fome": None, "intestino": None,
             "peso_kg": None, "comentario": None, "alerta": False, "alerta_motivo": None},
        ]

    def definir_acompanhamento(self, nutri_id, pid, modo, dias=None):
        self.modos = getattr(self, "modos", []) + [(modo, dias)]
        d = self.dados[nutri_id][pid]
        if modo == "desligar":
            d["acompanhamento_desde"] = d["acompanhamento_ate"] = None
        else:
            d["acompanhamento_desde"] = date(2026, 9, 30)
            d["acompanhamento_ate"] = date(2026, 11, 14) if modo == "validade" else None
        return True

    def evolucao(self, nutri_id, pid, limite=12):
        return self.checkins if pid == P1 else []


def _painel(area):
    app = criar_app(Config("bot", "https://exemplo.uazapi.com", TOKEN, SEGREDO, painel_segredo=SEG_PAINEL,
                           url_publica="https://app.exemplo"),
                    cliente=ClienteFalso(), repo=RepoFalso(), taco=object(), metricas=RepoMetricasFalso(), area=area)
    return TestClient(app)


def _post(c, caminho, dados):
    return c.post(BASE + caminho, content=urlencode(dados), follow_redirects=False,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})


def test_ligar_acompanhamento_e_ver_evolucao():
    area = RepoAreaCheckin()
    c = _painel(area)
    assert "Ligar por 45 dias" in c.get(f"{BASE}/pacientes/{P1}").text
    r = _post(c, f"/pacientes/{P1}/acompanhamento", {"ligar": "1"})
    assert r.status_code == 303 and area.dados["n1"][P1]["acompanhamento_desde"] == date(2026, 9, 30)
    pagina = c.get(r.headers["location"]).text
    assert "Acompanhamento ligado" in pagina and "Ativo desde 30/09/2026" in pagina
    assert "69,5 kg" in pagina and "Atenção:" in pagina and "variação de peso" in pagina and "sem resposta" in pagina
    assert '"adesao": [7, 9]' in pagina and '"peso": [72.0, 69.5]' in pagina
    _post(c, f"/pacientes/{P1}/acompanhamento", {"ligar": "0"})
    assert area.dados["n1"][P1]["acompanhamento_desde"] is None


def test_consultorio_salva_dia_e_hora_do_checkin():
    area = RepoAreaCheckin()
    c = _painel(area)
    assert 'value="18:00"' in c.get(BASE + "/consultorio").text
    r = _post(c, "/consultorio", {"checkin_dia": "1", "checkin_hora": "08:30", "duracao_min": "60",
                                  "antecedencia_horas": "12", "janela_dias": "14"})
    assert r.status_code == 303 and area.consultorios["n1"]["checkin_config"] == {"dia": 1, "hora": "08:30"}
    r = _post(c, "/consultorio", {"checkin_dia": "1", "checkin_hora": "23:00", "duracao_min": "60",
                                  "antecedencia_horas": "12", "janela_dias": "14"})
    assert r.status_code == 400 and "20:59" in r.text
