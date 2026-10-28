-- =====================================================================
-- Check-in semanal (acompanhamento entre consultas, bot/checkin.py).
--
--   pacientes.acompanhamento_desde    paciente no acompanhamento mensal (null = fora); a nutri liga no painel
--   nutricionistas.checkin_config     dia da semana (ISO, 1 = segunda) e hora do envio
--   checkins                          uma linha por paciente por semana: pergunta enviada e respostas
--                                     (evolução do paciente; apagada junto com o paciente)
-- =====================================================================

alter table public.pacientes
  add column if not exists acompanhamento_desde date;

alter table public.nutricionistas
  add column if not exists checkin_config jsonb not null default '{"dia": 7, "hora": "18:00"}'::jsonb;

create table if not exists public.checkins (
  id                uuid primary key default gen_random_uuid(),
  nutricionista_id  uuid not null,
  paciente_id       uuid not null,
  semana            date not null,                   -- dia do envio (fuso da nutri)
  status            text not null default 'enviado' check (status in ('enviado', 'respondido')),
  enviado_em        timestamptz not null default now(),
  respondido_em     timestamptz,
  adesao            smallint check (adesao between 0 and 10),
  fome              text check (fome in ('pouca', 'normal', 'muita')),
  intestino         text check (intestino in ('normal', 'preso', 'solto')),
  peso_kg           numeric(5, 1) check (peso_kg between 20 and 350),
  comentario        text,
  alerta            boolean not null default false,
  alerta_motivo     text,
  lembrete_enviado  boolean not null default false,   -- já pediu o que faltou (uma vez só)
  unique (paciente_id, semana),
  foreign key (paciente_id, nutricionista_id)
    references public.pacientes (id, nutricionista_id) on delete cascade
);
create index if not exists checkins_paciente_idx on public.checkins (paciente_id, semana desc);

alter table public.checkins enable row level security;
drop policy if exists dono on public.checkins;
create policy dono on public.checkins
  for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));
