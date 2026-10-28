"""Conexão com o Postgres que se recupera sozinha.

Num servidor que fica dias no ar, o pooler do Supabase derruba conexões ociosas.
`Conexao.executar` reabre a conexão e tenta de novo uma vez. Um lock serializa o uso,
porque o webhook processa mensagens em threads.
"""

from __future__ import annotations

import logging
import threading

import psycopg2
import psycopg2.extras

log = logging.getLogger(__name__)


class Conexao:
    def __init__(self, dsn: str, somente_leitura: bool = False):
        self._dsn = dsn
        self._somente_leitura = somente_leitura
        self._conn = None
        self._lock = threading.Lock()

    def _abrir(self) -> None:
        self._conn = psycopg2.connect(
            self._dsn, connect_timeout=10,
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        )
        self._conn.set_session(readonly=self._somente_leitura, autocommit=True)

    def _fechar(self) -> None:
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def executar(self, sql: str, params: tuple = (), *, retorno: str | None = None, como_dict: bool = False):
        """retorno: None (nada), 'um' (fetchone) ou 'todos' (fetchall)."""
        fabrica = psycopg2.extras.RealDictCursor if como_dict else None
        with self._lock:
            for tentativa in (1, 2):
                try:
                    if self._conn is None or self._conn.closed:
                        self._abrir()
                    with self._conn.cursor(cursor_factory=fabrica) as cur:
                        cur.execute(sql, params)
                        if retorno == "um":
                            return cur.fetchone()
                        if retorno == "todos":
                            return cur.fetchall()
                        return None
                except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                    self._fechar()
                    if tentativa == 2:
                        raise
                    log.warning("conexão com o banco caiu (%s); reconectando", type(e).__name__)
