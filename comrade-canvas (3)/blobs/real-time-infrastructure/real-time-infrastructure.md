# Real-time infrastructure

Supabase Realtime for all live updates: group chat, private AI threads, and consent queue notifications. Consent queue rule: always write to DB first, then push the live ping. Realtime is the notification layer; Postgres is the record. Users who are offline still see all pending consent items when they open the app.