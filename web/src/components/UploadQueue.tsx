import { STATUS_LABEL } from '../review'
import type { UploadStatus } from '../types'
import { pending, type Batch, type Status } from '../upload'

const STATUS_TEXT: Record<Status, string> = {
  uploading: 'Uploading…', queued: 'Queued', processing: 'Processing…', done: 'Done', error: 'Failed',
}
const STAGE_TEXT: Record<NonNullable<UploadStatus['progress']>['stage'], string> = {
  classify: 'Reading pages', extract: 'Extracting logs', 'lab results': 'Reading lab sheets', coordinates: 'Checking coordinates…', store: 'Combining records…',
}

function outcome(b: Batch): string {
  if (b.status === 'queued') return b.ahead ? `Queued (${b.ahead} ahead)` : 'Next up'
  if (b.status === 'error') return b.error ?? STATUS_TEXT.error
  if (b.status === 'processing' && b.progress) {
    const { stage, done, total } = b.progress
    return total ? `${STAGE_TEXT[stage]} ${done}/${total}` : STAGE_TEXT[stage]
  }
  if (b.status !== 'done') return STATUS_TEXT[b.status]
  if (!b.boreholes?.length) return 'No borehole recognised'
  return `Added ${b.boreholes.map((h) => `${h.name} (${STATUS_LABEL[h.review_status].toLowerCase()})`).join(', ')}`
}

export function UploadQueue({ batches, onDismiss }: { batches: Batch[]; onDismiss: (key: string) => void }) {
  if (!batches.length) return null
  return (
    <div className="upload-queue" role="status">
      {batches.map((b) => {
        const single = b.files.length === 1 && !b.skipped.length
        return (
          <div key={b.key} className={`upload-batch upload-${b.status}`}>
            {!single && b.files.map((f, i) => (
              <div key={i} className="upload-row">
                <span className="upload-name" title={f}>{f}</span>
                <span className="upload-state">{STATUS_TEXT[b.status]}</span>
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
