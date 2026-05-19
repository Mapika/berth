import { eventSourceUrl } from './api'

type EventSourceHandlers = {
  onMessage?: (event: MessageEvent) => void
  onError?: (event: Event, source: EventSource) => void
  onOpenError?: (error: Error) => void
  listeners?: Record<string, (event: MessageEvent) => void>
}

export function openAdminEventSource(
  path: string,
  handlers: EventSourceHandlers,
): () => void {
  let closed = false
  let source: EventSource | null = null

  eventSourceUrl(path)
    .then(url => {
      if (closed) return
      source = new EventSource(url)
      if (handlers.onMessage) source.onmessage = handlers.onMessage
      if (handlers.onError) {
        source.onerror = event => handlers.onError?.(event, source!)
      }
      for (const [eventName, listener] of Object.entries(handlers.listeners ?? {})) {
        source.addEventListener(eventName, listener as EventListener)
      }
    })
    .catch(error => {
      if (closed) return
      handlers.onOpenError?.(error instanceof Error ? error : new Error(String(error)))
    })

  return () => {
    closed = true
    source?.close()
  }
}
