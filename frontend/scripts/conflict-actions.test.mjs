import assert from 'node:assert/strict'

import { reloadConflict } from '../src/views/conflictActions.js'

const events = []
await reloadConflict({
  reload: async () => true,
  dismiss: () => events.push('reloaded'),
  report: () => events.push('error'),
})
assert.deepEqual(events, ['reloaded'])

await reloadConflict({
  reload: async () => false,
  dismiss: () => events.push('wrong-dismiss'),
  report: error => events.push(`error:${error.message}`),
})
assert.deepEqual(events, ['reloaded', 'error:reload failed'])
