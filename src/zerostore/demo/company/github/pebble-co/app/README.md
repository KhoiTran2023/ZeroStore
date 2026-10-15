# Pebble

Shared to-do boards for small teams. This repo holds the web app, the API and the help center docs.

## Running it locally

Install Node 22 and pnpm, then run `pnpm install` and `pnpm dev`. The API needs a local Postgres; `pnpm db:up` starts one.

## Deploys

Every merge to main deploys to production after the tests pass. Ben Okafor owns the release process.
