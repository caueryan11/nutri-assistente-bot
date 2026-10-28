-- =====================================================================
-- Rascunhos de resposta (bot/rascunho.py + whatsapp/app.py).
--
-- Quando o bot encaminha uma mensagem para a nutri, o aviso pode trazer uma sugestão de resposta pesquisada em
-- fontes confiáveis. A nutri responde "sim" (envia), "não" (descarta) ou escreve a versão dela; nada vai para o
-- paciente sem ela. Uma linha por aviso de encaminhamento, com ou sem sugestão: citar o aviso liga a resposta
-- dela ao paciente certo. Tem dado de saúde: apagada junto com o paciente e pela retenção (bot/rotinas.py).
-- =====================================================================

create table if not exists public.rascunhos (
  id                uuid primary key default gen_random_uuid(),
  nutricionista_id  uuid not null,
  paciente_id       uuid not null,
  pergunta          text not null,                    -- mensagem do paciente
  resposta          text,                             -- sugestão; null = sem sugestão (sem evidência, mídia...)
  versao_nutri      boolean not null default false,   -- resposta foi trocada pelo texto da nutri (espera o "sim")
  fontes            jsonb not null default '[]'::jsonb, -- [{titulo, url}] vindas da busca
  atencao           text,                             -- o que a nutri deve conferir antes de enviar
  confianca         text check (confianca in ('alta', 'media', 'baixa', 'sem_evidencia')),
  aviso_wa_id       text,                             -- id da mensagem de aviso no WhatsApp da nutri
  status            text not null default 'pendente'
                    check (status in ('pendente', 'enviado', 'editado', 'descartado')),
  texto_enviado     text,                             -- o que foi para o paciente (sugestão ou versão da nutri)
  buscas            smallint,
  tokens_entrada    integer,
  tokens_saida      integer,
  criado_em         timestamptz not null default now(),
  decidido_em       timestamptz,
  foreign key (paciente_id, nutricionista_id)
    references public.pacientes (id, nutricionista_id) on delete cascade
);
create index if not exists rascunhos_pendentes_idx on public.rascunhos (nutricionista_id, criado_em desc)
  where status = 'pendente';
create index if not exists rascunhos_aviso_idx on public.rascunhos (aviso_wa_id);

alter table public.rascunhos enable row level security;
drop policy if exists dono on public.rascunhos;
create policy dono on public.rascunhos
  for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));
