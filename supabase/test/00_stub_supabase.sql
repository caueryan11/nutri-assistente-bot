-- Imita o mínimo do Supabase para testar a migração num Postgres comum.
-- NÃO rode isto no Supabase de verdade.

create role anon          nologin noinherit;
create role authenticated nologin noinherit;
create role service_role  nologin noinherit bypassrls;

create schema auth;
create table auth.users (id uuid primary key, email text);

-- No Supabase, auth.uid() lê o "sub" do JWT da requisição.
create function auth.uid() returns uuid language sql stable as $$
  select nullif(
    coalesce(current_setting('request.jwt.claim.sub', true),
             current_setting('request.jwt.claims', true)::jsonb ->> 'sub'),
    '')::uuid
$$;

create schema extensions;  -- no Supabase já existe e os roles têm usage nele
grant usage on schema auth, public, extensions to anon, authenticated, service_role;
grant execute on function auth.uid() to anon, authenticated, service_role;

-- Privilégios padrão como no Supabase: tabelas acessíveis; quem barra é a RLS.
alter default privileges in schema public grant all on tables    to anon, authenticated, service_role;
alter default privileges in schema public grant all on sequences to anon, authenticated, service_role;
