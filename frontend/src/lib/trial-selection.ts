import type { TrialState } from './types'

export function trialSelectionId(trial: TrialState): string {
  return trial.selection_id || trial.trial_name
}

export function toggleTrialSelection(selected: TrialState[], rows: TrialState[], checked: boolean): TrialState[] {
  const result = new Map(selected.map(row => [trialSelectionId(row), row]))
  for (const row of rows) {
    if (checked) result.set(trialSelectionId(row), row)
    else result.delete(trialSelectionId(row))
  }
  return [...result.values()]
}
