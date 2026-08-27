/**
 * Model-role configuration semantics shared by every "is this role set up?"
 * decision in the UI. Mirrors the backend's `Settings.model_settings_for_role`:
 * a role with `reuse` set inherits the entire settings block of its target
 * role, and the backend leaves its own `model` field empty while doing so.
 */

/**
 * Effective settings block for a model role, following the `reuse` chain.
 * The `seen` set guards against cycles; the backend validator rejects them,
 * so a cycle can only appear in a stale client-side draft, where resolving
 * to the role's own (reuse-carrying) block is a safe fallback.
 */
export function resolveModelRoleConfig(config, role, seen = new Set()) {
  const roleConfig = config?.models?.[role] || {}
  if (!roleConfig.reuse || seen.has(role)) return roleConfig
  return resolveModelRoleConfig(config, roleConfig.reuse, new Set([...seen, role]))
}

/**
 * Whether a model role is configured. Must read the reuse-resolved model
 * name: reading the raw `models.<role>.model` field misreports a role that
 * legally reuses another one (empty `model`, `reuse: "supervisor"`) as
 * unconfigured.
 */
export function isModelRoleConfigured(config, role) {
  return !!resolveModelRoleConfig(config, role)?.model
}
