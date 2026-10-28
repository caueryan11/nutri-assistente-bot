-- =====================================================================
-- Nutri Assistente — schema inicial
-- Supabase (Postgres 15+), região São Paulo.
--
-- Conteúdo:
--   1. Extensões e funções auxiliares
--   2. Tabelas
--   3. RLS (cada nutri só vê os próprios dados)
--   4. Busca na TACO
--   5. Funções de LGPD (retenção e exclusão)
--
-- Convenções:
--   - Tudo em timestamptz (UTC no banco; fuso da nutri na aplicação).
--   - Toda tabela com dado de paciente tem nutricionista_id, usado pela RLS.
--   - FKs compostas (id, nutricionista_id) impedem ligar dado de uma nutri
--     a paciente de outra, mesmo por erro no backend.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. Extensões e funções auxiliares
-- ---------------------------------------------------------------------
create schema if not exists extensions;
create extension if not exists pg_trgm    with schema extensions;
create extension if not exists unaccent   with schema extensions;
create extension if not exists btree_gist with schema extensions;  -- agenda sem sobreposição

-- unaccent() não é IMMUTABLE; este wrapper é, e pode ser usado em índice
-- e em coluna gerada.
create or replace function public.normalizar_texto(t text)
returns text
language sql
immutable
parallel safe
as $$
  select lower(extensions.unaccent('extensions.unaccent'::regdictionary, coalesce(t, '')))
$$;

create or replace function public.tocar_atualizado_em()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.atualizado_em := now();
  return new;
end;
$$;

-- ---------------------------------------------------------------------
-- 2. Tabelas
-- ---------------------------------------------------------------------

-- Nutricionistas (clientes). auth_user_id liga ao login do Supabase Auth.
create table public.nutricionistas (
  id                        uuid primary key default gen_random_uuid(),
  auth_user_id              uuid unique references auth.users(id) on delete set null,
  nome                      text not null,
  crn                       text,
  email                     text,
  telefone_aviso            text,          -- WhatsApp pessoal da nutri (E.164), para encaminhamentos e resumo
  whatsapp_phone_number_id  text unique,   -- phone_number_id da Meta: roteia a mensagem recebida para a nutri certa
  fuso_horario              text not null default 'America/Sao_Paulo',
  ativo                     boolean not null default true,
  criado_em                 timestamptz not null default now(),
  atualizado_em             timestamptz not null default now()
);

create table public.pacientes (
  id                    uuid primary key default gen_random_uuid(),
  nutricionista_id      uuid not null references public.nutricionistas(id) on delete cascade,
  nome                  text not null,
  primeiro_nome         text generated always as (split_part(trim(nome), ' ', 1)) stored,
  telefone              text not null check (telefone ~ '^\+?[0-9]{10,15}$'),  -- E.164, como a Meta envia (sem "+")
  consentimento_em      timestamptz,     -- sem isso o bot NÃO responde
  consentimento_versao  text,            -- versão do termo aceito
  ativo                 boolean not null default true,
  retorno_previsto_em   date,
  criado_em             timestamptz not null default now(),
  atualizado_em         timestamptz not null default now(),
  unique (nutricionista_id, telefone),
  unique (id, nutricionista_id)          -- alvo das FKs compostas
);
create index pacientes_telefone_idx on public.pacientes (telefone);

-- Plano alimentar em JSON. O formato interno depende de como a nutri
-- entrega a dieta (decisão da Ray); o banco só garante que é um objeto
-- e que existe no máximo um plano ativo por paciente.
create table public.planos_alimentares (
  id                uuid primary key default gen_random_uuid(),
  paciente_id       uuid not null,
  nutricionista_id  uuid not null,
  conteudo          jsonb not null check (jsonb_typeof(conteudo) = 'object'),
  ativo             boolean not null default true,
  vigente_desde     date not null default current_date,
  criado_em         timestamptz not null default now(),
  atualizado_em     timestamptz not null default now(),
  foreign key (paciente_id, nutricionista_id)
    references public.pacientes (id, nutricionista_id) on delete cascade
);
create unique index planos_um_ativo_por_paciente
  on public.planos_alimentares (paciente_id) where ativo;

-- Regras clínicas (texto que entra no prompt). nutricionista_id NULL = regra padrão
-- do produto; a nutri pode ter a própria versão. Dona do conteúdo: Ray.
create table public.regras_clinicas (
  id                uuid primary key default gen_random_uuid(),
  nutricionista_id  uuid references public.nutricionistas(id) on delete cascade,
  versao            integer not null check (versao > 0),
  conteudo          text not null,
  ativo             boolean not null default true,
  criado_em         timestamptz not null default now(),
  unique nulls not distinct (nutricionista_id, versao)
);
create unique index regras_uma_ativa
  on public.regras_clinicas (coalesce(nutricionista_id, '00000000-0000-0000-0000-000000000000'::uuid))
  where ativo;

