"""A nutri manda o plano (PDF/foto) do WhatsApp de avisos para o assistente; o sistema cadastra o paciente,
monta o rascunho do plano e responde com o link para ela conferir e ativar.

A legenda (ou uma mensagem logo antes/depois) traz o nome e o WhatsApp do paciente, ex.:
"Maria Silva 51 99999-0000". Arquivo e dados do paciente esperam um pelo outro por 30 minutos (em memória).
"""

from __future__ import annotations

import re
import threading
import time

ESPERA_SEGUNDOS = 30 * 60
_TELEFONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_NAO_E_NOME = {"plano", "alimentar", "paciente", "whatsapp", "zap", "tel", "telefone", "cel", "celular", "numero",
               "número", "novo", "nova"}
_LIGACOES = {"da", "de", "do", "das", "dos", "e"}
_INICIO = _LIGACOES | {"é", "o", "a", "segue", "esse", "este", "aqui"}


def ler_legenda(texto: str) -> tuple[str | None, str | None]:
    """('Maria Silva', '5551999990000') a partir de 'Maria Silva - (51) 99999-0000'."""
    from whatsapp.painel import normalizar_telefone

    texto = texto or ""
    telefone = None
    for m in _TELEFONE.finditer(texto):
        telefone = normalizar_telefone(m.group(0))
        if telefone:
            texto = texto.replace(m.group(0), " ")
            break
    palavras = [p for p in re.sub(r"[^\wÀ-ÿ' ]", " ", texto).split()
                if not p.isdigit() and p.lower() not in _NAO_E_NOME]
    if not telefone:
        return None, None  # sem telefone, o texto não é a identificação do paciente
    while palavras and palavras[0].lower() in _INICIO:  # "Plano da Maria Silva" -> "Maria Silva"
        palavras.pop(0)
    if texto.strip() and all(p.islower() for p in palavras):  # "maria da silva" -> "Maria da Silva"
        palavras = [p if p in _LIGACOES else p.capitalize() for p in palavras]
    nome = " ".join(palavras)
    return (nome if len([p for p in palavras if p.lower() not in _LIGACOES]) >= 2 else None), telefone


class Pendentes:
    """Arquivo esperando o paciente, ou paciente esperando o arquivo, por nutri."""

    def __init__(self, agora=time.monotonic):
        self._arquivos: dict[str, tuple[float, bytes, str]] = {}
        self._pacientes: dict[str, tuple[float, str | None, str]] = {}
        self._agora = agora
        self._trava = threading.Lock()

    def _valido(self, t: float) -> bool:
        return self._agora() - t <= ESPERA_SEGUNDOS

    def guardar_arquivo(self, nutri_id: str, dados: bytes, mimetype: str) -> None:
        with self._trava:
            self._arquivos[nutri_id] = (self._agora(), dados, mimetype)

    def pegar_arquivo(self, nutri_id: str) -> tuple[bytes, str] | None:
        with self._trava:
            item = self._arquivos.pop(nutri_id, None)
        return (item[1], item[2]) if item and self._valido(item[0]) else None

    def tem_arquivo(self, nutri_id: str) -> bool:
        item = self._arquivos.get(nutri_id)
        return bool(item and self._valido(item[0]))

    def guardar_paciente(self, nutri_id: str, nome: str | None, telefone: str) -> None:
        with self._trava:
            self._pacientes[nutri_id] = (self._agora(), nome, telefone)

    def pegar_paciente(self, nutri_id: str) -> tuple[str | None, str] | None:
        with self._trava:
            item = self._pacientes.pop(nutri_id, None)
        return (item[1], item[2]) if item and self._valido(item[0]) else None


def resumo_para_nutri(nome: str, imp, avisos: list[str], link: str | None, criado: bool) -> str:
    linhas = [f"✅ Plano de *{nome}* lido: {len(imp.plano['refeicoes'])} refeição(ões), {imp.total_itens} itens."]
    if criado:
        linhas.append("Cadastro criado agora. Na primeira conversa, o assistente pede o aceite do termo.")
    for a in avisos[:6]:
        linhas.append(f"⚠️ {a}")
    if len(avisos) > 6:
        linhas.append(f"⚠️ e mais {len(avisos) - 6} ponto(s) no painel.")
    linhas.append("")
    linhas.append("O plano ainda *não está ativo*. Confira com o documento e ative aqui:")
    linhas.append(link or "(painel não configurado: fale com o suporte para ativar)")
    return "\n".join(linhas)
