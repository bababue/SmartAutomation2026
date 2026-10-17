import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchUploadStatus, postUpload } from '../api'
import { STATUS_LABEL as REVIEW_LABEL } from '../review'
import type { UploadStatus } from '../types'

type Status = 'uploading' | UploadStatus['status']

/** Files sent together: the server assembles them as one document group, so one hole spread over
 * several files becomes one record. */
interface Batch {
  key: string
  files: string[]
  /** Not a document (data exports, spreadsheets, GIS files): left out of the batch. */
  skipped: string[]
  jobId: string | null
  status: Status
  ahead?: number | null
  error?: string
  boreholes?: UploadStatus['boreholes']
}

const STATUS_TEXT: Record<Status, string> = {
  uploading: 'Uploading…', queued: 'Queued', processing: 'Processing…', done: 'Done', error: 'Failed',
}
const pending = (s: Status) => s === 'uploading' || s === 'queued' || s === 'processing'

export function useUploadQueue(onUploaded: () => void, onSelect: (id: string) => void) {
  const [batches, setBatches] = useState<Batch[]>([])
  const batchesRef = useRef(batches)
  useEffect(() => { batchesRef.current = batches }, [batches])

  const update = (key: string, patch: Partial<Batch>) =>
    setBatches((bs) => bs.map((b) => (b.key === key ? { ...b, ...patch } : b)))

  const addFiles = useCallback((list: FileList | File[]) => {
    const files = Array.from(list)
    if (!files.length) return
    const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`
    setBatches((bs) => [...bs, { key, files: files.map((f) => f.name), skipped: [], jobId: null, status: 'uploading' }])
    postUpload(files)
      .then((res) => update(key, { jobId: res.job_id, files: res.files, skipped: res.skipped, status: 'queued' }))
      .catch((err) => update(key, { status: 'error', error: err.message }))
  }, [])

  // One poller for every batch still on the server's queue.
  useEffect(() => {
    const t = setInterval(() => {
      batchesRef.current.filter((b) => b.jobId && (b.status === 'queued' || b.status === 'processing')).forEach((b) => {
        fetchUploadStatus(b.jobId!)
          .then((s) => {
            update(b.key, { status: s.status, ahead: s.ahead, error: s.error ?? undefined, boreholes: s.boreholes })
            if (s.status === 'done') {
              onUploaded()
              // Jump to the new record only when it is the sole result on the list: a batch should not steal focus.
              if (s.boreholes.length === 1 && batchesRef.current.length === 1) onSelect(s.boreholes[0].id)
            }
          })
          .catch(() => update(b.key, { status: 'error', error: 'Lost track of the upload' }))
      })
    }, 1500)
    return () => clearInterval(t)
  }, [onUploaded, onSelect])

  const dismiss = useCallback((key: string) => setBatches((bs) => bs.filter((b) => b.key !== key)), [])
  return { batches, addFiles, dismiss, active: batches.some((b) => pending(b.status)) }
}

function outcome(b: Batch): string {
  if (b.status === 'queued') return b.ahead ? `Queued (${b.ahead} ahead)` : 'Next up'
  if (b.status === 'error') return b.error ?? STATUS_TEXT.error
  if (b.status !== 'done') return STATUS_TEXT[b.status]
  if (!b.boreholes?.length) return 'No borehole recognised'
  return `Added ${b.boreholes.map((h) => `${h.name} (${REVIEW_LABEL[h.review_status].toLowerCase()})`).join(', ')}`
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
                <span className="upload-state" title="Only PDF, image and DOCX files are read">Skipped: not a document</span>
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
