"""Consulta à TACO e cálculo por porção.

Duas fontes com a mesma interface `buscar(termo, limite) -> list[dict]`:
  - TacoPostgres: chama public.buscar_alimento() no Supabase (produção).
  - TacoJSON: lê dados/taco.json e imita a mesma busca em Python (testes sem banco).

`consultar_taco()` é o que a ferramenta do bot chama: acha o alimento e calcula a porção.
Nunca inventa número: valor ausente vem com o motivo ("traço", "não se aplica"...).
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Protocol

# Nutrientes que o bot informa: coluna -> (rótulo, unidade, casas decimais)
NUTRIENTES = {
    "energia_kcal": ("energia", "kcal", 0),
    "proteina_g": ("proteína", "g", 1),
    "carboidrato_g": ("carboidrato", "g", 1),
    "lipideos_g": ("gorduras totais", "g", 1),
    "fibra_g": ("fibra alimentar", "g", 1),
}

SIGNIFICADO_MARCACAO = {
    "Tr": "traço (quantidade muito pequena)",
    "NA": "não se aplica a este alimento",
    "*": "em reavaliação pela TACO",
}
SEM_DADO = "sem dado na TACO"


class FonteTaco(Protocol):
    def buscar(self, termo: str, limite: int = 5) -> list[dict]: ...
    def obter(self, id_taco: int) -> dict | None: ...


# ---------------------------------------------------------------------
# Busca em Python (espelha supabase/migrations/20260928230000_busca_com_peso.sql)
# ---------------------------------------------------------------------
def normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return sem_acento.lower()


def _trigramas(palavra: str) -> set[str]:
    p = f"  {palavra} "
    return {p[i : i + 3] for i in range(len(p) - 2)}


def _semelhanca(palavra: str, nome: str) -> float:
    """Aproximação do word_similarity do pg_trgm: melhor palavra do nome."""
    t = _trigramas(palavra)
    melhor = 0.0
    for w in re.split(r"[^a-z0-9]+", nome):
        if w:
            melhor = max(melhor, len(t & _trigramas(w)) / len(t))
    return melhor


def _palavras(termo: str) -> list[str]:
    frase = normalizar(termo.strip())
    palavras = [w for w in re.split(r"[^a-z0-9]+", frase) if len(w) >= 3]
    curta = re.sub(r"[^a-z0-9]+", "", frase)
    if not palavras and len(curta) >= 2:
        palavras = [curta]
    return palavras


# Termo com preparo (ex.: "cozida"): a versão crua perde 0,2 (cru e cozido mudam muito o valor).
PREPARADO = {"cozido", "cozida", "grelhado", "grelhada", "assado", "assada", "frito", "frita",
             "refogado", "refogada", "ensopado", "ensopada", "torrado", "torrada"}
_CRU = re.compile(r"(?<![a-z0-9])crua?(?![a-z0-9])")


def _tem_palavra(w: str, nome: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(w), nome) is not None


class TacoJSON:
    def __init__(self, caminho: str | Path):
        self.alimentos = json.loads(Path(caminho).read_text(encoding="utf-8"))
        for a in self.alimentos:
            a["_norm"] = normalizar(a["nome"])

    def _peso(self, w: str) -> float:
        """1 + ln(N / (1 + df)); palavra que não aparece em nenhum alimento pesa 1."""
        df = sum(1 for a in self.alimentos if _tem_palavra(w, a["_norm"]))
        return 1.0 if df == 0 else 1.0 + math.log(len(self.alimentos) / (1 + df))

    def buscar(self, termo: str, limite: int = 5) -> list[dict]:
        palavras = _palavras(termo)
        if not palavras:
            return []
        pesos = [self._peso(w) for w in palavras]
        preparado = any(w in PREPARADO for w in palavras)
        achados = []
        for a in self.alimentos:
            nome = a["_norm"]
            notas = [1.0 if _tem_palavra(w, nome) else _semelhanca(w, nome) for w in palavras]
            cobertura = sum(p * n for p, n in zip(pesos, notas)) / sum(pesos)
            if cobertura >= 0.5:
                relevancia = cobertura + (0.5 if nome.startswith(palavras[0]) else 0)
                if preparado and _CRU.search(nome):
                    relevancia -= 0.2
                achados.append((relevancia, a))
        achados.sort(key=lambda x: (-x[0], len(x[1]["nome"]), x[1]["id"]))
        limite = min(max(limite or 5, 1), 50)
        return [{k: v for k, v in a.items() if k != "_norm"} | {"relevancia": r} for r, a in achados[:limite]]

    def obter(self, id_taco: int) -> dict | None:
        a = next((a for a in self.alimentos if a["id"] == int(id_taco)), None)
        return {k: v for k, v in a.items() if k != "_norm"} if a else None


class TacoPostgres:
    def __init__(self, dsn: str):
        from bot.banco import Conexao  # só quando usa o banco

        self._db = Conexao(dsn, somente_leitura=True)

    def buscar(self, termo: str, limite: int = 5) -> list[dict]:
        linhas = self._db.executar("select * from public.buscar_alimento(%s, %s)", (termo, limite),
                                   retorno="todos", como_dict=True)
        return [dict(r) for r in linhas]

    def obter(self, id_taco: int) -> dict | None:
        r = self._db.executar(
            "select id, nome, grupo, energia_kcal, proteina_g, lipideos_g, carboidrato_g, fibra_g, marcacoes "
            "from public.alimentos_taco where id = %s", (int(id_taco),), retorno="um", como_dict=True)
        return dict(r) if r else None


# ---------------------------------------------------------------------
# Porção
# ---------------------------------------------------------------------
def _formatar(numero: float, casas: int, unidade: str) -> str:
    menor = 10 ** -casas  # 1 kcal, 0,1 g
    if 0 < numero < menor / 2:  # arredondaria para zero, mas não é zero
        return f"menos de {f'{menor:.{casas}f}'.replace('.', ',')} {unidade}"
    return f"{numero:.{casas}f}".replace(".", ",") + f" {unidade}"


def calcular_porcao(alimento: dict, gramas: float) -> dict:
    """Valores da TACO (por 100 g) escalados para `gramas`, já no formato de exibição."""
    marcacoes = alimento.get("marcacoes") or {}
    valores = {}
    for coluna, (rotulo, unidade, casas) in NUTRIENTES.items():
        bruto = alimento.get(coluna)
        if bruto is None:
            valores[rotulo] = SIGNIFICADO_MARCACAO.get(marcacoes.get(coluna), SEM_DADO)
        else:
            valores[rotulo] = _formatar(float(bruto) * gramas / 100, casas, unidade)
    return valores


# Nomes do dia a dia -> como a TACO chama. Lista para a Ray revisar e ampliar.
SINONIMOS = {
    "arroz branco": "arroz tipo 1",
    "aipim": "mandioca",
    "macaxeira": "mandioca",
}
_PALAVRAS_VAZIAS = {"com", "sem", "uma", "dos", "das", "pra", "que", "por", "para"}


def _aplicar_sinonimos(termo: str) -> str:
    t = normalizar(termo)
    for popular, taco in SINONIMOS.items():
        t = re.sub(r"(?<![a-z0-9])" + re.escape(popular) + r"(?![a-z0-9])", taco, t)
    return t


def _faltando(termo: str, nome_taco: str) -> list[str]:
    """Palavras do termo que não aparecem no alimento encontrado."""
    nome = normalizar(nome_taco)
    return [w for w in _palavras(termo) if w not in _PALAVRAS_VAZIAS and not _tem_palavra(w, nome)]


def melhor_alimento(fonte: FonteTaco, alimento: str) -> tuple[dict | None, list[str], list[dict]]:
    """(melhor alimento, palavras não encontradas, outras opções)."""
    termo = _aplicar_sinonimos(alimento)
    achados = fonte.buscar(termo, 4)
    if not achados:
        return None, [], []
    return achados[0], _faltando(termo, achados[0]["nome"]), achados[1:]


def consultar_taco(fonte: FonteTaco, alimento: str, gramas: float = 100) -> dict:
    """Resultado da ferramenta `consultar_taco` do bot."""
    if not alimento or not str(alimento).strip():
        return {"encontrado": False, "erro": "informe o nome do alimento"}
    try:
        gramas = float(gramas)
    except (TypeError, ValueError):
        return {"encontrado": False, "erro": "gramas precisa ser um número"}
    if not 0 < gramas <= 5000:
        return {"encontrado": False, "erro": "gramas precisa estar entre 0 e 5000"}

    melhor, faltando, outras = melhor_alimento(fonte, alimento)
    if melhor is None:
        return {"encontrado": False, "termo": alimento, "erro": "alimento não encontrado na TACO"}
    resultado = {
        "encontrado": True,
        "correspondencia": "aproximada" if faltando else "exata",
        "alimento_taco": melhor["nome"],
        "id_taco": melhor["id"],
        "gramas": gramas,
        "valores": calcular_porcao(melhor, gramas),
        "outras_opcoes": [a["nome"] for a in outras],
        "fonte": "TACO 4ª edição (NEPA/UNICAMP)",
    }
    if faltando:
        resultado["palavras_nao_encontradas"] = faltando
        resultado["aviso"] = (
            f"A TACO não tem exatamente '{alimento}'. Diga ao paciente que o valor é de "
            f"'{melhor['nome']}' e não chame de outro nome."
        )
    return resultado
