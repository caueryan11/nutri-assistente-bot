"""Anamnese do paciente: o que o bot nunca pode sugerir.

Formato (pacientes.anamnese, jsonb; a nutri preenche):
    {"alergias": ["amendoim", "camarão"], "intolerancias": ["lactose"],
     "nao_gosta": ["beterraba"], "restricoes": ["vegetariano"], "observacoes": "texto livre"}

Três camadas:
  1. o prompt traz a anamnese e as regras (montar_system);
  2. itens do plano que conflitam são apontados ao modelo (conflitos_no_plano);
  3. o código confere a resposta final: alimento sugerido em conflito é recusado, e a menção a um
     alergênico precisa ser confirmada pelo modelo (bot/assistente.py).

TERMOS_IMPLICITOS é um rascunho clínico: a Ray revisa (ex.: o que conta como "glúten").
"""

from __future__ import annotations

import re
import unicodedata

CATEGORIAS = {
    "alergias": "alergia",
    "intolerancias": "intolerância",
    "restricoes": "restrição alimentar",
    "nao_gosta": "não gosta",
}

# Termo da anamnese -> alimentos que ele exclui (além do próprio termo). Normalizados na hora de usar.
TERMOS_IMPLICITOS = {
    "lactose": ["leite", "queijo", "iogurte", "requeijao", "creme de leite", "nata", "manteiga", "doce de leite",
                "leite condensado", "coalhada", "ricota", "mucarela", "mussarela", "whey"],
    "leite": ["queijo", "iogurte", "requeijao", "creme de leite", "nata", "manteiga", "doce de leite",
              "leite condensado", "coalhada", "ricota", "mucarela", "mussarela", "whey"],
    "gluten": ["trigo", "farinha de trigo", "pao", "macarrao", "biscoito", "bolacha", "bolo", "cevada", "centeio",
               "cuscuz marroquino", "torrada", "pizza", "aveia"],
    "frutos do mar": ["camarao", "lula", "polvo", "marisco", "ostra", "lagosta", "caranguejo", "siri", "mexilhao"],
    "crustaceos": ["camarao", "lagosta", "caranguejo", "siri"],
    "ovo": ["omelete", "clara", "gema", "maionese"],
    "amendoim": ["pasta de amendoim", "pacoca"],
    "oleaginosas": ["castanha", "nozes", "amendoa", "avela", "pistache", "macadamia", "castanha de caju",
                    "castanha do para"],
    "soja": ["tofu", "proteina de soja", "shoyu", "edamame"],
    "peixe": ["atum", "sardinha", "salmao", "tilapia", "bacalhau", "merluza", "pescada"],
    "vegetariano": ["carne", "frango", "peixe", "patinho", "alcatra", "file", "bacon", "presunto", "peito de peru",
                    "linguica", "salsicha", "atum", "sardinha", "salmao", "tilapia", "camarao", "costela", "picanha",
                    "carne moida", "bife", "hamburguer"],
    "vegano": ["carne", "frango", "peixe", "patinho", "bacon", "presunto", "peito de peru", "atum", "sardinha",
               "salmao", "camarao", "ovo", "omelete", "leite", "queijo", "iogurte", "requeijao", "manteiga", "mel",
               "whey", "bife", "hamburguer"],
}

# "leite sem lactose" não conflita com intolerância à lactose.
EXCECOES = {
    "lactose": ["sem lactose", "zero lactose", "lactose free", "leite vegetal", "leite de coco", "leite de amendoa",
                "leite de aveia", "leite de soja", "leite de arroz", "leite de castanha"],
    "gluten": ["sem gluten", "gluten free"],
    "leite": ["leite vegetal", "leite de coco", "leite de amendoa", "leite de aveia", "leite de soja", "leite de arroz"],
}


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", t)).strip()


def limpar(anamnese: dict | None) -> dict:
    """Só as chaves conhecidas, listas de texto sem vazios."""
    a = anamnese if isinstance(anamnese, dict) else {}

    def lista(v) -> list[str]:
        v = [v] if isinstance(v, str) else (v if isinstance(v, list) else [])
        return [str(x).strip() for x in v if str(x).strip()]

    limpa = {k: lista(a.get(k)) for k in CATEGORIAS}
    obs = str(a.get("observacoes") or "").strip()
    return {**limpa, "observacoes": obs} if obs else limpa


def tem_dados(anamnese: dict | None) -> bool:
    a = limpar(anamnese)
    return any(a[k] for k in CATEGORIAS) or bool(a.get("observacoes"))


def _padrao(termo: str) -> re.Pattern:
    """Palavra inteira, aceitando plural simples (amendoins, camaroes)."""
    base = re.escape(termo)
    variantes = [base + "(?:s|es)?"]
    if termo.endswith("ao"):
        variantes.append(re.escape(termo[:-2]) + "oes")
    if termo.endswith("m"):
        variantes.append(re.escape(termo[:-1]) + "ns")
    return re.compile(r"\b(?:" + "|".join(variantes) + r")\b")


