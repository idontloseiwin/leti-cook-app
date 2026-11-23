insert into public.sync_runs(id, started_at, finished_at, status)
values (900001, now() - interval '40 days', now() - interval '40 days', 'success'),
       (900002, now() - interval '2 days', now() - interval '2 days', 'success');
insert into public.youtube_api_request_attempts(request_attempt_id, project_id, quota_bucket, phase, endpoint, quota_units,
                                                status, started_at, finished_at, quota_day)
values ('a0000000-0000-4000-8000-00000000000a', 'probe', 'general', 'other', 'videos', 1, 'success',
        now() - interval '40 days', now() - interval '40 days', (now() - interval '40 days')::date),
       ('a0000000-0000-4000-8000-00000000000b', 'probe', 'general', 'other', 'videos', 1, 'success',
        now() - interval '1 hour', now() - interval '1 hour', now()::date);
