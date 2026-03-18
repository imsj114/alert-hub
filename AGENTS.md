# Agent Notes

- Use `scripts/send_event.sh` as the primary sender-side helper for manual tests, shell automation, cron jobs, and CI.
- Keep the Alert Hub request contract unchanged when modifying sender tooling:
  - `POST /api/v1/events`
  - `Content-Type: application/json`
  - `X-AlertHub-Sender`
  - `X-AlertHub-Timestamp`
  - `X-AlertHub-Signature`
- Prefer `--payload-file` for richer payloads in shell workflows rather than expanding the bash script into a complex JSON builder.
- The Python helper at `scripts/send_event.py` remains available, but the documented/default sender path is the bash script.
