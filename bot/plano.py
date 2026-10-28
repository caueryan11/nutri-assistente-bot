"""Quanto cada refeição do plano representa no dia, com valores da TACO.

Só entram na conta itens com gramas (ou ml) na quantidade, ex.: "4 colheres de sopa (100 g)".
Item com `id_taco` usa aquele alimento exato; senão, busca pelo nome (e marca se foi aproximado).
Valor ausente ("Tr", "NA") conta como zero na soma: é traço ou não se aplica.
"""

from __future__ import annotations

import re

from bot.taco import FonteTaco, melhor_alimento

NUTRIENTES = ("energia_kcal", "proteina_g", "carboidrato_g", "lipideos_g")
_GRAMAS = re.compile(r"\(\s*(\d+(?:[.,]\d+)?)\s*(g|ml)\s*\)", re.I)
# também "150g", "150 g", "150 gramas", "200 ml" soltos (comum em plano digitado ou foto)
_GRAMAS_SOLTAS = re.compile(r"(?<![\d/])(\d+(?:[.,]\d+)?)\s*(g|gr|grama|gramas|ml)\b", re.I)


def gramas_do_item(quantidade: str) -> float | None:
    m = _GRAMAS.search(quantidade or "") or _GRAMAS_SOLTAS.search(quantidade or "")
    return float(m.group(1).replace(",", ".")) if m else None


def _somar(itens: list[dict]) -> dict:
    return {n: sum(i["_valores"][n] for i in itens) for n in NUTRIENTES}


def resumo_do_plano(plano: dict, fonte: FonteTaco) -> dict:
    refeicoes = []
    for ref in plano.get("refeicoes", []):
        calculados, fora = [], []
        for item in ref.get("itens", []):
            nome = item.get("alimento", "")
            gramas = gramas_do_item(item.get("quantidade", ""))
            if not gramas:
                fora.append(f"{nome} (sem gramas no plano)")
                continue
            if item.get("id_taco"):
                alimento, faltando = fonte.obter(item["id_taco"]), []
            else:
                alimento, faltando, _ = melhor_alimento(fonte, nome)
            if alimento is None:
                fora.append(f"{nome} (não encontrado na TACO)")
                continue
            calculados.append({
                "alimento_plano": nome,
                "gramas": gramas,
                "alimento_taco": alimento["nome"],
                "correspondencia": "aproximada" if faltando else "exata",
                "_valores": {n: float(alimento.get(n) or 0) * gramas / 100 for n in NUTRIENTES},
            })
        refeicoes.append({"nome": ref.get("nome", ""), "horario": ref.get("horario"),
                          "_soma": _somar(calculados), "itens": calculados, "fora_da_conta": fora})

    dia = {n: sum(r["_soma"][n] for r in refeicoes) for n in NUTRIENTES}

    def pct(valor: float, total: float) -> int | None:
        return round(100 * valor / total) if total else None

    saida = []
    for r in refeicoes:
        s = r["_soma"]
        saida.append({
            "refeicao": r["nome"],
            "horario": r["horario"],
            "energia_kcal": round(s["energia_kcal"]),
            "proteina_g": round(s["proteina_g"], 1),
            "carboidrato_g": round(s["carboidrato_g"], 1),
            "lipideos_g": round(s["lipideos_g"], 1),
            "percentual_do_dia": {
                "energia": pct(s["energia_kcal"], dia["energia_kcal"]),
                "proteina": pct(s["proteina_g"], dia["proteina_g"]),
                "carboidrato": pct(s["carboidrato_g"], dia["carboidrato_g"]),
            },
            "itens": [{k: v for k, v in i.items() if k != "_valores"} for i in r["itens"]],
            "fora_da_conta": r["fora_da_conta"],
        })
    return {
        "refeicoes": saida,
        "total_do_dia": {"energia_kcal": round(dia["energia_kcal"]), "proteina_g": round(dia["proteina_g"], 1),
                         "carboidrato_g": round(dia["carboidrato_g"], 1), "lipideos_g": round(dia["lipideos_g"], 1)},
        "observacao": ("Soma dos itens do plano com quantidade em gramas, valores da TACO 4ª ed. "
                       "Itens em 'fora_da_conta' não entram. Não é a meta do paciente: o plano não traz metas."),
    }
