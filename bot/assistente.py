"""Laço do bot do paciente.

O modelo sempre termina chamando `enviar_resposta` com acao = responder | encaminhar.
Pode antes chamar `consultar_taco` (valores nutricionais). Até MAX_PASSOS chamadas;
se não chegar a uma resposta válida, ou se a API falhar, encaminha para a nutri.

Uso:
    from bot.assistente import responder
    r = responder("Posso trocar o arroz?", paciente, plano, taco=TacoJSON("dados/taco.json"))
    r.acao, r.mensagem_paciente, r.resumo_para_nutri
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import anthropic

from bot import avisos
from bot.anamnese import conflitos, mencoes_de_risco, normalizar, texto_para_prompt
from bot.plano import resumo_do_plano
from bot.taco import FonteTaco, consultar_taco

log = logging.getLogger(__name__)

MODELO_PADRAO = "claude-haiku-4-5"
FUSO_PADRAO = "America/Sao_Paulo"
MAX_PASSOS = 6
MAX_TOKENS = 1024
ARQUIVO_REGRAS = Path(__file__).with_name("regras_clinicas.md")

# Texto provisório — a Ray revisa (tom e conteúdo são decisão dela).
MENSAGEM_ENCAMINHAMENTO_PADRAO = (
    "Oi, {primeiro_nome}! Vou passar sua mensagem para a nutricionista {nome_nutri}, "
    "e ela te responde assim que puder."
)

FERRAMENTAS = [
    {
        "name": "consultar_taco",
        "description": (
            "Consulta a Tabela Brasileira de Composição de Alimentos (TACO) e devolve energia, "
            "proteína, carboidrato, gorduras e fibra para a quantidade pedida. Use sempre que o "
            "paciente perguntar valor nutricional; nunca informe número que não veio desta ferramenta."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "alimento": {"type": "string", "description": (
                    "Nome do alimento com tipo, parte e preparo, ex.: 'arroz integral cozido', "
                    "'peito de frango grelhado', 'patinho grelhado'. Uma chamada por alimento.")},
                "gramas": {"type": "number", "description": "Quantidade em gramas. Use 100 se o paciente não disser."},
            },
            "required": ["alimento", "gramas"],
            "additionalProperties": False,
        },
    },
    {
        "name": "resumo_do_plano",
        "description": (
            "Soma, com valores da TACO, quanto cada refeição do plano deste paciente representa no dia "
            "(calorias, proteína, carboidrato, gordura e percentual do dia). Use quando o paciente perguntar "
            "o que acontece se pular ou trocar uma refeição, ou quanto uma refeição do plano tem."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "enviar_resposta",
        "description": (
            "Envia a resposta final. Toda conversa termina com esta ferramenta. "
            "acao='responder' quando a dúvida está coberta pelas regras e pelo plano; "
            "acao='encaminhar' quando a regra manda encaminhar ou há qualquer incerteza."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "acao": {"type": "string", "enum": ["responder", "encaminhar"]},
                "mensagem_paciente": {
                    "type": "string",
                    "description": "Texto que o paciente recebe no WhatsApp.",
                },
                "motivo": {
                    "type": "string",
                    "description": "Por que encaminhou (obrigatório se acao='encaminhar'; vazio se responder).",
                },
                "resumo_para_nutri": {
                    "type": "string",
                    "description": ("Obrigatório se acao='encaminhar'. UMA frase (até ~150 caracteres) dizendo o que o paciente "
                                    "quer ou relatou, como numa anotação: sem nome, telefone, saudação ou 'o paciente "
                                    "perguntou'. Ex.: 'Quer trocar o arroz do jantar por batata-doce.' / 'Tontura e enjoo "
                                    "desde ontem; começou remédio para pressão.'"),
                },
                "urgente": {
                    "type": "boolean",
                    "description": ("true só em assunto de risco à saúde (sintoma, mal-estar, gestação, medicamento novo, "
                                    "transtorno alimentar, emergência); a nutricionista recebe o aviso em vermelho. "
                                    "false em dúvidas comuns e quando acao='responder'."),
                },
                "alimentos_sugeridos": {
                    "type": "array", "items": {"type": "string"},
                    "description": ("Todo alimento que a mensagem sugere, libera ou oferece como troca (vazio se nenhum). "
                                    "O sistema confere com a anamnese do paciente."),
                },
            },
            "required": ["acao", "mensagem_paciente", "motivo", "resumo_para_nutri", "urgente", "alimentos_sugeridos"],
            "additionalProperties": False,
        },
    },
]

FERRAMENTAS_AGENDA = [
    {
        "name": "ver_horarios_livres",
        "description": (
            "Horários livres para consulta com a nutricionista, já cruzados com a agenda dela. "
            "Use quando o paciente quiser marcar ou perguntar por horários."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "a_partir_de": {"type": "string", "description": "Data AAAA-MM-DD a partir da qual procurar (opcional)."},
                "periodo": {"type": "string", "enum": ["qualquer", "manha", "tarde", "noite"]},
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "marcar_consulta",
        "description": (
            "Marca a consulta na agenda da nutricionista. Só chame depois que o paciente escolheu, "
            "com clareza, um dos horários que vieram de ver_horarios_livres."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"inicio": {"type": "string", "description": "Campo 'inicio' do horário escolhido, AAAA-MM-DDTHH:MM."}},
            "required": ["inicio"],
            "additionalProperties": False,
        },
    },
    {
        "name": "minhas_consultas",
        "description": "Próximas consultas deste paciente marcadas pelo assistente (com agendamento_id). Use antes de remarcar ou cancelar.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "remarcar_consulta",
        "description": (
            "Troca uma consulta de horário: marca o novo e cancela o antigo. Só chame depois que o paciente "
            "escolheu, com clareza, um novo horário que veio de ver_horarios_livres."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agendamento_id": {"type": "string", "description": "agendamento_id da consulta atual (de minhas_consultas)."},
                "novo_inicio": {"type": "string", "description": "Campo 'inicio' do novo horário escolhido, AAAA-MM-DDTHH:MM."},
            },
            "required": ["agendamento_id", "novo_inicio"],
            "additionalProperties": False,
        },
    },
    {
        "name": "cancelar_consulta",
        "description": "Cancela uma consulta. Só chame depois que o paciente confirmou claramente que quer cancelar.",
        "input_schema": {
            "type": "object",
            "properties": {"agendamento_id": {"type": "string", "description": "agendamento_id (de minhas_consultas)."}},
            "required": ["agendamento_id"],
            "additionalProperties": False,
        },
    },
]

NOMES_AGENDA = {f["name"] for f in FERRAMENTAS_AGENDA}

INSTRUCOES_AGENDA = """
## Agenda

