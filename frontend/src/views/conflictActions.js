export async function reloadConflict({ reload, dismiss, report }) {
  try {
    if ((await reload()) === false) {
      throw new Error('reload failed')
    }
    dismiss()
    return true
  } catch (error) {
    report(error)
    return false
  }
}
