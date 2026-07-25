## Change

Describe the user-visible change and why it is needed.

## Safety checklist

- [ ] `scripts/verify-release.sh` passed locally.
- [ ] No `.env`, database, Cookie, API key, webhook, production log, or real import config is included.
- [ ] The change does not restart or modify Nginx, light-proxy, new-api, or another CY16 service.
- [ ] Database migrations remain compatible with the previous image and rollback path.
- [ ] A CODEOWNER reviewed the change and the required `verify` check passed.
