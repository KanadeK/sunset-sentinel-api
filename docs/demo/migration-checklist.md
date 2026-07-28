# API migration checklist

Generated: `2026-07-23T00:00:00Z`

## fixture-api GET /v1/orders

- [ ] <!-- ss:assign-owner:682773d21ceb --> Assign a migration owner.
- [ ] <!-- ss:verify-docs:583db322fa80 --> Verify the provider documentation and lifecycle dates.
- [ ] <!-- ss:inventory:b07c75b77f80 --> Confirm all affected local consumers.
- [ ] <!-- ss:confirm-replacement:496099bd22b9 --> Confirm the documented replacement API.
- [ ] <!-- ss:contract-tests:230e888bd60e --> Add or update contract and regression tests.
- [ ] <!-- ss:stage:ffae4e2faf63 --> Validate the migration in staging.
- [ ] <!-- ss:production:ef48129f57a2 --> Deploy the migration to production.
- [ ] <!-- ss:monitor:ea0163dbc2c7 --> Monitor errors, latency, and fallback usage.
- [ ] <!-- ss:remove-old:9c3dce9c742e --> Remove the deprecated dependency and obsolete credentials.

## fixture-api GET /v1/search

- [ ] <!-- ss:assign-owner:afb75ea9a452 --> Assign a migration owner.
- [ ] <!-- ss:verify-docs:b0c29c956369 --> Verify the provider documentation and lifecycle dates.
- [ ] <!-- ss:inventory:35b69ffa14ba --> Confirm all affected local consumers.
- [ ] <!-- ss:select-replacement:981c3c7c978f --> Select and document a replacement API.
- [ ] <!-- ss:contract-tests:2f14b993d233 --> Add or update contract and regression tests.
- [ ] <!-- ss:stage:d57632f89337 --> Validate the migration in staging.
- [ ] <!-- ss:production:9bd27b15ece8 --> Deploy the migration to production.
- [ ] <!-- ss:monitor:b817816c40a8 --> Monitor errors, latency, and fallback usage.
- [ ] <!-- ss:remove-old:c71fda3febc3 --> Remove the deprecated dependency and obsolete credentials.

## partner-catalog (service)

- [ ] <!-- ss:assign-owner:3b9e6559887e --> Assign a migration owner.
- [ ] <!-- ss:verify-docs:9881fec4dc6b --> Verify the provider documentation and lifecycle dates.
- [ ] <!-- ss:inventory:b0c463a377eb --> Confirm all affected local consumers.
- [ ] <!-- ss:confirm-replacement:cb11a517f59c --> Confirm the documented replacement API.
- [ ] <!-- ss:contract-tests:2d0db69e96af --> Add or update contract and regression tests.
- [ ] <!-- ss:stage:b1920cc0ed36 --> Validate the migration in staging.
- [ ] <!-- ss:production:eba933ffb5e7 --> Deploy the migration to production.
- [ ] <!-- ss:monitor:2266a128ddda --> Monitor errors, latency, and fallback usage.
- [ ] <!-- ss:remove-old:e29c03bb9886 --> Remove the deprecated dependency and obsolete credentials.
