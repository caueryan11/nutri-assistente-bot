"""Atendimento comercial de quem ainda NÃO é paciente (lead): triagem, convênio e primeira consulta.

Mesmo laço do bot clínico (Claude Haiku 4.5 + ferramentas + enviar_resposta), com outro papel:
acolher, entender o objetivo, responder sobre convênio/reembolso, valor e metodologia, e marcar
a primeira consulta. Não dá orientação nutricional nem clínica, e não pede detalhes de saúde.

Ao marcar, o lead vira paciente (sem consentimento): o webhook manda o termo em seguida e o
fluxo clínico só começa depois do "Aceito".
Textos provisórios: a Ray revisa.
"""

from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Callable
from zoneinfo import ZoneInfo

import anthropic

from bot import avisos
from bot.assistente import (FUSO_PADRAO, MAX_PASSOS, MAX_TOKENS, MODELO_PADRAO, _validar_resposta,
                            ajustar_emojis, formatar_whatsapp)

log = logging.getLogger(__name__)

MENSAGEM_ENCAMINHAMENTO_LEAD = ("Obrigado pela mensagem! Vou passar para a nutricionista {nutri}, "
                                "e ela te responde assim que puder.")
MENSAGEM_NAO_TEXTO = ("Recebi sua mensagem! Por aqui eu consigo te ajudar melhor por texto. "
                      "Me conta por escrito como posso ajudar?")


# ---------------------------------------------------------------------
# Dados da nutri e convênio
# ---------------------------------------------------------------------
@dataclass
class NutriComercial:
    nome: str
    atende_convenio: bool = False
    convenios_aceitos: list[str] = field(default_factory=list)
    valor_consulta: Decimal | float | None = None
    politica_reembolso: str | None = None
    apresentacao_servico: str | None = None


