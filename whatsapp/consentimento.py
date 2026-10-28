"""Trava de consentimento LGPD: sem aceite, o bot não conversa (nem chama a IA).

Textos provisórios: o termo e as mensagens precisam de revisão da Ray e do advogado.
"""

from __future__ import annotations

import re
import unicodedata

VERSAO_TERMO = "v1.0"
TERMO_URL_PROVISORIO = "https://nutri-assistente-production.up.railway.app/termo"  # servido pelo app (/termo)

PEDIDO = (
    "Olá! Sou o assistente virtual da sua nutricionista. Antes de tirar dúvidas sobre o seu plano "
    "e agendamentos, precisamos do seu consentimento com os nossos termos de uso e privacidade de "
    "dados de saúde: {url}. Por favor, responda 'Aceito' para continuar."
)
CONFIRMACAO = (
    "Termo aceito com sucesso! Agora você já pode tirar suas dúvidas sobre seu plano alimentar "
    "ou consultar seus horários. Se quiser revogar o consentimento, é só responder PARAR a qualquer momento. "
    "Como posso te ajudar hoje?"
)
PEDIDO_REATIVACAO = (
    "Que bom falar com você! Para eu mostrar os horários por aqui, preciso antes do seu aceite aos termos de uso e "
    "privacidade do assistente: {url}. Responda 'Aceito' para continuar."
)
CONFIRMACAO_REATIVACAO = (
    "Termo aceito, obrigado! Quer que eu veja os horários para o seu retorno? Me diga o melhor dia ou período "
    "(manhã, tarde ou noite). Se quiser revogar o consentimento, é só responder PARAR a qualquer momento."
)
CONFIRMACAO_REVOGACAO = (
    "Pronto, seu consentimento foi revogado. O assistente não vai mais responder nem enviar mensagens "
    "automáticas; a nutricionista {nutri} segue à disposição pelo contato de sempre. Para pedir a exclusão "
    "dos seus dados, fale com ela. Se mudar de ideia, responda 'Aceito'."
)
CONFIRMACAO_SAIDA_LEAD = "Tudo bem! Não vou mais te enviar mensagens. Se quiser falar com a gente de novo, é só escrever."

# Respostas que valem como aceite (depois de normalizar: minúsculas, sem acento, sem pontuação).
# Só a resposta curta de aceite: "sim, mas antes quero saber..." NÃO é aceite.
_ACEITES = {
    "aceito", "eu aceito", "aceito sim", "sim aceito", "sim eu aceito", "aceito os termos", "eu aceito os termos",
    "concordo", "eu concordo", "concordo sim", "sim concordo", "concordo com os termos",
    "sim", "sim sim", "ok aceito", "aceito ok",
}


# Mensagem inteira que vale como revogação (normalizada). Só a mensagem sozinha: "sair da dieta" não é revogação.
_REVOGACOES = {
    "parar", "pare", "sair", "stop", "revogar", "revogo", "descadastrar", "me descadastra", "me descadastre",
    "revogar consentimento", "revogo o consentimento", "revogo meu consentimento", "cancelar consentimento",
    "nao quero mais receber mensagens", "parar mensagens", "parar de receber mensagens",
}


def normalizar(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)  # tira pontuação e emoji
    return re.sub(r"\s+", " ", t).strip()


def eh_aceite(texto: str) -> bool:
    """Aceite precisa ser inequívoco: pergunta ("aceito?") não conta."""
    return "?" not in (texto or "") and normalizar(texto) in _ACEITES


def eh_revogacao(texto: str) -> bool:
    return normalizar(texto) in _REVOGACOES


def pedido(url: str, reativacao: bool = False) -> str:
    return (PEDIDO_REATIVACAO if reativacao else PEDIDO).format(url=url or TERMO_URL_PROVISORIO)
