"""Check-in semanal do acompanhamento: pergunta, lê a resposta livre do paciente e alerta a nutri.

Fluxo
  1. No dia/hora da nutri (checkin_config), cada paciente no acompanhamento recebe as perguntas.
  2. A resposta (texto livre, uma ou várias mensagens) é lida pelo Claude Haiku numa ferramenta com
     schema: adesão 0-10, fome, intestino, peso, comentário, sinais de alerta.
  3. Faltou algo essencial: o assistente pede uma vez. Depois agradece e grava.
  4. Alerta (sintoma, mal-estar, variação de peso >= 2 kg na semana) avisa a nutri na hora.
Se a mensagem não for resposta do check-in (ex.: dúvida do plano), segue para o bot normal; se tiver
resposta E dúvida, grava a resposta e a dúvida também vai para o bot.
Textos provisórios: a Ray revisa. O check-in NÃO orienta: só registra, agradece e avisa a nutri.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Protocol

import anthropic

from bot import avisos

log = logging.getLogger("checkin")

MODELO_PADRAO = "claude-haiku-4-5"
ESSENCIAIS = ("adesao", "fome", "intestino")
ROTULOS = {"adesao": "quanto seguiu o plano (0 a 10)", "fome": "como esteve a fome", "intestino": "como está o intestino"}
VARIACAO_PESO_ALERTA = 2.0  # kg em uma semana

PERGUNTA = ("Oi, {nome}! Hora do check-in da semana. Me conta rapidinho:\n"
            "1. De 0 a 10, quanto você conseguiu seguir o plano?\n"
            "2. Como esteve a fome? (pouca, normal ou muita)\n"
            "3. E o intestino? (normal, preso ou solto)\n"
            "4. Se tiver se pesado, qual foi o peso?\n"
            "Pode responder tudo numa mensagem só. A nutricionista {nutri} acompanha suas respostas.")
FALTOU = "Obrigado, {nome}! Só faltou me dizer {itens}. Pode me contar?"
OBRIGADO = "Obrigado, {nome}! Anotei tudo para a nutricionista {nutri}."
ANIMO_ALTO = " Ótima semana, continue assim!"
ANIMO_BAIXO = " Semanas assim acontecem. Se quiser, me conta o que dificultou que eu passo para ela."
ALERTA_PACIENTE = ("Obrigado por contar, {nome}. Já avisei a nutricionista {nutri} para olhar isso com atenção. "
                   "Se for algo urgente, como falta de ar, dor forte ou desmaio, ligue 192 (SAMU).")


def texto_pergunta(nome: str, nutri: str) -> str:
    return PERGUNTA.format(nome=nome, nutri=nutri)


def resumo_respostas(c: dict) -> str:
    partes = [f"adesão {c['adesao']}/10" if c.get("adesao") is not None else "adesão ?",
              f"fome {c.get('fome') or '?'}", f"intestino {c.get('intestino') or '?'}"]
    if c.get("peso_kg") is not None:
        partes.append(f"peso {float(c['peso_kg']):.1f} kg".replace(".", ","))
    if c.get("comentario"):
        partes.append(f"disse: \"{c['comentario']}\"")
    return " · ".join(partes)


# ---------------------------------------------------------------------
# Leitura da resposta
# ---------------------------------------------------------------------
INSTRUCOES = """Você lê a resposta de um paciente ao check-in semanal da nutricionista. As perguntas foram:
1) de 0 a 10, quanto seguiu o plano alimentar; 2) fome (pouca, normal ou muita); 3) intestino (normal, preso ou solto);
4) peso, se tiver se pesado. Chame a ferramenta `registrar_checkin`.

Regras:
- `eh_resposta_checkin` = true se a mensagem responde qualquer uma das perguntas, mesmo em parte. Se for só uma
  dúvida ou outro assunto (ex.: "posso trocar o arroz?", "quero remarcar"), false.
- Preencha só o que o paciente disse. Não invente. Campo não respondido = null.
- Adesão: número de 0 a 10. O paciente costuma responder na ordem das perguntas: um número de 0 a 10 no começo
  da resposta (ex.: "7.", "8,", "uns 6") é a adesão. Se ele descrever em palavras, converta só quando for claro: "segui tudo" = 10,
  "quase tudo" = 8, "metade" = 5, "pouco" = 2, "nada" = 0.
- Fome e intestino: normalize para as opções (ex.: "com muita fome" = muita; "funcionando bem" = normal;
  "preso" / "constipado" = preso; "solto" / "diarreia" = solto).
