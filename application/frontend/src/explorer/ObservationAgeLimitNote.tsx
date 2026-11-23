/** The server reports the same bound used to select rows, including for summaries. */
export function ObservationAgeLimitNote({limit}:{limit:number|null|undefined}) {
  if (limit === undefined || limit === null) return null;
  return <p role="status">{limit === 0
    ? 'No video is refreshed often enough for this change window under the current collection schedule.'
    : `Only videos up to ${Math.round(limit / 86400)} days old are included: only they are refreshed often enough for this change window.`}</p>;
}
