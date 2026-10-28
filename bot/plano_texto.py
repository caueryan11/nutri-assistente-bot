"""Plano alimentar em texto simples <-> JSON que o bot usa (planos_alimentares.conteudo).

A nutri escreve (ou cola) assim, na área dela:

    # Café da manhã 07:00
    Pão francês | 1 unidade (50 g) | 2 fatias de pão de forma integral (50 g); 3 colheres de sopa de tapioca (45 g)
    Café sem açúcar | à vontade

    # Almoço 12:30
    Arroz integral cozido | 4 colheres de sopa (100 g) | 1 batata-doce média cozida (130 g)

    Observações:
    Beber pelo menos 2 litros de água por dia.

  - "# Nome HH:MM" abre uma refeição (o horário é opcional, mas o bot usa para saber o que já passou)
  - cada item: alimento | quantidade | substituições separadas por ";"  (quantidade e substituições opcionais)
  - "Observações:" até o fim: uma orientação geral por linha
Colocar a quantidade em gramas, ex.: "(100 g)", deixa o bot calcular pela TACO.
"""

from __future__ import annotations

import re

_HORARIO = re.compile(r"\s+(\d{1,2})[:h](\d{2})\s*$")
_OBS = re.compile(r"^\s*observa[cç](?:ões|oes|ão|ao)\s*(gerais)?\s*:?\s*$", re.I)


def para_texto(plano: dict | None) -> str:
    if not isinstance(plano, dict):
        return ""
    blocos = []
    for ref in plano.get("refeicoes", []) or []:
        cab = f"# {ref.get('nome', '').strip()}" + (f" {ref['horario']}" if ref.get("horario") else "")
        linhas = [cab]
        for item in ref.get("itens", []) or []:
            partes = [str(item.get("alimento", "")).strip(), str(item.get("quantidade", "") or "").strip()]
            subs = "; ".join(str(s).strip() for s in item.get("substituicoes", []) or [] if str(s).strip())
            if subs:
                partes.append(subs)
            while partes and not partes[-1]:
                partes.pop()
            linhas.append(" | ".join(partes))
        blocos.append("\n".join(linhas))
    obs = plano.get("observacoes_gerais") or []
    if isinstance(obs, str):
        obs = [obs]
    if obs:
        blocos.append("Observações:\n" + "\n".join(str(o).strip() for o in obs))
    return "\n\n".join(blocos)


def de_texto(texto: str) -> tuple[dict, list[str]]:
    """(plano, erros). Com erros, o plano não deve ser salvo."""
    refeicoes: list[dict] = []
    obs: list[str] = []
    erros: list[str] = []
    atual = None
    em_obs = False
    for n, bruta in enumerate((texto or "").splitlines(), start=1):
        linha = bruta.strip()
        if not linha:
            continue
        if _OBS.match(linha):
            em_obs = True
            continue
        if em_obs:
            obs.append(linha.lstrip("-• ").strip())
            continue
        if linha.startswith("#"):
            cab = linha.lstrip("#").strip()
            horario = None
            m = _HORARIO.search(cab)
            if m:
                h, mi = int(m.group(1)), int(m.group(2))
                if h > 23 or mi > 59:
                    erros.append(f"linha {n}: horário inválido em \"{cab}\"")
                horario = f"{h:02d}:{mi:02d}"
                cab = cab[:m.start()].strip()
            if not cab:
                erros.append(f"linha {n}: refeição sem nome")
            atual = {"nome": cab, "horario": horario, "itens": []}
            if horario is None:
                del atual["horario"]
            refeicoes.append(atual)
            continue
        if atual is None:
            erros.append(f"linha {n}: item antes da primeira refeição (comece com \"# Café da manhã 07:00\")")
            continue
        partes = [p.strip() for p in linha.lstrip("-• ").split("|")]
        if len(partes) > 3:
            erros.append(f"linha {n}: use no máximo duas barras \"|\" (alimento | quantidade | substituições)")
            continue
        alimento = partes[0]
        if not alimento:
            erros.append(f"linha {n}: item sem alimento")
            continue
        quantidade = partes[1] if len(partes) > 1 else ""
        subs = [s.strip() for s in partes[2].split(";") if s.strip()] if len(partes) > 2 else []
        atual["itens"].append({"alimento": alimento, "quantidade": quantidade, "substituicoes": subs})
    if not refeicoes:
        erros.append("o plano precisa de pelo menos uma refeição (linha começando com #)")
    for r in refeicoes:
        if not r["itens"]:
            erros.append(f"a refeição \"{r['nome']}\" está sem itens")
    plano = {"refeicoes": refeicoes}
    if obs:
        plano["observacoes_gerais"] = obs
    return plano, erros


def mesclar_extras(novo: dict, antigo: dict | None) -> dict:
    """Mantém campos que o texto não mostra (ex.: id_taco) nos itens com o mesmo alimento."""
    if not isinstance(antigo, dict):
        return novo
    extras = {}
    for ref in antigo.get("refeicoes", []) or []:
        for item in ref.get("itens", []) or []:
            outros = {k: v for k, v in item.items() if k not in ("alimento", "quantidade", "substituicoes")}
            if outros:
                extras[str(item.get("alimento", "")).strip().lower()] = outros
    for ref in novo.get("refeicoes", []):
        for item in ref["itens"]:
            item.update({k: v for k, v in extras.get(item["alimento"].lower(), {}).items() if k not in item})
    return novo
