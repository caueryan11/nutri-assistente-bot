-- =====================================================================
-- Busca na TACO palavra por palavra.
--
-- A versão anterior comparava a frase inteira; com a TACO real,
-- "ovo cozido" trazia "Cará, cozido" e "pao frances" trazia
-- "Torrada, pão francês" antes de "Pão, trigo, francês".
--
-- Agora cada palavra do termo (3+ letras) ganha uma nota de 0 a 1:
--   1   se alguma palavra do nome começa com ela;
--   senão, a semelhança por trigramas (tolera erro de digitação).
-- cobertura  = média das notas (precisa ser >= 0,5 para aparecer)
-- relevancia = cobertura + 0,5 se o nome começa pela 1ª palavra do termo
-- Mesma assinatura e mesmas colunas de retorno da versão anterior.
-- =====================================================================
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
  with frase as (
    select public.normalizar_texto(trim(termo)) as f
  ),
  palavras as (
    select p.w, p.ordem
    from frase, regexp_split_to_table(frase.f, '[^a-z0-9]+') with ordinality as p(w, ordem)
    where length(p.w) >= 3
    union all
    -- termo curto (ex.: "pé"): usa o termo inteiro
    select frase.f, 1 from frase
    where length(frase.f) >= 2
      and not exists (select 1 from regexp_split_to_table(frase.f, '[^a-z0-9]+') w where length(w) >= 3)
  ),
  notas as (
    select a.id,
           avg(case when a.nome_normalizado ~ ('\m' || p.w) then 1
                    else extensions.word_similarity(p.w, a.nome_normalizado) end) as cobertura,
           bool_or(p.ordem = (select min(ordem) from palavras) and a.nome_normalizado like p.w || '%') as comeca
    from public.alimentos_taco a
    cross join palavras p
    group by a.id
  )
  select a.id, a.nome, a.grupo, a.energia_kcal, a.proteina_g, a.lipideos_g,
         a.carboidrato_g, a.fibra_g, a.marcacoes,
         (n.cobertura + case when n.comeca then 0.5 else 0 end)::real as relevancia
  from notas n
  join public.alimentos_taco a on a.id = n.id
  where n.cobertura >= 0.5
  order by relevancia desc, length(a.nome), a.id
  limit least(greatest(coalesce(limite, 5), 1), 50)
$$;
