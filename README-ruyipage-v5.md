# HTTP V5 workflow

Workflow: `.github/workflows/register-ruyipage-v5.yml`

V5 keeps the V4 persistent HTTP registration and captcha-gate submission flow.
It adds independent selections for the solver, Arkose browser adapter, and
registration network route.

## Workflow inputs

- `count`: `1..256`
- `max_parallel`: `1..20`
- `solver`: `v11`, `yescaptcha`, or `capmonster`
- `yescaptcha_key`: optional manual key; the `YESCAPTCHA_API_KEY` repository
  secret takes precedence
- `capmonster_key`: optional manual key; the `CAPMONSTER_API_KEY` repository
  secret takes precedence
- `browser`: `ruyipage` or `cloakbrowser`
- `network`: `direct` or `proxy`
- `proxy_file`: repository-relative proxy pool path, default `IP.txt`

## Required secrets

- `YESCAPTCHA_API_KEY` when `solver=yescaptcha`
- `CAPMONSTER_API_KEY` when `solver=capmonster`

V11 does not require an API key. The workflow starts the local V11 service only
for `solver=v11`. For compatibility with the V2 workflow, either provider key
can instead be entered in the matching workflow input. Provider credentials are
validated before the registration matrix starts and are masked in every job.

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
- `yescaptcha`: opens the selected browser and uses the per-image
  `FunCaptchaClassification` API. The solver loop is ported from the verified
  local V4 implementation: it reads the rendered question again for every
  wave, removes only the `(n of m)` suffix, accepts compact RTIG strips, retries
  transient provider failures with the same image, and uses the local V4
  RuyiPage baseline of balanced native clicks with a random `250..600ms` gap
  between arrow clicks. CloakBrowser keeps its existing adapter click path.
- `capmonster`: sends the HTTP-captured Arkose context to CapMonster and receives
  the completed token directly. With `network=proxy`, the same per-job proxy is
  included in the CapMonster task; direct mode uses CapMonster's built-in proxy.
  The protocol session and CapMonster task share the current Windows Chrome 150
  user agent by default (`V5_USER_AGENT` or `--protocol-user-agent` overrides it).
  No solver browser is needed in this branch.

All branches submit the resulting token through the same persisted V4 HTTP
session. Registration country remains fixed to `GBR`.
