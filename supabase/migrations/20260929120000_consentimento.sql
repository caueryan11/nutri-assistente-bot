-- =====================================================================
-- Consentimento LGPD do paciente (trava antes de qualquer conversa com o bot).
--
--   pacientes.consentimento_em      quando aceitou (NULL = não aceitou)       [já existia]
--   pacientes.versao_termo          versão do termo aceito                    [era consentimento_versao]
--   pacientes.status_consentimento  pendente | aceito | revogado
--
-- consentimento_versao existia desde o schema inicial e nenhum código usava:
-- é renomeada em vez de criar uma coluna duplicada.
-- Um gatilho mantém status e data coerentes: com consentimento_em preenchido
-- o status vira 'aceito'; sem data, 'aceito' volta para 'pendente'.
-- =====================================================================

do $$ begin
  if exists (select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'pacientes' and column_name = 'consentimento_versao')
     and not exists (select 1 from information_schema.columns
             where table_schema = 'public' and table_name = 'pacientes' and column_name = 'versao_termo') then
    alter table public.pacientes rename column consentimento_versao to versao_termo;
  end if;
end $$;

alter table public.pacientes
  add column if not exists consentimento_em timestamptz,
  add column if not exists versao_termo text,
  add column if not exists status_consentimento text not null default 'pendente';

alter table public.pacientes
  drop constraint if exists pacientes_status_consentimento_valido;
alter table public.pacientes
  add constraint pacientes_status_consentimento_valido
  check (status_consentimento in ('pendente', 'aceito', 'revogado'));

create or replace function public.sincronizar_status_consentimento()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  if new.consentimento_em is not null then
    new.status_consentimento := 'aceito';
  elsif new.status_consentimento = 'aceito' then
    new.status_consentimento := 'pendente';
  end if;
  return new;
end;
$$;

drop trigger if exists pacientes_status_consentimento on public.pacientes;
create trigger pacientes_status_consentimento
  before insert or update of consentimento_em, status_consentimento on public.pacientes
  for each row execute function public.sincronizar_status_consentimento();

-- Pacientes que já tinham aceitado antes desta migração
update public.pacientes
   set status_consentimento = 'aceito'
 where consentimento_em is not null and status_consentimento <> 'aceito';
