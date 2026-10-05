# Crease

Preview a public URL on both iPhone Duo screens: cover 5.4 in (466 by 678 points) and inner 7.6 in (951 by 669 open). The crease is a 40-point band, not half the width.

Live: https://crease.hermz.xyz

Not a CSS autopsy. Bare does that. Crease is two viewports.

## Limits

- Public `http`/`https` only, ports 80 and 443
- Private, loopback, and link-local addresses refused
- 10s timeout, 1 MB HTML, 20 previews per IP per hour
- Sites that fight bots, demand login, or break out of iframes will look empty or wrong

## Run

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8880
```

Health: `GET /health`

MIT. Built on holy-hermes.
