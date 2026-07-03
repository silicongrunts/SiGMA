import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { AlertTriangle, BookOpen, Check, CheckCircle2, ChevronDown, Circle, Code2, Eye, Globe2, Link2, Loader2, Minus, RotateCcw, Save, Sparkles, XCircle, X } from 'lucide-react'
import { systemAPI } from '../api'
import { createSSEStreamParser } from '../utils/sse'
import { toastError, toastSuccess } from './Toast'

const MODEL_ROLES = [
  ['supervisor', 'system.role.supervisor'],
  ['ra', 'system.role.ra'],
  ['vision', 'system.role.vision'],
  ['draw', 'system.role.draw'],
  ['embedding', 'system.role.embedding'],
  ['rerank', 'system.role.rerank'],
]

const MODEL_ROLE_DETAILS = {
  supervisor: {
    badge: 'system.badge.primary',
    description: 'system.desc.supervisor',
    icon: Sparkles,
  },
  ra: {
    badge: 'system.badge.research',
    description: 'system.desc.ra',
    icon: Link2,
  },
  vision: {
    badge: 'system.badge.multimodal',
    description: 'system.desc.vision',
    icon: Eye,
  },
  draw: {
    badge: 'system.badge.image',
    description: 'system.desc.draw',
    icon: Sparkles,
  },
  embedding: {
    badge: 'system.badge.retrieval',
    description: 'system.desc.embedding',
    icon: Link2,
  },
  rerank: {
    badge: 'system.badge.retrieval',
    description: 'system.desc.rerank',
    icon: Link2,
  },
}

const ROLE_REUSE_OPTIONS = {
  ra: [
    ['', 'system.reuse.independent'],
    ['supervisor', 'system.reuse.supervisor'],
  ],
  vision: [
    ['', 'system.reuse.independent'],
    ['ra', 'system.reuse.ra'],
    ['supervisor', 'system.reuse.supervisor'],
  ],
}

const LOCAL_CAPABLE_ROLES = new Set(['embedding', 'rerank'])

const ADVANCED_ROLES = new Set(['supervisor', 'ra', 'vision'])
const CONTEXT_BUDGET_ROLES = new Set(['supervisor', 'ra', 'vision'])

// The first entry (empty value) means "do not send the parameter"; the rest
// match LiteLLM's `reasoning_effort` Literal and are mapped per provider.
const REASONING_EFFORT_OPTIONS = [
  ['', 'system.reasoningEffortOption.noOverride'],
  ['none', 'system.reasoningEffortOption.none'],
  ['minimal', 'system.reasoningEffortOption.minimal'],
  ['low', 'system.reasoningEffortOption.low'],
  ['medium', 'system.reasoningEffortOption.medium'],
  ['high', 'system.reasoningEffortOption.high'],
  ['xhigh', 'system.reasoningEffortOption.xhigh'],
  ['default', 'system.reasoningEffortOption.default'],
]

const SEARCH_ENGINES = [
  ['https://www.google.com/search?q=', 'system.engine.google'],
  ['https://www.bing.com/search?q=', 'system.engine.bing'],
  ['https://duckduckgo.com/?q=', 'system.engine.duckduckgo'],
  ['https://search.yahoo.com/search?p=', 'system.engine.yahoo'],
  ['https://yandex.com/search/?text=', 'system.engine.yandex'],
  ['https://www.baidu.com/s?wd=', 'system.engine.baidu'],
]

function cloneConfig(config) {
  return JSON.parse(JSON.stringify(config || {}))
}

function autoCompressThreshold(maxContextLength) {
  const max = Number(maxContextLength)
  if (!Number.isInteger(max) || max < 50000) return ''
  return Math.min(200000, Math.floor(max * 0.8), max - 20000)
}

function roleFromValidationMessage(message) {
  return String(message || '').match(/models\.(supervisor|ra|vision|draw|embedding|rerank)/)?.[1]
}

function humanizeSettingsError(message, t) {
  const text = String(message || '').trim()
  const role = roleFromValidationMessage(text)
  const roleName = role ? t(roleLabelKey(role)) : t('system.modelLabel')

  if (text.includes('max_context_length is required when compress_threshold is set')) {
    return t('system.error.contextRequired', { role: roleName })
  }
  if (text.includes('max_context_length must be at least 50000 when compress_threshold is set')) {
    return t('system.error.maxContextTooSmall', { role: roleName })
  }
  if (text.includes('compress_threshold must be between 30000 and max_context_length - 20000')) {
    return t('system.error.thresholdRange', { role: roleName })
  }

  const valueError = text.match(/Value error,\s*([^\[]+)/)
  if (valueError?.[1]) return valueError[1].trim()
  return text.replace(/\s*For further information visit https:\/\/errors\.pydantic\.dev\/\S+/g, '')
}

function getNested(object, path) {
  return path.reduce((current, key) => current?.[key], object)
}

function updateNested(object, path, value) {
  const next = cloneConfig(object)
  let current = next
  for (let i = 0; i < path.length - 1; i += 1) {
    const key = path[i]
    current[key] = current[key] || {}
    current = current[key]
  }
  current[path[path.length - 1]] = value
  return next
}

function resolveRoleConfig(config, role, seen = new Set()) {
  const roleConfig = config?.models?.[role] || {}
  if (!roleConfig.reuse || seen.has(role)) return roleConfig
  return resolveRoleConfig(config, roleConfig.reuse, new Set([...seen, role]))
}

function endpointSummary(roleConfig) {
  if (!roleConfig?.model) return 'system.noModelConfigured'
  return [roleConfig.provider, roleConfig.model].filter(Boolean).join('/') || roleConfig.model
}

function roleLabelKey(role) {
  return MODEL_ROLES.find(([value]) => value === role)?.[1] || role
}

function toOptionalPositiveInt(value) {
  if (value === '') return null
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : value
}

function NumberField({ label, value, onChange, min = 1, max, step, placeholder, disabled = false }) {
  return (
    <label className="block">
      <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">{label}</span>
      <input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value ?? ''}
        placeholder={placeholder}
        disabled={disabled}
        onChange={e => {
          if (e.target.value === '') return onChange('')
          const parsed = Number(e.target.value)
          return onChange(Number.isNaN(parsed) ? '' : parsed)
        }}
        className="w-full px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm disabled:opacity-60 disabled:cursor-not-allowed"
      />
    </label>
  )
}

