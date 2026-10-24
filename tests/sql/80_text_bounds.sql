-- Provider text is stored within generous bounds (audit S5-12): values at the
-- bound are accepted, and one character or tag more is refused.
begin;
insert into public.channels(channel_id, title, avatar_url)
values ('UCtextbounds0000000000aa', repeat('t', 1000), 'https://yt3.ggpht.com/' || repeat('a', 2026));
insert into public.videos(video_id, channel_id, title, duration, tags)
values ('textbound01', 'UCtextbounds0000000000aa', repeat('v', 1000), 'P' || repeat('1', 62) || 'D',
        array_fill('x'::text, array[500]));
do $$
declare
  refused int := 0;
  probe text;
begin
  foreach probe in array array[
    $q$update public.channels set title = repeat('t', 1001) where channel_id = 'UCtextbounds0000000000aa'$q$,
    $q$update public.channels set avatar_url = repeat('a', 2049) where channel_id = 'UCtextbounds0000000000aa'$q$,
    $q$update public.videos set title = repeat('v', 1001) where video_id = 'textbound01'$q$,
    $q$update public.videos set duration = repeat('P', 65) where video_id = 'textbound01'$q$,
    $q$update public.videos set tags = array_fill('x'::text, array[501]) where video_id = 'textbound01'$q$,
    $q$update public.videos set tags = array[repeat('x', 25601)] where video_id = 'textbound01'$q$,
    $q$update public.videos set tags = array['x', null] where video_id = 'textbound01'$q$
  ] loop
    begin
      execute probe;
    exception when check_violation then
      refused := refused + 1;
    end;
  end loop;
  assert refused = 7, format('%s of 7 oversized values were refused', refused);
end $$;
rollback;
