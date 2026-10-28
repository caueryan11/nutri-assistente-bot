-- =====================================================================
-- Agenda no Google Calendar.
--   nutricionistas.google_calendar_id  agenda que a nutri compartilhou com a conta de serviço
--   nutricionistas.agenda_config       horário de atendimento, duração, antecedência, janela
--   agendamentos.google_event_id       evento criado pelo bot (para cancelar/remarcar depois)
-- =====================================================================
alter table public.nutricionistas
  add column if not exists google_calendar_id text,
  add column if not exists agenda_config jsonb not null default '{
    "duracao_min": 60,
    "antecedencia_horas": 12,
    "janela_dias": 14,
    "atendimento": {"1": ["08:00-12:00", "14:00-18:00"], "2": ["08:00-12:00", "14:00-18:00"],
                    "3": ["08:00-12:00", "14:00-18:00"], "4": ["08:00-12:00", "14:00-18:00"],
                    "5": ["08:00-12:00", "14:00-18:00"]}
  }'::jsonb;

alter table public.nutricionistas
  add constraint nutricionistas_agenda_config_objeto check (jsonb_typeof(agenda_config) = 'object');

alter table public.agendamentos
  add column if not exists google_event_id text;
