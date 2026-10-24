-- The protected readable views use this fixed proof without granting readers
-- access to its raw inputs. A nested security-invoker view would check those
-- inputs as the original HTTP reader, even below a non-invoker outer view.
-- The postgres owner, security barrier, query and existing ACL are unchanged.
ALTER VIEW public.video_type_automatic_candidates SET (security_invoker=false);
