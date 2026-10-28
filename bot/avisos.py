"""Avisos que o assistente manda para o WhatsApp da nutri. Todos no mesmo formato, para ler em 5 segundos:

    🔴 *Urgente · Maria Silva*          <- cor + o que é + quem (1ª linha já diz se precisa parar o que está fazendo)
    Relata tontura e enjoo desde ontem.  <- o que aconteceu, numa frase
    👉 Fale com ela hoje: wa.me/55...     <- o que fazer, com o link que abre a conversa

Cores: 🔴 urgente (risco à saúde) · 🟡 precisa da nutri (responder quando puder) · 📅/🆕/⛔ só informativo.
Textos provisórios: a Ray revisa.
"""

from __future__ import annotations

import re

LIMITE_RESUMO = 220
TIPOS_MIDIA = {"audio": "um áudio", "ptt": "um áudio", "imagem": "uma foto", "image": "uma foto", "video": "um vídeo",
               "documento": "um arquivo", "document": "um arquivo", "sticker": "uma figurinha", "figurinha": "uma figurinha",
               "localizacao": "uma localização", "contato": "um contato"}


def link_whatsapp(telefone: str) -> str:
    """wa.me/<só dígitos>: tocando, abre a conversa com o paciente."""
    return "wa.me/" + re.sub(r"\D", "", telefone or "")


def curto(texto: str, limite: int = LIMITE_RESUMO) -> str:
    """Uma linha só, sem espaços sobrando, cortada com reticências."""
    t = re.sub(r"\s+", " ", (texto or "").strip())
    if len(t) > limite:
        t = t[: limite - 1].rstrip(" ,.;:") + "…"
    if t and t[-1] not in ".!?…\"”":
        t += "."
    return t[:1].upper() + t[1:]


def _cabecalho(icone: str, titulo: str, nome: str) -> str:
    return f"{icone} *{titulo} · {nome}*"


def paciente_precisa(nome: str, telefone: str, resumo: str, urgente: bool = False, rascunho=None) -> str:
    """Dúvida encaminhada pelo bot (ou mensagem que ele não conseguiu responder).
    rascunho: bot.rascunho.Rascunho (sugestão de resposta pesquisada) ou None."""
    linhas = [_cabecalho("🔴", "Urgente", nome) if urgente else _cabecalho("🟡", "Responder", nome), curto(resumo)]
    if rascunho is not None and rascunho.resposta:
        linhas += ["", "💬 *Sugestão de resposta:*", f"\"{rascunho.resposta}\""]
        if rascunho.fontes:
            linhas.append("📚 " + " · ".join(_fonte(f) for f in rascunho.fontes))
        if rascunho.atencao:
            linhas.append(f"⚠️ {curto(rascunho.atencao, 160)}")
        linhas += ["", "👉 Responda *sim* para enviar, *não* para descartar, ou escreva a sua versão."]
        linhas.append(f"{'Fale com o paciente hoje' if urgente else 'Conversa direta'}: {link_whatsapp(telefone)}")
        return "\n".join(linhas)
    if rascunho is not None and rascunho.atencao:  # pesquisou, mas não há resposta segura para sugerir
        linhas.append(f"💬 Sem sugestão de resposta: {curto(rascunho.atencao, 180)}")
    linhas.append(f"👉 Fale com o paciente hoje: {link_whatsapp(telefone)}" if urgente
                  else f"👉 Responda quando puder: {link_whatsapp(telefone)}")
    return "\n".join(linhas)


def _fonte(f: dict) -> str:
    """'Título curto (pmc.ncbi.nlm.nih.gov/articles/PMC9033014)': dá para tocar e conferir."""
    titulo = f.get("titulo") or "fonte"
    if len(titulo) > 60:
        titulo = titulo[:59].rstrip(" ,:;-") + "…"
    url = re.sub(r"^https?://(www\.)?", "", f.get("url") or "").rstrip("/")
    return f"{titulo} ({url})" if url else titulo


# --- respostas para a nutri quando ela decide sobre um rascunho -----------------------------------
def rascunho_enviado(nome: str, editado: bool) -> str:
    return f"✅ {'Sua versão foi enviada' if editado else 'Enviado'} para {nome}."


