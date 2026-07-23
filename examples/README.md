# Offline synthetic fixtures

These files are original synthetic fixtures distributed under the repository's MIT license.
They contain no production endpoint, credential, personal data, or copied provider response.

- `openapi.yaml` demonstrates an OpenAPI operation with lifecycle metadata.
- `manual-feed.yaml` demonstrates a manually curated service-level retirement.
- `consumers.json` maps local synthetic consumers to affected endpoints.

The demo and test suite read these files locally and do not contact an external API. The
loopback fixture server is available with:

```bash
python -m sunset_sentinel_api.adapters.fixture_server
```
