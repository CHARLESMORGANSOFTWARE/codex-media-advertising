# Authentication and account ownership

Every coworker connects their own services. The plugin never ships an account
identifier, browser profile, cookie, OAuth client secret, access token, or
relay state. Keep secret files in `~/.codex-media-ads/secrets/`; the setup
service enforces private permissions and rejects symlinks.

## API accounts

- **Meta (Instagram and Facebook):** configure the user's Meta app and page or
  professional account through the supported API route. App review, scopes,
  page roles, media-container readiness, and destination identity must be
  confirmed by the account owner.
- **Google (YouTube):** complete OAuth with the user's Google project and
  channel. Store only a reference to the credential in nonsecret config. An
  unverified project may be restricted to private uploads; setup must report
  that restriction instead of claiming a public post.
- **X:** provide user-context API credentials with the scopes required for media
  upload and posting. If API access is unavailable or app review is incomplete,
  use the isolated browser route after confirming the exact signed-in identity.

The configuration records `expected_identity` and a route mode; it does not
store secret values. Use `codex-media-ads publish probe --platform PLATFORM`
before enabling any channel. A probe that cannot prove identity is blocked.

## Browser sessions

Instagram, TikTok, YouTube Studio, Facebook, X, and Threads browser routes use a
plugin-owned isolated Chrome/CDP profile. Sign in interactively as the intended
account, complete any consent or verification steps, then run the platform
probe. Do not copy a personal Chrome profile into the repository, and do not
switch to another account when a probe mismatches.

TikTok direct posting and some Meta/YouTube features require user-facing consent
or app review. The browser session remains user-owned and may need periodic
re-authentication. A visible page or generic success banner is not proof of a
post; receipt evidence is required.

## Safe import

```bash
codex-media-ads setup --import-secret PROVIDER_NAME=PATH --format json
```

The command copies the credential into private state without printing it and
returns only a redacted destination. If import fails, fix ownership or
permissions; never weaken the secret file mode or replace the path with a
symlink.