def rascunho_descartado(nome: str, telefone: str) -> str:
    return f"🗑️ Sugestão para {nome} descartada. Para responder direto: {link_whatsapp(telefone)}"


def rascunho_ja_resolvido(nome: str) -> str:
    return f"Essa sugestão para {nome} já foi resolvida antes; nada foi enviado de novo."


def rascunho_expirado(nome: str, telefone: str) -> str:
    return f"⏰ A sugestão para {nome} tem mais de 48 h e não foi enviada. Se ainda fizer sentido: {link_whatsapp(telefone)}"


def rascunho_sem_texto(nome: str, telefone: str) -> str:
    return (f"Esse aviso de {nome} não tem sugestão pronta. Escreva a resposta citando o aviso, "
            f"ou fale direto: {link_whatsapp(telefone)}")


def rascunho_sem_consentimento(nome: str) -> str:
    return f"⛔ {nome} saiu do assistente (mandou PARAR): não posso enviar. Se precisar, fale direto com o paciente."


def confirmar_versao(nome: str, texto: str) -> str:
    return f"Vou enviar para {nome}:\n\"{texto.strip()}\"\n\nResponda *sim* para confirmar ou *não* para cancelar."


def rascunho_qual(nomes: list[str]) -> str:
    return ("Você tem mais de um aviso esperando resposta (" + ", ".join(nomes) + "). Para eu saber de quem é, "
            "responda *citando* o aviso (segure a mensagem e toque em Responder). Nada foi enviado.")


def mensagem_da_nutri(nutri: str, texto: str) -> str:
    """O que o paciente recebe: deixa claro que quem respondeu foi a nutricionista."""
    return f"*{nutri}:* {texto.strip()}"


def resumo_midia(tipo: str) -> str:
    return f"Mandou {TIPOS_MIDIA.get(tipo, 'uma mensagem que não é texto')}; o assistente só lê texto, então você precisa ver"


def resumo_sem_resposta(pergunta: str) -> str:
    """Quando a IA falhou: mostra a mensagem do paciente, sem jargão técnico."""
    return f"O assistente não conseguiu responder: \"{curto(pergunta, 160).rstrip('.')}\""


def checkin(nome: str, telefone: str, motivos: list[str], respostas: str) -> str:
    """Check-in semanal com sinal de alerta (sintoma, mal-estar, variação de peso)."""
    return "\n".join([_cabecalho("🔴", "Check-in pede atenção", nome),
                      curto("; ".join(motivos)),
                      f"Semana: {respostas}",
                      f"👉 Fale com o paciente hoje: {link_whatsapp(telefone)}"])


def agenda(nome: str, ag: dict) -> str:
    tipo = ag.get("tipo", "marcado")
    if tipo == "remarcado":
        return f"{_cabecalho('📅', 'Consulta remarcada', nome)}\n{ag['de']} → {ag['para']}. Sua agenda já foi atualizada."
    if tipo == "cancelado":
        return f"{_cabecalho('📅', 'Consulta cancelada', nome)}\n{ag['descricao']}. O horário foi liberado na sua agenda."
    return f"{_cabecalho('📅', 'Consulta marcada', nome)}\n{ag['descricao']}. Já está na sua agenda."


def novo_paciente(nome: str, telefone: str, descricao: str, convenio: str | None, objetivo: str | None) -> str:
    return "\n".join([_cabecalho("🆕", "Novo paciente", nome),
                      f"1ª consulta: {descricao}. Já está na sua agenda.",
                      f"Objetivo: {objetivo or 'não informado'} · Convênio: {convenio or 'particular/não informado'}",
                      f"Contato: {link_whatsapp(telefone)}"])


def lead_precisa(telefone: str, nome: str | None, resumo: str) -> str:
    quem = f"{nome} (ainda não é paciente)" if nome else "novo contato (ainda não é paciente)"
    return "\n".join([_cabecalho("🟡", "Responder", quem), curto(resumo),
                      f"👉 Responda quando puder: {link_whatsapp(telefone)}"])


def revogacao(nome: str) -> str:
    return "\n".join([_cabecalho("⛔", "Saiu do assistente", nome),
                      "Revogou o consentimento (mandou PARAR). O assistente não responde nem manda lembretes para ele.",
                      "Se pedir para apagar os dados: painel → paciente → Excluir."])
