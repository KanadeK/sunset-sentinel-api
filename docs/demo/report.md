# Sunset Sentinel lifecycle report

Generated: `2026-07-23T00:00:00Z`

Records: **3**

## fixture-api GET /v1/orders

- State: `deprecated`
- Scope: `endpoint`
- Deprecation: `2026-06-30T23:59:59Z`
- Sunset: `2026-09-30T23:59:59Z`
- Scores: urgency **75**, blast radius **27**, priority **75**
- Date conflict: `no`
- Signal sources: `openapi`
- Consumers: Checkout Web (`critical`)
- Documentation: <http://127.0.0.1:8765/migration/orders>
- Replacements: GET /v2/orders

## fixture-api GET /v1/search

- State: `deprecated_date_unknown`
- Scope: `endpoint`
- Deprecation: `unknown`
- Sunset: `unknown`
- Scores: urgency **60**, blast radius **12**, priority **60**
- Date conflict: `no`
- Signal sources: `openapi`
- Consumers: Operations Reporter (`medium`)
- Documentation: _none_
- Replacements: _not specified_

## partner-catalog (service)

- State: `deprecation_scheduled`
- Scope: `service`
- Deprecation: `2026-10-01T00:00:00Z`
- Sunset: `2027-01-31T23:59:59Z`
- Scores: urgency **45**, blast radius **32**, priority **45**
- Date conflict: `no`
- Signal sources: `manual`
- Consumers: Checkout Web (`critical`)
- Documentation: <https://docs.example.test/catalog-v2-migration>
- Replacements: partner-catalog-v2
