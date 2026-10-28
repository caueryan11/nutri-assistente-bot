"""Roda as perguntas-teste no bot com a API real e gera um CSV para a Ray revisar.

Uso:
    python testes/rodar_testes.py                  # todas as perguntas
    python testes/rodar_testes.py --ids 1 8 15     # só algumas

Precisa de ANTHROPIC_API_KEY no .env. Usa a TACO do banco se DATABASE_URL estiver
definida; senão, dados/taco.json (gere com scripts/importar_taco.py).
Custo aproximado: US$ 0,01 por pergunta com o Haiku 4.5.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(RAIZ / ".env", override=True)

from bot.assistente import responder  # noqa: E402
from bot.taco import TacoJSON, TacoPostgres  # noqa: E402

# US$ por milhão de tokens (Haiku 4.5): entrada, saída, leitura de cache
PRECO = {"entrada": 1.00, "saida": 5.00, "cache": 0.10}

PACIENTE_TESTE = {"primeiro_nome": "Maria", "consentimento_em": "2026-09-01T10:00:00-03:00"}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--ids", nargs="*", help="só estas perguntas")
    p.add_argument("--perguntas", type=Path, default=RAIZ / "testes" / "perguntas.csv")
    p.add_argument("--plano", type=Path, default=RAIZ / "dados" / "plano_exemplo.json")
    args = p.parse_args()

    if os.environ.get("DATABASE_URL"):
        taco, origem = TacoPostgres(os.environ["DATABASE_URL"]), "Supabase"
    else:
        caminho = RAIZ / os.environ.get("TACO_JSON", "dados/taco.json")
        if not caminho.exists():
            sys.exit(f"{caminho} não existe. Rode: python scripts/importar_taco.py dados/taco_4ed_nepa.xlsx --json {caminho}")
        taco, origem = TacoJSON(caminho), caminho.name

    plano = json.loads(args.plano.read_text(encoding="utf-8"))
    with args.perguntas.open(encoding="utf-8") as f:
        perguntas = [l for l in csv.DictReader(f) if not args.ids or l["id"] in args.ids]

    print(f"{len(perguntas)} perguntas | modelo {os.environ.get('MODELO_BOT', 'claude-haiku-4-5')} | TACO: {origem}\n")
    linhas, custo_total, acertos = [], 0.0, 0
    for q in perguntas:
        r = responder(q["pergunta"], PACIENTE_TESTE, plano, taco=taco)
        sem_cache = r.tokens_entrada  # input_tokens já exclui o que veio do cache
        custo = (sem_cache * PRECO["entrada"] + r.tokens_saida * PRECO["saida"]
                 + r.tokens_cache_lidos * PRECO["cache"]) / 1e6
        custo_total += custo
        acertou = r.acao == q["esperado"]
        acertos += acertou
        print(f"[{'OK ' if acertou else 'ERR'}] {q['id']:>3} esperado={q['esperado']:<10} obtido={r.acao:<10} "
              f"passos={r.passos} US${custo:.4f}  {q['pergunta'][:50]}")
        linhas.append({
            "id": q["id"], "pergunta": q["pergunta"], "esperado": q["esperado"], "obtido": r.acao,
            "acertou": "sim" if acertou else "NÃO", "mensagem_paciente": r.mensagem_paciente,
            "motivo": r.motivo, "resumo_para_nutri": r.resumo_para_nutri,
            "consultas_taco": "; ".join(f"{c['entrada'].get('alimento')} {c['entrada'].get('gramas')} g -> "
                                        f"{c['saida'].get('alimento_taco', c['saida'].get('erro'))}"
                                        for c in r.consultas_taco),
            "encaminhamento_automatico": "sim" if r.fallback else "",
            "observacao_esperada": q["observacao"], "passos": r.passos,
            "tokens_entrada": r.tokens_entrada, "tokens_saida": r.tokens_saida,
            "tokens_cache": r.tokens_cache_lidos, "custo_usd": f"{custo:.5f}",
            "revisao_ray": "",
        })

    pasta = RAIZ / "testes" / "resultados"
    pasta.mkdir(exist_ok=True)
    saida = pasta / f"resultado_{datetime.now():%Y%m%d_%H%M}.csv"
    with saida.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig: abre certo no Excel
        w = csv.DictWriter(f, fieldnames=list(linhas[0]), delimiter=";")
        w.writeheader()
        w.writerows(linhas)
    print(f"\nAcertos: {acertos}/{len(linhas)} | custo total ~US$ {custo_total:.4f} | {saida}")


if __name__ == "__main__":
    main()
