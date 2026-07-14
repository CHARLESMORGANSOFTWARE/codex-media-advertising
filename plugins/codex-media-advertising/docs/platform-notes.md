# Platform notes

All six destinations share typed requests, optimization policy versions,
idempotency keys, receipts, caps, and pause rules. Routes remain independently
configured so one platform's outage does not flatten the campaign result.

| Destination | Default route | Important constraint |
| --- | --- | --- |
| Instagram | Browser or Meta API | Professional-account permissions, media-container readiness, and exact identity are required. |
| TikTok | Isolated browser | Direct-post API requires app review and user-facing consent; browser controls can change. |
| YouTube | YouTube API or Studio browser | Unverified API projects may be private-only; never claim public visibility without proof. |
| X | X API or isolated browser | User-context scopes and media upload authorization are required; API access is optional. |
| Facebook | Meta API or browser | Page identity and publish confirmation must be proved; an upload/container ID alone is insufficient. |
| Threads | Browser or supported API route | Signed-in identity and final post evidence must be destination-specific. |

## Browser fragility

Browser selectors are versioned and run in an isolated Chrome/CDP profile.
Require visible and enabled upload/submit controls, a matching account probe,
and platform-specific success evidence. A changed layout, expired session,
consent dialog, or disabled control is a blocked operation; do not bypass it
with manual clicks or a different account.

## Status truth

- `published`: final platform evidence proves the post exists.
- `submitted`: the platform accepted a request but final visibility is pending.
- `scheduled`: the platform confirmed a future publish time.
- `failed`: a classified terminal failure with no ambiguous mutation.
- `unknown` / `ambiguous_submit`: the mutation outcome is uncertain; do not
  retry automatically.
- `blocked`: policy, identity, auth, cap, pause, or readiness stopped work.

A queue entry, process exit, generic banner, container ID, or HTTP 2xx response
is not by itself publication proof. Inspect the destination receipt and report
its exact status, platform ID, URL when available, and path.
