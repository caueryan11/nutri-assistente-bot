-- =====================================================================
-- Módulo comercial: triagem de quem ainda NÃO é paciente (leads).
--
--   nutricionistas: dados comerciais que o bot usa para responder o lead
--   leads:          quem mandou mensagem e não é paciente (um por nutri + telefone)
--   lead_mensagens: conversa do lead (o bot precisa lembrar o que foi dito);
--                   entra na retenção via limpar_lead_mensagens_antigas(dias)
-- Quando o lead marca a primeira consulta, vira paciente (sem consentimento:
-- o fluxo clínico só começa depois do aceite do termo).
-- =====================================================================

alter table public.nutricionistas
  add column if not exists atende_convenio      boolean not null default false,
  add column if not exists convenios_aceitos    text[]  not null default '{}',
  add column if not exists valor_consulta       numeric(10, 2),
  add column if not exists politica_reembolso   text,
  add column if not exists apresentacao_servico text;

create table if not exists public.leads (
  id                  uuid primary key default gen_random_uuid(),
  nutricionista_id    uuid not null references public.nutricionistas(id) on delete cascade,
  telefone            text not null check (telefone ~ '^\+?[0-9]{10,15}$'),
  nome                text,
  objetivo            text,
  convenio_informado  text,
  status              text not null default 'em_triagem'
                      check (status in ('em_triagem', 'aguardando_escolha_horario', 'convertido', 'desistiu')),
  paciente_id         uuid references public.pacientes(id) on delete set null,
  criado_em           timestamptz not null default now(),
  atualizado_em       timestamptz not null default now(),
  unique (nutricionista_id, telefone)
);
create index if not exists leads_nutri_criado_idx on public.leads (nutricionista_id, criado_em);

create table if not exists public.lead_mensagens (
  id             uuid primary key default gen_random_uuid(),
  lead_id        uuid not null references public.leads(id) on delete cascade,
  direcao        text not null check (direcao in ('entrada', 'saida')),
  wa_message_id  text unique,
  conteudo       text,
  criado_em      timestamptz not null default clock_timestamp()  -- ordena mensagens da mesma transação
);
create index if not exists lead_mensagens_lead_idx on public.lead_mensagens (lead_id, criado_em desc);

drop trigger if exists leads_atualizado on public.leads;
create trigger leads_atualizado before update on public.leads
  for each row execute function public.tocar_atualizado_em();

-- RLS: a nutri vê só os leads dela (painel futuro); o backend usa service_role.
alter table public.leads          enable row level security;
alter table public.lead_mensagens enable row level security;

drop policy if exists dono on public.leads;
create policy dono on public.leads
  for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));

drop policy if exists dono_le on public.lead_mensagens;
create policy dono_le on public.lead_mensagens
  for select to authenticated
  using (exists (select 1 from public.leads l
                  where l.id = lead_id and l.nutricionista_id = (select public.minha_nutri_id())));

-- Retenção da conversa dos leads (mesmo prazo das mensagens de pacientes; roda pelo backend).
create or replace function public.limpar_lead_mensagens_antigas(dias integer)
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
  apagadas integer;
begin
  if dias is null or dias < 1 then
    raise exception 'dias precisa ser >= 1';
  end if;
  delete from public.lead_mensagens where criado_em < now() - make_interval(days => dias);
  get diagnostics apagadas = row_count;
  return apagadas;
end;
$$;
revoke execute on function public.limpar_lead_mensagens_antigas(integer) from public, anon, authenticated;
grant  execute on function public.limpar_lead_mensagens_antigas(integer) to service_role;

-- Convênio usado na consulta ('particular' quando não houver); a primeira consulta usa tipo = 'primeira'.
alter table public.agendamentos
  add column if not exists convenio text;
