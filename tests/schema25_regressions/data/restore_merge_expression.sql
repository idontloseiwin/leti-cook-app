jsonb_build_object(
   'schema_version',2,'labels',jsonb_build_array(jsonb_build_object('label_key','monetized','name','Monetized','label_type','preset','created_at',now())),'reviews','[]'::jsonb,
   'folders',jsonb_build_array(jsonb_build_object('id','f0000000-0000-4000-8000-000000000001','name','Music','created_at',now())),
   'channels',jsonb_build_array(
     jsonb_build_object('id','a0000000-0000-4000-8000-000000000001','user_input','https://www.youtube.com/@probe',
      'folder_id','f0000000-0000-4000-8000-000000000001','parent_ref',NULL,'is_hidden',false,'added_at',now()-interval '9 days',
      'label_keys','["monetized"]'::jsonb,
      'saved_views',jsonb_build_array(jsonb_build_object('id','e0000000-0000-4000-8000-000000000001','name','Recent',
        'conditions','{"version":1,"filters":{},"facets":{}}'::jsonb,'create_fingerprint',repeat('1',64),'revision',3,
        'created_at',now(),'updated_at',now()))),
     jsonb_build_object('id','b0000000-0000-4000-8000-000000000002','user_input','https://www.youtube.com/@probechild',
      'folder_id',NULL,'parent_ref','a0000000-0000-4000-8000-000000000001','is_hidden',false,'added_at',now()-interval '9 days',
      'label_keys','[]'::jsonb,'saved_views','[]'::jsonb)),
   'collections',jsonb_build_array(jsonb_build_object(
    'id','c0000000-0000-4000-8000-000000000001','name','Saved','create_fingerprint',repeat('0',64),
    'revision',1,'created_at',now(),'updated_at',now(),
    'members',jsonb_build_array(jsonb_build_object('video_id','aaaaaaaaaaa','added_at',now(),
      'channel_ref','a0000000-0000-4000-8000-000000000001')))))