- Para marcar consulta, chame `ver_horarios_livres` e ofereça até 3 opções, com dia da semana, data e hora.
  Se o paciente disser um período ou dia ("de manhã", "semana que vem", "sexta"), use os parâmetros da ferramenta.
- Só chame `marcar_consulta` quando o paciente tiver escolhido um horário claramente. Nunca invente horário.
- Depois de marcar, confirme dia e hora ao paciente (acao='responder'); a nutricionista é avisada automaticamente.
- Se a ferramenta disser que o horário não está mais livre, peça desculpas e ofereça outros.
- Remarcar: chame `minhas_consultas`, confirme qual consulta é (se houver mais de uma), chame `ver_horarios_livres`,
  ofereça até 3 opções e, quando o paciente escolher, chame `remarcar_consulta`. Confirme o horário antigo e o novo.
- Cancelar: chame `minhas_consultas`, pergunte se ele confirma o cancelamento daquela consulta (dia e hora) e só
  depois da confirmação chame `cancelar_consulta`. Ofereça remarcar em vez de cancelar, sem insistir.
- Se `minhas_consultas` não achar a consulta que o paciente cita, ela foi marcada pela nutricionista sem o telefone dele:
  encaminhe (acao='encaminhar') dizendo o que o paciente quer.
- Marcar, remarcar e cancelar pelo assistente: acao='responder' (a nutricionista é avisada automaticamente).
"""

SO_AGENDA = {
    "sem_acompanhamento": (
        "Este paciente fez a consulta simples, sem o acompanhamento entre consultas. Você cuida só da agenda "
        "(marcar, remarcar, cancelar, ver consultas). Para qualquer dúvida de alimentação, do plano ou de saúde, use "
        "acao='encaminhar' e responda nesta linha, sem responder a dúvida: \"Tirar dúvidas pelo WhatsApp entre as "
        "consultas faz parte do acompanhamento. Já passei sua mensagem para a nutricionista {nutri}. Se quiser, vejo "
        "um horário de retorno para você.\""),
    "sem_plano": (
        "Este paciente ainda não tem plano alimentar atualizado no sistema. Você cuida só da agenda (marcar, remarcar, "
        "cancelar, ver consultas). Para qualquer dúvida de alimentação ou de saúde, use acao='encaminhar' e diga que "
        "a nutricionista {nutri} vai responder; se fizer sentido, ofereça ver um horário de consulta."),
}


def instrucoes_so_agenda(motivo: str, nome_nutri: str) -> str:
    return ("\n## Modo só agenda (importante)\n\n" + SO_AGENDA[motivo].format(nutri=nome_nutri)
            + " Nunca sugira alimento, troca, porção ou valor nutricional; `alimentos_sugeridos` fica vazio.\n")


INSTRUCOES_FERRAMENTAS = """
## Como responder