function TextField({ label, value, onChange, placeholder, type = 'text' }) {
  return (
    <label className="block">
      <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">{label}</span>
      <input
        type={type}
        value={value ?? ''}
        placeholder={placeholder}
        onChange={e => onChange(e.target.value)}
        className="w-full px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm"
      />
    </label>
  )
}

function Section({ title, children }) {
  return (
    <section className="border-t border-gray-100 dark:border-gray-800 first:border-t-0 py-5 first:pt-0">
      <h3 className="text-sm font-black text-gray-900 dark:text-gray-100 mb-4">{title}</h3>
      {children}
    </section>
  )
}

function SettingsGroup({ title, icon: Icon, children }) {
  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm">
      <div className="flex items-center gap-3 mb-4">
        <div className="w-9 h-9 rounded-lg bg-sigma-50 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-400 flex items-center justify-center flex-shrink-0">
          <Icon className="w-4 h-4" />
        </div>
        <h4 className="text-sm font-black text-gray-900 dark:text-gray-100">{title}</h4>
      </div>
      {children}
    </div>
  )
}

function AdvancedSection({ role, roleConfig, effectiveConfig, isReused, reuseTarget, onChange }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  if (!ADVANCED_ROLES.has(role)) return null

  // When reused, the values come from the inherited config and inputs are read-only.
  const sourceConfig = isReused ? (effectiveConfig || {}) : (roleConfig || {})
  const temperature = sourceConfig.temperature ?? ''
  const topP = sourceConfig.top_p ?? ''
  const reasoningEffort = sourceConfig.reasoning_effort ?? ''
  const inheritedLabel = isReused
    ? t('system.advancedSettingsInherited', { role: t(roleLabelKey(reuseTarget)) })
    : t('system.advancedSettings')

  const handleNumberChange = (key, value) => {
    if (isReused) return
    onChange(['models', role, key], value === '' ? null : value)
  }
  const handleReasoningChange = (value) => {
    if (isReused) return
    onChange(['models', role, 'reasoning_effort'], value === '' ? null : value)
  }

  return (
    <div className="mt-3 border-t border-gray-100 dark:border-gray-800 pt-3">
      <button
        type="button"
        onClick={() => setOpen(prev => !prev)}
        className="flex items-center gap-1 text-xs font-bold text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
      >
        {inheritedLabel}
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3">
          <NumberField
            label={t('system.temperature')}
            value={temperature}
            min={0}
            max={2}
            step={0.01}
            placeholder={t('system.temperatureHint')}
            disabled={isReused}
            onChange={v => handleNumberChange('temperature', v)}
          />
          <NumberField
            label={t('system.topP')}
            value={topP}
            min={0}
            max={1}
            step={0.01}
            placeholder={t('system.topPHint')}
            disabled={isReused}
            onChange={v => handleNumberChange('top_p', v)}
          />
          <label className="block">
            <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">{t('system.reasoningEffort')}</span>
            <div className="relative">
              <select
                value={reasoningEffort}
                disabled={isReused}
                onChange={e => handleReasoningChange(e.target.value)}
                className="w-full appearance-none px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {REASONING_EFFORT_OPTIONS.map(([value, labelKey]) => (
                  <option key={value || 'no-override'} value={value}>{t(labelKey)}</option>
                ))}
              </select>
              <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
            </div>
          </label>
        </div>
      )}
    </div>
  )
}

function WarningDialog({ onCancel, onConfirm }) {
  const { t } = useTranslation()
  return (
    <div className="absolute inset-0 z-20 flex items-center justify-center bg-gray-900/30 p-4">
      <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-2xl shadow-2xl border border-gray-100 dark:border-gray-700 p-6">
        <div className="flex items-start gap-4">
          <div className="w-10 h-10 rounded-xl bg-amber-100 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400 flex items-center justify-center flex-shrink-0">
            <AlertTriangle className="w-5 h-5" />
          </div>
          <div>
            <h3 className="text-base font-black text-gray-900 dark:text-gray-100">{t('system.editYamlDirect')}</h3>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 leading-6">
              {t('system.yamlWarning')}
            </p>
          </div>
        </div>
        <div className="flex justify-end gap-2 mt-6">
          <button onClick={onCancel} className="px-4 py-2 text-sm font-bold text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg">
            {t('common.cancel')}
          </button>
          <button onClick={onConfirm} className="px-4 py-2 text-sm font-bold text-white bg-amber-600 hover:bg-amber-700 rounded-lg">
            {t('common.continue')}
          </button>
        </div>
      </div>
    </div>
  )
}

const CHECK_ITEM_STATUS = {
  pending: { icon: Circle, color: 'text-gray-300', bg: '' },
  running: { icon: Loader2, color: 'text-sigma-600', bg: '' },
  pass: { icon: CheckCircle2, color: 'text-green-500', bg: '' },
  fail: { icon: XCircle, color: 'text-red-500', bg: '' },
  skip: { icon: Minus, color: 'text-gray-400', bg: '' },
}

