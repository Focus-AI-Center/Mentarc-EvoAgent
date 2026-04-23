<!--
Thanks for the contribution! A few quick checks before you submit:

- One feature / fix per PR.
- If your change touches `execute_code`, auth, CORS, rate limiting, or any other
  security-sensitive surface, please update SECURITY.md alongside the code.
- Tool names are `snake_case`; agent / skill names are `kebab-case`.
- Ruff must pass locally: `ruff check . && ruff format --check .`
-->

## Summary

<!-- One or two sentences on what this PR does. Why, not what — the diff shows what. -->

## Type of change

- [ ] Bug fix
- [ ] New plugin / skill / sub-agent
- [ ] Framework change (agent loop / compressor / matcher / workspace)
- [ ] Docs / CI / infra only
- [ ] Other

## Checklist

- [ ] `ruff check .` passes
- [ ] `ruff format --check .` passes
- [ ] Added / updated user-visible docs (README, SOUL.md, or the relevant `.md`) if behaviour changed
- [ ] Updated SECURITY.md if a security surface changed
- [ ] No hardcoded secrets / API keys

## How I tested

<!-- Smoke steps, manual curl, CLI transcript, etc. -->
