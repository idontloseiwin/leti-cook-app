-- The synthetic transition is deliberately separate from fresh B.
ALTER TABLE public.items ADD COLUMN normalized text NOT NULL DEFAULT '';
UPDATE public.items SET normalized=lower(label),quantity=quantity+10;
ALTER TABLE public.items ADD CONSTRAINT items_quantity_nonnegative CHECK(quantity>=0);
CREATE INDEX items_normalized_idx ON public.items(normalized);
CREATE FUNCTION public.normalized_label(value text) RETURNS text LANGUAGE sql IMMUTABLE AS $function$SELECT lower(value)$function$;
CREATE VIEW readable.items WITH(security_barrier=true,security_invoker=false) AS SELECT id,normalized,quantity FROM public.items;
GRANT SELECT(normalized) ON public.items TO lic_reader;
GRANT SELECT ON readable.items TO lic_reader;
INSERT INTO public.settings(name,value) VALUES('new-setting',7);
