"""Importa a TACO (4ª edição, NEPA/UNICAMP) da planilha oficial para o banco.

Fonte: https://nepa.unicamp.br/publicacoes/tabela-taco-excel/ (aba "CMVCol taco3":
composição centesimal, minerais e vitaminas, por 100 g de parte comestível).

Regras:
  - "Tr" (traço), "NA" (não aplicável) e "*" (análise em reavaliação) viram NULL
    no número e ficam anotados em `marcacoes`, ex.: {"fibra_g": "Tr"}. Nunca viram zero.
  - Célula em branco = "análise não solicitada" (legenda da TACO): NULL sem marcação.
  - Carboidrato negativo por arredondamento (calculado por diferença) vira 0, como no PDF.
  - Os valores são os da planilha, sem arredondar. O livro/PDF mostra os mesmos
    valores arredondados; arredonde só na hora de exibir.

Uso:
  python scripts/importar_taco.py dados/taco_4ed_nepa.xlsx --sql dados/taco.sql
  python scripts/importar_taco.py dados/taco_4ed_nepa.xlsx --json dados/taco.json
  python scripts/importar_taco.py dados/taco_4ed_nepa.xlsx --db      # usa DATABASE_URL

O SQL gerado é um upsert numa transação: pode rodar de novo sem duplicar.
Só usa a biblioteca padrão (lê o .xlsx direto do zip); --db precisa de psycopg2.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
ABA = "xl/worksheets/sheet1.xml"  # "CMVCol taco3"

# coluna da planilha -> coluna de public.alimentos_taco
# (a coluna N repete o número do alimento e é usada só para conferência)
COLUNAS = {
    "C": "umidade_pct",
    "D": "energia_kcal",
    "E": "energia_kj",
    "F": "proteina_g",
    "G": "lipideos_g",
    "H": "colesterol_mg",
    "I": "carboidrato_g",
    "J": "fibra_g",
    "K": "cinzas_g",
    "L": "calcio_mg",
    "M": "magnesio_mg",
    "O": "manganes_mg",
    "P": "fosforo_mg",
    "Q": "ferro_mg",
    "R": "sodio_mg",
    "S": "potassio_mg",
    "T": "cobre_mg",
    "U": "zinco_mg",
    "V": "retinol_mcg",
    "W": "re_mcg",
    "X": "rae_mcg",
    "Y": "tiamina_mg",
    "Z": "riboflavina_mg",
    "AA": "piridoxina_mg",
    "AB": "niacina_mg",
    "AC": "vitamina_c_mg",
}
MARCACOES = {"Tr", "NA", "*"}

# Erros de digitação da planilha, conferidos no PDF oficial
# (taco_4_edicao_ampliada_e_revisada.pdf). Chave: (id, coluna do banco).
CORRECOES = {
    (373, "piridoxina_mg"): Decimal("0.02"),  # planilha: ",0,02"; PDF p. 54: 0,02
}

TOTAL_ESPERADO = 597
LINHAS_QUE_NAO_SAO_GRUPO = {"Número do", "Alimento", "Legenda"}


def _linhas_xlsx(caminho: Path) -> list[dict[str, str | None]]:
    with zipfile.ZipFile(caminho) as z:
        compartilhadas = [
            "".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t"))
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)
        ]
        raiz = ET.fromstring(z.read(ABA))
    linhas = []
    for row in raiz.iter(f"{{{NS['m']}}}row"):
        celulas: dict[str, str | None] = {}
        for c in row.findall("m:c", NS):
            col = re.match(r"[A-Z]+", c.get("r")).group()
            v = c.find("m:v", NS)
            if v is None:
                inline = c.find("m:is", NS)
                valor = "".join(t.text or "" for t in inline.iter(f"{{{NS['m']}}}t")) if inline is not None else None
            elif c.get("t") == "s":
                valor = compartilhadas[int(v.text)]
            else:
                valor = v.text
            celulas[col] = valor
        linhas.append(celulas)
    return linhas


def _converter(bruto: str | None, id_: int, coluna: str) -> tuple[Decimal | None, str | None]:
    """Retorna (número, marcação). Erro se o valor não for reconhecido."""
    if (id_, coluna) in CORRECOES:
        return CORRECOES[(id_, coluna)], None
    texto = (bruto or "").strip()
    if texto == "":
        return None, None
    if texto in MARCACOES:
        return None, texto
    try:
        numero = Decimal(texto)
    except InvalidOperation:
        raise ValueError(f"alimento {id_}, {coluna}: valor não reconhecido {bruto!r}") from None
    # A TACO calcula carboidrato por diferença; em 4 alimentos (288, 322, 337, 400)
    # a planilha traz resíduo negativo (ex.: -0,027) e o PDF oficial publica 0,0.
    if coluna == "carboidrato_g" and Decimal("-0.1") < numero < 0:
        return Decimal(0), None
    if numero < 0:
        raise ValueError(f"alimento {id_}, {coluna}: valor negativo {numero}")
    return numero, None


def ler_taco(caminho: Path) -> list[dict]:
    alimentos = []
    grupo = None
    for r in _linhas_xlsx(caminho)[3:]:  # 3 linhas de cabeçalho
        a = (r.get("A") or "").strip()
        nome = (r.get("B") or "").strip()
        if re.fullmatch(r"\d+(\.0)?", a):
            id_ = int(float(a))
            if r.get("N") and int(float(r["N"])) != id_:
                raise ValueError(f"alimento {id_}: coluna N diz {r['N']}")
            item = {"id": id_, "nome": nome, "grupo": grupo, "marcacoes": {}}
            for col, destino in COLUNAS.items():
                numero, marca = _converter(r.get(col), id_, destino)
                item[destino] = numero
                if marca:
                    item["marcacoes"][destino] = marca
            alimentos.append(item)
        elif a and not nome and a not in LINHAS_QUE_NAO_SAO_GRUPO:
            grupo = a
        elif a == "Legenda":
            break

    ids = [x["id"] for x in alimentos]
    if len(ids) != TOTAL_ESPERADO or len(set(ids)) != len(ids):
        raise ValueError(f"esperava {TOTAL_ESPERADO} alimentos únicos, veio {len(ids)} ({len(set(ids))} únicos)")
    if any(not x["nome"] or not x["grupo"] for x in alimentos):
        raise ValueError("alimento sem nome ou sem grupo")
    return alimentos


def _sql_valor(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, Decimal):
        return format(v.normalize(), "f")
    if isinstance(v, dict):
        return "'" + json.dumps(v, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


def gerar_sql(alimentos: list[dict]) -> str:
    colunas = ["id", "nome", "grupo", *COLUNAS.values(), "marcacoes"]
    linhas = [
        "(" + ", ".join(_sql_valor(a[c]) for c in colunas) + ")"
        for a in alimentos
    ]
    atualiza = ",\n  ".join(f"{c} = excluded.{c}" for c in colunas[1:])
    return (
        "-- TACO 4ª edição (NEPA/UNICAMP), gerado por scripts/importar_taco.py\n"
        "begin;\n"
        f"insert into public.alimentos_taco ({', '.join(colunas)}) values\n"
        + ",\n".join(linhas)
        + f"\non conflict (id) do update set\n  {atualiza};\n"
        f"do $$ begin\n"
        f"  if (select count(*) from public.alimentos_taco) <> {TOTAL_ESPERADO} then\n"
        f"    raise exception 'TACO: esperava {TOTAL_ESPERADO} alimentos';\n"
        f"  end if;\nend $$;\n"
        "commit;\n"
        "select count(*) as alimentos, count(distinct grupo) as grupos from public.alimentos_taco;\n"
    )


def gravar_banco(alimentos: list[dict]) -> None:
    import psycopg2  # só quando precisa

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL não definida")
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        cur.execute(gerar_sql(alimentos).split("select count(*) as alimentos")[0].replace("begin;\n", "").replace("commit;\n", ""))
    print(f"{len(alimentos)} alimentos gravados no banco")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("planilha", type=Path)
    p.add_argument("--sql", type=Path, help="gera arquivo SQL (upsert)")
    p.add_argument("--json", type=Path, help="gera JSON (para testes sem banco)")
    p.add_argument("--db", action="store_true", help="grava direto no banco (DATABASE_URL)")
    args = p.parse_args()

    alimentos = ler_taco(args.planilha)
    grupos = sorted({a["grupo"] for a in alimentos})
    print(f"{len(alimentos)} alimentos em {len(grupos)} grupos")

    if args.sql:
        args.sql.write_text(gerar_sql(alimentos), encoding="utf-8", newline="\n")
        print(f"SQL: {args.sql}")
    if args.json:
        dados = [{k: (float(v) if isinstance(v, Decimal) else v) for k, v in a.items()} for a in alimentos]
        args.json.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        print(f"JSON: {args.json}")
    if args.db:
        gravar_banco(alimentos)


if __name__ == "__main__":
    main()
