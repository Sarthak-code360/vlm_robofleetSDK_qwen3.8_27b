# Security

## Reporting a problem

Please report security problems privately through GitHub:
**Security > Report a vulnerability** on the repository page. Do not open a public issue for them.

## Keeping your setup safe

- `config/printer.json` contains the printer's access code, which gives full control of the printer. It is git-ignored.
  Never commit it or paste it into issues.
- Keep API keys (`OPENROUTER_API_KEY`, `VLM_API_KEY`) in environment variables, never in code.
- Bambu printers use a self-signed certificate that robofleet cannot verify. Only control printers on a network you
  trust, and reach them remotely through a private VPN (e.g. Tailscale), never by opening ports to the internet.
- A model server reachable from other machines should require an API key (`--api-key`).
- Keep the human confirmation before physical actions (the `PRINT` prompt) unless someone can see the machine.
