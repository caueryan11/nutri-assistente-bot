"""Testes sem internet e sem banco. Rodar: python -m pytest"""

import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

from bot.assistente import MAX_PASSOS, SemConsentimento, responder
from bot.taco import TacoJSON, calcular_porcao, consultar_taco
from scripts.importar_taco import ler_taco

RAIZ = Path(__file__).resolve().parents[1]
PACIENTE = {"primeiro_nome": "Maria", "consentimento_em": "2026-09-01T10:00:00-03:00"}
PLANO = json.loads((RAIZ / "dados" / "plano_exemplo.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def taco(tmp_path_factory) -> TacoJSON:
    """TACO real gerada da planilha oficial (o taco.json não vai para o git)."""
    alimentos = ler_taco(RAIZ / "dados" / "taco_4ed_nepa.xlsx")
    caminho = tmp_path_factory.mktemp("taco") / "taco.json"
    caminho.write_text(json.dumps(
        [{k: (float(v) if hasattr(v, "is_finite") else v) for k, v in a.items()} for a in alimentos],
        ensure_ascii=False), encoding="utf-8")
    return TacoJSON(caminho)


# ---------------------------------------------------------------------
# TACO
# ---------------------------------------------------------------------
@pytest.mark.parametrize("termo, id_esperado", [
    ("arroz integral cozido", 1),
    ("ovo cozido", 488),
    ("pao frances", 53),       # sem acento; não pode vir "Torrada, pão francês"
    ("peito de frango grelhado", 410),
    ("ACUCAR", 492),
    ("feijao carioca cozido", 561),
])
def test_busca_acha_o_alimento_certo(taco, termo, id_esperado):
    assert taco.buscar(termo, 1)[0]["id"] == id_esperado


def test_busca_tolera_erro_de_digitacao(taco):
    assert taco.buscar("arros", 1)[0]["nome"].lower().startswith("arroz")
    assert "banana" in taco.buscar("bananna", 1)[0]["nome"].lower()


def test_busca_sem_resultado(taco):
    assert taco.buscar("xyzqwk") == []
    assert taco.buscar("a") == []


def test_porcao_escala_e_arredonda(taco):
    arroz = taco.buscar("arroz integral cozido", 1)[0]
    v = calcular_porcao(arroz, 150)
    assert v["energia"] == "185 kcal"          # 123,53 kcal/100 g x 1,5
    assert v["proteína"] == "3,9 g"


def test_marcacao_nunca_vira_zero(taco):
    frango = taco.buscar("peito de frango grelhado", 1)[0]
    assert calcular_porcao(frango, 100)["fibra alimentar"] == "não se aplica a este alimento"


def test_consultar_taco_valida_entrada(taco):
    assert consultar_taco(taco, "", 100)["encontrado"] is False
    assert consultar_taco(taco, "arroz", -5)["encontrado"] is False
    assert consultar_taco(taco, "arroz", "muito")["encontrado"] is False
    assert consultar_taco(taco, "xyzqwk", 100)["encontrado"] is False
    ok = consultar_taco(taco, "banana prata", 70)
    assert ok["encontrado"] and ok["alimento_taco"] == "Banana, prata, crua"


# ---------------------------------------------------------------------
# Laço do bot com a API simulada
# ---------------------------------------------------------------------
def _uso(ent=100, sai=20):
    return SimpleNamespace(input_tokens=ent, output_tokens=sai, cache_read_input_tokens=0)


def _chamada(nome, entrada, id_="t1"):
    return SimpleNamespace(type="tool_use", id=id_, name=nome, input=entrada)


def _resposta(*blocos, stop="tool_use"):
    return SimpleNamespace(content=list(blocos), usage=_uso(), stop_reason=stop)


class ClienteFalso:
    """Devolve as respostas na ordem; guarda o que recebeu."""

    def __init__(self, respostas):
        self._respostas = list(respostas)
        self.pedidos = []
        self.messages = self

    def create(self, **kwargs):
        self.pedidos.append({**kwargs, "messages": list(kwargs["messages"])})  # cópia: o bot segue anexando
        item = self._respostas.pop(0) if self._respostas else self._ultima
        self._ultima = item
        if isinstance(item, Exception):
            raise item
        return item


FINAL_OK = {"acao": "responder", "mensagem_paciente": "Pode sim, Maria!", "motivo": "", "resumo_para_nutri": ""}


def test_consulta_taco_e_responde(taco):
    cliente = ClienteFalso([
        _resposta(_chamada("consultar_taco", {"alimento": "banana prata", "gramas": 70})),
        _resposta(_chamada("enviar_resposta", FINAL_OK, "t2")),
    ])
    r = responder("Quantas calorias tem a banana?", PACIENTE, PLANO, taco=taco, cliente=cliente)
    assert r.acao == "responder" and not r.fallback
    assert r.passos == 2 and len(r.consultas_taco) == 1
    # o resultado da TACO voltou para o modelo no 2º pedido
    ultimo = cliente.pedidos[1]["messages"][-1]["content"][0]
    assert ultimo["type"] == "tool_result" and "Banana, prata, crua" in ultimo["content"]
    # regras e plano no system, com cache no bloco do plano
    system = cliente.pedidos[0]["system"]
    assert "SAMU 192" in system[0]["text"] and "Ana Souza" in system[0]["text"]
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_sem_resposta_final_encaminha(taco):
    sempre_taco = _resposta(_chamada("consultar_taco", {"alimento": "arroz", "gramas": 100}))
    cliente = ClienteFalso([sempre_taco] * (MAX_PASSOS + 2))
    r = responder("oi", PACIENTE, PLANO, taco=taco, cliente=cliente)
    assert r.acao == "encaminhar" and r.fallback and r.passos == MAX_PASSOS
    assert "Maria" in r.mensagem_paciente and "Ana Souza" in r.mensagem_paciente


def test_resposta_invalida_pede_correcao(taco):
    ruim = {"acao": "encaminhar", "mensagem_paciente": "A nutri vai te responder.", "motivo": "", "resumo_para_nutri": ""}
    bom = {**ruim, "motivo": "sintoma", "resumo_para_nutri": "Paciente relata enjoo."}
    cliente = ClienteFalso([
        _resposta(_chamada("enviar_resposta", ruim)),
        _resposta(_chamada("enviar_resposta", bom, "t2")),
    ])
    r = responder("Estou enjoada", PACIENTE, PLANO, taco=taco, cliente=cliente)
    assert r.acao == "encaminhar" and not r.fallback and r.motivo == "sintoma"
    erro = cliente.pedidos[1]["messages"][-1]["content"][0]
    assert erro["is_error"] is True


def test_erro_da_api_encaminha(taco):
    erro = anthropic.APIConnectionError(request=None)
    r = responder("oi", PACIENTE, PLANO, taco=taco, cliente=ClienteFalso([erro]))
    assert r.acao == "encaminhar" and r.fallback and "APIConnectionError" in r.motivo


def test_sem_consentimento_nao_responde(taco):
    cliente = ClienteFalso([])
    with pytest.raises(SemConsentimento):
        responder("oi", {"primeiro_nome": "João", "consentimento_em": None}, PLANO, taco=taco, cliente=cliente)
    assert cliente.pedidos == []  # nem chamou a API


def test_valor_pequeno_nao_vira_zero(taco):
    banana = taco.buscar("banana prata", 1)[0]  # 0,065 g de gordura em 100 g
    assert calcular_porcao(banana, 70)["gorduras totais"] == "menos de 0,1 g"


@pytest.mark.parametrize("resposta, mensagem, esperado", [
    ("Pode sim! 😊", "posso trocar?", "Pode sim!"),               # paciente não usou emoji
    ("Pode sim! 😊🎉", "posso trocar? 🙏", "Pode sim! 😊"),        # usou: no máximo um
    ("Oi, Maria! ☀️ Bom dia", "oi", "Oi, Maria! Bom dia"),
])
def test_ajustar_emojis(resposta, mensagem, esperado):
    from bot.assistente import ajustar_emojis
    assert ajustar_emojis(resposta, mensagem) == esperado


def test_patinho_moido_cozido_nao_vira_outro_corte(taco):
    r = consultar_taco(taco, "carne de patinho moída cozida", 100)
    assert r["alimento_taco"] == "Carne, bovina, patinho, sem gordura, grelhado"
    assert r["correspondencia"] == "aproximada" and "patinho" not in r["palavras_nao_encontradas"]
    assert "patinho, sem gordura, grelhado" in r["aviso"]


def test_sinonimo_arroz_branco(taco):
    r = consultar_taco(taco, "arroz branco cozido", 100)
    assert r["alimento_taco"] == "Arroz, tipo 1, cozido" and r["correspondencia"] == "exata"


def test_preparado_prefere_nao_cru(taco):
    assert "cru" not in taco.buscar("patinho cozido", 1)[0]["nome"]


def test_termo_com_simbolo_nao_quebra(taco):
    assert taco.buscar("(x", 3) == [] or isinstance(taco.buscar("(x", 3), list)


def test_formatar_whatsapp():
    from bot.assistente import formatar_whatsapp
    assert formatar_whatsapp("**Total: 318 kcal**\n## Dica\nok") == "*Total: 318 kcal*\nDica\nok"


def test_resumo_do_plano_percentuais(taco):
    from bot.plano import gramas_do_item, resumo_do_plano
    assert gramas_do_item("4 colheres de sopa (100 g)") == 100 and gramas_do_item("1 copo (200 ml)") == 200
    assert gramas_do_item("à vontade") is None
    assert gramas_do_item("150g") == 150 and gramas_do_item("150 gramas") == 150 and gramas_do_item("200 ml") == 200
    assert gramas_do_item("5 colheres de sopa (125g)") == 125 and gramas_do_item("1 porção (1/2 xíc) 80 g") == 80
    assert gramas_do_item("3 ovos") is None and gramas_do_item("2 unidades") is None
    r = resumo_do_plano(PLANO, taco)
    jantar = next(x for x in r["refeicoes"] if x["refeicao"] == "Jantar")
    assert 0 < jantar["percentual_do_dia"]["energia"] < 100
    assert sum(x["energia_kcal"] for x in r["refeicoes"]) in range(r["total_do_dia"]["energia_kcal"] - 3, r["total_do_dia"]["energia_kcal"] + 4)
    cafe = next(x for x in r["refeicoes"] if x["refeicao"] == "Café da manhã")
    assert any("sem gramas" in f for f in cafe["fora_da_conta"])


def test_resumo_do_plano_usa_id_taco(taco):
    from bot.plano import resumo_do_plano
    plano = {"refeicoes": [{"nome": "X", "itens": [{"alimento": "qualquer nome", "id_taco": 1, "quantidade": "(100 g)"}]}]}
    item = resumo_do_plano(plano, taco)["refeicoes"][0]["itens"][0]
    assert item["alimento_taco"] == "Arroz, integral, cozido" and item["correspondencia"] == "exata"


def test_bot_chama_resumo_do_plano(taco):
    final = {"acao": "encaminhar", "mensagem_paciente": "O jantar é 23% do dia.", "motivo": "pular refeição",
             "resumo_para_nutri": "Quer pular o jantar."}
    cliente = ClienteFalso([
        _resposta(_chamada("resumo_do_plano", {})),
        _resposta(_chamada("enviar_resposta", final, "t2")),
    ])
    r = responder("E se eu pular a janta?", PACIENTE, PLANO, taco=taco, cliente=cliente)
    assert r.acao == "encaminhar" and not r.fallback
    resultado = cliente.pedidos[1]["messages"][-1]["content"][0]["content"]
    assert '"refeicao": "Jantar"' in resultado and "total_do_dia" in resultado


def test_system_tem_hora_de_brasilia_mesmo_com_servidor_em_utc():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from bot.assistente import montar_system
    # 03:45 UTC de quinta = 00:45 de quinta em Brasília; 23:30 de quarta em Brasília = 02:30 UTC de quinta
    agora = datetime(2026, 10, 1, 2, 30, tzinfo=timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo"))
    ultimo = montar_system(PACIENTE, PLANO, "Ana", agora=agora)[-1]["text"]
    assert "quarta, 30/09/2026, 23:30" in ultimo
    assert "use a lista pronta" in montar_system(PACIENTE, PLANO, "Ana")[0]["text"]
    assert "Jantar (19:30). Que ainda faltam hoje: nenhuma" in ultimo


def test_refeicoes_do_dia_pelo_horario():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from bot.assistente import refeicoes_do_dia
    t = refeicoes_do_dia(PLANO, datetime(2026, 9, 29, 11, 0, tzinfo=ZoneInfo("America/Sao_Paulo")))
    assert "já passaram hoje: Café da manhã (07:00), Lanche da manhã (10:00)." in t
    assert "Que ainda faltam hoje: Almoço (12:30), Lanche da tarde (16:00), Jantar (19:30)." in t
    assert refeicoes_do_dia({"refeicoes": [{"nome": "X", "horario": ""}]}, datetime(2026, 9, 29, 11, 0)) == ""


def test_refeicoes_do_dia_madrugada():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from bot.assistente import refeicoes_do_dia
    assert "madrugada" in refeicoes_do_dia(PLANO, datetime(2026, 9, 30, 0, 45, tzinfo=ZoneInfo("America/Sao_Paulo")))