create table public.agendamentos (
  id                   uuid primary key default gen_random_uuid(),
  nutricionista_id     uuid not null,
  paciente_id          uuid not null,
  inicio               timestamptz not null,
  fim                  timestamptz not null,
  tipo                 text not null default 'retorno'  check (tipo in ('primeira', 'retorno')),
  status               text not null default 'proposto' check (status in ('proposto', 'confirmado', 'cancelado', 'realizado', 'faltou')),
  origem               text not null default 'bot'      check (origem in ('bot', 'nutri')),
  lembrete_enviado_em  timestamptz,
  criado_em            timestamptz not null default now(),
  atualizado_em        timestamptz not null default now(),
  check (fim > inicio),
  foreign key (paciente_id, nutricionista_id)
    references public.pacientes (id, nutricionista_id) on delete cascade,
  -- Impede dois horários sobrepostos na agenda da mesma nutri
  constraint agendamentos_sem_sobreposicao exclude using gist (
    nutricionista_id with =,
    tstzrange(inicio, fim) with &&
  ) where (status in ('proposto', 'confirmado'))
);
create index agendamentos_nutri_inicio_idx on public.agendamentos (nutricionista_id, inicio);
create index agendamentos_lembrete_idx on public.agendamentos (inicio)
  where status = 'confirmado' and lembrete_enviado_em is null;

-- Mensagens do WhatsApp. wa_message_id único = idempotência do webhook
-- (a Meta reenvia o mesmo evento; o segundo insert falha e é ignorado).
create table public.mensagens (
  id                 uuid primary key default gen_random_uuid(),
  nutricionista_id   uuid not null,
  paciente_id        uuid not null,
  direcao            text not null check (direcao in ('entrada', 'saida')),
  wa_message_id      text unique,
  tipo               text not null default 'texto' check (tipo in ('texto', 'audio', 'imagem', 'documento', 'outro')),
  conteudo           text,
  acao               text check (acao in ('responder', 'encaminhar')),   -- decisão do bot (só em saída)
  motivo             text,                                                -- por que encaminhou
  resumo_para_nutri  text,
  tokens_entrada     integer,
  tokens_saida       integer,
  criado_em          timestamptz not null default now(),
  foreign key (paciente_id, nutricionista_id)
    references public.pacientes (id, nutricionista_id) on delete cascade
);
create index mensagens_paciente_idx on public.mensagens (paciente_id, criado_em desc);
create index mensagens_nutri_idx    on public.mensagens (nutricionista_id, criado_em desc);
create index mensagens_criado_idx   on public.mensagens (criado_em);

-- TACO (4ª edição), valores por 100 g de parte comestível.
-- "Tr", "NA" e "*" ficam NULL no número e anotados em marcacoes
-- (ex.: {"fibra_g": "Tr"}). Nunca viram zero.
create table public.alimentos_taco (
  id                integer primary key,      -- número do alimento na TACO
  nome              text not null,
  grupo             text,
  nome_normalizado  text generated always as (public.normalizar_texto(nome)) stored,
  umidade_pct       numeric,
  energia_kcal      numeric,
  energia_kj        numeric,
  proteina_g        numeric,
  lipideos_g        numeric,
  colesterol_mg     numeric,
  carboidrato_g     numeric,
  fibra_g           numeric,
  cinzas_g          numeric,
  calcio_mg         numeric,
  magnesio_mg       numeric,
  manganes_mg       numeric,
  fosforo_mg        numeric,
  ferro_mg          numeric,
  sodio_mg          numeric,
  potassio_mg       numeric,
  cobre_mg          numeric,
  zinco_mg          numeric,
  retinol_mcg       numeric,
  re_mcg            numeric,
  rae_mcg           numeric,
  tiamina_mg        numeric,
  riboflavina_mg    numeric,
  piridoxina_mg     numeric,
  niacina_mg        numeric,
  vitamina_c_mg     numeric,
  marcacoes         jsonb not null default '{}'::jsonb
);
create index alimentos_taco_trgm_idx
  on public.alimentos_taco using gin (nome_normalizado extensions.gin_trgm_ops);

-- Triggers de atualizado_em
create trigger nutricionistas_atualizado before update on public.nutricionistas
  for each row execute function public.tocar_atualizado_em();
create trigger pacientes_atualizado before update on public.pacientes
  for each row execute function public.tocar_atualizado_em();
create trigger planos_atualizado before update on public.planos_alimentares
  for each row execute function public.tocar_atualizado_em();
create trigger agendamentos_atualizado before update on public.agendamentos
  for each row execute function public.tocar_atualizado_em();

-- ---------------------------------------------------------------------
-- 3. RLS
-- O backend (bot, n8n) usa a service_role, que ignora RLS.
-- As políticas abaixo valem para a nutri logada (role authenticated),
-- por exemplo num painel futuro. anon não tem política = sem acesso.
-- ---------------------------------------------------------------------

