# HTTP V5 workflow

Workflow: `.github/workflows/register-ruyipage-v5.yml`

V5 keeps the V4 persistent HTTP registration and captcha-gate submission flow.
It adds independent selections for the solver, Arkose browser adapter, and
registration network route.

## Workflow inputs

- `count`: `1..256`
- `max_parallel`: `1..20`
- `solver`: `v11`, `yescaptcha`, or `capmonster`
- `browser`: `ruyipage` or `cloakbrowser`
- `network`: `direct` or `proxy`
- `proxy_file`: repository-relative proxy pool path, default `IP.txt`

## Required secrets

- `YESCAPTCHA_API_KEY` when `solver=yescaptcha`
- `CAPMONSTER_API_KEY` when `solver=capmonster`

V11 does not require an API key. The workflow starts the local V11 service only
for `solver=v11`.

## Proxy pool

V5 accepts either form per non-empty line:

```text
ip:port:username:password
username:password@ip:port
```

Blank lines and lines beginning with `#` are ignored. The prepare job validates
the complete pool, rejects duplicate normalized proxies, and requires at least
`count` entries. Matrix job `N` receives line `N`, so concurrent jobs never
share a pool entry. Proxy credentials are written to `GITHUB_ENV` only after
GitHub log masking is enabled.

With `network=direct`, `IP.txt` is not read and the GitHub runner route is used.

## Solver behavior

- `v11`: opens the selected browser, captures Arkose challenge images, calls the
  local V11 service, submits each answer, and returns the completed token.
- `yescaptcha`: opens the selected browser and reuses the V2 per-image
  `FunCaptchaClassification` loop.
- `capmonster`: sends the HTTP-captured Arkose context to CapMonster and receives
  the completed token directly. No solver browser is needed in this branch.

All branches submit the resulting token through the same persisted V4 HTTP
session. Registration country remains fixed to `GBR`.
