# Security Rules

Security-sensitive code should be centralized, testable, and boring. SiGMA is a
single-user local app, but users may still provide invalid paths, unusual files,
broken configuration, incompatible LLM APIs, or model-generated tool inputs. Do
not duplicate security decisions in components, tools, or routes.

## Filesystem And Path Safety

- Use shared path helpers for containment checks.
- Resolve paths before permission decisions.
- Never use string prefix checks for path containment.
- Uploaded filenames must be sanitized by the shared filename sanitizer.
- Reject path separators, traversal, empty names, and hidden names when a plain
  filename is expected.
- Symlink behavior must be considered when resolving user-supplied paths.

## Filesystem Permission Model

Agent file access follows the project permission model:

- Project sandbox and approved temporary areas may be writable.
- Writes outside allowed areas require user approval or must be rejected.
- Permission checks happen before tool execution.
- Individual tools should not invent their own write permission rules.

If the intended permission model changes, update this file and the central
permission implementation together.

## Uploads And Downloads

- Validate filenames and content type expectations.
- Do not trust client-provided paths.
- Treat uploaded content as untrusted even in single-user deployments.
- Downloads should resolve the requested path through the same path safety layer
  used for reads.
- Archive extraction must reject entries that escape the destination.
- Document and gracefully reject unsupported, encrypted, corrupt, oversized, or
  malformed documents.

## User Content Rendering

- Any HTML generated from Markdown, diffs, documents, model output, or user
  files must be sanitized before `dangerouslySetInnerHTML`.
- Prefer rendering structured content instead of raw HTML when practical.
- Sanitization fallback paths must also sanitize.

## LLM And External Provider Calls

- Non-streaming LLM calls go through `llm_service`.
- Structured LLM responses should be parsed and validated before use.
- Provider configuration errors should produce actionable user-facing errors,
  not crashes or silent fallback to a different model.
- OpenAI-compatible APIs may still differ in streaming format, tool-call shape,
  reasoning fields, error schema, and timeout behavior. Handle these variations
  defensively at the provider boundary.
- External provider calls that are not LLM calls should live behind a small
  client/service wrapper when they have retries, auth, rate limits, or response
  parsing.
- Do not log secrets, API keys, full prompts containing sensitive user data, or
  raw provider responses unless explicitly needed and redacted.

## Shell And Browser Tools

- Shell tools must run through the permission and safety layers.
- Read-only command classification must be conservative.
- Browser tools should avoid exposing raw privileged browser state unless the
  caller has a clear need.
- Tool inputs are untrusted even when produced by an LLM.

## Configuration And Secrets

- Backend configuration and all environment-variable access go through
  `core/config.py`; never read `os.environ` or `os.getenv()` directly elsewhere.
- Secrets should live in `settings.yaml`, not hardcoded constants.
- Logs must not include secrets.

## Security Review Checklist

For security-sensitive changes, check:

- Can user input alter a path, URL, command, selector, prompt, or rendered HTML?
- Is validation centralized?
- Is the failure mode reject-by-default?
- Are symlinks, traversal, separators, empty names, and hidden names handled?
- Are errors informative without leaking secrets?

## Rule Maintenance

When path, permission, rendering, provider, tool, or secret-handling rules
change, update this file in the same change and mention it in the handoff or PR.
