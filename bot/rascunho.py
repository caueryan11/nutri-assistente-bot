"""Rascunho de resposta para as mensagens que o bot encaminha à nutri.

Quando o bot não pode responder sozinho (dúvida fora do plano, suplemento, sintoma...), o assistente pesquisa em
fontes confiáveis, escreve uma sugestão de resposta e manda junto com o aviso. A nutri responde "sim" para
enviar, "não" para descartar ou escreve a versão dela (whatsapp/app.py). Nada vai para o paciente sem ela.

  - Busca só nos domínios de DOMINIOS (PubMed, SciELO, diretrizes, Ministério da Saúde...). Lista provisória:
    a Ray revisa.
  - A busca não leva dado do paciente: o modelo é instruído a pesquisar só o tema, e a pergunta vai sem nome.
  - Fonte que não apareceu nos resultados da busca é descartada (evita referência inventada).
  - Sem evidência sólida, ou emergência: não sugere resposta; o aviso diz isso.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

import anthropic

from bot.anamnese import texto_para_prompt

log = logging.getLogger("rascunho")

MODELO_PADRAO = "claude-sonnet-5-5"
MAX_PASSOS = 4
MAX_BUSCAS = 3
MAX_RESPOSTA = 700  # caracteres: mensagem de WhatsApp, não artigo

DOMINIOS = [
    "pubmed.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov", "cochranelibrary.com",
    "scielo.br", "scielo.org",
    "gov.br", "bvsms.saude.gov.br",                       # Ministério da Saúde, ANVISA, Guia Alimentar
    "who.int",
    "diabetes.org.br", "abeso.org.br", "sbem.org.br", "sbcbm.org.br", "asbran.org.br", "cfn.org.br", "sban.org.br",
    "espen.org", "clinicalnutritionjournal.com", "eatright.org",
]

INSTRUCOES = """Você ajuda a nutricionista {nutri} a responder um paciente no WhatsApp. O assistente automático não pôde
responder esta mensagem sozinho e a encaminhou para ela. Sua tarefa: pesquisar e escrever uma SUGESTÃO de resposta.
A nutricionista vai ler, aprovar, editar ou descartar; nada vai para o paciente sem ela.

Como trabalhar:
1. Se precisar de evidência, use `web_search` (no máximo {buscas} buscas). Pesquise só o tema, em português ou inglês
   (ex.: "whey protein gastritis tolerance"). NUNCA ponha nome, telefone ou outro dado do paciente na busca.
2. Escreva a resposta como se fosse a própria nutricionista falando com o paciente: primeira pessoa, tom acolhedor,
   claro e curto (até ~{maximo} caracteres), pronto para o WhatsApp (negrito com *um asterisco*, sem títulos, sem links).
   Não comece com o nome da nutricionista: o sistema já identifica que a mensagem é dela.
3. Respeite o plano alimentar e a anamnese abaixo: não sugira nada que conflite com alergias, intolerâncias ou
   restrições, e não mude o plano de forma grande; se a mudança for grande, sugira conversar no retorno.
4. Quase sempre dá para sugerir uma resposta útil e segura. Se a decisão depende de informação que falta ou da
   avaliação da nutricionista, a sugestão pode acolher, dar a orientação conservadora que a evidência sustenta e fazer
   ao paciente as 1-2 perguntas que faltam (ou propor ver no retorno). Nunca invente dado ou número: o que a
   evidência não sustenta, não afirme. Use confianca 'alta' (evidência e plano claros), 'media' ou 'baixa'.
5. confianca='sem_evidencia' com `resposta` vazia só quando não houver resposta segura possível, e sempre em
   emergência (desmaio, falta de ar, dor no peito, sangramento): aí `atencao` diz para a nutricionista falar com o
   paciente na hora (ele já recebeu a orientação do SAMU 192).
6. `atencao`: UMA frase curta (até ~140 caracteres) do que a nutricionista deve conferir antes de enviar
   (ex.: "Ela usa omeprazol: confira se mantém o whey longe da dose."). Vazio se não houver nada.
7. Termine sempre chamando `entregar_rascunho`. Em `fontes`, só os links que você realmente usou dos resultados da busca.

{paciente}

