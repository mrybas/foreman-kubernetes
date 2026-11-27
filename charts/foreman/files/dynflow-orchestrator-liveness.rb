# Liveness check of the Dynflow orchestrator.
# Sidekiq must be running, and this pod must hold the database session that
# LISTENs for Dynflow events. Without it the orchestrator stays up but never
# hears that a remote task finished. An unreachable database passes: restarting
# this pod would not help.
exit 1 unless File.read("/proc/1/cmdline").include?("busy]")
require "pg"
begin
  conn = PG.connect(ENV.fetch("DATABASE_URL"), connect_timeout: 3)
  listening = conn.exec(
    "select count(*) from pg_stat_activity " \
    "where client_addr = inet_client_addr() and query like 'LISTEN %'"
  ).getvalue(0, 0).to_i
rescue PG::Error
  exit 0
end
exit(listening > 0 ? 0 : 1)