- Responda sempre chamando a ferramenta `enviar_resposta`. Não escreva texto fora dela.
- Para qualquer número de valor nutricional, chame antes `consultar_taco` e use só os valores que ela devolver.
  Diga que os valores são da TACO. Se o alimento encontrado for diferente do que o paciente citou, diga qual usou.
- Se a ferramenta disser "traço", "menos de", "não se aplica" ou "sem dado", repita isso; nunca troque por zero
  nem diga que o alimento "não tem" o nutriente.
- Chame o alimento pelo nome que veio em `alimento_taco`. Se `correspondencia` for "aproximada",
  diga ao paciente qual alimento da TACO foi usado (ex.: "a TACO não tem patinho moído cozido; usei patinho grelhado").
- Para saber quais refeições do dia já passaram e quais faltam, use a lista pronta no fim deste texto (calculada
  pela hora atual e pelos horários do plano). Não cite refeição que não está no plano. Se não der para saber
  o que o paciente já comeu, pergunte em vez de supor.
- Ao citar item do plano, use a quantidade que o plano traz. Só diga "à vontade" se o plano disser isso daquele item.
- Formatação do WhatsApp: negrito com *um asterisco*; sem títulos com #.
- Antes de encaminhar, confira: a resposta está no plano ou na TACO? Se estiver, responda.
  Alimento fora do plano não é motivo para encaminhar pergunta de valor nutricional.
- Ao encaminhar, preencha `motivo`, `resumo_para_nutri` (uma frase de anotação, pronta para a nutricionista ler no
  celular) e `urgente` (true só em assunto de risco). Em assunto de risco (sintomas, gestação, doenças, exames,
  medicamentos, suplementos, fitoterápicos, transtorno alimentar, emergência), a `mensagem_paciente` acolhe, dá a
  orientação segura da tabela das regras (e nada além dela) e diz que a nutricionista foi avisada. Nos outros casos, a
  `mensagem_paciente` primeiro ajuda de forma concreta com o plano e a TACO e só no fim diz que a nutricionista foi avisada.
- Se só uma parte da mensagem precisa da nutricionista, use acao='encaminhar', mas escreva na `mensagem_paciente`
  a resposta das outras partes e diga qual parte a nutricionista vai responder.
"""


_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]️?")


def formatar_whatsapp(texto: str) -> str:
    """Markdown -> WhatsApp: **negrito** vira *negrito*; tira # de títulos."""
    texto = re.sub(r"\*\*(.+?)\*\*", r"*\1*", texto)
    texto = re.sub(r"^#{1,6}\s*", "", texto, flags=re.M)
    return texto


def ajustar_emojis(resposta: str, mensagem_paciente: str) -> str:
    """Regra da Ray: no máximo um emoji, e só se o paciente usou emoji."""
    limite = 1 if _EMOJI.search(mensagem_paciente) else 0
    contagem = 0

    def trocar(m: re.Match) -> str:
        nonlocal contagem
        contagem += 1
        return m.group(0) if contagem <= limite else ""

    texto = _EMOJI.sub(trocar, resposta)
    return re.sub(r"[ \t]{2,}", " ", texto).replace(" !", "!").strip()


class SemConsentimento(Exception):
    """O paciente não registrou consentimento: o bot não pode responder."""


