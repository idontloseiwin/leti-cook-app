jsonb_build_object(
   'schema_version',2,'labels',jsonb_build_array(jsonb_build_object('label_key','monetized','name','Monetized','label_type','preset','created_at',now())),'reviews','[]'::jsonb,'folders','[]'::jsonb,'collections','[]'::jsonb,
   'channels',jsonb_build_array(
     jsonb_build_object('id','d0000000-0000-4000-8000-000000000004','user_input','https://www.youtube.com/@parent',
      'folder_id',NULL,'parent_ref',NULL,'is_hidden',false,'added_at',now(),'label_keys','[]'::jsonb,'saved_views','[]'::jsonb),
     jsonb_build_object('id','a0000000-0000-4000-8000-000000000003','user_input','https://www.youtube.com/@probe',
      'folder_id',NULL,'parent_ref','d0000000-0000-4000-8000-000000000004',
      'is_hidden',false,'added_at',now(),'label_keys','["monetized"]'::jsonb,'saved_views','[]'::jsonb)))