- Peso em kg (ex.: "72,5" = 72.5). Ignore números que não sejam peso.
- `comentario`: algo relevante que o paciente contou (curto), ou vazio.
- `alerta` = true se ele citar sintoma ou mal-estar (dor, tontura, desmaio, vômito, diarreia forte, sangue,
  falta de ar), gravidez, uso de remédio novo, compulsão, vontade de vomitar após comer ou restrição extrema.
  Explique em `alerta_motivo` numa frase curta, do jeito que a nutricionista anotaria (ex.: "relata tontura e
  vômito desde terça"; "começou remédio novo para pressão"). Adesão baixa sozinha NÃO é alerta.
- `pergunta_extra`: se além de responder ele fizer uma pergunta ou pedido, copie a pergunta; senão vazio.
"""

FERRAMENTA = {
    "name": "registrar_checkin",
    "description": "Registra as respostas do check-in semanal.",
    "input_schema": {
        "type": "object",
        "properties": {
            "eh_resposta_checkin": {"type": "boolean"},
            "adesao": {"type": ["integer", "null"], "minimum": 0, "maximum": 10},
            "fome": {"type": ["string", "null"], "enum": ["pouca", "normal", "muita", None]},
            "intestino": {"type": ["string", "null"], "enum": ["normal", "preso", "solto", None]},
            "peso_kg": {"type": ["number", "null"]},
            "comentario": {"type": "string"},
            "alerta": {"type": "boolean"},
            "alerta_motivo": {"type": "string"},
            "pergunta_extra": {"type": "string"},
        },
        "required": ["eh_resposta_checkin", "adesao", "fome", "intestino", "peso_kg", "comentario", "alerta",
                     "alerta_motivo", "pergunta_extra"],
        "additionalProperties": False,
    },
}


@dataclass
class Leitura:
    eh_resposta: bool
    campos: dict = field(default_factory=dict)  # só o que veio preenchido
    alerta: bool = False
    alerta_motivo: str = ""
    pergunta_extra: str = ""


def interpretar(texto: str, *, cliente: anthropic.Anthropic | None = None, modelo: str | None = None) -> Leitura:
    cliente = cliente or anthropic.Anthropic()
    modelo = modelo or os.environ.get("MODELO_BOT") or MODELO_PADRAO
    r = cliente.messages.create(model=modelo, max_tokens=400, system=INSTRUCOES, tools=[FERRAMENTA],
                                tool_choice={"type": "tool", "name": "registrar_checkin"},
                                messages=[{"role": "user", "content": texto}])
    chamada = next((b for b in r.content if b.type == "tool_use"), None)
    e = chamada.input if chamada is not None and isinstance(chamada.input, dict) else {}
    campos = {}
    if isinstance(e.get("adesao"), (int, float)) and 0 <= e["adesao"] <= 10:
        campos["adesao"] = int(e["adesao"])
    if e.get("fome") in ("pouca", "normal", "muita"):
        campos["fome"] = e["fome"]
    if e.get("intestino") in ("normal", "preso", "solto"):
        campos["intestino"] = e["intestino"]
    if isinstance(e.get("peso_kg"), (int, float)) and 20 <= e["peso_kg"] <= 350:
        campos["peso_kg"] = round(float(e["peso_kg"]), 1)
    if str(e.get("comentario") or "").strip():
        campos["comentario"] = str(e["comentario"]).strip()[:300]
    return Leitura(eh_resposta=bool(e.get("eh_resposta_checkin")), campos=campos, alerta=bool(e.get("alerta")),
                   alerta_motivo=str(e.get("alerta_motivo") or "").strip()[:200],
                   pergunta_extra=str(e.get("pergunta_extra") or "").strip())


# ---------------------------------------------------------------------
# Decisão (pura: fácil de testar)
# ---------------------------------------------------------------------
@dataclass
class Decisao:
    salvar: dict                  # campos para gravar no check-in (inclui status)
    mensagem: str                 # para o paciente
    aviso_nutri: str | None       # alerta para a nutri
    seguir_para_bot: str | None   # pergunta que o bot normal deve responder


def decidir(leitura: Leitura, checkin: dict, peso_anterior: float | None, nome: str, nutri: str,
            telefone: str, nome_completo: str) -> Decisao:
    atual = {**{k: checkin.get(k) for k in (*ESSENCIAIS, "peso_kg", "comentario")},
             **{k: v for k, v in leitura.campos.items() if v is not None}}
    if leitura.campos.get("comentario") and checkin.get("comentario"):
        atual["comentario"] = f"{checkin['comentario']} / {leitura.campos['comentario']}"[:300]
    motivos = []
    if leitura.alerta:
        motivos.append(leitura.alerta_motivo or "o paciente relatou algo que pede atenção")
    peso = atual.get("peso_kg")
    if peso is not None and peso_anterior is not None and abs(float(peso) - float(peso_anterior)) >= VARIACAO_PESO_ALERTA:
        motivos.append(f"peso variou {float(peso) - float(peso_anterior):+.1f} kg desde o último check-in".replace(".", ","))
    alerta = bool(motivos) or bool(checkin.get("alerta"))
    faltando = [k for k in ESSENCIAIS if atual.get(k) is None]
    salvar = {k: atual.get(k) for k in (*ESSENCIAIS, "peso_kg", "comentario")}
    salvar["alerta"] = alerta
    if motivos:
        salvar["alerta_motivo"] = "; ".join(motivos)
    aviso = avisos.checkin(nome_completo, telefone, motivos, resumo_respostas(atual)) if motivos else None
    extra = leitura.pergunta_extra or None
    if motivos:
        salvar.update(status="respondido")
        return Decisao(salvar, ALERTA_PACIENTE.format(nome=nome, nutri=nutri), aviso, extra)
    if faltando and not checkin.get("lembrete_enviado"):
        itens = [ROTULOS[k] for k in faltando]
        texto_itens = itens[0] if len(itens) == 1 else ", ".join(itens[:-1]) + " e " + itens[-1]
        salvar.update(status="enviado", lembrete_enviado=True)
        return Decisao(salvar, FALTOU.format(nome=nome, itens=texto_itens), None, extra)
    salvar.update(status="respondido")
    msg = OBRIGADO.format(nome=nome, nutri=nutri)
    adesao = atual.get("adesao")
    if adesao is not None and adesao >= 8:
        msg += ANIMO_ALTO
    elif adesao is not None and adesao <= 4:
        msg += ANIMO_BAIXO
    return Decisao(salvar, msg, None, extra)


# ---------------------------------------------------------------------
# Envio semanal
# ---------------------------------------------------------------------
class RepositorioCheckin(Protocol):
    def reservar_checkins(self) -> list[dict]: ...
    def desfazer_checkin(self, checkin_id: str) -> None: ...
    def registrar_proativa(self, nutri_id: str, paciente_id: str, texto: str, motivo: str, wa_id: str | None) -> None: ...


SQL_RESERVAR = """
with alvo as (
  select p.id as paciente_id, p.nutricionista_id, (now() at time zone n.fuso_horario)::date as semana
    from public.pacientes p
    join public.nutricionistas n on n.id = p.nutricionista_id
   where p.ativo and n.ativo and p.consentimento_em is not null and p.acompanhamento_desde is not null
     and (p.acompanhamento_ate is null or p.acompanhamento_ate >= (now() at time zone n.fuso_horario)::date)
     and extract(isodow from now() at time zone n.fuso_horario) = (n.checkin_config->>'dia')::int
     and (now() at time zone n.fuso_horario)::time >= (n.checkin_config->>'hora')::time
     and (now() at time zone n.fuso_horario)::time <  (n.checkin_config->>'hora')::time + interval '3 hours'
), novos as (
  insert into public.checkins (nutricionista_id, paciente_id, semana)
  select nutricionista_id, paciente_id, semana from alvo
  on conflict (paciente_id, semana) do nothing
  returning id, paciente_id, nutricionista_id
)
select novos.id::text as checkin_id, novos.paciente_id::text as paciente_id, novos.nutricionista_id::text as nutri_id,
       p.primeiro_nome, p.telefone, n.nome as nutri_nome
  from novos
  join public.pacientes p on p.id = novos.paciente_id
  join public.nutricionistas n on n.id = novos.nutricionista_id
"""


def rodar_checkins(repo: RepositorioCheckin, enviar: Callable[[str, str], "str | None"]) -> int:
    """Roda a cada 15 min; só envia na janela de 3 h depois do horário da nutri, uma vez por semana (unique)."""
    enviados = 0
    for c in repo.reservar_checkins():
        texto = texto_pergunta(c["primeiro_nome"], c["nutri_nome"])
        try:
            wa_id = enviar(c["telefone"], texto)
        except Exception as e:
            repo.desfazer_checkin(c["checkin_id"])
            log.warning("check-in não enviado (paciente=%s): %s", c["paciente_id"], type(e).__name__)
            continue
        repo.registrar_proativa(c["nutri_id"], c["paciente_id"], texto, "check-in semanal", wa_id)
        enviados += 1
    if enviados:
        log.info("check-ins enviados: %d", enviados)
    return enviados


def validar_config(dia: str, hora: str) -> tuple[dict | None, str | None]:
    """Config do painel: dia 1-7 e hora HH:MM até 20:59 (a janela de envio é de 3 h no mesmo dia)."""
    try:
        d = int(dia)
        h, m = (int(x) for x in (hora or "").split(":"))
    except ValueError:
        return None, "dia e hora do check-in inválidos"
    if not (1 <= d <= 7) or not (0 <= h <= 20 and 0 <= m <= 59):
        return None, "o check-in precisa ser entre 00:00 e 20:59"
    return {"dia": d, "hora": f"{h:02d}:{m:02d}"}, None

