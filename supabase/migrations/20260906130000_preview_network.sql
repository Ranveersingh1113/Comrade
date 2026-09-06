-- The name the preview proxy dials.
--
-- Preview containers do not publish a host port. They join an internal Docker
-- network the API is also on, and the proxy reaches them by container NAME
-- through Docker's own DNS. So the name is not a convenience — it is the
-- address, and it has to be stored rather than reconstructed.
--
-- Relying on the short container id resolving in DNS would also work today and
-- is exactly the kind of undocumented coincidence that breaks on a Docker
-- upgrade with no error message.
alter table public.sandbox_processes add column if not exists container_name text;

grant select (container_name) on public.sandbox_processes to comrade_control;
