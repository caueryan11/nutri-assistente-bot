-- =====================================================================
-- Campanha de reativação da base (bot/reativacao.py) + aceite só depois de ver o termo.
--
--   campanhas         uma por disparo: mensagem, público (dias sem consulta), status
--   campanha_envios   um por paciente: fila de envio, horário do envio e da resposta (sem conteúdo)
--   pacientes.termo_pedido_em  quando o pedido do termo foi mostrado. "Aceito"/"Sim" só vale depois
--                              disso (resposta "SIM" à campanha não pode virar aceite de termo não visto)
-- =====================================================================

alter table public.pacientes
  add column if not exists termo_pedido_em timestamptz;
-- quem está esperando aceite já recebeu o pedido na primeira mensagem
update public.pacientes set termo_pedido_em = now()
 where termo_pedido_em is null and consentimento_em is null and status_consentimento = 'pendente'
   and exists (select 1 from public.mensagens m where m.paciente_id = pacientes.id);

create table if not exists public.campanhas (
  id                 uuid primary key default gen_random_uuid(),
  nutricionista_id   uuid not null references public.nutricionistas(id) on delete cascade,
  mensagem           text not null,
  dias_sem_consulta  integer not null,
  status             text not null default 'enviando' check (status in ('enviando', 'pausada', 'concluida')),
  total              integer not null default 0,
  criada_em          timestamptz not null default now()
);

create table if not exists public.campanha_envios (
  id                uuid primary key default gen_random_uuid(),
  campanha_id       uuid not null references public.campanhas(id) on delete cascade,
  nutricionista_id  uuid not null,
  paciente_id       uuid not null,
  status            text not null default 'pendente' check (status in ('pendente', 'enviado', 'pulado', 'falhou')),
  tentativas        integer not null default 0,
  criado_em         timestamptz not null default now(),
  enviado_em        timestamptz,
  respondeu_em      timestamptz,
  unique (campanha_id, paciente_id),
  foreign key (paciente_id, nutricionista_id)
    references public.pacientes (id, nutricionista_id) on delete cascade
);
create index if not exists campanha_envios_fila_idx on public.campanha_envios (status, criado_em) where status = 'pendente';
create index if not exists campanha_envios_paciente_idx on public.campanha_envios (paciente_id, enviado_em);

alter table public.campanhas enable row level security;
alter table public.campanha_envios enable row level security;
drop policy if exists dono on public.campanhas;
create policy dono on public.campanhas for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));
drop policy if exists dono on public.campanha_envios;
create policy dono on public.campanha_envios for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));
