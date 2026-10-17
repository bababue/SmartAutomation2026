import { STATUS_LABEL } from '../review'
import type { LlmUsage, UploadStatus } from '../types'
import { cancellable, pending, type Batch, type Status } from '../upload'

const STATUS_TEXT: Record<Status, string> = {
  uploading: 'Uploading…', queued: 'Queued', processing: 'Processing…', done: 'Done', error: 'Failed', cancelled: 'Cancelled',
}
const STAGE_TEXT: Record<NonNullable<UploadStatus['progress']>['stage'], string> = {
  classify: 'Reading pages', extract: 'Extracting logs', 'lab results': 'Reading lab sheets', coordinates: 'Checking coordinates…', store: 'Combining records…',
}

function outcome(b: Batch): string {
  const calls = b.calls ? ` · ${b.calls} model call${b.calls === 1 ? '' : 's'}` : ''
  if (b.status === 'queued') return b.ahead ? `Queued (${b.ahead} ahead)` : 'Next up'
  if (b.status === 'error') return b.error ?? STATUS_TEXT.error
  if (b.status === 'processing' && b.progress) {
    const { stage, done, total } = b.progress
    return (total ? `${STAGE_TEXT[stage]} ${done}/${total}` : STAGE_TEXT[stage]) + calls
  }
  if (b.status !== 'done') return STATUS_TEXT[b.status] + calls
  if (!b.boreholes?.length) return 'No borehole recognised' + calls
  return `Added ${b.boreholes.map((h) => `${h.name} (${STATUS_LABEL[h.review_status].toLowerCase()})`).join(', ')}${calls}`
}

const when = (at: string) => at.slice(0, 16).replace('T', ' ') + ' UTC'

interface Props {
  batches: Batch[]
  usage: LlmUsage | null
  onCancel: (key: string, files?: string[]) => void
  onDismiss: (key: string) => void
}

export function UploadQueue({ batches, usage, onCancel, onDismiss }: Props) {
  if (!batches.length) return null
  return (
    <div className="upload-queue" role="status">
      {usage && (
        <div className="upload-usage" title={`Calls go to ${usage.endpoint}. One upload may make at most ${usage.upload_limit}.`}>
          Model calls today: {usage.today} of {usage.daily_limit}
        </div>
      )}
      {batches.map((b) => {
        const single = b.files.length === 1 && !b.skipped.length && !b.duplicates.length
        const live = cancellable(b)
        return (
          <div key={b.key} className={`upload-batch upload-${b.status}`}>
            {!single && b.files.map((f, i) => {
              const out = b.cancelled.includes(f)
              return (
                <div key={i} className="upload-row">
                  <span className="upload-name" title={f}>{f}</span>
                  <span className="upload-state">{out ? 'Cancelled' : STATUS_TEXT[b.status]}</span>
                  {live && !out && (
                    <button className="upload-dismiss" onClick={() => onCancel(b.key, [f])} aria-label={`Leave out ${f}`} title="Leave this file out">×</button>
                  )}
                </div>
              )
            })}
            {b.duplicates.map((d, i) => (
              <div key={`dup-${i}`} className="upload-row upload-skipped">
                <span className="upload-name" title={d.file}>{d.file}</span>
                <span className="upload-state" title={`Same file as ${d.of}${d.at ? `, uploaded ${when(d.at)}` : ' in this batch'}`}>
                  {d.at ? 'Already uploaded' : 'Sent twice'}
                </span>
              </div>
            ))}
            {b.skipped.map((f, i) => (
              <div key={`skipped-${i}`} className="upload-row upload-skipped">
                <span className="upload-name" title={f}>{f}</span>
                <span className="upload-state" title="Only PDF, image, DOCX, TXT and XLSX files are read">Skipped: not a document</span>
              </div>
            ))}
            <div className="upload-row upload-summary">
              <span className="upload-name" title={b.files.join(', ')}>{single ? b.files[0] : `Batch of ${b.files.length} ${b.jobId ? 'document' : 'file'}${b.files.length === 1 ? '' : 's'}`}</span>
              <span className="upload-state" title={outcome(b)}>{outcome(b)}</span>
              {live && (
                <button className="upload-cancel" onClick={() => onCancel(b.key)}>Cancel</button>
              )}
              {!pending(b.status) && (
                <button className="upload-dismiss" onClick={() => onDismiss(b.key)} aria-label={`Dismiss ${single ? b.files[0] : 'batch'}`}>×</button>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}
