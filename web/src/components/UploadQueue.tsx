import { useCallback, useEffect, useRef, useState } from 'react'
import { cancelUpload, fetchLlmUsage, fetchUploadStatus, postUpload } from '../api'
import { STATUS_LABEL as REVIEW_LABEL } from '../review'
import type { DuplicateFile, LlmUsage, UploadStatus } from '../types'

type Status = 'uploading' | UploadStatus['status']

/** Files sent together: the server assembles them as one document group, so one hole spread over
 * several files becomes one record. */
interface Batch {
  key: string
  files: string[]
  /** Not a document (e.g. GIS files): left out of the batch. */
  skipped: string[]
  duplicates: DuplicateFile[]
  cancelled: string[]
  jobId: string | null
  status: Status
  ahead?: number | null
  progress?: UploadStatus['progress']
  calls?: number
  error?: string
  boreholes?: UploadStatus['boreholes']
}

const STATUS_TEXT: Record<Status, string> = {
  uploading: 'Uploading…', queued: 'Queued', processing: 'Processing…', done: 'Done', error: 'Failed', cancelled: 'Cancelled',
}
const STAGE_TEXT: Record<NonNullable<UploadStatus['progress']>['stage'], string> = {
  classify: 'Reading pages', extract: 'Extracting logs', 'lab results': 'Reading lab sheets', coordinates: 'Checking coordinates…', store: 'Combining records…',
}
const pending = (s: Status) => s === 'uploading' || s === 'queued' || s === 'processing'
const onServer = (b: Batch) => !!b.jobId && (b.status === 'queued' || b.status === 'processing')
/** Once records are being stored, a cancellation would come too late. */
const cancellable = (b: Batch) => onServer(b) && b.progress?.stage !== 'store'

export function useUploadQueue(onUploaded: () => void, onSelect: (id: string) => void) {
  const [batches, setBatches] = useState<Batch[]>([])
  const [usage, setUsage] = useState<LlmUsage | null>(null)
  const batchesRef = useRef(batches)
  useEffect(() => { batchesRef.current = batches }, [batches])

  const update = (key: string, patch: Partial<Batch>) =>
    setBatches((bs) => bs.map((b) => (b.key === key ? { ...b, ...patch } : b)))
  const apply = useCallback((key: string, s: UploadStatus) => update(key, {
    status: s.status, ahead: s.ahead, progress: s.progress, calls: s.calls, cancelled: s.cancelled,
    error: s.error ?? undefined, boreholes: s.boreholes,
  }), [])

  const addFiles = useCallback((list: FileList | File[]) => {
    const files = Array.from(list)
    if (!files.length) return
    const key = `${Date.now()}-${Math.random().toString(36).slice(2)}`
    setBatches((bs) => [...bs, { key, files: files.map((f) => f.name), skipped: [], duplicates: [], cancelled: [], jobId: null, status: 'uploading' }])
    postUpload(files)
      .then((res) => update(key, { jobId: res.job_id, files: res.files, skipped: res.skipped, duplicates: res.duplicates, status: 'queued' }))
      .catch((err) => update(key, { status: 'error', error: err.message }))
  }, [])

  // One poller for every batch still on the server's queue, and for the day's model calls while any runs.
  useEffect(() => {
    const t = setInterval(() => {
      const running = batchesRef.current.filter(onServer)
      if (running.length) fetchLlmUsage().then(setUsage).catch(() => {})
      running.forEach((b) => {
        fetchUploadStatus(b.jobId!)
          .then((s) => {
            apply(b.key, s)
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
  }, [onUploaded, onSelect, apply])

  /** files: some documents of the batch; none: the whole batch. */
  const cancel = useCallback((key: string, files?: string[]) => {
    const b = batchesRef.current.find((x) => x.key === key)
    if (!b?.jobId) return
    cancelUpload(b.jobId, files).then((s) => apply(key, s)).catch((err) => update(key, { error: err.message }))
  }, [apply])

  const dismiss = useCallback((key: string) => setBatches((bs) => bs.filter((b) => b.key !== key)), [])
  return { batches, usage, addFiles, cancel, dismiss, active: batches.some((b) => pending(b.status)) }
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
  return `Added ${b.boreholes.map((h) => `${h.name} (${REVIEW_LABEL[h.review_status].toLowerCase()})`).join(', ')}${calls}`
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
