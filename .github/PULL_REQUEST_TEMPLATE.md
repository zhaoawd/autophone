## Summary

<!-- Briefly describe what this PR changes and why. -->

## Verification Checklist

- [ ] `uv run --only-group dev ruff check .`
- [ ] `uv run --group platform python scripts/generate_public_docs.py --check`
- [ ] `python3 scripts/check_public_content.py`
- [ ] Relevant pytest / `scripts/test_public_platform.sh` / Playwright E2E checks pass
- [ ] No private credentials, host paths, or unredacted device/user data included
