# Deployment contract

This document defines the public, server-agnostic deployment contract. It does not contain hostnames, credentials, private schedules, or machine-specific paths.

## Release channels

Code and private configuration use separate update channels:

- Application code is deployed only from immutable semantic-version tags such as `v0.1.0`.
- The `main` branch is for development and must not be pulled directly into production.
- Private rules may be updated independently from the private configuration repository after validation.

The production updater described below is the target design. It is not active until the server-specific operations document records that the initial migration has completed.

## Persistent boundaries

Deployments must keep these resources outside versioned application releases:

- secrets and environment configuration;
- private calendar rules;
- retained calendar history and other state;
- generated public ICS output.

Replacing application code must not delete or silently reset any persistent resource.

## Safe update sequence

An automated updater should:

1. Acquire a lock so calendar generation and another deployment cannot overlap.
2. Fetch the selected immutable application tag and the private configuration revision.
3. Prepare a new release directory without modifying the active release.
4. Create a fresh virtual environment and install the declared dependencies.
5. Validate the private rules and run automated tests or an equivalent smoke test.
6. Back up affected persistent state and apply every pending versioned migration.
7. Atomically switch the active-release pointer.
8. Restart or reload only the required services and verify a successful calendar run.
9. Roll back the active-release pointer if verification fails.

A failed fetch, installation, validation, migration, or smoke test must leave the previous release and configuration active.

## Migrations

Changes that affect server state must use ordered, versioned, idempotent migrations. Examples include changing a state-file format, moving a persistent path, or updating a systemd unit.

Each migration must:

- detect whether it has already completed;
- validate preconditions before writing;
- back up material state before changing it;
- fail without marking itself complete when verification fails;
- document whether and how it can be rolled back.

New secrets cannot be generated or inferred by deployment automation. A release that requires a new secret must stop with a clear message until an administrator supplies it.

## Configuration updates

Private rules may update more frequently than application releases. The updater must validate a candidate rules file before activating it and retain the last valid revision when validation fails.

Access to the private configuration repository should use a repository-scoped, read-only deploy key. The key and the checked-out private data must never be stored in this public repository.