def _norm(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    palavras = [p for p in t.split() if p not in {"plano", "de", "saude", "convenio", "o", "a", "do", "da"}]
    return " ".join(palavras)


def verificar_convenio(nutri: NutriComercial, convenio: str) -> dict:
    """Resultado da ferramenta consultar_convenio: a comparação é feita aqui, não pelo modelo."""
    pedido = _norm(convenio)
    base = {"convenio_citado": convenio, "politica_reembolso": nutri.politica_reembolso or ""}
    if not nutri.atende_convenio:
        return {**base, "atende_convenio": False, "aceito": False,
                "orientacao": ("A nutricionista atende só particular, para garantir um plano 100% individualizado. "
                               "Explique com elegância e destaque o recibo para pedir reembolso ao plano.")}
    if not pedido:
        return {**base, "atende_convenio": True, "aceito": False, "convenios_aceitos": nutri.convenios_aceitos,
                "orientacao": "Pergunte qual é o convênio do paciente."}
    for aceito in nutri.convenios_aceitos:
        a = _norm(aceito)
        if a and (pedido == a or pedido in a or a in pedido):
            return {**base, "atende_convenio": True, "aceito": True, "nome_na_lista": aceito,
                    "orientacao": ("Convênio atendido. Oriente a marcar a primeira consulta informando o convênio; "
                                   "a cobertura exata depende do plano do paciente e é confirmada no atendimento.")}
    return {**base, "atende_convenio": True, "aceito": False, "convenios_aceitos": nutri.convenios_aceitos,
            "orientacao": "Este convênio não está na lista. Ofereça o atendimento particular com recibo para reembolso."}


def _valor(v) -> str:
    if v is None:
        return "não informado (não cite valor; encaminhe se o lead insistir)"
    return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# ---------------------------------------------------------------------
# Prompt e ferramentas
# ---------------------------------------------------------------------
INSTRUCOES = """# Atendimento de novos interessados

Você é o assistente virtual do consultório da nutricionista {nutri}, no WhatsApp. Você conversa com pessoas que
AINDA NÃO SÃO pacientes. Você não é a nutricionista e nunca se passa por ela.

## Objetivo
Acolher, entender o que a pessoa busca, tirar dúvidas sobre o atendimento (convênio, reembolso, valor, como é a
consulta) e, quando ela quiser, marcar a primeira consulta. Conversão ética: informe e convide, sem pressionar.

## Como conduzir
- Tom acolhedor, profissional e prestativo. Frases curtas. Use o primeiro nome quando souber.
- Descubra o objetivo de forma natural (ex.: emagrecimento, ganho de massa, saúde/exames, reeducação alimentar),
  sem interrogatório. Registre com `atualizar_lead` o nome, o objetivo (poucas palavras) e o convênio assim que souber.
- Antes de propor horário, apresente como é a consulta e o valor (dados abaixo). Não invente desconto, pacote,
  prazo, endereço ou forma de pagamento que não estejam abaixo; se perguntarem, encaminhe.
- Convênio ou reembolso: SEMPRE use `consultar_convenio` e siga a orientação que ela devolver.
- Quando a pessoa quiser marcar: chame `listar_horarios_disponiveis`, ofereça até 3 opções e, quando ela escolher
  um horário claramente e você souber o nome completo dela, chame `marcar_primeira_consulta`. Se faltar o nome, pergunte.
- Depois de marcar: confirme dia e hora e avise que ela vai receber agora o termo de consentimento para o
  acompanhamento pelo WhatsApp.

## Limites (importante)
- Não dê orientação nutricional, não sugira alimentos, dietas ou suplementos e não comente corpo ou peso.
- Não peça detalhes de saúde (doenças, exames, remédios). Se a pessoa contar, acolha e diga que a nutricionista
  avalia tudo com cuidado na consulta. No objetivo, registre só algo genérico como "saúde/exames".
- Emergência (falta de ar, dor no peito, desmaio): oriente ligar já para o SAMU 192.
- Encaminhe para a nutricionista (acao='encaminhar') se a pessoa pedir para falar com ela, negociar valor,
  reclamar, ou perguntar algo que você não sabe responder com os dados abaixo.
- Responda sempre chamando `enviar_resposta`. Formatação do WhatsApp: negrito com *um asterisco*.

## Dados do consultório
- Nutricionista: {nutri}
- Atende convênio: {convenio}
- Valor da consulta: {valor}
- Como é o atendimento: {apresentacao}
- Reembolso: {reembolso}
"""

FERRAMENTAS_BASE = [
    {
        "name": "consultar_convenio",
        "description": "Verifica se a nutricionista atende o convênio citado e devolve a orientação sobre convênio e reembolso.",
        "input_schema": {"type": "object", "properties": {"convenio": {"type": "string", "description": "Nome do convênio citado (ex.: 'Unimed'). Vazio se a pessoa só perguntou se atende convênio."}},
                         "required": ["convenio"], "additionalProperties": False},
    },
    {
        "name": "atualizar_lead",
        "description": "Registra o que a pessoa contou: nome, objetivo (poucas palavras, sem detalhes de saúde) e convênio.",
        "input_schema": {"type": "object", "properties": {
            "nome": {"type": "string"}, "objetivo": {"type": "string"}, "convenio": {"type": "string"}},
            "required": [], "additionalProperties": False},
    },
    {
        "name": "enviar_resposta",
        "description": ("Envia a resposta final. Toda conversa termina com esta ferramenta. acao='encaminhar' "
                        "quando a pessoa precisa falar com a nutricionista."),
        "input_schema": {
            "type": "object",
            "properties": {
                "acao": {"type": "string", "enum": ["responder", "encaminhar"]},
                "mensagem_paciente": {"type": "string", "description": "Texto que a pessoa recebe no WhatsApp."},
                "motivo": {"type": "string", "description": "Por que encaminhou (obrigatório se encaminhar)."},
                "resumo_para_nutri": {"type": "string", "description": (
                    "Obrigatório se encaminhar. UMA frase (até ~150 caracteres) com o que a pessoa quer, como numa anotação, "
                    "sem nome, telefone ou saudação. Ex.: 'Quer negociar o valor da consulta.'")},
            },
            "required": ["acao", "mensagem_paciente", "motivo", "resumo_para_nutri"],
            "additionalProperties": False,
        },
    },
]

FERRAMENTAS_AGENDA = [
    {
        "name": "listar_horarios_disponiveis",
        "description": "Horários livres para a primeira consulta, já cruzados com a agenda da nutricionista.",
        "input_schema": {"type": "object", "properties": {
            "periodo": {"type": "string", "enum": ["qualquer", "manha", "tarde", "noite"]},
            "a_partir_de": {"type": "string", "description": "Data AAAA-MM-DD (opcional)."}},
            "required": [], "additionalProperties": False},
    },
    {
        "name": "marcar_primeira_consulta",
        "description": ("Marca a primeira consulta e cadastra a pessoa como paciente. Só chame depois que ela escolheu "
                        "claramente um horário de listar_horarios_disponiveis e você sabe o nome completo."),
        "input_schema": {"type": "object", "properties": {
            "nome": {"type": "string", "description": "Nome completo da pessoa."},
            "data_hora_iso": {"type": "string", "description": "Campo 'inicio' do horário escolhido, AAAA-MM-DDTHH:MM."},
            "convenio_usado": {"type": "string", "description": "Convênio usado, ou 'particular'."}},
            "required": ["nome", "data_hora_iso", "convenio_usado"], "additionalProperties": False},
    },
]


def montar_system(nutri: NutriComercial, lead: dict, com_agenda: bool, agora: datetime) -> list[dict]:
    convenio = ("sim: " + ", ".join(nutri.convenios_aceitos)) if nutri.atende_convenio and nutri.convenios_aceitos \
        else ("sim (lista não cadastrada; use consultar_convenio)" if nutri.atende_convenio else "não, só particular")
    fixo = INSTRUCOES.format(nutri=nutri.nome, convenio=convenio, valor=_valor(nutri.valor_consulta),
                             apresentacao=nutri.apresentacao_servico or "não informado",
                             reembolso=nutri.politica_reembolso or "não informado")
    if not com_agenda:
        fixo += "\n- Agenda online indisponível: para marcar, encaminhe para a nutricionista.\n"
    dias = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    sabido = {k: v for k, v in (("nome", lead.get("nome")), ("objetivo", lead.get("objetivo")),
                                 ("convênio", lead.get("convenio_informado"))) if v}
    return [
        {"type": "text", "text": fixo, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": (f"Agora é {dias[agora.isoweekday() - 1]}, {agora:%d/%m/%Y}, {agora:%H:%M} (horário de Brasília). "
                                  f"O que já sabemos desta pessoa: {json.dumps(sabido, ensure_ascii=False) if sabido else 'nada ainda'}.")},
    ]