## Plano alimentar atual
```json
{plano}
```
"""

FERRAMENTA = {
    "name": "entregar_rascunho",
    "description": "Entrega a sugestão de resposta para a nutricionista revisar. Toda tarefa termina aqui.",
    "input_schema": {
        "type": "object",
        "properties": {
            "resposta": {"type": "string", "description": "Texto para o paciente (vazio se sem_evidencia)."},
            "fontes": {"type": "array", "items": {"type": "object", "properties": {
                "titulo": {"type": "string"}, "url": {"type": "string"}},
                "required": ["titulo", "url"], "additionalProperties": False}},
            "atencao": {"type": "string", "description": "Uma frase do que conferir antes de enviar, ou vazio."},
            "confianca": {"type": "string", "enum": ["alta", "media", "baixa", "sem_evidencia"]},
        },
        "required": ["resposta", "fontes", "atencao", "confianca"],
        "additionalProperties": False,
    },
}


@dataclass
class Rascunho:
    resposta: str                      # vazio = sem sugestão (sem evidência ou emergência)
    fontes: list[dict] = field(default_factory=list)   # [{titulo, url}], só as que vieram da busca
    atencao: str = ""
    confianca: str = "media"
    buscas: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0


def _dominio_permitido(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in DOMINIOS)


def _urls_da_busca(conteudo) -> set[str]:
    urls = set()
    for bloco in conteudo:
        if getattr(bloco, "type", "") == "web_search_tool_result":
            for item in getattr(bloco, "content", None) or []:
                url = getattr(item, "url", None)
                if url:
                    urls.add(url.rstrip("/"))
    return urls


def gerar(pergunta: str, resumo: str, primeiro_nome: str, plano: dict | None, anamnese: dict | None, nutri: str,
          *, historico: list[dict] | None = None, cliente: anthropic.Anthropic | None = None,
          modelo: str | None = None) -> Rascunho | None:
    """Sugestão de resposta, ou None se a API falhar ou o modelo não entregar (o aviso sai sem sugestão)."""
    cliente = cliente or anthropic.Anthropic()
    modelo = modelo or os.environ.get("MODELO_RASCUNHO") or MODELO_PADRAO
    plano = plano or {"refeicoes": []}
    system = INSTRUCOES.format(nutri=nutri, buscas=MAX_BUSCAS, maximo=MAX_RESPOSTA,
                               paciente=texto_para_prompt(anamnese, plano, primeiro_nome),
                               plano=json.dumps(plano, ensure_ascii=False))
    conversa = "\n".join(f"{'Paciente' if m['role'] == 'user' else 'Assistente'}: {m['content']}"
                         for m in (historico or []) if isinstance(m.get("content"), str))
    pedido = (f"Mensagem do paciente ({primeiro_nome}):\n\"{pergunta}\"\n\n"
              f"Por que foi encaminhada: {resumo or 'o assistente não soube responder'}\n"
              + (f"\nConversa recente:\n{conversa}\n" if conversa else ""))
    ferramentas = [{"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_BUSCAS,
                    "allowed_domains": DOMINIOS}, FERRAMENTA]
    mensagens = [{"role": "user", "content": pedido}]
    r = Rascunho(resposta="")
    vistas: set[str] = set()
    for _ in range(MAX_PASSOS):
        try:
            resp = cliente.messages.create(model=modelo, max_tokens=2000, system=system, tools=ferramentas,
                                           messages=mensagens)
        except anthropic.APIError as e:
            log.warning("rascunho: erro na API (%s)", type(e).__name__)
            return None
        r.tokens_entrada += resp.usage.input_tokens
        r.tokens_saida += resp.usage.output_tokens
        r.buscas += sum(1 for b in resp.content if getattr(b, "type", "") == "server_tool_use")
        vistas |= _urls_da_busca(resp.content)
        entrega = next((b for b in resp.content if b.type == "tool_use" and b.name == "entregar_rascunho"), None)
        if entrega is not None:
            e = entrega.input if isinstance(entrega.input, dict) else {}
            r.confianca = e.get("confianca") if e.get("confianca") in ("alta", "media", "baixa", "sem_evidencia") else "baixa"
            r.resposta = "" if r.confianca == "sem_evidencia" else str(e.get("resposta") or "").strip()
            r.atencao = str(e.get("atencao") or "").strip()
            r.fontes = [{"titulo": str(f.get("titulo") or "").strip()[:120], "url": str(f.get("url") or "").strip()}
                        for f in (e.get("fontes") or []) if isinstance(f, dict)
                        and str(f.get("url") or "").strip().rstrip("/") in vistas and _dominio_permitido(str(f["url"]))][:3]
            log.info("rascunho: confianca=%s buscas=%d fontes=%d", r.confianca, r.buscas, len(r.fontes))
            return r
        mensagens.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue  # a busca no servidor pausou: reenviar retoma de onde parou
        mensagens.append({"role": "user", "content": "Entregue agora a sugestão chamando `entregar_rascunho`."})
    log.warning("rascunho: modelo não entregou em %d passos", MAX_PASSOS)
    return None
