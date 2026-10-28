"""Apaga os dados de teste de uma nutri antes do piloto com pacientes reais.

O que apaga (da nutri escolhida):
  - pacientes, e em cascata: planos, agendamentos, mensagens, check-ins e envios de campanha
  - interessados (leads) e as conversas deles
  - campanhas de reativação
O que fica: o cadastro da nutri, a configuração (agenda, horários, valores, convênios,
níveis de acompanhamento) e as regras clínicas. A TACO não é tocada.
Não apaga os eventos já criados no Google Agenda: remova-os lá, se quiser.

Por padrão só MOSTRA o que seria apagado (conexão somente leitura).
Para apagar de verdade: --apagar, e digitar o nome da nutri para confirmar.
Tudo numa transação: ou apaga tudo, ou nada.

Uso:
  python scripts/limpar_dados_teste.py                          # lista as nutris
  python scripts/limpar_dados_teste.py --nutri <id>             # mostra o que seria apagado
  python scripts/limpar_dados_teste.py --nutri <id> --manter 5551999990000 --apagar
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2

RAIZ = Path(__file__).resolve().parent.parent

# (rótulo, SQL de contagem). Todas filtram pela nutri (%(nutri)s) e poupam os telefones em %(manter)s.
_PACIENTES = "select id from public.pacientes where nutricionista_id = %(nutri)s and not (telefone = any(%(manter)s))"
CONTAGENS = [
    ("pacientes", f"select count(*) from public.pacientes where id in ({_PACIENTES})"),
    ("planos alimentares", f"select count(*) from public.planos_alimentares where paciente_id in ({_PACIENTES})"),
    ("agendamentos", f"select count(*) from public.agendamentos where paciente_id in ({_PACIENTES})"),
    ("mensagens", f"select count(*) from public.mensagens where paciente_id in ({_PACIENTES})"),
    ("check-ins", f"select count(*) from public.checkins where paciente_id in ({_PACIENTES})"),
    ("interessados (leads)", "select count(*) from public.leads where nutricionista_id = %(nutri)s"),
    ("campanhas", "select count(*) from public.campanhas where nutricionista_id = %(nutri)s"),
]

# Ordem: campanhas e leads primeiro (os envios de campanha e leads apontam para pacientes).
REMOCOES = [
    "delete from public.campanhas where nutricionista_id = %(nutri)s",
    "delete from public.leads where nutricionista_id = %(nutri)s",
    f"delete from public.pacientes where id in ({_PACIENTES})",
]


def dsn() -> str:
    try:
        from dotenv import dotenv_values
        valor = os.environ.get("DATABASE_URL") or dotenv_values(RAIZ / ".env").get("DATABASE_URL")
    except ImportError:
        valor = os.environ.get("DATABASE_URL")
    if not valor:
        sys.exit("DATABASE_URL não definida (nem no ambiente, nem no .env).")
    return valor


def listar_nutris(cur) -> None:
    cur.execute("""
        select n.id, n.nome, count(p.id)
          from public.nutricionistas n left join public.pacientes p on p.nutricionista_id = n.id
         group by n.id, n.nome order by n.nome
    """)
    print("Nutris cadastradas:")
    for id_, nome, pacientes in cur.fetchall():
        print(f"  {id_}  {nome}  ({pacientes} pacientes)")
    print("\nRode de novo com --nutri <id>.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nutri", help="id da nutri cujos dados de teste serão apagados")
    ap.add_argument("--manter", nargs="*", default=[], help="telefones de pacientes a manter (só dígitos, como no banco)")
    ap.add_argument("--apagar", action="store_true", help="apaga de verdade (pede confirmação)")
    args = ap.parse_args()

    conn = psycopg2.connect(dsn(), connect_timeout=10)
    conn.set_session(readonly=not args.apagar)
    try:
        with conn.cursor() as cur:
            if not args.nutri:
                listar_nutris(cur)
                return
            cur.execute("select nome from public.nutricionistas where id = %s", (args.nutri,))
            linha = cur.fetchone()
            if linha is None:
                sys.exit("Nutri não encontrada.")
            nome = linha[0]
            params = {"nutri": args.nutri, "manter": list(args.manter)}

            print(f"Nutri: {nome}")
            if args.manter:
                print(f"Mantendo os pacientes com telefone: {', '.join(args.manter)}")
            print("Seria apagado:" if not args.apagar else "Será apagado:")
            total = 0
            for rotulo, sql in CONTAGENS:
                cur.execute(sql, params)
                n = cur.fetchone()[0]
                total += n
                print(f"  {rotulo:<24}{n:>6}")
            if not args.apagar:
                print("\nNada foi apagado. Para apagar, rode de novo com --apagar.")
                return
            if total == 0:
                print("\nNada a apagar.")
                return

            confirmacao = input(f'\nIsto não tem volta. Digite o nome da nutri ("{nome}") para confirmar: ')
            if confirmacao.strip() != nome:
                print("Nome diferente. Nada foi apagado.")
                conn.rollback()
                return
            for sql in REMOCOES:
                cur.execute(sql, params)
        conn.commit()
        print("Pronto: dados de teste apagados. A configuração da nutri foi mantida.")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