@dataclass
class Resultado:
    acao: str
    mensagem_paciente: str
    motivo: str = ""
    resumo_para_nutri: str = ""
    urgente: bool = False  # risco à saúde: o aviso para a nutri sai em vermelho
    passos: int = 0
    tokens_entrada: int = 0
    tokens_saida: int = 0
    tokens_cache_lidos: int = 0
    consultas_taco: list[dict] = field(default_factory=list)
    fallback: bool = False  # True quando o encaminhamento foi automático (erro ou sem resposta)
    agendamento: dict | None = None  # consulta marcada nesta mensagem (o webhook avisa a nutri)
    bloqueios_anamnese: int = 0  # respostas recusadas por conflitar com a anamnese


def carregar_regras(nome_nutri: str) -> str:
    texto = ARQUIVO_REGRAS.read_text(encoding="utf-8")
    texto = texto.split("-->", 1)[-1].strip()  # tira o comentário de cabeçalho
    return texto.replace("{nome_nutri}", nome_nutri)


def refeicoes_do_dia(plano: dict, agora: datetime) -> str:
    """Texto com as refeições do plano que já passaram e as que faltam, pelo horário de cada uma."""
    passaram, faltam = [], []
    for ref in plano.get("refeicoes", []):
        horario = str(ref.get("horario") or "")
        try:
            h, m = (int(x) for x in horario.split(":"))
        except ValueError:
            continue
        rotulo = f"{ref.get('nome', '')} ({horario})"
        (passaram if (h, m) <= (agora.hour, agora.minute) else faltam).append(rotulo)
    if not passaram and not faltam:
        return ""
    if agora.hour < 4:  # madrugada: "hoje" do paciente costuma ser o dia que acabou
        return ("É madrugada: se o paciente falar de \"hoje\", ele provavelmente se refere ao dia que acabou, "
                "em que todas as refeições do plano já passaram; na dúvida, pergunte.")
    return ("Pelo horário (isso não quer dizer que o paciente comeu), refeições do plano que já passaram hoje: "
            + (", ".join(passaram) or "nenhuma") + ". Que ainda faltam hoje: " + (", ".join(faltam) or "nenhuma") + ".")


def montar_system(paciente: dict, plano: dict, nome_nutri: str, com_agenda: bool = False,
                  agora: datetime | None = None, somente_agenda: str | None = None) -> list[dict]:
    """Parte fixa (regras + plano) com cache; data e hora no fim, fora do cache.

    agora: momento atual no fuso da nutri (o servidor roda em UTC)."""
    plano_texto = json.dumps(plano, ensure_ascii=False, indent=1)
    agora = agora or datetime.now(ZoneInfo(FUSO_PADRAO))
    dia_semana = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][agora.isoweekday() - 1]
    return [
        {"type": "text", "text": carregar_regras(nome_nutri) + "\n" + INSTRUCOES_FERRAMENTAS
                                 + (INSTRUCOES_AGENDA if com_agenda else "")
                                 + (instrucoes_so_agenda(somente_agenda, nome_nutri) if somente_agenda else "")},
        {
            "type": "text",
            "text": f"## Plano alimentar de {paciente['primeiro_nome']}\n\n```json\n{plano_texto}\n```\n\n"
                    + texto_para_prompt(paciente.get("anamnese"), plano, paciente["primeiro_nome"]),
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": f"Agora é {dia_semana}, {agora:%d/%m/%Y}, {agora:%H:%M} (horário de Brasília). "
                                 f"{refeicoes_do_dia(plano, agora)} "
                                 f"Primeiro nome do paciente: {paciente['primeiro_nome']}."},
    ]


def _validar_resposta(entrada: dict) -> str | None:
    """Retorna o erro, ou None se a resposta final está válida."""
    acao = entrada.get("acao")
    if acao not in ("responder", "encaminhar"):
        return "acao precisa ser 'responder' ou 'encaminhar'"
    if not str(entrada.get("mensagem_paciente") or "").strip():
        return "mensagem_paciente não pode ficar vazia"
    if acao == "encaminhar" and not (
        str(entrada.get("motivo") or "").strip() and str(entrada.get("resumo_para_nutri") or "").strip()
    ):
        return "ao encaminhar, preencha motivo e resumo_para_nutri"
    return None


