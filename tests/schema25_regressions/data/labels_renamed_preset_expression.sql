jsonb_build_object(
   'schema_version',2,'reviews','[]'::jsonb,'folders','[]'::jsonb,'collections','[]'::jsonb,'channels','[]'::jsonb,
   'labels',jsonb_build_array(
     jsonb_build_object('label_key','custom:0b000000-0000-4000-8000-000000000085','name','Monetized',
       'label_type','custom','created_at',now()),
     jsonb_build_object('label_key','monetized','name','Paid','label_type','preset','created_at',now())))