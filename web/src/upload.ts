import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import { fetchUploadStatus, postUpload } from './api'
import type { UploadStatus } from './types'

export type Status = 'uploading' | UploadStatus['status']

/** Files sent together: the server assembles them as one document group, so one hole spread over
 * several files becomes one record. */
export interface Batch {
  key: string
  files: string[]
  /** Not a document (e.g. GIS files): left out of the batch. */
  skipped: string[]
  jobId: string | null
  status: Status
  ahead?: number | null
  progress?: UploadStatus['progress']
  error?: string
  boreholes?: UploadStatus['boreholes']
}

export const pending = (s: Status) => s === 'uploading' || s === 'queued' || s === 'processing'

/** Uploads in batches, from the file chooser or dropped on the page, with their progress on the server. */
export function useUpload(onUploaded: () => void, onSelect: (id: string) => void) {
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
            update(b.key, { status: s.status, ahead: s.ahead, progress: s.progress, error: s.error ?? undefined, boreholes: s.boreholes })
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

  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const onFileChosen = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) addFiles(e.target.files)
    e.target.value = ''  // allow re-choosing the same files later
  }
  const dropTarget = {
    onDragOver: (e: DragEvent) => { if (e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true) } },
    onDragLeave: (e: DragEvent) => { if (!e.relatedTarget) setDragging(false) },
    onDrop: (e: DragEvent) => {
      e.preventDefault()
      setDragging(false)
      addFiles(e.dataTransfer.files)
    },
  }
  return { batches, dismiss, active: batches.some((b) => pending(b.status)), dragging, fileInput, onFileChosen, dropTarget }
}