def responder(
    pergunta: str,
    paciente: dict,
    plano: dict,
    *,
    taco: FonteTaco,
    nome_nutri: str | None = None,
    historico: list[dict] | None = None,
    cliente: anthropic.Anthropic | None = None,
    modelo: str | None = None,
    agenda=None,
    agora: datetime | None = None,
    somente_agenda: str | None = None,
) -> Resultado:
    """Responde uma mensagem do paciente.

    paciente: precisa de 'primeiro_nome' e 'consentimento_em' (sem consentimento -> SemConsentimento).
    historico: mensagens anteriores [{"role": "user"|"assistant", "content": "texto"}], da mais antiga para a mais nova.
    agenda: agenda.servico.AgendaNutri da nutri (None = sem agenda; o bot encaminha pedidos de consulta).
    somente_agenda: 'sem_acompanhamento' | 'sem_plano' = o bot só cuida da agenda e encaminha o resto
        (sem ferramentas de TACO/plano; sugerir alimento é barrado no código).
    """
    if not paciente.get("consentimento_em"):
        raise SemConsentimento("paciente sem consentimento registrado")

    nome_nutri = nome_nutri or plano.get("nutricionista") or "sua nutricionista"
    cliente = cliente or anthropic.Anthropic()
    modelo = modelo or os.environ.get("MODELO_BOT") or MODELO_PADRAO
    # só agenda: o modelo nem recebe o plano (não tem como orientar alimentação)
    system = montar_system(paciente, {"refeicoes": []} if somente_agenda else plano, nome_nutri,
                           com_agenda=agenda is not None, agora=agora, somente_agenda=somente_agenda)
    base = [f for f in FERRAMENTAS if f["name"] == "enviar_resposta"] if somente_agenda else FERRAMENTAS
    ferramentas = base + (FERRAMENTAS_AGENDA if agenda is not None else [])
    mensagens = [*(historico or []), {"role": "user", "content": pergunta}]
    r = Resultado(acao="encaminhar", mensagem_paciente="")
    anamnese = paciente.get("anamnese")
    mencoes_confirmadas: set[str] = set()

    def conferir_anamnese(entrada: dict) -> str | None:
        """Barreira em código: alimento sugerido em conflito é recusado; citar um alergênico exige confirmação."""
        sugeridos = [str(x) for x in (entrada.get("alimentos_sugeridos") or []) if str(x).strip()]
        if somente_agenda and sugeridos:
            return ("neste paciente você só cuida da agenda: não sugira alimentos. Encaminhe a dúvida para a "
                    "nutricionista (acao='encaminhar') e deixe alimentos_sugeridos vazio")
        pedidos_pelo_paciente = normalizar(pergunta)
        # "não gosta" é preferência: se o próprio paciente pediu o alimento, o bot pode liberar
        choques = [c for c in conflitos(sugeridos, anamnese)
                   if not (c["categoria"] == "não gosta" and normalizar(c["item"]) in pedidos_pelo_paciente)]
        if choques:
            r.bloqueios_anamnese += 1
            log.warning("bot: sugestão barrada pela anamnese (%d item(ns))", len(choques))
            itens = "; ".join(f"'{c['alimento']}' ({c['categoria']}: {c['item']})" for c in choques)
            return (f"a anamnese do paciente proíbe {itens}. Tire da mensagem e de alimentos_sugeridos e ofereça "
                    "outra opção segura do plano; se não houver, encaminhe")
        novas = [m for m in mencoes_de_risco(entrada.get("mensagem_paciente", ""), anamnese) if m not in mencoes_confirmadas]
        if novas:
            mencoes_confirmadas.update(novas)
            return (f"a mensagem cita {', '.join(novas)}, que está nas alergias/intolerâncias do paciente. Confira que "
                    "você NÃO está sugerindo isso (só avisando para evitar). Se estiver certo, envie de novo igual; "
                    "se não, corrija")
        return None

    def encaminhar_automatico(motivo: str) -> Resultado:
        r.acao = "encaminhar"
        r.mensagem_paciente = MENSAGEM_ENCAMINHAMENTO_PADRAO.format(
            primeiro_nome=paciente["primeiro_nome"], nome_nutri=nome_nutri
        )
        r.motivo = motivo
        r.resumo_para_nutri = avisos.resumo_sem_resposta(pergunta)
        r.fallback = True
        log.warning("bot encaminhou automaticamente: %s (passos=%d)", motivo, r.passos)
        return r

    for _ in range(MAX_PASSOS):
        r.passos += 1
        try:
            resposta = cliente.messages.create(
                model=modelo,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=ferramentas,
                tool_choice={"type": "any"},
                messages=mensagens,
            )
        except anthropic.APIError as e:
            return encaminhar_automatico(f"erro na API: {type(e).__name__}")

        uso = resposta.usage
        r.tokens_entrada += uso.input_tokens
        r.tokens_saida += uso.output_tokens
        r.tokens_cache_lidos += getattr(uso, "cache_read_input_tokens", 0) or 0

        chamadas = [b for b in resposta.content if b.type == "tool_use"]
        if not chamadas:
            return encaminhar_automatico(f"modelo não chamou ferramenta (stop_reason={resposta.stop_reason})")

        mensagens.append({"role": "assistant", "content": resposta.content})
        resultados = []
        for chamada in chamadas:
            entrada = chamada.input if isinstance(chamada.input, dict) else {}
            if chamada.name == "enviar_resposta":
                erro = _validar_resposta(entrada) or conferir_anamnese(entrada)
                if erro is None:
                    r.acao = entrada["acao"]
                    r.mensagem_paciente = formatar_whatsapp(ajustar_emojis(entrada["mensagem_paciente"], pergunta))
                    r.motivo = (entrada.get("motivo") or "").strip()
                    r.resumo_para_nutri = (entrada.get("resumo_para_nutri") or "").strip()
                    r.urgente = r.acao == "encaminhar" and bool(entrada.get("urgente"))
                    log.info("bot: acao=%s passos=%d", r.acao, r.passos)
                    return r
                resultados.append({"type": "tool_result", "tool_use_id": chamada.id,
                                   "content": f"Resposta inválida: {erro}. Corrija e chame enviar_resposta de novo.",
                                   "is_error": True})
            elif chamada.name == "resumo_do_plano":
                saida = resumo_do_plano(plano, taco)
                r.consultas_taco.append({"entrada": {"alimento": "(resumo do plano)", "gramas": "-"},
                                         "saida": {"alimento_taco": "resumo do plano"}})
                resultados.append({"type": "tool_result", "tool_use_id": chamada.id,
                                   "content": json.dumps(saida, ensure_ascii=False)})
            elif chamada.name in NOMES_AGENDA and agenda is not None:
                try:
                    if chamada.name == "ver_horarios_livres":
                        saida = agenda.ver_horarios_livres(entrada.get("a_partir_de"), entrada.get("periodo", "qualquer"))
                    elif chamada.name == "minhas_consultas":
                        saida = agenda.minhas_consultas()
                    elif chamada.name == "remarcar_consulta":
                        saida = agenda.remarcar_consulta(entrada.get("agendamento_id", ""), entrada.get("novo_inicio", ""))
                        if saida.get("remarcado"):
                            r.agendamento = {**saida, "tipo": "remarcado", "descricao": saida["para"]}
                    elif chamada.name == "cancelar_consulta":
                        saida = agenda.cancelar_consulta(entrada.get("agendamento_id", ""))
                        if saida.get("cancelado"):
                            r.agendamento = {**saida, "tipo": "cancelado"}
                    else:
                        saida = agenda.marcar_consulta(entrada.get("inicio", ""))
                        if saida.get("marcado"):
                            r.agendamento = {**saida, "tipo": "marcado"}
                    erro_agenda = False
                except Exception as e:  # Google fora do ar, sem permissão etc.
                    log.warning("agenda indisponível: %s", type(e).__name__)
                    saida, erro_agenda = {"erro": "agenda indisponível agora; encaminhe o pedido para a nutricionista"}, True
                resultados.append({"type": "tool_result", "tool_use_id": chamada.id,
                                   "content": json.dumps(saida, ensure_ascii=False), "is_error": erro_agenda})
            elif chamada.name == "consultar_taco":
                saida = consultar_taco(taco, entrada.get("alimento", ""), entrada.get("gramas", 100))
                r.consultas_taco.append({"entrada": entrada, "saida": saida})
                resultados.append({"type": "tool_result", "tool_use_id": chamada.id,
                                   "content": json.dumps(saida, ensure_ascii=False)})
            else:
                resultados.append({"type": "tool_result", "tool_use_id": chamada.id,
                                   "content": f"Ferramenta desconhecida: {chamada.name}", "is_error": True})
        mensagens.append({"role": "user", "content": resultados})

    return encaminhar_automatico(f"sem resposta final em {MAX_PASSOS} passos")