-- id da nutricionista do usuário logado
create or replace function public.minha_nutri_id()
returns uuid
language sql
stable
security definer
set search_path = ''
as $$
  select n.id from public.nutricionistas n where n.auth_user_id = auth.uid()
$$;
revoke execute on function public.minha_nutri_id() from public, anon;
grant  execute on function public.minha_nutri_id() to authenticated, service_role;

alter table public.nutricionistas     enable row level security;
alter table public.pacientes          enable row level security;
alter table public.planos_alimentares enable row level security;
alter table public.regras_clinicas    enable row level security;
alter table public.agendamentos       enable row level security;
alter table public.mensagens          enable row level security;
alter table public.alimentos_taco     enable row level security;

-- A nutri vê e edita o próprio cadastro; criar/apagar nutri é só pelo backend.
create policy nutri_ve_a_si on public.nutricionistas
  for select to authenticated using (id = (select public.minha_nutri_id()));
create policy nutri_edita_a_si on public.nutricionistas
  for update to authenticated
  using (id = (select public.minha_nutri_id()))
  with check (id = (select public.minha_nutri_id()));

create policy dono on public.pacientes
  for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));

create policy dono on public.planos_alimentares
  for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));

create policy dono on public.agendamentos
  for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));

-- Mensagens: a nutri só lê. Quem grava é o backend.
create policy dono_le on public.mensagens
  for select to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()));

-- Regras: lê as próprias e as padrão (NULL); edita só as próprias.
create policy le_proprias_e_padrao on public.regras_clinicas
  for select to authenticated
  using (nutricionista_id is null or nutricionista_id = (select public.minha_nutri_id()));
create policy edita_proprias on public.regras_clinicas
  for all to authenticated
  using (nutricionista_id = (select public.minha_nutri_id()))
  with check (nutricionista_id = (select public.minha_nutri_id()));

-- TACO: leitura para quem está logado; ninguém edita pelo cliente.
create policy leitura on public.alimentos_taco
  for select to authenticated using (true);

-- ---------------------------------------------------------------------
-- 4. Busca na TACO
-- Tolera acento, caixa e erro de digitação. Prioriza: nome começando
-- pelo termo > contendo o termo > parecido.
-- ---------------------------------------------------------------------
create or replace function public.buscar_alimento(termo text, limite integer default 5)
returns table (
  id            integer,
  nome          text,
  grupo         text,
  energia_kcal  numeric,
  proteina_g    numeric,
  lipideos_g    numeric,
  carboidrato_g numeric,
  fibra_g       numeric,
  marcacoes     jsonb,
  relevancia    real
)
language sql
stable
security invoker
set search_path = ''
as $$
  with t as (select public.normalizar_texto(trim(termo)) as q)
  select a.id, a.nome, a.grupo, a.energia_kcal, a.proteina_g, a.lipideos_g,
         a.carboidrato_g, a.fibra_g, a.marcacoes,
         (case when a.nome_normalizado like t.q || '%' then 2
               when a.nome_normalizado like '%' || t.q || '%' then 1
               else 0 end
          + extensions.word_similarity(t.q, a.nome_normalizado))::real as relevancia
  from public.alimentos_taco a, t
  where length(t.q) >= 2
    and (a.nome_normalizado like '%' || t.q || '%'
         or t.q operator(extensions.<%) a.nome_normalizado)
  order by relevancia desc, length(a.nome), a.id
  limit least(greatest(coalesce(limite, 5), 1), 50)
$$;
grant execute on function public.buscar_alimento(text, integer) to authenticated, service_role;

-- ---------------------------------------------------------------------
-- 5. LGPD
-- ---------------------------------------------------------------------

-- Retenção: apaga mensagens mais antigas que N dias. Roda pelo n8n
-- com a service_role. O prazo ainda será definido (semana 2).
create or replace function public.limpar_mensagens_antigas(dias integer)
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
  delete from public.mensagens where criado_em < now() - make_interval(days => dias);
  get diagnostics apagadas = row_count;
  return apagadas;
end;
$$;
revoke execute on function public.limpar_mensagens_antigas(integer) from public, anon, authenticated;
grant  execute on function public.limpar_mensagens_antigas(integer) to service_role;

-- Exclusão a pedido do titular: apaga o paciente e, em cascata, plano,
-- agendamentos e mensagens. Roda com os direitos de quem chama: a nutri
-- só consegue apagar paciente dela (RLS). Retorna true se apagou.
create or replace function public.excluir_paciente(p_paciente_id uuid)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  n integer;
begin
  delete from public.pacientes where id = p_paciente_id;
  get diagnostics n = row_count;
  return n > 0;
end;
$$;
revoke execute on function public.excluir_paciente(uuid) from public, anon;
grant  execute on function public.excluir_paciente(uuid) to authenticated, service_role;
