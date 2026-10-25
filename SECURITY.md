# Security Policy

Suavecito's SoundBoost.ai Toolkit handles SoundBoost account tokens, reads and writes audio files on your machine, and can change REAPER's configuration. Security reports are taken seriously and handled privately.

## Supported versions

Only the latest release on `main` receives security fixes.

| Version | Supported |
| :--- | :---: |
| 1.1.x | ✅ |
| < 1.1 | ❌ |

## Reporting a vulnerability

**Please do not open a public issue, pull request, or discussion for a security problem.**

Report it privately through GitHub: go to the repository's **Security** tab, choose **Report a vulnerability**, and fill in the advisory form. Only the maintainers can see it.

Please include:

- What the issue is and what an attacker could do with it.
- Steps to reproduce, or a proof of concept.
- The affected version or commit, your OS, and how you run the toolkit (CLI, MCP over stdio, or MCP over HTTP).

You can expect:

- An acknowledgement within **3 business days**.
- An initial assessment within **7 days**, including whether we consider it in scope.
- A fix or mitigation plan for confirmed issues, with high-severity fixes targeted within **30 days**.
- Credit in the advisory and release notes if you'd like it.

## Scope

In scope: code in this repository, including:

- Credential handling (OS keyring vault, fallback credentials file, browser sign-in callback).
- The MCP server: tool annotations, file-path sandboxing (`SOUNDBOOST_ALLOWED_DIRS`), and HTTP serving with inbound OAuth.
- The SoundBoost API client.
- The REAPER integration: `REAPER.ini` edits, the Lua bridge daemon and its file mailbox, and the JSFX.

Out of scope:

- Vulnerabilities in SoundBoost.ai's own services. Report those to SoundBoost directly.
- Issues in third-party dependencies that are already publicly disclosed. Dependabot and `pip-audit` track these; a report is still welcome if the toolkit uses the dependency in an exploitable way.
- Attacks that require an attacker to already run code as your user on your machine.
- REAPER's own OSC and web control surfaces, which REAPER exposes to the local network when you enable them. The toolkit keeps them off unless you opt in.

## Hardening guidance for users

- Keep your SoundBoost token in the OS keyring (the default). Avoid putting `SOUNDBOOST_ACCESS_TOKEN` in shared shell profiles or MCP config files you sync or commit.
- Set `SOUNDBOOST_ALLOWED_DIRS` to the folders you actually work in.
- Only serve the MCP server over HTTP with `--oauth` and a tight `SOUNDBOOST_MCP_ALLOWED_EMAILS` list. Everyone on that list acts as your SoundBoost account.
- Leave REAPER's OSC and web surfaces off unless you need them. If you enable the web surface, set a username and password in REAPER's preferences and firewall the port.