def _termos(item: str) -> list[str]:
    n = normalizar(item)
    return [n, *TERMOS_IMPLICITOS.get(n, [])] if n else []


def conflitos(alimentos: list[str], anamnese: dict | None, categorias=tuple(CATEGORIAS)) -> list[dict]:
    """[{alimento, item, categoria}] para cada alimento que bate com algum item da anamnese."""
    a = limpar(anamnese)
    achados = []
    for alimento in alimentos or []:
        alvo = normalizar(alimento)
        if not alvo:
            continue
        for cat in categorias:
            for item in a.get(cat, []):
                chave = normalizar(item)
                if any(normalizar(e) in alvo for e in EXCECOES.get(chave, [])):
                    continue
                if any(_padrao(t).search(alvo) for t in _termos(item)):
                    achados.append({"alimento": alimento, "item": item, "categoria": CATEGORIAS[cat]})
                    break
            else:
                continue
            break
    return achados


def mencoes_de_risco(texto: str, anamnese: dict | None) -> list[str]:
    """Itens de alergia/intolerância citados no texto (para o modelo confirmar que não está sugerindo)."""
    alvo = normalizar(texto)
    a = limpar(anamnese)
    return [item for cat in ("alergias", "intolerancias") for item in a.get(cat, [])
            if any(_padrao(t).search(alvo) for t in _termos(item))]


def itens_do_plano(plano: dict) -> list[str]:
    """Nomes dos alimentos do plano (itens e substituições), em qualquer nível do JSON."""
    nomes: list[str] = []

    def andar(no):
        if isinstance(no, dict):
            for k in ("alimento", "nome_alimento", "item"):
                if isinstance(no.get(k), str):
                    nomes.append(no[k])
            for v in no.values():
                andar(v)
        elif isinstance(no, list):
            for v in no:
                if isinstance(v, str):
                    nomes.append(v)
                else:
                    andar(v)

    for ref in plano.get("refeicoes", []) if isinstance(plano, dict) else []:
        andar(ref)
    return nomes


def conflitos_no_plano(plano: dict, anamnese: dict | None) -> list[dict]:
    vistos, saida = set(), []
    for c in conflitos(itens_do_plano(plano), anamnese):
        if c["alimento"] not in vistos:
            vistos.add(c["alimento"])
            saida.append(c)
    return saida


def texto_para_prompt(anamnese: dict | None, plano: dict, primeiro_nome: str) -> str:
    a = limpar(anamnese)
    if not tem_dados(a):
        return (f"## Anamnese de {primeiro_nome}\n\nNão cadastrada. Não suponha alergias nem restrições; "
                "se o paciente contar uma alergia, intolerância ou restrição nova, não sugira o alimento e "
                "encaminhe para a nutricionista atualizar a anamnese.")
    linhas = [f"## Anamnese de {primeiro_nome} (obrigatório respeitar)", ""]
    rotulos = {"alergias": "Alergias", "intolerancias": "Intolerâncias", "restricoes": "Restrições alimentares",
               "nao_gosta": "Não gosta de"}
    for k, rotulo in rotulos.items():
        if a[k]:
            linhas.append(f"- {rotulo}: {', '.join(a[k])}")
    if a.get("observacoes"):
        linhas.append(f"- Observações da nutricionista: {a['observacoes']}")
    linhas += [
        "",
        "Regras da anamnese:",
        "- Nunca sugira, como troca ou opção, alimento ou preparação que contenha algo das alergias, intolerâncias "
        "ou restrições, nem algo de que o paciente não gosta. Isso vale também para itens e substituições do plano.",
        "- Alergia, intolerância e restrição são questão de saúde: fale com clareza que não é indicado. \"Não gosta\" é "
        "preferência, não restrição: nunca sugira, mas se o próprio paciente pedir, pode liberar se o plano permitir, "
        "sem dizer que \"não é indicado\".",
        "- Liste em `alimentos_sugeridos` todo alimento que a sua mensagem sugere ou libera; o sistema confere com a anamnese.",
        "- Se o paciente perguntar se pode comer algo da lista de alergias ou intolerâncias, diga com clareza que não é "
        "indicado para ele e ofereça uma alternativa segura do plano; se não houver, encaminhe.",
        "- Alergia, intolerância ou restrição nova contada pelo paciente: não sugira o alimento e encaminhe para a "
        "nutricionista atualizar a anamnese.",
    ]
    no_plano = conflitos_no_plano(plano, a)
    if no_plano:
        itens = "; ".join(f"{c['alimento']} ({c['categoria']}: {c['item']})" for c in no_plano)
        linhas.append(f"- ATENÇÃO: o plano traz itens que conflitam com a anamnese: {itens}. Não sugira esses itens; "
                      "se o paciente perguntar por eles, encaminhe para a nutricionista revisar o plano.")
    return "\n".join(linhas)
