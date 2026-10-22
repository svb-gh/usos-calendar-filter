# Repository instructions

These instructions apply to the entire public source repository.

## Language and privacy

- Write documentation, comments, examples, commit messages, and user-facing messages in English.
- Never commit `.env`, `rules.yaml`, generated `.ics` files, private USOS URLs, access keys, real schedules, instructor names, or server credentials.
- Keep `rules.example.yaml` fictional and safe to publish.
- A sibling private repository named `usos-calendar-filter-config` may be available for local validation. Never copy its private contents into this repository, logs, patches, issues, or responses.

## Quality requirements

- Preserve backward compatibility for existing ICS state, event UIDs, environment variables, and rule syntax unless a documented migration is provided.
- Validate changes with pytest, Ruff linting, Ruff formatting, and private-rule validation when the sibling configuration repository is available.
- Keep `pyproject.toml` as the single source of Python dependencies.

## Releases and deployment

- Read `docs/deployment.md` before changing installation, persistence, configuration, systemd, or deployment behavior.
- Production code must be deployed only from immutable semantic-version tags such as `v0.1.0`. Never deploy production directly from `main`.
- Never move, overwrite, or recreate an existing release tag. Create a new version instead.
- Create a release tag only after CI passes for the exact commit and the package version matches the tag.
- Persistent state, secrets, generated output, and private configuration must remain outside release directories.
- Server-specific information belongs in the private configuration repository's `SERVER_OPERATIONS.md`, not here.
- The automatic server updater is planned but not implemented yet. Do not claim that production auto-deployment is active until the private operations document records a completed migration.
