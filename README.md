# Comrade

Comrade is an AI teammate for self-organising engineering teams.

Teams use one shared space to talk, keep work visible, connect a repository, and ask an agent to investigate or make changes. Comrade turns team conversations, documents, and repository activity into a cited team wiki. It can inspect code, run it in an isolated sandbox, and prepare a pull request—while people retain control over consequential actions.

## What it does

- **Shared threads:** public team conversations, restricted threads for selected members, and persistent agent sessions.
- **Team memory:** a cited, versioned wiki compiled from chat, uploaded documents, and GitHub activity.
- **Repository work:** GitHub App connection, per-team and per-thread workspaces, code search, edits, sandboxed commands, and proposed pull requests.
- **Visible execution:** agent turns stream tool activity, command output, and file diffs into the thread.
- **Human control:** consent is bound to the exact proposed action; execution uses a separate database role and cannot silently change the default branch.
- **Team safety:** row-level security isolates teams, agent identity is server-bound, and untrusted text is treated as data rather than instruction.

## Hosted product

Open Comrade at [https://13-62-11-26.nip.io](https://13-62-11-26.nip.io).

## Further reading

- [System architecture](docs/architecture.md)
- [Deployment and operations](docs/deployment.md)
