import * as React from 'react'
import { wsUrl } from './api'
import type { GpuSnapshot, JobMetricsSummary, JobProgress, JobStatus, MetricPoint, SystemMetrics } from './types'

const MAX_POINTS = 600
const MAX_LOG_CHARS = 800_000

/** Live system telemetry with a bounded ring buffer and a pause control. */
export function useSystemStream(enabled = true) {
  const [points, setPoints] = React.useState<MetricPoint[]>([])
  const [latest, setLatest] = React.useState<SystemMetrics | null>(null)
  const [gpu, setGpu] = React.useState<GpuSnapshot | null>(null)
  const [connected, setConnected] = React.useState(false)
  const [paused, setPaused] = React.useState(false)
  const pausedRef = React.useRef(paused)
  pausedRef.current = paused

  React.useEffect(() => {
    if (!enabled) return
    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let closed = false

    const connect = () => {
      socket = new WebSocket(wsUrl('/ws/system'))
      socket.onopen = () => setConnected(true)
      socket.onclose = () => {
        setConnected(false)
        if (!closed) retry = setTimeout(connect, 3000)
      }
      socket.onerror = () => socket?.close()
      socket.onmessage = (event) => {
        const payload = JSON.parse(event.data)
        if (payload.type === 'history') {
          setPoints(payload.points ?? [])
          setGpu(payload.gpu ?? null)
          return
        }
        if (payload.type === 'sample') {
          setGpu(payload.gpu ?? null)
          const metrics: SystemMetrics = payload.metrics
          setLatest(metrics)
          if (pausedRef.current) return
          setPoints((previous) => {
            const next = [
              ...previous,
              {
                at: metrics.at,
                cpu: metrics.cpu.percent,
                memory: metrics.memory.percent,
                gpu: metrics.gpu.avg_utilization,
                gpu_memory: metrics.gpu.avg_memory_percent,
                net_recv: metrics.network.recv_bps,
                net_sent: metrics.network.sent_bps,
                disk_read: metrics.disk.read_bps,
                disk_write: metrics.disk.write_bps,
                gpus: metrics.gpu.devices.map((device) => ({
                  index: device.index,
                  utilization: device.utilization ?? 0,
                  memory: device.memory_percent ?? 0,
                })),
              },
            ]
            return next.length > MAX_POINTS ? next.slice(next.length - MAX_POINTS) : next
          })
        }
      }
    }

    connect()
    return () => {
      closed = true
      if (retry) clearTimeout(retry)
      socket?.close()
    }
  }, [enabled])

  return { points, latest, gpu, connected, paused, setPaused }
}

export interface JobStreamState {
  log: string
  status: JobStatus | null
  progress: Partial<JobProgress> | null
  metrics: Partial<JobMetricsSummary> | null
  connected: boolean
  ended: boolean
}

/** Live job log + progress. Appends to a bounded buffer so long runs stay usable. */
export function useJobStream(jobId: string | undefined, enabled = true) {
  const [state, setState] = React.useState<JobStreamState>({
    log: '',
    status: null,
    progress: null,
    metrics: null,
    connected: false,
    ended: false,
  })

  React.useEffect(() => {
    if (!jobId || !enabled) return
    setState({ log: '', status: null, progress: null, metrics: null, connected: false, ended: false })

    let socket: WebSocket | null = null
    let retry: ReturnType<typeof setTimeout> | null = null
    let closed = false

    const connect = () => {
      socket = new WebSocket(wsUrl(`/ws/jobs/${jobId}/log`))
      socket.onopen = () => setState((previous) => ({ ...previous, connected: true }))
      socket.onclose = () => {
        setState((previous) => ({ ...previous, connected: false }))
        if (!closed) retry = setTimeout(connect, 4000)
      }
      socket.onerror = () => socket?.close()
      socket.onmessage = (event) => {
        const payload = JSON.parse(event.data)
        setState((previous) => {
          if (payload.type === 'log') {
            const merged = previous.log + payload.content
            return {
              ...previous,
              log: merged.length > MAX_LOG_CHARS ? merged.slice(merged.length - MAX_LOG_CHARS) : merged,
            }
          }
          if (payload.type === 'status') {
            return {
              ...previous,
              status: payload.status,
              progress: payload.progress,
              metrics: payload.metrics,
            }
          }
          if (payload.type === 'end') {
            closed = true
            socket?.close()
            return { ...previous, status: payload.status, ended: true }
          }
          return previous
        })
      }
    }

    connect()
    return () => {
      closed = true
      if (retry) clearTimeout(retry)
      socket?.close()
    }
  }, [jobId, enabled])

  return state
}