function CheckItemRow({ label, status, message, reason }) {
  const { t } = useTranslation()
  const style = CHECK_ITEM_STATUS[status] || CHECK_ITEM_STATUS.pending
  const Icon = style.icon
  return (
    <div className="flex items-start gap-3 py-1.5">
      <Icon className={`w-4 h-4 mt-0.5 flex-shrink-0 ${style.color} ${status === 'running' ? 'animate-spin' : ''}`} />
      <div className="min-w-0">
        <div className="text-sm font-bold text-gray-900 dark:text-gray-100">{t(label)}</div>
        {status === 'fail' && message && <div className="text-xs text-red-500 dark:text-red-400 mt-0.5">{message}</div>}
        {status === 'skip' && reason && <div className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">{reason}</div>}
        {status === 'running' && <div className="text-xs text-gray-400 dark:text-gray-500 mt-0.5">{t('system.testing')}</div>}
      </div>
    </div>
  )
}

function SaveConfirmDialog({ onCheckAndSave, onSaveDirect, onCancel }) {
  const { t } = useTranslation()
  return (
    <div className="absolute inset-0 z-20 flex items-center justify-center bg-gray-900/30 p-4">
      <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-2xl shadow-2xl border border-gray-100 dark:border-gray-700 p-6 animate-in zoom-in duration-200">
        <div className="flex items-start gap-4">
          <div className="w-10 h-10 rounded-xl bg-sigma-50 dark:bg-sigma-600/20 text-sigma-600 dark:text-sigma-400 flex items-center justify-center flex-shrink-0">
            <Save className="w-5 h-5" />
          </div>
          <div>
            <h3 className="text-base font-black text-gray-900 dark:text-gray-100">{t('system.saveSettings')}</h3>
            <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 leading-6">
              {t('system.saveVerifyPrompt')}
            </p>
            <p className="text-xs text-amber-600 dark:text-amber-400 mt-2 leading-5">
              {t('system.saveVerifyNote')}
            </p>
          </div>
        </div>
        <div className="flex flex-col gap-2 mt-6">
          <button onClick={onCheckAndSave} className="w-full px-4 py-2.5 text-sm font-bold text-white bg-sigma-600 hover:bg-sigma-700 rounded-lg transition-colors">
            {t('system.checkAndSave')}
          </button>
          <div className="flex gap-2">
            <button onClick={onSaveDirect} className="flex-1 px-4 py-2 text-sm font-bold text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors">
              {t('system.saveWithoutCheck')}
            </button>
            <button onClick={onCancel} className="flex-1 px-4 py-2 text-sm font-bold text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors">
              {t('common.cancel')}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function CheckProgressDialog({ results }) {
  const { t } = useTranslation()
  return (
    <div className="absolute inset-0 z-20 flex items-center justify-center bg-gray-900/30 p-4">
      <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-2xl shadow-2xl border border-gray-100 dark:border-gray-700 p-6 animate-in zoom-in duration-200">
        <div className="flex items-center gap-3 mb-4">
          <Loader2 className="w-5 h-5 text-sigma-600 animate-spin" />
          <h3 className="text-base font-black text-gray-900 dark:text-gray-100">{t('system.verifying')}</h3>
        </div>
        <div className="max-h-80 overflow-y-auto space-y-0.5">
          {results.map(r => <CheckItemRow key={r.role} {...r} />)}
        </div>
      </div>
    </div>
  )
}

function CheckResultsDialog({ results, onForceSave, onBackToEdit }) {
  const { t } = useTranslation()
  const passed = results.filter(r => r.status === 'pass').length
  const failed = results.filter(r => r.status === 'fail').length
  const skipped = results.filter(r => r.status === 'skip').length

  return (
    <div className="absolute inset-0 z-20 flex items-center justify-center bg-gray-900/30 p-4">
      <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-2xl shadow-2xl border border-gray-100 dark:border-gray-700 p-6 animate-in zoom-in duration-200">
        <div className="flex items-center gap-3 mb-4">
          {failed > 0 ? (
            <XCircle className="w-5 h-5 text-red-500" />
          ) : (
            <CheckCircle2 className="w-5 h-5 text-green-500" />
          )}
          <h3 className="text-base font-black text-gray-900 dark:text-gray-100">
            {failed > 0 ? t('system.checksFailed', { count: failed }) : t('system.allChecksPassed')}
          </h3>
        </div>
        <div className="max-h-72 overflow-y-auto space-y-0.5 mb-4">
          {results.map(r => <CheckItemRow key={r.role} {...r} />)}
        </div>
        <div className="text-xs text-gray-400 dark:text-gray-500 text-center mb-4">
          {t('system.checkSummary', { passed, failed, skipped })}
        </div>
        {failed > 0 ? (
          <div className="flex gap-2">
            <button onClick={onForceSave} className="flex-1 px-4 py-2.5 text-sm font-bold text-white bg-amber-600 hover:bg-amber-700 rounded-lg transition-colors">
              {t('system.forceSave')}
            </button>
            <button onClick={onBackToEdit} className="flex-1 px-4 py-2.5 text-sm font-bold text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors">
              {t('system.backToEdit')}
            </button>
          </div>
        ) : (
          <div className="text-center text-sm text-gray-500 dark:text-gray-400">
            {t('system.savingSettings')}
          </div>
        )}
      </div>
    </div>
  )
}

function ModelRoleForm({
  role,
  label,
  config,
  providers,
  providerRoles,
  modelSuggestions,
  contextSuggestion,
  loadingModels,
  loadingContext,
  onChange,
  onFetchModels,
  onFetchContext,
  onApplyContext,
}) {
  const { t } = useTranslation()
  const roleConfig = config.models?.[role] || {}
  const roleDetails = MODEL_ROLE_DETAILS[role] || {}
  const Icon = roleDetails.icon || Sparkles
  const datalistId = `model-suggestions-${role}`
  const hasContextBudget = CONTEXT_BUDGET_ROLES.has(role)
  const maxContext = roleConfig.max_context_length
  const compressMax = Number.isInteger(Number(maxContext)) ? Number(maxContext) - 20000 : undefined
  const maxContextInvalid = hasContextBudget && maxContext && Number(maxContext) < 50000
  const compressInvalid = hasContextBudget
    && roleConfig.compress_threshold
    && (!compressMax || compressMax < 30000 || roleConfig.compress_threshold < 30000 || roleConfig.compress_threshold > compressMax)
  const compressPlaceholder = Number.isInteger(Number(maxContext)) && Number(maxContext) >= 50000
    ? t('system.compressThresholdPlaceholderWithMax', { max: Number(maxContext) - 20000 })
    : t('system.compressThresholdPlaceholder')

  const updateRole = (key, value) => onChange(['models', role, key], value)
  const isLocal = !roleConfig.provider
  const canLocal = LOCAL_CAPABLE_ROLES.has(role)
  const reuseOptions = ROLE_REUSE_OPTIONS[role] || []
  const reuseTarget = roleConfig.reuse || ''
  const isReused = !!reuseTarget
  const effectiveConfig = resolveRoleConfig(config, role)
  const roleProviders = providerRoles?.[role] || providers

  const handleProviderChange = (nextProvider) => {
    if (nextProvider === '') {
      // Switching to Local: clear cloud-only fields
      onChange(['models', role, 'provider'], '')
      onChange(['models', role, 'api_key'], '')
      onChange(['models', role, 'base_url'], '')
      return
    }
    if (!roleConfig.provider) {
      // Switching from Local to Cloud: clear local-only fields
      onChange(['models', role, 'source'], '')
      onChange(['models', role, 'hf_endpoint'], '')
    }
    updateRole('provider', nextProvider)
  }

  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm">
      <div className="flex flex-col gap-3 mb-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex items-start gap-3 min-w-0">
          <div className="w-9 h-9 rounded-lg bg-sigma-50 dark:bg-sigma-600/20 text-sigma-700 dark:text-sigma-400 flex items-center justify-center flex-shrink-0">
            <Icon className="w-4 h-4" />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h4 className="text-sm font-black text-gray-900 dark:text-gray-100">{t(label)}</h4>
              <span className="text-[10px] font-bold uppercase tracking-wider text-gray-400 dark:text-gray-500">{t(roleDetails.badge)}</span>
            </div>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1 leading-5">{t(roleDetails.description)}</p>
          </div>
        </div>

        {reuseOptions.length > 0 && (
          <div className="inline-flex bg-gray-100 dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded-lg p-1 self-start">
            {reuseOptions.map(([value, optionLabelKey]) => (
              <button
                key={value || 'independent'}
                type="button"
                onClick={() => updateRole('reuse', value)}
                className={`px-3 py-1.5 rounded-md text-xs font-bold transition-colors ${
                  reuseTarget === value
                    ? 'bg-white dark:bg-gray-700 text-gray-900 dark:text-gray-100 shadow-sm'
                    : 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200'
                }`}
              >
                {t(optionLabelKey)}
              </button>
            ))}
          </div>
        )}
      </div>

      {isReused && (
        <div className="flex flex-col gap-2 rounded-lg border border-sigma-100 dark:border-sigma-600/40 bg-sigma-50 dark:bg-sigma-600/15 px-3 py-3 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <Link2 className="w-4 h-4 text-sigma-700 dark:text-sigma-400 flex-shrink-0" />
            <div className="min-w-0">
              <div className="text-sm font-bold text-gray-900 dark:text-gray-100">
                {t('system.using', { role: t(roleLabelKey(reuseTarget)) })}
              </div>
              <div className="text-xs text-gray-500 dark:text-gray-400 truncate">{t(endpointSummary(effectiveConfig))}</div>
            </div>
          </div>
          <button
            type="button"
            onClick={() => updateRole('reuse', '')}
            className="self-start md:self-auto px-3 py-1.5 text-xs font-bold text-sigma-700 dark:text-sigma-400 hover:bg-white dark:hover:bg-gray-800 rounded-md transition-colors"
          >
            {t('system.configureIndependently')}
          </button>
        </div>
      )}

      {!isReused && <div className="grid grid-cols-1 lg:grid-cols-4 gap-3">
        <label className="block">
          <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">{t('system.provider')}</span>
          <div className="relative">
            <select
              value={roleConfig.provider || ''}
              onChange={e => handleProviderChange(e.target.value)}
              className="w-full appearance-none px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm"
            >
              {canLocal && <option value="">{t('system.providerLocal')}</option>}
              {roleProviders.map(provider => <option key={provider} value={provider}>{provider}</option>)}
            </select>
            <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
          </div>
        </label>

        {!isLocal && (
          <>
            <TextField
              label={t('system.baseUrl')}
              value={roleConfig.base_url || ''}
              placeholder={t('system.baseUrlPlaceholder')}
              onChange={value => updateRole('base_url', value)}
            />
            <TextField
              label={t('system.apiKey')}
              type="password"
              value={roleConfig.api_key || ''}
              onChange={value => updateRole('api_key', value)}
            />
          </>
        )}

        <label className="block">
          <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">{t('system.modelLabel')}</span>
          <div className="relative">
            <input
              value={roleConfig.model || ''}
              list={datalistId}
              onFocus={() => onFetchModels(role)}
              onBlur={() => hasContextBudget && onFetchContext(role)}
              onChange={e => updateRole('model', e.target.value)}
              className="w-full px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm"
            />
            {loadingModels && <Loader2 className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 animate-spin" />}
          </div>
          <datalist id={datalistId}>
            {(modelSuggestions[role] || []).map(model => <option key={model} value={model} />)}
          </datalist>
        </label>
      </div>}

      {!isReused && isLocal && canLocal && (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-3">
          <label className="block">
            <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">{t('system.modelSource')}</span>
            <div className="relative">
              <select
                value={roleConfig.source || 'huggingface'}
                onChange={e => updateRole('source', e.target.value)}
                className="w-full appearance-none px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm"
              >
                <option value="huggingface">{t('system.huggingFace')}</option>
                <option value="modelscope">{t('system.modelScope')}</option>
              </select>
              <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
            </div>
          </label>
          {(!roleConfig.source || roleConfig.source === 'huggingface') && (
            <TextField
              label={t('system.hfEndpoint')}
              value={roleConfig.hf_endpoint || ''}
              placeholder={t('system.hfEndpointPlaceholder')}
              onChange={value => updateRole('hf_endpoint', value)}
            />
          )}
        </div>
      )}

      {!isReused && hasContextBudget && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-3">
          <div>
            <NumberField
              label={t('system.maxContextLength')}
              value={roleConfig.max_context_length ?? ''}
              min={50000}
              placeholder={t('system.maxContextPlaceholder')}
              onChange={value => {
                const parsed = toOptionalPositiveInt(value)
                const auto = autoCompressThreshold(parsed)
                updateRole('max_context_length', parsed)
                if (auto) onChange(['models', role, 'compress_threshold'], auto)
              }}
            />
            {maxContextInvalid && (
              <div className="text-xs text-red-500 mt-1">
                {t('system.maxContextError')}
              </div>
            )}
            {loadingContext && <div className="text-xs text-gray-400 dark:text-gray-500 mt-1">{t('system.checkingContext')}</div>}
            {contextSuggestion?.max_context_length && (
              <button
                type="button"
                onMouseDown={e => e.preventDefault()}
                onClick={() => onApplyContext(role)}
                className="mt-1 inline-flex items-center gap-1 text-xs font-bold text-sigma-600 hover:text-sigma-700"
              >
                <Check className="w-3 h-3" />
                {t('system.useContext', { value: contextSuggestion.max_context_length })}
              </button>
            )}
          </div>
          <div>
            <NumberField
              label={t('system.compressThreshold')}
              value={roleConfig.compress_threshold ?? ''}
              min={30000}
              max={compressMax}
              placeholder={compressPlaceholder}
              onChange={value => updateRole('compress_threshold', toOptionalPositiveInt(value))}
            />
            {compressInvalid && (
              <div className="text-xs text-red-500 mt-1">
                {t('system.thresholdError')}
              </div>
            )}
          </div>
        </div>
      )}

      <AdvancedSection
        role={role}
        roleConfig={roleConfig}
        effectiveConfig={effectiveConfig}
        isReused={isReused}
        reuseTarget={reuseTarget}
        onChange={onChange}
      />
    </div>
  )
}

export default function SystemSettingsModal({ isOpen, onClose, blockClose = false }) {
  const { t } = useTranslation()
  const [config, setConfig] = useState(null)
  const [originalConfig, setOriginalConfig] = useState(null)
  const [yamlContent, setYamlContent] = useState('')
  const [originalYaml, setOriginalYaml] = useState('')
  const [path, setPath] = useState('')
  const [providers, setProviders] = useState([])
  const [providerRoles, setProviderRoles] = useState({})
  const [modelSuggestions, setModelSuggestions] = useState({})
  const [contextSuggestions, setContextSuggestions] = useState({})
  const [loadingModels, setLoadingModels] = useState({})
  const [loadingContext, setLoadingContext] = useState({})
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState('form')
  const [showYamlWarning, setShowYamlWarning] = useState(false)
  const [showCloseConfirm, setShowCloseConfirm] = useState(false)
  const [checkPhase, setCheckPhase] = useState(null)   // null | 'confirming' | 'checking' | 'results'
  const [checkResults, setCheckResults] = useState([])
  const checkAbortRef = useRef(null)
  const [restarting, setRestarting] = useState(false)
  const [restartCountdown, setRestartCountdown] = useState(10)
  const restartTimerRef = useRef(null)

  const dirty = useMemo(() => {
    if (mode === 'yaml') return yamlContent !== originalYaml
    return JSON.stringify(config) !== JSON.stringify(originalConfig)
  }, [config, mode, originalConfig, originalYaml, yamlContent])

  const loadSettings = useCallback(async () => {
    if (!isOpen) return
    setLoading(true)
    setError('')
    try {
      const [settingsData, providerData] = await Promise.all([
        systemAPI.getSettings(),
        systemAPI.listProviders().catch(() => ({ providers: [], provider_roles: {} })),
      ])
      const loadedConfig = cloneConfig(settingsData.config)
      setConfig(loadedConfig)
      setOriginalConfig(cloneConfig(loadedConfig))
      setYamlContent(settingsData.content || '')
      setOriginalYaml(settingsData.content || '')
      setPath(settingsData.path || 'settings.yaml')
      setProviders(providerData.providers || [])
      setProviderRoles(providerData.provider_roles || {})
      setMode('form')
    } catch (err) {
      setError(humanizeSettingsError(err.message || t('system.toast.loadFailed'), t))
    } finally {
      setLoading(false)
    }
  }, [isOpen, t])

  useEffect(() => { loadSettings() }, [loadSettings])

  // Close guard — checks dirty before closing (used by backdrop, X button, Escape).
  // When blockClose is true the panel is mandatory (critical models unset) and
  // can only be dismissed by completing model configuration.
  const handleClose = () => {
    if (blockClose) return
    if (saving || checkPhase === 'checking') return
    if (checkPhase) { setCheckPhase(null); return }
    if (dirty) { setShowCloseConfirm(true); return }
    onClose()
  }

  // Escape key → handleClose (must be before early return to satisfy Rules of Hooks)
  useEffect(() => {
    if (!isOpen || blockClose) return
    const handler = (e) => { if (e.key === 'Escape') handleClose() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [isOpen, dirty, saving, checkPhase, blockClose])

  // Restart countdown — fires after a successful save.
  useEffect(() => {
    if (!restarting) return
    if (restartCountdown <= 0) {
      window.location.reload()
      return
    }
    restartTimerRef.current = setTimeout(() => {
      setRestartCountdown(c => c - 1)
    }, 1000)
    return () => clearTimeout(restartTimerRef.current)
  }, [restarting, restartCountdown])

  // Cleanup timer on unmount.
  useEffect(() => {
    return () => clearTimeout(restartTimerRef.current)
  }, [])

  if (!isOpen) return null

  // ── Restart overlay ─────────────────────────────────────────────
  // Rendered in place of the settings form after a successful save.
  if (restarting) {
    return (
      <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
        <div className="absolute inset-0 bg-gray-900/60 backdrop-blur-md" />
        <div className="relative bg-white dark:bg-gray-900 rounded-3xl w-full max-w-md p-10 shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 text-center animate-in zoom-in duration-300">
          <div className="w-16 h-16 rounded-full bg-sigma-100 dark:bg-sigma-900/30 flex items-center justify-center mx-auto mb-6">
            <RotateCcw size={28} className="text-sigma-600 dark:text-sigma-400 animate-spin" />
          </div>
          <h2 className="text-xl font-semibold text-gray-900 dark:text-white mb-3">
            {t('system.restart.title')}
          </h2>
          <p className="text-sm text-gray-500 dark:text-gray-400 mb-6 leading-relaxed">
            {t('system.restart.desc')}
          </p>
          <p className="text-xs text-gray-400 dark:text-gray-500 mb-8">
            {t('system.restart.countdown', { seconds: restartCountdown })}
          </p>
          <button
            onClick={() => window.location.reload()}
            className="w-full py-2.5 bg-gray-900 dark:bg-white text-white dark:text-gray-900 rounded-xl font-medium hover:opacity-90 transition-opacity"
          >
            {t('system.restart.refreshNow')}
          </button>
        </div>
      </div>
    )
  }

  const updateConfig = (pathParts, value) => {
    setConfig(current => updateNested(current, pathParts, value))
  }

  const fetchModels = async (role) => {
    const roleConfig = config?.models?.[role]
    if (!roleConfig) return
    setLoadingModels(current => ({ ...current, [role]: true }))
    try {
      const data = await systemAPI.listModels({
        provider: roleConfig.provider || '',
        baseUrl: roleConfig.base_url || '',
        apiKey: roleConfig.api_key || '',
      })
      setModelSuggestions(current => ({ ...current, [role]: data.models || [] }))
    } catch {
      setModelSuggestions(current => ({ ...current, [role]: [] }))
    } finally {
      setLoadingModels(current => ({ ...current, [role]: false }))
    }
  }

  const fetchContext = async (role) => {
    const roleConfig = config?.models?.[role]
    if (!roleConfig?.model) return
    setLoadingContext(current => ({ ...current, [role]: true }))
    try {
      const data = await systemAPI.getModelContext({
        provider: roleConfig.provider || '',
        model: roleConfig.model || '',
      })
      setContextSuggestions(current => ({ ...current, [role]: data.max_context_length ? data : null }))
    } catch {
      setContextSuggestions(current => ({ ...current, [role]: null }))
    } finally {
      setLoadingContext(current => ({ ...current, [role]: false }))
    }
  }

  const applyContextSuggestion = (role) => {
    const max = contextSuggestions[role]?.max_context_length
    if (!max) return
    setConfig(current => {
      let next = updateNested(current, ['models', role, 'max_context_length'], max)
      next = updateNested(next, ['models', role, 'compress_threshold'], autoCompressThreshold(max))
      return next
    })
  }

  const enterYamlMode = async () => {
    setShowYamlWarning(false)
    setSaving(true)
    setError('')
    try {
      const rendered = await systemAPI.renderSettingsYaml(config)
      setYamlContent(rendered.content || '')
      setMode('yaml')
    } catch (err) {
      const message = humanizeSettingsError(err.message || t('system.toast.yamlFailed'), t)
      setError(message)
      toastError(message)
    } finally {
      setSaving(false)
    }
  }

  const enterFormMode = async () => {
    setSaving(true)
    setError('')
    try {
      const data = await systemAPI.validateSettingsYaml(yamlContent)
      setConfig(cloneConfig(data.config))
      setMode('form')
    } catch (err) {
      const message = humanizeSettingsError(err.message || t('system.toast.validationFailed'), t)
      setError(message)
      toastError(message)
    } finally {
      setSaving(false)
    }
  }

  const handleSaveDirect = async () => {
    setSaving(true)
    setError('')
    try {
      const result = await systemAPI.updateSettings(mode === 'yaml' ? { content: yamlContent } : { config })
      const reloaded = await systemAPI.getSettings()
      setConfig(cloneConfig(reloaded.config))
      setOriginalConfig(cloneConfig(reloaded.config))
      setYamlContent(reloaded.content || '')
      setOriginalYaml(reloaded.content || '')
      setPath(reloaded.path || path)
      setCheckPhase(null)

      // Trigger service restart so model / config changes take effect.
      // Fire-and-forget — if the restart API fails the overlay still counts
      // down and the page will reload regardless.
      systemAPI.restart().catch(() => {})
      setRestarting(true)
      setRestartCountdown(10)
    } catch (err) {
      const message = humanizeSettingsError(err.message || t('system.toast.saveFailed'), t)
      setError(message)
      toastError(message)
    } finally {
      setSaving(false)
    }
  }

  const handleSave = () => setCheckPhase('confirming')

  const handleCheckAndSave = async () => {
    setCheckPhase('checking')
    setCheckResults([])
    const abort = new AbortController()
    checkAbortRef.current = abort

    const payload = mode === 'yaml' ? { content: yamlContent } : { config }

    try {
      const stream = await systemAPI.checkSettings(payload, abort.signal)
      const reader = stream.getReader()
      const decoder = new TextDecoder()

      const parser = createSSEStreamParser({
        onEvent: (type, data) => {
          if (type === 'check_start') {
            // Initialize all items as pending
            const items = [
              { role: 'structure', label: 'system.checkConfigStructure', status: 'pending' },
              { role: 'supervisor', label: 'system.checkSupervisor', status: 'pending' },
              { role: 'ra', label: 'system.checkRA', status: 'pending' },
              { role: 'vision', label: 'system.checkVision', status: 'pending' },
              { role: 'draw', label: 'system.checkDraw', status: 'pending' },
              { role: 'embedding', label: 'system.checkEmbedding', status: 'pending' },
              { role: 'rerank', label: 'system.checkRerank', status: 'pending' },
            ]
            setCheckResults(items)
          }
          if (type === 'check_progress') {
            setCheckResults(prev => prev.map(r =>
              r.role === data.role ? { ...r, status: 'running' } : r
            ))
          }
          if (type === 'check_result') {
            const result = data.message
              ? { ...data, message: humanizeSettingsError(data.message, t) }
              : data
            setCheckResults(prev => prev.map(r =>
              r.role === data.role ? { ...r, ...result } : r
            ))
          }
          if (type === 'check_done') {
            setCheckResults(prev => {
              const hasFailure = prev.some(r => r.status === 'fail')
              if (!hasFailure) {
                // Show results briefly then auto-save
                setTimeout(() => setCheckPhase('results'), 0)
                setTimeout(() => handleSaveDirect(), 1200)
              } else {
                setTimeout(() => setCheckPhase('results'), 0)
              }
              return prev
            })
          }
        },
        onError: () => {
          setCheckPhase('results')
        },
      })

      await parser.start(reader, decoder, abort.signal)
    } catch (err) {
      if (abort.signal.aborted) return
      setCheckResults(prev => {
        if (prev.length === 0) {
          return [{ role: 'structure', label: 'system.checkConfigStructure', status: 'fail', message: humanizeSettingsError(err.message || t('system.toast.checkStartFailed'), t) }]
        }
        return prev
      })
      setCheckPhase('results')
    } finally {
      checkAbortRef.current = null
    }
  }

  return (
    <div className="fixed inset-0 z-[5000] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-gray-900/40 backdrop-blur-sm animate-in fade-in duration-300" onClick={blockClose ? undefined : handleClose} />
      <div className="relative z-[5001] w-full max-w-6xl h-[86vh] bg-white dark:bg-gray-900 rounded-2xl shadow-[0_20px_70px_rgba(0,0,0,0.3)] border border-gray-100 dark:border-gray-800 overflow-hidden flex flex-col animate-in zoom-in duration-300">
        {showYamlWarning && <WarningDialog onCancel={() => setShowYamlWarning(false)} onConfirm={enterYamlMode} />}
        {checkPhase === 'confirming' && (
          <SaveConfirmDialog
            onCheckAndSave={handleCheckAndSave}
            onSaveDirect={handleSaveDirect}
            onCancel={() => setCheckPhase(null)}
          />
        )}
        {checkPhase === 'checking' && <CheckProgressDialog results={checkResults} />}
        {checkPhase === 'results' && (
          <CheckResultsDialog
            results={checkResults}
            onForceSave={handleSaveDirect}
            onBackToEdit={() => setCheckPhase(null)}
          />
        )}

        <header className="flex items-center justify-between px-6 py-4 border-b border-gray-100 dark:border-gray-800">
          <div className="min-w-0">
            <h2 className="text-lg font-black text-gray-900 dark:text-gray-100">{t('system.title')}</h2>
            <p className="text-xs text-gray-400 dark:text-gray-500 font-mono truncate">{path}</p>
          </div>
          <div className="flex items-center gap-2">
            {mode === 'yaml' ? (
              <button
                onClick={enterFormMode}
                disabled={loading || saving}
                className="px-3 py-2 text-sm font-bold text-gray-600 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors disabled:opacity-50"
              >
                {t('system.formButton')}
              </button>
            ) : (
              <button
                onClick={() => setShowYamlWarning(true)}
                disabled={loading || saving || !config}
                className="p-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors disabled:opacity-50"
                title={t('system.editYamlTitle')}
              >
                <Code2 className="w-4 h-4" />
              </button>
            )}
            <button
              onClick={loadSettings}
              disabled={loading || saving}
              className="p-2 text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors disabled:opacity-50"
              title={t('system.reload')}
            >
              <RotateCcw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button
              onClick={handleSave}
              disabled={!dirty || loading || saving || !config || !!checkPhase || Object.values(loadingModels).some(Boolean)}
              className="px-4 py-2 bg-sigma-600 hover:bg-sigma-700 disabled:bg-gray-200 dark:disabled:bg-gray-800 disabled:text-gray-400 text-white rounded-lg flex items-center gap-2 transition-colors font-bold text-sm"
            >
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              {t('common.save')}
            </button>
            {!blockClose && (
            <button onClick={handleClose} disabled={saving} className="p-2 text-gray-400 dark:text-gray-500 hover:text-gray-900 dark:hover:text-gray-100 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg transition-colors disabled:opacity-50" title={t('common.close')}>
              <X className="w-5 h-5" />
            </button>
            )}
          </div>
        </header>

        {error && (
          <div className="mx-6 mt-4 px-4 py-3 bg-red-50 dark:bg-red-900/20 border border-red-100 dark:border-red-800/50 text-red-700 dark:text-red-400 rounded-lg text-sm whitespace-pre-wrap">
            {error}
          </div>
        )}

        <div className="flex-1 min-h-0 overflow-y-auto p-6">
          {loading || !config ? (
            <div className="h-full flex items-center justify-center text-gray-400 dark:text-gray-500">
              <Loader2 className="w-5 h-5 animate-spin mr-2" />
              {t('system.loadingSettings')}
            </div>
          ) : mode === 'yaml' ? (
            <textarea
              value={yamlContent}
              onChange={e => setYamlContent(e.target.value)}
              spellCheck={false}
              className="w-full h-full min-h-[560px] resize-none rounded-xl border border-gray-200 bg-gray-950 text-gray-100 p-4 font-mono text-xs leading-5 outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600"
            />
          ) : (
            <div>
              <Section title={t('system.models')}>
                <div className="grid grid-cols-1 gap-4">
                  {MODEL_ROLES.map(([role, label]) => (
                    <ModelRoleForm
                      key={role}
                      role={role}
                      label={label}
                      config={config}
                      providers={providers}
                      providerRoles={providerRoles}
                      modelSuggestions={modelSuggestions}
                      contextSuggestion={contextSuggestions[role]}
                      loadingModels={!!loadingModels[role]}
                      loadingContext={!!loadingContext[role]}
                      onChange={updateConfig}
                      onFetchModels={fetchModels}
                      onFetchContext={fetchContext}
                      onApplyContext={applyContextSuggestion}
                    />
                  ))}
                </div>
              </Section>

              <Section title={t('system.browserSection')}>
                <SettingsGroup title={t('system.browserSection')} icon={Globe2}>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <NumberField label={t('system.domMaxChars')} value={getNested(config, ['browser', 'dom_max_chars'])} onChange={value => updateConfig(['browser', 'dom_max_chars'], value)} />
                    <NumberField label={t('system.foldThreshold')} value={getNested(config, ['browser', 'fold_threshold'])} onChange={value => updateConfig(['browser', 'fold_threshold'], value)} />
                    <NumberField label={t('system.consoleBuffer')} value={getNested(config, ['browser', 'console_buffer_size'])} onChange={value => updateConfig(['browser', 'console_buffer_size'], value)} />
                    <NumberField label={t('system.toolTimeout')} value={getNested(config, ['browser', 'tool_timeout'])} onChange={value => updateConfig(['browser', 'tool_timeout'], value)} />
                    <NumberField label={t('system.tabIdleTimeout')} value={getNested(config, ['browser', 'tab_idle_timeout'])} onChange={value => updateConfig(['browser', 'tab_idle_timeout'], value)} />
                    <label className="block">
                      <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">{t('system.searchEngine')}</span>
                      <div className="relative mb-2">
                        <select
                          value={SEARCH_ENGINES.some(([url]) => url === config.browser?.search_engine_url) ? config.browser.search_engine_url : ''}
                          onChange={e => updateConfig(['browser', 'search_engine_url'], e.target.value)}
                          className="w-full appearance-none px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm"
                        >
                          <option value="">{t('system.custom')}</option>
                          {SEARCH_ENGINES.map(([url, labelKey]) => <option key={url} value={url}>{t(labelKey)}</option>)}
                        </select>
                        <ChevronDown className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400 pointer-events-none" />
                      </div>
                      <input
                        value={config.browser?.search_engine_url || ''}
                        onChange={e => updateConfig(['browser', 'search_engine_url'], e.target.value)}
                        className="w-full px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg outline-none focus:ring-4 focus:ring-sigma-600/10 focus:border-sigma-600 text-sm"
                      />
                    </label>
                  </div>
                </SettingsGroup>
              </Section>

              <Section title={t('system.librarySection')}>
                <SettingsGroup title={t('system.librarySection')} icon={BookOpen}>
                  <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                    <NumberField label={t('system.topK')} value={config.library?.top_k} onChange={value => updateConfig(['library', 'top_k'], value)} />
                    <NumberField label={t('system.candidatePool')} value={config.library?.candidate_pool_size} onChange={value => updateConfig(['library', 'candidate_pool_size'], value)} />
                    <NumberField label={t('system.chunkMaxUnits')} value={config.library?.chunk_max_units} onChange={value => updateConfig(['library', 'chunk_max_units'], value)} />
                    <NumberField label={t('system.chunkMinUnits')} value={config.library?.chunk_min_units} onChange={value => updateConfig(['library', 'chunk_min_units'], value)} />
                    <NumberField label={t('system.chunkOverlap')} value={config.library?.chunk_overlap_units} onChange={value => updateConfig(['library', 'chunk_overlap_units'], value)} />
                    <NumberField label={t('system.maxMatches')} value={config.library?.max_matches_per_doc} onChange={value => updateConfig(['library', 'max_matches_per_doc'], value)} />
                    <label className="flex h-[66px] items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2">
                      <input
                        type="checkbox"
                        checked={config.library?.reranker_enabled !== false}
                        onChange={e => updateConfig(['library', 'reranker_enabled'], e.target.checked)}
                        className="w-4 h-4 rounded border-gray-300 text-sigma-600 focus:ring-sigma-600"
                      />
                      <span className="text-sm font-bold text-gray-700 dark:text-gray-300">{t('system.rerankerEnabled')}</span>
                    </label>
                    <label className="flex h-[66px] items-center gap-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2">
                      <input
                        type="checkbox"
                        checked={config.library?.auto_ai_metadata_enabled !== false}
                        onChange={e => updateConfig(['library', 'auto_ai_metadata_enabled'], e.target.checked)}
                        className="w-4 h-4 rounded border-gray-300 text-sigma-600 focus:ring-sigma-600"
                      />
                      <span className="text-sm font-bold text-gray-700 dark:text-gray-300">{t('system.autoAiMeta')}</span>
                    </label>
                    <label className="block md:col-span-3">
                      <span className="block text-xs font-bold text-gray-500 dark:text-gray-400 mb-1.5">{t('system.queryInstruction')}</span>
                      <textarea
                        value={config.library?.query_instruction || ''}
                        onChange={e => updateConfig(['library', 'query_instruction'], e.target.value)}
                        rows={3}
                        className="w-full resize-none rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-3 py-2 text-sm outline-none focus:border-sigma-600 focus:ring-4 focus:ring-sigma-600/10"
                      />
                    </label>
                  </div>
                </SettingsGroup>
              </Section>
            </div>
          )}
        </div>

        <footer className="px-6 py-3 border-t border-gray-100 dark:border-gray-800 text-xs text-gray-500 dark:text-gray-400 flex items-center justify-between">
          <span>{t('system.footerNote')}</span>
          <span className={dirty ? 'text-amber-600 dark:text-amber-400 font-bold' : 'text-gray-400 dark:text-gray-500'}>{dirty ? t('system.unsavedChanges') : t('system.toast.saved')}</span>
        </footer>

        {showCloseConfirm && (
          <div className="absolute inset-0 z-20 flex items-center justify-center bg-gray-900/30 p-4">
            <div className="w-full max-w-md bg-white dark:bg-gray-800 rounded-2xl shadow-2xl border border-gray-100 dark:border-gray-700 p-6">
              <div className="flex items-start gap-4">
                <div className="w-10 h-10 rounded-xl bg-red-100 dark:bg-red-900/30 text-red-600 dark:text-red-400 flex items-center justify-center flex-shrink-0">
                  <AlertTriangle className="w-5 h-5" />
                </div>
                <div>
                  <h3 className="text-base font-black text-gray-900 dark:text-gray-100">{t('system.unsavedTitle')}</h3>
                  <p className="text-sm text-gray-600 dark:text-gray-400 mt-1 leading-6">
                    {t('system.unsavedDesc')}
                  </p>
                </div>
              </div>
              <div className="flex justify-end gap-2 mt-6">
                <button onClick={() => setShowCloseConfirm(false)} className="px-4 py-2 text-sm font-bold text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-800 rounded-lg">
                  {t('common.cancel')}
                </button>
                <button onClick={() => { setShowCloseConfirm(false); onClose() }} className="px-4 py-2 text-sm font-bold text-white bg-red-600 hover:bg-red-700 rounded-lg">
                  {t('common.discard')}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