# ---------------------------------------------------------------------
# Laço
# ---------------------------------------------------------------------
@dataclass
class ResultadoLead:
    acao: str
    mensagem: str
    motivo: str = ""
    resumo_para_nutri: str = ""
    passos: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    convertido: dict | None = None      # {"nome", "descricao", "inicio"} quando marcou a primeira consulta
    fallback: bool = False


def atender_lead(
    mensagem: str,
    lead: dict,
    nutri: NutriComercial,
    *,
    historico: list[dict] | None = None,
    agenda_para: Callable[[str, str], object] | None = None,
    atualizar: Callable[[dict], None] | None = None,
    cliente: anthropic.Anthropic | None = None,
    modelo: str | None = None,
    agora: datetime | None = None,
) -> ResultadoLead:
    """agenda_para(nome, convenio) -> AgendaNutri cujo gravar converte o lead em paciente (None = sem agenda).
    atualizar(campos): grava nome/objetivo/convenio_informado/status no lead."""
    agora = agora or datetime.now(ZoneInfo(FUSO_PADRAO))
    atualizar = atualizar or (lambda campos: None)
    cliente = cliente or anthropic.Anthropic()
    modelo = modelo or os.environ.get("MODELO_BOT") or MODELO_PADRAO
    com_agenda = agenda_para is not None
    ferramentas = FERRAMENTAS_BASE + (FERRAMENTAS_AGENDA if com_agenda else [])
    system = montar_system(nutri, lead, com_agenda, agora)
    mensagens = [*(historico or []), {"role": "user", "content": mensagem}]
    r = ResultadoLead(acao="encaminhar", mensagem="")

    def encaminhar_automatico(motivo: str) -> ResultadoLead:
        r.acao, r.mensagem, r.motivo, r.fallback = "encaminhar", MENSAGEM_ENCAMINHAMENTO_LEAD.format(nutri=nutri.nome), motivo, True
        r.resumo_para_nutri = avisos.resumo_sem_resposta(mensagem)
        log.warning("lead: encaminhamento automático: %s", motivo)
        return r

    for _ in range(MAX_PASSOS):
        r.passos += 1
        try:
            resposta = cliente.messages.create(model=modelo, max_tokens=MAX_TOKENS, system=system, tools=ferramentas,
                                               tool_choice={"type": "any"}, messages=mensagens)
        except anthropic.APIError as e:
            return encaminhar_automatico(f"erro na API: {type(e).__name__}")
        r.tokens_entrada += resposta.usage.input_tokens
        r.tokens_saida += resposta.usage.output_tokens
        chamadas = [b for b in resposta.content if b.type == "tool_use"]
        if not chamadas:
            return encaminhar_automatico("modelo não chamou ferramenta")
        mensagens.append({"role": "assistant", "content": resposta.content})
        resultados = []
        for c in chamadas:
            e = c.input if isinstance(c.input, dict) else {}
            erro = False
            if c.name == "enviar_resposta":
                problema = _validar_resposta(e)
                if problema is None:
                    r.acao = e["acao"]
                    r.mensagem = formatar_whatsapp(ajustar_emojis(e["mensagem_paciente"], mensagem))
                    r.motivo = (e.get("motivo") or "").strip()
                    r.resumo_para_nutri = (e.get("resumo_para_nutri") or "").strip()
                    return r
                saida, erro = {"erro": f"Resposta inválida: {problema}. Corrija e chame enviar_resposta de novo."}, True
            elif c.name == "consultar_convenio":
                saida = verificar_convenio(nutri, e.get("convenio", ""))
                if e.get("convenio"):
                    atualizar({"convenio_informado": e["convenio"][:80]})
            elif c.name == "atualizar_lead":
                campos = {k: str(v)[:120] for k, v in (("nome", e.get("nome")), ("objetivo", e.get("objetivo")),
                                                       ("convenio_informado", e.get("convenio"))) if v}
                if campos:
                    atualizar(campos)
                    lead.update(campos)
                saida = {"ok": True}
            elif c.name in ("listar_horarios_disponiveis", "marcar_primeira_consulta") and com_agenda:
                try:
                    if c.name == "listar_horarios_disponiveis":
                        saida = agenda_para(lead.get("nome") or "Novo paciente", "").ver_horarios_livres(
                            e.get("a_partir_de"), e.get("periodo", "qualquer"))
                        if saida.get("horarios"):
                            atualizar({"status": "aguardando_escolha_horario"})
                    else:
                        nome = (e.get("nome") or "").strip()
                        if len(nome.split()) < 2:
                            saida, erro = {"marcado": False, "erro": "peça o nome completo antes de marcar"}, True
                        else:
                            saida = agenda_para(nome, e.get("convenio_usado", "")).marcar_consulta(e.get("data_hora_iso", ""))
                            if saida.get("marcado"):
                                r.convertido = {"nome": nome, "descricao": saida["descricao"], "inicio": saida["inicio"],
                                                "convenio": e.get("convenio_usado", "")}
                except Exception as ex:  # Google/banco fora do ar
                    log.warning("lead: agenda indisponível: %s", type(ex).__name__)
                    saida, erro = {"erro": "agenda indisponível agora; encaminhe para a nutricionista"}, True
            else:
                saida, erro = {"erro": f"ferramenta desconhecida: {c.name}"}, True
            resultados.append({"type": "tool_result", "tool_use_id": c.id,
                               "content": json.dumps(saida, ensure_ascii=False, default=str), "is_error": erro})
        mensagens.append({"role": "user", "content": resultados})
    return encaminhar_automatico(f"sem resposta final em {MAX_PASSOS} passos")
