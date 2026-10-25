"""Engineering wording for the versioned production result; no quality decisions."""

RELIABILITY = ('SPARSE_SUPPORT', 'HIGH_DRIVER_DISAGREEMENT',
               'RECENT_ESTIMATE_JUMP', 'DOMINATED_BY_SINGLE_DRIVER')
INFORMATION = ('COHORT_REFERENCE_INCOMPLETE', 'COHORT_IMBALANCE',
               'FIELD_REFERENCE_UNAVAILABLE', 'AUX_TRAFFIC_UNAVAILABLE', 'AUX_TRAFFIC_PENDING')
MESSAGES = {
    'SPARSE_SUPPORT': ('Limited data support', 'Only a small number of drivers define the live trend, so the estimate may move substantially as more laps arrive.'),
    'HIGH_DRIVER_DISAGREEMENT': ('Driver trends disagree', 'Supported drivers imply materially different track-evolution trends, making the combined estimate sensitive to new evidence.'),
    'RECENT_ESTIMATE_JUMP': ('Recent estimate change', 'New or revised timing evidence has recently moved the live estimate substantially; it may still be settling.'),
    'DOMINATED_BY_SINGLE_DRIVER': ('Concentrated data support', "A large share of the current estimate comes from one driver, so changes to that driver's evidence can materially move it."),
    'COHORT_IMBALANCE': ('Limited field representation', 'Current supported evidence is concentrated in one part of the qualifying performance order.'),
    'COHORT_REFERENCE_INCOMPLETE': ('Reference data incomplete', 'Some Q1 ranking reference information is missing. This limits the description of field representation; it does not invalidate track-evolution observations.'),
    'AUX_TRAFFIC_UNAVAILABLE': ('Traffic context unavailable', 'Reliable physical traffic information is unavailable for some observations. It is not required for the live estimate.'),
    'AUX_TRAFFIC_PENDING': ('Traffic context pending', 'Physical traffic checks are still pending for some observations. The live estimate does not depend on them.'),
    'FIELD_REFERENCE_UNAVAILABLE': ('Lap sanity reference unavailable', 'Some new laps could not be checked against a valid field reference and were omitted; existing supported observations remain usable.'),
}
SOURCE_LABELS = {'LIVE_PER_DRIVER': 'Live per-driver estimate',
                 'CONFIGURED_FP_DEFAULT_PRIOR': 'Configured fallback',
                 'MANUAL_OVERRIDE': 'Manual override'}
STATUS_LABELS = {'FALLBACK': 'Fallback', 'LIVE_LIMITED': 'Live — limited',
                 'LIVE_SUPPORTED': 'Live — supported'}


def fallback_label(result):
    kind = result.get('fallback_provenance', {}).get('kind')
    return {'CONFIGURED_DEFAULT': 'Configured default', 'LOADED_FP_PRIOR': 'Loaded FP prior'}.get(kind, 'Configured fallback')


def readiness_messages(result, *, inputs_only=False):
    """Explain missing inputs and admission/support separately from fit quality."""
    if not result:
        return []
    messages = []
    readiness = result.get('readiness', {})
    if readiness and not readiness.get('pace_reference_available'):
        messages.append('FP pace reference unavailable: initial FAST classification needs a usable FP 5%-distance or S1 reference.')
    elif readiness.get('drivers_without_pace_reference'):
        messages.append('FP pace reference unavailable for: ' + ', '.join(readiness['drivers_without_pace_reference']) + '.')
    if readiness and not readiness.get('cool_duration_available'):
        messages.append('COOL/PREP duration unavailable: effective finish after a push needs an FP-derived duration or a manual value.')
    if inputs_only:
        return messages
    rejected = result.get('attrition', {}).get('non_operational_fast_completions', 0)
    if rejected:
        messages.append(f'{rejected} completed laps ingested but not operational FAST at completion; these laps were not admitted to Track Evolution.')
    waiting = [row['driver'] for row in result.get('observation_support', ())
               if row.get('reason') == 'INSUFFICIENT_COMPARABLE_OBSERVATIONS']
    if waiting:
        messages.append('Admitted observations awaiting a second comparable tyre installation for a driver slope: ' + ', '.join(waiting) + '.')
    if (result.get('status') == 'FALLBACK'
            and result.get('support', {}).get('observations') == 0):
        messages.append('No admitted observations are available for a live fit; the configured fallback or manual override remains in use.')
    return messages


def display_messages(result):
    """At most two mapped entries; information never gains warning severity."""
    if result['status'] == 'FALLBACK':
        explanation = ('No supported live fit is available; using the manual override.'
                       if result['estimate_source'] == 'MANUAL_OVERRIDE' else
                       'There is not yet enough live evidence for a supported track-evolution fit; '
                       f"using {fallback_label(result).lower()}: {result['configured_fallback_s_per_global_push']:+.6f} s/push.")
        return [{'category': 'Insufficient live support', 'explanation': explanation, 'severity': 'fallback'}]
    codes = set(result['status_reasons']) | set(result['information'])
    entries = []
    for code in RELIABILITY + INFORMATION:
        if code not in codes or (code == 'AUX_TRAFFIC_PENDING' and 'AUX_TRAFFIC_UNAVAILABLE' in codes):
            continue
        category, explanation = MESSAGES[code]
        if code == 'AUX_TRAFFIC_UNAVAILABLE' and 'AUX_TRAFFIC_PENDING' in codes:
            explanation = 'Traffic context is unavailable or still pending for some observations; it is not required for the live estimate.'
        entries.append({'category': category, 'explanation': explanation,
                        'severity': 'reliability' if code in RELIABILITY else 'information'})
        if len(entries) == 2:
            break
    return entries
